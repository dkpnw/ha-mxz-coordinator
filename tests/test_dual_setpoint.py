"""Opt-in per-room dual setpoint, end to end (DESIGN.md sections 3 to 5).

Rooms are switched on and off through the real options flow (Configure,
Rooms, the room, Dual setpoint), written through HA's own number and climate
services, and driven by sensor reports, so every path a person, an
automation, or a bridge takes is the one tested. Expected numbers are worked
out by hand from the stated settings: drift 1 °F (0.5 °C), Mode switch
threshold 3 °F (1.5 °C), minimum gap 2 °F (1 °C), bounds 59-88 °F (15-31 °C).

Requires pytest-homeassistant-custom-component (Python 3.12+).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    ZONE_CLIMATE,
    ZONE_DUAL_SETPOINT,
    ZONE_NAME,
    ZONE_SENSOR,
)
from tests.dwell import expire_dwell
from tests.pin_scenario import (
    SENSOR_A,
    SENSOR_B,
    Recorder,
    room_page_pass,
    run_scenario,
    take_vote_rows,
)
from tests.test_drive import MockHead, MockHeadC, _setup_mock_heads
from tests.test_dual_setpoint_pin import PIN

UNITS = {
    "F": (US_CUSTOMARY_SYSTEM, MockHead),
    "C": (METRIC_SYSTEM, MockHeadC),
}


# --- helpers ------------------------------------------------------------------
async def _setup(
    hass: HomeAssistant, unit: str = "F", *, dual: tuple[int, ...] = (), temps=(70, 70),
    restore: list | None = None, zone_keys: dict | None = None,
) -> tuple[MockConfigEntry, tuple[str, str]]:
    system, head_cls = UNITS[unit]
    hass.config.units = system
    heads = await _setup_mock_heads(hass, cls=head_cls)
    await _temps(hass, *temps)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        version=2,
        minor_version=2,
        data={
            CONF_ZONES: [
                {
                    ZONE_NAME: name, ZONE_CLIMATE: head, ZONE_SENSOR: sensor,
                    **(zone_keys or {}),
                    **({ZONE_DUAL_SETPOINT: True} if index in dual else {}),
                }
                for index, (name, head, sensor) in enumerate(
                    zip(("Living", "Bedroom"), heads, (SENSOR_A, SENSOR_B), strict=True)
                )
            ],
        },
        options={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_FAN_BOOST_ENABLE: True,
            CONF_MODE_HYSTERESIS: 0,
        },
    )
    entry.add_to_hass(hass)
    if restore is not None:
        _restore(hass, entry, restore)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, heads


def _restore(hass: HomeAssistant, entry: MockConfigEntry, items: list) -> None:
    """Restore data for (unique-id suffix, value, last_updated or None)."""
    registry = er.async_get(hass)
    cached = []
    for suffix, value, when in items:
        domain = "switch" if suffix.endswith("enable") else "number"
        entity_id = registry.async_get_or_create(
            domain, DOMAIN, f"{entry.entry_id}_{suffix}", config_entry=entry,
            suggested_object_id=suffix,
        ).entity_id
        extra = {} if domain == "switch" else {
            "native_max_value": None, "native_min_value": None, "native_step": None,
            "native_unit_of_measurement": hass.config.units.temperature_unit,
            "native_value": value,
        }
        state = State(entity_id, str(value), **({"last_updated": when} if when else {}))
        cached.append((state, extra))
    mock_restore_cache_with_extra_data(hass, cached)


async def _temps(hass: HomeAssistant, a: float, b: float) -> None:
    unit = hass.config.units.temperature_unit
    for sensor, value in ((SENSOR_A, a), (SENSOR_B, b)):
        hass.states.async_set(sensor, str(value), {ATTR_UNIT_OF_MEASUREMENT: unit})
    await hass.async_block_till_done()


def _eid(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        {"target": "number", "drift": "number", "target_low": "number",
         "target_high": "number", "thermostat": "climate"}.get(
            suffix.split("_", 1)[1], "switch"
        ) if suffix.startswith(("primary_", "secondary_")) else "switch",
        DOMAIN, f"{entry.entry_id}_{suffix}",
    )


def _state(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> State:
    eid = _eid(hass, entry, suffix)
    assert eid is not None, suffix
    return hass.states.get(eid)


def _value(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> float:
    return float(_state(hass, entry, suffix).state)


async def _call(hass: HomeAssistant, domain: str, service: str, data: dict) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)
    await hass.async_block_till_done()


async def _set(hass: HomeAssistant, entry: MockConfigEntry, suffix: str, value: float) -> None:
    await _call(hass, "number", "set_value", {"entity_id": _eid(hass, entry, suffix), "value": value})


async def _range(hass: HomeAssistant, entry: MockConfigEntry, slug: str, low: float, high: float, **extra) -> None:
    await _call(
        hass, "climate", "set_temperature",
        {"entity_id": _eid(hass, entry, f"{slug}_thermostat"),
         "target_temp_low": low, "target_temp_high": high, **extra},
    )


async def _on(hass: HomeAssistant, entry: MockConfigEntry, *keys: str) -> None:
    for key in keys:
        await _call(hass, "switch", "turn_on", {"entity_id": _eid(hass, entry, key)})


async def _compute(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    expire_dwell(entry.runtime_data)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def _run(hass: HomeAssistant, entry: MockConfigEntry, a: float, b: float) -> None:
    await _temps(hass, a, b)
    await _compute(hass, entry)


def _row(entry: MockConfigEntry, index: int) -> dict[str, Any]:
    return entry.runtime_data.data["zones"][index]


def _zone(entry: MockConfigEntry, index: int):
    return entry.runtime_data.zones[index]


def _pair(entry: MockConfigEntry, index: int = 0) -> tuple[float | None, float | None]:
    zone = _zone(entry, index)
    return zone.target_low, zone.target_high


def _has(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> bool:
    return er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, f"{entry.entry_id}_{suffix}"
    ) is not None


async def _start(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _on(hass, entry, "primary_enable", "secondary_enable", "coordinator_enable")
    await _compute(hass, entry)


def _last_setpoints(rec: Recorder, head: str) -> tuple[str, float, float]:
    calls = [
        c["service_data"] for c in rec.calls
        if c["service"] == "set_temperature" and c["service_data"]["entity_id"] in (head, [head])
    ]
    assert calls, "no set_temperature reached the head"
    last = calls[-1]
    return last["hvac_mode"], last["target_temp_low"], last["target_temp_high"]


# --- turning it on and off through the options flow ----------------------------------
@pytest.mark.parametrize(
    ("unit", "target", "seeded"), [("F", 70.0, (69.0, 71.0)), ("C", 21.0, (20.5, 21.5))]
)
async def test_toggle_on_seeds_the_range_at_the_minimum_gap(
    hass: HomeAssistant, unit: str, target: float, seeded: tuple[float, float]
) -> None:
    entry, _ = await _setup(hass, unit)
    await _set(hass, entry, "primary_target", target)
    other_before = dict(_row(entry, 1))
    assert not _has(hass, entry, "primary_target_low")

    result = await room_page_pass(hass, entry, 1, {"dual_setpoint": True})
    assert result["type"] == "create_entry"

    zones = entry.data[CONF_ZONES]
    assert zones[0][ZONE_DUAL_SETPOINT] is True
    assert ZONE_DUAL_SETPOINT not in zones[1]
    assert _pair(entry) == seeded
    assert _value(hass, entry, "primary_target_low") == seeded[0]
    assert _value(hass, entry, "primary_target_high") == seeded[1]
    assert _value(hass, entry, "primary_target") == target  # the midpoint
    assert _eid(hass, entry, "primary_target_low") == "number.mxz_coordinator_living_heat_to"
    assert _eid(hass, entry, "primary_target_high") == "number.mxz_coordinator_living_cool_to"
    assert _state(hass, entry, "primary_target_low").name == "MXZ Coordinator Living heat to"
    assert _state(hass, entry, "primary_target_high").name == "MXZ Coordinator Living cool to"
    for suffix in ("primary_target_low", "primary_target_high"):
        attrs = _state(hass, entry, suffix).attributes
        assert (attrs["min"], attrs["max"]) == ((59, 88) if unit == "F" else (15, 31))
        assert attrs["step"] == (1.0 if unit == "F" else 0.5)
        assert attrs[ATTR_UNIT_OF_MEASUREMENT] == hass.config.units.temperature_unit

    tile = _state(hass, entry, "primary_thermostat").attributes
    features = tile["supported_features"]
    assert features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    assert not features & ClimateEntityFeature.TARGET_TEMPERATURE
    assert features & ClimateEntityFeature.TURN_ON and features & ClimateEntityFeature.TURN_OFF
    assert (tile["target_temp_low"], tile["target_temp_high"]) == seeded
    assert "temperature" not in tile
    assert set(tile["hvac_modes"]) == {"off", "heat_cool"}
    row = _row(entry, 0)
    assert (row["target_low"], row["target_high"], row["target"]) == (*seeded, target)
    # The other room is untouched: no new keys, entities, or values.
    assert _row(entry, 1) == other_before
    assert "target_low" not in _row(entry, 1)
    assert not _has(hass, entry, "secondary_target_low")
    assert not _has(hass, entry, "secondary_target_high")


async def test_toggle_on_seeds_from_the_current_target_not_an_old_range(
    hass: HomeAssistant,
) -> None:
    entry, _ = await _setup(hass)
    await room_page_pass(hass, entry, 1, {"dual_setpoint": True})
    await _range(hass, entry, "primary", 64, 76)
    await room_page_pass(hass, entry, 1, {"dual_setpoint": False})
    assert _value(hass, entry, "primary_target") == 70
    await _set(hass, entry, "primary_target", 66)
    await room_page_pass(hass, entry, 1, {"dual_setpoint": True})
    assert _pair(entry) == (65.0, 67.0)  # around 66, never the old 64-76
    assert _value(hass, entry, "primary_target_low") == 65


@pytest.mark.parametrize(
    ("unit", "pair", "target"),
    [("F", (68, 73), 70.0), ("F", (69, 74), 72.0), ("F", (64, 76), 70.0),
     ("C", (20.0, 21.5), 21.0), ("C", (19.5, 22.5), 21.0)],
)
async def test_toggle_off_sets_the_snapped_midpoint_and_removes_the_entities(
    hass: HomeAssistant, unit: str, pair: tuple[float, float], target: float
) -> None:
    entry, _ = await _setup(hass, unit)
    single_tile_keys = set(_state(hass, entry, "primary_thermostat").attributes)
    single_row_keys = set(_row(entry, 0))
    stored_before = json.dumps(entry.data[CONF_ZONES][0], sort_keys=True)
    await room_page_pass(hass, entry, 1, {"dual_setpoint": True})
    await _range(hass, entry, "primary", *pair)
    low_id = _eid(hass, entry, "primary_target_low")

    await room_page_pass(hass, entry, 1, {"dual_setpoint": False})

    # The key is removed (never stored False): exactly the bytes it had.
    assert ZONE_DUAL_SETPOINT not in entry.data[CONF_ZONES][0]
    assert json.dumps(entry.data[CONF_ZONES][0], sort_keys=True) == stored_before
    assert ZONE_DUAL_SETPOINT not in json.dumps(dict(entry.options))
    assert _value(hass, entry, "primary_target") == target
    assert _zone(entry, 0).target == target
    assert not _has(hass, entry, "primary_target_low")
    assert not _has(hass, entry, "primary_target_high")
    assert hass.states.get(low_id) is None
    tile = _state(hass, entry, "primary_thermostat").attributes
    assert tile["supported_features"] & ClimateEntityFeature.TARGET_TEMPERATURE
    assert not tile["supported_features"] & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    assert tile["temperature"] == target
    assert set(tile) == single_tile_keys
    assert set(_row(entry, 0)) == single_row_keys


async def test_toggling_on_mid_run_resets_the_engage_latch(hass: HomeAssistant) -> None:
    """DESIGN 5: a room heating at 70.5 toward a 72 target is switched to 71-73.
    A fresh decision at 70.5 (heat starts below 70) coasts; the latch the reload
    resumes from the head's mode would keep heating to 71."""
    entry, (head_a, _) = await _setup(hass, temps=(72, 70))
    await _set(hass, entry, "primary_target", 72)
    await _start(hass, entry)
    await _run(hass, entry, 68, 70)
    assert entry.runtime_data.data["primary_engage"] == "heat"
    await _run(hass, entry, 70.5, 70)  # inside the drift: only the latch says heat
    assert entry.runtime_data.data["primary_engage"] == "heat"
    assert hass.states.get(head_a).state == "heat"

    await room_page_pass(hass, entry, 1, {"dual_setpoint": True})
    assert _pair(entry) == (71.0, 73.0)
    await _compute(hass, entry)
    assert entry.runtime_data.data["primary_engage"] == "satisfied"


