"""A °F/°C switch: the Configure temperatures and differences follow it, once.

Seasons and the Away limits are temperatures, so they convert. Allowed drift, Mode switch
threshold and the two uneven drift sides, on Comfort and on a room's page, are differences,
so they only scale. The expected numbers below are worked by hand, not computed with the
code's own formula. All rooms, heads and values are invented.
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
from tests.flow_pages import (
    default,
    open_options_page,
    prefilled,
    press,
    submit,
    suggested,
)
from tests.one_home import DATA_KEYS, assert_one_home
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
    """Save as Configure does: the rooms in data, every setting in options.

    ``options=False`` stores the entry as a release before 2.3 could leave it when its
    options were emptied: every setting in data, mirrored from options, and options empty.
    """
    entry = rig.entry
    if not options:
        rig.hass.config_entries.async_update_entry(
            entry, data={**entry.data, **entry.options, **saved}, options={}
        )
        return
    rooms = {key: value for key, value in saved.items() if key in DATA_KEYS}
    rig.hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, **rooms},
        options={**entry.options, **{k: v for k, v in saved.items() if k not in rooms}},
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
    """The load in use and the entry hold these numbers in this unit, each in its one home."""
    entry = rig.entry
    for key, value in expected.items():
        assert getattr(rig.coord, ATTRIBUTES[key]) == value, key
        assert entry.options[key] == value, key
    assert entry.data[CONF_SAVED_TEMP_UNIT] == unit
    assert_one_home(entry)


def _convert(saved: dict[str, Any], saved_in: str, unit: str) -> dict[str, Any]:
    """What one conversion writes, both stores read as one, with nothing else of Home Assistant."""
    hass = MagicMock()
    entry = SimpleNamespace(data=saved, options={})
    _convert_saved_temps(hass, entry, saved_in, unit)
    written = hass.config_entries.async_update_entry.call_args.kwargs
    assert not set(written["data"]) & set(written["options"])
    return {**written["data"], **written["options"]}


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
    assert dict(rig.entry.data) == {**data, CONF_SAVED_TEMP_UNIT: C}
    assert dict(rig.entry.options) == {**options, **AS_C}
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
    assert rig.entry.options[CONF_CHANGEOVER_COOL_BELOW] == 10
    assert await lockouts_at(10) == ("off", "on")
    assert await lockouts_at(15) == ("off", "off")
    assert await lockouts_at(20) == ("on", "off")
    assert await lockouts_at(15) == ("off", "off")
    await switch(rig, F)
    assert await lockouts_at(59) == ("off", "off")
    assert rig.entry.options[CONF_CHANGEOVER_COOL_BELOW] == 50
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
    """No range and no floors saved, and only in data, as a release before 2.3 could leave
    it: it converts and is recorded, and the write puts it in its home, options."""
    rig = await _configured(hass, freezer, {CONF_CHANGEOVER_COOL_BELOW: 50.0}, options=False)
    assert rig.entry.options == {}
    await _live(rig, C)
    assert rig.entry.options[CONF_CHANGEOVER_COOL_BELOW] == 10
    assert rig.entry.data[CONF_SAVED_TEMP_UNIT] == C
    assert_one_home(rig.entry)
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
    data, options = writes[0].kwargs["data"], writes[0].kwargs["options"]
    assert data[CONF_SAVED_TEMP_UNIT] == F
    assert {key: options[key] for key in SAVED_F} == SAVED_F
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
    assert_one_home(entry)
    hass.config_entries.async_update_entry(
        entry, options={k: v for k, v in entry.options.items() if k != CONF_DRIFT_COOLER_BY}
    )
    await hass.async_block_till_done()
    await rig.reload()
    await _live(rig, F)
    assert entry.data[CONF_SAVED_TEMP_UNIT] == F
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_DRIFT_COOLER_BY: 3.0})
    await hass.async_block_till_done()
    await rig.reload()
    assert entry.options[CONF_DRIFT_COOLER_BY] == 3
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


async def test_a_rooms_own_uneven_drift_converts(hass: HomeAssistant, freezer: Any) -> None:
    """Flipped from keeping its number: the room page's own sides follow the switch, in the
    same write as what else is saved."""
    rig = await open_rig(hass, freezer)
    entry = rig.entry
    zones = [{**zone} for zone in entry.data[CONF_ZONES]]
    zones[0] |= {ZONE_DRIFT_COOLER_BY: 2.0, ZONE_DRIFT_WARMER_BY: 1.5}
    # Something else saved, so the conversion runs whatever it makes of the room's sides.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ZONES: zones}, options={**entry.options, CONF_ECO_HEAT_MIN: 50.0}
    )
    await hass.async_block_till_done()
    await rig.reload()
    await _set_target(hass, rig.eid("_primary_target"), 59)
    await _live(rig, C)
    assert entry.data[CONF_SAVED_TEMP_UNIT] == C
    assert entry.options[CONF_ECO_HEAT_MIN] == 10
    assert entry.data[CONF_ZONES][0][ZONE_DRIFT_COOLER_BY] == 1.125  # 1.11
    assert entry.data[CONF_ZONES][0][ZONE_DRIFT_WARMER_BY] == 0.875  # 0.83
    assert rig.coord.zones[0].drift_cooler_by == 1.125
    assert rig.coord.zones[0].drift_warmer_by == 0.875
    await rig.close()


# A room's own Uneven drift sides. Each row: saved, then that value in the other unit.
SIDES_F = {ZONE_DRIFT_COOLER_BY: 2.0, ZONE_DRIFT_WARMER_BY: 1.5}
SIDES_F_AS_C = {ZONE_DRIFT_COOLER_BY: 1.125, ZONE_DRIFT_WARMER_BY: 0.875}  # 1.11, 0.83
SIDES_C = {ZONE_DRIFT_COOLER_BY: 1.0, ZONE_DRIFT_WARMER_BY: 2.5}
SIDES_C_AS_F = {ZONE_DRIFT_COOLER_BY: 1.75, ZONE_DRIFT_WARMER_BY: 4.5}  # 1.8, 4.5
ROOM_SIDES = (ZONE_DRIFT_COOLER_BY, ZONE_DRIFT_WARMER_BY)
# The room page's flow fields, written out apart from config_flow.py.
ROOM_FIELD = {ZONE_DRIFT_COOLER_BY: "room_cooler_by", ZONE_DRIFT_WARMER_BY: "room_warmer_by"}


def _give_room_sides(rig: Rig, sides: dict[str, Any], *, options: bool = True, **other: Any) -> None:
    """The first room's page saved with ``sides``, as Configure stores a room."""
    zones = [{**zone} for zone in rig.entry.data[CONF_ZONES]]
    zones[0] |= sides
    _store(rig, {CONF_ZONES: zones, **other}, options=options)


