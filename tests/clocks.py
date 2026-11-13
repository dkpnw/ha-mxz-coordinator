"""Make time pass on the coordinator's clock, watch its waits, and step the wall clock.

Every hold, wait, time limit and rate limit in MXZ reads one clock,
``coord.clock``: monotonic seconds, from an arbitrary origin (on Linux, the
boot). The suite stands this module's ``Clock`` in for the production one before
each setup (``tests/conftest.py``), so a test makes time pass with ``pass_time``
and never writes a number into a stamp. It reads what the production clock reads
and adds the seconds passed (``Time``, one per test, so a reload keeps them), so
what that clock reads is still under test: were it to follow the wall clock, the
wall-clock step tests would fail. Passing time moves every hold at once, as real
time does. It does not run a wait: a test that wants a wakeup to run fires it as
well (``async_fire_time_changed``), or uses ``freezer``, which moves both, since
the production clock reads ``time.monotonic``.

Every wait MXZ arms goes through ``coord.clock.call_later``, so a test reads the
waits there and never patches Home Assistant's timers. ``coord.clock.log`` has
each one armed (``Armed``): its name, key and delay, when it was armed, whether
it was cancelled, how often it ran, and the callback Home Assistant's loop runs
for it. A name in
``coord.clock.held`` is armed and logged but left off the loop, so it runs only
when the test fires it.

``jump_wall_clock`` is the other half: an NTP step or a clock set by hand, as
the coordinator sees it through ``dt_util.utcnow``. The coordinator's clock
does not move with it, so a wait on that clock is neither cut short nor
stretched.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from custom_components.mxz_coordinator import clock as mxz_clock


class Time:
    """The seconds a test has made pass, for every coordinator it sets up."""

    def __init__(self) -> None:
        self.passed = 0.0

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"a monotonic clock never goes back ({seconds} s)")
        self.passed += seconds


@dataclass(eq=False)
class Armed:
    """One wait a clock armed, and what became of it."""

    name: str
    key: Hashable
    delay: float
    at: float  # when it was armed, on the event loop's clock
    job: HassJob[[datetime], None]  # the clock's own callback, as the loop runs it
    cancelled: bool = False
    fired: int = 0

    def fire(self, now: datetime | None = None) -> None:
        """Run the callback as the loop does, armed or not (a cancel cannot recall one the loop took)."""
        self.fired += 1
        self.job.target(now or dt_util.utcnow())


class Clock(mxz_clock.Clock):
    """A coordinator's clock in a test: the production reading plus ``time``, and a log of its waits."""

    def __init__(self, hass: HomeAssistant, *, time: Time) -> None:
        super().__init__(hass)
        self.time = time
        self.log: list[Armed] = []
        self.held: set[str] = set()

    def __call__(self) -> float:
        return super().__call__() + self.time.passed

    def advance(self, seconds: float) -> None:
        self.time.advance(seconds)

    def keys(self, name: str) -> set[Hashable]:
        """The keys of the waits of this name armed now (``{None}`` for a single wait)."""
        return {key for waiting, key in self._waits if waiting == name}

    def names(self) -> set[str]:
        """The name of every wait armed now."""
        return {name for name, _ in self._waits}

    def armed(self, name: str) -> list[Armed]:
        """Every wait of this name armed so far, oldest first."""
        return [armed for armed in self.log if armed.name == name]

    def _schedule(self, wait: mxz_clock.Wait, job: HassJob[[datetime], None]) -> CALLBACK_TYPE:
        armed = Armed(wait.name, wait.key, wait.delay, self._hass.loop.time(), job)
        self.log.append(armed)
        cancel: Callable[[], None] = lambda: None
        if wait.name not in self.held:

            @callback
            def run(now: datetime) -> None:
                armed.fire(now)

            cancel = super()._schedule(
                wait, HassJob(run, job.name, cancel_on_shutdown=job.cancel_on_shutdown)
            )

        def cancelled() -> None:
            armed.cancelled = True
            cancel()

        return cancelled


def pass_time(coord: Any, seconds: float) -> None:
    """``seconds`` pass for the coordinator: every hold, wait and limit it keeps."""
    coord.clock.advance(seconds)


def jump_wall_clock(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Step ``dt_util.utcnow`` by ``seconds`` (back if negative); steps add up."""
    current = dt_util.utcnow
    monkeypatch.setattr(dt_util, "utcnow", lambda: current() + timedelta(seconds=seconds))
