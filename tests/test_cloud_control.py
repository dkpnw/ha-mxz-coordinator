"""Cloud heads (beta): out of control, the repair issue, votes, and what is off.

A cloud head unavailable for 15 minutes, or available but silent (no report
at all, not even an unchanged one) for 35, is not controllable: MXZ
writes it nothing, its room's call stops counting toward the shared mode
(unless its Safety priority sensor is on, which keeps the room voting from
its room sensor), and a repair issue says so until the head reports again.
Silence gets longer because Home Assistant's own MELCloud integration polls
every 15 minutes (since 2026.2). Those limits are wall-clock time read off the head's own state, so these
tests move a frozen clock (``freezer``), as the sensor freshness tests do.

Off for cloud heads in v1 (DESIGN.md §4): the remote_temperature feed (MXZ
sends none to any head), vanes and the vane kick, dual setpoints, a hands-off
vote by action, and airflow (stage) sensors.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.mxz_coordinator.const import (
    DOMAIN,
    HEAD_TYPE_CLOUD,
    VOTE_BASIS_ACTION,
    ZONE_DUAL_SETPOINT,
    ZONE_HANDS_OFF,
    ZONE_HANDS_OFF_VOTE_BASIS,
    ZONE_HEAD_TYPE,
    ZONE_SAFETY_PRIORITY_ENTITY,
    ZONE_STAGE_SENSOR,
    ZONE_VANE_HORIZONTAL,
    ZONE_VANE_VERTICAL,
)
from tests.cloud_heads import (
    COMFORT,
    MELCLOUD,
    SENSORS,
    ComfortHead,
    MelcloudHead,
    past_startup,
    past_window,
    recompute,
    record,
    register,
    row,
    set_room,
    setup_rooms,
    writes,
)
from tests.test_drive import _eid

CLOUD = {ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD}
SAFETY = "binary_sensor.room_a_safety"


def _issue(hass: HomeAssistant, entry: Any, head: str) -> Any:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"cloud_head_unresponsive_{entry.entry_id}_{head}"
    )


async def _advance(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def _standoff(hass: HomeAssistant, *, safety: bool = False) -> tuple[Any, Any, Any]:
    """Room 1 (first priority) wants heat, room 2 wants cool, both cloud."""
    hass.config.units = METRIC_SYSTEM
    heads = [MelcloudHead("a", mode="fan_only"), MelcloudHead("b", mode="fan_only")]
    await register(hass, {MELCLOUD: heads})
    extra = dict(CLOUD)
    if safety:
        hass.states.async_set(SAFETY, "on")
        extra[ZONE_SAFETY_PRIORITY_ENTITY] = SAFETY
    entry = await setup_rooms(hass, [(heads[0], extra), (heads[1], CLOUD)], temps=(18, 24))
    await past_startup(hass, entry)
    assert entry.runtime_data.data["state"] == "heat"  # first priority wins
    return heads[0], heads[1], entry


@pytest.mark.parametrize("safety", [False, True], ids=["plain", "safety"])
async def test_unavailable_for_15_minutes_is_not_controllable(
    hass: HomeAssistant, freezer: Any, safety: bool
) -> None:
    a, b, entry = await _standoff(hass, safety=safety)
    a.go_unavailable()
    await hass.async_block_till_done()
    await _advance(hass, freezer, 14 * 60)
    b.poll()
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] != "not_controllable"
    assert _issue(hass, entry, a.entity_id) is None

    await _advance(hass, freezer, 60)
    b.poll()
    calls = record(hass)
    await set_room(hass, SENSORS[0], 17)
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] == "not_controllable"
    assert row(hass, entry, 1)["cloud_status"] != "not_controllable"
    issue = _issue(hass, entry, a.entity_id)
    assert issue is not None
    assert issue.translation_key == "cloud_head_unresponsive"
    assert issue.translation_placeholders == {
        "room": "Room 1", "head": a.entity_id, "minutes": "15",
    }
    assert writes(calls, a.entity_id) == []
    # Its room's heat no longer holds the outdoor unit; with Safety priority
    # on, it still does, from the room sensor.
    assert entry.runtime_data.data["state"] == ("heat" if safety else "cool")

    a.go_unavailable(False)
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] != "not_controllable"
    assert _issue(hass, entry, a.entity_id) is None
    assert entry.runtime_data.data["state"] == "heat"


async def test_a_silent_head_goes_out_and_one_unchanged_report_brings_it_back(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Available but quiet for 35 minutes. Its next poll, even unchanged, wakes MXZ."""
    a, b, entry = await _standoff(hass)
    a.poll()
    await _advance(hass, freezer, 34 * 60)
    b.poll()
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] != "not_controllable"
    await _advance(hass, freezer, 60)
    b.poll()
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] == "not_controllable"
    assert _issue(hass, entry, a.entity_id).translation_placeholders["minutes"] == "35"
    assert entry.runtime_data.data["state"] == "cool"
    assert _issue(hass, entry, a.entity_id) is not None

    a.poll()  # the same state again: a report, not a change
    await hass.async_block_till_done()
    # MXZ asked for the recompute itself; let its cooldown run out.
    await _advance(hass, freezer, REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)
    assert row(hass, entry, 0)["cloud_status"] != "not_controllable"
    assert _issue(hass, entry, a.entity_id) is None
    assert entry.runtime_data.data["state"] == "heat"


