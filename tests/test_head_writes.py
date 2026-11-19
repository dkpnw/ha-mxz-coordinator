"""head_writes.py called directly: no pytest-homeassistant.

MXZ's memory of its own mode and setpoint writes, field by field: what a head shows for a
field, how a report compares with a write (the mode as it is, a setpoint on the head's grid),
and the three moments the memory changes (a write going out, a report, a person's change the
adopt hold took). Each expectation is worked by hand from invented values.
"""

from __future__ import annotations

from custom_components.mxz_coordinator.grid import Grid
from custom_components.mxz_coordinator.head_writes import (
    HEAD_WRITE_MODE,
    RememberWrites,
    SendCall,
    field_shown,
    issue_steps,
    remember_writes,
    same_as,
    write_is_own,
    writes_caught_up,
    writes_pressed,
    writes_refused,
)
from custom_components.mxz_coordinator.logic import HeadWriteMemory

F = "°F"
PLAIN = Grid(system_unit=F, target_step=1.0, clamp_min=59.0, clamp_max=88.0)
# A 0.5 °C head in a °F house: 64.4 °F is where 64 °F latches.
CN105 = Grid(system_unit=F, target_step=1.0, clamp_min=59.0, clamp_max=88.0, native_step=0.5, native_unit="°C")
SHOWS = {"target_temp_low": 68.0, "target_temp_high": "70.0", "temperature": None}


def test_what_a_head_shows_for_each_field() -> None:
    assert field_shown("cool", SHOWS, HEAD_WRITE_MODE) == "cool"
    assert field_shown("cool", SHOWS, "target_temp_low") == 68.0
    assert field_shown("cool", SHOWS, "target_temp_high") == 70.0
    assert field_shown("cool", SHOWS, "temperature") is None
    for mode in (None, "unavailable", "unknown"):
        assert field_shown(mode, SHOWS, HEAD_WRITE_MODE) is None


def test_the_mode_compares_as_it_is_and_a_setpoint_on_the_heads_grid() -> None:
    assert same_as(HEAD_WRITE_MODE, CN105)("cool", "cool")
    assert not same_as(HEAD_WRITE_MODE, CN105)("cool", "heat")
    assert same_as("target_temp_low", CN105)(64.4, 64.0)
    assert not same_as("target_temp_low", PLAIN)(64.6, 64.0)


def test_a_write_is_remembered_per_field_it_carries() -> None:
    memories: dict[str, HeadWriteMemory] = {}
    counted = remember_writes(
        memories, {"hvac_mode": "cool", "target_temp_low": 66.0, "entity": "x"},
        mode="off", attributes=SHOWS, grid=PLAIN, size=4,
    )
    assert counted == {"hvac_mode": "cool", "target_temp_low": 66.0}
    assert set(memories) == {"hvac_mode", "target_temp_low"}
    assert memories["hvac_mode"].values == ("off", "cool")


def test_a_call_remembers_its_writes_before_it_is_sent() -> None:
    payload = {"hvac_mode": "heat", "temperature": 68.0}
    assert issue_steps(payload) == (RememberWrites(payload), SendCall())
    assert issue_steps(payload) != (SendCall(), RememberWrites(payload))


def test_a_refused_call_takes_back_only_the_copies_it_counted() -> None:
    memories: dict[str, HeadWriteMemory] = {}
    shows = {"temperature": 66.0}
    remember_writes(memories, {"temperature": 68.0}, mode="heat", attributes=shows, grid=PLAIN, size=4)
    counted = remember_writes(
        memories, {"hvac_mode": "heat", "temperature": 68.0}, mode="heat", attributes=shows,
        grid=PLAIN, size=4,
    )
    # The mode is what the head shows: no copy. 68 counts its second copy.
    assert counted == {"temperature": 68.0}
    assert memories["temperature"].record() == [[66.0, 0], [68.0, 2]]
    writes_refused(memories, counted)
    assert memories["temperature"].record() == [[66.0, 0], [68.0, 1]]
    assert memories["hvac_mode"].record() == [["heat", 0]]
    # Nothing counted, nothing taken back; a field with no memory is passed over.
    writes_refused(memories, {})
    writes_refused(memories, {"target_temp_low": 60.0})
    assert memories["temperature"].record() == [[66.0, 0], [68.0, 1]]
    assert "target_temp_low" not in memories


def test_a_write_of_what_the_head_shows_counts_no_copy() -> None:
    memories = {"target_temp_low": HeadWriteMemory(4)}
    memories["target_temp_low"].wrote(64.4)
    memories["target_temp_low"].caught_up(64.4, CN105.already_at, moved=True)
    counted = remember_writes(
        memories, {"target_temp_low": 64.0}, mode="cool",
        attributes={"target_temp_low": 64.4}, grid=CN105, size=4,
    )
    assert counted == {}


def test_a_report_passes_the_writes_before_it_and_says_whether_it_moved() -> None:
    memories: dict[str, HeadWriteMemory] = {}
    remember_writes(memories, {"target_temp_low": 66.0}, mode="cool", attributes=SHOWS, grid=PLAIN, size=4)
    remember_writes(memories, {"target_temp_low": 64.0}, mode="cool", attributes=SHOWS, grid=PLAIN, size=4)
    writes_caught_up(
        memories, old_mode="cool", old_attributes=SHOWS,
        mode="cool", attributes={"target_temp_low": 66.0}, grid=PLAIN,
    )
    assert memories["target_temp_low"].values == (66.0, 64.0)
    # A report of nothing usable passes nothing.
    writes_caught_up(memories, old_mode="cool", old_attributes=SHOWS, mode=None, attributes=None, grid=PLAIN)
    assert memories["target_temp_low"].values == (66.0, 64.0)


def test_a_persons_change_replaces_what_the_head_last_showed_in_the_fields_named() -> None:
    memories: dict[str, HeadWriteMemory] = {}
    remember_writes(
        memories, {"hvac_mode": "cool", "target_temp_low": 66.0}, mode="cool",
        attributes={"target_temp_low": 66.0}, grid=PLAIN, size=4,
    )
    writes_pressed(memories, ("target_temp_low", "temperature"), mode="cool",
                   attributes={"target_temp_low": 61.0}, grid=PLAIN)
    assert memories["target_temp_low"].values[0] == 61.0
    assert "temperature" not in memories
    assert memories["hvac_mode"].values == ("cool",)


def test_own_reads_the_fields_memory_on_its_grid() -> None:
    memories: dict[str, HeadWriteMemory] = {}
    remember_writes(memories, {"target_temp_low": 64.0}, mode="cool", attributes=SHOWS, grid=CN105, size=4)
    # 64.6 °F is 18.11 °C, the 18.0 °C 64 °F latches; on whole °F it is 0.6 off.
    assert write_is_own(memories, "target_temp_low", 64.6, CN105)
    assert not write_is_own(memories, "target_temp_low", 64.6, PLAIN)
    assert not write_is_own(memories, "target_temp_high", 70.0, CN105)