async def test_toggling_off_mid_run_resets_the_engage_latch(hass: HomeAssistant) -> None:
    """The other direction, with a 3 °F room drift: a 70-72 room heating at 69.5
    toward 70 is switched back to one target, 71. A fresh decision at 69.5 (heat
    starts below 68) coasts; a resumed latch would keep heating to 71."""
    entry, (head_a, _) = await _setup(hass, dual=(0,), temps=(71, 70))
    await _range(hass, entry, "primary", 70, 72)
    await _set(hass, entry, "primary_drift", 3)
    await _start(hass, entry)
    await _run(hass, entry, 66, 70)
    assert entry.runtime_data.data["primary_engage"] == "heat"
    await _run(hass, entry, 69.5, 70)
    assert entry.runtime_data.data["primary_engage"] == "heat"
    assert hass.states.get(head_a).state == "heat"

    await room_page_pass(hass, entry, 1, {"dual_setpoint": False})
    assert _zone(entry, 0).target == 71
    assert _zone(entry, 0).dual is False
    await _compute(hass, entry)
    assert entry.runtime_data.data["primary_engage"] == "satisfied"


async def test_an_untouched_room_page_keeps_a_dual_room_as_it_is(hass: HomeAssistant) -> None:
    entry, _ = await _setup(hass)
    await room_page_pass(hass, entry, 1, {"dual_setpoint": True})
    await _range(hass, entry, "primary", 66, 75)
    data, options = json.dumps(dict(entry.data)), json.dumps(dict(entry.options))
    await room_page_pass(hass, entry, 1)
    assert json.dumps(dict(entry.data)) == data
    assert json.dumps(dict(entry.options)) == options
    assert _pair(entry) == (66, 75)


