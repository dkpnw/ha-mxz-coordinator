"""What the manual fan hold decides and writes, step by step, recorded from the real integration.

The fan-hold refactor must change nothing anyone can see. Each scenario here opens a real MXZ
entry on ``tests/property_rig.py``'s rig (two rooms with fan boost on, a first head that can lag
behind its fan writes), moves the world (room readings, a person at a head's remote, Fan auto, a
window, a restart or a crash, time), and after each step records:

- every climate call MXZ made since the last step, in order;
- each head's mode and fan, and the fan writes it received since the last step;
- the plan row's ``fan_hold``;
- each Fan auto switch's state and attributes;
- per head, every record ``FanHold.publish`` published since the last step, in order, so a
  publish moved, added or dropped shows. While Home Assistant stops and starts, and while time
  passes, a publish of the same record as the one before it is not kept: how many of those a
  stop or a timer makes differs between Home Assistant releases (before 2025.11 the delivery
  queue's follow-up refresh waits for a timer). The order between two heads is not kept either: their deliveries run
  side by side. The switches' own state events are not recorded, for the same reason;
- ``FanHold``'s state of each head: ``diagnostics``, the raw older writes, which of the latch,
  the restored truth, the ladder rung and the waiting Fan auto on hold the head, and
  ``record().as_dict()``, what the room state store saves.

Time is frozen at ``START`` and moves only when a scenario says so, so a recording is the same on
every run and every supported Home Assistant. ``tests/test_fanhold_parity.py`` compares each
scenario with ``tests/fixtures/fanhold_parity/<scenario>.json``. Those were captured on 991ca10,
before the decision moved out of ``FanHold``, and ``eco_idle``, ``idle_off`` and
``missing_tokens`` on 7ebcf84, before the boost step moved to ``fan_latch.fan_step``; run with
``MXZ_PARITY_CAPTURE=1`` to rewrite them, only on a commit whose behavior is known right.

``random_walk`` draws its steps from ``random.Random(SEED)``.

Every room, head, sensor and value is invented; nothing here is a hardware result.
"""

from __future__ import annotations

import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import restore_state as rs

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    IDLE_ACTION_OFF,
    IDLE_SEED_MIN_ENTRY_AGE,
    STARTUP_RECOVER_DELAY,
)
from custom_components.mxz_coordinator.fan_hold import FanHold
from tests.property_rig import OPEN_DELAY, TARGET, Rig, open_rig
from tests.restart import restart, stopped
from tests.restore_store import replace_stored_state
from tests.room_state_store import forget_saved_fan
from tests.test_fan_hold_restore import _plant_stale_fan_auto

FIXTURES = Path(__file__).with_name("fixtures") / "fanhold_parity"
START = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
SEED = 1118
SLUGS = ("primary", "secondary")
HIGH_RUNG = TARGET + 6
MEDIUM_RUNG = TARGET + 2.2
LOW_RUNG = TARGET + 0.7


def recorded(name: str) -> Path:
    return FIXTURES / f"{name}.json"


