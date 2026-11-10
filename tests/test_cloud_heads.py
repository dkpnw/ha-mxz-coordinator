"""Cloud heads (beta): the write path, the write limits, and the confirm wait.

The heads are the invented fakes in tests/cloud_heads.py: a Kumo Cloud head
as comfort_HA presents it and a MELCloud head. Each fake's ``device`` is the
oracle of what landed, and the service calls MXZ made are recorded from the
event bus, independently of MXZ's own bookkeeping.

Waits run on the coordinator's clock, so a test makes time pass on it
(``pass_time``, ``past_window``) instead of sleeping; the 15-minute silence
limit is on the wall clock and states' own report times, so a test steps that
clock.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.mxz_coordinator.const import (
    CONF_IDLE_ACTION,
    HEAD_TYPE_CLOUD,
    HEAD_TYPE_GENERIC,
    IDLE_ACTION_OFF,
    ZONE_CONFIRM_WAIT,
    ZONE_DRIVE_OFFSET,
    ZONE_DRIVE_OFFSET_UNIT,
    ZONE_FLOOR_LOW,
    ZONE_HEAD_TYPE,
    ZONE_WRITE_INTERVAL,
)
from tests.clocks import pass_time
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
from tests.peek import cloud_record
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
    ("units", "head", "platform", "set_in", "room", "sent"),
    [
        # 4 °F set, now in °C: 2.22 °C past the 21 °C target, on 0.5 °C.
        (METRIC_SYSTEM, MelcloudHead, MELCLOUD, "°F", 18, 23.0),
        # The same offset with no unit stored reads as 4 °C, held to 3.
        (METRIC_SYSTEM, MelcloudHead, MELCLOUD, None, 18, 24.0),
        # 2.5 °C set, now in °F: 4.5 °F past 70, half up on 1 °F.
        (US_CUSTOMARY_SYSTEM, ComfortHead, COMFORT, "°C", 65, 75.0),
        (US_CUSTOMARY_SYSTEM, ComfortHead, COMFORT, None, 65, 73.0),
    ],
)
async def test_a_set_offset_drives_in_the_unit_in_use(
    hass: HomeAssistant, units, head, platform: str, set_in: str | None,
    room: float, sent: float,
) -> None:
    """Cloud review NIT 4: after a °F/°C switch a set offset drives the head
    by its converted value, not by the same number in the new unit. With no
    unit stored (saved before it was) it reads as before."""
    hass.config.units = units
    heads = [head("a", mode="fan_only"), head("b", mode="fan_only")]
    await register(hass, {platform: heads})
    offset = 4.0 if units is METRIC_SYSTEM else 2.5
    extra = {**CLOUD, ZONE_DRIVE_OFFSET: offset}
    if set_in is not None:
        extra[ZONE_DRIVE_OFFSET_UNIT] = set_in
    entry = await setup_rooms(hass, [(heads[0], extra), (heads[1], CLOUD)])
    await past_startup(hass, entry)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], room)
    assert ("set_temperature", {"temperature": sent}) in writes(calls, heads[0].entity_id)
    assert heads[0].device["temperature"] == sent


# What each step below needs in its unit: the room's sensor and its target.
_ROOM_AND_TARGET = {"°C": (18, 21), "°F": (60, 70)}


@pytest.mark.parametrize(
    ("head_type", "set_in", "offset", "steps"),
    [
        # 4 °F: 2.22 °C past 21 °C is 23 on the head's 0.5 °C grid. 4 °F past
        # 70 °F is 23.33 °C, 23.5 on that grid, 74.3 °F. Twice in °C: each load
        # converts what is stored, never its last result (1.23 °C would send 22).
        (HEAD_TYPE_CLOUD, "°F", 4.0, (
            (METRIC_SYSTEM, 2.22, 23.0, 23.0),
            (METRIC_SYSTEM, 2.22, 23.0, 23.0),
            (US_CUSTOMARY_SYSTEM, 4.0, 74.3, 23.5),
            (METRIC_SYSTEM, 2.22, 23.0, 23.0),
        )),
        # 2.5 °C is 4.5 °F: 74.5 °F is 23.61 °C, 23.5 on the grid, 74.3 °F.
        # Back in °C, the number it was set as.
        (HEAD_TYPE_CLOUD, "°C", 2.5, (
            (US_CUSTOMARY_SYSTEM, 4.5, 74.3, 23.5),
            (US_CUSTOMARY_SYSTEM, 4.5, 74.3, 23.5),
            (METRIC_SYSTEM, 2.5, 23.5, 23.5),
            (US_CUSTOMARY_SYSTEM, 4.5, 74.3, 23.5),
        )),
        (HEAD_TYPE_GENERIC, "°F", 4.0, (
            (METRIC_SYSTEM, 2.22, 23.0, 23.0),
            (US_CUSTOMARY_SYSTEM, 4.0, 74.3, 23.5),
        )),
    ],
)
async def test_a_set_offset_stays_right_over_repeated_switches(
    hass: HomeAssistant, head_type: str, set_in: str, offset: float, steps: tuple,
) -> None:
    """Cloud review NIT 4: each load drives the head by the offset converted
    from what was stored, in either direction, however many loads and
    switches come between, and the stored offset and its unit never change.

    The target is set again at each step because a switch doesn't convert it;
    this test is only about the offset."""
    hass.config.units = steps[0][0]
    heads = [MelcloudHead("a", mode="fan_only"), MelcloudHead("b", mode="fan_only")]
    head = heads[0]
    await register(hass, {MELCLOUD: heads})
    stored = {ZONE_DRIVE_OFFSET: offset, ZONE_DRIVE_OFFSET_UNIT: set_in}
    entry = await setup_rooms(hass, [
        (head, {ZONE_HEAD_TYPE: head_type, **stored}),
        (heads[1], {ZONE_HEAD_TYPE: head_type}),
    ])
    for units, shown, sent, landed in steps:
        room, target = _ROOM_AND_TARGET[units.temperature_unit]
        hass.config.units = units
        await set_room(hass, SENSORS[0], room)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        # Inside the new coordinator's startup wait, which holds every write.
        head.device.update(mode="fan_only", temperature=15.0)  # so each step writes
        head.poll()
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": _eid(hass, entry, "_primary_target"), "value": target}, blocking=True,
        )
        await hass.async_block_till_done()
        calls = record(hass)
        await past_startup(hass, entry)
        assert row(hass, entry, 0)["drive_offset"] == shown
        assert ("set_temperature", {"temperature": sent}) in writes(calls, head.entity_id)
        assert head.device["temperature"] == pytest.approx(landed)
        zone = entry.data["zones"][0]
        assert {key: zone[key] for key in stored} == stored


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
    assert cloud_record(entry.runtime_data, a.entity_id).release_at is not None
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
    assert cloud_record(entry.runtime_data, a.entity_id).release_at is not None
    past_window(entry, a.entity_id, 5)  # the interval is over
    await recompute(hass, entry)
    assert fans() == ["low"]
    assert a.device["fan"] == "low"
    for _ in range(3):  # "middle", which comfort_HA doesn't list, is passed over
        past_window(entry, a.entity_id, 300)
        await recompute(hass, entry)
    assert fans() == ["low", "medium", "high"]


async def _setpoint_only_wait(hass: HomeAssistant):
    """A comfort head 10 °F off (boost climbs), its first step (low) landed
    and confirmed, then a new target sends a batch with no fan: 74.0 alone,
    50 s before the next step is due. That batch's 90 s wait is running."""
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 60)
    calls = record(hass)

    def sent() -> list[tuple[str, Any]]:
        return [(w[0], next(iter(w[1].values()))) for w in writes(calls, a.entity_id)]

    past_window(entry, a.entity_id, 300)  # the first fan step
    await recompute(hass, entry)
    assert sent() == [("set_fan_mode", "low")]
    past_window(entry, a.entity_id, 250)  # 50 s before the next step
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] == "confirmed"  # the low landed
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, entry, "_primary_target"), "value": 72}, blocking=True,
    )
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert sent() == [("set_fan_mode", "low"), ("set_temperature", 74.0)]  # no fan
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    return a, entry, sent


