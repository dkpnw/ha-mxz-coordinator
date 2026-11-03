"""The door/window pause decision, on the real logic.opening_state.

Every edge the design names: a short open, a re-open while closing, no reading
(unavailable/unknown/missing) in each state, 0 s delays, and the override.
The expected states are written out case by case below (the oracle), never
computed from the function under test. No Home Assistant needed.
"""

from __future__ import annotations

import itertools

import pytest

from custom_components.mxz_coordinator.const import (
    OPENING_CLOSED,
    OPENING_CLOSING,
    OPENING_OPENING,
    OPENING_OVERRIDDEN,
    OPENING_PAUSED,
)
from custom_components.mxz_coordinator.logic import (
    CLOSED_READ,
    OPEN_READ,
    opening_due,
    opening_reading,
    opening_state,
)

OPEN_DELAY = 60.0
CLOSE_DELAY = 120.0
STATES = (OPENING_CLOSED, OPENING_OPENING, OPENING_PAUSED, OPENING_CLOSING, OPENING_OVERRIDDEN)


def _state(raw, held, prior, override=False, *, open_delay=OPEN_DELAY, close_delay=CLOSE_DELAY):
    """The state after ``raw`` has been read for ``held`` seconds."""
    return opening_state(
        raw=raw, since=1000.0, now=1000.0 + held,
        open_delay=open_delay, close_delay=close_delay, prior=prior, override=override,
    )


@pytest.mark.parametrize(
    ("state", "reading"),
    [
        ("on", OPEN_READ),
        ("off", CLOSED_READ),
        ("unavailable", None),
        ("unknown", None),
        (None, None),  # the entity is missing
        ("open", None),  # only on/off are readings
        ("On", None),
        ("", None),
    ],
)
def test_reading_is_on_or_off_only(state, reading) -> None:
    assert opening_reading(state) == reading


@pytest.mark.parametrize(
    ("raw", "held", "prior", "want"),
    [
        # A short open: opening, and a close then is closed with no pause.
        (OPEN_READ, 0, OPENING_CLOSED, OPENING_OPENING),
        (OPEN_READ, 59.9, OPENING_OPENING, OPENING_OPENING),
        (CLOSED_READ, 0, OPENING_OPENING, OPENING_CLOSED),
        # Open for the open delay pauses.
        (OPEN_READ, 60, OPENING_OPENING, OPENING_PAUSED),
        (OPEN_READ, 60, OPENING_CLOSED, OPENING_PAUSED),
        (OPEN_READ, 3600, OPENING_CLOSED, OPENING_PAUSED),
        # Paused and still open: paused, however long.
        (OPEN_READ, 0, OPENING_PAUSED, OPENING_PAUSED),
        # Closed while paused: closing until the close delay, then closed.
        (CLOSED_READ, 0, OPENING_PAUSED, OPENING_CLOSING),
        (CLOSED_READ, 119.9, OPENING_CLOSING, OPENING_CLOSING),
        (CLOSED_READ, 120, OPENING_CLOSING, OPENING_CLOSED),
        (CLOSED_READ, 120, OPENING_PAUSED, OPENING_CLOSED),
        # Re-open while closing: paused again at once (the close delay starts
        # over at the next closed reading, which has a new `since`).
        (OPEN_READ, 0, OPENING_CLOSING, OPENING_PAUSED),
        # Closed and staying closed.
        (CLOSED_READ, 0, OPENING_CLOSED, OPENING_CLOSED),
        (CLOSED_READ, 9999, OPENING_CLOSED, OPENING_CLOSED),
        # No reading never starts a pause ...
        (None, 0, OPENING_CLOSED, OPENING_CLOSED),
        (None, 9999, OPENING_CLOSED, OPENING_CLOSED),
        (None, 9999, OPENING_OPENING, OPENING_CLOSED),
        # ... and never ends one: it is not proof of closed.
        (None, 9999, OPENING_PAUSED, OPENING_PAUSED),
        (None, 9999, OPENING_CLOSING, OPENING_PAUSED),
        # An override that has ended (switch turned off) counts the opening again.
        (OPEN_READ, 60, OPENING_OVERRIDDEN, OPENING_PAUSED),
        (OPEN_READ, 10, OPENING_OVERRIDDEN, OPENING_OPENING),
        (CLOSED_READ, 0, OPENING_OVERRIDDEN, OPENING_CLOSED),
        (None, 9999, OPENING_OVERRIDDEN, OPENING_CLOSED),
    ],
)
def test_each_edge(raw, held, prior, want) -> None:
    assert _state(raw, held, prior) == want


