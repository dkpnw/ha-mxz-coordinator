"""What a start resumes, step by step, recorded from the real integration.

The resume refactor must change nothing anyone can see. Each scenario here opens a real MXZ
entry on ``tests/property_rig.py``'s rig (two rooms, the Coordinator on), changes the world
(room readings, a person at a head, the switches and the shared mode, a window, what the stores
and the restore data hold while Home Assistant is down, time) and restarts it. It records:

- at every start, right after ``MXZCoordinator._resume`` returns: what the coordinator then
  holds (``_coordinator`` below);
- every ``store_resumes`` and ``restore_shared_mode`` call, with its answer, and every warning
  MXZ logged. These are kept sorted, not in the order they came: Home Assistant sets the
  entity platforms up side by side, so the order between two entities' restores differs from
  run to run. Home Assistant's own error text inside a warning is left out, since it differs
  by release;
- after each step: every climate call MXZ made, each head's mode, fan and setpoints, what the
  coordinator holds, every entity's state, and the plan's attributes.

What the coordinator holds: each room's target, Heat to and Cool to, the pair the store
resumed, its drift, enable and hands-off release, its floor record, its engage latch and
startup seed (in their maps' order); the switches and whether the Coordinator came back on; the
shared mode, whether votes set it, the record as read and as last written, the change time, the
stamp as seconds before now and the hold left; which values the store resumed; each head's fan
hold and record, and any problem with its restore; the head-write memories; and what the store
held at the start.

Time is frozen at ``START`` and moves only when a scenario says so, so a recording is the same
on every run and every supported Home Assistant. ``tests/test_resume_parity.py`` compares each
scenario with ``tests/fixtures/resume_parity/<scenario>.json``. Those were captured on 6b7ad3b,
before resume moved out of the coordinator; run with ``MXZ_PARITY_CAPTURE=1`` to rewrite them,
only on a commit whose behavior is known right.

``random_walk`` draws its steps from ``random.Random(SEED)``.

Every room, head, sensor and value is invented; nothing here is a hardware result.
"""

from __future__ import annotations

import json
import logging
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state as rs
from homeassistant.util import dt as dt_util

