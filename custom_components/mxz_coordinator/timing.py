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

from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .const import HEALTH_REPORT_SENSITIVE, UNAVAILABLE_STATES
from .logic import CLOSED_READ, HeadSight, comfort_stale_in


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


@dataclass(frozen=True, slots=True)
class CancelAll:
    """Every wakeup of this name cancelled, whatever its key."""

    name: str


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


def startup_quiet(*, running: bool, startup_ran: bool) -> bool:
    """Whether MXZ's start is still quiet: it was set up before Home Assistant was running
    (``running`` False), and its ``startup`` wakeup, ``STARTUP_RECOVER_DELAY`` after Home
    Assistant started, has not run yet (``startup_ran``; ``sim_timing.start`` arms it). While
    quiet, a change at a head is not adopted (``planner.AdoptGates.starting``) and a room sensor
    with no usable reading waits to warn. A reload while Home Assistant runs is never quiet."""
    return not running and not startup_ran


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


def hold_end_arm(remaining: float, *, retired: bool) -> Arm:
    """The hold-end wakeup for a mode-change hold with ``remaining`` seconds left; a retired
    coordinator cancels it."""
    return Arm("hold_end", None, None if retired else hold_end_delay(remaining))


@dataclass(frozen=True, slots=True)
class Watched:
    """A room's head as the lost and settle wakeups read it.

    ``by_action``: a hands-off room whose vote follows its head's run. ``released``: its
    hands-off phase is released.
    """

    sight: HeadSight
    by_action: bool
    released: bool


@dataclass(frozen=True, slots=True)
class CloudWaits:
    """A cloud head's deadlines as its record holds them.

    ``pending``: its confirm window is open, until ``window_until``. ``release_at``: when a
    batch or fan step its limit held back may go. ``seen``: when it last showed it is there
    (wall clock) and the limit after which it is out of MXZ's control (None for none); not
    read for a head already out (``unresponsive``).
    """

    pending: bool
    window_until: float
    release_at: float | None
    unresponsive: bool
    seen: tuple[float, int | None] | None


def cloud_arm(
    retired: bool, clouds: Sequence[CloudWaits], clock: float, wall: float
) -> Arm:
    """The one wakeup the cloud heads share: at the nearest deadline still ahead.

    The end of a confirm window (confirm, or send again), a batch or fan step held back by
    its limit, and the moment a quiet head would become not controllable. None for a retired
    coordinator or no cloud head.
    """
    if retired or not clouds:
        return Arm("cloud", None, None)
    waits: list[float] = []
    for cloud in clouds:
        if cloud.pending:
            waits.append(cloud.window_until - clock)
        if cloud.release_at is not None and cloud.release_at > clock:
            waits.append(cloud.release_at - clock)
        if not cloud.unresponsive and cloud.seen is not None:
            since, limit = cloud.seen
            if limit is not None:
                waits.append(since + limit - wall)
    return Arm("cloud", None, cloud_delay(waits))


@dataclass(frozen=True, slots=True)
class Wakeups:
    """What the wakeups armed after a compute read.

    ``retired``: this coordinator is retired; ``clock_retired``: its clock is too (a unit
    switch retires the coordinator and reloads it, with the clock still running).
    ``coordinator`` and ``standby``: the Coordinator switch is on, and a standby hold parks
    every head at one fixed mode. ``clock`` and ``wall``: MXZ's clock and the wall clock, read
    once. ``remaining``, ``allowed`` and ``held_back``: what :func:`dwell_delay` reads.
    ``coasted``: a room let go of a run. ``fresh``: each room's freshness deadline (wall
    clock). ``sights``: every head's record; ``rooms``: each room's, as the lost and settle
    wakeups read it; ``lost_ttl`` and ``action_hold``: how long a lost head's vote lasts and
    a stopped head's vote is held. ``comfort_stale_after`` and ``comfort``: when an "on"
    Comfort priority goes stale (None: never), and each room's entity (domain, state, age).
    ``clouds``: each cloud head's deadlines. ``openings``: each door/window room's slug and
    the end of its delay (wall clock; None with no delay running).
    """

    retired: bool
    clock_retired: bool
    coordinator: bool
    standby: bool
    clock: float
    wall: float
    remaining: float
    allowed: bool
    held_back: Callable[[], bool]
    coasted: bool
    fresh: Sequence[float]
    sights: Sequence[HeadSight]
    rooms: Sequence[Watched]
    lost_ttl: float
    action_hold: float
    comfort_stale_after: float | None
    comfort: Sequence[tuple[str | None, str | None, float | None]]
    clouds: Sequence[CloudWaits]
    openings: Sequence[tuple[str, float | None]]


def arms(w: Wakeups) -> tuple[Arm | CancelAll, ...]:
    """Every wakeup a compute leaves armed or cancels, in the order the coordinator arms them.

    Each keeps exactly one wait for what it watches, re-armed from this compute's readings,
    so it moves when a reading moves it and goes when nothing is left to wait for. Nothing is
    captured: each wakeup only asks for what ``WAKEUPS`` says, which re-reads everything.

    - ``dwell``: a flip the mode-change hold holds back (:func:`dwell_delay`), while the
      coordinator is on, its clock not retired, and no fixed standby park holds.
    - ``coast``: just after a room coasts, while the coordinator is on and not parked.
    - ``fresh``: the nearest sensor freshness deadline.
    - ``lost``: the nearest lost-vote expiry, and the nearest end of a stopped head's held
      vote in a hands-off room that votes by its run.
    - ``settle``: the nearest end of a settle window of a released hands-off head that votes
      by its run; none once retired.
    - ``comfort``: the moment the nearest "on" Comfort priority goes stale; none once retired
      or with no stale time set.
    - ``cloud``: :func:`cloud_arm`.
    - ``opening``: every room's cancelled, then, unless retired, one at the end of each
      room's open or close delay.
    """
    out: list[Arm | CancelAll] = [
        Arm("dwell", None, dwell_delay(
            w.remaining,
            active=not (w.clock_retired or not w.coordinator or w.standby),
            allowed=w.allowed,
            held_back=w.held_back,
        )),
        Arm("coast", None, coast_delay(w.coasted, active=w.coordinator and not w.standby)),
        Arm("fresh", None, fresh_delay(w.fresh, w.wall)),
        # The boundary itself, as for freshness: expiry is half-open, so the refresh this
        # runs reads the vote as expired.
        Arm("lost", None, nearest([
            *(sight.expires_in(w.clock, w.lost_ttl) for sight in w.sights),
            *(room.sight.hold_ends_in(w.clock, w.action_hold) for room in w.rooms if room.by_action),
        ])),
        Arm("settle", None, None if w.retired else nearest(
            room.sight.settles_in(w.clock) for room in w.rooms if room.by_action and room.released
        )),
        Arm("comfort", None, None if w.retired or w.comfort_stale_after is None else nearest(
            comfort_stale_in(domain, state, age=age, stale_after=w.comfort_stale_after)
            for domain, state, age in w.comfort
        )),
        cloud_arm(w.retired, w.clouds, w.clock, w.wall),
        CancelAll("opening"),
    ]
    if not w.retired:
        out.extend(
            Arm("opening", slug, opening_delay(due - w.wall))
            for slug, due in w.openings if due is not None
        )
    return tuple(out)
