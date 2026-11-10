"""The mode-change hold's promises across Resting mode, reloads, restarts and crashes.

Three rules (verification/modehold, findings F1 to F3, fixed together):

* F1. Resting mode does not take the shared mode from a room still running
  toward its target in it. That room finishes first, then the mode rests.
  Another room's real call for the other mode, and a person's pick, still
  take it as before.
* F2. A mode only hands-off votes chose is still known as such after a reload
  or a restart, so a coordinated room's run in it starts the hold, as it does
  without one. A mode a room chose starts no fresh hold after a reload.
* F3. The shared mode is written to the entry's record at once, with the time
  of the last real change, and a start prefers it over the select's restore
  data unless that is newer.

The replays of the model's counterexamples are tests/test_modehold_findings.py;
these are the same promises from other sides, with the cases that must not
change. All rooms, heads and values are invented.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache,
)

from custom_components.mxz_coordinator.const import (
    CONF_RESTING_MODE_BIAS,
    DOMAIN,
    MODE_COOL,
    MODE_HEAT,
)
from custom_components.mxz_coordinator.logic import arbitrate
from tests.clocks import jump_wall_clock
from tests.dwell import assert_not_restamped, assert_restamped, expire_dwell
from tests.restart import restart
from tests.test_drive import SENSOR_A, SENSOR_B, _eid, _recompute, _set_temp
from tests.test_dwell_wakeup import _setup_dwell
from tests.test_hands_off import _plan, _remote, _s, _setup
from tests.test_modehold_properties import _active, _first_ranked, _rooms3

STORE_KEY = "mxz_coordinator.{}.mode_change"


def _record(hass_storage: dict, entry) -> dict:
    return hass_storage[STORE_KEY.format(entry.entry_id)]["data"]


# --- F1: Resting mode waits for a room still running --------------------------


async def _cooling_run_inside_the_threshold(hass: HomeAssistant, **setup):
    """Resting mode Heat; the Primary calls cool at 76 (target 70), runs cool,
    and has cooled to 72: inside the Mode switch threshold, still running.
    The hold since the cool change has passed."""
    entry, head_a, head_b = await _setup(
        hass, hysteresis=None, **{CONF_RESTING_MODE_BIAS: MODE_HEAT}, **setup,
    )
    coord = entry.runtime_data
    expire_dwell(coord)
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)
    assert (_plan(hass, entry).state, _s(hass, head_a)) == (MODE_COOL, MODE_COOL)
    await _set_temp(hass, SENSOR_A, 72)
    expire_dwell(coord)
    return entry, head_a, head_b


async def test_resting_mode_waits_for_the_run_then_rests(hass: HomeAssistant) -> None:
    """The cool run continues to the target; once there, the mode rests at heat."""
    entry, head_a, _b = await _cooling_run_inside_the_threshold(
        hass, enable=("_primary_enable", "_coordinator_enable"),
    )
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.attributes["zones"][0]["demand"] == "neutral"  # not calling
    assert plan.attributes["zones"][0]["engage"] == MODE_COOL  # still running
    assert (plan.state, _s(hass, head_a)) == (MODE_COOL, MODE_COOL)

    await _set_temp(hass, SENSOR_A, 70)  # the target: the run is done
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.attributes["zones"][0]["engage"] != MODE_COOL
    assert plan.state == MODE_HEAT  # Resting mode applies now
    assert _s(hass, head_a) == "fan_only"  # satisfied: parked, not heating


async def test_another_rooms_real_call_still_takes_the_mode(hass: HomeAssistant) -> None:
    """The Secondary calls heat (64, past the threshold): the cool run yields."""
    entry, head_a, head_b = await _cooling_run_inside_the_threshold(hass)
    await _set_temp(hass, SENSOR_B, 64)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_HEAT
    assert (_s(hass, head_a), _s(hass, head_b)) == ("fan_only", MODE_HEAT)


async def test_a_persons_pick_still_takes_the_mode(hass: HomeAssistant) -> None:
    """A person picks heat on the shared-mode select while the cool run continues."""
    entry, head_a, _b = await _cooling_run_inside_the_threshold(
        hass, enable=("_primary_enable", "_coordinator_enable"),
    )
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": _eid(hass, entry, "_shared_mode"), "option": MODE_HEAT},
        blocking=True,
    )
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_HEAT
    assert _s(hass, head_a) == "fan_only"


def test_a_run_in_the_shared_mode_changes_only_where_nothing_calls() -> None:
    """On the real logic.arbitrate, over the property table's three-room cases
    with a room engaged: a run in ``current`` keeps the mode where nothing
    calls (Resting mode does not apply), and changes no other answer."""
    cases = changed = 0
    for entries, kw in _rooms3():
        if not kw["engaged"]:
            continue  # a room running in the shared mode is engaged
        cases += 1
        kept = arbitrate(entries, **kw, running=True)
        free = arbitrate(entries, **kw)
        if _first_ranked(_active(entries, True)) is None:
            assert kept.direction == kw["current"], (entries, kw, kept)
            changed += kept != free
        else:
            assert kept == free, (entries, kw, kept, free)
    assert cases == 324_000
    assert changed > 0  # the table holds cases where Resting mode would have moved it


# --- F2: a mode votes chose stays theirs across a reload or restart -----------


async def _vote_chooses_heat(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch):
    """The Secondary is run from its own remote; a person sets it to heat while
    no enabled room calls, so the vote alone changes the mode (no hold)."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    await _recompute(hass, entry)
    expire_dwell(entry.runtime_data)
    jump_wall_clock(monkeypatch, 3600)
    await _remote(hass, head_b, MODE_HEAT)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_by_hands_off"]) == (MODE_HEAT, True)
    return entry, head_a, head_b


