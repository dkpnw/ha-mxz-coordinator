"""End-to-end tests for the local-weather seasonal changeover.

Simulates seasons against a temperature sensor AND a real (mock) weather entity's
daily forecast, asserting the integration auto-drives the heat/cool lockout switches
and that the resulting room decision follows. Proves the decision comes from local
weather, not the calendar.

Requires pytest-homeassistant-custom-component (Python 3.12+).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import (  # noqa: E402
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.weather import (  # noqa: E402
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.const import UnitOfTemperature  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (  # noqa: E402
    CONF_CHANGEOVER_ENTITY,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)

SENSOR_A = "sensor.room_a_temp"
SENSOR_B = "sensor.room_b_temp"


class MockHead(ClimateEntity):
    """Minimal dual-setpoint head."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, suffix: str) -> None:
        self._attr_unique_id = f"mock_head_{suffix}"
        self._attr_name = f"Mock Head {suffix}"
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature_low = None
        self._attr_target_temperature_high = None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = mode
        if (low := kwargs.get("target_temp_low")) is not None:
            self._attr_target_temperature_low = low
        if (high := kwargs.get("target_temp_high")) is not None:
            self._attr_target_temperature_high = high
        self.async_write_ha_state()


class MockWeather(WeatherEntity):
    """A weather entity with a settable daily-high forecast."""

    _attr_should_poll = False
    _attr_supported_features = WeatherEntityFeature.FORECAST_DAILY
    _attr_native_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_condition = "sunny"

    def __init__(self) -> None:
        self._attr_unique_id = "mock_weather"
        self._attr_name = "Mock Weather"
        self._high = 70.0
        self._attr_native_temperature = 70.0

    def set_high(self, value: float) -> None:
        self._high = value
        self._attr_native_temperature = value
        self.async_write_ha_state()

    async def async_forecast_daily(self) -> list[Forecast]:
        return [Forecast(datetime="2026-07-01T00:00:00+00:00", native_temperature=self._high)]


async def _setup_mock_heads(hass: HomeAssistant) -> tuple[str, str]:
    heads = [MockHead("a"), MockHead("b")]

    async def _setup_platform(hass, config, async_add_entities, discovery_info=None):  # noqa: ANN001
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(
        hass, "test.climate", MockPlatform(async_setup_platform=_setup_platform)
    )
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    await hass.async_block_till_done()
    return heads[0].entity_id, heads[1].entity_id


async def _setup_mock_weather(hass: HomeAssistant) -> MockWeather:
    weather = MockWeather()

    async def _setup_platform(hass, config, async_add_entities, discovery_info=None):  # noqa: ANN001
        async_add_entities([weather])

    mock_platform(
        hass, "test.weather", MockPlatform(async_setup_platform=_setup_platform)
    )
    assert await async_setup_component(hass, "weather", {"weather": {"platform": "test"}})
    await hass.async_block_till_done()
    return weather


def _eid(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str:
    reg = er.async_get(hass)
    for ent in reg.entities.values():
        if ent.config_entry_id == entry.entry_id and ent.unique_id.endswith(suffix):
            return ent.entity_id
    raise AssertionError(f"no mxz entity ending in {suffix}")


def _lockouts(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[str, str]:
    return (
        hass.states.get(_eid(hass, entry, "_heat_lockout")).state,
        hass.states.get(_eid(hass, entry, "_cool_lockout")).state,
    )


async def _mxz_entry(hass: HomeAssistant, head_a, head_b, changeover) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_CHANGEOVER_ENTITY: changeover,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_changeover_from_temperature_sensor(hass: HomeAssistant) -> None:
    """A plain outdoor-temp sensor drives the lockouts across a season sweep."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    hass.states.async_set(SENSOR_A, "70")
    hass.states.async_set(SENSOR_B, "70")
    hass.states.async_set("sensor.outdoor_high", "72")  # start warm (defaults 68/50)
    await hass.async_block_till_done()

    entry = await _mxz_entry(hass, head_a, head_b, "sensor.outdoor_high")

    # Initial eval: 72 >= 68 -> warm -> heat locked, cool free.
    assert _lockouts(hass, entry) == ("on", "off")

    # Walk the seasons; each sensor change re-evaluates.
    for high, expected in [
        ("40", ("off", "on")),    # deep winter -> cool locked
        ("60", ("off", "off")),   # shoulder -> both free
        ("92", ("on", "off")),    # midsummer -> heat locked
        ("50", ("off", "on")),    # threshold: <= 50 -> cool locked
        ("68", ("on", "off")),    # threshold: >= 68 -> heat locked
    ]:
        hass.states.async_set("sensor.outdoor_high", high)
        await hass.async_block_till_done()
        assert _lockouts(hass, entry) == expected, f"outdoor_high={high}"

    # Weather goes unavailable -> no lockout (safe fallback).
    hass.states.async_set("sensor.outdoor_high", "unavailable")
    await hass.async_block_till_done()
    assert _lockouts(hass, entry) == ("off", "off")


async def test_changeover_from_weather_forecast(hass: HomeAssistant) -> None:
    """A real weather entity's daily forecast high drives the lockouts."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    hass.states.async_set(SENSOR_A, "70")
    hass.states.async_set(SENSOR_B, "70")
    weather = await _setup_mock_weather(hass)
    weather.set_high(85)  # summer forecast
    await hass.async_block_till_done()

    entry = await _mxz_entry(hass, head_a, head_b, weather.entity_id)
    assert _lockouts(hass, entry) == ("on", "off")  # 85 -> heat locked

    weather.set_high(35)  # winter forecast; the weather state-change re-evaluates
    await hass.async_block_till_done()
    assert _lockouts(hass, entry) == ("off", "on")  # 35 -> cool locked

    weather.set_high(60)  # shoulder
    await hass.async_block_till_done()
    assert _lockouts(hass, entry) == ("off", "off")


async def test_changeover_end_to_end_head_behavior(hass: HomeAssistant) -> None:
    """Summer changeover -> a below-target room idles instead of heating."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    hass.states.async_set(SENSOR_A, "63")  # 7 below the default 70 target -> wants heat
    hass.states.async_set(SENSOR_B, "70")
    hass.states.async_set("sensor.outdoor_high", "80")  # summer
    await hass.async_block_till_done()

    entry = await _mxz_entry(hass, head_a, head_b, "sensor.outdoor_high")
    # Hysteresis arms at startup now (#6): age the clock so this test's flip is allowed.
    entry.runtime_data._last_mode_change_ts = 0.0

    # Enable both rooms + the coordinator.
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    # Summer -> heat_lockout on -> the cold room idles in fan_only, does NOT heat.
    assert _lockouts(hass, entry) == ("on", "off")
    assert hass.states.get(_eid(hass, entry, "_plan")).attributes["primary_demand"] == "neutral"
    assert hass.states.get(head_a).state == "fan_only"

    # Flip to winter -> heat unlocked -> the same cold room now heats.
    hass.states.async_set("sensor.outdoor_high", "40")
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _lockouts(hass, entry) == ("off", "on")
    assert hass.states.get(head_a).state == "heat"
