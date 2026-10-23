"""Hands-off disabled rooms: a per-room opt-in, off by default.

Built from Chris Singley's #27 (a disabled head's observed heat/cool votes in
the shared mode) and #28 (park a disabled head once, then leave it alone), and
from the probes run against both. Every probe is here as a real test of the
behavior that ships:

* opted OUT (the default, and every entry stored before this option existed):
  a disabled room is held off every cycle, exactly as before — an outside
  turn-on is turned back off, a rejected park is retried, a restart parks, the
  tile reads off, and the head has no say in the shared mode;
* opted IN: disabling sends off until the head is SEEN off, then the head is
  left alone until the room is enabled again — across a restart too. While a
  person runs it, the tile shows what the head is doing, and its heat/cool is
  a vote in the shared mode that never outranks an enabled room's demand or
  run -- unless the room is marked Safety priority and that sensor is on.
  A safety vote that takes the mode from an enabled room's demand or engaged
  run parks that head, so it starts the mode-change wait like any flip.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import pytest
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import restore_state as rs
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import (
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_RESTING_MODE_BIAS,
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    INHIBIT_ACTION_ECO,
    INHIBIT_ACTION_OFF,
    MODE_COOL,
    MODE_HEAT,
    ZONE_CLIMATE,
    ZONE_HANDS_OFF,
    ZONE_NAME,
    ZONE_SAFETY_PRIORITY_ENTITY,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.logic import (
    PRIORITY_DEMAND,
    PRIORITY_SAFETY,
    PRIORITY_VOTE,
    Demand,
    arbitrate,
    shared_mode,
)
from tests.dwell import assert_restamped, expire_dwell
from tests.flow_pages import default, open_options_page, prefilled, submit
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_target,
    _set_temp,
    _setup_mock_heads,
)
from tests.test_startup_continuity import _head, _shutdown, _startup, _switch

INHIBIT = "binary_sensor.grid_down"
SAFETY_B = "binary_sensor.secondary_safety"


def _s(hass: HomeAssistant, entity_id: str) -> str:
    return hass.states.get(entity_id).state


async def _remote(hass: HomeAssistant, head: str, mode: str) -> None:
    """A person sets the head on its own remote / an automation / Apple Home."""
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": head, "hvac_mode": mode}, blocking=True
    )
    await hass.async_block_till_done()


def _plan(hass: HomeAssistant, entry: MockConfigEntry):
    return hass.states.get(_eid(hass, entry, "_plan"))


async def _setup(
    hass: HomeAssistant,
    *,
    hands_off: tuple[bool, bool] = (False, False),
    enable: tuple[str, ...] = ("_primary_enable", "_secondary_enable", "_coordinator_enable"),
    hysteresis: int | None = 0,
    legacy_key: bool = True,
    safety: tuple[str | None, str | None] = (None, None),
    **extra,
) -> tuple[MockConfigEntry, str, str]:
    """Two mock heads, both rooms at 70 / target 70.

    ``hands_off`` opts each room in. A room left opted out stores NO key at
    all (``legacy_key``), exactly like every entry saved before the option
    existed; ``legacy_key=False`` stores an explicit False instead.
    ``safety`` stores each room's Safety priority sensor, if any.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    zones = []
    for name, head, sensor, opted, safe in (
        ("Primary", head_a, SENSOR_A, hands_off[0], safety[0]),
        ("Secondary", head_b, SENSOR_B, hands_off[1], safety[1]),
    ):
        zone = {ZONE_NAME: name, ZONE_CLIMATE: head, ZONE_SENSOR: sensor}
        if opted or not legacy_key:
            zone[ZONE_HANDS_OFF] = opted
        if safe:
            zone[ZONE_SAFETY_PRIORITY_ENTITY] = safe
        zones.append(zone)
    data = {CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY, CONF_ZONES: zones, **extra}
    if hysteresis is not None:
        data[CONF_MODE_HYSTERESIS] = hysteresis
    entry = MockConfigEntry(domain=DOMAIN, title="MXZ Coordinator", data=data, version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in enable:
        await _switch(hass, _eid(hass, entry, suffix), True)
    for suffix in ("_primary_target", "_secondary_target"):
        await _set_target(hass, _eid(hass, entry, suffix), 70)
    await _recompute(hass, entry)
    return entry, head_a, head_b


def _hvac_mode_calls(hass: HomeAssistant, head: str) -> list[str]:
    """Record every set_hvac_mode sent to ``head`` from here on."""
    calls: list[str] = []

    @callback
    def _record(event) -> None:
        data = event.data["service_data"]
        if (
            event.data["domain"] == "climate"
            and event.data["service"] == "set_hvac_mode"
            and data.get("entity_id") == head
        ):
            calls.append(data["hvac_mode"])

    hass.bus.async_listen("call_service", _record)
    return calls


def _mode_commands(hass: HomeAssistant, head: str) -> list[str]:
    """Record every mode sent to ``head`` from here on: set_hvac_mode, and
    set_temperature's hvac_mode (how heat and cool are sent)."""
    calls: list[str] = []

    @callback
    def _record(event) -> None:
        data = event.data["service_data"]
        if event.data["domain"] != "climate" or data.get("entity_id") != head:
            return
        if event.data["service"] == "set_hvac_mode" or (
            event.data["service"] == "set_temperature" and "hvac_mode" in data
        ):
            calls.append(data["hvac_mode"])

    hass.bus.async_listen("call_service", _record)
    return calls


# ---------------------------------------------------------------------------
# Opted out (default): held off, exactly as before
# ---------------------------------------------------------------------------
async def test_existing_entries_load_opted_out(hass: HomeAssistant) -> None:
    """No stored key (every entry before this option) and False both mean off."""
    entry, _a, _b = await _setup(hass)
    assert [z.hands_off for z in entry.runtime_data.zones] == [False, False]


async def test_non_bool_option_is_opted_out(hass: HomeAssistant) -> None:
    """Only a real True opts in; a hand-edited "yes" or 1 is not consent."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(domain=DOMAIN, title="MXZ", version=2, data={
        CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
        CONF_ZONES: [
            {ZONE_NAME: "P", ZONE_CLIMATE: head_a, ZONE_SENSOR: SENSOR_A, ZONE_HANDS_OFF: "yes"},
            {ZONE_NAME: "S", ZONE_CLIMATE: head_b, ZONE_SENSOR: SENSOR_B, ZONE_HANDS_OFF: 1},
        ],
    })
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert [z.hands_off for z in entry.runtime_data.zones] == [False, False]


@pytest.mark.parametrize("legacy_key", [True, False], ids=["no_key", "explicit_false"])
async def test_opted_out_outside_turn_on_is_held_off(
    hass: HomeAssistant, legacy_key: bool
) -> None:
    """Probe pr28d: a head timer turns a disabled head on; it goes back off, every cycle."""
    entry, _a, head_b = await _setup(hass, legacy_key=legacy_key)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    states = []
    for _ in range(3):
        await _remote(hass, head_b, "cool")  # e.g. the head's own on-timer
        await _recompute(hass, entry)
        states.append(_s(hass, head_b))
    assert states == ["off", "off", "off"]


async def test_opted_out_rejected_park_is_retried(hass: HomeAssistant) -> None:
    """Probe pr28e, opted out: a rejected park is sent again next cycle."""
    entry, _a, head_b = await _setup(hass)
    await _remote(hass, head_b, "heat")
    head = _head(hass, head_b)
    real = head.async_set_hvac_mode
    calls = {"n": 0}

    async def flaky(hvac_mode):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HomeAssistantError("invented rejection")
        await real(hvac_mode)

    head.async_set_hvac_mode = flaky
    # Turning the switch off runs a cycle of its own: that park is rejected.
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    assert _s(hass, head_b) == "heat"
    assert entry.runtime_data.command_attributes(head_b)["command_status"] == "rejected"
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    assert calls["n"] == 2


async def test_opted_out_restart_parks_a_remote_held_head(hass: HomeAssistant) -> None:
    """Probe pr28a, opted out: a restart (like any cycle) parks the head again."""
    entry, _a, head_b = await _setup(hass)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _shutdown(hass, entry)
    await _remote(hass, head_b, "heat")  # turned on while HA was down
    await _startup(hass, entry)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_opted_out_tile_reads_off_and_head_has_no_vote(hass: HomeAssistant) -> None:
    """Probes pr28b/pr28c and #27, opted out: the disabled head is not looked at.

    It is parked within the same cycle, so the tile reads off and the plan
    shows no standoff, whatever the head was doing a moment before.
    """
    entry, head_a, head_b = await _setup(hass, enable=("_primary_enable", "_coordinator_enable"))
    await _set_temp(hass, SENSOR_A, 76)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (_s(hass, head_a), _s(hass, head_b)) == ("cool", "off")
    assert plan.attributes["standoff"] is False
    assert plan.attributes["secondary_demand"] == "off"
    tile = hass.states.get(_eid(hass, entry, "_secondary_thermostat"))
    assert tile.state == HVACMode.OFF
    assert tile.attributes["hvac_action"] == HVACAction.OFF


# ---------------------------------------------------------------------------
# Opted in: off until it lands, then hands off
# ---------------------------------------------------------------------------
async def test_parks_until_seen_off_then_hands_off(hass: HomeAssistant) -> None:
    """#28's test, kept: park once it lands, never again; a new disablement parks anew."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "heat")
    calls = _hvac_mode_calls(hass, head_b)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    assert calls == ["off"]
    # A person picks the remote back up: never re-parked, however many cycles.
    await _remote(hass, head_b, "heat")
    for _ in range(3):
        await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"
    assert calls == ["off", "heat"]  # the second one is the person's
    # Re-enabling hands the head back to the plan; disabling again parks again.
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), True)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_head_already_off_is_released_without_a_command(hass: HomeAssistant) -> None:
    """Nothing to land: an already-off head is left alone from the first cycle."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "off")
    calls = _hvac_mode_calls(hass, head_b)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _remote(hass, head_b, "cool")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "cool"
    assert calls == ["cool"]


async def test_rejected_park_is_retried_until_it_lands(hass: HomeAssistant) -> None:
    """Probe pr28e, opted in: a rejected off is NOT landed; it is sent again."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "heat")
    head = _head(hass, head_b)
    real = head.async_set_hvac_mode
    calls = {"n": 0}

    async def flaky(hvac_mode):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise HomeAssistantError("invented rejection")
        await real(hvac_mode)

    head.async_set_hvac_mode = flaky
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)  # rejected
    assert _s(hass, head_b) == "heat"
    attrs = entry.runtime_data.command_attributes(head_b)
    assert attrs["command_status"] == "rejected"
    assert attrs["command_error"] == "invented rejection"
    await _recompute(hass, entry)
    assert (_s(hass, head_b), calls["n"]) == ("heat", 2)
    await _recompute(hass, entry)
    assert (_s(hass, head_b), calls["n"]) == ("off", 3)
    # Landed: from here the head is the person's.
    head.async_set_hvac_mode = real
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"