async def _room_with_sides(
    hass: HomeAssistant, freezer: Any, sides: dict[str, Any], *, options: bool = True, **other: Any
) -> Rig:
    """An entry in °F whose first room has ``sides``, and a target saved in the same unit."""
    rig = await open_rig(hass, freezer)
    _give_room_sides(rig, sides, options=options, **other)
    await hass.async_block_till_done()
    await rig.reload()
    await _set_target(hass, rig.eid("_primary_target"), 59)
    return rig


def _assert_room_sides(rig: Rig, expected: dict[str, float], unit: str) -> None:
    """The entry (its rooms, in data), the load in use and what the room shows hold these
    sides in this unit."""
    store = rig.entry.data
    for key, value in expected.items():
        assert store[CONF_ZONES][0][key] == value, key
    assert store[CONF_SAVED_TEMP_UNIT] == unit
    assert not set(ROOM_SIDES) & set(store[CONF_ZONES][1])
    assert_one_home(rig.entry)
    zone = rig.coord.zones[0]
    cooler, warmer = expected[ZONE_DRIFT_COOLER_BY], expected[ZONE_DRIFT_WARMER_BY]
    assert (zone.drift_cooler_by, zone.drift_warmer_by) == (cooler, warmer)
    drift = _state(rig, "_primary_drift").attributes
    assert (drift["drift_heat"], drift["drift_cool"]) == (cooler, warmer)
    row = rig.row()
    assert (row["drift_heat"], row["drift_cool"]) == (cooler, warmer)
    assert (row["drift_heat_from"], row["drift_cool_from"]) == ("room_cooler_by", "room_warmer_by")


