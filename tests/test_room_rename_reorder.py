"""Room renaming and priority reordering keep each room's own settings.

A room's comfort settings live in that room's entities: the target number, the
drift number, the enable switch and the Fan auto hold. Those entities are keyed
by the room's PRIORITY SLOT (``const.zone_slug``: slot 0 is ``primary``, slot 1
``secondary``, then ``zone_N``), so the registry — and with it every restored
value — follows the slot, not the head.

Reordering the head list therefore hands each room the settings of whichever
room used to sit in its new position: the bedroom's 62 °F target lands on the
office, the office's disabled coordination lands on the bedroom, and a manual
fan hold moves to a head nobody touched. Nothing warns at runtime, because
every value is individually plausible.

These tests pin the fix from both sides:

* settings and registry records must follow the ROOM (its head), across a
  rename, a reorder, both together, a removal and an addition;
* a room whose slot did not move must not have its records touched at all, and
  a user's own entity rename must survive a room rename.

"Reconfigure" here is the real config flow, driven to its abort, and the entry
reload it triggers. Restore data travels through the same path the reload uses,
so a value that "survives" here survived a genuine entity teardown and rebuild.
It is not a Home Assistant restart (``test_registry_lifecycle.py`` owns that)
and it is not hardware.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_ZONES,
    DOMAIN,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
)
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    MockHead,
    _eid,
    _recompute,
    _set_fan_auto,
    _set_target,
    _set_temp,
    _user_set_fan,
)

SENSOR_C = "sensor.room_c_temp"

# Every per-room entity suffix, in the order the platforms register them.
ROOM_SUFFIXES = (
    "target", "drift", "drift_follow_global", "enable", "thermostat", "fan_auto",
)


async def _setup_heads(hass: HomeAssistant, count: int) -> list[str]:
    """Register ``count`` mock climate heads and return their entity_ids.

    ``test_drive._setup_mock_heads`` registers exactly two; the removal and
    addition cases need three, so this is the same platform setup widened.
    """
    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.common import (
        MockModule,
        MockPlatform,
        mock_integration,
        mock_platform,
    )

    heads = [MockHead(suffix) for suffix in "abc"[:count]]

    async def _async_setup_platform(
        hass, config, async_add_entities, discovery_info=None
    ):
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(
        hass, "test.climate", MockPlatform(async_setup_platform=_async_setup_platform)
    )
    assert await async_setup_component(
        hass, "climate", {"climate": {"platform": "test"}}
    )
    await hass.async_block_till_done()
    return [head.entity_id for head in heads]


def _zone(name: str, head: str, sensor: str) -> dict[str, str]:
    return {ZONE_NAME: name, ZONE_CLIMATE: head, ZONE_SENSOR: sensor}


async def _setup_rooms(
    hass: HomeAssistant, count: int = 2
) -> tuple[list[str], list[str], MockConfigEntry]:
    """A loaded v2 entry with one named room per head, in priority order."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = await _setup_heads(hass, count)
    sensors = [SENSOR_A, SENSOR_B, SENSOR_C][:count]
    names = ["Bedroom", "Office", "Den"][:count]
    for sensor in sensors:
        await _set_temp(hass, sensor, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        version=2,
        unique_id="|".join(heads),
        data={
            CONF_ZONES: [
                _zone(name, head, sensor)
                for name, head, sensor in zip(names, heads, sensors)
            ],
            CONF_FAN_BOOST_ENABLE: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return heads, sensors, entry


async def _reconfigure(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    heads: list[str],
    sensors: list[str],
    names: list[str] | None = None,
) -> None:
    """Drive the real reconfigure flow to its abort, then let the reload land."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": list(heads)}
    )
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "reconfigure_rooms"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {}
        if names is None
        else {f"room_name_{i + 1}": name for i, name in enumerate(names)},
    )
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "reconfigure_sensors"
    payload: dict[str, Any] = {
        f"sensor_{i + 1}": sensor for i, sensor in enumerate(sensors)
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)
    assert result["type"] is FlowResultType.MENU, result
    assert result["step_id"] == "reconfigure_review"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_finish"}
    )
    assert result["type"] is FlowResultType.ABORT, result
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()


# -- reading a room's settings ---------------------------------------------


def _slug(index: int) -> str:
    return ("primary", "secondary")[index] if index < 2 else f"zone_{index + 1}"


def _room_eid(
    hass: HomeAssistant, entry: MockConfigEntry, index: int, suffix: str
) -> str:
    return _eid(hass, entry, f"_{_slug(index)}_{suffix}")


def _settings(
    hass: HomeAssistant, entry: MockConfigEntry, index: int
) -> dict[str, Any]:
    """Every stored comfort setting of the room now in priority slot ``index``."""
    drift = hass.states.get(_room_eid(hass, entry, index, "drift"))
    coordinator = entry.runtime_data
    zone = coordinator.zones[index]
    return {
        "name": zone.name,
        "head": zone.climate_id,
        "sensor": zone.sensor_id,
        "target": float(hass.states.get(_room_eid(hass, entry, index, "target")).state),
        "enable": hass.states.get(_room_eid(hass, entry, index, "enable")).state,
        "drift": float(drift.state),
        "drift_override": drift.attributes["override"],
        "fan_hold": not coordinator.fan_auto_is_on(zone.climate_id),
    }


async def _make_rooms_distinct(
    hass: HomeAssistant, entry: MockConfigEntry, heads: list[str]
) -> list[dict[str, Any]]:
    """Give every room settings no other room shares, then read them back.

    Distinct on every axis the fix must carry: target, enable, drift override
    and the manual fan hold. A slot transfer is then visible in any one of them.
    """
    for index, head in enumerate(heads):
        await _set_target(
            hass, _room_eid(hass, entry, index, "target"), 62 + index * 4
        )
        if index == 0:
            await hass.services.async_call(
                "switch",
                "turn_on",
                {"entity_id": _room_eid(hass, entry, index, "enable")},
                blocking=True,
            )
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": _room_eid(hass, entry, index, "drift"), "value": 4.0},
                blocking=True,
            )
            # A deliberate manual hold: the user picks a speed on the head, then
            # switches Fan auto OFF so the coordinator stops driving that fan.
            await _user_set_fan(hass, head, "high")
            await _set_fan_auto(hass, _room_eid(hass, entry, index, "fan_auto"), False)
    await hass.async_block_till_done()
    return [_settings(hass, entry, i) for i in range(len(heads))]


def _by_head(rooms: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {room["head"]: room for room in rooms}


def _assert_room_settings_followed_their_heads(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    ignore: tuple[str, ...] = (),
) -> None:
    """Each head's settings must be the ones it had, wherever it now sits."""
    was, now = _by_head(before), _by_head(after)
    for head, room in now.items():
        assert head in was, f"unexpected head {head}"
        for key, value in was[head].items():
            if key in ignore:
                continue
            assert room[key] == value, (
                f"{head}: {key} is {room[key]!r}, was {value!r} — "
                "settings moved with the slot, not the room"
            )


def _records(
    hass: HomeAssistant, entry: MockConfigEntry, heads: list[str]
) -> dict[tuple[str, str], er.RegistryEntry]:
    """Every per-room registry record, keyed by (head, suffix)."""
    coordinator = entry.runtime_data
    reg = er.async_get(hass)
    slot_of = {zone.climate_id: zone.index for zone in coordinator.zones}
    out: dict[tuple[str, str], er.RegistryEntry] = {}
    for head in heads:
        for suffix in ROOM_SUFFIXES:
            eid = _room_eid(hass, entry, slot_of[head], suffix)
            out[(head, suffix)] = reg.async_get(eid)
    return out


# -- the reorder repro ------------------------------------------------------


async def test_reorder_keeps_every_room_setting_with_its_own_room(
    hass: HomeAssistant,
) -> None:
    """Swapping two rooms' priority must not swap their comfort settings."""
    heads, sensors, entry = await _setup_rooms(hass)
    before = await _make_rooms_distinct(hass, entry, heads)
    assert before[0]["target"] == 62.0 and before[1]["target"] == 66.0
    assert before[0]["enable"] == "on" and before[1]["enable"] == "off"
    assert before[0]["drift_override"] is True and before[1]["drift_override"] is False
    assert before[0]["fan_hold"] is True and before[1]["fan_hold"] is False

    await _reconfigure(hass, entry, heads[::-1], sensors[::-1])

    assert [z.climate_id for z in entry.runtime_data.zones] == heads[::-1]
    after = [_settings(hass, entry, i) for i in range(2)]
    _assert_room_settings_followed_their_heads(before, after)


async def test_reorder_keeps_each_room_registry_record(
    hass: HomeAssistant,
) -> None:
    """The record itself — id, entity_id, custom name, area, disabled — moves too.

    M17's guarantee restated for a reorder: a retained room's record is never
    removed and recreated, so the customizations HA cannot reconstruct on
    2024.12 (name, area, disabled_by) stay with the room.
    """
    heads, sensors, entry = await _setup_rooms(hass)
    area = ar.async_get(hass).async_get_or_create("Upstairs")
    reg = er.async_get(hass)
    reg.async_update_entity(
        _room_eid(hass, entry, 0, "target"), name="Bedroom comfort", area_id=area.id
    )
    reg.async_update_entity(
        _room_eid(hass, entry, 0, "drift"),
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    await hass.async_block_till_done()
    before = _records(hass, entry, heads)

    await _reconfigure(hass, entry, heads[::-1], sensors[::-1])

    after = _records(hass, entry, heads)
    for key, was in before.items():
        now = after[key]
        assert now is not None, f"{key}: record is gone"
        assert now.id == was.id, f"{key}: got a new registry id"
        assert now.entity_id == was.entity_id, f"{key}: entity_id moved rooms"
        assert now.name == was.name, f"{key}: lost its custom name"
        assert now.area_id == was.area_id, f"{key}: lost its area"
        assert now.disabled_by == was.disabled_by, f"{key}: lost its disabled flag"


async def test_reorder_moves_the_unique_ids_and_leaves_no_duplicate(
    hass: HomeAssistant,
) -> None:
    """Each room's records carry its NEW slot's unique_ids, exactly once."""
    heads, sensors, entry = await _setup_rooms(hass)
    await _reconfigure(hass, entry, heads[::-1], sensors[::-1])

    reg = er.async_get(hass)
    entities = er.async_entries_for_config_entry(reg, entry.entry_id)
    unique_ids = [ent.unique_id for ent in entities]
    assert len(unique_ids) == len(set(unique_ids)), "duplicate unique_id after reorder"
    # No parked/temporary id survives the save + reload.
    assert not [uid for uid in unique_ids if "reorder" in uid], unique_ids
    for index, head in enumerate(heads[::-1]):
        for suffix in ROOM_SUFFIXES:
            eid = _room_eid(hass, entry, index, suffix)
            assert reg.async_get(eid).unique_id == (
                f"{entry.entry_id}_{_slug(index)}_{suffix}"
            )


async def test_untouched_order_leaves_every_record_alone(
    hass: HomeAssistant,
) -> None:
    """Control: a save that only edits a sensor must move no record at all."""
    heads, sensors, entry = await _setup_rooms(hass)
    before = await _make_rooms_distinct(hass, entry, heads)
    records = _records(hass, entry, heads)

    hass.states.async_set(SENSOR_C, "70", {"unit_of_measurement": "°F"})
    await hass.async_block_till_done()
    await _reconfigure(hass, entry, heads, [SENSOR_C, sensors[1]])

    assert entry.runtime_data.zones[0].sensor_id == SENSOR_C
    after = [_settings(hass, entry, i) for i in range(2)]
    _assert_room_settings_followed_their_heads(before, after, ignore=("sensor",))
    for key, was in records.items():
        now = _records(hass, entry, heads)[key]
        assert now.id == was.id and now.unique_id == was.unique_id, f"{key} churned"


async def test_rotating_three_rooms_carries_every_setting_round_with_them(
    hass: HomeAssistant,
) -> None:
    """A 3-cycle, not a swap: every room lands one slot up, settings included.

    A swap can be got right by accident (two records exchanged in either
    direction look the same from one room's seat). A rotation cannot.
    """
    heads, sensors, entry = await _setup_rooms(hass, 3)
    await _make_rooms_distinct(hass, entry, heads)
    # Distinguish the two rooms _make_rooms_distinct leaves alike.
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _room_eid(hass, entry, 2, "enable")},
        blocking=True,
    )
    await hass.async_block_till_done()
    before = [_settings(hass, entry, i) for i in range(3)]
    assert [room["target"] for room in before] == [62.0, 66.0, 70.0]

    rotated = heads[1:] + heads[:1]
    await _reconfigure(hass, entry, rotated, sensors[1:] + sensors[:1])

    assert [z.climate_id for z in entry.runtime_data.zones] == rotated
    after = [_settings(hass, entry, i) for i in range(3)]
    assert [room["target"] for room in after] == [66.0, 70.0, 62.0]
    _assert_room_settings_followed_their_heads(before, after)


# -- renaming ---------------------------------------------------------------


async def test_reconfigure_offers_a_name_field_per_room_prefilled(
    hass: HomeAssistant,
) -> None:
    """The sensors step shows each room's stored name, ready to edit."""
    heads, _sensors, entry = await _setup_rooms(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": heads}
    )
    suggested = {}
    for marker in result["data_schema"].schema:
        if str(marker.schema).startswith("room_name_"):
            suggested[str(marker.schema)] = marker.description.get("suggested_value")
    assert suggested == {"room_name_1": "Bedroom", "room_name_2": "Office"}


async def test_rename_keeps_the_settings_and_the_registry_identity(
    hass: HomeAssistant,
) -> None:
    """A rename changes the stored name and nothing else about the room."""
    heads, sensors, entry = await _setup_rooms(hass)
    before = await _make_rooms_distinct(hass, entry, heads)
    records = _records(hass, entry, heads)

    await _reconfigure(hass, entry, heads, sensors, names=["Nursery", "Study"])

    assert [z[ZONE_NAME] for z in entry.data[CONF_ZONES]] == ["Nursery", "Study"]
    assert [z.name for z in entry.runtime_data.zones] == ["Nursery", "Study"]
    after = [_settings(hass, entry, i) for i in range(2)]
    _assert_room_settings_followed_their_heads(before, after, ignore=("name",))
    for key, was in records.items():
        now = _records(hass, entry, heads)[key]
        assert now.id == was.id, f"{key}: rename churned the registry id"
        assert now.unique_id == was.unique_id, f"{key}: rename changed a unique_id"
        assert now.entity_id == was.entity_id, f"{key}: rename moved an entity_id"
    # The default display name follows the new room name.
    assert (
        hass.states.get(_room_eid(hass, entry, 0, "target")).attributes["friendly_name"]
        == "MXZ Coordinator Nursery target"
    )


async def test_rename_respects_a_user_renamed_entity(hass: HomeAssistant) -> None:
    """A name the user typed into the entity settings outranks the room name."""
    heads, sensors, entry = await _setup_rooms(hass)
    reg = er.async_get(hass)
    target_eid = _room_eid(hass, entry, 0, "target")
    reg.async_update_entity(target_eid, name="Kids' comfort")
    await hass.async_block_till_done()

    await _reconfigure(hass, entry, heads, sensors, names=["Nursery", "Study"])

    assert reg.async_get(target_eid).name == "Kids' comfort"
    assert (
        hass.states.get(target_eid).attributes["friendly_name"] == "Kids' comfort"
    )
    # The room the user did NOT rename by hand still follows its new room name.
    assert (
        hass.states.get(_room_eid(hass, entry, 1, "target")).attributes[
            "friendly_name"
        ]
        == "MXZ Coordinator Study target"
    )


async def test_blank_name_falls_back_to_the_head_name(hass: HomeAssistant) -> None:
    """Clearing the field resets the room to the head's own name."""
    heads, sensors, entry = await _setup_rooms(hass)

    await _reconfigure(hass, entry, heads, sensors, names=["  ", "Study"])

    assert [z[ZONE_NAME] for z in entry.data[CONF_ZONES]] == ["Mock Head a", "Study"]


async def test_a_submission_without_name_fields_keeps_the_stored_names(
    hass: HomeAssistant,
) -> None:
    """An absent name key is not a cleared name.

    The vane and stage overrides read an absent key as "the user cleared it",
    because every one of those fields is rendered on every submission. A room
    name is not safe to treat that way: a submission that never mentions the
    room would silently rename it after the fact.
    """
    heads, sensors, entry = await _setup_rooms(hass)

    await _reconfigure(hass, entry, heads, sensors, names=None)

    assert [z[ZONE_NAME] for z in entry.data[CONF_ZONES]] == ["Bedroom", "Office"]


async def test_rename_and_reorder_together(hass: HomeAssistant) -> None:
    """Both at once: each head keeps its settings AND takes its new name."""
    heads, sensors, entry = await _setup_rooms(hass)
    before = await _make_rooms_distinct(hass, entry, heads)

    await _reconfigure(
        hass, entry, heads[::-1], sensors[::-1], names=["Study", "Nursery"]
    )

    after = [_settings(hass, entry, i) for i in range(2)]
    _assert_room_settings_followed_their_heads(before, after, ignore=("name",))
    assert _by_head(after)[heads[0]]["name"] == "Nursery"
    assert _by_head(after)[heads[1]]["name"] == "Study"


# -- removing and adding a room ---------------------------------------------


async def test_removed_room_is_pruned_and_the_survivors_keep_their_settings(
    hass: HomeAssistant,
) -> None:
    """Dropping the top-priority room must not shuffle the other two's settings."""
    heads, sensors, entry = await _setup_rooms(hass, 3)
    before = await _make_rooms_distinct(hass, entry, heads)
    # Give the two survivors a hold and an override of their own, so the
    # promotion has something to lose.
    await _set_fan_auto(hass, _room_eid(hass, entry, 2, "fan_auto"), False)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _room_eid(hass, entry, 1, "drift"), "value": 3.0},
        blocking=True,
    )
    await hass.async_block_till_done()
    before = [_settings(hass, entry, i) for i in range(3)]
    dropped_eids = [_room_eid(hass, entry, 0, s) for s in ROOM_SUFFIXES]

    await _reconfigure(hass, entry, heads[1:], sensors[1:])

    assert [z.climate_id for z in entry.runtime_data.zones] == heads[1:]
    after = [_settings(hass, entry, i) for i in range(2)]
    _assert_room_settings_followed_their_heads(before[1:], after)
    reg = er.async_get(hass)
    for eid in dropped_eids:
        assert reg.async_get(eid) is None, f"{eid} survived its room's removal"
    remaining = er.async_entries_for_config_entry(reg, entry.entry_id)
    assert len(remaining) == 2 * len(ROOM_SUFFIXES) + 6


