"""The revamped setup flow, scenario by scenario (M27 design -> M28).

Every test here names the M27 scenario id and the walkthrough-checklist row it
binds, so a reader can go from `planning/eval/M27/scenarios.feature` to an
executed assertion without guessing. Fake heads and fake sensors only: this is
an in-process Home Assistant, and nothing here says anything about hardware.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import (
    CONF_CHANGEOVER_COOL_BELOW,
    CONF_CHANGEOVER_HEAT_ABOVE,
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    CONF_COOL_LOCKOUT_CEILING,
    CONF_DEMAND_THRESHOLD,
    CONF_ENGAGE_DEADBAND,
    CONF_FAN_BOOST_ENABLE,
    CONF_HEAT_LOCKOUT_FLOOR,
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
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
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
    ZONE_STAGE_SENSOR,
    ZONE_VANE_HORIZONTAL,
    ZONE_VANE_VERTICAL,
    unit_profile,
)

LIVING = "climate.living_room"
BEDROOM = "climate.bedroom"
SPARE = "climate.spare"
LIVING_SENSOR = "sensor.living_room_temperature"
BEDROOM_SENSOR = "sensor.bedroom_temperature"
HALL_SENSOR = "sensor.hall_temperature"

# Every tuning key the twenty-field advanced form can carry. Written out here
# on purpose: an independent list, so a key silently vanishing from the flow
# would show up as a difference rather than as two matching mistakes.
_TUNING_KEYS = frozenset(
    {
        "demand_threshold",
        "engage_deadband",
        "mode_hysteresis",
        "eco_cool_max",
        "eco_heat_min",
        "clamp_min",
        "clamp_max",
        "resting_mode_bias",
        "heat_lockout_floor",
        "cool_lockout_ceiling",
        "changeover_heat_above",
        "changeover_cool_below",
        "fan_boost_enable",
        "fan_boost_max",
        "inhibit_active_state",
        "inhibit_action",
        "idle_action",
        "coil_dry_minutes",
    }
)


def _head(
    hass: HomeAssistant,
    entity_id: str,
    *,
    friendly_name: str | None = None,
    hvac_modes: list[str] | None = None,
    advertise: bool = True,
) -> None:
    """Publish one fake indoor head with the attributes a real head exposes."""
    attrs: dict[str, Any] = {}
    if friendly_name is not None:
        attrs["friendly_name"] = friendly_name
    if advertise:
        attrs["hvac_modes"] = hvac_modes or ["off", "cool", "heat", "fan_only"]
        attrs["supported_features"] = int(ClimateEntityFeature.FAN_MODE)
        attrs["fan_modes"] = ["auto", "quiet", "low", "medium", "middle", "high"]
    hass.states.async_set(entity_id, "off", attrs)


def _sensor(
    hass: HomeAssistant,
    entity_id: str,
    value: str,
    unit: Any = "unset",
) -> None:
    """Publish one fake room-temperature sensor."""
    attrs: dict[str, Any] = {"device_class": "temperature"}
    if unit != "unset":
        attrs["unit_of_measurement"] = unit
    hass.states.async_set(entity_id, value, attrs)


@pytest.fixture(autouse=True)
def _household(hass: HomeAssistant) -> None:
    """Two rooms, both healthy, plus the spares the negative cases need."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    _head(hass, LIVING, friendly_name="Living Room")
    _head(hass, BEDROOM, friendly_name="Bedroom")
    _head(hass, SPARE, friendly_name="Spare")
    _sensor(hass, LIVING_SENSOR, "70.7", "°F")
    _sensor(hass, BEDROOM_SENSOR, "67.6", "°F")
    _sensor(hass, HALL_SENSOR, "69.0", "°F")


# --- flow driving ----------------------------------------------------------


async def _start(hass: HomeAssistant) -> config_entries.ConfigFlowResult:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _submit(
    hass: HomeAssistant, result: config_entries.ConfigFlowResult, data: dict[str, Any]
) -> config_entries.ConfigFlowResult:
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def _press(
    hass: HomeAssistant, result: config_entries.ConfigFlowResult, option: str
) -> config_entries.ConfigFlowResult:
    assert result["type"] is FlowResultType.MENU, result
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )


async def _to_review(
    hass: HomeAssistant,
    *,
    heads: list[str] | None = None,
    names: dict[str, str] | None = None,
    sensors: list[str] | None = None,
    title: str | None = None,
    notify: str | None = None,
) -> config_entries.ConfigFlowResult:
    """Walk user -> rooms -> sensors and stop on the review screen."""
    heads = heads or [LIVING, BEDROOM]
    sensors = sensors or [LIVING_SENSOR, BEDROOM_SENSOR]
    result = await _start(hass)
    payload: dict[str, Any] = {"heads": heads}
    if title is not None:
        payload["entry_title"] = title
    if notify is not None:
        payload["notify_service"] = notify
    result = await _submit(hass, result, payload)
    assert result["step_id"] == "rooms", result
    result = await _submit(hass, result, names or {})
    assert result["step_id"] == "sensors", result
    return await _submit(
        hass,
        result,
        {f"sensor_{index + 1}": entity for index, entity in enumerate(sensors)},
    )


def _suggested(result: config_entries.ConfigFlowResult, field: str) -> Any:
    for marker in result["data_schema"].schema:
        if marker.schema == field:
            return marker.description.get("suggested_value")
    raise AssertionError(f"missing schema field: {field}")


