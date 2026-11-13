"""Decision + actuator + self-heal core for MXZ Coordinator.

This is the Python port of three pieces of ``packages/mxz_coordinator.yaml``:

* the decision ``template`` sensor ``sensor.mxz_plan``  -> :meth:`MXZCoordinator._compute`
* the actuator ``script.mxz_coordinate`` (sole head-writer) -> :meth:`MXZCoordinator._apply`
* the trigger + two self-heal ``automation``s             -> the listeners wired in
  :meth:`MXZCoordinator.async_setup`

The pure helpers (:func:`room_call`, :func:`shared_mode`, :func:`setpoints`,
:func:`head_action`) carry the decision math with no Home Assistant dependency so they
can be unit-tested directly against the package's validated truth table.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import operator
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ClimateEntityFeature,
)
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    EVENT_CORE_CONFIG_UPDATE,
    STATE_ON,
    STATE_UNAVAILABLE,
    UnitOfTemperature,
)
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_start
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    REQUEST_REFRESH_DEFAULT_COOLDOWN,
    REQUEST_REFRESH_DEFAULT_IMMEDIATE,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .capabilities import head_has_fan_auto
from .clock import Clock
from .const import (
    ACTION_VOTE_HOLD,
    ACTION_VOTE_SETTLE,
    ADOPT_DELAY,
    BAND_DRIFT_DELAY,
    BANNED_MODES,
    CHANGEOVER_INTERVAL_MINUTES,
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
    CONF_NOTIFY_SERVICE,
    CONF_RESTING_MODE_BIAS,
    CONF_SAVED_TEMP_UNIT,
    CONF_ZONES,
    DEFAULT_ACCEPT_HEAD_CHANGES,
    DEFAULT_COIL_DRY_MINUTES,
    DEFAULT_COMFORT_STALE_HOURS,
    DEFAULT_FAN_BOOST_ENABLE,
    DEFAULT_FAN_BOOST_MAX,
    DEFAULT_IDLE_ACTION,
    DEFAULT_INHIBIT_ACTION,
    DEFAULT_INHIBIT_ACTIVE_STATE,
    DEFAULT_LOST_VOTE_TTL,
    DEFAULT_MODE_HYSTERESIS,
    DEFAULT_OPENING_CLOSE_DELAY,
    DEFAULT_OPENING_OPEN_DELAY,
    DEFAULT_RESTING_MODE_BIAS,
    DEMAND_NEUTRAL,
    DOMAIN,
    DUAL_HANDOFF_KEY,
    DUAL_ZONE_ENTITY_SUFFIXES,
    ENGAGE_SATISFIED,
    EVENT_RECOMPUTE,
    EVIDENCE_HA_WRITE,
    FAN_LADDER,
    FLOOR_RANGE_C,
    FLOOR_RANGE_F,
    HEAD_TYPE_LOCAL,
    HEAD_WRITES_REMEMBERED,
    HEAL_NOTICE_SECONDS,
    HEALTH_AWAITING,
    HEALTH_ELIGIBLE,
    HEALTH_HEALTHY,
    HEALTH_INVALID,
    HEALTH_REPORT_SENSITIVE,
    HEALTH_STALE,
    HEALTH_UNHEALTHY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
    INHIBIT_ACTION_ECO,
    INHIBIT_ACTION_FAN_ONLY,
    INHIBIT_ACTION_OFF,
    KEY_COOL_LOCKOUT,
    KEY_HEAT_LOCKOUT,
    LOST_VOTE_TTL_BOUNDS,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
    OFF_WHILE_ENABLED_DELAY,
    OFFSET_HEAD_TYPES,
    OPENING_CLOSE_DELAY_BOUNDS,
    OPENING_OPEN_DELAY_BOUNDS,
    STARTUP_RECOVER_DELAY,
    UNAVAILABLE_STATES,
    VANE_KICK_APPLY,
    VANE_KICK_RETIRE_TIMEOUT,
    VANE_KICK_SPINUP,
    VOTE_BASIS_ACTION,
    VOTE_BASIS_MODE,
    VOTE_EXPIRED,
    ZONE_ALLOW_DRY,
    ZONE_CLIMATE,
    ZONE_COMFORT_PRIORITY_ENTITY,
    ZONE_CONFIRM_WAIT,
    ZONE_DRIFT_COOLER_BY,
    ZONE_DRIFT_WARMER_BY,
    ZONE_DRIVE_OFFSET,
    ZONE_DRIVE_OFFSET_UNIT,
    ZONE_DUAL_SETPOINT,
    ZONE_EVIDENCE_BASIS,
    ZONE_FLOOR_HIGH,
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
    ZONE_HANDS_OFF_VOTE_BASIS,
    ZONE_HEAD_TYPE,
    ZONE_MAX_AGE,
    ZONE_NAME,
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
    ZONE_SENSOR,
    ZONE_STAGE_SENSOR,
    ZONE_STARTUP_GRACE,
    ZONE_VANE_HORIZONTAL,
    ZONE_VANE_VERTICAL,
    ZONE_WRITE_INTERVAL,
    unit_profile,
    zone_slug,
)
from .delivery import DeliveryQueue
from .dry import DryMode
from .fan_hold import FanHold
from .floor import FloorGuard
from .heads import HeadDrivers
from .host import Effect
from .logic import (
    PRIORITY_DEMAND,
    PRIORITY_FLOOR_HEAT,
    PRIORITY_SAFETY,
    PRIORITY_VOTE,
    Demand,
    HeadSight,
    HeadWriteMemory,
    RangeError,
    arbitrate,
    as_float,
    bounded,
    call_edges,
    comfort_boost,
    comfort_priority,
    comfort_stale_in,
    engage_with_latch,
    evidence_contract,
    finite_number,
    freshness_window,
    head_action,
    range_delta,
    room_call,
    sample_evidence,
    season_lockouts,
    sensor_health,
    setpoints,
    shift_range,
    snapped_midpoint,
)
from .openings import OpeningPause
from .plan import Plan, RoomPlan
from .settings import entry_config, one_home
from .units import clamp, offset_in_unit, snap_to_step, target_in_unit

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import EventStateChangedData, EventStateReportedData, State

_LOGGER = logging.getLogger(__name__)

# What MXZ remembers of its head writes (logic.HeadWriteMemory), by service field.
_HEAD_WRITE_MODE = "hvac_mode"
_HEAD_WRITE_FIELDS = (_HEAD_WRITE_MODE, "temperature", "target_temp_low", "target_temp_high")


def read_room_temp(state: Any, system_unit: str) -> float | None:
    """Return a room sensor's finite temperature in ``system_unit``, else None.

    The ONE reader every room-sensor consumer shares — automatic demand and the
    thermostat facade both call it, so a tile can never show a number the
    coordinator has rejected, nor show it in the wrong unit.

    HA normally converts temperature SensorEntity states to their configured
    display unit before storing them. Read that State unit as authoritative:
    an already-normalized state needs no second conversion, while a supported
    per-entity unit override is converted once into the system unit. A sensor
    declaring NO unit keeps its long-standing system-unit reading (unitless
    template sensors have always worked); only an explicitly unsupported unit
    is rejected. So is a malformed one: HA stores whatever attribute a source
    publishes, and a ``[]`` or ``{}`` unit is not hashable, so it must be
    rejected by type BEFORE the membership test or that test itself raises and
    takes the whole refresh down. Non-finite values (``nan``, ``±inf``) are
    invalid: nothing can be compared against them and HA's own display
    rounding raises on them. Any finite temperature is accepted; climate
    plausibility limits are policy, not sensor validity.
    """
    if state is None or state.state in UNAVAILABLE_STATES:
        return None
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit is None:
        unit = system_unit
    elif not isinstance(unit, str):
        return None
    if (
        unit not in TemperatureConverter.VALID_UNITS
        or system_unit not in TemperatureConverter.VALID_UNITS
    ):
        return None
    try:
        value = float(state.state)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(value):
        return None
    if unit != system_unit:
        value = TemperatureConverter.convert(value, unit, system_unit)
    return value if math.isfinite(value) else None


def _read_temp(
    state: Any, fallback: float, system_unit: str
) -> tuple[bool, float]:
    """Demand-path adapter: ``(ok, value)``, with ``fallback`` when invalid.

    Plan and actuator arithmetic still needs a number for a room that gets no
    vote, so an invalid reading yields ``ok=False`` plus the fallback. ``ok`` is
    what keeps that fallback from voting (:func:`room_call`), and the fallback
    is never published as the room's temperature.
    """
    value = read_room_temp(state, system_unit)
    if value is None:
        return (False, fallback)
    return (True, value)


def _read_marker(value: Any, *, marker_is_a_time: bool) -> float | None:
    """One trusted sample marker as a comparable number, else None.

    A sample TIME is an aware datetime or an ISO string carrying an offset —
    HA stores whatever an integration publishes, and a time with no zone is
    not a time this can compare against a UTC clock. A sample SEQUENCE is any
    finite number (``True`` is a checkbox, not a sequence), kept as the number
    it is: an integer stays an integer, so a count already past 2**53 still
    advances by one instead of rounding back onto the last one accepted.
    Anything else is an unusable marker: the write it rides on then proves
    nothing.
    """
    if not marker_is_a_time:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, str):
        value = dt_util.parse_datetime(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.timestamp()


@dataclass
class Zone:
    """One indoor head + its room sensor (+ optional vanes) and runtime state.

    ``index`` is the standoff priority (0 = highest); ``slug`` is the stable
    unique-id fragment ("primary"/"secondary" for zones 0/1, zone_N beyond).
    ``target``/``enable`` are owned by the zone's number/switch entities (seeded
    on restore, mutated on user action).
    """

    index: int
    slug: str
    name: str
    climate_id: str
    sensor_id: str
    vane_vertical_id: str | None = None
    vane_horizontal_id: str | None = None
    stage_sensor_id: str | None = None
    target: float = 70.0
    enable: bool = field(default=False)
    # Per-room re-engage drift override (#18). None = use the global
    # engage_deadband. Owned by the zone's drift number entity, exactly like
    # target/enable; automations write it (presence tiers etc.).
    drift: float | None = None
    # Hands-off opt-in (ZONE_HANDS_OFF), from config; only a stored True opts in.
    hands_off: bool = False
    # What its released head's vote follows (ZONE_HANDS_OFF_VOTE_BASIS), from
    # config; only a stored "action" opts in, anything else is "mode".
    vote_basis: str = VOTE_BASIS_MODE
    # Optional binary_sensor (ZONE_SAFETY_PRIORITY_ENTITY): "on" marks this
    # zone's Demand PRIORITY_SAFETY for this tick, regardless of whether its
    # direction comes from room_call() or a hands-off vote. Static wiring,
    # same tier as climate_id/sensor_id, not a live-adjustable number.
    safety_priority_entity: str | None = None
    # Optional Comfort priority entity (ZONE_COMFORT_PRIORITY_ENTITY): read
    # live every compute into this room's boost (logic.comfort_priority), which
    # orders its demand and votes inside their own tier. Static wiring, like
    # the Safety priority sensor.
    comfort_priority_entity: str | None = None
    # This SENSOR's reporting contract, as configured (durations in minutes,
    # or None when the room's cadence is unknown; the basis defaults to
    # unknown). Read once into the resolved freshness window and evidence
    # contract; kept raw here because config is not validated on the way in.
    report_interval: Any = None
    max_age: Any = None
    startup_grace: Any = None
    evidence_basis: Any = None
    sample_timestamp_attribute: Any = None
    sample_sequence_attribute: Any = None
    # Optional freeze floor / overheat ceiling (ZONE_FLOOR_LOW/HIGH), system
    # unit, acting only while the room is disabled. None = not configured; a
    # stored value that is not a finite number is none too.
    floor_low: float | None = None
    floor_high: float | None = None
    # This room's own uneven drift (ZONE_DRIFT_COOLER_BY/WARMER_BY), from
    # config, clamped into the unit profile's engage_bounds on load. None =
    # not set (or not a finite number): that side falls through.
    drift_cooler_by: float | None = None
    drift_warmer_by: float | None = None
    # Dual setpoint (ZONE_DUAL_SETPOINT), from config; only a stored True opts
    # in. Then target_low / target_high are its Heat to and Cool to, owned by
    # the room's heat-to and cool-to numbers (None until they restore), and
    # target is their midpoint, kept in step by every range write.
    dual: bool = False
    target_low: float | None = None
    target_high: float | None = None
    # The options flow's one-shot handoff for this room ({"seed_range": True}
    # when it was just switched on, {"target": t} when just switched off),
    # taken when the coordinator is built and read once by the numbers.
    handoff: dict[str, Any] | None = None
    # A restored range that no longer fit and was re-centered: the room's
    # detail says so until the range is next written.
    range_note: str | None = None
    # Head type (ZONE_HEAD_TYPE), from config; only a stored "cloud" or
    # "generic" opts in, and the coordinator turns an ESPHome head back to
    # local. A cloud or generic room's
    # drive offset (in drive_offset_unit, the unit it was set in, if stored)
    # and its write interval and confirm wait (seconds), as stored (None = the
    # default); resolved with the head type, the offset into the system unit.
    head_type: str = HEAD_TYPE_LOCAL
    drive_offset: float | None = None
    drive_offset_unit: str | None = None
    write_interval: float | None = None
    confirm_wait: float | None = None
    # Allow Dry mode (ZONE_ALLOW_DRY), from config; only a stored True opts in.
    allow_dry: bool = False
    # Door/window pause (ZONE_OPENING_*), from config: the opening entity, the
    # pause (only a stored True opts in), its delays in seconds, and its
    # notices. It acts only with both an entity and the pause on.
    opening_entity: str | None = None
    opening_pause: bool = False
    opening_open_delay: float = float(DEFAULT_OPENING_OPEN_DELAY)
    opening_close_delay: float = float(DEFAULT_OPENING_CLOSE_DELAY)
    opening_notify: bool = False
    opening_notify_service: str | None = None

    @property
    def opening_active(self) -> bool:
        """Whether this room pauses for its door/window."""
        return bool(self.opening_entity) and self.opening_pause


# A room's optional wiring, read as ``z.get(key) or None``, so an empty value
# and no key are the same room. The config flow stores None when it detects no
# vane or stage sensor, and the room page drops an empty one on save.
_OPTIONAL_WIRING_KEYS = frozenset(
    (
        ZONE_VANE_VERTICAL, ZONE_VANE_HORIZONTAL, ZONE_STAGE_SENSOR,
        ZONE_SAFETY_PRIORITY_ENTITY, ZONE_COMFORT_PRIORITY_ENTITY,
        ZONE_OPENING_ENTITY, ZONE_OPENING_NOTIFY_SERVICE,
    )
)


def _without_dual(conf: dict[str, Any]) -> dict[str, Any]:
    """The config with each room's dual setpoint key and empty wiring left out (for comparing saves)."""
    raw = conf.get(CONF_ZONES)
    if not isinstance(raw, list):
        return conf
    return {
        **conf,
        CONF_ZONES: [
            {
                k: v for k, v in z.items()
                if k != ZONE_DUAL_SETPOINT and (v or k not in _OPTIONAL_WIRING_KEYS)
            }
            if isinstance(z, dict) else z
            for z in raw
        ],
    }


def _parse_zones(
    conf: dict[str, Any],
    target_default: float,
    engage_bounds: tuple[float, float],
) -> list[Zone]:
    """Build the ordered Zone list from the entry's rooms (``settings.entry_config``).

    List order is standoff priority. ``engage_bounds`` clamps a room's own
    uneven drift, exactly as it clamps the global sides.
    """
    raw = conf.get(CONF_ZONES) or []
    zones = [
        Zone(
            index=i,
            slug=zone_slug(i),
            name=_zone_name(z, i),
            climate_id=z[ZONE_CLIMATE],
            sensor_id=z[ZONE_SENSOR],
            vane_vertical_id=z.get(ZONE_VANE_VERTICAL) or None,
            vane_horizontal_id=z.get(ZONE_VANE_HORIZONTAL) or None,
            stage_sensor_id=z.get(ZONE_STAGE_SENSOR) or None,
            target=target_default,
            hands_off=z.get(ZONE_HANDS_OFF) is True,
            vote_basis=(
                VOTE_BASIS_ACTION
                if z.get(ZONE_HANDS_OFF_VOTE_BASIS) == VOTE_BASIS_ACTION
                else VOTE_BASIS_MODE
            ),
            safety_priority_entity=z.get(ZONE_SAFETY_PRIORITY_ENTITY) or None,
            comfort_priority_entity=z.get(ZONE_COMFORT_PRIORITY_ENTITY) or None,
            report_interval=z.get(ZONE_REPORT_INTERVAL),
            max_age=z.get(ZONE_MAX_AGE),
            startup_grace=z.get(ZONE_STARTUP_GRACE),
            evidence_basis=z.get(ZONE_EVIDENCE_BASIS),
            sample_timestamp_attribute=z.get(ZONE_SAMPLE_TIMESTAMP_ATTR),
            sample_sequence_attribute=z.get(ZONE_SAMPLE_SEQUENCE_ATTR),
            floor_low=_finite(z.get(ZONE_FLOOR_LOW)),
            floor_high=_finite(z.get(ZONE_FLOOR_HIGH)),
            drift_cooler_by=_drift_side(
                z.get(ZONE_DRIFT_COOLER_BY),
                *engage_bounds,
                f'"Let it get cooler by" for {_zone_name(z, i)}',
            ),
            drift_warmer_by=_drift_side(
                z.get(ZONE_DRIFT_WARMER_BY),
                *engage_bounds,
                f'"Let it get warmer by" for {_zone_name(z, i)}',
            ),
            dual=z.get(ZONE_DUAL_SETPOINT) is True,
            head_type=(
                z[ZONE_HEAD_TYPE] if z.get(ZONE_HEAD_TYPE) in OFFSET_HEAD_TYPES
                else HEAD_TYPE_LOCAL
            ),
            drive_offset=_finite(z.get(ZONE_DRIVE_OFFSET)),
            drive_offset_unit=z.get(ZONE_DRIVE_OFFSET_UNIT),
            write_interval=_finite(z.get(ZONE_WRITE_INTERVAL)),
            confirm_wait=_finite(z.get(ZONE_CONFIRM_WAIT)),
            allow_dry=z.get(ZONE_ALLOW_DRY) is True,
            opening_entity=z.get(ZONE_OPENING_ENTITY) or None,
            opening_pause=z.get(ZONE_OPENING_PAUSE) is True,
            opening_open_delay=bounded(
                _stored_number(
                    z.get(ZONE_OPENING_OPEN_DELAY), f'"Open delay" for {_zone_name(z, i)}'
                ),
                DEFAULT_OPENING_OPEN_DELAY, OPENING_OPEN_DELAY_BOUNDS,
            ),
            opening_close_delay=bounded(
                _stored_number(
                    z.get(ZONE_OPENING_CLOSE_DELAY), f'"Close delay" for {_zone_name(z, i)}'
                ),
                DEFAULT_OPENING_CLOSE_DELAY, OPENING_CLOSE_DELAY_BOUNDS,
            ),
            opening_notify=z.get(ZONE_OPENING_NOTIFY) is True,
            opening_notify_service=z.get(ZONE_OPENING_NOTIFY_SERVICE) or None,
        )
        for i, z in enumerate(raw)
    ]
    return zones


# How long after a coast (an engage latch disengaging) the room is looked at
# again. It is the request-refresh cooldown plus a second, because that is the
# interval a head WITH a fan mode already gets: its echo asks for a refresh,
# which the debouncer runs one cooldown later. Landing after that cooldown
# leaves the echo path in charge on the heads that have one — that refresh
# coasts nothing, so it drops this wakeup — and gives the heads that have no
# fan mode the same re-evaluation instead of none.
COAST_FOLLOWUP_DELAY = REQUEST_REFRESH_DEFAULT_COOLDOWN + 1  # s


# ---------------------------------------------------------------------------
# The last real mode change, kept across reloads and restarts
# ---------------------------------------------------------------------------
# One small storage file per config entry, written whenever the shared mode
# changes (and when a room's run takes over a mode votes chose) and read once
# per setup. A Store, not the shared mode select's restore data: restore data
# reaches disk only at shutdown and every 15 minutes, so a crash soon after a
# change would forget it; a disabled select restores nothing at all; and it
# restores only after the first plan has been computed. Times are wall-clock
# UTC epoch seconds, the only clock that means anything to the next process.
#
# The record holds the time of the last real change (MODE_CHANGE_KEY, absent
# while none was ever made) and the shared mode as of MODE_AT_KEY, with whether
# hands-off votes alone set it (MODE_BY_VOTES_KEY). Earlier versions wrote the
# time only; their records read as before, with no mode on record.
MODE_CHANGE_STORE_VERSION = 1
MODE_CHANGE_KEY = "last_mode_change"
MODE_KEY = "mode"
MODE_AT_KEY = "mode_at"
MODE_BY_VOTES_KEY = "mode_by_votes"


@dataclass(frozen=True)
class ModeRecord:
    """The entry's mode-change record, as a start reads it.

    ``changed_at`` is the last real cool/heat change (None: none on record).
    ``mode`` is the shared mode when the record was written at ``mode_at``,
    and ``by_votes`` whether hands-off votes alone had set it; ``mode`` is
    None when the record has no readable mode (an earlier version wrote it).
    """

    changed_at: float | None = None
    mode: str | None = None
    mode_at: float = 0.0
    by_votes: bool = False


def _mode_change_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """This entry's store, one instance per Home Assistant run.

    Shared by every coordinator incarnation of the entry, so a reload reads a
    change that is still waiting to be written (Store serves pending data).
    """
    stores: dict[str, Store[dict[str, Any]]] = hass.data.setdefault(
        f"{DOMAIN}_mode_change_stores", {}
    )
    if entry_id not in stores:
        stores[entry_id] = Store(
            hass, MODE_CHANGE_STORE_VERSION, f"{DOMAIN}.{entry_id}.mode_change"
        )
    return stores[entry_id]


