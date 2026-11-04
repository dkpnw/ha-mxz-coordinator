"""Repair issues for configured entities that are gone, and stay gone.

Three faults a person can fix, one issue per entity:

* a head that has been missing or unavailable (``head_unavailable``);
* a room temperature sensor, or the door/window sensor of a room with the pause
  on, that has been missing (``sensor_missing``). Unavailable is not missing: a
  sensor that is only offline is already in the room's ``sensor_health``;
* any configured entity that was deleted or no longer exists (``entity_deleted``).

A fault must last ``REPAIR_DEBOUNCE`` without a break before its issue appears, and
any recovery starts that wait over, so a blip or a flapping entity raises nothing.
The clock starts once Home Assistant has started, because entities are missing
while it boots. An issue clears itself when its fault does, and when the entry
unloads; a reload or restart then times the fault again.

This module only reads states and the entity registry and only writes the issue
registry. It takes nothing from the coordinator but its wiring and tells it nothing,
so no control decision can depend on it.

Cloud and generic heads keep their own ``*_head_unresponsive`` issue, so they get
no ``head_unavailable`` too. Their deleted entity is still reported.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import Event, HassJob, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_entity_registry_updated_event,
)
from homeassistant.helpers.start import async_at_started

from .const import DOMAIN, HEAD_TYPE_LOCAL

if TYPE_CHECKING:
    from .coordinator import MXZCoordinator

_LOGGER = logging.getLogger(__name__)

REPAIR_DEBOUNCE = timedelta(minutes=15)
LEARN_MORE_URL = "https://github.com/dkpnw/ha-mxz-coordinator#repair-issues"

FAULT_HEAD = "head_unavailable"
FAULT_SENSOR = "sensor_missing"
FAULT_DELETED = "entity_deleted"
_FAULTS = (FAULT_HEAD, FAULT_SENSOR, FAULT_DELETED)

# What MXZ needs an entity for, strongest first: an entity used twice is watched
# for the strongest use.
_ROLE_HEAD = 0
_ROLE_SENSOR = 1
_ROLE_OPTIONAL = 2


@dataclass
class _Reference:
    """One configured entity: its strongest use, and every place it is used."""

    role: int
    uses: list[str] = field(default_factory=list)
    # A cloud or generic head: its own issue already covers unavailable.
    reports_itself: bool = False


def _references(coordinator: MXZCoordinator) -> dict[str, _Reference]:
    """Every entity the coordinator was configured with, as it resolved them."""
    found: dict[str, _Reference] = {}

    def add(entity_id: str | None, role: int, use: str, *, reports_itself: bool = False) -> None:
        if not entity_id:
            return
        reference = found.setdefault(entity_id, _Reference(role))
        reference.role = min(reference.role, role)
        reference.uses.append(use)
        reference.reports_itself = reference.reports_itself or reports_itself

    for zone in coordinator.zones:
        add(
            zone.climate_id, _ROLE_HEAD, f"{zone.name}: head",
            reports_itself=zone.head_type != HEAD_TYPE_LOCAL,
        )
        add(zone.sensor_id, _ROLE_SENSOR, f"{zone.name}: temperature sensor")
        # A door/window sensor with the pause off does nothing, so only its
        # deletion is worth a notice.
        add(
            zone.opening_entity,
            _ROLE_SENSOR if zone.opening_active else _ROLE_OPTIONAL,
            f"{zone.name}: door or window sensor",
        )
        add(zone.vane_vertical_id, _ROLE_OPTIONAL, f"{zone.name}: vertical vane")
        add(zone.vane_horizontal_id, _ROLE_OPTIONAL, f"{zone.name}: horizontal vane")
        add(zone.stage_sensor_id, _ROLE_OPTIONAL, f"{zone.name}: airflow sensor")
        add(zone.safety_priority_entity, _ROLE_OPTIONAL, f"{zone.name}: Safety priority sensor")
        add(zone.comfort_priority_entity, _ROLE_OPTIONAL, f"{zone.name}: Comfort priority")
    add(coordinator.changeover_entity, _ROLE_OPTIONAL, "Seasons: weather source")
    add(coordinator.inhibit_entity, _ROLE_OPTIONAL, "Standby hold: hold signal")
    return found


def issue_id(fault: str, entry_id: str, entity_id: str) -> str:
    """The stable issue ID of one fault on one entity."""
    return f"{fault}_{entry_id}_{entity_id}"


@callback
def async_clear_entry_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete every issue this module raised for an entry, active or not."""
    prefixes = tuple(f"{fault}_{entry_id}_" for fault in _FAULTS)
    for domain, known in list(ir.async_get(hass).issues):
        if domain == DOMAIN and known.startswith(prefixes):
            ir.async_delete_issue(hass, DOMAIN, known)