# --- what the room decides, and what the head is sent ----------------------------------
@pytest.mark.parametrize(
    ("unit", "pair", "cold", "hot", "heat_sent", "cool_sent"),
    [
        ("F", (68, 74), 64, 78, (68, 70), (72, 74)),
        ("C", (20.0, 23.0), 18.0, 25.0, (20.0, 21.0), (22.0, 23.0)),
    ],
)
async def test_head_setpoints_come_from_heat_to_and_cool_to(
    hass: HomeAssistant, unit, pair, cold, hot, heat_sent, cool_sent
) -> None:
    entry, (head_a, head_b) = await _setup(hass, unit, dual=(0,), temps=(pair[0], 21 if unit == "C" else 70))
    await _range(hass, entry, "primary", *pair)
    rec = Recorder(hass, (head_a, head_b))
    await _start(hass, entry)
    await _run(hass, entry, cold, 21 if unit == "C" else 70)
    assert entry.runtime_data.data["state"] == "heat"
    assert _last_setpoints(rec, head_a) == ("heat", *heat_sent)
    await _run(hass, entry, hot, 21 if unit == "C" else 70)
    assert entry.runtime_data.data["state"] == "cool"
    assert _last_setpoints(rec, head_a) == ("cool", *cool_sent)


async def test_runs_end_at_the_edge_and_start_points_are_the_edges_used(
    hass: HomeAssistant,
) -> None:
    entry, (head_a, _) = await _setup(hass, dual=(0,), temps=(71, 70))
    await _range(hass, entry, "primary", 68, 74)
    await _start(hass, entry)
    row = _row(entry, 0)
    assert (row["heat_start"], row["cool_start"]) == (67.0, 75.0)
    assert (row["heat_demand_below"], row["cool_demand_above"]) == (65.0, 77.0)
    tile = _state(hass, entry, "primary_thermostat").attributes
    assert (tile["heat_start"], tile["cool_start"]) == (67.0, 75.0)
    for temp, engage in (
        (67.0, "satisfied"),  # at the start edge: strict, still coasting
        (66.5, "heat"), (67.5, "heat"), (67.9, "heat"),
        (68.0, "satisfied"),  # reached Heat to: coasts
        (70.5, "satisfied"),  # never runs on to the midpoint
        (75.0, "satisfied"), (75.5, "cool"), (74.5, "cool"),
        (74.0, "satisfied"),  # reached Cool to
    ):
        await _run(hass, entry, temp, 70)
        assert entry.runtime_data.data["primary_engage"] == engage, temp
    assert hass.states.get(head_a).state == "fan_only"


