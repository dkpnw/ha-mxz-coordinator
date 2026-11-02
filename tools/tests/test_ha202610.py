"""Additional lane controls, with invented metadata/platform/phase fixtures only."""

import copy
import importlib.util
import json
import os
import re
import shutil
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
    assert job["needs"] == "approval" and job["if"] == "${{ !cancelled() }}"
    assert set(wf["jobs"]) == {"approval", "exact-ha202610"}
    assert wf["jobs"]["approval"]["permissions"] == {"contents": "read", "actions": "read"}
    actions = [s for s in job["steps"] if "uses" in s]
    assert actions == [{"uses": "actions/checkout@v4", "with": {"persist-credentials": "false"}}]
    assert job["steps"][-1]["if"] == "always()"
    script = (ROOT / "tools/ci-ha202610.sh").read_text()
    assert "args=(300s tests/)" in script
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


FRESH_BOOT_STEP = {"name": "Full suite on a fresh-boot clock", "if": "${{ !cancelled() }}",
                   "run": "bash tools/ci-ha202610.sh fakeboot"}


def test_fresh_boot_pass_follows_the_suite_and_precedes_the_export():
    steps = workflow()["jobs"]["exact-ha202610"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.count(FRESH_BOOT_STEP["name"]) == 1
    fresh = names.index(FRESH_BOOT_STEP["name"])
    assert names[fresh - 1] == "Full retained suite with original phases"
    assert steps[fresh - 1]["run"] == "bash tools/ci-ha202610.sh suite"
    assert steps[fresh] == FRESH_BOOT_STEP
    assert fresh == len(steps) - 2 and steps[-1]["run"] == "bash tools/ci-ha202610.sh export"


def script_branch(case):
    script = (ROOT / "tools/ci-ha202610.sh").read_text()
    head = "\n  " + case + ")\n"
    assert script.count(head) == 1
    return script.split(head, 1)[1].split("\n    ;;\n", 1)[0]


def fakeboot_branch():
    return script_branch("fakeboot")


def test_fresh_boot_pass_is_the_full_suite_on_the_two_minute_clock():
    branch = fakeboot_branch()
    assert ('FAKE_UPTIME=120 timeout --signal=TERM --kill-after=5s 300s "$python" -m pytest \\\n'
            '      -p tools.fakeboot -p tools.pytest_phases tests/ -q -s -p no:cacheprovider'
            ' > "$root/fakeboot.log" 2>&1 || result=$?') in branch
    assert "printf '%s\\n' \"$result\" > \"$root/fakeboot.exit\"" in branch
    assert branch.rstrip().endswith('exit "$result"')


@pytest.mark.parametrize("status", [0, 1, 2, 124, 137])
def test_fresh_boot_exit_is_saved_and_returned(tmp_path, status):
    """Run the script's own fakeboot branch against an invented interpreter that
    reports its arguments and clock, then exits with ``status``."""
    stub = tmp_path / "invented-python"
    stub.write_text('#!/bin/bash\nprintf "ARGV %s\\n" "$*"\nprintf "FAKE_UPTIME=%s\\n" "$FAKE_UPTIME"\n'
                    f"exit {status}\n")
    stub.chmod(0o755)
    root = tmp_path / "invented-root"
    root.mkdir()
    result = subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c",
                             f'python={stub}; root={root}\n' + fakeboot_branch()],
                            env={"PATH": os.defpath}, text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == status
    assert (root / "fakeboot.exit").read_text() == f"{status}\n"
    assert (root / "fakeboot.log").read_text().splitlines() == [
        "ARGV -m pytest -p tools.fakeboot -p tools.pytest_phases tests/ -q -s -p no:cacheprovider",
        "FAKE_UPTIME=120"]
    # A short failed log is shown whole in the step; a passing one is not shown.
    assert result.stdout == ("" if status == 0 else (root / "fakeboot.log").read_text())
    assert result.stderr == ""


def test_fresh_boot_clock_line():
    assert lane.fakeboot_clock_errors("FAKEBOOT_UPTIME=120\n" + phase_fixture()) == []


