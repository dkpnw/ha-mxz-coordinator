# Restart continuity

A Home Assistant restart is not a fresh start for MXZ. The mode-change wait
continues from the stored time of the last change, so a restart 400 s into a
600 s wait holds only the 200 s left, and the thermostats show that; when it
ends the change goes through by itself. A room caught mid-run by a restart,
inside its drift and still running to its target, keeps running after the
restart and stops at its target.

## Sub-features

- `restart-wait-left` after a restart the wait holds only what is left.
- `restart-wait-shown` the room thermostat's `mode_change_hold_remaining`
  shows the time left, never a fresh full wait.
- `restart-wait-ends` the wakeup makes the held change when the rest ends.
- `restart-run-resumes` a run in progress (cool or heat) resumes after a
  restart and ends at the target.

## How to get to it (user POV)

- Restart Home Assistant (Settings, System, Restart) while a mode change is
  recent, or while a room is still running toward its target.
- Watch `sensor.mxz_coordinator_plan` (`state`, `mode_change_allowed`,
  `seconds_since_mode_change`, `zones[n].engage`) and the room thermostat's
  `mode_change_hold_remaining`.

## Driving it with pytest-homeassistant-custom-component

Preconditions:

- Wait: `tests/test_dwell_wakeup.py` `_setup_dwell(hass)` (default 600 s
  wait, both rooms enabled at 70) and `tests/test_antiflip_persist.py`
  `_flip_to_heat`.
- Run: `tests/test_startup_continuity.py` `_setup` (no wait) and `_run_b`.
- Restart: `journeys/mxz_journey.py` `restart` (restore data dumped, entry
  unloaded, the per-entry store instances dropped, entry set up again).
- Journeys: `journeys/test_restart_continuity.py`
  `test_a_restart_mid_wait_keeps_only_what_is_left` and
  `test_a_run_in_progress_resumes_after_a_restart[cool|heat]`.

- **Change.** Room 1 reads 60. The plan changes to `heat`.
- **Restart mid-wait.** The wall clock steps 400 s (`jump_wall_clock`), Room 1
  reads 75, Home Assistant restarts. The plan is `heat` with
  `mode_change_allowed: false`, `seconds_since_mode_change` 400 to 410, the
  thermostat's `mode_change_hold_remaining` above 190 and at most 200, and
  Room 1's head is not `cool`.
- **Rest of the wait.** Both clocks move the time left plus 2 s
  (`_let_time_pass`) and Home Assistant's timers fire (`_fire_due`). The plan
  is `cool`, Room 1's head `cool`, and a new wait shows.
- **Run, then restart.** Room 2 runs `cool` (or `heat`) and reads 71 (69),
  inside its drift: engage and head are that mode. Home Assistant restarts:
  engage and head are still that mode, Room 1's head `fan_only`.
- **Run ends.** Room 2 reads 70.5 (69.5): still running. Then 70: `satisfied`,
  head `fan_only`.
- **Proof.** The transcripts under `<clock>/restart-continuity/`.

## Gotchas

- The restart is in process: a store round trip and a new coordinator, not a
  new Python process and not hardware. A power cut that loses an unwritten
  store is outside it.
- Across the restart the wait is timed on the wall clock (the stored change
  time); within a run it is the monotonic clock. To pass time inside a run,
  move both (`_let_time_pass`); `jump_wall_clock` alone does not shorten a
  running wait, by design.
- The restored mode comes from the shared-mode select's restore data, which
  the restart helper dumps first, as a real shutdown does; the wait's start
  comes from the entry's own store, which the helper drops from memory so the
  setup reads it back from storage.
