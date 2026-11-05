"""Static boundaries of the shared tooling: pinned tools, the coverage floor, strict mypy."""

import re
from copy import deepcopy
from pathlib import Path

import pytest
import tomllib
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
LANE = "py3.14-ha2026.9.4"
# Raising the floor needs only pyproject.toml; lowering it has to come through here too.
COVERAGE_FLOOR = 96
COVERAGE_RUN = "python -m pytest -p tools.pytest_phases tests/ -q --cov --cov-report=term"
MYPY_RUN = '"$RUNNER_TEMP/mypy-venv/bin/mypy" --python-executable "$RUNNER_TEMP/mxz-venv/bin/python"'
HASH_INSTALL = re.compile(r"pip install --require-hashes -r requirements_ci\.txt$")
PINNED_ACTION = re.compile(r"[\w./-]+@[0-9a-f]{40}")


def load(path):
    # BaseLoader keeps every scalar a string, as in test_ci_contract.py.
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def named_step(job, name):
    return next((s for s in job.get("steps", []) if s.get("name") == name), {})


def workflow_violations(quality, lane):
    """What this workflow must keep; lane is the ci.yml matrix entry it shares a lock with."""
    errors = []
    if quality.get("permissions") != {"contents": "read"}:
        errors.append("token permissions")
    jobs = quality.get("jobs", {})
    if set(jobs) != {"coverage", "mypy"}:
        errors.append("missing or extra job")
    install = f"tools/ci-install.sh {lane['constraints']} {lane['sha256']} {lane['python-version']} "
    for job in jobs.values():
        if {"permissions", "secrets", "environment", "continue-on-error", "if"} & set(job):
            errors.append("job override")
        for step in job.get("steps", []):
            if "continue-on-error" in step or "if" in step or "secrets." in str(step):
                errors.append("step override")
            action = step.get("uses", "")
            if action and not PINNED_ACTION.fullmatch(action):
                errors.append("unpinned action")
            if action.startswith("actions/checkout@") and step["with"].get("persist-credentials") != "false":
                errors.append("persistent checkout")
            if action.startswith("actions/setup-python@") and (
                step["with"] != {"python-version": lane["python-version"]}
            ):
                errors.append("python differs from the lane")
        if install not in named_step(job, "Exact locked installation").get("run", ""):
            errors.append("lock differs from the lane")
    if COVERAGE_RUN not in named_step(jobs.get("coverage", {}), "Full suite with the coverage floor").get(
        "run", ""
    ).splitlines():
        errors.append("coverage run")
    mypy = named_step(jobs.get("mypy", {}), "Strict type check against the lane's Home Assistant")
    if mypy.get("run", "").strip() != MYPY_RUN:
        errors.append("mypy run")
    return errors


def config_violations(config):
    errors = []
    coverage = config.get("tool", {}).get("coverage", {})
    if coverage.get("run", {}).get("source") != ["custom_components/mxz_coordinator"]:
        errors.append("coverage source")
    floor = coverage.get("report", {}).get("fail_under")
    if not isinstance(floor, int) or floor < COVERAGE_FLOOR:
        errors.append("coverage floor")
    mypy = config.get("tool", {}).get("mypy", {})
    if mypy.get("strict") is not True or mypy.get("files") != ["custom_components/mxz_coordinator"]:
        errors.append("mypy strictness")
    # Only Home Assistant's own modules may be relaxed, and only for its re-exports.
    if mypy.get("overrides") != [{"module": "homeassistant.*", "implicit_reexport": True}] or (
        set(mypy) - {"strict", "files", "explicit_package_bases", "overrides"}
    ):
        errors.append("mypy relaxed")
    return errors


