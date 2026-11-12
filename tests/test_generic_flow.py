"""Generic heads (beta): choosing them in setup, on the room page and in Reconfigure.

Setup's first step has a Generic heads (beta) opt-in, off by default; with it
on, every head picked is generic, and one head alone is an entry of its own
(its own outdoor unit). One local or cloud head is still refused, in setup,
in Reconfigure and on the room page. A head that can't heat and cool, or has
no idle mode, is refused as any head is, and an ESPHome head is refused as
generic. Generic is offered in the cloud step and on the room page and is
never pre-filled. The heads are the invented fakes in tests/generic_heads.py.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.mxz_coordinator.const import (
    DOMAIN,
    HEAD_TYPE_CLOUD,
    HEAD_TYPE_GENERIC,
    HEAD_TYPE_LOCAL,
    ZONE_CONFIRM_WAIT,
    ZONE_DRIVE_OFFSET,
    ZONE_HEAD_TYPE,
    ZONE_WRITE_INTERVAL,
)
from tests.cloud_heads import (
    ESPHOME,
    MELCLOUD,
    SENSORS,
    MelcloudHead,
    register,
    set_room,
    setup_rooms,
)
from tests.flow_pages import (
    default,
    fields,
    marker_of,
    open_options_page,
    prefilled,
    sections,
    submit,
    suggested,
)
from tests.generic_heads import (
    GENERIC,
    CoolOnlyHead,
    HeatOnlyHead,
    NoIdleHead,
    PlainHead,
    RichHead,
)
from tests.peek import cloud_record

GEN = {ZONE_HEAD_TYPE: HEAD_TYPE_GENERIC}
TIMING = {ZONE_DRIVE_OFFSET, ZONE_WRITE_INTERVAL, ZONE_CONFIRM_WAIT}


async def _heads(hass: HomeAssistant, heads: dict[str, list[Any]]) -> list[Any]:
    hass.config.units = US_CUSTOMARY_SYSTEM
    await register(hass, heads)
    picked = [head for group in heads.values() for head in group]
    for sensor in SENSORS[: len(picked)]:
        await set_room(hass, sensor, 70)
    return picked


async def _pick(hass: HomeAssistant, heads: list[Any], **extra: Any) -> dict[str, Any]:
    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    return await manager.async_configure(
        result["flow_id"], {"heads": [head.entity_id for head in heads], **extra}
    )


async def _finish(hass: HomeAssistant, result: dict[str, Any], count: int) -> dict[str, Any]:
    manager = hass.config_entries.flow
    assert result["step_id"] == "rooms", result
    result = await manager.async_configure(result["flow_id"], {})
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


# -- setup ------------------------------------------------------------------


async def test_the_opt_in_is_off_by_default(hass: HomeAssistant) -> None:
    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert default(result, "generic_heads") is False
    manager.async_abort(result["flow_id"])


async def test_one_generic_head_is_an_entry_of_its_own(hass: HomeAssistant) -> None:
    (head,) = await _heads(hass, {GENERIC: [RichHead("a")]})
    result = await _pick(hass, [head], generic_heads=True)
    result = await _finish(hass, result, 1)  # no head type step
    (zone,) = result["data"]["zones"]
    assert zone[ZONE_HEAD_TYPE] == HEAD_TYPE_GENERIC
    assert (zone["vane_vertical"], zone["vane_horizontal"], zone["stage_sensor"]) == (None, None, None)
    assert not TIMING & set(zone)  # the defaults follow the type
    assert "Head type: generic (beta)" in result["summary"]
    assert "generic_heads" not in {**result["data"], **result["options"]}
    entry = result["result"]
    assert entry.unique_id == head.entity_id
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert cloud_record(coordinator, head.entity_id).generic is True
    assert (coordinator.zones[0].write_interval, coordinator.zones[0].confirm_wait) == (30, 30)


@pytest.mark.parametrize("platform", [GENERIC, MELCLOUD, ESPHOME])
async def test_one_head_without_the_opt_in_is_refused(hass: HomeAssistant, platform: str) -> None:
    (head,) = await _heads(hass, {platform: [RichHead("a")]})
    result = await _pick(hass, [head])
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "need_two_heads"}
    result = await _pick(hass, [head], generic_heads=False)
    assert result["errors"] == {"base": "need_two_heads"}
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    ("cls", "platform", "error"),
    [
        (CoolOnlyHead, GENERIC, "head_missing_heat_cool"),
        (HeatOnlyHead, GENERIC, "head_missing_heat_cool"),
        (NoIdleHead, GENERIC, "head_missing_idle_modes"),
        (RichHead, ESPHOME, "generic_esphome_head"),
    ],
    ids=["cool_only", "heat_only", "no_idle", "esphome"],
)
async def test_the_opt_in_refuses_a_head_it_cannot_drive(
    hass: HomeAssistant, cls: type, platform: str, error: str
) -> None:
    (head,) = await _heads(hass, {platform: [cls("a")]})
    result = await _pick(hass, [head], generic_heads=True)
    assert result["step_id"] == "user"
    assert result["errors"] == {"heads": error}
    assert default(result, "generic_heads") is True  # the answer is kept
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_a_cool_only_head_beside_a_good_one_is_refused(hass: HomeAssistant) -> None:
    heads = await _heads(hass, {GENERIC: [RichHead("a"), CoolOnlyHead("b")]})
    result = await _pick(hass, heads, generic_heads=True)
    assert result["errors"] == {"heads": "head_missing_heat_cool"}
    assert result["description_placeholders"]["unsupported_heads"] == heads[1].entity_id


async def test_the_opt_in_with_several_heads_makes_each_generic(hass: HomeAssistant) -> None:
    """One outdoor unit mapped to several heads: no cloud step, all generic."""
    heads = await _heads(hass, {GENERIC: [RichHead("a")], MELCLOUD: [RichHead("b")]})
    result = await _pick(hass, heads, generic_heads=True)
    result = await _finish(hass, result, 2)
    assert [zone[ZONE_HEAD_TYPE] for zone in result["data"]["zones"]] == [HEAD_TYPE_GENERIC] * 2
    assert result["summary"].count("Head type: generic (beta)") == 2


async def test_taking_the_opt_in_back_refuses_one_head_again(hass: HomeAssistant) -> None:
    (head,) = await _heads(hass, {GENERIC: [RichHead("a")]})
    manager = hass.config_entries.flow
    result = await _pick(hass, [head], generic_heads=True)
    result = await manager.async_configure(result["flow_id"], {})
    result = await manager.async_configure(result["flow_id"], {"sensor_1": SENSORS[0]})
    assert result["step_id"] == "review"
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "user"})
    assert default(result, "generic_heads") is True
    result = await manager.async_configure(
        result["flow_id"], {"heads": [head.entity_id], "generic_heads": False}
    )
    assert result["errors"] == {"base": "need_two_heads"}


async def test_the_cloud_step_offers_generic_but_never_prefills_it(hass: HomeAssistant) -> None:
    heads = await _heads(hass, {ESPHOME: [RichHead("a")], MELCLOUD: [RichHead("b")]})
    result = await _pick(hass, heads)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "head_type"
    assert fields(result) == ["head_type_2"]
    assert default(result, "head_type_2") == HEAD_TYPE_CLOUD
    _, picker = marker_of(result, "head_type_2")
    assert list(picker.config["options"]) == [HEAD_TYPE_LOCAL, HEAD_TYPE_CLOUD, HEAD_TYPE_GENERIC]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"head_type_2": HEAD_TYPE_GENERIC}
    )
    manager = hass.config_entries.flow
    result = await manager.async_configure(
        result["flow_id"], {"sensor_1": SENSORS[0], "sensor_2": SENSORS[1]}
    )
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "finish"})
    zones = result["data"]["zones"]
    assert ZONE_HEAD_TYPE not in zones[0]
    assert zones[1][ZONE_HEAD_TYPE] == HEAD_TYPE_GENERIC


# -- the room page ----------------------------------------------------------


async def _entry(hass: HomeAssistant, rooms: list[tuple[Any, dict]], platform: str = GENERIC):
    hass.config.units = US_CUSTOMARY_SYSTEM
    await register(hass, {platform: [head for head, _ in rooms]})
    return await setup_rooms(hass, rooms, enable=False)


async def test_a_generic_room_page_shows_generic_and_its_own_defaults(hass: HomeAssistant) -> None:
    head = RichHead("a")
    entry = await _entry(hass, [(head, GEN)])
    before = [dict(zone) for zone in entry.data["zones"]]
    result = await open_options_page(hass, entry, "room_1")
    assert default(result, "head_type") == HEAD_TYPE_GENERIC
    assert sections(result)["cloud"].options["collapsed"] is False
    assert (suggested(result, "write_interval"), suggested(result, "confirm_wait")) == (30.0, 30.0)
    result = await submit(hass.config_entries.options, result, prefilled(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data["zones"] == before


async def test_switching_a_room_to_generic_stores_no_cloud_defaults(hass: HomeAssistant) -> None:
    """The page showed 60/90 for a local room; untouched, nothing is stored."""
    a, b = RichHead("a"), RichHead("b")
    entry = await _entry(hass, [(a, {}), (b, {})])
    result = await open_options_page(hass, entry, "room_1")
    values = {**prefilled(result), "head_type": HEAD_TYPE_GENERIC}
    assert (values["write_interval"], values["confirm_wait"]) == (60.0, 90.0)
    await submit(hass.config_entries.options, result, values)
    await hass.async_block_till_done()
    zone = entry.data["zones"][0]
    assert zone[ZONE_HEAD_TYPE] == HEAD_TYPE_GENERIC
    assert not TIMING & set(zone)
    assert (entry.runtime_data.zones[0].write_interval, entry.runtime_data.zones[0].confirm_wait) == (30, 30)

    # A changed box is stored; back to local clears it and the type.
    result = await open_options_page(hass, entry, "room_1")
    await submit(hass.config_entries.options, result, {**prefilled(result), "confirm_wait": 120})
    await hass.async_block_till_done()
    assert entry.data["zones"][0][ZONE_CONFIRM_WAIT] == 120.0
    result = await open_options_page(hass, entry, "room_1")
    await submit(hass.config_entries.options, result, {**prefilled(result), "head_type": HEAD_TYPE_LOCAL})
    await hass.async_block_till_done()
    zone = entry.data["zones"][0]
    assert ZONE_HEAD_TYPE not in zone and not TIMING & set(zone)
    assert entry.runtime_data.heads._clouds == {}


async def test_switching_a_cloud_room_to_generic_stores_no_cloud_timings(hass: HomeAssistant) -> None:
    """The page showed the cloud room's 60/90; untouched, nothing is stored."""
    a, b = MelcloudHead("a"), MelcloudHead("b")
    entry = await _entry(hass, [(a, {ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD}), (b, {})], MELCLOUD)
    result = await open_options_page(hass, entry, "room_1")
    assert default(result, "head_type") == HEAD_TYPE_CLOUD
    values = {**prefilled(result), "head_type": HEAD_TYPE_GENERIC}
    assert (values["write_interval"], values["confirm_wait"]) == (60.0, 90.0)
    await submit(hass.config_entries.options, result, values)
    await hass.async_block_till_done()
    zone = entry.data["zones"][0]
    assert zone[ZONE_HEAD_TYPE] == HEAD_TYPE_GENERIC
    assert not TIMING & set(zone)
    assert cloud_record(entry.runtime_data, a.entity_id).generic is True
    assert (entry.runtime_data.zones[0].write_interval, entry.runtime_data.zones[0].confirm_wait) == (30, 30)


