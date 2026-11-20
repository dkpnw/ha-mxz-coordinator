"""Restart resume: what a start takes back from the room state store and the mode record.

Standard library only, so the simulator and the tests call it without Home Assistant.
``MXZCoordinator`` reads the store (:class:`SavedState`) and the mode record, gathers each
room's head and target bounds, and applies what these return through the same calls an
entity's restore data uses:

- :func:`mode_record` reads a stored mode record with the wall clock, and
  :func:`resumed_mode` weighs it against the shared-mode select's restore data;
- :func:`resume` reads every room's values, the switches, each head's fan hold and MXZ's
  memory of its head writes out of the store, and says which values the store resumes;
- :func:`resumed_enable` is what a resumed enable does to the engage latch and the startup
  seeds, the hands-off release and the floor record.

Time comes in as numbers. Which source wins (the store, or an entity's newer restore data),
and when each is read, stays with the coordinator and the entities.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .const import (
    HEAD_WRITES_REMEMBERED,
    KEY_COOL_LOCKOUT,
    KEY_COORDINATOR_ENABLE,
    KEY_ECO_IDLE,
    KEY_HEAT_LOCKOUT,
    MODE_COOL,
    MODE_HEAT,
)
from .fan_latch import FanRecord
from .head_writes import HEAD_WRITE_FIELDS
from .logic import HeadWriteMemory, finite_number
from .units import offset_in_unit, snap_to_step, target_in_unit

# The room state store's sections (room_state.py says what each holds).
SECTION_ROOMS = "rooms"
SECTION_SWITCHES = "switches"
SECTION_FAN = "fan"
SECTION_FLOOR = "floor"
SECTION_MODE = "mode"
SECTION_OPENINGS = "openings"
SECTION_HEAD_WRITES = "head_writes"

# The switches the room state store keeps; each is a coordinator attribute of that name.
SWITCH_KEYS = (KEY_COORDINATOR_ENABLE, KEY_ECO_IDLE, KEY_HEAT_LOCKOUT, KEY_COOL_LOCKOUT)

# What MXZ remembers of its head writes (logic.HeadWriteMemory), by service field.

# The mode record: the room state store's ``mode`` section, and for one release also its
# own file (coordinator.py, where it is written and loaded). It holds the time of the last
# real change (MODE_CHANGE_KEY, absent while none was ever made) and the shared mode as of
# MODE_AT_KEY, with whether hands-off votes alone set it (MODE_BY_VOTES_KEY). Earlier
# versions wrote the time only; their records read as before, with no mode on record.
# Times are wall-clock UTC epoch seconds, the only clock that means anything to the next
# process.
MODE_CHANGE_KEY = "last_mode_change"
MODE_KEY = "mode"
MODE_AT_KEY = "mode_at"
MODE_BY_VOTES_KEY = "mode_by_votes"


@dataclass(frozen=True)
class SavedState:
    """What the store held at a start.

    ``readable`` is False for a store that can't be loaded, such as one a later
    release wrote: it is neither read nor written over. Without ``saved_at`` the store is older than any restore
    record.
    """

    saved_at: float | None = None
    unit: str | None = None
    sections: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    readable: bool = True

    def section(self, name: str) -> dict[str, Any] | None:
        """One section as stored, or None."""
        return self.sections.get(name)

    def item(self, name: str, key: str) -> dict[str, Any] | None:
        """One head's (or one switch section's) record, or None if it isn't a record."""
        value = (self.sections.get(name) or {}).get(key)
        return value if isinstance(value, dict) else None

    def outranks(self, recorded_at: datetime | None) -> bool:
        """Whether this store is newer than a restore record taken at ``recorded_at``.

        No record (None) is always older. A record as new as the store or newer
        was written after it, by this release or by an earlier one after a
        rollback, and wins.
        """
        if recorded_at is None:
            return True
        return self.saved_at is not None and self.saved_at > recorded_at.timestamp()


@dataclass(frozen=True)
class ModeRecord:
    """The entry's mode-change record, as a start reads it.

    ``changed_at`` is the last real cool/heat change (None: none on record).
    ``mode`` is the shared mode when the record was written at ``mode_at``,
    and ``by_votes`` whether hands-off votes alone had set it; ``mode`` is
    None when the record has no readable mode (an earlier version wrote it).
    """

    changed_at: float | None = None
    mode: str | None = None
    mode_at: float = 0.0
    by_votes: bool = False

    def stored(self) -> dict[str, Any] | None:
        """The record as it is written; None for no record."""
        data: dict[str, Any] = {}
        if self.mode is not None:
            data = {MODE_KEY: self.mode, MODE_AT_KEY: self.mode_at, MODE_BY_VOTES_KEY: self.by_votes}
        if self.changed_at is not None:
            data[MODE_CHANGE_KEY] = self.changed_at
        return data or None