def _fields(result: config_entries.ConfigFlowResult) -> list[str]:
    return [marker.schema for marker in result["data_schema"].schema]


# ---------- S01-S06: the basic flow ----------------------------------------


async def test_s01_default_install_is_four_screens_with_no_tuning_field(
    hass: HomeAssistant,
) -> None:
    """S01 / W1-W6: user -> rooms -> sensors -> review, and no knob on the way."""
    result = await _start(hass)
    assert result["step_id"] == "user"
    seen: list[list[str]] = [_fields(result)]

    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    assert result["step_id"] == "rooms"
    seen.append(_fields(result))

    result = await _submit(hass, result, {})
    assert result["step_id"] == "sensors"
    seen.append(_fields(result))

    result = await _submit(
        hass, result, {"sensor_1": LIVING_SENSOR, "sensor_2": BEDROOM_SENSOR}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "review"
    assert result["menu_options"] == ["finish", "tuning", "user"]

    shown = {field for fields in seen for field in fields}
    assert not shown & _TUNING_KEYS, shown & _TUNING_KEYS
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_s02_priority_order_is_the_same_on_every_screen(
    hass: HomeAssistant,
) -> None:
    """S02 / W2, W3, W5: the number a user reads is the priority, everywhere."""
    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    rooms_text = result["description_placeholders"]["rooms"]
    assert rooms_text.index(LIVING) < rooms_text.index(BEDROOM)
    assert rooms_text.startswith(f"1. {LIVING} (priority 1)")

    result = await _submit(hass, result, {})
    sensors_text = result["description_placeholders"]["rooms"]
    assert sensors_text.index("1. Living Room") < sensors_text.index("2. Bedroom")

    result = await _submit(
        hass, result, {"sensor_1": LIVING_SENSOR, "sensor_2": BEDROOM_SENSOR}
    )
    summary = result["description_placeholders"]["summary"]
    assert summary.index("Priority 1  Living Room") < summary.index(
        "Priority 2  Bedroom"
    )


async def test_s03_room_name_is_prefilled_from_the_head_and_editable(
    hass: HomeAssistant,
) -> None:
    """S03 / W3: the auto-name arrives pre-filled and one keystroke replaces it."""
    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    assert _suggested(result, "room_name_1") == "Living Room"
    assert _suggested(result, "room_name_2") == "Bedroom"

    result = await _submit(hass, result, {"room_name_1": "Lounge"})
    result = await _submit(
        hass, result, {"sensor_1": LIVING_SENSOR, "sensor_2": BEDROOM_SENSOR}
    )
    result = await _press(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    zones = result["data"][CONF_ZONES]
    # The name lands in the EXISTING zone key; no new stored key appears.
    assert zones[0][ZONE_NAME] == "Lounge"
    assert zones[1][ZONE_NAME] == "Bedroom"


async def test_s04_a_blank_room_name_is_refused(hass: HomeAssistant) -> None:
    """S04 / W3a: clearing a name stops the flow; nothing is created."""
    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    result = await _submit(hass, result, {"room_name_1": "   "})

    assert result["step_id"] == "rooms"
    assert result["errors"] == {"base": "room_name_empty"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_s05_two_rooms_cannot_share_one_name(hass: HomeAssistant) -> None:
    """S05 / W3b: a duplicate is named back, case-insensitively."""
    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    result = await _submit(
        hass, result, {"room_name_1": "Bedroom", "room_name_2": "bedroom"}
    )

    assert result["step_id"] == "rooms"
    assert result["errors"] == {"base": "duplicate_room_names"}
    assert result["description_placeholders"]["problem_names"] == "bedroom"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_s06_summary_says_nothing_is_controlled_yet(
    hass: HomeAssistant,
) -> None:
    """S06 / W6, W7b: the review screen is where "starts off" is disclosed."""
    result = await _to_review(hass)
    # The sentence lives in the step description (translatable); this asserts
    # the fact it states — nothing is enabled by what the flow stores.
    assert result["step_id"] == "review"
    result = await _press(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "coordinator_enable" not in result["data"]
    assert all("enable" not in zone for zone in result["data"][CONF_ZONES])


# ---------- S07-S11: advanced tuning behind the gate ------------------------


async def _skip_advanced_options(hass: HomeAssistant) -> dict[str, Any]:
    result = await _to_review(hass)
    result = await _press(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return dict(result["options"])


async def _submit_advanced_untouched(hass: HomeAssistant) -> dict[str, Any]:
    result = await _to_review(hass)
    result = await _press(hass, result, "tuning")
    assert result["step_id"] == "tuning"
    result = await _submit(hass, result, {})
    result = await _press(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return dict(result["options"])


@pytest.mark.parametrize("celsius", [False, True], ids=["fahrenheit", "celsius"])
async def test_s07_s08_skipping_advanced_equals_submitting_it_untouched(
    hass: HomeAssistant, celsius: bool
) -> None:
    """S07, S08 / W7, W7c: the two paths produce one option set, in both units.

    The oracle is the OTHER path, driven for real — not a hand-written dict that
    could drift alongside the code it is supposed to check.
    """
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    skipped = await _skip_advanced_options(hass)
    # Same heads, so the comparison is between the two PATHS and nothing else.
    await hass.config_entries.async_remove(
        hass.config_entries.async_entries(DOMAIN)[0].entry_id
    )
    submitted = await _submit_advanced_untouched(hass)
    assert skipped == submitted

    profile = dict(unit_profile(celsius)["defaults"])
    assert set(skipped) == _TUNING_KEYS
    # changeover_entity / inhibit_entity are optional with no default, so a
    # default submit has never stored them. Eighteen keys, not twenty.
    assert len(skipped) == 18
    assert "changeover_entity" not in skipped
    assert "inhibit_entity" not in skipped
    assert skipped[CONF_DEMAND_THRESHOLD] == pytest.approx(
        profile[CONF_DEMAND_THRESHOLD]
    )
    assert skipped[CONF_ENGAGE_DEADBAND] == pytest.approx(
        profile[CONF_ENGAGE_DEADBAND]
    )


async def test_s09_advanced_is_reachable_and_returns_to_the_summary(
    hass: HomeAssistant,
) -> None:
    """S09 / W8, W8c: the knob screen is one button away and hands back."""
    result = await _to_review(hass)
    result = await _press(hass, result, "tuning")
    assert result["step_id"] == "tuning"
    assert CONF_DEMAND_THRESHOLD in _fields(result)

    result = await _submit(hass, result, {CONF_DEMAND_THRESHOLD: 5.0})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "review"
    assert "your choices" in result["description_placeholders"]["summary"]
    assert hass.config_entries.async_entries(DOMAIN) == []

    result = await _press(hass, result, "finish")
    assert result["options"][CONF_DEMAND_THRESHOLD] == 5.0


async def test_s10_equal_changeover_pair_is_still_rejected(
    hass: HomeAssistant,
) -> None:
    """S10 / W8a: accepted M18's key and both field names, unchanged."""
    defaults = dict(unit_profile(celsius=False)["defaults"])
    result = await _to_review(hass)
    result = await _press(hass, result, "tuning")
    result = await _submit(
        hass,
        result,
        {CONF_CHANGEOVER_COOL_BELOW: defaults[CONF_CHANGEOVER_HEAT_ABOVE]},
    )

    assert result["step_id"] == "tuning"
    assert result["errors"] == {
        CONF_CHANGEOVER_COOL_BELOW: "changeover_inverted",
        CONF_CHANGEOVER_HEAT_ABOVE: "changeover_inverted",
    }
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_s10a_equal_lockout_pair_is_still_accepted(
    hass: HomeAssistant,
) -> None:
    """S10a / W8a1: the CONTROL. Accepted M18 allows lockout equality.

    A "fix" that rejected this would quietly narrow a safety setting the
    validator deliberately permits, so this test exists to forbid it.
    """
    defaults = dict(unit_profile(celsius=False)["defaults"])
    ceiling = float(defaults[CONF_COOL_LOCKOUT_CEILING])
    result = await _to_review(hass)
    result = await _press(hass, result, "tuning")
    result = await _submit(hass, result, {CONF_HEAT_LOCKOUT_FLOOR: ceiling})

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "review"
    result = await _press(hass, result, "finish")
    assert result["options"][CONF_HEAT_LOCKOUT_FLOOR] == ceiling
    assert result["options"][CONF_COOL_LOCKOUT_CEILING] == ceiling


async def test_s11_non_finite_number_is_still_rejected(hass: HomeAssistant) -> None:
    """S11 / W8b: `not_a_number`, spelled exactly as accepted M18 spells it."""
    result = await _to_review(hass)
    result = await _press(hass, result, "tuning")
    result = await _submit(hass, result, {CONF_DEMAND_THRESHOLD: float("nan")})

    assert result["step_id"] == "tuning"
    assert result["errors"] == {CONF_DEMAND_THRESHOLD: "not_a_number"}
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize("celsius", [False, True], ids=["fahrenheit", "celsius"])
async def test_boundary_engage_deadband_is_symmetric_in_both_units(
    hass: HomeAssistant, celsius: bool
) -> None:
    """Boundary + unit symmetry through the NEW flow, not the old one.

    The re-engage bounds are per-unit (0.5-5 °F, 0.25-2.5 °C). Both endpoints
    must be reachable and the value outside must not be, in either unit.
    """
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    low, high = unit_profile(celsius)["engage_bounds"]

    for value in (low, high):
        result = await _to_review(hass)
        result = await _press(hass, result, "tuning")
        result = await _submit(hass, result, {CONF_ENGAGE_DEADBAND: value})
        assert result["type"] is FlowResultType.MENU, (value, result)
        result = await _press(hass, result, "finish")
        assert result["options"][CONF_ENGAGE_DEADBAND] == pytest.approx(value)
        await hass.config_entries.async_remove(
            hass.config_entries.async_entries(DOMAIN)[0].entry_id
        )

    result = await _to_review(hass)
    result = await _press(hass, result, "tuning")
    with pytest.raises(Exception):  # noqa: B017 - HA wraps the schema rejection
        await _submit(hass, result, {CONF_ENGAGE_DEADBAND: high * 2})
    assert hass.config_entries.async_entries(DOMAIN) == []


# ---------- S12-S14: ownership ---------------------------------------------


async def test_s12_head_owned_by_another_entry_is_refused(
    hass: HomeAssistant,
) -> None:
    """S12 / W9: `heads_already_configured`, naming the head."""
    MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: [{ZONE_CLIMATE: BEDROOM, ZONE_SENSOR: BEDROOM_SENSOR}]},
        unique_id=f"{BEDROOM}|{SPARE}",
        version=2,
    ).add_to_hass(hass)

    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    assert result["step_id"] == "user"
    assert result["errors"] == {"heads": "heads_already_configured"}
    assert result["description_placeholders"]["conflicting_heads"] == BEDROOM


async def test_s13_head_reserved_by_another_open_flow_is_refused(
    hass: HomeAssistant,
) -> None:
    """S13 / W10: the reservation error, unchanged, on the picker."""
    first = await _start(hass)
    second = await _start(hass)
    first = await _submit(hass, first, {"heads": [LIVING, BEDROOM]})
    assert first["step_id"] == "rooms"

    second = await _submit(hass, second, {"heads": [BEDROOM, SPARE]})
    assert second["step_id"] == "user"
    assert second["errors"] == {"heads": "heads_reserved_by_flow"}
    assert second["description_placeholders"]["conflicting_heads"] == BEDROOM


async def test_s14_conflict_at_final_save_returns_and_releases_the_reservation(
    hass: HomeAssistant,
) -> None:
    """S14 / W11: the last-moment recheck, with M11's release."""
    result = await _to_review(hass)
    flow_id = result["flow_id"]
    MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: [{ZONE_CLIMATE: BEDROOM, ZONE_SENSOR: BEDROOM_SENSOR}]},
        unique_id=f"{BEDROOM}|{SPARE}",
        version=2,
    ).add_to_hass(hass)

    result = await _press(hass, result, "finish")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"heads": "heads_already_configured"}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    progress = next(
        item
        for item in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if item["flow_id"] == flow_id
    )
    assert "mxz_selected_heads" not in progress["context"]


# ---------- S15-S17: capability --------------------------------------------


async def test_capability_lost_while_reading_the_summary_blocks_the_save(
    hass: HomeAssistant,
) -> None:
    """S14 sibling / W11: capability is re-asked at the save, like ownership.

    A head can stop advertising heat and cool while the summary is on screen —
    an integration reloads, a device drops off. The recheck M12 put on the old
    final step has to reach the new one, and it must release M11's reservation
    on the way out, or the abandoned flow keeps another one locked out.
    """
    result = await _to_review(hass)
    flow_id = result["flow_id"]
    _head(hass, BEDROOM, hvac_modes=["off", "cool", "fan_only"])

    result = await _press(hass, result, "finish")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"heads": "head_missing_heat_cool"}
    assert result["description_placeholders"]["unsupported_heads"] == BEDROOM
    assert hass.config_entries.async_entries(DOMAIN) == []
    progress = next(
        item
        for item in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if item["flow_id"] == flow_id
    )
    assert "mxz_selected_heads" not in progress["context"]



