"""Config and options flow for MXZ Coordinator."""

from __future__ import annotations

import math
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfTemperature
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .capabilities import head_mode_problem, supported_idle_actions
from .const import (
    CONF_CHANGEOVER_COOL_BELOW,
    CONF_CHANGEOVER_ENTITY,
    CONF_CHANGEOVER_HEAT_ABOVE,
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    CONF_COIL_DRY_MINUTES,
    CONF_COOL_LOCKOUT_CEILING,
    CONF_DEMAND_THRESHOLD,
    CONF_ECO_COOL_MAX,
    CONF_ECO_HEAT_MIN,
    CONF_ENGAGE_DEADBAND,
    CONF_FAN_BOOST_ENABLE,
    CONF_FAN_BOOST_MAX,
    CONF_HEAT_LOCKOUT_FLOOR,
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ACTIVE_STATE,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_PRIMARY_CLIMATE,
    CONF_RESTING_MODE_BIAS,
    CONF_SECONDARY_CLIMATE,
    CONF_ZONES,
    DEFAULT_CHANGEOVER_COOL_BELOW,
    DEFAULT_CHANGEOVER_HEAT_ABOVE,
    DEFAULT_CLAMP_MAX,
    DEFAULT_CLAMP_MIN,
    DEFAULT_COIL_DRY_MINUTES,
    DEFAULT_COOL_LOCKOUT_CEILING,
    DEFAULT_DEMAND_THRESHOLD,
    DEFAULT_ECO_COOL_MAX,
    DEFAULT_ECO_HEAT_MIN,
    DEFAULT_ENGAGE_DEADBAND,
    DEFAULT_FAN_BOOST_ENABLE,
    DEFAULT_FAN_BOOST_MAX,
    DEFAULT_HEAT_LOCKOUT_FLOOR,
    DEFAULT_IDLE_ACTION,
    DEFAULT_INHIBIT_ACTION,
    DEFAULT_INHIBIT_ACTIVE_STATE,
    DEFAULT_MODE_HYSTERESIS,
    DEFAULT_RESTING_MODE_BIAS,
    DOMAIN,
    EVIDENCE_BASES,
    EVIDENCE_SAMPLE_TIMESTAMP,
    FAN_LADDER,
    IDLE_ACTION_OPTIONS,
    INHIBIT_ACTION_OPTIONS,
    MAX_ZONES,
    MIN_ZONES,
    RESTING_BIAS_OPTIONS,
    UNAVAILABLE_STATES,
    ZONE_CLIMATE,
    ZONE_ENTITY_SUFFIXES,
    ZONE_EVIDENCE_BASIS,
    ZONE_MAX_AGE,
    ZONE_NAME,
    ZONE_REPORT_INTERVAL,
    ZONE_SAMPLE_SEQUENCE_ATTR,
    ZONE_SAMPLE_TIMESTAMP_ATTR,
    ZONE_SENSOR,
    ZONE_STAGE_SENSOR,
    ZONE_STARTUP_GRACE,
    ZONE_VANE_HORIZONTAL,
    ZONE_VANE_VERTICAL,
    unit_profile,
    zone_slug,
)
from .coordinator import read_room_temp
from .logic import evidence_contract, freshness_window

_CLIMATE_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="climate")
)
_SENSOR_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
)
_VANE_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="select")
)
# The stage/airflow sensor is a sensor (ESPHome text_sensors register in the
# `sensor` domain), so a sensor picker covers it.
_STAGE_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor")
)
_ROOM_NAME_SELECTOR = selector.TextSelector()

# Reserved slug for a record parked mid-reorder. zone_slug() only ever returns
# "primary", "secondary" or "zone_N", so this can collide with no real room.
_PARKED_SLUG = "reordering"


def _notify_options(hass: HomeAssistant) -> list[str]:
    """Available `notify.*` targets, for a friendly drift-alert dropdown."""
    return sorted(f"notify.{name}" for name in hass.services.async_services().get("notify", {}))


def _detect_vanes(hass: HomeAssistant, climate_id: str) -> dict[str, str]:
    """Best-effort vane `select` entities on the SAME device as ``climate_id``.

    The CN105/ESPHome head and its vertical/horizontal vane selects live on one
    device, so we can infer them from the chosen head instead of asking the user.
    Returns {"vertical": eid, "horizontal": eid} for whatever is found.
    """
    reg = er.async_get(hass)
    entry = reg.async_get(climate_id)
    if entry is None or entry.device_id is None:
        return {}
    found: dict[str, str] = {}
    for e in er.async_entries_for_device(reg, entry.device_id, include_disabled_entities=True):
        if e.domain != "select":
            continue
        text = f"{e.entity_id} {e.original_name or ''} {e.name or ''}".lower()
        if "vane" not in text and "swing" not in text:
            continue
        if "horizontal" in text:
            found.setdefault("horizontal", e.entity_id)
        elif "vertical" in text:
            found.setdefault("vertical", e.entity_id)
        else:
            found.setdefault("vertical", e.entity_id)  # lone unlabeled vane -> vertical
    return found


def _detect_stage(hass: HomeAssistant, climate_id: str) -> str | None:
    """Best-effort actual-airflow (`stage`) sensor on the head's OWN device.

    CN105/ESPHome heads publish the decoded blower speed as a `stage`
    text_sensor (registers in the `sensor` domain) on the same device as the
    climate entity, so we can infer it from the chosen head — exactly like
    ``_detect_vanes``. Conservative: require the literal word "stage" in the
    entity_id or name so we never mistake an unrelated sensor for airflow.
    Returns the entity_id, or None if nothing qualifies.
    """
    reg = er.async_get(hass)
    entry = reg.async_get(climate_id)
    if entry is None or entry.device_id is None:
        return None
    for e in er.async_entries_for_device(reg, entry.device_id, include_disabled_entities=True):
        if e.domain != "sensor":
            continue
        text = f"{e.entity_id} {e.original_name or ''} {e.name or ''}".lower()
        if "stage" in text:
            return e.entity_id
    return None


# Flow-local key for the multi-head picker on the first step.
_CONF_HEADS = "heads"
_CONTEXT_HEADS = "mxz_selected_heads"
_CONTEXT_ENTRY_ID = "mxz_reconfigure_entry_id"

# The config entry's own title. Flow-input only: a title is not a unique_id,
# not an option key and not stored in entry.data, so naming the outdoor unit
# cannot move an entity or change what the coordinator does.
_CONF_ENTRY_TITLE = "entry_title"
DEFAULT_ENTRY_TITLE = "MXZ Coordinator"


def _entry_head_order(entry: ConfigEntry) -> list[str]:
    """Heads explicitly stored by one v1 or v2 entry, in PRIORITY order.

    The order is the slot order, and the slot is where a room's entity
    unique_ids come from (``zone_slug``), so a reorder needs the sequence and
    not just the membership.
    """
    zones = entry.data.get(CONF_ZONES)
    if isinstance(zones, list) and zones:
        return [
            climate_id
            for zone in zones
            if isinstance(zone, dict)
            and isinstance((climate_id := zone.get(ZONE_CLIMATE)), str)
        ]
    return [
        climate_id
        for key in (CONF_PRIMARY_CLIMATE, CONF_SECONDARY_CLIMATE)
        if isinstance((climate_id := entry.data.get(key)), str)
    ]


def _entry_heads(entry: ConfigEntry) -> set[str]:
    """Return heads explicitly stored by one v1 or v2 coordinator entry."""
    return set(_entry_head_order(entry))


def _room_name_key(index: int) -> str:
    """Flow-input-only room-name field for priority slot ``index``."""
    return f"room_name_{index + 1}"


def _resolve_room_name(submitted: Any, fallback: str) -> str:
    """Turn one submitted room-name field into the name that will be stored.

    An ABSENT key is not an edit: the room keeps the name it already had. A
    submitted EMPTY string is the user clearing the box, and stays empty here
    so each step can apply its own rule — setup refuses it (a room must be
    named before it exists), reconfigure reads it as "go back to the head's own
    name", which is the behaviour its help text has always promised.
    """
    if submitted is None:
        return fallback
    return str(submitted).strip()


