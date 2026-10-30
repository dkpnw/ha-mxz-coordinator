"""Cloud heads (beta): the write path, the write limits, and the confirm wait.

The heads are the invented fakes in tests/cloud_heads.py: a Kumo Cloud head
as comfort_HA presents it and a MELCloud head. Each fake's ``device`` is the
oracle of what landed, and the service calls MXZ made are recorded from the
event bus, independently of MXZ's own bookkeeping.

Waits run on the monotonic clock, so a test ages a head's record
(``age_cloud``) instead of sleeping; the 15-minute silence limit is on the
wall clock and states' own report times, so a test steps that clock.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

from custom_components.mxz_coordinator.const import (
    CONF_IDLE_ACTION,
    HEAD_TYPE_CLOUD,
    IDLE_ACTION_OFF,
    ZONE_CONFIRM_WAIT,
    ZONE_DRIVE_OFFSET,
    ZONE_HEAD_TYPE,
    ZONE_WRITE_INTERVAL,
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


async def _comfort_pair(
    hass: HomeAssistant, *, extra: dict | None = None, delayed: bool = False, **kwargs
) -> tuple[ComfortHead, ComfortHead, object]:
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [ComfortHead("a", mode="fan_only"), ComfortHead("b", mode="fan_only")]
    for head in heads:
        head.delayed = delayed
    await register(hass, {COMFORT: heads})
    entry = await setup_rooms(
        hass, [(head, {**CLOUD, **(extra or {})}) for head in heads], **kwargs
    )
    return heads[0], heads[1], entry


async def test_wake_is_mode_then_one_offset_setpoint(hass: HomeAssistant) -> None:
    """A parked comfort head wakes: its mode alone, then one setpoint, target + 2 °F."""
    a, b, entry = await _comfort_pair(hass)
    calls = record(hass)
    await set_room(hass, SENSORS[0], 65)
    await recompute(hass, entry)
    # A new coordinator holds a cloud head for one confirm wait.
    assert writes(calls, a.entity_id) == []
    await past_startup(hass, entry)

    assert writes(calls, a.entity_id) == [
        ("set_hvac_mode", {"hvac_mode": "heat"}),
        ("set_temperature", {"temperature": 72.0}),
        # No fan yet: a start counts as a fan step (one per five minutes).
    ]
    # Landed, though the head advertises a range and drops a mode in
    # set_temperature: only the separate call could have woken it.
    assert a.device["mode"] == "heat"
    assert a.device["temperature"] == 72.0
    assert writes(calls, b.entity_id) == []


async def _driven(hass: HomeAssistant, entry, sensor: str, value: float) -> None:
    await set_room(hass, sensor, value)
    await recompute(hass, entry)


async def test_cool_drives_below_target(hass: HomeAssistant) -> None:
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 75)
    assert writes(calls, a.entity_id)[:2] == [
        ("set_hvac_mode", {"hvac_mode": "cool"}),
        ("set_temperature", {"temperature": 68.0}),
    ]
    assert a.device == {"mode": "cool", "temperature": 68.0, "fan": "auto"}


@pytest.mark.parametrize(
    ("offset", "act", "room", "sent"),
    [
        (0.0, "heat", 65, 70.0),  # 0 sends the bare target
        (0.0, "cool", 75, 70.0),
        (3.0, "heat", 65, 73.0),
        (3.5, "heat", 65, 74.0),  # 73.5 on a 1 °F step, half up
        (3.5, "cool", 75, 67.0),  # 66.5, half up
        (6.0, "heat", 65, 76.0),
    ],
)
async def test_drive_offset_per_room(
    hass: HomeAssistant, offset: float, act: str, room: float, sent: float
) -> None:
    """Each room's own offset; the other room keeps the default."""
    a, b, entry = await _comfort_pair(hass)
    entry_zones = [dict(z) for z in entry.data["zones"]]
    entry_zones[0][ZONE_DRIVE_OFFSET] = offset
    hass.config_entries.async_update_entry(entry, data={**entry.data, "zones": entry_zones})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await past_startup(hass, entry)
    calls = record(hass)
    await set_room(hass, SENSORS[1], room)
    await _driven(hass, entry, SENSORS[0], room)
    assert ("set_temperature", {"temperature": sent}) in writes(calls, a.entity_id)
    default = 72.0 if act == "heat" else 68.0
    assert ("set_temperature", {"temperature": default}) in writes(calls, b.entity_id)
    assert a.device["temperature"] == sent
    assert row(hass, entry, 0)["drive_offset"] == offset
    assert row(hass, entry, 1)["drive_offset"] == 2.0


