"""Fan boost and the manual fan hold: the speed MXZ writes to each head, and when it writes none.

Fan boost drives a running head's fan speed from how far its room is off target,
and hands a satisfied head back to the firmware's own "auto". A person who picks a
speed at the head keeps it: that head latches, and MXZ makes no fan write to it
until it is handed back, on the room's Fan auto switch or by the head reading
"auto". The room state store carries the latch and the echo memory across a restart
(``FanHold.record``), and the Fan auto switch's restore data does too.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.util import dt as dt_util

from .const import (
    CLOUD_FAN_STEP_SECONDS,
    FAN_AUTO,
    FAN_LADDER,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
)
from .fan_latch import (
    FanEcho,
    FanLatch,
    FanLimits,
    FanRecord,
    fan_auto,
    fan_record,
    fan_step,
    observe_fan,
    restored,
)
from .host import ControllerHost, HeadCommands
from .logic import fan_for_delta

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .cloud import CloudHead, CloudRecords
    from .room_state import RoomStateStore

_LOGGER = logging.getLogger(__package__)


@dataclass(frozen=True)
class FanHoldRestoreData(FanRecord, ExtraStoredData):
    """``fan_latch.FanRecord`` as the Fan auto switch's restore data."""


def _keep_or_drop[V](kept: dict[str, V], climate_id: str, value: V | None) -> None:
    """Set the head's value, or leave none for None."""
    if value is None:
        kept.pop(climate_id, None)
    else:
        kept[climate_id] = value


