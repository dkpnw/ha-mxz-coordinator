"""The manual fan hold's decision: whether a fan speed a head shows is a person's pick.

Standard library only, so the simulator and the tests call it without Home Assistant.
``fan_hold.FanHold`` reads the head, the settings and the clock, calls these, and keeps the
result. A person's pick latches the head, and MXZ makes no fan write to it until it is handed
back, on the room's Fan auto switch or by the head reading "auto". MXZ's own writes, which a
head can show late, never latch it (``FanEcho``).

One head's state is a ``FanLatch``. Each head report is first passed to
``FanLatch.echo.caught_up``, then ``observe_fan`` reads the speed for the next fan write.
A start's record comes back through ``restored`` and a Fan auto gesture through ``fan_auto``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from .const import (
    FAN_AUTO,
    FAN_LADDER,
    FAN_OLDER_WRITES,
    IDLE_ACTION_FAN_ONLY,
    IDLE_SEED_MIN_ENTRY_AGE,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
)
from .logic import fan_for_delta


class FanEcho:
    """The fan speeds MXZ wrote to one head that the head may still show.

    ``last`` is the last speed commanded or adopted, and ``prior`` the one
    before it: a head can briefly still show that one, so a speed is a
    person's only if it is neither. A head can lag further than that (F1,
    docs/formal/PARKED-FINDINGS.md): ``older`` holds the speeds MXZ wrote that
    this head may yet show, in the order written, a speed written twice
    appearing twice. Its reports drop what came before them, so a head caught
    up leaves the last write alone in it (see ``caught_up``). ``None`` means no
    memory yet, and an empty ``older`` no older write.

    The latch sets its own baseline by starting a new one: a hold, an adopted
    speed, a stamp of ``auto``, a restore.
    """

    def __init__(
        self, last: str | None = None, prior: str | None = None, older: Iterable[str] = (),
    ) -> None:
        self.last = last
        self.prior = prior
        self.older: deque[str] = deque(older, maxlen=FAN_OLDER_WRITES + 2)

    def wrote(self, token: str) -> None:
        """Roll the memory for a fan speed MXZ is sending.

        A rewrite of the token already commanded rolls nothing (see
        ``FanHold._async_write``). With no memory yet, as after a start or a
        hold, it starts from the two before this one, which the head may still
        show (F1's cost, docs/formal/PARKED-FINDINGS.md). ``auto`` counts like a
        speed in it: a later report of ``auto`` may be that first one.
        """
        last = self.last
        if token == last:
            return
        prev = self.prior
        self.prior = token if last is None else last
        self.last = token
        if last is None:
            return
        memory = self.older
        if not memory:
            memory.extend(dict.fromkeys(written for written in (prev, last) if written is not None))
        memory.append(token)

    def own(self, observed: str) -> bool:
        """Whether a speed the head shows can be one MXZ wrote: never a person's pick.

        One of the last two, or an older write the head's reports haven't
        passed yet. ``auto`` is not asked about: its report releases a hold
        whatever is remembered.
        """
        if observed in (self.last, self.prior):
            return True
        return observed in self.older_commands()

    def caught_up(self, observed: Any) -> bool:
        """A head's report: the writes before it are behind the head for good.

        A head shows its writes in order, so it can't show those again, and a
        pick of one from here is a pick. A speed written more than once counts as
        its earliest write left: this report may be that one, with the rest of
        the writes still coming. A report of ``prior`` drops nothing, which is
        wider than it has to be and only for a head still one write behind.

        A report of the last command with no other write left has caught up, and
        the memory stays as that one write. The next write adds to it, so a speed
        the head has passed doesn't come back. That can't tell a head that jumped
        straight to the last of two writes of a speed, so the writes between
        stay echoes until a report rules them out (F1's cost,
        docs/formal/PARKED-FINDINGS.md).

        Returns whether the memory changed.
        """
        memory = self.older
        if not memory or observed not in memory:
            return False
        changed = False
        if observed == self.last or observed != self.prior:
            changed = memory[0] != observed
            while memory[0] != observed:
                memory.popleft()
        return changed

    def older_commands(self) -> list[str]:
        """The older writes this adds to the last two, oldest first, each once.

        Never one of the last two, and never auto.
        """
        skip = (self.last, self.prior, FAN_AUTO)
        return [token for token in dict.fromkeys(self.older) if token not in skip]


@dataclass(frozen=True, slots=True)
class FanLatch:
    """One head's manual fan hold, as a decision reads it and leaves it.

    ``held`` is None until something decides it, which reads as not held.
    ``restored`` is a start's held-or-not truth, not yet read against a speed
    the head shows. ``on_pending`` is a Fan auto on given while the head showed
    no speed, waiting for its first one. ``rung`` is the boost ladder's index,
    None when none is kept.
    """

    echo: FanEcho
    held: bool | None = None
    restored: bool | None = None
    on_pending: bool = False
    rung: int | None = None


@dataclass(frozen=True, slots=True)
class FanLimits:
    """What the decision reads of the settings and the clock.

    ``ceiling`` is the highest ladder index boost may write. ``resumed`` is
    whether this entry's coordinator switch came back on at the start, and
    ``age`` how long the entry has existed (None when it doesn't know).
    """

    ceiling: int
    up_at: tuple[float, ...]
    down_at: tuple[float, ...]
    eco_idle: bool
    idle_action: str
    resumed: bool
    age: timedelta | None


def observe_fan(
    latch: FanLatch, observed: str, act: str, delta: float, limits: FanLimits
) -> FanLatch:
    """Read the speed a head shows (``observed``, never None) for the manual fan hold.

    ``act`` is the mode this head is run in and ``delta`` how far its room is
    off target; the returned latch's ``held`` says whether MXZ leaves the fan
    alone. The report was passed to ``latch.echo.caught_up`` first, as every
    head report is. The hold moves off that OBSERVED speed:

    * a pending restore (the first speed observed after a restart, reload or
      standby release) is reconciled by ``reconcile_restored_hold``:
      restored truth beats token guessing
    * observed "auto"                 -> released (user handed control back)
    * observed token MXZ can't have written -> latched (a user departure;
      ``FanEcho.own`` absorbs the echo race where the head still reports
      the token we wrote one cycle ago, or an older write the head's
      reports haven't passed yet)
    * no echo memory yet (first compute with nothing restored): a
      non-"auto" reading seeds LATCHED, mirroring the engage-latch's "resume
      from the head's own state" — a manual pick that predates the restart
      is honored — UNLESS the reading is a speed the ladder would hold at
      the current delta, which is our own boost speed echoing back (see
      ``seed_matches_boost``), or our own fan_only idle on an established
      entry (see ``seed_is_own_idle``).

    A hold ends ONLY on a gesture: the Fan-auto switch, or an observed "auto".
    Room drift, target changes, and slider moves between speeds never release
    one, at any speed including max.

    History, because this rule got simpler twice: through v2.17.0 a slider-set
    hold at the head's top token ALSO merged back into auto on any cycle where
    the ladder would have commanded max anyway — a hold releasing itself with
    no user gesture. Through v2.18.0 a *departure* to that top token was
    likewise read as "hand it back", because HomeKit's fan slider has no
    "auto" stop and a user who latched a zone from Apple Home had no on-slider
    way out. The per-zone Fan-auto switch bridges to HomeKit as a plain toggle
    and is that way out, so both readings of "max means give it back" are
    gone: max now holds like any other speed.

    Because the coordinator only sees observed state per cycle — not events —
    a re-gesture to the SAME token the head already reports is invisible; that
    limitation is accepted.
    """
    if latch.on_pending:
        return adopt(replace(latch, on_pending=False, restored=None), observed, limits.ceiling)
    if latch.restored is not None:
        return reconcile_restored_hold(
            replace(latch, restored=None), observed, latch.restored, act, limits
        )

    echo = latch.echo
    if observed == FAN_AUTO:
        # Observed auto releases everything.
        if echo.last is None:
            # Baseline stamp: without it a head idle-at-auto since startup
            # keeps an empty command memory (the return-to-auto write is
            # idempotency-skipped), so a LATER live pick would arrive as an
            # ambiguous seed instead of a clean departure.
            echo = FanEcho(FAN_AUTO, FAN_AUTO)
        return replace(latch, held=False, echo=echo)

    seeding = echo.last is None
    departed = not seeding and not echo.own(observed)
    if not (seeding or departed):
        # A standing hold: no gesture this cycle, so nothing changes.
        return latch

    # A seed at exactly the speed the ladder would command right now is our
    # own boost speed echoing back, not a manual pick -> adopt it and keep
    # driving. A DEPARTURE never adopts: every slider move is a hold.
    if seeding:
        if seed_is_own_idle(observed, act, limits):
            # Baseline stamped so a slow echo of this token isn't a
            # departure; the return-to-auto write then proceeds.
            return replace(latch, held=False, echo=FanEcho(observed, observed))
        idx = seed_matches_boost(observed, act, delta, limits)
        if idx is not None:
            return adopt(latch, observed, idx)

    # Seed latched (not at auto, not our rung); a departure always latches.
    return hold_at(latch, observed)


def reconcile_restored_hold(
    latch: FanLatch, observed: str, held: bool, act: str, limits: FanLimits
) -> FanLatch:
    """Consume a restored hold truth (``held``) against the first speed the head reports.

    The Fan-auto switch restores the held/not-held bool and, for a zone that
    was not held, the previous incarnation's command/observation baselines
    (already in the echo memory, see ``restored``). The token itself is
    always taken from the OBSERVED head.

    * observed "auto": released — including a hold the user let go of during
      the outage. Restored command memory stays, so a late report of a
      pre-restart token is still read as our own echo, not a departure.
    * held: still held, at the observed token (same token: the hold simply
      survived; different non-auto token: the user moved the hold during the
      outage — theirs either way).
    * not held: boost was driving. A token boost could never have written —
      outside the ladder, above the ceiling — holds, even if remembered:
      memory can contain user picks. An in-ceiling ladder token is ours
      when there is no memory, when it is remembered, or when the
      room is idling in fan_only under idle_action fan_only (#25): such a
      head can report a speed of its own while idling, and a hold there never
      self-corrects because the room never reads a clean "auto". Otherwise
      (an active room, another idle action) a nonmatching token
      conservatively holds; a stale dump can cause a false hold, so this is
      NOT proof of a manual gesture. The documented edge: a pick made while
      HA itself was down is indistinguishable from residue wherever the
      token is read as ours. Never a fixed-point guess: a mere change of
      delta is not a manual gesture. An active room resumes the ladder from
      the observed rung.
    """
    echo = latch.echo
    if observed == FAN_AUTO:
        if echo.last is None:
            echo = FanEcho(FAN_AUTO, FAN_AUTO)
        return replace(latch, held=False, echo=echo)
    if held:
        return hold_at(latch, observed)
    remembered = echo.own(observed)
    ours = (
        observed in FAN_LADDER
        and FAN_LADDER.index(observed) <= limits.ceiling
        and (
            echo.last is None
            or remembered
            or (act == MODE_FAN_ONLY and limits.idle_action == IDLE_ACTION_FAN_ONLY)
        )
    )
    if not ours:
        return hold_at(latch, observed)
    if not remembered:
        # Baseline stamped so a slow echo of this token isn't a departure.
        echo = FanEcho(observed, observed)
    rung = latch.rung
    if act in (MODE_COOL, MODE_HEAT) and observed in FAN_LADDER:
        rung = min(FAN_LADDER.index(observed), limits.ceiling)
    return replace(latch, held=False, echo=echo, rung=rung)


def hold_at(latch: FanLatch, observed: str) -> FanLatch:
    """Latch the head at the token it reports, as a STANDING hold.

    Records the held token as the command baseline so the zone stays held
    next cycle (the same trick a Fan auto off uses). Without this
    the identical reading re-reads as a fresh seed/departure every cycle and
    the latch decision is re-litigated forever — which is how the v2.18.0
    "standing merge" removal was defeated for slider holds: the departure
    branch kept re-running the max handback, so a max hold still released
    itself on drift.
    """
    return replace(latch, held=True, echo=FanEcho(observed, observed))


def adopt(latch: FanLatch, observed: str, rung: int) -> FanLatch:
    """Release the latch and adopt the observed token into the boost's memory.

    Rolls prior, records the observed token as the last command, and pins
    the rung so the ladder continues from where the head actually is: the
    natural ramp proceeds idempotently under DOWN_AT hysteresis, and the head
    doesn't read as a fresh departure and re-latch once the ladder steps off
    that token. ``rung`` is the ladder index to resume from — the seed adopt
    passes the index it matched; the Fan-auto switch passes the ceiling, which
    only ever ramps down from there.
    """
    last = latch.echo.last
    return replace(
        latch, held=False, echo=FanEcho(observed, observed if last is None else last), rung=rung,
    )


def seed_matches_boost(observed: str, act: str, delta: float, limits: FanLimits) -> int | None:
    """Ladder index if a seed reading is our own boost speed, else ``None``.

    The latch seeds from whatever the head reports on the first compute after
    a restart, and a head boost had been driving is reporting OUR speed — not
    a manual pick. Latching that parks the zone silently (Fan auto reads OFF,
    boost stops driving it) until a human hands it back, after EVERY restart
    that catches a room conditioning — including the weekly update.

    So: while actively conditioning, if the observed token is a rung the
    ladder would STAY on at the current delta — a fixed point under the
    UP_AT/DOWN_AT hysteresis — it's a speed the boost could legitimately
    have parked the head at, so it's ours: adopt it and keep driving. The
    hysteresis makes this a BAND, not a single value (as a room closes in,
    DOWN_AT holds the fan a rung or two above what a cold read would pick;
    that is the boost's normal resting state, not a manual hold). Checking
    only the cold read (cur_idx=0) — as v2.19.0/beta.18 did — matched just
    the bottom rung of that band, so a restart mid-ramp-down still parked
    the head as "held".

    A real manual hold that predates the restart reads as a token OUTSIDE
    the band — above the boost ceiling, or past a hysteresis edge — so it
    still latches. The accepted tradeoff: a hold deliberately placed inside
    the band is indistinguishable from the boost's own speed and is lost to
    adoption. Only reachable on a seed — a departure is always a hold.
    """
    if act not in (MODE_COOL, MODE_HEAT) or limits.eco_idle:
        return None
    if observed not in FAN_LADDER:
        return None
    obs_idx = FAN_LADDER.index(observed)
    if obs_idx > limits.ceiling:
        return None  # above the boost ceiling: never ours
    idx = fan_for_delta(
        delta=delta,
        cur_idx=obs_idx,
        up_at=limits.up_at,
        down_at=limits.down_at,
        max_idx=limits.ceiling,
    )
    return obs_idx if idx == obs_idx else None


def seed_is_own_idle(observed: str, act: str, limits: FanLimits) -> bool:
    """True if a seed with no restore data is our own fan_only idle (#25).

    idle_action="fan_only" drives a satisfied head to "auto" every cycle,
    yet a real head can report a concrete token while idling that way.
    Seeding that as a hold never self-corrects: unlike an active seed, the
    zone never leaves fan_only for a clean "auto" reading. So on a restart
    of an ESTABLISHED coordinator — this entry's own coordinator switch
    restored ON (it was running before), and the entry at least
    ``IDLE_SEED_MIN_ENTRY_AGE`` old — an in-ceiling ladder token boost
    could have written is read as ours. A first-ever compute (fresh
    install or re-added entry: nothing restored ON; or a first live
    enable, however long after install) keeps honoring a pre-install
    manual pick (S10). The accepted edge: a pick made while the zone
    idled in fan_only, followed by a restart that loses the Fan-auto
    restore, is indistinguishable and is not held.
    """
    if (
        act != MODE_FAN_ONLY
        or limits.idle_action != IDLE_ACTION_FAN_ONLY
        or not limits.resumed
        or observed not in FAN_LADDER
        or FAN_LADDER.index(observed) > limits.ceiling
    ):
        return False
    return limits.age is not None and limits.age >= timedelta(seconds=IDLE_SEED_MIN_ENTRY_AGE)


def restored(
    latch: FanLatch,
    held: bool,
    *,
    last: str | None,
    prior: str | None,
    older: Iterable[str],
    on_pending: bool,
    ceiling: int,
) -> FanLatch:
    """A start's record of the head's hold, waiting for the first speed it reports.

    It replaces any record restored before. ``held`` is the pre-restart truth,
    which ``observe_fan`` reconciles against the observed head state.

    ``last``/``prior`` are command/adopted-observation baselines from the
    previous incarnation, when the record carried them. For a zone that was
    NOT held they re-arm the echo tolerance across the restart, so a head that
    reports a pre-restart token late — after a provisional ``auto``, or once
    its missing speed arrives — is read as our own echo, not as a departure. A
    held zone's memory is the held token itself: the seed records it from the
    observed head, so nothing is carried for it (a hold released during the
    outage stays released, and a later non-auto report on it is a new hold
    either way).

    ``older`` is the same for a head that was further behind at the stop
    (``FanHold.older_record``). Only speeds boost could write are taken back:
    anything else is no echo.

    Only a strictly parsed fresh not-held record can carry an explicit waiting
    Fan auto on (``on_pending``). It adopts ONE first usable report, including
    a user token above the ceiling, just like a live Fan auto on; it proves no
    own echo.
    """
    echo = FanEcho()
    if not held and last is not None:
        # Memory includes adopted user tokens, not just our commands.
        # S11b wins even when an impossible token is reported AFTER a
        # provisional auto. Such a token cannot arm restored echo tolerance.
        def eligible(token: str) -> str:
            if token in FAN_LADDER and FAN_LADDER.index(token) <= ceiling:
                return token
            return FAN_AUTO

        command = eligible(last)
        before = eligible(last if prior is None else prior)
        kept = [token for token in older if eligible(token) == token]
        last_two = dict.fromkeys((before, command))
        echo = FanEcho(command, before, (*kept, *last_two) if kept else ())
    return replace(
        latch, echo=echo, restored=held, on_pending=on_pending is True and held is False,
    )


def fan_auto(
    latch: FanLatch, observed: str | None, on: bool, ceiling: int
) -> tuple[FanLatch, bool]:
    """A Fan auto gesture against the speed the head shows: the new latch, and whether it acted.

    ON  -> release the latch and ADOPT the head's current observed token into
           the boost memory, so the still-at-manual-speed head doesn't read as
           a fresh departure and immediately re-latch. With no speed showing it
           waits for the first one (``on_pending``).
    OFF -> latch at whatever the head is doing right now (a deliberate hold).
           Latching "at auto" is meaningless, so an observed ``auto`` is
           ignored (the switch stays ON), and so is no speed with no Fan auto
           on waiting. With one waiting, OFF cancels it without guessing a
           token: the first non-auto report becomes the held speed, and an
           observed auto still releases as usual. The echo memory is seeded
           with the observed token so the observation treats the hold as
           already accounted for, not a new departure. The hold sticks at any
           speed, max included, until the switch (or an observed ``auto``)
           releases it.

    A gesture replaces a restored truth still waiting for a speed. With fan
    boost disabled the fan machinery is inert: OFF still records the latch (it
    simply has no effect until boost is re-enabled), and ON still clears it —
    the switch stays honest either way. Ignored, nothing is published.
    """
    if on:
        latch = replace(latch, restored=None, on_pending=False)
        if observed is not None:
            return adopt(latch, observed, ceiling), True
        return replace(latch, held=False, on_pending=True), True
    if observed is None or observed == FAN_AUTO:
        if observed is None and latch.on_pending:
            return replace(latch, on_pending=False, restored=True, held=True), True
        if observed == FAN_AUTO:
            latch = replace(latch, restored=None)
        return latch, False
    last = latch.echo.last
    return replace(
        latch,
        restored=None,
        echo=FanEcho(observed, observed if last is None else last),
        held=True,
        on_pending=False,
    ), True