async def test_the_demand_vote_measures_from_each_edge_not_the_midpoint(
    hass: HomeAssistant,
) -> None:
    """Threshold 3 °F from each edge: a 68-74 room asks the shared unit for heat
    below 65 and for cool above 77. From the midpoint (71) it would ask for heat
    below 68 and for cool above 74, so at 66 and 76 the two answers differ."""
    entry, _ = await _setup(hass, dual=(0,), temps=(71, 70))
    await _range(hass, entry, "primary", 68, 74)
    await _start(hass, entry)
    row = _row(entry, 0)
    assert (row["heat_demand_below"], row["cool_demand_above"]) == (65.0, 77.0)
    for temp, engage, demand in (
        (66.0, "heat", "neutral"),  # past the start edge, short of the vote
        (65.0, "heat", "neutral"),  # at heat_demand_below: strict, no vote yet
        (64.9, "heat", "heat"),
        (71.0, "satisfied", "neutral"),
        (76.0, "cool", "neutral"),
        (77.0, "cool", "neutral"),  # at cool_demand_above: strict, no vote yet
        (77.1, "cool", "cool"),
    ):
        await _run(hass, entry, temp, 70)
        plan = entry.runtime_data.data
        assert (plan["primary_engage"], plan["primary_demand"]) == (engage, demand), temp
        # The shown switching points are the ones the vote just used.
        row = _row(entry, 0)
        assert (row["heat_demand_below"], row["cool_demand_above"]) == (65.0, 77.0), temp


