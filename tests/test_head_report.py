"""The adopt hold's transitions, called directly: no pytest-homeassistant.

planner.head_report (a head's report against the hold), holds_change (the apply's gate),
adopt_end (the hold's end), startup_report (a startup seed and a reported off) and
HeadWriteMemory.pressed, and planned_act, the mode the plan wants a head in. Each expectation is the rule as Accept changes made at the head states
it, worked by hand; each callable that may be read only where it decides raises when read
anywhere else. Every head and value is invented.
"""

from __future__ import annotations

import pytest

from custom_components.mxz_coordinator.const import (
    ADOPT_DELAY,
    MODE_COOL,
    MODE_HEAT,
    MODE_OFF,
)
from custom_components.mxz_coordinator.head_writes import (
    HEAD_WRITE_FIELDS,
    HEAD_WRITE_MODE,
)
from custom_components.mxz_coordinator.logic import HeadWriteMemory
from custom_components.mxz_coordinator.planner import (
    Adoption,
    adopt_end,
    adopted,
    head_report,
    holds_change,
    planned_act,
    startup_report,
)
from custom_components.mxz_coordinator.timing import Arm

KEY = ("climate.a", "adopt")


def _never(*_args: object) -> object:
    raise AssertionError("read where it decides nothing")


# -- a head's report -------------------------------------------------------------------------


def test_a_report_that_moves_nothing_leaves_the_hold_as_it_is() -> None:
    assert head_report(set(), {"setpoint"}, key=KEY, holding=True, adoption=_never) is None


def test_a_change_with_something_to_adopt_arms_the_hold_with_all_that_moved() -> None:
    judged: list[set[str]] = []

    def adoption(moved: set[str]) -> Adoption:
        judged.append(moved)
        return Adoption(target=64.0)

    report = head_report({"mode"}, {"setpoint"}, key=KEY, holding=True, adoption=adoption)
    assert judged == [{"mode", "setpoint"}]
    assert report is not None
    assert report.pending == {"mode", "setpoint"}
    assert report.hold == Arm("heal", KEY, ADOPT_DELAY)
    assert report.disown and not report.replan


@pytest.mark.parametrize("result", [None, Adoption()], ids=["not_a_person", "nothing_to_adopt"])
@pytest.mark.parametrize("holding", [True, False])
def test_a_change_with_nothing_to_adopt_drops_the_hold(result, holding) -> None:
    report = head_report({"setpoint"}, set(), key=KEY, holding=holding, adoption=lambda _m: result)
    assert report is not None
    assert report.pending is None and report.hold == Arm("heal", KEY, None)
    assert not report.disown
    # Only a hold that was armed and now ends with nothing adopted owes the plan a compute.
    assert report.replan is holding


def test_an_enable_alone_is_something_to_adopt() -> None:
    report = head_report({"mode"}, set(), key=KEY, holding=False, adoption=lambda _m: Adoption(enable=False))
    assert report is not None and report.hold.delay == ADOPT_DELAY


# -- the apply's gate --------------------------------------------------------------------------


def test_a_hold_not_armed_protects_nothing_and_is_not_judged() -> None:
    assert holds_change(False, _never) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("result", "holds"),
    [(Adoption(target=64.0), True), (Adoption(enable=True), True), (Adoption(), False), (None, False)],
)
def test_an_armed_hold_protects_the_head_only_while_its_change_would_count(result, holds) -> None:
    assert holds_change(True, lambda: result) is holds
    assert adopted(result) is holds


# -- the hold's end ------------------------------------------------------------------------


def test_the_end_marks_what_moved_and_writes_the_target() -> None:
    end = adopt_end(
        {"mode", "setpoint"}, Adoption(target=64.0), mode=MODE_COOL, field="target_temp_high",
        dual=False,
    )
    assert end.seen == ("hvac_mode", "target_temp_high")
    assert (end.number, end.target, end.enable) == ("target", 64.0, None)


def test_the_mode_field_is_the_head_write_memorys() -> None:
    assert "hvac_mode" == HEAD_WRITE_MODE
    assert {"target_temp_high", "target_temp_low", "temperature"} < set(HEAD_WRITE_FIELDS)


