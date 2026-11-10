"""Let the mode-change hold run out, and check what started it, the same way in every test.

The hold runs on the coordinator's clock (#32, ``coord.clock``): monotonic
seconds from an arbitrary origin, on Linux the boot. The suite's old
wall-clock sentinel ``0.0`` meant "long ago" only on a host up for longer than
the hysteresis; ``tools/fakeboot.py`` runs the suite with the clock reading
two minutes since boot to keep it that way. So a test never writes a stamp: it
lets the hold's time pass on that clock (``tests/clocks.py``), and reads the
time since the last change where MXZ publishes it (``tests/peek.py``).

A fresh install has no mode change on record and holds nothing. A test whose
subject needs the hold from setup says so with ``record_mode_change``: a
change a previous run stored before this setup, as a restart finds it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.util import dt as dt_util

from custom_components.mxz_coordinator.coordinator import (
    MODE_CHANGE_KEY,
    _mode_change_store,
)
from tests.clocks import pass_time
from tests.peek import since_mode_change

# A change made during the step under test is at most this many seconds old,
# generous for a slow runner. A test that checks a step stamped nothing lets
# at least this much pass after the change it allows.
RESTAMP_TOLERANCE = 10.0


def expire_dwell(coord: Any) -> None:
    """Let the hold run out: what is left of it passes on the coordinator's
    clock, and one second more, so the gate reads it open. Every other wait
    on that clock moves by as much, as it would in the house. With no hold
    running, no time passes.
    """
    if (remaining := coord.mode_hold_remaining()) > 0:
        pass_time(coord, remaining + 1)


async def assert_restamped(hass: Any, entry: Any) -> None:
    """A real mode change was made within the step under test: the hold starts now."""
    since = await since_mode_change(hass, entry)
    assert since is not None and 0 <= since < RESTAMP_TOLERANCE, since


async def assert_not_restamped(hass: Any, entry: Any) -> None:
    """No real mode change in the last ``RESTAMP_TOLERANCE`` seconds.

    The test lets at least that much pass after the change it allows, with
    ``expire_dwell`` or ``pass_time``, before the step that must not stamp.
    """
    since = await since_mode_change(hass, entry)
    assert since is None or since >= RESTAMP_TOLERANCE, since


async def record_mode_change(hass: Any, entry_id: str, seconds_ago: float = 0.0) -> float:
    """Store a mode change ``seconds_ago``, as the run before this setup would have.

    Written through the entry's own store, so the next setup or reload reads
    it exactly as it reads a real record. Returns the wall-clock time stored.
    """
    changed_at = dt_util.utcnow().timestamp() - seconds_ago
    await _mode_change_store(hass, entry_id).async_save({MODE_CHANGE_KEY: changed_at})
    return changed_at
