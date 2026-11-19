"""``sim_timing.step`` computes, fires and beats as the real coordinator did, on every release.

Each recording in ``tests/fixtures/timing_parity/<release>/`` (``tests/timing_parity.py``) is
replayed through the model. The replay feeds it what a simulator's own compute would report and
what the script did: each script step's asks and wakeups, and for each compute whether it sent,
what its heads asked for while it was applied, and the wakeups it armed. Everything else comes
from the model: when each compute runs and why, each wakeup's run and what it asks for, the
heartbeat, the cooldown, and the start. The computes, wakeup runs and heartbeats it produces must
equal the recording's, entry for entry.

Where a release differs from the model, the difference is named in ``DIFFERENCES``, with what it
changes; each must change the comparison, so one that stops applying fails too.

No Home Assistant is loaded here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from custom_components.mxz_coordinator import sim_timing
from custom_components.mxz_coordinator.sim_timing import (
    Arm,
    Ask,
    Beat,
    Computed,
    Fire,
    Tick,
    Timers,
    Why,
    due,
    start,
    step,
)
from custom_components.mxz_coordinator.timing import WAKEUPS, Refresh

FIXTURES = Path(__file__).with_name("fixtures") / "timing_parity"
# The same lists as tests/timing_parity.py, which imports Home Assistant.
RELEASES = ("2024.12.5", "2026.2.3", "2026.9.4", "2026.10.4", "2026.11.2")
SCENARIOS = (
    "burst", "coil_dry_dwell", "coast", "cooldown_after_report", "window_now", "fresh",
    "lost_settle", "restart",
)
# A refresh the coordinator asked for, by its kind in the recording.
ASKS = {"request": Refresh.REQUEST, "report": Refresh.REPORT, "now": Refresh.NOW, "direct": Refresh.NOW}
STARTS = ("setup", "restart")
# The delivery queue's own zero-delay wait on HA before 2025.11 (DeliveryQueue.legacy).
LEGACY_WAIT = "refresh_followup"

Entry = dict[str, Any]


def _recording(release: str, name: str) -> list[Entry]:
    return json.loads((FIXTURES / release / f"{name}.json").read_text())


def _outputs(timeline: list[Entry]) -> list[Entry]:
    """What the model must produce: computes, wakeup runs and heartbeats, in order."""
    return [
        {key: entry[key] for key in ("t", "compute", "why", "fire", "heartbeat") if key in entry}
        for entry in timeline
        if "compute" in entry or "fire" in entry or "heartbeat" in entry
    ]


@dataclass
class _Compute:
    """What one recorded compute reported, as a simulator's compute would."""

    sent: bool = False
    asked: tuple[Refresh, ...] = ()
    armed: tuple[Arm, ...] = ()


def _arm(entry: Entry) -> Arm:
    if "arm" in entry:
        name, key, delay = entry["arm"]
        return Arm(name, key, delay)
    name, key = entry["cancel"]
    return Arm(name, key, None)


def _computes(timeline: list[Entry]) -> dict[int, _Compute]:
    computes: dict[int, _Compute] = {}
    for entry in timeline:
        source = entry.get("from", "")
        if "compute" in entry:
            computes[entry["compute"]] = _Compute()
        elif "sent" in entry:
            computes[entry["sent"]].sent = True
        elif source.startswith("compute "):
            compute = computes[int(source.removeprefix("compute "))]
            if "ask" in entry:
                compute.asked += (ASKS[entry["ask"]],)
            elif entry.get("arm", entry.get("cancel"))[0] != LEGACY_WAIT:
                compute.armed += (_arm(entry),)
    return computes


