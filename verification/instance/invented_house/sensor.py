"""One invented room temperature sensor per room."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature

from . import DOMAIN
from .entity import InventedEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import Bench


async def async_setup_platform(
    hass: HomeAssistant, config: Any, add: AddEntitiesCallback, discovery_info: Any = None
) -> None:
    bench: Bench = hass.data[DOMAIN]
    add(InventedTemperature(bench, room) for room in bench.house["rooms"])


class InventedTemperature(InventedEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    def __init__(self, bench: Bench, room: dict[str, Any]) -> None:
        super().__init__(bench, room, "temperature")

    @property
    def native_value(self) -> float:
        return self.bench.devices.rooms[self.key].temperature