@pytest.mark.parametrize("raw", ["", "FAKEBOOT_UPTIME=86400\n", "FAKEBOOT_UPTIME=120\nFAKEBOOT_UPTIME=120\n",
                                 "FAKEBOOT_UPTIME=120.0\n", "FAKEBOOT_UPTIME=120 \n"],
                         ids=["missing", "wrong", "repeated", "reformatted", "trailing"])
def test_missing_or_wrong_fresh_boot_clock_refused(raw):
    assert lane.fakeboot_clock_errors(raw + phase_fixture()) == ["missing, repeated or wrong fresh-boot clock"]


def export_errors(root, capsys):
    with pytest.raises(ValueError, match="incomplete evidence export"):
        lane.export(root, lane.load_bundle())
    line = [x for x in capsys.readouterr().out.splitlines() if x.startswith("EVIDENCE_ERRORS ")]
    assert len(line) == 1
    return json.loads(line[0][len("EVIDENCE_ERRORS "):])


@pytest.mark.parametrize("fault", ["none", "no-log", "no-exit", "exit", "phases", "clock", "oversize"])
def test_export_requires_complete_fresh_boot_evidence(tmp_path, capsys, fault):
    """Only the fresh-boot files are present, so every error that is not about a
    missing file of another step is about them."""
    log, status = "FAKEBOOT_UPTIME=120\n" + phase_fixture(), "0\n"
    if fault == "exit":
        status = "1\n"
    elif fault == "phases":
        log = log.replace("PHASES_VALID=true", "PHASES_VALID=false")
    elif fault == "clock":
        log = log.replace("FAKEBOOT_UPTIME=120", "FAKEBOOT_UPTIME=86400")
    elif fault == "oversize":
        log += "x" * (5 * 2**20)
    if fault != "no-log":
        (tmp_path / "fakeboot.log").write_text(log)
    if fault != "no-exit":
        (tmp_path / "fakeboot.exit").write_text(status)
    others = {"missing " + name for name in (
        "source.txt", "source-files.sha256", "runner.txt", "setup.log", "checks.log",
        "generated-artifact.json", "actual-artifacts.json", "install.lock", "wheel-tags.json",
        "sentinel.log", "sentinel.exit", "suite.log", "suite.exit")}
    errors = [e for e in export_errors(tmp_path, capsys)
              if e not in others and not e.startswith("invalid setup receipt ")]
    expected = {
        "none": [],
        "no-log": ["missing fakeboot.log"],
        "no-exit": ["missing fakeboot.exit"],
        "exit": ["nonzero or missing suite exit"],
        "phases": ["incomplete phase completion"],
        "clock": ["missing, repeated or wrong fresh-boot clock"],
        "oversize": ["oversize or symlink " + str(tmp_path / "fakeboot.log")],
    }[fault]
    assert errors == expected


def test_both_full_suite_passes_have_the_same_300_second_limit():
    script = (ROOT / "tools/ci-ha202610.sh").read_text()
    assert re.findall(r"args=\((\d+)s ", script) == ["60", "300"]
    assert re.findall(r"--kill-after=5s (\d+)s ", script) == ["300"]
    assert "args=(300s tests/)" in script and "180s" not in script


INVENTED_LINES = "".join(f"INVENTED LINE {i}\n" for i in range(1, 251))


def run_branch(tmp_path, case, ending, mode=None, limit=None, path=os.defpath, pad=0):
    """Run one of the script's own branches against an invented interpreter that
    writes its arguments and 250 numbered lines (each ``pad`` x's longer), then
    ends with ``ending``."""
    stub = tmp_path / "invented-python"
    stub.write_text('#!/bin/bash\nprintf "ARGV %s\\n" "$*"\n'
                    f'for ((i = 1; i <= 250; i++)); do printf "INVENTED LINE %s{"x" * pad}\\n" "$i"; done\n'
                    + ending + "\n")
    stub.chmod(0o755)
    root = tmp_path / "invented-root"
    root.mkdir()
    branch = script_branch(case)
    if limit:
        # A unit fixture: a short limit stands in for 300 s so a real timeout fires.
        assert branch.count("300s") == 1
        branch = branch.replace("300s", limit)
    prefix = f"python={stub}; root={root}\n" + (f"mode={mode}\n" if mode else "")
    result = subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", prefix + branch],
                            env={"PATH": path}, text=True, capture_output=True, check=False, timeout=30)
    return result, root


