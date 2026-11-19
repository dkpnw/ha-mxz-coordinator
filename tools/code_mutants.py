"""Broken copies of MXZ's code, each with the test that must fail on it. Standard library only.

    tools/code_mutants.py               every mutant
    tools/code_mutants.py C3 H9         only these

Each mutant names a function, not a file: the text it breaks must occur exactly once in the
bodies of the functions of that name under custom_components/mxz_coordinator/, wherever they
live. A function can move to another module and its mutants follow it. A change to the text
itself needs the mutant re-anchored to the same logic; tests/test_formal_contract.py fails
until it is.

C mutants are the code-side twins of the rules in docs/formal/tla/MXZCoordinator.tla
(docs/formal/README.md). H mutants are the twins of the mode-hold rules in
verification/modehold/MXZModeHold.tla, which `verification/run mutants` runs with the model's
own broken copies.

Each run breaks a scratch copy of the checkout, never the checkout, and runs the mutant's
test there with MXZ_PYTHON, a lane venv's python with hypothesis in it.

A mutant whose code runs only on some Home Assistant releases says so with ``needs``: a probe
run first in MXZ_PYTHON. On a lane where the probe says no, the mutant is not run: its line
starts "N/A", says what it needs and why this lane lacks it, and it counts as not applicable,
never as caught. A probe that errors, or says anything but "yes" or "no <why>", is an error.

The last two lines are the counts (caught, not applicable, missed, probe errors) and a marker:
MUTANTS_ALL_CAUGHT when no mutant was missed, no probe errored and at least one was caught;
MUTANTS_NONE_APPLICABLE when nothing was run because no mutant asked for applies to this lane;
MUTANTS_MISSED otherwise. The exit status is 0 only with MUTANTS_ALL_CAUGHT.
"""

import argparse
import ast
import functools
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path("custom_components/mxz_coordinator")


@dataclass(frozen=True)
class Needs:
    """What a lane must have for a mutant's broken code to run there."""

    what: str  # in words, as "needs <what>"
    probe: str  # Python run in MXZ_PYTHON; prints "yes", or "no <why this lane lacks it>"


@dataclass(frozen=True)
class Mutant:
    what: str  # what breaks, in words
    symbol: str  # the function whose body holds ``old``
    old: str
    new: str
    test: str  # a pytest node id that must fail on the broken copy
    needs: Needs | None = None  # None: the broken code runs on every lane


# delivery._legacy_busy's own guard: the code it guards runs only where this says yes.
HA_BEFORE_2025_11 = Needs(
    "HA before 2025.11",
    "from homeassistant.helpers.debounce import Debouncer\n"
    "print('no (Debouncer.async_lock present)' if hasattr(Debouncer, 'async_lock') else 'yes')\n",
)


