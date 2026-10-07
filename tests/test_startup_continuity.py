"""Startup continuity under real HA: store-backed restart regressions.

Two obligations from the issue-25 release work, both exercised through the
real lifecycle — restore state dumped and re-read through the mocked
``core.restore_state`` store, the entry unloaded and set up again with a fresh
coordinator — not through cleared dictionaries or injected restore truth:

* H1: a room caught mid-run by a restart RESUMES that run to its target. The
  engage latch seeds from the head's own mode on the room's first ENABLED
  compute; the construction-time compute, which runs before the enable
  switches restore, must not spend that seed.
* F (fan ownership): a room the coordinator was driving comes back driven.
  The Fan auto switch carries the coordinator's echo memory — its last two
  fan commands to the head — across the restart, so a coordinator-written
  token reported late (after a provisional ``auto``, or once a missing speed
  arrives) is its own residue, not a hand on the fan, while a token outside
  that memory is a hold. A head that reports no speed gets no fan write.

Like test_registry_lifecycle, "restart" is an in-process store round trip:
not a new Home Assistant process, and not hardware. HA-only; the local
non-HA contract batch cannot run these.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import restore_state as rs
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_restore_state_shutdown_restart,
)

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)
from custom_components.mxz_coordinator.switch import (
    ATTR_LAST_FAN_COMMAND,
    ATTR_PRIOR_FAN_COMMAND,
    FanHoldRestoreData,
)
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_fan_auto,
    _set_target,
    _set_temp,
    _setup_mock_heads,
    _user_set_fan,
)

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
    """Dump restore state through the store, then unload the entry."""
    await async_mock_restore_state_shutdown_restart(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _startup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the entry up again: a fresh coordinator, restored helpers, first refresh."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


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


async def test_restored_disabled_room_does_not_resume(hass: HomeAssistant) -> None:
    """A room restored DISABLED stays parked; enabling it later starts no run."""
    entry, _head_a, head_b = await _setup(hass)
    await _run_b(hass, entry, head_b, "cool")
    enable_b = _eid(hass, entry, "_secondary_enable")
    await _switch(hass, enable_b, False)
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == "off"  # a disabled room is parked off
    # A hand turns the head on again just before the restart.
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": head_b, "hvac_mode": "cool"},
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


async def test_late_token_without_memory_is_still_a_departure(
    hass: HomeAssistant,
) -> None:
    """Control: a record from before the memory (state only, no attributes) has
    nothing to tolerate the late token with — it is a departure, as it was.
    The memory, not a blanket post-restart exemption, decides."""
    entry, head_a, _head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
    (await _idle_on_residue(hass, entry, head_a))()
    switch_a = _eid(hass, entry, "_primary_fan_auto")

    await _shutdown(hass, entry)
    # Explicitly labelled negative: the switch's record is rewritten without
    # its attributes (same incarnation, so it is still honored).
    data = rs.async_get(hass)
    old = data.last_states[switch_a]
    data.last_states[switch_a] = rs.StoredState(State(switch_a, "on"), None, old.last_seen)
    await _report(hass, head_a, "auto")
    await _startup(hass, entry)
    assert hass.states.get(switch_a).state == "on"

    writes = _fan_writes(hass)
    await _report(hass, head_a, "high")
    await _recompute(hass, entry)
    assert hass.states.get(switch_a).state == "off"
    assert _fan_hold(hass, entry, 0) is True
    assert writes == []


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
    [("medium", True), ("low", False)],
    ids=["outside-memory-holds", "remembered-token-edge"],
)
async def test_token_changed_by_hand_during_outage(
    hass: HomeAssistant, token: str, expect_hold: bool
) -> None:
    """A hand on the fan while HA was down, on a room that was not held.

    A token the coordinator never commanded is the person's: it holds. The
    documented edge: a pick of a token the memory remembers (here the 'low'
    rung the boost eased to before handing back) is indistinguishable from the
    coordinator's own echo and is handed back — as it would be live.
    """
    entry, head_a, _head_b = await _setup(hass, **{CONF_FAN_BOOST_ENABLE: True})
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
