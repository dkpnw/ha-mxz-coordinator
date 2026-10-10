"""Startup continuity under real HA: store-backed restart regressions.

Two obligations from the issue-25 release work, both exercised through the
real lifecycle — restore state dumped and re-read through the mocked
``core.restore_state`` store, the entry unloaded and set up again with a fresh
coordinator — not through cleared dictionaries or injected restore truth:

* H1: a room caught mid-run by a restart resumes to its target with genuinely
  restored zone and coordinator ON. Missing startup evidence stays pending;
  construction and first live enable cannot adopt a hand-run head.
* F (fan ownership): a room the coordinator was driving comes back driven.
  The Fan auto switch carries echo baselines (commands and adopted observations)
  across the restart, so an eligible coordinator-written
  token reported late (after a provisional ``auto``, or once a missing speed
  arrives) is its own residue, not a hand on the fan, while a token outside
  that memory conservatively holds — except on the first reading of a room
  idling under idle_action fan_only, where any speed the boost could have set
  is the room's own (issue 25). Legacy and stale-memory false holds remain
  explicit limitations, not passing fix claims. No speed means no fan write.

Like test_registry_lifecycle, "restart" is an in-process store round trip:
not a new Home Assistant process, and not hardware. HA-only; the local
non-HA contract batch cannot run these. All 48 cumulative new parameterized
cases (including the two in test_fan_hold_restore) are locally HA UNRUN.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import restore_state as rs
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    async_mock_restore_state_shutdown_restart,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (
    CONF_DEMAND_THRESHOLD,
    CONF_FAN_BOOST_ENABLE,
    CONF_FAN_BOOST_MAX,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.switch import (
    ATTR_FAN_ON_PENDING,
    ATTR_LAST_FAN_COMMAND,
    ATTR_PRIOR_FAN_COMMAND,
    FanHoldRestoreData,
)
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    MockHead,
    _eid,
    _recompute,
    _set_fan_auto,
    _set_target,
    _set_temp,
    _setup_mock_heads,
    _user_set_fan,
)
from tests.test_idle_action import _settle_requested_refreshes

# Target 70 and the default drift of 1 for both rooms. "run" starts a run,
# "inside" is inside the drift (a fresh room coasts there, a running room
# continues), "past" is past it (a fresh room engages there).
RUN = {"cool": 75, "heat": 65}
INSIDE = {"cool": 71, "heat": 69}
NEARER = {"cool": 70.5, "heat": 69.5}
PAST = {"cool": 72, "heat": 68.9}


async def _setup(hass: HomeAssistant, **extra: Any) -> tuple[MockConfigEntry, str, str]:
    """Two mock heads, both rooms at 70 / target 70, coordinator and rooms enabled."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_MODE_HYSTERESIS: 0,  # the heat mirror needs the shared mode to flip
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await _switch(hass, _eid(hass, entry, suffix), True)
    for suffix in ("_primary_target", "_secondary_target"):
        await _set_target(hass, _eid(hass, entry, suffix), 70)
    await _recompute(hass, entry)
    return entry, head_a, head_b


async def _switch(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    await hass.services.async_call(
        "switch", "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done()


async def _shutdown(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Dump, unload, then LOAD: removal must not substitute its memory record."""
    switches = [_eid(hass, entry, f"_{room}_fan_auto") for room in ("primary", "secondary")]
    await async_mock_restore_state_shutdown_restart(hass)
    restore = rs.async_get(hass)
    raw = deepcopy(await restore.store.async_load())
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    removed = {sid: restore.last_states[sid] for sid in switches}
    await _load_store(hass, raw)
    records = {r["state"]["entity_id"]: r for r in raw}
    for sid in switches:
        loaded = restore.last_states[sid]
        assert loaded is not removed[sid]
        assert loaded.state.entity_id == sid
        assert loaded.state.state == records[sid]["state"]["state"]
        assert dict(loaded.state.attributes) == records[sid]["state"]["attributes"]
        assert loaded.state.last_updated == datetime.fromisoformat(records[sid]["state"]["last_updated"])
        assert loaded.state.last_updated >= entry.created_at
        assert (loaded.extra_data.as_dict() if loaded.extra_data else None) == records[sid].get("extra_data")


async def _load_store(hass: HomeAssistant, expected: list) -> None:
    """Cold-load the persisted input, independently of removal memory/cache."""
    restore = rs.async_get(hass)
    previous_store = restore.store
    removed = dict(restore.last_states)
    # Use HA's own Store construction (version/key/encoder), retaining the
    # active restore helper. phcc seeds only a cold Store from hass_storage.
    restore.store = rs.RestoreStateData(hass).store
    assert restore.store is not previous_store
    real_load = restore.store.async_load
    calls = []

    async def observed_load():
        assert restore.store._data is None  # observe the fixture-read branch; never clear it
        result = await real_load()
        calls.append(deepcopy(result))
        return result

    with patch.object(restore.store, "async_load", new=observed_load):
        await restore.async_load()
    assert calls == [expected]
    for record in expected:
        sid = record["state"]["entity_id"]
        loaded = restore.last_states[sid]
        assert loaded is not removed.get(sid)
        assert loaded.state.entity_id == sid
        assert loaded.state.state == record["state"]["state"]
        assert dict(loaded.state.attributes) == record["state"]["attributes"]
        assert loaded.state.last_updated == datetime.fromisoformat(record["state"]["last_updated"])
        assert (loaded.extra_data.as_dict() if loaded.extra_data else None) == record.get("extra_data")


async def _startup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Prove both Fan-auto restore getters read the supplied loaded records."""
    restore = rs.async_get(hass)
    supplied = {
        _eid(hass, entry, f"_{room}_fan_auto"): restore.last_states[
            _eid(hass, entry, f"_{room}_fan_auto")
        ] for room in ("primary", "secondary")
    }
    reads = []
    real_state = rs.RestoreEntity.async_get_last_state
    real_extra = rs.RestoreEntity.async_get_last_extra_data

    async def state_read(entity):
        result = await real_state(entity)
        if entity.entity_id in supplied:
            assert result is supplied[entity.entity_id].state
            reads.append((entity.entity_id, "state"))
        return result

    async def extra_read(entity):
        result = await real_extra(entity)
        if entity.entity_id in supplied:
            assert result is supplied[entity.entity_id].extra_data
            reads.append((entity.entity_id, "extra"))
        return result

    with (
        patch.object(rs.RestoreEntity, "async_get_last_state", new=state_read),
        patch.object(rs.RestoreEntity, "async_get_last_extra_data", new=extra_read),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    expected = {(sid, "state") for sid in supplied}
    expected.update((sid, "extra") for sid, record in supplied.items()
                    if record.state.state not in ("on", "off")
                    and record.state.last_updated >= entry.created_at)
    assert set(reads) == expected
    assert len(reads) == len(expected)


def _head(hass: HomeAssistant, entity_id: str):
    return hass.data["entity_components"]["climate"].get_entity(entity_id)


async def _report(hass: HomeAssistant, entity_id: str, fan_mode: str | None) -> None:
    """The head reports a fan speed (or none) on its own, no command behind it."""
    head = _head(hass, entity_id)
    head._attr_fan_mode = fan_mode
    head.async_write_ha_state()
    await hass.async_block_till_done()


def _engage(hass: HomeAssistant, entry: MockConfigEntry, room: int) -> str:
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    return plan.attributes["zones"][room]["engage"]


def _fan_hold(hass: HomeAssistant, entry: MockConfigEntry, room: int) -> bool:
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    return plan.attributes["zones"][room]["fan_hold"]


def _fan_writes(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Record every fan write from here on, without replacing the real service."""
    writes: list[tuple[str, str]] = []

    @callback
    def _record(event) -> None:
        if event.data["domain"] == "climate" and event.data["service"] == "set_fan_mode":
            data = event.data["service_data"]
            writes.append((data["entity_id"], data["fan_mode"]))

    hass.bus.async_listen(EVENT_CALL_SERVICE, _record)
    return writes


async def _run_b(hass: HomeAssistant, entry: MockConfigEntry, head_b: str, mode: str) -> None:
    """Start B's run, then bring it inside the drift: the run continues there."""
    await _set_temp(hass, SENSOR_B, RUN[mode])
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == mode
    await _set_temp(hass, SENSOR_B, INSIDE[mode])
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == mode  # band 0: it runs to the target
    assert _engage(hass, entry, 1) == mode


async def _finish_b(hass: HomeAssistant, entry: MockConfigEntry, head_b: str, mode: str) -> None:
    """B ran and reached its target: parked at the idle action, shared mode kept."""
    await _set_temp(hass, SENSOR_B, RUN[mode])
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == mode
    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == "fan_only"
    assert _engage(hass, entry, 1) == "satisfied"


# --- H1: an in-flight run survives a restart --------------------------------------


@pytest.mark.parametrize("mode", ["cool", "heat"])
async def test_restored_run_inside_drift_resumes_to_target(
    hass: HomeAssistant, mode: str
) -> None:
    """B restarts mid-run, inside its drift: after setup it is still running, to 70."""
    entry, head_a, head_b = await _setup(hass)
    await _run_b(hass, entry, head_b, mode)

    await _shutdown(hass, entry)
    await _startup(hass, entry)

    # The first refresh after setup, with the restored enables ON and the head
    # still reporting its run, resumes it. The other room is untouched.
    assert _engage(hass, entry, 1) == mode
    assert hass.states.get(head_b).state == mode
    assert _engage(hass, entry, 0) == "satisfied"
    assert hass.states.get(head_a).state == "fan_only"

    await _set_temp(hass, SENSOR_B, NEARER[mode])
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == mode  # still running
    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)
    assert _engage(hass, entry, 1) == "satisfied"  # the number the user set
    assert hass.states.get(head_b).state == "fan_only"


@pytest.mark.parametrize(
    ("mode", "head_before"),
    [("cool", "fan_only"), ("cool", "off"), ("heat", "fan_only")],
    ids=["cool-parked", "cool-turned-off-by-hand", "heat-parked"],
)
async def test_restored_idle_head_inside_drift_stays_satisfied(
    hass: HomeAssistant, mode: str, head_before: str
) -> None:
    """Negative: inside the drift with no run to resume, the room coasts after restart.

    "parked": B finished a run and sits at the idle action. "turned off by
    hand": B was mid-run and a person switched the head off before the
    restart — a manual OFF ends the run; nothing resumes it.
    """
    entry, _head_a, head_b = await _setup(hass)
    if head_before == "off":
        await _run_b(hass, entry, head_b, mode)
        await hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": head_b, "hvac_mode": "off"},
            blocking=True,
        )
        await hass.async_block_till_done()
    else:
        await _finish_b(hass, entry, head_b, mode)
        await _set_temp(hass, SENSOR_B, INSIDE[mode])
        await _recompute(hass, entry)
        assert _engage(hass, entry, 1) == "satisfied"
    assert hass.states.get(head_b).state == head_before

    await _shutdown(hass, entry)
    await _startup(hass, entry)

    assert _engage(hass, entry, 1) == "satisfied"
    assert hass.states.get(head_b).state != mode
    if mode == "heat":
        # Keep the heat manual-OFF control as well as the parked heat control.
        await _run_b(hass, entry, head_b, mode)
        await hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": head_b, "hvac_mode": "off"},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert hass.states.get(head_b).state == "off"
        await _shutdown(hass, entry)
        await _startup(hass, entry)
        assert _engage(hass, entry, 1) == "satisfied"
        assert hass.states.get(head_b).state != mode


@pytest.mark.parametrize("mode", ["cool", "heat"])
async def test_fresh_reading_past_drift_engages_without_prior_run(
    hass: HomeAssistant, mode: str
) -> None:
    """Control: a parked room whose reading moved past the drift during the outage engages."""
    entry, _head_a, head_b = await _setup(hass)
    await _finish_b(hass, entry, head_b, mode)

    await _shutdown(hass, entry)
    await _set_temp(hass, SENSOR_B, PAST[mode])  # while HA was down
    await _startup(hass, entry)

    assert _engage(hass, entry, 1) == mode
    assert hass.states.get(head_b).state == mode


@pytest.mark.parametrize("mode", ["cool", "heat"])
async def test_restored_disabled_room_does_not_resume(hass: HomeAssistant, mode: str) -> None:
    """A room restored DISABLED stays parked; enabling it later starts no run."""
    entry, _head_a, head_b = await _setup(hass)
    await _run_b(hass, entry, head_b, mode)
    enable_b = _eid(hass, entry, "_secondary_enable")
    await _switch(hass, enable_b, False)
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == "off"  # a disabled room is parked off
    # A hand turns the head on again just before the restart.
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": head_b, "hvac_mode": mode},
        blocking=True,
    )
    await hass.async_block_till_done()

    await _shutdown(hass, entry)
    await _startup(hass, entry)

    assert _engage(hass, entry, 1) == "off"
    assert hass.states.get(head_b).state == "off"  # parked again, not resumed

    await _switch(hass, enable_b, True)
    await _recompute(hass, entry)
    assert _engage(hass, entry, 1) == "satisfied"  # 71 is inside the drift
    assert hass.states.get(head_b).state == "fan_only"

    # Discriminator: with the coordinator OFF a hand can run the head. Neither
    # first live zone-enable nor first live coordinator-enable adopts that run.
    coordinator_enable = _eid(hass, entry, "_coordinator_enable")
    await _switch(hass, coordinator_enable, False)
    await _switch(hass, enable_b, False)
    await _shutdown(hass, entry)
    await _startup(hass, entry)
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": head_b, "hvac_mode": mode},
        blocking=True,
    )
    await _switch(hass, enable_b, True)
    assert hass.states.get(head_b).state == mode  # kill switch still OFF
    assert _engage(hass, entry, 1) == "satisfied"
    await _switch(hass, coordinator_enable, True)
    await _switch(hass, coordinator_enable, True)  # repeated live ON earns no restore
    await _recompute(hass, entry)
    assert _engage(hass, entry, 1) == "satisfied"
    assert hass.states.get(head_b).state == "fan_only"

    # Restored zone ON is insufficient when coordinator ON was not restored.
    await _switch(hass, coordinator_enable, False)
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": head_b, "hvac_mode": mode},
        blocking=True,
    )
    await hass.async_block_till_done()
    await _shutdown(hass, entry)
    await _startup(hass, entry)
    assert hass.states.get(head_b).state == mode
    assert _engage(hass, entry, 1) == "satisfied"
    await _switch(hass, coordinator_enable, True)
    await _recompute(hass, entry)
    assert _engage(hass, entry, 1) == "satisfied"
    assert hass.states.get(head_b).state == "fan_only"


