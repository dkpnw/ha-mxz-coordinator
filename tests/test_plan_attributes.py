"""The plan's public attributes, recorded key by key and in order.

One install with a room of each kind MXZ shows differently: a local head with a
dual setpoint, Comfort priority and a door/window pause; an Allow Dry room; a
hands-off room that votes by its head's action; a disabled room with a freeze
floor; and a cloud head. It is driven through a Dry picked at a head, a call,
a standoff with a floor trip, a door/window pause and a standby hold. After each step the test
records the plan sensor's state and attributes, each room thermostat's state
and attributes, and the ``plan`` section of diagnostics, as JSON with the key
order kept.

``tests/plan_attributes.json`` was captured on main ``3bf558d`` before the plan
became a typed ``Plan``, and the typed plan must render the same bytes. Only ISO
times and the second counters (``seconds_since_mode_change``, ``*sensor_age``)
are replaced, the same way on both sides. The thermostats leave out the keys in
``HA_CORE_CLIMATE_KEYS``: HA's climate base class adds them on some supported
HA versions and not others, and MXZ renders none of them. Run with
``MXZ_PLAN_CAPTURE=1`` to rewrite the file, only on a commit whose attributes
are known to be right.

Every room, head and sensor is invented; nothing here is a hardware result.
"""

from __future__ import annotations

import json
import os
import re
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.mxz_coordinator.climate import MXZRoomClimate
from custom_components.mxz_coordinator.const import (
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_ZONES,
    DOMAIN,
    HEAD_TYPE_CLOUD,
    IDLE_ACTION_FAN_ONLY,
    INHIBIT_ACTION_FAN_ONLY,
    VOTE_BASIS_ACTION,
    ZONE_ALLOW_DRY,
    ZONE_CLIMATE,
    ZONE_COMFORT_PRIORITY_ENTITY,
    ZONE_DUAL_SETPOINT,
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
    ZONE_HANDS_OFF_VOTE_BASIS,
    ZONE_HEAD_TYPE,
    ZONE_NAME,
    ZONE_OPENING_ENTITY,
    ZONE_OPENING_PAUSE,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.cloud_heads import ComfortHead, register
from tests.test_drive import MockHead, _eid, _set_target, _set_temp
from tests.test_dry_mode import DryHead
from tests.test_startup_continuity import _head

GOLDEN = Path(__file__).with_name("plan_attributes.json")
CAPTURE = os.environ.get("MXZ_PLAN_CAPTURE") == "1"

WINDOW = "binary_sensor.study_window"
COMFORT = "binary_sensor.study_occupied"
STANDBY = "input_boolean.standby"
NOTIFY = "plan_test"
ROOMS = ("Study", "Den", "Loft", "Attic", "Hall")
SENSORS = tuple(f"sensor.{name.lower()}_temp" for name in ROOMS)
SLUGS = ("primary", "secondary", "zone_3", "zone_4", "zone_5")

_ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:[+-]\d\d:\d\d|Z)?")
_SECONDS_KEYS = re.compile(r"(^|_)(seconds_since_mode_change|sensor_age)$")

# Thermostat attributes that HA core's ClimateEntity writes by itself, whose
# presence or place depends on the HA version and not on MXZ.
# - temperature_unit: the system unit (hass.config.units). HA 2026.10.4 and older
#   do not write it; 2026.11.0.dev0 adds it to state_attributes after
#   current_temperature; 2026.12.0.dev0 moves it to the front of
#   capability_attributes.
# Leaving a key out keeps every other key and the order of the rest.
HA_CORE_CLIMATE_KEYS = frozenset({"temperature_unit"})


