"""Config and options flow tests (requires pytest-homeassistant-custom-component)."""

from __future__ import annotations

from copy import deepcopy

import pytest
import voluptuous as vol

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.config_flow import (
    MXZOptionsFlow,
    _detect_stage,
    _detect_vanes,
    _tunables_schema,
    _validate_tunables,
)
from custom_components.mxz_coordinator.const import (
    CONF_CHANGEOVER_COOL_BELOW,
    CONF_CHANGEOVER_ENTITY,
    CONF_CHANGEOVER_HEAT_ABOVE,
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    CONF_COIL_DRY_MINUTES,
    CONF_COOL_LOCKOUT_CEILING,
    CONF_DEMAND_THRESHOLD,
    CONF_ECO_COOL_MAX,
    CONF_ECO_HEAT_MIN,
    CONF_ENGAGE_DEADBAND,
    CONF_HEAT_LOCKOUT_FLOOR,
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
    ZONE_STAGE_SENSOR,
    ZONE_VANE_HORIZONTAL,
    ZONE_VANE_VERTICAL,
    unit_profile,
)

_VALID = {
    CONF_PRIMARY_CLIMATE: "climate.primary",
    CONF_SECONDARY_CLIMATE: "climate.secondary",
    CONF_PRIMARY_SENSOR: "sensor.primary_temp",
    CONF_SECONDARY_SENSOR: "sensor.secondary_temp",
}

_HEADS = (
    "climate.primary",
    "climate.secondary",
    "climate.office",
    "climate.a",
    "climate.b",
    "climate.c",
    "climate.d",
    "climate.e",
    *(f"climate.head_{index}" for index in range(9)),
)


def _set_capabilities(
    hass: HomeAssistant,
    entity_id: str,
    *,
    hvac_modes: list[str] | None = None,
    fan_modes: list[str] | None = None,
    fan_feature: bool = True,
) -> None:
    """Publish the capability attributes a real HA climate entity exposes."""
    attrs = {
        "hvac_modes": hvac_modes or ["off", "cool", "heat", "fan_only"],
        "supported_features": int(ClimateEntityFeature.FAN_MODE) if fan_feature else 0,
    }
    if fan_modes is not None:
        attrs["fan_modes"] = fan_modes
    elif fan_feature:
        attrs["fan_modes"] = ["auto", "quiet", "low", "medium", "middle", "high"]
    hass.states.async_set(entity_id, "off", attrs)


_ROOM_SENSORS = (
    "sensor.primary_temp",
    "sensor.secondary_temp",
    "sensor.office_temp",
    "sensor.a_temp",
    "sensor.b_temp",
    "sensor.c_temp",
    "sensor.a_new",
    "sensor.b_new",
    "sensor.c_new",
    "sensor.a_corrected",
    "sensor.b_corrected",
    "sensor.d_corrected",
    *(f"sensor.head_{index}_temp" for index in range(9)),
)


@pytest.fixture(autouse=True)
def _advertise_default_head_capabilities(hass: HomeAssistant) -> None:
    """Keep legacy flow cases focused on ownership while using real HA semantics."""
    for entity_id in _HEADS:
        _set_capabilities(hass, entity_id)
    # The sensors step reads each pick, so these cases need real states: a
    # picker in a real Home Assistant only ever offers entities that have one.
    for entity_id in _ROOM_SENSORS:
        hass.states.async_set(
            entity_id,
            "20.0",
            {"device_class": "temperature", "unit_of_measurement": "°C"},
        )


def _zones(*heads: str) -> list[dict[str, str]]:
    """Build the stored v2 zone shape for ownership-flow tests."""
    return [
        {
            ZONE_NAME: head.rsplit(".", 1)[-1].title(),
            ZONE_CLIMATE: head,
            ZONE_SENSOR: f"sensor.{head.rsplit('.', 1)[-1]}_temp",
        }
        for head in heads
    ]


def _entry_snapshot(entry: MockConfigEntry) -> tuple[dict, dict, str | None]:
    """Capture fields that a rejected flow must not mutate."""
    return deepcopy(dict(entry.data)), deepcopy(dict(entry.options)), entry.unique_id


def _form_heads(result: config_entries.ConfigFlowResult) -> list[str]:
    """Return the head picker's submitted/default selection."""
    return result["data_schema"]({})["heads"]


def _form_suggested(
    result: config_entries.ConfigFlowResult, field: str
) -> str | None:
    """Return a form field's suggested value."""
    for marker in result["data_schema"].schema:
        if marker.schema == field:
            return marker.description.get("suggested_value")
    raise AssertionError(f"missing schema field: {field}")


def _select_options(
    result: config_entries.ConfigFlowResult, field: str
) -> list[str]:
    """Return the options exposed by one SelectSelector field."""
    for marker, field_selector in result["data_schema"].schema.items():
        if marker.schema == field:
            return list(field_selector.config["options"])
    raise AssertionError(f"missing schema field: {field}")


def _form_has_field(result: config_entries.ConfigFlowResult, field: str) -> bool:
    """Whether one form schema displays a named field."""
    return any(marker.schema == field for marker in result["data_schema"].schema)


async def _start_user_flow(hass: HomeAssistant) -> config_entries.ConfigFlowResult:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _pass_rooms(
    hass: HomeAssistant, result: config_entries.ConfigFlowResult, step: str = "rooms"
) -> config_entries.ConfigFlowResult:
    """Accept the naming step's pre-filled names and move on."""
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == step, result
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})


async def _press(
    hass: HomeAssistant, result: config_entries.ConfigFlowResult, option: str
) -> config_entries.ConfigFlowResult:
    """Press one button on a menu step."""
    assert result["type"] is FlowResultType.MENU, result
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )


async def _start_reconfigure_flow(
    hass: HomeAssistant, entry: MockConfigEntry
) -> config_entries.ConfigFlowResult:
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )


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
    """Four-screen flow: heads -> rooms -> sensors -> review -> zones entry."""
    hass.config.units = US_CUSTOMARY_SYSTEM  # °F defaults on the tuning step
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"heads": ["climate.primary", "climate.secondary", "climate.office"]},
    )
    result = await _pass_rooms(hass, result)
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
    # Step 4: review, the only save point. Saving without opening advanced
    # accepts the same defaults the advanced form would have submitted.
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "review"
    result = await _press(hass, result, "finish")
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
    result = await _pass_rooms(hass, result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sensor_1": "sensor.primary_temp", "sensor_2": "sensor.secondary_temp"},
    )
    result = await _press(hass, result, "tuning")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEMAND_THRESHOLD: 5.0}
    )
    result = await _press(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_DEMAND_THRESHOLD] == 5.0


