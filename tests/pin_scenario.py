"""A scripted 3.9.0 scenario whose every output is recorded (the off-means-3.9.0 pin).

``run_scenario`` drives one install through heat, cool (including a cool run
a few tenths above the target), idle, a restart with restore, a two-room
standoff, fan auto, eco, both lockouts, the freeze floor, hands-off and a
target write, in °F or °C, using only what a person or an
automation can reach: services, the options flow and sensor reports. After
every step it records every entity the entry owns (state and full attribute
dict), the entity registry's unique IDs, the stored entry data and options,
and every climate service call made to a head since the step before.

``tests/pin_3_9_0.json`` holds that record captured on main ``abd946e``
(3.9.0); ``tests/test_dual_setpoint_pin.py`` replays it. Run that test with
``MXZ_PIN_CAPTURE=1`` to rewrite the file (only ever on 3.9.0 itself).

Only values that change with the wall clock or the host's uptime are
normalized, the same way on both sides: ISO timestamps (``<time>``) and the
integer second counters ``seconds_since_mode_change`` and ``*sensor_age``
(``<seconds>``). Nothing else is filtered.

The second room is driven only at temperatures that sit far outside any
comfort band or exactly on its target, so a room switched to a dual setpoint
there makes the same calls as the single target it replaced. That lets the
mixed-install test hold the first room's outputs against this same record.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from enum import Enum
from typing import Any

from homeassistant.components.climate import const as climate_const
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
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
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
    ZONE_NAME,
    ZONE_SENSOR,
)
from tests.dwell import expire_dwell
from tests.flow_pages import open_options_page, prefilled, submit
from tests.test_drive import MockHead, MockHeadC, _setup_mock_heads

SENSOR_A = "sensor.room_a_temp"
SENSOR_B = "sensor.room_b_temp"

# Per unit: the restored targets, then each step's two sensor readings. The
# first room does the interesting things; the second is only ever far off its
# target (60/80 °F, 8/27 °C) or exactly on it (70 °F, 21 °C).
UNITS: dict[str, dict[str, Any]] = {
    "F": {
        "system": US_CUSTOMARY_SYSTEM,
        "head": MockHead,
        "targets": (68.0, 70.0),
        "target_write": 72.0,
        "floor_low": 50.0,
        "temps": {
            "boot": (68, 70),
            "heat": (63, 70),
            "heat_run": (67.5, 70),
            "heat_done": (68, 70),
            "cool": (74, 70),
            "cool_run": (68.3, 70),
            "idle": (68, 70),
            "standoff": (63, 80),
            "heat_lockout_held": (63, 70),
            "heat_lockout_floor": (56, 70),
            "cool_lockout_held": (74, 70),
            "cool_lockout_ceiling": (82, 70),
            "freeze_floor": (68, 45),
            "freeze_floor_clear": (68, 70),
        },
    },
    "C": {
        "system": METRIC_SYSTEM,
        "head": MockHeadC,
        "targets": (20.0, 21.0),
        "target_write": 22.0,
        "floor_low": 10.0,
        "temps": {
            "boot": (20, 21),
            "heat": (18, 21),
            "heat_run": (19.7, 21),
            "heat_done": (20, 21),
            "cool": (22, 21),
            "cool_run": (20.2, 21),
            "idle": (20, 21),
            "standoff": (18, 27),
            "heat_lockout_held": (18, 21),
            "heat_lockout_floor": (13, 21),
            "cool_lockout_held": (22, 21),
            "cool_lockout_ceiling": (28, 21),
            "freeze_floor": (20, 8),
            "freeze_floor_clear": (20, 21),
        },
    },
}

_ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:[+-]\d\d:\d\d|Z)?")
_SECONDS_KEYS = re.compile(r"(^|_)(seconds_since_mode_change|sensor_age)$")


def normalize(value: Any, key: str = "") -> Any:
    """JSON-ready, with only the wall-clock and uptime values replaced."""
    if isinstance(value, dict):
        return {str(k): normalize(v, str(k)) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, Enum):
        return normalize(value.value, key)
    if _SECONDS_KEYS.search(key) and isinstance(value, int) and not isinstance(value, bool):
        return "<seconds>"
    if isinstance(value, str):
        return _ISO.sub("<time>", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _ISO.sub("<time>", str(value))


class Recorder:
    """Collects the steps of one run."""

    def __init__(self, hass: HomeAssistant, heads: tuple[str, str]) -> None:
        self.hass = hass
        self.heads = heads
        self.calls: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        hass.bus.async_listen(EVENT_CALL_SERVICE, callback(lambda event: self._on_call(event)))

    def _on_call(self, event: Any) -> None:
        data = dict(event.data)
        if data.get("domain") != "climate":
            return
        target = data.get("service_data", {}).get("entity_id")
        targets = [target] if isinstance(target, str) else list(target or [])
        if any(head in targets for head in self.heads):
            self.calls.append(
                {"service": data["service"], "service_data": data.get("service_data", {})}
            )

    def snapshot(self, name: str, entry: MockConfigEntry) -> None:
        registry = er.async_get(self.hass)
        entities = {}
        for ent in sorted(
            er.async_entries_for_config_entry(registry, entry.entry_id),
            key=lambda e: e.unique_id,
        ):
            key = ent.unique_id.removeprefix(f"{entry.entry_id}_")
            state = self.hass.states.get(ent.entity_id)
            entities[key] = {
                "entity_id": ent.entity_id,
                "state": None if state is None else state.state,
                "attributes": None if state is None else dict(state.attributes),
            }
        heads = {}
        for head in self.heads:
            state = self.hass.states.get(head)
            heads[head] = {"state": state.state, "attributes": dict(state.attributes)}
        self.steps.append(
            normalize(
                {
                    "step": name,
                    "entities": entities,
                    "heads": heads,
                    "calls": self.calls,
                    "data": dict(entry.data),
                    "options": dict(entry.options),
                }
            )
        )
        self.calls = []


async def _temps(hass: HomeAssistant, a: float, b: float) -> None:
    unit = hass.config.units.temperature_unit
    for sensor, value in ((SENSOR_A, a), (SENSOR_B, b)):
        hass.states.async_set(sensor, str(value), {ATTR_UNIT_OF_MEASUREMENT: unit})
    await hass.async_block_till_done()


def _eid(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str:
    unique_id = f"{entry.entry_id}_{suffix}"
    for ent in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if ent.unique_id == unique_id:
            return ent.entity_id
    raise AssertionError(f"no entity {unique_id}")


async def _call(hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)
    await hass.async_block_till_done()


async def _switch(hass: HomeAssistant, entry: MockConfigEntry, key: str, on: bool) -> None:
    await _call(
        hass, "switch", "turn_on" if on else "turn_off", {"entity_id": _eid(hass, entry, key)}
    )


async def _compute(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """One deterministic compute and apply, with the mode-flip dwell expired."""
    expire_dwell(entry.runtime_data)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def room_page_pass(
    hass: HomeAssistant, entry: MockConfigEntry, room: int, changes: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Open one room's page, submit what it shows (plus ``changes``), as the frontend does."""
    result = await open_options_page(hass, entry, f"room_{room}")
    values = {**prefilled(result), **(changes or {})}
    result = await submit(hass.config_entries.options, result, values)
    await hass.async_block_till_done()
    return result


