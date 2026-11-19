"""The planner: what one compute decides from what it read. No Home Assistant.

``MXZCoordinator._compute`` reads each room's sensor, head and controllers, hands the
readings to :func:`decide` as one :class:`RoomInput` per room and one :class:`Shared`,
and builds the plan from the :class:`Decision`. ``_apply`` does what :func:`apply_plan` yields,
one part at a time: each head's gates, its idle park (:func:`idle_for`) and
:func:`head_command` as :class:`HeadStep` values, then the plan's mode written back as
:func:`mode_write_back` says, stamped by :func:`stamp_mode_change`. Each head's driver then runs that command's
:func:`delivery_steps`.
Time comes in as numbers: the seconds since the last mode change, the hold's length,
the coil-dry clock, and the clocks a start reads (:func:`mode_change_stamp`).
The engage latch and the startup seeds come in as they are and go out advanced; nothing
here keeps state.
"""

from __future__ import annotations

from collections.abc import (
    Callable,
    Collection,
    Hashable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import asdict, dataclass, replace
from typing import Any

from .const import (
    ADOPT_DELAY,
    BAND_DRIFT_DELAY,
    BANNED_MODES,
    DEMAND_NEUTRAL,
    ENGAGE_SATISFIED,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
    OFF_WHILE_ENABLED_DELAY,
    UNAVAILABLE_STATES,
    VOTE_EXPIRED,
)
from .grid import Grid
from .head_writes import HEAD_WRITE_MODE, write_is_own
from .logic import (
    PRIORITY_DEMAND,
    PRIORITY_FLOOR_HEAT,
    PRIORITY_SAFETY,
    PRIORITY_VOTE,
    STATE_UNAVAILABLE,
    Demand,
    HeadWriteMemory,
    arbitrate,
    as_float,
    comfort_boost,
    engage_with_latch,
    head_action,
    range_delta,
    room_call,
    setpoints,
)
from .plan import Plan, RoomPlan
from .timing import (
    Arm,
    Refresh,
    coil_dry_delay,
    head_refresh,
    head_shows_new,
    hold_end_arm,
    run_changed,
)
from .units import clamp


@dataclass(frozen=True, slots=True)
class CallLimits:
    """The lockouts and eco every room's call reads, as :func:`logic.room_call` takes them."""

    eco: bool
    eco_cool_max: float
    eco_heat_min: float
    heat_lockout: bool
    heat_lockout_floor: float
    cool_lockout: bool
    cool_lockout_ceiling: float


@dataclass(frozen=True, slots=True)
class RoomInput:
    """One room as this compute read it."""

    slug: str
    enabled: bool
    # The reading, or the fallback when it was rejected; sensor_ok says which.
    temp: float
    # A valid reading that is not stale.
    sensor_ok: bool
    target: float
    # Each side's target: Heat to and Cool to, or the one target twice.
    heat_target: float
    cool_target: float
    heat_drift: float
    cool_drift: float
    # The head's reported state; None when it has none.
    head: str | None
    paused: bool
    dry: bool
    # A tripped floor's or ceiling's want.
    floor: str | None
    safety: bool
    # A released hands-off head's vote.
    vote: str | None
    # Whether the room's own demand and run still count (see holds()).
    holds: bool
    # Comfort priority (0 = none).
    boost: float


@dataclass(frozen=True, slots=True)
class Shared:
    """What every room's decision shares this compute."""

    limits: CallLimits
    demand_threshold: float
    # The shared mode now.
    mode: str
    # Seconds since the last real mode change; None when none is on record.
    elapsed: float | None
    # The hold: the least time between two real mode changes.
    hold: float
    resting: str | None
    # Whether hands-off votes alone set the shared mode.
    by_votes: bool
    # Coordinator on, and on before the start: a startup seed may resume a run.
    resume: bool
    accept_head_changes: bool


@dataclass(frozen=True, slots=True)
class RoomDecision:
    demand: str
    engage: str
    # The demand and engage arbitration counts (see holds()).
    held_demand: str
    held_engage: str
    dry_vote: str | None


@dataclass(frozen=True, slots=True)
class Decision:
    rooms: tuple[RoomDecision, ...]
    # The engage latch and startup seeds, advanced by this compute.
    latch: dict[str, str]
    startup: dict[str, str | None]
    # A room let go of a run this compute.
    coasted: bool
    state: str
    by_votes: bool
    by_safety: bool
    by_comfort: bool
    by_floor: bool
    standoff: bool
    allowed: bool


def arbitration_mode(mode: str) -> str:
    """The shared mode arbitration starts from (cold start / junk -> cool)."""
    return mode if mode in (MODE_COOL, MODE_HEAT) else MODE_COOL


def demand_bands(threshold: float, heat_drift: float, cool_drift: float) -> tuple[float, float]:
    """How far a room may sit off each side's target before it calls.

    A room's demand vote respects its OWN drift (#18): a room told to tolerate a
    wide band must not steer the shared compressor inside it. max() reproduces
    the threshold exactly whenever drift <= threshold (the default). Per side, so
    a wide "warmer by" also holds back cool demand.
    """
    return max(threshold, heat_drift), max(threshold, cool_drift)


def holds(status: str | None, safety: bool, cloud_out: bool = False) -> bool:
    """Whether a room's own demand and run still count in arbitration.

    Not once its head's vote has expired: a long-lost head must not hold
    the shared mode for its room forever while a sibling waits. Kept as in
    3.10.0, pending a maintainer decision: a room marked Safety priority with its
    sensor on (its demand is a safety want), and a tripped floor or
    ceiling (its own entry, never dropped here). Likewise not while a
    cloud head is out of MXZ's control (``cloud_out``): MXZ can't run it,
    so its room's call would only hold the outdoor unit for nothing. A
    Safety priority room still calls from its room sensor.
    """
    return safety or (status != VOTE_EXPIRED and not cloud_out)


def arbitration_entries(
    *, demand: str, vote: str | None, safety: bool, index: int,
    floor: str | None = None, dry: str | None = None, boost: float = 0.0,
) -> tuple[Demand, ...]:
    """One zone's (demand, vote[, dry][, floor]) as :class:`Demand` entries.

    ``boost`` is the room's Comfort priority. It goes on the demand, the
    vote and the dry vote, never on a floor or ceiling entry, and
    :func:`logic.arbitrate` ignores it on a Safety priority entry.

    Shared by :func:`decide` (live) and the coordinator's dwell wakeup
    (replayed from the already-computed plan) so the priority-tier math lives
    in exactly one place. A dry head's cool (Allow Dry mode) is one more vote,
    always at PRIORITY_VOTE: the room's Safety priority sensor never lifts it. A
    tripped floor is a commanding entry: a freeze floor's heat at
    PRIORITY_FLOOR_HEAT, a ceiling's cool at PRIORITY_SAFETY (see
    :func:`logic.arbitrate`). It is independent of the room's own Safety
    priority sensor.
    """
    tier = PRIORITY_SAFETY if safety else None
    pair: tuple[Demand, ...] = (
        Demand(
            direction=demand if demand in (MODE_COOL, MODE_HEAT) else None,
            priority=(tier if tier is not None else PRIORITY_DEMAND, index),
            boost=boost,
        ),
        Demand(
            direction=vote,
            priority=(tier if tier is not None else PRIORITY_VOTE, index),
            read_only=True,
            boost=boost,
        ),
    )
    if dry is not None:
        pair = (
            *pair,
            Demand(
                direction=dry, priority=(PRIORITY_VOTE, index), read_only=True,
                boost=boost,
            ),
        )
    if floor not in (MODE_COOL, MODE_HEAT):
        return pair
    floor_tier = PRIORITY_FLOOR_HEAT if floor == MODE_HEAT else PRIORITY_SAFETY
    return (*pair, Demand(direction=floor, priority=(floor_tier, index)))


def held_back(rooms: Sequence[RoomPlan], *, current: str, resting: str | None) -> bool:
    """Whether the mode-change hold keeps the shared mode from changing: with the hold open,
    arbitration would pick another mode than ``current`` from these rooms.

    The rooms are a computed plan's, replayed as :func:`decide` counted them (:func:`holds`),
    so the dwell wakeup weighs exactly what that compute arbitrated.
    """
    demands: list[str] = []
    engages: list[str] = []
    for room in rooms:
        if holds(room.vote_status, room.safety_priority, room.cloud_out):
            demands.append(room.demand)
            engages.append(room.engage)
        else:
            demands.append(DEMAND_NEUTRAL)
            engages.append(room.engage if room.floor_want is not None else ENGAGE_SATISFIED)
    engaged = any(e in (MODE_COOL, MODE_HEAT) for e in engages)
    entries: list[Demand] = []
    for i, (room, demand) in enumerate(zip(rooms, demands)):
        entries.extend(
            arbitration_entries(
                demand=demand,
                vote=room.hands_off_vote,
                safety=room.safety_priority,
                index=i,
                floor=room.floor_want,
                dry=room.dry_vote,
                boost=room.comfort_priority,
            )
        )
    return arbitrate(
        entries, engaged=engaged, current=current, allowed=True,
        resting=resting, running=current in engages,
    ).direction != current


def _seed_latch(
    room: RoomInput, shared: Shared, latch: dict[str, str], startup: dict[str, str | None]
) -> None:
    """Put a resumed or retargeted room's run in the latch before its engage is decided.

    A start resumes each room's run from its head (``startup``), once the head
    reports and the room has a usable reading; missing startup evidence is kept
    apart from a known idle run. An enabled room with no latch (its target
    changed) takes the run its head shows. With changes at the head accepted, a
    head a person flipped off the shared mode is no run of MXZ's: its heat/cool
    isn't adopted.
    """
    head = room.head
    seen = head is not None and head not in UNAVAILABLE_STATES
    if room.slug in startup:
        if not (room.enabled and shared.resume):
            startup.pop(room.slug)
        else:
            if startup[room.slug] is None and seen:
                startup[room.slug] = head if head in (MODE_COOL, MODE_HEAT) else ""
            seed = startup[room.slug]
            if room.sensor_ok and seed is not None and seen:
                latch[room.slug] = seed
                startup.pop(room.slug)
    elif room.slug not in latch and room.enabled:
        latch[room.slug] = (
            head
            if head in (MODE_COOL, MODE_HEAT)
            and (not shared.accept_head_changes or head == shared.mode)
            else ""
        )


def decide(
    rooms: Sequence[RoomInput],
    shared: Shared,
    latch: Mapping[str, str],
    startup: Mapping[str, str | None],
) -> Decision:
    """Each room's demand and engage, and the shared mode they arbitrate to.

    It advances each room's engage latch and any pending startup seed, and
    returns them with the decision.
    """
    latch = dict(latch)
    startup = dict(startup)
    limits = asdict(shared.limits)
    decided: list[RoomDecision] = []
    entries: list[Demand] = []
    coasted = False
    for i, room in enumerate(rooms):
        heat_band, cool_band = demand_bands(shared.demand_threshold, room.heat_drift, room.cool_drift)
        demand = room_call(
            temp=room.temp, target=room.target, enabled=room.enabled,
            heat_target=room.heat_target, cool_target=room.cool_target,
            sensor_ok=room.sensor_ok,
            heat_band=heat_band,
            cool_band=cool_band,
            neutral=DEMAND_NEUTRAL,
            **limits,
        )
        _seed_latch(room, shared, latch, startup)
        prior_engage = latch.get(room.slug, "")
        engage = engage_with_latch(
            prior=prior_engage or None,
            temp=room.temp, target=room.target, enabled=room.enabled,
            heat_target=room.heat_target, cool_target=room.cool_target,
            sensor_ok=room.sensor_ok, heat_band=room.heat_drift, cool_band=room.cool_drift,
            neutral=ENGAGE_SATISFIED,
            **limits,
        )
        if room.slug in latch:
            latch[room.slug] = engage if engage in (MODE_COOL, MODE_HEAT) else ""
        # Door/window pause: a paused room has no call and no run. It never
        # votes, so it can neither keep nor turn the shared mode, and it doesn't
        # hold Resting mode off; its run starts afresh on resume.
        if room.paused:
            demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED
            if room.slug in latch:
                latch[room.slug] = ""
            prior_engage = ""
        # Allow Dry mode (never while paused): a person runs this head in dry,
        # so the room's temperature goal pauses (no call, no run). Its vote is
        # the dry below. Out of dry it starts afresh.
        dry = room.dry and not room.paused
        if dry:
            demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED
            if room.slug in latch:
                latch[room.slug] = ""
            prior_engage = ""
        # This room let go of a run and is now coasting: it is parked at the
        # idle action, and only a later compute may engage it again. A disabled
        # room (MODE_OFF) is not coasting.
        if prior_engage and engage == ENGAGE_SATISFIED:
            coasted = True
        # A tripped floor is this disabled room's engage (the latch stays
        # untouched: it belongs to the room's own enabled runs).
        if room.floor is not None:
            engage = room.floor
        # A dry head runs the outdoor unit the cooling way: a cool vote at
        # hands-off rank, whatever the room's Safety priority sensor says, and
        # none at all under a cool lockout.
        dry_vote = MODE_COOL if dry and not shared.limits.cool_lockout else None
        held_demand = demand if room.holds else DEMAND_NEUTRAL
        held_engage = engage if room.holds or room.floor is not None else ENGAGE_SATISFIED
        entries.extend(
            arbitration_entries(
                demand=held_demand, vote=room.vote, safety=room.safety, index=i,
                floor=room.floor, dry=dry_vote, boost=room.boost,
            )
        )
        decided.append(RoomDecision(demand, engage, held_demand, held_engage, dry_vote))

    current = arbitration_mode(shared.mode)
    held_engages = [room.held_engage for room in decided]
    allowed = shared.elapsed is None or shared.elapsed >= shared.hold
    engaged = any(e in (MODE_COOL, MODE_HEAT) for e in held_engages)
    # A room still running to its target in the shared mode keeps it from
    # Resting mode until it gets there (logic.arbitrate).
    running = current in held_engages
    grant = arbitrate(
        entries,
        engaged=engaged,
        current=current,
        allowed=allowed,
        resting=shared.resting,
        running=running,
    )
    # Comfort priority earned the answer only if the same compute with
    # every boost at 0 answers differently. Run only when one counts.
    by_comfort = any(comfort_boost(d) > 0 for d in entries) and arbitrate(
        [replace(d, boost=0.0) for d in entries],
        engaged=engaged,
        current=current,
        allowed=allowed,
        resting=shared.resting,
        running=running,
    ).direction != grant.direction
    state, by_votes = grant.direction, grant.read_only
    if (
        not by_votes and shared.by_votes and state != current
        and not any(
            c in (MODE_COOL, MODE_HEAT)
            for c in (*(room.demand for room in decided), *(room.engage for room in decided))
        )
    ):
        by_votes = True
    floors = [room.floor for room in rooms]
    calls = [
        *(room.held_demand for room in decided),
        *(room.vote for room in rooms),
        *(room.dry_vote for room in decided),
        *floors,
    ]
    return Decision(
        rooms=tuple(decided),
        latch=latch,
        startup=startup,
        coasted=coasted,
        state=state,
        by_votes=by_votes,
        by_safety=grant.by_safety,
        by_comfort=by_comfort,
        by_floor=state in floors,
        # Both directions physically asked of the one outdoor unit, a hands-off
        # head's and a floor's included (a room whose head's vote expired asks
        # nothing any more).
        standoff=MODE_COOL in calls and MODE_HEAT in calls,
        allowed=allowed,
    )


@dataclass(frozen=True, slots=True)
class ModeWriteBack:
    """What one plan's mode does to the shared mode and its record."""

    mode: str
    by_votes: bool
    # The shared mode changed: the shared-mode select re-renders.
    changed: bool
    # Start the hold.
    stamp: bool
    # Write the record.
    record: bool


def mode_write_back(
    mode: str,
    by_votes: bool,
    *,
    state: str,
    by_hands_off: bool,
    calls: Collection[str],
    resting: str | None,
) -> ModeWriteBack:
    """The shared mode ``mode`` (set by votes alone if ``by_votes``) after a plan for ``state``.

    ``calls`` is every room's engage and demand in that plan. Stamp the flip only on
    a real mode change (cool<->heat). A flip only hands-off heads asked for is not
    one the coordinator caused, so it starts no dwell (see logic.arbitrate()). Once
    a coordinated room runs in that mode, the mode is ours: stamp it then, as if
    that room had flipped it, so the next reversal is dwelled. The vote's undoing
    (back to the resting bias) starts no dwell either, but leaves a mode that owes
    the vote nothing: a run there is no flip.
    """
    if state != mode:
        return ModeWriteBack(
            mode=state,
            by_votes=by_hands_off and state != resting,
            changed=True,
            stamp=not by_hands_off,
            record=True,
        )
    if by_votes and state in calls:
        return ModeWriteBack(
            mode=mode,
            by_votes=False,
            changed=False,
            stamp=True,
            record=True,
        )
    return ModeWriteBack(mode=mode, by_votes=by_votes, changed=False, stamp=False, record=False)


def mode_change_stamp(stored: float | None, now: float, wall: float) -> float | None:
    """The stamp on MXZ's clock, read as ``now``, for a stored wall-clock change time.

    ``wall`` is the wall clock read at the same start. The gate itself runs on
    the monotonic clock (an NTP step inside one run must not move it), so the
    stored time becomes "this many seconds ago" on that clock once, at startup.
    A stored time in the future (the clock was set back, or it ran ahead when
    the change was written) counts as now: the hold is then the full window
    from this start, never longer.
    """
    if stored is None:
        return None
    ago = max(0.0, wall - stored)
    return now - ago


@dataclass(frozen=True, slots=True)
class ModeStamp:
    """A real mode change, stamped: ``at`` on the monotonic clock and ``wall`` on the wall
    clock, and the hold-end wakeup it arms (None: a retired coordinator arms none)."""

    at: float
    wall: float
    hold_end: Arm | None


def stamp_mode_change(
    clock: Callable[[], float], wall: Callable[[], float], *, retired: bool, hold: float
) -> ModeStamp:
    """A real mode change happens now (:attr:`ModeWriteBack.stamp`): the hold starts.

    ``clock`` and ``wall`` read the clocks, in that order. The hold-end wakeup is armed for the
    hold left as the clock reads it again, the comparison the gate makes (``hold``, the
    hold's length). Its end only redraws the thermostats.
    """
    at = clock()
    when = wall()
    if retired:
        return ModeStamp(at=at, wall=when, hold_end=None)
    return ModeStamp(
        at=at, wall=when, hold_end=hold_end_arm(max(0.0, hold - (clock() - at)), retired=False),
    )


@dataclass(frozen=True, slots=True)
class SetpointSettings:
    """How a head's setpoints are built, in the system unit (see logic.setpoints)."""

    clamp_min: float
    clamp_max: float
    band: float
    step: float
    eco_cool: tuple[float, float]
    eco_heat: tuple[float, float]


@dataclass(frozen=True, slots=True)
class HeadCommand:
    """What one apply queues for a head."""

    mode: str
    low: float
    high: float
    # How far the room sits outside its range: the fan boost's distance.
    delta: float
    # The engage the delivery's fan step follows; None for a floor run, which
    # never writes the head's fan.
    fan: str | None


@dataclass(frozen=True, slots=True)
class LastRun:
    """The head's last run becomes ``run`` (its act and the clock reading it was sent at): the
    coil-dry memory the coil-dry run counts from."""

    run: tuple[str, float]


@dataclass(frozen=True, slots=True)
class DryNotice:
    """People are told, once, that the head's Dry is parked for another room's heat."""


@dataclass(frozen=True, slots=True)
class FanRead:
    """The head's fan is read for ``act`` (``fan_latch.fan_read``): MXZ sends it no fan, and a
    person's pick on it is kept."""

    act: str


@dataclass(frozen=True, slots=True)
class HandBack:
    """A released hands-off room's hand-back, if one is owed, is queued (P3)."""


@dataclass(frozen=True, slots=True)
class Landed:
    """The parking hands-off head has landed: it is someone else's now."""


@dataclass(frozen=True, slots=True)
class Queue:
    """``command`` is queued for the head; ``taken``: the floor expects it over a person's run."""

    command: HeadCommand
    taken: bool = False


# What a parking read (IdlePark.steps) or a delivery's note (DeliverySteps.note) does to a
# head's coil-dry memory: its last run kept, its coil-dry wakeup armed or cancelled.
type MemoryStep = LastRun | Arm
# One thing an apply does at one head (HeadApply.steps).
type HeadStep = MemoryStep | DryNotice | FanRead | HandBack | Landed | Queue


def idle_park(
    idle_action: str,
    last_run: tuple[str, float] | None,
    coil_dry_seconds: float,
    clock: float,
) -> tuple[str, float | None]:
    """The parking mode (fan_only|off) for a non-running head now, and the seconds
    its coil-dry run has left (None when none is owed).

    ``off_after_dry`` resolves per head from its last run (``last_run``, the mode
    and the clock reading it was sent at): off immediately unless the head was
    actively COOLING within the coil-dry window (a wet coil sealed behind closed
    vanes is what grows the smell), in which case it dwells in fan_only until the
    coil-dry clock runs out. The dwell is a clock comparison, so a restart can
    never strand a head in fan_only.
    """
    if idle_action == IDLE_ACTION_OFF:
        return MODE_OFF, None
    if idle_action != IDLE_ACTION_OFF_AFTER_DRY:
        return MODE_FAN_ONLY, None
    if last_run is None or last_run[0] != MODE_COOL:
        return MODE_OFF, None  # heat leaves no wet coil; never-ran owes no dwell
    remaining = coil_dry_seconds - (clock - last_run[1])
    if remaining <= 0:
        return MODE_OFF, None
    return MODE_FAN_ONLY, remaining


def rebuilt_last_run(shown: str | None, clock: float) -> tuple[str, float] | None:
    """A head's last run after a restart, rebuilt from the mode it shows (``shown``).

    A head seen still cooling was cooling until the restart; one seen in fan_only may be
    mid-dwell. Both owe the full coil-dry run from now, erring a few minutes toward coil dry,
    never stranding the head (the clock runs out regardless). Off, heat, no state and
    unavailable owe nothing.
    """
    if shown in (MODE_COOL, MODE_FAN_ONLY):
        return (MODE_COOL, clock)
    return None


@dataclass(frozen=True, slots=True)
class IdlePark:
    """A non-running head's parking mode and what it does to the coil-dry memory.

    ``seeded``: the last run rebuilt from what the head shows, to keep. ``arm``: the coil-dry
    wakeup to arm, None when none is owed or one is already armed.
    """

    idle: str
    seeded: tuple[str, float] | None
    arm: Arm | None

    @property
    def steps(self) -> tuple[MemoryStep, ...]:
        """What reading this park does, in order: the rebuilt memory kept, then the wakeup."""
        return (
            *((LastRun(self.seeded),) if self.seeded is not None else ()),
            *((self.arm,) if self.arm is not None else ()),
        )


def idle_for(
    head: Hashable,
    idle_action: str,
    last_run: tuple[str, float] | None,
    shown: str | None,
    coil_dry_seconds: float,
    clock: float,
    *,
    dry_waiting: bool,
) -> IdlePark:
    """:func:`idle_park` for one head (key ``head``), with its memory rebuilt after a restart.

    For ``off_after_dry`` a head with no last run on record has it rebuilt from what it shows
    (``rebuilt_last_run``). A coil-dry run with time left gets one wakeup just after its end,
    unless one is armed (``dry_waiting``): the stamp doesn't move while idle, and the wakeup
    only makes the end prompt.
    """
    seeded = None
    if last_run is None and idle_action == IDLE_ACTION_OFF_AFTER_DRY:
        seeded = last_run = rebuilt_last_run(shown, clock)
    idle, coil_dry = idle_park(idle_action, last_run, coil_dry_seconds, clock)
    arm = None
    if coil_dry is not None and not dry_waiting:
        arm = Arm("dry", head, coil_dry_delay(coil_dry))
    return IdlePark(idle=idle, seeded=seeded, arm=arm)


def head_command(
    *,
    engage: str,
    floor_run: tuple[str, float] | None,
    temp: float | None,
    enabled: bool,
    mode: str,
    idle: str,
    eco: bool,
    target: float,
    heat_target: float,
    cool_target: float,
    taken: tuple[float, float] | None,
    settings: SetpointSettings,
) -> HeadCommand:
    """The mode, setpoints, range delta and fan step one head is sent for the plan.

    ``engage`` and ``temp`` are the plan's room (its engage, and the reading it
    shows: None when rejected); ``mode`` is the plan's shared mode and ``idle`` the
    head's parking mode (:func:`idle_park`). ``floor_run`` is a tripped floor's
    want and its clear line; ``taken`` the low and high of a person's own run in
    the floor's direction that the run took over (P10).
    """
    if not enabled:
        # A disabled room never idles in fan_only: a floor waiting out the mode
        # is held off. (Without a floor, engage is off and decides first.)
        idle = MODE_OFF
    act = head_action(engage=engage, mode=mode, eco=eco, idle=idle)
    low, high = setpoints(
        mode=mode,
        # A floor runs the head to its clear line, never the eco band.
        target=target if floor_run is None else floor_run[1],
        # Heat to while the shared mode is heat, Cool to while it's cool (the
        # one target, twice, in a single-target room). A floor's clear line is
        # its own target.
        heat_target=heat_target if floor_run is None else None,
        cool_target=cool_target if floor_run is None else None,
        eco=eco and floor_run is None,
        clamp_min=settings.clamp_min,
        clamp_max=settings.clamp_max,
        band=settings.band,
        step=settings.step,
        eco_cool=settings.eco_cool,
        eco_heat=settings.eco_heat,
    )
    if floor_run is not None and taken is not None:
        # A run taken over from a person's own heat keeps their setpoints where
        # those are warmer than the clear line's (cooler, for a ceiling over
        # their cool) (P10): their latest, if they changed them during the run
        # (P15).
        keep = max if floor_run[0] == MODE_HEAT else min
        low, high = keep(low, taken[0]), keep(high, taken[1])
    # How far the room sits outside its range (off its target, for a single
    # target, where the range is the target itself).
    delta = 0.0 if temp is None else range_delta(temp=temp, low=heat_target, high=cool_target)
    # A floor delivery takes the fan-free path (engage None, as the standby park
    # does): mode and setpoint only, so a manual fan pick is kept and MXZ never
    # writes this head's fan.
    return HeadCommand(
        mode=act, low=low, high=high, delta=delta, fan=engage if floor_run is None else None
    )


@dataclass(frozen=True, slots=True)
class CoilDryMemory:
    """A head's coil-dry memory: its last run (None when none is kept) and whether its coil-dry
    wakeup is armed."""

    last_run: tuple[str, float] | None
    waiting: bool


@dataclass(frozen=True, slots=True)
class ApplyHead:
    """One room's head as an apply reads it, when the apply reaches it.

    ``settling`` is whether a change made at the head still holds it (``holds_change``) and
    ``coil_dry`` the head's coil-dry memory. Both are called only when the apply reaches them,
    in that order: reading ``settling`` can rebuild that memory and arm its wakeup (``planned``
    reads the head's idle park), and a gate before either leaves both unread. ``head`` is the
    state the head shows (None for none), ``hands_off`` the room's hands-off phase,
    ``released`` whether its parking head has landed, ``busy`` whether a delivery runs on it,
    ``startup`` whether its startup seed waits. ``floor_clear`` is a tripped floor's clear line
    (None without a floor want) and ``taken_over`` the person's run a floor run took over.
    """

    climate_id: str
    vane_kick: bool
    settling: Callable[[], bool]
    paused: bool
    head: str | None
    hands_off: str | None
    released: bool
    busy: bool
    startup: bool
    enabled: bool
    target: float
    heat_target: float
    cool_target: float
    floor_clear: float | None
    taken_over: tuple[str, float, float] | None
    coil_dry: Callable[[], CoilDryMemory]


@dataclass(frozen=True, slots=True)
class ApplyShared:
    """What every head's apply reads.

    ``standby`` is the mode a fixed standby park sends every head (None when none parks them).
    ``mode``, ``by_votes`` and ``resting`` are what the write-back reads: the shared mode now,
    whether votes alone set it, and Resting mode.
    """

    retired: bool
    coordinator: bool
    standby: str | None
    inhibited: bool
    eco: bool
    idle_action: str
    coil_dry_seconds: float
    clock: float
    settings: SetpointSettings
    mode: str
    by_votes: bool
    resting: str | None


@dataclass(frozen=True, slots=True)
class HeadApply:
    """What one apply does at one head: ``steps``, performed in order.

    ``skip`` says why the head gets nothing more than its steps (``vane_kick``, ``settling``,
    ``unavailable``: a paused head or a startup seed with no state, ``handback``: a released
    hands-off room's hand-back, if any, is all it gets, ``landed``: a parking hands-off head is
    off, ``dry``: a person's Dry is held); None for a head that is sent its command.
    """

    climate_id: str
    skip: str | None = None
    steps: tuple[HeadStep, ...] = ()


_VALID_ENGAGES = (MODE_COOL, MODE_HEAT, ENGAGE_SATISFIED, MODE_OFF)
# A paused head's off: the queue call _apply made, with no delta and no fan step.
_PAUSE_OFF = HeadCommand(mode=MODE_OFF, low=0.0, high=0.0, delta=0.0, fan=None)


def _head_apply(
    room: RoomPlan, head: ApplyHead, state: str, shared: ApplyShared
) -> HeadApply:
    """One head's part of an apply: its gates in order, then its idle park and command."""
    cid = head.climate_id
    if head.vane_kick:
        return HeadApply(cid, skip="vane_kick")  # mid vane-kick: left alone until it finishes
    if head.settling():
        return HeadApply(cid, skip="settling")  # a change made at the head is holding
    if head.paused:
        # Door/window pause: straight to off (no coil-dry run, no fan, setpoint or vane
        # write), and only to a head that has a state. A person's fan pick on the off head is
        # still read, so it is kept for the resume.
        if head.head is None or head.head in UNAVAILABLE_STATES:
            return HeadApply(cid, skip="unavailable")
        return HeadApply(cid, steps=(FanRead(MODE_OFF), Queue(_PAUSE_OFF)))
    if head.hands_off in ("parking", "released") and not shared.inhibited:
        # (A floor run skips all of this: the head is MXZ's for the run. Any standby hold,
        # eco included, holds it off like any disabled room.)
        if head.released:
            # A floor run ended on a head a person was already running that way: theirs
            # again, as they had it (P3). Its off landed: the head is someone else's now.
            return HeadApply(cid, skip="handback", steps=(HandBack(),))
        if head.head == MODE_OFF and not head.busy:
            return HeadApply(cid, skip="landed", steps=(Landed(),))
        # Otherwise the ordinary disabled path below sends off again.
    if head.startup and (head.head is None or head.head in UNAVAILABLE_STATES):
        # Do not make up the missing startup mode with a park or write of MXZ's. A known
        # head still follows sensor-failure parking.
        return HeadApply(cid, skip="unavailable")
    memory = head.coil_dry()
    last_run = memory.last_run
    steps: list[HeadStep] = []
    dry = room.dry_state
    if dry is not None:
        if head.head == MODE_DRY:
            # Dry wets the coil as cooling does: whenever the head leaves dry, the coil-dry
            # run is owed from then. (A Dry picked on the thermostat and not reported yet
            # hasn't.)
            last_run = (MODE_COOL, shared.clock)
            steps.append(LastRun(last_run))
        if dry == "held":
            # A person's dry: no mode, setpoint, fan or vane write.
            return HeadApply(
                cid, skip="dry", steps=(*steps, Arm("dry", cid, None), FanRead(MODE_DRY)),
            )
        # Another room won heat: park it at its idle action below, once told. Dry is never
        # sent back.
        steps.append(DryNotice())
    park = idle_for(
        cid, shared.idle_action, last_run, head.head, shared.coil_dry_seconds, shared.clock,
        dry_waiting=memory.waiting,
    )
    floor = room.floor_want
    floor_run = None
    if floor:
        if head.floor_clear is None:
            raise ValueError(f"{cid}: a floor want needs its clear line")
        floor_run = (floor, head.floor_clear)
    taken = head.taken_over if floor == state else None
    command = head_command(
        engage=room.engage,
        floor_run=floor_run,
        temp=room.temp,
        enabled=head.enabled,
        mode=state,
        idle=park.idle,
        eco=shared.eco,
        target=head.target,
        heat_target=head.heat_target,
        cool_target=head.cool_target,
        taken=None if taken is None else (taken[1], taken[2]),
        settings=shared.settings,
    )
    return HeadApply(cid, steps=(*steps, *park.steps, Queue(command, taken=taken is not None)))


def planned_act(
    *,
    active: str | None,
    paused: bool,
    state: str | None,
    engage: str | None,
    enabled: bool,
    eco: bool,
    idle: Callable[[], str],
) -> str | None:
    """The mode the plan wants a head in now; None when no plan says.

    ``active`` is the act of a delivery running on the head: a newer decision may already be
    published while the head still reports an older operation, and the delivery owns its
    writes. A paused room's head is off by plan, never drift. Otherwise the plan's (``state``,
    the room's ``engage``; None for a head no room of the plan runs) as the apply would send it,
    a disabled room parking off. ``idle`` is the head's parking mode (:func:`idle_for`), read
    for any room the plan runs, as the apply reads it.
    """
    if active is not None:
        return active
    if paused:
        return MODE_OFF
    if state not in (MODE_COOL, MODE_HEAT) or engage is None:
        return None
    if engage not in _VALID_ENGAGES:
        return None
    parked = idle()
    return head_action(engage=engage, mode=state, eco=eco, idle=parked if enabled else MODE_OFF)


def apply_gate(plan: Plan, shared: ApplyShared) -> str | None:
    """Why an apply leaves the plan unapplied, in the order the gates run: this coordinator
    ``retired``, the Coordinator switch off (``coordinator_off``: the heads are left
    untouched), a fixed ``standby`` park (every head is sent its park mode instead), a plan
    ``not_ready`` and an engage this does not know (``unknown_engage``). None: the plan is
    applied."""
    if shared.retired:
        return "retired"
    if not shared.coordinator:
        return "coordinator_off"
    if shared.standby is not None:
        return "standby"
    if plan.state not in (MODE_COOL, MODE_HEAT):
        return "not_ready"
    if any(room.engage not in _VALID_ENGAGES for room in plan.rooms):
        return "unknown_engage"
    return None


def apply_plan(
    plan: Plan, heads: Iterable[ApplyHead], shared: ApplyShared
) -> Iterator[HeadApply | ModeWriteBack]:
    """What one apply does, in the order it is done: each head's part in room order, then the
    plan's mode write-back.

    ``heads`` is read one head at a time, as the apply reaches it, and each part is decided only
    once the part before it has been taken: the caller performs a part before asking for the
    next, so a head's gates read what the heads before it left (reading a head's ``settling``
    can arm its coil-dry wakeup). For a fixed standby park (:func:`apply_gate`) each head is
    sent the park mode, mode only (never the fan machinery, so a manual fan hold survives the
    hold), except one mid vane-kick, and nothing is written back. For any other gate nothing is
    done. The write-back comes after every head's part and before any head is sent anything:
    the heads' commands are only queued until then.
    """
    gate = apply_gate(plan, shared)
    if gate == "standby" and shared.standby is not None:
        park = HeadCommand(mode=shared.standby, low=0.0, high=0.0, delta=0.0, fan=None)
        for head in heads:
            if head.vane_kick:
                yield HeadApply(head.climate_id, skip="vane_kick")
            else:
                yield HeadApply(head.climate_id, steps=(Queue(park),))
        return
    if gate is not None:
        return
    state = plan.state
    for room, head in zip(plan.rooms, heads):
        yield _head_apply(room, head, state, shared)
    yield mode_write_back(
        shared.mode,
        shared.by_votes,
        state=state,
        by_hands_off=plan.mode_by_hands_off,
        calls=(*(room.engage for room in plan.rooms), *(room.demand for room in plan.rooms)),
        resting=shared.resting,
    )


@dataclass(frozen=True, slots=True)
class DeliverySteps:
    """What one head's delivery does besides its mode and setpoints.

    ``handback``: the fan goes back to auto before the mode, on the way to off.
    ``last_run``: the act is the head's last run, which the coil-dry run counts from.
    ``ends_coil_dry``: the act ends a coil-dry run's wakeup.
    ``fan_machinery``: the delivery has a fan step at all (not a fixed standby
    park or a floor); :meth:`fan` says which.
    """

    handback: bool
    last_run: bool
    ends_coil_dry: bool
    fan_machinery: bool

    @property
    def order(self) -> tuple[str, ...]:
        """The delivery's steps at a local head, in order.

        ``"note"``: the coil-dry note (:meth:`note`). ``"handback"``, only
        with a hand-back: the fan back to auto (the fan boost step for fan_only). ``"setpoints"``:
        the mode and setpoints. ``"fan"``, only with fan machinery: :meth:`fan`, which reads
        the standby hold again when it runs. After each step that writes to the head, the
        delivery stops if it is no longer owned.
        """
        return (
            "note",
            *(("handback",) if self.handback else ()),
            "setpoints",
            *(("fan",) if self.fan_machinery else ()),
        )

    def note(self, head: Hashable, act: str, at: float) -> tuple[MemoryStep, ...]:
        """The coil-dry note: what sending ``head`` ``act`` at ``at`` does to its coil-dry
        memory, in order. ``act`` becomes its last run, then its coil-dry wakeup is cancelled."""
        return (
            *((LastRun((act, at)),) if self.last_run else ()),
            *((Arm("dry", head, None),) if self.ends_coil_dry else ()),
        )

    def fan(self, *, inhibited: bool) -> str | None:
        """The step after the mode and setpoints.

        ``"drop_rung"`` after a hand-back (the ladder starts from the bottom
        next run), ``"boost"`` (the fan boost step for the act) outside a
        standby hold, or None (no fan step). ``inhibited`` is the standby hold
        as read when this step runs. A local head reads it again after its mode
        write returns, so a hold that starts or ends during the write decides
        this step, while the hand-back, decided before the write, stands.
        """
        if not self.fan_machinery:
            return None
        if self.handback:
            return "drop_rung"
        return None if inhibited else "boost"


def delivery_steps(
    act: str,
    engage: str | None,
    *,
    idle_action: str,
    eco: bool,
    inhibited: bool,
    head_state: str | None,
) -> DeliverySteps:
    """What a delivery of ``act`` does at its head besides the mode and setpoints.

    ``engage`` None is a fixed standby park or a floor: no coil-dry note and no
    fan machinery. ``head_state`` is the state the head shows, None for none.

    Heat or cool is the head's last run, and anything but fan_only ends a
    coil-dry run's wakeup. The fan goes back to auto before an off only on a
    head that is on now, parked off by the plan (an idle action other than
    fan_only, a room that is not off), outside eco and a standby hold
    (``inhibited``, as read before the delivery's first write). A local head is
    sent the hand-back, then the mode and setpoints, then the fan step
    (:meth:`DeliverySteps.fan`); a cloud head's batch carries the same decisions.
    """
    if engage is None:
        return DeliverySteps(
            handback=False, last_run=False, ends_coil_dry=False, fan_machinery=False,
        )
    handback = (
        act == MODE_OFF and idle_action != IDLE_ACTION_FAN_ONLY
        and engage != MODE_OFF and not eco and not inhibited
        and head_state is not None and head_state not in (MODE_OFF, *UNAVAILABLE_STATES)
    )
    return DeliverySteps(
        handback=handback,
        last_run=act in (MODE_COOL, MODE_HEAT),
        ends_coil_dry=act != MODE_FAN_ONLY,
        fan_machinery=True,
    )


def setpoint_field(mode: str, *, features: int | None, cloud: bool) -> str | None:
    """The head setpoint that carries the room's target in the mode the head shows.

    The one MXZ writes: the high edge in cool, the low edge in heat, or the one
    setpoint of a head without a range (``features`` lacks
    ``TARGET_TEMPERATURE_RANGE``, 2) or of a cloud head. None off heat and cool.
    """
    if mode not in (MODE_COOL, MODE_HEAT):
        return None
    if cloud or not int(features or 0) & 2:
        return "temperature"
    return "target_temp_high" if mode == MODE_COOL else "target_temp_low"


def head_moved(
    *,
    accept: bool,
    old_mode: str | None,
    new_mode: str | None,
    field: str | None,
    old_setpoint: Any,
    new_setpoint: Any,
) -> set[str]:
    """What a head's report moved that a person may have changed: "mode", "setpoint".

    ``field`` is the setpoint carrying the room's target in the new mode
    (:func:`setpoint_field`), and the setpoints are that field's values as
    reported, compared as they are. Nothing with Accept changes off, and nothing
    from or to no state or unavailable: that is the head (re)loading. A fan or
    vane report moves nothing.
    """
    if (
        not accept or old_mode is None or new_mode is None
        or old_mode in UNAVAILABLE_STATES or new_mode in UNAVAILABLE_STATES
    ):
        return set()
    moved = {"mode"} if old_mode != new_mode else set()
    if field is not None and old_setpoint != new_setpoint:
        moved.add("setpoint")
    return moved


@dataclass(frozen=True, slots=True)
class HeadChange:
    """A change a head shows, and its room, as :func:`adopt_head_change` reads them.

    ``moved`` is what moved since the hold began (:func:`head_moved`). The gates:
    Accept changes made at the head (``accept``), this coordinator retired, the
    Coordinator switch on (``coordinator``), a standby hold (``inhibited``),
    eco, a start still quiet (``starting``), a disabled hands-off room's head
    (``hands_off``), a floor run (``floor``), a door/window pause (``paused``),
    and a cloud head's report of an older mode (``stale``).

    ``mode`` is the mode the head shows, None for no state or unavailable, and
    ``mode_own`` whether MXZ's own writes explain it. ``enabled`` is the room's
    enable, and ``planned`` the mode the plan wants for the head now, read only
    for an off pressed at an enabled room's head (reading it can arm the
    coil-dry wakeup). ``field`` is the setpoint carrying the room's target
    (:func:`setpoint_field`), ``setpoint`` its value, None if unreadable,
    ``setpoint_own`` whether MXZ's own writes explain it and ``setpoint_stale``
    whether it is a cloud head's poll from before MXZ's last batch. ``driven``
    is a cloud head run past the target by its drive offset. ``target`` is the
    room target on the room's step that leaves the head at ``setpoint``, held
    to the room's bounds, ``current`` the room's target on that side now, and
    ``step`` the target step.
    """

    moved: Collection[str]
    accept: bool
    retired: bool
    coordinator: bool
    inhibited: bool
    eco: bool
    starting: bool
    hands_off: bool
    floor: bool
    paused: bool
    stale: bool
    mode: str | None
    mode_own: bool
    enabled: bool
    planned: Callable[[], str | None]
    field: str | None
    setpoint: float | None
    setpoint_own: bool
    setpoint_stale: bool
    driven: bool
    target: float | None
    current: float | None
    step: float


@dataclass(frozen=True, slots=True)
class Adoption:
    """What a person's change at a head asks of its room: its new enable and target.

    Neither is a person's change with nothing to adopt, like a heat/cool choice:
    the head still goes back on the plan.
    """

    enable: bool | None = None
    target: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """The change as the coordinator keeps and logs it: ``enable`` first, then ``target``."""
        change: dict[str, Any] = {} if self.enable is None else {"enable": self.enable}
        if self.target is not None:
            change["target"] = self.target
        return change


def adopt_head_change(change: HeadChange) -> Adoption | None:
    """What a change at a head asks of its room now; None if it isn't a person's.

    Only what moved counts, and only if MXZ's own writes can't explain it, so a
    mode change alone never reads the setpoint the head happened to be on as a
    new target. An off pressed at an enabled room's head disables the room,
    unless the plan itself wants the head off. Heat or cool pressed at a
    disabled room's head enables it. A setpoint pressed in heat or cool is the
    room's new target when it differs from the room's by half a step or more,
    except on a cloud head driven past the target. fan_only is the park, and
    heat_cool, auto and dry are healed: none is adopted.
    """
    if (
        not change.accept or change.retired or not change.coordinator or change.inhibited
        or change.eco or change.starting or change.mode is None
        # A disabled hands-off room's head is the person's, on purpose; a floor
        # run's is MXZ's until it ends; a paused room's has its own rule.
        or change.hands_off or change.floor or change.paused or change.stale
    ):
        return None
    mode_press = "mode" in change.moved and not change.mode_own
    if change.mode == MODE_OFF:
        if mode_press and change.enabled and change.planned() != MODE_OFF:
            return Adoption(enable=False)
        return None
    if change.field is None:
        return None
    if not change.enabled and not mode_press:
        return None
    setpoint_press = (
        "setpoint" in change.moved and change.setpoint is not None
        and not change.setpoint_own and not change.setpoint_stale
    )
    if not mode_press and not setpoint_press:
        return None
    target = None
    if (
        setpoint_press and not change.driven
        and change.target is not None and change.current is not None
        and abs(change.target - change.current) >= change.step / 2
    ):
        target = change.target
    return Adoption(enable=None if change.enabled else True, target=target)


def adopted(adoption: Adoption | None) -> bool:
    """Whether a change at a head asks anything of its room: a person's change with a target
    or an enable in it. One with nothing to adopt, like a heat/cool choice, protects nothing."""
    return adoption is not None and bool(adoption.as_dict())


def holds_change(holding: bool, adoption: Callable[[], Adoption | None]) -> bool:
    """Whether a person's change at a head is still holding (``holding``: its adopt hold is
    armed) and would still count: the apply leaves that head alone.

    ``adoption`` is :func:`adopt_head_change` for what moved since the hold began, read only
    while the hold is armed.
    """
    return holding and adopted(adoption())


@dataclass(frozen=True, slots=True)
class HeadReport:
    """What a head's report does to its adopt hold.

    ``pending``: what moved since the hold began, to keep for its end; None drops it.
    ``hold``: the hold armed for ``ADOPT_DELAY``, or cancelled. ``disown``: a command of MXZ's
    still waiting for the head is no longer owed (a person's change is newer). ``replan``: the
    report ended a hold with nothing adopted (a press taken back, say): the plan skipped the
    head while it held, and arming it disowned any command still waiting, so re-plan.
    """

    pending: set[str] | None
    hold: Arm
    disown: bool
    replan: bool


def head_report(
    moved: set[str],
    pending: Collection[str],
    *,
    key: Hashable,
    holding: bool,
    adoption: Callable[[set[str]], Adoption | None],
) -> HeadReport | None:
    """A head's report against its adopt hold; None when it moved nothing (:func:`head_moved`).

    ``pending`` is what moved since the hold began, ``key`` the hold's key and ``holding``
    whether it is armed. Each report that moves the mode or the setpoint starts the hold over,
    with everything moved since it began; ``adoption`` judges that (:func:`adopt_head_change`).
    A change with nothing to adopt (a heat/cool flip alone) holds nothing: the head goes back
    on the plan at the next compute, as with the option off.
    """
    if not moved:
        return None
    moved = moved | set(pending)
    if not adopted(adoption(moved)):
        return HeadReport(pending=None, hold=Arm("heal", key, None), disown=False, replan=holding)
    return HeadReport(pending=moved, hold=Arm("heal", key, ADOPT_DELAY), disown=True, replan=False)


@dataclass(frozen=True, slots=True)
class AdoptEnd:
    """What the end of an adopt hold writes.

    ``seen``: the head-write fields whose memory takes what the head shows now
    (``head_writes.writes_pressed``): ``HEAD_WRITE_MODE`` when the mode moved, the setpoint field when
    the setpoint did. ``number``: the room's number the target goes to (``target``, or a dual
    room's ``target_high`` in cool and ``target_low`` otherwise), None for no target.
    ``target`` and ``enable``: the room's new target and enable (None: unchanged). Then a
    compute is asked for.
    """

    seen: tuple[str, ...]
    number: str | None
    target: float | None
    enable: bool | None

    @property
    def steps(self) -> tuple[AdoptStep, ...]:
        """What the end writes, in order: the memory takes what the head shows, the room's
        target, its enable, then the off heal is checked again against the room as it is now."""
        return (
            Pressed(self.seen),
            *((SetTarget(self.number, self.target),)
              if self.number is not None and self.target is not None else ()),
            *((SetEnable(self.enable),) if self.enable is not None else ()),
            OffHealRecheck(),
        )


@dataclass(frozen=True, slots=True)
class Pressed:
    """MXZ's write memory takes what the head shows for ``fields`` (``writes_pressed``): a
    person's change is what the head shows now, whatever MXZ wrote before it."""

    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SetTarget:
    """The room's ``number`` (``target``, ``target_high`` or ``target_low``) is set to ``value``,
    through the room's own number."""

    number: str
    value: float


@dataclass(frozen=True, slots=True)
class SetEnable:
    """The room's enable is set to ``on``, through the room's own switch."""

    on: bool


@dataclass(frozen=True, slots=True)
class OffHealRecheck:
    """The head's off heal, if armed, is checked again against the room as it is now
    (:func:`off_heal_recheck`): an off adopted as "disable the room" is the room's, not drift."""


type AdoptStep = Pressed | SetTarget | SetEnable | OffHealRecheck


def adopt_end(
    moved: Collection[str], adoption: Adoption, *, mode: str, field: str | None, dual: bool
) -> AdoptEnd:
    """The end of an adopt hold whose change still counts (``adoption``, judged again now).

    ``moved`` is what moved since the hold began, ``mode`` the mode the head shows and
    ``field`` the setpoint carrying the room's target in it (:func:`setpoint_field`).
    """
    seen = (*((HEAD_WRITE_MODE,) if "mode" in moved else ()),
            *((field,) if "setpoint" in moved and field else ()))
    number = None
    if adoption.target is not None:
        number = "target"
        if dual:
            number += "_high" if mode == MODE_COOL else "_low"
    return AdoptEnd(seen=seen, number=number, target=adoption.target, enable=adoption.enable)


@dataclass(frozen=True, slots=True)
class StartupReport:
    """What a head's report does to its room's startup seed: ``cancel`` it (the room resumes
    no run), and ``replan`` now."""

    cancel: bool
    replan: bool


def startup_report(
    mode: str | None,
    old_mode: str | None,
    *,
    planned: Callable[[], str | None],
    stale: Callable[[], bool],
) -> StartupReport:
    """A report from the head of a room whose startup seed is still pending.

    A reported off beyond MXZ's own sensor-failure park (``planned``, the mode the plan wants
    for the head) cancels the remembered startup run, just like an off present at restore,
    unless it is a cloud head's report of an older state (``stale``). Each is read only when
    it decides. A real mode, new since the last report, re-plans.
    """
    return StartupReport(
        cancel=mode == MODE_OFF and planned() != MODE_OFF and not stale(),
        replan=mode not in (None, *UNAVAILABLE_STATES) and mode != old_mode,
    )


@dataclass(frozen=True, slots=True)
class HoldEnd:
    """What the end of an adopt hold does: ``steps`` in order, then a compute asked for
    (``refresh``), also after a write that is refused."""

    steps: tuple[AdoptStep, ...]
    refresh: Refresh = Refresh.REQUEST


def hold_end(
    moved: Collection[str],
    adoption: Adoption | None,
    *,
    mode: str | None,
    field: str | None,
    dual: bool,
) -> HoldEnd:
    """An adopt hold ends (its ``heal`` wakeup for ``(head, "adopt")`` ran).

    ``moved`` is the pending set, taken: the hold's end consumes it. ``adoption`` is the change
    judged again now (:func:`adopt_head_change` of :func:`head_change`); None adopts nothing and
    only asks for a compute. ``mode`` and ``field`` are what the head shows now and the setpoint
    carrying the room's target in it.
    """
    if adoption is None or mode is None:
        return HoldEnd(steps=())
    return HoldEnd(steps=adopt_end(moved, adoption, mode=mode, field=field, dual=dual).steps)


def head_dropped(key: Hashable) -> HeadReport:
    """A change to the room in Home Assistant (its target, range or enable) during an adopt
    hold ends the hold with nothing adopted: the later change wins. ``key`` is the hold's."""
    return HeadReport(pending=None, hold=Arm("heal", key, None), disown=False, replan=False)


# -- a person's write to a room's target, range or enable ------------------------------------
@dataclass(frozen=True, slots=True)
class Latch:
    """The room's engage-latch entry becomes ``entry``: ``""`` decides afresh, None forgets it
    (an enabled, live room's next compute seeds it again from its head's mode, :func:`decide`)."""

    entry: str | None


@dataclass(frozen=True, slots=True)
class SeedDropped:
    """The room's startup seed, if one waits, is dropped: the room resumes no run."""


@dataclass(frozen=True, slots=True)
class ReleaseEnded:
    """A hands-off room is no longer released: the plan drives its head again."""


type RoomStep = HeadReport | ReleaseEnded | SeedDropped | Latch


def range_target(low: float, high: float) -> float:
    """A dual-setpoint room's target: the middle of its range."""
    return (low + high) / 2


def target_written(
    head: Hashable, *, startup: bool, coordinator: bool, enabled: bool
) -> tuple[RoomStep, ...]:
    """A person wrote a room's target, or its range (whose middle is the target,
    :func:`range_target`): a fresh decision. What it does, in order.

    ``head`` is the room's head. The adopt hold ends with nothing adopted
    (:func:`head_dropped`). Then a room whose startup seed still waits (``startup``), under the
    Coordinator switch off (``coordinator`` False), or disabled (``enabled`` False) drops the
    seed and decides afresh: new intent must not revive an old pending run. Any other room
    forgets its latch, which its next compute seeds again from its head's mode.
    """
    if startup or not coordinator or not enabled:
        latch: tuple[RoomStep, ...] = (SeedDropped(), Latch(""))
    else:
        latch = (Latch(None),)
    return (head_dropped((head, "adopt")), *latch)


def enable_written(head: Hashable, *, was_on: bool, on: bool) -> tuple[RoomStep, ...]:
    """A person turned a room on or off (``on``; it was ``was_on``). What it does, in order,
    once the room's enable is ``on``.

    Turned on, a hands-off room's release ends: the plan drives its head again, and the next
    disablement parks afresh. Turned on from off, or off, is a fresh decision: its startup seed
    is dropped and its latch decides afresh. Last, the adopt hold ends with nothing adopted
    (:func:`head_dropped`); ``head`` is the room's head.
    """
    return (
        *((ReleaseEnded(),) if on else ()),
        *((SeedDropped(), Latch("")) if not was_on or not on else ()),
        head_dropped((head, "adopt")),
    )


def band_drift(mode: str | None, *, hands_off_head: bool, dry_allowed: bool) -> bool:
    """A head in a mode MXZ never runs (``BANNED_MODES``), unless it is a released hands-off
    room's (``hands_off_head``) or in a Dry its room lets stay (``dry_allowed``)."""
    return mode in BANNED_MODES and not hands_off_head and not dry_allowed


def off_while_enabled(
    mode: str | None, *, enabled: bool, eco_idle: bool, planned: Callable[[], str | None]
) -> bool:
    """Off while its room is enabled and the plan wants it on: the off heal's drift. ``planned``
    (:func:`planned_act`) is read last, only for an off at an enabled room's head outside eco."""
    return mode == MODE_OFF and enabled and not eco_idle and planned() != MODE_OFF


def off_heal_recheck(
    head: Hashable,
    waiting: bool,
    mode: str | None,
    *,
    enabled: bool,
    eco_idle: bool,
    planned: Callable[[], str | None],
) -> Arm | None:
    """:class:`OffHealRecheck` at ``head``: an armed off heal (``waiting``) whose head no longer
    drifts (:func:`off_while_enabled`, ``mode`` as it shows now) is cancelled; None leaves it as
    it is. A later re-enable is read from the head's next report, not from this wait."""
    if waiting and not off_while_enabled(mode, enabled=enabled, eco_idle=eco_idle, planned=planned):
        return Arm("heal", (head, "off"), None)
    return None


def heal_due(
    kind: str,
    mode: str | None,
    *,
    hands_off_head: Callable[[], bool],
    dry_allowed: Callable[[], bool],
    enabled: bool,
    eco_idle: bool,
    planned: Callable[[], str | None],
    stale: Callable[[], bool],
    coordinator: bool,
    inhibited: bool,
) -> Refresh | None:
    """A ``band`` or ``off`` heal wait ran: if its drift still holds, the plan is applied to the
    head again, so it returns the compute to ask for (and someone is told); None does nothing.
    ``mode`` is what the head shows now; ``stale``, a cloud head's report of an older state, is
    not drift. Each read is made only when it decides."""
    if kind == "band":
        still = band_drift(mode, hands_off_head=hands_off_head(), dry_allowed=dry_allowed())
    else:
        still = off_while_enabled(mode, enabled=enabled, eco_idle=eco_idle, planned=planned)
    return Refresh.REQUEST if still and not stale() and coordinator and not inhibited else None


@dataclass(frozen=True, slots=True)
class AdoptGates:
    """What turns head-change adoption off for a head (:class:`HeadChange`'s gates): Accept
    changes made at the head (``accept``), this coordinator retired, the Coordinator switch on
    (``coordinator``), a standby hold (``inhibited``), eco, a start still quiet (``starting``),
    a disabled hands-off room (``hands_off``), a floor run (``floor``) and a door/window pause
    (``paused``)."""

    accept: bool
    retired: bool
    coordinator: bool
    inhibited: bool
    eco: bool
    starting: bool
    hands_off: bool
    floor: bool
    paused: bool


def head_change(
    moved: Collection[str],
    *,
    state: str | None,
    attributes: Mapping[str, Any],
    cloud: bool,
    gates: AdoptGates,
    enabled: bool,
    heat_target: float,
    cool_target: float,
    driven: bool,
    step: float,
    memories: Mapping[str, HeadWriteMemory],
    grid: Grid,
    mode_stale: Callable[[str], bool],
    setpoint_stale: Callable[[float], bool],
    planned: Callable[[], str | None],
) -> HeadChange:
    """The :class:`HeadChange` a head shows (``state`` and ``attributes``, None and empty for no
    state) for what moved since its adopt hold began.

    The head's mode is what it shows, None for no state or unavailable; the setpoint is the one
    carrying the room's target in it (:func:`setpoint_field`), as a number. Whether MXZ's own
    writes explain each is read off ``memories`` on the head's ``grid``. The candidate target is
    the target on the room's step that leaves the head where a person put it
    (``Grid.target_for_setpoint``), held to the bounds a target may take on it. ``current`` is
    the room's Cool to in cool and its Heat to otherwise. ``driven`` is a cloud head run past
    the target by its drive offset. ``mode_stale`` and ``setpoint_stale`` say whether a cloud
    head's poll is from before MXZ's last batch; ``step`` is the room's target step.
    """
    mode = state if state is not None and state not in UNAVAILABLE_STATES else None
    field: str | None = None
    shown: float | None = None
    if mode is not None:
        field = setpoint_field(mode, features=attributes.get("supported_features"), cloud=cloud)
        shown = None if field is None else as_float(attributes.get(field))
    target = None if shown is None else clamp(grid.target_for_setpoint(shown), grid.target_bounds())
    return HeadChange(
        moved=moved,
        accept=gates.accept,
        retired=gates.retired,
        coordinator=gates.coordinator,
        inhibited=gates.inhibited,
        eco=gates.eco,
        starting=gates.starting,
        hands_off=gates.hands_off,
        floor=gates.floor,
        paused=gates.paused,
        stale=mode is not None and mode_stale(mode),
        mode=mode,
        mode_own=mode is not None and write_is_own(memories, HEAD_WRITE_MODE, mode, grid),
        enabled=enabled,
        planned=planned,
        field=field,
        setpoint=shown,
        setpoint_own=(
            shown is not None and field is not None and write_is_own(memories, field, shown, grid)
        ),
        setpoint_stale=shown is not None and setpoint_stale(shown),
        driven=driven,
        target=target,
        current=cool_target if mode == MODE_COOL else heat_target,
        step=step,
    )


# -- a head's report -------------------------------------------------------------------------
# The attribute a head's run shows (Home Assistant's ATTR_HVAC_ACTION).
_HVAC_ACTION = "hvac_action"


@dataclass(frozen=True, slots=True)
class FanShown:
    """The head shows ``fan``: the fan writes before it are behind it (``FanEcho.caught_up``)."""

    fan: Any


@dataclass(frozen=True, slots=True)
class WritesShown:
    """MXZ's mode and setpoint writes before this report are behind the head for good
    (``head_writes.writes_caught_up``)."""


@dataclass(frozen=True, slots=True)
class SightLost:
    """MXZ loses sight of the head: what it showed just before (``observe_old``: the old state
    was in sight) is its last-known vote, then it is out of sight (``HeadSight``)."""

    observe_old: bool


@dataclass(frozen=True, slots=True)
class SightObserved:
    """The head's run as it shows it now is observed (``HeadSight.observe``): a parking
    hands-off head's run MXZ drove ends here."""


@dataclass(frozen=True, slots=True)
class SeedCancelled:
    """The room's startup seed is cancelled: it resumes no run."""


@dataclass(frozen=True, slots=True)
class FloorYielded:
    """A person's mode change takes a hands-off head back from the floor run (decision 8b)."""


@dataclass(frozen=True, slots=True)
class TakenOver:
    """A person's change at a paused room's head takes the room over until the opening
    closes."""


type ReportStep = (
    FanShown | WritesShown | SightLost | SightObserved | SeedCancelled | Landed | FloorYielded
    | TakenOver | Arm | LastRun | HeadReport | Refresh
)


@dataclass(frozen=True, slots=True)
class ReportedHead:
    """A head's report as MXZ reads it.

    ``key`` is the head. ``mode`` and ``attributes`` are what the new state shows (None and
    empty for none), ``old_mode`` and ``old_attributes`` the state before it. Read before the
    report does anything: ``accept`` (Accept changes made at the head), ``cloud`` (a cloud
    head), ``sight`` (MXZ keeps sight of it), ``released_by_action`` (a released hands-off room
    votes by its run), ``startup`` (its room's startup seed waits), ``owned_off`` (no command of
    MXZ's but an off runs on it), ``parking`` (its hands-off room is parking), ``kicking`` (a
    vane kick runs or just woke it), ``floor_run`` (a floor run has it), ``paused`` (its room
    is paused for an opening), ``stale`` (a cloud head's report of an older state), ``allow_dry``
    (its room offers Dry), ``dry_allowed`` (a Dry its room lets stay), ``enabled``,
    ``eco_idle``, ``inhibited`` (a standby hold), ``unresponsive`` (a cloud head out of control
    reported again), ``pending`` (what moved since its adopt hold began) and ``holding`` (the
    hold is armed).

    Read when the report reaches them, after what it did before: ``hands_off_head`` (its room
    is a released hands-off room: a floor run the report ended released it), ``dry_reported``
    (Dry's reading of the report, which keeps what it reads; true when it ends a room's Dry),
    ``planned`` (:func:`planned_act`; reading it can rebuild the head's coil-dry memory and arm
    its wakeup, ``IdlePark.steps``), ``clock`` and ``adoption`` (:func:`adopt_head_change` of
    :func:`head_change` for what moved).
    """

    key: str
    mode: str | None
    attributes: Mapping[str, Any]
    old_mode: str | None
    old_attributes: Mapping[str, Any]
    accept: bool
    cloud: bool
    sight: bool
    released_by_action: bool
    startup: bool
    owned_off: bool
    parking: bool
    kicking: bool
    floor_run: bool
    paused: bool
    stale: bool
    allow_dry: bool
    dry_allowed: bool
    enabled: bool
    eco_idle: bool
    inhibited: bool
    unresponsive: bool
    pending: Collection[str]
    holding: bool
    hands_off_head: Callable[[], bool]
    dry_reported: Callable[[], bool]
    planned: Callable[[], str | None]
    clock: Callable[[], float]
    adoption: Callable[[set[str]], Adoption | None]


def report_steps(r: ReportedHead) -> Iterator[ReportStep]:
    """What a head's report does, in order; each step is done before the next is decided.

    The fan echo and MXZ's write memory catch up on what it shows. Out of sight: its last-known
    vote is kept. A startup seed it cancels (:func:`startup_report`). A parking hands-off head
    seen off with nothing of MXZ's but an off in flight has landed. A person's real mode change
    (between two available states, not a vane kick, not stale) to anything but what the plan
    wants takes a floor-run head back, and, to anything but off, a paused room over. The band
    heal wait is armed or cancelled (:func:`band_drift`); leaving dry owes the coil-dry run from
    now. The off heal wait (:func:`off_while_enabled`, outside a standby hold). The adopt hold
    (:func:`head_report`). Last, the compute it asks for (``timing.head_refresh``), if any: at
    once for a released room that votes by its run and started or stopped one, else for any
    step that re-plans or anything new the head shows.
    """
    mode, old_mode = r.mode, r.old_mode
    key = r.key
    if mode is not None:
        yield FanShown(r.attributes.get("fan_mode"))
        yield WritesShown()
    replan = False
    lost = mode in (None, STATE_UNAVAILABLE)
    if r.sight and lost:
        yield SightLost(observe_old=old_mode is not None and old_mode != STATE_UNAVAILABLE)
    if r.sight and lost != (old_mode is None or old_mode == STATE_UNAVAILABLE):
        # Out of sight, or back: unknown <-> unavailable recomputes the vote now too.
        replan = True
    run = False
    if mode is not None and old_mode is not None and r.released_by_action:
        run = run_changed(
            old_mode, mode, r.old_attributes.get(_HVAC_ACTION), r.attributes.get(_HVAC_ACTION),
        )
    if r.startup:
        seed = startup_report(mode, old_mode, planned=r.planned, stale=lambda: r.stale)
        if seed.cancel:
            yield SeedCancelled()
        if seed.replan:
            replan = True
    if mode == MODE_OFF and r.owned_off and r.parking:
        yield Landed()
        if r.sight:
            yield SightObserved()
    real = (
        mode != old_mode and mode not in (None, *UNAVAILABLE_STATES)
        and old_mode not in (None, *UNAVAILABLE_STATES) and not r.kicking and not r.stale
    )
    if real and r.floor_run and mode != r.planned():
        yield FloorYielded()
        replan = True
    if real and mode != MODE_OFF and r.paused and mode != r.planned():
        yield TakenOver()
        replan = True
    band = band_drift(mode, hands_off_head=r.hands_off_head(), dry_allowed=r.dry_allowed)
    yield Arm("heal", (key, "band"), BAND_DRIFT_DELAY if band and not r.stale else None)
    if (mode == MODE_DRY) != (old_mode == MODE_DRY) and r.allow_dry:
        replan = True
        if old_mode == MODE_DRY and not r.stale and mode not in (None, *UNAVAILABLE_STATES):
            # Dry wets the coil as cooling does: leaving it owes the coil-dry run from now.
            yield LastRun((MODE_COOL, r.clock()))
    if r.dry_reported():
        replan = True
    off = (
        off_while_enabled(mode, enabled=r.enabled, eco_idle=r.eco_idle, planned=r.planned)
        and not r.inhibited and not r.stale
    )
    yield Arm("heal", (key, "off"), OFF_WHILE_ENABLED_DELAY if off else None)
    field = None if mode is None else setpoint_field(
        mode, features=r.attributes.get("supported_features"), cloud=r.cloud,
    )
    moved = head_moved(
        accept=r.accept, old_mode=old_mode, new_mode=mode, field=field,
        old_setpoint=None if old_mode is None or field is None else r.old_attributes.get(field),
        new_setpoint=None if mode is None or field is None else r.attributes.get(field),
    )
    report = head_report(moved, r.pending, key=(key, "adopt"), holding=r.holding, adoption=r.adoption)
    if report is not None:
        yield report
        replan = replan or report.replan
    if r.unresponsive:
        replan = True
    refresh = head_refresh(
        run_changed=run,
        moved=replan or head_shows_new(old_mode, mode, r.old_attributes, r.attributes),
    )
    if refresh is not None:
        yield refresh
