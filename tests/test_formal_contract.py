"""The formal docs, the spec, its runner and its workflow stay in step with the code."""

import json
import re
from pathlib import Path

import pytest
import yaml

from tools import formal_tlc

ROOT = Path(__file__).parents[1]
FORMAL = ROOT / "docs/formal"
TLA = FORMAL / "tla"
CODE = ROOT / "custom_components/mxz_coordinator"
PINNED_ACTION = re.compile(r"[\w./-]+@[0-9a-f]{40}")
LOCKS = sorted((ROOT / "requirements").glob("constraints-*.txt"))
# What a property needs from PyPI, the same on every lane.
PROPERTY_PINS = {"hypothesis": "6.170.15", "sortedcontainers": "2.4.0"}


def spec_names() -> set[str]:
    return set(re.findall(r"^(\w+)(?:\([^)]*\))? ==", (TLA / "MXZCoordinator.tla").read_text(), re.MULTILINE))


def config_rules(name: str) -> list[str]:
    text = (TLA / f"{name}.cfg").read_text()
    return re.search(r"^INVARIANTS (.+)$", text, re.MULTILINE).group(1).split()


def code_anchors(text: str) -> list[tuple[str, str]]:
    return re.findall(r"`(\w+\.py)` `(\w+)`", text)


def missing_anchors(text: str) -> list[tuple[str, str]]:
    return [
        (name, symbol) for name, symbol in code_anchors(text)
        if not re.search(rf"\bdef {symbol}\b", (CODE / name).read_text())
    ]


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


def test_every_config_is_run_and_every_rule_exists() -> None:
    assert {path.stem for path in TLA.glob("*.cfg")} == set(formal_tlc.CONFIGS)
    names = spec_names()
    for config in formal_tlc.CONFIGS:
        assert set(config_rules(config)) <= names
    for rule, config in {**formal_tlc.COVERS, **formal_tlc.FINDINGS}.items():
        assert rule in names and config in formal_tlc.CONFIGS
    for _what, _old, _new, config, rule in formal_tlc.MUTANTS.values():
        assert rule in names and config in formal_tlc.CONFIGS


def test_each_spec_mutant_and_code_mutant_still_finds_its_text() -> None:
    spec = (TLA / "MXZCoordinator.tla").read_text()
    for name, (_what, old, *_rest) in formal_tlc.MUTANTS.items():
        assert spec.count(old) == 1, name
    for name, (_what, filename, old, _new, test) in formal_tlc.CODE_MUTANTS.items():
        assert (CODE / filename).read_text().count(old) == 1, name
        path, function = test.split("::")
        assert f"def {function}(" in (ROOT / path).read_text(), name


def test_every_rule_has_a_broken_copy_that_breaks_it() -> None:
    """A rule with no negative test proves nothing. TypeOK is a sanity check, not a rule."""
    broken = {rule for *_rest, rule in formal_tlc.MUTANTS.values()}
    rules = set(config_rules("ci-fan")) - {"TypeOK"}
    assert rules <= broken, rules - broken


def _property_tests() -> list[str]:
    return [
        match for path in sorted((ROOT / "tests").glob("test_properties_*.py"))
        for match in re.findall(r"^(?:async )?def (test_\w+)", path.read_text(), re.MULTILINE)
    ]


def test_every_property_has_a_broken_copy_of_the_code_that_fails_it() -> None:
    caught = {test.split("::")[1] for *_rest, test in formal_tlc.CODE_MUTANTS.values()}
    assert set(_property_tests()) <= caught, set(_property_tests()) - caught


def test_the_readme_names_each_rule_and_each_property() -> None:
    readme = (FORMAL / "README.md").read_text()
    for rule in set(config_rules("ci-fan")) - {"TypeOK"}:
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
    assert missing_anchors("`coordinator.py` `_invented_function`") == [("coordinator.py", "_invented_function")]


@pytest.mark.parametrize("lock", LOCKS, ids=lambda path: path.stem)
def test_every_lock_pins_what_the_properties_need(lock) -> None:
    rows = dict(line.split("==") for line in lock.read_text().splitlines())
    assert {name: rows.get(name) for name in PROPERTY_PINS} == PROPERTY_PINS


def test_the_exact_2026_10_bundle_pins_what_the_properties_need() -> None:
    rows = json.loads((ROOT / "requirements/ha2026.10/bundle.json").read_text())["artifacts"]
    assert {r["name"]: r["version"] for r in rows if r["name"] in PROPERTY_PINS} == PROPERTY_PINS
