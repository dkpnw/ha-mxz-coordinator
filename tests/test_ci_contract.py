"""Static CI boundaries; live repository/token receipts are required separately."""

import io
import logging
import re
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ALLOWED_ACTIONS = {
    "actions/checkout@v4", "actions/setup-python@v5",
    "home-assistant/actions/hassfest@master", "hacs/action@main",
}


def violations(workflow):
    errors = []
    if workflow.get("permissions") != {"contents": "read"}:
        errors.append("token permissions")
    jobs = workflow.get("jobs", {})
    if set(jobs) != {"approval", "hassfest", "hacs", "ruff", "pytest", "yamllint", "issue25"}:
        errors.append("missing or extra job")
    for name, job in jobs.items():
        if job.get("runs-on") != "ubuntu-24.04" or "environment" in job:
            errors.append("runner/environment boundary")
        # The approval admission alone may read Actions run metadata, nothing more.
        allowed = {"contents": "read", "actions": "read"} if name == "approval" else None
        if job.get("permissions", None) != allowed or "secrets" in job:
            errors.append("job credential override")
        if name != "approval" and job.get("needs") != "approval":
            errors.append("approval dependency")
        for step in job.get("steps", []):
            if "ulimit -f" in step.get("run", ""):
                errors.append("process-wide file limit for log")
            action = step.get("uses")
            if action and action not in ALLOWED_ACTIONS:
                errors.append("unknown action/cache/upload")
            if action == "actions/checkout@v4" and step.get("with", {}).get("persist-credentials") != "false":
                errors.append("persistent checkout")
            if action == "actions/setup-python@v5" and "cache" in step.get("with", {}):
                errors.append("dependency cache")
            if "secrets." in str(step):
                errors.append("secret input")
    errors += fresh_boot_violations(jobs.get("pytest", {}))
    return errors


FRESH_BOOT_RUN = (
    'FAKE_UPTIME=120 timeout --signal=TERM --kill-after=5s 300s python -m pytest '
    '-p tools.fakeboot -p tools.pytest_phases tests/ -q -s > "$RUNNER_TEMP/fakeboot.log" 2>&1'
)


# Each full-suite step runs only in a job whose matrix.runs owns its exact token.
SUITE_IF = "${{ contains(format(' {0} ', matrix.runs), ' suite ') }}"
FRESH_BOOT_IF = "${{ !cancelled() && contains(format(' {0} ', matrix.runs), ' fakeboot ') }}"
SUITE_STEPS = {"suite": "Full retained suite", "fakeboot": "Full suite on a fresh-boot clock"}
# The only ownership values the export accepts, and the runs each one owns.
OWNED_RUNS = {"suite fakeboot": ("suite", "fakeboot"), "suite": ("suite",), "fakeboot": ("fakeboot",)}
MATRIX_KEYS = {"lane", "runs", "python-version", "constraints", "sha256"}
# py3.14 is split; the earlier two lanes keep both runs in one job.
LANE_RUNS = [
    ("py3.12-ha2024.12.0", "suite fakeboot"), ("py3.13-ha2026.2.3", "suite fakeboot"),
    ("py3.14-ha2026.9.0", "suite"), ("py3.14-ha2026.9.0-fakeboot", "fakeboot"),
]
EXPORT_OWNERSHIP = '''case "${OWNED_RUNS:-}" in
  'suite fakeboot') runs=(suite fakeboot) ;;
  suite) runs=(suite) ;;
  fakeboot) runs=(fakeboot) ;;
  *) echo "INVALID_OWNED_RUNS"; runs=(); result=1 ;;
esac'''


def step_runs(step, entry):
    """Whether a step's own ``if`` admits it in the job for one matrix entry."""
    return "if" not in step or admitted(step["if"], {}, matrix=entry)


def fresh_boot_violations(job):
    """Every lane runs the whole suite, and again on a clock 120 s after boot,
    each exactly once across that lane's jobs."""
    steps = [s for s in job.get("steps", []) if "-p tools.fakeboot" in s.get("run", "")]
    names = [s.get("name") for s in job.get("steps", [])]
    if (len(steps) != 1 or steps[0].get("name") != "Full suite on a fresh-boot clock"
            or steps[0].get("if") != FRESH_BOOT_IF
            or FRESH_BOOT_RUN not in steps[0]["run"]
            or "Full retained suite" not in names
            or names.count("Full retained suite") != 1
            or next(s for s in job["steps"] if s.get("name") == "Full retained suite").get("if") != SUITE_IF
            or names.index("Full retained suite") > names.index("Full suite on a fresh-boot clock")
            or names.index("Full suite on a fresh-boot clock") > names.index("Complete logs and owned cleanup")):
        return ["fresh-boot suite guard"]
    export_step = next(s for s in job["steps"] if s.get("name") == "Complete logs and owned cleanup")
    export = export_step["run"]
    if (export_step.get("env") != {"OWNED_RUNS": "${{ matrix.runs }}"}
            or export.count(EXPORT_OWNERSHIP) != 1
            or 'logs=(install sentinel "${runs[@]}")' not in export
            or 'for run in "${runs[@]}"; do' not in export
            or "grep -qx 'FAKEBOOT_UPTIME=120'" not in export):
        return ["fresh-boot suite guard"]
    include = job.get("strategy", {}).get("matrix", {}).get("include", [])
    if ([(e.get("lane"), e.get("runs")) for e in include] != LANE_RUNS
            or any(set(e) != MATRIX_KEYS for e in include)):
        return ["suite lane ownership"]
    by_lock = {}
    for entry in include:
        ran = tuple(run for run, name in SUITE_STEPS.items()
                    if step_runs(next(s for s in job["steps"] if s.get("name") == name), entry))
        # The steps a job executes are exactly the runs its export requires.
        if ran != OWNED_RUNS[entry["runs"]]:
            return ["suite lane ownership"]
        by_lock.setdefault((entry["python-version"], entry["constraints"], entry["sha256"]), []).extend(ran)
    # Three locks; each runs the suite and the fresh-boot suite exactly once.
    if len(by_lock) != 3 or any(sorted(runs) != ["fakeboot", "suite"] for runs in by_lock.values()):
        return ["suite lane ownership"]
    return []


@pytest.fixture(scope="module")
def parsed_workflow():
    path = Path(__file__).parents[1] / ".github/workflows/ci.yml"
    # BaseLoader preserves YAML's literal on/false keys/values as strings.
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


@pytest.fixture
def workflow(parsed_workflow):
    # Parse once per module; each test still gets its own copy to change.
    return deepcopy(parsed_workflow)


def test_declared_ci_boundary(workflow):
    assert violations(workflow) == []


