"""Existing HA room surfaces disclose actual lookup and software delivery status."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED
from homeassistant.core import callback
from homeassistant.helpers.entity_component import DATA_INSTANCES

from custom_components.mxz_coordinator.const import (
    CONF_DEMAND_THRESHOLD,
    CONF_FAN_BOOST_ENABLE,
    CONF_FAN_BOOST_MAX,
)
from tests.test_command_delivery import delivery as delivery_fixture
from tests.test_command_delivery import until
from tests.test_drive import SENSOR_A, SENSOR_B, _eid

# Reuse the actual-head fixture without another fixture implementation.
delivery = delivery_fixture


def reasons(r, side=0):
    view = r.zone(side)
    assert all(item["next_step"] for item in view["control_reasons"])
    return json.dumps(view["control_reasons"])


async def test_actual_rejection_then_ordinary_recovery(delivery):
    r = delivery
    gate = r.arm(behavior="reject")
    r.sensor()
    r.tick()
    await until(gate["entered"].is_set)
    await until(lambda: r.zone()["command_status"] == "rejected")
    view = r.zone()
    assert "invented definite rejection" in reasons(r)
    assert "next ordinary update" in reasons(r)
    attempted = datetime.fromisoformat(view["command_attempted_at"])
    failed = datetime.fromisoformat(view["command_failed_at"])
    assert failed >= attempted
    # Exactly one rejected temperature attempt, no timer retry policy.
    assert len(r.calls("a", kind="enter")) == 1
    assert not r.calls("a")
    await until(lambda: bool(r.calls("b")), 1.0)
    await r.coord.async_refresh()
    await r.settle()
    assert len(r.calls("a", kind="enter")) == 2
    assert len(r.calls("a")) == 1
    assert r.zone()["command_status"] == "returned"
    assert "rejected at" not in reasons(r)
    assert datetime.fromisoformat(r.zone()["command_returned_at"]) >= failed


async def test_software_timestamps_are_absent_until_observed_and_immediate(delivery):
    r = delivery
    # A pending attempt must not overwrite the separately observed prior return.
    before = r.zone()["command_returned_at"]
    gate = r.arm()
    r.sensor()
    r.tick()
    await until(gate["entered"].is_set)
    view = r.zone()
    assert view["command_status"] == "pending"
    assert view["command_returned_at"] == before
    assert view["command_failed_at"] == "not yet recorded"
    assert view["command_retired_at"] == "not yet recorded"
    assert "not physical receipt" in view["command_timestamp_basis"]
    assert "pending since" in reasons(r)
    room = r.hass.states.get(_eid(r.hass, r.entry, "_primary_thermostat"))
    assert room.attributes["command_attempted_at"] == view["command_attempted_at"]
    assert room.attributes["command_status"] == "pending"
    r.release(gate)
    await until(lambda: r.zone()["command_status"] == "returned")
    assert datetime.fromisoformat(r.zone()["command_returned_at"]) >= datetime.fromisoformat(view["command_attempted_at"])


@pytest.mark.parametrize("missing", ["head", "sensor"])
async def test_missing_binding_then_recovery_keeps_peer_eligible(delivery, missing):
    r = delivery
    entity = r.heads["a"].entity_id if missing == "head" else SENSOR_A
    prior = r.hass.states.get(entity)
    if missing == "head":
        r.heads["a"]._attr_available = False
        r.heads["a"].async_write_ha_state()
    else:
        r.hass.states.async_remove(entity)
    await r.coord.async_refresh()
    await r.settle()
    expected = f"Head {entity} unavailable." if missing == "head" else f"Room sensor {entity} unavailable or invalid."
    assert expected in reasons(r)
    if missing == "sensor":
        assert r.zone()["temp"] is None
    state_b = r.hass.states.get(SENSOR_B)
    r.hass.states.async_set(SENSOR_B, "76", dict(state_b.attributes))
    await r.coord.async_refresh()
    await r.settle()
    assert expected in reasons(r)
    assert r.hass.states.get(r.heads["b"].entity_id).state == "cool"
    assert r.calls("b")[0]["payload"]["target_temp_high"] == 70
    if missing == "head":
        r.heads["a"]._attr_available = True
        r.heads["a"].async_write_ha_state()
    else:
        r.hass.states.async_set(entity, prior.state, dict(prior.attributes))
    await r.coord.async_refresh()
    await r.settle()
    assert expected not in reasons(r)


@pytest.mark.parametrize(("modes", "available", "message"), [
    (["auto", "low"], True, None),
    (["auto", 7], True, "advertised auto remains available"),
    (["low", 7], False, "no supported auto"),
    ([7], False, "malformed"),
    (None, False, "malformed"),
    (["low"], False, "not supported"),
])
async def test_fan_metadata_uses_existing_capability_contract(delivery, modes, available, message):
    r = delivery
    head = r.heads["a"]
    head._attr_fan_modes = modes
    head.async_write_ha_state()
    await r.coord.async_refresh()
    await r.settle()
    switch = r.hass.states.get(_eid(r.hass, r.entry, "_primary_fan_auto"))
    assert (switch.state != "unavailable") is available
    if message:
        assert message in reasons(r)
    else:
        assert "malformed" not in reasons(r) and "not supported" not in reasons(r)
    peer = r.hass.states.get(_eid(r.hass, r.entry, "_secondary_fan_auto"))
    assert peer.state != "unavailable"
    head._attr_fan_modes = ["auto", "low", "medium", "high"]
    head.async_write_ha_state()
    await r.coord.async_refresh()
    await r.settle()
    assert r.hass.states.get(switch.entity_id).state != "unavailable"
    assert "malformed" not in reasons(r)


async def test_no_fan_feature_then_recovery(delivery):
    r = delivery
    head = r.heads["a"]
    features = head.supported_features
    head._attr_supported_features = features & ~ClimateEntityFeature.FAN_MODE
    head.async_write_ha_state()
    await r.coord.async_refresh()
    assert "not advertised" in reasons(r)
    assert r.hass.states.get(_eid(r.hass, r.entry, "_primary_fan_auto")).state == "unavailable"
    head._attr_supported_features = features
    head.async_write_ha_state()
    await r.coord.async_refresh()
    assert "not advertised" not in reasons(r)


@pytest.mark.parametrize("state_band", [True, False])
async def test_native_object_absence_discloses_actual_fallback_and_recovers(delivery, monkeypatch, state_band):
    r = delivery
    await r.service("switch", "turn_off", "_coordinator_enable")
    head = r.heads["a"]
    component = r.hass.data[DATA_INSTANCES]["climate"]
    original = component.get_entity
    with monkeypatch.context() as m:
        m.setattr(component, "get_entity", lambda eid: None if eid == head.entity_id else original(eid))
        if not state_band:
            state = r.hass.states.get(head.entity_id)
            attrs = dict(state.attributes)
            attrs.pop("min_temp", None)
            attrs.pop("max_temp", None)
            r.hass.states.async_set(head.entity_id, state.state, attrs)
        await r.coord.async_refresh()
        text = reasons(r)
        assert ("reported state band" if state_band else "configured temperature clamps") in text
        if not state_band:
            assert "do not prove hardware acceptance" in text
        assert "Native head limits unavailable" not in reasons(r, 1)
    head.async_write_ha_state()
    await r.coord.async_refresh()
    assert "limits unavailable" not in reasons(r)


@pytest.mark.parametrize("missing", [None, {}])
async def test_real_absent_plan_is_disclosed_on_existing_surfaces(delivery, missing):
    r = delivery
    r.coord.async_set_updated_data(missing)
    await asyncio.sleep(0)
    assert "Current plan unavailable" in reasons(r)
    room = r.hass.states.get(_eid(r.hass, r.entry, "_primary_thermostat"))
    assert "Current plan unavailable" in json.dumps(room.attributes["control_reasons"])
    await r.coord.async_refresh()
    await r.settle()
    assert "Current plan unavailable" not in reasons(r)


@pytest.mark.parametrize("delivery", [
    {},
    {CONF_DEMAND_THRESHOLD: 1.0},
    {CONF_FAN_BOOST_MAX: "invented_invalid"},
], indirect=True)
async def test_settings_absence_mirror_invalid_and_recovery(delivery):
    r = delivery
    conf = r.entry.data
    text = reasons(r)
    if CONF_DEMAND_THRESHOLD in conf:
        assert "data mirror" in text
    if conf.get(CONF_FAN_BOOST_MAX) == "invented_invalid":
        assert "Stored fan settings are invalid" in text
    else:
        assert "Default fan settings used for: maximum fan speed" in text
        assert "lost" not in text.lower()
    r.hass.config_entries.async_update_entry(r.entry, options={
        CONF_FAN_BOOST_ENABLE: False, CONF_FAN_BOOST_MAX: "high",
    })
    await r.hass.async_block_till_done()
    r.coord = r.entry.runtime_data
    await r.settle()
    text = reasons(r)
    assert "Stored fan settings are invalid" not in text
    assert "Default fan settings" not in text
    assert "data mirror" not in text


async def test_lost_settings_and_optional_defaults_do_not_invent_history(delivery):
    r = delivery
    r.hass.config_entries.async_update_entry(r.entry, options={
        CONF_FAN_BOOST_ENABLE: False, CONF_FAN_BOOST_MAX: "medium",
    })
    await r.hass.async_block_till_done()
    r.coord = r.entry.runtime_data
    await r.settle()
    assert "Default fan settings" not in reasons(r)
    data = {key: value for key, value in r.entry.data.items()
            if key not in (CONF_FAN_BOOST_ENABLE, CONF_FAN_BOOST_MAX, CONF_DEMAND_THRESHOLD)}
    r.hass.config_entries.async_update_entry(r.entry, data=data, options={})
    await r.hass.async_block_till_done()
    r.coord = r.entry.runtime_data
    await r.settle()
    text = reasons(r)
    assert "Default fan settings used" in text
    assert "does not establish whether defaults were intended" in text
    assert "lost" not in text.lower() and "fresh" not in text.lower()
    assert r.coord.fan_boost_enable is True
    assert r.coord.fan_boost_max == "high"


@pytest.mark.parametrize("delivery", [{CONF_FAN_BOOST_ENABLE: True}], indirect=True)
async def test_missing_speed_retains_auto_intent_without_blind_fan_write(delivery):
    r = delivery
    head = r.heads["a"]
    head._attr_fan_mode = None
    head.async_write_ha_state()
    r.sensor()
    await r.coord.async_refresh()
    await r.settle()
    assert "Current fan speed unavailable" in reasons(r)
    assert r.hass.states.get(_eid(r.hass, r.entry, "_primary_fan_auto")).state != "unavailable"
    assert not r.calls("a", kind="enter", method="fan")
    head._attr_fan_mode = "auto"
    head.async_write_ha_state()
    await r.coord.async_refresh()
    assert "Current fan speed unavailable" not in reasons(r)


@pytest.mark.parametrize("delivery", [{"start_enabled": False}], indirect=True)
async def test_fresh_public_command_observations_are_explicitly_absent(delivery):
    r = delivery
    view = r.zone()
    assert view["command_status"] == "not yet recorded"
    for key in ("command_attempted_at", "command_returned_at", "command_failed_at", "command_retired_at"):
        assert view[key] == "not yet recorded"
    head = r.hass.states.get(r.heads["a"].entity_id)
    assert view["head_state_updated_at"] == head.last_updated.isoformat()
    assert not r.calls("a", kind="enter")
    assert not r.calls("b", kind="enter")


@pytest.mark.parametrize("delivery", [{"start_enabled": False}], indirect=True)
async def test_head_attribute_only_update_adds_no_room_thermostat_event(delivery):
    """A head report the room tile does not show creates no room state row.

    The head's last_updated stays on the plan's zones[] (as of its publish)
    and on the head itself; a live copy on the thermostat made every head
    attribute-only update a thermostat state_changed event and recorder row.
    """
    r = delivery
    room = _eid(r.hass, r.entry, "_primary_thermostat")
    head = r.heads["a"]
    assert "head_state_updated_at" not in r.hass.states.get(room).attributes
    changed = []
    unsub = r.hass.bus.async_listen(
        EVENT_STATE_CHANGED, callback(lambda event: changed.append(event.data["entity_id"]))
    )
    try:
        head._attr_current_temperature = 71.5  # the room tile shows the room sensor
        head.async_write_ha_state()
        await r.settle()
        assert head.entity_id in changed
        assert room not in changed
        # Control: a change the tile does show still publishes.
        head._attr_fan_mode = "high"
        head.async_write_ha_state()
        await r.settle()
        assert room in changed
    finally:
        unsub()


async def test_late_vane_return_updates_reloaded_room_disclosure(hass, monkeypatch, request):
    """A cleanup return never stands in for the pending wake, across reload."""
    from tests.test_vane_kick import _setup_kickable, _start_blocked_kick

    entry, heads, vane = await _setup_kickable(hass)
    old = entry.runtime_data
    harness = await _start_blocked_kick(hass, entry, heads[0], vane, "wake", monkeypatch, request)
    old._vane_kick_retire = 0.05
    reload = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
    try:
        await until(reload.done, 3.0)
        assert await reload
        assert entry.state is ConfigEntryState.LOADED
        assert not harness.kick_task.done()
        assert harness.commands == [("head", "fan_only"), ("head", "off")]
        room_id = _eid(hass, entry, "_primary_thermostat")
        await until(lambda: (room := hass.states.get(room_id)) is not None
                    and room.attributes.get("command_status") == "pending", 1.0)
        current = entry.runtime_data
        assert current is not old
        room = hass.states.get(room_id)
        assert room.attributes["command_status"] == "pending"
        assert room.attributes["command_ownership_retired"] is True
        assert room.attributes["vane_retirement_cleanup"]["command_status"] == "returned"
        assert "pending since" in json.dumps(room.attributes["control_reasons"])
        assert room.attributes["command_deferred"] is True
        assert "Own-wake vane retirement parking call returned" in json.dumps(room.attributes["control_reasons"])
        assert not harness.kick_task.done()
        assert harness.commands == [("head", "fan_only"), ("head", "off")]
        retired_at = room.attributes["command_retired_at"]
        cleanup_returned_at = room.attributes["vane_retirement_cleanup"]["command_returned_at"]
        await hass.services.async_call("switch", "turn_off", {
            "entity_id": _eid(hass, entry, "_eco_idle"),
        }, blocking=True)
        await until(lambda: hass.states.get(room_id).attributes["command_deferred"])
        assert not harness.kick_task.done()
        assert harness.commands == [("head", "fan_only"), ("head", "off")]
        harness.barrier.release.set()
        await until(harness.kick_task.done)
        await harness.kick_task
        await until(lambda: len(harness.commands) == 3, 1.0)
        await until(lambda: hass.states.get(room_id).attributes["command_status"] == "returned", 1.0)
        room = hass.states.get(room_id)
        assert room.attributes["command_retired_at"] == retired_at
        assert "pending since" not in json.dumps(room.attributes["control_reasons"])
        assert datetime.fromisoformat(room.attributes["command_returned_at"]) >= datetime.fromisoformat(
            cleanup_returned_at
        )
        assert harness.commands == [("head", "fan_only"), ("head", "off"), ("head", "fan_only")]
        # A later real ordinary call replaces the old wake observation. Neither
        # public surface may keep historical cleanup as a current obstacle.
        await hass.services.async_call("climate", "set_fan_mode", {
            "entity_id": room_id, "fan_mode": "low",
        }, blocking=True)
        await until(lambda: hass.states.get(room_id).attributes["command_status"] == "returned")
        plan = hass.states.get(_eid(hass, entry, "_plan"))
        for view in (hass.states.get(room_id).attributes, plan.attributes["zones"][0]):
            assert view["command_status"] == "returned"
            assert view["command_ownership_retired"] is False
            assert "Own-wake vane retirement" not in json.dumps(view["control_reasons"])
            assert "pending since" not in json.dumps(view["control_reasons"])
    finally:
        harness.barrier.release.set()
        await until(harness.kick_task.done)
        await harness.kick_task
        assert await reload
        if hasattr(entry, "runtime_data") and not entry.runtime_data._retired:
            assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_deferred_no_issuance_completion_updates_both_room_surfaces(hass, monkeypatch, request):
    """Actual completion clears deferral even when the planned head is already off."""
    from tests.test_vane_kick import _setup_kickable, _start_blocked_kick

    entry, heads, vane = await _setup_kickable(hass)
    old = entry.runtime_data
    cid = heads[0].entity_id
    harness = await _start_blocked_kick(hass, entry, heads[0], vane, "wake", monkeypatch, request)
    old._vane_kick_retire = 0.05
    reload = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
    service_calls = []
    unsubscribe = hass.bus.async_listen(
        EVENT_CALL_SERVICE, callback(lambda event: service_calls.append(dict(event.data)))
    )
    try:
        await until(reload.done, 3.0)
        assert await reload
        assert entry.state is ConfigEntryState.LOADED
        current = entry.runtime_data
        assert current is not old
        room_id = _eid(hass, entry, "_primary_thermostat")
        plan_id = _eid(hass, entry, "_plan")
        await until(lambda: cid in current._deliveries, 1.0)
        generation = current._refresh_generation
        # Let already scheduled peer work finish, without another input or clock sweep.
        await asyncio.sleep(0.5)
        assert current._refresh_generation == generation
        assert not current._plan_lock.locked()
        assert set(current._deliveries) == {cid}
        assert current._deliveries[cid]["owner"] is current
        assert current._deliveries[cid]["locked"] is False
        assert current._head_locks[cid].locked()
        assert not harness.kick_task.done()
        assert harness.commands == [("head", "fan_only"), ("head", "off")]
        assert hass.states.get(cid).state == "off"
        before = hass.states.get(room_id).attributes
        for view in (before, hass.states.get(plan_id).attributes["zones"][0]):
            assert view["command_deferred"] is True
            assert view["command_status"] == "pending"
            assert view["command_ownership_retired"] is True
            assert view["vane_retirement_cleanup"]["command_status"] == "returned"
            assert "deferred behind the pending command" in json.dumps(view["control_reasons"])
            assert "pending since" in json.dumps(view["control_reasons"])
        calls_before = list(service_calls)
        harness.barrier.release.set()
        await until(harness.kick_task.done, 1.0)
        await harness.kick_task
        await until(lambda: cid not in current._deliveries, 2.0)
        await hass.async_block_till_done()
        # The handler returned, the queued no-op left, and nobody issued anything.
        assert not current._deliveries
        assert not current._vane_cleanup_pending
        assert not current._head_locks[cid].locked()
        assert current._refresh_generation == generation
        assert service_calls == calls_before
        assert harness.commands == [("head", "fan_only"), ("head", "off")]
        print("DELIVERY_NO_ISSUANCE " + json.dumps({
            "issued_after_release": service_calls[len(calls_before):],
            "deliveries_remaining": len(current._deliveries),
            "refresh_generation_unchanged": current._refresh_generation == generation,
        }))
        for view in (hass.states.get(room_id).attributes, hass.states.get(plan_id).attributes["zones"][0]):
            assert view["command_deferred"] is False
            assert "deferred behind the pending command" not in json.dumps(view["control_reasons"])
            assert view["command_status"] == "returned"
            assert "pending since" not in json.dumps(view["control_reasons"])
            assert view["command_ownership_retired"] is True
            assert view["command_attempted_at"] == before["command_attempted_at"]
            assert view["command_retired_at"] == before["command_retired_at"]
            assert view["vane_retirement_cleanup"] == before["vane_retirement_cleanup"]
    finally:
        harness.barrier.release.set()
        await until(harness.kick_task.done)
        await harness.kick_task
        assert await reload
        if hasattr(entry, "runtime_data") and not entry.runtime_data._retired:
            assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        unsubscribe()
        assert not old._deliveries
        assert not old._vane_cleanup_pending
        assert not old._head_locks[cid].locked()


async def test_inhibit_retires_only_its_pending_vane_wake(hass, monkeypatch, request):
    """Kick retirement is visible even when the coordinator remains live."""
    from tests.test_vane_kick import _setup_kickable, _start_blocked_kick

    entry, heads, vane = await _setup_kickable(hass, inhibitable=True)
    coord = entry.runtime_data
    harness = await _start_blocked_kick(hass, entry, heads[0], vane, "wake", monkeypatch, request)
    coord._vane_kick_retire = 0.05
    room_id = _eid(hass, entry, "_primary_thermostat")
    before = hass.states.get(room_id).attributes
    try:
        assert before["command_status"] == "pending"
        assert before["command_ownership_retired"] is False
        hass.states.async_set("binary_sensor.grid_hold", "on")
        await until(lambda: hass.states.get(room_id).attributes["command_ownership_retired"], 1.0)
        observed = hass.states.get(room_id).attributes
        assert observed["command_status"] == "pending"
        assert observed["command_attempted_at"] == before["command_attempted_at"]
        assert observed["command_returned_at"] == before["command_returned_at"]
        assert observed["command_retired_at"] != "not yet recorded"
        assert "Command ownership retired" in json.dumps(observed["control_reasons"])
        assert coord.inhibited and not coord._retired
        await until(lambda: ("head", "off") in harness.commands, 1.0)
        await until(lambda: hass.states.get(room_id).attributes["vane_retirement_cleanup"].get(
            "command_status") == "returned", 1.0)
        assert not harness.kick_task.done()
        assert hass.states.get(room_id).attributes["command_status"] == "pending"
    finally:
        harness.barrier.release.set()
        await until(harness.kick_task.done)
        await harness.kick_task
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert harness.commands == [("head", "fan_only"), ("head", "off")]
