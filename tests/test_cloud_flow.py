"""Cloud heads (beta): choosing a head type in setup and on the room page.

Setup suggests the type from the head's entity platform (melcloud, kumo,
kumo_cloud) and the user confirms it; the room page suggests nothing, so a
room with no stored type shows local there. An ESPHome head is always local.
What is stored: ``head_type: cloud``, or ``head_type: local`` where the platform
suggested cloud and the user kept local, and otherwise nothing, so every
entry stored before this option, and every CN105 room, reads local and stores
exactly what it stored before.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

from custom_components.mxz_coordinator.const import (
    DOMAIN,
    HEAD_TYPE_CLOUD,
    HEAD_TYPE_LOCAL,
    VOTE_BASIS_ACTION,
    ZONE_CONFIRM_WAIT,
    ZONE_DRIFT_COOLER_BY,
    ZONE_DRIVE_OFFSET,
    ZONE_HEAD_TYPE,
    ZONE_WRITE_INTERVAL,
)
from tests.cloud_heads import (
    COMFORT,
    ESPHOME,
    MELCLOUD,
    SENSORS,
    ComfortHead,
    MelcloudHead,
    register,
    set_room,
    setup_rooms,
)
from tests.flow_pages import (
    default,
    fields,
    nest,
    open_options_page,
    prefilled,
    sections,
    submit,
)


async def _heads(hass: HomeAssistant, platforms: list[str]) -> list[Any]:
    """One head per platform, in this order, registered on that platform."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [
        (MelcloudHead if platform == MELCLOUD else ComfortHead)(chr(ord("a") + index), mode="fan_only")
        for index, platform in enumerate(platforms)
    ]
    groups: dict[str, list[Any]] = {}
    for platform, head in zip(platforms, heads):
        groups.setdefault(platform, []).append(head)
    await register(hass, groups)
    for sensor in SENSORS[: len(heads)]:
        await set_room(hass, sensor, 70)
    return heads


async def _setup_to_names(hass: HomeAssistant, heads: list[Any]) -> dict[str, Any]:
    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await manager.async_configure(
        result["flow_id"], {"heads": [head.entity_id for head in heads]}
    )
    assert result["step_id"] == "rooms", result
    return await manager.async_configure(result["flow_id"], {})


async def _finish(hass: HomeAssistant, result: dict[str, Any], count: int) -> dict[str, Any]:
    manager = hass.config_entries.flow
    assert result["step_id"] == "sensors", result
    result = await manager.async_configure(
        result["flow_id"], {f"sensor_{i + 1}": SENSORS[i] for i in range(count)}
    )
    assert result["step_id"] == "review", result
    summary = result["description_placeholders"]["summary"]
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    result["summary"] = summary
    return result


async def test_setup_asks_only_for_the_cloud_platform_heads(hass: HomeAssistant) -> None:
    heads = await _heads(hass, [ESPHOME, MELCLOUD, COMFORT])
    result = await _setup_to_names(hass, heads)
    assert result["step_id"] == "head_type"
    assert fields(result) == ["head_type_2", "head_type_3"]
    assert default(result, "head_type_2") == HEAD_TYPE_CLOUD  # pre-filled
    assert default(result, "head_type_3") == HEAD_TYPE_CLOUD
    assert "(melcloud)" in result["description_placeholders"]["rooms"]
    assert "(kumo_cloud)" in result["description_placeholders"]["rooms"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"head_type_2": HEAD_TYPE_CLOUD, "head_type_3": HEAD_TYPE_LOCAL}
    )
    result = await _finish(hass, result, 3)
    zones = result["data"]["zones"]
    assert ZONE_HEAD_TYPE not in zones[0]  # ESPHome: never asked, nothing stored
    assert zones[1][ZONE_HEAD_TYPE] == HEAD_TYPE_CLOUD
    assert (zones[1]["vane_vertical"], zones[1]["vane_horizontal"], zones[1]["stage_sensor"]) == (
        None, None, None,
    )
    assert zones[2][ZONE_HEAD_TYPE] == HEAD_TYPE_LOCAL  # suggested cloud, kept local
    assert result["summary"].count("Head type: cloud (beta)") == 1
    for key in ("head_type_2", "head_type_3"):
        assert key not in {**result["data"], **result["options"]}


async def test_setup_with_no_cloud_platform_head_has_no_new_step(hass: HomeAssistant) -> None:
    heads = await _heads(hass, [ESPHOME, ESPHOME])
    result = await _setup_to_names(hass, heads)
    result = await _finish(hass, result, 2)
    assert all(ZONE_HEAD_TYPE not in zone for zone in result["data"]["zones"])
    assert "Head type" not in result["summary"]