@pytest.mark.parametrize("change", [
    "token", "runner", "environment", "checkout", "upload", "secret", "missing",
    "fakeboot-missing", "fakeboot-one-lane", "fakeboot-skipped-after-failure", "fakeboot-uptime",
    "fakeboot-no-plugin", "fakeboot-subset", "fakeboot-duplicate", "fakeboot-not-exported",
    "fakeboot-clock-unchecked", "fakeboot-after-export",
    "fakeboot-substring-owner", "suite-ungated", "suite-substring-owner", "suite-duplicate",
    "export-all-runs", "export-all-logs", "export-ownership-unread", "export-accepts-any-runs",
    "lane-merged", "lane-fakeboot-dropped", "lane-suite-only", "lane-unknown-runs",
    "lane-runs-missing", "lane-lock-changed", "lane-renamed", "lane-extra-key",
])
def test_weakened_ci_boundary_rejects(workflow, change):
    bad = deepcopy(workflow)
    job = bad["jobs"]["pytest"]
    include = job["strategy"]["matrix"]["include"]
    steps = job["steps"]
    suite = next(s for s in steps if s.get("name") == "Full retained suite")
    export = next(s for s in steps if s.get("name") == "Complete logs and owned cleanup")
    ownership = {
        "fakeboot-substring-owner": lambda: next(
            s for s in steps if s.get("name") == "Full suite on a fresh-boot clock").update(
            {"if": "${{ !cancelled() && contains(matrix.runs, 'fakeboot') }}"}),
        "suite-ungated": lambda: suite.pop("if"),
        "suite-substring-owner": lambda: suite.update({"if": "${{ contains(matrix.runs, 'suite') }}"}),
        "suite-duplicate": lambda: steps.insert(steps.index(suite), deepcopy(suite)),
        "export-all-runs": lambda: export.update(
            {"run": export["run"].replace('for run in "${runs[@]}"; do', "for run in suite fakeboot; do")}),
        "export-all-logs": lambda: export.update(
            {"run": export["run"].replace('logs=(install sentinel "${runs[@]}")',
                                          "logs=(install sentinel suite fakeboot)")}),
        "export-ownership-unread": lambda: export.pop("env"),
        "export-accepts-any-runs": lambda: export.update(
            {"run": export["run"].replace('*) echo "INVALID_OWNED_RUNS"; runs=(); result=1 ;;',
                                          '*) runs=() ;;')}),
    }
    if change in ownership:
        ownership[change]()
        assert violations(bad) == ["fresh-boot suite guard"]
        return
    if change.startswith("lane-"):
        py314, fresh = include[2], include[3]
        if change == "lane-merged":
            py314["runs"] = "suite fakeboot"
            include.remove(fresh)
        elif change == "lane-fakeboot-dropped":
            include.remove(fresh)
        elif change == "lane-suite-only":
            include[0]["runs"] = "suite"
        elif change == "lane-unknown-runs":
            fresh["runs"] = "fakeboot extra"
        elif change == "lane-runs-missing":
            del include[1]["runs"]
        elif change == "lane-lock-changed":
            fresh["sha256"] = include[1]["sha256"]
        elif change == "lane-renamed":
            py314["lane"] = "py3.14-ha2026.9.0-suite"
        else:
            fresh["cache"] = "pip"
        assert violations(bad) == ["suite lane ownership"]
        return
    if change.startswith("fakeboot-"):
        steps = job["steps"]
        fresh = next(s for s in steps if s.get("name") == "Full suite on a fresh-boot clock")
        export = next(s for s in steps if s.get("name") == "Complete logs and owned cleanup")
        if change == "fakeboot-missing":
            steps.remove(fresh)
        elif change == "fakeboot-one-lane":
            fresh["if"] = "${{ !cancelled() && matrix.lane == 'py3.14-ha2026.9.0' }}"
        elif change == "fakeboot-skipped-after-failure":
            del fresh["if"]
        elif change == "fakeboot-uptime":
            fresh["run"] = fresh["run"].replace("FAKE_UPTIME=120", "FAKE_UPTIME=86400")
        elif change == "fakeboot-no-plugin":
            fresh["run"] = fresh["run"].replace("-p tools.fakeboot -p tools.pytest_phases",
                                                "-p tools.pytest_phases")
        elif change == "fakeboot-subset":
            fresh["run"] = fresh["run"].replace("tests/ -q", "tests/test_dwell_wakeup.py -q")
        elif change == "fakeboot-duplicate":
            steps.insert(steps.index(fresh), deepcopy(fresh))
        elif change == "fakeboot-not-exported":
            export["run"] = export["run"].replace('logs=(install sentinel "${runs[@]}")',
                                                  "logs=(install sentinel suite)")
        elif change == "fakeboot-clock-unchecked":
            export["run"] = export["run"].replace("grep -qx 'FAKEBOOT_UPTIME=120'", "true")
        else:
            steps.remove(fresh)
            steps.append(fresh)
        assert violations(bad) == ["fresh-boot suite guard"]
        return
    if change == "token":
        bad["permissions"] = {"contents": "write"}
    elif change == "runner":
        job["runs-on"] = "self-hosted"
    elif change == "environment":
        job["environment"] = "invented-private-environment"
    elif change == "checkout":
        next(s for s in job["steps"] if s.get("uses") == "actions/checkout@v4")["with"]["persist-credentials"] = "true"
    elif change == "upload":
        job["steps"].append({"uses": "actions/upload-artifact@v4"})
    elif change == "secret":
        job["steps"].append({"run": "echo forbidden", "env": {"X": "${{ secrets.INVENTED }}"}})
    else:
        del bad["jobs"]["hacs"]
    assert violations(bad)


def test_installer_and_tests_do_not_inherit_log_file_limits(workflow):
    bad = deepcopy(workflow)
    bad["jobs"]["pytest"]["steps"].append({"run": "ulimit -f 6144\ninstaller > install.log"})
    assert "process-wide file limit for log" in violations(bad)
    for path in ("tools/issue25/run.sh", "tools/issue25/pack.sh", "tools/ci-install.sh"):
        assert "ulimit -f" not in (Path(__file__).parents[1] / path).read_text()


def admitted(expression, context, approval="", matrix=None):
    """Evaluate the authored Boolean subset, with no provider call.

    ``approval`` is the approval job's ``admitted`` output: "" when it was skipped.
    ``matrix`` is one strategy include entry. ``format`` takes the one ``{0}`` used.
    """
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    return bool(eval(expression.strip(), {"__builtins__": {}}, {
        "github": SimpleNamespace(**context), "startsWith": lambda value, prefix: value.startswith(prefix),
        "needs": SimpleNamespace(approval=SimpleNamespace(outputs=SimpleNamespace(admitted=approval))),
        "cancelled": lambda: False,
        "matrix": SimpleNamespace(**(matrix or {})),
        "contains": lambda value, item: item in value,
        "format": lambda template, value: template.replace("{0}", value),
    }))


@pytest.mark.parametrize("runs,suite,fakeboot", [
    ("suite fakeboot", True, True), ("suite", True, False), ("fakeboot", False, True),
    ("", False, False), ("suites", False, False), ("xsuite", False, False),
    ("fakeboots", False, False), ("pre-fakeboot", False, False), ("suite-fakeboot", False, False),
])
def test_suite_steps_match_exact_owned_tokens(workflow, runs, suite, fakeboot):
    """Ownership is by whole token: a run name inside another word owns nothing."""
    steps = {s.get("name"): s for s in workflow["jobs"]["pytest"]["steps"]}
    entry = {"runs": runs}
    assert step_runs(steps["Full retained suite"], entry) is suite
    assert step_runs(steps["Full suite on a fresh-boot clock"], entry) is fakeboot