@pytest.mark.parametrize(
    "hvac_modes",
    [
        ["off", "heat", "fan_only"],
        ["off", "cool", "fan_only"],
    ],
)
async def test_setup_rejects_head_without_required_heat_and_cool(
    hass: HomeAssistant, hvac_modes: list[str]
) -> None:
    """A selected head must advertise both operating modes before setup advances."""
    _set_capabilities(hass, "climate.secondary", hvac_modes=hvac_modes)
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"heads": ["climate.primary", "climate.secondary"]},
    )

    assert result["step_id"] == "user"
    assert result["errors"] == {"heads": "head_missing_heat_cool"}
    assert result["description_placeholders"]["unsupported_heads"] == (
        "climate.secondary"
    )
    assert _form_heads(result) == ["climate.primary", "climate.secondary"]
    progress = next(
        item
        for item in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if item["flow_id"] == result["flow_id"]
    )
    assert "mxz_selected_heads" not in progress["context"]
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_setup_rejects_head_with_unavailable_capabilities(
    hass: HomeAssistant,
) -> None:
    """A missing state is unknown and produces a retryable, named error."""
    hass.states.async_remove("climate.secondary")
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"heads": ["climate.primary", "climate.secondary"]},
    )

    assert result["step_id"] == "user"
    assert result["errors"] == {"heads": "head_capabilities_unavailable"}
    assert result["description_placeholders"]["unsupported_heads"] == (
        "climate.secondary"
    )


async def test_setup_requires_explicit_supported_idle_alternative(
    hass: HomeAssistant,
) -> None:
    """No FAN_ONLY means the default is not silently rewritten to off."""
    for entity_id in ("climate.primary", "climate.secondary"):
        _set_capabilities(hass, entity_id, hvac_modes=["off", "cool", "heat"])
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"heads": ["climate.primary", "climate.secondary"]},
    )
    result = await _pass_rooms(hass, result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sensor_1": "sensor.primary_temp", "sensor_2": "sensor.secondary_temp"},
    )

    # Saving cannot fill the parking mode itself, so it routes into advanced.
    result = await _press(hass, result, "finish")
    assert result["step_id"] == "tuning"
    assert result["errors"] == {CONF_IDLE_ACTION: "idle_action_unsupported"}
    assert _select_options(result, CONF_IDLE_ACTION) == [IDLE_ACTION_OFF]
    idle_marker = next(
        marker
        for marker in result["data_schema"].schema
        if marker.schema == CONF_IDLE_ACTION
    )
    assert idle_marker.default is vol.UNDEFINED

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IDLE_ACTION: IDLE_ACTION_OFF}
    )
    result = await _press(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_IDLE_ACTION] == IDLE_ACTION_OFF
    assert result["options"][CONF_IDLE_ACTION] == IDLE_ACTION_OFF


async def test_setup_rechecks_idle_capability_at_final_save(
    hass: HomeAssistant,
) -> None:
    """A late FAN_ONLY loss cannot create an entry with an invalid default."""
    result = await _start_user_flow(hass)
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"heads": ["climate.primary", "climate.secondary"]}
    )
    result = await _pass_rooms(hass, result)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"sensor_1": "sensor.primary_temp", "sensor_2": "sensor.secondary_temp"},
    )
    result = await _press(hass, result, "tuning")
    assert result["step_id"] == "tuning"

    _set_capabilities(
        hass, "climate.secondary", hvac_modes=["off", "cool", "heat"]
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY}
    )

    assert result["step_id"] == "tuning"
    assert result["errors"] == {CONF_IDLE_ACTION: "idle_action_unsupported"}
    assert result["description_placeholders"] == {
        "idle_action": IDLE_ACTION_FAN_ONLY,
        "unsupported_heads": "climate.secondary",
        "supported_idle_actions": IDLE_ACTION_OFF,
    }
    assert _select_options(result, CONF_IDLE_ACTION) == [IDLE_ACTION_OFF]
    assert hass.config_entries.async_entries(DOMAIN) == []


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


@pytest.mark.parametrize(
    ("heads", "error"),
    [
        (["climate.a", "climate.a"], "duplicate_heads"),
        (["climate.a"], "need_two_heads"),
        ([f"climate.head_{index}" for index in range(9)], "too_many_heads"),
    ],
)
async def test_user_validation_errors_preserve_submitted_values(
    hass: HomeAssistant, heads: list[str], error: str
) -> None:
    """Every pre-existing picker error keeps the attempted heads and notify value."""
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"heads": heads, CONF_NOTIFY_SERVICE: "notify.mobile_app_test"},
    )

    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}
    assert _form_heads(result) == heads
    assert _form_suggested(result, CONF_NOTIFY_SERVICE) == "notify.mobile_app_test"


@pytest.mark.parametrize(
    ("heads", "error"),
    [
        (["climate.a", "climate.a"], "duplicate_heads"),
        (["climate.a"], "need_two_heads"),
        ([f"climate.head_{index}" for index in range(9)], "too_many_heads"),
    ],
)
async def test_reconfigure_validation_errors_preserve_submitted_values(
    hass: HomeAssistant, heads: list[str], error: str
) -> None:
    """Reconfigure keeps invalid submissions editable without changing the entry."""
    current = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.a", "climate.b")},
        options={CONF_DEMAND_THRESHOLD: 4.0},
        title="Current coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    current.add_to_hass(hass)
    before = _entry_snapshot(current)
    result = await _start_reconfigure_flow(hass, current)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"heads": heads, CONF_NOTIFY_SERVICE: "notify.mobile_app_test"},
    )

    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": error}
    assert _form_heads(result) == heads
    assert _form_suggested(result, CONF_NOTIFY_SERVICE) == "notify.mobile_app_test"
    assert _entry_snapshot(current) == before


