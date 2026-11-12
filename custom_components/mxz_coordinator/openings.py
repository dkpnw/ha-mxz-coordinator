"""Door/window pause (opt-in, per room): its record, its store, its wakeups.

Idea from @mikepell007's MIT blueprint mxz_coordinator_blueprint. The states and
their delays are decided in ``logic.opening_state``/``logic.opening_due``; this
module keeps each room's record, writes it at once on every change, wakes at the
end of each delay, and sends the pause and resume notices. The coordinator asks
it whether a room is paused and calls it at the points listed on ``OpeningPause``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    OPENING_CLOSED,
    OPENING_CLOSING,
    OPENING_NOTIFY_SECONDS,
    OPENING_OPENING,
    OPENING_OVERRIDDEN,
    OPENING_PAUSED,
    OPENING_PAUSED_STATES,
    OPENING_REASON_OVERRIDDEN,
    OPENING_REASON_PAUSED,
    UNAVAILABLE_STATES,
)
from .host import ControllerHost
from .logic import (
    CLOSED_READ,
    finite_number,
    opening_due,
    opening_reading,
    opening_state,
)

if TYPE_CHECKING:
    from homeassistant.core import EventStateChangedData

    from .coordinator import Zone
    from .dry import DryMode

_LOGGER = logging.getLogger(__package__)

# ---------------------------------------------------------------------------
# One small storage file per config entry, beside the mode-change record and
# apart from it (the hold's record is never touched by a pause). Written at
# once on every change of a room's door/window state, keyed by the room's
# head (a reorder moves a room's slug, never its head). Each room: its state,
# a person's override ("switch" or "manual", or None), when its pause began
# (wall clock), the head as it was then (the snapshot a resume restores), and
# whether its pause notice went out (its resume notice follows only then).
OPENINGS_STORE_VERSION = 1
OPENINGS_ROOMS_KEY = "rooms"
_OPENING_STATES = (
    OPENING_CLOSED, OPENING_OPENING, OPENING_PAUSED, OPENING_CLOSING, OPENING_OVERRIDDEN,
)
_OPENING_OVERRIDES = ("switch", "manual")


def _openings_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """This entry's door/window store, one instance per Home Assistant run."""
    stores: dict[str, Store[dict[str, Any]]] = hass.data.setdefault(
        f"{DOMAIN}_openings_stores", {}
    )
    if entry_id not in stores:
        stores[entry_id] = Store(
            hass, OPENINGS_STORE_VERSION, f"{DOMAIN}.{entry_id}.openings"
        )
    return stores[entry_id]


async def async_load_openings(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Each room's stored door/window record, by head; {} when none is readable."""
    stored = await _openings_store(hass, entry_id).async_load()
    rooms = stored.get(OPENINGS_ROOMS_KEY) if isinstance(stored, dict) else None
    if not isinstance(rooms, dict):
        return {}
    return {key: value for key, value in rooms.items() if isinstance(value, dict)}


async def async_remove_openings(hass: HomeAssistant, entry_id: str) -> None:
    """Delete a removed entry's door/window record."""
    await _openings_store(hass, entry_id).async_remove()
    hass.data.get(f"{DOMAIN}_openings_stores", {}).pop(entry_id, None)


@dataclass
class OpeningRecord:
    """One room's door/window record: what the store keeps, and when its delay ends.

    ``override`` is a person's: "switch" or "manual", or None. ``paused_at``
    is the wall-clock start of the pause and ``snapshot`` the head as the
    pause found it. ``notified`` says whether the pause notice went out.
    ``due`` is in memory only: it is re-read from the opening entity at every
    evaluation.
    """

    state: str = OPENING_CLOSED
    override: str | None = None
    paused_at: float | None = None
    snapshot: dict[str, Any] | None = None
    notified: bool = False
    due: float | None = None

    @classmethod
    def restored(cls, stored: Any) -> OpeningRecord:
        """The record as a start reads it; anything unreadable is closed."""
        data: Any = stored if isinstance(stored, dict) else {}
        state = data.get("state")
        override = data.get("override")
        paused_at = data.get("paused_at")
        snapshot = data.get("snapshot")
        return cls(
            state=state if state in _OPENING_STATES else OPENING_CLOSED,
            override=override if override in _OPENING_OVERRIDES else None,
            paused_at=float(paused_at) if finite_number(paused_at) else None,
            snapshot=snapshot if isinstance(snapshot, dict) else None,
            notified=data.get("notified") is True,
        )

    def stored(self) -> dict[str, Any]:
        """What the store keeps of it."""
        return {
            "state": self.state,
            "override": self.override,
            "paused_at": self.paused_at,
            "snapshot": self.snapshot,
            "notified": self.notified,
        }


def _delay_text(seconds: float) -> str:
    """A door/window delay as a notice says it: whole hours, else minutes, else seconds."""
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds / 3600:g} h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds / 60:g} min"
    return f"{seconds:g} s"