@pytest.mark.parametrize(
    ("target", "sent"),
    [
        (85, 86.0),  # the room at 70 °F heats: 85 + 2 = 87, past the head's 86 °F max
        (62, 61.0),  # and cools: 62 - 2 = 60, below the head's 61 °F min
    ],
)
async def test_setpoint_is_clamped_to_the_head(
    hass: HomeAssistant, target: float, sent: float
) -> None:
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    calls = record(hass)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, entry, "_primary_target"), "value": target},
        blocking=True,
    )
    await recompute(hass, entry)
    sets = [w for w in writes(calls, a.entity_id) if w[0] == "set_temperature"]
    assert sets == [("set_temperature", {"temperature": sent})]
    assert a.device["temperature"] == sent


async def test_melcloud_rounds_to_its_half_degree(hass: HomeAssistant) -> None:
    """A °C system: 21 + 1 = 22; an offset of 0.6 lands on 21.5, the head's grid."""
    hass.config.units = METRIC_SYSTEM
    heads = [MelcloudHead("a", mode="fan_only"), MelcloudHead("b", mode="fan_only")]
    await register(hass, {MELCLOUD: heads})
    entry = await setup_rooms(
        hass, [(heads[0], CLOUD), (heads[1], {**CLOUD, ZONE_DRIVE_OFFSET: 0.6})]
    )
    await past_startup(hass, entry)
    calls = record(hass)
    await set_room(hass, SENSORS[1], 18)
    await _driven(hass, entry, SENSORS[0], 18)
    assert writes(calls, heads[0].entity_id)[:2] == [
        ("set_hvac_mode", {"hvac_mode": "heat"}),
        ("set_temperature", {"temperature": 22.0}),
    ]
    assert ("set_temperature", {"temperature": 21.5}) in writes(calls, heads[1].entity_id)
    # MELCloud polls: the device holds it, HA shows it at the next poll.
    assert heads[0].device["temperature"] == 22.0
    assert hass.states.get(heads[0].entity_id).state == "fan_only"
    heads[0].poll()
    await hass.async_block_till_done()
    assert hass.states.get(heads[0].entity_id).state == "heat"


