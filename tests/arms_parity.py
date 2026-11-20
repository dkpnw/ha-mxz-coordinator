"""Every call MXZ makes on its clock, recorded from the real coordinator on each supported release.

The wakeup refactor must change nothing anyone can see, and a wakeup is what a later compute
sees. This records, on ``tests/timing_parity.py``'s ``Timeline``, every call MXZ makes on its
clock (``clock.Clock``): ``call_later`` (name, key, delay), ``cancel`` (name, key; one that
finds nothing armed too) and ``cancel_all`` (name), with what made it (a script step, a compute,
a wakeup, the heartbeat), in order with the timeline's computes, asks, arms, fires and
heartbeats. A ``cancel`` that ``call_later`` makes of its own wait is not a separate entry.

The scenarios are every scenario of ``tests/timing_parity.py``, then three more for the gates the
wakeups apply after a compute: the Coordinator switch off and a fixed standby park (with a
hands-off room that votes by its run, a Comfort priority entity, a door/window room and a cloud
head, so every wakeup has something to arm); and a reload that retires the coordinator while its
plan's delivery runs (Home Assistant's unit switched), so that compute's wakeups are synced by a
retired coordinator whose clock still runs. Their steps keep off the instants where two of
MXZ's timers, or one and the request cooldown's end, fall due together (the startup wakeup at
40 s; the dwell and hold-end wakeups): Home Assistant does not fix the order of those
(``tests/test_sim_timing_parity.py`` names one).

``tests/test_arms_parity.py`` compares each scenario with
``tests/fixtures/arms_parity/<release>/<scenario>.json``. Those were captured on d4cffd2, with
the wakeups still synced by the coordinator's ``_sync_*`` methods (``cooldown_and_hold_off`` and
``dwell_and_hold`` on 37494ed's integration); run with
``MXZ_PARITY_CAPTURE=1`` to rewrite this release's, only on a commit whose behavior is known right.

Every room, head, sensor and value is invented; nothing here is a hardware result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable
from pathlib import Path
from typing import Any

from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.mxz_coordinator import clock as mxz_clock
from custom_components.mxz_coordinator.const import (
    CONF_COMFORT_STALE_HOURS,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_LOST_VOTE_TTL,
    CONF_MODE_HYSTERESIS,
    HEAD_TYPE_CLOUD,
    INHIBIT_ACTION_OFF,
    VOTE_BASIS_ACTION,
    ZONE_COMFORT_PRIORITY_ENTITY,
    ZONE_HANDS_OFF,
    ZONE_HANDS_OFF_VOTE_BASIS,
    ZONE_HEAD_TYPE,
    ZONE_OPENING_ENTITY,
    ZONE_OPENING_OPEN_DELAY,
    ZONE_OPENING_PAUSE,
)
from tests import planner_parity, timing_parity
from tests.cloud_heads import register
from tests.test_drive import MockHead
from tests.timing_parity import (
    RELEASE,
    RELEASES,
    Timeline,
    _head,
    _setup,
    _temp,
    _window,
)

FIXTURES = Path(__file__).with_name("fixtures") / "arms_parity"
INHIBIT = "binary_sensor.arms_inhibit"
WINDOW = "binary_sensor.arms_window"
COMFORT = "input_boolean.arms_comfort"

__all__ = ["RELEASE", "RELEASES", "SCENARIOS", "Arms", "recorded"]


def recorded(release: str, name: str) -> Path:
    return FIXTURES / release / f"{name}.json"


def _key(key: Hashable) -> str | None:
    return None if key is None else str(key)


class CloudHead(MockHead):
    """A °F head behind an invented cloud integration."""

    PLATFORM = "invented_cloud"


class Arms(Timeline):
    """A ``Timeline`` that also records every call MXZ makes on its clock."""

    def __init__(self, hass: HomeAssistant, freezer: Any, monkeypatch: Any) -> None:
        super().__init__(hass, freezer, monkeypatch)
        self._inside = False
        arms = self
        call_later = mxz_clock.Clock.call_later
        cancel = mxz_clock.Clock.cancel
        cancel_all = mxz_clock.Clock.cancel_all

        def _call_later(clock: mxz_clock.Clock, delay: float, action: Any, **kwargs: Any) -> Any:
            arms._clock_call("call_later", kwargs["name"], _key(kwargs.get("key")), round(delay, 6))
            arms._inside = True
            try:
                return call_later(clock, delay, action, **kwargs)
            finally:
                arms._inside = False

        def _cancel(clock: mxz_clock.Clock, name: str, key: Hashable = None) -> None:
            if not arms._inside:
                arms._clock_call("cancel", name, _key(key))
            cancel(clock, name, key)

        def _cancel_all(clock: mxz_clock.Clock, name: str) -> None:
            arms._clock_call("cancel_all", name)
            cancel_all(clock, name)

        monkeypatch.setattr(mxz_clock.Clock, "call_later", _call_later)
        monkeypatch.setattr(mxz_clock.Clock, "cancel", _cancel)
        monkeypatch.setattr(mxz_clock.Clock, "cancel_all", _cancel_all)

        async def _register(hass: HomeAssistant, heads: dict[str, list[Any]]) -> None:
            by_platform: dict[str, list[Any]] = {}
            for group in heads.values():
                for head in group:
                    by_platform.setdefault(getattr(head, "PLATFORM", "esphome"), []).append(head)
            await register(hass, by_platform)

        monkeypatch.setattr(planner_parity, "register", _register)

    def _clock_call(self, *call: Any) -> None:
        self._log(clock=list(call), **{"from": timing_parity._origin.get()})


Scenario = Callable[[Arms], Awaitable[None]]


async def _switch(house: Arms, suffix: str, on: bool) -> None:
    async def switch() -> None:
        await planner_parity.House.switch(house, suffix, on)

    await house.do(f"switch {suffix} {'on' if on else 'off'}", switch)


async def _state(house: Arms, entity_id: str, state: str) -> None:
    async def set_state() -> None:
        house.hass.states.async_set(entity_id, state)

    await house.do(f"state {entity_id} {state}", set_state)


async def _everything(house: Arms, **conf: Any) -> None:
    """Four rooms: a coordinated one, a hands-off one that votes by its run, a door/window room
    with a Comfort priority entity, and a cloud head's."""
    hass = house.hass
    hass.states.async_set(WINDOW, "off")
    hass.states.async_set(COMFORT, "on")
    hass.states.async_set(INHIBIT, "off")
    await _setup(
        house,
        [
            {},
            {ZONE_HANDS_OFF: True, ZONE_HANDS_OFF_VOTE_BASIS: VOTE_BASIS_ACTION},
            {
                ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_PAUSE: True, ZONE_OPENING_OPEN_DELAY: 30,
                ZONE_COMFORT_PRIORITY_ENTITY: COMFORT,
            },
            {ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD},
        ],
        enabled=[True, False, True, True],
        head_classes=[MockHead, MockHead, MockHead, CloudHead],
        **{
            CONF_MODE_HYSTERESIS: 600, CONF_LOST_VOTE_TTL: 5, CONF_COMFORT_STALE_HOURS: 1,
            CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_OFF,
            **conf,
        },
    )


