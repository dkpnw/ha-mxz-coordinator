"""Setup / coordinator behavior tests (requires pytest-homeassistant-custom-component)."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.mxz_coordinator.const import (  # noqa: E402
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
    hass.states.async_set("sensor.primary_temp", "72")
    hass.states.async_set("sensor.secondary_temp", "70")
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
    from homeassistant.helpers import device_registry as dr

    entry = await _setup(hass)
    device = dr.async_get(hass).async_get_device({(DOMAIN, entry.entry_id)})
    assert device is not None
    assert (
        device.configuration_url
        == "https://github.com/dkpnw/ha-mxz-coordinator#removing"
    )
