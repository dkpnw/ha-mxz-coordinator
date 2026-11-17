"""MXZ keeps each room's state in its own store, so a crash or a disabled helper changes nothing.

Before this store, targets, enables, the drift override, the dual-setpoint range, the global
switches and the fan hold were kept only in Home Assistant's restore data, which reaches disk
at a stop and every 15 minutes, and which an entity disabled in the registry never reads. Each
test here drives the real integration through its entities and a restart (``tests/restart.py``)
and reads what a person sees: the entities, the plan, and the heads. Rooms, heads and sensors
are invented.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.const import ATTR_RESTORED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.mxz_coordinator.const import ZONE_DUAL_SETPOINT, ZONE_HANDS_OFF
from tests.ha_test import async_fire_time_changed
from tests.property_rig import Rig, open_rig
from tests.restart import stopped

# Time for a head to settle, far shorter than restore data's 15-minute dump.
LATER = 2


async def _next_moment(rig: Rig) -> None:
    """The event loop runs what is due now, with no time passing.

    Under the frozen test clock a write scheduled for now waits until time is fired.
    """
    await rig.later(0)


async def _number(rig: Rig, suffix: str, value: float) -> None:
    await rig.hass.services.async_call(
        "number", "set_value", {"entity_id": rig.eid(suffix), "value": value}, blocking=True
    )
    await rig.hass.async_block_till_done()
    await rig.settle()


async def _due_now(rig: Rig) -> None:
    """What is due now runs, with no time passing and no compute of the test's own.

    ``_next_moment`` computes after, which would save whatever the gesture left unsaved.
    """
    async_fire_time_changed(rig.hass, dt_util.utcnow())
    await rig.hass.async_block_till_done()


def _state(rig: Rig, suffix: str) -> str:
    return rig.hass.states.get(rig.eid(suffix)).state


async def _change_target(rig: Rig) -> None:
    await _number(rig, "_primary_target", 74)


def _target_kept(rig: Rig) -> bool:
    return rig.row(0)["target"] == 74 and float(_state(rig, "_primary_target")) == 74


async def _change_drift(rig: Rig) -> None:
    await _number(rig, "_primary_drift", 3)


def _drift_kept(rig: Rig) -> bool:
    return rig.row(0)["drift"] == 3 and rig.hass.states.get(rig.eid("_primary_drift")).attributes["override"]


async def _change_enable(rig: Rig) -> None:
    await rig.switch("_secondary_enable", False)


def _enable_kept(rig: Rig) -> bool:
    return rig.row(1)["enabled"] is False and _state(rig, "_secondary_enable") == "off"


async def _change_eco(rig: Rig) -> None:
    await rig.switch("_eco_idle", True)


def _eco_kept(rig: Rig) -> bool:
    return _state(rig, "_eco_idle") == "on" and rig.coord.eco_idle is True


async def _change_heat_lockout(rig: Rig) -> None:
    await rig.switch("_heat_lockout", True)


def _heat_lockout_kept(rig: Rig) -> bool:
    return _state(rig, "_heat_lockout") == "on" and rig.coord.heat_lockout is True


async def _change_cool_lockout(rig: Rig) -> None:
    await rig.switch("_cool_lockout", True)


def _cool_lockout_kept(rig: Rig) -> bool:
    return _state(rig, "_cool_lockout") == "on" and rig.coord.cool_lockout is True


async def _change_fan(rig: Rig) -> None:
    await rig.person_fan("high", 0)
    assert rig.fan_held(0)


def _fan_kept(rig: Rig) -> bool:
    return rig.fan_held(0) and rig.fan_auto_switch(0) == "off" and rig.heads[0].fan == "high"


async def _change_range(rig: Rig) -> None:
    await _number(rig, "_primary_target_low", 66)


def _range_kept(rig: Rig) -> bool:
    return rig.row(0)["target_low"] == 66 and float(_state(rig, "_primary_target_low")) == 66


CHANGES = {
    "target": (_change_target, _target_kept, None),
    "drift": (_change_drift, _drift_kept, None),
    "enable": (_change_enable, _enable_kept, None),
    "eco": (_change_eco, _eco_kept, None),
    "heat_lockout": (_change_heat_lockout, _heat_lockout_kept, None),
    "cool_lockout": (_change_cool_lockout, _cool_lockout_kept, None),
    "fan_hold": (_change_fan, _fan_kept, None),
    "range": (_change_range, _range_kept, {ZONE_DUAL_SETPOINT: True}),
}


@pytest.mark.parametrize("change", CHANGES)
async def test_a_crash_right_after_a_change_keeps_it(
    hass: HomeAssistant, freezer: Any, change: str
) -> None:
    """The change was made after the last restore data dump, and a crash followed at once."""
    make, kept, room = CHANGES[change]
    rig = await open_rig(hass, freezer, first_room=room)
    try:
        await rig.restart()  # restore data now holds everything from before the change
        await rig.later(1)
        await make(rig)
        assert kept(rig)
        await _next_moment(rig)
        await rig.restart(crash=True)
        assert kept(rig)
    finally:
        await rig.close()


async def _fan_auto_off(rig: Rig) -> None:
    """A person holds the speed boost chose, with the Fan auto switch."""
    await _boost(rig)
    assert rig.fan_auto_switch(0) == "on"


async def _fan_auto_on(rig: Rig) -> None:
    """A person hands a held speed back to boost, with the Fan auto switch."""
    await _change_fan(rig)
    assert rig.fan_auto_switch(0) == "off"


def _fan_auto_off_kept(rig: Rig) -> bool:
    return rig.fan_held(0) and rig.fan_auto_switch(0) == "off" and rig.heads[0].fan != "auto"


def _fan_auto_on_kept(rig: Rig) -> bool:
    return not rig.fan_held(0) and rig.fan_auto_switch(0) == "on"


@pytest.mark.parametrize(
    ("before", "on", "kept"),
    [(_fan_auto_off, False, _fan_auto_off_kept), (_fan_auto_on, True, _fan_auto_on_kept)],
    ids=["off_holds", "on_hands_back"],
)
async def test_a_crash_right_after_a_fan_auto_gesture_keeps_it(
    hass: HomeAssistant, freezer: Any, before: Any, on: bool, kept: Any
) -> None:
    """The switch asks for the debounced refresh, and a refresh has just run, so its own waits.

    In a house a head report or a sensor change runs that refresh. Nothing here refreshes
    after the gesture: what the crash keeps is what the gesture itself saved.
    """
    rig = await open_rig(hass, freezer)
    try:
        await rig.restart()  # restore data now holds everything from before the gesture
        await rig.later(1)
        await before(rig)
        await rig.coord.async_request_refresh()
        await rig.hass.async_block_till_done()
        await rig.hass.services.async_call(
            "switch", "turn_on" if on else "turn_off",
            {"entity_id": rig.eid("_primary_fan_auto")}, blocking=True,
        )
        await rig.hass.async_block_till_done()
        # The plan waits for the refresh; the switch and the fan memory show the gesture now.
        assert rig.fan_auto_switch(0) == ("on" if on else "off")
        assert rig.coord.fan.auto_is_on(rig.coord.zones[0].climate_id) is on
        await _due_now(rig)
        await rig.restart(crash=True)
        assert kept(rig)
    finally:
        await rig.close()


async def test_a_crash_before_the_write_lands_loses_only_that_change(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The boundary: a change whose write hasn't run yet is lost at a crash, and only it."""
    rig = await open_rig(hass, freezer)
    try:
        await rig.restart()
        await rig.later(1)
        await _change_drift(rig)
        await _next_moment(rig)
        await _change_target(rig)
        await rig.restart(crash=True)
        assert _drift_kept(rig)
        assert rig.row(0)["target"] == 70
    finally:
        await rig.close()