class _Replay:
    """The model driven by one recording's inputs; ``out`` is what it produced."""

    def __init__(self, timeline: list[Entry]) -> None:
        self.computes = _computes(timeline)
        self.timers: Timers | None = None
        self.out: list[Entry] = []
        self.count = 0

    def record(self, now: float, ran: tuple[Fire | Beat | Why, ...]) -> None:
        for what in ran:
            if isinstance(what, Fire):
                self.out.append({"t": now, "fire": [what.name, what.key]})
            elif isinstance(what, Beat):
                self.out.append({"t": now, "heartbeat": True})
            else:
                self.count += 1
                self.out.append({"t": now, "compute": self.count, "why": str(what)})

    def run(self, now: float, result: tuple[Timers, Any]) -> None:
        """Keep the timers, and run each compute the model asks for with what it reported."""
        self.timers, did = result
        self.record(now, did.ran)
        while did.compute is not None:
            report = self.computes.get(self.count, _Compute())
            self.timers, did = step(
                self.timers, Computed(report.sent, report.asked, report.armed), now,
            )
            self.record(now, did.ran)

    def until(self, now: float) -> None:
        """Every timer due by ``now`` runs, each at its own due time."""
        while self.timers is not None and (at := due(self.timers)) <= now:
            self.run(at, step(self.timers, Tick(), at))

    def send(self, now: float, event: Ask | Arm) -> None:
        assert self.timers is not None, "an input before MXZ started"
        self.run(now, step(self.timers, event, now))


def replay(timeline: list[Entry]) -> list[Entry]:
    """The model's computes, wakeup runs and heartbeats, on the inputs of a recording."""
    model = _Replay(timeline)
    action = ""
    started = False
    for entry in timeline:
        now = entry["t"]
        model.until(now)
        source = entry.get("from")
        if "do" in entry:
            action, started = entry["do"], False
        elif source != action or "end" in entry:
            continue
        elif action in STARTS and not started and entry.get("ask") == "direct":
            model.run(now, start(now))
            started = True
        elif action in STARTS and (entry.get("arm", [None])[0] == "startup" or "cancel" in entry):
            continue  # the model's start arms the startup wakeup and drops every old one
        elif "ask" in entry:
            model.send(now, Ask(ASKS[entry["ask"]]))
        elif "arm" in entry or "cancel" in entry:
            model.send(now, _arm(entry))
    model.until(timeline[-1]["t"])
    return model.out


@dataclass(frozen=True)
class Difference:
    """A way recordings differ from the model: on which releases, why, and what it changes.

    ``change`` turns the model's output for a scenario into what those releases recorded.
    """

    releases: tuple[str, ...]
    why: str
    change: Callable[[list[Entry], str], list[Entry]]


def _drop_legacy_wait(recorded: list[Entry]) -> list[Entry]:
    return [entry for entry in recorded if entry.get("fire", [None])[0] != LEGACY_WAIT]


def _swap_dry_at_321(expected: list[Entry], name: str) -> list[Entry]:
    if name != "coil_dry_dwell":
        return expected
    changed = [dict(entry) for entry in expected]
    first, second = (i for i, entry in enumerate(changed) if entry["t"] == 321.0 and "fire" in entry)
    changed[first]["fire"], changed[second]["fire"] = changed[second]["fire"], changed[first]["fire"]
    return changed


def _dwell_before_hold_end(expected: list[Entry], name: str) -> list[Entry]:
    if name != "coil_dry_dwell":
        return expected
    return [entry for entry in expected if entry != {"t": 1001.0, "fire": ["hold_end", None]}]


# Each difference between the recordings and the model (pr-body.md lists them).
DIFFERENCES: dict[str, Difference] = {
    "legacy_followup_wait": Difference(
        ("2024.12.5",),
        "Before HA 2025.11 the delivery queue runs its follow-up after a plan a request landed "
        "in from a zero-delay refresh_followup wait (delivery._legacy_busy and _legacy_defer "
        "make up for that debouncer dropping a request made during a refresh); later releases "
        "run it from a task. The computes are the same; the wait's runs are only in this "
        "release's recordings, and are left out of them here.",
        lambda expected, name: expected,
    ),
    "same_instant_order": Difference(
        ("2024.12.5",),
        "Two coil-dry wakeups armed in one compute and due at the same instant (321 s in "
        "coil_dry_dwell) run in the other order: head 1's first. Which one starts the compute "
        "and which one's request is followed up changes; the computes and their times do not.",
        _swap_dry_at_321,
    ),
    "dwell_before_hold_end": Difference(
        RELEASES,
        "At the end of a mode-change hold the dwell and hold-end wakeups are both due one second "
        "after it (each is the hold left plus one). Home Assistant runs the dwell one first here "
        "(their due times differ in the last bit of the event loop's float clock); its compute "
        "starts a new hold, which cancels the hold-end wakeup before it runs. The model runs "
        "them in the order they were armed, so it runs the hold-end wakeup, which asks for "
        "nothing, first. The computes are the same.",
        _dwell_before_hold_end,
    ),
}