async def test_returned_but_not_seen_off_is_not_landed(hass: HomeAssistant) -> None:
    """A call that returns without the head ever reporting off is sent again."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "heat")
    head = _head(hass, head_b)
    real = head.async_set_hvac_mode
    calls = {"n": 0}

    async def swallowed(hvac_mode):
        calls["n"] += 1
        if calls["n"] == 1:
            return  # accepted by the integration, never reached the head
        await real(hvac_mode)

    head.async_set_hvac_mode = swallowed
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    assert entry.runtime_data.command_attributes(head_b)["command_status"] == "returned"
    assert _s(hass, head_b) == "heat"
    await _recompute(hass, entry)
    assert (_s(hass, head_b), calls["n"]) == ("off", 2)


async def test_off_seen_while_our_run_is_in_flight_is_not_landed(
    hass: HomeAssistant,
) -> None:
    """Our cool is still in flight when the room is disabled and the head
    reports off: that cool can still turn it back on, so the park has not
    landed. Our off follows it and the room is released only once that lands."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    head = _head(hass, head_b)
    real = head.async_set_temperature
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        if kwargs.get("hvac_mode") == "cool":
            entered.set()
            await release.wait()
        await real(**kwargs)

    head.async_set_temperature = slow
    # The enabled secondary needs cool. (No block_till_done while the cool is
    # paused: it would wait on it.)
    hass.states.async_set(SENSOR_B, "76", {ATTR_UNIT_OF_MEASUREMENT: "°F"})
    task = hass.async_create_task(entry.runtime_data.async_refresh())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await hass.services.async_call(
            "switch", "turn_off",
            {"entity_id": _eid(hass, entry, "_secondary_enable")}, blocking=True,
        )
        head._attr_hvac_mode = "off"  # a person's remote, or a stray report
        head.async_write_ha_state()
        for _ in range(5):
            await asyncio.sleep(0)
        assert _s(hass, head_b) == "off"
        assert entry.runtime_data.hands_off_released("secondary") is False
    finally:
        release.set()
        await task
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    zone = _plan(hass, entry).attributes["zones"][1]
    assert zone["hands_off"] == "released"