MUTANTS: dict[str, Mutant] = {
    'C1': Mutant(
        'the hold is ignored', 'decide',
        'allowed = shared.elapsed is None or shared.elapsed >= shared.hold\n    engaged',
        'allowed = True\n    engaged',
        'tests/test_properties_mode.py::test_no_flip_inside_the_hold_and_no_wait_after_it',
    ),
    'C2': Mutant(
        'the hold runs long', 'decide',
        'allowed = shared.elapsed is None or shared.elapsed >= shared.hold\n    engaged',
        'allowed = shared.elapsed is None or shared.elapsed >= shared.hold * 2\n    engaged',
        'tests/test_properties_mode.py::test_no_flip_inside_the_hold_and_no_wait_after_it',
    ),
    'C3': Mutant(
        "a flip isn't stamped", 'mode_write_back',
        'stamp=not by_hands_off,',
        'stamp=False,',
        'tests/test_properties_mode.py::test_no_flip_inside_the_hold_and_no_wait_after_it',
    ),
    'C4': Mutant(
        'a start restarts the hold', 'mode_change_stamp',
        'ago = max(0.0, wall - stored)',
        'ago = 0.0',
        'tests/test_properties_restart.py::test_a_start_changes_nothing_anyone_can_see',
    ),
    'C5': Mutant(
        'arbitrate ignores the gate', 'shared_mode',
        'if proposed != current and allowed and (standoff or not still):',
        'if proposed != current and (standoff or not still):',
        'tests/test_properties_mode.py::test_arbitrate_never_changes_the_mode_while_the_hold_holds',
    ),
    'C6': Mutant(
        'a start ignores the record', 'resumed_mode',
        'mode, by_votes = record.mode, record.by_votes\n    else:',
        'mode, by_votes = restored or MODE_COOL, False\n    else:',
        'tests/test_properties_mode.py::test_a_start_resumes_the_recorded_mode',
    ),
    'C7': Mutant(
        "Resting mode doesn't wait", 'arbitrate',
        'if running:\n        resting = None',
        'if False:\n        resting = None',
        'tests/test_properties_mode.py::test_a_run_in_the_shared_mode_keeps_it_from_resting_mode',
    ),
    'C8': Mutant(
        "a departure isn't a hold", 'observe_fan',
        'if not (seeding or departed):',
        'if not seeding:',
        'tests/test_properties_fan.py::test_a_pick_is_never_written_over_until_it_is_handed_back',
    ),
    'C9': Mutant(
        "a hold doesn't stop the write", 'fan_step',
        'if latch.held is True:\n        return latch, None',
        'if False:\n        return latch, None',
        'tests/test_properties_fan.py::test_a_pick_is_never_written_over_until_it_is_handed_back',
    ),
    'C10': Mutant(
        "a restored hold isn't kept", 'reconcile_restored_hold',
        'if held:\n        return hold_at(latch, observed)\n    remembered =',
        'if False:\n        return hold_at(latch, observed)\n    remembered =',
        'tests/test_properties_restart.py::test_a_start_changes_nothing_anyone_can_see',
    ),
    'C11': Mutant(
        'the echo rule widens to the whole ladder', 'own',
        'if observed in (self.last, self.prior):\n            return True',
        'if observed in FAN_LADDER:\n            return True',
        'tests/test_properties_fan.py::test_the_exception_is_exactly_the_last_two',
    ),
    'C12': Mutant(
        'the echo exception is gone', 'own',
        'if observed in (self.last, self.prior):\n            return True',
        'if observed == self.last:\n            return True',
        'tests/test_properties_fan.py::test_the_exception_can_lose_the_pick',
    ),
    'C13': Mutant(
        "a cloud head's hold doesn't stop the write", 'cloud_token',
        'if held:\n            return None\n        if fan_act in',
        'if False:\n            return None\n        if fan_act in',
        'tests/test_properties_fan.py::test_a_cloud_heads_pick_is_never_written_over',
    ),
    'C14': Mutant(
        "a paused head isn't sent off", '_head_apply',
        'return HeadApply(cid, steps=(FanRead(MODE_OFF), Queue(_PAUSE_OFF)))',
        'return HeadApply(cid, steps=(FanRead(MODE_OFF),))',
        'tests/test_properties_window.py::test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes',
    ),
    'C15': Mutant(
        'a paused room still votes', 'decide',
        'if room.paused:\n            demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED',
        'if False:\n            demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED',
        'tests/test_properties_window.py::test_a_paused_room_never_keeps_or_turns_the_shared_mode',
    ),
    'C16': Mutant(
        "a person's change isn't a takeover", 'take_over',
        'self._records[zone.slug].override = "manual"',
        'pass',
        'tests/test_properties_window.py::test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes',
    ),
    'C17': Mutant(
        "a closed reading doesn't end an override", 'step',
        'if raw == CLOSED_READ and record.override is not None:',
        'if False:',
        'tests/test_properties_window.py::test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes',
    ),
    'C18': Mutant(
        "a pause isn't stored at once or in its own file", '_store_write',
        'return lambda: self._host.room_state.save_now(SECTION_OPENINGS)',
        'return lambda: None',
        'tests/test_door_window.py::test_a_pause_is_stored_as_the_record_a_start_reads',
    ),
    'C19': Mutant(
        'opening_state pauses on no reading', 'opening_state',
        'return OPENING_PAUSED if paused else OPENING_CLOSED',
        'return OPENING_PAUSED',
        'tests/test_properties_window.py::test_no_reading_never_starts_a_pause_and_never_ends_one',
    ),
    'C20': Mutant(
        "a target isn't converted on a unit switch", 'target_in_unit',
        'if set_in == UNIT_F and unit == UNIT_C:\n        return snap_to_step((value - 32) * 5 / 9, step)',
        'if False:\n        return snap_to_step((value - 32) * 5 / 9, step)',
        'tests/test_properties_restart.py::test_a_target_is_converted_on_a_unit_switch',
    ),
    'C22': Mutant(
        'only the last command is an echo', 'own',
        'if observed in (self.last, self.prior):\n            return True',
        'if observed == self.last:\n            return True',
        'tests/test_properties_fan.py::test_a_slow_heads_own_echo_is_not_a_hold_one_write_behind',
    ),
    'C23': Mutant(
        'the echo exception is gone, on a cloud head', 'own',
        'if observed in (self.last, self.prior):\n            return True',
        'if observed == self.last:\n            return True',
        'tests/test_properties_fan.py::test_the_exception_on_a_cloud_head',
    ),
    'C24': Mutant(
        'a start ignores the time since the change', 'mode_change_stamp',
        'ago = max(0.0, wall - stored)',
        'ago = 0.0',
        'tests/test_properties_mode.py::test_a_start_keeps_what_is_left_of_the_hold_and_never_more',
    ),
    'C25': Mutant(
        'no change on record still holds', 'mode_change_stamp',
        'if stored is None:\n        return None\n    ago',
        'if stored is None:\n        return now\n    ago',
        'tests/test_properties_mode.py::test_no_stored_change_holds_nothing',
    ),
    'C26': Mutant(
        'an infinite stored time reads as a time', 'finite_number',
        'return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)',
        'return isinstance(value, (int, float)) and not isinstance(value, bool)',
        'tests/test_properties_mode.py::test_any_stored_record_loads_as_a_readable_record',
    ),
    'C27': Mutant(
        'an override can be paused', 'opening_state',
        'if raw == OPEN_READ or paused or prior == OPENING_OVERRIDDEN:\n            return OPENING_OVERRIDDEN',
        'if raw == OPEN_READ or paused or prior == OPENING_OVERRIDDEN:\n            return OPENING_PAUSED',
        'tests/test_properties_window.py::test_an_override_is_never_paused_and_a_closed_reading_ends_it',
    ),
    'C28': Mutant(
        'the open delay is ignored', 'opening_state',
        'return OPENING_PAUSED if now - since >= open_delay else OPENING_OPENING',
        'return OPENING_PAUSED',
        'tests/test_properties_window.py::test_the_delays_decide_a_pause_and_a_resume',
    ),
    'C29': Mutant(
        'a close delay has no wakeup', 'opening_due',
        'if state == OPENING_CLOSING:\n        return since + close_delay\n    return None',
        'return None',
        'tests/test_properties_window.py::test_only_a_running_delay_has_a_wakeup',
    ),
    'C30': Mutant(
        'unavailable reads as closed', 'opening_reading',
        'if state == STATE_OFF:\n        return CLOSED_READ',
        'if state in (STATE_OFF, STATE_UNAVAILABLE):\n        return CLOSED_READ',
        'tests/test_properties_window.py::test_only_on_and_off_are_readings',
    ),
    'C21': Mutant(
        "a live unit switch doesn't reload", '_on_core_config_update',
        'self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)',
        'pass',
        'tests/test_properties_restart.py::test_a_live_unit_switch_reloads_with_a_held_fan_and_the_hold_kept',
    ),
    'C31': Mutant(
        "a slow head's older write reads as a pick", 'own',
        'return observed in self.older_commands()',
        'return False',
        'tests/test_properties_fan.py::test_no_person_no_hold_however_far_a_head_lags',
    ),
    'C32': Mutant(
        'older writes still count once the head has caught up', 'caught_up',
        'memory = self.older\n        if not memory or observed not in memory:',
        'memory = None\n        if not memory or observed not in memory:',
        'tests/test_properties_fan.py::test_the_exception_is_exactly_the_last_two',
    ),
    'C33': Mutant(
        'the whole ladder reads as an older write', 'own',
        'return observed in self.older_commands()',
        'return observed in FAN_LADDER',
        'tests/test_properties_fan.py::test_a_lagging_heads_pick_of_a_speed_mxz_never_wrote_is_held',
    ),
    'C34': Mutant(
        'any report of the last write reads as caught up, a late one too', 'caught_up',
        '        if observed == self.last or observed != self.prior:\n',
        '        if observed == self.last:\n            memory.clear()\n            memory.append(observed)\n        elif observed != self.prior:\n',
        'tests/test_properties_fan.py::test_no_person_no_hold_when_a_head_reports_every_write_in_order',
    ),
    'C35': Mutant(
        'a start forgets where a repeated write came first', 'extra_state_attributes',
        'if older := self.coordinator.fan.older_record(self._zone.climate_id):',
        'if older := self.coordinator.fan.older_commands(self._zone.climate_id):',
        'tests/test_fan_lag_memory.py::test_a_start_keeps_where_a_repeated_write_came_first',
    ),
    'C36': Mutant(
        'a report drops nothing before it', 'caught_up',
        '            while memory[0] != observed:\n                memory.popleft()\n',
        '',
        'tests/test_fan_lag_memory.py::test_a_report_drops_the_writes_before_it_and_no_others',
    ),
    'C37': Mutant(
        'a report of the last write empties the memory, so the next write brings back a speed the head has passed', 'caught_up',
        '        return changed\n',
        '        if observed == self.last and len(memory) == 1:\n            memory.clear()\n        return changed\n',
        'tests/test_fan_lag_memory.py::test_a_prompt_heads_passed_speed_holds_during_the_next_writes_window',
    ),
    'C39': Mutant(
        "a start's record loses a queued write of auto", 'fan_record',
        '    while older and older[0] == FAN_AUTO:\n        del older[0]\n',
        '    older = [token for token in older if token != FAN_AUTO]\n',
        'tests/test_properties_fan.py::test_no_person_no_hold_when_a_head_reports_every_write_in_order',
    ),
    'C38': Mutant(
        'a speed written twice is kept only once', 'wrote',
        '        memory.append(token)\n',
        '        if token not in memory:\n            memory.append(token)\n',
        'tests/test_fan_lag_memory.py::test_a_later_write_of_a_speed_outlives_a_report_past_its_first',
    ),
    'C40': Mutant(
        'a write after a head has caught up brings back the speed before the last', 'wrote',
        '        if not memory:\n            memory.extend(',
        '        if len(memory) <= 1:\n            memory.clear()\n            memory.extend(',
        'tests/test_properties_fan.py::test_the_exception_is_still_the_last_two_with_a_write_in_flight',
    ),
    'C41': Mutant(
        'the memory keeps only the latest write', 'wrote',
        '        memory.append(token)\n',
        '        memory.clear()\n        memory.append(token)\n',
        'tests/test_properties_fan.py::test_no_person_no_hold_when_a_head_jumps_to_its_latest_write',
    ),
    # The mode's record and each head's command (RecordLeadsHeads, ci-apply).
    'C42': Mutant(
        'the heads are sent the mode before it is recorded', '_apply',
        'self._write_back_mode(part)\n',
        'await self.deliveries.async_wait(tasks)\n                self._write_back_mode(part)\n',
        'tests/test_head_fault.py::test_a_head_is_sent_a_new_mode_only_once_it_is_the_recorded_mode',
    ),
    'C43': Mutant(
        "a head's unexpected error fails the plan", '_async_call',
        'if isinstance(err, HomeAssistantError):\n                raise',
        'if True:\n                raise',
        'tests/test_head_fault.py::test_a_head_that_raises_during_a_mode_change_doesnt_undo_the_change[RuntimeError]',
    ),
    # Changes made at a head and MXZ's memory of its own writes (OwnWriteNeverAdopted,
    # HoldBlocksWrite, StartWriteNeverAdopted in ci-adopt and ci-adopt-restart).
    'C44': Mutant(
        "a write to a head isn't remembered", 'remember_writes',
        'if name in payload:',
        'if False:',
        'tests/test_accept_head_changes.py::test_a_head_two_writes_behind_is_not_adopted',
    ),
    'C45': Mutant(
        "a change at the head doesn't stop MXZ's write", 'holds_change',
        'return holding and adopted(adoption())',
        'return False and adopted(adoption())',
        'tests/test_accept_head_changes.py::test_a_change_must_hold_and_isnt_overwritten_while_it_does',
    ),
    'C46': Mutant(
        'a start loses the memory', 'resume',
        'writes = (saved.item(SECTION_HEAD_WRITES, cid) or {}) if saved.unit == unit else {}',
        'writes = {}',
        'tests/test_accept_head_changes.py::test_the_memory_rides_a_reload_with_the_head_still_behind',
    ),
    'C52': Mutant(
        'a restart loses the memory', '_resume',
        'for name, memory in room.head_writes.items():',
        'for name, memory in (room.head_writes if self.hass.is_running else {}).items():',
        'tests/test_accept_head_changes.py::test_a_pre_restart_write_shown_late_is_not_adopted[after_startup_quiet]',
    ),
    'C47': Mutant(
        "a report doesn't pass the writes before it", 'caught_up',
        'while not same(reported, self._entries[0][0]):\n            self._entries.popleft()\n            passed = True',
        'pass',
        'tests/test_accept_head_changes.py::test_a_report_of_the_last_write_ends_all_the_others',
    ),
    'C48': Mutant(
        "a person's value replaces the writes still coming", 'seen',
        'if self._entries and (not self._entries[0][1] or len(self._entries) == self._size):\n'
        '            self._entries.popleft()\n        self._entries.appendleft([shown, 0])',
        'self._entries.clear()\n        self._entries.append([shown, 0])',
        'tests/test_accept_head_changes.py::test_a_person_replaces_what_the_head_last_showed_not_what_is_still_coming',
    ),
    # A write still queued at a head stays remembered (QueuedWriteKept in ci-adopt and ci-adopt-restart).
    'C49': Mutant(
        'a rewrite of the last value is kept once', 'wrote',
        'self._entries[-1][1] += 1',
        'pass',
        'tests/test_accept_head_changes.py::test_a_rewrite_still_queued_is_not_adopted_over_a_later_press',
    ),
    'C50': Mutant(
        "a person's value replaces a value with copies still to come", 'seen',
        '(not self._entries[0][1] or len(self._entries) == self._size)',
        'True',
        'tests/test_accept_head_changes.py::test_a_rewrite_still_queued_is_not_adopted_over_a_later_press',
    ),
    'C51': Mutant(
        'a report that moves nothing shows a copy', 'caught_up',
        'if (passed or moved) and self._entries[0][1]:',
        'if self._entries[0][1]:',
        'tests/test_accept_head_changes.py::test_a_report_that_moves_nothing_shows_no_copy',
    ),
    'C53': Mutant(
        'a write of what the head shows adds a copy', 'wrote',
        'same(shown, value) and same(shown, self._entries[-1][0])\n'
        '            and not any(copies for _, copies in list(self._entries)[:-1])',
        'False',
        'tests/test_accept_head_changes.py::test_off_and_on_at_the_head_count_after_target_changes',
    ),
    'C54': Mutant(
        'a write of what the head shows adds nothing behind another write', 'wrote',
        'same(shown, value) and same(shown, self._entries[-1][0])\n'
        '            and not any(copies for _, copies in list(self._entries)[:-1])',
        'same(shown, value)',
        'tests/test_accept_head_changes.py::test_a_write_of_what_the_head_shows_adds_no_copy',
    ),
    # A call the head refused is never shown: the copy it counted is taken back (review 3 of
    # smfix1111). The model has no refused call, so this is a code-only check.
    'C55': Mutant(
        "a refused write's copy is kept", '_async_call',
        'self._host.take_back_head_write(climate_id, counted)',
        'pass',
        'tests/test_accept_head_changes.py::test_a_refused_write_leaves_no_copy_to_take_a_later_press',
    ),
    # A write of the value the head shows behind another write still to come counts a copy
    # (shape B, fix round 5 of smfix1111): the old no-copy test, the last value remembered.
    'C56': Mutant(
        'a rewrite of the shown value behind another write counts no copy', 'wrote',
        'same(shown, value) and same(shown, self._entries[-1][0])\n'
        '            and not any(copies for _, copies in list(self._entries)[:-1])',
        'same(shown, value) and same(shown, self._entries[-1][0])',
        'tests/test_accept_head_changes.py::test_a_rewrite_of_the_shown_value_behind_another_write_is_kept',
    ),
    # A write of the value the head shows counts no copy when it is the last value remembered
    # and no value before it counts one, even with an older shown value still remembered, as
    # after a restart (review 6 of smfix1111): fix round 5's no-copy test, all that is remembered.
    'C57': Mutant(
        'a rewrite of the shown value behind an older shown value counts a copy', 'wrote',
        'same(shown, value) and same(shown, self._entries[-1][0])\n'
        '            and not any(copies for _, copies in list(self._entries)[:-1])',
        'same(shown, value) and len(self._entries) == 1',
        'tests/test_accept_head_changes.py::test_after_a_restart_a_rewrite_of_the_shown_mode_takes_no_later_press',
    ),
    # A head's report during a plan waits out the request cooldown on Home Assistant before
    # 2025.11, as it does from 2025.11 (review 8 of smfix1111). Refreshed right after the plan,
    # it wrote the target a second time, whose copy took a later press back to it. The broken
    # branch runs only before 2025.11, so this one runs and is caught on the py312-ha2024.12.5
    # lane and shows N/A on later ones; CI's py3.12-ha2024.12.5 job runs its test. It is not in
    # formal_area's adopt area, which runs on any lane.
    'C58': Mutant(
        "a report during a plan is refreshed right after it, before HA 2025.11", 'request_for_report',
        'self._defer_request()',
        'self._host.hass.async_create_task(self._request())',
        'tests/test_accept_head_changes.py::test_one_target_change_takes_no_later_press_back_to_it',
        HA_BEFORE_2025_11,
    ),
    'H1': Mutant(
        'the hold no longer gates a change', 'shared_mode',
        'if proposed != current and allowed and (standoff or not still):',
        'if proposed != current and (standoff or not still):',
        'tests/test_modehold_properties.py::test_arbitration_property[hold_blocks_every_change-ROOMS3]',
    ),
    'H2': Mutant(
        'votes count while a room is mid-run', 'arbitrate',
        'gate_open = commanding_silent and not engaged',
        'gate_open = commanding_silent',
        'tests/test_modehold_properties.py::test_arbitration_property[vote_never_beats_a_call_or_a_run-ROOMS3]',
    ),
    'H3': Mutant(
        'Comfort priority crosses tiers', '_rank',
        'return (demand.priority[0], -comfort_boost(demand), demand.priority[1])',
        'return (-comfort_boost(demand), demand.priority[0], demand.priority[1])',
        'tests/test_modehold_properties.py::test_arbitration_property[highest_rank_decides-ROOMS3]',
    ),
    'H4': Mutant(
        'a vote change is exempt with the gate shut', 'arbitrate',
        'read_only = full != plain and gate_open',
        'read_only = full != plain',
        'tests/test_modehold_properties.py::test_arbitration_property[exempt_only_with_the_gate_open-ROOMS3]',
    ),
    'H5': Mutant(
        'by_safety ignores the direction', 'arbitrate',
        'd.direction == full and d.priority[0] <= PRIORITY_SAFETY for d in active',
        'd.priority[0] <= PRIORITY_SAFETY for d in active',
        'tests/test_modehold_properties.py::test_arbitration_property[by_safety_means_a_safety_want_agrees-ROOMS3]',
    ),
    'H6': Mutant(
        'Resting mode ignored', 'shared_mode',
        'rest = resting if resting in (MODE_COOL, MODE_HEAT) else current',
        'rest = current',
        'tests/test_modehold_properties.py::test_arbitration_property[nothing_calling_rests-ROOMS3]',
    ),
    'H7': Mutant(
        'a stored time in the future holds longer', 'mode_change_stamp',
        'ago = max(0.0, wall - stored)',
        'ago = wall - stored',
        'tests/test_modehold_properties.py::test_a_start_keeps_what_is_left_of_the_hold_and_never_more',
    ),
    'H8': Mutant(
        'an unreadable record holds nothing', 'mode_record',
        '    return ModeRecord(changed_at=now, **shared), True',
        '    return ModeRecord(changed_at=None, **shared), True',
        'tests/test_modehold_properties.py::test_a_start_reads_the_record_or_holds_the_full_window',
    ),
    'H9': Mutant(
        "a room's run in the voted mode is not stamped", 'mode_write_back',
        'stamp=True,',
        'stamp=False,',
        'tests/test_modehold_findings.py::test_f2_a_reload_forgets_that_a_room_now_runs_in_the_voted_mode[control_no_reload]',
    ),
    'H10': Mutant(
        'Resting mode always heat', 'shared_mode',
        'rest = resting if resting in (MODE_COOL, MODE_HEAT) else current',
        'rest = MODE_HEAT',
        'tests/test_modehold_findings.py::test_f1_resting_mode_takes_the_mode_from_a_room_still_running[control_last_used]',
    ),
    'H11': Mutant(
        'the select does not restore the mode', 'async_added_to_hass',
        '                if last.state in _OPTIONS:\n                    restored = last.state',
        '                if last.state in _OPTIONS:\n                    pass',
        'tests/test_hold_promises.py::test_a_start_takes_the_newer_of_the_record_and_the_restore_data[record_without_a_mode]',
    ),
    'H12': Mutant(
        'Resting mode takes the mode from a run', 'arbitrate',
        '    if running:\n        resting = None',
        '    if False:\n        resting = None',
        'tests/test_modehold_findings.py::test_f1_resting_mode_takes_the_mode_from_a_room_still_running[finding]',
    ),
    'H13': Mutant(
        'the plan never says a room runs in the shared mode', 'decide',
        '    running = current in held_engages\n',
        '    running = False\n',
        'tests/test_hold_promises.py::test_resting_mode_waits_for_the_run_then_rests',
    ),
    'H14': Mutant(
        'a start forgets that votes chose the mode', 'resumed_mode',
        '        mode, by_votes = record.mode, record.by_votes\n',
        '        mode, by_votes = record.mode, False\n',
        'tests/test_hold_promises.py::test_a_restart_keeps_that_votes_chose_the_mode[2]',
    ),
    'H15': Mutant(
        'a change votes made is not written to the record', 'mode_write_back',
        'stamp=not by_hands_off,\n            record=True,',
        'stamp=not by_hands_off,\n            record=False,',
        'tests/test_modehold_findings.py::test_f2_a_reload_forgets_that_a_room_now_runs_in_the_voted_mode[finding]',
    ),
    'H16': Mutant(
        "the select's restore data always beats the record", 'resumed_mode',
        '        or record.mode_at >= restored_at\n',
        '        or False\n',
        'tests/test_modehold_findings.py::test_f3_a_crash_soon_after_a_change_restores_the_old_mode_inside_the_hold[finding]',
    ),
    'H17': Mutant(
        'the record always beats newer restore data', 'resumed_mode',
        '        or record.mode_at >= restored_at\n',
        '        or True\n',
        'tests/test_hold_promises.py::test_a_start_takes_the_newer_of_the_record_and_the_restore_data[restore_data_newer]',
    ),
    'H18': Mutant(
        'a paused room still votes', 'decide',
        '        if room.paused:\n            demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED\n',
        '        if False:\n            demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED\n',
        'tests/test_door_window.py::test_a_paused_room_never_votes',
    ),
    'H19': Mutant(
        'a pause restamps the hold', '_transition',
        '            record.paused_at = now\n',
        '            record.paused_at = now\n            self._host._stamp_mode_change()\n',
        'tests/test_door_window.py::test_a_pause_and_a_resume_never_touch_the_hold',
    ),
    'H20': Mutant(
        'no reading starts a pause', 'opening_state',
        '    return OPENING_PAUSED if paused else OPENING_CLOSED\n',
        '    return OPENING_PAUSED\n',
        'tests/test_door_window.py::test_no_reading_never_starts_a_pause_and_never_ends_one[unavailable]',
    ),
    'H21': Mutant(
        "a person's change on a paused head is not a takeover", 'take_over',
        '        self._records[zone.slug].override = "manual"\n',
        '        pass\n',
        'tests/test_door_window.py::test_a_manual_change_holds_until_the_window_closes',
    ),
    'H22': Mutant(
        'a pause turns the room Off', '_transition',
        '            record.paused_at = now\n',
        '            record.paused_at = now\n            zone.enable = False\n',
        'tests/test_door_window.py::test_the_rooms_mode_is_never_rewritten_and_off_stays_off',
    ),
    'H23': Mutant(
        'a restart forgets the pause', '__init__',
        '            zone.slug: OpeningRecord.restored((stored or {}).get(zone.climate_id))\n',
        '            zone.slug: OpeningRecord.restored(None)\n',
        'tests/test_door_window.py::test_a_pause_outlives_a_restart',
    ),
    'H24': Mutant(
        'the pause sends the coil-dry fan run first', '_head_apply',
        'return HeadApply(cid, steps=(FanRead(MODE_OFF), Queue(_PAUSE_OFF)))',
        'return HeadApply(cid, steps=(FanRead(MODE_OFF), Queue(replace(_PAUSE_OFF, mode=idle_for(cid, shared.idle_action, head.coil_dry().last_run, head.head, shared.coil_dry_seconds, shared.clock, dry_waiting=head.coil_dry().waiting).idle))))',
        'tests/test_door_window.py::test_open_for_the_open_delay_pauses_straight_to_off',
    ),

}


