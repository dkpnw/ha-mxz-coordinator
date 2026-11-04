"""Cloud heads (beta): the HA-free record and setpoint math in cloud.py.

The oracles are written out by hand: the drive offset from
@mikepell007's blueprint (target + offset in heat, - offset in cool, 0 =
bare), step rounding half up, and the window rules in DESIGN.md §3.
"""

from __future__ import annotations

import pytest

from custom_components.mxz_coordinator.cloud import (
    CLOUD_CONFIRMED,
    CLOUD_NOT_CONTROLLABLE,
    CLOUD_PENDING,
    CLOUD_UNCONFIRMED,
    CloudHead,
    clamp,
    drive_setpoint,
    offset_in_unit,
    snap_to_step,
)


@pytest.mark.parametrize(
    ("act", "target", "offset", "expected"),
    [
        ("heat", 70.0, 2.0, 72.0),
        ("cool", 70.0, 2.0, 68.0),
        ("heat", 70.0, 0.0, 70.0),
        ("cool", 21.0, 1.0, 20.0),
        ("fan_only", 70.0, 2.0, 70.0),
        ("off", 70.0, 2.0, 70.0),
    ],
)
def test_drive_setpoint(act: str, target: float, offset: float, expected: float) -> None:
    assert drive_setpoint(act=act, target=target, offset=offset) == expected


@pytest.mark.parametrize(
    ("value", "step", "expected"),
    [
        (72.0, 1.0, 72.0),
        (72.4, 1.0, 72.0),
        (72.5, 1.0, 73.0),  # half up, not half to even (round() gives 72)
        (22.25, 0.5, 22.5),
        (22.2, 0.5, 22.0),
        (22.24, 0.5, 22.0),
        (71.3, 0.0, 71.3),  # no usable step: unchanged
        (71.3, -1.0, 71.3),
        (71.3, float("nan"), 71.3),
    ],
)
def test_snap_to_step(value: float, step: float, expected: float) -> None:
    assert snap_to_step(value, step) == expected


def test_clamp() -> None:
    assert clamp(90.0, (61.0, 86.0)) == 86.0
    assert clamp(50.0, (61.0, 86.0)) == 61.0
    assert clamp(70.0, (61.0, 86.0)) == 70.0
    assert clamp(90.0, None) == 90.0


def _head() -> CloudHead:
    return CloudHead(write_interval=60, confirm_wait=90, window_until=1090.0)


def test_startup_window_reads_every_report_as_possibly_stale() -> None:
    head = _head()  # opened at 1000 with a 90 s wait
    assert head.pending(1000.0)
    assert head.stale(1000.0, mode="heat")
    assert head.stale(1089.9, fan="high")
    assert head.status(1000.0) == CLOUD_PENDING
    # The window is half open: at its end nothing is stale any more.
    assert not head.pending(1090.0)
    assert not head.stale(1090.0, mode="heat")
    assert head.status(1090.0) == CLOUD_UNCONFIRMED


def test_a_batch_window_reads_only_older_states_as_stale() -> None:
    head = _head()
    head.sent(2000.0, ("heat", 72.0, "low"), "fan_only", "auto")
    assert head.window_until == 2090.0
    assert not head.startup
    assert head.intent == ("heat", 72.0, "low")
    # What the head showed before the batch is stale inside the window...
    assert head.stale(2010.0, mode="fan_only")
    assert head.stale(2010.0, fan="auto")
    # ...what the batch sent is not stale (it is the echo), and neither is
    # a value the head never had: that is a real change.
    assert not head.stale(2010.0, mode="heat")
    assert not head.stale(2010.0, fan="low")
    assert not head.stale(2010.0, mode="cool")
    assert not head.stale(2010.0, fan="high")
    # After the window nothing is stale.
    assert not head.stale(2090.0, mode="fan_only")


def test_an_unconfirmed_intent_becomes_an_older_state() -> None:
    head = _head()
    head.sent(2000.0, ("heat", 72.0, "low"), "fan_only", "auto")
    head.sent(2100.0, ("cool", 68.0, "quiet"), "fan_only", "auto")
    # The heat batch never showed: a late report of it is stale now.
    assert head.stale(2110.0, mode="heat")
    assert head.stale(2110.0, fan="low")
    assert head.older_modes == {"fan_only", "heat"}
    assert head.older_fans == {"auto", "low"}