@pytest.mark.parametrize("restarts", [1, 2])
async def test_a_restart_keeps_that_votes_chose_the_mode(
    hass: HomeAssistant, hass_storage: dict, monkeypatch: pytest.MonkeyPatch,
    restarts: int,
) -> None:
    """After a restart (or two), the Primary's heat run in the voted mode
    starts the hold, so its cool call moments later waits."""
    entry, head_a, head_b = await _vote_chooses_heat(hass, monkeypatch)
    for _ in range(restarts):
        await restart(hass, entry)
    coord = entry.runtime_data
    assert coord.current_shared_mode == MODE_HEAT
    assert _plan(hass, entry).attributes["mode_change_allowed"] is True  # nothing held yet
    await _set_temp(hass, SENSOR_A, 64)  # the Primary calls heat
    await _recompute(hass, entry)
    assert _s(hass, head_a) == MODE_HEAT
    await _remote(hass, head_b, "off")
    await _set_temp(hass, SENSOR_A, 76)  # the Primary now calls cool
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_change_allowed"]) == (MODE_HEAT, False), (
        plan.state, plan.attributes["mode_change_allowed"], _s(hass, head_a),
    )
    assert _s(hass, head_a) != MODE_COOL
    await assert_restamped(hass, entry)  # the run in the voted mode started the hold


async def test_a_vote_change_records_the_mode_and_no_new_change_time(
    hass: HomeAssistant, hass_storage: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record says votes chose heat, at once; its change time stays the
    setup's real change, an hour before (a vote's change starts no hold)."""
    entry, _a, _b = await _vote_chooses_heat(hass, monkeypatch)
    await hass.async_block_till_done()
    record = _record(hass_storage, entry)
    assert (record["mode"], record["mode_by_votes"]) == (MODE_HEAT, True)
    assert record["mode_at"] - record["last_mode_change"] >= 3600


async def test_a_reload_starts_no_fresh_hold_for_a_mode_a_room_chose(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Primary itself chose heat an hour ago. After a reload its run in heat
    is not a new change: its cool call goes through at once (PR #64)."""
    entry, head_a, _b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    await _recompute(hass, entry)
    expire_dwell(entry.runtime_data)
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_by_hands_off"]) == (MODE_HEAT, False)
    jump_wall_clock(monkeypatch, 3600)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _recompute(hass, entry)  # the Primary still runs heat
    assert _s(hass, head_a) == MODE_HEAT
    await assert_not_restamped(hass, entry)  # not stamped again
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_COOL  # at once, no hold
    assert _s(hass, head_a) == MODE_COOL
    await assert_restamped(hass, entry)  # and that change is a real one


# --- F3: the record carries the mode, and a start reads it --------------------


async def test_a_change_writes_the_mode_with_its_time_at_once(
    hass: HomeAssistant, hass_storage: dict,
) -> None:
    """A real change writes mode, change time and by-votes in one record, no
    older than the select's own state for that change."""
    entry, _a, _b = await _setup_dwell(hass, changed_ago=None)
    before = dt_util.utcnow().timestamp()
    await _set_temp(hass, SENSOR_A, 60)
    await _recompute(hass, entry)
    await hass.async_block_till_done()
    record = _record(hass_storage, entry)
    assert record["mode"] == MODE_HEAT
    assert record["mode_by_votes"] is False
    assert before <= record["last_mode_change"] <= record["mode_at"]
    select = hass.states.get(_eid(hass, entry, "_shared_mode"))
    assert select.state == MODE_HEAT
    assert record["mode_at"] >= select.last_changed.timestamp()


