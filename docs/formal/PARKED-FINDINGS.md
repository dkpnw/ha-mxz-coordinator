# Open findings from the formal checks

These are real, and not fixed here: this work changes no product code. Each one has a
reproducer that fails on the code today, and a check that fails the day it's fixed.

## F1-fan-two-writes-behind: a slow head can read as a person's pick

**What happens.** No person touches anything, yet a room's fan goes on hold. Boost stops
driving it, and Fan auto reads off until someone hands it back. It stays that way whatever
the room's temperature does, across restarts too, since a hold is restored as a hold.

**When.** A local head reports its fan late, and MXZ sends it a second, different speed
before it has reported the first. `_observe_fan_latch` allows for the two speeds in
`_fan_cmd` and `_fan_prev`, so a report one write behind is read as an echo. A head still
showing the speed before those two reads as a departure, and `_hold_at` latches at it. When
the head then reports the speed it was really sent, that departs from the hold too, and the
hold moves there.

The fan machinery's tests and docs cover a head one write behind, and restart's conservative
false holds. This case isn't documented anywhere. Cloud heads don't hit it this way: their
fan writes are at least five minutes apart, and a report of an older speed is set aside
inside the confirm wait.

**How likely.** Not measured: nothing here ran on hardware. It takes two different ladder
rungs in two computes, inside one head's report lag. A head that reports at once can't hit
it. One polled every 30 s or so could, if a target change or a sensor jump moves the ladder
twice in that time.

**Reproducer on the real code.** `verification/findings/test_f1_fan_two_writes_behind.py`,
a strict xfail:

```sh
python -m pytest verification/findings -q -p no:cacheprovider
```

Its example, with the target at 70 and the head reporting only where it says:

| Step | Head shows | MXZ wrote | `(_fan_cmd, _fan_prev)` | Held |
| --- | --- | --- | --- | --- |
| Room at 76, the head reports | high | high | high, auto | no |
| Room at 72.2 | high | medium | medium, high | no |
| Room at 70.7 | high | low | **high, high** | **yes** |
| The head reports | low | | low, low | yes |
| Room back at 76 | low | nothing | low, low | yes |

**TLC trace.** `NoFalseHoldTwoBehind` breaks in `docs/formal/tla/ci-fan-lag.cfg`, and
`tools/formal_tlc.py cover` requires that it does. The rule next to it, `NoFalseHold`,
passes: at most one write behind, MXZ never holds a speed nobody touched.

**Why the xfail is not in `tests/`.** `tools/pytest_phases.py`, which every CI suite pass
runs under, fails a run on any xfail, by design. A strict xfail in `tests/` would turn
every lane red, so it sits in `verification/findings/`, where
`.claude/skills/verify-mxz-formal/SKILL.md` already says an open finding's replay goes.

**To decide.** Keep more than two speeds in the echo memory, or age them out with time,
at the cost of reading a real pick of an older speed as an echo for that long; or accept it
and document it.
