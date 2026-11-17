"""Make MXZ's room state store agree with a test's premise about what was saved.

Before ``custom_components/mxz_coordinator/room_state.py`` the only saved room state was
Home Assistant's restore data. A test whose premise is set up there alone (no record, a
record that can't be used, a record an earlier release wrote, an older dump that survived)
says nothing about MXZ's own store, which holds the current record. The store has none
of it when that premise is true: a release before the store never wrote one, and a
re-added entry starts with none. These helpers take it out, so the test's premise holds
with no change to what it asserts. Call them while Home Assistant is down
(``tests/restart.py``) or before a reload.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.mxz_coordinator.room_state import (
    SECTION_FAN,
    _room_state_store,
    async_remove_room_state,
)
from tests.ha_test import async_fire_time_changed


async def forget_room_state(hass: HomeAssistant, entry: Any) -> None:
    """No room state store: an install whose saved state is all restore data.

    A write still waiting lands first, so it can't bring the store back after
    (the test storage's remove leaves it scheduled, and a frozen clock holds it).
    """
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await async_remove_room_state(hass, entry.entry_id)


async def forget_saved_fan(hass: HomeAssistant, entry: Any, climate_ids: list[str] | None = None) -> None:
    """The store keeps no fan hold for these heads (every head when None)."""
    store = _room_state_store(hass, entry.entry_id)
    data = await store.async_load()
    if not isinstance(data, dict) or not isinstance(data.get(SECTION_FAN), dict):
        return
    fan = data[SECTION_FAN]
    for climate_id in list(fan) if climate_ids is None else climate_ids:
        fan.pop(climate_id, None)
    await store.async_save(data)