# --- F: fan ownership across a restart --------------------------------------------


async def _idle_on_residue(
    hass: HomeAssistant, entry: MockConfigEntry, head_a: str
) -> Callable[[], None]:
    """A finishes a run but its handback to auto never lands: idle on 'high'.

    The coordinator's memory is then (last=auto, prior=high): the shape every
    restart under issue 25 starts from. The head keeps swallowing fan writes
    until the returned callable reinstates its real handler.
    """
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    head = _head(hass, head_a)
    real_set = head.async_set_fan_mode

    async def _swallow(fan_mode):  # accepted on the wire, never applied
        return None

    def _reinstate() -> None:
        head.async_set_fan_mode = real_set

    head.async_set_fan_mode = _swallow
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    assert _fan_hold(hass, entry, 0) is False
    switch = hass.states.get(_eid(hass, entry, "_primary_fan_auto"))
    assert switch.state == "on"
    assert switch.attributes[ATTR_LAST_FAN_COMMAND] == "auto"
    assert switch.attributes[ATTR_PRIOR_FAN_COMMAND] == "high"
    return _reinstate


def _stored(hass_storage: dict, entity_id: str) -> dict:
    data = hass_storage["core.restore_state"]["data"]
    return next(r for r in data if r["state"]["entity_id"] == entity_id)


async def test_restart_mid_ramp_keeps_boost_driving(hass: HomeAssistant) -> None:
    """Hypothesis 1 (R4 cell 06): the room cooled during the outage, so the restored
    'high' is not the ladder's rung at the new delta. Not held means it is ours:
    boost keeps driving and steps the fan down from there."""
    entry, _head_a, head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    await _set_temp(hass, SENSOR_B, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_b).attributes["fan_mode"] == "high"

    await _shutdown(hass, entry)
    await _set_temp(hass, SENSOR_B, 72)  # delta 2 now; 'high' is a delta >= 3.5 rung
    await _startup(hass, entry)

    switch_b = _eid(hass, entry, "_secondary_fan_auto")
    assert hass.states.get(switch_b).state == "on"
    assert _fan_hold(hass, entry, 1) is False
    assert hass.states.get(head_b).state == "cool"
    assert hass.states.get(head_b).attributes["fan_mode"] == "medium"

    await _set_temp(hass, SENSOR_B, 71)
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == "cool"  # H1: the run continues
    assert hass.states.get(head_b).attributes["fan_mode"] == "low"
    assert _fan_hold(hass, entry, 1) is False


