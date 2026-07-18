"""On/off helpers: per-room enables, eco-idle, and the coordinator kill-switch.

Replaces the input_boolean.* helpers from the YAML package. All default OFF on a
fresh install (matching the package, where `initial` was omitted) and restore their
last state across restarts — except the per-zone Fan auto switches, which are live
mirrors of coordinator latch state and restore nothing of their own.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    KEY_COOL_LOCKOUT,
    KEY_COORDINATOR_ENABLE,
    KEY_ECO_IDLE,
    KEY_HEAT_LOCKOUT,
    KEY_PRIMARY_ENABLE,
    KEY_PRIMARY_FAN_AUTO,
    KEY_SECONDARY_ENABLE,
    KEY_SECONDARY_FAN_AUTO,
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
    entities: list[SwitchEntity] = [
        MXZSwitch(coordinator, key)
        for key in (
            KEY_PRIMARY_ENABLE,
            KEY_SECONDARY_ENABLE,
            KEY_COORDINATOR_ENABLE,
            KEY_ECO_IDLE,
            KEY_HEAT_LOCKOUT,
            KEY_COOL_LOCKOUT,
        )
    ]
    entities.extend(
        MXZFanAutoSwitch(coordinator, key, climate_id)
        for key, climate_id in (
            (KEY_PRIMARY_FAN_AUTO, coordinator.primary_climate_id),
            (KEY_SECONDARY_FAN_AUTO, coordinator.secondary_climate_id),
        )
    )
    async_add_entities(entities)


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
        if (
            last := await self.async_get_last_state()
        ) is not None and not self._restored_state_is_stale(last):
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


class MXZFanAutoSwitch(MXZEntity, CoordinatorEntity[MXZCoordinator], SwitchEntity):
    """Per-zone "Fan auto" toggle — a live mirror of the manual-fan latch.

    ON  = boost/auto drives this head's fan (zone not held).
    OFF = a manual speed is being held.

    It is NOT restored: the latch machinery is the single source of truth and
    seeds itself from observed head state on restart, so the switch just reflects
    it (CoordinatorEntity re-renders every cycle). Turning it ON hands control
    back to boost; turning it OFF pins the head's current speed. Apple's Home app
    renders only a climate service's fixed characteristics — there's no room for a
    custom control inside the climate tile — so this rides alongside it as a plain
    toggle and doubles as a visible who's-driving-the-fan indicator.
    """

    _attr_icon = "mdi:fan-auto"

    def __init__(
        self, coordinator: MXZCoordinator, key: str, climate_id: str
    ) -> None:
        MXZEntity.__init__(self, coordinator, key)
        CoordinatorEntity.__init__(self, coordinator)
        self._climate_id = climate_id

    @property
    def is_on(self) -> bool:
        """Mirror the latch: ON when boost drives, OFF when a manual hold is active."""
        return self.coordinator.fan_auto_is_on(self._climate_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Release the zone's latch and let boost reassert."""
        await self.coordinator.async_set_fan_auto(self._climate_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Latch the zone at the head's current fan speed (no-op if it's at auto)."""
        await self.coordinator.async_set_fan_auto(self._climate_id, False)
        self.async_write_ha_state()
