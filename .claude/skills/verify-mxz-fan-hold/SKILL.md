---
name: verify-mxz-fan-hold
description: "Formal checks of MXZ Coordinator's manual fan hold and Fan auto: a fan speed a person picks at the head is held and never written over until it is handed back, a speed MXZ wrote is never mistaken for a person's pick (also from a head that reports late), and both survive a restart or crash as documented. A TLA+ model checked with TLC (ci-fan, ci-fan-crash, ci-fan-lag), Hypothesis properties and tests on the real code, and broken copies of both that each check must catch. Use before and after any change to coordinator.py _observe_fan_latch, _apply_fan, _remember_fan_write, _fan_caught_up, _hold_at, _reconcile_restored_hold, async_set_fan_auto, _cloud_fan, or the Fan auto switch's restore data in switch.py, and when the model or a replay reports a break."
---

# Verify the manual fan hold

MXZ's fan boost picks a fan speed for each head, and a person can pick one at the head's
remote. MXZ can only tell the two apart by remembering what it wrote. The harm this guards:
boost writes over a speed a person chose, or MXZ holds a speed nobody touched (Fan auto turns
off by itself) because a slow head showed an older write of MXZ's, during a run or across a
restart.

Paths are from the repository root. `docs/STATE-MACHINES.md` section 3.12 is the machine in
words; `docs/formal/STATECHART.md` "A head's fan" (F1 to F11) is its chart, and "User flows"
has the chart the model itself draws. `docs/formal/README.md` "Documented exceptions" lists
every case where a pick is written over on purpose, and `docs/formal/PARKED-FINDINGS.md` has
F1 (fixed) and its limits.

## What is checked

