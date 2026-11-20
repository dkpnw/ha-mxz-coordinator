"""Each local head is sent exactly what it was, in the same calls, before its send shaping moved.

Each scenario in ``tests/send_parity.py`` runs the real integration and is compared, byte for
byte, with its recording in ``tests/fixtures/send_parity/``, captured on d4cffd2 with the send
shaping still inside ``LocalDriver``. ``MXZ_PARITY_CAPTURE=1`` rewrites the recordings.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from tests.send_parity import SCENARIOS, Sends, recorded

CAPTURE = os.environ.get("MXZ_PARITY_CAPTURE") == "1"


async def _run(hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str) -> Sends:
    house = Sends(hass, freezer, monkeypatch)
    await SCENARIOS[name](house)
    return house


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_each_head_is_sent_what_it_was_on_the_base(
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


def _sent(step: dict[str, Any], head: str) -> list[dict[str, Any]]:
    return [call for call in step["calls"] if call["data"].get("entity_id") == head]


@pytest.mark.parametrize("change", ["one_call", "not_clamped", "resent", "memory"])
async def test_the_recording_catches_one_changed_value(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, change: str
) -> None:
    """The comparison fails on one call where there were two, an edge past the head's band, a
    write the head already showed, or a write left out of MXZ's memory."""
    house = await _run(hass, freezer, monkeypatch, "fahrenheit_house")
    assert house.rendered() == recorded("fahrenheit_house").read_text()
    steps = {step["step"]: step for step in house.steps}
    if change == "one_call":
        sent = _sent(steps["targets_at_the_top"], "climate.mock_head_parity_1")
        heat = next(
            call for call in sent
            if call["call"] == "climate.set_hvac_mode" and call["data"]["hvac_mode"] == "heat"
        )
        after = sent[sent.index(heat) + 1]
        assert after["call"] == "climate.set_temperature" and "hvac_mode" not in after["data"]
        steps["targets_at_the_top"]["calls"].remove(heat)
    elif change == "not_clamped":
        sent = _sent(steps["targets_at_the_top"], "climate.mock_head_parity_1")
        data = next(call["data"] for call in sent if "target_temp_high" in call["data"])
        assert data["target_temp_high"] <= 84
        data["target_temp_high"] = 85
    elif change == "resent":
        assert _sent(steps["same_command_again"], "climate.mock_head_parity_0") == []
        steps["same_command_again"]["calls"].append(
            {"call": "climate.set_temperature", "data": {"entity_id": "climate.mock_head_parity_0"}}
        )
    else:
        memory = steps["cool_call"]["memory"]
        assert memory
        memory.pop(next(iter(memory)))
    assert house.rendered() != recorded("fahrenheit_house").read_text()
