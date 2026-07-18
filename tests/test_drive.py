"""End-to-end actuator test: enable the coordinator and watch it drive real
ClimateEntity heads (decide -> act). Uses a mock climate platform so the full
service path (climate.set_temperature / climate.set_hvac_mode) actually runs.

Requires pytest-homeassistant-custom-component (Python 3.12+).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import (  # noqa: E402
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.util.unit_system import (  # noqa: E402
    METRIC_SYSTEM,
    US_CUSTOMARY_SYSTEM,
)
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (  # noqa: E402
    CONF_ENGAGE_DEADBAND,
    CONF_FAN_BOOST_ENABLE,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
    FAN_LADDER,
)

SENSOR_A = "sensor.room_a_temp"
SENSOR_B = "sensor.room_b_temp"


class MockHead(ClimateEntity):
    """A minimal dual-setpoint head that records what it's told."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY]
    # RAW/unsorted order, as reported by the real head (includes "middle" and "high").
    _attr_fan_modes = ["auto", "low", "medium", "middle", "high", "quiet"]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, suffix: str) -> None:
        self._attr_unique_id = f"mock_head_{suffix}"
        self._attr_name = f"Mock Head {suffix}"
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature_low = None
        self._attr_target_temperature_high = None
        self._attr_fan_mode = "auto"

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._attr_fan_mode = fan_mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = mode
        if (low := kwargs.get("target_temp_low")) is not None:
            self._attr_target_temperature_low = low
        if (high := kwargs.get("target_temp_high")) is not None:
            self._attr_target_temperature_high = high
        self.async_write_ha_state()


