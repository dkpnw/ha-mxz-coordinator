"""Turning a room's dual setpoint on or off applies in place, without a reload.

Found on hardware (HA 2026.10.2, Oct 23, 2026): the options save reloaded the
entry, so the room thermostat went heat_cool -> unavailable -> heat_cool, and
only the reborn entity had the range feature. HA's HomeKit bridge rebuilds an
accessory on a supported_features change only when neither the old nor the
new state is unavailable, so Apple Home kept the single-target accessory.

These tests drive the real options flow (Configure, Rooms, the room, Dual
setpoint) and record every state_changed event for the room's entities. The
HomeKit rule is mimicked from HA's own source, because no lane installs
HAP-python (``pyhap``) and HA's HomeKit bridge can't be imported without it:
``homekit/accessories.py``, ``HomeAccessory.async_update_event_state_callback``,
read in HA 2024.12.0, 2026.2.3, 2026.9.0, and 2026.10.0 (the same rule in all
four), with ``RELOAD_ON_CHANGE_ATTRS`` = supported_features, device_class, and
unit_of_measurement.

Requires pytest-homeassistant-custom-component (Python 3.12+).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import (
    CONF_DEMAND_THRESHOLD,
    CONF_ZONES,
    DOMAIN,
    DUAL_HANDOFF_KEY,
    ZONE_DRIFT_COOLER_BY,
    ZONE_DUAL_SETPOINT,
    ZONE_STAGE_SENSOR,
    ZONE_VANE_HORIZONTAL,
    ZONE_VANE_VERTICAL,
)
from tests.flow_pages import open_options_page, prefilled, submit
from tests.pin_scenario import room_page_pass
from tests.test_dual_setpoint import (
    _call,
    _compute,
    _eid,
    _has,
    _pair,
    _range,
    _row,
    _run,
    _set,
    _setup,
    _start,
    _state,
    _value,
    _zone,
)

# The mock heads offer fan modes and no vanes, so a room thermostat is
# TARGET_TEMPERATURE | FAN_MODE | TURN_OFF | TURN_ON (393) with one target,
# and TARGET_TEMPERATURE_RANGE in place of TARGET_TEMPERATURE (394) as a range.
SINGLE = 393
RANGE = 394
assert SINGLE == int(
    ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE
    | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
)
assert RANGE == SINGLE - ClimateEntityFeature.TARGET_TEMPERATURE + ClimateEntityFeature.TARGET_TEMPERATURE_RANGE

# homekit/accessories.py RELOAD_ON_CHANGE_ATTRS.
HOMEKIT_RELOAD_ON_CHANGE_ATTRS = ("supported_features", "device_class", "unit_of_measurement")


def homekit_would_rebuild(old: State | None, new: State | None) -> bool:
    """HA HomeKit's rule for rebuilding an accessory on a state_changed event.

    Both states present, neither unavailable, and one of the reload-on-change
    attributes differs. Otherwise the accessory only updates its values, and
    keeps the characteristics it was built with.
    """
    return bool(
        new and old
        and STATE_UNAVAILABLE not in (old.state, new.state)
        and any(
            old.attributes.get(attr) != new.attributes.get(attr)
            for attr in HOMEKIT_RELOAD_ON_CHANGE_ATTRS
        )
    )


class StateLog:
    """Every state_changed event for the given entities, in order."""

    def __init__(self, hass: HomeAssistant, entity_ids: list[str]) -> None:
        self.events: list[tuple[str, State | None, State | None]] = []
        watched = set(entity_ids)

        @callback
        def _record(event: Event) -> None:
            if event.data["entity_id"] in watched:
                self.events.append(
                    (event.data["entity_id"], event.data["old_state"], event.data["new_state"])
                )

        self.stop = hass.bus.async_listen(EVENT_STATE_CHANGED, _record)

    def of(self, entity_id: str) -> list[tuple[State | None, State | None]]:
        return [(old, new) for eid, old, new in self.events if eid == entity_id]

    def feature_changes(self, entity_id: str) -> list[tuple[State, State]]:
        return [
            (old, new) for old, new in self.of(entity_id)
            if old is not None and new is not None
            and old.attributes.get("supported_features") != new.attributes.get("supported_features")
        ]

    def homekit_rebuilds(self, entity_id: str) -> int:
        return sum(homekit_would_rebuild(old, new) for old, new in self.of(entity_id))

    def went_away(self, entity_id: str) -> bool:
        """True if the entity was ever removed or unavailable/unknown on the way."""
        return any(
            new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            for old, new in self.of(entity_id)
        )


def _entity(hass: HomeAssistant, entity_id: str):
    return hass.data[DATA_INSTANCES][entity_id.split(".")[0]].get_entity(entity_id)


def _entry_entities(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    """entity_id -> registry id for every entity the entry has registered."""
    return {
        e.entity_id: e.id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


async def _room_page(hass: HomeAssistant, entry: MockConfigEntry, room: int, **changes: Any) -> None:
    result = await room_page_pass(hass, entry, room, changes)
    assert result["type"] == "create_entry", result


async def _comfort_page(hass: HomeAssistant, entry: MockConfigEntry, **changes: Any) -> None:
    result = await open_options_page(hass, entry, "comfort")
    result = await submit(hass.config_entries.options, result, {**prefilled(result), **changes})
    await hass.async_block_till_done()
    assert result["type"] == "create_entry", result


# --- (1)-(3) the switch keeps every entity in place -----------------------------------
@pytest.mark.parametrize(
    ("unit", "target", "seeded", "pair", "midpoint"),
    [
        ("F", 70.0, (69.0, 71.0), (68.0, 73.0), 70.0),
        ("C", 21.0, (20.5, 21.5), (20.0, 21.5), 21.0),
    ],
)
async def test_switching_on_and_off_never_makes_the_thermostat_unavailable(
    hass: HomeAssistant, unit: str, target: float, seeded: tuple[float, float],
    pair: tuple[float, float], midpoint: float,
) -> None:
    entry, _ = await _setup(hass, unit)
    await _set(hass, entry, "primary_target", target)
    coordinator = entry.runtime_data
    tile_id = _eid(hass, entry, "primary_thermostat")
    tile = _entity(hass, tile_id)
    before = _entry_entities(hass, entry)
    assert _state(hass, entry, "primary_thermostat").attributes["supported_features"] == SINGLE
    log = StateLog(hass, list(before))

    await _room_page(hass, entry, 1, dual_setpoint=True)

    assert entry.data[CONF_ZONES][0][ZONE_DUAL_SETPOINT] is True
    # No reload: the same coordinator, the same entity object, the same registry records.
    assert entry.runtime_data is coordinator
    assert _entity(hass, tile_id) is tile
    after_on = _entry_entities(hass, entry)
    assert {k: v for k, v in after_on.items() if k in before} == before
    assert set(after_on) - set(before) == {
        _eid(hass, entry, "primary_target_low"), _eid(hass, entry, "primary_target_high")
    }
    # Every entity that was there stayed available the whole time.
    for entity_id in before:
        assert not log.went_away(entity_id), entity_id
        assert all(old is not None for old, _ in log.of(entity_id)), entity_id
    # The features changed on exactly one state write, available to available,
    # and that write already carries the range (and no single target).
    (old, new), = log.feature_changes(tile_id)
    assert (old.attributes["supported_features"], new.attributes["supported_features"]) == (SINGLE, RANGE)
    assert old.state == new.state == "off"
    assert old.attributes["temperature"] == target
    assert "target_temp_low" not in old.attributes
    assert (new.attributes["target_temp_low"], new.attributes["target_temp_high"]) == seeded
    assert "temperature" not in new.attributes
    assert log.homekit_rebuilds(tile_id) == 1
    # The new numbers, seeded around the target.
    assert _value(hass, entry, "primary_target_low") == seeded[0]
    assert _value(hass, entry, "primary_target_high") == seeded[1]
    assert _value(hass, entry, "primary_target") == target
    assert (_row(entry, 0)["target_low"], _row(entry, 0)["target_high"]) == seeded

    await _range(hass, entry, "primary", *pair)
    low_id, high_id = _eid(hass, entry, "primary_target_low"), _eid(hass, entry, "primary_target_high")
    log.stop()
    log = StateLog(hass, list(after_on))

    await _room_page(hass, entry, 1, dual_setpoint=False)

    assert ZONE_DUAL_SETPOINT not in entry.data[CONF_ZONES][0]
    assert entry.runtime_data is coordinator
    assert _entity(hass, tile_id) is tile
    assert _entry_entities(hass, entry) == before
    for entity_id in before:
        assert not log.went_away(entity_id), entity_id
    (old, new), = log.feature_changes(tile_id)
    assert (old.attributes["supported_features"], new.attributes["supported_features"]) == (RANGE, SINGLE)
    assert (old.attributes["target_temp_low"], old.attributes["target_temp_high"]) == pair
    assert new.attributes["temperature"] == midpoint
    assert "target_temp_low" not in new.attributes and "target_temp_high" not in new.attributes
    assert log.homekit_rebuilds(tile_id) == 1
    assert _value(hass, entry, "primary_target") == midpoint
    assert _zone(entry, 0).target == midpoint
    # The heat-to and cool-to numbers are gone: state and registry, and they
    # sit in the registry's deleted list as the reload's prune left them.
    for entity_id, suffix in ((low_id, "primary_target_low"), (high_id, "primary_target_high")):
        assert hass.states.get(entity_id) is None
        assert not _has(hass, entry, suffix)
        assert ("number", DOMAIN, f"{entry.entry_id}_{suffix}") in er.async_get(hass).deleted_entities
        assert [new for _, new in log.of(entity_id)] == [None]  # removed, never unavailable first
    assert "target_low" not in _row(entry, 0)
    log.stop()


async def test_the_other_room_is_untouched_by_the_switch(hass: HomeAssistant) -> None:
    entry, _ = await _setup(hass)
    other = [_eid(hass, entry, s) for s in ("secondary_thermostat", "secondary_target", "secondary_drift")]
    log = StateLog(hass, other)
    await _room_page(hass, entry, 1, dual_setpoint=True)
    await _room_page(hass, entry, 1, dual_setpoint=False)
    for entity_id in other:
        assert not log.went_away(entity_id), entity_id
        assert log.feature_changes(entity_id) == []
    log.stop()


async def test_a_switch_moves_the_thermostats_registry_features_once(hass: HomeAssistant) -> None:
    """The registry update HA makes on a feature change is what cloud Google
    (``cloud/google_config.py``) and cloud Alexa (``cloud/alexa_config.py``)
    re-sync on: an ``update`` whose changes include supported_features."""
    entry, _ = await _setup(hass)
    tile_id = _eid(hass, entry, "primary_thermostat")
    registry = er.async_get(hass)
    record = registry.async_get(tile_id)
    assert record.supported_features == SINGLE
    seen: list[dict[str, Any]] = []

    @callback
    def _record(event: Event) -> None:
        if event.data.get("entity_id") == tile_id:
            seen.append(dict(event.data))

    stop = hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _record)
    await _room_page(hass, entry, 1, dual_setpoint=True)
    assert [e["action"] for e in seen] == ["update"]
    assert "supported_features" in seen[0]["changes"]
    assert registry.async_get(tile_id).supported_features == RANGE
    assert registry.async_get(tile_id).id == record.id
    assert registry.async_get(tile_id).capabilities == record.capabilities
    seen.clear()
    await _room_page(hass, entry, 1, dual_setpoint=False)
    assert [e["action"] for e in seen] == ["update"]
    assert "supported_features" in seen[0]["changes"]
    assert registry.async_get(tile_id).supported_features == SINGLE
    stop()


async def test_writes_follow_the_switch(hass: HomeAssistant) -> None:
    """The same thermostat and target number take range writes once switched on,
    and single-target writes again once switched off."""
    entry, _ = await _setup(hass)
    tile_id = _eid(hass, entry, "primary_thermostat")
    await _room_page(hass, entry, 1, dual_setpoint=True)
    await _range(hass, entry, "primary", 66, 72)
    assert _pair(entry) == (66.0, 72.0)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "climate", "set_temperature", {"entity_id": tile_id, "temperature": 70})
    await _set(hass, entry, "primary_target", 71)  # moves the range, keeping its width
    assert _pair(entry) == (68.0, 74.0)
    assert _value(hass, entry, "primary_target_low") == 68

    await _room_page(hass, entry, 1, dual_setpoint=False)
    assert _zone(entry, 0).target == 71
    with pytest.raises(ServiceValidationError):
        await _range(hass, entry, "primary", 66, 72)
    await _call(hass, "climate", "set_temperature", {"entity_id": tile_id, "temperature": 67})
    assert _value(hass, entry, "primary_target") == 67
    assert _zone(entry, 0).target == 67
    assert _pair(entry) == (None, None)
    await _set(hass, entry, "primary_target", 65)
    assert _state(hass, entry, "primary_thermostat").attributes["temperature"] == 65


async def test_google_sees_the_range_after_the_switch(hass: HomeAssistant) -> None:
    from tests.test_dual_setpoint_bridges import _google

    entry, _ = await _setup(hass)
    await _call(hass, "climate", "turn_on", {"entity_id": _eid(hass, entry, "primary_thermostat")})
    await _room_page(hass, entry, 1, dual_setpoint=True)
    await _range(hass, entry, "primary", 68, 74)
    trait_mod, trait = _google(hass, entry, "primary")
    features = _state(hass, entry, "primary_thermostat").attributes["supported_features"]
    assert trait_mod.TemperatureSettingTrait.supported("climate", features, None, None)
    query = trait.query_attributes()
    assert query["thermostatTemperatureSetpointLow"] == 20.0  # 68 °F
    assert query["thermostatTemperatureSetpointHigh"] == 23.3  # 74 °F
    await _room_page(hass, entry, 1, dual_setpoint=False)
    _, trait = _google(hass, entry, "primary")
    query = trait.query_attributes()
    # One target again (71), reported as both ends, as in 3.9.0.
    assert query["thermostatTemperatureSetpointLow"] == query["thermostatTemperatureSetpointHigh"] == 21.7


async def test_a_switch_is_recomputed_at_once(hass: HomeAssistant) -> None:
    """A target write just before the switch puts the refresh debounce in its
    cooldown; the switch still recomputes the plan at once, as the reload did."""
    entry, _ = await _setup(hass, temps=(66, 70))
    await _start(hass, entry)
    await _set(hass, entry, "primary_target", 66)
    await _room_page(hass, entry, 1, dual_setpoint=True)
    row = _row(entry, 0)
    assert (row["target_low"], row["target_high"]) == (65.0, 67.0)
    assert (row["heat_start"], row["cool_start"]) == (64.0, 68.0)


async def test_the_thermostat_shows_the_range_before_the_numbers_appear(hass: HomeAssistant) -> None:
    """The switch's own state write: the thermostat's new features come before
    the heat-to number's first state, not with the refresh that follows."""
    entry, _ = await _setup(hass)
    tile_id = _eid(hass, entry, "primary_thermostat")
    low_id = er.async_get(hass).async_get_or_create(
        "number", DOMAIN, f"{entry.entry_id}_primary_target_low", config_entry=entry
    ).entity_id
    log = StateLog(hass, [tile_id, low_id])
    await _room_page(hass, entry, 1, dual_setpoint=True)
    (old, new), = log.feature_changes(tile_id)
    order = [(entity_id, new_state) for entity_id, _, new_state in log.events]
    assert order.index((tile_id, new)) < [entity_id for entity_id, _ in order].index(low_id), order
    log.stop()


