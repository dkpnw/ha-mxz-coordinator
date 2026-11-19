"""The planner: what one compute decides from what it read. No Home Assistant.

``MXZCoordinator._compute`` reads each room's sensor, head and controllers, hands the
readings to :func:`decide` as one :class:`RoomInput` per room and one :class:`Shared`,
and builds the plan from the :class:`Decision`. ``_apply`` queues each head's
:func:`head_command` and writes the plan's mode back as :func:`mode_write_back` says. Each
head's driver then runs that command's :func:`delivery_steps`.
Time comes in as numbers: the seconds since the last mode change, the hold's length,
the coil-dry clock, and the clocks a start reads (:func:`mode_change_stamp`).
The engage latch and the startup seeds come in as they are and go out advanced; nothing
here keeps state.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from .const import (
    DEMAND_NEUTRAL,
    ENGAGE_SATISFIED,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
    UNAVAILABLE_STATES,
    VOTE_EXPIRED,
)
from .logic import (
    PRIORITY_DEMAND,
    PRIORITY_FLOOR_HEAT,
    PRIORITY_SAFETY,
    PRIORITY_VOTE,
    Demand,
    arbitrate,
    comfort_boost,
    engage_with_latch,
    head_action,
    range_delta,
    room_call,
    setpoints,
)


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
class DeliverySteps:
    """What one head's delivery does besides its mode and setpoints.

    ``handback``: the fan goes back to auto before the mode, on the way to off.
    ``last_run``: the act is the head's last run, which the coil-dry run counts from.
    ``ends_coil_dry``: the act ends a coil-dry run's wakeup.
    ``fan``: the step after the mode and setpoints, ``"boost"`` (the fan boost
    step for the act), ``"drop_rung"`` (the ladder starts from the bottom next
    run, after a hand-back) or None (no fan step).
    """

    handback: bool
    last_run: bool
    ends_coil_dry: bool
    fan: str | None


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
    fan_only, a room that is not off), outside eco and a standby hold. A local
    head is sent the hand-back, then the mode and setpoints, then the fan step;
    a cloud head's batch carries the same decisions.
    """
    if engage is None:
        return DeliverySteps(handback=False, last_run=False, ends_coil_dry=False, fan=None)
    handback = (
        act == MODE_OFF and idle_action != IDLE_ACTION_FAN_ONLY
        and engage != MODE_OFF and not eco and not inhibited
        and head_state is not None and head_state not in (MODE_OFF, *UNAVAILABLE_STATES)
    )
    return DeliverySteps(
        handback=handback,
        last_run=act in (MODE_COOL, MODE_HEAT),
        ends_coil_dry=act != MODE_FAN_ONLY,
        fan="drop_rung" if handback else None if inhibited else "boost",
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