async def test_s15_head_without_heat_and_cool_is_refused(
    hass: HomeAssistant,
) -> None:
    """S15 / W12: `head_missing_heat_cool`, naming the head."""
    _head(hass, BEDROOM, hvac_modes=["off", "cool", "fan_only"])
    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})

    assert result["step_id"] == "user"
    assert result["errors"] == {"heads": "head_missing_heat_cool"}
    assert result["description_placeholders"]["unsupported_heads"] == BEDROOM


async def test_s16_unknown_capabilities_are_reported_as_unknown(
    hass: HomeAssistant,
) -> None:
    """S16 / W12a: nothing is assumed about a head HA is not describing."""
    _head(hass, BEDROOM, advertise=False)
    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})

    assert result["step_id"] == "user"
    assert result["errors"] == {"heads": "head_capabilities_unavailable"}
    assert result["description_placeholders"]["unsupported_heads"] == BEDROOM


async def test_s17_unsupported_idle_default_forces_the_advanced_screen(
    hass: HomeAssistant,
) -> None:
    """S17 / W13: no silent save of a parking mode the heads cannot do."""
    for entity_id in (LIVING, BEDROOM):
        _head(hass, entity_id, hvac_modes=["off", "cool", "heat"])

    result = await _to_review(hass)
    assert "needs your choice" in result["description_placeholders"]["summary"]

    result = await _press(hass, result, "finish")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tuning"
    assert result["errors"] == {CONF_IDLE_ACTION: "idle_action_unsupported"}
    assert hass.config_entries.async_entries(DOMAIN) == []

    result = await _submit(hass, result, {CONF_IDLE_ACTION: IDLE_ACTION_OFF})
    result = await _press(hass, result, "finish")
    assert result["options"][CONF_IDLE_ACTION] == IDLE_ACTION_OFF


