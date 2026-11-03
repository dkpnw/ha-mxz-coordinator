# Manual fan choices preserved

With fan boost on, MXZ drives a cooling or heating head's fan speed. When a
person picks a speed on the head itself, MXZ stops writing that head's fan:
the room's Fan auto switch turns off and the plan shows a fan hold. The
speed stays through later computes, through the room reaching its target
(no return to `auto`), and through a Home Assistant restart. Turning Fan auto
back on hands the fan back, and MXZ writes it again at once.

## Sub-features

- `fan-boost` a room cooling hard gets the boost speed (`high`).
- `fan-hold` a person's pick holds: `fan_hold: true`, Fan auto `off`.
- `fan-idle` reaching the target parks the head without resetting the fan.
- `fan-restart` the hold and the speed survive a restart, with no MXZ fan write.
- `fan-handback` Fan auto on ends the hold and MXZ writes the fan again.

## How to get to it (user POV)

- Leave Fan boost on (Configure, Fan and idle; on by default), then pick a fan
  speed on a head's own card or remote.
- Watch the head's `fan_mode`, the room's `switch.mxz_coordinator_*_fan_auto`,
  and `sensor.mxz_coordinator_plan` `zones[n].fan_hold`.
- Hand the fan back with the room's Fan auto switch.

## Driving it with pytest-homeassistant-custom-component

Preconditions:

- `tests/test_drive.py` `_setup_mock_heads` and `_setup_fan_boost` (fan
  boost on, both rooms enabled, Room 1 target 62).
- Journey: `journeys/test_manual_fan.py`
  `test_a_picked_fan_speed_holds_through_a_restart`.

- **Boost.** Room 1 reads 67. Head `cool`, fan `high`, Fan auto `on`.
- **Person picks quiet.** `climate.set_fan_mode` `quiet` on the head, then two
  computes. Fan `quiet`, `fan_hold: true`, Fan auto `off`.
- **Target reached.** Room 1 reads 62. Head `fan_only`, fan still `quiet`.
- **Restart.** Room 1 reads 66 and Home Assistant restarts. Head `cool`, fan
  `quiet`, `fan_hold: true`, Fan auto `off`, and no `set_fan_mode` to the
  head since the person's own.
- **Hand back.** `switch.turn_on` on Room 1's Fan auto. `fan_hold: false`,
  the fan is no longer `quiet`, and MXZ has sent `set_fan_mode` again.
- **Proof.** `<clock>/manual-fan-preserved/test_a_picked_fan_speed_holds_through_a_restart.json`.

## Gotchas

- The pick is a call to the head (`climate.mock_head_a`), not to the MXZ
  thermostat: that is how a person reaches a head's fan.
- The transcript counts the person's own `set_fan_mode` call too; compare
  counts from after the pick, as the journey does.
- Without the hand-back step a silent fan could also mean a broken boost; the
  hand-back is what shows the silence was the hold.
