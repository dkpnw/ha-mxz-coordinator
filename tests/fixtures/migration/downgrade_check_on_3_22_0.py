"""A rollback to 3.22.0: it loads each migrated (v2.3) entry and runs it as the saved one.

Run from a checkout of tag v3.22.0, with this file copied into its tests/ directory:

    git worktree add /tmp/mxz-3.22.0 v3.22.0
    cp tests/fixtures/migration/downgrade_check_on_3_22_0.py /tmp/mxz-3.22.0/tests/
    cd /tmp/mxz-3.22.0 && MXZ_FIXTURES=$OLDPWD/tests/fixtures/migration \
        python -m pytest -p no:cacheprovider -q tests/downgrade_check_on_3_22_0.py

For each saved entry, 3.22.0 runs the entry as it was saved and then the same entry as
this repository migrates it (migrated/, held to the current code by
tests/test_one_home_migration.py), and must decide, store and send the same: the
coordinator's settings, its rooms, the plan, and every head command. It must also
accept v2.3 unchanged. Every head and sensor is invented.
"""
import dataclasses
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
    mock_restore_cache,
)

from custom_components.mxz_coordinator.const import DOMAIN
from tests.test_drive import _eid, _recompute

SAVED = Path(os.environ["MXZ_FIXTURES"])
MIGRATED = SAVED / "migrated"
NAMES = ("saved_by_2_20_0", "saved_by_3_22_0_fahrenheit", "saved_by_3_22_0_switched_to_celsius")


def household(hass, saved):
    rooms = saved["data"].get("zones") or [
        {"climate": saved["data"][f"{s}_climate"], "sensor": saved["data"][f"{s}_sensor"]}
        for s in ("primary", "secondary")]
    celsius = {**saved["data"], **saved["options"]}.get("saved_temp_unit") == "°C"
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    for zone in rooms:
        hass.states.async_set(zone["climate"], "off", {
            "hvac_modes": ["off", "cool", "heat", "fan_only", "dry"],
            "supported_features": int(ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TARGET_TEMPERATURE),
            "fan_modes": ["auto", "quiet", "low", "medium", "middle", "high"],
            "temperature": 21 if celsius else 70, "min_temp": 16 if celsius else 61, "max_temp": 31 if celsius else 88})
        hass.states.async_set(zone["sensor"], "21" if celsius else "70",
                              {"device_class": "temperature", "unit_of_measurement": "°C" if celsius else "°F"})
    return rooms, celsius


async def run(hass, freezer, saved):
    freezer.move_to("2026-11-13 18:00:00+00:00")
    mock_restore_cache(hass, ())
    rooms, celsius = household(hass, saved)
    entry = MockConfigEntry(domain=DOMAIN, title=saved["title"], unique_id=saved["unique_id"],
                            version=saved["version"], minor_version=saved["minor_version"],
                            data=deepcopy(saved["data"]), options=deepcopy(saved["options"]),
                            entry_id="pinned_entry")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    version = (entry.version, entry.minor_version)
    calls = [async_mock_service(hass, "climate", s) for s in
             ("set_hvac_mode", "set_temperature", "set_fan_mode", "turn_off", "turn_on")]
    hass.states.async_set(rooms[0]["sensor"], "17" if celsius else "62",
                          {"device_class": "temperature", "unit_of_measurement": "°C" if celsius else "°F"})
    for suffix in ("_coordinator_enable", "_primary_enable", "_secondary_enable"):
        await hass.services.async_call("switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True)
    await _recompute(hass, entry)
    await hass.async_block_till_done()
    coord = entry.runtime_data
    out = {
        "settings": {k: v for k, v in vars(coord).items() if not k.startswith("_")
                     and isinstance(v, (bool, int, float, str, tuple, type(None)))},
        "rooms": [dataclasses.asdict(z) for z in coord.zones],
        "plan": dict(hass.states.get(_eid(hass, entry, "_plan")).attributes),
        "sent": [(c.service, dict(c.data)) for r in calls for c in r],
    }
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    return version, out


@pytest.mark.parametrize("name", NAMES)
async def test_3_22_0_runs_the_migrated_entry_as_the_saved_one(hass, freezer, name):
    saved = json.loads((SAVED / f"{name}.json").read_text())
    migrated = json.loads((MIGRATED / f"{name}.json").read_text())
    assert migrated["minor_version"] == 3
    _, before = await run(hass, freezer, saved)
    version, after = await run(hass, freezer, migrated)
    assert version == (2, 3)  # 3.22.0 accepts it and leaves the version alone
    assert after == before
    assert after["sent"]
