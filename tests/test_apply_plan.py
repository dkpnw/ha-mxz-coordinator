"""planner.apply_plan, apply_gate, idle_for and stamp_mode_change, called directly: no Home
Assistant, no pytest-homeassistant.

Each expectation comes from the rule it states (a paused head goes straight to off, a head mid
vane-kick is left alone, a restart rebuilds the coil-dry memory from a head seen cooling, and so
on), not read back from the code. Each gate has a case where it holds and one where it does not.
Every room and value is invented.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
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
    CoilDryMemory,
    DryNotice,
    FanRead,
    HandBack,
    HeadApply,
    HeadCommand,
    Landed,
    LastRun,
    ModeStamp,
    ModeWriteBack,
    Queue,
    SetpointSettings,
    apply_gate,
    apply_plan,
    idle_for,
    rebuilt_last_run,
    stamp_mode_change,
)
from custom_components.mxz_coordinator.timing import Arm

NOW = 5000.0
COIL = 600.0
SETTINGS = SetpointSettings(
    clamp_min=59, clamp_max=88, band=1.0, step=1.0, eco_cool=(78, 80), eco_heat=(60, 62),
)
PAUSE_OFF = HeadCommand(mode=MODE_OFF, low=0.0, high=0.0, delta=0.0, fan=None)


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


def _never() -> Any:
    raise AssertionError("read past a gate that held")


def _memory(last_run: tuple[str, float] | None = None, waiting: bool = False) -> Callable[[], CoilDryMemory]:
    return lambda: CoilDryMemory(last_run=last_run, waiting=waiting)


def _head(
    cid: str = "climate.a", *, last_run: tuple[str, float] | None = None, dry_waiting: bool = False,
    **change: Any,
) -> ApplyHead:
    head = ApplyHead(
        climate_id=cid, vane_kick=False, settling=lambda: False, paused=False, head=MODE_COOL,
        hands_off=None, released=False, busy=False, startup=False, enabled=True, target=70.0,
        heat_target=70.0, cool_target=70.0, floor_clear=None, taken_over=None,
        coil_dry=_memory(last_run, dry_waiting),
    )
    return replace(head, **change)


def _shared(**change: Any) -> ApplyShared:
    shared = ApplyShared(
        retired=False, coordinator=True, standby=None, inhibited=False, eco=False,
        idle_action=IDLE_ACTION_OFF, coil_dry_seconds=COIL, clock=NOW, settings=SETTINGS,
        mode=MODE_COOL, by_votes=False, resting=None,
    )
    return replace(shared, **change)


def _apply(
    plan: Plan, heads: list[ApplyHead], **shared: Any
) -> tuple[str | None, list[HeadApply], ModeWriteBack | None]:
    """The gate, each head's part and the write-back, checking the write-back comes last."""
    parts = list(apply_plan(plan, heads, _shared(**shared)))
    applied = [part for part in parts if isinstance(part, HeadApply)]
    back = [part for part in parts if isinstance(part, ModeWriteBack)]
    assert parts == [*applied, *back] and len(back) <= 1
    return apply_gate(plan, _shared(**shared)), applied, (back[0] if back else None)


def _one(room: RoomPlan | None = None, head: ApplyHead | None = None, **shared: Any) -> HeadApply:
    gate, (applied,), _ = _apply(_plan(room or _room()), [head or _head()], **shared)
    assert gate is None
    return applied


def _command(head: HeadApply) -> HeadCommand | None:
    queued = [step.command for step in head.steps if isinstance(step, Queue)]
    assert len(queued) <= 1
    return queued[0] if queued else None


# -- the gates before any head ---------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "gate"),
    [
        ({"retired": True, "coordinator": False}, "retired"),
        ({"coordinator": False, "standby": MODE_OFF}, "coordinator_off"),
    ],
)
def test_retired_and_coordinator_off_apply_nothing_in_that_order(change, gate) -> None:
    assert _apply(_plan(_room()), [_head(settling=_never)], **change) == (gate, [], None)