async def test_a_fan_step_due_inside_a_setpoint_only_wait_goes_out_then(
    hass: HomeAssistant,
) -> None:
    """Cloud review NIT 3. A batch with no fan doesn't stop fan decisions for
    its confirm wait: the fan the head shows isn't an older state (that batch
    didn't change it), and a boost step that comes due inside the wait goes
    out then, on its own, as soon as the write interval allows. Before, it
    waited for the wait to end. The setpoint is not sent again with it, and
    both limits still hold: no step before five minutes since the last, and
    no write inside the write interval."""
    a, entry, sent = await _setpoint_only_wait(hass)
    cloud = cloud_record(entry.runtime_data, a.entity_id)
    assert cloud.older_fans == set()
    past_window(entry, a.entity_id, 30)  # 20 s before the step: not yet
    await recompute(hass, entry)
    assert sent()[2:] == []
    past_window(entry, a.entity_id, 20)  # the step is due; the interval has 10 s left
    await recompute(hass, entry)
    assert sent()[2:] == []
    assert cloud.release_at is not None  # the plan wakes when the slot opens
    assert a.device["fan"] == "low"
    past_window(entry, a.entity_id, 10)  # the interval is over; the wait has 30 s left
    await recompute(hass, entry)
    assert sent()[2:] == [("set_fan_mode", "medium")]  # the fan alone
    assert a.device == {"mode": "heat", "temperature": 74.0, "fan": "medium"}
    # That write opens its own wait, in which the fan it replaced is older.
    assert cloud.intent == ("heat", 74.0, "medium")
    assert cloud.older_fans == {"low"}
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    past_window(entry, a.entity_id)
    await recompute(hass, entry)
    assert sent()[3:] == []
    assert row(hass, entry, 0)["cloud_status"] == "confirmed"


