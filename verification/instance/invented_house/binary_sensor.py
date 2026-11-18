"""One invented door/window sensor and one occupancy sensor per room.

Occupancy is read by MXZ only where a journey wires it as a room's Comfort
priority entity; the demo model's rules never read it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

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
    rooms = bench.house["rooms"]
    add([*(InventedWindow(bench, room) for room in rooms), *(InventedOccupancy(bench, room) for room in rooms)])


class InventedWindow(InventedEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.WINDOW

    def __init__(self, bench: Bench, room: dict[str, Any]) -> None:
        super().__init__(bench, room, "window")

    @property
    def is_on(self) -> bool:
        return self.bench.devices.rooms[self.key].window_open


class InventedOccupancy(InventedEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, bench: Bench, room: dict[str, Any]) -> None:
        super().__init__(bench, room, "occupancy")

    @property
    def is_on(self) -> bool:
        return self.bench.devices.rooms[self.key].occupied
