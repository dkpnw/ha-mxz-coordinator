"""What Accept changes made at the head adopts, step by step, recorded from the real integration.

The head-change refactor must change nothing anyone can see. Each scenario here opens a real MXZ
entry on ``tests/property_rig.py``'s rig (two rooms, the first one cooling), has a person press
the first head's remote, lets time pass, and after each step records:

- every climate, number and switch service call since the last step, in order (MXZ's head
  writes and its adoption through the room's own number and switch);
- the first room's target, enable and the edges its head shows, and its head's mode;
- whether the adoption hold is armed, and what moved at the head since it was armed;
- MXZ's memory of its own head writes, as the room state store keeps it;
- every adoption line MXZ logged.

Time is frozen at ``START`` and moves only when a scenario says so. The cases are those of
``tests/test_accept_head_changes.py``: MXZ's own echo from a lagging head, a person's setpoint
change held and adopted, each press starting the hold over, a change undone inside the hold,
the head turned off and back on, a write the head refused and a press after it, eco during the
hold, and the option off.

``tests/test_headchange_parity.py`` compares each scenario with
``tests/fixtures/headchange_parity/<scenario>.json``. Those were captured on 7ebcf84, before the
adoption rule moved to ``planner.adopt_head_change``; run with ``MXZ_PARITY_CAPTURE=1`` to
rewrite them, only on a commit whose behavior is known right.

Every room, head, sensor and value is invented; nothing here is a hardware result.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from custom_components.mxz_coordinator.const import (
    ADOPT_DELAY,
    CONF_ACCEPT_HEAD_CHANGES,
)
from tests.property_rig import TARGET, Rig, open_rig

FIXTURES = Path(__file__).with_name("fixtures") / "headchange_parity"
START = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
HOT = TARGET + 5
RECORDED_DOMAINS = frozenset({"climate", "number", "switch"})
LOGGER = "custom_components.mxz_coordinator"


def recorded(name: str) -> Path:
    return FIXTURES / f"{name}.json"


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "adopt" in message:
            self.lines.append(f"{record.levelname} {message}")


class Presses:
    """A rig, a person at its first head, and what MXZ adopted at each step."""

    def __init__(self, hass: HomeAssistant, freezer: Any, monkeypatch: Any) -> None:
        self.hass = hass
        self.freezer = freezer
        self.monkeypatch = monkeypatch
        self.steps: list[dict[str, Any]] = []
        self.rig: Rig
        self._calls: list[dict[str, Any]] = []
        self._log = _Lines()
        freezer.move_to(START)

        @callback
        def _call(event: Any) -> None:
            if event.data.get("domain") in RECORDED_DOMAINS:
                self._calls.append(
                    {
                        "call": f"{event.data['domain']}.{event.data['service']}",
                        "data": dict(event.data.get("service_data", {})),
                    }
                )

        hass.bus.async_listen("call_service", _call)
        logger = logging.getLogger(LOGGER)
        logger.addHandler(self._log)
        monkeypatch.setattr(logger, "level", logging.INFO)

    def close(self) -> None:
        logging.getLogger(LOGGER).removeHandler(self._log)

    async def open(self, **kwargs: Any) -> Rig:
        self.rig = await open_rig(self.hass, self.freezer, temps=(HOT, TARGET), **kwargs)
        self._calls = []
        self._log.lines = []
        return self.rig

    async def press(self, **change: Any) -> None:
        """A person at the first head's remote, with no compute after it."""
        self.rig.heads[0].person_setpoint(**change)
        await self.hass.async_block_till_done()

    async def target(self, value: float) -> None:
        """A person sets the first room's target in Home Assistant."""
        await self.hass.services.async_call(
            "number", "set_value", {"entity_id": self.rig.eid("_primary_target"), "value": value},
            blocking=True,
        )
        await self.hass.async_block_till_done()

    def snap(self, step: str) -> None:
        rig = self.rig
        coord = rig.coord
        cid = rig.heads[0].entity_id
        head = self.hass.states.get(cid)
        self.steps.append(
            {
                "step": step,
                "calls": self._calls,
                "room": {
                    "target": self.hass.states.get(rig.eid("_primary_target")).state,
                    "enable": self.hass.states.get(rig.eid("_primary_enable")).state,
                    "mode": head.state,
                    "edges": [
                        head.attributes.get("target_temp_low"),
                        head.attributes.get("target_temp_high"),
                    ],
                },
                "hold": coord.clock.waiting("heal", (cid, "adopt")),
                "moved": sorted(coord._adopt_moved.get(cid, set())),
                "memory": json.loads(json.dumps(coord._head_writes_record())),
                "log": self._log.lines,
            }
        )
        self._calls = []
        self._log.lines = []

    def rendered(self) -> str:
        return json.dumps(self.steps, indent=1) + "\n"