def _room_name_problem(names: list[str]) -> tuple[str, dict[str, str]] | None:
    """Reject an unnamed room, or two rooms a picker could not tell apart.

    Duplicate names break no invariant — entity unique_ids come from the
    priority slot, never the name — but two identically named thermostats are
    indistinguishable in every Home Assistant picker, which is the problem this
    step exists to prevent.
    """
    if any(not name for name in names):
        return "room_name_empty", {}
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in names:
        folded = name.casefold()
        if folded in seen:
            if name not in duplicates:
                duplicates.append(name)
        else:
            seen.add(folded)
    if duplicates:
        return "duplicate_room_names", {"problem_names": ", ".join(duplicates)}
    return None


def _sensor_unit_supported(state: State) -> bool:
    """Whether ``read_room_temp`` could ever read this sensor's unit.

    Deliberately the same rule as the shared reader (``coordinator.py``): an
    ABSENT or explicitly null unit means the system unit, any other non-string
    is malformed, and a string must be a temperature unit Home Assistant can
    convert. Anything stricter would refuse sensors the coordinator reads
    correctly — kelvin is the case that catches people out.
    """
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit is None:
        return True
    if not isinstance(unit, str):
        return False
    return unit in TemperatureConverter.VALID_UNITS


def _sensor_problem(
    hass: HomeAssistant, sensors: list[str]
) -> tuple[str, dict[str, str]] | None:
    """Describe a PERMANENT room-sensor misconfiguration, for a flow error.

    Only conditions that retrying later cannot fix belong here, because a
    config flow's only feedback channel blocks the whole install: two rooms
    sharing one thermometer, an entity that does not exist, and a unit the
    coordinator will always refuse. A sensor that is merely asleep or
    momentarily non-numeric is reported on the review step instead
    (``_sensor_status``), because blocking on it would make a legitimate
    install impossible at the wrong hour — and because the coordinator itself
    treats those as a temporarily neutral room, not a fatal entry.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for entity_id in sensors:
        if entity_id in seen and entity_id not in duplicates:
            duplicates.append(entity_id)
        seen.add(entity_id)
    if duplicates:
        return "duplicate_sensors", {"problem_sensors": ", ".join(duplicates)}

    states = {entity_id: hass.states.get(entity_id) for entity_id in sensors}
    missing = [entity_id for entity_id, state in states.items() if state is None]
    if missing:
        return "sensor_missing", {"problem_sensors": ", ".join(missing)}

    unsupported = [
        entity_id
        for entity_id, state in states.items()
        if state is not None and not _sensor_unit_supported(state)
    ]
    if unsupported:
        return "sensor_unit_unsupported", {"problem_sensors": ", ".join(unsupported)}
    return None


def _relative_age(state: State) -> str:
    """Plain-English age of one state report.

    The ONE age formatter this flow has, so the sensors step and the review
    step never describe the same reading two different ways.
    """
    reported = getattr(state, "last_reported", None) or state.last_updated
    seconds = max(0.0, (dt_util.utcnow() - reported).total_seconds())
    for limit, size, unit in (
        (60.0, 1.0, "second"),
        (3600.0, 60.0, "minute"),
        (86400.0, 3600.0, "hour"),
    ):
        if seconds < limit:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    count = int(seconds // 86400.0)
    return f"{count} day{'' if count == 1 else 's'} ago"


def _sensor_status(hass: HomeAssistant, entity_id: str, system_unit: str) -> str:
    """One line describing what this room's sensor is reporting right now.

    Says what the coordinator would read, using the coordinator's own reader,
    so the flow can never promise a number the control loop rejects. The
    recovery sentence is attached here rather than raised as an error: the room
    is neutral until a valid number arrives, and saying so is more use than
    refusing the install.
    """
    recovery = (
        ' Fix the sensor, or pick another one with "Go back to heads and rooms".'
        " Until it reports a valid number this room makes no automatic demand."
    )
    state = hass.states.get(entity_id)
    if state is None:
        return f"{entity_id} — Home Assistant has no state for this entity.{recovery}"
    value = read_room_temp(state, system_unit)
    if value is None:
        reason = (
            "unavailable"
            if state.state in UNAVAILABLE_STATES
            else "not reporting a number"
        )
        return f"{entity_id} — {reason}.{recovery}"
    return f"{entity_id} — {value:g} {system_unit}, {_relative_age(state)}"


@callback
def _async_move_room_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    old_heads: list[str],
    new_heads: list[str],
) -> list[str]:
    """Re-key each moved room's entities onto that room's NEW priority slot.

    A room's target, drift, enable and Fan-auto hold are restored by its own
    entities, and those entities are keyed by the priority SLOT (``zone_slug``).
    Reordering the heads therefore hands every room the settings of whoever
    used to sit in its new position — a silent swap, because each value on its
    own is plausible. Moving the registry records with the room fixes that at
    the identity level: the record keeps its entity_id, its registry id and the
    customizations HA cannot rebuild (name, area, disabled flag), and only the
    slot-derived unique_id changes, so the room's stored values restore onto
    the room they belong to.

    Two passes, because a swap is a cycle: park every moving record at a
    reserved id first, then place it. Records left parked belong to rooms this
    save removed; ``_async_prune_stale_entities`` deletes them on the setup
    that follows, which is where removal is documented and tested.

    Returns the entity_ids that were moved (empty when nothing was reordered).
    """
    registry = er.async_get(hass)
    by_unique_id = {
        entity.unique_id: entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    new_slot = {head: index for index, head in enumerate(new_heads)}
    parked: list[tuple[str, str, str]] = []
    for old_index, head in enumerate(old_heads):
        if new_slot.get(head) == old_index:
            continue  # this room stayed put: leave its records untouched
        for suffix in ZONE_ENTITY_SUFFIXES:
            entity_id = by_unique_id.get(
                f"{entry.entry_id}_{zone_slug(old_index)}_{suffix}"
            )
            if entity_id is None:
                continue  # never registered, or already removed by hand
            registry.async_update_entity(
                entity_id,
                new_unique_id=f"{entry.entry_id}_{_PARKED_SLUG}_{old_index}_{suffix}",
            )
            parked.append((entity_id, head, suffix))

    moved: list[str] = []
    for entity_id, head, suffix in parked:
        if (new_index := new_slot.get(head)) is None:
            continue  # this room is gone; its parked record is pruned at setup
        registry.async_update_entity(
            entity_id,
            new_unique_id=f"{entry.entry_id}_{zone_slug(new_index)}_{suffix}",
        )
        moved.append(entity_id)
    return moved


def _head_conflicts(
    hass: HomeAssistant,
    heads: list[str],
    *,
    exclude_entry_id: str | None = None,
    exclude_flow_id: str | None = None,
    grandfathered_heads: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return separately committed and in-progress head conflicts.

    A reconfigure may retain heads already stored by its target entry even when
    an older installation also stored them elsewhere. It may not add another
    entry's head. Reservations give earlier feedback between open flows; the
    committed-entry check at the final step remains authoritative.
    """
    committed = set().union(
        *(
            _entry_heads(entry)
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id != exclude_entry_id
        )
    )
    committed.difference_update(grandfathered_heads or ())

    reserved: set[str] = set()
    for progress in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        if progress["flow_id"] == exclude_flow_id:
            continue
        context = progress["context"]
        if (
            exclude_entry_id is not None
            and context.get(_CONTEXT_ENTRY_ID) == exclude_entry_id
        ):
            continue
        reserved.update(context.get(_CONTEXT_HEADS, ()))

    return (
        [head for head in heads if head in committed],
        [head for head in heads if head in reserved and head not in committed],
    )


def _conflict_error(
    conflicts: tuple[list[str], list[str]],
) -> tuple[str, list[str]] | None:
    """Choose the truthful error and heads for one submitted selection."""
    committed, reserved = conflicts
    if committed:
        return "heads_already_configured", committed
    if reserved:
        return "heads_reserved_by_flow", reserved
    return None


def _conflict_placeholders(conflicts: list[str]) -> dict[str, str]:
    """Build the translated error value that names every conflicting head."""
    return {"conflicting_heads": ", ".join(conflicts)}


def _user_schema(
    notify_options: list[str],
    default_heads: list[str] | None = None,
    default_notify: str | None = None,
    default_title: str | None = None,
) -> vol.Schema:
    notify_selector: selector.Selector = (
        selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=notify_options,
                custom_value=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
        if notify_options
        else selector.TextSelector()
    )
    return vol.Schema(
        {
            vol.Required(_CONF_HEADS, default=default_heads or []): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate", multiple=True)
            ),
            vol.Optional(
                _CONF_ENTRY_TITLE,
                description={
                    "suggested_value": default_title or DEFAULT_ENTRY_TITLE
                },
            ): selector.TextSelector(),
            vol.Optional(
                CONF_NOTIFY_SERVICE,
                description={"suggested_value": default_notify},
            ): notify_selector,
        }
    )