class Fans:
    """A rig and what its fan hold decided and wrote at each step."""

    def __init__(self, hass: HomeAssistant, freezer: Any, monkeypatch: Any) -> None:
        self.hass = hass
        self.freezer = freezer
        self.steps: list[dict[str, Any]] = []
        self.rig: Rig
        self._calls: list[dict[str, Any]] = []
        self._published: dict[str, list[dict[str, Any]]] = {}
        self._writes = [0, 0]
        self._collapsing = False
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
        publish = FanHold.publish

        def _publish(fan: FanHold, climate_id: str) -> None:
            published = self._published.setdefault(climate_id, [])
            record = fan.record(climate_id).as_dict()
            if not (self._collapsing and published and published[-1] == record):
                published.append(record)
            publish(fan, climate_id)

        monkeypatch.setattr(FanHold, "publish", _publish)

    async def open(self, **kwargs: Any) -> Rig:
        self.rig = await open_rig(self.hass, self.freezer, **kwargs)
        self._writes = [len(head.fan_writes) for head in self.rig.heads]
        return self.rig

    def _head(self, room: int) -> dict[str, Any]:
        rig = self.rig
        head = rig.heads[room]
        cid = head.entity_id
        fan = rig.coord.fan
        state = self.hass.states.get(cid)
        switch = self.hass.states.get(rig.eid(f"_{SLUGS[room]}_fan_auto"))
        plan = self.hass.states.get(rig.eid("_plan"))
        rows = plan.attributes.get("zones") if plan is not None else None
        writes = head.fan_writes[self._writes[room]:]
        self._writes[room] = len(head.fan_writes)
        return {
            "mode": state.state,
            "fan": state.attributes.get("fan_mode"),
            "fan_writes": writes,
            "fan_hold": rows[room].get("fan_hold") if rows else None,
            "switch": None if switch is None else [switch.state, dict(switch.attributes)],
            "hold": fan.diagnostics(cid),
            "older": list(fan._echo[cid].older),
            "keys": [
                name for name, held in (
                    ("latched", fan._latched), ("restore", fan._restore),
                    ("rung", fan._idx), ("on_pending", fan._on_pending),
                )
                if cid in held
            ],
            "record": fan.record(cid).as_dict(),
        }

    def snap(self, step: str) -> None:
        self.steps.append(
            {
                "step": step,
                "calls": self._calls,
                "published": dict(sorted(self._published.items())),
                "heads": [self._head(room) for room in range(len(SLUGS))],
            }
        )
        self._calls = []
        self._published = {}

    def rendered(self) -> str:
        return json.dumps(self.steps, indent=1, default=str) + "\n"

    @asynccontextmanager
    async def outage(self) -> AsyncIterator[None]:
        """Home Assistant is down for the ``async with`` body, then starts and computes once."""
        self._collapsing = True
        try:
            async with stopped(self.hass, self.rig.entry):
                yield
        finally:
            self._collapsing = False
        await self.rig.settle()

    async def later(self, seconds: float) -> None:
        self._collapsing = True
        try:
            await self.rig.later(seconds)
        finally:
            self._collapsing = False

    async def restart(self, *, crash: bool = False) -> None:
        self._collapsing = True
        try:
            await restart(self.hass, self.rig.entry, crash=crash)
        finally:
            self._collapsing = False
        await self.rig.settle()

    async def fan_auto(self, on: bool, room: int = 0) -> None:
        await self.rig.switch(f"_{SLUGS[room]}_fan_auto", on)

    async def no_speed(self, room: int = 0) -> None:
        """The head advertises fan control but reports no current speed."""
        await self.rig.person_fan(None, room)  # type: ignore[arg-type]


Scenario = Callable[[Fans], Awaitable[None]]


async def pick_held(fans: Fans) -> None:
    """A person's pick holds through drift; Fan auto on hands it back; a report of auto releases."""
    rig = await fans.open(temps=(MEDIUM_RUNG, TARGET))
    fans.snap("boost_drives")
    await rig.person_fan("quiet")
    fans.snap("pick_held")
    await rig.temp(0, HIGH_RUNG)
    fans.snap("drift_keeps_the_hold")
    await rig.person_fan("high")
    fans.snap("a_new_pick_moves_the_hold")
    await fans.fan_auto(True)
    fans.snap("fan_auto_on_hands_back")
    await rig.temp(0, LOW_RUNG)
    fans.snap("boost_ramps_down")
    await rig.person_fan("middle")
    fans.snap("held_again")
    await rig.person_fan("auto")
    fans.snap("auto_releases")
    await rig.temp(0, TARGET)
    fans.snap("satisfied_back_to_auto")


async def echo_lag(fans: Fans) -> None:
    """A head one write behind and two behind: its late reports are MXZ's own, never a pick."""
    rig = await fans.open(temps=(HIGH_RUNG, TARGET), lags=True)
    fans.snap("high_unreported")
    await rig.report_fan()
    fans.snap("high_reported")
    await rig.temp(0, MEDIUM_RUNG)
    fans.snap("medium_unreported")
    await rig.temp(0, HIGH_RUNG)
    fans.snap("one_behind_still_high")
    await rig.temp(0, MEDIUM_RUNG)
    await rig.temp(0, LOW_RUNG)
    fans.snap("two_behind")
    while await rig.report_next_fan():
        fans.snap(f"in_order_{rig.heads[0].fan}")
    await rig.temp(0, HIGH_RUNG)
    await rig.temp(0, MEDIUM_RUNG)
    await rig.temp(0, TARGET)
    fans.snap("three_more_queued")
    await rig.report_fan()
    fans.snap("jumps_to_the_latest")


async def pick_while_lagging(fans: Fans) -> None:
    """A pick MXZ never wrote holds while the head lags; one it wrote is read as its echo."""
    rig = await fans.open(temps=(HIGH_RUNG, TARGET), lags=True)
    await rig.report_fan()
    await rig.temp(0, MEDIUM_RUNG)
    await rig.temp(0, LOW_RUNG)
    fans.snap("two_behind")
    await rig.person_fan("quiet")
    fans.snap("unwritten_pick_held")
    await rig.temp(0, HIGH_RUNG)
    fans.snap("still_held")
    await fans.fan_auto(True)
    fans.snap("handed_back")
    await rig.person_fan("medium")
    fans.snap("pick_of_a_written_speed")
    await rig.report_fan()
    fans.snap("reports_the_last")


