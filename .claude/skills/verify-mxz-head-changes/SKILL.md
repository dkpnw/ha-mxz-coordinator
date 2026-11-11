---
name: verify-mxz-head-changes
description: "Formal checks of MXZ Coordinator's Accept changes made at the head (on by default): a person's change at a head's remote becomes the room's target, and MXZ's own setpoint writes, which a lagging head shows late, never do. A TLA+ model checked with TLC (ci-adopt, ci-adopt-restart), the integration tests in tests/test_accept_head_changes.py, and broken copies of both that each check must catch. Use before and after any change to logic.HeadWriteMemory, coordinator.py _remember_head_write, _watch_head_change, _head_adoption, _head_change_settling, _async_adopt_head_change, startup quiet, or where the head write memory is kept, and when the model or a replay reports a break."
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
- **StartWriteNeverAdopted.** A write from before a reload is never adopted. It passes in
  `ci-adopt`, where a reload keeps the memory. In `ci-adopt-restart` it is **open finding
  SM-1**: a process start loses the memory.
- **QueuedWriteNeverAdopted.** A write still queued at the head that the memory has dropped is
  never adopted. **Open finding SM-10**, in `ci-adopt`.
- Reachability goals, which must break so the rules above aren't vacuous: `NeverAdopts` (a
  press is adopted at all) and `NeverLateOwnWriteInQuiet` (a late own write does land inside
  startup quiet).

Code checks (`tests/test_accept_head_changes.py`, in CI on every lane): the integration on
invented heads, including the Hypothesis tests
`test_no_person_no_adoption_however_a_head_lags` and
`test_every_report_of_a_head_showing_writes_in_order_reads_as_own`, the hold
(`test_a_change_must_hold_and_isnt_overwritten_while_it_does`), a reload and a restart
(`test_the_memory_rides_a_reload_with_the_head_still_behind`,
`test_a_restart_loses_the_memory_and_the_start_window_keeps_a_late_write_out`), and the gates
the model leaves out.

## Coverage record

Sync commit: `ca78324aca3d5402252a585693ae98193cee24ca` (main, release 3.22.0). The model and
tests were confirmed against the code at that commit. Move the sync commit forward only after
you have re-read the functions below at the new commit, confirmed the model still describes
them, and run both commands under Run.

| Check | Covers (custom_components/mxz_coordinator/) |
| --- | --- |
| Model `WriteSp` | `coordinator.py` `_apply` (skips a head while `_head_change_settling`), `_call_head_service`, `_remember_head_write`; `logic.py` `HeadWriteMemory.wrote` |
| Model `HeadShows`, `PersonPress` (`Report`) | `coordinator.py` `_on_head_change`, `_head_writes_caught_up`, `_watch_head_change`, `_head_adoption`, `_arm_or_cancel`, `_disown_waiting_command`; `logic.py` `HeadWriteMemory.own`, `HeadWriteMemory.caught_up` |
| Model `AdoptFires` | `coordinator.py` `_on_heal_timer`, `_async_adopt_head_change`, `_head_change_seen`; `logic.py` `HeadWriteMemory.seen` |
| Model `SetTarget` | `number.py` `MXZTargetNumber.async_set_native_value`; `coordinator.py` `drop_head_change` |
| Model `Reload`, `Restart`, `QuietEnds` | `coordinator.py` `MXZCoordinator.__init__` (`_head_writes` in `hass.data`, `_startup_quiet`), `_on_startup_timer` |
| Code mutants C44 to C48 | `_remember_head_write`, `_head_change_settling`, `MXZCoordinator.__init__`, `HeadWriteMemory.caught_up`, `HeadWriteMemory.seen` |

Does a change touch it?

```sh
git diff --stat ca78324aca3d5402252a585693ae98193cee24ca -- \
  custom_components/mxz_coordinator/coordinator.py custom_components/mxz_coordinator/logic.py \
  custom_components/mxz_coordinator/number.py docs/formal/tla/MXZCoordinator.tla
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
checkout: A1 to A3 break the spec, C44 to C48 the code, and each must be caught by its named
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
   shortest SM-1 path goes that way and does not replay; SM-1 itself does, on the path in
   `docs/formal/PARKED-FINDINGS.md`.

Never weaken a rule, a bound or a test to make a check pass. When the promise changes on
purpose, change the rule, the docs and the tests together, and say so in the PR.

## Findings

Two open findings, each waiting on the maintainer's decision. `check` requires both to keep
breaking until it is made. `docs/STATE-MACHINES.md` sections 4 and 7 have the evidence and the
options, and `docs/formal/PARKED-FINDINGS.md` the short form.

- **SM-1.** MXZ's memory of its writes is in `hass.data`, so a process start loses it. A
  pre-restart write that a lagging head shows after startup quiet (40 s) is adopted. Replayed
  at the sync commit: the target ends at 72 where the person left 74.
- **SM-10.** A rewrite kept once, a person's press of a remembered value, or
  `HeadWriteMemory.seen` can drop a write still queued at the head. When that write lands after
  a later press, it is adopted over the press. Replayed at the sync commit on the chart's path.

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
