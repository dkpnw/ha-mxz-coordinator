"""On/off helpers: per-room enables, eco-idle, and the coordinator kill-switch.

Replaces the input_boolean.* helpers from the YAML package. All default OFF on a
fresh install (matching the package, where `initial` was omitted) and restore their
last state across restarts.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    KEY_COOL_LOCKOUT,
    KEY_COORDINATOR_ENABLE,
    KEY_ECO_IDLE,
    KEY_HEAT_LOCKOUT,
    KEY_PRIMARY_ENABLE,
    KEY_SECONDARY_ENABLE,
)
from .coordinator import MXZCoordinator
from .entity import MXZEntity

_ICONS = {
    KEY_PRIMARY_ENABLE: "mdi:bed",
    KEY_SECONDARY_ENABLE: "mdi:sofa",
    KEY_COORDINATOR_ENABLE: "mdi:hvac",
    KEY_ECO_IDLE: "mdi:leaf",
    KEY_HEAT_LOCKOUT: "mdi:fire-off",
    KEY_COOL_LOCKOUT: "mdi:snowflake-off",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the room/coordinator switches."""
    coordinator: MXZCoordinator = entry.runtime_data
    async_add_entities(
        MXZSwitch(coordinator, key)
        for key in (
            KEY_PRIMARY_ENABLE,
            KEY_SECONDARY_ENABLE,
            KEY_COORDINATOR_ENABLE,
            KEY_ECO_IDLE,
            KEY_HEAT_LOCKOUT,
            KEY_COOL_LOCKOUT,
        )
    )


class MXZSwitch(MXZEntity, SwitchEntity, RestoreEntity):
    """A restorable on/off helper that seeds a coordinator field."""

    def __init__(self, coordinator: MXZCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._key = key
        self._attr_icon = _ICONS[key]
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        """Restore last state and seed the coordinator."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
        self._seed()

    def _seed(self) -> None:
        setattr(self.coordinator, self._key, self._attr_is_on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        self._attr_is_on = value
        self._seed()
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()
