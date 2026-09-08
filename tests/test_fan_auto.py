"""Focused Fan auto restore and registry-pruning controls.

These unit tests exercise the production switch and coordinator with a state
reader and service recorder. The HA restore/service lifecycle is covered in
test_climate.py; refresh scheduling is deliberately outside this unit boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.core import State

from custom_components.mxz_coordinator import _async_prune_stale_entities
from custom_components.mxz_coordinator.const import FAN_LADDER
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from custom_components.mxz_coordinator.switch import MXZZoneFanAutoSwitch

HEAD = "climate.test_head"


def _pending_restore(held: bool) -> tuple[MXZCoordinator, MXZZoneFanAutoSwitch]:
    """A live, supported head with boost off and unconsumed restore truth."""
    head = State(HEAD, "cool", {
        "supported_features": int(ClimateEntityFeature.FAN_MODE),
        "fan_modes": ["auto", "low", "high"],
        "fan_mode": "high",
    })
    coordinator = object.__new__(MXZCoordinator)
    coordinator.hass = SimpleNamespace(
        states=SimpleNamespace(get={HEAD: head}.get),
        services=SimpleNamespace(async_call=AsyncMock()),
    )
    coordinator.config_entry = SimpleNamespace(entry_id="test_entry")
    coordinator.last_update_success = True
    coordinator.fan_boost_enable = False
    coordinator.fan_boost_max = FAN_LADDER[-1]
    coordinator._fan_restore = {}
    coordinator._fan_latched = {}
    coordinator._fan_cmd = {}
    coordinator._fan_prev = {}
    coordinator._fan_idx = {}
    coordinator.async_request_refresh = AsyncMock()
    coordinator.restore_fan_hold(HEAD, held)
    switch = MXZZoneFanAutoSwitch(
        coordinator, SimpleNamespace(slug="primary", name="Primary", climate_id=HEAD)
    )
    switch.hass = coordinator.hass
    return coordinator, switch


@pytest.mark.parametrize(("held", "expected_on"), [(True, False), (False, True)])
async def test_pending_restore_display_with_boost_disabled(
    held: bool, expected_on: bool,
) -> None:
    """Held and unheld restores display correctly while fan seeding is inert."""
    coordinator, switch = _pending_restore(held)
    await coordinator._apply_fan(HEAD, "cool", 20.0)

    assert coordinator._fan_restore == {HEAD: held}  # the pending path is reached
    assert switch.available
    assert switch.is_on is expected_on, "pending restore must determine Fan auto display"
    coordinator.hass.services.async_call.assert_not_awaited()


async def test_handback_supersedes_pending_restore_with_boost_disabled() -> None:
    """An available switch accepts ON even before boost consumes its restore."""
    coordinator, switch = _pending_restore(True)
    assert switch.available
    assert switch.is_on is False

    await switch.async_turn_on()
    await coordinator._apply_fan(HEAD, "cool", 20.0)

    assert switch.is_on is True, "live Fan auto handback must supersede pending hold"
    assert coordinator.hass.states.get(HEAD).attributes["fan_mode"] == "high"
    coordinator.async_request_refresh.assert_awaited_once_with()
    coordinator.hass.services.async_call.assert_not_awaited()


def test_prune_keeps_retained_fan_auto_and_removes_dropped_zone() -> None:
    """Pruning itself keeps retained Fan auto entries, before HA can recreate them."""
    entry = SimpleNamespace(entry_id="test_entry")
    coordinator = SimpleNamespace(zones=[SimpleNamespace(slug="primary")])
    registry = Mock()
    entries = [
        SimpleNamespace(
            unique_id="test_entry_primary_fan_auto", entity_id="switch.retained_fan_auto"
        ),
        SimpleNamespace(
            unique_id="test_entry_dropped_fan_auto", entity_id="switch.dropped_fan_auto"
        ),
    ]
    with (
        patch("custom_components.mxz_coordinator.er.async_get", return_value=registry),
        patch(
            "custom_components.mxz_coordinator.er.async_entries_for_config_entry",
            return_value=entries,
        ),
    ):
        _async_prune_stale_entities(SimpleNamespace(), entry, coordinator)

    assert registry.async_remove.call_args_list == [call("switch.dropped_fan_auto")], (
        "pruning must remove the dropped zone and retain the current zone's Fan auto"
    )