def _rooms_schema(suggestions: list[str]) -> vol.Schema:
    """One room-name box per priority slot (room_name_1..room_name_N).

    Optional markers with a suggested value, not required ones: the box arrives
    pre-filled, and a user who clears it must reach ``room_name_empty`` rather
    than a schema rejection Home Assistant renders without our copy.
    """
    return vol.Schema(
        {
            vol.Optional(
                _room_name_key(index), description={"suggested_value": name}
            ): _ROOM_NAME_SELECTOR
            for index, name in enumerate(suggestions)
        }
    )


def _sensors_schema(count: int, suggestions: list[str] | None = None) -> vol.Schema:
    """One room-temperature picker per chosen head (sensor_1..sensor_N).

    Pre-filled from ``suggestions`` so returning to this step from the review
    screen — or from a rejected submission — shows the answers already given.
    """
    chosen = suggestions or []
    return vol.Schema(
        {
            vol.Required(
                f"sensor_{i + 1}",
                description={
                    "suggested_value": chosen[i] if i < len(chosen) else None
                },
            ): _SENSOR_SELECTOR
            for i in range(count)
        }
    )


def _zone_override_keys() -> set[str]:
    """Every possible per-zone override key (flow-input-only, never stored flat).

    Covers vane vertical/horizontal AND the airflow (stage) sensor: all fold
    into the zones list, and a stale flat copy would shadow it in the
    coordinator's {**data, **options} merge.
    """
    keys: set[str] = set()
    for i in range(MAX_ZONES):
        slug = zone_slug(i)
        keys.add(f"{slug}_vane_vertical")
        keys.add(f"{slug}_vane_horizontal")
        keys.add(f"{slug}_stage")
    return keys


_FRESHNESS_ZONE_FIELDS = (
    ("report_interval", ZONE_REPORT_INTERVAL),
    ("max_age", ZONE_MAX_AGE),
    ("startup_grace", ZONE_STARTUP_GRACE),
    ("evidence_basis", ZONE_EVIDENCE_BASIS),
    ("sample_timestamp_attribute", ZONE_SAMPLE_TIMESTAMP_ATTR),
    ("sample_sequence_attribute", ZONE_SAMPLE_SEQUENCE_ATTR),
)


def _freshness_profile_keys() -> set[str]:
    """Return the flow-only freshness keys that fold into a zone record."""
    return {
        f"{zone_slug(index)}_{suffix}"
        for index in range(MAX_ZONES)
        for suffix, _ in _FRESHNESS_ZONE_FIELDS
    }


def _freshness_error(profile: dict[str, Any]) -> str | None:
    """Validate one complete M23 freshness profile with one diagnostic.

    An empty profile deliberately remains an unknown cadence.  A duration is
    never inferred, and sample attributes are accepted only for the basis that
    uses an advancing marker.
    """
    values: dict[str, float] = {}
    for suffix in ("report_interval", "max_age", "startup_grace"):
        raw = profile.get(suffix)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return "freshness_profile_invalid"
        if not math.isfinite(value) or value <= 0:
            return "freshness_profile_invalid"
        values[suffix] = value
    if (
        "report_interval" in values
        and "max_age" in values
        and values["max_age"] < values["report_interval"]
    ):
        return "freshness_profile_invalid"
    basis = profile.get("evidence_basis") or "unknown"
    if basis not in EVIDENCE_BASES:
        return "freshness_profile_invalid"
    timestamp = profile.get("sample_timestamp_attribute") or ""
    sequence = profile.get("sample_sequence_attribute") or ""
    if basis == EVIDENCE_SAMPLE_TIMESTAMP:
        if bool(timestamp) == bool(sequence):
            return "freshness_profile_invalid"
    elif timestamp or sequence:
        return "freshness_profile_invalid"
    return None


def _zone_freshness_profile(zone: dict[str, Any]) -> dict[str, Any]:
    """Read one stored profile using flow names, without changing its values."""
    return {
        suffix: zone.get(zone_key)
        for suffix, zone_key in _FRESHNESS_ZONE_FIELDS
        if zone.get(zone_key) is not None
    }


def _effective_zones(entry: ConfigEntry) -> list[dict[str, Any]]:
    """Copy the zone list the coordinator reads after options precedence."""
    conf = {**entry.data, **entry.options}
    return [dict(zone) for zone in conf.get(CONF_ZONES, [])]


def _minutes(value: float) -> str:
    """Format one stored minute duration for review copy."""
    return f"{value:g} min"


def _freshness_summary(zone: dict[str, Any]) -> str:
    """Describe the profile without claiming an unenforced cutoff."""
    profile = _zone_freshness_profile(zone)
    if not profile:
        return "Cadence: unknown — MXZ does not time out this sensor."
    interval = profile.get("report_interval")
    explicit_max = profile.get("max_age")
    explicit_grace = profile.get("startup_grace")
    window = freshness_window(
        report_interval=interval,
        max_age=explicit_max,
        startup_grace=explicit_grace,
    )
    contract = evidence_contract(
        basis=profile.get("evidence_basis"),
        sample_timestamp_attribute=profile.get("sample_timestamp_attribute"),
        sample_sequence_attribute=profile.get("sample_sequence_attribute"),
    )
    # Stored HA-write profiles may have unused markers that submission rejects.
    # Only the consumer's window and contract decide whether a cutoff applies.
    if (window is None or contract is None) and _freshness_error(profile):
        return "Cadence: invalid stored profile — MXZ does not time out this sensor."

    parts: list[str] = []
    if interval is not None:
        parts.append(f"expected every {_minutes(float(interval))}")
    if window is not None:
        if explicit_max is None:
            parts.append(f"maximum age {_minutes(window[0])} (three expected reports)")
        elif interval is not None:
            parts.append(
                f"maximum age {_minutes(window[0])} "
                "(explicit; overrides three-report default)"
            )
        else:
            parts.append(f"maximum age {_minutes(window[0])} (explicit)")
        if explicit_grace is None:
            parts.append(
                f"startup grace {_minutes(window[1])} (defaults to maximum age)"
            )
        else:
            parts.append(f"startup grace {_minutes(window[1])} (explicit)")
    elif explicit_grace is not None:
        parts.append(
            f"startup grace {_minutes(float(explicit_grace))} "
            "(inactive without a maximum age)"
        )

    if contract is None:
        parts.append("evidence basis unknown")
        return "Cadence: " + "; ".join(parts) + " — MXZ does not time out this sensor."
    basis, marker, marker_is_time = contract
    if basis == EVIDENCE_SAMPLE_TIMESTAMP:
        marker_kind = "timestamp" if marker_is_time else "sequence"
        parts.append(f'evidence: sample {marker_kind} attribute "{marker}"')
    else:
        parts.append("evidence: each HA state write (source contract required)")
    if window is None:
        parts.append("no maximum age, so MXZ does not time out this sensor")
    return "Cadence: " + "; ".join(parts) + "."


def _num() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step="any")
    )


def _options_schema(
    current: dict[str, Any],
    celsius: bool,
    zones: list[dict[str, Any]],
    idle_options: tuple[str, ...],
) -> vol.Schema:
    # Unset temperature tunables fall back to the system-unit profile (clean
    # metric values on a °C system, the legacy °F values otherwise); an
    # already-saved value always wins. Non-temperature keys aren't in the
    # profile, so they fall through to their plain DEFAULT_* below.
    profile = unit_profile(celsius)
    eff = {**profile["defaults"], **current}
    engage_min, engage_max = profile["engage_bounds"]

    # Vane selects + the airflow (stage) sensor are auto-detected at setup;
    # expose per-zone overrides (suggested_value shows what's currently wired).
    zone_fields: dict[Any, Any] = {}
    for i, zone in enumerate(zones):
        slug = zone_slug(i)
        zone_fields[
            vol.Optional(
                f"{slug}_vane_vertical",
                description={"suggested_value": zone.get(ZONE_VANE_VERTICAL)},
            )
        ] = _VANE_SELECTOR
        zone_fields[
            vol.Optional(
                f"{slug}_vane_horizontal",
                description={"suggested_value": zone.get(ZONE_VANE_HORIZONTAL)},
            )
        ] = _VANE_SELECTOR
        zone_fields[
            vol.Optional(
                f"{slug}_stage",
                description={"suggested_value": zone.get(ZONE_STAGE_SENSOR)},
            )
        ] = _STAGE_SELECTOR
        profile = _zone_freshness_profile(zone)
        for suffix, _ in _FRESHNESS_ZONE_FIELDS:
            key = f"{slug}_{suffix}"
            if suffix == "evidence_basis":
                # Custom values keep invalid raw API submissions inside this
                # form, where M23 requires one diagnostic and no mutation.
                zone_fields[vol.Optional(key, default=profile.get(suffix, "unknown"))] = (
                    selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(EVIDENCE_BASES),
                            custom_value=True,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                )
            elif suffix in {"sample_timestamp_attribute", "sample_sequence_attribute"}:
                zone_fields[vol.Optional(key, description={"suggested_value": profile.get(suffix)})] = _ROOM_NAME_SELECTOR
            else:
                zone_fields[vol.Optional(key, description={"suggested_value": profile.get(suffix)})] = _num()

    return _tunables_schema(
        eff, engage_min, engage_max, idle_options
    ).extend(zone_fields)


