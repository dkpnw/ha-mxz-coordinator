"""Dual setpoint in the pure decision math (no Home Assistant required).

A dual-setpoint room passes Heat to (L) and Cool to (H) as ``heat_target`` /
``cool_target``. The oracle here is the design's table (DESIGN.md section 4),
written out by hand rather than derived from the code: heat starts below
``L - heat_band``, cool starts above ``H + cool_band``, a heat run ends at L
and a cool run at H, eco ignores L and H, and each lockout moves its own side
out to its safety line. A single target is the case ``L == H == target``, and
for it every function returns exactly what it returns with no per-side
targets at all.
"""

from __future__ import annotations

import itertools

import pytest

from tests.test_logic import COOL, HEAT, NEUTRAL, OFF, logic

_ECO = {"eco_cool_max": 78.0, "eco_heat_min": 60.0}
_COMMON = {
    "eco": False, **_ECO, "heat_lockout": False, "heat_lockout_floor": 58.0,
    "cool_lockout": False, "cool_lockout_ceiling": 80.0,
}


def _oracle(
    *, temp, low, high, heat_band, cool_band, eco=False,
    heat_lockout=False, heat_lockout_floor=58.0,
    cool_lockout=False, cool_lockout_ceiling=80.0,
):
    """The design's dual-setpoint call, independently of call_edges."""
    if eco:
        if temp > _ECO["eco_cool_max"]:
            return COOL
        if temp < _ECO["eco_heat_min"]:
            return HEAT
        return NEUTRAL
    if temp > high + cool_band and not (cool_lockout and temp <= cool_lockout_ceiling):
        return COOL
    if temp < low - heat_band and not (heat_lockout and temp >= heat_lockout_floor):
        return HEAT
    return NEUTRAL


def _call(temp, low, high, heat_band, cool_band, **over):
    kwargs = {**_COMMON, **over}
    return logic.room_call(
        temp=temp, target=(low + high) / 2, enabled=True, sensor_ok=True,
        heat_band=heat_band, cool_band=cool_band, neutral=NEUTRAL,
        heat_target=low, cool_target=high, **kwargs,
    )


# --- the call: per-side starts, eco, lockouts --------------------------------
@pytest.mark.parametrize(
    ("eco", "heat_lockout", "cool_lockout"),
    list(itertools.product((False, True), repeat=3)),
)
def test_room_call_matches_the_design_table(eco, heat_lockout, cool_lockout):
    """Every reading, range, uneven band pair, eco, and lockout combination."""
    temps = [55 + 0.25 * i for i in range(121)]  # 55 .. 85
    for (low, high), (hb, cb) in itertools.product(
        ((68.0, 74.0), (69.0, 71.0), (66.0, 76.0), (70.0, 72.0)),
        ((1.0, 1.0), (3.0, 0.5), (0.5, 4.0), (0.0, 0.0)),
    ):
        for temp in temps:
            over = {"eco": eco, "heat_lockout": heat_lockout, "cool_lockout": cool_lockout}
            want = _oracle(
                temp=temp, low=low, high=high, heat_band=hb, cool_band=cb, **over
            )
            assert _call(temp, low, high, hb, cb, **over) == want, (
                temp, low, high, hb, cb, over,
            )


def test_heat_and_cool_start_at_each_side_with_its_own_drift():
    """A 68-74 room with defaults heats below 67 and cools above 75."""
    assert _call(66.9, 68, 74, 1, 1) == HEAT
    assert _call(67.0, 68, 74, 1, 1) == NEUTRAL  # strict, like the single target
    assert _call(71.0, 68, 74, 1, 1) == NEUTRAL  # the middle never calls
    assert _call(75.0, 68, 74, 1, 1) == NEUTRAL
    assert _call(75.1, 68, 74, 1, 1) == COOL
    # Uneven drift: each side keeps its own margin from its own edge.
    assert _call(64.9, 68, 74, 3, 0.5) == HEAT
    assert _call(65.1, 68, 74, 3, 0.5) == NEUTRAL
    assert _call(74.6, 68, 74, 3, 0.5) == COOL


