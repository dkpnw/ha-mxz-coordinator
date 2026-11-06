"""Additional lane controls, with invented metadata/platform/phase fixtures only."""

import base64
import copy
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
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
    assert len(bundle["artifacts"]) == 159
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
    # Both 600 s passes, the 60 s sentinel, then setup, checks and export with margin.
    assert job["timeout-minutes"] == "35"
    assert "permissions" not in job and "environment" not in job and "secrets" not in job
    assert job["needs"] == "approval" and job["if"] == "${{ !cancelled() }}"
    assert set(wf["jobs"]) == {"approval", "exact-ha202610"}
    assert wf["jobs"]["approval"]["permissions"] == {"contents": "read", "actions": "read"}
    actions = [s for s in job["steps"] if "uses" in s]
    assert actions == [{"uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                        "with": {"persist-credentials": "false"}}]
    assert job["steps"][-1]["if"] == "always()"
    script = (ROOT / "tools/ci-ha202610.sh").read_text()
    assert "args=(600s tests/)" in script
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
    assert ('FAKE_UPTIME=120 timeout --signal=TERM --kill-after=5s 600s "$python" -m pytest \\\n'
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
        log += "x" * (32 * 2**20)
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


FIVE_MIB, CAP = 5242880, 33554432
SETUP_LABELS = ("create-venv", "bootstrap-pip", "build-tools", "plugin-identity", "core-identity",
                "core-tags", "core-tag-target", "stock-generation", "stock-package", "platform-wheel-tags",
                "source-wheels", "install", "pip-check", "binding", "isolated-binding")
# Read from the README, so the recipe people are told to use is the one tested.
README = (ROOT / "requirements/ha2026.10/README.md").read_text()


def write_receipt(root, label, rc=0):
    folder = root / "logs" / label
    folder.mkdir(parents=True)
    (folder / "stdout").write_text(f"invented {label} output\n")
    (folder / "stderr").write_text("")
    write = {name: {"bytes": (folder / name).stat().st_size,
                    "sha256": hashlib.sha256((folder / name).read_bytes()).hexdigest()}
             for name in ("stdout", "stderr")}
    (folder / "receipt.json").write_text(json.dumps({"rc": rc, **write}))


def pass_log(size, fakeboot=False, middle=""):
    """A valid log of exactly ``size`` bytes, with its padding (and ``middle``) far from
    both ends. Compressible, as real logs are; each line is unique."""
    start = "FAKEBOOT_UPTIME=120\n" if fakeboot else ""
    phases = phase_fixture()
    room = size - len(start) - len(phases) - len(middle)
    # 54 bytes per padding line, with a shorter line to land on ``size`` exactly.
    lines = room // 54
    padding = "".join(f"{i:012d} invented warning about a test, padding only.....\n"[-54:] for i in range(lines))
    padding += "z" * (room - len(padding) - 1) + "\n" if room > len(padding) else ""
    half = padding.rfind("\n", 0, len(padding) // 2) + 1
    log = start + padding[:half] + middle + padding[half:] + phases
    assert len(log) == size
    return log


def full_evidence(root, suite=None, fakeboot=None):
    """Everything a complete run leaves, so a green export is a real one."""
    for label in SETUP_LABELS:
        write_receipt(root, label)
    for name in ("source.txt", "source-files.sha256", "runner.txt", "setup.log",
                 "generated-artifact.json", "actual-artifacts.json", "install.lock", "wheel-tags.json"):
        (root / name).write_text(f"invented {name}\n")
    (root / "checks.log").write_text(phase_fixture())
    (root / "sentinel.log").write_text(phase_fixture())
    (root / "suite.log").write_text(suite or phase_fixture())
    (root / "fakeboot.log").write_text(fakeboot or "FAKEBOOT_UPTIME=120\n" + phase_fixture())
    for name in ("sentinel", "suite", "fakeboot"):
        (root / (name + ".exit")).write_text("0\n")


def run_export(root):
    """The real command line, as ci-ha202610.sh runs it; exit code and exact output."""
    return subprocess.run([sys.executable, "-B", str(ROOT / "tools/ha202610.py"), "export", str(root)],
                          text=True, capture_output=True, check=False, timeout=60)


def errors_of(stdout):
    line = [x for x in stdout.splitlines() if x.startswith("EVIDENCE_ERRORS ")]
    assert len(line) == 1
    return json.loads(line[0][len("EVIDENCE_ERRORS "):])


def recover(stdout, name, tmp_path):
    """The README's own shell recipe, on the export as a job log shows it: timestamped.
    gunzip, base64 and sha256sum are the oracle, not the code under test."""
    job_log = tmp_path / "job.log"
    job_log.write_text("".join("2026-11-06T22:00:28.6513921Z " + line + "\n" for line in stdout.splitlines()))
    recipe = re.search(r"```bash\n(sed -E [^`]*?)```", README).group(1).replace("suite", name.removesuffix(".log"))
    result = subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", recipe], cwd=tmp_path,
                            text=True, capture_output=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr
    digest, recovered = result.stdout.split()
    return digest, (tmp_path / recovered).read_bytes()


@pytest.mark.parametrize("size", [FIVE_MIB + 1, 6 * 2**20, CAP], ids=["five-mib-plus-one", "six-mib", "cap"])
def test_a_large_log_passes_and_comes_back_whole(tmp_path, size):
    root = tmp_path / "root"
    root.mkdir()
    suite, fakeboot = pass_log(size), pass_log(size, fakeboot=True)
    full_evidence(root, suite, fakeboot)
    result = run_export(root)
    assert result.returncode == 0, result.stderr
    assert errors_of(result.stdout) == []
    out = tmp_path / "out"
    out.mkdir()
    for name, log in (("suite.log", suite), ("fakeboot.log", fakeboot)):
        raw = log.encode()
        expected = hashlib.sha256(raw).hexdigest()
        # The hash and size are of the uncompressed bytes.
        assert f"FILE {name} BYTES {size} SHA256 {expected}\n" in result.stdout
        digest, recovered = recover(result.stdout, name, out)
        assert digest == expected and recovered == raw
    # The whole text is not printed, and what is printed is under the old limit per log.
    assert suite not in result.stdout and len(result.stdout) < 2 * FIVE_MIB


def test_truncation_is_marked_with_the_exact_bytes_left_out(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    log = pass_log(FIVE_MIB + 1, middle="INVENTED MIDDLE OF THE LOG\n")
    full_evidence(root, log)
    out = run_export(root).stdout
    block = out[out.index("FILE suite.log "):out.index("GZIP_BASE64_BEGIN suite.log ")]
    omitted = FIVE_MIB + 1 - 16384 - 65536
    assert f"TRUNCATED_VIEW suite.log HEAD_BYTES 16384 OMITTED_BYTES {omitted} TAIL_BYTES 65536\n" in block
    assert f"TRUNCATED {omitted} BYTES OF suite.log; THE COMPLETE LOG IS THE GZIP_BASE64 BLOCK BELOW\n" in block
    head, tail = block.split(f"\nTRUNCATED {omitted} BYTES OF suite.log", 1)
    assert head.endswith(log[:16384])
    assert tail.split("\n", 1)[1].rstrip("\n") == log[-65536:].rstrip("\n")
    assert "INVENTED MIDDLE OF THE LOG" not in block
    assert "PHASES_VALID=true" in tail
    # Exactly one view and one block for this log.
    assert out.count("TRUNCATED_VIEW suite.log") == 1 and out.count("GZIP_BASE64_END suite.log\n") == 1
    assert "TRUNCATED_VIEW fakeboot.log" not in out


def test_the_gzip_block_is_declared_and_repeatable(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    log = pass_log(FIVE_MIB + 1)
    full_evidence(root, log)
    first = run_export(root).stdout
    assert first == run_export(root).stdout
    begin = re.search(r"^GZIP_BASE64_BEGIN suite\.log GZIP_BYTES (\d+) GZIP_SHA256 ([0-9a-f]{64})$", first, re.MULTILINE)
    body = first[begin.end():first.index("GZIP_BASE64_END suite.log")]
    # No line a job log could be asked to treat as a command, and none over 4 KiB.
    assert all(re.fullmatch(r"[A-Za-z0-9+/=]{1,4096}", line) for line in body.split())
    packed = base64.b64decode("".join(body.split()), validate=True)
    assert len(packed) == int(begin.group(1)) and hashlib.sha256(packed).hexdigest() == begin.group(2)
    assert gzip.decompress(packed) == log.encode()


def test_a_view_that_cuts_a_character_in_two_still_exports_it_whole(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    # After one ASCII byte, every even offset falls inside a two-byte character.
    log = "x" + "\u00e9" * (3 * 2**20) + "\n" + phase_fixture()
    full_evidence(root, log)
    result = run_export(root)
    assert result.returncode == 0 and errors_of(result.stdout) == []
    assert "\ufffd" in result.stdout
    digest, recovered = recover(result.stdout, "suite.log", tmp_path)
    assert recovered == log.encode() and digest == hashlib.sha256(log.encode()).hexdigest()


def test_a_log_at_exactly_five_mib_is_still_printed_as_text(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    log = pass_log(FIVE_MIB)
    full_evidence(root, log)
    result = run_export(root)
    assert result.returncode == 0 and errors_of(result.stdout) == []
    assert log in result.stdout and "TRUNCATED" not in result.stdout and "GZIP_BASE64" not in result.stdout


@pytest.mark.parametrize("name", ["suite.log", "fakeboot.log"])
def test_a_log_past_the_cap_fails_and_prints_nothing_of_itself(tmp_path, name):
    root = tmp_path / "root"
    root.mkdir()
    full_evidence(root)
    (root / name).write_text(pass_log(CAP + 1, fakeboot=name == "fakeboot.log"))
    result = run_export(root)
    assert result.returncode == 1 and "incomplete evidence export" in result.stderr
    assert errors_of(result.stdout) == ["oversize or symlink " + str((root / name).resolve())]
    assert f"FILE {name}" not in result.stdout and f"GZIP_BASE64_BEGIN {name}" not in result.stdout


def test_a_log_that_will_not_compress_enough_fails(tmp_path):
    """Random text: well under the cap, over the 5 MiB of text even gzipped."""
    root = tmp_path / "root"
    root.mkdir()
    full_evidence(root)
    noise = base64.b64encode(os.urandom(6 * 2**20)).decode()
    (root / "suite.log").write_text(noise + "\n" + phase_fixture())
    assert FIVE_MIB < (root / "suite.log").stat().st_size < CAP
    result = run_export(root)
    assert result.returncode == 1
    assert errors_of(result.stdout) == ["oversize gzipped export " + str(root / "suite.log")]
    assert "FILE suite.log" not in result.stdout and "GZIP_BASE64" not in result.stdout


@pytest.mark.parametrize("fault,expected", [
    # Each fault sits in the omitted middle of the log or its exit, never in the view.
    ("failed-phase", "missing, repeated or unsuccessful phases"),
    ("repeated-collection", "missing or repeated collection"),
    ("exit", "nonzero or missing suite exit"),
    ("no-exit", "missing suite.exit"),
])
def test_a_large_failed_log_still_fails_the_export(tmp_path, fault, expected):
    root = tmp_path / "root"
    root.mkdir()
    middle = {"failed-phase": 'PHASE {"node": "invented::test", "phase": "call", "outcome": "failed"}\n',
              "repeated-collection": 'COLLECTED ["invented::test"]\n'}.get(fault, "")
    full_evidence(root, pass_log(6 * 2**20, middle=middle))
    if fault == "exit":
        (root / "suite.exit").write_text("1\n")
    elif fault == "no-exit":
        (root / "suite.exit").unlink()
    result = run_export(root)
    assert result.returncode == 1 and "incomplete evidence export" in result.stderr
    assert expected in errors_of(result.stdout)
    # The failed log is still exported, whole.
    assert "GZIP_BASE64_END suite.log\n" in result.stdout


def test_a_large_fresh_boot_log_without_its_clock_fails(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    full_evidence(root, fakeboot=pass_log(6 * 2**20))
    result = run_export(root)
    assert result.returncode == 1
    assert errors_of(result.stdout) == ["missing, repeated or wrong fresh-boot clock"]


@pytest.mark.parametrize("name", ["suite.log", "fakeboot.log"])
def test_a_missing_pass_log_fails(tmp_path, name):
    root = tmp_path / "root"
    root.mkdir()
    full_evidence(root)
    (root / name).unlink()
    result = run_export(root)
    assert result.returncode == 1
    assert errors_of(result.stdout) == ["missing " + name]


def test_a_symlinked_large_log_is_refused_though_the_target_is_valid(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    full_evidence(root)
    target = tmp_path / "elsewhere.log"
    target.write_text(pass_log(6 * 2**20))
    (root / "suite.log").unlink()
    (root / "suite.log").symlink_to(target)
    result = run_export(root)
    assert result.returncode == 1
    assert errors_of(result.stdout) == ["oversize or symlink " + str(root / "suite.log")]
    assert "GZIP_BASE64" not in result.stdout


@pytest.mark.parametrize("name,limit", [("checks.log", 2**20), ("sentinel.log", 2**20), ("install.lock", 6 * 2**20)])
def test_other_evidence_keeps_its_own_limit_and_is_not_gzipped(tmp_path, name, limit):
    root = tmp_path / "root"
    root.mkdir()
    full_evidence(root)
    # Valid phases, so the only possible complaint is the size.
    (root / name).write_text("x" * (limit - len(phase_fixture())) + "\n" + phase_fixture())
    result = run_export(root)
    assert result.returncode == 1
    assert errors_of(result.stdout) == ["oversize or symlink " + str(root / name)]
    assert "GZIP_BASE64" not in result.stdout


def test_other_evidence_over_five_mib_is_still_printed_as_text(tmp_path):
    """Within its own 6 MiB limit; only the two full passes ever go in gzipped."""
    root = tmp_path / "root"
    root.mkdir()
    full_evidence(root)
    text = "invented lock line\n" * (FIVE_MIB // 19 + 1)
    (root / "install.lock").write_text(text)
    assert FIVE_MIB < len(text) < 6 * 2**20
    result = run_export(root)
    assert result.returncode == 0 and errors_of(result.stdout) == []
    assert text in result.stdout and "GZIP_BASE64" not in result.stdout


def test_a_failed_setup_receipt_is_still_exported(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    full_evidence(root, pass_log(6 * 2**20))
    shutil.rmtree(root / "logs/install")
    write_receipt(root, "install", rc=1)
    result = run_export(root)
    assert result.returncode == 1
    assert errors_of(result.stdout) == ["invalid setup receipt install: nonzero setup command"]
    assert "FILE logs/install/receipt.json " in result.stdout
    assert "invented install output" in result.stdout


def test_suite_pass_and_fresh_boot_pass_each_have_a_600_second_limit():
    script = (ROOT / "tools/ci-ha202610.sh").read_text()
    assert re.findall(r"args=\((\d+)s ", script) == ["60", "600"]
    assert re.findall(r"--kill-after=5s (\d+)s ", script) == ["600"]
    assert "args=(600s tests/)" in script
    assert "300s" not in script and "180s" not in script


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
        # A unit fixture: a short limit stands in for the branch's own limit (600 s
        # for the fresh-boot pass and for the suite) so a real timeout fires.
        own = "600s"
        assert branch.count(own) == 1
        branch = branch.replace(own, limit)
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
