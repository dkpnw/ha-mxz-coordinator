"""Pure decision math for MXZ Coordinator — no Home Assistant dependency.

Mirrors the Jinja in the YAML package's ``sensor.mxz_plan`` and the setpoint/action
math in ``script.mxz_coordinate``. Kept HA-free so the validated truth table can be
unit-tested directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from .const import (
    ECO_COOL_HIGH,
    ECO_COOL_LOW,
    ECO_HEAT_HIGH,
    ECO_HEAT_LOW,
    ENGAGE_SATISFIED,
    EVIDENCE_HA_WRITE,
    EVIDENCE_SAMPLE_TIMESTAMP,
    FLOOR_REARM_SECONDS,
    HEALTH_AWAITING,
    HEALTH_CADENCE_UNKNOWN,
    HEALTH_HEALTHY,
    HEALTH_INVALID,
    HEALTH_STALE,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
    STALE_MISSED_REPORTS,
    VOTE_EXPIRED,
    VOTE_LIVE,
    VOTE_STALE,
    VOTE_UNKNOWN,
)


def room_call(
    *,
    temp: float,
    target: float,
    enabled: bool,
    eco: bool,
    sensor_ok: bool,
    band: float | None = None,
    eco_cool_max: float,
    eco_heat_min: float,
    neutral: str,
    heat_lockout: bool = False,
    heat_lockout_floor: float = 0.0,
    cool_lockout: bool = False,
    cool_lockout_ceiling: float = 200.0,
    heat_band: float | None = None,
    cool_band: float | None = None,
    heat_target: float | None = None,
    cool_target: float | None = None,
) -> str:
    """Per-room call for one band (demand uses S/'neutral', engage uses D/'satisfied').

    Mirrors the primary/secondary_demand and primary/secondary_engage attributes.

    ``band`` is how far the room may sit off target either way. ``heat_band``
    (below target) and ``cool_band`` (above target) replace it for one side
    each; a side left None uses ``band``. Every side needs a value.

    ``heat_target`` / ``cool_target`` are a dual-setpoint room's Heat to and
    Cool to: the heat side measures from the first and the cool side from the
    second. Either left None uses ``target``, so a single-target room is the
    case where both equal ``target``.

    ``heat_lockout`` holds off the heat call (the room idles instead of heating — e.g.
    to let passive solar warm it in summer) UNLESS the room has dropped below
    ``heat_lockout_floor``, a safety floor so a genuinely cold room still gets heat.
    ``cool_lockout`` is the mirror for the cold season: it holds off the cool call
    (let the room drift down on its own) UNLESS it rises above ``cool_lockout_ceiling``.
    The eco extremes are unaffected; the two lockouts are independent.
    """
    heat_band = band if heat_band is None else heat_band
    cool_band = band if cool_band is None else cool_band
    if heat_band is None or cool_band is None:
        raise TypeError("room_call needs band, or both heat_band and cool_band")
    if not enabled:
        return MODE_OFF
    if not sensor_ok:
        return neutral
    # The comparisons read the same edges the plan shows (call_edges), so the
    # displayed start points and the decision cannot disagree.
    heat_below, cool_above = call_edges(
        target=target, heat_band=heat_band, cool_band=cool_band, eco=eco,
        heat_target=heat_target, cool_target=cool_target,
        eco_cool_max=eco_cool_max, eco_heat_min=eco_heat_min,
        heat_lockout=heat_lockout, heat_lockout_floor=heat_lockout_floor,
        cool_lockout=cool_lockout, cool_lockout_ceiling=cool_lockout_ceiling,
    )
    if temp > cool_above:
        return MODE_COOL
    if temp < heat_below:
        return MODE_HEAT
    return neutral


def call_edges(
    *,
    target: float,
    heat_band: float,
    cool_band: float,
    eco: bool,
    eco_cool_max: float,
    eco_heat_min: float,
    heat_lockout: bool,
    heat_lockout_floor: float,
    cool_lockout: bool,
    cool_lockout_ceiling: float,
    heat_target: float | None = None,
    cool_target: float | None = None,
) -> tuple[float, float]:
    """The two edges :func:`room_call` compares a reading to: (heat_below, cool_above).

    An enabled room with a usable reading calls cool iff ``temp > cool_above``
    (checked first), else heat iff ``temp < heat_below``. One function decides
    both the call and the start points the plan shows, so they can't drift.

    * Eco: the eco extremes, whatever the target, bands, or lockouts.
    * Otherwise ``heat_target - heat_band`` / ``cool_target + cool_band``,
      where each per-side target left None is ``target`` (a single-target
      room; a dual-setpoint room passes its Heat to and Cool to).
    * A lockout moves its side's edge out to the safety line: cool is held off
      while ``temp <= cool_lockout_ceiling`` and heat while
      ``temp >= heat_lockout_floor``, and with strict comparisons "past the
      comfort edge AND past the line" is exactly "past the farther of the two".
      The comfort edge is max()'s/min()'s FIRST argument, so a NaN on either
      side resolves as the original two comparisons did.

    Holding cool off can only leave a reading in the heat region if the two
    regions overlap, i.e. ``heat_band + cool_band`` below
    ``heat_target - cool_target`` (below 0 for a single target). The old
    cool-first order answered "hold" there, which no pair of strict edges can
    express, so that one combination is refused rather than answered
    differently. No caller passes it: drift is clamped positive, the demand
    band is at least the drift, the latch passes 0, and a dual-setpoint
    room's Heat to is always below its Cool to.
    """
    if eco:
        return eco_heat_min, eco_cool_max
    heat_from = target if heat_target is None else heat_target
    cool_from = target if cool_target is None else cool_target
    heat_below = heat_from - heat_band
    cool_above = cool_from + cool_band
    if heat_lockout:
        heat_below = min(heat_below, heat_lockout_floor)
    if cool_lockout:
        if heat_band + cool_band < heat_from - cool_from:
            raise ValueError(
                "a cool lockout needs heat_band + cool_band >= heat_target - cool_target"
            )
        cool_above = max(cool_above, cool_lockout_ceiling)
    return heat_below, cool_above


def engage_with_latch(
    *,
    prior: str | None,
    band: float | None = None,
    neutral: str,
    heat_band: float | None = None,
    cool_band: float | None = None,
    **call_kwargs,
) -> str:
    """Per-room engage with run-to-target hysteresis.

    The engage deadband exists so a satisfied room COASTS instead of flip-
    flopping — it was never meant to truncate the approach. Without a latch,
    a room cooling toward 63 would stop at 64 (target + band) and never reach
    the number the user actually set.

    * Not engaged (``prior`` is None): a room must drift PAST ``band`` to
      engage — unchanged behavior.
    * Engaged (``prior`` is cool|heat): the room runs all the way TO its
      target — satisfied means exactly the number the user set. Crossing the target — or a lockout / eco / disable /
      sensor dropout, all still evaluated via :func:`room_call` — disengages
      it back to ``neutral`` to coast until it drifts past ``band`` again.
    * A direction flip while engaged (overshoot, opened window) never
      whiplashes: anything other than continuing in ``prior``'s direction
      disengages to ``neutral`` first.

    Eco mode bypasses the latch entirely (protection extremes are already a
    wide band; run-to-extreme would defeat their purpose).

    ``heat_band`` / ``cool_band`` split ``band`` by side exactly as in
    :func:`room_call`. They only move where a run STARTS: every run still ends
    at the target, whichever side's drift started it.

    A dual-setpoint room passes ``heat_target`` / ``cool_target`` through
    ``call_kwargs``: a heat run then ends at Heat to and a cool run at Cool
    to (band 0 measured from each side's own target), then the room coasts.
    """
    if prior in (MODE_COOL, MODE_HEAT) and not call_kwargs.get("eco"):
        # Engaged phase: band 0 on both sides — the room runs exactly TO its
        # target, then coasts. The re-engage drift (``band``, or its per-side
        # split) is the only tunable here.
        raw = room_call(band=0.0, neutral=neutral, **call_kwargs)
        if raw == prior or raw == MODE_OFF:
            return raw
        return neutral  # target reached / lockout / direction flip -> coast
    return room_call(
        band=band, heat_band=heat_band, cool_band=cool_band, neutral=neutral,
        **call_kwargs,
    )


def freshness_window(
    *,
    report_interval: Any,
    max_age: Any,
    startup_grace: Any,
) -> tuple[float, float] | None:
    """Resolve one sensor's ``(maximum_age, startup_grace)``, or None.

    Same duration unit in and out (the coordinator passes minutes). ``None``
    means the sensor's cadence is UNKNOWN — no age cutoff applies to it, which
    is the honest default for a change-only or uncontracted source. There is no
    universal fallback interval to invent one from.

    * ``report_interval`` (P) alone -> ``3P``: stale at the third due instant.
    * an explicit ``max_age`` wins over that derivation, and must be at least
      P: a window narrower than one report would park a healthy room on its
      first quiet interval.
    * ``max_age`` alone is enough: a documented maximum age needs no cadence.
    * ``startup_grace`` defaults to the resolved maximum age.

    An input that is set must be a real, finite, positive number. A profile
    with one that is not — or with a maximum age shorter than its interval —
    is INVALID as a whole, and an invalid profile is no profile: ``None``,
    exactly as if nothing had been configured. Nothing in it is read as
    unset, raised, rounded or otherwise coerced into a cutoff the person did
    not write (the caller reports the rejection once).
    """
    durations: list[float | None] = []
    for value in (report_interval, max_age, startup_grace):
        if value is not None and (value := _positive(value)) is None:
            return None
        durations.append(value)
    interval, window, grace = durations
    if window is None:
        window = None if interval is None else interval * STALE_MISSED_REPORTS
    elif interval is not None and window < interval:
        return None
    if window is None:
        return None
    return (window, window if grace is None else grace)


def evidence_contract(
    *,
    basis: Any,
    sample_timestamp_attribute: Any,
    sample_sequence_attribute: Any,
) -> tuple[str, str | None, bool] | None:
    """Resolve one sensor's evidence basis, or None when nothing is trusted.

    Returns ``(basis, marker_attribute, marker_is_a_time)``. ``None`` is both
    the default and the answer to every unrecognised or unusable declaration:
    an untrusted source shows its age and is never enforced, however precise
    its configured cadence is.

    ``ha_state_write`` is a claim about the SOURCE, not about Home Assistant:
    it may be declared only where every accepted write — including the first
    one after setup or reload — is a current acquisition. ``sample_timestamp``
    needs exactly one marker attribute named; naming both is ambiguous, and an
    ambiguous marker is not trustworthy evidence.
    """
    if basis == EVIDENCE_HA_WRITE:
        return (EVIDENCE_HA_WRITE, None, False)
    if basis == EVIDENCE_SAMPLE_TIMESTAMP:
        stamp = _attribute(sample_timestamp_attribute)
        sequence = _attribute(sample_sequence_attribute)
        if stamp is not None and sequence is None:
            return (EVIDENCE_SAMPLE_TIMESTAMP, stamp, True)
        if sequence is not None and stamp is None:
            return (EVIDENCE_SAMPLE_TIMESTAMP, sequence, False)
    return None


def _attribute(name: Any) -> str | None:
    """``name`` as a usable attribute name, else None."""
    return name if isinstance(name, str) and name else None


def sample_evidence(
    *,
    marker: float | None,
    last: float | None,
    now: float,
    receipt: float | None,
    marker_is_a_time: bool,
) -> tuple[float | None, float | None]:
    """One marker observation -> ``(marker to remember, evidence time)``.

    Pure, and the whole of the marker contract. ``marker`` is the trusted
    marker carried by this write (a POSIX sample time, or a sample sequence
    number, compared exactly as the number it is — an integer sequence is
    never rounded through a float); ``last`` is the last marker accepted for
    this sensor.

    * a missing, equal or older marker is a cached or replayed write: it is
      diagnostic only, moves no deadline and recovers nothing;
    * a sample TIME later than the evaluation clock is not evidence either —
      it is a clock fault, and accepting it would grant a free window;
    * an accepted sample time IS the evidence time, so a genuinely old sample
      arrives already aged and recovers nothing;
    * a sample SEQUENCE has no wall-clock meaning, so its evidence time is the
      HA receipt time of the write that carried the advance. The first
      sequence seen is a baseline only: nothing has advanced yet.
    """
    if marker is None or (last is not None and marker <= last):
        return (last, None)
    if marker_is_a_time:
        if marker > now:
            return (last, None)
        return (marker, marker)
    if last is None or receipt is None:
        return (marker, None)
    return (marker, min(receipt, now))


def _positive(value: Any) -> float | None:
    """``value`` as a finite positive float, else None (unset or unusable)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def sensor_health(
    *,
    valid: bool,
    max_age: float | None,
    evidence_age: float | None,
    in_grace: bool,
) -> str:
    """Classify one room sensor. Pure; the whole freshness decision lives here.

    Evaluation order is validity, then basis, then age — M13's value handling
    precedes every age question, so freshness can never make a bad value good.

    ``max_age`` is None for every sensor that is not enforcement-capable: no
    trusted evidence basis, or no maximum age. Such a room is
    ``cadence_unknown`` — its age is shown and no cutoff exists for it.

    ``evidence_age`` is how long ago this incarnation witnessed a qualifying
    report, or ``None`` when it has witnessed none. A report qualifies when it
    is a state write of a valid value that this incarnation saw; it is FRESH —
    the word every recovery rule turns on — when its own deadline is still in
    the future, so a backlog flush delivering an already-aged report recovers
    nothing. A negative age is a write stamped in the future: not evidence.

    ``in_grace`` is the bounded startup grace. It makes a configured room
    provisionally eligible while it awaits its first witnessed report; it never
    claims the current value is fresh, and it never excuses an invalid one.
    """
    if not valid:
        return HEALTH_INVALID
    if max_age is None:
        return HEALTH_CADENCE_UNKNOWN
    if evidence_age is not None and 0.0 <= evidence_age < max_age:
        return HEALTH_HEALTHY
    if in_grace:
        return HEALTH_AWAITING
    return HEALTH_STALE


def season_lockouts(
    *, outdoor_high: float | None, heat_above: float, cool_below: float
) -> tuple[bool, bool]:
    """Derive (heat_lockout, cool_lockout) from the local outdoor daily-high temp.

    The decision is driven by *local weather*, not the calendar: pass the forecast
    daily high (from a weather entity) or an outdoor temperature reading.

    * high >= ``heat_above`` -> warm season -> heat_lockout on (don't actively heat).
    * high <= ``cool_below`` -> cold season -> cool_lockout on (don't actively cool).
    * in between (shoulder season) -> neither -> normal heat + cool.

    The gap between the two thresholds is the hysteresis band — the forecast has to
    swing across the whole shoulder zone to flip a lockout, so it won't chatter.
    ``None`` (no signal — weather unavailable) yields no lockout (safe: normal auto).
    """
    if outdoor_high is None:
        return (False, False)
    if outdoor_high >= heat_above:
        return (True, False)
    if outdoor_high <= cool_below:
        return (False, True)
    return (False, False)


def shared_mode(
    *,
    demands: list[str],
    current: str,
    allowed: bool,
    resting: str | None = None,
) -> str:
    """Choose the shared mode (cool|heat). Mirrors the mxz_plan state template.

    ``demands`` is one demand per zone in PRIORITY ORDER (index 0 = highest).
    ``current`` must already be normalized to cool|heat (cold start -> cool).
    ``allowed`` is the hysteresis gate. In a standoff (some zones calling cool
    while others call heat) the HIGHEST-PRIORITY calling zone wins — the N-zone
    generalization of "the primary room wins".

    ``resting`` biases the mode used when NO room is calling. ``None`` (or any
    non cool|heat value) keeps the original behavior — rest at ``current`` (the
    last called mode). Set it to cool|heat to always settle there when idle; a
    genuine opposite demand still flips the mode (the bias only changes the
    neutral fallback, gated by the same hysteresis as any other flip).
    """
    rest = resting if resting in (MODE_COOL, MODE_HEAT) else current
    any_cool = MODE_COOL in demands
    any_heat = MODE_HEAT in demands
    standoff = any_cool and any_heat

    if standoff:
        proposed = next(
            (d for d in demands if d in (MODE_COOL, MODE_HEAT)), current
        )
    elif any_cool:
        proposed = MODE_COOL
    elif any_heat:
        proposed = MODE_HEAT
    else:
        proposed = rest

    still = (current == MODE_COOL and any_cool) or (current == MODE_HEAT and any_heat)
    if proposed != current and allowed and (standoff or not still):
        return proposed
    return current


# Demand.priority tiers: lower sorts first / wins a standoff. These are the
# ONLY thing that encodes "a real demand beats a vote" -- it falls out of
# comparing (PRIORITY_VOTE, i) > (PRIORITY_DEMAND, j) for any i, j, not a
# hardcoded rule, which is what lets PRIORITY_SAFETY override either kind
# without a special case (see arbitrate()'s docstring).
PRIORITY_SAFETY = -1
PRIORITY_DEMAND = 0
PRIORITY_VOTE = 1
# A tripped freeze floor (a disabled room's heat at its floor) outranks every
# other want, a PRIORITY_SAFETY one included: freeze first. A tripped overheat
# ceiling sits at PRIORITY_SAFETY with its room index, so it settles against a
# safety room by room order. Nothing else ever carries this tier.
PRIORITY_FLOOR_HEAT = -2


@dataclass(frozen=True)
class Demand:
    """One zone's current want for the shared compressor's direction this tick.

    ``direction`` is :data:`MODE_COOL`, :data:`MODE_HEAT`, or ``None`` (wants nothing).
    ``priority`` is ``(tier, configured_index)``; lower sorts first.
    ``read_only`` is True for an advisory want (a vote): it is never sent to the
    voting head, though a safety vote that wins can still park another room's
    head (see :class:`Grant`). False for a coordinated room's own demand.
    """

    direction: str | None
    priority: tuple[int, int]
    read_only: bool = False


@dataclass(frozen=True)
class Grant:
    """The arbitrated outcome for one tick.

    ``read_only`` is True only when THIS tick's answer differs from what it
    would be considering commanding Demands alone (an advisory want is the
    reason the answer moved, not merely that one happens to be active) AND
    the ordinary vote gate was open: no commanding Demand had a direction
    and no room was engaged. Such a flip runs no coordinated head into a
    reversal, so it is exempt from the dwell stamp. A safety vote that moves
    the answer while a coordinated room is calling or engaged mid-run parks
    that room's head, so it reads False and is stamped like any other flip.
    Once the answer settles (nothing is actively changing it any more), this
    reads False again, even while a vote is still what the settled answer
    agrees with -- there is nothing left to exempt from the dwell clock once
    nothing is proposing a change.

    ``by_safety`` is True while a PRIORITY_SAFETY (or freeze floor) Demand
    with a direction is active and that direction is the answer. It does not
    claim the safety tier changed the answer: the same answer may follow from
    order or the dwell alone.
    """

    direction: str
    read_only: bool
    by_safety: bool


def arbitrate(
    demands: list[Demand],
    *,
    engaged: bool,
    current: str,
    allowed: bool,
    resting: str | None = None,
) -> Grant:
    """:func:`shared_mode`, generalized over a priority-ordered Demand list.

    Reproduces the original ``shared_mode_with_votes``'s precedence exactly
    as the degenerate case where every Demand sits at PRIORITY_DEMAND or
    PRIORITY_VOTE and none at PRIORITY_SAFETY. What it fixes: today's "a
    real demand always fully preempts every vote, unconditionally" was a
    hardcoded wall, not a priority relationship -- so a hands-off zone's
    safety-tier vote could never preempt an enabled zone's merely-comfort
    demand, no matter how urgently it needed to. Here a PRIORITY_SAFETY
    Demand -- from EITHER a coordinated room or a hands-off vote -- outranks
    every ordinary Demand structurally, because ``(-1, i) < (0, j)`` always,
    not via a special case, and participates regardless of ``engaged`` or
    whether anything else is commanding.

    ``engaged`` mirrors the original gate's other half: a room mid-run
    toward its target (its own demand back inside the band, but still
    engaged) must not have the mode taken from it by a mere vote, even
    though its demand itself has gone quiet. It only ever suppresses an
    ORDINARY vote (PRIORITY_VOTE, read_only) -- never a safety one.

    A tripped floor is one more commanding Demand: a freeze floor at
    PRIORITY_FLOOR_HEAT (ahead of every safety want), an overheat ceiling at
    PRIORITY_SAFETY. A standoff goes to the first ranked direction, so heat
    from a floor beats cool from a ceiling whatever their rooms' order, and
    two trips of one kind settle by room order.
    """
    ranked = sorted(demands, key=lambda d: d.priority)
    commanding = [d for d in ranked if not d.read_only]
    safety_votes = [
        d for d in ranked if d.read_only and d.priority[0] <= PRIORITY_SAFETY
    ]
    ordinary_votes = [
        d for d in ranked if d.read_only and d.priority[0] > PRIORITY_SAFETY
    ]
    commanding_silent = not any(d.direction for d in commanding)
    gate_open = commanding_silent and not engaged
    active = sorted(
        commanding + safety_votes + (ordinary_votes if gate_open else []),
        key=lambda d: d.priority,
    )
    full = shared_mode(
        demands=[d.direction for d in active if d.direction],
        current=current, allowed=allowed, resting=resting,
    )
    by_safety = any(
        d.direction == full and d.priority[0] <= PRIORITY_SAFETY for d in active
    )
    if not any(d.read_only and d.direction for d in active):
        return Grant(direction=full, read_only=False, by_safety=by_safety)
    plain = shared_mode(
        demands=[d.direction for d in active if d.direction and not d.read_only],
        current=current, allowed=allowed, resting=resting,
    )
    # Exempt from the dwell stamp only when the vote gate was open: no
    # commanding room calling AND none still engaged mid-run. A safety vote
    # that takes the mode from a calling room or from an engaged run that is
    # coasting inside its band parks a running head -- a real reversal the
    # coordinator causes -- so it stamps like any other flip.
    read_only = full != plain and gate_open
    return Grant(direction=full, read_only=read_only, by_safety=by_safety)


class HeadSight:
    """What MXZ last saw of one head, and since when it has been out of sight.

    Adapted from ``Zone.observe`` / ``lost`` / ``status`` / ``vote`` in
    @csingley's ha-climate-zone-sequencer, commit f911e4a (``models.py``,
    MIT License; see NOTICE). The idea is his: out of sight, a head keeps its
    last-known vote for a time limit, because it may still be running that
    want and nothing can steer it either way, so dropping the vote at once
    would let a sibling contend blind. Past the limit the vote lapses.

    What differs here: the caller's clock is monotonic seconds; the boundary
    is half-open, as for a sensor's freshness cutoff (expired AT the limit,
    not after it); and a head not seen since MXZ started is timed from that
    start (:meth:`status` reads unknown until then, expired after), because
    every head is briefly unavailable while Home Assistant boots.

    It also keeps what the head was last seen running, for a room that votes
    by action (:meth:`action_vote`). That filter is MXZ's own extension of
    Chris's observed vote, not part of his design.
    """

    def __init__(self, started: float, started_wall: float) -> None:
        self.mode: str | None = None  # the state last seen; None if never seen
        # The head's own hvac_action last seen; None if it reported none.
        self.action: str | None = None
        # Whether it has reported one at all since MXZ started: a head that
        # has can't fall back to its mode by dropping it for one report.
        self.reports_action = False
        self.lost_at: float | None = started  # monotonic; None while in sight
        self.lost_wall: float | None = started_wall  # the same moment, for display
        # The heat or cool the head was last seen running in its own mode,
        # and when it was first seen no longer running it (None while it runs).
        self.ran: str | None = None
        self.stopped_at: float | None = None

    def observe(self, mode: str, action: Any = None, now: float | None = None) -> None:
        """Record the head's state and its own hvac_action, seen just now.

        A run counts only when the action agrees with the mode: heating in
        heat, cooling in cool. A head's action lags a mode change, so it can
        read heating in off or cooling in fan_only for a poll; such a
        contradiction is never a run, so it can't start, extend, or refresh
        one. A head that leaves the mode it ran in ends that run, hold and
        all; one that stops running in the same mode starts its hold, timed
        from the first sighting that shows it stopped. ``now`` (monotonic)
        is None only for a caller that never reads :meth:`action_vote`.
        """
        self.mode = mode
        self.action = action if isinstance(action, str) else None
        self.reports_action = self.reports_action or self.action is not None
        self.lost_at = None
        self.lost_wall = None
        if now is None:
            return
        if self.action is not None and _RUNNING.get(mode) == self.action:
            self.ran, self.stopped_at = mode, None
        elif mode != self.ran:
            self.ran, self.stopped_at = None, None
        elif self.stopped_at is None:
            self.stopped_at = now

    def lose(self, now: float, wall: float) -> None:
        """Record that the head can't be seen; the limit runs from the first call."""
        if self.lost_at is None:
            self.lost_at = now
            self.lost_wall = wall

    def status(self, now: float, ttl: float) -> str:
        """live, stale, expired, or unknown (see VOTE_* in const)."""
        if self.lost_at is None:
            return VOTE_LIVE
        if now - self.lost_at >= ttl:
            return VOTE_EXPIRED
        return VOTE_UNKNOWN if self.mode is None else VOTE_STALE

    def expires_in(self, now: float, ttl: float) -> float | None:
        """Seconds until the vote expires, or None (in sight or already expired)."""
        if self.lost_at is None or now - self.lost_at >= ttl:
            return None
        return ttl - (now - self.lost_at)

    def vote(self, now: float, ttl: float) -> str | None:
        """The heat or cool that counts right now: the last seen, live or stale."""
        if self.status(now, ttl) not in (VOTE_LIVE, VOTE_STALE):
            return None
        return self.mode if self.mode in (MODE_COOL, MODE_HEAT) else None

    def action_vote(self, now: float, ttl: float, hold: float) -> str | None:
        """:meth:`vote`, counted only while the head runs it, or for ``hold`` after.

        Never more than :meth:`vote`: this only drops a vote, it never makes
        one. A lost head is judged as it was last seen (the moment it was
        lost), so it keeps the vote it last had, running or held, stale until
        the time limit, exactly as :meth:`vote` does. The hold is half-open,
        like the time limit: over AT ``hold``, not after it.
        """
        vote = self.vote(now, ttl)
        if vote is None or vote != self.ran:
            return None
        if self.stopped_at is None:
            return vote
        seen = now if self.lost_at is None else self.lost_at
        return vote if seen - self.stopped_at < hold else None

    def hold_ends_in(self, now: float, hold: float) -> float | None:
        """Seconds until a stopped head's held vote drops, or None (no hold running).

        None while the head is out of sight: its vote is then kept as last
        seen, and :meth:`expires_in` times it.
        """
        if self.lost_at is not None or self.ran is None or self.stopped_at is None:
            return None
        left = hold - (now - self.stopped_at)
        return left if left > 0 else None


# The head's own hvac_action that means it is running its mode.
_RUNNING = {MODE_HEAT: "heating", MODE_COOL: "cooling"}


def floor_call(
    *,
    temp: float,
    sensor_ok: bool,
    low: float | None,
    high: float | None,
    prior: str | None,
    margin: float,
) -> str | None:
    """A disabled room's floor want: heat, cool, or None. Pure.

    Untripped (``prior`` None): heat below ``low``, cool above ``high``.
    Tripped heat holds until the room reaches ``low + margin`` (the clear
    line), tripped cool until ``high - margin``; then the floor is untripped
    and re-trips only past the floor again. The margin is the whole minimum
    run: no separate timer. Without a usable reading there is no want, from
    any prior: a floor never acts blind. ``None`` for ``low``/``high`` is no
    floor/ceiling.
    """
    if not sensor_ok:
        return None
    if prior == MODE_HEAT and low is not None and temp < low + margin:
        return MODE_HEAT
    if prior == MODE_COOL and high is not None and temp > high - margin:
        return MODE_COOL
    if low is not None and temp < low:
        return MODE_HEAT
    if high is not None and temp > high:
        return MODE_COOL
    return None


@dataclass(frozen=True)
class FloorRun:
    """One tripped floor's progress record (the no-progress cap's state).

    ``mark`` is the best reading credited this run (highest for heat, lowest
    for cool), moved only by a whole ``step``; ``driven`` is how many seconds
    MXZ has driven the head in the floor's direction since the mark last
    moved; ``worst`` is the worst reading (lowest for heat) in the last
    ``FLOOR_REARM_SECONDS`` of that driving, frozen once the run stalls, so
    only a genuinely new low past it re-arms the run. ``recent`` holds the
    readings that window needs, as (``driven`` when read, reading), oldest
    first: only those that are still in it and worse than every later one.
    """

    direction: str
    mark: float
    worst: float
    driven: float = 0.0
    stalled: bool = False
    recent: tuple[tuple[float, float], ...] = ()


def floor_progress(
    run: FloorRun, *, temp: float, driven_for: float, step: float, cap: float
) -> FloorRun:
    """Advance one floor run by one reading. Pure.

    ``driven_for`` is the time the head was actually driven in the run's
    direction since the last reading (0 while it waited). A reading one
    ``step`` past the mark moves the mark there and restarts the clock; a
    clock that reaches ``cap`` stalls the run. A stalled run re-arms (as a
    new run) only on a reading one step past its worst one in the last
    ``FLOOR_REARM_SECONDS`` of driving before the stall (decision 11, P9);
    reaching the clear line is the caller's other way out
    (:func:`floor_call`). The window is on the run's own clock (driven
    time), so a dip an hour of driving or more before the stall no longer
    counts. ``worst`` restarts at each gain and is frozen while stalled, so
    a room that keeps cooling in sub-step increments re-arms once it is one
    step below it. A sensor flickering between two adjacent values does
    neither: its low is in the window, and is already the window's worst,
    while that low recurs within each ``FLOOR_REARM_SECONDS`` of driving. A
    value held longer than that with nothing worse, then a reading one step
    past it, re-arms (P14).
    """
    sign = 1.0 if run.direction == MODE_HEAT else -1.0
    if run.stalled:
        if sign * (run.worst - temp) >= step - 1e-9:
            return FloorRun(direction=run.direction, mark=temp, worst=temp)
        return run
    gained = sign * (temp - run.mark) >= step - 1e-9
    if gained:
        return replace(run, mark=temp, worst=temp, driven=0.0, recent=())
    driven = run.driven + max(driven_for, 0.0)
    # A reading leaves the window once older than its span, or once a later
    # one is as bad or worse (it can no longer be the window's worst).
    recent = (
        *(
            (at, seen) for at, seen in run.recent
            if at >= driven - FLOOR_REARM_SECONDS and sign * (temp - seen) > 0
        ),
        (driven, temp),
    )
    return replace(
        run, worst=recent[0][1], driven=driven, stalled=driven >= cap, recent=recent
    )


def setpoints(
    *,
    mode: str,
    target: float,
    eco: bool,
    clamp_min: float,
    clamp_max: float,
    band: float = 2.0,
    step: float = 1.0,
    eco_cool: tuple[float, float] = (float(ECO_COOL_LOW), float(ECO_COOL_HIGH)),
    eco_heat: tuple[float, float] = (float(ECO_HEAT_LOW), float(ECO_HEAT_HIGH)),
    heat_target: float | None = None,
    cool_target: float | None = None,
) -> tuple[float, float]:
    """Return (low, high) setpoint edges, clamped. Mirrors the actuator's edge math.

    Unit-agnostic: ``band``/``step``/``eco_*`` carry the temperature unit. The
    defaults reproduce the original °F behavior (2° band, whole-degree steps,
    76/78 & 59/61 eco edges); a °C caller passes a 1° band, 0.5° step, and metric
    eco edges. Edges are rounded to ``step`` and clamped to [clamp_min, clamp_max].

    A dual-setpoint room builds them from ``cool_target`` (Cool to) while the
    mode is cool and from ``heat_target`` (Heat to) otherwise; either left
    None uses ``target``.
    """
    if eco:
        return eco_cool if mode == MODE_COOL else eco_heat
    side = cool_target if mode == MODE_COOL else heat_target
    tc = max(min(target if side is None else side, clamp_max), clamp_min)
    if mode == MODE_COOL:
        low, high = max(tc - band, clamp_min), tc
    else:
        low, high = tc, min(tc + band, clamp_max)
    return (_round_to(low, step), _round_to(high, step))


def range_delta(*, temp: float, low: float, high: float) -> float:
    """How far ``temp`` sits outside ``[low, high]``: the fan boost's distance.

    ``low - temp`` below the range, ``temp - high`` above it, 0 inside. A
    single target is ``low == high == target``, where this is
    ``abs(temp - target)`` exactly (IEEE subtraction is antisymmetric).
    """
    if temp < low:
        return low - temp
    if temp > high:
        return temp - high
    return 0.0


class RangeError(ValueError):
    """A dual-setpoint write that is refused; ``reason`` is its translation key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# RangeError reasons (each is an exception translation key in strings.json).
RANGE_OUT_OF_BOUNDS = "range_out_of_bounds"
RANGE_TOO_NARROW = "range_too_narrow"
RANGE_PUSH_OUT_OF_BOUNDS = "range_push_out_of_bounds"
RANGE_TOO_WIDE = "range_too_wide"


def resolve_range(
    *,
    low: float,
    high: float,
    new_low: float,
    new_high: float,
    gap: float,
    bounds: tuple[float, float],
    step: float,
) -> tuple[float, float]:
    """The (Heat to, Cool to) a dual-setpoint write lands on, or RangeError.

    ``low``/``high`` are the current pair and ``new_low``/``new_high`` the
    asked-for one (pass the current value for an edge that isn't written).
    Writes are refused, never silently clamped:

    * each edge snaps to ``step`` (the climate tile's snap) and must sit
      inside ``bounds``;
    * a pair at least ``gap`` wide is taken as it is;
    * when only ONE edge moved and the pair got too narrow (or crossed), the
      other edge is pushed to keep exactly ``gap``, the way ecobee and Nest
      behave; a push past ``bounds`` is refused;
    * when BOTH moved and the pair is too narrow or inverted, it's refused.
    """
    lo, hi = bounds
    nl, nh = _round_to(new_low, step), _round_to(new_high, step)
    if not (lo <= nl <= hi and lo <= nh <= hi):
        raise RangeError(RANGE_OUT_OF_BOUNDS)
    if nh - nl >= gap - 1e-9:
        return nl, nh
    low_moved, high_moved = nl != low, nh != high
    if low_moved and not high_moved:
        nh = nl + gap
        if nh > hi:
            raise RangeError(RANGE_PUSH_OUT_OF_BOUNDS)
        return nl, nh
    if high_moved and not low_moved:
        nl = nh - gap
        if nl < lo:
            raise RangeError(RANGE_PUSH_OUT_OF_BOUNDS)
        return nl, nh
    raise RangeError(RANGE_TOO_NARROW)


def shift_range(
    *,
    width: float,
    center: float,
    bounds: tuple[float, float],
    step: float,
) -> tuple[float, float]:
    """A ``width``-wide (Heat to, Cool to) centered on ``center``, inside ``bounds``.

    What writing a dual-setpoint room's target does, and how a range is
    seeded and re-centered. Heat to snaps to ``step`` (halves round up, so a
    range whose width is an odd number of steps sits half a step above
    ``center`` rather than flipping with Python's half-to-even rounding);
    Cool to is Heat to plus ``width`` exactly. A range that would cross a
    bound slides back inside, still ``width`` wide; one wider than the
    bounds is refused.
    """
    lo, hi = bounds
    if width > hi - lo + 1e-9:
        raise RangeError(RANGE_TOO_WIDE)
    low = center - width / 2
    if step > 0:
        low = math.floor(low / step + 0.5) * step
    low = min(max(low, lo), hi - width)
    return low, low + width


def snapped_midpoint(*, low: float, high: float, step: float) -> float:
    """The target a dual-setpoint room returns to: its midpoint, on ``step``.

    The climate tile's own snap (``round(x / step) * step``).
    """
    return _round_to((low + high) / 2, step)


def _round_to(value: float, step: float) -> float:
    """Round ``value`` to the nearest multiple of ``step`` (step<=0 -> unchanged)."""
    if step <= 0:
        return value
    return round(value / step) * step


def fan_for_delta(
    *,
    delta: float,
    cur_idx: int,
    up_at: tuple[float, ...],
    down_at: tuple[float, ...],
    max_idx: int,
) -> int:
    """Pick a fan-ladder INDEX (0..) from how far the room is off-target.

    Tesla-style: a big ``delta`` steps the fan up toward ``max_idx``; as the room
    closes on target the fan eases down. ``up_at``/``down_at`` carry a 0.5 °F
    hysteresis band so the speed doesn't chatter on the boundary. The caller owns
    persisting ``cur_idx`` between calls (per head).
    """
    # step UP as far as delta allows, never above max_idx
    while cur_idx < len(up_at) and cur_idx < max_idx and delta >= up_at[cur_idx]:
        cur_idx += 1
    # step DOWN only once delta clears the lower rung's down-threshold
    while cur_idx > 0 and delta < down_at[cur_idx - 1]:
        cur_idx -= 1
    return min(cur_idx, max_idx)


def head_action(*, engage: str, mode: str, eco: bool, idle: str = MODE_FAN_ONLY) -> str:
    """Map a head's engage state to the mode to command. Mirrors p_act/s_act.

    ``idle`` is the pre-resolved parking mode (fan_only or off) for a head that
    is neither disabled nor running — satisfied rooms and standoff losers both
    land there. Precedence: disabled > eco-satisfied > running > idle.
    """
    if engage == MODE_OFF:
        return MODE_OFF
    if eco and engage == ENGAGE_SATISFIED:
        return MODE_OFF
    if engage == mode:
        return mode
    return idle
