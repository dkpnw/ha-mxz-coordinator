"""Generic heads (beta): the write path, the silence rule, safety and grouping.

The heads are the invented push-only fakes in tests/generic_heads.py. Each
fake's ``device`` is the oracle of what landed, and the service calls MXZ made
are recorded from the event bus, independently of MXZ's own bookkeeping.

A generic head takes the cloud path (tests/test_cloud_heads.py), with a 30 s
write interval and confirm wait by default, and only unavailability takes it
out of control: silence never does. Monotonic waits are aged
(``past_startup`` / ``past_window``); the wall-clock limits are stepped with
a frozen clock (``freezer``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.mxz_coordinator.const import (
    CONF_IDLE_ACTION,
    DOMAIN,
    EVIDENCE_HA_WRITE,
    HEAD_TYPE_CLOUD,
    HEAD_TYPE_GENERIC,
    HEAD_TYPE_LOCAL,
    IDLE_ACTION_OFF,
    ZONE_EVIDENCE_BASIS,
    ZONE_FLOOR_HIGH,
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
    ZONE_HEAD_TYPE,
    ZONE_REPORT_INTERVAL,
    ZONE_STAGE_SENSOR,
    ZONE_VANE_VERTICAL,
)
from tests.cloud_heads import (
    MELCLOUD,
    SENSORS,
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
from tests.generic_heads import GENERIC, OddFanHead, PlainHead, RichHead
from tests.test_drive import _eid

GEN = {ZONE_HEAD_TYPE: HEAD_TYPE_GENERIC}
OFF = {CONF_IDLE_ACTION: IDLE_ACTION_OFF}  # a PlainHead has no fan_only


def _issue(hass: HomeAssistant, entry: Any, head: str) -> Any:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"cloud_head_unresponsive_{entry.entry_id}_{head}"
    )


async def _advance(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def _driven(hass: HomeAssistant, entry, sensor: str, value: float) -> None:
    await set_room(hass, sensor, value)
    await recompute(hass, entry)


async def _plain_alone(
    hass: HomeAssistant, extra: dict | None = None, **kwargs
) -> tuple[PlainHead, Any]:
    """One feature-poor generic head in its own 1-head entry, in °F."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head = PlainHead("a", mode="off")
    await register(hass, {GENERIC: [head]})
    entry = await setup_rooms(hass, [(head, {**GEN, **(extra or {})})], options=OFF, **kwargs)
    return head, entry


# -- the write path ---------------------------------------------------------


async def test_a_feature_poor_head_alone_is_driven_by_mode_then_one_setpoint(
    hass: HomeAssistant,
) -> None:
    """No fan, step, current temperature or hvac_action: it is still driven.

    Its mode on its own call, then one setpoint, target + 2 °F on the 1 °F
    fallback grid, and no fan write ever.
    """
    head, entry = await _plain_alone(hass)
    state = hass.states.get(head.entity_id)
    for missing in ("fan_modes", "target_temp_step", "current_temperature", "hvac_action"):
        assert state.attributes.get(missing) is None, missing
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 65)
    assert writes(calls, head.entity_id) == []  # the startup confirm wait
    await past_startup(hass, entry)
    assert writes(calls, head.entity_id) == [
        ("set_hvac_mode", {"hvac_mode": "heat"}),
        ("set_temperature", {"temperature": 72.0}),
    ]
    assert head.device == {"mode": "heat", "temperature": 72.0}
    plan = row(hass, entry, 0)
    assert plan["head_type"] == HEAD_TYPE_GENERIC
    assert (plan["write_interval"], plan["confirm_wait"]) == (30, 30)
    assert entry.runtime_data.data.state == "heat"

    # The room sensor decides when it is done; then cool on the room's own
    # demand: a 1-head entry is its own outdoor unit.
    past_window(entry, head.entity_id)
    await _driven(hass, entry, SENSORS[0], 70)
    assert head.device["mode"] == "off"
    past_window(entry, head.entity_id)
    await _driven(hass, entry, SENSORS[0], 75)
    assert head.device == {"mode": "cool", "temperature": 68.0}
    assert entry.runtime_data.data.state == "cool"
    assert not [call for call in head.received if call[0] not in ("set_hvac_mode", "set_temperature")]


async def test_a_feature_rich_head_gets_vanes_off_and_its_half_degree(
    hass: HomeAssistant,
) -> None:
    """°C, a 0.5 step: 21 + 1 °C. Wiring a generic room can't use is cleared."""
    hass.config.units = METRIC_SYSTEM
    head = RichHead("a", mode="fan_only")
    await register(hass, {GENERIC: [head]})
    hass.states.async_set("select.vane", "auto", {"options": ["auto"]})
    entry = await setup_rooms(
        hass,
        [(head, {**GEN, ZONE_VANE_VERTICAL: "select.vane", ZONE_STAGE_SENSOR: "sensor.stage"})],
    )
    zone = entry.runtime_data.zones[0]
    assert (zone.vane_vertical_id, zone.stage_sensor_id, zone.dual) == (None, None, False)
    await past_startup(hass, entry)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 18)
    assert writes(calls, head.entity_id)[:2] == [
        ("set_hvac_mode", {"hvac_mode": "heat"}),
        ("set_temperature", {"temperature": 22.0}),
    ]
    # Never a swing or vane call: only mode, setpoint and fan reach it.
    assert {call[0] for call in head.received} <= {"set_hvac_mode", "set_temperature", "set_fan_mode"}


