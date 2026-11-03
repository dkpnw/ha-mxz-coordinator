"""Per-room door/window pause indicator (only for a room with the pause on).

On while the room is paused for its open door/window: its head is off and it
has no vote. A diagnostic, so automations can follow the pause without reading
the thermostat's attributes. The pause itself is the coordinator's.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import MXZCoordinator, Zone
from .entity import MXZEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """One paused indicator per room with a door/window pause."""
    coordinator: MXZCoordinator = entry.runtime_data
    async_add_entities(
        MXZPausedOpenSensor(coordinator, zone)
        for zone in coordinator.zones
        if zone.opening_active
    )


class MXZPausedOpenSensor(MXZEntity, CoordinatorEntity[MXZCoordinator], BinarySensorEntity):
    """On while the room is paused because its door/window is open."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:window-open-variant"

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        MXZEntity.__init__(self, coordinator, f"{zone.slug}_paused_open")
        CoordinatorEntity.__init__(self, coordinator)
        self._zone = zone
        self._attr_translation_key = "zone_paused_open"
        self._attr_translation_placeholders = {"zone": zone.name}

    @property
    def is_on(self) -> bool:
        return self.coordinator._opening_paused(self._zone)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.opening_attributes(self._zone)
