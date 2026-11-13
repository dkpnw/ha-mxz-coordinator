"""Entry v2.3: each key in its one home, by a migration that changes no value.

Every saved entry here was written by the real code of an older release
(tests/fixtures/migration, made by the generators beside them): 3.22.0 in °F, the
same entry after 3.22.0 converted it to °C, and v2.20.0, the last release with the
v1 flat layout. What each one means is worked out here, apart from settings.py:
how 3.22.0 ran it, after 3.22.0's own migration, read as ``{**data, **options}``
with a set flat vane key wiring room 1 or 2.

Every head, sensor and service is invented. Nothing talks to a device.
"""

from __future__ import annotations

import dataclasses
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
    mock_restore_cache,
)

from custom_components.mxz_coordinator.const import DOMAIN
from custom_components.mxz_coordinator.settings import entry_config, one_home
from tests.one_home import DATA_KEYS, assert_one_home
from tests.test_drive import _eid, _recompute

FIXTURES = Path(__file__).with_name("fixtures") / "migration"
SAVED = ("saved_by_2_20_0", "saved_by_3_22_0_fahrenheit", "saved_by_3_22_0_switched_to_celsius")
SLOTS = (("primary", "Primary"), ("secondary", "Secondary"))
START = "2026-11-13 18:00:00+00:00"