# Duration/magnitude tunables where a negative value is meaningless. Temperature
# tunables are deliberately absent: below zero is an ordinary °C reading.
_NONNEGATIVE_TUNABLES = (
    CONF_DEMAND_THRESHOLD,
    CONF_ENGAGE_DEADBAND,
    CONF_MODE_HYSTERESIS,
    CONF_COIL_DRY_MINUTES,
)
# Every numeric tunable that must be a finite real number. `NumberSelector`
# coerces via `float()`, which happily accepts "nan"/"inf" and passes them
# through unchanged (NaN fails every min/max comparison, so the selector's own
# bounds never catch it) — so finiteness must be checked explicitly here.
_FINITE_TUNABLES = _NONNEGATIVE_TUNABLES + (
    CONF_ECO_COOL_MAX,
    CONF_ECO_HEAT_MIN,
    CONF_CLAMP_MIN,
    CONF_CLAMP_MAX,
    CONF_HEAT_LOCKOUT_FLOOR,
    CONF_COOL_LOCKOUT_CEILING,
    CONF_CHANGEOVER_HEAT_ABOVE,
    CONF_CHANGEOVER_COOL_BELOW,
)
# (low, high, error, may_be_equal) for every pair a single field's selector can't
# see. Inverted (low above high) always breaks its consumer:
#   eco    — `room_call` tests `temp > eco_cool_max` BEFORE
#            `temp < eco_heat_min`, so an inverted band silently calls cool for
#            rooms that also qualify as too cold.
#   clamp  — `setpoints` computes max(min(t, clamp_max), clamp_min), which
#            returns clamp_min and ignores clamp_max entirely once inverted.
#   lockout— between an inverted floor and ceiling NEITHER safety override is
#            suppressed, so a heat-locked and cool-locked room can call both.
#   changeover — `season_lockouts` checks `high >= heat_above` BEFORE
#            `high <= cool_below` and returns on the first match, so an inverted
#            pair reads the warm season for every high at or above heat_above,
#            including forecasts the user set cool_below to call cold.
# Equality is rejected ONLY for the changeover pair. The gap between those two
# thresholds is the shoulder season documented on `season_lockouts`: the
# hysteresis that keeps the forecast from chattering the lockouts. Equal values
# leave a zero-width shoulder, so a single reading flips the heat lockout to the
# cool lockout. It never turns both on — those branches return, they don't OR.
# For the other three pairs, equality merely collapses the band to zero width —
# degenerate, but the consumers handle it consistently, so refusing it would
# reject a combination that works today.
_ORDERED_PAIRS: tuple[tuple[str, str, str, bool], ...] = (
    (CONF_ECO_HEAT_MIN, CONF_ECO_COOL_MAX, "eco_band_inverted", True),
    (CONF_CLAMP_MIN, CONF_CLAMP_MAX, "clamp_inverted", True),
    (CONF_HEAT_LOCKOUT_FLOOR, CONF_COOL_LOCKOUT_CEILING, "lockout_inverted", True),
    (
        CONF_CHANGEOVER_COOL_BELOW,
        CONF_CHANGEOVER_HEAT_ABOVE,
        "changeover_inverted",
        False,
    ),
)


def _validate_tunables(user_input: dict[str, Any]) -> dict[str, str]:
    """Return {field: error_key} for every submitted tunable that fails validation.

    Runs after the schema/selectors have already coerced the submission, so
    this only needs to catch what they miss: non-finite numbers, negative
    durations, and the cross-field orderings in ``_ORDERED_PAIRS``. An ordering
    error names BOTH fields, because either one of them can be the wrong number.
    """
    errors: dict[str, str] = {}
    parsed: dict[str, float] = {}
    for key in _FINITE_TUNABLES:
        if key not in user_input:
            continue
        try:
            value = float(user_input[key])
        except (TypeError, ValueError):
            errors[key] = "not_a_number"
        else:
            if math.isfinite(value):
                parsed[key] = value
            else:
                errors[key] = "not_a_number"

    for key in _NONNEGATIVE_TUNABLES:
        if parsed.get(key, 0.0) < 0:
            errors[key] = "must_not_be_negative"

    for low_key, high_key, error, may_be_equal in _ORDERED_PAIRS:
        if low_key in errors or high_key in errors:
            continue  # the value itself is wrong; ordering it says nothing
        low, high = parsed.get(low_key), parsed.get(high_key)
        if low is None or high is None:
            continue  # a field this submission never carried
        if high < low or (high == low and not may_be_equal):
            errors[low_key] = errors[high_key] = error
    return errors


