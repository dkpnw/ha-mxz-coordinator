"""Phase evidence stays whole when CI writes stdout and stderr into one ``-s`` log."""

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ha202610", ROOT / "tools/ha202610.py")
lane = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lane)
SPEC = importlib.util.spec_from_file_location("invented_phases", ROOT / "tools/pytest_phases.py")
phases = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(phases)

STDERR_LINE = "WARNING:invented.loader:invented stderr record"
CHILD_TEST = '''import sys

import pytest


@pytest.mark.parametrize("case", [f"{n:05d}-" + "x" * 1000 for n in range(CASES)])
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
    # The real helper run exactly as CI does: -s, stdout and stderr redirected to one
    # file. "repository" adds the suite conftest (tests/); "absent" is how tools/tests
    # and tools/issue25 run. All test data is invented.
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
        # (3.14); the collection line must outgrow that buffer on every lane.
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
    assert lane.phase_log_errors(raw, "0\n") == []
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
    config = SimpleNamespace(option=SimpleNamespace())
    phases.pytest_configure(config)
    item = SimpleNamespace(nodeid="invented::test_case", config=config, path=Path("invented/test_case.py"))
    session = SimpleNamespace(config=config, items=[item], exitstatus=0)
    item.session = session
    # Restored before this test's own call phase is reported: under CI's -s the real
    # helper writes that record to whatever sys.stdout is then.
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", stdout)
        phases.pytest_collection_finish(session)
        for when in ("setup", "call", "teardown"):
            report = SimpleNamespace(when=when, outcome="passed", failed=False, skipped=False, duration=0.0)
            hook = phases.pytest_runtest_makereport(item, SimpleNamespace(when=when, excinfo=None))
            next(hook)
            with pytest.raises(StopIteration):
                hook.send(SimpleNamespace(get_result=lambda report=report: report))
        phases.pytest_sessionfinish(session, 0)
    writes = [call[1] for call in stdout.calls if call[0] == "write"]
    assert [line.split(" ", 1)[0].split("=", 1)[0] for line in writes] == [
        "COLLECTED", "PHASE", "PHASE", "PHASE", "PHASES_COMPLETE", "PHASES_VALID"]
    assert all(line.endswith("\n") and line.count("\n") == 1 for line in writes)
    assert stdout.calls == [call for text in writes for call in (("write", text), ("flush",))]
    assert lane.phase_log_errors("".join(writes), "0\n") == []
