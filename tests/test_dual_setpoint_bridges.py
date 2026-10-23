"""A dual-setpoint room through HA's own bridge code (DESIGN.md section 6).

Each test drives the real code each bridge runs in the installed Home
Assistant: Google's ``TemperatureSettingTrait``, Alexa's thermostat
capability and ``SetTargetTemperature`` / ``AdjustTargetTemperature``
handlers, and Assist's ``HassClimateSetTemperature`` intent. None is
re-implemented here.

HomeKit is not tested: HA's HomeKit bridge needs HAP-python (``pyhap``),
which no lane installs, so its behavior is reasoned from
``homekit/type_thermostats.py`` in DONE.md instead.

Workaround, declared: on HA 2025.x and later, ``google_assistant`` and
``alexa`` import ``camera``, whose ``img_util`` imports ``turbojpeg`` at module
load (it is only used to resize snapshots). Where ``turbojpeg`` is not
installed, an empty stand-in module is registered for that one import and
removed again, so the bridges' thermostat code can be imported; nothing here
touches a camera. Where an import still fails, the test is skipped with the
reason, never passed.

Requires pytest-homeassistant-custom-component (Python 3.12+).
"""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import intent

from tests.test_dual_setpoint import (
    _compute,
    _eid,
    _pair,
    _range,
    _setup,
    _state,
)