async def restarts(fans: Fans) -> None:
    """A pick held across one restart and two; a not-held room's boost across a restart."""
    rig = await fans.open(temps=(MEDIUM_RUNG, HIGH_RUNG))
    await rig.person_fan("quiet")
    fans.snap("pick_held")
    await fans.restart()
    fans.snap("after_one_restart")
    await rig.temp(0, HIGH_RUNG)
    fans.snap("drift_after_restart")
    await fans.restart()
    fans.snap("after_two_restarts")
    await fans.fan_auto(True)
    fans.snap("handed_back")
    await fans.restart()
    fans.snap("not_held_after_restart")
    await rig.temp(1, TARGET + 2.7)
    fans.snap("room_1_eases_down_to_middle")
    await fans.restart()
    fans.snap("room_1_resumes_from_its_rung")


async def reconciled(fans: Fans) -> None:
    """A restored hold read against a head that changed while Home Assistant was down."""
    rig = await fans.open(temps=(MEDIUM_RUNG, MEDIUM_RUNG), fan_max="middle")
    await rig.person_fan("quiet")
    fans.snap("room_0_held")
    async with fans.outage():
        rig.heads[0].person_fan("low")
        rig.heads[1].person_fan("high")
    fans.snap("held_moved_and_not_held_above_the_ceiling")
    async with fans.outage():
        rig.heads[0].person_fan("auto")
        rig.heads[1].person_fan("auto")
    fans.snap("both_released_to_auto")
    await rig.temp(1, HIGH_RUNG)
    fans.snap("boost_drives_room_1")
    async with fans.outage():
        rig.heads[1].person_fan("quiet")
    fans.snap("not_held_rung_never_written")
    await rig.temp(0, HIGH_RUNG)
    await rig.temp(0, MEDIUM_RUNG)
    _, prior = rig.coord.fan.command_memory(rig.heads[0].entity_id)
    fans.snap("room_0_walks_down")
    async with fans.outage():
        rig.heads[0].person_fan(prior or "auto")
    fans.snap("not_held_remembered_speed")
    await rig.person_fan("high")
    await fans.fan_auto(True)
    fans.snap("fan_auto_on_adopts_a_speed_above_the_ceiling")
    async with fans.outage():
        rig.heads[0].person_fan("auto")
    fans.snap("its_memory_is_not_restored_above_the_ceiling")
    await rig.person_fan("high")
    fans.snap("so_a_later_report_of_it_holds")


async def fan_auto_switch(fans: Fans) -> None:
    """Fan auto off at a speed, off at auto, on with no speed showing, then the first report."""
    rig = await fans.open(temps=(MEDIUM_RUNG, TARGET))
    await fans.fan_auto(False)
    fans.snap("off_holds_the_speed")
    await rig.temp(0, HIGH_RUNG)
    fans.snap("held_through_drift")
    await fans.fan_auto(True)
    fans.snap("on_hands_back")
    await fans.fan_auto(False, room=1)
    fans.snap("off_at_auto_does_nothing")
    await fans.no_speed()
    fans.snap("no_speed_showing")
    await fans.fan_auto(False)
    await fans.fan_auto(True)
    fans.snap("on_waits_for_a_speed")
    await fans.restart()
    fans.snap("waiting_on_across_a_restart")
    await rig.person_fan("quiet")
    fans.snap("first_report_adopted")
    await fans.no_speed()
    await fans.fan_auto(True)
    await fans.fan_auto(False)
    fans.snap("off_cancels_a_waiting_on")
    await rig.person_fan("low")
    fans.snap("first_report_held")


async def boost_off(fans: Fans) -> None:
    """Fan boost off: nothing is read or written, Fan auto still records, and a restored truth
    waits until a gesture replaces it."""
    rig = await fans.open(temps=(MEDIUM_RUNG, TARGET), data={CONF_FAN_BOOST_ENABLE: False})
    await rig.person_fan("quiet")
    fans.snap("pick_not_read")
    await fans.fan_auto(False)
    fans.snap("off_holds")
    await fans.restart()
    fans.snap("restored_truth_waits")
    await rig.person_fan("auto")
    fans.snap("auto_not_read")
    await fans.fan_auto(False)
    fans.snap("off_at_auto_drops_the_restored_truth")
    await rig.person_fan("low")
    await fans.fan_auto(False)
    await fans.restart()
    fans.snap("held_again_across_a_restart")
    await fans.fan_auto(True)
    fans.snap("on_drops_it")


