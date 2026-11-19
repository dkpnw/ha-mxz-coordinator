"""Which heads one apply commands, and how, recorded from the real coordinator.

The apply-gates refactor must change nothing anyone can see. Each scenario here runs on
``tests/delivery_parity.py``'s ``Deliveries`` (a real MXZ entry over invented heads) and records,
per step, what ``House.snap`` does (the plan, every climate and notify call in order, every wait
armed, the mode record, the coil-dry stamps, what each head shows) and the waits still armed. A
marker, ``mxz.write_back``, goes into the calls where a plan's mode write-back changes the shared
mode, its stamp or its record, so its place among the head writes shows (a write-back that changes
nothing leaves none: how many computes a step runs varies in this rig). The released hands-off
rooms and each head's fan hold are recorded too.

The scenarios walk ``MXZCoordinator._apply``'s gates in order: the Coordinator switch, a fixed
standby park (and a head mid vane-kick skipped by it), a change at a head still holding, a door/window pause
(a head that is on, and one with no state), a hands-off room parking and landing, a startup seed
whose head is unavailable, Dry held and Dry parked for heat, the idle park with coil-dry time
left, and a restart that rebuilds the coil-dry memory from what each head shows.

``tests/test_apply_parity.py`` compares each scenario with ``tests/fixtures/apply_parity/``.
Those were captured on d4cffd2, before the gates moved to ``planner.apply_plan``; run with
``MXZ_PARITY_CAPTURE=1`` to rewrite them, only on a commit whose behavior is known right.

Every room, head, sensor and value is invented; nothing here is a hardware result.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import (
    ADOPT_DELAY,
    CONF_COIL_DRY_MINUTES,
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
    INHIBIT_ACTION_FAN_ONLY,
    ZONE_ALLOW_DRY,
    ZONE_HANDS_OFF,
    ZONE_OPENING_ENTITY,
    ZONE_OPENING_PAUSE,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from tests.delivery_parity import Deliveries
from tests.planner_parity import NOTIFY
from tests.test_drive import MockHead
from tests.test_dry_mode import DryHead
from tests.test_startup_continuity import _head

FIXTURES = Path(__file__).with_name("fixtures") / "apply_parity"
INHIBIT = "binary_sensor.apply_parity_inhibit"
WINDOW = "binary_sensor.apply_parity_window"


def recorded(name: str) -> Path:
    return FIXTURES / f"{name}.json"


def _mode_state(coord: MXZCoordinator) -> tuple[Any, ...]:
    return (
        coord.current_shared_mode, coord._mode_by_votes, coord._last_mode_change_ts,
        dict(coord._mode_data or {}),
    )


class Applies(Deliveries):
    """``Deliveries``, with the write-back's place among the calls, the released hands-off
    rooms and each head's fan hold."""

    def __init__(self, hass: HomeAssistant, freezer: Any, monkeypatch: Any) -> None:
        super().__init__(hass, freezer)
        self._kicks: list[asyncio.Event] = []
        house = self
        write_back = MXZCoordinator._write_back_mode

        def _write_back_mode(coord: MXZCoordinator, *args: Any, **kwargs: Any) -> Any:
            before = _mode_state(coord)
            result = write_back(coord, *args, **kwargs)
            if _mode_state(coord) == before:
                return result
            house._calls.append(
                {
                    "call": "mxz.write_back",
                    "data": {"shared_mode": coord.current_shared_mode, "by_votes": coord._mode_by_votes},
                }
            )
            return result

        monkeypatch.setattr(MXZCoordinator, "_write_back_mode", _write_back_mode)

    def snap(self, step: str) -> None:
        super().snap(step)
        coord = self.coord
        self.steps[-1]["released"] = sorted(coord._hands_off_released)
        self.steps[-1]["fan_auto"] = [coord.fan.auto_is_on(head.entity_id) for head in self.heads]

    def kick(self, room: int) -> asyncio.Event:
        """A vane kick in progress on this room's head, until the returned event is set."""
        done = asyncio.Event()
        self._kicks.append(done)
        # On the loop, not Home Assistant's task list: block_till_done must not wait for it.
        self.coord._vane_kicks[self.heads[room].entity_id] = self.hass.loop.create_task(done.wait())
        return done

    async def unkick(self, room: int, done: asyncio.Event) -> None:
        done.set()
        await self.hass.async_block_till_done()
        self.coord._vane_kicks.pop(self.heads[room].entity_id, None)

    async def press(self, room: int, **change: Any) -> None:
        """A person at the head's remote: the head shows it, and nothing is recomputed."""
        head = _head(self.hass, self.heads[room].entity_id)
        if "mode" in change:
            head._attr_hvac_mode = change["mode"]
        if "low" in change:
            head._attr_native_target_temperature_low = change["low"]
        if "high" in change:
            head._attr_native_target_temperature_high = change["high"]
        if "fan" in change:
            head._attr_fan_mode = change["fan"]
        head.async_write_ha_state()
        await self.hass.async_block_till_done()


