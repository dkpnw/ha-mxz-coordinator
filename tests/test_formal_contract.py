"""The formal docs, the spec, its runner and its workflow stay in step with the code."""

import ast
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tools import code_mutants, formal_tlc

ROOT = Path(__file__).parents[1]
FORMAL = ROOT / "docs/formal"
TLA = FORMAL / "tla"
CODE = ROOT / "custom_components/mxz_coordinator"
MODEHOLD = ROOT / "verification/modehold"
PINNED_ACTION = re.compile(r"[\w./-]+@[0-9a-f]{40}")
LOCKS = sorted((ROOT / "requirements").glob("constraints-*.txt"))
# What a property needs from PyPI, the same on every lane.
PROPERTY_PINS = {"hypothesis": "6.170.15", "sortedcontainers": "2.4.0"}


def spec_names() -> set[str]:
    return set(re.findall(r"^(\w+)(?:\([^)]*\))? ==", (TLA / "MXZCoordinator.tla").read_text(), re.MULTILINE))


def config_rules(name: str) -> list[str]:
    text = (TLA / f"{name}.cfg").read_text()
    return re.search(r"^INVARIANTS (.+)$", text, re.MULTILINE).group(1).split()


def all_rules() -> set[str]:
    """Every rule some config checks, and every open finding's rule. TypeOK is a sanity check."""
    rules = {rule for config in formal_tlc.CONFIGS for rule in config_rules(config)}
    return (rules | set(formal_tlc.FINDINGS)) - {"TypeOK"}


def code_anchors(text: str) -> list[str]:
    """Every name in the Code column of the statechart's keys."""
    anchors, in_key = [], False
    for line in text.splitlines():
        if line.startswith("| Id | What happens | Code |"):
            in_key = True
        elif not line.startswith("|"):
            in_key = False
        elif in_key and not line.startswith("| ---"):
            anchors += re.findall(r"`([\w.]+)`", line.rstrip(" |").rsplit("|", 1)[1])
    return anchors