def _import(name: str):
    """Import an HA bridge module, standing in for a missing turbojpeg only."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as err:
        if err.name != "turbojpeg":
            pytest.skip(f"{name} can't be imported here: {err}")
    stand_in = types.ModuleType("turbojpeg")
    stand_in.TurboJPEG = object  # img_util only names it at import
    sys.modules["turbojpeg"] = stand_in
    try:
        return importlib.import_module(name)
    except ImportError as err:  # pragma: no cover - depends on the lane
        pytest.skip(f"{name} can't be imported here: {err}")
    finally:
        sys.modules.pop("turbojpeg", None)


# --- Google Home ---------------------------------------------------------------
class _GoogleConfig:
    """The two things the trait reads from its config."""

    def __init__(self, report_state: bool) -> None:
        self.should_report_state = report_state


def _google(hass: HomeAssistant, entry, slug: str, report_state: bool = False):
    trait = _import("homeassistant.components.google_assistant.trait")
    state = _state(hass, entry, f"{slug}_thermostat")
    return trait, trait.TemperatureSettingTrait(hass, state, _GoogleConfig(report_state))


async def test_google_reports_heatcool_with_the_range(hass: HomeAssistant) -> None:
    entry, _ = await _setup(hass, dual=(0,))
    await _range(hass, entry, "primary", 68, 74)
    trait_mod, trait = _google(hass, entry, "primary")
    features = _state(hass, entry, "primary_thermostat").attributes["supported_features"]
    assert trait_mod.TemperatureSettingTrait.supported("climate", features, None, None)
    sync = trait.sync_attributes()
    assert "heatcool" in sync["availableThermostatModes"]
    assert "off" in sync["availableThermostatModes"]
    assert sync["thermostatTemperatureUnit"] == "F"
    # A disabled room reads off; turn it on (heat_cool) for the setpoints.
    await hass.services.async_call(
        "climate", "turn_on", {"entity_id": _eid(hass, entry, "primary_thermostat")}, blocking=True
    )
    _, trait = _google(hass, entry, "primary")
    query = trait.query_attributes()
    assert query["thermostatMode"] == "heatcool"
    assert query["thermostatTemperatureSetpointLow"] == 20.0  # 68 °F
    assert query["thermostatTemperatureSetpointHigh"] == 23.3  # 74 °F
    assert "thermostatTemperatureSetpoint" not in query


async def test_google_single_target_room_is_unchanged(hass: HomeAssistant) -> None:
    entry, _ = await _setup(hass, dual=(0,))
    await hass.services.async_call(
        "climate", "turn_on", {"entity_id": _eid(hass, entry, "secondary_thermostat")}, blocking=True
    )
    _, trait = _google(hass, entry, "secondary")
    query = trait.query_attributes()
    # heat_cool with one target: HA reports it as both ends, as in 3.9.0.
    assert query["thermostatTemperatureSetpointLow"] == query["thermostatTemperatureSetpointHigh"] == 21.1


async def test_google_set_range_lands_as_one_pair(hass: HomeAssistant) -> None:
    entry, _ = await _setup(hass, dual=(0,))
    trait_mod, trait = _google(hass, entry, "primary")
    data = SimpleNamespace(context=Context())
    await trait.execute(
        trait_mod.COMMAND_THERMOSTAT_TEMPERATURE_SET_RANGE, data,
        {"thermostatTemperatureSetpointLow": 19.0, "thermostatTemperatureSetpointHigh": 24.0}, {},
    )
    await hass.async_block_till_done()
    assert _pair(entry) == (66.0, 75.0)  # 66.2 and 75.2 °F, rounded by the trait
    # Too narrow: refused by MXZ, and nothing changes.
    _, trait = _google(hass, entry, "primary")
    with pytest.raises(ServiceValidationError) as err:
        await trait.execute(
            trait_mod.COMMAND_THERMOSTAT_TEMPERATURE_SET_RANGE, data,
            {"thermostatTemperatureSetpointLow": 21.0, "thermostatTemperatureSetpointHigh": 21.5}, {},
        )
    assert err.value.translation_key == "range_too_narrow"
    assert _pair(entry) == (66.0, 75.0)


async def test_google_single_setpoint_on_a_range_room_is_refused_by_ha(
    hass: HomeAssistant,
) -> None:
    """What HA's trait does with a spoken single number for a range-only room.

    It forwards it as ``temperature``, and HA's climate service refuses that
    for an entity without TARGET_TEMPERATURE. Nothing is converted into a
    range and nothing changes.
    """
    entry, _ = await _setup(hass, dual=(0,))
    trait_mod, trait = _google(hass, entry, "primary")
    with pytest.raises(ServiceValidationError) as err:
        await trait.execute(
            trait_mod.COMMAND_THERMOSTAT_TEMPERATURE_SETPOINT,
            SimpleNamespace(context=Context()), {"thermostatTemperatureSetpoint": 22.0}, {},
        )
    assert err.value.translation_key == "missing_target_temperature_entity_feature"
    assert _pair(entry) == (69.0, 71.0)


# --- Alexa ---------------------------------------------------------------------
class _AlexaConfig:
    """What the directive and the climate adapter read from Alexa's config."""

    def __init__(self) -> None:
        self.entity_config: dict = {}
        self.locale = "en-US"

    def should_expose(self, entity_id: str) -> bool:
        return True


def _directive(state_report, entity_id: str, name: str, payload: dict):
    return state_report.AlexaDirective(
        {
            "directive": {
                "header": {
                    "namespace": "Alexa.ThermostatController", "name": name,
                    "messageId": "m1", "correlationToken": "t1", "payloadVersion": "3",
                },
                "endpoint": {"endpointId": entity_id.replace(".", "#"), "cookie": {}},
                "payload": payload,
            }
        }
    )


async def test_alexa_reports_auto_with_lower_and_upper_setpoints(hass: HomeAssistant) -> None:
    capabilities = _import("homeassistant.components.alexa.capabilities")
    entry, _ = await _setup(hass, dual=(0,))
    await _range(hass, entry, "primary", 68, 74)
    await hass.services.async_call(
        "climate", "turn_on", {"entity_id": _eid(hass, entry, "primary_thermostat")}, blocking=True
    )
    controller = capabilities.AlexaThermostatController(
        hass, _state(hass, entry, "primary_thermostat")
    )
    names = {p["name"] for p in controller.properties_supported()}
    assert {"lowerSetpoint", "upperSetpoint", "thermostatMode"} <= names
    assert "targetSetpoint" not in names
    assert controller.get_property("lowerSetpoint") == {"value": 68.0, "scale": "FAHRENHEIT"}
    assert controller.get_property("upperSetpoint") == {"value": 74.0, "scale": "FAHRENHEIT"}
    assert controller.get_property("thermostatMode") == "AUTO"


