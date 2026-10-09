"""Phase evidence stays whole when CI writes stdout and stderr into one ``-s`` log."""

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ha202610", ROOT / "tools/ha202610.py")
lane = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lane)

STDERR_LINE = "WARNING:invented.loader:invented stderr record"
CHILD_TEST = '''import sys

import pytest


@pytest.mark.parametrize("case", [f"{n:05d}-" + "x" * 100 for n in range(CASES)])
def test_invented_case(case):
    # Like Home Assistant logging: straight to stderr while stdout is still buffered.
    sys.stderr.write("STDERR_LINE\\n")
'''


@pytest.mark.parametrize("conftest", ["repository", "absent"])
def test_stderr_never_splits_phase_evidence(tmp_path, conftest):
    # The real helper and the real suite conftest, run exactly as CI does: -s, with
    # stdout and stderr redirected to one file. All test data is invented.
    (tmp_path / "pytest_phases.py").write_bytes((ROOT / "tools/pytest_phases.py").read_bytes())
    if conftest == "repository":
        (tmp_path / "conftest.py").write_bytes((ROOT / "tests/conftest.py").read_bytes())
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    log = tmp_path / "suite.log"
    with log.open("wb") as stream:
        # Python buffers a file's stdout by st_blksize (3.12, 3.13) or at least 128 KiB
        # (3.14); the collection line must outgrow that buffer on every lane.
        buffer = max(io.DEFAULT_BUFFER_SIZE, os.fstat(stream.fileno()).st_blksize)
        cases = 2 * buffer // 100
        (tmp_path / "test_invented_split.py").write_text(
            CHILD_TEST.replace("CASES", str(cases)).replace("STDERR_LINE", STDERR_LINE))
        result = subprocess.run([sys.executable, "-m", "pytest", "-p", "pytest_phases",
                                 "test_invented_split.py", "-q", "-s", "-p", "no:cacheprovider"],
                                cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=subprocess.STDOUT, check=False, timeout=60)
    raw = log.read_text()
    assert result.returncode == 0, raw
    lines = raw.splitlines()
    collected = [line for line in lines if "COLLECTED [" in line]
    assert len(collected) == 1 and len(collected[0]) > 2 * buffer
    if conftest == "absent":
        # Block-buffered stdout leaves the newline behind the first stderr record.
        assert lane.phase_log_errors(raw, "0\n") == ["malformed phase evidence"]
        assert collected[0].endswith(STDERR_LINE)
    else:
        assert lane.phase_log_errors(raw, "0\n") == []
        assert lines.count(STDERR_LINE) == cases
