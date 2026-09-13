"""Idle action option (v3.3.0): satisfied/standoff heads park off instead of
fan_only when idle_action="off".

Covers the four interaction points the option touches: the transition-edge
fan-auto handback (so an off head never rests on a boost ladder token), the
plan-aware off-drift self-heal, the vane kick on an idle-off head, and the
restart seeds. Default-config behavior is pinned unchanged by the existing
suite; this file only exercises the non-default values.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
    IDLE_ACTION_OFF,
    OFF_WHILE_ENABLED_DELAY,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from tests.test_drive import (
    EVENT_CALL_SERVICE,
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_temp,
    _setup_mock_heads,
    _user_set_fan,
)
from tests.test_fan_hold_restore import _restart


async def _setup_idle(
    hass: HomeAssistant, idle_action: str = IDLE_ACTION_OFF, **extra: Any
) -> tuple[MockConfigEntry, str, str]:
    """Heads + an idle_action entry; coordinator and both rooms enabled."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: True,
            CONF_IDLE_ACTION: idle_action,
            CONF_MODE_HYSTERESIS: 0,
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    return entry, head_a, head_b


def _record_calls(hass: HomeAssistant) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    hass.bus.async_listen(
        EVENT_CALL_SERVICE, callback(lambda e: calls.append(dict(e.data)))
    )
    return calls


def _head_calls(calls: list[dict[str, Any]], head: str) -> list[tuple[str, str]]:
    """(service, extra) tuples of climate calls addressed to one head."""
    out = []
    for c in calls:
        if c["domain"] != "climate":
            continue
        data = c["service_data"]
        if data.get("entity_id") != head:
            continue
        extra = data.get("fan_mode") or data.get("hvac_mode") or ""
        out.append((c["service"], extra))
    return out


async def _settle_requested_refreshes(
    hass: HomeAssistant, coordinator: MXZCoordinator
) -> int:
    """Run the refreshes setup already requested, and return how many ran.

    `async_request_refresh` is debounced, so entry setup and the enable
    switches leave a trailing refresh armed behind a cooldown timer. Expire
    that cooldown for real until a whole cooldown passes with no refresh: a
    test that then measures a timer window starts from an idle coordinator
    instead of racing its own setup burst.
    """
    refreshes = 0

    @callback
    def count_refresh() -> None:
        nonlocal refreshes
        refreshes += 1

    unsub = coordinator.async_add_listener(count_refresh)
    try:
        for _ in range(5):
            ran = refreshes
            async_fire_time_changed(
                hass,
                dt_util.utcnow()
                + timedelta(seconds=REQUEST_REFRESH_DEFAULT_COOLDOWN + 1),
            )
            await hass.async_block_till_done()
            if refreshes == ran:
                return refreshes
        raise AssertionError("coordinator kept requesting refreshes")
    finally:
        unsub()


def _force_state_dispatch(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, *, deferred: bool
) -> None:
    """Select actual inline/next-loop HA state callback delivery for a control."""
    from homeassistant.helpers import event as event_helper

    inline = event_helper._async_dispatch_entity_id_event

    @callback
    def next_loop(hass, callbacks, event):
        hass.loop.call_soon(inline, hass, callbacks, event)

    tracker = event_helper._KEYED_TRACK_STATE_CHANGE
    assert tracker.key not in hass.data  # patch before MXZ registers its listeners
    monkeypatch.setattr(
        event_helper,
        "_KEYED_TRACK_STATE_CHANGE",
        dataclasses.replace(
            tracker, dispatcher_callable=next_loop if deferred else inline
        ),
    )


async def test_idle_off_satisfied_parks_off_after_auto_handback(
    hass: HomeAssistant,
) -> None:
    """Satisfied -> fan handed back to auto FIRST (head still awake), then off."""
    entry, head_a, _b = await _setup_idle(hass)

    # Drive the primary so the boost holds a non-auto ladder token.
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    assert a.state == "cool"
    assert a.attributes["fan_mode"] != "auto"  # boost is driving

    calls = _record_calls(hass)
    await _set_temp(hass, SENSOR_A, 70)  # satisfied
    await _recompute(hass, entry)

    a = hass.states.get(head_a)
    assert a.state == "off"  # parked off, not fan_only
    assert a.attributes["fan_mode"] == "auto"  # no ladder-token residue
    seq = _head_calls(calls, head_a)
    assert ("set_fan_mode", "auto") in seq
    assert ("set_hvac_mode", "off") in seq
    assert seq.index(("set_fan_mode", "auto")) < seq.index(("set_hvac_mode", "off"))