async def test_fan_boost_uses_the_heads_own_names_and_skips_the_rest(
    hass: HomeAssistant,
) -> None:
    """A rich head climbs its own low/medium/high; an odd-named one gets none."""
    hass.config.units = METRIC_SYSTEM
    rich, odd = RichHead("a", mode="fan_only"), OddFanHead("b", mode="fan_only")
    await register(hass, {GENERIC: [rich, odd]})
    entry = await setup_rooms(hass, [(rich, GEN), (odd, GEN)])
    await past_startup(hass, entry)
    await set_room(hass, SENSORS[0], 17)
    await _driven(hass, entry, SENSORS[1], 17)
    assert (rich.device["mode"], odd.device["mode"]) == ("heat", "heat")
    for _ in range(4):  # 4 fan steps' worth (one rung per five minutes)
        past_window(entry, rich.entity_id, 300)
        past_window(entry, odd.entity_id, 300)
        await recompute(hass, entry)
    fans = [call[1]["fan_mode"] for call in rich.received if call[0] == "set_fan_mode"]
    assert fans and set(fans) <= {"low", "medium", "high"}
    assert rich.device["fan"] in ("low", "medium", "high")
    # The odd head lists none of the ladder's names: no fan write at all, and
    # it still heats.
    assert [call for call in odd.received if call[0] == "set_fan_mode"] == []
    assert odd.device == {"mode": "heat", "temperature": 22.0, "fan": "auto"}


# -- the silence rule -------------------------------------------------------


