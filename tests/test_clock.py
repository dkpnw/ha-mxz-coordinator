"""MXZ's clock on its own: one wait per name and key, and nothing runs once cancelled or retired.

The production ``Clock`` runs on Home Assistant's loop here. The waits that a test fires by
hand are held off the loop by the suite's ``Clock`` (``tests/clocks.py``), so a callback the
loop "already took" can be run after its wait was cancelled, replaced or retired.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.mxz_coordinator.clock import Clock as MXZClock
from tests.clocks import Clock, Time


async def _later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def _held(hass: HomeAssistant, *names: str) -> Clock:
    clock = Clock(hass, time=Time())
    clock.held.update(names)
    return clock


async def test_a_wait_runs_once_at_its_delay(hass: HomeAssistant) -> None:
    clock = MXZClock(hass)
    ran: list[str] = []
    clock.call_later(10, lambda: ran.append("dwell"), name="dwell")
    assert clock.waiting("dwell")
    await _later(hass, 9)
    assert ran == []
    await _later(hass, 11)
    assert ran == ["dwell"]
    assert not clock.waiting("dwell")
    await _later(hass, 30)
    assert ran == ["dwell"]


async def test_arming_again_replaces_the_wait(hass: HomeAssistant) -> None:
    clock = MXZClock(hass)
    ran: list[str] = []
    clock.call_later(5, lambda: ran.append("first"), name="coast")
    clock.call_later(20, lambda: ran.append("second"), name="coast")
    await _later(hass, 10)
    assert ran == []  # the first is gone, not waiting beside the second
    await _later(hass, 25)
    assert ran == ["second"]


async def test_each_key_is_its_own_wait(hass: HomeAssistant) -> None:
    clock = MXZClock(hass)
    ran: list[str] = []
    clock.call_later(5, lambda: ran.append("den"), name="opening", key="den")
    clock.call_later(5, lambda: ran.append("study"), name="opening", key="study")
    clock.call_later(5, lambda: ran.append("dry"), name="dry", key="den")
    clock.cancel("opening", "study")
    assert (clock.waiting("opening", "den"), clock.waiting("opening", "study")) == (True, False)
    await _later(hass, 6)
    assert sorted(ran) == ["den", "dry"]


async def test_cancel_all_ends_every_key_of_that_name_only(hass: HomeAssistant) -> None:
    clock = MXZClock(hass)
    ran: list[str] = []
    for key in ("den", "study"):
        clock.call_later(5, lambda key=key: ran.append(key), name="opening", key=key)
    clock.call_later(5, lambda: ran.append("lost"), name="lost")
    clock.cancel_all("opening")
    await _later(hass, 6)
    assert ran == ["lost"]


async def test_a_returned_cancel_ends_only_its_own_wait(hass: HomeAssistant) -> None:
    clock = MXZClock(hass)
    ran: list[str] = []
    old = clock.call_later(5, lambda: ran.append("old"), name="refresh_followup")
    clock.call_later(5, lambda: ran.append("new"), name="refresh_followup")
    old()
    assert clock.waiting("refresh_followup")
    await _later(hass, 6)
    assert ran == ["new"]


@pytest.mark.parametrize("end", ["cancelled", "replaced", "retired"])
async def test_a_callback_the_loop_already_took_does_nothing(hass: HomeAssistant, end: str) -> None:
    clock = _held(hass, "dwell")
    ran: list[str] = []
    clock.call_later(5, lambda: ran.append("first"), name="dwell")
    (taken,) = clock.armed("dwell")
    if end == "cancelled":
        clock.cancel("dwell")
    elif end == "replaced":
        clock.call_later(5, lambda: ran.append("second"), name="dwell")
    else:
        clock.retire()
    assert taken.cancelled
    taken.fire()
    assert ran == []


async def test_the_control_a_callback_still_armed_runs(hass: HomeAssistant) -> None:
    clock = _held(hass, "dwell")
    ran: list[str] = []
    clock.call_later(5, lambda: ran.append("dwell"), name="dwell")
    (taken,) = clock.armed("dwell")
    taken.fire()
    assert ran == ["dwell"]
    assert not clock.waiting("dwell")
    taken.fire()  # a second run of the same callback finds nothing armed
    assert ran == ["dwell"]


async def test_retire_cancels_every_wait_and_arms_none_again(hass: HomeAssistant) -> None:
    clock = MXZClock(hass)
    ran: list[str] = []
    clock.call_later(5, lambda: ran.append("dwell"), name="dwell")
    clock.call_later(5, lambda: ran.append("heal"), name="heal", key=("climate.den", "off"))
    clock.retire()
    cancel = clock.call_later(1, lambda: ran.append("late"), name="dwell")
    cancel()
    assert not clock.waiting("dwell") and not clock.waiting("heal", ("climate.den", "off"))
    await _later(hass, 10)
    assert ran == []
    assert clock.retired


async def test_only_a_wait_asked_to_is_cancelled_at_shutdown(hass: HomeAssistant) -> None:
    clock = Clock(hass, time=Time())
    clock.call_later(900, lambda: None, name="repair", key="sensor.den", cancel_on_shutdown=True)
    clock.call_later(600, lambda: None, name="dwell")
    repair, dwell = clock.log
    assert (repair.job.cancel_on_shutdown, dwell.job.cancel_on_shutdown) == (True, None)
    clock.retire()


async def test_it_reads_the_monotonic_clock(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = MXZClock(hass)
    monkeypatch.setattr(time, "monotonic", lambda: 1234.5)
    assert clock() == 1234.5