async def test_celsius_head_in_a_fahrenheit_house(hass: HomeAssistant) -> None:
    """70 °F + 2 = 72 °F = 22.2 °C: the head's grid makes it 22.0 °C, sent as 71.6 °F."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [MelcloudHead("a", mode="fan_only"), MelcloudHead("b", mode="fan_only")]
    await register(hass, {MELCLOUD: heads})
    entry = await setup_rooms(hass, [(heads[0], CLOUD), (heads[1], CLOUD)])
    await past_startup(hass, entry)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 65)
    assert ("set_temperature", {"temperature": 71.6}) in writes(calls, heads[0].entity_id)
    assert heads[0].device["temperature"] == pytest.approx(22.0)


class _NoStepHead(ComfortHead):
    _attr_target_temperature_step = None


@pytest.mark.parametrize(
    ("units", "offset", "room", "sent"),
    [
        (US_CUSTOMARY_SYSTEM, 1.5, 65, 72.0),  # 71.5 on the 1 °F fallback
        (METRIC_SYSTEM, 0.3, 18, 21.5),  # 21.3 on the 0.5 °C fallback
    ],
)
async def test_a_head_with_no_step_uses_the_unit_fallback(
    hass: HomeAssistant, units, offset: float, room: float, sent: float
) -> None:
    hass.config.units = units
    heads = [_NoStepHead("a", mode="fan_only"), _NoStepHead("b", mode="fan_only")]
    await register(hass, {COMFORT: heads})
    entry = await setup_rooms(
        hass, [(heads[0], {**CLOUD, ZONE_DRIVE_OFFSET: offset}), (heads[1], CLOUD)]
    )
    await past_startup(hass, entry)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], room)
    assert ("set_temperature", {"temperature": sent}) in writes(calls, heads[0].entity_id)


async def test_the_room_sensor_decides_when_the_room_is_done(hass: HomeAssistant) -> None:
    """The head holds 72 °F; the room reaching 70 °F parks it."""
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 65)
    assert a.device["mode"] == "heat"
    await _driven(hass, entry, SENSORS[0], 69)
    assert a.device["mode"] == "heat"  # 1 °F short: still running
    past_window(entry, a.entity_id)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 70)
    assert writes(calls, a.entity_id)[0] == ("set_hvac_mode", {"hvac_mode": "fan_only"})
    assert a.device["mode"] == "fan_only"
    assert a.device["temperature"] == 72.0  # never told to stop at 70: the sensor did


async def test_nothing_is_resent_inside_the_confirm_wait(hass: HomeAssistant) -> None:
    """A delayed head that hasn't reported yet is not written again until its wait ends."""
    a, _b, entry = await _comfort_pair(hass, delayed=True)
    await past_startup(hass, entry)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 65)
    first = writes(calls, a.entity_id)
    assert [w[0] for w in first] == ["set_hvac_mode", "set_temperature"]
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    for value in (64.8, 64.6, 64.9):
        await _driven(hass, entry, SENSORS[0], value)
    past_window(entry, a.entity_id, 89)
    await recompute(hass, entry)
    assert writes(calls, a.entity_id) == first
    # The wait is over and the head still shows fan_only: sent again.
    past_window(entry, a.entity_id, 1)
    await recompute(hass, entry)
    assert writes(calls, a.entity_id)[2:] == first


async def test_newer_intent_waits_for_the_write_interval_and_only_the_latest_goes(
    hass: HomeAssistant,
) -> None:
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 65)
    calls = record(hass)
    target = _eid(hass, entry, "_primary_target")
    for value in (71, 73, 74):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": target, "value": value}, blocking=True
        )
        await recompute(hass, entry)
    past_window(entry, a.entity_id, 59)
    await recompute(hass, entry)
    assert writes(calls, a.entity_id) == []
    assert entry.runtime_data._cloud[a.entity_id].release_at is not None
    past_window(entry, a.entity_id, 1)
    await recompute(hass, entry)
    assert writes(calls, a.entity_id) == [("set_temperature", {"temperature": 76.0})]
    assert a.device["temperature"] == 76.0


@pytest.mark.parametrize(("interval", "early"), [(30, 29), (120, 119), (300, 299)])
async def test_write_interval_is_per_room(
    hass: HomeAssistant, interval: int, early: int
) -> None:
    a, _b, entry = await _comfort_pair(hass, extra={ZONE_WRITE_INTERVAL: interval})
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 65)
    calls = record(hass)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, entry, "_primary_target"), "value": 72}, blocking=True,
    )
    past_window(entry, a.entity_id, early)
    await recompute(hass, entry)
    assert writes(calls, a.entity_id) == []
    past_window(entry, a.entity_id, 1)
    await recompute(hass, entry)
    # (At 300 s the fan's next rung is due too, in the same batch.)
    assert [w for w in writes(calls, a.entity_id) if w[0] != "set_fan_mode"] == [
        ("set_temperature", {"temperature": 74.0})
    ]
    assert row(hass, entry, 0)["write_interval"] == interval


async def test_settings_out_of_range_are_clamped(hass: HomeAssistant) -> None:
    """A hand-edited entry can't make MXZ write faster than every 30 s."""
    _a, _b, entry = await _comfort_pair(
        hass, extra={ZONE_WRITE_INTERVAL: 1, ZONE_CONFIRM_WAIT: 5000, ZONE_DRIVE_OFFSET: 40}
    )
    view = row(hass, entry, 0)
    assert (view["write_interval"], view["confirm_wait"], view["drive_offset"]) == (30, 600, 6.0)


