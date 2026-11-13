"""Save an MXZ Coordinator entry with the real v2.20.0 flows (the v1 flat layout), as JSON.

v2.20.0 is the last release before the zones list (3.0.0), so its entry is the
oldest layout this integration still migrates. Run from a checkout of tag
v2.20.0, with this file copied into its tests/ directory:

    git worktree add /tmp/mxz-2.20.0 v2.20.0
    cp tests/fixtures/migration/gen_saved_by_2_20_0.py /tmp/mxz-2.20.0/tests/
    cd /tmp/mxz-2.20.0 && MXZ_FIXTURE_OUT=$OLDPWD/tests/fixtures/migration \
        python -m pytest -p no:cacheprovider -q tests/gen_saved_by_2_20_0.py

Every head, sensor and service here is invented. Nothing talks to a device.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from homeassistant import config_entries
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.mxz_coordinator.const import DOMAIN

OUT = Path(os.environ["MXZ_FIXTURE_OUT"])


async def test_save_entry_with_2_20_0(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    for head in ("climate.living", "climate.bedroom"):
        hass.states.async_set(head, "off", {
            "hvac_modes": ["off", "cool", "heat", "fan_only"],
            "supported_features": int(ClimateEntityFeature.FAN_MODE),
            "fan_modes": ["auto", "quiet", "low", "medium", "middle", "high"],
        })
    for sensor in ("sensor.living_temp", "sensor.bedroom_temp"):
        hass.states.async_set(sensor, "70", {"device_class": "temperature", "unit_of_measurement": "°F"})
    async_mock_service(hass, "notify", "phone")

    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await flow.async_configure(result["flow_id"], {
        "primary_climate": "climate.living", "secondary_climate": "climate.bedroom",
        "primary_sensor": "sensor.living_temp", "secondary_sensor": "sensor.bedroom_temp",
        "notify_service": "notify.phone",
    })
    assert result["step_id"] == "tuning", result
    result = await flow.async_configure(result["flow_id"], {
        "demand_threshold": 2.0, "engage_deadband": 1.5, "mode_hysteresis": 300,
        "resting_mode_bias": "heat", "fan_boost_max": "medium",
    })
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    # Configure: the 2.x vane and airflow overrides, stored flat.
    options = hass.config_entries.options
    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(result["flow_id"], {
        "primary_vane_vertical": "select.living_vane_vertical",
        "secondary_vane_horizontal": "select.bedroom_vane_horizontal",
        "secondary_stage": "sensor.bedroom_stage",
        "clamp_min": 61,
    })
    await hass.async_block_till_done()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "saved_by_2_20_0.json").write_text(json.dumps({
        "saved_by": "2.20.0",
        "version": entry.version,
        "minor_version": entry.minor_version,
        "title": entry.title,
        "unique_id": entry.unique_id,
        "data": dict(entry.data),
        "options": dict(entry.options),
    }, indent=1, sort_keys=True) + "\n")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