# ---------- S18-S21: sensors ------------------------------------------------


async def test_s18_one_sensor_cannot_serve_two_rooms(hass: HomeAssistant) -> None:
    """S18 / W14: `duplicate_sensors`, naming the shared sensor."""
    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    result = await _submit(hass, result, {})
    result = await _submit(
        hass, result, {"sensor_1": HALL_SENSOR, "sensor_2": HALL_SENSOR}
    )

    assert result["step_id"] == "sensors"
    assert result["errors"] == {"base": "duplicate_sensors"}
    assert result["description_placeholders"]["problem_sensors"] == HALL_SENSOR
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_s19_unreadable_unit_is_refused(hass: HomeAssistant) -> None:
    """S19 / W14a: a genuinely unreadable unit, named back to the user."""
    _sensor(hass, BEDROOM_SENSOR, "42", "%")
    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    result = await _submit(hass, result, {})
    result = await _submit(
        hass, result, {"sensor_1": LIVING_SENSOR, "sensor_2": BEDROOM_SENSOR}
    )

    assert result["step_id"] == "sensors"
    assert result["errors"] == {"base": "sensor_unit_unsupported"}
    assert result["description_placeholders"]["problem_sensors"] == BEDROOM_SENSOR
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        ("293.15", "K", "68 °F"),
        ("20", "°C", "68 °F"),
        ("68", "°F", "68 °F"),
        ("68", "unset", "68 °F"),
        ("68", None, "68 °F"),
    ],
    ids=["kelvin", "celsius", "fahrenheit", "absent_unit", "explicit_null_unit"],
)
async def test_s19_control_supported_and_absent_units_are_accepted(
    hass: HomeAssistant, value: str, unit: Any, expected: str
) -> None:
    """S19 CONTROL / W14a: the flow must not be stricter than the reader.

    Accepted M13 reads any unit in HA's TemperatureConverter.VALID_UNITS and
    treats an absent OR explicitly null unit as the system unit. Refusing
    kelvin or a unitless template sensor here would reject sensors the
    coordinator reads correctly. The explicit-null case is checked before the
    non-string rejection below, following the accepted reader's own order.
    """
    _sensor(hass, BEDROOM_SENSOR, value, unit)
    result = await _to_review(hass)

    assert result["type"] is FlowResultType.MENU, result
    summary = result["description_placeholders"]["summary"]
    assert expected in summary, summary
    result = await _press(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("unit", [[], {}, 7], ids=["list", "dict", "int"])
async def test_s19_control_non_string_unit_is_refused(
    hass: HomeAssistant, unit: Any
) -> None:
    """S19 CONTROL / W14a: a malformed unit is rejected by type, not by lookup.

    The membership test itself would raise on an unhashable unit, which is why
    the accepted reader types it first; the flow has to do the same.
    """
    _sensor(hass, BEDROOM_SENSOR, "68", unit)
    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    result = await _submit(hass, result, {})
    result = await _submit(
        hass, result, {"sensor_1": LIVING_SENSOR, "sensor_2": BEDROOM_SENSOR}
    )

    assert result["step_id"] == "sensors"
    assert result["errors"] == {"base": "sensor_unit_unsupported"}


async def test_sensor_with_no_state_is_refused(hass: HomeAssistant) -> None:
    """W14 sibling: an entity Home Assistant has never heard of is permanent."""
    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    result = await _submit(hass, result, {})
    result = await _submit(
        hass, result, {"sensor_1": LIVING_SENSOR, "sensor_2": "sensor.does_not_exist"}
    )

    assert result["step_id"] == "sensors"
    assert result["errors"] == {"base": "sensor_missing"}
    assert (
        result["description_placeholders"]["problem_sensors"] == "sensor.does_not_exist"
    )


@pytest.mark.parametrize(
    ("state", "phrase"),
    [("unavailable", "unavailable"), ("banana", "not reporting a number")],
    ids=["unavailable", "non_numeric"],
)
async def test_s20_a_transient_sensor_problem_is_reported_not_blocked(
    hass: HomeAssistant, state: str, phrase: str
) -> None:
    """S20 / W15: transient conditions belong on the summary, not in the way.

    Blocking here would make a legitimate install impossible at the wrong hour,
    and the coordinator itself treats an unreadable room as temporarily
    neutral rather than fatal.
    """
    _sensor(hass, BEDROOM_SENSOR, state, "°F")
    result = await _to_review(hass)

    assert result["type"] is FlowResultType.MENU, result
    summary = result["description_placeholders"]["summary"]
    assert phrase in summary
    assert "makes no automatic demand" in summary
    assert "Go back to heads and rooms" in summary

    result = await _press(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_s21_cadence_is_unknown_and_no_cadence_field_exists(
    hass: HomeAssistant,
) -> None:
    """S21 / W5a, W6a: status, never a cutoff, and nothing stored about it."""
    result = await _start(hass)
    seen = list(_fields(result))
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    seen += _fields(result)
    result = await _submit(hass, result, {})
    seen += _fields(result)
    sensors_text = result["description_placeholders"]["rooms"]
    assert "1. Living Room" in sensors_text

    result = await _submit(
        hass, result, {"sensor_1": LIVING_SENSOR, "sensor_2": BEDROOM_SENSOR}
    )
    summary = result["description_placeholders"]["summary"]
    assert summary.count("Cadence: unknown — MXZ does not time out this sensor.") == 2
    assert "70.7 °F" in summary
    assert "ago" in summary

    result = await _press(hass, result, "tuning")
    seen += _fields(result)
    result = await _submit(hass, result, {})
    result = await _press(hass, result, "finish")

    forbidden = ("maximum_age", "expected_report_interval", "startup_grace", "evidence_basis")
    assert not [f for f in seen if any(word in str(f) for word in forbidden)]
    stored = {**result["data"], **result["options"]}
    assert not [key for key in stored if any(word in key for word in forbidden)]


# ---------- S22-S24: manual control, review, back navigation ----------------


async def test_s22_manual_control_paragraph_states_only_shipped_behaviour(
    hass: HomeAssistant,
) -> None:
    """S22 / W6b: Block A only. M24's durable holds are not built yet.

    Read from the shipped translation file, because that is what a user sees;
    asserting a Python string would prove nothing about the rendered copy.
    """
    import json
    from pathlib import Path

    import custom_components.mxz_coordinator as component

    strings = json.loads(
        (Path(component.__file__).parent / "strings.json").read_text("utf-8")
    )
    review = strings["config"]["step"]["review"]["description"]

    assert "MXZ owns each head's mode while a room is enabled." in review
    assert "knocked off the coordinated mode" in review
    assert '"Fan auto" switch off to hold' in review
    assert "turn that room's enable switch off" in review
    assert '"Coordinator enable" off' in review
    # Nothing here may promise the M24 semantics no code implements yet.
    for unbuilt in ("Resume automatic control", "wall remote", "survives a restart"):
        assert unbuilt not in review


async def test_s23_summary_is_complete_and_nothing_is_written_until_confirmed(
    hass: HomeAssistant,
) -> None:
    """S23 / W6, W6c: everything on one screen; closing it creates nothing."""
    result = await _to_review(
        hass, title="Upstairs MXZ", notify="notify.mobile_app_phone"
    )
    summary = result["description_placeholders"]["summary"]
    for expected in (
        "Outdoor unit: Upstairs MXZ",
        "Drift alerts: notify.mobile_app_phone",
        "Priority 1  Living Room",
        f"  Head:   {LIVING}",
        LIVING_SENSOR,
        "Priority 2  Bedroom",
        f"  Head:   {BEDROOM}",
        BEDROOM_SENSOR,
        "Comfort settings: defaults for °F (18 values). Idle: fan_only.",
    ):
        assert expected in summary, (expected, summary)
    assert hass.config_entries.async_entries(DOMAIN) == []

    # W6c: abandoning the flow. Whether closing the DIALOG deletes the flow is
    # a frontend behaviour nothing here exercises (M11 review R5, UNKNOWN);
    # this asserts only what the backend guarantees.
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_s24_going_back_keeps_my_answers(hass: HomeAssistant) -> None:
    """S24 / W6d: the review screen's back button is not a reset button."""
    result = await _to_review(
        hass,
        names={"room_name_1": "Lounge", "room_name_2": "Guest Room"},
        title="Upstairs MXZ",
    )
    result = await _press(hass, result, "user")

    assert result["step_id"] == "user"
    assert result["data_schema"]({})["heads"] == [LIVING, BEDROOM]
    assert _suggested(result, "entry_title") == "Upstairs MXZ"

    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    assert result["step_id"] == "rooms"
    assert _suggested(result, "room_name_1") == "Lounge"
    assert _suggested(result, "room_name_2") == "Guest Room"

    result = await _submit(hass, result, {})
    assert result["step_id"] == "sensors"
    assert _suggested(result, "sensor_1") == LIVING_SENSOR
    assert _suggested(result, "sensor_2") == BEDROOM_SENSOR


# ---------- S25-S28: migration and stored shape -----------------------------


def _v2_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ZONES: [
                {
                    ZONE_NAME: "Living Room",
                    ZONE_CLIMATE: LIVING,
                    ZONE_SENSOR: LIVING_SENSOR,
                    ZONE_VANE_VERTICAL: None,
                    ZONE_VANE_HORIZONTAL: None,
                    ZONE_STAGE_SENSOR: None,
                },
                {
                    ZONE_NAME: "Bedroom",
                    ZONE_CLIMATE: BEDROOM,
                    ZONE_SENSOR: BEDROOM_SENSOR,
                    ZONE_VANE_VERTICAL: None,
                    ZONE_VANE_HORIZONTAL: None,
                    ZONE_STAGE_SENSOR: None,
                },
            ],
            CONF_DEMAND_THRESHOLD: 3.0,
        },
        options={CONF_DEMAND_THRESHOLD: 3.0, CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY},
        title="MXZ Coordinator",
        unique_id=f"{LIVING}|{BEDROOM}",
        version=2,
    )
    entry.add_to_hass(hass)
    return entry


