"""Fan boost and the manual fan hold: the speed MXZ writes to each head, and when it writes none.

Fan boost drives a running head's fan speed from how far its room is off target,
and hands a satisfied head back to the firmware's own "auto". A person who picks a
speed at the head keeps it: that head latches, and MXZ makes no fan write to it
until it is handed back, on the room's Fan auto switch or by the head reading
"auto". The Fan auto switch carries the latch and the echo memory across a restart
in its restore data.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

from homeassistant.util import dt as dt_util

from .const import (
    CLOUD_FAN_STEP_SECONDS,
    FAN_AUTO,
    FAN_LADDER,
    FAN_OLDER_WRITES,
    IDLE_ACTION_FAN_ONLY,
    IDLE_SEED_MIN_ENTRY_AGE,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
)
from .host import ControllerHost
from .logic import fan_for_delta

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .cloud import CloudHead

_LOGGER = logging.getLogger(__package__)


class FanHost(ControllerHost, Protocol):
    """The coordinator members the fan reads or calls, beyond every controller's.

    Read: the fan boost settings, eco idle, the idle action, whether this
    entry's coordinator switch came back on at a start, the entry (its age),
    and a cloud head's write record (whether a fan report can be older than
    its last batch). Called: the head service a fan write sends.
    """

    fan_boost_enable: bool
    fan_boost_max: str
    fan_up_at: tuple[float, ...]
    fan_down_at: tuple[float, ...]
    eco_idle: bool
    idle_action: str
    restored_coordinator_on: bool
    config_entry: ConfigEntry

    def cloud_head(self, climate_id: str) -> CloudHead | None: ...
    async def async_head_service(
        self, climate_id: str, service: str, payload: dict[str, Any],
        *, manual: bool = False, vane: bool = False,
    ) -> None: ...


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


class FanHold:
    """Fan boost and the manual fan hold of every head, with what a restart restores of them.

    Per head: the boost ladder's rung, the echo memory (``FanEcho``), whether
    a person's pick is held, the Fan auto switch's restored truth until the
    first speed read settles it, an explicit Fan auto on still waiting for a
    speed, the switch's state writer, and why a restore record could not be
    used.

    The coordinator calls it from a delivery (``async_apply``, ``cloud_token``,
    ``sent``, ``drop_rung``), the apply for a head it sends no fan (``read``),
    a head's report (``head_reported``), the plan and the delivery intent
    (``auto_is_on``), the standby release (``reseed_after_standby``), its
    control reasons and diagnostics. The Fan auto switch calls ``restore``,
    ``restore_failed``, ``attach``, ``command_memory``, ``older_record``,
    ``on_pending``, ``auto_is_on`` and ``async_set_auto``.
    """

    def __init__(self, host: FanHost) -> None:
        """Nothing written, held or restored yet."""
        self._host = host
        self._idx: dict[str, int] = {}  # the ladder's rung (hysteresis state)
        self._echo: defaultdict[str, FanEcho] = defaultdict(FanEcho)
        self._latched: dict[str, bool] = {}
        # Pre-restart latch truth restored by the Fan auto switch
        # (RestoreEntity), consumed once at the first seed observation per
        # head. Reconciliation always reads the token from the OBSERVED head
        # state; explicit pending ON is a separate saved instruction. Absent or
        # stale restore data -> plain seeding (see _observe_latch).
        self._restore: dict[str, bool] = {}
        # Explicit ON with no current speed is an instruction, not evidence
        # that the first speed subsequently reported was a manual departure.
        self._on_pending: set[str] = set()
        # The Fan auto entities publish clean state and extra data from the
        # same synchronous memory change, even mid-apply. This is
        # observation/intent, not evidence that a service reached hardware.
        self._writers: dict[str, Callable[[], None]] = {}
        # A restore record that existed but could not be used (stale or
        # malformed), set by the Fan auto switch. No record at all — a fresh
        # install or newly added room — is not a problem and adds no reason.
        self._restore_problem: dict[str, str] = {}

    # -- boost: what a delivery sends -----------------------------------------
    async def async_apply(self, climate_id: str, act: str, delta: float) -> None:
        """Drive the head's fan speed from ``delta`` (°F off-target). Idempotent.

        Only active when fan boost is enabled. An actively-conditioning head
        (cool/heat, not eco-idle) gets a ladder speed proportional to how far the
        room is off target; a satisfied/fan_only head (or eco-idle) is returned to
        the firmware's own "auto"; an off head is left alone.

        A manual pick latches (see ``_observe_latch``): while a head is latched
        the coordinator makes NO fan writes to it, so a user's chosen speed
        survives every apply cycle. The latch is evaluated first because it also
        suppresses the return-to-"auto" branch; it releases only on an observed
        "auto".
        """
        host = self._host
        if not host.fan_boost_enable:
            return

        state = host.hass.states.get(climate_id)
        if state is None:
            return
        modes = state.attributes.get("fan_modes")
        if not modes or FAN_AUTO not in modes:
            # No fan control, or no "auto" to release the latch (or return a
            # satisfied head to) -> leave the head's fan entirely alone. This
            # supersedes the old best-effort ladder writes to auto-less heads,
            # which could only ratchet the fan up with no way back down.
            return

        observed = state.attributes.get("fan_mode")
        if observed is None:
            # Fan control is advertised but no current speed is reported (a
            # head still loading, or one that publishes its speed late). There
            # is nothing to reconcile a restored hold or the latch against, and
            # a write now would override a speed nobody can see — so no fan
            # decision this cycle. Restored truth stays pending and a standing
            # hold stays standing until the head reports a speed.
            return

        # Evaluate the manual-fan latch from what the head is actually reporting.
        held = self._observe_latch(climate_id, observed, act, delta)
        self.publish(climate_id)
        if held:
            return  # latched: leave the user's fan pick untouched

        if act in (MODE_COOL, MODE_HEAT) and not host.eco_idle:
            max_idx = self._max_idx()
            idx = fan_for_delta(
                delta=delta,
                cur_idx=self._idx.get(climate_id, 0),
                up_at=host.fan_up_at,
                down_at=host.fan_down_at,
                max_idx=max_idx,
            )
            self._idx[climate_id] = idx
            token = FAN_LADDER[idx]
        elif act == MODE_FAN_ONLY or host.eco_idle:
            self._idx.pop(climate_id, None)
            token = FAN_AUTO
        else:  # MODE_OFF
            self._idx.pop(climate_id, None)
            return

        if token not in modes:
            return  # head lacks this ladder token -> skip safely
        if observed == token:
            return  # idempotent
        await self._async_write(climate_id, token)

    def cloud_token(
        self, climate_id: str, cloud: CloudHead, state: Any, fan_act: str,
        delta: float, clock: float,
    ) -> str | None:
        """The fan token this cloud batch carries, else None (no fan write).

        ``fan_act`` is the batch's mode, or fan_only for a hand-back to auto
        on the way to off (the delivery decides that, as for a local head).

        Local fan boost, with two differences. No fan decision on a report of
        an older fan inside a confirm wait: it is a poll from before the last
        batch, and the manual-fan latch must not read it as a person's pick
        (a fan the head never had is read as usual). And a boost speed is
        written at most once per
        CLOUD_FAN_STEP_SECONDS, one ladder rung at a time (a start counts as
        a write); a step held back sends nothing and wakes the plan when due.
        As locally, only the head's own tokens are sent: a ladder rung it
        doesn't list is passed over. The rung is committed by ``sent``, once
        the batch goes out.
        """
        host = self._host
        if not host.fan_boost_enable:
            return None
        modes = state.attributes.get("fan_modes")
        if not modes or FAN_AUTO not in modes:
            return None
        observed = state.attributes.get("fan_mode")
        if observed is None or cloud.stale(clock, fan=observed):
            # Nothing to decide on: no speed, or a late report of an older
            # one inside the confirm wait (not a person's pick).
            return None
        held = self._observe_latch(climate_id, observed, fan_act, delta)
        self.publish(climate_id)
        if held:
            return None
        if fan_act in (MODE_COOL, MODE_HEAT) and not host.eco_idle:
            cur = self._idx.get(climate_id, 0)
            want = fan_for_delta(
                delta=delta, cur_idx=cur, up_at=host.fan_up_at,
                down_at=host.fan_down_at, max_idx=self._max_idx(),
            )
            if want != cur:
                want = cur + (1 if want > cur else -1)
            token = FAN_LADDER[want]
            if observed == token:
                self._idx[climate_id] = want
                return None
            since = clock - (cloud.fan_at if cloud.fan_at is not None else -math.inf)
            if since < CLOUD_FAN_STEP_SECONDS:
                # Not yet: the head keeps the fan it has, and the plan wakes
                # when the step is due.
                cloud.release_at = clock + CLOUD_FAN_STEP_SECONDS - since
                return None
            if token in modes:
                return token  # its rung is committed when the batch goes out
            # A rung this head doesn't list is passed over without a write,
            # so it doesn't spend the step: the next rung can follow at once.
            self._idx[climate_id] = want
            return None
        elif fan_act == MODE_FAN_ONLY or host.eco_idle:
            self._idx.pop(climate_id, None)
            token = FAN_AUTO
        else:  # off
            self._idx.pop(climate_id, None)
            return None
        if token not in modes or observed == token:
            return None
        return token

    def sent(self, climate_id: str, token: str) -> None:
        """A cloud batch carrying this fan token went out.

        A boost rung counts only once it goes out: a batch the write interval
        held must not climb on while it waits. The echo memory rolls exactly
        as a local fan write rolls it.
        """
        if token in FAN_LADDER:
            self._idx[climate_id] = FAN_LADDER.index(token)
        self._echo[climate_id].wrote(token)
        self.publish(climate_id)

    def drop_rung(self, climate_id: str) -> None:
        """A local hand-back to auto went out: the ladder starts from the bottom next run."""
        self._idx.pop(climate_id, None)

    def read(self, climate_id: str, act: str) -> None:
        """Read the fan of a head MXZ sends no fan, for the manual-fan hold; write nothing.

        A head paused for an open door or window (act off), or one a person
        runs in dry under Allow Dry (act dry). A speed a person picks there is
        theirs: it holds from then on, on the Fan auto switch and across a
        restart, as one picked in a run does, so the resume, a park for heat
        and every apply after keep that pick. MXZ set no speed there, so a
        speed seen is never read as its own boost or idle. A cloud head's late
        report of an older speed is not read at all (as in a cloud batch).
        """
        host = self._host
        if not host.fan_boost_enable:
            return
        state = host.hass.states.get(climate_id)
        if state is None:
            return
        modes = state.attributes.get("fan_modes")
        observed = state.attributes.get("fan_mode")
        if not modes or FAN_AUTO not in modes or observed is None:
            return
        cloud = host.cloud_head(climate_id)
        if cloud is not None and cloud.stale(host.clock(), fan=observed):
            return
        self._observe_latch(climate_id, observed, act, 0.0)
        self.publish(climate_id)

    def _max_idx(self) -> int:
        """Ladder index the boost is allowed to reach, respecting fan_boost_max."""
        ceiling = self._host.fan_boost_max
        return FAN_LADDER.index(ceiling) if ceiling in FAN_LADDER else len(FAN_LADDER) - 1

    async def _async_write(self, climate_id: str, token: str) -> None:
        """Issue a fan_mode write and remember it (last + prior, for the echo race).

        A REWRITE of the token already commanded (the idempotency path retrying
        a write a slow head hasn't applied yet) must NOT roll the memory: prior
        keeps the pre-write token for as long as the head lags, so the stale
        reading stays echo-tolerated instead of counting as a user departure.
        Rolling it was #14 — at the satisfied auto-handback, a compute burst
        rewrote auto until prior==last==auto and the head's own stale boost
        token latched as a manual hold with no user anywhere near it.

        prior therefore survives until the next DISTINCT command — the original
        design's invariant. The cost is unchanged from what the README already
        documents: a user re-pick of the token prior remembers is invisible.
        Speeds older than prior stay echoes only until the head reports a later
        write (``FanEcho.older``), so the same is true of those for that long.
        """
        self._echo[climate_id].wrote(token)
        self.publish(climate_id)
        await self._host.async_head_service(
            climate_id, "set_fan_mode",
            {"fan_mode": token},
        )

    # -- the manual-fan latch -------------------------------------------------
    def head_reported(self, climate_id: str, observed: Any) -> None:
        """A head's report of a speed: the writes before it are behind it (``FanEcho.caught_up``).

        It is checked on the head's own report, not only at the next compute: a
        pick can arrive before that compute runs.
        """
        if self._echo[climate_id].caught_up(observed):
            self.publish(climate_id)

    def _observe_latch(
        self, climate_id: str, observed: str, act: str, delta: float
    ) -> bool:
        """Update and return the manual-fan latch for a head from its state.

        Called once per apply per head, guaranteed the head has fan_modes with an
        "auto" token and reports a current speed (``observed``). Latch
        transitions off that OBSERVED fan_mode:

        * a pending restore (the first speed observed after a restart, reload or
          standby release) is reconciled by ``_reconcile_restored_hold``:
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
          ``_seed_matches_boost``), or our own fan_only idle on an established
          entry (see ``_seed_is_own_idle``).

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
        self.head_reported(climate_id, observed)
        if climate_id in self._on_pending:
            self._on_pending.remove(climate_id)
            self._restore.pop(climate_id, None)
            self._adopt(climate_id, observed)
            return False
        restored = self._restore.pop(climate_id, None)
        if restored is not None:
            return self._reconcile_restored_hold(climate_id, observed, restored, act)

        echo = self._echo[climate_id]
        if observed == FAN_AUTO:
            # Observed auto releases everything.
            self._latched[climate_id] = False
            if echo.last is None:
                # Baseline stamp: without it a head idle-at-auto since startup
                # keeps an empty command memory (the return-to-auto write is
                # idempotency-skipped), so a LATER live pick would arrive as an
                # ambiguous seed instead of a clean departure.
                self._echo[climate_id] = FanEcho(FAN_AUTO, FAN_AUTO)
            return False

        seeding = echo.last is None
        departed = not seeding and not echo.own(observed)
        if not (seeding or departed):
            # A standing hold: no gesture this cycle, so nothing changes.
            return self._latched.get(climate_id, False)

        # A seed at exactly the speed the ladder would command right now is our
        # own boost speed echoing back, not a manual pick -> adopt it and keep
        # driving. A DEPARTURE never adopts: every slider move is a hold.
        if seeding:
            if self._seed_is_own_idle(observed, act):
                # Baseline stamped so a slow echo of this token isn't a
                # departure; the return-to-auto write then proceeds.
                self._latched[climate_id] = False
                self._echo[climate_id] = FanEcho(observed, observed)
                return False
            idx = self._seed_matches_boost(climate_id, observed, act, delta)
            if idx is not None:
                self._adopt(climate_id, observed, idx)
                return False

        # Seed latched (not at auto, not our rung); a departure always latches.
        self._hold_at(climate_id, observed)
        return True

    def _reconcile_restored_hold(
        self, climate_id: str, observed: str, held: bool, act: str
    ) -> bool:
        """Consume a restored hold truth against the first speed the head reports.

        The Fan-auto switch restores the held/not-held bool and, for a zone that
        was not held, the previous incarnation's command/observation baselines
        (already in the echo memory, see ``restore``). The token itself is
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
        if observed == FAN_AUTO:
            self._latched[climate_id] = False
            if self._echo[climate_id].last is None:
                self._echo[climate_id] = FanEcho(FAN_AUTO, FAN_AUTO)
            return False
        if held:
            self._hold_at(climate_id, observed)
            return True
        echo = self._echo[climate_id]
        remembered = echo.own(observed)
        ours = (
            observed in FAN_LADDER
            and FAN_LADDER.index(observed) <= self._max_idx()
            and (
                echo.last is None
                or remembered
                or (act == MODE_FAN_ONLY and self._host.idle_action == IDLE_ACTION_FAN_ONLY)
            )
        )
        if not ours:
            self._hold_at(climate_id, observed)
            return True
        self._latched[climate_id] = False
        if not remembered:
            # Baseline stamped so a slow echo of this token isn't a departure.
            self._echo[climate_id] = FanEcho(observed, observed)
        if act in (MODE_COOL, MODE_HEAT) and observed in FAN_LADDER:
            self._idx[climate_id] = min(FAN_LADDER.index(observed), self._max_idx())
        return False

    def _hold_at(self, climate_id: str, observed: str) -> None:
        """Latch the head at the token it reports, as a STANDING hold.

        Records the held token as the command baseline so the zone stays held
        next cycle (the same trick async_set_auto OFF uses). Without this
        the identical reading re-reads as a fresh seed/departure every cycle and
        the latch decision is re-litigated forever — which is how the v2.18.0
        "standing merge" removal was defeated for slider holds: the departure
        branch kept re-running the max handback, so a max hold still released
        itself on drift.
        """
        self._latched[climate_id] = True
        self._echo[climate_id] = FanEcho(observed, observed)

    def _adopt(self, climate_id: str, observed: str, idx: int | None = None) -> None:
        """Release the latch and adopt the observed token into the boost's memory.

        Rolls prior, records the observed token as the last command, and pins
        the rung so the ladder continues from where the head actually is: the
        natural ramp proceeds idempotently under DOWN_AT hysteresis, and the head
        doesn't read as a fresh departure and re-latch once the ladder steps off
        that token. ``idx`` is the ladder index to resume from — the seed adopt
        passes the index it matched; the Fan-auto switch passes none and resumes
        from the top, which only ever ramps down from there.
        """
        self._latched[climate_id] = False
        last = self._echo[climate_id].last
        self._echo[climate_id] = FanEcho(observed, observed if last is None else last)
        self._idx[climate_id] = self._max_idx() if idx is None else idx

    def _seed_matches_boost(
        self, climate_id: str, observed: str, act: str, delta: float
    ) -> int | None:
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
        host = self._host
        if act not in (MODE_COOL, MODE_HEAT) or host.eco_idle:
            return None
        if observed not in FAN_LADDER:
            return None
        obs_idx = FAN_LADDER.index(observed)
        if obs_idx > self._max_idx():
            return None  # above the boost ceiling: never ours
        idx = fan_for_delta(
            delta=delta,
            cur_idx=obs_idx,
            up_at=host.fan_up_at,
            down_at=host.fan_down_at,
            max_idx=self._max_idx(),
        )
        return obs_idx if idx == obs_idx else None

    def _seed_is_own_idle(self, observed: str, act: str) -> bool:
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
        host = self._host
        if (
            act != MODE_FAN_ONLY
            or host.idle_action != IDLE_ACTION_FAN_ONLY
            or not host.restored_coordinator_on
            or observed not in FAN_LADDER
            or FAN_LADDER.index(observed) > self._max_idx()
        ):
            return False
        created = host.config_entry.created_at
        return created is not None and dt_util.utcnow() - created >= timedelta(
            seconds=IDLE_SEED_MIN_ENTRY_AGE
        )

    # -- what a restart carries (the Fan auto switch's restore data) ----------
    def restore(
        self,
        climate_id: str,
        held: bool,
        *,
        last: str | None = None,
        prior: str | None = None,
        older: Iterable[str] = (),
        on_pending: bool = False,
    ) -> None:
        """Record the Fan-auto switch's restored pre-restart latch truth.

        Called from the switch's async_added_to_hass (RestoreEntity) — which
        runs during platform setup, BEFORE the coordinator's first compute —
        and consumed once by the seed, which reconciles it against the
        observed head state. Stale restores (older than the entry) are
        filtered by the switch and never reach here. With fan boost disabled
        the data is simply never consumed (the fan machinery is inert).

        ``last``/``prior`` are command/adopted-observation baselines from the
        previous incarnation, when the restored record carried them. For a
        zone that was NOT held they re-arm the echo tolerance across the
        restart, so a head that reports a pre-restart token late — after a
        provisional ``auto``, or once its missing speed arrives — is read as
        our own echo, not as a departure. A held zone's memory is the held
        token itself: the seed records it from the observed head, so nothing
        is carried for it (a hold released during the outage stays released,
        and a later non-auto report on it is a new hold either way).

        ``older`` is the same for a head that was further behind at the stop
        (see ``older_record``). Only speeds boost could write are taken back:
        anything else is no echo.
        """
        self._restore[climate_id] = held
        self._restore_problem.pop(climate_id, None)
        self._on_pending.discard(climate_id)
        # Only a strictly parsed fresh not-held record can carry this explicit
        # instruction. It adopts ONE first usable report, including a user
        # token above the ceiling, just like live ON; it proves no own echo.
        if on_pending is True and held is False:
            self._on_pending.add(climate_id)
        if not held and last is not None:
            # Memory includes adopted user tokens, not just our commands.
            # S11b wins even when an impossible token is reported AFTER a
            # provisional auto. Such a token cannot arm restored echo tolerance.
            def eligible(token: str) -> str:
                if token in FAN_LADDER and FAN_LADDER.index(token) <= self._max_idx():
                    return token
                return FAN_AUTO

            command = eligible(last)
            before = eligible(last if prior is None else prior)
            kept = [token for token in older if eligible(token) == token]
            last_two = dict.fromkeys((before, command))
            self._echo[climate_id] = FanEcho(command, before, (*kept, *last_two) if kept else ())

    def restore_failed(self, climate_id: str, reason: str) -> None:
        """The Fan auto switch found a restore record it could not use, and why."""
        self._restore_problem[climate_id] = reason

    def command_memory(self, climate_id: str) -> tuple[str | None, str | None]:
        """The last two command/adopted-observation baselines: ``(last, prior)``.

        ``None`` where no baseline exists. Restore filters tokens outside the
        current ceiling/ladder to auto; this is echo-tolerance state, NOT an
        audit of sent or delivered commands. Published by the Fan-auto switch
        so the memory rides a restart (see ``restore``).
        """
        echo = self._echo[climate_id]
        return (echo.last, echo.prior)

    def older_commands(self, climate_id: str) -> list[str]:
        """Older echo baselines of a head still behind, oldest first (``FanEcho.older_commands``)."""
        return self._echo[climate_id].older_commands()

    def older_record(self, climate_id: str) -> list[str]:
        """What a start needs back of the older writes, oldest first.

        Unlike ``older_commands`` it's the writes in order, so a last-two
        speed written earlier too keeps that place, and a late report of it isn't
        read as the last write. The trailing last two are left out: the restore
        puts them back. So is a leading auto, which the head is showing or has
        passed, and which MXZ won't write again while it's showing.

        Empty for a head showing the last write with no older speed left to
        read as an echo. That's what a prompt head looks like, and its record
        keeps its old shape. A head in the same state that was really showing an
        earlier write of that speed loses its place (F1's limits,
        docs/formal/PARKED-FINDINGS.md).
        """
        echo = self._echo[climate_id]
        if not echo.older_commands():
            head = self._host.hass.states.get(climate_id)
            if head is not None and head.attributes.get("fan_mode") == echo.last:
                return []
        record = list(echo.older)
        for written in (echo.last, echo.prior):
            if record and record[-1] == written:
                record.pop()
        while record and record[0] == FAN_AUTO:
            del record[0]
        return record

    def reseed_after_standby(self, climate_ids: Iterable[str]) -> None:
        """Release edge: carry each head's pre-hold latch truth into the restore
        slot and clear the per-head command memory, so the next fan observation
        reconciles restored-truth-vs-observed (the same path a restart takes)
        instead of reading the fan the hold left behind as a fresh user
        departure and latching a phantom hold.
        """
        for cid in climate_ids:
            # An UNCONSUMED startup restore outranks the latch: a restart during
            # the hold seeds the restore slot before any fan write can consume it
            # (fan writes are frozen while held), and the latch is empty at that
            # point — overwriting the slot here would drop a pre-restart manual
            # hold on release. Only fill the slot when it is empty.
            if cid not in self._restore:
                self._restore[cid] = self._latched.get(cid, False)
            self._echo.pop(cid, None)
            self._latched.pop(cid, None)
            self._idx.pop(cid, None)
            self.publish(cid)

    # -- the Fan auto switch (the discoverable manual-hold handback) ----------
    def attach(self, climate_id: str, writer: Callable[[], None]) -> Callable[[], None]:
        """The Fan auto switch's state writer for this head; returns its detach."""
        self._writers[climate_id] = writer

        def detach() -> None:
            self._writers.pop(climate_id, None)

        return detach

    def publish(self, climate_id: str) -> None:
        """Keep the existing clean-state channel current before yielding."""
        if writer := self._writers.get(climate_id):
            writer()

    def on_pending(self, climate_id: str) -> bool:
        """An explicit Fan auto on is waiting for the head to report a speed."""
        return climate_id in self._on_pending

    def restore_problem(self, climate_id: str) -> str | None:
        """Why this head's restore record could not be used, if it couldn't."""
        return self._restore_problem.get(climate_id)

    def auto_is_on(self, climate_id: str) -> bool:
        """True when boost drives this head's fan (zone NOT manually held).

        The switch is a live mirror of the latch: ON = auto/boost in charge,
        OFF = a manual speed is being held. The latch machinery is the single
        source of truth while running; across restarts the switch restores the
        held/not-held bool and hands it back via ``restore``.
        """
        if climate_id in self._restore:
            return not self._restore[climate_id]
        return not self._latched.get(climate_id, False)

    async def async_set_auto(self, climate_id: str, on: bool) -> None:
        """Drive the fan-auto switch: ON hands control back, OFF holds the speed.

        ON  -> release the latch and ADOPT the head's current observed token into
               the boost memory (the same trick as the max handback), so the
               still-at-manual-speed head doesn't read as a fresh departure and
               immediately re-latch; then recompute so boost reasserts promptly.
        OFF -> latch at whatever the head is doing right now (a deliberate hold).
               Latching "at auto" is meaningless, so an observed ``auto`` is a
               no-op (the switch stays ON) — documented, pinned in a test. We
               seed the echo memory with the observed token so the observation
               path treats the hold as already-accounted-for, not a new departure.
               The hold sticks at any speed, max included, until the switch (or
               an observed ``auto``) releases it.

        With fan boost disabled the fan machinery is inert: OFF still records the
        latch (it simply has no effect until boost is re-enabled), and ON still
        clears it — the switch stays honest either way.
        """
        # With boost disabled, restored truth stays pending even with a live head.
        # A switch gesture supersedes that pending truth.
        state = self._host.hass.states.get(climate_id)
        observed = state.attributes.get("fan_mode") if state is not None else None
        if on:
            self._restore_problem.pop(climate_id, None)
            self._restore.pop(climate_id, None)
            self._on_pending.discard(climate_id)
            if observed is not None:
                # Adopt the current speed so boost resumes from it without a
                # spurious re-latch (rolls prior, pins the rung to max for a
                # clean DOWN_AT ramp-down).
                self._adopt(climate_id, observed)
            else:
                self._latched[climate_id] = False
                self._on_pending.add(climate_id)
            self.publish(climate_id)
            await self._host.async_request_refresh()
            return
        # OFF: hold at the current speed. Auto is nothing to hold onto.
        if observed is None or observed == FAN_AUTO:
            if observed is None and climate_id in self._on_pending:
                # Cancel the explicit waiting ON without guessing a token or
                # issuing a write. First non-auto report becomes the held
                # speed; observed auto still releases as usual. Known held
                # truth without a pending ON is left intact.
                self._on_pending.discard(climate_id)
                self._restore[climate_id] = True
                self._latched[climate_id] = True
                self.publish(climate_id)
                await self._host.async_request_refresh()
                return
            if observed == FAN_AUTO:
                self._restore.pop(climate_id, None)
            _LOGGER.debug(
                "fan-auto OFF for %s ignored: head at %s (nothing to hold)",
                climate_id,
                observed,
            )
            return
        self._restore.pop(climate_id, None)
        last = self._echo[climate_id].last
        self._echo[climate_id] = FanEcho(observed, observed if last is None else last)
        self._latched[climate_id] = True
        self._on_pending.discard(climate_id)
        self.publish(climate_id)
        await self._host.async_request_refresh()

    def diagnostics(self, climate_id: str) -> dict[str, Any]:
        """The hold and echo memory of one head, as the diagnostics download shows them."""
        echo = self._echo[climate_id]
        return {
            "latched": self._latched.get(climate_id, False),
            "commanded": echo.last,
            "previous": echo.prior,
            "older": echo.older_commands(),
            "ladder_index": self._idx.get(climate_id),
            "turn_on_pending": climate_id in self._on_pending,
            "restored_latch": self._restore.get(climate_id),
            "restore_problem": self._restore_problem.get(climate_id),
        }
