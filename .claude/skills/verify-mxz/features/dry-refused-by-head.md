# Dry refused by a head

A room with Allow Dry mode on offers Dry on its MXZ thermostat. Picking it
sends the head dry once. When the head rejects that write, the thermostat
shows Heat/Cool again the moment the call returns its error. When the head
takes the call but never reports dry, the thermostat leaves Dry when the
pick's wait (60 s) ends, the log says why, and the room's own call runs
again. Either way MXZ never sends dry a second time.

## Sub-features

- `dry-offered` Dry is in the thermostat's modes for an Allow Dry room.
- `dry-rejected` a rejected pick shows Heat/Cool at once, before any plan.
- `dry-unreported` an unreported pick shows Dry, then Heat/Cool at the wait's
  end, with a log line.
- `dry-once` dry is sent exactly once in both cases.
- `dry-plan-back` after an unreported pick, the room's cool call runs.

## How to get to it (user POV)

- Turn on Allow Dry mode on the room's Configure page, then pick Dry on the
  room's thermostat (`climate.mxz_coordinator_bedroom_thermostat` here).
- Watch that thermostat's state and the head's mode; read the Home Assistant
  log for the wait's end.

## Driving it with pytest-homeassistant-custom-component

Preconditions:

- `tests/test_dry_mode.py` `_setup` (Rec room and Bedroom at 70, target 70,
  Bedroom with Allow Dry mode on, no mode wait), with the head class
  `tests/test_climate_immediate.py` `_RefusingHead` (rejects dry with an
  invented error) or `tests/test_dry_entity.py` `LateHead` (with `late`
  set, it takes mode writes and never reports them).
- Journeys: `journeys/test_dry_refused.py`
  `test_a_head_that_rejects_dry_shows_heat_cool_at_once` and
  `test_a_head_that_never_reports_dry_leaves_dry_when_the_wait_ends`.

- **Offered.** The Bedroom thermostat is `heat_cool` and lists `dry`.
- **Rejected pick.** `climate.set_hvac_mode` `dry` on the Bedroom thermostat
  raises the head's error. The thermostat is `heat_cool` at once, the head
  is not `dry`, dry was sent once.
- **Plans after.** The refusal's plan runs, then two computes. The Bedroom
  row's `dry` is `null`, the thermostat `heat_cool`, dry still sent once.
- **Unreported pick.** Pick Dry while the Bedroom reads 76. The thermostat
  shows `dry`, the room's demand is `neutral` while the pick counts, dry
  sent once.
- **Wait ends.** Timers fire past 60 s and the cooldown. The thermostat is
  `heat_cool`, the row's `dry` is `null` and demand `cool`, the log has "was
  sent dry but its head didn't report it in time", dry sent once, and the
  head was then sent `cool`.
- **Proof.** The two transcripts under `<clock>/dry-refused-by-head/`.

## Gotchas

- The rejected pick raises in the caller (the service call fails); the
  journey expects that error. A person sees the call fail and the tile go
  back to Heat/Cool.
- `LateHead` with `late` set models a head that never reports; it is set
  through the head entity, as the suite does, before the pick.
- With the coordinator off or a standby hold, Dry is refused by MXZ itself,
  not by the head; that is a different path (`tests/test_dry_entity.py`).
