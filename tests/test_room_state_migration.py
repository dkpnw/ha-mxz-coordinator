"""Moving to the room state store: from a 3.22.0 install, with old data missing or corrupt, and back.

``fixtures/room_state/saved_by_3_22_0.json`` is what 3.22.0 itself saved at a clean stop
(``gen_saved_by_3_22_0.py``, run on tag v3.22.0): every entity's restore data and the entry's
mode-change and door/window files, and no room state store. The first start of this release
reads all of it into the store. While this release runs it keeps the old channels current, so
3.22.0 finds its own data after a rollback, and a start after that takes whatever 3.22.0
changed. Rooms, heads, sensors and the window are invented.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.helpers import restore_state as rs
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.mxz_coordinator.const import DOMAIN, MODE_HEAT
from tests.cloud_heads import register
from tests.ha_test import MockConfigEntry, async_mock_load_restore_state_from_storage
from tests.property_rig import _HEADS, SENSORS, WINDOW, Rig, RigHead, open_rig
from tests.restart import stopped
from tests.restore_store import (
    find_stored_state,
    replace_stored_state,
    set_stored_states,
)
from tests.test_drive import _set_temp

SAVED = json.loads((Path(__file__).parent / "fixtures/room_state/saved_by_3_22_0.json").read_text())
ENTRY = SAVED["entry"]
STORE = f"{DOMAIN}.{ENTRY['entry_id']}.room_state"
MODE_FILE = f"{DOMAIN}.{ENTRY['entry_id']}.mode_change"
OPENINGS_FILE = f"{DOMAIN}.{ENTRY['entry_id']}.openings"
RESTORE = "core.restore_state"
HEAD_A, HEAD_B = "climate.property_head_a", "climate.property_head_b"


async def _start(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Home Assistant starts with the entry, as tests/restart.py starts it."""
    hass.set_state(CoreState.not_running)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.set_state(CoreState.starting)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()


async def _start_saved_install(
    hass: HomeAssistant, freezer: Any, hass_storage: dict, storage: dict[str, Any]
) -> Rig:
    """The 3.22.0 house as it stood at the stop, started on this release with ``storage``."""
    freezer.move_to("2026-11-13 19:00:00+00:00")
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads: list[RigHead] | None = hass.data.get(_HEADS)
    if heads is None:
        heads = [RigHead("a"), RigHead("b")]
        await register(hass, {"esphome": heads})
        hass.data[_HEADS] = heads
    for head in heads:
        head.reset()
        saved = SAVED["heads"][head.entity_id]
        head.person_mode(saved["state"])
        head.person_fan(saved["fan_mode"])
    await _set_temp(hass, SENSORS[0], 70)
    await _set_temp(hass, SENSORS[1], 45)
    hass.states.async_set(WINDOW, "on", {"friendly_name": "Property window"})
    hass_storage.update(deepcopy(storage))
    await async_mock_load_restore_state_from_storage(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title=ENTRY["title"], data=ENTRY["data"], options=ENTRY["options"],
        version=ENTRY["version"], minor_version=ENTRY["minor_version"], entry_id=ENTRY["entry_id"],
    )
    entry.add_to_hass(hass)
    entry.created_at = datetime.fromisoformat(ENTRY["created_at"])
    await _start(hass, entry)
    rig = Rig(hass, freezer, entry, heads)
    await rig.later(0)  # what is due now runs: under the frozen clock it waits for time to fire
    return rig


def _saved(*leave_out: str) -> dict[str, Any]:
    return deepcopy({key: value for key, value in SAVED["storage"].items() if key not in leave_out})


def _as_3_22_0_left_it(rig: Rig) -> None:
    """Every value the fixture's docstring names, as a person and the heads see it."""
    study, den = rig.row(0), rig.row(1)
    assert (study["target"], study["drift"], study["enabled"]) == (72, 3, True)
    assert rig.hass.states.get(rig.eid("_primary_drift")).attributes["override"] is True
    assert (den["enabled"], den["target_low"], den["target_high"]) == (False, 66, 76)
    assert den["floor_state"] == "heat" and rig.heads[1].mode == "heat"
    assert rig.coord.eco_idle is True and rig.coord.coordinator_enable is True
    assert rig.coord.current_shared_mode == MODE_HEAT
    assert rig.coord.openings.state_of("primary") == "paused" and rig.heads[0].mode == "off"
    assert rig.fan_held(0) and rig.heads[0].fan == "high"


