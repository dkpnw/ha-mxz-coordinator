"""One invented head per room, the shape of the suite's MockHead (tests/test_drive.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature

from . import DOMAIN
from .entity import InventedEntity
from .model import action

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import Bench
    from .devices import Head

# The model's word for what a head does, as Home Assistant shows it. Standby is
# idle to Home Assistant; the head's `standby` attribute says which idle it is.
ACTIONS = {
    "off": HVACAction.OFF, "fan": HVACAction.FAN, "standby": HVACAction.IDLE,
    "heating": HVACAction.HEATING, "cooling": HVACAction.COOLING, "idle": HVACAction.IDLE,
}


async def async_setup_platform(
    hass: HomeAssistant, config: Any, add: AddEntitiesCallback, discovery_info: Any = None
) -> None:
    bench: Bench = hass.data[DOMAIN]
    add(InventedHead(bench, room) for room in bench.house["rooms"])


class InventedHead(InventedEntity, ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, bench: Bench, room: dict[str, Any]) -> None:
        super().__init__(bench, room, "head")
        spec = bench.house["head"]
        self._attr_hvac_modes = [HVACMode(mode) for mode in spec["hvac_modes"]]
        self._attr_fan_modes = list(spec["fan_modes"])
        self._attr_min_temp = spec["min_temp"]
        self._attr_max_temp = spec["max_temp"]

    @property
    def _head(self) -> Head:
        return self.bench.devices.heads[self.key]

    @property
    def available(self) -> bool:
        return self._head.available

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode(self._head.hvac_mode)

    @property
    def hvac_action(self) -> HVACAction | None:
        """Only while the demo model runs; the proof journeys' heads report no action."""
        return ACTIONS[self._doing] if self.bench.modelled else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return {"standby": self._doing == "standby"} if self.bench.modelled else None

    @property
    def _doing(self) -> str:
        return action(self._head, self.bench.devices.rooms[self.key], self.bench.devices.outdoor)

    @property
    def target_temperature_low(self) -> float | None:
        return self._head.target_temp_low

    @property
    def target_temperature_high(self) -> float | None:
        return self._head.target_temp_high

    @property
    def fan_mode(self) -> str:
        return self._head.fan_mode

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._head.hvac_mode = str(hvac_mode)
        await self.bench.changed(self.key)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._head.fan_mode = fan_mode
        await self.bench.changed(self.key)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._head.hvac_mode = str(mode)
        if (low := kwargs.get("target_temp_low")) is not None:
            self._head.target_temp_low = low
        if (high := kwargs.get("target_temp_high")) is not None:
            self._head.target_temp_high = high
        await self.bench.changed(self.key)