async def test_a_fan_pick_inside_a_setpoint_only_wait_is_read_then(
    hass: HomeAssistant,
) -> None:
    """Cloud review NIT 3: a speed someone picks on the head inside a
    setpoint-only batch's wait is held from the next plan, not from the
    wait's end, and MXZ writes no fan over it, then or after."""
    a, entry, sent = await _setpoint_only_wait(hass)
    coordinator = entry.runtime_data
    assert coordinator.fan_auto_is_on(a.entity_id)
    a.device["fan"] = "high"
    a.poll()
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] == "pending"  # still inside the wait
    assert not coordinator.fan_auto_is_on(a.entity_id)
    assert row(hass, entry, 0)["fan_hold"] is True
    past_window(entry, a.entity_id, 300)  # past the wait and a boost step
    await recompute(hass, entry)
    assert [w for w in sent()[2:] if w[0] == "set_fan_mode"] == []
    assert a.device["fan"] == "high"


async def _setpoint_only_wait_on_an_unconfirmed_fan(hass: HomeAssistant):
    """A long confirm wait: the wake batch sends mode, setpoint, and low (the
    startup wait aged the step limit past due). 100 s in, inside that wait, a
    new target sends 74.0 alone, so the low was never confirmed. The next step
    is due 200 s later, with 500 s of the setpoint batch's wait still to run."""
    a, _b, entry = await _comfort_pair(hass, extra={ZONE_CONFIRM_WAIT: 600})
    await past_startup(hass, entry)
    calls = record(hass)

    def sent() -> list[tuple[str, Any]]:
        return [(w[0], next(iter(w[1].values()))) for w in writes(calls, a.entity_id)]

    await _driven(hass, entry, SENSORS[0], 60)
    assert sent() == [
        ("set_hvac_mode", "heat"), ("set_temperature", 72.0), ("set_fan_mode", "low"),
    ]
    past_window(entry, a.entity_id, 100)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, entry, "_primary_target"), "value": 72}, blocking=True,
    )
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert sent()[3:] == [("set_temperature", 74.0)]  # no fan
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    return a, entry, sent


