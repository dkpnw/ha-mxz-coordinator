"""The property rig's own promise, apart from the rules of tests/test_properties_*.py."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state as rs
from homeassistant.util import dt as dt_util

from tests.property_rig import open_rig
from tests.restore_store import find_stored_state, replace_stored_state


async def test_each_example_starts_with_no_restored_state(hass: HomeAssistant, freezer: Any) -> None:
    """The rig drops what the last example left, registered or not, on every core.

    Cores from 2026.11 file the two kinds of record in separate dicts.
    """
    registered = er.async_get(hass).async_get_or_create("switch", "invented", "leftover").entity_id
    unregistered = "switch.invented_unregistered"
    for entity_id in (registered, unregistered):
        replace_stored_state(hass, entity_id, rs.StoredState(State(entity_id, "on"), None, dt_util.utcnow()))
        assert find_stored_state(hass, entity_id) is not None
    rig = await open_rig(hass, freezer)
    try:
        assert find_stored_state(hass, registered) is None
        assert find_stored_state(hass, unregistered) is None
    finally:
        await rig.close()