async def test_a_dropped_write_is_sent_again_after_the_wait_then_confirmed(
    hass: HomeAssistant,
) -> None:
    """MELCloud drops writes while rate limited: MXZ re-sends once the wait ends."""
    hass.config.units = METRIC_SYSTEM
    heads = [MelcloudHead("a", mode="fan_only"), MelcloudHead("b", mode="fan_only")]
    await register(hass, {MELCLOUD: heads})
    head = heads[0]
    entry = await setup_rooms(hass, [(head, CLOUD), (heads[1], CLOUD)])
    await past_startup(hass, entry)
    head.rate_limited = True
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 18)
    assert len(head.received) == 2  # reached the integration, dropped there
    head.poll()
    await hass.async_block_till_done()
    assert hass.states.get(head.entity_id).state == "fan_only"
    await recompute(hass, entry)
    assert len(writes(calls, head.entity_id)) == 2  # pending: not re-sent
    past_window(entry, head.entity_id)
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    assert len(writes(calls, head.entity_id)) == 4
    assert head.device["mode"] == "fan_only"
    head.rate_limited = False
    past_window(entry, head.entity_id)
    await recompute(hass, entry)
    head.poll()
    await hass.async_block_till_done()
    assert head.device == {"mode": "heat", "temperature": 22.0, "fan": "auto"}
    past_window(entry, head.entity_id)
    await recompute(hass, entry)
    view = row(hass, entry, 0)
    assert view["cloud_status"] == "confirmed"
    assert view["cloud_confirmed_at"] is not None
    assert view["cloud_pending_until"] is None


async def test_confirm_wait_up_to_ten_minutes(hass: HomeAssistant) -> None:
    a, _b, entry = await _comfort_pair(hass, delayed=True, extra={ZONE_CONFIRM_WAIT: 600})
    await past_startup(hass, entry)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 65)

    def sent() -> list:
        return [w for w in writes(calls, a.entity_id) if w[0] != "set_fan_mode"]

    assert len(sent()) == 2
    past_window(entry, a.entity_id, 599)
    await recompute(hass, entry)
    assert len(sent()) == 2
    past_window(entry, a.entity_id, 1)
    await recompute(hass, entry)
    assert sent()[2:] == sent()[:2]  # mode and setpoint again
    assert row(hass, entry, 0)["confirm_wait"] == 600


async def test_fan_boost_moves_one_rung_per_five_minutes(hass: HomeAssistant) -> None:
    """10 °F off: local boost would go straight to high; a cloud head climbs."""
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 60)

    def fans() -> list[str]:
        return [w[1]["fan_mode"] for w in writes(calls, a.entity_id) if w[0] == "set_fan_mode"]

    # The start counts as a step: past_startup aged it 90 s.
    assert fans() == []
    past_window(entry, a.entity_id, 209)  # 299 s since the start
    await recompute(hass, entry)
    assert fans() == []
    past_window(entry, a.entity_id, 1)  # 300 s
    await recompute(hass, entry)
    assert fans() == ["low"]
    past_window(entry, a.entity_id, 299)
    await recompute(hass, entry)
    assert fans() == ["low"]
    past_window(entry, a.entity_id, 1)
    await recompute(hass, entry)
    assert fans() == ["low", "medium"]
    # The next rung, "middle", is not one comfort_HA lists: skipped, not sent.
    past_window(entry, a.entity_id, 300)
    await recompute(hass, entry)
    assert fans() == ["low", "medium"]
    past_window(entry, a.entity_id, 300)
    await recompute(hass, entry)
    assert fans() == ["low", "medium", "high"]
    assert a.device["fan"] == "high"


