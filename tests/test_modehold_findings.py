"""Each TLC counterexample from verification/modehold, replayed on the real integration.

F1 to F3 were found by the model and fixed: Resting mode waits for a room
still running in the shared mode (F1), the record keeps whether votes alone
chose the mode across a reload or restart (F2), and it keeps the mode itself,
written at the change, so a crash cannot restore an older one (F3). Every test
asserts the documented promise; each ``finding`` case failed before its fix,
and its control (the same steps without the trigger) passed before and after.
``verification/run`` requires both to pass. The steps are the model's steps,
one action each (.claude/skills/verify-mxz-formal/SKILL.md).

Requires pytest-homeassistant-custom-component. All entities are invented.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.helpers import restore_state as rs
from pytest_homeassistant_custom_component.common import (
    async_mock_load_restore_state_from_storage,
)

from custom_components.mxz_coordinator.const import CONF_RESTING_MODE_BIAS
from tests.clocks import jump_wall_clock
from tests.dwell import assert_restamped, expire_dwell
from tests.test_drive import SENSOR_A, SENSOR_B, _recompute, _set_temp
from tests.test_dwell_wakeup import COLD, HOT, _setup_dwell
from tests.test_hands_off import _plan, _remote, _s, _setup


@pytest.mark.parametrize(
    "resting", ["heat", "last"], ids=["finding", "control_last_used"]
)
async def test_f1_resting_mode_takes_the_mode_from_a_room_still_running(
    hass: HomeAssistant, resting: str,
) -> None:
    """PausedRunKept, model config finding-resting.

    A room whose head is running in the shared mode keeps it until the room
    reaches its target, unless another room's real call takes it. Resting
    mode Heat takes it from a cool run that has not reached its target.
    Control: the same steps with Resting mode Last used keep the run.
    """
    entry, head_a, _b = await _setup(
        hass, enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None, **{CONF_RESTING_MODE_BIAS: resting},
    )
    coord = entry.runtime_data
    expire_dwell(coord)
    await _set_temp(hass, SENSOR_A, 76)  # model: Reading pos 3 (calls cool)
    await _recompute(hass, entry)  # model: Decide
    assert (_plan(hass, entry).state, _s(hass, head_a)) == ("cool", "cool")
    await _set_temp(hass, SENSOR_A, 72)  # model: Reading pos 2 (still above target)
    expire_dwell(coord)  # model: Tick, Tick (the hold has passed)
    await _recompute(hass, entry)  # model: Decide
    plan = _plan(hass, entry)
    assert plan.attributes["zones"][0]["engage"] == "cool"  # the run is not done
    assert (plan.state, _s(hass, head_a)) == ("cool", "cool"), (
        plan.state, _s(hass, head_a),
    )


@pytest.mark.parametrize("reload", [True, False], ids=["finding", "control_no_reload"])
async def test_f2_a_reload_forgets_that_a_room_now_runs_in_the_voted_mode(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, reload: bool,
) -> None:
    """HoldKept, model config finding-reload-vote.

    A vote picked heat (no hold). An enabled room then runs heat in it: per
    the docs that run owns the mode, so the next reversal waits out the hold
    (tests/test_hands_off.py test_enabled_run_in_the_voted_mode_stamps_the_dwell
    proves it without a reload). With a reload between the vote and the run,
    the reversal comes at once. Control: the same steps without the reload.
    """
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    await _recompute(hass, entry)  # the park lands
    # The last real change is long past, on both clocks: the in-memory stamp,
    # and the wall clock the stored record is read against after the reload.
    expire_dwell(entry.runtime_data)
    jump_wall_clock(monkeypatch, 3600)
    await _remote(hass, head_b, "heat")  # model: HandsOff heat
    await _recompute(hass, entry)  # model: Decide (a vote-only change)
    assert _plan(hass, entry).state == "heat"
    if reload:
        assert await hass.config_entries.async_reload(entry.entry_id)  # model: Reload
        await hass.async_block_till_done()
    coord = entry.runtime_data
    reloaded = coord._last_mode_change_ts  # the stamp the reload read from the record
    await _set_temp(hass, SENSOR_A, 64)  # model: Reading (room 1 calls heat)
    await _recompute(hass, entry)  # model: Decide (room 1 runs heat in the voted mode)
    assert _s(hass, head_a) == "heat"
    await _remote(hass, head_b, "off")
    await _set_temp(hass, SENSOR_A, 76)  # model: Reading (room 1 now calls cool)
    await _recompute(hass, entry)  # model: Decide
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_change_allowed"]) == ("heat", False), (
        plan.state, plan.attributes["mode_change_allowed"], _s(hass, head_a),
    )
    assert_restamped(coord, reloaded)  # the run owned the mode: it was stamped


@pytest.mark.parametrize("crash", [True, False], ids=["finding", "control_clean_stop"])
async def test_f3_a_crash_soon_after_a_change_restores_the_old_mode_inside_the_hold(
    hass: HomeAssistant, hass_storage: dict, crash: bool,
) -> None:
    """HoldKept, model config finding-crash.

    The change's time is in its Store at once, but the shared mode is in the
    select's restore data, written every 15 minutes and at a stop. After a
    crash seconds after cool -> heat, MXZ starts in cool, holds cool for the
    rest of the wait it just started, and reverses the outdoor unit to cool.
    Control: a clean stop (restore data written at the stop) keeps heat.
    """
    entry, head_a, head_b = await _setup_dwell(hass, changed_ago=None)
    # model: RestoreDump (the periodic write, while the mode is still cool)
    await rs.async_get(hass).async_dump_states()
    await _set_temp(hass, SENSOR_A, COLD[False])  # room 1 (first) calls heat
    await _set_temp(hass, SENSOR_B, HOT[False])  # room 2 calls cool
    await _recompute(hass, entry)  # model: Decide (heat wins, the change is stamped)
    await hass.async_block_till_done()  # the record's write lands
    assert (_plan(hass, entry).state, _s(hass, head_a), _s(hass, head_b)) == (
        "heat", "heat", "fan_only",
    )
    # model: Crash. No stop: the restore data a new process reads is the dump
    # above. The Store record of the change is on disk. (Control: model
    # Restart, which writes restore data at the stop.)
    if not crash:
        await rs.async_get(hass).async_dump_states()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.data.pop("mxz_coordinator_mode_change_stores")
    await async_mock_load_restore_state_from_storage(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()  # model: Decide (the first plan)
    plan = _plan(hass, entry)
    assert plan.attributes["mode_change_allowed"] is False  # the wait it just began
    assert (plan.state, _s(hass, head_b)) == ("heat", "fan_only"), (
        plan.state, _s(hass, head_a), _s(hass, head_b),
    )