async def test_without_the_number_platform_callback_the_save_reloads(hass: HomeAssistant) -> None:
    """Nowhere to add the new numbers: the save reloads, and the options
    flow's handoff seeds the range exactly as on f01252f."""
    entry, _ = await _setup(hass)
    await _set(hass, entry, "primary_target", 70)
    coordinator = entry.runtime_data
    coordinator.add_range_numbers = None
    tile_id = _eid(hass, entry, "primary_thermostat")
    log = StateLog(hass, [tile_id])
    await _room_page(hass, entry, 1, dual_setpoint=True)
    assert entry.runtime_data is not coordinator
    assert log.went_away(tile_id)
    assert _zone(entry, 0).dual is True
    assert _pair(entry) == (69.0, 71.0)
    assert _value(hass, entry, "primary_target_low") == 69
    assert _value(hass, entry, "primary_target_high") == 71
    assert not hass.data.get(DUAL_HANDOFF_KEY)
    log.stop()


# --- a room whose stored wiring is empty -----------------------------------------------
# The config flow stores a vane or stage key as None when it detects none
# (config_flow._build_zones), and the room page drops an empty key on save. So
# a room's first save drops it, which is no change to the room.
@pytest.mark.parametrize("stored", [None, ""])
@pytest.mark.parametrize("key", [ZONE_VANE_VERTICAL, ZONE_VANE_HORIZONTAL, ZONE_STAGE_SENSOR])
async def test_a_rooms_first_switch_is_in_place_with_empty_wiring_stored(
    hass: HomeAssistant, key: str, stored: str | None,
) -> None:
    entry, _ = await _setup(hass, zone_keys={key: stored})
    await _set(hass, entry, "primary_target", 70)
    coordinator = entry.runtime_data
    tiles = [_eid(hass, entry, s) for s in ("primary_thermostat", "secondary_thermostat")]
    objs = [_entity(hass, t) for t in tiles]
    log = StateLog(hass, tiles)

    await _room_page(hass, entry, 1, dual_setpoint=True)

    assert key not in entry.data[CONF_ZONES][0]  # the save dropped it
    assert entry.data[CONF_ZONES][1][key] == stored  # the other room's is untouched
    assert entry.runtime_data is coordinator
    assert _entity(hass, tiles[0]) is objs[0]
    assert not log.went_away(tiles[0])
    (old, new), = log.feature_changes(tiles[0])
    assert (old.attributes["supported_features"], new.attributes["supported_features"]) == (SINGLE, RANGE)
    assert log.homekit_rebuilds(tiles[0]) == 1
    assert _pair(entry) == (69.0, 71.0)

    # The other room's first save drops its key too, and is in place as well.
    await _room_page(hass, entry, 2, dual_setpoint=True)
    assert key not in entry.data[CONF_ZONES][1]
    assert entry.runtime_data is coordinator
    assert _entity(hass, tiles[1]) is objs[1]
    assert not log.went_away(tiles[1])
    assert log.homekit_rebuilds(tiles[1]) == 1
    log.stop()


