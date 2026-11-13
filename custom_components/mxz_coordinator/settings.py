"""What MXZ stores for an entry: every setting, one row each, and where it lives.

A row says what a setting is (its key, kind, default, unit and bounds), how a
save checks it, which Configure (and setup) page shows it, and which of the
entry's two stores keeps it. The config flow builds its pages from these rows;
nothing else lists the settings again.

No Home Assistant dependency, so the rules are tested directly.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from .const import (
    COMFORT_DOMAINS,
    COMFORT_STALE_HOURS_BOUNDS,
    CONF_ACCEPT_HEAD_CHANGES,
    CONF_CHANGEOVER_COOL_BELOW,
    CONF_CHANGEOVER_ENTITY,
    CONF_CHANGEOVER_HEAT_ABOVE,
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    CONF_COIL_DRY_MINUTES,
    CONF_COMFORT_STALE_HOURS,
    CONF_COOL_LOCKOUT_CEILING,
    CONF_DEMAND_THRESHOLD,
    CONF_DRIFT_COOLER_BY,
    CONF_DRIFT_WARMER_BY,
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
    CONF_LOST_VOTE_TTL,
    CONF_MODE_HYSTERESIS,
    CONF_RESTING_MODE_BIAS,
    CONFIRM_WAIT_BOUNDS,
    DEFAULT_ACCEPT_HEAD_CHANGES,
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
    DEFAULT_OPENING_CLOSE_DELAY,
    DEFAULT_OPENING_OPEN_DELAY,
    DEFAULT_RESTING_MODE_BIAS,
    DRIVE_OFFSET_MAX_C,
    DRIVE_OFFSET_MAX_F,
    EVIDENCE_BASES,
    EVIDENCE_UNKNOWN,
    FAN_LADDER,
    HEAD_TYPE_LOCAL,
    HEAD_TYPES,
    IDLE_ACTION_OPTIONS,
    INHIBIT_ACTION_OPTIONS,
    LOST_VOTE_TTL_BOUNDS,
    OPENING_CLOSE_DELAY_BOUNDS,
    OPENING_OPEN_DELAY_BOUNDS,
    RESTING_BIAS_OPTIONS,
    VOTE_BASES,
    VOTE_BASIS_MODE,
    WRITE_INTERVAL_BOUNDS,
    ZONE_ALLOW_DRY,
    ZONE_COMFORT_PRIORITY_ENTITY,
    ZONE_CONFIRM_WAIT,
    ZONE_DRIFT_COOLER_BY,
    ZONE_DRIFT_WARMER_BY,
    ZONE_DRIVE_OFFSET,
    ZONE_DUAL_SETPOINT,
    ZONE_EVIDENCE_BASIS,
    ZONE_FLOOR_HIGH,
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
    ZONE_HANDS_OFF_VOTE_BASIS,
    ZONE_HEAD_TYPE,
    ZONE_MAX_AGE,
    ZONE_OPENING_CLOSE_DELAY,
    ZONE_OPENING_ENTITY,
    ZONE_OPENING_NOTIFY,
    ZONE_OPENING_NOTIFY_SERVICE,
    ZONE_OPENING_OPEN_DELAY,
    ZONE_OPENING_PAUSE,
    ZONE_REPORT_INTERVAL,
    ZONE_SAFETY_PRIORITY_ENTITY,
    ZONE_SAMPLE_SEQUENCE_ATTR,
    ZONE_SAMPLE_TIMESTAMP_ATTR,
    ZONE_STAGE_SENSOR,
    ZONE_STARTUP_GRACE,
    ZONE_VANE_HORIZONTAL,
    ZONE_VANE_VERTICAL,
    ZONE_WRITE_INTERVAL,
    unit_profile,
)

# A setting's kind: what its field is and what it stores.
NUMBER = "number"
FLAG = "flag"
CHOICE = "choice"
ENTITY = "entity"
TEXT = "text"

# What a number box shows as its unit. TEMPERATURE is the system unit.
TEMPERATURE = "temperature"
SECONDS = "s"
MINUTES = "min"
HOURS = "h"

# Bounds that follow the system unit: the profile's engage_bounds, with a step
# of half the low bound (Allowed drift, the uneven drift sides).
ENGAGE = "engage"


@dataclass(frozen=True, slots=True)
class Page:
    """One Configure (and setup Advanced) page: its menu name and its sections, in order."""

    name: str
    sections: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Setting:
    """One entry-level setting.

    ``default`` is what an untouched field submits; None leaves the field
    blank (only a stored value is suggested). ``blank_clears``: a blank field
    stores no key, and clearing one removes the saved key. ``clearable``: an
    entity picker its page always shows, so emptying it stores None.
    ``bounds`` and ``step`` make a number a bounded box (ENGAGE: the unit
    profile's engage_bounds, stepped by half the low bound); without bounds it
    is a free box. ``label`` is the field's name as its page shows it, for the
    Needs attention line (a test holds it to strings.json).
    """

    key: str
    page: str
    section: str | None = None
    kind: str = NUMBER
    default: Any = None
    unit: str | None = None
    bounds: tuple[float, float] | str | None = None
    step: float = 1
    nonnegative: bool = False
    blank_clears: bool = False
    clearable: bool = False
    choices: tuple[str, ...] = ()
    translation_key: str = ""
    domains: tuple[str, ...] = ()
    label: str | None = None


PAGES: dict[str, Page] = {
    "comfort": Page("Comfort", ("advanced",)),
    "fan_idle": Page("Fan and idle"),
    "seasons": Page("Seasons", ("safety",)),
    "limits": Page("Away and setpoint limits", ("setpoint_range",)),
    "standby": Page("Standby hold"),
}

# Every tunable, in the order its page shows it. Each sits on exactly one page,
# and each validated pair (ORDERED_PAIRS) on one page, so a page's own
# validation never needs a value it doesn't show.
SETTINGS: tuple[Setting, ...] = (
    Setting(
        CONF_ENGAGE_DEADBAND, "comfort", default=DEFAULT_ENGAGE_DEADBAND,
        unit=TEMPERATURE, bounds=ENGAGE, nonnegative=True, label="Allowed drift",
    ),
    Setting(
        CONF_DEMAND_THRESHOLD, "comfort", default=DEFAULT_DEMAND_THRESHOLD,
        unit=TEMPERATURE, nonnegative=True, label="Mode switch threshold",
    ),
    Setting(
        CONF_RESTING_MODE_BIAS, "comfort", kind=CHOICE, default=DEFAULT_RESTING_MODE_BIAS,
        choices=RESTING_BIAS_OPTIONS, translation_key="resting_mode_bias",
    ),
    Setting(
        CONF_ACCEPT_HEAD_CHANGES, "comfort", kind=FLAG, default=DEFAULT_ACCEPT_HEAD_CHANGES,
    ),
    Setting(
        CONF_MODE_HYSTERESIS, "comfort", "advanced", default=DEFAULT_MODE_HYSTERESIS,
        unit=SECONDS, nonnegative=True, label="Minimum time between mode switches",
    ),
    Setting(
        CONF_DRIFT_COOLER_BY, "comfort", "advanced", unit=TEMPERATURE, bounds=ENGAGE,
        nonnegative=True, blank_clears=True, label="Let it get cooler by",
    ),
    Setting(
        CONF_DRIFT_WARMER_BY, "comfort", "advanced", unit=TEMPERATURE, bounds=ENGAGE,
        nonnegative=True, blank_clears=True, label="Let it get warmer by",
    ),
    Setting(
        CONF_LOST_VOTE_TTL, "comfort", "advanced", unit=MINUTES, bounds=LOST_VOTE_TTL_BOUNDS,
        blank_clears=True, label="Keep a lost head's vote for",
    ),
    Setting(
        CONF_COMFORT_STALE_HOURS, "comfort", "advanced", unit=HOURS,
        bounds=COMFORT_STALE_HOURS_BOUNDS, nonnegative=True, blank_clears=True,
        label="Comfort priority stale limit",
    ),
    Setting(CONF_FAN_BOOST_ENABLE, "fan_idle", kind=FLAG, default=DEFAULT_FAN_BOOST_ENABLE),
    Setting(
        CONF_FAN_BOOST_MAX, "fan_idle", kind=CHOICE, default=DEFAULT_FAN_BOOST_MAX,
        choices=FAN_LADDER, translation_key="fan_speed",
    ),
    # The choices a page offers are the idle modes every head supports
    # (capabilities.supported_idle_actions); these are all there are.
    Setting(
        CONF_IDLE_ACTION, "fan_idle", kind=CHOICE, default=DEFAULT_IDLE_ACTION,
        choices=IDLE_ACTION_OPTIONS, translation_key="idle_action",
    ),
    Setting(
        CONF_COIL_DRY_MINUTES, "fan_idle", default=DEFAULT_COIL_DRY_MINUTES,
        unit=MINUTES, nonnegative=True, label="Coil drying time",
    ),
    Setting(
        CONF_CHANGEOVER_ENTITY, "seasons", kind=ENTITY, clearable=True,
        domains=("weather", "sensor"),
    ),
    Setting(
        CONF_CHANGEOVER_HEAT_ABOVE, "seasons", default=DEFAULT_CHANGEOVER_HEAT_ABOVE,
        unit=TEMPERATURE, label="Lock out heat when the high is at least",
    ),
    Setting(
        CONF_CHANGEOVER_COOL_BELOW, "seasons", default=DEFAULT_CHANGEOVER_COOL_BELOW,
        unit=TEMPERATURE, label="Lock out cool when the high is at most",
    ),
    Setting(
        CONF_HEAT_LOCKOUT_FLOOR, "seasons", "safety", default=DEFAULT_HEAT_LOCKOUT_FLOOR,
        unit=TEMPERATURE, label="Heat anyway below",
    ),
    Setting(
        CONF_COOL_LOCKOUT_CEILING, "seasons", "safety", default=DEFAULT_COOL_LOCKOUT_CEILING,
        unit=TEMPERATURE, label="Cool anyway above",
    ),
    Setting(
        CONF_ECO_HEAT_MIN, "limits", default=DEFAULT_ECO_HEAT_MIN,
        unit=TEMPERATURE, label="Away low limit",
    ),
    Setting(
        CONF_ECO_COOL_MAX, "limits", default=DEFAULT_ECO_COOL_MAX,
        unit=TEMPERATURE, label="Away high limit",
    ),
    Setting(
        CONF_CLAMP_MIN, "limits", "setpoint_range", default=DEFAULT_CLAMP_MIN,
        unit=TEMPERATURE, label="Lowest setpoint",
    ),
    Setting(
        CONF_CLAMP_MAX, "limits", "setpoint_range", default=DEFAULT_CLAMP_MAX,
        unit=TEMPERATURE, label="Highest setpoint",
    ),
    Setting(
        CONF_INHIBIT_ENTITY, "standby", kind=ENTITY, clearable=True,
        domains=("binary_sensor", "switch", "input_boolean"),
    ),
    Setting(
        CONF_INHIBIT_ACTIVE_STATE, "standby", kind=TEXT, default=DEFAULT_INHIBIT_ACTIVE_STATE,
    ),
    Setting(
        CONF_INHIBIT_ACTION, "standby", kind=CHOICE, default=DEFAULT_INHIBIT_ACTION,
        choices=INHIBIT_ACTION_OPTIONS, translation_key="inhibit_action",
    ),
)
BY_KEY: dict[str, Setting] = {setting.key: setting for setting in SETTINGS}

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
ORDERED_PAIRS: tuple[tuple[str, str, str, bool], ...] = (
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


def page_layout(page: str) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """(top-level fields, {section: fields}) of one page, in display order."""
    rows = [setting for setting in SETTINGS if setting.page == page]
    return (
        tuple(setting.key for setting in rows if setting.section is None),
        {
            name: tuple(setting.key for setting in rows if setting.section == name)
            for name in PAGES[page].sections
        },
    )


def page_fields(page: str) -> tuple[str, ...]:
    """Every field one page shows, sections included."""
    top, groups = page_layout(page)
    return top + tuple(field for fields in groups.values() for field in fields)


def finite(raw: Any) -> float | None:
    """``raw`` as a finite float, or None for anything else.

    The number selectors coerce through ``float()``, which accepts "nan" and
    "inf" and passes them on (NaN fails every min/max comparison, so a
    selector's own bounds never catch it), so finiteness is checked here.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def blank(raw: Any) -> bool:
    """Whether a submitted field is empty (stores no key where blank means none)."""
    return raw in (None, "")


def tunable_errors(values: Mapping[str, Any]) -> dict[str, str]:
    """{field: error_key} for every submitted tunable that fails validation.

    Runs after the schema/selectors have already coerced the submission, so
    this only needs to catch what they miss: non-finite numbers, negative
    durations, and the cross-field orderings in ``ORDERED_PAIRS``. An ordering
    error names BOTH fields, because either one of them can be the wrong number.
    """
    errors: dict[str, str] = {}
    parsed: dict[str, float] = {}
    numbers = [setting for setting in SETTINGS if setting.kind == NUMBER]
    for setting in numbers:
        if setting.key not in values:
            continue
        if (value := finite(values[setting.key])) is None:
            errors[setting.key] = "not_a_number"
        else:
            parsed[setting.key] = value

    for setting in numbers:
        if setting.nonnegative and parsed.get(setting.key, 0.0) < 0:
            errors[setting.key] = "must_not_be_negative"

    for low_key, high_key, error, may_be_equal in ORDERED_PAIRS:
        if low_key in errors or high_key in errors:
            continue  # the value itself is wrong; ordering it says nothing
        low, high = parsed.get(low_key), parsed.get(high_key)
        if low is None or high is None:
            continue  # a field this submission never carried
        if high < low or (high == low and not may_be_equal):
            errors[low_key] = errors[high_key] = error
    return errors


# --- The room page ------------------------------------------------------------
# Each field on a room's page (Configure -> Rooms -> the room) folds into that
# room's record in the zones list, never into a flat key. ``key`` is the field's
# name on the page and in strings.json; ``zone_key`` is what the record stores.

# How a submitted value is stored in the room's record. Every rule stores no
# key for "off" or blank, so a room that never set a field stores exactly what
# it stored before the field existed.
STORE_VALUE = "value"  # the value while it is set
STORE_TRUE = "true"  # True while on
STORE_CHOICE = "choice"  # the choice while it is not the default
STORE_FLOAT = "float"  # the number while it is set
STORE_NON_DEFAULT = "non_default"  # the number while it is set and not the default
STORE_RAW = "raw"  # the value as submitted while it is set
STORE_HOOK = "hook"  # stored by the room page itself (head type and cloud settings)

# Bounds that follow the system unit: 0 up to the unit's largest drive offset.
DRIVE_OFFSET = "drive_offset"

# The room page's collapsed sections, in display order.
ROOM_SECTIONS = ("drift", "freshness", "floor", "dual", "cloud", "opening")


@dataclass(frozen=True, slots=True)
class RoomField:
    """One field on a room's page.

    ``default`` is what the page shows while the record stores nothing.
    ``custom_value``: a choice the page keeps as typed, even outside its
    choices. ``needs_dry``: shown, and stored, only for a head that lists dry,
    so a page that doesn't show it leaves what is stored alone.
    """

    key: str
    zone_key: str
    section: str | None = None
    kind: str = ENTITY
    store: str = STORE_VALUE
    default: Any = None
    unit: str | None = None
    bounds: tuple[float, float] | str | None = None
    step: float = 1
    choices: tuple[str, ...] = ()
    translation_key: str = ""
    custom_value: bool = False
    domains: tuple[str, ...] = ()
    needs_dry: bool = False


ROOM_FIELDS: tuple[RoomField, ...] = (
    RoomField(
        "head_type", ZONE_HEAD_TYPE, kind=CHOICE, store=STORE_HOOK, default=HEAD_TYPE_LOCAL,
        choices=HEAD_TYPES, translation_key="head_type",
    ),
    # Wiring. ESPHome text_sensors register in the sensor domain, so a sensor
    # picker covers the airflow (stage) sensor.
    RoomField("stage", ZONE_STAGE_SENSOR, domains=("sensor",)),
    RoomField("vane_vertical", ZONE_VANE_VERTICAL, domains=("select",)),
    RoomField("vane_horizontal", ZONE_VANE_HORIZONTAL, domains=("select",)),
    RoomField("safety_priority_entity", ZONE_SAFETY_PRIORITY_ENTITY, domains=("binary_sensor",)),
    RoomField("comfort_priority_entity", ZONE_COMFORT_PRIORITY_ENTITY, domains=COMFORT_DOMAINS),
    RoomField("hands_off", ZONE_HANDS_OFF, kind=FLAG, store=STORE_TRUE),
    RoomField(
        "hands_off_vote_basis", ZONE_HANDS_OFF_VOTE_BASIS, kind=CHOICE, store=STORE_CHOICE,
        default=VOTE_BASIS_MODE, choices=VOTE_BASES, translation_key="hands_off_vote_basis",
    ),
    RoomField("allow_dry", ZONE_ALLOW_DRY, kind=FLAG, store=STORE_TRUE, needs_dry=True),
    # The room's own uneven drift. The page keys differ from the global
    # CONF_DRIFT_* keys on purpose: page keys are scrubbed from flat storage,
    # and the global ones are stored flat.
    RoomField(
        "room_cooler_by", ZONE_DRIFT_COOLER_BY, "drift", kind=NUMBER, store=STORE_FLOAT,
        unit=TEMPERATURE, bounds=ENGAGE,
    ),
    RoomField(
        "room_warmer_by", ZONE_DRIFT_WARMER_BY, "drift", kind=NUMBER, store=STORE_FLOAT,
        unit=TEMPERATURE, bounds=ENGAGE,
    ),
    # The sensor freshness profile (see const.ZONE_REPORT_INTERVAL). Stored as
    # submitted; a profile left wholly empty is the unknown cadence and stores
    # nothing (store_room).
    RoomField(
        "report_interval", ZONE_REPORT_INTERVAL, "freshness", kind=NUMBER, store=STORE_RAW,
        unit=MINUTES,
    ),
    RoomField("max_age", ZONE_MAX_AGE, "freshness", kind=NUMBER, store=STORE_RAW, unit=MINUTES),
    RoomField(
        "startup_grace", ZONE_STARTUP_GRACE, "freshness", kind=NUMBER, store=STORE_RAW,
        unit=MINUTES,
    ),
    # A choice kept as typed, so an invalid raw API submission stays inside the
    # form, where it gets one diagnostic and changes nothing.
    RoomField(
        "evidence_basis", ZONE_EVIDENCE_BASIS, "freshness", kind=CHOICE, store=STORE_RAW,
        default=EVIDENCE_UNKNOWN, choices=EVIDENCE_BASES, translation_key="evidence_basis",
        custom_value=True,
    ),
    RoomField(
        "sample_timestamp_attribute", ZONE_SAMPLE_TIMESTAMP_ATTR, "freshness", kind=TEXT,
        store=STORE_RAW,
    ),
    RoomField(
        "sample_sequence_attribute", ZONE_SAMPLE_SEQUENCE_ATTR, "freshness", kind=TEXT,
        store=STORE_RAW,
    ),
    # Freeze floor and overheat ceiling, in the system unit: blank is none.
    RoomField("floor_low", ZONE_FLOOR_LOW, "floor", kind=NUMBER, store=STORE_FLOAT, unit=TEMPERATURE),
    RoomField("floor_high", ZONE_FLOOR_HIGH, "floor", kind=NUMBER, store=STORE_FLOAT, unit=TEMPERATURE),
    RoomField("dual_setpoint", ZONE_DUAL_SETPOINT, "dual", kind=FLAG, store=STORE_TRUE),
    # A cloud or generic head's settings (beta). Their defaults follow the head
    # type and the unit, so the room page shows and stores them itself.
    RoomField(
        "drive_offset", ZONE_DRIVE_OFFSET, "cloud", kind=NUMBER, store=STORE_HOOK,
        unit=TEMPERATURE, bounds=DRIVE_OFFSET, step=0.5,
    ),
    RoomField(
        "write_interval", ZONE_WRITE_INTERVAL, "cloud", kind=NUMBER, store=STORE_HOOK,
        unit=SECONDS, bounds=WRITE_INTERVAL_BOUNDS,
    ),
    RoomField(
        "confirm_wait", ZONE_CONFIRM_WAIT, "cloud", kind=NUMBER, store=STORE_HOOK,
        unit=SECONDS, bounds=CONFIRM_WAIT_BOUNDS,
    ),
    # Door/window pause. The page keys are the zone keys.
    RoomField(ZONE_OPENING_ENTITY, ZONE_OPENING_ENTITY, "opening", domains=("binary_sensor",)),
    RoomField(ZONE_OPENING_PAUSE, ZONE_OPENING_PAUSE, "opening", kind=FLAG, store=STORE_TRUE),
    RoomField(
        ZONE_OPENING_OPEN_DELAY, ZONE_OPENING_OPEN_DELAY, "opening", kind=NUMBER,
        store=STORE_NON_DEFAULT, default=DEFAULT_OPENING_OPEN_DELAY, unit=SECONDS,
        bounds=OPENING_OPEN_DELAY_BOUNDS,
    ),
    RoomField(
        ZONE_OPENING_CLOSE_DELAY, ZONE_OPENING_CLOSE_DELAY, "opening", kind=NUMBER,
        store=STORE_NON_DEFAULT, default=DEFAULT_OPENING_CLOSE_DELAY, unit=SECONDS,
        bounds=OPENING_CLOSE_DELAY_BOUNDS,
    ),
    RoomField(ZONE_OPENING_NOTIFY, ZONE_OPENING_NOTIFY, "opening", kind=FLAG, store=STORE_TRUE),
    RoomField(
        ZONE_OPENING_NOTIFY_SERVICE, ZONE_OPENING_NOTIFY_SERVICE, "opening", kind=TEXT,
    ),
)
ROOM_FIELD: dict[str, RoomField] = {field.key: field for field in ROOM_FIELDS}


def room_fields(section: str | None) -> tuple[RoomField, ...]:
    """The room page's fields in one section (None: the top of the page), in order."""
    return tuple(field for field in ROOM_FIELDS if field.section == section)


def number_bounds(
    bounds: tuple[float, float] | str | None, step: float, celsius: bool
) -> tuple[float, float, float] | None:
    """(min, max, step) of a bounded number box in the system unit; None for a free box."""
    if bounds is None:
        return None
    if bounds == ENGAGE:
        low, high = unit_profile(celsius)["engage_bounds"]
        return low, high, low / 2
    if bounds == DRIVE_OFFSET:
        return 0.0, DRIVE_OFFSET_MAX_C if celsius else DRIVE_OFFSET_MAX_F, step
    low, high = cast("tuple[float, float]", bounds)
    return low, high, step


def shown_choice(field: RoomField, value: Any) -> Any:
    """What a choice field shows for ``value``: a known choice, else its default.

    A field that keeps custom values shows any non-empty value as it is.
    """
    if field.custom_value:
        return value or field.default
    return value if value in field.choices else field.default


def room_view(zone: Mapping[str, Any]) -> dict[str, Any]:
    """A stored room record as its page's field values (head type and cloud as stored)."""
    view: dict[str, Any] = {}
    for field in ROOM_FIELDS:
        stored = zone.get(field.zone_key)
        if field.kind == FLAG:
            view[field.key] = stored is True
        elif field.kind == CHOICE and not field.custom_value:
            view[field.key] = shown_choice(field, stored)
        elif field.store == STORE_NON_DEFAULT:
            view[field.key] = zone.get(field.zone_key, field.default)
        else:
            view[field.key] = stored
    return view


def store_room(zone: dict[str, Any], values: Mapping[str, Any], *, dry: bool) -> None:
    """Fold one room page's submitted ``values`` into the room's record ``zone``.

    Every field is rendered and an untouched field is submitted with its
    value, so an absent or empty field is the user clearing it. A field the
    page didn't show (Allow Dry for a head that lists no dry) leaves what is
    stored alone, and the hook fields are the page's own to store. A wholly
    empty freshness profile is an explicit return to the unknown cadence.
    """
    empty_profile = all(
        values.get(field.key) in (None, "", EVIDENCE_UNKNOWN)
        if field.kind == CHOICE else blank(values.get(field.key))
        for field in room_fields("freshness")
    )
    for field in ROOM_FIELDS:
        if field.store == STORE_HOOK or (field.needs_dry and not dry):
            continue
        value: Any = values.get(field.key)
        if field.store == STORE_TRUE:
            keep: Any = True if value is True else None
        elif field.store == STORE_CHOICE:
            keep = value if value in field.choices and value != field.default else None
        elif field.store == STORE_FLOAT:
            keep = None if blank(value) else float(value)
        elif field.store == STORE_NON_DEFAULT:
            keep = None if blank(value) or float(value) == field.default else float(value)
        elif field.store == STORE_RAW:
            keep = None if blank(value) or (field.section == "freshness" and empty_profile) else value
        else:
            keep = value or None
        if keep is None:
            zone.pop(field.zone_key, None)
        else:
            zone[field.zone_key] = keep