def _disable(rig: Rig, suffix: str) -> None:
    er.async_get(rig.hass).async_update_entity(
        rig.eid(suffix), disabled_by=er.RegistryEntryDisabler.USER
    )


def _not_live(rig: Rig, suffix: str) -> bool:
    """Home Assistant runs no entity for the helper.

    Unloading the entry leaves the registry's placeholder (unavailable, ``restored``) for
    each entity, and a disabled entity is never added again to replace it. Through 2026.10
    the placeholder stays; from 2026.11 the registry removes it when the entity is
    disabled (helpers/entity_registry.py ``sync_restored_state``), so there is no state.
    """
    state = rig.hass.states.get(rig.eid(suffix))
    return state is None or (
        state.state == STATE_UNAVAILABLE and state.attributes.get(ATTR_RESTORED) is True
    )


async def _no_change(rig: Rig) -> None:
    pass


async def _boost(rig: Rig) -> None:
    await rig.temp(0, 76)
    assert rig.heads[0].fan != "auto"
    assert not rig.fan_held(0)


@pytest.mark.parametrize(
    ("suffix", "change", "kept"),
    [
        ("_primary_target", _change_target, lambda rig: rig.row(0)["target"] == 74),
        ("_secondary_enable", _no_change, lambda rig: rig.row(1)["enabled"] is True),
        ("_primary_fan_auto", _change_fan, lambda rig: rig.fan_held(0) and rig.heads[0].fan == "high"),
        ("_primary_fan_auto", _boost, lambda rig: not rig.fan_held(0)),
        ("_eco_idle", _change_eco, lambda rig: rig.coord.eco_idle is True),
        ("_heat_lockout", _change_heat_lockout, lambda rig: rig.coord.heat_lockout is True),
        ("_cool_lockout", _change_cool_lockout, lambda rig: rig.coord.cool_lockout is True),
    ],
    ids=["target", "enable", "fan_hold", "fan_boost", "eco", "heat_lockout", "cool_lockout"],
)
async def test_a_helper_disabled_in_the_registry_changes_no_control(
    hass: HomeAssistant, freezer: Any, suffix: str, change: Any, kept: Any
) -> None:
    """A disabled entity is never added, so it restores nothing; the room runs on as it was."""
    rig = await open_rig(hass, freezer)
    try:
        await change(rig)
        assert not _not_live(rig, suffix)
        async with stopped(hass, rig.entry):
            _disable(rig, suffix)
        await rig.settle()
        assert _not_live(rig, suffix)
        assert kept(rig)
        assert rig.coord.coordinator_enable is True
    finally:
        await rig.close()


