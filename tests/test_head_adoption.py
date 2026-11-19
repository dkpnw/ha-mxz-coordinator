"""planner's rule for Accept changes made at the head, called directly: no Home Assistant.

Each expectation is worked out from the rule it states. A report from or to unavailable is the
head loading, never a person. A change MXZ's own writes explain is never adopted. An off pressed
at an enabled room's head disables the room unless the plan wants the head off; heat or cool
pressed at a disabled room's head enables it; a setpoint pressed in heat or cool is the room's
new target if it differs by half a step or more. Eco, a standby hold, Coordinator off, a start,
a hands-off room, a floor run, a pause and a cloud head's older report adopt nothing. Every
value is invented.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.mxz_coordinator.const import (
    MODE_COOL,
    MODE_DRY,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
)
from custom_components.mxz_coordinator.planner import (
    Adoption,
    HeadChange,
    adopt_head_change,
    head_moved,
    setpoint_field,
)


def _planned_cool() -> str:
    return MODE_COOL


# A person pressed the high edge from 70 to 66 at an enabled cooling room's dual head.
PRESS = HeadChange(
    moved={"setpoint"},
    accept=True,
    retired=False,
    coordinator=True,
    inhibited=False,
    eco=False,
    starting=False,
    hands_off=False,
    floor=False,
    paused=False,
    stale=False,
    mode=MODE_COOL,
    mode_own=True,
    enabled=True,
    planned=_planned_cool,
    field="target_temp_high",
    setpoint=66.0,
    setpoint_own=False,
    setpoint_stale=False,
    driven=False,
    target=66.0,
    current=70.0,
    step=1.0,
)


# -- what a report moved -------------------------------------------------------


def moved(**changes: object) -> set[str]:
    report: dict[str, object] = {
        "accept": True, "old_mode": MODE_COOL, "new_mode": MODE_COOL,
        "field": "target_temp_high", "old_setpoint": 70, "new_setpoint": 70,
    }
    return head_moved(**{**report, **changes})  # type: ignore[arg-type]


def test_a_mode_change_and_a_setpoint_change_each_move() -> None:
    assert moved(new_mode=MODE_HEAT) == {"mode"}
    assert moved(new_setpoint=66) == {"setpoint"}
    assert moved(new_mode=MODE_HEAT, new_setpoint=66) == {"mode", "setpoint"}


def test_a_report_that_changes_neither_moves_nothing() -> None:
    assert moved() == set()


def test_with_no_setpoint_field_a_setpoint_change_moves_nothing() -> None:
    assert moved(new_mode=MODE_FAN_ONLY, field=None, new_setpoint=66) == {"mode"}


@pytest.mark.parametrize(
    "changes",
    [
        {"accept": False, "new_mode": MODE_HEAT},
        {"old_mode": None, "new_mode": MODE_HEAT},
        {"new_mode": None},
        {"old_mode": "unavailable", "new_mode": MODE_HEAT},
        {"new_mode": "unavailable"},
        {"old_mode": "unknown", "new_setpoint": 66},
    ],
    ids=["option_off", "no_old_state", "no_new_state", "from_unavailable", "to_unavailable", "from_unknown"],
)
def test_the_option_off_or_a_head_loading_moves_nothing(changes: dict) -> None:
    assert moved(**changes) == set()


# -- which setpoint carries the target --------------------------------------------


def test_a_range_head_carries_the_target_on_the_edge_mxz_writes() -> None:
    assert setpoint_field(MODE_COOL, features=2 | 8, cloud=False) == "target_temp_high"
    assert setpoint_field(MODE_HEAT, features=2, cloud=False) == "target_temp_low"


@pytest.mark.parametrize(("features", "cloud"), [(1, False), (None, False), (0, False), (2, True)])
def test_a_single_setpoint_or_cloud_head_carries_it_on_its_one_setpoint(features, cloud) -> None:
    assert setpoint_field(MODE_COOL, features=features, cloud=cloud) == "temperature"


@pytest.mark.parametrize("mode", [MODE_OFF, MODE_FAN_ONLY, MODE_DRY, "heat_cool", "auto", "unavailable"])
def test_off_heat_and_cool_no_setpoint_carries_it(mode: str) -> None:
    assert setpoint_field(mode, features=2, cloud=False) is None


# -- the rule ---------------------------------------------------------------------


def test_a_setpoint_pressed_is_the_new_target() -> None:
    assert adopt_head_change(PRESS) == Adoption(target=66.0)
    assert adopt_head_change(PRESS).as_dict() == {"target": 66.0}


@pytest.mark.parametrize(
    "gate",
    [
        {"accept": False}, {"retired": True}, {"coordinator": False}, {"inhibited": True},
        {"eco": True}, {"starting": True}, {"hands_off": True}, {"floor": True},
        {"paused": True}, {"stale": True}, {"mode": None},
    ],
    ids=[
        "option_off", "retired", "coordinator_off", "standby", "eco", "starting", "hands_off",
        "floor_run", "paused", "stale_cloud_mode", "unavailable",
    ],
)
def test_every_gate_adopts_nothing(gate: dict) -> None:
    assert adopt_head_change(replace(PRESS, **gate)) is None


def test_a_setpoint_mxz_wrote_or_a_stale_poll_or_nothing_readable_is_no_press() -> None:
    assert adopt_head_change(replace(PRESS, setpoint_own=True)) is None
    assert adopt_head_change(replace(PRESS, setpoint_stale=True)) is None
    assert adopt_head_change(replace(PRESS, setpoint=None, target=None)) is None
    assert adopt_head_change(replace(PRESS, moved={"mode"}, mode_own=True)) is None


def test_a_target_less_than_half_a_step_away_is_a_press_with_nothing_to_adopt() -> None:
    assert adopt_head_change(replace(PRESS, target=70.0)) == Adoption()
    assert adopt_head_change(replace(PRESS, target=69.5)) == Adoption(target=69.5)
    assert adopt_head_change(replace(PRESS, target=69.6)) == Adoption()
    assert Adoption().as_dict() == {}


def test_a_driven_cloud_head_s_setpoint_is_not_the_target() -> None:
    assert adopt_head_change(replace(PRESS, driven=True)) == Adoption()


def test_a_heat_cool_flip_alone_is_a_press_with_nothing_to_adopt() -> None:
    flip = replace(PRESS, moved={"mode"}, mode=MODE_HEAT, mode_own=False, field="target_temp_low")
    assert adopt_head_change(flip) == Adoption()


def test_an_off_pressed_at_an_enabled_room_disables_it() -> None:
    off = replace(PRESS, moved={"mode"}, mode=MODE_OFF, mode_own=False, field=None, setpoint=None)
    assert adopt_head_change(off) == Adoption(enable=False)
    assert adopt_head_change(off).as_dict() == {"enable": False}


def test_an_off_the_plan_wants_mxz_wrote_or_at_a_disabled_room_is_no_request() -> None:
    off = replace(PRESS, moved={"mode"}, mode=MODE_OFF, mode_own=False, field=None, setpoint=None)
    assert adopt_head_change(replace(off, planned=lambda: MODE_OFF)) is None
    assert adopt_head_change(replace(off, mode_own=True)) is None
    assert adopt_head_change(replace(off, enabled=False)) is None
    assert adopt_head_change(replace(off, moved={"setpoint"})) is None


def test_the_plan_is_read_only_for_an_off_pressed_at_an_enabled_room() -> None:
    reads: list[str] = []

    def planned() -> str:
        reads.append("read")
        return MODE_COOL

    adopt_head_change(replace(PRESS, planned=planned))
    adopt_head_change(replace(PRESS, moved={"mode"}, mode=MODE_OFF, mode_own=False, enabled=False,
                              field=None, planned=planned))
    assert reads == []
    adopt_head_change(replace(PRESS, moved={"mode"}, mode=MODE_OFF, mode_own=False, field=None,
                              planned=planned))
    assert reads == ["read"]


def test_heat_or_cool_pressed_at_a_disabled_room_enables_it_with_its_setpoint() -> None:
    on = replace(PRESS, moved={"mode", "setpoint"}, mode_own=False, enabled=False)
    assert adopt_head_change(on) == Adoption(enable=True, target=66.0)
    assert list(adopt_head_change(on).as_dict()) == ["enable", "target"]
    assert adopt_head_change(replace(on, target=70.0)) == Adoption(enable=True)


def test_a_setpoint_alone_at_a_disabled_room_is_nothing() -> None:
    assert adopt_head_change(replace(PRESS, enabled=False)) is None


@pytest.mark.parametrize("mode", [MODE_FAN_ONLY, MODE_DRY, "heat_cool", "auto"])
def test_a_mode_with_no_setpoint_carrying_the_target_is_not_adopted(mode: str) -> None:
    change = replace(PRESS, moved={"mode"}, mode=mode, mode_own=False, field=None)
    assert adopt_head_change(change) is None
