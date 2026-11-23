"""The simulator page (docs/simulator): its MXZ row is the product's, and the page says what it is.

The parity tests run each script on the real coordinator over the real invented heads and on the
page's row from the shipped bundle without Home Assistant (``tests/simlab_parity.py``), and
compare them after every step.

Every room, head and number is invented; nothing here is a hardware result.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.exceptions import ServiceValidationError

from tests.simlab_parity import WALL, Lab, climate_id, run_page, store
from tests.test_drive import _eid

ROOT = Path(__file__).resolve().parents[1]
BUNDLER = ROOT / "tools" / "simulator_bundle.py"
OFF = {"hvac_mode": "off", "target_temp_low": None, "target_temp_high": None, "fan_mode": "auto"}
ALL_ON = {"studio": True, "loft": True, "den": True}
AT_70 = {"studio": 70.0, "loft": 70.0, "den": 70.0}


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The page's Python as the bundle ships it."""
    target = tmp_path_factory.mktemp("simulator")
    subprocess.run([sys.executable, str(BUNDLER), "--tree", str(target)], check=True)
    return target


def _script(steps: list[Any], *, temps: dict[str, float] | None = None, heads: dict[str, Any] | None = None,
            saved: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "wall": WALL,
        "temps": {**AT_70, **(temps or {})},
        "heads": {key: {**OFF, **(heads or {}).get(key, {})} for key in AT_70},
        "saved": saved or store(targets=AT_70, enables=ALL_ON),
        "steps": steps,
    }


COOL_120_S_AGO = store(
    targets=AT_70, enables=ALL_ON,
    mode={"mode": "cool", "mode_at": WALL - 120, "mode_by_votes": False, "last_mode_change": WALL - 120},
)
COOL_2_HOURS_AGO = store(
    targets=AT_70, enables=ALL_ON,
    mode={"mode": "cool", "mode_at": WALL - 7200, "mode_by_votes": False, "last_mode_change": WALL - 7200},
)
# The page's standoff starting point, as its world fed the MXZ row (the file says how).
STANDOFF_FAN_CALL = json.loads((ROOT / "tests" / "fixtures" / "simlab" / "standoff_fan_call.json").read_text())