async def test_a_room_set_up_through_the_config_flow_switches_in_place_first_time(
    hass: HomeAssistant,
) -> None:
    """The entry the setup flow creates (no vane or stage sensor detected on
    the mock heads, so those keys are stored as None), then the first room's
    first save with only the toggle changed."""
    from homeassistant import config_entries
    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

    from tests.flow_pages import press
    from tests.test_drive import MockHead, _setup_mock_heads
    from tests.test_dual_setpoint import SENSOR_A, SENSOR_B, _temps

    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = await _setup_mock_heads(hass, cls=MockHead)
    await _temps(hass, 70, 70)
    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await manager.async_configure(result["flow_id"], {"heads": list(heads)})
    result = await manager.async_configure(result["flow_id"], {})
    result = await manager.async_configure(
        result["flow_id"], {"sensor_1": SENSOR_A, "sensor_2": SENSOR_B}
    )
    result = await press(manager, result, "finish")
    await hass.async_block_till_done()
    assert result["type"] == "create_entry", result
    entry = result["result"]
    stored = entry.data[CONF_ZONES][0]
    assert (stored[ZONE_VANE_VERTICAL], stored[ZONE_VANE_HORIZONTAL], stored[ZONE_STAGE_SENSOR]) == (
        None, None, None
    )
    coordinator = entry.runtime_data
    tile_id = _eid(hass, entry, "primary_thermostat")
    tile = _entity(hass, tile_id)
    log = StateLog(hass, [tile_id])

    await _room_page(hass, entry, 1, dual_setpoint=True)

    assert entry.data[CONF_ZONES][0][ZONE_DUAL_SETPOINT] is True
    assert entry.runtime_data is coordinator
    assert _entity(hass, tile_id) is tile
    assert not log.went_away(tile_id)
    (old, new), = log.feature_changes(tile_id)
    assert (old.attributes["supported_features"], new.attributes["supported_features"]) == (SINGLE, RANGE)
    assert log.homekit_rebuilds(tile_id) == 1
    log.stop()