@pytest.mark.parametrize(
    ("raw", "prior", "want"),
    [
        # An override never lets the room pause while the opening isn't closed.
        (OPEN_READ, OPENING_CLOSED, OPENING_OVERRIDDEN),
        (OPEN_READ, OPENING_OPENING, OPENING_OVERRIDDEN),
        (OPEN_READ, OPENING_PAUSED, OPENING_OVERRIDDEN),
        (OPEN_READ, OPENING_CLOSING, OPENING_OVERRIDDEN),
        (OPEN_READ, OPENING_OVERRIDDEN, OPENING_OVERRIDDEN),
        (None, OPENING_PAUSED, OPENING_OVERRIDDEN),
        (None, OPENING_CLOSING, OPENING_OVERRIDDEN),
        (None, OPENING_OVERRIDDEN, OPENING_OVERRIDDEN),
        # Nothing to override: no pause, and no reading to start one.
        (None, OPENING_CLOSED, OPENING_CLOSED),
        (None, OPENING_OPENING, OPENING_CLOSED),
        # A closed reading ends it, whatever came before: no close delay.
        (CLOSED_READ, OPENING_OVERRIDDEN, OPENING_CLOSED),
        (CLOSED_READ, OPENING_PAUSED, OPENING_CLOSED),
        (CLOSED_READ, OPENING_CLOSING, OPENING_CLOSED),
        (CLOSED_READ, OPENING_CLOSED, OPENING_CLOSED),
    ],
)
def test_override(raw, prior, want) -> None:
    for held in (0, 60, 9999):
        assert _state(raw, held, prior, override=True) == want


def test_zero_delays_act_at_once() -> None:
    assert _state(OPEN_READ, 0, OPENING_CLOSED, open_delay=0) == OPENING_PAUSED
    assert _state(CLOSED_READ, 0, OPENING_PAUSED, close_delay=0) == OPENING_CLOSED
    # Even at 0 s, no reading never pauses.
    assert _state(None, 0, OPENING_CLOSED, open_delay=0) == OPENING_CLOSED


def test_every_input_gives_a_state_and_no_reading_never_pauses() -> None:
    """Over a grid of every input: a state always, and the two safety rules hold."""
    for raw, prior, override, held, delay in itertools.product(
        (OPEN_READ, CLOSED_READ, None), STATES, (False, True), (0, 30, 60, 120, 9999), (0, 60, 120),
    ):
        got = _state(raw, held, prior, override, open_delay=delay, close_delay=delay)
        assert got in STATES
        if raw is None and prior not in (OPENING_PAUSED, OPENING_CLOSING):
            assert got not in (OPENING_PAUSED, OPENING_CLOSING)  # never starts a pause
        if override:
            assert got not in (OPENING_PAUSED, OPENING_CLOSING)  # never paused while overridden


@pytest.mark.parametrize(
    ("state", "want"),
    [
        (OPENING_OPENING, 1060.0),  # the end of the open delay
        (OPENING_CLOSING, 1120.0),  # the resume time
        (OPENING_CLOSED, None),
        (OPENING_PAUSED, None),  # an open window has no resume time
        (OPENING_OVERRIDDEN, None),
    ],
)
def test_due(state, want) -> None:
    assert opening_due(
        state=state, since=1000.0, open_delay=OPEN_DELAY, close_delay=CLOSE_DELAY,
    ) == want
