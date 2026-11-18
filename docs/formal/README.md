# Formal checks of the coordinator

Three checks of one set of rules: the coordinator's mode arbitration, the hold between mode
changes (dwell, anti-flip), the door/window pause, the manual fan hold, restart continuity, the
apply as separate steps, and changes made at a head.

| File | What it is |
| --- | --- |
| [STATECHART.md](STATECHART.md) | The machine as a chart, each transition mapped to its code. |
| [tla/MXZCoordinator.tla](tla/MXZCoordinator.tla) | The same machine in TLA+. TLC checks every order of steps at small bounds. |
| `tests/test_properties_*.py` | Hypothesis properties on the real integration, in the normal suite on every lane. |
| [PARKED-FINDINGS.md](PARKED-FINDINGS.md) | What these checks found, and the accepted exception on cloud heads. |
| [charts/](charts) | The user flows as the pstack `/statechart` skill draws them from the spec, cut small enough to draw whole. [STATECHART.md](STATECHART.md#user-flows-drawn-from-the-model) shows them. |
| `tools/formal_area.py` | One subsystem's checks as one command: the fan hold, the door/window pause, changes made at a head, and the mode's record with each head's command. |
| `.claude/skills/verify-mxz-fan-hold/`, `verify-mxz-door-window/`, `verify-mxz-head-changes/`, `verify-mxz-mode-record/` | One skill per subsystem: when to run its check, what it covers at which commit, and how to tell a real bug from a modeling error. |
| [../STATE-MACHINES.md](../STATE-MACHINES.md) | Every state machine in the integration, explicit or implicit, with the invalid states found and where each is held. This folder is its formal part. |

The model finds orderings nobody thought to test, and the properties check that the code
itself does what the model says. A pass covers the model at these bounds and the examples
drawn. It doesn't cover the whole integration, and nothing here is a hardware result.

`verification/modehold/` and `.claude/skills/verify-mxz-formal/` go deeper on one part:
hands-off votes, wall-clock steps across restarts, and the select's restore data. They
stay as they are, and this model doesn't repeat them.

## The rules

| Rule | TLC invariant | Hypothesis properties on the real code |
| --- | --- | --- |
| A manual fan choice is never written over, apart from the exceptions below. | `ManualFanKept` | `test_a_pick_is_never_written_over_until_it_is_handed_back`, `test_a_cloud_heads_pick_is_never_written_over`, `test_a_lagging_heads_pick_of_a_speed_mxz_never_wrote_is_held` |
| A manual fan choice shows as a hold, across every start. | `ManualFanShown` | `test_a_pick_is_never_written_over_until_it_is_handed_back`, `test_a_start_changes_nothing_anyone_can_see` |
| MXZ doesn't hold a speed nobody touched, with a head up to one write behind. | `NoFalseHold` | `test_a_slow_heads_own_echo_is_not_a_hold_one_write_behind` |
| Nor with a head further behind, including at a start's first read, whether it jumps to the latest write or shows each in order (F1). [The limits](PARKED-FINDINGS.md#f1-limits) are out of order, past the memory's bound, and one moment for a start. | `NoFalseHoldTwoBehind`, in `ci-fan-lag`, for a head that jumps only | `test_no_person_no_hold_however_far_a_head_lags`, `test_no_person_no_hold_when_a_head_jumps_to_its_latest_write`, `test_no_person_no_hold_when_a_head_reports_every_write_in_order` |
| No mode flip inside the hold, across any start. | `NoFlipInDwell` | `test_no_flip_inside_the_hold_and_no_wait_after_it`, `test_arbitrate_never_changes_the_mode_while_the_hold_holds` |
| With the hold over and only the other mode calling, the mode changes. | `NoExtraWait` | `test_no_flip_inside_the_hold_and_no_wait_after_it` |
| A start neither restarts the hold nor forgets it. | `HoldClockKept` | `test_no_flip_inside_the_hold_and_no_wait_after_it`, `test_a_start_keeps_what_is_left_of_the_hold_and_never_more`, `test_no_stored_change_holds_nothing` |
| A start restores what was stored: the mode, the hold, the door/window state and an override. | `RestartRestores` | `test_a_start_changes_nothing_anyone_can_see`, `test_a_start_resumes_the_recorded_mode`, `test_any_stored_record_loads_as_a_readable_record` |
| After a decision no head runs against the shared mode. A run keeps Resting mode waiting. | `HeadsFollowMode` | `test_no_flip_inside_the_hold_and_no_wait_after_it`, `test_a_run_in_the_shared_mode_keeps_it_from_resting_mode` |
| A window pause stops the head, writes it no fan, and its call counts for nothing. | `PauseStopsHead` | `test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes`, `test_a_paused_room_never_keeps_or_turns_the_shared_mode`, `test_the_delays_decide_a_pause_and_a_resume`, `test_only_a_running_delay_has_a_wakeup`, `test_only_on_and_off_are_readings`, `test_no_reading_never_starts_a_pause_and_never_ends_one` |
| A person's override holds until the window reads closed. | `OverrideHolds` | `test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes`, `test_an_override_is_never_paused_and_a_closed_reading_ends_it` |
| A head is never sent a direction the record doesn't hold, so a head's error, a new input or a reload can't leave the record behind. | `RecordLeadsHeads`, in `ci-apply` | `test_a_head_is_sent_a_new_mode_only_once_it_is_the_recorded_mode`, `test_a_head_that_raises_during_a_mode_change_doesnt_undo_the_change` in `tests/test_head_fault.py` |
| A change made at a head is never MXZ's own write: the target never takes a value only MXZ put on the head, apart from the open findings below. | `OwnWriteNeverAdopted`, in `ci-adopt` and `ci-adopt-restart` | `test_no_person_no_adoption_however_a_head_lags`, `test_every_report_of_a_head_showing_writes_in_order_reads_as_own` in `tests/test_accept_head_changes.py` |
| A reload or a restart keeps MXZ's record of its writes (the room state store), so a write from before it is never adopted, inside startup quiet or after it (SM-1). | `StartWriteNeverAdopted`, in `ci-adopt` and `ci-adopt-restart` | `test_the_memory_rides_a_reload_with_the_head_still_behind`, `test_a_late_own_write_inside_startup_quiet_is_not_adopted`, `test_a_pre_restart_write_shown_late_is_not_adopted` |
| MXZ doesn't write a head's setpoint while a change made there holds. | `HoldBlocksWrite`, in `ci-adopt` | `test_a_change_must_hold_and_isnt_overwritten_while_it_does`, `test_a_press_while_mxzs_command_waits_for_the_head_is_adopted` |
| A write still queued at a head stays in the memory, each copy of a rewrite too, so it is never adopted when it lands, unless a person has pressed a value MXZ still had queued (SM-10, the part a report can tell apart). A call the head refused counts no copy. | `QueuedWriteKept`, in `ci-adopt` and `ci-adopt-restart` | `test_a_rewrite_still_queued_is_not_adopted_over_a_later_press`, `test_no_write_still_queued_is_forgotten_but_by_a_press_of_its_value`; a refused call (not in the model): `test_a_refused_write_leaves_no_copy_to_take_a_later_press` |

## Documented exceptions

Each is explicit, not a gap. In the spec, a pick's `pickKind` names which of these covers
it, and only a `strict` pick is bound with no exception. In the tests, each exception is
pinned by a passing test that fails if the behavior changes. They aren't xfails because
`tools/pytest_phases.py`, which every CI suite pass runs under, fails a run on any xfail.
[The cloud1104 parked list](#where-the-known-items-came-from) is where the first was found.

| Exception | Spec | Pinned by |
| --- | --- | --- |
| **A pick of a remembered speed reads as MXZ's echo**, and boost writes over it. It's found on cloud heads and shared with local ones. The memory is the last two distinct speeds in `FanEcho.last` and `FanEcho.prior`, which also takes in a speed that was held or handed back: hold low, hand it back from the remote, pick low again with nothing written between, and it isn't held. The same goes for the older writes in `FanEcho.older` that a head's reports haven't passed yet (F1's cost): a pick of one of those reads as an echo too. That can outlast the lag by one such pick, when a head jumps to the last of two writes of a speed. The first write after a start can also read the speed before the last write that way. | `pickKind = "echo"`; reachable by `NeverKnownExceptionLosesAPick` | `test_the_exception_is_exactly_the_last_two`, `test_the_exception_can_lose_the_pick`, `test_the_exception_on_a_cloud_head`, `test_a_head_caught_up_reads_an_older_write_as_a_pick`, `test_a_prompt_heads_passed_speed_holds_during_the_next_writes_window`, `test_a_head_that_only_reports_the_repeat_costs_one_pick_at_most` and `test_known_cost_the_first_write_after_a_start_can_read_one_speed_more` in `tests/test_fan_lag_memory.py` |
| **On a cloud head, a re-pick of the speed MXZ just wrote can't be told from its own write**, and boost's next step writes over it. Accepted: see [PARKED-FINDINGS.md](PARKED-FINDINGS.md). | Not a step of the machine | `test_a_cloud_re_pick_of_the_speed_just_written_is_overwritten` in `tests/test_cloud_heads.py` |
| **Picking the speed already showing can't be read.** Nothing changes for MXZ to see. | Not a step of the machine | `test_picking_the_speed_already_showing_is_invisible` in `tests/test_drive.py` |
| **A pick MXZ hasn't read before a stop, or made while it is down**, can read as its own speed where the token could be ours. `fan_latch.reconcile_restored_hold` documents it. | `pickKind = "outage"` | `test_s6_gesture_during_outage_not_held_before` in `tests/test_fan_hold_restore.py` |
| **A crash can lose a hold** only while its write is on its way to disk. The hold and its echo memory are in the room state store, written at once on every change (`room_state.py`; before it they rode restore data, written at a stop and every 15 minutes). The spec keeps the step that lets the saved record lag (`Dump`), so it checks the wider case. After such a crash, the switch's docstring's remembered-token edge applies. Most other speeds still hold, conservatively. | `pickKind = "crash"` | The spec's `ci-fan-crash`; a crash right after a pick or a Fan auto gesture keeps it: `test_a_crash_right_after_a_change_keeps_it[fan_hold]` and `test_a_crash_right_after_a_fan_auto_gesture_keeps_it` in `tests/test_room_state.py` |
| **A hold seeded with no memory can be adopted** if it is a rung the ladder would stay on. `fan_latch.seed_matches_boost` accepts this. | `pickKind = "seed"` | `test_restart_adopts_a_rung_held_by_hysteresis` in `tests/test_drive.py` |
| **A start can hold a speed nobody touched**: `fan_latch.reconcile_restored_hold`'s conservative false hold. A speed boost could never have written holds, even right after a hand-back. | `NoFalseHold` counts live reads only | `test_s11b_not_held_seed_above_boost_ceiling` in `tests/test_fan_hold_restore.py` |
| **A write already sent can land after a Fan auto gesture.** MXZ writes nothing after the gesture, but a head still applying the earlier write can report it next. | `pickKind = "inflight"`; `gesture` | The spec's `ci-fan-lag` |
| **A press back to a setpoint MXZ wrote again while its head lagged reads as MXZ's write once.** The memory keeps each copy of a rewrite until a report shows it, and a copy that lands while the head already shows the value shows nothing. After a person's press of another value is adopted, a press back to the rewritten one reads as that copy: the head goes back to the room's target, and once it shows that write, a press of the value is a press. A rewrite of the value the head shows counts a copy too when a value remembered before it still counts one, that is, another write of MXZ's still to come before it (the shape-B fix). If the head showed that value from the write before, a person's later press of it reads as MXZ's once (accepted under the overnight release mandate, Nov 17, 2026; shown on `HeadWriteMemory` alone). After a restart the copy before it can also be a write the head took while MXZ was down, since nothing reconciles the restored memory with what the head shows. A rewrite of the shown value counts no copy when that value is the last one remembered and nothing before it counts a copy, so an older shown value that a restart keeps in the memory adds none (review 6 of smfix1111). Two copies of the mode queued across a restart take a press back once the same way. | Each memory entry's copies, `n` | `test_a_press_back_to_a_rewritten_value_reads_as_mxzs_once`, `test_known_cost_a_rewrite_of_the_shown_value_behind_another_write_takes_a_press_back_once`, `test_known_cost_after_a_restart_a_rewrite_behind_a_write_shown_while_mxz_was_down_counts_a_copy` and `test_known_cost_two_copies_of_the_mode_across_a_restart_take_a_press_back_once` in `tests/test_accept_head_changes.py`; the restart case that counts none: `test_after_a_restart_a_rewrite_of_the_shown_mode_takes_no_later_press` |
| **A press, at the remote, of a value MXZ still has queued at a head that shows writes late** can drop that write from the memory, and when it lands after the person's next press it is adopted over it (SM-10 paths (b) and (c); an accepted known cost, the owner's decision of Nov 17, 2026). The reports are the same as in `test_up_and_back_are_both_adopted` and `test_a_press_of_a_write_the_head_has_passed_is_adopted`, where the press must be adopted, so no rule that reads only reports can give both answers. A write of the value the head already shows, still queued and landing after a press, is adopted the same way, and can pass the writes queued after it or use up a later queued write's copy (shape A, accepted under the overnight release mandate, Nov 17, 2026). Whether a real head queues commands like this is unknown; a house check is needed before a stable release. | `QueuedWriteNeverAdopted` breaks; it is not checked, and `QueuedWriteKept` is | `test_known_cost_a_press_of_the_queued_value_at_a_lagging_head_reads_as_mxzs`, `test_known_cost_a_press_of_an_older_queued_value_reads_as_mxzs`, `test_known_cost_a_write_of_the_shown_value_landing_after_a_press_is_adopted`, `test_known_cost_a_late_write_of_the_shown_value_passes_the_writes_queued_after_it` and `test_known_cost_a_late_write_of_the_shown_value_uses_up_the_later_writes_copy` in `tests/test_accept_head_changes.py` |
| **A change on the head during the close delay** lasts one compute: the window has already closed, so the room goes on closing. It's chart transition W8. | Reachable in `ci-window` | `OverrideHolds` binds only while the window isn't closed |
| **A head that refuses its command keeps its old mode** while the others run the new one, until it takes a retry at a later update (SM-5). Sending parks before runs would be a change to the delivery queue. | `refused`; `HeadsFollowMode` and `PauseStopsHead` leave a refused head out; reachable by `NeverRefused` in `ci-apply` | `test_a_head_that_raises_during_a_mode_change_doesnt_undo_the_change` |
| **A fixed standby park sends every head to the park mode**, a paused one included. `PauseStopsHead` holds because standby is outside the model. | Not a step of the machine | `docs/FEATURES.md`, standby hold |

