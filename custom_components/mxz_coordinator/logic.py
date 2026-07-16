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
    cool_lockout: bool = False,
    cool_lockout_ceiling: float = 200.0,
) -> str:
    """Per-room call for one band (demand uses S/'neutral', engage uses D/'satisfied').

    Mirrors the primary/secondary_demand and primary/secondary_engage attributes.

    ``heat_lockout`` holds off the heat call (the room idles instead of heating — e.g.
    to let passive solar warm it in summer) UNLESS the room has dropped below
    ``heat_lockout_floor``, a safety floor so a genuinely cold room still gets heat.
    ``cool_lockout`` is the mirror for the cold season: it holds off the cool call
    (let the room drift down on its own) UNLESS it rises above ``cool_lockout_ceiling``.
    The eco extremes are unaffected; the two lockouts are independent.
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
        if cool_lockout and temp <= cool_lockout_ceiling:
            return neutral
        return MODE_COOL
    if temp < target - band:
        if heat_lockout and temp >= heat_lockout_floor:
            return neutral
        return MODE_HEAT
    return neutral


def engage_with_latch(
    *,
    prior: str | None,
    band: float,
    neutral: str,
    **call_kwargs,
) -> str:
    """Per-room engage with run-to-target hysteresis.

    The engage deadband exists so a satisfied room COASTS instead of flip-
    flopping — it was never meant to truncate the approach. Without a latch,
    a room cooling toward 63 would stop at 64 (target + band) and never reach
    the number the user actually set.

    * Not engaged (``prior`` is None): a room must drift PAST ``band`` to
      engage — unchanged behavior.
    * Engaged (``prior`` is cool|heat): the room runs all the way TO its
      target (band 0). Crossing the target — or a lockout / eco / disable /
      sensor dropout, all still evaluated via :func:`room_call` — disengages
      it back to ``neutral`` to coast until it drifts past ``band`` again.
    * A direction flip while engaged (overshoot, opened window) never
      whiplashes: anything other than continuing in ``prior``'s direction
      disengages to ``neutral`` first.

    Eco mode bypasses the latch entirely (protection extremes are already a
    wide band; run-to-extreme would defeat their purpose).
    """
    if prior in (MODE_COOL, MODE_HEAT) and not call_kwargs.get("eco"):
        raw = room_call(band=0.0, neutral=neutral, **call_kwargs)
        if raw == prior or raw == MODE_OFF:
            return raw
        return neutral  # target reached / lockout / direction flip -> coast
    return room_call(band=band, neutral=neutral, **call_kwargs)


def season_lockouts(
    *, outdoor_high: float | None, heat_above: float, cool_below: float
) -> tuple[bool, bool]:
    """Derive (heat_lockout, cool_lockout) from the local outdoor daily-high temp.

    The decision is driven by *local weather*, not the calendar: pass the forecast
    daily high (from a weather entity) or an outdoor temperature reading.

    * high >= ``heat_above`` -> warm season -> heat_lockout on (don't actively heat).
    * high <= ``cool_below`` -> cold season -> cool_lockout on (don't actively cool).
    * in between (shoulder season) -> neither -> normal heat + cool.

    The gap between the two thresholds is the hysteresis band — the forecast has to
    swing across the whole shoulder zone to flip a lockout, so it won't chatter.
    ``None`` (no signal — weather unavailable) yields no lockout (safe: normal auto).
    """
    if outdoor_high is None:
        return (False, False)
    if outdoor_high >= heat_above:
        return (True, False)
    if outdoor_high <= cool_below:
        return (False, True)
    return (False, False)


def shared_mode(
    *,
    demands: list[str],
    current: str,
    allowed: bool,
    resting: str | None = None,
) -> str:
    """Choose the shared mode (cool|heat). Mirrors the mxz_plan state template.

    ``demands`` is one demand per zone in PRIORITY ORDER (index 0 = highest).
    ``current`` must already be normalized to cool|heat (cold start -> cool).
    ``allowed`` is the hysteresis gate. In a standoff (some zones calling cool
    while others call heat) the HIGHEST-PRIORITY calling zone wins — the N-zone
    generalization of "the primary room wins".

    ``resting`` biases the mode used when NO room is calling. ``None`` (or any
    non cool|heat value) keeps the original behavior — rest at ``current`` (the
    last called mode). Set it to cool|heat to always settle there when idle; a
    genuine opposite demand still flips the mode (the bias only changes the
    neutral fallback, gated by the same hysteresis as any other flip).
    """
    rest = resting if resting in (MODE_COOL, MODE_HEAT) else current
    any_cool = MODE_COOL in demands
    any_heat = MODE_HEAT in demands
    standoff = any_cool and any_heat

    if standoff:
        proposed = next(
            (d for d in demands if d in (MODE_COOL, MODE_HEAT)), current
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
    *,
    mode: str,
    target: float,
    eco: bool,
    clamp_min: float,
    clamp_max: float,
    band: float = 2.0,
    step: float = 1.0,
    eco_cool: tuple[float, float] = (float(ECO_COOL_LOW), float(ECO_COOL_HIGH)),
    eco_heat: tuple[float, float] = (float(ECO_HEAT_LOW), float(ECO_HEAT_HIGH)),
) -> tuple[float, float]:
    """Return (low, high) setpoint edges, clamped. Mirrors the actuator's edge math.

    Unit-agnostic: ``band``/``step``/``eco_*`` carry the temperature unit. The
    defaults reproduce the original °F behavior (2° band, whole-degree steps,
    76/78 & 59/61 eco edges); a °C caller passes a 1° band, 0.5° step, and metric
    eco edges. Edges are rounded to ``step`` and clamped to [clamp_min, clamp_max].
    """
    if eco:
        return eco_cool if mode == MODE_COOL else eco_heat
    tc = max(min(target, clamp_max), clamp_min)
    if mode == MODE_COOL:
        low, high = max(tc - band, clamp_min), tc
    else:
        low, high = tc, min(tc + band, clamp_max)
    return (_round_to(low, step), _round_to(high, step))


def _round_to(value: float, step: float) -> float:
    """Round ``value`` to the nearest multiple of ``step`` (step<=0 -> unchanged)."""
    if step <= 0:
        return value
    return round(value / step) * step


def fan_for_delta(
    *,
    delta: float,
    cur_idx: int,
    up_at: tuple[float, ...],
    down_at: tuple[float, ...],
    max_idx: int,
) -> int:
    """Pick a fan-ladder INDEX (0..) from how far the room is off-target.

    Tesla-style: a big ``delta`` steps the fan up toward ``max_idx``; as the room
    closes on target the fan eases down. ``up_at``/``down_at`` carry a 0.5 °F
    hysteresis band so the speed doesn't chatter on the boundary. The caller owns
    persisting ``cur_idx`` between calls (per head).
    """
    # step UP as far as delta allows, never above max_idx
    while cur_idx < len(up_at) and cur_idx < max_idx and delta >= up_at[cur_idx]:
        cur_idx += 1
    # step DOWN only once delta clears the lower rung's down-threshold
    while cur_idx > 0 and delta < down_at[cur_idx - 1]:
        cur_idx -= 1
    return min(cur_idx, max_idx)


def head_action(*, engage: str, mode: str, eco: bool) -> str:
    """Map a head's engage state to the mode to command. Mirrors p_act/s_act."""
    if engage == MODE_OFF:
        return MODE_OFF
    if eco and engage == ENGAGE_SATISFIED:
        return MODE_OFF
    if engage == mode:
        return mode
    return MODE_FAN_ONLY