async def test_a_3_22_0_install_comes_across_whole_and_the_store_alone_then_holds_it(
    hass: HomeAssistant, freezer: Any, hass_storage: dict
) -> None:
    rig = await _start_saved_install(hass, freezer, hass_storage, _saved())
    # 3.22.0's last real mode change, about an hour before the start: its hold has run out.
    changed_at = SAVED["storage"][MODE_FILE]["data"]["last_mode_change"]
    try:
        _as_3_22_0_left_it(rig)
        assert (rig.coord._last_mode_change_at, rig.coord.mode_hold_remaining()) == (changed_at, 0.0)
        assert STORE in hass_storage
        async with stopped(hass, rig.entry):
            set_stored_states(hass, {})  # every restore record lost
            del hass_storage[MODE_FILE], hass_storage[OPENINGS_FILE]
        await rig.settle()
        _as_3_22_0_left_it(rig)
        # The mode record came from the store alone: the change is not restamped and nothing is held.
        assert (rig.coord._last_mode_change_at, rig.coord.mode_hold_remaining()) == (changed_at, 0.0)
        await rig.window("off")
        await rig.later(200)
        assert rig.coord.openings.state_of("primary") == "closed"
        assert rig.fan_held(0) and rig.heads[0].fan == "high"  # the person's pick, given back
    finally:
        await rig.close()


async def test_the_manual_fan_pick_survives_the_migration_a_reload_and_a_restart(
    hass: HomeAssistant, freezer: Any, hass_storage: dict
) -> None:
    rig = await _start_saved_install(hass, freezer, hass_storage, _saved())
    try:
        await rig.window("off")
        await rig.later(200)
        writes = len(rig.heads[0].fan_writes)
        for start in (rig.reload, rig.restart, rig.restart):
            await start()
            await rig.temp(0, 76)  # a room boost would answer with a speed of its own
            assert rig.fan_held(0) and rig.heads[0].fan == "high"
        assert rig.heads[0].fan_writes[writes:] == []
    finally:
        await rig.close()


@pytest.mark.parametrize(
    ("leave_out", "kept"),
    [
        ((RESTORE,), "mode and pause, from their files"),
        ((MODE_FILE, OPENINGS_FILE), "every room value, from restore data"),
    ],
    ids=["no_restore_data", "no_store_files"],
)
async def test_a_migration_with_an_old_source_missing_takes_the_rest(
    hass: HomeAssistant, freezer: Any, hass_storage: dict, leave_out: tuple[str, ...], kept: str
) -> None:
    rig = await _start_saved_install(hass, freezer, hass_storage, _saved(*leave_out))
    try:
        assert STORE in hass_storage
        if RESTORE in leave_out:
            assert rig.coord.current_shared_mode == MODE_HEAT
            assert rig.coord.openings.state_of("primary") == "paused"
            assert rig.row(0)["target"] == 70  # the head's setpoint seeds it, as on a fresh install
            assert rig.coord.coordinator_enable is False
        else:
            assert rig.coord.current_shared_mode == MODE_HEAT  # the select's restore data
            assert rig.row(0)["target"] == 72 and rig.fan_held(0)
            assert rig.row(1)["target_low"] == 66
            # The pause began again: its record was in the missing file.
            assert rig.coord.openings.state_of("primary") in ("opening", "paused")
    finally:
        await rig.close()


async def test_a_migration_with_corrupt_old_sources_reads_each_as_before(
    hass: HomeAssistant, freezer: Any, hass_storage: dict
) -> None:
    """Unreadable old data is read as 3.22.0 read it, and the readable rest still comes across."""
    storage = _saved()
    storage[MODE_FILE] = {**storage[MODE_FILE], "data": ["not", "a", "record"]}
    records = storage[RESTORE]["data"]
    fan = next(r for r in records if r["state"]["entity_id"].endswith("study_fan_auto"))
    fan["state"]["state"], fan["state"]["attributes"], fan["extra_data"] = "unavailable", {}, None
    target = next(r for r in records if r["state"]["entity_id"].endswith("study_target"))
    target["extra_data"] = None
    rig = await _start_saved_install(hass, freezer, hass_storage, storage)
    try:
        assert rig.coord.mode_hold_remaining() > 0  # an unreadable mode record holds the full window
        assert "missing or malformed" in " ".join(r["reason"] for r in rig.row(0)["control_reasons"])
        assert rig.row(0)["target"] == 70  # no usable number data: the head's setpoint
        assert rig.row(0)["drift"] == 3 and rig.row(1)["target_low"] == 66
        assert rig.coord.eco_idle is True
    finally:
        await rig.close()


