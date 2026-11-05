# Formal checks of the coordinator

Three checks of one set of rules: the coordinator's mode arbitration, the hold between mode
changes (dwell, anti-flip), the door/window pause, the manual fan hold and restart continuity.

| File | What it is |
| --- | --- |
| [STATECHART.md](STATECHART.md) | The machine as a chart, each transition mapped to its code. |
| [tla/MXZCoordinator.tla](tla/MXZCoordinator.tla) | The same machine in TLA+. TLC checks every order of steps at small bounds. |
| `tests/test_properties_*.py` | Hypothesis properties on the real integration, in the normal suite on every lane. |
| [PARKED-FINDINGS.md](PARKED-FINDINGS.md) | What these checks found and this work did not fix. |

The model finds orderings nobody thought to test, and the properties check that the code
itself does what the model says. A pass covers the model at these bounds and the examples
drawn. It doesn't cover the whole integration, and nothing here is a hardware result.

`verification/modehold/` and `.claude/skills/verify-mxz-formal/` go deeper on one part:
hands-off votes, wall-clock steps across restarts, and the select's restore data. They
stay as they are, and this model doesn't repeat them.

## The rules

| Rule | TLC invariant | Hypothesis properties on the real code |
| --- | --- | --- |
| A manual fan choice is never written over, apart from the exceptions below. | `ManualFanKept` | `test_a_pick_is_never_written_over_until_it_is_handed_back`, `test_a_cloud_heads_pick_is_never_written_over` |
| A manual fan choice shows as a hold, across every start. | `ManualFanShown` | `test_a_pick_is_never_written_over_until_it_is_handed_back`, `test_a_start_changes_nothing_anyone_can_see` |
| MXZ doesn't hold a speed nobody touched, with a head up to one write behind. | `NoFalseHold` | `test_a_slow_heads_own_echo_is_not_a_hold_one_write_behind` |
| No mode flip inside the hold, across any start. | `NoFlipInDwell` | `test_no_flip_inside_the_hold_and_no_wait_after_it`, `test_arbitrate_never_changes_the_mode_while_the_hold_holds` |
| With the hold over and only the other mode calling, the mode changes. | `NoExtraWait` | `test_no_flip_inside_the_hold_and_no_wait_after_it` |
| A start neither restarts the hold nor forgets it. | `HoldClockKept` | `test_no_flip_inside_the_hold_and_no_wait_after_it`, `test_a_start_keeps_what_is_left_of_the_hold_and_never_more`, `test_no_stored_change_holds_nothing` |
| A start restores what was stored: the mode, the hold, the door/window state and an override. | `RestartRestores` | `test_a_start_changes_nothing_anyone_can_see`, `test_a_start_resumes_the_recorded_mode`, `test_any_stored_record_loads_as_a_readable_record` |
| After a decision no head runs against the shared mode. A run keeps Resting mode waiting. | `HeadsFollowMode` | `test_no_flip_inside_the_hold_and_no_wait_after_it`, `test_a_run_in_the_shared_mode_keeps_it_from_resting_mode` |
| A window pause stops the head, writes it no fan, and its call counts for nothing. | `PauseStopsHead` | `test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes`, `test_a_paused_room_never_keeps_or_turns_the_shared_mode`, `test_the_delays_decide_a_pause_and_a_resume`, `test_only_a_running_delay_has_a_wakeup`, `test_only_on_and_off_are_readings`, `test_no_reading_never_starts_a_pause_and_never_ends_one` |
| A person's override holds until the window reads closed. | `OverrideHolds` | `test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes`, `test_an_override_is_never_paused_and_a_closed_reading_ends_it` |

## Documented exceptions

