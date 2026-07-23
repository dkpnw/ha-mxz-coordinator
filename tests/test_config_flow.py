"""Config and options flow tests (requires pytest-homeassistant-custom-component)."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM  # noqa: E402
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
    CONF_INHIBIT_ENTITY,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    CONF_ZONES,
    DOMAIN,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
    ZONE_STAGE_SENSOR,
    ZONE_VANE_VERTICAL,
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
    """Three-step flow: heads -> sensors -> tuning (defaults) -> zones entry."""
    hass.config.units = US_CUSTOMARY_SYSTEM  # °F defaults on the tuning step
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"heads": ["climate.primary", "climate.secondary", "climate.office"]},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sensors"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "sensor_1": "sensor.primary_temp",
            "sensor_2": "sensor.secondary_temp",
            "sensor_3": "sensor.office_temp",
        },
    )
    # Step 3: the full tuning form, pre-filled — submit as-is accepts defaults.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tuning"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    zones = result["data"][CONF_ZONES]
    assert [z[ZONE_CLIMATE] for z in zones] == [
        "climate.primary", "climate.secondary", "climate.office",
    ]
    assert zones[2][ZONE_SENSOR] == "sensor.office_temp"
    # defaults flowed into options (and mirrored into data)
    assert result["options"][CONF_DEMAND_THRESHOLD] == 3.0
    assert result["data"][CONF_DEMAND_THRESHOLD] == 3.0


async def test_setup_tuning_accepts_overrides(hass: HomeAssistant) -> None:
    """A value changed on the setup tuning step lands in the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": ["climate.primary", "climate.secondary"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sensor_1": "sensor.primary_temp", "sensor_2": "sensor.secondary_temp"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEMAND_THRESHOLD: 5.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_DEMAND_THRESHOLD] == 5.0


async def test_rejects_duplicate_heads(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"heads": ["climate.primary", "climate.primary"]},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "duplicate_heads"}


async def test_rejects_single_head(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": ["climate.primary"]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "need_two_heads"}


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


async def test_options_flow_zone_override_folds_into_zones(hass: HomeAssistant) -> None:
    """A per-zone override (here the airflow/stage sensor) folds into the zones
    list in entry.data — never persisted as a flat key, which would shadow the
    zones list in the coordinator's {**data, **options} merge."""
    zones = [
        {
            ZONE_NAME: "Primary",
            ZONE_CLIMATE: "climate.primary",
            ZONE_SENSOR: "sensor.primary_temp",
        },
        {
            ZONE_NAME: "Secondary",
            ZONE_CLIMATE: "climate.secondary",
            ZONE_SENSOR: "sensor.secondary_temp",
        },
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: zones},
        options={CONF_DEMAND_THRESHOLD: 3.0},
        title="MXZ Coordinator",
        version=2,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"primary_stage": "sensor.primary_stage"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Folded into the zones list, wired to the right zone only.
    assert entry.data[CONF_ZONES][0][ZONE_STAGE_SENSOR] == "sensor.primary_stage"
    assert ZONE_STAGE_SENSOR not in entry.data[CONF_ZONES][1]
    # Never stored flat (options or the data mirror).
    assert "primary_stage" not in entry.options
    assert "primary_stage" not in entry.data


async def test_options_flow_clears_inhibit_entity(hass: HomeAssistant) -> None:
    """A real submit with the standby-hold entity field cleared actually clears it.

    The field is always rendered and a pre-filled value is submitted back, so an
    absent key on a non-empty submit is a deliberate clear — the resilience merge
    must not resurrect the old entity (in options OR the data mirror). The
    coordinator then reads it as "no standby hold configured".
    """
    from custom_components.mxz_coordinator.coordinator import MXZCoordinator

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_VALID,
        options={CONF_INHIBIT_ENTITY: "binary_sensor.grid", CONF_DEMAND_THRESHOLD: 3.0},
        title="MXZ Coordinator",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEMAND_THRESHOLD: 5.0}  # inhibit field cleared
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INHIBIT_ENTITY] is None
    assert entry.data[CONF_INHIBIT_ENTITY] is None  # mirror cleared too
    assert result["data"][CONF_DEMAND_THRESHOLD] == 5.0  # merge still merges

    coordinator = MXZCoordinator(hass, entry)
    assert coordinator.inhibit_entity is None


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


async def test_options_flow_clears_auto_detected_zone_override(
    hass: HomeAssistant,
) -> None:
    """A per-zone override the user clears in Configure is removed from the zone.

    Every vane/stage field is rendered for every zone, so an absent key on
    submit means the user cleared it. Regression: an auto-detected vane could
    never be removed (the flow skipped absent keys, so it stuck forever) — e.g.
    a ducted air handler that has no vane still advertised a phantom one.
    """
    zones = [
        {
            ZONE_NAME: "Primary",
            ZONE_CLIMATE: "climate.primary",
            ZONE_SENSOR: "sensor.primary_temp",
            ZONE_VANE_VERTICAL: "select.primary_vane",  # auto-detected at setup
        },
        {
            ZONE_NAME: "Secondary",
            ZONE_CLIMATE: "climate.secondary",
            ZONE_SENSOR: "sensor.secondary_temp",
        },
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: zones},
        options={CONF_DEMAND_THRESHOLD: 3.0},
        title="MXZ Coordinator",
        version=2,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Submit without the primary vane field — i.e. the user cleared it.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEMAND_THRESHOLD: 3.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The auto-detected vane is gone from the zone (cleared, not stuck).
    assert entry.data[CONF_ZONES][0].get(ZONE_VANE_VERTICAL) is None


async def test_options_flow_preserves_untouched_zone_override(
    hass: HomeAssistant,
) -> None:
    """A pre-filled override left untouched is resubmitted with its value and kept.

    The frontend submits a suggested-value field the user does not clear, so
    unchanged wiring survives a save (only an explicit clear removes it).
    """
    zones = [
        {
            ZONE_NAME: "Primary",
            ZONE_CLIMATE: "climate.primary",
            ZONE_SENSOR: "sensor.primary_temp",
            ZONE_VANE_VERTICAL: "select.primary_vane",
        },
        {
            ZONE_NAME: "Secondary",
            ZONE_CLIMATE: "climate.secondary",
            ZONE_SENSOR: "sensor.secondary_temp",
        },
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: zones},
        options={CONF_DEMAND_THRESHOLD: 3.0},
        title="MXZ Coordinator",
        version=2,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEMAND_THRESHOLD: 3.0, "primary_vane_vertical": "select.primary_vane"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_ZONES][0][ZONE_VANE_VERTICAL] == "select.primary_vane"
