"""Visible start points: where each room starts heating and cooling.

Each room's plan row and room thermostat show the edges its calls compare a
reading to: ``heat_start`` / ``cool_start`` (where the room's head starts a run)
and ``heat_demand_below`` / ``cool_demand_above`` (where the room asks the
shared outdoor unit to switch), plus where each side's drift comes from.
Read-only: the thermostat stays a single-target tile. Fake heads and sensors
only; every expected number is worked out by hand from the stated settings.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.unit_system import METRIC_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.climate import MXZRoomClimate
from custom_components.mxz_coordinator.const import (
    CONF_IDLE_ACTION,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
)
from tests.flow_pages import open_options_page, prefilled, submit
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    MockHeadC,
    _eid,
    _recompute,
    _set_target,
    _set_temp,
    _setup_mock_heads,
)
from tests.test_issue18_drift import _set_drift
from tests.test_m29_follow_global import _press_follow_global
from tests.test_uneven_drift import COOLER, WARMER, _at, _setup, _zones

# The new keys, written out independently of coordinator.START_POINT_KEYS.
KEYS = (
    "drift_source", "drift_heat_from", "drift_cool_from",
    "heat_start", "cool_start", "heat_demand_below", "cool_demand_above",
    "start_points_mode", "start_points_lockouts",
)
EDGES = ("heat_start", "cool_start", "heat_demand_below", "cool_demand_above")


def _row(hass: HomeAssistant, entry, index: int = 0) -> dict[str, Any]:
    return _zones(hass, entry)[index]


def _tile(hass: HomeAssistant, entry, slug: str = "primary") -> dict[str, Any]:
    return dict(hass.states.get(_eid(hass, entry, f"_{slug}_thermostat")).attributes)


def _points(view: dict[str, Any]) -> dict[str, Any]:
    return {key: view[key] for key in KEYS}


def _edges(view: dict[str, Any]) -> tuple:
    return tuple(view[key] for key in EDGES)


def _assert_tile_matches_plan(hass: HomeAssistant, entry) -> None:
    """Both surfaces read one computed plan, so they never disagree."""
    for index, slug in enumerate(("primary", "secondary")):
        assert _points(_tile(hass, entry, slug)) == _points(_row(hass, entry, index))


async def _switch(hass: HomeAssistant, entry, suffix: str, on: bool) -> None:
    await hass.services.async_call(
        "switch", "turn_on" if on else "turn_off",
        {"entity_id": _eid(hass, entry, suffix)}, blocking=True,
    )
    await hass.async_block_till_done()
    await _recompute(hass, entry)


async def _save_comfort(hass: HomeAssistant, entry, **changes: Any) -> None:
    """Save Configure > Comfort as a person would (the entry reloads)."""
    result = await open_options_page(hass, entry, "comfort")
    result = await submit(
        hass.config_entries.options, result, {**prefilled(result), **changes}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    await _recompute(hass, entry)


# --- both options blank: the 3.8.0 values ------------------------------------


async def test_blank_sides_show_allowed_drift_and_threshold(hass: HomeAssistant) -> None:
    """Primary target 62, Allowed drift 1, threshold 3 (the defaults): heads
    start past 61 / 63, the outdoor unit is asked to switch past 59 / 65."""
    entry = await _setup(hass)
    assert _points(_row(hass, entry)) == {
        "drift_source": "global",
        "drift_heat_from": "allowed_drift",
        "drift_cool_from": "allowed_drift",
        "heat_start": 61.0,
        "cool_start": 63.0,
        "heat_demand_below": 59.0,
        "cool_demand_above": 65.0,
        "start_points_mode": "comfort",
        "start_points_lockouts": [],
    }
    # The secondary room's own target, 70.
    assert _edges(_row(hass, entry, 1)) == (69.0, 71.0, 67.0, 73.0)
    # The existing keys are unchanged beside them.
    row = _row(hass, entry)
    assert (row["drift"], row["drift_heat"], row["drift_cool"]) == (1.0, 1.0, 1.0)
    _assert_tile_matches_plan(hass, entry)


@pytest.mark.parametrize(
    "stored",
    [{}, {COOLER: 3.0, WARMER: 5.0}, {WARMER: 4.0}, {COOLER: 1.5}],
    ids=["blank", "uneven", "warmer-only", "cooler-under-threshold"],
)
async def test_the_shown_edges_are_where_the_calls_change(
    hass: HomeAssistant, stored: dict[str, Any]
) -> None:
    """Each reading is taken from a coasting room (back at target first), so
    the engage call is the start decision. On an edge nothing starts; a
    quarter degree past it, it does."""
    entry = await _setup(hass, stored)
    shown = _row(hass, entry)
    cases = (
        ("heat_start", "engage", 0.0, "satisfied"),
        ("heat_start", "engage", -0.25, "heat"),
        ("cool_start", "engage", 0.0, "satisfied"),
        ("cool_start", "engage", 0.25, "cool"),
        ("heat_demand_below", "demand", 0.0, "neutral"),
        ("heat_demand_below", "demand", -0.25, "heat"),
        ("cool_demand_above", "demand", 0.0, "neutral"),
        ("cool_demand_above", "demand", 0.25, "cool"),
    )
    for key, call, offset, expected in cases:
        await _at(hass, entry, 62.0)
        row = await _at(hass, entry, shown[key] + offset)
        assert row[call] == expected, (key, offset, row)
        assert _points(row) == _points(shown)


# --- uneven sides, one side, room override, Follow global --------------------


@pytest.mark.parametrize(
    ("stored", "edges", "sources"),
    [
        # Cooler by 3, warmer by 5: heat start 59, cool start 67; demand is
        # max(3, side): 59 and 67.
        ({COOLER: 3.0, WARMER: 5.0}, (59.0, 67.0, 59.0, 67.0), ("cooler_by", "warmer_by")),
        # Only warmer by 4: the heat side stays Allowed drift 1.
        ({WARMER: 4.0}, (61.0, 66.0, 59.0, 66.0), ("allowed_drift", "warmer_by")),
        # Only cooler by 1.5, under the threshold: the head starts at 60.5,
        # the outdoor unit is still asked at 59.
        ({COOLER: 1.5}, (60.5, 63.0, 59.0, 65.0), ("cooler_by", "allowed_drift")),
    ],
    ids=["both-sides", "warmer-only", "cooler-only"],
)
async def test_each_side_shows_its_own_drift_and_source(
    hass: HomeAssistant, stored: dict[str, Any], edges: tuple, sources: tuple
) -> None:
    entry = await _setup(hass, stored)
    row = _row(hass, entry)
    assert _edges(row) == edges
    assert (row["drift_source"], row["drift_heat_from"], row["drift_cool_from"]) == (
        "global", *sources,
    )
    _assert_tile_matches_plan(hass, entry)


async def test_room_drift_replaces_both_and_follow_global_restores_sides(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass, {COOLER: 3.0, WARMER: 5.0})
    assert _edges(_row(hass, entry)) == (59.0, 67.0, 59.0, 67.0)

    await _set_drift(hass, entry, 2.0)
    await _recompute(hass, entry)
    row = _row(hass, entry)
    # Drift 2 both sides: start 60 / 64; demand max(3, 2) = 3: 59 / 65.
    assert _edges(row) == (60.0, 64.0, 59.0, 65.0)
    assert (row["drift_source"], row["drift_heat_from"], row["drift_cool_from"]) == (
        "room", "room", "room",
    )
    # The other room keeps the uneven globals.
    other = _row(hass, entry, 1)
    assert (other["drift_source"], other["drift_heat_from"], other["drift_cool_from"]) == (
        "global", "cooler_by", "warmer_by",
    )
    _assert_tile_matches_plan(hass, entry)

    await _press_follow_global(hass, entry)
    await _recompute(hass, entry)
    row = _row(hass, entry)
    assert _edges(row) == (59.0, 67.0, 59.0, 67.0)
    assert (row["drift_source"], row["drift_heat_from"], row["drift_cool_from"]) == (
        "global", "cooler_by", "warmer_by",
    )
    _assert_tile_matches_plan(hass, entry)


# --- settings changes reach the plan and the thermostat together -------------


async def test_target_change_moves_both_surfaces(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 66)
    await _recompute(hass, entry)
    assert _edges(_row(hass, entry)) == (65.0, 67.0, 63.0, 69.0)
    assert _edges(_tile(hass, entry)) == (65.0, 67.0, 63.0, 69.0)
    _assert_tile_matches_plan(hass, entry)


async def test_global_sides_and_threshold_from_configure(hass: HomeAssistant) -> None:
    """Configure > Comfort reloads the entry; the new coordinator's first
    plan carries the new edges to both surfaces."""
    entry = await _setup(hass)
    await _save_comfort(hass, entry, **{WARMER: 4.0})
    # Warmer by 4: cool start 66, cool demand max(3, 4) = 66.
    assert _edges(_row(hass, entry)) == (61.0, 66.0, 59.0, 66.0)
    assert _row(hass, entry)["drift_cool_from"] == "warmer_by"
    _assert_tile_matches_plan(hass, entry)

    await _save_comfort(hass, entry, demand_threshold=5.0)
    # Threshold 5 now beats both sides: demand 57 / 67; starts unchanged.
    assert _edges(_row(hass, entry)) == (61.0, 66.0, 57.0, 67.0)
    _assert_tile_matches_plan(hass, entry)

    await _save_comfort(hass, entry, **{COOLER: 2.5})
    assert _edges(_row(hass, entry)) == (59.5, 66.0, 57.0, 67.0)
    assert _row(hass, entry)["drift_heat_from"] == "cooler_by"
    _assert_tile_matches_plan(hass, entry)


async def test_room_drift_change_reaches_the_thermostat(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _set_drift(hass, entry, 4.0)
    await _recompute(hass, entry)
    # Drift 4 both sides; demand max(3, 4) = 4: every edge 4 off target 62.
    assert _edges(_tile(hass, entry)) == (58.0, 66.0, 58.0, 66.0)
    assert _tile(hass, entry)["drift_source"] == "room"
    _assert_tile_matches_plan(hass, entry)


# --- eco, lockouts, a disabled room, a missing reading -----------------------


async def test_eco_shows_the_eco_extremes(hass: HomeAssistant) -> None:
    """Eco calls only past the eco extremes (defaults 50 / 78 °F), for both
    the head and the outdoor unit, and the lockouts don't apply in eco."""
    entry = await _setup(hass)
    await _switch(hass, entry, "_heat_lockout", True)
    await _switch(hass, entry, "_eco_idle", True)
    row = _row(hass, entry)
    assert _edges(row) == (50.0, 78.0, 50.0, 78.0)
    assert (row["start_points_mode"], row["start_points_lockouts"]) == ("eco", [])
    _assert_tile_matches_plan(hass, entry)

    await _switch(hass, entry, "_eco_idle", False)
    row = _row(hass, entry)
    # Back to comfort with the heat lockout on: heat waits for the 58 °F
    # safety floor (the default), cool is unchanged.
    assert _edges(row) == (58.0, 63.0, 58.0, 65.0)
    assert (row["start_points_mode"], row["start_points_lockouts"]) == (
        "comfort", ["heat"],
    )
    _assert_tile_matches_plan(hass, entry)


