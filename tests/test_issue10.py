"""Issue #10: a global clamp_max/clamp_min can exceed an individual head's
hardware band. On a °C-native head (CN105/ESPHome) whose native max is 26.0 °C,
a °F-domain target of 79 °F converts to 26.11 °C and is REJECTED by HA's
set_temperature validator — even though the head REPORTS max_temp = 79 °F
(78.8 rounded up for display). The coordinator must clamp each outgoing setpoint
to the head's own native band (rounded toward the safe interior) so it lands on
a value the head actually accepts, and the UI-facing bounds must be narrowed the
same way so a rejectable target can't be entered in the first place.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import UnitOfTemperature  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.util.unit_conversion import TemperatureConverter  # noqa: E402
from homeassistant.util.unit_system import METRIC_SYSTEM  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (  # noqa: E402
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)

from .test_drive import MockHeadC  # noqa: E402
from .test_single_setpoint import (  # noqa: E402
    MockSingleSetpointHead,
    _eid,
    _enable,
    _set_temp,
    _setup,
)


class CelsiusSingleHead(MockSingleSetpointHead):
    """A °C-native single-setpoint head: native band 10.0–26.0 °C.

    In a °F system HA reports max_temp = 79 °F (78.8 rounded up) but rejects
    79 °F, because it converts to 26.11 °C > the 26.0 °C native max.
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 10.0
    _attr_max_temp = 26.0
    _attr_target_temperature_step = 0.5

    def __init__(self, suffix: str) -> None:
        super().__init__(suffix)
        self._attr_target_temperature = 24.0  # a sane native seed


class CelsiusRangeHead(MockHeadC):
    """A °C-native DUAL-setpoint head with the same 10.0–26.0 °C native band."""

    _attr_min_temp = 10.0
    _attr_max_temp = 26.0
    _attr_target_temperature_step = 0.5


async def test_cool_high_edge_clamped_to_native_max(hass: HomeAssistant) -> None:
    """Cool target above the head's native ceiling -> lands on the ceiling, no error.

    The reporter's exact case: 79 °F cool edge on a 26.0 °C head. Pre-fix this
    raised ServiceValidationError every cycle (zone degraded). Post-fix the head
    is commanded 78 °F (= 25.56 °C, safely under 26.0) and runs.
    """
    heads = [CelsiusSingleHead("a"), CelsiusSingleHead("b")]
    entry = await _setup(hass, heads)
    await _enable(hass, entry)
    coord = entry.runtime_data

    # A target above the head's true ceiling can still arrive (legacy/restore/
    # config), so the outgoing clamp must handle it even with the UI bound in
    # place. Drive the room hot so the zone actively cools toward that target.
    coord.zones[0].target = 79.0
    coord.reset_engage_latch(coord.zones[0].slug)
    await _set_temp(hass, "sensor.room_a_temp", 82)
    await coord.async_refresh()
    await hass.async_block_till_done()

    a = hass.states.get(heads[0].entity_id)
    assert a.state == "cool"
    # 79 °F would have been rejected; the head was given the safe 78 °F instead.
    assert a.attributes["temperature"] == 78
    assert coord.last_update_success is True
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "cool"


async def test_heat_low_edge_clamped_to_native_min(hass: HomeAssistant) -> None:
    """Heat target below the head's native floor -> lands on the floor, no error."""
    heads = [CelsiusSingleHead("a"), CelsiusSingleHead("b")]
    # Raise the native floor so a plausible °F target lands under it:
    # 17.0 °C = 62.6 °F, so a 62 °F heat edge (16.67 °C) would be rejected.
    for h in heads:
        h._attr_min_temp = 17.0
    entry = await _setup(hass, heads)
    await _enable(hass, entry)
    coord = entry.runtime_data
    coord._last_mode_change_ts = 0.0  # allow the cool->heat flip

    coord.zones[0].target = 62.0
    coord.reset_engage_latch(coord.zones[0].slug)
    await _set_temp(hass, "sensor.room_a_temp", 55)  # well below target -> heat
    await coord.async_refresh()
    await hass.async_block_till_done()

    a = hass.states.get(heads[0].entity_id)
    assert a.state == "heat"
    # 62 °F (16.67 °C) would have been rejected; floor-snapped to 63 °F (17.22 °C).
    assert a.attributes["temperature"] == 63
    assert coord.last_update_success is True