GATE_CASES = [
    ("admission", "push", "refs/heads/ci/issue25-admission", "", "", 1, True),
    ("ordinary", "push", "refs/heads/feature", "", "", 1, True),
    ("ordinary-pr", "pull_request", "refs/pull/2/merge", "feature", "main", 1, True),
    ("ordinary-manual", "workflow_dispatch", "refs/heads/feature", "", "", 1, True),
    ("r", "push", "refs/heads/ci/issue25-discriminator", "", "", 1, False),
    ("repeat", "push", "refs/heads/ci/issue25-independent-repeat", "", "", 1, False),
    ("a-rerun", "push", "refs/heads/ci/issue25-admission", "", "", 2, False),
    ("r-rerun", "push", "refs/heads/ci/issue25-discriminator", "", "", 2, False),
    ("repeat-rerun", "push", "refs/heads/ci/issue25-independent-repeat", "", "", 2, False),
    ("missing-attempt", "push", "refs/heads/feature", "", "", "", False),
    ("bad-attempt", "push", "refs/heads/feature", "", "", "bad", False),
    ("r-manual", "workflow_dispatch", "refs/heads/ci/issue25-discriminator", "", "", 1, False),
    ("repeat-manual", "workflow_dispatch", "refs/heads/ci/issue25-independent-repeat", "", "", 1, False),
    ("r-pr-head", "pull_request", "refs/pull/2/merge", "ci/issue25-discriminator", "main", 1, False),
    ("r-pr-base", "pull_request", "refs/pull/2/merge", "feature", "ci/issue25-discriminator", 1, False),
    ("repeat-pr-head", "pull_request", "refs/pull/2/merge", "ci/issue25-independent-repeat", "main", 1, False),
    ("repeat-pr-base", "pull_request", "refs/pull/2/merge", "feature", "ci/issue25-independent-repeat", 1, False),
    ("missing-ref", "push", "", "", "", 1, False),
    ("tag", "push", "refs/tags/v0", "", "", 1, False),
    ("missing-event", "", "refs/heads/feature", "", "", 1, False),
    ("bad-event", "schedule", "refs/heads/feature", "", "", 1, False),
    ("missing-pr-head", "pull_request", "refs/pull/2/merge", "", "main", 1, False),
]


@pytest.mark.parametrize("case,event,ref,head,base,attempt,ordinary", GATE_CASES,
                         ids=[row[0] for row in GATE_CASES])
def test_actual_job_gates(workflow, case, event, ref, head, base, attempt, ordinary):
    context = {"event_name": event, "ref": ref, "head_ref": head, "base_ref": base, "run_attempt": attempt}
    selected = {name for name, job in workflow["jobs"].items()
                if name != "approval" and admitted(job["if"], context)}
    assert selected == ({"hassfest", "hacs", "ruff", "pytest", "yamllint"} if ordinary else {"issue25"})
    assert workflow["on"]["push"] == {"branches": ["**"]}
    guard = workflow["jobs"]["issue25"]["steps"][0]["run"]
    env = {"GITHUB_EVENT_NAME": event, "GITHUB_REF": ref, "GITHUB_RUN_ATTEMPT": str(attempt),
           "GITHUB_SHA": "a" * 40, "REPOSITORY_PRIVATE": "false",
           "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_ARCH": "X64"}
    # Match noninteractive CI execution without host profile/rc startup.
    result = subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", guard], env=env,
                            check=False, capture_output=True, text=True, timeout=5)
    valid = False
    assert (result.returncode == 0) is valid
    if not valid:
        reason = "event" if event != "push" else "ref"
        assert result.stderr.strip() == "ISSUE25_REFUSED=" + reason


@pytest.mark.parametrize("key,value", [
    ("GITHUB_SHA", ""), ("GITHUB_SHA", "wrong"),
    ("REPOSITORY_PRIVATE", ""), ("REPOSITORY_PRIVATE", "true"),
    ("RUNNER_ENVIRONMENT", ""), ("RUNNER_ENVIRONMENT", "self-hosted"),
    ("RUNNER_ARCH", ""), ("RUNNER_ARCH", "ARM64"),
], ids=["missing-sha", "bad-sha", "missing-visibility", "private", "missing-runner", "self-hosted",
        "missing-arch", "wrong-arch"])
def test_actual_preacquisition_settings_guard(workflow, key, value):
    env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/ci/issue25-harness-replacement",
           "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40, "REPOSITORY_PRIVATE": "false",
           "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_ARCH": "X64"}
    env[key] = value
    for name in ("pytest", "issue25"):
        steps = workflow["jobs"][name]["steps"]
        assert "uses" not in steps[0]
        result = subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", steps[0]["run"]], env=env,
                                check=False, capture_output=True, text=True, timeout=5)
        assert result.returncode != 0
        reason = {"GITHUB_SHA": "source", "REPOSITORY_PRIVATE": "repository",
                  "RUNNER_ENVIRONMENT": "runner", "RUNNER_ARCH": "architecture"}[key]
        assert result.stderr.strip() == "ISSUE25_REFUSED=" + reason


def test_checkout_binding_and_process_layout(workflow, tmp_path):
    executable = tmp_path / "git"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
    executable.chmod(0o755)
    for name in ("pytest", "issue25"):
        steps = workflow["jobs"][name]["steps"]
        binding = next(s["run"] for s in steps if s.get("name") == "Checkout source binding")
        for sha in ("a" * 40, "b" * 40, ""):
            result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", binding],
                                    env={"PATH": str(tmp_path), "GITHUB_SHA": sha},
                                    check=False, capture_output=True, text=True, timeout=5)
            assert (result.returncode == 0) is (sha == "a" * 40)
        commands = "\n".join(s.get("run", "") for s in steps)
        assert ("tests/ -q" in commands) is (name == "pytest")
        assert "bash tools/issue25/run.sh" in commands
        assert "timeout --signal=TERM --kill-after=5s 60s" in commands
    include = workflow["jobs"]["pytest"]["strategy"]["matrix"]["include"]
    assert [(e["lane"], e["runs"]) for e in include] == LANE_RUNS


@pytest.mark.parametrize("key,value", [
    ("GITHUB_EVENT_NAME", ""), ("GITHUB_EVENT_NAME", "workflow_dispatch"),
    ("GITHUB_REF", ""), ("GITHUB_REF", "refs/heads/feature"),
    ("GITHUB_RUN_ATTEMPT", ""), ("GITHUB_RUN_ATTEMPT", "bad"), ("GITHUB_RUN_ATTEMPT", "2"),
    ("GITHUB_SHA", ""), ("GITHUB_SHA", "bad"), ("GITHUB_SHA", "b" * 40),
    ("REPOSITORY_PRIVATE", ""), ("REPOSITORY_PRIVATE", "true"),
    ("RUNNER_ENVIRONMENT", "self-hosted"), ("RUNNER_ARCH", "ARM64"),
], ids=["missing-event", "manual", "missing-ref", "ordinary-ref", "missing-attempt", "bad-attempt",
        "rerun", "missing-sha", "bad-sha", "wrong-sha", "missing-visibility", "private", "self-hosted", "wrong-arch"])