async def test_reconfigure_rejects_head_that_cannot_honor_stored_idle_action(
    hass: HomeAssistant,
) -> None:
    """Reconfigure preserves the stored policy and entry instead of switching it."""
    current = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.a", "climate.b")},
        options={CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY},
        title="Current coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    current.add_to_hass(hass)
    before = _entry_snapshot(current)
    _set_capabilities(hass, "climate.c", hvac_modes=["off", "cool", "heat"])

    result = await _start_reconfigure_flow(hass, current)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": ["climate.a", "climate.c"]}
    )

    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"heads": "idle_action_unsupported"}
    assert result["description_placeholders"] == {
        "idle_action": IDLE_ACTION_OFF_AFTER_DRY,
        "unsupported_heads": "climate.c",
        "supported_idle_actions": IDLE_ACTION_OFF,
    }
    assert _form_heads(result) == ["climate.a", "climate.c"]
    assert _entry_snapshot(current) == before


async def test_reconfigure_rechecks_capabilities_before_update(
    hass: HomeAssistant,
) -> None:
    """A capability loss at final save releases M11's reservation and changes nothing."""
    current = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.a", "climate.b")},
        options={CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY},
        title="Current coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    current.add_to_hass(hass)
    before = _entry_snapshot(current)
    result = await _start_reconfigure_flow(hass, current)
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"heads": ["climate.a", "climate.c"]}
    )
    result = await _pass_rooms(hass, result, "reconfigure_rooms")
    assert result["step_id"] == "reconfigure_sensors"

    _set_capabilities(hass, "climate.c", hvac_modes=["off", "cool", "heat"])
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"sensor_1": "sensor.a_temp", "sensor_2": "sensor.c_temp"},
    )
    result = await _press(hass, result, "reconfigure_finish")

    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"heads": "idle_action_unsupported"}
    assert _entry_snapshot(current) == before
    progress = next(
        item
        for item in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if item["flow_id"] == flow_id
    )
    assert "mxz_selected_heads" not in progress["context"]


@pytest.mark.parametrize(
    ("existing_data", "existing_version", "selection", "conflicts"),
    [
        (
            {CONF_ZONES: _zones("climate.a", "climate.b")},
            2,
            ["climate.b", "climate.a"],
            "climate.b, climate.a",
        ),
        (
            {CONF_ZONES: _zones("climate.a", "climate.b")},
            2,
            ["climate.b", "climate.c"],
            "climate.b",
        ),
        (
            {
                CONF_PRIMARY_CLIMATE: "climate.a",
                CONF_SECONDARY_CLIMATE: "climate.b",
                CONF_PRIMARY_SENSOR: "sensor.a_temp",
                CONF_SECONDARY_SENSOR: "sensor.b_temp",
            },
            1,
            ["climate.b", "climate.c"],
            "climate.b",
        ),
        (
            {
                CONF_ZONES: [],
                CONF_PRIMARY_CLIMATE: "climate.a",
                CONF_SECONDARY_CLIMATE: "climate.b",
                CONF_PRIMARY_SENSOR: "sensor.a_temp",
                CONF_SECONDARY_SENSOR: "sensor.b_temp",
            },
            2,
            ["climate.b", "climate.c"],
            "climate.b",
        ),
    ],
)
async def test_user_flow_rejects_heads_owned_by_another_entry(
    hass: HomeAssistant,
    existing_data: dict,
    existing_version: int,
    selection: list[str],
    conflicts: str,
) -> None:
    """Reversed/partial selections conflict with both stored entry schemas."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        data=existing_data,
        options={CONF_DEMAND_THRESHOLD: 3.0},
        title="Existing coordinator",
        unique_id="climate.a|climate.b",
        version=existing_version,
    )
    existing.add_to_hass(hass)
    before = _entry_snapshot(existing)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": selection}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"heads": "heads_already_configured"}
    assert result["description_placeholders"]["conflicting_heads"] == conflicts
    assert _entry_snapshot(existing) == before


async def test_user_flow_allows_heads_disjoint_from_existing_entry(
    hass: HomeAssistant,
) -> None:
    """A separately identified head group remains valid."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.a", "climate.b")},
        title="Existing coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    existing.add_to_hass(hass)
    before = _entry_snapshot(existing)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": ["climate.c", "climate.d"]}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "rooms"
    assert _entry_snapshot(existing) == before


async def test_parallel_user_flows_reserve_selected_heads(
    hass: HomeAssistant,
) -> None:
    """An open flow reserves each selected head until HA finishes the flow."""
    first = await _start_user_flow(hass)
    second = await _start_user_flow(hass)

    first = await hass.config_entries.flow.async_configure(
        first["flow_id"], {"heads": ["climate.a", "climate.b"]}
    )
    assert first["step_id"] == "rooms"

    second = await hass.config_entries.flow.async_configure(
        second["flow_id"], {"heads": ["climate.b", "climate.c"]}
    )
    assert second["type"] is FlowResultType.FORM
    assert second["step_id"] == "user"
    assert second["errors"] == {"heads": "heads_reserved_by_flow"}
    assert second["description_placeholders"]["conflicting_heads"] == "climate.b"


async def test_flow_manager_abort_releases_reserved_heads(
    hass: HomeAssistant,
) -> None:
    """HA's real abort lifecycle removes the flow and its reservation."""
    first = await _start_user_flow(hass)
    second = await _start_user_flow(hass)
    first_id = first["flow_id"]
    second_id = second["flow_id"]

    first = await hass.config_entries.flow.async_configure(
        first_id, {"heads": ["climate.a", "climate.b"]}
    )
    assert first["step_id"] == "rooms"
    blocked = await hass.config_entries.flow.async_configure(
        second_id, {"heads": ["climate.b", "climate.c"]}
    )
    assert blocked["errors"] == {"heads": "heads_reserved_by_flow"}

    hass.config_entries.flow.async_abort(first_id)
    assert all(
        progress["flow_id"] != first_id
        for progress in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )
    retry = await hass.config_entries.flow.async_configure(
        second_id, {"heads": ["climate.b", "climate.c"]}
    )
    assert retry["step_id"] == "rooms"


