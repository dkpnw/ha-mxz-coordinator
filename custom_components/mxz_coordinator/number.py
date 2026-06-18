"""Setpoint target numbers (replaces input_number.hvac_*_target)."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    KEY_PRIMARY_TARGET,
    KEY_SECONDARY_TARGET,
    TARGET_DEFAULT,
    TARGET_STEP,
)
from .coordinator import MXZCoordinator
from .entity import MXZEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the two target numbers."""
    coordinator: MXZCoordinator = entry.runtime_data
    async_add_entities(
        [
            MXZTargetNumber(coordinator, KEY_PRIMARY_TARGET, primary=True),
            MXZTargetNumber(coordinator, KEY_SECONDARY_TARGET, primary=False),
        ]
    )


class MXZTargetNumber(MXZEntity, RestoreNumber):
    """A restorable comfort-target setpoint, bounded to the firmware band."""

    _attr_native_step = float(TARGET_STEP)
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermostat"

    def __init__(self, coordinator: MXZCoordinator, key: str, *, primary: bool) -> None:
        super().__init__(coordinator, key)
        self._primary = primary
        # Match the climate tile: bound the target to the firmware operating
        # band [clamp_min, clamp_max] so the slider can't request the
        # unreachable, and the tile (same bounds) can set the full range.
        self._attr_native_min_value = float(coordinator.clamp_min)
        self._attr_native_max_value = float(coordinator.clamp_max)
        self._attr_native_value = float(TARGET_DEFAULT)

    async def async_added_to_hass(self) -> None:
        """Restore the last setpoint and seed the coordinator."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and (
            last.native_value is not None
        ):
            self._attr_native_value = last.native_value
        self._seed()

    def _seed(self) -> None:
        if self._primary:
            self.coordinator.primary_target = self._attr_native_value
        else:
            self.coordinator.secondary_target = self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        """User changed the target -> persist and recompute."""
        self._attr_native_value = value
        self._seed()
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()