async def _entry(hass: HomeAssistant, platforms: list[str], extras: list[dict] | None = None):
    heads = await _heads(hass, platforms)
    extras = extras or [{} for _ in heads]
    return heads, await setup_rooms(hass, list(zip(heads, extras)), enable=False)


@pytest.mark.parametrize(
    ("platform", "stored", "shown"),
    [
        (MELCLOUD, None, HEAD_TYPE_LOCAL),  # never asked: local, no suggestion
        (COMFORT, None, HEAD_TYPE_LOCAL),
        ("kumo", None, HEAD_TYPE_LOCAL),
        (ESPHOME, None, HEAD_TYPE_LOCAL),
        ("mqtt", None, HEAD_TYPE_LOCAL),
        (MELCLOUD, HEAD_TYPE_LOCAL, HEAD_TYPE_LOCAL),  # answered: kept
        ("mqtt", HEAD_TYPE_CLOUD, HEAD_TYPE_CLOUD),
    ],
)
async def test_room_page_shows_the_stored_type_or_local(
    hass: HomeAssistant, platform: str, stored: str | None, shown: str
) -> None:
    extra = {} if stored is None else {ZONE_HEAD_TYPE: stored}
    _h, entry = await _entry(hass, [platform, ESPHOME], [extra, {}])
    result = await open_options_page(hass, entry, "room_1")
    assert default(result, "head_type") == shown
    assert sections(result)["cloud"].options["collapsed"] is (shown != HEAD_TYPE_CLOUD)
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_room_page_saves_cloud_settings_and_local_clears_them(hass: HomeAssistant) -> None:
    _h, entry = await _entry(hass, [MELCLOUD, ESPHOME])
    result = await open_options_page(hass, entry, "room_1")
    assert default(result, "head_type") == HEAD_TYPE_LOCAL
    values = {
        **prefilled(result), "head_type": HEAD_TYPE_CLOUD,
        "drive_offset": 3.0, "write_interval": 120, "confirm_wait": 300,
    }
    await submit(hass.config_entries.options, result, values)
    await hass.async_block_till_done()
    zone = entry.data["zones"][0]
    assert zone[ZONE_HEAD_TYPE] == HEAD_TYPE_CLOUD
    assert (zone[ZONE_DRIVE_OFFSET], zone[ZONE_WRITE_INTERVAL], zone[ZONE_CONFIRM_WAIT]) == (
        3.0, 120.0, 300.0,
    )
    for key in ("head_type", "drive_offset", "write_interval", "confirm_wait", "cloud"):
        assert key not in entry.options and key not in entry.data
    assert entry.runtime_data.zones[0].drive_offset == 3.0

    result = await open_options_page(hass, entry, "room_1")
    assert default(result, "head_type") == HEAD_TYPE_CLOUD
    await submit(hass.config_entries.options, result, {**prefilled(result), "head_type": HEAD_TYPE_LOCAL})
    await hass.async_block_till_done()
    zone = entry.data["zones"][0]
    # Kept local on a cloud platform: remembered, and the settings are gone.
    assert zone[ZONE_HEAD_TYPE] == HEAD_TYPE_LOCAL
    assert not {ZONE_DRIVE_OFFSET, ZONE_WRITE_INTERVAL, ZONE_CONFIRM_WAIT} & set(zone)
    assert entry.runtime_data._cloud == {}


async def test_settings_left_at_their_defaults_store_nothing_and_follow_the_unit(
    hass: HomeAssistant,
) -> None:
    """Cloud chosen with the boxes untouched: no setting is stored.

    So the default offset follows the unit: after a switch to Celsius the room
    drives by 1 °C, not by a stored 2 read as 2 °C. A changed box is stored.
    """
    _h, entry = await _entry(hass, [MELCLOUD, ESPHOME])
    result = await open_options_page(hass, entry, "room_1")
    values = {**prefilled(result), "head_type": HEAD_TYPE_CLOUD}
    assert (values["drive_offset"], values["write_interval"], values["confirm_wait"]) == (
        2.0, 60.0, 90.0,
    )
    await submit(hass.config_entries.options, result, values)
    await hass.async_block_till_done()
    zone = entry.data["zones"][0]
    assert zone[ZONE_HEAD_TYPE] == HEAD_TYPE_CLOUD
    assert not {ZONE_DRIVE_OFFSET, ZONE_WRITE_INTERVAL, ZONE_CONFIRM_WAIT} & set(zone)
    assert entry.runtime_data.zones[0].drive_offset == 2.0

    hass.config.units = METRIC_SYSTEM
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.zones[0].drive_offset == 1.0

    result = await open_options_page(hass, entry, "room_1")
    await submit(
        hass.config_entries.options, result,
        {**prefilled(result), "drive_offset": 1.5, "write_interval": 60},
    )
    await hass.async_block_till_done()
    zone = entry.data["zones"][0]
    assert zone[ZONE_DRIVE_OFFSET] == 1.5
    assert not {ZONE_WRITE_INTERVAL, ZONE_CONFIRM_WAIT} & set(zone)
    assert entry.runtime_data.zones[0].drive_offset == 1.5


