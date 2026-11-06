"""A °F/°C switch: the Configure temperatures and differences follow it, once.

Seasons and the Away limits are temperatures, so they convert. Allowed drift, Mode switch
threshold and the two uneven drift sides are differences, so they only scale. The expected
numbers below are worked by hand, not computed with the code's own formula. All rooms,
heads and values are invented.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from hypothesis import given
from hypothesis import strategies as st
from pytest_homeassistant_custom_component.common import MockModule, mock_integration

from custom_components.mxz_coordinator.const import (
    CONF_CHANGEOVER_COOL_BELOW,
    CONF_CHANGEOVER_ENTITY,
    CONF_CHANGEOVER_HEAT_ABOVE,
    CONF_COIL_DRY_MINUTES,
    CONF_COMFORT_STALE_HOURS,
    CONF_COOL_LOCKOUT_CEILING,
    CONF_DEMAND_THRESHOLD,
    CONF_DRIFT_COOLER_BY,
    CONF_DRIFT_WARMER_BY,
    CONF_ECO_COOL_MAX,
    CONF_ECO_HEAT_MIN,
    CONF_ENGAGE_DEADBAND,
    CONF_HEAT_LOCKOUT_FLOOR,
    CONF_LOST_VOTE_TTL,
    CONF_MODE_HYSTERESIS,
    CONF_SAVED_TEMP_UNIT,
    CONF_ZONES,
    ZONE_DRIFT_COOLER_BY,
    ZONE_DRIFT_WARMER_BY,
)
from custom_components.mxz_coordinator.coordinator import _convert_saved_temps
from tests.flow_pages import default, prefilled, press, submit, suggested
from tests.property_rig import HOLD, PURE, Rig, open_rig
from tests.restore_store import replace_stored_state, stored_state
from tests.test_changeover import _setup_mock_weather
from tests.test_drive import _set_target
from tests.test_unit_switch import _live, _offline, _state

F, C = "°F", "°C"
OUTDOOR = "sensor.property_outdoor_high"

# Every key, the coordinator attribute that reads it, its value saved in °F, and that value
# in °C. A temperature lands on 0.5 °C, a difference on 0.125 °C.
ATTRIBUTES = {
    CONF_CHANGEOVER_HEAT_ABOVE: "changeover_heat_above",
    CONF_CHANGEOVER_COOL_BELOW: "changeover_cool_below",
    CONF_HEAT_LOCKOUT_FLOOR: "heat_lockout_floor",
    CONF_COOL_LOCKOUT_CEILING: "cool_lockout_ceiling",
    CONF_ECO_HEAT_MIN: "eco_heat_min",
    CONF_ECO_COOL_MAX: "eco_cool_max",
    CONF_ENGAGE_DEADBAND: "engage_deadband",
    CONF_DEMAND_THRESHOLD: "demand_threshold",
    CONF_DRIFT_COOLER_BY: "drift_cooler_by",
    CONF_DRIFT_WARMER_BY: "drift_warmer_by",
}
SAVED_F = {
    CONF_CHANGEOVER_HEAT_ABOVE: 68.0,
    CONF_CHANGEOVER_COOL_BELOW: 50.0,
    CONF_HEAT_LOCKOUT_FLOOR: 58.0,
    CONF_COOL_LOCKOUT_CEILING: 80.0,
    CONF_ECO_HEAT_MIN: 50.0,
    CONF_ECO_COOL_MAX: 78.0,
    CONF_ENGAGE_DEADBAND: 1.0,
    CONF_DEMAND_THRESHOLD: 3.0,
    CONF_DRIFT_COOLER_BY: 2.0,
    CONF_DRIFT_WARMER_BY: 1.5,
}
AS_C = {
    CONF_CHANGEOVER_HEAT_ABOVE: 20.0,
    CONF_CHANGEOVER_COOL_BELOW: 10.0,
    CONF_HEAT_LOCKOUT_FLOOR: 14.5,  # 14.44
    CONF_COOL_LOCKOUT_CEILING: 26.5,  # 26.67
    CONF_ECO_HEAT_MIN: 10.0,
    CONF_ECO_COOL_MAX: 25.5,  # 25.56
    CONF_ENGAGE_DEADBAND: 0.5,  # 0.56
    CONF_DEMAND_THRESHOLD: 1.625,  # 1.67
    CONF_DRIFT_COOLER_BY: 1.125,  # 1.11
    CONF_DRIFT_WARMER_BY: 0.875,  # 0.83
}
# Saved in °C, and that value in °F. A temperature lands on 1 °F, a difference on 0.25 °F.
SAVED_C = {
    CONF_CHANGEOVER_HEAT_ABOVE: 20.0,
    CONF_CHANGEOVER_COOL_BELOW: 10.0,
    CONF_HEAT_LOCKOUT_FLOOR: 14.0,
    CONF_COOL_LOCKOUT_CEILING: 27.0,
    CONF_ECO_HEAT_MIN: 10.0,
    CONF_ECO_COOL_MAX: 26.0,
    CONF_ENGAGE_DEADBAND: 0.5,
    CONF_DEMAND_THRESHOLD: 1.5,
    CONF_DRIFT_COOLER_BY: 1.0,
    CONF_DRIFT_WARMER_BY: 2.5,
}
AS_F = {
    CONF_CHANGEOVER_HEAT_ABOVE: 68.0,
    CONF_CHANGEOVER_COOL_BELOW: 50.0,
    CONF_HEAT_LOCKOUT_FLOOR: 57.0,  # 57.2
    CONF_COOL_LOCKOUT_CEILING: 81.0,  # 80.6
    CONF_ECO_HEAT_MIN: 50.0,
    CONF_ECO_COOL_MAX: 79.0,  # 78.8
    CONF_ENGAGE_DEADBAND: 1.0,  # 0.9
    CONF_DEMAND_THRESHOLD: 2.75,  # 2.7
    CONF_DRIFT_COOLER_BY: 1.75,  # 1.8
    CONF_DRIFT_WARMER_BY: 4.5,
}
# Settings with no temperature in them: a switch must leave these alone.
UNIT_FREE = {
    CONF_MODE_HYSTERESIS: 600,
    CONF_LOST_VOTE_TTL: 30,
    CONF_COMFORT_STALE_HOURS: 12,
    CONF_COIL_DRY_MINUTES: 10,
}


def _store(rig: Rig, saved: dict[str, Any], *, options: bool = True) -> None:
    """Save as the Configure pages do: into options, mirrored into data."""
    entry = rig.entry
    rig.hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, **saved},
        options={**entry.options, **saved} if options else entry.options,
    )


async def _configured(
    hass: HomeAssistant, freezer: Any, saved: dict[str, Any], *, options: bool = True
) -> Rig:
    """An entry in °F with ``saved`` on it and a room target saved in the same unit."""
    rig = await open_rig(hass, freezer)
    _store(rig, saved, options=options)
    await hass.async_block_till_done()
    await rig.reload()
    await _set_target(hass, rig.eid("_primary_target"), 59)
    return rig


def _assert_saved(rig: Rig, expected: dict[str, float], unit: str) -> None:
    """The load in use, and both stores, hold these numbers in this unit."""
    entry = rig.entry
    for key, value in expected.items():
        assert getattr(rig.coord, ATTRIBUTES[key]) == value, key
        assert entry.data[key] == value, key
        assert entry.options[key] == value, key
    assert entry.data[CONF_SAVED_TEMP_UNIT] == unit
    assert entry.options[CONF_SAVED_TEMP_UNIT] == unit


def _convert(saved: dict[str, Any], saved_in: str, unit: str) -> dict[str, Any]:
    """What one conversion writes to the store, with nothing else of Home Assistant."""
    hass = MagicMock()
    entry = SimpleNamespace(data=saved, options={})
    _convert_saved_temps(hass, entry, saved_in, unit)
    return hass.config_entries.async_update_entry.call_args.kwargs["data"]


@pytest.mark.parametrize("switch", [_live, _offline], ids=["live", "while-down"])
async def test_they_convert_both_ways_across_repeats_and_restarts(
    hass: HomeAssistant, freezer: Any, switch: Any
) -> None:
    rig = await _configured(hass, freezer, {**SAVED_F, **UNIT_FREE})
    for _ in range(3):
        await switch(rig, C)
        _assert_saved(rig, AS_C, C)
        await rig.restart()
        await rig.reload()
        _assert_saved(rig, AS_C, C)
        await switch(rig, F)
        _assert_saved(rig, SAVED_F, F)
        await rig.reload()
        await rig.restart()
        _assert_saved(rig, SAVED_F, F)
    await rig.close()


@pytest.mark.parametrize("switch", [_live, _offline], ids=["live", "while-down"])
async def test_a_celsius_entry_converts_to_f_first(hass: HomeAssistant, freezer: Any, switch: Any) -> None:
    rig = await open_rig(hass, freezer)
    await _offline(rig, C)
    _store(rig, SAVED_C)
    await hass.async_block_till_done()
    await rig.reload()
    await _set_target(hass, rig.eid("_primary_target"), 21)
    await rig.reload()
    await switch(rig, F)
    _assert_saved(rig, AS_F, F)
    assert rig.coord.zones[0].target == 70
    await rig.restart()
    _assert_saved(rig, AS_F, F)
    await rig.close()


async def test_nothing_else_in_the_entry_moves(hass: HomeAssistant, freezer: Any) -> None:
    """The negative of the table: a store after a switch is the store before it, those
    keys and the unit aside."""
    rig = await _configured(hass, freezer, {**SAVED_F, **UNIT_FREE})
    data, options = dict(rig.entry.data), dict(rig.entry.options)
    await _live(rig, C)
    assert dict(rig.entry.data) == {**data, **AS_C, CONF_SAVED_TEMP_UNIT: C}
    assert dict(rig.entry.options) == {**options, **AS_C, CONF_SAVED_TEMP_UNIT: C}
    await rig.close()


async def test_a_load_with_no_switch_converts_nothing_and_writes_nothing(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _configured(hass, freezer, SAVED_F)
    before = (dict(rig.entry.data), dict(rig.entry.options))
    await rig.reload()
    await rig.restart()
    assert (dict(rig.entry.data), dict(rig.entry.options)) == before
    assert CONF_SAVED_TEMP_UNIT not in rig.entry.data
    for key, value in SAVED_F.items():
        assert getattr(rig.coord, ATTRIBUTES[key]) == value, key
    await rig.close()


@pytest.mark.parametrize("switch", [_live, _offline], ids=["live", "while-down"])
async def test_lock_out_cool_at_50_f_doesnt_become_50_c(hass: HomeAssistant, freezer: Any, switch: Any) -> None:
    """Read as 50 °C, a 15 °C day locks out cooling, as every day of the year would."""
    rig = await _configured(hass, freezer, {**SAVED_F, CONF_CHANGEOVER_ENTITY: OUTDOOR})

    async def lockouts_at(high: float) -> tuple[str, str]:
        hass.states.async_set(OUTDOOR, str(high))
        await hass.async_block_till_done()
        return _state(rig, "_heat_lockout").state, _state(rig, "_cool_lockout").state

    assert await lockouts_at(59) == ("off", "off")
    await switch(rig, C)
    assert await lockouts_at(15) == ("off", "off")
    assert rig.entry.data[CONF_CHANGEOVER_COOL_BELOW] == 10
    assert await lockouts_at(10) == ("off", "on")
    assert await lockouts_at(15) == ("off", "off")
    assert await lockouts_at(20) == ("on", "off")
    assert await lockouts_at(15) == ("off", "off")
    await switch(rig, F)
    assert await lockouts_at(59) == ("off", "off")
    assert rig.entry.data[CONF_CHANGEOVER_COOL_BELOW] == 50
    assert await lockouts_at(50) == ("off", "on")
    await rig.close()


async def test_a_weather_forecast_is_read_against_the_converted_numbers(hass: HomeAssistant, freezer: Any) -> None:
    """The source this matters most for. The forecast is native °F, and Home Assistant hands
    it over in the system unit, so a 59 °F high arrives as 15 °C."""
    mock_integration(hass, MockModule("test"))
    weather = await _setup_mock_weather(hass)
    rig = await _configured(hass, freezer, {**SAVED_F, CONF_CHANGEOVER_ENTITY: weather.entity_id})

    async def lockouts_at(high: float) -> tuple[str, str]:
        weather.set_high(high)
        await hass.async_block_till_done()
        return _state(rig, "_heat_lockout").state, _state(rig, "_cool_lockout").state

    assert await lockouts_at(59) == ("off", "off")
    await _live(rig, C)
    assert await lockouts_at(60) == ("off", "off")
    assert await lockouts_at(50) == ("off", "on")
    assert await lockouts_at(59) == ("off", "off")
    assert await lockouts_at(68) == ("on", "off")
    await rig.close()


async def _room_at_21_c(rig: Rig) -> None:
    await _live(rig, C)
    for slug in ("primary", "secondary"):
        await _set_target(rig.hass, rig.eid(f"_{slug}_target"), 21)
    await rig.temp(0, 21.0)
    await rig.temp(1, 21.0)


async def test_heat_anyway_below_acts_in_the_new_unit(hass: HomeAssistant, freezer: Any) -> None:
    """A heat lockout at 58 °F holds a 15 °C room. Read as 58 °C it would heat it."""
    rig = await _configured(hass, freezer, SAVED_F)
    await _room_at_21_c(rig)
    await rig.switch("_heat_lockout", True)
    await rig.temp(0, 15.0)
    assert rig.heads[0].mode != "heat"
    await rig.temp(0, 14.0)
    assert rig.heads[0].mode == "heat"
    await rig.close()


async def test_cool_anyway_above_acts_in_the_new_unit(hass: HomeAssistant, freezer: Any) -> None:
    """A cool lockout at 80 °F cools a 27 °C room. Read as 80 °C it never would."""
    rig = await _configured(hass, freezer, SAVED_F)
    await _room_at_21_c(rig)
    await rig.switch("_cool_lockout", True)
    await rig.temp(0, 26.0)
    assert rig.heads[0].mode != "cool"
    await rig.temp(0, 27.0)
    assert rig.heads[0].mode == "cool"
    await rig.close()


async def test_the_away_limits_act_in_the_new_unit(hass: HomeAssistant, freezer: Any) -> None:
    """Away low 50 °F leaves a 15 °C room alone. Read as 50 °C it would heat it; Away high
    78 °F would never cool, read as 78 °C."""
    rig = await _configured(hass, freezer, SAVED_F)
    await _room_at_21_c(rig)
    await rig.switch("_eco_idle", True)
    await rig.temp(0, 15.0)
    assert rig.heads[0].mode != "heat"
    await rig.temp(0, 9.0)
    assert rig.heads[0].mode == "heat"
    await rig.later(HOLD + 1)
    await rig.temp(0, 15.0)
    assert rig.heads[0].mode != "cool"
    await rig.temp(0, 26.0)
    await rig.later(HOLD + 1)
    assert rig.heads[0].mode == "cool"
    await rig.close()


async def test_a_first_switch_rereads_them_in_the_same_load(hass: HomeAssistant, freezer: Any) -> None:
    """With no unit on record the switch is found when a target restores, after the load has
    read them. That load must not run on the old numbers, and a room that follows the
    global drift must show the converted one."""
    rig = await _configured(hass, freezer, SAVED_F)
    assert CONF_SAVED_TEMP_UNIT not in rig.entry.data
    coord = rig.coord
    await _live(rig, C)
    assert rig.coord is not coord
    _assert_saved(rig, AS_C, C)
    assert rig.coord._built_conf[CONF_CHANGEOVER_COOL_BELOW] == 10
    drift = _state(rig, "_primary_drift")
    assert float(drift.state) == 0.5
    assert drift.attributes["override"] is False
    assert rig.coord.zones[0].drift is None
    await rig.close()


async def test_only_a_leftover_saved_is_enough_to_convert_and_record(hass: HomeAssistant, freezer: Any) -> None:
    """No range and no floors saved, and in the data mirror only: empty options stay empty."""
    rig = await _configured(hass, freezer, {CONF_CHANGEOVER_COOL_BELOW: 50.0}, options=False)
    await _live(rig, C)
    assert rig.entry.options == {}
    assert rig.entry.data[CONF_CHANGEOVER_COOL_BELOW] == 10
    assert rig.entry.data[CONF_SAVED_TEMP_UNIT] == C
    assert rig.coord.changeover_cool_below == 10
    await rig.close()


async def test_a_page_never_saved_keeps_the_new_units_default(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _configured(hass, freezer, {CONF_DEMAND_THRESHOLD: 3.0})
    await _live(rig, C)
    assert rig.coord.demand_threshold == 1.625
    assert rig.coord.changeover_cool_below == 10  # the °C default
    assert rig.coord.eco_cool_max == 26  # the °C default
    assert CONF_CHANGEOVER_COOL_BELOW not in {**rig.entry.data, **rig.entry.options}
    assert CONF_DRIFT_COOLER_BY not in {**rig.entry.data, **rig.entry.options}
    assert rig.coord.drift_cooler_by is None
    await rig.close()


async def test_a_switch_that_converts_still_reloads_once(hass: HomeAssistant, freezer: Any) -> None:
    """The write mustn't start a second reload, on the first switch or a later one."""
    rig = await _configured(hass, freezer, SAVED_F)
    real = hass.config_entries.async_reload
    for unit in (C, F):
        with patch.object(hass.config_entries, "async_reload", side_effect=real) as reload:
            await _live(rig, unit)
            await rig.settle()
            await hass.async_block_till_done()
            assert reload.call_count == 1
    await rig.close()


