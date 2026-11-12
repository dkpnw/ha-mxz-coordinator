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
test there with MXZ_PYTHON, a lane venv's python with hypothesis in it. The exit status is 0
only if every named test failed on its broken copy.
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
class Mutant:
    what: str  # what breaks, in words
    symbol: str  # the function whose body holds ``old``
    old: str
    new: str
    test: str  # a pytest node id that must fail on the broken copy


MUTANTS: dict[str, Mutant] = {
    'C1': Mutant(
        'the hold is ignored', '_compute',
        'allowed = elapsed is None or elapsed >= self.hysteresis\n        engaged',
        'allowed = True\n        engaged',
        'tests/test_properties_mode.py::test_no_flip_inside_the_hold_and_no_wait_after_it',
    ),
    'C2': Mutant(
        'the hold runs long', '_compute',
        'allowed = elapsed is None or elapsed >= self.hysteresis\n        engaged',
        'allowed = elapsed is None or elapsed >= self.hysteresis * 2\n        engaged',
        'tests/test_properties_mode.py::test_no_flip_inside_the_hold_and_no_wait_after_it',
    ),
    'C3': Mutant(
        "a flip isn't stamped", '_write_back_mode',
        'if not plan.get("mode_by_hands_off"):\n                self._stamp_mode_change()',
        'if False:\n                self._stamp_mode_change()',
        'tests/test_properties_mode.py::test_no_flip_inside_the_hold_and_no_wait_after_it',
    ),
    'C4': Mutant(
        'a start restarts the hold', 'mode_change_stamp',
        'ago = max(0.0, dt_util.utcnow().timestamp() - stored)',
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
        'a start ignores the record', 'restore_shared_mode',
        'mode, by_votes = record.mode, record.by_votes\n        else:',
        'mode, by_votes = restored or MODE_COOL, False\n        else:',
        'tests/test_properties_mode.py::test_a_start_resumes_the_recorded_mode',
    ),
    'C7': Mutant(
        "Resting mode doesn't wait", 'arbitrate',
        'if running:\n        resting = None',
        'if False:\n        resting = None',
        'tests/test_properties_mode.py::test_a_run_in_the_shared_mode_keeps_it_from_resting_mode',
    ),
    'C8': Mutant(
        "a departure isn't a hold", '_observe_latch',
        'if not (seeding or departed):',
        'if not seeding:',
        'tests/test_properties_fan.py::test_a_pick_is_never_written_over_until_it_is_handed_back',
    ),
    'C9': Mutant(
        "a hold doesn't stop the write", 'async_apply',
        "if held:\n            return  # latched: leave the user's fan pick untouched",
        "if False:\n            return  # latched: leave the user's fan pick untouched",
        'tests/test_properties_fan.py::test_a_pick_is_never_written_over_until_it_is_handed_back',
    ),
    'C10': Mutant(
        "a restored hold isn't kept", '_reconcile_restored_hold',
        'if held:\n            self._hold_at(climate_id, observed)\n            return True\n        echo =',
        'if False:\n            self._hold_at(climate_id, observed)\n            return True\n        echo =',
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
        "a paused head isn't sent off", '_apply',
        'task = self.deliveries.queue(zone.climate_id, MODE_OFF, 0.0, 0.0)\n                if task is not None:\n                    tasks.append(task)\n                continue',
        'continue',
        'tests/test_properties_window.py::test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes',
    ),
    'C15': Mutant(
        'a paused room still votes', '_compute',
        'if paused:\n                demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED',
        'if False:\n                demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED',
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
        "a pause isn't stored", '_save',
        'self._store.async_delay_save(lambda: data, 0)',
        'pass',
        'tests/test_properties_restart.py::test_a_start_changes_nothing_anyone_can_see',
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
        'ago = max(0.0, dt_util.utcnow().timestamp() - stored)',
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
        "a start's record loses a queued write of auto", 'older_record',
        '        while record and record[0] == FAN_AUTO:\n            del record[0]\n',
        '        record = [token for token in record if token != FAN_AUTO]\n',
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
        'self._write_back_mode(plan, engages)\n        await self.deliveries.async_wait(tasks)',
        'await self.deliveries.async_wait(tasks)\n        self._write_back_mode(plan, engages)',
        'tests/test_head_fault.py::test_a_head_is_sent_a_new_mode_only_once_it_is_the_recorded_mode',
    ),
    'C43': Mutant(
        "a head's unexpected error fails the plan", '_async_issue',
        'if isinstance(err, HomeAssistantError):\n                raise',
        'if True:\n                raise',
        'tests/test_head_fault.py::test_a_head_that_raises_during_a_mode_change_doesnt_undo_the_change[RuntimeError]',
    ),
    # Changes made at a head and MXZ's memory of its own writes (OwnWriteNeverAdopted,
    # HoldBlocksWrite, StartWriteNeverAdopted in ci-adopt).
    'C44': Mutant(
        "a write to a head isn't remembered", 'remember_head_write',
        'if name in payload:',
        'if False:',
        'tests/test_accept_head_changes.py::test_a_head_two_writes_behind_is_not_adopted',
    ),
    'C45': Mutant(
        "a change at the head doesn't stop MXZ's write", '_head_change_settling',
        'return (climate_id, "adopt") in self._heal_timers and bool(',
        'return False and bool(',
        'tests/test_accept_head_changes.py::test_a_change_must_hold_and_isnt_overwritten_while_it_does',
    ),
    'C46': Mutant(
        'a reload loses the memory, as a process start does', '__init__',
        'hass.data.setdefault(\n            f"{DOMAIN}_head_writes", {}\n        )',
        '{}',
        'tests/test_accept_head_changes.py::test_the_memory_rides_a_reload_with_the_head_still_behind',
    ),
    'C47': Mutant(
        "a report doesn't pass the writes before it", 'caught_up',
        'while not same(reported, self._values[0]):\n                self._values.popleft()',
        'pass',
        'tests/test_accept_head_changes.py::test_a_report_of_the_last_write_ends_all_the_others',
    ),
    'C48': Mutant(
        "a person's value replaces the writes still coming", 'seen',
        'if self._values:\n            self._values.popleft()\n        self._values.appendleft(shown)',
        'self._values.clear()\n        self._values.append(shown)',
        'tests/test_accept_head_changes.py::test_a_person_replaces_what_the_head_last_showed_not_what_is_still_coming',
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
        'ago = max(0.0, dt_util.utcnow().timestamp() - stored)',
        'ago = dt_util.utcnow().timestamp() - stored',
        'tests/test_modehold_properties.py::test_a_start_keeps_what_is_left_of_the_hold_and_never_more',
    ),
    'H8': Mutant(
        'an unreadable record holds nothing', 'async_load_mode_record',
        '    return ModeRecord(changed_at=dt_util.utcnow().timestamp(), **shared)',
        '    return ModeRecord(changed_at=None, **shared)',
        'tests/test_modehold_properties.py::test_a_start_reads_the_record_or_holds_the_full_window',
    ),
    'H9': Mutant(
        "a room's run in the voted mode is not stamped", '_write_back_mode',
        '            self._mode_by_votes = False\n            self._stamp_mode_change()\n            self._record_mode()\n',
        '            self._mode_by_votes = False\n            self._record_mode()\n',
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
        'the plan never says a room runs in the shared mode', '_compute',
        '        running = current in held_engages\n',
        '        running = False\n',
        'tests/test_hold_promises.py::test_resting_mode_waits_for_the_run_then_rests',
    ),
    'H14': Mutant(
        'a start forgets that votes chose the mode', 'restore_shared_mode',
        '            mode, by_votes = record.mode, record.by_votes\n',
        '            mode, by_votes = record.mode, False\n',
        'tests/test_hold_promises.py::test_a_restart_keeps_that_votes_chose_the_mode[2]',
    ),
    'H15': Mutant(
        'a change votes made is not written to the record', '_write_back_mode',
        '            self.async_update_listeners()  # let the shared-mode select re-render\n            self._record_mode()\n',
        '            self.async_update_listeners()  # let the shared-mode select re-render\n',
        'tests/test_modehold_findings.py::test_f2_a_reload_forgets_that_a_room_now_runs_in_the_voted_mode[finding]',
    ),
    'H16': Mutant(
        "the select's restore data always beats the record", 'restore_shared_mode',
        '            or record.mode_at >= restored_at\n',
        '            or False\n',
        'tests/test_modehold_findings.py::test_f3_a_crash_soon_after_a_change_restores_the_old_mode_inside_the_hold[finding]',
    ),
    'H17': Mutant(
        'the record always beats newer restore data', 'restore_shared_mode',
        '            or record.mode_at >= restored_at\n',
        '            or True\n',
        'tests/test_hold_promises.py::test_a_start_takes_the_newer_of_the_record_and_the_restore_data[restore_data_newer]',
    ),
    'H18': Mutant(
        'a paused room still votes', '_compute',
        '            if paused:\n                demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED\n',
        '            if False:\n                demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED\n',
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
        'the pause sends the coil-dry fan run first', '_apply',
        '                task = self.deliveries.queue(zone.climate_id, MODE_OFF, 0.0, 0.0)\n',
        '                task = self.deliveries.queue(\n                    zone.climate_id, self._idle_act_for(zone.climate_id), 0.0, 0.0\n                )\n',
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


def run(name: str, python: str, out: Path) -> bool:
    """Break a scratch copy with mutant ``name`` and run its test there; True if the test failed."""
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
    good = True
    for name in args.only or MUTANTS:
        good = run(name, python, out) and good
    print("MUTANTS_ALL_CAUGHT" if good else "MUTANTS_MISSED", flush=True)
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
