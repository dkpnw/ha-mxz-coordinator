"""The delivery queue: each head's commands in turn, and the refreshes that decide them.

One plan is decided at a time. Each head has at most one MXZ command sequence
running, and a newer plan that wants something else for that head waits for it
and then asks for one current-input refresh, never a queue of old plans. A
person's command through MXZ, an Allow Dry pick or a vane kick takes the same
per-head turn. The registry, the per-head locks and what each software call
returned live in ``hass.data``, so a reload cannot forget a call an older
coordinator still has running; what a command did at the head is never claimed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .host import ControllerHost

if TYPE_CHECKING:
    from .fan_hold import FanHold

_LOGGER = logging.getLogger(__package__)


class HeadSteps(Protocol):
    """The per-head drivers (``heads.HeadDrivers``): one delivery's steps at its head."""

    async def async_deliver(
        self, climate_id: str, delivery: Delivery, act: str,
        low: float, high: float, delta: float, engage: str | None,
    ) -> None: ...


class FloorReports(Protocol):
    """The freeze floor (``floor.FloorGuard``): what became of a command to a head."""

    def rejected(self, climate_id: str, err: Exception, delivery: Delivery) -> None: ...
    def accepted(self, climate_id: str, delivery: Delivery) -> None: ...


class DeliveryHost(ControllerHost, Protocol):
    """The coordinator members the queue reads or calls, beyond every controller's.

    Read: the kill switch, the standby hold and the fan hold (each part of what
    a delivery is for), and whether the last update succeeded. Called: the
    per-head drivers that run one delivery's steps, the memory of MXZ's own head
    writes, the floor's record of a refused command, the publication of the
    plan's rows, and Home Assistant's own refresh and listener update.
    """

    coordinator_enable: bool
    inhibited: bool
    fan: FanHold
    last_update_success: bool

    @property
    def heads(self) -> HeadSteps: ...
    @property
    def floor(self) -> FloorReports: ...

    def async_update_listeners(self) -> None: ...
    async def async_refresh(self) -> None: ...
    def publish_delivery(self) -> None: ...
    def remember_head_write(self, climate_id: str, payload: dict[str, Any]) -> None: ...


@dataclass(eq=False)
class Delivery:
    """One head's current MXZ command sequence.

    ``owner`` is the queue that issued it. ``next`` is the queue owed one
    current-input refresh when it ends, if a newer plan wanted something else.
    ``superseded``, ``retired`` and ``manual`` disown what it has not sent yet;
    ``locked`` is set once it holds the head's turn.
    """

    owner: DeliveryQueue
    act: str
    intent: tuple[Any, ...]
    next: DeliveryQueue | None = None
    superseded: bool = False
    retired: bool = False
    manual: bool = False
    locked: bool = False
    task: asyncio.Task[Any] | None = None


def _legacy_busy(debouncer: Debouncer[Any]) -> Callable[[], bool] | None:
    """Whether the request debouncer is running a refresh, on Home Assistant before 2025.11.

    Those releases drop a refresh requested while the debouncer runs one, and
    their ``DataUpdateCoordinator.async_refresh`` does not take the debouncer's
    lock. 2025.11 added the public ``Debouncer.async_lock`` and both fixes at
    once: there this is None and nothing private is read. Before it, only the
    debouncer's lock tells when it drops a request: it stays held until the
    debouncer's own task resumes after the refresh ends. Those releases no
    longer change, so the read cannot break under MXZ. It goes when the minimum
    Home Assistant version reaches 2025.11.
    """
    if hasattr(Debouncer, "async_lock"):
        return None
    lock: asyncio.Lock = debouncer._execute_lock
    return lock.locked