def test_demand_keeps_the_mode_switch_threshold_from_each_edge():
    """The shared-unit vote: below L - max(T, d_heat), above H + max(T, d_cool)."""
    threshold = 3.0
    for d_heat, d_cool in ((1.0, 1.0), (4.0, 1.0), (1.0, 5.0)):
        hb, cb = max(threshold, d_heat), max(threshold, d_cool)
        assert _call(68 - hb - 0.01, 68, 74, hb, cb) == HEAT
        assert _call(68 - hb, 68, 74, hb, cb) == NEUTRAL
        assert _call(74 + cb, 68, 74, hb, cb) == NEUTRAL
        assert _call(74 + cb + 0.01, 68, 74, hb, cb) == COOL


def test_lockouts_move_their_own_side_out_to_the_safety_line():
    on = {"heat_lockout": True, "heat_lockout_floor": 58.0}
    assert _call(60, 68, 74, 1, 1, **on) == NEUTRAL  # held, past L - d but above the floor
    assert _call(57.9, 68, 74, 1, 1, **on) == HEAT  # heat anyway below the floor
    assert _call(76, 68, 74, 1, 1, **on) == COOL  # the cool side is untouched
    on = {"cool_lockout": True, "cool_lockout_ceiling": 80.0}
    assert _call(78, 68, 74, 1, 1, **on) == NEUTRAL
    assert _call(80.1, 68, 74, 1, 1, **on) == COOL
    assert _call(66, 68, 74, 1, 1, **on) == HEAT


def test_eco_ignores_heat_to_and_cool_to():
    for low, high in ((68, 74), (62, 64), (75, 77)):
        assert _call(61, low, high, 1, 1, eco=True) == NEUTRAL
        assert _call(59.9, low, high, 1, 1, eco=True) == HEAT
        assert _call(78.1, low, high, 1, 1, eco=True) == COOL
        assert logic.call_edges(
            target=70.0, heat_band=1, cool_band=1, heat_target=low, cool_target=high,
            **{**_COMMON, "eco": True},
        ) == (60.0, 78.0)


def test_disabled_and_blind_rooms_are_unchanged():
    for enabled, ok, want in ((False, True, OFF), (False, False, OFF), (True, False, NEUTRAL)):
        assert logic.room_call(
            temp=40.0, target=71.0, enabled=enabled, sensor_ok=ok, band=1.0,
            neutral=NEUTRAL, heat_target=68.0, cool_target=74.0, **_COMMON,
        ) == want


# --- start points are the edges used -----------------------------------------
def test_call_edges_are_the_edges_room_call_compares_to():
    for low, high, hb, cb in ((68, 74, 1, 1), (69, 71, 3, 0.5), (66, 76, 0.5, 4)):
        for lockouts in itertools.product((False, True), repeat=2):
            over = {"heat_lockout": lockouts[0], "cool_lockout": lockouts[1]}
            edges = logic.call_edges(
                target=(low + high) / 2, heat_band=hb, cool_band=cb,
                heat_target=low, cool_target=high, **{**_COMMON, **over},
            )
            want = (
                min(low - hb, 58.0) if lockouts[0] else low - hb,
                max(high + cb, 80.0) if lockouts[1] else high + cb,
            )
            assert edges == want
            for temp in (edges[0] - 0.01, edges[0], edges[1], edges[1] + 0.01):
                heat_below, cool_above = edges
                shown = COOL if temp > cool_above else HEAT if temp < heat_below else NEUTRAL
                assert _call(temp, low, high, hb, cb, **over) == shown