@pytest.mark.parametrize("switch", [False, True])
@pytest.mark.parametrize(
    ("field", "key", "wired"),
    [
        ("vane_vertical", ZONE_VANE_VERTICAL, "select.living_vane_vertical"),
        ("vane_horizontal", ZONE_VANE_HORIZONTAL, "select.living_vane_horizontal"),
        ("stage", ZONE_STAGE_SENSOR, "sensor.living_stage"),
    ],
)
async def test_wiring_a_room_with_empty_wiring_stored_still_reloads(
    hass: HomeAssistant, field: str, key: str, wired: str, switch: bool,
) -> None:
    """Only an empty value is the same as no key: wiring one in is a real
    change, so the save reloads, with or without the switch."""
    entry, _ = await _setup(hass, zone_keys={key: None})
    coordinator = entry.runtime_data
    tile_id = _eid(hass, entry, "primary_thermostat")
    log = StateLog(hass, [tile_id])
    await _room_page(hass, entry, 1, dual_setpoint=switch, **{field: wired})
    assert entry.data[CONF_ZONES][0][key] == wired
    assert entry.runtime_data is not coordinator
    assert log.went_away(tile_id)
    assert log.homekit_rebuilds(tile_id) == 0
    assert _zone(entry, 0).dual is switch
    if switch:
        assert _pair(entry) == (69.0, 71.0)  # the reload's handoff seeds it
    assert not hass.data.get(DUAL_HANDOFF_KEY)
    log.stop()


