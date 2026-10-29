"""Cloud heads (beta): the per-head write record and setpoint math.

A cloud head reaches Home Assistant through an integration that polls a
vendor cloud (MELCloud, Kumo Cloud): its reports trail a command by up to
minutes, a burst of writes can be dropped, and it can't take MXZ's room
sensor as its own. So MXZ writes it in batches (mode, one setpoint, fan), at
most one per write interval, and after each batch holds it "pending" for a
confirm wait: MXZ re-sends nothing it already sent, and a report of an older
state inside that wait is stale, not a person's change. While a room runs, the
setpoint is pushed past the target by a drive offset so the head's own
thermistor doesn't stop it early; the room sensor still decides when the room
is done. The offset is @mikepell007's (mxz_coordinator_blueprint, MIT).

No Home Assistant dependency, so the math is tested directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .const import MODE_COOL, MODE_HEAT

# What a cloud row's plan status reads.
CLOUD_PENDING = "pending"
CLOUD_CONFIRMED = "confirmed"
CLOUD_UNCONFIRMED = "unconfirmed"
CLOUD_NOT_CONTROLLABLE = "not_controllable"


@dataclass
class CloudHead:
    """What MXZ has sent one cloud head, and what it is waiting to see.

    On the monotonic clock, like every other MXZ timer. In memory only: a new
    coordinator (a restart or a reload) opens one confirm wait of its own
    (``startup``), in which every report may be a stale poll from before it.
    """

    write_interval: float
    confirm_wait: float
    # The end of the current pending window, and whether it is the startup one.
    window_until: float
    startup: bool = True
    # The last batch's (mode, setpoint or None, fan token or None).
    intent: tuple[str, float | None, str | None] | None = None
    # What the head showed when that batch went out, and every earlier intent
    # since the last confirmation: a report of one of these inside the window
    # is stale.
    older_modes: set[str] = field(default_factory=set)
    older_fans: set[str] = field(default_factory=set)
    last_batch: float | None = None
    # The last fan boost step written, for the one-rung-per-step limit.
    fan_at: float | None = None
    # When the next batch may go out, while one waits on the write interval or
    # a fan step waits on its limit; None otherwise.
    release_at: float | None = None
    # Whether the head has been seen holding everything the last batch sent
    # once its window closed, and when that was (ISO, display only).
    confirmed: bool = False
    confirmed_at: str | None = None
    # Not controllable: unavailable or silent past the limit.
    unresponsive: bool = False

    def pending(self, clock: float) -> bool:
        """Whether the head is inside a confirm window now."""
        return clock < self.window_until

    def stale(self, clock: float, *, mode: str | None = None, fan: str | None = None) -> bool:
        """Whether a report inside the window can be an older state.

        Inside the startup window any report can be. Inside a batch's window,
        a mode or fan the head showed before that batch (or one MXZ sent
        earlier and has not seen confirmed) is stale. A value the head never
        had is a real change, window or not.
        """
        if not self.pending(clock):
            return False
        if self.startup:
            return True
        return (mode is not None and mode in self.older_modes) or (
            fan is not None and fan in self.older_fans
        )

    def sent(
        self,
        clock: float,
        intent: tuple[str, float | None, str | None],
        shown_mode: str | None,
        shown_fan: str | None,
    ) -> None:
        """Record one batch: a new window, and the states it supersedes."""
        if self.intent is not None and not self.confirmed:
            self.older_modes.add(self.intent[0])
            if self.intent[2] is not None:
                self.older_fans.add(self.intent[2])
        elif self.confirmed:
            self.older_modes.clear()
            self.older_fans.clear()
        if shown_mode is not None:
            self.older_modes.add(shown_mode)
        if shown_fan is not None:
            self.older_fans.add(shown_fan)
        self.older_modes.discard(intent[0])
        if intent[2] is not None:
            self.older_fans.discard(intent[2])
        self.intent = intent
        self.last_batch = clock
        self.window_until = clock + self.confirm_wait
        self.startup = False
        self.release_at = None
        self.confirmed = False

    def wait_for_slot(self, clock: float) -> float:
        """Seconds until the write interval lets the next batch out (0 = now)."""
        if self.last_batch is None:
            return 0.0
        return max(0.0, self.last_batch + self.write_interval - clock)

    def status(self, clock: float) -> str:
        """The plan's word for this head now."""
        if self.unresponsive:
            return CLOUD_NOT_CONTROLLABLE
        if self.pending(clock):
            return CLOUD_PENDING
        return CLOUD_CONFIRMED if self.confirmed else CLOUD_UNCONFIRMED


def drive_setpoint(*, act: str, target: float, offset: float) -> float:
    """The setpoint a running cloud head gets: past the target by ``offset``.

    Heat drives above the target and cool below it; ``offset`` 0 is the bare
    target. Any other act has no setpoint to drive and returns ``target``.
    """
    if act == MODE_HEAT:
        return target + offset
    if act == MODE_COOL:
        return target - offset
    return target


def snap_to_step(value: float, step: float) -> float:
    """``value`` on the nearest multiple of ``step``, half up.

    Half up, as the CN105 path reads a head's grid (``_head_already_at``):
    Python's round() is half to even. No usable step leaves it unchanged.
    """
    if not step > 0 or not math.isfinite(step):
        return value
    return math.floor(value / step + 0.5) * step


def clamp(value: float, band: tuple[float, float] | None) -> float:
    """``value`` inside ``band`` (low, high); unchanged with no band."""
    if band is None:
        return value
    low, high = band
    return min(max(value, low), high)