def _tunables_schema(
    eff: dict[str, Any],
    engage_min: float,
    engage_max: float,
    idle_options: tuple[str, ...] = IDLE_ACTION_OPTIONS,
) -> vol.Schema:
    idle_action = eff.get(CONF_IDLE_ACTION, DEFAULT_IDLE_ACTION)
    idle_field: dict[vol.Marker, selector.SelectSelector] = {}
    if idle_options:
        idle_marker: vol.Marker = (
            vol.Optional(CONF_IDLE_ACTION, default=idle_action)
            if idle_action in idle_options
            else vol.Required(CONF_IDLE_ACTION)
        )
        idle_field[idle_marker] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(idle_options),
                translation_key="idle_action",
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
    return vol.Schema(
        {
            vol.Optional(
                CONF_DEMAND_THRESHOLD,
                default=eff.get(CONF_DEMAND_THRESHOLD, DEFAULT_DEMAND_THRESHOLD),
            ): _num(),
            vol.Optional(
                CONF_ENGAGE_DEADBAND,
                default=eff.get(CONF_ENGAGE_DEADBAND, DEFAULT_ENGAGE_DEADBAND),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    mode=selector.NumberSelectorMode.BOX,
                    min=engage_min,
                    max=engage_max,
                    step=engage_min / 2,
                )
            ),
            vol.Optional(
                CONF_MODE_HYSTERESIS,
                default=eff.get(CONF_MODE_HYSTERESIS, DEFAULT_MODE_HYSTERESIS),
            ): _num(),
            vol.Optional(
                CONF_ECO_COOL_MAX,
                default=eff.get(CONF_ECO_COOL_MAX, DEFAULT_ECO_COOL_MAX),
            ): _num(),
            vol.Optional(
                CONF_ECO_HEAT_MIN,
                default=eff.get(CONF_ECO_HEAT_MIN, DEFAULT_ECO_HEAT_MIN),
            ): _num(),
            vol.Optional(
                CONF_CLAMP_MIN,
                default=eff.get(CONF_CLAMP_MIN, DEFAULT_CLAMP_MIN),
            ): _num(),
            vol.Optional(
                CONF_CLAMP_MAX,
                default=eff.get(CONF_CLAMP_MAX, DEFAULT_CLAMP_MAX),
            ): _num(),
            vol.Optional(
                CONF_RESTING_MODE_BIAS,
                default=eff.get(
                    CONF_RESTING_MODE_BIAS, DEFAULT_RESTING_MODE_BIAS
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(RESTING_BIAS_OPTIONS),
                    translation_key="resting_mode_bias",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_HEAT_LOCKOUT_FLOOR,
                default=eff.get(
                    CONF_HEAT_LOCKOUT_FLOOR, DEFAULT_HEAT_LOCKOUT_FLOOR
                ),
            ): _num(),
            vol.Optional(
                CONF_COOL_LOCKOUT_CEILING,
                default=eff.get(
                    CONF_COOL_LOCKOUT_CEILING, DEFAULT_COOL_LOCKOUT_CEILING
                ),
            ): _num(),
            vol.Optional(
                CONF_CHANGEOVER_ENTITY,
                description={"suggested_value": eff.get(CONF_CHANGEOVER_ENTITY)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["weather", "sensor"])
            ),
            vol.Optional(
                CONF_CHANGEOVER_HEAT_ABOVE,
                default=eff.get(
                    CONF_CHANGEOVER_HEAT_ABOVE, DEFAULT_CHANGEOVER_HEAT_ABOVE
                ),
            ): _num(),
            vol.Optional(
                CONF_CHANGEOVER_COOL_BELOW,
                default=eff.get(
                    CONF_CHANGEOVER_COOL_BELOW, DEFAULT_CHANGEOVER_COOL_BELOW
                ),
            ): _num(),
            vol.Optional(
                CONF_INHIBIT_ENTITY,
                description={"suggested_value": eff.get(CONF_INHIBIT_ENTITY)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor", "switch", "input_boolean"]
                )
            ),
            vol.Optional(
                CONF_INHIBIT_ACTIVE_STATE,
                default=eff.get(
                    CONF_INHIBIT_ACTIVE_STATE, DEFAULT_INHIBIT_ACTIVE_STATE
                ),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_INHIBIT_ACTION,
                default=eff.get(CONF_INHIBIT_ACTION, DEFAULT_INHIBIT_ACTION),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(INHIBIT_ACTION_OPTIONS),
                    translation_key="inhibit_action",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            **idle_field,
            vol.Optional(
                CONF_COIL_DRY_MINUTES,
                default=eff.get(CONF_COIL_DRY_MINUTES, DEFAULT_COIL_DRY_MINUTES),
            ): _num(),
            vol.Optional(
                CONF_FAN_BOOST_ENABLE,
                default=eff.get(
                    CONF_FAN_BOOST_ENABLE, DEFAULT_FAN_BOOST_ENABLE
                ),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_FAN_BOOST_MAX,
                default=eff.get(CONF_FAN_BOOST_MAX, DEFAULT_FAN_BOOST_MAX),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(FAN_LADDER),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class MXZConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial config (household entity IDs)."""

    VERSION = 2

    def __init__(self) -> None:
        self._heads: list[str] = []
        self._notify: str | None = None
        self._title: str = DEFAULT_ENTRY_TITLE
        self._room_names: list[str] = []
        self._sensors: list[str] = []
        self._zones: list[dict[str, Any]] = []
        self._tunables: dict[str, Any] | None = None

    def _head_name(self, entity_id: str) -> str:
        """Friendly display name for a head (used as the zone name)."""
        state = self.hass.states.get(entity_id)
        if state is not None and state.name:
            return str(state.name)
        return entity_id.split(".", 1)[-1].replace("_", " ").title()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: pick 2..MAX_ZONES heads (first = highest standoff priority)."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] | None = None
        if user_input is not None:
            heads: list[str] = user_input.get(_CONF_HEADS) or []
            if len(set(heads)) != len(heads):
                errors["base"] = "duplicate_heads"
            elif len(heads) < MIN_ZONES:
                errors["base"] = "need_two_heads"
            elif len(heads) > MAX_ZONES:
                errors["base"] = "too_many_heads"
            elif conflict := _conflict_error(
                _head_conflicts(self.hass, heads, exclude_flow_id=self.flow_id)
            ):
                error, conflicts = conflict
                errors[_CONF_HEADS] = error
                placeholders = _conflict_placeholders(conflicts)
            elif problem := head_mode_problem(self.hass, heads):
                error, placeholders = problem
                errors[_CONF_HEADS] = error
            else:
                self.context[_CONTEXT_HEADS] = tuple(heads)
                await self.async_set_unique_id("|".join(heads))
                self._abort_if_unique_id_configured()
                if heads != self._heads:
                    # A different selection invalidates the answers keyed to the
                    # old slots; the same selection keeps them, which is what
                    # "go back and look again" has to mean.
                    self._room_names = []
                    self._sensors = []
                self._heads = heads
                self._notify = user_input.get(CONF_NOTIFY_SERVICE) or None
                self._title = (
                    str(user_input.get(_CONF_ENTRY_TITLE) or "").strip()
                    or DEFAULT_ENTRY_TITLE
                )
                return await self.async_step_rooms()

        # Re-showing this step is also how the review screen goes back, so the
        # answers already given have to survive the round trip.
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(
                _notify_options(self.hass),
                default_heads=(
                    user_input.get(_CONF_HEADS) if user_input else self._heads or None
                ),
                default_notify=(
                    user_input.get(CONF_NOTIFY_SERVICE)
                    if user_input
                    else self._notify
                ),
                default_title=(
                    user_input.get(_CONF_ENTRY_TITLE) if user_input else self._title
                ),
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_rooms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: name each room, in the priority order chosen on step 1.

        Names are display copy: they land in the existing ``ZONE_NAME`` and no
        new key is stored. Priority is shown, not edited — the picker order on
        the previous step already IS the priority, and a second control for one
        list would be a second source of truth.
        """
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        suggestions = [
            self._room_names[index]
            if index < len(self._room_names)
            else self._head_name(head)
            for index, head in enumerate(self._heads)
        ]
        if user_input is not None:
            names = [
                _resolve_room_name(user_input.get(_room_name_key(index)), suggestion)
                for index, suggestion in enumerate(suggestions)
            ]
            suggestions = names
            if problem := _room_name_problem(names):
                error, placeholders = problem
                errors["base"] = error
            else:
                self._room_names = names
                return await self.async_step_sensors()

        placeholders["rooms"] = "\n".join(
            f"{index + 1}. {head} (priority {index + 1})"
            for index, head in enumerate(self._heads)
        )
        return self.async_show_form(
            step_id="rooms",
            data_schema=_rooms_schema(suggestions),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _system_unit(self) -> str:
        """The unit Home Assistant displays temperatures in."""
        return str(self.hass.config.units.temperature_unit)

    def _room_map(self, sensors: list[str] | None = None) -> str:
        """The room-to-head-to-sensor mapping, one line per priority slot.

        Rendered through the step description. The matching per-slot names are
        also supplied to the sensor-field descriptions on this flow surface.
        """
        chosen = sensors if sensors is not None else self._sensors
        unit = self._system_unit()
        lines: list[str] = []
        for index, head in enumerate(self._heads):
            name = (
                self._room_names[index]
                if index < len(self._room_names)
                else self._head_name(head)
            )
            line = f"{index + 1}. {name} — {head}"
            if index < len(chosen) and chosen[index]:
                line += f"\n   Sensor: {_sensor_status(self.hass, chosen[index], unit)}"
            lines.append(line)
        return "\n".join(lines)

    def _sensor_field_placeholders(self) -> dict[str, str]:
        """Map every shown sensor-field slot to its current room name."""
        return {
            f"room_{index + 1}": (
                self._room_names[index]
                if index < len(self._room_names)
                else self._head_name(head)
            )
            for index, head in enumerate(self._heads)
        }

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: one room temperature sensor per room, by room name."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        suggestions = list(self._sensors)
        if user_input is not None:
            sensors = [
                user_input[f"sensor_{index + 1}"] for index in range(len(self._heads))
            ]
            suggestions = sensors
            if problem := _sensor_problem(self.hass, sensors):
                error, placeholders = problem
                errors["base"] = error
            else:
                self._sensors = sensors
                self._zones = self._build_zones()
                return await self.async_step_review()

        placeholders["rooms"] = self._room_map(suggestions)
        placeholders.update(self._sensor_field_placeholders())
        return self.async_show_form(
            step_id="sensors",
            data_schema=_sensors_schema(len(self._heads), suggestions),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _build_zones(self) -> list[dict[str, Any]]:
        """Assemble the stored zone list from the answers given so far."""
        zones: list[dict[str, Any]] = []
        for index, head in enumerate(self._heads):
            # Auto-detect each head's vane selects from its own device so
            # the user never has to pick them (overridable via Configure).
            vanes = _detect_vanes(self.hass, head)
            zones.append(
                {
                    ZONE_NAME: self._room_names[index],
                    ZONE_CLIMATE: head,
                    ZONE_SENSOR: self._sensors[index],
                    ZONE_VANE_VERTICAL: vanes.get("vertical"),
                    ZONE_VANE_HORIZONTAL: vanes.get("horizontal"),
                    ZONE_STAGE_SENSOR: _detect_stage(self.hass, head),
                }
            )
        return zones

    def _default_tunables(self) -> dict[str, Any] | None:
        """The option set a submit-as-is of the advanced step would produce.

        Built by validating an EMPTY submission against the very schema that
        step would have rendered, so skipping advanced and accepting its
        defaults are the same values by construction rather than by a second
        hand-written list that could drift. Returns None when the schema cannot
        fill itself — the M12 case where no head advertises the default parking
        mode, so the choice has to be made explicitly.
        """
        profile = unit_profile(
            self.hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS
        )
        engage_min, engage_max = profile["engage_bounds"]
        idle_options = supported_idle_actions(self.hass, self._heads)
        try:
            return _tunables_schema(
                profile["defaults"], engage_min, engage_max, idle_options
            )({})
        except vol.Invalid:
            return None

    def _summary(self) -> str:
        """The review screen's body: everything about to be saved, in order."""
        unit = self._system_unit()
        lines = [
            f"Outdoor unit: {self._title}",
            f"Drift alerts: {self._notify or 'none'}",
            "",
        ]
        for index, head in enumerate(self._heads):
            lines.append(f"Priority {index + 1}  {self._room_names[index]}")
            lines.append(f"  Head:   {head}")
            lines.append(
                f"  Sensor: {_sensor_status(self.hass, self._sensors[index], unit)}"
            )
            lines.append(
                "  Cadence: unknown — MXZ does not time out this sensor."
            )
            lines.append("")
        if self._tunables is not None:
            lines.append(
                f"Comfort settings: your choices ({len(self._tunables)} values)."
                f" Idle: {self._tunables.get(CONF_IDLE_ACTION, DEFAULT_IDLE_ACTION)}."
            )
        elif (defaults := self._default_tunables()) is not None:
            lines.append(
                f"Comfort settings: defaults for {unit} ({len(defaults)} values)."
                f" Idle: {defaults.get(CONF_IDLE_ACTION, DEFAULT_IDLE_ACTION)}."
            )
        else:
            lines.append(
                "Comfort settings: needs your choice — these heads cannot idle in"
                f' "{DEFAULT_IDLE_ACTION}". Use "Change advanced settings".'
            )
        return "\n".join(lines)

    async def async_step_review(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 4: check everything, then save. The ONLY save point.

        A menu step, so the summary is the description and the actions are the
        buttons: Home Assistant reads the text before it offers anything to
        press, which is exactly what review-before-save means. Advanced tuning
        hangs off this step too, which is why the twenty knobs are no longer on
        the way in.
        """
        return self.async_show_menu(
            step_id="review",
            menu_options=["finish", "tuning", "user"],
            description_placeholders={"summary": self._summary()},
        )

    def _back_to_user(
        self, error: str, placeholders: dict[str, str] | None
    ) -> ConfigFlowResult:
        """Return to the head picker, releasing this flow's reservation.

        A flow parked on an error is not entitled to keep other flows out of
        heads it has just been told it cannot have.
        """
        self.context.pop(_CONTEXT_HEADS, None)
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(
                _notify_options(self.hass), self._heads, self._notify, self._title
            ),
            errors={_CONF_HEADS: error},
            description_placeholders=placeholders,
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save. Every final recheck happens here, once, and then the entry.

        Ownership and capability can both change while the user is reading the
        review screen, so they are re-asked at the last possible moment and in
        the order the flow already used.
        """
        if problem := head_mode_problem(self.hass, self._heads):
            error, placeholders = problem
            return self._back_to_user(error, placeholders)
        if conflict := _conflict_error(
            _head_conflicts(self.hass, self._heads, exclude_flow_id=self.flow_id)
        ):
            error, conflicts = conflict
            return self._back_to_user(error, _conflict_placeholders(conflicts))
        if problem := _sensor_problem(self.hass, self._sensors):
            error, placeholders = problem
            placeholders = {
                **placeholders,
                "rooms": self._room_map(),
                **self._sensor_field_placeholders(),
            }
            return self.async_show_form(
                step_id="sensors",
                data_schema=_sensors_schema(len(self._heads), self._sensors),
                errors={"base": error},
                description_placeholders=placeholders,
            )
        tunables = self._tunables
        if tunables is None and (tunables := self._default_tunables()) is None:
            # M12: the stored default parking mode is not advertised by every
            # head, so advanced is not skippable. Say which choice is missing
            # instead of saving something a head cannot do.
            return await self.async_step_tuning()
        if head_mode_problem(
            self.hass, self._heads, tunables.get(CONF_IDLE_ACTION, DEFAULT_IDLE_ACTION)
        ):
            # An advanced answer given earlier is only final if it still fits
            # the heads being saved: a head can drop the parking mode while the
            # review screen is open, and going back can swap the heads out from
            # under the answer. Re-ask the advanced step with those same answers
            # so it raises its own idle error, on its own field, and keeps every
            # answer that is still valid.
            return await self.async_step_tuning(tunables)
        data: dict[str, Any] = {CONF_ZONES: self._zones, **tunables}
        if self._notify:
            data[CONF_NOTIFY_SERVICE] = self._notify
        # Tunables live in options (with the data mirror above), exactly as
        # an options-flow save would leave them.
        return self.async_create_entry(
            title=self._title, data=data, options=dict(tunables)
        )

    async def async_step_tuning(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Advanced: every tunable, pre-filled with unit-appropriate defaults.

        Reached only from the review screen, and it returns there — this step
        no longer creates the entry. Nothing here is required; the same values
        stay editable later via the integration's Configure dialog.
        """
        if problem := head_mode_problem(self.hass, self._heads):
            error, placeholders = problem
            return self._back_to_user(error, placeholders)
        idle_options = supported_idle_actions(self.hass, self._heads)
        profile = unit_profile(
            self.hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS
        )
        engage_min, engage_max = profile["engage_bounds"]
        if user_input is not None:
            idle_action = user_input.get(CONF_IDLE_ACTION, DEFAULT_IDLE_ACTION)
            if problem := head_mode_problem(self.hass, self._heads, idle_action):
                error, placeholders = problem
                return self.async_show_form(
                    step_id="tuning",
                    data_schema=_tunables_schema(
                        {**profile["defaults"], **user_input},
                        engage_min,
                        engage_max,
                        idle_options,
                    ),
                    errors={CONF_IDLE_ACTION: error},
                    description_placeholders=placeholders,
                )
            if tuning_errors := _validate_tunables(user_input):
                return self.async_show_form(
                    step_id="tuning",
                    data_schema=_tunables_schema(
                        {**profile["defaults"], **user_input},
                        engage_min,
                        engage_max,
                        idle_options,
                    ),
                    errors=tuning_errors,
                )
            self._tunables = dict(user_input)
            return await self.async_step_review()

        errors: dict[str, str] = {}
        placeholders: dict[str, str] | None = None
        # Arrived here from "Save and finish" when the schema could not fill the
        # parking mode itself: name the missing choice on the field that needs it.
        if (
            self._tunables is None
            and self._default_tunables() is None
            and (problem := head_mode_problem(
                self.hass, self._heads, DEFAULT_IDLE_ACTION
            ))
        ):
            error, placeholders = problem
            errors[CONF_IDLE_ACTION] = error
        return self.async_show_form(
            step_id="tuning",
            data_schema=_tunables_schema(
                {**profile["defaults"], **(self._tunables or {})},
                engage_min,
                engage_max,
                idle_options,
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure the heads / sensors of an existing entry in place (#7).

        Avoids the delete-and-re-add cycle (which also resurrects stale restore
        state) when a zone's sensor or head was mis-assigned at setup.
        """
        entry = self._get_reconfigure_entry()
        current_heads = _entry_heads(entry)
        errors: dict[str, str] = {}
        placeholders: dict[str, str] | None = None
        if user_input is not None:
            heads: list[str] = user_input.get(_CONF_HEADS) or []
            if len(set(heads)) != len(heads):
                errors["base"] = "duplicate_heads"
            elif len(heads) < MIN_ZONES:
                errors["base"] = "need_two_heads"
            elif len(heads) > MAX_ZONES:
                errors["base"] = "too_many_heads"
            elif conflict := _conflict_error(
                _head_conflicts(
                    self.hass,
                    heads,
                    exclude_entry_id=entry.entry_id,
                    exclude_flow_id=self.flow_id,
                    grandfathered_heads=current_heads,
                )
            ):
                error, conflicts = conflict
                errors[_CONF_HEADS] = error
                placeholders = _conflict_placeholders(conflicts)
            elif problem := head_mode_problem(
                self.hass,
                heads,
                {**entry.data, **entry.options}.get(
                    CONF_IDLE_ACTION, DEFAULT_IDLE_ACTION
                ),
            ):
                error, placeholders = problem
                errors[_CONF_HEADS] = error
            else:
                self.context[_CONTEXT_HEADS] = tuple(heads)
                self.context[_CONTEXT_ENTRY_ID] = entry.entry_id
                uid = "|".join(heads)
                if any(
                    e.unique_id == uid and e.entry_id != entry.entry_id
                    for e in self.hass.config_entries.async_entries(DOMAIN)
                ):
                    return self.async_abort(reason="already_configured")
                if heads != self._heads:
                    self._room_names = []
                    self._sensors = []
                self._heads = heads
                self._notify = user_input.get(CONF_NOTIFY_SERVICE) or None
                self._title = (
                    str(user_input.get(_CONF_ENTRY_TITLE) or "").strip()
                    or entry.title
                )
                return await self.async_step_reconfigure_rooms()

        current = entry.data.get(CONF_ZONES, [])
        # Also the way the reconfigure review screen goes back, so in-flight
        # answers win over the stored ones when there are any.
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_schema(
                _notify_options(self.hass),
                default_heads=(
                    user_input.get(_CONF_HEADS)
                    if user_input is not None
                    else self._heads or [z[ZONE_CLIMATE] for z in current]
                ),
                default_notify=(
                    user_input.get(CONF_NOTIFY_SERVICE)
                    if user_input is not None
                    # Keyed on whether this step has been answered, not on
                    # whether the answer is truthy: an emptied box is an
                    # answer, and re-suggesting the stored target would undo
                    # the clearing every time the user goes back. Same test
                    # the title below already uses.
                    else self._notify
                    if self._heads
                    else entry.data.get(CONF_NOTIFY_SERVICE)
                ),
                default_title=(
                    user_input.get(_CONF_ENTRY_TITLE)
                    if user_input is not None
                    else self._title
                    if self._heads
                    else entry.title
                ),
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _stored_room_name(self, entry: ConfigEntry, head: str) -> str:
        """The name this entry already stores for ``head``, else the head's."""
        for zone in entry.data.get(CONF_ZONES, []):
            if isinstance(zone, dict) and zone.get(ZONE_CLIMATE) == head:
                name = zone.get(ZONE_NAME)
                if isinstance(name, str) and name:
                    return name
        return self._head_name(head)

    async def async_step_reconfigure_rooms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Room names for reconfigure, prefilled from the stored zone names.

        Clearing a box here is not an error, unlike setup: on an entry that
        already exists, an empty name is the documented way back to the head's
        own name. Renaming changes no unique_id, so no entity moves.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        suggestions = [
            self._room_names[index]
            if index < len(self._room_names)
            else self._stored_room_name(entry, head)
            for index, head in enumerate(self._heads)
        ]
        if user_input is not None:
            names = [
                _resolve_room_name(user_input.get(_room_name_key(index)), suggestion)
                or self._head_name(self._heads[index])
                for index, suggestion in enumerate(suggestions)
            ]
            suggestions = names
            if problem := _room_name_problem(names):
                error, placeholders = problem
                errors["base"] = error
            else:
                self._room_names = names
                return await self.async_step_reconfigure_sensors()

        placeholders["rooms"] = "\n".join(
            f"{index + 1}. {head} (priority {index + 1})"
            for index, head in enumerate(self._heads)
        )
        return self.async_show_form(
            step_id="reconfigure_rooms",
            data_schema=_rooms_schema(suggestions),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Per-room sensors for reconfigure, prefilled from the existing zones."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        suggestions = list(self._sensors) or [
            zone.get(ZONE_SENSOR)
            for head in self._heads
            for zone in [self._stored_zone(entry, head)]
        ]
        if user_input is not None:
            sensors = [
                user_input[f"sensor_{index + 1}"] for index in range(len(self._heads))
            ]
            suggestions = sensors
            if problem := _sensor_problem(self.hass, sensors):
                error, placeholders = problem
                errors["base"] = error
            else:
                self._sensors = sensors
                return await self.async_step_reconfigure_review()

        placeholders["rooms"] = self._room_map(
            [entity_id or "" for entity_id in suggestions]
        )
        placeholders.update(self._sensor_field_placeholders())
        return self.async_show_form(
            step_id="reconfigure_sensors",
            data_schema=_sensors_schema(
                len(self._heads), [entity_id or "" for entity_id in suggestions]
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _stored_zone(self, entry: ConfigEntry, head: str) -> dict[str, Any]:
        """The zone dict this entry already stores for ``head``, else empty."""
        for zone in entry.data.get(CONF_ZONES, []):
            if isinstance(zone, dict) and zone.get(ZONE_CLIMATE) == head:
                return zone
        return {}

    async def async_step_reconfigure_review(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Check the changes, then save. The only save point for reconfigure.

        No advanced button: the comfort settings are edited with Configure, and
        a second editing path for the same twenty keys would be a second writer
        for one set of values.
        """
        return self.async_show_menu(
            step_id="reconfigure_review",
            menu_options=["reconfigure_finish", "reconfigure"],
            description_placeholders={"summary": self._reconfigure_summary()},
        )

    def _reconfigure_summary(self) -> str:
        """What this reconfigure is about to write, in priority order."""
        entry = self._get_reconfigure_entry()
        unit = self._system_unit()
        lines = [
            f"Outdoor unit: {self._title}",
            f"Drift alerts: {self._notify or 'none'}",
            "",
        ]
        old_order = _entry_head_order(entry)
        effective_zones = _effective_zones(entry)
        for index, head in enumerate(self._heads):
            lines.append(f"Priority {index + 1}  {self._room_names[index]}")
            lines.append(f"  Head:   {head}")
            lines.append(
                f"  Sensor: {_sensor_status(self.hass, self._sensors[index], unit)}"
            )
            zone = next(
                (
                    candidate
                    for candidate in effective_zones
                    if candidate.get(ZONE_CLIMATE) == head
                ),
                {},
            )
            lines.append(f"  {_freshness_summary(zone)}")
            if head in old_order and old_order.index(head) != index:
                lines.append(
                    f"  Moved from priority {old_order.index(head) + 1}. Its name,"
                    " sensor, vane wiring, target, drift, enable and fan hold"
                    " move with it."
                )
            lines.append("")
        for head in old_order:
            if head not in self._heads:
                lines.append(f"Removed: {head}. That room's entities are deleted.")
        return "\n".join(lines)

    async def async_step_reconfigure_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save the reconfigure. Every final recheck happens here, once."""
        entry = self._get_reconfigure_entry()
        if conflict := _conflict_error(
            _head_conflicts(
                self.hass,
                self._heads,
                exclude_entry_id=entry.entry_id,
                exclude_flow_id=self.flow_id,
                grandfathered_heads=_entry_heads(entry),
            )
        ):
            error, conflicts = conflict
            return self._back_to_reconfigure(
                error, _conflict_placeholders(conflicts)
            )
        if problem := head_mode_problem(
            self.hass,
            self._heads,
            {**entry.data, **entry.options}.get(CONF_IDLE_ACTION, DEFAULT_IDLE_ACTION),
        ):
            error, placeholders = problem
            return self._back_to_reconfigure(error, placeholders)
        if problem := _sensor_problem(self.hass, self._sensors):
            error, sensor_placeholders = problem
            return self.async_show_form(
                step_id="reconfigure_sensors",
                data_schema=_sensors_schema(len(self._heads), self._sensors),
                errors={"base": error},
                description_placeholders={
                    **sensor_placeholders,
                    "rooms": self._room_map(),
                    **self._sensor_field_placeholders(),
                },
            )

        zones: list[dict[str, Any]] = []
        for index, head in enumerate(self._heads):
            old = self._stored_zone(entry, head)
            if old:
                # Kept head: keep its vane wiring (incl. any user overrides);
                # the sensor and the room name come from the form.
                zone = dict(old)
                zone[ZONE_SENSOR] = self._sensors[index]
            else:
                vanes = _detect_vanes(self.hass, head)
                zone = {
                    ZONE_CLIMATE: head,
                    ZONE_SENSOR: self._sensors[index],
                    ZONE_VANE_VERTICAL: vanes.get("vertical"),
                    ZONE_VANE_HORIZONTAL: vanes.get("horizontal"),
                    ZONE_STAGE_SENSOR: _detect_stage(self.hass, head),
                }
            # The room name is display copy and lands in the existing
            # ZONE_NAME; the field itself is flow-input-only.
            zone[ZONE_NAME] = self._room_names[index]
            zones.append(zone)
        # Notify is always written (None included): data_updates merges
        # and can't delete a key, so clearing the field in the form must
        # store an explicit None to actually turn the alerts off.
        data_updates: dict[str, Any] = {
            CONF_ZONES: zones,
            CONF_NOTIFY_SERVICE: self._notify,
        }
        # Move each reordered room's entity records onto its new slot
        # BEFORE the entry is written: the update below triggers the
        # reload that rebuilds the entities, and they must find the
        # records — and the restored values — of their own room.
        _async_move_room_entities(
            self.hass, entry, _entry_head_order(entry), self._heads
        )
        # ONE reload per save (#15): async_update_reload_and_abort both
        # fires the update listener (which reloads) AND schedules its own
        # reload — two full back-to-back reloads, the same double the
        # options save had (#14). Update the entry directly and let the
        # listener do the single reload; an unchanged submit reloads
        # nothing, which is the correct amount of nothing.
        self.hass.config_entries.async_update_entry(
            entry,
            title=self._title,
            data={**entry.data, **data_updates},
            unique_id="|".join(self._heads),
        )
        return self.async_abort(reason="reconfigure_successful")

    def _back_to_reconfigure(
        self, error: str, placeholders: dict[str, str] | None
    ) -> ConfigFlowResult:
        """Return to the reconfigure head picker, releasing the reservation."""
        self.context.pop(_CONTEXT_HEADS, None)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_schema(
                _notify_options(self.hass), self._heads, self._notify, self._title
            ),
            errors={_CONF_HEADS: error},
            description_placeholders=placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MXZOptionsFlow:
        return MXZOptionsFlow()


class MXZOptionsFlow(OptionsFlow):
    """Tune the constants that were hardcoded in the YAML package."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones = _effective_zones(self.config_entry)
        heads = [zone[ZONE_CLIMATE] for zone in zones]
        if not heads:
            heads = sorted(_entry_heads(self.config_entry))
        current = {
            **self.config_entry.data,
            **self.config_entry.options,
            **(user_input or {}),
        }
        celsius = (
            self.hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS
        )
        if problem := head_mode_problem(self.hass, heads):
            error, placeholders = problem
            return self.async_show_form(
                step_id="init",
                data_schema=_options_schema(current, celsius, zones, ()),
                errors={"base": error},
                description_placeholders=placeholders,
            )
        idle_options = supported_idle_actions(self.hass, heads)
        override_keys = _zone_override_keys()
        freshness_keys = _freshness_profile_keys()
        if user_input is not None:
            idle_action = user_input.get(
                CONF_IDLE_ACTION,
                {**self.config_entry.data, **self.config_entry.options}.get(
                    CONF_IDLE_ACTION, DEFAULT_IDLE_ACTION
                ),
            )
            if problem := head_mode_problem(self.hass, heads, idle_action):
                error, placeholders = problem
                return self.async_show_form(
                    step_id="init",
                    data_schema=_options_schema(
                        current, celsius, zones, idle_options
                    ),
                    errors={CONF_IDLE_ACTION: error},
                    description_placeholders=placeholders,
                )
            if tuning_errors := _validate_tunables(user_input):
                return self.async_show_form(
                    step_id="init",
                    data_schema=_options_schema(
                        current, celsius, zones, idle_options
                    ),
                    errors=tuning_errors,
                )
            for i, zone in enumerate(zones):
                slug = zone_slug(i)
                profile = {
                    suffix: user_input.get(f"{slug}_{suffix}")
                    for suffix, _ in _FRESHNESS_ZONE_FIELDS
                }
                if _freshness_error(profile):
                    return self.async_show_form(
                        step_id="init",
                        data_schema=_options_schema(
                            current, celsius, zones, idle_options
                        ),
                        errors={"base": "freshness_profile_invalid"},
                    )
            # Per-zone vane + airflow-sensor overrides are flow-input-only: fold
            # them into the zones list in entry.data, never store them as flat
            # keys (a stale flat key would shadow the zones list in the
            # coordinator's {**data, **options} merge).
            #
            # Every override field is rendered for every zone (see
            # _options_schema), and a pre-filled field the user leaves untouched
            # is submitted with its value. So an ABSENT/empty key means the user
            # cleared that field -> drop it from the zone. The old behavior
            # skipped absent keys, so a cleared override could never be removed:
            # an auto-detected vane/stage stuck forever, even on heads that have
            # no vane (e.g. a ducted air handler advertising a phantom vane).
            for i, zone in enumerate(zones):
                slug = zone_slug(i)
                for suffix, zkey in (
                    ("vane_vertical", ZONE_VANE_VERTICAL),
                    ("vane_horizontal", ZONE_VANE_HORIZONTAL),
                    ("stage", ZONE_STAGE_SENSOR),
                ):
                    if value := user_input.get(f"{slug}_{suffix}"):
                        zone[zkey] = value
                    else:
                        zone.pop(zkey, None)
                # A complete all-empty profile is an explicit return to
                # unknown cadence.  Otherwise preserve the submitted values
                # byte-for-byte under M36's existing zone keys.
                profile = {
                    suffix: user_input.get(f"{slug}_{suffix}")
                    for suffix, _ in _FRESHNESS_ZONE_FIELDS
                }
                empty_profile = (
                    profile["evidence_basis"] in (None, "", "unknown")
                    and all(
                        profile[suffix] in (None, "")
                        for suffix in (
                            "report_interval",
                            "max_age",
                            "startup_grace",
                            "sample_timestamp_attribute",
                            "sample_sequence_attribute",
                        )
                    )
                )
                for suffix, zkey in _FRESHNESS_ZONE_FIELDS:
                    value = profile[suffix]
                    if empty_profile or value in (None, ""):
                        zone.pop(zkey, None)
                    else:
                        zone[zkey] = value
            tunables = {
                k: v
                for k, v in user_input.items()
                if k not in override_keys and k not in freshness_keys
            }
            # The standby-hold entity is clearable the same way the zone
            # overrides are: the field is always rendered, a pre-filled value
            # the user leaves alone is submitted back, so an absent/empty key
            # on a real (non-empty) submit means the user cleared it. Write an
            # explicit None so the resilience merge below doesn't resurrect the
            # old entity — and note the failure direction is safe: losing this
            # key means "no standby hold", i.e. normal coordination. A
            # degenerate empty submit (schema bypass) still wipes nothing.
            if user_input and not user_input.get(CONF_INHIBIT_ENTITY):
                tunables[CONF_INHIBIT_ENTITY] = None
            # Resilience: MERGE onto the existing options (a partial/empty submit
            # must never wipe the rest) and refuse to persist an empty set. Also
            # MIRROR the tuned config into entry.data — the coordinator reads
            # {**data, **options}, so if anything clears options out-of-band the
            # config self-recovers from the data mirror instead of silently
            # reverting to defaults. Legacy flat vane keys are scrubbed from
            # both stores (the zones list is authoritative now).
            merged = {
                k: v
                for k, v in {**self.config_entry.options, **tunables}.items()
                if k not in override_keys
            }
            # If options already owns the effective zones list, update that
            # mirror too so its old profile cannot shadow this save or clear.
            if zones and CONF_ZONES in self.config_entry.options:
                merged[CONF_ZONES] = [dict(zone) for zone in zones]
            if not merged and not zones:
                return self.async_abort(reason="empty_options")
            data = {
                k: v
                for k, v in {**self.config_entry.data, **merged}.items()
                if k not in override_keys
            }
            if zones:
                data[CONF_ZONES] = zones
            # ONE combined update: writing data and options separately fired
            # the update listener twice -> two full back-to-back entry reloads
            # per options save (#14's compute-burst trigger). With options
            # already set here, the create_entry below is a no-change update
            # and fires nothing.
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=data, options=merged
            )
            return self.async_create_entry(title="", data=merged)
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(current, celsius, zones, idle_options),
        )
