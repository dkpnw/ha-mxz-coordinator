"""When MXZ computes, for a simulator: MXZ's own timing on a model of Home Assistant's debouncer.

Standard library only, and for a simulator only: no module of the integration imports it
(tools/import_bounds.py ``SIMULATOR_ONLY``). The coordinator keeps Home Assistant's own request
debouncer and ``DataUpdateCoordinator``, and the delivery queue on top of them. This models
them as Home Assistant 2025.11 and later behave, with each delivery done within its compute, so
a simulator with no Home Assistant computes when the product would. MXZ's own rules come from
``timing``: what each input and wakeup asks for, and the heartbeat.

The model, for a refresh asked for (``timing.Refresh``):

- ``REQUEST`` or ``REPORT`` computes at once, unless the request cooldown is running; then it is
  kept, and one compute runs when the cooldown ends. A compute the debouncer ran starts the
  cooldown when it ends.
- ``NOW`` computes at once and ends the cooldown and the request it kept; it starts none.
- While a compute runs: a ``REPORT`` is kept for the end of a cooldown that starts then. A
  ``REQUEST`` made while that plan's deliveries run is followed up by a compute at once after
  it, and so is a ``NOW``; either ends the cooldown and what it kept. A ``REQUEST`` made during
  a plan that sent nothing is asked once it ends. Timers due at the same instant all run before
  the compute the first of them started has ended.
- The heartbeat runs every ``timing.HEARTBEAT`` seconds from MXZ's start and asks ``REQUEST``.
  Each wakeup runs at its due time and asks what ``timing.WAKEUPS`` says.

``tests/test_sim_timing_parity.py`` checks this against the real coordinator on every supported
release (``tests/timing_parity.py``); where a release differs it names the difference.

A simulator calls ``start`` at MXZ's start, then ``step`` with each input (``Ask``, ``Arm``), with
``Tick`` at ``due(timers)``, and with ``Computed`` after each compute a step asked for. A
restart is ``start`` again: nothing of the old timers is kept.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .const import STARTUP_RECOVER_DELAY
from .timing import HEARTBEAT, REQUEST_COOLDOWN, WAKEUPS, Arm, CancelAll, Refresh

__all__ = [
    "Arm", "Ask", "Beat", "CancelAll", "Computed", "Due", "Event", "Fire", "Step", "Tick", "Timers", "Why",
    "due", "start", "step",
]


class Why(StrEnum):
    """Why a compute runs: the debouncer ran a request at once (``ASK``) or at the end of its
    cooldown (``COOLDOWN``), a refresh at once (``NOW``), or the delivery queue's follow-up."""

    ASK = "ask"
    COOLDOWN = "cooldown"
    NOW = "now"
    FOLLOWUP = "followup"


@dataclass(frozen=True, slots=True)
class Ask:
    """An input asked for a refresh."""

    refresh: Refresh


@dataclass(frozen=True, slots=True)
class Computed:
    """The compute a step asked for ran.

    ``sent``: its plan queued a delivery to a head. ``asked``: what its heads' reports asked for
    while it was applied, in order. ``armed``: the wakeups it armed or cancelled, in the order
    the product's steps did them: each ``Arm`` among the apply's ``HeadApply.steps`` and the
    write-back's ``ModeStamp.hold_end`` (``planner.apply_plan``, ``stamp_mode_change``), then
    each one its deliveries' notes did (``DeliverySteps.note``), then ``timing.arms``.
    """

    sent: bool
    asked: tuple[Refresh, ...] = ()
    armed: tuple[Arm | CancelAll, ...] = ()


@dataclass(frozen=True, slots=True)
class Tick:
    """Time has come to ``now``: every timer due by then runs."""


type Event = Ask | Arm | Computed | Tick


@dataclass(frozen=True, slots=True)
class Fire:
    """A wakeup ran."""

    name: str
    key: Hashable


@dataclass(frozen=True, slots=True)
class Beat:
    """The heartbeat ran."""


@dataclass(frozen=True, slots=True)
class Due:
    """A timer: when it runs, and the order it was set in (timers due together run in it)."""

    at: float
    order: int


@dataclass(frozen=True, slots=True)
class Timers:
    """MXZ's timers and the debouncer's, between two steps.

    ``cooldown``: the end of the request cooldown, None when none runs. ``kept``: a request
    waits for it. ``waits``: each armed wakeup by name and key. ``computing``: why the compute a
    step asked for runs, until ``Computed``; ``during``: what was asked meanwhile.
    """

    heartbeat: Due
    cooldown: Due | None = None
    kept: bool = False
    waits: Mapping[tuple[str, Hashable], Due] = field(default_factory=dict)
    computing: Why | None = None
    during: tuple[Refresh, ...] = ()
    order: int = 0