Scenario = Callable[[Applies], Awaitable[None]]


async def coordinator_and_standby(house: Applies) -> None:
    """Coordinator off sends nothing; a fixed standby park sends every head fan_only, and skips
    one mid vane-kick; outside the park a head mid vane-kick is skipped too."""
    house.hass.states.async_set(INHIBIT, "off")
    await house.setup(
        [{}, {}, {}],
        **{
            CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF,
            CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_FAN_ONLY,
        },
    )
    await house.temp(0, 76)
    house.snap("cooling")
    await house.switch("_coordinator_enable", False)
    await house.temp(0, 78)
    house.snap("coordinator_off_sends_nothing")
    await house.switch("_coordinator_enable", True)
    await house.recompute()
    house.snap("coordinator_on")
    await house.state(INHIBIT, "on")
    await house.recompute()
    house.snap("standby_parks_every_head")
    await house.press(2, mode=HVACMode.COOL)
    done = house.kick(2)
    await house.recompute()
    house.snap("standby_skips_the_head_mid_kick")
    await house.unkick(2, done)
    await house.recompute()
    house.snap("standby_parks_the_head_after_its_kick")
    await house.state(INHIBIT, "off")
    await house.recompute()
    house.snap("standby_over")
    done = house.kick(1)
    await house.temp(0, 69.5)
    await house.temp(1, 64)
    house.snap("heat_written_back_before_the_heads_and_the_kick_skipped")
    await house.unkick(1, done)
    await house.recompute()
    house.snap("kick_over")


async def head_change_holding(house: Applies) -> None:
    """A person's setpoint at one head holds it out of the apply; the other head is still sent
    its command; the hold's end puts the head back on the plan."""
    await house.setup([{}, {}], **{CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF})
    await house.temp(0, 76)
    await house.temp(1, 77)
    house.snap("both_cooling")
    await house.press(0, low=60, high=64)
    house.snap("pressed")
    await house.temp(0, 78)
    await house.temp(1, 69.5)
    house.snap("the_pressed_head_is_skipped")
    await house.later(ADOPT_DELAY + 1)
    await house.recompute()
    house.snap("adopted_and_sent")


async def pause(house: Applies) -> None:
    """A paused head that is on is sent off with its fan only read; a fan picked on it is held;
    a paused head with no state is skipped; the resume runs it again."""
    house.hass.states.async_set(WINDOW, "off")
    await house.setup(
        [{ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_PAUSE: True}, {}],
        **{CONF_MODE_HYSTERESIS: 0},
    )
    await house.temp(0, 76)
    house.snap("cooling")
    await house.state(WINDOW, "on")
    await house.later(61)
    await house.recompute()
    house.snap("paused_off")
    await house.press(0, fan="quiet")
    await house.recompute()
    house.snap("fan_picked_while_paused")
    await house.available(0, False)
    await house.recompute()
    house.snap("paused_head_unavailable")
    await house.available(0, True)
    await house.recompute()
    house.snap("paused_head_back")
    await house.state(WINDOW, "off")
    await house.later(121)
    await house.recompute()
    house.snap("resumed")