async def test_success_reclassifies_reservation_as_committed_ownership(
    hass: HomeAssistant,
) -> None:
    """A finished setup leaves an entry, not an in-progress reservation."""
    first = await _start_user_flow(hass)
    first_id = first["flow_id"]
    first = await hass.config_entries.flow.async_configure(
        first_id, {"heads": ["climate.a", "climate.b"]}
    )
    first = await _pass_rooms(hass, first)
    first = await hass.config_entries.flow.async_configure(
        first_id,
        {"sensor_1": "sensor.a_temp", "sensor_2": "sensor.b_temp"},
    )
    assert first["step_id"] == "review"
    first = await _press(hass, first, "finish")
    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert all(
        progress["flow_id"] != first_id
        for progress in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )

    second = await _start_user_flow(hass)
    second = await hass.config_entries.flow.async_configure(
        second["flow_id"], {"heads": ["climate.b", "climate.c"]}
    )
    assert second["errors"] == {"heads": "heads_already_configured"}
    assert second["description_placeholders"]["conflicting_heads"] == "climate.b"


async def test_parallel_reconfigure_flows_for_same_entry_do_not_self_conflict(
    hass: HomeAssistant,
) -> None:
    """Two flows editing one entry ignore each other's early reservation."""
    current = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.a", "climate.b")},
        title="Current coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    current.add_to_hass(hass)
    first = await _start_reconfigure_flow(hass, current)
    second = await _start_reconfigure_flow(hass, current)

    first = await hass.config_entries.flow.async_configure(
        first["flow_id"], {"heads": ["climate.a", "climate.b"]}
    )
    second = await hass.config_entries.flow.async_configure(
        second["flow_id"], {"heads": ["climate.b", "climate.a"]}
    )

    assert first["step_id"] == "reconfigure_rooms"
    assert second["step_id"] == "reconfigure_rooms"


async def test_parallel_reconfigure_flows_for_different_entries_conflict(
    hass: HomeAssistant,
) -> None:
    """Separate entries may not reserve the same newly selected head."""
    first_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.a", "climate.b")},
        title="First coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    second_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.c", "climate.d")},
        title="Second coordinator",
        unique_id="climate.c|climate.d",
        version=2,
    )
    first_entry.add_to_hass(hass)
    second_entry.add_to_hass(hass)
    first = await _start_reconfigure_flow(hass, first_entry)
    second = await _start_reconfigure_flow(hass, second_entry)

    first = await hass.config_entries.flow.async_configure(
        first["flow_id"], {"heads": ["climate.a", "climate.e"]}
    )
    second = await hass.config_entries.flow.async_configure(
        second["flow_id"], {"heads": ["climate.c", "climate.e"]}
    )

    assert first["step_id"] == "reconfigure_rooms"
    assert second["step_id"] == "reconfigure"
    assert second["errors"] == {"heads": "heads_reserved_by_flow"}
    assert second["description_placeholders"]["conflicting_heads"] == "climate.e"


async def test_reconfigure_rejects_other_entry_overlap_without_changes(
    hass: HomeAssistant,
) -> None:
    """A rejected reconfigure leaves both coordinators byte-for-byte equivalent."""
    current = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.a", "climate.b")},
        options={CONF_DEMAND_THRESHOLD: 4.0},
        title="Current coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    other = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.c", "climate.d")},
        options={CONF_DEMAND_THRESHOLD: 5.0},
        title="Other coordinator",
        unique_id="climate.c|climate.d",
        version=2,
    )
    current.add_to_hass(hass)
    other.add_to_hass(hass)
    before = (_entry_snapshot(current), _entry_snapshot(other))

    result = await _start_reconfigure_flow(hass, current)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": ["climate.b", "climate.c"]}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"heads": "heads_already_configured"}
    assert result["description_placeholders"]["conflicting_heads"] == "climate.c"
    assert (_entry_snapshot(current), _entry_snapshot(other)) == before


async def test_reconfigure_grandfathers_current_preexisting_overlap(
    hass: HomeAssistant,
) -> None:
    """An entry may retain its already-stored overlap and correct its sensors."""
    current_zones = _zones("climate.a", "climate.b")
    current_zones[0][ZONE_NAME] = "Living room"
    current_zones[0][ZONE_VANE_VERTICAL] = "select.a_vertical"
    current = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: current_zones, "sentinel": "data"},
        options={CONF_DEMAND_THRESHOLD: 4.0, "sentinel": "options"},
        title="Current coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    other = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.b", "climate.c")},
        title="Older overlapping coordinator",
        unique_id="climate.b|climate.c",
        version=2,
    )
    current.add_to_hass(hass)
    other.add_to_hass(hass)
    entry_id = current.entry_id
    options_before = deepcopy(dict(current.options))
    other_before = _entry_snapshot(other)

    result = await _start_reconfigure_flow(hass, current)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": ["climate.a", "climate.b"]}
    )
    result = await _pass_rooms(hass, result, "reconfigure_rooms")
    assert result["step_id"] == "reconfigure_sensors"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sensor_1": "sensor.a_corrected", "sensor_2": "sensor.b_corrected"},
    )
    result = await _press(hass, result, "reconfigure_finish")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert current.entry_id == entry_id
    assert dict(current.options) == options_before
    assert current.data["sentinel"] == "data"
    assert current.data[CONF_ZONES][0][ZONE_NAME] == "Living room"
    assert current.data[CONF_ZONES][0][ZONE_VANE_VERTICAL] == "select.a_vertical"
    assert [zone[ZONE_SENSOR] for zone in current.data[CONF_ZONES]] == [
        "sensor.a_corrected",
        "sensor.b_corrected",
    ]
    assert _entry_snapshot(other) == other_before