async def test_a_fan_step_held_by_the_write_interval_does_not_climb_on(
    hass: HomeAssistant,
) -> None:
    """Confirm wait shorter than the write interval: still one speed at a time.

    The fan step comes due while the interval still holds the batch, and every
    recompute in the hold asks for the fan again. Only a rung sent counts, so
    the head gets low first, not a speed it climbed to while waiting.
    """
    a, _b, entry = await _comfort_pair(
        hass, extra={ZONE_WRITE_INTERVAL: 300, ZONE_CONFIRM_WAIT: 30}
    )
    await past_startup(hass, entry)  # the start, a fan step, is now 30 s old
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 60)

    def fans() -> list[str]:
        return [w[1]["fan_mode"] for w in writes(calls, a.entity_id) if w[0] == "set_fan_mode"]

    assert [w[0] for w in writes(calls, a.entity_id)] == ["set_hvac_mode", "set_temperature"]
    past_window(entry, a.entity_id, 270)  # the fan step is due, the interval holds 30 s more
    await recompute(hass, entry)
    for _ in range(5):
        past_window(entry, a.entity_id, 5)
        await recompute(hass, entry)
    assert fans() == []
    assert entry.runtime_data._cloud[a.entity_id].release_at is not None
    past_window(entry, a.entity_id, 5)  # the interval is over
    await recompute(hass, entry)
    assert fans() == ["low"]
    assert a.device["fan"] == "low"
    for _ in range(3):  # "middle", which comfort_HA doesn't list, is passed over
        past_window(entry, a.entity_id, 300)
        await recompute(hass, entry)
    assert fans() == ["low", "medium", "high"]