class FanHost(ControllerHost, Protocol):
    """The coordinator members the fan reads or calls, beyond every controller's.

    Read: the fan boost settings, eco idle, the idle action, whether this
    entry's coordinator switch came back on at a start, the entry (its age),
    and a cloud head's write record through the head drivers (whether a fan
    report can be older than its last batch). Called: the delivery queue, for
    the head service a fan write sends, and the room state store, which saves
    the fan record each time it is published.
    """

    fan_boost_enable: bool
    fan_boost_max: str
    fan_up_at: tuple[float, ...]
    fan_down_at: tuple[float, ...]
    eco_idle: bool
    idle_action: str
    restored_coordinator_on: bool
    config_entry: ConfigEntry
    room_state: RoomStateStore

    @property
    def deliveries(self) -> HeadCommands: ...
    @property
    def heads(self) -> CloudRecords: ...


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

        A manual pick latches (see ``fan_latch.observe_fan``): while a head is
        latched the coordinator makes NO fan writes to it, so a user's chosen
        speed survives every apply cycle. ``fan_latch.fan_step`` decides, after
        the head's report is read here (``head_reported``); the hold is
        published before the write goes out.
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

        self.head_reported(climate_id, observed)
        latch, token = fan_step(
            self._latch(climate_id), observed, act, delta, modes=modes, limits=self._limits()
        )
        self._keep(climate_id, latch)
        self.publish(climate_id)
        if token is not None:
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
        cloud = host.heads.cloud(climate_id)
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
        await self._host.deliveries.async_send(
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
        """Update and return the manual-fan latch for a head from the speed it shows.

        Called once per apply per head, guaranteed the head has fan_modes with an
        "auto" token and reports a current speed (``observed``). The report is
        passed to the echo memory first (``head_reported``), then
        ``fan_latch.observe_fan`` decides.
        """
        self.head_reported(climate_id, observed)
        latch = observe_fan(self._latch(climate_id), observed, act, delta, self._limits())
        self._keep(climate_id, latch)
        return latch.held is True

    def _latch(self, climate_id: str) -> FanLatch:
        """This head's hold as ``fan_latch`` reads it."""
        return FanLatch(
            echo=self._echo[climate_id],
            held=self._latched.get(climate_id),
            restored=self._restore.get(climate_id),
            on_pending=climate_id in self._on_pending,
            rung=self._idx.get(climate_id),
        )

    def _keep(self, climate_id: str, latch: FanLatch) -> None:
        """Keep what ``fan_latch`` left of this head's hold; a None leaves nothing set."""
        self._echo[climate_id] = latch.echo
        _keep_or_drop(self._latched, climate_id, latch.held)
        _keep_or_drop(self._restore, climate_id, latch.restored)
        _keep_or_drop(self._idx, climate_id, latch.rung)
        if latch.on_pending:
            self._on_pending.add(climate_id)
        else:
            self._on_pending.discard(climate_id)

    def _limits(self) -> FanLimits:
        """The settings and the clock reading the fan hold's decision reads."""
        host = self._host
        created = host.config_entry.created_at
        return FanLimits(
            boost=host.fan_boost_enable,
            ceiling=self._max_idx(),
            up_at=host.fan_up_at,
            down_at=host.fan_down_at,
            eco_idle=host.eco_idle,
            idle_action=host.idle_action,
            resumed=host.restored_coordinator_on,
            age=None if created is None else dt_util.utcnow() - created,
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
        """Record the restored pre-restart latch truth, replacing any restored before.

        Called at a start, BEFORE the coordinator's first compute, from the
        room state store or from the Fan auto switch's restore data, whichever
        is newer, and consumed once by the seed, which reconciles it against the
        observed head state. Stale restores (older than the entry) are
        filtered by the switch and never reach here. With fan boost disabled
        the data is simply never consumed (the fan machinery is inert). What it
        keeps of the record is ``fan_latch.restored``'s rule.
        """
        self._restore_problem.pop(climate_id, None)
        self._keep(climate_id, restored(
            self._latch(climate_id), held,
            last=last, prior=prior, older=older, on_pending=on_pending, ceiling=self._max_idx(),
        ))

    def restore_record(self, climate_id: str, record: FanRecord) -> None:
        """``restore`` from a saved record; replaces what an earlier record restored.

        An explicit Fan auto on still waiting counts only on an entry that
        knows when it was created.
        """
        self.restore(
            climate_id,
            held=record.held,
            last=record.last,
            prior=record.prior,
            older=record.older,
            on_pending=record.on_pending and self._host.config_entry.created_at is not None,
        )

    def record(self, climate_id: str) -> FanHoldRestoreData:
        """What a start needs back of this head's hold and echo memory (``fan_latch.fan_record``)."""
        record = fan_record(self._latch(climate_id), self._shown(climate_id))
        return FanHoldRestoreData(
            held=record.held,
            last=record.last,
            prior=record.prior,
            on_pending=record.on_pending,
            older=record.older,
        )

    def _shown(self, climate_id: str) -> str | None:
        """The fan speed the head shows; None for none, or no head."""
        head = self._host.hass.states.get(climate_id)
        return head.attributes.get("fan_mode") if head is not None else None

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
        """What a start needs back of the older writes, oldest first (``fan_latch.fan_record``).

        Unlike ``older_commands`` it's the writes in order, trimmed against the
        fan the head shows.
        """
        return list(fan_record(self._latch(climate_id), self._shown(climate_id)).older)

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
        """Keep both saved channels current before yielding.

        The Fan auto switch writes its state and restore data, and the room
        state store saves the fan record now. The store is written after a
        refresh too, but a Fan auto gesture only asks for a debounced one, which
        can wait out its cooldown: a crash in that wait would lose the gesture.
        """
        self._host.room_state.changed()
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
        return not self._latch(climate_id).holds

    async def async_set_auto(self, climate_id: str, on: bool) -> None:
        """Drive the fan-auto switch: ON hands control back, OFF holds the speed.

        The gesture's rule is ``fan_latch.fan_auto``, against the speed the head
        shows now. ON also clears a restore problem. A gesture that acted is
        published and asks for a recompute, so boost reasserts promptly; one that
        was ignored (OFF at ``auto``, or with no speed and no ON waiting) is only
        logged. With boost disabled, restored truth stays pending even with a
        live head, and a switch gesture supersedes it.
        """
        state = self._host.hass.states.get(climate_id)
        observed = state.attributes.get("fan_mode") if state is not None else None
        if on:
            self._restore_problem.pop(climate_id, None)
        latch, acted = fan_auto(self._latch(climate_id), observed, on, self._max_idx())
        self._keep(climate_id, latch)
        if not acted:
            _LOGGER.debug(
                "fan-auto OFF for %s ignored: head at %s (nothing to hold)",
                climate_id,
                observed,
            )
            return
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