@pytest.mark.parametrize("switch", [_live, _offline], ids=["live", "while-down"])
async def test_a_rooms_sides_convert_both_ways_across_repeats_and_restarts(
    hass: HomeAssistant, freezer: Any, switch: Any
) -> None:
    rig = await _room_with_sides(hass, freezer, SIDES_F)
    for _ in range(3):
        await switch(rig, C)
        _assert_room_sides(rig, SIDES_F_AS_C, C)
        await rig.restart()
        await rig.reload()
        _assert_room_sides(rig, SIDES_F_AS_C, C)
        await switch(rig, F)
        _assert_room_sides(rig, SIDES_F, F)
        await rig.reload()
        await rig.restart()
        _assert_room_sides(rig, SIDES_F, F)
    await rig.close()


@pytest.mark.parametrize("switch", [_live, _offline], ids=["live", "while-down"])
async def test_a_rooms_sides_saved_in_c_convert_to_f_first(hass: HomeAssistant, freezer: Any, switch: Any) -> None:
    rig = await open_rig(hass, freezer)
    await _offline(rig, C)
    _give_room_sides(rig, SIDES_C)
    await hass.async_block_till_done()
    await rig.reload()
    await _set_target(hass, rig.eid("_primary_target"), 21)
    await rig.reload()
    await switch(rig, F)
    _assert_room_sides(rig, SIDES_C_AS_F, F)
    await rig.restart()
    _assert_room_sides(rig, SIDES_C_AS_F, F)
    await switch(rig, C)
    _assert_room_sides(rig, SIDES_C, C)
    await rig.restart()
    _assert_room_sides(rig, SIDES_C, C)
    await switch(rig, F)
    _assert_room_sides(rig, SIDES_C_AS_F, F)
    await rig.close()


@pytest.mark.parametrize("options", [True, False], ids=["one-home", "data-only"])
async def test_a_first_switch_with_only_a_rooms_sides_saved_converts_in_the_same_load(
    hass: HomeAssistant, freezer: Any, options: bool
) -> None:
    """Nothing else is saved, so only these can start the write, and no unit is on record, so
    the switch is found after the load has read them. That load must not run on the old
    numbers. An entry left with every setting in data and empty options (before 2.3) is
    written to its homes."""
    rig = await _room_with_sides(hass, freezer, SIDES_F, options=options)
    assert CONF_SAVED_TEMP_UNIT not in rig.entry.data
    coord = rig.coord
    await _live(rig, C)
    assert rig.coord is not coord
    assert rig.entry.data[CONF_SAVED_TEMP_UNIT] == C
    assert {key: rig.entry.data[CONF_ZONES][0][key] for key in ROOM_SIDES} == SIDES_F_AS_C
    assert rig.coord._built_conf[CONF_ZONES][0][ZONE_DRIFT_COOLER_BY] == 1.125
    zone = rig.coord.zones[0]
    assert (zone.drift_cooler_by, zone.drift_warmer_by) == (1.125, 0.875)
    row = rig.row()
    assert (row["heat_start"], row["cool_start"]) == (13.875, 15.875)
    _assert_room_sides(rig, SIDES_F_AS_C, C)
    await rig.close()