async def test_s25_an_existing_v2_entry_is_untouched_by_the_new_code(
    hass: HomeAssistant,
) -> None:
    """S25 / M1: setting up the revamped code changes nothing already stored."""
    entry = _v2_entry(hass)
    before = (deepcopy(dict(entry.data)), deepcopy(dict(entry.options)), entry.unique_id)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (dict(entry.data), dict(entry.options), entry.unique_id) == before
    assert entry.version == 2
    registry = er.async_get(hass)
    entities = {
        item.entity_id: item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert entities, "the entry produced no entities"

    # A reload with the same code must not churn identity either.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert {
        item.entity_id: item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == entities
    assert (dict(entry.data), dict(entry.options), entry.unique_id) == before


async def test_s25_a_v1_flat_entry_still_migrates_exactly_as_before(
    hass: HomeAssistant,
) -> None:
    """S25 / M2: the v1 -> v2 conversion is the pinned one, not a new one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PRIMARY_CLIMATE: LIVING,
            CONF_SECONDARY_CLIMATE: BEDROOM,
            CONF_PRIMARY_SENSOR: LIVING_SENSOR,
            CONF_SECONDARY_SENSOR: BEDROOM_SENSOR,
        },
        options={CONF_DEMAND_THRESHOLD: 3.0},
        unique_id=f"{LIVING}|{BEDROOM}",
        version=1,
    )
    entry.add_to_hass(hass)
    options_before = deepcopy(dict(entry.options))

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    assert entry.unique_id == f"{LIVING}|{BEDROOM}"
    assert dict(entry.options) == options_before
    zones = entry.data[CONF_ZONES]
    assert [zone[ZONE_CLIMATE] for zone in zones] == [LIVING, BEDROOM]
    assert [zone[ZONE_SENSOR] for zone in zones] == [LIVING_SENSOR, BEDROOM_SENSOR]
    # No key this node invented appears anywhere in the migrated entry.
    assert not [key for key in entry.data if key.startswith("room_name_")]
    assert "entry_title" not in entry.data


async def test_m3_configure_form_is_unchanged_for_an_existing_entry(
    hass: HomeAssistant,
) -> None:
    """M3: Configure is still one form. Progressive disclosure is setup-only."""
    entry = _v2_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    fields = set(_fields(result))
    assert _TUNING_KEYS <= fields
    assert "primary_vane_vertical" in fields
    assert "review" not in fields


async def test_s27_no_setup_only_input_leaks_into_stored_configuration(
    hass: HomeAssistant,
) -> None:
    """S27 / W7a: room names and the entry title are flow input, never keys."""
    result = await _to_review(
        hass, names={"room_name_1": "Lounge"}, title="Upstairs MXZ"
    )
    result = await _press(hass, result, "finish")

    assert result["title"] == "Upstairs MXZ"
    stored = {**result["data"], **result["options"]}
    assert not [key for key in stored if key.startswith("room_name_")]
    assert "entry_title" not in stored
    allowed = {
        ZONE_NAME,
        ZONE_CLIMATE,
        ZONE_SENSOR,
        ZONE_VANE_VERTICAL,
        ZONE_VANE_HORIZONTAL,
        ZONE_STAGE_SENSOR,
    }
    for zone in result["data"][CONF_ZONES]:
        assert set(zone) == allowed, set(zone) - allowed
    assert result["data"][CONF_ZONES][0][ZONE_NAME] == "Lounge"
    assert CONF_NOTIFY_SERVICE not in result["data"]


@pytest.mark.parametrize("celsius", [False, True], ids=["fahrenheit", "celsius"])
async def test_s28_no_safety_default_moves(
    hass: HomeAssistant, celsius: bool
) -> None:
    """S28 / W7b: the values a fresh install saves are the pinned profile's."""
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    options = await _skip_advanced_options(hass)
    profile = dict(unit_profile(celsius)["defaults"])

    assert options[CONF_IDLE_ACTION] == IDLE_ACTION_FAN_ONLY
    assert options[CONF_MODE_HYSTERESIS] == 600
    assert options[CONF_FAN_BOOST_ENABLE] is True
    assert options[CONF_INHIBIT_ACTION] == "eco"
    for key in (
        CONF_CLAMP_MIN,
        CONF_CLAMP_MAX,
        CONF_HEAT_LOCKOUT_FLOOR,
        CONF_COOL_LOCKOUT_CEILING,
    ):
        assert options[key] == pytest.approx(float(profile[key])), key
    assert not [
        key
        for key in options
        if "maximum_age" in key or "max_age" in key or "stale" in key
    ]

    # The flow's own create already set the entry up; just let it settle.
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    registry = er.async_get(hass)
    enables = {
        item.unique_id.removeprefix(f"{entry.entry_id}_"): item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.unique_id.endswith("_enable")
        or item.unique_id.endswith("coordinator_enable")
    }
    assert {"coordinator_enable", "primary_enable", "secondary_enable"} <= set(
        enables
    ), enables
    for entity_id in enables.values():
        assert hass.states.get(entity_id).state == "off", entity_id


# ---------- reconfigure: R1-R8 ----------------------------------------------


async def test_r1_r4_reconfigure_walks_rooms_sensors_and_a_review_with_no_advanced(
    hass: HomeAssistant,
) -> None:
    """R1-R4: the same four screens, prefilled, and no second tuning path."""
    entry = _v2_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["step_id"] == "reconfigure"
    assert result["data_schema"]({})["heads"] == [LIVING, BEDROOM]
    assert _suggested(result, "entry_title") == "MXZ Coordinator"

    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    assert result["step_id"] == "reconfigure_rooms"
    assert _suggested(result, "room_name_1") == "Living Room"
    assert _suggested(result, "room_name_2") == "Bedroom"

    result = await _submit(hass, result, {})
    assert result["step_id"] == "reconfigure_sensors"
    assert _suggested(result, "sensor_1") == LIVING_SENSOR
    assert not [f for f in _fields(result) if str(f).startswith("room_name_")]

    result = await _submit(
        hass, result, {"sensor_1": LIVING_SENSOR, "sensor_2": BEDROOM_SENSOR}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reconfigure_review"
    assert result["menu_options"] == ["reconfigure_finish", "reconfigure"]
    assert "tuning" not in result["menu_options"]


async def test_reconfigure_can_rename_the_outdoor_unit(hass: HomeAssistant) -> None:
    """M27 review r2 O4: the title is editable where it was first set."""
    entry = _v2_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    result = await _submit(
        hass, result, {"heads": [LIVING, BEDROOM], "entry_title": "Upstairs MXZ"}
    )
    result = await _submit(hass, result, {})
    result = await _submit(
        hass, result, {"sensor_1": LIVING_SENSOR, "sensor_2": BEDROOM_SENSOR}
    )
    result = await _press(hass, result, "reconfigure_finish")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Upstairs MXZ"
    assert entry.unique_id == f"{LIVING}|{BEDROOM}"


async def test_reconfigure_blank_name_returns_to_the_head_name(
    hass: HomeAssistant,
) -> None:
    """M30 semantics preserved: on an existing entry, empty means "the head".

    Setup refuses a blank name because the room does not exist yet; reconfigure
    reads it as a reset, which is what its help text has always promised.
    """
    entry = _v2_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    result = await _submit(hass, result, {"room_name_1": "  "})
    assert result["step_id"] == "reconfigure_sensors", result

    result = await _submit(
        hass, result, {"sensor_1": LIVING_SENSOR, "sensor_2": BEDROOM_SENSOR}
    )
    result = await _press(hass, result, "reconfigure_finish")
    assert entry.data[CONF_ZONES][0][ZONE_NAME] == "Living Room"


async def test_r8_reconfigure_rejects_another_entrys_head_and_changes_nothing(
    hass: HomeAssistant,
) -> None:
    """R8: the grandfathered heads survive the rejection untouched."""
    entry = _v2_entry(hass)
    MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ZONES: [{ZONE_CLIMATE: SPARE, ZONE_SENSOR: HALL_SENSOR}]},
        unique_id=f"{SPARE}|climate.other",
        version=2,
    ).add_to_hass(hass)
    before = (deepcopy(dict(entry.data)), deepcopy(dict(entry.options)), entry.unique_id)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    result = await _submit(hass, result, {"heads": [LIVING, SPARE]})

    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"heads": "heads_already_configured"}
    assert (dict(entry.data), dict(entry.options), entry.unique_id) == before


