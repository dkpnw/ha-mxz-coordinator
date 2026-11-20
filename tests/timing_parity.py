"""When the real coordinator computes, and what it arms, recorded on each supported release.

``sim_timing.step`` models the timing a simulator needs: MXZ's own wakeups and heartbeat
(``timing.py``) on a model of Home Assistant's request debouncer and the delivery queue's
follow-up. This module records what the real coordinator does on a scripted run, so the model
can be checked against it on every supported release.

Each scenario sets up a real MXZ entry over invented heads and rooms (``tests/planner_parity.py``'s
``House``) and moves only the world: room readings, a head, a switch, a restart. It never asks
for a refresh itself; every compute comes from MXZ's own handlers, wakeups, heartbeat and Home
Assistant's debouncer. Time moves from one due timer to the next, exactly, so each runs at its
due time. The timeline records, in order, with seconds since ``START``:

- ``do``: a step of the script;
- ``ask``: a refresh MXZ asked for, of each kind (``request``: ``async_request_refresh``,
  ``report``: ``DeliveryQueue.request_for_report``, ``now``: ``async_refresh_now``, ``direct``:
  ``async_refresh`` called by MXZ's own setup or a person's in-place switch), and ``from``, what
  asked: a script step, a compute (``compute <n>``: a report while that plan was applied), a
  wakeup (``fire <name>``) or the heartbeat;
- ``compute``: a compute starts, with ``why``: ``ask`` (Home Assistant's debouncer ran it at
  once), ``cooldown`` (the debouncer ran a request it kept, at the end of its cooldown), ``now``
  (a direct refresh) or ``followup`` (the delivery queue's follow-up after a plan);
- ``sent``: that compute queued a delivery to a head, so its plan waits on deliveries in flight;
- ``arm`` and ``cancel``: a wakeup armed (name, key, delay) or cancelled, with ``from``;
- ``fire``: a wakeup ran; ``heartbeat``: the 15-minute heartbeat ran;
- ``end``: the time the scenario ran to.

The asks made while a plan is applied, and the wakeups each compute arms, are what a simulator's
own compute reports to ``step``; the computes, fires and heartbeats are what ``step`` must
produce. ``tests/test_timing_parity.py`` checks both sides.

``burst`` draws its readings from ``random.Random(SEED)``. Every room, head, sensor and value is
invented; nothing here is a hardware result.
"""

from __future__ import annotations

