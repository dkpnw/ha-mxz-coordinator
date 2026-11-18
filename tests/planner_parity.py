"""What the planner decides and sends, step by step, recorded from the real coordinator.

The planner refactor (``planner.py``) must change nothing anyone can see. Each scenario
here sets up a real MXZ entry over invented heads and rooms, moves the world (room
readings, targets, switches, a person at a head, a window, a lost head, a restart, time),
and after each step records:

- the plan, as ``Plan.to_attributes()`` renders it (with ``tests/test_plan_attributes.py``'s
  normalization: ISO times and second counters replaced);
- every climate, notify and select service call MXZ made since the last step, in order;
- every wait MXZ armed on its clock since the last step (name, key, delay);
- the shared mode, whether votes set it, the hold left, the mode record (wall times as
  seconds after the scenario's start), the engage latch, the startup seeds, whether a room
  coasted, and each head's coil-dry stamp (seconds before now);
- what each head shows: its mode, setpoints and fan.

Time is frozen at ``START`` and moves only when a scenario says so, so a recording is the
same on every run and every supported Home Assistant. ``tests/test_planner_parity.py``
compares each scenario with ``tests/fixtures/planner_parity/<scenario>.json``. Those were
captured on 9322c5d, before the planner moved out of the coordinator; run with
``MXZ_PARITY_CAPTURE=1`` to rewrite them, only on a commit whose behavior is known right.

``random_walk`` draws its steps from ``random.Random(SEED)``.

Every room, head, sensor and value is invented; nothing here is a hardware result.
"""

from __future__ import annotations

import json
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

