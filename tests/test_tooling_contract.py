"""Static boundaries of the shared tooling: pinned tools, the coverage floor, strict mypy."""

import re
import tomllib
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

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
    "lock differs from the lane": lambda q: _replace_run(q, "coverage", "Exact locked installation", "c170656", "0170656"),
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
    (lambda s, k, t: (s, k.replace("ruff==0.17.4", "ruff==0.16.0"), t), "lock differs from its source"),
    (lambda s, k, t: (s, k, t.replace("ruff==0.17.4", "ruff==0.16.0")), "two ruff versions"),
    (lambda s, k, t: (s, re.sub(r"(ruff==\S+)(?: \\\n\s+--hash=\S+)+", r"\1", k), t), "requirement without a hash"),
])
def test_a_drifted_tool_lock_is_refused(change, expected):
    assert expected in lock_violations(*change(*lock_texts()))


@pytest.mark.parametrize("line", [
    "- run: pip install ruff==0.17.4",
    "- run: pip install -r requirements_ci.txt",
    "- run: python -m pip install --require-hashes -r requirements_test.txt",
])
def test_an_unhashed_install_is_refused(line):
    assert unhashed_installs([line]) == [line]


def floor_violations(test_requirements, lock):
    """Each requirements_test.txt line must admit the lane lock's exact pin.

    CI installs with -c lock -r lock -r requirements_test.txt, so a floor above the pin
    is ResolutionImpossible (Dependabot PR #108: pytest>=9.2.0 against pytest==8.3.3).
    """
    locked = {canonicalize_name(name): Version(version) for name, version in pins(lock).items()}
    errors = []
    for line in test_requirements.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        version = locked.get(canonicalize_name(requirement.name))
        if version is None:
            errors.append(f"{requirement.name} not in the lock")
        elif not requirement.specifier.contains(version, prereleases=True):
            errors.append(f"{requirement.name}{requirement.specifier} refuses {version}")
    return errors


def lane_locks():
    include = load(WORKFLOWS / "ci.yml")["jobs"]["pytest"]["strategy"]["matrix"]["include"]
    return sorted({entry["constraints"] for entry in include})


def test_test_requirements_install_under_every_lane_lock():
    locks = lane_locks()
    assert len(locks) == 3
    test_requirements = (ROOT / "requirements_test.txt").read_text()
    for lock in locks:
        assert floor_violations(test_requirements, (ROOT / lock).read_text()) == [], lock


FLOOR_LOCK = "requirements/constraints-py312-ha2024.12.5.txt"


@pytest.mark.parametrize(("requirements", "expected"), [
    # The floors Dependabot PR #108 proposed.
    ("pytest>=9.2.0\npytest-asyncio>=1.4.1.post1\npytest-homeassistant-custom-component>=0.13.375\n",
     ["pytest>=9.2.0 refuses 8.3.3", "pytest-asyncio>=1.4.1.post1 refuses 0.24.0",
      "pytest-homeassistant-custom-component>=0.13.375 refuses 0.13.195"]),
    ("ruff==0.16.0\n", ["ruff==0.16.0 refuses 0.17.4"]),
    ("hypothesis>=6.170.16  # one above the lock\n", ["hypothesis>=6.170.16 refuses 6.170.15"]),
    ("invented-plugin>=1\n", ["invented-plugin not in the lock"]),
    # Names compare normalized, and a floor equal to the pin is admitted.
    ("Pytest_Asyncio>=0.24.0\npytest>=8.3.3\n", []),
])
def test_a_floor_the_lock_cannot_meet_is_refused(requirements, expected):
    assert floor_violations(requirements, (ROOT / FLOOR_LOCK).read_text()) == expected


RUFF_HOOK = "https://github.com/astral-sh/ruff-pre-commit"


def hook_violations(pre_commit, ruff_pin):
    """The ruff pre-commit hook is frozen to a commit and runs the ruff CI runs.

    With 0.16.0 in the hook and 0.17.4 in CI, a commit could pass the hook and fail CI.
    Comments are not YAML data, so the frozen tag is read from the text; that the SHA
    is that tag's commit is checked against GitHub when the hook is bumped.
    """
    repos = [r for r in yaml.safe_load(pre_commit).get("repos", []) if r.get("repo") == RUFF_HOOK]
    if len(repos) != 1:
        return ["no single ruff hook"]
    errors = []
    rev = str(repos[0].get("rev", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", rev):
        errors.append("ruff hook not frozen to a commit")
    frozen = re.search(rf"^\s*rev:\s*{re.escape(rev)}\s+# frozen: v(\S+)$", pre_commit, re.MULTILINE)
    if not frozen or frozen.group(1) != ruff_pin:
        errors.append("ruff hook differs from CI")
    if "ruff-check" not in [hook.get("id") for hook in repos[0].get("hooks", [])]:
        errors.append("ruff hook does not check")
    return errors


def pre_commit_text():
    return (ROOT / ".pre-commit-config.yaml").read_text()


def test_the_ruff_hook_runs_the_ci_ruff():
    ruff = pins((ROOT / "requirements_ci.in").read_text())["ruff"]
    assert ruff == "0.17.4"
    assert hook_violations(pre_commit_text(), ruff) == []


FROZEN = "c0f278f1254ccc8fb09676bcdb8d847e7db493e5  # frozen: v0.17.4"


@pytest.mark.parametrize(("old", "new", "expected"), [
    # The pin this PR replaced.
    (FROZEN, "cb8c523fd4835aba42af70f4cad5568db4df0b6c  # frozen: v0.16.0", ["ruff hook differs from CI"]),
    (FROZEN, "v0.17.4", ["ruff hook not frozen to a commit", "ruff hook differs from CI"]),
    (FROZEN, "c0f278f1254ccc8fb09676bcdb8d847e7db493e5", ["ruff hook differs from CI"]),
    (FROZEN, "c0f278f1254ccc8fb09676bcdb8d847e7db493e5  # frozen: v0.17.40", ["ruff hook differs from CI"]),
    ("id: ruff-check", "id: ruff-format", ["ruff hook does not check"]),
    (RUFF_HOOK, "https://github.com/invented/ruff-pre-commit", ["no single ruff hook"]),
])
def test_a_drifted_ruff_hook_is_refused(old, new, expected):
    text = pre_commit_text()
    assert text.count(old) == 1
    assert hook_violations(text.replace(old, new), "0.17.4") == expected