async def test_restart_keeps_the_hold(hass: HomeAssistant) -> None:
    """Probe pr28a, opted in: a person's run survives an HA restart."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    await _shutdown(hass, entry)
    await _startup(hass, entry)
    for _ in range(2):
        await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"
    # And the restored room still re-parks on a fresh disablement.
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), True)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_restart_before_the_park_landed_still_parks(hass: HomeAssistant) -> None:
    """Disabled, but the off never landed before HA went down: it parks after."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "heat")
    head = _head(hass, head_b)
    real = head.async_set_hvac_mode

    async def reject(hvac_mode):
        raise HomeAssistantError("invented rejection")

    head.async_set_hvac_mode = reject
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"
    await _shutdown(hass, entry)
    head.async_set_hvac_mode = real
    await _startup(hass, entry)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_disabled_while_down_without_record_still_parks(hass: HomeAssistant) -> None:
    """A disabled room with no hands-off record (older version, lost store) parks."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _remote(hass, head_b, "heat")
    await _shutdown(hass, entry)
    # Strip the record, as a store from before this option (state only, no extra data) has it.
    restore = rs.async_get(hass)
    sid = _eid(hass, entry, "_secondary_enable")
    restore.last_states[sid] = rs.StoredState(
        restore.last_states[sid].state, None, restore.last_states[sid].last_seen
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_opting_out_resumes_the_hold(hass: HomeAssistant) -> None:
    """A released room that is opted out again is held off from the next cycle."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"
    zones = [dict(z) for z in entry.data[CONF_ZONES]]
    zones[1].pop(ZONE_HANDS_OFF)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONES: zones})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_standby_hold_still_parks_a_released_head(hass: HomeAssistant) -> None:
    """A grid-down standby parks every head, hands-off or not; release leaves it off."""
    hass.states.async_set(INHIBIT, "off")
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True),
        **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_OFF},
    )
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _remote(hass, head_b, "heat")
    hass.states.async_set(INHIBIT, "on")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    hass.states.async_set(INHIBIT, "off")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"


async def test_eco_standby_hold_also_holds_a_released_head_off(hass: HomeAssistant) -> None:
    """An eco hold is no fixed park, but it is still a hold: the head is held off."""
    hass.states.async_set(INHIBIT, "off")
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True),
        **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_ECO},
    )
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    await _remote(hass, head_b, "heat")
    hass.states.async_set(INHIBIT, "on")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    assert _plan(hass, entry).attributes["zones"][1]["hands_off_vote"] is None
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    hass.states.async_set(INHIBIT, "off")
    await hass.async_block_till_done()
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"


async def test_kill_switch_off_sends_nothing_and_parking_waits(hass: HomeAssistant) -> None:
    """With the coordinator off nothing is written; the park lands once it is back on."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _remote(hass, head_b, "heat")
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), False)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), True)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"


async def test_released_head_in_a_banned_mode_is_not_healed(hass: HomeAssistant) -> None:
    """dry/auto on a hands-off head is the person's choice: no heal, no notify."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    head = _head(hass, head_b)
    head._attr_hvac_mode = "dry"
    head.async_write_ha_state()
    await hass.async_block_till_done()
    assert (head_b, "band") not in entry.runtime_data._heal_timers
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "dry"


