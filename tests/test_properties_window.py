"""Hypothesis properties of the door/window pause, on the real code.

The rules (docs/FEATURES.md "Door/window pause", docs/formal/README.md): open past
its delay stops the head and the room's vote; a person's change on the paused head
takes the room over until the window closes; a pause and an override outlive a
reload or restart; no reading never starts a pause; a restart can only lengthen a
delay. The oracle is those sentences, on a clock the test keeps.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from hypothesis import given
from hypothesis import strategies as st

from custom_components.mxz_coordinator.const import (
    MODE_COOL,
    MODE_HEAT,
    OPENING_CLOSED,
    OPENING_CLOSING,
    OPENING_OPENING,
    OPENING_OVERRIDDEN,
    OPENING_PAUSED,
    OPENING_PAUSED_STATES,
)
from custom_components.mxz_coordinator.logic import (
    CLOSED_READ,
    OPEN_READ,
    opening_due,
    opening_reading,
    opening_state,
)
from tests.property_rig import (
    CLOSE_DELAY,
    OPEN_DELAY,
    PURE,
    SEQUENCES,
    TARGET,
    open_rig,
)

STATES = (OPENING_CLOSED, OPENING_OPENING, OPENING_PAUSED, OPENING_CLOSING, OPENING_OVERRIDDEN)
READINGS = (OPEN_READ, CLOSED_READ, None)
times = st.floats(min_value=0, max_value=1e6, allow_nan=False)
delays = st.floats(min_value=0, max_value=1e4, allow_nan=False)


def _state(raw, age, open_delay, close_delay, prior, override):
    return opening_state(
        raw=raw, since=0.0, now=age, open_delay=open_delay, close_delay=close_delay,
        prior=prior, override=override,
    )


@PURE
@given(age=times, open_delay=delays, close_delay=delays, prior=st.sampled_from(STATES), override=st.booleans())
def test_no_reading_never_starts_a_pause_and_never_ends_one(age, open_delay, close_delay, prior, override) -> None:
    state = _state(None, age, open_delay, close_delay, prior, override)
    if override:
        assert state not in OPENING_PAUSED_STATES
    elif prior in OPENING_PAUSED_STATES:
        assert state == OPENING_PAUSED
    else:
        assert state == OPENING_CLOSED


@PURE
@given(age=times, open_delay=delays, close_delay=delays, prior=st.sampled_from(STATES))
def test_an_override_is_never_paused_and_a_closed_reading_ends_it(age, open_delay, close_delay, prior) -> None:
    assert _state(OPEN_READ, age, open_delay, close_delay, prior, True) == OPENING_OVERRIDDEN
    assert _state(CLOSED_READ, age, open_delay, close_delay, prior, True) == OPENING_CLOSED


@PURE
@given(age=times, open_delay=delays, close_delay=delays, prior=st.sampled_from(STATES))
def test_the_delays_decide_a_pause_and_a_resume(age, open_delay, close_delay, prior) -> None:
    if prior == OPENING_OVERRIDDEN:
        return  # an override's end is its own property above
    opened = _state(OPEN_READ, age, open_delay, close_delay, prior, False)
    if prior in OPENING_PAUSED_STATES or age >= open_delay:
        assert opened == OPENING_PAUSED
    else:
        assert opened == OPENING_OPENING
    closed = _state(CLOSED_READ, age, open_delay, close_delay, prior, False)
    if prior in OPENING_PAUSED_STATES and age < close_delay:
        assert closed == OPENING_CLOSING
    else:
        assert closed == OPENING_CLOSED


@PURE
@given(since=times, open_delay=delays, close_delay=delays, state=st.sampled_from(STATES))
def test_only_a_running_delay_has_a_wakeup(since, open_delay, close_delay, state) -> None:
    due = opening_due(state=state, since=since, open_delay=open_delay, close_delay=close_delay)
    assert due == {OPENING_OPENING: since + open_delay, OPENING_CLOSING: since + close_delay}.get(state)


@PURE
@given(state=st.one_of(st.none(), st.text(max_size=12), st.sampled_from(("on", "off", "unavailable", "unknown"))))
def test_only_on_and_off_are_readings(state) -> None:
    assert opening_reading(state) == {"on": OPEN_READ, "off": CLOSED_READ}.get(state)


class Pause:
    """The pause as the docs state it: the window's reading, for how long, and a takeover."""

    def __init__(self, rig: Any) -> None:
        self.rig = rig
        self.reading = "off"
        self.since = rig.now()
        self.paused = False
        self.overriding = False  # an override is on and has something to override
        self.armed = False  # the switch is on, with no reading and no pause: it waits
        self.mode_writes_at = 0

    def window(self, reading: str) -> None:
        if reading != self.reading:
            self.reading, self.since = reading, self.rig.now()
        if reading == "off":
            self.overriding = self.armed = False  # a closed reading ends an override at once
        elif reading == "on" and self.armed:
            self.overriding, self.armed = True, False

    def started(self) -> None:
        """A start counts a delay anew: it can lengthen one, never shorten it."""
        self.since = self.rig.now()

    def _takeover(self) -> None:
        self.overriding, self.paused = True, False
        self.mode_writes_at = len(self.rig.heads[0].mode_writes)

    def person_mode(self, mode: str, shown: str) -> None:
        if self.paused and mode not in ("off", shown) and self.reading != "off":
            self._takeover()

    def override_switch(self, on: bool) -> None:
        if not on:
            self.overriding = self.armed = False
        elif self.reading == "on" or (self.paused and self.reading != "off"):
            self._takeover()
        elif self.reading != "off" and not self.overriding:
            self.armed = True

    def check(self) -> None:
        rig, head = self.rig, self.rig.heads[0]
        age = rig.now() - self.since
        state = rig.coord.opening_state_of("primary")
        if self.overriding:
            assert state == OPENING_OVERRIDDEN
            assert rig.row(0)["window_override"] is True
            assert "off" not in head.mode_writes[self.mode_writes_at:], "an override was turned off"
            return
        assert rig.coord.window_override("primary") is self.armed
        if self.reading == "on":
            self.paused = self.paused or age >= OPEN_DELAY
            assert state == (OPENING_PAUSED if self.paused else OPENING_OPENING)
        elif self.reading == "off":
            self.paused = self.paused and age < CLOSE_DELAY
            assert state == (OPENING_CLOSING if self.paused else OPENING_CLOSED)
        else:
            assert state == (OPENING_PAUSED if self.paused else OPENING_CLOSED)
        if self.paused:
            # The head is stopped and the room has no call, no run and no vote.
            assert head.mode == "off", "a paused head still runs"
            assert rig.row(0)["demand"] == "neutral"
            assert rig.row(0)["engage"] == "satisfied"
            assert rig.row(0)["paused_reason"] == "door/window open"


