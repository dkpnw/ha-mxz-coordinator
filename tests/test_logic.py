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
        demands=[pd, sd],
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


# --- N-zone shared-mode (priority-ordered demands) -------------------------------
def test_nzone_standoff_highest_priority_wins():
    # zone2 wants heat, zone5 wants cool; zone0/1 neutral -> first CALLING zone wins.
    assert logic.shared_mode(
        demands=[NEUTRAL, NEUTRAL, HEAT, NEUTRAL, NEUTRAL, COOL],
        current=COOL, allowed=True,
    ) == HEAT


def test_nzone_single_caller_flips():
    assert logic.shared_mode(
        demands=[NEUTRAL] * 5 + [HEAT], current=COOL, allowed=True
    ) == HEAT


def test_nzone_hysteresis_blocks_flip():
    assert logic.shared_mode(
        demands=[NEUTRAL, NEUTRAL, HEAT, NEUTRAL, NEUTRAL, COOL],
        current=COOL, allowed=False,
    ) == COOL


def test_nzone_still_serving_blocks_minority_flip():
    # current=cool still has a cool caller; a lower-priority heat caller can't flip
    # unless it's a standoff won by priority — here priority-0 calls COOL, so cool wins.
    assert logic.shared_mode(
        demands=[COOL, NEUTRAL, HEAT], current=COOL, allowed=True
    ) == COOL


def test_nzone_all_neutral_rests():
    assert logic.shared_mode(demands=[NEUTRAL] * 6, current=HEAT, allowed=True) == HEAT
    assert logic.shared_mode(
        demands=[NEUTRAL] * 6, current=HEAT, allowed=True, resting=COOL
    ) == COOL


# --- engage latch (run-to-target hysteresis) ------------------------------------
def latched(temp, target, prior, **kw):
    return logic.engage_with_latch(
        prior=prior, band=D, neutral=SAT,
        temp=temp, target=target, enabled=True, eco=False, sensor_ok=True,
        eco_cool_max=78.0, eco_heat_min=50.0, **kw,
    )


def test_latch_fresh_needs_full_band():
    assert latched(63.5, 63, None) == SAT   # within band, not engaged -> coast
    assert latched(64.5, 63, None) == COOL  # past band -> engage


def test_latch_runs_all_the_way_to_target():
    # engaged-cooling continues INSIDE the band until the target is crossed
    assert latched(63.5, 63, COOL) == COOL
    assert latched(63.1, 63, COOL) == COOL
    assert latched(63.0, 63, COOL) == SAT   # reached -> disengage, coast


def test_latch_never_whiplashes_on_overshoot():
    assert latched(62.5, 63, COOL) == SAT   # overshot past target -> coast, not heat
    assert latched(63.5, 63, HEAT) == SAT   # mirror


def test_latch_heat_mirror():
    assert latched(62.5, 63, None) == SAT
    assert latched(61.5, 63, None) == HEAT
    assert latched(62.9, 63, HEAT) == HEAT  # runs up to target
    assert latched(63.0, 63, HEAT) == SAT


def test_latch_lockout_disengages_mid_run():
    # heat-lockout flips on while engaged-heating above the floor -> coast
    assert latched(62.0, 63, HEAT, heat_lockout=True, heat_lockout_floor=58.0) == SAT
    # but below the safety floor the run continues
    assert latched(57.0, 63, HEAT, heat_lockout=True, heat_lockout_floor=58.0) == HEAT


def test_latch_disable_still_wins():
    assert logic.engage_with_latch(
        prior=COOL, band=D, neutral=SAT,
        temp=70, target=63, enabled=False, eco=False, sensor_ok=True,
        eco_cool_max=78.0, eco_heat_min=50.0,
    ) == OFF


def test_latch_cool_lockout_disengages_mid_run():
    # cool-lockout mirror: flips on while engaged-cooling below the ceiling -> coast
    assert latched(63.5, 63, COOL, cool_lockout=True, cool_lockout_ceiling=80.0) == SAT
    # but above the safety ceiling the run continues
    assert latched(81.0, 63, COOL, cool_lockout=True, cool_lockout_ceiling=80.0) == COOL


def test_latch_sensor_dropout_disengages_mid_run():
    # a puck dropout mid-run coasts (safe) instead of running blind to target
    assert logic.engage_with_latch(
        prior=COOL, band=D, neutral=SAT,
        temp=63.5, target=63, enabled=True, eco=False, sensor_ok=False,
        eco_cool_max=78.0, eco_heat_min=50.0,
    ) == SAT


