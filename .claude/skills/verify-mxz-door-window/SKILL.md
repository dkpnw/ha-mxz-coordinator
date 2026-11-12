---
name: verify-mxz-door-window
description: "Formal checks of MXZ Coordinator's door/window pause: a room whose door or window is open past its open delay has its head turned off, counts for nothing in the shared mode, gets no fan write, and stays paused across a reload or restart; a person's change at the head or the Window Override switch takes it over until the window reads closed. A TLA+ model checked with TLC (ci-window, ci-window-fan, ci-arbitration-window), Hypothesis properties and integration tests on the real code, and broken copies of both that each check must catch. Use before and after any change to openings.py (OpeningPause), logic.opening_state, opening_due or opening_reading, the pause branches of coordinator.py _compute and _apply, or _observe_paused_fan, and when the model or a replay reports a break."
---

# Verify the door/window pause

A room with a door/window sensor stops heating or cooling while it is open, and resumes once
it has been closed for its delay. The harm this guards: a head left running with the window
open, a paused room still steering the shared mode, a pause or an override lost at a restart,
or a person's takeover undone while the window is still open.

Paths are from the repository root. `docs/STATE-MACHINES.md` section 3.13 is the machine in
words; `docs/formal/STATECHART.md` "A door or window" (W1 to W11) is its chart, and "User
flows" has the chart the model itself draws.

## What is checked

Model rules (`docs/formal/tla/MXZCoordinator.tla` with `WithWindow`, room 1):

- **PauseStopsHead.** After a decision, a paused room's head is off, gets no fan write, and its
  call counts for nothing. Harm: the head runs with the window open, or a paused room keeps or
  turns the shared mode.
- **OverrideHolds.** A person's override (a change at the head, or Window Override on) holds
  until the window reads closed. Harm: MXZ turns the head back off under the person.
- **RestartRestores.** A start brings back the stored door/window state and override (with
  the mode and the hold). Harm: a restart resumes a room whose window is still open.
- The other rules each config lists hold with the window in the model.
- Reachability goals, which must break so the rules aren't vacuous: `NeverPaused`,
  `NeverOverrideRunning`, `NeverPauseRestored`.

Code checks (in CI on every lane): `tests/test_properties_window.py` (Hypothesis, on the real
integration and on `logic.opening_state`), the window part of
`tests/test_properties_restart.py` (`test_a_start_changes_nothing_anyone_can_see`),
`tests/test_door_window_logic.py` and `tests/test_door_window.py`.

## Coverage record

Sync commit: `ca78324aca3d5402252a585693ae98193cee24ca` (main, release 3.22.0). The model and
tests were confirmed against the code at that commit. Move the sync commit forward only after
you have re-read the functions below at the new commit, confirmed the model still describes
them, and run both commands under Run.

| Check | Covers (custom_components/mxz_coordinator/) |
| --- | --- |
| Model `Decide` (the window part) | `openings.py` `OpeningPause.step`, `OpeningPause._transition`, `OpeningPause._save`; `logic.py` `opening_state`, `opening_due`, `opening_reading`; `coordinator.py` `_compute` (a paused room has no call, no run, an empty latch), `_apply` (a paused head straight to off), `_observe_paused_fan` |
| Model `WindowReads` | `openings.py` `OpeningPause.on_change` |
| Model `PersonMode` | `coordinator.py` `_on_head_change`; `openings.py` `OpeningPause.take_over` |
| Model `OverrideSwitch` | `openings.py` `OpeningPause.async_set_override` |
| Model `Reload`, `Restart` | `openings.py` `async_load_openings`, `OpeningRecord.restored` |
| Code mutants C14 to C19, C27 to C30 | `_apply`, `_compute`, `OpeningPause.take_over`, `OpeningPause.step`, `OpeningPause._save`, `opening_state`, `opening_due`, `opening_reading` |

Does a change touch it?

```sh
git diff --stat ca78324aca3d5402252a585693ae98193cee24ca -- \
  custom_components/mxz_coordinator/openings.py custom_components/mxz_coordinator/logic.py \
  custom_components/mxz_coordinator/coordinator.py docs/formal/tla/MXZCoordinator.tla
```