def test_actual_parent_guard_refuses_before_inventory_or_export(tmp_path, key, value):
    import os

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "forbidden"
    for name, source in {
        "git": '#!/bin/sh\nprintf "%s\\n" aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n',
        "python": '#!/bin/sh\necho forbidden > "$GUARD_MARKER"\nexit 9\n',
    }.items():
        path = bin_dir / name
        path.write_text(source)
        path.chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.defpath}", "RUNNER_TEMP": str(tmp_path),
           "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/ci/issue25-harness-replacement", "ISSUE25_MODE": "pack",
           "GITHUB_RUN_ID": "123", "GITHUB_REPOSITORY": "dkpnw/ha-mxz-coordinator",
           "GITHUB_HEAD_REF": "", "GITHUB_BASE_REF": "", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40, "REPOSITORY_PRIVATE": "false",
           "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_ARCH": "X64", "GUARD_MARKER": str(marker)}
    env[key] = value
    run = Path(__file__).parents[1] / "tools/issue25/run.sh"
    result = subprocess.run(["bash", str(run)], cwd=tmp_path, env=env,
                            check=False, capture_output=True, text=True, timeout=5)
    reason = {"GITHUB_EVENT_NAME": "event", "GITHUB_REF": "ref", "GITHUB_RUN_ATTEMPT": "attempt",
              "GITHUB_SHA": "source", "REPOSITORY_PRIVATE": "repository",
              "RUNNER_ENVIRONMENT": "runner", "RUNNER_ARCH": "architecture"}[key]
    assert result.returncode != 0 and result.stderr.strip() == "ISSUE25_REFUSED=" + reason
    assert not marker.exists()
    assert not (tmp_path / "issue25-inventory-before.txt").exists()
    assert not (tmp_path / "issue25-released").exists()


def suite_payload(exit_code=0, fakeboot=False):
    """Invented ordinary phase output; no HA or candidate imports.

    ``fakeboot``: as ``tools/fakeboot.py`` prints it, once, at session start.
    """
    call_outcome = "failed" if exit_code else "passed"
    return (
        ('FAKEBOOT_UPTIME=120\n' if fakeboot else '')
        + 'invented complete suite output\n'
        'COLLECTED ["invented::test_case"]\n'
        'PHASE {"node": "invented::test_case", "phase": "setup", "outcome": "passed"}\n'
        f'PHASE {{"node": "invented::test_case", "phase": "call", "outcome": "{call_outcome}"}}\n'
        'PHASE {"node": "invented::test_case", "phase": "teardown", "outcome": "passed"}\n'
        f'PHASES_COMPLETE {{"classes": {{}}, "errors": [], "exit": {exit_code}}}\n'
        'PHASES_VALID=true\n'
    )


@pytest.fixture
def suite_export(tmp_path, workflow):
    import os

    root = tmp_path / "scratch"
    root.mkdir()
    activate = root / "mxz-venv/bin/activate"
    activate.parent.mkdir(parents=True)
    activate.write_text("# invented activation; no environment or dependency loading\n")
    for name in ("issue25-released", "issue25-main"):
        (root / name).mkdir()
    (root / "install.log").write_text("invented installation\n")
    (root / "sentinel.log").write_text("invented sentinel\n")
    (root / "suite.log").write_text(suite_payload())
    (root / "suite.exit").write_text("0\n")
    (root / "fakeboot.log").write_text(suite_payload(fakeboot=True))
    (root / "fakeboot.exit").write_text("0\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Only the two Git cleanliness commands in the real export block are stubbed.
    git = bin_dir / "git"
    git.write_text('''#!/bin/bash
case "$*" in
  'diff --exit-code HEAD -- .'|'ls-files --others --exclude-standard') exit 0 ;;
  *) exit 9 ;;
esac
''')
    git.chmod(0o755)
    steps = workflow["jobs"]["pytest"]["steps"]
    export = next(s for s in steps if s.get("name") == "Complete logs and owned cleanup")
    assert export["if"] == "always()"
    suite = next(s["run"] for s in steps if s.get("name") == "Full retained suite")
    assert export["env"] == {"OWNED_RUNS": "${{ matrix.runs }}"}
    # A job owning both runs, as py3.12/py3.13 do; split ownership is tested separately.
    env = {"PATH": f"{bin_dir}:{os.defpath}", "RUNNER_TEMP": str(root), "OWNED_RUNS": "suite fakeboot"}
    return root, bin_dir, env, suite, export["run"]


def run_suite_source(source, root, env):
    return subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", source], cwd=root, env=env,
                          check=False, capture_output=True, text=True, timeout=5)


# Each full-suite step: its pytest arguments, and the environment it must set.
SUITE_RUNS = {
    "suite": ("Full retained suite", "-m pytest -p tools.pytest_phases tests/ -q -s", ""),
    "fakeboot": ("Full suite on a fresh-boot clock",
                 "-m pytest -p tools.fakeboot -p tools.pytest_phases tests/ -q -s", "120"),
}


@pytest.mark.parametrize("owner", ["both", "split"])
@pytest.mark.parametrize("run", sorted(SUITE_RUNS))
@pytest.mark.parametrize("outcome,expected", [
    ("success", 0), ("failure", 1), ("error", 2), ("timeout", 124),
    ("success-oversize", 0), ("failure-oversize", 1),
])
def test_actual_suite_exit_survives_export(suite_export, workflow, run, outcome, expected, owner):
    """``both``: a py3.12/py3.13 job. ``split``: a py3.14 job owning this run alone,
    with no evidence at all from the other run."""
    root, bin_dir, env, _suite, export = suite_export
    name, argv, uptime = SUITE_RUNS[run]
    suite = next(s["run"] for s in workflow["jobs"]["pytest"]["steps"] if s.get("name") == name)
    label = run.upper()
    other = "suite" if run == "fakeboot" else "fakeboot"
    if owner == "split":
        env["OWNED_RUNS"] = run
        (root / f"{other}.exit").unlink()
        (root / f"{other}.log").unlink()
    # Execute the unmodified workflow block with an invented executable, never pytest.
    python = bin_dir / "python"
    python.write_text(f'''#!/bin/bash
set -eu
test "$*" = '{argv}'
test "${{FAKE_UPTIME-unset}}" = '{uptime or "unset"}'
''' + '''if test "$INVENTED_OUTCOME" = timeout; then
  echo 'invented partial suite before timeout'
  exec sleep 10
fi
cat "$RUNNER_TEMP/payload"
exit "$INVENTED_EXIT"
''')
    python.chmod(0o755)
    # Keep the authored watchdog arguments under assertion; shorten only the
    # invented child's deadline so the real timeout branch is cheap to exercise.
    watchdog = bin_dir / "timeout"
    watchdog.write_text('''#!/bin/bash
set -eu
test "$1" = --signal=TERM
test "$2" = --kill-after=5s
test "$3" = 300s
shift 3
if test "$INVENTED_OUTCOME" = timeout; then
  exec /usr/bin/timeout --signal=TERM --kill-after=1s 0.2s "$@"
fi
exec /usr/bin/timeout --signal=TERM --kill-after=5s 300s "$@"
''')
    watchdog.chmod(0o755)
    env.update(INVENTED_OUTCOME=outcome, INVENTED_EXIT=str(expected))
    payload = suite_payload(expected, fakeboot=run == "fakeboot")
    if outcome.endswith("-oversize"):
        payload = "x" * 5242880 + "\n" + payload
    elif outcome == "error":
        payload = 'invented interrupted collection\n'
    (root / "payload").write_text(payload)
    # A prior status must be overwritten, never appended or reused.
    (root / f"{run}.exit").write_text("99\n")
    result = run_suite_source(suite, root, env)
    assert result.returncode == expected, result.stderr
    assert result.stdout.splitlines() == [f"{label}_EXIT={expected}"]
    assert (root / f"{run}.exit").read_text() == f"{expected}\n"
    raw = (root / f"{run}.log").read_text()
    assert raw == ("invented partial suite before timeout\n" if outcome == "timeout" else payload)
    exported = run_suite_source(export, root, env)
    incomplete = outcome in ("timeout", "error") or outcome.endswith("-oversize")
    assert exported.returncode == int(incomplete), exported.stderr
    assert f"RETAINED_{label}_EXIT={expected}\n" in exported.stdout
    if outcome.endswith("-oversize"):
        assert f"LOG_BUDGET_EXCEEDED={run} COMPLETE_EXPORT=false" in exported.stdout
        assert raw not in exported.stdout
    else:
        assert raw in exported.stdout
    assert (root / f"{run}.exit").read_text() == f"{expected}\n"
    assert (root / f"{run}.log").read_text() == raw
    assert "OWNED_SCRATCH_REMOVED=true" in exported.stdout
    assert not (root / "mxz-venv").exists()
    if outcome in ("timeout", "error"):
        assert f"INCOMPLETE_{label}_PHASES" in exported.stdout
        if run == "fakeboot":
            assert "MISSING_FAKEBOOT_CLOCK" in exported.stdout
    if owner == "both":
        # The other run's evidence is untouched and still certifies on its own.
        assert f"RETAINED_{other.upper()}_EXIT=0\n" in exported.stdout
        assert f"INCOMPLETE_{other.upper()}_PHASES" not in exported.stdout
    else:
        # A run this job does not own is neither required nor reported.
        assert f"{other.upper()}_EXIT" not in exported.stdout
        assert f"LOG={other}" not in exported.stdout and f"={other}:" not in exported.stdout
        assert "INVALID_OWNED_RUNS" not in exported.stdout
    assert f"LOG_EXPORT_COMPLETE result={int(incomplete)}" in exported.stdout


