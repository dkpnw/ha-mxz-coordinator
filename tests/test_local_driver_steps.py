"""LocalDriver.async_deliver's own steps around the mode write, on a stand-in host.

The real driver runs; the host around it is a stand-in that logs the fan hold's calls and the
head's writes, and can flip the standby hold while the head's write is in flight. That edge is
the point: the hand-back is decided before the write, and the fan step after it reads the hold
as it is when that step runs, so a hold that starts during the write stops the boost step and
one that ends lets it run (review 1 of planner1118c; 7ebcf84 did this with ``elif not
host.inhibited`` after the write). The idle action fan_only never hands the fan back, which no
recording reaches: the only off with an engage that such a plan sends is eco's, and eco stops
the hand-back too, unless eco ends between the plan and the delivery.

Each expectation is the rule stated above, not a reading of the code. Invented head only.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import (
    ENGAGE_SATISFIED,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_OFF,
)
from custom_components.mxz_coordinator.grid import Grid
from custom_components.mxz_coordinator.heads import LocalDriver

HEAD = "climate.invented_head"


class Fan:
    def __init__(self, log: list[tuple[str, ...]]) -> None:
        self._log = log

    async def async_apply(self, climate_id: str, act: str, delta: float) -> None:
        self._log.append(("fan", act))

    def drop_rung(self, climate_id: str) -> None:
        self._log.append(("drop_rung",))


class Deliveries:
    def __init__(self, host: Host) -> None:
        self._host = host

    def owned(self, delivery: object) -> bool:
        return True

    async def async_send(self, climate_id: str, service: str, data: dict[str, Any]) -> None:
        host = self._host
        host.log.append(("send", service, str(data.get("hvac_mode"))))
        if host.hold_after_write is not None:
            host.inhibited = host.hold_after_write
        await asyncio.sleep(0)


class Floor:
    def accepted(self, climate_id: str, delivery: object) -> None:
        pass


class Heads:
    """A head with no band and no grid of its own: nothing is clamped, and its 70 °F is never
    where a command puts it."""

    def read(self, climate_id: str) -> Grid:
        return Grid(system_unit="°F", target_step=1.0, clamp_min=50.0, clamp_max=90.0)


class Host:
    """The coordinator members ``LocalDriver.async_deliver`` reads or calls."""

    def __init__(
        self, hass: HomeAssistant, *, idle_action: str, inhibited: bool,
        hold_after_write: bool | None,
    ) -> None:
        self.hass = hass
        self.idle_action = idle_action
        self.inhibited = inhibited
        self.hold_after_write = hold_after_write
        self.log: list[tuple[str, ...]] = []
        self.fan = Fan(self.log)
        self.deliveries = Deliveries(self)
        self.floor = Floor()

    def clock(self) -> float:
        return 1000.0

    def eco_active(self) -> bool:
        return False

    def note_head_act(self, *args: Any) -> None:
        pass


async def _deliver(
    hass: HomeAssistant, act: str, engage: str, *, shows: str, idle_action: str = IDLE_ACTION_OFF,
    inhibited: bool = False, hold_after_write: bool | None = None,
) -> list[tuple[str, ...]]:
    hass.states.async_set(HEAD, shows, {"supported_features": 0, "temperature": 70.0})
    host = Host(hass, idle_action=idle_action, inhibited=inhibited, hold_after_write=hold_after_write)
    driver = LocalDriver(HEAD, host, Heads())  # type: ignore[arg-type]
    await driver.async_deliver(object(), act, 68.0, 72.0, 3.0, engage)  # type: ignore[arg-type]
    return host.log


def _write(act: str) -> tuple[str, ...]:
    return ("send", "set_temperature" if act == MODE_COOL else "set_hvac_mode", act)


@pytest.mark.parametrize(
    ("before", "after"),
    [(False, True), (True, False), (False, None), (True, None)],
    ids=["hold_starts", "hold_ends", "no_hold", "held_throughout"],
)
@pytest.mark.parametrize("act", [MODE_COOL, MODE_FAN_ONLY])
async def test_the_boost_step_reads_the_hold_after_the_mode_write(
    hass: HomeAssistant, act: str, before: bool, after: bool | None,
) -> None:
    engage = MODE_COOL if act == MODE_COOL else ENGAGE_SATISFIED
    log = await _deliver(
        hass, act, engage, shows=MODE_OFF, inhibited=before, hold_after_write=after,
    )
    held_when_the_step_runs = before if after is None else after
    expected = [_write(act)] + ([] if held_when_the_step_runs else [("fan", act)])
    assert log == expected


@pytest.mark.parametrize("hold_after_write", [None, True], ids=["no_hold", "hold_starts"])
async def test_a_hand_back_drops_the_rung_even_if_a_hold_starts_during_the_off(
    hass: HomeAssistant, hold_after_write: bool | None,
) -> None:
    log = await _deliver(
        hass, MODE_OFF, ENGAGE_SATISFIED, shows=MODE_COOL, hold_after_write=hold_after_write,
    )
    assert log == [("fan", MODE_FAN_ONLY), _write(MODE_OFF), ("drop_rung",)]


@pytest.mark.parametrize(
    ("idle_action", "expected"),
    [
        (IDLE_ACTION_OFF, [("fan", MODE_FAN_ONLY), _write(MODE_OFF), ("drop_rung",)]),
        (IDLE_ACTION_FAN_ONLY, [_write(MODE_OFF), ("fan", MODE_OFF)]),
    ],
    ids=["idle_off_hands_back", "idle_fan_only_never_hands_back"],
)
async def test_the_idle_action_fan_only_never_hands_the_fan_back(
    hass: HomeAssistant, idle_action: str, expected: list[tuple[str, ...]],
) -> None:
    log = await _deliver(
        hass, MODE_OFF, ENGAGE_SATISFIED, shows=MODE_COOL, idle_action=idle_action,
    )
    assert log == expected