# --- (4) every other change still reloads ---------------------------------------------
@pytest.mark.parametrize("dual", [False, True])
async def test_a_room_drift_change_still_reloads(hass: HomeAssistant, dual: bool) -> None:
    entry, _ = await _setup(hass, dual=(0,) if dual else ())
    if dual:
        await _range(hass, entry, "primary", 64, 76)
    coordinator = entry.runtime_data
    tile_id = _eid(hass, entry, "primary_thermostat")
    log = StateLog(hass, [tile_id])
    await _room_page(hass, entry, 1, room_cooler_by=2.0)
    assert entry.data[CONF_ZONES][0][ZONE_DRIFT_COOLER_BY] == 2.0
    assert entry.runtime_data is not coordinator  # reloaded
    assert log.went_away(tile_id)  # through unavailable, exactly as before
    assert log.homekit_rebuilds(tile_id) == 0
    if dual:
        assert _pair(entry) == (64.0, 76.0)  # restored, not re-seeded
    log.stop()


async def test_a_global_options_change_still_reloads(hass: HomeAssistant) -> None:
    entry, _ = await _setup(hass, dual=(0,))
    await _range(hass, entry, "primary", 64, 76)
    coordinator = entry.runtime_data
    tile_id = _eid(hass, entry, "primary_thermostat")
    log = StateLog(hass, [tile_id])
    await _comfort_page(hass, entry, demand_threshold=4.0)
    assert entry.options[CONF_DEMAND_THRESHOLD] == 4.0
    assert entry.runtime_data is not coordinator
    assert log.went_away(tile_id)
    assert _pair(entry) == (64.0, 76.0)
    log.stop()


