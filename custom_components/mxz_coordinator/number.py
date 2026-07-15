"""Per-zone setpoint target numbers (replaces input_number.hvac_*_target)."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MXZCoordinator, Zone
from .entity import MXZEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one target number per zone."""
    coordinator: MXZCoordinator = entry.runtime_data
    async_add_entities(
        MXZTargetNumber(coordinator, zone) for zone in coordinator.zones
    )


class MXZTargetNumber(MXZEntity, RestoreNumber):
    """A restorable comfort-target setpoint, bounded to the firmware band."""

    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermostat"

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"{zone.slug}_target")
        self._zone = zone
        if zone.index >= 2:
            # Zones beyond the legacy pair use the generic translated name with
            # the zone's own name substituted in ("Bedroom target").
            self._attr_translation_key = "zone_target"
            self._attr_translation_placeholders = {"zone": zone.name}
        # Track the HA system temperature unit + resolution (°F: whole degrees;
        # °C: 0.5° steps). Match the climate tile: bound the target to the
        # firmware operating band [clamp_min, clamp_max].
        self._attr_native_unit_of_measurement = coordinator.temp_unit
        self._attr_native_step = coordinator.target_step
        self._attr_native_min_value = float(coordinator.clamp_min)
        self._attr_native_max_value = float(coordinator.clamp_max)
        self._attr_native_value = coordinator.target_default

    async def async_added_to_hass(self) -> None:
        """Restore the last setpoint and seed the coordinator's zone."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and (
            last.native_value is not None
        ):
            self._attr_native_value = last.native_value
        self._zone.target = self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        """User changed the target -> persist and recompute."""
        self._attr_native_value = value
        self._zone.target = value
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()
