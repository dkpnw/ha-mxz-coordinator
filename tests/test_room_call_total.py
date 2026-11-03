"""room_call and call_edges answer every input; reachable answers are unchanged.

The coordinator only passes non-negative bands (drift is clamped into the
unit profile's positive bounds, the demand band is max(threshold, drift),
the engaged latch passes 0) and a Heat to at or below Cool to (a single
target is both). Over that table, both the call and the shown edges must be
exactly what they were. Outside it (negative bands or an inverted range), a
held-off cool call used to raise ValueError; it now answers as the
cool-first rule does.

The oracles are written out here, independently of logic.py: the cool-first
rule room_call has always followed, and the plain edge formula the plan has
always shown.
"""

from __future__ import annotations

import itertools
import math

from tests.test_logic import COOL, HEAT, NEUTRAL, logic

_ECO = {"eco_cool_max": 78.0, "eco_heat_min": 60.0}


def _cool_first(
    *,
    temp: float,
    heat_from: float,
    cool_from: float,
    heat_band: float,
    cool_band: float,
    eco: bool,
    heat_lockout: bool,
    heat_lockout_floor: float,
    cool_lockout: bool,
    cool_lockout_ceiling: float,
) -> str:
    """The cool-first rule: cool past its edge unless held, then heat likewise."""
    if eco:
        if temp > _ECO["eco_cool_max"]:
            return COOL
        if temp < _ECO["eco_heat_min"]:
            return HEAT
        return NEUTRAL
    if temp > cool_from + cool_band:
        if cool_lockout and temp <= cool_lockout_ceiling:
            return NEUTRAL
        return COOL
    if temp < heat_from - heat_band:
        if heat_lockout and temp >= heat_lockout_floor:
            return NEUTRAL
        return HEAT
    return NEUTRAL


def _plain_edges(
    *,
    heat_from: float,
    cool_from: float,
    heat_band: float,
    cool_band: float,
    eco: bool,
    heat_lockout: bool,
    heat_lockout_floor: float,
    cool_lockout: bool,
    cool_lockout_ceiling: float,
) -> tuple[float, float]:
    """The edges the plan has shown for every reachable input."""
    if eco:
        return _ECO["eco_heat_min"], _ECO["eco_cool_max"]
    heat_below = heat_from - heat_band
    cool_above = cool_from + cool_band
    if heat_lockout:
        heat_below = min(heat_below, heat_lockout_floor)
    if cool_lockout:
        cool_above = max(cool_above, cool_lockout_ceiling)
    return heat_below, cool_above


def _temps(*lines: float) -> list[float]:
    """Every line, the doubles either side of it, and a half degree out."""
    out: set[float] = set()
    for line in lines:
        out.update(
            (line, math.nextafter(line, -math.inf), math.nextafter(line, math.inf),
             line - 0.5, line + 0.5)
        )
    return sorted(out)


def _check(targets, bands, floors, ceilings) -> int:
    """Compare room_call and call_edges to the cool-first rule over a grid.

    Returns how many readings were checked, so a caller can prove the grid ran.
    """
    checked = 0
    for (heat_from, cool_from), hb, cb, eco, hl, floor, cl, ceiling in itertools.product(
        targets, bands, bands, (False, True), (False, True), floors, (False, True), ceilings,
    ):
        rule = {
            "heat_band": hb, "cool_band": cb, "eco": eco,
            "heat_lockout": hl, "heat_lockout_floor": floor,
            "cool_lockout": cl, "cool_lockout_ceiling": ceiling,
        }
        call = {
            "target": (heat_from + cool_from) / 2,
            "heat_target": heat_from, "cool_target": cool_from, **rule, **_ECO,
        }
        heat_below, cool_above = logic.call_edges(**call)
        lines = (
            heat_from - hb, cool_from + cb, heat_from, cool_from, floor, ceiling,
            *_ECO.values(),
        )
        for temp in _temps(*lines):
            expected = _cool_first(
                temp=temp, heat_from=heat_from, cool_from=cool_from, **rule
            )
            assert logic.room_call(
                temp=temp, enabled=True, sensor_ok=True, neutral=NEUTRAL, **call,
            ) == expected, (temp, call)
            # The shown edges promise the same call.
            shown = (
                COOL if temp > cool_above else HEAT if temp < heat_below else NEUTRAL
            )
            assert shown == expected, (temp, call, (heat_below, cool_above))
            checked += 1
    return checked