Each is explicit, not a gap. In the spec, a pick's `pickKind` names which of these covers
it, and only a `strict` pick is bound with no exception. In the tests, each exception is
pinned by a passing test that fails if the behavior changes. They aren't xfails because
`tools/pytest_phases.py`, which every CI suite pass runs under, fails a run on any xfail.
[The cloud1104 parked list](#where-the-known-items-came-from) is where the first three
were found.

| Exception | Spec | Pinned by |
| --- | --- | --- |
| **A pick of a remembered speed reads as MXZ's echo**, and boost writes over it. It's found on cloud heads and shared with local ones. The memory is the last two distinct speeds in `_fan_cmd` and `_fan_prev`, which also takes in a speed that was held or handed back: hold low, hand it back from the remote, pick low again with nothing written between, and it isn't held. | `pickKind = "echo"`; reachable by `NeverKnownExceptionLosesAPick` | `test_the_exception_is_exactly_the_last_two`, `test_the_exception_can_lose_the_pick`, `test_the_exception_on_a_cloud_head` |
| **A room's target isn't converted on a °F/°C switch.** The stored number is read in the new unit and held to its range. `docs/CONFIGURATION.md` tells people to re-enter targets. | Not modeled: one unit | `test_known_exception_a_target_is_not_converted_on_a_unit_switch` |
| **A live unit switch takes effect at the next load.** `docs/CONFIGURATION.md` says to reload or restart. | Not modeled: one unit | `test_known_exception_a_live_unit_switch_takes_effect_at_the_next_load` |
| **Picking the speed already showing can't be read.** Nothing changes for MXZ to see. | Not a step of the machine | `test_picking_the_speed_already_showing_is_invisible` in `tests/test_drive.py` |
| **A pick MXZ hasn't read before a stop, or made while it is down**, can read as its own speed where the token could be ours. `_reconcile_restored_hold` documents it. | `pickKind = "outage"` | `test_s6_gesture_during_outage_not_held_before` in `tests/test_fan_hold_restore.py` |
| **A crash can lose a hold.** The Fan auto truth is in restore data, which Home Assistant writes at a stop and every 15 minutes. After a crash, the switch's docstring's remembered-token edge applies. Most other speeds still hold, conservatively. | `pickKind = "crash"` | The spec's `ci-fan-crash` |
| **A hold seeded with no memory can be adopted** if it is a rung the ladder would stay on. `_seed_matches_boost` accepts this. | `pickKind = "seed"` | `test_restart_adopts_a_rung_held_by_hysteresis` in `tests/test_drive.py` |
| **A start can hold a speed nobody touched**: `_reconcile_restored_hold`'s conservative false hold. A speed boost could never have written holds, even right after a hand-back. | `NoFalseHold` counts live reads only | `test_s11b_not_held_seed_above_boost_ceiling` in `tests/test_fan_hold_restore.py` |
| **A write already sent can land after a Fan auto gesture.** MXZ writes nothing after the gesture, but a head still applying the earlier write can report it next. | `pickKind = "inflight"`; `gesture` | The spec's `ci-fan-lag` |
| **A change on the head during the close delay** lasts one compute: the window has already closed, so the room goes on closing. It's chart transition W8. | Reachable in `ci-window` | `OverrideHolds` binds only while the window isn't closed |

### Where the known items came from

The first three exceptions in the table were found by the cloud head review, and parked
outside this repository for a product decision: stored targets across a unit switch, a live
unit switch, and a pick of the previous fan speed.

Open finding F1, a slow head read as a person's pick, is in
[PARKED-FINDINGS.md](PARKED-FINDINGS.md). `NoFalseHoldTwoBehind` is required to break until
it's fixed.

## What is modeled, and what isn't

**In the spec.** Rooms in priority order, enabled, with the default drift and threshold.
Each room's reading as one of six positions, its engage latch, and its head's mode. The
shared mode, Resting mode, the hold and its record. Room 1's door/window state, delays,
override and store. Room 1's fan: reported speed, command memory, ladder index, latch,
pending restore, restore data, and a head that reports late. A person's pick of the mode,
the fan and the head's mode. The Fan auto and Window Override switches. Reload, restart with
the fan touched during the outage, and crash.

**Not in the spec.**

- Hands-off votes and wall-clock steps: `verification/modehold/` has them. There's one clock here.
- Safety priority, floors and ceilings, Comfort priority, Dry, eco, lockouts, standby, lost
  heads, sensor freshness: `tests/test_modehold_properties.py` covers the arbitration ones for one decision.
- Cloud heads' batching, write interval and confirm wait. Their latch is the same
  `_observe_fan_latch`, so the rule is in the model, and the properties run on a cloud head.
- Idle actions other than fan_only, the coil-dry wait, vanes, setpoints, notices, `fan_on_pending`.
- Anything between `_compute` and the end of `_apply`: one Decide is one step, so a pick
  arriving while a delivery awaits isn't modeled.
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
export TLA2TOOLS=/path/to/tla2tools.jar   # and JAVA, if java isn't on PATH
python3 tools/formal_tlc.py check     # every config passes every rule it lists
python3 tools/formal_tlc.py cover     # every reachability goal has a path, each open finding still breaks
python3 tools/formal_tlc.py mutants   # every broken copy of the spec breaks its named rule
python3 tools/formal_tlc.py all       # those three, as the workflow runs them
```

By hand, from `docs/formal/tla/`: `java -cp "$TLA2TOOLS" tlc2.TLC -workers auto -config ci-fan.cfg MXZCoordinator.tla`.

| Config | What varies |
| --- | --- |
| `ci-fan` | One room's fan, outage picks and restarts, no passing time |
| `ci-fan-crash` | The same, with crashes and the periodic restore-data dump |
| `ci-fan-lag` | The same, with a head that reports late |
| `ci-window` | One room's door/window, with delays, reloads and restarts |
| `ci-window-fan` | The door/window and the fan together, reloads, delays at 0, one reading |
| `ci-arbitration` | Two rooms, picks, reloads, restarts, crashes, a hold of 2 ticks |
| `ci-arbitration-rest-heat` | The same with Resting mode set to heat |
| `ci-arbitration-window` | Two rooms, one with a door/window |

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
MXZ_PYTHON="$(command -v python)" python3 tools/formal_tlc.py code   # every broken copy of the code fails its named property
python -m pytest verification/findings -q -p no:cacheprovider        # open findings, each a strict xfail
```