def test_on_and_not_retired_applies_the_plan() -> None:
    assert _apply(_plan(_room()), [_head()])[0] is None


def test_a_fixed_standby_park_sends_every_head_its_mode_but_one_mid_kick() -> None:
    gate, applied, back = _apply(
        _plan(_room(), _room(slug="b")),
        [
            _head(settling=_never, coil_dry=_never, paused=True),
            _head("climate.b", vane_kick=True, settling=_never, coil_dry=_never),
        ],
        standby=MODE_FAN_ONLY,
    )
    assert gate == "standby"
    park = HeadCommand(mode=MODE_FAN_ONLY, low=0.0, high=0.0, delta=0.0, fan=None)
    assert applied == [
        HeadApply("climate.a", steps=(Queue(park),)), HeadApply("climate.b", skip="vane_kick"),
    ]
    assert back is None


@pytest.mark.parametrize("state", ["off", "unknown"])
def test_a_plan_not_ready_applies_nothing(state) -> None:
    assert _apply(_plan(_room(), state=state), [_head(settling=_never)]) == ("not_ready", [], None)


def test_an_engage_it_does_not_know_applies_nothing() -> None:
    assert _apply(_plan(_room(engage="dry")), [_head(settling=_never)]) == ("unknown_engage", [], None)
    for engage in (MODE_COOL, MODE_HEAT, ENGAGE_SATISFIED, MODE_OFF):
        assert _apply(_plan(_room(engage=engage)), [_head()])[0] is None


# -- one head at a time ---------------------------------------------------------------------


def test_each_head_is_read_only_once_the_part_before_it_is_taken() -> None:
    read: list[str] = []

    def heads() -> Iterator[ApplyHead]:
        for cid in ("climate.a", "climate.b"):
            read.append(f"read {cid}")
            yield _head(cid)

    parts = apply_plan(_plan(_room(), _room(slug="b")), heads(), _shared())
    assert read == []
    first = next(parts)
    assert isinstance(first, HeadApply) and first.climate_id == "climate.a"
    assert read == ["read climate.a"]  # b not yet: a's part may still change what b reads
    second = next(parts)
    assert isinstance(second, HeadApply) and second.climate_id == "climate.b"
    assert read == ["read climate.a", "read climate.b"]
    assert isinstance(next(parts), ModeWriteBack)
    assert next(parts, None) is None


def test_the_coil_dry_memory_is_read_after_the_settling_gate() -> None:
    # Reading the gate reads the plan's act for the head, which can rebuild its coil-dry
    # memory and arm its wakeup; the park after it must see both, not arm a second wakeup.
    memory = CoilDryMemory(last_run=None, waiting=False)

    def settling() -> bool:
        nonlocal memory
        memory = CoilDryMemory(last_run=(MODE_COOL, NOW - 100), waiting=True)
        return False

    head = _one(
        _room(engage=ENGAGE_SATISFIED),
        _head(head=MODE_COOL, settling=settling, coil_dry=lambda: memory),
        idle_action=IDLE_ACTION_OFF_AFTER_DRY,
    )
    command = _command(head)
    # Read before the gate, the memory would be empty: rebuilt again and a second wakeup armed.
    assert command is not None and command.mode == MODE_FAN_ONLY
    assert head.steps == (Queue(command),)


# -- each head's gates, in order ---------------------------------------------------------


def test_a_head_mid_vane_kick_is_skipped_before_anything_else_is_read() -> None:
    head = _one(head=_head(vane_kick=True, settling=_never, coil_dry=_never, paused=True))
    assert head == HeadApply("climate.a", skip="vane_kick")
    assert _command(_one(head=_head(vane_kick=False))) is not None


def test_a_change_at_the_head_still_holding_skips_it() -> None:
    assert _one(head=_head(settling=lambda: True, coil_dry=_never, paused=True)) == HeadApply(
        "climate.a", skip="settling",
    )
    assert _one(head=_head(settling=lambda: False)).skip is None


