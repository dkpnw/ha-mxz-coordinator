"""A °F/°C switch: what's saved follows it, once, and a live switch reloads the entry.

A room's target and range are temperatures, so they convert. A room's drift is a
difference, so it only scales. Each save holds the unit it was made in, which is what
makes a repeat or a restart change nothing. All rooms, heads and values are invented.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.number import NumberExtraStoredData
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from hypothesis import given
from hypothesis import strategies as st

from custom_components.mxz_coordinator.const import (
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    CONF_SAVED_TEMP_UNIT,
    CONF_ZONES,
    ZONE_DUAL_SETPOINT,
    ZONE_FLOOR_HIGH,
    ZONE_FLOOR_LOW,
)
from custom_components.mxz_coordinator.units import target_in_unit
from tests.one_home import assert_one_home
from tests.property_rig import PURE, TARGET, Rig, open_rig
from tests.restore_store import replace_stored_state, stored_state
from tests.test_drive import _set_target

F, C = "°F", "°C"
SYSTEMS = {F: US_CUSTOMARY_SYSTEM, C: METRIC_SYSTEM}
NAMES = {F: "us_customary", C: "metric"}
WHOLE_F = range(50, 100)
HALF_C = [value / 2 for value in range(20, 70)]


async def _live(rig: Rig, unit: str) -> None:
    await rig.hass.config.async_update(unit_system=NAMES[unit])
    await rig.hass.async_block_till_done()
    await rig.settle()


async def _offline(rig: Rig, unit: str) -> None:
    """The unit changes while Home Assistant is down."""
    rig.hass.config.units = SYSTEMS[unit]
    await rig.restart()


def _state(rig: Rig, suffix: str) -> Any:
    return rig.hass.states.get(rig.eid(suffix))


@pytest.mark.parametrize(
    ("start", "end", "before", "after"),
    [(F, C, 59, 15), (F, C, 72, 22), (F, C, 70, 21), (C, F, 15, 59), (C, F, 21, 70), (C, F, 20.5, 69)],
)
@pytest.mark.parametrize("switch", [_live, _offline], ids=["live", "while-down"])
async def test_a_target_keeps_its_temperature(
    hass: HomeAssistant, freezer: Any, switch: Any, start: str, end: str, before: float, after: float
) -> None:
    """The report that started this: 59 °F came back as 31 °C."""
    rig = await open_rig(hass, freezer)
    hass.config.units = SYSTEMS[start]
    await rig.reload()
    await _set_target(hass, rig.eid("_primary_target"), before)
    await switch(rig, end)
    assert rig.coord.zones[0].target == after
    state = _state(rig, "_primary_target")
    assert float(state.state) == after
    assert state.attributes["unit_of_measurement"] == end
    await rig.close()


async def test_the_head_is_driven_at_the_converted_target_not_its_limit(hass: HomeAssistant, freezer: Any) -> None:
    """The head's setpoint in the report landed at its maximum, with a cold room."""
    rig = await open_rig(hass, freezer, temps=(55.0, TARGET))
    await _set_target(hass, rig.eid("_primary_target"), 59)
    await rig.settle()
    await _live(rig, C)
    await rig.temp(0, 12.0)
    head = rig.heads[0]  # °F native
    assert head.mode == "heat"
    assert head._attr_native_target_temperature_low < 65
    await rig.close()


@pytest.mark.parametrize("switch", [_live, _offline], ids=["live", "while-down"])
async def test_repeats_and_restarts_change_nothing(hass: HomeAssistant, freezer: Any, switch: Any) -> None:
    rig = await open_rig(hass, freezer)
    await _set_target(hass, rig.eid("_primary_target"), 59)
    for _ in range(3):
        await switch(rig, C)
        assert rig.coord.zones[0].target == 15
        await rig.restart()
        await rig.reload()
        assert rig.coord.zones[0].target == 15
        await switch(rig, F)
        assert rig.coord.zones[0].target == 59
        await rig.reload()
        await rig.restart()
        assert rig.coord.zones[0].target == 59
    await rig.close()


async def test_a_new_target_after_a_switch_is_kept_as_entered(hass: HomeAssistant, freezer: Any) -> None:
    rig = await open_rig(hass, freezer)
    await _live(rig, C)
    await _set_target(hass, rig.eid("_primary_target"), 23.5)
    await rig.restart()
    assert rig.coord.zones[0].target == 23.5
    await _live(rig, F)
    assert rig.coord.zones[0].target == 74  # 74.3
    await rig.close()


@PURE
@given(st.sampled_from(WHOLE_F))
def test_every_whole_f_comes_back_exact(value: int) -> None:
    there = target_in_unit(value, F, C, 0.5)
    assert there * 2 == int(there * 2)
    assert target_in_unit(there, C, F, 1.0) == value