async def test_alexa_set_and_adjust_move_the_range(hass: HomeAssistant) -> None:
    handlers = _import("homeassistant.components.alexa.handlers")
    state_report = _import("homeassistant.components.alexa.state_report")
    entry, _ = await _setup(hass, dual=(0,))
    eid = _eid(hass, entry, "primary_thermostat")
    config = _AlexaConfig()

    directive = _directive(state_report, eid, "SetTargetTemperature", {
        "lowerSetpoint": {"value": 67.0, "scale": "FAHRENHEIT"},
        "upperSetpoint": {"value": 76.0, "scale": "FAHRENHEIT"},
    })
    directive.load_entity(hass, config)
    await handlers.async_api_set_target_temp(hass, config, directive, Context())
    await hass.async_block_till_done()
    assert _pair(entry) == (67.0, 76.0)

    # "Make it two degrees warmer": both edges move, the width is kept.
    directive = _directive(state_report, eid, "AdjustTargetTemperature", {
        "targetSetpointDelta": {"value": 2.0, "scale": "FAHRENHEIT"},
    })
    directive.load_entity(hass, config)
    await handlers.async_api_adjust_target_temp(hass, config, directive, Context())
    await hass.async_block_till_done()
    assert _pair(entry) == (69.0, 78.0)


async def test_alexa_lone_target_setpoint_on_a_range_room_changes_nothing(
    hass: HomeAssistant,
) -> None:
    """Alexa's handler calls the service without waiting (blocking=False), so it
    answers before HA refuses the lone ``temperature``; the range stays put."""
    handlers = _import("homeassistant.components.alexa.handlers")
    state_report = _import("homeassistant.components.alexa.state_report")
    entry, _ = await _setup(hass, dual=(0,))
    eid = _eid(hass, entry, "primary_thermostat")
    config = _AlexaConfig()
    directive = _directive(state_report, eid, "SetTargetTemperature", {
        "targetSetpoint": {"value": 72.0, "scale": "FAHRENHEIT"},
    })
    directive.load_entity(hass, config)
    response = await handlers.async_api_set_target_temp(hass, config, directive, Context())
    await hass.async_block_till_done()
    assert response.name == "Response"
    assert _pair(entry) == (69.0, 71.0)


# --- Assist (HA voice) -----------------------------------------------------------
async def test_assist_set_temperature_matches_only_single_target_rooms(hass: HomeAssistant) -> None:
    climate_intent = importlib.import_module("homeassistant.components.climate.intent")
    if not hasattr(climate_intent, "SetTemperatureIntent"):
        pytest.skip("this HA has no HassClimateSetTemperature intent (added after 2024.12)")
    entry, _ = await _setup(hass, dual=(0,))
    await climate_intent.async_setup_intents(hass)
    living = _state(hass, entry, "primary_thermostat").name
    bedroom = _state(hass, entry, "secondary_thermostat").name
    with pytest.raises(intent.MatchFailedError):
        await intent.async_handle(
            hass, "test", climate_intent.INTENT_SET_TEMPERATURE,
            {"name": {"value": living}, "temperature": {"value": 72}},
        )
    assert _pair(entry) == (69.0, 71.0)
    await intent.async_handle(
        hass, "test", climate_intent.INTENT_SET_TEMPERATURE,
        {"name": {"value": bedroom}, "temperature": {"value": 72}},
    )
    await _compute(hass, entry)
    assert entry.runtime_data.zones[1].target == 72