async def test_dual_setpoint_range_edges_clamped(hass: HomeAssistant) -> None:
    """A RANGE head gets both edges clamped into its native band (no error)."""
    heads = [CelsiusRangeHead("a"), CelsiusRangeHead("b")]
    entry = await _setup(hass, heads)
    await _enable(hass, entry)
    coord = entry.runtime_data

    coord.zones[0].target = 79.0
    coord.reset_engage_latch(coord.zones[0].slug)
    await _set_temp(hass, "sensor.room_a_temp", 82)
    await coord.async_refresh()
    await hass.async_block_till_done()

    a = hass.states.get(heads[0].entity_id)
    assert a.state == "cool"
    # high = target 79 -> clamped to the 78 °F ceiling; low = 79-2 = 77 (in band).
    assert a.attributes["target_temp_high"] == 78
    assert a.attributes["target_temp_low"] == 77
    assert coord.last_update_success is True


async def test_ui_bounds_narrowed_to_head_band(hass: HomeAssistant) -> None:
    """The number entity + thermostat facade advertise the head-narrowed range.

    clamp band 59..88 °F intersected with the head's 50..78 °F accept-able band
    (10.0 °C -> 50 °F floor-up, 26.0 °C -> 78 °F ceiling-down) = 59..78, so no
    entry point (UI/HomeKit/Google/voice) can offer the rejectable 79 °F.
    """
    heads = [CelsiusSingleHead("a"), CelsiusSingleHead("b")]
    entry = await _setup(hass, heads)

    num = hass.states.get(_eid(hass, entry, "_primary_target"))
    assert num.attributes["min"] == 59
    assert num.attributes["max"] == 78

    tile = hass.states.get(_eid(hass, entry, "_primary_thermostat"))
    assert tile.attributes["min_temp"] == 59
    assert tile.attributes["max_temp"] == 78


async def test_safe_band_round_trips_at_float_boundary(hass: HomeAssistant) -> None:
    """A snapped edge must survive the EXACT conversion HA's validator performs.

    A °F-native head with a fractional native max of 78.8 °F in a °C system:
    convert(78.8 °F → °C) = 25.999999999999996, and the snap epsilon promotes it
    to 26.0 °C — which converts BACK to 78.800…01 °F > 78.8 and would still be
    rejected, a float-ulp past the true ceiling. The band must round-trip through
    HA's own converter and land one step inward at 25.5 °C.
    """

    class FractionalFahrenheitHead(MockSingleSetpointHead):
        _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
        _attr_min_temp = 40.0
        _attr_max_temp = 78.8

    heads = [FractionalFahrenheitHead("a"), FractionalFahrenheitHead("b")]
    hass.config.units = METRIC_SYSTEM

    async def _climate(hass, config, async_add_entities, discovery_info=None):  # noqa: ANN001
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=_climate))
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: heads[0].entity_id,
            CONF_SECONDARY_CLIMATE: heads[1].entity_id,
            CONF_PRIMARY_SENSOR: "sensor.room_a_temp",
            CONF_SECONDARY_SENSOR: "sensor.room_b_temp",
        },
    )
    entry.add_to_hass(hass)
    await _set_temp(hass, "sensor.room_a_temp", 21)
    await _set_temp(hass, "sensor.room_b_temp", 21)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coord = entry.runtime_data
    band = coord._head_safe_band(heads[0].entity_id)
    assert band is not None
    lo_c, hi_c = band
    # One step inward of the 1-ulp-overshooting 26.0 °C.
    assert hi_c == 25.5
    # Both edges survive the validator's conversion back into the native unit.
    assert (
        TemperatureConverter.convert(
            hi_c, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
        )
        <= heads[0].max_temp
    )
    assert (
        TemperatureConverter.convert(
            lo_c, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
        )
        >= heads[0].min_temp
    )


async def test_number_bounds_track_the_head_live(hass: HomeAssistant) -> None:
    """The target number's bounds follow the head WITHOUT a reload.

    Regression for the init-frozen edge: bounds were computed once when the
    number entity was created, so a head whose integration loaded AFTER ours
    kept the wide [clamp_min, clamp_max] fallback until a reload — and a
    later change to the head's own limits was never picked up. The bounds
    are live properties now: validation always checks the head's real band.
    """
    heads = [CelsiusSingleHead("a"), CelsiusSingleHead("b")]
    await _setup(hass, heads)

    reg_entity = None
    component = hass.data["entity_components"]["number"]
    for ent in component.entities:
        if ent.unique_id.endswith("_primary_target"):
            reg_entity = ent
            break
    assert reg_entity is not None
    assert reg_entity.native_max_value == 78  # head-narrowed (26.0 C)

    # The head's band changes in place (firmware update, config change) —
    # the number's bounds must follow with no reload.
    heads[0]._attr_max_temp = 24.0  # 75.2 F -> floor to 75
    assert reg_entity.native_max_value == 75
    heads[0]._attr_max_temp = 26.0
    assert reg_entity.native_max_value == 78
