"""Additional lane controls, with invented metadata/platform/phase fixtures only."""

import copy
import importlib.util
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ha202610", ROOT / "tools/ha202610.py")
lane = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lane)


def workflow():
    return yaml.load((ROOT / ".github/workflows/ci-ha2026.10.yml").read_text(), Loader=yaml.BaseLoader)


def test_public_bundle_and_exact_three_abi_constraints():
    bundle = lane.load_bundle()
    constrained = {r["name"] for r in bundle["artifacts"] if "manylinux_2_41" in r["url"]}
    assert constrained == {"bluetooth-data-tools", "fnv-hash-fast", "ulid-transform"}
    assert len(bundle["core"]["inputs"]) == 24
    assert len(bundle["plugin"]["outputs"]) == 27
    assert len(bundle["artifacts"]) == 157
    assert len(bundle["build_names"]) == 7


@pytest.mark.parametrize("change", ["core", "beta", "plugin", "python", "ubuntu24", "archive",
                                   "private", "bare-plugin", "duplicate", "no-hash", "origin"])
def test_wrong_bundle_refused(change):
    bundle = copy.deepcopy(lane.load_bundle())
    if change == "core":
        bundle["core"]["commit"] = "a" * 40
    elif change == "beta":
        next(r for r in bundle["artifacts"] if r["name"] == "homeassistant")["version"] = "2026.10.0b4"
    elif change == "plugin":
        bundle["plugin"]["commit"] = "b" * 40
    elif change == "python":
        bundle["python"]["version"] = "3.14.2"
    elif change == "ubuntu24":
        bundle["python"]["platform_version"] = "24.04"
    elif change == "archive":
        bundle["python"]["sha256"] = "f" * 64
    elif change == "private":
        bundle["artifacts"][0]["url"] = "file:///invented-private/runtime.whl"
    elif change == "bare-plugin":
        bundle["artifacts"][0]["name"] = lane.PLUGIN
    elif change == "duplicate":
        bundle["artifacts"][0] = bundle["artifacts"][1]
    elif change == "no-hash":
        bundle["artifacts"][0]["sha256"] = ""
    else:
        bundle["core"]["repository"] = "https://example.invalid/core.git"
    with pytest.raises(ValueError):
        lane.validate_bundle(bundle)


def metadata_wheel(tmp_path, *, version="0.13.370", core="2026.10.0", vendored=False):
    path = tmp_path / "invented.whl"
    metadata = f"Metadata-Version: 2.4\nName: pytest-homeassistant-custom-component\nVersion: {version}\nRequires-Dist: homeassistant=={core}\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("invented.dist-info/METADATA", metadata)
        if vendored:
            archive.writestr("invented/_vendor/other.dist-info/METADATA", "Name: other\nVersion: 1\n")
    return path


@pytest.mark.parametrize("core", ["2026.10.0b4", "2026.9.0", "2026.10.1"])
def test_actual_wheel_metadata_rejects_wrong_core(tmp_path, core):
    with pytest.raises(ValueError, match="plugin requires beta or wrong core"):
        lane.wheel_metadata(metadata_wheel(tmp_path, core=core), lane.PLUGIN, "0.13.370")


def test_actual_wheel_metadata_rejects_wrong_plugin(tmp_path):
    with pytest.raises(ValueError, match="wrong wheel identity"):
        lane.wheel_metadata(metadata_wheel(tmp_path, version="0.13.369"), lane.PLUGIN, "0.13.370")


def test_metadata_has_one_top_level_identity_despite_vendored_packages(tmp_path):
    assert lane.wheel_metadata(metadata_wheel(tmp_path, vendored=True), lane.PLUGIN, "0.13.370")["version"] == "0.13.370"


def test_changed_archive_or_generated_source_refused(tmp_path):
    path = tmp_path / "fixture.py"
    path.write_text("invented source")
    expected = {"fixture.py": {"sha256": lane.digest(path)}}
    lane.check_files(tmp_path, expected)
    path.write_text("invented mutation")
    with pytest.raises(ValueError, match="source/output mismatch"):
        lane.check_files(tmp_path, expected)


