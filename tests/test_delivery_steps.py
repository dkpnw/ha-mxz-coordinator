"""planner.delivery_steps, called directly: no Home Assistant, no pytest-homeassistant.

Each expectation is worked out from the rule it states. A running head the plan parks off has
its fan handed back to auto first, so the next run starts from the bottom of the ladder; a head
that is already off, idles in fan_only, or is held by eco or a standby hold gets no hand-back.
A heat or cool run is the head's last run, which the coil-dry run counts from, and anything but
fan_only ends a coil-dry wakeup. A fixed standby park or a floor (no engage) has no fan
machinery and no coil-dry note. Every value is invented.
"""

from __future__ import annotations

import pytest

from custom_components.mxz_coordinator.const import (
    ENGAGE_SATISFIED,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
)
from custom_components.mxz_coordinator.planner import DeliverySteps, delivery_steps

PARK = {
    "idle_action": IDLE_ACTION_OFF,
    "eco": False,
    "inhibited": False,
    "head_state": MODE_COOL,
}


def steps(act: str = MODE_OFF, engage: str | None = ENGAGE_SATISFIED, **changes: object) -> DeliverySteps:
    return delivery_steps(act, engage, **{**PARK, **changes})  # type: ignore[arg-type]


@pytest.mark.parametrize("idle_action", [IDLE_ACTION_OFF, IDLE_ACTION_OFF_AFTER_DRY])
@pytest.mark.parametrize("head_state", [MODE_COOL, MODE_HEAT, MODE_FAN_ONLY])
def test_a_running_head_parked_off_is_handed_back_then_its_rung_is_dropped(
    idle_action: str, head_state: str,
) -> None:
    result = steps(idle_action=idle_action, head_state=head_state)
    assert result == DeliverySteps(
        handback=True, last_run=False, ends_coil_dry=True, fan_machinery=True,
    )
    # The hand-back was decided before the write: a hold read after it still drops the rung.
    assert result.fan(inhibited=False) == "drop_rung"
    assert result.fan(inhibited=True) == "drop_rung"


@pytest.mark.parametrize(
    ("act", "engage", "changes"),
    [
        (MODE_FAN_ONLY, ENGAGE_SATISFIED, {}),
        (MODE_COOL, MODE_COOL, {}),
        (MODE_OFF, ENGAGE_SATISFIED, {"idle_action": IDLE_ACTION_FAN_ONLY}),
        (MODE_OFF, MODE_OFF, {}),
        (MODE_OFF, ENGAGE_SATISFIED, {"eco": True}),
        (MODE_OFF, ENGAGE_SATISFIED, {"inhibited": True}),
        (MODE_OFF, ENGAGE_SATISFIED, {"head_state": None}),
        (MODE_OFF, ENGAGE_SATISFIED, {"head_state": MODE_OFF}),
        (MODE_OFF, ENGAGE_SATISFIED, {"head_state": "unavailable"}),
        (MODE_OFF, ENGAGE_SATISFIED, {"head_state": "unknown"}),
    ],
    ids=[
        "not_off", "a_run", "idle_fan_only", "room_off", "eco", "standby", "no_head",
        "head_off", "head_unavailable", "head_unknown",
    ],
)
def test_no_hand_back_unless_every_condition_holds(act: str, engage: str, changes: dict) -> None:
    assert steps(act, engage, **changes).handback is False


@pytest.mark.parametrize("act", [MODE_COOL, MODE_HEAT])
def test_a_run_is_the_last_run_ends_a_coil_dry_wait_and_gets_the_boost_step(act: str) -> None:
    result = steps(act, act)
    assert result == DeliverySteps(
        handback=False, last_run=True, ends_coil_dry=True, fan_machinery=True,
    )
    assert result.fan(inhibited=False) == "boost"


def test_fan_only_is_no_run_and_keeps_a_coil_dry_wait() -> None:
    result = steps(MODE_FAN_ONLY)
    assert result == DeliverySteps(
        handback=False, last_run=False, ends_coil_dry=False, fan_machinery=True,
    )
    assert result.fan(inhibited=False) == "boost"


def test_an_off_with_no_hand_back_is_no_run_ends_a_coil_dry_wait_and_gets_the_boost_step() -> None:
    result = steps(head_state=MODE_OFF)
    assert result == DeliverySteps(
        handback=False, last_run=False, ends_coil_dry=True, fan_machinery=True,
    )
    assert result.fan(inhibited=False) == "boost"


@pytest.mark.parametrize("act", [MODE_COOL, MODE_OFF, MODE_FAN_ONLY])
def test_a_standby_hold_has_no_fan_step_but_still_counts_the_run(act: str) -> None:
    result = steps(act, MODE_COOL if act == MODE_COOL else ENGAGE_SATISFIED, inhibited=True)
    assert result.fan_machinery is True
    assert result.fan(inhibited=True) is None
    assert result.handback is False
    assert result.last_run is (act == MODE_COOL)
    assert result.ends_coil_dry is (act != MODE_FAN_ONLY)


@pytest.mark.parametrize("act", [MODE_OFF, MODE_FAN_ONLY, MODE_COOL, MODE_HEAT])
def test_a_fixed_park_or_a_floor_has_no_note_and_no_fan_machinery(act: str) -> None:
    result = steps(act, None)
    assert result == DeliverySteps(
        handback=False, last_run=False, ends_coil_dry=False, fan_machinery=False,
    )
    assert result.fan(inhibited=False) is None
    assert result.fan(inhibited=True) is None


@pytest.mark.parametrize(("before", "after", "fan"), [(False, True, None), (True, False, "boost")])
@pytest.mark.parametrize("act", [MODE_COOL, MODE_FAN_ONLY, MODE_OFF])
def test_the_fan_step_follows_the_hold_as_read_when_it_runs(
    act: str, before: bool, after: bool, fan: str | None,
) -> None:
    """A hold that starts during the mode write stops the boost step; one that ends lets it
    run. The early read decides only the hand-back (none here: under a hold, or a head off)."""
    result = steps(
        act, MODE_COOL if act == MODE_COOL else ENGAGE_SATISFIED,
        inhibited=before, head_state=MODE_OFF,
    )
    assert result.handback is False
    assert result.fan(inhibited=after) == fan