# ---------------------------------------------------------------------------
# Opted in: the room tile shows the head
# ---------------------------------------------------------------------------
async def test_tile_reflects_the_head_a_person_runs(hass: HomeAssistant) -> None:
    """Probe pr28c: hvac_action follows the real head; hvac_mode stays off.

    hvac_mode is the room's coordinator switch (off = the coordinator is not
    running this room) and its only other mode is heat_cool, so off is the
    honest one; turning the tile on still enables the room.
    """
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    tile_id = _eid(hass, entry, "_secondary_thermostat")
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert hass.states.get(tile_id).attributes["hvac_action"] == HVACAction.OFF
    for mode, action in (("heat", HVACAction.HEATING), ("cool", HVACAction.COOLING),
                         ("fan_only", HVACAction.FAN), ("off", HVACAction.OFF)):
        await _remote(hass, head_b, mode)
        tile = hass.states.get(tile_id)
        assert tile.state == HVACMode.OFF
        assert tile.attributes["hvac_action"] == action, mode
    await _remote(hass, head_b, "heat")
    await hass.services.async_call(
        "climate", "turn_on", {"entity_id": tile_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert _s(hass, _eid(hass, entry, "_secondary_enable")) == "on"
    assert hass.states.get(tile_id).state == HVACMode.HEAT_COOL
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": tile_id, "hvac_mode": "off"}, blocking=True
    )
    await hass.async_block_till_done()
    assert _s(hass, _eid(hass, entry, "_secondary_enable")) == "off"


async def test_tile_prefers_the_heads_own_action(hass: HomeAssistant) -> None:
    """A head that reports hvac_action (idle at setpoint) is shown as it reports."""
    entry, _a, head_b = await _setup(hass, hands_off=(False, True))
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    head = _head(hass, head_b)
    head._attr_hvac_mode = "heat"
    head._attr_hvac_action = HVACAction.IDLE
    head.async_write_ha_state()
    await hass.async_block_till_done()
    tile = hass.states.get(_eid(hass, entry, "_secondary_thermostat"))
    assert tile.attributes["hvac_action"] == HVACAction.IDLE


# ---------------------------------------------------------------------------
# Opted in: the shared-mode vote, never above an enabled room
# ---------------------------------------------------------------------------
async def test_opposite_mode_head_is_a_visible_standoff(hass: HomeAssistant) -> None:
    """Probe pr28b and #27's test: the enabled primary keeps cooling, the plan says standoff."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)  # the disabled secondary's park lands here
    await _remote(hass, head_b, "heat")
    for _ in range(2):
        await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (_s(hass, head_a), _s(hass, head_b)) == ("cool", "heat")
    assert plan.state == "cool"
    assert plan.attributes["standoff"] is True
    assert plan.attributes["secondary_demand"] == "off"  # the coordinator asks nothing of it
    assert plan.attributes["zones"][1]["hands_off_vote"] == "heat"


async def test_safety_priority_vote_preempts_an_enabled_demand(hass: HomeAssistant) -> None:
    """A hands-off room's safety-asserting vote outranks an enabled room's
    ordinary demand -- the real scenario the safety tier exists for: a
    released head in genuine distress must be able to get the compressor
    into its needed mode even though a sibling merely wants comfort in the
    opposite direction and would otherwise outrank it by configured order.

    Also covers the required dwell-stamp fix: parking an ACTIVELY RUNNING
    demand is a real compressor reversal and stamps like any other, so a
    safety sensor that flaps can't reverse the compressor twice inside one
    hysteresis window -- confirmed by checking the reversal back is itself
    held by the dwell, not by poking internal state, but by the attribute
    the UI/REST API would show."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    await _set_temp(hass, SENSOR_A, 76)  # primary's ordinary demand: cool
    await _recompute(hass, entry)  # the disabled secondary's park lands here
    await _remote(hass, head_b, "heat")  # secondary's own remote: heat
    for _ in range(2):
        await _recompute(hass, entry)
    # Baseline, no safety assertion: primary's demand wins (today's rule,
    # unaffected by this patch when nothing claims safety).
    plan = _plan(hass, entry)
    assert (plan.state, _s(hass, head_a)) == ("cool", "cool")

    safety_entity = "binary_sensor.secondary_safety"
    hass.states.async_set(safety_entity, "on")
    manager = hass.config_entries.options
    result = await open_options_page(hass, entry, "room_2")
    result = await submit(
        manager, result, {**prefilled(result), "safety_priority_entity": safety_entity},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()  # the option change reloads the entry
    expire_dwell(entry.runtime_data)  # hysteresis long elapsed
    await _recompute(hass, entry)

    plan = _plan(hass, entry)
    assert plan.state == "heat"  # secondary's safety vote now wins
    assert plan.attributes["standoff"] is True  # primary still wants cool
    assert plan.attributes["zones"][1]["hands_off_vote"] == "heat"
    assert plan.attributes["zones"][1]["safety_priority"] is True
    assert plan.attributes["mode_by_safety"] is True
    assert _s(hass, head_a) != "cool"  # primary's denied demand is not commanded
    # It parked an ACTIVELY RUNNING demand: this stamps the dwell (NOT
    # exempt), unlike a vote settling into otherwise-idle silence. (The gate
    # that just PERMITTED this flip reads True on this same tick by
    # construction; the stamp's effect shows up starting next tick.)
    assert plan.attributes["mode_by_hands_off"] is False

    # Prove the stamp actually protects the compressor: the safety condition
    # clearing immediately afterward must NOT reverse it back right away.
    hass.states.async_set(safety_entity, "off")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == "heat"  # held by the dwell this reversal just started
    assert plan.attributes["mode_change_allowed"] is False


async def test_disabled_primary_never_idles_an_enabled_room(hass: HomeAssistant) -> None:
    """Probes pr28b2 and #27: a hands-off PRIMARY in heat vs an enabled hot secondary."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(True, False), enable=("_secondary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    await _set_temp(hass, SENSOR_B, 76)
    await _recompute(hass, entry)
    assert (_s(hass, head_a), _s(hass, head_b)) == ("off", "cool")
    coord = entry.runtime_data
    expire_dwell(coord)
    await _remote(hass, head_a, "heat")
    seen = []
    for _ in range(4):
        await _recompute(hass, entry)
        seen.append((_plan(hass, entry).state, _s(hass, head_a), _s(hass, head_b)))
        expire_dwell(coord)  # hysteresis long elapsed every cycle
    assert seen == [("cool", "heat", "cool")] * 4


async def test_vote_decides_only_when_no_enabled_room_calls(hass: HomeAssistant) -> None:
    """No enabled room calling: the plan follows the hands-off head, without
    stamping the dwell clock, so an enabled room's later call flips at once."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    coord = entry.runtime_data
    await _recompute(hass, entry)  # park lands
    long_ago = expire_dwell(coord)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == "heat"
    assert coord.current_shared_mode == "heat"
    assert coord._last_mode_change_ts == long_ago  # the vote stamped nothing
    assert _s(hass, head_a) == "fan_only"  # satisfied primary idles as always
    # The enabled primary now needs cooling: the dwell does not hold it back.
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == "cool"
    assert plan.attributes["standoff"] is True
    assert _s(hass, head_a) == "cool"
    assert _s(hass, head_b) == "heat"


async def test_return_to_rest_after_a_vote_starts_no_dwell(hass: HomeAssistant) -> None:
    """With a resting bias, the plan going back to rest when the hands-off head
    stops is the vote's undoing, not a flip of ours: an enabled room calling
    right after is not held back by a dwell."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None, **{CONF_RESTING_MODE_BIAS: "cool"},
    )
    coord = entry.runtime_data
    await _recompute(hass, entry)
    long_ago = expire_dwell(coord)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == "heat"
    await _remote(hass, head_b, "off")
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == "cool"
    assert coord._last_mode_change_ts == long_ago
    await _set_temp(hass, SENSOR_A, 64)  # the enabled primary needs heat
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == "heat"
    assert _s(hass, head_a) == "heat"
    assert_restamped(coord, long_ago)  # an enabled room's flip is stamped


async def test_plan_shows_mode_by_hands_off(hass: HomeAssistant) -> None:
    """``mode_by_hands_off`` on the plan sensor is true on the plan whose mode
    the vote changed, false once nothing changes, and false again when an
    enabled room calling decides the mode."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    coord = entry.runtime_data
    await _recompute(hass, entry)  # park lands
    assert _plan(hass, entry).attributes["mode_by_hands_off"] is False
    expire_dwell(coord)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_by_hands_off"]) == ("heat", True)
    # Without a resting mode the vote now matches the plan: no change, false.
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_by_hands_off"]) == ("heat", False)
    # The enabled primary needs cooling: it decides, so false.
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_by_hands_off"]) == ("cool", False)


async def test_plan_shows_mode_by_hands_off_through_the_undo(hass: HomeAssistant) -> None:
    """With a resting mode, true while the vote holds the mode away from rest
    and on the plan going back to rest when the head stops; false after."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None, **{CONF_RESTING_MODE_BIAS: "cool"},
    )
    coord = entry.runtime_data
    await _recompute(hass, entry)
    expire_dwell(coord)
    await _remote(hass, head_b, "heat")
    seen = []
    for _ in range(2):
        await _recompute(hass, entry)
        plan = _plan(hass, entry)
        seen.append((plan.state, plan.attributes["mode_by_hands_off"]))
    await _remote(hass, head_b, "off")
    for _ in range(2):
        await _recompute(hass, entry)
        plan = _plan(hass, entry)
        seen.append((plan.state, plan.attributes["mode_by_hands_off"]))
    assert seen == [("heat", True), ("heat", True), ("cool", True), ("cool", False)]


async def test_opted_out_plan_shows_mode_by_hands_off_false(hass: HomeAssistant) -> None:
    """An install with no room opted in shows the attribute, always false."""
    entry, head_a, _b = await _setup(
        hass, enable=("_primary_enable", "_coordinator_enable"), hysteresis=None,
    )
    await _recompute(hass, entry)
    expire_dwell(entry.runtime_data)
    seen = [_plan(hass, entry).attributes["mode_by_hands_off"]]
    await _set_temp(hass, SENSOR_A, 64)  # the primary needs heat: a real flip
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, _s(hass, head_a)) == ("heat", "heat")
    seen.append(plan.attributes["mode_by_hands_off"])
    assert seen == [False, False]


async def _cool_then_heat_at_rest(hass: HomeAssistant, entry, head_a: str) -> float:
    """The enabled primary runs cool at the resting mode, then needs heat
    seconds later. Returns the dwell clock as it stood after the cool run."""
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)
    assert _s(hass, head_a) == "cool"
    stamped = coord._last_mode_change_ts
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    return stamped


async def test_opted_out_run_at_rest_starts_no_dwell(hass: HomeAssistant) -> None:
    """The base the next test is held to: an enabled run in the mode already
    set is no flip, so a reversal seconds later is not held."""
    entry, head_a, _b = await _setup(
        hass, enable=("_primary_enable", "_coordinator_enable"), hysteresis=None,
        **{CONF_RESTING_MODE_BIAS: "cool"},
    )
    await _recompute(hass, entry)
    long_ago = expire_dwell(entry.runtime_data)
    assert await _cool_then_heat_at_rest(hass, entry, head_a) == long_ago
    assert _plan(hass, entry).state == "heat"
    assert _s(hass, head_a) == "heat"


async def test_run_at_rest_after_a_vote_undo_starts_no_dwell(hass: HomeAssistant) -> None:
    """Back at rest after the vote's undoing, the mode owes nothing to the
    vote: an enabled run there is the base case above, not a flip to stamp."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None, **{CONF_RESTING_MODE_BIAS: "cool"},
    )
    coord = entry.runtime_data
    await _recompute(hass, entry)  # park lands
    long_ago = expire_dwell(coord)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == "heat"
    await _remote(hass, head_b, "off")  # the vote ends
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == "cool"
    assert coord._mode_by_votes is False
    assert await _cool_then_heat_at_rest(hass, entry, head_a) == long_ago
    assert _plan(hass, entry).state == "heat"
    assert _s(hass, head_a) == "heat"


async def test_enabled_run_in_the_voted_mode_stamps_the_dwell(hass: HomeAssistant) -> None:
    """The vote picked heat (no stamp); an enabled room then runs heat in it.
    The compressor now heats for a room we coordinate, so the next reversal
    waits out the dwell exactly as if that room had flipped the mode itself."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    coord = entry.runtime_data
    await _recompute(hass, entry)  # park lands
    long_ago = expire_dwell(coord)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == "heat"
    assert coord._last_mode_change_ts == long_ago  # the vote alone stamps nothing
    await _set_temp(hass, SENSOR_A, 64)  # the enabled primary needs heat too
    await _recompute(hass, entry)
    assert _s(hass, head_a) == "heat"
    assert_restamped(coord, long_ago)  # our heat run owns the mode now
    assert coord._mode_by_votes is False
    await _remote(hass, head_b, "off")  # the person stops; only our run remains
    await _set_temp(hass, SENSOR_A, 76)  # seconds later the primary needs cool
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == "heat"  # held by the dwell, as for an enabled flip
    assert plan.attributes["mode_change_allowed"] is False
    assert _s(hass, head_a) != "cool"


async def test_rest_after_an_enabled_run_in_the_voted_mode_is_dwelled(
    hass: HomeAssistant,
) -> None:
    """Resting bias: once an enabled room ran in the voted mode, going back to
    rest is our reversal, not the vote's undoing: it waits out the dwell and,
    when it happens, it is stamped."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None, **{CONF_RESTING_MODE_BIAS: "cool"},
    )
    coord = entry.runtime_data
    await _recompute(hass, entry)
    expire_dwell(coord)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == "heat"
    await _set_temp(hass, SENSOR_A, 64)  # the enabled primary runs heat
    await _recompute(hass, entry)
    assert _s(hass, head_a) == "heat"
    await _set_temp(hass, SENSOR_A, 70)  # satisfied, at its target
    await _remote(hass, head_b, "off")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.attributes["primary_engage"] not in ("cool", "heat")
    assert plan.state == "heat"  # rest waits out the dwell
    assert plan.attributes["mode_change_allowed"] is False
    long_ago = expire_dwell(coord)  # the dwell has run out
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == "cool"
    assert coord._mode_by_votes is False
    assert_restamped(coord, long_ago)  # the return to rest is stamped


