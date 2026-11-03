"""Properties of the mode-change hold and the shared-mode arbitration, on the real code.

Each property is one plain sentence from the docs (docs/HOW-IT-WORKS.md "How
MXZ decides", docs/FEATURES.md "Mode-change wait"), checked for every input in
a stated table, not for chosen examples. The oracles are those sentences, not
an older copy of the code. The TLA+ model in verification/modehold/ checks
the same hold over sequences of steps (restarts, reloads, clock steps); its
skill is .claude/skills/verify-mxz-formal/SKILL.md, which also shows each
property here failing on a broken copy of the code.

Tables (stdlib only):

* ROOMS3: three rooms, one entry each: no direction, cool or heat; a freeze
  floor, a Safety priority demand, a Safety priority vote, a real demand or an
  ordinary vote; Comfort priority 0 or 1. Every combination, with engaged,
  current, allowed and resting each over every value: 648,000 cases.
* PAIRS2: two rooms built by the coordinator's own
  ``MXZCoordinator._zone_arbitration_pair`` (demand, hands-off vote, Safety
  priority, floor or ceiling, Dry vote, Comfort priority 0 or 1), with
  engaged, current and allowed over every value: 373,248 cases.

All rooms and values are invented.
"""

from __future__ import annotations

import itertools
import math
import random
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from custom_components.mxz_coordinator.const import MODE_COOL, MODE_HEAT
from custom_components.mxz_coordinator.logic import (
    PRIORITY_DEMAND,
    PRIORITY_FLOOR_HEAT,
    PRIORITY_SAFETY,
    PRIORITY_VOTE,
    Demand,
    arbitrate,
)

COOL, HEAT = MODE_COOL, MODE_HEAT
MODES = (COOL, HEAT)
DIRECTIONS = (None, COOL, HEAT)
KINDS = (
    (PRIORITY_FLOOR_HEAT, False),
    (PRIORITY_SAFETY, False),
    (PRIORITY_SAFETY, True),
    (PRIORITY_DEMAND, False),
    (PRIORITY_VOTE, True),
)
BOOSTS = (0.0, 1.0)
GATES = list(itertools.product((False, True), MODES, (False, True)))  # engaged, current, allowed
RESTING = (None, COOL, HEAT)


def _rooms3():
    per_room = [
        Demand(direction=d, priority=(tier, 0), read_only=ro, boost=b)
        for d, (tier, ro), b in itertools.product(DIRECTIONS, KINDS, BOOSTS)
    ]
    for rooms in itertools.product(per_room, repeat=3):
        entries = [
            Demand(r.direction, (r.priority[0], i), r.read_only, r.boost)
            for i, r in enumerate(rooms)
        ]
        for (engaged, current, allowed), resting in itertools.product(GATES, RESTING):
            yield entries, {
                "engaged": engaged, "current": current, "allowed": allowed, "resting": resting,
            }


def _pairs2():
    from custom_components.mxz_coordinator.coordinator import MXZCoordinator

    per_room = list(
        itertools.product(
            ("neutral", COOL, HEAT),  # the room's demand (DEMAND_NEUTRAL or a mode)
            DIRECTIONS,               # its hands-off vote
            (False, True),            # its Safety priority sensor is on
            DIRECTIONS,               # a tripped floor (heat) or ceiling (cool)
            (None, COOL),             # its head in Dry: a cool vote
            BOOSTS,                   # its Comfort priority
        )
    )
    built: dict[int, list[tuple[Any, ...]]] = {}
    for index in (0, 1):
        built[index] = [
            MXZCoordinator._zone_arbitration_pair(
                demand=demand, vote=vote, safety=safety, index=index,
                floor=floor, dry=dry, boost=boost,
            )
            for demand, vote, safety, floor, dry, boost in per_room
        ]
    for first, second in itertools.product(built[0], built[1]):
        entries = [*first, *second]
        for engaged, current, allowed in GATES:
            yield entries, {
                "engaged": engaged, "current": current, "allowed": allowed, "resting": None,
            }


# --- the oracles: the documented rules, read off the inputs -----------------


def _ordinary_vote(d: Demand) -> bool:
    return d.read_only and d.priority[0] > PRIORITY_SAFETY


