"""Download diagnostics: what MXZ was configured with, and what it knows right now.

Read-only. Nothing here calls into the decision path, except the room view and hold
readings that the plan sensor and thermostats already read on every state write.

It never raises: each section, and each part of a room, is guarded on its own, so a
broken one reads ``{"error": "<ExceptionType>: message"}`` and the rest still download.

Redaction runs on the finished document, after everything is plain JSON data, in
three layers: ``async_redact_data`` over every key that names something sensitive,
then the values those keys held wherever else they appear, then any e-mail address,
IP address, or MAC address left in free text such as an error message. Room names and
entity IDs stay: they are what makes a report readable, and the README says so.
"""

from __future__ import annotations

import ipaddress
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from enum import Enum
from typing import Any

from homeassistant.components.climate.const import ATTR_HVAC_ACTION
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .capabilities import head_capabilities
from .const import DOMAIN

# Keys whose values never leave, matched at any depth after normalizing (lower case,
# snake case), so "Password", "API-Key", and "macAddress" count too.
TO_REDACT = frozenset(
    {
        # Credentials
        "access_token", "api_key", "api_token", "apikey", "auth", "authorization",
        "bearer", "client_id", "client_secret", "cookie", "credentials", "id_token",
        "pass", "passwd", "password", "pin", "refresh_token", "secret", "session",
        "token", "webhook_id",
        # People and cloud accounts
        "account", "account_id", "account_number", "cloud_account", "cloud_id",
        "cloud_user", "customer_id", "email", "email_address", "home_id", "login",
        "user", "user_id", "user_name", "username",
        # Devices and networks
        "address", "bssid", "connections", "device_id", "external_url", "host",
        "hostname", "internal_url", "ip", "ip_address", "ipaddress", "mac",
        "mac_address", "macaddress", "serial", "serial_number", "serialnumber", "sn",
        "ssid", "url",
        # Location
        "elevation", "gps", "lat", "latitude", "lng", "location", "lon", "long",
        "longitude",
        # MXZ's own: a notify service is usually named after a person's phone
        "notify_service", "opening_notify_service",
    }
)
# A key with any of these words is sensitive whatever else it says: wifi_password,
# cloud_account_id, device_serial.
_SENSITIVE_WORDS = frozenset(
    {
        "account", "credential", "credentials", "email", "ip", "latitude", "longitude",
        "mac", "passwd", "password", "secret", "serial", "token", "username",
    }
)
_KEY_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_LIKE = re.compile(r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")
_MASK = "**REDACTED**"
# A shorter value is too likely to be part of ordinary words to replace everywhere.
_MIN_SWEPT = 4
_MAX_DEPTH = 32


def _error(err: Exception) -> dict[str, str]:
    return {"error": f"{type(err).__name__}: {err}"}


def _guard(read: Callable[..., Any], *args: Any) -> Any:
    """One section's value, or its error."""
    try:
        return read(*args)
    except Exception as err:  # noqa: BLE001 - a broken part must not fail the download
        return _error(err)


async def _guard_async(read: Callable[..., Awaitable[Any]], *args: Any) -> Any:
    try:
        return await read(*args)
    except Exception as err:  # noqa: BLE001 - as _guard
        return _error(err)


# -- plain JSON data ---------------------------------------------------------
def _jsonable(value: Any, depth: int = 0) -> Any:
    """``value`` as data ``json.dumps(allow_nan=False)`` accepts."""
    if depth > _MAX_DEPTH:
        return "<nested too deeply>"
    if isinstance(value, Enum):
        return _jsonable(value.value, depth + 1)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Mapping):
        return {str(_jsonable(k, depth + 1)): _jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(v, depth + 1) for v in value), key=str)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, depth + 1) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name), depth + 1) for f in fields(value)}
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - as _guard
        return f"<unprintable {type(value).__name__}>"


# -- redaction ---------------------------------------------------------------
def _is_sensitive(key: str) -> bool:
    words = [word.lower() for word in _KEY_WORD.findall(key)]
    return (
        "_".join(words) in TO_REDACT
        or not _SENSITIVE_WORDS.isdisjoint(words)
        or "apikey" in "".join(words)
    )


