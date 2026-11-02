"""Freeze floor / overheat ceiling: the pure decision math.

Hand-written oracles throughout: every expected value below is worked out from
the approved design (docs and DESIGN decisions 1-11), not read back from the
code under test. Behaviour through a config entry is in ``test_floor.py``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import pytest

from custom_components.mxz_coordinator.const import FLOOR_STALL_SECONDS
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from custom_components.mxz_coordinator.logic import (
    PRIORITY_DEMAND,
    PRIORITY_FLOOR_HEAT,
    PRIORITY_SAFETY,
    PRIORITY_VOTE,
    Demand,
    FloorRun,
    arbitrate,
    floor_call,
    floor_progress,
    shared_mode,
)

HOUR = 3600.0
pair = MXZCoordinator._zone_arbitration_pair


def call(temp, prior=None, ok=True, low=50.0, high=90.0, margin=3.0):
    return floor_call(temp=temp, sensor_ok=ok, low=low, high=high, prior=prior, margin=margin)


# ---------------------------------------------------------------------------
# floor_call: trip, hold, clear, re-trip (hysteresis)
# ---------------------------------------------------------------------------
def test_freeze_floor_trips_below_the_line_and_runs_to_the_clear_line() -> None:
    """Decision 2 (freeze floor): below 50 trips heat; it holds through 52.9
    and clears exactly at 53 (floor + 3 °F margin); untripped, 50 itself and
    52 do nothing, and only a reading below 50 trips again."""
    assert call(50.0) is None
    assert call(49.9) == "heat"
    assert [call(t, prior="heat") for t in (45.0, 50.0, 52.9)] == ["heat"] * 3
    assert call(53.0, prior="heat") is None
    assert [call(t) for t in (52.0, 50.0)] == [None, None]
    assert call(49.9) == "heat"


def test_overheat_ceiling_mirrors_the_floor() -> None:
    """Decision 2 (overheat ceiling): above 90 trips cool, holds to 87, clears at 87."""
    assert call(90.0) is None
    assert call(90.1) == "cool"
    assert [call(t, prior="cool") for t in (95.0, 90.0, 87.1)] == ["cool"] * 3
    assert call(87.0, prior="cool") is None
    assert call(88.0) is None


def test_celsius_floor_uses_the_metric_margin() -> None:
    """°C install: a 10 °C floor with the 1.5 °C margin clears at 11.5."""
    c = {"low": 10.0, "high": 32.0, "margin": 1.5}
    assert call(9.9, **c) == "heat"
    assert call(11.4, prior="heat", **c) == "heat"
    assert call(11.5, prior="heat", **c) is None
    assert call(32.5, **c) == "cool"
    assert call(30.5, prior="cool", **c) is None


@pytest.mark.parametrize("prior", [None, "heat", "cool"])
@pytest.mark.parametrize("temp", [-40.0, 20.0, 49.0, 70.0, 95.0, 130.0])
def test_no_usable_reading_never_wants_anything(prior, temp) -> None:
    """Decision 4 / fail safe: sensor_ok False gives None from every prior,
    whatever the (fallback) number says."""
    assert call(temp, prior=prior, ok=False) is None


def test_only_the_configured_side_acts() -> None:
    """Decision 2: a floor-only room never cools; a ceiling-only room never heats."""
    assert call(95.0, high=None) is None
    assert call(40.0, low=None) is None
    assert call(40.0, high=None) == "heat"
    assert call(95.0, low=None) == "cool"


# ---------------------------------------------------------------------------
# floor_progress: the no-progress cap (decision 11)
# ---------------------------------------------------------------------------
def run_for(direction="heat", temp=45.0):
    return FloorRun(direction=direction, mark=temp, worst=temp)


def step(run, temp, driven_for, step_=1.0):
    return floor_progress(run, temp=temp, driven_for=driven_for, step=step_, cap=FLOOR_STALL_SECONDS)


def test_cap_fires_at_two_driven_hours_not_a_tick_before() -> None:
    """Decision 11: a frozen reading stalls at exactly 2 h of driving."""
    assert FLOOR_STALL_SECONDS == 2 * HOUR
    run = step(run_for(), 45.0, 2 * HOUR - 1)
    assert (run.stalled, run.driven) == (False, 2 * HOUR - 1)
    run = step(run, 45.0, 1)
    assert run.stalled is True


def test_waiting_charges_nothing() -> None:
    """Decision 11: a standoff loser or a dwell wait (driven_for 0) never stalls,
    however long it waits."""
    run = run_for()
    for _ in range(100):
        run = step(run, 45.0, 0.0)
    assert (run.stalled, run.driven) == (False, 0.0)


def test_one_step_gain_then_frozen_stalls_two_hours_after_the_gain() -> None:
    """Decision 11: progress is against a high-water mark. A 1 °F gain at 1 h
    restarts the clock; frozen after it, the run stalls 2 h after the gain."""
    run = step(run_for(), 45.5, HOUR)  # half a step: no credit
    assert (run.mark, run.driven) == (45.0, HOUR)
    run = step(run, 46.0, 0.5 * HOUR)  # one full step: mark moves, clock restarts
    assert (run.mark, run.driven) == (46.0, 0.0)
    run = step(run, 46.0, 2 * HOUR - 1)
    assert run.stalled is False
    run = step(run, 46.0, 1)
    assert run.stalled is True


def test_adjacent_value_flicker_neither_progresses_nor_rearms() -> None:
    """Decision 11: a 1 °F-resolution sensor flickering 44/45 gets one step of
    credit at most, stalls, and the flicker never re-arms the stalled run."""
    run = run_for(temp=44.0)
    run = step(run, 45.0, 600)  # one step up from the trip reading: credited once
    assert (run.mark, run.driven) == (45.0, 0.0)
    for temp in itertools.islice(itertools.cycle((44.0, 45.0)), 13):
        run = step(run, temp, 600)
    assert run.stalled is True
    for temp in itertools.islice(itertools.cycle((44.0, 45.0)), 50):
        run = step(run, temp, 600)
        assert run.stalled is True, temp


def test_a_new_low_rearms_a_stalled_run() -> None:
    """Decision 11: one step below the run's lowest reading re-arms it as a new
    run (mark there, clock at zero)."""
    run = step(step(run_for(temp=44.0), 44.0, 2 * HOUR), 44.0, 0)
    assert run.stalled is True
    assert step(run, 43.5, 0).stalled is True  # half a step lower: not new
    rearmed = step(run, 43.0, 0)
    assert rearmed == FloorRun(direction="heat", mark=43.0, worst=43.0)


@pytest.mark.parametrize(
    ("direction", "stall_at", "step_"),
    [("heat", 44.0, 1.0), ("cool", 95.0, 1.0), ("heat", 7.0, 0.5), ("cool", 30.0, 0.5)],
    ids=["floor_f", "ceiling_f", "floor_c", "ceiling_c"],
)
def test_a_gradual_slide_after_a_stall_rearms_at_one_full_step(direction, stall_at, step_) -> None:
    """Decision 11 (ii): the worst reading is frozen while stalled, so a room
    that keeps going the wrong way in tenths (colder for a floor, hotter for
    a ceiling) re-arms exactly one step past it: not a tenth sooner, and not
    never, which is what chasing each sub-step low would do."""
    away = -1.0 if direction == "heat" else 1.0
    run = step(run_for(direction, stall_at), stall_at, 2 * HOUR, step_)
    assert run.stalled is True
    for tenths in range(1, round(step_ * 10)):
        temp = round(stall_at + away * tenths / 10, 1)
        run = step(run, temp, 0, step_)
        assert (run.stalled, run.worst) == (True, stall_at), temp
    temp = round(stall_at + away * step_, 1)
    assert step(run, temp, 0, step_) == FloorRun(direction=direction, mark=temp, worst=temp)


@pytest.mark.parametrize(
    ("direction", "trip", "best", "step_"),
    [("heat", 40.0, 44.0, 1.0), ("cool", 95.0, 91.0, 1.0), ("heat", 5.0, 7.0, 0.5), ("cool", 36.0, 34.0, 0.5)],
    ids=["floor_f", "ceiling_f", "floor_c", "ceiling_c"],
)
def test_a_run_that_gained_then_stalled_rearms_one_step_past_the_stall_not_the_trip(
    direction, trip, best, step_
) -> None:
    """Decision 11 (ii), "one step below the lowest reading since the stall":
    a floor trips at 40 °F, heats to 44 °F a step at a time, then stalls at
    44. Sliding back half a step stays stalled; 43 °F re-arms it, although
    the trip reading (40) is far below. Mirrored for a ceiling and in °C."""
    toward = 1.0 if direction == "heat" else -1.0
    run = run_for(direction, trip)
    temp = trip
    while temp != best:
        temp = round(temp + toward * step_, 1)
        run = step(run, temp, 600, step_)
        assert (run.mark, run.driven) == (temp, 0.0)
    run = step(run, best, 2 * HOUR, step_)
    assert (run.stalled, run.worst) == (True, best)
    half = round(best - toward * step_ / 2, 2)
    assert step(run, half, 0, step_).stalled is True
    rearm = round(best - toward * step_, 1)
    assert step(run, rearm, 0, step_) == FloorRun(direction=direction, mark=rearm, worst=rearm)


P9_CASES = pytest.mark.parametrize(
    ("direction", "trip", "best", "dip", "back", "step_"),
    [
        ("heat", 40.0, 44.0, 41.0, 43.5, 1.0),
        ("cool", 95.0, 91.0, 94.0, 91.5, 1.0),
        ("heat", 5.0, 7.0, 5.5, 6.8, 0.5),
        ("cool", 36.0, 34.0, 35.5, 34.2, 0.5),
    ],
    ids=["floor_f", "ceiling_f", "floor_c", "ceiling_c"],
)


def _gain_to_best(direction, trip, best, step_):
    toward = 1.0 if direction == "heat" else -1.0
    run, temp = run_for(direction, trip), trip
    while temp != best:
        temp = round(temp + toward * step_, 1)
        run = step(run, temp, 600, step_)
        assert (run.mark, run.driven) == (temp, 0.0)
    return run, toward


@P9_CASES
def test_p9_a_dip_more_than_an_hour_of_driving_before_the_stall_does_not_count(
    direction, trip, best, dip, back, step_
) -> None:
    """P9, review 2's case: trip 40 °F, heated to 44 a step at a time; a
    door opens (41) half an hour of driving after the last gain, the room
    comes back to 43.5 and stalls there 2 h after the gain. The dip was 1.5 h
    of driving before the stall, outside the last hour, so the re-arm line is
    one step past 43.5: 43 stays stalled, 42.5 re-arms (the old since-gain
    rule waited for 40). Mirrored for a ceiling and in °C."""
    run, toward = _gain_to_best(direction, trip, best, step_)
    run = step(run, dip, 1800, step_)
    run = step(run, back, 600, step_)
    for _ in range(7):
        run = step(run, back, 600, step_)
    assert (run.stalled, run.driven) == (False, 6600)
    run = step(run, back, 600, step_)
    assert (run.stalled, run.worst) == (True, back)
    half = round(back - toward * step_ / 2, 2)
    assert step(run, half, 0, step_).stalled is True
    rearm = round(back - toward * step_, 1)
    assert step(run, rearm, 0, step_) == FloorRun(direction=direction, mark=rearm, worst=rearm)


@P9_CASES
def test_p9_a_dip_in_the_last_hour_of_driving_before_the_stall_still_counts(
    direction, trip, best, dip, back, step_
) -> None:
    """P9: the same run, but the door opens 1 h 10 min of driving after the
    gain, inside the last hour before the stall. The dip is the window's
    worst: one step past the stall reading stays stalled, half a step past
    the dip too, and one full step past the dip re-arms."""
    run, toward = _gain_to_best(direction, trip, best, step_)
    run = step(run, back, 600, step_)
    for _ in range(5):
        run = step(run, back, 600, step_)
    run = step(run, dip, 600, step_)  # 70 min after the gain
    run = step(run, back, 600, step_)
    for _ in range(3):
        run = step(run, back, 600, step_)
    run = step(run, back, 600, step_)
    assert (run.stalled, run.driven, run.worst) == (True, 7200, dip)
    assert step(run, round(back - toward * step_, 1), 0, step_).stalled is True
    assert step(run, round(dip - toward * step_ / 2, 2), 0, step_).stalled is True
    rearm = round(dip - toward * step_, 1)
    assert step(run, rearm, 0, step_) == FloorRun(direction=direction, mark=rearm, worst=rearm)


def test_p9_the_window_is_the_last_hour_of_driving_to_the_second() -> None:
    """P9 boundary: a dip exactly one hour of driving before the stall is in
    the window; one second earlier it is not. Waiting (no driving) does not
    move the window: a dip during a 3 h wait, an hour of driving before the
    stall, still counts."""
    def stall_after_dip_at(at: float, wait: float = 0.0) -> FloorRun:
        run = step(run_for(temp=44.0), 43.5, at)
        run = step(run, 41.0, 0)
        for _ in range(int(wait // 600)):
            run = step(run, 41.0, 0)
        run = step(run, 43.5, 0)
        return step(run, 43.5, 2 * HOUR - at)

    assert stall_after_dip_at(HOUR).worst == 41.0
    assert stall_after_dip_at(HOUR - 1).worst == 43.5
    assert stall_after_dip_at(HOUR, wait=3 * HOUR).worst == 41.0
    assert stall_after_dip_at(HOUR - 1, wait=3 * HOUR).worst == 43.5


def test_ceiling_cap_mirrors_with_a_new_high() -> None:
    """Decision 11 (ceiling): progress is falling; a stalled ceiling re-arms
    only on a new high one step above its highest reading since the stall
    window began (the last gain), not since the trip."""
    run = run_for("cool", 95.0)
    run = step(run, 94.0, HOUR)
    assert (run.mark, run.driven) == (94.0, 0.0)
    run = step(run, 94.5, 2 * HOUR)
    assert run.stalled is True and run.worst == 94.5
    assert step(run, 95.0, 0).stalled is True
    assert step(run, 95.5, 0) == FloorRun(direction="cool", mark=95.5, worst=95.5)


def test_celsius_step_is_half_a_degree() -> None:
    """Decision 11: 0.5 °C of new best reading is progress on a °C install."""
    run = floor_progress(
        FloorRun("heat", 7.0, 7.0, driven=HOUR), temp=7.5, driven_for=60, step=0.5,
        cap=FLOOR_STALL_SECONDS,
    )
    assert (run.mark, run.driven) == (7.5, 0.0)


# ---------------------------------------------------------------------------
# Arbitration: where a tripped floor ranks (decision 6, freeze first)
# ---------------------------------------------------------------------------
def decide(zones, *, current="cool", allowed=True, engaged=None):
    """zones: (demand, vote, safety, floor) per room in priority order."""
    demands = []
    for i, (demand, vote, safety, floor) in enumerate(zones):
        demands.extend(pair(demand=demand, vote=vote, safety=safety, index=i, floor=floor))
    if engaged is None:
        engaged = any(floor for *_, floor in zones)
    grant = arbitrate(demands, engaged=engaged, current=current, allowed=allowed)
    return grant.direction, grant.read_only, grant.by_safety


def test_floor_demand_tiers() -> None:
    """A freeze floor is commanding at PRIORITY_FLOOR_HEAT, a ceiling at
    PRIORITY_SAFETY, both with the room's index; no floor adds nothing."""
    assert PRIORITY_FLOOR_HEAT < PRIORITY_SAFETY < PRIORITY_DEMAND < PRIORITY_VOTE
    assert pair(demand="off", vote=None, safety=False, index=2, floor="heat")[2] == Demand(
        "heat", (PRIORITY_FLOOR_HEAT, 2), read_only=False
    )
    assert pair(demand="off", vote=None, safety=False, index=1, floor="cool")[2] == Demand(
        "cool", (PRIORITY_SAFETY, 1), read_only=False
    )
    assert len(pair(demand="off", vote=None, safety=True, index=0, floor=None)) == 2


