"""Config and options flow tests (requires pytest-homeassistant-custom-component)."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.mxz_coordinator.config_flow import MXZOptionsFlow  # noqa: E402
from custom_components.mxz_coordinator.const import (  # noqa: E402
    CONF_CHANGEOVER_ENTITY,
    CONF_DEMAND_THRESHOLD,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)

_VALID = {
    CONF_PRIMARY_CLIMATE: "climate.primary",
    CONF_SECONDARY_CLIMATE: "climate.secondary",
    CONF_PRIMARY_SENSOR: "sensor.primary_temp",
    CONF_SECONDARY_SENSOR: "sensor.secondary_temp",
}


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], _VALID)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PRIMARY_CLIMATE] == "climate.primary"


async def test_rejects_same_head(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**_VALID, CONF_SECONDARY_CLIMATE: "climate.primary"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "same_head"}


async def test_options_flow_round_trips(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=_VALID, title="MXZ Coordinator")
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEMAND_THRESHOLD: 4.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEMAND_THRESHOLD] == 4.0


async def test_options_flow_merges_and_mirrors_to_data(hass: HomeAssistant) -> None:
    """A save merges onto existing options and mirrors the config into entry.data.

    Resilience: a field left out of the submit (here changeover_entity) is
    PRESERVED, and entry.data ends up holding the config so an out-of-band
    options wipe self-recovers (the coordinator reads {**data, **options}).
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_VALID,
        options={CONF_CHANGEOVER_ENTITY: "weather.home", CONF_DEMAND_THRESHOLD: 3.0},
        title="MXZ Coordinator",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEMAND_THRESHOLD: 5.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # merge: the un-submitted changeover_entity survives; the new value applies.
    assert result["data"][CONF_CHANGEOVER_ENTITY] == "weather.home"
    assert result["data"][CONF_DEMAND_THRESHOLD] == 5.0
    # mirror: entry.data now carries the config for out-of-band recovery.
    assert entry.data[CONF_CHANGEOVER_ENTITY] == "weather.home"
    assert entry.data[CONF_DEMAND_THRESHOLD] == 5.0


async def test_options_flow_refuses_empty(hass: HomeAssistant) -> None:
    """An empty submit on an empty-options entry aborts instead of persisting {}."""
    entry = MockConfigEntry(domain=DOMAIN, data=_VALID, title="MXZ Coordinator")
    entry.add_to_hass(hass)
    flow = MXZOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_init(user_input={})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "empty_options"


async def test_coordinator_recovers_config_from_data_mirror(hass: HomeAssistant) -> None:
    """With options wiped to {}, the coordinator reads its config from the data mirror."""
    from custom_components.mxz_coordinator.coordinator import MXZCoordinator

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**_VALID, CONF_CHANGEOVER_ENTITY: "weather.home", CONF_DEMAND_THRESHOLD: 5.0},
        options={},  # wiped out-of-band
        title="MXZ Coordinator",
    )
    entry.add_to_hass(hass)
    coordinator = MXZCoordinator(hass, entry)
    assert coordinator.changeover_entity == "weather.home"
    assert coordinator.demand_threshold == 5.0
