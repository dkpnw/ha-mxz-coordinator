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
    CONF_FAN_BOOST_MAX,
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
    hass: HomeAssistant,
    head_a: str,
    head_b: str,
    fan_boost_max: str | None = None,
) -> MockConfigEntry:
    """Fan-boost entry, coordinator + both rooms enabled, primary target 62."""
    data = {
        CONF_PRIMARY_CLIMATE: head_a,
        CONF_SECONDARY_CLIMATE: head_b,
        CONF_PRIMARY_SENSOR: SENSOR_A,
        CONF_SECONDARY_SENSOR: SENSOR_B,
        CONF_FAN_BOOST_ENABLE: True,
    }
    if fan_boost_max is not None:
        data[CONF_FAN_BOOST_MAX] = fan_boost_max
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data=data,
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


async def test_manual_fan_latch_tolerates_one_cycle_state_lag(
    hass: HomeAssistant,
) -> None:
    """The real echo race: a slow head still reporting the PRIOR commanded token.

    MockHead echoes writes instantly, so this test forges the lag by hand: after
    the coordinator has written two different ladder tokens, the head is made to
    report the older one again (ESPHome state lag / unavailable-restore replay).
    That must NOT latch — but a token the coordinator never wrote still must.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    await _set_temp(hass, SENSOR_A, 67)  # big delta -> "high"
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    await _set_temp(hass, SENSOR_A, 63.4)  # eases down the ladder
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "low"

    def _forge_fan(token: str) -> None:
        st = hass.states.get(head_a)
        hass.states.async_set(head_a, st.state, {**st.attributes, "fan_mode": token})

    # Lagged echo of the PRIOR write ("high" while cmd is "low") -> absorbed.
    _forge_fan("high")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False  # not a departure
    assert hass.states.get(head_a).attributes["fan_mode"] == "low"  # re-asserted

    # A token we NEVER commanded ("quiet") -> a genuine user departure, latches.
    _forge_fan("quiet")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"  # untouched


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


async def _set_fan_auto(
    hass: HomeAssistant, entity_id: str, on: bool
) -> None:
    """Flip a zone's Fan auto switch."""
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_fan_auto_switch_mirrors_latch(hass: HomeAssistant) -> None:
    """The Fan auto switch reflects the latch: manual speed -> OFF, auto -> ON."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    sw = _eid(hass, entry, "_primary_fan_auto")

    # Boost driving -> switch reads ON.
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    assert hass.states.get(sw).state == "on"

    # Manual pick latches -> switch flips OFF by itself (it's a mirror).
    await _user_set_fan(hass, head_a, "quiet")
    await _recompute(hass, entry)
    assert hass.states.get(sw).state == "off"

    # Observed auto releases -> switch flips back ON.
    await _user_set_fan(hass, head_a, "auto")
    await _recompute(hass, entry)
    assert hass.states.get(sw).state == "on"


async def test_fan_auto_switch_on_releases_latch(hass: HomeAssistant) -> None:
    """Turning Fan auto ON releases the hold; boost resumes with no re-latch."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    sw = _eid(hass, entry, "_primary_fan_auto")

    # Latch on "quiet" while cooling hard.
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "quiet")
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"  # held
    assert hass.states.get(sw).state == "off"

    # Turn the switch ON -> latch released, boost writes resume next cycle.
    await _set_fan_auto(hass, sw, True)
    assert hass.states.get(sw).state == "on"
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"  # boost resumed
    # The still-at-speed head must NOT re-latch as a fresh departure.
    await _recompute(hass, entry)
    assert hass.states.get(sw).state == "on"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False


async def test_fan_auto_switch_off_latches_at_current_speed(
    hass: HomeAssistant,
) -> None:
    """Turning Fan auto OFF while boost drives holds the head's current speed."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    sw = _eid(hass, entry, "_primary_fan_auto")

    # Boost eases the head to a mid-ladder speed (target 62, ~1.5 F out -> "low").
    await _set_temp(hass, SENSOR_A, 63.5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "low"

    # Flip Fan auto OFF -> latch at the current (non-top) speed; no further fan
    # writes even as the delta grows (boost would otherwise ramp up).
    await _set_fan_auto(hass, sw, False)
    assert hass.states.get(sw).state == "off"
    await _set_temp(hass, SENSOR_A, 57)  # would boost to "high" if not held
    await _recompute(hass, entry)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "low"  # held, not boosted
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True


async def test_fan_auto_switch_off_at_auto_is_noop(hass: HomeAssistant) -> None:
    """OFF while the head is at 'auto' is a no-op: nothing to hold, switch stays ON."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    sw = _eid(hass, entry, "_primary_fan_auto")

    # Head satisfied (at target 62) -> boost returns it to firmware "auto".
    await _set_temp(hass, SENSOR_A, 62)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "auto"
    assert hass.states.get(sw).state == "on"

    # Turning OFF here is meaningless -> no-op, switch remains ON, no latch.
    await _set_fan_auto(hass, sw, False)
    assert hass.states.get(sw).state == "on"
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False