from custom_components.mxz_coordinator.const import (
    CONF_COIL_DRY_MINUTES,
    CONF_COOL_LOCKOUT_CEILING,
    CONF_HEAT_LOCKOUT_FLOOR,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_RESTING_MODE_BIAS,
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
    ZONE_ALLOW_DRY,
    ZONE_CLIMATE,
    ZONE_COMFORT_PRIORITY_ENTITY,
    ZONE_DUAL_SETPOINT,
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
    ZONE_NAME,
    ZONE_OPENING_ENTITY,
    ZONE_OPENING_PAUSE,
    ZONE_SAFETY_PRIORITY_ENTITY,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from tests.cloud_heads import register
from tests.ha_test import MockConfigEntry, async_fire_time_changed, async_mock_service
from tests.restart import stopped
from tests.test_drive import MockHead, MockHeadC, _eid, _set_target, _set_temp
from tests.test_dry_mode import DryHead
from tests.test_plan_attributes import _normal
from tests.test_startup_continuity import _head

FIXTURES = Path(__file__).with_name("fixtures") / "planner_parity"
START = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
SEED = 1118
SLUGS = ("primary", "secondary", "zone_3", "zone_4")
NOTIFY = "parity"
RECORDED_DOMAINS = frozenset({"climate", "notify", "select"})
# Armed only on Home Assistant before 2025.11 (DeliveryQueue.legacy), where the delivery
# queue runs its follow-up refresh from a zero-delay wait instead of at once. Left out of
# the record so one recording holds on every supported release.
LEGACY_WAITS = frozenset({"refresh_followup"})


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


class House:
    """One MXZ entry over invented rooms, and what it decided and sent at each step."""

    def __init__(self, hass: HomeAssistant, freezer: Any) -> None:
        self.hass = hass
        self.freezer = freezer
        self.steps: list[dict[str, Any]] = []
        self.heads: list[Any] = []
        self.sensors: list[str] = []
        self._calls: list[dict[str, Any]] = []
        self._clock: Any = None
        self._seen_waits = 0
        freezer.move_to(START)

        @callback
        def _listen(event: Any) -> None:
            if event.data.get("domain") in RECORDED_DOMAINS:
                self._calls.append(
                    {
                        "call": f"{event.data['domain']}.{event.data['service']}",
                        "data": dict(event.data.get("service_data", {})),
                    }
                )

        hass.bus.async_listen("call_service", _listen)

    @property
    def coord(self) -> MXZCoordinator:
        return self.entry.runtime_data

    async def setup(
        self,
        rooms: list[dict[str, Any]],
        *,
        celsius: bool = False,
        temps: list[float] | None = None,
        targets: list[float] | None = None,
        enabled: list[bool] | None = None,
        head_classes: list[type] | None = None,
        **conf: Any,
    ) -> None:
        """Rooms at their targets; every room listed in ``enabled`` (default all) on."""
        hass = self.hass
        hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
        default = 21.0 if celsius else 70.0
        classes = head_classes or [MockHeadC if celsius else MockHead] * len(rooms)
        self.heads = [cls(f"parity_{i}") for i, cls in enumerate(classes)]
        await register(hass, {"esphome": self.heads})
        self.sensors = [f"sensor.parity_{i}_temp" for i in range(len(rooms))]
        for sensor, value in zip(self.sensors, temps or [default] * len(rooms), strict=True):
            await _set_temp(hass, sensor, value)
        if CONF_NOTIFY_SERVICE in conf:
            async_mock_service(hass, "notify", NOTIFY)
        self.entry = MockConfigEntry(
            domain=DOMAIN,
            title="MXZ Coordinator",
            data={
                CONF_ZONES: [
                    {
                        ZONE_NAME: f"Room {i}",
                        ZONE_CLIMATE: head.entity_id,
                        ZONE_SENSOR: sensor,
                        **room,
                    }
                    for i, (head, sensor, room) in enumerate(
                        zip(self.heads, self.sensors, rooms, strict=True)
                    )
                ],
                **conf,
            },
            version=2,
            minor_version=2,
        )
        self.entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(self.entry.entry_id)
        await hass.async_block_till_done()
        for i, room in enumerate(rooms):
            target = (targets or [default] * len(rooms))[i]
            if room.get(ZONE_DUAL_SETPOINT):
                await self.range(i, target - 2, target + 2)
            else:
                await self.target(i, target)
        for i, on in enumerate(enabled or [True] * len(rooms)):
            if on:
                await self.switch(f"_{SLUGS[i]}_enable", True)
        await self.switch("_coordinator_enable", True)
        await self.recompute()

    async def recompute(self) -> None:
        await self.coord.async_refresh()
        await self.hass.async_block_till_done()

    async def temp(self, room: int, value: float) -> None:
        await _set_temp(self.hass, self.sensors[room], value)
        await self.recompute()

    async def target(self, room: int, value: float) -> None:
        await _set_target(self.hass, _eid(self.hass, self.entry, f"_{SLUGS[room]}_target"), value)

    async def range(self, room: int, low: float, high: float) -> None:
        slug = SLUGS[room]
        await _set_target(self.hass, _eid(self.hass, self.entry, f"_{slug}_target_low"), low)
        await _set_target(self.hass, _eid(self.hass, self.entry, f"_{slug}_target_high"), high)

    async def switch(self, suffix: str, on: bool) -> None:
        await self.hass.services.async_call(
            "switch",
            "turn_on" if on else "turn_off",
            {"entity_id": _eid(self.hass, self.entry, suffix)},
            blocking=True,
        )
        await self.hass.async_block_till_done()

    async def state(self, entity_id: str, value: str) -> None:
        self.hass.states.async_set(entity_id, value)
        await self.hass.async_block_till_done()

    async def person(
        self,
        room: int,
        mode: str,
        *,
        action: str | None = None,
        low: float | None = None,
        high: float | None = None,
    ) -> None:
        """A person changes the head at its remote: the head shows it, no service is called."""
        head = _head(self.hass, self.heads[room].entity_id)
        head._attr_hvac_mode = mode
        if action is not None:
            head._attr_hvac_action = action
        if low is not None:
            head._attr_native_target_temperature_low = low
        if high is not None:
            head._attr_native_target_temperature_high = high
        head.async_write_ha_state()
        await self.hass.async_block_till_done()

    async def available(self, room: int, on: bool) -> None:
        head = _head(self.hass, self.heads[room].entity_id)
        head._attr_available = on
        head.async_write_ha_state()
        await self.hass.async_block_till_done()

    async def later(self, seconds: float) -> None:
        """Time passes, every timer due by then runs, and so does any refresh one asked for."""
        self.freezer.tick(seconds)
        for _ in range(2):
            async_fire_time_changed(self.hass)
            await self.hass.async_block_till_done()

    async def restart(self, outage: Callable[[], Awaitable[None]] | None = None) -> None:
        async with stopped(self.hass, self.entry):
            if outage is not None:
                await outage()
        await self.hass.async_block_till_done()

    def _waits(self) -> list[list[Any]]:
        clock = self.coord.clock
        if clock is not self._clock:
            self._clock, self._seen_waits = clock, 0
        armed = clock.log[self._seen_waits:]
        self._seen_waits = len(clock.log)
        return [
            [wait.name, None if wait.key is None else str(wait.key), _round(wait.delay)]
            for wait in armed
            if wait.name not in LEGACY_WAITS
        ]

    def _mode(self) -> dict[str, Any]:
        coord = self.coord
        start = START.timestamp()
        record = {
            key: _round(value - start) if key in ("mode_at", "last_mode_change") else value
            for key, value in (coord._mode_data or {}).items()
        }
        now = coord.clock()
        return {
            "shared_mode": coord.current_shared_mode,
            "by_votes": coord._mode_by_votes,
            "hold_remaining": _round(coord.mode_hold_remaining()),
            "record": record,
            "engage_latch": dict(coord._engage_latch),
            "startup_resume": dict(coord._startup_resume),
            "coasted": coord._coasted,
            "coil_dry": {
                head: [mode, _round(now - at)] for head, (mode, at) in coord._last_active.items()
            },
        }

    def _shown(self) -> list[dict[str, Any]]:
        shown = []
        for head in self.heads:
            state = self.hass.states.get(head.entity_id)
            shown.append(
                {
                    "state": state.state,
                    **{
                        key: state.attributes.get(key)
                        for key in ("target_temp_low", "target_temp_high", "temperature", "fan_mode")
                    },
                }
            )
        return shown

    def snap(self, step: str) -> None:
        plan = self.coord.data
        record = {
            "step": step,
            "plan": None if plan is None else plan.to_attributes(),
            "calls": self._calls,
            "waits": self._waits(),
            "mode": self._mode(),
            "heads": self._shown(),
        }
        self._calls = []
        self.steps.append(_normal(record))

    def rendered(self) -> str:
        return json.dumps(self.steps, indent=1) + "\n"


Scenario = Callable[[House], Awaitable[None]]


async def mixed_demand(house: House) -> None:
    """A dual-setpoint room and two single-target rooms: satisfied, a call, a standoff,
    a run to target, a coast, the other mode, and a room switched off."""
    await house.setup([{ZONE_DUAL_SETPOINT: True}, {}, {}], **{CONF_MODE_HYSTERESIS: 0})
    house.snap("all_satisfied")
    await house.temp(0, 75)
    house.snap("cool_engages_inside_the_demand_band")
    await house.temp(0, 78)
    house.snap("one_cool_call")
    await house.temp(1, 64)
    house.snap("standoff_cool_first")
    await house.temp(0, 73)
    house.snap("cool_call_gone_heat_takes_over")
    await house.temp(0, 71.5)
    house.snap("cool_coasts")
    await house.temp(1, 69.5)
    house.snap("heat_runs_to_target")
    await house.temp(1, 70.5)
    house.snap("all_satisfied_again")
    await house.switch("_zone_3_enable", False)
    await house.temp(2, 60)
    house.snap("disabled_room_calls_nothing")


async def comfort_priority(house: House) -> None:
    """Room 1's Comfort priority sensor wins a standoff it would lose on room order."""
    comfort = "binary_sensor.parity_comfort"
    house.hass.states.async_set(comfort, "on")
    await house.setup(
        [{}, {ZONE_COMFORT_PRIORITY_ENTITY: comfort}], **{CONF_MODE_HYSTERESIS: 0}
    )
    await house.temp(0, 76)
    house.snap("cool_call")
    await house.temp(1, 64)
    house.snap("comfort_wins_heat")
    await house.state(comfort, "off")
    await house.recompute()
    house.snap("comfort_off_room_order_wins")
    await house.state(comfort, "on")
    await house.recompute()
    house.snap("comfort_back_on")


async def safety_priority(house: House) -> None:
    """A Safety priority room's call outranks the first room's."""
    safety = "binary_sensor.parity_safety"
    house.hass.states.async_set(safety, "off")
    await house.setup([{}, {ZONE_SAFETY_PRIORITY_ENTITY: safety}], **{CONF_MODE_HYSTERESIS: 0})
    await house.temp(0, 76)
    await house.temp(1, 64)
    house.snap("standoff_room_order")
    await house.state(safety, "on")
    await house.recompute()
    house.snap("safety_heat_wins")


async def dwell(house: House) -> None:
    """The mode-switch hold: the first change is free, the next is held, then released."""
    await house.setup([{}, {}], **{CONF_MODE_HYSTERESIS: 600})
    await house.temp(1, 64)
    house.snap("first_change_free")
    await house.temp(1, 70)
    await house.temp(0, 76)
    house.snap("reversal_held")
    await house.later(300)
    await house.recompute()
    house.snap("still_held")
    await house.later(301)
    house.snap("released_by_the_wakeup")
    await house.later(15)
    house.snap("after_the_wakeup")


async def hands_off_votes(house: House) -> None:
    """A released hands-off head's vote sets the mode, is held while the head is lost,
    expires, comes back, and a coordinated room's run in the voted mode stamps it."""
    await house.setup(
        [{}, {ZONE_HANDS_OFF: True}],
        enabled=[True, False],
        **{CONF_MODE_HYSTERESIS: 600, CONF_RESTING_MODE_BIAS: "cool"},
    )
    await house.recompute()
    house.snap("released")
    await house.person(1, HVACMode.HEAT)
    await house.recompute()
    house.snap("vote_sets_heat")
    await house.available(1, False)
    await house.recompute()
    house.snap("lost_vote_held")
    await house.later(31 * 60)
    await house.recompute()
    house.snap("lost_vote_expired")
    await house.available(1, True)
    await house.recompute()
    house.snap("back_in_sight")
    await house.temp(0, 64)
    house.snap("room_runs_in_the_voted_mode")
    await house.temp(0, 70.5)
    await house.person(1, HVACMode.OFF)
    await house.recompute()
    house.snap("vote_gone")
    await house.later(601)
    await house.recompute()
    house.snap("rests")


async def carry_over(house: House) -> None:
    """Votes alone set heat; when the vote goes, the way back to the resting mode is
    the votes' too (by_votes carries over) and starts no hold."""
    await house.setup(
        [{}, {ZONE_HANDS_OFF: True}],
        enabled=[True, False],
        **{CONF_MODE_HYSTERESIS: 600, CONF_RESTING_MODE_BIAS: "cool"},
    )
    await house.recompute()
    await house.person(1, HVACMode.HEAT)
    await house.recompute()
    house.snap("votes_set_heat")
    await house.person(1, HVACMode.FAN_ONLY)
    await house.recompute()
    house.snap("vote_gone_back_to_rest")
    await house.person(1, HVACMode.HEAT)
    await house.recompute()
    house.snap("votes_again")
    await house.temp(0, 76)
    house.snap("a_cool_call_against_the_vote")


async def pause_and_dry(house: House) -> None:
    """A door/window pause, a Dry picked at a head and held, then parked by heat."""
    window = "binary_sensor.parity_window"
    house.hass.states.async_set(window, "off")
    await house.setup(
        [{ZONE_OPENING_ENTITY: window, ZONE_OPENING_PAUSE: True}, {ZONE_ALLOW_DRY: True}, {}],
        head_classes=[MockHead, DryHead, MockHead],
        **{CONF_MODE_HYSTERESIS: 0, CONF_NOTIFY_SERVICE: f"notify.{NOTIFY}"},
    )
    await house.temp(0, 76)
    house.snap("cool_call")
    await house.state(window, "on")
    await house.later(61)
    await house.recompute()
    house.snap("paused")
    await house.person(1, HVACMode.DRY)
    await house.recompute()
    house.snap("dry_held")
    await house.temp(2, 64)
    house.snap("heat_parks_dry")
    await house.temp(2, 70.5)
    house.snap("heat_satisfied")
    await house.state(window, "off")
    await house.later(121)
    await house.recompute()
    house.snap("resumed")


async def startup_seed(house: House) -> None:
    """A restart resumes each room's run from its head; a head unavailable at the start
    is seeded once it is back."""
    await house.setup([{}, {}], **{CONF_MODE_HYSTERESIS: 600})
    await house.temp(0, 76)
    await house.temp(1, 75)
    house.snap("both_cooling")

    async def outage() -> None:
        await house.available(1, False)

    await house.restart(outage)
    house.snap("after_restart")
    await house.recompute()
    house.snap("first_compute")
    await house.available(1, True)
    await house.recompute()
    house.snap("head_back_seeded")
    await house.temp(0, 72)
    house.snap("run_continues")
    await house.temp(0, 69.5)
    house.snap("run_ends")


async def lockouts_eco(house: House) -> None:
    """Heat lockout with its floor, cool lockout with its ceiling, and eco."""
    await house.setup(
        [{}, {}],
        **{CONF_MODE_HYSTERESIS: 0, CONF_HEAT_LOCKOUT_FLOOR: 60, CONF_COOL_LOCKOUT_CEILING: 80},
    )
    await house.switch("_heat_lockout", True)
    await house.temp(0, 64)
    house.snap("heat_held_off")
    await house.temp(0, 58)
    house.snap("below_the_floor_heats")
    await house.temp(0, 70)
    await house.switch("_heat_lockout", False)
    await house.switch("_cool_lockout", True)
    await house.temp(1, 78)
    house.snap("cool_held_off")
    await house.temp(1, 82)
    house.snap("above_the_ceiling_cools")
    await house.switch("_cool_lockout", False)
    await house.switch("_eco_idle", True)
    await house.temp(1, 76)
    house.snap("eco_band")
    await house.temp(1, 90)
    house.snap("eco_extreme_cools")
    await house.switch("_eco_idle", False)
    await house.recompute()
    house.snap("eco_off")


async def _idle(house: House, idle: str) -> None:
    await house.setup(
        [{}, {}, {}],
        enabled=[True, True, False],
        **{CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: idle, CONF_COIL_DRY_MINUTES: 10},
    )
    house.snap("start")
    await house.temp(0, 76)
    house.snap("cooling")
    await house.temp(0, 69.5)
    house.snap("satisfied_after_cooling")
    await house.later(300)
    await house.recompute()
    house.snap("five_minutes_later")
    await house.later(301)
    house.snap("coil_dry_over")
    await house.temp(1, 64)
    house.snap("heating")
    await house.temp(1, 70.5)
    house.snap("satisfied_after_heating")


async def idle_off_after_dry(house: House) -> None:
    await _idle(house, IDLE_ACTION_OFF_AFTER_DRY)


async def idle_fan_only(house: House) -> None:
    await _idle(house, IDLE_ACTION_FAN_ONLY)


async def idle_off(house: House) -> None:
    await _idle(house, IDLE_ACTION_OFF)


async def floor_run(house: House) -> None:
    """A disabled room's freeze floor, from off and over a person's own heat (kept
    where it protects more)."""
    await house.setup(
        [{}, {ZONE_FLOOR_LOW: 50, ZONE_HANDS_OFF: True}, {ZONE_FLOOR_LOW: 50}],
        enabled=[True, False, False],
        **{CONF_MODE_HYSTERESIS: 0, CONF_NOTIFY_SERVICE: f"notify.{NOTIFY}"},
    )
    await house.recompute()
    house.snap("armed")
    await house.temp(2, 45)
    house.snap("floor_trips_from_off")
    await house.temp(2, 53)
    house.snap("floor_clears")
    await house.person(1, HVACMode.HEAT, low=70, high=72)
    await house.recompute()
    house.snap("person_heats_warmer")
    await house.temp(1, 45)
    house.snap("floor_takes_over_warmer")
    await house.temp(1, 53)
    house.snap("handed_back_warmer")
    await house.person(1, HVACMode.HEAT, low=56, high=58)
    await house.recompute()
    await house.temp(1, 45)
    house.snap("floor_takes_over_cooler")
    await house.temp(0, 76)
    house.snap("cool_call_against_the_floor")
    await house.temp(1, 53)
    house.snap("handed_back_cooler")


async def celsius(house: House) -> None:
    """°C rooms: a call, a standoff, the hold, and eco."""
    await house.setup(
        [{ZONE_DUAL_SETPOINT: True}, {}], celsius=True, **{CONF_MODE_HYSTERESIS: 600}
    )
    house.snap("start")
    await house.temp(0, 26)
    house.snap("cool_call")
    await house.temp(1, 18)
    house.snap("standoff")
    await house.temp(0, 21)
    house.snap("heat_takes_over")
    await house.temp(1, 21.5)
    await house.temp(0, 25)
    house.snap("reversal_held")
    await house.later(601)
    house.snap("released")
    await house.switch("_eco_idle", True)
    await house.recompute()
    house.snap("eco")


async def random_walk(house: House) -> None:
    """Seeded steps over three rooms (one dual-setpoint), the default hold, coil dry,
    a heat resting bias: readings, targets, time, eco and a room switched off and on."""
    rng = random.Random(SEED)
    await house.setup(
        [{ZONE_DUAL_SETPOINT: True}, {}, {}],
        **{
            CONF_MODE_HYSTERESIS: 600,
            CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY,
            CONF_RESTING_MODE_BIAS: "heat",
        },
    )
    house.snap("start")
    eco = False
    off: set[int] = set()
    for step in range(60):
        draw = rng.random()
        room = rng.randrange(3)
        if draw < 0.55:
            value = round(rng.uniform(62, 78) * 2) / 2
            await house.temp(room, value)
            label = f"{step}_temp_{room}_{value}"
        elif draw < 0.7:
            value = float(rng.randrange(66, 75))
            if room == 0:
                await house.range(0, value - 2, value + 2)
            else:
                await house.target(room, value)
            await house.recompute()
            label = f"{step}_target_{room}_{value}"
        elif draw < 0.9:
            seconds = rng.choice((30, 120, 300, 610))
            await house.later(seconds)
            label = f"{step}_later_{seconds}"
        elif draw < 0.95:
            eco = not eco
            await house.switch("_eco_idle", eco)
            await house.recompute()
            label = f"{step}_eco_{eco}"
        else:
            if room in off:
                off.discard(room)
            else:
                off.add(room)
            await house.switch(f"_{SLUGS[room]}_enable", room not in off)
            await house.recompute()
            label = f"{step}_enable_{room}_{room not in off}"
        house.snap(label)


SCENARIOS: dict[str, Scenario] = {
    scenario.__name__: scenario
    for scenario in (
        mixed_demand,
        comfort_priority,
        safety_priority,
        dwell,
        hands_off_votes,
        carry_over,
        pause_and_dry,
        startup_seed,
        lockouts_eco,
        idle_off_after_dry,
        idle_fan_only,
        idle_off,
        floor_run,
        celsius,
        random_walk,
    )
}


def recorded(name: str) -> Path:
    return FIXTURES / f"{name}.json"