@pytest.mark.parametrize("shown", [None, "unavailable", "unknown"])
def test_a_paused_head_with_no_state_is_skipped(shown) -> None:
    head = _one(head=_head(paused=True, head=shown, coil_dry=_never))
    assert head == HeadApply("climate.a", skip="unavailable")


def test_a_paused_head_goes_straight_to_off_with_its_fan_only_read() -> None:
    head = _one(
        head=_head(paused=True, head=MODE_COOL, last_run=(MODE_COOL, NOW - 10)),
        idle_action=IDLE_ACTION_OFF_AFTER_DRY,
    )
    # No coil-dry run, no rebuilt memory, no wakeup: the fan read, then off.
    assert head == HeadApply("climate.a", steps=(FanRead(MODE_OFF), Queue(PAUSE_OFF)))
    assert not any(isinstance(step, FanRead) for step in _one(head=_head(paused=False)).steps)


def test_a_released_hands_off_room_gets_its_hand_back_only() -> None:
    head = _one(head=_head(hands_off="released", released=True, coil_dry=_never))
    assert head == HeadApply("climate.a", skip="handback", steps=(HandBack(),))


def test_a_parking_hands_off_head_seen_off_with_nothing_in_flight_has_landed() -> None:
    head = _one(head=_head(hands_off="parking", head=MODE_OFF, coil_dry=_never))
    assert head == HeadApply("climate.a", skip="landed", steps=(Landed(),))


@pytest.mark.parametrize(
    "change",
    [{"busy": True}, {"head": MODE_COOL}, {"hands_off": "floor"}],
    ids=["busy", "not_off_yet", "floor_run"],
)
def test_a_hands_off_head_still_owed_its_off_takes_the_ordinary_path(change) -> None:
    base = {"hands_off": "parking", "head": MODE_OFF, "enabled": False} | change
    head = _one(_room(engage=MODE_OFF, enabled=False), _head(**base))
    assert head.skip is None and Landed() not in head.steps
    command = _command(head)
    assert command is not None and command.mode == MODE_OFF


def test_a_standby_hold_holds_a_hands_off_head_like_any_disabled_room() -> None:
    head = _one(head=_head(hands_off="released", released=True), inhibited=True)
    assert head.skip is None and _command(head) is not None and HandBack() not in head.steps


@pytest.mark.parametrize("shown", [None, "unavailable"])
def test_a_startup_seed_whose_head_has_no_state_is_skipped(shown) -> None:
    head = _one(head=_head(startup=True, head=shown, coil_dry=_never))
    assert head == HeadApply("climate.a", skip="unavailable")


def test_a_startup_seed_whose_head_reports_is_commanded() -> None:
    assert _command(_one(head=_head(startup=True, head=MODE_OFF))) is not None
    assert _command(_one(head=_head(startup=False, head=None))) is not None


def test_a_held_dry_is_left_alone_its_fan_read_and_its_coil_dry_wait_cancelled() -> None:
    head = _one(_room(dry={"dry": "held"}), _head(head=MODE_DRY))
    assert head == HeadApply("climate.a", skip="dry", steps=(
        LastRun((MODE_COOL, NOW)), Arm("dry", "climate.a", None), FanRead(MODE_DRY),
    ))


def test_a_dry_picked_but_not_shown_yet_owes_no_coil_dry_run() -> None:
    head = _one(_room(dry={"dry": "held"}), _head(head=MODE_COOL))
    assert head == HeadApply("climate.a", skip="dry", steps=(
        Arm("dry", "climate.a", None), FanRead(MODE_DRY),
    ))


def test_a_dry_parked_for_heat_is_told_once_and_parked_at_its_idle_action() -> None:
    head = _one(
        _room(dry={"dry": "parked"}, engage=ENGAGE_SATISFIED), _head(head=MODE_DRY),
        idle_action=IDLE_ACTION_OFF_AFTER_DRY, mode=MODE_HEAT,
    )
    command = _command(head)
    # Dry wet the coil: the park is the coil-dry run, its wakeup just after its end.
    assert command is not None and command.mode == MODE_FAN_ONLY
    assert head.steps == (
        LastRun((MODE_COOL, NOW)), DryNotice(), Arm("dry", "climate.a", COIL + 1), Queue(command),
    )
    assert DryNotice() not in _one(_room(dry={}), _head(head=MODE_DRY)).steps


