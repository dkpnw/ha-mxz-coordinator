"""Uneven drift: "Let it get cooler by" / "Let it get warmer by" (Comfort › Advanced).

Two optional global options split Allowed drift by side: how far below target
a room may get before heat starts, and how far above before cool starts. Blank
means Allowed drift, so an install that never sets them runs exactly as 3.7.1.
A room's own drift number replaces both. Every run still ends at the target.
Fake heads and sensors only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.mxz_coordinator as component
from custom_components.mxz_coordinator.const import (
    CONF_ENGAGE_DEADBAND,
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
)
from tests.dwell import expire_dwell
from tests.flow_pages import (
    default,
    marker_of,
    open_options_page,
    prefilled,
    press,
    sections,
    setup_tuning,
    submit,
    suggested,
)
from tests.test_configure_menu import HEADS, SENSORS, _entry, _head, _setup_review
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_target,
    _set_temp,
    _setup_mock_heads,
)
from tests.test_issue18_drift import _set_drift
from tests.test_m29_follow_global import _press_follow_global

# Stored keys, written out independently of const.py.
COOLER = "drift_cooler_by"
WARMER = "drift_warmer_by"


async def _setup(hass: HomeAssistant, stored: dict[str, Any] | None = None):
    """Two rooms, coordinator on, primary target 62 °F, secondary 70 °F."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 62)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: True,
            **(stored or {}),
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 62)
    await _recompute(hass, entry)
    return entry


def _zones(hass: HomeAssistant, entry) -> list[dict[str, Any]]:
    return hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"]


async def _at(hass: HomeAssistant, entry, temp: float) -> dict[str, Any]:
    """Move the primary room to ``temp``, recompute, return its plan row."""
    await _set_temp(hass, SENSOR_A, temp)
    await _recompute(hass, entry)
    return _zones(hass, entry)[0]


# --- both blank equals today -----------------------------------------------

# Primary target 62, Allowed drift 1, mode switch threshold 3 (the defaults):
# engage starts past 63 / below 61 and runs to 62; demand past 65 / below 59.
# Written out by hand from the 3.7.1 rule, as (temp, engage, demand).
_TODAY = (
    (62.0, "satisfied", "neutral"),
    (63.0, "satisfied", "neutral"),
    (63.5, "cool", "neutral"),
    (64.0, "cool", "neutral"),
    (62.5, "cool", "neutral"),
    (62.0, "satisfied", "neutral"),
    (61.0, "satisfied", "neutral"),
    (60.5, "heat", "neutral"),
    (59.0, "heat", "neutral"),
    (58.5, "heat", "heat"),
    (62.0, "satisfied", "neutral"),
    (65.5, "cool", "cool"),
)


@pytest.mark.parametrize(
    "stored",
    [{}, {COOLER: 1.0, WARMER: 1.0}, {CONF_ENGAGE_DEADBAND: 1.0, COOLER: 1.0}],
    ids=["both-blank", "both-equal-allowed-drift", "one-equal-one-blank"],
)
async def test_blank_or_equal_sides_reproduce_today(
    hass: HomeAssistant, stored: dict[str, Any]
) -> None:
    """Both blank is 3.7.1 exactly, through starts, runs to target and demand,
    and so is setting a side to Allowed drift."""
    entry = await _setup(hass, stored)
    seen = []
    for temp, _, _ in _TODAY:
        row = await _at(hass, entry, temp)
        seen.append((temp, row["engage"], row["demand"]))
        assert row["drift"] == row["drift_heat"] == row["drift_cool"] == 1.0
    assert tuple(seen) == _TODAY


async def test_the_today_sweep_notices_a_changed_side(hass: HomeAssistant) -> None:
    """Negative control for the sweep: warmer by 2 changes its answers."""
    entry = await _setup(hass, {WARMER: 2.0})
    seen = []
    for temp, _, _ in _TODAY:
        row = await _at(hass, entry, temp)
        seen.append((temp, row["engage"], row["demand"]))
    assert tuple(seen) != _TODAY
    assert seen[2] == (63.5, "satisfied", "neutral")  # inside warmer-by 2


# --- each side starts on its own drift; every run ends at target -------------