@pytest.mark.parametrize("system,machine,libc,version,os_id,os_version", [
    ("Linux", "x86_64", "glibc", "2.39", "ubuntu", "24.04"),
    ("Linux", "aarch64", "glibc", "2.43", "ubuntu", "26.04"),
    ("Linux", "x86_64", "musl", "1.2", "ubuntu", "26.04"),
    ("Darwin", "x86_64", "", "", "", ""),
    ("Linux", "x86_64", "glibc", "2.41", "debian", "13"),
])
def test_unsupported_hosted_platform_refused(system, machine, libc, version, os_id, os_version):
    assert lane.platform_errors(system, machine, libc, version, os_id, os_version, hosted=True)


def test_documented_hosted_platform_and_separate_local_platform():
    assert lane.platform_errors("Linux", "x86_64", "glibc", "2.43", "ubuntu", "26.04", hosted=True) == []
    assert lane.platform_errors("Linux", "x86_64", "glibc", "2.41", "debian", "13") == []


def guard_env():
    return {"PATH": os.defpath, "REPOSITORY_PRIVATE": "false", "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_ARCH": "X64", "ImageOS": "ubuntu26", "ImageVersion": "invented-image",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40,
            "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/feature",
            "GITHUB_HEAD_REF": "", "GITHUB_BASE_REF": ""}


@pytest.mark.parametrize("key,value,reason", [
    ("REPOSITORY_PRIVATE", "true", "repository"), ("REPOSITORY_PRIVATE", "", "repository"),
    ("RUNNER_ENVIRONMENT", "self-hosted", "runner"), ("RUNNER_ARCH", "ARM64", "architecture"),
    ("ImageOS", "", "image"),
    ("GITHUB_RUN_ATTEMPT", "2", "attempt"), ("GITHUB_RUN_ATTEMPT", "", "attempt"),
    ("GITHUB_SHA", "bad", "source"), ("GITHUB_EVENT_NAME", "schedule", "event"),
    ("GITHUB_REF", "refs/tags/v1", "event"),
    ("GITHUB_REF", "refs/heads/ci/issue25-harness-repeat", "diagnostic-ref"),
])
def test_actual_precheckout_guard_refuses(key, value, reason):
    steps = workflow()["jobs"]["exact-ha202610"]["steps"]
    assert "uses" not in steps[0]
    env = guard_env()
    env[key] = value
    result = subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", steps[0]["run"]],
                            env=env, text=True, capture_output=True, check=False, timeout=5)
    assert result.returncode != 0
    assert result.stderr.strip() == "HA202610_REFUSED=" + reason


@pytest.mark.parametrize("os_id,os_version,expected", [("ubuntu", "26.04", 0), ("ubuntu", "24.04", 1), ("debian", "13", 1)])
def test_guard_os_control_with_explicit_invented_os_file(tmp_path, os_id, os_version, expected):
    guard = workflow()["jobs"]["exact-ha202610"]["steps"][0]["run"]
    assert guard.count(". /etc/os-release") == 1
    fixture = tmp_path / "invented-os-release"
    fixture.write_text(f"ID={os_id}\nVERSION_ID={os_version}\n")
    # This substitution is a unit fixture, never a claim about this execution host.
    guard = guard.replace(". /etc/os-release", '. "$INVENTED_OS_FILE"')
    result = subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", guard],
                            env={**guard_env(), "INVENTED_OS_FILE": str(fixture)},
                            text=True, capture_output=True, check=False, timeout=5)
    assert result.returncode == expected
    if expected:
        assert result.stderr.strip() == "HA202610_REFUSED=os"


def phase_fixture():
    return ('COLLECTED ["invented::test"]\n' + "".join(
        "PHASE " + json.dumps({"node": "invented::test", "phase": p, "outcome": "passed"}) + "\n"
        for p in ("setup", "call", "teardown")) +
        'PHASES_COMPLETE {"classes": {}, "errors": [], "exit": 0}\nPHASES_VALID=true\n')


