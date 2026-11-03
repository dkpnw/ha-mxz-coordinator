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
  call for the other mode, or a person, takes it. Harm: a run cut off and the
  room left short of its target.

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
- `test_a_start_keeps_what_is_left_of_the_hold_and_never_more`:
  `mode_change_stamp` over 1,018 generated offsets.
- `test_a_start_reads_the_record_or_holds_the_full_window`:
  `async_load_last_mode_change` over 73 generated records.

## Coverage record

Sync commit: `b9d0663acf91eae4fbb10ff85d6daee9a4d86e53` (main, 3.19.0). The
model and tests were confirmed against the code at that commit. Move the sync
commit forward only after you have confirmed that the model and tests still
describe the code at the new commit (re-read the functions below, then run
the check and the mutants).

| Check | Covers (custom_components/mxz_coordinator/) |
| --- | --- |
| Model `Decide` | `coordinator.py` `_compute` (allowed, engaged, the `_mode_by_votes` undo rule), `_apply` (the stamp rules at its end), `_stamp_mode_change`; `logic.py` `room_call`, `engage_with_latch`, `shared_mode`, `arbitrate`, `head_action` |
| Model `Reload` / `Restart` / `Crash` | `__init__.py` `async_setup_entry`; `coordinator.py` `async_load_last_mode_change`, `mode_change_stamp`, `_mode_change_store`, `MXZCoordinator.__init__` (`_last_mode_change_ts`, `_mode_by_votes`, `_engage_latch`, `_startup_resume`); `switch.py` (sets `_startup_resume`); `select.py` `async_added_to_hass` |
| Model `Select` | `coordinator.py` `async_select_shared_mode` |
| Arbitration properties | `logic.py` `shared_mode`, `arbitrate`, `_rank`, `comfort_boost`; `coordinator.py` `_zone_arbitration_pair` |
| Start properties | `coordinator.py` `mode_change_stamp`, `async_load_last_mode_change` |
| Replays F1–F3 | the integration end to end (setup, `_compute`, `_apply`, reload, restore) |

Does a change touch it?

```sh
git diff --stat b9d0663acf91eae4fbb10ff85d6daee9a4d86e53 -- \
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
real function. No Python verifier is in the hash-pinned lane locks, so the
code-level checks are stdlib `itertools` and seeded `random` (no hypothesis;
do not add requirements).

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
verification/run            # about 1.5 minutes
verification/run mutants    # about 2 minutes; or name some: verification/run mutants C3 M5
```

`verification/run` checks, and ends `VERIFY_OK` or `VERIFY_FAILED`:

- each `ci-*` TLC config passes every rule it lists (`verification/modehold/*.cfg`);
- each `finding-*` config breaks exactly its finding's rule (below);
- `tests/test_modehold_properties.py` passes, on the host clock and with
  `-p tools.fakeboot` (`FAKE_UPTIME=120`), as CI runs it;
- each replay in `verification/findings/test_findings.py` fails as the
  finding while its control (same steps, without the trigger) passes.

`verification/run mutants` copies the checkout to a scratch directory, breaks
one thing there (never the checkout), and requires the named check to catch
it: C1–C11 break the real code, M1–M7 the model (each run with only its rule).
It ends `MUTANTS_ALL_CAUGHT` or `MUTANTS_MISSED`. The list, with each break in
words, is `MUTANTS` in `verification/verify.py`. If a mutant reports that its
text is not in the file exactly once, the code moved: re-anchor it to the
same logic.