def _leaves(value: Any) -> set[str]:
    """Every value under a sensitive key, as text."""
    if isinstance(value, dict):
        return set().union(*map(_leaves, value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*map(_leaves, value)) if value else set()
    return set() if value is None or isinstance(value, bool) else {str(value)}


def _find_sensitive(value: Any) -> tuple[set[str], set[str]]:
    """The sensitive keys in this data, and the values they hold."""
    keys: set[str] = set()
    secrets: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_sensitive(key):
                keys.add(key)
                secrets |= _leaves(item)
            else:
                more_keys, more_secrets = _find_sensitive(item)
                keys |= more_keys
                secrets |= more_secrets
    elif isinstance(value, list):
        for item in value:
            more_keys, more_secrets = _find_sensitive(item)
            keys |= more_keys
            secrets |= more_secrets
    return keys, secrets


def _mask_ipv6(match: re.Match[str]) -> str:
    # "21:49:00" looks like one until it is parsed.
    try:
        ipaddress.IPv6Address(match.group(0))
    except ValueError:
        return match.group(0)
    return _MASK


def _scrub_text(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, _MASK)
    text = _EMAIL.sub(_MASK, text)
    text = _MAC.sub(_MASK, text)
    text = _IPV4.sub(_MASK, text)
    return _IPV6_LIKE.sub(_mask_ipv6, text)


def _scrub(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return _scrub_text(value, secrets)
    if isinstance(value, dict):
        return {_scrub_text(k, secrets): _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    return value


def _finished(document: Any) -> dict[str, Any]:
    """The document as plain data with everything sensitive gone."""
    data = _jsonable(document)
    keys, found = _find_sensitive(data)
    redacted = async_redact_data(data, TO_REDACT | keys)
    secrets = sorted((s for s in found if len(s) >= _MIN_SWEPT), key=len, reverse=True)
    return _scrub(redacted, secrets)


# -- sections ----------------------------------------------------------------
async def _integration(hass: HomeAssistant) -> dict[str, Any]:
    integration = await async_get_integration(hass, DOMAIN)
    return {"domain": DOMAIN, "version": integration.version}


def _home_assistant(hass: HomeAssistant) -> dict[str, Any]:
    return {"version": HA_VERSION, "temperature_unit": hass.config.units.temperature_unit}


def _config_entry(entry: ConfigEntry) -> dict[str, Any]:
    return {
        "title": entry.title,
        "version": entry.version,
        "minor_version": entry.minor_version,
        "state": entry.state,
        "source": entry.source,
        "disabled_by": entry.disabled_by,
        "created_at": entry.created_at,
        "modified_at": entry.modified_at,
        "data": dict(entry.data),
        "options": dict(entry.options),
    }


def _repair_issues(hass: HomeAssistant, entry_id: str) -> list[dict[str, Any]]:
    return [
        {
            "issue_id": issue.issue_id,
            "translation_key": issue.translation_key,
            "severity": issue.severity,
            "active": issue.active,
            "created": issue.created,
            "dismissed_version": issue.dismissed_version,
            "translation_placeholders": issue.translation_placeholders,
        }
        for (domain, _id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and entry_id in issue.issue_id
    ]


def _loaded_coordinator(entry: ConfigEntry) -> Any:
    """The running coordinator, or a reason it isn't there."""
    if entry.state is not ConfigEntryState.LOADED:
        raise LookupError(f"coordinator not loaded (entry state: {entry.state.value})")
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        raise LookupError("coordinator not loaded (no runtime data)")
    return coordinator


def _wall(stamp: float | None) -> str | None:
    """A stored wall-clock time as ISO 8601."""
    if stamp is None:
        return None
    return dt_util.utc_from_timestamp(stamp).isoformat()


def _ago(stamp: float | None, now: float) -> float | None:
    """Seconds since a monotonic stamp: its absolute value means nothing to a reader."""
    return None if stamp is None else round(now - stamp, 1)


def _coordinator(coordinator: Any) -> dict[str, Any]:
    plan = coordinator.data or {}
    elapsed = coordinator._mode_change_elapsed()
    record = coordinator._mode_record
    return {
        "retired": coordinator._retired,
        "plan_ready": coordinator.plan_ready,
        "last_update_success": coordinator.last_update_success,
        "started_at": _wall(coordinator._started_ts),
        "enabled": coordinator.coordinator_enable,
        "shared_mode": coordinator.current_shared_mode,
        "eco_idle": coordinator.eco_idle,
        "heat_lockout": coordinator.heat_lockout,
        "cool_lockout": coordinator.cool_lockout,
        "inhibited": coordinator.inhibited,
        "inhibit_action": coordinator.inhibit_action,
        "idle_action": coordinator.idle_action,
        "plan": {key: value for key, value in plan.items() if key != "zones"},
        "anti_flip": {
            "minimum_seconds_between_mode_changes": coordinator.hysteresis,
            "last_mode_change_at": _wall(coordinator._last_mode_change_at),
            "seconds_since_mode_change": None if elapsed is None else round(elapsed, 1),
            "mode_by_votes": coordinator._mode_by_votes,
            **coordinator.mode_hold_attributes(),
        },
        "persistence": {
            "stored_last_mode_change_at": _wall(record.changed_at),
            "stored_mode": record.mode,
            "stored_mode_at": _wall(record.mode_at) if record.mode is not None else None,
            "stored_mode_by_votes": record.by_votes,
            "restored_coordinator_on": coordinator._restored_coordinator_on,
        },
    }


# Zone fields that name an entity, under the names a report reads them by.
_ENTITY_FIELDS = {
    "head": "climate_id",
    "temperature_sensor": "sensor_id",
    "vane_vertical": "vane_vertical_id",
    "vane_horizontal": "vane_horizontal_id",
    "airflow_sensor": "stage_sensor_id",
    "safety_priority": "safety_priority_entity",
    "comfort_priority": "comfort_priority_entity",
    "door_window": "opening_entity",
}


def _entity_ids(zone: Any) -> dict[str, str | None]:
    return {label: getattr(zone, name) for label, name in _ENTITY_FIELDS.items()}


def _settings(zone: Any) -> dict[str, Any]:
    named = {*_ENTITY_FIELDS.values(), "name", "slug", "index", "head_type"}
    return {f.name: getattr(zone, f.name) for f in fields(zone) if f.name not in named}


def _mode(coordinator: Any, zone: Any) -> dict[str, Any]:
    head = coordinator.hass.states.get(zone.climate_id)
    if head is None:
        return {"head_state": None}
    return {
        "head_state": head.state,
        "hvac_action": head.attributes.get(ATTR_HVAC_ACTION),
        "last_changed": head.last_changed,
        "last_updated": head.last_updated,
    }


def _capabilities(coordinator: Any, zone: Any) -> dict[str, Any]:
    known = head_capabilities(coordinator.hass, zone.climate_id)
    head = coordinator.hass.states.get(zone.climate_id)
    return {
        "hvac_modes": known.hvac_modes,
        "fan_modes": known.fan_modes,
        "supported_features": None if head is None else head.attributes.get(ATTR_SUPPORTED_FEATURES),
    }


def _votes(coordinator: Any, zone: Any, now: float) -> dict[str, Any]:
    sight = coordinator._sight[zone.climate_id]
    ttl = coordinator.lost_vote_ttl
    return {
        "basis": zone.vote_basis,
        "status": sight.status(now, ttl),
        "vote": sight.vote(now, ttl),
        "action_vote": sight.action_vote(now, ttl, coordinator.action_vote_hold),
        "seen_mode": sight.mode,
        "seen_action": sight.action,
        "reports_action": sight.reports_action,
        "ran": sight.ran,
        "lost_since": _wall(sight.lost_wall),
        "seconds_since_lost": _ago(sight.lost_at, now),
        "seconds_since_stopped": _ago(sight.stopped_at, now),
        "seconds_since_mode_changed": _ago(sight.changed_at, now),
    }


def _holds(coordinator: Any, zone: Any) -> dict[str, Any]:
    run = coordinator._floor_runs.get(zone.slug)
    return {
        **coordinator.mode_hold_attributes(),
        "hands_off": zone.hands_off,
        "hands_off_released": zone.slug in coordinator._hands_off_released,
        "dry": coordinator._dry_rooms.get(zone.slug),
        "dry_pick_pending": zone.slug in coordinator._dry_picks,
        "dry_released": zone.slug in coordinator._dry_released,
        "floor_run": run,
        "floor_yield": coordinator._floor_yield.get(zone.slug),
        "floor_rejecting": coordinator._floor_rejecting.get(zone.slug),
        "vane_kick_running": zone.climate_id in coordinator._vane_kicks,
    }


def _manual_fan(coordinator: Any, zone: Any) -> dict[str, Any]:
    head = zone.climate_id
    return {
        "latched": coordinator._fan_latched.get(head, False),
        "commanded": coordinator._fan_cmd.get(head),
        "previous": coordinator._fan_prev.get(head),
        "ladder_index": coordinator._fan_idx.get(head),
        "turn_on_pending": head in coordinator._fan_on_pending,
        "restored_latch": coordinator._fan_restore.get(head),
        "restore_problem": coordinator._fan_restore_problem.get(head),
    }


def _window_pause(coordinator: Any, zone: Any) -> dict[str, Any]:
    return {
        "active": zone.opening_active,
        "record": coordinator._openings.get(zone.slug),
        **coordinator.opening_attributes(zone),
    }


def _anti_flip(coordinator: Any, zone: Any, now: float) -> dict[str, Any]:
    last_active = coordinator._last_active.get(zone.climate_id)
    return {
        "engage_latch": coordinator._engage_latch.get(zone.slug),
        "startup_resume": coordinator._startup_resume.get(zone.slug),
        "last_active_mode": None if last_active is None else last_active[0],
        "seconds_since_last_active": None if last_active is None else _ago(last_active[1], now),
    }


def _sensor_health(coordinator: Any, zone: Any) -> dict[str, Any]:
    slug = zone.slug
    window = coordinator._freshness.get(slug)
    return {
        "health": coordinator._health.get(slug),
        "maximum_age_seconds": None if window is None else window[0],
        "startup_grace_seconds": None if window is None else window[1],
        "evidence": coordinator._evidence.get(slug),
        "last_evidence_at": _wall(coordinator._evidence_ts.get(slug)),
        "grace_until": _wall(coordinator._grace_until.get(slug)),
        "fresh_until": _wall(coordinator._fresh_deadline.get(slug)),
    }


def _room(coordinator: Any, zone: Any, last_decision: Any, now: float) -> dict[str, Any]:
    return {
        "name": zone.name,
        "index": zone.index,
        "head_type": zone.head_type,
        "entity_ids": _guard(_entity_ids, zone),
        "settings": _guard(_settings, zone),
        "mode": _guard(_mode, coordinator, zone),
        "capabilities": _guard(_capabilities, coordinator, zone),
        "votes": _guard(_votes, coordinator, zone, now),
        "holds": _guard(_holds, coordinator, zone),
        "manual_fan": _guard(_manual_fan, coordinator, zone),
        "window_pause": _guard(_window_pause, coordinator, zone),
        "anti_flip": _guard(_anti_flip, coordinator, zone, now),
        "sensor_health": _guard(_sensor_health, coordinator, zone),
        "cloud": coordinator._cloud.get(zone.climate_id),
        "last_decision": last_decision,
    }


def _rooms(coordinator: Any) -> dict[str, Any]:
    now = time.monotonic()
    details = _guard(coordinator.room_details)
    rooms: dict[str, Any] = {}
    for index, zone in enumerate(coordinator.zones):
        row = details[index] if isinstance(details, list) and index < len(details) else details
        rooms[zone.slug] = _guard(_room, coordinator, zone, row, now)
    return rooms


def _device(device: dr.DeviceEntry) -> dict[str, Any]:
    # Named fields only: a device's identifiers and connections can hold a serial or MAC.
    return {
        "name": device.name,
        "name_by_user": device.name_by_user,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "sw_version": device.sw_version,
        "hw_version": device.hw_version,
        "disabled_by": device.disabled_by,
    }


async def _document(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    entry_id = _guard(lambda: entry.entry_id)
    coordinator = _guard(_loaded_coordinator, entry)
    loaded = not (isinstance(coordinator, dict) and "error" in coordinator)
    return {
        "generated_at": dt_util.utcnow(),
        "integration": await _guard_async(_integration, hass),
        "home_assistant": _guard(_home_assistant, hass),
        "config_entry": _guard(_config_entry, entry),
        "coordinator": _guard(_coordinator, coordinator) if loaded else coordinator,
        "rooms": _guard(_rooms, coordinator) if loaded else coordinator,
        "repair_issues": _guard(_repair_issues, hass, entry_id),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Diagnostics for the config entry."""
    try:
        return _finished(await _document(hass, entry))
    except Exception as err:  # noqa: BLE001 - as _guard
        # The redaction itself failed, so nothing else is safe to include, not
        # even the message.
        return {"error": type(err).__name__}


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> dict[str, Any]:
    """Diagnostics for the entry's device: the same document, and the device."""
    try:
        return _finished(
            {**await _document(hass, entry), "device": _guard(_device, device)}
        )
    except Exception as err:  # noqa: BLE001 - as _guard
        return {"error": type(err).__name__}