def pins(text):
    return dict(re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", text, re.MULTILINE))


def lock_violations(source, lock, test_requirements):
    errors = []
    wanted = pins(source)
    locked = pins(lock)
    if not wanted or any(locked.get(name) != version for name, version in wanted.items()):
        errors.append("lock differs from its source")
    if pins(test_requirements).get("ruff") != wanted.get("ruff"):
        errors.append("two ruff versions")
    # Each requirement is a pin followed by its own hash lines.
    lines = lock.splitlines()
    if any(
        re.match(r"[A-Za-z0-9_.-]+==", line) and not following.lstrip().startswith("--hash=sha256:")
        for line, following in zip(lines, [*lines[1:], ""], strict=True)
    ):
        errors.append("requirement without a hash")
    return errors


def unhashed_installs(texts):
    return [
        line.strip()
        for text in texts
        for line in text.splitlines()
        if "pip install" in line and not HASH_INSTALL.search(line.strip())
    ]


@pytest.fixture
def quality():
    return load(WORKFLOWS / "quality.yml")


@pytest.fixture
def lane():
    include = load(WORKFLOWS / "ci.yml")["jobs"]["pytest"]["strategy"]["matrix"]["include"]
    return next(entry for entry in include if entry["lane"] == LANE)


@pytest.fixture
def config():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def lock_texts():
    return [(ROOT / name).read_text() for name in ("requirements_ci.in", "requirements_ci.txt", "requirements_test.txt")]


def test_declared_tooling_boundary(quality, lane, config):
    assert workflow_violations(quality, lane) == []
    assert config_violations(config) == []
    assert lock_violations(*lock_texts()) == []


def test_every_workflow_install_is_hash_locked():
    assert unhashed_installs(path.read_text() for path in WORKFLOWS.glob("*.yml")) == []


def _job(quality, name):
    return quality["jobs"][name]


def _step(quality, job, name):
    return named_step(quality["jobs"][job], name)


def _replace_run(quality, job, name, old, new):
    step = _step(quality, job, name)
    assert old in step["run"]
    step["run"] = step["run"].replace(old, new)


COVERAGE_STEP = "Full suite with the coverage floor"
MYPY_STEP = "Strict type check against the lane's Home Assistant"
WORKFLOW_FAULTS = {
    "token permissions": lambda q: q.update(permissions={"contents": "write"}),
    "missing or extra job": lambda q: q["jobs"].pop("mypy"),
    "job override": lambda q: _job(q, "coverage").update({"continue-on-error": "true"}),
    "step override": lambda q: _step(q, "coverage", COVERAGE_STEP).update({"continue-on-error": "true"}),
    "unpinned action": lambda q: _job(q, "mypy")["steps"][0].update(uses="actions/checkout@v7"),
    "persistent checkout": lambda q: _job(q, "mypy")["steps"][0]["with"].update({"persist-credentials": "true"}),
    "python differs from the lane": lambda q: _job(q, "mypy")["steps"][1]["with"].update({"python-version": "3.13"}),
    "lock differs from the lane": lambda q: _replace_run(q, "coverage", "Exact locked installation", "cf85bc9", "0f85bc9"),
    "coverage run": lambda q: _replace_run(q, "coverage", COVERAGE_STEP, " tests/ ", " tests/test_climate.py "),
    "mypy run": lambda q: _replace_run(q, "mypy", MYPY_STEP, "--python-executable", "--ignore-errors --python-executable"),
}


@pytest.mark.parametrize("expected", WORKFLOW_FAULTS)
def test_a_changed_workflow_is_refused(quality, lane, expected):
    bad = deepcopy(quality)
    WORKFLOW_FAULTS[expected](bad)
    assert expected in workflow_violations(bad, lane)


def test_the_coverage_flag_cannot_override_the_floor(quality, lane):
    _replace_run(quality, "coverage", COVERAGE_STEP, "--cov ", "--cov --cov-fail-under=1 ")
    assert "coverage run" in workflow_violations(quality, lane)


@pytest.mark.parametrize(("change", "expected"), [
    (lambda c: c["tool"]["coverage"]["report"].update(fail_under=COVERAGE_FLOOR - 1), "coverage floor"),
    (lambda c: c["tool"]["coverage"]["report"].pop("fail_under"), "coverage floor"),
    (lambda c: c["tool"]["coverage"]["run"].update(source=["tests"]), "coverage source"),
    (lambda c: c["tool"]["mypy"].update(strict=False), "mypy strictness"),
    (lambda c: c["tool"]["mypy"].update(files=["custom_components/mxz_coordinator/const.py"]), "mypy strictness"),
    (lambda c: c["tool"]["mypy"].update(ignore_errors=True), "mypy relaxed"),
    (lambda c: c["tool"]["mypy"]["overrides"].append(
        {"module": "custom_components.*", "ignore_errors": True}), "mypy relaxed"),
])
def test_a_relaxed_config_is_refused(config, change, expected):
    change(config)
    assert expected in config_violations(config)


def test_a_raised_floor_is_accepted(config):
    config["tool"]["coverage"]["report"]["fail_under"] = COVERAGE_FLOOR + 1
    assert config_violations(config) == []


@pytest.mark.parametrize(("change", "expected"), [
    (lambda s, k, t: (s.replace("mypy==2.4.1", "mypy==2.4.0"), k, t), "lock differs from its source"),
    (lambda s, k, t: (s, k.replace("ruff==0.16.0", "ruff==0.15.0"), t), "lock differs from its source"),
    (lambda s, k, t: (s, k, t.replace("ruff==0.16.0", "ruff==0.15.0")), "two ruff versions"),
    (lambda s, k, t: (s, re.sub(r"(ruff==\S+)(?: \\\n\s+--hash=\S+)+", r"\1", k), t), "requirement without a hash"),
])
def test_a_drifted_tool_lock_is_refused(change, expected):
    assert expected in lock_violations(*change(*lock_texts()))


@pytest.mark.parametrize("line", [
    "- run: pip install ruff==0.16.0",
    "- run: pip install -r requirements_ci.txt",
    "- run: python -m pip install --require-hashes -r requirements_test.txt",
])
def test_an_unhashed_install_is_refused(line):
    assert unhashed_installs([line]) == [line]