@pytest.mark.parametrize(
    ("key", "other"), [ROOM_SIDES, ROOM_SIDES[::-1]], ids=["cooler-by-text", "warmer-by-text"]
)
async def test_a_first_switch_holds_a_side_to_the_new_range_as_the_next_load_does(
    hass: HomeAssistant, freezer: Any, key: str, other: str
) -> None:
    """A hand edit: a number saved as text is read, not converted, like Comfort's sides. The
    load that finds the switch must read it as every later load will, at the top of the °C
    range and not at 9."""
    rig = await _room_with_sides(hass, freezer, {key: "9", other: 2.0})
    attribute = {ZONE_DRIFT_COOLER_BY: "drift_cooler_by", ZONE_DRIFT_WARMER_BY: "drift_warmer_by"}[key]
    await _live(rig, C)
    assert rig.entry.data[CONF_ZONES][0][key] == "9"
    assert getattr(rig.coord.zones[0], attribute) == 2.5
    await rig.reload()
    assert getattr(rig.coord.zones[0], attribute) == 2.5
    await rig.close()


@pytest.mark.parametrize("key", ROOM_SIDES)
def test_either_side_alone_starts_the_write(key: str) -> None:
    written = _convert({CONF_ZONES: [{key: 2.0}]}, F, C)
    assert written[CONF_ZONES][0][key] == 1.125
    assert written[CONF_SAVED_TEMP_UNIT] == C


@pytest.mark.parametrize("value", [None, "abc", math.inf], ids=repr)
def test_a_side_that_isnt_a_number_starts_no_write(value: Any) -> None:
    hass = MagicMock()
    entry = SimpleNamespace(data={CONF_ZONES: [{ZONE_DRIFT_COOLER_BY: value}]}, options={})
    _convert_saved_temps(hass, entry, F, C)
    hass.config_entries.async_update_entry.assert_not_called()


async def _rooms_at_target_c(rig: Rig) -> None:
    """Both rooms at their 21 °C target after a switch, with no threshold in the way."""
    await _room_at_21_c(rig)
    assert rig.coord.demand_threshold == 0


async def test_cooler_by_acts_in_the_new_unit(hass: HomeAssistant, freezer: Any) -> None:
    """2 °F is 1.125 °C, so heat starts at 19.5 under a 21 °C target. Read as 2 °C it wouldn't
    until 19."""
    rig = await _room_with_sides(hass, freezer, SIDES_F, **{CONF_DEMAND_THRESHOLD: 0.0})
    await _rooms_at_target_c(rig)
    await rig.temp(0, 20.0)
    assert rig.heads[0].mode != "heat"
    await rig.temp(0, 19.5)
    assert rig.heads[0].mode == "heat"
    await rig.close()


async def test_warmer_by_acts_in_the_new_unit(hass: HomeAssistant, freezer: Any) -> None:
    """1.5 °F is 0.875 °C, so cool starts at 22 over a 21 °C target. Read as 1.5 °C it wouldn't
    until 22.5."""
    rig = await _room_with_sides(hass, freezer, SIDES_F, **{CONF_DEMAND_THRESHOLD: 0.0})
    await _rooms_at_target_c(rig)
    await rig.temp(0, 21.5)
    assert rig.heads[0].mode != "cool"
    await rig.temp(0, 22.0)
    assert rig.heads[0].mode == "cool"
    await rig.close()


async def test_the_sides_act_in_the_old_unit_again_after_switching_back(hass: HomeAssistant, freezer: Any) -> None:
    """Back in °F the room's target is 70 and its cooler by 2 again: inside at 68.5, not at
    67.5. Left at 1.125 it would start heating at 68.5."""
    rig = await _room_with_sides(hass, freezer, SIDES_F, **{CONF_DEMAND_THRESHOLD: 0.0})
    await _rooms_at_target_c(rig)
    await _live(rig, F)
    assert rig.coord.zones[0].target == 70
    await rig.temp(0, 70.0)
    await rig.temp(1, 70.0)
    await rig.temp(0, 68.5)
    assert rig.heads[0].mode != "heat"
    await rig.temp(0, 67.5)
    assert rig.heads[0].mode == "heat"
    await rig.close()