def test_freeze_in_a_later_room_beats_a_ceiling_in_an_earlier_room() -> None:
    """Decision 6 (changed by the maintainer): freeze first, regardless of room order."""
    zones = [("off", None, False, "cool"), ("off", None, False, None), ("off", None, False, "heat")]
    assert decide(zones, current="cool")[0] == "heat"
    assert decide(zones, current="heat")[0] == "heat"
    # Reversed order: still heat.
    zones = [("off", None, False, "heat"), ("off", None, False, "cool")]
    assert decide(zones, current="cool")[0] == "heat"


def test_same_kind_trips_rank_by_room_order() -> None:
    """Decision 6: room order breaks ties between two trips of the same kind
    (the earlier room's Demand sorts first); they agree, so both are served."""
    a = pair(demand="off", vote=None, safety=False, index=0, floor="cool")[2]
    b = pair(demand="off", vote=None, safety=False, index=1, floor="cool")[2]
    assert sorted([b, a], key=lambda d: d.priority) == [a, b]
    assert decide([("off", None, False, "cool"), ("off", None, False, "cool")], current="heat")[0] == "cool"
    assert decide([("off", None, False, "heat"), ("off", None, False, "heat")])[0] == "heat"


@pytest.mark.parametrize("kind", ["demand", "vote"])
def test_freeze_floor_beats_a_safety_room_wanting_cool_in_any_order(kind) -> None:
    """Ranking vs #31: a tripped freeze floor outranks a #31 safety want (demand
    or hands-off vote) for cool, whichever room comes first."""
    safety_room = ("cool", None, True, None) if kind == "demand" else ("off", "cool", True, None)
    floor_room = ("off", None, False, "heat")
    assert decide([safety_room, floor_room])[0] == "heat"
    assert decide([floor_room, safety_room])[0] == "heat"