async def async_load_mode_record(hass: HomeAssistant, entry_id: str) -> ModeRecord:
    """The entry's record of the shared mode and its last real change.

    No change time means no change was ever recorded (a fresh install, the
    first start on a version that did not keep it, or a record that so far
    holds only a mode votes chose): there is nothing to wait out. A record
    that is there but whose time is unreadable is not a fresh install; it
    counts as a change made now, so the hold is the full window, as it was
    before this was kept. A mode that is not readable is no mode on record.
    """
    stored = await _mode_change_store(hass, entry_id).async_load()
    if stored is None:
        return ModeRecord()
    data: Any = stored if isinstance(stored, dict) else {}
    mode, mode_at, by_votes = (
        data.get(MODE_KEY), data.get(MODE_AT_KEY), data.get(MODE_BY_VOTES_KEY)
    )
    has_mode = (
        mode in (MODE_COOL, MODE_HEAT) and finite_number(mode_at)
        and isinstance(by_votes, bool)
    )
    shared: dict[str, Any] = (
        {"mode": mode, "mode_at": float(mode_at), "by_votes": by_votes} if has_mode else {}
    )
    value = data.get(MODE_CHANGE_KEY)
    if finite_number(value):
        return ModeRecord(changed_at=float(value), **shared)
    if has_mode and MODE_CHANGE_KEY not in data:
        return ModeRecord(changed_at=None, **shared)
    _LOGGER.warning(
        "MXZ: the stored time of the last mode change is unreadable (%r); "
        "holding the next cool/heat change for the full window",
        stored,
    )
    return ModeRecord(changed_at=dt_util.utcnow().timestamp(), **shared)


async def async_load_last_mode_change(hass: HomeAssistant, entry_id: str) -> float | None:
    """The stored time of the last real mode change, or None if there is none."""
    return (await async_load_mode_record(hass, entry_id)).changed_at


async def async_remove_last_mode_change(hass: HomeAssistant, entry_id: str) -> None:
    """Delete a removed entry's record; a re-added entry starts with none."""
    await _mode_change_store(hass, entry_id).async_remove()
    hass.data.get(f"{DOMAIN}_mode_change_stores", {}).pop(entry_id, None)


def mode_change_stamp(stored: float | None, now: float) -> float | None:
    """The stamp on MXZ's clock, read as ``now``, for a stored wall-clock change time.

    The gate itself runs on the monotonic clock (an NTP step inside one run
    must not move it), so the stored time becomes "this many seconds ago" on
    that clock once, at startup. A stored time in the future (the clock was
    set back, or it ran ahead when the change was written) counts as now:
    the hold is then the full window from this start, never longer.
    """
    if stored is None:
        return None
    ago = max(0.0, dt_util.utcnow().timestamp() - stored)
    return now - ago


# ---------------------------------------------------------------------------
# Door/window pause, kept across reloads and restarts
# What an entry saves in the system unit, by how it follows a °F/°C switch.
_SAVED_TEMPERATURES = (
    CONF_CLAMP_MIN,
    CONF_CLAMP_MAX,
    CONF_ECO_HEAT_MIN,
    CONF_ECO_COOL_MAX,
    CONF_HEAT_LOCKOUT_FLOOR,
    CONF_COOL_LOCKOUT_CEILING,
    CONF_CHANGEOVER_HEAT_ABOVE,
    CONF_CHANGEOVER_COOL_BELOW,
)
_SAVED_DRIFTS = (CONF_ENGAGE_DEADBAND, CONF_DRIFT_COOLER_BY, CONF_DRIFT_WARMER_BY)
_SAVED_ROOM_DRIFTS = (ZONE_DRIFT_COOLER_BY, ZONE_DRIFT_WARMER_BY)


def _convert_saved_temps(
    hass: HomeAssistant, entry: ConfigEntry, saved_in: str, unit: str
) -> None:
    """Move what the entry saved in the system unit from ``saved_in`` to ``unit``.

    A room's target converts where it restores (number.py), and can only be held
    to a range in the same unit. A floor runs a disabled room's head, and a
    lockout or Away limit decides what a room may do, so none can be read in the
    wrong one. Temperatures land on the target step. Differences, a room's own
    uneven drift among them, only scale, onto the step of the drift box, so Mode
    switch threshold keeps its place against Allowed drift. Their unit goes in the
    same write (``CONF_SAVED_TEMP_UNIT``), so a crash can't convert them twice. A
    floor and a drift are held to the new unit's range. With nothing saved and no
    unit on record there is nothing to record; a unit on record always moves, or
    the next save would convert again.
    """
    celsius = unit == UnitOfTemperature.CELSIUS
    profile = unit_profile(celsius)
    step = profile["target_step"]
    drift_min, drift_max = profile["engage_bounds"]
    low_min, low_max, high_min, high_max = FLOOR_RANGE_C if celsius else FLOOR_RANGE_F
    floors = {ZONE_FLOOR_LOW: (low_min, low_max), ZONE_FLOOR_HIGH: (high_min, high_max)}

    def moved(value: Any) -> Any:
        return target_in_unit(value, saved_in, unit, step) if finite_number(value) else value

    def scaled(value: float) -> float:
        return snap_to_step(offset_in_unit(value, saved_in, celsius), drift_min / 2)

    def field(key: str, value: Any) -> Any:
        if key in _SAVED_TEMPERATURES:
            return moved(value)
        if not finite_number(value):
            return value
        if key == CONF_DEMAND_THRESHOLD:
            return scaled(value)
        if key in _SAVED_DRIFTS:
            return min(max(scaled(value), drift_min), drift_max)
        return value

    def room(zone: Any) -> Any:
        if not isinstance(zone, dict):
            return zone
        out = dict(zone)
        for key, (low, high) in floors.items():
            if finite_number(out.get(key)):
                out[key] = min(max(moved(out[key]), low), high)
        for key in _SAVED_ROOM_DRIFTS:
            if finite_number(out.get(key)):
                out[key] = min(max(scaled(out[key]), drift_min), drift_max)
        return out

    def section(values: Any) -> dict[str, Any]:
        out = {k: field(k, v) for k, v in values.items()}
        if isinstance(out.get(CONF_ZONES), list):
            out[CONF_ZONES] = [room(zone) for zone in out[CONF_ZONES]]
        return out

    data, options = one_home(entry.data, entry.options)
    conf = {**data, **options}
    zones = conf.get(CONF_ZONES)
    if (
        CONF_SAVED_TEMP_UNIT not in conf
        and not any(
            finite_number(conf.get(key))
            for key in (*_SAVED_TEMPERATURES, CONF_DEMAND_THRESHOLD, *_SAVED_DRIFTS)
        )
        and not any(
            isinstance(zone, dict) and finite_number(zone.get(key))
            for zone in (zones if isinstance(zones, list) else ())
            for key in (*floors, *_SAVED_ROOM_DRIFTS)
        )
    ):
        return
    hass.config_entries.async_update_entry(
        entry, data={**section(data), CONF_SAVED_TEMP_UNIT: unit}, options=section(options)
    )