class MockHeadC(MockHead):
    """A metric head: reports/accepts °C (matches a Celsius HA system)."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS


async def _setup_mock_heads(
    hass: HomeAssistant, *, cls: type[MockHead] = MockHead
) -> tuple[str, str]:
    """Register two mock climate heads and return their entity_ids."""
    heads = [cls("a"), cls("b")]

    async def _async_setup_platform(
        hass, config, async_add_entities, discovery_info=None  # noqa: ANN001
    ):
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(
        hass, "test.climate", MockPlatform(async_setup_platform=_async_setup_platform)
    )
    assert await async_setup_component(
        hass, "climate", {"climate": {"platform": "test"}}
    )
    await hass.async_block_till_done()
    return heads[0].entity_id, heads[1].entity_id


def _eid(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str:
    """Resolve an mxz entity_id by its unique_id suffix."""
    reg = er.async_get(hass)
    for ent in reg.entities.values():
        if ent.config_entry_id == entry.entry_id and ent.unique_id.endswith(suffix):
            return ent.entity_id
    raise AssertionError(f"no mxz entity ending in {suffix}")


async def _set_temp(hass: HomeAssistant, entity_id: str, value: float) -> None:
    hass.states.async_set(entity_id, str(value))
    await hass.async_block_till_done()


async def _recompute(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Force a deterministic compute+apply (production path is debounced)."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_coordinator_drives_heads(hass: HomeAssistant) -> None:
    """Enable the coordinator and assert it actually commands the heads."""
    hass.config.units = US_CUSTOMARY_SYSTEM  # keep setpoints in °F

    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Enable both rooms and the coordinator kill-switch (defaults are OFF).
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    # --- Scenario 1: primary hot (75 vs target 70 -> wants COOL), secondary satisfied ---
    await _set_temp(hass, SENSOR_A, 75)
    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)

    a = hass.states.get(head_a)
    b = hass.states.get(head_b)
    print(f"\nS1 primary={a.state} low={a.attributes.get('target_temp_low')} "
          f"high={a.attributes.get('target_temp_high')} | secondary={b.state}")
    assert a.state == "cool"
    assert a.attributes["target_temp_high"] == 70  # high = target
    assert a.attributes["target_temp_low"] == 68  # low = target - 2
    assert b.state == "fan_only"  # satisfied -> idles, doesn't starve the other

    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.state == "cool"
    assert plan.attributes["primary_engage"] == "cool"
    assert plan.attributes["secondary_engage"] == "satisfied"

    # --- Scenario 2: standoff. primary wants COOL (75), secondary wants HEAT (60).
    # Primary wins -> shared cool; secondary (wrong direction) idles in fan_only. ---
    await _set_temp(hass, SENSOR_A, 75)
    await _set_temp(hass, SENSOR_B, 60)
    await _recompute(hass, entry)

    a = hass.states.get(head_a)
    b = hass.states.get(head_b)
    print(f"S2 (standoff) primary={a.state} | secondary={b.state} "
          f"(plan standoff={hass.states.get(_eid(hass, entry, '_plan')).attributes['standoff']})")
    assert a.state == "cool"
    assert b.state == "fan_only"

    # --- Scenario 3: eco-idle. Both within the 50–78 protection band -> both OFF. ---
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_eco_idle")}, blocking=True
    )
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)

    a = hass.states.get(head_a)
    b = hass.states.get(head_b)
    print(f"S3 (eco) primary={a.state} | secondary={b.state}")
    assert a.state == "off"
    assert b.state == "off"

    # --- Kill-switch: disable the coordinator, drift a head, confirm it does NOT fight ---
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": _eid(hass, entry, "_coordinator_enable")}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": _eid(hass, entry, "_eco_idle")}, blocking=True
    )
    await hass.async_block_till_done()
    # Force a head off-band; with the kill-switch OFF the coordinator must not touch it.
    await hass.services.async_call(
        "climate", "set_hvac_mode",
        {"entity_id": head_a, "hvac_mode": "heat_cool"}, blocking=True
    )
    await _set_temp(hass, SENSOR_A, 80)  # would normally trigger a cool command
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "heat_cool"  # untouched while disabled
    print("S4 (kill-switch off) primary stayed heat_cool -> coordinator did not write")


async def test_heat_lockout_suppresses_then_floors(hass: HomeAssistant) -> None:
    """heat_lockout: a below-target room idles (fan_only) unless below the safety floor."""
    hass.config.units = US_CUSTOMARY_SYSTEM

    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Hysteresis arms at startup now (#6): age the clock so this test's flip is allowed.
    entry.runtime_data._last_mode_change_ts = 0.0

    for suffix in (
        "_primary_enable", "_secondary_enable", "_coordinator_enable", "_heat_lockout",
    ):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    # Primary 64 (well below its 70 target -> would HEAT) but above the 58 floor.
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    assert hass.states.get(_eid(hass, entry, "_plan")).attributes["primary_demand"] == "neutral"
    assert hass.states.get(head_a).state == "fan_only"  # locked out -> idles, no heat

    # Drop below the safety floor -> heat kicks in regardless of the lockout.
    await _set_temp(hass, SENSOR_A, 57)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "heat"
    print("HEAT-LOCKOUT: 64F idled fan_only, 57F (< 58 floor) heated")


async def test_cool_lockout_suppresses_then_ceilings(hass: HomeAssistant) -> None:
    """cool_lockout: an above-target room idles unless above the safety ceiling."""
    hass.config.units = US_CUSTOMARY_SYSTEM

    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for suffix in (
        "_primary_enable", "_secondary_enable", "_coordinator_enable", "_cool_lockout",
    ):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    # Primary 75 (above its 70 target -> would COOL) but below the 80 ceiling -> idle.
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(_eid(hass, entry, "_plan")).attributes["primary_demand"] == "neutral"
    assert hass.states.get(head_a).state == "fan_only"  # locked out -> idles, no cool

    # Above the safety ceiling -> cool kicks in regardless of the lockout.
    await _set_temp(hass, SENSOR_A, 82)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    print("COOL-LOCKOUT: 75F idled fan_only, 82F (> 80 ceiling) cooled")


async def _set_target(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )
    await hass.async_block_till_done()


async def _setup_fan_boost(
    hass: HomeAssistant, head_a: str, head_b: str
) -> MockConfigEntry:
    """Fan-boost entry, coordinator + both rooms enabled, primary target 62."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 62)
    return entry


async def _user_set_fan(hass: HomeAssistant, climate_id: str, token: str) -> None:
    """Simulate the user reaching in and picking a fan speed on a head."""
    await hass.services.async_call(
        "climate", "set_fan_mode",
        {"entity_id": climate_id, "fan_mode": token}, blocking=True
    )
    await hass.async_block_till_done()


