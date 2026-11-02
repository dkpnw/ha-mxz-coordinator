"""Comfort priority in the pure tie-break: logic.arbitrate, comfort_priority, comfort_stale_in.

Two independent oracles, neither of them the code under test:

* ``_arbitrate_main`` is ``logic.arbitrate`` exactly as it was on main 0a947f0,
  before Comfort priority existed. It sorts by ``Demand.priority`` alone, so it
  cannot see a boost. Every boost at 0 (or on a safety-rank entry only) must
  give the same Grant over an exhaustive table.
* ``shared_mode`` over the demands in room order: for commanding entries only,
  the room-order answer the YAML package always gave.

All rooms and entities here are invented.
"""

from __future__ import annotations

import itertools
import math

import pytest

from custom_components.mxz_coordinator.const import (
    COMFORT_MISSING,
    COMFORT_NOT_A_NUMBER,
    COMFORT_NOT_SET,
    COMFORT_NUMBER,
    COMFORT_OFF,
    COMFORT_ON,
    COMFORT_STALE,
    COMFORT_UNAVAILABLE,
    COMFORT_UNKNOWN,
    MODE_COOL,
    MODE_HEAT,
)
from custom_components.mxz_coordinator.logic import (
    PRIORITY_DEMAND,
    PRIORITY_FLOOR_HEAT,
    PRIORITY_SAFETY,
    PRIORITY_VOTE,
    Demand,
    Grant,
    arbitrate,
    comfort_boost,
    comfort_priority,
    comfort_stale_in,
    shared_mode,
)

COOL, HEAT = MODE_COOL, MODE_HEAT
HOUR = 3600.0


def _arbitrate_main(
    demands: list[Demand], *, engaged: bool, current: str, allowed: bool,
    resting: str | None = None,
) -> Grant:
    """``logic.arbitrate`` as on main 0a947f0 (pre Comfort priority), verbatim
    apart from this docstring: the sort key is ``d.priority`` only."""
    ranked = sorted(demands, key=lambda d: d.priority)
    commanding = [d for d in ranked if not d.read_only]
    safety_votes = [
        d for d in ranked if d.read_only and d.priority[0] <= PRIORITY_SAFETY
    ]
    ordinary_votes = [
        d for d in ranked if d.read_only and d.priority[0] > PRIORITY_SAFETY
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
        d.direction == full and d.priority[0] <= PRIORITY_SAFETY for d in active
    )
    if not any(d.read_only and d.direction for d in active):
        return Grant(direction=full, read_only=False, by_safety=by_safety)
    plain = shared_mode(
        demands=[d.direction for d in active if d.direction and not d.read_only],
        current=current, allowed=allowed, resting=resting,
    )
    read_only = full != plain and gate_open
    return Grant(direction=full, read_only=read_only, by_safety=by_safety)


# Each room's one entry: (tier, read_only). A freeze floor, a safety demand, a
# safety vote, a real demand and an ordinary vote.
KINDS = (
    (PRIORITY_FLOOR_HEAT, False),
    (PRIORITY_SAFETY, False),
    (PRIORITY_SAFETY, True),
    (PRIORITY_DEMAND, False),
    (PRIORITY_VOTE, True),
)
DIRECTIONS = (None, COOL, HEAT)
ROOMS = 3


def _table():
    """Every (entries, engaged, current, allowed, resting) for three rooms."""
    per_room = list(itertools.product(DIRECTIONS, KINDS))
    for rooms in itertools.product(per_room, repeat=ROOMS):
        entries = [
            Demand(direction=d, priority=(tier, i), read_only=ro)
            for i, (d, (tier, ro)) in enumerate(rooms)
        ]
        for engaged, current, allowed, resting in itertools.product(
            (False, True), (COOL, HEAT), (False, True), (None, COOL, HEAT)
        ):
            yield entries, {"engaged": engaged, "current": current, "allowed": allowed, "resting": resting}


def _with(entries: list[Demand], boosts: list[float]) -> list[Demand]:
    return [
        Demand(direction=d.direction, priority=d.priority, read_only=d.read_only, boost=b)
        for d, b in zip(entries, boosts, strict=True)
    ]


def test_every_boost_zero_is_main_over_the_exhaustive_table() -> None:
    """Off means today: no boost anywhere gives main's Grant, every field, every case."""
    count = 0
    for entries, kw in _table():
        assert arbitrate(entries, **kw) == _arbitrate_main(entries, **kw), (entries, kw)
        count += 1
    assert count == (len(DIRECTIONS) * len(KINDS)) ** ROOMS * 24


