"""Static CI boundaries; live repository/token receipts are required separately."""

from copy import deepcopy
from pathlib import Path
import re
import subprocess
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
    if set(jobs) != {"hassfest", "hacs", "ruff", "pytest", "yamllint", "issue25"}:
        errors.append("missing or extra job")
    for job in jobs.values():
        if job.get("runs-on") != "ubuntu-24.04" or "environment" in job:
            errors.append("runner/environment boundary")
        if "permissions" in job or "secrets" in job:
            errors.append("job credential override")
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
    return errors


@pytest.fixture
def workflow():
    path = Path(__file__).parents[1] / ".github/workflows/ci.yml"
    # BaseLoader preserves YAML's literal on/false keys/values as strings.
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def test_declared_ci_boundary(workflow):
    assert violations(workflow) == []


@pytest.mark.parametrize("change", ["token", "runner", "environment", "checkout", "upload", "secret", "missing"])
def test_weakened_ci_boundary_rejects(workflow, change):
    bad = deepcopy(workflow)
    job = bad["jobs"]["pytest"]
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


def admitted(expression, context):
    """Evaluate the authored Boolean subset, with no provider call."""
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    return bool(eval(expression.strip(), {"__builtins__": {}}, {
        "github": SimpleNamespace(**context), "startsWith": lambda value, prefix: value.startswith(prefix),
    }))


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
    context = dict(event_name=event, ref=ref, head_ref=head, base_ref=base, run_attempt=attempt)
    selected = {name for name, job in workflow["jobs"].items() if admitted(job["if"], context)}
    assert selected == ({"hassfest", "hacs", "ruff", "pytest", "yamllint"} if ordinary else {"issue25"})
    assert workflow["on"]["push"] == {"branches": ["**"]}
    guard = workflow["jobs"]["issue25"]["steps"][0]["run"]
    env = {"GITHUB_EVENT_NAME": event, "GITHUB_REF": ref, "GITHUB_RUN_ATTEMPT": str(attempt),
           "GITHUB_SHA": "a" * 40, "REPOSITORY_PRIVATE": "false",
           "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_ARCH": "X64"}
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", guard], env=env,
                            capture_output=True, text=True, timeout=5)
    valid = case in ("r", "repeat")
    assert (result.returncode == 0) is valid
    if not valid:
        assert "ISSUE25_REFUSED=" in result.stderr


@pytest.mark.parametrize("key,value", [
    ("GITHUB_SHA", ""), ("GITHUB_SHA", "wrong"),
    ("REPOSITORY_PRIVATE", ""), ("REPOSITORY_PRIVATE", "true"),
    ("RUNNER_ENVIRONMENT", ""), ("RUNNER_ENVIRONMENT", "self-hosted"),
    ("RUNNER_ARCH", ""), ("RUNNER_ARCH", "ARM64"),
], ids=["missing-sha", "bad-sha", "missing-visibility", "private", "missing-runner", "self-hosted",
        "missing-arch", "wrong-arch"])
def test_actual_preacquisition_settings_guard(workflow, key, value):
    env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/ci/issue25-discriminator",
           "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40, "REPOSITORY_PRIVATE": "false",
           "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_ARCH": "X64"}
    env[key] = value
    for name in ("pytest", "issue25"):
        steps = workflow["jobs"][name]["steps"]
        assert "uses" not in steps[0]
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", steps[0]["run"]], env=env,
                                capture_output=True, text=True, timeout=5)
        assert result.returncode != 0


def test_checkout_binding_and_process_layout(workflow, tmp_path):
    executable = tmp_path / "git"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
    executable.chmod(0o755)
    for name in ("pytest", "issue25"):
        steps = workflow["jobs"][name]["steps"]
        binding = next(s["run"] for s in steps if s.get("name") == "Checkout source binding")
        for sha in ("a" * 40, "b" * 40, ""):
            result = subprocess.run(["/bin/bash", "-euo", "pipefail", "-c", binding],
                                    env={"PATH": str(tmp_path), "GITHUB_SHA": sha},
                                    capture_output=True, text=True, timeout=5)
            assert (result.returncode == 0) is (sha == "a" * 40)
        commands = "\n".join(s.get("run", "") for s in steps)
        assert ("tests/ -q" in commands) is (name == "pytest")
        assert ("bash tools/issue25/run.sh" in commands) is (name == "issue25")
        assert "timeout --signal=TERM --kill-after=5s 60s" in commands
    assert len(workflow["jobs"]["pytest"]["strategy"]["matrix"]["include"]) == 3


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
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RUNNER_TEMP=str(tmp_path),
               GITHUB_EVENT_NAME="push", GITHUB_REF="refs/heads/ci/issue25-discriminator",
               GITHUB_RUN_ATTEMPT="1", GITHUB_SHA="a" * 40, REPOSITORY_PRIVATE="false",
               RUNNER_ENVIRONMENT="github-hosted", RUNNER_ARCH="X64", GUARD_MARKER=str(marker))
    env[key] = value
    run = Path(__file__).parents[1] / "tools/issue25/run.sh"
    result = subprocess.run(["bash", str(run)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and "ISSUE25_REFUSED=" in result.stderr
    assert not marker.exists()
    assert not (tmp_path / "issue25-inventory-before.txt").exists()
    assert not (tmp_path / "issue25-released").exists()