def test_reachable_inputs_answer_and_show_exactly_as_before() -> None:
    """Every input the coordinator can pass: same call, same edges."""
    targets = ((70.0, 70.0), (68.0, 72.0), (69.75, 70.25))
    bands = (0.0, 0.25, 0.5, 1.0, 3.0)
    floors = (58.0, 67.5, 69.0, 72.0)
    ceilings = (80.0, 72.5, 71.0, 68.0)
    checked = _check(targets, bands, floors, ceilings)
    assert checked > 200_000  # the grid really ran
    for (heat_from, cool_from), hb, cb, eco, hl, floor, cl, ceiling in itertools.product(
        targets, bands, bands, (False, True), (False, True), floors, (False, True), ceilings,
    ):
        rule = {
            "heat_band": hb, "cool_band": cb, "eco": eco,
            "heat_lockout": hl, "heat_lockout_floor": floor,
            "cool_lockout": cl, "cool_lockout_ceiling": ceiling,
        }
        assert logic.call_edges(
            target=(heat_from + cool_from) / 2, heat_target=heat_from,
            cool_target=cool_from, **rule, **_ECO,
        ) == _plain_edges(heat_from=heat_from, cool_from=cool_from, **rule), rule


def test_unreachable_overlapping_regions_answer_the_cool_first_rule() -> None:
    """Negative bands and an inverted range, with and without lockouts: no
    input raises, and the call is the cool-first rule's, readings exactly on
    each edge included."""
    targets = ((70.0, 70.0), (72.0, 68.0), (68.0, 72.0))
    bands = (-3.0, -1.0, -0.5, 0.0, 1.0)
    floors = (58.0, 69.0, 74.0)
    ceilings = (80.0, 69.5, 66.0)
    assert _check(targets, bands, floors, ceilings) > 50_000


def test_the_former_value_error_input_holds() -> None:
    """The input the backlog named: a cool lockout with a negative band sum.
    It raised ValueError; the cool-first rule holds the call there."""
    kwargs = {
        "target": 70.0, "heat_band": -2.0, "cool_band": 0.0, "eco": False,
        "heat_lockout": False, "heat_lockout_floor": 58.0,
        "cool_lockout": True, "cool_lockout_ceiling": 74.0, **_ECO,
    }
    call = {"enabled": True, "sensor_ok": True, "neutral": NEUTRAL, **kwargs}
    # Past the cool edge (70), held by the ceiling: hold, never heat.
    assert logic.room_call(temp=70.5, **call) == NEUTRAL
    assert logic.room_call(temp=74.0, **call) == NEUTRAL
    # Past the ceiling: cool.
    assert logic.room_call(temp=74.5, **call) == COOL
    # At or below the cool edge, below the heat edge (72): heat.
    assert logic.room_call(temp=70.0, **call) == HEAT
    assert logic.room_call(temp=69.0, **call) == HEAT
    # The edges say the same: heat strictly below the double after 70.0.
    assert logic.call_edges(**kwargs) == (math.nextafter(70.0, math.inf), 74.0)
    # The latch's engaged phase (band 0 both sides) is unaffected and the
    # not-engaged phase uses the same answer.
    latch = {k: v for k, v in call.items() if k not in ("heat_band", "cool_band")}
    assert logic.engage_with_latch(
        prior=None, temp=70.5, heat_band=-2.0, cool_band=0.0, **latch
    ) == NEUTRAL
