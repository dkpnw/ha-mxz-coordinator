"""Visible start points in the pure decision math (no Home Assistant required).

``call_edges`` returns the two edges ``room_call`` compares a reading to, so
the start points the plan shows are the ones the decision uses. These pin
that the edges reproduce the 3.8.0 rule exactly, boundary values included,
for eco and both lockouts, and that a held-off cool call over overlapping
regions (once refused) answers as 3.8.0 did.
"""

from __future__ import annotations

import itertools
import math

import pytest

from tests.test_logic import COOL, HEAT, NEUTRAL, OFF, logic

_TARGET = 70.0
_ECO = {"eco_cool_max": 78.0, "eco_heat_min": 60.0}


def _rule_380(
    *,
    temp: float,
    heat_band: float,
    cool_band: float,
    eco: bool,
    heat_lockout: bool,
    heat_lockout_floor: float,
    cool_lockout: bool,
    cool_lockout_ceiling: float,
) -> str:
    """room_call's body as shipped in 3.8.0, written out independently."""
    if eco:
        if temp > _ECO["eco_cool_max"]:
            return COOL
        if temp < _ECO["eco_heat_min"]:
            return HEAT
        return NEUTRAL
    if temp > _TARGET + cool_band:
        if cool_lockout and temp <= cool_lockout_ceiling:
            return NEUTRAL
        return COOL
    if temp < _TARGET - heat_band:
        if heat_lockout and temp >= heat_lockout_floor:
            return NEUTRAL
        return HEAT
    return NEUTRAL


def _from_edges(temp: float, edges: tuple[float, float]) -> str:
    """The call the shown edges promise: cool above, else heat below."""
    heat_below, cool_above = edges
    if temp > cool_above:
        return COOL
    if temp < heat_below:
        return HEAT
    return NEUTRAL


# Floors and ceilings below, on, and past the comfort edges they compete with
# (69.5 is 70 - 0.5 and 70.5 is 70 + 0.5: a tie with a comfort edge).
_FLOORS = (58.0, 69.5, 72.0)
_CEILINGS = (80.0, 70.5, 68.0)
_BANDS = (0.0, 0.5, 1.0, 3.0)
_GRID = list(
    itertools.product(
        _BANDS,  # heat_band
        _BANDS,  # cool_band
        (False, True),  # eco
        (False, True),  # heat_lockout
        _FLOORS,
        (False, True),  # cool_lockout
        _CEILINGS,
    )
)


def _temps(*lines: float) -> list[float]:
    """Every line, the doubles either side of it, and a half degree out."""
    out: set[float] = set()
    for line in lines:
        out.update(
            (line, math.nextafter(line, -math.inf), math.nextafter(line, math.inf),
             line - 0.5, line + 0.5)
        )
    return sorted(out)


def test_edges_agree_with_the_380_rule_on_every_boundary() -> None:
    """Exact agreement, including readings exactly on each edge and line."""
    checked = 0
    for heat_band, cool_band, eco, hl, floor, cl, ceiling in _GRID:
        kwargs = {
            "heat_band": heat_band, "cool_band": cool_band, "eco": eco,
            "heat_lockout": hl, "heat_lockout_floor": floor,
            "cool_lockout": cl, "cool_lockout_ceiling": ceiling,
        }
        edges = logic.call_edges(target=_TARGET, **kwargs, **_ECO)
        lines = (
            _TARGET - heat_band, _TARGET + cool_band, floor, ceiling,
            _ECO["eco_cool_max"], _ECO["eco_heat_min"], _TARGET,
        )
        for temp in _temps(*lines):
            expected = _rule_380(temp=temp, **kwargs)
            assert _from_edges(temp, edges) == expected, (temp, kwargs)
            assert logic.room_call(
                temp=temp, target=_TARGET, enabled=True, sensor_ok=True,
                neutral=NEUTRAL, **kwargs, **_ECO,
            ) == expected, (temp, kwargs)
            checked += 1
    assert checked > 20_000  # the grid really ran


@pytest.mark.parametrize(
    ("kwargs", "edges"),
    [
        # Comfort: target -/+ each side's band.
        ({}, (69.0, 71.0)),
        ({"heat_band": 3.0, "cool_band": 5.0}, (67.0, 75.0)),
        # Eco: the extremes, whatever the bands and lockouts.
        ({"eco": True}, (60.0, 78.0)),
        ({"eco": True, "heat_lockout": True, "cool_lockout": True}, (60.0, 78.0)),
        # A lockout moves its side out to its safety line...
        ({"heat_lockout": True}, (58.0, 71.0)),
        ({"cool_lockout": True}, (69.0, 80.0)),
        ({"heat_lockout": True, "cool_lockout": True}, (58.0, 80.0)),
        # ...and never inward: a line inside the comfort edge changes nothing.
        ({"heat_lockout": True, "heat_lockout_floor": 69.5}, (69.0, 71.0)),
        ({"cool_lockout": True, "cool_lockout_ceiling": 70.5}, (69.0, 71.0)),
    ],
    ids=[
        "comfort", "uneven", "eco", "eco-ignores-lockouts", "heat-lockout",
        "cool-lockout", "both-lockouts", "floor-inside", "ceiling-inside",
    ],
)
def test_edge_values(kwargs: dict, edges: tuple[float, float]) -> None:
    base = {
        "heat_band": 1.0, "cool_band": 1.0, "eco": False,
        "heat_lockout": False, "heat_lockout_floor": 58.0,
        "cool_lockout": False, "cool_lockout_ceiling": 80.0,
    }
    assert logic.call_edges(target=_TARGET, **{**base, **kwargs}, **_ECO) == edges