async def test_idle_off_standoff_loser_parks_off(hass: HomeAssistant) -> None:
    """The wrong-direction room parks off; the winner runs untouched."""
    entry, head_a, head_b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)  # wants cool (priority: wins)
    await _set_temp(hass, SENSOR_B, 60)  # wants heat (loser)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    assert hass.states.get(head_b).state == "off"


async def test_idle_off_steady_state_is_idempotent(hass: HomeAssistant) -> None:
    """A second cycle makes no further climate writes to the parked head."""
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"

    calls = _record_calls(hass)
    await _recompute(hass, entry)
    assert _head_calls(calls, head_a) == []


async def test_idle_off_reengages_with_single_set_temperature(
    hass: HomeAssistant,
) -> None:
    """Waking from off needs one set_temperature carrying hvac_mode."""
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"

    calls = _record_calls(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    assert a.state == "cool"
    assert a.attributes["target_temp_high"] == 70
    modes = [s for s, _ in _head_calls(calls, head_a) if s == "set_hvac_mode"]
    assert modes == []  # the mode rode inside set_temperature


async def test_idle_off_latched_hold_gets_no_auto_write(hass: HomeAssistant) -> None:
    """A manual fan hold parks off WITHOUT the auto handback; the hold survives."""
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "medium")  # deliberate departure -> latch

    calls = _record_calls(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)

    a = hass.states.get(head_a)
    assert a.state == "off"
    assert a.attributes["fan_mode"] == "medium"  # the user's pick, untouched
    assert ("set_fan_mode", "auto") not in _head_calls(calls, head_a)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True


async def test_wall_off_during_active_call_still_arms_heal(
    hass: HomeAssistant,
) -> None:
    """The plan wants this head COOLING -> a wall-remote off is still drift."""
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"

    await hass.services.async_call(
        "climate", "set_hvac_mode",
        {"entity_id": head_a, "hvac_mode": "off"}, blocking=True,
    )
    await hass.async_block_till_done()
    coord = entry.runtime_data
    assert any(kind == "off" for (_, kind) in coord._heal_timers)


async def test_plan_parked_off_head_never_arms_heal(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head the PLAN parked off (idle_action) is not drift."""
    changes: list[tuple[str, str | None, str | None]] = []
    original = MXZCoordinator._on_head_change

    @callback
    def record_change(self, event):
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        changes.append(
            (
                event.data["entity_id"],
                old.state if old else None,
                new.state if new else None,
            )
        )
        original(self, event)

    monkeypatch.setattr(MXZCoordinator, "_on_head_change", record_change)
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)  # cool -> off transition observed by the listener
    assert hass.states.get(head_a).state == "off"
    coord = entry.runtime_data
    assert (head_a, "cool", "off") in changes  # actual registered callback ran
    assert not any(kind == "off" for (_, kind) in coord._heal_timers)


@pytest.mark.parametrize("deferred", [False, True], ids=["synchronous", "deferred"])
async def test_plan_parked_off_is_stable_across_callback_delivery(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, deferred: bool
) -> None:
    """Inline and delayed head echoes both use the plan that issued the off."""
    _force_state_dispatch(hass, monkeypatch, deferred=deferred)
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    coord = entry.runtime_data
    assert hass.states.get(head_a).state == "off"
    assert not any(kind == "off" for (_, kind) in coord._heal_timers)

    calls = _record_calls(hass)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=OFF_WHILE_ENABLED_DELAY + 5)
    )
    await hass.async_block_till_done()
    assert hass.states.get(head_a).state == "off"
    assert _head_calls(calls, head_a) == []


async def test_wall_off_timer_callback_reengages_active_head(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistent human off during active demand still executes one real heal."""
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"

    coord = entry.runtime_data
    # Setup's own trailing refresh would otherwise re-engage the head inside
    # the 30 s window and cancel the timer this test measures.
    await _settle_requested_refreshes(hass, coord)
    assert hass.states.get(head_a).state == "cool"

    heals = 0
    original = coord._heal_and_notify

    async def record_heal():
        nonlocal heals
        heals += 1
        await original()

    monkeypatch.setattr(coord, "_heal_and_notify", record_heal)
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": head_a, "hvac_mode": "off"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert any(kind == "off" for (_, kind) in coord._heal_timers)

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=OFF_WHILE_ENABLED_DELAY + 5)
    )
    await hass.async_block_till_done()
    assert heals == 1
    assert hass.states.get(head_a).state == "cool"
    assert not any(kind == "off" for (_, kind) in coord._heal_timers)