async def test_manual_fan_latch_holds_while_conditioning(
    hass: HomeAssistant,
) -> None:
    """User picks a speed on a conditioning head -> the coordinator stops writing fan."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    # Cooling hard: boost drives the primary to "high".
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"

    # User overrides to "quiet" while it's still cooling -> that must stick.
    await _user_set_fan(hass, head_a, "quiet")
    await _set_temp(hass, SENSOR_A, 67.1)  # still a big cooling delta
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"
    await _recompute(hass, entry)  # a second cycle must not stomp it either
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"
    # Surfaced for dashboards as a per-zone hold flag.
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True


async def test_manual_fan_latch_suppresses_return_to_auto(
    hass: HomeAssistant,
) -> None:
    """A latched head that goes satisfied/fan_only is NOT dragged back to auto."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "medium")

    # Room reaches target -> head idles fan_only. Without the latch this branch
    # would force "auto"; latched, the user's "medium" survives.
    await _set_temp(hass, SENSOR_A, 62)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    assert a.state == "fan_only"
    assert a.attributes["fan_mode"] == "medium"


async def test_manual_fan_latch_releases_on_auto(hass: HomeAssistant) -> None:
    """Setting the head back to 'auto' hands control back; boost resumes at once."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "quiet")
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"  # latched

    # User hands control back.
    await _user_set_fan(hass, head_a, "auto")
    await _set_temp(hass, SENSOR_A, 67)  # still a big cooling delta
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"  # boost resumed
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False


async def test_manual_fan_latch_ignores_own_echo(hass: HomeAssistant) -> None:
    """The coordinator's own just-written token, echoed back, must not latch.

    Drives the delta down the ladder so the coordinator writes several different
    tokens across cycles; each write leaves the head reporting the token from the
    PRIOR cycle for one beat. That echo must never read as a user departure.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    # Walk the room in toward target so the ladder eases high -> ... over cycles.
    for temp in (67, 66, 65, 64, 63.4, 63.0, 62.6):
        await _set_temp(hass, SENSOR_A, temp)
        await _recompute(hass, entry)
        await _recompute(hass, entry)  # extra cycle: exercise the echo read path
        plan = hass.states.get(_eid(hass, entry, "_plan"))
        assert plan.attributes["zones"][0]["fan_hold"] is False  # never false-latched
    # Fan still coordinator-controlled (a real ladder token), not stuck.
    assert hass.states.get(head_a).attributes["fan_mode"] in FAN_LADDER


async def test_manual_fan_latch_seeds_from_head_on_restart(
    hass: HomeAssistant,
) -> None:
    """First compute with no memory: non-auto head seeds latched; auto seeds free."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    coord = entry.runtime_data

    # Simulate a restart mid-manual-pick: head sitting at "medium", no _fan_cmd.
    await _user_set_fan(hass, head_a, "medium")
    coord._fan_cmd.clear()
    coord._fan_prev.clear()
    coord._fan_latched.clear()

    await _set_temp(hass, SENSOR_A, 67)  # a delta that WOULD boost to "high"
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"  # seeded latched

    # Head b was at "auto" across the same restart -> seeds free, boost applies.
    coord._fan_cmd.pop(head_b, None)
    coord._fan_latched.pop(head_b, None)
    await _set_target(hass, _eid(hass, entry, "_secondary_target"), 62)
    await _set_temp(hass, SENSOR_B, 67)
    await _recompute(hass, entry)
    assert hass.states.get(head_b).attributes["fan_mode"] == "high"  # seeded free


async def test_manual_fan_latch_isolated_per_zone(hass: HomeAssistant) -> None:
    """One zone latched by a manual pick while another keeps boosting normally."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    await _set_target(hass, _eid(hass, entry, "_secondary_target"), 62)

    # Both cooling hard -> both boost.
    await _set_temp(hass, SENSOR_A, 67)
    await _set_temp(hass, SENSOR_B, 67)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    assert hass.states.get(head_b).attributes["fan_mode"] == "high"

    # User latches A on "low"; B must keep tracking the ladder.
    await _user_set_fan(hass, head_a, "low")
    await _set_temp(hass, SENSOR_A, 67)
    await _set_temp(hass, SENSOR_B, 63.4)  # B eases down the ladder
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "low"  # held
    assert hass.states.get(head_b).attributes["fan_mode"] == "low"  # boosted (walk 4->1)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True
    assert plan.attributes["zones"][1]["fan_hold"] is False