def test_latch_eco_bypasses_mid_run():
    # eco flips on mid-run: the protection extremes are stateless — inside them
    # the room coasts even though it was latched, at them it calls regardless.
    def eco_latched(temp):
        return logic.engage_with_latch(
            prior=COOL, band=D, neutral=SAT,
            temp=temp, target=63, enabled=True, eco=True, sensor_ok=True,
            eco_cool_max=78.0, eco_heat_min=50.0,
        )

    assert eco_latched(63.5) == SAT   # latched, but eco band says coast
    assert eco_latched(79.0) == COOL  # above the protection extreme -> cool
    assert eco_latched(49.0) == HEAT  # below -> heat, latch direction irrelevant


def test_latch_metric_band_symmetry():
    # °C profile (band 0.5): same latch semantics at metric scale
    def latched_c(temp, prior):
        return logic.engage_with_latch(
            prior=prior, band=0.5, neutral=SAT,
            temp=temp, target=20.0, enabled=True, eco=False, sensor_ok=True,
            eco_cool_max=25.5, eco_heat_min=10.0,
        )

    assert latched_c(20.3, None) == SAT   # inside 0.5 band, fresh -> coast
    assert latched_c(20.6, None) == COOL  # past band -> engage
    assert latched_c(20.3, COOL) == COOL  # latched -> runs to 20.0
    assert latched_c(20.0, COOL) == SAT   # reached -> coast
    assert latched_c(19.7, HEAT) == HEAT  # heat mirror runs up
    assert latched_c(19.6, COOL) == SAT   # overshoot never whiplashes


# --- coast offset (configurable run-past-target) --------------------------------
def latched_coast(temp, target, prior, coast, **kw):
    return logic.engage_with_latch(
        prior=prior, band=D, neutral=SAT, coast_past=coast,
        temp=temp, target=target, enabled=True, eco=False, sensor_ok=True,
        eco_cool_max=78.0, eco_heat_min=50.0, **kw,
    )


def test_coast_offset_runs_past_target():
    # coast 0.5: engaged cool keeps running THROUGH the target down to 62.5
    assert latched_coast(63.0, 63, COOL, 0.5) == COOL
    assert latched_coast(62.6, 63, COOL, 0.5) == COOL
    assert latched_coast(62.5, 63, COOL, 0.5) == SAT  # banked the margin -> coast


def test_coast_offset_heat_mirror():
    assert latched_coast(63.0, 63, HEAT, 0.5) == HEAT
    assert latched_coast(63.5, 63, HEAT, 0.5) == SAT


def test_coast_offset_zero_is_run_to_target():
    assert latched_coast(63.0, 63, COOL, 0.0) == SAT
    assert latched_coast(63.1, 63, COOL, 0.0) == COOL


def test_coast_offset_negative_stops_short():
    # -0.5 (stop short): engaged cool disengages half a degree above target
    assert latched_coast(63.6, 63, COOL, -0.5) == COOL
    assert latched_coast(63.5, 63, COOL, -0.5) == SAT


def test_coast_offset_does_not_change_fresh_engage():
    # re-engagement still requires the full deadband regardless of offset
    assert latched_coast(63.9, 63, None, 0.5) == SAT
    assert latched_coast(64.1, 63, None, 0.5) == COOL


def test_coast_offset_overshoot_never_whiplashes():
    # past the coast point (target - coast) the room coasts, never flips to heat
    assert latched_coast(62.0, 63, COOL, 0.5) == SAT
    assert latched_coast(64.0, 63, HEAT, 0.5) == SAT


def test_coast_offset_lockouts_stay_absolute():
    # The shift moves only the target comparison — the lockout floor/ceiling
    # still gate on the room's ABSOLUTE temperature, mid-coast-past included.
    # cool-lockout flips on while banking margin below target -> coast
    assert latched_coast(
        62.8, 63, COOL, 0.5, cool_lockout=True, cool_lockout_ceiling=80.0
    ) == SAT
    # but above the safety ceiling the run continues, offset or not
    assert latched_coast(
        81.0, 63, COOL, 0.5, cool_lockout=True, cool_lockout_ceiling=80.0
    ) == COOL
    # heat mirror: lockout mid-coast-past -> coast; below the floor -> keep heating
    assert latched_coast(
        63.2, 63, HEAT, 0.5, heat_lockout=True, heat_lockout_floor=58.0
    ) == SAT
    assert latched_coast(
        57.0, 63, HEAT, 0.5, heat_lockout=True, heat_lockout_floor=58.0
    ) == HEAT
