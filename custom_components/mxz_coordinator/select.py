"""Shared-mode select (replaces input_select.hvac_shared_mode).

Offers the two directions one MXZ outdoor unit can share — `cool` and `heat`.
Restores the last value across restarts (no default) so the shared mode "rests" at
whatever was last called; cold start falls back to cool. The coordinator updates this
when it flips the mode, and the user can override it here.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    KEY_SHARED_MODE,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
)
from .coordinator import MXZCoordinator
from .entity import MXZEntity

_LOGGER = logging.getLogger(__name__)

_OPTIONS = [MODE_COOL, MODE_HEAT]

# Values this select stored while it also offered fan_only and off. Neither was
# a dependable stop or park: arbitration resolves anything that is not cool|heat
# to cool (coordinator._compute), so picking one left the coordinator running in
# cool — which could park a head that had been heating — and, on the enabled,
# non-parked apply path, wrote cool back over the selection. Disabled or held in
# a fixed-mode standby park, the stored value was retained instead. The stored
# value therefore does not say which direction was running; the migration lands
# on the same cool fallback, starting no hold and turning nothing off. What does
# stop the system is the Coordinator enable kill-switch; what holds the heads is
# the standby hold (eco still conditions protection extremes; off/fan_only park).
_LEGACY_OPTIONS = {MODE_FAN_ONLY: MODE_COOL, MODE_OFF: MODE_COOL}

ISSUE_LEGACY_OPTION = "shared_mode_legacy_option"


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
        last = await self.async_get_last_state()
        if last is not None and not self._restored_state_is_stale(last):
            if last.state in _OPTIONS:
                self._attr_current_option = last.state
            elif last.state in _LEGACY_OPTIONS:
                self._attr_current_option = _LEGACY_OPTIONS[last.state]
                self._explain_dropped_option(last.state)
        self.coordinator.current_shared_mode = self._attr_current_option
        self.async_on_remove(self.coordinator.async_add_listener(self._sync))

    def _explain_dropped_option(self, stored: str) -> None:
        """Say once that the dropped choice was no dependable stop, and what is."""
        _LOGGER.info(
            "MXZ: shared mode %r is no longer offered — arbitration resolved it to %s, "
            "so the selector now reads %s. Coordinator enable stops the coordinator; "
            "the standby hold holds the heads (eco still conditions protection "
            "extremes; off/fan_only park)",
            stored,
            self._attr_current_option,
            self._attr_current_option,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{self.coordinator.config_entry.entry_id}_{ISSUE_LEGACY_OPTION}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_LEGACY_OPTION,
            translation_placeholders={
                "stored": stored,
                "current": self._attr_current_option,
            },
        )

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
