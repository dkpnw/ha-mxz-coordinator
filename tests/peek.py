"""What a test may read of a running coordinator beyond its entities.

``debug_view`` is the diagnostics download: what MXZ itself publishes of its
holds, waits, votes and fan memory, by room. A test that needs one of those
reads it there, so the reading survives any move of the field behind it.

``armed`` is the one thing diagnostics does not publish: whether a wakeup is
waiting. Its names are the coordinator's private timer fields, and this is
the only module that reads them. A test asserts a wakeup only where firing
it cannot tell the difference, such as a wakeup that would do nothing yet.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.diagnostics import (
    async_get_config_entry_diagnostics,
)

# Each wakeup, by what it is for: the coordinator field that holds it.
_WAKEUPS = {
    "dwell": "_dwell_timer",  # a flip the mode-change hold deferred
    "hold_end": "_hold_end_timer",  # the thermostats' re-render as the hold ends
    "coast": "_coast_timer",  # the follow-up after a room let go of its run
    "lost": "_lost_timer",  # the nearest lost head's vote running out
    "settle": "_settle_timer",  # the nearest end of a head's settle window
    "comfort": "_comfort_timer",  # the nearest Comfort priority "on" going stale
    "cloud": "_cloud_timer",  # the nearest cloud head's write or stale window
    "fresh": "_fresh_timer",  # the nearest room sensor going stale
    "refresh_followup": "_refresh_followup_timer",  # a refresh asked for during one
}
# Wakeups kept per head or per room, by key.
_WAKEUP_MAPS = {
    "dry": "_dry_timers",  # a head's coil-dry run ending, by head
    "dry_pick": "_dry_picks",  # a room's Dry pick taking effect, by room
    "heal": "_heal_timers",  # a head's put-back or adoption, by (head, kind)
    "opening": "_opening_timers",  # a room's door/window delay, by room
}


async def debug_view(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """The entry's diagnostics: ``coordinator`` and ``rooms`` by slug, as downloaded."""
    return await async_get_config_entry_diagnostics(hass, entry)


async def since_mode_change(hass: HomeAssistant, entry: Any) -> float | None:
    """Seconds since the last real mode change on the coordinator's clock; None if none."""
    view = await debug_view(hass, entry)
    return view["coordinator"]["anti_flip"]["seconds_since_mode_change"]


def armed(coord: Any, wakeup: str, key: Any = None) -> bool:
    """Whether the named wakeup is waiting (for ``key``, on a per-head or per-room one).

    A per-head or per-room wakeup with no ``key`` reads True when any is waiting.
    """
    if wakeup in _WAKEUPS:
        assert key is None, f"{wakeup} is one wakeup, not one per key"
        return getattr(coord, _WAKEUPS[wakeup]) is not None
    waiting = getattr(coord, _WAKEUP_MAPS[wakeup])
    return bool(waiting) if key is None else key in waiting


def armed_wakeups(coord: Any) -> set[str]:
    """Every wakeup waiting now, by name."""
    return {name for name in (*_WAKEUPS, *_WAKEUP_MAPS) if armed(coord, name)}
