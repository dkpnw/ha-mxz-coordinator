"""Save MXZ Coordinator entries with the real 3.22.0 flows, and write them out as JSON.

Run from a checkout of tag v3.22.0, with this file copied into its tests/ directory:

    git worktree add /tmp/mxz-3.22.0 v3.22.0
    cp tests/fixtures/migration/gen_saved_by_3_22_0.py /tmp/mxz-3.22.0/tests/
    cd /tmp/mxz-3.22.0 && MXZ_FIXTURE_OUT=$OLDPWD/tests/fixtures/migration \
        python -m pytest -p no:cacheprovider -q tests/gen_saved_by_3_22_0.py

Every head, sensor and service here is invented. Nothing talks to a device.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from homeassistant import config_entries
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.mxz_coordinator.const import DOMAIN
from tests.flow_pages import open_options_page, prefilled, press, setup_tuning, submit

OUT = Path(os.environ["MXZ_FIXTURE_OUT"])
HEADS = ["climate.living", "climate.bedroom", "climate.office"]
SENSORS = ["sensor.living_temp", "sensor.bedroom_temp", "sensor.office_temp"]


def _household(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    # The office head comes from MELCloud, so setup suggests cloud for it.
    er.async_get(hass).async_get_or_create(
        "climate", "melcloud", "office-unit", suggested_object_id="office"
    )
    for head in HEADS:
        hass.states.async_set(head, "off", {
            "hvac_modes": ["off", "cool", "heat", "fan_only", "dry"],
            "supported_features": int(ClimateEntityFeature.FAN_MODE),
            "fan_modes": ["auto", "quiet", "low", "medium", "middle", "high"],
            "friendly_name": head.split(".")[1].title(),
        })
    for sensor, value in zip(SENSORS, ("70.5", "67.0", "69.0"), strict=True):
        hass.states.async_set(sensor, value, {"device_class": "temperature", "unit_of_measurement": "°F"})
    for entity in ("binary_sensor.grid", "binary_sensor.living_window", "weather.home"):
        hass.states.async_set(entity, "off")
    async_mock_service(hass, "notify", "phone")


def _dump(name: str, entry: config_entries.ConfigEntry) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps({
        "saved_by": "3.22.0",
        "version": entry.version,
        "minor_version": entry.minor_version,
        "title": entry.title,
        "unique_id": entry.unique_id,
        "data": dict(entry.data),
        "options": dict(entry.options),
    }, indent=1, sort_keys=True) + "\n")


async def _setup(hass: HomeAssistant) -> config_entries.ConfigEntry:
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await submit(flow, result, {
        "heads": HEADS, "entry_title": "Upstairs", "notify_service": "notify.phone",
    })
    result = await submit(flow, result, {"room_name_1": "Living", "room_name_2": "Bedroom", "room_name_3": "Office"})
    assert result["step_id"] == "head_type", result
    result = await submit(flow, result, {"head_type_3": "cloud"})
    result = await submit(flow, result, {f"sensor_{i + 1}": s for i, s in enumerate(SENSORS)})
    result = await setup_tuning(hass, result, {
        "engage_deadband": 1.5, "demand_threshold": 2.5, "accept_head_changes": False,
        "mode_hysteresis": 900, "drift_cooler_by": 2.0, "drift_warmer_by": 1.0,
        "lost_vote_ttl": 45, "comfort_priority_stale_hours": 0,
        "fan_boost_max": "middle", "idle_action": "off", "coil_dry_minutes": 15,
        "changeover_entity": "weather.home", "changeover_heat_above": 66, "changeover_cool_below": 52,
        "heat_lockout_floor": 57, "cool_lockout_ceiling": 81,
        "eco_heat_min": 52, "eco_cool_max": 79, "clamp_min": 60, "clamp_max": 86,
        "inhibit_entity": "binary_sensor.grid", "inhibit_active_state": "off", "inhibit_action": "fan_only",
    })
    assert result["step_id"] == "review", result
    result = await press(flow, result, "finish")
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    return hass.config_entries.async_entries(DOMAIN)[0]


async def _configure(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> None:
    options = hass.config_entries.options
    result = await open_options_page(hass, entry, "room_1")
    result = await submit(options, result, {
        **prefilled(result),
        "hands_off": True, "hands_off_vote_basis": "action", "allow_dry": True,
        "room_cooler_by": 1.0, "floor_low": 50, "floor_high": 85, "dual_setpoint": True,
        "report_interval": 5, "evidence_basis": "ha_state_write",
        "opening_entity": "binary_sensor.living_window", "opening_pause_enable": True,
        "opening_open_delay": 30, "opening_notify": True,
    })
    await hass.async_block_till_done()
    result = await open_options_page(hass, entry, "room_3")
    result = await submit(options, result, {
        **prefilled(result), "drive_offset": 3, "write_interval": 120,
    })
    await hass.async_block_till_done()
    # Clear one uneven drift side on Comfort: 3.22.0 removes the key.
    result = await open_options_page(hass, entry, "comfort")
    flat = prefilled(result)
    flat.pop("drift_warmer_by", None)
    await submit(options, result, flat)
    await hass.async_block_till_done()


async def test_save_entries_with_3_22_0(hass: HomeAssistant) -> None:
    _household(hass)
    entry = await _setup(hass)
    await _configure(hass, entry)
    _dump("saved_by_3_22_0_fahrenheit", entry)
    # A live switch to °C: 3.22.0 converts what is saved and records the unit.
    await hass.config.async_update(unit_system="metric")
    await hass.async_block_till_done()
    assert hass.config.units.temperature_unit == "°C"
    _dump("saved_by_3_22_0_switched_to_celsius", entry)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
