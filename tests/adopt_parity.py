"""The adopt hold's lifecycle, with every call it makes on MXZ's clock, recorded from the real
integration.

The head-change refactor must change nothing anyone can see. This runs on
``tests/headchange_parity.py``'s ``Presses`` and records, per step, what it records (the calls,
the room, the hold, what moved, MXZ's write memory, the adoption log lines), and also:

- every ``call_later`` and ``cancel`` of a ``heal`` wait (the adopt, band and off holds), with
  its delay, in order;
- how many refreshes MXZ asked for (``async_request_refresh``) since the last step;
- the first room's numbers (its target, and Heat to and Cool to for a dual room) and its
  pending startup seed.

The scenarios are every scenario of ``tests/headchange_parity.py`` (MXZ's own echo, a setpoint
held, re-armed on a second press and adopted, a change put back inside the hold, off and on at
the head, a refused write, eco during the hold, the option off), then a dual room, whose press
in cool goes to Cool to and in heat to Heat to; a disabled room enabled by heat pressed at its
head; a start whose seed a reported off cancels; and a °C head on a half-degree grid, whose
pressed setpoint maps to the target a step below the nearest, or to the nearest when no target
near it is held.

``tests/test_adopt_parity.py`` compares each scenario with ``tests/fixtures/adopt_parity/``.
Those were captured on d4cffd2, before the hold's transitions moved to ``planner.head_report``
and ``planner.adopt_end``; run with ``MXZ_PARITY_CAPTURE=1`` to rewrite them, only on a commit
whose behavior is known right.

Every room, head, sensor and value is invented; nothing here is a hardware result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator import clock as mxz_clock
from custom_components.mxz_coordinator.const import ADOPT_DELAY, ZONE_DUAL_SETPOINT
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from tests import headchange_parity, property_rig
from tests.headchange_parity import Presses
from tests.property_rig import SENSORS, TARGET, RigHead
from tests.test_drive import _set_temp

FIXTURES = Path(__file__).with_name("fixtures") / "adopt_parity"


def recorded(name: str) -> Path:
    return FIXTURES / f"{name}.json"


def _key(key: Hashable) -> str | None:
    return None if key is None else str(key)


class Holds(Presses):
    """``Presses``, with the heal waits' clock calls, the refreshes asked, the room's numbers and
    its startup seed."""

    def __init__(self, hass: HomeAssistant, freezer: Any, monkeypatch: Any) -> None:
        super().__init__(hass, freezer, monkeypatch)
        self._clock: list[list[Any]] = []
        self._refreshes = 0
        self._inside = False
        holds = self
        call_later = mxz_clock.Clock.call_later
        cancel = mxz_clock.Clock.cancel
        request = MXZCoordinator.async_request_refresh

        def _call_later(clock: mxz_clock.Clock, delay: float, action: Any, **kwargs: Any) -> Any:
            if kwargs["name"] == "heal":
                holds._clock.append(["call_later", _key(kwargs.get("key")), round(delay, 6)])
            holds._inside = True
            try:
                return call_later(clock, delay, action, **kwargs)
            finally:
                holds._inside = False

        def _cancel(clock: mxz_clock.Clock, name: str, key: Hashable = None) -> None:
            if name == "heal" and not holds._inside:
                holds._clock.append(["cancel", _key(key)])
            cancel(clock, name, key)

        async def async_request_refresh(coord: MXZCoordinator) -> None:
            holds._refreshes += 1
            await request(coord)

        monkeypatch.setattr(mxz_clock.Clock, "call_later", _call_later)
        monkeypatch.setattr(mxz_clock.Clock, "cancel", _cancel)
        monkeypatch.setattr(MXZCoordinator, "async_request_refresh", async_request_refresh)

    def snap(self, step: str) -> None:
        super().snap(step)
        hass = self.hass
        rig = self.rig
        numbers = {}
        for suffix in ("_primary_target", "_primary_target_low", "_primary_target_high"):
            try:
                numbers[suffix] = hass.states.get(rig.eid(suffix)).state
            except AssertionError:
                continue
        self.steps[-1].update(
            clock=self._clock,
            refreshes=self._refreshes,
            numbers=numbers,
            startup=dict(rig.coord._startup_resume),
        )
        self._clock = []
        self._refreshes = 0


Scenario = Callable[[Holds], Awaitable[None]]


async def dual_room(holds: Holds) -> None:
    """A dual room: a press in cool is its Cool to; heat pressed with a setpoint is its Heat to."""
    rig = await holds.open(first_room={ZONE_DUAL_SETPOINT: True})
    holds.snap("cooling")
    await holds.press(low=64, high=67)
    holds.snap("pressed_in_cool")
    await rig.later(ADOPT_DELAY + 1)
    holds.snap("cool_to_adopted")
    await holds.press(mode="heat", low=63, high=67)
    holds.snap("pressed_in_heat")
    await rig.later(ADOPT_DELAY + 1)
    holds.snap("heat_to_adopted")


async def enabled_by_heat(holds: Holds) -> None:
    """Heat pressed at a disabled room's head enables the room, at the setpoint shown."""
    rig = await holds.open()
    await rig.switch("_primary_enable", False)
    holds.snap("disabled")
    await holds.press(mode="heat", low=66, high=68)
    holds.snap("heat_pressed")
    await rig.later(ADOPT_DELAY - 1)
    holds.snap("held")
    await rig.later(2)
    holds.snap("enabled_at_the_setpoint")


async def startup_off(holds: Holds) -> None:
    """A start with the first room's sensor out: its seed waits; an off reported at its head
    cancels it, so the room does not resume its run when the sensor is back."""
    rig = await holds.open()
    holds.snap("cooling")
    holds.hass.states.async_set(SENSORS[0], "unavailable")
    await holds.hass.async_block_till_done()
    await rig.restart()
    holds.snap("restarted_seed_waiting")
    await holds.press(mode="off")
    holds.snap("off_reported")
    await _set_temp(holds.hass, SENSORS[0], TARGET + 5)
    await rig.settle()
    holds.snap("sensor_back")
    await rig.later(ADOPT_DELAY + 1)
    holds.snap("after_the_hold")


class HalfDegreeHead(RigHead):
    """The rig's head, native in °C on a 0.5 °C grid, showing half degrees (the CN105's)."""

    _attr_native_temperature_unit = "°C"
    _attr_target_temperature_step = 0.5
    _attr_precision = 0.5


async def half_degree_head(holds: Holds) -> None:
    """A °C head on a 0.5 °C grid that shows half degrees, in a °F house. 18.5 °C pressed reads
    65.5 °F, whose nearest whole °F (66) would move the head on to 19 °C: the target adopted is
    65 °F, the step below, which the head holds at 18.5 °C. 17.5 °C reads 63.5 °F, which no
    target a step either side holds: the target adopted is the nearest, 64 °F."""
    holds.monkeypatch.setattr(property_rig, "RigHead", HalfDegreeHead)
    rig = await holds.open()
    holds.snap("cooling")
    await holds.press(low=16.5, high=18.5)
    await rig.later(ADOPT_DELAY + 1)
    holds.snap("the_step_below_adopted")
    await holds.press(low=15.5, high=17.5)
    await rig.later(ADOPT_DELAY + 1)
    holds.snap("the_nearest_adopted")


SCENARIOS: dict[str, Scenario] = {
    **headchange_parity.SCENARIOS,
    "dual_room": dual_room,
    "enabled_by_heat": enabled_by_heat,
    "startup_off": startup_off,
    "half_degree_head": half_degree_head,
}
