"""The mode-flip hold (anti-flip dwell) survives a reload and a restart.

The hold is the wait between two cool<->heat changes of the one outdoor unit
(``mode_hysteresis``, 600 s by default). It is timed from the last real mode
change. A reload or a Home Assistant restart must time it from that change, not
from the moment the new coordinator started: a house whose last change was long
ago must not wait a fresh full window after every restart.

"Time passes" here is the wall clock stepped forward (``tests/clocks.py``):
across a reload or a restart the old coordinator's in-memory clock is gone, and
what the new one can know is the wall-clock time of the last change it finds
stored. The monotonic clock does not move with the step, so within one
coordinator nothing here shortens the hold.

Requires pytest-homeassistant-custom-component.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import (
    DEFAULT_MODE_HYSTERESIS,
    MODE_COOL,
    MODE_HEAT,
)
from tests.clocks import jump_wall_clock
from tests.dwell import RESTAMP_TOLERANCE, expire_dwell
from tests.test_drive import SENSOR_A, _recompute, _set_temp
from tests.test_dwell_wakeup import COLD, HOT, _plan, _quiesce, _setup_dwell


async def _flip_to_heat(hass: HomeAssistant, entry) -> None:
    """A real automatic cool -> heat change, stamped by the coordinator."""
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    assert coord.current_shared_mode == MODE_COOL
    expire_dwell(coord)  # whatever held before, the coming change is allowed
    await _set_temp(hass, SENSOR_A, COLD[False])
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_HEAT
    assert coord.current_shared_mode == MODE_HEAT


async def _reload(hass: HomeAssistant, entry):
    old = entry.runtime_data
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data is not old
    return entry.runtime_data


async def test_reload_after_the_window_has_passed_does_not_wait_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Last change 700 s ago (window 600 s), then a reload: no fresh wait.

    The room now wants cool. Its change back is allowed at once, because the
    last change is older than the window; a fresh 600 s wait from the reload
    is the defect.
    """
    entry, _a, _b = await _setup_dwell(hass)
    assert entry.runtime_data.hysteresis == DEFAULT_MODE_HYSTERESIS == 600
    await _flip_to_heat(hass, entry)

    jump_wall_clock(monkeypatch, 700)
    await _set_temp(hass, SENSOR_A, HOT[False])
    coord = await _reload(hass, entry)

    # The plan the reload computed: it made the change back at once.
    plan = _plan(hass, entry)
    assert plan.attributes["mode_change_allowed"] is True
    assert plan.attributes["seconds_since_mode_change"] >= 700
    assert plan.state == MODE_COOL
    assert coord.current_shared_mode == MODE_COOL


async def test_reload_part_way_through_the_window_keeps_only_what_is_left(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Last change 400 s ago, then a reload: the hold continues, it does not restart.

    The change back is still held (200 s are left), and the plan counts the
    time since the real change, not since the reload.
    """
    entry, _a, _b = await _setup_dwell(hass)
    await _flip_to_heat(hass, entry)

    jump_wall_clock(monkeypatch, 400)
    await _set_temp(hass, SENSOR_A, HOT[False])
    await _reload(hass, entry)

    plan = _plan(hass, entry)
    assert plan.attributes["mode_change_allowed"] is False
    assert plan.state == MODE_HEAT
    since = plan.attributes["seconds_since_mode_change"]
    assert 400 <= since < 400 + RESTAMP_TOLERANCE, since
