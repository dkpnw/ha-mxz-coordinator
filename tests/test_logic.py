"""Truth-table tests for the pure decision math (no Home Assistant required).

These port the 12-case validation the YAML package was signed off against, plus the
setpoint/action edge math. ``logic.py`` is loaded directly from disk so this file runs
on a bare ``pytest`` without installing Home Assistant.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

# --- load custom_components/mxz_coordinator/{const,logic}.py as a tiny package ---
_PKG_DIR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "mxz_coordinator"
)
_pkg = types.ModuleType("_mxzc")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules["_mxzc"] = _pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_mxzc.{name}", _PKG_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"_mxzc.{name}"] = module
    spec.loader.exec_module(module)
    return module


const = _load("const")
logic = _load("logic")

COOL, HEAT, OFF = const.MODE_COOL, const.MODE_HEAT, const.MODE_OFF
FAN, NEUTRAL, SAT = const.MODE_FAN_ONLY, const.DEMAND_NEUTRAL, const.ENGAGE_SATISFIED
S, D = const.DEFAULT_DEMAND_THRESHOLD, const.DEFAULT_ENGAGE_DEADBAND


def demand(
    temp, target, *, enabled=True, eco=False, sensor_ok=True,
    heat_lockout=False, heat_lockout_floor=58.0,
    cool_lockout=False, cool_lockout_ceiling=80.0,
):
    return logic.room_call(
        temp=temp, target=target, enabled=enabled, eco=eco, sensor_ok=sensor_ok,
        band=S, eco_cool_max=78.0, eco_heat_min=50.0, neutral=NEUTRAL,
        heat_lockout=heat_lockout, heat_lockout_floor=heat_lockout_floor,
        cool_lockout=cool_lockout, cool_lockout_ceiling=cool_lockout_ceiling,
    )


def engage(temp, target, *, enabled=True, eco=False, sensor_ok=True):
    return logic.room_call(
        temp=temp, target=target, enabled=enabled, eco=eco, sensor_ok=sensor_ok,
        band=D, eco_cool_max=78.0, eco_heat_min=50.0, neutral=SAT,
    )


# --- per-room demand (S = 3 °F) -------------------------------------------------
def test_demand_disabled_is_off():
    assert demand(90, 70, enabled=False) == OFF


def test_demand_above_band_is_cool():
    assert demand(74, 70) == COOL  # 74 > 70 + 3


def test_demand_below_band_is_heat():
    assert demand(66, 70) == HEAT  # 66 < 70 - 3


def test_demand_within_band_is_neutral():
    assert demand(72, 70) == NEUTRAL
    assert demand(73, 70) == NEUTRAL  # exactly target + S is not strictly greater


def test_demand_sensor_dropout_fails_safe_to_neutral():
    assert demand(90, 70, sensor_ok=False) == NEUTRAL


def test_demand_eco_extremes():
    assert demand(79, 70, eco=True) == COOL  # > 78
    assert demand(49, 70, eco=True) == HEAT  # < 50
    assert demand(70, 70, eco=True) == NEUTRAL


# --- heat lockout (summer passive-solar) ---------------------------------------
def test_heat_lockout_suppresses_heat_above_floor():
    # 66 is 4 °F below target (would normally HEAT) but above the 58 floor -> idle.
    assert demand(66, 70, heat_lockout=True, heat_lockout_floor=58.0) == NEUTRAL


def test_heat_lockout_still_heats_below_floor():
    # Genuinely cold: below the safety floor -> heat regardless of the lockout.
    assert demand(57, 70, heat_lockout=True, heat_lockout_floor=58.0) == HEAT


def test_heat_lockout_does_not_touch_cooling():
    # Cooling is unaffected by the heat lockout.
    assert demand(74, 70, heat_lockout=True, heat_lockout_floor=58.0) == COOL


def test_heat_lockout_off_heats_normally():
    # Default (unlocked) behaviour is unchanged.
    assert demand(66, 70, heat_lockout=False) == HEAT


# --- cool lockout (winter, mirror of heat lockout) -----------------------------
def test_cool_lockout_suppresses_cool_below_ceiling():
    # 74 is 4 °F above target (would normally COOL) but below the 80 ceiling -> idle.
    assert demand(74, 70, cool_lockout=True, cool_lockout_ceiling=80.0) == NEUTRAL


def test_cool_lockout_still_cools_above_ceiling():
    # Genuinely hot: above the safety ceiling -> cool regardless of the lockout.
    assert demand(82, 70, cool_lockout=True, cool_lockout_ceiling=80.0) == COOL


def test_cool_lockout_does_not_touch_heating():
    # Heating is unaffected by the cool lockout.
    assert demand(66, 70, cool_lockout=True, cool_lockout_ceiling=80.0) == HEAT


def test_cool_lockout_off_cools_normally():
    assert demand(74, 70, cool_lockout=False) == COOL


def test_both_lockouts_idle_the_middle_band():
    # With both locked, a room just off target in either direction idles; the
    # safety floor/ceiling still act at the extremes.
    assert demand(66, 70, heat_lockout=True, cool_lockout=True) == NEUTRAL  # 4 below
    assert demand(74, 70, heat_lockout=True, cool_lockout=True) == NEUTRAL  # 4 above
    assert demand(57, 70, heat_lockout=True, cool_lockout=True,
                  heat_lockout_floor=58.0) == HEAT  # below floor
    assert demand(82, 70, heat_lockout=True, cool_lockout=True,
                  cool_lockout_ceiling=80.0) == COOL  # above ceiling


# --- seasonal changeover from local weather ------------------------------------
def sl(high, *, heat_above=68.0, cool_below=50.0):
    return logic.season_lockouts(
        outdoor_high=high, heat_above=heat_above, cool_below=cool_below
    )


def test_changeover_warm_season_locks_heat():
    assert sl(85) == (True, False)  # hot day -> suppress heat
    assert sl(68) == (True, False)  # exactly at the threshold


def test_changeover_cold_season_locks_cool():
    assert sl(40) == (False, True)  # cold day -> suppress cool
    assert sl(50) == (False, True)  # exactly at the threshold


def test_changeover_shoulder_season_locks_neither():
    assert sl(60) == (False, False)  # spring/fall -> normal heat + cool
    assert sl(51) == (False, False)
    assert sl(67) == (False, False)


def test_changeover_no_signal_is_safe():
    assert sl(None) == (False, False)  # weather unavailable -> no lockout


def test_changeover_season_sweep():
    # Walk a year of forecast highs and assert the lockout pair each step.
    sweep = [
        (30, (False, True)),   # deep winter -> cool locked
        (48, (False, True)),   # late winter
        (58, (False, False)),  # spring shoulder -> both free
        (72, (True, False)),   # early summer -> heat locked
        (92, (True, False)),   # midsummer
        (64, (False, False)),  # fall shoulder
        (44, (False, True)),   # back to winter
    ]
    for high, expected in sweep:
        assert sl(high) == expected, f"high={high}"


# --- per-room engage (D = 1 °F) -------------------------------------------------
def test_engage_satisfied_within_deadband():
    assert engage(70.5, 70) == SAT


def test_engage_runs_outside_deadband():
    assert engage(71.5, 70) == COOL
    assert engage(68.5, 70) == HEAT


def test_engage_dropout_is_satisfied():
    assert engage(90, 70, sensor_ok=False) == SAT


# --- shared-mode selection (the 12-case heart) ---------------------------------
def sm(pd, sd, current=COOL, allowed=True, resting=None):
    return logic.shared_mode(
        primary_demand=pd,
        secondary_demand=sd,
        current=current,
        allowed=allowed,
        resting=resting,
    )


def test_both_neutral_holds_resting_mode():
    assert sm(NEUTRAL, NEUTRAL, current=HEAT) == HEAT
    assert sm(NEUTRAL, NEUTRAL, current=COOL) == COOL


def test_resting_bias_pins_idle_mode():
    # No demand: a cool bias settles to cool even though the last mode was heat
    # (after hysteresis), and a heat bias settles to heat from a cool resting mode.
    assert sm(NEUTRAL, NEUTRAL, current=HEAT, resting=COOL) == COOL
    assert sm(NEUTRAL, NEUTRAL, current=COOL, resting=HEAT) == HEAT
    # Bias matching the current mode is a no-op.
    assert sm(NEUTRAL, NEUTRAL, current=COOL, resting=COOL) == COOL


def test_resting_bias_none_keeps_last_mode():
    # resting=None (or any non cool|heat value) -> original last-mode behavior.
    assert sm(NEUTRAL, NEUTRAL, current=HEAT, resting=None) == HEAT
    assert sm(NEUTRAL, NEUTRAL, current=HEAT, resting="last") == HEAT


def test_resting_bias_does_not_block_real_demand():
    # A genuine opposite demand still flips the mode regardless of the bias.
    assert sm(HEAT, NEUTRAL, current=COOL, resting=COOL) == HEAT
    assert sm(NEUTRAL, COOL, current=HEAT, resting=HEAT) == COOL


def test_resting_bias_still_gated_by_hysteresis():
    # The bias flip is gated by hysteresis like any other flip.
    assert sm(NEUTRAL, NEUTRAL, current=HEAT, resting=COOL, allowed=False) == HEAT


def test_single_room_cool():
    assert sm(COOL, NEUTRAL, current=HEAT) == COOL


def test_single_room_heat():
    assert sm(NEUTRAL, HEAT, current=COOL) == HEAT


def test_both_cool_and_both_heat():
    assert sm(COOL, COOL, current=HEAT) == COOL
    assert sm(HEAT, HEAT, current=COOL) == HEAT


def test_standoff_primary_wins_cool():
    # primary cool, secondary heat -> primary wins -> cool
    assert sm(COOL, HEAT, current=HEAT) == COOL


def test_standoff_primary_wins_heat():
    assert sm(HEAT, COOL, current=COOL) == HEAT


def test_disabled_primary_lets_secondary_drive():
    # primary off, secondary cool -> not a standoff -> cool
    assert sm(OFF, COOL, current=HEAT) == COOL


def test_hysteresis_blocks_flip_when_not_allowed():
    # secondary wants heat while resting in cool, but hysteresis not elapsed
    assert sm(NEUTRAL, HEAT, current=COOL, allowed=False) == COOL


def test_hysteresis_does_not_block_standoff():
    # in a standoff the primary tiebreak applies even under hysteresis... no:
    # the YAML gates ALL flips on `allowed`; a standoff flip is still blocked.
    assert sm(HEAT, COOL, current=COOL, allowed=False) == COOL


def test_stuck_in_current_mode_does_not_flip():
    # current cool, secondary still wants cool AND primary wants heat -> standoff,
    # primary wins -> heat (allowed). But if only "still cool" with no opposing
    # demand, stay.
    assert sm(NEUTRAL, COOL, current=COOL) == COOL


# --- setpoints (clamped [59, 88]) ----------------------------------------------
def sp(mode, target, *, eco=False):
    return logic.setpoints(
        mode=mode, target=target, eco=eco, clamp_min=59, clamp_max=88
    )


def test_cool_setpoints():
    assert sp(COOL, 70) == (68, 70)  # low = target - 2, high = target


def test_heat_setpoints():
    assert sp(HEAT, 70) == (70, 72)  # low = target, high = target + 2


def test_setpoints_clamped_low():
    assert sp(COOL, 60) == (59, 60)  # target - 2 = 58 -> clamped to 59


def test_setpoints_clamped_high():
    assert sp(HEAT, 88) == (88, 88)  # target + 2 = 90 -> clamped to 88


def test_eco_setpoints():
    assert sp(COOL, 70, eco=True) == (76, 78)
    assert sp(HEAT, 70, eco=True) == (59, 61)


# --- head action ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("engage_state", "mode", "eco", "expected"),
    [
        (OFF, COOL, False, OFF),
        (SAT, COOL, False, FAN),  # satisfied -> idle in fan_only
        (SAT, COOL, True, OFF),  # eco-satisfied -> off
        (COOL, COOL, False, COOL),  # wants the shared mode -> runs
        (HEAT, COOL, False, FAN),  # standoff loser -> fan_only
    ],
)
def test_head_action(engage_state, mode, eco, expected):
    assert logic.head_action(engage=engage_state, mode=mode, eco=eco) == expected


# --- fan boost: delta -> ladder INDEX (pure) -----------------------------------
UP_AT = const.FAN_BOOST_UP_AT       # (1.0, 2.0, 3.0, 4.0)
DOWN_AT = const.FAN_BOOST_DOWN_AT   # (0.5, 1.5, 2.5, 3.5)


def fan(delta, cur, *, max_idx=4):
    return logic.fan_for_delta(
        delta=delta, cur_idx=cur, up_at=UP_AT, down_at=DOWN_AT, max_idx=max_idx
    )


def test_fan_big_delta_goes_max():
    # delta 5 from idle climbs all the way to the top rung (index 4 = "high").
    assert fan(5.0, 0) == 4


@pytest.mark.parametrize(
    ("delta", "expected"),
    [(4.5, 4), (3.6, 4), (3.4, 3)],  # from the top rung, hysteresis holds until < 3.5
)
def test_fan_hysteresis_from_top(delta, expected):
    assert fan(delta, 4) == expected


def test_fan_full_step_down_chain():
    # Starting at the top, walk the delta down; the fan eases one rung at a time.
    expected = [4, 4, 3, 2, 1, 0]
    deltas = [5.0, 4.5, 3.4, 2.4, 1.4, 0.4]
    cur = 4
    got = []
    for d in deltas:
        cur = fan(d, cur)
        got.append(cur)
    assert got == expected


def test_fan_max_idx_clamp():
    # A large delta cannot climb past the configured max rung.
    assert fan(9.0, 0, max_idx=2) == 2


@pytest.mark.parametrize("cur", [0])
def test_fan_monotonic_non_decreasing_in_delta(cur):
    prev = -1
    for tenths in range(0, 61):  # delta 0.0 .. 6.0
        idx = fan(tenths / 10.0, cur)
        assert idx >= prev
        prev = idx
