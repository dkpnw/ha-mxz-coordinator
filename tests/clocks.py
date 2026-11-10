"""Make time pass on the coordinator's clock, and step the wall clock.

Every hold, wait, time limit and rate limit in MXZ reads one clock,
``coord.clock``: monotonic seconds, from an arbitrary origin (on Linux, the
boot). The suite stands a ``Clock`` in for it before each setup
(``tests/conftest.py``), so a test makes time pass with ``pass_time`` and
never writes a number into a stamp. The ``Clock`` reads the production clock
it stands in for and adds the seconds passed, so what that clock reads is
still under test: were it to follow the wall clock, the wall-clock step tests
would fail. Passing time moves every wait at once, as real time does. It does
not fire Home Assistant's timers: a test that wants a wakeup to run fires it
as well (``async_fire_time_changed``), or uses ``freezer``, which moves both,
since the production clock reads ``time.monotonic``.

``jump_wall_clock`` is the other half: an NTP step or a clock set by hand, as
the coordinator sees it through ``dt_util.utcnow``. The coordinator's clock
does not move with it, so a wait on that clock is neither cut short nor
stretched.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.util import dt as dt_util


class Clock:
    """The clock it stands in for, plus the seconds a test has made pass."""

    def __init__(self, reads: Callable[[], float]) -> None:
        self.reads = reads
        self.passed = 0.0

    def __call__(self) -> float:
        return self.reads() + self.passed

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"a monotonic clock never goes back ({seconds} s)")
        self.passed += seconds


def pass_time(coord: Any, seconds: float) -> None:
    """``seconds`` pass for the coordinator: every hold, wait and limit it keeps."""
    coord.clock.advance(seconds)


def jump_wall_clock(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Step ``dt_util.utcnow`` by ``seconds`` (back if negative); steps add up."""
    current = dt_util.utcnow
    monkeypatch.setattr(dt_util, "utcnow", lambda: current() + timedelta(seconds=seconds))