async def hands_off(house: Applies) -> None:
    """A disabled hands-off room's head is parked off until it lands; after that it is the
    person's and gets nothing."""
    await house.setup(
        [{}, {ZONE_HANDS_OFF: True}], enabled=[True, True],
        **{CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF},
    )
    await house.temp(1, 76)
    house.snap("hands_off_room_cooling")
    await house.switch("_secondary_enable", False)
    house.snap("disabled_parking")
    await house.recompute()
    house.snap("landed")
    await house.press(1, mode=HVACMode.HEAT)
    await house.recompute()
    house.snap("the_persons_heat_is_left")


async def startup_unavailable(house: Applies) -> None:
    """A start with one head unavailable: no park or write is made up for it; once back it is
    seeded and coordinated."""
    await house.setup([{}, {}], **{CONF_MODE_HYSTERESIS: 600, CONF_IDLE_ACTION: IDLE_ACTION_OFF})
    await house.temp(0, 76)
    await house.temp(1, 75)
    house.snap("both_cooling")

    async def outage() -> None:
        await house.available(1, False)

    await house.restart(outage)
    house.snap("after_restart")
    await house.temp(1, 60)
    house.snap("unavailable_seed_skipped")
    await house.available(1, True)
    await house.recompute()
    house.snap("head_back")


async def dry(house: Applies) -> None:
    """Dry picked at a head is held (no write, its coil-dry wait cancelled, its fan read);
    heat parks it once, with one notice; it owes the coil-dry run from leaving dry."""
    await house.setup(
        [{ZONE_ALLOW_DRY: True}, {}],
        head_classes=[DryHead, MockHead],
        **{
            CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY,
            CONF_COIL_DRY_MINUTES: 10, CONF_NOTIFY_SERVICE: f"notify.{NOTIFY}",
        },
    )
    await house.temp(0, 76)
    await house.temp(0, 69.5)
    house.snap("drying_after_cool")
    await house.press(0, mode=HVACMode.DRY)
    await house.recompute()
    house.snap("dry_held")
    await house.press(0, fan="low")
    await house.recompute()
    house.snap("fan_picked_in_dry")
    await house.temp(1, 64)
    house.snap("heat_parks_dry")
    await house.temp(1, 63)
    house.snap("parked_again_no_second_notice")
    await house.later(300)
    await house.recompute()
    house.snap("coil_dry_run")


async def coil_dry_restart(house: Applies) -> None:
    """Off after dry: a parked head's coil-dry run has its wakeup; after a restart the memory is
    rebuilt from what each head shows (cool and fan_only owe the run, heat and off do not)."""
    await house.setup(
        [{}, {}, {}, {}],
        **{
            CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY,
            CONF_COIL_DRY_MINUTES: 10,
        },
    )
    await house.temp(0, 76)
    await house.temp(1, 76)
    house.snap("cooling")
    await house.temp(1, 69.5)
    house.snap("one_parked_with_time_left")
    await house.later(120)
    await house.recompute()
    house.snap("time_left_no_second_wakeup")

    async def outage() -> None:
        await house.press(2, mode=HVACMode.HEAT)

    await house.restart(outage)
    house.snap("after_restart")
    await house.temp(0, 69.5)
    house.snap("rebuilt_and_parked")
    await house.later(601)
    await house.recompute()
    house.snap("coil_dry_over")


SCENARIOS: dict[str, Scenario] = {
    scenario.__name__: scenario
    for scenario in (
        coordinator_and_standby, head_change_holding, pause, hands_off, startup_unavailable,
        dry, coil_dry_restart,
    )
}
