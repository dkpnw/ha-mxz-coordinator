# MXZ Coordinator: state machines

| | |
|---|---|
| Written | 2026-11-09 (America/Los_Angeles); updated 2026-11-10 after the second review (section 3.23, and the SM-3 and SM-6 fixes made whole) |
| Base | `f21dffef5b012ace9b31cf51fd277292f3ffa5b7` (`docs: clean-copy strings and correct accept-at-head help (#114)`). Every line number below is at that commit; the fixes in this change move some by a few lines. Code this change added or changed has no line there and is cited by name (`_write_back_mode`, `_disown_waiting_command`, `CloudHead.refused`, `_watch_head_change`'s return). The door/window pause has since moved to `openings.py` (`OpeningPause`), Allow Dry to `dry.py` (`DryMode`), the fan hold to `fan_hold.py` (`FanHold`, `FanEcho`), and the refresh serialization and the per-head delivery slot and command status to `delivery.py` (`DeliveryQueue`, `Delivery`); their line numbers here are from before those moves. The Allow Dry table in section 3.14 cites `DryMode` by name, and section 3.12 names the `FanHold` method beside each old line range, and sections 3.2 to 3.4 name the `DeliveryQueue` members. |
| How | The catalog and findings SM-1 to SM-9 come from a read-only review with scratch probes on invented heads (section 1). The fixes, the repros, the TLA+ extensions and SM-10 come from the change that added this file. Nothing ran on a real head, a real Home Assistant or a cloud account. |
| Companion documents | The formal checks: [formal/README.md](formal/README.md), [formal/STATECHART.md](formal/STATECHART.md), [formal/PARKED-FINDINGS.md](formal/PARKED-FINDINGS.md), [formal/tla/MXZCoordinator.tla](formal/tla/MXZCoordinator.tla), `verification/modehold/MXZModeHold.tla`. IDs such as H1 or M1 are those of the whole-repo audit of the same date. |

This file lists every state machine in the integration, the explicit ones and the ones that
exist only as a few fields and a timer. For each it gives the states, the rules, and the
transitions with their code. It then lists the invalid states and transitions the code can
reach, what became of each, and how the TLA+ spec was extended to check them.

It does not copy the charts in `docs/formal/STATECHART.md`. Where a machine is already charted
there, this file points at the chart's transition IDs (M1–M6, A1–A5, R1–R4, W1–W11, F1–F11,
L1–L4) and adds only what the chart leaves out.

Paths are under `custom_components/mxz_coordinator/` unless they start with `tests/`, `docs/`,
`tools/` or `verification/`. A bare line number means `coordinator.py`.

**Standing rule.** Build cleanly and modularly with real separation of concerns. Never bolt a
feature on. Where the clean fix is the coordinator split (audit chunk 5, H1) or the room-state
store (audit chunk 6, H2), this file says so and recommends no patch in its place. The room-state
store needs the owner's OK.

**Where each finding stands.**