@pytest.mark.parametrize(("mode", "number"), [(MODE_COOL, "target_high"), (MODE_HEAT, "target_low")])
def test_a_dual_rooms_target_goes_to_the_side_the_head_shows(mode, number) -> None:
    assert adopt_end({"setpoint"}, Adoption(target=66.0), mode=mode, field="x", dual=True).number == number


def test_no_target_writes_no_number_and_an_enable_alone_is_the_switch() -> None:
    end = adopt_end({"mode"}, Adoption(enable=True), mode=MODE_HEAT, field="target_temp_low", dual=True)
    assert end.seen == ("hvac_mode",)
    assert (end.number, end.target, end.enable) == (None, None, True)


def test_a_setpoint_moved_with_no_field_marks_nothing_for_it() -> None:
    assert adopt_end({"setpoint"}, Adoption(), mode=MODE_OFF, field=None, dual=False).seen == ()


# -- a startup seed ------------------------------------------------------------------------


def test_an_off_the_plan_does_not_want_cancels_the_seed() -> None:
    seed = startup_report(MODE_OFF, MODE_COOL, planned=lambda: MODE_COOL, stale=lambda: False)
    assert seed.cancel and seed.replan


def test_the_plans_own_off_or_a_stale_cloud_report_cancels_nothing() -> None:
    assert not startup_report(MODE_OFF, MODE_COOL, planned=lambda: MODE_OFF, stale=_never).cancel  # type: ignore[arg-type]
    assert not startup_report(MODE_OFF, MODE_COOL, planned=lambda: None, stale=lambda: True).cancel


@pytest.mark.parametrize(("mode", "old"), [(MODE_COOL, MODE_OFF), (MODE_HEAT, None)])
def test_a_new_real_mode_replans_and_reads_nothing_else(mode, old) -> None:
    seed = startup_report(mode, old, planned=_never, stale=_never)  # type: ignore[arg-type]
    assert seed.replan and not seed.cancel


@pytest.mark.parametrize(("mode", "old"), [(MODE_COOL, MODE_COOL), ("unavailable", MODE_COOL), (None, MODE_COOL)])
def test_the_same_mode_or_no_mode_does_not_replan(mode, old) -> None:
    assert not startup_report(mode, old, planned=_never, stale=_never).replan  # type: ignore[arg-type]


# -- what a person's change leaves in MXZ's write memory ------------------------------------


def _memory() -> HeadWriteMemory:
    memory = HeadWriteMemory(4)
    memory.wrote(70.0)
    memory.wrote(72.0)
    return memory


def test_a_persons_value_replaces_what_the_head_last_showed() -> None:
    memory = _memory()
    memory.pressed(64.0)
    assert memory.values[0] == 64.0


@pytest.mark.parametrize("shown", [None, 72.0, 70.0], ids=["nothing_shown", "own_last", "own_earlier"])
def test_nothing_shown_or_mxzs_own_value_changes_nothing(shown) -> None:
    memory = _memory()
    before = memory.record()
    memory.pressed(shown)
    assert memory.record() == before


# -- the mode the plan wants a head in -------------------------------------------------------


def _planned(**change: object) -> str | None:
    kwargs: dict[str, object] = {
        "active": None, "paused": False, "state": MODE_COOL, "engage": MODE_COOL,
        "enabled": True, "eco": False, "idle": lambda: "fan_only",
    } | change
    return planned_act(**kwargs)  # type: ignore[arg-type]


def test_a_running_delivery_owns_the_heads_writes() -> None:
    assert _planned(active=MODE_HEAT, paused=True, idle=_never) == MODE_HEAT


def test_a_paused_head_is_off_by_plan() -> None:
    assert _planned(paused=True, idle=_never) == MODE_OFF


@pytest.mark.parametrize("change", [{"state": None}, {"state": "off"}, {"engage": None}, {"engage": "dry"}])
def test_no_plan_for_the_head_says_nothing(change) -> None:
    assert _planned(idle=_never, **change) is None


def test_the_plans_run_and_its_idle_park() -> None:
    assert _planned() == MODE_COOL
    assert _planned(engage="satisfied") == "fan_only"
    # A disabled room parks off, though its parking mode is still read, as the apply reads it.
    read: list[bool] = []
    assert _planned(engage="satisfied", enabled=False, idle=lambda: read.append(True) or "fan_only") == MODE_OFF
    assert read == [True]