async def test_a_quiet_generic_head_stays_controllable_and_a_cloud_head_does_not(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Two hours without a report: the generic head is still driven.

    Beside it, in the same entry, a cloud head silent for the same two hours
    is out at 35 minutes, exactly as before.
    """
    hass.config.units = METRIC_SYSTEM
    quiet, cloud = RichHead("a", mode="fan_only"), MelcloudHead("b", mode="fan_only")
    await register(hass, {GENERIC: [quiet], MELCLOUD: [cloud]})
    entry = await setup_rooms(hass, [(quiet, GEN), (cloud, {ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD})])
    await past_startup(hass, entry)
    for _ in range(24):  # two hours, the wakeups running
        await _advance(hass, freezer, 5 * 60)
    await _advance(hass, freezer, REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] != "not_controllable"
    assert _issue(hass, entry, quiet.entity_id) is None
    assert row(hass, entry, 1)["cloud_status"] == "not_controllable"
    assert _issue(hass, entry, cloud.entity_id).translation_key == "cloud_head_unresponsive"

    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 18)
    assert writes(calls, quiet.entity_id)[:2] == [
        ("set_hvac_mode", {"hvac_mode": "heat"}),
        ("set_temperature", {"temperature": 22.0}),
    ]
    assert writes(calls, cloud.entity_id) == []


async def test_a_generic_head_unavailable_for_15_minutes_is_not_controllable(
    hass: HomeAssistant, freezer: Any
) -> None:
    head, entry = await _plain_alone(hass)
    await past_startup(hass, entry)
    head.go_unavailable()
    await hass.async_block_till_done()
    await _advance(hass, freezer, 14 * 60)
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] != "not_controllable"
    assert _issue(hass, entry, head.entity_id) is None

    await _advance(hass, freezer, 60)
    calls = record(hass)
    await _driven(hass, entry, SENSORS[0], 65)
    assert row(hass, entry, 0)["cloud_status"] == "not_controllable"
    issue = _issue(hass, entry, head.entity_id)
    assert issue.translation_key == "generic_head_unresponsive"
    assert issue.translation_placeholders == {
        "room": "Room 1", "head": head.entity_id, "minutes": "15",
    }
    assert writes(calls, head.entity_id) == []

    head.go_unavailable(False)
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert row(hass, entry, 0)["cloud_status"] != "not_controllable"
    assert _issue(hass, entry, head.entity_id) is None
    past_window(entry, head.entity_id)
    await recompute(hass, entry)
    assert head.device["mode"] == "heat"


# -- safety -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "room", "mode"),
    [
        ({ZONE_FLOOR_LOW: 60.0}, 55, "heat"),
        ({ZONE_FLOOR_HIGH: 85.0}, 90, "cool"),
        ({}, 55, "off"),  # negative: no floor, a disabled room is held off
    ],
    ids=["freeze_floor", "overheat_ceiling", "no_floor"],
)
async def test_a_disabled_generic_room_is_still_protected(
    hass: HomeAssistant, limit: dict, room: float, mode: str
) -> None:
    head, entry = await _plain_alone(hass, limit, enable=False)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_coordinator_enable")}, blocking=True
    )
    await past_startup(hass, entry)
    assert head.device["mode"] == "off"
    await _driven(hass, entry, SENSORS[0], room)
    assert entry.runtime_data.floor_tripped("primary") is bool(limit)
    assert head.device["mode"] == mode


async def test_a_stale_room_sensor_parks_a_generic_head(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Its sensor goes quiet past three report intervals: the call stops counting."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head = PlainHead("a", mode="off")
    await register(hass, {GENERIC: [head]})
    entry = await setup_rooms(
        hass,
        [(head, {**GEN, ZONE_REPORT_INTERVAL: 5, ZONE_EVIDENCE_BASIS: EVIDENCE_HA_WRITE})],
        options=OFF,
    )
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 65)
    assert row(hass, entry, 0)["sensor_health"] == "healthy"
    assert head.device["mode"] == "heat"
    await _advance(hass, freezer, 15 * 60)
    past_window(entry, head.entity_id)
    await recompute(hass, entry)
    assert row(hass, entry, 0)["sensor_health"] == "stale"
    assert head.device["mode"] == "off"
    # The generic head itself said nothing all that time, and is fine.
    assert row(hass, entry, 0)["cloud_status"] != "not_controllable"


async def test_a_manual_fan_survives_a_restart(hass: HomeAssistant) -> None:
    """A person's fan pick holds; a reload keeps the hold and writes no fan."""
    hass.config.units = METRIC_SYSTEM
    head = RichHead("a", mode="fan_only")
    await register(hass, {GENERIC: [head]})
    entry = await setup_rooms(hass, [(head, GEN)])
    await past_startup(hass, entry)
    await _driven(hass, entry, SENSORS[0], 18)
    assert head.device["mode"] == "heat"
    past_window(entry, head.entity_id)
    head.device["fan"] = "high"  # someone picks high on the unit
    head.push()
    await hass.async_block_till_done()
    await recompute(hass, entry)
    assert row(hass, entry, 0)["fan_hold"] is True
    assert hass.states.get(_eid(hass, entry, "_primary_fan_auto")).state == "off"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    calls = record(hass)
    await past_startup(hass, entry)
    for _ in range(3):  # well past a fan step
        past_window(entry, head.entity_id, 300)
        await recompute(hass, entry)
    assert row(hass, entry, 0)["fan_hold"] is True
    assert [w for w in writes(calls, head.entity_id) if w[0] == "set_fan_mode"] == []
    assert head.device["fan"] == "high"
    assert head.device["mode"] == "heat"


# -- grouping ---------------------------------------------------------------


async def test_generic_heads_mapped_to_one_outdoor_unit_share_its_mode(
    hass: HomeAssistant,
) -> None:
    """Several generic heads in one entry: the normal mode lock and vote."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    a, b = PlainHead("a", mode="off"), PlainHead("b", mode="off")
    await register(hass, {GENERIC: [a, b]})
    entry = await setup_rooms(hass, [(a, GEN), (b, GEN)], temps=(65, 75), options=OFF)
    await past_startup(hass, entry)
    await recompute(hass, entry)
    # Room 1 (first priority) wants heat, room 2 cool: heat wins, and room 2's
    # head is not run in cool against it.
    assert entry.runtime_data.data.state == "heat"
    assert a.device["mode"] == "heat"
    assert b.device["mode"] != "cool"


async def test_a_hands_off_generic_room_votes_by_mode(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    a, b = PlainHead("a", mode="off"), PlainHead("b", mode="off")
    await register(hass, {GENERIC: [a, b]})
    entry = await setup_rooms(
        hass, [(a, GEN), (b, {**GEN, ZONE_HANDS_OFF: True})], options=OFF
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _eid(hass, entry, "_secondary_enable")}, blocking=True
    )
    await past_startup(hass, entry)
    await recompute(hass, entry)
    assert row(hass, entry, 1)["hands_off"] == "released"
    b.device.update(mode="cool", temperature=72.0)  # its own remote
    b.push()
    await hass.async_block_till_done()
    past_window(entry, b.entity_id)
    await recompute(hass, entry)
    assert entry.runtime_data.data.state == "cool"
    # An enabled room's call outranks the vote.
    past_window(entry, a.entity_id)
    await _driven(hass, entry, SENSORS[0], 65)
    assert entry.runtime_data.data.state == "heat"


async def test_a_stored_generic_esphome_head_stays_local(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    a, b = PlainHead("a"), PlainHead("b")
    await register(hass, {"esphome": [a, b]})
    entry = await setup_rooms(hass, [(a, GEN), (b, {})], options=OFF, enable=False)
    assert entry.runtime_data.heads._clouds == {}
    assert [zone.head_type for zone in entry.runtime_data.zones] == [HEAD_TYPE_LOCAL] * 2
