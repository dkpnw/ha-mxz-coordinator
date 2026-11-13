"""Temperature units: how a saved value follows a °F/°C switch, and the rounding it uses.

No Home Assistant dependency, so the math is tested directly.
"""

from __future__ import annotations

import math

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

    Half up, as the CN105 path reads a head's grid (``HeadGrid.already_at``):
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