If it lists a file, read the diff against the table. A change inside a listed function needs
the check run, and possibly the model updated.

## Tools

The question is the order of readings, delays, a person's takeover, the switch and a start.
That suits a model checker, and TLA+ is the default for it. `logic.opening_state` is a pure
function with a small input space, so Hypothesis runs it and the integration on generated
sequences. TLC is `tla2tools.jar` 1.7.4 (sha256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`; the runner refuses any
other jar), on Java 11 or later. The tests run on a lane venv per `docs/DEVELOPMENT.md`.

## Run

```sh
export TLA2TOOLS=/path/to/tla2tools.jar      # and JAVA if java isn't on PATH
export MXZ_PYTHON=/path/to/lane/bin/python   # a lane venv with hypothesis in it
python3 tools/formal_area.py check window    # about 5 minutes; ends AREA_OK or AREA_FAILED
python3 tools/formal_area.py mutants window  # under a minute
```

`check` runs TLC on `ci-window`, `ci-window-fan` and `ci-arbitration-window` (each must pass
every rule it lists; `ci-window-fan` is most of the time), requires each reachability goal to
break, then runs the four test files above. `mutants` breaks a scratch copy, never the
checkout: W1 to W7 break the spec, C14 to C19 and C27 to C30 the code, and each must be caught
by its named rule or test. `python3 tools/formal_area.py list` prints what each part runs.
Output is kept in the folder printed on the first line (`--out` chooses it).

The mode-hold model has its own door/window configs and broken copies (`ci-openings*`, H18 to
H24 on `tests/test_door_window.py`); `verify-mxz-formal` runs those.

## Read the results

Read every TLC result, a pass or a break, with the pstack `/statechart` skill, on copies of
`docs/formal/tla/MXZCoordinator.tla` and the config in a scratch folder outside the
repository, with `docs/formal/charts/window.ts` as the view. It tells a break as its shortest
path in a person's words ("the window reads open", "a person takes the room over"). The `ci-*`
configs are past its chart budget, so it gives the bug paths only; for the whole chart, use
`docs/formal/charts/window.cfg` as `docs/formal/STATECHART.md` "User flows" says.

A break is a real bug or a modeling error. Tell them apart before reporting:

1. Replay the path on the real code, one step per model action, as `tests/test_door_window.py`
   does: an invented sensor's `on` and `off`, the clock stepped past a delay, a person's mode
   on the head, the Window Override switch, a reload or `rig.restart()`. Assert the rule, so
   the replay fails while the bug is there.
2. Give it a control: the same steps without the trigger. The control must pass.
3. If the replay does not fail, assume the model is wrong until you show it is not. The model
   counts time in whole ticks and has one door/window room, with its fan only in
   `ci-window-fan`.

Never weaken a rule, a bound or a test to make a check pass. When the promise changes on
purpose, change the rule, the docs and the tests together, and say so in the PR.

## Findings

None open. Two documented exceptions, each pinned (`docs/formal/README.md`, "Documented
exceptions"): a change on the head during the close delay lasts one compute (chart W8), and a
fixed standby park sends a paused head to the park mode, which `PauseStopsHead` doesn't see
because standby is outside the model.

## Scope and bounds

- `ci-window`: one room with every reading position, delays of 1 tick, time 0 to 3, reloads
  and restarts; 467,272 distinct states. `ci-window-fan`: the window and the fan together,
  delays 0, one reading, reloads; 4,130,301. `ci-arbitration-window`: two rooms, one with the
  window, a hold of 1, time 0 to 2, restarts; 1,269,790 (TLC 1.7.4, at the sync commit).
- Not in the model: notices, vanes owed at a resume, the Dry command on a resume, standby, and
  more than one door/window room. `tests/test_door_window.py` holds the first three.
- A pass covers the model at these bounds and the tests as drawn, not the whole integration.
  Nothing here ran on a real sensor or head.

Keep this in step with the code with `/maintain-formal-verification-skill`.
