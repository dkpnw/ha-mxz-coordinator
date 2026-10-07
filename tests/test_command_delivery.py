"""Actual HA handler barriers: peer progress, ordered current intent and retirement.

All heads, readings and entries are invented. Poll only real handler events;
never wrap fixture clock advancement in an asyncio timeout or touch a debouncer.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta

import pytest
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)
from tests.test_drive import SENSOR_A, SENSOR_B, MockHead, _eid, _setup_mock_heads
from tests.test_idle_action import _settle_requested_refreshes

STANDBY = "binary_sensor.delivery_standby"


async def until(predicate, seconds=3.0):
    """Bound a real-loop observation without installing a swept timeout handle."""
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert predicate(), "required actual handler/publication event did not arrive"


class DeliveryRig:
    def __init__(self, hass):
        self.hass = hass
        self.nonce = uuid.uuid4().hex
        self.events = []
        self.heads = {}
        self.active = {"a": 0, "b": 0}
        self.peak = {"a": 0, "b": 0}
        self.gates = []
        self.tasks = []
        self.rule = None
        self.entry = None
        self.coord = None

    def emit(self, kind, **data):
        event = dict(kind=kind, nonce=self.nonce, monotonic=time.monotonic(),
                     utc=dt_util.utcnow().isoformat(), **data)
        self.events.append(event)
        print("DELIVERY " + json.dumps(event, default=str, sort_keys=True))
        return event

    def arm(self, method="temperature", behavior="hold", after=False):
        gate = {
            "method": method, "behavior": behavior, "after": after, "barrier": uuid.uuid4().hex,
            "entered": asyncio.Event(), "release": asyncio.Event(), "cancelled": asyncio.Event(),
        }
        self.rule = gate
        self.gates.append(gate)
        self.emit("armed", barrier=gate["barrier"], method=method, behavior=behavior)
        return gate

    async def handler(self, head, method, payload, original):
        side = head.side
        gate = self.rule if side == "a" and self.rule and self.rule["method"] == method else None
        if gate:
            self.rule = None
            gate["task"] = asyncio.current_task()
        self.active[side] += 1
        self.peak[side] = max(self.peak[side], self.active[side])
        self.emit("enter", side=side, method=method, payload=payload,
                  barrier=gate["barrier"] if gate else None)
        try:
            if gate:
                if gate["after"]:
                    await original()
                gate["entered"].set()
                if gate["behavior"] == "reject":
                    raise HomeAssistantError("invented definite rejection")
                if gate["behavior"] == "delay":
                    await asyncio.sleep(0.25)
                else:
                    while not gate["release"].is_set():
                        try:
                            await gate["release"].wait()
                        except asyncio.CancelledError:
                            gate["cancelled"].set()
                            self.emit("cancel_received", barrier=gate["barrier"])
                            if gate["behavior"] != "resist":
                                raise
            if not gate or not gate["after"]:
                await original()
            self.emit("complete", side=side, method=method, payload=payload,
                      barrier=gate["barrier"] if gate else None)
        finally:
            self.active[side] -= 1
            self.emit("exit", side=side, method=method)

    def sensor(self, a=76, b=76):
        for entity, value in ((SENSOR_A, a), (SENSOR_B, b)):
            self.hass.states.async_set(entity, str(value),
                {ATTR_UNIT_OF_MEASUREMENT: self.hass.config.units.temperature_unit})

    def tick(self):
        self.emit("clock", seconds=11)
        async_fire_time_changed(self.hass, dt_util.utcnow() + timedelta(seconds=11))

    async def settle(self):
        await self.hass.async_block_till_done()
        await _settle_requested_refreshes(self.hass, self.coord)

    async def service(self, domain, service, suffix, **data):
        await self.hass.services.async_call(domain, service,
            {"entity_id": _eid(self.hass, self.entry, suffix), **data}, blocking=True)

    def start(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    def zone(self, i=0):
        state = self.hass.states.get(_eid(self.hass, self.entry, "_plan"))
        return state.attributes["zones"][i]

    def calls(self, side, kind="complete", method="temperature", high=None):
        return [e for e in self.events if e["kind"] == kind and e.get("side") == side
                and e.get("method") == method
                and (high is None or e["payload"].get("target_temp_high") == high)]

    def release(self, gate):
        self.emit("release", barrier=gate["barrier"])
        gate["release"].set()


@pytest.fixture
async def delivery(hass, request, monkeypatch):
    r = DeliveryRig(hass)
    options = dict(getattr(request, "param", {}))
    start_enabled = options.pop("start_enabled", True)
    deferred_states = options.pop("deferred_states", False)
    if deferred_states:
        from tests.test_idle_action import _force_state_dispatch

        _force_state_dispatch(hass, monkeypatch, deferred=True)

    class TracedHead(MockHead):
        def __init__(self, side):
            super().__init__(side)
            self.side = side
            r.heads[side] = self

        async def async_set_temperature(self, **kwargs):
            await r.handler(self, "temperature", kwargs,
                lambda: super(TracedHead, self).async_set_temperature(**kwargs))

        async def async_set_hvac_mode(self, hvac_mode):
            await r.handler(self, "mode", {"hvac_mode": hvac_mode},
                lambda: super(TracedHead, self).async_set_hvac_mode(hvac_mode))

        async def async_set_fan_mode(self, fan_mode):
            await r.handler(self, "fan", {"fan_mode": fan_mode},
                lambda: super(TracedHead, self).async_set_fan_mode(fan_mode))

    try:
        hass.config.units = US_CUSTOMARY_SYSTEM
        a, b = await _setup_mock_heads(hass, cls=TracedHead)
        r.sensor(70, 70)
        hass.states.async_set(STANDBY, "off")
        r.entry = MockConfigEntry(domain=DOMAIN, title="Invented delivery rooms", data={
            CONF_PRIMARY_CLIMATE: a, CONF_SECONDARY_CLIMATE: b,
            CONF_PRIMARY_SENSOR: SENSOR_A, CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_MODE_HYSTERESIS: 0, CONF_FAN_BOOST_ENABLE: False,
            CONF_INHIBIT_ENTITY: STANDBY, CONF_INHIBIT_ACTION: "off", **options,
        })
        r.entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(r.entry.entry_id)
        r.coord = r.entry.runtime_data
        if start_enabled:
            for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
                await r.service("switch", "turn_on", suffix)
        await r.settle()
        r.events.clear()
        yield r
    finally:
        for gate in r.gates:
            r.release(gate)
        if r.coord is not None:
            await r.settle()
            if not r.coord._retired:
                assert await hass.config_entries.async_unload(r.entry.entry_id)
        for task in r.tasks:
            await task
        await hass.async_block_till_done()
        await until(lambda: not any(r.active.values()))
        r.emit("cleanup", active=r.active, peak=r.peak)
        assert not any(r.active.values())
        await until(lambda: not r.coord._deliveries)
        assert not r.coord._deliveries


@pytest.mark.parametrize("park", [False, True])
async def test_held_head_does_not_block_peer(delivery, park):
    r = delivery
    if park:
        r.sensor()
        await r.coord.async_refresh()
        await r.settle()
        r.events.clear()
    gate = r.arm("mode" if park else "temperature")
    if park:
        r.hass.states.async_set(STANDBY, "on")
    else:
        r.sensor()
    r.tick()
    await until(gate["entered"].is_set)
    method = "mode" if park else "temperature"
    start = time.monotonic()
    await until(lambda: bool(r.calls("b", method=method)), 1.0)
    done = r.calls("b", method=method)
    assert done[0]["payload"] == ({"hvac_mode": "off"} if park else
        {"entity_id": [r.heads["b"].entity_id], "hvac_mode": "cool",
         "target_temp_low": 68.0, "target_temp_high": 70.0})
    for beat in range(20):
        await asyncio.sleep(0.05)
        r.emit("heartbeat", beat=beat, barrier=gate["barrier"])
        assert r.active["a"] == 1 and not gate["release"].is_set()
    entered_a = next(e for e in r.events if e["kind"] == "enter" and e.get("barrier") == gate["barrier"])
    entered_b = r.calls("b", kind="enter", method=method)[0]
    assert entered_b["monotonic"] >= entered_a["monotonic"]
    assert 0 <= done[0]["monotonic"] - entered_a["monotonic"] < 1.0
    assert done[0]["monotonic"] - start < 1.0
    assert len(r.calls("b", kind="enter", method=method)) == 1
    assert "pending since" in json.dumps(r.zone()["control_reasons"])
    if park:
        assert not r.coord._heal_timers
    r.release(gate)
    await r.settle()
    assert r.peak == {"a": 1, "b": 1}


@pytest.mark.parametrize("delivery", [{CONF_FAN_BOOST_ENABLE: False}, {CONF_FAN_BOOST_ENABLE: True}], indirect=True)
@pytest.mark.parametrize("long_hold", [False, True])
async def test_latest_target_without_echo_dependency(delivery, long_hold):
    r = delivery
    gate = r.arm()
    r.sensor()
    r.tick()
    await until(gate["entered"].is_set)
    await until(lambda: bool(r.calls("b")), 1.0)
    for target in (68, 69):
        task = r.start(r.service("number", "set_value", "_primary_target", value=target))
        await until(task.done)
        await task
    if long_hold:
        r.tick()
        await asyncio.sleep(0.1)
    await until(lambda: r.zone()["command_deferred"])
    assert r.zone()["target"] == 69
    assert "deferred" in json.dumps(r.zone()["control_reasons"])
    assert not r.calls("a", kind="enter", high=68)
    assert not r.calls("a", kind="enter", high=69)
    r.release(gate)
    # No tick/echo needed to deliver after completion.
    await until(lambda: bool(r.calls("a", high=69)), 1.0)
    await r.settle()
    assert len(r.calls("a", high=69)) == 1
    assert not r.calls("a", high=68)
    assert r.calls("a", high=69)[0]["monotonic"] > r.calls("a", high=70)[0]["monotonic"]
    assert r.peak == {"a": 1, "b": 1}
    if not r.coord.fan_boost_enable:
        assert not r.calls("a", kind="enter", method="fan")
        assert not r.calls("b", kind="enter", method="fan")


async def test_later_healthy_input_while_other_head_remains_held(delivery):
    r = delivery
    gate = r.arm()
    r.sensor()
    r.tick()
    await until(gate["entered"].is_set)
    await until(lambda: bool(r.calls("b")), 1.0)
    await r.service("number", "set_value", "_secondary_target", value=69)
    await until(lambda: bool(r.calls("b", high=69)), 1.0)
    assert r.active["a"] == 1
    assert len(r.calls("b", high=69)) == 1
    assert not r.calls("a")


async def test_quarter_second_delay_is_ordinary_completion(delivery):
    r = delivery
    gate = r.arm(behavior="delay")
    r.sensor()
    r.tick()
    await until(gate["entered"].is_set)
    await asyncio.sleep(0.05)
    assert r.zone()["command_status"] == "pending"
    assert not r.zone()["command_deferred"]
    await until(lambda: bool(r.calls("a")), 1.0)
    await r.settle()
    entered = r.calls("a", kind="enter")[0]
    elapsed = r.calls("a")[0]["monotonic"] - entered["monotonic"]
    assert 0.25 <= elapsed < 1.0
    assert len(r.calls("a", kind="enter")) == 1
    assert r.zone()["command_status"] == "returned"


@pytest.mark.parametrize("retire", ["disable", "unload", "reload"])
async def test_resistant_return_retirement_and_new_incarnation(delivery, retire):
    r = delivery
    old = r.coord
    gate = r.arm(behavior="resist")
    r.sensor()
    r.tick()
    await until(gate["entered"].is_set)
    gate["task"].cancel()
    await until(gate["cancelled"].is_set)
    await r.service("number", "set_value", "_primary_target", value=69)
    if retire == "disable":
        await r.service("switch", "turn_off", "_coordinator_enable")
        assert r.zone()["command_retired_at"] != "not yet recorded"
    else:
        unload = r.start(r.hass.config_entries.async_unload(r.entry.entry_id))
        await until(unload.done, 1.0)
        assert await unload
        assert old._retired
        if retire == "reload":
            setup = r.start(r.hass.config_entries.async_setup(r.entry.entry_id))
            await until(setup.done, 1.0)
            assert await setup
            r.coord = r.entry.runtime_data
            for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
                await r.service("switch", "turn_on", suffix)
            await r.service("number", "set_value", "_primary_target", value=67)
    assert r.active["a"] == 1
    assert len(r.calls("a", kind="enter")) == 1
    assert old.command_attributes(r.heads["a"].entity_id)["command_retired_at"] != "not yet recorded"
    before = len(r.events)
    r.release(gate)
    await until(lambda: bool(r.calls("a", high=70)))
    if retire == "reload":
        await until(lambda: bool(r.calls("a", high=67)), 1.0)
        assert not r.calls("a", high=69)
    else:
        await r.settle()
        assert not [e for e in r.events[before:] if e["kind"] == "enter"]
    assert r.peak["a"] == 1


@pytest.mark.parametrize("delivery", [
    {CONF_IDLE_ACTION: "off"},
    {CONF_IDLE_ACTION: "off", "deferred_states": True},
], indirect=True)
async def test_pending_park_keeps_its_applicable_action_during_new_plan(delivery, monkeypatch):
    r = delivery
    r.sensor()
    await r.coord.async_refresh()
    await r.settle()
    gate = r.arm(method="mode")
    r.sensor(70, 76)
    r.tick()
    await until(gate["entered"].is_set)
    assert r.hass.states.get(r.heads["a"].entity_id).state == "cool"
    await r.service("number", "set_value", "_primary_target", value=68)
    await until(lambda: r.zone()["command_deferred"])
    assert r.zone()["engage"] == "cool"
    assert r.zone()["target"] == 68
    assert r.coord._planned_act_for(r.heads["a"].entity_id) == "off"
    arms = []
    original = r.coord._arm_or_cancel

    def observe(entity, kind, condition, delay):
        if entity == r.heads["a"].entity_id and kind == "off":
            arms.append(condition)
        return original(entity, kind, condition, delay)

    monkeypatch.setattr(r.coord, "_arm_or_cancel", observe)
    # The actual older handler now writes OFF against a newer cooling plan.
    r.release(gate)
    await until(lambda: bool(r.calls("a", high=68)), 1.0)
    await r.settle()
    assert arms and not any(arms), "our old OFF report must never arm wall-remote healing"
    assert not any(kind == "off" for _, kind in r.coord._heal_timers)
    assert r.peak == {"a": 1, "b": 1}


@pytest.mark.parametrize("delivery", [{CONF_FAN_BOOST_ENABLE: True}], indirect=True)
async def test_manual_fan_intent_waits_for_old_handler_without_obsolete_boost(delivery):
    r = delivery
    gate = r.arm()
    r.sensor()
    r.tick()
    await until(gate["entered"].is_set)
    manual = r.start(r.service("climate", "set_fan_mode", "_primary_thermostat", fan_mode="low"))
    await asyncio.sleep(0.03)
    assert not manual.done()
    assert not r.calls("a", kind="enter", method="fan")
    r.release(gate)
    await until(manual.done)
    await manual
    await r.settle()
    assert r.hass.states.get(r.heads["a"].entity_id).attributes["fan_mode"] == "low"
    assert r.hass.states.get(_eid(r.hass, r.entry, "_primary_fan_auto")).state == "off"
    assert [e["payload"]["fan_mode"] for e in r.calls("a", kind="enter", method="fan")] == ["low"]
    assert r.peak["a"] == 1


async def test_later_standby_release_progresses_healthy_peer(delivery):
    r = delivery
    r.sensor()
    await r.coord.async_refresh()
    await r.settle()
    r.events.clear()
    gate = r.arm(method="mode")
    r.hass.states.async_set(STANDBY, "on")
    r.tick()
    await until(gate["entered"].is_set)
    await until(lambda: bool(r.calls("b", method="mode")), 1.0)
    r.hass.states.async_set(STANDBY, "off")
    await until(lambda: bool(r.calls("b")), 1.0)
    assert r.calls("b")[0]["payload"]["target_temp_high"] == 70
    assert r.active["a"] == 1
    r.release(gate)
    await until(lambda: bool(r.calls("a")), 1.0)
    assert not any(kind == "off" for _, kind in r.coord._heal_timers)
    assert r.peak == {"a": 1, "b": 1}


async def test_disable_then_enable_does_not_revive_old_followons(delivery):
    r = delivery
    gate = r.arm(behavior="resist")
    r.sensor()
    r.tick()
    await until(gate["entered"].is_set)
    await r.service("switch", "turn_off", "_coordinator_enable")
    await r.service("number", "set_value", "_primary_target", value=69)
    await r.service("switch", "turn_on", "_coordinator_enable")
    await until(lambda: r.zone()["command_deferred"])
    assert r.active["a"] == 1
    assert not r.calls("a", kind="enter", high=69)
    r.release(gate)
    await until(lambda: bool(r.calls("a", high=69)), 1.0)
    assert len(r.calls("a", high=69)) == 1
    assert r.peak["a"] == 1


@pytest.mark.parametrize("cleanup_returns_first", [True, False])
async def test_resistant_vane_retirement_preserves_guard_and_ordinary_order(
    hass, monkeypatch, request, cleanup_returns_first,
):
    """Legacy own-wake parking is separate; new work waits for both handlers."""
    from tests.test_vane_kick import _setup_kickable, _start_blocked_kick

    entry, heads, vane = await _setup_kickable(hass)
    coord = entry.runtime_data
    cid = heads[0].entity_id
    harness = await _start_blocked_kick(hass, entry, heads[0], vane, "wake", monkeypatch, request)
    coord._vane_kick_retire = 0.05
    before = coord.command_attributes(cid)
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    original_mode = heads[0].async_set_hvac_mode
    original_fan = heads[0].async_set_fan_mode
    next_calls = []

    async def mode(value):
        await original_mode(value)
        if value == "off":
            cleanup_entered.set()
            await cleanup_release.wait()

    async def fan(value):
        next_calls.append(value)
        await original_fan(value)

    monkeypatch.setattr(heads[0], "async_set_hvac_mode", mode)
    monkeypatch.setattr(heads[0], "async_set_fan_mode", fan)
    unload = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
    successor = None
    next_command = None
    try:
        await until(cleanup_entered.is_set, 1.0)
        assert coord._retired
        assert not harness.kick_task.done()
        assert harness.commands == [("head", "fan_only"), ("head", "off")]
        assert hass.states.get(cid).state == "off"
        observed = coord.command_attributes(cid)
        assert observed["command_status"] == "pending"
        assert observed["command_attempted_at"] == before["command_attempted_at"]
        assert observed["command_returned_at"] == before["command_returned_at"]
        assert observed["command_retired_at"] != "not yet recorded"
        assert observed["command_ownership_retired"] is True
        assert observed["vane_retirement_cleanup"]["command_status"] == "pending"
        assert "physically stopped" in json.dumps(coord.room_details())
        # A fresh coordinator uses the same real per-head ordering, without
        # depending on HA entry setup waiting for unload's cleanup to finish.
        successor = type(coord)(hass, entry)
        assert successor.command_attributes(cid) == observed
        next_command = asyncio.create_task(successor.async_head_service(
            cid, "set_fan_mode", {"fan_mode": "low"}, manual=True,
        ))
        await asyncio.sleep(0.03)
        assert not next_command.done() and next_calls == []
        if cleanup_returns_first:
            cleanup_release.set()
            await until(unload.done, 1.0)
            assert await unload
            assert not harness.kick_task.done()
            observed = coord.command_attributes(cid)
            assert observed["command_status"] == "pending"
            assert observed["command_returned_at"] == before["command_returned_at"]
            assert observed["vane_retirement_cleanup"]["command_status"] == "returned"
            assert coord._head_locks[cid].locked()
        else:
            harness.barrier.release.set()
            await until(harness.kick_task.done, 1.0)
            await harness.kick_task
            assert not next_command.done() and next_calls == []
            observed = coord.command_attributes(cid)
            assert observed["command_status"] == "returned"
            assert observed["vane_retirement_cleanup"]["command_status"] == "pending"
            assert not unload.done()
        await asyncio.sleep(0.03)
        assert not next_command.done() and next_calls == []
        cleanup_release.set()
        harness.barrier.release.set()
        await until(next_command.done, 1.0)
        await next_command
        assert next_calls == ["low"]
        assert successor.command_attributes(cid)["command_ownership_retired"] is False
        assert successor.command_attributes(cid)["command_retired_at"] == observed["command_retired_at"]
    finally:
        cleanup_release.set()
        harness.barrier.release.set()
        await until(harness.kick_task.done)
        await harness.kick_task
        await unload
        if next_command is not None:
            await next_command
        if successor is not None:
            await successor.async_shutdown_listeners()
        await hass.async_block_till_done()
    assert harness.commands == [("head", "fan_only"), ("head", "off")]
    assert not coord._vane_kicks and not coord._vane_kick_woken
    assert not coord._vane_cleanup_pending
    assert not coord._head_locks[cid].locked()


async def test_queued_vane_has_no_own_wake_retirement_permission(hass, monkeypatch):
    """Reported fan_only plus a queued kick is not an issued own wake."""
    from tests.test_vane_kick import _setup_kickable

    entry, heads, vane = await _setup_kickable(hass)
    coord = entry.runtime_data
    cid = heads[0].entity_id
    lock = coord._head_locks.setdefault(cid, asyncio.Lock())
    await lock.acquire()
    commands = []
    original = heads[0].async_set_hvac_mode

    async def mode(value):
        commands.append(value)
        await original(value)

    monkeypatch.setattr(heads[0], "async_set_hvac_mode", mode)
    try:
        await coord.async_apply_vane(cid, vane.entity_id, "SWING")
        kick = coord._vane_kicks[cid]
        await asyncio.sleep(0.03)
        assert not kick.done()
        assert cid not in coord._vane_kick_woken
        # A later device report cannot turn an unissued wake into ownership.
        await original("fan_only")
        assert hass.states.get(cid).state == "fan_only"
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert kick.done()
        assert commands == []
        assert hass.states.get(cid).state == "fan_only"
        assert coord.command_attributes(cid)["vane_retirement_cleanup"] == {}
    finally:
        lock.release()
        if not coord._retired:
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize("cleanup_returns_first", [True, False])
@pytest.mark.parametrize("latest", ["idle", "cool"])
async def test_deferred_delivery_releases_creating_service_and_transaction(
    hass, monkeypatch, request, cleanup_returns_first, latest,
):
    """Enable returns while unissued work waits; both real returns are required."""
    from tests.test_vane_kick import _setup_kickable, _start_blocked_kick

    entry, heads, vane = await _setup_kickable(hass)
    coord = entry.runtime_data
    cid = heads[0].entity_id
    room_id = _eid(hass, entry, "_primary_thermostat")
    harness = await _start_blocked_kick(hass, entry, heads[0], vane, "wake", monkeypatch, request)
    coord._vane_kick_retire = 0.05
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    original_mode = heads[0].async_set_hvac_mode
    calls = []
    peer_calls = []
    original_peer = heads[1].async_set_hvac_mode
    temperatures = []
    original_temperature = heads[0].async_set_temperature

    async def mode(value):
        calls.append(value)
        await original_mode(value)
        if value == "off":
            cleanup_entered.set()
            await cleanup_release.wait()

    async def peer(value):
        peer_calls.append(value)
        await original_peer(value)

    async def temperature(**kwargs):
        temperatures.append(kwargs)
        await original_temperature(**kwargs)

    monkeypatch.setattr(heads[0], "async_set_hvac_mode", mode)
    monkeypatch.setattr(heads[1], "async_set_hvac_mode", peer)
    monkeypatch.setattr(heads[0], "async_set_temperature", temperature)

    async def switch(service, suffix):
        await hass.services.async_call("switch", service, {
            "entity_id": _eid(hass, entry, suffix),
        }, blocking=True)

    disable = asyncio.create_task(switch("turn_off", "_coordinator_enable"))
    enable = None
    try:
        await until(cleanup_entered.is_set, 1.0)
        assert harness.barrier.cancel_requested.is_set()
        if cleanup_returns_first:
            cleanup_release.set()
            await until(disable.done, 1.0)
            await disable
            assert not harness.kick_task.done()
        else:
            harness.barrier.release.set()
            await until(harness.kick_task.done, 1.0)
            await harness.kick_task
            assert not disable.done()
        await switch("turn_off", "_eco_idle")
        # Expire only the pre-existing request cooldown while disabled. The
        # enable below is the sole new input and must create a completed plan.
        for _ in range(5):
            generation = coord._refresh_generation
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
            await asyncio.sleep(0.03)
            assert not coord.coordinator_enable and not coord._plan_lock.locked()
            if coord._refresh_generation == generation:
                break
        else:
            pytest.fail("disabled preparation never settled")
        generation = coord._refresh_generation
        enable = asyncio.create_task(switch("turn_on", "_coordinator_enable"))
        await until(lambda: cid in coord._deliveries or calls != ["off"], 1.0)
        assert calls == ["off"], "ordinary work must wait for the actual wake and cleanup returns"
        await until(enable.done, 1.0)
        await enable
        await until(lambda: coord._refresh_generation > generation and not coord._plan_lock.locked(), 1.0)
        await until(lambda: peer_calls == ["fan_only"], 1.0)
        observed = hass.states.get(room_id).attributes
        assert observed["command_deferred"] is True
        assert observed["command_ownership_retired"] is True
        assert observed["command_status"] == ("pending" if cleanup_returns_first else "returned")
        assert observed["vane_retirement_cleanup"]["command_status"] == (
            "returned" if cleanup_returns_first else "pending"
        )
        assert "Own-wake vane retirement" in json.dumps(observed["control_reasons"])
        assert "deferred" in json.dumps(observed["control_reasons"])
        assert calls == ["off"]
        # Latest intent replaces the unissued fan_only: no obsolete wake after
        # the real handlers return and no new refresh input at release.
        if latest == "idle":
            await switch("turn_on", "_eco_idle")
            await until(lambda: heads[1].hvac_mode == "off", 1.0)
        else:
            for target in (67, 66):
                await hass.services.async_call("number", "set_value", {
                    "entity_id": _eid(hass, entry, "_primary_target"), "value": target,
                }, blocking=True)
            await until(lambda: hass.states.get(room_id).attributes["temperature"] == 66, 1.0)
            assert temperatures == []
        cleanup_release.set()
        harness.barrier.release.set()
        await until(harness.kick_task.done, 1.0)
        await harness.kick_task
        await until(lambda: not coord._deliveries, 1.0)
        await hass.async_block_till_done()
        assert calls == ["off"]
        if latest == "idle":
            assert peer_calls == ["fan_only", "off"]
            assert temperatures == []
        else:
            assert peer_calls == ["fan_only"]
            assert len(temperatures) == 1
            assert temperatures[0]["target_temp_high"] == 66
        assert hass.states.get(room_id).attributes["command_deferred"] is False
    finally:
        cleanup_release.set()
        harness.barrier.release.set()
        await until(harness.kick_task.done)
        await harness.kick_task
        await disable
        if enable is not None:
            await enable
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert not coord._deliveries and not coord._vane_cleanup_pending
