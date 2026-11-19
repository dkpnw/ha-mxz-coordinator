"""What a local head is sent for each command, recorded from the real coordinator.

The send-shaping refactor must change nothing anyone can see. Each scenario here runs on
``tests/planner_parity.py``'s ``House`` over three invented heads and records, per step, what
``House.snap`` does (every climate call in order, and what each head shows) and MXZ's memory of
its own head writes, as the room state store keeps it.

The heads: a °C head on platform ``esphome`` with a 0.5 °C grid that latches each setpoint on
it, half up, as the CN105 does (the house-style head); a °F range head on platform
``invented_house``, which takes a mode change as its own call; and a °F single-setpoint head on
``invented_house``. Each has a narrower band than MXZ's clamp. The scenarios run them in a °F
house and in a °C house with half-degree targets: a cool call, the same command again (skipped
where the head already shows it on its grid), a flip to heat (one combined call on the esphome
head, two on the others), targets at both clamp edges (held to each head's band), and off.

``tests/test_send_parity.py`` compares each scenario with ``tests/fixtures/send_parity/``. Those
were captured on d4cffd2, before the send shaping moved to ``grid.local_calls``; run with
``MXZ_PARITY_CAPTURE=1`` to rewrite them, only on a commit whose behavior is known right.

Every room, head, sensor and value is invented; nothing here is a hardware result.
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import CONF_MODE_HYSTERESIS
from tests import planner_parity
from tests.cloud_heads import register
from tests.planner_parity import SLUGS, House
from tests.test_drive import MockHead, MockHeadC, _eid
from tests.test_single_setpoint import MockSingleSetpointHead

FIXTURES = Path(__file__).with_name("fixtures") / "send_parity"
INVENTED = "invented_house"


def recorded(name: str) -> Path:
    return FIXTURES / f"{name}.json"


class GridHeadC(MockHeadC):
    """A °C head on a 0.5 °C grid, 16 to 30 °C, that latches each setpoint half up on its grid."""

    PLATFORM = "esphome"
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 16.0
    _attr_max_temp = 30.0

    async def async_set_temperature(self, **kwargs: Any) -> None:
        for key in ("target_temp_low", "target_temp_high"):
            if kwargs.get(key) is not None:
                kwargs[key] = math.floor(kwargs[key] / 0.5 + 0.5) * 0.5
        await super().async_set_temperature(**kwargs)


class RangeHeadF(MockHead):
    """A °F range head on another platform, 62 to 84 °F, whole degrees."""

    PLATFORM = INVENTED
    _attr_native_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_target_temperature_step = 1.0
    _attr_min_temp = 62.0
    _attr_max_temp = 84.0


class SingleHeadF(MockSingleSetpointHead):
    """A °F single-setpoint head on another platform, 61 to 85 °F."""

    PLATFORM = INVENTED
    _attr_min_temp = 61.0
    _attr_max_temp = 85.0
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )


HEADS = [GridHeadC, RangeHeadF, SingleHeadF]


class Sends(House):
    """A ``House`` whose heads register on their own platforms, with MXZ's write memory."""

    def __init__(self, hass: HomeAssistant, freezer: Any, monkeypatch: Any) -> None:
        super().__init__(hass, freezer)

        async def _register(hass: HomeAssistant, heads: dict[str, list[Any]]) -> None:
            by_platform: dict[str, list[Any]] = {}
            for group in heads.values():
                for head in group:
                    by_platform.setdefault(getattr(head, "PLATFORM", "esphome"), []).append(head)
            await register(hass, by_platform)

        monkeypatch.setattr(planner_parity, "register", _register)

    def snap(self, step: str) -> None:
        super().snap(step)
        self.steps[-1]["memory"] = json.loads(json.dumps(self.coord._head_writes_record()))


Scenario = Callable[[Sends], Awaitable[None]]


async def _edge(house: Sends, room: int, bound: str) -> None:
    """The room's target at the edge its number offers (the clamp held to the head's band)."""
    number = _eid(house.hass, house.entry, f"_{SLUGS[room]}_target")
    await house.target(room, float(house.hass.states.get(number).attributes[bound]))


async def _walk(house: Sends, *, cool: float, heat: float) -> None:
    house.snap("start")
    for room in range(3):
        await house.temp(room, cool)
    house.snap("cool_call")
    await house.recompute()
    house.snap("same_command_again")
    for room in range(3):
        await _edge(house, room, "max")
    await house.recompute()
    house.snap("targets_at_the_top")
    for room in range(3):
        await house.temp(room, heat)
    for room in range(3):
        await _edge(house, room, "min")
    await house.recompute()
    house.snap("flip_to_heat_at_the_bottom")
    await house.recompute()
    house.snap("heat_again")
    await house.switch("_primary_enable", False)
    await house.switch("_secondary_enable", False)
    await house.switch("_zone_3_enable", False)
    await house.recompute()
    house.snap("off")


async def fahrenheit_house(house: Sends) -> None:
    await house.setup([{}, {}, {}], head_classes=HEADS, **{CONF_MODE_HYSTERESIS: 0})
    await _walk(house, cool=76, heat=58)


async def celsius_house(house: Sends) -> None:
    await house.setup([{}, {}, {}], celsius=True, head_classes=HEADS, **{CONF_MODE_HYSTERESIS: 0})
    for room, target in enumerate((21.5, 22.5, 20.5)):
        await house.target(room, target)
    await house.recompute()
    await _walk(house, cool=24.5, heat=15.5)


SCENARIOS: dict[str, Scenario] = {
    scenario.__name__: scenario for scenario in (fahrenheit_house, celsius_house)
}