class RepairMonitor:
    """Watches one entry's configured entities and raises and clears its issues."""

    def __init__(self, hass: HomeAssistant, entry_id: str, references: dict[str, _Reference]) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._references = references
        self._unsubs: list[Callable[[], None]] = []
        self._timers: dict[str, Callable[[], None]] = {}
        self._raised: dict[str, str] = {}

    @callback
    def async_start(self) -> None:
        """Start watching once Home Assistant has started."""
        self._unsubs.append(async_at_started(self._hass, self._async_watch))

    @callback
    def _async_watch(self, _hass: HomeAssistant) -> None:
        entity_ids = list(self._references)
        # A filtered bus listener, not async_track_state_change_event: that helper's
        # per-entity jobs are the coordinator's own subscriptions, one per input, which
        # tests/test_hands_off.py counts. The filter is one dictionary lookup per event.
        self._unsubs.append(
            self._hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._on_change, event_filter=self._is_watched
            )
        )
        # A disabled entity has no state to change, so deleting one shows only here.
        self._unsubs.append(
            async_track_entity_registry_updated_event(self._hass, entity_ids, self._on_change)
        )
        for entity_id in entity_ids:
            self._evaluate(entity_id)

    @callback
    def async_stop(self) -> None:
        """Stop watching and clear this entry's issues."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()
        self._raised.clear()
        async_clear_entry_issues(self._hass, self._entry_id)

    @callback
    def _is_watched(self, event_data: Mapping[str, Any]) -> bool:
        return event_data["entity_id"] in self._references

    @callback
    def _on_change(self, event: Event) -> None:
        # A rename's registry event names the new ID, and the watched one as old.
        self._evaluate(event.data.get("old_entity_id", event.data["entity_id"]))

    def _fault(self, entity_id: str) -> str | None:
        """What is wrong with this entity right now, if anything."""
        reference = self._references[entity_id]
        state = self._hass.states.get(entity_id)
        if state is not None:
            unavailable = state.state == STATE_UNAVAILABLE
            head = reference.role == _ROLE_HEAD and not reference.reports_itself
            return FAULT_HEAD if head and unavailable else None
        # No state. An entity still in the registry is disabled or not loaded;
        # one that left it was deleted or renamed. An optional entity that was
        # never there at all can only be a stale reference too.
        registry = er.async_get(self._hass)
        if registry.async_get(entity_id) is None and (
            reference.role == _ROLE_OPTIONAL
            or any(
                deleted.entity_id == entity_id
                for deleted in registry.deleted_entities.values()
            )
        ):
            return FAULT_DELETED
        if reference.role == _ROLE_HEAD:
            return None if reference.reports_itself else FAULT_HEAD
        return FAULT_SENSOR if reference.role == _ROLE_SENSOR else None

    @callback
    def _evaluate(self, entity_id: str) -> None:
        fault = self._fault(entity_id)
        raised = self._raised.get(entity_id)
        if fault is None:
            if cancel := self._timers.pop(entity_id, None):
                cancel()
            if raised is not None:
                del self._raised[entity_id]
                ir.async_delete_issue(
                    self._hass, DOMAIN, issue_id(raised, self._entry_id, entity_id)
                )
        elif raised is not None:
            # Still broken, differently (unavailable, then deleted): the wait is
            # already over, so the issue only changes.
            if raised != fault:
                ir.async_delete_issue(
                    self._hass, DOMAIN, issue_id(raised, self._entry_id, entity_id)
                )
                self._raise(entity_id, fault)
        elif entity_id not in self._timers:
            self._timers[entity_id] = async_call_later(
                self._hass,
                REPAIR_DEBOUNCE,
                HassJob(
                    partial(self._on_debounced, entity_id),
                    "MXZ repair issue wait",
                    cancel_on_shutdown=True,
                ),
            )

    @callback
    def _on_debounced(self, entity_id: str, _now: object) -> None:
        self._timers.pop(entity_id, None)
        if (fault := self._fault(entity_id)) is not None:
            self._raise(entity_id, fault)

    def _raise(self, entity_id: str, fault: str) -> None:
        self._raised[entity_id] = fault
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id(fault, self._entry_id, entity_id),
            is_fixable=False,
            learn_more_url=LEARN_MORE_URL,
            severity=ir.IssueSeverity.WARNING,
            translation_key=fault,
            translation_placeholders={
                "entity": entity_id,
                "uses": "; ".join(self._references[entity_id].uses),
                "minutes": str(int(REPAIR_DEBOUNCE.total_seconds() // 60)),
            },
        )


@callback
def async_start_repair_monitor(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: MXZCoordinator
) -> None:
    """Start the entry's monitor. A failure here costs the notices, never the control."""
    try:
        monitor = RepairMonitor(hass, entry.entry_id, _references(coordinator))
        entry.async_on_unload(monitor.async_stop)
        monitor.async_start()
    except Exception:
        _LOGGER.exception("MXZ: repair issue monitor did not start; control is unaffected")