async def test_fan_boost_measures_the_distance_to_the_range(hass: HomeAssistant, monkeypatch) -> None:
    entry, (head_a, head_b) = await _setup(hass, dual=(0,), temps=(71, 70))
    await _range(hass, entry, "primary", 68, 74)
    coord = entry.runtime_data
    seen: dict[str, float] = {}
    original = coord._queue_delivery

    def spy(climate_id, act, low, high, delta=0.0, engage=None):
        seen[climate_id] = delta
        return original(climate_id, act, low, high, delta, engage)

    monkeypatch.setattr(coord, "_queue_delivery", spy)
    await _start(hass, entry)
    for temp, want in ((71.0, 0.0), (68.0, 0.0), (74.0, 0.0), (65.5, 2.5), (77.0, 3.0)):
        await _run(hass, entry, temp, 72.5)
        assert seen[head_a] == want, temp
        assert seen[head_b] == 2.5  # the single-target room: abs(72.5 - 70)


@pytest.mark.parametrize(
    ("unit", "pair", "eco_band", "below", "above"),
    [
        ("F", (68, 82), (50.0, 78.0), 77.5, 79.0),
        ("C", (20.0, 28.0), (10.0, 26.0), 25.5, 26.5),
    ],
)
async def test_eco_ignores_a_cool_to_above_the_eco_ceiling(
    hass: HomeAssistant, unit, pair, eco_band, below, above
) -> None:
    """Eco runs the eco band even where cool to sits past its ceiling: the
    room cools above the eco ceiling, not above cool to."""
    other = 21 if unit == "C" else 70
    entry, _ = await _setup(hass, unit, dual=(0,), temps=(pair[0] + 1, other))
    await _range(hass, entry, "primary", *pair)
    await _start(hass, entry)
    await _on(hass, entry, "eco_idle")
    for temp, engage in ((below, "satisfied"), (above, "cool")):
        await _run(hass, entry, temp, other)
        row = _row(entry, 0)
        assert row["start_points_mode"] == "eco"
        assert (row["heat_start"], row["cool_start"]) == eco_band
        assert entry.runtime_data.data["primary_engage"] == engage, temp


@pytest.mark.parametrize("dual_index", [0, 1])
async def test_standoff_and_arbitration_with_one_dual_and_one_single_room(
    hass: HomeAssistant, dual_index: int
) -> None:
    entry, _ = await _setup(hass, dual=(dual_index,), temps=(70, 70))
    slug = ("primary", "secondary")[dual_index]
    await _range(hass, entry, slug, 68, 74)
    await _start(hass, entry)
    # The dual room asks for heat below 65 (68 - 3), the single room for cool
    # above 73 (70 + 3): a standoff, won by the higher-priority room.
    cold_dual = {0: (64, 74), 1: (74, 64)}[dual_index]
    await _run(hass, entry, *cold_dual)
    plan = entry.runtime_data.data
    assert plan["standoff"] is True
    assert plan["state"] == ("heat" if dual_index == 0 else "cool")
    assert plan[f"{slug}_demand"] == "heat"
    # Inside its range the dual room asks nothing, where a 71 target would
    # call cool at 72.5: the range is a deadband, so no standoff.
    inside = {0: (72.5, 64), 1: (64, 72.5)}[dual_index]
    await _run(hass, entry, *inside)
    plan = entry.runtime_data.data
    assert plan[f"{slug}_demand"] == "neutral"
    assert plan[f"{slug}_engage"] == "satisfied"
    assert plan["standoff"] is False
    assert plan["state"] == "heat"


