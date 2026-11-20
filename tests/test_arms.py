"""timing.arms, cloud_arm and hold_end_arm, called directly: no pytest-homeassistant.

Each expectation is the rule each wakeup states (what it waits for, which waits count, which
gate cancels it), worked by hand from invented numbers; a head's record is a stand-in that
answers its three waits. Each gate has a case where it holds and one where it does not.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from custom_components.mxz_coordinator import sim_timing
from custom_components.mxz_coordinator.timing import (
    COAST_FOLLOWUP_DELAY,
    Arm,
    CancelAll,
    CloudWaits,
    Refresh,
    Wakeups,
    Watched,
    arms,
    cloud_arm,
    hold_end_arm,
)

CLOCK = 1000.0
WALL = 1_800_000_000.0


class Sight:
    """A head's record that answers its waits with fixed numbers (None: nothing to wait for)."""

    def __init__(self, expires: float | None = None, hold: float | None = None,
                 settles: float | None = None) -> None:
        self.waits = (expires, hold, settles)

    def expires_in(self, now: float, ttl: float) -> float | None:
        assert (now, ttl) == (CLOCK, 300.0)
        return self.waits[0]

    def hold_ends_in(self, now: float, hold: float) -> float | None:
        assert (now, hold) == (CLOCK, 120.0)
        return self.waits[1]

    def settles_in(self, now: float) -> float | None:
        assert now == CLOCK
        return self.waits[2]


def _never() -> bool:
    raise AssertionError("held_back read when it cannot matter")


def _w(**change: Any) -> Wakeups:
    w = Wakeups(
        retired=False, clock_retired=False, coordinator=True, standby=False, clock=CLOCK,
        wall=WALL, remaining=-float("inf"), allowed=True, held_back=_never, coasted=False,
        fresh=(), sights=(), rooms=(), lost_ttl=300.0, action_hold=120.0,
        comfort_stale_after=None, comfort=(), clouds=(), openings=(),
    )
    return replace(w, **change)


def _by_name(w: Wakeups) -> dict[Any, Any]:
    return {
        (arm.name, arm.key) if isinstance(arm, Arm) else ("*", arm.name): arm.delay
        if isinstance(arm, Arm) else None
        for arm in arms(w)
    }


def _delay(w: Wakeups, name: str, key: Any = None) -> float | None:
    return _by_name(w)[name, key]


def test_the_order_is_the_coordinators() -> None:
    w = _w(openings=[("study", WALL + 5), ("den", None), ("hall", WALL + 9)])
    assert [
        (arm.name, arm.key) if isinstance(arm, Arm) else ("cancel all", arm.name) for arm in arms(w)
    ] == [
        ("dwell", None), ("coast", None), ("fresh", None), ("lost", None), ("settle", None),
        ("comfort", None), ("cloud", None), ("cancel all", "opening"),
        ("opening", "study"), ("opening", "hall"),
    ]


def test_with_nothing_to_wait_for_every_wakeup_is_cancelled() -> None:
    assert all(delay is None for delay in _by_name(_w()).values())


# -- dwell -----------------------------------------------------------------------------------


def test_a_flip_the_hold_holds_back_wakes_just_after_it_ends() -> None:
    assert _delay(_w(remaining=30.0, allowed=False, held_back=lambda: True), "dwell") == 31.0
    assert _delay(_w(remaining=30.0, allowed=False, held_back=lambda: False), "dwell") is None


def test_a_hold_that_ended_while_the_plan_was_applied_wakes_at_once() -> None:
    assert _delay(_w(remaining=-2.0, allowed=False, held_back=lambda: True), "dwell") == 0
    assert _delay(_w(remaining=-2.0, allowed=True), "dwell") is None


@pytest.mark.parametrize(
    "gate", [{"clock_retired": True}, {"coordinator": False}, {"standby": True}],
    ids=["clock_retired", "coordinator_off", "standby_park"],
)
def test_the_dwell_wakeup_is_cancelled_where_a_recompute_could_command_nothing(gate) -> None:
    assert _delay(_w(remaining=30.0, allowed=False, **gate), "dwell") is None


def test_a_retired_coordinator_whose_clock_runs_still_syncs_the_dwell() -> None:
    w = _w(retired=True, remaining=30.0, allowed=False, held_back=lambda: True)
    assert _delay(w, "dwell") == 31.0


# -- coast ------------------------------------------------------------------------------------


def test_a_coast_is_looked_at_just_after_the_cooldown() -> None:
    assert _delay(_w(coasted=True), "coast") == COAST_FOLLOWUP_DELAY == 11
    assert _delay(_w(coasted=True, retired=True), "coast") == 11
    assert _delay(_w(coasted=False), "coast") is None


@pytest.mark.parametrize("gate", [{"coordinator": False}, {"standby": True}])
def test_no_coast_wakeup_when_the_coordinator_could_command_nothing(gate) -> None:
    assert _delay(_w(coasted=True, **gate), "coast") is None


# -- fresh ------------------------------------------------------------------------------------


def test_the_fresh_wakeup_is_at_the_nearest_deadline_and_no_later() -> None:
    assert _delay(_w(fresh=[WALL + 90, WALL + 30]), "fresh") == 30.0
    assert _delay(_w(fresh=[WALL - 5]), "fresh") == 0.0
    assert _delay(_w(fresh=[], retired=True), "fresh") is None


# -- lost and settle --------------------------------------------------------------------------