Scenario = Callable[[Presses], Awaitable[None]]


async def own_echo(presses: Presses) -> None:
    """A head lagging on its mode and setpoints shows MXZ's writes late: never adopted."""
    rig = await presses.open()
    rig.heads[0].lags_commands = True
    for target in (72, 71):
        await presses.target(target)
    presses.snap("writes_queued")
    while await rig.report_next_command():
        presses.snap("own_write_shown")
    await rig.later(ADOPT_DELAY + 1)
    presses.snap("nothing_adopted")
    await presses.target(73)
    await presses.press(low=60, high=62)
    await rig.settle()
    presses.snap("press_while_lagging")
    await rig.report_next_command()
    presses.snap("own_write_shown_inside_the_hold")
    await rig.later(ADOPT_DELAY + 1)
    presses.snap("after_the_hold")


async def setpoint_change(presses: Presses) -> None:
    """A setpoint change holds, a compute inside the hold leaves it, and it is adopted."""
    rig = await presses.open()
    presses.snap("cooling")
    await presses.press(low=64, high=66)
    presses.snap("pressed")
    await rig.later(ADOPT_DELAY - 1)
    presses.snap("held")
    await rig.later(2)
    presses.snap("adopted")
    await presses.press(low=66, high=68)
    await rig.later(ADOPT_DELAY - 1)
    await presses.press(low=62, high=64)
    presses.snap("pressed_again_inside_the_hold")
    await rig.later(ADOPT_DELAY - 1)
    presses.snap("the_hold_started_over")
    await rig.later(2)
    presses.snap("the_last_press_adopted")


async def undone(presses: Presses) -> None:
    """A change put back inside the hold adopts nothing."""
    rig = await presses.open()
    await presses.press(low=64, high=66)
    await rig.later(2)
    presses.snap("pressed")
    await presses.press(low=TARGET - 2, high=TARGET)
    presses.snap("put_back")
    await rig.later(ADOPT_DELAY + 1)
    presses.snap("nothing_adopted")


async def mode_change(presses: Presses) -> None:
    """The head turned off disables the room; turned back on with a setpoint, it enables it
    with that setpoint; a heat/cool flip alone moves nothing."""
    rig = await presses.open()
    await presses.press(mode="off")
    presses.snap("turned_off")
    await rig.later(ADOPT_DELAY + 1)
    presses.snap("room_disabled")
    await presses.press(mode="cool", low=66, high=68)
    presses.snap("turned_on")
    await rig.later(ADOPT_DELAY + 1)
    presses.snap("room_enabled_at_the_setpoint")
    await presses.press(mode="heat")
    presses.snap("flipped_to_heat")
    await rig.later(ADOPT_DELAY + 1)
    presses.snap("flip_not_adopted")


async def refused(presses: Presses) -> None:
    """The head refuses MXZ's setpoint once; the next shows, then a press away and back."""
    rig = await presses.open()
    head = rig.heads[0]
    set_temperature = type(head).async_set_temperature
    refusals: list[dict[str, Any]] = []

    async def write(**kwargs: Any) -> None:
        if not refusals:
            refusals.append(kwargs)
            raise HomeAssistantError("invented cloud timeout")
        await set_temperature(head, **kwargs)

    presses.monkeypatch.setattr(head, "async_set_temperature", write)
    await presses.target(72)
    presses.snap("target_72_refused")
    for n in range(3):
        await rig.settle()
        presses.snap(f"settle_{n}")
    await presses.press(low=64, high=66)
    await rig.later(ADOPT_DELAY + 1)
    presses.snap("pressed_66_adopted")
    await presses.press(low=70, high=72)
    await rig.later(ADOPT_DELAY + 1)
    presses.snap("pressed_72_adopted")


async def eco_during_the_hold(presses: Presses) -> None:
    """Eco during the hold: the change no longer counts and the plan writes at once."""
    rig = await presses.open()
    await presses.press(low=64, high=66)
    presses.snap("pressed")
    await rig.switch("_eco_idle", True)
    presses.snap("eco_on")
    await rig.later(ADOPT_DELAY + 1)
    presses.snap("nothing_adopted")


async def option_off(presses: Presses) -> None:
    """With Accept changes off, a press arms nothing and the plan writes over it."""
    rig = await presses.open(data={CONF_ACCEPT_HEAD_CHANGES: False})
    await presses.press(low=64, high=66)
    presses.snap("pressed")
    await rig.settle()
    presses.snap("overwritten")
    await presses.press(mode="off")
    await rig.later(ADOPT_DELAY + 1)
    presses.snap("off_healed_not_adopted")


SCENARIOS: dict[str, Scenario] = {
    scenario.__name__: scenario
    for scenario in (
        own_echo, setpoint_change, undone, mode_change, refused, eco_during_the_hold, option_off,
    )
}