async def test_reconfigure_repairs_preexisting_overlap_without_deleting_entry(
    hass: HomeAssistant,
) -> None:
    """Dropping the shared head repairs ownership in place."""
    current_zones = _zones("climate.a", "climate.b")
    current_zones[0][ZONE_NAME] = "Living room"
    current_zones[0][ZONE_VANE_HORIZONTAL] = "select.a_horizontal"
    current = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: current_zones},
        options={CONF_DEMAND_THRESHOLD: 4.0, "sentinel": "kept"},
        title="Current coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    other = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.b", "climate.c")},
        title="Older overlapping coordinator",
        unique_id="climate.b|climate.c",
        version=2,
    )
    current.add_to_hass(hass)
    other.add_to_hass(hass)
    entry_id = current.entry_id
    options_before = deepcopy(dict(current.options))
    other_before = _entry_snapshot(other)

    result = await _start_reconfigure_flow(hass, current)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": ["climate.a", "climate.d"]}
    )
    result = await _pass_rooms(hass, result, "reconfigure_rooms")
    assert result["step_id"] == "reconfigure_sensors"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sensor_1": "sensor.a_corrected", "sensor_2": "sensor.d_corrected"},
    )
    result = await _press(hass, result, "reconfigure_finish")

    assert result["reason"] == "reconfigure_successful"
    assert current.entry_id == entry_id
    assert dict(current.options) == options_before
    assert current.unique_id == "climate.a|climate.d"
    assert current.data[CONF_ZONES][0][ZONE_NAME] == "Living room"
    assert current.data[CONF_ZONES][0][ZONE_VANE_HORIZONTAL] == "select.a_horizontal"
    assert [zone[ZONE_CLIMATE] for zone in current.data[CONF_ZONES]] == [
        "climate.a",
        "climate.d",
    ]
    assert [zone[ZONE_SENSOR] for zone in current.data[CONF_ZONES]] == [
        "sensor.a_corrected",
        "sensor.d_corrected",
    ]
    assert _entry_snapshot(other) == other_before
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_reconfigure_cannot_expand_preexisting_overlap(
    hass: HomeAssistant,
) -> None:
    """Grandfathering does not permit adding another head from the other entry."""
    current = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.a", "climate.b")},
        options={CONF_DEMAND_THRESHOLD: 4.0},
        title="Current coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    other = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.b", "climate.c")},
        title="Older overlapping coordinator",
        unique_id="climate.b|climate.c",
        version=2,
    )
    current.add_to_hass(hass)
    other.add_to_hass(hass)
    before = (_entry_snapshot(current), _entry_snapshot(other))

    result = await _start_reconfigure_flow(hass, current)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"heads": ["climate.a", "climate.b", "climate.c"]},
    )

    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"heads": "heads_already_configured"}
    assert result["description_placeholders"]["conflicting_heads"] == "climate.c"
    assert _form_heads(result) == ["climate.a", "climate.b", "climate.c"]
    assert (_entry_snapshot(current), _entry_snapshot(other)) == before


async def test_reconfigure_allows_reordered_heads_owned_by_current_entry(
    hass: HomeAssistant,
) -> None:
    """The current entry may keep and reorder its own heads."""
    current = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.a", "climate.b")},
        title="Current coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    current.add_to_hass(hass)

    result = await _start_reconfigure_flow(hass, current)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": ["climate.b", "climate.a"]}
    )
    result = await _pass_rooms(hass, result, "reconfigure_rooms")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_sensors"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sensor_1": "sensor.b_new", "sensor_2": "sensor.a_new"},
    )
    result = await _press(hass, result, "reconfigure_finish")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert current.unique_id == "climate.b|climate.a"
    assert [zone[ZONE_CLIMATE] for zone in current.data[CONF_ZONES]] == [
        "climate.b",
        "climate.a",
    ]


async def test_user_flow_rechecks_ownership_before_create(
    hass: HomeAssistant,
) -> None:
    """A head claimed while setup is open prevents the later create."""
    result = await _start_user_flow(hass)
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"heads": ["climate.a", "climate.b"]}
    )
    result = await _pass_rooms(hass, result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sensor_1": "sensor.a_temp", "sensor_2": "sensor.b_temp"},
    )
    assert result["step_id"] == "review"

    winner = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.b", "climate.c")},
        title="Interleaved winner",
        unique_id="climate.b|climate.c",
        version=2,
    )
    winner.add_to_hass(hass)
    before = _entry_snapshot(winner)
    result = await _press(hass, result, "finish")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"heads": "heads_already_configured"}
    assert result["description_placeholders"]["conflicting_heads"] == "climate.b"
    assert hass.config_entries.async_entries(DOMAIN) == [winner]
    assert _entry_snapshot(winner) == before
    progress = next(
        item
        for item in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if item["flow_id"] == flow_id
    )
    assert "mxz_selected_heads" not in progress["context"]


async def test_reconfigure_rechecks_ownership_before_update(
    hass: HomeAssistant,
) -> None:
    """A head claimed while reconfigure is open prevents its later update."""
    current = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.a", "climate.b")},
        options={CONF_DEMAND_THRESHOLD: 4.0},
        title="Current coordinator",
        unique_id="climate.a|climate.b",
        version=2,
    )
    current.add_to_hass(hass)
    current_before = _entry_snapshot(current)

    result = await _start_reconfigure_flow(hass, current)
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"heads": ["climate.a", "climate.c"]}
    )
    result = await _pass_rooms(hass, result, "reconfigure_rooms")
    assert result["step_id"] == "reconfigure_sensors"

    winner = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: _zones("climate.c", "climate.d")},
        title="Interleaved winner",
        unique_id="climate.c|climate.d",
        version=2,
    )
    winner.add_to_hass(hass)
    winner_before = _entry_snapshot(winner)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"sensor_1": "sensor.a_new", "sensor_2": "sensor.c_new"},
    )
    result = await _press(hass, result, "reconfigure_finish")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"heads": "heads_already_configured"}
    assert result["description_placeholders"]["conflicting_heads"] == "climate.c"
    assert _entry_snapshot(current) == current_before
    assert _entry_snapshot(winner) == winner_before
    progress = next(
        item
        for item in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if item["flow_id"] == flow_id
    )
    assert "mxz_selected_heads" not in progress["context"]


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


async def test_options_flow_round_trips_idle_action(hass: HomeAssistant) -> None:
    """The idle_action select saves and reads back through the options flow."""
    entry = MockConfigEntry(domain=DOMAIN, data=_VALID, title="MXZ Coordinator")
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IDLE_ACTION: "off_after_dry"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_IDLE_ACTION] == "off_after_dry"


