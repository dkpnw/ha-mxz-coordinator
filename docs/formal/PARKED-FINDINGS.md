# Findings from the formal checks

No finding is open for a head that shows MXZ's fan writes in the order it got them, or jumps
to the latest, apart from one narrow case at a start. A head that does anything else isn't
covered. [F1's limits](#f1-limits) has both.
This page keeps what F1 was and how it was fixed, the one accepted exception on cloud heads,
and two findings on changes made at a head: one fixed, and one fixed in part, with the rest an
accepted known cost apart from one narrow case still open. No finding is open in
`tools/formal_tlc.py` (`FINDINGS` is empty); the open case is in the tests only.

## Changes made at a head that adopted MXZ's own write

These broke the rule that a report MXZ's own writes can explain is never a person, with Accept
changes made at the head on (the default). Each was found or confirmed by TLC and run on the real
code on invented heads; nothing ran on a real head.
[STATE-MACHINES.md](../STATE-MACHINES.md#4-invalid-states-and-transitions-the-code-can-reach) has
the evidence.

- **SM-1, `StartWriteNeverAdopted` in `ci-adopt-restart` (fixed).** MXZ's memory of its own head
  writes was in `hass.data`, so a reload kept it and a process start didn't. A write from before
  the start that a lagging head showed more than 40 seconds after Home Assistant started (after
  startup quiet) read as a press and moved the room's target. The memory is now a section of the
  room state store, so every start keeps it, and the rule holds in `ci-adopt-restart`.
  `test_a_pre_restart_write_shown_late_is_not_adopted` (41 seconds and an hour after the start)
  fails on the code before the fix. Whether a real head can report that late is still not known.
- **SM-10 (fixed for a rewrite; the rest an accepted known cost).** A write a lagging head
  hasn't shown yet could drop out of the memory and be adopted over a person's later press when
  it landed. Path (a), a rewrite kept once, is fixed: each copy is counted, and `QueuedWriteKept`
  holds in `ci-adopt` and `ci-adopt-restart` (A4 and A5 break it).
  Paths (b) and (c) are an accepted known cost (the owner's decision, Nov 17, 2026), like the
  fan's F1 costs. Each needs a person to press, at the remote, exactly the value MXZ has queued at
  a head that shows writes late: the press reads as MXZ's write landing, so the memory catches up
  past older writes or uses the write's copy, and `HeadWriteMemory.seen` then replaces it. When
  that write lands after the person's next press, it is adopted over it. The reports are the
  same as in `test_up_and_back_are_both_adopted` (b) and
  `test_a_press_of_a_write_the_head_has_passed_is_adopted` (c), which require the press to be
  adopted, so no rule that reads only reports can give both answers.
  `test_known_cost_a_press_of_the_queued_value_at_a_lagging_head_reads_as_mxzs` and
  `test_known_cost_a_press_of_an_older_queued_value_reads_as_mxzs` pin them.
  `QueuedWriteNeverAdopted`, the strict rule they break, stays in the spec only as the form
  `QueuedWriteKept` is built from; it is no longer a finding TLC must see break.
  A write of the value the head already shows adds no copy when that value is all the memory
  remembers, since no report can count it off.
  One still queued that lands after a person's press of another value is adopted, as before this
  change, and is part of the same known cost:
  `test_known_cost_a_write_of_the_shown_value_landing_after_a_press_is_adopted` pins it. The
  model writes only values the head doesn't show, so this part is in the tests only.
  That write, landing late, can also match a later queued write of its value, so the memory
  passes the writes queued between them and one is adopted when it lands. This is an accepted
  known cost too (the owner's decision, Nov 17, 2026), part of path (c):
  `test_known_cost_a_late_write_of_the_shown_value_passes_the_writes_queued_after_it` pins it on
  `HeadWriteMemory` alone, and the queue oracle
  `test_no_write_still_queued_is_forgotten_but_by_a_press_of_its_value` always runs the
  example. Whether MXZ's real write pattern produces the shown value, another value and the
  shown value again, all queued, is unknown; a dual-setpoint head where one edge changes is a
  candidate.
  Under random seeds the queue oracle found two more shapes, which fail on the base too. Under
  the overnight release mandate (Nov 17, 2026), shape B is fixed and shape A and the F1 rewrite
  cost are accepted as known costs (STATE-MACHINES.md SM-10 and section 7). Shape B: the head
  shows 1 and MXZ writes 0, 1, 1; `wrote` counted no copy for the last 1 and a person's press
  forgot it while it was still queued. `wrote` now counts no copy only when the shown value is
  all that is remembered (`test_a_rewrite_of_the_shown_value_behind_another_write_is_kept`,
  code mutant C56; the area's code mutants are C44 to C56). Shape A: the late write of the shown
  value is taken as a later queued write of its value and uses up its copy, so a person's next
  press forgets that write while it is still queued
  (`test_known_cost_a_late_write_of_the_shown_value_uses_up_the_later_writes_copy`). From such
  a landing until its queue is empty the queue oracle checks no write. The F1 rewrite cost: a
  rewrite of the shown value with another value of MXZ's queued before it now counts a copy that
  no report counts off when the head showed that value from the write before, so a person's
  later press of that value reads as MXZ's once
  (`test_known_cost_a_rewrite_of_the_shown_value_behind_another_write_takes_a_press_back_once`).
  Shape A and the F1 rewrite cost are shown on `HeadWriteMemory` alone. Whether MXZ's real
  write pattern reaches them is unknown; a dual-setpoint head where one edge changes is a
  candidate.
  A call the head refuses takes back the copy it counted
  (`test_a_refused_write_leaves_no_copy_to_take_a_later_press`, code mutant C55).
  Whether a real CN105 or cloud head queues commands that way is unknown. A house check is
  needed before a stable release.

Both were replayed again on the real code at `ca78324`, on invented heads, each with a control
that passes (SM-1 before its fix): SM-10 on the path the `/statechart` chart draws, where the target ends at 72 over
the person's 70, and SM-1 on its probe in [STATE-MACHINES.md](../STATE-MACHINES.md#sm-1-a-late-report-of-mxzs-own-pre-restart-write-is-adopted-as-a-persons-press) (target 72 then 74 at a
lagging head, a restart, the reports after startup quiet), where it ends at 72 where the person
left 74.
The model's own shortest path to SM-1 does not happen on the real code: it has a person set the
target between a start and MXZ's first write after it.
[STATECHART.md](STATECHART.md#a-pre-restart-write-after-startup-quiet) has both paths.

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
before it had reported the first. The latch (now `FanHold._observe_latch`) allowed only for the
two speeds in `_fan_cmd` and `_fan_prev` (now `FanEcho.last` and `FanEcho.prior`), so a head still showing the speed before those read as a
departure.

**How likely.** Not measured: nothing here ran on hardware. It took two different ladder
rungs in two computes, inside one head's report lag.

A report two or more writes behind could also equal the last write. MXZ writes high, medium,
low and high inside one lag, and a head applying them one at a time shows the first high while
the rest are still coming. The first version of this fix read that as caught up, forgot
medium, and held medium when it was reported. An independent review reproduced that.

**The fix.** The coordinator keeps its writes per head (`_fan_older`, now `FanEcho.older`), in the order written, a
speed written twice appearing twice. A report that matches one is MXZ's own write, not a pick.
A report drops the writes before it, since a head can't show those again, and a repeated speed
counts as its earliest write left. When the last write is all that's left, the head has caught
up, and that write stays as the memory (`_fan_caught_up`, now `FanEcho.caught_up`), so the next write can't bring back a
speed the head has passed. A hold or a hand-back empties the memory. A report of `_fan_prev` drops
nothing, which is wider than it has to be, and lasts only while the head is one write behind. The
memory rides a reload or restart in the room state store (and the Fan auto switch's restore
data), limited to speeds boost could write.

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