async def test_cool_lockout_moves_cool_out_to_the_ceiling(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _switch(hass, entry, "_cool_lockout", True)
    row = _row(hass, entry)
    # Cool waits for the 80 °F safety ceiling (the default).
    assert _edges(row) == (61.0, 80.0, 59.0, 80.0)
    assert row["start_points_lockouts"] == ["cool"]
    await _at(hass, entry, 62.0)
    assert (await _at(hass, entry, 80.0))["engage"] == "satisfied"
    assert (await _at(hass, entry, 80.5))["engage"] == "cool"
    _assert_tile_matches_plan(hass, entry)


async def test_disabled_room_has_no_edges(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _switch(hass, entry, "_secondary_enable", False)
    row = _row(hass, entry, 1)
    assert _edges(row) == (None, None, None, None)
    assert (row["start_points_mode"], row["start_points_lockouts"]) == (None, [])
    # Its drift still has a source; only the calls are gone.
    assert (row["drift_source"], row["drift_heat_from"]) == ("global", "allowed_drift")
    assert _edges(_row(hass, entry)) == (61.0, 63.0, 59.0, 65.0)
    _assert_tile_matches_plan(hass, entry)

    await _switch(hass, entry, "_secondary_enable", True)
    assert _edges(_row(hass, entry, 1)) == (69.0, 71.0, 67.0, 73.0)
    _assert_tile_matches_plan(hass, entry)


async def test_missing_reading_still_shows_edges(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    hass.states.async_set(SENSOR_A, "unavailable")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    row = _row(hass, entry)
    assert row["temp"] is None
    assert _edges(row) == (61.0, 63.0, 59.0, 65.0)
    _assert_tile_matches_plan(hass, entry)


# --- Celsius -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "edges", "sources"),
    [
        # °C defaults: target 21, Allowed drift 0.5, threshold 1.5.
        ({}, (20.5, 21.5, 19.5, 22.5), ("allowed_drift", "allowed_drift")),
        # Sides on the 0.125 grain stay exact (no rounding to the 0.5 step).
        ({COOLER: 0.375, WARMER: 2.0}, (20.625, 23.0, 19.5, 23.0), ("cooler_by", "warmer_by")),
    ],
    ids=["defaults", "uneven"],
)
async def test_celsius_edges_are_in_celsius(
    hass: HomeAssistant, stored: dict[str, Any], edges: tuple, sources: tuple
) -> None:
    hass.config.units = METRIC_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass, cls=MockHeadC)
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
    await _switch(hass, entry, "_primary_enable", True)
    row = _row(hass, entry)
    assert _edges(row) == edges
    assert (row["drift_heat_from"], row["drift_cool_from"]) == sources
    _assert_tile_matches_plan(hass, entry)


async def test_edges_carry_no_float_noise(hass: HomeAssistant) -> None:
    """A threshold of 4.02 puts the heat demand edge at 62 - 4.02 = 57.98,
    which binary arithmetic carries as 57.980000000000004; the shown edge
    is the decimal value."""
    assert 62 - 4.02 != 57.98  # the noise this guards against is real
    entry = await _setup(hass, {"demand_threshold": 4.02})
    row = _row(hass, entry)
    assert (row["heat_demand_below"], row["cool_demand_above"]) == (57.98, 66.02)


# --- the thermostat stays a single-target tile ---------------------------------


async def test_thermostat_features_and_single_target_unchanged(
    hass: HomeAssistant,
) -> None:
    """The start points are extra attributes only: no range feature, no
    target_temp_low/high, the same modes and the same single target, so the
    tile, HomeKit, Google, and Alexa see exactly what they saw before."""
    entry = await _setup(hass, {COOLER: 3.0, WARMER: 5.0})
    for slug in ("primary", "secondary"):
        attrs = _tile(hass, entry, slug)
        # The mock heads advertise fan modes; no vane is configured.
        assert attrs["supported_features"] == (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.FAN_MODE
        )
        assert not attrs["supported_features"] & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        assert "target_temp_low" not in attrs
        assert "target_temp_high" not in attrs
        assert set(attrs["hvac_modes"]) == {"off", "heat_cool"}
        assert all(key in attrs for key in KEYS)
    assert _tile(hass, entry)["temperature"] == 62
    # Nor does the class define a low/high target for anything to pick up.
    for name in (
        "target_temperature_low", "target_temperature_high",
        "_attr_target_temperature_low", "_attr_target_temperature_high",
        "native_target_temperature_low", "native_target_temperature_high",
        "_attr_native_target_temperature_low", "_attr_native_target_temperature_high",
    ):
        assert name not in vars(MXZRoomClimate), name
