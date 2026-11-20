"""The simulator page's MXZ row against the real coordinator, on the same script.

The page (``docs/simulator``) runs MXZ's own standard-library functions over the invented house's
heads, through ``docs/simulator/mxz_row.py``. This module runs the same script on a real MXZ entry
in Home Assistant over the real ``invented_house`` heads and room sensors
(``verification/instance/invented_house``: ``InventedHead`` and ``InventedTemperature`` on the
``invented_house`` platform, with the demo model's actions on and its rooms moved only by the
script), and the page's row from the shipped bundle in a separate Python with every
``homeassistant`` import refused (``tests/simlab_runner.py``). ``tests/test_simulator.py``
compares the two after every step.

A script is JSON: ``temps`` (each room's temperature), ``heads`` (each head's mode, setpoints and
fan), ``saved`` (the room state store's record MXZ starts from), ``wall`` (the wall clock at the
start) and ``steps``, each one of:

- ``["until", t]``: time passes to ``t`` seconds after the start; every timer runs at its due time;
- ``["reading", room, temperature]``: the room's temperature changes (the ``room`` service);
- ``["readings", {room: temperature, ...}]``: several rooms change at once (the ``temperatures``
  service), as the page's model moves every room each tick;
- ``["press", room, fields]``: a person at the head's remote (the ``remote`` service);
- ``["target", room, value]`` and ``["enable", room, on]``: a person sets the room's target number
  or enable switch;
- ``["comfort", room, on]``: the room's Comfort priority entity turns on or off;
- ``["fan_auto", room, on]``: a person sets the room's Fan auto switch;
- ``["restart"]``: Home Assistant stops and starts.

After each step a snapshot has the time, the plan's decision (shared mode, why, and each room's
demand, engage, reading, target, fan hold, sensor health, vote and Comfort priority), every call
MXZ made to each head since the last step, each compute since the last step with its time and why,
every wakeup armed now with the seconds until it runs (but the delivery queue's own zero-delay
wait before Home Assistant 2025.11, ``tests/planner_parity.LEGACY_WAITS``), the shared mode and the hold left, what
each head shows, each room's target, enable and fan hold, and at a restart the store's record.

Every room, head and number is invented; nothing here is a hardware result.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.mxz_coordinator.const import (
    CONF_ZONES,
    DOMAIN,
    ZONE_CLIMATE,
    ZONE_COMFORT_PRIORITY_ENTITY,
    ZONE_NAME,
    ZONE_SENSOR,
    zone_slug,
)
from custom_components.mxz_coordinator.room_state import _room_state_store
from tests.ha_test import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)
from tests.planner_parity import LEGACY_WAITS, START
from tests.restart import stopped
from tests.test_drive import _eid, _set_target
from tests.timing_parity import Timeline

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT / "verification" / "instance"
RUNNER = Path(__file__).with_name("simlab_runner.py")
WALL = START.timestamp()


def _invented_house() -> Any:
    """``verification/instance/invented_house`` as the package ``invented_house``."""
    if "invented_house" in sys.modules:
        return sys.modules["invented_house"]
    spec = importlib.util.spec_from_file_location(
        "invented_house", INSTANCE / "invented_house" / "__init__.py",
        submodule_search_locations=[str(INSTANCE / "invented_house")],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["invented_house"] = module
    spec.loader.exec_module(module)
    return module


def climate_id(key: str) -> str:
    return f"climate.{key}_head"


def comfort_id(key: str) -> str:
    return f"input_boolean.{key}_comfort"


def plan_view(attributes: dict[str, Any] | None) -> dict[str, Any] | None:
    """The plan's decision, read off what the plan sensor shows (``Plan.to_attributes``)."""
    if attributes is None:
        return None
    return {
        **{
            key: attributes[key]
            for key in (
                "state", "standoff", "mode_by_hands_off", "mode_by_safety", "mode_by_comfort_priority",
                "mode_by_floor", "sensors_ok", "seconds_since_mode_change", "mode_change_allowed",
                "idle_action",
            )
        },
        "rooms": [
            {
                key: zone[key]
                for key in (
                    "demand", "engage", "temp", "target", "enabled", "fan_hold", "sensor_health",
                    "vote_status", "comfort_priority", "comfort_priority_status",
                )
            }
            for zone in attributes["zones"]
        ],
    }


