"""Restart continuity: Home Assistant restarts and MXZ carries on.

* A mode change, 400 s, then a restart while a room wants the other
  direction: the mode holds for the 200 s left of the 600 s wait (not a fresh
  600), the thermostats show what is left, and once it ends the change goes
  through by itself.
* A room caught mid-run by a restart (inside its drift, still running to its
  target) is still running after the restart, and stops at its target.

"Restart" is an in-process round trip: restore data dumped as at shutdown,
the entry unloaded, storage re-read on setup. Not a new process, no hardware.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from mxz_journey import entity

from tests.clocks import jump_wall_clock
from tests.restart import restart
from tests.test_antiflip_persist import _flip_to_heat, _let_time_pass
from tests.test_drive import SENSOR_A, SENSOR_B, _recompute, _set_temp
from tests.test_dwell_wakeup import HOT, _fire_due, _setup_dwell
from tests.test_startup_continuity import INSIDE, NEARER, _run_b
from tests.test_startup_continuity import _setup as _setup_running

FEATURE = "restart-continuity"


async def test_a_restart_mid_wait_keeps_only_what_is_left(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, journal
) -> None:
    entry, head_a, head_b = await _setup_dwell(hass)
    log = journal(hass, entry, FEATURE, heads=[head_a, head_b])
    await _flip_to_heat(hass, entry)  # Room 1 reads 60: a real cool -> heat change
    shown = log.step("sensor: Room 1 reads 60; the mode changes to heat")
    assert shown["plan"]["state"] == "heat"

    jump_wall_clock(monkeypatch, 400)  # 400 s pass while Home Assistant is down
    await _set_temp(hass, SENSOR_A, HOT[False])  # Room 1 now wants cool
    await restart(hass, entry)
    tile = entity(hass, entry, "primary_thermostat")
    shown = log.step("restart: 400 s after the change, Room 1 now reads 75")
    plan = shown["plan"]
    assert (plan["state"], plan["mode_change_allowed"]) == ("heat", False)
    assert 400 <= plan["seconds_since_mode_change"] < 410
    left = shown["thermostats"][tile]["mode_change_hold_remaining"]
    assert 190 < left <= 200, left  # the rest of the wait, never a fresh 600
    assert shown["heads"][head_a]["state"] != "cool"

    # The rest of the wait passes on both clocks; the wakeup makes the change.
    _let_time_pass(monkeypatch, entry.runtime_data, left + 2)
    await _fire_due(hass, left + 2)
    shown = log.step("time: the 200 s left of the wait pass; nothing else happens")
    assert shown["plan"]["state"] == "cool"
    assert shown["heads"][head_a]["state"] == "cool"
    assert shown["thermostats"][tile]["mode_change_hold_remaining"] > 0  # a new wait


@pytest.mark.parametrize("mode", ["cool", "heat"])
async def test_a_run_in_progress_resumes_after_a_restart(
    hass: HomeAssistant, mode: str, journal
) -> None:
    entry, head_a, head_b = await _setup_running(hass)  # targets 70, no mode wait
    log = journal(hass, entry, FEATURE, heads=[head_a, head_b])
    await _run_b(hass, entry, head_b, mode)  # past the drift, then back inside it
    shown = log.step(f"sensor: Room 2 runs {mode}, then reads {INSIDE[mode]} (inside its drift)")
    assert (shown["plan"]["zones"][1]["engage"], shown["heads"][head_b]["state"]) == (mode, mode)

    await restart(hass, entry)
    shown = log.step("restart: Home Assistant stops and starts again")
    assert shown["plan"]["zones"][1]["engage"] == mode  # the run is still on
    assert shown["heads"][head_b]["state"] == mode
    assert shown["heads"][head_a]["state"] == "fan_only"  # the other room untouched

    await _set_temp(hass, SENSOR_B, NEARER[mode])
    await _recompute(hass, entry)
    shown = log.step(f"sensor: Room 2 reads {NEARER[mode]}")
    assert shown["heads"][head_b]["state"] == mode  # still running to the target

    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)
    shown = log.step("sensor: Room 2 reaches 70 (its target)")
    assert shown["plan"]["zones"][1]["engage"] == "satisfied"
    assert shown["heads"][head_b]["state"] == "fan_only"