@pytest.mark.parametrize("fault", [
    "missing-exit", "empty-exit", "text-exit", "negative-exit", "range-exit", "leading-zero-exit",
    "duplicate-exit", "no-newline-exit", "nul-exit", "oversize-exit",
    "missing-suite", "missing-install", "missing-sentinel", "missing-collection",
    "missing-completion", "missing-validity", "invalid-phases", "duplicate-completion",
    "duplicate-validity", "duplicate-collection", "malformed-completion", "mismatched-exit",
    "truncated-suite", "suite-oversize", "install-oversize", "sentinel-oversize",
])
def test_actual_export_rejects_invalid_or_incomplete_evidence(suite_export, fault):
    root, _bin_dir, env, _suite, export = suite_export
    invalid_exits = {
        "missing-exit": None, "empty-exit": b"", "text-exit": b"bad\n", "negative-exit": b"-1\n",
        "range-exit": b"256\n", "leading-zero-exit": b"00\n", "duplicate-exit": b"0\n0\n",
        "no-newline-exit": b"0", "nul-exit": b"0\0\n", "oversize-exit": b"12345\n",
    }
    if fault in invalid_exits:
        value = invalid_exits[fault]
        if value is None:
            (root / "suite.exit").unlink()
        else:
            (root / "suite.exit").write_bytes(value)
    elif fault in ("missing-suite", "missing-install", "missing-sentinel"):
        (root / (fault.removeprefix("missing-") + ".log")).unlink()
    elif fault.endswith("-oversize"):
        name = fault.removesuffix("-oversize")
        limits = {"suite": 5242880, "install": 6291456, "sentinel": 1048576}
        (root / f"{name}.log").write_text("x" * (limits[name] + 1))
    else:
        prefixes = {"collection": "COLLECTED ", "completion": "PHASES_COMPLETE ", "validity": "PHASES_VALID="}
        payload = suite_payload()
        if fault == "malformed-completion":
            payload = payload.replace('PHASES_COMPLETE {"classes": {}, "errors": [], "exit": 0}',
                                      'PHASES_COMPLETE malformed')
        elif fault == "mismatched-exit":
            payload = payload.replace('"exit": 0', '"exit": 1')
        elif fault == "truncated-suite":
            payload = payload[:len(payload) // 2]
        elif fault == "invalid-phases":
            payload = payload.replace('PHASES_VALID=true', 'PHASES_VALID=false')
            payload = payload.replace('"errors": []', '"errors": ["invented missing call phase"]')
            payload = "\n".join(line for line in payload.splitlines() if '"phase": "call"' not in line) + "\n"
        else:
            prefix = prefixes[fault.split("-", 1)[1]]
            record = next(line for line in payload.splitlines(keepends=True) if line.startswith(prefix))
            payload = payload.replace(record, "") if fault.startswith("missing-") else payload + record
        (root / "suite.log").write_text(payload)
    before = {p.name: p.read_bytes() for p in root.glob("*.log")}
    result = run_suite_source(export, root, env)
    assert result.returncode != 0, result.stderr
    assert "LOG_EXPORT_COMPLETE result=1" in result.stdout
    assert {p.name: p.read_bytes() for p in root.glob("*.log")} == before
    assert not (root / "mxz-venv").exists()
    if fault in invalid_exits:
        assert "RETAINED_SUITE_EXIT=" not in result.stdout
        assert "SUITE_EXIT" in result.stdout
    else:
        assert "RETAINED_SUITE_EXIT=0\n" in result.stdout
    if fault.endswith("-oversize"):
        assert f"LOG_BUDGET_EXCEEDED={fault.removesuffix('-oversize')} COMPLETE_EXPORT=false" in result.stdout
    elif fault.startswith("missing-") and fault.split("-", 1)[1] in ("suite", "install", "sentinel"):
        assert f"MISSING_LOG={fault.removeprefix('missing-')}" in result.stdout
    elif fault not in invalid_exits:
        assert "INCOMPLETE_SUITE_PHASES" in result.stdout


@pytest.mark.parametrize("fault", [
    "missing-exit", "text-exit", "range-exit", "duplicate-exit", "no-newline-exit",
    "missing-log", "oversize-log", "missing-completion", "invalid-phases", "mismatched-exit",
    "missing-clock", "duplicate-clock", "other-uptime", "clock-not-at-line-start",
])
def test_actual_export_rejects_invalid_or_incomplete_fresh_boot_evidence(suite_export, fault):
    """The fresh-boot run certifies like the suite, and only from the fake clock."""
    root, _bin_dir, env, _suite, export = suite_export
    clean = run_suite_source(export, root, env)
    assert clean.returncode == 0 and "LOG_EXPORT_COMPLETE result=0" in clean.stdout, clean.stdout
    for name, payload in (("suite.log", suite_payload()), ("fakeboot.log", suite_payload(fakeboot=True))):
        (root / name).write_text(payload)
    for name in ("mxz-venv", "issue25-released", "issue25-main"):
        (root / name).mkdir(exist_ok=True)
    exits = {"missing-exit": None, "text-exit": b"bad\n", "range-exit": b"256\n",
             "duplicate-exit": b"0\n0\n", "no-newline-exit": b"0"}
    payload = suite_payload(fakeboot=True)
    if fault in exits:
        if exits[fault] is None:
            (root / "fakeboot.exit").unlink()
        else:
            (root / "fakeboot.exit").write_bytes(exits[fault])
    elif fault == "missing-log":
        (root / "fakeboot.log").unlink()
    elif fault == "oversize-log":
        (root / "fakeboot.log").write_text("x" * 5242880 + "\n" + payload)
    else:
        payload = {
            "missing-completion": "\n".join(l for l in payload.splitlines() if not l.startswith("PHASES_COMPLETE ")) + "\n",
            "invalid-phases": payload.replace("PHASES_VALID=true", "PHASES_VALID=false"),
            "mismatched-exit": payload.replace('"exit": 0', '"exit": 1'),
            "missing-clock": suite_payload(),
            "duplicate-clock": payload + "FAKEBOOT_UPTIME=120\n",
            "other-uptime": payload.replace("FAKEBOOT_UPTIME=120", "FAKEBOOT_UPTIME=86400"),
            "clock-not-at-line-start": payload.replace("FAKEBOOT_UPTIME=120", " FAKEBOOT_UPTIME=120"),
        }[fault]
        (root / "fakeboot.log").write_text(payload)
    before = {p.name: p.read_bytes() for p in root.glob("*.log")}
    result = run_suite_source(export, root, env)
    assert result.returncode != 0, result.stdout
    assert "LOG_EXPORT_COMPLETE result=1" in result.stdout
    assert {p.name: p.read_bytes() for p in root.glob("*.log")} == before
    assert not (root / "mxz-venv").exists()
    assert "RETAINED_SUITE_EXIT=0\n" in result.stdout
    assert "INCOMPLETE_SUITE_PHASES" not in result.stdout
    expected = {
        "missing-exit": "MISSING_OR_OVERSIZE_FAKEBOOT_EXIT", "missing-log": "MISSING_LOG=fakeboot",
        "oversize-log": "LOG_BUDGET_EXCEEDED=fakeboot COMPLETE_EXPORT=false",
        "missing-completion": "INCOMPLETE_FAKEBOOT_PHASES", "invalid-phases": "INCOMPLETE_FAKEBOOT_PHASES",
        "mismatched-exit": "INCOMPLETE_FAKEBOOT_PHASES",
    }.get(fault, "INVALID_FAKEBOOT_EXIT" if fault in exits else "MISSING_FAKEBOOT_CLOCK")
    assert expected in result.stdout.splitlines(), result.stdout


@pytest.mark.parametrize("run", sorted(SUITE_RUNS))
@pytest.mark.parametrize("fault", [
    None, "missing-exit", "invalid-exit", "missing-log", "oversize-log", "invalid-phases",
    "missing-install", "missing-sentinel", "dirty-tree", "untracked-file", "fakeboot-clock",
])
def test_actual_split_export_requires_owned_evidence(suite_export, run, fault):
    """A py3.14 job owning one run still needs that run's exit and log, the install
    and sentinel logs, and every check on them; the other run is not required."""
    root, bin_dir, env, _suite, export = suite_export
    other = "suite" if run == "fakeboot" else "fakeboot"
    env["OWNED_RUNS"] = run
    for suffix in ("exit", "log"):
        (root / f"{other}.{suffix}").unlink()
    expected = None
    if fault == "missing-exit":
        (root / f"{run}.exit").unlink()
        expected = f"MISSING_OR_OVERSIZE_{run.upper()}_EXIT"
    elif fault == "invalid-exit":
        (root / f"{run}.exit").write_text("00\n")
        expected = f"INVALID_{run.upper()}_EXIT"
    elif fault in ("missing-log", "missing-install", "missing-sentinel"):
        log = run if fault == "missing-log" else fault.removeprefix("missing-")
        (root / f"{log}.log").unlink()
        expected = f"MISSING_LOG={log}"
    elif fault == "oversize-log":
        (root / f"{run}.log").write_text("x" * 5242881)
        expected = f"LOG_BUDGET_EXCEEDED={run} COMPLETE_EXPORT=false"
    elif fault == "invalid-phases":
        (root / f"{run}.log").write_text(
            suite_payload(fakeboot=run == "fakeboot").replace("PHASES_VALID=true", "PHASES_VALID=false"))
        expected = f"INCOMPLETE_{run.upper()}_PHASES"
    elif fault in ("dirty-tree", "untracked-file"):
        # The stub reports a modified tracked file or an untracked one.
        command = "diff --exit-code HEAD -- ." if fault == "dirty-tree" else "ls-files --others --exclude-standard"
        reply = "exit 1" if fault == "dirty-tree" else "echo invented-untracked; exit 0"
        (bin_dir / "git").write_text(f'''#!/bin/bash
case "$*" in
  '{command}') {reply} ;;
  'diff --exit-code HEAD -- .'|'ls-files --others --exclude-standard') exit 0 ;;
  *) exit 9 ;;
esac
''')
    elif fault == "fakeboot-clock":
        # The fake-clock check binds only the fresh-boot log; a suite-only job has none.
        (root / f"{run}.log").write_text(suite_payload())
        expected = "MISSING_FAKEBOOT_CLOCK" if run == "fakeboot" else None
    result = run_suite_source(export, root, env)
    failed = fault is not None and not (fault == "fakeboot-clock" and run == "suite")
    assert result.returncode == int(failed), result.stdout
    assert f"LOG_EXPORT_COMPLETE result={int(failed)}" in result.stdout
    if expected:
        assert expected in result.stdout.splitlines(), result.stdout
    assert f"{other.upper()}_EXIT" not in result.stdout and f"={other}:" not in result.stdout
    assert "INVALID_OWNED_RUNS" not in result.stdout
    assert "OWNED_SCRATCH_REMOVED=true" in result.stdout
    assert not (root / "mxz-venv").exists()


@pytest.mark.parametrize("owned", [
    None, "", " ", "suites", "SUITE", "suite bogus", "bogus", "suite suite", "fakeboot suite",
    " suite", "suite ", "suite  fakeboot", "suite\tfakeboot", "suite fakeboot fakeboot",
])
def test_actual_export_rejects_empty_or_unknown_ownership(suite_export, owned):
    """No valid ownership certifies nothing, yet still exports and cleans up."""
    root, _bin_dir, env, _suite, export = suite_export
    if owned is None:
        del env["OWNED_RUNS"]
    else:
        env["OWNED_RUNS"] = owned
    result = run_suite_source(export, root, env)
    assert result.returncode == 1, result.stdout
    lines = result.stdout.splitlines()
    assert lines[0] == "INVALID_OWNED_RUNS"
    assert "RETAINED_" not in result.stdout and "LOG_BYTES=suite" not in result.stdout
    assert "LOG_BYTES=fakeboot" not in result.stdout
    assert any(line.startswith("LOG_BYTES=install:") for line in lines)
    assert any(line.startswith("LOG_BYTES=sentinel:") for line in lines)
    assert "OWNED_SCRATCH_REMOVED=true" in lines
    assert lines[-1] == "LOG_EXPORT_COMPLETE result=1"
    assert not (root / "mxz-venv").exists()


def test_suite_log_stream_keeps_warnings_while_tests_keep_info(caplog):
    """CI runs ``-s``: Home Assistant's per-setup INFO stays out of suite.log, not out of tests."""
    streams = [h for h in logging.getLogger().handlers if type(h) is logging.StreamHandler]
    assert streams, "pytest-homeassistant-custom-component's stderr handler is missing"
    logger = logging.getLogger("homeassistant.setup")
    sink = io.StringIO()
    old = [h.setStream(sink) for h in streams]
    try:
        logger.info("info for tests only")
        logger.warning("warning for the log too")
    finally:
        for handler, stream in zip(streams, old):
            handler.setStream(stream)
    assert "info for tests only" not in sink.getvalue()
    assert "warning for the log too" in sink.getvalue()
    assert [r.getMessage() for r in caplog.records] == ["info for tests only", "warning for the log too"]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_actual_export_retains_entire_four_mib_suite(suite_export, exit_code):
    root, _bin_dir, env, _suite, export = suite_export
    payload = suite_payload(exit_code)
    # Historical four MiB boundary: retain the whole file for both ordinary exits.
    payload = "x" * (4194304 - len(payload) - 1) + "\n" + payload
    (root / "suite.log").write_text(payload)
    (root / "suite.exit").write_text(f"{exit_code}\n")
    result = run_suite_source(export, root, env)
    assert result.returncode == 0, result.stderr
    assert f"RETAINED_SUITE_EXIT={exit_code}\n" in result.stdout
    assert "LOG_BYTES=suite:4194304\n" in result.stdout
    assert payload in result.stdout
    assert (root / "suite.log").read_text() == payload
    assert (root / "suite.exit").read_text() == f"{exit_code}\n"
    assert "LOG_EXPORT_COMPLETE result=0" in result.stdout


EXPORT_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
              "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1"}


