"""Phase evidence stays whole when stdout and stderr go into one ``-s`` log."""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("invented_phases", ROOT / "tools/pytest_phases.py")
phases = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(phases)


def phase_log_errors(raw):
    """Read a log as a person checks it: one collection, every test's three phases
    passed, one completion with exit 0 and no errors, and PHASES_VALID=true."""
    errors = []
    collected, complete, valid, reports = [], [], [], {}
    try:
        for line in raw.splitlines():
            # pytest progress marks share the teardown line with the phase helper.
            line = re.sub(r"^[.FEsxX]+(?=PHASE )", "", line)
            if line.startswith("COLLECTED "):
                collected.append(json.loads(line[10:]))
            elif line.startswith("PHASES_COMPLETE "):
                complete.append(json.loads(line[16:]))
            elif line.startswith("PHASES_VALID="):
                valid.append(line)
            elif line.startswith("PHASE "):
                phase = json.loads(line[6:])
                reports.setdefault(phase["node"], []).append((phase["phase"], phase["outcome"]))
        if (len(collected) != 1 or not isinstance(collected[0], list) or not collected[0] or
                not all(isinstance(node, str) for node in collected[0]) or
                len(set(collected[0])) != len(collected[0])):
            errors.append("missing or repeated collection")
        elif set(reports) != set(collected[0]) or any(
                reports.get(node) != [(p, "passed") for p in ("setup", "call", "teardown")]
                for node in collected[0]):
            errors.append("missing, repeated or unsuccessful phases")
        if (complete != [{"classes": {}, "errors": [], "exit": 0}] or
                type(complete[0]["exit"]) is not int or valid != ["PHASES_VALID=true"]):
            errors.append("incomplete phase completion")
    except (ValueError, KeyError, TypeError):
        errors.append("malformed phase evidence")
    return errors

STDERR_LINE = "WARNING:invented.loader:invented stderr record"
CHILD_TEST = '''import sys

import pytest


@pytest.mark.parametrize("case", [f"{n:05d}-" + "x" * 1000 for n in range(CASES)], ids=str)
def test_invented_case(case):
    if CONTROL and case.startswith("LAST-"):
        # Control, last so it cannot flush the collection line early: an ordinary
        # print longer than the buffer keeps its newline back.
        print("CONTROL " + "y" * WIDTH)
    # Like Home Assistant logging: straight to stderr while stdout is still buffered.
    sys.stderr.write("STDERR_LINE\\n")
'''


@pytest.mark.parametrize("conftest", ["repository", "absent"])
def test_stderr_never_splits_phase_evidence(tmp_path, conftest):
    # The real helper run with -s, stdout and stderr redirected to one file, as a
    # local run may be. "repository" adds the suite conftest (tests/); "absent" runs
    # without one. All test data is invented.
    (tmp_path / "pytest_phases.py").write_bytes((ROOT / "tools/pytest_phases.py").read_bytes())
    if conftest == "repository":
        (tmp_path / "conftest.py").write_bytes((ROOT / "tests/conftest.py").read_bytes())
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    log = tmp_path / "suite.log"
    control = conftest == "absent"
    with log.open("wb") as stream:
        # Python buffers a file's stdout by st_blksize (3.12, 3.13) or at least 128 KiB
        # (3.14); the collection line must outgrow that buffer on every lane. The child's
        # ids=str puts each 1 KB value in its node id: pytest 9.2's automatic ids drop any
        # string over 100 characters, which leaves the line far smaller than the buffer.
        buffer = max(io.DEFAULT_BUFFER_SIZE, os.fstat(stream.fileno()).st_blksize)
        cases = 2 * buffer // 1000 + 1
        (tmp_path / "test_invented_split.py").write_text(
            CHILD_TEST.replace("CASES", str(cases)).replace("STDERR_LINE", STDERR_LINE)
            .replace("CONTROL and", str(control) + " and").replace("WIDTH", str(2 * buffer))
            .replace("LAST", f"{cases - 1:05d}"))
        result = subprocess.run([sys.executable, "-m", "pytest", "-p", "pytest_phases",
                                 "test_invented_split.py", "-q", "-s", "-p", "no:cacheprovider"],
                                cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=subprocess.STDOUT, check=False, timeout=60)
    raw = log.read_text()
    assert result.returncode == 0, raw
    lines = raw.splitlines()
    collected = [line for line in lines if "COLLECTED [" in line]
    assert len(collected) == 1 and len(collected[0]) > 2 * buffer
    assert phase_log_errors(raw) == []
    if control:
        # The run reproduces the hazard: the print control is split by the next record.
        assert [line for line in lines if "CONTROL " in line] == ["CONTROL " + "y" * 2 * buffer + STDERR_LINE]
        assert lines.count(STDERR_LINE) == cases - 1
    else:
        assert lines.count(STDERR_LINE) == cases