# -- the idle park and the coil-dry memory -------------------------------------------------


def test_a_parked_head_with_coil_dry_time_left_arms_its_wakeup_once() -> None:
    room = _room(engage=ENGAGE_SATISFIED)
    parked = _one(
        room, _head(head=MODE_COOL, last_run=(MODE_COOL, NOW - 100)),
        idle_action=IDLE_ACTION_OFF_AFTER_DRY,
    )
    command = _command(parked)
    assert command is not None and command.mode == MODE_FAN_ONLY
    assert parked.steps == (Arm("dry", "climate.a", COIL - 100 + 1), Queue(command))
    armed = _one(
        room, _head(head=MODE_COOL, last_run=(MODE_COOL, NOW - 100), dry_waiting=True),
        idle_action=IDLE_ACTION_OFF_AFTER_DRY,
    )
    assert armed.steps == (Queue(command),)


def test_a_coil_dry_run_that_is_over_parks_off_with_no_wakeup() -> None:
    head = _one(
        _room(engage=ENGAGE_SATISFIED), _head(last_run=(MODE_COOL, NOW - COIL)),
        idle_action=IDLE_ACTION_OFF_AFTER_DRY,
    )
    command = _command(head)
    assert command is not None and command.mode == MODE_OFF and head.steps == (Queue(command),)


@pytest.mark.parametrize(("shown", "owed"), [
    (MODE_COOL, True), (MODE_FAN_ONLY, True), (MODE_HEAT, False), (MODE_OFF, False),
    (None, False), ("unavailable", False),
])
def test_a_restart_rebuilds_the_coil_dry_memory_from_what_the_head_shows(shown, owed) -> None:
    head = _one(
        _room(engage=ENGAGE_SATISFIED), _head(head=shown), idle_action=IDLE_ACTION_OFF_AFTER_DRY,
    )
    command = _command(head)
    assert command is not None
    assert command.mode == (MODE_FAN_ONLY if owed else MODE_OFF)
    rebuilt = (LastRun((MODE_COOL, NOW)), Arm("dry", "climate.a", COIL + 1)) if owed else ()
    assert head.steps == (*rebuilt, Queue(command))


@pytest.mark.parametrize("idle", [IDLE_ACTION_OFF, IDLE_ACTION_FAN_ONLY])
def test_only_off_after_dry_rebuilds_the_memory(idle) -> None:
    head = _one(_room(engage=ENGAGE_SATISFIED), _head(head=MODE_COOL), idle_action=idle)
    assert [step for step in head.steps if not isinstance(step, Queue)] == []


def test_a_head_with_a_last_run_keeps_it() -> None:
    park = idle_for(
        "climate.a", IDLE_ACTION_OFF_AFTER_DRY, (MODE_HEAT, NOW - 5), MODE_COOL, COIL, NOW,
        dry_waiting=False,
    )
    assert park.seeded is None and park.idle == MODE_OFF and park.arm is None
    assert park.steps == ()


def test_reading_a_park_keeps_the_rebuilt_memory_then_arms_the_wakeup() -> None:
    park = idle_for(
        "climate.a", IDLE_ACTION_OFF_AFTER_DRY, None, MODE_COOL, COIL, NOW, dry_waiting=False,
    )
    assert park.steps == (LastRun((MODE_COOL, NOW)), Arm("dry", "climate.a", COIL + 1))
    waiting = idle_for(
        "climate.a", IDLE_ACTION_OFF_AFTER_DRY, None, MODE_COOL, COIL, NOW, dry_waiting=True,
    )
    assert waiting.steps == (LastRun((MODE_COOL, NOW)),)


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
    command = _command(head)
    assert command is not None and command.mode == MODE_OFF