# --- validation ----------------------------------------------------------------
async def test_number_writes_push_the_other_edge_and_refuse_past_the_bounds(
    hass: HomeAssistant, monkeypatch
) -> None:
    entry, _ = await _setup(hass, dual=(0,))
    await _range(hass, entry, "primary", 68, 74)
    resets: list[str] = []
    coord = entry.runtime_data
    original = coord.reset_engage_latch
    monkeypatch.setattr(coord, "reset_engage_latch", lambda slug: (resets.append(slug), original(slug)))

    await _set(hass, entry, "primary_target_low", 73)
    assert _pair(entry) == (73, 75)  # pushed up
    assert _value(hass, entry, "primary_target_high") == 75
    assert _value(hass, entry, "primary_target") == 74
    assert resets == ["primary"]
    await _set(hass, entry, "primary_target_high", 72)
    assert _pair(entry) == (70, 72)  # pushed down
    await _set(hass, entry, "primary_target_high", 80)
    assert _pair(entry) == (70, 80)  # wide: nothing pushed

    await _range(hass, entry, "primary", 82, 87)
    with pytest.raises(ServiceValidationError) as err:
        await _set(hass, entry, "primary_target_low", 87)  # would push cool to 89
    assert err.value.translation_key == "range_push_out_of_bounds"
    assert err.value.translation_placeholders["zone"] == "Living"
    assert _pair(entry) == (82, 87)
    await _range(hass, entry, "primary", 60, 65)
    with pytest.raises(ServiceValidationError):
        await _set(hass, entry, "primary_target_high", 60)  # would push heat to 58
    assert _pair(entry) == (60, 65)
    # Outside the number's own bounds HA refuses it before we see it.
    with pytest.raises((ServiceValidationError, ValueError)):
        await _set(hass, entry, "primary_target_low", 58)
    assert _pair(entry) == (60, 65)
    assert len(resets) == 5  # three edge writes and two range writes, nothing for refusals


async def test_climate_pair_is_checked_and_written_atomically(hass: HomeAssistant, monkeypatch) -> None:
    entry, (head_a, head_b) = await _setup(hass, dual=(0,))
    coord = entry.runtime_data
    writes: list[tuple] = []
    original = coord.set_zone_range
    monkeypatch.setattr(
        coord, "set_zone_range", lambda zone, lo, hi: (writes.append((lo, hi)), original(zone, lo, hi))
    )
    await _range(hass, entry, "primary", 68, 74)
    assert writes == [(68, 74)]  # one write for the pair, not one per edge

    rec = Recorder(hass, (head_a, head_b))
    # The room is off, and each refused call asks to turn it on: a refused
    # call changes nothing, its mode included. Both edges move in each pair,
    # so none of them is a push.
    assert _state(hass, entry, "primary_enable").state == "off"
    for low, high in ((70, 71), (71, 71), (65, 66)):
        with pytest.raises(ServiceValidationError) as err:
            await _range(hass, entry, "primary", low, high, hvac_mode="heat_cool")
        assert err.value.translation_key == "range_too_narrow"
        assert _state(hass, entry, "primary_enable").state == "off", (low, high)
    with pytest.raises(ServiceValidationError) as err:
        await _range(hass, entry, "primary", 75, 70, hvac_mode="heat_cool")  # inverted: HA's own check
    assert _pair(entry) == (68, 74)
    assert writes == [(68, 74)]
    assert _state(hass, entry, "primary_enable").state == "off"
    assert rec.calls == []

    # A HomeKit-style pair with one edge unchanged follows the push rule.
    await _range(hass, entry, "primary", 73, 74)
    assert _pair(entry) == (73, 75)
    # hvac_mode is honored with a valid pair (#17).
    await _range(hass, entry, "primary", 66, 76, hvac_mode="heat_cool")
    assert _state(hass, entry, "primary_enable").state == "on"
    assert _pair(entry) == (66, 76)


async def test_single_number_and_range_writes_go_to_the_right_kind_of_room(
    hass: HomeAssistant,
) -> None:
    """HA refuses a lone temperature on a range room and a range on a single room."""
    entry, _ = await _setup(hass, dual=(0,))
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "climate", "set_temperature", {
            "entity_id": _eid(hass, entry, "primary_thermostat"), "temperature": 70})
    assert err.value.translation_key == "missing_target_temperature_entity_feature"
    with pytest.raises(ServiceValidationError) as err:
        await _range(hass, entry, "secondary", 68, 74)
    assert err.value.translation_key == "missing_target_temperature_range_entity_feature"
    with pytest.raises(ServiceValidationError) as err:
        await _range(hass, entry, "primary", 50, 74)  # below min_temp: HA's own check
    assert _pair(entry) == (69.0, 71.0)


