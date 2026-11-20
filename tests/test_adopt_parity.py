"""The adopt hold arms, re-arms, cancels and ends exactly as it did before its transitions moved.

Each scenario in ``tests/adopt_parity.py`` runs the real integration and is compared, byte for
byte, with its recording in ``tests/fixtures/adopt_parity/``, captured on d4cffd2 with the hold
still run by ``MXZCoordinator._watch_head_change`` and ``_async_adopt_head_change``.
``MXZ_PARITY_CAPTURE=1`` rewrites the recordings.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from tests.adopt_parity import SCENARIOS, Holds, recorded

CAPTURE = os.environ.get("MXZ_PARITY_CAPTURE") == "1"


async def _run(hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str) -> Holds:
    holds = Holds(hass, freezer, monkeypatch)
    try:
        await SCENARIOS[name](holds)
        await holds.rig.close()
    finally:
        holds.close()
    return holds


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_hold_runs_as_it_did_on_the_base(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str
) -> None:
    holds = await _run(hass, freezer, monkeypatch, name)
    rendered = holds.rendered()
    if CAPTURE:
        recorded(name).parent.mkdir(parents=True, exist_ok=True)
        recorded(name).write_text(rendered)
    steps = json.loads(recorded(name).read_text())
    assert [step["step"] for step in holds.steps] == [step["step"] for step in steps]
    for mine, theirs in zip(holds.steps, steps, strict=True):
        assert json.dumps(mine, indent=1) == json.dumps(theirs, indent=1), mine["step"]
    assert rendered == recorded(name).read_text()


@pytest.mark.parametrize("change", ["no_rearm", "wrong_number", "no_refresh", "seed_kept"])
async def test_the_recording_catches_one_changed_value(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, change: str
) -> None:
    """The comparison fails on a second press that doesn't re-arm the hold, a dual room's press
    written to the other number, a hold's end that asks for no refresh, or a reported off that
    leaves the startup seed."""
    name = {
        "no_rearm": "setpoint_change", "wrong_number": "dual_room",
        "no_refresh": "setpoint_change", "seed_kept": "startup_off",
    }[change]
    holds = await _run(hass, freezer, monkeypatch, name)
    assert holds.rendered() == recorded(name).read_text()
    steps = {step["step"]: step for step in holds.steps}
    if change == "no_rearm":
        clock = steps["pressed_again_inside_the_hold"]["clock"]
        assert any(call[0] == "call_later" and "adopt" in str(call[1]) for call in clock)
        clock[:] = [call for call in clock if not (call[0] == "call_later" and "adopt" in str(call[1]))]
    elif change == "wrong_number":
        calls = steps["cool_to_adopted"]["calls"]
        call = next(call for call in calls if call["call"] == "number.set_value")
        assert call["data"]["entity_id"].endswith("_cool_to")
        call["data"]["entity_id"] = call["data"]["entity_id"].replace("_cool_to", "_heat_to")
    elif change == "no_refresh":
        step = steps["adopted"]
        assert step["refreshes"] > 0
        step["refreshes"] = 0
    else:
        startup = steps["off_reported"]["startup"]
        assert startup == {"primary": ""}
        startup["primary"] = "cool"
    assert holds.rendered() != recorded(name).read_text()
