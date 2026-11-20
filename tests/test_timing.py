"""timing.py and planner.held_back, called directly: no Home Assistant, no pytest-homeassistant.

Each expectation is worked out from the rule it states, and each rule has a case where it says
no. Every value is invented.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.mxz_coordinator.const import (
    DEMAND_NEUTRAL,
    ENGAGE_SATISFIED,
    HEALTH_AWAITING,
    HEALTH_CADENCE_UNKNOWN,
    HEALTH_HEALTHY,
    HEALTH_INVALID,
    HEALTH_STALE,
    MODE_COOL,
    MODE_HEAT,
    VOTE_EXPIRED,
    VOTE_LIVE,
)
from custom_components.mxz_coordinator.planner import held_back
from custom_components.mxz_coordinator.timing import (
    COAST_FOLLOWUP_DELAY,
    HEARTBEAT,
    REQUEST_COOLDOWN,
    WAKEUPS,
    Refresh,
    cloud_delay,
    coast_delay,
    coil_dry_delay,
    dwell_delay,
    fresh_delay,
    head_refresh,
    head_shows_new,
    hold_end_delay,
    nearest,
    opening_delay,
    opening_wait,
    run_changed,
    same_reading_refresh,
    startup_quiet,
)

FAN = {"fan_mode": "auto", "fan_modes": ["auto", "low"], "supported_features": 395}


def test_a_start_is_quiet_until_its_startup_wakeup_runs() -> None:
    assert startup_quiet(running=False, startup_ran=False)
    assert not startup_quiet(running=False, startup_ran=True)
    # A reload while Home Assistant runs is never quiet, before or after the wakeup.
    assert not startup_quiet(running=True, startup_ran=False)
    assert not startup_quiet(running=True, startup_ran=True)


def test_the_constants() -> None:
    assert (REQUEST_COOLDOWN, HEARTBEAT, COAST_FOLLOWUP_DELAY) == (10, 900, 11)
    assert isinstance(COAST_FOLLOWUP_DELAY, int)


def test_what_each_wakeup_asks_for() -> None:
    assert {name for name, refresh in WAKEUPS.items() if refresh is Refresh.NOW} == {"settle", "opening"}
    assert {name for name, refresh in WAKEUPS.items() if refresh is None} == {"hold_end", "heal"}
    assert {name for name, refresh in WAKEUPS.items() if refresh is Refresh.REQUEST} == {
        "fresh", "dwell", "coast", "dry", "lost", "comfort", "cloud", "startup", "dry_pick",
    }


@pytest.mark.parametrize(
    ("changed", "moved", "refresh"),
    [
        (True, True, Refresh.NOW),
        (True, False, Refresh.NOW),
        (False, True, Refresh.REPORT),
        (False, False, None),
    ],
)
def test_a_head_report_asks_at_once_for_a_run_change_else_as_a_report(
    changed: bool, moved: bool, refresh: Refresh | None
) -> None:
    assert head_refresh(run_changed=changed, moved=moved) is refresh


def test_a_run_changes_with_the_mode_or_the_action() -> None:
    assert run_changed(MODE_HEAT, MODE_COOL, "idle", "idle")
    assert run_changed(MODE_HEAT, MODE_HEAT, "idle", "heating")
    assert not run_changed(MODE_HEAT, MODE_HEAT, "heating", "heating")


def test_what_a_head_shows_new() -> None:
    assert not head_shows_new(MODE_COOL, MODE_HEAT, FAN, dict(FAN))  # the mode alone: no
    assert head_shows_new(MODE_COOL, MODE_COOL, FAN, {**FAN, "fan_mode": "low"})
    assert head_shows_new(MODE_COOL, MODE_COOL, FAN, {**FAN, "fan_modes": ["auto"]})
    assert head_shows_new(MODE_COOL, MODE_COOL, FAN, {**FAN, "supported_features": 1})
    # The same value of another type is a change (395 against 395.0).
    assert head_shows_new(MODE_COOL, MODE_COOL, FAN, {**FAN, "supported_features": 395.0})
    assert head_shows_new(MODE_COOL, "unavailable", FAN, dict(FAN))
    assert head_shows_new(None, MODE_COOL, {}, {})
    # Unavailable to no state at all is out of sight either way.
    assert not head_shows_new("unavailable", None, {}, {})


@pytest.mark.parametrize("health", [HEALTH_STALE, HEALTH_INVALID, HEALTH_AWAITING])
def test_an_unchanged_reading_asks_only_for_a_room_waiting_on_a_report(health: str) -> None:
    assert same_reading_refresh([HEALTH_HEALTHY, health]) is Refresh.REQUEST
    assert same_reading_refresh([HEALTH_HEALTHY, HEALTH_CADENCE_UNKNOWN, None]) is None
    assert same_reading_refresh([]) is None


def test_nearest() -> None:
    assert nearest([30.0, None, 5.0, 12.0]) == 5.0
    assert nearest([None, None]) is None
    assert nearest([]) is None


def test_the_fresh_wakeup_is_at_the_nearest_deadline_and_never_negative() -> None:
    assert fresh_delay([1300.0, 1180.0], 1000.0) == 180.0
    assert fresh_delay([990.0], 1000.0) == 0.0
    assert fresh_delay([], 1000.0) is None


def _dwell(remaining: float, **changes: Any) -> float | None:
    return dwell_delay(remaining, **{"active": True, "allowed": False, "held_back": lambda: True, **changes})


def test_the_dwell_wakeup_is_a_second_after_the_hold_while_it_holds_a_change_back() -> None:
    assert _dwell(300.0) == 301.0
    assert _dwell(0.0) == 0  # ran out while the plan was applied: at once
    assert isinstance(_dwell(-5.0), int)
    assert _dwell(-math.inf) == 0
    assert _dwell(300.0, allowed=True) == 301.0  # the plan had it open, the hold has restarted


@pytest.mark.parametrize(
    ("remaining", "changes"),
    [
        (300.0, {"active": False}),
        (0.0, {"allowed": True}),
        (-math.inf, {"allowed": True}),
        (300.0, {"held_back": lambda: False}),
    ],
)
def test_no_dwell_wakeup(remaining: float, changes: dict[str, Any]) -> None:
    assert _dwell(remaining, **changes) is None


def test_whether_the_hold_is_held_back_is_read_only_when_it_matters() -> None:
    def never() -> bool:
        raise AssertionError("read")

    assert _dwell(300.0, active=False, held_back=never) is None
    assert _dwell(0.0, allowed=True, held_back=never) is None


def test_the_coast_wakeup() -> None:
    assert coast_delay(True, active=True) == COAST_FOLLOWUP_DELAY
    assert coast_delay(False, active=True) is None
    assert coast_delay(True, active=False) is None


def test_the_coil_dry_wakeup_is_a_second_after_the_run() -> None:
    assert coil_dry_delay(110.0) == 111.0


def test_the_cloud_wakeup_is_the_nearest_deadline_still_ahead() -> None:
    assert cloud_delay([-3.0, 0.0, 40.0, 25.0]) == 25.0
    assert cloud_delay([-3.0, 0.0]) is None


def test_a_door_or_window_wakeup() -> None:
    assert opening_wait("closed", open_delay=60.0, close_delay=120.0) == 120.0
    assert opening_wait("open", open_delay=60.0, close_delay=120.0) == 60.0
    assert opening_delay(5.0) == 5.0
    assert opening_delay(-2.0) == 0.0


def test_the_hold_end_wakeup() -> None:
    assert hold_end_delay(600.0) == 601.0
    assert hold_end_delay(0.0) is None
    assert hold_end_delay(-1.0) is None


def _room(
    demand: str = DEMAND_NEUTRAL, engage: str = ENGAGE_SATISFIED, *, status: str = VOTE_LIVE,
    vote: str | None = None, safety: bool = False, floor: str | None = None,
) -> Any:
    return SimpleNamespace(
        demand=demand, engage=engage, vote_status=status, hands_off_vote=vote,
        safety_priority=safety, cloud_out=False, floor_want=floor, dry_vote=None,
        comfort_priority=0.0,
    )


def test_a_call_for_the_other_mode_is_held_back() -> None:
    assert held_back([_room(MODE_HEAT)], current=MODE_COOL, resting=None)
    assert not held_back([_room(MODE_COOL)], current=MODE_COOL, resting=None)
    assert not held_back([_room()], current=MODE_COOL, resting=None)


def test_a_hands_off_vote_counts_and_an_expired_room_does_not() -> None:
    assert held_back([_room(vote=MODE_HEAT)], current=MODE_COOL, resting=None)
    assert not held_back([_room(MODE_HEAT, status=VOTE_EXPIRED)], current=MODE_COOL, resting=None)
    assert held_back(
        [_room(MODE_HEAT, status=VOTE_EXPIRED, safety=True)], current=MODE_COOL, resting=None
    )


def test_resting_mode_with_no_call() -> None:
    assert held_back([_room()], current=MODE_COOL, resting=MODE_HEAT)
    assert not held_back([_room()], current=MODE_HEAT, resting=MODE_HEAT)