async def test_the_wakeup_finds_a_silent_head_by_itself(
    hass: HomeAssistant, freezer: Any
) -> None:
    """No report, no sensor change: the cloud wakeup marks it at 35 minutes."""
    a, _b, entry = await _standoff(hass)
    a.poll()
    await hass.async_block_till_done()
    await recompute(hass, entry)
    for _ in range(36):
        await _advance(hass, freezer, 60)
    await _advance(hass, freezer, REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)
    assert _issue(hass, entry, a.entity_id) is not None


async def test_unloading_clears_the_issue(hass: HomeAssistant, freezer: Any) -> None:
    a, b, entry = await _standoff(hass)
    a.go_unavailable()
    await _advance(hass, freezer, 15 * 60)
    b.poll()
    await recompute(hass, entry)
    assert _issue(hass, entry, a.entity_id) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, entry, a.entity_id) is None


async def test_a_stale_report_inside_the_wait_is_no_hands_off_vote(
    hass: HomeAssistant,
) -> None:
    """A released hands-off room: a poll of the heat it had is not a vote; the person's later heat is."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [ComfortHead("a", mode="heat"), ComfortHead("b", mode="fan_only")]
    await register(hass, {COMFORT: heads})
    a = heads[0]
    entry = await setup_rooms(hass, [(a, {**CLOUD, ZONE_HANDS_OFF: True}), (heads[1], CLOUD)])
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _eid(hass, entry, "_primary_enable")}, blocking=True
    )
    await past_startup(hass, entry)
    assert a.device["mode"] == "off"  # parked once
    await recompute(hass, entry)
    assert row(hass, entry, 0)["hands_off"] == "released"
    a.report(mode="heat")  # a poll from before the off
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert row(hass, entry, 0)["hands_off_vote"] is None
    past_window(entry, a.entity_id)
    a.device["mode"] = "heat"  # now a person runs it
    a.poll()
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert row(hass, entry, 0)["hands_off_vote"] == "heat"


async def test_cloud_rooms_get_no_vanes_stage_dual_or_action_vote(
    hass: HomeAssistant,
) -> None:
    """Stored wiring a cloud head can't use is ignored (the room page refuses it)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [ComfortHead("a", mode="fan_only"), ComfortHead("b", mode="fan_only")]
    await register(hass, {COMFORT: heads})
    hass.states.async_set("select.a_vane", "auto", {"options": ["auto", "swing"]})
    hass.states.async_set("sensor.a_stage", "LOW")
    gated = {
        **CLOUD,
        ZONE_VANE_VERTICAL: "select.a_vane",
        ZONE_VANE_HORIZONTAL: "select.a_vane",
        ZONE_STAGE_SENSOR: "sensor.a_stage",
        ZONE_DUAL_SETPOINT: True,
        ZONE_HANDS_OFF: True,
        ZONE_HANDS_OFF_VOTE_BASIS: VOTE_BASIS_ACTION,
    }
    entry = await setup_rooms(hass, [(heads[0], gated), (heads[1], CLOUD)])
    coordinator = entry.runtime_data
    zone = coordinator.zones[0]
    assert (zone.vane_vertical_id, zone.vane_horizontal_id, zone.stage_sensor_id) == (None,) * 3
    assert zone.dual is False
    assert coordinator._vote_basis(zone) == "mode"
    view = row(hass, entry, 0)
    assert "target_low" not in view
    assert "vote_basis" not in view
    registry = er.async_get(hass)
    unique = {e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert f"{entry.entry_id}_primary_target_low" not in unique
    thermostat = hass.states.get(_eid(hass, entry, "_primary_thermostat"))
    assert "swing_modes" not in thermostat.attributes


async def test_only_climate_mode_setpoint_and_fan_calls_reach_a_cloud_head(
    hass: HomeAssistant,
) -> None:
    """No remote_temperature, no vane select, no other service, through a whole run."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [ComfortHead("a", mode="off"), ComfortHead("b", mode="off")]
    await register(hass, {COMFORT: heads})
    entry = await setup_rooms(hass, [(heads[0], CLOUD), (heads[1], CLOUD)])
    calls = record(hass)
    await past_startup(hass, entry)
    for value in (60, 64, 68, 70, 76, 70):
        await set_room(hass, SENSORS[0], value)
        await recompute(hass, entry)
        past_window(entry, heads[0].entity_id, 300)
        await recompute(hass, entry)
    ours = [
        call for call in calls
        if heads[0].entity_id in str(call["service_data"].get("entity_id"))
    ]
    assert ours
    assert {(c["domain"], c["service"]) for c in ours} <= {
        ("climate", "set_hvac_mode"), ("climate", "set_temperature"), ("climate", "set_fan_mode"),
    }
    assert not [c for c in calls if c["domain"] == "select"]
    assert "remote_temperature" not in str(calls)
    # One setpoint only, never a range, though the head advertises one.
    for service, data in writes(calls, heads[0].entity_id):
        if service == "set_temperature":
            assert set(data) == {"temperature"}


async def test_a_floor_run_on_a_cloud_head_ends_off_not_handed_back(
    hass: HomeAssistant,
) -> None:
    """A person heats a released cloud room; its freeze floor trips anyway.

    On a local head MXZ would take the run over and give the person's heat
    back afterwards (P3), reading their setpoint off the head. A cloud head
    shows MXZ's offset setpoint minutes late, so in the beta its run ends the
    way a run on a head that wasn't heating does: off.
    """
    from custom_components.mxz_coordinator.const import ZONE_FLOOR_LOW

    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [ComfortHead("a", mode="off"), ComfortHead("b", mode="fan_only")]
    await register(hass, {COMFORT: heads})
    a = heads[0]
    entry = await setup_rooms(
        hass, [(a, {**CLOUD, ZONE_HANDS_OFF: True, ZONE_FLOOR_LOW: 60.0}), (heads[1], CLOUD)]
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _eid(hass, entry, "_primary_enable")}, blocking=True
    )
    await past_startup(hass, entry)
    await recompute(hass, entry)
    assert row(hass, entry, 0)["hands_off"] == "released"
    a.device.update(mode="heat", temperature=64.0)  # the person's own heat
    a.poll()
    await hass.async_block_till_done()
    await set_room(hass, SENSORS[0], 55)  # below the floor anyway
    await recompute(hass, entry)
    coordinator = entry.runtime_data
    assert coordinator.floor_tripped("primary")
    assert "primary" not in coordinator._floor_prior  # nothing taken over
    past_window(entry, a.entity_id)
    await recompute(hass, entry)
    assert a.device["mode"] == "heat"
    await set_room(hass, SENSORS[0], 66)  # past the clear line
    past_window(entry, a.entity_id)
    await recompute(hass, entry)
    past_window(entry, a.entity_id)
    await recompute(hass, entry)
    assert not coordinator.floor_tripped("primary")
    assert a.device["mode"] == "off"


async def test_a_head_polled_every_15_minutes_stays_controllable(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Home Assistant's MELCloud reports a quiet head every 15 minutes: never out."""
    a, b, entry = await _standoff(hass)
    for _ in range(8):  # two hours
        await _advance(hass, freezer, 15 * 60)
        a.poll()
        b.poll()
        await hass.async_block_till_done()
        await recompute(hass, entry)
        assert row(hass, entry, 0)["cloud_status"] != "not_controllable"
        assert _issue(hass, entry, a.entity_id) is None
    assert entry.runtime_data.data["state"] == "heat"
