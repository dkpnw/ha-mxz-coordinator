"""planner.apply_plan and idle_for, called directly: no Home Assistant, no pytest-homeassistant.

Each expectation comes from the rule it states (a paused head goes straight to off, a head mid
vane-kick is left alone, a restart rebuilds the coil-dry memory from a head seen cooling, and so
on), not read back from the code. Each gate has a case where it holds and one where it does not.
Every room and value is invented.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from custom_components.mxz_coordinator.const import (
    ENGAGE_SATISFIED,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
)
from custom_components.mxz_coordinator.plan import Plan, RoomPlan
from custom_components.mxz_coordinator.planner import (
    ApplyHead,
    ApplyShared,
    HeadCommand,
    SetpointSettings,
    apply_plan,
    idle_for,
    rebuilt_last_run,
)
from custom_components.mxz_coordinator.timing import Arm

NOW = 5000.0
COIL = 600.0
SETTINGS = SetpointSettings(
    clamp_min=59, clamp_max=88, band=1.0, step=1.0, eco_cool=(78, 80), eco_heat=(60, 62),
)


def _room(**change: Any) -> RoomPlan:
    room = RoomPlan(
        slug="a", name="A", demand="neutral", engage=MODE_COOL, temp=74.0, target=70.0,
        target_range=None, enabled=True, drift=1.0, drift_heat=1.0, drift_cool=1.0,
        start_points={}, sensor_health="ok", sensor_age=None, hands_off=None,
        hands_off_vote=None, vote_status="none", lost_since=None, vote_basis={},
        safety_priority=False, comfort_priority_entity=None, comfort_priority=0.0,
        comfort_priority_status="off", floor_low=None, floor_high=None, floor_want=None,
        dry={}, opening={}, fan_hold=False, control_reasons=[], floor={}, delivery={}, head={},
    )
    return replace(room, **change)


def _plan(*rooms: RoomPlan, state: str = MODE_COOL, by_hands_off: bool = False) -> Plan:
    return Plan(
        rooms=list(rooms), state=state, standoff=False, mode_by_hands_off=by_hands_off,
        mode_by_safety=False, mode_by_comfort_priority=False, mode_by_floor=False,
        sensors_ok=True, seconds_since_mode_change=None, mode_change_allowed=True,
        inhibited=False, idle_action=IDLE_ACTION_OFF,
    )


def _never() -> bool:
    raise AssertionError("read past a gate that held")


def _head(cid: str = "climate.a", **change: Any) -> ApplyHead:
    head = ApplyHead(
        climate_id=cid, vane_kick=False, settling=lambda: False, paused=False, head=MODE_COOL,
        hands_off=None, released=False, busy=False, startup=False, enabled=True, target=70.0,
        heat_target=70.0, cool_target=70.0, floor_clear=None, taken_over=None, last_run=None,
        dry_waiting=False,
    )
    return replace(head, **change)


def _shared(**change: Any) -> ApplyShared:
    shared = ApplyShared(
        retired=False, coordinator=True, standby=None, inhibited=False, eco=False,
        idle_action=IDLE_ACTION_OFF, coil_dry_seconds=COIL, clock=NOW, settings=SETTINGS,
        mode=MODE_COOL, by_votes=False, resting=None, hold=600.0,
    )
    return replace(shared, **change)


def _one(room: RoomPlan | None = None, head: ApplyHead | None = None, **shared: Any):
    result = apply_plan(_plan(room or _room()), [head or _head()], _shared(**shared))
    assert result.gate is None
    return result.heads[0]


# -- the gates before any head ---------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "gate"),
    [
        ({"retired": True, "coordinator": False}, "retired"),
        ({"coordinator": False, "standby": MODE_OFF}, "coordinator_off"),
    ],
)
def test_retired_and_coordinator_off_apply_nothing_in_that_order(change, gate) -> None:
    result = apply_plan(_plan(_room()), [_head(settling=_never)], _shared(**change))
    assert result == type(result)(gate=gate)
    assert result.armed == ()


def test_on_and_not_retired_applies_the_plan() -> None:
    assert apply_plan(_plan(_room()), [_head()], _shared()).gate is None


def test_a_fixed_standby_park_sends_every_head_its_mode_but_one_mid_kick() -> None:
    result = apply_plan(
        _plan(_room(), _room(slug="b")),
        [_head(settling=_never, paused=True), _head("climate.b", vane_kick=True, settling=_never)],
        _shared(standby=MODE_FAN_ONLY),
    )
    assert result.gate == "standby"
    park = HeadCommand(mode=MODE_FAN_ONLY, low=0.0, high=0.0, delta=0.0, fan=None)
    assert [(h.climate_id, h.skip, h.command) for h in result.heads] == [
        ("climate.a", None, park), ("climate.b", "vane_kick", None),
    ]
    assert result.write_back is None and result.hold_end is None


@pytest.mark.parametrize("state", ["off", "unknown"])
def test_a_plan_not_ready_applies_nothing(state) -> None:
    assert apply_plan(_plan(_room(), state=state), [_head(settling=_never)], _shared()).gate == "not_ready"


def test_an_engage_it_does_not_know_applies_nothing() -> None:
    result = apply_plan(_plan(_room(engage="dry")), [_head(settling=_never)], _shared())
    assert result.gate == "unknown_engage"
    for engage in (MODE_COOL, MODE_HEAT, ENGAGE_SATISFIED, MODE_OFF):
        assert apply_plan(_plan(_room(engage=engage)), [_head()], _shared()).gate is None


# -- each head's gates, in order ---------------------------------------------------------


def test_a_head_mid_vane_kick_is_skipped_before_anything_else_is_read() -> None:
    head = _one(head=_head(vane_kick=True, settling=_never, paused=True))
    assert head.skip == "vane_kick" and head.command is None
    assert _one(head=_head(vane_kick=False)).command is not None


def test_a_change_at_the_head_still_holding_skips_it() -> None:
    assert _one(head=_head(settling=lambda: True, paused=True)).skip == "settling"
    assert _one(head=_head(settling=lambda: False)).skip is None


@pytest.mark.parametrize("shown", [None, "unavailable", "unknown"])
def test_a_paused_head_with_no_state_is_skipped(shown) -> None:
    head = _one(head=_head(paused=True, head=shown))
    assert head.skip == "unavailable" and head.fan_read is None and head.command is None


def test_a_paused_head_goes_straight_to_off_with_its_fan_only_read() -> None:
    head = _one(
        head=_head(paused=True, head=MODE_COOL, last_run=(MODE_COOL, NOW - 10)),
        idle_action=IDLE_ACTION_OFF_AFTER_DRY,
    )
    assert head.command == HeadCommand(mode=MODE_OFF, low=0.0, high=0.0, delta=0.0, fan=None)
    assert head.fan_read == MODE_OFF and head.arm is None and head.seeded is None
    assert _one(head=_head(paused=False)).fan_read is None


def test_a_released_hands_off_room_gets_its_hand_back_only() -> None:
    head = _one(head=_head(hands_off="released", released=True))
    assert head.skip == "handback" and head.command is None


def test_a_parking_hands_off_head_seen_off_with_nothing_in_flight_has_landed() -> None:
    head = _one(head=_head(hands_off="parking", head=MODE_OFF))
    assert head.skip == "landed" and head.released and head.command is None


@pytest.mark.parametrize(
    "change",
    [{"busy": True}, {"head": MODE_COOL}, {"hands_off": "floor"}],
    ids=["busy", "not_off_yet", "floor_run"],
)
def test_a_hands_off_head_still_owed_its_off_takes_the_ordinary_path(change) -> None:
    base = {"hands_off": "parking", "head": MODE_OFF, "enabled": False} | change
    head = _one(_room(engage=MODE_OFF, enabled=False), _head(**base))
    assert head.skip is None and not head.released
    assert head.command is not None and head.command.mode == MODE_OFF


def test_a_standby_hold_holds_a_hands_off_head_like_any_disabled_room() -> None:
    head = _one(head=_head(hands_off="released", released=True), inhibited=True)
    assert head.skip is None and head.command is not None


@pytest.mark.parametrize("shown", [None, "unavailable"])
def test_a_startup_seed_whose_head_has_no_state_is_skipped(shown) -> None:
    assert _one(head=_head(startup=True, head=shown)).skip == "unavailable"


def test_a_startup_seed_whose_head_reports_is_commanded() -> None:
    assert _one(head=_head(startup=True, head=MODE_OFF)).command is not None
    assert _one(head=_head(startup=False, head=None)).command is not None


def test_a_held_dry_is_left_alone_its_fan_read_and_its_coil_dry_wait_cancelled() -> None:
    head = _one(_room(dry={"dry": "held"}), _head(head=MODE_DRY))
    assert head.skip == "dry" and head.command is None and head.fan_read == MODE_DRY
    assert head.dry_stamp == (MODE_COOL, NOW)
    assert head.arm == Arm("dry", "climate.a", None)


def test_a_dry_picked_but_not_shown_yet_owes_no_coil_dry_run() -> None:
    head = _one(_room(dry={"dry": "held"}), _head(head=MODE_COOL))
    assert head.dry_stamp is None and head.skip == "dry"


def test_a_dry_parked_for_heat_is_told_once_and_parked_at_its_idle_action() -> None:
    head = _one(
        _room(dry={"dry": "parked"}, engage=ENGAGE_SATISFIED), _head(head=MODE_DRY),
        idle_action=IDLE_ACTION_OFF_AFTER_DRY, mode=MODE_HEAT,
    )
    assert head.dry_notice and head.dry_stamp == (MODE_COOL, NOW)
    # Dry wet the coil: the park is the coil-dry run, its wakeup just after its end.
    assert head.command is not None and head.command.mode == MODE_FAN_ONLY
    assert head.arm == Arm("dry", "climate.a", COIL + 1)
    assert not _one(_room(dry={}), _head(head=MODE_DRY)).dry_notice


# -- the idle park and the coil-dry memory -------------------------------------------------


def test_a_parked_head_with_coil_dry_time_left_arms_its_wakeup_once() -> None:
    room = _room(engage=ENGAGE_SATISFIED)
    head = _head(head=MODE_COOL, last_run=(MODE_COOL, NOW - 100))
    parked = _one(room, head, idle_action=IDLE_ACTION_OFF_AFTER_DRY)
    assert parked.command is not None and parked.command.mode == MODE_FAN_ONLY
    assert parked.arm == Arm("dry", "climate.a", COIL - 100 + 1)
    armed = _one(room, replace(head, dry_waiting=True), idle_action=IDLE_ACTION_OFF_AFTER_DRY)
    assert armed.arm is None and armed.command == parked.command


def test_a_coil_dry_run_that_is_over_parks_off_with_no_wakeup() -> None:
    head = _one(
        _room(engage=ENGAGE_SATISFIED), _head(last_run=(MODE_COOL, NOW - COIL)),
        idle_action=IDLE_ACTION_OFF_AFTER_DRY,
    )
    assert head.command is not None and head.command.mode == MODE_OFF and head.arm is None


@pytest.mark.parametrize(("shown", "owed"), [
    (MODE_COOL, True), (MODE_FAN_ONLY, True), (MODE_HEAT, False), (MODE_OFF, False),
    (None, False), ("unavailable", False),
])
def test_a_restart_rebuilds_the_coil_dry_memory_from_what_the_head_shows(shown, owed) -> None:
    head = _one(
        _room(engage=ENGAGE_SATISFIED), _head(head=shown), idle_action=IDLE_ACTION_OFF_AFTER_DRY,
    )
    assert head.seeded == ((MODE_COOL, NOW) if owed else None)
    assert head.arm == (Arm("dry", "climate.a", COIL + 1) if owed else None)
    assert head.command is not None
    assert head.command.mode == (MODE_FAN_ONLY if owed else MODE_OFF)


@pytest.mark.parametrize("idle", [IDLE_ACTION_OFF, IDLE_ACTION_FAN_ONLY])
def test_only_off_after_dry_rebuilds_the_memory(idle) -> None:
    head = _one(_room(engage=ENGAGE_SATISFIED), _head(head=MODE_COOL), idle_action=idle)
    assert head.seeded is None and head.arm is None


def test_a_head_with_a_last_run_keeps_it() -> None:
    park = idle_for(
        "climate.a", IDLE_ACTION_OFF_AFTER_DRY, (MODE_HEAT, NOW - 5), MODE_COOL, COIL, NOW,
        dry_waiting=False,
    )
    assert park.seeded is None and park.idle == MODE_OFF and park.arm is None


def test_rebuilt_last_run() -> None:
    assert rebuilt_last_run(MODE_COOL, 3.0) == (MODE_COOL, 3.0)
    assert rebuilt_last_run(MODE_FAN_ONLY, 3.0) == (MODE_COOL, 3.0)
    assert rebuilt_last_run(MODE_DRY, 3.0) is None


def test_a_disabled_room_never_idles_in_fan_only() -> None:
    head = _one(
        _room(engage=MODE_OFF, enabled=False),
        _head(enabled=False, last_run=(MODE_COOL, NOW - 10)),
        idle_action=IDLE_ACTION_OFF_AFTER_DRY,
    )
    assert head.command is not None and head.command.mode == MODE_OFF


# -- the floor ------------------------------------------------------------------------------


def test_a_floor_run_drives_its_clear_line_and_keeps_a_persons_warmer_run() -> None:
    room = _room(engage=MODE_HEAT, floor_want=MODE_HEAT, enabled=False)
    head = _head(enabled=False, floor_clear=50.0, taken_over=(MODE_HEAT, 66.0, 68.0))
    heat = apply_plan(_plan(room, state=MODE_HEAT), [head], _shared(mode=MODE_HEAT)).heads[0]
    assert heat.taken and heat.command is not None and heat.command.fan is None
    assert heat.command.mode == MODE_HEAT and heat.command.low == 66.0
    cool = apply_plan(_plan(room, state=MODE_COOL), [head], _shared()).heads[0]
    assert not cool.taken  # the plan is cool: the person's run is not the floor's direction


def test_a_floor_want_with_no_clear_line_is_refused() -> None:
    with pytest.raises(ValueError, match="clear line"):
        _one(_room(floor_want=MODE_HEAT), _head(floor_clear=None))


# -- the write-back -----------------------------------------------------------------------


def test_a_flip_is_written_back_and_stamped_with_the_hold_end_wakeup() -> None:
    result = apply_plan(_plan(_room(engage=MODE_HEAT), state=MODE_HEAT), [_head()], _shared())
    assert result.write_back is not None
    assert (result.write_back.mode, result.write_back.stamp, result.write_back.record) == (MODE_HEAT, True, True)
    assert result.hold_end == Arm("hold_end", None, 601.0)


def test_a_flip_with_no_hold_cancels_the_hold_end_wakeup() -> None:
    result = apply_plan(_plan(_room(engage=MODE_HEAT), state=MODE_HEAT), [_head()], _shared(hold=0.0))
    assert result.hold_end == Arm("hold_end", None, None)


def test_no_change_starts_no_hold() -> None:
    result = apply_plan(_plan(_room()), [_head()], _shared())
    assert result.write_back is not None and not result.write_back.stamp
    assert result.hold_end is None


def test_a_flip_hands_off_votes_alone_asked_for_starts_no_hold() -> None:
    result = apply_plan(
        _plan(_room(engage=ENGAGE_SATISFIED), state=MODE_HEAT, by_hands_off=True), [_head()], _shared(),
    )
    assert result.write_back is not None and result.write_back.by_votes
    assert result.hold_end is None


def test_armed_lists_each_heads_wakeup_in_room_order_then_the_hold_end() -> None:
    result = apply_plan(
        _plan(_room(engage=ENGAGE_SATISFIED), _room(slug="b", dry={"dry": "held"}), state=MODE_HEAT),
        [_head(last_run=(MODE_COOL, NOW - 1)), _head("climate.b", head=MODE_DRY)],
        _shared(idle_action=IDLE_ACTION_OFF_AFTER_DRY),
    )
    assert result.armed == (
        Arm("dry", "climate.a", COIL), Arm("dry", "climate.b", None), Arm("hold_end", None, 601.0),
    )
