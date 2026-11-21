"""The real coordinator makes exactly the calls on its clock it made on the base, on this release.

Each scenario in ``tests/arms_parity.py`` runs the real integration and is compared, byte for
byte, with its recording for the Home Assistant release this suite runs on,
``tests/fixtures/arms_parity/<release>/<scenario>.json``, captured on d4cffd2 with the wakeups
still synced by the coordinator's ``_sync_*`` methods (``cooldown_and_hold_off`` and
``dwell_and_hold`` on 37494ed's integration). ``MXZ_PARITY_CAPTURE=1`` rewrites this
release's recordings. A release with no recordings fails: capture them on it first. An
unrecorded Home Assistant dev build skips every test here (``tests/parity_release.py``).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from tests import arms_parity
from tests.arms_parity import RELEASE, RELEASES, SCENARIOS, Arms, recorded
from tests.parity_release import skip_reason

CAPTURE = os.environ.get("MXZ_PARITY_CAPTURE") == "1"
SKIP = skip_reason(RELEASE, RELEASES, capture=CAPTURE)
pytestmark = pytest.mark.skipif(SKIP is not None, reason=SKIP or "")


async def _run(hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str) -> Arms:
    house = Arms(hass, freezer, monkeypatch)
    await house.run(SCENARIOS[name])
    return house


def test_this_release_has_recordings() -> None:
    assert RELEASE in RELEASES, (
        f"no clock recordings for Home Assistant {RELEASE}: capture them on it with "
        "MXZ_PARITY_CAPTURE=1 and add it to tests/timing_parity.py's RELEASES"
    )
    missing = [name for name in SCENARIOS if not recorded(RELEASE, name).is_file()]
    assert missing == [], f"no clock recordings of {missing} for Home Assistant {RELEASE}"


def test_negative_a_release_missing_one_recording_fails_the_check(
    tmp_path: Path, monkeypatch: Any
) -> None:
    (tmp_path / RELEASE).mkdir()
    for name in SCENARIOS:
        if name != "fresh":
            shutil.copy(recorded(RELEASE, name), tmp_path / RELEASE)
    monkeypatch.setattr(arms_parity, "FIXTURES", tmp_path)
    with pytest.raises(AssertionError, match=r"no clock recordings of \['fresh'\]"):
        test_this_release_has_recordings()


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_coordinator_arms_what_it_did_on_the_base(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str
) -> None:
    house = await _run(hass, freezer, monkeypatch, name)
    rendered = house.rendered()
    path = recorded(RELEASE, name)
    if CAPTURE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
    theirs = json.loads(path.read_text())
    for at, (mine, recorded_entry) in enumerate(zip(house.timeline, theirs, strict=False)):
        assert mine == recorded_entry, f"entry {at}"
    assert len(house.timeline) == len(theirs)
    assert rendered == path.read_text()


async def test_negative_a_scenario_with_no_recording_fails(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(arms_parity, "FIXTURES", tmp_path)
    monkeypatch.setitem(globals(), "CAPTURE", False)
    with pytest.raises(FileNotFoundError):
        await test_the_coordinator_arms_what_it_did_on_the_base(hass, freezer, monkeypatch, "fresh")


def _clock(timeline: list[dict[str, Any]], *call: Any) -> dict[str, Any]:
    return next(
        entry for entry in timeline
        if "clock" in entry and entry["clock"][: len(call)] == list(call)
        and str(entry["from"]).startswith("compute ")
    )


@pytest.mark.parametrize("change", ["order", "delay", "gate", "retired"])
async def test_the_recording_catches_one_changed_value(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, change: str
) -> None:
    """The comparison fails on two wakeups synced in the other order, a delay a second longer,
    a wakeup armed that a gate cancels, or a retired coordinator that still arms settle."""
    name = {"order": "coast", "delay": "coast", "gate": "coordinator_off", "retired": "retired"}[change]
    house = await _run(hass, freezer, monkeypatch, name)
    assert house.rendered() == recorded(RELEASE, name).read_text()
    timeline = house.timeline
    if change == "order":
        first = _clock(timeline, "cancel", "dwell")
        at = timeline.index(first)
        timeline[at], timeline[at + 1] = timeline[at + 1], timeline[at]
    elif change == "delay":
        _clock(timeline, "call_later", "coast")["clock"][3] += 1
    elif change == "gate":
        off = next(i for i, entry in enumerate(timeline) if entry.get("do") == "switch _coordinator_enable off")
        entry = next(
            entry for entry in timeline[off:]
            if entry.get("clock", [None, None])[:2] == ["cancel", "coast"]
        )
        entry["clock"] = ["call_later", "coast", None, 11]
    else:
        entry = next(
            entry for entry in timeline
            if entry.get("clock", [None, None])[:2] == ["cancel", "settle"]
        )
        entry["clock"] = ["call_later", "settle", None, 1]
    assert house.rendered() != recorded(RELEASE, name).read_text()