def test_commanding_only_matches_room_order_shared_mode() -> None:
    """Commanding demands with no boost settle exactly as shared_mode in room order."""
    for dirs in itertools.product(DIRECTIONS, repeat=4):
        for current, allowed in itertools.product((COOL, HEAT), (False, True)):
            entries = [Demand(direction=d, priority=(PRIORITY_DEMAND, i)) for i, d in enumerate(dirs)]
            want = shared_mode(
                demands=[d for d in dirs if d], current=current, allowed=allowed
            )
            got = arbitrate(entries, engaged=False, current=current, allowed=allowed)
            assert got.direction == want, (dirs, current, allowed)


@pytest.mark.parametrize("boost", [5.0, 1.0, math.inf, -3.0, math.nan])
def test_a_boost_on_a_safety_rank_entry_changes_nothing(boost: float) -> None:
    """Exhaustive: any boost on any floor/safety/ceiling entry is main's answer."""
    for entries, kw in _table():
        boosted = _with(entries, [boost if d.priority[0] <= PRIORITY_SAFETY else 0.0 for d in entries])
        assert arbitrate(boosted, **kw) == _arbitrate_main(entries, **kw), (boosted, kw)


def test_a_boosted_vote_never_beats_a_calling_or_engaged_demand() -> None:
    """Exhaustive: while any commanding entry calls or a room is engaged, a boost
    on ordinary votes is main's answer (the vote gate is shut either way)."""
    checked = 0
    for entries, kw in _table():
        if not (kw["engaged"] or any(d.direction and not d.read_only for d in entries)):
            continue
        boosted = _with(entries, [9.0 if d.read_only else 0.0 for d in entries])
        assert arbitrate(boosted, **kw) == _arbitrate_main(entries, **kw), (boosted, kw)
        checked += 1
    assert checked > 0


def test_a_boosted_comfort_demand_never_beats_a_higher_tier() -> None:
    """Exhaustive: boosting ordinary demands and votes never changes the answer
    while a floor, safety or ceiling entry with a direction is active and the
    answer is that entry's direction (higher tiers always sort first)."""
    for entries, kw in _table():
        top = [d for d in entries if d.priority[0] <= PRIORITY_SAFETY and d.direction]
        if not top:
            continue
        boosted = _with(entries, [0.0 if d.priority[0] <= PRIORITY_SAFETY else 9.0 for d in entries])
        assert arbitrate(boosted, **kw) == _arbitrate_main(entries, **kw), (boosted, kw)


def test_a_boosted_lower_room_wins_a_demand_standoff() -> None:
    plain = [
        Demand(COOL, (PRIORITY_DEMAND, 0)),
        Demand(HEAT, (PRIORITY_DEMAND, 1)),
    ]
    assert arbitrate(plain, engaged=False, current=COOL, allowed=True).direction == COOL
    boosted = [plain[0], Demand(HEAT, (PRIORITY_DEMAND, 1), boost=1.0)]
    grant = arbitrate(boosted, engaged=False, current=COOL, allowed=True)
    assert grant == Grant(direction=HEAT, read_only=False, by_safety=False)


def test_a_boosted_hands_off_vote_wins_among_votes() -> None:
    votes = [
        Demand(HEAT, (PRIORITY_VOTE, 0), read_only=True),
        Demand(COOL, (PRIORITY_VOTE, 1), read_only=True),
    ]
    assert arbitrate(votes, engaged=False, current=HEAT, allowed=True).direction == HEAT
    votes[1] = Demand(COOL, (PRIORITY_VOTE, 1), read_only=True, boost=1.0)
    grant = arbitrate(votes, engaged=False, current=HEAT, allowed=True)
    assert grant.direction == COOL
    assert grant.read_only is True  # still a vote's answer: the dwell exemption holds


@pytest.mark.parametrize("boost", [1.0, 2.5])
def test_equal_boosts_fall_back_to_room_order(boost: float) -> None:
    entries = [
        Demand(HEAT, (PRIORITY_DEMAND, 0), boost=boost),
        Demand(COOL, (PRIORITY_DEMAND, 1), boost=boost),
        Demand(COOL, (PRIORITY_DEMAND, 2), boost=boost),
    ]
    assert arbitrate(entries, engaged=False, current=COOL, allowed=True).direction == HEAT


