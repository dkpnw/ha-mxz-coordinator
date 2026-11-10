"""An unloaded entry leaves no wakeup behind, and none it had armed reaches a head.

A coil-dry run, a door/window delay and a cloud head's write interval each arm
a wakeup. Each case first shows the wakeup doing its job on a loaded entry,
then unloads the entry while it is armed: nothing is left waiting, and time
past it sends the head nothing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from tests.clocks import pass_time
from tests.cloud_heads import SENSORS, past_startup, record, writes
from tests.peek import armed, armed_wakeups
from tests.test_cloud_heads import _comfort_pair, _driven
from tests.test_door_window import OPEN
from tests.test_door_window import _setup as _window_rig
from tests.test_drive import SENSOR_A, _eid, _recompute, _set_temp
from tests.test_idle_dry import _setup_dry


async def _unload(hass: HomeAssistant, entry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _coil_drying(hass: HomeAssistant):
    """The Primary cooled and is satisfied: fan_only while its coil dries."""
    entry, head_a, _b = await _setup_dry(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"
    assert armed(entry.runtime_data, "dry", head_a)
    return entry, head_a


async def _past_coil_dry(hass: HomeAssistant, coord) -> None:
    pass_time(coord, coord.coil_dry_seconds + 5)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=coord.coil_dry_seconds + 5))
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=coord.coil_dry_seconds + 20))
    await hass.async_block_till_done()


@pytest.mark.parametrize("unload", [False, True], ids=["control_loaded", "unloaded"])
async def test_a_coil_dry_wakeup(hass: HomeAssistant, unload: bool) -> None:
    entry, head_a = await _coil_drying(hass)
    coord = entry.runtime_data
    if unload:
        await _unload(hass, entry)
        assert armed_wakeups(coord) == set()
    await _past_coil_dry(hass, coord)
    assert hass.states.get(head_a).state == ("fan_only" if unload else "off")


@pytest.mark.parametrize("unload", [False, True], ids=["control_loaded", "unloaded"])
async def test_a_door_window_delay(hass: HomeAssistant, freezer, unload: bool) -> None:
    rig = await _window_rig(hass, freezer)
    await rig.window("on")
    assert armed(rig.coord, "opening", "primary")
    coord = rig.coord
    if unload:
        await _unload(hass, rig.entry)
        assert armed_wakeups(coord) == set()
    rig.forget()
    await rig.later(OPEN + 1)
    assert rig.modes_sent() == ([] if unload else ["off"])


@pytest.mark.parametrize("unload", [False, True], ids=["control_loaded", "unloaded"])
async def test_a_cloud_heads_write_interval(hass: HomeAssistant, freezer, unload: bool) -> None:
    """A target change inside a cloud head's write interval waits for its end."""
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 65)
    calls = record(hass)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, entry, "_primary_target"), "value": 72}, blocking=True,
    )
    await hass.async_block_till_done()
    coord = entry.runtime_data
    assert armed(coord, "cloud")
    if unload:
        await _unload(hass, entry)
        assert armed_wakeups(coord) == set()
    for _ in range(4):  # 4 x 31 s: past the 60 s interval and the refresh cooldown
        freezer.tick(timedelta(seconds=31))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    sent = ("set_temperature", {"temperature": 74.0}) in writes(calls, a.entity_id)
    assert sent is not unload

