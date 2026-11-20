"""sim_timing, called directly: no Home Assistant, no pytest-homeassistant.

Each expectation is worked out from the model's rules in the module's docstring (Home
Assistant 2025.11's request debouncer, the delivery queue's follow-up, MXZ's wakeups and
heartbeat); ``tests/test_sim_timing_parity.py`` checks those rules against the real
coordinator. Every time is invented, in seconds.
"""

from __future__ import annotations

import pytest

from custom_components.mxz_coordinator.sim_timing import (
    Arm,
    Ask,
    Beat,
    Computed,
    Fire,
    Step,
    Tick,
    Timers,
    Why,
    due,
    start,
    step,
)
from custom_components.mxz_coordinator.timing import Refresh

REQUEST, REPORT, NOW = Refresh.REQUEST, Refresh.REPORT, Refresh.NOW


def started(now: float = 0.0) -> Timers:
    """MXZ started at ``now``, its first compute ran sending nothing, and its startup wakeup is
    cancelled, so only the test's own timers run."""
    timers, _ = start(now)
    timers, _ = step(timers, Computed(sent=False), now)
    return step(timers, Arm("startup", None, None), now)[0]


def computes(timers: Timers, event: Ask | Arm | Tick, now: float) -> tuple[Timers, Step]:
    """The step, which must ask for a compute; then that compute ran, sending nothing."""
    timers, did = step(timers, event, now)
    assert did.compute is not None, did
    return step(timers, Computed(sent=False), now)[0], did


def test_a_start_computes_at_once_and_arms_its_startup_wakeup_and_heartbeat() -> None:
    timers, did = start(100.0)
    assert did.compute is Why.NOW
    timers, _ = step(timers, Computed(sent=False), 100.0)
    assert due(timers) == 140.0
    timers, did = computes(timers, Tick(), 140.0)
    assert did.ran == (Fire("startup", None), Why.ASK)
    assert due(timers) == 150.0  # the cooldown that compute started


def test_a_request_computes_at_once_then_one_kept_request_runs_at_the_cooldown_end() -> None:
    timers = started()
    timers, did = computes(timers, Ask(REQUEST), 60.0)
    assert did.compute is Why.ASK
    for at in (61.0, 63.5, 69.9):
        timers, did = step(timers, Ask(REQUEST), at)
        assert (did.compute, did.kept) == (None, True)
    assert due(timers) == 70.0
    timers, did = computes(timers, Tick(), 70.0)
    assert did.compute is Why.COOLDOWN
    assert due(timers) == 80.0
    timers, did = step(timers, Tick(), 80.0)
    assert did.ran == ()  # nothing kept: the cooldown just ends
    timers, did = step(timers, Ask(REQUEST), 81.0)
    assert did.compute is Why.ASK


def test_a_report_is_debounced_like_a_request() -> None:
    timers, _ = computes(started(), Ask(REPORT), 60.0)
    timers, did = step(timers, Ask(REPORT), 62.0)
    assert (did.compute, did.kept) == (None, True)


def test_a_refresh_at_once_cancels_the_cooldown_and_what_it_kept() -> None:
    timers, _ = computes(started(), Ask(REQUEST), 60.0)
    timers, _ = step(timers, Ask(REQUEST), 62.0)
    timers, did = computes(timers, Ask(NOW), 67.0)
    assert did.compute is Why.NOW
    timers, did = step(timers, Tick(), 70.0)
    assert did.ran == ()
    timers, did = step(timers, Ask(REQUEST), 71.0)
    assert did.compute is Why.ASK  # no cooldown after a refresh at once


def test_a_report_during_a_compute_runs_a_compute_at_the_cooldown_end() -> None:
    for first in (Ask(REQUEST), Ask(NOW)):
        timers, did = step(started(), first, 60.0)
        timers, did = step(timers, Computed(sent=True, asked=(REPORT,)), 60.0)
        assert did.compute is None
        assert due(timers) == 70.0
        timers, did = step(timers, Tick(), 70.0)
        assert did.compute is Why.COOLDOWN


def test_a_request_while_the_plan_s_deliveries_run_is_followed_up_at_once() -> None:
    timers, _ = step(started(), Ask(REQUEST), 60.0)
    timers, did = step(timers, Ask(REQUEST), 60.0)
    assert did.compute is None
    timers, did = step(timers, Computed(sent=True), 60.0)
    assert did.compute is Why.FOLLOWUP
    timers, _ = step(timers, Computed(sent=False), 60.0)
    timers, did = step(timers, Ask(REQUEST), 61.0)
    assert did.compute is Why.ASK  # the follow-up refreshed at once: no cooldown