async def test_a_fan_step_after_an_unconfirmed_fan_goes_out_inside_the_wait(
    hass: HomeAssistant,
) -> None:
    """Cloud review NIT 3, when the last fan batch was never confirmed: the
    speed that batch sent is the head's current one, not an older state, so
    the fan is still decided. The step limit still holds."""
    a, entry, sent = await _setpoint_only_wait_on_an_unconfirmed_fan(hass)
    past_window(entry, a.entity_id, 190)  # 10 s before the step: not yet
    await recompute(hass, entry)
    assert sent()[4:] == []
    past_window(entry, a.entity_id, 10)  # the step is due
    await recompute(hass, entry)
    assert sent()[4:] == [("set_fan_mode", "medium")]  # the fan alone
    assert a.device == {"mode": "heat", "temperature": 74.0, "fan": "medium"}
    assert row(hass, entry, 0)["fan_hold"] is False


async def test_a_fan_pick_after_an_unconfirmed_fan_survives_the_wait(
    hass: HomeAssistant,
) -> None:
    """A speed someone picks in that same wait is held at once, and MXZ never
    writes over it, though the step that went out in the test above comes due."""
    a, entry, sent = await _setpoint_only_wait_on_an_unconfirmed_fan(hass)
    a.device["fan"] = "high"
    a.poll()
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    assert row(hass, entry, 0)["fan_hold"] is True
    past_window(entry, a.entity_id, 200)  # the step would be due
    await recompute(hass, entry)
    past_window(entry, a.entity_id, 600)  # past the wait and more steps
    await recompute(hass, entry)
    assert sent()[4:] == []
    assert a.device == {"mode": "heat", "temperature": 74.0, "fan": "high"}
    assert row(hass, entry, 0)["fan_hold"] is True


async def test_a_late_report_of_the_replaced_fan_is_still_stale_in_the_wait(
    hass: HomeAssistant,
) -> None:
    """Negative for the two tests above: only the last fan sent is spared. A
    late poll of the fan it replaced, inside the wait, is no pick and decides
    nothing, even with a step due."""
    a, entry, sent = await _setpoint_only_wait_on_an_unconfirmed_fan(hass)
    a.report(fan="auto")
    await hass.async_block_till_done()
    past_window(entry, a.entity_id, 200)  # the step would be due
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    assert row(hass, entry, 0)["fan_hold"] is False
    assert sent()[4:] == []
    assert a.device["fan"] == "low"


@pytest.mark.parametrize(
    ("room", "first", "target", "second"),
    [
        (60, 72.0, 72, 74.0),  # heat: target + 2
        (80, 68.0, 68, 66.0),  # cool: target - 2
    ],
)
async def test_a_held_fan_survives_a_new_setpoint(
    hass: HomeAssistant, room: float, first: float, target: float, second: float,
) -> None:
    """A speed held before a setpoint-only batch stays held through its wait
    and after it: the setpoint goes out, no fan does."""
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], room)
    a.device["fan"] = "high"
    a.poll()
    await hass.async_block_till_done()
    past_window(entry, a.entity_id)
    await recompute(hass, entry)
    assert row(hass, entry, 0)["fan_hold"] is True
    assert a.device["temperature"] == first
    calls = record(hass)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, entry, "_primary_target"), "value": target}, blocking=True,
    )
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert writes(calls, a.entity_id) == [("set_temperature", {"temperature": second})]
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    past_window(entry, a.entity_id, 60)  # the write interval is over, the wait isn't
    await recompute(hass, entry)
    past_window(entry, a.entity_id, 600)  # well past the wait and several steps
    await recompute(hass, entry)
    assert writes(calls, a.entity_id) == [("set_temperature", {"temperature": second})]
    assert a.device["fan"] == "high"
    assert row(hass, entry, 0)["fan_hold"] is True


