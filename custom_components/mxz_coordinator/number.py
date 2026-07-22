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
        # Every zone uses the generic translated name with its own name
        # substituted in ("Bedroom target").
        self._attr_translation_key = "zone_target"
        self._attr_translation_placeholders = {"zone": zone.name}
        # Track the HA system temperature unit + resolution (°F: whole degrees;
        # °C: 0.5° steps). Match the climate tile: bound the target to the
        # firmware operating band [clamp_min, clamp_max].
        self._attr_native_unit_of_measurement = coordinator.temp_unit
        self._attr_native_step = coordinator.target_step
        lo, hi = coordinator.head_target_bounds(zone.climate_id)
        self._attr_native_value = min(max(coordinator.target_default, lo), hi)

    # [clamp_min, clamp_max] narrowed to what THIS head will actually accept
    # (its native operating band), so a rejectable target can't be entered from
    # the UI / HomeKit / voice in the first place (#10). LIVE properties, not
    # frozen at init: a head whose integration loads after ours would otherwise
    # keep the wide fallback bounds until a reload. Validation reads these at
    # set-time, so it is always against the head's real band; the UI slider
    # refreshes its cached bounds on the entity's next state write.
    @property
    def native_min_value(self) -> float:
        return self.coordinator.head_target_bounds(self._zone.climate_id)[0]

    @property
    def native_max_value(self) -> float:
        return self.coordinator.head_target_bounds(self._zone.climate_id)[1]

    async def async_added_to_hass(self) -> None:
        """Restore the last setpoint and seed the coordinator's zone.

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
        self._zone.target = self._attr_native_value

    def _head_setpoint(self) -> float | None:
        """The head's current setpoint, clamped and snapped to our resolution."""
        state = self.hass.states.get(self._zone.climate_id)
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
        return min(max(value, self.native_min_value), self.native_max_value)

    async def async_set_native_value(self, value: float) -> None:
        """User changed the target -> persist, reset the latch, recompute."""
        self._attr_native_value = value
        self._zone.target = value
        self.coordinator.reset_engage_latch(self._zone.slug)
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()
