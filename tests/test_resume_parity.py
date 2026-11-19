"""A start resumes exactly what it did before resume left the coordinator.

Each scenario in ``tests/resume_parity.py`` runs the real integration and is compared, byte for
byte, with its recording in ``tests/fixtures/resume_parity/``, captured on 6b7ad3b with resume
still inside ``MXZCoordinator``. ``MXZ_PARITY_CAPTURE=1`` rewrites the recordings.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from tests.resume_parity import SCENARIOS, Starts, recorded

CAPTURE = os.environ.get("MXZ_PARITY_CAPTURE") == "1"


async def _run(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, hass_storage: dict, name: str
) -> Starts:
    starts = Starts(hass, freezer, monkeypatch, hass_storage)
    try:
        await SCENARIOS[name](starts)
        await starts.rig.close()
    finally:
        starts.detach()
    return starts


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_a_start_resumes_what_it_did_on_the_base(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, hass_storage: dict, name: str
) -> None:
    starts = await _run(hass, freezer, monkeypatch, hass_storage, name)
    rendered = starts.rendered()
    if CAPTURE:
        recorded(name).parent.mkdir(parents=True, exist_ok=True)
        recorded(name).write_text(rendered)
    steps = json.loads(recorded(name).read_text())
    assert [step["step"] for step in starts.steps] == [step["step"] for step in steps]
    for mine, theirs in zip(json.loads(rendered), steps, strict=True):
        assert mine == theirs, mine["step"]
    assert rendered == recorded(name).read_text()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("coordinator", "rooms", 0, "target"), 71.0),
        (("resumed", 0, "startup"), []),
        (("coordinator", "mode"), "heat"),
        (("coordinator", "fan"), {}),
        (("asked",), []),
    ],
    ids=["target", "startup_seed", "mode", "fan_hold", "resume_calls"],
)
async def test_the_recording_catches_one_changed_value(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, hass_storage: dict,
    path: tuple[Any, ...], value: Any,
) -> None:
    """The comparison fails on one changed target, startup seed, mode, fan hold or resume call."""
    starts = await _run(hass, freezer, monkeypatch, hass_storage, "startup_seed")
    assert starts.rendered() == recorded("startup_seed").read_text()
    record: Any = next(step for step in starts.steps if step["step"] == "run_resumed")
    for key in path[:-1]:
        record = record[key]
    assert record[path[-1]] != value
    record[path[-1]] = value
    assert starts.rendered() != recorded("startup_seed").read_text()
