"""A real MXZ entry for the Hypothesis property tests, one fresh entry per example.

The properties in ``tests/test_properties_*.py`` state the rules of
docs/formal/README.md and drive the integration itself: real setup, compute,
apply, door/window pause, Fan auto and shared-mode entities, reload and restart.
Nothing of MXZ is reimplemented here. What is invented is the world around it:
the heads, the room sensors, the window, and the people.

``RigHead`` keeps what a test needs as its oracle. Every ``set_fan_mode`` or
``set_hvac_mode`` it receives is MXZ's, because the invented people here never
call a service: they change the head directly, as its remote does.

All rooms, heads, sensors and values are invented.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any, ClassVar

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import restore_state
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from hypothesis import HealthCheck, settings
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_restore_state_shutdown_restart,
)

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_FAN_BOOST_MAX,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_OPENING_ENTITY,
    ZONE_OPENING_PAUSE,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from tests.cloud_heads import register
from tests.head_entity import HeadEntity
from tests.test_drive import _eid, _set_target, _set_temp

# Seeded from each test's name, so a CI run can't fail on a draw an earlier run
# didn't make. For a wider search: MXZ_PROPERTY_EXAMPLES=500 MXZ_PROPERTY_RANDOM=1.
EXAMPLES = int(os.environ.get("MXZ_PROPERTY_EXAMPLES", "0"))
_COMMON: dict[str, Any] = {
    "deadline": None,
    "database": None,
    "derandomize": not os.environ.get("MXZ_PROPERTY_RANDOM"),
    # One hass per test function, with a new entry for each example.
    "suppress_health_check": [HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
}
# Each example of these sets up, drives and removes a whole entry.
SEQUENCES = settings(max_examples=EXAMPLES or 25, **_COMMON)
# These call one function, so they can afford many more.
PURE = settings(max_examples=EXAMPLES or 300, **_COMMON)

SENSORS = ("sensor.property_a_temp", "sensor.property_b_temp")
WINDOW = "binary_sensor.property_window"
TARGET = 70.0
HOLD = 600
OPEN_DELAY = 60
CLOSE_DELAY = 120
_HEADS = "property_rig_heads"


class RigHead(HeadEntity):
    """A dual-setpoint head that logs MXZ's writes and can lag behind them."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_native_temperature_unit = "°F"
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [
        HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY,
    ]
    _attr_fan_modes: ClassVar[list[str]] = ["auto", "quiet", "low", "medium", "middle", "high"]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, suffix: str) -> None:
        self._attr_unique_id = f"property_head_{suffix}"
        self._attr_name = f"Property Head {suffix}"
        self._attr_native_target_temperature_low = None
        self._attr_native_target_temperature_high = None
        self._clear()

    def _clear(self) -> None:
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_fan_mode = "auto"
        self.fan_writes: list[str] = []
        self.mode_writes: list[str] = []
        self.lags = False
        self.unreported_fan: str | None = None

    def reset(self) -> None:
        """A new example's head: off, fan auto, nothing written yet."""
        self._clear()
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self.mode_writes.append(hvac_mode)
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self.fan_writes.append(fan_mode)
        if self.lags:
            self.unreported_fan = fan_mode
            return
        self._attr_fan_mode = fan_mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (mode := kwargs.get("hvac_mode")) is not None:
            if mode != self._attr_hvac_mode:
                self.mode_writes.append(mode)
            self._attr_hvac_mode = mode
        if (low := kwargs.get("target_temp_low")) is not None:
            self._attr_native_target_temperature_low = low
        if (high := kwargs.get("target_temp_high")) is not None:
            self._attr_native_target_temperature_high = high
        self.async_write_ha_state()

    def report_fan(self) -> None:
        """A lagging head finally reports the fan MXZ last sent it."""
        if self.unreported_fan is not None:
            self._attr_fan_mode = self.unreported_fan
            self.unreported_fan = None
            self.async_write_ha_state()

    def person_fan(self, token: str) -> None:
        """A person picks a speed on the remote; it is the last thing the head got."""
        self.unreported_fan = None
        self._attr_fan_mode = token
        self.async_write_ha_state()

    def person_mode(self, mode: str) -> None:
        self._attr_hvac_mode = HVACMode(mode)
        self.async_write_ha_state()

    @property
    def fan(self) -> str:
        return self._attr_fan_mode

    @property
    def mode(self) -> str:
        return str(self._attr_hvac_mode)


