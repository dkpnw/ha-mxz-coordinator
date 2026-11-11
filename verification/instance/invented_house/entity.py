"""The part every invented device shares: its key, its name, and its refresh."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from . import DOMAIN, signal

if TYPE_CHECKING:
    from . import Bench


class InventedEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(self, bench: Bench, room: dict[str, Any], kind: str) -> None:
        self.bench = bench
        self.key: str = room["key"]
        self._attr_name = f"{room['name']} {kind}"
        self._attr_unique_id = f"{DOMAIN}_{self.key}_{kind}"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal(self.key), self.async_write_ha_state)
        )
