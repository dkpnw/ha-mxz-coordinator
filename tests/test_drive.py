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
from homeassistant.util.unit_system import (  # noqa: E402
    METRIC_SYSTEM,
    US_CUSTOMARY_SYSTEM,
)
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (  # noqa: E402
    CONF_FAN_BOOST_ENABLE,
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
    # RAW/unsorted order, as reported by the real head (includes "middle" and "high").
    _attr_fan_modes = ["auto", "low", "medium", "middle", "high", "quiet"]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
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
        self._attr_fan_mode = "auto"

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._attr_fan_mode = fan_mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = mode
        if (low := kwargs.get("target_temp_low")) is not None:
            self._attr_target_temperature_low = low
        if (high := kwargs.get("target_temp_high")) is not None:
            self._attr_target_temperature_high = high
        self.async_write_ha_state()


class MockHeadC(MockHead):
    """A metric head: reports/accepts °C (matches a Celsius HA system)."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS


async def _setup_mock_heads(
    hass: HomeAssistant, *, cls: type[MockHead] = MockHead
) -> tuple[str, str]:
    """Register two mock climate heads and return their entity_ids."""
    heads = [cls("a"), cls("b")]

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


async def test_heat_lockout_suppresses_then_floors(hass: HomeAssistant) -> None:
    """heat_lockout: a below-target room idles (fan_only) unless below the safety floor."""
    hass.config.units = US_CUSTOMARY_SYSTEM

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

    for suffix in (
        "_primary_enable", "_secondary_enable", "_coordinator_enable", "_heat_lockout",
    ):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    # Primary 64 (well below its 70 target -> would HEAT) but above the 58 floor.
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    assert hass.states.get(_eid(hass, entry, "_plan")).attributes["primary_demand"] == "neutral"
    assert hass.states.get(head_a).state == "fan_only"  # locked out -> idles, no heat

    # Drop below the safety floor -> heat kicks in regardless of the lockout.
    await _set_temp(hass, SENSOR_A, 57)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "heat"
    print("HEAT-LOCKOUT: 64F idled fan_only, 57F (< 58 floor) heated")


async def test_cool_lockout_suppresses_then_ceilings(hass: HomeAssistant) -> None:
    """cool_lockout: an above-target room idles unless above the safety ceiling."""
    hass.config.units = US_CUSTOMARY_SYSTEM

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

    for suffix in (
        "_primary_enable", "_secondary_enable", "_coordinator_enable", "_cool_lockout",
    ):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    # Primary 75 (above its 70 target -> would COOL) but below the 80 ceiling -> idle.
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(_eid(hass, entry, "_plan")).attributes["primary_demand"] == "neutral"
    assert hass.states.get(head_a).state == "fan_only"  # locked out -> idles, no cool

    # Above the safety ceiling -> cool kicks in regardless of the lockout.
    await _set_temp(hass, SENSOR_A, 82)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    print("COOL-LOCKOUT: 75F idled fan_only, 82F (> 80 ceiling) cooled")


async def _set_target(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )
    await hass.async_block_till_done()


async def test_fan_boost_drives_speed(hass: HomeAssistant) -> None:
    """fan boost: a conditioning head's fan tracks how far the room is off-target."""
    hass.config.units = US_CUSTOMARY_SYSTEM

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
            CONF_FAN_BOOST_ENABLE: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    # Primary target 62; secondary stays at its 70 default (satisfied).
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 62)

    # --- Big delta: room 67 vs target 62 (delta 5, cooling) -> MAX fan "high". ---
    await _set_temp(hass, SENSOR_A, 67)
    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    b = hass.states.get(head_b)
    print(f"FAN-BOOST big-delta: primary={a.state} fan={a.attributes.get('fan_mode')} "
          f"| secondary={b.state} fan={b.attributes.get('fan_mode')}")
    assert a.state == "cool"
    assert a.attributes["fan_mode"] == "high"
    # Satisfied/fan_only secondary -> firmware "auto".
    assert b.state == "fan_only"
    assert b.attributes["fan_mode"] == "auto"

    # --- Room closes to 63.4 (delta 1.4, still cooling past the 1F deadband) ->
    # the fan eases down the ladder toward "low" via the hysteresis walk 4->1. ---
    await _set_temp(hass, SENSOR_A, 63.4)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    print(f"FAN-BOOST small-delta: primary={a.state} fan={a.attributes.get('fan_mode')}")
    assert a.state == "cool"
    assert a.attributes["fan_mode"] == "low"  # hysteresis walk 4->1


async def test_fan_boost_disabled_leaves_fan_alone(hass: HomeAssistant) -> None:
    """With fan boost explicitly opted OUT, the coordinator never touches the fan."""
    hass.config.units = US_CUSTOMARY_SYSTEM

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
            CONF_FAN_BOOST_ENABLE: False,  # explicit opt-out (default is ON)
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # The default (no option saved) is ON; this entry opted out.
    assert entry.runtime_data.fan_boost_enable is False

    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    await _set_target(hass, _eid(hass, entry, "_primary_target"), 62)
    # Big delta that WOULD boost to "high" if the feature were on.
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    assert a.state == "cool"
    assert a.attributes["fan_mode"] == "auto"  # untouched -> stays at the default
    print("FAN-BOOST opted out: fan stayed 'auto' despite a 5F delta")


async def test_fan_boost_defaults_on(hass: HomeAssistant) -> None:
    """An entry with no fan_boost option gets the feature ON (v2.10.0 default)."""
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
    assert entry.runtime_data.fan_boost_enable is True


async def test_coordinator_drives_heads_metric(hass: HomeAssistant) -> None:
    """On a °C system the coordinator adopts metric defaults and drives °C setpoints."""
    hass.config.units = METRIC_SYSTEM

    head_a, head_b = await _setup_mock_heads(hass, cls=MockHeadC)
    await _set_temp(hass, SENSOR_A, 21)
    await _set_temp(hass, SENSOR_B, 21)

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

    # The coordinator resolved the metric profile, not the °F legacy defaults.
    coord = entry.runtime_data
    assert coord.celsius is True
    assert coord.target_default == 21.0
    assert coord.target_step == 0.5
    assert coord.clamp_min == 15.0
    assert coord.clamp_max == 31.0
    assert coord.demand_threshold == 1.5
    # The number/climate entities report °C.
    tgt = hass.states.get(_eid(hass, entry, "_primary_target"))
    assert tgt.attributes["unit_of_measurement"] == UnitOfTemperature.CELSIUS

    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    # Primary 24 (hot vs the 21 default target -> COOL); band=1 -> (20, 21).
    await _set_temp(hass, SENSOR_A, 24)
    await _set_temp(hass, SENSOR_B, 21)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    b = hass.states.get(head_b)
    print(f"\nMETRIC S1 primary={a.state} low={a.attributes.get('target_temp_low')} "
          f"high={a.attributes.get('target_temp_high')} | secondary={b.state}")
    assert a.state == "cool"
    assert a.attributes["target_temp_high"] == 21.0  # high = target
    assert a.attributes["target_temp_low"] == 20.0  # low = target - 1 (metric band)
    assert b.state == "fan_only"  # satisfied idles, no starvation

    # 0.5° resolution: target 21.5, room 25 -> cool band -> (20.5, 21.5).
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 21.5)
    await _set_temp(hass, SENSOR_A, 25)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    print(f"METRIC S2 half-step low={a.attributes.get('target_temp_low')} "
          f"high={a.attributes.get('target_temp_high')}")
    assert a.attributes["target_temp_low"] == 20.5
    assert a.attributes["target_temp_high"] == 21.5
