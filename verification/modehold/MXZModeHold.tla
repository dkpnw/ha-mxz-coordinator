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
(* code _apply records the mode change (_write_back_mode) with the heads'  *)
(* commands queued and none of them run, then awaits the deliveries       *)
(* (DeliveryQueue.async_wait). Nothing awaited comes between the decision  *)
(* and its record, so a pick, a reading or a reload during the deliveries  *)
(* finds the record written. docs/formal/tla/MXZCoordinator.tla checks the *)
(* deliveries as steps of their own (WithSplit, ci-apply).                 *)
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
(*                                                                         *)
(* WithOpenings adds the door/window pause (OpeningPause.step):            *)
(* a room's window opens past its open delay (Open: paused), closes past   *)
(* its close delay (Close: resumed), or a person takes the paused room     *)
(* over (Override: its head, or its Window Override switch) until the      *)
(* window closes. The delays are the steps themselves. A paused room has   *)
(* no demand and no run (MXZCoordinator._compute) and its head is off.     *)
(* Its state is stored at once, so a reload or restart keeps it.          *)
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
    WithCrash,    \* Home Assistant dies without its stop (power cut, kill)
    WithOpenings  \* each room has a door/window pause

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
    byVotes,            \* coordinator._mode_by_votes
    stamp,              \* coordinator._last_mode_change_ts (monotonic), or NoTime
    stored,             \* the Store record MODE_CHANGE_KEY (wall clock), or NoTime
    storedMode,         \* the Store record MODE_KEY, or None
    storedAt,           \* the Store record MODE_AT_KEY (wall clock)
    storedByV,          \* the Store record MODE_BY_VOTES_KEY
    modeAt,             \* the shared-mode select state's last_changed (wall clock)
    restoreMode,        \* the shared-mode select's restore record
    restoreAt,          \* that record's last_changed
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
    runBroken,          \* a running room lost the mode with no call against it
    \* The door/window pause (openings.OpeningPause), kept across starts.
    paused,             \* each room: paused or closing (off, no vote)
    overridden,         \* each room: a person runs it with its window open
    enabled,            \* each room's own MXZ mode (its enable switch)
    pausedVoted,        \* ghost: a decision counted a paused room's call or run
    stampMoved          \* ghost: a pause, resume or override moved the hold

vars == <<real, boot, skew, pos, latch, head, vote, mode, byVotes, stamp,
          stored, storedMode, storedAt, storedByV, modeAt, restoreMode,
          restoreAt, applied, lastChange, steppedSince,
          restartedSince, voteOwned, picked, lastStamp, steppedSinceStamp,
          holdBroken, runBroken, paused, overridden, enabled, pausedVoted,
          stampMoved>>
ghosts == <<applied, lastChange, steppedSince, restartedSince, voteOwned,
            picked, lastStamp, steppedSinceStamp, holdBroken, runBroken>>
openings == <<paused, overridden, enabled, pausedVoted, stampMoved>>

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

\* logic.shared_mode over directions in rank order. `running` is
\* logic.arbitrate's: a room is engaged in `current`, so Resting waits.
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
    /\ storedMode \in {None} \cup Modes /\ storedByV \in BOOLEAN
    /\ byVotes \in BOOLEAN /\ voteOwned \in BOOLEAN
    /\ applied \in {None} \cup Modes /\ picked \in {None} \cup Modes
    /\ paused \in [RoomIds -> BOOLEAN] /\ overridden \in [RoomIds -> BOOLEAN]
    /\ enabled \in [RoomIds -> BOOLEAN]

Init ==
    /\ real = 0 /\ boot = 0 /\ skew = 0
    /\ pos \in [RoomIds -> Positions]
    /\ latch = [i \in RoomIds |-> ""]     \* construction has no prior run
    /\ head = [i \in RoomIds |-> ""]
    /\ vote = None
    /\ mode = "cool"                      \* cold start (_arbitration_mode)
    /\ byVotes = FALSE
    /\ stamp = NoTime /\ stored = NoTime  \* a fresh install: no change on record
    /\ storedMode = None /\ storedAt = 0 /\ storedByV = FALSE
    /\ modeAt = 0                         \* the select shows cool from the start
    /\ restoreMode = "cool" /\ restoreAt = 0
    /\ applied = None /\ lastChange = NoTime
    /\ steppedSince = FALSE /\ restartedSince = FALSE
    /\ voteOwned = FALSE /\ picked = None
    /\ lastStamp = NoTime /\ steppedSinceStamp = FALSE
    /\ holdBroken = FALSE /\ runBroken = FALSE
    /\ paused = [i \in RoomIds |-> FALSE] /\ overridden = [i \in RoomIds |-> FALSE]
    /\ enabled = [i \in RoomIds |-> TRUE]
    /\ pausedVoted = FALSE /\ stampMoved = FALSE

(***************************************************************************)
(* Decide: MXZCoordinator._compute then _apply.                            *)
(*   allowed     = elapsed is None or elapsed >= self.hysteresis           *)
(*   engaged     = any held engage is cool|heat                            *)
(*   grant       = logic.arbitrate(...); by_votes = grant.read_only, then  *)
(*                 the _mode_by_votes undo rule in _compute                *)
(*   running     = some held engage is the current mode (Resting waits)    *)
(*   _apply      : a head runs the mode when its engage is that mode       *)
(*                 (logic.head_action), else parks; then the stamp rules   *)
(*                 at the end of _apply; a changed mode re-renders the     *)
(*                 select, and every changed mode or stamp writes the      *)
(*                 Store record at once (_record_mode,                     *)
(*                 async_delay_save(..., 0)): time, mode, by-votes.        *)
(***************************************************************************)
Decide ==
    LET nl       == [i \in RoomIds |-> IF paused[i] THEN "" ELSE Engage(latch[i], pos[i])]
        dem      == [i \in RoomIds |-> IF paused[i] THEN None ELSE Demand(pos[i])]
        allowed  == stamp = NoTime \/ Mono - stamp >= H
        engaged  == \E i \in RoomIds : nl[i] \in Modes
        cmd      == SelectSeq(dem, LAMBDA d : d # None)
        gateOpen == Len(cmd) = 0 /\ ~engaged
        votes    == IF WithVote /\ gateOpen /\ vote # None THEN <<vote>> ELSE <<>>
        running  == \E i \in RoomIds : nl[i] = mode
        full     == SharedMode(cmd \o votes, mode, allowed, running)
        plain    == SharedMode(cmd, mode, allowed, running)
        readOnly == Len(votes) > 0 /\ full # plain /\ gateOpen
        calls    == \E i \in RoomIds : dem[i] \in Modes \/ nl[i] \in Modes
        byV      == readOnly \/ (byVotes /\ full # mode /\ ~calls)
        state    == full
        runs     == \E i \in RoomIds : dem[i] = state \/ nl[i] = state
        restOk   == Resting \notin Modes \/ state # Resting
        doStamp  == IF state # mode THEN ~byV ELSE byVotes /\ runs
        newByV   == IF state # mode THEN byV /\ restOk
                    ELSE IF byVotes /\ runs THEN FALSE ELSE byVotes
        write    == state # mode \/ doStamp
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
    /\ byVotes' = newByV
    /\ stamp' = IF doStamp THEN Mono ELSE stamp
    /\ stored' = IF doStamp THEN Wall ELSE stored
    /\ storedMode' = IF write THEN state ELSE storedMode
    /\ storedAt' = IF write THEN Wall ELSE storedAt
    /\ storedByV' = IF write THEN newByV ELSE storedByV
    /\ modeAt' = IF state # mode THEN Wall ELSE modeAt
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
    /\ pausedVoted' = (pausedVoted \/ \E i \in RoomIds :
                          paused[i] /\ (dem[i] # None \/ nl[i] \in Modes \/ head'[i] # ""))
    /\ UNCHANGED <<real, boot, skew, pos, vote, restoreMode, restoreAt,
                   paused, overridden, enabled, stampMoved>>

\* The room's reading changes (any jump: a window opens, a sensor reports).
Reading == \E i \in RoomIds, p \in Positions :
    /\ pos' = [pos EXCEPT ![i] = p]
    /\ UNCHANGED <<real, boot, skew, latch, head, vote, mode, byVotes, stamp,
                   stored, storedMode, storedAt, storedByV, modeAt, restoreMode, restoreAt>> /\ UNCHANGED ghosts
    /\ UNCHANGED openings

\* A person runs the hands-off head (its vote, coordinator._hands_off_vote).
HandsOff == WithVote /\ \E v \in {None} \cup Modes :
    /\ v # vote /\ vote' = v
    /\ UNCHANGED <<real, boot, skew, pos, latch, head, mode, byVotes, stamp,
                   stored, storedMode, storedAt, storedByV, modeAt, restoreMode, restoreAt>> /\ UNCHANGED ghosts
    /\ UNCHANGED openings

Tick == real < MaxReal /\ real' = real + 1
    /\ UNCHANGED <<boot, skew, pos, latch, head, vote, mode, byVotes, stamp,
                   stored, storedMode, storedAt, storedByV, modeAt, restoreMode, restoreAt>> /\ UNCHANGED ghosts
    /\ UNCHANGED openings

\* NTP steps the wall clock, or a person sets it. Monotonic does not move.
ClockStep == \E s \in Skews :
    /\ s # skew /\ skew' = s
    /\ steppedSince' = TRUE /\ steppedSinceStamp' = TRUE
    /\ UNCHANGED <<real, boot, pos, latch, head, vote, mode, byVotes, stamp,
                   stored, storedMode, storedAt, storedByV, modeAt, restoreMode, restoreAt, applied, lastChange,
                   restartedSince, voteOwned, picked, lastStamp, holdBroken,
                   runBroken>> /\ UNCHANGED openings

\* select.async_select_option shows the pick, then
\* coordinator.async_select_shared_mode: a changed direction stamps the hold
\* and writes the record.
Select == WithSelect /\ \E m \in Modes :
    /\ m # mode
    /\ mode' = m /\ byVotes' = FALSE /\ modeAt' = Wall
    /\ stamp' = Mono /\ stored' = Wall
    /\ storedMode' = m /\ storedAt' = Wall /\ storedByV' = FALSE
    /\ lastStamp' = real /\ steppedSinceStamp' = FALSE
    /\ picked' = m /\ lastChange' = real
    /\ steppedSince' = FALSE /\ restartedSince' = FALSE /\ voteOwned' = FALSE
    /\ UNCHANGED <<real, boot, skew, pos, latch, head, vote, restoreMode,
                   restoreAt, applied, holdBroken, runBroken>> /\ UNCHANGED openings

\* A new coordinator: __init__ (stamp from the record's time; mode and
\* _mode_by_votes from the record; latch ""), then the select's restore
\* (coordinator.restore_shared_mode: the record wins unless the restored
\* state disagrees and is newer, which then is written back to the record;
\* the select's state is written anew, so its last_changed is now), then
\* switch restore puts each room in _startup_resume and the first _compute
\* seeds its latch from the head's mode.
NewCoordinator(newBoot, selMode, selAt, newRestore, newRestoreAt) ==
    LET fromRec == storedMode \in Modes
                   /\ (selMode = storedMode \/ storedAt >= selAt)
        newMode == IF fromRec THEN storedMode ELSE selMode
        rewrite == storedMode \in Modes /\ ~fromRec
    IN
    /\ boot' = newBoot
    /\ stamp' = StartStamp(stored, Wall, real - newBoot)
    /\ byVotes' = IF fromRec THEN storedByV ELSE FALSE
    /\ latch' = head
    /\ mode' = newMode
    /\ modeAt' = Wall
    /\ storedMode' = IF rewrite THEN newMode ELSE storedMode
    /\ storedAt' = IF rewrite THEN Wall ELSE storedAt
    /\ storedByV' = IF rewrite THEN FALSE ELSE storedByV
    /\ restoreMode' = newRestore /\ restoreAt' = newRestoreAt
    /\ restartedSince' = TRUE
    /\ UNCHANGED <<real, skew, pos, head, vote, stored, applied, lastChange,
                   steppedSince, voteOwned, picked, lastStamp,
                   steppedSinceStamp, holdBroken, runBroken>>
    \* The door/window record is stored at once on every change
    \* (OpeningPause._save) and read by the next start (async_load_openings).
    /\ UNCHANGED openings

\* __init__.async_reload: same process. The Store instance is shared
\* (_mode_change_store), and the select restores the state it had.
Reload == WithReload /\ NewCoordinator(boot, mode, modeAt, restoreMode, restoreAt)

\* A stop writes restore data (the select's state) and flushes the Store.
\* (Restore data is tracked only where a crash can read it.)
Restart == WithRestart
    /\ NewCoordinator(real, mode, modeAt, IF WithCrash THEN mode ELSE restoreMode,
                      IF WithCrash THEN modeAt ELSE restoreAt)

\* Restore data reaches disk every 15 minutes and at stop; between those, a
\* crash restores the select's state from the last periodic write. The
\* Store record was written at once (see the scope note in the skill).
Crash == WithCrash
    /\ NewCoordinator(real, restoreMode, restoreAt, restoreMode, restoreAt)

\* HA's periodic restore-state write.
RestoreDump == WithCrash /\ <<restoreMode, restoreAt>> # <<mode, modeAt>>
    /\ restoreMode' = mode /\ restoreAt' = modeAt
    /\ UNCHANGED <<real, boot, skew, pos, latch, head, vote, mode, byVotes,
                   stamp, stored, storedMode, storedAt, storedByV, modeAt>> /\ UNCHANGED ghosts
    /\ UNCHANGED openings

(***************************************************************************)
(* The door/window pause (OpeningPause.step, logic.opening_state).         *)
(* None of these is a mode change: each leaves the mode, the hold and the  *)
(* record alone (stampMoved watches that). The room's own mode, its enable *)
(* switch, is never touched by any of them.                                *)
(***************************************************************************)
\* Open past the open delay: paused. Never while a person overrides it.
Open == WithOpenings /\ \E i \in RoomIds :
    /\ ~paused[i] /\ ~overridden[i]
    /\ paused' = [paused EXCEPT ![i] = TRUE]
    /\ stamp' = stamp        \* a pause is not a mode change
    /\ enabled' = enabled    \* nor does it touch the room's own mode
    /\ UNCHANGED <<real, boot, skew, pos, latch, head, vote, mode, byVotes,
                   stored, storedMode, storedAt, storedByV, modeAt, restoreMode,
                   restoreAt, overridden, pausedVoted>> /\ UNCHANGED ghosts
    /\ stampMoved' = (stampMoved \/ stamp' # stamp)

\* Closed past the close delay (a paused room resumes), or closed at all (an
\* override ends, so the next opening pauses again).
Close == WithOpenings /\ \E i \in RoomIds :
    /\ paused[i] \/ overridden[i]
    /\ paused' = [paused EXCEPT ![i] = FALSE]
    /\ overridden' = [overridden EXCEPT ![i] = FALSE]
    /\ stamp' = stamp        \* a resume is not a mode change either
    /\ UNCHANGED <<real, boot, skew, pos, latch, head, vote, mode, byVotes,
                   stored, storedMode, storedAt, storedByV, modeAt, restoreMode,
                   restoreAt, enabled, pausedVoted>> /\ UNCHANGED ghosts
    /\ stampMoved' = (stampMoved \/ stamp' # stamp)

\* A person changes the paused head, or turns on its Window Override.
Override == WithOpenings /\ \E i \in RoomIds :
    /\ paused[i]
    /\ paused' = [paused EXCEPT ![i] = FALSE]
    /\ overridden' = [overridden EXCEPT ![i] = TRUE]
    /\ stamp' = stamp        \* nor is a takeover
    /\ UNCHANGED <<real, boot, skew, pos, latch, head, vote, mode, byVotes,
                   stored, storedMode, storedAt, storedByV, modeAt, restoreMode,
                   restoreAt, enabled, pausedVoted>> /\ UNCHANGED ghosts
    /\ stampMoved' = (stampMoved \/ stamp' # stamp)

Next == Decide \/ Reading \/ HandsOff \/ Tick \/ ClockStep \/ Select
        \/ Reload \/ Restart \/ Crash \/ RestoreDump
        \/ Open \/ Close \/ Override

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

\* A paused room never contributes a vote: no demand, no run, head off, in
\* every decision made while it is paused.
PausedNeverVotes == ~pausedVoted

\* A pause, a resume or an override never starts, resets or shortens the
\* hold. (HoldKept, with them in the model, is the rest: no change comes
\* inside the hold whatever they do.)
PauseKeepsHold == ~stampMoved

\* An overridden room is never paused; only a closed reading (Close) lets
\* the next opening pause it again.
OverrideNeverPaused == \A i \in RoomIds : ~(paused[i] /\ overridden[i])

\* A pause never sets a room's own MXZ mode to Off.
PauseKeepsMode == \A i \in RoomIds : enabled[i]
=============================================================================