class Rig:
    """One entry over two rooms, driven by steps. ``settle`` is one full compute and apply."""

    def __init__(self, hass: HomeAssistant, freezer: Any, entry: MockConfigEntry, heads: list[RigHead]) -> None:
        self.hass = hass
        self.freezer = freezer
        self.entry = entry
        self.heads = heads

    @property
    def coord(self) -> MXZCoordinator:
        return self.entry.runtime_data

    def now(self) -> float:
        return dt_util.utcnow().timestamp()

    def eid(self, suffix: str) -> str:
        return _eid(self.hass, self.entry, suffix)

    def row(self, room: int = 0) -> dict[str, Any]:
        return self.hass.states.get(self.eid("_plan")).attributes["zones"][room]

    def fan_held(self, room: int = 0) -> bool:
        return self.row(room)["fan_hold"]

    def fan_auto_switch(self, room: int = 0) -> str:
        slug = ("primary", "secondary")[room]
        return self.hass.states.get(self.eid(f"_{slug}_fan_auto")).state

    async def settle(self) -> None:
        await self.coord.async_refresh()
        await self.hass.async_block_till_done()

    async def later(self, seconds: float) -> None:
        """Time passes; wakeups that fall due fire on their own, then one compute."""
        self.freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(self.hass, dt_util.utcnow())
        await self.hass.async_block_till_done()
        await self.settle()

    async def temp(self, room: int, value: float) -> None:
        await _set_temp(self.hass, SENSORS[room], value)
        await self.settle()

    async def person_fan(self, token: str, room: int = 0) -> None:
        self.heads[room].person_fan(token)
        await self.hass.async_block_till_done()
        await self.settle()

    async def person_mode(self, mode: str, room: int = 0) -> None:
        self.heads[room].person_mode(mode)
        await self.hass.async_block_till_done()
        await self.settle()

    async def report_fan(self, room: int = 0) -> None:
        self.heads[room].report_fan()
        await self.hass.async_block_till_done()
        await self.settle()

    async def switch(self, suffix: str, on: bool) -> None:
        await self.hass.services.async_call(
            "switch", "turn_on" if on else "turn_off", {"entity_id": self.eid(suffix)}, blocking=True,
        )
        await self.hass.async_block_till_done()
        await self.settle()

    async def select_mode(self, mode: str) -> None:
        await self.hass.services.async_call(
            "select", "select_option", {"entity_id": self.eid("_shared_mode"), "option": mode}, blocking=True,
        )
        await self.hass.async_block_till_done()
        await self.settle()

    async def window(self, state: str) -> None:
        self.hass.states.async_set(WINDOW, state, {"friendly_name": "Property window"})
        await self.hass.async_block_till_done()
        await self.settle()

    async def reload(self) -> None:
        assert await self.hass.config_entries.async_reload(self.entry.entry_id)
        await self.hass.async_block_till_done()
        await self.settle()

    async def restart(self) -> None:
        """A clean stop and start: restore data written at the stop, every store read anew.

        The final write stands in for Home Assistant's own stop. The clock is
        frozen, so a store write delayed by 0 s has not run on its own yet.
        """
        hass, entry = self.hass, self.entry
        hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
        await hass.async_block_till_done()
        await async_mock_restore_state_shutdown_restart(hass)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        hass.data.pop(f"{DOMAIN}_mode_change_stores", None)
        hass.data.pop(f"{DOMAIN}_openings_stores", None)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await self.settle()

    async def close(self) -> None:
        """Remove the entry, its stores and its timers; the next example starts clean."""
        assert await self.hass.config_entries.async_remove(self.entry.entry_id)
        await self.hass.async_block_till_done()


async def open_rig(
    hass: HomeAssistant,
    freezer: Any,
    *,
    temps: tuple[float, float] = (TARGET, TARGET),
    window: str | None = None,
    fan_max: str | None = None,
    lags: bool = False,
) -> Rig:
    """A fresh entry with both rooms and the coordinator on, computed once.

    ``window`` is the first room's door/window reading, and turns its pause on.
    """
    # A restored state counts only if newer than the entry (#7): this example's
    # entities must not inherit the last example's states under the same ids.
    freezer.tick(timedelta(seconds=1))
    restore_state.async_get(hass).last_states.clear()
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads: list[RigHead] | None = hass.data.get(_HEADS)
    if heads is None:
        heads = [RigHead("a"), RigHead("b")]
        await register(hass, {"esphome": heads})
        hass.data[_HEADS] = heads
    for head in heads:
        head.reset()
    heads[0].lags = lags
    for sensor, value in zip(SENSORS, temps, strict=True):
        await _set_temp(hass, sensor, value)
    first = {ZONE_NAME: "Study", ZONE_CLIMATE: heads[0].entity_id, ZONE_SENSOR: SENSORS[0]}
    if window is not None:
        hass.states.async_set(WINDOW, window, {"friendly_name": "Property window"})
        first |= {ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_PAUSE: True}
    data: dict[str, Any] = {
        CONF_ZONES: [
            first,
            {ZONE_NAME: "Den", ZONE_CLIMATE: heads[1].entity_id, ZONE_SENSOR: SENSORS[1]},
        ],
        CONF_FAN_BOOST_ENABLE: True,
        CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
        CONF_MODE_HYSTERESIS: HOLD,
    }
    if fan_max is not None:
        data[CONF_FAN_BOOST_MAX] = fan_max
    entry = MockConfigEntry(domain=DOMAIN, title="MXZ Coordinator", data=data, version=2, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for slug in ("primary", "secondary"):
        await _set_target(hass, _eid(hass, entry, f"_{slug}_target"), TARGET)
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    rig = Rig(hass, freezer, entry, heads)
    await rig.settle()
    return rig
