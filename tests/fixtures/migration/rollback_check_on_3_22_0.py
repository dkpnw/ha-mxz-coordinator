"""A rollback to 3.22.0: it starts on what this release saved and shows every value as it was.

Run from a checkout of tag v3.22.0, with this file copied into its tests/ directory:

    git worktree add /tmp/mxz-3.22.0 v3.22.0
    cp tests/fixtures/migration/rollback_check_on_3_22_0.py /tmp/mxz-3.22.0/tests/
    cd /tmp/mxz-3.22.0 && MXZ_FIXTURES=$OLDPWD/tests/fixtures/room_state \\
        python -m pytest -p no:cacheprovider -q tests/rollback_check_on_3_22_0.py

saved_by_this_release.json is what gen_saved_by_this_release.py left: the room state store,
which 3.22.0 doesn't read, beside the restore data and the two files 3.22.0 does. Every head,
sensor and window is invented.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_load_restore_state_from_storage,
)

from custom_components.mxz_coordinator.const import DOMAIN
from tests.cloud_heads import register
from tests.property_rig import _HEADS, SENSORS, WINDOW, Rig, RigHead
from tests.test_drive import _set_temp

SAVED = json.loads((Path(os.environ["MXZ_FIXTURES"]) / "saved_by_this_release.json").read_text())
ENTRY = SAVED["entry"]


async def test_3_22_0_reads_what_this_release_left(
    hass: HomeAssistant, freezer: Any, hass_storage: dict
) -> None:
    freezer.move_to("2026-11-13 19:06:00+00:00")  # inside the 10-minute hold of the 19:01 change
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [RigHead("a"), RigHead("b")]
    await register(hass, {"esphome": heads})
    hass.data[_HEADS] = heads
    for head in heads:
        head.reset()
        head.person_mode(SAVED["heads"][head.entity_id]["state"])
        head.person_fan(SAVED["heads"][head.entity_id]["fan_mode"])
    await _set_temp(hass, SENSORS[0], 70)
    await _set_temp(hass, SENSORS[1], 45)
    hass.states.async_set(WINDOW, "off", {"friendly_name": "Property window"})
    hass_storage.update(deepcopy(SAVED["storage"]))
    await async_mock_load_restore_state_from_storage(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title=ENTRY["title"], data=ENTRY["data"], options=ENTRY["options"],
        version=ENTRY["version"], minor_version=ENTRY["minor_version"], entry_id=ENTRY["entry_id"],
    )
    entry.add_to_hass(hass)
    entry.created_at = datetime.fromisoformat(ENTRY["created_at"])
    hass.set_state(CoreState.not_running)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.set_state(CoreState.starting)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    rig = Rig(hass, freezer, entry, heads)
    await rig.later(0)
    study, den = rig.row(0), rig.row(1)
    assert (study["target"], study["drift"], study["enabled"]) == (73, 3, True)
    assert hass.states.get(rig.eid("_primary_drift")).attributes["override"] is True
    assert rig.fan_held(0) and rig.heads[0].fan == "high"
    assert (den["enabled"], den["target_low"], den["target_high"]) == (False, 65, 77)
    # The run this release saved, not a new one: a new run starts with nothing driven.
    assert rig.coord.floor_record("secondary")["floor_run"]["driven"] >= 121
    assert rig.coord.eco_idle is True and rig.coord.heat_lockout is True
    assert rig.coord.coordinator_enable is True
    assert rig.coord.current_shared_mode == "cool"
    assert rig.coord.openings.state_of("primary") == "closed"
    await rig.close()