async def test_options_flow_rejects_unsupported_idle_without_mutation(
    hass: HomeAssistant,
) -> None:
    """An options save cannot install a parking command a configured head rejects."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_VALID,
        options={CONF_IDLE_ACTION: IDLE_ACTION_OFF},
        title="MXZ Coordinator",
    )
    entry.add_to_hass(hass)
    before = _entry_snapshot(entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert IDLE_ACTION_OFF_AFTER_DRY in _select_options(result, CONF_IDLE_ACTION)

    # The displayed choice becomes invalid before the user submits it. This
    # reaches the in-flow recheck through HA's public schema-validated API.
    _set_capabilities(
        hass, "climate.secondary", hvac_modes=["off", "cool", "heat"]
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY}
    )

    assert result["step_id"] == "init"
    assert result["errors"] == {CONF_IDLE_ACTION: "idle_action_unsupported"}
    assert result["description_placeholders"] == {
        "idle_action": IDLE_ACTION_OFF_AFTER_DRY,
        "unsupported_heads": "climate.secondary",
        "supported_idle_actions": IDLE_ACTION_OFF,
    }
    assert _select_options(result, CONF_IDLE_ACTION) == [IDLE_ACTION_OFF]
    assert _entry_snapshot(entry) == before


@pytest.mark.parametrize(
    ("missing_state", "error"),
    [
        (True, "head_capabilities_unavailable"),
        (False, "head_missing_idle_modes"),
    ],
)
async def test_options_flow_keeps_recovery_form_when_no_idle_choice_is_known(
    hass: HomeAssistant, missing_state: bool, error: str
) -> None:
    """Configure renders an authored recovery error, never an empty selector."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_VALID,
        options={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_DEMAND_THRESHOLD: 4.0,
        },
        title="MXZ Coordinator",
    )
    entry.add_to_hass(hass)
    before = _entry_snapshot(entry)
    if missing_state:
        hass.states.async_remove("climate.secondary")
    else:
        for entity_id in ("climate.primary", "climate.secondary"):
            _set_capabilities(hass, entity_id, hvac_modes=["cool", "heat"])

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": error}
    assert result["description_placeholders"]["unsupported_heads"] == (
        "climate.secondary"
        if missing_state
        else "climate.primary, climate.secondary"
    )
    assert not _form_has_field(result, CONF_IDLE_ACTION)
    assert result["data_schema"]({})[CONF_DEMAND_THRESHOLD] == 4.0
    assert _entry_snapshot(entry) == before

    # A submit while the heads remain unresolved stays on the same actionable
    # public form and still cannot mutate stored data, options, or identity.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEMAND_THRESHOLD: 5.0}
    )
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": error}
    assert not _form_has_field(result, CONF_IDLE_ACTION)
    assert result["data_schema"]({})[CONF_DEMAND_THRESHOLD] == 5.0
    assert _entry_snapshot(entry) == before