async def test_vote_never_breaks_an_engaged_run(hass: HomeAssistant) -> None:
    """A room mid-run toward target (engaged, its demand back inside the band)
    is calling: the hands-off vote does not take the mode from it."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)
    assert _s(hass, head_a) == "cool"
    await _set_temp(hass, SENSOR_A, 71)  # inside the demand band, still above target
    expire_dwell(coord)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.attributes["primary_demand"] != "cool"
    assert plan.attributes["primary_engage"] == "cool"
    assert plan.state == "cool"
    assert _s(hass, head_a) == "cool"


async def test_only_heat_and_cool_vote(hass: HomeAssistant) -> None:
    """fan_only/dry/auto/heat_cool/off carry no vote. An unavailable head
    keeps the vote it last showed, stale (see tests/test_lost_vote.py): here
    that is the off just before it, so still no vote."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None, **{CONF_RESTING_MODE_BIAS: "cool"},
    )
    coord = entry.runtime_data
    await _recompute(hass, entry)
    head = _head(hass, head_b)
    for mode in ("fan_only", "dry", "auto", "heat_cool", "off", "unavailable"):
        expire_dwell(coord)
        if mode == "unavailable":
            hass.states.async_set(head_b, "unavailable")
        else:
            head._attr_hvac_mode = mode
            head.async_write_ha_state()
        await hass.async_block_till_done()
        await _recompute(hass, entry)
        plan = _plan(hass, entry)
        assert plan.state == "cool", mode
        assert plan.attributes["zones"][1]["hands_off_vote"] is None, mode
        assert plan.attributes["zones"][1]["vote_status"] == (
            "stale" if mode == "unavailable" else "live"
        ), mode


