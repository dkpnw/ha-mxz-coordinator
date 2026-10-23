"""Step the wall clock, and age the coordinator's in-memory timers on their own clock.

The coil-dry wait (``_last_active``), the floor's driven-time clock
(``_floor_eval_ts``), the floor notice limit (``_floor_notice_at``) and a lost
head's vote time limit (``_sight[head].lost_at``) run on
``time.monotonic()``, like the mode-flip dwell (#32, see ``tests/dwell.py``).
Monotonic seconds count from an arbitrary origin, on Linux the boot, so a test
never writes a number into these stamps: it moves a stamp back by the seconds
it wants to have passed, on the clock the coordinator reads.

``jump_wall_clock`` is the other half: an NTP step or a clock set by hand, as
the coordinator sees it through ``dt_util.utcnow``. The monotonic clock does
not move with it, so a wait on that clock is neither cut short nor stretched.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.util import dt as dt_util


def jump_wall_clock(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Step ``dt_util.utcnow`` by ``seconds`` (back if negative); steps add up."""
    current = dt_util.utcnow
    monkeypatch.setattr(dt_util, "utcnow", lambda: current() + timedelta(seconds=seconds))


def age_coil_dry(coord: Any, head: str, seconds: float) -> float:
    """Move a head's coil-dry stamp ``seconds`` into the past; return it."""
    mode, stamp = coord._last_active[head]
    coord._last_active[head] = (mode, stamp - seconds)
    return stamp - seconds


def age_floor_clock(coord: Any, slug: str, seconds: float) -> None:
    """Make the last floor compute ``seconds`` older, so the next one charges them."""
    coord._floor_eval_ts[slug] -= seconds


def age_floor_notice(coord: Any, slug: str, seconds: float) -> None:
    """Make the room's last floor notice ``seconds`` older."""
    coord._floor_notice_at[slug] -= seconds


def age_lost_vote(coord: Any, head: str, seconds: float) -> None:
    """Make the moment MXZ lost sight of ``head`` ``seconds`` older."""
    sight = coord._sight[head]
    assert sight.lost_at is not None, f"{head} is in sight; nothing to age"
    sight.lost_at -= seconds
