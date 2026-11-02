"""Issue #6 coverage: single-setpoint heads, per-zone isolation, sane first-enable."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)

from .dwell import expire_dwell, record_mode_change
from .test_drive import MockHead, _eid, _set_temp


class MockSingleSetpointHead(MockHead):
    """A head WITHOUT TARGET_TEMPERATURE_RANGE — accepts only `temperature`.

    Mirrors helicopterrun's units (#6): supported_features 425 = TARGET_TEMPERATURE
    | FAN_MODE | SWING_MODE | TURN_ON | TURN_OFF, and rejects low/high like HA core
    does for entities lacking the range feature.
    """

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_swing_modes: ClassVar[list[str]] = ["auto"]

    def __init__(self, suffix: str) -> None:
        super().__init__(suffix)
        self._attr_native_target_temperature = 66.0
        self._attr_swing_mode = "auto"

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if "target_temp_low" in kwargs or "target_temp_high" in kwargs:
            raise ServiceValidationError(
                "entity does not support Lower/Upper target temperature"
            )
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = mode
        if (temp := kwargs.get("temperature")) is not None:
            self._attr_native_target_temperature = temp
        self.async_write_ha_state()


async def _setup(
    hass: HomeAssistant, heads: list, *, changed_ago: float | None = None
) -> MockConfigEntry:
    """Two heads on one entry, both rooms at 70 °F, nothing enabled.

    ``changed_ago`` stores a mode change that many seconds before the setup,
    as the run before it would have; None (the default) is a fresh install.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM

    async def _climate(hass, config, async_add_entities, discovery_info=None):
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
    await _set_temp(hass, "sensor.room_a_temp", 70)
    await _set_temp(hass, "sensor.room_b_temp", 70)
    if changed_ago is not None:
        await record_mode_change(hass, entry.entry_id, changed_ago)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _enable(hass: HomeAssistant, entry) -> None:
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()


async def test_single_setpoint_head_gets_temperature(hass: HomeAssistant) -> None:
    """A head without RANGE gets a single `temperature`; nothing crashes (#6)."""
    heads = [MockSingleSetpointHead("a"), MockSingleSetpointHead("b")]
    # Keep the mock honest: exactly the reporter's feature mask.
    assert int(heads[0].supported_features) == 425
    entry = await _setup(hass, heads)
    await _enable(hass, entry)

    await _set_temp(hass, "sensor.room_a_temp", 72)  # 66-seeded target -> wants cool
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    a = hass.states.get(heads[0].entity_id)
    assert a.state == "cool"
    # single setpoint == the (clamped) room target; no low/high ever sent
    assert a.attributes["temperature"] == entry.runtime_data.zones[0].target
    # the coordinator stayed alive
    assert entry.runtime_data.last_update_success is True
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "cool"


async def test_one_bad_head_degrades_only_its_zone(hass: HomeAssistant) -> None:
    """A head that rejects every setpoint call degrades alone; the other zone runs."""

    class BrokenHead(MockHead):
        async def async_set_temperature(self, **kwargs: Any) -> None:
            raise ServiceValidationError("nope")

    heads = [BrokenHead("a"), MockHead("b")]
    entry = await _setup(hass, heads)
    await _enable(hass, entry)

    await _set_temp(hass, "sensor.room_a_temp", 75)
    await _set_temp(hass, "sensor.room_b_temp", 75)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    # zone B was still served despite zone A's head erroring
    assert hass.states.get(heads[1].entity_id).state == "cool"
    assert entry.runtime_data.last_update_success is True
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "cool"


async def test_fresh_target_seeds_from_head_setpoint(hass: HomeAssistant) -> None:
    """A fresh install seeds each target from the head's setpoint, not 70 (#6)."""
    heads = [MockSingleSetpointHead("a"), MockSingleSetpointHead("b")]  # setpoint 66
    entry = await _setup(hass, heads)
    assert entry.runtime_data.zones[0].target == 66.0
    assert float(hass.states.get(_eid(hass, entry, "_primary_target")).state) == 66.0


async def test_hysteresis_armed_from_startup(hass: HomeAssistant) -> None:
    """No instant mode flip after setup, and the dwell counter is sane (#6).

    Armed from startup by the last mode change, which a restart finds stored:
    here one made just before this setup. A fresh install has none, and its
    first decision waits for nothing (the next test).
    """
    heads = [MockHead("a"), MockHead("b")]
    entry = await _setup(hass, heads, changed_ago=0.0)
    await _enable(hass, entry)

    # A room 10° below target wants heat, but the fresh hysteresis clock gates it.
    await _set_temp(hass, "sensor.room_a_temp", 60)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.state == "cool"  # flip deferred until the dwell elapses
    assert plan.attributes["mode_change_allowed"] is False
    assert plan.attributes["seconds_since_mode_change"] < 3600  # not ~56,000 years

    # Once the clock ages past the dwell, the flip goes through.
    expire_dwell(entry.runtime_data)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "heat"


async def test_fresh_install_first_flip_waits_for_nothing(hass: HomeAssistant) -> None:
    """No mode change on record: the first flip goes through, the next one is held.

    The plan reports no time since a change it never saw (None), never an
    invented count (#6 was a ~56,000-year one). The first change starts the
    dwell like any other.
    """
    heads = [MockHead("a"), MockHead("b")]
    entry = await _setup(hass, heads)
    coord = entry.runtime_data
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["seconds_since_mode_change"] is None
    assert plan.attributes["mode_change_allowed"] is True

    await _enable(hass, entry)
    await _set_temp(hass, "sensor.room_a_temp", 60)  # wants heat
    await coord.async_refresh()
    await hass.async_block_till_done()
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.state == "heat"  # not deferred: nothing to wait out
    assert coord.current_shared_mode == "heat"

    # That change started the dwell: the way back is held.
    await _set_temp(hass, "sensor.room_a_temp", 80)  # wants cool
    await coord.async_refresh()
    await hass.async_block_till_done()
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.state == "heat"
    assert plan.attributes["mode_change_allowed"] is False
    assert 0 <= plan.attributes["seconds_since_mode_change"] < 3600
