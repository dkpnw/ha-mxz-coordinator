"""Freeze floor and overheat ceiling (opt-in, disabled rooms): runs, hand-backs, notices.

A disabled room with a floor or ceiling set has MXZ run its head when the room
passes it, until the room reaches its clear line. The trip and clear lines and a
run's progress are decided in ``logic.floor_call``/``logic.floor_progress``; this
module keeps each room's run, a person's setting it took over and gives back,
a yield to a person, the notices and their hourly limit, and the record the
room's enable switch restores. The coordinator calls it at the points listed on
``FloorGuard``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    FLOOR_NOTICE_SECONDS,
    FLOOR_STALL_SECONDS,
    FLOOR_YIELD_SECONDS,
    MODE_COOL,
    MODE_HEAT,
    UNAVAILABLE_STATES,
)
from .host import ControllerHost, Effect
from .logic import (
    FloorRun,
    as_float,
    finite_number,
    floor_call,
    floor_progress,
    setpoints,
)

if TYPE_CHECKING:
    from .coordinator import Zone
    from .delivery import Delivery, DeliveryQueue
    from .heads import HeadDrivers

_LOGGER = logging.getLogger(__package__)


class FloorHost(ControllerHost, Protocol):
    """The coordinator members the floor reads or calls, beyond every controller's.

    Read: the kill switch and the standby park (whether a run is driven), the
    unit, step, clamp band and setpoint band (a run's clear line and a
    message's unit), the notification service, startup quiet, a stale room
    sensor's window, a running vane kick, the head drivers (a cloud head, a
    head's grid) and the delivery queue (whether a head is busy). Called: the
    queue, for a hand-back and a yield; the hands-off phase and release, which
    a run takes and gives back.
    """

    coordinator_enable: bool
    celsius: bool
    target_step: float
    clamp_min: float
    clamp_max: float
    setpoint_band: float
    notify_service: str | None

    @property
    def startup_quiet(self) -> bool: ...
    @property
    def deliveries(self) -> DeliveryQueue: ...
    @property
    def heads(self) -> HeadDrivers: ...

    def parked_by_standby(self) -> bool: ...
    def stale_window(self, slug: str) -> float | None: ...
    def vane_kick_running(self, climate_id: str) -> bool: ...
    def hands_off_phase(self, zone: Zone) -> str | None: ...
    def hands_off_released(self, slug: str) -> bool: ...
    def set_hands_off_released(self, slug: str, released: bool) -> None: ...


class FloorGuard:
    """Each disabled room's freeze floor or overheat ceiling: run, hand-back, notices.

    The coordinator calls it from the compute (``want``, ``settle``,
    ``attributes``), the apply (``take_handbacks``, ``queue_handback``,
    ``clear``, ``taken_over``, ``expect_command``), a head's report
    (``yield_to_person``), the hands-off phase and the adoption gate
    (``tripped``), and its setup (``start``). The delivery queue and the head
    drivers call ``rejected`` and ``accepted``; the room thermostat calls
    ``tripped``; the enable switch calls ``record`` and ``restore``.
    """

    def __init__(
        self, host: FloorHost, entry_id: str, zones: list[Zone], margin: float
    ) -> None:
        """Per room: the tripped run's progress record; whether the last
        compute found MXZ driving its head in the floor's direction (the
        no-progress clock runs only then) and when that compute ran, on the
        monotonic clock (in memory only); a hands-off room's yield to a person
        (direction, wall-clock time: it is stored and shown); and what the
        plan shows. ``margin`` is the clear line's, a constant: the unit's
        default demand threshold.
        """
        self._host = host
        self._entry_id = entry_id
        self._zones = zones
        self.margin = margin
        self._runs: dict[str, FloorRun] = {}
        self._driving: dict[str, bool] = {}
        self._eval_ts: dict[str, float] = {}
        self._yield: dict[str, tuple[str, float]] = {}
        self._view: dict[str, tuple[str, str | None]] = {}
        # A hands-off run MXZ took over from a person already running the
        # head in the floor's direction: their (mode, low, high), given back
        # when the run ends (P3); a hand-back for the next apply to send, as
        # (theirs, which try, the head as MXZ left it: None on the first try);
        # one in flight, as (theirs, which try, the head as MXZ left it, its
        # delivery); and a refused one's notice, kept while the head still
        # shows MXZ's command, as (message, that head). While MXZ runs a head
        # over a person's own run: the setpoints it last queued for it, as the
        # head should show them, and what the head showed then (P15).
        self._prior: dict[str, tuple[str, float, float]] = {}
        self._expect: dict[str, tuple[tuple[float, float], tuple[Any, ...] | None]] = {}
        self._handback: dict[str, tuple[tuple[str, float, float], int, tuple[Any, ...] | None]] = {}
        self._handing_back: dict[
            str, tuple[tuple[str, float, float], int, tuple[Any, ...], Delivery | None]
        ] = {}
        self._handback_note: dict[str, tuple[str, tuple[Any, ...]]] = {}
        # One notice per episode: a blind room, a rejected floor command.
        self._blind_noticed: set[str] = set()
        self._reject_noticed: set[str] = set()
        # The rate limit (P5): when each room's last floor notice went out,
        # on the monotonic clock, and the notices held back since (each
        # logged once). In memory only.
        self._notice_at: dict[str, float] = {}
        self._held_back: dict[str, set[str]] = {}
        # A trip notice the limit held back, sent once the hour is up if its
        # run is still going then (P8).
        self._trip_held: dict[str, str] = {}
        # A head that rejected MXZ's floor command and has accepted none
        # since: its error. The plan then shows held, not heat/cool.
        self._rejecting: dict[str, str] = {}
        # Inert until start: the seed compute runs before the switches
        # restore, with every room still reading disabled.
        self.live = False

    def start(self) -> None:
        """The switches have restored: from now on each compute decides."""
        self.live = True

    def tripped(self, slug: str) -> bool:
        """Whether this room's floor is tripped and wants its direction now."""
        run = self._runs.get(slug)
        return run is not None and not run.stalled

    def clear(self, zone: Zone, direction: str) -> float:
        """The clear line of a run: the room is fine again from here."""
        if direction == MODE_HEAT:
            return float(cast("float", zone.floor_low)) + self.margin
        return float(cast("float", zone.floor_high)) - self.margin

    def want(
        self, zone: Zone, temp: float, ok: bool, now: float, out: list[Effect]
    ) -> str | None:
        """Advance this room's floor by one compute; return its want, if any.

        Only a disabled room with a floor or ceiling configured can have one.
        No usable reading (invalid, missing or stale: ``ok`` is the demand
        path's own test) or an unavailable head ends a run at once and casts
        nothing: a floor never acts blind (decision 4). A run that MXZ has
        driven for the cap without a step of new best reading is ``blind`` as
        well, until the room clears or reaches a new low (decision 11). The
        clock is charged only for time the LAST compute found the head being
        driven (see :meth:`settle`), so a wait, a standoff loss,
        Coordinator off or Home Assistant downtime costs nothing. The notices
        it decides go to ``out``, for the coordinator to send after the compute.
        """
        host = self._host
        slug = zone.slug
        # Driven time is counted on the monotonic clock, so a wall-clock step
        # cannot stall a run early or hold one back. ``now`` (wall clock)
        # stays for the yield, which is stored and shown as a time.
        clock = host.clock()
        driven_for = (
            clock - self._eval_ts[slug]
            if self._driving.get(slug) and slug in self._eval_ts
            else 0.0
        )
        self._eval_ts[slug] = clock
        self._driving[slug] = False
        configured = zone.floor_low is not None or zone.floor_high is not None
        if not self.live:
            # The seed compute: nothing is restored yet, so nothing is decided.
            self._view[slug] = ("armed" if configured else "none", None)
            return None
        self._flush_handback(zone, host.hass.states.get(zone.climate_id), out)
        if not configured or zone.enable:
            self._end(slug)
            self._yield.pop(slug, None)
            self._blind_noticed.discard(slug)
            self._view[slug] = ("enabled" if configured else "none", None)
            return None
        head = host.hass.states.get(zone.climate_id)
        # Before anything ends the run: the end gives back their latest.
        self._follow_person(zone, head)
        if not ok:
            self._end(slug, hand_back=True)
            if (window := host.stale_window(slug)) is not None:
                # Name the window that ran out and the two ways out of it: a
                # change-only sensor goes quiet in a room that holds steady.
                self._blind(
                    zone, f"Room sensor {zone.sensor_id} sent no report in "
                    f"{_span(window)}, its freshness limit, "
                    "so it is stale: no floor protection until it reports "
                    "again. A sensor that reports only when its reading "
                    "changes goes quiet in a steady room: clear Maximum age "
                    f"(Configure → Rooms → {zone.name} → Sensor freshness), "
                    "or use a sensor that reports periodically.", out,
                )
                return None
            self._blind(
                zone, "Room sensor unavailable, invalid or stale: no floor "
                "protection until it reports a usable reading.", out,
            )
            return None
        if head is None or head.state in UNAVAILABLE_STATES:
            self._end(slug, hand_back=True)
            self._blind(
                zone, f"Head {zone.climate_id} unavailable: no floor protection "
                "until it reports again.", out,
            )
            return None
        if (yielded := self._yield.get(slug)) is not None:
            direction, since = yielded
            cleared = floor_call(
                temp=temp, sensor_ok=True, low=zone.floor_low, high=zone.floor_high,
                prior=direction, margin=self.margin,
            ) != direction
            if not cleared and now - since < FLOOR_YIELD_SECONDS:
                self._blind_noticed.discard(slug)
                rearm = dt_util.utc_from_timestamp(since + FLOOR_YIELD_SECONDS)
                self._view[slug] = ("yielded", (
                    "A person changed the head during a floor run, so MXZ left "
                    "it to them. The floor re-arms when the room reaches its "
                    f"clear line or at {rearm.isoformat(timespec='minutes')}."
                ))
                return None
            self._yield.pop(slug)
        run = self._runs.get(slug)
        was_stalled = run is not None and run.stalled
        if run is not None:
            run = floor_progress(
                run, temp=temp, driven_for=driven_for,
                step=host.target_step, cap=FLOOR_STALL_SECONDS,
            )
        want = floor_call(
            temp=temp, sensor_ok=True, low=zone.floor_low, high=zone.floor_high,
            prior=run.direction if run is not None else None,
            margin=self.margin,
        )
        if run is not None and want != run.direction:
            # Cleared (or crossed straight to the other trip).
            self._end(slug, hand_back=True)
            run = None
        if run is None:
            if want is None:
                self._blind_noticed.discard(slug)
                self._view[slug] = ("armed", None)
                return None
            run = FloorRun(direction=want, mark=temp, worst=temp)
            self._notify_trip(zone, want, temp, out)
        elif was_stalled and not run.stalled:
            self._notify_trip(zone, run.direction, temp, out)  # a new low re-armed it
        elif (
            not run.stalled
            and (held := self._trip_held.get(slug)) is not None
            and self._send(zone, "trip", held, out)  # the hour is up (P8)
        ):
            del self._trip_held[slug]
        self._runs[slug] = run
        if run.stalled:
            self._hand_back(slug)
            step = f"{host.target_step:g} {self._unit_symbol()}"
            self._blind(
                zone, f"Stalled: {FLOOR_STALL_SECONDS // 3600} h of driving the head "
                f"without {step} of progress. Re-arms when the room reaches its "
                f"clear line or a new {'low' if run.direction == MODE_HEAT else 'high'}.",
                out, kind="stalled",
            )
            return None
        self._blind_noticed.discard(slug)
        if zone.hands_off:
            # The run is MXZ's: the room parks again when it ends (decision 5),
            # unless MXZ took the head over a person's own run that way (P3).
            if host.hands_off_released(slug):
                self._take_over(zone, run.direction, head)
            host.set_hands_off_released(slug, False)
        self._view[slug] = (run.direction, None)  # settled after arbitration
        return run.direction

    def settle(self, floors: list[str | None], state: str) -> None:
        """After arbitration: is each tripped floor driven now, or held?

        Driven means the plan runs the head in the floor's direction and the
        apply will send it: Coordinator on, no fixed standby park. Only then
        does the no-progress clock run until the next compute.
        """
        host = self._host
        active = host.coordinator_enable and not host.parked_by_standby()
        for zone, want in zip(self._zones, floors):
            if want is None:
                continue
            driving = active and want == state
            self._driving[zone.slug] = driving
            if driving:
                continue
            if not host.coordinator_enable:
                reason = "Coordinator is off: MXZ sends nothing."
            elif host.parked_by_standby():
                reason = "A standby hold parks every head."
            else:
                reason = (
                    f"Waiting: the shared mode is {state} (the mode-change wait, or "
                    "a higher-ranked want). The head is held off meanwhile."
                )
            self._view[zone.slug] = ("held", reason)

    def _end(self, slug: str, *, hand_back: bool = False) -> None:
        """End this room's run, if any. A hands-off room then parks
        (decision 5), unless ``hand_back`` and the run was taken over from a
        person: see :meth:`_hand_back`. Without ``hand_back`` (the room
        was enabled, the floor removed, a person changed the head) the
        person's earlier run is forgotten."""
        self._runs.pop(slug, None)
        self._trip_held.pop(slug, None)
        self._reject_noticed.discard(slug)
        self._rejecting.pop(slug, None)
        self._expect.pop(slug, None)
        if hand_back:
            self._hand_back(slug)
        else:
            self._prior.pop(slug, None)

    def _take_over(self, zone: Zone, direction: str, head: Any) -> None:
        """A hands-off run starts on a released room. If the person is
        running the head in the floor's direction already, remember their
        mode and setpoint to give back (P3). A head with no setpoint to read
        gets the clear line's."""
        if head.state != direction:
            return  # off, another mode: decision 5 (back to off)
        if self._host.heads.cloud(zone.climate_id) is not None:
            # Not on a cloud head (beta): giving a person's run back reads
            # their setpoints off the head, and a cloud head shows MXZ's
            # offset setpoint, minutes late. Its run ends as decision 5's.
            return
        if (setting := _floor_setting(head)) is None:
            setting = self._line(zone, direction)
        self._prior[zone.slug] = (direction, *setting)

    def _line(self, zone: Zone, direction: str) -> tuple[float, float]:
        """The setpoints of a run's clear line, as the apply computes
        them before it keeps a person's warmer (cooler) ones."""
        host = self._host
        return setpoints(
            mode=direction, target=self.clear(zone, direction), eco=False,
            clamp_min=host.clamp_min, clamp_max=host.clamp_max,
            band=host.setpoint_band, step=host.target_step,
        )

    def _as_sent(
        self, zone: Zone, head: Any, mode: str, low: float, high: float
    ) -> tuple[float, float]:
        """(low, high) as the head should show them once MXZ sends them:
        clamped to the head's band, and its one setpoint on a single-setpoint
        head, as :meth:`heads.LocalDriver._async_apply` sends them."""
        low, high = self._host.heads.grid.clamp(zone.climate_id, low, high)
        if head is not None and not (
            int(head.attributes.get("supported_features") or 0) & 2
        ):
            low = high = high if mode == MODE_COOL else low
        return (low, high)

    def taken_over(self, slug: str) -> tuple[str, float, float] | None:
        """The person's (mode, low, high) this room's run took over, if any."""
        return self._prior.get(slug)

    def expect_command(
        self, zone: Zone, mode: str, low: float, high: float
    ) -> None:
        """MXZ queued (low, high) for a head it runs over a person's own run:
        remember what the head should show once it lands
        (:meth:`_as_sent`) and what it showed until then. Neither is a
        person's change (P15). The same command to a head already showing it
        replaces nothing, so what it showed before stays."""
        head = self._host.hass.states.get(zone.climate_id)
        low, high = self._as_sent(zone, head, mode, low, high)
        setting = None if head is None else _floor_setting(head)
        known = self._expect.get(zone.slug)
        if known is not None and known[0] == (low, high) and (
            self._shows(zone.climate_id, setting, (low, high))
        ):
            return
        self._expect[zone.slug] = ((low, high), setting)

    def _shows(
        self, climate_id: str, setting: tuple[Any, ...] | None, want: tuple[float, float]
    ) -> bool:
        """Whether a head showing ``setting`` already holds ``want``, read on
        its own grid as the idempotent check reads it."""
        return setting is not None and all(
            self._host.heads.grid.already_at(climate_id, seen, edge)
            for seen, edge in zip(setting, want)
        )

    def _follow_person(self, zone: Zone, head: Any) -> None:
        """A person changed only the setpoints of a head MXZ runs over their
        own run: that is their setting now. The run keeps it where it protects
        more (P10), and it is what they get back at the end (P15).

        It is a person's change only if the head is in the run's mode, MXZ has
        nothing in flight to it and is not kicking its vanes, and it shows
        neither MXZ's last command nor the run's clear line (on the head's own
        grid, as the idempotent check reads it), nor what it showed before
        that last command (a late report from before it landed). MXZ may have
        sent the clear line at any point in the run, so a late report of it
        is never theirs (P17), even after they changed it since. Until MXZ
        has commanded the head in this run (after a restart, its first
        apply), nothing is taken.
        """
        slug = zone.slug
        prior, expect = self._prior.get(slug), self._expect.get(slug)
        if (
            prior is None or expect is None or head is None
            or head.state != prior[0]
            or self._host.deliveries.busy(zone.climate_id)
            or self._host.vane_kick_running(zone.climate_id)
        ):
            return
        sent, before = expect
        setting = _floor_setting(head)
        line = self._as_sent(
            zone, head, prior[0], *self._line(zone, prior[0])
        )
        if setting is None or setting in (before, prior[1:]) or any(
            self._shows(zone.climate_id, setting, ours)
            for ours in (sent, line)
        ):
            return
        self._prior[slug] = (prior[0], *setting)
        _LOGGER.info(
            "MXZ floor: %s's setpoint was changed by hand during a floor run; "
            "MXZ keeps it where it protects the room and gives it back at the "
            "end", zone.name,
        )

    def _hand_back(self, slug: str) -> None:
        """A hands-off run that MXZ took over from a person's own run in the
        floor's direction ended without that person changing the head's mode
        (cleared, blind or stalled): it is theirs again at once, and the next
        apply sends it their mode and latest setpoint, never off (P3, P15)."""
        self._expect.pop(slug, None)
        if (prior := self._prior.pop(slug, None)) is not None:
            self._host.set_hands_off_released(slug, True)
            self._handback[slug] = (prior, 1, None)

    def take_handbacks(
        self,
    ) -> dict[str, tuple[tuple[str, float, float], int, tuple[Any, ...] | None]]:
        """The hand-backs for this apply, which only it may send (P3): one an
        apply skips (Coordinator off, a standby park, no plan yet) leaves the
        head as MXZ left it."""
        handbacks, self._handback = self._handback, {}
        return handbacks

    def queue_handback(
        self,
        zone: Zone,
        back: tuple[tuple[str, float, float], int, tuple[Any, ...] | None] | None,
    ) -> asyncio.Task[Any] | None:
        """Queue a released room's hand-back (P3), if it has one and its head
        reports: theirs again, as they had it. A retry goes only over MXZ's
        own command, never over a change made since. Returns the delivery's
        task, if one was queued."""
        head = self._host.hass.states.get(zone.climate_id)
        if back is None or head is None or head.state in UNAVAILABLE_STATES:
            return None
        theirs, attempt, left = back
        seen = _head_setting(head)
        if left is not None and seen != left:
            return None
        deliveries = self._host.deliveries
        task = deliveries.queue(zone.climate_id, *theirs)
        if task is not None:
            # A delivery superseded before it reaches the head ends with
            # neither accepted nor rejected, so this entry stays until the
            # next hand-back overwrites it. Harmless: it is matched only by
            # its delivery's identity, never acted on.
            self._handing_back[zone.slug] = (
                theirs, attempt, seen, deliveries.current(zone.climate_id),
            )
        return task

    def _blind(
        self, zone: Zone, reason: str, out: list[Effect], kind: str = "blind"
    ) -> None:
        """Show the room ``blind``; notify once per episode (decision 7).

        A configured room can be blind before anything tripped: it has no
        protection either way. While Home Assistant starts, an unusable sensor
        or head may only be loading, so the notice waits for the post-start
        recompute, which sends it if the room is still blind. A notice the
        rate limit holds back is tried again at each compute while the room
        stays blind, so it goes out once the hour is up.
        """
        self._view[zone.slug] = ("blind", reason)
        if zone.slug in self._blind_noticed or self._host.startup_quiet:
            return
        if self._send(
            zone, kind, f"{zone.name}: the freeze/overheat floor is blind. {reason}", out
        ):
            self._blind_noticed.add(zone.slug)

    def _notify_trip(
        self, zone: Zone, direction: str, temp: float, out: list[Effect]
    ) -> None:
        """Notify a trip (decision 7). One the hourly limit holds back is kept
        and sent at the first compute after the hour while the run is still
        going (P8); a run that ends or stalls first never sends it."""
        unit = self._unit_symbol()
        if direction == MODE_HEAT:
            what = f"below its freeze floor of {zone.floor_low:g} {unit}"
            act = "heating"
        else:
            what = f"above its overheat ceiling of {zone.floor_high:g} {unit}"
            act = "cooling"
        message = (
            f"{zone.name} is at {temp:g} {unit}, {what}. MXZ is {act} this disabled "
            f"room until it reaches {self.clear(zone, direction):g} {unit}."
        )
        if self._send(zone, "trip", message, out):
            self._trip_held.pop(zone.slug, None)
        else:
            self._trip_held[zone.slug] = message

    def attributes(self, zone: Zone) -> dict[str, str | None]:
        """The room's floor state as the plan shows it. A driven floor whose
        head is rejecting MXZ's commands is held, not running."""
        state, reason = self._view[zone.slug]
        err = self._rejecting.get(zone.slug)
        if state in (MODE_HEAT, MODE_COOL) and err is not None:
            state, reason = "held", (
                f"{zone.climate_id} rejected MXZ's floor command ({err}). MXZ "
                "keeps trying at each update."
            )
        return {"floor_state": state, "floor_reason": reason}

    def diagnostics(self, slug: str) -> dict[str, Any]:
        """This room's run, yield and rejecting head, as the debug view shows them."""
        return {
            "floor_run": self._runs.get(slug),
            "floor_yield": self._yield.get(slug),
            "floor_rejecting": self._rejecting.get(slug),
        }

    def rejected(
        self, climate_id: str, err: Exception, delivery: Delivery
    ) -> None:
        """A command to a head whose floor is tripped was rejected: the plan
        shows it held (republished as the delivery ends), and it is notified
        once per run (decision 7); the next ordinary update tries again. A
        notice the rate limit holds back goes out at the first rejection
        after the hour is up. A refused hand-back: see
        :meth:`_handback_refused`."""
        out: list[Effect] = []
        for zone in self._zones:
            sending = self._handing_back.get(zone.slug)
            if sending is not None and sending[3] is delivery:
                del self._handing_back[zone.slug]
                self._handback_refused(zone, sending, err, out)
                continue
            if (
                zone.climate_id == climate_id
                and not zone.enable
                and self.tripped(zone.slug)
            ):
                self._rejecting[zone.slug] = str(err)
                if zone.slug in self._reject_noticed:
                    continue
                if self._send(
                    zone, "rejected",
                    f"{zone.name}: the floor's command to {climate_id} was rejected "
                    f"({err}). MXZ keeps trying at each update.",
                    out,
                ):
                    self._reject_noticed.add(zone.slug)
        for effect in out:
            effect()

    def accepted(self, climate_id: str, delivery: Delivery) -> None:
        """A command to this head returned (or was not needed): it is no
        longer rejecting MXZ's floor, and a hand-back it carried landed."""
        for zone in self._zones:
            if zone.climate_id == climate_id:
                self._rejecting.pop(zone.slug, None)
                sending = self._handing_back.get(zone.slug)
                if sending is not None and sending[3] is delivery:
                    del self._handing_back[zone.slug]

    def _handback_refused(
        self, zone: Zone, sending: tuple[Any, ...], err: Exception, out: list[Effect]
    ) -> None:
        """The head refused the command handing it back to its person (P3).
        It is tried once more at the next apply, if the room is still theirs
        and the head still shows MXZ's command; refused again, a floor notice
        says so (NIT-1), and MXZ sends that head nothing more for it."""
        (mode, low, high), attempt, seen, _ = sending
        if attempt == 1:
            self._handback[zone.slug] = ((mode, low, high), 2, seen)
            return
        unit = self._unit_symbol()
        setting = f"{low:g} {unit}" if low == high else f"{low:g}-{high:g} {unit}"
        message = (
            f"{zone.name}: the floor run is over, but {zone.climate_id} refused "
            f"MXZ's command giving it back to {mode} at {setting}, twice ({err}). "
            "It keeps MXZ's floor setting; MXZ sends it nothing more for this."
        )
        self._handback_note[zone.slug] = (message, seen)
        self._flush_handback(zone, self._host.hass.states.get(zone.climate_id), out)

    def _flush_handback(self, zone: Zone, head: Any, out: list[Effect]) -> None:
        """Send a refused hand-back's notice; one the hourly limit holds back
        goes out at the first compute after the hour (P5, P8), but only while
        the room is still released and the head still shows MXZ's command."""
        if (held := self._handback_note.get(zone.slug)) is None:
            return
        message, seen = held
        if self._host.hands_off_phase(zone) != "released" or head is None or (
            _head_setting(head) != seen
        ):
            del self._handback_note[zone.slug]  # no longer true
        elif self._send(zone, "handback", message, out):
            del self._handback_note[zone.slug]

    def _send(self, zone: Zone, kind: str, message: str, out: list[Effect]) -> bool:
        """Log one floor notice and decide it; return whether it goes out.

        At most one floor notice per room per hour, whatever its kind (trip,
        blind, stalled, rejected), so a flapping sensor or head cannot flood
        anyone (P5). A held-back notice is logged at WARNING, once, saying so.
        It goes to the integration's notification service; with none set, one
        that names no service, or one Home Assistant does not have (a removed
        app, a typo), it becomes a Home Assistant notification instead (P2,
        P11), one per room and kind, so a repeat replaces the last. The send
        itself goes to ``out``.
        """
        host = self._host
        slug = zone.slug
        now = host.clock()
        last = self._notice_at.get(slug)
        if last is not None and now - last < FLOOR_NOTICE_SECONDS:
            held = self._held_back.setdefault(slug, set())
            if message not in held:
                held.add(message)
                _LOGGER.warning(
                    "MXZ floor: %s (not sent: rate-limited to one floor notice "
                    "per room per hour)", message,
                )
            return False
        _LOGGER.warning("MXZ floor: %s", message)
        if host.retired:
            return False
        self._notice_at[slug] = now
        self._held_back.pop(slug, None)
        out.append(lambda: host.hass.async_create_task(self._deliver(slug, kind, message)))
        return True

    async def _deliver(self, slug: str, kind: str, message: str) -> None:
        """Send one floor notice :meth:`_send` decided; a failure is only logged."""
        host = self._host
        domain, _, service = (host.notify_service or "").partition(".")
        try:
            if domain and service:
                try:
                    await host.notify(message)
                    return
                except ServiceNotFound:
                    pass  # not registered: a Home Assistant notification
            await host.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "HVAC Update",
                    "message": message,
                    "notification_id": f"{DOMAIN}_{self._entry_id}_floor_{slug}_{kind}",
                },
                blocking=False,
            )
        except HomeAssistantError as err:
            _LOGGER.error("MXZ floor: the notice could not be sent: %s", err)

    def _unit_symbol(self) -> str:
        return "°C" if self._host.celsius else "°F"

    def yield_to_person(self, zone: Zone, now: float) -> None:
        """A person changed a hands-off head mid-run: MXZ stands down (8b).

        The room is released at once (the head is theirs), and the floor
        re-arms when the room reaches its clear line or after a while.
        """
        run = self._runs.get(zone.slug)
        if run is None:
            return
        self._end(zone.slug)  # their earlier run is not handed back
        self._yield[zone.slug] = (run.direction, now)
        self._host.set_hands_off_released(zone.slug, True)
        self._host.deliveries.disown(zone.climate_id)
        _LOGGER.info(
            "MXZ floor: %s's head was changed by hand during a floor run; "
            "MXZ leaves it to that person until the floor re-arms", zone.name,
        )

    def record(self, slug: str) -> dict[str, Any]:
        """What the enable switch persists for this room's floor (may be empty).

        A record exists only while a run or a yield does, so a room with no
        floor stores exactly what it stored before floors existed.
        """
        record: dict[str, Any] = {}
        if (run := self._runs.get(slug)) is not None:
            record["floor_run"] = {
                "direction": run.direction, "mark": run.mark, "worst": run.worst,
                "driven": run.driven, "stalled": run.stalled,
                "window": [[at, seen] for at, seen in run.recent],
            }
        if (prior := self._prior.get(slug)) is not None:
            record["floor_prior"] = {"mode": prior[0], "low": prior[1], "high": prior[2]}
        if (yielded := self._yield.get(slug)) is not None:
            record["floor_yield"] = {"direction": yielded[0], "at": yielded[1]}
        return record

    def restore(self, slug: str, data: dict[str, Any]) -> None:
        """The enable switch restored a disabled room's floor record.

        Only a well-formed record for a floor still configured is taken; any
        other is dropped, and the room's first compute decides afresh (a
        hands-off room then parks, since a run had un-released it). Downtime
        is not charged to the no-progress clock. A run without a readable
        ``window`` (as 3.7.0 betas before P9 stored it) restarts its re-arm
        window from the next reading.
        """
        zone = next((z for z in self._zones if z.slug == slug), None)
        if zone is None:
            return

        def configured(direction: Any) -> bool:
            return (direction == MODE_HEAT and zone.floor_low is not None) or (
                direction == MODE_COOL and zone.floor_high is not None
            )

        run = data.get("floor_run")
        if isinstance(run, dict) and configured(run.get("direction")):
            numbers: list[Any] = [_finite(run.get(key)) for key in ("mark", "worst", "driven")]
            if None not in numbers and isinstance(run.get("stalled"), bool):
                mark, worst, driven = numbers
                self._runs[slug] = FloorRun(
                    direction=run["direction"], mark=mark, worst=worst,
                    driven=max(driven, 0.0), stalled=run["stalled"],
                    recent=_floor_window(run.get("window")),
                )
                prior = data.get("floor_prior")
                if isinstance(prior, dict) and prior.get("mode") == run["direction"]:
                    low, high = _finite(prior.get("low")), _finite(prior.get("high"))
                    if low is not None and high is not None:
                        self._prior[slug] = (run["direction"], low, high)
        yielded = data.get("floor_yield")
        if isinstance(yielded, dict) and configured(yielded.get("direction")):
            since = _finite(yielded.get("at"))
            if since is not None:
                self._yield[slug] = (yielded["direction"], since)


