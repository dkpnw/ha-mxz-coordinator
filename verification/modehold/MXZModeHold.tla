---------------------------- MODULE MXZModeHold ----------------------------
(***************************************************************************)
(* The shared cool/heat mode of one outdoor unit, the wait between two     *)
(* changes of it (Minimum time between mode switches, the "hold"), and the *)
(* arbitration the hold gates, across reloads, restarts, crashes and       *)
(* wall-clock steps.                                                       *)
(*                                                                         *)
(* Every action names the code it stands for. Paths are under              *)
(* custom_components/mxz_coordinator/. Decide is one                       *)
(* MXZCoordinator._compute plus its _apply, taken here as one step. In the *)
(* code they are not one step: _apply awaits the head deliveries           *)
(* (_wait_deliveries) and only then stamps the mode change                 *)
(* (_stamp_mode_change). Nothing that happens during that await is         *)
(* modeled: a person's pick, a new reading, or a reload. A reload there    *)
(* retires the coordinator (async_shutdown_listeners sets _retired), so    *)
(* the stamp sets the in-memory time but skips the Store write, and the    *)
(* new coordinator's hold runs from the change before. Reload here never   *)
(* falls inside a Decide, so no config can show that.                      *)
(*                                                                         *)
(* Time is whole ticks. H is mode_hysteresis. The true time is `real`;     *)
(* the wall clock (dt_util.utcnow) is real + skew; the monotonic clock     *)
(* (time.monotonic) is real - boot and starts again at each process start. *)
(*                                                                         *)
(* Rooms are enabled, coordinated rooms in priority order (room 1 first),  *)
(* each with the defaults: Allowed drift 1, Mode switch threshold 3. A     *)
(* room's reading is one of six positions against its target:             *)
(*    3  above target + threshold   (demand cool, starts a cool run)       *)
(*    2  above target + drift       (starts a cool run, no demand)         *)
(*    1  above target, inside drift (a cool run continues)                 *)
(*   -1  below target, inside drift (a heat run continues)                 *)
(*   -2  below target - drift       (starts a heat run, no demand)         *)
(*   -3  below target - threshold   (demand heat, starts a heat run)       *)
(* (logic.room_call with band = max(threshold, drift) for the demand, and  *)
(* logic.engage_with_latch with band = drift for the run.)                 *)
(*                                                                         *)
(* WithVote adds one hands-off room after the coordinated ones: a person   *)
(* runs its head, and its mode is an ordinary vote (PRIORITY_VOTE).        *)
(* Safety priority, floors, Dry, Comfort priority, lost heads and eco are  *)
(* not in this model; tests/test_modehold_properties.py checks them on the *)
(* real logic.arbitrate for every input in its table.                      *)
(***************************************************************************)
EXTENDS Integers, Sequences

CONSTANTS
    H,            \* mode_hysteresis, ticks (> 0)
    MaxReal,      \* the model ends time here
    SkewMax,      \* an NTP step or a hand-set clock leaves the wall clock
                  \* -SkewMax, 0 or +SkewMax off true time (0: never stepped)
    Resting,      \* resting_mode_bias: "last", "cool" or "heat"
    Rooms,        \* coordinated rooms
    WithVote,     \* a hands-off room votes
    WithSelect,   \* a person picks the shared mode (select.async_select_option)
    WithReload,   \* config entry reload (an options save)
    WithRestart,  \* Home Assistant stops and starts
    WithCrash     \* Home Assistant dies without its stop (power cut, kill)

None == "none"
\* "No time": no record, no stamp. Outside every time the model reaches.
NoTime == -1000
Modes == {"cool", "heat"}
Positions == {-3, -2, -1, 1, 2, 3}
RoomIds == 1..Rooms
Max0(x) == IF x > 0 THEN x ELSE 0
Skews == {-SkewMax, 0, SkewMax}

VARIABLES
    real, boot, skew,   \* true time, process start, wall-clock offset
    pos,                \* each room's reading (positions above)
    latch,              \* coordinator._engage_latch: "", "cool" or "heat"
    head,               \* what each room's head runs: "", "cool" or "heat"
    vote,               \* the hands-off head's mode: None, "cool" or "heat"
    mode,               \* coordinator.current_shared_mode
    byVotes,            \* coordinator._mode_by_votes (memory only)
    stamp,              \* coordinator._last_mode_change_ts (monotonic), or NoTime
    stored,             \* the Store record MODE_CHANGE_KEY (wall clock), or NoTime
    restoreMode,        \* the shared-mode select's restore record
    \* Ghosts: what the world knows, not the code.
    applied,            \* the shared mode the heads were last given, or None
    lastChange,         \* true time the last real mode change began its hold
    steppedSince,       \* the wall clock was stepped since lastChange
    restartedSince,     \* a reload/restart/crash happened since lastChange
    voteOwned,          \* the heads' mode came from votes; no room has run in it since
    picked,             \* a person's pick not yet given to the heads, or None
    lastStamp,          \* true time of the last stamp, or NoTime
    steppedSinceStamp,  \* the wall clock was stepped since lastStamp
    holdBroken,         \* a change came sooner than the hold allows
    runBroken           \* a running room lost the mode with no call against it

vars == <<real, boot, skew, pos, latch, head, vote, mode, byVotes, stamp,
          stored, restoreMode, applied, lastChange, steppedSince,
          restartedSince, voteOwned, picked, lastStamp, steppedSinceStamp,
          holdBroken, runBroken>>
ghosts == <<applied, lastChange, steppedSince, restartedSince, voteOwned,
            picked, lastStamp, steppedSinceStamp, holdBroken, runBroken>>

Wall == real + skew
Mono == real - boot

\* logic.room_call for the demand (band = threshold 3 > drift 1).
Demand(p) == IF p = 3 THEN "cool" ELSE IF p = -3 THEN "heat" ELSE None

\* logic.engage_with_latch: engaged runs to the target (band 0), else starts past drift.
Engage(prior, p) ==
    IF prior \in Modes
    THEN LET raw == IF p > 0 THEN "cool" ELSE "heat"
         IN IF raw = prior THEN prior ELSE ""
    ELSE IF p >= 2 THEN "cool" ELSE IF p <= -2 THEN "heat" ELSE ""

\* logic.shared_mode over directions in rank order.
SharedMode(ds, current, allowed) ==
    LET rest == IF Resting \in Modes THEN Resting ELSE current
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

\* coordinator.mode_change_stamp of what async_load_last_mode_change read:
\* time.monotonic() - max(0.0, utcnow - stored), read at this start.
StartStamp(rec, wallNow, monoNow) ==
    IF rec = NoTime THEN NoTime ELSE monoNow - Max0(wallNow - rec)

TypeOK ==
    /\ real \in 0..MaxReal /\ boot \in 0..MaxReal /\ skew \in Skews
    /\ pos \in [RoomIds -> Positions]
    /\ latch \in [RoomIds -> {""} \cup Modes]
    /\ head \in [RoomIds -> {""} \cup Modes]
    /\ vote \in {None} \cup Modes
    /\ mode \in Modes /\ restoreMode \in Modes
    /\ byVotes \in BOOLEAN /\ voteOwned \in BOOLEAN
    /\ applied \in {None} \cup Modes /\ picked \in {None} \cup Modes

Init ==
    /\ real = 0 /\ boot = 0 /\ skew = 0
    /\ pos \in [RoomIds -> Positions]
    /\ latch = [i \in RoomIds |-> ""]     \* construction has no prior run
    /\ head = [i \in RoomIds |-> ""]
    /\ vote = None
    /\ mode = "cool"                      \* cold start (_arbitration_mode)
    /\ byVotes = FALSE
    /\ stamp = NoTime /\ stored = NoTime  \* a fresh install: no change on record
    /\ restoreMode = "cool"
    /\ applied = None /\ lastChange = NoTime
    /\ steppedSince = FALSE /\ restartedSince = FALSE
    /\ voteOwned = FALSE /\ picked = None
    /\ lastStamp = NoTime /\ steppedSinceStamp = FALSE
    /\ holdBroken = FALSE /\ runBroken = FALSE

(***************************************************************************)
(* Decide: MXZCoordinator._compute then _apply.                            *)
(*   allowed     = elapsed is None or elapsed >= self.hysteresis           *)
(*   engaged     = any held engage is cool|heat                            *)
(*   grant       = logic.arbitrate(...); by_votes = grant.read_only, then  *)
(*                 the _mode_by_votes undo rule in _compute                *)
(*   _apply      : a head runs the mode when its engage is that mode       *)
(*                 (logic.head_action), else parks; then the stamp rules   *)
(*                 at the end of _apply; _stamp_mode_change writes the     *)
(*                 Store record at once (async_delay_save(..., 0)).        *)
(***************************************************************************)
Decide ==
    LET nl       == [i \in RoomIds |-> Engage(latch[i], pos[i])]
        dem      == [i \in RoomIds |-> Demand(pos[i])]
        allowed  == stamp = NoTime \/ Mono - stamp >= H
        engaged  == \E i \in RoomIds : nl[i] \in Modes
        cmd      == SelectSeq(dem, LAMBDA d : d # None)
        gateOpen == Len(cmd) = 0 /\ ~engaged
        votes    == IF WithVote /\ gateOpen /\ vote # None THEN <<vote>> ELSE <<>>
        full     == SharedMode(cmd \o votes, mode, allowed)
        plain    == SharedMode(cmd, mode, allowed)
        readOnly == Len(votes) > 0 /\ full # plain /\ gateOpen
        calls    == \E i \in RoomIds : dem[i] \in Modes \/ nl[i] \in Modes
        byV      == readOnly \/ (byVotes /\ full # mode /\ ~calls)
        state    == full
        runs     == \E i \in RoomIds : dem[i] = state \/ nl[i] = state
        restOk   == Resting \notin Modes \/ state # Resting
        doStamp  == IF state # mode THEN ~byV ELSE byVotes /\ runs
        \* ghost bookkeeping
        change   == applied \in Modes /\ state # applied
        exempt   == byV \/ picked = state
        owns     == voteOwned /\ ~change /\ runs
        realNew  == (change /\ ~exempt) \/ owns
        tooSoon  == lastChange # NoTime /\ real - lastChange < H
                    /\ ~(steppedSince /\ restartedSince)
        runner   == \E i \in RoomIds : head[i] = mode /\ nl[i] = mode
        against  == \E i \in RoomIds : dem[i] = state
    IN
    /\ latch' = nl
    /\ head' = [i \in RoomIds |-> IF nl[i] = state THEN state ELSE ""]
    /\ mode' = state
    /\ byVotes' = IF state # mode THEN byV /\ restOk
                  ELSE IF byVotes /\ runs THEN FALSE ELSE byVotes
    /\ stamp' = IF doStamp THEN Mono ELSE stamp
    /\ stored' = IF doStamp THEN Wall ELSE stored
    /\ lastStamp' = IF doStamp THEN real ELSE lastStamp
    /\ steppedSinceStamp' = IF doStamp THEN FALSE ELSE steppedSinceStamp
    /\ applied' = state
    /\ holdBroken' = (holdBroken \/ (change /\ ~exempt /\ tooSoon))
    /\ runBroken' = (runBroken \/ (runner /\ state # mode /\ ~against
                                   /\ picked # state))
    /\ lastChange' = IF realNew THEN real ELSE lastChange
    /\ steppedSince' = IF realNew THEN FALSE ELSE steppedSince
    /\ restartedSince' = IF realNew THEN FALSE ELSE restartedSince
    /\ voteOwned' = IF change THEN byV /\ restOk /\ picked # state
                    ELSE IF owns THEN FALSE ELSE voteOwned
    /\ picked' = IF picked = state THEN None ELSE picked
    /\ UNCHANGED <<real, boot, skew, pos, vote, restoreMode>>

\* The room's reading changes (any jump: a window opens, a sensor reports).
Reading == \E i \in RoomIds, p \in Positions :
    /\ pos' = [pos EXCEPT ![i] = p]
    /\ UNCHANGED <<real, boot, skew, latch, head, vote, mode, byVotes, stamp,
                   stored, restoreMode>> /\ UNCHANGED ghosts

\* A person runs the hands-off head (its vote, coordinator._hands_off_vote).
HandsOff == WithVote /\ \E v \in {None} \cup Modes :
    /\ v # vote /\ vote' = v
    /\ UNCHANGED <<real, boot, skew, pos, latch, head, mode, byVotes, stamp,
                   stored, restoreMode>> /\ UNCHANGED ghosts

Tick == real < MaxReal /\ real' = real + 1
    /\ UNCHANGED <<boot, skew, pos, latch, head, vote, mode, byVotes, stamp,
                   stored, restoreMode>> /\ UNCHANGED ghosts

\* NTP steps the wall clock, or a person sets it. Monotonic does not move.
ClockStep == \E s \in Skews :
    /\ s # skew /\ skew' = s
    /\ steppedSince' = TRUE /\ steppedSinceStamp' = TRUE
    /\ UNCHANGED <<real, boot, pos, latch, head, vote, mode, byVotes, stamp,
                   stored, restoreMode, applied, lastChange, restartedSince,
                   voteOwned, picked, lastStamp, holdBroken, runBroken>>

\* coordinator.async_select_shared_mode: a changed direction stamps the hold.
Select == WithSelect /\ \E m \in Modes :
    /\ m # mode
    /\ mode' = m /\ byVotes' = FALSE
    /\ stamp' = Mono /\ stored' = Wall
    /\ lastStamp' = real /\ steppedSinceStamp' = FALSE
    /\ picked' = m /\ lastChange' = real
    /\ steppedSince' = FALSE /\ restartedSince' = FALSE /\ voteOwned' = FALSE
    /\ UNCHANGED <<real, boot, skew, pos, latch, head, vote, restoreMode,
                   applied, holdBroken, runBroken>>

\* A new coordinator: __init__ (stamp from the record, _mode_by_votes False,
\* latch ""), then switch restore puts each room in _startup_resume and the
\* first _compute seeds its latch from the head's mode.
NewCoordinator(rec, newBoot, newMode, newRestore) ==
    /\ boot' = newBoot
    /\ stamp' = StartStamp(rec, Wall, real - newBoot)
    /\ stored' = rec
    /\ byVotes' = FALSE
    /\ latch' = head
    /\ mode' = newMode
    /\ restoreMode' = newRestore
    /\ restartedSince' = TRUE
    /\ UNCHANGED <<real, skew, pos, head, vote, applied, lastChange,
                   steppedSince, voteOwned, picked, lastStamp,
                   steppedSinceStamp, holdBroken, runBroken>>

\* __init__.async_reload: same process. The Store instance is shared
\* (_mode_change_store), and the select restores the state it had.
Reload == WithReload /\ NewCoordinator(stored, boot, mode, restoreMode)

\* A stop writes restore data (the select's mode) and flushes the Store.
Restart == WithRestart
    /\ NewCoordinator(stored, real, mode, IF WithCrash THEN mode ELSE restoreMode)

\* Restore data reaches disk every 15 minutes and at stop; between those, a
\* crash restores the select's mode from the last periodic write. The Store
\* record was written at once (see the scope note in the skill).
Crash == WithCrash /\ NewCoordinator(stored, real, restoreMode, restoreMode)

\* HA's periodic restore-state write.
RestoreDump == WithCrash /\ restoreMode # mode /\ restoreMode' = mode
    /\ UNCHANGED <<real, boot, skew, pos, latch, head, vote, mode, byVotes,
                   stamp, stored>> /\ UNCHANGED ghosts

Next == Decide \/ Reading \/ HandsOff \/ Tick \/ ClockStep \/ Select
        \/ Reload \/ Restart \/ Crash \/ RestoreDump

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* The rules.                                                              *)
(***************************************************************************)
\* After a real mode change, MXZ itself changes the heads' mode again only
\* once the hold has passed. Exempt, as documented: a change only hands-off
\* votes caused (and its undoing), and a person's pick. Across a restart the
\* hold is timed on the wall clock, so a step of it between the change and
\* a restart moves the end of the hold (the docs say so).
HoldKept == ~holdBroken

\* The hold is never longer than what is left of it: what the gate has
\* counted since the last stamp is at least the true time since, unless the
\* wall clock was stepped since that stamp.
NoExtraWait == stamp = NoTime \/ lastStamp = NoTime \/ steppedSinceStamp
               \/ Mono - stamp >= real - lastStamp

\* Whatever the wall clock did, the hold is never more than the full window.
NeverLonger == stamp = NoTime \/ stamp <= Mono

\* No change on record, nothing to wait out.
FreshNoWait == stored = NoTime => stamp = NoTime

\* A room whose head is running in the shared mode keeps that mode until the
\* room reaches its target, unless another room's real call for the other
\* mode, or a person, takes it. (A room that has only drifted past its band
\* in the other mode steps aside: it is not running.)
PausedRunKept == ~runBroken
=============================================================================