# --- (5) a save that mixes the switch with another change reloads ----------------------
async def test_a_mixed_save_reloads_and_still_switches(hass: HomeAssistant) -> None:
    """Documented: the room page saved with the switch AND another change takes
    the reload (the handoff then seeds or sets the midpoint, as on f01252f).
    The thermostat passes through unavailable, so HomeKit keeps the old
    accessory until its bridge is reloaded or HA restarts."""
    entry, _ = await _setup(hass)
    await _set(hass, entry, "primary_target", 70)
    coordinator = entry.runtime_data
    tile_id = _eid(hass, entry, "primary_thermostat")
    log = StateLog(hass, [tile_id])

    await _room_page(hass, entry, 1, dual_setpoint=True, room_cooler_by=2.0)

    zone = entry.data[CONF_ZONES][0]
    assert zone[ZONE_DUAL_SETPOINT] is True and zone[ZONE_DRIFT_COOLER_BY] == 2.0
    assert entry.runtime_data is not coordinator
    assert log.went_away(tile_id)
    assert log.homekit_rebuilds(tile_id) == 0
    assert _state(hass, entry, "primary_thermostat").attributes["supported_features"] == RANGE
    assert _pair(entry) == (69.0, 71.0)
    assert _value(hass, entry, "primary_target_low") == 69
    assert not hass.data.get(DUAL_HANDOFF_KEY)

    await _range(hass, entry, "primary", 68, 73)
    coordinator = entry.runtime_data
    await _room_page(hass, entry, 1, dual_setpoint=False, room_cooler_by=1.5)
    assert entry.runtime_data is not coordinator
    assert _value(hass, entry, "primary_target") == 70  # 70.5 snapped (half to even)
    assert not _has(hass, entry, "primary_target_low")
    assert _state(hass, entry, "primary_thermostat").attributes["supported_features"] == SINGLE
    assert not hass.data.get(DUAL_HANDOFF_KEY)
    log.stop()