class Recorder:
    """A plain stdout stand-in: no ``reconfigure``, no buffer, every call recorded."""

    def __init__(self):
        self.calls = []

    def write(self, text):
        self.calls.append(("write", text))
        return len(text)

    def flush(self):
        self.calls.append(("flush",))


def test_each_evidence_line_is_one_flushed_write(monkeypatch):
    # One write per line leaves no gap for another thread's stderr record inside it.
    stdout = Recorder()
    record = phases.Phases(distributed=False, terminal=None)
    session = SimpleNamespace(items=[SimpleNamespace(nodeid="invented::test_case")], exitstatus=0)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", stdout)
        record.pytest_collection_finish(session)
        for when in ("setup", "call", "teardown"):
            record.pytest_runtest_logreport(SimpleNamespace(
                nodeid="invented::test_case", when=when, outcome="passed", failed=False, skipped=False,
                duration=0.0))
        record.pytest_sessionfinish(session)
    writes = [call[1] for call in stdout.calls if call[0] == "write"]
    assert [line.split(" ", 1)[0].split("=", 1)[0].strip() for line in writes] == [
        "COLLECTED", "PHASE", "PHASE", "PHASE", "", "PHASES_COMPLETE", "PHASES_VALID"]
    assert all(line.endswith("\n") and line.count("\n") == 1 for line in writes)
    assert stdout.calls == [call for text in writes for call in (("write", text), ("flush",))]
    assert phase_log_errors("".join(writes)) == []


XDIST_TESTS = {
    "test_invented_a.py": "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n",
    "test_invented_b.py": "def test_three():\n    pass\n\n\ndef test_four():\n    pass\n",
}
SPECIAL = {
    "skip": "import pytest\n\n\ndef test_special():\n    pytest.skip('invented')\n",
    "xfail": "import pytest\n\n\n@pytest.mark.xfail\ndef test_special():\n    assert False\n",
}


def _child(tmp_path, files, *args):
    """The real helper in a child pytest over invented tests, as the lanes run it."""
    (tmp_path / "pytest_phases.py").write_bytes((ROOT / "tools/pytest_phases.py").read_bytes())
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "xdist.plugin", "-p", "pytest_phases",
                             "-q", "-p", "no:cacheprovider", *args],
                            cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, check=False, timeout=120)
    return result.returncode, result.stdout + result.stderr


def _completion(raw):
    lines = [line for line in raw.splitlines() if line.startswith("PHASES_COMPLETE ")]
    assert len(lines) == 1, raw
    return json.loads(lines[0][16:])


WORKERS = {"serial": ["-n", "0"], "xdist": ["-n", "2", "--dist", "loadfile"]}


@pytest.mark.parametrize("workers", WORKERS)
def test_every_test_is_accounted_for_once(tmp_path, workers):
    code, raw = _child(tmp_path, XDIST_TESTS, *WORKERS[workers])
    assert code == 0, raw
    assert phase_log_errors(raw) == []
    collected = json.loads(next(line for line in raw.splitlines() if line.startswith("COLLECTED "))[10:])
    assert sorted(collected) == sorted(f"{name}::test_{n}" for name, ns in (
        ("test_invented_a.py", ("one", "two")), ("test_invented_b.py", ("three", "four"))) for n in ns)


@pytest.mark.parametrize("workers", WORKERS)
@pytest.mark.parametrize("special", SPECIAL)
def test_a_skip_or_xfail_fails_the_run(tmp_path, workers, special):
    code, raw = _child(tmp_path, {**XDIST_TESTS, "test_special.py": SPECIAL[special]}, *WORKERS[workers])
    assert code == 1, raw
    assert _completion(raw)["errors"] == ["test_special.py::test_special: skip/xfail/xpass"]
    assert "PHASES_VALID=false" in raw.splitlines()


@pytest.mark.parametrize("workers", WORKERS)
def test_a_deselected_test_fails_the_run(tmp_path, workers):
    code, raw = _child(tmp_path, XDIST_TESTS, *WORKERS[workers], "-k", "not three")
    assert code == 1, raw
    assert _completion(raw)["errors"] == ["deselected tests"]
    assert "PHASES_VALID=false" in raw.splitlines()