async def test_a_disabled_coordinator_switch_keeps_the_coordinator_on(
    hass: HomeAssistant, freezer: Any
) -> None:
    rig = await open_rig(hass, freezer)
    try:
        async with stopped(hass, rig.entry):
            _disable(rig, "_coordinator_enable")
        await rig.settle()
        assert rig.coord.coordinator_enable is True
        assert rig.row(0)["enabled"] is True
    finally:
        await rig.close()


async def test_a_hands_off_room_released_after_the_last_dump_stays_released_after_a_crash(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Released: the room's off landed, so MXZ leaves its head to its person from then on.

    The off lands after the last restore data dump. After the crash the person's own
    change at the head must stand: a room that forgot its release would park it again.
    """
    rig = await open_rig(hass, freezer, first_room={ZONE_HANDS_OFF: True})
    try:
        await rig.later(1)
        rig.heads[0].lags_commands = True
        await rig.switch("_primary_enable", False)
        assert rig.heads[0].unreported_commands  # the off is sent and has not landed
        assert rig.heads[0].mode != "off"
        await rig.restart()
        await rig.later(1)
        while await rig.report_next_command(0):
            pass
        rig.heads[0].lags_commands = False
        await rig.settle()
        assert rig.heads[0].mode == "off"
        await rig.person_mode("heat", 0)
        await rig.later(LATER)
        assert rig.heads[0].mode == "heat"
        writes = len(rig.heads[0].mode_writes)
        await rig.restart(crash=True)
        await rig.later(LATER)
        assert rig.heads[0].mode == "heat"
        assert rig.heads[0].mode_writes[writes:] == []
    finally:
        await rig.close()


async def test_a_disabled_range_number_keeps_the_rooms_whole_range(
    hass: HomeAssistant, freezer: Any
) -> None:
    """With the store, a disabled Cool to number leaves the room's own range, not a seeded one.

    Without a store (an install from before it) the pair is seeded, never half restored:
    tests/test_dual_setpoint.py.
    """
    rig = await open_rig(hass, freezer, first_room={ZONE_DUAL_SETPOINT: True})
    try:
        await _number(rig, "_primary_target_low", 64)
        await _number(rig, "_primary_target_high", 76)
        _disable(rig, "_primary_target_high")
        await rig.reload()
        assert (rig.row(0)["target_low"], rig.row(0)["target_high"]) == (64, 76)
    finally:
        await rig.close()


async def test_an_older_dump_cant_make_a_later_boost_speed_a_hold(
    hass: HomeAssistant, freezer: Any
) -> None:
    """F4 at a crash: restore data holds an older speed, the store the one boost wrote since.

    Without a store, the later speed can't be proven MXZ's and is held, conservatively
    (tests/test_startup_continuity.py, test_older_store_cannot_prove_a_later_rung_is_owned).
    """
    rig = await open_rig(hass, freezer)
    try:
        await rig.temp(0, 73)
        first = rig.heads[0].fan
        await rig.restart()  # restore data holds the speed boost wrote at 73
        await rig.later(1)
        await rig.temp(0, 77)
        assert rig.heads[0].fan not in ("auto", first)
        assert not rig.fan_held(0)
        await _next_moment(rig)
        await rig.restart(crash=True)
        assert not rig.fan_held(0)
        await rig.temp(0, 70)
        assert rig.heads[0].fan == "auto"  # boost still drives it
    finally:
        await rig.close()