@pytest.fixture(scope="module")
def boundary_phases(tmp_path_factory):
    """Real child pytest output per exit code; both size boundaries reuse it.

    The child loads every installed plugin, as the suite does, which costs seconds per
    interpreter; the two exit codes run concurrently and only once per module.
    """
    import sys

    plugin = Path(__file__).parents[1] / "tools/pytest_phases.py"
    children = {}
    for exit_code in (0, 1):
        root = tmp_path_factory.mktemp(f"boundary-child-{exit_code}")
        (root / "conftest.py").write_bytes(plugin.read_bytes())
        (root / "test_invented_boundary.py").write_text(
            f"def test_invented_boundary():\n    assert {exit_code} == 0\n")
        # Explicit child fixture scope for pytest-asyncio; plain pytest accepts -o
        # without introducing an unregistered option into an ini configuration file.
        # -p no:respx is what the repository's pytest.ini adds to the suite itself.
        children[exit_code] = subprocess.Popen(
            [sys.executable, "-I", "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:respx",
             "-o", "asyncio_default_fixture_loop_scope=function",
             "-q", "-s", "test_invented_boundary.py"], cwd=root, env=EXPORT_ENV,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    results = {}
    try:
        for exit_code, child in children.items():
            stdout, stderr = child.communicate(timeout=30)
            results[exit_code] = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
    finally:
        for child in children.values():
            child.kill()
    return results


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("size", [5242880, 5242881], ids=["exact-five-mib", "five-mib-plus-one"])
def test_actual_export_five_mib_boundary(tmp_path, workflow, boundary_phases, exit_code, size):
    # Real pytest emits the phase records; real Git runs the cleanliness checks.
    # All input data is invented. No endpoint, interpreter or Git stand-ins.
    env = dict(EXPORT_ENV)
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (["git", "-c", "init.templateDir=", "init", "-q"],
                 ["git", "-c", "user.name=Invented fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "Invented export fixture"]):
        result = subprocess.run(argv, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                check=False, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
    root = tmp_path / "scratch"
    root.mkdir()
    phases = boundary_phases[exit_code]
    assert phases.returncode == exit_code and phases.stderr == b"", phases
    assert b"PHASES_VALID=true\n" in phases.stdout, phases
    payload = b"x" * (size - len(phases.stdout) - 1) + b"\n" + phases.stdout
    assert len(payload) == size
    (root / "suite.log").write_bytes(payload)
    (root / "suite.exit").write_text(f"{exit_code}\n")
    # Invented phases, not the child's: the oversize suite must stay unexported.
    (root / "fakeboot.log").write_text(suite_payload(exit_code, fakeboot=True))
    (root / "fakeboot.exit").write_text(f"{exit_code}\n")
    for name in ("install", "sentinel"):
        (root / f"{name}.log").write_text(f"invented {name} log\n")
    for name in ("mxz-venv", "issue25-released", "issue25-main"):
        (root / name).mkdir()
    export = next(s for s in workflow["jobs"]["pytest"]["steps"]
                  if s.get("name") == "Complete logs and owned cleanup")
    assert export["if"] == "always()"
    assert "suite) limit=5242880 ;;" in export["run"]
    assert "fakeboot) limit=5242880 ;;" in export["run"]
    env["RUNNER_TEMP"] = str(root)
    env["OWNED_RUNS"] = "suite fakeboot"
    result = subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", export["run"]],
                            cwd=repo, env=env, stdin=subprocess.DEVNULL,
                            check=False, capture_output=True, timeout=5)
    oversize = size > 5242880
    assert result.returncode == int(oversize) and result.stderr == b"", result.stderr
    assert f"RETAINED_SUITE_EXIT={exit_code}\n".encode() in result.stdout
    assert f"LOG_BYTES=suite:{size}\n".encode() in result.stdout
    if oversize:
        assert b"LOG_BUDGET_EXCEEDED=suite COMPLETE_EXPORT=false\n" in result.stdout
        assert payload not in result.stdout and phases.stdout not in result.stdout
    else:
        assert result.stdout.count(payload) == 1
        assert b"INCOMPLETE_SUITE_PHASES" not in result.stdout
    assert (root / "suite.log").read_bytes() == payload
    assert (root / "suite.exit").read_text() == f"{exit_code}\n"
    assert b"OWNED_SCRATCH_REMOVED=true\n" in result.stdout
    assert all(not (root / name).exists() for name in ("mxz-venv", "issue25-released", "issue25-main"))
    assert f"LOG_EXPORT_COMPLETE result={int(oversize)}\n".encode() in result.stdout


RECOVERY_ROUTES = [
    ('A3-push', 'push', 'refs/heads/ci/issue25-harness-admission', '', '', 1, True, None),
    ('A3-PR-head', 'pull_request', 'refs/pull/2/merge', 'ci/issue25-harness-admission', 'main', 1, False, 'event'),
    ('A3-PR-base', 'pull_request', 'refs/pull/2/merge', 'feature', 'ci/issue25-harness-admission', 1, False, 'event'),
    ('A3-dispatch', 'workflow_dispatch', 'refs/heads/ci/issue25-harness-admission', '', '', 1, False, 'event'),
    ('A3-attempt2', 'push', 'refs/heads/ci/issue25-harness-admission', '', '', 2, False, 'ref'),
    ('A3-tag-shaped-ref', 'push', 'refs/tags/ci/issue25-harness-admission', '', '', 1, False, 'ref'),
    ('R2-push', 'push', 'refs/heads/ci/issue25-harness-replacement', '', '', 1, False, None),
    ('I1-push', 'push', 'refs/heads/ci/issue25-harness-repeat', '', '', 1, False, None),
    ('R2-dispatch', 'workflow_dispatch', 'refs/heads/ci/issue25-harness-replacement', '', '', 1, False, 'event'),
    ('I1-dispatch', 'workflow_dispatch', 'refs/heads/ci/issue25-harness-repeat', '', '', 1, False, 'event'),
    ('R2-PR-head', 'pull_request', 'refs/pull/2/merge', 'ci/issue25-harness-replacement', 'main', 1, False, 'event'),
    ('R2-PR-base', 'pull_request', 'refs/pull/2/merge', 'feature', 'ci/issue25-harness-replacement', 1, False, 'event'),
    ('I1-PR-head', 'pull_request', 'refs/pull/2/merge', 'ci/issue25-harness-repeat', 'main', 1, False, 'event'),
    ('I1-PR-base', 'pull_request', 'refs/pull/2/merge', 'feature', 'ci/issue25-harness-repeat', 1, False, 'event'),
    ('R2-attempt2', 'push', 'refs/heads/ci/issue25-harness-replacement', '', '', 2, False, 'attempt'),
    ('I1-attempt2', 'push', 'refs/heads/ci/issue25-harness-repeat', '', '', 2, False, 'attempt'),
    ('missing-PR-base', 'pull_request', 'refs/pull/2/merge', 'feature', '', 1, False, 'event'),
    ('A3-empty-attempt', 'push', 'refs/heads/ci/issue25-harness-admission', '', '', '', False, 'ref'),
    ('A3-bad-attempt', 'push', 'refs/heads/ci/issue25-harness-admission', '', '', 'bad', False, 'ref'),
    ('malformed-ref', 'push', 'bad', '', '', 1, False, 'ref'),
]


@pytest.mark.parametrize('case,event,ref,head,base,attempt,ordinary,reason', RECOVERY_ROUTES,
                         ids=[f'G{i:02d}' for i in range(1, 21)])
def test_recovery_route_actual_guards(workflow, case, event, ref, head, base, attempt, ordinary, reason):
    context = {'event_name': event, 'ref': ref, 'head_ref': head, 'base_ref': base, 'run_attempt': attempt}
    jobs = workflow['jobs']
    selected = {name for name, job in jobs.items() if name != 'approval' and admitted(job['if'], context)}
    assert selected == ({'hassfest', 'hacs', 'ruff', 'pytest', 'yamllint'} if ordinary else {'issue25'})
    floor_steps = jobs['pytest']['steps']
    lanes = [entry['lane'] for entry in jobs['pytest']['strategy']['matrix']['include']]
    assert lanes == [lane for lane, _runs in LANE_RUNS]
    for step in floor_steps:
        if step.get('name') in ('Six exported setup controls', 'Exact admission comparison objects'):
            for lane in lanes:
                expression = step['if'].replace('matrix.lane', repr(lane))
                assert admitted(expression, context) is (case == 'A3-push' and lane == 'py3.12-ha2024.12.0')
    guard = jobs['pytest' if ordinary else 'issue25']['steps'][0]['run']
    env = {'GITHUB_EVENT_NAME': event, 'GITHUB_REF': ref, 'GITHUB_HEAD_REF': head, 'GITHUB_BASE_REF': base,
           'GITHUB_RUN_ATTEMPT': str(attempt), 'GITHUB_SHA': 'a' * 40, 'REPOSITORY_PRIVATE': 'false',
           'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_ARCH': 'X64'}
    result = subprocess.run(['bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c', guard], env=env,
                            check=False, capture_output=True, text=True, timeout=5)
    assert result.returncode == (0 if reason is None else 1), result.stderr
    assert result.stderr.strip() == ('' if reason is None else 'ISSUE25_REFUSED=' + reason)


P_CASES = ('missing-mode', 'empty-mode', 'unknown-mode', 'pack-on-A3', 'admission-on-R2',
           'admission-on-I1', 'unexpected-positional', 'CLI-mode-plus-env', 'legacy-R-ref',
           'legacy-repeat-ref', 'missing-runner', 'missing-architecture', 'unknown-ISSUE25-key',
           'PYTHONPATH', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS')


@pytest.mark.parametrize('fault', P_CASES, ids=[f'P{i:02d}' for i in range(1, 17)])
def test_parent_entry_refusal_reasons(tmp_path, fault):
    from tests.test_issue25_pack import PACK, parent_inputs

    root, env, calls, exports = parent_inputs(tmp_path, stage='A3')
    args = []
    reason = 'mode'
    if fault == 'missing-mode':
        del env['ISSUE25_MODE']
    elif fault in ('empty-mode', 'unknown-mode'):
        env['ISSUE25_MODE'] = '' if fault == 'empty-mode' else 'unknown'
    elif fault == 'pack-on-A3':
        env['ISSUE25_MODE'] = 'pack'
        reason = 'mode-ref'
    elif fault in ('admission-on-R2', 'admission-on-I1'):
        env['GITHUB_REF'] = 'refs/heads/ci/issue25-harness-' + ('replacement' if fault.endswith('R2') else 'repeat')
        reason = 'mode-ref'
    elif fault in ('unexpected-positional', 'CLI-mode-plus-env'):
        args = ['invented'] if fault == 'unexpected-positional' else ['--mode', 'admission']
        reason = 'arguments'
    elif fault.startswith('legacy-'):
        env['GITHUB_REF'] = 'refs/heads/ci/issue25-' + ('discriminator' if fault == 'legacy-R-ref' else 'independent-repeat')
        reason = 'ref'
    elif fault == 'missing-runner':
        del env['RUNNER_ENVIRONMENT']
        reason = 'runner'
    elif fault == 'missing-architecture':
        del env['RUNNER_ARCH']
        reason = 'architecture'
    elif fault == 'unknown-ISSUE25-key':
        # One actual call, four separately enumerated refusal records. The actual
        # parent visits every supplied key without short-circuiting its scan.
        env.update(ISSUE25_UNEXPECTED='x', ISSUE25_INJECTION='', ISSUE25_BASE='released', ISSUE25_CASE_ID='P-release')
        reason = None
    else:
        env[fault] = 'invented'
        reason = 'override-' + fault
    result = subprocess.run(['bash', str(PACK.with_name('run.sh')), *args], cwd=root, env=env,
                            check=False, capture_output=True, text=True, timeout=5)
    assert result.returncode == 1, result.stdout + result.stderr
    assert not calls.exists() and not exports.exists()
    assert not (tmp_path / 'issue25-A3-123-a1').exists()
    assert result.stdout == ''
    if reason is None:
        assert len(result.stderr.splitlines()) == 4
        assert set(result.stderr.splitlines()) == {'ISSUE25_REFUSED=control-key:' + key for key in (
            'ISSUE25_UNEXPECTED', 'ISSUE25_INJECTION', 'ISSUE25_BASE', 'ISSUE25_CASE_ID')}
    else:
        assert result.stderr.strip() == 'ISSUE25_REFUSED=' + reason