def test_a_higher_number_beats_a_lower_one() -> None:
    """A number entity reading 2 beats an occupied on/off sensor (1)."""
    entries = [
        Demand(COOL, (PRIORITY_DEMAND, 0)),
        Demand(HEAT, (PRIORITY_DEMAND, 1), boost=1.0),
        Demand(COOL, (PRIORITY_DEMAND, 2), boost=2.0),
    ]
    assert arbitrate(entries, engaged=False, current=HEAT, allowed=True).direction == COOL


def test_a_boosted_vote_loses_to_an_unboosted_real_demand() -> None:
    entries = [
        Demand(COOL, (PRIORITY_DEMAND, 1)),
        Demand(HEAT, (PRIORITY_VOTE, 0), read_only=True, boost=100.0),
    ]
    assert arbitrate(entries, engaged=False, current=COOL, allowed=True).direction == COOL
    # And to an engaged run whose demand has gone quiet.
    entries[0] = Demand(None, (PRIORITY_DEMAND, 1))
    assert arbitrate(entries, engaged=True, current=COOL, allowed=True).direction == COOL


@pytest.mark.parametrize(
    ("winner", "want"),
    [
        # An unboosted Safety priority room (its hands-off vote, so it differs
        # from the ceiling's commanding entry below).
        (Demand(COOL, (PRIORITY_SAFETY, 1), read_only=True), COOL),
        (Demand(HEAT, (PRIORITY_FLOOR_HEAT, 1)), HEAT),  # a tripped freeze floor
        (Demand(COOL, (PRIORITY_SAFETY, 1)), COOL),  # a tripped overheat ceiling's entry
    ],
    ids=["safety", "freeze_floor", "overheat_ceiling"],
)
def test_a_boosted_comfort_demand_loses_to_safety_floor_and_ceiling(
    winner: Demand, want: str
) -> None:
    other = HEAT if want == COOL else COOL
    entries = [Demand(other, (PRIORITY_DEMAND, 0), boost=50.0), winner]
    for current in (COOL, HEAT):
        assert arbitrate(entries, engaged=False, current=current, allowed=True).direction == want


def test_a_boost_on_a_safety_entry_does_not_reorder_safety_rooms() -> None:
    """Safety rooms settle by room order exactly as today, whatever their boosts."""
    entries = [
        Demand(COOL, (PRIORITY_SAFETY, 0)),
        Demand(HEAT, (PRIORITY_SAFETY, 1), boost=10.0),
    ]
    assert arbitrate(entries, engaged=False, current=HEAT, allowed=True).direction == COOL


def test_the_wait_holds_a_boosted_flip_until_it_ends() -> None:
    entries = [
        Demand(COOL, (PRIORITY_DEMAND, 0)),
        Demand(HEAT, (PRIORITY_DEMAND, 1), boost=1.0),
    ]
    assert arbitrate(entries, engaged=False, current=COOL, allowed=False).direction == COOL
    assert arbitrate(entries, engaged=False, current=COOL, allowed=True).direction == HEAT


@pytest.mark.parametrize(
    ("boost", "tier", "want"),
    [
        (1.0, PRIORITY_DEMAND, 1.0),
        (2.5, PRIORITY_VOTE, 2.5),
        (0.0, PRIORITY_DEMAND, 0.0),
        (-1.0, PRIORITY_DEMAND, 0.0),
        (math.nan, PRIORITY_DEMAND, 0.0),
        (math.inf, PRIORITY_DEMAND, 0.0),
        (-math.inf, PRIORITY_VOTE, 0.0),
        (True, PRIORITY_DEMAND, 0.0),
        (3.0, PRIORITY_SAFETY, 0.0),
        (3.0, PRIORITY_FLOOR_HEAT, 0.0),
    ],
)
def test_comfort_boost_reads_only_a_positive_finite_number_below_safety(
    boost: float, tier: int, want: float
) -> None:
    assert comfort_boost(Demand(HEAT, (tier, 0), boost=boost)) == want


def test_a_nan_or_negative_boost_never_demotes_a_room() -> None:
    """A bad boost on the top room leaves it on top (it is 0, not last)."""
    for bad in (math.nan, -5.0, -math.inf):
        entries = [
            Demand(COOL, (PRIORITY_DEMAND, 0), boost=bad),
            Demand(HEAT, (PRIORITY_DEMAND, 1)),
        ]
        assert arbitrate(entries, engaged=False, current=HEAT, allowed=True).direction == COOL


# --- the reading: comfort_priority -------------------------------------------