async def test_comfort_and_a_rooms_sides_each_convert_once(hass: HomeAssistant, freezer: Any) -> None:
    """The two levels share key names, and each side falls through on its own. A room's blank
    side stays blank and follows Comfort's converted one."""
    rig = await _room_with_sides(
        hass, freezer, {ZONE_DRIFT_COOLER_BY: 2.0}, **{CONF_DRIFT_COOLER_BY: 3.0, CONF_DRIFT_WARMER_BY: 4.0}
    )
    for unit, global_cooler, global_warmer, room_cooler in ((C, 1.625, 2.25, 1.125), (F, 3.0, 4.0, 2.0)):
        await _live(rig, unit)
        options, data = rig.entry.options, rig.entry.data
        assert (options[CONF_DRIFT_COOLER_BY], options[CONF_DRIFT_WARMER_BY]) == (global_cooler, global_warmer)
        assert data[CONF_ZONES][0][ZONE_DRIFT_COOLER_BY] == room_cooler
        assert ZONE_DRIFT_WARMER_BY not in data[CONF_ZONES][0]
        assert_one_home(rig.entry)
        row = rig.row()
        assert (row["drift_heat"], row["drift_heat_from"]) == (room_cooler, "room_cooler_by")
        assert (row["drift_cool"], row["drift_cool_from"]) == (global_warmer, "warmer_by")
        other = rig.row(1)
        assert (other["drift_heat"], other["drift_cool"]) == (global_cooler, global_warmer)
    await rig.close()


async def test_a_rooms_drift_number_and_its_sides_convert_on_their_own(hass: HomeAssistant, freezer: Any) -> None:
    """The number still goes through its own restore. The cooler side comes from the page, the
    warmer side from the number, and neither is converted through the other."""
    rig = await _room_with_sides(hass, freezer, {ZONE_DRIFT_COOLER_BY: 2.0})
    await hass.services.async_call(
        "number", "set_value", {"entity_id": rig.eid("_primary_drift"), "value": 3.0}, blocking=True
    )
    await hass.async_block_till_done()
    await rig.settle()
    for unit, side, number in ((C, 1.125, 1.625), (F, 2.0, 3.0), (C, 1.125, 1.625)):
        await _live(rig, unit)
        assert rig.entry.data[CONF_ZONES][0][ZONE_DRIFT_COOLER_BY] == side
        assert rig.coord.zones[0].drift == number
        row = rig.row()
        assert (row["drift_heat"], row["drift_heat_from"]) == (side, "room_cooler_by")
        assert (row["drift_cool"], row["drift_cool_from"]) == (number, "room")
    await rig.close()


async def test_nothing_else_in_a_room_moves(hass: HomeAssistant, freezer: Any) -> None:
    """The negative: a store after a switch is the store before it, the sides and the unit
    aside, and a room with no sides of its own doesn't gain any."""
    rig = await _room_with_sides(hass, freezer, SIDES_F, **UNIT_FREE)
    data, options = dict(rig.entry.data), dict(rig.entry.options)
    await _live(rig, C)
    first, second = data[CONF_ZONES]
    zones = [{**first, **SIDES_F_AS_C}, second]
    assert dict(rig.entry.data) == {**data, CONF_ZONES: zones, CONF_SAVED_TEMP_UNIT: C}
    assert dict(rig.entry.options) == options
    await rig.close()


