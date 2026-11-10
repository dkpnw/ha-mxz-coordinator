"""Hypothesis properties of the shared mode, the hold and restart continuity, on the real code.

docs/formal/README.md states each rule; docs/formal/tla/MXZCoordinator.tla checks
the same rules over every order of steps in a small model. Here Hypothesis picks
the steps and the code under test is the integration itself (tests/property_rig.py).
The oracle in each test is the rule's own sentence, computed from the room
temperatures and a clock the test keeps, never read back from MXZ.

tests/test_modehold_properties.py sweeps ``logic.arbitrate`` over fixed tables;
these add arbitrary room counts and Comfort priority values, and whole sequences.
"""

from __future__ import annotations

import math
import time
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from hypothesis import example, given
from hypothesis import strategies as st

from custom_components.mxz_coordinator.const import MODE_COOL, MODE_HEAT
from custom_components.mxz_coordinator.coordinator import (
    ModeRecord,
    MXZCoordinator,
    _mode_change_store,
    async_load_mode_record,
    mode_change_stamp,
)
from custom_components.mxz_coordinator.logic import (
    PRIORITY_DEMAND,
    PRIORITY_FLOOR_HEAT,
    PRIORITY_SAFETY,
    PRIORITY_VOTE,
    Demand,
    arbitrate,
)
from tests.property_rig import HOLD, PURE, SEQUENCES, TARGET, Rig, open_rig

MODES = (MODE_COOL, MODE_HEAT)
THRESHOLD = 3.0  # DEFAULT_DEMAND_THRESHOLD, as the docs state it
# Off the threshold on each side, inside it, and at the target. None sits on an edge.
TEMPS = (TARGET - 6, TARGET - 3.5, TARGET - 2, TARGET - 0.5, TARGET, TARGET + 0.5, TARGET + 2, TARGET + 3.5, TARGET + 6)

demands = st.builds(
    lambda direction, tier, vote, boost: (direction, tier, vote, boost),
    st.sampled_from((None, MODE_COOL, MODE_HEAT)),
    st.sampled_from((PRIORITY_FLOOR_HEAT, PRIORITY_SAFETY, PRIORITY_DEMAND, PRIORITY_VOTE)),
    st.booleans(),
    st.one_of(st.just(0.0), st.floats(allow_nan=True, allow_infinity=True)),
)


def _entries(rows: list[tuple[Any, int, bool, Any]]) -> list[Demand]:
    return [
        Demand(direction, (tier, index), vote, boost)
        for index, (direction, tier, vote, boost) in enumerate(rows)
    ]


@PURE
@given(
    rows=st.lists(demands, max_size=6),
    engaged=st.booleans(),
    current=st.sampled_from(MODES),
    resting=st.sampled_from((None, *MODES)),
    running=st.booleans(),
)
def test_arbitrate_never_changes_the_mode_while_the_hold_holds(
    rows, engaged, current, resting, running
) -> None:
    """Anything may be asked of it, at any rank: with the gate shut the mode stays."""
    grant = arbitrate(
        _entries(rows), engaged=engaged, current=current, allowed=False,
        resting=resting, running=running,
    )
    assert grant.direction == current


@PURE
@given(rows=st.lists(demands, max_size=6), current=st.sampled_from(MODES), resting=st.sampled_from(MODES))
def test_a_run_in_the_shared_mode_keeps_it_from_resting_mode(rows, current, resting) -> None:
    """A silent table never leaves the mode a room is running in, whatever Resting mode says."""
    silent = [(None, tier, vote, boost) for _, tier, vote, boost in rows]
    grant = arbitrate(
        _entries(silent), engaged=True, current=current, allowed=True,
        resting=resting, running=True,
    )
    assert grant.direction == current


def _calling(temp: float) -> str | None:
    """A room calls once it is past the threshold on one side of its target."""
    if temp >= TARGET + THRESHOLD:
        return MODE_COOL
    if temp <= TARGET - THRESHOLD:
        return MODE_HEAT
    return None


class HoldOracle:
    """The hold as the docs state it, on the test's own clock."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.mode = rig.coord.current_shared_mode
        self.changed_at: float | None = None

    def allowed(self) -> bool:
        return self.changed_at is None or self.rig.now() - self.changed_at >= HOLD

    def picked(self, mode: str) -> None:
        """A person's pick of another mode starts the hold."""
        if mode != self.mode:
            self.mode, self.changed_at = mode, self.rig.now()

    def check(self, temps: list[float], allowed_before: bool) -> None:
        coord = self.rig.coord
        mode = coord.current_shared_mode
        # No flip inside the hold.
        if mode != self.mode:
            assert allowed_before, f"{self.mode} -> {mode} inside the hold"
            self.mode, self.changed_at = mode, self.rig.now()
        # With the gate open, the first calling room in order decides.
        first = next((call for call in map(_calling, temps) if call), None)
        if allowed_before and first:
            assert mode == first, f"no flip to {first} though nothing holds it"
        # The hold left is the window less the time since the change, exactly:
        # never longer (a start adds no wait), never shorter.
        left = 0.0 if self.changed_at is None else max(0.0, HOLD - (self.rig.now() - self.changed_at))
        assert coord.mode_hold_remaining() == pytest.approx(left, abs=1e-3)
        # One outdoor unit: no head runs against the shared mode.
        for head in self.rig.heads:
            assert head.mode in (mode, "fan_only", "off")


