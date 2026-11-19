"""DeliverySteps.order and fan_latch.fan_read, called directly: no pytest-homeassistant.

The order is the one a local head's delivery runs in: the coil-dry note, the fan's hand-back to
auto only on the way to off, the mode and setpoints, then the fan step only where the delivery
has fan machinery (not a fixed standby park or a floor). ``fan_read`` is the fan hold read on a
head MXZ sends no fan: it reads, never writes, and reads nothing it cannot. Each expectation is
the rule as stated, worked by hand. Every head and value is invented.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.mxz_coordinator.const import (
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN_ONLY,
    MODE_OFF,
)
from custom_components.mxz_coordinator.fan_latch import (
    FanEcho,
    FanLatch,
    fan_read,
    observe_fan,
)
from custom_components.mxz_coordinator.planner import DeliverySteps, delivery_steps
from tests.test_fan_latch import LIMITS

MODES = ["auto", "low", "medium", "high"]


def _steps(act: str, engage: str | None, **change: object) -> DeliverySteps:
    kwargs: dict[str, object] = {
        "idle_action": IDLE_ACTION_OFF, "eco": False, "inhibited": False, "head_state": MODE_COOL,
    } | change
    return delivery_steps(act, engage, **kwargs)  # type: ignore[arg-type]


def test_a_run_is_the_note_the_setpoints_then_the_fan() -> None:
    assert _steps(MODE_COOL, MODE_COOL).order == ("note", "setpoints", "fan")


def test_an_off_from_a_running_head_hands_the_fan_back_before_the_mode() -> None:
    assert _steps(MODE_OFF, "satisfied").order == ("note", "handback", "setpoints", "fan")


@pytest.mark.parametrize(
    "change",
    [{"head_state": MODE_OFF}, {"idle_action": IDLE_ACTION_FAN_ONLY}, {"eco": True}, {"inhibited": True}],
    ids=["head_off", "idle_fan_only", "eco", "standby"],
)
def test_no_hand_back_where_the_rule_gives_none(change) -> None:
    assert _steps(MODE_OFF, "satisfied", **change).order == ("note", "setpoints", "fan")


def test_a_fixed_park_or_a_floor_has_no_fan_step() -> None:
    assert _steps(MODE_OFF, None).order == ("note", "setpoints")


def test_the_order_and_the_fan_step_agree() -> None:
    # The fan step after a hand-back drops the rung; otherwise it is the boost step, and a
    # hold read when it runs takes it away.
    handback = _steps(MODE_OFF, "satisfied")
    assert handback.fan(inhibited=True) == "drop_rung"
    run = _steps(MODE_COOL, MODE_COOL)
    assert run.fan(inhibited=False) == "boost" and run.fan(inhibited=True) is None
    assert _steps(MODE_OFF, None).fan(inhibited=False) is None


def _latch(**change: object) -> FanLatch:
    return replace(FanLatch(echo=FanEcho(last="low", prior="auto")), **change)  # type: ignore[arg-type]


@pytest.mark.parametrize("act", [MODE_OFF, MODE_DRY])
def test_a_speed_picked_on_a_head_mxz_sends_no_fan_is_held(act) -> None:
    read = fan_read(_latch(), "high", act, modes=MODES, limits=LIMITS)
    assert read.held is True
    # The same read as observe_fan with no delta, which is all it is.
    direct = observe_fan(_latch(), "high", act, 0.0, LIMITS)
    assert (read.held, read.rung, read.echo.last, read.echo.prior) == (
        direct.held, direct.rung, direct.echo.last, direct.echo.prior,
    )


def test_mxzs_own_last_write_shown_is_not_a_pick() -> None:
    assert fan_read(_latch(), "low", MODE_OFF, modes=MODES, limits=LIMITS).held is not True


@pytest.mark.parametrize(
    ("observed", "modes", "boost"),
    [("high", MODES, False), ("high", None, True), ("high", ["low", "high"], True), (None, MODES, True)],
    ids=["boost_off", "no_fan_list", "no_auto", "no_speed"],
)
def test_nothing_is_read_where_nothing_can_be(observed, modes, boost) -> None:
    latch = _latch()
    assert fan_read(latch, observed, MODE_OFF, modes=modes, limits=replace(LIMITS, boost=boost)) is latch


def test_a_read_keeps_the_rung() -> None:
    assert fan_read(_latch(rung=2), "low", MODE_FAN_ONLY, modes=MODES, limits=LIMITS).rung == 2