def _normal(value: Any, key: str = "") -> Any:
    """JSON-ready, key order kept, with only the wall-clock and uptime values replaced."""
    if isinstance(value, dict):
        return {str(k): _normal(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normal(v) for v in value]
    if isinstance(value, Enum):
        return _normal(value.value, key)
    if _SECONDS_KEYS.search(key) and isinstance(value, int) and not isinstance(value, bool):
        return "<seconds>"
    if isinstance(value, str):
        return _ISO.sub("<time>", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(f"{key}: {type(value).__name__} is not recorded")


class _House:
    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry, heads: list[Any]) -> None:
        self.hass = hass
        self.entry = entry
        self.heads = heads
        self.steps: dict[str, str] = {}

    async def recompute(self) -> None:
        await self.entry.runtime_data.async_refresh()
        await self.hass.async_block_till_done()

    def _state(self, entity_id: str) -> dict[str, Any]:
        state = self.hass.states.get(entity_id)
        assert state is not None, entity_id
        return {"state": state.state, "attributes": dict(state.attributes)}

    def _thermostat(self, entity_id: str) -> dict[str, Any]:
        """The thermostat's state without the HA-core keys MXZ does not render."""
        own = _head(self.hass, entity_id).extra_state_attributes
        assert not HA_CORE_CLIMATE_KEYS & set(own), f"{entity_id} renders {set(own)}"
        record = self._state(entity_id)
        record["attributes"] = {
            key: value
            for key, value in record["attributes"].items()
            if key not in HA_CORE_CLIMATE_KEYS
        }
        return record

    async def snapshot(self, step: str) -> None:
        registry = er.async_get(self.hass)
        thermostats = {
            slug: next(
                ent.entity_id for ent in registry.entities.values()
                if ent.config_entry_id == self.entry.entry_id
                and ent.unique_id.endswith(f"_{slug}_thermostat")
            )
            for slug in SLUGS
        }
        diagnostics = await async_get_config_entry_diagnostics(self.hass, self.entry)
        record = {
            "plan_sensor": self._state(_eid(self.hass, self.entry, "_plan")),
            "thermostats": {slug: self._thermostat(eid) for slug, eid in thermostats.items()},
            "diagnostics_plan": diagnostics["coordinator"]["plan"],
        }
        self.steps[step] = json.dumps(_normal(record), indent=1)


async def _house(hass: HomeAssistant) -> _House:
    hass.config.units = US_CUSTOMARY_SYSTEM
    study, den, loft, attic = MockHead("a"), DryHead("b"), MockHead("c"), MockHead("d")
    hall = ComfortHead("e", mode=HVACMode.OFF)
    hall.delayed = False
    await register(hass, {"esphome": [study, den, loft, attic], "kumo_cloud": [hall]})
    for sensor in SENSORS:
        await _set_temp(hass, sensor, 70)
    hass.states.async_set(WINDOW, "off", {"friendly_name": "Study window"})
    hass.states.async_set(COMFORT, "on")
    hass.states.async_set(STANDBY, "off")
    async_mock_service(hass, "notify", NOTIFY)
    extra: list[dict[str, Any]] = [
        {
            ZONE_DUAL_SETPOINT: True,
            ZONE_COMFORT_PRIORITY_ENTITY: COMFORT,
            ZONE_OPENING_ENTITY: WINDOW,
            ZONE_OPENING_PAUSE: True,
        },
        {ZONE_ALLOW_DRY: True},
        {ZONE_HANDS_OFF: True, ZONE_HANDS_OFF_VOTE_BASIS: VOTE_BASIS_ACTION},
        {ZONE_FLOOR_LOW: 50},
        {ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD},
    ]
    heads = [study, den, loft, attic, hall]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_ZONES: [
                {ZONE_NAME: name, ZONE_CLIMATE: head.entity_id, ZONE_SENSOR: sensor, **keys}
                for name, head, sensor, keys in zip(ROOMS, heads, SENSORS, extra, strict=True)
            ],
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_MODE_HYSTERESIS: 0,
            CONF_NOTIFY_SERVICE: f"notify.{NOTIFY}",
            CONF_INHIBIT_ENTITY: STANDBY,
            CONF_INHIBIT_ACTION: INHIBIT_ACTION_FAN_ONLY,
        },
        version=2,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for slug in ("primary", "secondary", "zone_5"):
        if slug != "primary":
            await _set_target(hass, _eid(hass, entry, f"_{slug}_target"), 70)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, f"_{slug}_enable")}, blocking=True
        )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_coordinator_enable")}, blocking=True
    )
    await hass.async_block_till_done()
    house = _House(hass, entry, heads)
    await house.recompute()
    return house


async def _person(hass: HomeAssistant, entity_id: str, mode: str, action: str | None = None) -> None:
    head = _head(hass, entity_id)
    head._attr_hvac_mode = mode
    if action is not None:
        head._attr_hvac_action = action
    head.async_write_ha_state()
    await hass.async_block_till_done()


async def _walk(hass: HomeAssistant, freezer) -> dict[str, str]:
    house = await _house(hass)
    await house.snapshot("start")

    await _person(hass, house.heads[1].entity_id, HVACMode.DRY)
    await _set_temp(hass, SENSORS[0], 77)
    await house.recompute()
    await house.snapshot("dry_and_a_call")

    await _person(hass, house.heads[2].entity_id, HVACMode.HEAT, "heating")
    await _set_temp(hass, SENSORS[3], 45)
    await house.recompute()
    await house.snapshot("standoff_and_floor")

    hass.states.async_set(WINDOW, "on", {"friendly_name": "Study window"})
    await hass.async_block_till_done()
    freezer.tick(61)
    await house.recompute()
    await house.snapshot("window_paused")

    hass.states.async_set(STANDBY, "on")
    await hass.async_block_till_done()
    await house.recompute()
    await house.snapshot("standby")
    return house.steps


async def test_the_plan_attributes_are_unchanged(hass: HomeAssistant, freezer) -> None:
    steps = await _walk(hass, freezer)
    if CAPTURE:
        GOLDEN.write_text(json.dumps(steps, indent=1) + "\n")
    recorded = json.loads(GOLDEN.read_text())
    assert list(recorded) == list(steps)
    for step, rendered in steps.items():
        assert rendered == recorded[step], step


async def test_the_recording_catches_a_reordered_key(hass: HomeAssistant, freezer) -> None:
    """The comparison is by bytes: the same attributes in another order fail it.

    The walk as rendered matches first, so the reorder alone is what fails. The
    thermostat is checked too, since its record leaves the HA-core keys out.
    """
    steps = await _walk(hass, freezer)
    recorded = json.loads(GOLDEN.read_text())
    assert steps["start"] == recorded["start"]
    for path in (("plan_sensor",), ("thermostats", "primary")):
        first = json.loads(steps["start"])
        record = first
        for part in path:
            record = record[part]
        attributes = record["attributes"]
        swapped = dict(reversed(list(attributes.items())))
        assert swapped == attributes
        assert list(swapped) != list(attributes)
        record["attributes"] = swapped
        assert json.dumps(first, indent=1) != recorded["start"], path


async def test_mxz_cannot_hide_a_key_behind_the_ha_core_list(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key on HA_CORE_CLIMATE_KEYS that MXZ renders itself fails the recording."""
    house = await _house(hass)
    rendered = MXZRoomClimate.extra_state_attributes.fget
    monkeypatch.setattr(
        MXZRoomClimate,
        "extra_state_attributes",
        property(lambda self: {**rendered(self), "temperature_unit": "°F"}),
    )
    with pytest.raises(AssertionError, match="renders"):
        await house.snapshot("start")