async def test_each_side_starts_on_its_own_drift_and_runs_to_target(
    hass: HomeAssistant,
) -> None:
    """Target 62, cooler by 3, warmer by 5: heat below 59, cool above 67,
    both running back to 62."""
    entry = await _setup(hass, {COOLER: 3.0, WARMER: 5.0})
    row = await _at(hass, entry, 66.5)
    assert (row["engage"], row["demand"]) == ("satisfied", "neutral")
    assert (row["drift"], row["drift_heat"], row["drift_cool"]) == (1.0, 3.0, 5.0)
    row = await _at(hass, entry, 67.5)
    assert (row["engage"], row["demand"]) == ("cool", "cool")
    row = await _at(hass, entry, 63.0)
    assert (row["engage"], row["demand"]) == ("cool", "neutral")  # still running
    row = await _at(hass, entry, 62.0)
    assert row["engage"] == "satisfied"  # ends at the target, not at 67

    row = await _at(hass, entry, 59.5)
    assert (row["engage"], row["demand"]) == ("satisfied", "neutral")
    row = await _at(hass, entry, 58.5)
    assert (row["engage"], row["demand"]) == ("heat", "heat")
    row = await _at(hass, entry, 61.5)
    assert row["engage"] == "heat"  # still running
    row = await _at(hass, entry, 62.0)
    assert row["engage"] == "satisfied"  # ends at the target, not at 59

    # The global values apply to every room without its own drift.
    other = _zones(hass, entry)[1]
    assert (other["drift"], other["drift_heat"], other["drift_cool"]) == (1.0, 3.0, 5.0)


@pytest.mark.parametrize(
    ("stored", "temp", "engage"),
    [
        ({COOLER: 3.0}, 60.5, "satisfied"),  # inside cooler-by 3
        ({COOLER: 3.0}, 63.5, "cool"),  # warmer side stays Allowed drift 1
        ({WARMER: 4.0}, 65.5, "satisfied"),  # inside warmer-by 4
        ({WARMER: 4.0}, 60.5, "heat"),  # cooler side stays Allowed drift 1
    ],
)
async def test_only_one_side_set(
    hass: HomeAssistant, stored: dict[str, Any], temp: float, engage: str
) -> None:
    entry = await _setup(hass, stored)
    assert (await _at(hass, entry, temp))["engage"] == engage


@pytest.mark.parametrize(
    ("stored", "temp", "engage", "demand"),
    [
        # A side under the threshold: the room runs, the shared mode doesn't
        # move for it until the threshold (max(3, 1.5) = 3).
        ({COOLER: 1.5}, 60.4, "heat", "neutral"),
        ({COOLER: 1.5}, 58.9, "heat", "heat"),
        # A side over the threshold widens that side's demand (max(3, 4) = 4);
        # today's 3 would call cool at 65.5.
        ({WARMER: 4.0}, 65.5, "satisfied", "neutral"),
        ({WARMER: 4.0}, 66.1, "cool", "cool"),
        # ...and only that side: heat demand stays at the threshold.
        ({WARMER: 4.0}, 58.9, "heat", "heat"),
    ],
)
async def test_demand_band_is_the_larger_of_threshold_and_side_drift(
    hass: HomeAssistant,
    stored: dict[str, Any],
    temp: float,
    engage: str,
    demand: str,
) -> None:
    entry = await _setup(hass, stored)
    row = await _at(hass, entry, temp)
    assert (row["engage"], row["demand"]) == (engage, demand)


async def test_a_wide_warmer_by_does_not_steer_the_shared_mode(
    hass: HomeAssistant,
) -> None:
    """A room inside its warmer-by casts no cool vote, so the room that wants
    heat gets the system (the #18 standoff rule, per side)."""
    entry = await _setup(hass, {WARMER: 4.0})
    await _set_temp(hass, SENSOR_B, 65)  # 5 below its 70 target: heat
    expire_dwell(entry.runtime_data)
    row = await _at(hass, entry, 65.5)  # 3.5 warm: over 3, inside 4
    assert row["demand"] == "neutral"
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "heat"


# --- a room's own drift replaces both; Follow global drift hands it back ----