class Lab(Timeline):
    """A real MXZ entry over the real invented heads, run by a parity script."""

    def __init__(self, hass: HomeAssistant, freezer: Any, monkeypatch: Any, tmp_path: Path) -> None:
        super().__init__(hass, freezer, monkeypatch)
        package = _invented_house()
        from invented_house import climate as invented_climate
        from invented_house import devices as dv
        from invented_house import sensor as invented_sensor

        class StillBench(package.Bench):  # type: ignore[misc,name-defined]
            """The demo model's bench with its actions on and no tick: rooms move only when
            the script says."""

            @property
            def modelled(self) -> bool:
                return True

        self.dv = dv
        self.house = dv.load_house()
        self.keys = [room["key"] for room in self.house["rooms"]]
        self.bench = StillBench(hass, self.house, dv.Devices.fresh(self.house), tmp_path / "invented_house.json")
        self.climate_entities = [invented_climate.InventedHead(self.bench, room) for room in self.house["rooms"]]
        self.sensor_entities = [invented_sensor.InventedTemperature(self.bench, room) for room in self.house["rooms"]]
        self.snapshots: list[dict[str, Any]] = []
        self._seen_calls = 0
        self._seen_timeline = 0

    @property
    def entity_key(self) -> dict[str, str]:
        return {climate_id(key): key for key in self.keys}

    async def _register(self) -> None:
        hass = self.hass

        def adder(group: list[Any]) -> Any:
            async def setup_platform(hass: Any, config: Any, add: Any, discovery_info: Any = None) -> None:
                add(group)

            return setup_platform

        mock_integration(hass, MockModule("invented_house"))
        mock_platform(hass, "invented_house.climate", MockPlatform(async_setup_platform=adder(self.climate_entities)))
        mock_platform(hass, "invented_house.sensor", MockPlatform(async_setup_platform=adder(self.sensor_entities)))
        assert await async_setup_component(hass, "climate", {"climate": [{"platform": "invented_house"}]})
        assert await async_setup_component(hass, "sensor", {"sensor": [{"platform": "invented_house"}]})
        await hass.async_block_till_done()

    async def start(self, script: dict[str, Any]) -> None:
        """MXZ starts with Home Assistant, from the store record ``script["saved"]``."""
        hass = self.hass
        hass.config.units = US_CUSTOMARY_SYSTEM
        devices = self.bench.devices
        for key, temperature in script["temps"].items():
            devices.rooms[key].temperature = temperature
        for key, head in script["heads"].items():
            devices.heads[key] = self.dv.Head(**head)
        await self._register()
        for key in self.keys:
            hass.states.async_set(comfort_id(key), "off")
        self.entry = MockConfigEntry(
            domain=DOMAIN,
            title="MXZ Coordinator",
            data={
                CONF_ZONES: [
                    {
                        ZONE_NAME: room["name"],
                        ZONE_CLIMATE: climate_id(room["key"]),
                        ZONE_SENSOR: f"sensor.{room['key']}_temperature",
                        ZONE_COMFORT_PRIORITY_ENTITY: comfort_id(room["key"]),
                    }
                    for room in self.house["rooms"]
                ],
            },
            version=2,
            minor_version=2,
        )
        self.entry.add_to_hass(hass)
        await _room_state_store(hass, self.entry.entry_id).async_save(script["saved"])
        await self.do("start", self._started)
        self.snap("start")

    async def _started(self) -> None:
        """Set up before Home Assistant runs, then it starts (``tests/restart.py``)."""
        hass = self.hass
        hass.set_state(CoreState.not_running)
        assert await hass.config_entries.async_setup(self.entry.entry_id)
        await hass.async_block_till_done()
        hass.set_state(CoreState.starting)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
        await hass.async_block_till_done()
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()

    async def run(self, script: dict[str, Any]) -> list[dict[str, Any]]:  # type: ignore[override]
        await self.start(script)
        for step in script["steps"]:
            kind, *args = step
            label = " ".join(str(arg) for arg in step)
            saved = None
            if kind == "until":
                await self.until(args[0])
            elif kind == "restart":
                saved = await self._restart()
            else:
                await self.do(label, self._action(kind, args))
            self.snap(label, saved)
        return self.snapshots

    def _action(self, kind: str, args: list[Any]) -> Any:
        hass, bench, entry = self.hass, self.bench, self.entry

        async def reading() -> None:
            bench.devices.rooms[args[0]].temperature = args[1]
            await bench.changed(args[0])

        async def readings() -> None:
            for key, temperature in args[0].items():
                bench.devices.rooms[key].temperature = temperature
            await bench.changed(*args[0])

        async def press() -> None:
            head = bench.devices.heads[args[0]]
            for name, value in args[1].items():
                setattr(head, name, value)
            await bench.changed(args[0])

        async def target() -> None:
            await _set_target(hass, _eid(hass, entry, f"_{self._slug(args[0])}_target"), args[1])

        async def switch(suffix: str) -> None:
            await hass.services.async_call(
                "switch", "turn_on" if args[1] else "turn_off",
                {"entity_id": _eid(hass, entry, f"_{self._slug(args[0])}_{suffix}")}, blocking=True,
            )

        async def enable() -> None:
            await switch("enable")

        async def fan_auto() -> None:
            await switch("fan_auto")

        async def comfort() -> None:
            hass.states.async_set(comfort_id(args[0]), "on" if args[1] else "off")

        return {"reading": reading, "readings": readings, "press": press, "target": target, "enable": enable,
                "fan_auto": fan_auto, "comfort": comfort}[kind]

    def _slug(self, key: str) -> str:
        return zone_slug(self.keys.index(key))

    async def _restart(self) -> dict[str, Any]:
        record: dict[str, Any] = {}
        self._log(do="restart")
        async with stopped(self.hass, self.entry):
            loaded = await _room_state_store(self.hass, self.entry.entry_id).async_load()
            record = dict(loaded or {})
        await self.hass.async_block_till_done()
        return record

    def _pending(self) -> dict[str, float]:
        clock = self.coord.clock
        now = self.hass.loop.time()
        out = {}
        for (name, key), wait in sorted(clock._waits.items(), key=lambda item: str(item[0])):
            if name in LEGACY_WAITS:
                continue  # the delivery queue's own zero-delay wait before HA 2025.11
            armed = next(a for a in reversed(clock.log) if a.name == name and a.key == key and a.delay == wait.delay)
            out[f"{name} {key}"] = round(armed.at + armed.delay - now, 6)
        return out

    def snap(self, label: str, saved: dict[str, Any] | None = None) -> None:
        coord = self.coord
        calls = self._calls[self._seen_calls:]
        self._seen_calls = len(self._calls)
        entries = self.timeline[self._seen_timeline:]
        self._seen_timeline = len(self.timeline)
        keys = self.entity_key
        devices = self.bench.devices
        self.snapshots.append({
            "do": label,
            "t": self._t(),
            "plan": plan_view(None if coord.data is None else coord.data.to_attributes()),
            "calls": [
                [keys[call["data"]["entity_id"]], call["call"].removeprefix("climate."),
                 {k: v for k, v in call["data"].items() if k != "entity_id"}]
                for call in calls if call["call"].startswith("climate.")
            ],
            "computes": [[entry["t"], entry["why"]] for entry in entries if "compute" in entry],
            "waits": self._pending(),
            "mode": coord.current_shared_mode,
            "hold": round(coord.mode_hold_remaining(), 3),
            "heads": {
                key: [h.hvac_mode, h.target_temp_low, h.target_temp_high, h.fan_mode]
                for key, h in devices.heads.items()
            },
            "rooms": {
                key: {
                    "target": zone.target, "enable": zone.enable,
                    "fan_held": not coord.fan.auto_is_on(zone.climate_id),
                }
                for key, zone in zip(self.keys, coord.zones, strict=True)
            },
            **({"saved": saved} if saved is not None else {}),
        })


def run_page(tree: Path, script: dict[str, Any]) -> list[dict[str, Any]]:
    """The page's MXZ row on ``script``, from the bundle ``tree``, without Home Assistant."""
    done = subprocess.run(
        [sys.executable, "-I", str(RUNNER), str(tree)],
        input=json.dumps(script), capture_output=True, text=True, check=False, cwd=tree,
    )
    if done.returncode != 0:
        raise AssertionError(f"the page's row failed:\n{done.stderr}")
    return json.loads(done.stdout)


def store(*, targets: dict[str, float], enables: dict[str, bool], **sections: Any) -> dict[str, Any]:
    """A store record with the Coordinator on, these targets and enables, and ``sections``."""
    return {
        "saved_at": WALL - 3600,
        "unit": "°F",
        "switches": {"coordinator_enable": True},
        "rooms": {
            climate_id(key): {"target": targets[key], "enable": enables[key]} for key in targets
        },
        **sections,
    }