@dataclass(frozen=True, slots=True)
class Step:
    """What one step did.

    ``compute``: run one compute now, then send ``Computed``; None: nothing to run. ``ran``:
    what ran, in order: each wakeup, the heartbeat, and the start of the compute. ``kept``: a
    request waits for the end of the cooldown (the debounce outcome of an ask that computed
    nothing).
    """

    compute: Why | None = None
    ran: tuple[Fire | Beat | Why, ...] = ()
    kept: bool = False


def start(now: float) -> tuple[Timers, Step]:
    """MXZ starts: it computes at once, arms its startup wakeup, and its heartbeat starts."""
    run = _Run(Timers(heartbeat=Due(now + HEARTBEAT, 0), order=1), now)
    run.arm(Arm("startup", None, STARTUP_RECOVER_DELAY))
    run.begin(Why.NOW)
    return run.done()


def due(timers: Timers) -> float:
    """When the next timer runs."""
    return min(timer.at for timer in _timers(timers))


def step(timers: Timers, event: Event, now: float) -> tuple[Timers, Step]:
    """One input, one ``Tick``, or the end of a compute, at ``now``."""
    run = _Run(timers, now)
    match event:
        case Ask(refresh):
            run.ask(refresh)
        case Arm():
            run.arm(event)
        case Computed():
            run.computed(event)
        case Tick():
            run.tick()
    return run.done()


def _timers(timers: Timers) -> list[Due]:
    return [
        timers.heartbeat,
        *([timers.cooldown] if timers.cooldown is not None else []),
        *timers.waits.values(),
    ]


class _Run:
    """One step's working copy of the timers, and what it did."""

    def __init__(self, timers: Timers, now: float) -> None:
        self.timers = timers
        self.now = now
        self.waits = dict(timers.waits)
        self.during = list(timers.during)
        self.ran: list[Fire | Beat | Why] = []
        self.kept = False
        self.order = timers.order

    def done(self) -> tuple[Timers, Step]:
        timers = replace(self.timers, waits=self.waits, during=tuple(self.during), order=self.order)
        computing = timers.computing if timers.computing in self.ran else None
        return timers, Step(compute=computing, ran=tuple(self.ran), kept=self.kept)

    def due(self, delay: float) -> Due:
        self.order += 1
        return Due(self.now + delay, self.order)

    def set(self, **changes: object) -> None:
        self.timers = replace(self.timers, **changes)  # type: ignore[arg-type]

    def arm(self, arm: Arm | CancelAll) -> None:
        if isinstance(arm, CancelAll):
            for name, key in [wait for wait in self.waits if wait[0] == arm.name]:
                del self.waits[name, key]
            return
        self.waits.pop((arm.name, arm.key), None)
        if arm.delay is not None:
            self.waits[arm.name, arm.key] = self.due(arm.delay)

    def begin(self, why: Why) -> None:
        if why in (Why.NOW, Why.FOLLOWUP):
            self.set(cooldown=None, kept=False)
        self.set(computing=why)
        self.ran.append(why)

    def ask(self, refresh: Refresh) -> None:
        if self.timers.computing is not None:
            self.during.append(refresh)
        elif refresh is Refresh.NOW:
            self.begin(Why.NOW)
        elif self.timers.cooldown is not None:
            self.set(kept=True)
            self.kept = True
        else:
            self.begin(Why.ASK)

    def computed(self, event: Computed) -> None:
        why = self.timers.computing
        if why is None:
            raise ValueError("no compute is running")
        during = [*self.during, *event.asked]
        self.during = []
        self.set(computing=None)
        debounced = why in (Why.ASK, Why.COOLDOWN)
        if Refresh.REPORT in during:
            self.set(kept=True)
            if not debounced and self.timers.cooldown is None:
                self.set(cooldown=self.due(REQUEST_COOLDOWN))
        for arm in event.armed:
            self.arm(arm)
        if debounced:
            self.set(cooldown=self.due(REQUEST_COOLDOWN))
        if Refresh.NOW in during:
            self.begin(Why.NOW)
        elif Refresh.REQUEST in during:
            if event.sent:
                self.begin(Why.FOLLOWUP)
            else:
                self.ask(Refresh.REQUEST)

    def tick(self) -> None:
        if self.timers.computing is not None:
            raise ValueError("a compute is running: send Computed first")
        timers = self.timers
        names = {timer: name for name, timer in timers.waits.items()}
        for timer in sorted(
            (timer for timer in _timers(timers) if timer.at <= self.now),
            key=lambda timer: (timer.at, timer.order),
        ):
            if timer is timers.heartbeat:
                self.set(heartbeat=self.due(HEARTBEAT))
                self.ran.append(Beat())
                self.ask(Refresh.REQUEST)
            elif timer is timers.cooldown:
                kept = self.timers.kept
                self.set(cooldown=None, kept=False)
                if kept:
                    self.begin(Why.COOLDOWN)
            else:
                name, key = names[timer]
                del self.waits[name, key]
                self.ran.append(Fire(name, key))
                if (refresh := WAKEUPS.get(name)) is not None:
                    self.ask(refresh)