async def test_provisional_auto_then_late_token_is_the_coordinators_echo(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """Hypothesis 2 (R4 cell 07): the head reports 'auto' first, then the
    pre-restart 'high' late. The restored memory says the coordinator commanded
    both, so the late token is its own echo: no hold, the handback is reissued.
    A live pick afterwards is still a hold."""
    entry, head_a, _head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    (await _idle_on_residue(hass, entry, head_a))()
    switch_a = _eid(hass, entry, "_primary_fan_auto")

    await _shutdown(hass, entry)
    record = _stored(hass_storage, switch_a)
    assert record["state"]["state"] == "on"
    assert record["state"]["attributes"][ATTR_LAST_FAN_COMMAND] == "auto"
    assert record["state"]["attributes"][ATTR_PRIOR_FAN_COMMAND] == "high"
    await _report(hass, head_a, "auto")  # provisional: the handback never landed
    await _startup(hass, entry)
    assert hass.states.get(switch_a).state == "on"
    assert _fan_hold(hass, entry, 0) is False

    writes = _fan_writes(hass)
    await _report(hass, head_a, "high")  # the late report of the pre-restart token
    await _recompute(hass, entry)
    assert hass.states.get(switch_a).state == "on"
    assert _fan_hold(hass, entry, 0) is False
    assert [w for w in writes if w[0] == head_a] == [(head_a, "auto")]
    assert hass.states.get(head_a).attributes["fan_mode"] == "auto"

    await _user_set_fan(hass, head_a, "medium")  # a hand on the fan, live
    await _recompute(hass, entry)
    assert hass.states.get(switch_a).state == "off"
    assert _fan_hold(hass, entry, 0) is True
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"


@pytest.mark.parametrize("origin", ["late-echo", "manual-pick"])
async def test_late_token_without_memory_is_still_a_departure(
    hass: HomeAssistant, hass_storage: dict, origin: str, record_property,
) -> None:
    """F7 remains unresolved: an old bool-only record has no echo provenance.

    The public manual service is an available observable distinction from the
    report-only echo. This pair proves no complete-history equivalence. The
    separate wall-report control shares the echo ingress without a service.
    Invented legacy-format Store input, NOT an old-version execution or a fix.
    """
    entry, head_a, _head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    history = _observable_history(hass, entry, head_a, None)
    (await _idle_on_residue(hass, entry, head_a))()
    switch_a = _eid(hass, entry, "_primary_fan_auto")

    await _shutdown(hass, entry)
    # Labelled legacy-format input. Load it through Store after removal; do
    # not let an in-memory injection masquerade as loaded-record evidence.
    legacy = deepcopy(hass_storage["core.restore_state"])
    record = next(r for r in legacy["data"] if r["state"]["entity_id"] == switch_a)
    record["state"]["attributes"].pop(ATTR_LAST_FAN_COMMAND)
    record["state"]["attributes"].pop(ATTR_PRIOR_FAN_COMMAND)
    record["state"]["attributes"].pop("fan_control_reason", None)
    record["extra_data"] = {"held": False}
    hass_storage["core.restore_state"] = legacy
    await _load_store(hass, legacy["data"])
    await _report(hass, head_a, "auto")
    await _startup(hass, entry)
    assert hass.states.get(switch_a).state == "on"

    history["record"] = deepcopy(record)
    history["reconnected_head"] = hass.states.get(head_a).as_dict()
    writes = _fan_writes(hass)
    if origin == "late-echo":
        await _report(hass, head_a, "high")
    else:
        await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    assert hass.states.get(switch_a).state == "off"
    assert _fan_hold(hass, entry, 0) is True
    assert writes == ([] if origin == "late-echo" else [(head_a, "high")])
    record_property("observable_history", json.dumps(history, default=str))


@pytest.mark.parametrize("held", [False, True], ids=["not-held", "held"])
async def test_missing_speed_at_restart_keeps_truth_pending(
    hass: HomeAssistant, held: bool
) -> None:
    """R4 cell 08 and its manual twin: the head comes back with no fan speed.

    No fan write while the speed is unknown; the restored truth is shown — the
    switch and the plan agree — and is reconciled only once a speed arrives:
    a not-held residue is handed back, a hold stays a hold until explicit ON.
    """
    entry, head_a, _head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    (await _idle_on_residue(hass, entry, head_a))()
    switch_a = _eid(hass, entry, "_primary_fan_auto")
    if held:
        await _set_fan_auto(hass, switch_a, False)  # pins the current speed, 'high'
        await _recompute(hass, entry)
        assert _fan_hold(hass, entry, 0) is True

    await _shutdown(hass, entry)
    await _report(hass, head_a, None)
    writes = _fan_writes(hass)
    await _startup(hass, entry)

    assert hass.states.get(head_a).attributes["fan_mode"] is None
    assert hass.states.get(switch_a).state == ("off" if held else "on")
    assert _fan_hold(hass, entry, 0) is held
    assert writes == []
    assert "Current fan speed unavailable" in hass.states.get(switch_a).attributes["fan_control_reason"]
    if held:
        await _set_fan_auto(hass, switch_a, False)  # absent speed cannot erase known OFF
        assert hass.states.get(switch_a).state == "off"
        assert _fan_hold(hass, entry, 0) is True
        assert writes == []

    # Missing capability is a separate state, not speed=None or proof of no fan.
    head = _head(hass, head_a)
    modes = head.fan_modes
    head._attr_fan_modes = None
    head.async_write_ha_state()
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert hass.states.get(switch_a).state == "unavailable"
    assert _fan_hold(hass, entry, 0) is held
    assert writes == []
    head._attr_fan_modes = modes
    head.async_write_ha_state()
    await hass.async_block_till_done()

    await _report(hass, head_a, "high")  # the speed arrives
    await _recompute(hass, entry)
    assert hass.states.get(switch_a).state == ("off" if held else "on")
    assert _fan_hold(hass, entry, 0) is held
    if held:
        assert writes == []
        assert hass.states.get(head_a).attributes["fan_mode"] == "high"
        await _set_fan_auto(hass, switch_a, True)  # the documented recovery
        await _set_temp(hass, SENSOR_A, 75)
        await _recompute(hass, entry)
        assert hass.states.get(switch_a).state == "on"
        assert _fan_hold(hass, entry, 0) is False
        assert hass.states.get(head_a).attributes["fan_mode"] == "high"  # boost's rung
        # Explicit ON must also survive when the speed is still unknown, with
        # no blind write and no reinterpretation of recovery as a manual pick.
        await _set_fan_auto(hass, switch_a, False)
        await _report(hass, head_a, None)
        writes.clear()
        await _set_fan_auto(hass, switch_a, True)
        await _recompute(hass, entry)
        assert hass.states.get(switch_a).state == "on"
        assert _fan_hold(hass, entry, 0) is False
        assert writes == []
        await _report(hass, head_a, "medium")
        await _recompute(hass, entry)
        assert hass.states.get(switch_a).state == "on"
        assert _fan_hold(hass, entry, 0) is False
        assert hass.states.get(switch_a).attributes["fan_control_reason"] is None
        assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    else:
        assert writes == [(head_a, "auto")]
        assert hass.states.get(head_a).attributes["fan_mode"] == "auto"


async def test_memory_rides_the_unavailable_record(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """The switches are unavailable at shutdown (a head command fails, so the
    refresh fails as a whole — the issue-25 harness's own schedule-02 shape):
    HA persists ``unavailable`` with no attributes, so the memory travels in
    extra restore data and the late token is still read as the coordinator's
    own."""
    entry, head_a, head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    reinstate = await _idle_on_residue(hass, entry, head_a)
    switch_a = _eid(hass, entry, "_primary_fan_auto")
    b = _head(hass, head_b)
    real_set_temperature = b.async_set_temperature

    async def _fail(**kwargs):
        raise RuntimeError("invented dependency failure")

    b.async_set_temperature = _fail
    await _set_temp(hass, SENSOR_B, 75)  # B is called; its head fails the command
    await _recompute(hass, entry)
    assert hass.states.get(switch_a).state == "unavailable"
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"  # residue kept

    await _shutdown(hass, entry)
    record = _stored(hass_storage, switch_a)
    assert record["state"]["state"] == "unavailable"
    assert record["extra_data"] == {
        "held": False, ATTR_LAST_FAN_COMMAND: "auto", ATTR_PRIOR_FAN_COMMAND: "high",
    }
    b.async_set_temperature = real_set_temperature
    reinstate()
    await _report(hass, head_a, "auto")  # provisional
    await _startup(hass, entry)
    assert hass.states.get(switch_a).state == "on"
    assert _fan_hold(hass, entry, 0) is False

    writes = _fan_writes(hass)
    await _report(hass, head_a, "high")  # the late report of the pre-restart token
    await _recompute(hass, entry)
    assert hass.states.get(switch_a).state == "on"
    assert _fan_hold(hass, entry, 0) is False
    assert [w for w in writes if w[0] == head_a] == [(head_a, "auto")]
    assert hass.states.get(head_a).attributes["fan_mode"] == "auto"


@pytest.mark.parametrize(
    ("token", "expect_hold"),
    [("medium", False), ("low", False), ("turbo", True)],
    ids=["outside-memory-idle-edge", "remembered-token-edge", "non-ladder-holds"],
)
async def test_token_changed_by_hand_during_outage(
    hass: HomeAssistant, hass_storage: dict, token: str, expect_hold: bool, record_property,
) -> None:
    """A hand on the fan while HA was down, on a room that was not held.

    The room idles in fan_only (the idle action _setup stores), so any ladder
    speed the boost could have set is read as the room's own idle (issue 25:
    the head reports such speeds by itself, and a hold there never clears).
    The documented edge: a pick of such a speed during the outage —
    remembered ('low', the rung the boost eased to) or not ('medium') — is
    handed back. A token the boost could never have written is the person's:
    it holds. An active room still holds a token outside memory
    (test_active_room_keeps_the_memory_rule_after_restart).
    """
    entry, head_a, _head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    head = _head(hass, head_a)
    head._attr_fan_modes = [*head.fan_modes, "turbo"]
    head.async_write_ha_state()
    history = _observable_history(hass, entry, head_a, None)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 71)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "low"
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "auto"  # handback landed
    switch_a = _eid(hass, entry, "_primary_fan_auto")
    assert hass.states.get(switch_a).attributes[ATTR_PRIOR_FAN_COMMAND] == "low"

    await _shutdown(hass, entry)
    history["record"] = deepcopy(_stored(hass_storage, switch_a))
    await _report(hass, head_a, token)  # the wall remote, during the outage
    writes = _fan_writes(hass)
    await _startup(hass, entry)

    assert hass.states.get(switch_a).state == ("off" if expect_hold else "on")
    assert _fan_hold(hass, entry, 0) is expect_hold
    if expect_hold:
        assert writes == []
        assert hass.states.get(head_a).attributes["fan_mode"] == token
    else:
        assert writes == [(head_a, "auto")]
        assert hass.states.get(head_a).attributes["fan_mode"] == "auto"
    record_property("observable_history", json.dumps(history, default=str))


async def _idle_after_boost(hass: HomeAssistant, entry: MockConfigEntry, head: str, sensor: str) -> None:
    """The room ran a boost to 'high', reached its target and idles in fan_only.

    The handback landed: the head reports auto and the record's memory is
    (last=auto, prior=high), the state after any ordinary run.
    """
    await _set_temp(hass, sensor, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head).attributes["fan_mode"] == "high"
    await _set_temp(hass, sensor, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head).state == "fan_only"
    assert hass.states.get(head).attributes["fan_mode"] == "auto"


@pytest.mark.parametrize(
    "token", ["low", "high"], ids=["token-outside-memory", "token-in-memory"],
)
async def test_idle_fan_only_room_reads_its_own_speed_after_restart(
    hass: HomeAssistant, hass_storage: dict, token: str,
) -> None:
    """Issue 25 on an ordinary restart with a saved record (3.4.0's steady state).

    An established room idling under idle_action fan_only comes back with its
    head reporting a speed of its own. Whether or not the token is one the
    record remembers, it is a speed the boost could have set on a room the
    coordinator idles, not a hand on the fan: no hold, and the handback is
    reissued.
    """
    entry, head_a, _head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    entry.created_at = dt_util.utcnow() - timedelta(days=30)
    await _idle_after_boost(hass, entry, head_a, SENSOR_A)
    switch_a = _eid(hass, entry, "_primary_fan_auto")

    await _shutdown(hass, entry)
    record = _stored(hass_storage, switch_a)
    assert record["state"]["state"] == "on"
    assert record["state"]["attributes"][ATTR_LAST_FAN_COMMAND] == "auto"
    assert record["state"]["attributes"][ATTR_PRIOR_FAN_COMMAND] == "high"
    await _report(hass, head_a, token)  # the head's own reading on reconnect
    writes = _fan_writes(hass)
    await _startup(hass, entry)

    assert hass.states.get(switch_a).state == "on"
    assert _fan_hold(hass, entry, 0) is False
    assert writes == [(head_a, "auto")]
    assert hass.states.get(head_a).state == "fan_only"
    assert hass.states.get(head_a).attributes["fan_mode"] == "auto"

    # Later restarts are ordinary restarts too: the same reading again, no hold.
    await _shutdown(hass, entry)
    await _report(hass, head_a, token)
    await _startup(hass, entry)
    assert _fan_hold(hass, entry, 0) is False


@pytest.mark.parametrize("token", ["turbo", "high"], ids=["non-ladder", "above-ceiling"])
async def test_idle_restart_still_holds_what_boost_could_not_have_set(
    hass: HomeAssistant, token: str,
) -> None:
    """The fan_only-idle reading is narrow: a token outside the ladder or above
    the ceiling is not one the boost could have set, and holds."""
    entry, head_a, _head_b = await _setup(
        hass, **{CONF_FAN_BOOST_ENABLE: True, CONF_FAN_BOOST_MAX: "medium"},
    )
    entry.created_at = dt_util.utcnow() - timedelta(days=30)
    head = _head(hass, head_a)
    head._attr_fan_modes = [*head.fan_modes, "turbo"]
    head.async_write_ha_state()
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "auto"
    switch_a = _eid(hass, entry, "_primary_fan_auto")

    await _shutdown(hass, entry)
    await _report(hass, head_a, token)
    writes = _fan_writes(hass)
    await _startup(hass, entry)
    assert hass.states.get(switch_a).state == "off"
    assert _fan_hold(hass, entry, 0) is True
    assert writes == []
    assert hass.states.get(head_a).attributes["fan_mode"] == token


@pytest.mark.parametrize(
    ("idle_action", "expect_hold"),
    [(IDLE_ACTION_FAN_ONLY, False), (IDLE_ACTION_OFF, True)],
    ids=["idle-fan-only", "idle-off-keeps-memory-rule"],
)
async def test_room_satisfied_during_outage_by_idle_action(
    hass: HomeAssistant, idle_action: str, expect_hold: bool,
) -> None:
    """The room reached its target while HA was down; its head still cools and
    reports a rung outside the record's memory (medium, auto).

    Under idle_action fan_only the room now idles in fan_only, so the rung is
    its own idle. Under idle_action off the head is handed back through
    fan_only before parking, and that reading keeps the memory rule: it holds.
    """
    entry, head_a, _head_b = await _setup(
        hass, **{CONF_FAN_BOOST_ENABLE: True, CONF_FAN_BOOST_MAX: "medium",
                 CONF_IDLE_ACTION: idle_action},
    )
    entry.created_at = dt_util.utcnow() - timedelta(days=30)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"
    switch_a = _eid(hass, entry, "_primary_fan_auto")

    await _shutdown(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _report(hass, head_a, "low")
    assert hass.states.get(head_a).state == "cool"
    writes = _fan_writes(hass)
    await _startup(hass, entry)
    assert hass.states.get(switch_a).state == ("off" if expect_hold else "on")
    assert _fan_hold(hass, entry, 0) is expect_hold
    if expect_hold:
        assert writes == []
        assert hass.states.get(head_a).state == "off"
        assert hass.states.get(head_a).attributes["fan_mode"] == "low"
    else:
        assert writes == [(head_a, "auto")]
        assert hass.states.get(head_a).state == "fan_only"


@pytest.mark.parametrize(
    ("token", "expect_hold"),
    [("middle", True), ("high", False)],
    ids=["outside-memory-holds", "commanded-rung-keeps-driving"],
)
async def test_active_room_keeps_the_memory_rule_after_restart(
    hass: HomeAssistant, token: str, expect_hold: bool,
) -> None:
    """Control for the idle reading: a room still cooling at restart is matched
    against the record's memory (high, medium), as before."""
    entry, head_a, _head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    entry.created_at = dt_util.utcnow() - timedelta(days=30)
    await _set_temp(hass, SENSOR_A, 72)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    switch_a = _eid(hass, entry, "_primary_fan_auto")
    assert hass.states.get(switch_a).attributes[ATTR_PRIOR_FAN_COMMAND] == "medium"

    await _shutdown(hass, entry)
    await _report(hass, head_a, token)
    writes = _fan_writes(hass)
    await _startup(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    assert hass.states.get(switch_a).state == ("off" if expect_hold else "on")
    assert _fan_hold(hass, entry, 0) is expect_hold
    assert [w for w in writes if w[0] == head_a] == []
    assert hass.states.get(head_a).attributes["fan_mode"] == token


async def _setup_four(hass: HomeAssistant) -> tuple[MockConfigEntry, list[str], list[str]]:
    """Four heads on one outdoor unit, boost on, every room at its target."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [MockHead(name) for name in "abcd"]
    sensors = [f"sensor.room_{name}_temp" for name in "abcd"]

    async def _setup_platform(hass, config, async_add_entities, discovery_info=None):
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=_setup_platform))
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    await hass.async_block_till_done()
    for sensor in sensors:
        await _set_temp(hass, sensor, 70)
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="MXZ Coordinator",
        data={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_ZONES: [
                {ZONE_NAME: f"Room {i + 1}", ZONE_CLIMATE: head.entity_id, ZONE_SENSOR: sensor}
                for i, (head, sensor) in enumerate(zip(heads, sensors, strict=True))
            ],
            CONF_FAN_BOOST_ENABLE: True,
            CONF_MODE_HYSTERESIS: 0,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    slugs = ("primary", "secondary", "zone_3", "zone_4")
    for slug in slugs:
        await _switch(hass, _eid(hass, entry, f"_{slug}_enable"), True)
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), True)
    for slug in slugs:
        await _set_target(hass, _eid(hass, entry, f"_{slug}_target"), 70)
    await _recompute(hass, entry)
    return entry, [head.entity_id for head in heads], sensors


async def test_reporter_restart_four_rooms_three_idle(hass: HomeAssistant) -> None:
    """Issue 25 as reported: four rooms, three idling in fan_only, one cooling,
    an ordinary restart. 3.3.0 held the three idle rooms; none is held now, and
    the cooling room keeps being driven from its rung."""
    entry, heads, sensors = await _setup_four(hass)
    entry.created_at = dt_util.utcnow() - timedelta(days=30)
    for sensor in sensors:
        await _set_temp(hass, sensor, 75)
    await _recompute(hass, entry)
    assert [hass.states.get(h).attributes["fan_mode"] for h in heads] == ["high"] * 4
    for sensor in sensors[:3]:
        await _set_temp(hass, sensor, 70)
    await _recompute(hass, entry)
    assert [hass.states.get(h).state for h in heads] == ["fan_only"] * 3 + ["cool"]
    assert [hass.states.get(h).attributes["fan_mode"] for h in heads] == ["auto"] * 3 + ["high"]

    await _shutdown(hass, entry)
    for head, token in zip(heads, ("low", "quiet", "medium", "high"), strict=True):
        await _report(hass, head, token)
    writes = _fan_writes(hass)
    await _startup(hass, entry)

    assert [_fan_hold(hass, entry, room) for room in range(4)] == [False] * 4
    assert sorted(writes) == sorted((head, "auto") for head in heads[:3])
    assert [hass.states.get(h).state for h in heads] == ["fan_only"] * 3 + ["cool"]
    assert [hass.states.get(h).attributes["fan_mode"] for h in heads] == ["auto"] * 3 + ["high"]
    await _set_temp(hass, sensors[3], 71)
    await _recompute(hass, entry)
    assert hass.states.get(heads[3]).attributes["fan_mode"] == "low"


@pytest.mark.parametrize(
    "fan_auto_record", ["on", "unavailable"], ids=["clean-record", "no-usable-record"],
)
async def test_upgrade_restart_from_330_records(
    hass: HomeAssistant, hass_storage: dict, fan_auto_record: str,
) -> None:
    """The first restart after upgrading from 3.3.0 on an established entry.

    3.3.0 stored the Fan auto switch as a bare on/off with no memory and no
    extra data, and the coordinator switch under the same unique id. A clean
    record restores not-held with no memory; an unavailable one is no record,
    and the coordinator switch's 3.3.0 ON record is what marks the previous
    run. Either way the idle room's own reading is not a hold.
    """
    entry, head_a, _head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    entry.created_at = dt_util.utcnow() - timedelta(days=30)
    await _idle_after_boost(hass, entry, head_a, SENSOR_A)
    switch_a = _eid(hass, entry, "_primary_fan_auto")

    await _shutdown(hass, entry)
    # Labelled 3.3.0-format input, loaded through Store after removal.
    legacy = deepcopy(hass_storage["core.restore_state"])
    record = next(r for r in legacy["data"] if r["state"]["entity_id"] == switch_a)
    for key in (ATTR_LAST_FAN_COMMAND, ATTR_PRIOR_FAN_COMMAND, "fan_control_reason"):
        record["state"]["attributes"].pop(key, None)
    record["state"]["state"] = fan_auto_record
    record.pop("extra_data", None)
    enable = _eid(hass, entry, "_coordinator_enable")
    assert next(r for r in legacy["data"] if r["state"]["entity_id"] == enable)["state"]["state"] == "on"
    hass_storage["core.restore_state"] = legacy
    await _load_store(hass, legacy["data"])
    await _report(hass, head_a, "low")
    writes = _fan_writes(hass)
    await _startup(hass, entry)

    assert entry.runtime_data._restored_coordinator_on is True
    assert hass.states.get(switch_a).state == "on"
    assert _fan_hold(hass, entry, 0) is False
    assert writes == [(head_a, "auto")]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"held": False}, FanHoldRestoreData(False)),  # a record from before the memory
        (
            {"held": True, ATTR_LAST_FAN_COMMAND: 3, ATTR_PRIOR_FAN_COMMAND: None},
            FanHoldRestoreData(True),  # a token that is not a string is no memory
        ),
        ({"held": "off", ATTR_LAST_FAN_COMMAND: "auto"}, None),  # no clean bool: no answer
    ],
    ids=["old-record", "malformed-token", "malformed-held"],
)
def test_restore_record_memory_is_tolerant(record: dict, expected) -> None:
    """Old, partial and malformed records restore the bool alone, or nothing."""
    assert FanHoldRestoreData.from_dict(record) == expected
    assert FanHoldRestoreData(False, "auto", "high").as_dict() == {
        "held": False, ATTR_LAST_FAN_COMMAND: "auto", ATTR_PRIOR_FAN_COMMAND: "high",
    }


@pytest.mark.parametrize(
    ("mode", "missing"),
    [("cool", "absent-head"), ("heat", "unavailable-head"),
     ("cool", "unknown-head"), ("cool", "sensor"), ("heat", "sensor")],
)
async def test_restored_run_waits_for_late_evidence(
    hass: HomeAssistant, mode: str, missing: str,
) -> None:
    """Unknown startup evidence is not a spent run; usable 71/69 resumes to 70."""
    entry, head_a, head_b = await _setup(hass)
    await _run_b(hass, entry, head_b, mode)
    await _shutdown(hass, entry)
    head = _head(hass, head_b)
    if missing == "sensor":
        hass.states.async_set(SENSOR_B, "unavailable")
    elif missing == "absent-head":
        hass.states.async_remove(head_b)
    else:
        hass.states.async_set(head_b, missing.removesuffix("-head"))
    await hass.async_block_till_done()
    await _startup(hass, entry)
    assert _engage(hass, entry, 0) == "satisfied"
    assert hass.states.get(head_a).state == "fan_only"
    global_enable = _eid(hass, entry, "_coordinator_enable")
    await _switch(hass, global_enable, True)  # genuine restored ON -> ON
    await _switch(hass, global_enable, True)
    if missing == "sensor":
        assert hass.states.get(head_b).state == "fan_only"  # invalid sensor still parks
        # Sensor recovery alone cannot spend the pending seed if the head has
        # meanwhile disappeared. Both inputs must be usable together.
        hass.states.async_remove(head_b)
        await hass.async_block_till_done()
        await _switch(hass, global_enable, True)  # still waiting for the head
        await _set_temp(hass, SENSOR_B, INSIDE[mode])
        await _recompute(hass, entry)
        assert hass.states.get(head_b) is None
        head.async_write_ha_state()  # our earlier fan_only park, not a new hand run
        await hass.async_block_till_done()
    else:
        assert hass.states.get(head_b) is None or hass.states.get(head_b).state in ("unknown", "unavailable")
        head.async_write_ha_state()  # original cool/heat report arrives unchanged
        await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _engage(hass, entry, 1) == mode
    assert hass.states.get(head_b).state == mode
    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)
    assert _engage(hass, entry, 1) == "satisfied"
    assert hass.states.get(head_b).state == "fan_only"

    if missing == "sensor":
        # Manual OFF while the sensor is missing also cancels remembered run
        # evidence. It differs from the coordinator's own fan_only park.
        await _run_b(hass, entry, head_b, mode)
        await _shutdown(hass, entry)
        hass.states.async_set(SENSOR_B, "unavailable")
        await hass.async_block_till_done()
        await _startup(hass, entry)
        await hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": head_b, "hvac_mode": "off"},
            blocking=True,
        )
        await hass.async_block_till_done()
        await _switch(hass, global_enable, True)  # cannot undo the manual OFF
        await _set_temp(hass, SENSOR_B, INSIDE[mode])
        await _recompute(hass, entry)
        assert _engage(hass, entry, 1) == "satisfied"
        assert hass.states.get(head_b).state == "fan_only"

        # A changed target while waiting invalidates the pending startup run.
        await _run_b(hass, entry, head_b, mode)
        await _shutdown(hass, entry)
        hass.states.async_set(SENSOR_B, "unavailable")
        await hass.async_block_till_done()
        await _startup(hass, entry)
        await _set_target(hass, _eid(hass, entry, "_secondary_target"), NEARER[mode])
        await _switch(hass, global_enable, True)  # cannot resurrect a retargeted run
        await _set_temp(hass, SENSOR_B, INSIDE[mode])
        await _recompute(hass, entry)
        assert _engage(hass, entry, 1) == "satisfied"
        assert hass.states.get(head_b).state == "fan_only"

        # A real global OFF spends the restored intent. First live ON and
        # repeated ON cannot recreate it while the sensor is still unknown.
        await _set_target(hass, _eid(hass, entry, "_secondary_target"), 70)
        await _run_b(hass, entry, head_b, mode)
        await _shutdown(hass, entry)
        hass.states.async_set(SENSOR_B, "unavailable")
        await hass.async_block_till_done()
        await _startup(hass, entry)
        await _switch(hass, global_enable, False)
        await _switch(hass, global_enable, True)
        await _switch(hass, global_enable, True)
        await _set_temp(hass, SENSOR_B, INSIDE[mode])
        await _recompute(hass, entry)
        assert _engage(hass, entry, 1) == "satisfied"
        assert hass.states.get(head_b).state == "fan_only"


@pytest.mark.parametrize("token", ["high", "turbo"], ids=["above-ceiling", "non-ladder"])
async def test_remembered_user_token_never_waives_ceiling(
    hass: HomeAssistant, token: str,
) -> None:
    """ON adopts user observations too: persistence must not turn those into authority."""
    entry, head_a, _head_b = await _setup(
        hass, **{CONF_FAN_BOOST_ENABLE: True, CONF_FAN_BOOST_MAX: "medium"},
    )
    head = _head(hass, head_a)
    head._attr_fan_modes = [*head.fan_modes, "turbo"]
    head.async_write_ha_state()
    await _user_set_fan(hass, head_a, token)
    await _recompute(hass, entry)
    switch = _eid(hass, entry, "_primary_fan_auto")
    assert hass.states.get(switch).state == "off"
    await _set_fan_auto(hass, switch, True)
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "on"
    assert hass.states.get(switch).attributes[ATTR_PRIOR_FAN_COMMAND] == token

    await _shutdown(hass, entry)
    await _report(hass, head_a, token)
    writes = _fan_writes(hass)
    await _startup(hass, entry)
    assert hass.states.get(switch).state == "off"
    assert _fan_hold(hass, entry, 0) is True
    assert writes == []
    assert hass.states.get(head_a).attributes["fan_mode"] == token
    await _set_fan_auto(hass, switch, True)
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "on"
    assert hass.states.get(switch).attributes[ATTR_PRIOR_FAN_COMMAND] == token
    await _shutdown(hass, entry)
    # Test the late path as well as the first restored observation: auto must
    # not arm echo tolerance for the remembered impossible user token.
    await _report(hass, head_a, "auto")
    await _startup(hass, entry)
    writes = _fan_writes(hass)
    await _report(hass, head_a, token)
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "off"
    assert _fan_hold(hass, entry, 0) is True
    assert writes == []
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert writes == []
    assert hass.states.get(head_a).attributes["fan_mode"] == token


@pytest.mark.parametrize("room", ["active", "idle"])
async def test_older_store_cannot_prove_a_later_rung_is_owned(
    hass: HomeAssistant, hass_storage: dict, room: str, record_property,
) -> None:
    """F4 limitation: actual dump predates later commands, despite valid identity.

    This models an older surviving periodic record, not the timing/frequency of
    a real crash. In a room still cooling it intentionally documents a
    conservative false hold; the fresh manual outside-memory control forbids
    silently broadening adoption there. A room idling in fan_only (the idle
    action _setup stores) needs no proof: the boost's rung is read as its own
    idle (issue 25), so the older record cannot cause a hold.
    """
    entry, head_a, _head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    history = _observable_history(hass, entry, head_a, None)
    switch = _eid(hass, entry, "_primary_fan_auto")
    await _set_temp(hass, SENSOR_A, 72)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"
    await async_mock_restore_state_shutdown_restart(hass)
    older = deepcopy(hass_storage["core.restore_state"])
    history["record"] = deepcopy(_stored(hass_storage, switch))
    assert _stored(hass_storage, switch)["state"]["attributes"][ATTR_LAST_FAN_COMMAND] == "medium"
    if room == "idle":
        (await _idle_on_residue(hass, entry, head_a))()
    else:
        await _set_temp(hass, SENSOR_A, 75)
        await _recompute(hass, entry)
        assert hass.states.get(head_a).state == "cool"
        assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # No new shutdown dump: load the genuine earlier record after removal.
    assert hass_storage["core.restore_state"] == older
    removed = rs.async_get(hass).last_states[switch]
    await _load_store(hass, older["data"])
    assert rs.async_get(hass).last_states[switch] is not removed
    writes = _fan_writes(hass)
    await _startup(hass, entry)
    if room == "idle":
        assert hass.states.get(switch).state == "on"
        assert _fan_hold(hass, entry, 0) is False
        assert writes == [(head_a, "auto")]
        assert hass.states.get(head_a).attributes["fan_mode"] == "auto"
    else:
        assert hass.states.get(switch).state == "off"  # unresolved ownership, not proof of a hand
        assert _fan_hold(hass, entry, 0) is True
        assert writes == []
        assert hass.states.get(head_a).attributes["fan_mode"] == "high"
        await _set_fan_auto(hass, switch, True)
        await _recompute(hass, entry)
        assert hass.states.get(switch).state == "on"
        assert _fan_hold(hass, entry, 0) is False
        assert hass.states.get(head_a).attributes["fan_mode"] == "high"  # boost's rung at delta 5
    record_property("observable_history", json.dumps(history, default=str))


async def test_entry_reload_preserves_ownership_and_manual_handback(hass: HomeAssistant) -> None:
    """Rows 1–4 under entry reload, deliberately separate from cold Store reads."""
    entry, head_a, head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    reinstate = await _idle_on_residue(hass, entry, head_a)
    await _set_temp(hass, SENSOR_B, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_b).attributes["fan_mode"] == "high"
    # Change the sensor at the unload boundary, before the new incarnation can
    # drive it. No direct latch mutation or fake restore is involved.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _set_temp(hass, SENSOR_B, 72)
    reinstate()
    await _startup(hass, entry)  # memory-only reload reads removal-time records
    switch = _eid(hass, entry, "_primary_fan_auto")
    assert hass.states.get(switch).state == "on"
    assert _fan_hold(hass, entry, 0) is False
    assert hass.states.get(head_a).attributes["fan_mode"] == "auto"
    assert hass.states.get(head_b).attributes["fan_mode"] == "medium"
    assert _fan_hold(hass, entry, 1) is False
    await _set_temp(hass, SENSOR_B, 71)
    await _recompute(hass, entry)
    assert hass.states.get(head_b).attributes["fan_mode"] == "low"

    await _user_set_fan(hass, head_a, "medium")
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "off"
    writes = _fan_writes(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "off"
    assert _fan_hold(hass, entry, 0) is True
    assert [w for w in writes if w[0] == head_a] == []
    await _set_fan_auto(hass, switch, True)
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "on"
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    await _report(hass, head_a, "medium")  # adopted pre-ON token, delayed echo
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "on"
    assert _fan_hold(hass, entry, 0) is False
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    await _set_fan_auto(hass, switch, False)
    assert hass.states.get(switch).state == "off"
    await _set_fan_auto(hass, switch, True)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "auto"
    await _set_fan_auto(hass, switch, False)  # OFF at auto remains a no-op
    assert hass.states.get(switch).state == "on"


def _observable_history(hass, entry, head_id, record):
    """Full available ingress, not synthetic handler lists as product knowledge.

    HA Context/timestamps are recorded, never presumed to identify a wall
    gesture or establish causal ordering with hardware. Each recorder owns
    its listener until fixture teardown. This is evidence, not a discriminator.
    """
    watched = {head_id, SENSOR_A, SENSOR_B}
    watched.update(_eid(hass, entry, suffix) for suffix in (
        "_primary_fan_auto", "_primary_enable", "_primary_target",
        "_coordinator_enable", "_plan",
    ))
    history = {
        "record": deepcopy(record), "head": hass.states.get(head_id).as_dict(),
        "entry_id": entry.entry_id, "created_at": entry.created_at.isoformat(),
        "data": dict(entry.data), "options": dict(entry.options),
        "sensors": [hass.states.get(sid).as_dict() for sid in (SENSOR_A, SENSOR_B)],
        "plan": hass.states.get(_eid(hass, entry, "_plan")).as_dict(),
        "initial_states": {eid: hass.states.get(eid).as_dict() for eid in watched},
        "events": [],
    }

    @callback
    def capture(event):
        if event.event_type == EVENT_STATE_CHANGED:
            if event.data["entity_id"] not in watched:
                return
            data = {
                "entity_id": event.data["entity_id"],
                **{key: event.data[key].as_dict() if event.data[key] else None
                   for key in ("old_state", "new_state")},
            }
        else:
            data = deepcopy(dict(event.data))
        history["events"].append({
            "order": len(history["events"]), "type": event.event_type,
            "time_fired": event.time_fired.isoformat(), "origin": str(event.origin),
            "context": event.context.as_dict(), "data": data,
        })

    hass.bus.async_listen(EVENT_STATE_CHANGED, capture)
    hass.bus.async_listen(EVENT_CALL_SERVICE, capture)
    return history


async def test_report_only_wall_pick_collides_with_unmarked_legacy_echo(
    hass: HomeAssistant, hass_storage: dict, record_property,
) -> None:
    """The wall negative uses the SAME report ingress as the old echo.

    Conditional policy boundary: required echo ON/not-held and genuine wall
    OFF/held cannot both follow from this unmarked auto->high observation.
    This asserts the manual protection, not acceptance of the failing echo.
    """
    entry, head_a, _ = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    history = _observable_history(hass, entry, head_a, None)
    (await _idle_on_residue(hass, entry, head_a))()
    switch = _eid(hass, entry, "_primary_fan_auto")
    await _shutdown(hass, entry)
    legacy = deepcopy(hass_storage["core.restore_state"])
    record = next(r for r in legacy["data"] if r["state"]["entity_id"] == switch)
    for key in (ATTR_LAST_FAN_COMMAND, ATTR_PRIOR_FAN_COMMAND, ATTR_FAN_ON_PENDING,
                "fan_control_reason"):
        record["state"]["attributes"].pop(key, None)
    record["extra_data"] = {"held": False}
    hass_storage["core.restore_state"] = legacy
    await _load_store(hass, legacy["data"])
    await _report(hass, head_a, "auto")
    await _startup(hass, entry)
    assert hass.states.get(switch).state == "on"
    history["record"] = deepcopy(record)
    history["reconnected_head"] = hass.states.get(head_a).as_dict()
    collision_start = len(history["events"])
    writes = _fan_writes(hass)
    await _report(hass, head_a, "high")  # physical wall report, no HA service
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "off"
    assert _fan_hold(hass, entry, 0) is True
    assert writes == []
    collision = history["events"][collision_start:]
    events = [e for e in collision if e["type"] == EVENT_STATE_CHANGED
              and e["data"]["entity_id"] == head_a]
    assert events[0]["data"]["old_state"]["attributes"]["fan_mode"] == "auto"
    assert events[0]["data"]["new_state"]["attributes"]["fan_mode"] == "high"
    assert all("context" in event and "time_fired" in event for event in events)
    assert not any(e["data"].get("service") == "set_fan_mode" for e in collision)
    record_property("observable_history", json.dumps(history, default=str))


@pytest.mark.parametrize("channel", ["clean", "unavailable"])
@pytest.mark.parametrize("first", ["medium", "high"], ids=["new-token", "above-ceiling"])
async def test_saved_pending_handback_survives_restart_and_reload(
    hass: HomeAssistant, hass_storage: dict, channel: str, first: str,
) -> None:
    """Row12: earned ON adopts once; no token guess, permanent exemption or crash claim."""
    entry, head_a, _ = await _setup(
        hass, **{CONF_FAN_BOOST_ENABLE: True, CONF_FAN_BOOST_MAX: "medium"},
    )
    switch = _eid(hass, entry, "_primary_fan_auto")
    await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "off"
    await _report(hass, head_a, None)
    writes = _fan_writes(hass)
    await _set_fan_auto(hass, switch, True)
    assert hass.states.get(switch).attributes[ATTR_FAN_ON_PENDING] is True
    assert writes == []
    head = _head(hass, head_a)
    if channel == "unavailable":
        hass.states.async_remove(head_a)
        await hass.async_block_till_done()
        assert hass.states.get(switch).state == "unavailable"
    await _shutdown(hass, entry)
    record = _stored(hass_storage, switch)
    assert record["state"]["state"] == ("on" if channel == "clean" else "unavailable")
    payload = record["state"]["attributes"] if channel == "clean" else record["extra_data"]
    assert payload[ATTR_FAN_ON_PENDING] is True
    assert record["extra_data"]["held"] is False
    head.async_write_ha_state()  # still no speed
    await hass.async_block_till_done()
    await _startup(hass, entry)
    assert hass.states.get(switch).state == "on"
    assert hass.states.get(switch).attributes[ATTR_FAN_ON_PENDING] is True
    assert writes == []
    await _report(hass, head_a, first)
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "on"
    assert _fan_hold(hass, entry, 0) is False
    assert ATTR_FAN_ON_PENDING not in hass.states.get(switch).attributes
    assert hass.states.get(head_a).attributes["fan_mode"] == "auto"

    # A second explicit waiting ON also survives actual entry reload. Auto
    # consumes it; a later impossible token is a manual hold, not an exemption.
    await _report(hass, head_a, None)
    await _set_fan_auto(hass, switch, True)
    if channel == "unavailable":
        hass.states.async_remove(head_a)
        await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    head.async_write_ha_state()
    await hass.async_block_till_done()
    await _startup(hass, entry)  # actual removal records and original getters
    assert hass.states.get(switch).attributes[ATTR_FAN_ON_PENDING] is True
    await _report(hass, head_a, "auto")
    await _recompute(hass, entry)
    assert ATTR_FAN_ON_PENDING not in hass.states.get(switch).attributes
    writes.clear()
    await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "off"
    assert _fan_hold(hass, entry, 0) is True
    assert writes == [(head_a, "high")]

    # ON -> OFF while still unknown cancels durably, retaining an unresolved
    # hold until a report exists. It must not issue a blind command.
    await _report(hass, head_a, None)
    await _set_fan_auto(hass, switch, True)
    await _settle_requested_refreshes(hass, entry.runtime_data)
    assert hass.states.get(head_a).attributes.get("fan_mode") is None
    assert _fan_hold(hass, entry, 0) is False
    assert "Explicit Fan auto ON is waiting" in _reasons(hass, entry)
    writes.clear()
    await _set_fan_auto(hass, switch, False)
    await _settle_requested_refreshes(hass, entry.runtime_data)
    assert hass.states.get(switch).state == "off"
    assert ATTR_FAN_ON_PENDING not in hass.states.get(switch).attributes
    assert _fan_hold(hass, entry, 0) is True
    assert "Explicit Fan auto ON is waiting" not in _reasons(hass, entry)
    assert writes == []
    writes.clear()
    await _shutdown(hass, entry)
    await _startup(hass, entry)
    await _report(hass, head_a, "medium")
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "off"
    assert _fan_hold(hass, entry, 0) is True
    assert "fan hold" not in _reasons(hass, entry)  # a usable record needs no reason
    assert writes == []
    await _report(hass, head_a, "auto")
    await _recompute(hass, entry)
    await _set_fan_auto(hass, switch, False)
    assert hass.states.get(switch).state == "on"  # accepted OFF-at-auto no-op


@pytest.mark.parametrize("bad", ["legacy", "string", "integer", "held", "stale",
                                 "malformed-held", "malformed-memory"])
async def test_pending_handback_requires_fresh_strict_not_held_record(
    hass: HomeAssistant, hass_storage: dict, bad: str,
) -> None:
    """Labelled malformed/legacy Store inputs through both real getter channels.

    The reported speed is above the ceiling: token inference holds it, and
    only an honored instruction would adopt it.
    """
    entry, head_a, _ = await _setup(
        hass, **{CONF_FAN_BOOST_ENABLE: True, CONF_FAN_BOOST_MAX: "medium"},
    )
    switch = _eid(hass, entry, "_primary_fan_auto")
    for channel in ("clean", "unavailable"):
        await _user_set_fan(hass, head_a, "high")
        await _recompute(hass, entry)
        await _report(hass, head_a, None)
        await _set_fan_auto(hass, switch, True)
        await _shutdown(hass, entry)
        saved = deepcopy(hass_storage["core.restore_state"])
        record = next(r for r in saved["data"] if r["state"]["entity_id"] == switch)
        attrs = record["state"]["attributes"]
        extra = record["extra_data"]
        for payload in (attrs, extra):
            payload.pop(ATTR_FAN_ON_PENDING, None)
            if bad != "legacy":
                payload[ATTR_FAN_ON_PENDING] = {"string": "true", "integer": 1}.get(bad, True)
        if bad == "held":
            record["state"]["state"] = "off"
            extra["held"] = True
        if bad == "malformed-held":
            record["state"]["state"] = "off"  # clean state wins; extra is not read
            extra["held"] = "false"
        if bad == "malformed-memory":
            attrs[ATTR_PRIOR_FAN_COMMAND] = []
            extra[ATTR_PRIOR_FAN_COMMAND] = []
        if bad == "stale":
            record["state"]["last_updated"] = (entry.created_at - timedelta(seconds=1)).isoformat()
        if channel == "unavailable":
            record["state"]["state"] = "unavailable"
            record["state"]["attributes"] = {}
        hass_storage["core.restore_state"] = saved
        await _load_store(hass, saved["data"])
        await _report(hass, head_a, "high")  # the user's token, above the ceiling
        writes = _fan_writes(hass)
        await _startup(hass, entry)
        assert hass.states.get(switch).state == "off"
        assert _fan_hold(hass, entry, 0) is True
        assert ATTR_FAN_ON_PENDING not in hass.states.get(switch).attributes
        assert writes == []
        assert hass.states.get(head_a).attributes["fan_mode"] == "high"
        if bad == "stale":
            assert "older entry" in _reasons(hass, entry)
        if bad == "malformed-held" and channel == "unavailable":
            assert "missing or malformed" in _reasons(hass, entry)


def _reasons(hass, entry, room=0):
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.state not in ("unavailable", "unknown")
    reasons = plan.attributes["zones"][room]["control_reasons"]
    assert all(item["reason"] and item["next_step"] for item in reasons)
    return " ".join(item["reason"] for item in reasons)


@pytest.mark.parametrize("group", ["head-sensor", "capabilities", "settings"])
async def test_plan_explains_unavailable_room_inputs(
    hass: HomeAssistant, hass_storage: dict, group: str,
) -> None:
    """Backend room diagnostics only; rendered frontend acceptance remains owed."""
    entry, head_a, _ = await _setup(hass)
    switch = _eid(hass, entry, "_primary_fan_auto")
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), False)
    writes = _fan_writes(hass)
    head = _head(hass, head_a)
    original = hass.states.get(head_a)
    # A fresh install has no previous fan hold to lose: no reason is shown.
    # Stale and malformed records still explain themselves (below and in
    # test_pending_handback_requires_fresh_strict_not_held_record).
    assert "fan hold" not in _reasons(hass, entry)
    assert "fan hold" not in _reasons(hass, entry, 1)
    await _settle_requested_refreshes(hass, entry.runtime_data)
    healthy = deepcopy(hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][1])
    healthy_switch = _eid(hass, entry, "_secondary_fan_auto")
    if group == "head-sensor":
        # No fan observation or sensor event may conceal head invalidation.
        await _report(hass, head_a, None)
        await _settle_requested_refreshes(hass, entry.runtime_data)
        available = hass.states.get(head_a)
        for missing in (None, "unknown", "unavailable"):
            if missing is None:
                hass.states.async_remove(head_a)
            else:
                hass.states.async_set(head_a, missing, available.attributes)
            await hass.async_block_till_done()
            await _settle_requested_refreshes(hass, entry.runtime_data)
            assert f"Head {head_a} unavailable" in _reasons(hass, entry)
            # Retained fan capabilities keep ON intent available; absence cannot.
            assert hass.states.get(switch).state == (
                "unavailable" if missing is None else "on"
            )
            assert hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][1] == healthy
            assert hass.states.get(healthy_switch).state == "on"
            assert writes == []
            hass.states.async_set(SENSOR_A, "unavailable")
            await hass.async_block_till_done()
            await _settle_requested_refreshes(hass, entry.runtime_data)
            reasons = _reasons(hass, entry)
            assert f"Head {head_a} unavailable" in reasons
            assert f"Room sensor {SENSOR_A} unavailable" in reasons
            assert hass.states.get(switch).state == (
                "unavailable" if missing is None else "on"
            )
            assert "Head" not in _reasons(hass, entry, 1)
            # Restore only the head while the sensor remains unavailable.
            hass.states.async_set(head_a, available.state, available.attributes)
            await hass.async_block_till_done()
            await _settle_requested_refreshes(hass, entry.runtime_data)
            assert "Head" not in _reasons(hass, entry)
            assert hass.states.get(switch).state == "on"
            await _set_temp(hass, SENSOR_A, 70)
            await _settle_requested_refreshes(hass, entry.runtime_data)
        head.async_write_ha_state()
        await _set_temp(hass, SENSOR_A, 70)
        await _recompute(hass, entry)
        assert "Head" not in _reasons(hass, entry)
        assert "Room sensor" not in _reasons(hass, entry)
        assert hass.states.get(switch).state == "on"
        # Labelled missing-extra restore: no answer is fabricated from an
        # unavailable record. The available plan explains the fallback.
        await _shutdown(hass, entry)
        saved = deepcopy(hass_storage["core.restore_state"])
        record = next(r for r in saved["data"] if r["state"]["entity_id"] == switch)
        record["state"]["state"] = "unavailable"
        record["state"]["attributes"] = {}
        record.pop("extra_data", None)
        hass_storage["core.restore_state"] = saved
        await _load_store(hass, saved["data"])
        await _startup(hass, entry)
        assert "missing or malformed" in _reasons(hass, entry)
        await _set_fan_auto(hass, switch, True)
        await _recompute(hass, entry)
        assert "Previous fan hold" not in _reasons(hass, entry)
    elif group == "capabilities":
        for features, modes, expected in (
            (None, None, "capabilities unavailable"),
            ("invalid", "auto", "capabilities unavailable"),
            (int(ClimateEntityFeature.FAN_MODE), None, "capabilities unavailable"),
            (int(ClimateEntityFeature.FAN_MODE), [None], "capabilities unavailable"),
            (0, None, "not advertised"),
            (int(ClimateEntityFeature.FAN_MODE), ["low", "high"], "not supported"),
        ):
            attrs = {**original.attributes, "supported_features": features, "fan_modes": modes}
            hass.states.async_set(head_a, original.state, attrs)
            await hass.async_block_till_done()
            await _settle_requested_refreshes(hass, entry.runtime_data)
            assert expected in _reasons(hass, entry)
            assert hass.states.get(switch).state == "unavailable"
            assert hass.states.get(head_a).state == original.state
            assert hass.states.get(head_a).attributes["fan_mode"] == original.attributes["fan_mode"]
            assert hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][1] == healthy
            assert hass.states.get(healthy_switch).state == "on"
            assert writes == []
            # Metadata-only recovery keeps both HVAC state and speed identical.
            hass.states.async_set(head_a, original.state, original.attributes)
            await hass.async_block_till_done()
            await _settle_requested_refreshes(hass, entry.runtime_data)
            assert "capabilities" not in _reasons(hass, entry)
            assert "not advertised" not in _reasons(hass, entry)
            assert "not supported" not in _reasons(hass, entry)
            assert hass.states.get(switch).state == "on"
            assert hass.states.get(head_a).state == original.state
            assert hass.states.get(head_a).attributes["fan_mode"] == original.attributes["fan_mode"]
            assert hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][1] == healthy
            assert hass.states.get(healthy_switch).state == "on"
            assert writes == []
        head.async_write_ha_state()
        await _report(hass, head_a, None)
        await _recompute(hass, entry)
        assert "Current fan speed unavailable" in _reasons(hass, entry)
        assert hass.states.get(switch).state == "on"
        await _report(hass, head_a, "auto")
        await _recompute(hass, entry)
        assert "Current fan speed" not in _reasons(hass, entry)
        assert "capabilities" not in _reasons(hass, entry)
    else:
        assert "Default fan settings" in _reasons(hass, entry)
        # Existing settings read paths, through actual entry updates/reload.
        valid = {CONF_DEMAND_THRESHOLD: 1, CONF_FAN_BOOST_ENABLE: False,
                 CONF_FAN_BOOST_MAX: "medium"}
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        hass.config_entries.async_update_entry(entry, data={**entry.data, **valid}, options={})
        await _startup(hass, entry)
        assert "data mirror" in _reasons(hass, entry)
        assert "Default fan settings" not in _reasons(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        hass.config_entries.async_update_entry(entry, options={**valid, CONF_FAN_BOOST_MAX: "invalid"})
        await _startup(hass, entry)
        assert "Stored fan settings are invalid" in _reasons(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        hass.config_entries.async_update_entry(entry, options=valid)
        await _startup(hass, entry)
        assert "Stored fan settings are invalid" not in _reasons(hass, entry)
        assert "data mirror" not in _reasons(hass, entry)
    assert writes == []


async def test_clean_snapshot_matches_extra_before_fan_service_returns(
    hass: HomeAssistant, hass_storage: dict,
) -> None:
    """F4 publication lag control, distinct from an older surviving Store.

    Save the actual pending command snapshot, not a fabricated completion.
    A held manual control still wins after this restart and another save.
    """
    entry, head_a, _ = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    switch = _eid(hass, entry, "_primary_fan_auto")
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    head = _head(hass, head_a)
    real_set = head.async_set_fan_mode
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(fan_mode):
        assert fan_mode == "auto"
        entered.set()
        await release.wait()  # returns with no report: intent is not delivery

    async def satisfy():
        await _set_temp(hass, SENSOR_A, 70)
        await _recompute(hass, entry)

    head.async_set_fan_mode = paused
    task = hass.async_create_task(satisfy())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        state = hass.states.get(switch)
        assert state.state == "on"
        assert state.attributes[ATTR_LAST_FAN_COMMAND] == "auto"
        assert state.attributes[ATTR_PRIOR_FAN_COMMAND] == "high"
        assert hass.states.get(head_a).attributes["fan_mode"] == "high"
        await async_mock_restore_state_shutdown_restart(hass)
        saved = deepcopy(hass_storage["core.restore_state"])
        record = _stored(hass_storage, switch)
        assert record["state"]["attributes"][ATTR_LAST_FAN_COMMAND] == record["extra_data"][ATTR_LAST_FAN_COMMAND] == "auto"
        assert record["state"]["attributes"][ATTR_PRIOR_FAN_COMMAND] == record["extra_data"][ATTR_PRIOR_FAN_COMMAND] == "high"
    finally:
        release.set()
        await task
        head.async_set_fan_mode = real_set
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage["core.restore_state"] == saved
    await _load_store(hass, saved["data"])
    await _report(hass, head_a, "auto")
    await _startup(hass, entry)
    await _report(hass, head_a, "high")
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "on"
    assert _fan_hold(hass, entry, 0) is False
    await _user_set_fan(hass, head_a, "medium")
    await _recompute(hass, entry)
    assert hass.states.get(switch).state == "off"
    await _shutdown(hass, entry)
    await _startup(hass, entry)
    assert hass.states.get(switch).state == "off"
    assert _fan_hold(hass, entry, 0) is True
