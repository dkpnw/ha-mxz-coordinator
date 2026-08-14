"""off_after_dry: fan_only dwell after active COOLING, then park off.

The dwell is a timestamp comparison (the recompute cycle is the clock) with a
one-shot nudge timer for promptness — so tests drive it by rewinding the
stamped timestamp (the same private-dict idiom the restart tests use) and by
firing HA's time machinery for the nudge.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    async_fire_time_changed,
)

from custom_components.mxz_coordinator.const import (  # noqa: E402
    IDLE_ACTION_OFF_AFTER_DRY,
    MODE_COOL,
)
from tests.test_drive import (  # noqa: E402
    SENSOR_A,
    SENSOR_B,
    _recompute,
    _set_temp,
)
from tests.test_idle_action import _setup_idle  # noqa: E402


async def _setup_dry(hass: HomeAssistant):
    return await _setup_idle(hass, IDLE_ACTION_OFF_AFTER_DRY)


def _rewind(coord, head: str, seconds: float) -> None:
    """Age the head's last-active stamp by `seconds`."""
    mode, ts = coord._last_active[head]
    coord._last_active[head] = (mode, ts - seconds)


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

    _rewind(coord, head_a, coord.coil_dry_seconds + 5)
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
    _rewind(coord, head_a, coord.coil_dry_seconds + 5)
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

    _rewind(coord, head_a, coord.coil_dry_seconds + 5)
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
    _rewind(coord, head_a, coord.coil_dry_seconds + 5)
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
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "heat"

    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"
