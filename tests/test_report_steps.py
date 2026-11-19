"""A head's report and the heal and adopt holds around it, called directly: no pytest-homeassistant.

planner.report_steps (what a head's report does, in order), band_drift, off_while_enabled,
heal_due, off_heal_recheck, head_dropped, hold_end with AdoptEnd.steps, and head_change (the
HeadChange a head shows). Each expectation is the rule as the coordinator states it, worked by
hand; each callable that may be read only where it decides raises when read anywhere else, and
each rule has a case where it holds and one where it does not. Every head and value is invented.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from custom_components.mxz_coordinator.const import (
    ADOPT_DELAY,
    BAND_DRIFT_DELAY,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
    OFF_WHILE_ENABLED_DELAY,
)
from custom_components.mxz_coordinator.grid import Grid
from custom_components.mxz_coordinator.head_writes import HEAD_WRITE_MODE
from custom_components.mxz_coordinator.logic import HeadWriteMemory
from custom_components.mxz_coordinator.planner import (
    AdoptGates,
    Adoption,
    FanShown,
    FloorYielded,
    HeadReport,
    HoldEnd,
    Landed,
    LastRun,
    OffHealRecheck,
    Pressed,
    ReportedHead,
    SeedCancelled,
    SetEnable,
    SetTarget,
    SightLost,
    SightObserved,
    TakenOver,
    WritesShown,
    band_drift,
    head_change,
    head_dropped,
    heal_due,
    hold_end,
    off_heal_recheck,
    off_while_enabled,
    report_steps,
)
from custom_components.mxz_coordinator.timing import Arm, Refresh

HEAD = "climate.a"
NOW = 5000.0
RANGE = 2  # ClimateEntityFeature.TARGET_TEMPERATURE_RANGE


def _never(*_args: object) -> Any:
    raise AssertionError("read where it decides nothing")


def _report(**change: Any) -> ReportedHead:
    report = ReportedHead(
        key=HEAD, mode=MODE_COOL, attributes={"fan_mode": "auto"}, old_mode=MODE_COOL,
        old_attributes={"fan_mode": "auto"}, accept=True, cloud=False, sight=False,
        released_by_action=False, startup=False, owned_off=True, parking=False, kicking=False,
        floor_run=False, paused=False, stale=False, allow_dry=False, dry_allowed=False,
        enabled=True, eco_idle=False, inhibited=False, unresponsive=False, pending=set(),
        holding=False, hands_off_head=lambda: False, dry_reported=lambda: False,
        planned=lambda: MODE_COOL, clock=lambda: NOW, adoption=lambda moved: None,
    )
    return replace(report, **change)


def _steps(**change: Any) -> list[Any]:
    return list(report_steps(_report(**change)))


def _heals(steps: list[Any]) -> list[Arm]:
    return [step for step in steps if isinstance(step, Arm)]


BAND_OFF = Arm("heal", (HEAD, "band"), None)
OFF_OFF = Arm("heal", (HEAD, "off"), None)


# -- the order --------------------------------------------------------------------------------


def test_a_quiet_report_catches_up_cancels_both_heals_and_asks_nothing() -> None:
    assert _steps() == [FanShown("auto"), WritesShown(), BAND_OFF, OFF_OFF]


def test_no_new_state_catches_nothing_up() -> None:
    steps = _steps(mode=None, attributes={})
    assert FanShown(None) not in steps and WritesShown() not in steps
    assert steps[-1] is Refresh.REPORT  # out of sight: the plan reads that


def test_a_report_that_does_everything_does_it_in_order() -> None:
    planned: list[str] = []

    def plan() -> str:
        planned.append("read")
        return MODE_HEAT

    steps = _steps(
        mode=MODE_COOL, old_mode=MODE_DRY, attributes={"fan_mode": "low", "target_temp_high": 70.0,
        "supported_features": RANGE}, old_attributes={"fan_mode": "auto", "target_temp_high": 72.0,
        "supported_features": RANGE},
        startup=True, floor_run=True, paused=True, allow_dry=True, planned=plan,
        adoption=lambda moved: Adoption(target=70.0),
    )
    kinds = [type(step).__name__ if not isinstance(step, Refresh) else "Refresh" for step in steps]
    assert kinds == [
        "FanShown", "WritesShown", "FloorYielded", "TakenOver", "Arm", "LastRun", "Arm",
        "HeadReport", "Refresh",
    ]
    assert steps[5] == LastRun((MODE_COOL, NOW))
    # Read for the floor run and the pause; a cool report reads it for neither the seed nor the
    # off heal.
    assert planned == ["read", "read"]


# -- out of sight ----------------------------------------------------------------------------


@pytest.mark.parametrize(("old", "observe"), [(MODE_COOL, True), ("unavailable", False), (None, False)])
def test_losing_sight_keeps_what_the_head_showed_before(old, observe) -> None:
    steps = _steps(mode="unavailable", old_mode=old, sight=True)
    assert SightLost(observe_old=observe) in steps
    assert SightLost(observe_old=observe) not in _steps(mode="unavailable", old_mode=old, sight=False)


def test_back_in_sight_re_plans_and_unknown_counts_as_in_sight() -> None:
    assert _steps(mode=MODE_COOL, old_mode="unavailable", sight=True)[-1] is Refresh.REPORT
    steps = _steps(mode="unknown", old_mode="unavailable", sight=True, attributes={}, old_attributes={})
    assert not any(isinstance(step, SightLost) for step in steps)
    assert steps[-1] is Refresh.REPORT  # unknown <-> unavailable recomputes the vote


# -- the startup seed --------------------------------------------------------------------------


def test_a_reported_off_the_plan_does_not_want_cancels_the_seed() -> None:
    assert SeedCancelled() in _steps(mode=MODE_OFF, startup=True, planned=lambda: MODE_COOL)
    assert SeedCancelled() not in _steps(mode=MODE_OFF, startup=True, planned=lambda: MODE_OFF)
    assert SeedCancelled() not in _steps(mode=MODE_OFF, startup=True, stale=True, planned=lambda: MODE_COOL)
    assert SeedCancelled() not in _steps(mode=MODE_OFF, startup=False, enabled=False, planned=_never)


# -- hands-off, floor and pause ------------------------------------------------------------------


def test_a_parking_head_seen_off_with_nothing_else_in_flight_has_landed() -> None:
    landed = _steps(mode=MODE_OFF, parking=True, sight=True, enabled=False)
    assert landed.index(Landed()) + 1 == landed.index(SightObserved())
    assert Landed() not in _steps(mode=MODE_OFF, parking=True, owned_off=False, enabled=False)
    assert Landed() not in _steps(mode=MODE_FAN_ONLY, parking=True, enabled=False)


def test_a_persons_mode_change_takes_a_floor_run_back_unless_it_is_the_plans() -> None:
    change = {"mode": MODE_HEAT, "old_mode": MODE_COOL, "floor_run": True}
    assert FloorYielded() in _steps(**change, planned=lambda: MODE_COOL)
    assert FloorYielded() not in _steps(**change, planned=lambda: MODE_HEAT)
    for gate in ({"kicking": True}, {"stale": True}, {"old_mode": "unavailable"}, {"old_mode": MODE_HEAT}):
        assert FloorYielded() not in _steps(**(change | gate), planned=lambda: MODE_COOL)


def test_a_change_at_a_paused_head_takes_the_room_over_but_not_an_off() -> None:
    change = {"mode": MODE_HEAT, "old_mode": MODE_OFF, "paused": True}
    assert TakenOver() in _steps(**change, planned=lambda: MODE_OFF)
    assert TakenOver() not in _steps(**change, planned=lambda: MODE_HEAT)
    assert TakenOver() not in _steps(**(change | {"mode": MODE_OFF, "old_mode": MODE_HEAT}), planned=lambda: MODE_HEAT)


# -- the band and off heals --------------------------------------------------------------------


def test_a_banned_mode_arms_the_band_heal_unless_a_rule_lets_it_stay() -> None:
    armed = Arm("heal", (HEAD, "band"), BAND_DRIFT_DELAY)
    assert armed in _steps(mode="heat_cool")
    for gate in ({"hands_off_head": lambda: True}, {"dry_allowed": True, "mode": MODE_DRY}, {"stale": True}):
        assert BAND_OFF in _steps(**({"mode": "heat_cool"} | gate))


def test_the_band_heal_reads_a_hands_off_release_the_report_itself_made() -> None:
    released = False
    report = report_steps(_report(
        mode="auto", old_mode=MODE_HEAT, floor_run=True, planned=lambda: MODE_HEAT,
        hands_off_head=lambda: released,
    ))
    for step in report:
        if isinstance(step, FloorYielded):
            released = True  # the yield released the room before the band check reads it
        if isinstance(step, Arm) and step.key == (HEAD, "band"):
            assert step == BAND_OFF
            break
    else:
        raise AssertionError("no band heal step")


def test_leaving_dry_owes_the_coil_dry_run_from_now() -> None:
    assert LastRun((MODE_COOL, NOW)) in _steps(mode=MODE_FAN_ONLY, old_mode=MODE_DRY, allow_dry=True)
    assert not any(isinstance(step, LastRun) for step in _steps(mode=MODE_FAN_ONLY, old_mode=MODE_DRY))
    assert not any(isinstance(step, LastRun) for step in _steps(
        mode=MODE_FAN_ONLY, old_mode=MODE_DRY, allow_dry=True, stale=True,
    ))
    assert not any(isinstance(step, LastRun) for step in _steps(
        mode="unavailable", old_mode=MODE_DRY, allow_dry=True,
    ))


def test_dry_ending_a_rooms_dry_re_plans() -> None:
    assert _steps(dry_reported=lambda: True)[-1] is Refresh.REPORT
    assert not isinstance(_steps(dry_reported=lambda: False)[-1], Refresh)


def test_an_off_the_plan_does_not_want_arms_the_off_heal() -> None:
    armed = Arm("heal", (HEAD, "off"), OFF_WHILE_ENABLED_DELAY)
    assert armed in _steps(mode=MODE_OFF, planned=lambda: MODE_COOL)
    assert OFF_OFF in _steps(mode=MODE_OFF, planned=lambda: MODE_OFF)
    assert OFF_OFF in _steps(mode=MODE_OFF, planned=lambda: MODE_COOL, inhibited=True)
    assert OFF_OFF in _steps(mode=MODE_OFF, planned=lambda: MODE_COOL, stale=True)
    assert OFF_OFF in _steps(mode=MODE_OFF, eco_idle=True, planned=_never)
    assert OFF_OFF in _steps(mode=MODE_OFF, enabled=False, planned=_never)


def test_band_drift_and_off_while_enabled() -> None:
    assert band_drift("auto", hands_off_head=False, dry_allowed=False)
    assert not band_drift(MODE_COOL, hands_off_head=False, dry_allowed=False)
    assert not band_drift(MODE_DRY, hands_off_head=False, dry_allowed=True)
    assert not band_drift("heat_cool", hands_off_head=True, dry_allowed=False)
    assert off_while_enabled(MODE_OFF, enabled=True, eco_idle=False, planned=lambda: MODE_FAN_ONLY)
    assert not off_while_enabled(MODE_OFF, enabled=True, eco_idle=False, planned=lambda: MODE_OFF)
    assert not off_while_enabled(MODE_COOL, enabled=True, eco_idle=False, planned=_never)


# -- the adopt hold -----------------------------------------------------------------------------


def test_a_setpoint_press_arms_the_adopt_hold_last_before_the_ask() -> None:
    steps = _steps(
        attributes={"fan_mode": "auto", "target_temp_high": 68.0, "supported_features": RANGE},
        old_attributes={"fan_mode": "auto", "target_temp_high": 70.0, "supported_features": RANGE},
        adoption=lambda moved: Adoption(target=68.0),
    )
    assert steps[-1] == HeadReport(
        pending={"setpoint"}, hold=Arm("heal", (HEAD, "adopt"), ADOPT_DELAY), disown=True, replan=False,
    )


def test_a_hold_a_report_ends_with_nothing_adopted_re_plans() -> None:
    steps = _steps(
        mode=MODE_HEAT, holding=True, pending={"mode"}, adoption=lambda moved: Adoption(),
    )
    assert steps[-2] == HeadReport(pending=None, hold=Arm("heal", (HEAD, "adopt"), None), disown=False, replan=True)
    assert steps[-1] is Refresh.REPORT
    assert not any(isinstance(step, HeadReport) for step in _steps(accept=False, mode=MODE_HEAT))


def test_a_released_room_that_votes_by_its_run_asks_at_once() -> None:
    change = {"released_by_action": True, "attributes": {"fan_mode": "auto", "hvac_action": "idle"},
              "old_attributes": {"fan_mode": "auto", "hvac_action": "cooling"}}
    assert _steps(**change)[-1] is Refresh.NOW
    assert _steps(**(change | {"released_by_action": False}))[-1] is not Refresh.NOW
    assert _steps(unresponsive=True)[-1] is Refresh.REPORT


# -- the heal waits running ---------------------------------------------------------------------


def _due(kind: str, mode: str | None, **change: Any) -> bool:
    reads: dict[str, Any] = {
        "hands_off_head": lambda: False, "dry_allowed": lambda: False, "enabled": True,
        "eco_idle": False, "planned": lambda: MODE_COOL, "stale": lambda: False,
        "coordinator": True, "inhibited": False,
    } | change
    return heal_due(kind, mode, **reads)


def test_a_band_heal_runs_only_while_its_drift_holds() -> None:
    assert _due("band", "auto", planned=_never)
    assert not _due("band", MODE_COOL, planned=_never)
    assert not _due("band", "auto", hands_off_head=lambda: True, planned=_never)
    assert not _due("band", "auto", stale=lambda: True, planned=_never)
    assert not _due("band", "auto", coordinator=False, planned=_never)
    assert not _due("band", "auto", inhibited=True, planned=_never)


def test_an_off_heal_runs_only_while_the_plan_still_wants_the_head_on() -> None:
    assert _due("off", MODE_OFF, hands_off_head=_never, dry_allowed=_never)
    assert not _due("off", MODE_OFF, planned=lambda: MODE_OFF, hands_off_head=_never)
    assert not _due("off", MODE_COOL, planned=_never, stale=_never, hands_off_head=_never)
    assert not _due("off", MODE_OFF, eco_idle=True, planned=_never, stale=_never, hands_off_head=_never)


def test_the_off_heal_is_checked_again_at_a_holds_end() -> None:
    cancel = Arm("heal", (HEAD, "off"), None)
    kwargs: dict[str, Any] = {"enabled": True, "eco_idle": False}
    assert off_heal_recheck(HEAD, True, MODE_OFF, planned=lambda: MODE_OFF, **kwargs) == cancel
    assert off_heal_recheck(HEAD, True, MODE_OFF, planned=lambda: MODE_COOL, **kwargs) is None
    assert off_heal_recheck(HEAD, False, MODE_OFF, planned=_never, **kwargs) is None
    assert off_heal_recheck(HEAD, True, MODE_OFF, planned=_never, enabled=False, eco_idle=False) == cancel


# -- a drop and a hold's end ----------------------------------------------------------------------


def test_a_change_to_the_room_drops_the_hold() -> None:
    assert head_dropped((HEAD, "adopt")) == HeadReport(
        pending=None, hold=Arm("heal", (HEAD, "adopt"), None), disown=False, replan=False,
    )


def test_a_hold_that_ends_with_nothing_to_adopt_only_asks_for_a_compute() -> None:
    assert hold_end({"mode"}, None, mode=MODE_HEAT, field=None, dual=False) == HoldEnd(steps=())
    assert HoldEnd(steps=()).refresh is Refresh.REQUEST


def test_a_hold_end_writes_the_memory_the_target_the_enable_then_checks_the_off_heal() -> None:
    end = hold_end(
        {"mode", "setpoint"}, Adoption(enable=True, target=66.0),
        mode=MODE_COOL, field="target_temp_high", dual=True,
    )
    assert end == HoldEnd(steps=(
        Pressed((HEAD_WRITE_MODE, "target_temp_high")), SetTarget("target_high", 66.0),
        SetEnable(True), OffHealRecheck(),
    ))
    off = hold_end({"mode"}, Adoption(enable=False), mode=MODE_OFF, field=None, dual=False)
    assert off.steps == (Pressed((HEAD_WRITE_MODE,)), SetEnable(False), OffHealRecheck())


# -- the change a head shows ------------------------------------------------------------------------


GRID = Grid(
    system_unit="°F", target_step=1.0, clamp_min=59.0, clamp_max=88.0,
    native=(16.0, 30.0, "°C"), native_step=0.5, native_unit="°C",
)
GATES = AdoptGates(
    accept=True, retired=False, coordinator=True, inhibited=False, eco=False, starting=False,
    hands_off=False, floor=False, paused=False,
)


def _change(state: str | None, attributes: dict[str, Any], **change: Any):
    kwargs: dict[str, Any] = {
        "state": state, "attributes": attributes, "cloud": False, "gates": GATES, "enabled": True,
        "heat_target": 68.0, "cool_target": 74.0, "driven": False, "step": 1.0, "memories": {},
        "grid": GRID, "mode_stale": lambda mode: False, "setpoint_stale": lambda shown: False,
        "planned": _never,
    } | change
    return head_change({"setpoint"}, **kwargs)


def test_a_head_change_reads_the_setpoint_carrying_the_target_in_the_mode_shown() -> None:
    shown = {"supported_features": RANGE, "target_temp_high": 65.5, "target_temp_low": 61.5}
    cool = _change(MODE_COOL, shown)
    # 65.5 F is 18.5 C on the head's half-degree grid; 66 F would move it on to 19 C.
    assert (cool.mode, cool.field, cool.setpoint, cool.target, cool.current) == (
        MODE_COOL, "target_temp_high", 65.5, 65.0, 74.0,
    )
    heat = _change(MODE_HEAT, shown)
    assert (heat.field, heat.setpoint, heat.current) == ("target_temp_low", 61.5, 68.0)


def test_a_head_change_holds_the_target_to_the_bounds_the_head_allows() -> None:
    low = _change(MODE_COOL, {"supported_features": RANGE, "target_temp_high": 50.0})
    assert low.target == 61.0  # 16 C, the head's floor, snapped up to a whole F


@pytest.mark.parametrize("state", [None, "unavailable", "unknown"])
def test_a_head_with_no_state_shows_no_mode_and_reads_nothing_stale(state) -> None:
    change = _change(state, {}, mode_stale=_never, setpoint_stale=_never)
    assert (change.mode, change.field, change.setpoint, change.target, change.stale) == (
        None, None, None, None, False,
    )


def test_a_head_change_knows_mxzs_own_writes() -> None:
    mode = HeadWriteMemory(4)
    mode.wrote(MODE_COOL)
    setpoint = HeadWriteMemory(4)
    setpoint.wrote(65.0)
    memories = {HEAD_WRITE_MODE: mode, "target_temp_high": setpoint}
    own = _change(MODE_COOL, {"supported_features": RANGE, "target_temp_high": 65.0}, memories=memories)
    assert own.mode_own and own.setpoint_own
    other = _change(MODE_HEAT, {"supported_features": RANGE, "target_temp_low": 70.0}, memories=memories)
    assert not other.mode_own and not other.setpoint_own


def test_a_cloud_heads_stale_poll_and_drive_offset_are_read_as_given() -> None:
    seen: list[Any] = []

    def stale(value: Any) -> bool:
        seen.append(value)
        return True

    change = _change(
        MODE_COOL, {"temperature": 70.0}, cloud=True, driven=True, mode_stale=stale, setpoint_stale=stale,
    )
    assert change.field == "temperature"  # a cloud head's one setpoint
    assert (change.stale, change.setpoint_stale, change.driven) == (True, True, True)
    assert seen == [MODE_COOL, 70.0]


def test_the_gates_are_passed_through() -> None:
    gates = replace(GATES, eco=True, paused=True)
    change = _change(MODE_COOL, {}, gates=gates)
    assert (change.eco, change.paused, change.accept, change.floor) == (True, True, True, False)
