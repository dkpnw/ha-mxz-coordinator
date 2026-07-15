"""Native single-target room thermostats (HomeKit/Google-facing facade).

Each room (primary, secondary) is exposed as a single-setpoint thermostat that
HomeKit/Google render as a clean Tesla/Nest-style "Auto" tile: one number, the
system picks heat vs cool. These entities own NO state of their own. They READ the
coordinator's plan/targets for display and WRITE by driving the integration's own
``number.*_target`` / ``switch.*_enable`` entities (the single source of truth +
restore layer) -- exactly as the echavet proxy's ``coordinator_single_target`` mode
drives the legacy ``input_number``/``input_boolean`` helpers, redirected to this
integration's entities. The coordinator stays the sole writer to the real heads.

Optional vane ``select`` entities are mirrored as swing modes so the native tile
keeps vane control, removing the need for the proxy entirely.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    MODE_COOL,
    MODE_HEAT,
    UNAVAILABLE_STATES,
)
from .coordinator import MXZCoordinator, Zone
from .entity import MXZEntity

# Independent horizontal swing was added in HA 2024.12; guard for older cores.
_HAS_HORIZONTAL_SWING = hasattr(ClimateEntityFeature, "SWING_HORIZONTAL_MODE")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one single-target room thermostat per zone."""
    coordinator: MXZCoordinator = entry.runtime_data
    async_add_entities(
        MXZRoomClimate(coordinator, zone) for zone in coordinator.zones
    )


