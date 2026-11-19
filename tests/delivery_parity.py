"""What each head's delivery sends and in what order, recorded from the real coordinator.

The delivery-steps refactor must change nothing anyone can see. Each scenario here runs on
``tests/planner_parity.py``'s ``House`` (a real MXZ entry over invented heads) and records, per
step, what ``House.snap`` does (the plan, every climate call in order, every wait armed, the
mode record, the coil-dry stamps, what each head shows) and the waits still armed then, so a
coil-dry wait a delivery ended shows too.

The scenarios are the beats of a local head's delivery: a head parked off with its fan handed
back to auto first, and without (an off head, the idle action fan_only, eco, a standby hold);
the coil-dry run after cooling and its end; a heat run's last-run stamp; a fixed standby park;
a reading that rises under an eco standby hold.

``tests/test_delivery_parity.py`` compares each scenario with
``tests/fixtures/delivery_parity/<scenario>.json``. Those were captured on 7ebcf84, before the
delivery steps moved to ``planner.delivery_steps``; run with ``MXZ_PARITY_CAPTURE=1`` to rewrite
them, only on a commit whose behavior is known right.

Every room, head, sensor and value is invented; nothing here is a hardware result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from homeassistant.components.climate import HVACMode

from custom_components.mxz_coordinator.const import (
    CONF_COIL_DRY_MINUTES,
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
    INHIBIT_ACTION_ECO,
    INHIBIT_ACTION_OFF,
)
from tests.planner_parity import LEGACY_WAITS, House

FIXTURES = Path(__file__).with_name("fixtures") / "delivery_parity"
INHIBIT = "binary_sensor.parity_inhibit"


def recorded(name: str) -> Path:
    return FIXTURES / f"{name}.json"


class Deliveries(House):
    """A ``House`` that also records the waits still armed at each step."""

    def snap(self, step: str) -> None:
        super().snap(step)
        clock = self.coord.clock
        self.steps[-1]["armed"] = sorted(
            [name, None if key is None else str(key)]
            for name, key in clock._waits
            if name not in LEGACY_WAITS
        )


Scenario = Callable[[Deliveries], Awaitable[None]]


async def _cool_then_satisfied(house: Deliveries) -> None:
    await house.temp(0, 76)
    house.snap("cooling")
    await house.temp(0, 69.5)
    house.snap("satisfied_after_cooling")


async def parked_off(house: Deliveries) -> None:
    """Idle action off: a running head gets its fan handed back before the off; an off head,
    and a head a person turned on in fan_only, are parked as the plan says."""
    await house.setup([{}, {}], **{CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF})
    house.snap("start")
    await _cool_then_satisfied(house)
    await house.temp(0, 69.4)
    house.snap("already_off")
    await house.person(0, HVACMode.FAN_ONLY)
    await house.recompute()
    house.snap("person_fan_only")
    await house.temp(1, 64)
    house.snap("heating")
    await house.temp(1, 70.5)
    house.snap("satisfied_after_heating")


async def coil_dry(house: Deliveries) -> None:
    """Off after dry: cooling owes a coil-dry run in fan_only, a new run ends its wait, and its
    end is an off (the fan already shows auto, so no hand-back goes out); a heat run owes none."""
    await house.setup(
        [{}, {}],
        **{CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY,
           CONF_COIL_DRY_MINUTES: 10},
    )
    house.snap("start")
    await _cool_then_satisfied(house)
    await house.later(300)
    await house.recompute()
    house.snap("drying")
    await house.temp(0, 76)
    house.snap("cooling_again_ends_the_dry_wait")
    await house.temp(0, 69.5)
    house.snap("drying_again")
    await house.later(601)
    await house.recompute()
    house.snap("dry_over_off")
    await house.temp(1, 64)
    house.snap("heating")
    await house.temp(1, 70.5)
    house.snap("satisfied_after_heating")


async def fan_only_idle(house: Deliveries) -> None:
    """Idle action fan_only: no hand-back, the fan goes to auto after the mode."""
    await house.setup([{}, {}], **{CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY})
    await _cool_then_satisfied(house)
    await house.switch("_primary_enable", False)
    await house.recompute()
    house.snap("room_off")


async def eco(house: Deliveries) -> None:
    """Eco idle: a parked head gets no hand-back and its fan goes to auto."""
    await house.setup([{}, {}], **{CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF})
    await house.temp(0, 76)
    house.snap("cooling")
    await house.switch("_eco_idle", True)
    await house.recompute()
    house.snap("eco_on")
    await house.temp(0, 82)
    house.snap("eco_cooling")
    await house.temp(0, 69.5)
    house.snap("eco_satisfied")
    await house.switch("_eco_idle", False)
    await house.recompute()
    house.snap("eco_off")


async def standby_eco(house: Deliveries) -> None:
    """A standby hold in eco: the plan runs, with no fan step and no hand-back."""
    house.hass.states.async_set(INHIBIT, "off")
    await house.setup(
        [{}, {}],
        **{CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF,
           CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_ECO},
    )
    await house.temp(0, 82)
    house.snap("cooling")
    await house.state(INHIBIT, "on")
    await house.recompute()
    house.snap("held")
    await house.temp(0, 69.5)
    house.snap("held_satisfied")
    await house.state(INHIBIT, "off")
    await house.recompute()
    house.snap("released")


async def standby_eco_rise(house: Deliveries) -> None:
    """A standby hold in eco that starts before any run, then a rising reading: the hold's
    protection run gets no fan step, so the fan stays at auto where the hold found it; the
    release brings the boost step back."""
    house.hass.states.async_set(INHIBIT, "off")
    await house.setup(
        [{}, {}],
        **{CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF,
           CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_ECO},
    )
    house.snap("start")
    await house.state(INHIBIT, "on")
    await house.recompute()
    house.snap("held")
    await house.temp(0, 90)
    house.snap("held_hot")
    await house.state(INHIBIT, "off")
    await house.recompute()
    house.snap("released")


async def standby_park(house: Deliveries) -> None:
    """A fixed standby park: off with no fan machinery and no coil-dry note."""
    house.hass.states.async_set(INHIBIT, "off")
    await house.setup(
        [{}, {}],
        **{CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY,
           CONF_COIL_DRY_MINUTES: 10, CONF_INHIBIT_ENTITY: INHIBIT,
           CONF_INHIBIT_ACTION: INHIBIT_ACTION_OFF},
    )
    await house.temp(0, 76)
    house.snap("cooling")
    await house.state(INHIBIT, "on")
    await house.recompute()
    house.snap("parked")
    await house.state(INHIBIT, "off")
    await house.recompute()
    house.snap("released")


SCENARIOS: dict[str, Scenario] = {
    scenario.__name__: scenario
    for scenario in (
        parked_off, coil_dry, fan_only_idle, eco, standby_eco, standby_eco_rise, standby_park,
    )
}
