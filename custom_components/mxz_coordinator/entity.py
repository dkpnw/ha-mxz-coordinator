"""Shared base for MXZ Coordinator entities (device grouping + unique ids)."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import MXZCoordinator


class MXZEntity(Entity):
    """Base: groups every helper under one device and names it via translations."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def _restored_state_is_stale(self, last_state) -> bool:
        """True if a restored state predates this config entry.

        HA keeps restore data for removed entities ~7 days. When an entry is
        deleted and re-added, the new entities reuse the old entity ids and
        would resurrect the PREVIOUS install's values (#7) — stale targets, or
        worse, a stale kill-switch ON. A restore older than the entry's own
        creation belongs to a previous incarnation and must be ignored.
        """
        created = self.coordinator.config_entry.created_at
        return (
            last_state is not None
            and created is not None
            and last_state.last_updated < created
        )

    def __init__(self, coordinator: MXZCoordinator, key: str) -> None:
        self.coordinator = coordinator
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="MXZ Coordinator",
            manufacturer="MXZ Coordinator",
            model="Multi-zone mini-split coordinator",
            # "Visit" link on the device page -> the docs, incl. the Removing
            # section (the device intentionally has no Delete button — the
            # config entry is the removal handle; see issue #5).
            configuration_url="https://github.com/dkpnw/ha-mxz-coordinator#removing",
        )