@pytest.mark.parametrize(
    "store",
    [
        {"version": 1, "data": {"rooms": "not a section", "fan": {HEAD_A: {"held": "yes"}}}},
        {"version": 1, "data": ["not", "a", "store"]},
        {"version": 2, "data": {"rooms": {HEAD_A: {"target": 60}}}},
    ],
    ids=["corrupt_sections", "not_a_record", "from_a_later_release"],
)
async def test_a_store_that_cant_be_read_falls_back_to_the_old_sources(
    hass: HomeAssistant, freezer: Any, hass_storage: dict, store: dict[str, Any]
) -> None:
    rig = await _start_saved_install(hass, freezer, hass_storage, {**_saved(), STORE: store})
    try:
        _as_3_22_0_left_it(rig)
        later_release = store["version"] == 2
        assert (hass_storage[STORE] == store) is later_release  # a later release's store is left as it is
    finally:
        await rig.close()


async def test_what_this_release_leaves_reads_as_3_22_0_reads_it(
    hass: HomeAssistant, freezer: Any, hass_storage: dict
) -> None:
    """Rollback: the old channels hold the current values, in 3.22.0's shapes.

    The same check ran on 3.22.0 itself (rollback_check_on_3_22_0.py, PR description).
    """
    rig = await _start_saved_install(hass, freezer, hass_storage, _saved())
    try:
        await rig.later(60)
        await rig.hass.services.async_call(
            "number", "set_value", {"entity_id": rig.eid("_primary_target"), "value": 73}, blocking=True
        )
        await rig.select_mode("cool")
        await rig.window("off")
        await rig.later(200)
        async with stopped(hass, rig.entry):
            records = {r["state"]["entity_id"]: r for r in hass_storage[RESTORE]["data"]}
            target = records[rig.eid("_primary_target")]
            assert target["extra_data"]["native_value"] == 73
            assert target["extra_data"]["native_unit_of_measurement"] == "°F"
            fan = records[rig.eid("_primary_fan_auto")]
            assert fan["state"]["state"] == "off" and fan["extra_data"]["held"] is True
            assert fan["state"]["attributes"]["last_fan_command"] == "high"
            den = records[rig.eid("_secondary_enable")]
            assert den["state"]["state"] == "off" and den["extra_data"]["floor_run"]["direction"] == "heat"
            assert records[rig.eid("_eco_idle")]["state"]["state"] == "on"
            assert hass_storage[MODE_FILE]["data"]["mode"] == "cool"
            assert hass_storage[OPENINGS_FILE]["data"]["rooms"][HEAD_A]["state"] == "closed"
    finally:
        await rig.close()


async def test_after_a_rollback_a_start_takes_what_the_earlier_release_changed(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Roll-forward: newer restore data wins, older or stale restore data never does."""
    rig = await open_rig(hass, freezer)
    try:
        await rig.later(10)
        saved_at = rig.now()
        await rig.hass.services.async_call(
            "number", "set_value", {"entity_id": rig.eid("_primary_target"), "value": 74}, blocking=True
        )
        await rig.later(60)
        target, other = rig.eid("_primary_target"), rig.eid("_secondary_target")
        async with stopped(hass, rig.entry):
            # The earlier release ran after this one and moved the Study's target to 66.
            _plant_number(hass, target, 66, datetime.fromtimestamp(rig.now() + 1, UTC))
            # A record from before this release's last save loses to it.
            _plant_number(hass, other, 60, datetime.fromtimestamp(saved_at - 5, UTC))
        await rig.settle()
        assert rig.row(0)["target"] == 66
        assert rig.row(1)["target"] == 70
        async with stopped(hass, rig.entry):
            _plant_number(hass, target, 61, rig.entry.created_at - timedelta(seconds=1))
        await rig.settle()
        assert rig.row(0)["target"] == 66
    finally:
        await rig.close()


def _plant_number(hass: HomeAssistant, entity_id: str, value: float, when: datetime) -> None:
    stored = find_stored_state(hass, entity_id)
    assert stored is not None
    extra = {**stored.extra_data.as_dict(), "native_value": value}
    replace_stored_state(hass, entity_id, rs.StoredState(
        State(entity_id, str(value), stored.state.attributes, last_changed=when, last_updated=when),
        rs.RestoredExtraData(extra), when,
    ))