# ---------- negative controls on what the revamp removed --------------------


async def test_the_old_three_screen_path_is_gone_for_setup(
    hass: HomeAssistant,
) -> None:
    """The tuning step no longer creates the entry, and is not on the way in.

    The design removes exactly this and nothing else: the OPTIONS flow still
    submits the same twenty keys in one form (see the M3 test above).
    """
    result = await _start(hass)
    result = await _submit(hass, result, {"heads": [LIVING, BEDROOM]})
    # The old flow went straight to sensors here.
    assert result["step_id"] != "sensors"

    result = await _submit(hass, result, {})
    result = await _submit(
        hass, result, {"sensor_1": LIVING_SENSOR, "sensor_2": BEDROOM_SENSOR}
    )
    # The old flow showed the twenty-field form here and created on submit.
    assert result["type"] is not FlowResultType.FORM

    result = await _press(hass, result, "tuning")
    result = await _submit(hass, result, {})
    assert result["type"] is not FlowResultType.CREATE_ENTRY
    assert result["step_id"] == "review"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_a_changed_head_selection_drops_the_answers_keyed_to_it(
    hass: HomeAssistant,
) -> None:
    """Going back and choosing different heads must not reuse the old rooms."""
    result = await _to_review(hass, names={"room_name_1": "Lounge"})
    result = await _press(hass, result, "user")
    result = await _submit(hass, result, {"heads": [LIVING, SPARE]})

    assert result["step_id"] == "rooms"
    assert _suggested(result, "room_name_1") == "Living Room"
    assert _suggested(result, "room_name_2") == "Spare"


