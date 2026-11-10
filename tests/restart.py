"""Stop and start Home Assistant around a loaded MXZ entry, as its bootstrap does.

The suite's one restart. A stop writes restore data and flushes every pending
store write, unloads the entry, and then nothing of MXZ is left in memory:
every ``hass.data`` key under the domain goes, since a new process starts with
none. A start sets the entry up before Home Assistant is running and then
starts it, so MXZ's startup quiet window is open afterwards and ends
``STARTUP_RECOVER_DELAY`` seconds later, as after a real start. Each store and
the restore data are read anew.

A crash is a stop that writes nothing: the restore data a start reads is the
last one dumped, and a store write still waiting is lost.

The clock does not move (``tests/clocks.py``): the coordinator built at the
start reads the same clock, as a new process reads the same monotonic clock.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from homeassistant.const import (
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    EVENT_HOMEASSISTANT_START,
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import restore_state as rs
from pytest_homeassistant_custom_component.common import (
    async_mock_load_restore_state_from_storage,
)

from custom_components.mxz_coordinator.const import DOMAIN


@asynccontextmanager
async def stopped(hass: HomeAssistant, entry: Any, *, crash: bool = False) -> AsyncIterator[None]:
    """Home Assistant is down for the ``async with`` body, and starts at its end.

    The body is the outage: a head changed at its remote, a restore record
    planted, a store edited. MXZ is not running to see any of it.
    """
    if not crash:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
        await hass.async_block_till_done()
        await rs.async_get(hass).async_dump_states()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    for key in [key for key in hass.data if isinstance(key, str) and key.startswith(DOMAIN)]:
        del hass.data[key]
    await async_mock_load_restore_state_from_storage(hass)
    yield
    hass.set_state(CoreState.not_running)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.set_state(CoreState.starting)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()


async def restart(hass: HomeAssistant, entry: Any, *, crash: bool = False) -> None:
    """A stop and a start with nothing in between."""
    async with stopped(hass, entry, crash=crash):
        pass