def test_complete_ordinary_phase_evidence():
    assert lane.phase_log_errors(phase_fixture(), "0\n") == []
    assert lane.phase_log_errors(phase_fixture().replace("PHASE {", ".PHASE {"), "0\n") == []


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "failed", "skipped", "exit", "completion", "boolean-exit", "malformed"])
def test_incomplete_or_failed_phase_evidence_refused(mutation):
    raw, status = phase_fixture(), "0\n"
    if mutation == "missing":
        raw = "\n".join(line for line in raw.splitlines() if '"teardown"' not in line)
    elif mutation == "duplicate":
        raw += raw.splitlines()[1] + "\n"
    elif mutation in ("failed", "skipped"):
        raw = raw.replace('"passed"', '"' + mutation + '"', 1)
    elif mutation == "exit":
        status = "1\n"
    elif mutation == "boolean-exit":
        raw = raw.replace('"exit": 0', '"exit": false')
    elif mutation == "completion":
        raw = raw.replace("PHASES_VALID=true", "PHASES_VALID=false")
    else:
        raw = raw.replace('COLLECTED ["invented::test"]', 'COLLECTED invalid')
    assert lane.phase_log_errors(raw, status)


def test_additional_workflow_permissions_and_ordinary_execution():
    wf = workflow()
    assert wf["permissions"] == {"contents": "read"}
    assert wf["on"]["push"] == {"branches": ["**"]}
    job = wf["jobs"]["exact-ha202610"]
    assert job["runs-on"] == "ubuntu-26.04"
    assert job["timeout-minutes"] == "25"
    assert "permissions" not in job and "environment" not in job and "secrets" not in job
    actions = [s for s in job["steps"] if "uses" in s]
    assert actions == [{"uses": "actions/checkout@v4", "with": {"persist-credentials": "false"}}]
    assert job["steps"][-1]["if"] == "always()"
    script = (ROOT / "tools/ci-ha202610.sh").read_text()
    assert "args=(180s tests/)" in script
    assert "-p tools.pytest_phases" in script
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in script
    assert "--no-deps" not in (ROOT / "tools/ha202610.py").read_text()


def test_python_wrapper_rebinds_sanitized_children_without_python_substitution(tmp_path):
    root = tmp_path / "invented owned root"
    root.mkdir()
    wrapper = lane.python_wrapper(root)
    assert " -B -X " in wrapper
    assert "LD_LIBRARY_PATH=" in wrapper
    for name in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        assert "export " + name + "=" in wrapper
    # Exercise the actual shell prefix; the interpreter itself is not faked.
    prefix, command = wrapper.split("exec -a ", 1)
    assert str(root / "python26/bin/python3.14") in command
    result = subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c",
                             prefix + 'printf "%s\\n" "$HOME" "$TMPDIR" "$LD_LIBRARY_PATH"'],
                            env={"PATH": os.defpath}, capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [str(root / "home"), str(root / "tmp"), str(root / "python26/lib")]


def test_missing_export_receipts_and_logs_fail(tmp_path, capsys):
    with pytest.raises(ValueError, match="incomplete evidence export"):
        lane.export(tmp_path, lane.load_bundle())
    assert "invalid setup receipt binding" in capsys.readouterr().out


def test_changed_actual_archive_is_refused_before_inventory(tmp_path):
    bundle = lane.load_bundle()
    actual = [{"name": r["name"], "path": str(tmp_path / "invented.whl"), "sha256": "0" * 64,
               "version": r["version"], "kind": r["kind"]} for r in bundle["artifacts"]]
    actual.append({"name": lane.PLUGIN})
    (tmp_path / "invented.whl").write_text("invented archive")
    with pytest.raises(ValueError, match="actual archive binding differs"):
        lane.validate_actual_artifacts(tmp_path, bundle, actual)


def test_foreign_interpreter_refused_before_install(tmp_path):
    wrapper = tmp_path / "venv/bin/python"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\nexec /invented/private/python \"$@\"\n")
    with pytest.raises(ValueError, match="wrong owned Python wrapper"):
        lane.check_runtime_wrapper(tmp_path, lane.load_bundle())
