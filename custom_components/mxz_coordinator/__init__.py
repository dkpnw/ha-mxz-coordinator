"""The MXZ Coordinator integration.

A software control layer for Mitsubishi MXZ multi-zone mini-splits (multiple indoor
heads on ONE outdoor unit) that fixes the "idle head starves the other" AUTO deadlock
and gives each room a single Tesla-style comfort target.

This is the config-flow / HACS port of the original YAML package
(``packages/mxz_coordinator.yaml``). Behavior is preserved 1:1; the package's helpers
become integration-owned ``number``/``switch``/``select`` entities and the decision
``template`` sensor + actuator ``script`` + trigger/self-heal ``automation``s become the
``MXZCoordinator`` in ``coordinator.py``.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_DEMAND_THRESHOLD,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_PRIMARY_STAGE,
    CONF_PRIMARY_VANE_HORIZONTAL,
    CONF_PRIMARY_VANE_VERTICAL,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    CONF_SECONDARY_STAGE,
    CONF_SECONDARY_VANE_HORIZONTAL,
    CONF_SECONDARY_VANE_VERTICAL,
    CONF_ZONES,
    DOMAIN,
    PLATFORMS,
    SERVICE_RECOMPUTE,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
    ZONE_STAGE_SENSOR,
    ZONE_VANE_HORIZONTAL,
    ZONE_VANE_VERTICAL,
)
from .coordinator import MXZCoordinator

_LOGGER = logging.getLogger(__name__)

# Typed config entry: the coordinator lives on entry.runtime_data.
MXZConfigEntry = ConfigEntry[MXZCoordinator]


async def async_migrate_entry(hass: HomeAssistant, entry: MXZConfigEntry) -> bool:
    """Migrate a v1 (flat primary/secondary) entry to the v2 zones-list shape.

    Zones 0/1 keep the primary/secondary slugs, so no entity unique_id changes —
    existing installs migrate with zero registry churn.
    """
    if entry.version > 2:
        return False  # downgrade from a future version: bail
    if entry.version == 1:
        data = dict(entry.data)
        if CONF_ZONES not in data:
            data[CONF_ZONES] = [
                {
                    ZONE_NAME: "Primary",
                    ZONE_CLIMATE: data[CONF_PRIMARY_CLIMATE],
                    ZONE_SENSOR: data[CONF_PRIMARY_SENSOR],
                    ZONE_VANE_VERTICAL: data.get(CONF_PRIMARY_VANE_VERTICAL),
                    ZONE_VANE_HORIZONTAL: data.get(CONF_PRIMARY_VANE_HORIZONTAL),
                    ZONE_STAGE_SENSOR: data.get(CONF_PRIMARY_STAGE),
                },
                {
                    ZONE_NAME: "Secondary",
                    ZONE_CLIMATE: data[CONF_SECONDARY_CLIMATE],
                    ZONE_SENSOR: data[CONF_SECONDARY_SENSOR],
                    ZONE_VANE_VERTICAL: data.get(CONF_SECONDARY_VANE_VERTICAL),
                    ZONE_VANE_HORIZONTAL: data.get(CONF_SECONDARY_VANE_HORIZONTAL),
                    ZONE_STAGE_SENSOR: data.get(CONF_SECONDARY_STAGE),
                },
            ]
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info("Migrated MXZ Coordinator entry to the v2 zones format")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MXZConfigEntry) -> bool:
    """Set up MXZ Coordinator from a config entry."""
    # Visibility: options should carry the tunables, but the options flow mirrors
    # them into entry.data too (the coordinator reads {**data, **options}). If
    # options is empty yet the data mirror holds tunables, something cleared the
    # options out-of-band — surface it rather than silently running on defaults.
    if not entry.options and CONF_DEMAND_THRESHOLD in entry.data:
        _LOGGER.warning(
            "MXZ Coordinator options are empty but the data mirror has the config; "
            "recovering from the mirror. Something cleared this entry's options "
            "out-of-band — re-save the options once to re-populate them."
        )

    coordinator = MXZCoordinator(hass, entry)
    entry.runtime_data = coordinator

    # A reconfigure can shrink the zone list; prune registry entities from
    # dropped zones so they don't linger as unavailable ghosts.
    _async_prune_stale_entities(hass, entry, coordinator)

    # Build the helper entities first; the coordinator reads their (restored)
    # values, so it must not start applying until they exist. The kill-switch
    # defaults OFF, so the first refresh is a safe no-op regardless.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await coordinator.async_setup()

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _async_register_recompute_service(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MXZConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: MXZCoordinator = entry.runtime_data
    await coordinator.async_shutdown_listeners()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Drop the shared service once the last entry is gone.
    if unload_ok and not _other_entries_loaded(hass, entry):
        hass.services.async_remove(DOMAIN, SERVICE_RECOMPUTE)
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: MXZConfigEntry) -> None:
    """Reload the entry when its options (tunable constants) change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _other_entries_loaded(hass: HomeAssistant, entry: MXZConfigEntry) -> bool:
    """Return True if any OTHER loaded entry of this domain remains."""
    return any(
        e.entry_id != entry.entry_id and e.state.recoverable
        for e in hass.config_entries.async_entries(DOMAIN)
    )


def _async_prune_stale_entities(
    hass: HomeAssistant, entry: MXZConfigEntry, coordinator: MXZCoordinator
) -> None:
    """Remove registry entities this entry no longer provides (dropped zones)."""
    valid = {f"{entry.entry_id}_{key}" for key in (
        "coordinator_enable", "eco_idle", "heat_lockout", "cool_lockout",
        "shared_mode", "plan",
    )}
    for zone in coordinator.zones:
        for suffix in ("target", "enable", "thermostat"):
            valid.add(f"{entry.entry_id}_{zone.slug}_{suffix}")
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id not in valid:
            _LOGGER.info("Pruning stale entity %s (dropped zone)", entity.entity_id)
            registry.async_remove(entity.entity_id)


def _async_register_recompute_service(hass: HomeAssistant) -> None:
    """Register the domain-wide ``mxz_coordinator.recompute`` service once."""
    if hass.services.has_service(DOMAIN, SERVICE_RECOMPUTE):
        return

    async def _handle_recompute(call: ServiceCall) -> None:
        """Force every loaded coordinator to recompute and re-apply now."""
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coordinator: MXZCoordinator = entry.runtime_data
            await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_RECOMPUTE, _handle_recompute)