async def test_a_held_back_step_wakes_the_plan_by_itself(
    hass: HomeAssistant, freezer
) -> None:
    """No report, no sensor change: the cloud wakeup sends the waiting batch."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 65)
    calls = record(hass)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, entry, "_primary_target"), "value": 72}, blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.runtime_data._cloud_timer is not None
    for _ in range(4):  # 4 x 31 s: past the 60 s interval and the refresh cooldown
        freezer.tick(timedelta(seconds=31))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    assert ("set_temperature", {"temperature": 74.0}) in writes(calls, a.entity_id)


async def test_a_stale_fan_report_inside_the_wait_is_not_a_manual_pick(
    hass: HomeAssistant,
) -> None:
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 65)
    past_window(entry, a.entity_id, 300)  # the first fan step
    await recompute(hass, entry)
    assert a.device["fan"] == "low"
    coordinator = entry.runtime_data
    # A late poll from before the batch: the old fan, auto, and the old mode.
    a.report(mode="fan_only", fan="auto")
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert coordinator.fan_auto_is_on(a.entity_id)
    a.poll()
    past_window(entry, a.entity_id)
    await recompute(hass, entry)
    assert coordinator.fan_auto_is_on(a.entity_id)
    assert row(hass, entry, 0)["fan_hold"] is False
    # Negative: once the wait is over, a fan the head never had is a person's.
    a.device["fan"] = "powerful"
    a.poll()
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert not coordinator.fan_auto_is_on(a.entity_id)
    assert row(hass, entry, 0)["fan_hold"] is True


async def test_a_new_fan_inside_the_wait_is_a_manual_pick_once_read(
    hass: HomeAssistant,
) -> None:
    """A value the head never had is a real change, window or not."""
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 65)
    coordinator = entry.runtime_data
    a.device["fan"] = "powerful"
    a.poll()
    await hass.async_block_till_done()
    assert coordinator._cloud_stale_report(a.entity_id, fan="powerful") is False
    assert coordinator._cloud_stale_report(a.entity_id, fan="auto") is True
    past_window(entry, a.entity_id)
    calls = record(hass)
    await recompute(hass, entry)
    assert not coordinator.fan_auto_is_on(a.entity_id)
    assert [w for w in writes(calls, a.entity_id) if w[0] == "set_fan_mode"] == []


async def test_a_stale_off_report_starts_no_heal_but_a_real_one_does(
    hass: HomeAssistant,
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [ComfortHead("a", mode="off"), ComfortHead("b", mode="off")]
    await register(hass, {COMFORT: heads})
    a = heads[0]
    entry = await setup_rooms(
        hass, [(a, CLOUD), (heads[1], CLOUD)], options={CONF_IDLE_ACTION: IDLE_ACTION_OFF}
    )
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 65)
    assert a.device["mode"] == "heat"
    coordinator = entry.runtime_data
    a.report(mode="off")  # a poll from before the batch
    await hass.async_block_till_done()
    assert (a.entity_id, "off") not in coordinator._heal_timers
    a.poll()
    await hass.async_block_till_done()
    past_window(entry, a.entity_id)
    a.device["mode"] = "off"  # someone turned it off at the wall
    a.poll()
    await hass.async_block_till_done()
    assert (a.entity_id, "off") in coordinator._heal_timers


async def test_a_reload_starts_pending_and_sends_nothing_for_one_wait(
    hass: HomeAssistant,
) -> None:
    """Restart mid-confirm: the new coordinator neither re-sends nor reads the poll as a person."""
    a, _b, entry = await _comfort_pair(hass, delayed=True)
    await past_startup(hass, entry)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 65)
    assert len(writes(calls, a.entity_id)) == 2
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    # The head's late report of its pre-batch state, then a fan it never had:
    # neither is read while the restart's wait runs.
    a.report(fan="powerful")
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert len(writes(calls, a.entity_id)) == 2
    assert coordinator.fan_auto_is_on(a.entity_id)
    assert (a.entity_id, "off") not in coordinator._heal_timers
    a.poll()
    await hass.async_block_till_done()
    past_window(entry, a.entity_id)
    await recompute(hass, entry)
    # The batch from before the reload landed: nothing to send, confirmed.
    assert len(writes(calls, a.entity_id)) == 2
    assert row(hass, entry, 0)["cloud_status"] == "confirmed"


async def test_the_latch_skips_only_an_older_fan_inside_the_wait(hass: HomeAssistant) -> None:
    """Two steps behind: a late "low" is not a pick inside the wait, and is one after it.

    The record and the latch memory are set to the moment after MXZ stepped
    medium -> high on a head that last showed low; the fan decision is then
    asked directly, as a batch inside the wait would ask it.
    """
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    coordinator = entry.runtime_data
    cloud = coordinator._cloud[a.entity_id]
    clock = cloud.window_until  # any clock; the record is set relative to it
    cloud.sent(clock, ("heat", 72.0, "high"), "heat", "low")
    coordinator._fan_cmd[a.entity_id], coordinator._fan_prev[a.entity_id] = "high", "medium"
    coordinator._fan_idx[a.entity_id] = 4
    a.report(mode="heat", fan="low")
    await hass.async_block_till_done()
    state = hass.states.get(a.entity_id)
    assert coordinator._cloud_fan(a.entity_id, cloud, state, "heat", 5.0, "heat", clock + 10) is None
    assert coordinator.fan_auto_is_on(a.entity_id)
    # A fan the head never had is read inside the wait as usual: a pick.
    a.report(mode="heat", fan="powerful")
    await hass.async_block_till_done()
    state = hass.states.get(a.entity_id)
    assert coordinator._cloud_fan(a.entity_id, cloud, state, "heat", 5.0, "heat", clock + 10) is None
    assert not coordinator.fan_auto_is_on(a.entity_id)
    # After the wait, the older "low" is a departure from what MXZ sent.
    coordinator._fan_latched[a.entity_id] = False
    coordinator._fan_cmd[a.entity_id], coordinator._fan_prev[a.entity_id] = "high", "medium"
    a.report(mode="heat", fan="low")
    await hass.async_block_till_done()
    state = hass.states.get(a.entity_id)
    assert coordinator._cloud_fan(a.entity_id, cloud, state, "heat", 5.0, "heat", clock + 90) is None
    assert not coordinator.fan_auto_is_on(a.entity_id)


async def test_an_unavailable_head_is_sent_nothing(hass: HomeAssistant) -> None:
    """Before the 15 minutes too: MXZ writes no head it can't see."""
    hass.config.units = METRIC_SYSTEM
    heads = [MelcloudHead("a", mode="fan_only"), MelcloudHead("b", mode="fan_only")]
    await register(hass, {MELCLOUD: heads})
    entry = await setup_rooms(hass, [(heads[0], CLOUD), (heads[1], CLOUD)])
    await past_startup(hass, entry)
    heads[0].go_unavailable()
    await hass.async_block_till_done()
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 18)
    assert writes(calls, heads[0].entity_id) == []
    assert row(hass, entry, 0)["cloud_status"] != "not_controllable"
    heads[0].go_unavailable(False)
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert writes(calls, heads[0].entity_id)[:2] == [
        ("set_hvac_mode", {"hvac_mode": "heat"}),
        ("set_temperature", {"temperature": 22.0}),
    ]