def mode_record(stored: Any, now: float) -> tuple[ModeRecord, bool]:
    """The mode record a start reads from ``stored``, and whether its change time was unreadable.

    ``stored`` is the record as loaded (None: there is none) and ``now`` the wall clock.

    No change time means no change was ever recorded (a fresh install, the
    first start on a version that did not keep it, or a record that so far
    holds only a mode votes chose): there is nothing to wait out. A record
    that is there but whose time is unreadable is not a fresh install; it
    counts as a change made now, so the hold is the full window, as it was
    before this was kept. A mode that is not readable is no mode on record.
    """
    if stored is None:
        return ModeRecord(), False
    data: Any = stored if isinstance(stored, dict) else {}
    mode, mode_at, by_votes = (
        data.get(MODE_KEY), data.get(MODE_AT_KEY), data.get(MODE_BY_VOTES_KEY)
    )
    has_mode = (
        mode in (MODE_COOL, MODE_HEAT) and finite_number(mode_at)
        and isinstance(by_votes, bool)
    )
    shared: dict[str, Any] = (
        {"mode": mode, "mode_at": float(mode_at), "by_votes": by_votes} if has_mode else {}
    )
    value = data.get(MODE_CHANGE_KEY)
    if finite_number(value):
        return ModeRecord(changed_at=float(value), **shared), False
    if has_mode and MODE_CHANGE_KEY not in data:
        return ModeRecord(changed_at=None, **shared), False
    return ModeRecord(changed_at=now, **shared), True


def resumed_mode(
    record: ModeRecord, restored: str | None, restored_at: float | None
) -> tuple[str, bool]:
    """The shared mode a start resumes in, and whether votes alone set it.

    ``restored`` is the shared-mode select's restored option (None: no restore
    data) and ``restored_at`` when that state last changed (wall clock). The
    record is written at every change, at once, and restore data only every
    15 minutes and at a stop, so where the two disagree the record wins unless
    the restore data is newer: then the mode changed while a version that did
    not keep it in the record ran, and it is not known whether votes set it.
    Where they agree the record also says whether votes set the mode (the
    select's state is written anew at every start, so its time says nothing
    then).
    """
    if record.mode is None:
        mode, by_votes = restored or MODE_COOL, False
    elif (
        restored in (None, record.mode)
        or restored_at is None
        or record.mode_at >= restored_at
    ):
        mode, by_votes = record.mode, record.by_votes
    else:
        mode, by_votes = restored, False
    return mode, by_votes


@dataclass(frozen=True, slots=True)
class RoomSlot:
    """A room as resume reads it: its head, its slug, and its head's target bounds."""

    climate_id: str
    slug: str
    bounds: tuple[float, float]


@dataclass(frozen=True, slots=True)
class RoomResume:
    """What the store resumes of one room; None (and ``has_drift`` False) where it holds nothing.

    ``target`` is in the system unit, on the target step, held to the head's bounds;
    ``target_range`` is the Heat to / Cool to pair, only when both edges are stored.
    ``drift`` None with ``has_drift`` is a stored "follow the global". ``enable_record``
    is the floor record and the hands-off release that go with a stored enable.
    """

    target: float | None = None
    target_range: tuple[float, float] | None = None
    has_drift: bool = False
    drift: float | None = None
    enable: bool | None = None
    enable_record: dict[str, Any] = field(default_factory=dict)
    fan: FanRecord | None = None
    head_writes: dict[str, HeadWriteMemory] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Resume:
    """What the store resumes: the switches, each room in order, and which values it holds.

    ``resumed`` names each value as ``MXZCoordinator.store_resumes`` does: ("switch", key),
    ("target" | "range" | "drift" | "enable", slug), ("fan", climate_id).
    """

    switches: dict[str, bool]
    rooms: tuple[RoomResume, ...]
    resumed: frozenset[tuple[str, str]]


def saved_temperature(value: Any, saved_in: str | None, unit: str, step: float) -> float | None:
    """A stored temperature in ``unit``, on ``step``; None if it isn't one."""
    if not finite_number(value):
        return None
    return target_in_unit(float(value), saved_in, unit, step)


def saved_drift(
    value: float | None, saved_in: str | None, celsius: bool, bounds: tuple[float, float]
) -> float | None:
    """A stored drift override in the system unit, held to ``bounds``; None follows the global.

    A band is a difference, so it only scales, onto the drift number's step.
    """
    if value is None:
        return None
    lo, hi = bounds
    scaled = offset_in_unit(float(value), saved_in, celsius)
    if scaled != value:
        scaled = snap_to_step(scaled, lo / 2)
    return min(max(scaled, lo), hi)


