"""Shared-mode select (replaces input_select.hvac_shared_mode).

Restores the last value across restarts (no default) so the shared mode "rests" at
whatever was last called; cold start falls back to cool. The coordinator updates this
when it flips the mode, and the user can override it here.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    KEY_SHARED_MODE,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
)
from .coordinator import MXZCoordinator
from .entity import MXZEntity

_OPTIONS = [MODE_COOL, MODE_HEAT, MODE_FAN_ONLY, MODE_OFF]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the shared-mode select."""
    coordinator: MXZCoordinator = entry.runtime_data
    async_add_entities([MXZSharedModeSelect(coordinator)])


class MXZSharedModeSelect(MXZEntity, SelectEntity, RestoreEntity):
    """The current shared mode, persisted across restarts."""

    _attr_options = _OPTIONS
    _attr_icon = "mdi:swap-horizontal"

    def __init__(self, coordinator: MXZCoordinator) -> None:
        super().__init__(coordinator, KEY_SHARED_MODE)
        self._attr_current_option = MODE_COOL

    async def async_added_to_hass(self) -> None:
        """Restore last mode (resting mode) and seed the coordinator."""
        await super().async_added_to_hass()
        if (
            (last := await self.async_get_last_state()) is not None
            and last.state in _OPTIONS
            and not self._restored_state_is_stale(last)
        ):
            self._attr_current_option = last.state
        self.coordinator.current_shared_mode = self._attr_current_option
        self.async_on_remove(self.coordinator.async_add_listener(self._sync))

    @callback
    def _sync(self) -> None:
        """Reflect a coordinator-driven mode flip."""
        if self._attr_current_option != self.coordinator.current_shared_mode:
            self._attr_current_option = self.coordinator.current_shared_mode
            self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Manual override."""
        self._attr_current_option = option
        self.async_write_ha_state()
        await self.coordinator.async_select_shared_mode(option)
