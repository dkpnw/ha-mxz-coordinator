"""End-to-end actuator test: enable the coordinator and watch it drive real
ClimateEntity heads (decide -> act). Uses a mock climate platform so the full
service path (climate.set_temperature / climate.set_hvac_mode) actually runs.

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
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)

SENSOR_A = "sensor.room_a_temp"
SENSOR_B = "sensor.room_b_temp"


class MockHead(ClimateEntity):
    """A minimal dual-setpoint head that records what it's told."""

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


async def _setup_mock_heads(hass: HomeAssistant) -> tuple[str, str]:
    """Register two mock climate heads and return their entity_ids."""
    heads = [MockHead("a"), MockHead("b")]

    async def _async_setup_platform(
        hass, config, async_add_entities, discovery_info=None  # noqa: ANN001
    ):
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(
        hass, "test.climate", MockPlatform(async_setup_platform=_async_setup_platform)
    )
    assert await async_setup_component(
        hass, "climate", {"climate": {"platform": "test"}}
    )
    await hass.async_block_till_done()
    return heads[0].entity_id, heads[1].entity_id


def _eid(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str:
    """Resolve an mxz entity_id by its unique_id suffix."""
    reg = er.async_get(hass)
    for ent in reg.entities.values():
        if ent.config_entry_id == entry.entry_id and ent.unique_id.endswith(suffix):
            return ent.entity_id
    raise AssertionError(f"no mxz entity ending in {suffix}")


async def _set_temp(hass: HomeAssistant, entity_id: str, value: float) -> None:
    hass.states.async_set(entity_id, str(value))
    await hass.async_block_till_done()


async def _recompute(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Force a deterministic compute+apply (production path is debounced)."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_coordinator_drives_heads(hass: HomeAssistant) -> None:
    """Enable the coordinator and assert it actually commands the heads."""
    hass.config.units = US_CUSTOMARY_SYSTEM  # keep setpoints in °F

    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Enable both rooms and the coordinator kill-switch (defaults are OFF).
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    # --- Scenario 1: primary hot (75 vs target 70 -> wants COOL), secondary satisfied ---
    await _set_temp(hass, SENSOR_A, 75)
    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)

    a = hass.states.get(head_a)
    b = hass.states.get(head_b)
    print(f"\nS1 primary={a.state} low={a.attributes.get('target_temp_low')} "
          f"high={a.attributes.get('target_temp_high')} | secondary={b.state}")
    assert a.state == "cool"
    assert a.attributes["target_temp_high"] == 70  # high = target
    assert a.attributes["target_temp_low"] == 68  # low = target - 2
    assert b.state == "fan_only"  # satisfied -> idles, doesn't starve the other

    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.state == "cool"
    assert plan.attributes["primary_engage"] == "cool"
    assert plan.attributes["secondary_engage"] == "satisfied"

    # --- Scenario 2: standoff. primary wants COOL (75), secondary wants HEAT (60).
    # Primary wins -> shared cool; secondary (wrong direction) idles in fan_only. ---
    await _set_temp(hass, SENSOR_A, 75)
    await _set_temp(hass, SENSOR_B, 60)
    await _recompute(hass, entry)

    a = hass.states.get(head_a)
    b = hass.states.get(head_b)
    print(f"S2 (standoff) primary={a.state} | secondary={b.state} "
          f"(plan standoff={hass.states.get(_eid(hass, entry, '_plan')).attributes['standoff']})")
    assert a.state == "cool"
    assert b.state == "fan_only"

    # --- Scenario 3: eco-idle. Both within the 50–78 protection band -> both OFF. ---
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_eco_idle")}, blocking=True
    )
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)

    a = hass.states.get(head_a)
    b = hass.states.get(head_b)
    print(f"S3 (eco) primary={a.state} | secondary={b.state}")
    assert a.state == "off"
    assert b.state == "off"

    # --- Kill-switch: disable the coordinator, drift a head, confirm it does NOT fight ---
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": _eid(hass, entry, "_coordinator_enable")}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": _eid(hass, entry, "_eco_idle")}, blocking=True
    )
    await hass.async_block_till_done()
    # Force a head off-band; with the kill-switch OFF the coordinator must not touch it.
    await hass.services.async_call(
        "climate", "set_hvac_mode",
        {"entity_id": head_a, "hvac_mode": "heat_cool"}, blocking=True
    )
    await _set_temp(hass, SENSOR_A, 80)  # would normally trigger a cool command
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "heat_cool"  # untouched while disabled
    print("S4 (kill-switch off) primary stayed heat_cool -> coordinator did not write")