def _iso(stamp: float | None) -> str | None:
    """A wall-clock time as ISO 8601 to the second, or None."""
    if stamp is None:
        return None
    return dt_util.utc_from_timestamp(stamp).isoformat(timespec="seconds")


class OpeningHost(ControllerHost, Protocol):
    """The coordinator members the pause reads or calls, beyond every controller's.

    Read: the kill switch, and the Fan auto and Dry state a pause snapshots.
    Called: a resume's Dry and vanes, and the recompute that follows a
    person's change.
    """

    coordinator_enable: bool
    dry: DryMode

    def fan_auto_is_on(self, climate_id: str) -> bool: ...
    async def async_apply_vane(self, climate_id: str, vane_id: str, option: str) -> None: ...
    async def async_user_changed(self, *, at_once: bool = False) -> None: ...


class OpeningPause:
    """The door/window pause of each room that has it on: records, store, wakeups.

    The coordinator calls it from the compute (``step``), the apply (``paused``,
    then ``async_restore_vanes``), a head's report (``take_over``), a vane pick
    (``keep_vane``), the Window Override switch (``async_set_override``), and
    its own setup and teardown (``start``, ``sync_timers``, ``cancel_timers``).
    """

    def __init__(
        self,
        host: OpeningHost,
        entry_id: str,
        zones: list[Zone],
        stored: dict[str, Any] | None,
        started: float,
    ) -> None:
        """Each room with the pause on, its record as last stored (see
        OpeningRecord.restored), so a pause outlives a reload or a restart.
        Evaluated from ``start`` on; the seed compute only shows it. One
        wakeup per room for the end of its open or close delay; the monotonic
        time of each room's last pause notice (the rate limit, in memory
        only); and the vanes a resume still owes a room's head. ``started``
        is the coordinator's start, on the wall clock: a reading held since
        before it counts from it.
        """
        self._host = host
        self._hass = host.hass
        self._zones = zones
        self._started = started
        self._store = _openings_store(host.hass, entry_id)
        self._records: dict[str, OpeningRecord] = {
            zone.slug: OpeningRecord.restored((stored or {}).get(zone.climate_id))
            for zone in zones
            if zone.opening_active
        }
        # A stored room that no longer pauses (turned off, or removed) is
        # dropped from the record at setup, so turning the pause back on later
        # never brings back an old pause.
        self._stale = bool(
            set(stored or {}) - {z.climate_id for z in zones if z.opening_active}
        )
        self._live = False
        self._timers: dict[str, Any] = {}
        self._notice_at: dict[str, float] = {}
        self._vanes_due: dict[str, dict[str, str]] = {}

    @property
    def any_room(self) -> bool:
        """Whether any room has the pause on."""
        return bool(self._records)

    def start(self) -> None:
        """Evaluate from now on; drop a stale room's record from the store."""
        self._live = True
        if self._stale:
            self._save()

    def record(self, slug: str) -> dict[str, Any] | None:
        """This room's record as it stands, for diagnostics; None without the pause."""
        record = self._records.get(slug)
        return None if record is None else asdict(record)

    def paused(self, zone: Zone) -> bool:
        """Whether this room is paused for its door/window now (and so off, no vote).

        Only an enabled room: a disabled room's head is off anyway, and its
        freeze floor / overheat ceiling (a safety run) is never paused.
        """
        record = self._records.get(zone.slug)
        return (
            record is not None and zone.enable
            and record.state in OPENING_PAUSED_STATES
        )

    def step(self, zone: Zone, now: float) -> bool:
        """Evaluate this room's door/window state; return whether it is paused.

        Part of the compute (it commands nothing): it advances the room's
        state (logic.opening_state) and, on a change, snapshots the head for
        a pause or restores it on a resume, sends the notice, and saves the
        record at once. A reading held since before this coordinator started
        counts from the start, so a reload or restart can only lengthen a
        delay, never shorten it. The seed compute in the coordinator's
        __init__ only shows the stored state.
        """
        record = self._records.get(zone.slug)
        if record is None:
            return False
        if self._live and not self._host.retired:
            sensor = self._hass.states.get(cast("str", zone.opening_entity))
            raw = opening_reading(sensor.state if sensor is not None else None)
            since = max(
                sensor.last_changed.timestamp() if sensor is not None else now,
                self._started,
            )
            changed = False
            if raw == CLOSED_READ and record.override is not None:
                # A closed reading ends an override: the next opening pauses.
                record.override = None
                changed = True
            old = record.state
            new = opening_state(
                raw=raw, since=since, now=now,
                open_delay=zone.opening_open_delay,
                close_delay=zone.opening_close_delay,
                prior=old, override=record.override is not None,
            )
            record.due = opening_due(
                state=new, since=since,
                open_delay=zone.opening_open_delay,
                close_delay=zone.opening_close_delay,
            )
            if new != old:
                record.state = new
                self._transition(zone, record, old, new, now)
                changed = True
            if changed:
                self._save()
        return self.paused(zone)

    def _transition(
        self, zone: Zone, record: OpeningRecord, old: str, new: str, now: float
    ) -> None:
        """One change of a room's door/window state: snapshot, restore, notice."""
        was_paused = old in OPENING_PAUSED_STATES
        is_paused = new in OPENING_PAUSED_STATES
        _LOGGER.info("MXZ: %s door/window %s -> %s", zone.name, old, new)
        if is_paused and not was_paused:
            record.paused_at = now
            record.snapshot = self._snapshot(zone)
            sensor = self._hass.states.get(cast("str", zone.opening_entity))
            what = sensor.name if sensor is not None else zone.opening_entity
            self._notice(
                zone, record, "pause",
                f"{zone.name} paused: {what} open for "
                f"{_delay_text(zone.opening_open_delay)}. Heat/cool off in this room.",
            )
            return
        if was_paused == is_paused:
            # paused <-> closing (still paused: the close delay is counting),
            # or opening <-> closed, or an override before any pause.
            return
        snapshot = record.snapshot or {}
        manual = record.override == "manual"
        record.paused_at = None
        record.snapshot = None
        if new == OPENING_OVERRIDDEN:
            self._notice(
                zone, record, "override",
                f"{zone.name}: {OPENING_REASON_OVERRIDDEN}.",
            )
        else:
            self._notice(
                zone, record, "resume",
                f"{zone.name} resumed: closed for {_delay_text(zone.opening_close_delay)}.",
            )
        if manual:
            return  # a person's own change on the head stands
        # Resume: the room is back in the vote, so the plan decides on/off
        # and the direction (the shared mode now, not the old one). The fan
        # needs nothing: MXZ wrote none while paused, so a hold (or a pick
        # made while paused) is still the head's. Vanes and a Dry are sent.
        vanes = snapshot.get("vanes")
        if isinstance(vanes, dict) and vanes:
            self._vanes_due[zone.slug] = {
                str(key): str(value) for key, value in vanes.items()
            }
        if (
            snapshot.get("dry") is True and zone.enable
            and self._host.dry.pick_refusal(zone) is None and self._host.dry.start_pick(zone)
        ):
            self._hass.async_create_task(self._send_dry(zone))

    def _snapshot(self, zone: Zone) -> dict[str, Any]:
        """The head as a pause found it: mode, fan, whether the fan was held,
        its vanes, and whether it was in a Dry the room keeps."""
        head = self._hass.states.get(zone.climate_id)
        mode = head.state if head is not None else None
        vanes = {}
        for vane_id in (zone.vane_vertical_id, zone.vane_horizontal_id):
            vane = self._hass.states.get(vane_id) if vane_id else None
            if vane is not None and vane.state not in UNAVAILABLE_STATES:
                vanes[vane_id] = vane.state
        return {
            "hvac_mode": None if mode in (None, *UNAVAILABLE_STATES) else mode,
            "fan_mode": head.attributes.get("fan_mode") if head is not None else None,
            "fan_held": not self._host.fan_auto_is_on(zone.climate_id),
            "vanes": vanes,
            "dry": self._host.dry.in_dry(zone),
        }

    def _notice(
        self, zone: Zone, record: OpeningRecord, kind: str, message: str
    ) -> None:
        """Send one door/window notice, if the room asks for them, rate limited.

        A pause notice goes out at most once per OPENING_NOTIFY_SECONDS per
        room; its resume (or override) notice goes out only when the pause
        notice did, so a door that flaps sends at most one pair in that time.
        A pause of a room that is off, or of a coordinator that is off, sends
        nothing: nothing in the room changed.
        """
        if not zone.opening_notify:
            return
        if kind == "pause":
            clock = self._host.clock()
            last = self._notice_at.get(zone.slug)
            if (
                not zone.enable or not self._host.coordinator_enable
                or (last is not None and clock - last < OPENING_NOTIFY_SECONDS)
            ):
                record.notified = False
                return
            self._notice_at[zone.slug] = clock
            record.notified = True
        else:
            if not record.notified:
                return
            record.notified = False
        self._hass.async_create_task(
            self._send_notice(message, zone.opening_notify_service)
        )

    async def _send_notice(self, message: str, target: str | None) -> None:
        """A notice that fails is logged; it never holds up control."""
        try:
            await self._host.notify(message, target)
        except Exception as err:  # noqa: BLE001 - any notify failure is only logged
            _LOGGER.warning("MXZ: door/window notice not sent (%s): %s", message, err)

    async def _send_dry(self, zone: Zone) -> None:
        """Put back the Dry a pause found, once; a refusal is logged."""
        try:
            await self._host.dry.async_send(zone)
        except HomeAssistantError as err:
            _LOGGER.warning("MXZ: %s Dry not put back after its pause: %s", zone.name, err)

    def take_over(self, zone: Zone) -> None:
        """A person changed this paused room's head: their change stands until it closes."""
        self._records[zone.slug].override = "manual"
        self._save()

    def keep_vane(self, zone: Zone, vane_id: str, option: str) -> None:
        """A vane picked while paused: no kick now; the resume sends it."""
        record = self._records[zone.slug]
        snapshot = record.snapshot or {}
        vanes: Any = snapshot.get("vanes") if isinstance(snapshot.get("vanes"), dict) else {}
        record.snapshot = {**snapshot, "vanes": {**vanes, vane_id: option}}
        self._save()

    async def async_restore_vanes(self) -> None:
        """After a resume's apply, send each vane the paused room kept for it.

        The snapshot's position, or a pick made while paused. Only where the
        vane reads otherwise now; a head the plan left off gets the usual
        vane kick. A room paused again keeps them owed.
        """
        for zone in self._zones:
            vanes = self._vanes_due.get(zone.slug)
            if not vanes or self.paused(zone) or self._host.retired:
                continue
            self._vanes_due.pop(zone.slug)
            for vane_id, option in vanes.items():
                vane = self._hass.states.get(vane_id)
                if (
                    vane is None or vane.state in UNAVAILABLE_STATES
                    or vane.state == option
                    or option not in (vane.attributes.get("options") or ())
                ):
                    continue
                try:
                    await self._host.async_apply_vane(zone.climate_id, vane_id, option)
                except HomeAssistantError as err:
                    _LOGGER.warning(
                        "MXZ: %s vane %s not put back after its pause: %s",
                        zone.name, vane_id, err,
                    )

    def _save(self) -> None:
        """Write every room's door/window record at once (never on a retired one)."""
        if self._host.retired:
            return
        data = {
            OPENINGS_ROOMS_KEY: {
                zone.climate_id: self._records[zone.slug].stored()
                for zone in self._zones
                if zone.slug in self._records
            }
        }
        self._store.async_delay_save(lambda: data, 0)

    @callback
    def on_change(self, event: Event[EventStateChangedData]) -> None:
        """A door/window entity changed -> recompute, and wake at its delay's end.

        The recompute is the ordinary request, which can wait out the request
        cooldown; the wakeup is armed from the reading itself, so a short
        delay still ends on time. The compute that follows re-arms it from
        the room's state (sync_timers).
        """
        new_state = event.data.get("new_state")
        reading = opening_reading(new_state.state if new_state is not None else None)
        for zone in self._zones:
            if zone.slug not in self._records or zone.opening_entity != event.data["entity_id"]:
                continue
            if reading is None:
                continue
            delay = (
                zone.opening_open_delay if reading != CLOSED_READ else zone.opening_close_delay
            )
            self._arm_timer(zone.slug, delay)
        self._hass.async_create_task(self._host.async_request_refresh())

    def sync_timers(self) -> None:
        """One wakeup per room at the end of its open or close delay."""
        self.cancel_timers()
        if self._host.retired:
            return
        now = dt_util.utcnow().timestamp()
        for slug, record in self._records.items():
            if record.due is not None:
                self._arm_timer(slug, record.due - now)

    def _arm_timer(self, slug: str, delay: float) -> None:
        """Replace this room's wakeup with one ``delay`` seconds from now."""
        if (cancel := self._timers.pop(slug, None)) is not None:
            cancel()
        if self._host.retired:
            return

        @callback
        def _fire(_now: Any) -> None:
            # At once, not after the request cooldown: the delay is the
            # promise, so a pause or resume lands when it ends.
            self._timers.pop(slug, None)
            if not self._host.retired:
                self._hass.async_create_task(self._host.async_refresh_now())

        self._timers[slug] = async_call_later(self._hass, max(0.0, delay), _fire)

    def cancel_timers(self) -> None:
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    def state_of(self, slug: str) -> str | None:
        """This room's door/window state, or None for a room without the pause."""
        record = self._records.get(slug)
        return None if record is None else record.state

    def attributes(self, zone: Zone) -> dict[str, Any]:
        """A paused room says so, and when it resumes; nothing for a room without it.

        ``opening_state`` is the door/window's own state. ``paused_reason``
        names why the room is off, only while the pause holds it (a disabled
        room is off anyway, and its floor may be running it), or why it runs
        with its window open; ``resumes_at`` is set while the closed reading
        counts down the close delay (an open window has no resume time yet).
        """
        record = self._records.get(zone.slug)
        if record is None:
            return {}
        state = record.state
        reason = (
            OPENING_REASON_PAUSED if self.paused(zone)
            else OPENING_REASON_OVERRIDDEN if state == OPENING_OVERRIDDEN
            else None
        )
        return {
            "opening_entity": zone.opening_entity,
            "opening_state": state,
            "paused_reason": reason,
            "window_override": record.override is not None,
            "paused_since": _iso(record.paused_at),
            "resumes_at": _iso(record.due) if state == OPENING_CLOSING else None,
        }

    def override_on(self, slug: str) -> bool:
        """Whether a person is running this room with its door/window open."""
        record = self._records.get(slug)
        return record is not None and record.override is not None

    async def async_set_override(self, zone: Zone, on: bool) -> None:
        """The room's Window Override switch.

        On: ignore the open door/window, so a paused room resumes at once by
        the ordinary resume (the mode-change hold applies as always). A
        closed reading ends it, so it does nothing while the opening reads
        closed. Off: the opening counts again (an open one pauses the room).
        """
        record = self._records.get(zone.slug)
        if record is None:
            return
        sensor = self._hass.states.get(cast("str", zone.opening_entity))
        closed = opening_reading(sensor.state if sensor is not None else None) == CLOSED_READ
        override = "switch" if on and not closed else None
        if override == record.override or (on and record.override is not None):
            self._host.redraw_rooms()
            return
        record.override = override
        self._save()
        await self._host.async_user_changed(at_once=True)
