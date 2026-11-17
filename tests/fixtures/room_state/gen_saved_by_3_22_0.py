"""Run an install on 3.22.0, stop Home Assistant, and write what it saved as JSON.

Run from a checkout of tag v3.22.0, with this file copied into its tests/ directory:

    git worktree add /tmp/mxz-3.22.0 v3.22.0
    cp tests/fixtures/room_state/gen_saved_by_3_22_0.py /tmp/mxz-3.22.0/tests/
    cd /tmp/mxz-3.22.0 && MXZ_FIXTURE_OUT=$OLDPWD/tests/fixtures/room_state \\
        python -m pytest -p no:cacheprovider -q tests/gen_saved_by_3_22_0.py

What it saves: the restore data of every entity and the entry's two stores (the
mode-change record and the door/window record), as a clean stop leaves them. The
Study: target 72, drift 3, a fan picked at the head (held), its window open and the
room paused. The Den: dual setpoint 66 to 76, hands-off with a freeze floor at 50,
disabled, and the floor running at 45. Eco on, the shared mode set to heat. Every
head, sensor and window is invented.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import (
    CONF_ZONES,
    DOMAIN,
    ZONE_DUAL_SETPOINT,
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
)
from tests.property_rig import OPEN_DELAY, open_rig
from tests.restart import stopped

OUT = Path(os.environ["MXZ_FIXTURE_OUT"])


async def _number(rig: Any, suffix: str, value: float) -> None:
    await rig.hass.services.async_call(
        "number", "set_value", {"entity_id": rig.eid(suffix), "value": value}, blocking=True
    )
    await rig.hass.async_block_till_done()
    await rig.settle()


async def test_save_an_install_with_3_22_0(hass: HomeAssistant, freezer: Any, hass_storage: dict) -> None:
    freezer.move_to("2026-11-13 18:00:00+00:00")
    rig = await open_rig(hass, freezer, window="off")
    entry = rig.entry
    zones = [dict(zone) for zone in entry.data[CONF_ZONES]]
    zones[1] |= {ZONE_HANDS_OFF: True, ZONE_FLOOR_LOW: 50, ZONE_DUAL_SETPOINT: True}
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONES: zones})
    await rig.reload()
    await rig.later(60)
    await _number(rig, "_primary_target", 72)
    await _number(rig, "_primary_drift", 3)
    await _number(rig, "_secondary_target_low", 66)
    await _number(rig, "_secondary_target_high", 76)
    await rig.switch("_eco_idle", True)
    await rig.select_mode("heat")
    await rig.person_fan("high", 0)
    assert rig.fan_held(0)
    await rig.switch("_secondary_enable", False)
    await rig.temp(1, 45)
    assert rig.row(1)["floor_state"] == "heat", rig.row(1)
    await rig.window("on")
    await rig.later(OPEN_DELAY + 1)
    assert rig.coord.openings.state_of("primary") == "paused"
    heads = {head.entity_id: hass.states.get(head.entity_id).as_dict() for head in rig.heads}
    async with stopped(hass, entry):
        storage = {
            key: value for key, value in hass_storage.items()
            if key == "core.restore_state" or key.startswith(f"{DOMAIN}.{entry.entry_id}.")
        }
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "saved_by_3_22_0.json").write_text(json.dumps({
            "saved_by": "3.22.0",
            "entry": {
                "entry_id": entry.entry_id,
                "created_at": entry.created_at.isoformat(),
                "title": entry.title,
                "version": entry.version,
                "minor_version": entry.minor_version,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "heads": {
                entity_id: {"state": state["state"], "fan_mode": state["attributes"].get("fan_mode")}
                for entity_id, state in heads.items()
            },
            "storage": storage,
        }, indent=1, sort_keys=True, default=str) + "\n")
    await rig.close()