steps = st.lists(
    st.one_of(
        st.tuples(st.just("temp"), st.integers(0, 1), st.sampled_from(TEMPS)),
        st.tuples(st.just("later"), st.sampled_from((1, HOLD // 2, HOLD - 1, HOLD, HOLD + 1))),
        st.tuples(st.just("select"), st.sampled_from(MODES)),
        st.tuples(st.just("reload")),
        st.tuples(st.just("restart")),
    ),
    min_size=1,
    max_size=14,
)


@SEQUENCES
@given(first=st.tuples(st.sampled_from(TEMPS), st.sampled_from(TEMPS)), steps=steps)
async def test_no_flip_inside_the_hold_and_no_wait_after_it(
    hass: HomeAssistant, freezer: Any, first: tuple[float, float], steps: list[tuple[Any, ...]]
) -> None:
    """Over any order of readings, time, picks, reloads and restarts: the shared mode
    changes only with the hold passed, does change once it has, and a start neither
    lengthens nor shortens what is left of it."""
    temps = list(first)
    rig = await open_rig(hass, freezer, temps=first)
    try:
        oracle = HoldOracle(rig)
        # A fresh install holds nothing, so a first call has flipped already at setup,
        # then starting the hold. Which is the one fact read from MXZ: when that was.
        oracle.changed_at = rig.coord._last_mode_change_at
        for step in steps:
            kind = step[0]
            if kind == "temp":
                allowed = oracle.allowed()
                temps[step[1]] = step[2]
                await rig.temp(step[1], step[2])
            elif kind == "later":
                # The gate is read at the time the step ends.
                allowed = oracle.changed_at is None or (
                    rig.now() + step[1] - oracle.changed_at >= HOLD
                )
                await rig.later(step[1])
            elif kind == "select":
                oracle.picked(step[1])
                allowed = oracle.allowed()
                await rig.select_mode(step[1])
            else:
                allowed = oracle.allowed()
                await (rig.reload() if kind == "reload" else rig.restart())
            oracle.check(temps, allowed)
    finally:
        await rig.close()


@PURE
@given(ago=st.floats(min_value=-1e7, max_value=1e7, allow_nan=False))
def test_a_start_keeps_what_is_left_of_the_hold_and_never_more(ago: float) -> None:
    """The stamp reads as ``ago`` seconds back; a stored time in the future reads as now."""
    stored = dt_util.utcnow().timestamp() - ago
    now = time.monotonic()
    stamp = mode_change_stamp(stored, now)
    assert stamp is not None
    # Rounding in the epoch-seconds round trip, plus a slow runner's pause.
    slack = 1.0
    assert now - max(0.0, ago) - slack <= stamp <= now - max(0.0, ago) + slack
    assert stamp <= now


def test_no_stored_change_holds_nothing() -> None:
    assert mode_change_stamp(None, time.monotonic()) is None


_finite = st.floats(allow_nan=False, allow_infinity=False, min_value=0, max_value=4e9)
_junk = st.one_of(
    st.none(), st.booleans(), st.text(max_size=4), st.integers(), st.floats(),
    st.lists(st.integers(), max_size=2), st.sampled_from(MODES),
    st.sampled_from((math.inf, -math.inf, math.nan)),
)
_records = st.dictionaries(
    st.sampled_from(("last_mode_change", "mode", "mode_at", "mode_by_votes", "other")), _junk, max_size=5,
)


@PURE
@given(data=st.one_of(_records, _junk))
# Every non-finite time, always: what Hypothesis draws shifts with the code it has loaded.
@example(data={"last_mode_change": math.inf})
@example(data={"last_mode_change": -math.inf})
@example(data={"last_mode_change": math.nan})
@example(data={"last_mode_change": 1.0, "mode": MODES[0], "mode_at": math.inf, "mode_by_votes": False})
async def test_any_stored_record_loads_as_a_readable_record(hass: HomeAssistant, data: Any) -> None:
    """Whatever the file holds, a start gets a usable record: a mode only with its
    time and flag, and a change time that is finite or none."""
    store = _mode_change_store(hass, "invented_entry")

    async def _load() -> Any:
        return data

    store.async_load = _load
    record = await async_load_mode_record(hass, "invented_entry")
    assert record.changed_at is None or math.isfinite(record.changed_at)
    if record.mode is not None:
        assert record.mode in MODES
        assert math.isfinite(record.mode_at)
        assert isinstance(record.by_votes, bool)


@PURE
@given(
    record_mode=st.sampled_from((None, *MODES)),
    record_at=_finite,
    by_votes=st.booleans(),
    restored=st.sampled_from((None, *MODES)),
    restored_at=st.one_of(st.none(), _finite),
)
def test_a_start_resumes_the_recorded_mode(record_mode, record_at, by_votes, restored, restored_at) -> None:
    """The record wins over the select's restore data, unless they disagree and the
    restore data is newer; then the record is brought up to date and by-votes is unknown."""
    writes: list[bool] = []
    # The real method, on just the attributes it reads and sets.
    coord = SimpleNamespace(
        _mode_record=ModeRecord(mode=record_mode, mode_at=record_at, by_votes=by_votes if record_mode else False),
        _record_mode=lambda: writes.append(True),
        current_shared_mode=None,
        _mode_by_votes=None,
    )
    mode = MXZCoordinator.restore_shared_mode(coord, restored, restored_at)
    assert mode == coord.current_shared_mode
    if record_mode is None:
        assert (mode, coord._mode_by_votes, writes) == (restored or MODE_COOL, False, [])
    elif restored in (None, record_mode) or restored_at is None or record_at >= restored_at:
        assert (mode, coord._mode_by_votes, writes) == (record_mode, by_votes, [])
    else:
        assert (mode, coord._mode_by_votes, writes) == (restored, False, [True])
