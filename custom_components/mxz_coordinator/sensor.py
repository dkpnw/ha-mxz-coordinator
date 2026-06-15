"""The MXZ Plan sensor (replaces the decision template sensor.mxz_plan).

State = the chosen shared mode (cool|heat). Attributes expose the decision internals
(demand/engage per room, standoff, sensors_ok, hysteresis) for debugging — the same
attributes the YAML template sensor published.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import KEY_PLAN
from .coordinator import MXZCoordinator
from .entity import MXZEntity

_ATTRS = (
    "primary_demand",
    "secondary_demand",
    "primary_engage",
    "secondary_engage",
    "standoff",
    "sensors_ok",
    "seconds_since_mode_change",
    "mode_change_allowed",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the plan sensor."""
    coordinator: MXZCoordinator = entry.runtime_data
    async_add_entities([MXZPlanSensor(coordinator)])


class MXZPlanSensor(MXZEntity, CoordinatorEntity[MXZCoordinator], SensorEntity):
    """Read-only view of the coordinator's computed plan."""

    _attr_icon = "mdi:head-cog"

    def __init__(self, coordinator: MXZCoordinator) -> None:
        MXZEntity.__init__(self, coordinator, KEY_PLAN)
        CoordinatorEntity.__init__(self, coordinator)

    @property
    def native_value(self) -> str | None:
        """The chosen shared mode."""
        return self.coordinator.data.get("state")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {key: data.get(key) for key in _ATTRS}
