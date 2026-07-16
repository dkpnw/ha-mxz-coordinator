"""Setpoint target numbers (replaces input_number.hvac_*_target)."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import KEY_PRIMARY_TARGET, KEY_SECONDARY_TARGET
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

    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermostat"

    def __init__(self, coordinator: MXZCoordinator, key: str, *, primary: bool) -> None:
        super().__init__(coordinator, key)
        self._primary = primary
        # Track the HA system temperature unit + resolution (°F: whole degrees;
        # °C: 0.5° steps). Match the climate tile: bound the target to the
        # firmware operating band [clamp_min, clamp_max].
        self._attr_native_unit_of_measurement = coordinator.temp_unit
        self._attr_native_step = coordinator.target_step
        self._attr_native_min_value = float(coordinator.clamp_min)
        self._attr_native_max_value = float(coordinator.clamp_max)
        self._attr_native_value = coordinator.target_default

    async def async_added_to_hass(self) -> None:
        """Restore the last setpoint and seed the coordinator.

        On a FRESH install (nothing to restore) the target seeds from the
        head's current setpoint instead of a hard default, so enabling the
        coordinator never plans against a temperature nobody chose (#6 —
        a 70 °F default vs a 66 °F room planned heat in July).
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if (
            not self._restored_state_is_stale(last_state)
            and (last := await self.async_get_last_number_data())
            and last.native_value is not None
        ):
            self._attr_native_value = last.native_value
        elif (seed := self._head_setpoint()) is not None:
            self._attr_native_value = seed
        self._seed()

    def _seed(self) -> None:
        if self._primary:
            self.coordinator.primary_target = self._attr_native_value
        else:
            self.coordinator.secondary_target = self._attr_native_value

    @property
    def _climate_id(self) -> str:
        return (
            self.coordinator.primary_climate_id
            if self._primary
            else self.coordinator.secondary_climate_id
        )

    def _head_setpoint(self) -> float | None:
        """The head's current setpoint, clamped and snapped to our resolution."""
        state = self.hass.states.get(self._climate_id)
        if state is None:
            return None
        attrs = state.attributes
        raw = attrs.get("temperature")
        if raw is None and attrs.get("target_temp_low") is not None:
            try:
                raw = (
                    float(attrs["target_temp_low"])
                    + float(attrs.get("target_temp_high", attrs["target_temp_low"]))
                ) / 2
            except (TypeError, ValueError):
                raw = None
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        step = self.coordinator.target_step or 1.0
        value = round(value / step) * step
        return min(max(value, self._attr_native_min_value), self._attr_native_max_value)

    def _head_setpoint(self) -> float | None:
        """The head's current setpoint, clamped and snapped to our resolution."""
        state = self.hass.states.get(self._climate_id)
        if state is None:
            return None
        attrs = state.attributes
        raw = attrs.get("temperature")
        if raw is None and attrs.get("target_temp_low") is not None:
            try:
                raw = (
                    float(attrs["target_temp_low"])
                    + float(attrs.get("target_temp_high", attrs["target_temp_low"]))
                ) / 2
            except (TypeError, ValueError):
                raw = None
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        step = self.coordinator.target_step or 1.0
        value = round(value / step) * step
        return min(max(value, self._attr_native_min_value), self._attr_native_max_value)

    async def async_set_native_value(self, value: float) -> None:
        """User changed the target -> persist and recompute."""
        self._attr_native_value = value
        self._seed()
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()
