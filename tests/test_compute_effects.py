"""The compute decides; the coordinator performs what it decided (audit M4).

A compute that pauses a room for its open window, trips a disabled room's
freeze floor, puts back a Dry when a pause ends, or finds a cloud head out of
MXZ's control decides a notice, a store write, a head command or a repair
issue. None of them is made while the compute runs: each comes back from it
and is made right after it, in the order it was decided, before the plan's
own commands. What people receive is the same as before, in the same order.

The recorder below watches Home Assistant's own entry points (a task, a
service call, a store write, a repair issue) while the coordinator's
``_compute`` runs, so it sees an effect however it is made.

Every room, head and sensor is invented; nothing here is a hardware result.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceRegistry
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.mxz_coordinator.const import (
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_ZONES,
    DOMAIN,
    HEAD_TYPE_CLOUD,
    IDLE_ACTION_FAN_ONLY,
    ZONE_ALLOW_DRY,
    ZONE_CLIMATE,
    ZONE_FLOOR_LOW,
    ZONE_HEAD_TYPE,
    ZONE_NAME,
    ZONE_OPENING_NOTIFY,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from custom_components.mxz_coordinator.openings import async_load_openings
from tests.cloud_heads import (
    MELCLOUD,
    MelcloudHead,
    past_startup,
    recompute,
    register,
    setup_rooms,
)
from tests.test_cloud_control import _advance, _issue, _standoff
from tests.test_door_window import CLOSE, OPEN, PAUSE_NOTICE, WINDOW, WINDOW_ON
from tests.test_door_window import _setup as door_window_setup
from tests.test_drive import MockHead, _eid, _set_target, _set_temp
from tests.test_dry_mode import DryHead

NOTIFY = "effects_test"
SENSORS = ("sensor.study_temp", "sensor.den_temp")


class ComputeRecorder:
    """What reached Home Assistant while any coordinator's ``_compute`` ran."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.made: list[str] = []
        self._depth = 0
        compute = MXZCoordinator._compute

        def watched(coordinator: MXZCoordinator) -> Any:
            self._depth += 1
            try:
                return compute(coordinator)
            finally:
                self._depth -= 1

        monkeypatch.setattr(MXZCoordinator, "_compute", watched)
        for owner, name in (
            (HomeAssistant, "async_create_task"),
            (HomeAssistant, "async_create_background_task"),
            (ServiceRegistry, "async_call"),
            (Store, "async_delay_save"),
            (Store, "async_save"),
            (ir.IssueRegistry, "async_get_or_create"),
            (ir.IssueRegistry, "async_delete"),
        ):
            monkeypatch.setattr(owner, name, self._watch(name, getattr(owner, name)))

    def _watch(self, name: str, original: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            if self._depth:
                self.made.append(name)
            return original(*args, **kwargs)

        return call


async def test_a_pause_and_a_floor_trip_in_one_compute_are_made_after_it_in_order(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Study's window has been open past its delay when Den's reading drops
    below its freeze floor: the same compute pauses Study and trips Den."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [MockHead("a"), MockHead("b")]
    await register(hass, {"esphome": heads})
    hass.states.async_set(WINDOW, "off", {"friendly_name": "Study window"})
    await _set_temp(hass, SENSORS[0], 76)
    await _set_temp(hass, SENSORS[1], 60)
    notices = async_mock_service(hass, "notify", NOTIFY)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_ZONES: [
                {ZONE_NAME: "Study", ZONE_CLIMATE: heads[0].entity_id, ZONE_SENSOR: SENSORS[0],
                 **WINDOW_ON, ZONE_OPENING_NOTIFY: True},
                {ZONE_NAME: "Den", ZONE_CLIMATE: heads[1].entity_id, ZONE_SENSOR: SENSORS[1],
                 ZONE_FLOOR_LOW: 50},
            ],
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_MODE_HYSTERESIS: 0,
            CONF_NOTIFY_SERVICE: f"notify.{NOTIFY}",
        },
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 70)
    for suffix in ("_primary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True,
        )
    await hass.async_block_till_done()
    await recompute(hass, entry)
    recorder = ComputeRecorder(monkeypatch)

    hass.states.async_set(WINDOW, "on", {"friendly_name": "Study window"})
    await hass.async_block_till_done()
    # The open delay passes with no wakeup fired; Den's new reading brings the
    # compute that finds both.
    freezer.tick(timedelta(seconds=OPEN + 1))
    await _set_temp(hass, SENSORS[1], 45)
    await hass.async_block_till_done()

    messages = [call.data["message"] for call in notices]
    assert messages[0] == PAUSE_NOTICE
    assert messages[1].startswith("Den is at 45 °F, below its freeze floor of 50 °F.")
    assert len(messages) == 2
    stored = await async_load_openings(hass, entry.entry_id)
    assert stored[heads[0].entity_id]["state"] == "paused"
    assert recorder.made == []


async def test_a_dry_put_back_on_resume_is_sent_after_the_compute(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await door_window_setup(
        hass, freezer, temps=(70, 70), head_cls=DryHead, study={**WINDOW_ON, ZONE_ALLOW_DRY: True},
    )
    await rig.person("dry")
    await rig.recompute()
    await rig.open_and_pause()
    assert rig.head() == "off"
    rig.forget()
    recorder = ComputeRecorder(monkeypatch)

    await rig.window("off")
    await rig.later(CLOSE + 1)

    assert rig.modes_sent() == ["dry"]
    assert rig.head() == "dry"
    assert recorder.made == []


async def test_a_cloud_heads_repair_issue_is_written_after_the_compute(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, entry = await _standoff(hass)
    recorder = ComputeRecorder(monkeypatch)

    a.go_unavailable()
    await hass.async_block_till_done()
    await _advance(hass, freezer, 15 * 60)
    b.poll()
    await recompute(hass, entry)
    assert _issue(hass, entry, a.entity_id) is not None

    a.go_unavailable(False)
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert _issue(hass, entry, a.entity_id) is None

    assert recorder.made == []


async def test_a_cloud_head_out_before_setup_gets_its_repair_issue_at_setup(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The seed compute in setup finds the head out; what it decided is made too."""
    hass.config.units = METRIC_SYSTEM
    heads = [MelcloudHead("a", mode="fan_only"), MelcloudHead("b", mode="fan_only")]
    await register(hass, {MELCLOUD: heads})
    heads[0].go_unavailable()
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=16))
    cloud = {ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD}
    entry = await setup_rooms(hass, [(heads[0], cloud), (heads[1], cloud)], temps=(18, 24))
    assert _issue(hass, entry, heads[0].entity_id) is not None
    await past_startup(hass, entry)
    assert _issue(hass, entry, heads[0].entity_id) is not None
    assert _issue(hass, entry, heads[1].entity_id) is None