def _applies(release: str) -> list[Difference]:
    return [difference for difference in DIFFERENCES.values() if release in difference.releases]


def _expected(release: str, name: str) -> list[Entry]:
    expected = replay(_recording(release, name))
    for difference in _applies(release):
        expected = difference.change(expected, name)
    return expected


def _recorded(release: str, name: str) -> list[Entry]:
    recorded = _outputs(_recording(release, name))
    if release in DIFFERENCES["legacy_followup_wait"].releases:
        recorded = _drop_legacy_wait(recorded)
    return recorded


@pytest.mark.parametrize("release", RELEASES)
@pytest.mark.parametrize("name", SCENARIOS)
def test_the_model_computes_as_the_coordinator_did(release: str, name: str) -> None:
    expected, recorded = _expected(release, name), _recorded(release, name)
    for at, (mine, theirs) in enumerate(zip(expected, recorded, strict=False)):
        assert mine == theirs, f"{release} {name}: output {at}"
    assert len(expected) == len(recorded), f"{release} {name}"


def test_every_scenario_and_release_is_recorded() -> None:
    assert {path.name for path in FIXTURES.iterdir()} == set(RELEASES)
    for release in RELEASES:
        assert {path.stem for path in (FIXTURES / release).iterdir()} == set(SCENARIOS)


def test_each_named_difference_still_applies() -> None:
    """Without it, each named difference's releases differ from the model, so one that stops
    applying fails here. The legacy wait is in exactly its releases' recordings."""
    for key, difference in DIFFERENCES.items():
        for release in difference.releases:
            others = [d for d in _applies(release) if d is not difference]
            changed = False
            for name in SCENARIOS:
                expected = replay(_recording(release, name))
                for other in others:
                    expected = other.change(expected, name)
                if key == "legacy_followup_wait":
                    changed |= _outputs(_recording(release, name)) != _recorded(release, name)
                else:
                    changed |= expected != _recorded(release, name)
            assert changed, (key, release)
    for release in RELEASES:
        legacy = any(
            LEGACY_WAIT in (entry.get("arm") or entry.get("fire") or [None])
            for name in SCENARIOS for entry in _recording(release, name)
        )
        assert legacy == (release in DIFFERENCES["legacy_followup_wait"].releases), release


def test_each_wakeup_asks_what_the_coordinator_asked() -> None:
    """The refresh the model asks for at each wakeup's run is the one the coordinator asked."""
    seen = set()
    for release in RELEASES:
        for name in SCENARIOS:
            timeline = _recording(release, name)
            for entry in timeline:
                source = entry.get("from", "")
                if "ask" in entry and source.startswith("fire "):
                    wakeup = source.removeprefix("fire ")
                    assert WAKEUPS[wakeup] is ASKS[entry["ask"]], (release, name, entry)
                    seen.add(wakeup)
    assert seen >= {"startup", "dry", "coast", "dwell", "fresh", "lost", "settle", "opening"}


@pytest.mark.parametrize("change", ["cooldown", "followup", "heartbeat", "now_cancels"])
def test_the_replay_catches_a_broken_model(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    """A model with a longer cooldown, no delivery follow-up, a heartbeat every 10 minutes, or a
    refresh at once that leaves the cooldown running differs from the recordings."""
    begin = sim_timing._Run.begin
    name = {"cooldown": "burst", "followup": "coil_dry_dwell", "heartbeat": "lost_settle"}.get(
        change, "window_now"
    )
    assert _expected("2026.11.2", name) == _recorded("2026.11.2", name)
    if change == "cooldown":
        monkeypatch.setattr(sim_timing, "REQUEST_COOLDOWN", 11)
    elif change == "heartbeat":
        monkeypatch.setattr(sim_timing, "HEARTBEAT", 600)
    elif change == "followup":

        def no_followup(run: Any, why: Why) -> None:
            if why is not Why.FOLLOWUP:
                begin(run, why)

        monkeypatch.setattr(sim_timing._Run, "begin", no_followup)
    else:

        def keeps_cooldown(run: Any, why: Why) -> None:
            run.set(computing=why)
            run.ran.append(why)

        monkeypatch.setattr(sim_timing._Run, "begin", keeps_cooldown)
    assert _expected("2026.11.2", name) != _recorded("2026.11.2", name)