@pytest.mark.parametrize(
    "field",
    ["temp", "heat_band", "cool_band", "heat_lockout_floor", "cool_lockout_ceiling"],
)
def test_a_nan_anywhere_answers_as_380_did(field: str) -> None:
    """max()/min() take the comfort edge first, so NaN resolves like the old
    pair of comparisons (a NaN line never holds a call off; a NaN band or
    reading never starts one)."""
    for temp, hl, cl in itertools.product(
        (55.0, 66.0, 70.0, 74.0, 85.0), (False, True), (False, True)
    ):
        kwargs = {
            "temp": temp, "heat_band": 1.0, "cool_band": 1.0, "eco": False,
            "heat_lockout": hl, "heat_lockout_floor": 66.0,
            "cool_lockout": cl, "cool_lockout_ceiling": 74.0,
        }
        kwargs[field] = math.nan
        assert logic.room_call(
            target=_TARGET, enabled=True, sensor_ok=True, neutral=NEUTRAL,
            **kwargs, **_ECO,
        ) == _rule_380(**kwargs), kwargs


def test_disabled_and_no_reading_answer_before_any_edge() -> None:
    """A disabled room is off and a room with no reading neutral, whatever the
    bands, exactly as before: the edges are only asked of a live room."""
    for band in (1.0, -5.0):
        kwargs = {
            "temp": 90.0, "target": _TARGET, "heat_band": band, "cool_band": band,
            "eco": False, "neutral": NEUTRAL, "cool_lockout": True, **_ECO,
        }
        assert logic.room_call(enabled=False, sensor_ok=True, **kwargs) == OFF
        assert logic.room_call(enabled=True, sensor_ok=False, **kwargs) == NEUTRAL


def test_negative_bands_without_a_cool_lockout_still_answer_as_380() -> None:
    """Overlapping regions are only a problem for a held-off cool call; with
    no cool lockout, cool-first edges are exact even there."""
    for temp, hl in itertools.product(_temps(66.0, 69.0, 71.0, 74.0), (False, True)):
        kwargs = {
            "temp": temp, "heat_band": -2.0, "cool_band": -1.0, "eco": False,
            "heat_lockout": hl, "heat_lockout_floor": 66.0,
            "cool_lockout": False, "cool_lockout_ceiling": 74.0,
        }
        assert logic.room_call(
            target=_TARGET, enabled=True, sensor_ok=True, neutral=NEUTRAL,
            **kwargs, **_ECO,
        ) == _rule_380(**kwargs), kwargs


def test_a_held_cool_call_over_overlapping_regions_answers_as_380() -> None:
    """3.8.0 answered "hold" at 70.5 here (cool first, held by the lockout),
    though 70.5 is also past the heat edge 72. It is answered exactly as
    3.8.0 did, never as heat: heat starts at or below the cool edge, the
    double after it as a strict edge. (This case once raised ValueError.)"""
    rule = {
        "heat_band": -2.0, "cool_band": 0.0, "eco": False,
        "heat_lockout": False, "heat_lockout_floor": 58.0,
        "cool_lockout": True, "cool_lockout_ceiling": 74.0,
    }
    kwargs = {"target": _TARGET, **rule, **_ECO}
    assert _rule_380(temp=70.5, **rule) == NEUTRAL
    edges = logic.call_edges(**kwargs)
    assert edges == (math.nextafter(70.0, math.inf), 74.0)
    for temp in _temps(70.0, 72.0, 74.0):
        expected = _rule_380(temp=temp, **rule)
        assert _from_edges(temp, edges) == expected, temp
        assert logic.room_call(
            temp=temp, enabled=True, sensor_ok=True, neutral=NEUTRAL, **kwargs
        ) == expected, temp
    assert logic.room_call(
        temp=70.5, enabled=True, sensor_ok=True, neutral=NEUTRAL, **kwargs
    ) == NEUTRAL
    # A zero sum (no overlap) keeps the plain edges.
    assert logic.call_edges(**{**kwargs, "cool_band": 2.0}) == (72.0, 74.0)


def test_engage_with_latch_starts_on_the_same_edges() -> None:
    """The start edge is the not-yet-engaged engage call's edge; an engaged
    room still runs to the target (band 0), past the shown start point."""
    common = {
        "target": _TARGET, "enabled": True, "sensor_ok": True, "eco": False,
        "heat_lockout": False, "heat_lockout_floor": 58.0,
        "cool_lockout": False, "cool_lockout_ceiling": 80.0, **_ECO,
    }
    heat_start, cool_start = logic.call_edges(
        heat_band=1.5, cool_band=3.0,
        **{k: v for k, v in common.items() if k not in ("enabled", "sensor_ok")},
    )
    assert (heat_start, cool_start) == (68.5, 73.0)

    def engage(temp: float, prior: str | None) -> str:
        return logic.engage_with_latch(
            prior=prior, temp=temp, heat_band=1.5, cool_band=3.0, neutral="satisfied",
            **common,
        )

    assert engage(cool_start, None) == "satisfied"
    assert engage(math.nextafter(cool_start, math.inf), None) == COOL
    assert engage(heat_start, None) == "satisfied"
    assert engage(math.nextafter(heat_start, -math.inf), None) == HEAT
    # Engaged: keeps running inside the start points, until the target.
    assert engage(71.0, COOL) == COOL
    assert engage(_TARGET, COOL) == "satisfied"