Hook = Callable[[HomeAssistant, MockConfigEntry], Awaitable[None]]


async def run_scenario(
    hass: HomeAssistant, unit: str, *, after_room_pages: Hook | None = None
) -> list[dict[str, Any]]:
    """Run the whole scenario in ``unit`` ("F" or "C"); return the recorded steps.

    ``after_room_pages`` replaces the second room's untouched room-page pass
    (the mixed-install test switches that room to a dual setpoint there).
    """
    spec = UNITS[unit]
    temps = spec["temps"]
    hass.config.units = spec["system"]
    head_a, head_b = await _setup_mock_heads(hass, cls=spec["head"])
    rec = Recorder(hass, (head_a, head_b))
    await _temps(hass, *temps["boot"])

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        version=2,
        minor_version=2,
        entry_id="pinentry",
        data={
            CONF_ZONES: [
                {
                    ZONE_NAME: "Living",
                    ZONE_CLIMATE: head_a,
                    ZONE_SENSOR: SENSOR_A,
                    ZONE_HANDS_OFF: True,
                },
                {
                    ZONE_NAME: "Bedroom",
                    ZONE_CLIMATE: head_b,
                    ZONE_SENSOR: SENSOR_B,
                    ZONE_FLOOR_LOW: spec["floor_low"],
                },
            ],
        },
        options={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_FAN_BOOST_ENABLE: True,
            CONF_MODE_HYSTERESIS: 0,
        },
    )
    entry.add_to_hass(hass)
    # A restart with restore: both targets and both room switches come back
    # from the last run; the coordinator itself was off.
    registry = er.async_get(hass)
    restored = []
    for slug, target in zip(("primary", "secondary"), spec["targets"], strict=True):
        number = registry.async_get_or_create(
            "number", DOMAIN, f"pinentry_{slug}_target", config_entry=entry,
            suggested_object_id=f"{slug}_target",
        ).entity_id
        switch = registry.async_get_or_create(
            "switch", DOMAIN, f"pinentry_{slug}_enable", config_entry=entry,
            suggested_object_id=f"{slug}_enable",
        ).entity_id
        unit = hass.config.units.temperature_unit
        restored.append((State(number, str(target)), {
            "native_max_value": None, "native_min_value": None, "native_step": None,
            "native_unit_of_measurement": unit, "native_value": target,
        }))
        restored.append((State(switch, "on"), {}))
    mock_restore_cache_with_extra_data(hass, restored)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rec.snapshot("boot", entry)

    # An options-flow pass over both room pages that changes nothing.
    await room_page_pass(hass, entry, 1)
    if after_room_pages is None:
        await room_page_pass(hass, entry, 2)
    else:
        await after_room_pages(hass, entry)
    rec.snapshot("room_pages", entry)

    await _switch(hass, entry, "coordinator_enable", True)
    await _compute(hass, entry)
    rec.snapshot("coordinator_on", entry)

    async def step(name: str) -> None:
        await _temps(hass, *temps[name])
        await _compute(hass, entry)
        rec.snapshot(name, entry)

    await step("heat")
    await step("heat_run")

    # Restart in the middle of the heat run (an entry reload restores from
    # what the entities saved on removal).
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _compute(hass, entry)
    rec.snapshot("restart_mid_heat", entry)

    await step("heat_done")
    await step("cool")
    # Still cooling a few tenths above the target: a small cool-side fan
    # boost distance, where an off-by-a-half would change the fan speed.
    await step("cool_run")
    await step("idle")
    await step("standoff")

    # Fan auto off (a hold at the current speed), then back on.
    await _switch(hass, entry, "primary_fan_auto", False)
    await _compute(hass, entry)
    rec.snapshot("fan_auto_off", entry)
    await _switch(hass, entry, "primary_fan_auto", True)
    await _compute(hass, entry)
    rec.snapshot("fan_auto_on", entry)

    await _switch(hass, entry, "eco_idle", True)
    await _compute(hass, entry)
    rec.snapshot("eco", entry)
    await _switch(hass, entry, "eco_idle", False)
    await step("idle")

    await _switch(hass, entry, "heat_lockout", True)
    await step("heat_lockout_held")
    await step("heat_lockout_floor")
    await _switch(hass, entry, "heat_lockout", False)
    await step("idle")

    await _switch(hass, entry, "cool_lockout", True)
    await step("cool_lockout_held")
    await step("cool_lockout_ceiling")
    await _switch(hass, entry, "cool_lockout", False)
    await step("idle")

    # The freeze floor: the second room is switched off and gets cold.
    await _switch(hass, entry, "secondary_enable", False)
    await step("freeze_floor")
    await step("freeze_floor_clear")
    await _switch(hass, entry, "secondary_enable", True)
    await step("idle")

    # Hands-off: the first room is switched off, its head is released, and
    # someone runs it from its own remote.
    await _switch(hass, entry, "primary_enable", False)
    await _compute(hass, entry)
    rec.snapshot("hands_off_release", entry)
    await _call(hass, "climate", "set_hvac_mode", {"entity_id": head_a, "hvac_mode": "heat"})
    await _compute(hass, entry)
    rec.snapshot("hands_off_vote", entry)
    await _switch(hass, entry, "primary_enable", True)
    await step("idle")

    # A target write (an automation or the room's slider).
    await _call(
        hass, "number", "set_value",
        {"entity_id": _eid(hass, entry, "primary_target"), "value": spec["target_write"]},
    )
    await _compute(hass, entry)
    rec.snapshot("target_write", entry)
    await step("heat")
    return rec.steps


