"""The drift-alert service is checked like a room's (audit L5).

The service set in setup and Reconfigure must be a notify service Home Assistant has,
as a room's door/window notify service already must. Every head, sensor and service is
invented.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.mxz_coordinator.const import DOMAIN
from tests.test_setup_flow import (
    BEDROOM,
    BEDROOM_SENSOR,
    LIVING,
    LIVING_SENSOR,
    _household,  # noqa: F401  (autouse: the fake heads and sensors)
)

ROOMS = [
    {"name": "Living Room", "climate": LIVING, "sensor": LIVING_SENSOR},
    {"name": "Bedroom", "climate": BEDROOM, "sensor": BEDROOM_SENSOR},
]
ERROR = {"notify_service": "notify_service_invalid"}


def _entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        unique_id=f"{LIVING}|{BEDROOM}",
        version=2,
        minor_version=2,
        data={"zones": ROOMS},
        options={"idle_action": "fan_only", **options},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup_heads(hass: HomeAssistant, notify: str) -> dict[str, Any]:
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await flow.async_configure(
        result["flow_id"], {"heads": [LIVING, BEDROOM], "notify_service": notify}
    )


async def _reconfigure_heads(hass: HomeAssistant, entry: MockConfigEntry, notify: str) -> dict[str, Any]:
    flow = hass.config_entries.flow
    result = await flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    return await flow.async_configure(
        result["flow_id"], {"heads": [LIVING, BEDROOM], "notify_service": notify}
    )


@pytest.mark.parametrize(
    "notify", ["notify.not_there", "light.kitchen", "notify.", "notify", "phone"]
)
async def test_setup_refuses_a_notify_service_home_assistant_does_not_have(
    hass: HomeAssistant, notify: str
) -> None:
    async_mock_service(hass, "notify", "phone")
    result = await _setup_heads(hass, notify)
    assert result["type"] is FlowResultType.FORM
    assert (result["step_id"], result["errors"]) == ("user", ERROR)


@pytest.mark.parametrize("notify", ["notify.phone", ""])
async def test_setup_takes_a_notify_service_it_has_or_none(hass: HomeAssistant, notify: str) -> None:
    async_mock_service(hass, "notify", "phone")
    result = await _setup_heads(hass, notify)
    assert result["step_id"] == "rooms", result


async def test_reconfigure_refuses_a_notify_service_home_assistant_does_not_have(
    hass: HomeAssistant,
) -> None:
    async_mock_service(hass, "notify", "phone")
    entry = _entry(hass)
    result = await _reconfigure_heads(hass, entry, "notify.not_there")
    assert (result["step_id"], result["errors"]) == ("reconfigure", ERROR)
    result = await _reconfigure_heads(hass, entry, "notify.phone")
    assert result["step_id"] == "reconfigure_rooms", result
