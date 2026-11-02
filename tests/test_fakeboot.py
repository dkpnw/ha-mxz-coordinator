"""tools/fakeboot.py, the CI fresh-boot clock, run as CI runs it: ``-p tools.fakeboot``.

Each case is a child pytest over invented tests, with every installed plugin
loaded (freezegun among them), so the parent's own clock never matters.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]

CLOCK_TESTS = '''
import asyncio
import time

import tools.fakeboot as fakeboot

UPTIME = {uptime}


def test_the_clock_reads_the_fake_uptime():
    assert UPTIME <= time.monotonic() < UPTIME + 60


def test_the_event_loop_keeps_the_same_clock():
    loop = asyncio.new_event_loop()
    try:
        assert abs(loop.time() - time.monotonic()) < 1
    finally:
        loop.close()


def test_a_frozen_test_runs(freezer):
    freezer.tick(5)


def test_the_clock_is_back_after_freezer():
    assert time.monotonic is fakeboot.monotonic
    assert UPTIME <= time.monotonic() < UPTIME + 60
'''

LATER_TESTS = '''
import time

import tools.fakeboot as fakeboot

UPTIME = {uptime}


def test_the_host_runs_on():
    """The real clock moves an hour on, as across a long run."""
    real = fakeboot._real
    fakeboot._real = lambda: real() + 3600
    assert time.monotonic() >= UPTIME + 3600


def test_the_next_test_starts_just_booted():
    assert UPTIME <= time.monotonic() < UPTIME + 60
'''

REPLACED_TESTS = '''
import time


def test_replaces_the_clock():
    time.monotonic = lambda: 0.0


def test_runs_after_it():
    pass
'''


def _child(tmp_path: Path, source: str, uptime: str | None) -> subprocess.CompletedProcess:
    (tmp_path / "test_invented_clock.py").write_text(source)
    env = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONPATH": str(ROOT),
           "PYTHONDONTWRITEBYTECODE": "1"}
    if uptime is not None:
        env["FAKE_UPTIME"] = uptime
    # Outside the checkout, so -p no:respx is what the repository's pytest.ini adds.
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tools.fakeboot", "-p", "no:cacheprovider", "-p", "no:respx",
         "-o", "asyncio_mode=auto", "-o", "asyncio_default_fixture_loop_scope=function",
         "-q", "-s", "test_invented_clock.py"],
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, check=False, capture_output=True, text=True,
        timeout=120,
    )


def _outcomes(stdout: str) -> dict[str, int]:
    """The child's final summary as counts, its warnings left out.

    How many warnings a child prints depends on the installed core and plugin
    (HA 2026.11's own plugin surfaces its import-time deprecations), not on
    fakeboot. Every other outcome is counted exactly.
    """
    summary = stdout.rstrip().splitlines()[-1]
    match = re.fullmatch(r"((?:\d+ \w+, )*\d+ \w+) in [\d.]+s(?: \([\d:]+\))?", summary)
    assert match, stdout
    counts = {}
    for part in match.group(1).split(", "):
        number, outcome = part.split(" ")
        counts[outcome] = int(number)
    counts.pop("warnings", None)
    counts.pop("warning", None)
    return counts


@pytest.mark.parametrize("uptime,expected", [(None, 120), ("120", 120), ("86400", 86400)],
                         ids=["default", "ci", "a-day"])
def test_the_clock_reads_as_just_booted(tmp_path, uptime, expected):
    result = _child(tmp_path, CLOCK_TESTS.format(uptime=expected), uptime)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines().count(f"FAKEBOOT_UPTIME={expected}") == 1, result.stdout
    assert _outcomes(result.stdout) == {"passed": 4}


def test_a_wrong_uptime_fails_the_clock_check(tmp_path):
    """Negative control: the oracle above is the uptime asked for, not any clock."""
    result = _child(tmp_path, CLOCK_TESTS.format(uptime=86400), "120")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAKEBOOT_UPTIME=120" in result.stdout.splitlines()
    assert _outcomes(result.stdout) == {"failed": 2, "passed": 2}


def test_each_test_starts_at_the_fake_uptime(tmp_path):
    """Not the uptime plus the run so far: the clock reads it again at each setup."""
    result = _child(tmp_path, LATER_TESTS.format(uptime=120), "120")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines().count("FAKEBOOT_UPTIME=120") == 1, result.stdout
    assert _outcomes(result.stdout) == {"passed": 2}


def test_a_replaced_clock_errors_the_next_test(tmp_path):
    result = _child(tmp_path, REPLACED_TESTS, "120")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "fakeboot: time.monotonic was replaced" in result.stdout
    assert _outcomes(result.stdout) == {"passed": 1, "error": 1}
