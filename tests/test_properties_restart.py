"""Hypothesis properties of restart continuity, on the real integration.

The rule (docs/formal/README.md): a reload or restart changes nothing a person or a
head can see. The shared mode, what is left of the hold, each head's mode and fan,
each fan hold, the door/window state and an override all come back from what was
stored. The steps before the start are Hypothesis's; the oracle is the state
before it, read after two computes, so nothing was still settling.

The two known unit exceptions (the cloud1104 PARKED.md, items 1 and 2)
are pinned at the end as what they are: tests of today's documented behavior, not
xfails, since CI's phase plugin (tools/pytest_phases.py) refuses xfails. A fix
fails them, and they then change with it.
"""

from __future__ import annotations

from fractions import Fraction
from math import floor
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from hypothesis import given
from hypothesis import strategies as st

from custom_components.mxz_coordinator.const import (
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    FAN_AUTO,
    FAN_LADDER,
    MODE_COOL,
    MODE_HEAT,
    unit_profile,
)
from tests.property_rig import HOLD, OPEN_DELAY, SEQUENCES, TARGET, Rig, open_rig
from tests.test_drive import _set_target

MODES = (MODE_COOL, MODE_HEAT)
TEMPS = (TARGET - 6, TARGET - 2, TARGET, TARGET + 2, TARGET + 6)


def _visible(rig: Rig) -> dict[str, Any]:
    coord = rig.coord
    return {
        "mode": coord.current_shared_mode,
        "hold_left": round(coord.mode_hold_remaining(), 3),
        "head_modes": [head.mode for head in rig.heads],
        "head_fans": [head.fan for head in rig.heads],
        "fan_held": [rig.fan_held(0), rig.fan_held(1)],
        "fan_auto": [rig.fan_auto_switch(0), rig.fan_auto_switch(1)],
        "opening": coord.opening_state_of("primary"),
        "override": coord.window_override("primary"),
    }


async def _drive(rig: Rig, step: tuple[Any, ...]) -> None:
    kind = step[0]
    if kind == "temp":
        await rig.temp(step[1], step[2])
    elif kind == "later":
        await rig.later(step[1])
    elif kind == "select":
        await rig.select_mode(step[1])
    elif kind == "pick":
        await rig.person_fan(step[1], step[2])
    elif kind == "window":
        await rig.window(step[1])
    elif kind == "override":
        await rig.switch("_primary_window_override", step[1])
    else:
        await rig.person_mode(step[1])