@callback
def async_keep_saved_temps_in_unit(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Before a load: an entry that has been through a switch follows the next one.

    An entry that hasn't has no unit on record. Its first switch is found when a
    room's target restores (``MXZCoordinator.note_saved_unit``).
    """
    saved_in = entry_config(entry).get(CONF_SAVED_TEMP_UNIT)
    unit = str(hass.config.units.temperature_unit)
    if saved_in in (UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT) and saved_in != unit:
        _convert_saved_temps(hass, entry, saved_in, unit)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------
class MXZCoordinator(DataUpdateCoordinator[Plan | None]):
    """Owns the decision state, drives the heads, and self-heals drift."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        record: ModeRecord | None = None,
        openings: dict[str, Any] | None = None,
    ) -> None:
        """Initialise from the config entry (data) and options (tunables).

        ``record`` is the entry's stored mode record (``async_load_mode_record``):
        the wall-clock time of the last real mode change, and the shared mode
        as last written. None is no record. ``openings`` is the entry's stored
        door/window record (``openings.async_load_openings``), by head.
        """
        # Home Assistant's own request debouncer, made here so the delivery
        # queue holds it without reaching into the base class.
        debouncer: Debouncer[Any] = Debouncer(
            hass,
            _LOGGER,
            cooldown=REQUEST_REFRESH_DEFAULT_COOLDOWN,
            immediate=REQUEST_REFRESH_DEFAULT_IMMEDIATE,
        )
        super().__init__(
            hass,
            _LOGGER,
            name="MXZ Plan",
            update_interval=None,  # all updates are event/heartbeat-driven, not polled
            config_entry=entry,
            request_refresh_debouncer=debouncer,
        )
        # The one clock every hold, wait, time limit and rate limit reads, and
        # every wait is armed on (see clock.py).
        self.clock = Clock(hass)
        conf: dict[str, Any] = entry_config(entry)

        # Operate in the HA system temperature unit. The °F profile reproduces
        # every legacy default exactly; a °C system gets clean metric defaults,
        # a 1° setpoint band, metric eco edges, and 0.5° display resolution.
        self.temp_unit: str = hass.config.units.temperature_unit
        self.celsius: bool = self.temp_unit == UnitOfTemperature.CELSIUS
        self._profile: dict[str, Any] = unit_profile(self.celsius)
        _defaults: dict[str, Any] = self._profile["defaults"]
        self.target_default: float = self._profile["target_default"]
        self.target_step: float = self._profile["target_step"]
        self.setpoint_band: float = self._profile["setpoint_band"]
        self.eco_cool: tuple[float, float] = self._profile["eco_cool"]
        self.eco_heat: tuple[float, float] = self._profile["eco_heat"]
        self.fan_up_at: tuple[float, ...] = self._profile["fan_up_at"]
        self.fan_down_at: tuple[float, ...] = self._profile["fan_down_at"]

        # Ordered zone list (index 0 = highest standoff priority). Each zone
        # carries its head/sensor/vane ids plus the entity-owned target/enable.
        self.zones: list[Zone] = _parse_zones(
            conf, self.target_default, self._profile["engage_bounds"]
        )
        # This coordinator's start, on the wall clock: a room sensor's grace
        # (see the freshness below) and a cloud head with no state count from it.
        self._started_ts: float = dt_util.utcnow().timestamp()
        # Each room's head driver, local or cloud (see heads.HeadDrivers). It
        # settles each room's head type now, before anything reads it.
        self.heads = HeadDrivers(self, entry.entry_id, self.zones, self._started_ts)
        self.dual_min_gap: float = self._profile["dual_min_gap"]
        handoffs: dict[tuple[str, str], dict[str, Any]] = hass.data.setdefault(
            DUAL_HANDOFF_KEY, {}
        )
        for zone in self.zones:
            zone.handoff = handoffs.pop((entry.entry_id, zone.climate_id), None)
        # A dual room's restored edges until both have arrived (see
        # offer_range_restore): {slug: {is_low: value or None}}.
        self._range_offers: dict[str, dict[bool, float | None]] = {}
        # The config this coordinator was built from, so a save that only
        # switches rooms to or from a dual setpoint can be told apart from
        # every other change (see async_switch_dual_in_place).
        self._built_conf: dict[str, Any] = copy.deepcopy(conf)
        # Adds a room's heat-to and cool-to numbers after setup (set by the
        # number platform).
        self.add_range_numbers: Callable[[Zone], None] | None = None
        self.notify_service: str | None = conf.get(CONF_NOTIFY_SERVICE) or None

        # Unit-dependent tunables fall back to the system-unit profile default;
        # unit-free ones (hysteresis seconds, resting bias, fan boost) keep their
        # plain DEFAULT_*.
        self._read_saved_tunables(conf)
        self.hysteresis: int = conf.get(CONF_MODE_HYSTERESIS, DEFAULT_MODE_HYSTERESIS)
        # How long a lost head's last-known vote still counts, in seconds.
        # Blank (no key) is the default; a stored value is clamped into the
        # UI's bounds, and one that isn't a finite number reads as blank.
        self.lost_vote_ttl: float = 60.0 * _lost_vote_minutes(conf.get(CONF_LOST_VOTE_TTL))
        # How long an on/off Comfort priority entity's "on" counts, in seconds
        # by its own last_changed; None = no limit (a stored 0). Blank is the
        # default, read like the lost-vote limit above.
        self.comfort_stale_after: float | None = _comfort_stale_seconds(
            conf.get(CONF_COMFORT_STALE_HOURS)
        )
        # How long an action-basis vote still counts after its head stops
        # running, in seconds (see logic.HeadSight.action_vote).
        self.action_vote_hold: float = float(ACTION_VOTE_HOLD)
        self.clamp_min: float = float(conf.get(CONF_CLAMP_MIN, _defaults[CONF_CLAMP_MIN]))
        self.clamp_max: float = float(conf.get(CONF_CLAMP_MAX, _defaults[CONF_CLAMP_MAX]))
        self.resting_mode_bias: str = conf.get(
            CONF_RESTING_MODE_BIAS, DEFAULT_RESTING_MODE_BIAS
        )
        # Optional local-weather seasonal changeover (auto-drives the lockouts).
        self.changeover_entity: str | None = conf.get(CONF_CHANGEOVER_ENTITY) or None
        # Optional external inhibit / low-power standby (grid-down, load-shed):
        # while the watched entity reads `inhibit_active_state` the coordinator
        # parks its heads at `inhibit_action` and self-restores on release.
        # Orthogonal to the enable kill-switch -> no prior state to snapshot.
        self.inhibit_entity: str | None = conf.get(CONF_INHIBIT_ENTITY) or None
        self.inhibit_active_state: str = conf.get(
            CONF_INHIBIT_ACTIVE_STATE, DEFAULT_INHIBIT_ACTIVE_STATE
        )
        self.inhibit_action: str = conf.get(
            CONF_INHIBIT_ACTION, DEFAULT_INHIBIT_ACTION
        )
        self.inhibited: bool = False
        # How a satisfied (or standoff-parked) head idles: fan_only, off, or
        # (default) off after a coil-dry fan run following active cooling. Airflow
        # only — the refrigerant valve position is the same either way.
        self.idle_action: str = conf.get(CONF_IDLE_ACTION, DEFAULT_IDLE_ACTION)
        self.coil_dry_seconds: float = 60.0 * float(
            conf.get(CONF_COIL_DRY_MINUTES, DEFAULT_COIL_DRY_MINUTES)
        )
        # Per-head memory of the last ACTIVE mode we commanded (mode, ts),
        # re-stamped every apply cycle while running — so the timestamp reads
        # as "when conditioning stopped". Drives the off_after_dry dwell.
        # Monotonic, like the mode-change dwell: a wall-clock step must not
        # cut the coil-dry wait short or stretch it. In memory only.
        self._last_active: dict[str, tuple[str, float]] = {}
        # Delta-proportional fan boost (overrides the firmware's weak "auto").
        # Adopt a setpoint or on/off set at a head instead of writing over it.
        self.accept_head_changes: bool = bool(
            conf.get(CONF_ACCEPT_HEAD_CHANGES, DEFAULT_ACCEPT_HEAD_CHANGES)
        )
        self.fan_boost_enable: bool = bool(
            conf.get(CONF_FAN_BOOST_ENABLE, DEFAULT_FAN_BOOST_ENABLE)
        )
        self.fan_boost_max: str = conf.get(CONF_FAN_BOOST_MAX, DEFAULT_FAN_BOOST_MAX)
        # Fan boost and the manual fan hold, per head (fan_hold.py).
        self.fan = FanHold(self)

        # Engage latch (decision state, like the fan's rung): "" = coasting, cool|heat
        # = mid-run toward target (the head may still be parked in fan_only by a
        # shared-mode mismatch; the run resumes when the mode returns). Seeded
        # from the head only with restored zone AND coordinator ON intent.
        # Construction and first live enable have no prior run to adopt.
        self._engage_latch: dict[str, str] = {zone.slug: "" for zone in self.zones}
        self.restored_coordinator_on = False
        # None = head evidence unknown; "" = known idle; cool/heat = observed
        # run awaiting a usable sensor. Keep that observation across our own
        # sensor-failure park, without changing the ordinary dropout policy.
        self._startup_resume: dict[str, str | None] = {}
        # Hands-off rooms (opted in, disabled) whose off has LANDED: the head
        # was seen off with none of our other commands still in flight. From
        # then on the coordinator leaves that head alone — nothing tells a
        # person's later change from an off that never arrived, so the line is
        # drawn at the first off seen. A disabled hands-off room NOT in here is
        # still parking, and its off is re-sent every cycle, a rejected one
        # included, exactly as an opted-out room's is. Cleared when the room is
        # enabled again; restored across a restart by its enable switch.
        self._hands_off_released: set[str] = set()
        # The shared mode was last moved by hands-off votes alone. Going back
        # from it while no coordinated room calls is undoing the vote, not a
        # flip of ours, so that starts no dwell either (see _compute).
        self._mode_by_votes = False
        # Freeze floor / overheat ceiling, for a disabled room that sets one
        # (see floor.FloorGuard). Built before the seed compute below, which
        # it only shows: it decides from async_setup on. Its clear line's
        # margin is the unit's default demand threshold.
        self.floor = FloorGuard(
            self, entry.entry_id, self.zones, float(_defaults[CONF_DEMAND_THRESHOLD])
        )
        # Whether a plan has been computed since async_setup, from restored
        # inputs. The seed is not one: its shared mode is only the default.
        self.plan_ready = False

        # Vane-kick bookkeeping: heads mid-kick are skipped by _apply so the
        # plan doesn't turn them back off while the louvre is still traveling.
        # One entry per head with a kick in flight, holding that kick's task —
        # the single owner record. A kick owns its head while (and only while)
        # this maps the head to its own task.
        self._vane_kicks: dict[str, asyncio.Task[None]] = {}
        # Heads this coordinator ran up to fan_only for a kick and has not
        # parked again yet. Retirement finishes that job (see _restore_woken).
        self._vane_kick_woken: set[str] = set()
        self._vane_pending: dict[str, tuple[str, str]] = {}
        self._vane_kick_spinup: float = VANE_KICK_SPINUP
        self._vane_kick_apply: float = VANE_KICK_APPLY
        self._vane_kick_retire: float = VANE_KICK_RETIRE_TIMEOUT
        # Set once this incarnation is unloaded: it must never write again.
        self.retired: bool = False
        # Per head: what MXZ last saw of it and since when it has been out of
        # sight (see logic.HeadSight). On the monotonic clock, like the dwell:
        # a wall-clock step must not shorten or stretch the time limit (the
        # wall-clock copy is display only). In memory only: after a restart or
        # a reload a head is unknown until it is seen, timed from that start.
        # One wait for the nearest expiry (see _sync_lost_timer), and one
        # for the nearest end of a settle window (see _sync_settle_timer).
        started = self.clock()
        started_wall = dt_util.utcnow().timestamp()
        self._sight: dict[str, HeadSight] = {
            zone.climate_id: HeadSight(started, started_wall, ACTION_VOTE_SETTLE)
            for zone in self.zones
        }

        # Helper values (owned by the switch/select entities; seeded on
        # restore, mutated on user action). Kill-switch defaults OFF for safety.
        # Per-zone target/enable live on the Zone objects.
        self.coordinator_enable: bool = False
        self.eco_idle: bool = False
        self.heat_lockout: bool = False
        self.cool_lockout: bool = False
        self.current_shared_mode: str = MODE_COOL  # restored by the select entity

        # When the last real mode change was, on the monotonic clock: an NTP
        # step correction must not be able to transiently defeat or prematurely
        # satisfy the dwell gate. A reload or restart does not restart the
        # dwell: it is timed from the stored wall-clock time of that change,
        # converted once here (a future time counts as now, see
        # mode_change_stamp). None is no change on record — a fresh install —
        # so the first decision has nothing to wait out and the plan reports
        # no time since a change (never an invented one; #6 was a ~56,000-year
        # dwell read off a 0.0 stand-in).
        record = record or ModeRecord()
        self._last_mode_change_ts: float | None = mode_change_stamp(
            record.changed_at, self.clock()
        )
        # The same change on the wall clock, written back with every record.
        self._last_mode_change_at: float | None = record.changed_at
        self._mode_change_store = _mode_change_store(hass, entry.entry_id)
        # The shared mode resumes from the record (written at each change),
        # unless the select's restore data is newer (restore_shared_mode). So
        # does whether votes alone set it: a room's run in that mode then
        # starts the hold, as it would have without the reload or restart.
        self._mode_record = record
        if record.mode is not None:
            self.current_shared_mode = record.mode
            self._mode_by_votes = record.by_votes
        # Whether the last compute let go of an engage latch, so a room that
        # coasts through a direction flip is re-evaluated on a head that echoes
        # nothing back (see _sync_coast_timer).
        self._coasted = False
        # Allow Dry mode, per room (see dry.DryMode).
        self.dry = DryMode(self, self.zones)
        # Door/window pause, per room (see openings.OpeningPause).
        self.openings = OpeningPause(self, entry.entry_id, self.zones, openings, self._started_ts)
        # Per-sensor freshness. NOTHING about sensor health survives a reload:
        # a new coordinator re-derives each room's validity, last write time
        # and sample marker from HA state, and a write it did not witness is
        # not evidence of a current report (an existing value can be a restored
        # or cached one). So a configured room opens in `awaiting_report` with
        # exactly one bounded, visibly provisional grace period — the
        # availability tradeoff the policy makes deliberately, not knowledge of
        # pre-reload health — unless its sensor already carries a sample TIME
        # inside its maximum age, which dates itself and starts the room
        # healthy on that time. A room with no usable reading is invalid at
        # once, with no grace, as at any other time (the notice after the
        # first compute below reports what each room actually started as).
        self._freshness: dict[str, tuple[float, float]] = {}  # slug -> (max age, grace) s
        # slug -> (basis, marker attribute, marker is a time); only the
        # enforcement-capable sensors are in here, and only they have a cutoff.
        self._evidence: dict[str, tuple[str, str | None, bool]] = {}
        # The per-sensor evidence RECORD of a sample-marker source, written
        # only at an observation boundary (_observe_report) and read by every
        # compute: the last ACCEPTED marker, the time of the evidence it
        # proved, and whether an unhealthy evaluation has since spent it.
        self._sample_marker: dict[str, float] = {}
        self._evidence_ts: dict[str, float] = {}
        self._evidence_spent: set[str] = set()
        self._health: dict[str, str] = {}
        # slug -> the window (s) a stale room's last report ran out: its
        # maximum age, or its startup grace when no report was witnessed.
        self._stale_window: dict[str, float] = {}
        self._grace_until: dict[str, float] = {}
        self._fresh_deadline: dict[str, float] = {}
        self._unhealthy_logged: set[str] = set()
        # Set up before HA is running, a room sensor can be missing, unavailable
        # or unknown only because its integration is still loading. The room is
        # out of demand at once, as always; only its "no usable reading"
        # WARNING waits for the post-start recompute (_on_startup_timer), which
        # logs it then if it still applies. A reload while running warns at once.
        self._startup_quiet = hass.state is not CoreState.running
        self._quiet_unusable: set[str] = set()
        self._arm_freshness(self._started_ts)
        self._unsubs: list[Any] = []
        # When each head's last heal notice went out (monotonic), for the hourly limit.
        self._heal_notice_at: dict[str, float] = {}
        # State callbacks may run inside a service call or one loop turn later.
        # Keep the current decision, while each delivery slot separately owns
        # its head's applicable action until those pending reports are dispatched.
        self._applying_plan: Plan | None = None
        self._delivery_listeners: set[Callable[[], None]] = set()
        self.deliveries = DeliveryQueue(self, debouncer, super().async_request_refresh)
        # Lives with HA like the above, so a reload keeps what the head may still
        # report. Held per unit: a °F/°C switch's reload must not compare a
        # remembered 70 °F with a report in °C.
        self._head_writes: dict[str, dict[str, HeadWriteMemory]] = hass.data.setdefault(
            f"{DOMAIN}_head_writes", {}
        ).setdefault(self.temp_unit, {})
        # What moved at each head since its adopt timer was armed: "mode", "setpoint".
        self._adopt_moved: dict[str, set[str]] = {}
        # Seed data so entities have something to read before the first refresh.
        # Only a cloud head already out of MXZ's control has an effect here.
        self.data, effects = self._compute()
        for effect in effects:
            effect()
        if self._freshness:
            # That first compute classified every room from what HA holds
            # now; the notice reports its result rather than estimating it. A
            # room with a valid reading and a sample TIME inside its maximum
            # age starts healthy (the sample dates itself); every other room
            # with a valid reading is provisional, because a sequence found
            # at startup is a baseline only and a contracted HA write needs a
            # write made after this start; a room with no usable reading is
            # invalid at once and gets no grace.
            starts = [self._health[slug] for slug in self._freshness]
            _LOGGER.info(
                "MXZ: sensor health is not retained across a reload; %d room(s) "
                "are provisionally eligible until their first witnessed report "
                "or the end of their startup grace, %d room(s) start healthy "
                "on a sample time already in Home Assistant that is inside their "
                "maximum age, and %d room(s) have no usable reading and are "
                "rejected at once, with no grace",
                starts.count(HEALTH_AWAITING),
                starts.count(HEALTH_HEALTHY),
                starts.count(HEALTH_INVALID),
            )

    def _arm_freshness(self, started: float) -> None:
        """Resolve each sensor's evidence contract and window; open its grace.

        A room is enforcement-capable only when BOTH halves of the approved
        configuration are present: a trusted evidence basis, and a maximum age
        (its own or the one its cadence derives). Either half alone is an
        unknown cadence — age shown, nothing enforced — because a duration
        describes how often a source promises to write, and a basis is what
        says a write is a report at all. An invalid profile — a duration that
        is not a finite positive number, or a maximum age shorter than the
        interval — is reported once and is no profile at all: nothing in it is
        rounded, raised or read past into a cutoff nobody asked for.
        """
        for zone in self.zones:
            window = freshness_window(
                report_interval=zone.report_interval,
                max_age=zone.max_age,
                startup_grace=zone.startup_grace,
            )
            configured = (zone.report_interval, zone.max_age, zone.startup_grace)
            if window is None:
                if any(value is not None for value in configured):
                    _LOGGER.warning(
                        "MXZ: %s sensor %s has no usable reporting interval or "
                        "maximum age (interval, maximum age, grace = %s): that "
                        "is not a freshness profile, so nothing of it is "
                        "enforced; the sensor's age is shown but never enforced",
                        zone.name,
                        zone.sensor_id,
                        configured,
                    )
                continue
            contract = evidence_contract(
                basis=zone.evidence_basis,
                sample_timestamp_attribute=zone.sample_timestamp_attribute,
                sample_sequence_attribute=zone.sample_sequence_attribute,
            )
            if contract is None:
                _LOGGER.warning(
                    "MXZ: %s sensor %s has a maximum age but no trusted evidence "
                    "basis (%s); its age is shown but never enforced. A source "
                    "gets a cutoff only when its own contract says every write "
                    "is a current reading, or it publishes an advancing sample "
                    "marker",
                    zone.name,
                    zone.sensor_id,
                    zone.evidence_basis,
                )
                continue
            self._evidence[zone.slug] = contract
            max_age, grace = (window[0] * 60.0, window[1] * 60.0)
            self._freshness[zone.slug] = (max_age, grace)
            self._grace_until[zone.slug] = started + grace
            # The state found at startup is the first observation — with no
            # receipt, because this incarnation did not witness its write.
            self._observe_report(
                zone, self.hass.states.get(zone.sensor_id), started, None
            )

    # -- lifecycle ----------------------------------------------------------
    async def async_setup(self) -> None:
        """Wire up listeners and run the first compute/apply."""
        self.floor.start()  # the switches have restored by now
        self.openings.start()
        for zone in self.zones:
            if zone.dual and zone.target_low is None:
                # An edge that never restored (its number disabled in the
                # registry, say): settle the pair now, seeded, so the room
                # never advertises a range it doesn't have.
                self._settle_range(zone)
            if zone.handoff is not None:
                # Just switched to or from a dual setpoint: a fresh decision,
                # exactly as a target write makes one (the switches have
                # restored by now, so this sees the room's real state).
                self.reset_engage_latch(zone.slug)
                zone.handoff = None
        self._unsubs.append(
            async_track_state_change_event(
                self.hass,
                [z.sensor_id for z in self.zones],
                self._on_input_change,
            )
        )
        if self._freshness:
            # Only where a cutoff can actually apply, and only for OUR sensors:
            # an unchanged write fires no state_change, so a room that reports
            # the same temperature forever would otherwise look silent. Ignored
            # entirely while every configured room's cadence is unknown, so no
            # install pays for this stream without a use for it.
            self._unsubs.append(
                async_track_state_report_event(
                    self.hass,
                    [z.sensor_id for z in self.zones],
                    self._on_input_report,
                )
            )
        self._unsubs.append(
            async_track_state_change_event(
                self.hass,
                [z.climate_id for z in self.zones],
                self._on_head_change,
            )
        )
        if (unsub := self.heads.track_reports()) is not None:
            self._unsubs.append(unsub)
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._on_heartbeat, timedelta(minutes=15)
            )
        )
        # Keep the mxz_recompute event so the echavet proxy companion can poke us.
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_RECOMPUTE, self._on_recompute_event)
        )
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, self._on_core_config_update)
        )
        # Optional local-weather seasonal changeover: re-read on the weather entity's
        # own updates + hourly, and drive the lockout switches from it.
        if self.changeover_entity:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [self.changeover_entity], self._on_changeover_change
                )
            )
            self._unsubs.append(
                async_track_time_interval(
                    self.hass,
                    self._on_changeover_timer,
                    timedelta(minutes=CHANGEOVER_INTERVAL_MINUTES),
                )
            )
            self.hass.async_create_task(self._evaluate_changeover())
        # Optional external inhibit / low-power standby: re-read on the watched
        # entity's own updates and evaluate the initial state.
        if self.inhibit_entity:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [self.inhibit_entity], self._on_inhibit_change
                )
            )
            self.hass.async_create_task(self._evaluate_inhibit())
        # React to a safety-priority sensor immediately rather than waiting
        # for the next unrelated temp report/heartbeat.
        safety_entities = [
            zone.safety_priority_entity for zone in self.zones if zone.safety_priority_entity
        ]
        if safety_entities:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, safety_entities, self._on_safety_change
                )
            )
        # And to a Comfort priority entity, the same way.
        comfort_entities = [
            zone.comfort_priority_entity for zone in self.zones if zone.comfort_priority_entity
        ]
        if comfort_entities:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, comfort_entities, self._on_comfort_change
                )
            )
        # A door/window reading pauses or resumes its room: react at once,
        # then the delay wakeups (OpeningPause.sync_timers) take it from there.
        openings: list[Any] = [zone.opening_entity for zone in self.zones if zone.opening_active]
        if openings:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, openings, self.openings.on_change
                )
            )
        self._unsubs.append(async_at_start(self.hass, self._on_ha_start))
        self._arm_hold_end_timer()  # a hold carried over from the last run
        await self.async_refresh()

    def _read_saved_tunables(self, conf: dict[str, Any]) -> None:
        """Read the tunables a °F/°C switch converts, from a load or a first switch."""
        _defaults: dict[str, Any] = self._profile["defaults"]
        self.demand_threshold: float = conf.get(
            CONF_DEMAND_THRESHOLD, _defaults[CONF_DEMAND_THRESHOLD]
        )
        # Re-engage drift: clamped to the unit profile's sane range so a
        # hand-edited or legacy value can't collapse the coast window or park
        # rooms degrees off target. The UI enforces the same bounds. One that
        # isn't a finite number is the unit's default: setup must not fail on
        # it, and the Configure menu names it so it can be fixed.
        _emin, _emax = self._profile["engage_bounds"]
        _allowed = _stored_number(conf.get(CONF_ENGAGE_DEADBAND), '"Allowed drift"')
        if _allowed is None:
            _allowed = float(_defaults[CONF_ENGAGE_DEADBAND])
        self.engage_deadband: float = min(max(_allowed, _emin), _emax)
        # Uneven drift: None (no key) means Allowed drift on that side. A
        # stored value is clamped into the same bounds, for the same reason,
        # and one that isn't a finite number reads as blank.
        self.drift_cooler_by: float | None = _drift_side(
            conf.get(CONF_DRIFT_COOLER_BY), _emin, _emax, '"Let it get cooler by"'
        )
        self.drift_warmer_by: float | None = _drift_side(
            conf.get(CONF_DRIFT_WARMER_BY), _emin, _emax, '"Let it get warmer by"'
        )
        self.eco_cool_max: float = conf.get(
            CONF_ECO_COOL_MAX, _defaults[CONF_ECO_COOL_MAX]
        )
        self.eco_heat_min: float = conf.get(
            CONF_ECO_HEAT_MIN, _defaults[CONF_ECO_HEAT_MIN]
        )
        self.heat_lockout_floor: float = conf.get(
            CONF_HEAT_LOCKOUT_FLOOR, _defaults[CONF_HEAT_LOCKOUT_FLOOR]
        )
        self.cool_lockout_ceiling: float = conf.get(
            CONF_COOL_LOCKOUT_CEILING, _defaults[CONF_COOL_LOCKOUT_CEILING]
        )
        self.changeover_heat_above: float = conf.get(
            CONF_CHANGEOVER_HEAT_ABOVE, _defaults[CONF_CHANGEOVER_HEAT_ABOVE]
        )
        self.changeover_cool_below: float = conf.get(
            CONF_CHANGEOVER_COOL_BELOW, _defaults[CONF_CHANGEOVER_COOL_BELOW]
        )

    def note_saved_unit(self, saved_in: str | None) -> None:
        """A room's value restored from the other unit: this is the entry's first switch.

        Called before that value is held to its bounds, so they convert first.
        Once the entry has a unit on record, ``async_keep_saved_temps_in_unit``
        has already dealt with this load, and a restore data dump older than the
        switch (a crash) can't convert anything a second time.
        """
        entry = self.config_entry
        if (
            saved_in not in (UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT)
            or saved_in == self.temp_unit
            or CONF_SAVED_TEMP_UNIT in entry_config(entry)
        ):
            return
        _convert_saved_temps(self.hass, entry, saved_in, self.temp_unit)
        conf = entry_config(entry)
        defaults = self._profile["defaults"]
        self.clamp_min = float(conf.get(CONF_CLAMP_MIN, defaults[CONF_CLAMP_MIN]))
        self.clamp_max = float(conf.get(CONF_CLAMP_MAX, defaults[CONF_CLAMP_MAX]))
        self._read_saved_tunables(conf)
        drift_min, drift_max = self._profile["engage_bounds"]
        for zone, room in zip(self.zones, conf.get(CONF_ZONES) or (), strict=False):
            if isinstance(room, dict):
                zone.floor_low = _finite(room.get(ZONE_FLOOR_LOW))
                zone.floor_high = _finite(room.get(ZONE_FLOOR_HIGH))
                zone.drift_cooler_by = _drift_side(
                    room.get(ZONE_DRIFT_COOLER_BY),
                    drift_min,
                    drift_max,
                    f'"Let it get cooler by" for {zone.name}',
                )
                zone.drift_warmer_by = _drift_side(
                    room.get(ZONE_DRIFT_WARMER_BY),
                    drift_min,
                    drift_max,
                    f'"Let it get warmer by" for {zone.name}',
                )
        self._built_conf = copy.deepcopy(conf)

    @callback
    def _on_core_config_update(self, _event: Event[Any]) -> None:
        """Home Assistant's temperature unit changed: reload this entry, once.

        Everything here works in the unit it loaded with, and Home Assistant now
        reads what MXZ sends in the new one, so this incarnation writes nothing
        more. The load that follows converts what was saved in the old unit.
        Any other change to Home Assistant's core config is ignored.
        """
        if self.retired or self.hass.config.units.temperature_unit == self.temp_unit:
            return
        _LOGGER.info(
            "Home Assistant's temperature unit changed from %s to %s: reloading MXZ",
            self.temp_unit, self.hass.config.units.temperature_unit,
        )
        self.retired = True
        self.deliveries.retire()
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    async def async_shutdown_listeners(self) -> None:
        """Retire this context, then cancel its listeners, timers, and kick tasks.

        ``retired`` first, so nothing this incarnation already scheduled can
        write a head again. Every wait on the clock is cancelled here, and the
        clock arms none again: a refresh still in flight re-syncs its waits on
        the way out, and a callback the loop already took does nothing. Retiring
        kicks is last: the existing own-wake guard parks a head still reporting
        fan_only (see _restore_woken_heads).
        """
        self.retired = True
        self.deliveries.retire()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.clock.retire()
        self.deliveries.drop_followup()
        self.heads.retire()
        await self._async_retire_vane_kicks()

    # -- decision (mirrors sensor.mxz_plan) ---------------------------------
    def _arbitration_mode(self) -> str:
        """The shared mode arbitration starts from (cold start / junk -> cool)."""
        return (
            self.current_shared_mode
            if self.current_shared_mode in (MODE_COOL, MODE_HEAT)
            else MODE_COOL
        )

    def _resting_bias(self) -> str | None:
        """The mode to settle at when no room is calling, or None for "last".

        "last" (or anything not cool|heat) -> None keeps the last-mode behavior.
        """
        return (
            self.resting_mode_bias
            if self.resting_mode_bias in (MODE_COOL, MODE_HEAT)
            else None
        )

    def _compute(self) -> tuple[Plan, list[Effect]]:
        """Recompute the plan from current inputs. Commands nothing.

        It advances the per-zone engage latch and any pending startup seed.
        Missing startup evidence is retained separately from a known idle run.
        What it decides beyond the plan (door/window and floor notices, the
        Dry a resume puts back, the door/window store write, a cloud head's
        repair issue) comes back as effects, in the order decided, for the
        caller to make.
        """
        effects: list[Effect] = []
        common: dict[str, Any] = {
            "eco": self.eco_active(),
            "eco_cool_max": self.eco_cool_max,
            "eco_heat_min": self.eco_heat_min,
            "heat_lockout": self.heat_lockout,
            "heat_lockout_floor": self.heat_lockout_floor,
            "cool_lockout": self.cool_lockout,
            "cool_lockout_ceiling": self.cool_lockout_ceiling,
        }
        now = dt_util.utcnow().timestamp()
        # The lost-vote clock (see logic.HeadSight); `now` above is wall-clock.
        clock = self.clock()
        demands: list[str] = []
        engages: list[str] = []
        # Each room's demand and engage as arbitration counts them: its own,
        # unless its head's vote has expired (see _holds).
        held_demands: list[str] = []
        held_engages: list[str] = []
        statuses: list[str] = []
        healths: list[str] = []
        ages: list[int | None] = []
        shown: list[float | None] = []
        votes: list[str | None] = []
        arb_demands: list[Demand] = []
        floors: list[str | None] = []
        drys: list[bool] = []
        dry_votes: list[str | None] = []
        sides: list[tuple[float, float]] = []
        starts: list[dict[str, Any]] = []
        comforts: list[tuple[float, str]] = []
        all_ok = True
        coasted = False
        for i, zone in enumerate(self.zones):
            sight = self._watch_head(zone.climate_id, clock, now)
            status = sight.status(clock, self.lost_vote_ttl)
            # A cloud head out of MXZ's control (never a local one).
            cloud_out = self.heads.watch(zone, now, effects)
            state: Any = self.hass.states.get(zone.sensor_id)
            valid, temp = _read_temp(state, self.target_default, self.temp_unit)
            health, age = self._sensor_health(zone, state, valid, now)
            # A stale room votes exactly like a room with no reading at all:
            # no demand, no engagement, its head parked by the ordinary idle
            # path. The reading itself is still valid, so it is still SHOWN —
            # what the room loses is authority, not its last measurement.
            ok = valid and health != HEALTH_STALE
            all_ok = all_ok and ok
            (heat_drift, heat_from), (cool_drift, cool_from) = (
                self.zone_drift_sides_from(zone)
            )
            sides.append((heat_drift, cool_drift))
            # A room's demand vote respects its OWN drift (#18): a room
            # told to tolerate a wide band must not steer the shared
            # compressor inside it. max() reproduces today's behavior
            # exactly whenever drift <= demand_threshold (the default).
            # Per side, so a wide "warmer by" also holds back cool demand.
            heat_demand_band = max(self.demand_threshold, heat_drift)
            cool_demand_band = max(self.demand_threshold, cool_drift)
            # Each side measures from its own target: Heat to and Cool to in
            # a dual-setpoint room, the one target (twice) otherwise.
            heat_target, cool_target = self.zone_side_targets(zone)
            demand = room_call(
                temp=temp, target=zone.target, enabled=zone.enable,
                heat_target=heat_target, cool_target=cool_target,
                sensor_ok=ok,
                heat_band=heat_demand_band,
                cool_band=cool_demand_band,
                neutral=DEMAND_NEUTRAL,
                **common,
            )
            # Shown, not decided: the edges room_call just compared against.
            starts.append(
                _start_points(
                    zone, common,
                    sides=(heat_drift, cool_drift),
                    demand_bands=(heat_demand_band, cool_demand_band),
                    sources=(heat_from, cool_from),
                    targets=(heat_target, cool_target),
                )
            )
            if zone.slug in self._startup_resume:
                if not (
                    zone.enable and self.coordinator_enable and self.restored_coordinator_on
                ):
                    self._startup_resume.pop(zone.slug)
                else:
                    head = self.hass.states.get(zone.climate_id)
                    if (
                        self._startup_resume[zone.slug] is None
                        and head is not None and head.state not in UNAVAILABLE_STATES
                    ):
                        self._startup_resume[zone.slug] = (
                            head.state if head.state in (MODE_COOL, MODE_HEAT) else ""
                        )
                    seed = self._startup_resume[zone.slug]
                    if (
                        ok and seed is not None and head is not None
                        and head.state not in UNAVAILABLE_STATES
                    ):
                        self._engage_latch[zone.slug] = seed
                        self._startup_resume.pop(zone.slug)
            elif zone.slug not in self._engage_latch and zone.enable:
                # An enabled room's target changed: preserve the existing
                # live retargeting behavior, separately from restart intent.
                # With changes at the head accepted, a head a person flipped off
                # the shared mode is no run of MXZ's: its heat/cool isn't adopted.
                head = self.hass.states.get(zone.climate_id)
                self._engage_latch[zone.slug] = (
                    head.state
                    if head is not None and head.state in (MODE_COOL, MODE_HEAT)
                    and (not self.accept_head_changes or head.state == self.current_shared_mode)
                    else ""
                )
            prior_engage = self._engage_latch.get(zone.slug, "")
            engage = engage_with_latch(
                prior=prior_engage or None,
                temp=temp, target=zone.target, enabled=zone.enable,
                heat_target=heat_target, cool_target=cool_target,
                sensor_ok=ok, heat_band=heat_drift, cool_band=cool_drift,
                neutral=ENGAGE_SATISFIED,
                **common,
            )
            if zone.slug in self._engage_latch:
                self._engage_latch[zone.slug] = (
                    engage if engage in (MODE_COOL, MODE_HEAT) else ""
                )
            # Door/window pause: a paused room has no call and no run. It
            # never votes, so it can neither keep nor turn the shared mode,
            # and it doesn't hold Resting mode off; its run starts afresh on
            # resume. (Evaluated before Dry: a resume can put a Dry back.)
            paused = self.openings.step(zone, now, effects)
            if paused:
                demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED
                if zone.slug in self._engage_latch:
                    self._engage_latch[zone.slug] = ""
                prior_engage = ""
            dry = not paused and self.dry.in_dry(zone)
            if dry:
                # Allow Dry mode: a person runs this head in dry, so the
                # room's temperature goal pauses (no call, no run). Its vote
                # is the dry below. Out of dry it starts afresh.
                demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED
                if zone.slug in self._engage_latch:
                    self._engage_latch[zone.slug] = ""
                prior_engage = ""
            # This room let go of a run and is now coasting: it is parked at the
            # idle action, and only a later compute may engage it again (see
            # _sync_coast_timer). A disabled room (MODE_OFF) is not coasting.
            if prior_engage and engage == ENGAGE_SATISFIED:
                coasted = True
            # A tripped floor is this disabled room's engage (the latch stays
            # untouched: it belongs to the room's own enabled runs).
            floor = self.floor.want(zone, temp, ok, now, effects)
            if floor is not None:
                engage = floor
            safety = self._zone_safety(zone)
            vote = self._hands_off_vote(zone, clock, safety=safety)
            # A dry head runs the outdoor unit the cooling way: a cool vote at
            # hands-off rank, whatever the room's Safety priority sensor says,
            # and none at all under a cool lockout.
            dry_vote = MODE_COOL if dry and not self.cool_lockout else None
            holds = self._holds(status, safety, cloud_out)
            held_demand = demand if holds else DEMAND_NEUTRAL
            held_engage = engage if holds or floor is not None else ENGAGE_SATISFIED
            comfort = self._zone_comfort(zone, now)
            comforts.append(comfort)
            arb_demands.extend(
                self._zone_arbitration_pair(
                    demand=held_demand, vote=vote, safety=safety, index=i,
                    floor=floor, dry=dry_vote, boost=comfort[0],
                )
            )
            drys.append(dry)
            dry_votes.append(dry_vote)
            floors.append(floor)
            demands.append(demand)
            engages.append(engage)
            held_demands.append(held_demand)
            held_engages.append(held_engage)
            statuses.append(status)
            votes.append(vote)
            healths.append(health)
            ages.append(None if age is None else int(age))
            # Display only: a rejected reading publishes NO temperature. The
            # fallback stays inside the arithmetic above (and in _apply's
            # delta) instead of being shown as the room's measured value. A
            # STALE reading is a real measurement and keeps being shown — with
            # its health and its age beside it. No value is ever substituted.
            shown.append(temp if valid else None)

        self._coasted = coasted
        current = self._arbitration_mode()
        # Monotonic delta, not derived from `now` above (wall-clock, used for
        # display/freshness elsewhere) — an NTP step must not perturb dwell.
        elapsed = self._mode_change_elapsed()
        allowed = elapsed is None or elapsed >= self.hysteresis
        engaged = any(e in (MODE_COOL, MODE_HEAT) for e in held_engages)
        # A room still running to its target in the shared mode keeps it from
        # Resting mode until it gets there (logic.arbitrate).
        running = current in held_engages
        grant = arbitrate(
            arb_demands,
            engaged=engaged,
            current=current,
            allowed=allowed,
            resting=self._resting_bias(),
            running=running,
        )
        # Comfort priority earned the answer only if the same compute with
        # every boost at 0 answers differently. Run only when one counts.
        by_comfort = any(comfort_boost(d) > 0 for d in arb_demands) and arbitrate(
            [replace(d, boost=0.0) for d in arb_demands],
            engaged=engaged,
            current=current,
            allowed=allowed,
            resting=self._resting_bias(),
            running=running,
        ).direction != grant.direction
        state, by_votes = grant.direction, grant.read_only
        if (
            not by_votes and self._mode_by_votes and state != current
            and not any(c in (MODE_COOL, MODE_HEAT) for c in (*demands, *engages))
        ):
            by_votes = True
        self.floor.settle(floors, state)
        self.dry.settle(drys, state, held_demands, held_engages, votes)
        calls = [*held_demands, *votes, *dry_votes, *floors]
        rooms = [
            RoomPlan(
                slug=zone.slug,
                name=zone.name,
                demand=demands[i],
                engage=engages[i],
                temp=shown[i],
                target=zone.target,
                # A dual-setpoint room's Heat to and Cool to (its target
                # above is their midpoint).
                target_range=(zone.target_low, zone.target_high) if zone.dual else None,
                enabled=zone.enable,
                drift=self.zone_drift(zone),
                # The drift in use below / above target (the same as drift
                # unless uneven drift is set).
                drift_heat=sides[i][0],
                drift_cool=sides[i][1],
                start_points=starts[i],
                fan_hold=not self.fan.auto_is_on(zone.climate_id),
                sensor_health=healths[i],
                sensor_age=ages[i],
                control_reasons=self._control_reasons(zone),
                hands_off=self.hands_off_phase(zone),
                hands_off_vote=votes[i],
                # How far this room's head vote can be trusted now, and since
                # when MXZ has not seen the head (None while it is in sight).
                vote_status=statuses[i],
                lost_since=self._lost_since(zone.climate_id),
                # A room that votes by action: what its vote follows now, and
                # when a stopped head's held vote drops.
                vote_basis=self._vote_basis_row(zone, clock, now),
                safety_priority=self._zone_safety(zone),
                # The Comfort priority entity, the boost this compute read
                # from it (0 = none), and why (see logic.comfort_priority).
                # The dwell wakeup replays it.
                comfort_priority_entity=zone.comfort_priority_entity,
                comfort_priority=comforts[i][0],
                comfort_priority_status=comforts[i][1],
                floor_low=zone.floor_low,
                floor_high=zone.floor_high,
                floor=self.floor.attributes(zone),
                floor_want=floors[i],
                delivery=self.deliveries.attributes(zone.climate_id),
                head=self.heads.attributes(zone),
                dry=self.dry.attributes(zone, dry_votes[i]),
                opening=self.openings.attributes(zone),
            )
            for i, zone in enumerate(self.zones)
        ]
        return Plan(
            rooms=rooms,
            state=state,
            # Both directions physically asked of the one outdoor unit, a
            # hands-off head's and a floor's included (a room whose head's
            # vote expired asks nothing any more).
            standoff=MODE_COOL in calls and MODE_HEAT in calls,
            mode_by_hands_off=by_votes,
            mode_by_safety=grant.by_safety,
            mode_by_comfort_priority=by_comfort,
            mode_by_floor=state in floors,
            sensors_ok=all_ok,
            seconds_since_mode_change=None if elapsed is None else int(elapsed),
            mode_change_allowed=allowed,
            inhibited=self.inhibited,
            idle_action=self.idle_action,
            # Only an entry with a door/window pause has the list, so every
            # other entry's plan is exactly what it was before the feature.
            paused_rooms=(
                [zone.name for zone in self.zones if self.openings.paused(zone)]
                if self.openings.any_room else None
            ),
        ), effects

    def _control_reasons(self, zone: Zone) -> list[dict[str, str]]:
        """Room detail on the existing plan, including unavailable Fan-auto.

        These describe lookup, intent and software delivery, never hardware receipt.
        The plan itself can be unavailable after a global update failure.
        """
        reasons = []

        def add(reason: str, next_step: str) -> None:
            reasons.append({"reason": reason, "next_step": next_step})

        head = self.hass.states.get(zone.climate_id)
        if head is None or head.state in UNAVAILABLE_STATES:
            add(f"Head {zone.climate_id} unavailable.",
                "Restore the configured head's connection and wait for its report.")
        else:
            features = head.attributes.get("supported_features")
            modes = head.attributes.get("fan_modes")
            if not isinstance(features, int) or isinstance(features, bool):
                add("Fan capabilities unavailable or malformed.",
                    "Wait for valid capability metadata from the head integration.")
            elif not features & ClimateEntityFeature.FAN_MODE:
                add("Fan control is not advertised by this head.",
                    "Use the head's own controls; Fan auto requires advertised auto support.")
            elif not isinstance(modes, (list, tuple)) or not any(
                isinstance(mode, str) for mode in modes
            ):
                add("Fan capabilities unavailable or malformed.",
                    "Wait for valid fan options from the head integration.")
            elif any(not isinstance(mode, str) for mode in modes):
                supported = head_has_fan_auto(self.hass, zone.climate_id)
                add("Malformed fan options ignored; " + (
                    "advertised auto remains available." if supported
                    else "no supported auto option is advertised."
                ), "Review capability metadata in the head integration.")
            elif not head_has_fan_auto(self.hass, zone.climate_id):
                add("Fan auto is not supported by this head.",
                    "Use the head's own fan controls; no auto option is invented.")
            elif head.attributes.get("fan_mode") is None:
                add("Current fan speed unavailable; fan intent is retained.",
                    "Wait for a usable head report; no blind fan command is sent.")
        sensor = self.hass.states.get(zone.sensor_id)
        if read_room_temp(sensor, self.temp_unit) is None:
            add(f"Room sensor {zone.sensor_id} unavailable or invalid.",
                "Restore a numeric temperature report with a supported unit.")
        if zone.range_note:
            add(zone.range_note,
                "Check this room's heat to and cool to; writing either one clears this note.")
        if problem := self.fan.restore_problem(zone.climate_id):
            add(problem, "Reported speed is the conservative fallback; turn Fan auto ON to hand back control.")
        if self.fan.on_pending(zone.climate_id):
            add("Explicit Fan auto ON is waiting for a usable speed report.",
                "Wait for the report, or turn Fan auto OFF to cancel the handback.")
        if not self.data or not self.last_update_success:
            add("Current plan unavailable.",
                "Check the integration error and restore its inputs, then recompute.")
        if self.heads.grid.native_limits(zone.climate_id) is None:
            if self.heads.grid.safe_band(zone.climate_id) is not None:
                add("Native head limits unavailable; using the reported state band.",
                    "Restore native limit metadata; displayed limits can be rounded.")
            else:
                add("Head limits unavailable; using configured temperature clamps.",
                    "Restore valid head limits; configured clamps do not prove hardware acceptance.")
        observed = self.deliveries.attributes(zone.climate_id)
        status = observed["command_status"]
        if status == "pending":
            add(f"Command to {zone.climate_id} pending since {observed['command_attempted_at']}.",
                "Wait for the existing handler to return; ordinary replacements remain queued.")
        if cleanup := observed["vane_retirement_cleanup"]:
            add(f"Own-wake vane retirement parking call {cleanup['command_status']}.",
                "This guarded cleanup does not prove the wake handler returned or the head physically stopped.")
        if observed["command_deferred"]:
            add("Newest room intent deferred behind the pending command.",
                "It will be recomputed from current inputs after that handler returns.")
        if status == "rejected":
            add(f"Last command to {zone.climate_id} rejected at {observed['command_failed_at']}: "
                f"{observed['command_error']}",
                "Check the head integration; the next ordinary update may try again.")
        if status == "cancelled; outcome unknown":
            add("Command cancelled; external outcome unknown.",
                "Check the head integration and its report before relying on delivery.")
        if observed["command_ownership_retired"] and status == "pending":
            add(f"Command ownership retired at {observed['command_retired_at']}.",
                "Already accepted work may return late; retired follow-on commands are suppressed.")
        entry = self.config_entry
        if not entry.options and CONF_DEMAND_THRESHOLD in entry.data:
            add("Options are empty; settings are read from the data mirror.",
                "Review and save Configure to repopulate options.")
        conf = entry_config(entry)
        absent = [label for key, label in (
            (CONF_FAN_BOOST_ENABLE, "fan boost"), (CONF_FAN_BOOST_MAX, "maximum fan speed"),
        ) if key not in conf]
        if absent:
            add(f"Default fan settings used for: {', '.join(absent)}.",
                "The stored entry does not establish whether defaults were intended; review Configure.")
        if (
            CONF_FAN_BOOST_ENABLE in conf and not isinstance(conf[CONF_FAN_BOOST_ENABLE], bool)
            or CONF_FAN_BOOST_MAX in conf and conf[CONF_FAN_BOOST_MAX] not in FAN_LADDER
        ):
            add("Stored fan settings are invalid.",
                "Review and save valid fan settings in Configure.")
        return reasons

    # -- room-sensor freshness ----------------------------------------------
    def _sensor_health(
        self, zone: Zone, state: Any, valid: bool, now: float
    ) -> tuple[str, float | None]:
        """Classify one room sensor; log its episode edges; arm its deadline.

        Returns ``(health, write_age)``. ``write_age`` is a DIAGNOSTIC: how
        long ago Home Assistant last wrote this entity's state, changed or
        unchanged, whoever wrote it. It is published for every room, including
        the ones no cutoff applies to — visible age is what an unknown-cadence
        room gets instead of an invented deadline.

        Evidence is the narrower thing, and what counts as evidence is the
        sensor's declared basis (:func:`evidence_contract`), never the mere
        existence of a duration.
        """
        window = self._freshness.get(zone.slug)
        max_age = None if window is None else window[0]
        reported = None if state is None else state.last_reported.timestamp()
        write_age = None if reported is None else now - reported
        # A report qualifies only if the reading it carries is valid; an
        # invalid write is an M13 failure, not fresher evidence.
        evidence_age = (
            self._evidence_age(zone, reported, now)
            if valid and max_age is not None
            else None
        )
        grace_until = self._grace_until.get(zone.slug)
        health = sensor_health(
            valid=valid,
            max_age=max_age,
            evidence_age=evidence_age,
            in_grace=grace_until is not None and now < grace_until,
        )
        if health != HEALTH_AWAITING:
            self._grace_until.pop(zone.slug, None)  # one grace per incarnation
        self._log_health(zone, health, write_age, evidence_age)
        self._health[zone.slug] = health
        if health == HEALTH_STALE:
            self._stale_window[zone.slug] = cast("tuple[float, float]", window)[0 if evidence_age is not None else 1]
        if health == HEALTH_HEALTHY:
            self._fresh_deadline[zone.slug] = now - cast("float", evidence_age) + cast("float", max_age)
        elif health == HEALTH_AWAITING:
            self._fresh_deadline[zone.slug] = cast("float", grace_until)
        else:
            self._fresh_deadline.pop(zone.slug, None)
        if health in HEALTH_UNHEALTHY:
            # An unhealthy evaluation SPENDS the marker evidence on record:
            # whatever it proved, it did not keep this room healthy, and the
            # room comes back only on an advance observed after this. The
            # marker itself stays as the baseline, so the same sample cannot
            # come back as a new one (a cached value returning unchanged after
            # an outage is the same reading it was).
            self._evidence_spent.add(zone.slug)
        return (health, write_age)

    def _observe_report(
        self, zone: Zone, state: Any, now: float, receipt: float | None
    ) -> None:
        """One observation of a sample-marker sensor, at its observation boundary.

        Evidence is captured HERE, from the write that carried it and with the
        receipt that write's own event stamped — never re-derived from the
        state machine at compute time. ``State.last_reported`` is mutable: an
        unchanged write moves it in place, so a compute re-reading it would
        date a sequence advance from the cache flush that followed it; and a
        marker rejected once for being in the future would be accepted by
        whichever compute ran after the clock caught up with it, with no new
        report at all. So a write is looked at once, as itself, and the record
        it leaves is small: the last ACCEPTED marker and the time of the
        evidence it proved. An observation that does not advance is diagnostic
        only and leaves both alone; so does a rejected one.

        A changed and an unchanged write are observed alike, each at its own
        receipt. An unchanged write carries the marker of the write before it
        — which is not necessarily the marker on RECORD: a write rejected for
        a sample time still in the future left the record alone, so the next
        report of that same sample, judged at its own receipt, can be the
        first acceptable one. A rewrite of a marker already accepted moves
        nothing, by the marker rule above.

        ``receipt`` is None for the state found at startup — that write was
        not witnessed, so a sequence found there is a baseline only, while a
        sample TIME still dates itself (:func:`sample_evidence`). A source on
        the ``ha_state_write`` basis keeps no record: every write it makes is
        a report by contract, and HA's write time is the one it proves.
        """
        contract = self._evidence.get(zone.slug)
        if contract is None or contract[0] == EVIDENCE_HA_WRITE:
            return
        _basis, attribute, marker_is_a_time = contract
        # A report qualifies only if the reading it carries is valid; an
        # invalid write is an M13 failure, not fresher evidence.
        if read_room_temp(state, self.temp_unit) is None:
            return
        accepted, evidence_ts = sample_evidence(
            marker=_read_marker(
                state.attributes.get(attribute), marker_is_a_time=marker_is_a_time
            ),
            last=self._sample_marker.get(zone.slug),
            now=now,
            receipt=receipt,
            marker_is_a_time=marker_is_a_time,
        )
        if accepted is not None:
            self._sample_marker[zone.slug] = accepted
        if evidence_ts is not None:
            self._evidence_ts[zone.slug] = evidence_ts
            self._evidence_spent.discard(zone.slug)

    def _evidence_age(
        self, zone: Zone, reported: float | None, now: float
    ) -> float | None:
        """How long ago this sensor's basis last proved a current reading.

        ``None`` means it has proved none yet — the state may be perfectly
        valid, but nothing here has established that it is a REPORT.

        * ``ha_state_write``: the HA write itself, and only one this
          incarnation witnessed. That is the whole of what a reload costs (see
          ``__init__``): a value already sitting in the state machine could be
          a restored or cached one, so it is not treated as a report.
        * ``sample_timestamp``: the evidence on record (:meth:`_observe_report`),
          unless an unhealthy evaluation has spent it — then nothing on record
          is fresh, whatever the clock says, until the next observed advance.
        """
        basis = self._evidence[zone.slug][0]
        if basis == EVIDENCE_HA_WRITE:
            if reported is None or reported < self._started_ts:
                return None
            return now - reported
        if zone.slug in self._evidence_spent:
            return None
        taken = self._evidence_ts.get(zone.slug)
        return None if taken is None else now - taken

    def _log_health(
        self,
        zone: Zone,
        health: str,
        write_age: float | None,
        evidence_age: float | None,
    ) -> None:
        """One line per episode edge — never per tick, never twice per episode.

        EVERY unhealthy entry opens the episode, with the reason it entered on:
        a room whose sensor drops out or goes non-numeric has stopped steering
        the house exactly as surely as one that went quiet, and the recovery
        line has to have something to close. A continuous run of unhealthy
        states is still ONE episode however its subtype changes inside it (a
        stale room whose sensor then disappears does not deserve a second
        failure line). Recovery to an ELIGIBLE state closes it once — `healthy`
        for an enforcement-capable sensor, `cadence_unknown` for a room that
        never had a cutoff to come back to.

        A stale entry names what actually expired: the evidence (with its age
        against the maximum age), or the startup grace with no report
        witnessed inside it. HA's own write age is given beside it as a
        diagnostic only — for a cached source the two differ, and the write
        age is the one that lies.
        """
        opened = zone.slug in self._unhealthy_logged
        if health == HEALTH_INVALID and not opened and self._startup_quiet:
            if zone.slug not in self._quiet_unusable:
                self._quiet_unusable.add(zone.slug)
                _LOGGER.debug(
                    "MXZ: %s sensor %s has no usable reading yet while Home "
                    "Assistant starts; the room is out of automatic demand. "
                    "Logged as a warning if it persists after startup",
                    zone.name,
                    zone.sensor_id,
                )
        elif health in HEALTH_UNHEALTHY and not opened:
            self._unhealthy_logged.add(zone.slug)
            if health == HEALTH_STALE and evidence_age is not None:
                _LOGGER.warning(
                    "MXZ: %s sensor %s: its last qualifying report is %ds old, "
                    "past its %ds maximum age (Home Assistant last wrote the "
                    "entity %ds ago): the room is out of automatic demand and "
                    "its head parks at the idle action. Other rooms are "
                    "unaffected",
                    zone.name,
                    zone.sensor_id,
                    int(evidence_age),
                    int(self._freshness[zone.slug][0]),
                    int(write_age or 0),
                )
            elif health == HEALTH_STALE:
                _LOGGER.warning(
                    "MXZ: %s sensor %s: no report was witnessed within its %ds "
                    "startup grace (Home Assistant last wrote the entity %ds "
                    "ago): the room is out of automatic demand and its head "
                    "parks at the idle action. Other rooms are unaffected",
                    zone.name,
                    zone.sensor_id,
                    int(self._freshness[zone.slug][1]),
                    int(write_age or 0),
                )
            else:
                _LOGGER.warning(
                    "MXZ: %s sensor %s has no usable reading (missing, "
                    "unavailable or not a finite number): the room is out of "
                    "automatic demand and its head parks at the idle action. "
                    "Other rooms are unaffected",
                    zone.name,
                    zone.sensor_id,
                )
        elif opened and health in HEALTH_ELIGIBLE:
            self._unhealthy_logged.discard(zone.slug)
            _LOGGER.info(
                "MXZ: %s sensor %s is reporting again (%s): automatic demand resumes",
                zone.name,
                zone.sensor_id,
                health,
            )

    def _sync_freshness_timer(self) -> None:
        """Keep exactly one wakeup armed for the nearest freshness deadline.

        A sensor that stops reporting emits nothing to wake us, so without this
        its deadline would only be noticed at the next unrelated recompute or
        the 15-min heartbeat — a 5-minute window could take 20. Every refresh
        re-syncs against the deadlines that compute just derived, so the wakeup
        follows the earliest one, moves when a report moves it, and disappears
        when no room has one left.

        Nothing about the room is captured. The callback only asks for a
        refresh, which re-reads the evidence of the moment it fires — so a
        report landing at the boundary wins by having moved its own deadline,
        and a recovery or a config reload in between is simply what gets seen.
        """
        if not self._fresh_deadline:
            self.clock.cancel("fresh")
            return
        # The boundary itself, with no allowance added: the deadline IS the
        # third due instant, and a room that is stale at it must not keep
        # steering the house for another second by construction. The comparison
        # is half-open, so the refresh this fires evaluates the boundary as
        # stale — and a report that landed at the boundary has already moved
        # its own deadline, so re-reading is what lets it win.
        remaining = min(self._fresh_deadline.values()) - dt_util.utcnow().timestamp()
        self.clock.call_later(max(remaining, 0.0), self._wake, name="fresh")

    @callback
    def _wake(self) -> None:
        """A wait ran out: ask for the ordinary recompute, which re-reads everything."""
        self.hass.async_create_task(self.async_request_refresh())

    async def async_request_refresh(self) -> None:
        """Retain current inputs independently of HA's debounce timer."""
        await self.deliveries.async_request()

    async def async_refresh_now(self) -> None:
        """Recompute at once, without waiting out the request cooldown.

        A plan already running is released from waiting on its deliveries,
        as by any new input, and this one computes next.
        """
        if self.retired:
            return
        self.deliveries.release()
        await self.async_refresh()

    async def _async_update_data(self) -> Plan:
        """Serialize plan decisions on every supported HA version."""
        async with self.deliveries.deciding():
            return await self._async_update_data_once()

    async def _async_update_data_once(self) -> Plan:
        """Recompute and apply one plan."""
        plan, effects = self._compute()
        self._applying_plan = plan
        # Before the plan's commands, as the plan being applied: a head command
        # an effect sends is published on its rooms, as theirs are, while the
        # entities still show the last plan until this one is published below.
        for effect in effects:
            effect()
        # This is computed intent, not delivery confirmation. Per-head software
        # status accompanies it, and in-flight action ownership remains separate.
        self.data = plan
        self.plan_ready = self.plan_ready or self.floor.live
        # Resolve an existing coast wakeup as soon as the decision resolves it;
        # an unrelated pending transport must not postpone its cancellation.
        if not self._coasted:
            self.clock.cancel("coast")
        try:
            await self._apply(plan)
            # _apply is what settles the manual-fan latch (it reads each head's
            # observed fan_mode), so re-stamp fan_hold from the post-apply state —
            # otherwise the plan's diagnostic would lag the latch by a cycle. It
            # is the same truth the Fan auto switch shows, pending restore first.
            for room, zone in zip(plan.rooms, self.zones):
                room.fan_hold = not self.fan.auto_is_on(zone.climate_id)
                room.control_reasons = self._control_reasons(zone)
            return plan
        finally:
            self._applying_plan = None
            self._sync_dwell_timer(plan)
            self._sync_coast_timer()
            self._sync_freshness_timer()
            self._sync_lost_timer()
            self._sync_settle_timer()
            self._sync_comfort_timer()
            self.heads.sync_timer()
            self.openings.sync_timers()

    # -- mode-dwell wakeup ---------------------------------------------------
    def _sync_dwell_timer(self, plan: Plan) -> None:
        """Keep exactly one wakeup armed for a flip the dwell is holding back.

        The hysteresis gate in ``_compute`` is a timestamp comparison, so a
        deferred flip would otherwise wait for the next sensor event or the
        15-min heartbeat — stretching a 10-min dwell to 25. Every refresh
        re-syncs this timer against the CURRENT deferral, so it is replaced
        when the deferral changes and dropped when nothing is deferred: dwell
        elapsed, demand gone, kill-switch off, or a standby park (all of which
        make a wakeup pointless — the recompute could command nothing).

        Nothing about the pending flip is captured. The callback only asks for
        a refresh, which re-reads the inputs of the moment it fires, so a
        manual choice or a newer automatic decision made in between wins.

        A head command is an await, so the dwell can run out between the
        compute and this re-sync — ``plan`` deferred the flip while the clock
        has since passed expiry. That is not "nothing to wait for": it wakes
        up immediately instead. The refresh it asks for computes with the dwell
        elapsed, so its own re-sync takes the ordinary path and this cannot
        chain.
        """
        elapsed = self._mode_change_elapsed()
        remaining = -math.inf if elapsed is None else self.hysteresis - elapsed
        if (
            self.clock.retired
            or not self.coordinator_enable
            or self.parked_by_standby()
        ):
            self.clock.cancel("dwell")
            return
        if remaining <= 0 and plan.mode_change_allowed:
            self.clock.cancel("dwell")  # this plan already had its free hand
            return
        current = self._arbitration_mode()
        held_demands, held_engages = self._held_calls(plan)
        engaged = any(e in (MODE_COOL, MODE_HEAT) for e in held_engages)
        arb_demands: list[Demand] = []
        for i, (room, demand) in enumerate(zip(plan.rooms, held_demands)):
            arb_demands.extend(
                self._zone_arbitration_pair(
                    demand=demand,
                    vote=room.hands_off_vote,
                    safety=room.safety_priority,
                    index=i,
                    floor=room.floor_want,
                    dry=room.dry_vote,
                    boost=room.comfort_priority,
                )
            )
        would_flip_to = arbitrate(
            arb_demands, engaged=engaged, current=current, allowed=True,
            resting=self._resting_bias(), running=current in held_engages,
        ).direction
        if would_flip_to == current:
            self.clock.cancel("dwell")  # nothing is waiting on this dwell
            return
        self._arm_dwell_timer(remaining)

    def _arm_dwell_timer(self, remaining: float) -> None:
        """Replace the pending wakeup with one for this dwell's expiry.

        ``remaining <= 0`` is a dwell that ran out while the last decision was
        being applied: that one wakes up at the first opportunity.
        """
        delay = remaining + 1 if remaining > 0 else 0
        self.clock.call_later(delay, self._wake, name="dwell")

    # -- the mode-change hold: clock, record, display -------------------------
    def _mode_change_elapsed(self) -> float | None:
        """Seconds since the last real mode change; None when none is on record."""
        if self._last_mode_change_ts is None:
            return None
        return self.clock() - self._last_mode_change_ts

    def mode_hold_remaining(self) -> float:
        """Seconds the gate still holds a cool/heat change back (0 when it does not).

        The same comparison the plan's ``mode_change_allowed`` makes, read now.
        """
        elapsed = self._mode_change_elapsed()
        return 0.0 if elapsed is None else max(0.0, self.hysteresis - elapsed)

    def mode_hold_attributes(self) -> dict[str, Any]:
        """The thermostats' view of the hold: whole seconds left, and until when.

        Seconds round up, so 0 means the gate is open, never "almost".
        """
        remaining = self.mode_hold_remaining()
        if remaining <= 0:
            return {"mode_change_hold_remaining": 0, "mode_change_hold_until": None}
        until = dt_util.utcnow() + timedelta(seconds=remaining)
        return {
            "mode_change_hold_remaining": math.ceil(remaining),
            "mode_change_hold_until": until.isoformat(timespec="seconds"),
        }

    def _stamp_mode_change(self) -> None:
        """A real mode change happened now: start the hold.

        The caller writes the record (``_record_mode``) once the shared mode
        and the select show the change.
        """
        self._last_mode_change_ts = self.clock()
        self._last_mode_change_at = dt_util.utcnow().timestamp()
        if self.retired:
            return
        self._arm_hold_end_timer()

    def _record_mode(self) -> None:
        """Write the shared mode, whether votes alone set it, and the last
        real change's time to the record, at once.

        The write itself is scheduled; a reload in between reads it from the
        shared store, and Home Assistant flushes it on shutdown. Called after
        the shared-mode select has shown the change, so for any change made
        here the record is never older than the select's restore data
        (restore_shared_mode compares the two).
        """
        if self.retired:
            return
        data: dict[str, Any] = {
            MODE_KEY: self.current_shared_mode,
            MODE_AT_KEY: dt_util.utcnow().timestamp(),
            MODE_BY_VOTES_KEY: self._mode_by_votes,
        }
        if self._last_mode_change_at is not None:
            data[MODE_CHANGE_KEY] = self._last_mode_change_at
        self._mode_change_store.async_delay_save(lambda: data, 0)

    def restore_shared_mode(self, restored: str | None, restored_at: float | None) -> str:
        """The shared mode a start resumes in; the select calls this once.

        ``restored`` is the select's restored option (None: no restore data)
        and ``restored_at`` when that state last changed (wall clock). The
        record is written at every change, at once, and restore data only
        every 15 minutes and at a stop, so where the two disagree the record
        wins unless the restore data is newer: then the mode changed while a
        version that did not keep it in the record ran, it is not known
        whether votes set it, and the record is brought up to date. Where they
        agree the record also says whether votes set the mode (the select's
        state is written anew at every start, so its time says nothing then).
        """
        record = self._mode_record
        if record.mode is None:
            mode, by_votes = restored or MODE_COOL, False
        elif (
            restored in (None, record.mode)
            or restored_at is None
            or record.mode_at >= restored_at
        ):
            mode, by_votes = record.mode, record.by_votes
        else:
            mode, by_votes = restored, False
        self.current_shared_mode = mode
        self._mode_by_votes = by_votes
        if record.mode not in (None, mode):
            self._record_mode()
        return mode

    def _arm_hold_end_timer(self) -> None:
        """Re-render the entities when the hold ends, and do nothing else.

        It asks for no refresh and decides nothing: a flip the hold deferred
        has the dwell wakeup for that. This only keeps the hold attributes
        honest when nothing else would write them at the end.
        """
        remaining = self.mode_hold_remaining()
        if self.retired or remaining <= 0:
            self.clock.cancel("hold_end")
            return

        @callback
        def _fire() -> None:
            if not self.retired:
                self.async_update_listeners()

        self.clock.call_later(remaining + 1, _fire, name="hold_end")

    # -- engage-latch coast follow-up ----------------------------------------
    def _sync_coast_timer(self) -> None:
        """Keep exactly one wakeup armed for a room that just started coasting.

        The anti-whiplash rule disengages a room before it may run the other
        way, so the compute that sees a direction flip parks the head at the
        idle action and only the NEXT compute can engage the new direction.
        Nothing here scheduled that next compute: it arrived as the echo of our
        own fan write, and a valid dual-setpoint head with no fan mode never
        sends one — so on that head the room stayed parked until an unrelated
        sensor event, the 15-min heartbeat, an ``mxz_recompute`` event or a
        manual call.

        Every refresh re-syncs this against the CURRENT compute, so the wakeup
        is replaced when another room coasts and dropped as soon as a compute
        coasts nothing — which is the compute that resolved it, including the
        one an echoing head's own refresh already ran. It is dropped for a
        kill-switch or standby park too (a recompute could command nothing).

        Nothing about the coast is captured — not the room, not the direction,
        not the plan. The callback only asks for a refresh, which re-reads the
        inputs of the moment it fires, so a room that has drifted back to its
        target by then simply stays parked and a manual choice made in between
        wins. It cannot chain: the compute it asks for finds the latch already
        let go, so that compute coasts nothing.
        """
        if (
            not self.coordinator_enable
            or self.parked_by_standby()
            or not self._coasted
        ):
            self.clock.cancel("coast")
            return
        self.clock.call_later(COAST_FOLLOWUP_DELAY, self._wake, name="coast")

    # -- actuator (mirrors script.mxz_coordinate) ---------------------------
    async def _apply(self, plan: Plan) -> None:
        """Schedule each head's ordered plan writes, record the mode, then wait; idempotent.

        Nothing is awaited between the compute and the mode's writeback, so a
        person's pick is either read by this plan or made after its record,
        and no head's outcome, a reload or a new input can skip the record of
        a change already sent to the heads.
        """
        # A hand-back (P3) is for this apply only: one skipped (Coordinator
        # off, a standby park, no plan yet) leaves the head as MXZ left it.
        handbacks = self.floor.take_handbacks()
        if self.retired:
            return
        if not self.coordinator_enable:
            return  # kill-switch: leave the heads untouched

        # External standby hold (grid-down / load-shed): a fixed-mode park is a
        # no-plan short-circuit; the `eco` hold falls through to the normal plan
        # with eco forced on (see eco_active) so protection extremes still run.
        if self.parked_by_standby():
            await self._park_heads(
                MODE_OFF
                if self.inhibit_action == INHIBIT_ACTION_OFF
                else MODE_FAN_ONLY
            )
            return

        state = plan.state
        valid_eng = (MODE_COOL, MODE_HEAT, ENGAGE_SATISFIED, MODE_OFF)
        if state not in (MODE_COOL, MODE_HEAT):
            return  # plan not ready
        engages = [room.engage for room in plan.rooms]
        if any(engage not in valid_eng for engage in engages):
            return

        tasks = []
        for zone, room in zip(self.zones, plan.rooms):
            engage, floor, dry = room.engage, room.floor_want, room.dry_state
            if zone.climate_id in self._vane_kicks:
                continue  # mid vane-kick: leave the head alone until it finishes
            if self._head_change_settling(zone.climate_id):
                continue  # a change made at the head is holding: don't undo it yet
            if self.openings.paused(zone):
                # Door/window pause: straight to off (no coil-dry run, no fan,
                # setpoint or vane write), and only to a head that is on. A
                # person's fan pick on the off head is still read, so it is
                # kept for the resume.
                head = self.hass.states.get(zone.climate_id)
                if head is None or head.state in UNAVAILABLE_STATES:
                    continue
                self.fan.read(zone.climate_id, MODE_OFF)
                task = self.deliveries.queue(zone.climate_id, MODE_OFF, 0.0, 0.0)
                if task is not None:
                    tasks.append(task)
                continue
            if (
                self.hands_off_phase(zone) in ("parking", "released")
                and not self.inhibited
            ):
                # (A floor run skips all of this: the head is MXZ's for the run.)
                # (Any standby hold, eco included, holds it off like any
                # disabled room: the fixed parks never reach here.)
                if zone.slug in self._hands_off_released:
                    # A floor run ended on a head a person was already
                    # running that way: theirs again, as they had it (P3).
                    task = self.floor.queue_handback(zone, handbacks.get(zone.slug))
                    if task is not None:
                        tasks.append(task)
                    continue  # its off landed: the head is someone else's now
                head = self.hass.states.get(zone.climate_id)
                if (
                    head is not None and head.state == MODE_OFF
                    and not self.deliveries.busy(zone.climate_id)
                ):
                    self._hands_off_released.add(zone.slug)
                    continue  # already off, nothing of ours in flight: landed
                # Otherwise the ordinary disabled path below sends off again.
            if zone.slug in self._startup_resume:
                head = self.hass.states.get(zone.climate_id)
                if head is None or head.state in UNAVAILABLE_STATES:
                    # Do not manufacture the missing startup mode with our own
                    # park/write. A known head still follows sensor-failure parking.
                    continue
            if dry is not None:
                head = self.hass.states.get(zone.climate_id)
                if head is not None and head.state == MODE_DRY:
                    # Dry wets the coil as cooling does: whenever the head
                    # leaves dry, the coil-dry run is owed from then. (A Dry
                    # picked on the thermostat and not reported yet hasn't.)
                    self._last_active[zone.climate_id] = (MODE_COOL, self.clock())
                if dry == "held":
                    # A person's dry: no mode, setpoint, fan or vane write.
                    self.clock.cancel("dry", zone.climate_id)
                    self.fan.read(zone.climate_id, MODE_DRY)
                    continue
                # Another room won heat: park it at its idle action below,
                # once told. Dry is never sent back.
                self.dry.notice_parked(zone)
            idle = self._idle_act_for(zone.climate_id)
            if not zone.enable:
                # A disabled room never idles in fan_only: a floor waiting out
                # the mode is held off. (Without a floor, engage is off and
                # decides first.)
                idle = MODE_OFF
            act = head_action(
                engage=engage,
                mode=state,
                eco=self.eco_active(),
                idle=idle,
            )
            heat_target, cool_target = self.zone_side_targets(zone)
            low, high = setpoints(
                mode=state,
                # A floor runs the head to its clear line, never the eco band.
                target=(
                    self.floor.clear(zone, floor) if floor else float(zone.target)
                ),
                # Heat to while the shared mode is heat, Cool to while it's
                # cool (the one target, twice, in a single-target room). A
                # floor's clear line is its own target.
                heat_target=None if floor else float(heat_target),
                cool_target=None if floor else float(cool_target),
                eco=self.eco_active() and not floor,
                clamp_min=self.clamp_min,
                clamp_max=self.clamp_max,
                band=self.setpoint_band,
                step=self.target_step,
                eco_cool=self.eco_cool,
                eco_heat=self.eco_heat,
            )
            taken = self.floor.taken_over(zone.slug) if floor == state else None
            if taken is not None:
                # A run taken over from a person's own heat keeps their
                # setpoints where those are warmer than the clear line's
                # (cooler, for a ceiling over their cool) (P10): their latest,
                # if they changed them during the run (P15).
                keep = max if floor == MODE_HEAT else min
                low, high = keep(low, taken[1]), keep(high, taken[2])
            shown = room.temp
            # How far the room sits outside its range (off its target, for a
            # single target, where the range is the target itself).
            delta = 0.0 if shown is None else range_delta(
                temp=shown, low=float(heat_target), high=float(cool_target)
            )
            # A floor delivery takes the fan-free path (engage None, as the
            # standby park does): mode and setpoint only, so a manual fan pick
            # is kept and MXZ never writes this head's fan.
            task = self.deliveries.queue(
                zone.climate_id, act, low, high, delta, None if floor else engage
            )
            if task is not None:
                tasks.append(task)
                if taken is not None:
                    self.floor.expect_command(zone, state, low, high)
        self._write_back_mode(plan, engages)
        await self.deliveries.async_wait(tasks)
        await self.openings.async_restore_vanes()

    def _write_back_mode(self, plan: Plan, engages: list[str]) -> None:
        """The plan's mode becomes the shared mode, stamped and recorded.

        Called with the heads' commands queued and none of them run yet. Stamp
        the flip only on a real mode change (cool<->heat). A flip only
        hands-off heads asked for is not one the coordinator caused, so it
        starts no dwell (see logic.arbitrate()). Once a coordinated room runs
        in that mode, the mode is ours: stamp it then, as if that room had
        flipped it, so the next reversal is dwelled. The vote's undoing (back
        to the resting bias) starts no dwell either, but leaves a mode that
        owes the vote nothing: a run there is no flip.
        """
        state = plan.state
        if state != self.current_shared_mode:
            self.current_shared_mode = state
            self._mode_by_votes = plan.mode_by_hands_off and state != self._resting_bias()
            if not plan.mode_by_hands_off:
                self._stamp_mode_change()
            self.async_update_listeners()  # let the shared-mode select re-render
            self._record_mode()
        elif self._mode_by_votes and state in (
            *engages, *(room.demand for room in plan.rooms)
        ):
            self._mode_by_votes = False
            self._stamp_mode_change()
            self._record_mode()

    def room_details(self) -> list[dict[str, Any]]:
        """Existing room diagnostics remain useful even without a current plan.

        Only the plan carries the head's ``last_updated``, as of this publish.
        The room thermostat re-renders on every head event, so a live copy
        there would turn each head attribute-only update into a state event and
        recorder row; the head entity already shows its own time.
        """
        rooms = self.data.rooms if self.data else []
        heads = [self.hass.states.get(zone.climate_id) for zone in self.zones]
        return [
            {
                **(rooms[i].to_attributes() if i < len(rooms) else {"name": zone.name}),
                **self.deliveries.attributes(zone.climate_id),
                "head_state_updated_at": head.last_updated.isoformat() if head else "not yet recorded",
                "control_reasons": self._control_reasons(zone),
            }
            for i, (zone, head) in enumerate(zip(self.zones, heads))
        ]

    def start_points(self, zone: Zone) -> dict[str, Any]:
        """This room's start points as of the latest computed plan.

        Read from the plan the room thermostat shares with the plan sensor, so
        both change together on the recompute a settings change requests. Each
        key reads None until a plan exists.
        """
        rooms = self.data.rooms if self.data else []
        points: dict[str, Any] = next(
            (rooms[i].start_points for i, known in enumerate(self.zones)
             if known is zone and i < len(rooms)),
            {},
        )
        return {key: points.get(key) for key in START_POINT_KEYS}

    @callback
    def redraw_rooms(self) -> None:
        """Re-render the room surfaces (each thermostat, the plan sensor) now.

        Only a re-render, no decision: the plan and the head writes are unchanged.
        """
        for listener in tuple(self._delivery_listeners):
            listener()

    @callback
    def async_add_delivery_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Observe software delivery without a refresh-completion notification."""
        self._delivery_listeners.add(listener)
        return partial(self._delivery_listeners.discard, listener)

    def publish_delivery(self) -> None:
        """Publish changed diagnostics immediately, without another decision."""
        for plan in (self.data, self._applying_plan):
            if plan:
                for room, zone in zip(plan.rooms, self.zones):
                    room.delivery = self.deliveries.attributes(zone.climate_id)
                    room.floor = self.floor.attributes(zone)
                    room.head = self.heads.attributes(zone)
                    room.control_reasons = self._control_reasons(zone)
        for listener in tuple(self._delivery_listeners):
            listener()
        successor = getattr(self.config_entry, "runtime_data", None)
        if isinstance(successor, MXZCoordinator) and successor is not self and not successor.retired:
            # The old handler may return after unload; publish that software
            # observation on the current incarnation's existing room surfaces.
            successor.publish_delivery()

    def parked_by_standby(self) -> bool:
        """Whether an inhibit hold is parking every head at one fixed mode.

        The `eco` hold is not a park: it falls through to the normal plan.
        """
        return self.inhibited and self.inhibit_action in (
            INHIBIT_ACTION_OFF,
            INHIBIT_ACTION_FAN_ONLY,
        )

    def eco_active(self) -> bool:
        """Whether the eco protection band is in effect: the user's eco-idle
        switch, OR an external inhibit hold configured to hold at the eco band."""
        return self.eco_idle or (
            self.inhibited and self.inhibit_action == INHIBIT_ACTION_ECO
        )

    # -- idle action (fan_only / off / off_after_dry) -----------------------
    def _idle_act_for(self, climate_id: str) -> str:
        """The terminal parking mode (fan_only|off) for a non-running head now.

        ``off_after_dry`` resolves per head: off immediately unless the head
        was actively COOLING within the coil-dry window (a wet coil sealed
        behind closed vanes is what grows the smell), in which case it dwells
        in fan_only until the coil-dry clock runs out.
        """
        if self.idle_action == IDLE_ACTION_OFF:
            return MODE_OFF
        if self.idle_action == IDLE_ACTION_OFF_AFTER_DRY:
            return self._resolve_dry_idle(climate_id)
        return MODE_FAN_ONLY

    def _resolve_dry_idle(self, climate_id: str) -> str:
        """off_after_dry: fan_only while the post-cooling dry dwell runs, else off.

        The dwell is a monotonic timestamp comparison — the recompute cycle is
        the clock, so a restart can never strand a head in fan_only. The one-shot nudge
        timer only makes the flip prompt (the 15-min heartbeat would otherwise
        stretch a 10-min dwell).
        """
        rec = self._last_active.get(climate_id)
        if rec is None:
            rec = self._seed_last_active(climate_id)
        if rec is None or rec[0] != MODE_COOL:
            return MODE_OFF  # heat leaves no wet coil; never-ran owes no dwell
        remaining = self.coil_dry_seconds - (self.clock() - rec[1])
        if remaining <= 0:
            return MODE_OFF
        if not self.clock.waiting("dry", climate_id):  # the stamp doesn't move while idle
            self.clock.call_later(remaining + 1, self._wake, name="dry", key=climate_id)
        return MODE_FAN_ONLY

    def _seed_last_active(self, climate_id: str) -> tuple[str, float] | None:
        """Reconstruct dwell memory after a restart from the observed head.

        A head observed still cooling was cooling until the restart; one
        observed in fan_only may be mid-dwell. Both restart the full dwell
        from now — erring a few minutes toward coil-dry, never stranding the
        head (the stamped clock runs out regardless). off/heat/unavailable
        owe nothing.
        """
        state = self.hass.states.get(climate_id)
        mode = state.state if state is not None else None
        if mode in (MODE_COOL, MODE_FAN_ONLY):
            rec = (MODE_COOL, self.clock())
            self._last_active[climate_id] = rec
            return rec
        return None

    def note_head_act(self, climate_id: str, act: str, at: float) -> None:
        """A head driver is sending this head ``act`` for the plan, at ``at``.

        Heat or cool is the head's last run, which the coil-dry dwell counts
        from. Anything but fan_only ends a coil-dry run's wakeup.
        """
        if act in (MODE_COOL, MODE_HEAT):
            self._last_active[climate_id] = (act, at)
        if act != MODE_FAN_ONLY:
            self.clock.cancel("dry", climate_id)

    def _planned_act_for(self, entity_id: str) -> str | None:
        """The action owning this head's writes, else the current plan's action.

        A newer decision may already be published while this head still reports
        an older operation. Lets the off-drift self-heal tell a plan-parked off head (idle_action
        off, eco) from a genuine wall-remote off during an active call — the
        former must not be "healed" back awake.
        """
        if active := self.deliveries.current(entity_id):
            return active.act
        if any(
            zone.climate_id == entity_id and self.openings.paused(zone) for zone in self.zones
        ):
            return MODE_OFF  # a paused room's head is off by plan, never drift
        plan = self._applying_plan if self._applying_plan is not None else self.data
        if not plan or plan.state not in (MODE_COOL, MODE_HEAT):
            return None
        state = plan.state
        for zone, room in zip(self.zones, plan.rooms):
            if zone.climate_id == entity_id:
                engage = room.engage
                if engage not in (MODE_COOL, MODE_HEAT, ENGAGE_SATISFIED, MODE_OFF):
                    return None
                idle = self._idle_act_for(entity_id)
                return head_action(
                    engage=engage,
                    mode=state,
                    eco=self.eco_active(),
                    idle=idle if zone.enable else MODE_OFF,  # as _apply
                )
        return None

    async def _park_heads(self, mode: str) -> None:
        """Standby: drive every coordinated head to a fixed mode (off/fan_only).

        Mode-only — never the fan-boost/latch machinery — so a manual fan hold
        survives the hold (reconciled on release, see FanHold.reseed_after_standby).
        Same per-zone isolation as _apply, and skips a head mid vane-kick.
        """
        tasks = []
        for zone in self.zones:
            if self.retired:
                return
            if zone.climate_id in self._vane_kicks:
                continue
            task = self.deliveries.queue(zone.climate_id, mode, 0.0, 0.0)
            if task is not None:
                tasks.append(task)
        await self.deliveries.async_wait(tasks)

    def _zone_of(self, climate_id: str) -> Zone | None:
        return next((zone for zone in self.zones if zone.climate_id == climate_id), None)

    # -- vane apply / kick ----------------------------------------------------
    async def async_apply_vane(self, climate_id: str, vane_id: str, option: str) -> None:
        """Apply a vane option for a head, kicking an OFF head awake to do it.

        A powered-off head can't move its louvre and forgets vane commands on
        power-up, so when the head is off (eco/away, or a disabled zone) the
        coordinator briefly runs it in fan_only, commands the vane, then hands
        the head back to the plan. A running head just gets the select write
        (the firmware applies it live). With the kill-switch off we never touch
        the head — best-effort select write only.
        """
        if self.retired:
            return
        zone = self._zone_of(climate_id)
        if (
            zone is not None and self.openings.paused(zone)
            and self.coordinator_enable and not self.inhibited
        ):
            # Door/window pause: no kick (it would run the head with the
            # window open). The pick is kept for the resume, which sends it.
            self.openings.keep_vane(zone, vane_id, option)
            self.redraw_rooms()
            return
        state = self.hass.states.get(climate_id)
        running = state is not None and state.state not in (
            MODE_OFF,
            *UNAVAILABLE_STATES,
        )
        # Suppress the kick while held: a kick runs an off head in fan_only,
        # which would wake a head the standby hold deliberately parked. A
        # best-effort select write only (a powered-off head ignores it).
        if running or not self.coordinator_enable or self.inhibited:
            await self._select_option(vane_id, option)
            return
        self._vane_pending[climate_id] = (vane_id, option)
        if climate_id in self._vane_kicks:
            return  # the in-flight kick will pick up the newest pending option
        # eager_start=False so the registry entry — which *is* the kick's
        # ownership token — exists before the coroutine's first ownership check.
        self._vane_kicks[climate_id] = self.hass.async_create_task(
            self._vane_kick(climate_id),
            eager_start=False,
        )

    async def _vane_kick(self, climate_id: str) -> None:
        """fan_only -> apply pending vane option(s) -> off -> re-assert plan.

        Every await here is a real Home Assistant service call or a real delay,
        so the kill-switch, the standby hold or an unload can land between any
        two steps. Ownership is rechecked after each one: a kick that has been
        retired stops commanding, and the retiring path — not this task — parks
        the head it woke.
        """
        try:
            if not self._owns_vane_kick(climate_id):
                return
            # The guard records wake ownership immediately before issuance,
            # after the call has acquired this head's delivery lock.
            await self.deliveries.async_send(
                climate_id, "set_hvac_mode", {"hvac_mode": MODE_FAN_ONLY},
                guard=partial(self._vane_kick_issues, climate_id, waking=True),
            )
            if not self._owns_vane_kick(climate_id):
                return
            await asyncio.sleep(self._vane_kick_spinup)
            if not self._owns_vane_kick(climate_id):
                return
            while (pending := self._vane_pending.pop(climate_id, None)) is not None:
                await self._select_option(*pending)
                if not self._owns_vane_kick(climate_id):
                    return
                await asyncio.sleep(self._vane_kick_apply)
                if not self._owns_vane_kick(climate_id):
                    return
            await self.deliveries.async_send(
                climate_id, "set_hvac_mode", {"hvac_mode": MODE_OFF},
                guard=partial(self._vane_kick_issues, climate_id, waking=False),
            )
            self._vane_kick_woken.discard(climate_id)  # parked, nothing owed
            if not self._owns_vane_kick(climate_id):
                return
            await self.async_request_refresh()
        finally:
            # Only the current owner cleans up: a retired task must not erase a
            # newer kick's entry, and must leave an unparked head to retirement.
            if self._vane_kicks.get(climate_id) is asyncio.current_task():
                self._vane_kicks.pop(climate_id, None)
                self._vane_pending.pop(climate_id, None)

    def _vane_kick_issues(self, climate_id: str, *, waking: bool) -> bool:
        """Whether the kick still owns its head as its call is sent; a wake is recorded then.

        Ownership starts at issuance, not while waiting for the head's turn.
        """
        if not self._owns_vane_kick(climate_id):
            return False
        if waking:
            self._vane_kick_woken.add(climate_id)
        return True

    def _owns_vane_kick(self, climate_id: str) -> bool:
        """True while the calling task still owns this head's live kick."""
        return (
            not self.retired
            and self.coordinator_enable
            and not self.inhibited
            and self._vane_kicks.get(climate_id) is asyncio.current_task()
        )

    async def _async_retire_vane_kicks(self) -> None:
        """Disown, cancel and bound-wait every kick, then park what it woke.

        Dropping the registry entry first is what actually stops the commands:
        a task that resumes after this can never pass _owns_vane_kick again,
        whether or not it honors the cancellation.
        """
        tasks = tuple(self._vane_kicks.values())
        for climate_id in tuple(self._vane_kicks):
            task = self._vane_kicks.pop(climate_id, None)
            self._vane_pending.pop(climate_id, None)
            self.deliveries.disown_task(climate_id, task)
        self.publish_delivery()
        for task in tasks:
            task.cancel()
        if tasks:
            # asyncio.wait, not wait_for/gather: cancellation is a request, and
            # a third-party service handler that defers it must not hold up an
            # unload. A deferred task is disowned already and cannot command.
            _done, pending = await asyncio.wait(tasks, timeout=self._vane_kick_retire)
            if pending:
                _LOGGER.warning(
                    "MXZ: %d vane kick(s) did not unwind within %ss; "
                    "they are disowned and can no longer command a head",
                    len(pending),
                    self._vane_kick_retire,
                )
        await self._restore_woken_heads()

    async def _restore_woken_heads(self) -> None:
        """Park heads this coordinator ran up to fan_only for a kick.

        The kick's own ``off`` restore is what hands a temporarily woken head
        back to the plan; a retired kick never gets to send it, so the retiring
        path sends it here instead. Only a head
        still sitting in the fan_only *we* commanded is parked — anything else
        means someone took the head over, and that choice stands. This existing
        cleanup can run while its wake handler is still pending. It neither
        releases that handler's lock nor admits ordinary queued replacements.
        """
        for climate_id in tuple(self._vane_kick_woken):
            self._vane_kick_woken.discard(climate_id)
            state = self.hass.states.get(climate_id)
            if state is None or state.state != MODE_FAN_ONLY:
                continue
            try:
                # Keep this exception inside the own-wake/reported-mode guard.
                # Its return must not overwrite an outstanding wake's status.
                await self.deliveries.async_send_cleanup(
                    climate_id, "set_hvac_mode", {"hvac_mode": MODE_OFF},
                )
            except HomeAssistantError as err:
                _LOGGER.error(
                    "MXZ: parking %s after its vane kick failed "
                    "(zone degraded, others continue): %s",
                    climate_id,
                    err,
                )

    async def _select_option(self, vane_id: str, option: str) -> None:
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": vane_id, "option": option},
            blocking=True,
        )

    # -- triggers -----------------------------------------------------------
    @callback
    def _on_input_change(self, event: Event[EventStateChangedData]) -> None:
        """A temp sensor changed -> observe the write, then recompute.

        Every write that can carry a new sample marker lands here: a marker
        that moved is an attribute that changed, whatever the temperature did.
        """
        self._observe_input(event)
        generation = self.deliveries.generation

        async def _request_current_input() -> None:
            # A decision since this receipt already read the current sensor
            # states. Do not turn the rest of that burst into a delivery retry.
            if self.deliveries.generation == generation:
                await self.async_request_refresh()

        self.hass.async_create_task(_request_current_input(), eager_start=False)

    @callback
    def _on_input_report(self, event: Event[EventStateReportedData]) -> None:
        """A temp sensor re-reported the SAME value.

        It is observed first, exactly as a changed write is, at its own
        immutable receipt (:meth:`_observe_input`): an unchanged write carries
        the marker of the write before it, and when that write was rejected —
        a sample time still in the future when it arrived — the record still
        holds the older accepted marker, so this new report of the same sample
        is the one that can recover the room. A rewrite of an accepted marker
        moves neither the receipt nor the deadline (:func:`sample_evidence`).

        Then: an unchanged write is exactly the report an unhealthy or
        provisional room is waiting for: it recovers a stale or invalid room,
        and it replaces a provisional room's startup grace with that report's
        own evidence deadline — which can be much sooner than the grace. A
        healthy or unknown-cadence room learns nothing from it (the value did
        not change, and its deadline is re-read when it next matters), so a
        chatty sensor cannot turn this stream into a recompute treadmill.
        """
        self._observe_input(event)
        entity_id: str = event.data["entity_id"]
        if any(
            zone.sensor_id == entity_id
            and self._health.get(zone.slug) in HEALTH_REPORT_SENSITIVE
            for zone in self.zones
        ):
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _observe_input(
        self, event: Event[EventStateChangedData] | Event[EventStateReportedData]
    ) -> None:
        """Record one sensor write as the observation it is (:meth:`_observe_report`).

        The event's ``time_fired`` is the instant HA stamped into this write's
        ``last_reported`` — and, unlike that field, it never moves.
        """
        entity_id: str = event.data["entity_id"]
        receipt = event.time_fired_timestamp
        for zone in self.zones:
            if zone.sensor_id == entity_id:
                self._observe_report(zone, event.data.get("new_state"), receipt, receipt)

    @callback
    def _on_heartbeat(self, _now: Any) -> None:
        """15-min drift re-assert."""
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_recompute_event(self, _event: Event) -> None:
        """mxz_recompute event (manual / proxy) -> recompute."""
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_ha_start(self, _hass: HomeAssistant) -> None:
        """After HA start, recompute once entities have had time to settle."""
        self.clock.call_later(STARTUP_RECOVER_DELAY, self._on_startup_timer, name="startup")

    @callback
    def _on_startup_timer(self) -> None:
        # Startup has settled: this recompute logs any room still unusable.
        self._startup_quiet = False
        self.hass.async_create_task(self.async_request_refresh())

    # -- self-heal A (band drift) -------------------------------------------
    @callback
    def _on_head_change(self, event: Event[EventStateChangedData]) -> None:
        """Track head inputs and detect banned-mode/off-while-enabled drift."""
        entity_id: str = event.data["entity_id"]
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        mode = new_state.state if new_state else None
        if new_state is not None:
            self.fan.head_reported(entity_id, new_state.attributes.get("fan_mode"))
            self._head_writes_caught_up(entity_id, new_state)
        refresh_needed = False
        refresh_now = False
        sight = self._sight.get(entity_id)
        if sight is not None and mode in (None, STATE_UNAVAILABLE):
            # Lost sight of it: what it showed just before is its last-known
            # vote, even if no compute ran in between.
            clock = self.clock()
            if old_state is not None and old_state.state != STATE_UNAVAILABLE:
                sight.observe(
                    old_state.state, old_state.attributes.get(ATTR_HVAC_ACTION), clock
                )
            sight.lose(clock, dt_util.utcnow().timestamp())
        if sight is not None and (mode in (None, STATE_UNAVAILABLE)) != (
            old_state is None or old_state.state == STATE_UNAVAILABLE
        ):
            # Out of sight, or back. HA's unknown is in sight but counts as
            # unavailable below, so unknown<->unavailable needs this to
            # recompute the vote and the hold or expiry wakeup now.
            refresh_needed = True
        if (
            new_state is not None and old_state is not None
            and any(
                zone.climate_id == entity_id
                and zone.vote_basis == VOTE_BASIS_ACTION
                and self.hands_off_phase(zone) == "released"
                for zone in self.zones
            )
        ):
            # A released room that votes by action (before release its head
            # has no vote to change): the head starting or stopping a run
            # starts or ends its vote (or its hold) from this report, so
            # recompute at once. A debounced request would wait out any
            # cooldown already running, and room reports and the person's own
            # mode change open one every few seconds. A run reported just
            # after a mode change is seen here too, but counts only at its
            # settle window's end (see _sync_settle_timer).
            refresh_now = (
                mode != old_state.state
                or new_state.attributes.get(ATTR_HVAC_ACTION)
                != old_state.attributes.get(ATTR_HVAC_ACTION)
            )
        pending = next(
            (zone for zone in self.zones
             if zone.climate_id == entity_id and zone.slug in self._startup_resume),
            None,
        )
        if pending is not None:
            if (
                mode == MODE_OFF and self._planned_act_for(entity_id) != MODE_OFF
                and not self.heads.stale_report(entity_id, mode=mode)
            ):
                # A reported OFF beyond our own sensor-failure park cancels
                # the remembered startup run, just like OFF present at restore.
                self._startup_resume[pending.slug] = ""
            if mode not in (None, *UNAVAILABLE_STATES) and mode != (
                old_state.state if old_state else None
            ):
                refresh_needed = True

        if mode == MODE_OFF:
            # A parking hands-off head seen off has landed, unless a command
            # of ours other than that off is still in flight (it could still
            # turn the head back on). The off may be ours or a person's: the
            # head is off either way, and anything after this is theirs.
            active = self.deliveries.current(entity_id)
            if active is None or active.act == MODE_OFF:
                for zone in self.zones:
                    if (
                        zone.climate_id == entity_id
                        and self.hands_off_phase(zone) == "parking"
                    ):
                        self._hands_off_released.add(zone.slug)
                        if sight is not None:
                            # Any run MXZ drove ends here, not at the next
                            # compute: a person's heat before that one is
                            # not that run stopping, so it starts no hold.
                            sight.observe(
                                mode, cast("State", new_state).attributes.get(ATTR_HVAC_ACTION),
                                self.clock(),
                            )
        # A person changing a hands-off head during a floor run takes it back
        # (decision 8b): a real mode change, between two available states, to
        # anything but what MXZ is sending or would send, and not a vane kick.
        old_mode = old_state.state if old_state else None
        if (
            mode != old_mode
            and mode not in (None, *UNAVAILABLE_STATES)
            and old_mode not in (None, *UNAVAILABLE_STATES)
            and entity_id not in self._vane_kicks
            and entity_id not in self._vane_kick_woken
            and not self.heads.stale_report(entity_id, mode=mode)
        ):
            for zone in self.zones:
                if (
                    zone.climate_id == entity_id
                    and self.hands_off_phase(zone) == "floor"
                    and mode != self._planned_act_for(entity_id)
                ):
                    self.floor.yield_to_person(zone, dt_util.utcnow().timestamp())
                    refresh_needed = True
        # A person turning a paused room's head on, or changing its mode (its
        # remote, its app, the head entity in HA), takes the room over until
        # the opening closes: a real mode change between two available
        # states, to anything but off, not MXZ's own command or vane kick.
        if (
            mode != old_mode
            and mode not in (None, MODE_OFF, *UNAVAILABLE_STATES)
            and old_mode not in (None, *UNAVAILABLE_STATES)
            and entity_id not in self._vane_kicks
            and entity_id not in self._vane_kick_woken
            and not self.heads.stale_report(entity_id, mode=mode)
        ):
            for zone in self.zones:
                if (
                    zone.climate_id == entity_id
                    and self.openings.paused(zone)
                    and mode != self._planned_act_for(entity_id)
                ):
                    self.openings.take_over(zone)
                    refresh_needed = True
        # A cloud head's report of an older state inside its confirm wait is
        # not drift either (never true for a local head).
        stale = self.heads.stale_report(entity_id, mode=mode)
        self._arm_or_cancel(
            entity_id, "band",
            mode in BANNED_MODES and not self._hands_off_head(entity_id) and not stale
            and not self.dry.allowed(entity_id, mode),
            BAND_DRIFT_DELAY,
        )
        if (mode == MODE_DRY) != (old_mode == MODE_DRY) and any(
            zone.climate_id == entity_id and zone.allow_dry for zone in self.zones
        ):
            # Allow Dry mode: into dry pauses the room and starts its vote,
            # out of dry ends both, so recompute now, not at the heartbeat.
            # Dry wets the coil as cooling does: leaving it owes the coil-dry
            # run from this moment.
            refresh_needed = True
            if old_mode == MODE_DRY and not stale and mode not in (None, *UNAVAILABLE_STATES):
                self._last_active[entity_id] = (MODE_COOL, self.clock())
        if self.dry.head_reported(entity_id, old_state, new_state, stale=stale):
            refresh_needed = True
        # Never heal a head the standby hold parked off (nor a wall override
        # mid-hold — a human turning a parked head on during an outage wins),
        # and never heal a head the PLAN itself parked off (idle_action) — but
        # a wall-remote off during an active call or a coil-dry dwell is still
        # drift (the plan wants that head awake).
        off_drift = self._off_while_enabled(entity_id, mode) and not self.inhibited and not stale
        self._arm_or_cancel(entity_id, "off", off_drift, OFF_WHILE_ENABLED_DELAY)
        if self._watch_head_change(entity_id, old_state, new_state):
            refresh_needed = True  # a hold ended with nothing adopted: the plan owes the head
        if (cloud := self.heads.cloud(entity_id)) is not None and cloud.unresponsive:
            refresh_needed = True  # an out-of-control cloud head reported again

        # A fan_mode change is a latch-relevant observation (a manual pick, an
        # `auto` handback, or our own write's echo): refresh promptly so the
        # hold engages and the Fan auto switch mirrors it now, not at the next
        # heartbeat. Idempotent writes + the echo-tolerant departure check make
        # a refresh on our own echo harmless.
        new_fan = new_state.attributes.get("fan_mode") if new_state else None
        old_fan = old_state.attributes.get("fan_mode") if old_state else None
        # Capability metadata and head availability also feed the plan's room
        # explanations, even when there is no new fan observation. Use the same
        # debounced refresh; ordinary echoes still rely on idempotent apply.
        new_attrs = new_state.attributes if new_state else {}
        old_attrs = old_state.attributes if old_state else {}
        capabilities_changed = any(
            type(new_attrs.get(key)) is not type(old_attrs.get(key))
            or new_attrs.get(key) != old_attrs.get(key)
            for key in ("fan_modes", "supported_features")
        )
        availability_changed = (
            new_state is None or mode in UNAVAILABLE_STATES
        ) != (
            old_state is None or old_state.state in UNAVAILABLE_STATES
        )
        if refresh_now:
            self.hass.async_create_task(self.async_refresh_now())
        elif refresh_needed or new_fan != old_fan or capabilities_changed or availability_changed:
            self.deliveries.request_for_report()

    @callback
    def _arm_or_cancel(
        self, entity_id: str, kind: str, condition: bool, delay: int
    ) -> None:
        """(Re)arm a debounce wait for a drift condition, or cancel it if cleared."""
        key = (entity_id, kind)
        if condition:
            self.clock.call_later(delay, partial(self._on_heal_timer, key), name="heal", key=key)
        else:
            self.clock.cancel("heal", key)

    @callback
    def _on_heal_timer(self, key: tuple[str, str]) -> None:
        """A drift condition persisted past its debounce window."""
        entity_id, kind = key
        if kind == "adopt":
            self.hass.async_create_task(self._async_adopt_head_change(entity_id))
            return
        state = self.hass.states.get(entity_id)
        mode = state.state if state else None
        if kind == "band":
            still = (
                mode in BANNED_MODES and not self._hands_off_head(entity_id)
                and not self.dry.allowed(entity_id, mode)
            )
        else:
            still = self._off_while_enabled(entity_id, mode)
        still = still and not self.heads.stale_report(entity_id, mode=mode)
        if still and self.coordinator_enable and not self.inhibited:
            self.hass.async_create_task(self._heal_and_notify(entity_id, kind, mode))

    def _off_while_enabled(self, entity_id: str, mode: str | None) -> bool:
        """Off while its room is enabled and the plan wants it on: the off heal's drift."""
        return (
            mode == MODE_OFF
            and self._enable_for(entity_id)
            and not self.eco_idle
            and self._planned_act_for(entity_id) != MODE_OFF
        )

    # -- accept changes made at the head ---------------------------------------
    # From @BHSPitMonkey's fork (Discussion #103), rebuilt on MXZ's write memory: a
    # report MXZ's own writes can explain is never a person, however late it comes.
    def _head_write_same(self, climate_id: str, name: str) -> Callable[[Any, Any], bool]:
        """How a report of this field compares with a write of it."""
        if name == _HEAD_WRITE_MODE:
            return operator.eq
        return partial(self.heads.grid.already_at, climate_id)

    @staticmethod
    def _head_shows(state: Any, name: str) -> Any:
        """What a head's state shows for this field, None if nothing usable."""
        if state is None or state.state in UNAVAILABLE_STATES:
            return None
        if name == _HEAD_WRITE_MODE:
            return state.state
        return as_float(state.attributes.get(name))

    def remember_head_write(self, climate_id: str, payload: dict[str, Any]) -> None:
        """Keep a mode or setpoint MXZ is sending, as ``FanEcho.wrote`` does a fan."""
        state = self.hass.states.get(climate_id)
        memory = self._head_writes.setdefault(climate_id, {})
        for name in _HEAD_WRITE_FIELDS:
            if name in payload:
                memory.setdefault(name, HeadWriteMemory(HEAD_WRITES_REMEMBERED)).wrote(
                    payload[name], self._head_shows(state, name),
                    self._head_write_same(climate_id, name),
                )

    def _head_writes_caught_up(self, climate_id: str, state: Any) -> None:
        """A head's report: the writes before it are behind the head for good."""
        for name, memory in self._head_writes.get(climate_id, {}).items():
            shown = self._head_shows(state, name)
            if shown is not None:
                memory.caught_up(shown, self._head_write_same(climate_id, name))

    def _head_write_is_own(self, climate_id: str, name: str, shown: Any) -> bool:
        memory = self._head_writes.get(climate_id, {}).get(name)
        return memory is not None and memory.own(shown, self._head_write_same(climate_id, name))

    def _head_setpoint_field(self, climate_id: str, state: Any) -> str | None:
        """The setpoint carrying the room's target in the mode this head shows.

        The same one MXZ writes: the high edge in cool, the low edge in heat, or
        a single-setpoint or cloud head's one setpoint. None off heat and cool.
        """
        if state.state not in (MODE_COOL, MODE_HEAT):
            return None
        features = int(state.attributes.get("supported_features") or 0)
        if self.heads.cloud(climate_id) is not None or not features & 2:
            return "temperature"
        return "target_temp_high" if state.state == MODE_COOL else "target_temp_low"

    def _target_for_head_setpoint(self, climate_id: str, shown: float) -> float:
        """The target on our step that leaves this head where a person put it.

        18.5 °C on a CN105 reads 65.5 °F here, whose nearest whole °F is 66, and 66
        °F would move the head on to 19 °C. So the nearest target the head would
        hold ``shown`` for, a step either side if need be; with none, the nearest.
        """
        step = self.target_step or 1.0
        nearest = snap_to_step(shown, step)
        return next(
            (
                target for target in (nearest, nearest - step, nearest + step)
                if self.heads.grid.already_at(climate_id, shown, target)
            ),
            nearest,
        )

    def _head_adoption(self, climate_id: str, moved: set[str]) -> dict[str, Any] | None:
        """What this head's change asks of its room now; None if it isn't a person's.

        An empty result is a person's change with nothing to adopt, like a
        heat/cool choice: the head still goes back on the plan. ``enable`` and
        ``target`` are the room's new enable and target. Only what moved counts, and
        only if MXZ's own writes can't explain it, so a mode change alone never
        reads the setpoint the head happened to be on as a new target.
        """
        zone = self._zone_of(climate_id)
        state = self.hass.states.get(climate_id)
        if (
            not self.accept_head_changes
            or self.retired
            or not self.coordinator_enable
            or self.inhibited
            or self.eco_active()
            or self._startup_quiet
            or zone is None
            or state is None
            or state.state in UNAVAILABLE_STATES
            # A disabled hands-off room's head is the person's, on purpose; a
            # floor run's is MXZ's until it ends; a paused room's has its own rule.
            or self.hands_off_phase(zone) is not None
            or self.floor.tripped(zone.slug)
            or self.openings.paused(zone)
            or self.heads.stale_report(climate_id, mode=state.state)
        ):
            return None
        mode = state.state
        mode_press = "mode" in moved and not self._head_write_is_own(
            climate_id, _HEAD_WRITE_MODE, mode
        )
        if mode == MODE_OFF:
            # An off the plan itself wants is not a request.
            if mode_press and zone.enable and self._planned_act_for(climate_id) != MODE_OFF:
                return {"enable": False}
            return None
        name = self._head_setpoint_field(climate_id, state)
        if name is None:
            return None  # fan_only is the park; heat_cool, auto and dry are healed
        if not zone.enable and not mode_press:
            return None
        shown = as_float(state.attributes.get(name))
        setpoint_press = (
            "setpoint" in moved and shown is not None
            and not self._head_write_is_own(climate_id, name, shown)
            # A cloud head's poll from before MXZ's last batch, as for its mode.
            and not self.heads.stale_report(climate_id, setpoint=shown)
        )
        if not mode_press and not setpoint_press:
            return None
        change: dict[str, Any] = {} if zone.enable else {"enable": True}
        # A cloud head runs past the room's target by its drive offset, so what it
        # shows isn't the target and stays MXZ's to write.
        driven = self.heads.cloud(climate_id) is not None and bool(zone.drive_offset)
        if setpoint_press and not driven:
            target = clamp(
                self._target_for_head_setpoint(climate_id, cast("float", shown)),
                self.heads.grid.target_bounds(climate_id),
            )
            step = self.target_step or 1.0
            heat_target, cool_target = self.zone_side_targets(zone)
            current = cool_target if mode == MODE_COOL else heat_target
            if abs(target - current) >= step / 2:
                change["target"] = target
        return change

    @callback
    def _watch_head_change(self, climate_id: str, old_state: Any, new_state: Any) -> bool:
        """Arm, re-arm or cancel the adopt timer on a head's report.

        A report from or to unavailable is the head (re)loading, never a person,
        and a fan or vane report moves nothing here. Each report that moves the
        mode or the setpoint starts the hold over. A change with nothing to adopt
        (a heat/cool flip alone) holds nothing: the head goes back on the plan at
        the next compute, as with the option off.

        True when this report ended a hold with nothing adopted (a press taken
        back, say): the plan skipped the head while it held, and arming it
        disowned any command still waiting, so the caller must re-plan.
        """
        if (
            not self.accept_head_changes or old_state is None or new_state is None
            or old_state.state in UNAVAILABLE_STATES or new_state.state in UNAVAILABLE_STATES
        ):
            return False
        moved = {"mode"} if old_state.state != new_state.state else set()
        name = self._head_setpoint_field(climate_id, new_state)
        if name is not None and old_state.attributes.get(name) != new_state.attributes.get(name):
            moved.add("setpoint")
        if not moved:
            return False
        moved |= self._adopt_moved.get(climate_id, set())
        if not self._head_adoption(climate_id, moved):
            held = self.clock.waiting("heal", (climate_id, "adopt"))
            self.drop_head_change(climate_id)
            return held
        self._adopt_moved[climate_id] = moved
        self._arm_or_cancel(climate_id, "adopt", True, ADOPT_DELAY)
        # A person's change is newer than a command of ours still to be sent.
        # Whoever ends the hold re-plans: an adoption does, and so does a hold
        # that ends with nothing adopted (``_watch_head_change``).
        self.deliveries.disown(climate_id)
        return False

    def _head_change_settling(self, climate_id: str) -> bool:
        """Whether a person's change at this head is still holding, and would still count.

        Only a change with a target or an enable in it: an empty one protects nothing.
        """
        return self.clock.waiting("heal", (climate_id, "adopt")) and bool(
            self._head_adoption(climate_id, self._adopt_moved.get(climate_id, set()))
        )

    @callback
    def drop_head_change(self, climate_id: str) -> None:
        """A hold ends with nothing to adopt. A change to the room in Home Assistant
        during one ends it too: the later change wins."""
        self._arm_or_cancel(climate_id, "adopt", False, ADOPT_DELAY)
        self._adopt_moved.pop(climate_id, None)

    def _head_change_seen(self, climate_id: str, state: Any, moved: set[str]) -> None:
        """A person's change is what the head shows now, whatever MXZ wrote before it."""
        memory = self._head_writes.get(climate_id, {})
        names = []
        if "mode" in moved:
            names.append(_HEAD_WRITE_MODE)
        if "setpoint" in moved and (name := self._head_setpoint_field(climate_id, state)):
            names.append(name)
        for name in names:
            shown = self._head_shows(state, name)
            if name in memory and shown is not None and not self._head_write_is_own(
                climate_id, name, shown
            ):
                memory[name].seen(shown)

    def _sibling_entity(self, platform: str, key: str) -> str | None:
        """One of this entry's own entities by its key, under whatever it was renamed to."""
        return er.async_get(self.hass).async_get_entity_id(
            platform, DOMAIN, f"{self.config_entry.entry_id}_{key}"
        )

    async def _async_adopt_head_change(self, climate_id: str) -> None:
        """A change at a head held past the delay: copy it to the room, then re-plan.

        Through the room's own number and switch, so it follows their rules and
        restores like any other change. The apply this ends then puts the head on
        the plan's mode, with the new target. If a write is refused, nothing is
        adopted and that apply overwrites the head, as it would with the option off.
        """
        moved = self._adopt_moved.pop(climate_id, set())
        change = self._head_adoption(climate_id, moved)
        try:
            if change is None:
                return
            zone = cast("Zone", self._zone_of(climate_id))
            state = self.hass.states.get(climate_id)
            self._head_change_seen(climate_id, state, moved)
            if change:
                _LOGGER.info("MXZ: adopting a change made at %s: %s", climate_id, change)
            if "target" in change:
                suffix = "target"
                if zone.dual:
                    suffix += "_high" if cast("State", state).state == MODE_COOL else "_low"
                if eid := self._sibling_entity("number", f"{zone.slug}_{suffix}"):
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": eid, "value": change["target"]}, blocking=True,
                    )
            if "enable" in change and (
                eid := self._sibling_entity("switch", f"{zone.slug}_enable")
            ):
                await self.hass.services.async_call(
                    "switch", "turn_on" if change["enable"] else "turn_off",
                    {"entity_id": eid}, blocking=True,
                )
            # The off heal, rechecked against the room as it is now: an off
            # adopted as "disable the room" is the room's, not drift, and a
            # later re-enable is read from the head's next report, not from
            # this timer. A refused adopt leaves it running, as with the option off.
            head = self.hass.states.get(climate_id)
            if self.clock.waiting("heal", (climate_id, "off")) and not self._off_while_enabled(
                climate_id, head.state if head is not None else None
            ):
                self._arm_or_cancel(climate_id, "off", False, OFF_WHILE_ENABLED_DELAY)
        except HomeAssistantError as err:
            _LOGGER.warning("MXZ: couldn't adopt a change made at %s: %s", climate_id, err)
        finally:
            if not self.retired:
                await self.async_request_refresh()

    async def _heal_and_notify(self, entity_id: str, kind: str, mode: str | None) -> None:
        """Re-apply the plan to a head that drifted; log it, and tell someone once an hour.

        Every heal is logged. The notice names the room and head, and goes out
        at most once per head per HEAL_NOTICE_SECONDS, as floor notices are
        limited per room, so a head that keeps drifting can't flood anyone.
        """
        zone = self._zone_of(entity_id)
        room = zone.name if zone is not None else entity_id
        now = self.clock()
        last = self._heal_notice_at.get(entity_id)
        held = bool(self.notify_service) and last is not None and now - last < HEAL_NOTICE_SECONDS
        _LOGGER.warning(
            "MXZ heal: room %s, head %s: %s drift, reported %s; re-applying the plan%s",
            room, entity_id, kind, mode,
            " (notice not sent: one heal notice per head per hour)" if held else "",
        )
        if not held:
            self._heal_notice_at[entity_id] = now
        await self.async_request_refresh()
        if held:
            return
        if kind == "off":
            what = "was off while its room is enabled"
        else:
            what = f"was in {mode}, off its coordinated mode"
        await self.notify(f"{room}: {entity_id} {what} - re-applying via the coordinator.")

    async def notify(self, message: str, target: str | None = None) -> None:
        target = target or self.notify_service
        if not target:
            return
        domain, _, service = target.partition(".")
        if not service:
            return
        await self.hass.services.async_call(
            domain,
            service,
            {"title": "HVAC Update", "message": message},
            blocking=False,
        )

    def _enable_for(self, climate_id: str) -> bool:
        return any(
            zone.enable for zone in self.zones if zone.climate_id == climate_id
        )

    # -- hands-off rooms (opt-in) --------------------------------------------
    def hands_off_phase(self, zone: Zone) -> str | None:
        """None unless the room is opted in AND disabled; then parking|released,
        or floor while its tripped floor has MXZ running the head."""
        if not zone.hands_off or zone.enable:
            return None
        if self.floor.tripped(zone.slug):
            return "floor"
        return "released" if zone.slug in self._hands_off_released else "parking"

    def _hands_off_head(self, climate_id: str) -> bool:
        """True when this head belongs to a released hands-off room.

        A person's dry or auto on a floor-run head is not healed either: the
        yield in :meth:`_on_head_change` releases the room before the band
        check reads this.
        """
        return any(
            zone.climate_id == climate_id and self.hands_off_phase(zone) == "released"
            for zone in self.zones
        )

    def _hands_off_vote(self, zone: Zone, clock: float, *, safety: bool) -> str | None:
        """What a released hands-off head is running, if it is heat or cool.

        Before the off lands, the head's mode is ours or stale, so it has no
        vote. Every other reported state (fan_only, dry, auto, heat_cool, off,
        unknown) asks nothing of the outdoor unit's direction. A head MXZ has
        lost sight of (unavailable, or gone) keeps the vote it last showed,
        stale, until the time limit runs out; then it has none (see
        logic.HeadSight, adapted from @csingley's ha-climate-zone-sequencer).
        A head not seen since MXZ started has no last vote at all.

        A room that votes by action counts that heat or cool only while its
        head's own hvac_action says it is running it, and for the hold after
        (logic.HeadSight.action_vote). Votes read from observations, not
        configuration, is @csingley's idea (ha-climate-zone-sequencer f911e4a,
        "derive zone votes from observations, not event recency"); filtering
        by the running action is MXZ's extension of it. A room with its Safety
        priority sensor on votes its mode, running or not: safety never waits
        on the filter.
        """
        if self.hands_off_phase(zone) != "released" or self.inhibited:
            return None  # (a standby hold is holding it off)
        sight = self._sight[zone.climate_id]
        if (cloud := self.heads.cloud(zone.climate_id)) is not None and (
            cloud.unresponsive or cloud.stale(clock, mode=sight.mode)
        ):
            # A cloud head out of control, or reporting what may be an older
            # state inside its confirm wait, shows nothing to vote on.
            return None
        if safety or self._vote_basis(zone) != VOTE_BASIS_ACTION:
            return sight.vote(clock, self.lost_vote_ttl)
        return sight.action_vote(clock, self.lost_vote_ttl, self.action_vote_hold)

    def _vote_basis(self, zone: Zone) -> str:
        """What this room's head vote follows now: its mode, or its action.

        Action only for a room set to it whose head reports an hvac_action of
        its own; a head seen, but never seen reporting one since MXZ started,
        votes by its mode. A head that has reported one stays on action even
        if a later report lacks it, so a dropped attribute can't hand it a
        vote it isn't running for. Only the head's own attribute counts: MXZ's
        thermostat derives an action from the head's mode for display, and
        that is never an observation.
        """
        if zone.vote_basis != VOTE_BASIS_ACTION:
            return VOTE_BASIS_MODE
        sight = self._sight[zone.climate_id]
        if sight.mode is not None and not sight.reports_action:
            return VOTE_BASIS_MODE
        return VOTE_BASIS_ACTION

    def _vote_basis_row(self, zone: Zone, clock: float, wall: float) -> dict[str, Any]:
        """A hands-off action-basis room's vote_basis and vote_hold_until; else nothing.

        Display only. vote_hold_until is when a stopped head's held vote
        drops, as an ISO time, while that hold is running.
        """
        if not zone.hands_off or zone.vote_basis != VOTE_BASIS_ACTION:
            return {}
        basis = self._vote_basis(zone)
        left = (
            self._sight[zone.climate_id].hold_ends_in(clock, self.action_vote_hold)
            if basis == VOTE_BASIS_ACTION else None
        )
        return {
            "vote_basis": basis,
            "vote_hold_until": (
                None if left is None
                # To the second: the two clocks drift apart by microseconds
                # between computes, and the row should not.
                else dt_util.utc_from_timestamp(round(wall + left)).isoformat()
            ),
        }

    # -- lost heads: a stale, then an expired vote ----------------------------
    def _watch_head(self, climate_id: str, clock: float, wall: float) -> HeadSight:
        """Record what this compute sees of the head, and return its record.

        Only unavailable, or no state at all (the entity is gone), is out of
        sight: MXZ can neither read nor steer such a head. Every other state is
        a sighting, unknown included: the head is there and takes commands,
        and its vote is what it shows now (unknown asks for no direction, as
        it always has). A head that only reports nothing new is still in
        sight: nothing here times a quiet head.
        """
        sight = self._sight[climate_id]
        head = self.hass.states.get(climate_id)
        if head is None or head.state == STATE_UNAVAILABLE:
            sight.lose(clock, wall)
        else:
            sight.observe(head.state, head.attributes.get(ATTR_HVAC_ACTION), clock)
        return sight

    def _lost_since(self, climate_id: str) -> str | None:
        """When MXZ last lost sight of the head (or started, if never seen), or None."""
        wall = self._sight[climate_id].lost_wall
        return None if wall is None else dt_util.utc_from_timestamp(wall).isoformat()

    @staticmethod
    def _holds(status: str | None, safety: bool, cloud_out: bool = False) -> bool:
        """Whether a room's own demand and run still count in arbitration.

        Not once its head's vote has expired: a long-lost head must not hold
        the shared mode for its room forever while a sibling waits. Kept as in
        3.10.0, pending a maintainer decision: a room marked Safety priority with its
        sensor on (its demand is a safety want), and a tripped floor or
        ceiling (its own entry, never dropped here). Likewise not while a
        cloud head is out of MXZ's control (``cloud_out``): MXZ can't run it,
        so its room's call would only hold the outdoor unit for nothing. A
        Safety priority room still calls from its room sensor.
        """
        return safety or (status != VOTE_EXPIRED and not cloud_out)

    def _held_calls(self, plan: Plan) -> tuple[list[str], list[str]]:
        """Each room's (demand, engage) from a computed plan, as arbitration counts them.

        Replays _compute's rule from the plan's own rooms, so the dwell wakeup
        weighs exactly what that compute arbitrated.
        """
        demands: list[str] = []
        engages: list[str] = []
        for room in plan.rooms:
            if self._holds(room.vote_status, room.safety_priority, room.cloud_out):
                demands.append(room.demand)
                engages.append(room.engage)
            else:
                demands.append(DEMAND_NEUTRAL)
                engages.append(room.engage if room.floor_want is not None else ENGAGE_SATISFIED)
        return demands, engages

    def _sync_lost_timer(self) -> None:
        """Keep exactly one wakeup armed for the nearest lost-vote expiry.

        A head that stays unavailable emits nothing more, so without this its
        vote would only expire at the next unrelated recompute or the 15-min
        heartbeat. Like the freshness wakeup, nothing is captured: the
        callback only asks for a refresh, which re-reads every head as it is
        then, so a head back in sight by then is simply live again.
        """
        clock = self.clock()
        waits = [
            wait
            for sight in self._sight.values()
            if (wait := sight.expires_in(clock, self.lost_vote_ttl)) is not None
        ]
        # And the nearest end of a held action vote: a head that stopped
        # running and stays quiet emits nothing more either.
        waits += [
            wait
            for zone in self.zones
            if zone.hands_off and zone.vote_basis == VOTE_BASIS_ACTION
            and (
                wait := self._sight[zone.climate_id].hold_ends_in(
                    clock, self.action_vote_hold
                )
            ) is not None
        ]
        if not waits:
            self.clock.cancel("lost")
            return
        # The boundary itself, as for freshness: expiry is half-open, so the
        # refresh this fires reads the vote as expired.
        self.clock.call_later(min(waits), self._wake, name="lost")

    def _sync_settle_timer(self) -> None:
        """Keep one wakeup armed for the nearest end of a settle window.

        A released action-basis head that reported running just after its
        mode changed is not counted yet (logic.HeadSight.observe); if it keeps
        running it reports nothing new, so this wakes at the window's end and
        recomputes at once, not through the request cooldown. That compute
        sees it still running and counts it, or sees it stopped and counts
        nothing. Every compute re-arms this from what it saw, so a stop, a new
        mode change or a lost head cancels or moves it. Votes by mode and
        Safety priority votes never wait on it: they don't read the run.
        """
        if self.retired:
            self.clock.cancel("settle")
            return
        clock = self.clock()
        waits = [
            wait
            for zone in self.zones
            if zone.hands_off and zone.vote_basis == VOTE_BASIS_ACTION
            and self.hands_off_phase(zone) == "released"
            and (wait := self._sight[zone.climate_id].settles_in(clock)) is not None
        ]
        if not waits:
            self.clock.cancel("settle")
            return

        @callback
        def _fire() -> None:
            if not self.retired:
                self.hass.async_create_task(self.async_refresh_now())

        self.clock.call_later(min(waits), _fire, name="settle")

    @staticmethod
    def _zone_arbitration_pair(
        *, demand: str, vote: str | None, safety: bool, index: int,
        floor: str | None = None, dry: str | None = None, boost: float = 0.0,
    ) -> tuple[Demand, ...]:
        """One zone's (demand, vote[, dry][, floor]) as :class:`Demand` entries.

        ``boost`` is the room's Comfort priority. It goes on the demand, the
        vote and the dry vote, never on a floor or ceiling entry, and
        :func:`logic.arbitrate` ignores it on a Safety priority entry.

        Shared by ``_compute`` (live) and ``_sync_dwell_timer`` (replayed from
        the already-computed plan) so the priority-tier math lives in exactly
        one place. A dry head's cool (Allow Dry mode) is one more vote, always
        at PRIORITY_VOTE: the room's Safety priority sensor never lifts it. A
        tripped floor is a commanding entry: a freeze floor's heat at
        PRIORITY_FLOOR_HEAT, a ceiling's cool at PRIORITY_SAFETY (see
        :func:`logic.arbitrate`). It is independent of the room's own Safety
        priority sensor.
        """
        tier = PRIORITY_SAFETY if safety else None
        pair: tuple[Demand, ...] = (
            Demand(
                direction=demand if demand in (MODE_COOL, MODE_HEAT) else None,
                priority=(tier if tier is not None else PRIORITY_DEMAND, index),
                boost=boost,
            ),
            Demand(
                direction=vote,
                priority=(tier if tier is not None else PRIORITY_VOTE, index),
                read_only=True,
                boost=boost,
            ),
        )
        if dry is not None:
            pair = (
                *pair,
                Demand(
                    direction=dry, priority=(PRIORITY_VOTE, index), read_only=True,
                    boost=boost,
                ),
            )
        if floor not in (MODE_COOL, MODE_HEAT):
            return pair
        floor_tier = PRIORITY_FLOOR_HEAT if floor == MODE_HEAT else PRIORITY_SAFETY
        return (*pair, Demand(direction=floor, priority=(floor_tier, index)))

    def _zone_safety(self, zone: Zone) -> bool:
        """Whether this zone's Demand carries PRIORITY_SAFETY this tick.

        Reads ``zone.safety_priority_entity`` (a binary_sensor) regardless of
        enable/hands_off -- either a coordinated room's own demand or a
        hands-off room's vote may be marked safety. Unset, missing, or any
        state but exactly ``on`` (off, unavailable, unknown): False, i.e.
        fail-safe -- no information, no override.
        """
        if zone.safety_priority_entity is None:
            return False
        state = self.hass.states.get(zone.safety_priority_entity)
        return state is not None and state.state == STATE_ON

    def _zone_comfort_reading(self, zone: Zone, now: float) -> tuple[str | None, str | None, float | None]:
        """This room's Comfort priority entity as (domain, state, age seconds).

        (None, None, None) when none is set; a state of None when the entity
        doesn't exist. The age is by Home Assistant's own last_changed on the
        wall clock (a clock stepped back reads negative: fresh).
        """
        entity_id = zone.comfort_priority_entity
        if entity_id is None:
            return None, None, None
        domain = entity_id.split(".", 1)[0]
        state = self.hass.states.get(entity_id)
        if state is None:
            return domain, None, None
        return domain, state.state, now - state.last_changed.timestamp()

    def _zone_comfort(self, zone: Zone, now: float) -> tuple[float, str]:
        """This room's Comfort priority this compute: (boost, status).

        Read live, never stored. Any reading that isn't a usable "on" or a
        positive number is no boost (see logic.comfort_priority): it never
        blocks a decision or pushes the room down.
        """
        domain, state, age = self._zone_comfort_reading(zone, now)
        return comfort_priority(domain, state, age=age, stale_after=self.comfort_stale_after)

    def _sync_comfort_timer(self) -> None:
        """Keep one wakeup armed for the moment the nearest "on" goes stale.

        An entity that stays "on" emits nothing more, so without this its
        boost would drop only at the next unrelated recompute. Like the lost
        vote wakeup, nothing is captured: the callback only asks for a
        refresh, which re-reads every entity as it is then.
        """
        if self.retired or self.comfort_stale_after is None:
            self.clock.cancel("comfort")
            return
        now = dt_util.utcnow().timestamp()
        waits = []
        for zone in self.zones:
            domain, state, age = self._zone_comfort_reading(zone, now)
            wait = comfort_stale_in(
                domain, state, age=age, stale_after=self.comfort_stale_after
            )
            if wait is not None:
                waits.append(wait)
        if not waits:
            self.clock.cancel("comfort")
            return

        @callback
        def _fire() -> None:
            if not self.retired:
                self.hass.async_create_task(self.async_request_refresh())

        self.clock.call_later(min(waits), _fire, name="comfort")

    def restore_hands_off(self, slug: str) -> None:
        """The enable switch restored a released hands-off room (see switch.py)."""
        self._hands_off_released.add(slug)

    def hands_off_released(self, slug: str) -> bool:
        """Whether this room's park landed (what the enable switch persists)."""
        return slug in self._hands_off_released

    def set_hands_off_released(self, slug: str, released: bool) -> None:
        """A floor run takes a released room's head (not released) or gives
        it back to its person or yields it (released)."""
        if released:
            self._hands_off_released.add(slug)
        else:
            self._hands_off_released.discard(slug)

    @property
    def startup_quiet(self) -> bool:
        """Whether Home Assistant is still starting (see _on_startup_timer)."""
        return self._startup_quiet

    def stale_window(self, slug: str) -> float | None:
        """The freshness window this room's sensor ran out of, while it reads
        stale; None while it does not."""
        if self._health.get(slug) != HEALTH_STALE:
            return None
        return self._stale_window[slug]

    def vane_kick_running(self, climate_id: str) -> bool:
        """Whether a vane kick owns this head now."""
        return climate_id in self._vane_kicks

    # -- entity-driven mutations --------------------------------------------
    def zone_drift(self, zone: Zone) -> float:
        """The zone's re-engage drift: its own override, else the global.

        The symmetric value the room's drift number shows; what starts a run
        on each side is :meth:`zone_drift_sides`.

        Clamped to the unit profile's bounds at READ, same as the global — a
        restored or hand-edited value must not collapse the coast window or
        park a room degrees off target.
        """
        if zone.drift is None:
            return self.engage_deadband
        lo, hi = self._profile["engage_bounds"]
        return cast("float", min(max(float(zone.drift), lo), hi))

    def zone_drift_sides(self, zone: Zone) -> tuple[float, float]:
        """The drift in use per side: (below target before heat, above before cool).

        The one place that decides it; every start decision reads it. Per
        side, the first of these that is set wins: the room's own "cooler by"
        / "warmer by", the room's drift number (an explicit write, never the
        global it displays while following), the global "cooler by" /
        "warmer by", then Allowed drift. With nothing set this is
        (zone_drift, zone_drift), exactly the single drift of before.
        """
        (heat, _), (cool, _) = self.zone_drift_sides_from(zone)
        return heat, cool

    def zone_drift_sides_from(
        self, zone: Zone
    ) -> tuple[tuple[float, str], tuple[float, str]]:
        """:meth:`zone_drift_sides`, each side paired with where it came from.

        The precedence lives here only, so the plan's ``drift_heat_from`` /
        ``drift_cool_from`` name the same branch that chose the value:
        ``room_cooler_by`` / ``room_warmer_by`` (the room's own side),
        ``room`` (the room's drift number), ``cooler_by`` / ``warmer_by``
        (the global side), or ``allowed_drift`` (nothing else set).
        """

        def side(
            own: float | None, own_from: str, glob: float | None, glob_from: str
        ) -> tuple[float, str]:
            if own is not None:
                return own, own_from
            if zone.drift is not None:
                return self.zone_drift(zone), "room"
            if glob is not None:
                return glob, glob_from
            return self.engage_deadband, "allowed_drift"

        return (
            side(zone.drift_cooler_by, "room_cooler_by", self.drift_cooler_by, "cooler_by"),
            side(zone.drift_warmer_by, "room_warmer_by", self.drift_warmer_by, "warmer_by"),
        )

    def zone_side_targets(self, zone: Zone) -> tuple[float, float]:
        """The target each side measures from: (heat side, cool side).

        A dual-setpoint room's Heat to and Cool to; a single-target room's
        target, twice (so every decision computes exactly what it did before
        dual setpoints existed). A dual room whose numbers haven't restored
        yet reads its target until they do.
        """
        if zone.dual and zone.target_low is not None and zone.target_high is not None:
            return zone.target_low, zone.target_high
        return zone.target, zone.target

    def set_zone_range(self, zone: Zone, low: float, high: float) -> None:
        """Store a dual-setpoint room's validated range; nothing is checked here.

        The target becomes the midpoint, the engage latch resets exactly as a
        target write resets it, any restore note is spent, and the room's
        entities redraw. The caller requests the recompute.
        """
        zone.target_low, zone.target_high = low, high
        zone.target = (low + high) / 2
        zone.range_note = None
        self.reset_engage_latch(zone.slug)
        self.async_update_listeners()

    def dual_switches(self) -> list[Zone] | None:
        """The rooms an options save switched to or from a dual setpoint, if that's all it did.

        Compares the entry's config now with the config this coordinator was
        built from, leaving out each room's dual setpoint key and any empty
        vane, stage, or safety wiring (the same room either way). If anything
        else differs, or no room was switched, it returns None.
        """
        entry = self.config_entry
        new = entry_config(entry)
        if _without_dual(new) != _without_dual(self._built_conf):
            return None
        raw = new.get(CONF_ZONES)
        if not isinstance(raw, list) or len(raw) != len(self.zones):
            return None
        switched = [
            zone for zone, z in zip(self.zones, raw, strict=True)
            if zone.dual != (z.get(ZONE_DUAL_SETPOINT) is True)
        ]
        return switched or None

    async def async_switch_dual_in_place(self) -> bool:
        """Apply an options save that only switched rooms to or from a dual setpoint.

        The room's entities stay in place and available, so its thermostat's
        features change on one state write (HomeKit rebuilds its accessory
        only then; a reload passes through unavailable, and it doesn't). The
        outcome is the reload's: switched on, a range seeded around the target
        at the minimum gap and new heat-to and cool-to numbers; switched off,
        the target at the range's snapped midpoint and those numbers removed
        from the registry. Both give the room a fresh engage decision, and the
        plan is recomputed at once. Returns False, changing nothing, for any other
        save; the caller then reloads the entry exactly as before.
        """
        switched = self.dual_switches()
        if switched is None or self.add_range_numbers is None:
            return False
        entry = self.config_entry
        # The options flow's one-shot note is for a reload; this replaces it.
        handoffs = self.hass.data.get(DUAL_HANDOFF_KEY, {})
        for zone in switched:
            handoffs.pop((entry.entry_id, zone.climate_id), None)
            if zone.dual:
                if zone.target_low is not None and zone.target_high is not None:
                    zone.target = snapped_midpoint(
                        low=zone.target_low, high=zone.target_high, step=self.target_step
                    )
                zone.dual = False
                zone.target_low = zone.target_high = None
                zone.range_note = None
            else:
                zone.dual = True
                zone.target_low, zone.target_high = self._seeded_range(zone)
                self.finish_range_restore(zone)
            _LOGGER.info(
                "%s switched %s a dual setpoint in place",
                zone.name, "to" if zone.dual else "from",
            )
            # A fresh decision, never a run resumed from the head's mode:
            # what the reload's latch reset left (it ran while the room was
            # still resuming from startup).
            self._startup_resume.pop(zone.slug, None)
            self._engage_latch[zone.slug] = ""
        self._built_conf = entry_config(entry)
        # One state write per entity: the thermostat's new features, range,
        # and target together.
        self.async_update_listeners()
        registry = er.async_get(self.hass)
        for zone in switched:
            if zone.dual:
                self.add_range_numbers(zone)
                continue
            for suffix in DUAL_ZONE_ENTITY_SUFFIXES:
                if entity_id := registry.async_get_entity_id(
                    "number", DOMAIN, f"{entry.entry_id}_{zone.slug}_{suffix}"
                ):
                    registry.async_remove(entity_id)
        await self.async_user_changed(at_once=True)
        return True

    def offer_range_restore(
        self, zone: Zone, is_low: bool, restored: float | None
    ) -> None:
        """One edge's restored value (None: nothing usable); the second settles both.

        The pair is decided as a whole, so a range never mixes one restored
        edge with one seeded edge: both restored edges, unless the room was
        just switched on (the options flow's handoff) or either is missing;
        then a range seeded around the (already restored) target at the
        minimum gap, never one an earlier opt-in left in the restore data.
        :meth:`finish_range_restore` then re-checks it, and the room's
        entities redraw.
        """
        offers = self._range_offers.setdefault(zone.slug, {})
        offers[is_low] = restored
        if len(offers) == 2:
            self._settle_range(zone)

    def _settle_range(self, zone: Zone) -> None:
        """Decide a dual room's pair from the edges offered so far (see above)."""
        offers: dict[bool, Any] = self._range_offers.pop(zone.slug, {})
        fresh = zone.handoff is not None and zone.handoff.get("seed_range") is True
        if not fresh and offers.get(True) is not None and offers.get(False) is not None:
            low, high = offers[True], offers[False]
        else:
            low, high = self._seeded_range(zone)
        zone.target_low, zone.target_high = float(low), float(high)
        self.finish_range_restore(zone)
        self.async_update_listeners()

    def _seeded_range(self, zone: Zone) -> tuple[float, float]:
        """A new range: centered on the room's target at the minimum gap, inside its bounds."""
        bounds = self.heads.grid.target_bounds(zone.climate_id)
        try:
            return shift_range(
                width=self.dual_min_gap, center=zone.target,
                bounds=bounds, step=self.target_step,
            )
        except RangeError:
            return bounds  # a head band narrower than the gap

    def finish_range_restore(self, zone: Zone) -> None:
        """Re-check a dual room's range once both of its numbers have restored.

        Each edge is clamped into the head's current bounds. If that leaves
        the pair narrower than the minimum gap, the range is re-centered at
        the minimum gap around its midpoint (sliding inside the bounds), and
        the room's detail says so once, until the range is next written.
        """
        if zone.target_low is None or zone.target_high is None:
            return
        lo, hi = self.heads.grid.target_bounds(zone.climate_id)
        low = min(max(zone.target_low, lo), hi)
        high = min(max(zone.target_high, lo), hi)
        if high - low < self.dual_min_gap - 1e-9:
            was = (zone.target_low, zone.target_high)
            try:
                low, high = shift_range(
                    width=self.dual_min_gap, center=(low + high) / 2,
                    bounds=(lo, hi), step=self.target_step,
                )
            except RangeError:
                low, high = lo, hi  # a head band narrower than the gap
            zone.range_note = (
                f"Restored heat to {was[0]:g} and cool to {was[1]:g} didn't fit"
                f" this head's limits {lo:g} to {hi:g} with the minimum gap,"
                f" so the range was re-centered at {low:g} to {high:g}."
            )
            _LOGGER.warning("%s: %s", zone.name, zone.range_note)
        zone.target_low, zone.target_high = low, high
        zone.target = (low + high) / 2

    def reset_engage_latch(self, slug: str) -> None:
        """Forget a zone's engage latch (its target changed -> fresh decision).

        An enabled, live room re-seeds from its head's mode, preserving existing
        retargeting behavior. A disabled room or a still-unresolved startup gets
        a fresh decision; new intent must not revive an old pending run.
        """
        for zone in self.zones:
            if zone.slug == slug:
                self.drop_head_change(zone.climate_id)
        if slug in self._startup_resume or not self.coordinator_enable or not any(
            zone.slug == slug and zone.enable for zone in self.zones
        ):
            self._startup_resume.pop(slug, None)
            self._engage_latch[slug] = ""
        else:
            self._engage_latch.pop(slug, None)

    async def async_user_changed(self, *, at_once: bool = False) -> None:
        """A helper entity changed by the user -> recompute + act.

        ``at_once`` skips the request debounce (a room switched to or from a
        dual setpoint in place is recomputed at once, as a reload would be).

        The room surfaces (each thermostat, the plan sensor) re-render first,
        so a room's new enable, target or range shows at once rather than
        when the plan that follows finishes: a dashboard card showing the old
        value invites a second tap that undoes the first. Only a re-render,
        no decision; the plan itself is unchanged.
        """
        if not self.coordinator_enable:
            self.deliveries.retire()
            await self._async_retire_vane_kicks()
        self.redraw_rooms()
        if at_once:
            await self.async_refresh()
        else:
            await self.async_request_refresh()

    async def async_select_shared_mode(self, mode: str) -> None:
        """A person asked for a shared direction (stamps the hysteresis clock).

        The request is then handled by the existing arbitration, unchanged: a
        changed direction stamps the mode-flip clock exactly as an automatic
        flip does, and what happens once that dwell elapses is arbitration's
        answer, not a rule of this method's. Nothing here gives the request an
        expiry, a renewal, a hold or any storage of its own.

        The choice picks a direction and nothing else. It does not turn the
        coordinator on, wake a disabled room, or lift a lockout, the eco band, a
        standby hold or a setpoint clamp.
        """
        if mode != self.current_shared_mode:
            self.current_shared_mode = mode
            self._mode_by_votes = False
            self._stamp_mode_change()
            self._record_mode()
        await self.async_request_refresh()

    # -- seasonal changeover (optional, from local weather) -----------------
    @callback
    def _on_changeover_change(self, _event: Event[Any]) -> None:
        """The changeover weather entity updated -> re-evaluate the season."""
        self.hass.async_create_task(self._evaluate_changeover())

    @callback
    def _on_changeover_timer(self, _now: Any) -> None:
        """Hourly changeover re-read (forecasts refresh slowly)."""
        self.hass.async_create_task(self._evaluate_changeover())

    async def _evaluate_changeover(self) -> None:
        """Read local weather and auto-drive the heat/cool lockout switches."""
        if not self.changeover_entity:
            return
        outdoor_high = await self._read_outdoor_high()
        heat_lock, cool_lock = season_lockouts(
            outdoor_high=outdoor_high,
            heat_above=self.changeover_heat_above,
            cool_below=self.changeover_cool_below,
        )
        await self._drive_lockout(KEY_HEAT_LOCKOUT, heat_lock)
        await self._drive_lockout(KEY_COOL_LOCKOUT, cool_lock)

    async def _read_outdoor_high(self) -> float | None:
        """Local daily-high °F: a weather entity's forecast, or a temp entity's state."""
        entity_id = self.changeover_entity
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in UNAVAILABLE_STATES:
            return None
        if cast("str", entity_id).startswith("weather."):
            try:
                resp: Any = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": entity_id, "type": "daily"},
                    blocking=True,
                    return_response=True,
                )
                forecast = (resp or {}).get(entity_id, {}).get("forecast") or []
                return float(forecast[0]["temperature"])
            except (HomeAssistantError, KeyError, IndexError, ValueError, TypeError):
                _LOGGER.warning(
                    "MXZ changeover: could not read a daily forecast from %s", entity_id
                )
                return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    async def _drive_lockout(self, key: str, desired: bool) -> None:
        """Set a lockout switch to ``desired`` (idempotent — only on a change)."""
        registry = er.async_get(self.hass)
        eid = registry.async_get_entity_id(
            "switch", DOMAIN, f"{self.config_entry.entry_id}_{key}"
        )
        if eid is None:
            return
        state = self.hass.states.get(eid)
        is_on = state is not None and state.state == "on"
        if is_on == desired:
            return
        await self.hass.services.async_call(
            "switch",
            "turn_on" if desired else "turn_off",
            {"entity_id": eid},
            blocking=True,
        )

    # -- external inhibit / low-power standby --------------------------------
    def _read_inhibit(self) -> bool:
        """True iff the watched entity is in its active state.

        A missing entity, or one reading unavailable/unknown, is NOT held: fail
        toward normal coordination — a stuck or dropped sensor must never park
        the house. A genuine "off" from an inverted grid sensor is a real state,
        not a dropout, so it still holds when inhibit_active_state == "off".
        """
        if not self.inhibit_entity:
            return False
        st = self.hass.states.get(self.inhibit_entity)
        if st is None or st.state in UNAVAILABLE_STATES:
            return False
        return st.state == self.inhibit_active_state

    @callback
    def _on_inhibit_change(self, _event: Event[Any]) -> None:
        """The watched inhibit entity changed -> re-evaluate the hold."""
        self.hass.async_create_task(self._evaluate_inhibit())

    @callback
    def _on_safety_change(self, _event: Event[Any]) -> None:
        """A zone's safety-priority sensor changed -> recompute now."""
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_comfort_change(self, _event: Event[Any]) -> None:
        """A zone's Comfort priority entity changed -> recompute now."""
        self.hass.async_create_task(self.async_request_refresh())

    async def _evaluate_inhibit(self) -> None:
        """Re-read the inhibit entity; act only on a change of hold state.

        On the RELEASE edge, reseed the fan latch (the fan the hold left behind
        must not read as a manual departure) before re-applying the live plan.
        """
        new = self._read_inhibit()
        if new == self.inhibited:
            return
        released = self.inhibited and not new
        self.inhibited = new
        if new:
            await self._async_retire_vane_kicks()
        elif released:
            self.fan.reseed_after_standby(zone.climate_id for zone in self.zones)
        await self.async_request_refresh()