async def test_added_room_starts_fresh_and_the_others_keep_their_settings(
    hass: HomeAssistant,
) -> None:
    """A new room appended at the bottom disturbs nobody above it."""
    heads = await _setup_heads(hass, 3)
    hass.config.units = US_CUSTOMARY_SYSTEM
    for sensor in (SENSOR_A, SENSOR_B, SENSOR_C):
        await _set_temp(hass, sensor, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        version=2,
        unique_id="|".join(heads[:2]),
        data={
            CONF_ZONES: [
                _zone("Bedroom", heads[0], SENSOR_A),
                _zone("Office", heads[1], SENSOR_B),
            ],
            CONF_FAN_BOOST_ENABLE: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = await _make_rooms_distinct(hass, entry, heads[:2])

    await _reconfigure(
        hass, entry, heads, [SENSOR_A, SENSOR_B, SENSOR_C], names=None
    )

    after = [_settings(hass, entry, i) for i in range(3)]
    _assert_room_settings_followed_their_heads(before, after[:2])
    assert after[2]["head"] == heads[2]
    assert after[2]["enable"] == "off"
    assert after[2]["drift_override"] is False
    assert after[2]["fan_hold"] is False
    assert after[2]["name"] == "Mock Head c"


async def test_promotion_after_a_removal_does_not_inherit_the_dropped_room(
    hass: HomeAssistant,
) -> None:
    """The promoted room must not pick up the removed room's target or hold.

    This is the collision case: the dropped room's records still hold the
    ``primary`` unique_ids when the room below it is promoted into that slot.
    """
    heads, sensors, entry = await _setup_rooms(hass, 3)
    await _make_rooms_distinct(hass, entry, heads)
    promoted = _settings(hass, entry, 1)

    await _reconfigure(hass, entry, heads[1:], sensors[1:])

    now = _settings(hass, entry, 0)
    assert now["head"] == heads[1]
    assert now["target"] == promoted["target"] == 66.0
    assert now["enable"] == "off"
    assert now["drift_override"] is False
    assert now["fan_hold"] is False


# -- an existing stored entry -----------------------------------------------


async def test_stored_entry_loads_unchanged_and_keeps_its_settings(
    hass: HomeAssistant,
) -> None:
    """A pinned-shape entry loads, keeps its data, and reloads with its values."""
    heads, _sensors, entry = await _setup_rooms(hass)
    stored_data = dict(entry.data)
    stored_options = dict(entry.options)
    before = await _make_rooms_distinct(hass, entry, heads)
    records = _records(hass, entry, heads)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert dict(entry.data) == stored_data
    assert dict(entry.options) == stored_options
    assert entry.version == 2
    after = [_settings(hass, entry, i) for i in range(2)]
    _assert_room_settings_followed_their_heads(before, after)
    for key, was in records.items():
        now = _records(hass, entry, heads)[key]
        assert now.id == was.id and now.unique_id == was.unique_id, f"{key} churned"


async def test_legacy_flat_entry_migrates_and_still_reconfigures(
    hass: HomeAssistant,
) -> None:
    """A v1 entry migrates to zones, then renames and reorders like any other."""
    from custom_components.mxz_coordinator.const import (
        CONF_PRIMARY_CLIMATE,
        CONF_PRIMARY_SENSOR,
        CONF_SECONDARY_CLIMATE,
        CONF_SECONDARY_SENSOR,
    )

    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = await _setup_heads(hass, 2)
    for sensor in (SENSOR_A, SENSOR_B):
        await _set_temp(hass, sensor, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        version=1,
        data={
            CONF_PRIMARY_CLIMATE: heads[0],
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_CLIMATE: heads[1],
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 2
    assert [z[ZONE_NAME] for z in entry.data[CONF_ZONES]] == ["Primary", "Secondary"]
    before = await _make_rooms_distinct(hass, entry, heads)

    await _reconfigure(
        hass, entry, heads[::-1], [SENSOR_B, SENSOR_A], names=["Study", "Nursery"]
    )

    after = [_settings(hass, entry, i) for i in range(2)]
    _assert_room_settings_followed_their_heads(before, after, ignore=("name",))
    assert _by_head(after)[heads[0]]["name"] == "Nursery"


# -- the plan still reads by priority ---------------------------------------


async def test_plan_attributes_still_describe_the_priority_order(
    hass: HomeAssistant,
) -> None:
    """Slot-keyed plan attributes keep meaning priority, not a fixed room."""
    heads, sensors, entry = await _setup_rooms(hass)
    await _make_rooms_distinct(hass, entry, heads)
    await _reconfigure(hass, entry, heads[::-1], sensors[::-1])
    await _recompute(hass, entry)

    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert [zone["name"] for zone in plan.attributes["zones"]] == ["Office", "Bedroom"]
    # Each slot reports the settings of the room now in it, not the ones the
    # slot used to hold: the Office keeps 66 at position 1, the Bedroom 62.
    assert [zone["target"] for zone in plan.attributes["zones"]] == [66.0, 62.0]
    assert [zone["enabled"] for zone in plan.attributes["zones"]] == [False, True]
    assert [zone["drift"] for zone in plan.attributes["zones"]] == [
        entry.runtime_data.engage_deadband,
        4.0,
    ]
    # The Fan-auto switch is the user-visible hold, and it moved with the room.
    assert hass.states.get(_room_eid(hass, entry, 0, "fan_auto")).state == "on"
    assert hass.states.get(_room_eid(hass, entry, 1, "fan_auto")).state == "off"
