"""MXZ's own timing: what each input asks of the plan, each wakeup's delay, the heartbeat.

Standard library only (tools/import_bounds.py). The coordinator and its controllers read Home
Assistant and the clock, call these, and arm what they say on ``clock.Clock``. ``sim_timing``
runs the same rules for a simulator, on a model of Home Assistant's request debouncer.

A refresh is asked for in one of three ways (``Refresh``). A head's report asks what
``head_refresh`` says, a room sensor's unchanged report what ``same_reading_refresh`` says, and
each wakeup what ``WAKEUPS`` says when it runs. A room switched to or from a dual setpoint in
place, and MXZ's own start, refresh at once. Every other input asks ``Refresh.REQUEST``: a room
reading that changed, a person's change to one of MXZ's controls (a room's target, range or
enable, the shared-mode select, the Fan auto switch), a Safety or Comfort priority entity, a door
or window, the standby hold, the ``mxz_recompute`` event and the heartbeat.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .const import HEALTH_REPORT_SENSITIVE, UNAVAILABLE_STATES
from .logic import CLOSED_READ


class Refresh(StrEnum):
    """How an input asks for a compute.

    ``REQUEST``: through the request debouncer, as new input. ``REPORT``: a head's report,
    through the debouncer, never as new input (``DeliveryQueue.request_for_report``). ``NOW``: at
    once, past the request cooldown (``async_refresh_now``).
    """

    REQUEST = "request"
    REPORT = "report"
    NOW = "now"


@dataclass(frozen=True, slots=True)
class Arm:
    """A wakeup armed ``delay`` seconds from now, in place of the one of this name and key;
    a delay of None cancels it."""

    name: str
    key: Hashable
    delay: float | None


# Seconds the request debouncer waits after a refresh it ran before it runs another.
REQUEST_COOLDOWN = 10
# Seconds between two heartbeats: a refresh even when nothing reports.
HEARTBEAT = 900
# A run that coasts to its target is looked at again just after the cooldown it may have started.
COAST_FOLLOWUP_DELAY = REQUEST_COOLDOWN + 1

# What each wakeup asks for when it runs. None asks nothing by itself: the end of the
# mode-change hold only redraws the entities, and a heal or adopt hold judges first.
WAKEUPS: Mapping[str, Refresh | None] = {
    "fresh": Refresh.REQUEST,
    "dwell": Refresh.REQUEST,
    "coast": Refresh.REQUEST,
    "dry": Refresh.REQUEST,
    "lost": Refresh.REQUEST,
    "comfort": Refresh.REQUEST,
    "cloud": Refresh.REQUEST,
    "startup": Refresh.REQUEST,
    "dry_pick": Refresh.REQUEST,
    "settle": Refresh.NOW,
    "opening": Refresh.NOW,
    "hold_end": None,
    "heal": None,
}


def head_refresh(*, run_changed: bool, moved: bool) -> Refresh | None:
    """What a head's report asks for.

    ``run_changed``: the head of a released room that votes by its run started or stopped one
    (``run_changed``); that counts at once, not after a cooldown. ``moved``: the report showed
    something the plan reads (``head_shows_new``, or a change one of the head's rules waits for).
    """
    if run_changed:
        return Refresh.NOW
    return Refresh.REPORT if moved else None


def run_changed(old_mode: str, new_mode: str, old_action: Any, new_action: Any) -> bool:
    """Whether a head's report starts or stops a run: its mode or its action changed."""
    return new_mode != old_mode or new_action != old_action


def head_shows_new(
    old_mode: str | None, new_mode: str | None,
    old_attributes: Mapping[str, Any], new_attributes: Mapping[str, Any],
) -> bool:
    """Whether a head's report changed its fan, its fan list or features, or its availability.

    A mode of None is no state at all, with no attributes.
    """
    capabilities_changed = any(
        type(new_attributes.get(key)) is not type(old_attributes.get(key))
        or new_attributes.get(key) != old_attributes.get(key)
        for key in ("fan_modes", "supported_features")
    )
    availability_changed = (new_mode is None or new_mode in UNAVAILABLE_STATES) != (
        old_mode is None or old_mode in UNAVAILABLE_STATES
    )
    return (
        new_attributes.get("fan_mode") != old_attributes.get("fan_mode")
        or capabilities_changed
        or availability_changed
    )


def same_reading_refresh(healths: Iterable[str | None]) -> Refresh | None:
    """What a room sensor's report of an unchanged value asks for, by the health of each room
    it serves: only a room waiting for a report (stale, invalid, or in its startup grace) asks."""
    return Refresh.REQUEST if any(health in HEALTH_REPORT_SENSITIVE for health in healths) else None


def nearest(waits: Iterable[float | None]) -> float | None:
    """The soonest of these waits, None for none: one wakeup serves them all."""
    found = [wait for wait in waits if wait is not None]
    return min(found) if found else None


def fresh_delay(deadlines: Iterable[float], now: float) -> float | None:
    """The fresh wakeup: at the nearest sensor freshness deadline (wall clock), none without one.

    The deadline itself, with nothing added: the refresh it runs reads the room as stale.
    """
    soonest = nearest(deadlines)
    return None if soonest is None else max(soonest - now, 0.0)


def dwell_delay(
    remaining: float, *, active: bool, allowed: bool, held_back: Callable[[], bool]
) -> float | None:
    """The dwell wakeup: when the mode-change hold ends, while it holds a change back.

    ``remaining``: seconds the hold has left (-inf with no change on record, 0 or less once it
    ended). ``active``: the coordinator is on, not retired, and no standby hold parks every
    head. ``allowed``: the plan just decided had the hold open. ``held_back``: whether the hold
    is what keeps the plan from another mode (``planner.held_back``), read only when it matters.
    A hold that ended while the plan was applied wakes at once.
    """
    if not active:
        return None
    if remaining <= 0 and allowed:
        return None
    if not held_back():
        return None
    return remaining + 1 if remaining > 0 else 0


def coast_delay(coasted: bool, *, active: bool) -> float | None:
    """The coast wakeup: a compute just after a run coasted, while the coordinator can act."""
    return COAST_FOLLOWUP_DELAY if active and coasted else None


def coil_dry_delay(left: float) -> float:
    """The coil-dry wakeup: just after a parked head's coil-dry run ends (``planner.idle_park``)."""
    return left + 1


def cloud_delay(waits: Iterable[float]) -> float | None:
    """The cloud wakeup: the nearest cloud head deadline still ahead."""
    return nearest(wait for wait in waits if wait > 0)


def opening_wait(reading: str, *, open_delay: float, close_delay: float) -> float:
    """A door or window's new reading: its room's wakeup is at the end of this delay."""
    return close_delay if reading == CLOSED_READ else open_delay


def opening_delay(delay: float) -> float:
    """A door/window wakeup: at the end of the room's delay, at once for one already over."""
    return max(0.0, delay)


def hold_end_delay(remaining: float) -> float | None:
    """The hold-end wakeup: just after the mode-change hold ends, none with no hold left."""
    return remaining + 1 if remaining > 0 else None