async def crash(fans: Fans) -> None:
    """A crash with a write the head hasn't reported, and with a pick made just before.

    A crash reads the restore data of the last clean stop, so one comes first.
    """
    rig = await fans.open(temps=(HIGH_RUNG, TARGET), lags=True)
    await rig.report_fan()
    await fans.restart()
    await fans.later(STARTUP_RECOVER_DELAY + 1)
    fans.snap("after_a_clean_restart")
    await rig.temp(0, MEDIUM_RUNG)
    fans.snap("write_unreported")
    await fans.restart(crash=True)
    fans.snap("after_the_crash")
    await fans.later(STARTUP_RECOVER_DELAY + 1)
    fans.snap("after_the_startup_quiet")
    await rig.report_fan()
    fans.snap("late_report_is_ours")
    await rig.person_fan("quiet")
    fans.snap("pick")
    await fans.restart(crash=True)
    await fans.later(STARTUP_RECOVER_DELAY + 1)
    fans.snap("pick_then_crash")
    await rig.temp(0, LOW_RUNG)
    fans.snap("drift_after_the_crash")
    await rig.person_fan("low")
    await fans.restart()
    await rig.temp(0, HIGH_RUNG)
    await fans.restart(crash=True)
    await fans.later(STARTUP_RECOVER_DELAY + 1)
    fans.snap("pick_kept_by_the_clean_stop_before_the_crash")


async def seeds(fans: Fans) -> None:
    """No record at a start: a pick from before is held, a boost rung is adopted, and a
    fan_only idle on an established entry is read as MXZ's own."""
    rig = await fans.open(temps=(MEDIUM_RUNG, TARGET), fan_max="middle")
    fans.snap("open")
    await fans.later(IDLE_SEED_MIN_ENTRY_AGE + 1)

    async def lost(room_0: str, room_1: str) -> None:
        await _plant_stale_fan_auto(fans.hass, rig.entry, "off")
        await _plant_stale_room_1(fans, rig)
        rig.heads[0].person_fan(room_0)
        rig.heads[1].person_fan(room_1)

    async with fans.outage():
        await lost("high", "low")
    fans.snap("above_ceiling_held_and_idle_read_as_ours")
    await fans.fan_auto(True)
    await rig.temp(0, MEDIUM_RUNG)
    fans.snap("handed_back")
    rung = rig.heads[0].fan
    async with fans.outage():
        await lost(rung, "middle")
    fans.snap("boost_rung_adopted")
    async with fans.outage():
        await lost("quiet", "auto")
    fans.snap("rung_off_the_band_held")


async def _plant_stale_room_1(fans: Fans, rig: Rig) -> None:
    """The second room's Fan auto record is from before the entry, and the store has none."""
    entity_id = rig.eid("_secondary_fan_auto")
    when = rig.entry.created_at - timedelta(seconds=1)
    replace_stored_state(fans.hass, entity_id, rs.StoredState(
        State(entity_id, "off", last_changed=when, last_updated=when), None, when,
    ))
    await forget_saved_fan(fans.hass, rig.entry, [rig.heads[1].entity_id])


async def fresh_install_idle(fans: Fans) -> None:
    """A seed under another idle action, and on an entry not yet established."""
    rig = await fans.open(temps=(TARGET, MEDIUM_RUNG), data={CONF_IDLE_ACTION: IDLE_ACTION_OFF})
    await rig.person_fan("low")
    fans.snap("idle_pick_held")
    await rig.person_fan("auto")
    await rig.temp(0, HIGH_RUNG)
    await rig.temp(0, TARGET)
    fans.snap("idle_after_a_run")


async def paused_pick(fans: Fans) -> None:
    """A pick at a head paused for an open window is read and held, and kept after the pause."""
    rig = await fans.open(temps=(MEDIUM_RUNG, TARGET), window="off")
    fans.snap("boost_drives")
    await rig.window("on")
    await fans.later(OPEN_DELAY + 1)
    fans.snap("paused")
    await rig.person_fan("middle")
    fans.snap("pick_while_paused")
    await rig.window("off")
    await fans.later(121)
    fans.snap("resumed_held")
    await fans.restart()
    fans.snap("held_after_restart")


