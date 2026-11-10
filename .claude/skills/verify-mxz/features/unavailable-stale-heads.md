# Unavailable or stale heads

A head can drop off Home Assistant (its integration reports it
`unavailable`), and a room sensor can stop reporting. MXZ keeps a lost head's
call counting, shown as `stale`, for the lost-vote time limit (default 30
minutes), because the head may still be running it; at the limit the call is
`expired`: still shown, no longer counted, and the sibling room gets the
outdoor unit. A room sensor with a contracted reporting interval goes `stale`
at the third missed report: that room stops calling and its head parks, its
last reading still shown, until one fresh report recovers it.

## Sub-features

- `lost-stale` a lost head's room still holds the mode while `stale`.
- `lost-expired` at exactly the limit the room is `expired` and the sibling runs.
- `lost-back` a head seen again is `live` at once, with no `lost_since`.
- `sensor-stale` a sensor silent for 3 intervals makes its room `stale`:
  demand `neutral`, head parked, last reading shown.
- `sensor-recover` one fresh report makes it `healthy` and calling again.

## How to get to it (user POV)

- Unplug or lose a head: its entity reads `unavailable`.
- Give a room a sensor freshness contract (Configure, the room's page,
  Sensor freshness: Report interval) and let the sensor go quiet.
- Watch `sensor.mxz_coordinator_plan` `zones[n]`: `vote_status`,
  `lost_since`, `sensor_health`, `demand`, `temp`.

## Driving it with pytest-homeassistant-custom-component

Preconditions:

- Lost head: `tests/test_lost_vote.py` `_standoff(hass)` (Room 1 at 76 calls
  cool and wins, Room 2 at 64 calls heat; default 600 s wait).
- Stale sensor: `tests/test_sensor_freshness.py` `_setup_fresh` with
  `primary={ZONE_REPORT_INTERVAL: 5}` (a 5-minute interval, so a 15-minute
  window), Room A at 75 under a frozen clock (`freezer`).
- Journeys: `journeys/test_unavailable_stale.py`
  `test_a_lost_head_holds_while_stale_then_lets_the_sibling_run` and
  `test_a_stale_sensor_stops_calling_and_one_fresh_report_recovers`.

- **Head lost.** Room 1's head goes unavailable (`_lose`), then a compute.
  Room 1's row is `stale` with demand `cool` and a `lost_since`; the plan
  stays `cool`; Room 2's head stays `fan_only`.
- **Just short of the limit.** 29 min 58 s pass on the coordinator's clock
  (`pass_time`). Still `stale`, still `cool`.
- **At the limit.** 2 s more, with no mode-change wait left. Room 1 is `expired` (its demand `cool`
  still shown); the plan is `heat` and Room 2's head is `heat`.
- **Head back.** `_back`. Room 1 is `live`, `lost_since` is `null`, and the
  standoff is back.
- **Sensor healthy.** Room A reports 75. `healthy`, demand `cool`, head `cool`.
- **Sensor just short.** The clock moves 14 min 59 s. Still `healthy` and `cool`.
- **Sensor stale.** 1 s more. `stale`, demand `neutral`, engage `satisfied`,
  `temp` still 75, head `fan_only`.
- **Sensor recovers.** Room A reports 75 again. `healthy`, `cool`, head `cool`.
- **Proof.** The two transcripts under `<clock>/unavailable-stale-heads/`.

## Gotchas

- `_standoff` runs the default 600 s mode-change wait. The journey lets it
  run out (`expire_dwell`) before the head is lost: after the loss it would
  age the loss too, by up to 600 s, since every wait reads the one clock.
- The lost-vote limit is half-open: `expired` AT the limit, not after it.
- Freshness runs on the wall clock against the sensor's `last_reported`, so
  the sensor journey moves a frozen clock (`freezer`) and fires timers;
  the mode-change wait and the lost-vote limit run on the coordinator's
  clock, so time passes on it (`pass_time`) instead.
- Home Assistant drops calls to an unavailable entity, so what MXZ sends a
  lost head is not evidence of what reached it.
