"""Tests for the native single-target room thermostats (climate platform).

The climate entities are facades: they READ the coordinator's plan/targets and
WRITE by driving the integration's own number/switch entities. They must never
write the real heads directly (only the coordinator does, via recompute).

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
    async_mock_service,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (  # noqa: E402
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_PRIMARY_VANE_VERTICAL,
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


async def _setup(
    hass: HomeAssistant, **extra_data: Any
) -> tuple[MockConfigEntry, str, str]:
    """Stand up the heads, sensors, and an mxz config entry."""
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
            **extra_data,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, head_a, head_b


async def _enable(
    hass: HomeAssistant, entry: MockConfigEntry, *suffixes: str
) -> None:
    for suffix in suffixes:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()


async def test_thermostats_created(hass: HomeAssistant) -> None:
    """Two single-target thermostats exist with HomeKit-clean feature flags."""
    entry, _, _ = await _setup(hass)

    for suffix in ("_primary_thermostat", "_secondary_thermostat"):
        state = hass.states.get(_eid(hass, entry, suffix))
        assert state is not None
        assert set(state.attributes["hvac_modes"]) == {"off", "heat_cool"}
        feats = state.attributes["supported_features"]
        assert feats & ClimateEntityFeature.TARGET_TEMPERATURE
        assert feats & ClimateEntityFeature.TURN_ON
        assert feats & ClimateEntityFeature.TURN_OFF
        # The HomeKit single-tile guarantee: never a dual-threshold range.
        assert not (feats & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)
        # No vane configured -> no swing.
        assert not (feats & ClimateEntityFeature.SWING_MODE)


async def test_set_temperature_propagates(hass: HomeAssistant) -> None:
    """Setting the tile temperature drives the number entity and reaches the head."""
    entry, head_a, _ = await _setup(hass)
    await _enable(hass, entry, "_primary_enable", "_secondary_enable", "_coordinator_enable")
    await _set_temp(hass, SENSOR_A, 75)  # primary hot -> will want cool
    await _set_temp(hass, SENSOR_B, 70)

    prim = _eid(hass, entry, "_primary_thermostat")
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": prim, "temperature": 72},
        blocking=True,
    )
    await hass.async_block_till_done()

    # The number entity (single source of truth) and coordinator both updated.
    assert float(hass.states.get(_eid(hass, entry, "_primary_target")).state) == 72.0
    assert entry.runtime_data.primary_target == 72

    # And a recompute reaches the head with the computed cool band (high=target).
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    assert a.state == "cool"
    assert a.attributes["target_temp_high"] == 72
    assert a.attributes["target_temp_low"] == 70


async def test_turn_off_disables_room(hass: HomeAssistant) -> None:
    """Turning the tile off flips the room enable switch and stops the head."""
    entry, head_a, _ = await _setup(hass)
    await _enable(hass, entry, "_primary_enable", "_secondary_enable", "_coordinator_enable")
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"  # running before

    prim = _eid(hass, entry, "_primary_thermostat")
    await hass.services.async_call(
        "climate", "turn_off", {"entity_id": prim}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "off"
    assert entry.runtime_data.primary_enable is False

    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"  # disabled room -> head off

    # And back on.
    await hass.services.async_call(
        "climate", "turn_on", {"entity_id": prim}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "on"
    assert entry.runtime_data.primary_enable is True


async def test_hvac_mode_and_action(hass: HomeAssistant) -> None:
    """hvac_mode follows the enable; hvac_action follows the plan engage."""
    entry, _, _ = await _setup(hass)
    await _enable(hass, entry, "_primary_enable", "_secondary_enable", "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")

    # Hot room -> enabled (heat_cool) and actively cooling.
    await _set_temp(hass, SENSOR_A, 75)
    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)
    st = hass.states.get(prim)
    assert st.state == "heat_cool"
    assert st.attributes["hvac_action"] == "cooling"

    # Satisfied -> idle.
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(prim).attributes["hvac_action"] == "idle"

    # Kill-switch off -> action off (heads no longer driven), still enabled.
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": _eid(hass, entry, "_coordinator_enable")}, blocking=True,
    )
    await _recompute(hass, entry)
    assert hass.states.get(prim).attributes["hvac_action"] == "off"
    assert hass.states.get(prim).state == "heat_cool"


async def test_facade_never_writes_head_directly(hass: HomeAssistant) -> None:
    """With the kill-switch OFF, a tile write must not touch the head."""
    entry, head_a, _ = await _setup(hass)
    # Room enabled but coordinator kill-switch OFF (the default).
    await _enable(hass, entry, "_primary_enable", "_secondary_enable")
    await _set_temp(hass, SENSOR_A, 80)  # would normally want cool

    prim = _eid(hass, entry, "_primary_thermostat")
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": prim, "temperature": 70},
        blocking=True,
    )
    await _recompute(hass, entry)

    # Number updated, but the head stayed at its initial OFF (only the
    # coordinator writes heads, and it's gated by the kill-switch).
    assert float(hass.states.get(_eid(hass, entry, "_primary_target")).state) == 70.0
    assert hass.states.get(head_a).state == "off"


async def test_auto_flip_updates_thermostat(hass: HomeAssistant) -> None:
    """A coordinator mode flip is reflected on the tile (CoordinatorEntity sync)."""
    entry, _, _ = await _setup(hass)
    await _enable(hass, entry, "_primary_enable", "_secondary_enable", "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")

    # Cold primary -> wants heat; first flip from the cool resting mode is allowed.
    await _set_temp(hass, SENSOR_A, 60)
    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)

    assert hass.states.get(_eid(hass, entry, "_plan")).state == "heat"
    assert hass.states.get(prim).attributes["hvac_action"] == "heating"


async def test_vane_passthrough(hass: HomeAssistant) -> None:
    """A configured vane select is mirrored as swing mode and written through."""
    vane = "select.bedroom_vane"
    hass.states.async_set(vane, "swing", {"options": ["auto", "1", "2", "swing"]})

    entry, _, _ = await _setup(hass, **{CONF_PRIMARY_VANE_VERTICAL: vane})
    prim = _eid(hass, entry, "_primary_thermostat")

    st = hass.states.get(prim)
    assert st.attributes["supported_features"] & ClimateEntityFeature.SWING_MODE
    assert st.attributes["swing_modes"] == ["auto", "1", "2", "swing"]
    assert st.attributes["swing_mode"] == "swing"

    calls = async_mock_service(hass, "select", "select_option")
    await hass.services.async_call(
        "climate", "set_swing_mode", {"entity_id": prim, "swing_mode": "auto"},
        blocking=True,
    )
    assert len(calls) == 1
    assert calls[0].data["entity_id"] == vane
    assert calls[0].data["option"] == "auto"

    # Secondary has no vane -> no swing feature.
    sec = hass.states.get(_eid(hass, entry, "_secondary_thermostat"))
    assert not (sec.attributes["supported_features"] & ClimateEntityFeature.SWING_MODE)
