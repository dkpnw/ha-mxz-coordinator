"""A person's write to a room's target, range or enable, called directly: no pytest-homeassistant.

planner.target_written, enable_written and range_target: what the write does to the room's
adopt hold, engage latch, startup seed and hands-off release, in order. Each expectation is
the rule the coordinator states (``reset_engage_latch``, ``set_room_enable``,
``set_zone_range``), worked by hand; each branch has a case where it is taken and one where it
is not. Every head and value is invented.
"""

from __future__ import annotations

import itertools

from custom_components.mxz_coordinator.planner import (
    HeadReport,
    Latch,
    ReleaseEnded,
    SeedDropped,
    enable_written,
    range_target,
    target_written,
)
from custom_components.mxz_coordinator.timing import Arm

HEAD = "climate.a"
DROP = HeadReport(pending=None, hold=Arm("heal", (HEAD, "adopt"), None), disown=False, replan=False)
FRESH = (SeedDropped(), Latch(""))


def test_a_target_write_on_a_live_enabled_room_forgets_its_latch() -> None:
    assert target_written(HEAD, startup=False, coordinator=True, enabled=True) == (DROP, Latch(None))


def test_a_target_write_decides_afresh_while_a_seed_waits_the_switch_is_off_or_the_room_is_off() -> None:
    for startup, coordinator, enabled in itertools.product((False, True), repeat=3):
        if not startup and coordinator and enabled:
            continue
        steps = target_written(HEAD, startup=startup, coordinator=coordinator, enabled=enabled)
        assert steps == (DROP, *FRESH), (startup, coordinator, enabled)


def test_a_target_write_ends_the_adopt_hold_first() -> None:
    assert target_written("climate.b", startup=True, coordinator=True, enabled=True)[0] == HeadReport(
        pending=None, hold=Arm("heal", ("climate.b", "adopt"), None), disown=False, replan=False,
    )


def test_turning_a_room_on_from_off_ends_its_release_and_decides_afresh() -> None:
    assert enable_written(HEAD, was_on=False, on=True) == (ReleaseEnded(), *FRESH, DROP)


def test_turning_an_enabled_room_on_again_only_ends_its_release() -> None:
    assert enable_written(HEAD, was_on=True, on=True) == (ReleaseEnded(), DROP)


def test_turning_a_room_off_decides_afresh_and_keeps_its_release() -> None:
    assert enable_written(HEAD, was_on=True, on=False) == (*FRESH, DROP)
    assert enable_written(HEAD, was_on=False, on=False) == (*FRESH, DROP)


def test_a_ranges_target_is_its_middle() -> None:
    assert range_target(66.0, 74.0) == 70.0
    assert range_target(20.5, 23.0) == 21.75
    assert range_target(68.0, 71.0) == 69.5