# --- a single target is the special case L == H == target ---------------------
def test_single_target_is_identical_with_or_without_per_side_targets():
    temps = [60 + 0.5 * i for i in range(41)]
    for target, hb, cb, eco, hl, cl in itertools.product(
        (62.0, 70.0, 21.5), (1.0, 3.0, 0.0), (1.0, 0.5, 4.0),
        (False, True), (False, True), (False, True),
    ):
        kwargs = {
            **_COMMON, "eco": eco, "heat_lockout": hl, "cool_lockout": cl,
        }
        assert logic.call_edges(
            target=target, heat_band=hb, cool_band=cb, **kwargs
        ) == logic.call_edges(
            target=target, heat_band=hb, cool_band=cb,
            heat_target=target, cool_target=target, **kwargs,
        )
        for temp in temps:
            base = {"temp": temp, "target": target, "enabled": True, "sensor_ok": True}
            assert logic.room_call(
                **base, heat_band=hb, cool_band=cb, neutral=NEUTRAL, **kwargs
            ) == logic.room_call(
                **base, heat_band=hb, cool_band=cb, neutral=NEUTRAL,
                heat_target=target, cool_target=target, **kwargs,
            )
            for prior in (None, HEAT, COOL):
                assert logic.engage_with_latch(
                    prior=prior, **base, heat_band=hb, cool_band=cb,
                    neutral="satisfied", **kwargs,
                ) == logic.engage_with_latch(
                    prior=prior, **base, heat_band=hb, cool_band=cb,
                    neutral="satisfied", heat_target=target, cool_target=target,
                    **kwargs,
                )


# --- the latch: a heat run ends at L, a cool run at H -------------------------
def _engage(prior, temp, low=68.0, high=74.0, **over):
    return logic.engage_with_latch(
        prior=prior, temp=temp, target=(low + high) / 2, enabled=True,
        sensor_ok=True, heat_band=1.0, cool_band=1.0, neutral="satisfied",
        heat_target=low, cool_target=high, **{**_COMMON, **over},
    )


def test_heat_run_ends_at_heat_to_not_the_midpoint():
    assert _engage(None, 66.9) == HEAT  # starts below L - drift
    assert _engage(HEAT, 67.5) == HEAT  # keeps running inside the drift
    assert _engage(HEAT, 67.99) == HEAT
    assert _engage(HEAT, 68.0) == "satisfied"  # reached L: coasts
    assert _engage(HEAT, 70.0) == "satisfied"  # never runs on to the midpoint (71)
    assert _engage(None, 67.5) == "satisfied"  # coasting, not past the start edge


def test_cool_run_ends_at_cool_to_not_the_midpoint():
    assert _engage(None, 75.1) == COOL
    assert _engage(COOL, 74.5) == COOL
    assert _engage(COOL, 74.0) == "satisfied"  # reached H
    assert _engage(COOL, 72.0) == "satisfied"


def test_latch_never_whiplashes_and_eco_lockouts_disable_still_win():
    assert _engage(HEAT, 75.5) == "satisfied"  # overshoot past H: coast first
    assert _engage(COOL, 66.0) == "satisfied"
    assert _engage(HEAT, 67.5, eco=True) == "satisfied"  # eco bypasses the latch
    assert _engage(HEAT, 59.0, eco=True) == HEAT
    assert _engage(HEAT, 67.5, heat_lockout=True) == "satisfied"
    assert logic.engage_with_latch(
        prior=HEAT, temp=60.0, target=71.0, enabled=False, sensor_ok=True,
        heat_band=1.0, cool_band=1.0, neutral="satisfied",
        heat_target=68.0, cool_target=74.0, **_COMMON,
    ) == OFF


# --- the ValueError guard ------------------------------------------------------
def test_guard_refuses_only_overlapping_regions():
    lock = {**_COMMON, "cool_lockout": True}
    # Single target: exactly as before (a negative band sum is refused).
    with pytest.raises(ValueError):
        logic.call_edges(target=70.0, heat_band=-1.0, cool_band=0.5, **lock)
    logic.call_edges(target=70.0, heat_band=0.0, cool_band=0.0, **lock)
    # Dual: the regions overlap only when L - heat_band > H + cool_band.
    logic.call_edges(
        target=70.0, heat_band=-1.0, cool_band=-1.0, heat_target=68.0,
        cool_target=72.0, **lock,
    )  # 69 < 71: no overlap
    logic.call_edges(
        target=70.0, heat_band=-2.0, cool_band=-2.0, heat_target=68.0,
        cool_target=72.0, **lock,
    )  # 70 == 70: touching, still no overlap
    with pytest.raises(ValueError):
        logic.call_edges(
            target=70.0, heat_band=-3.0, cool_band=-2.0, heat_target=68.0,
            cool_target=72.0, **lock,
        )  # 71 > 70
    # The latch's band 0 never trips it for a valid range.
    assert _engage(HEAT, 67.0, cool_lockout=True) == HEAT