def _saved(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _meant(saved: dict[str, Any]) -> dict[str, Any]:
    """What 3.22.0 ran a saved entry as, worked out from 3.22.0's own code by hand."""
    data, options = deepcopy(saved["data"]), deepcopy(saved["options"])
    if saved["version"] == 1:
        # v1 -> v2: the rooms come from data's flat keys.
        data["zones"] = [
            {
                "name": name,
                "climate": data[f"{slot}_climate"],
                "sensor": data[f"{slot}_sensor"],
                "vane_vertical": data.get(f"{slot}_vane_vertical"),
                "vane_horizontal": data.get(f"{slot}_vane_horizontal"),
                "stage_sensor": data.get(f"{slot}_stage"),
            }
            for slot, name in SLOTS
        ]
    if saved["minor_version"] < 2 and "idle_action" not in {**data, **options}:
        # v2.1 -> v2.2: an entry that never chose was running fan_only.
        data["idle_action"] = "fan_only"
        if options:
            options["idle_action"] = "fan_only"
    merged = {**data, **options}
    rooms = [dict(zone) for zone in merged["zones"]]
    for index, (slot, _) in enumerate(SLOTS):
        for side in ("vertical", "horizontal"):
            if merged.get(f"{slot}_vane_{side}"):
                rooms[index][f"vane_{side}"] = merged[f"{slot}_vane_{side}"]
    meant = {k: v for k, v in merged.items() if not k.startswith(("primary_", "secondary_"))}
    meant["zones"] = rooms
    return meant


def _household(hass: HomeAssistant, saved: dict[str, Any]) -> None:
    """The heads and sensors a saved entry names, all invented, in the unit it was saved in."""
    celsius = _meant(saved).get("saved_temp_unit") == "°C"
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    for zone in _meant(saved)["zones"]:
        hass.states.async_set(zone["climate"], "off", {
            "hvac_modes": ["off", "cool", "heat", "fan_only", "dry"],
            "supported_features": int(ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TARGET_TEMPERATURE),
            "fan_modes": ["auto", "quiet", "low", "medium", "middle", "high"],
            "temperature": 21 if celsius else 70,
            "min_temp": 16 if celsius else 61,
            "max_temp": 31 if celsius else 88,
        })
        hass.states.async_set(zone["sensor"], "21" if celsius else "70", {
            "device_class": "temperature", "unit_of_measurement": "°C" if celsius else "°F",
        })


def _entry(hass: HomeAssistant, saved: dict[str, Any], **changes: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=saved["title"],
        unique_id=saved["unique_id"],
        version=saved["version"],
        minor_version=saved["minor_version"],
        data=deepcopy(saved["data"]),
        options=deepcopy(saved["options"]),
        **changes,
    )
    entry.add_to_hass(hass)
    return entry


async def _set_up(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# --- the migration ---------------------------------------------------------------------


@pytest.mark.parametrize("name", SAVED)
async def test_a_saved_entry_migrates_to_what_it_meant(hass: HomeAssistant, name: str) -> None:
    saved = _saved(name)
    _household(hass, saved)
    entry = _entry(hass, saved)
    await _set_up(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert (entry.version, entry.minor_version) == (2, 3)
    assert entry.unique_id == saved["unique_id"]
    assert {**entry.data, **entry.options} == _meant(saved)
    assert_one_home(entry)
    assert entry_config(entry) == _meant(saved)


@pytest.mark.parametrize("name", SAVED)
async def test_the_committed_migrated_entries_are_what_this_code_writes(
    hass: HomeAssistant, name: str
) -> None:
    """fixtures/migration/migrated is what a rollback to 3.22.0 was checked against
    (downgrade_check_on_3_22_0.py): it must stay what the migration writes."""
    saved = _saved(name)
    committed = json.loads((FIXTURES / "migrated" / f"{name}.json").read_text())
    _household(hass, saved)
    entry = _entry(hass, saved)
    await _set_up(hass, entry)
    assert (entry.version, entry.minor_version) == (committed["version"], committed["minor_version"])
    assert (dict(entry.data), dict(entry.options)) == (committed["data"], committed["options"])


def test_the_saved_entries_cover_what_the_migration_moves() -> None:
    """A guard against a thin fixture: each case the migration handles is in one of them."""
    v1, fahrenheit, celsius = (_saved(name) for name in SAVED)
    assert v1["version"] == 1 and "primary_vane_vertical" in v1["options"]
    assert "secondary_stage" in v1["options"] and "idle_action" not in {**v1["data"], **v1["options"]}
    for saved in (fahrenheit, celsius):
        assert saved["minor_version"] == 2
        mirrored = set(saved["data"]) & set(saved["options"])
        assert len(mirrored) > 20, mirrored
        assert "notify_service" in saved["data"] and "zones" in saved["data"]
        rooms = saved["data"]["zones"]
        assert any(zone.get("head_type") == "cloud" for zone in rooms)
        assert any(zone.get("opening_entity") for zone in rooms)
        assert any(zone.get("floor_low") for zone in rooms)
    assert "saved_temp_unit" in celsius["data"] and "saved_temp_unit" in celsius["options"]


def _what_it_runs(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    return {
        "settings": {
            name: value
            for name, value in vars(coordinator).items()
            if not name.startswith("_")
            and isinstance(value, (bool, int, float, str, tuple, type(None)))
        },
        "rooms": [dataclasses.asdict(zone) for zone in coordinator.zones],
        "plan": dict(plan.attributes),
    }


async def _drive(hass: HomeAssistant, entry: MockConfigEntry, saved: dict[str, Any]) -> dict[str, Any]:
    """Turn the entry on with room 1 cold, and record what it decides and sends."""
    calls = [
        async_mock_service(hass, "climate", service)
        for service in ("set_hvac_mode", "set_temperature", "set_fan_mode", "turn_off", "turn_on")
    ]
    celsius = hass.config.units is METRIC_SYSTEM
    rooms = _meant(saved)["zones"]
    hass.states.async_set(rooms[0]["sensor"], "17" if celsius else "62", {
        "device_class": "temperature", "unit_of_measurement": "°C" if celsius else "°F",
    })
    for suffix in ("_coordinator_enable", *(f"_{slug}_enable" for slug in ("primary", "secondary"))):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await _recompute(hass, entry)
    await hass.async_block_till_done()
    return {
        **_what_it_runs(hass, entry),
        "sent": [
            (call.service, dict(call.data))
            for record in calls for call in record
        ],
    }


@pytest.mark.parametrize("name", SAVED)
async def test_a_migrated_entry_runs_as_an_entry_built_in_the_new_layout(
    hass: HomeAssistant, freezer: Any, name: str
) -> None:
    """Same settings, same rooms, same plan and the same head commands as an entry stored
    from scratch in the v2.3 layout with what the old one meant. Both run from the same
    instant, so what each plan says about time is compared too."""
    saved = _saved(name)
    freezer.move_to(START)
    mock_restore_cache(hass, ())
    _household(hass, saved)
    migrated = _entry(hass, saved, entry_id="pinned_entry")
    await _set_up(hass, migrated)
    ran = await _drive(hass, migrated, saved)
    assert ran["sent"], "the scenario sent the heads nothing"
    assert await hass.config_entries.async_remove(migrated.entry_id)
    await hass.async_block_till_done()

    meant = _meant(saved)
    freezer.move_to(START)
    mock_restore_cache(hass, ())
    _household(hass, saved)
    built = _entry(
        hass,
        {
            **saved,
            "version": 2,
            "minor_version": 3,
            "data": {k: v for k, v in meant.items() if k in DATA_KEYS},
            "options": {k: v for k, v in meant.items() if k not in DATA_KEYS},
        },
        entry_id="pinned_entry",
    )
    await _set_up(hass, built)
    assert await _drive(hass, built, saved) == ran


# --- what is refused, and what is left alone ---------------------------------------------


async def test_a_future_major_version_is_refused_and_left_as_it_is(hass: HomeAssistant) -> None:
    saved = _saved("saved_by_3_22_0_fahrenheit")
    _household(hass, saved)
    entry = _entry(hass, {**saved, "version": 3, "minor_version": 1})
    before = (deepcopy(dict(entry.data)), deepcopy(dict(entry.options)))
    await _set_up(hass, entry)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert (entry.version, entry.minor_version) == (3, 1)
    assert (dict(entry.data), dict(entry.options)) == before


async def test_a_future_minor_version_loads_and_is_left_as_it_is(hass: HomeAssistant) -> None:
    saved = _saved("saved_by_3_22_0_fahrenheit")
    _household(hass, saved)
    data, options = one_home(saved["data"], saved["options"])
    entry = _entry(hass, {**saved, "minor_version": 4, "data": data, "options": options})
    await _set_up(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 4
    assert (dict(entry.data), dict(entry.options)) == (data, options)


@pytest.mark.parametrize("name", SAVED)
def test_3_22_0_reads_a_migrated_entry_as_it_read_the_saved_one(name: str) -> None:
    """The downgrade: 3.22.0 accepts a v2.3 entry unchanged (its migration only acts on
    v1 and on minor 1) and reads it with the same rule, so it runs what it ran before."""
    saved = _saved(name)
    data, options = one_home(_meant_layout(saved)["data"], _meant_layout(saved)["options"])
    assert _meant({"version": 2, "minor_version": 3, "data": data, "options": options}) == _meant(saved)


def _meant_layout(saved: dict[str, Any]) -> dict[str, Any]:
    """The saved stores after 3.22.0's own migration (v1 rooms, v2.2 idle action)."""
    meant = _meant(saved)
    data = {**saved["data"], "zones": meant["zones"]}
    options = dict(saved["options"])
    if "idle_action" in meant and "idle_action" not in {**data, **options}:
        data["idle_action"] = meant["idle_action"]
    return {"data": data, "options": options}


# --- one_home, without Home Assistant ------------------------------------------------------


@pytest.mark.parametrize("name", SAVED)
def test_one_home_changes_nothing_the_second_time(name: str) -> None:
    saved = _saved(name)
    once = one_home(saved["data"], saved["options"])
    assert one_home(*once) == once


def test_options_win_and_each_key_goes_home() -> None:
    data = {"zones": [{"climate": "climate.a"}], "demand_threshold": 1.0, "notify_service": "notify.a"}
    options = {"zones": [{"climate": "climate.b"}], "demand_threshold": 2.0, "saved_temp_unit": "°C"}
    assert one_home(data, options) == (
        {"zones": [{"climate": "climate.b"}], "notify_service": "notify.a", "saved_temp_unit": "°C"},
        {"demand_threshold": 2.0},
    )


def test_a_set_flat_vane_wires_its_room_and_an_empty_one_does_not() -> None:
    data = {"zones": [{"climate": "climate.a", "vane_vertical": "select.zone_a"},
                      {"climate": "climate.b", "vane_horizontal": "select.zone_b"},
                      {"climate": "climate.c"}]}
    options = {"primary_vane_vertical": "select.flat_a", "secondary_vane_horizontal": "",
               "primary_stage": "sensor.ignored"}
    rooms = one_home(data, options)[0]["zones"]
    assert rooms[0]["vane_vertical"] == "select.flat_a"
    assert rooms[1]["vane_horizontal"] == "select.zone_b"
    assert "stage_sensor" not in rooms[0]
    assert rooms[2] == {"climate": "climate.c"}


def test_a_key_mxz_does_not_know_stays_where_it_was_and_flow_keys_go() -> None:
    data = {"zones": [], "extra": 1, "hands_off": True, "advanced": {}, "generic_heads": True}
    options = {"other": 2, "room_cooler_by": 1.0, "zone_3_stage": "sensor.x"}
    assert one_home(data, options) == ({"zones": [], "extra": 1}, {"other": 2})


def test_an_entry_with_no_rooms_stays_without_them() -> None:
    assert one_home({}, {"demand_threshold": 3.0}) == ({}, {"demand_threshold": 3.0})


def test_the_accessor_reads_only_settings_each_from_its_home() -> None:
    stored = SimpleNamespace(
        data={"zones": [{"climate": "climate.a"}], "demand_threshold": 9.0, "extra": 1},
        options={"demand_threshold": 2.0, "idle_action": "off"},
    )
    assert entry_config(stored) == {
        "zones": [{"climate": "climate.a"}], "demand_threshold": 2.0, "idle_action": "off",
    }