async def test_no_vote_before_the_park_lands(hass: HomeAssistant) -> None:
    """Until our off lands, the head's mode is ours or stale: it does not vote."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable",), hysteresis=None,
    )
    coord = entry.runtime_data
    await _remote(hass, head_b, "heat")  # running before the coordinator is on
    head = _head(hass, head_b)

    async def reject(hvac_mode):
        raise HomeAssistantError("invented rejection")

    head.async_set_hvac_mode = reject
    expire_dwell(coord)
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), True)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert _s(hass, head_b) == "heat"
    assert plan.attributes["zones"][1]["hands_off"] == "parking"
    assert plan.attributes["zones"][1]["hands_off_vote"] is None
    assert plan.state == "cool"
    assert plan.attributes["standoff"] is False


# ---------------------------------------------------------------------------
# Safety priority: a room's sensor lifts the want it already has
# ---------------------------------------------------------------------------
async def _safety_setup(hass: HomeAssistant, **extra) -> tuple[MockConfigEntry, str, str]:
    """Enabled primary; hands-off secondary whose stored config names SAFETY_B
    as its Safety priority sensor; the default mode-change wait."""
    return await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None, safety=(None, SAFETY_B), **extra,
    )


async def _secondary_heating_vs_primary_calling_cool(
    hass: HomeAssistant, entry: MockConfigEntry, head_b: str
) -> None:
    """The primary demands cool (76 F); a person runs the released secondary in heat."""
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)  # the disabled secondary's park lands here
    await _remote(hass, head_b, "heat")
    for _ in range(2):
        await _recompute(hass, entry)


async def _primary_coasting_in_band(
    hass: HomeAssistant, entry: MockConfigEntry, head_a: str, head_b: str
) -> None:
    """As above, then the primary cools to 72 F (target 70): its demand is back
    inside the band, but it is still engaged and its head still cools."""
    await _secondary_heating_vs_primary_calling_cool(hass, entry, head_b)
    await _set_temp(hass, SENSOR_A, 72)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, _s(hass, head_a)) == ("cool", "cool")
    assert plan.attributes["primary_demand"] == "neutral"
    assert plan.attributes["primary_engage"] == "cool"


def _reversals(states: list[str], start: str = "cool") -> int:
    return sum(1 for a, b in zip([start, *states], states) if a != b)


def _tracked(hass: HomeAssistant, entity_id: str) -> int:
    """How many state-change jobs HA holds for ``entity_id`` (HA drops its
    bookkeeping once nothing is tracked, e.g. after the entry unloads)."""
    data = hass.data.get("track_state_change_data")
    return 0 if data is None else len(data.callbacks.get(entity_id, ()))


async def test_safety_vote_over_an_engaged_run_parks_it_and_stamps_the_dwell(
    hass: HomeAssistant,
) -> None:
    """The primary is mid-run with its demand back inside the band. The
    secondary's safety vote takes the mode, which parks the primary's running
    head: a reversal the coordinator causes, so it starts the mode-change wait
    exactly as it does over a room that is still demanding."""
    entry, head_a, head_b = await _safety_setup(hass)
    coord = entry.runtime_data
    await _primary_coasting_in_band(hass, entry, head_a, head_b)
    long_ago = expire_dwell(coord)  # the last flip's wait has long run out
    sent = _mode_commands(hass, head_a)
    hass.states.async_set(SAFETY_B, "on")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == "heat"
    assert plan.attributes["mode_by_safety"] is True
    assert plan.attributes["mode_by_hands_off"] is False  # a flip of ours
    assert (_s(hass, head_a), sent) == ("fan_only", ["fan_only"])  # the run is parked
    assert_restamped(coord, long_ago)  # ...and the park starts the wait
    assert coord._mode_by_votes is False


@pytest.mark.parametrize("resting", [None, MODE_COOL], ids=["last_used", "rest_cool"])
async def test_flapping_safety_sensor_vs_an_engaged_run_reverses_once_per_wait(
    hass: HomeAssistant, resting: str | None,
) -> None:
    """The safety sensor toggles on/off/on/off against the primary's engaged,
    coasting cool run, and the parked primary warms back out of the band and
    calls cool, all inside one mode-change wait: one compressor reversal, one
    park, no re-stamp. When the wait runs out the primary's run resumes."""
    extra = {} if resting is None else {CONF_RESTING_MODE_BIAS: resting}
    entry, head_a, head_b = await _safety_setup(hass, **extra)
    coord = entry.runtime_data
    await _primary_coasting_in_band(hass, entry, head_a, head_b)
    long_ago = expire_dwell(coord)
    sent = _mode_commands(hass, head_a)
    seen: list[str] = []

    async def step() -> None:
        await _recompute(hass, entry)
        seen.append(_plan(hass, entry).state)

    hass.states.async_set(SAFETY_B, "on")
    await step()
    assert_restamped(coord, long_ago)  # the park started the wait
    stamp = coord._last_mode_change_ts
    hass.states.async_set(SAFETY_B, "off")
    await step()
    await _set_temp(hass, SENSOR_A, 74)  # the parked room calls cool again
    await step()
    hass.states.async_set(SAFETY_B, "on")
    await step()
    hass.states.async_set(SAFETY_B, "off")
    await step()
    assert seen == ["heat"] * 5
    assert _reversals(seen) == 1
    assert coord._last_mode_change_ts == stamp  # nothing re-stamped inside the wait
    assert sent == ["fan_only"]
    assert _plan(hass, entry).attributes["mode_change_allowed"] is False

    expire_dwell(coord)  # the wait runs out, the sensor still off
    await _recompute(hass, entry)
    assert (_plan(hass, entry).state, _s(hass, head_a)) == ("cool", "cool")
    assert sent == ["fan_only", "cool"]


async def test_flapping_safety_sensor_vs_a_calling_room_reverses_once_per_wait(
    hass: HomeAssistant,
) -> None:
    """Control for the engaged case: the primary demands cool throughout."""
    entry, head_a, head_b = await _safety_setup(hass, **{CONF_RESTING_MODE_BIAS: MODE_COOL})
    coord = entry.runtime_data
    await _secondary_heating_vs_primary_calling_cool(hass, entry, head_b)
    expire_dwell(coord)
    sent = _mode_commands(hass, head_a)
    seen = []
    for toggle in ("on", "off", "on", "off"):
        hass.states.async_set(SAFETY_B, toggle)
        await _recompute(hass, entry)
        seen.append(_plan(hass, entry).state)
    assert (seen, _reversals(seen), sent) == (["heat"] * 4, 1, ["fan_only"])


@pytest.mark.parametrize(
    ("engaged", "resting"), [(False, None), (True, MODE_COOL)], ids=["calling", "engaged"]
)
async def test_a_dwelled_safety_clear_arms_the_wakeup(
    hass: HomeAssistant, engaged: bool, resting: str | None
) -> None:
    """The sensor clearing inside the wait the safety flip started is held, and
    the dwell wakeup is armed so the mode returns when the wait runs out.

    The engaged case needs a resting bias to have anywhere to return to: an
    engaged run inside its band demands nothing, so under Last used the mode
    stays until that room calls again (the flap test above covers that)."""
    extra = {} if resting is None else {CONF_RESTING_MODE_BIAS: resting}
    entry, head_a, head_b = await _safety_setup(hass, **extra)
    coord = entry.runtime_data
    if engaged:
        await _primary_coasting_in_band(hass, entry, head_a, head_b)
    else:
        await _secondary_heating_vs_primary_calling_cool(hass, entry, head_b)
    long_ago = expire_dwell(coord)
    hass.states.async_set(SAFETY_B, "on")
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == "heat"
    assert_restamped(coord, long_ago)
    hass.states.async_set(SAFETY_B, "off")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_change_allowed"]) == ("heat", False)
    assert coord._dwell_timer is not None
    expire_dwell(coord)  # the wait runs out
    await _recompute(hass, entry)
    assert (_plan(hass, entry).state, _s(hass, head_a)) == ("cool", "cool")


@pytest.mark.parametrize("sensor_state", ["off", "unavailable", "unknown", "On", "true", None])
async def test_only_an_on_safety_sensor_claims_priority(
    hass: HomeAssistant, sensor_state: str | None
) -> None:
    """Off, unavailable, unknown, any other spelling, or no entity at all is no
    claim: the enabled primary keeps the mode. The same sensor turning on then
    does take it, so the wiring under test is live."""
    entry, head_a, head_b = await _safety_setup(hass)
    coord = entry.runtime_data
    if sensor_state is not None:
        hass.states.async_set(SAFETY_B, sensor_state)
    await _secondary_heating_vs_primary_calling_cool(hass, entry, head_b)
    expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, _s(hass, head_a)) == ("cool", "cool")
    assert plan.attributes["zones"][1]["safety_priority"] is False
    assert plan.attributes["mode_by_safety"] is False
    hass.states.async_set(SAFETY_B, "on")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["zones"][1]["safety_priority"]) == ("heat", True)