# Most examples open on the state under study, so a seeded run of a few dozen reaches it.
_PAUSE = [("window", "on"), ("later", OPEN_DELAY)]
openers = st.sampled_from((
    [],
    _PAUSE,
    [*_PAUSE, ("person", MODE_COOL)],
    [*_PAUSE, ("person", MODE_HEAT)],
    [*_PAUSE, ("override", True)],
))
steps = st.lists(
    st.one_of(
        st.tuples(st.just("window"), st.sampled_from(("on", "off", "unavailable"))),
        st.tuples(st.just("later"), st.sampled_from((1, OPEN_DELAY - 1, OPEN_DELAY, CLOSE_DELAY - 1, CLOSE_DELAY, 3600))),
        st.tuples(st.just("temp"), st.sampled_from((TARGET - 6, TARGET, TARGET + 6))),
        st.tuples(st.just("person"), st.sampled_from((MODE_COOL, MODE_HEAT, "fan_only", "off"))),
        st.tuples(st.just("override"), st.booleans()),
        st.tuples(st.just("reload")),
        st.tuples(st.just("restart")),
    ),
    min_size=1,
    max_size=14,
)


@SEQUENCES
@given(first=st.sampled_from((TARGET - 6, TARGET + 6)), opener=openers, steps=steps)
async def test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes(
    hass: HomeAssistant, freezer: Any, first: float, opener: list[tuple[Any, ...]], steps: list[tuple[Any, ...]]
) -> None:
    rig = await open_rig(hass, freezer, temps=(first, TARGET), window="off")
    try:
        oracle = Pause(rig)
        for step in (*opener, *steps):
            kind = step[0]
            if kind == "window":
                oracle.window(step[1])
                await rig.window(step[1])
            elif kind == "later":
                await rig.later(step[1])
            elif kind == "temp":
                await rig.temp(0, step[1])
            elif kind == "person":
                oracle.person_mode(step[1], rig.heads[0].mode)
                await rig.person_mode(step[1])
            elif kind == "override":
                oracle.override_switch(step[1])
                await rig.switch("_primary_window_override", step[1])
            else:
                await (rig.reload() if kind == "reload" else rig.restart())
                oracle.started()
            oracle.check()
    finally:
        await rig.close()


@SEQUENCES
@given(temps=st.sampled_from((TARGET - 6, TARGET + 6)), other=st.sampled_from((TARGET - 6, TARGET, TARGET + 6)))
async def test_a_paused_room_never_keeps_or_turns_the_shared_mode(
    hass: HomeAssistant, freezer: Any, temps: float, other: float
) -> None:
    """The paused first room calls hard; only the other room's call can decide."""
    rig = await open_rig(hass, freezer, temps=(TARGET, other), window="on")
    try:
        await rig.later(OPEN_DELAY)
        await rig.temp(0, temps)
        before = rig.coord.current_shared_mode
        await rig.later(3600)
        want = {TARGET - 6: MODE_HEAT, TARGET + 6: MODE_COOL}.get(other, before)
        assert rig.coord.current_shared_mode == want
        assert rig.heads[0].mode == "off"
    finally:
        await rig.close()
