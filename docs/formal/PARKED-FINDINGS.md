# Findings from the formal checks

No finding is open for a head that shows MXZ's fan writes in the order it got them, or jumps
to the latest, apart from one narrow case at a start. A head that does anything else isn't
covered. [F1's limits](#f1-limits) has both.
This page keeps what F1 was and how it was fixed, the one accepted exception on cloud heads, and
two open findings on changes made at a head, each waiting on a decision.

## Open: changes made at a head can adopt MXZ's own write

Both break the rule that a report MXZ's own writes can explain is never a person, with Accept
changes made at the head on (the default). Both were found or confirmed by TLC and run on the real
code on invented heads; nothing ran on a real head. Each is a rule that `tools/formal_tlc.py cover`
requires to keep breaking until it is decided, and
[STATE-MACHINES.md](../STATE-MACHINES.md#4-invalid-states-and-transitions-the-code-can-reach) has
the evidence and the options.

- **SM-1, `StartWriteNeverAdopted` in `ci-adopt-restart`.** MXZ's memory of its own head writes is
  in `hass.data`, so a reload keeps it and a process start doesn't. A write from before the start
  that a lagging head shows more than 40 seconds after Home Assistant starts (after startup quiet)
  reads as a press and moves the room's target. Whether a real head can report that late is not
  known. `test_a_restart_loses_the_memory_and_the_start_window_keeps_a_late_write_out` pins the
  quiet window, which holds.
- **SM-10, `QueuedWriteNeverAdopted` in `ci-adopt`.** A write a lagging head hasn't shown yet can
  drop out of the memory: a rewrite of the same target is kept once, a person's press of a
  remembered value catches the memory up past older writes, and `HeadWriteMemory.seen` replaces
  the value it takes for the one shown. When that write lands after a person's later press, it is
  adopted over the press.

## Accepted: a re-pick of the same speed on a cloud head

On a cloud head, if a person picks the same fan speed MXZ just wrote, MXZ can't tell. It
reads the speed as its own write, doesn't hold it, and fan boost's next step writes over it.

A cloud head's report only says which speed it has, not who set it, and here the speed is
the one MXZ expects. No change of behavior could separate the two, so this is accepted and
documented, not a bug. To keep that speed, turn the room's **Fan auto** off, which holds
whatever is showing. `test_a_cloud_re_pick_of_the_speed_just_written_is_overwritten` in
`tests/test_cloud_heads.py` pins it.

The speed before the one MXZ just wrote goes the same way, for the same reason, since a head
that dropped a write keeps showing it: `test_the_exception_on_a_cloud_head` pins that. Both are
in the [exceptions table](README.md#documented-exceptions), with the other documented cases.
A speed MXZ didn't just write is held.

## F1-fan-two-writes-behind: a slow head could read as a person's pick (fixed)

**What happened.** No person touched anything, yet a room's fan went on hold. Boost stopped
driving it, and Fan auto read off until someone handed it back, across restarts too.

**When.** A local head reported its fan late, and MXZ sent it a second, different speed
before it had reported the first. `_observe_fan_latch` allowed only for the two speeds in
`_fan_cmd` and `_fan_prev`, so a head still showing the speed before those read as a
departure.

**How likely.** Not measured: nothing here ran on hardware. It took two different ladder
rungs in two computes, inside one head's report lag.

A report two or more writes behind could also equal the last write. MXZ writes high, medium,
low and high inside one lag, and a head applying them one at a time shows the first high while
the rest are still coming. The first version of this fix read that as caught up, forgot
medium, and held medium when it was reported. An independent review reproduced that.

**The fix.** The coordinator keeps its writes per head, `_fan_older`, in the order written, a
speed written twice appearing twice. A report that matches one is MXZ's own write, not a pick.
A report drops the writes before it, since a head can't show those again, and a repeated speed
counts as its earliest write left. When the last write is all that's left, the head has caught
up, and that write stays as the memory (`_fan_caught_up`), so the next write can't bring back a
speed the head has passed. A hold or a hand-back empties the memory. A report of `_fan_prev` drops
nothing, which is wider than it has to be, and lasts only while the head is one write behind. The
memory rides a reload or restart in the Fan auto switch's restore data, limited to speeds boost
could write.

An independent review found the first version of this rule emptied the memory on that report, and
the next write put back the speed before the last. A head that reported every write then read that
speed as an echo for as long as the next write was in flight. A pick of it was written over, where
the code before F1 held it.

**What it costs.** While a head is behind, a person's pick of one of those older writes reads
as an echo, and boost writes over it. A pick of any other speed holds at once, including
across a start. This widens the first row of the
[exceptions table](README.md#documented-exceptions). A head that has reported MXZ's last write
reads only the last two speeds as echoes, with the next write in flight too, as it did before F1.
`test_a_prompt_heads_passed_speed_holds_during_the_next_writes_window` and
`test_the_exception_is_still_the_last_two_with_a_write_in_flight` hold that.

The first write after a start is the exception. A start can't tell a head that has passed the speed
before the last write from one still showing it, so that write reads the speed as an echo until the
head reports it. A pick of the speed in that window is written over, and the next one holds.
`test_known_cost_the_first_write_after_a_start_can_read_one_speed_more` pins this.

It can outlast the lag once. If a head jumps straight to the last write, and MXZ wrote that speed
earlier in the same lag too, MXZ can't tell that from the head showing the earlier write. The
speeds between stay echoes with nothing left to report. A pick of one is written over, and the
head's report of that ends the memory, so the next pick holds. A write of a speed
not in the memory ends it too, once reported.
`test_a_head_that_only_reports_the_repeat_costs_one_pick_at_most` pins this.

<a id="f1-limits"></a>**Its limits.** Each of these still reads as a pick and holds, with nobody
touching the fan:

- A head that shows a write after one that came later, once MXZ has dropped it. Nothing here
  says whether a real head ever does this: nothing ran on hardware.
- A lag of more than `FAN_OLDER_WRITES` writes, 20 today. The oldest goes first.
- A reload or restart at one moment in a queue. The head shows the first of two writes of a
  speed, say the first low of low, medium, low. That looks just like a prompt head that went
  back to low, whose stored record can't change, so the first low's place is lost. If two more
  writes follow before the queue is done, a later report holds. To release it, turn the room's
  **Fan auto** back on. `test_known_limit_a_start_on_the_first_of_two_writes_can_still_hold`
  pins this, and the in-order property leaves this one start out.

**Evidence.**

| Step | Head shows | MXZ wrote | `_fan_cmd`, `_fan_prev`, older writes | Held |
| --- | --- | --- | --- | --- |
| Room at 76, the head reports | high | high | high, auto, none | no |
| Room at 72.2 | high | medium | medium, high, none | no |
| Room at 70.7 | high | low | low, medium, high | no (it was yes) |
| The head reports | low | | low, medium, none | no |
| Room back at 76 | low | high | high, low, none | no |

And with every write shown in order, from a head showing auto:

| Step | Head shows | `_fan_cmd`, `_fan_prev`, older writes | Held |
| --- | --- | --- | --- |
| MXZ has written high, medium, low, high | auto | high, low, medium | no |
| The head reports | high | high, low, medium | no (it forgot medium here) |
| The head reports | medium | high, low, medium | no (it was yes) |
| The head reports | low | high, low, medium | no |
| The head reports | high | high, low, none | no |

`test_no_person_no_hold_however_far_a_head_lags`,
`test_no_person_no_hold_when_a_head_jumps_to_its_latest_write`,
`test_no_person_no_hold_when_a_head_reports_every_write_in_order` and
`tests/test_fan_lag_memory.py` hold it on the real code in the suite, on every lane.
`verification/findings/test_f1_fan_two_writes_behind.py` is the original replay, with its
strict xfail lifted. In the model, `NoFalseHoldTwoBehind` is now a rule of
`docs/formal/tla/ci-fan-lag.cfg` that must pass. `tools/formal_tlc.py mutants F7 F8 F9` shows
it and `ManualFanKept` break if the memory is dropped, if it outlives the lag, or if a start forgets it.

The model's head only ever reports the latest write, and its memory is a set emptied by that
report. So the in-order stream and the repeat rule aren't in the model: only the tests above
hold them, and `tools/code_mutants.py` shows those tests break without them (C34 to C41).
The model's next write also puts back the speed before the last, so its echo is wider than the
code's. That doesn't hide a false hold for its head: the code's memory keeps the speed that head
shows, and `test_no_person_no_hold_when_a_head_jumps_to_its_latest_write` holds that on the real code.