# Every plan zones row gained these after 3.10.0 (the lost-head vote): the
# 3.9.0 record predates them. A replay takes them out of each recorded row
# with take_vote_rows and checks them on their own, then compares the rest of
# the step to the record unchanged.
VOTE_ROW_KEYS = ("vote_status", "lost_since")


def take_vote_rows(step: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[Any, ...]]]:
    """``step`` without VOTE_ROW_KEYS in its plan rows, and each row's values of them.

    A row missing a key reads ``"<missing>"`` for it, so a replay that expects
    the keys fails on a row without them instead of passing on a default.
    """
    step = json.loads(json.dumps(step))
    taken = [
        tuple(row.pop(key, "<missing>") for key in VOTE_ROW_KEYS)
        for row in step["entities"]["plan"]["attributes"]["zones"]
    ]
    return step, taken


# Home Assistant 2026.11 publishes temperature_unit, the system unit, on every
# climate entity (the thermostats and the heads alike); the 3.9.0 record
# predates it. A replay takes it out of each climate entity's attributes with
# take_climate_units and checks it on its own. On an older core every entity
# reads "<missing>", so a unit that appears there fails as well.
HA_PUBLISHES_CLIMATE_UNIT = hasattr(
    getattr(climate_const, "ClimateEntityStateAttribute", None), "TEMPERATURE_UNIT"
)


def _climate_attributes(step: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each climate entity's attributes in ``step`` (the dicts themselves), by name."""
    return {
        name: entity["attributes"]
        for group in ("entities", "heads")
        for name, entity in step[group].items()
        if entity["attributes"] is not None and "hvac_modes" in entity["attributes"]
    }


def take_climate_units(step: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``step`` without temperature_unit on its climate entities, and each one's value."""
    step = deepcopy(step)
    taken = {
        name: attributes.pop("temperature_unit", "<missing>")
        for name, attributes in _climate_attributes(step).items()
    }
    return step, taken


def climate_units(recorded: dict[str, Any], unit: str) -> dict[str, Any]:
    """What take_climate_units must find, on this core, in a replay of ``recorded``."""
    value = ("°F" if unit == "F" else "°C") if HA_PUBLISHES_CLIMATE_UNIT else "<missing>"
    return dict.fromkeys(_climate_attributes(recorded), value)


def dumps(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=1, sort_keys=True) + "\n"
