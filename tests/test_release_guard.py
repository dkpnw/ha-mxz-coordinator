"""The release-provenance guard: the tag, the manifest and the pre-release flag agree.

Runs the workflow's own step, with a stand-in for ``gh``, in a folder holding only a
manifest. The workflow is the code under test, so nothing here restates its rule.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

FLOW = Path(__file__).parents[1] / ".github/workflows/release-provenance.yml"
GUARD = "Check the tag, the manifest and the pre-release flag agree"


def _steps() -> list[dict]:
    return yaml.safe_load(FLOW.read_text())["jobs"]["zip"]["steps"]


def _run(tmp_path: Path, tag: str, version: str | None, prerelease: str) -> subprocess.CompletedProcess[str]:
    folder = tmp_path / "custom_components/mxz_coordinator"
    folder.mkdir(parents=True)
    if version is not None:
        (folder / "manifest.json").write_text(json.dumps({"domain": "mxz_coordinator", "version": version}))
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text("#!/bin/sh\n" + ("exit 1\n" if prerelease == "gh-fails" else f"echo {prerelease}\n"))
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    script = next(step["run"] for step in _steps() if step.get("name") == GUARD)
    # GitHub runs a plain `run` step with `bash -e {0}`.
    return subprocess.run(
        ["bash", "-e", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False,
        env={"PATH": f"{shim}:/usr/bin:/bin", "TAG": tag},
    )


@pytest.mark.parametrize(
    ("tag", "version", "prerelease"),
    [
        ("v3.21.0", "3.21.0", "false"),
        ("v3.21.0b1", "3.21.0b1", "true"),
        ("v3.21.0b12", "3.21.0b12", "true"),
        ("v10.0.3", "10.0.3", "false"),
    ],
)
def test_the_guard_passes_what_agrees(tmp_path: Path, tag: str, version: str, prerelease: str) -> None:
    done = _run(tmp_path, tag, version, prerelease)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    ("tag", "version", "prerelease", "says"),
    [
        ("v3.21.0", "3.20.0", "false", "is not 'v3.20.0'"),
        ("v3.21.0b1", "3.21.0", "true", "is not 'v3.21.0'"),
        ("3.21.0", "3.21.0", "false", "is not 'v3.21.0'"),
        ("v3.21.0b1", "3.21.0b1", "false", "needs 'true'"),
        ("v3.21.0", "3.21.0", "true", "needs 'false'"),
        ("v3.0.0-beta.19", "3.0.0-beta.19", "true", "neither"),
        ("v3.21.0rc1", "3.21.0rc1", "true", "neither"),
        ("v3.21", "3.21", "false", "neither"),
    ],
)
def test_the_guard_refuses_what_doesnt(tmp_path: Path, tag: str, version: str, prerelease: str, says: str) -> None:
    done = _run(tmp_path, tag, version, prerelease)
    assert done.returncode != 0
    assert says in done.stdout
    assert "::error::" in done.stdout


def test_no_answer_from_github_is_a_failure(tmp_path: Path) -> None:
    assert _run(tmp_path, "v3.21.0", "3.21.0", "gh-fails").returncode != 0


def test_no_manifest_is_a_failure(tmp_path: Path) -> None:
    assert _run(tmp_path, "v3.21.0", None, "false").returncode != 0


def test_the_guard_runs_before_anything_is_built_or_attached_and_runs_no_tag_code() -> None:
    names = [step.get("name", step.get("uses", "")) for step in _steps()]
    guard = names.index(GUARD)
    assert names.index("Build a deterministic source zip") > guard
    assert names.index("Upload to the existing release") > guard
    assert next(i for i, name in enumerate(names) if name.startswith("actions/attest")) > guard
    assert next(i for i, name in enumerate(names) if name.startswith("actions/checkout")) < guard
    assert "tools/" not in _steps()[guard]["run"]