@pytest.mark.parametrize(
    ("pair", "value", "moved", "shown"),
    [
        ((68, 74), 66, (63, 69), 66), ((68, 74), 60, (59, 65), 62),
        ((68, 74), 87, (82, 88), 85), ((68, 73), 70, (68, 73), 70.5),
        ((68, 73), 71, (69, 74), 71.5),
    ],
)
async def test_the_target_number_moves_the_range(
    hass: HomeAssistant, pair, value, moved, shown
) -> None:
    entry, _ = await _setup(hass, dual=(0,))
    await _range(hass, entry, "primary", *pair)
    assert _value(hass, entry, "primary_target") == (pair[0] + pair[1]) / 2
    await _set(hass, entry, "primary_target", value)
    assert _pair(entry) == moved
    assert _value(hass, entry, "primary_target") == shown
    assert _value(hass, entry, "primary_target_low") == moved[0]
    tile = _state(hass, entry, "primary_thermostat").attributes
    assert (tile["target_temp_low"], tile["target_temp_high"]) == moved
    await _compute(hass, entry)  # the plan follows on its (debounced) recompute
    row = _row(entry, 0)
    assert (row["target"], row["target_low"], row["target_high"]) == (shown, *moved)


# --- restart and restore ----------------------------------------------------------
async def test_a_restart_restores_the_range(hass: HomeAssistant) -> None:
    entry, _ = await _setup(hass, dual=(0,), temps=(71, 70))
    await _range(hass, entry, "primary", 66, 76)
    await _start(hass, entry)
    await _run(hass, entry, 64, 70)
    assert entry.runtime_data.data["primary_engage"] == "heat"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _pair(entry) == (66.0, 76.0)
    assert _value(hass, entry, "primary_target") == 71
    await _run(hass, entry, 65.5, 70)
    # The run resumes from the head's mode and still runs to Heat to.
    assert entry.runtime_data.data["primary_engage"] == "heat"
    await _run(hass, entry, 66, 70)
    assert entry.runtime_data.data["primary_engage"] == "satisfied"


async def test_a_stale_restore_is_ignored_and_the_range_is_seeded(hass: HomeAssistant) -> None:
    stale = dt_util.utcnow() - timedelta(days=2)
    entry, _ = await _setup(
        hass, dual=(0,), restore=[
            ("primary_target", 66, None),
            ("primary_target_low", 60, stale), ("primary_target_high", 80, stale),
        ],
    )
    assert _value(hass, entry, "primary_target") == 66
    assert _pair(entry) == (65.0, 67.0)  # seeded around the fresh target, not 60-80


async def test_one_missing_edge_reseeds_the_pair(hass: HomeAssistant) -> None:
    entry, _ = await _setup(
        hass, dual=(0,), restore=[("primary_target", 72, None), ("primary_target_low", 60, None)],
    )
    assert _pair(entry) == (71.0, 73.0)  # never 60 with a seeded high


