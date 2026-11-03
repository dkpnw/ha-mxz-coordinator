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

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_restore_state_shutdown_restart,
)

from custom_components.mxz_coordinator.const import (
    CONF_MODE_HYSTERESIS,
    DEFAULT_MODE_HYSTERESIS,
    MODE_COOL,
    MODE_HEAT,
)
from custom_components.mxz_coordinator.coordinator import COAST_FOLLOWUP_DELAY
from tests.clocks import jump_wall_clock
from tests.dwell import RESTAMP_TOLERANCE, expire_dwell
from tests.test_drive import SENSOR_A, _eid, _recompute, _set_temp
from tests.test_dwell_wakeup import (
    COLD,
    HOT,
    NEUTRAL,
    _count_refresh_requests,
    _fire_due,
    _plan,
    _quiesce,
    _setup_dwell,
)
from tests.test_idle_action import (
    _head_calls,
    _record_calls,
    _settle_requested_refreshes,
)


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


# --- what is stored, and a restart ------------------------------------------------

STORE_KEY = "mxz_coordinator.{}.mode_change"  # the file name is part of the contract


def _stored(hass_storage: dict, entry) -> float:
    return hass_storage[STORE_KEY.format(entry.entry_id)]["data"]["last_mode_change"]


async def _restart(hass: HomeAssistant, entry) -> None:
    """Stop and start again: restore data dumped, the store read from storage.

    The unload drops the entry; dropping the shared store instances is what a
    new Home Assistant process starts with, so the setup reads what was written.
    """
    await async_mock_restore_state_shutdown_restart(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.data.pop("mxz_coordinator_mode_change_stores")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_mode_change_is_written_at_once_as_wall_clock_time(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """The record is the wall-clock time of the change, on disk without a shutdown."""
    entry, _a, _b = await _setup_dwell(hass, changed_ago=None)
    assert STORE_KEY.format(entry.entry_id) not in hass_storage  # nothing yet
    before = dt_util.utcnow().timestamp()
    await _flip_to_heat(hass, entry)
    after = dt_util.utcnow().timestamp()
    assert before <= _stored(hass_storage, entry) <= after


@pytest.mark.parametrize(
    ("passed", "held"), [(700, False), (400, True)], ids=["past", "part_way"]
)
async def test_restart_continues_the_hold_from_the_stored_change(
    hass: HomeAssistant, hass_storage: dict, monkeypatch: pytest.MonkeyPatch,
    passed: int, held: bool,
) -> None:
    """A Home Assistant restart times the hold from the stored change, as a reload does."""
    entry, _a, _b = await _setup_dwell(hass)
    await _flip_to_heat(hass, entry)
    stored = _stored(hass_storage, entry)

    jump_wall_clock(monkeypatch, passed)
    await _set_temp(hass, SENSOR_A, HOT[False])  # wants the change back
    await _restart(hass, entry)

    plan = _plan(hass, entry)
    if held:
        assert plan.attributes["mode_change_allowed"] is False
        assert plan.state == MODE_HEAT  # restored, and held there
        since = plan.attributes["seconds_since_mode_change"]
        assert passed <= since < passed + RESTAMP_TOLERANCE, since
        assert _stored(hass_storage, entry) == stored  # nothing changed, nothing written
    else:
        assert plan.attributes["mode_change_allowed"] is True
        assert plan.state == MODE_COOL  # changed back at once
        await hass.async_block_till_done()  # its write is scheduled for the next turn
        assert _stored(hass_storage, entry) > stored  # and that change was recorded


async def _flip_to(hass: HomeAssistant, entry, mode: str) -> None:
    """A real automatic change into ``mode``, stamped by the coordinator."""
    await _flip_to_heat(hass, entry)
    if mode == MODE_HEAT:
        return
    coord = entry.runtime_data
    expire_dwell(coord)  # the heat change's own hold is not the subject
    await _set_temp(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_COOL
    assert coord.current_shared_mode == MODE_COOL


def _let_time_pass(monkeypatch: pytest.MonkeyPatch, coord, seconds: float) -> None:
    """``seconds`` pass for one running coordinator: both of its clocks move.

    The wall clock carries the record; the monotonic stamp is what the gate
    reads within the run (``tests/clocks.py``).
    """
    jump_wall_clock(monkeypatch, seconds)
    coord._last_mode_change_ts -= seconds


@pytest.mark.parametrize(
    ("first", "wanted", "temp"),
    [(MODE_HEAT, MODE_COOL, HOT[False]), (MODE_COOL, MODE_HEAT, COLD[False])],
    ids=["heat_then_cool", "cool_then_heat"],
)
async def test_restart_mid_hold_keeps_the_mode_then_changes_when_the_hold_ends(
    hass: HomeAssistant, hass_storage: dict, monkeypatch: pytest.MonkeyPatch,
    first: str, wanted: str, temp: float,
) -> None:
    """End to end at the real 600 s window: a restart 400 s in holds the rest, no more.

    A change, 400 s, the room now wants the other direction, a restart: the
    plan stays where it was. 190 s later (10 s still left) it still holds,
    although the dwell wakeup has run. Once the window is past, the wakeup
    makes the change and a new, later change time is written.
    """
    entry, _a, _b = await _setup_dwell(hass)
    assert entry.runtime_data.hysteresis == DEFAULT_MODE_HYSTERESIS == 600
    await _flip_to(hass, entry, first)
    stored = _stored(hass_storage, entry)

    jump_wall_clock(monkeypatch, 400)
    await _set_temp(hass, SENSOR_A, temp)  # wants the other direction
    await _restart(hass, entry)
    coord = entry.runtime_data

    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_change_allowed"]) == (first, False)
    assert coord.current_shared_mode == first
    since = plan.attributes["seconds_since_mode_change"]
    assert 400 <= since < 400 + RESTAMP_TOLERANCE, since
    assert coord._dwell_timer is not None  # the restart armed the wakeup for the rest

    # 10 s short of the window: the wakeup has fired, the hold still holds.
    _let_time_pass(monkeypatch, coord, 190)
    await _fire_due(hass, 190)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_change_allowed"]) == (first, False)
    assert _stored(hass_storage, entry) == stored
    assert coord._dwell_timer is not None  # re-armed for the 10 s left

    # Past the window: the change goes through on the wakeup, no manual recompute.
    _let_time_pass(monkeypatch, coord, 10 + 2)
    await _fire_due(hass, 10 + 2)
    await hass.async_block_till_done()  # the record's write is scheduled for the next turn
    plan = _plan(hass, entry)
    assert plan.state == wanted
    assert coord.current_shared_mode == wanted
    new = _stored(hass_storage, entry)
    assert new - stored >= 400 + 190 + 12, (stored, new)  # the new change, not the old one


# --- no record, a future record, an unreadable record -----------------------------


async def test_fresh_install_does_not_wait_for_its_first_change(hass: HomeAssistant) -> None:
    """No change on record: the first cool/heat decision goes through at once.

    Negative control in the same run: that first change starts a full hold.
    """
    entry, _a, _b = await _setup_dwell(hass, changed_ago=None)
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    plan = _plan(hass, entry)
    assert plan.attributes["seconds_since_mode_change"] is None
    assert plan.attributes["mode_change_allowed"] is True
    assert _hold(hass, entry) == (0, None)

    await _set_temp(hass, SENSOR_A, COLD[False])
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_HEAT  # no wait
    remaining, _until = _hold(hass, entry)
    assert DEFAULT_MODE_HYSTERESIS - RESTAMP_TOLERANCE < remaining <= DEFAULT_MODE_HYSTERESIS

    await _set_temp(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_change_allowed"]) == (MODE_HEAT, False)


@pytest.mark.parametrize(
    ("changed_ago", "low", "high"),
    [
        (-3600, 600 - RESTAMP_TOLERANCE, 600),  # an hour in the future: the full window
        (-1, 600 - RESTAMP_TOLERANCE, 600),  # a second in the future: the same
        (100, 500 - RESTAMP_TOLERANCE, 500),  # control: a past time is honoured
    ],
    ids=["hour_ahead", "second_ahead", "control_past"],
)
async def test_a_stored_time_in_the_future_holds_the_full_window_and_no_longer(
    hass: HomeAssistant, changed_ago: float, low: float, high: float
) -> None:
    """A clock set back after the change: the stored time counts as now.

    The hold is then the configured window from this start, never the window
    plus however far ahead the stored time is.
    """
    entry, _a, _b = await _setup_dwell(hass, changed_ago=changed_ago)
    coord = entry.runtime_data
    assert low <= coord.mode_hold_remaining() <= high
    remaining, _until = _hold(hass, entry)
    assert low <= remaining <= high
    await _quiesce(hass, coord)
    await _set_temp(hass, SENSOR_A, COLD[False])
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_COOL  # held either way
    assert _plan(hass, entry).attributes["mode_change_allowed"] is False


@pytest.mark.parametrize(
    "data",
    [{"last_mode_change": "yesterday"}, {"last_mode_change": None}, {}, ["x"]],
    ids=["text", "null", "no_key", "not_a_dict"],
)
async def test_an_unreadable_record_holds_the_full_window(
    hass: HomeAssistant, hass_storage: dict, caplog: pytest.LogCaptureFixture, data: object
) -> None:
    """A record that is there but unreadable is not a fresh install: full window, said once."""
    hass_storage[STORE_KEY.format("badrecord")] = {"version": 1, "data": data}
    entry, _a, _b = await _setup_dwell(hass, entry_id="badrecord", changed_ago=None)
    coord = entry.runtime_data
    remaining = coord.mode_hold_remaining()
    assert DEFAULT_MODE_HYSTERESIS - RESTAMP_TOLERANCE < remaining <= DEFAULT_MODE_HYSTERESIS
    assert caplog.text.count("stored time of the last mode change is unreadable") == 1


# --- the thermostat attributes -----------------------------------------------------


def _hold(hass: HomeAssistant, entry, room: str = "primary") -> tuple[int, str | None]:
    attributes = hass.states.get(_eid(hass, entry, f"_{room}_thermostat")).attributes
    return attributes["mode_change_hold_remaining"], attributes["mode_change_hold_until"]


async def test_hold_attributes_show_the_gate_on_every_room(hass: HomeAssistant) -> None:
    """While the gate holds: whole seconds left and the UTC end, the same on each room.

    The values agree with the gate itself (``mode_change_allowed`` on the plan
    computed at the same moment) and with each other (end = now + remaining).
    """
    entry, _a, _b = await _setup_dwell(hass, changed_ago=200)
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    await _set_temp(hass, SENSOR_A, COLD[False])
    await _recompute(hass, entry)
    assert _plan(hass, entry).attributes["mode_change_allowed"] is False

    remaining, until = _hold(hass, entry)
    assert isinstance(remaining, int)
    assert 400 - RESTAMP_TOLERANCE < remaining <= 400
    end = dt_util.parse_datetime(until)
    assert end.utcoffset() == timedelta(0)  # UTC
    expected = dt_util.utcnow() + timedelta(seconds=remaining)
    assert abs((end - expected).total_seconds()) <= 2
    assert _hold(hass, entry, "secondary") == (remaining, until)

    # The gate opens with nothing waiting on it (the room is satisfied again).
    await _set_temp(hass, SENSOR_A, NEUTRAL[False])
    expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_change_allowed"]) == (MODE_COOL, True)
    assert _hold(hass, entry) == (0, None)
    assert _hold(hass, entry, "secondary") == (0, None)


async def test_no_hold_shows_zero_and_none(hass: HomeAssistant) -> None:
    """A window of 0 never holds: 0 and None, before and after a change."""
    entry, _a, _b = await _setup_dwell(hass, **{CONF_MODE_HYSTERESIS: 0})
    assert _hold(hass, entry) == (0, None)
    await _set_temp(hass, SENSOR_A, COLD[False])
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_HEAT
    assert _hold(hass, entry) == (0, None)


async def test_the_hold_end_rewrites_the_attributes_and_decides_nothing(
    hass: HomeAssistant,
) -> None:
    """At the end of the hold the thermostats are written again, with nothing else.

    The control: aging the clock alone leaves the old countdown on screen, so
    it is the hold-end wakeup that brings it to 0. It asks for no refresh and
    commands no head.
    """
    entry, head_a, head_b = await _setup_dwell(hass, changed_ago=None)
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    await _set_temp(hass, SENSOR_A, COLD[False])
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_HEAT
    await _fire_due(hass, COAST_FOLLOWUP_DELAY + 1)  # drain the change's own follow-ups
    await _settle_requested_refreshes(hass, coord)
    shown = _hold(hass, entry)
    assert shown[0] > 0

    expire_dwell(coord)
    await hass.async_block_till_done()
    assert _hold(hass, entry) == shown  # nothing has written them yet

    calls = _record_calls(hass)
    with _count_refresh_requests(coord) as requests:
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_MODE_HYSTERESIS + 2)
        )
        await hass.async_block_till_done()
    assert _hold(hass, entry) == (0, None)
    assert _hold(hass, entry, "secondary") == (0, None)
    assert requests == []
    assert _head_calls(calls, head_a) == [] and _head_calls(calls, head_b) == []
    assert coord._hold_end_timer is None


async def test_unload_cancels_the_hold_end_wakeup(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup_dwell(hass)  # a change just now: the wakeup is armed
    coord = entry.runtime_data
    assert coord._hold_end_timer is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coord._hold_end_timer is None


# --- a person's choice, and removing the entry -------------------------------------


async def test_a_manual_shared_mode_choice_is_recorded_and_survives_a_reload(
    hass: HomeAssistant, hass_storage: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking the other direction is a real change: stored, and held after a reload."""
    entry, _a, _b = await _setup_dwell(hass, changed_ago=None)
    await _quiesce(hass, entry.runtime_data)
    before = dt_util.utcnow().timestamp()
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": _eid(hass, entry, "_shared_mode"), "option": MODE_HEAT},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert _stored(hass_storage, entry) >= before

    jump_wall_clock(monkeypatch, 100)
    await _set_temp(hass, SENSOR_A, HOT[False])  # wants cool
    await _reload(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_change_allowed"]) == (MODE_HEAT, False)
    remaining, _until = _hold(hass, entry)
    assert 500 - RESTAMP_TOLERANCE < remaining <= 500


async def test_removing_the_entry_deletes_its_record(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """A removed entry leaves no record behind (#7: no previous install's values)."""
    entry, _a, _b = await _setup_dwell(hass)
    assert STORE_KEY.format(entry.entry_id) in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert STORE_KEY.format(entry.entry_id) not in hass_storage
    assert entry.entry_id not in hass.data.get("mxz_coordinator_mode_change_stores", {})