@pytest.mark.parametrize("head_type", [HEAD_TYPE_LOCAL, HEAD_TYPE_CLOUD])
async def test_a_one_head_entry_stays_generic(hass: HomeAssistant, head_type: str) -> None:
    head = RichHead("a")
    entry = await _entry(hass, [(head, GEN)])
    before = [dict(zone) for zone in entry.data["zones"]]
    result = await open_options_page(hass, entry, "room_1")
    result = await submit(hass.config_entries.options, result, {**prefilled(result), "head_type": head_type})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "single_head_needs_generic"}
    assert entry.data["zones"] == before


async def test_a_head_that_turns_cool_only_is_refused_on_the_room_page(hass: HomeAssistant) -> None:
    head = RichHead("a")
    entry = await _entry(hass, [(head, GEN)])
    before = [dict(zone) for zone in entry.data["zones"]]
    result = await open_options_page(hass, entry, "room_1")
    head._attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.FAN_ONLY]
    head.push()
    await hass.async_block_till_done()
    result = await submit(hass.config_entries.options, result, prefilled(result))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "head_missing_heat_cool"}
    assert fields(result) == []
    assert entry.data["zones"] == before
    # And Configure opens on the same refusal until the head is fixed.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "head_missing_heat_cool"}


@pytest.mark.parametrize(
    ("platform", "change", "error"),
    [
        (ESPHOME, {}, "generic_esphome_head"),
        (GENERIC, {"vane_vertical": "select.vane"}, "generic_unsupported_options"),
        (GENERIC, {"dual_setpoint": True}, "generic_unsupported_options"),
        (GENERIC, {"hands_off_vote_basis": "action"}, "generic_unsupported_options"),
    ],
)
async def test_the_room_page_refuses_what_a_generic_head_cannot_do(
    hass: HomeAssistant, platform: str, change: dict, error: str
) -> None:
    hass.states.async_set("select.vane", "auto", {"options": ["auto"]})
    entry = await _entry(hass, [(RichHead("a"), {}), (RichHead("b"), {})], platform)
    before = [dict(zone) for zone in entry.data["zones"]]
    result = await open_options_page(hass, entry, "room_1")
    result = await submit(
        hass.config_entries.options, result,
        {**prefilled(result), "head_type": HEAD_TYPE_GENERIC, **change},
    )
    assert result["errors"] == {"base": error}
    assert entry.data["zones"] == before


