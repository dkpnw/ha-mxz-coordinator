"""Each apply commands exactly the heads it did, the same way, before its gates moved.

Each scenario in ``tests/apply_parity.py`` runs the real integration and is compared, byte for
byte, with its recording in ``tests/fixtures/apply_parity/``, captured on d4cffd2 with the gates
still inside ``MXZCoordinator._apply``. ``MXZ_PARITY_CAPTURE=1`` rewrites the recordings.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from tests.apply_parity import SCENARIOS, Applies, recorded

CAPTURE = os.environ.get("MXZ_PARITY_CAPTURE") == "1"


async def _run(hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str) -> Applies:
    house = Applies(hass, freezer, monkeypatch)
    await SCENARIOS[name](house)
    return house


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_each_apply_commands_what_it_did_on_the_base(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str
) -> None:
    house = await _run(hass, freezer, monkeypatch, name)
    rendered = house.rendered()
    if CAPTURE:
        recorded(name).parent.mkdir(parents=True, exist_ok=True)
        recorded(name).write_text(rendered)
    steps = json.loads(recorded(name).read_text())
    assert [step["step"] for step in house.steps] == [step["step"] for step in steps]
    for mine, theirs in zip(house.steps, steps, strict=True):
        assert json.dumps(mine, indent=1) == json.dumps(theirs, indent=1), mine["step"]
    assert rendered == recorded(name).read_text()


def _calls(step: dict[str, Any]) -> list[str]:
    return [call["call"] for call in step["calls"]]


@pytest.mark.parametrize("change", ["write_back_late", "kick_sent", "paused_not_off", "seed_lost"])
async def test_the_recording_catches_one_changed_value(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, change: str
) -> None:
    """The comparison fails on the mode written back after the heads, a head mid vane-kick
    sent the park, a paused head left on, or a restart that forgets the coil-dry run."""
    name = {
        "write_back_late": "dry",
        "kick_sent": "coordinator_and_standby",
        "paused_not_off": "pause",
        "seed_lost": "coil_dry_restart",
    }[change]
    house = await _run(hass, freezer, monkeypatch, name)
    assert house.rendered() == recorded(name).read_text()
    steps = {step["step"]: step for step in house.steps}
    if change == "write_back_late":
        calls = steps["heat_parks_dry"]["calls"]
        marker = next(
            call for call in calls
            if call["call"] == "mxz.write_back" and call["data"]["shared_mode"] == "heat"
        )
        assert calls.index(marker) < max(
            i for i, call in enumerate(calls) if call["call"].startswith("climate.")
        )
        calls.remove(marker)
        calls.append(marker)
    elif change == "kick_sent":
        calls = steps["standby_skips_the_head_mid_kick"]["calls"]
        assert not any(call["data"].get("entity_id") == "climate.mock_head_parity_2" for call in calls)
        calls.append(
            {"call": "climate.set_hvac_mode",
             "data": {"entity_id": "climate.mock_head_parity_2", "hvac_mode": "fan_only"}}
        )
    elif change == "paused_not_off":
        step = steps["paused_off"]
        assert step["heads"][0]["state"] == "off"
        step["heads"][0]["state"] = "cool"
    else:
        stamps = steps["after_restart"]["mode"]["coil_dry"]
        assert "climate.mock_head_parity_0" in stamps
        del stamps["climate.mock_head_parity_0"]
    assert house.rendered() != recorded(name).read_text()
