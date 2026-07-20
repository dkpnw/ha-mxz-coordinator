"""Issue #7 coverage: stale restore-state guard + reconfigure flow + zone pruning."""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries  # noqa: E402
from homeassistant.core import HomeAssistant, State  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.mxz_coordinator.const import (  # noqa: E402
    CONF_NOTIFY_SERVICE,
    CONF_ZONES,
    DOMAIN,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
)

from .test_drive import _set_temp  # noqa: E402
from .test_single_setpoint import MockSingleSetpointHead  # noqa: E402

# Restore states must be seeded BEFORE setup, so this is the entity_id HA will
# generate: zone entities are named after the zone ("Zone 1" -> _zone_1_target),
# so it tracks _zones() below, not the zone's slug.
TARGET_EID = "number.mxz_coordinator_zone_1_target"
KILL_EID = "switch.mxz_coordinator_coordinator_enable"


def _zones(heads) -> list[dict]:
    return [
        {
            ZONE_NAME: f"Zone {i + 1}",
            ZONE_CLIMATE: h.entity_id,
            ZONE_SENSOR: f"sensor.room_{i}_temp",
        }
        for i, h in enumerate(heads)
    ]


async def _platform(hass: HomeAssistant, heads: list) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM

    async def _climate(hass, config, async_add_entities, discovery_info=None):  # noqa: ANN001
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=_climate))
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    await hass.async_block_till_done()
    for i in range(len(heads)):
        await _set_temp(hass, f"sensor.room_{i}_temp", 70)


async def _add_entry(hass: HomeAssistant, heads) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="MXZ Coordinator",
        data={CONF_ZONES: _zones(heads)},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_stale_restore_ignored_seed_wins(hass: HomeAssistant) -> None:
    """A restore from a PREVIOUS entry (older than created_at) is ignored (#7):
    the target seeds from the head's setpoint instead of resurrecting 70."""
    heads = [MockSingleSetpointHead("a"), MockSingleSetpointHead("b")]  # setpoint 66
    await _platform(hass, heads)
    stale = dt_util.utcnow() - timedelta(days=2)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(TARGET_EID, "70.0", last_updated=stale),
                {
                    "native_max_value": 88.0, "native_min_value": 59.0,
                    "native_step": 1.0, "native_unit_of_measurement": "°F",
                    "native_value": 70.0,
                },
            )
        ],
    )
    entry = await _add_entry(hass, heads)  # created NOW -> restore is older
    assert entry.runtime_data.zones[0].target == 66.0  # seeded, not restored


async def test_fresh_restore_still_honored(hass: HomeAssistant) -> None:
    """A restore NEWER than the entry (normal restart) is honored as before."""
    heads = [MockSingleSetpointHead("a"), MockSingleSetpointHead("b")]
    await _platform(hass, heads)
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="MXZ Coordinator",
        data={CONF_ZONES: _zones(heads)},
    )
    entry.add_to_hass(hass)  # created FIRST
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(TARGET_EID, "71.0"),  # last_updated = now > created_at
                {
                    "native_max_value": 88.0, "native_min_value": 59.0,
                    "native_step": 1.0, "native_unit_of_measurement": "°F",
                    "native_value": 71.0,
                },
            )
        ],
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.zones[0].target == 71.0  # restored


async def test_stale_kill_switch_not_resurrected(hass: HomeAssistant) -> None:
    """A deleted entry's kill-switch ON must not auto-enable a re-added entry."""
    heads = [MockSingleSetpointHead("a"), MockSingleSetpointHead("b")]
    await _platform(hass, heads)
    stale = dt_util.utcnow() - timedelta(days=2)
    mock_restore_cache(hass, [State(KILL_EID, "on", last_updated=stale)])
    entry = await _add_entry(hass, heads)
    assert entry.runtime_data.coordinator_enable is False
    assert hass.states.get(KILL_EID).state == "off"


async def test_reconfigure_swaps_a_sensor_in_place(hass: HomeAssistant) -> None:
    """The reconfigure flow fixes a mis-assigned sensor without delete/re-add (#7)."""
    heads = [MockSingleSetpointHead("a"), MockSingleSetpointHead("b")]
    await _platform(hass, heads)
    await _set_temp(hass, "sensor.the_right_one", 70)
    entry = await _add_entry(hass, heads)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": [h.entity_id for h in heads]}
    )
    assert result["step_id"] == "reconfigure_sensors"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sensor_1": "sensor.room_0_temp", "sensor_2": "sensor.the_right_one"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    zones = entry.data[CONF_ZONES]
    assert zones[1][ZONE_SENSOR] == "sensor.the_right_one"
    assert zones[0][ZONE_SENSOR] == "sensor.room_0_temp"  # unchanged zone kept
    assert zones[0][ZONE_NAME] == "Zone 1"  # name preserved for unchanged head
    # Notify left blank in the form -> explicitly cleared (not silently kept).
    assert entry.data.get(CONF_NOTIFY_SERVICE) is None


async def test_reconfigure_reorder_zone_dicts_follow_heads(hass: HomeAssistant) -> None:
    """Reordering heads moves each zone DICT with its head (name/sensor/vanes),
    while targets/enables stay with the priority slot — the documented caveat."""
    heads = [MockSingleSetpointHead("a"), MockSingleSetpointHead("b")]
    await _platform(hass, heads)
    entry = await _add_entry(hass, heads)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"heads": [heads[1].entity_id, heads[0].entity_id]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"sensor_1": "sensor.room_1_temp", "sensor_2": "sensor.room_0_temp"},
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()
    zones = entry.data[CONF_ZONES]
    # Zone dicts followed their heads: old zone 2 is now priority slot 0.
    assert zones[0][ZONE_CLIMATE] == heads[1].entity_id
    assert zones[0][ZONE_NAME] == "Zone 2"
    assert zones[1][ZONE_NAME] == "Zone 1"
    assert entry.unique_id == f"{heads[1].entity_id}|{heads[0].entity_id}"


async def test_dropped_zone_entities_pruned(hass: HomeAssistant) -> None:
    """Shrinking the zone list removes the dropped zone's registry entities."""
    heads = [MockSingleSetpointHead(c) for c in "abc"]
    await _platform(hass, heads)
    entry = await _add_entry(hass, heads)
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_zone_3_target")

    # Reconfigure down to two zones (direct data update + reload).
    await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(
        entry, data={CONF_ZONES: _zones(heads[:2])}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert reg.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_zone_3_target") is None
    assert reg.async_get_entity_id("climate", DOMAIN, f"{entry.entry_id}_zone_3_thermostat") is None
    # survivors intact
    assert reg.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_primary_target")
