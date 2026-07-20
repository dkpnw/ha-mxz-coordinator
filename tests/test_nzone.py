"""N-zone tests: v1->v2 entry migration and a 6-zone end-to-end drive sim."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM  # noqa: E402
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
    CONF_PRIMARY_VANE_VERTICAL,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    CONF_ZONES,
    DOMAIN,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
    ZONE_VANE_VERTICAL,
)

from .test_drive import MockHead, _eid, _set_temp  # noqa: E402


async def test_v1_entry_migrates_to_zones(hass: HomeAssistant) -> None:
    """A v1 (flat primary/secondary) entry gains the zones list on setup."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [MockHead("a"), MockHead("b")]

    async def _setup_platform(hass, config, async_add_entities, discovery_info=None):  # noqa: ANN001
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=_setup_platform))
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: heads[0].entity_id,
            CONF_SECONDARY_CLIMATE: heads[1].entity_id,
            CONF_PRIMARY_SENSOR: "sensor.room_a_temp",
            CONF_SECONDARY_SENSOR: "sensor.room_b_temp",
            CONF_PRIMARY_VANE_VERTICAL: "select.vane_a",
        },
    )
    entry.add_to_hass(hass)
    await _set_temp(hass, "sensor.room_a_temp", 70)
    await _set_temp(hass, "sensor.room_b_temp", 70)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    zones = entry.data[CONF_ZONES]
    assert len(zones) == 2
    assert zones[0][ZONE_CLIMATE] == heads[0].entity_id
    assert zones[0][ZONE_VANE_VERTICAL] == "select.vane_a"
    assert zones[0][ZONE_NAME] == "Primary"
    # Legacy entity unique_ids survive: the primary/secondary entities exist.
    assert _eid(hass, entry, "_primary_target")
    assert _eid(hass, entry, "_secondary_enable")
    assert _eid(hass, entry, "_primary_thermostat")


async def test_six_zone_drive(hass: HomeAssistant) -> None:
    """Six heads on one outdoor unit: standoff priority + per-zone engage."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [MockHead(chr(ord("a") + i)) for i in range(6)]
    sensors = [f"sensor.zone_{i}_temp" for i in range(6)]

    async def _setup_platform(hass, config, async_add_entities, discovery_info=None):  # noqa: ANN001
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=_setup_platform))
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="MXZ Coordinator",
        data={
            CONF_ZONES: [
                {
                    ZONE_NAME: f"Zone {i + 1}",
                    ZONE_CLIMATE: heads[i].entity_id,
                    ZONE_SENSOR: sensors[i],
                }
                for i in range(6)
            ]
        },
    )
    entry.add_to_hass(hass)
    for s in sensors:
        await _set_temp(hass, s, 70)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coord = entry.runtime_data
    assert len(coord.zones) == 6
    assert [z.slug for z in coord.zones] == [
        "primary", "secondary", "zone_3", "zone_4", "zone_5", "zone_6",
    ]

    # Enable everything (all six zone switches + the kill-switch).
    for slug in ("primary", "secondary", "zone_3", "zone_4", "zone_5", "zone_6"):
        await hass.services.async_call(
            "switch", "turn_on",
            {"entity_id": _eid(hass, entry, f"_{slug}_enable")}, blocking=True,
        )
    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": _eid(hass, entry, "_coordinator_enable")}, blocking=True,
    )
    await hass.async_block_till_done()

    # Zone 1 (highest priority) hot -> COOL; zone 6 cold (wants heat) loses the
    # standoff and idles fan_only; satisfied middles idle fan_only.
    await _set_temp(hass, sensors[0], 75)
    await _set_temp(hass, sensors[5], 65)
    await coord.async_refresh()
    await hass.async_block_till_done()

    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.state == "cool"
    assert plan.attributes["standoff"] is True
    assert hass.states.get(heads[0].entity_id).state == "cool"
    assert hass.states.get(heads[5].entity_id).state == "fan_only"  # loser idles
    for i in (1, 2, 3, 4):
        assert hass.states.get(heads[i].entity_id).state == "fan_only"

    # The zones attribute exposes all six rooms.
    assert len(plan.attributes["zones"]) == 6
    assert plan.attributes["zones"][5]["demand"] == "heat"

    # Now the primary is satisfied and only zone 6 calls -> flip to heat is
    # blocked by hysteresis... but the first flip is always allowed (epoch 0),
    # and we already flipped nothing (started cool). Force allowed by zeroing.
    await _set_temp(hass, sensors[0], 70)
    coord._last_mode_change_ts = 0.0
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "heat"
    assert hass.states.get(heads[5].entity_id).state == "heat"
    assert hass.states.get(heads[0].entity_id).state == "fan_only"


async def test_every_zone_is_named_after_itself(hass: HomeAssistant) -> None:
    """Zones 0/1 name their entities after the zone, like zones 3+ always have.

    Their slugs stay "primary"/"secondary" (stable unique ids), but the DISPLAY
    name follows the name the user gave the head — a 6-zone setup shouldn't have
    four rooms and two "Primary"/"Secondary" strangers on the device page.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [MockHead("a"), MockHead("b"), MockHead("c")]
    names = ["Bedroom", "Rec room", "Snug"]

    async def _setup_platform(hass, config, async_add_entities, discovery_info=None):  # noqa: ANN001
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=_setup_platform))
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="MXZ Coordinator",
        data={
            CONF_ZONES: [
                {
                    ZONE_NAME: names[i],
                    ZONE_CLIMATE: heads[i].entity_id,
                    ZONE_SENSOR: f"sensor.zone_{i}_temp",
                }
                for i in range(3)
            ]
        },
    )
    entry.add_to_hass(hass)
    for i in range(3):
        await _set_temp(hass, f"sensor.zone_{i}_temp", 70)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    slugs = [z.slug for z in entry.runtime_data.zones]
    assert slugs == ["primary", "secondary", "zone_3"]  # unique ids unchanged

    for name, slug in zip(names, slugs, strict=True):
        for suffix, label in (
            ("_target", "target"),
            ("_enable", "enable"),
            ("_fan_auto", "fan auto"),
            ("_thermostat", "thermostat"),
        ):
            state = hass.states.get(_eid(hass, entry, f"_{slug}{suffix}"))
            assert state.attributes["friendly_name"].endswith(f"{name} {label}")