class MXZRoomClimate(MXZEntity, CoordinatorEntity[MXZCoordinator], ClimateEntity):
    """A single-target thermostat facade over one zone's helper entities."""

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]
    _attr_icon = "mdi:home-thermometer"
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        MXZEntity.__init__(self, coordinator, f"{zone.slug}_thermostat")
        CoordinatorEntity.__init__(self, coordinator)
        self._zone = zone
        if zone.index >= 2:
            self._attr_translation_key = "zone_thermostat"
            self._attr_translation_placeholders = {"zone": zone.name}

        # Report the HA system temperature unit + its resolution (°F: whole
        # degrees; °C: 0.5° steps).
        self._attr_temperature_unit = coordinator.temp_unit
        self._attr_target_temperature_step = coordinator.target_step

        # Clamp the setpoint slider to the firmware operating band (the same
        # [clamp_min, clamp_max] the coordinator clamps head setpoints to), so
        # HomeKit/Google won't offer targets the heads can't actually reach.
        self._attr_min_temp = float(coordinator.clamp_min)
        self._attr_max_temp = float(coordinator.clamp_max)

        # The underlying head this tile passes fan/vane control through to.
        self._head_id = zone.climate_id
        self._vane_vertical_id = zone.vane_vertical_id
        self._vane_horizontal_id = zone.vane_horizontal_id

        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self._vane_vertical_id:
            features |= ClimateEntityFeature.SWING_MODE
        if self._vane_horizontal_id and _HAS_HORIZONTAL_SWING:
            features |= ClimateEntityFeature.SWING_HORIZONTAL_MODE
        self._attr_supported_features = features

    async def async_added_to_hass(self) -> None:
        """Re-render when the underlying head changes (fan/vane reflected live)."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._head_id], self._handle_head_change
            )
        )

    @callback
    def _handle_head_change(self, _event: Event) -> None:
        self.async_write_ha_state()

    def _sibling_eid(self, platform: str, key: str) -> str | None:
        """Resolve a sibling number/switch entity_id by its unique_id suffix.

        Resolved lazily (not cached at add-time): platforms are forwarded
        concurrently, so the number/switch entities may not be registered yet
        when this climate entity is added. By the time a write fires, they are.
        """
        reg = er.async_get(self.hass)
        entry_id = self.coordinator.config_entry.entry_id
        return reg.async_get_entity_id(platform, DOMAIN, f"{entry_id}_{key}")

    # -- room-state plumbing ------------------------------------------------
    @property
    def _enabled(self) -> bool:
        return self._zone.enable

    @property
    def _sensor_id(self) -> str:
        return self._zone.sensor_id

    # -- display ------------------------------------------------------------
    @property
    def current_temperature(self) -> float | None:
        """The room sensor the coordinator reads; None on dropout (no fake value)."""
        state = self.hass.states.get(self._sensor_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    @property
    def target_temperature(self) -> float | None:
        return self._zone.target

    @property
    def hvac_mode(self) -> HVACMode:
        """OFF when the room is disabled; otherwise the single-target auto mode."""
        return HVACMode.HEAT_COOL if self._enabled else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction:
        """Derive from the coordinator's plan engage state for this room."""
        if not self.coordinator.coordinator_enable or not self._enabled:
            return HVACAction.OFF
        engage = self.coordinator.data.get(f"{self._zone.slug}_engage")
        if engage == MODE_COOL:
            return HVACAction.COOLING
        if engage == MODE_HEAT:
            return HVACAction.HEATING
        return HVACAction.IDLE  # satisfied / off / neutral

    # -- vane / swing (optional passthrough to a select entity) -------------
    @property
    def swing_modes(self) -> list[str] | None:
        return self._vane_options(self._vane_vertical_id)

    @property
    def swing_mode(self) -> str | None:
        return self._vane_state(self._vane_vertical_id)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        await self._set_vane(self._vane_vertical_id, swing_mode)

    @property
    def swing_horizontal_modes(self) -> list[str] | None:
        return self._vane_options(self._vane_horizontal_id)

    @property
    def swing_horizontal_mode(self) -> str | None:
        return self._vane_state(self._vane_horizontal_id)

    async def async_set_swing_horizontal_mode(
        self, swing_horizontal_mode: str
    ) -> None:
        await self._set_vane(self._vane_horizontal_id, swing_horizontal_mode)

    def _vane_options(self, vane_id: str | None) -> list[str] | None:
        if not vane_id:
            return None
        state = self.hass.states.get(vane_id)
        return state.attributes.get("options") if state else None

    def _vane_state(self, vane_id: str | None) -> str | None:
        if not vane_id:
            return None
        state = self.hass.states.get(vane_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            return None
        return state.state

    async def _set_vane(self, vane_id: str | None, option: str) -> None:
        if not vane_id:
            return
        # The coordinator applies it — and if the head is OFF (eco/away), it
        # briefly kicks the head into fan_only so the louvre physically moves,
        # then hands the head back to the plan.
        await self.coordinator.async_apply_vane(self._head_id, vane_id, option)

    # -- fan (passthrough to the underlying head) ---------------------------
    @property
    def fan_modes(self) -> list[str] | None:
        state = self.hass.states.get(self._head_id)
        return state.attributes.get("fan_modes") if state else None

    @property
    def fan_mode(self) -> str | None:
        state = self.hass.states.get(self._head_id)
        return state.attributes.get("fan_mode") if state else None

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Fan is independent of the coordinator's mode/setpoints -> drive the head."""
        await self.hass.services.async_call(
            "climate",
            "set_fan_mode",
            {"entity_id": self._head_id, "fan_mode": fan_mode},
            blocking=True,
        )

    # -- write paths (drive the helper entities; never coordinator state) ---
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Single-target write -> drive the room's number entity."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        target_eid = self._sibling_eid("number", f"{self._zone.slug}_target")
        if temp is None or target_eid is None:
            return
        # Snap to the unit resolution (whole °F / 0.5 °C). The number entity
        # seeds the coordinator + recomputes (number.py).
        step = self.coordinator.target_step or 1.0
        value = round(float(temp) / step) * step
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": target_eid, "value": value},
            blocking=True,
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._set_enable(hvac_mode != HVACMode.OFF)

    async def async_turn_on(self) -> None:
        await self._set_enable(True)

    async def async_turn_off(self) -> None:
        await self._set_enable(False)

    async def _set_enable(self, on: bool) -> None:
        enable_eid = self._sibling_eid("switch", f"{self._zone.slug}_enable")
        if enable_eid is None:
            return
        # The switch entity seeds the coordinator + recomputes (switch.py).
        await self.hass.services.async_call(
            "switch",
            "turn_on" if on else "turn_off",
            {"entity_id": enable_eid},
            blocking=True,
        )
