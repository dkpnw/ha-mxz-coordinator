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

A generic head (beta: any other climate entity with heat and cool) takes the
same path and the same record, with ``generic`` set: only unavailability puts
it out of control, never silence.

No Home Assistant dependency, so the math is tested directly.
"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable
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
    older_setpoints: set[float] = field(default_factory=set)
    # The last fan and setpoint sent since the last confirmation. Not older
    # states until a later batch sends another: a batch with no fan or no
    # setpoint leaves that one current.
    last_fan: str | None = None
    last_setpoint: float | None = None
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
    # A generic head (beta): silence never makes it unresponsive.
    generic: bool = False

    def pending(self, clock: float) -> bool:
        """Whether the head is inside a confirm window now."""
        return clock < self.window_until

    def stale(
        self,
        clock: float,
        *,
        mode: str | None = None,
        fan: str | None = None,
        setpoint: float | None = None,
        same: Callable[[float, float], bool] = operator.eq,
    ) -> bool:
        """Whether a report inside the window can be an older state.

        Inside the startup window any report can be. Inside a batch's window,
        a mode, fan or setpoint the head showed before that batch (or one MXZ
        sent earlier and has not seen confirmed) is stale. A value the head
        never had is a real change, window or not, and so is the batch's own
        setpoint. ``same(reported, older)`` compares setpoints, on the head's
        grid where the caller knows it.
        """
        if not self.pending(clock):
            return False
        if self.startup:
            return True
        return (
            (mode is not None and mode in self.older_modes)
            or (fan is not None and fan in self.older_fans)
            or (
                setpoint is not None
                and any(same(setpoint, older) for older in self.older_setpoints)
                and not (
                    self.intent is not None and self.intent[1] is not None
                    and same(setpoint, self.intent[1])
                )
            )
        )

    def sent(
        self,
        clock: float,
        intent: tuple[str, float | None, str | None],
        shown_mode: str | None,
        shown_fan: str | None,
        shown_setpoint: float | None = None,
    ) -> None:
        """Record one batch: a new window, and the states it supersedes.

        A batch with no fan supersedes no fan: the fan the head shows then,
        and the last fan MXZ sent even if unconfirmed, is the one it should
        keep showing, so a report of it is current. The same for a batch
        with no setpoint.
        """
        if self.confirmed:
            self.older_modes.clear()
            self.older_fans.clear()
            self.older_setpoints.clear()
            self.last_fan = None
            self.last_setpoint = None
        elif self.intent is not None:
            self.older_modes.add(self.intent[0])
        if shown_mode is not None:
            self.older_modes.add(shown_mode)
        self.older_modes.discard(intent[0])
        if intent[2] is not None:
            self.older_fans.update(f for f in (self.last_fan, shown_fan) if f is not None)
            self.older_fans.discard(intent[2])
            self.last_fan = intent[2]
        if intent[1] is not None:
            self.older_setpoints.update(
                v for v in (self.last_setpoint, shown_setpoint) if v is not None
            )
            self.older_setpoints.discard(intent[1])
            self.last_setpoint = intent[1]
        self.intent = intent
        self.last_batch = clock
        self.window_until = clock + self.confirm_wait
        self.startup = False
        self.release_at = None
        self.confirmed = False

    def refused(self, clock: float) -> None:
        """The last batch raised, so it is not one to wait on.

        The next batch goes out as soon as the write interval allows, which
        still counts from the refused one: that is the rate the user set.
        ``release_at`` is that moment (``clock`` if it has already come), so
        the plan can wake for it with nothing else happening. The window
        stays open, so a late report of an older state is still stale.
        """
        self.intent = None
        self.release_at = clock + self.wait_for_slot(clock)

    def wait_for_slot(self, clock: float) -> float:
        """Seconds until the write interval lets the next batch out (0 = now)."""
        if self.last_batch is None:
            return 0.0
        return max(0.0, self.last_batch + self.write_interval - clock)

    def status(self, clock: float) -> str:
        """The plan's word for this head now."""
        if self.unresponsive:
            return CLOUD_NOT_CONTROLLABLE
        if self.pending(clock) and (self.startup or self.intent is not None):
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


# Home Assistant's UnitOfTemperature values, as a set drive offset's unit is
# stored (ZONE_DRIVE_OFFSET_UNIT).
UNIT_C = "°C"
UNIT_F = "°F"


def offset_in_unit(value: float, set_in: object, celsius: bool) -> float:
    """A drive offset set in ``set_in`` (°C or °F), in the system unit.

    An offset is a difference, so it scales by 5/9 or 9/5 with no 32° shift:
    3 °F is 1.67 °C. To two places, as the room page shows it. Any other
    ``set_in`` (none stored: an offset saved before its unit was) is taken to
    be in the system unit already, and so is returned unchanged.
    """
    if set_in == UNIT_F and celsius:
        return round(value * 5 / 9, 2)
    if set_in == UNIT_C and not celsius:
        return round(value * 9 / 5, 2)
    return value


def target_in_unit(value: float, set_in: object, unit: str, step: float) -> float:
    """An absolute temperature set in ``set_in`` (°C or °F), in ``unit``, on ``step``.

    59 °F is 15 °C. Anything else ``set_in`` can be (the same unit, none stored)
    leaves the value as it is, so a value converted once is never converted again.
    """
    if set_in == UNIT_F and unit == UNIT_C:
        return snap_to_step((value - 32) * 5 / 9, step)
    if set_in == UNIT_C and unit == UNIT_F:
        return snap_to_step(value * 9 / 5 + 32, step)
    return value


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
