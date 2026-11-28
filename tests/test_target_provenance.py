"""Where each room's target came from, and when (Discussion #166, Ask 3).

A room's plan row shows ``target_source`` and ``target_set_at``: ``"head"`` for a change made
at the head and adopted by Accept changes made at the head, ``"home_assistant"`` for a write
through MXZ's number or room thermostat entities, and None while no write is on record. The
time is the write's, as ISO 8601 UTC to the second. Both are kept in the room state store, so
a restart or a reload brings them back, and nothing expires them. They are metadata only:
nothing MXZ sends a head changes because of them.

Every room, head, sensor and value is invented; nothing here is a hardware result.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import restore_state as rs
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util

from custom_components.mxz_coordinator.const import ADOPT_DELAY, ZONE_DUAL_SETPOINT
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from custom_components.mxz_coordinator.number import MXZRangeTargetNumber
from custom_components.mxz_coordinator.resume import (
    SECTION_ROOMS,
    RoomSlot,
    SavedState,
    resume,
)
from custom_components.mxz_coordinator.room_state import _room_state_store
from tests.cloud_heads import recompute, record, register, row, setup_rooms, writes
from tests.ha_test import async_fire_time_changed
from tests.property_rig import TARGET, Rig, RigHead, open_rig
from tests.restart import stopped
from tests.restore_store import replace_stored_state
from tests.room_state_store import saved_section
from tests.test_drive import _set_target

HOT = TARGET + 5
HELD = ADOPT_DELAY + 1
HOURS = 6 * 3600


def _iso(stamp: float) -> str:
    """The plan row's form of a wall-clock time, worked out here rather than read from MXZ."""
    return datetime.fromtimestamp(stamp, tz=UTC).isoformat(timespec="seconds")


def _provenance(rig: Rig, room: int = 0) -> tuple[Any, Any]:
    row = rig.row(room)
    return row["target_source"], row["target_set_at"]


async def _saved(rig: Rig, room: int = 0) -> dict[str, Any]:
    return (await saved_section(rig.hass, rig.entry, SECTION_ROOMS))[rig.heads[room].entity_id]


async def _cooling(hass: HomeAssistant, freezer: Any, **kwargs: Any) -> Rig:
    """The first room is hot, so its head cools; both targets were set through Home Assistant."""
    rig = await open_rig(hass, freezer, temps=(HOT, TARGET), **kwargs)
    assert rig.heads[0].mode == "cool"
    return rig


async def _press_and_adopt(rig: Rig, *, low: float, high: float) -> float:
    """A person at the first head's remote; the hold runs out and MXZ adopts it. Returns when."""
    rig.heads[0].person_setpoint(low=low, high=high)
    await rig.hass.async_block_till_done()
    await rig.later(HELD)
    return rig.now()


async def _thermostat(rig: Rig, **data: Any) -> None:
    await rig.hass.services.async_call(
        "climate", "set_temperature", {"entity_id": rig.eid("_primary_thermostat"), **data},
        blocking=True,
    )
    await rig.hass.async_block_till_done()
    await rig.settle()


async def _number(rig: Rig, suffix: str, value: float) -> None:
    await _set_target(rig.hass, rig.eid(suffix), value)
    await rig.settle()


# ---------------------------------------------------------------------------
# What each writer records
# ---------------------------------------------------------------------------


async def test_a_target_nobody_has_set_has_no_source(hass: HomeAssistant) -> None:
    """A fresh install's target is seeded from the head, not set: no source, no time."""
    heads = [RigHead("p"), RigHead("q")]
    await register(hass, {"esphome": heads})
    entry = await setup_rooms(hass, [(heads[0], {}), (heads[1], {})])
    await recompute(hass, entry)
    for index in (0, 1):
        assert (row(hass, entry, index)["target_source"], row(hass, entry, index)["target_set_at"]) == (
            None, None,
        )


