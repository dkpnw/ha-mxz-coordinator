"""The planner decides and sends exactly what it did before it left the coordinator.

Each scenario in ``tests/planner_parity.py`` runs the real integration and is compared,
byte for byte, with its recording in ``tests/fixtures/planner_parity/``, captured on
9322c5d with the decision still inside ``MXZCoordinator``. ``MXZ_PARITY_CAPTURE=1``
rewrites the recordings.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from tests.planner_parity import SCENARIOS, House, recorded

CAPTURE = os.environ.get("MXZ_PARITY_CAPTURE") == "1"


async def _run(hass: HomeAssistant, freezer: Any, name: str) -> House:
    house = House(hass, freezer)
    await SCENARIOS[name](house)
    return house


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_planner_decides_and_sends_what_it_did_on_the_base(
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


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("plan", "state"), "fan_only"),
        (("calls", 0, "data", "target_temp_high"), 99.0),
        (("waits",), [["dwell", None, 1.0]]),
        (("mode", "engage_latch", "primary"), "heat"),
    ],
    ids=["plan", "command", "wait", "latch"],
)
async def test_the_recording_catches_one_changed_value(
    hass: HomeAssistant, freezer: Any, path: tuple[Any, ...], value: Any
) -> None:
    """The comparison fails on one changed plan field, head command, wait or latch."""
    house = await _run(hass, freezer, "mixed_demand")
    assert house.rendered() == recorded("mixed_demand").read_text()
    step = next(
        step for step in house.steps
        if step["calls"] and step["waits"] and step["mode"]["engage_latch"].get("primary")
    )
    record = step
    for key in path[:-1]:
        record = record[key]
    assert record[path[-1]] != value
    record[path[-1]] = value
    assert house.rendered() != recorded("mixed_demand").read_text()
