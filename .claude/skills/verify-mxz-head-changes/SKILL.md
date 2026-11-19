---
name: verify-mxz-head-changes
description: "Formal checks of MXZ Coordinator's Accept changes made at the head (on by default): a person's change at a head's remote becomes the room's target, and MXZ's own setpoint writes, which a lagging head shows late, never do. A TLA+ model checked with TLC (ci-adopt, ci-adopt-restart), the integration tests in tests/test_accept_head_changes.py, and broken copies of both that each check must catch. Use before and after any change to logic.HeadWriteMemory, coordinator.py remember_head_write, _on_head_change, _perform_head_report, _head_adoption, _head_change_settling, _async_adopt_head_change, planner.py head_moved and adopt_head_change, startup quiet, or where the head write memory is kept, and when the model or a replay reports a break."
---

# Verify changes made at a head

With **Accept changes made at the head** on, a room's target has two writers: the person in
Home Assistant, and a person at the head's remote. MXZ also writes the head's setpoint itself,
and a head can show those writes seconds later, in order. The harm this guards: MXZ reads its
own late write as a person's press and moves the room's target with nobody touching anything,
or writes over a press a person just made.

Paths are from the repository root. `docs/STATE-MACHINES.md` section 3.11 is the machine in
words; `docs/formal/STATECHART.md` "Changes made at a head" (A1 to A5) is its chart, and
"User flows" has the chart the model itself draws.

## What is checked

Model rules (`docs/formal/tla/MXZCoordinator.tla` with `WithAdopt`, room 1's setpoint):

- **OwnWriteNeverAdopted.** The target never takes a value only MXZ put on the head while MXZ
  still remembers writing it. Harm: the target moves with nobody touching it.
- **HoldBlocksWrite.** While a change made at the head is holding, MXZ writes nothing to that
  head's setpoint. Harm: a person's press is written over before it can be adopted.
- **StartWriteNeverAdopted.** A write from before a reload or a restart is never adopted. It
  passes in `ci-adopt` and `ci-adopt-restart`: the room state store keeps the memory (SM-1,
  fixed).
- **QueuedWriteKept.** Until a person presses a value MXZ still has queued at the head, no
  queued write leaves the memory and is adopted; each copy of a rewrite is counted. Passes in
  `ci-adopt` and `ci-adopt-restart`.
- **QueuedWriteNeverAdopted.** A write still queued at the head that the memory has dropped is
  never adopted, also after such a press. Not checked: SM-10 paths (b) and (c) break it, an
  accepted known cost. It stays in the spec as the form `QueuedWriteKept` is built from.
- Reachability goals, which must break so the rules above aren't vacuous: `NeverAdopts` (a
  press is adopted at all) and `NeverLatePreStartWrite` (a write queued before a start is still
  queued after startup quiet ends).

Code checks (`tests/test_accept_head_changes.py`, in CI on every lane): the integration on
invented heads, including the Hypothesis tests
`test_no_person_no_adoption_however_a_head_lags` and
`test_every_report_of_a_head_showing_writes_in_order_reads_as_own`, the hold
(`test_a_change_must_hold_and_isnt_overwritten_while_it_does`), a reload and a restart
(`test_the_memory_rides_a_reload_with_the_head_still_behind`,
`test_a_late_own_write_inside_startup_quiet_is_not_adopted`,
`test_a_pre_restart_write_shown_late_is_not_adopted`), the copies of a rewrite
(`test_a_rewrite_still_queued_is_not_adopted_over_a_later_press`,
`test_no_write_still_queued_is_forgotten_but_by_a_press_of_its_value`, and the cost,
`test_a_press_back_to_a_rewritten_value_reads_as_mxzs_once`), and the gates the model leaves out.

## Coverage record

Sync commit: `ca78324aca3d5402252a585693ae98193cee24ca` (main, release 3.22.0). The model and
tests were confirmed against the code at that commit. Move the sync commit forward only after
you have re-read the functions below at the new commit, confirmed the model still describes
them, and run both commands under Run.