async def test_a_room_name_of_only_whitespace_is_stored_stripped(
    hass: HomeAssistant,
) -> None:
    """Names are stripped before they are compared and before they are stored."""
    result = await _to_review(hass, names={"room_name_1": "  Lounge  "})
    result = await _press(hass, result, "finish")
    assert result["data"][CONF_ZONES][0][ZONE_NAME] == "Lounge"


def test_every_new_error_key_exists_in_both_string_files() -> None:
    """Both files carry every key this flow can raise, and stay identical.

    hassfest and HACS are the only CI jobs that structurally validate these
    files, and they do not run here; this is the local guard.
    """
    import json
    from pathlib import Path

    import custom_components.mxz_coordinator as component

    folder = Path(component.__file__).parent
    raw = (folder / "strings.json").read_bytes()
    assert raw == (folder / "translations" / "en.json").read_bytes()

    strings = json.loads(raw.decode("utf-8"))
    errors = strings["config"]["error"]
    for key in (
        "duplicate_sensors",
        "sensor_missing",
        "sensor_unit_unsupported",
        "room_name_empty",
        "duplicate_room_names",
    ):
        assert key in errors, key
    steps = strings["config"]["step"]
    for step in ("rooms", "review", "reconfigure_rooms", "reconfigure_review"):
        assert step in steps, step
    assert set(steps["review"]["menu_options"]) == {"finish", "tuning", "user"}
    assert set(steps["reconfigure_review"]["menu_options"]) == {
        "reconfigure_finish",
        "reconfigure",
    }
    # Every placeholder a step's copy uses must be one the flow actually sends.
    assert "{summary}" in steps["review"]["description"]
    assert "{rooms}" in steps["rooms"]["description"]
    assert "{rooms}" in steps["sensors"]["description"]