import inspect
import json
import random
from collections.abc import Awaitable, Callable, Hashable
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant import const as ha_const
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from custom_components.mxz_coordinator import clock as mxz_clock
from custom_components.mxz_coordinator.const import (
    CONF_COIL_DRY_MINUTES,
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_LOST_VOTE_TTL,
    CONF_MODE_HYSTERESIS,
    EVENT_RECOMPUTE,
    EVIDENCE_HA_WRITE,
    IDLE_ACTION_OFF_AFTER_DRY,
    VOTE_BASIS_ACTION,
    ZONE_EVIDENCE_BASIS,
    ZONE_HANDS_OFF,
    ZONE_HANDS_OFF_VOTE_BASIS,
    ZONE_OPENING_ENTITY,
    ZONE_OPENING_OPEN_DELAY,
    ZONE_OPENING_PAUSE,
    ZONE_REPORT_INTERVAL,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from custom_components.mxz_coordinator.delivery import DeliveryQueue
from tests.clocks import Armed, Clock
from tests.ha_test import async_fire_time_changed_exact
from tests.planner_parity import START, House
from tests.test_coast_followup import NoFanHead
from tests.test_startup_continuity import _head as _head_entity

FIXTURES = Path(__file__).with_name("fixtures") / "timing_parity"
SEED = 1118
# Each supported release, by its Home Assistant version (CI's pytest matrix).
RELEASES = ("2024.12.5", "2026.2.3", "2026.9.4", "2026.10.4", "2026.11.2")
RELEASE = ha_const.__version__

_origin: ContextVar[str] = ContextVar("timing_origin", default="")


Scenario = Callable[["Timeline"], Awaitable[None]]


def recorded(release: str, name: str) -> Path:
    return FIXTURES / release / f"{name}.json"


def _key(key: Hashable) -> str | None:
    return None if key is None else str(key)


def _called_from(*names: str) -> str | None:
    """The first of ``names`` that is on the calling stack, None if none is."""
    stack = {frame.function for frame in inspect.stack(context=0)[2:]}
    return next((name for name in names if name in stack), None)


class Timeline(House):
    """A ``House`` whose coordinator's computes, asks, wakeups and heartbeats are recorded."""

    def __init__(self, hass: HomeAssistant, freezer: Any, monkeypatch: Any) -> None:
        super().__init__(hass, freezer)
        self.timeline: list[dict[str, Any]] = []
        self._computes = 0
        self._sent: set[str] = set()
        self._hook(monkeypatch)

    def _t(self) -> float:
        return round((dt_util.utcnow() - START).total_seconds(), 6)

    def _log(self, **entry: Any) -> None:
        self.timeline.append({"t": self._t(), **entry})

    def _hook(self, monkeypatch: Any) -> None:
        timeline = self

        update = MXZCoordinator._async_update_data
        request = MXZCoordinator.async_request_refresh
        now = MXZCoordinator.async_refresh_now
        direct = MXZCoordinator.async_refresh
        heartbeat = MXZCoordinator._on_heartbeat
        report = DeliveryQueue.request_for_report
        queue = DeliveryQueue.queue
        schedule = Clock._schedule
        drop = mxz_clock.Clock._drop
        fire = Armed.fire

        async def _async_update_data(coord: MXZCoordinator) -> Any:
            caller = _called_from("_refresh_current", "_handle_timer_finish", "async_call", "async_refresh")
            why = {
                "_refresh_current": "followup",
                "_handle_timer_finish": "cooldown",
                "async_call": "ask",
                "async_refresh": "now",
            }[caller or "async_refresh"]
            timeline._computes += 1
            n = timeline._computes
            timeline._log(compute=n, why=why)
            token = _origin.set(f"compute {n}")
            try:
                return await update(coord)
            finally:
                _origin.reset(token)

        async def async_request_refresh(coord: MXZCoordinator) -> None:
            if _called_from("_refresh_current") is None:
                timeline._log(ask="request", **{"from": _origin.get()})
            await request(coord)

        async def async_refresh_now(coord: MXZCoordinator) -> None:
            timeline._log(ask="now", **{"from": _origin.get()})
            await now(coord)

        async def async_refresh(coord: MXZCoordinator) -> None:
            if _called_from("async_refresh_now", "_refresh_current", "async_call", "_handle_timer_finish") is None:
                timeline._log(ask="direct", **{"from": _origin.get()})
            await direct(coord)

        @callback
        def _on_heartbeat(coord: MXZCoordinator, _now: Any) -> None:
            timeline._log(heartbeat=True)
            token = _origin.set("heartbeat")
            try:
                heartbeat(coord, _now)
            finally:
                _origin.reset(token)

        def request_for_report(queue: DeliveryQueue) -> None:
            timeline._log(ask="report", **{"from": _origin.get()})
            report(queue)

        def _queue(delivery: DeliveryQueue, climate_id: str, *args: Any, **kwargs: Any) -> Any:
            origin = _origin.get()
            if origin.startswith("compute ") and origin not in timeline._sent:
                timeline._sent.add(origin)
                timeline._log(sent=int(origin.removeprefix("compute ")))
            return queue(delivery, climate_id, *args, **kwargs)

        def _schedule(clock: Clock, wait: mxz_clock.Wait, job: Any) -> Any:
            timeline._log(arm=[wait.name, _key(wait.key), round(wait.delay, 6)], **{"from": _origin.get()})
            return schedule(clock, wait, job)

        def _drop(clock: mxz_clock.Clock, wait: mxz_clock.Wait) -> None:
            if clock._waits.get((wait.name, wait.key)) is wait:
                timeline._log(cancel=[wait.name, _key(wait.key)], **{"from": _origin.get()})
            drop(clock, wait)

        def _fire(armed: Armed, when: datetime | None = None) -> None:
            timeline._log(fire=[armed.name, _key(armed.key)])
            token = _origin.set(f"fire {armed.name}")
            try:
                fire(armed, when)
            finally:
                _origin.reset(token)

        monkeypatch.setattr(MXZCoordinator, "_async_update_data", _async_update_data)
        monkeypatch.setattr(MXZCoordinator, "async_request_refresh", async_request_refresh)
        monkeypatch.setattr(MXZCoordinator, "async_refresh_now", async_refresh_now)
        monkeypatch.setattr(MXZCoordinator, "async_refresh", async_refresh)
        monkeypatch.setattr(MXZCoordinator, "_on_heartbeat", _on_heartbeat)
        monkeypatch.setattr(DeliveryQueue, "request_for_report", request_for_report)
        monkeypatch.setattr(DeliveryQueue, "queue", _queue)
        monkeypatch.setattr(Clock, "_schedule", _schedule)
        monkeypatch.setattr(mxz_clock.Clock, "_drop", _drop)
        monkeypatch.setattr(Armed, "fire", _fire)

    async def do(self, label: str, step: Callable[[], Awaitable[None]]) -> None:
        """One step of the script, recorded with what it asked and armed."""
        self._log(do=label)
        token = _origin.set(label)
        try:
            await step()
            await self.hass.async_block_till_done()
        finally:
            _origin.reset(token)

    async def until(self, seconds: float) -> None:
        """Time passes to ``seconds`` after ``START``; each timer runs at its own due time."""
        hass = self.hass
        loop = hass.loop
        while True:
            pending = [
                handle.when() - loop.time()
                for handle in loop._scheduled
                if not handle.cancelled()
            ]
            left = seconds - self._t()
            step = min([wait for wait in pending if wait <= left], default=None)
            if step is None:
                if left > 0:
                    self.freezer.tick(left)
                return
            if step > 0:
                self.freezer.tick(step)
            async_fire_time_changed_exact(hass)
            await hass.async_block_till_done()

    async def run(self, scenario: Scenario) -> None:
        """The whole scenario, then a last entry for the time it ran to."""
        await scenario(self)
        self._log(end=True)

    def rendered(self) -> str:
        return "[\n" + ",\n".join(json.dumps(entry) for entry in self.timeline) + "\n]\n"


async def _setup(house: Timeline, rooms: list[dict[str, Any]], **conf: Any) -> None:
    async def setup() -> None:
        await House.setup(house, rooms, **conf)

    await house.do("setup", setup)


async def _temp(house: Timeline, room: int, value: float) -> None:
    async def temp() -> None:
        hass = house.hass
        hass.states.async_set(
            house.sensors[room], str(value), {"unit_of_measurement": hass.config.units.temperature_unit}
        )

    await house.do(f"temp {room} {value}", temp)


def _fire_event(house: Timeline) -> Callable[[], Awaitable[None]]:
    async def fire() -> None:
        house.hass.bus.async_fire(EVENT_RECOMPUTE)

    return fire


async def _head(
    house: Timeline, room: int, mode: str, *, action: str | None = None, available: bool = True
) -> None:
    """A person at the head's remote, or the head itself: it shows this; no service is called."""

    async def head() -> None:
        entity = _head_entity(house.hass, house.heads[room].entity_id)
        entity._attr_hvac_mode = mode
        entity._attr_hvac_action = action
        entity._attr_available = available
        entity.async_write_ha_state()

    label = f"head {room} {mode}" + (f" {action}" if action else "") + ("" if available else " unavailable")
    await house.do(label, head)


async def _person(house: Timeline, room: int, mode: str, *, low: float | None = None) -> None:
    """A person changes the head at its remote (``House.person``): an adopt hold starts."""

    async def person() -> None:
        await House.person(house, room, mode, low=low)

    await house.do(f"person {room} {mode}" + ("" if low is None else f" low {low}"), person)


async def _window(house: Timeline, entity_id: str, state: str) -> None:
    async def window() -> None:
        house.hass.states.async_set(entity_id, state)

    await house.do(f"window {state}", window)


async def burst(house: Timeline) -> None:
    """Readings inside one cooldown collapse into one compute at its end; the heartbeat runs
    every 15 minutes from the start; ``mxz_recompute`` asks as a reading does."""
    rng = random.Random(SEED)
    await _setup(house, [{}, {}], **{CONF_MODE_HYSTERESIS: 0})
    await house.until(60)
    at = 60.0
    for _ in range(5):
        at += round(rng.uniform(0.5, 3.0), 1)
        await house.until(at)
        await _temp(house, rng.randrange(2), round(rng.uniform(69.0, 71.0), 1))
    await house.until(200)
    await house.do("mxz_recompute", _fire_event(house))
    await house.until(205)
    await house.do("mxz_recompute", _fire_event(house))
    await house.until(31 * 60 + 5)


async def coil_dry_dwell(house: Timeline) -> None:
    """Two cool runs whose heads report MXZ's writes while its plan is applied; both rooms
    reach their targets in one compute and park in fan_only, so both coil-dry wakeups are due
    at once: the first refreshes, and the second asks while that plan's deliveries run, which
    the delivery queue follows up at once. A cool call inside the mode-switch hold that the
    heat run started waits for the dwell wakeup."""
    await _setup(
        house, [{}, {}],
        **{CONF_MODE_HYSTERESIS: 600, CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY,
           CONF_COIL_DRY_MINUTES: 2},
    )
    await house.until(60)
    await _temp(house, 0, 76)
    await house.until(62)
    await _temp(house, 1, 76)
    await house.until(200)
    await house.do("mxz_recompute", _fire_event(house))
    await house.until(201)
    await _temp(house, 0, 69)
    await house.until(202)
    await _temp(house, 1, 69)
    await house.until(400)
    await _temp(house, 0, 64)
    await house.until(500)
    await _temp(house, 0, 70)
    await house.until(502)
    await _temp(house, 1, 76)
    await house.until(1300)


async def coast(house: Timeline) -> None:
    """A run on heads that report nothing back coasts to its target: the coast follow-up runs
    the next compute."""
    await _setup(
        house, [{}, {}], head_classes=[NoFanHead, NoFanHead],
        **{CONF_MODE_HYSTERESIS: 0, CONF_FAN_BOOST_ENABLE: False},
    )
    await house.until(60)
    await _temp(house, 0, 76)
    await house.until(120)
    await _temp(house, 0, 69)
    await house.until(300)


async def cooldown_after_report(house: Timeline) -> None:
    """A plan whose heads report while it is applied asks for the next compute at the end of the
    cooldown; a reading inside the cooldown that follows that compute waits for its end."""
    await _setup(house, [{}, {}], **{CONF_MODE_HYSTERESIS: 0})
    await house.until(60)
    await _temp(house, 0, 76)
    await house.until(75)
    await _temp(house, 1, 70.3)
    await house.until(200)


async def window_now(house: Timeline) -> None:
    """A window opens inside the cooldown: its request waits, and the pause's wakeup at the
    open delay's end refreshes at once, which cancels the waiting request. The resume at the
    close delay's end refreshes at once too, and the head the resumed room runs reports while
    that plan is applied."""
    window = "binary_sensor.timing_window"
    house.hass.states.async_set(window, "off")
    await _setup(
        house, [{ZONE_OPENING_ENTITY: window, ZONE_OPENING_PAUSE: True, ZONE_OPENING_OPEN_DELAY: 5}, {}],
        **{CONF_MODE_HYSTERESIS: 0},
    )
    await house.until(60)
    await _temp(house, 0, 76)
    await house.until(62)
    await _window(house, window, "on")
    await house.until(64)
    await _temp(house, 1, 70.5)
    await house.until(120)
    await _window(house, window, "off")
    await house.until(200)
    await _temp(house, 0, 72)
    await house.until(400)


async def fresh(house: Timeline) -> None:
    """A room whose sensor stops reporting goes stale at its deadline, by the fresh wakeup."""
    await _setup(
        house, [{ZONE_REPORT_INTERVAL: 1, ZONE_EVIDENCE_BASIS: EVIDENCE_HA_WRITE}, {}],
        **{CONF_MODE_HYSTERESIS: 0},
    )
    await house.until(30)
    await _temp(house, 0, 70.2)
    await house.until(600)


async def lost_settle(house: Timeline) -> None:
    """A hands-off head that votes by its run: a run reported just after a mode change counts at
    the settle wakeup; a run starting or stopping refreshes at once; the head lost keeps its vote
    until the lost wakeup."""
    await _setup(
        house, [{}, {ZONE_HANDS_OFF: True, ZONE_HANDS_OFF_VOTE_BASIS: VOTE_BASIS_ACTION}],
        enabled=[True, False],
        **{CONF_MODE_HYSTERESIS: 0, CONF_LOST_VOTE_TTL: 5},
    )
    await house.until(60)
    await _head(house, 1, HVACMode.HEAT, action="heating")
    await house.until(120)
    await _head(house, 1, HVACMode.HEAT, action="idle")
    await house.until(150)
    await _head(house, 1, HVACMode.HEAT, action="heating")
    await house.until(200)
    await _head(house, 1, HVACMode.HEAT, action="heating", available=False)
    await house.until(1200)


async def restart(house: Timeline) -> None:
    """A restart: the new coordinator refreshes at once, arms its startup wakeup, and its
    heartbeat counts from its own start."""
    await _setup(house, [{}, {}], **{CONF_MODE_HYSTERESIS: 0})
    await house.until(60)
    await _temp(house, 0, 76)
    await house.until(300)

    async def stop_start() -> None:
        await House.restart(house)

    await house.do("restart", stop_start)
    await house.until(305)
    await _temp(house, 0, 69)
    await house.until(1300)


async def cooldown_and_hold_off(house: Timeline) -> None:
    """A reading's compute starts the cooldown, and a person turns room 0's head off in it: the
    hold's end asks inside the cooldown, so one request is kept for its end. Room 2's head is
    turned off at that hold's end, so its hold ends at the instant the cooldown does."""
    await _setup(house, [{}, {}, {}], temps=[76.0, 70.0, 76.0], **{CONF_MODE_HYSTERESIS: 0})
    await house.until(60.5)
    await _temp(house, 1, 70.2)
    await _person(house, 0, HVACMode.OFF)
    await house.until(65.5)
    await _person(house, 2, HVACMode.OFF)
    await house.until(120.5)


async def dwell_and_hold(house: Timeline) -> None:
    """Heat after cool starts a mode-change hold; a cool call waits for the dwell wakeup at its
    end, and a person sets the heating room's head 5 seconds before, so that adopt hold ends at
    the instant the dwell and hold-end wakeups run."""
    await _setup(house, [{}, {}], **{CONF_MODE_HYSTERESIS: 600})
    await house.until(60)
    await _temp(house, 0, 76)
    await house.until(100)
    await _temp(house, 0, 70)
    await house.until(120)
    await _temp(house, 1, 64)
    await house.until(200)
    await _temp(house, 0, 76)
    await house.until(716)
    await _person(house, 1, HVACMode.HEAT, low=68.0)
    await house.until(900)


SCENARIOS: dict[str, Scenario] = {
    "burst": burst,
    "coil_dry_dwell": coil_dry_dwell,
    "coast": coast,
    "cooldown_after_report": cooldown_after_report,
    "window_now": window_now,
    "fresh": fresh,
    "lost_settle": lost_settle,
    "restart": restart,
    "cooldown_and_hold_off": cooldown_and_hold_off,
    "dwell_and_hold": dwell_and_hold,
}
