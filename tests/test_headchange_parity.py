"""Accept changes made at the head adopts exactly what it did before its rule moved.

Each scenario in ``tests/headchange_parity.py`` runs the real integration and is compared, byte
for byte, with its recording in ``tests/fixtures/headchange_parity/``, captured on 7ebcf84 with
the rule still inside ``MXZCoordinator._head_adoption``. ``MXZ_PARITY_CAPTURE=1`` rewrites the
recordings.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from tests.headchange_parity import SCENARIOS, Presses, recorded

CAPTURE = os.environ.get("MXZ_PARITY_CAPTURE") == "1"


async def _run(hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str) -> Presses:
    presses = Presses(hass, freezer, monkeypatch)
    try:
        await SCENARIOS[name](presses)
        await presses.rig.close()
    finally:
        presses.close()
    return presses


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_a_change_at_the_head_is_adopted_as_it_was_on_the_base(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str
) -> None:
    presses = await _run(hass, freezer, monkeypatch, name)
    rendered = presses.rendered()
    if CAPTURE:
        recorded(name).parent.mkdir(parents=True, exist_ok=True)
        recorded(name).write_text(rendered)
    steps = json.loads(recorded(name).read_text())
    assert [step["step"] for step in presses.steps] == [step["step"] for step in steps]
    for mine, theirs in zip(presses.steps, steps, strict=True):
        assert mine == theirs, mine["step"]
    assert rendered == recorded(name).read_text()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("room", "target"), "70.0"),
        (("hold",), True),
        (("log",), []),
    ],
    ids=["target", "hold", "log"],
)
async def test_the_recording_catches_one_changed_value(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, path: tuple[Any, ...], value: Any
) -> None:
    """The comparison fails on one changed target, adoption hold or adoption log line."""
    presses = await _run(hass, freezer, monkeypatch, "setpoint_change")
    assert presses.rendered() == recorded("setpoint_change").read_text()
    record: Any = next(step for step in presses.steps if step["step"] == "adopted")
    for key in path[:-1]:
        record = record[key]
    assert record[path[-1]] != value
    record[path[-1]] = value
    assert presses.rendered() != recorded("setpoint_change").read_text()
