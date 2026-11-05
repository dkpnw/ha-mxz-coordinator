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
(* Time is whole ticks on one clock. Rooms are in priority order, enabled, *)
(* with Allowed drift 1 and Mode switch threshold 3 as multiples of one    *)
(* reading position (see Demand and Engage). Room 1 is the room with the   *)
(* door/window and the fan under study: _fan_cmd, _fan_prev, _fan_idx,     *)
(* _fan_latched and _openings are per head and never read another head's.  *)
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
    WithCrash     \* Home Assistant dies without its stop

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
\*   echo      one of the last two speeds commanded (the known exception)
\*   outage    made before MXZ read it, across a stop or before the first read
\*   crash     a crash lost the hold's restore data
\*   seed      made with no command memory (the seed's documented adoption)
\*   inflight  a Fan auto off with a write of MXZ's already sent, which can still land
PickKinds == {"strict", "echo", "outage", "crash", "seed", "inflight"}

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
    fanCmd, fanPrev,    \* coordinator._fan_cmd, _fan_prev, or None
    fanIdx,             \* coordinator._fan_idx, or NoIdx
    fanLatched,         \* coordinator._fan_latched
    fanRestore,         \* coordinator._fan_restore: None, "held" or "free"
    saved,              \* the Fan auto switch's restore data; has = FALSE: none yet
    \* Room 1's door/window (coordinator._openings).
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
    falseHoldTwoBehind, \* the same latch with the head two writes behind (open finding F1)
    restoreBroken,      \* a start brought back something other than what was running
    pauseBroken,        \* a paused room's head got a fan write, or its call counted
    overrideBroken      \* a live override was parked or dropped with the window not closed

fanVars == <<fanShown, fanSent, fanCmd, fanPrev, fanIdx, fanLatched, fanRestore, saved>>
winVars == <<winRead, winChanged, winState, winOverride, storedWin, storedOverride>>
ghosts == <<lastChange, decided, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
            fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, twoBehind, falseHoldTwoBehind, restoreBroken,
            pauseBroken, overrideBroken>>
vars == <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
          fanVars, winVars, ghosts>>

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

Memory == [cmd |-> fanCmd, prev |-> fanPrev, idx |-> fanIdx, latched |-> fanLatched]

\* coordinator._hold_at
HoldAt(f, o) == [f EXCEPT !.latched = TRUE, !.prev = o, !.cmd = o]
\* coordinator._adopt_fan_speed; `i` NoIdx resumes from the ceiling, as the switch does.
Adopt(f, o, i) ==
    [f EXCEPT !.latched = FALSE, !.prev = IF f.cmd = None THEN o ELSE f.cmd, !.cmd = o,
              !.idx = IF i = NoIdx THEN FanMaxIdx ELSE i]
\* The first branch of _observe_fan_latch and of _reconcile_restored_hold.
AtAuto(f) ==
    [f EXCEPT !.latched = FALSE,
              !.prev = IF f.cmd = None THEN "auto" ELSE f.prev,
              !.cmd = IF f.cmd = None THEN "auto" ELSE f.cmd]
InCeiling(t) == t \in RungSet /\ RungIdx(t) <= FanMaxIdx

\* coordinator._reconcile_restored_hold
Reconcile(f, o, restoredHeld, act) ==
    IF o = "auto" THEN [f |-> AtAuto(f), held |-> FALSE]
    ELSE IF restoredHeld THEN [f |-> HoldAt(f, o), held |-> TRUE]
    ELSE LET remembered == o \in {f.cmd, f.prev}
             ours == InCeiling(o) /\ (f.cmd = None \/ remembered \/ act = "fan_only")
         IN IF ~ours THEN [f |-> HoldAt(f, o), held |-> TRUE]
            ELSE LET g == [f EXCEPT !.latched = FALSE,
                                    !.prev = IF remembered THEN f.prev ELSE o,
                                    !.cmd = IF remembered THEN f.cmd ELSE o]
                     h == IF act \in Modes /\ o \in RungSet
                          THEN [g EXCEPT !.idx = Min(RungIdx(o), FanMaxIdx)] ELSE g
                 IN [f |-> h, held |-> FALSE]

\* coordinator._seed_is_own_idle (idle action fan_only, an entry past its first minutes).
SeedIsOwnIdle(o, act) == act = "fan_only" /\ restoredOn /\ InCeiling(o)
\* coordinator._seed_matches_boost
SeedMatchesBoost(o, act, delta) ==
    act \in Modes /\ InCeiling(o) /\ FanFor(delta, RungIdx(o)) = RungIdx(o)

\* coordinator._observe_fan_latch (fan_on_pending is not modeled: the head always
\* reports a speed here).
Observe(f, o, act, delta) ==
    IF fanRestore # None THEN Reconcile(f, o, fanRestore = "held", act)
    ELSE IF o = "auto" THEN [f |-> AtAuto(f), held |-> FALSE]
    ELSE IF f.cmd = None
    THEN IF SeedIsOwnIdle(o, act)
         THEN [f |-> [f EXCEPT !.latched = FALSE, !.prev = o, !.cmd = o], held |-> FALSE]
         ELSE IF SeedMatchesBoost(o, act, delta)
         THEN [f |-> Adopt(f, o, RungIdx(o)), held |-> FALSE]
         ELSE [f |-> HoldAt(f, o), held |-> TRUE]
    ELSE IF o \notin {f.cmd, f.prev}
    THEN [f |-> HoldAt(f, o), held |-> TRUE]
    ELSE [f |-> f, held |-> f.latched]

\* coordinator._apply_fan on what _observe_fan_latch left; `token` is None for no write.
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

\* coordinator._write_fan: a rewrite of the token already commanded rolls nothing.
Rolled(f, t) ==
    IF t = None \/ t = f.cmd THEN f
    ELSE [f EXCEPT !.prev = IF f.cmd = None THEN t ELSE f.cmd, !.cmd = t]

\* coordinator.fan_auto_is_on
FanAutoOn == IF fanRestore # None THEN fanRestore = "free" ELSE ~fanLatched

\* What the Fan auto switch writes as restore data (switch.extra_state_attributes).
Dumped == [has |-> TRUE, held |-> ~FanAutoOn, last |-> fanCmd, prior |-> fanPrev]
NoDump == [has |-> FALSE, held |-> FALSE, last |-> None, prior |-> None]

\* switch.py's restore eligibility in restore_fan_hold.
Eligible(t) == IF InCeiling(t) THEN t ELSE "auto"

TypeOK ==
    /\ real \in 0..MaxReal /\ boot \in 0..MaxReal
    /\ pos \in [RoomIds -> Readings]
    /\ latch \in [RoomIds -> {""} \cup Modes]
    /\ head \in [RoomIds -> {"off", "fan_only"} \cup Modes]
    /\ mode \in Modes /\ storedMode \in {None} \cup Modes
    /\ fanShown \in Tokens /\ fanSent \in {None} \cup Tokens
    /\ fanCmd \in {None} \cup Tokens /\ fanPrev \in {None} \cup Tokens
    /\ fanIdx \in {NoIdx} \cup 0..2 /\ fanLatched \in BOOLEAN
    /\ fanRestore \in {None, "held", "free"}
    /\ winRead \in {"open", "closed", None}
    /\ winState \in OpeningStates /\ storedWin \in OpeningStates
    /\ winOverride \in {None, "switch", "manual"}
    /\ storedOverride \in {None, "switch", "manual"}
    /\ pick \in {None} \cup Tokens /\ pickKind \in {None} \cup PickKinds

Init ==
    /\ real = 0 /\ boot = 0
    /\ pos \in [RoomIds -> Readings]
    /\ latch = [r \in RoomIds |-> ""]
    /\ head = [r \in RoomIds |-> "off"]
    /\ mode = "cool"                      \* a fresh install, _arbitration_mode
    /\ stamp = NoTime /\ storedStamp = NoTime /\ storedMode = None
    /\ restoredOn = FALSE
    /\ fanShown = "auto" /\ fanSent = None /\ fanCmd = None /\ fanPrev = None
    /\ fanIdx = NoIdx /\ fanLatched = FALSE /\ fanRestore = None /\ saved = NoDump
    /\ winRead = "closed" /\ winChanged = 0 /\ winState = Closed /\ winOverride = None
    /\ storedWin = Closed /\ storedOverride = None
    /\ lastChange = NoTime /\ decided = FALSE
    /\ pick = None /\ pickKind = None /\ pickSeen = FALSE /\ gesture = FALSE
    /\ flipInDwell = FALSE /\ flipLate = FALSE
    /\ fanOverwritten = FALSE /\ exceptionLost = FALSE /\ falseHold = FALSE
    /\ twoBehind = FALSE /\ falseHoldTwoBehind = FALSE
    /\ restoreBroken = FALSE /\ pauseBroken = FALSE /\ overrideBroken = FALSE

(***************************************************************************)
(* Decide: MXZCoordinator._compute then _apply.                            *)
(*   _compute : each room's demand and engage (room_call,                  *)
(*              engage_with_latch); _opening_step (a closed reading ends   *)
(*              an override; logic.opening_state; saved at once); a paused *)
(*              room has no demand, no engage and an empty latch; then     *)
(*              logic.arbitrate with `allowed` and `running`.              *)
(*   _apply   : a paused head goes to off with its fan only read           *)
(*              (_observe_paused_fan); any other head gets                 *)
(*              logic.head_action, then _apply_fan; a changed mode is      *)
(*              stamped (_stamp_mode_change) and stored (_record_mode).    *)
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
    IN
    /\ latch' = nl
    /\ head' = [r \in RoomIds |->
                  IF r = 1 /\ isPaused THEN "off"
                  ELSE IF nl[r] = state THEN state ELSE "fan_only"]
    /\ mode' = state
    /\ stamp' = IF changed THEN real ELSE stamp
    /\ storedMode' = IF changed THEN state ELSE storedMode
    /\ storedStamp' = IF changed THEN real ELSE storedStamp
    /\ fanShown' = IF lands THEN fan.token ELSE fanShown
    /\ fanSent' = IF written /\ WithLag THEN fan.token ELSE fanSent
    /\ fanCmd' = rolled.cmd /\ fanPrev' = rolled.prev
    /\ fanIdx' = rolled.idx /\ fanLatched' = rolled.latched
    /\ fanRestore' = IF WithFan THEN None ELSE fanRestore
    /\ winState' = newWin /\ winOverride' = override0
    /\ storedWin' = newWin /\ storedOverride' = override0
    /\ lastChange' = IF changed THEN real ELSE lastChange
    /\ decided' = TRUE
    /\ pick' = IF overwritesPick THEN None ELSE pick
    /\ pickKind' = IF overwritesPick THEN None ELSE pickKind
    /\ pickSeen' = IF WithFan THEN pick # None ELSE pickSeen
    /\ flipInDwell' = (flipInDwell \/ (changed /\ tooSoon))
    /\ flipLate' = (flipLate \/ (~changed /\ over /\ otherCalls))
    /\ fanOverwritten' = (fanOverwritten \/ (overwritesPick /\ strict))
    /\ exceptionLost' = (exceptionLost \/ (overwritesPick /\ ~strict))
    /\ falseHold' = (falseHold \/ (newHold /\ ~twoBehind))
    /\ falseHoldTwoBehind' = (falseHoldTwoBehind \/ (newHold /\ twoBehind))
    /\ twoBehind' = (fanSent' # None /\ (twoBehind \/ (written /\ fanSent # None /\ fan.token # fanSent)))
    /\ pauseBroken' = (pauseBroken \/ (isPaused /\ (written \/ head'[1] # "off"
                                       \/ (changed /\ Rooms = 1 /\ state # Resting))))
    /\ overrideBroken' = (overrideBroken \/ (winOverride # None /\ winRead # "closed"
                                             /\ (override0 = None \/ isPaused \/ head'[1] = "off")))
    /\ UNCHANGED <<real, boot, pos, restoredOn, saved, winRead, winChanged, restoreBroken, gesture>>

\* The room's reading changes (a sensor reports).
Reading == \E r \in RoomIds, p \in Readings :
    /\ p # pos[r] /\ pos' = [pos EXCEPT ![r] = p]
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, latch, head, mode, stamp, storedMode, storedStamp, restoredOn, fanVars, winVars>>
    /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

Tick ==
    /\ real < MaxReal /\ real' = real + 1
    /\ decided' = FALSE
    /\ UNCHANGED <<boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn, fanVars, winVars>>
    /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

\* select.async_select_option, then coordinator.async_select_shared_mode: a changed
\* direction stamps the hold and writes the record.
Select == WithSelect /\ \E m \in Modes :
    /\ m # mode
    /\ mode' = m /\ stamp' = real /\ storedMode' = m /\ storedStamp' = real
    /\ lastChange' = real /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, restoredOn, fanVars, winVars>>
    /\ UNCHANGED <<pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

\* A person picks a speed on the head (its remote: not a service call of MXZ's).
\* Picking the speed already showing changes nothing that can be read, so it is
\* not a step: _observe_fan_latch's docstring accepts that.
PersonFan == WithFan /\ \E t \in Tokens :
    /\ t # fanShown
    /\ fanShown' = t /\ fanSent' = None
    /\ pick' = IF t = "auto" THEN None ELSE t
    /\ pickKind' = IF t = "auto" THEN None
                   ELSE IF fanRestore # None THEN "outage"
                   ELSE IF fanCmd = None THEN "seed"
                   ELSE IF ~fanLatched /\ t \in {fanCmd, fanPrev} THEN "echo"
                   ELSE "strict"
    /\ pickSeen' = FALSE
    /\ gesture' = (t # "auto")
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
                   fanCmd, fanPrev, fanIdx, fanLatched, fanRestore, saved, winVars>>
    /\ twoBehind' = FALSE
    /\ UNCHANGED <<lastChange, flipInDwell, flipLate, fanOverwritten, exceptionLost, falseHold, falseHoldTwoBehind,
                   restoreBroken, pauseBroken, overrideBroken>>

\* A lagging head reports the last fan MXZ sent it.
HeadReports ==
    /\ WithLag
    /\ fanSent # None
    /\ fanShown' = fanSent /\ fanSent' = None
    /\ pick' = IF pick = fanSent THEN pick ELSE None
    /\ pickKind' = IF pick = fanSent THEN pickKind ELSE None
    /\ twoBehind' = FALSE
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
                   fanCmd, fanPrev, fanIdx, fanLatched, fanRestore, saved, winVars>>
    /\ UNCHANGED <<lastChange, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

\* coordinator.async_set_fan_auto(on=True): hand back and adopt the showing speed.
FanAutoOnGesture ==
    /\ WithFan
    /\ fanRestore' = None
    /\ LET a == Adopt(Memory, fanShown, NoIdx)
       IN fanCmd' = a.cmd /\ fanPrev' = a.prev /\ fanIdx' = a.idx /\ fanLatched' = a.latched
    /\ pick' = None /\ pickKind' = None /\ pickSeen' = FALSE
    \* A write still unreported can land after this and read as a new speed.
    /\ gesture' = (fanSent # None)
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
                   fanShown, fanSent, saved, winVars>>
    /\ UNCHANGED <<lastChange, flipInDwell, flipLate, fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind,
                   restoreBroken, pauseBroken, overrideBroken>>

\* coordinator.async_set_fan_auto(on=False): hold the showing speed. At auto, a no-op
\* (it only drops a pending restore, as its code does).
FanAutoOffGesture ==
    /\ WithFan
    /\ fanShown # "auto"
    /\ fanRestore' = None
    /\ fanPrev' = IF fanCmd = None THEN fanShown ELSE fanCmd
    /\ fanCmd' = fanShown /\ fanLatched' = TRUE
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
    /\ saved' = Dumped
    /\ saved # Dumped
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn,
                   fanShown, fanSent, fanCmd, fanPrev, fanIdx, fanLatched, fanRestore, winVars>>
    /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

\* The door/window reads something else (coordinator._on_opening_change's recompute
\* is a later Decide).
WindowReads == WithWindow /\ \E x \in {"open", "closed", None} :
    /\ x # winRead /\ winRead' = x /\ winChanged' = real
    /\ decided' = FALSE
    /\ UNCHANGED <<real, boot, pos, latch, head, mode, stamp, storedMode, storedStamp, restoredOn, fanVars,
                   winState, winOverride, storedWin, storedOverride>>
    /\ UNCHANGED <<lastChange, pick, pickKind, pickSeen, gesture, flipInDwell, flipLate,
                   fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind, restoreBroken, pauseBroken, overrideBroken>>

\* A person changes room 1's head mode. coordinator._on_head_change: a real change to
\* anything but off, on a paused room, is a takeover, saved at once.
PersonMode == WithWindow /\ \E m \in {"off", "fan_only"} \cup Modes :
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
(* switch restore hands the Fan auto truth to restore_fan_hold and puts    *)
(* each room in _startup_resume, so the first compute seeds its latch from *)
(* the head's mode. A reload or a stop writes restore data; a crash leaves *)
(* the last periodic dump. `outage` is the fan the head shows when this    *)
(* start first reads it.                                                   *)
(***************************************************************************)
Start(crash, outage) ==
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
    /\ UNCHANGED <<real, pos, head, storedMode, storedStamp, winRead, winChanged, storedWin, storedOverride>>
    /\ UNCHANGED <<lastChange, flipInDwell, flipLate, fanOverwritten, exceptionLost, falseHold, twoBehind, falseHoldTwoBehind,
                   pauseBroken, overrideBroken>>

Reload == WithReload /\ Start(FALSE, fanShown)
Restart == WithRestart /\ \E t \in (IF WithFan THEN Tokens ELSE {fanShown}) : Start(FALSE, t)
Crash == WithCrash /\ \E t \in (IF WithFan THEN Tokens ELSE {fanShown}) : Start(TRUE, t)

Next ==
    \/ Decide \/ Reading \/ Tick \/ Select
    \/ PersonFan \/ HeadReports \/ FanAutoOnGesture \/ FanAutoOffGesture \/ Dump
    \/ WindowReads \/ PersonMode \/ OverrideSwitch
    \/ Reload \/ Restart \/ Crash

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

\* Open finding F1 (docs/formal/PARKED-FINDINGS.md): this one BREAKS. A head two different
\* writes behind reads as a person's pick. tools/formal_tlc.py requires it to break,
\* so that a fix has to move it into NoFalseHold.
NoFalseHoldTwoBehind == ~falseHoldTwoBehind

\* No mode flip inside the dwell window, from any path, across any start.
NoFlipInDwell == ~flipInDwell

\* With the hold over and only the other mode calling, a Decide changes the mode.
NoExtraWait == ~flipLate

\* The hold's clock is the last real change: a start neither restarts nor forgets it.
HoldClockKept == stamp = lastChange

\* A start brings back the shared mode, the hold, the door/window state and an override.
RestartRestores == ~restoreBroken

\* After a Decide no head runs against the shared mode.
HeadsFollowMode == decided => \A r \in RoomIds : head[r] \in {mode, "fan_only", "off"}

\* After a Decide a paused head is off, and in a pause no fan is written and its call counts for nothing.
PauseStopsHead == /\ (decided /\ winState \in PausedStates) => head[1] = "off"
                  /\ ~pauseBroken

\* A person's override holds until the window reads closed.
OverrideHolds == ~overrideBroken

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
=============================================================================
