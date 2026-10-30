"""Cloud heads (beta): the CN105 and local paths are unchanged.

Every cloud branch keys off a room whose head type is cloud. These pin what a
local head gets next to cloud heads and on its own: an ESPHome (CN105) head
its one combined call with its low/high pair, no offset, no write limit, no
confirm wait and no startup hold; a local head off ESPHome the separate mode
call that shipped before cloud heads, with the bare target; and an all-CN105
install no cloud record, wakeup, row field or repair issue at all.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.mxz_coordinator.const import (
    DOMAIN,
    HEAD_TYPE_CLOUD,
    ZONE_HEAD_TYPE,
)
from tests.cloud_heads import (
    COMFORT,
    ESPHOME,
    MELCLOUD,
    SENSORS,
    ComfortHead,
    MelcloudHead,
    past_startup,
    recompute,
    record,
    register,
    row,
    set_room,
    setup_rooms,
    writes,
)
from tests.test_drive import MockHead, _eid

CLOUD_KEYS = {
    "head_type", "cloud_status", "cloud_pending_until", "cloud_confirmed_at",
    "drive_offset", "write_interval", "confirm_wait",
}


async def test_a_cn105_head_beside_a_cloud_head_gets_todays_call(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    cn105, cloud = MockHead("a"), ComfortHead("b", mode="fan_only")
    await register(hass, {ESPHOME: [cn105], COMFORT: [cloud]})
    entry = await setup_rooms(hass, [(cn105, {}), (cloud, {ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD})])
    calls = record(hass)
    await set_room(hass, SENSORS[0], 65)
    await set_room(hass, SENSORS[1], 65)
    await recompute(hass, entry)
    # At once, no startup hold; the combined call, its pair, no offset.
    assert [w for w in writes(calls, cn105.entity_id) if w[0] != "set_fan_mode"] == [
        ("set_temperature", {"hvac_mode": "heat", "target_temp_low": 70.0, "target_temp_high": 72.0}),
    ]
    assert writes(calls, cloud.entity_id) == []  # the cloud head waits out its start
    assert not CLOUD_KEYS & set(row(hass, entry, 0))
    assert row(hass, entry, 1)["head_type"] == "cloud"
    await past_startup(hass, entry)
    assert writes(calls, cloud.entity_id)[:2] == [
        ("set_hvac_mode", {"hvac_mode": "heat"}),
        ("set_temperature", {"temperature": 72.0}),
    ]
    # Ten quick retargets: the CN105 head takes every one, unlimited.
    target = _eid(hass, entry, "_primary_target")
    before = len(writes(calls, cn105.entity_id))
    for value in range(71, 81):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": target, "value": value}, blocking=True
        )
        await recompute(hass, entry)
    sets = [w for w in writes(calls, cn105.entity_id)[before:] if w[0] == "set_temperature"]
    assert [w[1]["target_temp_low"] for w in sets] == [float(v) for v in range(71, 81)]


@pytest.mark.parametrize("stored", [None, "local"], ids=["never_asked", "kept_local"])
async def test_a_local_head_off_esphome_keeps_the_separate_mode_call(
    hass: HomeAssistant, stored: str | None
) -> None:
    """A MELCloud head left local: the mode call, the bare target, no limit."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [MelcloudHead("a", mode="fan_only"), MelcloudHead("b", mode="fan_only")]
    heads[0].delayed = heads[1].delayed = False
    await register(hass, {MELCLOUD: heads})
    extra = {} if stored is None else {ZONE_HEAD_TYPE: stored}
    entry = await setup_rooms(hass, [(heads[0], extra), (heads[1], extra)])
    assert entry.runtime_data._cloud == {}
    calls = record(hass)
    await set_room(hass, SENSORS[0], 65)
    await recompute(hass, entry)
    assert writes(calls, heads[0].entity_id)[:2] == [
        ("set_hvac_mode", {"hvac_mode": "heat"}),
        ("set_temperature", {"temperature": 70.0}),
    ]
    target = _eid(hass, entry, "_primary_target")
    for value in (71, 72):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": target, "value": value}, blocking=True
        )
        await recompute(hass, entry)
    temps = [w[1]["temperature"] for w in writes(calls, heads[0].entity_id) if w[0] == "set_temperature"]
    assert temps == [70.0, 71.0, 72.0]
    assert not CLOUD_KEYS & set(row(hass, entry, 0))


async def test_an_all_cn105_install_has_no_cloud_machinery(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [MockHead("a"), MockHead("b")]
    await register(hass, {ESPHOME: heads})
    entry = await setup_rooms(hass, [(heads[0], {}), (heads[1], {})])
    coordinator = entry.runtime_data
    await set_room(hass, SENSORS[0], 65)
    await recompute(hass, entry)
    assert coordinator._cloud == {}
    assert coordinator._cloud_timer is None
    for index in (0, 1):
        assert not CLOUD_KEYS & set(row(hass, entry, index))
    assert not [
        issue for (domain, issue) in ir.async_get(hass).issues if domain == DOMAIN
    ]
    # A CN105 head reporting off is drift at once: no confirm wait guards it.
    heads[0]._attr_hvac_mode = "off"
    heads[0].async_write_ha_state()
    await hass.async_block_till_done()
    assert (heads[0].entity_id, "off") in coordinator._heal_timers
