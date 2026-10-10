"""A room sensor HA has not loaded yet at startup is not a WARNING until startup settles.

During HA startup a sensor's integration may still be loading, so its entity is
missing, ``unavailable`` or ``unknown`` for a while. The room is still out of
automatic demand and parked at once, exactly as at any other time; only the
"no usable reading" WARNING waits for the post-start recompute
(``STARTUP_RECOVER_DELAY`` after HA starts). If the sensor is still unusable
then, the WARNING is logged once; if it came up in time, never.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
    HEALTH_CADENCE_UNKNOWN,
    HEALTH_INVALID,
    IDLE_ACTION_FAN_ONLY,
    MODE_COOL,
    MODE_FAN_ONLY,
    STARTUP_RECOVER_DELAY,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from tests.test_drive import SENSOR_A, SENSOR_B, _eid, _set_temp, _setup_mock_heads

# The startup timer is armed at the START event and the in-grace steps fire
# relative to "now". A frozen clock keeps a slow host's real stall between the
# two from moving the timer due early; only async_fire_time_changed moves time.
pytestmark = pytest.mark.usefixtures("freezer")

LOGGER = "custom_components.mxz_coordinator"
WARNING = "has no usable reading"
HOT = 75.0


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith(LOGGER) and r.levelno >= logging.WARNING
    ]


def _unusable(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith(LOGGER) and WARNING in r.getMessage()]


async def _later(hass: HomeAssistant, seconds: float) -> None:
    """Fire every timer due within ``seconds``, then the refresh it requested."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(seconds=seconds + REQUEST_REFRESH_DEFAULT_COOLDOWN + 1),
    )
    await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, *, running: bool) -> tuple[MockConfigEntry, str]:
    """Set the entry up as HA's bootstrap does (not yet running) or as a reload does."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    # Room A calls COOL, so the house runs and room B's head visibly parks.
    await _set_temp(hass, SENSOR_A, HOT)
    if not running:
        hass.set_state(CoreState.not_running)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: False,
            CONF_MODE_HYSTERESIS: 0,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    # The enable switches only queue a debounced refresh; run it now.
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(head_a).state == MODE_COOL, "the healthy room keeps steering"
    return entry, head_b


async def _start(hass: HomeAssistant) -> None:
    """HA finishes starting: the start event arms the post-start recompute."""
    hass.set_state(CoreState.starting)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()


def _zone_b(coord: MXZCoordinator) -> str:
    return coord.zones[1].slug


@pytest.mark.parametrize("unusable", ["absent", "unavailable", "unknown"])
async def test_still_unusable_after_startup_warns_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, unusable: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        if unusable != "absent":
            hass.states.async_set(SENSOR_B, unusable)
        entry, head_b = await _setup(hass, running=False)
        coord: MXZCoordinator = entry.runtime_data
        slug = _zone_b(coord)
        # Behaviour is not deferred: the room is out of demand and parked at once.
        assert coord.data[f"{slug}_sensor_health"] == HEALTH_INVALID
        assert coord.data["sensors_ok"] is False
        assert hass.states.get(head_b).state == MODE_FAN_ONLY
        assert not _warnings(caplog)
        assert [r.levelno for r in _unusable(caplog)] == [logging.DEBUG]

        await _start(hass)
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=STARTUP_RECOVER_DELAY - 1)
        )
        await hass.async_block_till_done()
        await coord.async_refresh()  # a compute inside the grace stays quiet
        await hass.async_block_till_done()
        assert not _warnings(caplog), "still inside the startup grace"
        assert coord.data[f"{slug}_sensor_health"] == HEALTH_INVALID

        await _later(hass, STARTUP_RECOVER_DELAY + 1)
        warned = _unusable(caplog)
        assert [r.levelno for r in warned] == [logging.DEBUG, logging.WARNING]
        assert SENSOR_B in warned[-1].getMessage()
        assert _warnings(caplog) == [warned[-1].getMessage()]

        # One episode, one WARNING: later computes add nothing.
        await coord.async_refresh()
        await _later(hass, 20 * 60)
        assert len(_warnings(caplog)) == 1
        assert hass.states.get(head_b).state == MODE_FAN_ONLY

        # The existing recovery line still closes the episode.
        await _set_temp(hass, SENSOR_B, 70)
        await coord.async_refresh()
        await hass.async_block_till_done()
        assert coord.data[f"{slug}_sensor_health"] == HEALTH_CADENCE_UNKNOWN
        assert sum("reporting again" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("unusable", ["absent", "unavailable"])
async def test_sensor_that_loads_during_startup_never_warns(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, unusable: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        if unusable != "absent":
            hass.states.async_set(SENSOR_B, unusable)
        entry, head_b = await _setup(hass, running=False)
        coord: MXZCoordinator = entry.runtime_data
        slug = _zone_b(coord)
        assert coord.data[f"{slug}_sensor_health"] == HEALTH_INVALID
        assert hass.states.get(head_b).state == MODE_FAN_ONLY
        await _start(hass)
        await _later(hass, 10)

        await _set_temp(hass, SENSOR_B, 70)
        await coord.async_refresh()
        await hass.async_block_till_done()
        assert coord.data[f"{slug}_sensor_health"] == HEALTH_CADENCE_UNKNOWN

        await _later(hass, STARTUP_RECOVER_DELAY + 1)
        await _later(hass, 20 * 60)
        assert not _warnings(caplog)
        # Nothing was announced, so there is no episode for a recovery line to close.
        assert not [r for r in caplog.records if "reporting again" in r.getMessage()]


async def test_reload_while_running_warns_at_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Control: with HA already running nothing is still loading; no grace."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        entry, head_b = await _setup(hass, running=True)
        coord: MXZCoordinator = entry.runtime_data
        assert coord.data[f"{_zone_b(coord)}_sensor_health"] == HEALTH_INVALID
        assert hass.states.get(head_b).state == MODE_FAN_ONLY
        assert [r.levelno for r in _unusable(caplog)] == [logging.WARNING]
        await _later(hass, STARTUP_RECOVER_DELAY + 1)
        assert len(_warnings(caplog)) == 1