class DeliveryQueue:
    """Serializes the plan's decisions and each head's commands, across reloads.

    ``debouncer`` is the coordinator's request debouncer and ``request`` Home
    Assistant's own debounced request (the coordinator overrides it).
    """

    def __init__(
        self,
        host: DeliveryHost,
        debouncer: Debouncer[Any],
        request: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        self._host = host
        self._request = request
        self._debouncer_busy = _legacy_busy(debouncer)
        # HA before 2025.11 needs MXZ's own follow-up after a refresh a
        # request landed in; later releases schedule it themselves.
        self.legacy = self._debouncer_busy is not None
        # Refreshes serialize decisions, not external completion. An input can
        # release the current wait; each head retains its applicable action until
        # its real handler returns. A deferred head retains only current-input
        # work, never a queue of obsolete plans.
        self._lock = asyncio.Lock()
        self.generation = 0
        self._followup = False
        self._error: Exception | None = None
        self._pending = False
        self._wake = asyncio.Event()
        self._followup_timer: Any | None = None
        hass = host.hass
        self._active: dict[str, Delivery] = hass.data.setdefault(f"{DOMAIN}_deliveries", {})
        self._locks: dict[str, asyncio.Lock] = hass.data.setdefault(f"{DOMAIN}_head_locks", {})
        self._cleanup: dict[str, asyncio.Event] = hass.data.setdefault(
            f"{DOMAIN}_vane_cleanup_pending", {}
        )
        self._observations: dict[str, dict[str, Any]] = hass.data.setdefault(
            f"{DOMAIN}_command_observations", {}
        )

    @property
    def retired(self) -> bool:
        return self._host.retired

    # -- the refresh serialization -------------------------------------------
    async def async_request(self) -> None:
        """Retain current inputs independently of HA's debounce timer."""
        if self.retired:
            return
        if self._active:
            # New intent must progress even while another head is still held.
            self._followup = True
        await self.async_request_ordinary()

    async def async_request_ordinary(self) -> None:
        """Retain ordinary head reports without promoting echoes to new intent."""
        if self.retired:
            return
        if self._lock.locked():
            self._wake.set()
        if self._followup or self._debouncer_busy is not None and (
            self._lock.locked() or self._debouncer_busy()
        ):
            self._pending = True
            self._wake.set()
            if not self._lock.locked():
                self._sync_followup()
            return
        await self._request()

    @callback
    def request_for_report(self) -> None:
        """A head's report asks for a refresh, as an echo, not as new intent."""
        if (
            self._debouncer_busy is not None
            and self._lock.locked()
            and not self._debouncer_busy()
            and not self.retired
        ):
            # A direct refresh leaves HA's debouncer free. Its tracked
            # task waits for the plan, keeping this echo in the same drain.
            self._wake.set()
            self._host.hass.async_create_task(self._request())
        else:
            self._host.hass.async_create_task(self.async_request_ordinary())

    def release(self) -> None:
        """A plan still waiting on its deliveries stops waiting, as for any new input."""
        if self._lock.locked():
            self._wake.set()

    @asynccontextmanager
    async def deciding(self) -> AsyncIterator[None]:
        """One plan's decision and apply, alone; a refresh asked for meanwhile follows it."""
        async with self._lock:
            self.generation += 1
            self._error = None
            if self._followup_timer is not None or self.legacy:
                self._cancel_followup()
            self._pending = False
            self._followup = False
            self._wake.clear()
            try:
                yield
            finally:
                if self._pending or self.legacy:
                    self._sync_followup()

    def follow_up(self) -> None:
        """A delivery this queue was waiting for ended: one current-input refresh now."""
        self._pending = True
        self._followup = True
        self._sync_followup()

    def drop_followup(self) -> None:
        """Retired: no follow-up is owed or armed."""
        self._pending = False
        if self._followup_timer is not None or self.legacy:
            self._cancel_followup()

    def _sync_followup(self) -> None:
        """Arm exactly one current-input refresh requested during this one."""
        if self.retired:
            self._pending = False
            self._cancel_followup()
            return
        if not self._pending:
            return
        self._pending = False
        self._cancel_followup()

        generation = self.generation

        async def _refresh_current() -> None:
            # A direct refresh already waiting on the plan lock consumes this
            # request. Check after it publishes, not when the timer is picked.
            async with self._lock:
                if self.retired or self.generation != generation:
                    return
            if self._followup:
                self._followup = False
                await self._host.async_refresh()
            elif self._active:
                await self.async_request_ordinary()
            else:
                await self._host.async_request_refresh()

        @callback
        def _fire(_now: Any) -> None:
            self._followup_timer = None
            if self.retired:
                return
            self._host.hass.async_create_task(_refresh_current())

        if self.legacy:
            self._followup_timer = async_call_later(self._host.hass, 0, _fire)
        else:
            async def _run() -> None:
                self._followup_timer = None
                await _refresh_current()

            task = self._host.hass.async_create_task(_run(), eager_start=False)
            self._followup_timer = task.cancel

    def _cancel_followup(self) -> None:
        """Cancel the one queued current-input follow-up, if any."""
        if self._followup_timer is not None:
            self._followup_timer()
            self._followup_timer = None

    # -- each head's deliveries ----------------------------------------------
    def current(self, climate_id: str) -> Delivery | None:
        """The head's running delivery, whichever coordinator issued it."""
        return self._active.get(climate_id)

    def busy(self, climate_id: str) -> bool:
        return climate_id in self._active

    def queue(
        self, climate_id: str, act: str, low: float, high: float,
        delta: float = 0.0, engage: str | None = None,
    ) -> asyncio.Task[Any] | None:
        """One real operation per head, with at most one current-input follow-up.

        The registry lives with HA, so reload cannot forget an older handler.
        A waiting new incarnation inherits no old intent or fan provenance.
        """
        host = self._host
        intent = (act, low, high, delta, engage, host.inhibited, host.fan.auto_is_on(climate_id))
        if active := self._active.get(climate_id):
            changed = (
                active.owner is not self or active.retired or active.manual
                or active.intent != intent
            )
            active.next = self if changed else None
            active.superseded = changed
            if changed:
                host.publish_delivery()
            return None
        active = Delivery(owner=self, act=act, intent=intent)
        self._active[climate_id] = active
        task = host.hass.async_create_task(
            self._deliver(climate_id, active, act, low, high, delta, engage),
            eager_start=False,
        )
        active.task = task
        task.add_done_callback(partial(self._finish, climate_id, active))
        return task

    async def async_wait(self, tasks: list[asyncio.Task[Any]]) -> None:
        """Wait for issued work; deferred work or new inputs release the plan."""
        if not tasks:
            return
        wake = asyncio.create_task(self._wake.wait())
        pending = set(tasks)
        try:
            while pending and not wake.done():
                done, _ = await asyncio.wait(pending | {wake}, return_when=asyncio.FIRST_COMPLETED)
                pending -= done
        finally:
            wake.cancel()
            await asyncio.gather(wake, return_exceptions=True)
        if self._error is not None:
            raise self._error

    def owned(self, active: Delivery) -> bool:
        """Whether this delivery may still send its next step."""
        return (
            not self.retired and self._host.coordinator_enable
            and not active.superseded and not active.retired and not active.manual
        )

    async def _deliver(
        self, climate_id: str, active: Delivery, act: str,
        low: float, high: float, delta: float, engage: str | None,
    ) -> None:
        """Preserve the existing per-head sequence; never follow obsolete work."""
        host = self._host
        try:
            # Own the complete per-head sequence, including fan observation.
            # A queued manual command runs before the next sequence inspects its
            # fan token; no pre-lock guess can overwrite that manual choice.
            lock = self._locks.setdefault(climate_id, asyncio.Lock())
            if lock.locked() or climate_id in self._cleanup:
                # This sequence has issued nothing. Keep it queued behind the
                # real handler/cleanup, but do not hold its creating service or
                # entry setup open until an unrelated input wakes the plan.
                self._wake.set()
                host.publish_delivery()
            async with lock:
                await self._wait_cleanup(climate_id)
                active.locked = True
                if self.owned(active):
                    await host.heads.async_deliver(climate_id, active, act, low, high, delta, engage)
        except HomeAssistantError as err:
            _LOGGER.error(
                "MXZ: applying %s to %s failed (zone degraded, others continue): %s",
                act, climate_id, err,
            )
            host.floor.rejected(climate_id, err, active)
        except Exception as err:
            # A head's own refusal arrives above as HomeAssistantError
            # (_async_issue); this is a fault in MXZ's own steps, and
            # fails the plan, even if this head outlives it.
            _LOGGER.exception("MXZ: unexpected command failure for %s", climate_id)
            self._error = err
            host.last_update_success = False
            host.publish_delivery()
            # A global failure still updates every coordinator entity, even
            # when the failing delivery has outlived its creating refresh.
            host.async_update_listeners()

    @callback
    def _finish(self, climate_id: str, active: Delivery, _task: asyncio.Task[Any]) -> None:
        """Release ownership after the call's queued state callbacks run."""
        if self._active.get(climate_id) is active:
            self._active.pop(climate_id)
            self._host.publish_delivery()
        successor = active.next
        if successor is not None and not successor.retired:
            successor.follow_up()

    def retire(self) -> None:
        """Disown follow-ons without pretending to interrupt external work."""
        for climate_id, active in self._active.items():
            if active.owner is self:
                active.retired = True
                active.superseded = True
                observed = self._observations.get(climate_id, {})
                if observed.get("_owner") is self:
                    observed.update(
                        command_retired_at=dt_util.utcnow().isoformat(), command_ownership_retired=True,
                    )
            if active.next is self:
                active.next = None
        for observed in self._observations.values():
            if observed.get("_owner") is self and observed.get("command_status") == "pending":
                observed["command_retired_at"] = dt_util.utcnow().isoformat()
                observed["command_ownership_retired"] = True
        self._wake.set()
        self._host.publish_delivery()

    def disown(self, climate_id: str) -> None:
        """A person's change at the head is newer than this queue's command still to be sent."""
        active = self._active.get(climate_id)
        if active is not None and active.owner is self:
            active.superseded = True
            self._host.publish_delivery()

    def disown_task(self, climate_id: str, task: asyncio.Task[Any] | None) -> None:
        """A retired task's call still pending at this head is no longer anyone's."""
        observed = self._observations.get(climate_id, {})
        if observed.get("_task") is task and observed.get("command_status") == "pending":
            observed.update(
                command_retired_at=dt_util.utcnow().isoformat(), command_ownership_retired=True,
            )

    # -- one service call to a head ------------------------------------------
    async def async_send(
        self, climate_id: str, service: str, payload: dict[str, Any],
        *, manual: bool = False, guard: Callable[[], bool] | None = None,
    ) -> None:
        """Serialize ordinary climate handlers even across entry incarnations.

        ``manual``: a person's command, which disowns the running delivery's
        remaining steps. ``guard`` runs once this call holds the head's turn,
        and the call is not sent if it returns False.
        """
        active = self._active.get(climate_id)
        if manual and active:
            # A facade gesture supersedes the old sequence before it yields,
            # while the actual manual handler still waits for the old return.
            active.manual = True
        if active and active.task is asyncio.current_task() and active.locked:
            await self._wait_cleanup(climate_id)
            if self.owned(active):
                await self._async_issue(climate_id, service, payload)
            return
        async with self._locks.setdefault(climate_id, asyncio.Lock()):
            await self._wait_cleanup(climate_id)
            if self.retired:
                return
            if guard is not None and not guard():
                return
            await self._async_issue(climate_id, service, payload)

    async def async_send_cleanup(
        self, climate_id: str, service: str, payload: dict[str, Any],
    ) -> None:
        """A retiring coordinator's cleanup call: ordinary calls to the head wait for it.

        It runs outside the head's turn, because the call it cleans up after may
        still hold it, and is recorded apart, so it never overwrites that call's
        status.
        """
        pending = asyncio.Event()
        self._cleanup[climate_id] = pending
        try:
            observed = self._observations.setdefault(climate_id, {})
            cleanup = observed.setdefault("vane_retirement_cleanup", {})
            await self._async_issue(climate_id, service, payload, observation=cleanup)
        finally:
            self._cleanup.pop(climate_id, None)
            pending.set()

    async def _wait_cleanup(self, climate_id: str) -> None:
        """A guarded cleanup may outlive its wake; ordinary work waits for both."""
        while pending := self._cleanup.get(climate_id):
            await pending.wait()

    async def _async_issue(
        self, climate_id: str, service: str, payload: dict[str, Any],
        *, observation: dict[str, Any] | None = None,
    ) -> None:
        """Observe one software call, never claim physical receipt."""
        # Before the call: the head's report can arrive while it runs.
        self._host.remember_head_write(climate_id, payload)
        observed = observation if observation is not None else self._observations.setdefault(climate_id, {})
        if observation is None:
            # Ordinary issuance has waited for both the old handler and its
            # guarded cleanup. That cleanup is no longer a current obstacle.
            observed.pop("vane_retirement_cleanup", None)
            observed.update(_owner=self, _task=asyncio.current_task(), command_ownership_retired=False)
        observed.update(command_status="pending", command_attempted_at=dt_util.utcnow().isoformat())
        self._host.publish_delivery()
        try:
            await self._host.hass.services.async_call(
                "climate", service, {"entity_id": climate_id, **payload}, blocking=True,
            )
        except asyncio.CancelledError:
            observed["command_status"] = "cancelled; outcome unknown"
            raise
        except Exception as err:
            observed.update(
                command_status="rejected", command_failed_at=dt_util.utcnow().isoformat(),
                command_error=str(err),
            )
            if isinstance(err, HomeAssistantError):
                raise
            # Whatever a head's handler raises is that head's refusal, not a
            # fault of MXZ's: its room is degraded and the plan goes on.
            raise HomeAssistantError(f"{climate_id} raised {err!r}") from err
        else:
            observed.update(command_status="returned", command_returned_at=dt_util.utcnow().isoformat())
        finally:
            self._host.publish_delivery()

    def attributes(self, climate_id: str) -> dict[str, Any]:
        """Software observations, with honest absence rather than invented dates."""
        observed = self._observations.get(climate_id, {})
        active = self._active.get(climate_id)
        if active and active.owner is not self:
            observed = active.owner._observations.get(climate_id, {})
        return {
            **{key: observed.get(key, "not yet recorded") for key in (
                "command_attempted_at", "command_returned_at", "command_failed_at", "command_retired_at",
            )},
            "command_status": observed.get("command_status", "not yet recorded"),
            "command_error": observed.get("command_error"),
            "command_ownership_retired": observed.get("command_ownership_retired", False),
            "vane_retirement_cleanup": dict(observed.get("vane_retirement_cleanup", {})),
            "command_deferred": bool(active and (active.next is self or not active.locked)),
            "command_timestamp_basis": "Home Assistant software observations; not physical receipt",
            "plan_target_basis": "computed intent; command return does not prove physical application",
        }
