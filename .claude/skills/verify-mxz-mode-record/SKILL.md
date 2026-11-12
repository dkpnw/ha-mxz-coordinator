---
name: verify-mxz-mode-record
description: "Formal checks of how MXZ Coordinator applies a change of the shared mode: the mode is recorded, stamped and stored before any head is sent it, each head's command is its own step that can be refused, and a reload, a restart, a reading or a person's pick can land between them. A TLA+ model checked with TLC (ci-apply), tests/test_head_fault.py on the real integration, and broken copies of both that each check must catch. Use before and after any change to coordinator.py _apply or _write_back_mode, or to delivery.py DeliveryQueue (queue, async_wait, _deliver, _async_issue, retire), and when the model or a replay reports a break."
---

# Verify the mode's record and the heads' commands

One outdoor unit runs one direction, and MXZ sends that direction to every head. A change is
decided in one compute and then sent to each head as a separate task, which can wait for the
head, raise, or be cut short by a reload. The harm this guards: a head runs a direction the
stored record doesn't hold, so a restart or a head's error leaves the unit reversing with no
hold, or the select and the heads disagree.

Paths are from the repository root. `docs/STATE-MACHINES.md` sections 3.3 and 3.5 are the
machine in words, and findings SM-2, SM-4, SM-5 and SM-8 there are its history.
`docs/formal/STATECHART.md` "An apply's commands" (D1 to D4) is its chart. The hold itself
(how long, across restarts, hands-off votes) is `verify-mxz-formal`'s, not this one's.

## What is checked

Model rules (`docs/formal/tla/MXZCoordinator.tla` with `WithSplit`, config `ci-apply`):

- **RecordLeadsHeads.** No head is sent a direction the record doesn't hold, unless a person
  has picked a mode since. Harm: a head's error, an input or a reload leaves the record behind
  the heads, and the next start reverses the unit with no hold (SM-2, SM-4, SM-8, fixed).
- **HeadsFollowMode.** After a decision, every head that took its command runs the shared
  mode or idles. A head that refused is left out: that is the documented exception SM-5.
- The other rules `ci-apply` lists (the hold, restarts, the pause, the fan) hold with the
  apply as steps too.
- Reachability goals, which must break so the rules aren't vacuous: `NeverRefused` (a head
  does refuse) and `NeverCutShort` (a start does land inside an apply).

Code checks (`tests/test_head_fault.py`, in CI on every lane), on the real integration with
invented heads:
`test_a_head_is_sent_a_new_mode_only_once_it_is_the_recorded_mode`,
`test_a_head_that_raises_during_a_mode_change_doesnt_undo_the_change` (a `RuntimeError` and a
`HomeAssistantError`), and its negative,
`test_a_head_that_raises_while_nothing_changes_degrades_only_its_room`.

## Coverage record

Sync commit: `ca78324aca3d5402252a585693ae98193cee24ca` (main, release 3.22.0). The model and
tests were confirmed against the code at that commit. Move the sync commit forward only after
you have re-read the functions below at the new commit, confirmed the model still describes
them, and run both commands under Run.

| Check | Covers (custom_components/mxz_coordinator/) |
| --- | --- |
| Model `Decide` (with `WithSplit`) | `coordinator.py` `_compute`, `_apply` (queues every head, then `_write_back_mode`, then `DeliveryQueue.async_wait`, with nothing awaited before the writeback), `_write_back_mode`, `_stamp_mode_change`, `_record_mode`; `delivery.py` `DeliveryQueue.queue` |
| Model `Deliver`, `Refuse` | `delivery.py` `DeliveryQueue._deliver`, `DeliveryQueue._async_issue` (whatever a head's handler raises is that room's), `DeliveryQueue._finish` |
| Model `Select` during an apply | `select.py` `async_select_option`; `coordinator.py` `async_select_shared_mode` |
| Model `Reload`, `Restart` during an apply | `delivery.py` `DeliveryQueue.retire`, `DeliveryQueue.owned`; the record's load as in `verify-mxz-formal` |
| Code mutants C42, C43 | `_apply` (the writeback after the wait), `DeliveryQueue._async_issue` (a non-Home-Assistant error fails the plan) |

Does a change touch it?