| Check | Covers (custom_components/mxz_coordinator/) |
| --- | --- |
| Model `WriteSp` | `coordinator.py` `_apply` (skips a head while `_head_change_settling`, `planner.py` `holds_change`), `remember_head_write` (`head_writes.py` `issue_steps`, `remember_writes`); `delivery.py` `DeliveryQueue._async_issue`; `heads.py` `LocalDriver._async_apply`, `grid.py` `local_calls` and `Grid.already_at` (the head's grid, which the memory compares on); `logic.py` `HeadWriteMemory.wrote` |
| Model `HeadShows`, `PersonPress` (`Report`) | `coordinator.py` `_on_head_change`, `_head_writes_caught_up`, `_perform_head_report`, `_head_adoption`, `_heal`; `planner.py` `report_steps`, `head_moved`, `setpoint_field`, `head_change`, `adopt_head_change`, `head_report`, `startup_report`; `grid.py` `Grid.target_for_setpoint`, `Grid.target_bounds`; `delivery.py` `DeliveryQueue.disown`; `logic.py` `HeadWriteMemory.own`, `HeadWriteMemory.caught_up` |
| Model `AdoptFires` | `coordinator.py` `_on_heal_timer`, `_async_adopt_head_change`, `_perform_adopt_step`, `_head_change_seen`; `planner.py` `hold_end`, `adopt_end`, `off_heal_recheck`; `head_writes.py` `writes_pressed`; `logic.py` `HeadWriteMemory.pressed`, `HeadWriteMemory.seen` |
| Model `SetTarget` | `number.py` `MXZTargetNumber.async_set_native_value`; `coordinator.py` `reset_engage_latch`, `_perform_room_steps`; `planner.py` `target_written`, `head_dropped` |
| Model `Reload`, `Restart`, `QuietEnds` | `coordinator.py` `MXZCoordinator.__init__` (`_startup_quiet`), `_resume` and `_head_writes_record` (the ledger in the room state store, `resume.py` `SECTION_HEAD_WRITES`, read back by `resume.py` `resume`), `_on_startup_timer` (`timing.py` `startup_quiet`); `logic.py` `HeadWriteMemory.record`, `HeadWriteMemory.restored` |
| Code mutants C44 to C57 | `remember_writes` (C44), `holds_change` (C45), `resume` (C46), `_resume` (C52), `HeadWriteMemory.caught_up`, `HeadWriteMemory.seen`, `HeadWriteMemory.wrote`, `DeliveryQueue._async_call` (C55, with `head_writes.py` `writes_refused`) |

Does a change touch it?

```sh
git diff --stat ca78324aca3d5402252a585693ae98193cee24ca -- \
  custom_components/mxz_coordinator/coordinator.py custom_components/mxz_coordinator/delivery.py \
  custom_components/mxz_coordinator/heads.py \
  custom_components/mxz_coordinator/logic.py custom_components/mxz_coordinator/number.py \
  custom_components/mxz_coordinator/planner.py \
  custom_components/mxz_coordinator/resume.py custom_components/mxz_coordinator/room_state.py \
  docs/formal/tla/MXZCoordinator.tla
```

If it lists a file, read the diff against the table. A change inside a listed function needs
the check run, and possibly the model updated.

## Tools

The question is the order of events: a write, a late report, a press, a reload, a restart, the
hold's end. That suits a model checker, and TLA+ is the default for it. The tests run the real
integration, since the memory's rule is spread over the coordinator, the memory class and the
number entity. TLC is `tla2tools.jar` 1.7.4 (sha256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`; the runner refuses any
other jar), on Java 11 or later. The tests run on a lane venv per `docs/DEVELOPMENT.md`.

## Run

```sh
export TLA2TOOLS=/path/to/tla2tools.jar      # and JAVA if java isn't on PATH
export MXZ_PYTHON=/path/to/lane/bin/python   # a lane venv with hypothesis in it
python3 tools/formal_area.py check adopt     # about 30 seconds; ends AREA_OK or AREA_FAILED
python3 tools/formal_area.py mutants adopt   # under a minute
```

`check` runs TLC on `ci-adopt` and `ci-adopt-restart` (each must pass every rule its config
lists), requires each reachability goal and each open finding's rule to break in its config,
then runs `tests/test_accept_head_changes.py`. `mutants` breaks a scratch copy, never the
checkout: A1 to A6 break the spec, C44 to C57 the code, and each must be caught by its named
rule or test. `python3 tools/formal_area.py list` prints what each part runs. Output is kept
in the folder printed on the first line (`--out` chooses it).

## Read the results

Read every TLC result, a pass or a break, with the pstack `/statechart` skill, on copies of
`docs/formal/tla/MXZCoordinator.tla` and the config in a scratch folder outside the
repository, with `docs/formal/charts/adopt.ts` as the view. It tells a break as its shortest
path in a person's words ("a person presses a value at the head", "the head shows MXZ's
write"). The `ci-*` configs are past its chart budget, so it gives the bug paths only; for the
whole chart, use `docs/formal/charts/adopt.cfg` as `docs/formal/STATECHART.md` "User flows"
says.

A break is a real bug or a modeling error. Tell them apart before reporting:

1. Replay the path on the real code, one step per model action, on invented heads with
   `tests/property_rig.py` (`head.lags_commands = True`, `rig.report_next_command()`,
   `rig.restart()`, `rig.later(...)`), as `tests/test_accept_head_changes.py` does. Assert
   the rule, so the replay fails while the bug is there.
2. Give it a control: the same steps without the trigger. The control must pass, or the
   replay itself is wrong.
3. If the replay does not fail, assume the model is wrong until you show it is not. One case
   is known: the model lets a person set the target in Home Assistant between a process start
   and MXZ's first write after it, and the integration writes first. `adopt-restart.cfg`'s
   shortest SM-1 path (drawn before SM-1's fix) goes that way and did not replay.

Never weaken a rule, a bound or a test to make a check pass. When the promise changes on
purpose, change the rule, the docs and the tests together, and say so in the PR.

## Findings

No open finding: `FINDINGS` in `tools/formal_tlc.py` is empty. `docs/STATE-MACHINES.md`
sections 4 and 7 have the evidence and the decisions, and `docs/formal/PARKED-FINDINGS.md` the
short form.

- **SM-10, after a person's press of a value MXZ still has queued: an accepted known cost**
  (the owner's decision, Nov 17, 2026). The press reads as MXZ's write landing, so
  `HeadWriteMemory.caught_up` passes older writes or uses the write's copy, and
  `HeadWriteMemory.seen` can then drop it. When that write lands after a later press, it is
  adopted over the press. Its events are the same as those of
  `test_up_and_back_are_both_adopted` and `test_a_press_of_a_write_the_head_has_passed_is_adopted`,
  which require the opposite answer. The `test_known_cost_` tests in
  `tests/test_accept_head_changes.py` pin it. A rewrite kept once, the other way, is fixed
  (`QueuedWriteKept`), and so is a rewrite of the shown value behind another write (shape B,
  code mutant C56). A rewrite of the shown last write after a restart counts no copy (review 6
  of smfix1111, code mutant C57). Shape A and the F1 rewrite cost of the shape-B fix are
  accepted known costs under the overnight release mandate (Nov 17, 2026).

SM-1 (a process start lost the memory) is fixed: the room state store keeps it.

When one is decided and fixed, its rule moves into its config's `INVARIANTS` and out of
`FINDINGS` in `tools/formal_tlc.py`, and the docs say what MXZ now promises.

## Scope and bounds

- One room, always calling for cool (`FarCooling`), its head running, so only the setpoint
  moves. Targets 1 and 2 are set in Home Assistant; 3 is pressed only at the head. A head up to
  two writes behind (`QueueMax = 2`). No passing time (`MaxReal = 0`). `ci-adopt`: reloads,
  30,733 distinct states. `ci-adopt-restart`: reloads and process starts with startup quiet,
  152,590 distinct states (TLC 1.7.4, at the sync commit).
- Not in the model: the enable (on and off at the head), the heat/cool choice, and every gate
  but startup quiet (eco, standby, Coordinator off, a pause, a floor, a hands-off room, a stale
  cloud report). `tests/test_accept_head_changes.py` holds those.
- A pass covers the model at these bounds and the tests as drawn, not the whole integration.
  Nothing here ran on a real head.

Keep this in step with the code with `/maintain-formal-verification-skill`.