def test_a_confirmed_intent_clears_the_older_states() -> None:
    head = _head()
    head.sent(2000.0, ("heat", 72.0, "low"), "fan_only", "auto")
    head.confirmed = True
    head.sent(2200.0, ("cool", 68.0, None), "heat", "low")
    assert head.older_modes == {"heat"}
    # A batch with no fan supersedes no fan: the "low" the head shows is
    # what it should keep showing (cloud review NIT 3).
    assert head.older_fans == set()
    assert not head.stale(2210.0, fan="low")
    assert head.stale(2210.0, mode="heat")
    assert not head.confirmed
    # A batch with a fan does supersede the one the head showed.
    head.confirmed = True
    head.sent(2250.0, ("cool", 68.0, "medium"), "cool", "low")
    assert head.older_fans == {"low"}
    assert head.stale(2260.0, fan="low")
    assert not head.stale(2260.0, fan="medium")
    # A batch's own values are never older states.
    head.sent(2300.0, ("heat", 72.0, None), "heat", None)
    assert "heat" not in head.older_modes


def test_a_batch_with_no_fan_keeps_an_unconfirmed_fan_current() -> None:
    """Cloud review NIT 3: the last fan sent stays what the head should show
    until a batch sends another, confirmed or not."""
    head = _head()
    head.sent(2000.0, ("heat", 72.0, "low"), "fan_only", "auto")
    head.sent(2070.0, ("heat", 74.0, None), "heat", "low")
    assert not head.stale(2080.0, fan="low")
    # The fan that batch replaced is still older: only the current one is spared.
    assert head.stale(2080.0, fan="auto")
    assert head.older_fans == {"auto"}
    # Negative: a later fan makes it older, though the head never showed it.
    head.sent(2150.0, ("heat", 74.0, "medium"), "heat", "auto")
    assert head.stale(2160.0, fan="low")
    assert not head.stale(2160.0, fan="medium")
    assert head.older_fans == {"auto", "low"}


def test_a_confirmation_forgets_the_last_fan() -> None:
    head = _head()
    head.sent(2000.0, ("heat", 72.0, "low"), "fan_only", "auto")
    head.confirmed = True
    head.sent(2200.0, ("heat", 74.0, None), "heat", "low")
    head.sent(2300.0, ("heat", 74.0, "medium"), "heat", None)
    # "low" was seen and confirmed, so no later batch can make it a late report.
    assert head.older_fans == set()


def test_write_interval_slot() -> None:
    head = _head()
    assert head.wait_for_slot(1000.0) == 0.0  # nothing sent yet
    head.sent(2000.0, ("heat", 72.0, None), "off", None)
    assert head.wait_for_slot(2000.0) == 60.0
    assert head.wait_for_slot(2045.0) == 15.0
    assert head.wait_for_slot(2060.0) == 0.0
    assert head.wait_for_slot(3000.0) == 0.0


def test_status() -> None:
    head = _head()
    head.sent(2000.0, ("heat", 72.0, None), "off", None)
    assert head.status(2050.0) == CLOUD_PENDING
    assert head.status(2090.0) == CLOUD_UNCONFIRMED
    head.confirmed = True
    assert head.status(2090.0) == CLOUD_CONFIRMED
    head.unresponsive = True
    assert head.status(2090.0) == CLOUD_NOT_CONTROLLABLE
    assert head.status(2050.0) == CLOUD_NOT_CONTROLLABLE  # out of control beats pending


@pytest.mark.parametrize(
    ("value", "set_in", "celsius", "expected"),
    [
        (4.0, "°F", True, 2.22),  # a difference: 5/9, no 32° shift
        (3.0, "°F", True, 1.67),
        (6.0, "°F", True, 3.33),  # the caller holds it to the °C range
        (0.0, "°F", True, 0.0),
        (2.5, "°C", False, 4.5),
        (1.5, "°C", False, 2.7),
        (3.0, "°C", False, 5.4),
        # Set in the unit in use: unchanged, either way.
        (4.0, "°F", False, 4.0),
        (2.5, "°C", True, 2.5),
        # No unit stored (or one MXZ never writes): the system unit, as before.
        (4.0, None, True, 4.0),
        (4.0, None, False, 4.0),
        (4.0, "K", True, 4.0),
        (4.0, "F", True, 4.0),
    ],
)
def test_offset_in_unit(value: float, set_in: object, celsius: bool, expected: float) -> None:
    assert offset_in_unit(value, set_in, celsius) == expected


def test_offset_in_unit_round_trips() -> None:
    """Saved again in °C and switched back, an offset comes back within one
    hundredth (the two places the °C value is kept to): 1 °F is 0.56 °C, then
    1.01 °F. A page never saved in between keeps the exact value it was set to."""
    for value in (0.5, 1.0, 2.5, 3.5, 4.0, 5.5, 6.0):
        there = offset_in_unit(value, "°F", True)
        assert abs(offset_in_unit(there, "°C", False) - value) <= 0.01 + 1e-9