def test_the_lost_wakeup_counts_every_heads_expiry_and_only_action_rooms_holds() -> None:
    action = Sight(expires=200.0, hold=50.0)
    mode = Sight(expires=80.0, hold=10.0)
    w = _w(
        sights=[action, mode],
        rooms=[Watched(action, by_action=True, released=False), Watched(mode, by_action=False, released=True)],
    )
    assert _delay(w, "lost") == 50.0
    assert _delay(replace(w, rooms=[]), "lost") == 80.0
    assert _delay(_w(sights=[Sight()]), "lost") is None


def test_the_settle_wakeup_counts_only_released_rooms_that_vote_by_their_run() -> None:
    rooms = [
        Watched(Sight(settles=4.0), by_action=True, released=False),
        Watched(Sight(settles=6.0), by_action=False, released=True),
        Watched(Sight(settles=9.0), by_action=True, released=True),
    ]
    assert _delay(_w(rooms=rooms), "settle") == 9.0
    assert _delay(_w(rooms=rooms, retired=True), "settle") is None
    assert _delay(_w(rooms=rooms[:2]), "settle") is None


# -- comfort ------------------------------------------------------------------------------


def test_the_comfort_wakeup_is_when_the_nearest_on_goes_stale() -> None:
    comfort = [("input_boolean", "on", 600.0), ("binary_sensor", "on", 100.0), ("input_boolean", "off", 0.0)]
    assert _delay(_w(comfort_stale_after=3600.0, comfort=comfort), "comfort") == 3000.0


@pytest.mark.parametrize("gate", [{"retired": True}, {"comfort_stale_after": None}])
def test_no_comfort_wakeup_once_retired_or_with_no_stale_time(gate) -> None:
    w = _w(comfort_stale_after=3600.0, comfort=[("input_boolean", "on", 600.0)])
    assert _delay(replace(w, **gate), "comfort") is None


# -- cloud ------------------------------------------------------------------------------


def _cloud(**change: Any) -> CloudWaits:
    return replace(
        CloudWaits(pending=False, window_until=0.0, release_at=None, unresponsive=False, seen=None),
        **change,
    )


def test_the_cloud_wakeup_is_the_nearest_deadline_still_ahead() -> None:
    clouds = [
        _cloud(pending=True, window_until=CLOCK + 40),
        _cloud(release_at=CLOCK + 25),
        _cloud(seen=(WALL - 1000, 1200)),
    ]
    assert cloud_arm(False, clouds, CLOCK, WALL) == Arm("cloud", None, 25.0)
    assert cloud_arm(False, clouds[2:], CLOCK, WALL) == Arm("cloud", None, 200.0)


@pytest.mark.parametrize(
    "cloud",
    [
        _cloud(pending=False, window_until=CLOCK + 40),
        _cloud(release_at=CLOCK - 1),
        _cloud(unresponsive=True, seen=(WALL, 100)),
        _cloud(seen=(WALL, None)),
        _cloud(seen=(WALL - 500, 100)),
    ],
    ids=["window_closed", "released", "already_out", "never_out", "past"],
)
def test_a_deadline_that_is_not_ahead_counts_for_nothing(cloud) -> None:
    assert cloud_arm(False, [cloud], CLOCK, WALL) == Arm("cloud", None, None)


def test_no_cloud_wakeup_once_retired_or_with_no_cloud_head() -> None:
    clouds = [_cloud(release_at=CLOCK + 25)]
    assert cloud_arm(True, clouds, CLOCK, WALL).delay is None
    assert cloud_arm(False, [], CLOCK, WALL).delay is None
    assert _delay(_w(clouds=clouds), "cloud") == 25.0


# -- openings and the hold's end ------------------------------------------------------------


def test_each_running_door_window_delay_has_its_wakeup_and_one_already_over_runs_at_once() -> None:
    w = _w(openings=[("study", WALL + 30), ("den", None), ("hall", WALL - 3)])
    assert _delay(w, "opening", "study") == 30.0
    assert _delay(w, "opening", "hall") == 0.0
    assert ("opening", "den") not in _by_name(w)


def test_a_retired_coordinator_only_cancels_the_door_window_wakeups() -> None:
    out = arms(_w(retired=True, openings=[("study", WALL + 30)]))
    assert out[-1] == CancelAll("opening")


def test_the_hold_end_wakeup() -> None:
    assert hold_end_arm(600.0, retired=False) == Arm("hold_end", None, 601.0)
    assert hold_end_arm(0.0, retired=False) == Arm("hold_end", None, None)
    assert hold_end_arm(600.0, retired=True) == Arm("hold_end", None, None)


# -- the simulator takes them as they are ---------------------------------------------------


def test_the_simulator_cancels_every_wakeup_of_a_name_for_cancel_all() -> None:
    timers, _ = sim_timing.start(0.0)
    timers, _ = sim_timing.step(timers, sim_timing.Computed(sent=False), 0.0)
    for slug in ("study", "den"):
        timers, _ = sim_timing.step(timers, Arm("opening", slug, 30.0), 0.0)
    timers, _ = sim_timing.step(timers, Arm("dwell", None, 50.0), 0.0)
    timers, _ = sim_timing.step(timers, sim_timing.Ask(Refresh.NOW), 1.0)
    timers, _ = sim_timing.step(
        timers, sim_timing.Computed(sent=False, armed=arms(_w(openings=[("hall", WALL + 9)]))), 1.0,
    )
    assert {name for name, _ in timers.waits} == {"startup", "opening"}
    assert set(timers.waits) == {("startup", None), ("opening", "hall")}
