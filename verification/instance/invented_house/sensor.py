"""One invented room temperature sensor per room."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import DOMAIN, OUTDOOR, signal
from .entity import InventedEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import Bench


async def async_setup_platform(
    hass: HomeAssistant, config: Any, add: AddEntitiesCallback, discovery_info: Any = None
) -> None:
    bench: Bench = hass.data[DOMAIN]
    add([*(InventedTemperature(bench, room) for room in bench.house["rooms"]), OutdoorUnit(bench)])


class InventedTemperature(InventedEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    def __init__(self, bench: Bench, room: dict[str, Any]) -> None:
        super().__init__(bench, room, "temperature")

    @property
    def native_value(self) -> float:
        return self.bench.devices.rooms[self.key].temperature


class OutdoorUnit(SensorEntity):
    """The direction the demo model's outdoor unit runs: heat, cool, or none."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Outdoor unit"
    _attr_unique_id = f"{DOMAIN}_{OUTDOOR}"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, bench: Bench) -> None:
        self.bench = bench
        self._attr_options = ["heat", "cool", "none"]

    @property
    def native_value(self) -> str | None:
        """Unknown while the model is off: nothing then decides a direction."""
        if not self.bench.modelled:
            return None
        return self.bench.devices.outdoor or "none"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, signal(OUTDOOR), self.async_write_ha_state))
