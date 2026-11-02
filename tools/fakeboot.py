"""Run the suite as if the host had just booted: ``-p tools.fakeboot``.

The mode-flip dwell runs on ``time.monotonic()`` (#32), whose origin on Linux
is the boot; so do the coil-dry wait and the floor's driven-time clock and
notice limit. A test that ages one of them with a wall-clock habit (stamp
``0.0`` for "long ago") passes on a long-running host and fails on a fresh
one, so CI runs the whole suite a second time with this plugin, to fail it on
any host.

It offsets ``time.monotonic`` so the clock reads ``FAKE_UPTIME`` seconds
(default 120, below the default hysteresis) when the plugin loads, and again as
each test's setup starts, then counts up at the real rate through that test.
So a test late in a long run sees the same just-booted clock as the first one,
not that uptime plus the run so far. Between tests the clock steps back; no
test keeps a monotonic stamp from one test to the next (each has its own event
loop and Home Assistant). ``asyncio``'s ``loop.time()`` reads ``time.monotonic`` on
each call, so Home Assistant's timers stay consistent with it. ``-p`` loads
this before the installed plugins, so freezegun (``freezer``) records this
clock as its real one and puts it back when a frozen test ends. Inside a
``freezer`` test, though, freezegun replaces ``time.monotonic`` with its own,
which reads the frozen wall-clock epoch, so this guard does not cover a
long-ago sentinel written inside a frozen test: there ``0.0`` still reads as
long ago on any host. If anything else leaves ``time.monotonic`` replaced, the
next test errors instead of quietly running on the real clock. The log carries
``FAKEBOOT_UPTIME=<s>`` once, as evidence the run was made on this clock.

Only ``time.monotonic`` moves; ``monotonic_ns`` and ``perf_counter`` do not.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

UPTIME = float(os.environ.get("FAKE_UPTIME", "120"))

_real = time.monotonic
_offset = 0.0


def monotonic() -> float:
    """The real monotonic clock, as if the host booted ``UPTIME`` s before the
    plugin loaded or, once tests run, before the current test's setup."""
    return _real() - _offset


def _boot() -> None:
    """Make the clock read ``UPTIME`` now."""
    global _offset
    _offset = _real() - UPTIME


_boot()
time.monotonic = monotonic


def pytest_sessionstart(session: pytest.Session) -> None:
    """One evidence line: the run was made on the fake clock, from this uptime."""
    sys.stdout.write(f"FAKEBOOT_UPTIME={UPTIME:g}\n")
    sys.stdout.flush()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Refuse to run a test on any clock but this one, then start it just booted."""
    if time.monotonic is not monotonic:
        raise RuntimeError(f"fakeboot: time.monotonic was replaced by {time.monotonic!r}")
    _boot()
