--------------------------- MODULE MXZCoordinator ---------------------------
(***************************************************************************)
(* The coordinator's cool/heat arbitration, the hold between mode changes, *)
(* the door/window pause, the manual fan hold, and what comes back after a *)
(* reload, restart or crash, as one machine.                               *)
(*                                                                         *)
(* docs/formal/STATECHART.md is the same machine as a chart, and           *)
(* docs/formal/README.md says what is and isn't modeled. Every action      *)
(* names the code it stands for, under custom_components/mxz_coordinator/. *)
(* verification/modehold/MXZModeHold.tla goes deeper on one part: hands-   *)
(* off votes and wall-clock steps across restarts. Neither is repeated     *)
(* here.                                                                   *)
(*                                                                         *)
(* Decide is one MXZCoordinator._compute plus its _apply, taken as a step. *)
(* With WithSplit, Decide is the compute and the mode's record, with no    *)
(* await between them as in _apply, and each head's command is its own     *)
(* later step (Deliver, Refuse), so inputs and starts can land between.    *)
(* WithAdopt adds room 1's setpoint as the head shows it and the changes   *)
(* made at the head (Accept changes made at the head).                     *)
(* Time is whole ticks on one clock. Rooms are in priority order, enabled, *)
(* with Allowed drift 1 and Mode switch threshold 3 as multiples of one    *)
(* reading position (see Demand and Engage). Room 1 is the room with the   *)
(* door/window and the fan under study: FanHold's rung, latch and echo     *)
(* memory (FanEcho) and the door/window records (OpeningPause) are         *)
(* per head and never read another head's.                                 *)
(***************************************************************************)
EXTENDS Integers, Sequences

CONSTANTS
    H,            \* mode_hysteresis, ticks (> 0)
    MaxReal,      \* the model ends time here
    Rooms,        \* coordinated rooms
    Readings,     \* the reading positions a room can be at (see Demand and Engage)
    Resting,      \* resting_mode_bias: "last", "cool" or "heat"
    OpenDelay,    \* zone.opening_open_delay, ticks
    CloseDelay,   \* zone.opening_close_delay, ticks
    FanMaxIdx,    \* the ladder index of fan_boost_max: rungs above it are never written
    WithFan,      \* a person can touch room 1's fan
    WithLag,      \* room 1's head reports a fan write later, not at once
    WithWindow,   \* room 1 has the door/window pause
    WithSelect,   \* a person picks the shared mode
    WithReload,   \* a config entry reload
    WithRestart,  \* Home Assistant stops, a person may touch the fan, it starts
    WithCrash,    \* Home Assistant dies without its stop
    WithSplit,    \* the heads' commands are steps after the decision, and can be refused
    WithAdopt,    \* room 1's setpoint, a lagging head, and changes made at the head
    QueueMax      \* WithAdopt: MXZ's setpoint writes a head can be behind by

None == "none"
\* "No time": no record, no stamp. Outside every time the model reaches.
NoTime == -1000
NoIdx == -1
Modes == {"cool", "heat"}
RoomIds == 1..Rooms
\* What a config's Readings can be (a .cfg file can't write a negative number).
AllReadings == {-3, -2, -1, 1, 2, 3}
CoolingReadings == {1, 2, 3}
FarReadings == {-3, -1, 1, 3}
FarCooling == {3}
Rungs == <<"r0", "r1", "r2">>    \* FAN_LADDER, cut to three rungs
RungSet == {"r0", "r1", "r2"}
Tokens == {"auto"} \cup RungSet
RungIdx(t) == CASE t = "r0" -> 0 [] t = "r1" -> 1 [] t = "r2" -> 2
Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b

\* What a door/window reads, and the states of logic.opening_state.
Closed == "closed"     Opening == "opening"     Paused == "paused"
Closing == "closing"   Overridden == "overridden"
OpeningStates == {Closed, Opening, Paused, Closing, Overridden}
PausedStates == {Paused, Closing}   \* const.OPENING_PAUSED_STATES

\* A person's standing fan pick, by how the code can read it.
\*   strict    the rule binds with no exception
\*   echo      one of the last two speeds commanded, or an older write while the head
\*             hasn't reported the last one (the known exception)
\*   outage    made before MXZ read it, across a stop or before the first read
\*   crash     a crash lost the hold's restore data
\*   seed      made with no command memory (the seed's documented adoption)
\*   inflight  a Fan auto off with a write of MXZ's already sent, which can still land
PickKinds == {"strict", "echo", "outage", "crash", "seed", "inflight"}

\* WithAdopt: setpoints as three values. A person sets 1 or 2 as the room's target in
\* Home Assistant; 3 is pressed only at the head, so a target of 3 was adopted.
Targets == {1, 2}
Setpoints == {1, 2, 3}
WriteKinds == {"mxz", "start", "echo", "seen"}

VARIABLES
    real, boot,         \* true time, this coordinator's start
    pos,                \* each room's reading: see Demand, Engage, FanDelta
    latch,              \* coordinator._engage_latch: "", "cool" or "heat"
    head,               \* each head's hvac mode
    mode,               \* coordinator.current_shared_mode
    stamp,              \* coordinator._last_mode_change_ts, or NoTime
    storedMode,         \* the Store record's mode, or None
    storedStamp,        \* the Store record's change time, or NoTime
    restoredOn,         \* coordinator._restored_coordinator_on
    \* Room 1's fan.
    fanShown,           \* the fan_mode its head reports
    fanSent,            \* a write the head hasn't reported yet, or None (WithLag)
    fanCmd, fanPrev,    \* FanEcho.last, FanEcho.prior, or None
    fanOlder,           \* FanEcho.older, as a set: it never repeats a speed
    fanIdx,             \* FanHold._idx, or NoIdx
    fanLatched,         \* FanHold._latched
    fanRestore,         \* FanHold._restore: None, "held" or "free"
    saved,              \* the Fan auto switch's restore data; has = FALSE: none yet
    \* Room 1's door/window (openings.OpeningPause).
    winRead,            \* "open", "closed" or None (unavailable)
    winChanged,         \* when that reading began
    winState,           \* record["state"]
    winOverride,        \* record["override"]: None, "switch" or "manual"
    storedWin,          \* the store's state
    storedOverride,     \* the store's override
    \* Ghosts: what the world knows, not the code.
    lastChange,         \* when the last real mode change happened, or NoTime
    decided,            \* the step just taken was a Decide
    pick, pickKind,     \* a person's standing pick (None: none), and its kind
    pickSeen,           \* a Decide has read it
    gesture,            \* a person has touched the fan since it last read auto
    flipInDwell,        \* a Decide changed the mode inside the hold
    flipLate,           \* a Decide held the mode with the hold over and only the other mode called
    fanOverwritten,     \* MXZ wrote over a strict pick
    exceptionLost,      \* MXZ wrote over a pick the exceptions cover
    falseHold,          \* a live read latched with no gesture, the head at most one write behind
    twoBehind,          \* MXZ sent a second, different fan before the head reported the first
    falseHoldTwoBehind, \* the same latch with the head two or more writes behind (F1, fixed)
    restoreBroken,      \* a start brought back something other than what was running
    pauseBroken,        \* a paused room's head got a fan write, or its call counted
    overrideBroken,     \* a live override was parked or dropped with the window not closed
    \* WithSplit: the apply in flight (_apply's queued deliveries).
    phase,              \* "idle", or "applying" while a command is still to go
    want,               \* each head's command in the apply (logic.head_action)
    left,               \* the heads whose command hasn't run yet
    refused,            \* the heads whose command raised in the last apply
    fresh,              \* no input since the decision: the apply is current
    picked,             \* ghost: a person picked a mode since the decision
    trailing,           \* ghost: a head was sent a direction the record doesn't hold
    cutShort,           \* ghost: a start landed inside an apply
    \* WithAdopt: room 1's setpoint.
    target,             \* the room's target (its number entity)
    spShown,            \* the setpoint the head shows
    spQueue,            \* MXZ's writes the head hasn't shown yet, oldest first, with ghost kinds
    spMem,              \* logic.HeadWriteMemory for it, in hass.data: oldest first
    holding,            \* the adopt hold is armed (_heal_timers "adopt")
    quiet,              \* coordinator._startup_quiet
    shownKind,          \* ghost: what put spShown there: "person", or a queued write's kind
    adopted,            \* ghost: the kinds of MXZ's own writes the target took
    writeInHold         \* ghost: MXZ wrote room 1's setpoint while a hold counted

splitVars == <<phase, want, left, refused, fresh, picked, trailing, cutShort>>
adoptVars == <<target, spShown, spQueue, spMem, holding, quiet, shownKind, adopted, writeInHold>>
fanVars == <<fanShown, fanSent, fanCmd, fanPrev, fanOlder, fanIdx, fanLatched, fanRestore, saved>>
winVars == <<winRead, winChanged, winState, winOverride, storedWin, storedOverride>>
ghosts == <<lastChange, decided, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
            fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, twoBehind, falseHoldTwoBehind, restoreBroken,
            pauseBroken, overrideBroken>>
vars == <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
          fanVars, winVars, ghosts, splitVars, adoptVars>>

\* logic.room_call: a demand past the threshold, band = max(threshold, drift).
Demand(p) == IF p = 3 THEN "cool" ELSE IF p = -3 THEN "heat" ELSE None

\* logic.engage_with_latch: engaged runs to the target, else starts past drift.
Engage(prior, p) ==
    IF prior \in Modes
    THEN LET raw == IF p > 0 THEN "cool" ELSE "heat"
         IN IF raw = prior THEN prior ELSE ""
    ELSE IF p >= 2 THEN "cool" ELSE IF p <= -2 THEN "heat" ELSE ""

\* logic.shared_mode with no votes, through logic.arbitrate's `running`: a room
\* engaged in the shared mode keeps Resting mode waiting.
SharedMode(ds, current, allowed, running) ==
    LET rest == IF Resting \in Modes /\ ~running THEN Resting ELSE current
        anyCool == \E k \in 1..Len(ds) : ds[k] = "cool"
        anyHeat == \E k \in 1..Len(ds) : ds[k] = "heat"
        standoff == anyCool /\ anyHeat
        proposed == IF standoff THEN ds[1]
                    ELSE IF anyCool THEN "cool"
                    ELSE IF anyHeat THEN "heat"
                    ELSE rest
        still == (current = "cool" /\ anyCool) \/ (current = "heat" /\ anyHeat)
    IN IF proposed # current /\ allowed /\ (standoff \/ ~still)
       THEN proposed ELSE current

\* logic.opening_state, branch for branch.
OpeningState(raw, age, prior, override) ==
    LET wasPaused == prior \in PausedStates
    IN  IF raw = "closed"
        THEN IF wasPaused /\ ~override
             THEN IF age >= CloseDelay THEN Closed ELSE Closing
             ELSE Closed
        ELSE IF override
        THEN IF raw = "open" \/ wasPaused \/ prior = Overridden THEN Overridden ELSE Closed
        ELSE IF raw = "open"
        THEN IF wasPaused THEN Paused
             ELSE IF age >= OpenDelay THEN Paused ELSE Opening
        ELSE IF wasPaused THEN Paused ELSE Closed

\* logic.range_delta in tenths of a degree, at a representative temperature of
\* each reading position, against FAN_BOOST_UP_AT / FAN_BOOST_DOWN_AT.
FanDelta(p) == CASE p \in {1, -1} -> 7 [] p \in {2, -2} -> 22 [] p \in {3, -3} -> 40
UpAt(i) == CASE i = 0 -> 10 [] i = 1 -> 20
DownAt(i) == CASE i = 0 -> 5 [] i = 1 -> 15

\* logic.fan_for_delta.
RECURSIVE StepUp(_, _)
StepUp(delta, i) ==
    IF i < 2 /\ i < FanMaxIdx /\ delta >= UpAt(i) THEN StepUp(delta, i + 1) ELSE i
RECURSIVE StepDown(_, _)
StepDown(delta, i) ==
    IF i > 0 /\ delta < DownAt(i - 1) THEN StepDown(delta, i - 1) ELSE i
FanFor(delta, i) == Min(StepDown(delta, StepUp(delta, i)), FanMaxIdx)

Memory == [cmd |-> fanCmd, prev |-> fanPrev, older |-> fanOlder, idx |-> fanIdx, latched |-> fanLatched]

\* FanHold._hold_at
HoldAt(f, o) == [f EXCEPT !.latched = TRUE, !.prev = o, !.cmd = o, !.older = {}]
\* FanHold._adopt; `i` NoIdx resumes from the ceiling, as the switch does.
Adopt(f, o, i) ==
    [f EXCEPT !.latched = FALSE, !.prev = IF f.cmd = None THEN o ELSE f.cmd, !.cmd = o, !.older = {},
              !.idx = IF i = NoIdx THEN FanMaxIdx ELSE i]
\* The first branch of FanHold._observe_latch and of FanHold._reconcile_restored_hold.
AtAuto(f) ==
    [f EXCEPT !.latched = FALSE,
              !.prev = IF f.cmd = None THEN "auto" ELSE f.prev,
              !.cmd = IF f.cmd = None THEN "auto" ELSE f.cmd]
InCeiling(t) == t \in RungSet /\ RungIdx(t) <= FanMaxIdx

\* FanHold._reconcile_restored_hold
Reconcile(f, o, restoredHeld, act) ==
    IF o = "auto" THEN [f |-> AtAuto(f), held |-> FALSE]
    ELSE IF restoredHeld THEN [f |-> HoldAt(f, o), held |-> TRUE]
    ELSE LET remembered == o \in {f.cmd, f.prev} \cup f.older
             ours == InCeiling(o) /\ (f.cmd = None \/ remembered \/ act = "fan_only")
         IN IF ~ours THEN [f |-> HoldAt(f, o), held |-> TRUE]
            ELSE LET g == [f EXCEPT !.latched = FALSE,
                                    !.prev = IF remembered THEN f.prev ELSE o,
                                    !.cmd = IF remembered THEN f.cmd ELSE o,
                                    !.older = IF remembered THEN f.older ELSE {}]
                     h == IF act \in Modes /\ o \in RungSet
                          THEN [g EXCEPT !.idx = Min(RungIdx(o), FanMaxIdx)] ELSE g
                 IN [f |-> h, held |-> FALSE]

\* FanHold._seed_is_own_idle (idle action fan_only, an entry past its first minutes).
SeedIsOwnIdle(o, act) == act = "fan_only" /\ restoredOn /\ InCeiling(o)
\* FanHold._seed_matches_boost
SeedMatchesBoost(o, act, delta) ==
    act \in Modes /\ InCeiling(o) /\ FanFor(delta, RungIdx(o)) = RungIdx(o)

\* FanHold._observe_latch (fan_on_pending is not modeled: the head always
\* reports a speed here).
Observe(f0, o, act, delta) ==
    \* FanEcho.caught_up: a report of the last command ends the lag.
    LET f == IF o = f0.cmd THEN [f0 EXCEPT !.older = {}] ELSE f0 IN
    IF fanRestore # None THEN Reconcile(f, o, fanRestore = "held", act)
    ELSE IF o = "auto" THEN [f |-> AtAuto(f), held |-> FALSE]
    ELSE IF f.cmd = None
    THEN IF SeedIsOwnIdle(o, act)
         THEN [f |-> [f EXCEPT !.latched = FALSE, !.prev = o, !.cmd = o], held |-> FALSE]
         ELSE IF SeedMatchesBoost(o, act, delta)
         THEN [f |-> Adopt(f, o, RungIdx(o)), held |-> FALSE]
         ELSE [f |-> HoldAt(f, o), held |-> TRUE]
    ELSE IF o \notin {f.cmd, f.prev} \cup f.older
    THEN [f |-> HoldAt(f, o), held |-> TRUE]
    ELSE [f |-> f, held |-> f.latched]

\* FanHold.async_apply on what FanHold._observe_latch left; `token` is None for no write.
ApplyFan(act, delta) ==
    LET ob == Observe(Memory, fanShown, act, delta)
        g == ob.f
    IN  IF ob.held THEN [f |-> g, token |-> None]
        ELSE IF act \in Modes
        THEN LET i == FanFor(delta, IF g.idx = NoIdx THEN 0 ELSE g.idx)
                 t == Rungs[i + 1]
             IN [f |-> [g EXCEPT !.idx = i], token |-> IF t = fanShown THEN None ELSE t]
        ELSE IF act = "fan_only"
        THEN [f |-> [g EXCEPT !.idx = NoIdx], token |-> IF fanShown = "auto" THEN None ELSE "auto"]
        ELSE [f |-> [g EXCEPT !.idx = NoIdx], token |-> None]

\* FanEcho.wrote: a rewrite of the token already commanded rolls nothing.
Rolled(f, t) ==
    IF t = None \/ t = f.cmd THEN f
    ELSE [f EXCEPT !.prev = IF f.cmd = None THEN t ELSE f.cmd, !.cmd = t,
                   !.older = (f.older \cup ({f.prev} \cap RungSet)) \ {t, f.cmd}]

\* FanHold.auto_is_on
FanAutoOn == IF fanRestore # None THEN fanRestore = "free" ELSE ~fanLatched

\* What the Fan auto switch writes as restore data (switch.extra_state_attributes).
Dumped == [has |-> TRUE, held |-> ~FanAutoOn, last |-> fanCmd, prior |-> fanPrev, older |-> fanOlder]
NoDump == [has |-> FALSE, held |-> FALSE, last |-> None, prior |-> None, older |-> {}]

\* switch.py's restore eligibility in FanHold.restore.
Eligible(t) == IF InCeiling(t) THEN t ELSE "auto"

\* The mode a start would read from the record (restore_shared_mode, no restore data).
Recorded == IF storedMode = None THEN "cool" ELSE storedMode

\* logic.HeadWriteMemory: own, seen, wrote and caught_up.
Own(mem, v) == \E i \in 1..Len(mem) : mem[i] = v
Seen(mem, v) == IF Len(mem) = 0 THEN <<v>> ELSE <<v>> \o Tail(mem)
Wrote(mem, v, shown) ==
    LET m == IF Own(mem, shown) THEN mem ELSE Seen(mem, shown)
    IN IF Len(m) > 0 /\ m[Len(m)] = v THEN m ELSE Append(m, v)
RECURSIVE CaughtUp(_, _)
CaughtUp(mem, v) == IF ~Own(mem, v) \/ mem[1] = v THEN mem ELSE CaughtUp(Tail(mem), v)

\* The queued writes the memory no longer holds, filed under what dropped them.
Orphan(queue, mem, cause) ==
    [i \in 1..Len(queue) |-> IF queue[i].kind = "mxz" /\ ~Own(mem, queue[i].v)
                              THEN [queue[i] EXCEPT !.kind = cause] ELSE queue[i]]

TypeOK ==
    /\ real \in 0..MaxReal /\ boot \in 0..MaxReal
    /\ pos \in [RoomIds -> Readings]
    /\ latch \in [RoomIds -> {""} \cup Modes]
    /\ head \in [RoomIds -> {"off", "fan_only"} \cup Modes]
    /\ mode \in Modes /\ storedMode \in {None} \cup Modes
    /\ fanShown \in Tokens /\ fanSent \in {None} \cup Tokens
    /\ fanCmd \in {None} \cup Tokens /\ fanPrev \in {None} \cup Tokens
    /\ fanOlder \subseteq RungSet
    /\ fanIdx \in {NoIdx} \cup 0..2 /\ fanLatched \in BOOLEAN
    /\ fanRestore \in {None, "held", "free"}
    /\ winRead \in {"open", "closed", None}
    /\ winState \in OpeningStates /\ storedWin \in OpeningStates
    /\ winOverride \in {None, "switch", "manual"}
    /\ storedOverride \in {None, "switch", "manual"}
    /\ pick \in {None} \cup Tokens /\ pickKind \in {None} \cup PickKinds
    /\ phase \in {"idle", "applying"} /\ left \subseteq RoomIds /\ refused \subseteq RoomIds
    /\ want \in [RoomIds -> {"off", "fan_only"} \cup Modes]
    /\ target \in Setpoints /\ spShown \in Setpoints /\ Len(spQueue) <= QueueMax
    /\ shownKind \in WriteKinds \cup {"person"} /\ adopted \subseteq WriteKinds

Init ==
    /\ real = 0 /\ boot = 0
    /\ pos \in [RoomIds -> Readings]
    /\ latch = [r \in RoomIds |-> ""]
    /\ head = [r \in RoomIds |-> "off"]
    /\ mode = "cool"                      \* a fresh install, _arbitration_mode
    /\ stamp = NoTime /\ storedStamp = NoTime /\ storedMode = None
    /\ restoredOn = FALSE
    /\ fanShown = "auto" /\ fanSent = None /\ fanCmd = None /\ fanPrev = None /\ fanOlder = {}
    /\ fanIdx = NoIdx /\ fanLatched = FALSE /\ fanRestore = None /\ saved = NoDump
    /\ winRead = "closed" /\ winChanged = 0 /\ winState = Closed /\ winOverride = None
    /\ storedWin = Closed /\ storedOverride = None
    /\ lastChange = NoTime /\ decided = FALSE
    /\ pick = None /\ pickKind = None /\ pickSeen = FALSE /\ gesture = FALSE
    /\ flipInDwell = FALSE /\ flipLate = FALSE
    /\ fanOverwritten = FALSE /\ exceptionLost = FALSE /\ falseHold = FALSE
    /\ twoBehind = FALSE /\ falseHoldTwoBehind = FALSE
    /\ restoreBroken = FALSE /\ pauseBroken = FALSE /\ overrideBroken = FALSE
    /\ phase = "idle" /\ want = head /\ left = {} /\ refused = {} /\ fresh = TRUE
    /\ picked = FALSE /\ trailing = FALSE /\ cutShort = FALSE
    /\ target = 1 /\ spShown = 1 /\ spQueue = <<>> /\ spMem = <<>> /\ holding = FALSE
    /\ quiet = FALSE /\ shownKind = "mxz" /\ adopted = {} /\ writeInHold = FALSE

(***************************************************************************)
(* Decide: MXZCoordinator._compute then _apply.                            *)
(*   _compute : each room's demand and engage (room_call,                  *)
(*              engage_with_latch); OpeningPause.step (a closed reading    *)
(*              ends an override; logic.opening_state; saved at once); a   *)
(*              paused room has no demand, no engage and an empty latch;   *)
(*              then logic.arbitrate with `allowed` and `running`.         *)
(*   _apply   : a paused head goes to off with its fan only read           *)
(*              (FanHold.read); any other head gets                        *)
(*              logic.head_action, then FanHold.async_apply; a changed     *)
(*              mode is stamped (_stamp_mode_change) and stored            *)
(*              (_record_mode).                                            *)
(* Idle action is fan_only; eco, standby, floors, Dry and lost heads are   *)
(* out of this model.                                                      *)
(***************************************************************************)
Decide ==
    LET override0 == IF winRead = "closed" THEN None ELSE winOverride
        newWin == IF WithWindow
                  THEN OpeningState(winRead, real - Max(winChanged, boot), winState, override0 # None)
                  ELSE Closed
        isPaused == newWin \in PausedStates
        raw == [r \in RoomIds |-> Engage(latch[r], pos[r])]
        nl == [r \in RoomIds |-> IF r = 1 /\ isPaused THEN "" ELSE raw[r]]
        dem == [r \in RoomIds |-> IF r = 1 /\ isPaused THEN None ELSE Demand(pos[r])]
        allowed == stamp = NoTime \/ real - stamp >= H
        running == \E r \in RoomIds : nl[r] = mode
        cmd == SelectSeq([k \in 1..Rooms |-> dem[k]], LAMBDA d : d # None)
        state == SharedMode(cmd, mode, allowed, running)
        changed == state # mode
        act1 == IF isPaused THEN "off" ELSE IF nl[1] = state THEN state ELSE "fan_only"
        fan == IF ~WithFan THEN [f |-> Memory, token |-> None]
               ELSE IF isPaused
                    THEN LET ob == Observe(Memory, fanShown, "off", 0)
                         IN [f |-> ob.f, token |-> None]
                    ELSE ApplyFan(act1, FanDelta(pos[1]))
        written == fan.token # None
        rolled == Rolled(fan.f, fan.token)
        lands == written /\ ~WithLag
        overwritesPick == written /\ pick # None /\ fan.token # pick
        strict == pickKind = "strict"
        \* Ghost oracles, from true time and the rules' own sentences.
        tooSoon == lastChange # NoTime /\ real - lastChange < H
        hasCalled == Len(cmd) > 0
        over == lastChange = NoTime \/ real - lastChange >= H
        first == IF hasCalled THEN cmd[1] ELSE None
        otherCalls == hasCalled /\ first # mode
        newHold == rolled.latched /\ ~fanLatched /\ ~gesture /\ fanRestore = None /\ fanCmd # None
        \* A start's first read of a head still behind. A speed boost could never write is the
        \* README's conservative hold (S11b), so it isn't one of these.
        startHoldBehind == rolled.latched /\ ~fanLatched /\ ~gesture /\ fanRestore = "free"
                           /\ fanCmd # None /\ twoBehind /\ InCeiling(fanShown)
        \* logic.head_action for each head.
        acts == [r \in RoomIds |->
                  IF r = 1 /\ isPaused THEN "off"
                  ELSE IF nl[r] = state THEN state ELSE "fan_only"]
    IN
    /\ phase = "idle"
    /\ latch' = nl
    /\ head' = IF WithSplit THEN head ELSE acts
    /\ want' = IF WithSplit THEN acts ELSE want
    /\ left' = IF WithSplit THEN {r \in RoomIds : acts[r] # head[r]} ELSE {}
    /\ phase' = IF left' = {} THEN "idle" ELSE "applying"
    /\ refused' = {} /\ fresh' = TRUE /\ picked' = FALSE
    /\ mode' = state
    /\ stamp' = IF changed THEN real ELSE stamp
    /\ storedMode' = IF changed THEN state ELSE storedMode
    /\ storedStamp' = IF changed THEN real ELSE storedStamp
    /\ fanShown' = IF lands THEN fan.token ELSE fanShown
    /\ fanSent' = IF written /\ WithLag THEN fan.token ELSE fanSent
    /\ fanCmd' = rolled.cmd /\ fanPrev' = rolled.prev
    /\ fanOlder' = IF lands THEN {} ELSE rolled.older
    /\ fanIdx' = rolled.idx /\ fanLatched' = rolled.latched
    /\ fanRestore' = IF WithFan THEN None ELSE fanRestore
    /\ winState' = newWin /\ winOverride' = override0
    /\ storedWin' = newWin /\ storedOverride' = override0
    /\ lastChange' = IF changed THEN real ELSE lastChange
    /\ decided' = (left' = {})
    /\ pick' = IF overwritesPick THEN None ELSE pick
    /\ pickKind' = IF overwritesPick THEN None ELSE pickKind
    /\ pickSeen' = IF WithFan THEN pick # None ELSE pickSeen
    /\ flipInDwell' = (flipInDwell \/ (changed /\ tooSoon))
    /\ flipLate' = (flipLate \/ (~changed /\ over /\ otherCalls))
    /\ fanOverwritten' = (fanOverwritten \/ (overwritesPick /\ strict))
    /\ exceptionLost' = (exceptionLost \/ (overwritesPick /\ ~strict))
    /\ falseHold' = (falseHold \/ (newHold /\ ~twoBehind))
    /\ falseHoldTwoBehind' = (falseHoldTwoBehind \/ (newHold /\ twoBehind) \/ startHoldBehind)
    /\ twoBehind' = (fanSent' # None /\ (twoBehind \/ (written /\ fanSent # None /\ fan.token # fanSent)))
    /\ pauseBroken' = (pauseBroken \/ (isPaused /\ (written \/ acts[1] # "off"
                                       \/ (changed /\ Rooms = 1 /\ state # Resting))))
    /\ overrideBroken' = (overrideBroken \/ (winOverride # None /\ winRead # "closed"
                                             /\ (override0 = None \/ isPaused \/ acts[1] = "off")))
    /\ UNCHANGED <<real, boot, pos, restoredOn, saved, winRead, winChanged, restoreBroken, gesture>>
    /\ UNCHANGED <<trailing, cutShort, adoptVars>>

(***************************************************************************)
(* WithSplit: one head's command from the apply in flight, as              *)
(* DeliveryQueue._deliver runs it once the head's lock is free. The mode   *)
(* was recorded by the Decide that queued it (_write_back_mode runs before *)
(* DeliveryQueue.async_wait). Refuse is a head whose handler raised: since *)
(* DeliveryQueue._async_issue, whatever it raised is that room's alone,    *)
(* and the next update tries again. The apply is over once no command is   *)
(* left; it counts as a decision only if nothing came in since (`fresh`).  *)
(***************************************************************************)
Finish(r) ==
    /\ left' = left \ {r}
    /\ phase' = IF left' = {} THEN "idle" ELSE "applying"
    /\ decided' = (left' = {} /\ fresh)

Deliver == WithSplit /\ \E r \in left :
    /\ phase = "applying"
    /\ head' = [head EXCEPT ![r] = want[r]]
    /\ Finish(r)
    /\ trailing' = (trailing \/ (want[r] \in Modes /\ want[r] # Recorded /\ ~picked))
    /\ UNCHANGED <<real, boot, pos, latch, mode, stamp, storedMode, storedStamp, restoredOn, fanVars, winVars>>
    /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate, fanOverwritten,
                   exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>
    /\ UNCHANGED <<want, refused, fresh, picked, cutShort, adoptVars>>

Refuse == WithSplit /\ \E r \in left :
    /\ phase = "applying"
    /\ refused' = refused \cup {r}
    /\ Finish(r)
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn, fanVars, winVars>>
    /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate, fanOverwritten,
                   exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>
    /\ UNCHANGED <<want, fresh, picked, trailing, cutShort, adoptVars>>

\* The room's reading changes (a sensor reports).
Reading == \E r \in RoomIds, p \in Readings :
    /\ p # pos[r] /\ pos' = [pos EXCEPT ![r] = p]
    /\ decided' = FALSE
    /\ fresh' = IF phase = "applying" THEN FALSE ELSE fresh
    /\ UNCHANGED <<phase, want, left, refused, picked, trailing, cutShort, adoptVars>>
    /\ UNCHANGED <<real, boot, latch, head, mode, stamp, storedMode, storedStamp, restoredOn, fanVars, winVars>>
    /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

Tick ==
    /\ real < MaxReal /\ real' = real + 1
    /\ decided' = FALSE
    /\ UNCHANGED <<splitVars, adoptVars>>
    /\ UNCHANGED <<boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn, fanVars, winVars>>
    /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

\* select.async_select_option, then coordinator.async_select_shared_mode: a changed
\* direction stamps the hold and writes the record.
Select == WithSelect /\ \E m \in Modes :
    /\ m # mode
    /\ mode' = m /\ stamp' = real /\ storedMode' = m /\ storedStamp' = real
    /\ lastChange' = real /\ decided' = FALSE
    /\ fresh' = IF phase = "applying" THEN FALSE ELSE fresh
    /\ picked' = (picked \/ phase = "applying")
    /\ UNCHANGED <<phase, want, left, refused, trailing, cutShort, adoptVars>>
    /\ UNCHANGED <<real, boot, pos, latch, head, restoredOn, fanVars, winVars>>
    /\ UNCHANGED <<pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

\* A person picks a speed on the head (its remote: not a service call of MXZ's).
\* Picking the speed already showing changes nothing that can be read, so it is
\* not a step: FanHold._observe_latch's docstring accepts that.
PersonFan == WithFan /\ \E t \in Tokens :
    /\ fresh' = IF phase = "applying" THEN FALSE ELSE fresh
    /\ UNCHANGED <<phase, want, left, refused, picked, trailing, cutShort, adoptVars>>
    /\ t # fanShown
    /\ fanShown' = t /\ fanSent' = None
    /\ pick' = IF t = "auto" THEN None ELSE t
    /\ pickKind' = IF t = "auto" THEN None
                   ELSE IF fanRestore # None THEN "outage"
                   ELSE IF fanCmd = None THEN "seed"
                   ELSE IF ~fanLatched /\ t \in {fanCmd, fanPrev} THEN "echo"
                   ELSE IF ~fanLatched /\ fanShown # fanCmd /\ t \in fanOlder THEN "echo"
                   ELSE "strict"
    /\ pickSeen' = FALSE
    /\ gesture' = (t # "auto")
    /\ fanOlder' = IF t = fanCmd THEN {} ELSE fanOlder
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
                   fanCmd, fanPrev, fanIdx, fanLatched, fanRestore, saved, winVars>>
    /\ twoBehind' = FALSE
    /\ UNCHANGED <<lastChange, flipInDwell, flipLate, fanOverwritten, exceptionLost, falseHold, falseHoldTwoBehind,
                   restoreBroken, pauseBroken, overrideBroken>>

\* A lagging head reports the last fan MXZ sent it.
HeadReports ==
    /\ fresh' = IF phase = "applying" THEN FALSE ELSE fresh
    /\ UNCHANGED <<phase, want, left, refused, picked, trailing, cutShort, adoptVars>>
    /\ WithLag
    /\ fanSent # None
    /\ fanShown' = fanSent /\ fanSent' = None
    /\ pick' = IF pick = fanSent THEN pick ELSE None
    /\ pickKind' = IF pick = fanSent THEN pickKind ELSE None
    /\ fanOlder' = IF fanSent = fanCmd THEN {} ELSE fanOlder
    /\ twoBehind' = FALSE
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
                   fanCmd, fanPrev, fanIdx, fanLatched, fanRestore, saved, winVars>>
    /\ UNCHANGED <<lastChange, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

\* FanHold.async_set_auto(on=True): hand back and adopt the showing speed.
FanAutoOnGesture ==
    /\ fresh' = IF phase = "applying" THEN FALSE ELSE fresh
    /\ UNCHANGED <<phase, want, left, refused, picked, trailing, cutShort, adoptVars>>
    /\ WithFan
    /\ fanRestore' = None
    /\ LET a == Adopt(Memory, fanShown, NoIdx)
       IN fanCmd' = a.cmd /\ fanPrev' = a.prev /\ fanOlder' = a.older
          /\ fanIdx' = a.idx /\ fanLatched' = a.latched
    /\ pick' = None /\ pickKind' = None /\ pickSeen' = FALSE
    \* A write still unreported can land after this and read as a new speed.
    /\ gesture' = (fanSent # None)
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
                   fanShown, fanSent, saved, winVars>>
    /\ UNCHANGED <<lastChange, flipInDwell, flipLate, fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind,
                   restoreBroken, pauseBroken, overrideBroken>>

\* FanHold.async_set_auto(on=False): hold the showing speed. At auto, a no-op
\* (it only drops a pending restore, as its code does).
FanAutoOffGesture ==
    /\ fresh' = IF phase = "applying" THEN FALSE ELSE fresh
    /\ UNCHANGED <<phase, want, left, refused, picked, trailing, cutShort, adoptVars>>
    /\ WithFan
    /\ fanShown # "auto"
    /\ fanRestore' = None
    /\ fanPrev' = IF fanCmd = None THEN fanShown ELSE fanCmd
    /\ fanCmd' = fanShown /\ fanLatched' = TRUE /\ fanOlder' = {}
    /\ pick' = fanShown /\ pickSeen' = TRUE /\ gesture' = TRUE
    /\ pickKind' = IF fanSent # None THEN "inflight" ELSE "strict"
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
                   fanShown, fanSent, fanIdx, saved, winVars>>
    /\ UNCHANGED <<lastChange, flipInDwell, flipLate, fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind,
                   restoreBroken, pauseBroken, overrideBroken>>

\* HA writes restore data every 15 minutes (the periodic dump a crash falls back on).
Dump ==
    /\ WithCrash
    /\ UNCHANGED <<splitVars, adoptVars>>
    /\ saved' = Dumped
    /\ saved # Dumped
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
                   fanShown, fanSent, fanCmd, fanPrev, fanOlder, fanIdx, fanLatched, fanRestore, winVars>>
    /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

\* The door/window reads something else (OpeningPause.on_change's recompute
\* is a later Decide).
WindowReads == WithWindow /\ \E x \in {"open", "closed", None} :
    /\ fresh' = IF phase = "applying" THEN FALSE ELSE fresh
    /\ UNCHANGED <<phase, want, left, refused, picked, trailing, cutShort, adoptVars>>
    /\ x # winRead /\ winRead' = x /\ winChanged' = real
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn, fanVars,
                   winState, winOverride, storedWin, storedOverride>>
    /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

\* A person changes room 1's head mode. coordinator._on_head_change: a real change to
\* anything but off, on a paused room, is a takeover, saved at once.
PersonMode == WithWindow /\ \E m \in {"off", "fan_only"} \cup Modes :
    /\ fresh' = IF phase = "applying" THEN FALSE ELSE fresh
    /\ UNCHANGED <<phase, want, left, refused, picked, trailing, cutShort, adoptVars>>
    /\ m # head[1]
    /\ head' = [head EXCEPT ![1] = m]
    /\ IF m # "off" /\ winState \in PausedStates
       THEN winOverride' = "manual" /\ storedOverride' = "manual"
       ELSE UNCHANGED <<winOverride, storedOverride>>
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, mode, stamp, storedMode, storedStamp, restoredOn, fanVars,
                   winRead, winChanged, winState, storedWin>>
    /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

\* coordinator.async_set_window_override: on while the reading is not closed; off always.
OverrideSwitch == WithWindow /\ \E on \in BOOLEAN :
    LET new == IF on /\ winRead # "closed" THEN "switch" ELSE None
    IN /\ new # winOverride
       /\ ~(on /\ winOverride # None)
       /\ fresh' = IF phase = "applying" THEN FALSE ELSE fresh
       /\ UNCHANGED <<phase, want, left, refused, picked, trailing, cutShort, adoptVars>>
       /\ winOverride' = new /\ storedOverride' = new
       /\ decided' = FALSE
       /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn, fanVars,
                      winRead, winChanged, winState, storedWin>>
       /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                      fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

(***************************************************************************)
(* A new coordinator: __init__ reads the Store record (mode and the time   *)
(* of the last change: async_load_mode_record, mode_change_stamp,          *)
(* restore_shared_mode), the door/window store (async_load_openings); the  *)
(* switch restore hands the Fan auto truth to FanHold.restore and puts     *)
(* each room in _startup_resume, so the first compute seeds its latch from *)
(* the head's mode. A reload or a stop writes restore data; a crash leaves *)
(* the last periodic dump. `outage` is the fan the head shows when this    *)
(* start first reads it.                                                   *)
(***************************************************************************)
Start(process, crash, outage) ==
    LET record == IF crash THEN saved ELSE Dumped
        heldBack == record.has /\ record.held
        carries == record.has /\ ~record.held /\ record.last # None
        moved == outage # fanShown
        newMode == IF storedMode = None THEN "cool" ELSE storedMode
    IN
    /\ boot' = real
    /\ latch' = [r \in RoomIds |-> IF head[r] \in Modes THEN head[r] ELSE ""]
    /\ mode' = newMode
    /\ stamp' = storedStamp
    /\ restoredOn' = TRUE
    /\ fanShown' = outage
    /\ fanSent' = IF moved THEN None ELSE fanSent
    /\ fanLatched' = FALSE
    /\ fanIdx' = NoIdx
    /\ fanRestore' = IF ~record.has THEN None ELSE IF record.held THEN "held" ELSE "free"
    /\ fanCmd' = IF carries THEN Eligible(record.last) ELSE None
    /\ fanPrev' = IF carries THEN Eligible(IF record.prior = None THEN record.last ELSE record.prior)
                  ELSE None
    /\ fanOlder' = IF carries THEN {t \in record.older : InCeiling(t)} \ {fanCmd', fanPrev'} ELSE {}
    /\ saved' = record
    /\ winState' = storedWin /\ winOverride' = storedOverride
    /\ decided' = FALSE
    /\ pick' = IF moved THEN (IF outage = "auto" THEN None ELSE outage) ELSE pick
    /\ pickKind' =
         IF moved THEN (IF outage = "auto" THEN None ELSE "outage")
         ELSE IF pick = None THEN None
         ELSE IF pickKind # "strict" THEN pickKind
         ELSE IF ~pickSeen THEN "outage"
         ELSE IF crash /\ ~heldBack THEN "crash"
         ELSE "strict"
    /\ pickSeen' = (~moved /\ pickSeen)
    /\ gesture' = (gesture \/ (moved /\ outage # "auto") \/ crash)
    /\ restoreBroken' = (restoreBroken \/ newMode # mode \/ storedStamp # stamp
                         \/ storedWin # winState \/ storedOverride # winOverride)
    \* A retired apply sends nothing more (DeliveryQueue.owned); what it sent stays sent.
    /\ phase' = "idle" /\ left' = {} /\ refused' = {} /\ fresh' = TRUE /\ picked' = FALSE
    /\ cutShort' = (cutShort \/ phase = "applying")
    /\ UNCHANGED <<want, trailing>>
    \* The head write memory is in hass.data: a reload keeps it, a new process has none.
    \* No hold is saved, and a process start is quiet until STARTUP_RECOVER_DELAY.
    /\ spMem' = IF process THEN <<>> ELSE spMem
    /\ holding' = FALSE
    /\ quiet' = (WithAdopt /\ process)
    /\ spQueue' = IF process THEN Orphan(spQueue, <<>>, "start") ELSE spQueue
    /\ UNCHANGED <<target, spShown, shownKind, adopted, writeInHold>>
    /\ UNCHANGED <<real, pos, head, storedMode, storedStamp, winRead, winChanged, storedWin, storedOverride>>
    /\ UNCHANGED <<lastChange, flipInDwell, flipLate, fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind,
                   pauseBroken, overrideBroken>>

Reload == WithReload /\ Start(FALSE, FALSE, fanShown)
Restart == WithRestart /\ \E t \in (IF WithFan THEN Tokens ELSE {fanShown}) : Start(TRUE, FALSE, t)
Crash == WithCrash /\ \E t \in (IF WithFan THEN Tokens ELSE {fanShown}) : Start(TRUE, TRUE, t)

(***************************************************************************)
(* WithAdopt: room 1's setpoint and changes made at its head               *)
(* (coordinator._watch_head_change, _head_adoption,                        *)
(* _async_adopt_head_change; logic.HeadWriteMemory). The head is running,  *)
(* so its setpoint field is fixed; only the setpoint moves. A head that    *)
(* lags shows MXZ's writes in order, each later (spQueue). Gates other     *)
(* than startup quiet are out of this model. Each queued write carries a   *)
(* ghost kind: "mxz" while the memory still holds its value, else what     *)
(* dropped it: a process start, a person's press of a remembered value     *)
(* ("echo"), or HeadWriteMemory.seen in an adoption or a write ("seen").  *)
(***************************************************************************)
others == <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
            fanVars, winVars, ghosts, splitVars>>

\* A report that moves the setpoint (_on_head_change): the memory catches up, then the
\* hold is armed if the report is a change to adopt, and dropped if not.
Report(v, by) ==
    LET own == Own(spMem, v)
    IN /\ spShown' = v /\ shownKind' = by
       /\ spMem' = IF own THEN CaughtUp(spMem, v) ELSE spMem
       /\ holding' = (~own /\ v # target /\ ~quiet)

\* number.async_set_native_value: a person sets the target in Home Assistant, which
\* ends a hold (drop_head_change): the later change wins.
SetTarget == WithAdopt /\ \E v \in Targets :
    /\ v # target /\ target' = v /\ holding' = FALSE
    /\ UNCHANGED <<spShown, spQueue, spMem, quiet, shownKind, adopted, writeInHold, others>>

\* The apply's setpoint write (LocalDriver._async_apply, DeliveryQueue._async_issue): skipped while a hold
\* counts (_head_change_settling), remembered before it goes out.
WriteSp ==
    /\ WithAdopt /\ ~holding /\ spShown # target /\ Len(spQueue) < QueueMax
    /\ spMem' = Wrote(spMem, target, spShown)
    /\ spQueue' = Append(Orphan(spQueue, spMem', "seen"), [v |-> target, kind |-> "mxz"])
    /\ writeInHold' = (writeInHold \/ holding)
    /\ UNCHANGED <<target, spShown, holding, quiet, shownKind, adopted, others>>

\* The lagging head shows the oldest write it hasn't shown yet.
HeadShows ==
    /\ WithAdopt /\ Len(spQueue) > 0
    /\ spQueue' = Tail(spQueue)
    /\ IF Head(spQueue).v = spShown
       THEN UNCHANGED <<spShown, spMem, holding, shownKind>>
       ELSE Report(Head(spQueue).v, Head(spQueue).kind)
    /\ UNCHANGED <<target, quiet, adopted, writeInHold, others>>

\* A person at the head's remote. Writes still queued at the head can land after it.
PersonPress == WithAdopt /\ \E v \in Setpoints :
    /\ v # spShown /\ Report(v, "person")
    /\ spQueue' = Orphan(spQueue, spMem', "echo")
    /\ UNCHANGED <<target, quiet, adopted, writeInHold, others>>

\* The hold ends (ADOPT_DELAY): a change that still counts becomes the room's target.
\* The ghost files an adopted write of MXZ's by how it got past the memory.
AdoptFires ==
    /\ WithAdopt /\ holding
    /\ holding' = FALSE
    /\ IF ~Own(spMem, spShown) /\ spShown # target /\ ~quiet
       THEN /\ target' = spShown
            /\ spMem' = IF Len(spMem) > 0 THEN Seen(spMem, spShown) ELSE spMem
            /\ spQueue' = Orphan(spQueue, spMem', "seen")
            /\ adopted' = adopted \cup (IF shownKind = "person" THEN {} ELSE {shownKind})
       ELSE UNCHANGED <<target, spMem, spQueue, adopted>>
    /\ UNCHANGED <<spShown, quiet, shownKind, writeInHold, others>>

\* coordinator._on_startup_timer, STARTUP_RECOVER_DELAY after Home Assistant starts.
QuietEnds ==
    /\ WithAdopt /\ quiet
    /\ quiet' = FALSE
    /\ UNCHANGED <<target, spShown, spQueue, spMem, holding, shownKind, adopted, writeInHold, others>>

Next ==
    \/ Decide \/ Reading \/ Tick \/ Select
    \/ PersonFan \/ HeadReports \/ FanAutoOnGesture \/ FanAutoOffGesture \/ Dump
    \/ WindowReads \/ PersonMode \/ OverrideSwitch
    \/ Reload \/ Restart \/ Crash
    \/ Deliver \/ Refuse
    \/ SetTarget \/ WriteSp \/ HeadShows \/ PersonPress \/ AdoptFires \/ QuietEnds

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* The rules. docs/formal/README.md says each in words and names the       *)
(* Hypothesis property on the real code that enforces the same rule.       *)
(***************************************************************************)

\* A manual fan choice is never written over. The exceptions are explicit, by name in
\* PickKinds: only a pick of kind "strict" is bound with none.
ManualFanKept == ~fanOverwritten

\* A strict pick MXZ has read is a hold, and shows as one, across every start.
ManualFanShown == (pick # None /\ pickKind = "strict" /\ pickSeen) => ~FanAutoOn

\* Read live, MXZ never holds a speed no person touched. (A start can: see the README's exceptions.)
NoFalseHold == ~falseHold

\* F1 (docs/formal/PARKED-FINDINGS.md): a head two or more different writes behind isn't a
\* person's pick either. fanOlder keeps those writes until the head reports the last one.
NoFalseHoldTwoBehind == ~falseHoldTwoBehind

\* No mode flip inside the dwell window, from any path, across any start.
NoFlipInDwell == ~flipInDwell

\* With the hold over and only the other mode calling, a Decide changes the mode.
NoExtraWait == ~flipLate

\* The hold's clock is the last real change: a start neither restarts nor forgets it.
HoldClockKept == stamp = lastChange

\* A start brings back the shared mode, the hold, the door/window state and an override.
RestartRestores == ~restoreBroken

\* After a Decide no head runs against the shared mode, apart from a head that refused
\* its command (the README's exceptions).
HeadsFollowMode == decided => \A r \in RoomIds \ refused : head[r] \in {mode, "fan_only", "off"}

\* After a Decide a paused head is off, and in a pause no fan is written and its call counts for nothing.
PauseStopsHead == /\ (decided /\ winState \in PausedStates /\ 1 \notin refused) => head[1] = "off"
                  /\ ~pauseBroken

\* A person's override holds until the window reads closed.
OverrideHolds == ~overrideBroken

\* A head is never sent a direction the record doesn't hold, unless a person has picked
\* a mode since that apply's decision: nothing a head does can leave the record behind.
RecordLeadsHeads == ~trailing

\* The target never takes a value that only MXZ's own writes put on the head. Three ways
\* past the memory are open findings in docs/STATE-MACHINES.md, each a rule of its own:
\* a write from before a process start (SM-1), and a write still queued that a person's
\* press of a remembered value, or an adoption, dropped from the memory (SM-10).
OwnWriteNeverAdopted == "mxz" \notin adopted
StartWriteNeverAdopted == "start" \notin adopted
QueuedWriteNeverAdopted == adopted \cap {"echo", "seen"} = {}

\* No setpoint write to a head while a change made at it holds.
HoldBlocksWrite == ~writeInHold

(***************************************************************************)
(* Reachability. Each of these is expected to BREAK, in the config named   *)
(* for it: a path to the thing exists, so the rule above it isn't vacuous. *)
(***************************************************************************)
NeverFlips == mode = "cool"
NeverHoldBlocksACall == ~(decided /\ stamp # NoTime /\ real - stamp < H
                          /\ \E r \in RoomIds : Demand(pos[r]) \in Modes /\ Demand(pos[r]) # mode)
NeverStrictHoldAcrossAStart == ~(pick # None /\ pickKind = "strict" /\ pickSeen /\ fanRestore = "held")
NeverKnownExceptionLosesAPick == ~exceptionLost
NeverPaused == ~(decided /\ winState = Paused)
NeverOverrideRunning == ~(decided /\ winOverride = "manual" /\ winRead = "open" /\ head[1] \in Modes)
NeverPauseRestored == ~(~decided /\ boot > 0 /\ winState = Paused)
NeverLagEcho == ~(fanSent # None /\ fanCmd = fanSent /\ fanShown = fanPrev /\ fanShown # fanCmd)
NeverTwoBehindEcho == ~(decided /\ twoBehind /\ fanShown \in fanOlder /\ ~fanLatched)
NeverRefused == refused = {}
NeverCutShort == ~cutShort
NeverAdopts == target \in Targets
NeverLateOwnWriteInQuiet == ~(quiet /\ shownKind = "start" /\ spShown # target /\ ~Own(spMem, spShown))
=============================================================================