async def test_room_drift_overrides_both_sides_and_follow_global_restores_them(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass, {COOLER: 3.0, WARMER: 5.0})
    coord = entry.runtime_data
    zone = coord.zones[0]
    assert coord.zone_drift_sides(zone) == (3.0, 5.0)

    await _set_drift(hass, entry, 2.0)
    assert coord.zone_drift_sides(zone) == (2.0, 2.0)
    row = await _at(hass, entry, 64.5)  # past 2: inside the global warmer-by 5
    assert (row["drift"], row["drift_heat"], row["drift_cool"]) == (2.0, 2.0, 2.0)
    assert row["engage"] == "cool"
    assert (await _at(hass, entry, 62.0))["engage"] == "satisfied"
    assert (await _at(hass, entry, 59.9))["engage"] == "heat"  # past 2, inside 3
    assert (await _at(hass, entry, 62.0))["engage"] == "satisfied"
    # The other room keeps the uneven globals.
    assert coord.zone_drift_sides(coord.zones[1]) == (3.0, 5.0)

    await _press_follow_global(hass, entry)
    assert coord.zone_drift_sides(zone) == (3.0, 5.0)
    row = await _at(hass, entry, 64.5)
    assert (row["drift"], row["drift_heat"], row["drift_cool"]) == (1.0, 3.0, 5.0)
    assert row["engage"] == "satisfied"
    assert (await _at(hass, entry, 59.5))["engage"] == "satisfied"
    assert (await _at(hass, entry, 58.9))["engage"] == "heat"


# --- stored values are clamped on load, like Allowed drift -------------------


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ({COOLER: 0.0, WARMER: 40.0}, (0.5, 5.0)),
        ({COOLER: -2.0, WARMER: 5.5}, (0.5, 5.0)),
        ({COOLER: 2.5, WARMER: 4.0}, (2.5, 4.0)),
        ({COOLER: 0.5, WARMER: 5.0}, (0.5, 5.0)),
        ({COOLER: "3", WARMER: 7}, (3.0, 5.0)),  # read like Allowed drift
        # Not a number: blank (Allowed drift), never a failed setup.
        ({COOLER: "warm", WARMER: float("nan")}, (None, None)),
        ({COOLER: float("inf"), WARMER: "-inf"}, (None, None)),
        # Documents the float() read Allowed drift uses, not an endorsed input.
        ({COOLER: True}, (1.0, None)),
        ({COOLER: None}, (None, None)),
    ],
)
async def test_stored_sides_are_clamped_on_load(
    hass: HomeAssistant, stored: dict[str, Any], expected: tuple
) -> None:
    entry = await _setup(hass, stored)
    coord = entry.runtime_data
    assert (coord.drift_cooler_by, coord.drift_warmer_by) == expected
    in_use = tuple(coord.engage_deadband if side is None else side for side in expected)
    assert coord.zone_drift_sides(coord.zones[0]) == in_use
    row = _zones(hass, entry)[0]
    assert (row["drift_heat"], row["drift_cool"]) == in_use


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ({COOLER: 0.1, WARMER: 9.0}, (0.25, 2.5)),
        ({COOLER: 1.5, WARMER: 2.0}, (1.5, 2.0)),
        ({}, (0.5, 0.5)),  # blank: the °C Allowed drift default
    ],
)
async def test_celsius_sides_clamp_to_celsius_bounds(
    hass: HomeAssistant, stored: dict[str, Any], expected: tuple
) -> None:
    hass.config.units = METRIC_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 21)
    await _set_temp(hass, SENSOR_B, 21)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            **stored,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coord = entry.runtime_data
    assert coord.zone_drift_sides(coord.zones[0]) == expected


# --- the options page ---------------------------------------------------------


@pytest.fixture
def house(hass: HomeAssistant):
    def build(celsius: bool = False) -> None:
        hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
        unit = "°C" if celsius else "°F"
        for head in HEADS:
            _head(hass, head)
        for sensor in SENSORS:
            hass.states.async_set(
                sensor, "21" if celsius else "70",
                {"device_class": "temperature", "unit_of_measurement": unit},
            )

    return build


def _stores(entry) -> tuple[dict[str, Any], dict[str, Any]]:
    return dict(entry.data), dict(entry.options)