async def test_an_off_batch_with_no_fan_sends_no_fan_after_the_off(
    hass: HomeAssistant,
) -> None:
    """Negative for NIT 3: only a heat or cool wait decides the fan. An off
    sent with no fan (fan boost was off, so the boost speed wasn't handed
    back to auto first) waits out its wait: turning boost back on inside it
    sends no auto after the off, and once the head shows off there is
    nothing to hand back."""
    a, _b, entry = await _comfort_pair(hass, options={CONF_IDLE_ACTION: IDLE_ACTION_OFF})
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 60)
    past_window(entry, a.entity_id, 300)  # the first fan step
    await recompute(hass, entry)
    assert a.device == {"mode": "heat", "temperature": 72.0, "fan": "low"}
    past_window(entry, a.entity_id)
    await recompute(hass, entry)
    coordinator = entry.runtime_data
    coordinator.fan_boost_enable = False
    a.delayed = True  # the off is reported only when polled
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 70)  # at target: the head goes off
    assert writes(calls, a.entity_id) == [("set_hvac_mode", {"hvac_mode": "off"})]
    assert hass.states.get(a.entity_id).state == "heat"  # not reported yet
    coordinator.fan_boost_enable = True
    past_window(entry, a.entity_id, 60)  # past the write interval, inside the wait
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    assert writes(calls, a.entity_id) == [("set_hvac_mode", {"hvac_mode": "off"})]
    a.poll()
    await hass.async_block_till_done()
    past_window(entry, a.entity_id)
    await recompute(hass, entry)
    assert writes(calls, a.entity_id) == [("set_hvac_mode", {"hvac_mode": "off"})]
    assert a.device == {"mode": "off", "temperature": 72.0, "fan": "low"}


async def test_a_fan_batch_still_waits_out_its_own_wait(hass: HomeAssistant) -> None:
    """Negative for NIT 3: after a batch that sent a fan, nothing is decided
    for its wait, the fan included: a speed reported then is read when the
    wait ends, not before (the fan MXZ sent may still be on its way)."""
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 60)
    past_window(entry, a.entity_id, 300)  # the first fan step
    calls = record(hass)
    await recompute(hass, entry)
    assert writes(calls, a.entity_id) == [("set_fan_mode", {"fan_mode": "low"})]
    coordinator = entry.runtime_data
    a.device["fan"] = "high"
    a.poll()
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    assert coordinator.fan_auto_is_on(a.entity_id)  # not read inside the wait
    past_window(entry, a.entity_id)
    await recompute(hass, entry)
    assert not coordinator.fan_auto_is_on(a.entity_id)  # read once it ends
    assert writes(calls, a.entity_id) == [("set_fan_mode", {"fan_mode": "low"})]


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
    """A value the head never had is a real change, window or not.

    The wake batch sent a mode and a setpoint but no fan, so the mode the
    head showed before it is an older state inside the wait, and the auto it
    showed is not (that batch didn't change the fan; cloud review NIT 3).
    The pick is held from the next plan inside the wait and stays held.
    """
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 65)
    coordinator = entry.runtime_data
    a.device["fan"] = "powerful"
    a.poll()
    await hass.async_block_till_done()
    assert coordinator._cloud_stale_report(a.entity_id, fan="powerful") is False
    assert coordinator._cloud_stale_report(a.entity_id, mode="fan_only") is True
    assert coordinator._cloud_stale_report(a.entity_id, fan="auto") is False
    calls = record(hass)
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] == "pending"
    assert not coordinator.fan_auto_is_on(a.entity_id)
    past_window(entry, a.entity_id)
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
    cloud = cloud_record(coordinator, a.entity_id)
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


async def test_a_cloud_re_pick_of_the_speed_just_written_is_overwritten(hass: HomeAssistant) -> None:
    """The accepted exception (docs/formal/PARKED-FINDINGS.md): a person picks the speed MXZ
    just wrote. The head's report can't show it, so it isn't held, and boost's next step
    writes over it. The control is test_the_exception_on_a_cloud_head's never-sent pick."""
    from tests.test_properties_fan import _cloud_entry, _cloud_step, _fans

    hass.config.units = US_CUSTOMARY_SYSTEM
    head = ComfortHead("a", temperature=70.0)
    await register(hass, {"kumo_cloud": [head]})
    entry = await _cloud_entry(hass, head)
    await _cloud_step(hass, entry, head)
    assert _fans(head)[-1] == "low"
    assert head.device["fan"] == "low"
    head.device["fan"] = "low"  # the person's pick: nothing a report could show
    head.poll()
    await hass.async_block_till_done()
    await _cloud_step(hass, entry, head)
    assert row(hass, entry, 0)["fan_hold"] is False
    assert _fans(head)[-1] == "medium"
    assert head.device["fan"] == "medium"