### Where the known items came from

The first exception in the table was found by the cloud head review and parked outside this
repository for a product decision, with two more that have since been fixed: stored targets
across a unit switch, and a live unit switch. `test_a_target_is_converted_on_a_unit_switch`
and `test_a_live_unit_switch_reloads_with_a_held_fan_and_the_hold_kept` hold those now.

F1, a slow head read as a person's pick, is fixed for a head that shows its writes in order or
jumps to the latest, and is described in [PARKED-FINDINGS.md](PARKED-FINDINGS.md). No finding is
open beyond the limits listed there.

## What is modeled, and what isn't

**A second writer, in `ci-adopt` and `ci-adopt-restart` for room 1's setpoint.** A person's
change at a head adopted by its room (`coordinator.py` `_head_adoption`, Accept changes made at
the head) writes the room's target. Its rule, that a report MXZ's own writes can explain is never
a person, is the fan memory's rule for a head's mode and setpoints (`logic.py`
`HeadWriteMemory`). The model has room 1's target, the setpoint its head shows, the writes a
lagging head hasn't shown yet in order, the memory as `HeadWriteMemory` keeps it, the hold, startup
quiet, a reload and a process start. Each queued write carries a ghost kind saying how it left the
memory, if it did, so the strict rule is checked and each way past it is a rule of its own:
SM-1 (a process start, fixed: the room state store keeps the memory) and SM-10 (a person's press of a value MXZ still has queued, after which
`HeadWriteMemory.caught_up` or `HeadWriteMemory.seen` can drop a queued write: an accepted known
cost, so its strict rule `QueuedWriteNeverAdopted` is not checked). The memory counts
each value's copies, so a rewrite is not a way past it (`QueuedWriteKept`). The enable (on and off at the head), the heat/cool choice and every gate
but startup quiet are not in it; the tests in `tests/test_accept_head_changes.py` hold those.
[STATECHART.md](STATECHART.md#changes-made-at-a-head) charts it.

**The apply as steps, in `ci-apply`.** With `WithSplit`, a Decide is the compute and the mode's
record (`_write_back_mode` runs with the heads' commands queued and none run yet), and each head's
command is a later step that can be refused. Readings, picks, reloads and restarts land between
them. An apply counts as a decision only if nothing came in since.

**In the spec.** Rooms in priority order, enabled, with the default drift and threshold.
Each room's reading as one of six positions, its engage latch, and its head's mode. The
shared mode, Resting mode, the hold and its record. Room 1's door/window state, delays,
override and store. Room 1's fan: reported speed, command memory with its older writes, ladder index, latch,
pending restore, restore data, and a head that reports late. A person's pick of the mode,
the fan and the head's mode. The Fan auto and Window Override switches. Reload, restart with
the fan touched during the outage, and crash.

**Not in the spec.**

- Hands-off votes and wall-clock steps: `verification/modehold/` has them. There's one clock here.
- °F and °C: the machine has one unit. `tests/test_unit_switch.py` and the two properties
  named above cover a switch.
- Safety priority, floors and ceilings, Comfort priority, Dry, eco, lockouts, standby, lost
  heads, sensor freshness: `tests/test_modehold_properties.py` covers the arbitration ones for one decision.
- Cloud heads' batching, write interval and confirm wait. Their latch is the same
  `fan_latch.observe_fan`, so the rule is in the model, and the properties run on a cloud head.
- Idle actions other than fan_only, the coil-dry wait, vanes, setpoints, notices, `fan_on_pending`.
- A head working through a queue of older fan writes. The model's head reports only the latest,
  and its older writes are a set, so where the code keeps them after a report of a speed written
  twice, the model has forgotten them. And where the code's memory stays as the last write once
  a head has caught up, the model's next write puts back the speed before it, so the model's echo
  is wider there. `tests/test_fan_lag_memory.py`,
  `test_no_person_no_hold_when_a_head_reports_every_write_in_order`,
  `test_no_person_no_hold_when_a_head_jumps_to_its_latest_write` and
  `test_the_exception_is_still_the_last_two_with_a_write_in_flight` cover this.
- The delivery queue itself (`DeliveryQueue` in `delivery.py`): one apply at a time, every command of an apply
  goes before the next decision, and a later plan never supersedes a queued command. A head's
  lock, a vane cleanup, a manual gesture waiting its turn, and a head whose handler never returns
  are in `tests/test_command_delivery.py` and `tests/test_refresh_race.py`. A change made at a
  head while a command waits (SM-6) is in `tests/test_accept_head_changes.py`.
- Cloud heads' batching (SM-3 is held by `tests/test_cloud_heads.py`), the refresh debouncer
  and its follow-up, floors, Dry, seasons, standby and vanes: see
  [STATE-MACHINES.md](../STATE-MACHINES.md) section 6.3.
- The fan ladder is cut to three rungs with the ceiling at the second. Delays and the hold are whole ticks.
- Fan rules for a second room. Those dicts are per head, and `test_manual_fan_latch_isolated_per_zone` covers it.

**Not in the properties.** Crash: the spec covers it, and `tests/test_modehold_findings.py`
replays the mode part. Each sequence has at most 14 steps, and 25 examples per test in CI.

## Run TLC

You need Java 11 or later and `tla2tools.jar` 1.7.4 (sha256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`, from
[the TLA+ release](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4)).
`.github/workflows/tla.yml` does the same on each change to the spec. It isn't a required check.

```sh
export TLA2TOOLS=/path/to/tla2tools.jar   # and JAVA, if java isn't on PATH; TLC_WORKERS (default auto)
python3 tools/formal_tlc.py check     # every config passes every rule it lists
python3 tools/formal_tlc.py cover     # every reachability goal has a path, any open finding still breaks
python3 tools/formal_tlc.py mutants   # every broken copy of the spec breaks its named rule
python3 tools/formal_tlc.py all       # those three, as the workflow runs them
```

One subsystem at a time, with its tests on the real code (`MXZ_PYTHON`, a lane venv's python,
as for the properties below):

```sh
python3 tools/formal_area.py list            # fan, window, adopt, apply, and what each runs
python3 tools/formal_area.py check adopt     # its configs pass, its goals and open findings break, its tests pass
python3 tools/formal_area.py mutants adopt   # each broken copy of the spec and of the code for it is caught
```

`ci-arbitration` and `ci-arbitration-rest-heat` belong to none of them: they are the mode
hold, which `verification/modehold/` covers in depth, and `formal_tlc.py all` runs them.

By hand, from `docs/formal/tla/`: `java -cp "$TLA2TOOLS" tlc2.TLC -workers auto -config ci-fan.cfg MXZCoordinator.tla`.

| Config | What varies |
| --- | --- |
| `ci-fan` | One room's fan, outage picks and restarts, no passing time |
| `ci-fan-crash` | The same, with crashes and the periodic restore-data dump |
| `ci-fan-lag` | The same, with a head that reports late, however many writes behind |
| `ci-window` | One room's door/window, with delays, reloads and restarts |
| `ci-window-fan` | The door/window and the fan together, reloads, delays at 0, one reading |
| `ci-arbitration` | Two rooms, picks, reloads, restarts, crashes, a hold of 2 ticks |
| `ci-arbitration-rest-heat` | The same with Resting mode set to heat |
| `ci-arbitration-window` | Two rooms, one with a door/window |
| `ci-apply` | Two rooms, the apply as steps with refused commands, picks, readings, reloads and restarts between them |
| `ci-adopt` | Room 1's setpoint: a head up to two writes behind, presses at the head, targets set in Home Assistant, reloads |
| `ci-adopt-restart` | The same with process starts and startup quiet instead of only reloads |

## Run the property tests

They're in the normal suite, seeded from each test's name, so a CI run can't fail on a
draw an earlier run didn't make. A lane without Hypothesis fails visibly at collection.

```sh
python -m pytest tests/test_properties_mode.py tests/test_properties_fan.py \
  tests/test_properties_window.py tests/test_properties_restart.py -q -p no:cacheprovider
```

For a wider search with fresh random draws, and to prove each one can fail:

```sh
MXZ_PROPERTY_EXAMPLES=400 MXZ_PROPERTY_RANDOM=1 python -m pytest tests/test_properties_fan.py -q -p no:cacheprovider
MXZ_PYTHON="$(command -v python)" python3 tools/code_mutants.py      # every broken copy of the code fails its named property
python -m pytest verification/findings -q -p no:cacheprovider        # each finding's replay: F1's now passes
```

A code mutant whose broken code runs only on some Home Assistant releases declares `needs` in
`tools/code_mutants.py`, a probe the runner runs in the lane's own `MXZ_PYTHON` first. On a lane
that lacks it, the mutant is not run: its line starts `N/A` and says what it needs, and it counts
as not applicable, never as caught. A probe that errors fails the run. The run ends with the
counts (caught, not applicable, missed, probe errors) and `MUTANTS_ALL_CAUGHT` (exit 0) only if
nothing was missed, no probe errored and at least one mutant was caught; a run where no mutant
asked for applies ends `MUTANTS_NONE_APPLICABLE` (exit 1). `formal_area.py` and `verification/run`
take none of these mutants, so their runs mean the same on every lane.

C58 is the only such mutant. It breaks `DeliveryQueue.request_for_report`'s branch for Home
Assistant before 2025.11, so it runs and is caught on the py312-ha2024.12.5 lane and shows
`N/A` on 2025.11 and later. CI's py3.12-ha2024.12.5 job runs its test,
`test_one_target_change_takes_no_later_press_back_to_it`, in the full suite of every pull
request and every push to main.