def test_a_request_during_a_plan_that_sent_nothing_waits_for_the_cooldown() -> None:
    timers, _ = step(started(), Ask(REQUEST), 60.0)
    timers, _ = step(timers, Ask(REQUEST), 60.0)
    timers, did = step(timers, Computed(sent=False), 60.0)
    assert (did.compute, did.kept) == (None, True)
    assert due(timers) == 70.0


def test_a_refresh_at_once_during_a_compute_runs_right_after_it() -> None:
    timers, _ = step(started(), Ask(REQUEST), 60.0)
    timers, did = step(timers, Computed(sent=True, asked=(REPORT, NOW)), 60.0)
    assert did.compute is Why.NOW
    timers, _ = step(timers, Computed(sent=False), 60.0)
    assert due(timers) != 70.0  # the report's cooldown went with it


def test_wakeups_due_together_run_before_the_compute_the_first_started_ends() -> None:
    timers = started()
    for head in ("a", "b"):
        timers, _ = step(timers, Arm("dry", head, 100.0), 50.0)
    timers, did = step(timers, Tick(), 150.0)
    assert did.ran == (Fire("dry", "a"), Why.ASK)
    assert due(timers) == 150.0
    timers, did = step(timers, Tick(), 150.0)  # inside the compute dry a started
    assert (did.ran, did.compute) == ((Fire("dry", "b"),), None)
    timers, did = step(timers, Computed(sent=True), 150.0)
    assert did.compute is Why.FOLLOWUP


def test_each_tick_runs_one_timer_in_the_order_they_were_set() -> None:
    timers = started()
    timers, _ = step(timers, Arm("hold_end", None, 50.0), 50.0)
    timers, _ = step(timers, Arm("heal", ("a", "adopt"), 95.0), 5.0)
    timers, did = step(timers, Tick(), 100.0)
    assert (did.ran, did.compute) == ((Fire("hold_end", None),), None)
    assert due(timers) == 100.0
    timers, did = step(timers, Tick(), 100.0)
    assert (did.ran, did.compute) == ((Fire("heal", ("a", "adopt")),), None)
    timers, did = step(timers, Tick(), 100.0)
    assert did.ran == ()
    assert due(timers) == 900.0


def test_heartbeat_cooldown_end_and_a_wakeup_at_one_instant_run_one_per_tick() -> None:
    """At 900 s: the heartbeat (set first), the cooldown's end with a request it kept, then a
    wakeup set after it. The cooldown's compute starts before the wakeup runs, and the wakeup's
    request lands in it."""
    timers = started()
    timers, _ = computes(timers, Ask(REQUEST), 890.0)
    timers, _ = step(timers, Ask(REQUEST), 895.0)
    timers, _ = step(timers, Arm("coast", None, 5.0), 895.0)
    assert due(timers) == 900.0
    timers, did = step(timers, Tick(), 900.0)
    assert (did.ran, did.compute, did.kept) == ((Beat(),), None, True)
    timers, did = step(timers, Tick(), 900.0)
    assert (did.ran, did.compute) == ((Why.COOLDOWN,), Why.COOLDOWN)
    assert ("coast", None) in timers.waits  # still to run when the compute starts
    timers, did = step(timers, Tick(), 900.0)
    assert (did.ran, did.compute) == ((Fire("coast", None),), None)
    assert due(timers) == 1800.0
    timers, did = step(timers, Computed(sent=True), 900.0)
    assert did.compute is Why.FOLLOWUP


def test_a_compute_that_ends_before_the_next_timer_at_its_instant_leaves_it_to_the_cooldown() -> None:
    """``Computed`` between two timers due at one instant: the second one's request comes after
    the compute, so it waits for the cooldown that compute started."""
    timers = started()
    for head in ("a", "b"):
        timers, _ = step(timers, Arm("dry", head, 100.0), 50.0)
    timers, did = step(timers, Tick(), 150.0)
    assert did.compute is Why.ASK
    timers, did = step(timers, Computed(sent=False), 150.0)
    assert did.compute is None
    timers, did = step(timers, Tick(), 150.0)
    assert (did.ran, did.compute, did.kept) == ((Fire("dry", "b"),), None, True)
    assert due(timers) == 160.0
    timers, did = step(timers, Tick(), 160.0)
    assert did.compute is Why.COOLDOWN


