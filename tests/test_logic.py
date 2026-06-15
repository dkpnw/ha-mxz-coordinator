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


def demand(temp, target, *, enabled=True, eco=False, sensor_ok=True):
    return logic.room_call(
        temp=temp, target=target, enabled=enabled, eco=eco, sensor_ok=sensor_ok,
        band=S, eco_cool_max=78.0, eco_heat_min=50.0, neutral=NEUTRAL,
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


# --- per-room engage (D = 1 °F) -------------------------------------------------
def test_engage_satisfied_within_deadband():
    assert engage(70.5, 70) == SAT


def test_engage_runs_outside_deadband():
    assert engage(71.5, 70) == COOL
    assert engage(68.5, 70) == HEAT


def test_engage_dropout_is_satisfied():
    assert engage(90, 70, sensor_ok=False) == SAT


# --- shared-mode selection (the 12-case heart) ---------------------------------
def sm(pd, sd, current=COOL, allowed=True):
    return logic.shared_mode(
        primary_demand=pd, secondary_demand=sd, current=current, allowed=allowed
    )


def test_both_neutral_holds_resting_mode():
    assert sm(NEUTRAL, NEUTRAL, current=HEAT) == HEAT
    assert sm(NEUTRAL, NEUTRAL, current=COOL) == COOL


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
