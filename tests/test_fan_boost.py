"""fan_latch.fan_boost, a local delivery's fan step in order, called directly: no pytest-homeassistant.

Each expectation is the rule as the fan hold states it: with boost off, no fan list, no ``auto``
in it or no speed showing, nothing is read or done; otherwise the head's report goes to the
echo memory first, then ``fan_step`` decides with the settings read after that, its hold is
kept, and a token to send is remembered before it is sent. The ladder and settings are those of
tests/test_fan_latch.py. Every value is invented.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.mxz_coordinator.const import FAN_AUTO, MODE_COOL, MODE_FAN_ONLY
from custom_components.mxz_coordinator.fan_latch import (
    EchoCaughtUp,
    FanEcho,
    FanLatch,
    FanLimits,
    KeepLatch,
    SendFan,
    WroteFan,
    fan_boost,
    fan_step,
)
from tests.test_fan_latch import LIMITS, MEDIUM

MODES = ["auto", "quiet", "low", "medium", "middle", "high"]


def _never(*_args: object) -> Any:
    raise AssertionError("read where it decides nothing")


def _driving() -> FanLatch:
    return FanLatch(echo=FanEcho("medium", "low"), held=False, rung=MEDIUM)


@pytest.mark.parametrize(
    ("boost", "modes", "observed"),
    [(False, MODES, "medium"), (True, None, "medium"), (True, ["low", "high"], "low"), (True, MODES, None)],
    ids=["boost_off", "no_fan_list", "no_auto", "no_speed"],
)
def test_nothing_is_read_or_done_without_a_fan_to_drive(boost, modes, observed) -> None:
    steps = fan_boost(_never, observed, MODE_COOL, 4.0, boost=boost, modes=modes, limits=_never)
    assert steps == ()


def test_a_room_far_off_target_climbs_a_rung_kept_then_remembered_then_sent() -> None:
    latch = _driving()
    expected, token = fan_step(_driving(), "medium", MODE_COOL, 3.2, modes=MODES, limits=LIMITS)
    assert token == "middle"
    steps = fan_boost(lambda: latch, "medium", MODE_COOL, 3.2, boost=True, modes=MODES, limits=lambda: LIMITS)
    kept, wrote, sent = steps[-3:]
    assert (wrote, sent) == (WroteFan("middle"), SendFan("middle"))
    assert isinstance(kept, KeepLatch)
    assert (kept.latch.held, kept.latch.rung) == (expected.held, expected.rung)
    assert (kept.latch.echo.last, kept.latch.echo.prior) == ("medium", "low")  # wrote comes after


def test_a_held_head_keeps_its_hold_and_is_sent_nothing() -> None:
    held = FanLatch(echo=FanEcho("medium", "low"), held=True)
    steps = fan_boost(lambda: held, "quiet", MODE_COOL, 4.0, boost=True, modes=MODES, limits=lambda: LIMITS)
    assert [type(step) for step in steps if not isinstance(step, EchoCaughtUp)] == [KeepLatch]
    (kept,) = [step for step in steps if isinstance(step, KeepLatch)]
    assert kept.latch.held is True


def test_a_speed_the_head_already_shows_is_not_sent_again() -> None:
    latch = FanLatch(echo=FanEcho(FAN_AUTO, FAN_AUTO), held=False)
    steps = fan_boost(lambda: latch, FAN_AUTO, MODE_FAN_ONLY, 0.0, boost=True, modes=MODES, limits=lambda: LIMITS)
    assert not any(isinstance(step, (WroteFan, SendFan)) for step in steps)


def test_the_report_reaches_the_echo_before_the_settings_are_read() -> None:
    echo = FanEcho("high", "medium", ("low", "medium", "high"))
    latch = FanLatch(echo=echo, held=False, rung=MEDIUM)
    order: list[str] = []

    def limits() -> FanLimits:
        order.append(f"limits after echo at {echo.last}/{echo.prior}/{list(echo.older)}")
        return LIMITS

    # The head shows "high", MXZ's last write: the writes before it are behind it for good.
    steps = fan_boost(lambda: latch, "high", MODE_COOL, 2.2, boost=True, modes=MODES, limits=limits)
    assert order == ["limits after echo at high/medium/['high']"]
    assert steps[0] == EchoCaughtUp()


def test_no_echo_change_publishes_nothing_first() -> None:
    latch = _driving()
    steps = fan_boost(lambda: latch, "medium", MODE_COOL, 2.2, boost=True, modes=MODES, limits=lambda: LIMITS)
    assert EchoCaughtUp() not in steps
    assert isinstance(steps[0], KeepLatch)