Output, kept: `$MXZ_FORMAL_OUT`, default `~/verify-mxz-formal/<UTC stamp>/`
(each TLC run's `tlc-<config>.out`, the pytest logs, `mutant-<id>.log`).

CI runs only `tests/test_modehold_properties.py` (it is under `tests/`).
The TLC configs and the replays are not in CI: they need Java and, for the
findings, they fail by design.

## Read the results

Read every TLC result, a pass or a break, with the pstack `/statechart`
skill, on a copy of `verification/modehold/MXZModeHold.tla` and the config in
a scratch folder outside the repository, with `verification/modehold/view.ts`
as the view. It tells a break as its shortest path of steps in a person's
words ("MXZ decides and commands the heads", "the entry reloads"), and draws
the chart when the model is small enough. `view.ts` hard-codes `HOLD = 2`, the
configs' H; change both together.

A break is a real bug, or a modeling error. Tell them apart before reporting:

1. Replay the path on the real code, one step per model action, as a test in
   `verification/findings/` (copy the shape of the three there).
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

## Findings (documented, kept out of CI)

The current code breaks three rules. They are not weakened checks: each has
its own `finding-*` config, a replay that fails on the real integration, and
a control that passes. The counterexamples are in the replays' docstrings
in `verification/findings/test_findings.py`.

- **F1, PausedRunKept, `finding-resting`.** With Resting mode set to the other
  direction, a room still running toward its target loses the shared mode
  once the hold passes: room 1 calls cool at 76 (target 70), runs cool, cools
  to 72 (still engaged cool), the hold passes, and with no room calling
  `shared_mode` proposes the resting heat; room 1's head is parked short of
  its target. With Resting mode Last used it holds (the `ci-*` configs).
- **F2, HoldKept, `finding-reload-vote`.** `_mode_by_votes` is memory only. A
  hands-off vote sets heat (exempt), the entry reloads, room 1 then runs heat
  in it (not stamped, since the new coordinator's `_mode_by_votes` is False),
  and when room 1 calls cool a moment later the mode reverses at once.
  Without the reload the run is stamped and the reversal waits (control).
  The model finds the same path through a restart; only the reload is
  replayed on the real code.
- **F3, HoldKept, `finding-crash`.** The change's time is in its Store at
  once, but the direction is in the select's restore data, written every 15
  minutes and at a stop. A crash seconds after cool to heat restarts MXZ in
  cool, holding cool for the rest of the wait the heat change began: the
  outdoor unit reverses at once and the heat room is parked for the rest of
  the hold. A clean stop keeps heat (control).

Because of F2, no `ci-*` config combines votes with reloads or restarts.
Because of F1, PausedRunKept is checked in CI configs only with Resting mode
Last used; the `*-rest-*` configs check every other rule with Resting set.
When a finding is fixed, its replay passes and `verification/run` says so:
then move that config's rule into the `ci-*` configs and delete the finding
config.

## Scope and bounds

- The model: one or two coordinated rooms (priority order) and at most one
  hands-off room; defaults drift 1 and threshold 3, so six reading
  positions; H = 2 ticks; true time 0 to 5; wall-clock offsets of -3, 0 and
  +3 ticks. `ci-restarts*` use one room with clock steps, select, reload and
  restart; `ci-votes*` use two rooms with votes and select, no restarts and
  no clock steps. Distinct states: ci-restarts 936,774;
  ci-restarts-rest-cool 1,699,080; ci-restarts-rest-heat 1,704,294; ci-votes
  308,988; ci-votes-rest-heat 455,436.
- One `Decide` is one `_compute` plus `_apply` with no event between; a
  person's pick or a reading arriving during an `_apply` (the
  `_selection_seq` guard) is not modeled.
- The Store record is written at the change (`async_delay_save(..., 0)`): a
  crash within that one event-loop turn would lose it too, and is not
  modeled.
- Not modeled: Safety priority, floors and ceilings, Dry, Comfort priority,
  lost heads, eco, lockouts, standby, sensor freshness, cloud heads, the dwell
  wakeup timer. The arbitration tables cover Safety, floors, Dry and Comfort
  priority for one decision, not over time.
- The tables cover Comfort priority 0 and 1 only; non-numeric and
  non-positive values are `tests/test_comfort_priority_logic.py`'s.
- A pass covers the model and the tables at these bounds, not the whole
  integration, and nothing here is a hardware result.

Keep this in step with the code with `/maintain-formal-verification-skill`.