async def test_options_flow_preserves_incompatible_stored_idle_until_explicit_change(
    hass: HomeAssistant,
) -> None:
    """Existing bytes stay intact and the compatible alternative has no silent default."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_VALID,
        options={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_DEMAND_THRESHOLD: 4.0,
        },
        title="MXZ Coordinator",
    )
    entry.add_to_hass(hass)
    before = _entry_snapshot(entry)
    for entity_id in ("climate.primary", "climate.secondary"):
        _set_capabilities(hass, entity_id, hvac_modes=["off", "cool", "heat"])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert _select_options(result, CONF_IDLE_ACTION) == [IDLE_ACTION_OFF]
    idle_marker = next(
        marker
        for marker in result["data_schema"].schema
        if marker.schema == CONF_IDLE_ACTION
    )
    assert idle_marker.default is vol.UNDEFINED
    assert _entry_snapshot(entry) == before

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IDLE_ACTION: IDLE_ACTION_OFF}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_IDLE_ACTION] == IDLE_ACTION_OFF
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


async def test_reconfigure_save_reloads_the_entry_exactly_once(
    hass: HomeAssistant,
) -> None:
    """#15: one reconfigure save = one reload; an unchanged save = zero.

    async_update_reload_and_abort fired the update listener AND scheduled its
    own reload — two full back-to-back reloads per save, the reconfigure twin
    of #14's options-save double. The entry is now updated directly and the
    listener does the single reload.
    """
    from unittest.mock import patch

    from homeassistant import config_entries

    from tests.test_drive import (
        SENSOR_A,
        SENSOR_B,
        _set_temp,
        _setup_fan_boost,
        _setup_mock_heads,
    )

    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    reloads = 0
    real_reload = hass.config_entries.async_reload

    async def _counting_reload(entry_id):
        nonlocal reloads
        reloads += 1
        return await real_reload(entry_id)

    async def _run_reconfigure(first: str, second: str) -> None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        assert result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"heads": [head_a, head_b]}
        )
        result = await _pass_rooms(hass, result, "reconfigure_rooms")
        assert result["step_id"] == "reconfigure_sensors"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"sensor_1": first, "sensor_2": second},
        )
        result = await _press(hass, result, "reconfigure_finish")
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()

    with patch.object(
        hass.config_entries, "async_reload", side_effect=_counting_reload
    ):
        # A real change: the two rooms' sensors are swapped. They stay
        # distinct, because one sensor may not drive two rooms.
        await _run_reconfigure(SENSOR_B, SENSOR_A)
        assert reloads == 1, f"changed reconfigure ran {reloads} reloads"
        await _run_reconfigure(SENSOR_B, SENSOR_A)  # identical submit
        assert reloads == 1, f"unchanged reconfigure ran {reloads - 1} extra reloads"


# --- M18: tuning validation (invalid values can't reach stored options) -------


def _tuning_defaults(celsius: bool) -> dict[str, float]:
    """The effective tunable defaults for a unit system, read from const.py.

    An independent oracle: these tests must not restate the numbers the flow
    itself fills in, and the °F and °C profiles hold different ones.
    """
    return dict(unit_profile(celsius)["defaults"])


_FAHRENHEIT = _tuning_defaults(celsius=False)


@pytest.mark.parametrize(
    ("submission", "expected_errors"),
    [
        # Non-finite / non-numeric: `NumberSelector` coerces with float(), which
        # accepts "nan"/"inf" and passes them straight through — NaN then fails
        # every comparison, so no min/max bound can catch it downstream.
        ({CONF_DEMAND_THRESHOLD: float("nan")}, {CONF_DEMAND_THRESHOLD: "not_a_number"}),
        ({CONF_CLAMP_MAX: float("inf")}, {CONF_CLAMP_MAX: "not_a_number"}),
        ({CONF_ECO_COOL_MAX: float("-inf")}, {CONF_ECO_COOL_MAX: "not_a_number"}),
        ({CONF_DEMAND_THRESHOLD: "nan"}, {CONF_DEMAND_THRESHOLD: "not_a_number"}),
        # Negative where the quantity is a magnitude or a duration.
        ({CONF_DEMAND_THRESHOLD: -1.0}, {CONF_DEMAND_THRESHOLD: "must_not_be_negative"}),
        ({CONF_MODE_HYSTERESIS: -1}, {CONF_MODE_HYSTERESIS: "must_not_be_negative"}),
        (
            {CONF_COIL_DRY_MINUTES: -0.5},
            {CONF_COIL_DRY_MINUTES: "must_not_be_negative"},
        ),
        # Inverted pairs: both members are named, because either one of them can
        # be the number the user got wrong.
        (
            {CONF_ECO_HEAT_MIN: _FAHRENHEIT[CONF_ECO_COOL_MAX] + 1.0},
            {
                CONF_ECO_HEAT_MIN: "eco_band_inverted",
                CONF_ECO_COOL_MAX: "eco_band_inverted",
            },
        ),
        (
            {CONF_CLAMP_MIN: _FAHRENHEIT[CONF_CLAMP_MAX] + 1.0},
            {CONF_CLAMP_MIN: "clamp_inverted", CONF_CLAMP_MAX: "clamp_inverted"},
        ),
        (
            {CONF_HEAT_LOCKOUT_FLOOR: _FAHRENHEIT[CONF_COOL_LOCKOUT_CEILING] + 1.0},
            {
                CONF_HEAT_LOCKOUT_FLOOR: "lockout_inverted",
                CONF_COOL_LOCKOUT_CEILING: "lockout_inverted",
            },
        ),
        (
            {CONF_CHANGEOVER_COOL_BELOW: _FAHRENHEIT[CONF_CHANGEOVER_HEAT_ABOVE] + 1.0},
            {
                CONF_CHANGEOVER_COOL_BELOW: "changeover_inverted",
                CONF_CHANGEOVER_HEAT_ABOVE: "changeover_inverted",
            },
        ),
        # Equal changeover thresholds are rejected too: the gap between them is
        # the shoulder season logic.season_lockouts documents as its hysteresis,
        # and a zero-width shoulder flips the heat lockout to the cool lockout on
        # a single forecast reading.
        (
            {CONF_CHANGEOVER_COOL_BELOW: _FAHRENHEIT[CONF_CHANGEOVER_HEAT_ABOVE]},
            {
                CONF_CHANGEOVER_COOL_BELOW: "changeover_inverted",
                CONF_CHANGEOVER_HEAT_ABOVE: "changeover_inverted",
            },
        ),
    ],
    ids=[
        "nan",
        "inf",
        "negative_inf",
        "nan_string",
        "negative_demand_threshold",
        "negative_mode_hysteresis",
        "negative_coil_dry_minutes",
        "eco_band_inverted",
        "clamp_inverted",
        "lockout_inverted",
        "changeover_inverted",
        "changeover_equal",
    ],
)
async def test_options_flow_rejects_invalid_tuning_and_preserves_last_valid(
    hass: HomeAssistant, submission, expected_errors
) -> None:
    """Every invalid class shows its field error and leaves the entry untouched.

    The browser number widget can't produce most of these, but a raw
    WebSocket/API client submits whatever it likes, and the last valid options
    must survive it.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_VALID,
        options={CONF_DEMAND_THRESHOLD: 3.0, CONF_CLAMP_MAX: 88.0},
        title="MXZ Coordinator",
    )
    entry.add_to_hass(hass)
    before = _entry_snapshot(entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], submission
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == expected_errors
    assert _entry_snapshot(entry) == before
    # The rejected form still renders the field the user must correct.
    for field in expected_errors:
        assert _form_has_field(result, field)


@pytest.mark.parametrize(
    "submission",
    [
        # Just inside each ordering bound.
        {CONF_ECO_HEAT_MIN: _FAHRENHEIT[CONF_ECO_COOL_MAX] - 0.1},
        {CONF_CLAMP_MIN: _FAHRENHEIT[CONF_CLAMP_MAX] - 0.1},
        {CONF_HEAT_LOCKOUT_FLOOR: _FAHRENHEIT[CONF_COOL_LOCKOUT_CEILING] - 0.1},
        {CONF_CHANGEOVER_COOL_BELOW: _FAHRENHEIT[CONF_CHANGEOVER_HEAT_ABOVE] - 0.1},
        # Exactly ON the bound: a zero-width band is degenerate but consistent,
        # and these three consumers handle it, so it stays accepted rather than
        # being tightened away. Only the changeover pair is refused, because its
        # gap is the documented shoulder season.
        {CONF_ECO_HEAT_MIN: _FAHRENHEIT[CONF_ECO_COOL_MAX]},
        {CONF_CLAMP_MIN: _FAHRENHEIT[CONF_CLAMP_MAX]},
        {CONF_HEAT_LOCKOUT_FLOOR: _FAHRENHEIT[CONF_COOL_LOCKOUT_CEILING]},
        # Zero stays a legal duration/magnitude (no dwell, no coil-dry period,
        # flip on any deviation) — this change must not redefine it.
        {CONF_DEMAND_THRESHOLD: 0.0},
        {CONF_MODE_HYSTERESIS: 0},
        {CONF_COIL_DRY_MINUTES: 0},
    ],
    ids=[
        "eco_just_inside",
        "clamp_just_inside",
        "lockout_just_inside",
        "changeover_just_inside",
        "eco_equal",
        "clamp_equal",
        "lockout_equal",
        "zero_demand_threshold",
        "zero_mode_hysteresis",
        "zero_coil_dry_minutes",
    ],
)
async def test_options_flow_accepts_boundary_tuning_values(
    hass: HomeAssistant, submission
) -> None:
    """A value at or just inside a bound saves exactly as submitted."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = MockConfigEntry(domain=DOMAIN, data=_VALID, title="MXZ Coordinator")
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], submission
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    key, value = next(iter(submission.items()))
    assert result["data"][key] == value
    assert entry.options[key] == value


async def test_options_flow_keeps_every_untouched_tunable_on_rejection(
    hass: HomeAssistant,
) -> None:
    """A rejected submit preserves the OTHER stored tunables, not just the entry.

    The stored set here is deliberately non-default, so a silent revert to the
    profile defaults would be visible.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    stored = {
        CONF_DEMAND_THRESHOLD: 4.5,
        CONF_MODE_HYSTERESIS: 900,
        CONF_CLAMP_MIN: 60.0,
        CONF_CLAMP_MAX: 86.0,
    }
    entry = MockConfigEntry(
        domain=DOMAIN, data=_VALID, options=dict(stored), title="MXZ Coordinator"
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CLAMP_MIN: 90.0}  # above the stored clamp_max
    )

    assert result["errors"] == {
        CONF_CLAMP_MIN: "clamp_inverted",
        CONF_CLAMP_MAX: "clamp_inverted",
    }
    assert dict(entry.options) == stored