def _gate_open(entries: list[Demand], engaged: bool) -> bool:
    """Votes count only while no coordinated room calls and none is mid-run."""
    return not engaged and not any(d.direction for d in entries if not d.read_only)


def _active(entries: list[Demand], engaged: bool) -> list[Demand]:
    """What arbitration weighs: every commanding entry and every Safety vote,
    and the ordinary votes only while the gate is open."""
    gate = _gate_open(entries, engaged)
    return [d for d in entries if not _ordinary_vote(d) or gate]


def _documented_rank(position: int, d: Demand) -> tuple[Any, ...]:
    """Freeze floor first, then the Safety tier, then real demands, then votes;
    within the demand and vote tiers a higher Comfort priority first; then the
    room's position; within one room and rank, a real want before a vote
    ("a real demand beats a vote", logic.py); then entry order."""
    boost = d.boost if d.priority[0] >= PRIORITY_DEMAND else 0.0
    return (d.priority[0], -boost, d.priority[1], d.read_only, position)


def _first_ranked(active: list[Demand]) -> Demand | None:
    calling = [(p, d) for p, d in enumerate(active) if d.direction]
    if not calling:
        return None
    return min(calling, key=lambda pd: _documented_rank(*pd))[1]


def _check(entries: list[Demand], kw: dict[str, Any]) -> dict[str, bool]:
    """Every property for one case: True where it holds."""
    grant = arbitrate(entries, **kw)
    engaged, current, allowed, resting = (
        kw["engaged"], kw["current"], kw["allowed"], kw["resting"],
    )
    active = _active(entries, engaged)
    top = _first_ranked(active)
    without_votes = [
        Demand(None, d.priority, d.read_only, d.boost) if _ordinary_vote(d) else d
        for d in entries
    ]
    rest = resting if resting in MODES else current
    return {
        # The hold: while it holds, the shared mode does not change.
        "hold_blocks_every_change": allowed or grant.direction == current,
        # A hands-off vote never takes the mode from a calling or running room.
        "vote_never_beats_a_call_or_a_run": _gate_open(entries, engaged)
        or arbitrate(without_votes, **kw).direction == grant.direction,
        # Once the hold allows it, the highest-ranked want decides.
        "highest_rank_decides": not allowed or top is None
        or grant.direction == top.direction,
        # With nothing calling, the mode rests: Resting mode, else where it was.
        "nothing_calling_rests": not allowed or top is not None
        or grant.direction == rest,
        # A change exempt from the hold is one only votes made, with the gate open.
        "exempt_only_with_the_gate_open": not grant.read_only
        or _gate_open(entries, engaged),
        # "By safety" is exactly: a Safety-tier want agrees with the answer.
        "by_safety_means_a_safety_want_agrees": grant.by_safety == any(
            d.direction == grant.direction and d.priority[0] <= PRIORITY_SAFETY
            for d in active
        ),
    }


PROPERTIES = (
    "hold_blocks_every_change",
    "vote_never_beats_a_call_or_a_run",
    "highest_rank_decides",
    "nothing_calling_rests",
    "exempt_only_with_the_gate_open",
    "by_safety_means_a_safety_want_agrees",
)
TABLES = {"ROOMS3": (_rooms3, 27_000 * 24), "PAIRS2": (_pairs2, 216 * 216 * 8)}


@pytest.fixture(scope="module")
def sweep() -> dict[str, dict[str, Any]]:
    """One pass over each table: the case count, and each property's first
    counterexample (None while it holds everywhere)."""
    out: dict[str, dict[str, Any]] = {}
    for name, (table, _size) in TABLES.items():
        first: dict[str, Any] = dict.fromkeys(PROPERTIES)
        cases = 0
        for entries, kw in table():
            cases += 1
            for prop, ok in _check(entries, kw).items():
                if not ok and first[prop] is None:
                    first[prop] = (entries, kw, arbitrate(entries, **kw))
        out[name] = {"cases": cases, "first": first}
    return out


@pytest.mark.parametrize("table", list(TABLES))
def test_every_case_in_the_table_was_checked(sweep, table: str) -> None:
    assert sweep[table]["cases"] == TABLES[table][1]