Model rules (`docs/formal/tla/MXZCoordinator.tla` with `WithFan`, room 1's fan):

- **ManualFanKept.** A person's pick is never written over, apart from the documented
  exceptions; in the spec, only a pick of kind `strict` is bound with none. Harm: the speed a
  person chose is lost at the next update.
- **ManualFanShown.** A held pick shows as a hold (Fan auto off), across every start. Harm:
  Fan auto reads on while MXZ holds, or the hold is lost at a restart.
- **NoFalseHold.** MXZ doesn't hold a speed nobody touched, with the head up to one write
  behind. **NoFalseHoldTwoBehind** (in `ci-fan-lag`): nor further behind, including at a
  start's first read. Harm: Fan auto turns itself off and boost stops.
- The other rules each config lists hold with the fan in the model.
- Reachability goals, which must break so the rules aren't vacuous:
  `NeverStrictHoldAcrossAStart`, `NeverKnownExceptionLosesAPick` (the documented exception
  does happen), `NeverLagEcho`, `NeverTwoBehindEcho`.

Code checks (in CI on every lane): `tests/test_properties_fan.py` (Hypothesis, on the real
integration, local and cloud heads), the fan part of `tests/test_properties_restart.py`,
`tests/test_fan_lag_memory.py` (the order of a lagging head's reports, which the model
doesn't have) and `tests/test_fan_hold_restore.py` (what a start reconciles).

## Coverage record

Sync commit: `ca78324aca3d5402252a585693ae98193cee24ca` (main, release 3.22.0). The model and
tests were confirmed against the code at that commit. Move the sync commit forward only after
you have re-read the functions below at the new commit, confirmed the model still describes
them, and run both commands under Run.

| Check | Covers (custom_components/mxz_coordinator/) |
| --- | --- |
| Model `Decide` (`Observe`, `ApplyFan`, `Rolled`) | `coordinator.py` `_observe_fan_latch`, `_seed_matches_boost`, `_seed_is_own_idle`, `_hold_at`, `_apply_fan`, `fan_for_delta`, `_write_fan`, `_remember_fan_write`, `_fan_caught_up`, `_cloud_fan` |
| Model `PersonFan`, `HeadReports` | `coordinator.py` `_on_head_change` (the fan report), `_fan_caught_up` |
| Model `FanAutoOnGesture`, `FanAutoOffGesture` | `coordinator.py` `async_set_fan_auto`, `_adopt_fan_speed` |
| Model `Restart`, `Crash`, `Dump` (`Reconcile`) | `switch.py` `MXZZoneFanAutoSwitch.async_added_to_hass`, `MXZZoneFanAutoSwitch.extra_restore_state_data`; `coordinator.py` `restore_fan_hold`, `fan_command_memory`, `fan_older_record`, `_reconcile_restored_hold` |
| Code mutants C8 to C13, C22, C23, C31 to C41 | the functions above; each mutant names its function in `tools/code_mutants.py` |

Does a change touch it?

```sh
git diff --stat ca78324aca3d5402252a585693ae98193cee24ca -- \
  custom_components/mxz_coordinator/coordinator.py custom_components/mxz_coordinator/switch.py \
  docs/formal/tla/MXZCoordinator.tla
```

If it lists a file, read the diff against the table. A change inside a listed function needs
the check run, and possibly the model updated.

## Tools

The question is the order of MXZ's writes, a head's late reports, a person's picks, Fan auto
and a start. That suits a model checker, and TLA+ is the default for it. The model's lagging
head reports only the latest write, so the order of a head that shows every write is held by
Hypothesis and plain tests on the real code instead. TLC is `tla2tools.jar` 1.7.4 (sha256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`; the runner refuses any
other jar), on Java 11 or later. The tests run on a lane venv per `docs/DEVELOPMENT.md`.

## Run

```sh
export TLA2TOOLS=/path/to/tla2tools.jar      # and JAVA if java isn't on PATH
export MXZ_PYTHON=/path/to/lane/bin/python   # a lane venv with hypothesis in it
python3 tools/formal_area.py check fan       # about 2 minutes; ends AREA_OK or AREA_FAILED
python3 tools/formal_area.py mutants fan     # about 3 minutes
```

`check` runs TLC on `ci-fan`, `ci-fan-crash` and `ci-fan-lag` (each must pass every rule it
lists), requires each reachability goal to break, then runs the four test files above.
`mutants` breaks a scratch copy, never the checkout: F1 to F9 break the spec, and C8 to C13,
C22, C23 and C31 to C41 the code; each must be caught by its named rule or test.
`python3 tools/formal_area.py list` prints what each part runs. Output is kept in the folder
printed on the first line (`--out` chooses it).

## Read the results

Read every TLC result, a pass or a break, with the pstack `/statechart` skill, on copies of
`docs/formal/tla/MXZCoordinator.tla` and the config in a scratch folder outside the
repository, with `docs/formal/charts/fan.ts` as the view. It tells a break as its shortest
path in a person's words ("a person picks high at the remote", "MXZ writes medium"). The
`ci-*` configs are past its chart budget, so it gives the bug paths only; for the whole chart,
use `docs/formal/charts/fan.cfg` as `docs/formal/STATECHART.md` "User flows" says.

A break is a real bug or a modeling error. Tell them apart before reporting:

1. Replay the path on the real code, one step per model action, as
   `tests/test_fan_lag_memory.py` does: an invented head that reports late, a person's pick at
   it, Fan auto on or off, `rig.restart()` or a crash. Assert the rule, so the replay fails
   while the bug is there. A finding's replay goes in `verification/findings/`, outside CI's
   suite, as F1's did.
2. Give it a control: the same steps without the trigger. The control must pass.
3. If the replay does not fail, assume the model is wrong until you show it is not. Known
   differences: the model's head reports only the latest write and keeps older writes as a
   set, its ladder is three rungs with the ceiling at the second, and its echo after a head
   catches up is wider than the code's (`docs/formal/README.md`, "What is modeled"). And
   in the model a gesture can come between a person's pick and MXZ's next update. In the test
   rig, a head's fan report asks for an update that runs at once, so such a path doesn't
   replay there. Whether Home Assistant's refresh debouncer can let a gesture in first in a
   house is not tested (2026-11-11: Fan auto on between a pick and its read, a scratch probe).

Check every break against the documented exceptions first: a pick of kind `echo`, `outage`,
`crash`, `seed` or `inflight` can be written over on purpose, and each has a pinned test.
Never weaken a rule, a bound or a test to make a check pass. When the promise changes on
purpose, change the rule, the docs and the tests together, and say so in the PR.

## Findings

None open for a head that shows MXZ's writes in order or jumps to the latest. F1, a slow head
read as a person's pick, is fixed; its limits (a head that shows writes out of order, a lag
past `FAN_OLDER_WRITES`, one moment for a start) are in `docs/formal/PARKED-FINDINGS.md`. A
re-pick, on a cloud head, of the speed MXZ just wrote is an accepted exception.

## Scope and bounds

- One room's fan, readings that only call for cooling (`CoolingReadings`), a three-rung ladder
  with the boost ceiling at the second, no passing time. `ci-fan`: picks during an outage and
  restarts, 145,949 distinct states. `ci-fan-crash`: crashes and the periodic restore-data
  dump too, 287,947. `ci-fan-lag`: a head that reports late, however many writes behind,
  310,946 (TLC 1.7.4, at the sync commit).
- Not in the model: a second room's fan (the state is per head), cloud batching and its write
  interval, idle actions other than `fan_only`, vanes. The properties run on a cloud head.
- A pass covers the model at these bounds and the tests as drawn, not the whole integration.
  Nothing here ran on a real head.

Keep this in step with the code with `/maintain-formal-verification-skill`.
