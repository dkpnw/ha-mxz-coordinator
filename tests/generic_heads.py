"""Invented generic heads for the generic-head (beta) tests.

A generic head is any Home Assistant climate entity that offers heat and cool
and isn't a Mitsubishi head: a window AC, another brand's mini-split, a unit
bridged in over HomeKit. These fakes model only what MXZ has to cope with;
neither copies any integration's code.

* ``PlainHead``, feature-poor: heat, cool and off only. No fan modes, no
  target step, no current temperature, no hvac_action, no swing, no dry.
* ``RichHead``, feature-rich: off, heat, cool, dry and fan_only; fan modes
  auto/low/medium/high; swing; a 0.5 °C step in °C; current temperature and
  hvac_action.
* ``OddFanHead``: a RichHead whose fan names match none of MXZ's ladder.
* ``CoolOnlyHead`` and ``HeatOnlyHead``: what MXZ must refuse.

Each is push-only: it reports a write at once, and otherwise reports nothing
at all while nothing changes (no poll), so a quiet head stays quiet for hours.
The fake's ``device`` is what the unit holds; a test reads it as the oracle
of what landed. ``received`` lists every call that reached it.

They register on an invented platform, ``generic_ac``, so no real integration
is implied.
"""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.climate import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback

from tests.head_entity import HeadEntity

GENERIC = "generic_ac"


class _GenericFake(HeadEntity):
    """What every generic fake shares: a device, and a push report of it."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _enable_turn_on_off_backwards_compatibility = False
    _attr_native_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_min_temp = 60.0
    _attr_max_temp = 86.0
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    has_fan = False

    def __init__(self, suffix: str, *, mode: str = HVACMode.OFF, temperature: float = 70.0) -> None:
        self._attr_unique_id = f"{type(self).__name__.lower()}_{suffix}"
        self._attr_name = f"{type(self).__name__} {suffix}"
        self.device: dict[str, Any] = {"mode": mode, "temperature": temperature}
        if self.has_fan:
            self.device["fan"] = "auto"
        self.received: list[tuple[str, dict[str, Any]]] = []
        self._show()

    def _show(self) -> None:
        self._attr_hvac_mode = self.device["mode"]
        self._attr_native_target_temperature = self.device["temperature"]
        if self.has_fan:
            self._attr_fan_mode = self.device["fan"]

    @callback
    def push(self) -> None:
        """The unit reports what it holds (a person changed something on it)."""
        self._show()
        self.async_write_ha_state()

    @callback
    def go_unavailable(self, unavailable: bool = True) -> None:
        self._attr_available = not unavailable
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self.received.append(("set_hvac_mode", {"hvac_mode": hvac_mode}))
        self.device["mode"] = hvac_mode
        self.push()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        self.received.append(("set_temperature", dict(kwargs)))
        if (temperature := kwargs.get("temperature")) is not None:
            self.device["temperature"] = temperature
        self.push()


class PlainHead(_GenericFake):
    """Feature-poor (see the module docstring)."""


class CoolOnlyHead(_GenericFake):
    """Most window ACs: cool and fan, no heat."""

    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.OFF, HVACMode.COOL, HVACMode.FAN_ONLY]


class HeatOnlyHead(_GenericFake):
    """A heater: heat and off, no cool."""

    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.OFF, HVACMode.HEAT]


class NoIdleHead(_GenericFake):
    """Heat and cool, but neither off nor fan_only to idle in."""

    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.HEAT, HVACMode.COOL]


class RichHead(_GenericFake):
    """Feature-rich (see the module docstring)."""

    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 16.0
    _attr_max_temp = 30.0
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [
        HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.DRY, HVACMode.FAN_ONLY,
    ]
    _attr_fan_modes: ClassVar[list[str]] = ["auto", "low", "medium", "high"]
    _attr_swing_modes: ClassVar[list[str]] = ["off", "vertical"]
    _attr_swing_mode = "off"
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    has_fan = True

    def __init__(self, suffix: str, *, mode: str = HVACMode.OFF, temperature: float = 21.0) -> None:
        super().__init__(suffix, mode=mode, temperature=temperature)
        self._attr_native_current_temperature = 21.0

    def _show(self) -> None:
        super()._show()
        self._attr_hvac_action = {
            HVACMode.HEAT: HVACAction.HEATING,
            HVACMode.COOL: HVACAction.COOLING,
            HVACMode.DRY: HVACAction.DRYING,
            HVACMode.FAN_ONLY: HVACAction.FAN,
        }.get(self.device["mode"], HVACAction.OFF)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self.received.append(("set_fan_mode", {"fan_mode": fan_mode}))
        self.device["fan"] = fan_mode
        self.push()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        self.received.append(("set_swing_mode", {"swing_mode": swing_mode}))
        self._attr_swing_mode = swing_mode
        self.push()


class OddFanHead(RichHead):
    """A RichHead whose fan names match none of MXZ's ladder but auto."""

    _attr_fan_modes: ClassVar[list[str]] = ["auto", "silent", "turbo"]