@pytest.mark.parametrize(
    ("celsius", "bounds", "unit"),
    [(False, (0.5, 5.0), "°F"), (True, (0.25, 2.5), "°C")],
    ids=["fahrenheit", "celsius"],
)
async def test_comfort_advanced_shows_both_blank_with_drift_bounds(
    hass: HomeAssistant, house, celsius: bool, bounds: tuple, unit: str
) -> None:
    house(celsius)
    entry = _entry(hass)
    result = await open_options_page(hass, entry, "comfort")
    advanced = sections(result)["advanced"]
    assert advanced.options["collapsed"] is True
    inner = [marker.schema for marker in advanced.schema.schema]
    assert COOLER in inner and WARMER in inner
    top = [marker.schema for marker in result["data_schema"].schema]
    assert COOLER not in top and WARMER not in top
    _, allowed = marker_of(result, CONF_ENGAGE_DEADBAND)
    for key in (COOLER, WARMER):
        assert default(result, key) is None
        assert suggested(result, key) is None
        _, box = marker_of(result, key)
        assert (box.config["min"], box.config["max"]) == bounds
        assert box.config["unit_of_measurement"] == unit
        # The same box Allowed drift uses.
        assert box.config == allowed.config
        assert key not in prefilled(result)


async def test_an_untouched_comfort_save_stores_no_key(
    hass: HomeAssistant, house
) -> None:
    house()
    entry = _entry(hass)
    data_before, options_before = _stores(entry)
    result = await open_options_page(hass, entry, "comfort")
    result = await submit(hass.config_entries.options, result, prefilled(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data, options = _stores(entry)
    for store in (data, options):
        assert COOLER not in store and WARMER not in store
    # Unrelated keys are untouched.
    for key, value in options_before.items():
        assert options[key] == value, key
    for key, value in data_before.items():
        assert data[key] == value, key


async def test_setting_one_side_stores_only_that_side(
    hass: HomeAssistant, house
) -> None:
    house()
    entry = _entry(hass)
    result = await open_options_page(hass, entry, "comfort")
    result = await submit(
        hass.config_entries.options, result, {**prefilled(result), WARMER: 4.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data, options = _stores(entry)
    assert options[WARMER] == data[WARMER] == 4.0
    assert COOLER not in options and COOLER not in data


async def test_submitting_blank_clears_a_saved_side(hass: HomeAssistant, house) -> None:
    house()
    entry = _entry(hass, options={COOLER: 3.0, WARMER: 4.0})
    data_before, options_before = _stores(entry)
    result = await open_options_page(hass, entry, "comfort")
    assert suggested(result, COOLER) == 3.0
    assert suggested(result, WARMER) == 4.0
    shown = prefilled(result)
    assert shown[COOLER] == 3.0 and shown[WARMER] == 4.0
    del shown[COOLER]  # the user empties the box
    result = await submit(hass.config_entries.options, result, shown)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data, options = _stores(entry)
    assert COOLER not in options and COOLER not in data
    assert options[WARMER] == data[WARMER] == 4.0
    for key, value in options_before.items():
        if key != COOLER:
            assert options[key] == value, key
    for key, value in data_before.items():
        if key != COOLER:
            assert data[key] == value, key

    # Clearing the other side leaves neither behind.
    result = await open_options_page(hass, entry, "comfort")
    shown = prefilled(result)
    del shown[WARMER]
    await submit(hass.config_entries.options, result, shown)
    data, options = _stores(entry)
    for store in (data, options):
        assert COOLER not in store and WARMER not in store


async def test_blank_clears_a_side_held_only_in_the_data_mirror(
    hass: HomeAssistant, house
) -> None:
    """The coordinator reads {**data, **options}: a copy left in data would
    keep the side alive after the user cleared it."""
    house()
    entry = _entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, WARMER: 4.0})
    result = await open_options_page(hass, entry, "comfort")
    assert suggested(result, WARMER) == 4.0
    shown = prefilled(result)
    del shown[WARMER]
    await submit(hass.config_entries.options, result, shown)
    data, options = _stores(entry)
    assert WARMER not in data and WARMER not in options


async def test_saving_another_page_keeps_saved_sides(hass: HomeAssistant, house) -> None:
    house()
    entry = _entry(hass, options={COOLER: 3.0, WARMER: 4.0})
    result = await open_options_page(hass, entry, "fan_idle")
    result = await submit(hass.config_entries.options, result, prefilled(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data, options = _stores(entry)
    assert options[COOLER] == data[COOLER] == 3.0
    assert options[WARMER] == data[WARMER] == 4.0


@pytest.mark.parametrize(
    ("celsius", "value"),
    [
        (False, 0.25),
        (False, 5.5),
        (False, -1.0),
        (False, float("inf")),
        (True, 0.2),
        (True, 2.6),
    ],
)
@pytest.mark.parametrize("key", [COOLER, WARMER, CONF_ENGAGE_DEADBAND])
async def test_out_of_range_is_refused_like_allowed_drift(
    hass: HomeAssistant, house, celsius: bool, value: float, key: str
) -> None:
    """Each side is refused exactly where Allowed drift (the control) is."""
    house(celsius)
    entry = _entry(hass, options={COOLER: 1.0} if celsius else {COOLER: 3.0})
    before = _stores(entry)
    result = await open_options_page(hass, entry, "comfort")
    with pytest.raises(vol.Invalid):
        await submit(
            hass.config_entries.options, result, {**prefilled(result), key: value}
        )
    assert _stores(entry) == before


@pytest.mark.parametrize(
    ("celsius", "value"), [(False, 0.5), (False, 5.0), (True, 0.25), (True, 2.5)]
)
async def test_the_bounds_themselves_are_accepted(
    hass: HomeAssistant, house, celsius: bool, value: float
) -> None:
    house(celsius)
    entry = _entry(hass)
    result = await open_options_page(hass, entry, "comfort")
    result = await submit(
        hass.config_entries.options,
        result,
        {**prefilled(result), COOLER: value, WARMER: value},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[COOLER] == entry.options[WARMER] == value


@pytest.mark.parametrize("key", [COOLER, WARMER])
async def test_nan_is_refused_on_the_advanced_section(
    hass: HomeAssistant, house, key: str
) -> None:
    """NaN slips past the selector's bounds; the page's own check refuses it,
    reported on the section (the frontend drops field errors in sections)."""
    house()
    entry = _entry(hass)
    before = _stores(entry)
    result = await open_options_page(hass, entry, "comfort")
    result = await submit(
        hass.config_entries.options, result, {**prefilled(result), key: float("nan")}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"advanced": "not_a_number"}
    assert sections(result)["advanced"].options["collapsed"] is False
    assert _stores(entry) == before


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (
            {COOLER: "warm"},
            'the saved "Let it get cooler by" isn\'t a number. Open Comfort to fix it.',
        ),
        (
            {WARMER: -1.0},
            'the saved "Let it get warmer by" is negative. Open Comfort to fix it.',
        ),
    ],
)
async def test_menu_attention_names_a_bad_stored_side(
    hass: HomeAssistant, house, stored: dict[str, Any], expected: str
) -> None:
    house()
    entry = _entry(hass, options=stored)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"]["attention"] == (
        f"**Needs attention:** {expected}"
    )


async def test_setup_comfort_page_stores_only_what_was_entered(
    hass: HomeAssistant, house
) -> None:
    house()
    review = await _setup_review(hass)
    result = await setup_tuning(hass, review, {COOLER: 2.0})
    assert result["step_id"] == "review", result
    result = await press(hass.config_entries.flow, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][COOLER] == 2.0
    assert WARMER not in result["options"]
    assert WARMER not in result["data"]


def test_the_help_text_says_what_blank_means_in_both_flows() -> None:
    raw = (Path(component.__file__).parent / "strings.json").read_text("utf-8")
    strings = json.loads(raw)
    help_text = (
        "Leave blank to use Allowed drift. A room's own drift number, once set,"
        " replaces this. So does a room's own uneven drift (Rooms, then the room)."
    )
    for flow in ("config", "options"):
        advanced = strings[flow]["step"]["comfort"]["sections"]["advanced"]
        assert advanced["data"][COOLER] == "Let it get cooler by"
        assert advanced["data"][WARMER] == "Let it get warmer by"
        assert advanced["data_description"][COOLER].startswith(
            "How far below target a room can get before heating starts."
        )
        assert advanced["data_description"][WARMER].startswith(
            "How far above target a room can get before cooling starts."
        )
        for key in (COOLER, WARMER):
            assert advanced["data_description"][key].endswith(help_text)