def _defined(root: Path) -> set[str]:
    """Every function's name under ``root``, and ``Class.method`` for each method."""
    names = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            elif isinstance(node, ast.ClassDef):
                names |= {
                    f"{node.name}.{item.name}" for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
    return names


def missing_anchors(text: str, root: Path = CODE) -> list[str]:
    defined = _defined(root)
    return [name for name in code_anchors(text) if name not in defined]


def _verify() -> object:
    """verification/verify.py, which is a script, loaded from its path."""
    spec = importlib.util.spec_from_file_location("verify", ROOT / "verification/verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow_violations(flow: dict) -> list[str]:
    errors = []
    if flow.get("permissions") != {"contents": "read"}:
        errors.append("token permissions")
    if set(flow.get("jobs", {})) != {"tlc"}:
        errors.append("missing or extra job")
    job = flow.get("jobs", {}).get("tlc", {})
    env = job.get("env", {})
    if env.get("TLA2TOOLS_VERSION") != formal_tlc.TLA2TOOLS_VERSION:
        errors.append("jar version")
    if env.get("TLA2TOOLS_SHA256") != formal_tlc.TLA2TOOLS_SHA256:
        errors.append("jar digest")
    runs = "\n".join(step.get("run", "") for step in job.get("steps", []))
    if "sha256sum --check --strict" not in runs:
        errors.append("jar not checked")
    if "tools/formal_tlc.py all" not in runs:
        errors.append("runner")
    for step in job.get("steps", []):
        action = step.get("uses", "")
        if action and not PINNED_ACTION.fullmatch(action):
            errors.append("unpinned action")
        if action.startswith("actions/checkout@") and step["with"].get("persist-credentials") != "false":
            errors.append("persistent checkout")
        if "continue-on-error" in step or "secrets." in str(step):
            errors.append("step override")
    return errors


@pytest.fixture
def flow() -> dict:
    return yaml.load((ROOT / ".github/workflows/tla.yml").read_text(), Loader=yaml.BaseLoader)


def test_the_workflow_runs_the_pinned_jar_and_nothing_else(flow) -> None:
    assert workflow_violations(flow) == []


@pytest.mark.parametrize(("change", "expected"), [
    (lambda f: f["jobs"]["tlc"]["env"].update(TLA2TOOLS_SHA256="0" * 64), "jar digest"),
    (lambda f: f["jobs"]["tlc"]["env"].update(TLA2TOOLS_VERSION="1.8.0"), "jar version"),
    (lambda f: f["jobs"]["tlc"]["steps"][0].update(uses="actions/checkout@v7"), "unpinned action"),
    (lambda f: f.update(permissions={"contents": "write"}), "token permissions"),
    (lambda f: f["jobs"]["tlc"]["steps"].pop(1), "jar not checked"),
    (lambda f: f["jobs"].update(other={}), "missing or extra job"),
])
def test_a_workflow_that_drifts_is_refused(flow, change, expected) -> None:
    change(flow)
    assert expected in workflow_violations(flow)


def test_no_required_job_depends_on_the_model() -> None:
    """Its own workflow, named nowhere else: an extra check, never a gate on the others."""
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        if path.name != "tla.yml":
            assert "formal_tlc" not in path.read_text()
            assert "docs/formal" not in path.read_text()


@pytest.mark.parametrize(("value", "workers"), [(None, "auto"), ("", "auto"), ("4", "4")])
def test_both_runners_give_tlc_the_workers_asked_for(monkeypatch, tmp_path, value, workers) -> None:
    """TLC_WORKERS, or auto when it is unset or empty, reaches TLC from each runner."""
    if value is None:
        monkeypatch.delenv("TLC_WORKERS", raising=False)
    else:
        monkeypatch.setenv("TLC_WORKERS", value)
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, "", "")

    verify = _verify()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(formal_tlc, "jar", lambda: "tla2tools.jar")
    monkeypatch.setattr(verify, "_tools", lambda: ("java", "tla2tools.jar"))
    assert formal_tlc.tlc(tmp_path, "ci-fan", tmp_path / "formal.out")[0] == "error"
    assert verify.tlc(tmp_path, "ci-restarts", tmp_path / "verify.out")[0] == "error"
    assert [command[command.index("-workers") + 1] for command in commands] == [workers, workers]


def test_every_config_is_run_and_every_rule_exists() -> None:
    assert {path.stem for path in TLA.glob("*.cfg")} == set(formal_tlc.CONFIGS)
    names = spec_names()
    for config in formal_tlc.CONFIGS:
        assert set(config_rules(config)) <= names
    for rule, config in {**formal_tlc.COVERS, **formal_tlc.FINDINGS}.items():
        assert rule in names and config in formal_tlc.CONFIGS
    for _what, _old, _new, config, rule in formal_tlc.MUTANTS.values():
        assert rule in names and config in formal_tlc.CONFIGS


def test_each_spec_mutant_still_finds_its_text() -> None:
    spec = (TLA / "MXZCoordinator.tla").read_text()
    for name, (_what, old, *_rest) in formal_tlc.MUTANTS.items():
        assert spec.count(old) == 1, name


def _test_exists(node: str) -> bool:
    path, function = node.split("::")
    return f"def {function.split('[')[0]}(" in (ROOT / path).read_text()


def test_each_code_mutant_finds_its_text_in_one_function_and_names_a_real_test() -> None:
    for name, mutant in code_mutants.MUTANTS.items():
        assert len(code_mutants.locate(ROOT, mutant)) == 1, name
        assert _test_exists(mutant.test), name


def test_each_mode_hold_mutant_finds_its_text_and_breaks_a_real_rule() -> None:
    """The mode-hold model's broken copies, as the code's: each still applies, and its rule exists."""
    verify = _verify()
    spec = (MODEHOLD / "MXZModeHold.tla").read_text()
    names = set(re.findall(r"^(\w+)(?:\([^)]*\))? ==", spec, re.MULTILINE))
    assert verify.MODEL_MUTANTS
    for mid, _what, old, _new, config, rule in verify.MODEL_MUTANTS:
        assert spec.count(old) == 1, mid
        assert config in verify.CONFIGS and rule in names, mid
    assert verify.CODE_MUTANTS == [name for name in code_mutants.MUTANTS if name.startswith("H")]
    assert verify.CODE_MUTANTS


def _package(tmp_path: Path, files: dict[str, str]) -> Path:
    package = tmp_path / code_mutants.PACKAGE
    package.mkdir(parents=True)
    for name, text in files.items():
        (package / name).write_text(text)
    return tmp_path


def test_a_code_mutant_follows_its_function_to_another_module(tmp_path) -> None:
    root = _package(tmp_path, {"moved.py": "def gate(x):\n    return x >= 1\n"})
    mutant = code_mutants.Mutant("gate open", "gate", "x >= 1", "True", "tests/x.py::test_x")
    assert code_mutants.apply(root, mutant).name == "moved.py"
    assert "return True" in (root / code_mutants.PACKAGE / "moved.py").read_text()


@pytest.mark.parametrize(("files", "places"), [
    ({"a.py": "def gate(x):\n    return x\n"}, 0),  # the text is gone
    ({"a.py": "def other(x):\n    return x >= 1\n"}, 0),  # in another function only
    ({"a.py": "def gate(x):\n    return x >= 1\n", "b.py": "def gate(x):\n    return x >= 1\n"}, 2),
    ({"a.py": "def gate(x):\n    return x >= 1 or x >= 1\n"}, 2),
], ids=["gone", "elsewhere", "two_functions", "twice"])
def test_a_code_mutant_that_is_not_in_one_place_is_refused(tmp_path, files, places) -> None:
    root = _package(tmp_path, files)
    mutant = code_mutants.Mutant("gate open", "gate", "x >= 1", "True", "tests/x.py::test_x")
    assert len(code_mutants.locate(root, mutant)) == places
    with pytest.raises(ValueError, match=f"in {places} places"):
        code_mutants.apply(root, mutant)


def test_every_rule_has_a_broken_copy_that_breaks_it() -> None:
    """A rule with no negative test proves nothing. TypeOK is a sanity check, not a rule."""
    broken = {rule for *_rest, rule in formal_tlc.MUTANTS.values()}
    rules = all_rules() - set(formal_tlc.FINDINGS)
    assert rules <= broken, rules - broken


def test_an_open_finding_is_checked_nowhere_it_breaks() -> None:
    """A finding's rule breaks in its own config, so no config may list it as passing there."""
    for rule, config in formal_tlc.FINDINGS.items():
        assert rule not in config_rules(config), rule


def _property_nodes() -> list[str]:
    """Every property test, as ``path::function`` from the repository root."""
    return [
        f"{path.relative_to(ROOT).as_posix()}::{match}"
        for path in sorted((ROOT / "tests").glob("test_properties_*.py"))
        for match in re.findall(r"^(?:async )?def (test_\w+)", path.read_text(), re.MULTILINE)
    ]


def _property_tests() -> list[str]:
    return [node.split("::")[1] for node in _property_nodes()]


def uncaught_properties(mutants, nodes: list[str]) -> set[str]:
    """The property tests no mutant names, matched on file and function: the
    same function name in another file is another test."""
    caught = {mutant.test.split("[", 1)[0] for mutant in mutants}
    return set(nodes) - caught


def test_every_property_has_a_broken_copy_of_the_code_that_fails_it() -> None:
    nodes = _property_nodes()
    assert nodes
    assert uncaught_properties(code_mutants.MUTANTS.values(), nodes) == set()


def test_a_property_is_not_caught_by_a_namesake_in_another_file() -> None:
    """C24 is the one broken copy for its mode property; H7 names a test of the
    same name in test_modehold_properties.py, which does not stand in for it."""
    c24, h7 = code_mutants.MUTANTS["C24"], code_mutants.MUTANTS["H7"]
    assert c24.test.split("::")[1] == h7.test.split("::")[1]
    assert c24.test.split("::")[0] != h7.test.split("::")[0]
    without = [m for key, m in code_mutants.MUTANTS.items() if key != "C24"]
    assert uncaught_properties(without, _property_nodes()) == {c24.test}


def test_the_readme_names_each_rule_and_each_property() -> None:
    readme = (FORMAL / "README.md").read_text()
    for rule in all_rules():
        assert f"`{rule}`" in readme, rule
    tests = _property_tests()
    assert tests
    for test in tests:
        assert f"`{test}`" in readme, test


def test_the_statechart_points_at_real_code() -> None:
    text = (FORMAL / "STATECHART.md").read_text()
    assert len(code_anchors(text)) > 30
    assert missing_anchors(text) == []


def test_a_statechart_anchor_to_nothing_is_refused() -> None:
    key = "| Id | What happens | Code |\n| --- | --- | --- |\n| X1 | Text with `_compute`. | `_compute`, `_invented_function`, `MXZCoordinator._apply`, `MXZCoordinator._invented` |\n"
    assert code_anchors(key) == ["_compute", "_invented_function", "MXZCoordinator._apply", "MXZCoordinator._invented"]
    assert missing_anchors(key) == ["_invented_function", "MXZCoordinator._invented"]


def test_a_statechart_anchor_follows_its_function_to_another_module(tmp_path) -> None:
    key = "| Id | What happens | Code |\n| --- | --- | --- |\n| X1 | Text. | `gate` |\n"
    root = _package(tmp_path, {"moved.py": "def gate(x):\n    return x\n"})
    assert missing_anchors(key, root / code_mutants.PACKAGE) == []


@pytest.mark.parametrize("lock", LOCKS, ids=lambda path: path.stem)
def test_every_lock_pins_what_the_properties_need(lock) -> None:
    rows = dict(line.split("==") for line in lock.read_text().splitlines())
    assert {name: rows.get(name) for name in PROPERTY_PINS} == PROPERTY_PINS


def test_the_exact_2026_10_bundle_pins_what_the_properties_need() -> None:
    rows = json.loads((ROOT / "requirements/ha2026.10/bundle.json").read_text())["artifacts"]
    assert {r["name"]: r["version"] for r in rows if r["name"] in PROPERTY_PINS} == PROPERTY_PINS