@pytest.mark.parametrize(
    ("domain", "state", "want"),
    [
        (None, None, (0.0, COMFORT_NOT_SET)),
        ("binary_sensor", None, (0.0, COMFORT_MISSING)),
        ("binary_sensor", "on", (1.0, COMFORT_ON)),
        ("binary_sensor", "off", (0.0, COMFORT_OFF)),
        ("binary_sensor", "unavailable", (0.0, COMFORT_UNAVAILABLE)),
        ("binary_sensor", "unknown", (0.0, COMFORT_UNKNOWN)),
        ("binary_sensor", "On", (0.0, COMFORT_OFF)),
        ("input_boolean", "on", (1.0, COMFORT_ON)),
        ("input_boolean", "off", (0.0, COMFORT_OFF)),
        ("number", "2", (2.0, COMFORT_NUMBER)),
        ("number", "0", (0.0, COMFORT_NUMBER)),
        ("number", "-2", (0.0, COMFORT_NUMBER)),
        ("input_number", "1.5", (1.5, COMFORT_NUMBER)),
        ("sensor", "3", (3.0, COMFORT_NUMBER)),
        ("sensor", "nan", (0.0, COMFORT_NOT_A_NUMBER)),
        ("sensor", "inf", (0.0, COMFORT_NOT_A_NUMBER)),
        ("sensor", "-inf", (0.0, COMFORT_NOT_A_NUMBER)),
        ("sensor", "occupied", (0.0, COMFORT_NOT_A_NUMBER)),
        ("sensor", "on", (0.0, COMFORT_NOT_A_NUMBER)),
        ("sensor", "", (0.0, COMFORT_NOT_A_NUMBER)),
        ("number", "unavailable", (0.0, COMFORT_UNAVAILABLE)),
        ("sensor", "unknown", (0.0, COMFORT_UNKNOWN)),
        ("sensor", None, (0.0, COMFORT_MISSING)),
    ],
)
def test_comfort_priority_maps_every_reading(domain, state, want) -> None:
    assert comfort_priority(domain, state) == want
    # The boost is never below 0, whatever the reading.
    assert comfort_priority(domain, state)[0] >= 0


LIMIT = 12 * HOUR


@pytest.mark.parametrize(
    ("age", "want"),
    [
        (0.0, (1.0, COMFORT_ON)),  # fresh
        (LIMIT - 1, (1.0, COMFORT_ON)),  # just under the limit
        (LIMIT, (0.0, COMFORT_STALE)),  # at the limit: stale (half-open)
        (LIMIT + 1, (0.0, COMFORT_STALE)),  # just over
        (None, (1.0, COMFORT_ON)),  # unknown age is fresh
        (-60.0, (1.0, COMFORT_ON)),  # a clock stepped back is fresh too
    ],
)
def test_an_on_goes_stale_at_the_limit(age, want) -> None:
    for domain in ("binary_sensor", "input_boolean"):
        assert comfort_priority(domain, "on", age=age, stale_after=LIMIT) == want


def test_no_limit_never_goes_stale() -> None:
    assert comfort_priority("binary_sensor", "on", age=1e9, stale_after=None) == (1.0, COMFORT_ON)


@pytest.mark.parametrize("domain", ["number", "input_number", "sensor"])
def test_the_stale_limit_never_touches_a_number(domain: str) -> None:
    assert comfort_priority(domain, "1", age=LIMIT * 10, stale_after=LIMIT) == (1.0, COMFORT_NUMBER)
    assert comfort_stale_in(domain, "1", age=0.0, stale_after=LIMIT) is None


def test_off_is_off_at_any_age() -> None:
    assert comfort_priority("binary_sensor", "off", age=LIMIT * 2, stale_after=LIMIT) == (0.0, COMFORT_OFF)


@pytest.mark.parametrize(
    ("domain", "state", "age", "limit", "want"),
    [
        ("binary_sensor", "on", 0.0, LIMIT, LIMIT),
        ("input_boolean", "on", LIMIT - 5, LIMIT, 5.0),
        ("binary_sensor", "on", LIMIT, LIMIT, None),  # already stale
        ("binary_sensor", "on", None, LIMIT, None),  # unknown age: nothing to time
        ("binary_sensor", "on", 0.0, None, None),  # no limit
        ("binary_sensor", "off", 0.0, LIMIT, None),
        ("binary_sensor", None, 0.0, LIMIT, None),
        (None, None, None, LIMIT, None),
    ],
)
def test_comfort_stale_in_is_the_recheck_moment(domain, state, age, limit, want) -> None:
    assert comfort_stale_in(domain, state, age=age, stale_after=limit) == want