@pytest.mark.parametrize("table", list(TABLES))
@pytest.mark.parametrize("prop", PROPERTIES)
def test_arbitration_property(sweep, table: str, prop: str) -> None:
    assert sweep[table]["first"][prop] is None, sweep[table]["first"][prop]


# --- the hold after a start: mode_change_stamp ------------------------------

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
HOLD = 600.0


def _offsets() -> list[float]:
    """Seconds since the stored change, as a restart reads it: edges, then a
    seeded spread. Negative is a stored time in the future."""
    edges = [
        0.0, -0.0, 1e-9, -1e-9, 0.5, -0.5, 1.0, -1.0, HOLD - 1, HOLD, HOLD + 1,
        -HOLD, 3600.0, -3600.0, 86400.0 * 400, -86400.0 * 400, 1e9, -1e9,
    ]
    rng = random.Random(64)
    spread = [rng.uniform(-2 * HOLD, 2 * HOLD) for _ in range(500)]
    spread += [rng.choice((-1, 1)) * 10 ** rng.uniform(-6, 9) for _ in range(500)]
    return edges + spread


def test_a_start_keeps_what_is_left_of_the_hold_and_never_more(monkeypatch) -> None:
    """After a start, the hold left is the full window less the time since the
    stored change, and never more than the full window: a stored time in the
    future counts as now (docs/FEATURES.md "Mode-change wait")."""
    from homeassistant.util import dt as dt_util

    from custom_components.mxz_coordinator.coordinator import mode_change_stamp

    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    now = NOW.timestamp()
    assert mode_change_stamp(None) is None  # nothing on record, nothing to wait out
    for ago in _offsets():
        stored = now - ago
        before = time.monotonic()
        stamp = mode_change_stamp(stored)
        after = time.monotonic()
        waited = max(0.0, now - stored)  # what the record says has passed
        slack = 1e-6 * max(1.0, abs(waited)) + (after - before)
        assert stamp <= after, (ago, stamp, after)  # never longer than the window
        assert waited - slack <= after - stamp <= waited + slack, (ago, stamp, after)


# --- the record a start reads: async_load_last_mode_change ------------------

_GOOD = [0, 1, -5, 1.5, 1.7e9, 1e18, -1e18, 2**53 + 1]
_BAD = [
    math.nan, math.inf, -math.inf, True, False, None, "1700000000", "", [1], {"a": 1},
]


def _records() -> list[tuple[Any, Any]]:
    """(data in the file, what the start must read): a finite number is that
    number; anything else that is there is a change made now."""
    rng = random.Random(66)
    out: list[tuple[Any, Any]] = [({"last_mode_change": v}, float(v)) for v in _GOOD]
    out += [({"last_mode_change": rng.uniform(-1e10, 1e10)}, "same") for _ in range(50)]
    out += [({"last_mode_change": v}, "now") for v in _BAD]
    out += [({}, "now"), ({"other": 1.0}, "now"), ([1.0], "now"), ("x", "now"), (5, "now")]
    return out


async def test_a_start_reads_the_record_or_holds_the_full_window(
    hass, hass_storage, monkeypatch
) -> None:
    """No record is no wait; a readable one is that time; one that is there but
    unreadable counts as a change made now (a full hold), whatever is in it."""
    from homeassistant.util import dt as dt_util

    from custom_components.mxz_coordinator.const import DOMAIN
    from custom_components.mxz_coordinator.coordinator import (
        async_load_last_mode_change,
    )

    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    entry_id = "invented_entry"
    key = f"{DOMAIN}.{entry_id}.mode_change"

    def fresh_store() -> None:
        hass.data.get(f"{DOMAIN}_mode_change_stores", {}).pop(entry_id, None)

    fresh_store()
    hass_storage.pop(key, None)
    assert await async_load_last_mode_change(hass, entry_id) is None
    for data, want in _records():
        fresh_store()
        hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}
        got = await async_load_last_mode_change(hass, entry_id)
        if want == "now":
            assert got == NOW.timestamp(), (data, got)
        else:
            expect = float(data["last_mode_change"]) if want == "same" else want
            assert type(got) is float and got == expect, (data, got)
    fresh_store()
