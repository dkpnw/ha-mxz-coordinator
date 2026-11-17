"""Each room's state, kept by the coordinator in one store per entry.

What a reload, a restart or a crash must not change: each room's target, Heat to and Cool to,
drift override, enable and hands-off release (``rooms``), the Coordinator, eco and lockout
switches (``switches``), each head's fan hold and echo memory (``fan``), a disabled room's
floor run (``floor``), the shared mode and its last change (``mode``) and each door/window
pause (``openings``). Each section is its owner's record, by head where it is per room: a
reorder moves a room's slot, never its head. The file also holds when it was taken
(``saved_at``, wall clock) and the unit its temperatures are in (``unit``).

A change is written at once, so a crash loses only a write still on its way to disk; HA's
``Store`` folds changes made together into one write. The mode and door/window records are
written even when the rest is unchanged (``save_now``): a head is only sent what the record
already holds. Another section, such as each head's write memory, is one more ``track`` call
and one more read at the start.

For one release the old channels stay current, so an earlier release finds its own data after
a rollback: the mode and door/window records are also written to their own files at the same
moments as before, and the entities keep their restore data. A start reads those as well
(docs/MIGRATION.md).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .logic import finite_number

_LOGGER = logging.getLogger(__name__)

ROOM_STATE_VERSION = 1
SAVED_AT_KEY = "saved_at"
UNIT_KEY = "unit"
SECTION_ROOMS = "rooms"
SECTION_SWITCHES = "switches"
SECTION_FAN = "fan"
SECTION_FLOOR = "floor"
SECTION_MODE = "mode"
SECTION_OPENINGS = "openings"
_UNITS = (UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT)


def _room_state_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """This entry's store, one instance per Home Assistant run.

    Shared by every coordinator incarnation of the entry, so a reload reads a
    write that is still waiting (Store serves pending data).
    """
    stores: dict[str, Store[dict[str, Any]]] = hass.data.setdefault(
        f"{DOMAIN}_room_state_stores", {}
    )
    if entry_id not in stores:
        stores[entry_id] = Store(hass, ROOM_STATE_VERSION, f"{DOMAIN}.{entry_id}.room_state")
    return stores[entry_id]


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


async def async_load_room_state(hass: HomeAssistant, entry_id: str) -> SavedState:
    """The entry's room state; empty when there is none or none of it is readable."""
    try:
        stored = await _room_state_store(hass, entry_id).async_load()
    except (HomeAssistantError, NotImplementedError) as err:
        _LOGGER.warning(
            "MXZ: the room state store can't be read (%s), perhaps because a later release "
            "wrote it; this one neither reads nor writes it, and rooms resume from Home "
            "Assistant's restore data",
            err,
        )
        return SavedState(readable=False)
    if not isinstance(stored, dict):
        return SavedState()
    saved_at: Any = stored.get(SAVED_AT_KEY)
    unit = stored.get(UNIT_KEY)
    return SavedState(
        saved_at=float(saved_at) if finite_number(saved_at) else None,
        unit=unit if unit in _UNITS else None,
        sections={
            key: value for key, value in stored.items()
            if key not in (SAVED_AT_KEY, UNIT_KEY) and isinstance(value, dict)
        },
    )


async def async_remove_room_state(hass: HomeAssistant, entry_id: str) -> None:
    """Delete a removed entry's room state; a re-added entry starts with none."""
    await _room_state_store(hass, entry_id).async_remove()
    hass.data.get(f"{DOMAIN}_room_state_stores", {}).pop(entry_id, None)


class RoomStateStore:
    """Writes every section's record when one changes.

    Nothing is written before ``go_live`` (the start has settled every value)
    or after ``retire``.
    """

    def __init__(
        self, hass: HomeAssistant, entry_id: str, unit: str, *, writable: bool = True
    ) -> None:
        self._store = _room_state_store(hass, entry_id)
        self._unit = unit
        self._writable = writable
        self._sections: dict[str, Callable[[], dict[str, Any] | None]] = {}
        self._legacy: dict[str, Store[dict[str, Any]]] = {}
        self._live = False
        self._retired = False
        self._now: set[str] = set()
        self._last: dict[str, Any] | None = None

    def track(
        self,
        name: str,
        record: Callable[[], dict[str, Any] | None],
        legacy: Store[dict[str, Any]] | None = None,
    ) -> None:
        """Keep ``record()`` as section ``name``; None leaves the section out.

        ``legacy`` is the section's own file from before this store, kept current
        by ``save_now`` for one release.
        """
        self._sections[name] = record
        if legacy is not None:
            self._legacy[name] = legacy

    def go_live(self) -> None:
        """The start is done: write what it settled, and every change from now on.

        A section the start already changed (the shared mode, when restore data
        was newer than its record) is written at once.
        """
        if self._retired:
            return
        self._live = True
        if not self._now:
            self.changed()
        for name in sorted(self._now):
            self.save_now(name)
        self._now.clear()

    def retire(self) -> None:
        """This coordinator is unloaded or replaced: it writes nothing more.

        A write already waiting still lands, with what it was given.
        """
        self._live = False
        self._retired = True

    def changed(self) -> None:
        """Something may have changed: write it if it did."""
        self._save(force=False)

    def save_now(self, name: str) -> None:
        """Section ``name`` changed: write it with its own file, whatever else changed.

        Before ``go_live`` it is written when the start is done.
        """
        if not self._live:
            if not self._retired:
                self._now.add(name)
            return
        if (legacy := self._legacy.get(name)) is not None and (
            data := self._sections[name]()
        ) is not None:
            legacy.async_delay_save(lambda: data, 0)
        self._save(force=True)

    def record(self) -> dict[str, Any]:
        """Every section's record as it stands."""
        return {
            name: value for name, read in self._sections.items()
            if (value := read()) is not None
        }

    def _save(self, *, force: bool) -> None:
        if not self._live or not self._writable:
            return
        record = self.record()
        if record == self._last and not force:
            return
        self._last = record
        data = {SAVED_AT_KEY: dt_util.utcnow().timestamp(), UNIT_KEY: self._unit, **record}
        self._store.async_delay_save(lambda: data, 0)
