"""The idle-action default moved from fan_only to off_after_dry (entry v2.2).

New setups store off_after_dry. An existing entry that never stored a choice
was running fan_only, so the v2.1 -> v2.2 migration pins fan_only explicitly;
an entry that stored any choice keeps it. The oracle for "what it was running
under" is the literal "fan_only", not DEFAULT_IDLE_ACTION, which is the very
value that changed.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator import async_migrate_entry
from custom_components.mxz_coordinator.const import (
    CONF_DEMAND_THRESHOLD,
    CONF_ZONES,
    DOMAIN,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
)
from tests.test_setup_flow import (
    BEDROOM,
    BEDROOM_SENSOR,
    LIVING,
    LIVING_SENSOR,
    _household,  # noqa: F401  (autouse: the fake heads and sensors)
    _skip_advanced_options,
)

ZONES = [
    {ZONE_NAME: "Living Room", ZONE_CLIMATE: LIVING, ZONE_SENSOR: LIVING_SENSOR},
    {ZONE_NAME: "Bedroom", ZONE_CLIMATE: BEDROOM, ZONE_SENSOR: BEDROOM_SENSOR},
]


def _entry(
    hass: HomeAssistant,
    data: dict[str, Any],
    options: dict[str, Any],
    minor_version: int = 1,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={CONF_ZONES: deepcopy(ZONES), **data},
        options=options,
        version=2,
        minor_version=minor_version,
        unique_id=f"{LIVING}|{BEDROOM}",
    )
    entry.add_to_hass(hass)
    return entry


async def _set_up(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_entry_without_idle_action_keeps_fan_only(hass: HomeAssistant) -> None:
    """(a) A pre-3.3 entry that never chose is pinned to fan_only in both stores."""
    entry = _entry(
        hass, {CONF_DEMAND_THRESHOLD: 3.0}, {CONF_DEMAND_THRESHOLD: 3.0}
    )

    await _set_up(hass, entry)

    assert (entry.version, entry.minor_version) == (2, 2)
    assert dict(entry.options) == {CONF_DEMAND_THRESHOLD: 3.0, "idle_action": "fan_only"}
    assert entry.data["idle_action"] == "fan_only"
    assert entry.data[CONF_ZONES] == ZONES
    assert entry.runtime_data.idle_action == "fan_only"


async def test_data_only_entry_keeps_fan_only_and_its_disclosure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """(a) Options stay empty, so the data-mirror disclosure is still true."""
    entry = _entry(hass, {CONF_DEMAND_THRESHOLD: 3.0}, {})

    await _set_up(hass, entry)

    assert entry.minor_version == 2
    assert dict(entry.options) == {}
    assert entry.data["idle_action"] == "fan_only"
    assert entry.runtime_data.idle_action == "fan_only"
    assert "options are empty but the data mirror has the config" in caplog.text


@pytest.mark.parametrize("action", ["off", "off_after_dry", "fan_only"])
@pytest.mark.parametrize("store", ["options", "data"])
async def test_explicit_choice_is_untouched(
    hass: HomeAssistant, action: str, store: str
) -> None:
    """(b) A stored choice, in either store, survives the migration byte-for-byte."""
    data = {CONF_DEMAND_THRESHOLD: 3.0}
    options = {CONF_DEMAND_THRESHOLD: 3.0}
    (options if store == "options" else data)["idle_action"] = action
    entry = _entry(hass, data, options)
    data_before, options_before = deepcopy(dict(entry.data)), deepcopy(dict(entry.options))

    await _set_up(hass, entry)

    assert entry.minor_version == 2
    assert dict(entry.data) == data_before
    assert dict(entry.options) == options_before
    assert entry.runtime_data.idle_action == action


async def test_new_setup_defaults_to_off_after_dry(hass: HomeAssistant) -> None:
    """(c) A fresh entry stores off_after_dry, is born v2.2 and runs it."""

    options = await _skip_advanced_options(hass)
    await hass.async_block_till_done()

    assert options["idle_action"] == "off_after_dry"
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert (entry.version, entry.minor_version) == (2, 2)
    assert entry.data["idle_action"] == entry.options["idle_action"] == "off_after_dry"
    assert entry.runtime_data.idle_action == "off_after_dry"


async def test_current_entry_without_idle_action_is_not_pinned(
    hass: HomeAssistant,
) -> None:
    """Negative: the pin is a one-time v2.1 step, not a rewrite of every entry.

    A v2.2 entry with no stored choice runs the current default and stays
    unwritten.
    """
    entry = _entry(
        hass, {CONF_DEMAND_THRESHOLD: 3.0}, {CONF_DEMAND_THRESHOLD: 3.0}, 2
    )

    await _set_up(hass, entry)

    assert "idle_action" not in {**entry.data, **entry.options}
    assert entry.runtime_data.idle_action == "off_after_dry"


async def test_future_minor_is_accepted_untouched_and_major_refused(
    hass: HomeAssistant,
) -> None:
    """A newer minor (2.3) is compatible and left alone; v3 is still refused."""
    newer = _entry(hass, {CONF_DEMAND_THRESHOLD: 3.0}, {CONF_DEMAND_THRESHOLD: 3.0}, 3)
    assert await async_migrate_entry(hass, newer)
    assert newer.minor_version == 3
    assert "idle_action" not in {**newer.data, **newer.options}

    future = MockConfigEntry(domain=DOMAIN, data={}, version=3)
    future.add_to_hass(hass)
    assert not await async_migrate_entry(hass, future)