def _refuses(head: ComfortHead, monkeypatch: pytest.MonkeyPatch, times: int | None) -> None:
    """The cloud refuses ``head``'s mode changes ``times`` times (None: always), then takes them."""
    accept = type(head).async_set_hvac_mode
    left = [times]

    async def refuse(hvac_mode: Any) -> None:
        if left[0] is None or left[0] > 0:
            if left[0] is not None:
                left[0] -= 1
            head.received.append(("set_hvac_mode", {"hvac_mode": hvac_mode}))
            raise HomeAssistantError("invented cloud outage")
        await accept(head, hvac_mode)

    monkeypatch.setattr(head, "async_set_hvac_mode", refuse)


async def test_a_refused_batch_is_tried_again_once_the_write_interval_allows(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch the cloud refused was not sent: nothing waits on it, and the write interval still holds."""
    a, _b, entry = await _comfort_pair(hass)
    await past_startup(hass, entry)
    _refuses(a, monkeypatch, 1)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 65)
    assert writes(calls, a.entity_id) == [("set_hvac_mode", {"hvac_mode": "heat"})]
    view = row(hass, entry, 0)
    assert (view["command_status"], view["cloud_status"]) == ("rejected", "unconfirmed")
    past_window(entry, a.entity_id, 59)
    await recompute(hass, entry)
    assert len(writes(calls, a.entity_id)) == 1
    past_window(entry, a.entity_id, 1)
    await recompute(hass, entry)
    assert writes(calls, a.entity_id)[1:] == [
        ("set_hvac_mode", {"hvac_mode": "heat"}),
        ("set_temperature", {"temperature": 72.0}),
    ]
    assert a.device["mode"] == "heat"
    assert row(hass, entry, 0)["command_status"] == "returned"


async def test_a_floor_on_a_cloud_head_reads_held_while_its_command_is_refused(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    a, b = ComfortHead("a", mode="fan_only"), ComfortHead("b", mode="fan_only")
    await register(hass, {COMFORT: [a, b]})
    entry = await setup_rooms(
        hass, [(a, {**CLOUD, ZONE_FLOOR_LOW: 50}), (b, CLOUD)], enable=False,
    )
    for suffix in ("_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await past_startup(hass, entry)
    past_window(entry, a.entity_id)
    _refuses(a, monkeypatch, None)
    await _driven(hass, entry, SENSORS[0], 45)
    assert a.received[-1] == ("set_hvac_mode", {"hvac_mode": "heat"})
    assert row(hass, entry, 0)["floor_state"] == "held"
    await recompute(hass, entry)
    assert row(hass, entry, 0)["floor_state"] == "held"
    assert a.device["mode"] == "off"
    await entry.runtime_data.async_shutdown_listeners()


def _attempts(
    head: ComfortHead, monkeypatch: pytest.MonkeyPatch, freezer: Any, *, takes: float = 0.0,
) -> list[float]:
    """When each mode change reached ``head``, on the monotonic clock. Each call takes ``takes`` seconds."""
    times: list[float] = []
    call = head.async_set_hvac_mode

    async def timed(hvac_mode: Any) -> None:
        times.append(time.monotonic())
        if takes:
            freezer.tick(takes)
        await call(hvac_mode)

    monkeypatch.setattr(head, "async_set_hvac_mode", timed)
    return times


async def _quiet(hass: HomeAssistant, freezer: Any, seconds: int) -> None:
    """Time passes one second at a time, and only Home Assistant's own timers run."""
    for _ in range(seconds):
        freezer.tick(1)
        async_fire_time_changed(hass, dt_util.utcnow(), fire_all=False)
        await hass.async_block_till_done()


@pytest.mark.parametrize("confirm", [90, 600])
@pytest.mark.parametrize("refusals", [1, None])
async def test_a_refused_batch_goes_out_again_at_the_write_interval_with_nothing_else_happening(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, freezer: Any, confirm: int, refusals: int | None,
) -> None:
    """SM-3 on a quiet house: no compute is forced once the room needs heat, so MXZ must wake itself.

    A confirm wait longer than the write interval would otherwise set the retry's pace.
    """
    a, _b, entry = await _comfort_pair(hass, extra={ZONE_CONFIRM_WAIT: confirm})
    await set_room(hass, SENSORS[0], 65)
    await recompute(hass, entry)  # inside the startup wait: nothing goes out yet
    _refuses(a, monkeypatch, refusals)
    times = _attempts(a, monkeypatch, freezer)
    start = time.monotonic()
    await _quiet(hass, freezer, confirm + 200)
    assert times, "the startup wait ended and nothing was sent"
    assert times[0] - start <= confirm + 1
    gaps = [later - earlier for earlier, later in itertools.pairwise(times)]
    if refusals == 1:
        assert gaps == [60]  # one retry, taken, and nothing after it
        assert a.device["mode"] == "heat"
        assert row(hass, entry, 0)["command_status"] == "returned"
    else:
        assert gaps == [60, 60, 60]  # never faster than the write interval, never slower
        assert row(hass, entry, 0)["cloud_status"] == "unconfirmed"


async def test_a_refusal_that_outlasts_the_write_interval_is_sent_again_at_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, freezer: Any,
) -> None:
    """The cloud took 61 s to refuse: the write interval has already passed, so nothing more is waited."""
    a, _b, entry = await _comfort_pair(hass, extra={ZONE_CONFIRM_WAIT: 600})
    await set_room(hass, SENSORS[0], 65)
    await recompute(hass, entry)
    _refuses(a, monkeypatch, 1)
    times = _attempts(a, monkeypatch, freezer, takes=61)
    await _quiet(hass, freezer, 600 + 30)
    assert len(times) == 2, times
    # At once: when the refusal returns (61 s), or on the next second where Home Assistant
    # 2024.12's debouncer serves a request made during a running refresh. Without the
    # immediate request there is no retry until the 600 s window ends.
    assert 61 <= times[1] - times[0] <= 62
    assert a.device["mode"] == "heat"


