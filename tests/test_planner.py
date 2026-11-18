"""planner.py's decisions, called directly: no Home Assistant, no pytest-homeassistant.

Each expectation is worked out from the rule it states (a room calls past its target
by the demand band, runs to its target once engaged, the first calling room wins a
standoff, and so on), not read back from the code. Every room and value is invented.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.mxz_coordinator.const import (
    DEMAND_NEUTRAL,
    ENGAGE_SATISFIED,
    MODE_COOL,
    MODE_HEAT,
    MODE_OFF,
    VOTE_EXPIRED,
)
from custom_components.mxz_coordinator.logic import (
    PRIORITY_DEMAND,
    PRIORITY_FLOOR_HEAT,
    PRIORITY_SAFETY,
    PRIORITY_VOTE,
)
from custom_components.mxz_coordinator.planner import (
    CallLimits,
    RoomInput,
    Shared,
    arbitration_entries,
    arbitration_mode,
    decide,
    demand_bands,
    holds,
    mode_change_stamp,
    mode_write_back,
)

LIMITS = CallLimits(
    eco=False, eco_cool_max=85.0, eco_heat_min=55.0,
    heat_lockout=False, heat_lockout_floor=60.0,
    cool_lockout=False, cool_lockout_ceiling=80.0,
)
# Calls past 2 °F off target; engages past 1 °F; runs to the target once engaged.
SHARED = Shared(
    limits=LIMITS, demand_threshold=2.0, mode=MODE_COOL, elapsed=None, hold=600.0,
    resting=None, by_votes=False, resume=True, accept_head_changes=True,
)


def room(slug: str = "a", temp: float = 70.0, **changes) -> RoomInput:
    base = RoomInput(
        slug=slug, enabled=True, temp=temp, sensor_ok=True, target=70.0,
        heat_target=70.0, cool_target=70.0, heat_drift=1.0, cool_drift=1.0,
        head=MODE_OFF, paused=False, dry=False, floor=None, safety=False,
        vote=None, holds=True, boost=0.0,
    )
    return replace(base, **changes)


def calls(decision) -> list[tuple[str, str]]:
    return [(r.demand, r.engage) for r in decision.rooms]


# --- demand and engage ------------------------------------------------------

@pytest.mark.parametrize(
    ("temp", "want"),
    [
        (70.0, (DEMAND_NEUTRAL, ENGAGE_SATISFIED)),
        (71.5, (DEMAND_NEUTRAL, MODE_COOL)),  # past the drift, inside the demand band
        (72.5, (MODE_COOL, MODE_COOL)),
        (68.5, (DEMAND_NEUTRAL, MODE_HEAT)),
        (67.5, (MODE_HEAT, MODE_HEAT)),
    ],
)
def test_a_room_calls_past_the_demand_band_and_engages_past_its_drift(temp, want) -> None:
    decision = decide([room(temp=temp)], SHARED, {"a": ""}, {})
    assert calls(decision) == [want]


def test_a_wide_drift_widens_the_demand_band_on_its_side_only() -> None:
    assert demand_bands(2.0, 1.0, 1.0) == (2.0, 2.0)
    assert demand_bands(2.0, 3.0, 1.0) == (3.0, 2.0)
    assert demand_bands(2.0, 1.0, 4.5) == (2.0, 4.5)
    decision = decide([room(temp=67.5, heat_drift=3.0)], SHARED, {"a": ""}, {})
    assert calls(decision) == [(DEMAND_NEUTRAL, ENGAGE_SATISFIED)]


def test_each_side_measures_from_its_own_target() -> None:
    dual = room(heat_target=68.0, cool_target=72.0, target=70.0)
    assert calls(decide([replace(dual, temp=73.5)], SHARED, {"a": ""}, {})) == [
        (DEMAND_NEUTRAL, MODE_COOL)
    ]
    assert calls(decide([replace(dual, temp=71.5)], SHARED, {"a": ""}, {})) == [
        (DEMAND_NEUTRAL, ENGAGE_SATISFIED)
    ]


def test_an_engaged_room_runs_to_its_target_then_coasts() -> None:
    running = decide([room(temp=70.5)], SHARED, {"a": MODE_COOL}, {})
    assert calls(running) == [(DEMAND_NEUTRAL, MODE_COOL)]
    assert (running.latch, running.coasted) == ({"a": MODE_COOL}, False)
    reached = decide([room(temp=70.0)], SHARED, {"a": MODE_COOL}, {})
    assert calls(reached) == [(DEMAND_NEUTRAL, ENGAGE_SATISFIED)]
    assert (reached.latch, reached.coasted) == ({"a": ""}, True)


def test_a_disabled_room_is_off_and_not_coasting() -> None:
    decision = decide([room(temp=80.0, enabled=False)], SHARED, {"a": MODE_COOL}, {})
    assert calls(decision) == [(MODE_OFF, MODE_OFF)]
    assert decision.coasted is False


def test_a_bad_reading_calls_nothing() -> None:
    decision = decide([room(temp=90.0, sensor_ok=False)], SHARED, {"a": ""}, {})
    assert calls(decision) == [(DEMAND_NEUTRAL, ENGAGE_SATISFIED)]


def test_a_heat_lockout_holds_heat_off_above_its_floor() -> None:
    locked = replace(SHARED, limits=replace(LIMITS, heat_lockout=True))
    assert calls(decide([room(temp=64.0)], locked, {"a": ""}, {})) == [
        (DEMAND_NEUTRAL, ENGAGE_SATISFIED)
    ]
    assert calls(decide([room(temp=59.0)], locked, {"a": ""}, {})) == [(MODE_HEAT, MODE_HEAT)]


def test_eco_uses_the_eco_extremes() -> None:
    eco = replace(SHARED, limits=replace(LIMITS, eco=True))
    assert calls(decide([room(temp=80.0)], eco, {"a": ""}, {})) == [
        (DEMAND_NEUTRAL, ENGAGE_SATISFIED)
    ]
    assert calls(decide([room(temp=86.0)], eco, {"a": ""}, {})) == [(MODE_COOL, MODE_COOL)]


# --- overrides --------------------------------------------------------------

def test_a_paused_room_calls_nothing_and_drops_its_run() -> None:
    decision = decide(
        [room("a", temp=60.0, paused=True), room("b", temp=72.5)],
        replace(SHARED, mode=MODE_HEAT),
        {"a": MODE_HEAT, "b": ""},
        {},
    )
    assert calls(decision) == [(DEMAND_NEUTRAL, ENGAGE_SATISFIED), (MODE_COOL, MODE_COOL)]
    assert decision.latch == {"a": "", "b": MODE_COOL}
    assert decision.state == MODE_COOL
    assert decision.coasted is False  # a pause is no coast


def test_a_dry_room_votes_cool_unless_cool_is_locked_out() -> None:
    dry = decide([room(temp=67.0, dry=True)], SHARED, {"a": MODE_HEAT}, {})
    assert calls(dry) == [(DEMAND_NEUTRAL, ENGAGE_SATISFIED)]
    assert (dry.rooms[0].dry_vote, dry.latch) == (MODE_COOL, {"a": ""})
    locked = replace(SHARED, limits=replace(LIMITS, cool_lockout=True))
    assert decide([room(dry=True)], locked, {"a": ""}, {}).rooms[0].dry_vote is None


def test_a_paused_room_is_never_in_dry() -> None:
    decision = decide([room(dry=True, paused=True)], SHARED, {"a": ""}, {})
    assert decision.rooms[0].dry_vote is None


def test_a_floor_is_the_disabled_rooms_engage_and_can_win_the_mode() -> None:
    decision = decide(
        [room("a", temp=72.5), room("b", temp=45.0, enabled=False, floor=MODE_HEAT)],
        SHARED, {"a": ""}, {},
    )
    assert calls(decision) == [(MODE_COOL, MODE_COOL), (MODE_OFF, MODE_HEAT)]
    assert (decision.state, decision.by_floor, decision.standoff) == (MODE_HEAT, True, True)
    assert decision.latch == {"a": MODE_COOL}  # the floor leaves the latch alone


def test_a_room_that_does_not_hold_counts_for_nothing_but_its_floor() -> None:
    lost = decide([room(temp=67.5, holds=False)], SHARED, {"a": ""}, {})
    assert calls(lost) == [(MODE_HEAT, MODE_HEAT)]
    assert (lost.rooms[0].held_demand, lost.rooms[0].held_engage) == (DEMAND_NEUTRAL, ENGAGE_SATISFIED)
    assert lost.state == MODE_COOL
    floor = decide([room(enabled=False, holds=False, floor=MODE_HEAT)], SHARED, {}, {})
    assert floor.rooms[0].held_engage == MODE_HEAT


def test_holds() -> None:
    assert holds("live", False) is True
    assert holds("stale", False) is True
    assert holds(VOTE_EXPIRED, False) is False
    assert holds(VOTE_EXPIRED, True) is True
    assert holds("live", False, cloud_out=True) is False
    assert holds("live", True, cloud_out=True) is True


# --- the latch's seeds --------------------------------------------------------

def test_a_start_resumes_the_run_its_head_shows() -> None:
    decision = decide([room(temp=70.5, head=MODE_COOL)], SHARED, {"a": ""}, {"a": None})
    assert (decision.latch, decision.startup) == ({"a": MODE_COOL}, {})
    assert calls(decision) == [(DEMAND_NEUTRAL, MODE_COOL)]


def test_a_start_waits_for_a_head_that_has_not_reported() -> None:
    for head in (None, "unavailable", "unknown"):
        decision = decide([room(temp=70.5, head=head)], SHARED, {"a": ""}, {"a": None})
        assert (decision.latch, decision.startup) == ({"a": ""}, {"a": None}), head


def test_a_start_seen_idle_seeds_nothing_and_a_bad_reading_keeps_the_seed() -> None:
    idle = decide([room(temp=70.5, head="fan_only")], SHARED, {"a": ""}, {"a": None})
    assert (idle.latch, idle.startup) == ({"a": ""}, {})
    blind = decide(
        [room(temp=70.5, head=MODE_HEAT, sensor_ok=False)], SHARED, {"a": ""}, {"a": None}
    )
    assert (blind.latch, blind.startup) == ({"a": ""}, {"a": MODE_HEAT})


def test_a_start_with_the_coordinator_off_drops_the_seed() -> None:
    decision = decide(
        [room(temp=70.5, head=MODE_COOL)], replace(SHARED, resume=False), {"a": ""}, {"a": None}
    )
    assert (decision.latch, decision.startup) == ({"a": ""}, {})


def test_a_retargeted_room_takes_its_heads_run_unless_a_person_flipped_it() -> None:
    own = decide([room(temp=70.5, head=MODE_COOL)], SHARED, {}, {})
    assert own.latch == {"a": MODE_COOL}
    theirs = decide([room(temp=69.5, head=MODE_HEAT)], SHARED, {}, {})
    assert theirs.latch == {"a": ""}
    not_accepted = replace(SHARED, accept_head_changes=False)
    assert decide([room(temp=69.5, head=MODE_HEAT)], not_accepted, {}, {}).latch == {"a": MODE_HEAT}


def test_decide_leaves_its_inputs_alone() -> None:
    latch, startup = {"a": MODE_COOL}, {"b": None}
    decide([room("a", temp=70.0), room("b", head=MODE_HEAT)], SHARED, latch, startup)
    assert (latch, startup) == ({"a": MODE_COOL}, {"b": None})


# --- the shared mode ------------------------------------------------------------

def test_the_first_calling_room_wins_a_standoff() -> None:
    rooms = [room("a", temp=72.5), room("b", temp=67.5)]
    for mode in (MODE_COOL, MODE_HEAT):
        decision = decide(rooms, replace(SHARED, mode=mode), {}, {})
        assert (decision.state, decision.standoff) == (MODE_COOL, True)


def test_everyone_satisfied_keeps_the_mode_or_rests() -> None:
    rooms = [room("a"), room("b")]
    kept = decide(rooms, replace(SHARED, mode=MODE_HEAT), {}, {})
    assert (kept.state, kept.standoff, kept.by_votes) == (MODE_HEAT, False, False)
    rests = decide(rooms, replace(SHARED, mode=MODE_HEAT, resting=MODE_COOL), {}, {})
    assert rests.state == MODE_COOL


def test_a_run_in_the_shared_mode_keeps_it_from_resting() -> None:
    decision = decide(
        [room(temp=70.5)], replace(SHARED, resting=MODE_HEAT), {"a": MODE_COOL}, {}
    )
    assert decision.state == MODE_COOL


def test_the_hold_keeps_the_mode_until_it_has_run() -> None:
    rooms = [room(temp=67.5)]
    held = decide(rooms, replace(SHARED, elapsed=599.0), {}, {})
    assert (held.state, held.allowed) == (MODE_COOL, False)
    free = decide(rooms, replace(SHARED, elapsed=600.0), {}, {})
    assert (free.state, free.allowed) == (MODE_HEAT, True)
    never = decide(rooms, replace(SHARED, elapsed=None), {}, {})
    assert (never.state, never.allowed) == (MODE_HEAT, True)


def test_comfort_priority_wins_a_standoff_and_says_so() -> None:
    rooms = [room("a", temp=72.5), room("b", temp=67.5, boost=1.0)]
    decision = decide(rooms, SHARED, {}, {})
    assert (decision.state, decision.by_comfort) == (MODE_HEAT, True)
    no_boost = decide([rooms[0], replace(rooms[1], boost=0.0)], SHARED, {}, {})
    assert (no_boost.state, no_boost.by_comfort) == (MODE_COOL, False)
    # A boost that changes nothing earns nothing.
    alone = decide([room("a", temp=72.5, boost=1.0)], SHARED, {}, {})
    assert (alone.state, alone.by_comfort) == (MODE_COOL, False)


def test_safety_priority_outranks_room_order() -> None:
    decision = decide([room("a", temp=72.5), room("b", temp=67.5, safety=True)], SHARED, {}, {})
    assert (decision.state, decision.by_safety) == (MODE_HEAT, True)


def test_a_hands_off_vote_sets_an_idle_mode_by_votes() -> None:
    decision = decide([room("a"), room("b", enabled=False, vote=MODE_HEAT)], SHARED, {}, {})
    assert (decision.state, decision.by_votes) == (MODE_HEAT, True)
    calling = decide([room("a", temp=72.5), room("b", enabled=False, vote=MODE_HEAT)], SHARED, {}, {})
    assert (calling.state, calling.by_votes, calling.standoff) == (MODE_COOL, False, True)


def test_the_way_back_from_a_voted_mode_is_the_votes_too() -> None:
    voted = replace(SHARED, mode=MODE_HEAT, resting=MODE_COOL, by_votes=True)
    back = decide([room("a")], voted, {}, {})
    assert (back.state, back.by_votes) == (MODE_COOL, True)
    owned = decide([room("a")], replace(voted, by_votes=False), {}, {})
    assert (owned.state, owned.by_votes) == (MODE_COOL, False)
    called = decide([room("a", temp=72.5)], voted, {}, {})
    assert (called.state, called.by_votes) == (MODE_COOL, False)


def test_a_junk_mode_arbitrates_from_cool() -> None:
    assert arbitration_mode(MODE_HEAT) == MODE_HEAT
    assert arbitration_mode(MODE_COOL) == MODE_COOL
    for junk in ("fan_only", "", "off"):
        assert arbitration_mode(junk) == MODE_COOL
    assert decide([room("a")], replace(SHARED, mode="fan_only"), {}, {}).state == MODE_COOL


def test_arbitration_entries_rank_each_kind() -> None:
    plain = arbitration_entries(demand=MODE_COOL, vote=MODE_HEAT, safety=False, index=2)
    assert [(e.direction, e.priority, e.read_only) for e in plain] == [
        (MODE_COOL, (PRIORITY_DEMAND, 2), False),
        (MODE_HEAT, (PRIORITY_VOTE, 2), True),
    ]
    safe = arbitration_entries(
        demand=DEMAND_NEUTRAL, vote=None, safety=True, index=0, floor=MODE_HEAT,
        dry=MODE_COOL, boost=1.0,
    )
    assert [(e.direction, e.priority, e.read_only, e.boost) for e in safe] == [
        (None, (PRIORITY_SAFETY, 0), False, 1.0),
        (None, (PRIORITY_SAFETY, 0), True, 1.0),
        (MODE_COOL, (PRIORITY_VOTE, 0), True, 1.0),
        (MODE_HEAT, (PRIORITY_FLOOR_HEAT, 0), False, 0.0),
    ]
    ceiling = arbitration_entries(demand=DEMAND_NEUTRAL, vote=None, safety=False, index=1, floor=MODE_COOL)
    assert (ceiling[-1].direction, ceiling[-1].priority) == (MODE_COOL, (PRIORITY_SAFETY, 1))


# --- the mode's write-back --------------------------------------------------------

def _write_back(mode, by_votes, state, by_hands_off=False, calls=(), resting=None):
    step = mode_write_back(
        mode, by_votes, state=state, by_hands_off=by_hands_off, calls=calls, resting=resting
    )
    return step.mode, step.by_votes, step.changed, step.stamp, step.record


def test_a_change_the_rooms_made_is_stamped_and_recorded() -> None:
    assert _write_back(MODE_COOL, False, MODE_HEAT) == (MODE_HEAT, False, True, True, True)
    assert _write_back(MODE_HEAT, True, MODE_COOL, calls=(MODE_COOL,)) == (
        MODE_COOL, False, True, True, True
    )


def test_a_change_votes_made_is_recorded_but_starts_no_hold() -> None:
    assert _write_back(MODE_COOL, False, MODE_HEAT, by_hands_off=True) == (
        MODE_HEAT, True, True, False, True
    )
    # Back to the resting mode the votes' way: no hold, and it owes the votes nothing.
    assert _write_back(MODE_HEAT, True, MODE_COOL, by_hands_off=True, resting=MODE_COOL) == (
        MODE_COOL, False, True, False, True
    )


def test_a_room_running_in_the_voted_mode_makes_it_mxzs() -> None:
    for calls in ((MODE_HEAT,), (ENGAGE_SATISFIED, MODE_HEAT, DEMAND_NEUTRAL)):
        assert _write_back(MODE_HEAT, True, MODE_HEAT, calls=calls) == (
            MODE_HEAT, False, False, True, True
        )


def test_nothing_changed_writes_nothing() -> None:
    assert _write_back(MODE_HEAT, True, MODE_HEAT, calls=(MODE_COOL, ENGAGE_SATISFIED)) == (
        MODE_HEAT, True, False, False, False
    )
    assert _write_back(MODE_HEAT, False, MODE_HEAT, calls=(MODE_HEAT,)) == (
        MODE_HEAT, False, False, False, False
    )


# --- the hold after a start -------------------------------------------------------

def test_a_start_keeps_what_is_left_of_the_hold() -> None:
    wall, now = 1_800_000_000.0, 5_000.0
    assert mode_change_stamp(None, now, wall) is None
    assert mode_change_stamp(wall - 100.0, now, wall) == now - 100.0
    assert mode_change_stamp(wall, now, wall) == now


def test_a_stored_time_in_the_future_counts_as_now() -> None:
    wall, now = 1_800_000_000.0, 5_000.0
    assert mode_change_stamp(wall + 3600.0, now, wall) == now