FULL_PASSES = [("sentinel|suite", "suite", "suite"), ("fakeboot", None, "fakeboot")]


@pytest.mark.parametrize("case,mode,name", FULL_PASSES, ids=["suite", "fakeboot"])
@pytest.mark.parametrize("ending,status", [("exit 1", 1), ("exit 2", 2), ("exec sleep 30", 124), ("exit 137", 137)],
                         ids=["failed", "interrupted", "timeout", "killed"])
def test_failed_or_timed_out_pass_prints_the_tail_of_its_own_log(tmp_path, case, mode, name, ending, status):
    result, root = run_branch(tmp_path, case, ending, mode, limit="1s" if status == 124 else None)
    assert result.returncode == status
    assert (root / (name + ".exit")).read_text() == f"{status}\n"
    log = (root / (name + ".log")).read_text()
    assert log.startswith("ARGV -m pytest ") and log.endswith(INVENTED_LINES)
    # Exactly the last 200 of the 251 lines, and nothing else on either stream.
    assert result.stdout == "".join(f"INVENTED LINE {i}\n" for i in range(51, 251))
    assert result.stderr == ""


@pytest.mark.parametrize("case,mode,name", FULL_PASSES, ids=["suite", "fakeboot"])
def test_a_tail_of_long_lines_stops_at_64_kib(tmp_path, case, mode, name):
    """200 lines of 1 KiB each: only the last 64 KiB of them, still the log's end."""
    result, root = run_branch(tmp_path, case, "exit 1", mode, pad=1024)
    assert result.returncode == 1 and (root / (name + ".exit")).read_text() == "1\n"
    log = (root / (name + ".log")).read_bytes()
    assert len(log) > 200 * 1024
    assert result.stdout.encode() == log[-65536:]
    assert result.stderr == ""


@pytest.mark.parametrize("case,mode,name", FULL_PASSES, ids=["suite", "fakeboot"])
def test_a_passing_pass_prints_nothing(tmp_path, case, mode, name):
    result, root = run_branch(tmp_path, case, "exit 0", mode)
    assert result.returncode == 0 and (root / (name + ".exit")).read_text() == "0\n"
    assert (root / (name + ".log")).read_text().endswith(INVENTED_LINES)
    assert result.stdout == "" and result.stderr == ""


def test_a_failed_sentinel_prints_nothing(tmp_path):
    result, root = run_branch(tmp_path, "sentinel|suite", "exit 1", "sentinel")
    assert result.returncode == 1 and (root / "sentinel.exit").read_text() == "1\n"
    assert (root / "sentinel.log").read_text().endswith(INVENTED_LINES)
    assert result.stdout == "" and result.stderr == ""


@pytest.mark.parametrize("case,mode,name", FULL_PASSES, ids=["suite", "fakeboot"])
def test_a_failing_tail_cannot_change_the_exit(tmp_path, case, mode, name):
    tools = tmp_path / "invented-bin"
    tools.mkdir()
    # Only the shell and timeout are on PATH, so the tail itself fails.
    for tool in ("bash", "timeout"):
        (tools / tool).symlink_to(shutil.which(tool, path=os.defpath))
    result, root = run_branch(tmp_path, case, "exit 1", mode, path=str(tools))
    assert result.returncode == 1 and (root / (name + ".exit")).read_text() == "1\n"
    assert "tail" in result.stderr and result.stdout == ""


def uploads(wf):
    """As in ci.yml's contract, no job of this workflow uploads an artifact."""
    return [s for job in wf["jobs"].values() for s in job.get("steps", []) if "upload-artifact" in s.get("uses", "")]


def test_no_artifact_upload_as_in_the_original_ci():
    assert uploads(workflow()) == []
    assert "upload-artifact" not in (ROOT / ".github/workflows/ci-ha2026.10.yml").read_text()


@pytest.mark.parametrize("job,uses", [("exact-ha202610", "actions/upload-artifact@v4"),
                                      ("exact-ha202610", "actions/upload-artifact@v7"),
                                      ("approval", "actions/upload-artifact@v4")])
def test_an_added_upload_is_refused(job, uses):
    wf = copy.deepcopy(workflow())
    wf["jobs"][job]["steps"].append({"name": "Invented upload", "if": "always()", "uses": uses})
    assert len(uploads(wf)) == 1