steps = st.lists(
    st.one_of(
        st.tuples(st.just("temp"), st.integers(0, 1), st.sampled_from(TEMPS)),
        st.tuples(st.just("later"), st.sampled_from((1, HOLD // 2, HOLD + 1, OPEN_DELAY + 1))),
        st.tuples(st.just("select"), st.sampled_from(MODES)),
        st.tuples(st.just("pick"), st.sampled_from((FAN_AUTO, *FAN_LADDER)), st.integers(0, 1)),
        st.tuples(st.just("window"), st.sampled_from(("on", "off"))),
        st.tuples(st.just("override"), st.booleans()),
        st.tuples(st.just("person"), st.sampled_from(("cool", "heat", "off"))),
    ),
    max_size=10,
)


# Most examples reach a state a start has to bring back: a pause, an override or a held fan.
_PAUSE = [("window", "on"), ("later", OPEN_DELAY + 1)]
openers = st.sampled_from((
    [],
    _PAUSE,
    [*_PAUSE, ("override", True)],
    [*_PAUSE, ("person", "cool")],
    [("pick", "high", 0)],
))


@SEQUENCES
@given(
    first=st.tuples(st.sampled_from(TEMPS), st.sampled_from(TEMPS)),
    opener=openers,
    steps=steps,
    starts=st.lists(st.sampled_from(("reload", "restart")), min_size=1, max_size=3),
)
async def test_a_start_changes_nothing_anyone_can_see(
    hass: HomeAssistant,
    freezer: Any,
    first: tuple[float, float],
    opener: list[tuple[Any, ...]],
    steps: list[tuple[Any, ...]],
    starts: list[str],
) -> None:
    rig = await open_rig(hass, freezer, temps=first, window="off")
    try:
        for step in (*opener, *steps):
            await _drive(rig, step)
        await rig.settle()
        await rig.settle()
        before = _visible(rig)
        for kind in starts:
            await (rig.reload() if kind == "reload" else rig.restart())
            assert _visible(rig) == before
    finally:
        await rig.close()


def _converted(value: float, celsius: bool) -> float:
    """The formula, in exact arithmetic, on the new unit's target step, half up."""
    exact = (Fraction(value) - 32) * 5 / 9 if celsius else Fraction(value) * 9 / 5 + 32
    step = Fraction(1, 2) if celsius else Fraction(1)
    return float(floor(exact / step + Fraction(1, 2)) * step)


async def _start(rig: Rig, kind: str) -> None:
    await (rig.reload() if kind == "reload" else rig.restart())


@SEQUENCES
@given(
    to_fahrenheit=st.booleans(),
    start=st.sampled_from(("reload", "restart")),
    data=st.data(),
)
async def test_a_target_is_converted_on_a_unit_switch(
    hass: HomeAssistant, freezer: Any, to_fahrenheit: bool, start: str, data: Any
) -> None:
    """After a unit switch and a start, a stored target is the same temperature on the new
    unit's step and shows in that unit. More starts change nothing, and switching back
    lands within a step of where it began, and stays there however often it's repeated."""
    before, after = (METRIC_SYSTEM, US_CUSTOMARY_SYSTEM) if to_fahrenheit else (US_CUSTOMARY_SYSTEM, METRIC_SYSTEM)
    begin = unit_profile(to_fahrenheit)
    low, high = begin["defaults"][CONF_CLAMP_MIN], begin["defaults"][CONF_CLAMP_MAX]
    step = begin["target_step"]
    target = low + step * data.draw(st.integers(0, int((high - low) / step)))
    rig = await open_rig(hass, freezer)
    eid = rig.eid("_primary_target")
    try:
        hass.config.units = before
        await rig.reload()
        await _set_target(hass, eid, target)
        hass.config.units = after
        await _start(rig, start)
        once = _converted(target, celsius=not to_fahrenheit)
        assert rig.coord.zones[0].target == once
        assert float(hass.states.get(eid).state) == once
        assert hass.states.get(eid).attributes["unit_of_measurement"] == after.temperature_unit
        await rig.reload()
        await rig.restart()
        assert rig.coord.zones[0].target == once

        hass.config.units = before
        await _start(rig, start)
        back = rig.coord.zones[0].target
        assert back == _converted(once, celsius=to_fahrenheit)
        assert abs(back - target) <= step
        if not to_fahrenheit:
            assert back == target  # every whole °F has a place on the °C step and back
        for _ in range(2):
            hass.config.units = after
            await _start(rig, start)
            assert rig.coord.zones[0].target == _converted(back, celsius=not to_fahrenheit)
            hass.config.units = before
            await _start(rig, start)
            assert rig.coord.zones[0].target == back
    finally:
        await rig.close()


@pytest.mark.parametrize(("start", "switched"), [(US_CUSTOMARY_SYSTEM, METRIC_SYSTEM), (METRIC_SYSTEM, US_CUSTOMARY_SYSTEM)])
async def test_a_live_unit_switch_reloads_with_a_held_fan_and_the_hold_kept(
    hass: HomeAssistant, freezer: Any, start: Any, switched: Any
) -> None:
    """A live switch reloads the entry by itself, so the running coordinator is in the new
    unit straight away, with the manual fan and the mode-change hold as they were."""
    rig = await open_rig(hass, freezer)
    hass.config.units = start
    await rig.reload()
    assert rig.coord.temp_unit == start.temperature_unit
    await rig.person_fan("high")
    assert rig.fan_held() is True
    await rig.select_mode(MODE_HEAT)
    before = _visible(rig)
    assert before["hold_left"] > 0
    old = rig.coord
    writes = len(rig.heads[0].fan_writes)
    await hass.config.async_update(unit_system="metric" if switched is METRIC_SYSTEM else "us_customary")
    await hass.async_block_till_done()
    await rig.settle()
    assert hass.config.units.temperature_unit == switched.temperature_unit
    assert rig.coord is not old
    assert rig.coord.temp_unit == switched.temperature_unit
    assert hass.states.get(rig.eid("_primary_target")).attributes["unit_of_measurement"] == switched.temperature_unit
    assert _visible(rig) == before
    assert rig.heads[0].fan_writes[writes:] == []
    await rig.close()