# --- head setpoints ------------------------------------------------------------
@pytest.mark.parametrize(
    ("profile", "low", "high", "heat_want", "cool_want"),
    [
        ({"band": 2.0, "step": 1.0, "clamp_min": 59, "clamp_max": 88}, 68.0, 74.0, (68, 70), (72, 74)),
        ({"band": 1.0, "step": 0.5, "clamp_min": 15, "clamp_max": 31,
          "eco_cool": (24.0, 26.0), "eco_heat": (15.0, 16.0)}, 20.0, 23.5, (20.0, 21.0), (22.5, 23.5)),
    ],
    ids=["F", "C"],
)
def test_setpoints_heat_from_heat_to_and_cool_from_cool_to(profile, low, high, heat_want, cool_want):
    kw = {"target": (low + high) / 2, "eco": False, "heat_target": low, "cool_target": high, **profile}
    assert logic.setpoints(mode=HEAT, **kw) == heat_want
    assert logic.setpoints(mode=COOL, **kw) == cool_want
    # Any other shared mode reads Heat to (only the off attribute uses it).
    assert logic.setpoints(mode="fan_only", **kw) == heat_want
    assert logic.setpoints(mode=OFF, **kw) == heat_want
    # Eco is the eco band whatever the range.
    eco = {**kw, "eco": True}
    assert logic.setpoints(mode=COOL, **eco) == logic.setpoints(
        mode=COOL, target=0.0, eco=True, **profile
    )


def test_setpoints_clamp_each_side():
    kw = {"band": 2.0, "step": 1.0, "clamp_min": 59, "clamp_max": 88, "eco": False}
    assert logic.setpoints(mode=HEAT, target=60, heat_target=55, cool_target=65, **kw) == (59, 61)
    assert logic.setpoints(mode=COOL, target=85, heat_target=80, cool_target=90, **kw) == (86, 88)


def test_setpoints_single_target_unchanged():
    for mode, target, eco in itertools.product((HEAT, COOL, OFF), (58.0, 70.0, 70.4, 90.0), (False, True)):
        kw = {"mode": mode, "target": target, "eco": eco, "clamp_min": 59, "clamp_max": 88}
        assert logic.setpoints(**kw) == logic.setpoints(
            **kw, heat_target=target, cool_target=target
        )


# --- fan boost distance ----------------------------------------------------------
def test_range_delta_inside_below_and_above():
    assert logic.range_delta(temp=71.0, low=68.0, high=74.0) == 0.0
    assert logic.range_delta(temp=68.0, low=68.0, high=74.0) == 0.0
    assert logic.range_delta(temp=74.0, low=68.0, high=74.0) == 0.0
    assert logic.range_delta(temp=65.5, low=68.0, high=74.0) == 2.5
    assert logic.range_delta(temp=77.0, low=68.0, high=74.0) == 3.0
    # A single target: exactly abs(temp - target), bit for bit.
    for temp, target in itertools.product(
        [55 + 0.1 * i for i in range(300)], (70.0, 21.5, 68.3)
    ):
        assert logic.range_delta(temp=temp, low=target, high=target) == abs(temp - target)


# --- range writes: gap, push, bounds, inversion ------------------------------------
_F = {"gap": 2.0, "bounds": (59.0, 88.0), "step": 1.0}
_C = {"gap": 1.0, "bounds": (15.0, 31.0), "step": 0.5}


def _resolve(low, high, new_low, new_high, unit=_F):
    return logic.resolve_range(low=low, high=high, new_low=new_low, new_high=new_high, **unit)


def test_a_wide_enough_pair_is_taken_as_it_is():
    assert _resolve(68, 74, 66, 76) == (66, 76)
    assert _resolve(68, 74, 70, 72) == (70, 72)  # exactly the gap
    assert _resolve(20, 23, 20.5, 21.5, _C) == (20.5, 21.5)


