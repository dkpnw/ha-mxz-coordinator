"""fan_latch.py's decisions, called directly: no Home Assistant, no pytest-homeassistant.

Each expectation is worked out from the rule it states (a speed MXZ never wrote is a person's
pick and holds; one it wrote is its own echo; only "auto" or Fan auto hands a hold back; a
start's truth is read against the first speed the head shows), not read back from the code.
The ladder is quiet, low, medium, middle, high; boost steps up a rung at 1, 2, 3 and 4 °F off
target and down below 0.5, 1.5, 2.5 and 3.5. Every value is invented.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.mxz_coordinator.const import (
    FAN_AUTO,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
)
from custom_components.mxz_coordinator.fan_latch import (
    FanEcho,
    FanLatch,
    FanLimits,
    adopt,
    fan_auto,
    fan_record,
    fan_step,
    hold_at,
    observe_fan,
    reconcile_restored_hold,
    restored,
    seed_is_own_idle,
    seed_matches_boost,
)

QUIET, LOW, MEDIUM, MIDDLE, HIGH = 0, 1, 2, 3, 4
LIMITS = FanLimits(
    boost=True,
    ceiling=HIGH,
    up_at=(1.0, 2.0, 3.0, 4.0),
    down_at=(0.5, 1.5, 2.5, 3.5),
    eco_idle=False,
    idle_action=IDLE_ACTION_FAN_ONLY,
    resumed=True,
    age=timedelta(days=30),
)


def memory(latch: FanLatch) -> tuple[str | None, str | None, list[str]]:
    return latch.echo.last, latch.echo.prior, list(latch.echo.older)


def fresh() -> FanLatch:
    return FanLatch(echo=FanEcho())


# -- a speed the head shows, with no record and no memory (a seed) ------------


def test_a_seed_at_auto_is_not_held_and_stamps_auto_as_the_baseline() -> None:
    latch = observe_fan(fresh(), FAN_AUTO, MODE_COOL, 5.0, LIMITS)
    assert latch.held is False
    assert memory(latch) == (FAN_AUTO, FAN_AUTO, [])


@pytest.mark.parametrize(("observed", "rung", "delta"), [
    ("middle", MIDDLE, 2.7),  # a room easing down: middle holds above 2.5
    ("medium", MEDIUM, 2.7),  # a cold read at 2.7 picks medium
    ("high", HIGH, 9.0),
])
def test_a_seed_at_a_rung_boost_would_stay_on_is_its_own_speed(observed, rung, delta) -> None:
    latch = observe_fan(fresh(), observed, MODE_COOL, delta, LIMITS)
    assert latch.held is False
    assert latch.rung == rung
    assert memory(latch) == (observed, observed, [])


@pytest.mark.parametrize(("observed", "act", "delta", "limits"), [
    ("high", MODE_COOL, 2.7, LIMITS),  # boost would step down off high
    ("quiet", MODE_HEAT, 2.7, LIMITS),  # boost would step up off quiet
    ("medium", MODE_COOL, 2.7, replace(LIMITS, eco_idle=True)),
    ("medium", MODE_COOL, 2.7, replace(LIMITS, ceiling=LOW)),  # above the ceiling
    ("medium", MODE_OFF, 2.7, LIMITS),
    ("turbo", MODE_COOL, 9.0, LIMITS),  # not a ladder speed
])
def test_any_other_seed_is_a_pick_from_before_and_holds(observed, act, delta, limits) -> None:
    latch = observe_fan(fresh(), observed, act, delta, limits)
    assert latch.held is True
    assert memory(latch) == (observed, observed, [])
    assert latch.rung is None


def test_a_fan_only_idle_on_an_established_entry_is_its_own() -> None:
    latch = observe_fan(fresh(), "low", MODE_FAN_ONLY, 0.0, LIMITS)
    assert latch.held is False
    assert memory(latch) == ("low", "low", [])
    assert seed_is_own_idle("low", MODE_FAN_ONLY, replace(LIMITS, age=timedelta(seconds=600)))


@pytest.mark.parametrize("limits", [
    replace(LIMITS, age=timedelta(seconds=599)),
    replace(LIMITS, age=None),
    replace(LIMITS, resumed=False),
    replace(LIMITS, idle_action=IDLE_ACTION_OFF),
    replace(LIMITS, ceiling=QUIET),
], ids=["young", "no_age", "not_resumed", "other_idle", "above_ceiling"])
def test_a_fan_only_idle_seed_holds_unless_the_entry_is_established(limits) -> None:
    assert seed_is_own_idle("low", MODE_FAN_ONLY, limits) is False
    assert observe_fan(fresh(), "low", MODE_FAN_ONLY, 0.0, limits).held is True


def test_seed_matches_boost_names_the_rung_or_none() -> None:
    assert seed_matches_boost("middle", MODE_COOL, 2.7, LIMITS) == MIDDLE
    assert seed_matches_boost("middle", MODE_COOL, 2.4, LIMITS) is None
    assert seed_matches_boost("middle", MODE_FAN_ONLY, 2.7, LIMITS) is None


# -- a speed the head shows, with memory --------------------------------------


def driving(last: str = "medium", prior: str = "low", older: tuple[str, ...] = ()) -> FanLatch:
    return FanLatch(echo=FanEcho(last, prior, older), held=False, rung=MEDIUM)


@pytest.mark.parametrize("observed", ["medium", "low"])
def test_one_of_the_last_two_writes_is_mxzs_echo_and_changes_nothing(observed) -> None:
    latch = driving()
    assert observe_fan(latch, observed, MODE_COOL, 2.2, LIMITS) is latch


def test_an_older_write_a_lagging_head_still_shows_is_mxzs_echo() -> None:
    latch = driving("low", "medium", ("high", "medium", "low"))
    assert observe_fan(latch, "high", MODE_COOL, 1.2, LIMITS) is latch


@pytest.mark.parametrize("observed", ["quiet", "middle", "high"])
def test_a_speed_mxz_never_wrote_is_a_pick_and_holds_at_it(observed) -> None:
    latch = observe_fan(driving(), observed, MODE_COOL, 2.2, LIMITS)
    assert latch.held is True
    assert memory(latch) == (observed, observed, [])
    assert latch.rung == MEDIUM


def test_a_standing_hold_stays_through_any_drift() -> None:
    held = hold_at(driving(), "quiet")
    for delta in (0.0, 2.2, 9.0):
        assert observe_fan(held, "quiet", MODE_COOL, delta, LIMITS) is held


def test_a_hold_moved_at_the_remote_holds_at_the_new_speed() -> None:
    latch = observe_fan(hold_at(driving(), "quiet"), "high", MODE_COOL, 9.0, LIMITS)
    assert latch.held is True
    assert memory(latch) == ("high", "high", [])


def test_auto_releases_a_hold_and_keeps_the_memory() -> None:
    held = hold_at(driving(), "quiet")
    latch = observe_fan(held, FAN_AUTO, MODE_COOL, 2.2, LIMITS)
    assert latch.held is False
    assert latch.echo is held.echo


def test_a_waiting_fan_auto_on_adopts_the_first_speed_even_above_the_ceiling() -> None:
    waiting = FanLatch(echo=FanEcho("low", "quiet"), held=False, restored=True, on_pending=True)
    latch = observe_fan(waiting, "high", MODE_COOL, 0.0, replace(LIMITS, ceiling=MEDIUM))
    assert latch.held is False
    assert latch.on_pending is False
    assert latch.restored is None
    assert memory(latch) == ("high", "low", [])
    assert latch.rung == MEDIUM


def test_a_decision_never_changes_the_latch_it_was_given() -> None:
    latch = driving("low", "medium", ("high", "medium", "low"))
    before = memory(latch)
    observe_fan(latch, "quiet", MODE_COOL, 2.2, LIMITS)
    observe_fan(latch, FAN_AUTO, MODE_COOL, 2.2, LIMITS)
    fan_auto(latch, "quiet", True, HIGH)
    assert memory(latch) == before
    assert latch.held is False


# -- a start's record, read against the first speed the head shows -----------


def pending(held: bool, echo: FanEcho | None = None) -> FanLatch:
    return FanLatch(echo=echo or FanEcho(), restored=held)


def test_a_restored_hold_is_held_at_whatever_speed_the_head_shows() -> None:
    for observed in ("quiet", "high", "turbo"):
        latch = observe_fan(pending(True), observed, MODE_COOL, 9.0, LIMITS)
        assert latch.held is True
        assert latch.restored is None
        assert memory(latch) == (observed, observed, [])


@pytest.mark.parametrize("held", [True, False])
def test_a_restored_truth_is_released_by_auto_and_keeps_its_memory(held) -> None:
    echo = FanEcho("medium", "low")
    latch = observe_fan(pending(held, echo), FAN_AUTO, MODE_COOL, 2.2, LIMITS)
    assert latch.held is False
    assert latch.echo is echo
    latch = observe_fan(pending(held), FAN_AUTO, MODE_COOL, 2.2, LIMITS)
    assert memory(latch) == (FAN_AUTO, FAN_AUTO, [])


def test_not_held_a_speed_boost_could_never_write_holds_even_if_remembered() -> None:
    limits = replace(LIMITS, ceiling=MEDIUM)
    latch = observe_fan(pending(False, FanEcho("high", "medium")), "high", MODE_COOL, 9.0, limits)
    assert latch.held is True


def test_not_held_with_no_memory_an_in_ceiling_rung_is_boosts_and_resumes_from_it() -> None:
    latch = observe_fan(pending(False), "middle", MODE_COOL, 0.6, LIMITS)
    assert latch.held is False
    assert latch.rung == MIDDLE
    assert memory(latch) == ("middle", "middle", [])


def test_not_held_a_remembered_rung_is_boosts_and_keeps_the_memory() -> None:
    echo = FanEcho("medium", "low")
    latch = observe_fan(pending(False, echo), "low", MODE_HEAT, 1.2, LIMITS)
    assert latch.held is False
    assert latch.echo is echo
    assert latch.rung == LOW


def test_not_held_an_unremembered_rung_holds_unless_the_room_idles_in_fan_only() -> None:
    echo = FanEcho("low", "quiet")
    assert observe_fan(pending(False, echo), "high", MODE_COOL, 9.0, LIMITS).held is True
    idle = observe_fan(pending(False, echo), "high", MODE_FAN_ONLY, 0.0, LIMITS)
    assert idle.held is False
    assert memory(idle) == ("high", "high", [])
    assert idle.rung is None
    other = replace(LIMITS, idle_action=IDLE_ACTION_OFF)
    assert observe_fan(pending(False, echo), "high", MODE_FAN_ONLY, 0.0, other).held is True


def test_reconcile_is_what_observe_fan_does_with_a_restored_truth() -> None:
    echo = FanEcho("low", "quiet")
    direct = reconcile_restored_hold(FanLatch(echo=echo), "medium", False, MODE_COOL, LIMITS)
    via = observe_fan(pending(False, echo), "medium", MODE_COOL, 9.0, LIMITS)
    assert (direct.held, memory(direct), direct.rung) == (via.held, memory(via), via.rung)


def test_restored_keeps_only_speeds_boost_could_write() -> None:
    latch = restored(
        fresh(), False, last="high", prior="middle", older=("quiet", "turbo", "medium"),
        on_pending=False, ceiling=MIDDLE,
    )
    assert latch.restored is False
    assert memory(latch) == (FAN_AUTO, "middle", ["quiet", "medium", "middle", FAN_AUTO])


def test_restored_with_no_prior_uses_the_last_for_both() -> None:
    latch = restored(fresh(), False, last="low", prior=None, older=(), on_pending=False, ceiling=HIGH)
    assert memory(latch) == ("low", "low", [])


def test_a_restored_hold_carries_no_memory_and_no_waiting_fan_auto_on() -> None:
    before = FanLatch(echo=FanEcho("low", "quiet"), held=True, rung=LOW)
    latch = restored(before, True, last="low", prior="quiet", older=(), on_pending=True, ceiling=HIGH)
    assert memory(latch) == (None, None, [])
    assert (latch.restored, latch.on_pending) == (True, False)
    assert (latch.held, latch.rung) == (True, LOW)  # a restore leaves the live latch alone


def test_a_not_held_record_can_carry_a_waiting_fan_auto_on() -> None:
    latch = restored(fresh(), False, last=None, prior=None, older=(), on_pending=True, ceiling=HIGH)
    assert latch.on_pending is True
    assert memory(latch) == (None, None, [])


def test_a_pick_survives_two_restarts_through_the_same_functions() -> None:
    """What a start reads back is ``held``; the lab and FanHold feed it the same way."""
    latch = observe_fan(driving(), "quiet", MODE_COOL, 2.2, LIMITS)
    for _ in range(2):
        latch = restored(
            FanLatch(echo=FanEcho()), latch.held is True, last=latch.echo.last,
            prior=latch.echo.prior, older=(), on_pending=False, ceiling=HIGH,
        )
        latch = observe_fan(latch, "quiet", MODE_COOL, 2.2, LIMITS)
        assert latch.held is True
    latch = observe_fan(latch, "quiet", MODE_COOL, 9.0, LIMITS)
    assert latch.held is True


# -- Fan auto -----------------------------------------------------------------


def test_fan_auto_on_adopts_the_speed_showing_and_resumes_from_the_ceiling() -> None:
    held = replace(hold_at(driving(), "quiet"), restored=True)
    latch, acted = fan_auto(held, "quiet", True, MIDDLE)
    assert acted is True
    assert (latch.held, latch.restored, latch.rung) == (False, None, MIDDLE)
    assert memory(latch) == ("quiet", "quiet", [])
    assert observe_fan(latch, "quiet", MODE_COOL, 2.2, LIMITS).held is False


def test_fan_auto_on_with_no_speed_waits_for_one() -> None:
    latch, acted = fan_auto(hold_at(driving(), "quiet"), None, True, HIGH)
    assert acted is True
    assert (latch.held, latch.on_pending) == (False, True)


def test_fan_auto_off_at_a_speed_ends_a_waiting_on_and_a_restored_truth() -> None:
    """The hold a person asks for replaces both: neither is left to act on a later report."""
    waiting = replace(driving(), on_pending=True, restored=False)
    latch, acted = fan_auto(waiting, "quiet", False, HIGH)
    assert acted is True
    assert (latch.held, latch.on_pending, latch.restored) == (True, False, None)


def test_fan_auto_off_holds_the_speed_showing() -> None:
    latch, acted = fan_auto(driving(), "medium", False, HIGH)
    assert acted is True
    assert latch.held is True
    assert memory(latch) == ("medium", "medium", [])
    latch, _ = fan_auto(driving(), "middle", False, HIGH)
    assert memory(latch) == ("middle", "medium", [])
    assert observe_fan(latch, "middle", MODE_COOL, 9.0, LIMITS).held is True


def test_fan_auto_off_at_auto_is_ignored_and_drops_a_restored_truth() -> None:
    latch, acted = fan_auto(pending(True), FAN_AUTO, False, HIGH)
    assert acted is False
    assert latch.restored is None
    assert latch.held is None


def test_fan_auto_off_with_no_speed_cancels_a_waiting_on_or_does_nothing() -> None:
    waiting = replace(driving(), on_pending=True)
    latch, acted = fan_auto(waiting, None, False, HIGH)
    assert acted is True
    assert (latch.held, latch.restored, latch.on_pending) == (True, True, False)
    latch, acted = fan_auto(driving(), None, False, HIGH)
    assert acted is False
    assert memory(latch) == memory(driving())
    assert (latch.held, latch.restored, latch.on_pending) == (False, None, False)


def test_adopt_rolls_the_last_write_into_prior() -> None:
    assert memory(adopt(driving(), "high", HIGH)) == ("high", "medium", [])
    assert memory(adopt(fresh(), "high", HIGH)) == ("high", "high", [])


# -- the echo memory a report and a write move --------------------------------


def test_a_report_drops_the_writes_before_it_and_a_rewrite_rolls_nothing() -> None:
    echo = FanEcho("high", FAN_AUTO)
    echo.wrote("medium")
    echo.wrote("medium")
    echo.wrote("low")
    assert (echo.last, echo.prior, list(echo.older)) == ("low", "medium", [FAN_AUTO, "high", "medium", "low"])
    assert echo.caught_up("medium") is False  # a report of prior drops nothing
    assert echo.caught_up("high") is True
    assert list(echo.older) == ["high", "medium", "low"]
    assert echo.own("high") is True  # the head may still show the writes after it
    assert echo.caught_up("low") is True
    assert list(echo.older) == ["low"]
    assert echo.own("high") is False
    assert echo.own("medium") is True


# -- a delivery's boost step -------------------------------------------------

LISTED = (FAN_AUTO, "quiet", "low", "medium", "middle", "high")


def step(
    latch: FanLatch, observed: str | None, act: str, delta: float,
    modes: tuple[str, ...] | None = LISTED, limits: FanLimits = LIMITS,
) -> tuple[FanLatch, str | None]:
    return fan_step(latch, observed, act, delta, modes=modes, limits=limits)


@pytest.mark.parametrize(
    ("observed", "modes", "limits"),
    [
        ("quiet", LISTED, replace(LIMITS, boost=False)),
        ("quiet", None, LIMITS),
        ("quiet", (), LIMITS),
        ("quiet", ("quiet", "low", "high"), LIMITS),
        (None, LISTED, LIMITS),
    ],
    ids=["boost_off", "no_fan_list", "empty_fan_list", "no_auto", "no_speed"],
)
def test_nothing_is_read_or_written_without_boost_a_fan_list_with_auto_or_a_speed(
    observed, modes, limits,
) -> None:
    latch = driving()
    assert step(latch, observed, MODE_COOL, 6.0, modes, limits) == (latch, None)


def test_a_held_head_gets_no_write_at_any_delta() -> None:
    latch, token = step(driving(), "quiet", MODE_COOL, 6.0)
    assert token is None
    assert latch.held is True
    assert memory(latch) == ("quiet", "quiet", [])
    assert latch.rung == MEDIUM  # a hold doesn't move the rung


@pytest.mark.parametrize("act", [MODE_COOL, MODE_HEAT])
def test_a_run_gets_the_rung_for_its_delta_from_the_rung_it_keeps(act) -> None:
    latch, token = step(driving(), "medium", act, 4.0)  # 4 °F: up from medium to high
    assert (token, latch.rung, latch.held) == ("high", HIGH, False)
    latch, token = step(replace(driving(), rung=MIDDLE), "medium", act, 2.7)
    assert (token, latch.rung) == ("middle", MIDDLE)  # 2.7 is above middle's 2.5 down edge
    latch, token = step(replace(driving(), rung=None), "medium", act, 1.2)
    assert (token, latch.rung) == ("low", LOW)  # none kept: up from the bottom rung


def test_the_echo_memory_is_not_rolled_by_the_step() -> None:
    latch, token = step(driving(), "medium", MODE_COOL, 6.0)
    assert token == "high"
    assert memory(latch) == ("medium", "low", [])


def test_a_rung_the_head_already_shows_is_not_written_and_is_kept() -> None:
    latch, token = step(driving(), "medium", MODE_COOL, 2.2)
    assert (token, latch.rung) == (None, MEDIUM)


def test_a_rung_the_head_doesnt_list_is_not_written_but_is_kept() -> None:
    latch, token = step(driving(), "medium", MODE_COOL, 6.0, (FAN_AUTO, "quiet", "medium"))
    assert (token, latch.rung) == (None, HIGH)


@pytest.mark.parametrize(
    ("act", "limits"),
    [(MODE_FAN_ONLY, LIMITS), (MODE_COOL, replace(LIMITS, eco_idle=True)),
     (MODE_OFF, replace(LIMITS, eco_idle=True))],
    ids=["fan_only", "eco_idle_cool", "eco_idle_off"],
)
def test_fan_only_or_eco_idle_hands_back_to_auto_and_drops_the_rung(act, limits) -> None:
    latch, token = step(driving(), "medium", act, 6.0, limits=limits)
    assert (token, latch.rung) == (FAN_AUTO, None)


def test_auto_already_showing_is_not_written_again() -> None:
    latch, token = step(driving(), FAN_AUTO, MODE_FAN_ONLY, 0.0)
    assert (token, latch.rung, latch.held) == (None, None, False)


def test_off_drops_the_rung_and_writes_nothing() -> None:
    latch, token = step(driving(), "medium", MODE_OFF, 6.0)
    assert (token, latch.rung) == (None, None)


def test_a_step_never_changes_the_latch_it_was_given() -> None:
    latch = driving()
    before = (memory(latch), latch.held, latch.rung)
    step(latch, "medium", MODE_COOL, 6.0)
    assert (memory(latch), latch.held, latch.rung) == before


# -- what a stop saves ---------------------------------------------------------


@pytest.mark.parametrize(
    ("held", "restored", "holds"),
    [(None, None, False), (False, None, False), (True, None, True),
     (False, True, True), (True, False, False), (None, True, True)],
)
def test_a_start_truth_not_yet_read_wins_over_the_latch(held, restored, holds) -> None:
    latch = FanLatch(echo=FanEcho(), held=held, restored=restored)
    assert latch.holds is holds
    assert fan_record(latch, None).held is holds


def test_the_record_carries_the_last_two_and_a_waiting_fan_auto_on() -> None:
    latch = replace(driving(), on_pending=True)
    record = fan_record(latch, "medium")
    assert (record.held, record.last, record.prior, record.on_pending, record.older) == (
        False, "medium", "low", True, ()
    )


def test_the_older_writes_are_kept_in_order_without_the_last_two_or_a_leading_auto() -> None:
    latch = driving("low", "medium", (FAN_AUTO, "high", "medium", "low"))
    assert fan_record(latch, "high").older == ("high",)
    assert fan_record(latch, None).older == ("high",)


def test_a_later_auto_is_kept_only_the_leading_one_goes() -> None:
    latch = driving("low", "medium", (FAN_AUTO, "high", FAN_AUTO, "medium", "low"))
    assert fan_record(latch, "high").older == ("high", FAN_AUTO)


def test_a_head_showing_the_last_write_with_nothing_older_saves_no_older_writes() -> None:
    latch = driving("medium", "low", ("low", "medium"))
    assert fan_record(latch, "medium").older == ()
    # Showing something else, the same memory keeps its order (here nothing is left after
    # the last two are taken off).
    assert fan_record(latch, "low").older == ()
    latch = driving("medium", "low", ("medium", "low", "medium"))
    assert fan_record(latch, "medium").older == ()
    assert fan_record(latch, "low").older == ("medium",)


def test_a_start_with_no_memory_saves_no_older_writes() -> None:
    assert fan_record(fresh(), None).older == ()
    assert fan_record(fresh(), "high").older == ()