async def test_a_target_number_write_is_home_assistant_at_its_time(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    rig.freezer.tick(timedelta(seconds=40))
    when = rig.now()
    await _number(rig, "_primary_target", 72)
    assert rig.row()["target"] == 72
    assert _provenance(rig) == ("home_assistant", _iso(when))
    # The other room keeps what it had: the write at the rig's start, 40 s earlier.
    assert _provenance(rig, 1) == ("home_assistant", _iso(when - 40))
    await rig.close()


async def test_a_room_thermostat_write_is_home_assistant(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    rig.freezer.tick(timedelta(seconds=30))
    await _thermostat(rig, temperature=73)
    assert rig.row()["target"] == 73
    assert _provenance(rig) == ("home_assistant", _iso(rig.now()))
    await rig.close()


async def test_a_dual_rooms_range_writes_are_home_assistant(hass: HomeAssistant, freezer: Any) -> None:
    """The thermostat's pair, Heat to, Cool to and the target number that moves the range."""
    rig = await _cooling(hass, freezer, first_room={ZONE_DUAL_SETPOINT: True})
    steps = (
        lambda: _thermostat(rig, target_temp_low=66, target_temp_high=74),
        lambda: _number(rig, "_primary_target_low", 65),
        lambda: _number(rig, "_primary_target_high", 75),
        lambda: _number(rig, "_primary_target", 71),
    )
    for step in steps:
        rig.freezer.tick(timedelta(seconds=17))
        await step()
        assert _provenance(rig) == ("home_assistant", _iso(rig.now()))
    assert (rig.row()["target_low"], rig.row()["target_high"]) == (66, 76)
    await rig.close()


async def test_a_refused_write_records_nothing(hass: HomeAssistant, freezer: Any) -> None:
    """A pair closer than the minimum gap is refused; the room keeps its source and time."""
    rig = await _cooling(hass, freezer, first_room={ZONE_DUAL_SETPOINT: True})
    before = _provenance(rig)
    rig.freezer.tick(timedelta(seconds=30))
    with pytest.raises(HomeAssistantError):
        await rig.hass.services.async_call(
            "climate", "set_temperature",
            {"entity_id": rig.eid("_primary_thermostat"), "target_temp_low": 70, "target_temp_high": 70},
            blocking=True,
        )
    await rig.settle()
    assert _provenance(rig) == before
    await rig.close()


async def test_a_press_at_the_head_is_head_at_the_time_it_was_adopted(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    adopted = await _press_and_adopt(rig, low=64, high=66)
    assert rig.row()["target"] == 66
    assert _provenance(rig) == ("head", _iso(adopted))
    # The other room wasn't touched at its head.
    assert _provenance(rig, 1)[0] == "home_assistant"
    await rig.close()


async def test_a_press_at_a_dual_rooms_head_is_head(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer, first_room={ZONE_DUAL_SETPOINT: True})
    adopted = await _press_and_adopt(rig, low=63, high=67)
    assert rig.row()["target_high"] == 67
    assert _provenance(rig) == ("head", _iso(adopted))
    await rig.close()


async def test_a_write_after_an_adopted_press_is_home_assistant_again(hass: HomeAssistant, freezer: Any) -> None:
    """The mark for an adopted press is spent by that write: the next one is a person's in HA."""
    rig = await _cooling(hass, freezer)
    await _press_and_adopt(rig, low=64, high=66)
    rig.freezer.tick(timedelta(seconds=90))
    await _number(rig, "_primary_target", 69)
    assert _provenance(rig) == ("home_assistant", _iso(rig.now()))
    await rig.close()


async def test_a_refused_adopt_leaves_no_head_mark_behind(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adopt whose number write never lands records nothing, and the next write isn't "head"."""
    rig = await _cooling(hass, freezer)
    before = _provenance(rig)

    async def refuse(self: MXZRangeTargetNumber, value: float) -> None:
        raise HomeAssistantError("refused")

    monkeypatch.setattr(MXZRangeTargetNumber, "async_set_native_value", refuse)
    await _press_and_adopt(rig, low=64, high=66)
    monkeypatch.undo()
    assert _provenance(rig) == before
    rig.freezer.tick(timedelta(seconds=5))
    await _number(rig, "_primary_target", 68)
    assert _provenance(rig) == ("home_assistant", _iso(rig.now()))
    await rig.close()


# ---------------------------------------------------------------------------
# Kept across a restart and a reload; nothing expires it
# ---------------------------------------------------------------------------


async def test_the_store_keeps_the_source_and_the_epoch(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    adopted = await _press_and_adopt(rig, low=64, high=66)
    saved = await _saved(rig)
    assert (saved["target_source"], saved["target_set_at"]) == ("head", adopted)
    await rig.close()


@pytest.mark.parametrize("crash", [False, True])
async def test_a_restart_brings_the_source_back(hass: HomeAssistant, freezer: Any, crash: bool) -> None:
    rig = await _cooling(hass, freezer)
    adopted = await _press_and_adopt(rig, low=64, high=66)
    # The store's write has reached the disk; only one still on its way is lost to a crash.
    async_fire_time_changed(rig.hass, dt_util.utcnow())
    await rig.hass.async_block_till_done()
    await rig.restart(crash=crash)
    assert rig.row()["target"] == 66
    assert _provenance(rig) == ("head", _iso(adopted))
    await rig.close()


async def test_a_reload_brings_the_source_back(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer, first_room={ZONE_DUAL_SETPOINT: True})
    adopted = await _press_and_adopt(rig, low=63, high=67)
    await rig.reload()
    assert _provenance(rig) == ("head", _iso(adopted))
    await rig.close()


async def test_nothing_expires_or_reverts_it(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    adopted = await _press_and_adopt(rig, low=64, high=66)
    for _ in range(4):
        await rig.later(HOURS)
    assert rig.row()["target"] == 66
    assert _provenance(rig) == ("head", _iso(adopted))
    await rig.close()


async def _edit_store_while_down(rig: Rig, room: dict[str, Any]) -> None:
    """Home Assistant restarts with the first room's stored record changed by ``room``."""
    async with stopped(rig.hass, rig.entry):
        store = _room_state_store(rig.hass, rig.entry.entry_id)
        data = await store.async_load()
        stored = data[SECTION_ROOMS][rig.heads[0].entity_id]
        for key, value in room.items():
            if value is _GONE:
                stored.pop(key, None)
            else:
                stored[key] = value
        await store.async_save(data)
    await rig.settle()


_GONE = object()


async def test_a_store_from_before_this_release_has_no_source(hass: HomeAssistant, freezer: Any) -> None:
    """No keys, as every store an earlier release wrote: the target comes back, the source is unknown."""
    rig = await _cooling(hass, freezer)
    await _press_and_adopt(rig, low=64, high=66)
    await _edit_store_while_down(rig, {"target_source": _GONE, "target_set_at": _GONE})
    assert rig.row()["target"] == 66
    assert _provenance(rig) == (None, None)
    await rig.close()


@pytest.mark.parametrize(
    "stored",
    [
        {"target_source": "someone", "target_set_at": 1.0e9},
        {"target_source": "head", "target_set_at": "noon"},
        {"target_source": "head", "target_set_at": _GONE},
        {"target_source": _GONE, "target_set_at": 1.0e9},
    ],
    ids=["unknown_source", "time_not_a_number", "no_time", "no_source"],
)
async def test_a_store_record_that_cant_be_read_has_no_source(
    hass: HomeAssistant, freezer: Any, stored: dict[str, Any],
) -> None:
    rig = await _cooling(hass, freezer)
    await _press_and_adopt(rig, low=64, high=66)
    await _edit_store_while_down(rig, stored)
    assert _provenance(rig) == (None, None)
    await rig.close()


def _target_record(value: float) -> dict[str, Any]:
    return {
        "native_max_value": 90.0, "native_min_value": 50.0, "native_step": 1.0,
        "native_unit_of_measurement": "°F", "native_value": value,
    }


async def _plant_target(rig: Rig, value: float, when: datetime) -> None:
    entity_id = rig.eid("_primary_target")
    replace_stored_state(
        rig.hass,
        entity_id,
        rs.StoredState(
            State(entity_id, str(value), {}, last_changed=when, last_updated=when),
            rs.RestoredExtraData(_target_record(value)),
            when,
        ),
    )


async def test_newer_restore_data_with_another_target_has_no_source(hass: HomeAssistant, freezer: Any) -> None:
    """A release that keeps no source ran since (a rollback): its target wins, and its source is unknown."""
    rig = await _cooling(hass, freezer)
    await _press_and_adopt(rig, low=64, high=66)
    async with stopped(rig.hass, rig.entry):
        rig.freezer.tick(timedelta(seconds=30))
        await _plant_target(rig, 74.0, dt_util.utcnow())
    await rig.settle()
    assert rig.row()["target"] == 74
    assert _provenance(rig) == (None, None)
    await rig.close()


async def test_newer_restore_data_with_the_same_target_keeps_the_source(hass: HomeAssistant, freezer: Any) -> None:
    """Restore data newer than the store, as after a state write the store didn't follow, same value."""
    rig = await _cooling(hass, freezer)
    adopted = await _press_and_adopt(rig, low=64, high=66)
    async with stopped(rig.hass, rig.entry):
        rig.freezer.tick(timedelta(seconds=30))
        await _plant_target(rig, 66.0, dt_util.utcnow())
    await rig.settle()
    assert rig.row()["target"] == 66
    assert _provenance(rig) == ("head", _iso(adopted))
    await rig.close()


def test_resume_reads_the_pair_only_together() -> None:
    """``resume.resume`` on its own: a readable pair with its target, else nothing."""
    cid = "climate.invented"
    slots = [RoomSlot(cid, "primary", (50.0, 90.0))]
    system = {"unit": "°F", "step": 1.0, "celsius": False, "engage_bounds": (0.5, 10.0)}

    def read(room: dict[str, Any]) -> tuple[Any, Any]:
        stored = resume(SavedState(saved_at=1.0, unit="°F", sections={SECTION_ROOMS: {cid: room}}), slots, **system)
        return stored.rooms[0].target_source, stored.rooms[0].target_set_at

    assert read({"target": 70.0, "target_source": "head", "target_set_at": 5.0}) == ("head", 5.0)
    assert read({"target": 70.0, "target_source": "home_assistant", "target_set_at": 6}) == ("home_assistant", 6.0)
    assert read({"target": 70.0}) == (None, None)
    assert read({"target": 70.0, "target_source": "head"}) == (None, None)
    assert read({"target": 70.0, "target_source": "head", "target_set_at": float("nan")}) == (None, None)
    assert read({"target": 70.0, "target_source": "head", "target_set_at": True}) == (None, None)
    assert read({"target": 70.0, "target_source": "seed", "target_set_at": 5.0}) == (None, None)
    # Without a target the store resumes, its source describes nothing.
    assert read({"target": "warm", "target_source": "head", "target_set_at": 5.0}) == (None, None)


# ---------------------------------------------------------------------------
# Metadata only: what MXZ sends the heads doesn't change
# ---------------------------------------------------------------------------


async def test_a_write_of_the_same_target_records_it_and_sends_nothing(hass: HomeAssistant, freezer: Any) -> None:
    """The source and time move; no head gets a call."""
    rig = await _cooling(hass, freezer)
    await rig.later(5)
    calls = record(hass)
    rig.freezer.tick(timedelta(seconds=60))
    await _number(rig, "_primary_target", TARGET)
    assert _provenance(rig) == ("home_assistant", _iso(rig.now()))
    assert [writes(calls, head.entity_id) for head in rig.heads] == [[], []]
    await rig.close()


async def _scenario(hass: HomeAssistant, freezer: Any) -> list[list[tuple[str, dict[str, Any]]]]:
    """A write in HA, a press at the head, a restart, hours, a thermostat write: each head's calls."""
    rig = await _cooling(hass, freezer)
    calls = record(hass)
    await _number(rig, "_primary_target", 72)
    await _press_and_adopt(rig, low=64, high=66)
    await rig.restart()
    await rig.later(HOURS)
    await _thermostat(rig, temperature=71)
    await rig.later(60)
    sent = [writes(calls, head.entity_id) for head in rig.heads]
    await rig.close()
    return sent


def _no_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """MXZ as it would run with nothing recorded and nothing resumed."""
    monkeypatch.setattr(MXZCoordinator, "_stamp_target", lambda self, zone: None)
    monkeypatch.setattr(MXZCoordinator, "restore_target_source", lambda self, zone, source, set_at: None)


async def test_the_heads_get_the_same_calls_with_or_without_provenance(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_it = await _scenario(hass, freezer)
    _no_provenance(monkeypatch)
    without_it = await _scenario(hass, freezer)
    assert with_it[0], "the scenario sends the first head something"
    assert with_it == without_it


async def test_the_comparison_catches_a_stamp_that_sends_a_head_something(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative of the test above: a stamp that also wrote a head would show in the calls."""
    clean = await _scenario(hass, freezer)
    stamp = MXZCoordinator._stamp_target

    def stamp_and_send(self: MXZCoordinator, zone: Any) -> None:
        stamp(self, zone)
        self.hass.async_create_task(
            self.hass.services.async_call(
                "climate", "set_temperature", {"entity_id": zone.climate_id, "target_temp_low": 60, "target_temp_high": 80},
            )
        )

    monkeypatch.setattr(MXZCoordinator, "_stamp_target", stamp_and_send)
    assert await _scenario(hass, freezer) != clean


# ---------------------------------------------------------------------------
# The templates docs/FEATURES.md gives for automations and Comfort priority
# ---------------------------------------------------------------------------

FEATURES = Path(__file__).parents[1] / "docs/FEATURES.md"


def _documented_templates() -> tuple[str, str]:
    """The schedule condition and the Comfort priority sensor, as the guide prints them."""
    text = FEATURES.read_text()
    section = text[text.index("## Where a room's target came from"):]
    section = section[: section.index("\n## ", 1)]
    blocks = re.findall(r"```yaml\n(.*?)```", section, re.DOTALL)
    assert len(blocks) == 2
    condition, sensor = (yaml.safe_load(block) for block in blocks)
    return (
        condition["conditions"][0]["value_template"],
        sensor["template"][0]["binary_sensor"][0]["state"],
    )


async def test_the_documented_templates_read_the_plan_row(hass: HomeAssistant, freezer: Any) -> None:
    """A press at the Den's head: the schedule skips it and the sensor is on, until the window ends."""
    skip_unless, changed_at_head = _documented_templates()

    def render(source: str) -> Any:
        return Template(source, hass).async_render(parse_result=True)

    rig = await open_rig(hass, freezer, temps=(HOT, HOT))
    assert rig.row(1)["name"] == "Den"
    assert rig.heads[1].mode == "cool"
    assert rig.hass.states.get("sensor.mxz_coordinator_plan") is not None
    # Set in Home Assistant: the schedule may write the room, and it has no Comfort priority.
    assert (render(skip_unless), render(changed_at_head)) == (True, False)
    rig.heads[1].person_setpoint(low=64, high=66)
    await rig.hass.async_block_till_done()
    await rig.later(HELD)
    assert rig.row(1)["target_source"] == "head"
    assert (render(skip_unless), render(changed_at_head)) == (False, True)
    await rig.later(2 * 3600)
    assert (render(skip_unless), render(changed_at_head)) == (False, False)
    await rig.later(3600)
    assert (render(skip_unless), render(changed_at_head)) == (True, False)
    await rig.close()