```sh
git diff --stat ca78324aca3d5402252a585693ae98193cee24ca -- \
  custom_components/mxz_coordinator/coordinator.py custom_components/mxz_coordinator/delivery.py \
  custom_components/mxz_coordinator/select.py docs/formal/tla/MXZCoordinator.tla
```

If it lists a file, read the diff against the table. A change inside a listed function needs
the check run, and possibly the model updated.

## Tools

The question is which order a decision, its record, each head's command and an input or a
restart can come in. That suits a model checker, and TLA+ is the default for it. The tests run
the real integration with heads that raise or report what they saw. TLC is `tla2tools.jar`
1.7.4 (sha256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`; the runner
refuses any other jar), on Java 11 or later. The tests run on a lane venv per
`docs/DEVELOPMENT.md`.

## Run

```sh
export TLA2TOOLS=/path/to/tla2tools.jar      # and JAVA if java isn't on PATH
export MXZ_PYTHON=/path/to/lane/bin/python   # a lane venv with hypothesis in it
python3 tools/formal_area.py check apply     # about 1.5 minutes; ends AREA_OK or AREA_FAILED
python3 tools/formal_area.py mutants apply   # under a minute
```

`check` runs TLC on `ci-apply` (it must pass every rule it lists), requires `NeverRefused` and
`NeverCutShort` to break, then runs `tests/test_head_fault.py`. `mutants` breaks a scratch
copy, never the checkout: P1 (the record isn't written before the heads) and P2 (an apply an
input overtook counts as a decision) break the spec; C42 (the heads are sent the mode before it
is recorded) and C43 (a head's unexpected error fails the plan) break the code. Each must be
caught by its named rule or test. `python3 tools/formal_area.py list` prints what each part
runs. Output is kept in the folder printed on the first line (`--out` chooses it).

## Read the results

Read every TLC result, a pass or a break, with the pstack `/statechart` skill, on copies of
`docs/formal/tla/MXZCoordinator.tla` and the config in a scratch folder outside the
repository, with `docs/formal/charts/mode.ts` as the view. That view hard-codes `H = 1`, the
chart's; `ci-apply` has `H = 2`, so set it to match before you read a `ci-apply` result.
`ci-apply` is past `/statechart`'s chart budget, so it gives the bug paths only, in a person's
words ("MXZ changes the mode to heat and records it", "Home Assistant restarts").

A break is a real bug or a modeling error. Tell them apart before reporting:

1. Replay the path on the real code, one step per model action, with `tests/property_rig.py`
   and a head whose `async_set_hvac_mode` raises or records what the select and the record
   showed when it was called, as `tests/test_head_fault.py` does. Assert the rule, so the
   replay fails while the bug is there.
2. Give it a control: the same steps without the trigger. The control must pass.
3. If the replay does not fail, assume the model is wrong until you show it is not. The model
   takes `_compute` and the writeback as one step because nothing is awaited between them in
   `_apply`; if an `await` ever appears there, the model no longer describes the code.

Never weaken a rule, a bound or a test to make a check pass. When the promise changes on
purpose, change the rule, the docs and the tests together, and say so in the PR.

## Findings

None open. SM-2, SM-4 and SM-8 were fixed or closed by recording the mode before any head
runs (`_write_back_mode`). SM-5 is a documented exception: a head that refuses its command
keeps its old mode while the others run the new one, until a later update's retry. Sending
parks before runs would be a change to the delivery queue (`docs/STATE-MACHINES.md` section 7).

## Scope and bounds

- `ci-apply`: two rooms in priority order, readings far enough to call (`FarReadings`), a hold
  of 2 ticks, time 0 to 3, a person's pick of the mode, reloads and restarts, each head's
  command a step that can be refused. 3,743,440 distinct states (TLC 1.7.4, at the sync
  commit).
- One apply at a time. The delivery queue's locks, supersession, follow-ups and a handler
  that never returns are not modeled; `tests/test_command_delivery.py` and
  `tests/test_refresh_race.py` hold them. Cloud batching is not modeled; SM-3 is held by
  `tests/test_cloud_heads.py`.
- A pass covers the model at these bounds and the tests as drawn, not the whole integration.
  Nothing here ran on a real head.

Keep this in step with the code with `/maintain-formal-verification-skill`.
