"""Start the 3.22.0 install on this release, change it, stop, and write what is saved as JSON.

Run from this repository, then check it on 3.22.0 (rollback_check_on_3_22_0.py):

    cp tests/fixtures/room_state/gen_saved_by_this_release.py tests/
    MXZ_FIXTURE_OUT=tests/fixtures/room_state \\
        python -m pytest -p no:cacheprovider -q tests/gen_saved_by_this_release.py
    rm tests/gen_saved_by_this_release.py

The changes, made after the migration: the Study's target 73, the shared mode cool, the
window closed (the pause ends and gives the person's fan back), the Den's range 65 to 77, and
heat lockout on. Every head, sensor and window is invented.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import DOMAIN
from tests.restart import stopped
from tests.test_room_state_migration import ENTRY, _saved, _start_saved_install

OUT = Path(os.environ["MXZ_FIXTURE_OUT"])


async def _number(rig: Any, suffix: str, value: float) -> None:
    await rig.hass.services.async_call(
        "number", "set_value", {"entity_id": rig.eid(suffix), "value": value}, blocking=True
    )
    await rig.hass.async_block_till_done()
    await rig.settle()


async def test_save_with_this_release(hass: HomeAssistant, freezer: Any, hass_storage: dict) -> None:
    rig = await _start_saved_install(hass, freezer, hass_storage, _saved())
    await rig.later(60)
    await _number(rig, "_primary_target", 73)
    await rig.select_mode("cool")
    await rig.window("off")
    await rig.later(200)
    await _number(rig, "_secondary_target_low", 65)
    await _number(rig, "_secondary_target_high", 77)
    await rig.switch("_heat_lockout", True)
    await rig.later(1)
    assert rig.fan_held(0) and rig.heads[0].fan == "high"
    heads = {head.entity_id: hass.states.get(head.entity_id).as_dict() for head in rig.heads}
    async with stopped(hass, rig.entry):
        storage = {
            key: value for key, value in hass_storage.items()
            if key == "core.restore_state" or key.startswith(f"{DOMAIN}.{ENTRY['entry_id']}.")
        }
        (OUT / "saved_by_this_release.json").write_text(json.dumps({
            "saved_by": "store1111 (Unreleased)",
            "entry": ENTRY,
            "heads": {
                entity_id: {"state": state["state"], "fan_mode": state["attributes"].get("fan_mode")}
                for entity_id, state in heads.items()
            },
            "storage": storage,
        }, indent=1, sort_keys=True, default=str) + "\n")
    await rig.close()