# -- Reconfigure ------------------------------------------------------------


async def _reconfigure(hass: HomeAssistant, entry: Any, heads: list[str]) -> dict[str, Any]:
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert "generic_heads" not in fields(result)  # Reconfigure keeps stored types
    return await manager.async_configure(result["flow_id"], {"heads": heads})


async def test_reconfigure_keeps_a_one_head_generic_entry_but_not_a_new_one(
    hass: HomeAssistant,
) -> None:
    head, other = RichHead("a"), RichHead("b")
    hass.config.units = US_CUSTOMARY_SYSTEM
    await register(hass, {GENERIC: [head], "other_ac": [other]})
    entry = await setup_rooms(hass, [(head, GEN)], enable=False)
    result = await _reconfigure(hass, entry, [head.entity_id])
    assert result["step_id"] == "reconfigure_rooms"
    hass.config_entries.flow.async_abort(result["flow_id"])
    # A head new to the entry has no stored type: one alone is refused.
    result = await _reconfigure(hass, entry, [other.entity_id])
    assert result["errors"] == {"base": "need_two_heads"}


async def test_reconfigure_still_refuses_one_local_head(hass: HomeAssistant) -> None:
    a, b = PlainHead("a"), MelcloudHead("b")
    hass.config.units = US_CUSTOMARY_SYSTEM
    await register(hass, {GENERIC: [a], MELCLOUD: [b]})
    entry = await setup_rooms(hass, [(a, {}), (b, {ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD})], enable=False)
    for head in (a, b):
        result = await _reconfigure(hass, entry, [head.entity_id])
        assert result["errors"] == {"base": "need_two_heads"}