async def test_a_batch_refused_after_its_compute_has_ended_still_wakes_the_plan(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, freezer: Any,
) -> None:
    """The batch waited behind a fan pick at the room's thermostat, so the compute that queued it
    is over when the cloud refuses it: the refusal itself must set the wakeup."""
    a, _b, entry = await _comfort_pair(hass, extra={ZONE_CONFIRM_WAIT: 600})
    coordinator = entry.runtime_data
    await set_room(hass, SENSORS[0], 65)
    await recompute(hass, entry)  # inside the startup wait: nothing goes out yet
    # Past the startup recompute and any refresh cooldown, so neither can wake MXZ by chance.
    await _quiet(hass, freezer, 60)
    entered, release = asyncio.Event(), asyncio.Event()
    pick = a.async_set_fan_mode

    async def slow(fan_mode: str) -> None:
        entered.set()
        await release.wait()
        await pick(fan_mode)

    monkeypatch.setattr(a, "async_set_fan_mode", slow)
    gesture = hass.async_create_task(hass.services.async_call(
        "climate", "set_fan_mode",
        {"entity_id": _eid(hass, entry, "_primary_thermostat"), "fan_mode": a.device["fan"]},
        blocking=True,
    ))
    while not entered.is_set():
        await asyncio.sleep(0)
    pass_time(coordinator, cloud_record(coordinator, a.entity_id).confirm_wait)
    _refuses(a, monkeypatch, 1)
    times = _attempts(a, monkeypatch, freezer)
    refresh = hass.async_create_task(coordinator.async_refresh())
    for _ in range(30):
        await asyncio.sleep(0)
    assert refresh.done(), "the compute waited on the head"
    assert row(hass, entry, 0)["command_deferred"], "no batch is waiting for the head"
    release.set()
    await gesture
    await hass.async_block_till_done()
    assert len(times) == 1, "the batch never reached the head"
    await _quiet(hass, freezer, 120)
    assert [later - times[0] for later in times] == [0, 60]
    assert a.device["mode"] == "heat"