async def test_fan_auto_switch_off_at_top_speed_sticks(hass: HomeAssistant) -> None:
    """A switch-OFF hold at the head's TOP token sticks — no standing-merge.

    A slider-set top token is ambiguous with the max handback, so it merges into
    auto whenever boost would command max anyway. The switch gesture is not
    ambiguous: OFF means hold — even at max, even while boost keeps demanding
    max — until the switch flips back ON (or the fan is observed at "auto").
    Without the explicit-hold exemption the switch would bounce back ON on the
    very next cycle, silently contradicting the user's gesture.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    sw = _eid(hass, entry, "_primary_fan_auto")

    # Far off target (delta 5) -> boost drives the head to its top token.
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"

    # Flip OFF right there. Boost would still command max -> the slider merge
    # rule would release this instantly; the explicit switch hold must not.
    await _set_fan_auto(hass, sw, False)
    assert hass.states.get(sw).state == "off"
    await _recompute(hass, entry)
    await _recompute(hass, entry)
    assert hass.states.get(sw).state == "off"  # no bounce-back
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"  # held

    # Switch ON releases it; boost resumes (still max here) with no re-latch.
    await _set_fan_auto(hass, sw, True)
    await _recompute(hass, entry)
    assert hass.states.get(sw).state == "on"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False


async def test_fan_auto_switch_hold_demoted_by_slider_departure(
    hass: HomeAssistant,
) -> None:
    """A slider departure demotes an explicit switch hold to slider semantics.

    Explicit switch holds are exempt from the top-token standing-merge; but the
    moment the user reaches for the SLIDER instead, the hold is theirs again
    under the slider rules — including the merge, once they land back on the
    top token while boost would command max.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    sw = _eid(hass, entry, "_primary_fan_auto")

    # Boost at max (delta 5) -> explicit switch hold at the top token sticks.
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    await _set_fan_auto(hass, sw, False)
    await _recompute(hass, entry)
    assert hass.states.get(sw).state == "off"

    # User moves the slider to "quiet": a departure -> still held, but the hold
    # is now slider-origin (the explicit exemption is gone).
    await _user_set_fan(hass, head_a, "quiet")
    await _recompute(hass, entry)
    assert hass.states.get(sw).state == "off"
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"

    # Back to the top token while boost would command max -> the slider merge
    # applies again: the hold folds into auto and the switch reads ON.
    await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    assert hass.states.get(sw).state == "on"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False


async def test_fan_auto_switch_isolated_per_zone(hass: HomeAssistant) -> None:
    """Zone 1's Fan auto switch doesn't touch zone 2's latch."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    await _set_target(hass, _eid(hass, entry, "_secondary_target"), 62)
    sw_a = _eid(hass, entry, "_primary_fan_auto")
    sw_b = _eid(hass, entry, "_secondary_fan_auto")

    # A eases to a non-top speed; B boosts hard. Both driven by boost -> both ON.
    await _set_temp(hass, SENSOR_A, 63.5)  # ~1.5 F out -> "low"
    await _set_temp(hass, SENSOR_B, 67)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "low"
    assert hass.states.get(sw_a).state == "on"
    assert hass.states.get(sw_b).state == "on"

    # Hold A (at its non-top speed) via its switch; B keeps boosting untouched.
    await _set_fan_auto(hass, sw_a, False)
    await _recompute(hass, entry)
    assert hass.states.get(sw_a).state == "off"
    assert hass.states.get(sw_b).state == "on"
    assert hass.states.get(head_b).attributes["fan_mode"] == "high"  # still boosting
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True
    assert plan.attributes["zones"][1]["fan_hold"] is False


async def test_fan_auto_switch_seeds_from_head_on_restart(
    hass: HomeAssistant,
) -> None:
    """After a restart the latch seeds from head state -> switch matches on compute."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    coord = entry.runtime_data
    sw = _eid(hass, entry, "_primary_fan_auto")

    # Restart mid-manual-pick: head at "medium", coordinator memory wiped.
    await _user_set_fan(hass, head_a, "medium")
    coord._fan_cmd.clear()
    coord._fan_prev.clear()
    coord._fan_latched.clear()

    await _set_temp(hass, SENSOR_A, 67)  # a delta that WOULD boost to "high"
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"  # seeded latched
    assert hass.states.get(sw).state == "off"  # switch reflects the seeded hold