async def test_manual_fan_latch_inert_without_auto_token(
    hass: HomeAssistant,
) -> None:
    """A head whose fan_modes lack 'auto' never engages the latch machinery."""
    hass.config.units = US_CUSTOMARY_SYSTEM

    class NoAutoHead(MockHead):
        _attr_fan_modes = ["low", "medium", "high"]  # no "auto"

        def __init__(self, suffix: str) -> None:
            super().__init__(suffix)
            self._attr_fan_mode = "low"

    head_a, head_b = await _setup_mock_heads(hass, cls=NoAutoHead)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    # Fan never written (no auto to key off), no latch surfaced.
    assert hass.states.get(head_a).attributes["fan_mode"] == "low"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False


async def test_fan_boost_drives_speed(hass: HomeAssistant) -> None:
    """fan boost: a conditioning head's fan tracks how far the room is off-target."""
    hass.config.units = US_CUSTOMARY_SYSTEM

    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    # Primary target 62; secondary stays at its 70 default (satisfied).
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 62)

    # --- Big delta: room 67 vs target 62 (delta 5, cooling) -> MAX fan "high". ---
    await _set_temp(hass, SENSOR_A, 67)
    await _set_temp(hass, SENSOR_B, 70)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    b = hass.states.get(head_b)
    print(f"FAN-BOOST big-delta: primary={a.state} fan={a.attributes.get('fan_mode')} "
          f"| secondary={b.state} fan={b.attributes.get('fan_mode')}")
    assert a.state == "cool"
    assert a.attributes["fan_mode"] == "high"
    # Satisfied/fan_only secondary -> firmware "auto".
    assert b.state == "fan_only"
    assert b.attributes["fan_mode"] == "auto"

    # --- Room closes to 63.4 (delta 1.4, still cooling past the 1F deadband) ->
    # the fan eases down the ladder toward "low" via the hysteresis walk 4->1. ---
    await _set_temp(hass, SENSOR_A, 63.4)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    print(f"FAN-BOOST small-delta: primary={a.state} fan={a.attributes.get('fan_mode')}")
    assert a.state == "cool"
    assert a.attributes["fan_mode"] == "low"  # hysteresis walk 4->1


async def test_fan_boost_disabled_leaves_fan_alone(hass: HomeAssistant) -> None:
    """With fan boost explicitly opted OUT, the coordinator never touches the fan."""
    hass.config.units = US_CUSTOMARY_SYSTEM

    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: False,  # explicit opt-out (default is ON)
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    await _set_target(hass, _eid(hass, entry, "_primary_target"), 62)
    # Big delta that WOULD boost to "high" if the feature were on.
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    assert a.state == "cool"
    assert a.attributes["fan_mode"] == "auto"  # untouched -> stays at the default
    print("FAN-BOOST opted out: fan stayed 'auto' despite a 5F delta")


async def test_fan_boost_defaults_on(hass: HomeAssistant) -> None:
    """An entry with no fan_boost option gets the feature ON (v3 default)."""
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.fan_boost_enable is True


async def test_coordinator_drives_heads_metric(hass: HomeAssistant) -> None:
    """On a °C system the coordinator adopts metric defaults and drives °C setpoints."""
    hass.config.units = METRIC_SYSTEM

    head_a, head_b = await _setup_mock_heads(hass, cls=MockHeadC)
    await _set_temp(hass, SENSOR_A, 21)
    await _set_temp(hass, SENSOR_B, 21)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # The coordinator resolved the metric profile, not the °F legacy defaults.
    coord = entry.runtime_data
    assert coord.celsius is True
    assert coord.target_default == 21.0
    assert coord.target_step == 0.5
    assert coord.clamp_min == 15.0
    assert coord.clamp_max == 31.0
    assert coord.demand_threshold == 1.5
    # The number/climate entities report °C.
    tgt = hass.states.get(_eid(hass, entry, "_primary_target"))
    assert tgt.attributes["unit_of_measurement"] == UnitOfTemperature.CELSIUS

    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    # Primary 24 (hot vs the 21 default target -> COOL); band=1 -> (20, 21).
    await _set_temp(hass, SENSOR_A, 24)
    await _set_temp(hass, SENSOR_B, 21)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    b = hass.states.get(head_b)
    print(f"\nMETRIC S1 primary={a.state} low={a.attributes.get('target_temp_low')} "
          f"high={a.attributes.get('target_temp_high')} | secondary={b.state}")
    assert a.state == "cool"
    assert a.attributes["target_temp_high"] == 21.0  # high = target
    assert a.attributes["target_temp_low"] == 20.0  # low = target - 1 (metric band)
    assert b.state == "fan_only"  # satisfied idles, no starvation

    # 0.5° resolution: target 21.5, room 25 -> cool band -> (20.5, 21.5).
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 21.5)
    await _set_temp(hass, SENSOR_A, 25)
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    print(f"METRIC S2 half-step low={a.attributes.get('target_temp_low')} "
          f"high={a.attributes.get('target_temp_high')}")
    assert a.attributes["target_temp_low"] == 20.5
    assert a.attributes["target_temp_high"] == 21.5


