"""Static CI boundaries; live repository/token receipts are required separately."""

from copy import deepcopy
from pathlib import Path

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
    if set(jobs) != {"hassfest", "hacs", "ruff", "pytest", "yamllint"}:
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
        job["steps"][0]["with"]["persist-credentials"] = "true"
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
