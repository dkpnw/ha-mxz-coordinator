"""Unit-system tests: the °F profile is unchanged; the °C profile gives clean
metric defaults, a 1° setpoint band, metric eco edges, and 0.5° resolution."""

from __future__ import annotations

from custom_components.mxz_coordinator import logic
from custom_components.mxz_coordinator.const import (
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    CONF_DEMAND_THRESHOLD,
    CONF_ENGAGE_DEADBAND,
    MODE_COOL,
    MODE_HEAT,
    unit_profile,
)

COOL, HEAT = MODE_COOL, MODE_HEAT


# --- unit_profile --------------------------------------------------------------
def test_fahrenheit_profile_matches_legacy():
    p = unit_profile(False)
    assert p["target_default"] == 70.0
    assert p["target_step"] == 1.0
    assert p["setpoint_band"] == 2.0
    assert p["eco_cool"] == (76.0, 78.0)
    assert p["eco_heat"] == (59.0, 61.0)
    d = p["defaults"]
    assert d[CONF_DEMAND_THRESHOLD] == 3.0
    assert d[CONF_ENGAGE_DEADBAND] == 1.0
    assert d[CONF_CLAMP_MIN] == 59
    assert d[CONF_CLAMP_MAX] == 88


def test_celsius_profile_is_clean_metric():
    p = unit_profile(True)
    assert p["target_default"] == 21.0
    assert p["target_step"] == 0.5
    assert p["setpoint_band"] == 1.0
    assert p["eco_cool"] == (24.0, 26.0)
    assert p["eco_heat"] == (15.0, 16.0)
    d = p["defaults"]
    assert d[CONF_DEMAND_THRESHOLD] == 1.5
    assert d[CONF_ENGAGE_DEADBAND] == 0.5
    assert d[CONF_CLAMP_MIN] == 15
    assert d[CONF_CLAMP_MAX] == 31


# --- setpoints in °C (band=1, step=0.5, metric eco edges) ----------------------
def spc(mode, target, *, eco=False):
    return logic.setpoints(
        mode=mode, target=target, eco=eco, clamp_min=15, clamp_max=31,
        band=1.0, step=0.5, eco_cool=(24.0, 26.0), eco_heat=(15.0, 16.0),
    )


def test_celsius_cool_setpoints():
    assert spc(COOL, 21.0) == (20.0, 21.0)  # low = target - 1 band


def test_celsius_heat_setpoints():
    assert spc(HEAT, 21.0) == (21.0, 22.0)  # high = target + 1 band


def test_celsius_setpoints_clamped():
    assert spc(COOL, 15.0) == (15.0, 15.0)  # target-1=14 -> clamp 15
    assert spc(HEAT, 31.0) == (31.0, 31.0)  # target+1=32 -> clamp 31


def test_celsius_half_degree_resolution():
    # A 21.3° target snaps to the 0.5° grid, not truncated to a whole degree.
    assert spc(COOL, 21.3) == (20.5, 21.5)


def test_celsius_eco_edges():
    assert spc(COOL, 21.0, eco=True) == (24.0, 26.0)
    assert spc(HEAT, 21.0, eco=True) == (15.0, 16.0)


def test_setpoints_default_args_stay_fahrenheit():
    # No band/step/eco passed -> the legacy °F behavior (2° band, whole degrees).
    assert logic.setpoints(
        mode=COOL, target=70, eco=False, clamp_min=59, clamp_max=88
    ) == (68, 70)
