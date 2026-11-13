"""What a test may read of a running coordinator beyond its entities.

``debug_view`` is the diagnostics download: what MXZ itself publishes of its
holds, waits, votes and fan memory, by room. A test that needs one of those
reads it there, so the reading survives any move of the field behind it.

``cloud_record`` is a cloud head's record, read in place where a test
cannot wait for the download.

``armed`` is the one thing diagnostics does not publish: whether a wakeup is
waiting. It reads the coordinator's clock by the wait's name (``tests/clocks.py``),
and the delivery queue's follow-up, which is a task on current Home Assistant and
a wait on the clock only on the oldest. A test asserts a wakeup only where firing
it cannot tell the difference, such as a wakeup that would do nothing yet.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.diagnostics import (
    async_get_config_entry_diagnostics,
)

# Each wakeup, by its name on the clock: what it is for.
_WAKEUPS = {
    "dwell": "a flip the mode-change hold deferred",
    "hold_end": "the thermostats' re-render as the hold ends",
    "coast": "the follow-up after a room let go of its run",
    "lost": "the nearest lost head's vote running out",
    "settle": "the nearest end of a head's settle window",
    "comfort": 'the nearest Comfort priority "on" going stale',
    "cloud": "the nearest cloud head's write or stale window",
    "fresh": "the nearest room sensor going stale",
    "startup": "the recompute once Home Assistant has settled after its start",
}
# Wakeups kept per head or per room, by key.
_WAKEUP_MAPS = {
    "dry": "a head's coil-dry run ending, by head",
    "dry_pick": "a room's Dry pick taking effect, by room",
    "heal": "a head's put-back or adoption, by (head, kind)",
    "opening": "a room's door/window delay, by room",
}
# A refresh asked for during one: the delivery queue holds it, a task or a wait.
_FOLLOWUP = "refresh_followup"


async def debug_view(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """The entry's diagnostics: ``coordinator`` and ``rooms`` by slug, as downloaded."""
    return await async_get_config_entry_diagnostics(hass, entry)


def cloud_record(coord: Any, head: str) -> Any:
    """A cloud head's record, the one diagnostics shows as the room's ``cloud``; None for a local head.

    Read in place, for its settings (``confirm_wait``, ``write_interval``) and
    its status; a test never writes it.
    """
    return coord.heads.cloud(head)


async def since_mode_change(hass: HomeAssistant, entry: Any) -> float | None:
    """Seconds since the last real mode change on the coordinator's clock; None if none."""
    view = await debug_view(hass, entry)
    return view["coordinator"]["anti_flip"]["seconds_since_mode_change"]


def armed(coord: Any, wakeup: str, key: Any = None) -> bool:
    """Whether the named wakeup is waiting (for ``key``, on a per-head or per-room one).

    A per-head or per-room wakeup with no ``key`` reads True when any is waiting.
    """
    if wakeup == _FOLLOWUP or wakeup in _WAKEUPS:
        assert key is None, f"{wakeup} is one wakeup, not one per key"
        if wakeup == _FOLLOWUP:
            return coord.deliveries._followup_timer is not None
        return coord.clock.waiting(wakeup)
    keys = armed_keys(coord, wakeup)
    return bool(keys) if key is None else key in keys


def armed_keys(coord: Any, wakeup: str) -> set[Any]:
    """The heads or rooms a per-head or per-room wakeup is waiting for."""
    assert wakeup in _WAKEUP_MAPS, f"{wakeup} is not a per-head or per-room wakeup"
    return coord.clock.keys(wakeup)


def armed_wakeups(coord: Any) -> set[str]:
    """Every wakeup waiting now, by name."""
    names = coord.clock.names()
    assert names <= {*_WAKEUPS, *_WAKEUP_MAPS}, f"unknown wakeup in {names}"
    return names | ({_FOLLOWUP} if armed(coord, _FOLLOWUP) else set())