async def test_one_write_holds_the_values_and_their_unit(hass: HomeAssistant, freezer: Any) -> None:
    """Nothing to crash between: the numbers and the unit they're in land together."""
    rig = await _configured(hass, freezer, SAVED_F)
    await _live(rig, C)
    real = hass.config_entries.async_update_entry
    with patch.object(hass.config_entries, "async_update_entry", side_effect=real) as update:
        await _offline(rig, F)
        writes = [call for call in update.call_args_list if call.args[0] is rig.entry]
    assert len(writes) == 1
    for store in (writes[0].kwargs["data"], writes[0].kwargs["options"]):
        assert store[CONF_SAVED_TEMP_UNIT] == F
        assert {key: store[key] for key in SAVED_F} == SAVED_F
    await rig.close()


async def test_a_stale_dump_after_a_crash_doesnt_convert_them_again(hass: HomeAssistant, freezer: Any) -> None:
    """Restore data from before the switch comes back: the target converts from that save,
    and what the entry already converted doesn't move."""
    rig = await _configured(hass, freezer, SAVED_F)
    eid = rig.eid("_primary_target")
    assert await hass.config_entries.async_unload(rig.entry.entry_id)
    await hass.async_block_till_done()
    stale = stored_state(hass, eid)
    assert stale.extra_data.as_dict()["native_unit_of_measurement"] == F
    assert await hass.config_entries.async_setup(rig.entry.entry_id)
    await hass.async_block_till_done()
    await _live(rig, C)
    _assert_saved(rig, AS_C, C)
    assert await hass.config_entries.async_unload(rig.entry.entry_id)
    await hass.async_block_till_done()
    replace_stored_state(hass, eid, stale)
    assert await hass.config_entries.async_setup(rig.entry.entry_id)
    await hass.async_block_till_done()
    assert rig.coord.zones[0].target == 15
    _assert_saved(rig, AS_C, C)
    await rig.close()