@functools.cache
def _functions(text: str) -> dict[str, list[tuple[int, int]]]:
    """Where each function or method is, by name, as offsets into ``text``."""
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    spans: dict[str, list[tuple[int, int]]] = {}
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.setdefault(node.name, []).append((starts[node.lineno - 1], starts[node.end_lineno]))
    return spans


def locate(root: Path, mutant: Mutant) -> list[tuple[Path, int]]:
    """Every place ``mutant.old`` occurs inside a function named ``mutant.symbol``.

    A mutant applies only where this finds exactly one place.
    """
    found = []
    for path in sorted((root / PACKAGE).rglob("*.py")):
        text = path.read_text()
        for start, end in _functions(text).get(mutant.symbol, []):
            at = text.find(mutant.old, start, end)
            while at != -1 and at + len(mutant.old) <= end:
                found.append((path, at))
                at = text.find(mutant.old, at + 1, end)
    return found


def apply(root: Path, mutant: Mutant) -> Path:
    """Break the copy at ``root``; return the file broken. ValueError unless exactly one place."""
    places = locate(root, mutant)
    if len(places) != 1:
        raise ValueError(f"its text is in {len(places)} places in functions named {mutant.symbol}, not 1")
    path, at = places[0]
    text = path.read_text()
    path.write_text(text[:at] + mutant.new + text[at + len(mutant.old):])
    return path


