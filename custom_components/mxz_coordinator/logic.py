"""Pure decision math for MXZ Coordinator — no Home Assistant dependency.

Mirrors the Jinja in the YAML package's ``sensor.mxz_plan`` and the setpoint/action
math in ``script.mxz_coordinate``. Kept HA-free so the validated truth table can be
unit-tested directly.
"""

from __future__ import annotations

from .const import (
    ECO_COOL_HIGH,
    ECO_COOL_LOW,
    ECO_HEAT_HIGH,
    ECO_HEAT_LOW,
    ENGAGE_SATISFIED,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
)


def room_call(
    *,
    temp: float,
    target: float,
    enabled: bool,
    eco: bool,
    sensor_ok: bool,
    band: float,
    eco_cool_max: float,
    eco_heat_min: float,
    neutral: str,
    heat_lockout: bool = False,
    heat_lockout_floor: float = 0.0,
) -> str:
    """Per-room call for one band (demand uses S/'neutral', engage uses D/'satisfied').

    Mirrors the primary/secondary_demand and primary/secondary_engage attributes.

    ``heat_lockout`` holds off the heat call (the room idles instead of heating — e.g.
    to let passive solar warm it in summer) UNLESS the room has dropped below
    ``heat_lockout_floor``, a safety floor so a genuinely cold room still gets heat.
    Cooling and the eco extremes are unaffected.
    """
    if not enabled:
        return MODE_OFF
    if eco:
        if temp > eco_cool_max:
            return MODE_COOL
        if temp < eco_heat_min:
            return MODE_HEAT
        return neutral
    if not sensor_ok:
        return neutral
    if temp > target + band:
        return MODE_COOL
    if temp < target - band:
        if heat_lockout and temp >= heat_lockout_floor:
            return neutral
        return MODE_HEAT
    return neutral


def shared_mode(
    *,
    primary_demand: str,
    secondary_demand: str,
    current: str,
    allowed: bool,
    resting: str | None = None,
) -> str:
    """Choose the shared mode (cool|heat). Mirrors the mxz_plan state template.

    ``current`` must already be normalized to cool|heat (cold start -> cool).
    ``allowed`` is the hysteresis gate. The PRIMARY room wins a standoff.

    ``resting`` biases the mode used when NO room is calling. ``None`` (or any
    non cool|heat value) keeps the original behavior — rest at ``current`` (the
    last called mode). Set it to cool|heat to always settle there when idle; a
    genuine opposite demand still flips the mode (the bias only changes the
    neutral fallback, gated by the same hysteresis as any other flip).
    """
    rest = resting if resting in (MODE_COOL, MODE_HEAT) else current
    any_cool = primary_demand == MODE_COOL or secondary_demand == MODE_COOL
    any_heat = primary_demand == MODE_HEAT or secondary_demand == MODE_HEAT
    standoff = any_cool and any_heat

    if standoff:
        proposed = (
            primary_demand if primary_demand in (MODE_COOL, MODE_HEAT) else current
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


def setpoints(
    *, mode: str, target: int, eco: bool, clamp_min: int, clamp_max: int
) -> tuple[int, int]:
    """Return (low, high) setpoint edges, clamped. Mirrors the actuator's edge math."""
    if eco:
        if mode == MODE_COOL:
            return (ECO_COOL_LOW, ECO_COOL_HIGH)
        return (ECO_HEAT_LOW, ECO_HEAT_HIGH)
    tc = max(min(target, clamp_max), clamp_min)
    if mode == MODE_COOL:
        return (max(tc - 2, clamp_min), tc)
    return (tc, min(tc + 2, clamp_max))


def head_action(*, engage: str, mode: str, eco: bool) -> str:
    """Map a head's engage state to the mode to command. Mirrors p_act/s_act."""
    if engage == MODE_OFF:
        return MODE_OFF
    if eco and engage == ENGAGE_SATISFIED:
        return MODE_OFF
    if engage == mode:
        return mode
    return MODE_FAN_ONLY