# The drift_*_from values that come from the room itself (its own side, or its
# drift number): either one makes drift_source "room".
ROOM_DRIFT_SOURCES = frozenset({"room", "room_cooler_by", "room_warmer_by"})

# The per-room start-point keys, in the plan's room rows and on the room
# thermostat (read from the plan, never recomputed there).
START_POINT_KEYS = (
    "drift_source",
    "drift_heat_from",
    "drift_cool_from",
    "heat_start",
    "cool_start",
    "heat_demand_below",
    "cool_demand_above",
    "start_points_mode",
    "start_points_lockouts",
)


def _start_points(
    zone: Zone,
    common: dict[str, Any],
    *,
    sides: tuple[float, float],
    demand_bands: tuple[float, float],
    sources: tuple[str, str],
    targets: tuple[float, float],
) -> dict[str, Any]:
    """One room's start points: the edges its engage and demand calls compare to.

    ``common``, ``sides``, ``demand_bands``, and ``targets`` (each side's
    target) are exactly what this compute passed to room_call /
    engage_with_latch, and :func:`call_edges` is the one
    place those turn into edges, so the numbers shown are the numbers used.
    The start edges are where a coasting room starts a run (a running room
    still runs all the way to its target). A disabled room makes no call, so
    it has no edges; a missing reading doesn't move them.
    """
    eco = common["eco"]
    points: dict[str, Any] = {
        "drift_source": "room" if ROOM_DRIFT_SOURCES & set(sources) else "global",
        "drift_heat_from": sources[0],
        "drift_cool_from": sources[1],
        "heat_start": None,
        "cool_start": None,
        "heat_demand_below": None,
        "cool_demand_above": None,
        "start_points_mode": None,
        "start_points_lockouts": [],
    }
    if not zone.enable:
        return points
    edges = (
        *call_edges(
            target=zone.target, heat_band=sides[0], cool_band=sides[1],
            heat_target=targets[0], cool_target=targets[1], **common,
        ),
        *call_edges(
            target=zone.target, heat_band=demand_bands[0], cool_band=demand_bands[1],
            heat_target=targets[0], cool_target=targets[1], **common,
        ),
    )
    points.update(
        {
            # Rounded for display only: target - threshold can carry binary
            # noise (62 - 4.02 is 57.980000000000004). Six places keep any
            # setting entered to six decimals or fewer exact; finer ones are
            # shown to within a millionth of a degree. The plan's other
            # temperatures are entered values, shown as stored.
            **{
                key: round(edge, 6)
                for key, edge in zip(
                    ("heat_start", "cool_start", "heat_demand_below", "cool_demand_above"),
                    edges, strict=True,
                )
            },
            "start_points_mode": "eco" if eco else "comfort",
            # Eco ignores the lockouts, so they move nothing while it's on.
            "start_points_lockouts": [] if eco else [
                side for side, on in (
                    (MODE_HEAT, common["heat_lockout"]),
                    (MODE_COOL, common["cool_lockout"]),
                ) if on
            ],
        }
    )
    return points


