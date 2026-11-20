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


@pytest.mark.parametrize("name", sorted(SCRIPTS))
async def test_page_row_is_the_coordinator(
    name: str, tree: Path, hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    real, page = await _both(SCRIPTS[name], tree, hass, freezer, monkeypatch, tmp_path)
    assert len(page) == len(real)
    for want, got in zip(real, page, strict=True):
        assert modeled(got) == modeled(want), want["do"]


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
}


def test_every_script_names_what_it_reaches() -> None:
    assert set(REACHES) == set(SCRIPTS)


@pytest.mark.parametrize("name", sorted(SCRIPTS))
async def test_script_reaches_its_behavior(
    name: str, hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    REACHES[name](await Lab(hass, freezer, monkeypatch, tmp_path).run(SCRIPTS[name]))


def _differs(real: list[dict[str, Any]], page: list[dict[str, Any]]) -> bool:
    return len(real) != len(page) or any(modeled(a) != modeled(b) for a, b in zip(real, page, strict=False))


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
}


@pytest.mark.parametrize("what", sorted(BREAKS))
async def test_a_broken_page_row_is_caught(
    what: str, tree: Path, tmp_path_factory: pytest.TempPathFactory, hass: Any, freezer: Any,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    name, old, new = BREAKS[what]
    broken = tmp_path_factory.mktemp("broken")
    _bundler().write_tree(broken)
    source = broken / "mxz_row.py"
    text = source.read_text()
    assert text.count(old) == 1, what
    source.write_text(text.replace(old, new))
    real = await Lab(hass, freezer, monkeypatch, tmp_path).run(SCRIPTS[name])
    try:
        page = run_page(broken, SCRIPTS[name])
    except AssertionError:
        return  # the broken row failed outright, which is caught too
    assert _differs(real, page), what


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
    """The page's target control offers, and its row takes, the range the room's target number
    has in Home Assistant, and refuses a value outside it as Home Assistant does."""
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
    assert [[refused is None, target] for refused, target in taken] == [
        [False, 70.0], [True, low], [True, high], [False, high],
    ]
    assert "view.mxz.target_bounds" in (PAGE / "lab.js").read_text()


# The page's world: the target bounds it offers, then what each value does to Studio's target.
TARGETS = """
import json, sys
sys.path.insert(0, sys.argv[1])
import world
w = world.World("mixed")
taken = []
for value in json.loads(sys.argv[2]):
    refused = w.mxz_target("studio", value)
    taken.append([refused, json.loads(w.view())["mxz"]["rooms"][0]["target"]])
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


def _page_problems(html: str, css: str) -> list[str]:
    """What the page must say and load: the Simulation band that stays on screen, the stock
    rule as the README states it, what it leaves out, and the pinned Pyodide."""
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
    pinned = re.search(rf'<script src="{re.escape(PYODIDE)}pyodide\.js"\s+integrity="sha384-[\w+/=]+"', html)
    if pinned is None:
        problems.append("Pyodide is not pinned with its integrity hash")
    if '<script src="bundle.js"></script>' not in html:
        problems.append("the bundle is not loaded")
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
              .replace('integrity="sha384-', 'data-x="sha384-'))
    assert _page_problems(broken, css.replace("position: sticky", "position: static")) == [
        "no Simulation band",
        "the band does not stay on screen",
        "missing: It is not stock AUTO.",
        "does not say it leaves out: shared-mode select",
        "does not say it leaves out: up to 10 seconds later than in Home Assistant",
        "Pyodide is not pinned with its integrity hash",
    ]


def test_the_readme_links_the_simulator_beside_the_hook() -> None:
    readme = (ROOT / "README.md").read_text()
    caption = readme.split("</picture>", 1)[1].split("</sub>", 1)[0]
    assert "[Try it yourself in the simulator](docs/simulator/)" in caption
    assert "](simulator/)" in (ROOT / "docs" / "HOW-IT-WORKS.md").read_text()


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
            w.mxz_remote("studio", fan="low")
        if name == "restart" and until == 240:
            before = json.loads(w.view())["mxz"]
            w.restart()
            after = json.loads(w.view())["mxz"]
            seen.append({"restart": [before["hold_left"], after["hold_left"],
                                     before["rooms"][0]["fan_held"], after["rooms"][0]["fan_held"]]})
        if name == "standoff" and until == 300:
            w.mxz_comfort("den", True)
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


def test_standoff_preset_comfort_priority_gives_den_the_mode(presets: dict[str, list[dict[str, Any]]]) -> None:
    """Comfort priority turns on at 4:30 while Studio still calls cool: Den gets heat at once.
    (Which compute's plan said Comfort priority decided it is pinned by the comfort_priority
    script against the real coordinator.)"""
    before, after = presets["standoff"][8], presets["standoff"][9]
    assert before["mxz"]["mode"] == "cool" and before["mxz"]["standoff"]
    assert after["mxz"]["mode"] == "heat" and after["mxz"]["standoff"]
    assert _room(after, "mxz", "den")["mode"] == "heat" and _room(after, "mxz", "studio")["demand"] == "cool"


def test_mode_wait_preset_holds_until_the_wait_ends(presets: dict[str, list[dict[str, Any]]]) -> None:
    views = presets["mode_wait"]
    assert all(view["mxz"]["mode"] == "heat" for view in views if view["now"] < 540)
    assert next(view for view in views if view["mxz"]["mode"] == "cool")["now"] == 570.0


def test_fan_hold_preset_keeps_the_pick(presets: dict[str, list[dict[str, Any]]]) -> None:
    for view in presets["fan_hold"][3:]:
        studio = _room(view, "mxz", "studio")
        assert studio["fan_held"] and studio["fan"] == "low"


def test_restart_preset_carries_the_wait_and_the_pick(presets: dict[str, list[dict[str, Any]]]) -> None:
    restart = next(entry["restart"] for entry in presets["restart"] if "restart" in entry)
    hold_before, hold_after, held_before, held_after = restart
    assert hold_before == hold_after > 0 and held_before and held_after