def _finite(value: Any) -> float | None:
    """A stored number as a finite float, else None (a bool is not a number)."""
    return float(value) if finite_number(value) else None


def _span(seconds: float) -> str:
    """A freshness window as a person reads it: whole hours, else minutes."""
    minutes = seconds / 60.0
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes / 60:g} h"
    return f"{minutes:g} min"


def _head_setting(head: Any) -> tuple[Any, ...]:
    """What a head shows a person: its mode and setpoints."""
    return (head.state, *(
        head.attributes.get(key)
        for key in ("target_temp_low", "target_temp_high", "temperature")
    ))


def _floor_setting(head: Any) -> tuple[float, float] | None:
    """A head's setpoints as (low, high); a single-setpoint head's one
    setpoint is both. None if it shows none."""
    if int(head.attributes.get("supported_features") or 0) & 2:
        low = as_float(head.attributes.get("target_temp_low"))
        high = as_float(head.attributes.get("target_temp_high"))
    else:
        low = high = as_float(head.attributes.get("temperature"))
    return None if low is None or high is None else (low, high)


def _floor_window(value: Any) -> tuple[tuple[float, float], ...]:
    """A stored re-arm window: [[driven, reading], ...], else empty."""
    if not isinstance(value, list):
        return ()
    pairs: list[Any] = [
        (_finite(item[0]), _finite(item[1]))
        for item in value if isinstance(item, list) and len(item) == 2
    ]
    if len(pairs) != len(value) or any(None in pair for pair in pairs):
        return ()
    return tuple(pairs)