async def test_a_unit_on_record_moves_even_with_nothing_left_to_convert(hass: HomeAssistant, freezer: Any) -> None:
    """A blank side stores nothing. If the record stayed behind, the next save, made in the
    new unit, would be converted from the old one at the next load."""
    rig = await _configured(hass, freezer, {CONF_DRIFT_COOLER_BY: 2.0}, options=False)
    entry = rig.entry
    await _live(rig, C)
    assert entry.data[CONF_SAVED_TEMP_UNIT] == C
    hass.config_entries.async_update_entry(
        entry, data={k: v for k, v in entry.data.items() if k != CONF_DRIFT_COOLER_BY}
    )
    await hass.async_block_till_done()
    await rig.reload()
    await _live(rig, F)
    assert entry.data[CONF_SAVED_TEMP_UNIT] == F
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DRIFT_COOLER_BY: 3.0})
    await hass.async_block_till_done()
    await rig.reload()
    assert entry.data[CONF_DRIFT_COOLER_BY] == 3
    assert rig.coord.drift_cooler_by == 3
    await rig.close()


async def test_the_pages_show_them_converted_and_a_new_value_reads_in_the_new_unit(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Saving a page as it opens changes nothing. A value typed after the switch is in the
    new unit, and only the next switch converts it."""
    rig = await _configured(hass, freezer, SAVED_F)
    await _live(rig, C)
    manager = hass.config_entries.options

    menu = await manager.async_init(rig.entry.entry_id)
    assert menu["type"] is FlowResultType.MENU
    assert menu["description_placeholders"]["attention"] == ""
    comfort = await press(manager, menu, "comfort")
    assert default(comfort, CONF_ENGAGE_DEADBAND) == 0.5
    assert default(comfort, CONF_DEMAND_THRESHOLD) == 1.625
    assert suggested(comfort, CONF_DRIFT_COOLER_BY) == 1.125
    saved = await submit(manager, comfort, prefilled(comfort))
    assert saved["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    _assert_saved(rig, AS_C, C)

    menu = await manager.async_init(rig.entry.entry_id)
    seasons = await press(manager, menu, "seasons")
    assert default(seasons, CONF_CHANGEOVER_COOL_BELOW) == 10
    assert default(seasons, CONF_HEAT_LOCKOUT_FLOOR) == 14.5
    values = {**prefilled(seasons), CONF_CHANGEOVER_COOL_BELOW: 12.0}
    saved = await submit(manager, seasons, values)
    assert saved["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await rig.settle()
    assert rig.coord.changeover_cool_below == 12
    await rig.restart()
    assert rig.coord.changeover_cool_below == 12

    await _live(rig, F)
    assert rig.coord.changeover_cool_below == 54  # 53.6
    await rig.close()


@pytest.mark.parametrize(
    ("key", "saved_in", "unit", "value", "expected"),
    [
        # Allowed drift and the sides are held to the new unit's range.
        (CONF_ENGAGE_DEADBAND, F, C, 5.0, 2.5),  # 2.78
        (CONF_ENGAGE_DEADBAND, C, F, 2.5, 4.5),
        (CONF_ENGAGE_DEADBAND, F, C, 4.5, 2.5),
        (CONF_ENGAGE_DEADBAND, F, C, 0.5, 0.25),  # 0.28
        (CONF_ENGAGE_DEADBAND, C, F, 0.25, 0.5),  # 0.45
        (CONF_ENGAGE_DEADBAND, F, C, 8.0, 2.5),  # a hand edit past the range
        (CONF_ENGAGE_DEADBAND, F, C, 0.1, 0.25),
        (CONF_DRIFT_COOLER_BY, F, C, 5.0, 2.5),
        (CONF_DRIFT_WARMER_BY, F, C, 0.1, 0.25),
        (CONF_DRIFT_WARMER_BY, C, F, 2.5, 4.5),
        # The threshold has no range to hold it, and zero is allowed.
        (CONF_DEMAND_THRESHOLD, F, C, 10.0, 5.5),  # 5.56
        (CONF_DEMAND_THRESHOLD, C, F, 5.5, 10.0),  # 9.9
        (CONF_DEMAND_THRESHOLD, F, C, 0.0, 0.0),
        (CONF_DEMAND_THRESHOLD, C, F, 0.0, 0.0),
        # A difference has no 32° shift; a temperature does, and has no range.
        (CONF_DEMAND_THRESHOLD, F, C, 32.0, 17.75),  # 17.78
        (CONF_CHANGEOVER_COOL_BELOW, F, C, 32.0, 0.0),
        (CONF_CHANGEOVER_COOL_BELOW, F, C, -40.0, -40.0),
        (CONF_CHANGEOVER_COOL_BELOW, C, F, -40.0, -40.0),
        (CONF_ECO_HEAT_MIN, C, F, -10.0, 14.0),
        (CONF_HEAT_LOCKOUT_FLOOR, F, C, 200.0, 93.5),  # 93.33
    ],
)
def test_the_edges_of_each_kind(key: str, saved_in: str, unit: str, value: float, expected: float) -> None:
    assert _convert({key: value}, saved_in, unit)[key] == expected


@pytest.mark.parametrize("key", list(SAVED_F))
@pytest.mark.parametrize("value", [None, "abc", math.inf, -math.inf, True], ids=repr)
def test_what_isnt_a_finite_number_is_left_for_the_menu_to_name(key: str, value: Any) -> None:
    """Next to a saved value that does convert, so the write happens."""
    other = CONF_ECO_HEAT_MIN if key != CONF_ECO_HEAT_MIN else CONF_ECO_COOL_MAX
    written = _convert({key: value, other: 50.0}, F, C)
    assert written[key] is value


def test_nan_is_left_too() -> None:
    written = _convert({CONF_ENGAGE_DEADBAND: math.nan, CONF_ECO_HEAT_MIN: 50.0}, F, C)
    assert math.isnan(written[CONF_ENGAGE_DEADBAND])


def test_a_blank_side_stays_blank() -> None:
    written = _convert({CONF_ENGAGE_DEADBAND: 1.0}, F, C)
    assert CONF_DRIFT_COOLER_BY not in written
    assert CONF_DRIFT_WARMER_BY not in written


@PURE
@given(st.sampled_from([value / 4 for value in range(2, 19)]))
def test_every_quarter_f_of_drift_comes_back_exact(value: float) -> None:
    """0.5 to 4.5 °F: the drift box's grid. 4.75 and 5 are held at 2.5 °C on the way."""
    there = _convert({CONF_ENGAGE_DEADBAND: value}, F, C)[CONF_ENGAGE_DEADBAND]
    assert abs(there - value * 5 / 9) <= 0.0625
    assert _convert({CONF_ENGAGE_DEADBAND: there}, C, F)[CONF_ENGAGE_DEADBAND] == value


@PURE
@given(st.sampled_from([value / 8 for value in range(2, 21)]))
def test_a_drift_in_c_lands_within_a_step_and_never_walks(value: float) -> None:
    """0.125 °C is finer than 0.25 °F, so some come back one step off, once."""
    there = _convert({CONF_ENGAGE_DEADBAND: value}, C, F)[CONF_ENGAGE_DEADBAND]
    back = _convert({CONF_ENGAGE_DEADBAND: there}, F, C)[CONF_ENGAGE_DEADBAND]
    assert abs(back - value) <= 0.125
    again = _convert({CONF_ENGAGE_DEADBAND: back}, C, F)[CONF_ENGAGE_DEADBAND]
    assert _convert({CONF_ENGAGE_DEADBAND: again}, F, C)[CONF_ENGAGE_DEADBAND] == back


@PURE
@given(st.integers(-40, 120), st.integers(1, 60))
def test_whole_f_seasons_stay_in_order_with_their_shoulder(cool: int, gap: int) -> None:
    """The page refuses an equal pair, so the conversion can't make one from whole °F."""
    written = _convert(
        {CONF_CHANGEOVER_COOL_BELOW: float(cool), CONF_CHANGEOVER_HEAT_ABOVE: float(cool + gap)}, F, C
    )
    assert written[CONF_CHANGEOVER_COOL_BELOW] < written[CONF_CHANGEOVER_HEAT_ABOVE]


@PURE
@given(st.integers(-80, 100), st.integers(0, 60))
def test_half_c_pairs_never_invert(low: int, gap: int) -> None:
    written = _convert(
        {CONF_HEAT_LOCKOUT_FLOOR: low / 2, CONF_COOL_LOCKOUT_CEILING: (low + gap) / 2}, C, F
    )
    assert written[CONF_HEAT_LOCKOUT_FLOOR] <= written[CONF_COOL_LOCKOUT_CEILING]


async def test_a_half_c_shoulder_that_collapses_is_named_on_the_menu(hass: HomeAssistant, freezer: Any) -> None:
    """12.5 and 13 °C are both 55 °F. Half a degree of shoulder has no whole °F to keep it,
    so the pair lands equal, which the page would refuse: the menu says so."""
    rig = await open_rig(hass, freezer)
    await _offline(rig, C)
    _store(rig, {CONF_CHANGEOVER_COOL_BELOW: 12.5, CONF_CHANGEOVER_HEAT_ABOVE: 13.0})
    await hass.async_block_till_done()
    await rig.reload()
    await _set_target(hass, rig.eid("_primary_target"), 21)
    await rig.reload()
    await _live(rig, F)
    assert (rig.coord.changeover_cool_below, rig.coord.changeover_heat_above) == (55, 55)
    menu = await hass.config_entries.options.async_init(rig.entry.entry_id)
    attention = menu["description_placeholders"]["attention"]
    assert "the saved cool lockout temperature isn't below the heat lockout temperature" in attention
    assert "Seasons" in attention
    await rig.close()


async def test_a_rooms_own_uneven_drift_keeps_its_number(hass: HomeAssistant, freezer: Any) -> None:
    """Documented, and held for a product decision: the room page's own sides aren't part of
    this conversion, so they read as the same number in the new unit."""
    rig = await open_rig(hass, freezer)
    entry = rig.entry
    zones = [{**zone} for zone in entry.data[CONF_ZONES]]
    zones[0] |= {ZONE_DRIFT_COOLER_BY: 2.0, ZONE_DRIFT_WARMER_BY: 1.5}
    # Something else saved, so the conversion runs and has its chance to touch them.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ZONES: zones, CONF_ECO_HEAT_MIN: 50.0}
    )
    await hass.async_block_till_done()
    await rig.reload()
    await _set_target(hass, rig.eid("_primary_target"), 59)
    await _live(rig, C)
    assert entry.data[CONF_SAVED_TEMP_UNIT] == C
    assert entry.data[CONF_ECO_HEAT_MIN] == 10
    assert entry.data[CONF_ZONES][0][ZONE_DRIFT_COOLER_BY] == 2
    assert rig.coord.zones[0].drift_cooler_by == 2
    assert rig.coord.zones[0].drift_warmer_by == 1.5
    await rig.close()
