"""Each head's delivery sends exactly what it did, in the same order, before its steps moved.

Each scenario in ``tests/delivery_parity.py`` runs the real integration and is compared, byte
for byte, with its recording in ``tests/fixtures/delivery_parity/``, captured on 7ebcf84 with the
steps still inside ``LocalDriver.async_deliver``. ``MXZ_PARITY_CAPTURE=1`` rewrites the
recordings.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from tests.delivery_parity import SCENARIOS, Deliveries, recorded

CAPTURE = os.environ.get("MXZ_PARITY_CAPTURE") == "1"


async def _run(hass: HomeAssistant, freezer: Any, name: str) -> Deliveries:
    house = Deliveries(hass, freezer)
    await SCENARIOS[name](house)
    return house


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_each_delivery_sends_what_it_did_on_the_base(
    hass: HomeAssistant, freezer: Any, name: str
) -> None:
    house = await _run(hass, freezer, name)
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


@pytest.mark.parametrize("change", ["order", "stamp", "armed"])
async def test_the_recording_catches_one_changed_value(
    hass: HomeAssistant, freezer: Any, change: str
) -> None:
    """The comparison fails on a hand-back sent after the off, a moved coil-dry stamp, or a
    coil-dry wait left armed."""
    name = "parked_off" if change == "order" else "coil_dry"
    house = await _run(hass, freezer, name)
    assert house.rendered() == recorded(name).read_text()
    if change == "order":
        step = next(s for s in house.steps if s["step"] == "satisfied_after_cooling")
        assert _calls(step)[:2] == ["climate.set_fan_mode", "climate.set_hvac_mode"]
        step["calls"][:2] = step["calls"][1::-1]
    elif change == "stamp":
        step = next(s for s in house.steps if s["step"] == "drying")
        stamps = step["mode"]["coil_dry"]
        head = next(iter(stamps))
        stamps[head][1] += 1
    else:
        step = next(s for s in house.steps if s["step"] == "cooling_again_ends_the_dry_wait")
        assert not any(wait == "dry" for wait, _ in step["armed"])
        step["armed"].append(["dry", "climate.mock_head_parity_0"])
    assert house.rendered() != recorded(name).read_text()
