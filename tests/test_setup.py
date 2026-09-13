"""Setup / coordinator behavior tests (requires pytest-homeassistant-custom-component)."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import (
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)

_DATA = {
    CONF_PRIMARY_CLIMATE: "climate.primary",
    CONF_SECONDARY_CLIMATE: "climate.secondary",
    CONF_PRIMARY_SENSOR: "sensor.primary_temp",
    CONF_SECONDARY_SENSOR: "sensor.secondary_temp",
}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    temp_attrs = {
        ATTR_UNIT_OF_MEASUREMENT: hass.config.units.temperature_unit,
    }
    hass.states.async_set("sensor.primary_temp", "72", temp_attrs)
    hass.states.async_set("sensor.secondary_temp", "70", temp_attrs)
    attrs = {"target_temp_low": 0, "target_temp_high": 0}
    hass.states.async_set("climate.primary", "off", attrs)
    hass.states.async_set("climate.secondary", "off", attrs)
    entry = MockConfigEntry(domain=DOMAIN, data=_DATA, title="MXZ Coordinator")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_created_and_plan_computes(hass: HomeAssistant) -> None:
    await _setup(hass)

    assert hass.states.async_entity_ids_count("switch") >= 4
    assert hass.states.async_entity_ids_count("number") >= 2
    assert hass.states.async_entity_ids_count("select") >= 1

    plan = next(
        s
        for s in hass.states.async_all("sensor")
        if s.attributes.get("standoff") is not None
    )
    assert plan.state in ("cool", "heat")
    assert "primary_engage" in plan.attributes


async def test_kill_switch_blocks_writes(hass: HomeAssistant) -> None:
    """With the coordinator disabled (default), the heads are never commanded."""
    calls: list = []

    async def _record(call) -> None:
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", _record)
    hass.services.async_register("climate", "set_hvac_mode", _record)

    await _setup(hass)
    # coordinator_enable defaults OFF -> apply is a no-op
    assert calls == []


async def test_device_carries_docs_link(hass: HomeAssistant) -> None:
    """The service device links to the docs (incl. Removing) via its Visit button."""
    entry = await _setup(hass)
    # Look the device up through its config entry: the identifier-set lookup
    # (async_get_device) is deprecated and raises in observed HA 2026.9.0.
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    device = devices[0]
    assert (DOMAIN, entry.entry_id) in device.identifiers
    assert (
        device.configuration_url
        == "https://github.com/dkpnw/ha-mxz-coordinator#removing"
    )