async def test_engage_latch_runs_to_target_then_coasts(hass: HomeAssistant) -> None:
    """The reported scenario: target 67->63 runs ALL the way to 63, then coasts."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 67)
    await _set_temp(hass, SENSOR_B, 63)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    await _set_target(hass, _eid(hass, entry, "_primary_target"), 63)
    await _set_target(hass, _eid(hass, entry, "_secondary_target"), 63)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"  # 67 vs 63 -> engage

    # Inside the old static band (63 < t <= 64) the run now CONTINUES.
    await _set_temp(hass, SENSOR_A, 63.5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"  # would have parked at 64 before

    # Target reached -> coast in fan_only.
    await _set_temp(hass, SENSOR_A, 63.0)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"

    # Drifting inside the band stays coasting; past the band re-engages.
    await _set_temp(hass, SENSOR_A, 63.8)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"
    await _set_temp(hass, SENSOR_A, 64.5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"

    # The neighbor sat at its target the whole time and never got dragged in.
    assert hass.states.get(head_b).state == "fan_only"


async def test_engage_latch_survives_shared_mode_flip(hass: HomeAssistant) -> None:
    """A latched zone parked by a shared-mode flip resumes its run when the mode returns."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 67)
    await _set_temp(hass, SENSOR_B, 63)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    await _set_target(hass, _eid(hass, entry, "_primary_target"), 63)
    await _set_target(hass, _eid(hass, entry, "_secondary_target"), 63)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    await _set_temp(hass, SENSOR_A, 63.5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"  # mid-run, latched

    # B goes cold enough to DEMAND heat; hysteresis elapsed -> shared mode flips.
    await _set_temp(hass, SENSOR_B, 55)
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == "heat"
    # A's run is parked (mode mismatch), but the latch remembers it...
    assert hass.states.get(head_a).state == "fan_only"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["primary_engage"] == "cool"

    # ...so when the shared mode returns to cool (B now too warm), A — still at
    # 63.5, INSIDE the deadband — resumes straight to its target.
    await _set_temp(hass, SENSOR_B, 67)
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"

    # And the run still ends at the target, coasting from there.
    await _set_temp(hass, SENSOR_A, 63.0)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"


async def test_target_change_resets_latch(hass: HomeAssistant) -> None:
    """A target change mid-run: same direction continues seamlessly, a direction
    change coasts — it never whiplashes into the opposite mode."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 67)
    await _set_temp(hass, SENSOR_B, 63)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()

    await _set_target(hass, _eid(hass, entry, "_primary_target"), 63)
    await _set_target(hass, _eid(hass, entry, "_secondary_target"), 63)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 63.5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"  # mid-run, latched

    # Lower the target mid-run: latch resets, re-seeds from the head's own cool
    # mode, and the run continues seamlessly toward the NEW number.
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 62)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"

    # Raise the target ABOVE the room mid-run: fresh decision, and the stale
    # cool run disengages to coast — never a whiplash into heat.
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 64)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"


async def test_engage_deadband_clamped_to_profile_bounds(hass: HomeAssistant) -> None:
    """A hand-edited re-engage drift is clamped into the sane range (0.5-5 F)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_ENGAGE_DEADBAND: 40.0,  # absurd -> clamped to 5.0
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.engage_deadband == 5.0


async def test_engage_deadband_floor_keeps_coast_window(hass: HomeAssistant) -> None:
    """A zero/negative drift can't collapse the coast window (clamped up to 0.5 F)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_ENGAGE_DEADBAND: 0.0,  # would flip-flop at target -> clamped to 0.5
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.engage_deadband == 0.5