| Finding | Status | Held by |
|---|---|---|
| SM-1 late pre-restart write adopted | Open, needs a decision | `test_a_restart_loses_the_memory_and_the_start_window_keeps_a_late_write_out` (the quiet window); `StartWriteNeverAdopted` breaks in `ci-adopt-restart` |
| SM-2 a head's unexpected error skips the mode record | Fixed | `tests/test_head_fault.py` |
| SM-3 a refused cloud batch waits out the confirm wait | Fixed | `test_a_refused_batch_goes_out_again_at_the_write_interval_with_nothing_else_happening`, `test_a_batch_refused_after_its_compute_has_ended_still_wakes_the_plan`, `test_a_refusal_that_outlasts_the_write_interval_is_sent_again_at_once`, `test_a_refused_batch_is_tried_again_once_the_write_interval_allows`, `test_a_floor_on_a_cloud_head_reads_held_while_its_command_is_refused`, `test_a_refused_batch_leaves_nothing_to_wait_on_and_keeps_the_rate`, `test_a_refusal_slower_than_the_write_interval_releases_the_next_batch_at_once` |
| SM-4 the record depends on a race | Fixed | `test_a_head_is_sent_a_new_mode_only_once_it_is_the_recorded_mode`; `RecordLeadsHeads` in `ci-apply` |
| SM-5 a refusing head stays in the old mode | Documented exception | [formal/README.md](formal/README.md#documented-exceptions); `NeverRefused` reaches it |
| SM-6 a press while MXZ's command waits is written over | Fixed | `test_a_press_while_mxzs_command_waits_for_the_head_is_adopted`, `test_a_flip_alone_while_mxzs_command_waits_is_put_back_by_it`, `test_a_press_taken_back_in_its_hold_leaves_the_room_target_to_reach_the_head` |
| SM-7 a start releases restored season lockouts | Open, needs a decision | Confirmed by a run; no test landed |
| SM-8 a reload inside an apply loses the hold | Closed by the SM-4 fix | `RecordLeadsHeads` in `ci-apply`, with reloads and restarts inside an apply (`NeverCutShort` reaches them) |
| SM-9 restore data up to 15 minutes old after a crash | Open, room-state store | — |
| SM-10 a queued write that dropped out of the memory is adopted | Open, needs a decision | `QueuedWriteNeverAdopted` breaks in `ci-adopt` |

---

## 1. Method and evidence boundary

| Scope | Depth |
|---|---|
| `coordinator.py` 930–7840 (the class and its helpers) | Line by line |
| `coordinator.py` 1–930 (readers, `Zone`, stores, unit conversion) | Line by line |
| `logic.py` 420–1299 (arbitration, door/window, `HeadWriteMemory`, `HeadSight`, floor, setpoints, fan ladder) | Line by line |
| `logic.py` 186–420 (engage latch, freshness, seasons) | Outline and return paths only |
| `logic.py` 1–186 (`room_call`, `call_edges`) | Not read |
| `cloud.py` 18–190, `__init__.py`, `select.py` 40–133 | Line by line |
| `switch.py` 60–505, `number.py` 50–471, `climate.py` 259–520, `entity.py` 20–76 | Line by line |
| `config_flow.py` | Sampled: the step graph by `grep`, and the three save points (2287–2378, 2731–2817, 3226–3301) |
| `diagnostics.py`, `issues.py`, `button.py`, `sensor.py`, `binary_sensor.py`, `capabilities.py`, `const.py` | Swept by `grep` for state and timers; constants read where cited |
| `docs/formal/*`, both TLA+ specs, `tools/formal_tlc.py` tables | Read (specs line by line; the runner's tables sampled) |
| `.claude/skills/verify-mxz-formal/SKILL.md` | Headings and the sync-commit lines only |
| Tests | Read only as evidence: `tests/property_rig.py`, `tests/cloud_heads.py`, and the parts of `tests/test_accept_head_changes.py`, `tests/test_changeover.py`, `tests/test_logic.py` cited below |
| The accept-at-head repro hand-off (#114) | Its notes on a restart with a write in flight, and its scratch restart test |

**Labels on findings.**

- `confirmed (run)`: the cited lines were read and a probe on the real code showed the behavior.
- `confirmed (read)`: the cited lines were read and the path was walked step by step; nothing was run.
- `inferred`: follows from read lines plus reasoning or Home Assistant behavior that was not run.

**The probes.** The review's probes were scratch tests on a throwaway copy of the base, run on
one Python 3.14 / Home Assistant 2026.9 test lane, on invented heads only. Where a probe became a
landed test, section 4 names it; the rest are described closely enough to rebuild. TLC was run
afterwards, for section 6.

**Unknowns, recorded as unknowns.**

- Any behavior on real hardware. Nothing here ran on a head, an ESPHome device or a cloud account.
- Whether a real head can report a write MXZ made before a restart more than 40 seconds after Home Assistant starts (finding SM-1).
- Which real head integrations raise an error that is not a `HomeAssistantError` (finding SM-2).
- Whether the refresh follow-up logic (section 3.2) has a lost wakeup. None was found by reading. It is not modeled and was not tested here.

---

## 2. The machines at a glance

"Lives in" says what a start does to the state: `memory` is lost at any reload; `hass.data` is
kept across a reload and lost when the Home Assistant process ends; `restore data` is written at
a clean stop and every 15 minutes; `Store` is written at once.

| § | Machine | Owner | Lives in | Modeled |
|---|---|---|---|---|
| 3.1 | Coordinator lifecycle, Coordinator switch, startup quiet | `MXZCoordinator`, `__init__.py`, `switch.py` `MXZSwitch` | memory; the switch in restore data | Chart L1–L4; both specs (`Reload`, `Restart`, `Crash`). Startup quiet as adoption's gate and a retire inside an apply: `MXZCoordinator.tla` (`ci-adopt-restart`, `ci-apply`). The switch: nowhere |
| 3.2 | Refresh serialization | `DeliveryQueue` | memory | Inputs landing during one apply: `MXZCoordinator.tla` (`ci-apply`). The lock, the debouncer and the follow-up: nowhere |
| 3.3 | Per-head delivery slot | `DeliveryQueue` | `hass.data` | `MXZCoordinator.tla` `Deliver`, `Refuse` (`ci-apply`); the queue's locks and supersession in tests only |
| 3.4 | Command status | `DeliveryQueue` | `hass.data` | Nowhere |
| 3.5 | Shared mode and its hold | `MXZCoordinator`, `logic.arbitrate`, `select.py` | `Store`, with the select's restore data second | Chart M1–M6; both specs. The writeback's place in the apply: `MXZCoordinator.tla` `WithSplit` (`ci-apply`, `RecordLeadsHeads`) |
| 3.6 | A room's run, startup resume, coast wakeup | `MXZCoordinator`, `logic.engage_with_latch` | memory, re-read from the head | Chart R1–R4; both specs |
| 3.7 | Room sensor freshness | `MXZCoordinator`, `logic.sensor_health` | memory | Tests only |
| 3.8 | Head sight, lost vote, action vote | `logic.HeadSight` | memory | The vote as a mode in `MXZModeHold.tla`; the rest tests only |
| 3.9 | Hands-off room | `MXZCoordinator` | memory, with `released` in restore data | `MXZModeHold.tla` has it always released; phases tests only |
| 3.10 | Heal timers (band drift, off drift) | `MXZCoordinator` | memory | Tests only |
| 3.11 | Changes made at a head (adopt) and the head write ledger | `MXZCoordinator`, `logic.HeadWriteMemory` | hold in memory; ledger in `hass.data` | Chart A1–A5; `MXZCoordinator.tla` for room 1's setpoint (`ci-adopt`, `ci-adopt-restart`) |
| 3.12 | A head's fan hold and the fan echo ledger | `fan_hold.FanHold`, `switch.py` | memory, carried in restore data | Chart F1–F11; `MXZCoordinator.tla` |
| 3.13 | Door/window pause | `MXZCoordinator`, `logic.opening_state` | `Store` | Chart W1–W11; both specs |
| 3.14 | Coil-dry idle, and Allow Dry | `MXZCoordinator` (coil-dry), `dry.DryMode` (Allow Dry) | memory | Tests only |
| 3.15 | Freeze floor and overheat ceiling | `MXZCoordinator`, `logic.FloorRun` | memory, with the run in restore data | Arbitration tier in tests; the run nowhere |
| 3.16 | Cloud head batching | `cloud.CloudHead`, `MXZCoordinator` | memory | Nowhere |
| 3.17 | Seasons (lockouts) | `MXZCoordinator`, `logic.season_lockouts`, lockout switches | restore data | Nowhere |
| 3.18 | Standby hold and eco | `MXZCoordinator` | memory, re-read from the entity | Nowhere |
| 3.19 | Vane kick | `MXZCoordinator` | memory, with the cleanup flag in `hass.data` | Nowhere |
| 3.20 | Dual setpoint | `MXZCoordinator`, `number.py`, options flow | restore data, with a one-shot `hass.data` hand-off | Tests only |
| 3.21 | Persistence: what each start brings back | all of the above | — | `RestartRestores` covers the mode, hold, door/window; `ci-fan-crash` covers the fan |
| 3.22 | Setup, reconfigure and options flows | `config_flow.py` | the flow object | Tests only |
| 3.23 | Comfort priority | `MXZCoordinator`, `logic.comfort_priority`, `logic.comfort_stale_in` | nothing stored: read live from the entity, its age from Home Assistant's `last_changed` | `MXZModeHold.tla` leaves it out (its header); the arbitration in `tests/test_modehold_properties.py`, the rest tests only |

---

## 3. Catalog

Transition tables read: event, from → to, guard, action, code.

### 3.1 Coordinator lifecycle, Coordinator switch, startup quiet

**States.** Built (the seed compute has run and decided nothing) → Live with the Coordinator
switch off (computes, writes nothing) ⇄ Live with it on → Retired (never writes again). One
sub-state: startup quiet, from a build made before Home Assistant is running until 40 seconds
after it starts.

**Rules.**
- A retired coordinator writes no head, no record and no door/window store: `_apply` 2858, `_delivery_owned` 3102–3106, `_record_mode` 2725–2726, `_save_openings` 6226–6227, `async_head_service` 3232–3233.
- The Coordinator switch starts off, so the first refresh commands nothing: 1252–1255, `__init__.py:151-154`.
- The seed compute decides nothing for floors or door/window pauses: 1188–1190, 1209, 6619–6622, 6025.

| Event | From → to | Guard | Action | Code |
|---|---|---|---|---|
| Entry set up | none → Built | — | Load the mode record and the door/window store, build, seed compute | `__init__.py:141-145`; 1389 |
| Platforms added | Built | — | Each entity restores its value into the coordinator | `__init__.py:154`; `switch.py:170-186`, `number.py:107-150`, `select.py:71-96` |
| `async_setup` | Built → Live | — | Floors and openings go live, listeners attach, first refresh | 1472–1475, 1594 |
| Home Assistant starts, then 40 s | quiet → normal | built before start | One refresh; the "no usable reading" warning and blind-floor notice may now go out | 1339, 5040–5050; `const.py:657` |
| Coordinator switch | off ⇄ on | — | Off: retire deliveries and vane kicks. A live change clears the startup resume | `switch.py:212-220`; 7418–7420 |
| Temperature unit changed | Live → Retired | unit differs | Retire, schedule a reload | 1681–1697 |
| Unload or reload | Live → Retired | — | Retire first, then cancel every timer, then retire vane kicks | 1699–1749; `__init__.py:166-168` |
| Options saved | Live → Retired → Built | not a dual-only save | Reload; a dual-only save is applied in place | `__init__.py:183-196` |
| Entry removed | — | — | Both stores deleted; the per-head `hass.data` records are not (audit L3) | `__init__.py:176-180` |

Startup quiet has three readers: the sensor warning (2356), the blind-floor notice (6898) and
head adoption (5405). It is not a general "ignore the heads" switch.

**Modeled.** Chart L1–L4. `Reload`, `Restart` and `Crash` in both specs. Startup quiet is in
`MXZCoordinator.tla` as adoption's gate only: a process start sets `quiet` and `QuietEnds` ends it
(`ci-adopt-restart`, section 6.2). Its other two readers are not modeled. A retire that lands
inside an apply is `Reload` or `Restart` while `phase = "applying"` (`ci-apply`, section 6.1);
`NeverCutShort` shows it is reached. Not modeled: the Coordinator switch (every room is enabled
with the coordinator on). At the base, `MXZModeHold.tla` lines 10–18 named the retire inside an
apply as unmodeled; that header now says the record comes before the deliveries.

### 3.2 Refresh serialization

**State.** `_plan_lock` (held while one plan is computed and applied), `_refresh_generation`,
`_applying_plan`, `_refresh_pending`, `_delivery_followup`, `_refresh_wake`,
`_refresh_followup_timer`, and Home Assistant's own debouncer. `_needs_legacy_refresh_guard`
(1356–1360) selects one of two code paths by reading a private attribute of the debouncer (audit
M2).

Since the split, all of it but `_applying_plan` is `DeliveryQueue`'s, in `delivery.py`: `_lock`,
`generation`, `_pending`, `_followup`, `_wake`, `_followup_timer` and `legacy`. A refresh runs
inside `DeliveryQueue.deciding()`; requests go through `async_request`, `async_request_ordinary`
and, for a head's report, `request_for_report`; the follow-up is `_sync_followup`. The coordinator
now makes the request debouncer itself, through the public `request_refresh_debouncer` argument,
and `legacy` is chosen by the public `Debouncer.async_lock`, which arrived in Home Assistant 2025.11
together with the two debouncer fixes the legacy path makes up for. The one private read left,
the debouncer's `_execute_lock`, runs only before 2025.11 (`delivery._legacy_busy`).

**States.** Idle; Debouncing (a request waits out the cooldown); Deciding (lock held, compute);
Applying (lock held, waiting on deliveries); Applying with a refresh pending; Follow-up queued;
Retired.

**Rules.**
- One compute and apply at a time: 2489.
- A new input never waits for a head: it releases the apply's wait (2463–2464, 3090–3098).
- At most one follow-up is queued, and it captures nothing: 2544–2580.
- A retired coordinator ignores every request: 2452, 2461, 2481.

| Event | From → to | Guard | Action | Code |
|---|---|---|---|---|
| Input (sensor, timer, switch, service) | Idle → Debouncing | no delivery outstanding | Home Assistant's debounced request | 2450–2473 |
| Input | Applying → Applying, pending | lock held, or a delivery outstanding | Set the wake event and `_refresh_pending` | 2454–2472 |
| Sensor change whose plan already ran | — | generation moved since the event | Dropped: that plan read this reading | 4975–4983 |
| Refresh starts | → Deciding | lock acquired | Generation + 1, clear pending flags and the wake event | 2487–2497 |
| Compute done | Deciding → Applying | — | Publish the plan as data, then `_apply` | 2499–2516 |
| Apply done, or its wait released | Applying → Idle or Follow-up queued | — | Re-sync every wakeup; queue the follow-up if one is pending | 2525–2536 |
| Follow-up fires | Follow-up queued → Deciding | same generation, not retired | Refresh at once if an intent is waiting, else an ordinary request | 2551–2563 |
| Head change that must count now (action vote, door/window delay, settle) | any → Deciding next | — | `async_refresh_now`: wake the plan, refresh without the cooldown | 2475–2485, 5259–5260, 5856, 6285 |
| MXZ's own delivery step raises | Applying → failed | — | The refresh fails; the plan sensor and room thermostats read unavailable. At the base a head's own unexpected error did this too (SM-2, fixed) | 3099–3100, 3134–3143 |

**Modeled.** The lock, the debouncer and the follow-up: nowhere. `ci-apply` takes one apply's
commands as steps with readings, picks, reloads and restarts landing between them, and an apply
an input overtook does not count as a decision (section 6.1); it has one apply at a time and no
follow-up. At the base `docs/formal/README.md:107-108` said nothing between `_compute` and the end
of `_apply` was modeled. `tests/test_refresh_race.py` covers the rest by patching `_apply` and
reading private fields (audit H3).

### 3.3 Per-head delivery slot

**State.** One record per head in `hass.data["mxz_coordinator_deliveries"]`, one lock per head in
`…_head_locks`, and a cleanup event per head in `…_vane_cleanup_pending` (1368–1376). They outlive
a reload on purpose, so a new coordinator waits behind an old handler. Since the split the record
is a `Delivery` and the code is `DeliveryQueue`'s: `queue` (was `_queue_delivery`), `_deliver`
(`_deliver_head`), `owned` (`_delivery_owned`), `_finish` (`_finish_delivery`), `retire`
(`_retire_deliveries`), `async_send` (`async_head_service`, whose `vane` flag is now the vane
kick's `guard`), `disown` (`_disown_waiting_command`). The per-head steps stay in the coordinator
as `async_deliver_steps` (`_deliver_head_steps`).

**States per head.** Empty; Queued (task made, lock not yet held); Issuing (lock held); Superseded
(a newer intent is recorded as `next`); Superseded by a person's gesture (`manual`); Retired.

**Rules.**
- One real operation per head at a time, across coordinator incarnations: 3117–3127, 3230.
- A sequence that is superseded, retired or overtaken by a gesture issues nothing more. It is checked between steps: 3102–3106, 3163, 3184, 3188, 4044.
- A head keeps at most one follow-up, built from current inputs, never a queue of old plans: 3063–3072, 3153–3157.

| Event | From → to | Guard | Action | Code |
|---|---|---|---|---|
| The plan wants an action for a head | Empty → Queued | — | Create the task | 3073–3084 |
| The plan wants an action | occupied → Superseded | owner, retirement, gesture or intent differs | Record `next`; the plan does not wait for it | 3063–3072 |
| Task runs | Queued → Issuing | lock free, no vane cleanup pending | Run the per-head steps | 3124–3127 |
| Task runs | Queued (waits) | lock held or cleanup pending | Release the plan's wait | 3118–3123 |
| The head raises `HomeAssistantError` | Issuing → done | — | Log; this room only is degraded; a floor is marked rejecting | 3128–3133 |
| MXZ's own step raises | Issuing → done | — | The whole plan fails. A head's handler raising anything else now takes the row above (SM-2, fixed) | 3134–3143 |
| Task done | → Empty | — | The recorded successor refreshes | 3145–3157 |
| A person's gesture on the thermostat (fan, Dry) | occupied → gesture | — | Mark `manual`; the gesture waits for the lock | 3220–3239 |
| Retire, or Coordinator switch off | owned → Retired | — | Mark retired and superseded | 3195–3213, 7418–7420 |
| A person changes a head during a floor run | Queued → Superseded | — | Disown the queued command | 7083–7089, now `DeliveryQueue.disown` |
| A person's change at the head arms the adopt hold | Queued → Superseded | the change will be adopted (3.11) | Disown the queued command; one already sent goes ahead (SM-6, fixed) | `DeliveryQueue.disown` (was `_disown_waiting_command`), called where the hold is armed (5481–5482) |
| A head report ends that hold with nothing adopted (the head put back by hand) | Superseded → re-owned, or Empty → Queued | — | Request a compute. A disowned command still queued is owned again if the plan still wants it, or gets the plan's newer one as `next`; one already skipped is replaced by a new one | `_watch_head_change` returns true and `_on_head_change` refreshes, as the floor-yield row does (5161–5162) |

A head's handler has no timeout. A handler that never returns holds its head's lock for the life
of the process, reloads included, and the room's row says so (2156–2174). That is a documented
limit, not a finding.

**Modeled.** `MXZCoordinator.tla` `Deliver` and `Refuse` (`ci-apply`) take each head's command
as a step of its own. The locks, supersession, follow-ups and both disowns are in tests only:
`tests/test_command_delivery.py`, `tests/test_delivery_visibility.py`, and for SM-6
`tests/test_accept_head_changes.py`. The disown and its re-plan are tests only too: before the
second review the disown had no re-plan when the hold ended with nothing adopted, and the head kept
the old setting until an unrelated compute (section 4, SM-6).

### 3.4 Command status

**States per head.** not yet recorded → pending → returned | rejected | cancelled, outcome
unknown. Flags beside it: ownership retired, deferred, and a separate status for the off that a
retired vane kick's cleanup sends.

**Code.** `DeliveryQueue._async_issue` (was `_call_head_service`) and `DeliveryQueue.attributes`
(was `command_attributes`); a retired vane kick's cleanup goes through `async_send_cleanup`.

**Rules.** It is a software observation and says so (3294–3295). Every change publishes at once
(3260, 3277, 3348–3363).

| Event | From → to | Code |
|---|---|---|
| A service call is about to go out | → pending | 3252–3260 |
| It returns | pending → returned | 3275 |
| It raises | pending → rejected, with the error | 3268–3273 |
| It is cancelled | pending → cancelled, outcome unknown | 3265–3267 |
| The owner retires while pending | pending, ownership retired | 3201–3211, 4898–4902 |

**Modeled.** Nowhere. One mismatch with the cloud record is in SM-3.

### 3.5 Shared mode and its hold

Chart M1–M6, `MXZCoordinator.tla` (`Decide`, `Select`, `Start`) and `MXZModeHold.tla` (votes,
wall-clock steps). Not repeated here. What the chart leaves out is where the mode is written
inside the apply; `MXZCoordinator.tla` checks that with each head's command as a step of its own
(`WithSplit`, section 6.1).

In order, as `_apply` now runs. Nothing is awaited from the compute to the end of the writeback.

| Event | Guard | Action | Code |
|---|---|---|---|
| The apply returns early | retired, Coordinator off, a fixed standby park, or no plan | No head write, no writeback | 2858–2880 |
| The deliveries are queued | — | Each head's command is a task that has not run | 2883–3020 |
| Writeback, mode changed | — | Set the mode, stamp the hold unless votes alone moved it, re-render, write the record | 3037–3045, now `_write_back_mode` |
| Writeback, a room runs in a mode votes chose | — | Stamp the hold, write the record | 3046–3051, now `_write_back_mode` |
| The deliveries are awaited | — | The plan waits for every head or for a new input | 3021, 3086–3100 |
| A person picks a mode | — | Set the mode, stamp, write the record, request a refresh. A pick during the wait comes after the apply's record, and nothing in the apply writes the mode again | 7443–7448 |
| The record is written | not retired | `Store.async_delay_save(…, 0)` | 2715–2734 |
| A start | — | The record gives the mode, the hold's remainder and whether votes set it; the select's restore data wins only if newer | 1275–1287, 2736–2764 |

At the base the writeback came after the heads were commanded and could be skipped after they
were: SM-2, SM-4 and SM-8 are the three ways. It now runs with the commands queued and none run
(`_write_back_mode`), so none of the three can. At the base a count of picks (`_selection_seq`:
1264, 2505, 3035–3036, 7440–7442) made a writeback after the wait skip itself over a newer pick.
With no wait before the writeback it had nothing to guard, and it is gone.

Two wakeups serve the hold. One re-renders the entities when it ends (2766–2784). One asks for a
refresh when a change the hold deferred becomes allowed (2589–2667). Neither captures anything.

### 3.6 A room's run, startup resume, coast wakeup

Chart R1–R4 and `Engage` in both specs. Additions:

- **Startup resume** is three-valued per room (1111–1114): unknown head, known idle, or a run seen at the start. An enable switch that restores on puts its room in it (`switch.py:297-298`). The first compute with a usable sensor and a visible head takes the head's mode as the latch (1861–1881). A head reported off before that cancels the remembered run (5109–5116). A live Coordinator off, or a room turned off or on, clears it (`switch.py:218-220`, `299-301`).
- **A target write** forgets the latch, so the next compute re-reads the head (7388–7404). With changes at the head accepted, a head a person put in the other mode is not taken as a run (1882–1893).
- **Coast wakeup**: a compute that lets a run go arms one wakeup 11 seconds later so a head with no fan echo is looked at again (622–629, 2792–2845).

### 3.7 Room sensor freshness

**States per room.** cadence unknown (nothing enforced); awaiting report (one startup grace);
healthy; stale; invalid.

**Rules.** Nothing about sensor health survives a reload (1303–1314). One grace per coordinator
(2228–2229). Evidence is taken once, at the write that carried it (2250–2302). An unhealthy
evaluation spends the evidence on record (2240–2247).

| Event | From → to | Code |
|---|---|---|
| Build, valid reading, enforced contract | → awaiting report | 1461–1469 |
| Build, a sample time inside the maximum age | → healthy | 2289–2302; `logic.py:316-351` |
| Build or any time, no usable reading | → invalid | `logic.py:363-398` |
| A witnessed qualifying report | awaiting, stale or invalid → healthy | 4967–5012, 2298–2302 |
| The grace ends with no report | awaiting → stale | 2236–2237, 2410–2443 |
| The maximum age passes | healthy → stale | 2234–2235, 2410–2443 |

A stale or invalid room has no call and no run (1826), its head parks, and its floor goes blind
(6633–6652).

**Modeled.** Tests only. No invalid state was found.

### 3.8 Head sight, lost vote, action vote

**States per head.** unknown (not seen since this start) → live ⇄ stale (out of sight, inside the
limit) → expired. For a room that votes by action: no run → unconfirmed (15 s after a mode
change) → running → held (600 s after it stops) → no run.

| Event | From → to | Code |
|---|---|---|
| A compute or report sees the head | → live | 5714–5730; `logic.py:890-928` |
| The head goes unavailable or disappears | live → stale | 5066–5074; `logic.py:930-934` |
| The limit passes (30 min default) | stale or unknown → expired | `logic.py:936-942`; wakeup 5776–5818 |
| Running reported inside the settle window | no run → unconfirmed | `logic.py:921-926`; wakeup 5825–5858 |
| Still running at the window's end | unconfirmed → running | `logic.py:921-924` |
| Stops in the same mode | running → held | `logic.py:927-928` |
| Leaves the mode, or the hold ends | → no run | `logic.py:925-926`, `956-971` |

An expired vote drops its room's own call from arbitration unless its Safety priority sensor is
on (5737–5750).

**Modeled.** `MXZModeHold.tla` has a hands-off vote as a mode a person sets. Stale, expired and
the action filter are held by `tests/test_modehold_properties.py` and `tests/test_hands_off.py`.

### 3.9 Hands-off room

**States** (`_hands_off_phase`, 5618–5625). Not hands-off (not opted in, or enabled); parking;
released; floor.

| Event | From → to | Guard | Action | Code |
|---|---|---|---|---|
| The room is disabled | not hands-off → parking | opted in | Off is sent each apply until it lands | 2936–2943 |
| The head is seen off | parking → released | nothing of MXZ's but an off in flight | From here the head is left alone | 2937–2942, 5122–5142 |
| The room is enabled | any → not hands-off | — | Forget the release | `switch.py:293-296` |
| Its floor trips | parking or released → floor | — | MXZ runs the head; a person's own run in that direction is remembered | 6719–6724, 6770–6784 |
| The floor run ends | floor → parking, or → released by hand-back | — | See 3.15 | 6754–6768, 6877–6885 |
| A person changes the head's mode in a floor run | floor → released | a real change, not MXZ's | MXZ yields for 4 hours or until the room clears | 5143–5162, 7071–7093 |
| A start | → released | restore data says released | Restored by the enable switch | `switch.py:277-278`; 5989–5991 |

A standby hold parks a released head too (2903–2909, 3484–3500), which the docs state.

**Modeled.** `MXZModeHold.tla` treats the room as always released. The phases are in tests only.

### 3.10 Heal timers

**State.** `_heal_timers[(head, kind)]` for `band`, `off` and `adopt` (1345). Adoption shares the
dictionary and is a special case in the timer callback (5293–5295, audit M5).

**States per head and kind.** idle; armed (20 s for a banned mode, 30 s for off while enabled).

| Event | From → to | Guard | Action | Code |
|---|---|---|---|---|
| A head report | idle or armed → armed | `band`: the mode is heat_cool, auto or dry, the room is not a released hands-off room, the report is not a stale cloud poll, Dry is not allowed there | Replace the timer | 5184–5192, 5275–5286 |
| A head report | → armed | `off`: off, room enabled, not eco, the plan wants it on, no standby hold, not a stale cloud poll | Replace the timer | 5226–5232, 5309–5316 |
| A head report | armed → idle | the condition no longer holds | Cancel | 5281–5282 |
| The timer fires | armed → idle | still true, Coordinator on, no standby hold | Log, request a refresh, send at most one notice per head per hour | 5288–5307, 5570–5596 |
| An off is adopted as "disable the room" | `off` armed → idle | — | Cancel the off heal | 5555–5563 |

The hourly notice limit is in memory, so a reload restarts it (audit L4).

**Modeled.** Tests only.

### 3.11 Changes made at a head, and the head write ledger

Chart A1–A5. `MXZCoordinator.tla` checks room 1's setpoint with the ledger, the hold and startup
quiet (`WithAdopt`, `ci-adopt`, `ci-adopt-restart`, section 6.2). The enable, the heat/cool choice
and every other gate are in tests only. Additions:

- **The ledger.** One `HeadWriteMemory` per head and per field (mode, and each setpoint field), in `hass.data["mxz_coordinator_head_writes"]`, keyed by unit (1380–1385; `logic.py:787-847`). A write is remembered before it goes out, with what the head showed then (3251–3252, 5336–5345). A report of a remembered value drops the older ones (5062, 5347–5352). A report of anything else is a person's.
- **Gates.** No adoption while the option is off, the coordinator is retired or off, a standby hold or eco is on, startup quiet is on, the head is unavailable, the room is a disabled hands-off room, its floor is tripped, it is paused, or the report is a stale cloud poll (5399–5415).
- **The hold.** Five seconds (`const.py:656`). Each further move starts it over and adds to what moved (5471–5482). While it holds and still counts, the apply skips that head (2888–2889, 5484–5491). Arming it also disowns a command for that head still waiting to be sent (`_disown_waiting_command`, SM-6), so whatever ends the hold owes the head a compute.
- **What ends a hold with nothing adopted.** The head going back, any gate closing (5478–5480), a change to the room in Home Assistant (7395–7397, `switch.py:302-303`), any start. Each is followed by a compute that puts the head back on the plan: the first two because the head report that ends the hold asks for one (`_watch_head_change` returns true, SM-6), the room change through its own refresh, and a start by its first compute. At the base the first two asked for none, which was harmless only while nothing was disowned.
- **Adoption.** Through the room's own number and switch (5521–5568). A cloud head with a drive offset never gives a target, only an enable (5441–5444). A refused write adopts nothing (5564–5565).

**What a start does.** The hold and the set of what moved are in memory and are lost at any
start; `test_a_start_inside_the_hold_drops_the_change` pins that. The ledger survives a reload;
`test_the_memory_rides_a_reload_with_the_head_still_behind` pins that. The ledger does not
survive a process restart. The only guard then is startup quiet, pinned by
`test_while_home_assistant_is_starting_nothing_is_adopted`, whose docstring reads "A restart
loses the memory: the first reports have nothing to be read against." SM-1 is what happens after
that window.

### 3.12 A head's fan hold and the fan echo ledger

Chart F1–F11 and `MXZCoordinator.tla` (`Observe`, `Reconcile`, `Rolled`, `Start`). Not charted:

- **Fan auto on with no speed showing** is an instruction kept until the first usable report, which is then adopted (1092–1094, 4287–4291, 4646–4653; now `FanHold._observe_latch` and `FanHold.async_set_auto`). Turning it off again before that report makes the first speed a hold (4659–4669). It rides a restart (`switch.py:67-73`, `436-445`).
- **A standby release** moves each head's hold into the restore slot and clears the memory, so the fan the hold left behind is reconciled as after a start (7568–7589; now `FanHold.reseed_after_standby`).
- **A paused head and a dry head** are read for a pick and never written (6174–6194, 6526–6549; now one method, `FanHold.read`).
- **A cloud head** moves one rung per 300 seconds (4085–4108; now `FanHold.cloud_token`).

The fan memory and `HeadWriteMemory` are two implementations of one rule (audit M6).

### 3.13 Door/window pause

Chart W1–W11, both specs. Additions:

- It runs inside the compute and has effects there: a store write, a notice, a snapshot, and on a resume a scheduled Dry command (6011–6055, 6104–6108; audit M4).
- **Notices.** One pause notice per room per 600 seconds, and its resume notice only if the pause notice went out (6128–6158).
- **Vanes owed.** A resume sends each vane the pause found, or one picked while paused, after the apply (6099–6103, 6196–6222).
- **Wakeups.** One per room at the end of its delay, firing a refresh at once (6240–6292).
- **A fixed standby park** sends every head to the park mode, a paused one included (3484–3500). With a `fan_only` hold a paused head runs its fan. The docs say the hold "parks every head". The spec's `PauseStopsHead` holds only because standby is outside the model.

### 3.14 Coil-dry idle, and Allow Dry

**Coil-dry (idle action "off after drying").** Per head, the last active mode and when it
stopped, on the monotonic clock (1048–1054).

| Event | From → to | Code |
|---|---|---|
| The head is sent cool | → owes a dry run, restamped every apply | 3172–3173, 3971–3972 |
| The room is satisfied inside the window | cooling → fan_only, with one wakeup at the window's end | 3397–3414, 3433–3445 |
| The window ends | fan_only → off | 3408–3412 |
| A start | nothing known → a full window if the head shows cool or fan_only | 3416–3431 |
| The head leaves dry | → owes a dry run from now | 2950–2956, 5193–5202 |
| A door/window pause | → straight to off, no dry run | 2890–2902 |

**Allow Dry.** Per room: not in dry; pick pending; held; parked for heat; released. `DryMode`
in `dry.py` owns all of it; the coordinator calls it from the compute, the apply, the heals and
a head's report.

| Event | From → to | Guard | Action | Code |
|---|---|---|---|---|
| The head reports dry | not in dry → held | the room offers Dry and is enabled, no standby hold | No mode, setpoint, fan or vane write | `DryMode.in_dry`, `DryMode.settle`; `_apply` |
| Dry picked on the thermostat | not in dry → pick pending | Coordinator on, no standby hold | Enable the room, send dry once, 60 s wakeup | `climate.py` `_pick_dry`; `DryMode.pick_refusal`, `DryMode.start_pick`, `DryMode.async_send` |
| The head reports dry | pick pending → held | — | Cancel the wakeup | `DryMode.head_reported` |
| The wakeup fires, or the head refuses | pick pending → not in dry | — | Back to the plan | `DryMode._pick_expired`, `DryMode.async_send` |
| The shared mode is heat and a room wants heat | held → parked | — | One notice, the head parks, Dry is never sent back | `DryMode.settle`; `_apply`; `DryMode.notice_parked` |
| Heat/Cool or Off picked on the thermostat | held or pending → released | — | The plan has the head; cleared when the head reports another mode | `DryMode.release`, `DryMode.head_reported` |
| The head stops listing dry | held → not in dry | — | One notice | `DryMode.head_reported` |
| Allow Dry turned off in Configure | held → not in dry | — | One notice before the reload | `DryMode.option_turned_off` |

A dry head votes cool at vote rank, and not at all under a cool lockout (1938–1941). All of it is
in memory: after a start a head still in dry is simply held again (`DryMode.__init__`).

**Modeled.** Tests only (`tests/test_dry_mode.py`).

### 3.15 Freeze floor and overheat ceiling

**States per room** (`floor_state`). none; enabled (the room is on, so the floor is idle); armed;
heat or cool (tripped and driven); held (tripped, waiting or rejected); blind (no reading, head
unavailable, or stalled); yielded.

| Event | From → to | Guard | Action | Code |
|---|---|---|---|---|
| The room is disabled with a floor set | → armed | — | — | 6618–6629, 6692–6696 |
| The reading passes the floor | armed → heat or cool | usable reading, head available | Start a run, send the trip notice | 6683–6698; `logic.py:1012-1041` |
| The plan runs another mode, Coordinator off, or a fixed standby park | tripped → held | — | The no-progress clock does not run | 6728–6752 |
| The head rejects the command | tripped → held, rejecting | — | One notice per run; retried each update | 6925–6965 |
| The reading reaches the clear line | tripped → armed | — | End the run, hand the head back if it was a person's | 6688–6691, 6754–6768 |
| No usable reading, or the head is unavailable | any → blind | — | End the run, one notice per episode | 6633–6659, 6887–6903 |
| Two hours driven with no step of progress | tripped → blind (stalled) | — | Hand back; re-arms on a new worst reading or the clear line | 6676–6682, 6708–6717; `logic.py:1066-1108` |
| A person changes a hands-off head's mode mid-run | tripped → yielded | — | Release the room for 4 hours or until it clears | 7071–7093, 6660–6675 |
| The room is enabled, or the floor removed | any → enabled or none | — | Forget the run | 6624–6629 |

**Hand-back** (a hands-off run that took over a person's own run in the same direction).
Remember their mode and setpoints (6770–6784), follow their later setpoint changes (6836–6875), and
at the end queue one command giving the head back (6877–6885, 2855–2857, 2910–2935). A refusal is
tried once more, then reported (6977–6996).

**Persistence.** The run, the person's setting and a yield ride in the enable switch's extra
restore data (7095–7152, `switch.py:279-288`). That is the 15-minute window of audit H2.

**Modeled.** The arbitration tier is in `tests/test_modehold_properties.py`. The run is in tests
only. `_floor_expect` (1165, 6808–6875) is a fourth "is this report my own write" mechanism
(audit M6).

### 3.16 Cloud head batching

**States per cloud head** (`cloud.py:37-173`). startup window (any report may be an old poll) →
unconfirmed or confirmed; a batch sent → pending for the confirm wait → confirmed, or
unconfirmed and sent again; a batch refused → unconfirmed and waiting for the write interval,
which wakes the plan when it allows the next batch; waiting for the write interval; waiting for a
fan step; not controllable.

| Event | From → to | Guard | Action | Code |
|---|---|---|---|---|
| Build | → startup window | — | One confirm wait in which nothing is sent | 3797–3805, 3979–3991 |
| The plan wants something the head does not show | → pending | the write interval allows, the head is controllable | Record the batch, then send mode, setpoint, fan | 4012–4047; `cloud.py:118-159` |
| The same, too soon after the last batch | → waiting for the write interval | — | Wake when due; whatever the plan wants then goes out | 4019–4024 |
| A call in the batch raises | pending → unconfirmed, waiting for the write interval | — | Drop the batch's intent, so nothing waits on it. The write interval still counts from it, and the window stays open, so a report of an older state is still stale. Set `release_at` to the moment the interval allows the next batch and re-arm the cloud wakeup, which the compute that queued the batch may already have done without it; if the refusal itself took longer than the interval, request a compute now. The room reads rejected and a tripped floor reads held (SM-3, fixed) | 4043–4046; `CloudHead.refused(clock)`, `_sync_cloud_timer`; `CloudHead.status` reads pending only with an intent or in the startup window |
| The plan wants what the last batch carried | pending → pending | inside the confirm wait | Nothing is sent again | 3979–3991 |
| The head shows everything wanted | pending or unconfirmed → confirmed | the window is over | Stamp the time | 4012–4018 |
| A report of an older mode, fan or setpoint | pending | inside the window | Not a person, not drift, not a vote | 3906–3919; `cloud.py:83-116` |
| Unavailable 15 min, or silent 35 min (never by silence for a generic head) | → not controllable | — | Stop writing, drop the room's call, raise a repair issue | 3842–3904 |
| Any report from it | not controllable → controllable again | — | Refresh; the repair issue is cleared | 4160–4165, 5234–5235, 3879–3884 |

**Rules.** No write in the startup window. At most one batch per write interval (60 s default, 30
to 300). One fan rung per 300 s. No write while not controllable (3968–3969).

**Modeled.** Nowhere (`docs/formal/README.md:96-97`). Finding SM-3, now fixed, is here; its tests
are in `tests/test_cloud_heads.py` and `tests/test_cloud_logic.py`. The ones that show the wakeup
let only Home Assistant's own timers run once the room needs heat.

### 3.17 Seasons (lockouts)

**States.** The pair (heat lockout, cool lockout): neither, heat locked, or cool locked. The two
switches own the values and restore them. With a weather source set, MXZ drives the switches.

| Event | From → to | Code |
|---|---|---|
| Setup, the weather entity changes, or the hourly timer | re-read | 1539–1552, 7451–7459 |
| The daily high is at or above the heat threshold | → heat locked | `logic.py:401-423` |
| The daily high is at or below the cool threshold | → cool locked | `logic.py:401-423` |
| Between the two, or no reading at all | → neither | `logic.py:401-423`; pinned by `tests/test_logic.py:181-182` and `tests/test_changeover.py:207-208` |
| The pair differs from the switches | drive each switch through its own service | 7501–7518 |

**Modeled.** Nowhere. SM-7 is the start-up case.

### 3.18 Standby hold and eco

**States.** not held; held at eco (the plan runs with the eco band forced); held at a fixed park
(off or fan_only: no plan is applied).

| Event | From → to | Action | Code |
|---|---|---|---|
| The watched entity reads its active state | → held | Retire vane kicks, refresh; a fixed park sends every head the park mode, mode only | 7551–7566, 2863–2872, 3484–3500 |
| It reads anything else | held → not held | Move each fan hold into the restore slot, refresh | 7560–7589 |
| It is missing, unavailable or unknown | → not held | Fail toward normal | 7521–7534 |

While held: no adoption (5403), no heal (5231, 5306), no Dry (6391), no vane kick (4820), no
hands-off vote (5659), no hold or coast wakeup under a fixed park (2613–2619, 2817–2823). A fixed
park holds a tripped floor; an eco hold lets it run (6735–6752).

**Modeled.** Nowhere.

### 3.19 Vane kick

**States per head.** idle → waking (fan_only issued) → spinning up (8 s) → applying (a select
write and 20 s per option, taking the newest pending option) → parking (off) → idle. From any of
them: retired → cleanup (the retiring path sends off if the head still shows the fan_only MXZ
sent) → idle.

| Event | Guard | Action | Code |
|---|---|---|---|
| A vane option is picked | head running, Coordinator off, or a standby hold | A plain select write, no kick | 4812–4822 |
| A vane option is picked | head off, Coordinator on, no hold, not paused | Record it and start a kick, or let the running kick pick it up | 4823–4831 |
| A vane option is picked | room paused | Keep it in the pause's snapshot for the resume | 4799–4811 |
| Each await in the kick returns | still the owner | Next step | 4833–4876, 4878–4885 |
| Ownership is lost (retire, Coordinator off, a standby hold) | — | The task stops; the retiring path parks the head | 4887–4956 |

**Rules.** The apply leaves a head mid-kick alone (2886–2887, 3495–3496). A kick's own wake and off
are never read as a person (5151–5152, 5171–5172, and the ledger).

**Modeled.** Nowhere.

### 3.20 Dual setpoint

**States per room.** single target; dual, edges not yet settled; dual, settled.

| Event | From → to | Action | Code |
|---|---|---|---|
| An options save that only switches rooms | single ⇄ dual | Applied in place: seed a range or take the snapped midpoint, add or remove the two numbers, fresh engage decision, refresh at once | 7236–7314 |
| Any other save that also switches a room | single ⇄ dual | A one-shot note in `hass.data`, then a reload | `config_flow.py:3227-3257`; 987–991 |
| The numbers restore | unsettled → settled | The second edge settles the pair; a missing edge or a fresh opt-in seeds both | 7316–7344, `number.py:411-438` |
| `async_setup` finds an edge never restored | unsettled → settled | Seed the pair | 1478–1489 |
| The pair no longer fits the head's limits | settled → settled, re-centered | A note on the room until the range is written | 7357–7386 |
| The head is cloud or generic | dual → single | Forced at build | 3771–3774 |

**Rules.** Never one restored edge with one seeded edge. The gap is at least the minimum. The
target is the midpoint.

### 3.21 Persistence: what each start brings back

| State | Where | A reload | A clean restart | A crash |
|---|---|---|---|---|
| Shared mode, last change, set-by-votes | `Store`, written at once (2715–2734) | kept | kept | kept |
| Door/window state, override, snapshot | `Store`, written at once (6224–6238) | kept | kept | kept |
| Room target, drift override, dual edges | number restore data (`number.py:107-150`, `236-270`, `411-438`) | kept | kept | as last dumped, up to 15 min old |
| Room enable, Coordinator, eco, lockouts | switch restore data (`switch.py:170-186`) | kept | kept | as last dumped |
| Hands-off release, floor run, person's floor setting, yield | enable switch extra data (`switch.py:223-288`) | kept | kept | as last dumped |
| Fan hold and fan echo memory | Fan auto switch state and extra data (`switch.py:76-138`, `342-445`) | kept | kept | as last dumped |
| Head write ledger, delivery slots, head locks, command status, vane cleanup flag | `hass.data` (1368–1385) | kept | **lost** | **lost** |
| Dual-switch hand-off note | `hass.data` (987–991) | consumed | lost | lost |
| Engage latch | memory; re-read from the head (3.6) | re-read | re-read | re-read |
| Adopt hold and what moved, heal timers, notice limits | memory | lost | lost | lost |
| Sensor health, head sight and votes, cloud head record | memory, restarted with a grace or a window | restarted | restarted | restarted |
| Dry state, Dry pick, coil-dry clock | memory; the coil-dry clock is re-seeded from the head | lost | lost | lost |
| Standby hold | memory; re-read from the entity at setup | re-read | re-read | re-read |

The restore-data rows are audit H2 and belong to chunk 6. The `hass.data` row is what SM-1 turns
on.

### 3.22 Setup, reconfigure and options flows

Sampled. The step graphs, from the `async_step_*` methods:

- **Setup.** `user` → `rooms` → `head_type` (only with generic heads) → `sensors` → `review` (menu: `finish`, `tuning`, `user`). `tuning` is a menu of the five pages (`comfort`, `fan_idle`, `seasons`, `limits`, `standby`), each returning to `tuning`, with `review` as the way back. `finish` is the only save. It rechecks head modes, head ownership, sensors and the idle action, and can send the user back to `user`, `sensors` or `fan_idle` (`config_flow.py:2287-2378`).
- **Reconfigure.** `reconfigure` → `reconfigure_rooms` → `reconfigure_sensors` → `reconfigure_review` (menu: `reconfigure_finish`, `reconfigure`). The finish step rechecks, moves reordered rooms' entity records to their new slots, then updates the entry once; the update listener does the one reload (`config_flow.py:2731-2817`).
- **Options.** `init` (menu of the five pages and `rooms`) → a page, or `rooms` → `room_N` → `room`. Each page saves through one `_save`, which merges onto the stored options, mirrors into `data`, and makes one entry update (`config_flow.py:3260-3301`). The listener then switches a dual room in place or reloads (`__init__.py:183-196`).

A flow reserves its heads in its context and releases them when it is sent back with an error
(set at `config_flow.py:1963` and `2521`, released at `2304-2321` and `2819-2831`). No invalid transition was found at this depth. Audit M8
covers the structure.

### 3.23 Comfort priority

Added after the second review, which found this machine missing. From a read of the cited lines
and the existing tests; nothing new was run for it.

**State per room.** A status, shown as `comfort_priority_status` on the plan row, and the boost
that goes with it (`logic.py:525-566`):

| Status | Boost | When |
|---|---|---|
| `not_set` | 0 | no entity configured |
| `missing` | 0 | the entity does not exist |
| `unavailable`, `unknown` | 0 | its state is one of those |
| `off` | 0 | an on/off entity (`binary_sensor`, `input_boolean`; `const.py:128`) not exactly `on` |
| `on` | 1 | an on/off entity reading `on`, younger than the stale limit |
| `stale` | 0 | an on/off entity reading `on` for the stale limit or longer |
| `number` | its value, negative read as 0 | any other domain with a finite number |
| `not_a_number` | 0 | any other domain with text, NaN or infinity |

Nothing is stored. Each compute reads the entity (1945–1946; 5926–5950), and the age is the wall
clock minus Home Assistant's own `last_changed` for it, so a clock stepped back reads as fresh
(5926–5940).

**Rules.**
- A boost orders entries only inside their own tier, after the tier and before the room order. At Safety priority or above, and for anything not a positive finite number, it counts 0, so a reading can raise a room and never lower it (`logic.py:504-522`).
- Stale applies to on/off entities only, never to a number (`logic.py:554-566`).
- The stale limit is Configure → Comfort → Advanced, in hours: blank is 12, 0 is no limit, and a stored value is clamped to 0–168 (1013–1018, 7811–7826; `const.py:442-443`).
- `mode_by_comfort_priority` is true only when a boost changed the answer: the same arbitration with every boost at 0 decides otherwise (1993–2002, 2074). The dwell wakeup replays the boost from the plan row (2637).

| Event | From → to | Action | Code |
|---|---|---|---|
| Setup | → read at the first compute | Listen to every room's entity | 1573–1582 |
| The entity changes state | any → its new reading, aged from now | Request a compute | 7546–7549 |
| An `on` reaches the stale limit | on → stale | One wakeup, armed at the end of each compute for the nearest such moment; it only requests a compute, which re-reads every entity | 5952–5987, 2532; `logic.py:569-586` |
| A reload | unchanged | Home Assistant keeps the state and its `last_changed`, so a stale `on` stays stale | `test_the_age_survives_a_reload_and_restarts_with_a_new_state` |
| A Home Assistant restart | stale → on | The entity's state is written again at boot, so its age counts from then | the same test; `docs/FEATURES.md:744-750` documents it |
| Retire or unload | — | Cancel the wakeup; a late re-sync arms nothing | 1729, 5961; `test_unload_cancels_the_stale_wakeup` |

**Modeled.** `MXZModeHold.tla` leaves Comfort priority out (header, base line 46).
`tests/test_modehold_properties.py` checks the boost in `logic.arbitrate` for every input in its
table. The statuses, the stale moment and its wakeup are in tests only:
`tests/test_comfort_priority_logic.py` and `tests/test_comfort_priority.py`
(`test_the_stale_moment_wakes_a_recompute_by_itself`).

**Findings.** None found by reading. A restart giving a stuck `on` its boost back is documented
behavior, not a finding.

---

## 4. Invalid states and transitions the code can reach

Findings are numbered SM-1 to SM-10 in this document only. The priority order is in section 5.

### SM-1. A late report of MXZ's own pre-restart write is adopted as a person's press

**Label.** `confirmed (run)` on invented heads. Whether a real head can produce the report is unknown.

**The rule it breaks.** "A report MXZ's own writes can explain is never a person" (5319–5320,
`docs/formal/README.md:73-74`).

**Evidence.**
- The ledger is in `hass.data` (1380–1385). A reload keeps it. A process restart starts with none.
- With no ledger entry, `_head_write_is_own` is false (5354–5356), so a report that moves the setpoint is a press (5432–5437) and arms the five-second hold (5481–5482).
- After a restart the first apply rewrites the room's target if the head does not show it, and remembers that write and what the head showed then (5336–5345). A write from before the restart that the head has not shown yet is in neither.
- The only guard is startup quiet (5405), which ends 40 seconds after Home Assistant starts (5040–5050, `const.py:657`).
- `tests/property_rig.py` `Rig.restart` pops only the two stores (lines 305–321 of that file), so the ledger survives its "restart", and it sets the entry up with Home Assistant already running, so startup quiet is never on. `test_the_memory_rides_a_reload_with_the_head_still_behind` covers the reload only.

**Probe.** Study cooling, the head set to lag its commands, target set to 72 and then 74, then a
restart that drops every `hass.data` key starting `mxz_coordinator` at unload. Three cases:

| Case | Setup | Late 72 arrives | Result |
|---|---|---|---|
| running | Home Assistant already running (the earlier scratch test) | at once | target 74 → 72; first report armed a hold |
| quiet | entry set up with `hass.set_state(CoreState.not_running)`, then the start event | inside the 40 s | target stays 74; no hold armed |
| after quiet | the same, then 41 s | after the window | target 74 → 72; first report armed a hold |

In the two failing cases the room ends at 72 with the head at 72. A person set 74.

**How it could happen in a house.** A real restart always has the quiet window, so the "running"
case is not a real restart. What is needed is a head whose entity shows a value older than MXZ's
last write when Home Assistant comes back, and then catches up more than 40 seconds after the
start event.
- A local ESPHome head is unavailable until its device connects. `_watch_head_change` ignores a report from or to unavailable (5466–5470), and the device has had the whole restart to apply its last write. It would have to report an older setpoint first and the newer one later. Not known to happen.
- A cloud head also has its own startup window, one confirm wait from the build (90 s default, 30 to 600), in which nothing is adopted (5414, `cloud.py:103-104`). With the default drive offset its setpoint is never taken as a target at all (5441–5444), so only its on and off can be adopted. The code itself describes cloud reports as "minutes late" (6778–6780).
- The same path adopts a late mode. A late off for an enabled room disables it (5421–5424); a late heat or cool for a disabled room enables it (5429–5440). That variant was read, not run.

**Why this is not a small fix.** Three ways to close it, each a decision:
1. Persist the ledger. That is one more restore channel, which audit H2 says not to add before the room-state store (chunk 6).
2. Change the rule after a start, for example adopt nothing from a head until it has shown a write made since the start. That changes what a person at the remote gets after every restart.
3. Accept it as a documented exception, as the fan hold's "outage" and "start" exceptions are, and pin it with a passing test.

**Status: open, needs a decision.** Landed with no product change: `tests/property_rig.py`
`Rig.restart` is now a real restart (it drops every `hass.data` key MXZ keeps, sets the entry up
before Home Assistant runs, then starts it), and
`test_a_restart_loses_the_memory_and_the_start_window_keeps_a_late_write_out` pins the case that
holds today. Run on the new helper: inside the 40-second window the target stays 74; 41 seconds
after the start the late 72 is adopted. The case after the window is not pinned either way. In
the model, `StartWriteNeverAdopted` holds across a reload (`ci-adopt`) and breaks after a process
start once startup quiet ends (`ci-adopt-restart`), and `NeverLateOwnWriteInQuiet` shows the
window case is reachable.

### SM-2. An unexpected error from one head skips the mode record, so the unit can reverse with no hold

**Label.** `confirmed (run)`. This is audit M1 with its inferred consequence shown.

**The rules it breaks.** No flip inside the hold (`NoFlipInDwell`). After a decision no head runs
against the shared mode (`HeadsFollowMode`). Honest public state.

**Evidence.**
- Only `HomeAssistantError` is contained per head (3128–3133). Anything else sets `_delivery_error` and `last_update_success = False` (3134–3143).
- `_wait_deliveries` raises it after every head has been commanded (3099–3100), so `_apply` leaves before the writeback at 3035–3051 and before the owed vanes at 3022.
- `current_shared_mode`, the hold stamp and the stored record stay as they were. The next compute starts from the old mode with the gate open.

**Probe.** Two rooms cooling and idle, hold over. Head A's `set_hvac_mode` was made to raise
`RuntimeError`. Room A reached its target and room B went cold, so the plan chose heat.

| Moment | Coordinator's mode | Shared mode select | Plan sensor | Head A | Head B | Hold left |
|---|---|---|---|---|---|---|
| after the heat plan | cool | cool | unavailable | cool | heat | 0 |
| one second later, room B hot | cool | cool | — | cool | cool | 0 |

Head B's mode writes were `fan_only, heat, fan_only, cool`. It ran heat, then cool one second
later, and no hold was ever stamped.

**What goes unavailable.** The probe read the plan sensor. By reading, the same holds for every
entity built on `CoordinatorEntity`: the room thermostats (`climate.py:99`), the Fan auto and
Window Override switches (`switch.py:306-308`, `469`) and the paused-open sensor
(`binary_sensor.py:37`). The target numbers, the enable switches and the Shared mode select are
not, so they stay available, and the select goes on showing the old mode. Audit M1 says every
MXZ entity; it is these.

**Reach.** Any head whose integration raises something other than `HomeAssistantError` from a
climate service: a bug in a third-party integration, or a cloud library's own timeout or
connection error passed through. Which integrations do was not checked.

**Status: fixed.** Whatever a head's handler raises now reaches the per-head path as
`HomeAssistantError` (`_call_head_service`), so its room reads rejected and the next update tries
again; only a fault in MXZ's own delivery steps fails the plan. The mode is recorded
(`_write_back_mode`) with the heads' commands queued and none run, so the record no longer
depends on any of them. `tests/test_head_fault.py` fails on the base and passes with the fix;
reverting either part alone fails its own test. `_selection_seq` had nothing left to guard and
is gone.

### SM-3. A cloud batch is recorded as sent before it is sent, so a rejected batch is not retried until the confirm wait ends

**Label.** `confirmed (run)` for the missing retry, and for the floor row by the test below.

**Status: fixed.** `CloudHead.refused(clock)` drops the intent of a batch that raised and keeps the
write interval and the window in which an older report is stale. It also sets `release_at` to
when the interval lets the next batch out, and the refusal re-arms the cloud wakeup for it, or
requests a compute at once when the refusal took longer than the interval. The row reads
`unconfirmed`, not `pending`, and `cloud_pending_until` follows the status. The fan memory and
ladder index are still committed before the calls, so a refused fan write reads as MXZ's echo
until the next batch: the conservative direction, never a false hold.

The first fix dropped the intent but set no wakeup: `sent()` clears `release_at` and nothing
else stood for the write interval, so with no other input the cloud timer woke the plan only at
the window's end. The second review's probe showed the retry still 90 s after the refusal with
the default confirm wait, and 300 s (an unrelated wakeup) or 600 s with a 600 s one. The landed
test had recomputed by hand at +59 s and +60 s, so it never showed MXZ waking itself.
`test_a_refused_batch_goes_out_again_at_the_write_interval_with_nothing_else_happening` now
moves the clock a second at a time with no compute forced, for both confirm waits, once and
always refused: every retry is 60 s after the last.
`test_a_batch_refused_after_its_compute_has_ended_still_wakes_the_plan` covers a batch that
waited behind the head's lock, and
`test_a_refusal_that_outlasts_the_write_interval_is_sent_again_at_once` a refusal that took 61 s.
Each fails on the first fix, and each of the three new lines is needed by one of them.

**The rules it breaks.** "The next ordinary update may try again" (2168). A floor's "MXZ keeps
trying at each update" (6932–6933, 6962–6963). Honest public state.

**Evidence.**
- `cloud.sent(...)` runs at 4026–4030, before the calls at 4043–4046. It sets the intent and opens a new confirm window (`cloud.py:154-159`).
- If a call raises, the next compute finds `cloud.pending` true and the intent equal to what it wants, so it is "waiting" (3979–3982) and sends nothing (3983–3991, 4000–4018).
- On that path it also calls `_floor_accepted` (3990, 4013), which clears `_floor_rejecting` (6970–6972). A tripped floor's row then reads heat or cool, not held, though no command was accepted.
- The fan memory and ladder index are committed at 4031–4040, also before the calls.

**Probe.** Two cloud heads parked in fan_only and confirmed. Room 1 went cold; the head's
`set_hvac_mode` raised `HomeAssistantError` once.

| Moment | Calls the head received | `cloud_status` | `command_status` |
|---|---|---|---|
| after the rejected batch | the rejected heat | pending | rejected |
| three computes later, the cloud working again | nothing more | pending | rejected |
| after the confirm wait | heat, then the setpoint | pending | returned |

The room stayed cold with its head in fan_only for the whole confirm wait: 90 seconds by default,
up to 600.

**Reach.** Cloud and generic heads (beta), on any rejected call. The delay is bounded by the
confirm wait. For a tripped freeze floor on a cloud head that is the delay before heat is tried
again.

### SM-4. Whether an unexpected head error skips the mode record depends on a race

**Label.** `confirmed (read)`.

**Evidence.** `_wait_deliveries` stops waiting as soon as the wake event is set (3093–3095). A new
input sets it (2463–2464), and so does a head whose lock is held or whose vane cleanup is pending
(3118–3123). If that happens before a failing head has raised, `_delivery_error` is still unset
at 3099, the writeback runs, and the failure that follows only marks the plan failed (3134–3143).
If the failure comes first, the writeback is skipped (SM-2). The same fault gives two different
records of the mode.

**Reach.** Same trigger as SM-2. It shows that the record should not depend on a delivery's
outcome.

**Status: fixed** with SM-2: nothing awaited comes between the decision and its record.

### SM-5. A head that refuses its park stays in the old mode while the others run the new one

**Label.** `confirmed (run)` as a state (the SM-2 probe's first row: head A cool, head B heat). It
is the documented design, so it is listed as an exception to write down, not a bug to fix here.

**Evidence.** A flip queues every head at once (2885–3021). The heads leaving the old mode are not
confirmed parked before the others are sent the new one. A refused or failed park is that room's
alone (3128–3133), and it is retried at each update.

**Why it matters.** The rule "after a decision no head runs against the shared mode" is true of
what MXZ commands, not of what the heads show. One outdoor unit with heads in both modes is the
stand-off the integration exists to prevent, for as long as the head keeps refusing.

**Status: documented exception.** It is a row of the exceptions table in
[formal/README.md](formal/README.md#documented-exceptions). In the model a `Refuse` leaves the head
as it was, `HeadsFollowMode` leaves a refused head out, and `NeverRefused` shows the path is
reached. `test_a_head_that_raises_during_a_mode_change_doesnt_undo_the_change` shows it on the
real code: the refusing head stays in cool while the other runs heat. Sending parks before runs
is a change to the delivery queue (chunk 5) and a decision for the owner. The split that moved the
queue to `delivery.py` left it as it was: making a run wait on another head's park changes when
heads are sent their commands, so it is a behavior change, not part of a move. If it is made, it
goes in `DeliveryQueue.queue` and `_apply`'s order of queueing.

### SM-6. A press at the head while MXZ's command to it is queued is written over

**Label.** `confirmed (run)`. This is the in-flight-apply case the accept-at-head repros left unwritten.

**Status: fixed.** When a press that will be adopted arms the hold, the head's waiting command
is disowned (`_disown_waiting_command`, shared with the floor yield below).
`test_a_press_while_mxzs_command_waits_for_the_head_is_adopted` holds the head's lock with a fan
pick on the room's thermostat, queues a plan's command behind it, presses at the head and lets
the pick finish: on the base the head went back to 68–70 and the target stayed 70; with the fix
the press is adopted. The negative, `test_a_flip_alone_while_mxzs_command_waits_is_put_back_by_it`,
shows a change with nothing to adopt leaves the command to put the head back.

The first fix had a gap of its own, a regression against the base. A press arms the hold and
disowns the command; if the person then puts the head back inside the 5 seconds, nothing is
adopted and the hold ends, but nothing asked for a compute: the disowned command had no `next`,
a setpoint-only report requests no refresh, and the apply had skipped the head while it held.
The head kept 68–70 while the room's target read 74, with only Home Assistant's timers running
for 300 seconds. A head report that ends a hold with nothing adopted now requests a compute, as
the floor-yield caller of the same disown does.
`test_a_press_taken_back_in_its_hold_leaves_the_room_target_to_reach_the_head` queues a command
for a new target behind a fan pick, presses, takes the press back before or after the command's
turn, and lets only the timers run: the head gets 72–74. Its third case, a press that stays, is
adopted and the waiting command never goes.

**Evidence.**
- The apply checks for a holding press only when it queues (2888–2889).
- A task already queued waits for the head's lock (3124). When it gets the lock it issues its command if the head does not show the plan (3655–3703). Nothing re-checks the press: `_delivery_owned` (3102–3106) knows nothing of it.
- MXZ's write then shows on the head. That report is MXZ's own, so `_head_adoption` returns nothing and the hold is dropped (5478–5480).
- The code already has the right pattern for one case: a person's change during a floor run disowns a queued command (7083–7089).

**Reach.** The window is one event-loop turn for an idle local head. It is as long as the previous
handler takes when the head's lock is held: a slow integration, a cloud head, a vane cleanup.

**Repro sketch.** A head fake whose service handler waits on an event the test controls (a public
seam, no private field). Let one command hold the lock, change the room's target so a second
command queues, press a different setpoint at the head, release. Expected under the default: the
press is adopted. Predicted now: the head is written back and the target does not move.

### SM-7. At a start, an unreadable forecast releases restored season lockouts until the weather entity loads

**Label.** `confirmed (run)` with an outdoor temperature sensor as the source; a weather entity
takes the same `_read_outdoor_high` → None path, which was read, not run.

**Run.** Outdoor high 72, so the heat lockout is on. A restart with the sensor not loaded yet: after
setup both lockouts read off. The sensor loads at 72: the heat lockout is on again.

**Status: open, needs a decision.** Holding a restored lockout until the source has been read once
changes what the house does at a start.

**Evidence.**
- `async_setup` starts `_evaluate_changeover` before the first refresh (1552, 1594).
- With the weather entity missing or unavailable, `_read_outdoor_high` returns None (7477–7479) and `season_lockouts` returns neither lockout (`logic.py:401-423`).
- `_drive_lockout` then turns off a lockout switch that restored on (7501–7518). The entity's next state change puts it back (1541–1544, 7451–7454).
- MXZ declares no dependency on the weather integration (`manifest.json`), so its setup can run first.

**Consequence.** For the seconds, or with a provider outage the hours, in between, the plan runs
with no lockout. A room past its threshold can turn the shared mode the locked way, and the hold
then keeps it there. "No reading means no lockout" is deliberate in steady state and pinned by
tests. The start-up case is not pinned and may not be intended.

**Repro sketch.** Restore a lockout switch on, set the entry up with the weather entity absent,
assert the switch is still on after setup, then add the entity and assert again.

### SM-8. A reload inside an apply that changes the mode loses the hold

**Label.** `confirmed (read)`. At the base, `MXZModeHold.tla` lines 10–18 named it as unmodeled.
That header now says the record comes before the deliveries, and `ci-apply` reaches a reload
inside an apply (section 6.1).

**Evidence.** A reload retires the coordinator and releases the apply's wait (1708–1709,
3195–3213). The writeback then sets the mode and stamps the time in memory (3037–3043, 2709–2712)
and `_record_mode` returns without writing (2725–2726). Heads already commanded keep the new mode.
The new coordinator reads the old mode and the old change time.

**Consequence.** The new coordinator's first compute normally chooses the new mode again and
stamps it then. If the inputs moved in between, the unit can reverse with no hold. The window is
one apply.

**Status: closed by the SM-2/SM-4 fix.** The record is written before any head is sent the new
mode, so a reload during the deliveries finds it. A behavior test could not reach a reload inside
the wait through public seams: any input, the other heads' own reports included, releases the
wait first. `RecordLeadsHeads` in `ci-apply` checks it with reloads and restarts between the
decision and every command, and the broken copy P1 (the record not written before the heads)
breaks it.

### SM-9. State that rides restore data can be up to 15 minutes old after a crash

**Label.** `inferred`. This is audit H2, listed here once so the catalog is complete.

Section 3.21 lists the rows: a room's target, enable, drift override and dual edges, the fan hold
and its memory, a hands-off release, a floor run, the person's floor setting, and a yield. The fan
hold's case is already an accepted exception (`pickKind = "crash"`, `ci-fan-crash`). The others
have no test and no exception on record. The clean path is the room-state store (chunk 6, the owner's
OK). No patch is recommended.

### SM-10. A write still queued at a head can drop out of MXZ's memory and be adopted as a press

**Label.** `confirmed (run)` on invented heads, for two of the three ways below. Found by TLC
(`ci-adopt`, then `OwnWriteNeverAdopted` before the ways were split out).

**The rule it breaks.** "A report MXZ's own writes can explain is never a person".

**Evidence.** `HeadWriteMemory` (`logic.py:787-847`) keeps what the head last showed first and
MXZ's unshown writes after it. Three steps can drop a write the head has not shown yet:

- **A rewrite kept once.** Each apply rewrites the target while a lagging head still shows the old
  one. `wrote` keeps a rewrite of the last value once, by design. The head's report of the first
  copy then leaves that value first, as what the head shows, and a person's press replaces it
  (`seen`, at adoption). The second copy is no longer remembered.
- **A press of a remembered value.** It reads as MXZ's own, so `caught_up` drops every older
  write, including ones still queued at the head.
- **`seen` in a write.** `wrote` calls `seen` for a press MXZ hasn't read (a hold a target change
  ended), which can replace a queued write the same way. Model only.

**Runs.** With `RigHead` lagging (`tests/property_rig.py`):

| Way | Steps | Result |
|---|---|---|
| A rewrite kept once | target 72 set, two applies (two writes of 72 queued), the head shows the first, a person presses 66 (adopted), the head shows the second 72 | the target goes back to 72 |
| A press of a remembered value | target 72 set (queued), a person presses 72 at the head, then 66 (adopted), the head shows MXZ's 72 | the target goes back to 72 |

In both, the person's last press is lost to MXZ's own write with nobody touching anything after it.

**Why this is not a small fix.** The memory's rewrite rule is deliberate (`wrote`'s docstring)
and shared in spirit with the fan memory's F1 costs. Keeping each copy, or keeping a replaced value
while writes are queued, moves the cost: a person's later press of that value would read as MXZ's
echo for longer. Which cost to carry is a decision.

**Status: open, needs a decision.** In the model, each queued write carries the ghost kind of
what dropped it, so the strict rule `OwnWriteNeverAdopted` passes in both adoption configs and
`QueuedWriteNeverAdopted` is a finding that breaks in `ci-adopt` until this is decided.

### Checked and cleared

| Question | Answer | Evidence |
|---|---|---|
| Does a start inside an adopt hold adopt anything? | No, the head goes back on the plan | `test_a_start_inside_the_hold_drops_the_change`; 1740–1742 |
| Two heads pressed inside one hold; adoption inside the mode wait; an Allow-Dry room turned off at its head | Each pinned by #114 | `tests/test_accept_head_changes.py`, `tests/test_dry_mode.py` |
| Does the off heal fight an adopted off? | No, it is cancelled after the adoption | 5555–5563 |
| Is a sensor change dropped by the generation check? | Only when a later compute already read it | 4975–4983, 2490–2501 |
| Can a timer fire into a retired coordinator and write? | No. Each callback checks a retired flag or ends in a request that returns | 2432, 2662, 2836, 4149, 5812, 5854, 5978, 6284; 2452 |
| Is a vane kick's wake read as a person? | No | 5151–5152, 5171–5172; `test_a_vane_kick_is_not_a_person` |
| Does a person's pick lose to an apply already running? | No. The apply writes its mode back before it awaits anything, so a pick during its wait comes after that record and nothing in the apply writes the mode again. At the base the writeback came after the wait and skipped itself over a newer pick (3035–3036, 7440–7442) | `_write_back_mode`; `test_a_newer_selection_survives_an_older_in_flight_apply` |
| A fixed standby park sends a paused, disabled or released head to the park mode | Documented: the hold "parks every head" | 3484–3500; `docs/FEATURES.md:689` |
| A lost wakeup in the refresh follow-up | None found by reading. Not proven: unmodeled, not run | 2538–2580 |
| SM-2, SM-3, SM-4, SM-6, SM-8 | Fixed or closed in the change that added this file | The table at the top |

---

## 5. Priority, and what became of each

Ranked by what a household would feel, then by how sure the evidence is.

| Rank | Finding | Impact | Evidence | Outcome | Audit |
|---|---|---|---|---|---|
| 1 | SM-2, with SM-4 | The unit reverses with no hold; the room thermostats and plan sensor unavailable; the select shows the wrong mode | run | Fixed | M1 |
| 2 | SM-1 | A room's target or enable changes with nobody touching it, after a restart, under a default-on option | run on invented heads; hardware unknown | Open: a decision | M9, M10, H2 |
| 3 | SM-10 | A person's press at a lagging head is lost to MXZ's own later write | run on invented heads | Open: a decision | M6 |
| 4 | SM-3 | A cloud room, or a freeze floor on a cloud head, waits out the confirm wait after one rejected call; the row reads pending | run | Fixed | — |
| 5 | SM-6 | A press at the head is lost | run | Fixed | M9 |
| 6 | SM-7 | A season lockout lapses at a start | run | Open: a decision | — |
| 7 | SM-8 | A reversal with no hold in a one-apply window at a reload | read; model | Closed with SM-4 | — |
| 8 | SM-5 | Heads in both modes while one refuses its park | run; by design | Documented exception; chunk 5 for a change | H1 |
| 9 | SM-9 | A recent change lost at a crash | inferred | Chunk 6 | H2 |

The structural findings behind several of these (audit M4, M5, M6) belong to the coordinator split
(chunk 5). None of the fixes here needed it.

---

## 6. The TLA+ extensions

Both are in `docs/formal/tla/MXZCoordinator.tla`, behind constants that are FALSE in the eight
configs that were there before, so those check the same machine. No third spec was added.
`verification/modehold/MXZModeHold.tla` changed only in its header comment, which now says the
record comes before the deliveries.

### 6.1 The apply as steps (`WithSplit`, `ci-apply`)

A Decide is the compute and the mode's record, as `_apply` now does them with nothing awaited
between. Each head's command is a later step: `Deliver`, or `Refuse` for a head whose handler
raised. Readings, picks, reloads and restarts can land between them; a start drops the commands
not yet run, and an apply counts as a decision only if no input came in since. The rule
`RecordLeadsHeads` says no head is sent a direction the record doesn't hold, unless a person has
picked since. `HeadsFollowMode` and `PauseStopsHead` leave a refused head out (SM-5).

Broken copies: P1 (the record isn't written before the heads) breaks `RecordLeadsHeads`; P2 (an
apply an input overtook counts as a decision) breaks `HeadsFollowMode`. Covers: `NeverRefused`,
`NeverCutShort`.

### 6.2 Changes made at a head (`WithAdopt`, `ci-adopt`, `ci-adopt-restart`)

Room 1's target, the setpoint its head shows, MXZ's writes a lagging head hasn't shown yet (up to
two), the memory as `HeadWriteMemory` keeps it, the hold, startup quiet, a person's press, a target
set in Home Assistant, reloads, and process starts. Each queued write carries a ghost kind for what
dropped it from the memory, if anything did. Rules: `OwnWriteNeverAdopted`, `HoldBlocksWrite`,
`StartWriteNeverAdopted` (passes in `ci-adopt`, an open finding in `ci-adopt-restart`, SM-1), and
`QueuedWriteNeverAdopted` (an open finding in `ci-adopt`, SM-10).

Broken copies: A1 (a write isn't remembered) breaks `OwnWriteNeverAdopted`; A2 (a hold doesn't stop
the write) breaks `HoldBlocksWrite`; A3 (a reload loses the memory, as a process start does) breaks
`StartWriteNeverAdopted`. Covers: `NeverAdopts`, `NeverLateOwnWriteInQuiet`.

Not in it: the enable (on and off at a head), the heat/cool choice, and every gate but startup
quiet. `tests/test_accept_head_changes.py` holds those.

### 6.3 Gaps that stay documented

| Gap | Why it stays out | Where it is held |
|---|---|---|
| The delivery queue's locks, supersession and follow-ups (3.3) | One apply at a time in the model; the queue is chunk 5's `DeliveryQueue` | `tests/test_command_delivery.py`, `tests/test_refresh_race.py` |
| Cloud batching and confirm wait (3.16) | SM-3 is an ordering bug a test pins better than a model | `tests/test_cloud_heads.py` |
| Freeze floor run, hand-back and yield (3.15) | Large state, dense in tests; rides H2 | `tests/test_floor*.py`; chunk 6 |
| Dry, coil-dry, seasons, vane kick (3.14, 3.17, 3.19) | Independent of the hold and the two memories | Their tests |
| Standby hold (3.18), including a fixed park of a paused head | `PauseStopsHead` assumes no standby | A row of the README's exceptions |
| The refresh debouncer and follow-up (3.2) | Home Assistant internals on two code paths (audit M2) | `tests/test_refresh_race.py` |
| The crash rows of SM-9 other than the fan | Chunk 6 | — |

### 6.4 One subsystem at a time, and the user flows drawn

Added 2026-11-11. `tools/formal_area.py` runs one subsystem's part of `MXZCoordinator.tla`
with its tests: 3.12 (`fan`), 3.13 (`window`), 3.11 (`adopt`), and 3.3 with 3.5's writeback
(`apply`), and a skill under `.claude/skills/` that says when and how to run it
(`verify-mxz-fan-hold`, `verify-mxz-door-window`, `verify-mxz-head-changes`,
`verify-mxz-mode-record`). Each has broken copies of the spec and of the code that its check must catch; C42
to C48 in `tools/code_mutants.py` are the code's for `adopt` and `apply`, which had none. The
pstack `/statechart` skill draws five user flows from the spec in
[formal/STATECHART.md](formal/STATECHART.md#user-flows-drawn-from-the-model): the mode and its
hold, the door/window pause, the fan hold, changes made at a head, and SM-1's path. They found
nothing new. SM-10 replays on the real code on its chart path. The model's shortest SM-1 path
does not replay, since the model lets a person act between a start and MXZ's first write after
it; SM-1 still replays on its own path.

`.github/workflows/tla.yml` runs on changes under `docs/formal/tla/**`, so it runs for this work. It
still does not run on code-only changes (audit M12, chunk 2 or 4).

---

## 7. What is open, and who decides

Each needs the owner's decision; none needs a patch until then.

- **SM-1.** Persist the head write memory (one more restore channel, which belongs to the
  room-state store, chunk 6), change the rule after a start (adopt nothing from a head until it
  has shown a write made since the start), or accept it as a documented exception pinned by a
  passing test.
- **SM-10.** Keep every copy of a rewrite and keep a replaced value while writes are queued (a
  person's press of that value then reads as an echo for longer), or accept it as a documented
  exception like the fan's F1 costs.
- **SM-7.** Hold restored lockouts until the outdoor source has been read once, or keep "no
  reading means no lockout" at a start too and document it.
- **SM-5, as a change.** Send parks before runs: the delivery queue of chunk 5.
- **SM-9.** The room-state store, chunk 6.

**House rules for the tests.** No new test reads or writes a coordinator private (audit H3); the
repros above drive entities, head fakes and the clock. No xfail: `tools/pytest_phases.py` fails a run
on one. A known cost is a passing test that pins it.