async def test_a_disabled_edge_number_still_leaves_a_whole_range(hass: HomeAssistant) -> None:
    entry, _ = await _setup(hass, dual=(0,))
    await _range(hass, entry, "primary", 64, 76)
    registry = er.async_get(hass)
    registry.async_update_entity(
        _eid(hass, entry, "primary_target_high"), disabled_by=er.RegistryEntryDisabler.USER
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _pair(entry) == (69.0, 71.0)  # seeded around the target, never half restored
    tile = _state(hass, entry, "primary_thermostat").attributes
    assert (tile["target_temp_low"], tile["target_temp_high"]) == (69.0, 71.0)


async def test_a_restored_pair_that_no_longer_fits_is_re_centered_once(hass: HomeAssistant) -> None:
    entry, _ = await _setup(
        hass, dual=(0,), restore=[
            ("primary_target", 88, None),
            ("primary_target_low", 87, None), ("primary_target_high", 95, None),
        ],
    )
    # Clamped to 87-88, 1 °F apart: re-centered at the gap around 87.5, inside 59-88.
    assert _pair(entry) == (86.0, 88.0)
    assert _value(hass, entry, "primary_target_low") == 86
    assert _value(hass, entry, "primary_target_high") == 88
    assert _value(hass, entry, "primary_target") == 87
    reasons = [r["reason"] for r in entry.runtime_data._control_reasons(_zone(entry, 0))]
    note = [r for r in reasons if "re-centered" in r]
    assert note == [(
        "Restored heat to 87 and cool to 95 didn't fit this head's limits 59 to 88"
        " with the minimum gap, so the range was re-centered at 86 to 88."
    )]
    assert not any(
        "re-centered" in r["reason"]
        for r in entry.runtime_data._control_reasons(_zone(entry, 1))
    )
    await _set(hass, entry, "primary_target_low", 80)
    assert not any("re-centered" in r["reason"] for r in entry.runtime_data._control_reasons(_zone(entry, 0)))


async def test_a_restored_celsius_pair_is_re_centered_at_one_degree(hass: HomeAssistant) -> None:
    entry, _ = await _setup(
        hass, "C", temps=(21, 21), restore=[
            ("primary_target", 30.0, None),
            ("primary_target_low", 30.5, None), ("primary_target_high", 35.0, None),
        ], dual=(0,),
    )
    # Clamped to 30.5-31, half a degree apart: re-centered at 1 °C inside 15-31.
    assert _pair(entry) == (30.0, 31.0)
    assert _value(hass, entry, "primary_target") == 30.5


async def test_a_restored_pair_that_fits_is_kept_exactly(hass: HomeAssistant) -> None:
    entry, _ = await _setup(
        hass, dual=(0,), restore=[
            ("primary_target", 70, None),
            ("primary_target_low", 63, None), ("primary_target_high", 79, None),
        ],
    )
    assert _pair(entry) == (63.0, 79.0)
    assert _value(hass, entry, "primary_target") == 71  # the midpoint, not the restored 70
    assert not any(
        "re-centered" in r["reason"] for r in entry.runtime_data._control_reasons(_zone(entry, 0))
    )


# --- rename and reorder ------------------------------------------------------------
async def test_a_reorder_carries_the_range_with_its_room(hass: HomeAssistant) -> None:
    from tests.test_room_rename_reorder import _reconfigure

    entry, heads = await _setup(hass, dual=(0,))
    await _range(hass, entry, "primary", 64, 76)
    low_id = _eid(hass, entry, "primary_target_low")
    high_id = _eid(hass, entry, "primary_target_high")
    registry = er.async_get(hass)
    # The registry records themselves move (same registry id, so the user's
    # customizations stay); a dropped and recreated record would get new ids.
    record_ids = (registry.async_get(low_id).id, registry.async_get(high_id).id)
    await _reconfigure(hass, entry, list(heads[::-1]), [SENSOR_B, SENSOR_A], names=["Bedroom", "Living"])
    zones = entry.runtime_data.zones
    assert [z.climate_id for z in zones] == list(heads[::-1])
    assert zones[1].dual is True and zones[0].dual is False
    assert entry.data[CONF_ZONES][1][ZONE_DUAL_SETPOINT] is True
    assert ZONE_DUAL_SETPOINT not in entry.data[CONF_ZONES][0]
    assert _eid(hass, entry, "secondary_target_low") == low_id
    assert _eid(hass, entry, "secondary_target_high") == high_id
    assert (
        registry.async_get(_eid(hass, entry, "secondary_target_low")).id,
        registry.async_get(_eid(hass, entry, "secondary_target_high")).id,
    ) == record_ids
    assert not _has(hass, entry, "primary_target_low")
    assert _pair(entry, 1) == (64.0, 76.0)


# --- mixed install: the single room is 3.9.0 -------------------------------------------
def _single_room_slice(step: dict[str, Any]) -> dict[str, Any]:
    """Everything the first (single-target) room owns or is sent, plus the shared plan."""
    plan = step["entities"]["plan"]["attributes"]
    head_a = next(h for h in step["heads"] if h.endswith("_a"))
    return {
        "entities": {k: v for k, v in step["entities"].items() if not k.startswith("secondary_") and k != "plan"},
        "plan_state": step["entities"]["plan"]["state"],
        "plan": {k: v for k, v in plan.items() if k != "zones" and not k.startswith("secondary_")},
        "row": plan["zones"][0],
        "head": step["heads"][head_a],
        "calls": [c for c in step["calls"] if c["service_data"]["entity_id"] in (head_a, [head_a])],
        "zone": step["data"]["zones"][0],
        "options": step["options"],
    }


@pytest.mark.parametrize("unit", ["F", "C"])
async def test_mixed_install_single_room_is_3_9_0(hass: HomeAssistant, unit: str) -> None:
    async def second_room_dual(hass: HomeAssistant, entry: MockConfigEntry) -> None:
        await room_page_pass(hass, entry, 2, {"dual_setpoint": True})

    steps = await run_scenario(hass, unit, after_room_pages=second_room_dual)
    pinned = json.loads(PIN.read_text())[unit]
    assert [s["step"] for s in steps] == [s["step"] for s in pinned]
    for seen, want in zip(steps, pinned, strict=True):
        seen, votes = take_vote_rows(seen)  # (added after 3.10.0; see pin_scenario)
        assert votes == [("live", None), ("live", None)], seen["step"]
        assert _single_room_slice(seen) == _single_room_slice(want), seen["step"]
    # And the second room really was dual the whole way through.
    last = steps[-1]
    assert last["data"]["zones"][1][ZONE_DUAL_SETPOINT] is True
    assert "secondary_target_low" in last["entities"]
    assert "target_low" in last["entities"]["plan"]["attributes"]["zones"][1]
    # Before the toggle, the whole install is 3.9.0.
    assert take_vote_rows(steps[0])[0] == pinned[0]
