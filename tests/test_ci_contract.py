"""Static boundaries of .github/workflows/ci.yml; live check results are read separately."""

import re
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
# Every action is pinned to a full commit SHA. Which commit is Dependabot's to move, so
# the SHAs live only in the workflows; this checks their shape and the action names.
PINNED = re.compile(r"(?P<name>[\w.-]+/[\w./-]+)@[0-9a-f]{40}")
ACTIONS = {"actions/checkout", "actions/setup-python", "home-assistant/actions/hassfest", "hacs/action"}
JOBS = {"hassfest", "hacs", "ruff", "yamllint", "pytest"}
ACTIVATE = 'source "$RUNNER_TEMP/mxz-venv/bin/activate"'
INSTALL_RUN = 'tools/ci-install.sh "${{ matrix.constraints }}" "${{ matrix.python-version }}"'
SUITE_RUN = "python -m pytest -p tools.pytest_phases tests/ -q -n auto --dist loadfile"
FRESH_BOOT_RUN = ("FAKE_UPTIME=120 python -m pytest -p tools.fakeboot -p tools.pytest_phases tests/ -q"
                  " -n auto --dist loadfile")
# The fresh-boot pass runs after a failed suite too; nothing else in a lane is conditional.
FRESH_BOOT_IF = "${{ !cancelled() }}"
LANE_STEPS = ["Install the lane's lock", "Full suite", "Full suite on a fresh-boot clock"]
MATRIX_KEYS = {"lane", "python-version", "constraints"}