SCRIPTS: dict[str, dict[str, Any]] = {
    # The README hook: Loft and Den satisfied, Studio hot. MXZ cools Studio and leaves the
    # others off; Studio's fan steps down as it nears its target, then coil-dry, then off.
    "mixed_demand": _script(
        [
            ["until", 60.5],
            ["reading", "studio", 74.0],
            ["until", 120.5],
            ["reading", "studio", 72.0],
            ["until", 180.5],
            ["reading", "studio", 70.0],
            ["until", 1500.5],
        ],
        temps={"studio": 76.0},
    ),
    # Everyone at target with the heads left running heat: MXZ parks them, the outdoor unit
    # has nobody calling; then each room's sensor reports a small move, one after another, as
    # the page's rooms report.
    "satisfied_all": _script(
        [
            ["until", 300.5],
            ["reading", "studio", 70.4],
            ["until", 301.5],
            ["reading", "loft", 69.6],
            ["until", 302.5],
            ["reading", "den", 70.2],
            ["until", 2000.5],
        ],
        heads={key: {"hvac_mode": "heat", "target_temp_low": 70.0, "target_temp_high": 72.0}
               for key in AT_70},
        saved=store(targets=AT_70, enables=ALL_ON,
                    mode={"mode": "heat", "mode_at": WALL - 7200, "mode_by_votes": False,
                          "last_mode_change": WALL - 7200}),
    ),
    # Studio hot and Den cold at once: a standoff; Studio, first in order, wins it. Nothing
    # else changes, so after the startup wakeup only the 15-minute heartbeat recomputes.
    "standoff_heartbeat": _script(
        [["until", 2000.5]],
        temps={"studio": 75.0, "den": 65.0},
    ),
    # The same standoff, and Den's Comfort priority turns on: Den wins it and the shared mode
    # goes to heat; when it turns off, the mode-switch hold keeps heat until it ends.
    "comfort_priority": _script(
        [
            ["until", 60.5],
            ["comfort", "den", True],
            ["until", 300.5],
            ["comfort", "den", False],
            ["until", 1300.5],
        ],
        temps={"studio": 75.0, "den": 65.0},
    ),
    # A cool change two minutes before the start: the hold carries over, its hold-end wakeup is
    # armed at the start, and Den's heat call waits for the dwell wakeup before the mode flips.
    "dwell_hold": _script(
        [
            ["until", 30.5],
            ["reading", "den", 64.0],
            ["until", 700.5],
        ],
        saved=store(targets=AT_70, enables=ALL_ON,
                    mode={"mode": "cool", "mode_at": WALL - 120, "mode_by_votes": False,
                          "last_mode_change": WALL - 120}),
    ),
    # Studio cools at a boosted fan; a person picks low at its head and MXZ keeps it through
    # readings that would step the fan; Fan auto on hands it back.
    "fan_hold": _script(
        [
            ["until", 60.5],
            ["press", "studio", {"fan_mode": "low"}],
            ["until", 120.5],
            ["reading", "studio", 74.0],
            ["until", 180.5],
            ["reading", "studio", 72.5],
            ["until", 240.5],
            ["fan_auto", "studio", True],
            ["until", 400.5],
        ],
        temps={"studio": 76.0},
    ),
    # Den heats at a boosted fan and reaches its target: the plan parks it off, and its fan
    # goes back to auto before the off (the hand-back).
    "handback_before_off": _script(
        [
            ["until", 60.5],
            ["reading", "den", 68.0],
            ["until", 120.5],
            ["reading", "den", 70.0],
            ["until", 300.5],
        ],
        temps={"den": 66.0},
        saved=store(targets=AT_70, enables=ALL_ON,
                    mode={"mode": "heat", "mode_at": WALL - 7200, "mode_by_votes": False,
                          "last_mode_change": WALL - 7200}),
    ),
    # A person turns Studio off while it cools: the head goes off with no hand-back (its room
    # is off, not parked by the plan).
    "room_turned_off": _script(
        [
            ["until", 60.5],
            ["enable", "studio", False],
            ["until", 200.5],
        ],
        temps={"studio": 76.0},
    ),
    # Studio cools; a person picks low at its head; Home Assistant restarts inside the
    # mode-switch hold. The held fan, the hold and its hold-end wakeup survive the restart.
    "restart_held_fan": _script(
        [
            ["until", 60.5],
            ["press", "studio", {"fan_mode": "low"}],
            ["until", 120.5],
            ["restart"],
            ["until", 180.5],
            ["reading", "studio", 73.0],
            ["until", 900.5],
        ],
        temps={"studio": 76.0},
        saved=store(targets=AT_70, enables=ALL_ON,
                    mode={"mode": "heat", "mode_at": WALL - 7200, "mode_by_votes": False,
                          "last_mode_change": WALL - 7200}),
    ),
    # Accept changes made at the head: a person raises Studio's cool setpoint at its remote,
    # then sets Den's head to heat; MXZ adopts each as the room's target when the hold ends.
    "head_changes": _script(
        [
            ["until", 60.5],
            ["press", "studio", {"target_temp_high": 73.0}],
            ["until", 120.5],
            ["press", "den", {"hvac_mode": "heat", "target_temp_low": 73.0}],
            ["until", 400.5],
        ],
        temps={"studio": 76.0},
    ),
    # Den's Comfort priority wins the standoff and the shared mode goes to heat; the hold ends;
    # Home Assistant restarts. Comfort priority is Den's own entity and is still on, so the
    # first compute after the start keeps heat and sends nothing.
    "restart_comfort_priority": _script(
        [
            ["until", 60.5],
            ["comfort", "den", True],
            ["until", 700.5],
            ["restart"],
            ["until", 760.5],
        ],
        temps={"studio": 75.0, "den": 60.0},
    ),
    # Accept changes made at the head: a person turns Studio's head off at its remote, and MXZ
    # turns the room off when the hold ends; then sets it back to cool, and MXZ turns it on.
    "head_mode_change": _script(
        [
            ["until", 60.5],
            ["press", "studio", {"hvac_mode": "off"}],
            ["until", 120.5],
            ["press", "studio", {"hvac_mode": "cool"}],
            ["until", 200.5],
        ],
        temps={"studio": 76.0},
    ),
    # A person sets Studio's target number while MXZ holds a change made at its head.
    "target_write": _script(
        [
            ["until", 60.5],
            ["press", "studio", {"target_temp_high": 73.0}],
            ["until", 62.5],
            ["target", "studio", 68.0],
            ["until", 120.5],
        ],
        temps={"studio": 76.0},
    ),
    # A person sets Studio's target to 73; Home Assistant restarts. The target comes back from
    # the store, so the head keeps what MXZ sent it for 73.
    "restart_target": _script(
        [
            ["until", 60.5],
            ["target", "studio", 73.0],
            ["until", 120.5],
            ["restart"],
            ["until", 300.5],
        ],
        temps={"studio": 76.0},
    ),
    # Studio cools and MXZ steps its fan down; a person picks medium at its head. Home Assistant
    # restarts. Medium is a speed MXZ's fan boost could have left the head at this close to the
    # target, so the head alone would read as MXZ's own; the store's fan record says it is a
    # person's, and the pick stays held through readings that would step the fan.
    "restart_fan_record": _script(
        [
            ["until", 60.5],
            ["reading", "studio", 72.8],
            ["until", 90.5],
            ["press", "studio", {"fan_mode": "medium"}],
            ["until", 120.5],
            ["restart"],
            ["until", 240.5],
            ["reading", "studio", 71.6],
            ["until", 300.5],
            ["reading", "studio", 70.4],
            ["until", 700.5],
        ],
        temps={"studio": 76.0},
    ),
    # A person raises Studio's cool setpoint at its remote 10 seconds after a start, inside the
    # startup quiet: MXZ takes nothing from the head, and the startup compute writes 70 back.
    "startup_quiet": _script(
        [
            ["until", 10.5],
            ["press", "studio", {"target_temp_high": 73.0}],
            ["until", 120.5],
        ],
        temps={"studio": 76.0},
    ),
    # A person raises Studio's cool setpoint at its remote, and a reading asks for a compute one
    # second later, inside the 5-second hold: the compute leaves the head alone, and the hold's
    # end takes 73 as the target.
    "adopt_hold_reading": _script(
        [
            ["until", 60.5],
            ["press", "studio", {"target_temp_high": 73.0}],
            ["until", 61.5],
            ["reading", "studio", 75.0],
            ["until", 120.5],
        ],
        temps={"studio": 76.0},
    ),
    # Studio is hot and its head is heating when MXZ starts. MXZ resumes the run the head shows
    # (the startup seed): that heat run is over, so the head goes off and the room coasts before
    # the coast wakeup lets it cool.
    "startup_resume": _script(
        [["until", 120.5]],
        temps={"studio": 76.0},
        heads={"studio": {"hvac_mode": "heat", "target_temp_low": 70.0, "target_temp_high": 72.0}},
    ),
    # Den sits at its target in heat; a person turns its head to heat at 71, one degree above
    # the room. MXZ takes 71 as the target and forgets Den's run (a Latch step), so the next
    # compute takes the head's heat as the run and keeps heating, though a fresh decision would
    # wait out the 1-degree drift.
    "head_run_adopted": _script(
        [
            ["until", 60.5],
            ["press", "den", {"hvac_mode": "heat", "target_temp_low": 71.0}],
            ["until", 120.5],
        ],
        saved=store(targets=AT_70, enables=ALL_ON,
                    mode={"mode": "heat", "mode_at": WALL - 7200, "mode_by_votes": False,
                          "last_mode_change": WALL - 7200}),
    ),
    # A person raises Studio's cool setpoint at its remote, then turns Studio's enable switch on
    # (it already is) inside the hold: the write ends the hold with nothing taken, and Studio is
    # sent 70 again.
    "enable_in_hold": _script(
        [
            ["until", 60.5],
            ["press", "studio", {"target_temp_high": 73.0}],
            ["until", 62.5],
            ["enable", "studio", True],
            ["until", 120.5],
        ],
        temps={"studio": 76.0},
    ),
    # A person turns Studio's head off at its remote while it cools. The off arms the off heal
    # and the adopt hold; when the hold ends, MXZ takes the off as turning the room off and
    # cancels the off heal then, not when it would run.
    "head_off_adopted": _script(
        [
            ["until", 60.5],
            ["press", "studio", {"hvac_mode": "off"}],
            ["until", 70.5],
            ["until", 200.5],
        ],
        temps={"studio": 76.0},
    ),
    # Den heats; a person turns its head off 5 seconds after a start, inside the startup quiet,
    # so MXZ takes nothing from the head. The off heal runs 30 seconds later and asks for the
    # compute that turns the head back on, before the startup compute.
    "off_heal": _script(
        [
            ["until", 5.5],
            ["press", "den", {"hvac_mode": "off"}],
            ["until", 36.0],
            ["until", 120.5],
        ],
        temps={"den": 66.0},
        heads={"den": {"hvac_mode": "heat", "target_temp_low": 70.0, "target_temp_high": 72.0,
                       "fan_mode": "high"}},
        saved=store(targets=AT_70, enables=ALL_ON,
                    mode={"mode": "heat", "mode_at": WALL - 7200, "mode_by_votes": False,
                          "last_mode_change": WALL - 7200}),
    ),
    # Studio cools; Home Assistant restarts twice. MXZ's memory of what it wrote to each head
    # comes back from the store at the first start, so the second stop saves it again.
    "restart_head_writes": _script(
        [
            ["until", 60.5],
            ["restart"],
            ["until", 120.5],
            ["restart"],
            ["until", 180.5],
        ],
        temps={"studio": 76.0},
    ),
    # Studio and Den cool; a person turns both heads off at their remotes at one instant, so
    # both adopt holds end together with no compute in the 10 seconds before. Each hold end is
    # judged before the compute the first one asks for, so both rooms are turned off and
    # neither head is sent anything.
    "two_holds_off": _script(
        [
            ["until", 60.5],
            ["press", "studio", {"hvac_mode": "off"}],
            ["press", "den", {"hvac_mode": "off"}],
            ["until", 120.5],
        ],
        temps={"studio": 76.0, "den": 76.0},
    ),
    # As ``two_holds_off``, then Loft warms 5 seconds after the hold ends. Studio's hold end goes
    # on inside the follow-up its own write's compute started, so its last ask lands there and
    # leaves no cooldown running: Loft's reading is worked out at once, and Loft is sent cool.
    "two_holds_off_then_reading": _script(
        [
            ["until", 60.5],
            ["press", "studio", {"hvac_mode": "off"}],
            ["press", "den", {"hvac_mode": "off"}],
            ["until", 70.5],
            ["reading", "loft", 74.0],
            ["until", 120.5],
        ],
        temps={"studio": 76.0, "den": 76.0},
    ),
    # Loft's reading starts a compute and its 10-second cooldown; Studio and then Den are turned
    # off at their remotes, so Den's adopt hold ends at 70.5 s, when the cooldown does. The
    # cooldown's compute starts first and Den's hold end runs inside it.
    "cooldown_and_hold_off": _script(
        [
            ["until", 60.5],
            ["reading", "loft", 70.2],
            ["press", "studio", {"hvac_mode": "off"}],
            ["until", 65.5],
            ["press", "den", {"hvac_mode": "off"}],
            ["until", 120.5],
        ],
        temps={"studio": 76.0, "den": 76.0},
    ),
    # Cool was set 2 minutes before the start, so the mode wait ends at 481 s; Den is cold, so
    # the dwell wakeup is due then too, and a person sets Studio's head 5 seconds before. The
    # dwell wakeup's compute starts first and Studio's hold end runs inside it.
    "dwell_and_hold": _script(
        [
            ["until", 30.5],
            ["reading", "den", 64.0],
            ["until", 476.0],
            ["press", "studio", {"hvac_mode": "cool", "target_temp_high": 72.0}],
            ["until", 700.5],
        ],
        saved=COOL_120_S_AGO,
    ),
    # The page's standoff: Studio cools, then Den heats once Studio is within 3 °F of its
    # target. At 8:30 a person turns Studio's head off at its remote; at the next wakeup a person
    # sets Den's to 73. MXZ takes both, and Den's fan steps down at the time Home Assistant
    # steps it.
    "standoff_fan_call": _script(
        STANDOFF_FAN_CALL["steps"], temps=STANDOFF_FAN_CALL["temps"], saved=COOL_2_HOURS_AGO,
    ),
}


# The store sections the page keeps. The coordinator's store also has a ``floor`` and an
# ``openings`` section, for features the invented house does not have; ``modeled`` checks
# they are empty. ``saved_at`` is when the store last changed, which the page does not track:
# a start reads it only against restore data, and the page has none.
MODELED_SECTIONS = ("rooms", "switches", "fan", "mode", "head_writes")


def modeled(snapshot: dict[str, Any]) -> dict[str, Any]:
    """A snapshot as the comparison reads it: calls by head, in order (the product interleaves
    heads at each await; the page delivers them one after another), and the store's record by
    the sections the page keeps."""
    out = {key: value for key, value in snapshot.items() if key not in ("calls", "saved")}
    by_head: dict[str, list[Any]] = {}
    for head, service, data in snapshot["calls"]:
        by_head.setdefault(head, []).append([service, data])
    out["calls"] = by_head
    if (saved := snapshot.get("saved")) is not None:
        out["saved"] = {name: saved.get(name) for name in MODELED_SECTIONS}
        assert saved.get("floor", {}) == {}, saved["floor"]
        assert saved.get("openings", {"rooms": {}}) == {"rooms": {}}, saved["openings"]
    return out


