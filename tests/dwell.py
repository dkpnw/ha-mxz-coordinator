"""Age and check the mode-flip dwell clock the same way in every test.

The dwell stamp, ``MXZCoordinator._last_mode_change_ts``, is ``time.monotonic()``
(#32). Monotonic seconds count from an arbitrary origin, on Linux the boot, so
the suite's old wall-clock sentinel ``0.0`` meant "long ago" only on a host up
for longer than the hysteresis. On a freshly booted host or CI runner it reads
as a flip made moments ago and the dwell still holds. ``tools/fakeboot.py``
runs the suite with the clock reading two minutes since boot to keep it that way.

So a test never writes a number into the stamp: it ages the dwell relative to
the clock the coordinator reads, and checks a re-stamp against the stamp it
wrote, never against zero.

A fresh install has no mode change on record (the stamp is None) and holds
nothing. A test whose subject needs the dwell holding from setup says so with
``record_mode_change``: a change a previous run stored before this setup, as a
restart finds it.
"""

from __future__ import annotations

import time
from typing import Any

# A stamp written during the step under test is this fresh, at most. Seconds,
# generous for a slow runner. A missing stamp fails on ``before`` instead.
RESTAMP_TOLERANCE = 10.0


def dwell_long_ago(coord: Any) -> float:
    """A stamp whose dwell ran out one second ago, at the current hysteresis."""
    return time.monotonic() - coord.hysteresis - 1


def expire_dwell(coord: Any) -> float:
    """Age the dwell to expired and return the stamp written.

    Keep the return value to prove later that nothing re-stamped the clock
    (``== stamp``), or that a flip did (``assert_restamped(coord, stamp)``).
    """
    stamp = dwell_long_ago(coord)
    coord._last_mode_change_ts = stamp
    return stamp


def assert_restamped(coord: Any, before: float) -> None:
    """A flip wrote the dwell clock after ``before``, and it reads as now.

    Fails if the stamp was not written (it still equals ``before``), and if it
    was written from another clock: a wall-clock epoch value is not ``<= now``
    on the monotonic clock.
    """
    stamp = coord._last_mode_change_ts
    now = time.monotonic()
    assert before < stamp <= now, (before, stamp, now)
    assert now - stamp < RESTAMP_TOLERANCE, (stamp, now)


async def record_mode_change(hass: Any, entry_id: str, seconds_ago: float = 0.0) -> float:
    """Store a mode change ``seconds_ago``, as the run before this setup would have.

    Written through the entry's own store, so the next setup or reload reads
    it exactly as it reads a real record. Returns the wall-clock time stored.
    (Home Assistant is imported here, so this module stays importable without it.)
    """
    from homeassistant.util import dt as dt_util

    from custom_components.mxz_coordinator.coordinator import (
        MODE_CHANGE_KEY,
        _mode_change_store,
    )

    changed_at = dt_util.utcnow().timestamp() - seconds_ago
    await _mode_change_store(hass, entry_id).async_save({MODE_CHANGE_KEY: changed_at})
    return changed_at