async def test_safety_sensor_creates_no_want(hass: HomeAssistant) -> None:
    """A room with no heat or cool call of its own (disabled and held off, or
    enabled and coasting to target inside its band) gains nothing from its
    sensor being on; only its call is raised."""
    entry, head_a, head_b = await _setup(
        hass, enable=("_primary_enable", "_coordinator_enable"), hysteresis=None,
        safety=(None, SAFETY_B),
    )
    coord = entry.runtime_data
    hass.states.async_set(SAFETY_B, "on")
    await _set_temp(hass, SENSOR_A, 76)
    await _set_temp(hass, SENSOR_B, 60)  # the held-off secondary is cold
    expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, _s(hass, head_a), _s(hass, head_b)) == ("cool", "cool", "off")
    assert plan.attributes["zones"][1]["safety_priority"] is True
    assert plan.attributes["mode_by_safety"] is False
    # Enabled now, the secondary calls heat, and its sensor wins at once.
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), True)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, _s(hass, head_a), _s(hass, head_b)) == ("heat", "fan_only", "heat")
    assert plan.attributes["mode_by_safety"] is True
    # Back inside its band it still runs to target, but calls nothing: the
    # primary's call takes the mode once the wait has run out.
    await _set_temp(hass, SENSOR_B, 68)
    expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.attributes["secondary_demand"], plan.attributes["secondary_engage"]) == (
        "neutral", "heat",
    )
    assert (plan.state, _s(hass, head_a)) == ("cool", "cool")
    assert plan.attributes["mode_by_safety"] is False


async def test_standby_hold_silences_a_safety_vote(hass: HomeAssistant) -> None:
    """The hold outranks the safety tier: the released head is held off and
    casts no vote while the hold lasts."""
    hass.states.async_set(INHIBIT, "off")
    entry, _a, head_b = await _safety_setup(
        hass, **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_OFF},
    )
    await _secondary_heating_vs_primary_calling_cool(hass, entry, head_b)
    hass.states.async_set(SAFETY_B, "on")
    expire_dwell(entry.runtime_data)
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == "heat"
    hass.states.async_set(INHIBIT, "on")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.attributes["inhibited"] is True
    assert plan.attributes["zones"][1]["hands_off_vote"] is None
    assert plan.attributes["mode_by_safety"] is False
    assert _s(hass, head_b) == "off"


async def test_safety_sensor_is_tracked_only_when_configured(hass: HomeAssistant) -> None:
    await _setup(hass, hands_off=(False, True))
    assert _tracked(hass, SENSOR_A) >= 1  # the room sensors are; the helper sees them
    assert _tracked(hass, SAFETY_B) == 0


async def test_safety_sensor_alone_moves_the_plan_until_unload(hass: HomeAssistant) -> None:
    """The sensor's own change refreshes the plan (no _recompute here), and
    unloading the entry drops the subscription."""
    entry, _a, head_b = await _safety_setup(hass)
    assert _tracked(hass, SAFETY_B) == 1
    await _secondary_heating_vs_primary_calling_cool(hass, entry, head_b)
    expire_dwell(entry.runtime_data)
    hass.states.async_set(SAFETY_B, "on")
    for _ in range(3):
        await hass.async_block_till_done()
    assert _plan(hass, entry).state == "heat"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _tracked(hass, SAFETY_B) == 0


