"""Read and replace Home Assistant's restore records, on every supported core.

Home Assistant 2026.11 removed ``RestoreStateData.last_states``, one dict keyed by
entity_id. A record now sits in ``last_states_by_entity_registry_id`` when its
entity is in the entity registry and in ``last_states_by_entity_id`` otherwise,
and ``async_get_stored_state`` finds it either way. These helpers address the
record that restore hands an entity, so a test reads and plants exactly what
the integration will be given, on cores before and after that change.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state as rs


def _by_entity_id(restore: rs.RestoreStateData) -> bool:
    """True on cores that still keep every record under its entity_id."""
    return hasattr(restore, "last_states")


def find_stored_state(hass: HomeAssistant, entity_id: str) -> rs.StoredState | None:
    """The record restore would hand ``entity_id``, or None when it has none."""
    restore = rs.async_get(hass)
    if _by_entity_id(restore):
        return restore.last_states.get(entity_id)
    return restore.async_get_stored_state(entity_id)


def stored_state(hass: HomeAssistant, entity_id: str) -> rs.StoredState:
    """The record restore would hand ``entity_id``; KeyError when it has none."""
    if (stored := find_stored_state(hass, entity_id)) is None:
        raise KeyError(entity_id)
    return stored


def replace_stored_state(
    hass: HomeAssistant, entity_id: str, stored: rs.StoredState
) -> None:
    """Make ``stored`` the record restore hands ``entity_id``.

    On a core that indexes by registry entry, a registered entity's record is
    filed under its entry's id, as HA's own load files it.
    """
    restore = rs.async_get(hass)
    if _by_entity_id(restore):
        restore.last_states[entity_id] = stored
        return
    if (entry := er.async_get(hass).async_get(entity_id)) is None:
        restore.last_states_by_entity_id[entity_id] = stored
        return
    stored.entity_registry_id = entry.id
    restore.last_states_by_entity_registry_id[entry.id] = stored


def set_stored_states(hass: HomeAssistant, states: dict[str, rs.StoredState]) -> None:
    """Make ``states`` the whole of restore's memory, every other record dropped."""
    restore = rs.async_get(hass)
    if _by_entity_id(restore):
        restore.last_states = dict(states)
        return
    restore.last_states_by_entity_id = {}
    restore.last_states_by_entity_registry_id = {}
    for entity_id, stored in states.items():
        replace_stored_state(hass, entity_id, stored)