@PURE
@given(st.sampled_from(HALF_C))
def test_a_half_c_comes_back_within_a_step_and_never_walks(value: float) -> None:
    """0.5 °C is finer than 1 °F, so 22.5 °C (72.5 °F) can't come back exact: it lands on
    23 °C and stays. Every other half degree comes back exact."""
    there = target_in_unit(value, C, F, 1.0)
    back = target_in_unit(there, F, C, 0.5)
    assert abs(back - value) <= 0.5
    if value * 9 / 5 % 1 != 0.5:
        assert back == value
    assert target_in_unit(target_in_unit(back, C, F, 1.0), F, C, 0.5) == back


@pytest.mark.parametrize(
    ("value", "set_in", "unit"),
    [(59, F, F), (15, C, C), (59, None, C), (59, "K", C), (59, "", F)],
)
def test_no_other_unit_no_conversion(value: float, set_in: Any, unit: str) -> None:
    assert target_in_unit(value, set_in, unit, 0.5) == value


async def test_a_dual_rooms_range_converts_as_temperatures(hass: HomeAssistant, freezer: Any) -> None:
    rig = await open_rig(hass, freezer)
    entry = rig.entry
    zones = [{**zone} for zone in entry.data[CONF_ZONES]]
    zones[0][ZONE_DUAL_SETPOINT] = True
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONES: zones})
    await hass.async_block_till_done()
    await rig.reload()
    await hass.services.async_call(
        "number", "set_value", {"entity_id": rig.eid("_primary_target_high"), "value": 77}, blocking=True,
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": rig.eid("_primary_target_low"), "value": 68}, blocking=True,
    )
    await hass.async_block_till_done()
    await _live(rig, C)
    zone = rig.coord.zones[0]
    assert (zone.target_low, zone.target_high) == (20, 25)
    assert zone.target == 22.5
    assert float(_state(rig, "_primary_target_low").state) == 20
    assert _state(rig, "_primary_target_high").attributes["unit_of_measurement"] == C
    await _live(rig, F)
    zone = rig.coord.zones[0]
    assert (zone.target_low, zone.target_high) == (68, 77)
    await rig.close()


async def test_a_rooms_drift_only_scales(hass: HomeAssistant, freezer: Any) -> None:
    """A difference has no 32° shift: 2 °F of drift is about 1.1 °C, never -16.7 °C."""
    rig = await open_rig(hass, freezer)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": rig.eid("_primary_drift"), "value": 2}, blocking=True,
    )
    await hass.async_block_till_done()
    await _live(rig, C)
    assert rig.coord.zones[0].drift == 1.125
    state = _state(rig, "_primary_drift")
    assert float(state.state) == 1.125
    assert state.attributes["unit_of_measurement"] == C
    await rig.restart()
    assert rig.coord.zones[0].drift == 1.125
    await _live(rig, F)
    assert rig.coord.zones[0].drift == 2
    await rig.close()


async def test_a_drift_saved_with_no_unit_or_the_same_one_is_left_as_it_was(hass: HomeAssistant, freezer: Any) -> None:
    """Only a change of unit touches it: 1.3 isn't on the step, and isn't moved onto it."""
    rig = await open_rig(hass, freezer)
    eid = rig.eid("_primary_drift")
    await hass.services.async_call("number", "set_value", {"entity_id": eid, "value": 1.3}, blocking=True)
    await hass.async_block_till_done()
    await rig.reload()
    assert rig.coord.zones[0].drift == 1.3
    assert await hass.config_entries.async_unload(rig.entry.entry_id)
    await hass.async_block_till_done()
    stored = stored_state(hass, eid)
    extra = stored.extra_data.as_dict()
    assert extra["native_unit_of_measurement"] == F
    extra["native_unit_of_measurement"] = None
    stored.extra_data = NumberExtraStoredData.from_dict(extra)
    replace_stored_state(hass, eid, stored)
    assert await hass.config_entries.async_setup(rig.entry.entry_id)
    await hass.async_block_till_done()
    assert rig.coord.zones[0].drift == 1.3
    await rig.close()


async def test_a_drift_that_follows_the_global_stays_following(hass: HomeAssistant, freezer: Any) -> None:
    rig = await open_rig(hass, freezer)
    assert rig.coord.zones[0].drift is None
    await _live(rig, C)
    assert rig.coord.zones[0].drift is None
    await rig.close()