@pytest.mark.parametrize("kind", ["demand", "vote"])
def test_ceiling_and_a_safety_room_settle_by_room_order(kind) -> None:
    """Ranking vs #31: a tripped ceiling shares #31's tier, so against a safety
    want for heat the earlier room wins (the design's rule)."""
    safety_room = ("heat", None, True, None) if kind == "demand" else ("off", "heat", True, None)
    ceiling_room = ("off", None, False, "cool")
    assert decide([safety_room, ceiling_room], current="cool")[0] == "heat"
    assert decide([ceiling_room, safety_room], current="heat")[0] == "cool"


def test_floor_outranks_ordinary_demands_and_silences_votes() -> None:
    """A floor beats an enabled room's comfort demand in an earlier room, and an
    ordinary hands-off vote is silenced while a floor commands."""
    assert decide([("cool", None, False, None), ("off", None, False, "heat")])[0] == "heat"
    assert decide([("heat", None, False, None), ("off", None, False, "cool")], current="heat")[0] == "cool"
    assert decide([("off", "cool", False, None), ("off", None, False, "heat")])[0] == "heat"


def test_a_floor_flip_is_dwelled_and_stamped() -> None:
    """Decision 6 / dwell: the wait holds a floor's flip back; once allowed the
    flip is commanding (read_only False, so it is stamped) and by_safety."""
    zones = [("cool", None, False, None), ("off", None, False, "heat")]
    assert decide(zones, current="cool", allowed=False) == ("cool", False, False)
    assert decide(zones, current="cool", allowed=True) == ("heat", False, True)