CAUGHT, NOT_APPLICABLE, MISSED, PROBE_ERROR = "caught", "not applicable", "missed", "probe errors"


@functools.cache
def probe(python: str, needs: Needs) -> str | None:
    """None if the lane of ``python`` has what ``needs`` asks for, else why not.

    RuntimeError if the probe fails or answers anything but "yes" or "no <why>".
    """
    try:
        done = subprocess.run(
            [python, "-I", "-c", needs.probe], capture_output=True, text=True, check=False, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise RuntimeError(f"the probe for {needs.what} did not run: {err}") from err
    answer = done.stdout.strip()
    if done.returncode == 0 and answer == "yes":
        return None
    if done.returncode == 0 and answer.startswith("no ") and answer[3:].strip():
        return answer[3:].strip()
    said = (done.stderr.strip().splitlines() or [answer or "nothing"])[-1]
    raise RuntimeError(f"the probe for {needs.what} exited {done.returncode}: {said}")


def outcome(name: str, python: str, out: Path) -> str:
    """CAUGHT, NOT_APPLICABLE, MISSED or PROBE_ERROR for mutant ``name`` on the lane of ``python``."""
    needs = MUTANTS[name].needs
    if needs is not None:
        try:
            why = probe(python, needs)
        except RuntimeError as err:
            print(f"FAIL code {name}: {err}", flush=True)
            return PROBE_ERROR
        if why is not None:
            print(f"N/A  code {name} not run on this lane: needs {needs.what} {why}", flush=True)
            return NOT_APPLICABLE
    return CAUGHT if run(name, python, out) else MISSED


def verdict(outcomes: dict[str, str]) -> tuple[str, int]:
    """The counts line and the exit status for a run's outcomes, by mutant id."""
    count = {kind: [name for name, got in outcomes.items() if got == kind]
             for kind in (CAUGHT, NOT_APPLICABLE, MISSED, PROBE_ERROR)}
    line = ", ".join(f"{kind} {len(names)}" + (f" ({' '.join(names)})" if names and kind != CAUGHT else "")
                     for kind, names in count.items())
    if count[MISSED] or count[PROBE_ERROR]:
        return f"{line}\nMUTANTS_MISSED", 1
    if not count[CAUGHT]:
        return f"{line}\nMUTANTS_NONE_APPLICABLE", 1
    return f"{line}\nMUTANTS_ALL_CAUGHT", 0


def run(name: str, python: str, out: Path) -> bool:
    """Break a scratch copy with mutant ``name`` and run its test there; True if the test failed.

    It does not probe ``needs``. tools/formal_area.py and verification/verify.py call it for
    mutants that must be caught on every lane, and tests/test_formal_contract.py keeps mutants
    with ``needs`` out of those.
    """
    mutant = MUTANTS[name]
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / "repo"
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", ".hypothesis"))
        try:
            broken = apply(copy, mutant)
        except ValueError as err:
            print(f"FAIL code {name}: {err}: re-anchor it", flush=True)
            return False
        done = subprocess.run(
            [python, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--show-capture=no", "--tb=line", mutant.test],
            cwd=copy, capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    (out / f"code-{name}.out").write_text(done.stdout + done.stderr)
    tail = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    caught = done.returncode == 1 and "failed" in tail
    where = broken.relative_to(copy).as_posix()
    print(f"{'OK  ' if caught else 'FAIL'} code {name} ({mutant.what}; {where} {mutant.symbol}): "
          f"exit {done.returncode}, {tail}", flush=True)
    return caught


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("only", nargs="*", help="mutant ids (default: all)")
    parser.add_argument("--out", type=Path, help="where each run's pytest output is kept (default: a temp folder)")
    args = parser.parse_args()
    unknown = sorted(set(args.only) - set(MUTANTS))
    if unknown:
        sys.exit(f"no such mutant: {' '.join(unknown)}")
    python = os.environ.get("MXZ_PYTHON")
    if not python:
        sys.exit("set MXZ_PYTHON to a lane venv's python with hypothesis in it")
    out = args.out or Path(tempfile.mkdtemp(prefix="mxz-code-mutants-"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"output in {out}", flush=True)
    outcomes = {name: outcome(name, python, out) for name in args.only or MUTANTS}
    summary, status = verdict(outcomes)
    print(summary, flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