@pytest.mark.parametrize(
    "celsius", [False, True], ids=["fahrenheit", "celsius"]
)
async def test_setup_flow_rejects_then_accepts_symmetrically_per_unit(
    hass: HomeAssistant, celsius: bool
) -> None:
    """The same relative case behaves identically in °F and °C at setup.

    °C and °F have different default tunables (const.unit_profile), so the
    thresholds are derived per unit rather than hardcoded. An inverted clamp
    pair creates no entry; resubmitting the equal-value boundary does.
    """
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    defaults = _tuning_defaults(celsius)
    clamp_max = float(defaults[CONF_CLAMP_MAX])

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": ["climate.primary", "climate.secondary"]}
    )
    result = await _pass_rooms(hass, result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sensor_1": "sensor.primary_temp", "sensor_2": "sensor.secondary_temp"},
    )
    result = await _press(hass, result, "tuning")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CLAMP_MIN: clamp_max + 1.0}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tuning"
    assert result["errors"] == {
        CONF_CLAMP_MIN: "clamp_inverted",
        CONF_CLAMP_MAX: "clamp_inverted",
    }
    assert hass.config_entries.async_entries(DOMAIN) == []

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CLAMP_MIN: clamp_max}
    )
    result = await _press(hass, result, "finish")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_CLAMP_MIN] == clamp_max
    assert result["options"][CONF_CLAMP_MAX] == clamp_max
    # The unit's own defaults still fill everything the user didn't touch.
    assert result["options"][CONF_ECO_HEAT_MIN] == defaults[CONF_ECO_HEAT_MIN]


async def test_options_flow_validates_symmetrically_in_celsius(
    hass: HomeAssistant,
) -> None:
    """A sub-zero eco extreme is legal in °C; only inverting the band is not.

    Temperatures must never be caught by the negative-value rule — below zero is
    an ordinary Celsius reading.
    """
    hass.config.units = METRIC_SYSTEM
    defaults = _tuning_defaults(celsius=True)
    entry = MockConfigEntry(domain=DOMAIN, data=_VALID, title="MXZ Coordinator")
    entry.add_to_hass(hass)
    before = _entry_snapshot(entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({})[CONF_ECO_COOL_MAX] == defaults[CONF_ECO_COOL_MAX]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ECO_HEAT_MIN: float(defaults[CONF_ECO_COOL_MAX]) + 1.0},
    )
    assert result["errors"] == {
        CONF_ECO_HEAT_MIN: "eco_band_inverted",
        CONF_ECO_COOL_MAX: "eco_band_inverted",
    }
    assert _entry_snapshot(entry) == before

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ECO_HEAT_MIN: -5.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ECO_HEAT_MIN] == -5.0


def test_validate_tunables_covers_a_field_its_selector_already_bounds() -> None:
    """The deadband's own selector rejects negatives, so the flow never sees one.

    Kept in the validator anyway: it is the only check that survives a schema
    change, and the schema's own refusal is an exception (InvalidData), not a
    field error a user can read. Asserted here directly because no flow
    submission can reach it.
    """
    engage_min, engage_max = unit_profile(celsius=False)["engage_bounds"]
    schema = _tunables_schema({}, engage_min, engage_max)
    with pytest.raises(vol.Invalid):
        schema({CONF_ENGAGE_DEADBAND: -1.0})
    assert _validate_tunables({CONF_ENGAGE_DEADBAND: -1.0}) == {
        CONF_ENGAGE_DEADBAND: "must_not_be_negative"
    }
    # A value problem outranks an ordering it would make meaningless.
    assert _validate_tunables(
        {CONF_CLAMP_MIN: float("nan"), CONF_CLAMP_MAX: 50.0}
    ) == {CONF_CLAMP_MIN: "not_a_number"}


async def test_migration_does_not_revalidate_stored_tuning(hass: HomeAssistant) -> None:
    """A v1->v2 migration never re-validates stored tuning.

    Validation runs at flow-submit time only. A legacy entry already holding a
    combination the validator would now reject on resubmit (an inverted eco
    band) must still migrate unchanged — an upgrade may not turn a working
    install into a broken one.
    """
    from custom_components.mxz_coordinator import async_migrate_entry

    stored = {CONF_ECO_HEAT_MIN: 90.0, CONF_ECO_COOL_MAX: 78.0}
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        title="MXZ Coordinator",
        data=dict(_VALID),
        options=dict(stored),
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 2
    assert dict(entry.options) == stored
    assert entry.data[CONF_ZONES][0][ZONE_CLIMATE] == _VALID[CONF_PRIMARY_CLIMATE]
    # The same combination IS rejected when a human resubmits it.
    assert _validate_tunables(stored) == {
        CONF_ECO_HEAT_MIN: "eco_band_inverted",
        CONF_ECO_COOL_MAX: "eco_band_inverted",
    }