# ---------------------------------------------------------------------------
# No floor: the arbitration is dcf3bc7's (cheap two-room sweep; the full 2- and
# 3-room sweep against the real dcf3bc7 files lives outside the suite)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _OldDemand:
    direction: str | None
    priority: tuple[int, int]
    read_only: bool = False


def _old_pair(*, demand, vote, safety, index):
    """dcf3bc7's ``MXZCoordinator._zone_arbitration_pair``, copied verbatim."""
    tier = PRIORITY_SAFETY if safety else None
    return (
        _OldDemand(
            direction=demand if demand in ("cool", "heat") else None,
            priority=(tier if tier is not None else PRIORITY_DEMAND, index),
        ),
        _OldDemand(
            direction=vote,
            priority=(tier if tier is not None else PRIORITY_VOTE, index),
            read_only=True,
        ),
    )


def _old_arbitrate(demands, *, engaged, current, allowed, resting=None):
    """dcf3bc7's ``logic.arbitrate``, copied verbatim (docstring dropped)."""
    ranked = sorted(demands, key=lambda d: d.priority)
    commanding = [d for d in ranked if not d.read_only]
    safety_votes = [
        d for d in ranked if d.read_only and d.priority[0] == PRIORITY_SAFETY
    ]
    ordinary_votes = [
        d for d in ranked if d.read_only and d.priority[0] != PRIORITY_SAFETY
    ]
    commanding_silent = not any(d.direction for d in commanding)
    gate_open = commanding_silent and not engaged
    active = sorted(
        commanding + safety_votes + (ordinary_votes if gate_open else []),
        key=lambda d: d.priority,
    )
    full = shared_mode(
        demands=[d.direction for d in active if d.direction],
        current=current, allowed=allowed, resting=resting,
    )
    by_safety = any(
        d.direction == full and d.priority[0] == PRIORITY_SAFETY for d in active
    )
    if not any(d.read_only and d.direction for d in active):
        return (full, False, by_safety)
    plain = shared_mode(
        demands=[d.direction for d in active if d.direction and not d.read_only],
        current=current, allowed=allowed, resting=resting,
    )
    read_only = full != plain and gate_open
    return (full, read_only, by_safety)


