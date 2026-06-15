"""Shared base for MXZ Coordinator entities (device grouping + unique ids)."""

from __future__ import annotations

from homeassistant.helpers.device_info import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import MXZCoordinator


class MXZEntity(Entity):
    """Base: groups every helper under one device and names it via translations."""

    _attr_has_entity_name = True
    _attr_should_poll = False

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
        )