@pytest.mark.parametrize(
    ("record", "restored", "want"),
    [
        ("heat_newer", "cool_older", MODE_HEAT),
        ("heat_older", "cool_newer", MODE_COOL),
        ("heat_older", None, MODE_HEAT),
        ("time_only", "heat_older", MODE_HEAT),
    ],
    ids=["record_newer", "restore_data_newer", "no_restore_data", "record_without_a_mode"],
)
async def test_a_start_takes_the_newer_of_the_record_and_the_restore_data(
    hass: HomeAssistant, hass_storage: dict,
    record: str, restored: str | None, want: str,
) -> None:
    """The record and the select's restore data disagree; the newer one sets
    the mode, the select shows it, and a record the restore data beat is
    brought up to date. A record from a version that kept no mode defers to
    the restore data, as before (heat there, so it is not the cold-start
    cool)."""
    now = dt_util.utcnow().timestamp()
    entry_id = "invented_hold_entry"
    times = {"newer": now - 60, "older": now - 3600}
    data: dict = {"last_mode_change": now - 3600}
    if record != "time_only":
        data |= {"mode": MODE_HEAT, "mode_at": times[record.split("_")[1]],
                 "mode_by_votes": True}
    hass_storage[STORE_KEY.format(entry_id)] = {
        "version": 1, "minor_version": 1, "key": STORE_KEY.format(entry_id), "data": data,
    }
    if restored is not None:
        changed = dt_util.utc_from_timestamp(times[restored.split("_")[1]])
        # last_updated after the entry is created, so it is not a previous
        # install's (MXZEntity._restored_state_is_stale).
        updated = dt_util.utc_from_timestamp(now + 60)
        mock_restore_cache(hass, [State(
            "select.mxz_coordinator_shared_mode", restored.split("_")[0],
            last_changed=changed, last_updated=updated,
        )])
    entry, _a, _b = await _setup_dwell(hass, changed_ago=None, entry_id=entry_id)
    coord = entry.runtime_data
    assert coord.current_shared_mode == want
    assert hass.states.get(_eid(hass, entry, "_shared_mode")).state == want
    assert _plan(hass, entry).state == want
    from_record = record != "time_only" and want == data["mode"]
    assert coord._mode_by_votes is from_record  # known only with the record's mode
    await hass.async_block_till_done()
    stored = _record(hass_storage, entry)
    if record == "time_only":
        assert "mode" not in stored  # nothing changed: nothing written
    else:
        assert stored["mode"] == want
        assert stored["mode_by_votes"] is from_record


async def test_the_record_reads_a_mode_with_or_without_a_change_time(
    hass: HomeAssistant, hass_storage: dict,
) -> None:
    """A record holding only a mode votes chose is no change on record (no
    hold); an unreadable change time is a change made now, mode or not; an
    unreadable mode is no mode on record, and the time reads as before."""
    from custom_components.mxz_coordinator.coordinator import (
        ModeRecord,
        async_load_mode_record,
    )

    now = dt_util.utcnow().timestamp()
    entry_id = "invented_record_entry"
    key = STORE_KEY.format(entry_id)
    mode = {"mode": MODE_COOL, "mode_at": 5.0, "mode_by_votes": True}
    cases = [
        (mode, ModeRecord(None, MODE_COOL, 5.0, True)),
        ({"last_mode_change": 7.0, **mode}, ModeRecord(7.0, MODE_COOL, 5.0, True)),
        ({"last_mode_change": 7.0, **mode, "mode": "dry"}, ModeRecord(7.0)),
        ({"last_mode_change": 7.0, **mode, "mode_at": "x"}, ModeRecord(7.0)),
        ({"last_mode_change": 7.0, **mode, "mode_by_votes": 1}, ModeRecord(7.0)),
        ({"last_mode_change": 7.0}, ModeRecord(7.0)),
    ]
    for data, want in cases:
        hass.data.get(f"{DOMAIN}_mode_change_stores", {}).pop(entry_id, None)
        hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}
        assert await async_load_mode_record(hass, entry_id) == want, data
    for data in ({"last_mode_change": "x", **mode}, {**mode, "mode": "dry"}):
        hass.data.get(f"{DOMAIN}_mode_change_stores", {}).pop(entry_id, None)
        hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}
        got = await async_load_mode_record(hass, entry_id)
        assert got.changed_at is not None and got.changed_at >= now, (data, got)
        assert got.mode == (MODE_COOL if data["mode"] == MODE_COOL else None)
    hass.data.get(f"{DOMAIN}_mode_change_stores", {}).pop(entry_id, None)
