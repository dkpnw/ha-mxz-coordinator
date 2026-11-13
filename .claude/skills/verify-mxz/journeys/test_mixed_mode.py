"""Mixed-mode rooms: one room calls cool while another calls heat.

The outdoor unit runs one direction. Room 1 (Primary, priority position 1)
wins the standoff; Room 2 (Secondary) waits with its head parked and its run
paused. Once Room 1 is satisfied the mode turns to heat, but only when the
mode-change wait (default 600 s, stored just before setup) has run out, and
the wait's own wakeup makes that change: no manual recompute.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from mxz_journey import entity

from tests.dwell import expire_dwell
from tests.peek import armed
from tests.test_drive import SENSOR_A, SENSOR_B, _recompute, _set_temp
from tests.test_dwell_wakeup import _fire_due
from tests.test_hands_off import _setup

FEATURE = "mixed-mode-rooms"


async def test_priority_room_wins_then_the_waiting_room_resumes(
    hass: HomeAssistant, journal
) -> None:
    entry, head_a, head_b = await _setup(hass, hysteresis=None)  # the default wait
    coord = entry.runtime_data
    log = journal(hass, entry, FEATURE, heads=[head_a, head_b])
    tile_a = entity(hass, entry, "primary_thermostat")
    log.step("setup: both rooms enabled at target 70, a mode change stored just now")

    await _set_temp(hass, SENSOR_A, 76)  # Room 1 well above its target: calls cool
    await _set_temp(hass, SENSOR_B, 64)  # Room 2 well below its target: calls heat
    await _recompute(hass, entry)
    shown = log.step("sensors: Room 1 reads 76, Room 2 reads 64")
    plan = shown["plan"]
    assert (plan["state"], plan["standoff"]) == ("cool", True)
    assert [z["demand"] for z in plan["zones"]] == ["cool", "heat"]
    assert shown["heads"][head_a]["state"] == "cool"
    assert shown["heads"][head_b]["state"] == "fan_only"  # parked, waiting
    assert plan["zones"][1]["engage"] == "heat"  # its run is paused, not dropped

    await _set_temp(hass, SENSOR_A, 70)  # Room 1 reaches its target
    await _recompute(hass, entry)
    shown = log.step("sensor: Room 1 reaches 70 (its target)")
    plan = shown["plan"]
    assert plan["zones"][0]["engage"] == "satisfied"
    assert (plan["state"], plan["mode_change_allowed"]) == ("cool", False)  # the wait holds
    assert shown["heads"][head_b]["state"] == "fan_only"
    assert shown["thermostats"][tile_a]["mode_change_hold_remaining"] > 0
    assert armed(coord, "dwell")  # the wait's end will wake the plan

    expire_dwell(coord)  # the wait runs out (the suite's clock idiom)
    await _fire_due(hass, coord.hysteresis + 2)
    shown = log.step("time: the 600 s wait runs out; nothing else happens")
    plan = shown["plan"]
    assert plan["state"] == "heat"
    assert shown["heads"][head_b]["state"] == "heat"  # the paused run resumes
    assert shown["heads"][head_a]["state"] == "fan_only"
    assert shown["thermostats"][tile_a]["mode_change_hold_remaining"] > 0  # a new wait

    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)
    shown = log.step("sensor: Room 2 reaches 70 (its target)")
    assert shown["plan"]["zones"][1]["engage"] == "satisfied"
    assert shown["heads"][head_b]["state"] == "fan_only"
    assert shown["plan"]["state"] == "heat"  # Last used: the mode rests where it was
    # Room 2's head was never sent cool, and was sent heat (the resumed run).
    modes_b = [c["data"]["hvac_mode"] for c in log.head_calls(head_b) if "hvac_mode" in c["data"]]
    assert "cool" not in modes_b and "heat" in modes_b, modes_b