# --- the in-place switch leaves nothing behind for a later reload ---------------------
async def test_a_later_reload_restores_what_the_switch_left(hass: HomeAssistant) -> None:
    """The options flow's one-shot note is meant for a reload. Applied in place,
    it must not be left for a later, unrelated reload to act on: there it would
    re-seed a range the user set, or replace a target the user set."""
    entry, _ = await _setup(hass)
    await _room_page(hass, entry, 1, dual_setpoint=True)
    assert not hass.data.get(DUAL_HANDOFF_KEY)
    await _range(hass, entry, "primary", 64, 76)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _pair(entry) == (64.0, 76.0)
    assert _value(hass, entry, "primary_target_low") == 64

    await _room_page(hass, entry, 1, dual_setpoint=False)
    assert not hass.data.get(DUAL_HANDOFF_KEY)
    assert _value(hass, entry, "primary_target") == 70
    await _set(hass, entry, "primary_target", 66)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _value(hass, entry, "primary_target") == 66
    assert _zone(entry, 0).dual is False


async def test_switching_back_on_seeds_again_and_reuses_the_entity_ids(hass: HomeAssistant) -> None:
    entry, _ = await _setup(hass)
    await _room_page(hass, entry, 1, dual_setpoint=True)
    ids = (_eid(hass, entry, "primary_target_low"), _eid(hass, entry, "primary_target_high"))
    await _range(hass, entry, "primary", 64, 76)
    await _room_page(hass, entry, 1, dual_setpoint=False)
    await _set(hass, entry, "primary_target", 66)
    await _room_page(hass, entry, 1, dual_setpoint=True)
    assert (_eid(hass, entry, "primary_target_low"), _eid(hass, entry, "primary_target_high")) == ids
    assert _pair(entry) == (65.0, 67.0)  # around 66, never the old 64-76
    assert _value(hass, entry, "primary_target_low") == 65
    assert _value(hass, entry, "primary_target_high") == 67


async def test_switching_on_in_place_after_a_reload_removed_the_numbers_still_seeds(
    hass: HomeAssistant,
) -> None:
    """A reload (here a mixed save) removes the numbers while they still hold
    the range, so HA keeps 64 and 76 to restore if they come back while it
    runs. Switched on again in place, the room still seeds around its target."""
    entry, _ = await _setup(hass)
    await _room_page(hass, entry, 1, dual_setpoint=True)
    await _range(hass, entry, "primary", 64, 76)
    await _room_page(hass, entry, 1, dual_setpoint=False, room_cooler_by=2.0)
    assert not _has(hass, entry, "primary_target_low")
    await _set(hass, entry, "primary_target", 66)
    coordinator = entry.runtime_data
    await _room_page(hass, entry, 1, dual_setpoint=True)
    assert entry.runtime_data is coordinator  # in place
    assert _pair(entry) == (65.0, 67.0)
    assert _value(hass, entry, "primary_target_low") == 65
    assert _value(hass, entry, "primary_target_high") == 67


async def test_a_mid_run_switch_takes_a_fresh_decision_and_spares_the_other_room(
    hass: HomeAssistant,
) -> None:
    """The switched room decides afresh (as the reload left it); the other
    room's run carries on untouched."""
    entry, _ = await _setup(hass, temps=(72, 70))
    await _set(hass, entry, "primary_target", 72)
    await _set(hass, entry, "secondary_target", 72)
    await _start(hass, entry)
    await _run(hass, entry, 68, 68)
    plan = entry.runtime_data.data
    assert (plan["primary_engage"], plan["secondary_engage"]) == ("heat", "heat")
    await _run(hass, entry, 70.5, 71.5)  # inside the drift: only the latch says heat
    await _room_page(hass, entry, 1, dual_setpoint=True)
    assert _pair(entry) == (71.0, 73.0)
    await _compute(hass, entry)
    plan = entry.runtime_data.data
    assert (plan["primary_engage"], plan["secondary_engage"]) == ("satisfied", "heat")