async def test_human_correction_cancels_pending_off_heal(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A human returning an active head to plan wins before timer expiry."""
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    coord = entry.runtime_data

    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": head_a, "hvac_mode": "off"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert any(kind == "off" for (_, kind) in coord._heal_timers)

    heals = 0

    async def record_heal():
        nonlocal heals
        heals += 1

    monkeypatch.setattr(coord, "_heal_and_notify", record_heal)
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": head_a, "hvac_mode": "cool"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert not any(kind == "off" for (_, kind) in coord._heal_timers)

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=OFF_WHILE_ENABLED_DELAY + 5)
    )
    await hass.async_block_till_done()
    assert heals == 0
    assert hass.states.get(head_a).state == "cool"


async def test_new_demand_callback_wakes_parked_head_without_heal_timer(
    hass: HomeAssistant,
) -> None:
    """A sensor callback, not a stale heal timer, wakes a newly demanding room."""
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    coord = entry.runtime_data
    assert hass.states.get(head_a).state == "off"
    assert not any(kind == "off" for (_, kind) in coord._heal_timers)

    await _set_temp(hass, SENSOR_A, 75)  # no explicit recompute
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=15))
    await hass.async_block_till_done()
    assert hass.states.get(head_a).state == "cool"
    assert not any(kind == "off" for (_, kind) in coord._heal_timers)


async def test_unload_cancels_pending_off_heal_callback(hass: HomeAssistant) -> None:
    """An unloaded coordinator cannot wake a head from a previously armed timer."""
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    coord = entry.runtime_data
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": head_a, "hvac_mode": "off"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert any(kind == "off" for (_, kind) in coord._heal_timers)

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert coord._heal_timers == {}
    calls = _record_calls(hass)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=OFF_WHILE_ENABLED_DELAY + 5)
    )
    await hass.async_block_till_done()
    assert hass.states.get(head_a).state == "off"
    assert _head_calls(calls, head_a) == []


async def test_default_config_wall_off_still_arms_heal(hass: HomeAssistant) -> None:
    """Regression guard for the plan-aware term: with the DEFAULT idle_action a
    satisfied head's planned act is fan_only, so a wall-remote off still arms."""
    entry, head_a, _b = await _setup_idle(hass, idle_action="fan_only")
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"

    await hass.services.async_call(
        "climate", "set_hvac_mode",
        {"entity_id": head_a, "hvac_mode": "off"}, blocking=True,
    )
    await hass.async_block_till_done()
    coord = entry.runtime_data
    assert any(kind == "off" for (_, kind) in coord._heal_timers)


async def test_vane_change_on_idle_off_head_kicks_and_returns_off(
    hass: HomeAssistant,
) -> None:
    """An idle-off head takes the fan_only vane kick and lands back at off
    (same path an eco-off head takes today)."""
    entry, head_a, _b = await _setup_idle(hass)
    coord = entry.runtime_data
    coord._vane_kick_spinup = 0
    coord._vane_kick_apply = 0

    async def _noop(call: Any) -> None:
        return None

    hass.services.async_register("select", "select_option", _noop)

    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"

    calls = _record_calls(hass)
    await coord.async_apply_vane(head_a, "select.dummy_vane", "SWING")
    await hass.async_block_till_done()
    seq = _head_calls(calls, head_a)
    assert ("set_hvac_mode", "fan_only") in seq  # woken for the kick
    assert head_a not in coord._vane_kicks  # kick cleaned up
    assert hass.states.get(head_a).state == "off"  # and parked again


async def test_facade_reads_idle_while_parked_off(hass: HomeAssistant) -> None:
    """The room thermostat tile shows IDLE (not OFF) for an idle-off room."""
    entry, _a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    tile = hass.states.get(_eid(hass, entry, "_primary_thermostat"))
    assert tile.state == "heat_cool"  # room still enabled
    assert tile.attributes["hvac_action"] == "idle"


async def test_plan_sensor_reports_idle_action(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup_idle(hass)
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["idle_action"] == "off"


# --- restart seeds -------------------------------------------------------------


async def test_restart_after_idle_off_seeds_clean(hass: HomeAssistant) -> None:
    """The pre-off auto handback means a restart finds 'auto' -> no phantom hold."""
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    assert a.state == "off"
    assert a.attributes["fan_mode"] == "auto"

    coord = entry.runtime_data
    _restart(coord, {head_a: False})
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False


async def test_restart_after_idle_off_preserves_real_hold(
    hass: HomeAssistant,
) -> None:
    """A manual hold rides through park-off + restart via the switch's restore."""
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "medium")
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"

    coord = entry.runtime_data
    _restart(coord, {head_a: True})
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"


async def test_restart_residue_token_with_not_held_restore_is_dropped(
    hass: HomeAssistant,
) -> None:
    """An interrupted handback (off head resting on a ladder token) + a fresh
    not-held restore -> residue dropped by the seed carve-out, no phantom hold."""
    entry, head_a, _b = await _setup_idle(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "medium")  # leaves a non-auto token
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"

    coord = entry.runtime_data
    _restart(coord, {head_a: False})  # switch says: was NOT held
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False
