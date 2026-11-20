"""The real coordinator computes, arms and beats exactly as recorded on this release.

Each scenario in ``tests/timing_parity.py`` runs the real integration and is compared, byte for
byte, with its recording for the Home Assistant release this suite runs on,
``tests/fixtures/timing_parity/<release>/<scenario>.json``. Those were captured on 725ba24, with
MXZ's timing still written out in the coordinator's handlers; ``cooldown_and_hold_off`` and
``dwell_and_hold`` on 37494ed's integration. ``MXZ_PARITY_CAPTURE=1`` rewrites the recordings
of this release. A release with no recordings fails: capture them on it first.

``tests/test_sim_timing_parity.py`` checks ``sim_timing.step`` against every release's
recordings.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from tests.timing_parity import RELEASE, RELEASES, SCENARIOS, Timeline, recorded

CAPTURE = os.environ.get("MXZ_PARITY_CAPTURE") == "1"


async def _run(hass: HomeAssistant, freezer: Any, monkeypatch: Any, name: str) -> Timeline:
    house = Timeline(hass, freezer, monkeypatch)
    await house.run(SCENARIOS[name])
    return house


def test_this_release_has_recordings() -> None:
    assert RELEASE in RELEASES, (
        f"no timing recordings for Home Assistant {RELEASE}: capture them on it with "
        "MXZ_PARITY_CAPTURE=1 and add it to RELEASES"
    )


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_coordinator_times_what_it_did_on_the_base(
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


def _first(timeline: list[dict[str, Any]], key: str, **match: Any) -> dict[str, Any]:
    return next(
        entry for entry in timeline
        if key in entry and all(entry.get(k) == v for k, v in match.items())
    )


@pytest.mark.parametrize("change", ["compute_time", "why", "heartbeat", "arm_delay", "fire"])
async def test_the_recording_catches_one_changed_value(
    hass: HomeAssistant, freezer: Any, monkeypatch: Any, change: str
) -> None:
    """The comparison fails on a compute a second late, a compute run for another reason, a
    missing heartbeat, a wakeup armed for a second longer, or a wakeup that did not run."""
    name = {"heartbeat": "burst", "arm_delay": "coast", "fire": "coast"}.get(change, "coil_dry_dwell")
    house = await _run(hass, freezer, monkeypatch, name)
    assert house.rendered() == recorded(RELEASE, name).read_text()
    timeline = house.timeline
    if change == "compute_time":
        _first(timeline, "compute", why="cooldown")["t"] += 1
    elif change == "why":
        _first(timeline, "compute", why="followup")["why"] = "ask"
    elif change == "heartbeat":
        timeline.remove(_first(timeline, "heartbeat"))
    elif change == "arm_delay":
        _first(timeline, "arm")["arm"][2] += 1
    else:
        timeline.remove(_first(timeline, "fire", fire=["coast", None]))
    assert house.rendered() != recorded(RELEASE, name).read_text()