async def _saved_rig(hass: HomeAssistant, freezer: Any) -> Rig:
    """An entry whose Configure pages have been saved: range and floors are stored."""
    rig = await open_rig(hass, freezer)
    entry = rig.entry
    zones = [{**zone} for zone in entry.data[CONF_ZONES]]
    zones[0] |= {ZONE_FLOOR_LOW: 45.0, ZONE_FLOOR_HIGH: 100.0}
    zones[1] |= {ZONE_FLOOR_LOW: 35.0}
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_ZONES: zones},
        options={**entry.options, CONF_CLAMP_MIN: 59.0, CONF_CLAMP_MAX: 88.0},
    )
    await hass.async_block_till_done()
    await rig.reload()
    await _set_target(hass, rig.eid("_primary_target"), 59)
    return rig


async def test_a_saved_range_doesnt_undo_the_conversion(hass: HomeAssistant, freezer: Any) -> None:
    """A range saved as 59-88 read in °C would hold the converted 15 °C at 59 °C."""
    rig = await _saved_rig(hass, freezer)
    assert CONF_SAVED_TEMP_UNIT not in {**rig.entry.data, **rig.entry.options}
    await _live(rig, C)
    coord = rig.coord
    assert (coord.clamp_min, coord.clamp_max) == (15, 31)
    assert coord.zones[0].target == 15
    await rig.restart()
    assert (rig.coord.clamp_min, rig.coord.clamp_max) == (15, 31)
    await _live(rig, F)
    assert (rig.coord.clamp_min, rig.coord.clamp_max) == (59, 88)
    assert rig.coord.zones[0].target == 59
    assert rig.entry.data[CONF_SAVED_TEMP_UNIT] == F
    assert rig.entry.options[CONF_CLAMP_MIN] == 59
    assert_one_home(rig.entry)
    await rig.close()


async def test_a_rooms_floor_and_ceiling_follow_the_switch(hass: HomeAssistant, freezer: Any) -> None:
    """A stored floor runs a disabled room's head, so it can't read 45 °F as 45 °C. A value
    is held to the new unit's range, as a converted drive offset is."""
    rig = await _saved_rig(hass, freezer)
    await _live(rig, C)
    first, second = rig.coord.zones
    assert (first.floor_low, first.floor_high) == (7, 38)
    assert second.floor_low == 2  # 35 °F is 1.5 °C, below the °C range
    await rig.reload()
    assert rig.coord.zones[0].floor_low == 7
    await _live(rig, F)
    first, second = rig.coord.zones
    assert (first.floor_low, first.floor_high) == (45, 100)
    assert second.floor_low == 36  # 2 °C is 35.6 °F
    await rig.close()


async def test_a_load_with_no_switch_never_writes_the_entry(hass: HomeAssistant, freezer: Any) -> None:
    """An install that already switched before this version: nothing saved reads as the
    other unit, so its numbers stay as they read today, and nothing new is stored."""
    rig = await _saved_rig(hass, freezer)
    before = (dict(rig.entry.data), dict(rig.entry.options))
    await rig.reload()
    await rig.restart()
    assert (dict(rig.entry.data), dict(rig.entry.options)) == before
    assert (rig.coord.clamp_min, rig.coord.clamp_max) == (59, 88)
    await rig.close()


async def test_a_stale_dump_after_a_crash_doesnt_convert_the_range_again(hass: HomeAssistant, freezer: Any) -> None:
    """A crash brings back restore data from before the switch: the target converts from
    that save, as it should, and the range and floors, already converted, don't move."""
    rig = await _saved_rig(hass, freezer)
    await _live(rig, C)
    coord = rig.coord
    assert (coord.clamp_min, coord.clamp_max) == (15, 31)
    coord.note_saved_unit(F)
    assert (coord.clamp_min, coord.clamp_max) == (15, 31)
    assert coord.zones[0].floor_low == 7
    assert rig.entry.options[CONF_CLAMP_MIN] == 15
    await rig.close()


async def test_a_first_switch_found_with_nothing_saved_stores_nothing(hass: HomeAssistant, freezer: Any) -> None:
    rig = await open_rig(hass, freezer)
    rig.coord.temp_unit = C
    rig.coord.note_saved_unit(F)
    assert CONF_SAVED_TEMP_UNIT not in rig.entry.data
    rig.coord.temp_unit = F
    for unit in (None, "K", F):
        rig.coord.note_saved_unit(unit)
    assert CONF_SAVED_TEMP_UNIT not in rig.entry.data
    await rig.close()


async def _data_only(rig: Rig) -> None:
    """The entry as a release before 2.3 could leave it: every setting in data, options empty."""
    entry = rig.entry
    rig.hass.config_entries.async_update_entry(
        entry, data={**entry.data, **entry.options}, options={}
    )
    await rig.hass.async_block_till_done()
    await rig.reload()


async def test_nothing_saved_stores_nothing_and_empty_options_stay_empty(hass: HomeAssistant, freezer: Any) -> None:
    rig = await open_rig(hass, freezer)
    await _data_only(rig)
    await _live(rig, C)
    assert CONF_SAVED_TEMP_UNIT not in rig.entry.data
    assert rig.entry.options == {}
    await rig.close()


