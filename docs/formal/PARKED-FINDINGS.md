# Findings from the formal checks

No finding is open. This page keeps what F1 was and how it was fixed, and the one accepted
exception on cloud heads.

## Accepted: a re-pick of the same speed on a cloud head

On a cloud head, if a person picks the same fan speed MXZ just wrote, MXZ can't tell. It
reads the speed as its own write, doesn't hold it, and fan boost's next step writes over it.

A cloud head's report only says which speed it has, not who set it, and here the speed is
the one MXZ expects. No change of behavior could separate the two, so this is accepted and
documented, not a bug. To keep that speed, turn the room's **Fan auto** off, which holds
whatever is showing. `test_a_cloud_re_pick_of_the_speed_just_written_is_overwritten` in
`tests/test_cloud_heads.py` pins it. The wider rule it belongs to is in the
[exceptions table](README.md#documented-exceptions).

## F1-fan-two-writes-behind: a slow head could read as a person's pick (fixed)

**What happened.** No person touched anything, yet a room's fan went on hold. Boost stopped
driving it, and Fan auto read off until someone handed it back, across restarts too.

**When.** A local head reported its fan late, and MXZ sent it a second, different speed
before it had reported the first. `_observe_fan_latch` allowed only for the two speeds in
`_fan_cmd` and `_fan_prev`, so a head still showing the speed before those read as a
departure.

**How likely.** Not measured: nothing here ran on hardware. It took two different ladder
rungs in two computes, inside one head's report lag.

**The fix.** The coordinator keeps a small ring per head, `_fan_older`: the speeds that rolled
off `_fan_prev`, one for each rung at most, in the order written. A report that matches one
is MXZ's own older write, not a pick. The ring is only live while the head is behind: a
report of the last command empties it (`_fan_caught_up`), and so does a hold or a hand-back.
A head that reports promptly never reads more than the last two speeds as echoes, as before.
The ring rides a reload or restart in the Fan auto switch's restore data, limited to speeds
boost could write.

**What it costs.** While a head is behind, a person's pick of one of those older writes reads
as an echo, and boost writes over it. A pick of any other speed holds at once, including
across a start. This widens the first row of the
[exceptions table](README.md#documented-exceptions) for as long as the lag lasts, and no longer.

**Evidence.**

| Step | Head shows | MXZ wrote | `_fan_cmd`, `_fan_prev`, `_fan_older` | Held |
| --- | --- | --- | --- | --- |
| Room at 76, the head reports | high | high | high, auto, none | no |
| Room at 72.2 | high | medium | medium, high, none | no |
| Room at 70.7 | high | low | low, medium, high | no (it was yes) |
| The head reports | low | | low, medium, none | no |
| Room back at 76 | low | high | high, low, none | no |

`test_no_person_no_hold_however_far_a_head_lags` and
`tests/test_fan_lag_memory.py` hold it on the real code in the suite, on every lane.
`verification/findings/test_f1_fan_two_writes_behind.py` is the original replay, with its
strict xfail lifted. In the model, `NoFalseHoldTwoBehind` is now a rule of
`docs/formal/tla/ci-fan-lag.cfg` that must pass. `tools/formal_tlc.py mutants F7 F8 F9` shows
it and `ManualFanKept` break if the ring is dropped, if it outlives the lag, or if a start forgets it.
