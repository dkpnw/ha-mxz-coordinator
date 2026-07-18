"""Config and options flow tests (requires pytest-homeassistant-custom-component)."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402

from custom_components.mxz_coordinator.config_flow import (  # noqa: E402
    MXZOptionsFlow,
    _detect_stage,
    _detect_vanes,
)
from custom_components.mxz_coordinator.const import (  # noqa: E402
    CONF_CHANGEOVER_ENTITY,
    CONF_DEMAND_THRESHOLD,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_PRIMARY_STAGE,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    CONF_SECONDARY_STAGE,
    DOMAIN,
)

_VALID = {
    CONF_PRIMARY_CLIMATE: "climate.primary",
    CONF_SECONDARY_CLIMATE: "climate.secondary",
    CONF_PRIMARY_SENSOR: "sensor.primary_temp",
    CONF_SECONDARY_SENSOR: "sensor.secondary_temp",
}


async def test_detect_vanes_from_head_device(hass: HomeAssistant) -> None:
    """The head's vertical/horizontal vane selects are inferred from its device."""
    src = MockConfigEntry(domain="test")
    src.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=src.entry_id, identifiers={("test", "head_a")}
    )
    ent_reg = er.async_get(hass)
    climate = ent_reg.async_get_or_create(
        "climate", "test", "head_a", device_id=device.id,
        suggested_object_id="head_a_heat_pump",
    )
    vv = ent_reg.async_get_or_create(
        "select", "test", "head_a_vv", device_id=device.id,
        suggested_object_id="head_a_vertical_vane",
    )
    hv = ent_reg.async_get_or_create(
        "select", "test", "head_a_hv", device_id=device.id,
        suggested_object_id="head_a_horizontal_vane",
    )
    # an unrelated select on the same device must be ignored
    ent_reg.async_get_or_create(
        "select", "test", "head_a_pre", device_id=device.id,
        suggested_object_id="head_a_preset",
    )

    found = _detect_vanes(hass, climate.entity_id)
    assert found["vertical"] == vv.entity_id
    assert found["horizontal"] == hv.entity_id


def test_detect_vanes_no_device_is_safe(hass: HomeAssistant) -> None:
    """A head with no registry/device entry just yields nothing (no crash)."""
    assert _detect_vanes(hass, "climate.not_registered") == {}


async def test_detect_stage_from_head_device(hass: HomeAssistant) -> None:
    """The head's airflow (`stage`) sensor is inferred from its own device."""
    src = MockConfigEntry(domain="test")
    src.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=src.entry_id, identifiers={("test", "head_s")}
    )
    ent_reg = er.async_get(hass)
    climate = ent_reg.async_get_or_create(
        "climate", "test", "head_s", device_id=device.id,
        suggested_object_id="head_s_heat_pump",
    )
    stage = ent_reg.async_get_or_create(
        "sensor", "test", "head_s_stage", device_id=device.id,
        suggested_object_id="head_s_stage",
    )
    # An unrelated sensor on the same device must be ignored.
    ent_reg.async_get_or_create(
        "sensor", "test", "head_s_comp", device_id=device.id,
        suggested_object_id="head_s_compressor_frequency",
    )

    assert _detect_stage(hass, climate.entity_id) == stage.entity_id


def test_detect_stage_absent_is_none(hass: HomeAssistant) -> None:
    """No device / no stage sensor -> None (no crash)."""
    assert _detect_stage(hass, "climate.not_registered") is None


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

    hass.config.units = US_CUSTOMARY_SYSTEM  # °F defaults on the tuning step
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], _VALID)
    # Final step: the full tuning form, pre-filled — Submit as-is accepts defaults.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tuning"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PRIMARY_CLIMATE] == "climate.primary"
    # defaults flowed into options (and the data mirror)
    assert result["options"][CONF_DEMAND_THRESHOLD] == 3.0
    assert result["data"][CONF_DEMAND_THRESHOLD] == 3.0


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


async def test_options_flow_stage_override_lands_on_the_right_head(
    hass: HomeAssistant,
) -> None:
    """A per-head override (here the airflow/stage sensor) lands in the RIGHT
    head's flat conf key, mirrored into entry.data, and never bleeds onto the
    other head — the flat analogue of the zones fold-in on main."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_VALID,
        options={CONF_DEMAND_THRESHOLD: 3.0},
        title="MXZ Coordinator",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PRIMARY_STAGE: "sensor.primary_stage"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Wired to the primary head's key only, mirrored into data for recovery.
    assert entry.data[CONF_PRIMARY_STAGE] == "sensor.primary_stage"
    assert result["data"][CONF_PRIMARY_STAGE] == "sensor.primary_stage"
    # The other head is untouched (no stale shadow on secondary).
    assert CONF_SECONDARY_STAGE not in entry.data
    assert CONF_SECONDARY_STAGE not in entry.options
    # The un-submitted tunable survives the merge.
    assert entry.data[CONF_DEMAND_THRESHOLD] == 3.0


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