def load(path):
    # BaseLoader keeps every scalar a string, and the "on" key as written.
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def trigger_violations(workflow, prefix):
    """Push to main, every pull request, by hand; no path filter can skip a required check.

    A newer push to a pull request cancels its older run. Every other run has its own
    group, because GitHub cancels a pending run that a newer one queues behind.
    """
    errors = []
    on = workflow.get("on", {})
    if set(on) != {"push", "pull_request", "workflow_dispatch"}:
        errors.append("triggers")
    if on.get("push") != {"branches": ["main"]}:
        errors.append("push filter")
    if on.get("pull_request") != "" or on.get("workflow_dispatch") != "":
        errors.append("pull request or manual filter")
    if workflow.get("concurrency") != {
        "group": prefix + "-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }:
        errors.append("concurrency")
    return errors


def run_lines(step):
    return [line.strip() for line in step.get("run", "").strip().splitlines()]


def lane_job_violations(job):
    """Each lane installs its lock, runs the whole suite, then the whole suite on a
    clock 120 s after boot, in that order, in one job."""
    errors = []
    steps = job.get("steps", [])
    names = [step.get("name") for step in steps]
    if names[-3:] != LANE_STEPS or any(names.count(name) != 1 for name in LANE_STEPS):
        errors.append("lane steps")
        return errors
    install, suite, fresh = steps[-3:]
    if run_lines(install) != [INSTALL_RUN]:
        errors.append("lane install")
    if run_lines(suite) != [ACTIVATE, SUITE_RUN]:
        errors.append("suite run")
    if run_lines(fresh) != [ACTIVATE, FRESH_BOOT_RUN] or fresh.get("if") != FRESH_BOOT_IF:
        errors.append("fresh-boot run")
    if any("if" in step for step in steps if step is not fresh):
        errors.append("step condition")
    if job.get("strategy", {}).get("fail-fast") != "false":
        errors.append("fail-fast")
    return errors


def lane_lock_errors(entry, raw):
    """A lane's name, Python, lock path and locked HA must all agree."""
    python, ha = re.fullmatch(r"py3\.(\d+)-ha(.+)", entry["lane"]).groups()
    errors = []
    if entry["constraints"] != f"requirements/constraints-py3{python}-ha{ha}.txt":
        errors.append("lock path")
    if not (entry["python-version"] + ".").startswith(f"3.{python}."):
        errors.append("lane python")
    if f"homeassistant=={ha}" not in raw.decode().splitlines():
        errors.append("locked HA")
    return errors


def lane_violations(include, root=ROOT):
    """Every lock in requirements/ is exactly one lane, and every lane names its lock."""
    errors = []
    if not include or any(set(entry) != MATRIX_KEYS for entry in include):
        return ["lane keys"]
    lanes = [entry["lane"] for entry in include]
    if len(set(lanes)) != len(lanes) or any(not re.fullmatch(r"py3\.\d+-ha[\d.]+", lane) for lane in lanes):
        errors.append("lane names")
        return errors
    for entry in include:
        lock = root / entry["constraints"]
        errors += lane_lock_errors(entry, lock.read_bytes() if lock.is_file() else b"")
    locks = sorted(path.relative_to(root).as_posix() for path in (root / "requirements").glob("constraints-*.txt"))
    if sorted(entry["constraints"] for entry in include) != locks:
        errors.append("lock without a lane, or lane without a lock")
    return errors


def violations(workflow):
    errors = trigger_violations(workflow, "ci")
    if workflow.get("permissions") != {"contents": "read"}:
        errors.append("token permissions")
    jobs = workflow.get("jobs", {})
    if set(jobs) != JOBS:
        errors.append("missing or extra job")
    for job in jobs.values():
        if job.get("runs-on") != "ubuntu-24.04" or "environment" in job:
            errors.append("runner/environment boundary")
        # A job-level if would let a required check report "skipped", which passes.
        if {"permissions", "secrets", "if", "needs", "continue-on-error"} & set(job):
            errors.append("job override")
        if not re.fullmatch(r"[1-9][0-9]?", job.get("timeout-minutes", "")):
            errors.append("job time limit")
        for step in job.get("steps", []):
            if "continue-on-error" in step:
                errors.append("step override")
            action = step.get("uses")
            if action:
                pinned = PINNED.fullmatch(action)
                if not pinned:
                    errors.append("unpinned action")
                elif pinned["name"] not in ACTIONS:
                    errors.append("unknown action/cache/upload")
            name = (action or "").partition("@")[0]
            if name == "actions/checkout" and step.get("with", {}).get("persist-credentials") != "false":
                errors.append("persistent checkout")
            if name == "actions/setup-python" and "cache" in step.get("with", {}):
                errors.append("dependency cache")
            if "secrets." in str(step):
                errors.append("secret input")
    pytest_job = jobs.get("pytest", {})
    errors += lane_job_violations(pytest_job)
    errors += lane_violations(pytest_job.get("strategy", {}).get("matrix", {}).get("include", []))
    return errors


@pytest.fixture(scope="module")
def parsed_workflow():
    return load(ROOT / ".github/workflows/ci.yml")


@pytest.fixture
def workflow(parsed_workflow):
    # Parse once per module; each test still gets its own copy to change.
    return deepcopy(parsed_workflow)


def test_declared_ci_boundary(workflow):
    assert violations(workflow) == []


def _job(w, name):
    return w["jobs"][name]


def _lanes(w):
    return w["jobs"]["pytest"]["strategy"]["matrix"]["include"]


def _step(w, name):
    return next(s for s in w["jobs"]["pytest"]["steps"] if s.get("name") == name)


def _replace_run(w, name, old, new):
    step = _step(w, name)
    assert old in step["run"]
    step["run"] = step["run"].replace(old, new)


FAULTS = {
    # Triggers and cancelling.
    "push on every branch": (lambda w: w["on"]["push"].update(branches=["**"]), "push filter"),
    "push path filter": (lambda w: w["on"]["push"].update(paths=["custom_components/**"]), "push filter"),
    "pull request path filter": (lambda w: w["on"].update(pull_request={"paths-ignore": ["docs/**"]}),
                                 "pull request or manual filter"),
    "no manual run": (lambda w: w["on"].pop("workflow_dispatch"), "triggers"),
    "main runs share a group": (lambda w: w["concurrency"].update(group="ci-${{ github.ref }}"), "concurrency"),
    "main runs cancelled": (lambda w: w["concurrency"].update({"cancel-in-progress": "true"}), "concurrency"),
    "no concurrency": (lambda w: w.pop("concurrency"), "concurrency"),
    # Token, runner and actions.
    "token": (lambda w: w.update(permissions={"contents": "write"}), "token permissions"),
    "runner": (lambda w: _job(w, "ruff").update({"runs-on": "self-hosted"}), "runner/environment boundary"),
    "environment": (lambda w: _job(w, "hacs").update(environment="release"), "runner/environment boundary"),
    "job permissions": (lambda w: _job(w, "hassfest").update(permissions={"actions": "read"}), "job override"),
    "job skipped": (lambda w: _job(w, "pytest").update({"if": "${{ github.event_name == 'push' }}"}),
                    "job override"),
    "job needs": (lambda w: _job(w, "pytest").update(needs="ruff"), "job override"),
    "job allowed to fail": (lambda w: _job(w, "pytest").update({"continue-on-error": "true"}), "job override"),
    "step allowed to fail": (lambda w: _step(w, "Full suite").update({"continue-on-error": "true"}),
                             "step override"),
    "no time limit": (lambda w: _job(w, "pytest").pop("timeout-minutes"), "job time limit"),
    "unpinned": (lambda w: _job(w, "ruff")["steps"][0].update(uses="actions/checkout@v7"), "unpinned action"),
    "short sha": (lambda w: _job(w, "ruff")["steps"][0].update(uses="actions/checkout@3d3c42e"), "unpinned action"),
    "upload": (lambda w: _job(w, "pytest")["steps"].append(
        {"uses": "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9"}),
        "unknown action/cache/upload"),
    "cache": (lambda w: _job(w, "pytest")["steps"][1]["with"].update(cache="pip"), "dependency cache"),
    "checkout": (lambda w: _job(w, "hacs")["steps"][0]["with"].update({"persist-credentials": "true"}),
                 "persistent checkout"),
    "secret": (lambda w: _job(w, "hacs")["steps"][1]["with"].update(token="${{ secrets.TOKEN }}"),
               "secret input"),
    "missing job": (lambda w: w["jobs"].pop("yamllint"), "missing or extra job"),
    # The lane job's own steps.
    "suite on a subset": (lambda w: _replace_run(w, "Full suite", "tests/ ", "tests/test_climate.py "), "suite run"),
    "suite without phases": (lambda w: _replace_run(w, "Full suite", "-p tools.pytest_phases ", ""), "suite run"),
    "suite deselects": (lambda w: _replace_run(w, "Full suite", " -q", " -q -k 'not slow'"), "suite run"),
    "suite on one worker": (lambda w: _replace_run(w, "Full suite", " -n auto", " -n 0"), "suite run"),
    "suite splits a file": (lambda w: _replace_run(w, "Full suite", " --dist loadfile", ""), "suite run"),
    "fresh-boot splits a file": (lambda w: _replace_run(w, "Full suite on a fresh-boot clock", " --dist loadfile",
                                                        " --dist load"), "fresh-boot run"),
    "suite conditional": (lambda w: _step(w, "Full suite").update({"if": "${{ success() }}"}), "step condition"),
    "fresh-boot missing": (lambda w: _job(w, "pytest")["steps"].remove(_step(w, "Full suite on a fresh-boot clock")),
                           "lane steps"),
    "fresh-boot skipped after a failure": (lambda w: _step(w, "Full suite on a fresh-boot clock").pop("if"),
                                           "fresh-boot run"),
    "fresh-boot on one lane": (lambda w: _step(w, "Full suite on a fresh-boot clock").update(
        {"if": "${{ !cancelled() && matrix.lane == 'py3.14-ha2026.9.4' }}"}), "fresh-boot run"),
    "fresh-boot long uptime": (lambda w: _replace_run(w, "Full suite on a fresh-boot clock", "FAKE_UPTIME=120",
                                                      "FAKE_UPTIME=86400"), "fresh-boot run"),
    "fresh-boot without its clock": (lambda w: _replace_run(w, "Full suite on a fresh-boot clock",
                                                            "-p tools.fakeboot ", ""), "fresh-boot run"),
    "fresh-boot on a subset": (lambda w: _replace_run(w, "Full suite on a fresh-boot clock", "tests/ ",
                                                      "tests/test_dwell_wakeup.py "), "fresh-boot run"),
    "fresh-boot before the suite": (lambda w: _job(w, "pytest")["steps"].reverse(), "lane steps"),
    "fresh-boot twice": (lambda w: _job(w, "pytest")["steps"].append(
        deepcopy(_step(w, "Full suite on a fresh-boot clock"))), "lane steps"),
    "install another lock": (lambda w: _replace_run(w, "Install the lane's lock", "${{ matrix.constraints }}",
                                                    "requirements/constraints-py314-ha2026.9.4.txt"), "lane install"),
    "fail-fast": (lambda w: _job(w, "pytest")["strategy"].update({"fail-fast": "true"}), "fail-fast"),
    # The lanes.
    "lane dropped": (lambda w: _lanes(w).pop(), "lock without a lane, or lane without a lock"),
    "lane duplicated": (lambda w: _lanes(w).append(deepcopy(_lanes(w)[0])), "lane names"),
    "lane extra key": (lambda w: _lanes(w)[0].update(sha256="0" * 64), "lane keys"),
    "lane renamed": (lambda w: _lanes(w)[0].update(lane="py3.12-ha2024.12.0"), "lock path"),
    "lane on another python": (lambda w: _lanes(w)[0].update({"python-version": "3.13"}), "lane python"),
    "lane on a near python": (lambda w: _lanes(w)[2].update({"python-version": "3.140"}), "lane python"),
    "lane on another lock": (lambda w: _lanes(w)[0].update(constraints=_lanes(w)[1]["constraints"]), "lock path"),
}


@pytest.mark.parametrize("fault", FAULTS)
def test_weakened_ci_boundary_is_refused(workflow, fault):
    change, expected = FAULTS[fault]
    change(workflow)
    assert expected in violations(workflow)


@pytest.mark.parametrize(("change", "expected"), [
    ("lane", ["lock path", "locked HA"]),
    ("path", ["lock path"]),
    ("bytes", ["locked HA"]),
])
def test_a_lane_that_misnames_its_lock_is_refused(workflow, change, expected):
    entry = _lanes(workflow)[0]
    raw = (ROOT / entry["constraints"]).read_bytes()
    if change == "lane":
        entry["lane"] = "py3.12-ha2024.12.0"
    elif change == "path":
        entry["constraints"] = "requirements/constraints-py312-ha2024.12.0.txt"
    else:
        raw = raw.replace(b"homeassistant==", b"homeassistant-other==")
    assert lane_lock_errors(entry, raw) == expected


def _lock_tree(tmp_path, *names):
    for name in names:
        path = tmp_path / "requirements" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        ha = re.search(r"-ha(.+)\.txt$", name).group(1)
        path.write_text(f"homeassistant=={ha}\n")
    return tmp_path


def test_a_lock_no_lane_runs_is_refused(tmp_path):
    lanes = [{"lane": "py3.14-ha2026.9.4", "python-version": "3.14",
              "constraints": "requirements/constraints-py314-ha2026.9.4.txt"}]
    root = _lock_tree(tmp_path, "constraints-py314-ha2026.9.4.txt")
    assert lane_violations(lanes, root) == []
    _lock_tree(tmp_path, "constraints-py314-ha2026.11.2.txt")
    assert lane_violations(lanes, root) == ["lock without a lane, or lane without a lock"]


def test_a_lane_whose_lock_is_missing_is_refused(tmp_path):
    lanes = [{"lane": "py3.14-ha2026.11.2", "python-version": "3.14",
              "constraints": "requirements/constraints-py314-ha2026.11.2.txt"}]
    root = _lock_tree(tmp_path, "constraints-py314-ha2026.9.4.txt")
    assert lane_violations(lanes, root) == ["locked HA", "lock without a lane, or lane without a lock"]


INSTALLER = ROOT / "tools/ci-install.sh"
# The one install transaction, and the check that only the lock's distributions came in.
INSTALL_LINES = [
    'python -m pip install --index-url https://pypi.org/simple -c "$lock" -r "$lock" -r requirements_test.txt',
    "python -m pip check",
    'python tools/check_lock.py "$lock" "$python_lane"',
]


def installer_violations(text):
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    errors = []
    if lines[:1] != ["set -euo pipefail"]:
        errors.append("installer does not stop on errors")
    if [line for line in lines if line.startswith("python -m pip install")] != INSTALL_LINES[:1]:
        errors.append("installer installs something else")
    if lines[-2:] != INSTALL_LINES[1:]:
        errors.append("installer does not check the inventory")
    return errors


def test_the_installer_installs_exactly_the_lock():
    assert installer_violations(INSTALLER.read_text()) == []


@pytest.mark.parametrize(("old", "new", "expected"), [
    ("set -euo pipefail", "set -uo pipefail", "installer does not stop on errors"),
    ('-c "$lock" -r "$lock" ', '-r "$lock" ', "installer installs something else"),
    ("-r requirements_test.txt", "-r requirements_test.txt pytest-invented", "installer installs something else"),
    ('python tools/check_lock.py "$lock" "$python_lane"', "true", "installer does not check the inventory"),
    ("python -m pip check\n", "", "installer does not check the inventory"),
])
def test_a_changed_installer_is_refused(old, new, expected):
    text = INSTALLER.read_text()
    assert text.count(old) == 1
    assert expected in installer_violations(text.replace(old, new))