def test_a_tick_inside_a_compute_runs_only_a_timer_due() -> None:
    timers = started()
    timers, _ = step(timers, Arm("settle", None, 10.0), 50.0)
    timers, did = step(timers, Ask(REQUEST), 60.0)
    timers, did = step(timers, Tick(), 60.0)
    assert (did.ran, did.compute) == ((Fire("settle", None),), None)
    with pytest.raises(ValueError, match="send Computed"):
        step(timers, Tick(), 60.0)
    timers, did = step(timers, Computed(sent=False), 60.0)
    assert did.compute is Why.NOW  # the settle wakeup's refresh at once, asked inside it


def test_a_restart_keeps_none_of_the_old_timers() -> None:
    timers = started()
    timers, _ = step(timers, Arm("coast", None, 100.0), 10.0)
    timers, did = start(50.0)
    assert did.compute is Why.NOW
    timers, _ = step(timers, Computed(sent=False), 50.0)
    assert due(timers) == 90.0
    timers, did = step(timers, Tick(), 90.0)
    assert (did.ran, did.compute) == ((Fire("startup", None), Why.ASK), Why.ASK)
    timers, _ = step(timers, Computed(sent=False), 90.0)
    assert due(timers) == 100.0
    timers, did = step(timers, Tick(), 100.0)
    assert did.ran == ()  # the cooldown's end; the old coast wakeup at 110 is gone
    assert due(timers) == 950.0


def test_each_wakeup_asks_what_timing_says() -> None:
    timers = started()
    timers, _ = step(timers, Arm("hold_end", None, 20.0), 50.0)
    timers, did = step(timers, Tick(), 70.0)
    assert (did.ran, did.compute) == ((Fire("hold_end", None),), None)
    timers, _ = computes(timers, Ask(REQUEST), 71.0)
    timers, _ = step(timers, Arm("settle", None, 5.0), 71.0)
    timers, did = step(timers, Tick(), 76.0)
    assert did.compute is Why.NOW  # past the cooldown still running


def test_an_arm_replaces_the_wakeup_of_its_name_and_key_and_none_cancels_it() -> None:
    timers, _ = start(0.0)
    timers, _ = step(timers, Computed(sent=False), 0.0)
    timers, _ = step(timers, Arm("coast", None, 11.0), 10.0)
    timers, _ = step(timers, Arm("coast", None, 30.0), 12.0)
    assert due(timers) == 40.0  # the startup wakeup; the coast one moved to 42
    timers, _ = step(timers, Arm("startup", None, None), 12.0)
    assert due(timers) == 42.0
    timers, _ = step(timers, Arm("coast", None, None), 13.0)
    assert due(timers) == 900.0


def test_the_heartbeat_runs_every_15_minutes_from_the_start() -> None:
    timers = started(5.0)
    for beat in (905.0, 1805.0):
        assert due(timers) == beat
        timers, did = computes(timers, Tick(), beat)
        assert did.ran == (Beat(), Why.ASK)
        timers, did = step(timers, Tick(), beat + 10)
        assert did.ran == ()


def test_a_timer_another_one_cancelled_at_the_same_instant_does_not_run() -> None:
    """A refresh at once ends the cooldown due at the same instant, and the request it kept."""
    timers, _ = step(started(), Ask(REQUEST), 60.0)
    # A compute arms its wakeups before the debouncer starts the cooldown.
    timers, _ = step(timers, Computed(sent=False, armed=(Arm("settle", None, 10.0),)), 60.0)
    timers, _ = step(timers, Ask(REQUEST), 61.0)  # kept for 70
    timers, did = step(timers, Tick(), 70.0)
    assert did.ran == (Fire("settle", None), Why.NOW)
    timers, did = step(timers, Computed(sent=False), 70.0)
    assert did.compute is None
    assert due(timers) == 900.0


def test_a_compute_must_end_before_time_moves() -> None:
    timers, _ = step(started(), Ask(REQUEST), 60.0)
    with pytest.raises(ValueError, match="send Computed"):
        step(timers, Tick(), 61.0)
    with pytest.raises(ValueError, match="no compute"):
        step(started(), Computed(sent=False), 61.0)


def test_a_step_leaves_the_timers_it_was_given_as_they_were() -> None:
    timers = started()
    before = (timers, dict(timers.waits))
    step(timers, Arm("coast", None, 11.0), 10.0)
    step(timers, Ask(REQUEST), 10.0)
    assert (timers, dict(timers.waits)) == before
