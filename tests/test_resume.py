"""resume.py's rules, called directly: no Home Assistant, no pytest-homeassistant.

Each expectation is worked out from the rule it states (the record wins unless the select's
restore data is newer; an unreadable change time holds the full window; a stored value is
taken only when it is one, in this unit and inside its bounds; an enabled room waits for its
head's run, a disabled one has none), not read back from the code. The last test restarts a
house the way the simulator will: the store's records through ``resume`` and
``resumed_enable``, then ``planner.decide``. Every room, head and value is invented.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.mxz_coordinator.const import (
    DEMAND_NEUTRAL,
    KEY_COOL_LOCKOUT,
    KEY_COORDINATOR_ENABLE,
    KEY_ECO_IDLE,
    MODE_COOL,
    MODE_HEAT,
    MODE_OFF,
)
from custom_components.mxz_coordinator.fan_latch import FanRecord
from custom_components.mxz_coordinator.planner import (
    CallLimits,
    RoomInput,
    Shared,
    decide,
)
from custom_components.mxz_coordinator.resume import (
    SECTION_FAN,
    SECTION_FLOOR,
    SECTION_HEAD_WRITES,
    SECTION_ROOMS,
    SECTION_SWITCHES,
    ModeRecord,
    RoomSlot,
    SavedState,
    mode_record,
    resume,
    resumed_enable,
    resumed_mode,
)

NOW = 1_800_000_000.0
A, B = "climate.study", "climate.den"
SLOTS = [RoomSlot(A, "study", (61.0, 86.0)), RoomSlot(B, "den", (61.0, 86.0))]
# °F: targets on whole degrees, a drift between 0.5 and 6 on quarter degrees.
SYSTEM = {"unit": "°F", "step": 1.0, "celsius": False, "engage_bounds": (0.5, 6.0)}


def saved(unit: str | None = "°F", **sections: dict) -> SavedState:
    return SavedState(saved_at=NOW, unit=unit, sections=sections)


# -- the mode record -----------------------------------------------------------

def test_no_record_is_a_fresh_install_with_nothing_to_wait_out() -> None:
    assert mode_record(None, NOW) == (ModeRecord(), False)


@pytest.mark.parametrize(
    ("stored", "record"),
    [
        ({"last_mode_change": 7.0}, ModeRecord(7.0)),
        (
            {"last_mode_change": 7.0, "mode": "heat", "mode_at": 5.0, "mode_by_votes": True},
            ModeRecord(7.0, MODE_HEAT, 5.0, True),
        ),
        # Votes chose the mode and no real change was ever made: nothing to wait out.
        ({"mode": "cool", "mode_at": 5.0, "mode_by_votes": False}, ModeRecord(None, MODE_COOL, 5.0)),
        # A mode that isn't readable is no mode on record; the time still counts.
        ({"last_mode_change": 7.0, "mode": "dry", "mode_at": 5.0, "mode_by_votes": True}, ModeRecord(7.0)),
        ({"last_mode_change": 7.0, "mode": "heat", "mode_at": "x", "mode_by_votes": True}, ModeRecord(7.0)),
        ({"last_mode_change": 7.0, "mode": "heat", "mode_at": 5.0, "mode_by_votes": 1}, ModeRecord(7.0)),
    ],
)
def test_a_readable_record_reads_as_it_was_written(stored: dict, record: ModeRecord) -> None:
    assert mode_record(stored, NOW) == (record, False)


@pytest.mark.parametrize(
    ("stored", "record"),
    [
        ({"last_mode_change": "x"}, ModeRecord(NOW)),
        (
            {"last_mode_change": None, "mode": "heat", "mode_at": 5.0, "mode_by_votes": True},
            ModeRecord(NOW, MODE_HEAT, 5.0, True),
        ),
        (["not", "a", "record"], ModeRecord(NOW)),
        ({}, ModeRecord(NOW)),
    ],
)
def test_a_record_with_an_unreadable_time_holds_the_full_window_from_now(
    stored: object, record: ModeRecord
) -> None:
    assert mode_record(stored, NOW) == (record, True)


def test_a_record_reads_back_as_it_is_written() -> None:
    for record in (ModeRecord(7.0), ModeRecord(None, MODE_HEAT, 5.0, True), ModeRecord(7.0, MODE_COOL, 9.0)):
        assert mode_record(record.stored(), NOW) == (record, False)


# -- the shared mode a start resumes in ----------------------------------------

def test_with_no_mode_on_record_the_select_decides_and_votes_did_not() -> None:
    assert resumed_mode(ModeRecord(7.0), None, None) == (MODE_COOL, False)
    assert resumed_mode(ModeRecord(7.0), MODE_HEAT, 3.0) == (MODE_HEAT, False)


@pytest.mark.parametrize(
    ("restored", "restored_at", "want"),
    [
        (None, None, (MODE_HEAT, True)),  # no restore data
        (MODE_HEAT, 99.0, (MODE_HEAT, True)),  # they agree: the record says votes set it
        (MODE_COOL, None, (MODE_HEAT, True)),  # restore data with no time
        (MODE_COOL, 4.0, (MODE_HEAT, True)),  # older: a crash soon after the change
        (MODE_COOL, 5.0, (MODE_HEAT, True)),  # as old: the record still wins
        (MODE_COOL, 6.0, (MODE_COOL, False)),  # newer: changed while a release without it ran
    ],
)
def test_the_record_wins_unless_the_selects_restore_data_is_newer(
    restored: str | None, restored_at: float | None, want: tuple[str, bool]
) -> None:
    assert resumed_mode(ModeRecord(7.0, MODE_HEAT, 5.0, True), restored, restored_at) == want


# -- what the store resumes ----------------------------------------------------

def test_an_empty_store_resumes_nothing() -> None:
    stored = resume(SavedState(), SLOTS, **SYSTEM)
    assert stored.switches == {}
    assert stored.resumed == frozenset()
    assert [room.target for room in stored.rooms] == [None, None]
    assert all(not room.has_drift and room.enable is None and room.fan is None for room in stored.rooms)


def test_only_a_switch_stored_as_a_bool_is_resumed() -> None:
    stored = resume(
        saved(**{SECTION_SWITCHES: {KEY_COORDINATOR_ENABLE: True, KEY_ECO_IDLE: "on", KEY_COOL_LOCKOUT: False}}),
        SLOTS, **SYSTEM,
    )
    assert stored.switches == {KEY_COORDINATOR_ENABLE: True, KEY_COOL_LOCKOUT: False}
    assert stored.resumed == {("switch", KEY_COORDINATOR_ENABLE), ("switch", KEY_COOL_LOCKOUT)}


def test_a_target_is_held_to_its_heads_bounds_and_a_non_number_is_none() -> None:
    rooms = {A: {"target": 90.0}, B: {"target": "warm"}}
    stored = resume(saved(**{SECTION_ROOMS: rooms}), SLOTS, **SYSTEM)
    assert [room.target for room in stored.rooms] == [86.0, None]
    assert stored.resumed == {("target", "study")}


def test_a_target_kept_in_celsius_comes_back_in_fahrenheit_on_the_step() -> None:
    # 22.5 °C is 72.5 °F, which lands on 73 °F (half up); 18 °C is 64.4 °F, so 64 °F.
    rooms = {A: {"target": 22.5}, B: {"target": 18.0}}
    stored = resume(saved("°C", **{SECTION_ROOMS: rooms}), SLOTS, **SYSTEM)
    assert [room.target for room in stored.rooms] == [73.0, 64.0]


def test_a_pair_is_resumed_only_whole() -> None:
    rooms = {A: {"target_low": 66.0, "target_high": 76.0}, B: {"target_low": 66.0, "target_high": None}}
    stored = resume(saved(**{SECTION_ROOMS: rooms}), SLOTS, **SYSTEM)
    assert [room.target_range for room in stored.rooms] == [(66.0, 76.0), None]
    assert stored.resumed == {("range", "study")}


def test_a_stored_drift_follows_the_global_scales_and_stays_in_bounds() -> None:
    cases = {
        A: ({"drift": None}, (True, None)),  # stored "follow the global"
        B: ({}, (False, None)),  # nothing stored
    }
    stored = resume(saved(**{SECTION_ROOMS: {cid: room for cid, (room, _) in cases.items()}}), SLOTS, **SYSTEM)
    assert [(room.has_drift, room.drift) for room in stored.rooms] == [want for _, want in cases.values()]
    wide = resume(saved(**{SECTION_ROOMS: {A: {"drift": 2.5}, B: {"drift": 50.0}}}), SLOTS, **SYSTEM)
    assert [room.drift for room in wide.rooms] == [2.5, 6.0]
    # 1.5 °C is a 2.7 °F band, on the quarter-degree step: 2.75.
    celsius = resume(saved("°C", **{SECTION_ROOMS: {A: {"drift": 1.5}}}), SLOTS, **SYSTEM)
    assert celsius.rooms[0].drift == 2.75


def test_an_enable_brings_its_floor_record_and_only_a_true_release() -> None:
    floor = {"floor_run": {"direction": "heat"}}
    rooms = {A: {"enable": False, "hands_off_released": True}, B: {"enable": 1, "hands_off_released": "yes"}}
    stored = resume(saved(**{SECTION_ROOMS: rooms, SECTION_FLOOR: {A: floor}}), SLOTS, **SYSTEM)
    assert stored.rooms[0].enable is False
    assert stored.rooms[0].enable_record == {**floor, "hands_off_released": True}
    assert stored.rooms[1].enable is None
    assert stored.resumed == {("enable", "study")}
    released = resume(saved(**{SECTION_ROOMS: {A: {"enable": False, "hands_off_released": "yes"}}}), SLOTS, **SYSTEM)
    assert released.rooms[0].enable_record == {"hands_off_released": False}


def test_a_fan_record_counts_only_with_a_clean_held() -> None:
    fan = {
        A: {"held": True, "last_fan_command": "quiet", "prior_fan_command": "low"},
        B: {"held": "yes"},
    }
    stored = resume(saved(**{SECTION_FAN: fan}), SLOTS, **SYSTEM)
    assert stored.rooms[0].fan == FanRecord(True, "quiet", "low")
    assert stored.rooms[1].fan is None
    assert stored.resumed == {("fan", A)}


def test_a_fan_records_waiting_on_and_older_writes_are_read_as_the_switch_reads_them() -> None:
    fan = {
        A: {"held": False, "last_fan_command": None, "prior_fan_command": None, "fan_on_pending": True},
        B: {"held": True, "last_fan_command": 3, "older_fan_commands": ["low", 2]},
    }
    stored = resume(saved(**{SECTION_FAN: fan}), SLOTS, **SYSTEM)
    assert stored.rooms[0].fan == FanRecord(False, on_pending=True)
    # A token that isn't a string is no memory; older writes that aren't all tokens are none.
    assert stored.rooms[1].fan == FanRecord(True)


def test_the_head_write_memory_comes_back_only_in_the_unit_it_was_kept_in() -> None:
    writes = {A: {"hvac_mode": [["cool", 0], ["heat", 1]], "temperature": "junk"}}
    same = resume(saved(**{SECTION_HEAD_WRITES: writes}), SLOTS, **SYSTEM)
    assert list(same.rooms[0].head_writes) == ["hvac_mode"]
    assert same.rooms[0].head_writes["hvac_mode"].record() == [["cool", 0], ["heat", 1]]
    assert same.rooms[1].head_writes == {}
    assert resume(saved("°C", **{SECTION_HEAD_WRITES: writes}), SLOTS, **SYSTEM).rooms[0].head_writes == {}
    assert resume(saved(None, **{SECTION_HEAD_WRITES: writes}), SLOTS, **SYSTEM).rooms[0].head_writes == {}


def test_a_head_is_read_by_its_climate_id_whatever_its_place() -> None:
    rooms = {B: {"target": 66.0}, A: {"target": 74.0}}
    stored = resume(saved(**{SECTION_ROOMS: rooms}), list(reversed(SLOTS)), **SYSTEM)
    assert [room.target for room in stored.rooms] == [66.0, 74.0]


@pytest.mark.parametrize(
    ("recorded_at", "outranks"),
    [(None, True), (NOW - 1, True), (NOW, False), (NOW + 1, False)],
)
def test_the_store_outranks_only_an_older_restore_record(recorded_at: float | None, outranks: bool) -> None:
    when = None if recorded_at is None else datetime.fromtimestamp(recorded_at, UTC)
    assert saved().outranks(when) is outranks
    assert SavedState().outranks(when) is (recorded_at is None)  # a store with no time is older


# -- a resumed enable ------------------------------------------------------------

def test_an_enabled_room_waits_for_its_heads_run_and_keeps_no_floor() -> None:
    latch, startup = {"study": "", "den": MODE_COOL}, {"den": ""}
    seed = resumed_enable("study", True, True, {"hands_off_released": True, "floor_run": {}}, latch, startup)
    assert (seed.latch, seed.startup) == ({"study": "", "den": MODE_COOL}, {"den": "", "study": None})
    assert (seed.released, seed.floor) == (False, {})
    assert (latch, startup) == ({"study": "", "den": MODE_COOL}, {"den": ""})  # inputs untouched


def test_a_disabled_room_has_no_run_and_keeps_its_floor_record() -> None:
    record = {"hands_off_released": False, "floor_run": {"direction": "heat"}}
    seed = resumed_enable("study", False, False, record, {"study": MODE_HEAT}, {"study": None})
    assert (seed.latch, seed.startup) == ({"study": ""}, {})
    assert seed.floor is record


@pytest.mark.parametrize(
    ("hands_off", "released", "want"),
    [(True, True, True), (False, True, False), (True, False, False), (True, "yes", False)],
)
def test_a_disabled_room_is_released_only_opted_in_and_with_its_off_landed(
    hands_off: bool, released: object, want: bool
) -> None:
    seed = resumed_enable("study", False, hands_off, {"hands_off_released": released}, {}, {})
    assert seed.released is want


# -- a restart, the way the simulator will run one -------------------------------

LIMITS = CallLimits(
    eco=False, eco_cool_max=85.0, eco_heat_min=55.0,
    heat_lockout=False, heat_lockout_floor=60.0,
    cool_lockout=False, cool_lockout_ceiling=80.0,
)
SHARED = Shared(
    limits=LIMITS, demand_threshold=2.0, mode=MODE_COOL, elapsed=None, hold=600.0,
    resting=None, by_votes=False, resume=True, accept_head_changes=True,
)


def room_input(slug: str, temp: float, head: str, target: float, enabled: bool) -> RoomInput:
    return RoomInput(
        slug=slug, enabled=enabled, temp=temp, sensor_ok=True, target=target,
        heat_target=target, cool_target=target, heat_drift=1.0, cool_drift=1.0,
        head=head, paused=False, dry=False, floor=None, safety=False,
        vote=None, holds=True, boost=0.0,
    )


def test_a_restart_resumes_the_run_each_head_shows_through_the_same_functions() -> None:
    """The study was cooling to 72 when Home Assistant stopped; the den was disabled.

    The store gives the targets, the enables and the Coordinator back; the study waits for
    its head, which still shows cool, and the first decision carries its run on. The den
    is disabled, so it has no run and nothing is latched for it.
    """
    store = saved(**{
        SECTION_SWITCHES: {KEY_COORDINATOR_ENABLE: True},
        SECTION_ROOMS: {A: {"target": 72.0, "enable": True}, B: {"target": 68.0, "enable": False}},
    })
    stored = resume(store, SLOTS, **SYSTEM)
    latch: dict[str, str] = {"study": "", "den": ""}
    startup: dict[str, str | None] = {}
    for slot, room in zip(SLOTS, stored.rooms, strict=True):
        assert room.enable is not None
        seed = resumed_enable(slot.slug, room.enable, False, room.enable_record, latch, startup)
        latch, startup = seed.latch, seed.startup
    assert startup == {"study": None}
    # The study is half a degree warm: inside its drift, so a fresh decision would coast.
    rooms = [
        room_input("study", 72.5, MODE_COOL, stored.rooms[0].target or 0.0, True),
        room_input("den", 71.0, MODE_OFF, stored.rooms[1].target or 0.0, False),
    ]
    resumed = replace(SHARED, resume=stored.switches[KEY_COORDINATOR_ENABLE])
    decision = decide(rooms, resumed, latch, startup)
    assert decision.latch == {"study": MODE_COOL, "den": ""}
    assert decision.startup == {}
    study = decision.rooms[0]
    assert (study.demand, study.engage) == (DEMAND_NEUTRAL, MODE_COOL)
    fresh = decide(rooms, resumed, {"study": "", "den": ""}, {})
    assert fresh.rooms[0].engage != MODE_COOL  # without the seed the run would not carry on
