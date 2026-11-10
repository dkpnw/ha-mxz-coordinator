"""off_after_dry: fan_only dwell after active COOLING, then park off.

The dwell is a timestamp comparison on the monotonic clock (the recompute
cycle is the clock) with a one-shot nudge timer for promptness — so tests drive
it by rewinding the stamped timestamp on its own clock (``tests/clocks.py``, the
same private-dict idiom the restart tests use) and by firing HA's time machinery
for the nudge.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
)

from custom_components.mxz_coordinator.const import (
    IDLE_ACTION_OFF_AFTER_DRY,
    MODE_COOL,
)
from tests.clocks import jump_wall_clock, pass_time
from tests.dwell import RESTAMP_TOLERANCE
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _recompute,
    _set_temp,
)
from tests.test_idle_action import _setup_idle


async def _setup_dry(hass: HomeAssistant):
    return await _setup_idle(hass, IDLE_ACTION_OFF_AFTER_DRY)


async def test_dwell_after_cooling_then_off(hass: HomeAssistant) -> None:
    """Satisfied after cooling -> fan_only dwell; dwell expiry -> off."""
    entry, head_a, _b = await _setup_dry(hass)
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"

    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"  # dwelling, coil drying
    assert head_a in coord._dry_timers  # nudge armed

    pass_time(coord, coord.coil_dry_seconds + 5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"
    assert head_a not in coord._dry_timers  # nudge cancelled with the park


async def test_heat_leaves_no_wet_coil_immediate_off(hass: HomeAssistant) -> None:
    """A head that was HEATING parks straight off — no dwell owed."""
    entry, head_a, _b = await _setup_dry(hass)
    await _set_temp(hass, SENSOR_A, 60)
    await _set_temp(hass, SENSOR_B, 60)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "heat"

    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"


async def test_never_ran_head_parks_off_immediately(hass: HomeAssistant) -> None:
    """A head with no conditioning history owes no dwell."""
    entry, _a, head_b = await _setup_dry(hass)
    await _set_temp(hass, SENSOR_A, 75)  # primary runs; secondary never does
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == "off"


async def test_reengage_mid_dwell_cancels_the_nudge(hass: HomeAssistant) -> None:
    """The room calling again mid-dwell resumes cooling and drops the timer."""
    entry, head_a, _b = await _setup_dry(hass)
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"
    assert head_a in coord._dry_timers

    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    assert head_a not in coord._dry_timers


async def test_nudge_timer_flips_the_head_without_another_trigger(
    hass: HomeAssistant,
) -> None:
    """Dwell expiry alone (no sensor event, no heartbeat) parks the head."""
    entry, head_a, _b = await _setup_dry(hass)
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"

    # Age the stamp past the dwell, then let the scheduled nudge fire.
    pass_time(coord, coord.coil_dry_seconds + 5)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=coord.coil_dry_seconds + 60)
    )
    await hass.async_block_till_done()
    assert hass.states.get(head_a).state == "off"


# --- restart seeds -------------------------------------------------------------


async def test_restart_seed_observed_cooling_restarts_dwell(
    hass: HomeAssistant,
) -> None:
    """Head found still cooling -> it was cooling until the restart: full dwell."""
    entry, head_a, _b = await _setup_dry(hass)
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"

    coord._last_active.clear()  # restart wipes the in-memory stamp
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"  # dwell reconstructed
    assert coord._last_active[head_a][0] == MODE_COOL


async def test_restart_seed_observed_fan_only_restarts_dwell(
    hass: HomeAssistant,
) -> None:
    """Head found mid-dwell (fan_only) -> the dwell restarts, never strands."""
    entry, head_a, _b = await _setup_dry(hass)
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"

    coord._last_active.clear()
    coord._cancel_dry_timer(head_a)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"  # dwell running again

    pass_time(coord, coord.coil_dry_seconds + 5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"  # ...and it ends


async def test_restart_seed_observed_off_stays_off(hass: HomeAssistant) -> None:
    """Head found off owes nothing — no dwell, no wake."""
    entry, head_a, _b = await _setup_dry(hass)
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    pass_time(coord, coord.coil_dry_seconds + 5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"

    coord._last_active.clear()
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"
    assert head_a not in coord._dry_timers


async def test_mode_flip_cool_then_heat_owes_no_dwell(hass: HomeAssistant) -> None:
    """Cooling long ago, then heating: the HEAT stamp wins -> straight off."""
    entry, head_a, _b = await _setup_dry(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    await _set_temp(hass, SENSOR_A, 60)
    # A direction flip disengages the room for one cycle (no whiplash): the
    # head parks for that cycle and engages heat on the next compute.
    await _recompute(hass, entry)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "heat"

    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"


# --- the coil-dry clock --------------------------------------------------------


def _nudge_in(hass: HomeAssistant, coord, head: str) -> float:
    """Seconds until the head's coil-dry nudge fires, on the event loop's clock."""
    return coord._dry_timers[head].__self__.when() - hass.loop.time()


async def _dwelling(hass: HomeAssistant, origin: str):
    """A head in its coil-dry wait after cooling: stamped live, or seeded the
    way a restart reconstructs it from the head observed in fan_only."""
    entry, head_a, _b = await _setup_dry(hass)
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"
    if origin == "seeded":
        coord._last_active.clear()
        coord._cancel_dry_timer(head_a)
        await _recompute(hass, entry)
        assert hass.states.get(head_a).state == "fan_only"
    return entry, coord, head_a


async def test_the_coil_dry_wait_counts_on_the_monotonic_clock(hass: HomeAssistant) -> None:
    """The stamp reads as now on time.monotonic, and the wait ends exactly when
    the coil-dry length has passed on that clock: one minute short of it the
    head still dries and the nudge is set for what is left, plus one second."""
    entry, coord, head_a = await _dwelling(hass, "live")
    stamp = coord._last_active[head_a][1]
    now = time.monotonic()
    assert now - RESTAMP_TOLERANCE < stamp <= now, (stamp, now)

    pass_time(coord, coord.coil_dry_seconds - 60)
    coord._cancel_dry_timer(head_a)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"
    assert 61 - RESTAMP_TOLERANCE < _nudge_in(hass, coord, head_a) <= 61

    pass_time(coord, 60)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"
    assert head_a not in coord._dry_timers


@pytest.mark.parametrize("origin", ["live", "seeded"])
@pytest.mark.parametrize("step", [1, -1], ids=["forward", "back"])
async def test_a_wall_clock_step_neither_cuts_nor_stretches_the_coil_dry_wait(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, origin: str, step: int
) -> None:
    """An NTP step or a clock set by hand, an hour past the whole wait either
    way: the head keeps drying, the nudge is still set for the coil-dry length
    from the stamp, and the wait ends when that length has passed on the
    monotonic clock, not on the wall clock."""
    entry, coord, head_a = await _dwelling(hass, origin)
    jump_wall_clock(monkeypatch, step * (coord.coil_dry_seconds + 3600))
    coord._cancel_dry_timer(head_a)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only", "the step did not end the wait"
    left = _nudge_in(hass, coord, head_a)
    assert coord.coil_dry_seconds + 1 - RESTAMP_TOLERANCE < left <= coord.coil_dry_seconds + 1

    pass_time(coord, coord.coil_dry_seconds - 60)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"
    pass_time(coord, 60)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off", "and did not stretch it"
