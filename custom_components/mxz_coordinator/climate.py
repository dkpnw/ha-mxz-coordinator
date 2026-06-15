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
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    KEY_PRIMARY_ENABLE,
    KEY_PRIMARY_TARGET,
    KEY_PRIMARY_THERMOSTAT,
    KEY_SECONDARY_ENABLE,
    KEY_SECONDARY_TARGET,
    KEY_SECONDARY_THERMOSTAT,
    MODE_COOL,
    MODE_HEAT,
    TARGET_MAX,
    TARGET_MIN,
    TARGET_STEP,
    UNAVAILABLE_STATES,
)
from .coordinator import MXZCoordinator
from .entity import MXZEntity

# Independent horizontal swing was added in HA 2024.12; guard for older cores.
_HAS_HORIZONTAL_SWING = hasattr(ClimateEntityFeature, "SWING_HORIZONTAL_MODE")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the two single-target room thermostats."""
    coordinator: MXZCoordinator = entry.runtime_data
    async_add_entities(
        [
            MXZRoomClimate(coordinator, KEY_PRIMARY_THERMOSTAT, primary=True),
            MXZRoomClimate(coordinator, KEY_SECONDARY_THERMOSTAT, primary=False),
        ]
    )


class MXZRoomClimate(MXZEntity, CoordinatorEntity[MXZCoordinator], ClimateEntity):
    """A single-target thermostat facade over one room's helper entities."""

    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]
    _attr_min_temp = float(TARGET_MIN)
    _attr_max_temp = float(TARGET_MAX)
    _attr_target_temperature_step = float(TARGET_STEP)
    _attr_icon = "mdi:home-thermometer"
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self, coordinator: MXZCoordinator, key: str, *, primary: bool
    ) -> None:
        MXZEntity.__init__(self, coordinator, key)
        CoordinatorEntity.__init__(self, coordinator)
        self._primary = primary

        self._vane_vertical_id = (
            coordinator.primary_vane_vertical_id
            if primary
            else coordinator.secondary_vane_vertical_id
        )
        self._vane_horizontal_id = (
            coordinator.primary_vane_horizontal_id
            if primary
            else coordinator.secondary_vane_horizontal_id
        )

        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self._vane_vertical_id:
            features |= ClimateEntityFeature.SWING_MODE
        if self._vane_horizontal_id and _HAS_HORIZONTAL_SWING:
            features |= ClimateEntityFeature.SWING_HORIZONTAL_MODE
        self._attr_supported_features = features

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
        return (
            self.coordinator.primary_enable
            if self._primary
            else self.coordinator.secondary_enable
        )

    @property
    def _sensor_id(self) -> str:
        return (
            self.coordinator.primary_sensor_id
            if self._primary
            else self.coordinator.secondary_sensor_id
        )

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
        return (
            self.coordinator.primary_target
            if self._primary
            else self.coordinator.secondary_target
        )

    @property
    def hvac_mode(self) -> HVACMode:
        """OFF when the room is disabled; otherwise the single-target auto mode."""
        return HVACMode.HEAT_COOL if self._enabled else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction:
        """Derive from the coordinator's plan engage state for this room."""
        if not self.coordinator.coordinator_enable or not self._enabled:
            return HVACAction.OFF
        key = "primary_engage" if self._primary else "secondary_engage"
        engage = self.coordinator.data.get(key)
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
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": vane_id, "option": option},
            blocking=True,
        )

    # -- write paths (drive the helper entities; never coordinator state) ---
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Single-target write -> drive the room's number entity."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        target_key = KEY_PRIMARY_TARGET if self._primary else KEY_SECONDARY_TARGET
        target_eid = self._sibling_eid("number", target_key)
        if temp is None or target_eid is None:
            return
        # The number entity seeds the coordinator + recomputes (number.py).
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": target_eid, "value": round(float(temp))},
            blocking=True,
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._set_enable(hvac_mode != HVACMode.OFF)

    async def async_turn_on(self) -> None:
        await self._set_enable(True)

    async def async_turn_off(self) -> None:
        await self._set_enable(False)

    async def _set_enable(self, on: bool) -> None:
        enable_key = KEY_PRIMARY_ENABLE if self._primary else KEY_SECONDARY_ENABLE
        enable_eid = self._sibling_eid("switch", enable_key)
        if enable_eid is None:
            return
        # The switch entity seeds the coordinator + recomputes (switch.py).
        await self.hass.services.async_call(
            "switch",
            "turn_on" if on else "turn_off",
            {"entity_id": enable_eid},
            blocking=True,
        )