async def test_max_fan_handback_far_off_target_hands_back(
    hass: HomeAssistant,
) -> None:
    """User sets MAX while boost would already be at max -> handback, not latch.

    The observed top token equals what the ladder is commanding, so it reads as
    "give it back to auto": no latch, and as the room closes on target the
    coordinator ramps the fan DOWN — proving it adopted the token and resumed
    control.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    # Cooling hard: boost drives to the top token "high".
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"

    # User sets "high" (the top token) while still far off target -> handback.
    await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False  # NOT latched

    # As the room closes on target the fan eases down the ladder -> control resumed.
    for temp in (64, 63.4, 62.4):
        await _set_temp(hass, SENSOR_A, temp)
        await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False  # never re-latched


async def test_max_fan_handback_escapes_a_slower_hold(hass: HomeAssistant) -> None:
    """Sliding a SLOWER hold up to max while boost would command max -> handback.

    A zone held at "low" (non-top: drift never merges it), then the delta grows
    to where the ladder would command the top token. The slide to "high" is a
    genuine DEPARTURE (differs from both remembered commands), but because the
    ladder's pick for this delta IS the top token it reads as "you drive": adopt,
    don't re-latch, and ramp down under normal hysteresis afterward. This is the
    HomeKit escape hatch from any hold, not just a max one.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    # Boost settles at "middle" (delta 3); user picks "low" -> a non-top hold.
    await _set_temp(hass, SENSOR_A, 65)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "middle"
    await _user_set_fan(hass, head_a, "low")
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True

    # Room drifts hard (delta 5): a non-top hold never merges -> still held.
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True

    # User slides the hold up to "high" -> departure + ladder-at-max = handback.
    await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False  # adopted, not latched
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"

    # Ramp-down proceeds from max under DOWN_AT hysteresis -> control resumed.
    await _set_temp(hass, SENSOR_A, 62.4)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False


async def test_max_fan_handback_seed_at_max_adopts_on_restart(
    hass: HomeAssistant,
) -> None:
    """Restart mid-boost with the head AT the top token and a max-demanding delta:
    the seed runs the same handback check and ADOPTS instead of latching — the
    one restart case that self-heals (a non-max seed still latches, pinned by
    test_manual_fan_latch_seeds_from_head_on_restart)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    coord = entry.runtime_data

    # Boost to "high", then wipe the memory to simulate a restart.
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    coord._fan_cmd.clear()
    coord._fan_prev.clear()
    coord._fan_latched.clear()
    coord._fan_idx.clear()

    # First compute post-restart: delta still demands max -> adopt, not latch.
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"

    # And it ramps down like any adopted handback.
    await _set_temp(hass, SENSOR_A, 62.4)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"


async def test_max_fan_handback_near_target_latches(hass: HomeAssistant) -> None:
    """User sets MAX while the ladder is BELOW max -> a real request, latches."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    # Small cooling delta: ladder sits low, not at "high".
    await _set_temp(hass, SENSOR_A, 63)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] != "high"

    # User sets "high" -> genuinely more air than boost would give -> latches.
    await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    await _recompute(hass, entry)  # no fan writes after
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True


async def test_max_fan_handback_when_satisfied_latches(hass: HomeAssistant) -> None:
    """User sets MAX on a satisfied/fan_only head -> latches (no ramp exists)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    # Room at target -> head idles fan_only, no boost ramp.
    await _set_temp(hass, SENSOR_A, 62)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"

    # Setting "high" here is not a handback (nothing to hand back to) -> latch.
    await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True


async def test_max_fan_handback_capped_below_top_latches(
    hass: HomeAssistant,
) -> None:
    """fan_boost_max capped at "medium": "high" can never be the ladder's pick,
    so even far off target a user "high" latches (the ladder never reaches it)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b, fan_boost_max="medium")

    # Far off target, but the cap holds the ladder at "medium".
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"

    # User sets the head's top token "high" -> ladder can never reach it -> latch.
    await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True


