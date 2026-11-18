"""The manual fan hold decides and writes exactly what it did before it left ``FanHold``.

Each scenario in ``tests/fanhold_parity.py`` runs the real integration and is compared, byte
for byte, with its recording in ``tests/fixtures/fanhold_parity/``, captured on 991ca10 with the
decision still inside ``FanHold``. ``MXZ_PARITY_CAPTURE=1`` rewrites the recordings.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from tests.fanhold_parity import SCENARIOS, Fans, recorded

CAPTURE = os.environ.get("MXZ_PARITY_CAPTURE") == "1"


async def _run(hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str) -> Fans:
    fans = Fans(hass, freezer, monkeypatch)
    await SCENARIOS[name](fans)
    await fans.rig.close()
    return fans


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_fan_hold_decides_and_writes_what_it_did_on_the_base(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str
) -> None:
    fans = await _run(hass, freezer, monkeypatch, name)
    rendered = fans.rendered()
    if CAPTURE:
        recorded(name).parent.mkdir(parents=True, exist_ok=True)
        recorded(name).write_text(rendered)
    steps = json.loads(recorded(name).read_text())
    assert [step["step"] for step in fans.steps] == [step["step"] for step in steps]
    for mine, theirs in zip(json.loads(rendered), steps, strict=True):
        assert mine == theirs, mine["step"]
    assert rendered == recorded(name).read_text()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("heads", 0, "fan_hold"), False),
        (("heads", 0, "fan_writes"), ["high"]),
        (("heads", 0, "hold", "commanded"), "medium"),
        (("published",), {}),
    ],
    ids=["hold", "write", "memory", "publish"],
)
async def test_the_recording_catches_one_changed_value(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, path: tuple[Any, ...], value: Any
) -> None:
    """The comparison fails on one changed hold, fan write, echo memory or publish."""
    fans = await _run(hass, freezer, monkeypatch, "pick_held")
    assert fans.rendered() == recorded("pick_held").read_text()
    record: Any = next(step for step in fans.steps if step["step"] == "pick_held")
    for key in path[:-1]:
        record = record[key]
    assert record[path[-1]] != value
    record[path[-1]] = value
    assert fans.rendered() != recorded("pick_held").read_text()