def resume(
    saved: SavedState,
    rooms: Sequence[RoomSlot],
    *,
    unit: str,
    step: float,
    celsius: bool,
    engage_bounds: tuple[float, float],
) -> Resume:
    """Every value the room state store holds, as a start resumes it.

    ``unit`` is the system's temperature unit (``celsius`` whether it is °C), ``step`` the
    target step and ``engage_bounds`` the drift's bounds. Temperatures saved in the other
    unit are converted, as a restored target number's are, and a target is held to its
    head's bounds. MXZ's memory of its head writes resumes only in the unit it was kept in:
    a remembered 70 °F is no report in °C. A fan record counts only if it is usable.
    """
    resumed: set[tuple[str, str]] = set()
    stored_switches = saved.section(SECTION_SWITCHES) or {}
    switches: dict[str, bool] = {}
    for key in SWITCH_KEYS:
        if isinstance(on := stored_switches.get(key), bool):
            switches[key] = on
            resumed.add(("switch", key))
    resumed_rooms: list[RoomResume] = []
    for slot in rooms:
        cid, slug = slot.climate_id, slot.slug
        room = saved.item(SECTION_ROOMS, cid) or {}
        target = None
        if (value := saved_temperature(room.get("target"), saved.unit, unit, step)) is not None:
            lo, hi = slot.bounds
            target = min(max(value, lo), hi)
            resumed.add(("target", slug))
        target_range = None
        low = saved_temperature(room.get("target_low"), saved.unit, unit, step)
        high = saved_temperature(room.get("target_high"), saved.unit, unit, step)
        if low is not None and high is not None:
            target_range = (low, high)
            resumed.add(("range", slug))
        has_drift, drift = False, None
        stored_drift = room.get("drift")
        if (stored_drift is None and "drift" in room) or finite_number(stored_drift):
            has_drift, drift = True, saved_drift(stored_drift, saved.unit, celsius, engage_bounds)
            resumed.add(("drift", slug))
        enable, enable_record = None, {}
        if isinstance(on := room.get("enable"), bool):
            floor = saved.item(SECTION_FLOOR, cid) or {}
            released = room.get("hands_off_released") is True
            enable, enable_record = on, {**floor, "hands_off_released": released}
            resumed.add(("enable", slug))
        fan = None
        if (stored_fan := saved.item(SECTION_FAN, cid)) is not None and (
            fan := FanRecord.from_dict(stored_fan)
        ) is not None:
            resumed.add(("fan", cid))
        writes = (saved.item(SECTION_HEAD_WRITES, cid) or {}) if saved.unit == unit else {}
        memories: dict[str, HeadWriteMemory] = {}
        for name in HEAD_WRITE_FIELDS:
            memory = HeadWriteMemory.restored(HEAD_WRITES_REMEMBERED, writes.get(name))
            if memory is not None:
                memories[name] = memory
        resumed_rooms.append(
            RoomResume(
                target=target,
                target_range=target_range,
                has_drift=has_drift,
                drift=drift,
                enable=enable,
                enable_record=enable_record,
                fan=fan,
                head_writes=memories,
            )
        )
    return Resume(switches=switches, rooms=tuple(resumed_rooms), resumed=frozenset(resumed))


@dataclass(frozen=True, slots=True)
class EnableResume:
    """What a resumed enable leaves: the engage latch and startup seeds, the hands-off
    release, and the floor record ``FloorGuard.restore`` takes."""

    latch: dict[str, str]
    startup: dict[str, str | None]
    released: bool
    floor: dict[str, Any]


def resumed_enable(
    slug: str,
    on: bool,
    hands_off: bool,
    saved: dict[str, Any],
    latch: Mapping[str, str],
    startup: Mapping[str, str | None],
) -> EnableResume:
    """A start resumed a room's enable, and a disabled room's release and floor.

    ``saved`` holds ``hands_off_released`` and the floor record (``FloorGuard.record``);
    ``hands_off`` is whether the room is opted in now. An enabled room resumes a run its
    head shows: it waits in ``startup`` for its head's first report (``planner.decide``
    seeds its latch from it). A disabled one has no run. A disabled hands-off room is
    released only if its off had landed, and a disabled room's floor run or yield comes
    back only for a floor it still sets (``FloorGuard.restore`` checks that). The latch and
    the seeds come in as they are and go out advanced, as copies.
    """
    latch, startup = dict(latch), dict(startup)
    if on:
        startup[slug] = None
    else:
        startup.pop(slug, None)
        latch[slug] = ""
    return EnableResume(
        latch=latch,
        startup=startup,
        released=not on and hands_off and saved.get("hands_off_released") is True,
        floor={} if on else saved,
    )