async def test_max_fan_handback_uses_heads_top_available_token(
    hass: HomeAssistant,
) -> None:
    """A head lacking "high"/"middle": the top token is the head's ACTUAL max."""
    hass.config.units = US_CUSTOMARY_SYSTEM

    class LowTopHead(MockHead):
        # Top available ladder token is "medium" (no middle/high).
        _attr_fan_modes = ["auto", "quiet", "low", "medium"]

    head_a, head_b = await _setup_mock_heads(hass, cls=LowTopHead)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    # Cap the boost at this head's real top token so the ladder can reach it.
    entry = await _setup_fan_boost(hass, head_a, head_b, fan_boost_max="medium")

    # Far off target: boost drives to the head's top available token "medium".
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"

    # User sets "medium" (this head's top) while there -> handback, not latch.
    await _user_set_fan(hass, head_a, "medium")
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False

    # Ramps down as the room closes -> control resumed.
    await _set_temp(hass, SENSOR_A, 62.4)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False


async def test_max_hold_merges_into_auto_when_target_moves(
    hass: HomeAssistant,
) -> None:
    """Latched at the top token near target, then a TARGET change grows the delta
    until the ladder would command max -> the hold MERGES into auto (releases),
    and ramps DOWN as the room later closes in. This is the motivating scenario:
    a max hold is never a hold "above" auto, so it folds in the moment auto would
    be commanding max anyway."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    # Small cooling delta (temp 64, target 62): the ladder sits below "high".
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] != "high"

    # User picks "high" here -> a genuine request for more air -> latches.
    await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True

    # Now drop the TARGET so the delta grows to where the ladder commands max.
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 60)  # delta 4
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False  # merged into auto
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"

    # As the room closes on the new target the fan eases down -> control resumed.
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 64)  # satisfied
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] != "high"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False  # never re-latched


async def test_max_hold_stays_latched_when_delta_grows_only_partway(
    hass: HomeAssistant,
) -> None:
    """Latched at the top token, then the target moves only enough that the ladder
    would command a MIDDLE rung -> the hold stays latched (merge needs max)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    # Small delta -> user "high" latches (ladder is below high).
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True

    # Target down to 61 -> delta 3 -> fresh ladder commands "middle", not "high".
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 61)
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True  # not max -> still held
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"  # untouched


async def test_non_top_hold_never_merges_on_drift(hass: HomeAssistant) -> None:
    """Latched at a NON-top token, then the delta grows to where the ladder would
    command max -> stays latched. Only top-token holds merge; slower holds are
    gesture-released only (the deliberate asymmetry)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    # Boost sits at "middle"; user picks "low" (a non-top token, below what boost
    # commands) -> a departure -> latches.
    await _set_temp(hass, SENSOR_A, 65)  # delta 3 -> boost "middle"
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "middle"
    await _user_set_fan(hass, head_a, "low")
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True

    # Grow the delta hard (target 59 -> delta 6, where the ladder would be "high").
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 59)
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True  # slower hold never merges
    assert hass.states.get(head_a).attributes["fan_mode"] == "low"  # untouched


async def test_max_hold_capped_below_top_never_merges(hass: HomeAssistant) -> None:
    """fan_boost_max capped below the head's top token: latched at the top token,
    then a huge delta -> stays latched, because the ladder can never reach the top
    so the merge condition can never be satisfied (the cap excludes the merge)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b, fan_boost_max="medium")

    # Far off target, but the cap holds the ladder at "medium"; user "high" latches.
    await _set_temp(hass, SENSOR_A, 67)  # delta 5, but capped at "medium"
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"
    await _user_set_fan(hass, head_a, "high")
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True

    # Even a huge delta can't push the (capped) ladder to "high" -> stays latched.
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 59)  # delta 8
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"  # untouched


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


async def test_fan_change_triggers_prompt_refresh(hass: HomeAssistant) -> None:
    """A manual fan pick refreshes the coordinator promptly (no heartbeat wait).

    The latch itself was always safe (observation runs before any fan write),
    but without a refresh on fan_mode changes the Fan auto switch mirror sat
    stale until the next unrelated trigger — up to a full heartbeat.
    """
    import datetime

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import (
        async_fire_time_changed,
    )

    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    sw = _eid(hass, entry, "_primary_fan_auto")

    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    assert hass.states.get(sw).state == "on"

    # Manual pick -> NO explicit recompute. The head's state-change event alone
    # must drive the refresh (through the debouncer) and flip the mirror.
    await _user_set_fan(hass, head_a, "quiet")
    async_fire_time_changed(
        hass, dt_util.utcnow() + datetime.timedelta(seconds=15)
    )
    await hass.async_block_till_done()
    assert hass.states.get(sw).state == "off"

    # Handing back via the slider gets the same promptness.
    await _user_set_fan(hass, head_a, "auto")
    async_fire_time_changed(
        hass, dt_util.utcnow() + datetime.timedelta(seconds=15)
    )
    await hass.async_block_till_done()
    assert hass.states.get(sw).state == "on"
