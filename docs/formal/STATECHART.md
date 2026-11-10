# Statechart of the coordinator

This is the machine [`tla/MXZCoordinator.tla`](tla/MXZCoordinator.tla) checks, as a chart. It
was read from `custom_components/mxz_coordinator/`. It runs as six parallel regions: the
shared mode, each room's run, a room's door/window, a head's fan, an apply's commands, and the
coordinator's lifecycle, with changes made at a head beside them. [README.md](README.md) says what is and isn't modeled, and which rules are
checked on it.

Everything in a key's Code column is a function under `custom_components/mxz_coordinator/`,
named as it is defined (`Class.method` where the name alone is not one function), and found
by name wherever it lives. `tests/test_formal_contract.py` fails if a name here stops existing.

One compute and apply is one step of the machine. `MXZCoordinator._compute` decides and
commands no head (it does save the door/window store and send notices).
`MXZCoordinator._apply` queues each head's command, records the mode change before any of
them runs, then waits for them. Every region below advances inside that step, except where
the key says a report or a gesture moves it at once, and the
[apply's commands](#an-applys-commands), which are steps of their own in `ci-apply`.

## Shared mode and the hold

One outdoor unit runs one direction. A hold follows every real change of it.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> CoolOpen : fresh install (no record)
    CoolOpen --> HeatHeld : M1 the arbiter answers heat
    HeatOpen --> CoolHeld : M1 the arbiter answers cool
    CoolHeld --> CoolOpen : M2 the hold passes
    HeatHeld --> HeatOpen : M2 the hold passes
    CoolOpen --> HeatHeld : M3 a person picks heat
    CoolHeld --> HeatHeld : M3 a person picks heat
    HeatOpen --> CoolHeld : M3 a person picks cool
    HeatHeld --> CoolHeld : M3 a person picks cool
    CoolHeld --> CoolHeld : M4 a call for heat waits
    HeatHeld --> HeatHeld : M4 a call for cool waits
    CoolOpen --> HeatOpen : M5 only votes moved it
    HeatOpen --> CoolOpen : M5 only votes moved it
    CoolOpen --> CoolHeld : M6 a room runs in a mode votes chose
    HeatOpen --> HeatHeld : M6 a room runs in a mode votes chose
```

| Id | What happens | Code |
| --- | --- | --- |
| M1 | With the gate open, the first calling room in rank order decides. With nothing calling, Resting mode decides, once no room still runs in the shared mode. The new mode is stamped and stored before any head is sent it. | `_compute`, `arbitrate`, `shared_mode`, `_apply`, `_write_back_mode`, `_stamp_mode_change`, `_record_mode` |
| M2 | The gate reads `elapsed >= hysteresis` on the monotonic clock. A timer re-renders the entities at the hold's end and decides nothing; another wakes the plan for a flip the hold deferred. | `_mode_change_elapsed`, `_arm_hold_end_timer`, `_sync_dwell_timer` |
| M3 | A person's pick is never gated. A changed direction stamps the hold at once. An apply already running recorded its direction before it sent any head, so the pick is the newer record. | `async_select_option`, `async_select_shared_mode` |
| M4 | With the gate shut, the mode stays, whatever is asked, at any rank. A head wanting the other mode parks at its idle action. | `shared_mode`, `head_action` |
| M5 | A change only hands-off votes made starts no hold. Not in this spec: `verification/modehold/MXZModeHold.tla` covers it. | `arbitrate`, `_write_back_mode` |
| M6 | Once a coordinated room runs in a mode votes chose, the mode is MXZ's and the hold starts. Also in `MXZModeHold.tla`. | `_write_back_mode` |

## Changes made at a head

In the spec for room 1's setpoint (`WithAdopt`; the enable is not:
[README](README.md#what-is-modeled-and-what-isnt)). A second writer of a room's target and
enable, kept apart from MXZ's writes to the head. On by default; with **Accept changes
made at the head** off the machine stays in `Watching`, and a change at a head is written over.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Watching
    Watching --> Holding : A1 a report moves the mode or setpoint, and MXZ's writes can't explain it
    Holding --> Holding : A2 another such report starts the hold over
    Holding --> Watching : A3 it goes back, a gate closes, or the room is changed in Home Assistant
    Holding --> Adopted : A4 it held for ADOPT_DELAY
    Adopted --> Watching : A5 the plan puts the head on its mode, with the new target
```

| Id | What happens | Code |
| --- | --- | --- |
| A1 | Every report passes MXZ's older writes, then is read against what is left. A write is remembered as it goes out. | `_on_head_change`, `_watch_head_change`, `_call_head_service`, `_remember_head_write`, `_head_writes_caught_up`, `wrote`, `caught_up` |
| A2 | While the hold runs, `_apply` writes nothing to that head, and a command of MXZ's still waiting for the head is disowned. | `_head_change_settling`, `_apply`, `_arm_or_cancel`, `_disown_waiting_command` |
| A3 | Eco, a standby hold, Coordinator off, a start, a pause, a floor run, a hands-off room's disablement or a stale cloud report: the change counts for nothing. A head report that ends the hold this way, the head put back by hand say, asks for a compute, because the apply skipped that head and A2 disowned its waiting command. | `_head_adoption`, `drop_head_change`, `_watch_head_change`, `_on_head_change` |
| A4 | The room's own number and switch take the change, held to the room's bounds. What the head shows then replaces what it last showed in the memory. | `_on_heal_timer`, `_async_adopt_head_change`, `_head_change_seen`, `seen` |
| A5 | The next compute and apply. The head's heat or cool never reaches the shared mode. | `_apply` |

## A room's run

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Satisfied
    Satisfied --> Engaged : R1 past Allowed drift
    Engaged --> Satisfied : R2 reaches its target
    Engaged --> Satisfied : R3 paused, or in Dry
    Satisfied --> Engaged : R4 a start finds the head running
```

| Id | What happens | Code |
| --- | --- | --- |
| R1 | A room starts a run past its drift. Its call for the shared mode comes only past the larger Mode switch threshold. | `engage_with_latch`, `room_call` |
| R2 | The run lasts to the target, not to the drift. While engaged in the shared mode it keeps Resting mode waiting (`running`). | `engage_with_latch`, `arbitrate` |
| R3 | A paused room has no call, no run and an empty latch. Its run starts afresh on resume. | `_compute` |
| R4 | A restart seeds the latch from the head's mode at the first compute. | `_compute` |

## A door or window (a room with the pause on)

```mermaid
stateDiagram-v2
    direction LR
    [*] --> closed
    closed --> opening : W1 reads open
    opening --> closed : W2 reads closed, or no reading
    opening --> paused : W3 open for the open delay
    closed --> paused : W3 open for the open delay
    paused --> closing : W4 reads closed
    closing --> paused : W5 reads open, or no reading
    closing --> closed : W6 closed for the close delay
    paused --> overridden : W7 a change on the head, or the switch
    closing --> closing : W8 a change on the head ends at the next compute
    opening --> overridden : W9 the switch, with the window open
    closed --> overridden : W9 the switch, with the window open
    overridden --> closed : W10 reads closed
    overridden --> paused : W11 the switch off, with the window open
```

| Id | What happens | Code |
| --- | --- | --- |
| W1, W2, W3 | The delay counts from the reading, or from this coordinator's start if that is later. A start can lengthen a delay, never shorten it. No reading never starts a pause. | `OpeningPause.step`, `opening_state`, `opening_reading` |
| W3 | Entering the pause: the head goes straight to off, with no coil-dry run and no fan, setpoint or vane write. A speed picked on the off head is read and held. The state is stored at once. | `OpeningPause._transition`, `_apply`, `_observe_paused_fan`, `OpeningPause._save` |
| W4, W5, W6 | A closed reading counts down the close delay. Opening again starts it over. No reading doesn't end a pause. | `opening_state`, `opening_due` |
| W7 | A real mode change on a paused head, to anything but off, not MXZ's own, takes the room over at once. So does the Window Override switch. The room is coordinated normally: MXZ doesn't turn the head back off. | `_on_head_change`, `OpeningPause.take_over`, `OpeningPause.async_set_override` |
| W8 | With the window already closed and the close delay running, a change on the head is recorded, and the closed reading ends it at the next compute: the room goes on closing and the head is sent off again. This is the documented "until the window closes", at its edge. | `_on_head_change`, `OpeningPause.step` |
| W9 | The switch with the window open is an override with no pause to end. With no reading, it leaves the room closed and waits: the next open reading is overridden. | `OpeningPause.async_set_override`, `opening_state` |
| W10 | A closed reading ends an override at once, so the next opening pauses again. | `OpeningPause.step` |
| W11 | Turning the switch off with the window open counts the window again: paused once it has been open for the open delay, opening until then. | `OpeningPause.async_set_override` |

## A head's fan

A hold ends only on a gesture: the Fan auto switch, or an observed `auto`.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Unseeded : no command memory
    Unseeded --> Driving : F1 reads auto, or its own speed
    Unseeded --> Held : F2 reads any other speed
    Driving --> Driving : F3 a write, or its echo
    Driving --> Held : F4 reads a speed outside the last two
    Driving --> Driving : F5 known exception, a pick of one of the last two
    Held --> Held : F6 moved to another speed
    Held --> Driving : F7 reads auto, or Fan auto on
    Driving --> Held : F8 Fan auto off
    Driving --> Pending : F9 a start, not held
    Held --> Pending : F9 a start, held
    Pending --> Held : F10 held, or a speed that can't be ours
    Pending --> Driving : F11 auto, or a speed read as ours
```

| Id | What happens | Code |
| --- | --- | --- |
| F1, F2 | With no memory, `auto` sets the baseline. A speed the ladder would stay on is adopted (a hold placed there is lost: documented), a fan_only idle on an established entry reads as ours (#25), and anything else seeds a hold. | `_observe_fan_latch`, `_seed_matches_boost`, `_seed_is_own_idle` |
| F3 | Boost picks a rung and writes it once. A rewrite of the commanded token doesn't roll the memory (#14). | `_apply_fan`, `fan_for_delta`, `_write_fan` |
| F4 | A speed in neither `_fan_cmd` nor `_fan_prev`, and not in `_fan_older` either, is a person's. MXZ makes no fan write while held. | `_observe_fan_latch`, `_hold_at` |
| F5 | One of the two remembered speeds reads as MXZ's echo: not held, and boost writes over it. So does an older write, until the head's reports pass it (F1). A pick of the speed showing can't be read at all. | `_observe_fan_latch`, `_remember_fan_write`, `_fan_caught_up` |
| F6 | A hold's memory is the held speed, so any other speed moves it. | `_hold_at` |
| F7, F8 | Fan auto on adopts the showing speed and hands it back. Fan auto off holds it. At `auto`, off does nothing. | `async_set_fan_auto`, `_adopt_fan_speed` |
| F9 | The switch restores the held or not-held truth and, if not held, the remembered speeds with any older writes, limited to what boost could write. | `MXZZoneFanAutoSwitch.async_added_to_hass`, `restore_fan_hold` |
| F10, F11 | The first speed read settles it. Held stays held at that speed. Not held: a speed boost could never write holds; one in memory, or any in-ceiling rung with no memory or in a fan_only idle, is ours; otherwise it conservatively holds. | `_reconcile_restored_hold` |

## An apply's commands

One per head, in `ci-apply` (`WithSplit`). The mode is already recorded when the first runs.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Queued : D1 the apply wants an action for this head
    Queued --> Queued : D2 another plan, a gesture or a change at the head supersedes it
    Queued --> Done : D3 the head takes it, or refuses it
    Queued --> Done : D4 the coordinator retires, or Coordinator turns off
    Done --> [*]
```

| Id | What happens | Code |
| --- | --- | --- |
| D1 | One task per head, which waits for the head's lock. The apply records the mode, then waits until every head is done or a new input comes in. | `_queue_delivery`, `_write_back_mode`, `_wait_deliveries` |
| D2 | A superseded command issues nothing more. A change at the head that will be adopted disowns it. | `_delivery_owned`, `_disown_waiting_command` |
| D3 | Whatever a head's handler raises is that room's: its row reads rejected and the next update tries again. Only a fault in MXZ's own steps fails the plan. | `_deliver_head`, `_call_head_service`, `_finish_delivery` |
| D4 | A retired coordinator's command sends nothing more; what it sent stays sent. | `_retire_deliveries` |

## Lifecycle

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Running : first setup
    Running --> Running : L1 reload
    Running --> Down : L2 stop, restore data written
    Running --> Down : L3 crash, restore data as last dumped
    Down --> Running : L4 start
```

| Id | What happens | Code |
| --- | --- | --- |
| L1, L2 | The Fan auto switch's state goes to restore data at a reload or a stop, and every 15 minutes by Home Assistant. | `extra_restore_state_data`, `fan_command_memory` |
| L3 | The mode record and the door/window store were written at every change, so they survive. The Fan auto truth is as last dumped. | `_record_mode`, `OpeningPause._save` |
| L4 | The record gives the mode, whether votes set it, and what is left of the hold; the store gives each room's door/window state and override; each room resumes from its head. | `async_load_mode_record`, `mode_change_stamp`, `restore_shared_mode`, `async_load_openings`, `OpeningRecord.restored` |
