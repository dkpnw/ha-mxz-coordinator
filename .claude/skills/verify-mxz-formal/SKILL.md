---
name: verify-mxz-formal
description: "Formal checks of MXZ Coordinator's mode-change hold (Minimum time between mode switches, kept across reload and restart since PR #64) and the cool/heat arbitration it gates (room order, Safety priority, floors, Comfort priority #66, hands-off votes, a paused run, Resting mode): a TLA+ model checked with TLC, exhaustive property tests on the real logic.arbitrate, and replays of each counterexample on the real integration. Use before and after any change to logic.shared_mode, logic.arbitrate, the dwell/stamp code in coordinator.py, or the shared-mode select's restore, and when a model or replay reports a break."
---

# Verify the mode-change hold and the arbitration

MXZ runs one outdoor unit in one direction. Changing it between cool and heat
too often is the harm this guards: a reversal every few minutes, or a room's
run cut off before it reaches its target. Tests check the cases someone
wrote; this checks every order of steps in a small model, and every input in
a stated table of the real functions.

Paths are from the repository root.

## What is checked

Each rule in one sentence, with the harm if it breaks.

Model rules (`verification/modehold/MXZModeHold.tla`, checked by TLC):

- **HoldKept.** After a real mode change, MXZ itself changes the heads' mode
  again only once the hold has passed; a change only hands-off votes made (and
  its undoing) and a person's pick are exempt, as documented. Across a
  restart the hold runs on the wall clock, so a clock step between the
  change and the restart moves its end (documented). Harm: compressor
  reversals inside the hold.
