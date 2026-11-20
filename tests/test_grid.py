"""grid.py called directly: no pytest-homeassistant.

Each expectation comes from the rule it states (a band edge snaps toward the head's interior and
must round-trip, a setpoint compares on the head's own grid, a mode change on a head that is not
ESPHome is its own call), worked out by hand from the numbers. Every head and value is invented.
The one comparison with Home Assistant is ``convert`` against its ``TemperatureConverter``, on
whichever release this suite runs.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.util.unit_conversion import TemperatureConverter

from custom_components.mxz_coordinator.grid import Call, Grid, convert, local_calls

C, F = "°C", "°F"


def _grid(**change: Any) -> Grid:
    return replace(Grid(system_unit=F, target_step=1.0, clamp_min=59.0, clamp_max=88.0), **change)


# -- the unit conversion --------------------------------------------------------------------


@pytest.mark.parametrize("value", [-40.0, 0.0, 15.5, 17.78, 18.0, 25.56, 26.0, 30.25, 100.0])
@pytest.mark.parametrize(("source", "target"), [(C, F), (F, C), (C, C), (F, F)])
def test_convert_is_home_assistants_conversion(value, source, target) -> None:
    assert convert(value, source, target) == TemperatureConverter.convert(value, source, target)


def test_convert_by_hand() -> None:
    assert convert(25.0, C, F) == 77.0
    assert convert(212.0, F, C) == 100.0
    assert convert(21.37, C, C) == 21.37


def test_convert_refuses_any_other_unit() -> None:
    with pytest.raises(ValueError, match="no conversion"):
        convert(300.0, "K", C)


# -- the band a head accepts -------------------------------------------------------------


def test_a_same_unit_band_is_its_native_edges() -> None:
    assert _grid(native=(61.0, 85.0, F)).safe_band() == (61.0, 85.0)


def test_a_native_band_snaps_inward_to_our_step() -> None:
    # 16 °C to 26 °C is 60.8 °F to 78.8 °F: the floor goes up to 61, the ceiling down to 78.
    assert _grid(native=(16.0, 26.0, C)).safe_band() == (61.0, 78.0)


def test_an_edge_that_would_round_trip_past_the_native_band_steps_in() -> None:
    # 78.8 °F is 26.000000000000004 °C: on a 0.5 °C step that snaps to 26.0, which converts
    # back past 78.8 °F, so the edge goes one step in. The floor round-trips and stays.
    grid = _grid(system_unit=C, target_step=0.5, native=(50.0, 78.8, F))
    assert convert(26.0, C, F) > 78.8
    assert grid.safe_band() == (10.0, 25.5)


def test_a_band_narrower_than_a_step_is_none() -> None:
    assert _grid(native=(70.2, 70.8, F)).safe_band() is None


def test_with_no_native_band_the_shown_band_is_used() -> None:
    assert _grid(shown_band=(60.4, 79.0)).safe_band() == (61.0, 79.0)
    assert _grid(shown_band=(60.0, None)).safe_band() is None
    assert _grid(shown_band=None).safe_band() is None


def test_a_native_band_wins_over_the_shown_one() -> None:
    assert _grid(native=(62.0, 80.0, F), shown_band=(50.0, 90.0)).safe_band() == (62.0, 80.0)


def test_a_zero_target_step_snaps_to_whole_degrees() -> None:
    assert _grid(target_step=0.0, native=(60.5, 80.5, F)).safe_band() == (61.0, 80.0)


def test_a_command_is_clamped_into_the_band_at_both_edges() -> None:
    grid = _grid(native=(62.0, 84.0, F))
    assert grid.clamp(50.0, 90.0) == (62.0, 84.0)
    # Each edge on its own: a whole range below the band, and one above it.
    assert grid.clamp(50.0, 55.0) == (62.0, 62.0)
    assert grid.clamp(86.0, 90.0) == (84.0, 84.0)
    assert grid.clamp(64.0, 70.0) == (64.0, 70.0)
    assert _grid().clamp(50.0, 90.0) == (50.0, 90.0)


def test_target_bounds_are_the_clamp_band_held_to_the_head() -> None:
    assert _grid(native=(62.0, 84.0, F)).target_bounds() == (62.0, 84.0)
    assert _grid(native=(50.0, 95.0, F)).target_bounds() == (59.0, 88.0)
    assert _grid().target_bounds() == (59.0, 88.0)
    # Disjoint from the clamp band: the clamp band.
    assert _grid(native=(90.0, 95.0, F)).target_bounds() == (59.0, 88.0)


# -- the head's own grid ---------------------------------------------------------------------


CN105 = {"native_step": 0.5, "native_unit": C}


def test_a_setpoint_the_head_latched_on_its_grid_is_already_there() -> None:
    # 64 °F is 17.78 °C: the head latches 18.0 °C and shows 64.4 °F.
    grid = _grid(**CN105)
    assert grid.already_at(64.4, 64.0)
    # One native step off (18.5 °C, 65.3 °F) is not.
    assert not grid.already_at(65.3, 64.0)
    assert not grid.already_at(None, 64.0)


def test_a_setpoint_halfway_between_two_grid_steps_latches_half_up() -> None:
    # 21.25 °C on a 0.5 °C grid: the CN105 latches 21.5 (half up), not 21.0 (half to even).
    grid = _grid(system_unit=C, target_step=0.5, native_step=0.5, native_unit=C)
    assert grid.already_at(21.5, 21.25)
    assert not grid.already_at(21.0, 21.25)


@pytest.mark.parametrize("change", [{}, {"native_step": 0.0, "native_unit": C}, {"native_step": 0.5, "native_unit": "K"}])
def test_with_no_usable_native_grid_half_our_step_decides(change) -> None:
    grid = _grid(**change)
    assert grid.already_at(64.4, 64.0)
    assert not grid.already_at(64.5, 64.0)


def test_the_target_for_a_setpoint_is_the_one_the_head_holds_it_for() -> None:
    grid = _grid(**CN105)
    # 18.5 °C shows 65.5 °F; 66 °F would latch 19.0 °C, 65 °F latches 18.5 °C.
    assert grid.target_for_setpoint(65.5) == 65.0
    # Without a grid of its own: the nearest target, half up.
    assert _grid().target_for_setpoint(65.5) == 66.0
    assert _grid(target_step=0.5, system_unit=C).target_for_setpoint(21.3) == 21.5


def test_the_target_for_a_setpoint_can_be_the_step_above() -> None:
    # 78.4 °F is 25.78 °C. 78 °F latches 25.5 °C and 77 °F 25.0 °C, each more than a quarter
    # degree off; 79 °F latches 26.0 °C, 0.22 °C off: the step above holds it.
    assert _grid(**CN105).target_for_setpoint(78.4) == 79.0
    # Without a grid of its own: the nearest target.
    assert _grid().target_for_setpoint(78.4) == 78.0


# -- what a local head is sent -----------------------------------------------------------


RANGE = {"supported_features": 2, "target_temp_low": 68.0, "target_temp_high": 70.0}
SINGLE = {"supported_features": 1, "temperature": 70.0}


def _calls(act: str, low: float = 68.0, high: float = 70.0, *, mode: str | None = "cool",
           attributes: dict[str, Any] | None = None, grid: Grid | None = None,
           combined: bool = True) -> tuple[Call, ...]:
    return local_calls(
        act, low, high, mode=mode, attributes=RANGE if attributes is None else attributes,
        grid=grid or _grid(), combined=combined,
    )


def test_a_range_head_already_showing_the_command_is_sent_nothing() -> None:
    assert _calls("cool") == ()


@pytest.mark.parametrize(("low", "high"), [(67.0, 70.0), (68.0, 71.0)])
def test_one_edge_off_sends_both(low, high) -> None:
    assert _calls("cool", low, high) == (
        Call("set_temperature", {"hvac_mode": "cool", "target_temp_low": low, "target_temp_high": high}),
    )


def test_a_mode_change_on_an_esphome_head_is_one_call() -> None:
    assert _calls("heat", mode="fan_only") == (
        Call("set_temperature", {"hvac_mode": "heat", "target_temp_low": 68.0, "target_temp_high": 70.0}),
    )


def test_a_mode_change_on_another_head_is_the_mode_then_the_setpoints() -> None:
    assert _calls("heat", mode="fan_only", combined=False) == (
        Call("set_hvac_mode", {"hvac_mode": "heat"}),
        Call("set_temperature", {"target_temp_low": 68.0, "target_temp_high": 70.0}),
    )


def test_the_same_mode_on_another_head_is_still_one_call() -> None:
    assert _calls("cool", 66.0, 69.0, combined=False) == (
        Call("set_temperature", {"hvac_mode": "cool", "target_temp_low": 66.0, "target_temp_high": 69.0}),
    )


def test_a_command_past_the_band_goes_out_at_its_edges() -> None:
    grid = _grid(native=(62.0, 84.0, F))
    assert _calls("cool", 60.0, 86.0, mode="off", grid=grid) == (
        Call("set_temperature", {"hvac_mode": "cool", "target_temp_low": 62.0, "target_temp_high": 84.0}),
    )


def test_the_skip_compares_on_the_heads_grid() -> None:
    grid = _grid(**CN105)
    # 64.5 °F is 18.06 °C, the 18.0 °C 64 °F latches; on whole °F it is half a degree off.
    shows = {"supported_features": 2, "target_temp_low": 64.5, "target_temp_high": 68.0}
    assert _calls("cool", 64.0, 68.0, attributes=shows, grid=grid) == ()
    assert _calls("cool", 64.0, 68.0, attributes=shows) != ()


def test_a_single_setpoint_head_gets_its_edge_for_the_mode() -> None:
    assert _calls("cool", 66.0, 69.0, mode="off", attributes=SINGLE) == (
        Call("set_temperature", {"hvac_mode": "cool", "temperature": 69.0}),
    )
    assert _calls("heat", 66.0, 69.0, mode="off", attributes=SINGLE, combined=False) == (
        Call("set_hvac_mode", {"hvac_mode": "heat"}), Call("set_temperature", {"temperature": 66.0}),
    )


def test_a_single_setpoint_head_showing_it_is_sent_nothing() -> None:
    assert _calls("cool", 66.0, 70.0, attributes=SINGLE) == ()
    assert _calls("heat", 70.0, 72.0, attributes=SINGLE) != ()


def test_a_head_with_no_state_is_sent_the_mode_and_its_setpoint() -> None:
    assert local_calls("cool", 68.0, 70.0, mode=None, attributes=None, grid=_grid(), combined=False) == (
        Call("set_hvac_mode", {"hvac_mode": "cool"}), Call("set_temperature", {"temperature": 70.0}),
    )


@pytest.mark.parametrize("act", ["off", "fan_only"])
def test_a_park_is_the_mode_alone_and_nothing_once_shown(act) -> None:
    assert _calls(act, mode="cool", combined=False) == (Call("set_hvac_mode", {"hvac_mode": act}),)
    assert _calls(act, mode=act) == ()