async def test_a_saved_floor_in_a_data_only_entry_converts_and_puts_settings_home(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Before 2.3 an entry could run with every setting in data and options empty. The write
    that converts its floor puts each setting in its home, with the value it read as."""
    rig = await open_rig(hass, freezer)
    settings = dict(rig.entry.options)
    assert settings
    await _data_only(rig)
    zones = [{**zone} for zone in rig.entry.data[CONF_ZONES]]
    zones[0][ZONE_FLOOR_LOW] = 45.0
    hass.config_entries.async_update_entry(rig.entry, data={**rig.entry.data, CONF_ZONES: zones})
    await hass.async_block_till_done()
    await rig.reload()
    await _set_target(hass, rig.eid("_primary_target"), 59)
    await _live(rig, C)
    assert dict(rig.entry.options) == settings
    assert rig.entry.data[CONF_SAVED_TEMP_UNIT] == C
    assert rig.entry.data[CONF_ZONES][0][ZONE_FLOOR_LOW] == 7
    assert_one_home(rig.entry)
    await rig.close()


async def test_a_live_switch_reloads_once(hass: HomeAssistant, freezer: Any) -> None:
    rig = await open_rig(hass, freezer)
    real = hass.config_entries.async_reload
    with patch.object(hass.config_entries, "async_reload", side_effect=real) as reload:
        await _live(rig, C)
        assert reload.call_count == 1
        await rig.settle()
        await hass.async_block_till_done()
        assert reload.call_count == 1
    await rig.close()


@pytest.mark.parametrize(
    "change",
    [{"unit_system": "us_customary"}, {"location_name": "Elsewhere"}, {"time_zone": "Europe/Paris"}],
    ids=["same-unit", "name", "time-zone"],
)
async def test_any_other_core_config_change_doesnt_reload(hass: HomeAssistant, freezer: Any, change: dict[str, Any]) -> None:
    rig = await open_rig(hass, freezer)
    old = rig.coord
    with patch.object(hass.config_entries, "async_reload") as reload:
        await hass.config.async_update(**change)
        await hass.async_block_till_done()
        assert reload.call_count == 0
    assert rig.coord is old
    await rig.close()


async def test_the_old_load_writes_nothing_once_it_knows(hass: HomeAssistant, freezer: Any) -> None:
    """Between the switch and the reload, a head would read MXZ's old numbers in the new unit."""
    rig = await open_rig(hass, freezer)
    with patch.object(hass.config_entries, "async_schedule_reload") as scheduled:
        await hass.config.async_update(unit_system="metric")
        await hass.async_block_till_done()
        assert scheduled.call_count == 1
        old = rig.coord
        assert old.retired is True
        calls = []
        real = old.deliveries._async_issue
        async def spy(*args: Any, **kwargs: Any) -> None:
            calls.append(args)
            await real(*args, **kwargs)
        old.deliveries._async_issue = spy
        await old.deliveries.async_send(rig.heads[0].entity_id, "set_temperature", {"temperature": 70})
        assert calls == []
        await hass.config.async_update(location_name="Elsewhere")
        await hass.async_block_till_done()
        assert scheduled.call_count == 1
    hass.config.units = US_CUSTOMARY_SYSTEM
    await rig.reload()
    await rig.close()


async def test_each_room_converts_from_its_own_save(hass: HomeAssistant, freezer: Any) -> None:
    """Both rooms of the entry convert, each from its own save."""
    rig = await open_rig(hass, freezer)
    await _set_target(hass, rig.eid("_primary_target"), 59)
    await _set_target(hass, rig.eid("_secondary_target"), 77)
    await _live(rig, C)
    assert [zone.target for zone in rig.coord.zones] == [15, 25]
    await rig.close()


async def test_a_heads_own_unit_changes_no_stored_target(hass: HomeAssistant, freezer: Any) -> None:
    """Targets are in Home Assistant's unit, which converts for the head on every write and
    report. A head that changes its own unit is a head, not a switch: no conversion, no reload."""
    rig = await open_rig(hass, freezer, temps=(55.0, TARGET))
    await _set_target(hass, rig.eid("_primary_target"), 59)
    old = rig.coord
    head = rig.heads[0]
    head._attr_native_temperature_unit = C
    try:
        head.async_write_ha_state()
        await hass.async_block_till_done()
        await rig.settle()
        assert rig.coord is old
        assert rig.coord.zones[0].target == 59
        assert float(_state(rig, "_primary_target").state) == 59
        assert _state(rig, "_primary_target").attributes["unit_of_measurement"] == F
    finally:
        head._attr_native_temperature_unit = F
    await rig.close()
