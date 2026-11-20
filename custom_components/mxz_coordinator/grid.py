"""What a head accepts, and what a local head is sent for a command. No Home Assistant.

``heads.HeadGrid`` reads a head's live entity into a :class:`Grid`: its native band and unit,
its setpoint step, and the system's unit, target step and clamp band. The grid holds the
rules every caller reads it by: the band the head will accept in the system unit
(:meth:`Grid.safe_band`), a command clamped into it, the bounds a room's target may take, and
whether a setpoint is already where a command would put it on the head's own grid.
:func:`local_calls` is the service calls a local head gets for one command; ``LocalDriver``
sends them in order. :func:`convert` is Home Assistant's ``TemperatureConverter`` for °C and
°F (``tests/test_grid.py`` compares the two on every supported release).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from .const import MODE_COOL, MODE_HEAT
from .logic import as_float
from .units import UNIT_C, UNIT_F, snap_to_step

# ClimateEntityFeature.TARGET_TEMPERATURE_RANGE.
_RANGE = 2


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """A temperature in ``from_unit`` in ``to_unit``, as Home Assistant converts it.

    The same unit returns the value as it is. °C and °F only: anything else raises
    ``ValueError``.
    """
    if from_unit == to_unit:
        return value
    if from_unit == UNIT_C and to_unit == UNIT_F:
        return value * 1.8 + 32.0
    if from_unit == UNIT_F and to_unit == UNIT_C:
        return (value - 32.0) / 1.8
    raise ValueError(f"no conversion from {from_unit!r} to {to_unit!r}")


@dataclass(frozen=True, slots=True)
class Grid:
    """What one head accepts, read off its live entity (#10).

    ``native``: the head's unrounded native (min, max, unit), None if unknown. ``shown_band``:
    the min and max its state shows, in the system unit (None for no state, an edge of None
    for one it lacks), read only when ``native`` is unknown. ``native_step`` and
    ``native_unit``: its setpoint step and unit (0.0 and None when it has none readable).
    ``system_unit``, ``target_step``, ``clamp_min`` and ``clamp_max``: MXZ's.
    """

    system_unit: str
    target_step: float
    clamp_min: float
    clamp_max: float
    native: tuple[float, float, str] | None = None
    shown_band: tuple[float | None, float | None] | None = None
    native_step: float = 0.0
    native_unit: str | None = None

    def safe_band(self) -> tuple[float, float] | None:
        """The head's operating band as system-unit edges it will ACCEPT.

        Convert the head's native limits into the system unit and snap the ceiling DOWN / the
        floor UP to our step, so every edge round-trips back through HA's unit conversion
        still inside the native band (78.8 °F -> 78 °F, which converts to 25.56 °C <= the
        26.0 °C native max). For a head already native to the system unit this is a no-op on
        whole values. Falls back to the display-rounded band the state shows when the native
        one isn't known (keeps the common same-unit case correct).
        """
        temp_unit = self.system_unit
        native = self.native
        lo: float | None
        hi: float | None
        if native is not None:
            nmin, nmax, nunit = native
            lo = convert(nmin, nunit, temp_unit)
            hi = convert(nmax, nunit, temp_unit)
        else:
            if self.shown_band is None:
                return None
            lo, hi = self.shown_band
            if lo is None or hi is None:
                return None
        step = self.target_step or 1.0
        # +/-1e-9 absorbs float noise so an exact 26.0/0.5 doesn't fall a step.
        safe_low = math.ceil(lo / step - 1e-9) * step
        safe_high = math.floor(hi / step + 1e-9) * step
        if native is not None and nunit != temp_unit:
            # Verify each snapped edge round-trips through the SAME conversion HA's validator
            # performs: the noise epsilon can promote an edge a float-ulp past the true native
            # limit (a 78.8 °F native max in a °C system snaps to 26.0 °C, which converts back
            # to 78.800…01). One extra step inward always clears a 1-ulp overshoot.
            if convert(safe_high, temp_unit, nunit) > nmax:
                safe_high -= step
            if convert(safe_low, temp_unit, nunit) < nmin:
                safe_low += step
        if safe_low > safe_high:
            return None  # degenerate band (narrower than a step) -> don't clamp
        return (safe_low, safe_high)

    def clamp(self, low: float, high: float) -> tuple[float, float]:
        """Clamp (low, high) into the head's accept-able band; pass through if unknown."""
        band = self.safe_band()
        if band is None:
            return (low, high)
        safe_low, safe_high = band
        return (
            min(max(low, safe_low), safe_high),
            min(max(high, safe_low), safe_high),
        )

    def target_bounds(self) -> tuple[float, float]:
        """UI setpoint bounds for a zone: [clamp_min, clamp_max] narrowed to the head's own
        accept-able band, so the number/thermostat facade never offers a target the head
        would reject (#10). If the head band is unknown or disjoint from the clamp band, fall
        back to the plain clamp band.
        """
        lo, hi = float(self.clamp_min), float(self.clamp_max)
        band = self.safe_band()
        if band is None:
            return (lo, hi)
        safe_low, safe_high = band
        lo, hi = max(lo, safe_low), min(hi, safe_high)
        if lo > hi:
            return (float(self.clamp_min), float(self.clamp_max))
        return (lo, hi)

    def already_at(self, reported: float | None, desired: float) -> bool:
        """True if sending ``desired`` would leave the head's setpoint unchanged.

        A head can only hold setpoints on its own native grid. A °C-native 0.5 °C head
        (ESPHome CN105) in a °F system latches 18.0 °C for a commanded 64 °F (17.78 °C) and
        reports it back as 64.4 °F, displayed 64.5 °F. That is the closest it can get to
        64 °F, so comparing whole °F never matches and every cycle re-sends a value that
        changes nothing.

        So compare on the head's grid: convert ``desired`` to the native unit and snap it to
        the native step (the value the head will latch); the head already holds it when
        ``reported`` converts back to within half a native step. The reported value's display
        rounding (at most 0.14 °C at 0.5 °F precision) and the raw commanded value the head
        publishes just before its echo (at most 0.22 °C from the grid for a whole °F) both
        stay inside that half step; a head one native step off does not. With no known native
        unit/step, fall back to half our own step.

        Known limits: this assumes the head rounds to the nearest step, as the CN105 does; a
        head that truncated to its grid would still re-send. It also assumes the entity
        displays at 0.5 °F or finer; at whole-°F display precision a one-step move on a
        0.5 °C head can read as already there (the old whole-°F check had the same blind
        spot).
        """
        if reported is None:
            return False
        step, unit = self.native_step, self.native_unit
        if step <= 0 or unit not in (UNIT_C, UNIT_F):
            return abs(reported - desired) < self.target_step / 2
        # Half up, as the CN105 firmware rounds (round() is half to even).
        latched = math.floor(convert(desired, self.system_unit, unit) / step + 0.5) * step
        held = convert(reported, self.system_unit, unit)
        return abs(held - latched) < step / 2

    def target_for_setpoint(self, shown: float) -> float:
        """The target on our step that leaves this head where a person put it.

        18.5 °C on a CN105 reads 65.5 °F here, whose nearest whole °F is 66, and 66 °F would
        move the head on to 19 °C. So the nearest target the head would hold ``shown`` for, a
        step either side if need be; with none, the nearest.
        """
        step = self.target_step or 1.0
        nearest = snap_to_step(shown, step)
        return next(
            (
                target for target in (nearest, nearest - step, nearest + step)
                if self.already_at(shown, target)
            ),
            nearest,
        )


class Call(NamedTuple):
    """One climate service call to a head: the service and its data (no entity id)."""

    service: str
    data: dict[str, Any]


def _mode_and_setpoints(
    act: str, mode: str | None, setpoints: dict[str, float], *, combined: bool
) -> tuple[Call, ...]:
    """Cool/heat and its setpoint(s) the way this head will honor them.

    An ESPHome head (the CN105) applies the hvac_mode HA passes along inside set_temperature,
    so it gets one combined call. Other integrations may drop it: Kumo Cloud heads through
    comfort_HA parked in fan_only never woke. So a mode change on those goes first as its own
    set_hvac_mode, then set_temperature carries only the setpoint(s). A failed mode call
    raises before the setpoint write and the next compute retries. ``combined`` is a head
    that takes the one call (ESPHome, or one with no registry entry, which can't be
    identified); a head already in the mode always does.
    """
    if mode == act or combined:
        return (Call("set_temperature", {"hvac_mode": act, **setpoints}),)
    return (Call("set_hvac_mode", {"hvac_mode": act}), Call("set_temperature", setpoints))


def local_calls(
    act: str,
    low: float,
    high: float,
    *,
    mode: str | None,
    attributes: Mapping[str, Any] | None,
    grid: Grid,
    combined: bool,
) -> tuple[Call, ...]:
    """The calls a local head is sent for ``act`` with setpoints (``low``, ``high``), in order.

    ``mode`` and ``attributes`` are what the head shows (None for no state). None at all when
    the head already shows the command on its own grid.

    Heat and cool are clamped, edge by edge, to what THIS head will actually accept: the
    global clamp band can exceed an individual head's, and HA validates set_temperature by
    converting our (system-unit) value into the head's NATIVE unit and range-checking there,
    so a °C-native head whose native max is 26.0 °C rejects 79 °F (26.11 °C) even though it
    REPORTS max_temp = 79 °F (78.8 rounded up). Clamping in the head's native band (rounded
    toward the safe interior) keeps a head-exceeding target from erroring every cycle /
    degrading the zone (#10). A head without a setpoint range (single-setpoint, common on MXZ
    indoor units) rejects target_temp_low/high, so it gets the one setpoint it accepts (#6):
    the clamped room target, the high edge in cool and the low edge in heat. fan_only and off
    are a set_hvac_mode alone (a bare temperature throws).
    """
    if act in (MODE_COOL, MODE_HEAT):
        low, high = grid.clamp(low, high)
        features = int(attributes.get("supported_features") or 0) if attributes is not None else 0
        if not features & _RANGE:
            setpoint = high if act == MODE_COOL else low
            cur = as_float(attributes.get("temperature")) if attributes is not None else None
            if mode == act and grid.already_at(cur, setpoint):
                return ()
            return _mode_and_setpoints(act, mode, {"temperature": setpoint}, combined=combined)
        cur_low = as_float(attributes.get("target_temp_low")) if attributes is not None else None
        cur_high = as_float(attributes.get("target_temp_high")) if attributes is not None else None
        if mode == act and grid.already_at(cur_low, low) and grid.already_at(cur_high, high):
            return ()
        return _mode_and_setpoints(
            act, mode, {"target_temp_low": low, "target_temp_high": high}, combined=combined,
        )
    if mode == act:
        return ()
    return (Call("set_hvac_mode", {"hvac_mode": act}),)