async def coordinator_off(house: Arms) -> None:
    """Every wakeup armed, then the Coordinator switch off and on again."""
    await _everything(house)
    await house.until(32)
    await _temp(house, 0, 76)
    await house.until(47)
    await _head(house, 1, "heat", action="heating")
    await house.until(62)
    await _window(house, WINDOW, "on")
    await house.until(73)
    await _temp(house, 2, 64)
    await house.until(102)
    await _switch(house, "_coordinator_enable", False)
    await house.until(152)
    await _temp(house, 0, 78)
    await house.until(203)
    await _switch(house, "_coordinator_enable", True)
    await house.until(650)


async def standby_park(house: Arms) -> None:
    """Every wakeup armed, then a fixed standby park and its end."""
    await _everything(house)
    await house.until(32)
    await _temp(house, 0, 76)
    await house.until(47)
    await _head(house, 1, "heat", action="heating")
    await house.until(62)
    await _window(house, WINDOW, "on")
    await house.until(73)
    await _temp(house, 2, 64)
    await house.until(102)
    await _state(house, INHIBIT, "on")
    await house.until(152)
    await _temp(house, 0, 78)
    await house.until(203)
    await _state(house, INHIBIT, "off")
    await house.until(650)


class RetiringHead(MockHead):
    """A head that, on its first setpoint write, switches Home Assistant's unit: the
    coordinator retires while that write runs, and a reload follows."""

    switched = False

    async def async_set_temperature(self, **kwargs: Any) -> None:
        await super().async_set_temperature(**kwargs)
        if not RetiringHead.switched and self.hass is not None:
            RetiringHead.switched = True
            self.hass.config.units = METRIC_SYSTEM
            self.hass.bus.async_fire(EVENT_CORE_CONFIG_UPDATE)


async def retired(house: Arms) -> None:
    """A unit switch while a plan's delivery runs: that compute's wakeups are synced by a
    retired coordinator whose clock still runs, then the reload starts afresh."""
    RetiringHead.switched = True
    hass = house.hass
    hass.states.async_set(WINDOW, "off")
    hass.states.async_set(COMFORT, "on")
    await _setup(
        house,
        [
            {},
            {ZONE_HANDS_OFF: True, ZONE_HANDS_OFF_VOTE_BASIS: VOTE_BASIS_ACTION},
            {
                ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_PAUSE: True, ZONE_OPENING_OPEN_DELAY: 30,
                ZONE_COMFORT_PRIORITY_ENTITY: COMFORT,
            },
        ],
        enabled=[True, False, True],
        head_classes=[RetiringHead, MockHead, MockHead],
        **{CONF_MODE_HYSTERESIS: 600, CONF_LOST_VOTE_TTL: 5, CONF_COMFORT_STALE_HOURS: 1},
    )
    await house.until(31)
    await _head(house, 1, "heat", action="heating")
    await house.until(43)
    await _window(house, WINDOW, "on")
    await house.until(53)
    RetiringHead.switched = False
    await _temp(house, 0, 76)
    await house.until(400)


SCENARIOS: dict[str, Scenario] = {
    **timing_parity.SCENARIOS,
    "coordinator_off": coordinator_off,
    "standby_park": standby_park,
    "retired": retired,
}