async def _both(
    script: dict[str, Any], tree: Path, hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    real = await Lab(hass, freezer, monkeypatch, tmp_path).run(script)
    return real, run_page(tree, script)


# Where the real coordinator on Home Assistant before 2025.11 differs from the page, whose timing
# follows 2025.11 and later: by script, the snapshot fields left out of the comparison there.
BEFORE_2025_11: dict[str, tuple[str, ...]] = {
    # Those releases start the delivery queue's follow-up compute from a zero-delay wait
    # (``DeliveryQueue.legacy``), so both hold ends' closing asks land before it starts and one
    # follow-up takes them all. Later releases start it at once, and the closing asks bring a
    # second one. The calls, waits, plan and what each head and room show are the same.
    "two_holds_off": ("computes",),
    "two_holds_off_then_reading": ("computes",),
}


def _left_out(name: str) -> tuple[str, ...]:
    return BEFORE_2025_11.get(name, ()) if (MAJOR_VERSION, MINOR_VERSION) < (2025, 11) else ()


def _compared(snapshot: dict[str, Any], left_out: tuple[str, ...] = ()) -> dict[str, Any]:
    return {key: value for key, value in modeled(snapshot).items() if key not in left_out}


@pytest.mark.parametrize("name", sorted(SCRIPTS))
async def test_page_row_is_the_coordinator(
    name: str, tree: Path, hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    real, page = await _both(SCRIPTS[name], tree, hass, freezer, monkeypatch, tmp_path)
    left_out = _left_out(name)
    assert len(page) == len(real)
    for want, got in zip(real, page, strict=True):
        assert _compared(got, left_out) == _compared(want, left_out), want["do"]
    if left_out:  # the difference named for this release is still there
        assert any(modeled(got) != modeled(want) for want, got in zip(real, page, strict=True))


def _calls(snapshots: list[dict[str, Any]], head: str, *, after: str | None = None) -> list[Any]:
    """Every call to ``head``, in order, from the step labelled ``after`` on (all if None)."""
    seen = after is None
    out = []
    for snapshot in snapshots:
        seen = seen or snapshot["do"] == after
        if seen:
            out += [[service, data] for key, service, data in snapshot["calls"] if key == head]
    return out


def _step(snapshots: list[dict[str, Any]], label: str) -> dict[str, Any]:
    return next(snapshot for snapshot in snapshots if snapshot["do"] == label)


def _mixed_demand(real: list[dict[str, Any]]) -> None:
    fans = [data["fan_mode"] for service, data in _calls(real, "studio") if service == "set_fan_mode"]
    assert fans[:2] == ["high", "medium"]
    assert ["set_hvac_mode", {"hvac_mode": "fan_only"}] in _calls(real, "studio")
    assert {"cool"} == {snapshot["plan"]["state"] for snapshot in real}


def _satisfied_all(real: list[dict[str, Any]]) -> None:
    for head in ("studio", "loft", "den"):
        assert _calls(real, head) == [["set_hvac_mode", {"hvac_mode": "off"}]]
    assert real[-1]["computes"][-2:] == [[900.0, "ask"], [1800.0, "ask"]]


def _standoff_heartbeat(real: list[dict[str, Any]]) -> None:
    assert all(snapshot["plan"]["standoff"] for snapshot in real)
    assert real[-1]["computes"] == [[10.0, "cooldown"], [40.0, "ask"], [900.0, "ask"], [1800.0, "ask"]]


def _comfort_priority(real: list[dict[str, Any]]) -> None:
    on = _step(real, "comfort den True")
    assert on["plan"]["mode_by_comfort_priority"] and on["mode"] == "heat"
    assert "dwell None" in _step(real, "comfort den False")["waits"]
    assert real[-1]["mode"] == "cool"


def _dwell_hold(real: list[dict[str, Any]]) -> None:
    assert real[0]["waits"]["hold_end None"] == 481.0
    held = _step(real, "reading den 64.0")
    assert held["mode"] == "cool" and held["waits"]["dwell None"] == 450.5
    assert [481.0, "ask"] in real[-1]["computes"] and real[-1]["mode"] == "heat"


def _fan_hold(real: list[dict[str, Any]]) -> None:
    assert _step(real, "press studio {'fan_mode': 'low'}")["rooms"]["studio"]["fan_held"]
    picked = _calls(real, "studio", after="press studio {'fan_mode': 'low'}")
    handed = _calls(real, "studio", after="fan_auto studio True")
    assert picked[: len(picked) - len(handed)] == []
    assert handed and handed[0][0] == "set_fan_mode"


def _handback_before_off(real: list[dict[str, Any]]) -> None:
    last = _calls(real, "den", after="reading den 70.0")
    assert last[:2] == [["set_fan_mode", {"fan_mode": "auto"}], ["set_hvac_mode", {"hvac_mode": "off"}]]


def _room_turned_off(real: list[dict[str, Any]]) -> None:
    assert _calls(real, "studio", after="enable studio False") == [["set_hvac_mode", {"hvac_mode": "off"}]]


def _restart_held_fan(real: list[dict[str, Any]]) -> None:
    restart = _step(real, "restart")
    assert restart["saved"]["fan"][climate_id("studio")]["held"] is True
    assert restart["waits"]["hold_end None"] == 480.5 and restart["hold"] == 479.5
    assert all(snapshot["rooms"]["studio"]["fan_held"] for snapshot in real[real.index(restart):])
    assert [c for c in _calls(real, "studio", after="restart") if c[0] == "set_fan_mode"] == []


def _head_changes(real: list[dict[str, Any]]) -> None:
    assert _step(real, "until 120.5")["rooms"]["studio"]["target"] == 73.0
    assert real[-1]["rooms"]["den"]["target"] == 73.0


def _restart_comfort_priority(real: list[dict[str, Any]]) -> None:
    assert _step(real, "comfort den True")["mode"] == "heat"
    restart = _step(real, "restart")
    assert restart["hold"] == 0.0 and restart["mode"] == "heat"
    assert restart["plan"]["mode_by_comfort_priority"] and restart["plan"]["rooms"][2]["comfort_priority"]
    assert _calls(real, "studio", after="restart") == _calls(real, "den", after="restart") == []
    assert real[-1]["mode"] == "heat"


def _head_mode_change(real: list[dict[str, Any]]) -> None:
    assert _step(real, "until 120.5")["rooms"]["studio"]["enable"] is False
    assert real[-1]["rooms"]["studio"]["enable"] is True
    assert _calls(real, "studio", after="press studio {'hvac_mode': 'off'}") == []


ADOPT_STUDIO = "heal ('climate.studio_head', 'adopt')"


def _target_write(real: list[dict[str, Any]]) -> None:
    assert ADOPT_STUDIO in _step(real, "until 62.5")["waits"]
    written = _step(real, "target studio 68.0")
    assert ADOPT_STUDIO not in written["waits"]
    assert written["calls"] == [["studio", "set_temperature",
                                 {"hvac_mode": "cool", "target_temp_low": 66.0, "target_temp_high": 68.0}]]
    assert real[-1]["rooms"]["studio"]["target"] == 68.0


def _restart_target(real: list[dict[str, Any]]) -> None:
    restart = _step(real, "restart")
    assert restart["saved"]["rooms"][climate_id("studio")]["target"] == 73.0
    assert all(snapshot["rooms"]["studio"]["target"] == 73.0 for snapshot in real[real.index(restart):])
    assert restart["heads"]["studio"][:3] == ["cool", 71.0, 73.0]
    assert _calls(real, "studio", after="restart") == []


def _restart_fan_record(real: list[dict[str, Any]]) -> None:
    picked = "press studio {'fan_mode': 'medium'}"
    assert _calls(real, "studio", after="reading studio 72.8")[0] == ["set_fan_mode", {"fan_mode": "middle"}]
    restart = _step(real, "restart")
    assert restart["saved"]["fan"][climate_id("studio")]["held"] is True
    assert all(snapshot["rooms"]["studio"]["fan_held"] for snapshot in real[real.index(_step(real, picked)):])
    assert [c for c in _calls(real, "studio", after=picked) if c[0] == "set_fan_mode"] == []
    assert real[-1]["heads"]["studio"][3] == "medium"


def _startup_quiet(real: list[dict[str, Any]]) -> None:
    pressed = _step(real, "press studio {'target_temp_high': 73.0}")
    assert "startup None" in pressed["waits"] and ADOPT_STUDIO not in pressed["waits"]
    assert _calls(real, "studio", after="press studio {'target_temp_high': 73.0}") == [
        ["set_temperature", {"hvac_mode": "cool", "target_temp_low": 68.0, "target_temp_high": 70.0}],
    ]
    assert [40.0, "ask"] in real[-1]["computes"]
    assert real[-1]["rooms"]["studio"]["target"] == 70.0


def _adopt_hold_reading(real: list[dict[str, Any]]) -> None:
    read = _step(real, "reading studio 75.0")
    assert read["computes"] == [[61.5, "ask"]] and read["calls"] == []
    assert ADOPT_STUDIO in read["waits"] and read["heads"]["studio"][2] == 73.0
    assert _calls(real, "studio", after="until 120.5")[0] == [
        "set_temperature", {"hvac_mode": "cool", "target_temp_low": 71.0, "target_temp_high": 73.0},
    ]
    assert real[-1]["rooms"]["studio"]["target"] == 73.0


def _startup_resume(real: list[dict[str, Any]]) -> None:
    start = real[0]
    assert start["plan"]["rooms"][0]["demand"] == "cool" and start["plan"]["rooms"][0]["engage"] == "satisfied"
    assert start["calls"] == [["studio", "set_hvac_mode", {"hvac_mode": "off"}]]
    assert start["waits"]["coast None"] == 11.0
    assert [11.0, "ask"] in real[-1]["computes"] and real[-1]["plan"]["rooms"][0]["engage"] == "cool"
    assert ["set_hvac_mode", {"hvac_mode": "cool"}] in _calls(real, "studio", after="until 120.5")


def _head_run_adopted(real: list[dict[str, Any]]) -> None:
    den = real[-1]["plan"]["rooms"][2]
    assert real[-1]["rooms"]["den"]["target"] == 71.0
    assert den["demand"] == "neutral" and den["engage"] == "heat"
    assert _calls(real, "den", after="until 120.5")[0] == [
        "set_temperature", {"hvac_mode": "heat", "target_temp_low": 71.0, "target_temp_high": 73.0},
    ]


def _enable_in_hold(real: list[dict[str, Any]]) -> None:
    assert ADOPT_STUDIO in _step(real, "until 62.5")["waits"]
    written = _step(real, "enable studio True")
    assert ADOPT_STUDIO not in written["waits"]
    assert written["calls"] == [["studio", "set_temperature",
                                 {"hvac_mode": "cool", "target_temp_low": 68.0, "target_temp_high": 70.0}]]
    assert real[-1]["rooms"]["studio"]["target"] == 70.0


OFF_HEAL_STUDIO = f"heal ('{climate_id('studio')}', 'off')"
OFF_HEAL_DEN = f"heal ('{climate_id('den')}', 'off')"


def _head_off_adopted(real: list[dict[str, Any]]) -> None:
    pressed = _step(real, "press studio {'hvac_mode': 'off'}")
    assert pressed["waits"][OFF_HEAL_STUDIO] == 30.0 and pressed["waits"][ADOPT_STUDIO] == 5.0
    adopted = _step(real, "until 70.5")
    assert adopted["rooms"]["studio"]["enable"] is False and OFF_HEAL_STUDIO not in adopted["waits"]
    assert _calls(real, "studio", after="press studio {'hvac_mode': 'off'}") == []


def _off_heal(real: list[dict[str, Any]]) -> None:
    pressed = _step(real, "press den {'hvac_mode': 'off'}")
    assert pressed["waits"][OFF_HEAL_DEN] == 30.0
    assert not any(wait.startswith("heal") and "adopt" in wait for wait in pressed["waits"])
    healed = _step(real, "until 36.0")
    assert healed["computes"] == [[35.5, "ask"]] and "startup None" in healed["waits"]
    assert healed["calls"][0] == ["den", "set_hvac_mode", {"hvac_mode": "heat"}]
    assert healed["heads"]["den"][0] == "heat"


ADOPT_DEN = f"heal ('{climate_id('den')}', 'adopt')"


def _two_holds_off(real: list[dict[str, Any]]) -> None:
    pressed = _step(real, "press den {'hvac_mode': 'off'}")
    assert pressed["waits"][ADOPT_STUDIO] == pressed["waits"][ADOPT_DEN] == 5.0
    ended = real[-1]
    assert ended["computes"][0] == [65.5, "ask"]
    assert ended["rooms"]["studio"]["enable"] is False and ended["rooms"]["den"]["enable"] is False
    assert _calls(real, "studio", after="press studio {'hvac_mode': 'off'}") == []
    assert _calls(real, "den", after="press studio {'hvac_mode': 'off'}") == []


def _two_holds_off_then_reading(real: list[dict[str, Any]]) -> None:
    _two_holds_off(real[: real.index(_step(real, "until 70.5")) + 1])
    read = _step(real, "reading loft 74.0")
    assert read["computes"] == [[70.5, "ask"]]
    assert ["loft", "set_hvac_mode", {"hvac_mode": "cool"}] in read["calls"]


def _cooldown_and_hold_off(real: list[dict[str, Any]]) -> None:
    assert _step(real, "press den {'hvac_mode': 'off'}")["waits"][ADOPT_DEN] == 5.0
    ended = real[-1]
    assert ended["computes"] == [[70.5, "cooldown"], [70.5, "followup"]]
    assert ended["rooms"]["den"]["enable"] is False and ended["rooms"]["studio"]["enable"] is False


def _dwell_and_hold(real: list[dict[str, Any]]) -> None:
    pressed = _step(real, "press studio {'hvac_mode': 'cool', 'target_temp_high': 72.0}")
    assert pressed["waits"]["hold_end None"] == pressed["waits"]["dwell None"] == pressed["waits"][ADOPT_STUDIO] == 5.0
    ended = real[-1]
    assert ended["computes"] == [[481.0, "ask"], [481.0, "followup"], [491.0, "cooldown"]]
    assert ended["rooms"]["studio"]["target"] == 72.0 and ended["heads"]["studio"][0] == "heat"
    assert ["set_hvac_mode", {"hvac_mode": "fan_only"}] not in _calls(real, "studio")


def _standoff_fan_call(real: list[dict[str, Any]]) -> None:
    assert _step(real, "until 510.0")["mode"] == "heat" and real[0]["plan"]["standoff"]
    pressed = _step(real, "press den {'target_temp_low': 73.0}")
    assert pressed["t"] == 515.0 and ADOPT_DEN in pressed["waits"]
    assert real[-1]["rooms"]["studio"]["enable"] is False and real[-1]["rooms"]["den"]["target"] == 73.0
    stepped = next(snapshot for snapshot in real if snapshot["t"] >= 570.0)
    assert ["den", "set_fan_mode", {"fan_mode": "middle"}] in stepped["calls"]
    assert [570.0, "cooldown"] in stepped["computes"]


def _restart_head_writes(real: list[dict[str, Any]]) -> None:
    first, second = (snapshot for snapshot in real if snapshot["do"] == "restart")
    remembered = first["saved"]["head_writes"][climate_id("studio")]
    assert remembered["hvac_mode"] and remembered["target_temp_high"]
    assert second["saved"]["head_writes"] == first["saved"]["head_writes"]
    assert _calls(real, "studio", after="restart") == []


# What each script shows, read off the real coordinator alone: a script that stopped reaching
# its behavior would compare equal and prove nothing.
REACHES = {
    "mixed_demand": _mixed_demand,
    "satisfied_all": _satisfied_all,
    "standoff_heartbeat": _standoff_heartbeat,
    "comfort_priority": _comfort_priority,
    "dwell_hold": _dwell_hold,
    "fan_hold": _fan_hold,
    "handback_before_off": _handback_before_off,
    "room_turned_off": _room_turned_off,
    "restart_held_fan": _restart_held_fan,
    "head_changes": _head_changes,
    "restart_comfort_priority": _restart_comfort_priority,
    "head_mode_change": _head_mode_change,
    "target_write": _target_write,
    "restart_target": _restart_target,
    "restart_fan_record": _restart_fan_record,
    "startup_quiet": _startup_quiet,
    "adopt_hold_reading": _adopt_hold_reading,
    "startup_resume": _startup_resume,
    "head_run_adopted": _head_run_adopted,
    "enable_in_hold": _enable_in_hold,
    "head_off_adopted": _head_off_adopted,
    "off_heal": _off_heal,
    "restart_head_writes": _restart_head_writes,
    "two_holds_off": _two_holds_off,
    "two_holds_off_then_reading": _two_holds_off_then_reading,
    "cooldown_and_hold_off": _cooldown_and_hold_off,
    "dwell_and_hold": _dwell_and_hold,
    "standoff_fan_call": _standoff_fan_call,
}


def test_every_script_names_what_it_reaches() -> None:
    assert set(REACHES) == set(SCRIPTS)


@pytest.mark.parametrize("name", sorted(SCRIPTS))
async def test_script_reaches_its_behavior(
    name: str, hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    REACHES[name](await Lab(hass, freezer, monkeypatch, tmp_path).run(SCRIPTS[name]))


def _differs(real: list[dict[str, Any]], page: list[dict[str, Any]], left_out: tuple[str, ...] = ()) -> bool:
    return len(real) != len(page) or any(
        _compared(a, left_out) != _compared(b, left_out) for a, b in zip(real, page, strict=False)
    )


# The row runs a compute, then each timer still due at its instant inside it (``MxzRow._settle``).
INSIDE_THE_COMPUTE = (
    "        while self.due() <= self.now:\n"
    "            self._settle(self._step(st.Tick()))\n"
    "        self._settle(self._step(computed), ended=True)\n"
)
# Broken: every timer due at the compute's instant is taken off before it runs and done after,
# so the compute reads an adopt hold due then as already over (``sim_timing``'s tick before
# planner1120).
TAKEN_BEFORE_THE_COMPUTE = "        computed = self._compute()\n"
TAKEN_BEFORE_THE_COMPUTE_BROKEN = (
    "        taken = []\n"
    "        while self.due() <= self.now:\n"
    "            taken.append(self._step(st.Tick()))\n"
    "        computed = self._compute()\n"
    "        for each in taken:\n"
    "            self._settle(each)\n"
)

# One line of the page's row broken at a time, each where a script above must notice.
BREAKS = {
    "the hold-end wakeup at a start": (
        "restart_held_fan", "self._step(self._hold_end_arm())", "None",
    ),
    "a target a person set at the head": (
        "head_changes", "                self.set_target(room.key, value)\n", "                pass\n",
    ),
    "Comfort priority across a restart": (
        "restart_comfort_priority", "self.start(record, names=names, comfort=comfort)",
        "self.start(record, names=names)",
    ),
    "an enable a person set at the head": (
        "head_mode_change", "                self.set_enable(room.key, on)\n", "                pass\n",
    ),
    "a target write's steps": (
        "target_write", "pl.target_written(", "(lambda *_args, **_kwargs: ())(",
    ),
    "the latch the fan step keeps": (
        "fan_hold", "            case fl.KeepLatch(latch):\n                room.latch = latch\n",
        "            case fl.KeepLatch(latch):\n                pass\n",
    ),
    "the saved target at a start": (
        "restart_target", "                room.target = saved_room.target\n", "                pass\n",
    ),
    "the fan record at a start": (
        "restart_fan_record",
        (
            "                room.latch = fl.restored(\n"
            "                    room.latch, fan.held, last=fan.last, prior=fan.prior, older=fan.older,\n"
            "                    on_pending=fan.on_pending, ceiling=s.ceiling,\n"
            "                )\n"
        ),
        "                pass\n",
    ),
    "the startup quiet": (
        "startup_quiet", "self.starting = tm.startup_quiet(running=False, startup_ran=False)",
        "self.starting = False",
    ),
    "the adopt hold in an apply": (
        "adopt_hold_reading",
        (
            "                settling=lambda room=room: pl.holds_change(\n"
            "                    self.waiting(\"heal\", (room.climate_id, \"adopt\")),\n"
            "                    lambda: self._adoption(room, room.pending),\n"
            "                ),\n"
        ),
        "                settling=lambda: False,\n",
    ),
    "the startup seed": (
        "startup_resume", "                self.latch, self.startup = seed.latch, seed.startup\n", "",
    ),
    "a Latch step": (
        "head_run_adopted",
        (
            "                if entry is None:\n"
            "                    self.latch.pop(room.slug, None)\n"
            "                else:\n"
            "                    self.latch[room.slug] = entry\n"
        ),
        "                pass\n",
    ),
    "an enable write's steps": (
        "enable_in_hold", "pl.enable_written(", "(lambda *_args, **_kwargs: ())(",
    ),
    "the off heal's recheck": (
        "head_off_adopted",
        "                if cancel is not None:\n                    self._arm(cancel)\n",
        "                pass\n",
    ),
    "the off heal's compute": (
        "off_heal", "                self._ask(pl.heal_due(", "                (lambda _refresh: None)(pl.heal_due(",
    ),
    "head-write memories at a start": (
        "restart_head_writes", "            room.memories.update(saved_room.head_writes)\n", "",
    ),
    "a timer due at a compute's start, still armed while it runs": (
        "two_holds_off", TAKEN_BEFORE_THE_COMPUTE, TAKEN_BEFORE_THE_COMPUTE_BROKEN,
    ),
    "a timer due at a compute's start runs inside it": (
        "cooldown_and_hold_off", INSIDE_THE_COMPUTE, "        self._settle(self._step(computed), ended=True)\n",
    ),
    "an adopt hold due at the dwell wakeup, still on while its compute runs": (
        "dwell_and_hold", TAKEN_BEFORE_THE_COMPUTE, TAKEN_BEFORE_THE_COMPUTE_BROKEN,
    ),
    "an adopt hold end goes on inside the compute after its own": (
        "two_holds_off_then_reading", "            self._go_on()\n", "            pass\n",
    ),
    "a timer due at a compute's start runs inside it, in the standoff": (
        "standoff_fan_call", INSIDE_THE_COMPUTE, "        self._settle(self._step(computed), ended=True)\n",
    ),
}


def _broken(tmp_path_factory: pytest.TempPathFactory, what: str) -> Path:
    """The shipped tree with the line ``BREAKS[what]`` names broken in its ``mxz_row.py``."""
    _name, old, new = BREAKS[what]
    return _broken_row(tmp_path_factory, old, new)


def _broken_row(tmp_path_factory: pytest.TempPathFactory, old: str, new: str) -> Path:
    """The shipped tree with ``old`` replaced by ``new`` in its ``mxz_row.py``."""
    broken = tmp_path_factory.mktemp("broken")
    _bundler().write_tree(broken)
    source = broken / "mxz_row.py"
    text = source.read_text()
    assert text.count(old) == 1, old
    source.write_text(text.replace(old, new))
    return broken


@pytest.mark.parametrize("what", sorted(BREAKS))
async def test_a_broken_page_row_is_caught(
    what: str, tree: Path, tmp_path_factory: pytest.TempPathFactory, hass: Any, freezer: Any,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    name, _old, _new = BREAKS[what]
    broken = _broken(tmp_path_factory, what)
    real = await Lab(hass, freezer, monkeypatch, tmp_path).run(SCRIPTS[name])
    try:
        page = run_page(broken, SCRIPTS[name])
    except AssertionError:
        return  # the broken row failed outright, which is caught too
    assert _differs(real, page, _left_out(name)), what


# A person raises the cool setpoint at Studio's and Den's remotes at one instant. On the real
# coordinator both become targets on every run, and every call to each head is the same, but
# not how many computes follow: Home Assistant interleaves the two heads' deliveries, so a
# head's report lands before or after the next compute, where the page sends to one head after
# another (docs/simulator/README.md). So this compares each step's calls to each head and what
# the rooms, the heads and the shared mode show, and not the computes, waits or plan.
TWO_SETPOINTS = _script(
    [
        ["until", 60.5],
        ["press", "studio", {"target_temp_high": 73.0}],
        ["press", "den", {"target_temp_high": 74.0}],
        ["until", 120.5],
    ],
    temps={"studio": 76.0, "den": 76.0},
)


def _taken(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"do": s["do"], "calls": modeled(s)["calls"], "rooms": s["rooms"], "heads": s["heads"], "mode": s["mode"]}
        for s in snapshots
    ]


async def test_two_setpoints_pressed_at_once_are_both_taken(
    tree: Path, hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    real = await Lab(hass, freezer, monkeypatch, tmp_path).run(TWO_SETPOINTS)
    rooms = real[-1]["rooms"]
    assert rooms["studio"]["target"] == 73.0 and rooms["den"]["target"] == 74.0
    assert _calls(real, "den", after="press den {'target_temp_high': 74.0}")[0] == [
        "set_temperature", {"hvac_mode": "cool", "target_temp_low": 72.0, "target_temp_high": 74.0},
    ]
    assert _taken(run_page(tree, TWO_SETPOINTS)) == _taken(real)


async def test_two_setpoints_with_a_compute_between_hold_ends_is_caught(
    tmp_path_factory: pytest.TempPathFactory, hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A row whose compute takes the second hold end's timer off before it runs reads that hold
    as over, writes Den's head back to 70 and keeps its target: the comparison above catches it."""
    broken = _broken(tmp_path_factory, "a timer due at a compute's start, still armed while it runs")
    real = await Lab(hass, freezer, monkeypatch, tmp_path).run(TWO_SETPOINTS)
    page = run_page(broken, TWO_SETPOINTS)
    assert page[-1]["rooms"]["den"]["target"] == 70.0
    assert _taken(page) != _taken(real)


async def test_a_page_setting_that_differs_is_caught(
    tree: Path, hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The page at a 5-minute hold is not the coordinator at its 10-minute default."""
    script = {**SCRIPTS["dwell_hold"], "page_settings": {"hold": 300.0}}
    real = await Lab(hass, freezer, monkeypatch, tmp_path).run(script)
    assert _differs(real, run_page(tree, script))


async def test_the_target_takes_what_home_assistant_takes(
    tree: Path, hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The page's set-to control offers, and its MXZ row takes as the room's target, the range
    the room's target number has in Home Assistant, and refuses a value outside it as Home
    Assistant does. A refused value changes neither row; a taken one sets the stock head too."""
    lab = Lab(hass, freezer, monkeypatch, tmp_path)
    await lab.start(SCRIPTS["mixed_demand"])
    number = _eid(hass, lab.entry, f"_{lab._slug('studio')}_target")
    shown = hass.states.get(number)
    assert shown is not None
    low, high = shown.attributes["min"], shown.attributes["max"]
    for outside in (low - 1, high + 1):
        with pytest.raises(ServiceValidationError) as refused:
            await hass.services.async_call(
                "number", "set_value", {"entity_id": number, "value": outside}, blocking=True,
            )
        assert refused.value.translation_key == "out_of_range"
    bounds, taken = _page_targets(tree, [low - 1, low, high, high + 1])
    assert bounds == [low, high]
    assert [[refused is None, target, stock] for refused, target, stock in taken] == [
        [False, 70.0, 70.0], [True, low, low], [True, high, high], [False, high, high],
    ]
    assert [refused for refused, _, _ in taken[::3]] == [
        f"Target must be {low:g} to {high:g} °F. Home Assistant refused {value:g} °F." for value in (low - 1, high + 1)
    ]
    assert "view.mxz.target_bounds" in (PAGE / "lab.js").read_text()


# The page's world: the target bounds it offers, then what each set-to value does to Studio's
# MXZ target and to its stock head, which is set to cool, so it runs to its high setpoint.
TARGETS = """
import json, sys
sys.path.insert(0, sys.argv[1])
import world
w = world.World("mixed")
taken = []
for value in json.loads(sys.argv[2]):
    refused = w.set_to("studio", value)
    view = json.loads(w.view())
    taken.append([refused, view["mxz"]["rooms"][0]["target"], view["stock"]["rooms"][0]["high"]])
print(json.dumps([json.loads(w.view())["mxz"]["target_bounds"], taken]))
"""


def _page_targets(tree: Path, values: list[float]) -> Any:
    done = subprocess.run(
        [sys.executable, "-I", "-c", TARGETS, str(tree), json.dumps(values)],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_several_readings_in_one_dispatch_are_refused(tree: Path) -> None:
    """A dispatch that would ask for more than one compute is not modeled, and says so."""
    script = _script([["until", 60.5], ["readings", {"studio": 71.0, "loft": 69.0}]])
    with pytest.raises(AssertionError, match="NotModeled: several entities asking"):
        run_page(tree, script)


# One dispatch: Studio's fan changes (its report asks for a compute) and Den's head shows a new
# low setpoint (its report asks nothing). Prints what Den showed when the compute read it.
DISPATCH = """
import sys
sys.path.insert(0, sys.argv[1])
from invented_house import devices as dv
import mxz_row
house = dv.load_house()
devices = dv.Devices.fresh(house)
devices.rooms["studio"].temperature = 76.0
heads = mxz_row.InventedHeads(house, devices, modelled=True)
row = mxz_row.MxzRow(heads, start=0.0, wall=0.0)
keys = [room["key"] for room in house["rooms"]]
row.start({"switches": {"coordinator_enable": True},
           "rooms": {mxz_row.climate_id(k): {"target": 70.0, "enable": True} for k in keys}},
          names={k: k for k in keys})
row.advance(60.5)
seen = []
compute = row._compute
def watched():
    seen.append(row._room("den").shown[1]["target_temp_low"])
    return compute()
row._compute = watched
devices.heads["studio"].fan_mode = "low"
devices.heads["den"].target_temp_low = 65.0
heads.changed()
row.world_changed()
print(seen)
"""


def _dispatch(tree: Path) -> str:
    done = subprocess.run(
        [sys.executable, "-I", "-c", DISPATCH, str(tree)], capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def test_a_dispatch_is_read_whole_before_its_ask(tree: Path, tmp_path: Path) -> None:
    """Home Assistant runs every report of one dispatch before the compute one of them asks for:
    the compute reads Den's new setpoint. A row that computes at the first ask reads the old one."""
    assert _dispatch(tree) == "[65]"
    subprocess.run([sys.executable, str(BUNDLER), "--tree", str(tmp_path)], check=True)
    source = tmp_path / "mxz_row.py"
    text = source.read_text()
    old = "            self._dispatch.append(refresh)"
    assert text.count(old) == 1
    source.write_text(text.replace(old, "            self._settle(self._step(st.Ask(refresh)))"))
    assert _dispatch(tmp_path) == "[None]"


# -- the bundle --------------------------------------------------------------------------------
def _bundler() -> Any:
    spec = importlib.util.spec_from_file_location("simulator_bundle", BUNDLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_bundle_is_what_the_sources_make() -> None:
    assert subprocess.run([sys.executable, str(BUNDLER), "--check"], check=False).returncode == 0


def test_a_stale_bundle_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundler = _bundler()
    stale = tmp_path / "bundle.js"
    stale.write_text(bundler.render().replace("def climate_id", "def climate_id_renamed", 1))
    monkeypatch.setattr(bundler, "BUNDLE", stale)
    monkeypatch.setattr(bundler, "ROOT", tmp_path)
    assert bundler.main(["--check"]) == 1


def test_the_page_loads_only_standard_library_modules() -> None:
    bundler = _bundler()
    modules = bundler.integration_modules()
    assert {"planner", "timing", "sim_timing", "fan_latch", "grid", "head_writes", "resume"} <= set(modules)
    assert set(modules) <= bundler.STDLIB_ONLY
    for path, text in bundler.files().items():
        if path.endswith(".py"):
            for node in ast.walk(ast.parse(text)):
                names = (
                    [alias.name for alias in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                )
                assert not any(name.split(".")[0] == "homeassistant" for name in names), path


def test_a_module_that_needs_home_assistant_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    bundler = _bundler()
    monkeypatch.setattr(bundler, "STDLIB_ONLY", bundler.STDLIB_ONLY - {"fan_latch"})
    with pytest.raises(SystemExit, match="fan_latch is not in STDLIB_ONLY"):
        bundler.integration_modules()


# -- the page ----------------------------------------------------------------------------------
PAGE = ROOT / "docs" / "simulator"
PYODIDE = "https://cdn.jsdelivr.net/pyodide/v314.0.8/full/"
# Where the page is served (GitHub Pages, from main's docs/) and where its back link goes. A
# relative link to the README works from a clone and 404s on Pages, which serves only docs/.
LIVE = "https://dkpnw.github.io/ha-mxz-coordinator/simulator/"
BACK = "https://github.com/dkpnw/ha-mxz-coordinator#readme"
# What the page and docs/simulator/README.md say about the row's one ordering rule of its own
# (``MxzRow._go_on``; ``two_holds_off_then_reading`` fails without it), as words read across
# line breaks.
HOLD_END_RULE = "No MXZ function says so, so this page follows that order by a rule of its own"


def _words(text: str) -> str:
    return " ".join(text.split())


def _page_problems(html: str, css: str) -> list[str]:
    """What the page must say and load: the Simulation band that stays on screen, the stock
    rule as the README states it, what it leaves out, the pinned Pyodide, and a back link that
    works on Pages."""
    problems = []
    band = re.search(r'<header class="band"[^>]*>(.*?)</header>', html, re.DOTALL)
    if band is None or "<strong>Simulation</strong> · invented heads and rooms" not in band.group(1):
        problems.append("no Simulation band")
    if not re.search(r"\.band\s*\{[^}]*position:\s*sticky;\s*top:\s*0", css):
        problems.append("the band does not stay on screen")
    for words in ('"the unit selected last goes into standby mode."', "It is not stock AUTO.",
                  "Neither row measures real equipment"):
        if words not in html:
            problems.append(f"missing: {words}")
    for left_out in ("shared-mode select", "interleave", "A command a newer plan replaces",
                     "up to 10 seconds later than in Home Assistant"):
        if left_out not in html:
            problems.append(f"does not say it leaves out: {left_out}")
    if HOLD_END_RULE not in _words(html):
        problems.append(f"does not name the row's own rule: {HOLD_END_RULE}")
    pinned = re.search(rf'<script src="{re.escape(PYODIDE)}pyodide\.js"\s+integrity="sha384-[\w+/=]+"', html)
    if pinned is None:
        problems.append("Pyodide is not pinned with its integrity hash")
    if '<script src="bundle.js"></script>' not in html:
        problems.append("the bundle is not loaded")
    if f'<a href="{BACK}">Back to the README</a>' not in html:
        problems.append("the back link does not open the README on GitHub")
    if not re.search(r'<button type="button" data-speed="10" aria-pressed="true"', html):
        problems.append("10× is not the speed it starts at")
    if '<button id="replay" type="button"' not in html:
        problems.append("no Replay button")
    panel = html.find('id="shared-controls"')
    if html.count('id="shared-controls"') != 1 or not 0 < panel < html.find('id="stock-rooms"'):
        problems.append("no one shared control panel above the rows")
    rows = re.findall(r'<section class="row".*?</section>', html, re.DOTALL)
    if len(rows) != 2 or any(re.search(r"<(?:input|select|button|textarea)\b", row) for row in rows):
        problems.append("a row has controls of its own")
    return problems


def test_the_page_says_what_it_is() -> None:
    assert _page_problems((PAGE / "index.html").read_text(), (PAGE / "lab.css").read_text()) == []
    assert f'const PYODIDE = "{PYODIDE}";' in (PAGE / "lab.js").read_text()


def test_the_page_check_reports_each_missing_part() -> None:
    html = (PAGE / "index.html").read_text()
    css = (PAGE / "lab.css").read_text()
    broken = (html.replace("<strong>Simulation</strong>", "<strong>Demo</strong>")
              .replace("It is not stock AUTO.", "")
              .replace("shared-mode select", "select")
              .replace("up to 10 seconds later", "later")
              .replace("No MXZ function says so", "MXZ says so")
              .replace('integrity="sha384-', 'data-x="sha384-')
              .replace(BACK, "../../README.md")
              .replace('data-speed="10" aria-pressed="true"', 'data-speed="10"')
              .replace('<button id="replay"', '<button id="again"')
              .replace('id="shared-controls"', 'id="controls-panel"')
              .replace("<summary>Commands MXZ sent", '<button type="button">Restart</button><summary>Commands MXZ sent'))
    assert _page_problems(broken, css.replace("position: sticky", "position: static")) == [
        "no Simulation band",
        "the band does not stay on screen",
        "missing: It is not stock AUTO.",
        "does not say it leaves out: shared-mode select",
        "does not say it leaves out: up to 10 seconds later than in Home Assistant",
        f"does not name the row's own rule: {HOLD_END_RULE}",
        "Pyodide is not pinned with its integrity hash",
        "the back link does not open the README on GitHub",
        "10× is not the speed it starts at",
        "no Replay button",
        "no one shared control panel above the rows",
        "a row has controls of its own",
    ]
    assert "speed: 10," in (PAGE / "lab.js").read_text()


def test_the_simulator_readme_names_the_rows_own_rule() -> None:
    readme = (PAGE / "README.md").read_text()
    assert HOLD_END_RULE in _words(readme)
    assert HOLD_END_RULE not in _words(readme.replace("No MXZ function says so", "MXZ says so"))


def test_the_readme_links_the_simulator_beside_the_hook() -> None:
    readme = (ROOT / "README.md").read_text()
    caption = readme.split("</picture>", 1)[1].split("</sub>", 1)[0]
    assert f"[Try it yourself in the simulator]({LIVE})" in caption
    assert "[run it from your own copy](docs/simulator/README.md#run-it-from-your-own-copy)" in caption
    assert "](simulator/)" in (ROOT / "docs" / "HOW-IT-WORKS.md").read_text()


def test_the_simulator_readme_leads_with_the_live_page() -> None:
    first = (PAGE / "README.md").read_text().split("\n\n", 2)[1]
    assert first == f"Open the simulator at {LIVE}."


def _root_problems(html: str) -> list[str]:
    """What the Pages root (docs/index.html) must do: send a visitor on to the simulator, and
    link it for a browser that ignores the refresh."""
    problems = []
    if '<meta http-equiv="refresh" content="0; url=simulator/">' not in html:
        problems.append("does not refresh to simulator/")
    if '<a href="simulator/">' not in html:
        problems.append("does not link simulator/")
    return problems


def test_the_pages_root_sends_you_to_the_simulator() -> None:
    html = (ROOT / "docs" / "index.html").read_text()
    assert _root_problems(html) == []
    assert _root_problems(html.replace("url=simulator/", "url=../").replace('href="simulator/"', 'href="#"')) == [
        "does not refresh to simulator/",
        "does not link simulator/",
    ]


# -- the page's starting points, run for 40 simulated minutes from the bundle --------------------
PRESETS = """
import json, sys
sys.path.insert(0, sys.argv[1])
import world
out = {}
for name in world.PRESETS:
    w = world.World(name)
    seen = []
    for until in range(30, 2401, 30):
        if name in ("fan_hold", "restart") and until == 120:
            w.set_fan("studio", "low")
        w.advance(until)
        seen.append(json.loads(w.view()))
    out[name] = seen
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def presets(tree: Path) -> dict[str, list[dict[str, Any]]]:
    done = subprocess.run([sys.executable, "-I", "-c", PRESETS, str(tree)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _hardcoded_titles(js: str, titles: list[str]) -> list[str]:
    """Scenario titles the page's script spells out itself instead of reading world.presets()."""
    return [title for title in titles if title in js]


def test_the_page_reads_scenario_titles_from_world(tree: Path) -> None:
    done = subprocess.run([sys.executable, "-I", "-c", PRESET_LIST, str(tree)],
                          capture_output=True, text=True, check=True)
    listed = json.loads(done.stdout)
    assert [entry["name"] for entry in listed] == ["mixed", "satisfied", "standoff", "mode_wait", "fan_hold", "restart"]
    assert [entry["name"] for entry in listed if entry["more"]] == ["mode_wait", "fan_hold", "restart"]
    js = (PAGE / "lab.js").read_text()
    assert "lab.module.presets()" in js
    titles = [entry["title"] for entry in listed]
    assert _hardcoded_titles(js, titles) == []
    assert _hardcoded_titles(js + '\nconst PRESETS = [["mixed", "One room wants cool"]];', titles) == [
        "One room wants cool"]  # negative: a second copy of a title


PRESET_LIST = """
import sys
sys.path.insert(0, sys.argv[1])
import world
print(world.presets())
"""

# A fan pick of a speed MXZ's head does not show, made while MXZ's start is quiet: at 0:00 in
# "Manual fan speed", and right after the restart at 5:00 in "Restart during the wait". Each case
# also runs without the pick, to show the hold comes from it.
QUIET_PICK = """
import json, sys
sys.path.insert(0, sys.argv[1])
import world
def studio(w):
    view = json.loads(w.view())
    return [view["stock"]["rooms"][0]["fan"], view["mxz"]["rooms"][0]["fan"], view["mxz"]["rooms"][0]["fan_held"]]
out = {}
for name, at in (("fan_hold", 0), ("restart", 300)):
    case = {}
    w = world.World(name)
    w.advance(at)
    shown = studio(w)[1]
    pick = next(fan for fan in ("quiet", "low", "medium") if fan != shown)
    for picked in (True, False):
        w = world.World(name)
        w.advance(at)
        quiet = w.row.starting
        if picked:
            w.set_fan("studio", pick)
        w.advance(at + 15)
        early = [w.row.starting, studio(w)]
        seen = []
        for later in range(at + 30, at + 601, 30):
            w.advance(later)
            seen.append(studio(w))
        case["picked" if picked else "not picked"] = {"quiet": quiet, "early": early, "seen": seen}
    out[f"{name} {at}"] = {"shown": shown, "pick": pick, **case}
print(json.dumps(out))
"""


def test_a_fan_pick_during_the_startup_quiet_is_held(tree: Path) -> None:
    """MXZ's startup quiet keeps it from taking a mode or setpoint changed at a head, and the
    fan is the only control on the page pressed at a head. A fan pick inside the quiet is held
    in both rows within 15 seconds, while the quiet still runs, and for the next 10 minutes. So
    the page says nothing about MXZ ignoring a change."""
    done = subprocess.run([sys.executable, "-I", "-c", QUIET_PICK, str(tree)], capture_output=True, text=True,
                          check=True)
    cases = json.loads(done.stdout)
    assert set(cases) == {"fan_hold 0", "restart 300"}
    for name, case in cases.items():
        pick, picked, alone = case["pick"], case["picked"], case["not picked"]
        assert pick != case["shown"], name
        assert picked["quiet"] and alone["quiet"], name  # the pick lands inside the quiet
        assert picked["early"] == [True, [pick, pick, True]], (name, picked["early"])
        assert all(seen == [pick, pick, True] for seen in picked["seen"]), (name, picked["seen"])
        # negative: without the pick MXZ runs the fan and never holds it
        assert not alone["early"][1][2] and not any(held for _, _, held in alone["seen"]), (name, alone)
    page = (PAGE / "index.html").read_text() + (PAGE / "lab.js").read_text()
    assert "ignor" not in page.lower()


def test_mixed_preset_names_coil_drying_while_the_head_dries(presets: dict[str, list[dict[str, Any]]]) -> None:
    """At its default idle action MXZ parks a head on fan only to dry the coil after cooling, then
    turns it off. drying reads the dry wakeup, so over 40 minutes it is on exactly while a head is on
    fan only: Studio from 6:30, and Loft and Den once they drift up, are cooled and dry in turn."""
    rooms = [room for view in presets["mixed"] for room in view["mxz"]["rooms"]]
    assert [room["drying"] for room in rooms] == [room["mode"] == "fan_only" for room in rooms]
    assert {room["key"] for room in rooms if room["drying"]} == {"studio", "loft", "den"}
    assert [_room(view, "mxz", "studio")["drying"] for view in presets["mixed"][11:14]] == [False, True, True]
    assert not _room(presets["mixed"][-1], "mxz", "studio")["drying"]  # dried and off by 40:00


def _room(view: dict[str, Any], side: str, key: str) -> dict[str, Any]:
    return next(room for room in view[side]["rooms"] if room["key"] == key)


def test_mixed_preset_stock_waits_and_mxz_cools(presets: dict[str, list[dict[str, Any]]]) -> None:
    at_10 = presets["mixed"][19]
    assert at_10["now"] == 600.0
    assert _room(at_10, "stock", "studio")["doing"] == "standby" and at_10["stock"]["outdoor"] == "heat"
    assert _room(at_10, "mxz", "studio")["temperature"] <= 70.5 and at_10["mxz"]["mode"] == "cool"


def test_satisfied_preset_stock_idles_in_heat_and_mxz_turns_heads_off(
    presets: dict[str, list[dict[str, Any]]],
) -> None:
    at_10 = presets["satisfied"][19]
    assert {room["doing"] for room in at_10["stock"]["rooms"]} == {"idle"} and at_10["stock"]["outdoor"] == "heat"
    assert {room["mode"] for room in at_10["mxz"]["rooms"]} == {"off"} and at_10["mxz"]["outdoor"] is None


def test_standoff_preset_cools_studio_then_heats_den_while_stock_den_waits(
    presets: dict[str, list[dict[str, Any]]],
) -> None:
    """Studio, first in the room order, keeps cool until it is within 3 °F of its 70 °F target
    (73 °F at 6:00); then MXZ heats Den. Stock's outdoor unit stays in cool and its Den waits in
    standby for all 40 minutes. Loft is set to off, so it is off in both rows. (Comfort
    priority, which the page no longer offers, is pinned by the comfort_priority script.)"""
    views = presets["standoff"]
    before, after = views[11], views[12]
    assert (before["now"], before["mxz"]["mode"], before["mxz"]["standoff"]) == (360.0, "cool", True)
    assert _room(before, "mxz", "studio")["temperature"] == 73.0
    assert all(view["mxz"]["mode"] == "cool" for view in views[:12])
    assert after["mxz"]["mode"] == "heat" and _room(after, "mxz", "den")["mode"] == "heat"
    assert all(view["stock"]["outdoor"] == "cool" and _room(view, "stock", "den")["doing"] == "standby"
               for view in views)
    assert all(not _room(view, "mxz", "loft")["enable"] and _room(view, "stock", "loft")["mode"] == "off"
               for view in views)


def test_mode_wait_preset_holds_until_the_wait_ends(presets: dict[str, list[dict[str, Any]]]) -> None:
    views = presets["mode_wait"]
    assert all(view["mxz"]["mode"] == "heat" for view in views if view["now"] < 540)
    assert next(view for view in views if view["mxz"]["mode"] == "cool")["now"] == 570.0


def test_fan_hold_preset_keeps_the_pick(presets: dict[str, list[dict[str, Any]]]) -> None:
    for view in presets["fan_hold"][3:]:
        studio = _room(view, "mxz", "studio")
        assert studio["fan_held"] and studio["fan"] == "low"


def test_restart_preset_carries_the_wait_and_the_pick(presets: dict[str, list[dict[str, Any]]]) -> None:
    """Home Assistant restarts at 5:00 in this scenario. The 10-minute wait ends when it would
    have without the restart, and Studio's fan, picked at 2:00, stays held through it."""
    views = presets["restart"]
    before, after = (next(view for view in views if view["now"] == now) for now in (270.0, 300.0))
    assert (before["mxz"]["restarts"], after["mxz"]["restarts"]) == (0, 1)
    ends = [view["now"] + view["mxz"]["hold_left"] for view in (before, after)]
    assert ends[0] == ends[1] > after["now"]
    assert _room(before, "mxz", "studio")["fan_held"] and _room(after, "mxz", "studio")["fan_held"]
    assert all(view["mxz"]["restarts"] == 1 for view in views[views.index(after):])


# Each of the page's controls, on Den in the "mixed" scenario after MXZ's start is quiet, and
# what each row shows after it: the stock head, and MXZ's room.
CONTROLS = """
import json, sys
sys.path.insert(0, sys.argv[1])
import world
w = world.World("mixed")
w.advance(120)
def den():
    view = json.loads(w.view())
    stock, mxz = view["stock"]["rooms"][2], view["mxz"]["rooms"][2]
    return {"stock": [stock["temperature"], stock["mode"], stock["low"], stock["fan"]],
            "mxz": [mxz["temperature"], mxz["enable"], mxz["target"], mxz["fan_held"], mxz["fan"]]}
out = {"start": den()}
w.set_temperature("den", 64); out["temperature 64"] = den()
w.set_to("den", 72); out["set to 72"] = den()
w.set_to("den", 95); out["set to 95"] = den()
w.set_fan("den", "low"); w.advance(130); out["fan low"] = den()
w.set_fan("den", "auto"); w.advance(140); out["fan auto"] = den()
w.set_mode("den", "off"); w.advance(150); out["off"] = den()
w.set_mode("den", "cool"); w.advance(160); out["cool"] = den()
print(json.dumps(out))
"""


def test_each_control_changes_both_rows(tree: Path) -> None:
    """One control, one change in each row: the room's temperature in both; the set-to as the
    stock head's setpoint and MXZ's target, or neither when Home Assistant refuses it; the fan
    on the stock head and, pressed at MXZ's head, a held speed that auto hands back; off as the
    stock head's mode and MXZ's room switch, and heat or cool turning the room back on."""
    done = subprocess.run([sys.executable, "-I", "-c", CONTROLS, str(tree)], capture_output=True, text=True, check=True)
    seen = json.loads(done.stdout)
    assert seen["start"]["stock"][1:] == ["heat", 70.0, "auto"] and seen["start"]["mxz"][1:4] == [True, 70.0, False]
    assert seen["temperature 64"]["stock"][0] == seen["temperature 64"]["mxz"][0] == 64
    assert seen["set to 72"]["stock"][2] == seen["set to 72"]["mxz"][2] == 72.0
    assert seen["set to 95"] == seen["set to 72"]  # negative: refused, so neither row moved
    assert seen["fan low"]["stock"][3] == "low" and seen["fan low"]["mxz"][3:] == [True, "low"]
    assert seen["fan auto"]["stock"][3] == "auto" and seen["fan auto"]["mxz"][3] is False
    assert seen["off"]["stock"][1] == "off" and seen["off"]["mxz"][1] is False
    assert seen["cool"]["stock"][1] == "cool" and seen["cool"]["mxz"][1] is True


# A fan pick of the speed MXZ's head already shows, in each scenario that cools Studio on a
# boosted fan: at 1:03, after MXZ's start is quiet, and at 0:00, inside it. Each case also runs
# without the pick, to show the fan would have stepped.
SAME_SPEED = """
import json, sys
sys.path.insert(0, sys.argv[1])
import world
def studio(w):
    view = json.loads(w.view())
    return [view["stock"]["rooms"][0]["fan"], view["mxz"]["rooms"][0]["fan"], view["mxz"]["rooms"][0]["fan_held"]]
out = {}
for name, at in (("fan_hold", 63), ("fan_hold", 0), ("mixed", 63), ("restart", 63)):
    case = {}
    for pick in (True, False):
        w = world.World(name)
        w.advance(at)
        shown = studio(w)[1]
        before = len(w.row.log.calls)
        if pick:
            w.set_fan("studio", shown)
        seen = [studio(w)]
        for later in range(at + 30, at + 601, 30):
            w.advance(later)
            seen.append(studio(w))
        fan_calls = [c for c in w.row.log.calls[before:] if c[1] == "studio" and c[2] == "set_fan_mode"]
        case["picked" if pick else "not picked"] = {"shown": shown, "seen": seen, "fan_calls": len(fan_calls)}
    out[f"{name} {at}"] = case
print(json.dumps(out))
"""


def test_picking_the_speed_the_mxz_head_shows_holds_it(tree: Path) -> None:
    """A pick of the speed MXZ's head already shows is held in both rows for the next 10
    minutes, through the restart scenario's restart at 5:00, and MXZ sends that head no fan
    command. Without the pick, MXZ steps the same fan within those 10 minutes."""
    done = subprocess.run([sys.executable, "-I", "-c", SAME_SPEED, str(tree)], capture_output=True, text=True,
                          check=True)
    cases = json.loads(done.stdout)
    assert set(cases) == {"fan_hold 63", "fan_hold 0", "mixed 63", "restart 63"}
    for name, case in cases.items():
        picked, alone = case["picked"], case["not picked"]
        assert picked["shown"] == alone["shown"] == "high", name
        assert all(seen == ["high", "high", True] for seen in picked["seen"]), (name, picked["seen"])
        assert picked["fan_calls"] == 0, name
        # negative: without the pick MXZ runs the fan, so it is never held and it steps down
        assert not any(held for _, _, held in alone["seen"]), name
        assert {fan for _, fan, _ in alone["seen"]} - {"high"}, (name, alone["seen"])
        assert alone["fan_calls"] > 0, name