async def random_walk(fans: Fans) -> None:
    """A walk of temperatures, picks, reports, Fan auto, windows, restarts and crashes."""
    rng = random.Random(SEED)
    rig = await fans.open(temps=(MEDIUM_RUNG, TARGET), lags=True, window="off")
    temps = (TARGET, TARGET - 1, LOW_RUNG, MEDIUM_RUNG, HIGH_RUNG, TARGET + 10)
    tokens = ("auto", "quiet", "low", "medium", "middle", "high")
    fans.snap("open")
    for n in range(60):
        kind = rng.choice(
            ("temp", "temp", "temp", "pick", "pick", "report", "report_next", "fan_auto",
             "window", "later", "restart", "crash", "no_speed")
        )
        room = rng.randrange(2)
        if kind == "temp":
            value = rng.choice(temps)
            await rig.temp(room, value)
            label = f"temp_{room}_{value}"
        elif kind == "pick":
            token = rng.choice(tokens)
            await rig.person_fan(token, room)
            label = f"pick_{room}_{token}"
        elif kind == "report":
            await rig.report_fan()
            label = "report"
        elif kind == "report_next":
            await rig.report_next_fan()
            label = "report_next"
        elif kind == "fan_auto":
            on = rng.random() < 0.5
            await fans.fan_auto(on, room)
            label = f"fan_auto_{room}_{on}"
        elif kind == "window":
            on = rng.random() < 0.5
            await rig.window("on" if on else "off")
            label = f"window_{on}"
        elif kind == "later":
            seconds = rng.choice((30, OPEN_DELAY + 1, 121, 601))
            await fans.later(seconds)
            label = f"later_{seconds}"
        elif kind == "restart":
            await fans.restart()
            label = "restart"
        elif kind == "crash":
            await fans.restart(crash=True)
            label = "crash"
        else:
            await fans.no_speed(room)
            label = f"no_speed_{room}"
        fans.snap(f"{n:02d}_{label}")


async def eco_idle(fans: Fans) -> None:
    """Eco idle: a running head's fan goes to auto, not a rung, and the rung is dropped."""
    rig = await fans.open(temps=(HIGH_RUNG, TARGET))
    fans.snap("boost_high")
    await rig.switch("_eco_idle", True)
    fans.snap("eco_idle_on")
    await rig.temp(0, TARGET + 12)
    fans.snap("eco_run")
    await rig.person_fan("low")
    fans.snap("pick_held_in_eco")
    await fans.fan_auto(True)
    fans.snap("handed_back_in_eco")
    await rig.switch("_eco_idle", False)
    fans.snap("eco_idle_off_starts_the_ladder_again")


async def idle_off(fans: Fans) -> None:
    """Idle action off: a satisfied head is handed back to auto before the off, the off head
    gets no fan write, and a pick on the off head is held for the next run."""
    rig = await fans.open(temps=(HIGH_RUNG, TARGET), data={CONF_IDLE_ACTION: IDLE_ACTION_OFF})
    fans.snap("boost_high")
    await rig.temp(0, TARGET)
    fans.snap("handed_back_then_off")
    await rig.temp(0, TARGET - 0.1)
    fans.snap("off_no_write")
    await rig.person_fan("middle")
    fans.snap("pick_on_the_off_head")
    await rig.temp(0, HIGH_RUNG)
    fans.snap("run_keeps_the_pick")


async def missing_tokens(fans: Fans) -> None:
    """A head that lists only some ladder speeds gets only those; one with no auto gets none."""
    rig = await fans.open(temps=(TARGET, TARGET))
    first, second = rig.heads
    first._attr_fan_modes = ["auto", "quiet", "high"]
    first.async_write_ha_state()
    second._attr_fan_modes = ["quiet", "low", "high"]
    second.async_write_ha_state()
    await fans.hass.async_block_till_done()
    await rig.settle()
    fans.snap("listed")
    await rig.temp(0, MEDIUM_RUNG)
    await rig.temp(1, HIGH_RUNG)
    fans.snap("medium_unlisted_no_write")
    await rig.temp(0, HIGH_RUNG)
    fans.snap("high_listed_written")
    await rig.temp(0, LOW_RUNG)
    fans.snap("ramps_down_to_an_unlisted_rung")
    await rig.temp(0, TARGET)
    fans.snap("satisfied_back_to_auto")


SCENARIOS: dict[str, Scenario] = {
    scenario.__name__: scenario
    for scenario in (
        pick_held, echo_lag, pick_while_lagging, restarts, reconciled, fan_auto_switch,
        boost_off, crash, seeds, fresh_install_idle, paused_pick, random_walk,
        eco_idle, idle_off, missing_tokens,
    )
}
