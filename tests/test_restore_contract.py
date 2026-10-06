"""Actual pinned restore/platform contracts, inside the ordinary locked suites."""

import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers import restore_state as rs
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockModule,
    MockPlatform,
    async_mock_restore_state_shutdown_restart,
    mock_integration,
    mock_platform,
)

from tools.issue25.test_restore import (
    LifecycleReads,
    dependency_provenance,
    load_after_removal,
    observe_lifecycle,
    trace,
)


class InventedRestoreSensor(SensorEntity, rs.RestoreEntity):
    _attr_should_poll = False

    def __init__(self, name, failure=None):
        self.entity_id = f"sensor.invented_restore_{name}"
        self._attr_unique_id = f"invented_restore_{name}"
        self._attr_name = f"Invented restore {name}"
        self._attr_native_value = "cycle-0"
        self._attr_available = True
        self.failure = failure
        self.finished = False
        self.removed = False
        self.state_answer = self.extra_answer = None

    @property
    def extra_restore_state_data(self):
        return rs.RestoredExtraData({"cycle_token": self._attr_native_value})

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.state_answer = await self.async_get_last_state()
        if self.failure is not None:
            raise self.failure
        self.extra_answer = await self.async_get_last_extra_data()
        self.finished = True

    async def async_will_remove_from_hass(self):
        await super().async_will_remove_from_hass()
        self.removed = True


async def platform(hass, entities):
    callbacks = []

    async def setup_platform(hass, config, async_add_entities, discovery_info=None):
        callbacks.append(async_add_entities)
        async_add_entities(entities)

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.sensor", MockPlatform(async_setup_platform=setup_platform))
    result = await async_setup_component(hass, "sensor", {"sensor": {"platform": "test"}})
    await hass.async_block_till_done()
    assert len(callbacks) == 1, "UNKNOWN: platform add callback missing/duplicate"
    return result, callbacks[0]


async def remove_owned(hass, entities):
    for entity in entities:
        if not entity.removed and entity.entity_id in rs.async_get(hass).entities:
            await entity.async_remove()
    await hass.async_block_till_done()
    remaining = set(rs.async_get(hass).entities).intersection(e.entity_id for e in entities)
    trace("dependency-cleanup", remaining=sorted(remaining),
          unfinished=[e.entity_id for e in entities if not e.finished],
          tasks=sorted(task.get_name() for task in asyncio.all_tasks() if not task.done()))
    assert not remaining, "UNKNOWN: owned restore registration remains"
    # hass/phcc own final shutdown and timer/task verification. Never clear them.


async def test_real_restore_serialization_and_getters_twice(hass, hass_storage):
    dependency_provenance()
    created = datetime.now(timezone.utc)
    current = InventedRestoreSensor("cycle")
    owned = [current]
    try:
        initial = LifecycleReads()
        with ExitStack() as stack:
            observe_lifecycle(stack, initial, InventedRestoreSensor,
                              lambda entity: isinstance(entity, InventedRestoreSensor))
            setup, add = await platform(hass, [current])
        initial.validate({current.entity_id: None}, {current.entity_id})
        assert setup and current.finished
        sid = current.entity_id
        for cycle in (1, 2):
            current._attr_native_value = f"cycle-{cycle}"
            current._attr_available = cycle == 1
            current.async_write_ha_state()
            await hass.async_block_till_done()
            published = hass.states.get(sid)
            assert published.state == ("cycle-1" if cycle == 1 else "unavailable")
            await async_mock_restore_state_shutdown_restart(hass)
            raw = deepcopy(hass_storage["core.restore_state"])
            record = next(r for r in raw["data"] if r["state"]["entity_id"] == sid)
            assert record["state"]["state"] == published.state
            assert record["extra_data"] == {"cycle_token": f"cycle-{cycle}"}
            await current.async_remove()
            await hass.async_block_till_done()
            assert current.removed
            supplied = await load_after_removal(rs.async_get(hass), raw, [sid], created)
            assert json.loads(supplied[sid].state.as_dict_json) == record["state"]
            current = InventedRestoreSensor("cycle")
            owned.append(current)
            observer = LifecycleReads()
            with ExitStack() as stack:
                observe_lifecycle(stack, observer, InventedRestoreSensor,
                                  lambda entity: isinstance(entity, InventedRestoreSensor))
                add([current])
                await hass.async_block_till_done()
            observer.validate(supplied, {sid})
            assert current.finished and current.entity_id == sid
            assert current.state_answer is supplied[sid].state
            assert current.extra_answer is supplied[sid].extra_data
            assert current.extra_answer.as_dict() == record["extra_data"]
            assert hass_storage["core.restore_state"] == raw
            trace("dependency-cycle", cycle=cycle, record=record, completed=current.finished)
    finally:
        await remove_owned(hass, owned)


async def test_platform_add_failure_is_unknown_after_real_getter(hass, hass_storage):
    failure = RuntimeError("invented entity add failure")
    bad = InventedRestoreSensor("failure", failure)
    good = InventedRestoreSensor("companion")
    observer = LifecycleReads()
    propagated = None
    try:
        with ExitStack() as stack:
            observe_lifecycle(stack, observer, InventedRestoreSensor,
                              lambda entity: isinstance(entity, InventedRestoreSensor))
            try:
                setup, _ = await platform(hass, [good, bad])
            except RuntimeError as error:
                assert error is failure
                propagated = error
                setup = None
            await hass.async_block_till_done()
        assert good.finished and not bad.finished
        assert hass.states.get(good.entity_id) is not None
        add_failure = [c for c in observer.calls if c["entity"] is bad and c["kind"] == "add"]
        assert len(add_failure) == 1 and add_failure[0]["exception"] is failure
        assert any(c["entity"] is bad and c["kind"] == "state" and c["complete"] for c in observer.calls)
        with pytest.raises(AssertionError, match="UNKNOWN"):
            observer.validate({good.entity_id: None, bad.entity_id: None}, {good.entity_id})
        trace("dependency-add-failure", setup=setup, propagated=propagated is failure,
              state_visible=hass.states.get(bad.entity_id) is not None,
              companion_complete=good.finished, rejected="UNKNOWN")
    finally:
        await remove_owned(hass, [good, bad])