from custom_components.mxz_coordinator.const import (
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    DOMAIN,
    KEY_COOL_LOCKOUT,
    KEY_COORDINATOR_ENABLE,
    KEY_ECO_IDLE,
    KEY_HEAT_LOCKOUT,
    STARTUP_RECOVER_DELAY,
    ZONE_DUAL_SETPOINT,
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from tests.property_rig import CLOSE_DELAY, HOLD, OPEN_DELAY, TARGET, Rig, open_rig
from tests.restart import stopped
from tests.restore_store import replace_stored_state
from tests.room_state_store import forget_room_state

FIXTURES = Path(__file__).with_name("fixtures") / "resume_parity"
START = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
SEED = 1118
SLUGS = ("primary", "secondary")
SWITCHES = (KEY_COORDINATOR_ENABLE, KEY_ECO_IDLE, KEY_HEAT_LOCKOUT, KEY_COOL_LOCKOUT)
_HA_ERROR = re.compile(r"(the room state store can't be read )\(.*\)(, perhaps)")
HOT = TARGET + 3
COLD = TARGET - 3
# Far enough off target to ask for the outdoor unit's direction, not only to run in it.
FREEZING = TARGET - 8
SCORCHING = TARGET + 8


def recorded(name: str) -> Path:
    return FIXTURES / f"{name}.json"



def _items(mapping: dict[str, Any]) -> list[list[Any]]:
    return [[key, value] for key, value in mapping.items()]


def _coordinator(coord: MXZCoordinator) -> dict[str, Any]:
    """What the coordinator holds of everything a start resumes."""
    fan = coord.fan
    stamp = coord._last_mode_change_ts
    saved = coord.saved
    return {
        "rooms": [
            {
                "target": zone.target,
                "low": zone.target_low,
                "high": zone.target_high,
                "saved_range": coord._saved_ranges.get(zone.slug),
                "drift": zone.drift,
                "enable": zone.enable,
                "released": zone.slug in coord._hands_off_released,
                "floor": coord.floor.record(zone.slug),
            }
            for zone in coord.zones
        ],
        "latch": _items(coord._engage_latch),
        "startup": _items(coord._startup_resume),
        "switches": {key: getattr(coord, key) for key in SWITCHES},
        "restored_on": coord.restored_coordinator_on,
        "mode": coord.current_shared_mode,
        "by_votes": coord._mode_by_votes,
        "record": asdict(coord._mode_record),
        "mode_data": coord._mode_data,
        "changed_at": coord._last_mode_change_at,
        "stamp_ago": None if stamp is None else coord.clock() - stamp,
        "hold_left": coord.mode_hold_remaining(),
        "resumed": sorted(coord._resumed),
        "fan": {
            zone.climate_id: {
                "hold": fan.diagnostics(zone.climate_id),
                "keys": [
                    name for name, held in (
                        ("latched", fan._latched), ("restore", fan._restore),
                        ("rung", fan._idx), ("on_pending", fan._on_pending),
                    )
                    if zone.climate_id in held
                ],
                "record": fan.record(zone.climate_id).as_dict(),
                "problem": fan.restore_problem(zone.climate_id),
            }
            for zone in coord.zones
        },
        "head_writes": coord._head_writes_record(),
        "saved": {
            "saved_at": saved.saved_at,
            "unit": saved.unit,
            "readable": saved.readable,
            "sections": sorted(saved.sections),
        },
    }


class _Warnings(logging.Handler):
    def __init__(self, starts: Starts) -> None:
        super().__init__(logging.WARNING)
        self._starts = starts

    def emit(self, record: logging.LogRecord) -> None:
        self._starts.note(["warning", record.getMessage()])


class Starts:
    """A rig and what each of its starts resumed."""

    def __init__(self, hass: HomeAssistant, freezer: Any, monkeypatch: Any, hass_storage: dict) -> None:
        self.hass = hass
        self.freezer = freezer
        self.storage = hass_storage
        self.steps: list[dict[str, Any]] = []
        self.rig: Rig
        self._calls: list[dict[str, Any]] = []
        self._noted: list[Any] = []
        self._entry_id = ""
        freezer.move_to(START)

        @callback
        def _call(event: Any) -> None:
            if event.data.get("domain") == "climate":
                self._calls.append(
                    {
                        "call": f"climate.{event.data['service']}",
                        "data": dict(event.data.get("service_data", {})),
                    }
                )

        hass.bus.async_listen("call_service", _call)
        resume = MXZCoordinator._resume
        store_resumes = MXZCoordinator.store_resumes
        restore_shared_mode = MXZCoordinator.restore_shared_mode

        def _resume(coord: MXZCoordinator, saved: Any) -> None:
            resume(coord, saved)
            self.note(["resumed", _coordinator(coord)])

        def _store_resumes(coord: MXZCoordinator, kind: str, key: str, recorded_at: datetime | None) -> bool:
            answer = store_resumes(coord, kind, key, recorded_at)
            self.note(["store_resumes", kind, key, None if recorded_at is None else recorded_at.isoformat(), answer])
            return answer

        def _restore_shared_mode(coord: MXZCoordinator, restored: str | None, restored_at: float | None) -> str:
            mode = restore_shared_mode(coord, restored, restored_at)
            self.note(["restore_shared_mode", restored, restored_at, mode])
            return mode

        monkeypatch.setattr(MXZCoordinator, "_resume", _resume)
        monkeypatch.setattr(MXZCoordinator, "store_resumes", _store_resumes)
        monkeypatch.setattr(MXZCoordinator, "restore_shared_mode", _restore_shared_mode)
        self._warnings = _Warnings(self)
        logging.getLogger("custom_components.mxz_coordinator").addHandler(self._warnings)

    def note(self, what: Any) -> None:
        if what[0] == "warning":
            message = what[1].replace(self._entry_id, "<entry>") if self._entry_id else what[1]
            # Home Assistant's own words for a store it can't load differ by release.
            what = ["warning", _HA_ERROR.sub(r"\1(<Home Assistant's error>)\2", message)]
        self._noted.append(what)

    def detach(self) -> None:
        logging.getLogger("custom_components.mxz_coordinator").removeHandler(self._warnings)

    async def open(self, **kwargs: Any) -> Rig:
        self.rig = await open_rig(self.hass, self.freezer, **kwargs)
        self._entry_id = self.rig.entry.entry_id
        return self.rig

    @property
    def store_key(self) -> str:
        return f"{DOMAIN}.{self._entry_id}.room_state"

    @property
    def mode_key(self) -> str:
        return f"{DOMAIN}.{self._entry_id}.mode_change"

    def store(self) -> dict[str, Any]:
        """The room state store's data, to edit while Home Assistant is down."""
        return self.storage[self.store_key]["data"]

    def _entities(self) -> list[list[Any]]:
        registry = er.async_get(self.hass)
        found = []
        for entry in sorted(
            er.async_entries_for_config_entry(registry, self._entry_id), key=lambda e: e.entity_id
        ):
            state = self.hass.states.get(entry.entity_id)
            found.append([entry.entity_id, None if state is None else state.state])
        return found

    def snap(self, step: str) -> None:
        rig = self.rig
        plan = self.hass.states.get(rig.eid("_plan"))
        self.steps.append(
            {
                "step": step,
                "resumed": [what[1] for what in self._noted if what[0] == "resumed"],
                "asked": sorted(
                    (what for what in self._noted if what[0] != "resumed"), key=json.dumps
                ),
                "calls": self._calls,
                "heads": [
                    {
                        "mode": head.mode,
                        "fan": head.fan,
                        "low": head.target_temperature_low,
                        "high": head.target_temperature_high,
                    }
                    for head in rig.heads
                ],
                "coordinator": _coordinator(rig.coord),
                "entities": self._entities(),
                "plan": None if plan is None else dict(plan.attributes),
            }
        )
        self._calls = []
        self._noted = []

    def rendered(self) -> str:
        return json.dumps(self.steps, indent=1, default=str) + "\n"

    @asynccontextmanager
    async def outage(self, *, crash: bool = False) -> AsyncIterator[None]:
        """Home Assistant is down for the ``async with`` body, then starts and computes once."""
        async with stopped(self.hass, self.rig.entry, crash=crash):
            yield
        await self.rig.settle()

    async def restart(self, *, crash: bool = False) -> None:
        async with self.outage(crash=crash):
            pass

    def plant(self, suffix: str, state: str, *, attributes: dict[str, Any] | None = None,
              extra: dict[str, Any] | None = None, at: datetime | None = None) -> None:
        """Restore data for one of the entry's entities, last changed ``at`` (now by default)."""
        entity_id = self.rig.eid(suffix)
        when = at or dt_util.utcnow()
        replace_stored_state(
            self.hass,
            entity_id,
            rs.StoredState(
                State(entity_id, state, attributes or {}, last_changed=when, last_updated=when),
                None if extra is None else rs.RestoredExtraData(extra),
                when,
            ),
        )


Scenario = Callable[[Starts], Awaitable[None]]


async def touch(starts: Starts, token: str = "quiet") -> None:
    """Time passes and the store is written again, by a pick at the second head.

    Every start writes each entity's state anew, at the time the store is written too, and a
    restore record as new as the store wins. A write after that makes the store the newer.
    """
    await starts.rig.later(5)
    await starts.rig.person_fan(token, 1)


def _number(value: float) -> dict[str, Any]:
    return {
        "native_max_value": 90.0, "native_min_value": 50.0, "native_step": 1.0,
        "native_unit_of_measurement": "°F", "native_value": value,
    }


async def held_vote(starts: Starts) -> None:
    """A hands-off room released to its person, running heat: its vote, and the mode it set, across restarts."""
    rig = await starts.open(temps=(TARGET, TARGET), first_room={ZONE_HANDS_OFF: True})
    await rig.later(1)
    await rig.switch("_primary_enable", False)
    starts.snap("parked")
    await rig.person_mode("heat", 0)
    await rig.later(2)
    starts.snap("released_and_voting")
    await starts.restart()
    starts.snap("after_a_restart")
    await rig.later(STARTUP_RECOVER_DELAY + 1)
    starts.snap("quiet_over")
    await starts.restart(crash=True)
    starts.snap("after_a_crash")
    await rig.later(STARTUP_RECOVER_DELAY + 1)
    starts.snap("quiet_over_again")
    await rig.temp(1, SCORCHING)
    starts.snap("a_coordinated_room_asks_cool")
    await starts.restart()
    starts.snap("after_another_restart")


async def dwell(starts: Starts) -> None:
    """A mode change's hold across a restart, a crash soon after a change, and after the hold."""
    rig = await starts.open(temps=(SCORCHING, TARGET))
    await rig.later(HOLD + 1)
    await starts.restart()
    starts.snap("cooling_after_a_restart")
    await rig.temp(0, FREEZING)
    starts.snap("flipped_to_heat")
    await rig.later(1)  # the record's write lands; the select's restore data is the last dump
    await starts.restart(crash=True)
    starts.snap("crash_soon_after_the_change")
    await rig.later(200)
    await starts.restart()
    starts.snap("restart_inside_the_hold")
    await rig.temp(0, SCORCHING)
    starts.snap("cool_asked_inside_the_hold")
    await starts.restart(crash=True)
    starts.snap("crash_inside_the_hold")
    await rig.later(HOLD)
    starts.snap("hold_over")
    await starts.restart()
    starts.snap("restart_after_the_hold")
    await rig.select_mode("heat")
    await starts.restart(crash=True)
    starts.snap("crash_right_after_a_pick")


async def paused(starts: Starts) -> None:
    """A room paused by an open window across a restart, and a window closed while down."""
    rig = await starts.open(temps=(HOT, HOT), window="off")
    await rig.window("on")
    await rig.later(OPEN_DELAY + 1)
    starts.snap("paused")
    await starts.restart()
    starts.snap("still_paused_after_a_restart")
    async with starts.outage():
        starts.hass.states.async_set(
            "binary_sensor.property_window", "off", {"friendly_name": "Property window"}
        )
    starts.snap("closed_while_down")
    await rig.later(CLOSE_DELAY + 1)
    starts.snap("resumed_after_the_close_delay")


def _available(rig: Rig, available: bool) -> None:
    rig.heads[0]._attr_available = available
    rig.heads[0].async_write_ha_state()


async def startup_seed(starts: Starts) -> None:
    """Each room's run resumed from its head: running, off, unavailable, and with the Coordinator off."""
    rig = await starts.open(temps=(HOT, COLD))
    await rig.later(HOLD + 1)
    await rig.temp(1, TARGET)
    starts.snap("room_0_cooling")
    await starts.restart()
    starts.snap("run_resumed")
    async with starts.outage():
        rig.heads[0].person_mode("off")
    starts.snap("head_turned_off_while_down")
    await rig.temp(0, HOT + 1)
    async with starts.outage():
        _available(rig, False)
    starts.snap("head_unavailable_at_the_start")
    _available(rig, True)
    await rig.settle()
    starts.snap("head_back")
    await touch(starts)
    await rig.switch("_coordinator_enable", False)
    await starts.restart()
    starts.snap("coordinator_off")
    await rig.switch("_coordinator_enable", True)
    starts.snap("coordinator_on_again")


FLOOR_RUN = {
    "floor_run": {
        "direction": "heat", "mark": 49.0, "worst": 48.0, "driven": 30.0, "stalled": False,
        "window": [[1795003000.0, 48.5]],
    },
    "floor_prior": {"mode": "heat", "low": 66.0, "high": 74.0},
}


async def records(starts: Starts) -> None:
    """Empty, old and malformed records: each source read as before."""
    rig = await starts.open(temps=(HOT, TARGET), first_room={ZONE_FLOOR_LOW: 50.0})
    cid0, cid1 = (head.entity_id for head in rig.heads)
    await rig.later(HOLD + 1)
    starts.snap("opened")
    async with starts.outage():
        await forget_room_state(starts.hass, rig.entry)
    starts.snap("no_store_restore_data_only")
    await touch(starts)
    async with starts.outage():
        starts.store().pop("saved_at")
    starts.snap("store_without_its_time")
    await touch(starts, "low")
    async with starts.outage():
        store = starts.store()
        store["switches"] = {KEY_ECO_IDLE: "on", KEY_HEAT_LOCKOUT: True}
        rooms = store["rooms"]
        rooms[cid0] |= {"target": "warm", "drift": None, "enable": 1, "target_low": 66.0, "target_high": None}
        rooms[cid1] |= {"target": 99.0, "drift": 50.0, "enable": False, "hands_off_released": True}
        store["fan"] = {cid0: {"held": "yes"}, cid1: {"held": True, "last_fan_command": 3}}
        store["head_writes"] = {cid0: "junk", cid1: {"hvac_mode": "junk"}}
        store["floor"] = {cid1: {"floor_run": {"direction": "heat"}}}
    starts.snap("malformed_values")
    await touch(starts, "quiet")
    async with starts.outage():
        starts.storage[starts.mode_key] = {
            "version": 1, "key": starts.mode_key, "data": {"last_mode_change": 5.0},
        }
    starts.snap("mode_record_without_a_mode")
    await touch(starts, "low")
    async with starts.outage():
        data = starts.storage[starts.mode_key]["data"]
        starts.storage[starts.mode_key]["data"] = {**data, "last_mode_change": "x"}
    starts.snap("unreadable_change_time")
    await touch(starts, "quiet")
    async with starts.outage():
        starts.storage.pop(starts.mode_key)
        starts.store()["mode"] = {"mode": "heat", "mode_at": 1.0, "mode_by_votes": True}
    starts.snap("mode_only_in_the_store")
    await touch(starts, "low")
    async with starts.outage():
        store = starts.store()
        store["rooms"][cid0] |= {"enable": True}
        store["floor"] = {cid0: FLOOR_RUN}
    starts.snap("floor_record_for_an_enabled_room")
    await touch(starts, "quiet")
    async with starts.outage():
        store = starts.store()
        store["rooms"][cid0] |= {"enable": False}
        store["floor"] = {cid0: FLOOR_RUN}
    starts.snap("floor_record_for_a_disabled_room")
    await touch(starts, "low")
    async with starts.outage():
        rig.entry.created_at = None
        starts.store()["fan"] = {
            cid0: {"held": False, "last_fan_command": None, "prior_fan_command": None, "fan_on_pending": True},
        }
    starts.snap("entry_without_its_creation_time")
    await touch(starts, "quiet")
    async with starts.outage():
        starts.storage[starts.store_key] = {
            "version": 2, "key": starts.store_key, "data": {"rooms": {}},
        }
    starts.snap("store_from_a_later_release")


async def newer_restore_data(starts: Starts) -> None:
    """Restore data newer than the store wins for that value; older loses."""
    rig = await starts.open(temps=(HOT, TARGET))
    await rig.later(HOLD + 1)
    await touch(starts)
    starts.snap("opened")
    async with starts.outage():
        starts.freezer.tick(timedelta(seconds=30))
        starts.plant("_primary_target", "74.0", extra=_number(74.0))
        starts.plant("_eco_idle", "on")
        starts.plant("_secondary_enable", "off")
        starts.plant(
            "_primary_fan_auto", "off",
            attributes={"last_fan_command": "high", "prior_fan_command": None},
        )
        starts.plant("_shared_mode", "heat")
    starts.snap("newer_restore_data")
    await touch(starts, "low")
    # Newer than the entry (an older record belongs to an earlier entry, #7), older than the store.
    older = START + timedelta(seconds=2)
    async with starts.outage():
        starts.plant("_primary_target", "66.0", extra=_number(66.0), at=older)
        starts.plant("_shared_mode", "cool", at=older)
        starts.plant("_secondary_enable", "on", at=older)
    starts.snap("older_restore_data")


async def other_unit(starts: Starts) -> None:
    """A store and settings kept in °C read by a °F system: both converted once, and the
    head-write memory not kept."""
    rig = await starts.open(temps=(HOT, TARGET))
    await rig.person_fan("quiet")
    await touch(starts)
    starts.snap("opened")
    async with starts.outage():
        store = starts.store()
        store["unit"] = "°C"
        cid0, cid1 = (head.entity_id for head in rig.heads)
        store["rooms"][cid0] |= {"target": 22.5, "drift": 1.5}
        store["rooms"][cid1] |= {"target": 40.0, "drift": None}
        starts.hass.config_entries.async_update_entry(
            rig.entry, data={**rig.entry.data, CONF_CLAMP_MIN: 16.0, CONF_CLAMP_MAX: 30.0}
        )
    starts.snap("store_and_settings_in_celsius")


async def dual_range(starts: Starts) -> None:
    """A dual-setpoint room's Heat to and Cool to: a whole pair resumed, half a pair not."""
    rig = await starts.open(temps=(HOT, TARGET), first_room={ZONE_DUAL_SETPOINT: True})
    cid0 = rig.heads[0].entity_id
    for suffix, value in (("_primary_target_low", 66), ("_primary_target_high", 76)):
        await starts.hass.services.async_call(
            "number", "set_value", {"entity_id": rig.eid(suffix), "value": value}, blocking=True
        )
        await rig.settle()
    await touch(starts)
    starts.snap("pair_set")
    await starts.restart()
    starts.snap("pair_resumed")
    await touch(starts, "low")
    async with starts.outage():
        starts.store()["rooms"][cid0] |= {"target_low": 64.0, "target_high": 78.0}
    starts.snap("stored_pair_newer")
    await touch(starts, "quiet")
    async with starts.outage():
        starts.store()["rooms"][cid0] |= {"target_low": 62.0, "target_high": None}
    starts.snap("half_a_pair")


ACTIONS = (
    "temp", "temp", "person_mode", "person_fan", "select", "enable", "coordinator", "eco",
    "window", "later", "later", "restart", "crash", "drift",
)


async def random_walk(starts: Starts) -> None:
    """Steps drawn from ``random.Random(SEED)``, with restarts and crashes among them."""
    rng = random.Random(SEED)
    rig = await starts.open(temps=(HOT, TARGET), window="off")
    starts.snap("opened")
    for index in range(40):
        action = rng.choice(ACTIONS)
        room = rng.randrange(2)
        if action == "temp":
            await rig.temp(room, rng.choice((COLD, TARGET, HOT, TARGET + 1.5, TARGET - 1.5)))
        elif action == "person_mode":
            await rig.person_mode(rng.choice(("off", "cool", "heat", "fan_only")), room)
        elif action == "person_fan":
            await rig.person_fan(rng.choice(("auto", "quiet", "low", "high")), room)
        elif action == "select":
            await rig.select_mode(rng.choice(("cool", "heat")))
        elif action == "enable":
            await rig.switch(f"_{SLUGS[room]}_enable", rng.random() < 0.5)
        elif action == "coordinator":
            await rig.switch("_coordinator_enable", rng.random() < 0.7)
        elif action == "eco":
            await rig.switch("_eco_idle", rng.random() < 0.5)
        elif action == "window":
            await rig.window(rng.choice(("on", "off")))
        elif action == "later":
            await rig.later(rng.choice((1, 30, OPEN_DELAY + 1, HOLD + 1)))
        elif action == "drift":
            await starts.hass.services.async_call(
                "number", "set_value",
                {"entity_id": rig.eid(f"_{SLUGS[room]}_drift"), "value": rng.choice((1, 2, 3))},
                blocking=True,
            )
            await rig.settle()
        else:
            await starts.restart(crash=action == "crash")
        starts.snap(f"{index}_{action}_{room}")


SCENARIOS: dict[str, Scenario] = {
    "held_vote": held_vote,
    "dwell": dwell,
    "paused": paused,
    "startup_seed": startup_seed,
    "records": records,
    "newer_restore_data": newer_restore_data,
    "other_unit": other_unit,
    "dual_range": dual_range,
    "random_walk": random_walk,
}