def test_no_floor_two_room_arbitration_is_dcf3bc7() -> None:
    """Every two-room input with every #31 safety tag combination: with no
    floor configured, the arbitration (mode, stamp, by_safety) is dcf3bc7's."""
    cases = 0
    # Engages reach arbitrate() only as the one ``engaged`` bool.
    for demands, engaged, votes, safety, current, allowed, resting in itertools.product(
        itertools.product(("cool", "heat", "neutral", "off"), repeat=2),
        (False, True),
        itertools.product(("cool", "heat", None), repeat=2),
        itertools.product((False, True), repeat=2),
        ("cool", "heat"),
        (True, False),
        (None, "cool", "heat"),
    ):
        old, new = [], []
        for i in range(2):
            old.extend(_old_pair(demand=demands[i], vote=votes[i], safety=safety[i], index=i))
            new.extend(pair(demand=demands[i], vote=votes[i], safety=safety[i], index=i, floor=None))
        expected = _old_arbitrate(old, engaged=engaged, current=current, allowed=allowed, resting=resting)
        grant = arbitrate(new, engaged=engaged, current=current, allowed=allowed, resting=resting)
        assert (grant.direction, grant.read_only, grant.by_safety) == expected
        cases += 1
    assert cases == 4**2 * 2 * 3**2 * 2**2 * 2 * 2 * 3