def test_one_edge_too_close_pushes_the_other():
    assert _resolve(68, 74, 73, 74) == (73, 75)  # heat to up: cool to pushed up
    assert _resolve(68, 74, 76, 74) == (76, 78)  # crossing it pushes too
    assert _resolve(68, 74, 68, 69) == (67, 69)  # cool to down: heat to pushed down
    assert _resolve(20, 23, 23, 23, _C) == (23, 24)


def test_a_push_past_the_bounds_is_refused():
    with pytest.raises(logic.RangeError) as err:
        _resolve(80, 86, 87, 86)
    assert err.value.reason == logic.RANGE_PUSH_OUT_OF_BOUNDS
    with pytest.raises(logic.RangeError):
        _resolve(60, 66, 60, 60)
    assert _resolve(80, 86, 86, 86) == (86, 88)  # pushing to the bound itself is fine


def test_both_edges_too_close_or_inverted_are_refused():
    for new in ((70, 71), (71, 71), (72, 70)):
        with pytest.raises(logic.RangeError) as err:
            _resolve(68, 74, *new)
        assert err.value.reason == logic.RANGE_TOO_NARROW
    with pytest.raises(logic.RangeError):
        _resolve(20, 23, 21, 21.5, _C)


def test_an_edge_outside_the_bounds_is_refused_not_clamped():
    for new in ((58, 74), (68, 89), (40, 100)):
        with pytest.raises(logic.RangeError) as err:
            _resolve(68, 74, *new)
        assert err.value.reason == logic.RANGE_OUT_OF_BOUNDS
    assert _resolve(68, 74, 59, 88) == (59, 88)  # the bounds themselves are fine


def test_edges_snap_to_the_unit_step():
    assert _resolve(68, 74, 66.4, 75.6) == (66, 76)
    assert _resolve(20, 23, 20.2, 22.8, _C) == (20.0, 23.0)
    # A HomeKit round trip (°F -> °C -> °F) is noise on an unchanged edge.
    assert _resolve(68, 74, 68.04, 72.96) == (68, 73)


# --- moving the range, seeding, and the midpoint ---------------------------------
def test_shift_keeps_the_width_and_slides_inside_the_bounds():
    assert logic.shift_range(width=6, center=66, **_F_BOUNDS) == (63, 69)
    assert logic.shift_range(width=6, center=60, **_F_BOUNDS) == (59, 65)  # slid up
    assert logic.shift_range(width=6, center=87, **_F_BOUNDS) == (82, 88)  # slid down
    assert logic.shift_range(width=3, center=70, **_F_BOUNDS) == (69, 72)  # half rounds up
    assert logic.shift_range(width=3, center=71, **_F_BOUNDS) == (70, 73)
    assert logic.shift_range(width=1.0, center=21.0, bounds=(15.0, 31.0), step=0.5) == (20.5, 21.5)
    with pytest.raises(logic.RangeError) as err:
        logic.shift_range(width=40, center=70, **_F_BOUNDS)
    assert err.value.reason == logic.RANGE_TOO_WIDE


_F_BOUNDS = {"bounds": (59.0, 88.0), "step": 1.0}


def test_seeding_at_the_minimum_gap_around_the_target():
    assert logic.shift_range(width=2.0, center=70.0, **_F_BOUNDS) == (69.0, 71.0)
    assert logic.shift_range(width=1.0, center=21.0, bounds=(15.0, 31.0), step=0.5) == (20.5, 21.5)
    assert logic.shift_range(width=2.0, center=59.0, **_F_BOUNDS) == (59.0, 61.0)


def test_snapped_midpoint_uses_the_tile_snap():
    assert logic.snapped_midpoint(low=68, high=74, step=1.0) == 71
    # Exact halves go to the even step, as the tile's round() does.
    assert logic.snapped_midpoint(low=68, high=73, step=1.0) == 70  # 70.5
    assert logic.snapped_midpoint(low=69, high=74, step=1.0) == 72  # 71.5
    assert logic.snapped_midpoint(low=20.0, high=21.5, step=0.5) == 21.0  # 20.75: 41.5 steps -> 42
    assert logic.snapped_midpoint(low=20.5, high=21.5, step=0.5) == 21.0