async def test_options_reload_subscribes_and_drops_the_safety_sensor(
    hass: HomeAssistant,
) -> None:
    """Set through Configure > Rooms: the reloaded coordinator follows the
    sensor. Cleared: the subscription and the stored key are gone."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    assert _tracked(hass, SAFETY_B) == 0
    manager = hass.config_entries.options
    result = await open_options_page(hass, entry, "room_2")
    result = await submit(manager, result, {**prefilled(result), "safety_priority_entity": SAFETY_B})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert _tracked(hass, SAFETY_B) == 1
    await _secondary_heating_vs_primary_calling_cool(hass, entry, head_b)
    expire_dwell(entry.runtime_data)
    hass.states.async_set(SAFETY_B, "on")
    for _ in range(3):
        await hass.async_block_till_done()
    assert _plan(hass, entry).state == "heat"

    result = await open_options_page(hass, entry, "room_2")
    values = {**prefilled(result)}
    values.pop("safety_priority_entity")  # the user cleared the selector
    result = await submit(manager, result, values)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert _tracked(hass, SAFETY_B) == 0
    assert [z.safety_priority_entity for z in entry.runtime_data.zones] == [None, None]
    assert ZONE_SAFETY_PRIORITY_ENTITY not in entry.data[CONF_ZONES][1]


# ---------------------------------------------------------------------------
# The ranking itself, pure
# ---------------------------------------------------------------------------
def _arb_demands(demands, votes, safety=None):
    """Build the (demand, vote) Demand pairs _zone_arbitration_pair would, for
    a test that wants to call :func:`arbitrate` directly without a Zone."""
    safety = safety or [False] * len(demands)
    pairs = []
    for i, (demand, vote, is_safety) in enumerate(zip(demands, votes, safety)):
        tier = PRIORITY_SAFETY if is_safety else None
        pairs.append(Demand(
            direction=demand if demand in ("cool", "heat") else None,
            priority=(tier if tier is not None else PRIORITY_DEMAND, i),
        ))
        pairs.append(Demand(
            direction=vote,
            priority=(tier if tier is not None else PRIORITY_VOTE, i),
            read_only=True,
        ))
    return pairs


def test_vote_ranking_pure() -> None:
    """Votes only fill silence; an enabled demand or run always decides --
    UNLESS a safety priority is asserted, which outranks either kind."""
    def decide(demands, engages, votes, current="cool", allowed=True, safety=None):
        engaged = any(e in ("cool", "heat") for e in engages)
        grant = arbitrate(
            _arb_demands(demands, votes, safety),
            engaged=engaged, current=current, allowed=allowed,
        )
        return (grant.direction, grant.read_only)

    # Silence: the vote decides, and says so (no dwell stamp).
    assert decide(["neutral", "off"], ["satisfied", "off"], [None, "heat"]) == ("heat", True)
    # An enabled demand in either direction outranks it.
    assert decide(["cool", "off"], ["cool", "off"], [None, "heat"]) == ("cool", False)
    assert decide(["neutral", "off"], ["cool", "off"], [None, "heat"]) == ("cool", False)
    assert decide(["heat", "off"], ["heat", "off"], ["cool", None], current="cool") == ("heat", False)
    # The dwell still gates a vote; nothing changes, so nothing is claimed.
    assert decide(["neutral", "off"], ["satisfied", "off"], [None, "heat"], allowed=False) == (
        "cool", False,
    )
    # Non-heat/cool observations are no vote at all.
    for junk in ("fan_only", "dry", "auto", "heat_cool", "off", "unavailable", None):
        assert decide(["neutral", "off"], ["satisfied", "off"], [None, junk]) == ("cool", False)
    # A safety-asserting VOTE preempts an ordinary DEMAND -- the gap that
    # motivated generalizing priority instead of hardcoding "demand beats
    # vote": zone 1 is hands-off and in safety distress (wants heat); zone 0
    # is merely comfort-calling cool. The safety vote wins despite being a
    # vote -- and reads back NOT read_only, since it's parking zone 0's
    # actively running demand: a real compressor reversal, stamped like any
    # other, so a flapping safety sensor can't reverse it twice inside one
    # hysteresis window.
    assert decide(
        ["cool", "off"], ["cool", "off"], [None, "heat"], safety=[False, True],
    ) == ("heat", False)
    # A safety-asserting DEMAND preempts an ordinary vote, and is NOT
    # read_only (it's a coordinated room's own demand, commands hardware).
    assert decide(
        ["heat", "off"], ["heat", "off"], ["cool", None], current="cool", safety=[True, False],
    ) == ("heat", False)
    # A safety vote deciding in silence (nothing commanding is running) is
    # still exempt -- only OVERRIDING an active commanding demand stamps.
    assert decide(
        ["neutral", "off"], ["satisfied", "off"], [None, "heat"], safety=[False, True],
    ) == ("heat", True)
    # An engaged run coasting inside its band is just as active: its demand
    # is neutral, but its head is running and the safety vote parks it, so
    # that is a stamped reversal too, not an exempt one.
    assert decide(
        ["neutral", "off"], ["cool", "off"], [None, "heat"], safety=[False, True],
    ) == ("heat", False)
    # The wait still holds a safety vote back, and once the vote holds the
    # mode there is nothing left to exempt.
    assert decide(
        ["cool", "off"], ["cool", "off"], [None, "heat"], allowed=False, safety=[False, True],
    ) == ("cool", False)
    assert decide(
        ["cool", "off"], ["cool", "off"], [None, "heat"], current="heat", safety=[False, True],
    ) == ("heat", False)


def test_by_safety_means_an_asserting_want_is_the_mode() -> None:
    """``by_safety`` (the plan's ``mode_by_safety``) is True while a safety
    room's want is the answer, even when order alone gives the same answer,
    and False when the wait holds the mode against that want."""
    def by_safety(demands, votes, current, allowed, safety):
        grant = arbitrate(
            _arb_demands(demands, votes, safety),
            engaged=any(d in ("cool", "heat") for d in demands),
            current=current, allowed=allowed,
        )
        return (grant.direction, grant.by_safety)

    # Both rooms want heat; the second happens to be marked safety.
    assert by_safety(["heat", "heat"], [None, None], "cool", True, [False, True]) == ("heat", True)
    assert by_safety(["heat", "heat"], [None, None], "cool", True, None) == ("heat", False)
    # The wait holds cool against the safety vote for heat.
    assert by_safety(["cool", "off"], [None, "heat"], "cool", False, [False, True]) == (
        "cool", False,
    )


def _shared_mode_with_votes_3_6_0(
    *,
    demands: list[str],
    engages: list[str],
    votes: list[str | None],
    current: str,
    allowed: bool,
    resting: str | None = None,
) -> tuple[str, bool]:
    """3.6.0's ``logic.shared_mode_with_votes``, copied verbatim as the oracle."""
    plain = shared_mode(
        demands=demands, current=current, allowed=allowed, resting=resting
    )
    calls = (MODE_COOL, MODE_HEAT)
    cast = [vote for vote in votes if vote in calls]
    if not cast or any(call in calls for call in (*demands, *engages)):
        return (plain, False)
    voted = shared_mode(demands=cast, current=current, allowed=allowed, resting=resting)
    return (voted, voted != plain)


def test_without_a_safety_sensor_arbitration_is_3_6_0() -> None:
    """Every two-room input, junk votes included: with no room marked safety,
    arbitrate() gives 3.6.0's mode and stamp decision, and never by_safety."""
    cases = 0
    for demands, engages, votes, current, allowed, resting in itertools.product(
        itertools.product(("cool", "heat", "neutral", "off"), repeat=2),
        itertools.product(("cool", "heat", "satisfied", "off"), repeat=2),
        itertools.product(("cool", "heat", None, "fan_only"), repeat=2),
        ("cool", "heat"),
        (True, False),
        (None, "cool", "heat"),
    ):
        old = _shared_mode_with_votes_3_6_0(
            demands=list(demands), engages=list(engages), votes=list(votes),
            current=current, allowed=allowed, resting=resting,
        )
        grant = arbitrate(
            _arb_demands(demands, votes),
            engaged=any(e in ("cool", "heat") for e in engages),
            current=current, allowed=allowed, resting=resting,
        )
        assert (grant.direction, grant.read_only, grant.by_safety) == (*old, False), (
            demands, engages, votes, current, allowed, resting,
        )
        cases += 1
    assert cases == 4**6 * 2 * 2 * 3


# ---------------------------------------------------------------------------
# Configure > Rooms > room page
# ---------------------------------------------------------------------------
async def test_room_page_opts_one_room_in_and_out(hass: HomeAssistant) -> None:
    """Off by default; on stores True on that room only; off removes the key."""
    entry, _a, _b = await _setup(hass)
    manager = hass.config_entries.options
    before = [dict(zone) for zone in entry.data[CONF_ZONES]]
    result = await open_options_page(hass, entry, "room_2")
    assert default(result, "hands_off") is False
    result = await submit(manager, result, {**prefilled(result), "hands_off": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_ZONES] == [before[0], {**before[1], ZONE_HANDS_OFF: True}]
    for store in (entry.data, entry.options):
        assert not {"hands_off", "primary_hands_off", "secondary_hands_off"} & set(store)
    assert [z.hands_off for z in entry.runtime_data.zones] == [False, True]

    result = await open_options_page(hass, entry, "room_2")
    assert default(result, "hands_off") is True
    result = await submit(manager, result, {**prefilled(result), "hands_off": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_ZONES] == before
    assert [z.hands_off for z in entry.runtime_data.zones] == [False, False]


_HANDS_OFF_LABEL = "Let its own remote run it while disabled"
_HANDS_OFF_DESCRIPTION = (
    "Off (the default): a disabled room's head is held off, as always. "
    "On: when you disable the room, MXZ turns the head off once, retrying until "
    "the head reports off. Then MXZ leaves the head alone until you enable the "
    "room again."
)


def test_room_page_copy_is_translated() -> None:
    """The field has this label and description in strings.json and en.json alike."""
    root = Path("custom_components/mxz_coordinator")
    copies = []
    for path in (root / "strings.json", root / "translations/en.json"):
        room = json.loads(path.read_text())["options"]["step"]["room"]
        copies.append((room["data"]["hands_off"], room["data_description"]["hands_off"]))
    assert copies == [(_HANDS_OFF_LABEL, _HANDS_OFF_DESCRIPTION)] * 2


def test_docs_name_the_setting_by_its_label() -> None:
    """README and MIGRATION.md use the field's label, never the retired one."""
    for path in (Path("README.md"), Path("docs/MIGRATION.md")):
        text = path.read_text()
        assert f"**{_HANDS_OFF_LABEL}**" in text, path
        assert "Hands-off when disabled" not in text, path
