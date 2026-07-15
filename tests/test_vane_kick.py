"""Vane-kick tests: applying a vane change while a head is OFF briefly runs the
head in fan_only so the louvre physically moves, then hands it back to the plan."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.select import SelectEntity  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
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
    CONF_ZONES,
    DOMAIN,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
    ZONE_VANE_VERTICAL,
)

from .test_drive import MockHead, _eid, _set_temp  # noqa: E402

VANE_OPTIONS = ["AUTO", "↑↑", "↑", "—", "↓", "↓↓", "SWING"]


class MockVane(SelectEntity):
    """A vane select that records every option it is commanded to."""

    _attr_should_poll = False
    _attr_options = VANE_OPTIONS

    def __init__(self) -> None:
        self._attr_unique_id = "mock_vane_a"
        self._attr_name = "Mock Vane A"
        self._attr_current_option = "AUTO"
        self.history: list[str] = []

    async def async_select_option(self, option: str) -> None:
        self.history.append(option)
        self._attr_current_option = option
        self.async_write_ha_state()


async def _setup(hass: HomeAssistant):
    """Two mock heads + one vane select, zone 0 wired to the vane."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [MockHead("a"), MockHead("b")]
    vane = MockVane()

    async def _climate(hass, config, async_add_entities, discovery_info=None):  # noqa: ANN001
        async_add_entities(heads)

    async def _select(hass, config, async_add_entities, discovery_info=None):  # noqa: ANN001
        async_add_entities([vane])

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=_climate))
    mock_platform(hass, "test.select", MockPlatform(async_setup_platform=_select))
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    assert await async_setup_component(hass, "select", {"select": {"platform": "test"}})
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="MXZ Coordinator",
        data={
            CONF_ZONES: [
                {
                    ZONE_NAME: "A",
                    ZONE_CLIMATE: heads[0].entity_id,
                    ZONE_SENSOR: "sensor.room_a_temp",
                    ZONE_VANE_VERTICAL: vane.entity_id,
                },
                {
                    ZONE_NAME: "B",
                    ZONE_CLIMATE: heads[1].entity_id,
                    ZONE_SENSOR: "sensor.room_b_temp",
                },
            ]
        },
    )
    entry.add_to_hass(hass)
    await _set_temp(hass, "sensor.room_a_temp", 70)
    await _set_temp(hass, "sensor.room_b_temp", 70)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # no real sleeping in tests
    entry.runtime_data._vane_kick_spinup = 0
    entry.runtime_data._vane_kick_apply = 0
    return entry, heads, vane


async def _enable_all(hass, entry):
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()


async def test_vane_kick_wakes_off_head(hass: HomeAssistant) -> None:
    """Eco/away (head off) + a swing change -> fan_only kick, vane applied, back off."""
    entry, heads, vane = await _setup(hass)
    await _enable_all(hass, entry)
    # eco on -> both rooms within the extremes -> heads OFF
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_eco_idle")}, blocking=True
    )
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(heads[0].entity_id).state == "off"

    # change swing via the native tile
    await hass.services.async_call(
        "climate",
        "set_swing_mode",
        {"entity_id": _eid(hass, entry, "_primary_thermostat"), "swing_mode": "—"},
        blocking=True,
    )
    await hass.async_block_till_done()

    # the vane was commanded while the head was awake, and the head is off again
    assert vane.history == ["—"]
    assert vane.current_option == "—"
    assert hass.states.get(heads[0].entity_id).state == "off"


async def test_vane_applies_directly_on_running_head(hass: HomeAssistant) -> None:
    """A running head gets the select write live — no mode change."""
    entry, heads, vane = await _setup(hass)
    await _enable_all(hass, entry)
    # room A hot -> head A actively cooling
    await _set_temp(hass, "sensor.room_a_temp", 75)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(heads[0].entity_id).state == "cool"

    await hass.services.async_call(
        "climate",
        "set_swing_mode",
        {"entity_id": _eid(hass, entry, "_primary_thermostat"), "swing_mode": "SWING"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert vane.history == ["SWING"]
    assert hass.states.get(heads[0].entity_id).state == "cool"  # untouched


async def test_vane_kick_respects_kill_switch(hass: HomeAssistant) -> None:
    """Kill-switch off: the head is never touched; best-effort select write only."""
    entry, heads, vane = await _setup(hass)
    # zones enabled but the coordinator disabled; head stays wherever it is (off)
    for suffix in ("_primary_enable", "_secondary_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    assert hass.states.get(heads[0].entity_id).state == "off"

    await hass.services.async_call(
        "climate",
        "set_swing_mode",
        {"entity_id": _eid(hass, entry, "_primary_thermostat"), "swing_mode": "↓"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert vane.history == ["↓"]  # select still written (best effort)
    assert hass.states.get(heads[0].entity_id).state == "off"  # never woken
