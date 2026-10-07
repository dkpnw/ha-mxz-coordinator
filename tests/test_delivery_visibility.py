"""Existing HA room surfaces disclose actual lookup and software delivery status."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from homeassistant.components.climate import ClimateEntityFeature
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


async def test_late_vane_return_updates_reloaded_room_disclosure(hass, monkeypatch, request):
    """A cleanup return never stands in for the pending wake, across reload."""
    from tests.test_vane_kick import _setup_kickable, _start_blocked_kick

    entry, heads, vane = await _setup_kickable(hass)
    old = entry.runtime_data
    harness = await _start_blocked_kick(hass, entry, heads[0], vane, "wake", monkeypatch, request)
    old._vane_kick_retire = 0.05
    unload = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
    setup = None
    try:
        await until(unload.done, 1.0)
        assert await unload
        assert not harness.kick_task.done()
        assert harness.commands == [("head", "fan_only"), ("head", "off")]
        room_id = _eid(hass, entry, "_primary_thermostat")
        setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
        await until(lambda: (room := hass.states.get(room_id)) is not None
                    and room.attributes.get("command_status") == "pending", 1.0)
        current = entry.runtime_data
        assert current is not old
        room = hass.states.get(room_id)
        assert room.attributes["command_status"] == "pending"
        assert room.attributes["command_ownership_retired"] is True
        assert room.attributes["vane_retirement_cleanup"]["command_status"] == "returned"
        assert "pending since" in json.dumps(room.attributes["control_reasons"])
        retired_at = room.attributes["command_retired_at"]
        harness.barrier.release.set()
        await until(harness.kick_task.done)
        await harness.kick_task
        await until(lambda: hass.states.get(room_id).attributes["command_status"] == "returned", 1.0)
        room = hass.states.get(room_id)
        assert room.attributes["command_retired_at"] == retired_at
        assert "pending since" not in json.dumps(room.attributes["control_reasons"])
        assert datetime.fromisoformat(room.attributes["command_returned_at"]) >= datetime.fromisoformat(
            room.attributes["vane_retirement_cleanup"]["command_returned_at"]
        )
        assert harness.commands == [("head", "fan_only"), ("head", "off")]
    finally:
        harness.barrier.release.set()
        await until(harness.kick_task.done)
        await harness.kick_task
        await unload
        if setup is not None:
            assert await setup
        if hasattr(entry, "runtime_data") and not entry.runtime_data._retired:
            assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