async def test_a_load_with_no_switch_leaves_a_rooms_sides_alone(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _room_with_sides(hass, freezer, SIDES_F)
    before = (dict(rig.entry.data), dict(rig.entry.options))
    await rig.reload()
    await rig.restart()
    assert (dict(rig.entry.data), dict(rig.entry.options)) == before
    assert CONF_SAVED_TEMP_UNIT not in rig.entry.data
    zone = rig.coord.zones[0]
    assert (zone.drift_cooler_by, zone.drift_warmer_by) == (2, 1.5)
    await rig.close()


async def test_a_switch_with_a_rooms_sides_still_reloads_once(hass: HomeAssistant, freezer: Any) -> None:
    """Rewriting the rooms mustn't read as a room page saved, on the first switch or a later one."""
    rig = await _room_with_sides(hass, freezer, SIDES_F)
    real = hass.config_entries.async_reload
    for unit in (C, F):
        with patch.object(hass.config_entries, "async_reload", side_effect=real) as reload:
            await _live(rig, unit)
            await rig.settle()
            await hass.async_block_till_done()
            assert reload.call_count == 1
    await rig.close()


async def test_one_write_holds_a_rooms_sides_and_their_unit(hass: HomeAssistant, freezer: Any) -> None:
    """Nothing to crash between: the sides and the unit they're in land together."""
    rig = await _room_with_sides(hass, freezer, SIDES_F)
    await _live(rig, C)
    real = hass.config_entries.async_update_entry
    with patch.object(hass.config_entries, "async_update_entry", side_effect=real) as update:
        await _offline(rig, F)
        writes = [call for call in update.call_args_list if call.args[0] is rig.entry]
    assert len(writes) == 1
    data, options = writes[0].kwargs["data"], writes[0].kwargs["options"]
    assert data[CONF_SAVED_TEMP_UNIT] == F
    assert {key: data[CONF_ZONES][0][key] for key in ROOM_SIDES} == SIDES_F
    assert CONF_ZONES not in options
    await rig.close()


async def test_a_stale_dump_after_a_crash_doesnt_convert_a_rooms_sides_again(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Restore data from before the switch comes back: the target converts from that save, and
    the sides, already converted, don't move."""
    rig = await _room_with_sides(hass, freezer, SIDES_F)
    eid = rig.eid("_primary_target")
    assert await hass.config_entries.async_unload(rig.entry.entry_id)
    await hass.async_block_till_done()
    stale = stored_state(hass, eid)
    assert stale.extra_data.as_dict()["native_unit_of_measurement"] == F
    assert await hass.config_entries.async_setup(rig.entry.entry_id)
    await hass.async_block_till_done()
    await _live(rig, C)
    _assert_room_sides(rig, SIDES_F_AS_C, C)
    assert await hass.config_entries.async_unload(rig.entry.entry_id)
    await hass.async_block_till_done()
    replace_stored_state(hass, eid, stale)
    assert await hass.config_entries.async_setup(rig.entry.entry_id)
    await hass.async_block_till_done()
    await rig.settle()
    assert rig.coord.zones[0].target == 15
    _assert_room_sides(rig, SIDES_F_AS_C, C)
    await rig.close()


async def test_a_rooms_page_shows_them_converted_and_a_new_value_reads_in_the_new_unit(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Saving the page as it opens changes nothing, and nothing needs attention. A value typed
    after the switch is in the new unit, and only the next switch converts it. A side emptied
    then stays empty through that switch."""
    rig = await _room_with_sides(hass, freezer, SIDES_F)
    await _live(rig, C)
    manager = hass.config_entries.options

    menu = await manager.async_init(rig.entry.entry_id)
    assert menu["description_placeholders"]["attention"] == ""
    manager.async_abort(menu["flow_id"])
    page = await open_options_page(hass, rig.entry, "room_1")
    for key, value in SIDES_F_AS_C.items():
        assert suggested(page, ROOM_FIELD[key]) == value
    saved = await submit(manager, page, prefilled(page))
    assert saved["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await rig.settle()
    _assert_room_sides(rig, SIDES_F_AS_C, C)

    page = await open_options_page(hass, rig.entry, "room_1")
    values = {**prefilled(page), ROOM_FIELD[ZONE_DRIFT_COOLER_BY]: 1.5}
    del values[ROOM_FIELD[ZONE_DRIFT_WARMER_BY]]
    saved = await submit(manager, page, values)
    assert saved["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await rig.settle()
    await rig.restart()
    assert rig.coord.zones[0].drift_cooler_by == 1.5
    assert rig.coord.zones[0].drift_warmer_by is None

    await _live(rig, F)
    assert rig.coord.zones[0].drift_cooler_by == 2.75  # 2.7
    assert rig.coord.zones[0].drift_warmer_by is None
    assert rig.entry.data[CONF_ZONES][0][ZONE_DRIFT_COOLER_BY] == 2.75
    assert ZONE_DRIFT_WARMER_BY not in rig.entry.data[CONF_ZONES][0]
    assert_one_home(rig.entry)
    await rig.close()


@pytest.mark.parametrize("key", ROOM_SIDES)
@pytest.mark.parametrize(
    ("saved_in", "unit", "value", "expected"),
    [
        # Held to the new unit's range, 0.5–5 °F or 0.25–2.5 °C.
        (F, C, 5.0, 2.5),  # 2.78
        (F, C, 4.5, 2.5),
        (C, F, 2.5, 4.5),
        (F, C, 0.5, 0.25),  # 0.28
        (C, F, 0.25, 0.5),  # 0.45
        (F, C, 8.0, 2.5),  # a hand edit past the range
        (F, C, 0.1, 0.25),
        (F, C, -1.0, 0.25),  # the same as Comfort's sides
        # On the box's step. With a 32° shift these would be held at 0.25 °C and 5 °F.
        (F, C, 3.0, 1.625),  # 1.67
        (C, F, 2.0, 3.5),  # 3.6
        (F, C, 1.75, 1.0),  # 0.97
        (C, F, 1.125, 2.0),  # 2.03
    ],
)
def test_the_edges_of_a_rooms_sides(key: str, saved_in: str, unit: str, value: float, expected: float) -> None:
    written = _convert({CONF_ZONES: [{key: value}]}, saved_in, unit)
    assert written[CONF_ZONES][0][key] == expected


@pytest.mark.parametrize("key", ROOM_SIDES)
@pytest.mark.parametrize("value", [None, "abc", math.inf, -math.inf, True], ids=repr)
def test_a_side_that_isnt_a_finite_number_is_left_for_the_menu_to_name(key: str, value: Any) -> None:
    """Next to a saved value that does convert, so the write happens."""
    written = _convert({CONF_ZONES: [{key: value}], CONF_ECO_HEAT_MIN: 50.0}, F, C)
    assert written[CONF_ZONES][0][key] is value


@pytest.mark.parametrize("key", ROOM_SIDES)
def test_a_nan_side_is_left_too(key: str) -> None:
    written = _convert({CONF_ZONES: [{key: math.nan}], CONF_ECO_HEAT_MIN: 50.0}, F, C)
    assert math.isnan(written[CONF_ZONES][0][key])


def test_a_blank_room_side_stays_blank_and_a_room_that_isnt_a_record_is_left() -> None:
    zones = ["not a room", {ZONE_DRIFT_COOLER_BY: 2.0}]
    written = _convert({CONF_ZONES: zones}, F, C)
    assert written[CONF_ZONES] == ["not a room", {ZONE_DRIFT_COOLER_BY: 1.125}]


@PURE
@given(st.sampled_from(ROOM_SIDES), st.sampled_from([value / 4 for value in range(2, 19)]))
def test_every_quarter_f_of_a_rooms_side_comes_back_exact(key: str, value: float) -> None:
    """0.5 to 4.5 °F: the box's grid. 4.75 and 5 are held at 2.5 °C on the way."""
    there = _convert({CONF_ZONES: [{key: value}]}, F, C)[CONF_ZONES][0][key]
    assert abs(there - value * 5 / 9) <= 0.0625
    assert _convert({CONF_ZONES: [{key: there}]}, C, F)[CONF_ZONES][0][key] == value


@PURE
@given(st.sampled_from(ROOM_SIDES), st.sampled_from([value / 8 for value in range(2, 21)]))
def test_a_rooms_side_in_c_lands_within_a_step_and_never_walks(key: str, value: float) -> None:
    """0.125 °C is finer than 0.25 °F, so some come back one step off, once."""

    def across(number: float, saved_in: str, unit: str) -> float:
        return _convert({CONF_ZONES: [{key: number}]}, saved_in, unit)[CONF_ZONES][0][key]

    back = across(across(value, C, F), F, C)
    assert abs(back - value) <= 0.125
    assert across(across(back, C, F), F, C) == back