# -- the floor ------------------------------------------------------------------------------


def test_a_floor_run_drives_its_clear_line_and_keeps_a_persons_warmer_run() -> None:
    room = _room(engage=MODE_HEAT, floor_want=MODE_HEAT, enabled=False)
    head = _head(enabled=False, floor_clear=50.0, taken_over=(MODE_HEAT, 66.0, 68.0))
    (heat,) = _apply(_plan(room, state=MODE_HEAT), [head], mode=MODE_HEAT)[1]
    (queued,) = [step for step in heat.steps if isinstance(step, Queue)]
    assert queued.taken and queued.command.fan is None
    assert queued.command.mode == MODE_HEAT and queued.command.low == 66.0
    (cool,) = _apply(_plan(room, state=MODE_COOL), [head])[1]
    # The plan is cool: the person's run is not the floor's direction.
    assert [step.taken for step in cool.steps if isinstance(step, Queue)] == [False]


def test_a_floor_want_with_no_clear_line_is_refused() -> None:
    with pytest.raises(ValueError, match="clear line"):
        _one(_room(floor_want=MODE_HEAT), _head(floor_clear=None))


# -- the write-back and its stamp ------------------------------------------------------------


def test_a_flip_is_written_back_and_stamped() -> None:
    back = _apply(_plan(_room(engage=MODE_HEAT), state=MODE_HEAT), [_head()])[2]
    assert back is not None
    assert (back.mode, back.stamp, back.record) == (MODE_HEAT, True, True)


def test_no_change_starts_no_hold() -> None:
    back = _apply(_plan(_room()), [_head()])[2]
    assert back is not None and not back.stamp


def test_a_flip_hands_off_votes_alone_asked_for_starts_no_hold() -> None:
    back = _apply(
        _plan(_room(engage=ENGAGE_SATISFIED), state=MODE_HEAT, by_hands_off=True), [_head()],
    )[2]
    assert back is not None and back.by_votes and not back.stamp


def test_the_parts_come_in_room_order_then_the_write_back() -> None:
    gate, applied, back = _apply(
        _plan(_room(engage=ENGAGE_SATISFIED), _room(slug="b", dry={"dry": "held"}), state=MODE_HEAT),
        [_head(last_run=(MODE_COOL, NOW - 1)), _head("climate.b", head=MODE_DRY)],
        idle_action=IDLE_ACTION_OFF_AFTER_DRY,
    )
    assert gate is None and [head.climate_id for head in applied] == ["climate.a", "climate.b"]
    assert [step for head in applied for step in head.steps if isinstance(step, Arm)] == [
        Arm("dry", "climate.a", COIL), Arm("dry", "climate.b", None),
    ]
    assert back is not None and back.stamp


def _clock(*readings: float) -> Callable[[], float]:
    left = iter(readings)
    return lambda: next(left)


def test_a_stamp_reads_the_clock_then_arms_the_hold_end_for_the_hold_left() -> None:
    stamp = stamp_mode_change(_clock(100.0, 100.25), lambda: 1.7e9, retired=False, hold=600.0)
    # The hold left is read again on the clock, as the gate compares it: 0.25 s has gone.
    assert stamp == ModeStamp(at=100.0, wall=1.7e9, hold_end=Arm("hold_end", None, 600.75))


def test_a_stamp_with_no_hold_cancels_the_hold_end_wakeup() -> None:
    stamp = stamp_mode_change(_clock(100.0, 100.0), lambda: 1.7e9, retired=False, hold=0.0)
    assert stamp.hold_end == Arm("hold_end", None, None)


def test_a_retired_coordinators_stamp_arms_nothing_and_reads_the_clock_once() -> None:
    stamp = stamp_mode_change(_clock(100.0), lambda: 1.7e9, retired=True, hold=600.0)
    assert stamp == ModeStamp(at=100.0, wall=1.7e9, hold_end=None)
