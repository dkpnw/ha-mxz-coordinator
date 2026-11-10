# Mixed-mode rooms

Two rooms on one outdoor unit want opposite directions. The room in priority
position 1 gets the shared mode; the other room's head parks and its run is
paused, not dropped. When the first room is satisfied, the shared mode turns
to the waiting room's direction once the mode-change wait (Minimum time
between mode switches, default 600 s) has run out, without anything else
happening, and the paused run resumes.

## Sub-features

- `mixed-standoff` priority position 1 wins a cool/heat standoff.
- `mixed-park` the losing room's head parks at the idle action (`fan_only`).
- `mixed-paused` the losing room keeps its run (`engage` stays its direction).
- `mixed-wait` a reversal waits out the mode-change wait; the thermostats
  show the time left.
- `mixed-wakeup` the wait's end changes the mode by itself.
- `mixed-resume` the paused run resumes and stops at its target.

## How to get to it (user POV)

- Set room targets (`number.mxz_coordinator_*_target`) and let the rooms'
  temperatures drift apart.
- Watch the plan sensor `sensor.mxz_coordinator_plan` (state, `standoff`,
  `zones`) and each room thermostat's `mode_change_hold_remaining`.

## Driving it with pytest-homeassistant-custom-component

Preconditions:

- `tests/test_hands_off.py` `_setup(hass, hysteresis=None)`: both rooms and
  the coordinator enabled, targets 70, the default 600 s wait holding from a
  change stored just before setup.
- Journey: `journeys/test_mixed_mode.py`
  `test_priority_room_wins_then_the_waiting_room_resumes`.

- **Standoff.** Room 1's sensor reads 76 and Room 2's reads 64, then a
  compute. The plan is `cool` with `standoff: true`, demands `cool` and
  `heat`; Room 1's head is `cool`, Room 2's is `fan_only`, and Room 2's
  `engage` is `heat` (paused).
- **Wait holds.** Room 1 reads 70. Room 1 is `satisfied`; the plan stays
  `cool` with `mode_change_allowed: false`; Room 2's head stays `fan_only`;
  the thermostat's `mode_change_hold_remaining` is above 0.
- **Wait ends.** The wait is aged out (`expire_dwell`) and Home Assistant's
  timers fire 602 s on (`_fire_due`), with no recompute asked for. The plan is
  `heat`, Room 2's head `heat`, Room 1's `fan_only`, and a new wait starts.
- **Resume ends at target.** Room 2 reads 70. Room 2 is `satisfied`, its head
  `fan_only`, the plan rests at `heat` (Resting mode Last used), and Room 2's
  head was never sent `cool`.
- **Proof.** `<clock>/mixed-mode-rooms/test_priority_room_wins_then_the_waiting_room_resumes.json`.

## Gotchas

- A reversal inside the wait is held by design. A journey that expects a
  flip must age the wait first, with `tests/dwell.py`, never by writing the
  shared mode.
- `expire_dwell` moves only the coordinator's clock; the wakeup is a Home
  Assistant timer, so it also needs `_fire_due` (time-changed events), or the
  change waits for the next compute.
- Resting mode set to Cool or Heat changes where the mode settles when no
  room calls; this journey uses the default, Last used.