@pytest.mark.parametrize("platform", [MELCLOUD, COMFORT, "kumo"])
async def test_an_unrelated_save_leaves_a_never_asked_cloud_platform_room_local(
    hass: HomeAssistant, platform: str
) -> None:
    """An upgraded entry: a cloud-platform room with no stored type, saved for a drift change.

    The room stays local and stores no head type: only the drift changes.
    """
    _h, entry = await _entry(hass, [platform, ESPHOME])
    before = dict(entry.data["zones"][0])
    assert ZONE_HEAD_TYPE not in before
    result = await open_options_page(hass, entry, "room_1")
    result = await submit(
        hass.config_entries.options, result, {**prefilled(result), "room_cooler_by": 1.5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data["zones"][0] == {**before, ZONE_DRIFT_COOLER_BY: 1.5}
    assert entry.runtime_data._cloud == {}
    assert entry.runtime_data.zones[0].head_type == HEAD_TYPE_LOCAL


async def test_a_cn105_room_page_save_stores_what_it_stored_before(hass: HomeAssistant) -> None:
    _h, entry = await _entry(hass, [ESPHOME, ESPHOME])
    before = [dict(zone) for zone in entry.data["zones"]]
    result = await open_options_page(hass, entry, "room_2")
    assert default(result, "head_type") == HEAD_TYPE_LOCAL
    await submit(hass.config_entries.options, result, prefilled(result))
    await hass.async_block_till_done()
    assert entry.data["zones"] == before


@pytest.mark.parametrize(
    ("platform", "change", "error"),
    [
        (ESPHOME, {}, "cloud_esphome_head"),
        (MELCLOUD, {"vane_vertical": "select.vane"}, "cloud_unsupported_options"),
        (MELCLOUD, {"stage": "sensor.stage"}, "cloud_unsupported_options"),
        (MELCLOUD, {"dual_setpoint": True}, "cloud_unsupported_options"),
        (MELCLOUD, {"hands_off_vote_basis": VOTE_BASIS_ACTION}, "cloud_unsupported_options"),
    ],
)
async def test_room_page_refuses_what_a_cloud_head_cannot_do(
    hass: HomeAssistant, platform: str, change: dict, error: str
) -> None:
    hass.states.async_set("select.vane", "auto", {"options": ["auto"]})
    hass.states.async_set("sensor.stage", "LOW")
    _h, entry = await _entry(hass, [platform, ESPHOME])
    before = [dict(zone) for zone in entry.data["zones"]]
    result = await open_options_page(hass, entry, "room_1")
    result = await submit(
        hass.config_entries.options, result,
        {**prefilled(result), "head_type": HEAD_TYPE_CLOUD, **change},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert entry.data["zones"] == before
    # Local takes the same wiring.
    result = await submit(
        hass.config_entries.options, result,
        {**prefilled(result), "head_type": HEAD_TYPE_LOCAL, **change},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_room_page_refuses_a_setting_that_is_not_a_number(hass: HomeAssistant) -> None:
    _h, entry = await _entry(hass, [MELCLOUD, ESPHOME])
    result = await open_options_page(hass, entry, "room_1")
    flat = {**prefilled(result), "head_type": HEAD_TYPE_CLOUD, "drive_offset": float("nan")}
    result = await hass.config_entries.options.async_configure(result["flow_id"], nest(result, flat))
    assert result["errors"] == {"cloud": "not_a_number"}


async def test_an_existing_entry_reads_local(hass: HomeAssistant) -> None:
    """No stored key: local, even on a MELCloud head (the upgrade changes nothing).

    What a local MELCloud head is sent: tests/test_cloud_cn105.py.
    """
    _h, entry = await _entry(hass, [MELCLOUD, ESPHOME])
    assert entry.runtime_data._cloud == {}
    assert [zone.head_type for zone in entry.runtime_data.zones] == ["local", "local"]
    assert ZONE_HEAD_TYPE not in entry.data["zones"][0]


async def test_a_stored_cloud_esphome_head_stays_local(hass: HomeAssistant) -> None:
    _h, entry = await _entry(hass, [ESPHOME, ESPHOME], [{ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD}, {}])
    assert entry.runtime_data._cloud == {}
    assert entry.runtime_data.zones[0].head_type == HEAD_TYPE_LOCAL