- **NoExtraWait.** Unless the wall clock was stepped since the last change,
  the hold is never longer than what is left of it. Harm: a full fresh wait
  after every reload or restart (the bug PR #64 fixed).
- **NeverLonger.** Whatever the wall clock did, the hold is never more than
  the full window. Harm: a clock set back holds the mode for hours.
- **FreshNoWait.** With no change on record, nothing is held. Harm: a fresh
  install waits for nothing.
- **PausedRunKept.** A room whose head is running in the shared mode keeps
  that mode until the room reaches its target, unless another room's real
  call for the other mode, or a person, takes it. Resting mode waits for the
  run (docs/HOW-IT-WORKS.md step 4). Harm: a run cut off and the room left
  short of its target.

Code properties (`tests/test_modehold_properties.py`, in CI on every lane),
on the real `logic.arbitrate` over every case of two tables (648,000 three-room
cases; 373,248 two-room cases built by `MXZCoordinator._zone_arbitration_pair`):

- `hold_blocks_every_change`: while the hold holds, the shared mode does not change.
- `vote_never_beats_a_call_or_a_run`: a hands-off vote never takes the mode
  from a calling or running room.
- `highest_rank_decides`: once allowed, the highest-ranked want decides:
  freeze floor, then Safety tier, then real demands, then votes; Comfort
  priority only within the demand and vote tiers; then room order; within one
  room and rank a real want before a vote.
- `nothing_calling_rests`: with nothing calling, Resting mode, else where it was.
- `exempt_only_with_the_gate_open`: a change exempt from the hold is one only
  votes made while no room called or ran.
- `by_safety_means_a_safety_want_agrees`.

`tests/test_hold_promises.py`
`test_a_run_in_the_shared_mode_changes_only_where_nothing_calls` sweeps the
same three-room table with a room engaged (324,000 cases) on
`logic.arbitrate(..., running=True)`: with nothing calling the mode stays
where the run is, and every other answer is the one without `running`.
- `test_a_start_keeps_what_is_left_of_the_hold_and_never_more`:
  `mode_change_stamp` over 1,018 generated offsets.
- `test_a_start_reads_the_record_or_holds_the_full_window`:
  `async_load_last_mode_change` over 73 generated records.

## Coverage record

Sync commit: `289eff3b37ef2cfa4a2da1406855a039e6f86739` (branch `fix/hold-promises`, the fix
of F1 to F3, on main `5a3a128`). The model and tests were confirmed against
the code at that commit. Move the sync
commit forward only after you have confirmed that the model and tests still
describe the code at the new commit (re-read the functions below, then run
the check and the mutants).

| Check | Covers (custom_components/mxz_coordinator/) |
| --- | --- |
| Model `Decide` | `coordinator.py` `_compute` (allowed, engaged, running, the `_mode_by_votes` undo rule), `_apply` (the stamp and record rules at its end), `_stamp_mode_change`, `_record_mode`; `logic.py` `room_call`, `engage_with_latch`, `shared_mode`, `arbitrate` (`running`), `head_action` |
| Model `Reload` / `Restart` / `Crash` | `__init__.py` `async_setup_entry`; `coordinator.py` `ModeRecord`, `async_load_mode_record`, `mode_change_stamp`, `_mode_change_store`, `MXZCoordinator.__init__` (`_last_mode_change_ts`, the record's mode and `_mode_by_votes`, `_engage_latch`, `_startup_resume`), `restore_shared_mode`; `switch.py` (sets `_startup_resume`); `select.py` `async_added_to_hass` |
| Model `Select` | `select.py` `async_select_option`; `coordinator.py` `async_select_shared_mode` |
| Arbitration properties | `logic.py` `shared_mode`, `arbitrate`, `_rank`, `comfort_boost`; `coordinator.py` `_zone_arbitration_pair` |
| Start properties | `coordinator.py` `mode_change_stamp`, `async_load_last_mode_change` (`async_load_mode_record`) |
| Replays F1–F3, `tests/test_hold_promises.py` | the integration end to end (setup, `_compute`, `_apply`, reload, restart, crash, restore, the record's contents) |

Does a change touch it?

```sh
git diff --stat 289eff3b37ef2cfa4a2da1406855a039e6f86739 -- \
  custom_components/mxz_coordinator/logic.py custom_components/mxz_coordinator/coordinator.py \
  custom_components/mxz_coordinator/select.py custom_components/mxz_coordinator/switch.py \
  custom_components/mxz_coordinator/__init__.py
```

If it lists a file, read the diff against the table: a change inside a
listed function needs the check run, and possibly the model updated.

## Tools

Chosen because the question is mostly about the order of events (a change, a
reload, a crash, a clock step, a vote), which suits a model checker, and TLA+
is the default for that; and because the arbitration is a pure Python
function with a small finite input space, which suits exhaustive tests on the
real function. Hypothesis is a pinned test dependency (requirements_test.txt and every
lane lock), so code-level checks use Hypothesis property tests
(tests/test_properties_*.py) alongside stdlib `itertools` and seeded `random`.

| Tool | Version | sha256 | On this host |
| --- | --- | --- | --- |
| tla2tools.jar | 1.7.4 (TLC2 2.19, rev 5a47802), from github.com/tlaplus/tlaplus releases v1.7.4 | `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88` (the runner refuses any other) | `/root/mxz-work/tools/tla2tools-1.7.4/tla2tools.jar` |
| Java | Eclipse Temurin 21.0.13+9 LTS, `OpenJDK21U-jdk_x64_linux_hotspot_21.0.13_9.tar.gz` | `d7f5d5cf865a2902b70ff549b97586a7d3f234e016f041ab447ea740c7d66d1c` | `/root/mxz-work/tools/jdk-21.0.13+9/bin/java` |
| Python | any lane venv per docs/DEVELOPMENT.md | | `/root/mxz-work/lanes/<lane>/bin/python` |

Elsewhere: download those two files, check the sha256, unpack the JDK, and
point the variables below at them. Any Java 11 or later runs TLC.

## Run

```sh
export MXZ_PYTHON=/root/mxz-work/lanes/py314-ha2026.10.4/bin/python
export JAVA=/root/mxz-work/tools/jdk-21.0.13+9/bin/java
export TLA2TOOLS=/root/mxz-work/tools/tla2tools-1.7.4/tla2tools.jar
verification/run            # about 3 minutes
verification/run mutants    # about 1.5 minutes; or name some: verification/run mutants C3 M5
```

`verification/run` checks, and ends `VERIFY_OK` or `VERIFY_FAILED`:

- each TLC config passes every rule it lists (`verification/modehold/*.cfg`):
  the `ci-*` configs, and the `finding-*` configs of the fixed findings F1 to
  F3 (each the shortest path to its finding, with only its rule);
- `tests/test_modehold_properties.py` passes, on the host clock and with
  `-p tools.fakeboot` (`FAKE_UPTIME=120`), as CI runs it;
- each replay in `tests/test_modehold_findings.py` passes, the finding case
  and its control (same steps, without the trigger), and so does
  `tests/test_hold_promises.py`, on both clocks.

`verification/run mutants` copies the checkout to a scratch directory, breaks
one thing there (never the checkout), and requires the named check to catch
it: C1–C17 break the real code, M1–M10 the model (each run with only its rule).
C12–C17 and M8–M10 undo the F1 to F3 fixes one piece at a time.
It ends `MUTANTS_ALL_CAUGHT` or `MUTANTS_MISSED`. The list, with each break in
words, is `MUTANTS` in `verification/verify.py`. If a mutant reports that its
text is not in the file exactly once, the code moved: re-anchor it to the
same logic.

Output, kept: `$MXZ_FORMAL_OUT`, default `~/verify-mxz-formal/<UTC stamp>/`
(each TLC run's `tlc-<config>.out`, the pytest logs, `mutant-<id>.log`).

CI's `pytest tests/` runs `tests/test_modehold_properties.py`, the replays
in `tests/test_modehold_findings.py` and `tests/test_hold_promises.py`, on
every lane and on the fresh-boot clock. The TLC configs are not in CI: they
need Java, and the workflows have no verification job; run them here.

## Read the results

Read every TLC result, a pass or a break, with the pstack `/statechart`
skill, on a copy of `verification/modehold/MXZModeHold.tla` and the config in
a scratch folder outside the repository, with `verification/modehold/view.ts`
as the view. It tells a break as its shortest path of steps in a person's
words ("MXZ decides and commands the heads", "the entry reloads"), and draws
the chart when the model is small enough. `view.ts` hard-codes `HOLD = 2`, the
configs' H; change both together.

A break is a real bug, or a modeling error. Tell them apart before reporting:

1. Replay the path on the real code, one step per model action (copy the
   shape of the three in `tests/test_modehold_findings.py`). While the
   finding is open its replay fails, so keep it out of CI's `pytest tests/`:
   put it in `verification/findings/` with a `conftest.py` that re-exports
   `tests.conftest`'s `auto_enable_custom_integrations` and
   `pytest_configure` (as F1 to F3 had before their fix), list it in
   `verification/verify.py` as a finding (its config must break its rule),
   and move it into `tests/` when it is fixed.
2. Give it a control: the same steps without the trigger. The control must
   pass, or the replay itself is wrong.
3. If the replay does not fail, assume the model is wrong until you show it
   is not. Errors met while building this one: a reload kept the process's
   monotonic clock but the model reset it; a person's pick that a vote agreed
   with was booked as a vote's change; the rank oracle missed "a real want
   before a vote" within one room; and `tests/dwell.py` `expire_dwell` ages
   only the in-memory stamp, so a replay that reloads must also step the wall
   clock (`tests/clocks.py` `jump_wall_clock`), or the stored record still
   reads "just now".

Never weaken a rule, a table or a bound to make a check pass. When a change
is intended (the docs change what MXZ promises), change the rule and the
docs together, and say so in the PR.

## Findings (fixed)

The code at 3.19.0 broke three rules. They were fixed together on the
maintainer's decisions of November 3, 2026, and the docs say what MXZ now
promises. Each keeps its `finding-*` config (now must pass) and its replay
in `tests/test_modehold_findings.py` (the finding case failed before the
fix; it and its control pass now). There are no open findings.

- **F1, PausedRunKept, `finding-resting`.** With Resting mode set to the other
  direction, a room still running toward its target lost the shared mode
  once the hold passed (room 1 calls cool at 76, target 70, cools to 72,
  still engaged cool; with no room calling `shared_mode` proposed the
  resting heat). Fix: `logic.arbitrate(..., running=...)`, true while a
  held engage is the current mode, makes Resting wait; `_compute` and the
  dwell wakeup pass it. Model: `SharedMode`'s `running`.
- **F2, HoldKept, `finding-reload-vote`.** `_mode_by_votes` was memory only,
  so after a reload or restart a room's run in a mode a vote chose was not
  stamped and its next reversal came at once. Fix: the Store record keeps
  `mode_by_votes` with the mode, and a start restores it with the record's
  mode (`restore_shared_mode`). A mode a room chose still starts no fresh
  hold after a reload (`tests/test_hold_promises.py`).
- **F3, HoldKept, `finding-crash`.** The change's time was in its Store at
  once, but the direction only in the select's restore data, so a crash
  seconds after cool to heat restarted MXZ in cool inside the heat change's
  hold. Fix: `_record_mode` writes the mode, `mode_at`, `mode_by_votes` and
  the last change time in that same record at every change of the mode,
  after the select has shown it; `restore_shared_mode` takes the record
  unless the select's restore data disagrees and is newer (then the record
  is brought up to date). Where they agree the record wins, since the
  select's state is written anew at every start.

The `ci-*` configs now check every rule, PausedRunKept with Resting set
included; `ci-votes-restarts*` combine votes with reloads and restarts, and
`ci-crash*` a crash with two rooms.

## Scope and bounds

- The model: one or two coordinated rooms (priority order) and at most one
  hands-off room; defaults drift 1 and threshold 3, so six reading
  positions; H = 2 ticks; true time 0 to 5; wall-clock offsets of -3, 0 and
  +3 ticks. `ci-restarts*` use one room with clock steps, select, reload and
  restart; `ci-votes` and `ci-votes-rest-heat` two rooms with votes and
  select, no restarts and no clock steps; `ci-votes-restarts*` one room with
  votes, select, reload and restart, no clock steps; `ci-crash*` two rooms
  with select, restart and crash, no clock steps. Distinct states:
  ci-restarts 1,789,554; ci-restarts-rest-cool 1,688,430;
  ci-restarts-rest-heat 1,699,962; ci-votes 553,176; ci-votes-rest-heat
  327,348; ci-votes-restarts 285,732; ci-votes-restarts-rest-heat 188,622;
  ci-crash 2,327,940; ci-crash-rest-heat 2,309,184.
- No config combines a crash with clock steps. One that does (one room,
  SkewMax 3, select and crash, HoldKept only, not kept) breaks HoldKept: the
  clock set back, then a pick of heat, then a crash restores the cool of
  restore data whose time now reads later than the record's. The record wins
  only when it is newer (the decision for F3), so that path stays; it is
  documented in docs/FEATURES.md "Mode-change wait" and has not been
  replayed on the real code.
- One `Decide` is one `_compute` plus `_apply` with no event between; a
  person's pick or a reading arriving during an `_apply` (the
  `_selection_seq` guard) is not modeled.
- The Store record is written at the change (`async_delay_save(..., 0)`): a
  crash within that one event-loop turn would lose it too, and is not
  modeled. The model's record times and the select's `last_changed` are the
  same tick for one change; in the code the record's is the later of the two
  (`tests/test_hold_promises.py` checks that order).
- Not modeled: Safety priority, floors and ceilings, Dry, Comfort priority,
  lost heads, eco, lockouts, standby, sensor freshness, cloud heads, the dwell
  wakeup timer. The arbitration tables cover Safety, floors, Dry and Comfort
  priority for one decision, not over time.
- The tables cover Comfort priority 0 and 1 only; non-numeric and
  non-positive values are `tests/test_comfort_priority_logic.py`'s.
- A pass covers the model and the tables at these bounds, not the whole
  integration, and nothing here is a hardware result.

Keep this in step with the code with `/maintain-formal-verification-skill`.