def _zone_name(z: dict[str, Any], index: int) -> str:
    """A stored room's name, or the one its slug gives it."""
    return z.get(ZONE_NAME) or zone_slug(index).replace("_", " ").title()


def _stored_number(value: Any, label: str) -> float | None:
    """A stored drift read by ``float()`` (a numeric string counts), else None.

    None (blank) is None quietly. Anything else that isn't a finite number is
    None too, logged once per load naming ``label``, never a failed setup.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if math.isfinite(number):
        return number
    _LOGGER.warning(
        "MXZ: the saved %s is %r, which isn't a number; ignoring it."
        " Configure names it so it can be fixed",
        label,
        value,
    )
    return None


def _drift_side(value: Any, low: float, high: float, label: str) -> float | None:
    """A stored uneven-drift side clamped into ``[low, high]``, or None (blank).

    Read like Allowed drift; anything that isn't a finite number is blank.
    """
    number = _stored_number(value, label)
    return None if number is None else min(max(number, low), high)


def _lost_vote_minutes(value: Any) -> float:
    """A stored lost-vote time limit in minutes, clamped into the UI's bounds.

    Blank (None) is the default; anything that isn't a finite number reads as
    blank rather than failing setup.
    """
    low, high = LOST_VOTE_TTL_BOUNDS
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_LOST_VOTE_TTL)
    if not math.isfinite(number):
        return float(DEFAULT_LOST_VOTE_TTL)
    return min(max(number, float(low)), float(high))


def _comfort_stale_seconds(value: Any) -> float | None:
    """A stored Comfort priority stale limit (hours) in seconds; 0 is None (no limit).

    Blank (None) is the default; anything that isn't a finite number reads as
    blank rather than failing setup. A stored value is clamped into the UI's
    bounds, so a negative one reads as 0.
    """
    low, high = COMFORT_STALE_HOURS_BOUNDS
    try:
        hours = float(value)
    except (TypeError, ValueError):
        hours = float(DEFAULT_COMFORT_STALE_HOURS)
    if isinstance(value, bool) or not math.isfinite(hours):
        hours = float(DEFAULT_COMFORT_STALE_HOURS)
    hours = min(max(hours, float(low)), float(high))
    return None if hours == 0 else 3600.0 * hours


def _finite(value: Any) -> float | None:
    """A stored number as a finite float, else None (a bool is not a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None
