"""The planner: what one compute decides from what it read. No Home Assistant.

``MXZCoordinator._compute`` reads each room's sensor, head and controllers, hands the
readings to :func:`decide` as one :class:`RoomInput` per room and one :class:`Shared`,
and builds the plan from the :class:`Decision`. Time comes in as numbers: the seconds
since the last mode change and the hold's length. The engage latch and the startup
seeds come in as they are and go out advanced; nothing here keeps state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace

from .const import (
    DEMAND_NEUTRAL,
    ENGAGE_SATISFIED,
    MODE_COOL,
    MODE_HEAT,
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
    room_call,
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
