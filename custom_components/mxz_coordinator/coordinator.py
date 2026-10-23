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
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_ON, UnitOfTemperature
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_start
from homeassistant.helpers.update_coordinator import (
    REQUEST_REFRESH_DEFAULT_COOLDOWN,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .capabilities import head_has_fan_auto
from .const import (
    BAND_DRIFT_DELAY,
    BANNED_MODES,
    CHANGEOVER_INTERVAL_MINUTES,
    CONF_CHANGEOVER_COOL_BELOW,
    CONF_CHANGEOVER_ENTITY,
    CONF_CHANGEOVER_HEAT_ABOVE,
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    CONF_COIL_DRY_MINUTES,
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
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_PRIMARY_STAGE,
    CONF_PRIMARY_VANE_HORIZONTAL,
    CONF_PRIMARY_VANE_VERTICAL,
    CONF_RESTING_MODE_BIAS,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    CONF_SECONDARY_STAGE,
    CONF_SECONDARY_VANE_HORIZONTAL,
    CONF_SECONDARY_VANE_VERTICAL,
    CONF_ZONES,
    DEFAULT_COIL_DRY_MINUTES,
    DEFAULT_FAN_BOOST_ENABLE,
    DEFAULT_FAN_BOOST_MAX,
    DEFAULT_IDLE_ACTION,
    DEFAULT_INHIBIT_ACTION,
    DEFAULT_INHIBIT_ACTIVE_STATE,
    DEFAULT_MODE_HYSTERESIS,
    DEFAULT_RESTING_MODE_BIAS,
    DEMAND_NEUTRAL,
    DOMAIN,
    DUAL_HANDOFF_KEY,
    ENGAGE_SATISFIED,
    EVENT_RECOMPUTE,
    EVIDENCE_HA_WRITE,
    FAN_AUTO,
    FAN_LADDER,
    FLOOR_NOTICE_SECONDS,
    FLOOR_STALL_SECONDS,
    FLOOR_YIELD_SECONDS,
    HEALTH_AWAITING,
    HEALTH_ELIGIBLE,
    HEALTH_HEALTHY,
    HEALTH_INVALID,
    HEALTH_REPORT_SENSITIVE,
    HEALTH_STALE,
    HEALTH_UNHEALTHY,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
    IDLE_SEED_MIN_ENTRY_AGE,
    INHIBIT_ACTION_ECO,
    INHIBIT_ACTION_FAN_ONLY,
    INHIBIT_ACTION_OFF,
    KEY_COOL_LOCKOUT,
    KEY_HEAT_LOCKOUT,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
    OFF_WHILE_ENABLED_DELAY,
    STARTUP_RECOVER_DELAY,
    UNAVAILABLE_STATES,
    VANE_KICK_APPLY,
    VANE_KICK_RETIRE_TIMEOUT,
    VANE_KICK_SPINUP,
    ZONE_CLIMATE,
    ZONE_DRIFT_COOLER_BY,
    ZONE_DRIFT_WARMER_BY,
    ZONE_DUAL_SETPOINT,
    ZONE_EVIDENCE_BASIS,
    ZONE_FLOOR_HIGH,
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
    ZONE_MAX_AGE,
    ZONE_NAME,
    ZONE_REPORT_INTERVAL,
    ZONE_SAFETY_PRIORITY_ENTITY,
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
from .logic import (
    PRIORITY_DEMAND,
    PRIORITY_FLOOR_HEAT,
    PRIORITY_SAFETY,
    PRIORITY_VOTE,
    Demand,
    FloorRun,
    RangeError,
    arbitrate,
    call_edges,
    engage_with_latch,
    evidence_contract,
    fan_for_delta,
    floor_call,
    floor_progress,
    freshness_window,
    head_action,
    range_delta,
    room_call,
    sample_evidence,
    season_lockouts,
    sensor_health,
    setpoints,
    shift_range,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


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
    # Optional binary_sensor (ZONE_SAFETY_PRIORITY_ENTITY): "on" marks this
    # zone's Demand PRIORITY_SAFETY for this tick, regardless of whether its
    # direction comes from room_call() or a hands-off vote. Static wiring,
    # same tier as climate_id/sensor_id, not a live-adjustable number.
    safety_priority_entity: str | None = None
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


def _parse_zones(
    conf: dict[str, Any],
    target_default: float,
    engage_bounds: tuple[float, float],
) -> list[Zone]:
    """Build the ordered Zone list from entry config.

    Prefers the v2 ``zones`` list; falls back to the legacy flat
    primary_*/secondary_* keys (pre-migration entries and old tests). Legacy
    flat vane keys still override zones 0/1 when present (v2.9 options-flow
    overrides live there on migrated entries). ``engage_bounds`` clamps a
    room's own uneven drift, exactly as it clamps the global sides.
    """
    raw = conf.get(CONF_ZONES)
    if not raw:
        raw = [
            {
                ZONE_NAME: "Primary",
                ZONE_CLIMATE: conf[CONF_PRIMARY_CLIMATE],
                ZONE_SENSOR: conf[CONF_PRIMARY_SENSOR],
                ZONE_VANE_VERTICAL: conf.get(CONF_PRIMARY_VANE_VERTICAL),
                ZONE_VANE_HORIZONTAL: conf.get(CONF_PRIMARY_VANE_HORIZONTAL),
                ZONE_STAGE_SENSOR: conf.get(CONF_PRIMARY_STAGE),
            },
            {
                ZONE_NAME: "Secondary",
                ZONE_CLIMATE: conf[CONF_SECONDARY_CLIMATE],
                ZONE_SENSOR: conf[CONF_SECONDARY_SENSOR],
                ZONE_VANE_VERTICAL: conf.get(CONF_SECONDARY_VANE_VERTICAL),
                ZONE_VANE_HORIZONTAL: conf.get(CONF_SECONDARY_VANE_HORIZONTAL),
                ZONE_STAGE_SENSOR: conf.get(CONF_SECONDARY_STAGE),
            },
        ]
    zones = [
        Zone(
            index=i,
            slug=zone_slug(i),
            name=z.get(ZONE_NAME) or zone_slug(i).replace("_", " ").title(),
            climate_id=z[ZONE_CLIMATE],
            sensor_id=z[ZONE_SENSOR],
            vane_vertical_id=z.get(ZONE_VANE_VERTICAL) or None,
            vane_horizontal_id=z.get(ZONE_VANE_HORIZONTAL) or None,
            stage_sensor_id=z.get(ZONE_STAGE_SENSOR) or None,
            target=target_default,
            hands_off=z.get(ZONE_HANDS_OFF) is True,
            safety_priority_entity=z.get(ZONE_SAFETY_PRIORITY_ENTITY) or None,
            report_interval=z.get(ZONE_REPORT_INTERVAL),
            max_age=z.get(ZONE_MAX_AGE),
            startup_grace=z.get(ZONE_STARTUP_GRACE),
            evidence_basis=z.get(ZONE_EVIDENCE_BASIS),
            sample_timestamp_attribute=z.get(ZONE_SAMPLE_TIMESTAMP_ATTR),
            sample_sequence_attribute=z.get(ZONE_SAMPLE_SEQUENCE_ATTR),
            floor_low=_finite(z.get(ZONE_FLOOR_LOW)),
            floor_high=_finite(z.get(ZONE_FLOOR_HIGH)),
            drift_cooler_by=_drift_side(z.get(ZONE_DRIFT_COOLER_BY), *engage_bounds),
            drift_warmer_by=_drift_side(z.get(ZONE_DRIFT_WARMER_BY), *engage_bounds),
            dual=z.get(ZONE_DUAL_SETPOINT) is True,
        )
        for i, z in enumerate(raw)
    ]
    # Legacy flat vane overrides (options-flow writes on migrated entries).
    _legacy_vanes = (
        (0, CONF_PRIMARY_VANE_VERTICAL, CONF_PRIMARY_VANE_HORIZONTAL),
        (1, CONF_SECONDARY_VANE_VERTICAL, CONF_SECONDARY_VANE_HORIZONTAL),
    )
    for idx, vkey, hkey in _legacy_vanes:
        if idx < len(zones):
            if conf.get(vkey):
                zones[idx].vane_vertical_id = conf[vkey]
            if conf.get(hkey):
                zones[idx].vane_horizontal_id = conf[hkey]
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
# Coordinator
# ---------------------------------------------------------------------------
class MXZCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the decision state, drives the heads, and self-heals drift."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise from the config entry (data) and options (tunables)."""
        super().__init__(
            hass,
            _LOGGER,
            name="MXZ Plan",
            update_interval=None,  # all updates are event/heartbeat-driven, not polled
            config_entry=entry,
        )
        conf: dict[str, Any] = {**entry.data, **entry.options}

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
        self.dual_min_gap: float = self._profile["dual_min_gap"]
        handoffs: dict[tuple[str, str], dict[str, Any]] = hass.data.setdefault(
            DUAL_HANDOFF_KEY, {}
        )
        for zone in self.zones:
            zone.handoff = handoffs.pop((entry.entry_id, zone.climate_id), None)
        # A dual room's restored edges until both have arrived (see
        # offer_range_restore): {slug: {is_low: value or None}}.
        self._range_offers: dict[str, dict[bool, float | None]] = {}
        self.notify_service: str | None = conf.get(CONF_NOTIFY_SERVICE) or None

        # Unit-dependent tunables fall back to the system-unit profile default;
        # unit-free ones (hysteresis seconds, resting bias, fan boost) keep their
        # plain DEFAULT_*.
        self.demand_threshold: float = conf.get(
            CONF_DEMAND_THRESHOLD, _defaults[CONF_DEMAND_THRESHOLD]
        )
        # Re-engage drift: clamped to the unit profile's sane range so a
        # hand-edited or legacy value can't collapse the coast window or park
        # rooms degrees off target. The UI enforces the same bounds.
        _emin, _emax = self._profile["engage_bounds"]
        self.engage_deadband: float = min(
            max(float(conf.get(CONF_ENGAGE_DEADBAND, _defaults[CONF_ENGAGE_DEADBAND])), _emin),
            _emax,
        )
        # Uneven drift: None (no key) means Allowed drift on that side. A
        # stored value is clamped into the same bounds, for the same reason.
        # One that isn't a finite number reads as blank: setup must not fail
        # on it, and the Configure menu names it so it can be fixed.
        self.drift_cooler_by: float | None = _drift_side(
            conf.get(CONF_DRIFT_COOLER_BY), _emin, _emax
        )
        self.drift_warmer_by: float | None = _drift_side(
            conf.get(CONF_DRIFT_WARMER_BY), _emin, _emax
        )
        self.hysteresis: int = conf.get(CONF_MODE_HYSTERESIS, DEFAULT_MODE_HYSTERESIS)
        self.eco_cool_max: float = conf.get(
            CONF_ECO_COOL_MAX, _defaults[CONF_ECO_COOL_MAX]
        )
        self.eco_heat_min: float = conf.get(
            CONF_ECO_HEAT_MIN, _defaults[CONF_ECO_HEAT_MIN]
        )
        self.clamp_min: float = float(conf.get(CONF_CLAMP_MIN, _defaults[CONF_CLAMP_MIN]))
        self.clamp_max: float = float(conf.get(CONF_CLAMP_MAX, _defaults[CONF_CLAMP_MAX]))
        self.resting_mode_bias: str = conf.get(
            CONF_RESTING_MODE_BIAS, DEFAULT_RESTING_MODE_BIAS
        )
        self.heat_lockout_floor: float = conf.get(
            CONF_HEAT_LOCKOUT_FLOOR, _defaults[CONF_HEAT_LOCKOUT_FLOOR]
        )
        self.cool_lockout_ceiling: float = conf.get(
            CONF_COOL_LOCKOUT_CEILING, _defaults[CONF_COOL_LOCKOUT_CEILING]
        )
        # Optional local-weather seasonal changeover (auto-drives the lockouts).
        self.changeover_entity: str | None = conf.get(CONF_CHANGEOVER_ENTITY) or None
        self.changeover_heat_above: float = conf.get(
            CONF_CHANGEOVER_HEAT_ABOVE, _defaults[CONF_CHANGEOVER_HEAT_ABOVE]
        )
        self.changeover_cool_below: float = conf.get(
            CONF_CHANGEOVER_COOL_BELOW, _defaults[CONF_CHANGEOVER_COOL_BELOW]
        )
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
        self._dry_timers: dict[str, Any] = {}
        # Delta-proportional fan boost (overrides the firmware's weak "auto").
        self.fan_boost_enable: bool = bool(
            conf.get(CONF_FAN_BOOST_ENABLE, DEFAULT_FAN_BOOST_ENABLE)
        )
        self.fan_boost_max: str = conf.get(CONF_FAN_BOOST_MAX, DEFAULT_FAN_BOOST_MAX)
        self._fan_idx: dict[str, int] = {}  # per-head ladder index (hysteresis state)

        # Manual fan-speed latch ("deliberate departure"). Fan boost is normally
        # the sole fan-writer, but a user who reaches in and picks a speed should
        # keep it: once a head's observed fan_mode is neither "auto" nor a token
        # we commanded, that head LATCHES and the coordinator makes NO fan writes
        # to it at all — not ladder speeds, and crucially not the return-to-"auto"
        # on satisfied/fan_only/eco (which would steal the user's pick and self-
        # unlatch). The latch releases only when the head is observed back at
        # "auto" (the user handing control back). Per-head decision memory, like
        # _fan_idx: _fan_cmd is the last commanded OR adopted observation;
        # _fan_prev the prior baseline (an echo can briefly still read as the prior
        # value — a mismatch is only a user departure if it differs from BOTH).
        self._fan_cmd: dict[str, str] = {}
        self._fan_prev: dict[str, str] = {}
        self._fan_latched: dict[str, bool] = {}
        # Pre-restart latch truth restored by the Fan-auto switch
        # (RestoreEntity), consumed once at the first seed observation per
        # head. Reconciliation always reads the token from the OBSERVED head
        # state; explicit pending ON is a separate saved instruction. Absent or stale
        # restore data -> plain seeding (below).
        self._fan_restore: dict[str, bool] = {}
        # Explicit ON with no current speed is an instruction, not evidence
        # that the first speed subsequently reported was a manual departure.
        self._fan_on_pending: set[str] = set()
        # The existing Fan-auto entities publish clean state and extra data
        # from the same synchronous memory change, even mid-apply. This is
        # observation/intent, not evidence that a service reached hardware.
        self._fan_state_writers: dict[str, Callable[[], None]] = {}
        # A restore record that existed but could not be used (stale or
        # malformed), set by the Fan-auto switch. No record at all — a fresh
        # install or newly added room — is not a problem and adds no reason.
        self._fan_restore_problem: dict[str, str] = {}

        # Engage latch (decision state, like _fan_idx): "" = coasting, cool|heat
        # = mid-run toward target (the head may still be parked in fan_only by a
        # shared-mode mismatch; the run resumes when the mode returns). Seeded
        # from the head only with restored zone AND coordinator ON intent.
        # Construction and first live enable have no prior run to adopt.
        self._engage_latch: dict[str, str] = {zone.slug: "" for zone in self.zones}
        self._restored_coordinator_on = False
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
        # Freeze floor / overheat ceiling, for a disabled room that sets one.
        # Per room: the tripped run's progress record; whether the last compute
        # found MXZ driving its head in the floor's direction (the no-progress
        # clock runs only then) and when that compute ran, on the monotonic
        # clock (in memory only); a hands-off room's yield to a person
        # (direction, wall-clock time: it is stored and shown); and what the
        # plan shows.
        self._floor_runs: dict[str, FloorRun] = {}
        self._floor_driving: dict[str, bool] = {}
        self._floor_eval_ts: dict[str, float] = {}
        self._floor_yield: dict[str, tuple[str, float]] = {}
        self._floor_view: dict[str, tuple[str, str | None]] = {}
        # A hands-off run MXZ took over from a person already running the
        # head in the floor's direction: their (mode, low, high), given back
        # when the run ends (P3); a hand-back for the next apply to send, as
        # (theirs, which try, the head as MXZ left it: None on the first try);
        # one in flight, as (theirs, which try, the head as MXZ left it, its
        # delivery); and a refused one's notice, kept while the head still
        # shows MXZ's command, as (message, that head). While MXZ runs a head
        # over a person's own run: the setpoints it last queued for it, as the
        # head should show them, and what the head showed then (P15).
        self._floor_prior: dict[str, tuple[str, float, float]] = {}
        self._floor_expect: dict[str, tuple[tuple[float, float], tuple | None]] = {}
        self._floor_handback: dict[str, tuple[tuple[str, float, float], int, tuple | None]] = {}
        self._floor_handing_back: dict[
            str, tuple[tuple[str, float, float], int, tuple, dict[str, Any]]
        ] = {}
        self._floor_handback_note: dict[str, tuple[str, tuple]] = {}
        # One notice per episode: a blind room, a rejected floor command.
        self._floor_blind_noticed: set[str] = set()
        self._floor_reject_noticed: set[str] = set()
        # The rate limit (P5): when each room's last floor notice went out,
        # on the monotonic clock, and the notices held back since (each
        # logged once). In memory only.
        self._floor_notice_at: dict[str, float] = {}
        self._floor_held_back: dict[str, set[str]] = {}
        # A trip notice the limit held back, sent once the hour is up if its
        # run is still going then (P8).
        self._floor_trip_held: dict[str, str] = {}
        # A head that rejected MXZ's floor command and has accepted none
        # since: its error. The plan then shows held, not heat/cool.
        self._floor_rejecting: dict[str, str] = {}
        # The clear line's margin is a constant, the unit's default demand
        # threshold; a step is the unit's setpoint step (1 °F / 0.5 °C).
        self._floor_margin: float = float(_defaults[CONF_DEMAND_THRESHOLD])
        # Inert until async_setup: the seed compute below runs before the
        # switches restore, with every room still reading disabled.
        self._floor_live = False

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
        self._retired: bool = False

        # Helper values (owned by the switch/select entities; seeded on
        # restore, mutated on user action). Kill-switch defaults OFF for safety.
        # Per-zone target/enable live on the Zone objects.
        self.coordinator_enable: bool = False
        self.eco_idle: bool = False
        self.heat_lockout: bool = False
        self.cool_lockout: bool = False
        self.current_shared_mode: str = MODE_COOL  # restored by the select entity
        # Ordinal of explicit shared-mode requests. It carries no direction, no
        # clock and no persistence: it exists so an apply that started before a
        # person's request cannot write its older plan back over that request
        # (see the writeback at the end of _apply).
        self._selection_seq: int = 0

        # Hysteresis is armed from startup: a mode flip must wait out the dwell
        # even right after setup/restart (#6 — 0.0 made the first flip always
        # allowed and the plan sensor report a ~56,000-year dwell).
        # Monotonic, not wall-clock: an NTP step correction must not be able to
        # transiently defeat or prematurely satisfy the dwell gate.
        self._last_mode_change_ts: float = time.monotonic()
        # One-shot wakeup at dwell expiry, armed only while the dwell is
        # holding a flip back (see _sync_dwell_timer). Retirement is one-way
        # for this incarnation: a reload builds a new coordinator.
        self._dwell_timer: Any | None = None
        self._dwell_retired = False
        # One-shot wakeup after a compute disengaged an engage latch, so a room
        # that coasts through a direction flip is re-evaluated on a head that
        # echoes nothing back (see _sync_coast_timer). Retirement is one-way for
        # this incarnation, exactly as for the dwell wakeup above.
        self._coasted = False
        self._coast_timer: Any | None = None
        self._coast_retired = False
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
        self._started_ts: float = dt_util.utcnow().timestamp()
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
        self._fresh_timer: Any | None = None
        self._fresh_retired = False
        self._arm_freshness(self._started_ts)
        self._unsubs: list[Any] = []
        self._heal_timers: dict[tuple[str, str], Any] = {}
        # State callbacks may run inside a service call or one loop turn later.
        # Keep the current decision, while each delivery slot separately owns
        # its head's applicable action until those pending reports are dispatched.
        self._applying_plan: dict[str, Any] | None = None
        # Refreshes serialize decisions, not external completion. An input can
        # release the current wait; each head retains its applicable action until
        # its real handler returns. A deferred head retains only current-input
        # work, never a queue of obsolete plans.
        self._needs_legacy_refresh_guard = not hasattr(
            self._debounced_refresh, "_execute_lock_owner"
        )
        self._refresh_lock = asyncio.Lock()
        self._plan_lock = self._refresh_lock if self._needs_legacy_refresh_guard else asyncio.Lock()
        self._refresh_generation = 0
        self._delivery_followup = False
        self._delivery_listeners: set[Callable[[], None]] = set()
        self._delivery_error: Exception | None = None
        self._refresh_pending = False
        self._refresh_wake = asyncio.Event()
        self._refresh_followup_timer: Any | None = None
        self._deliveries: dict[str, dict[str, Any]] = hass.data.setdefault(
            f"{DOMAIN}_deliveries", {}
        )
        self._head_locks: dict[str, asyncio.Lock] = hass.data.setdefault(
            f"{DOMAIN}_head_locks", {}
        )
        self._vane_cleanup_pending: dict[str, asyncio.Event] = hass.data.setdefault(
            f"{DOMAIN}_vane_cleanup_pending", {}
        )
        self._command_observations: dict[str, dict[str, Any]] = hass.data.setdefault(
            f"{DOMAIN}_command_observations", {}
        )
        # Seed data so entities have something to read before the first refresh.
        self.data = self._compute()
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
        self._floor_live = True  # the switches have restored by now
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
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._on_heartbeat, timedelta(minutes=15)
            )
        )
        # Keep the mxz_recompute event so the echavet proxy companion can poke us.
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_RECOMPUTE, self._on_recompute_event)
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
        self._unsubs.append(async_at_start(self.hass, self._on_ha_start))
        await self.async_refresh()

    async def async_shutdown_listeners(self) -> None:
        """Retire this context, then cancel its listeners, timers, and kick tasks.

        ``_retired`` first, so nothing this incarnation already scheduled can
        write a head again. Every pending self-heal, dry, dwell and coast timer
        is cancelled here. Retiring kicks is last: the existing own-wake guard
        parks a head still reporting fan_only (see _restore_woken_heads).
        """
        self._retired = True
        self._retire_deliveries()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        # Retire the dwell before cancelling it: a refresh still in flight runs
        # its own _sync_dwell_timer on the way out, and a callback the loop has
        # already picked up cannot be cancelled at all.
        self._dwell_retired = True
        self._cancel_dwell_timer()
        self._coast_retired = True
        self._cancel_coast_timer()
        self._refresh_pending = False
        if self._refresh_followup_timer is not None or self._needs_legacy_refresh_guard:
            self._cancel_refresh_followup()
        self._fresh_retired = True
        self._cancel_fresh_timer()
        for cancel in self._heal_timers.values():
            cancel()
        self._heal_timers.clear()
        for cancel in self._dry_timers.values():
            cancel()
        self._dry_timers.clear()
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

    def _compute(self) -> dict[str, Any]:
        """Recompute the plan dict from current inputs. Commands nothing.

        It advances the per-zone engage latch and any pending startup seed.
        Missing startup evidence is retained separately from a known idle run.

        The plan carries per-zone keys (``{slug}_demand`` / ``{slug}_engage`` /
        ``{slug}_temp``) — zones 0/1 use the primary/secondary slugs, so the
        legacy plan attributes are preserved verbatim — plus a compact ``zones``
        list for display.
        """
        common = {
            "eco": self._eco_active(),
            "eco_cool_max": self.eco_cool_max,
            "eco_heat_min": self.eco_heat_min,
            "heat_lockout": self.heat_lockout,
            "heat_lockout_floor": self.heat_lockout_floor,
            "cool_lockout": self.cool_lockout,
            "cool_lockout_ceiling": self.cool_lockout_ceiling,
        }
        now = dt_util.utcnow().timestamp()
        plan: dict[str, Any] = {}
        demands: list[str] = []
        engages: list[str] = []
        healths: list[str] = []
        ages: list[float | None] = []
        votes: list[str | None] = []
        arb_demands: list[Demand] = []
        floors: list[str | None] = []
        sides: list[tuple[float, float]] = []
        starts: list[dict[str, Any]] = []
        all_ok = True
        coasted = False
        for i, zone in enumerate(self.zones):
            state = self.hass.states.get(zone.sensor_id)
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
                    zone.enable and self.coordinator_enable and self._restored_coordinator_on
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
                head = self.hass.states.get(zone.climate_id)
                self._engage_latch[zone.slug] = (
                    head.state
                    if head is not None and head.state in (MODE_COOL, MODE_HEAT)
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
            # This room let go of a run and is now coasting: it is parked at the
            # idle action, and only a later compute may engage it again (see
            # _sync_coast_timer). A disabled room (MODE_OFF) is not coasting.
            if prior_engage and engage == ENGAGE_SATISFIED:
                coasted = True
            # A tripped floor is this disabled room's engage (the latch stays
            # untouched: it belongs to the room's own enabled runs).
            floor = self._floor_want(zone, temp, ok, now)
            if floor is not None:
                engage = floor
            vote = self._hands_off_vote(zone)
            arb_demands.extend(
                self._zone_arbitration_pair(
                    demand=demand, vote=vote, safety=self._zone_safety(zone), index=i,
                    floor=floor,
                )
            )
            floors.append(floor)
            demands.append(demand)
            engages.append(engage)
            votes.append(vote)
            healths.append(health)
            ages.append(age)
            plan[f"{zone.slug}_demand"] = demand
            plan[f"{zone.slug}_engage"] = engage
            # Display only: a rejected reading publishes NO temperature. The
            # fallback stays inside the arithmetic above (and in _apply's
            # delta) instead of being shown as the room's measured value. A
            # STALE reading is a real measurement and keeps being shown — with
            # its health and its age beside it. No value is ever substituted.
            plan[f"{zone.slug}_temp"] = temp if valid else None
            plan[f"{zone.slug}_sensor_health"] = health
            plan[f"{zone.slug}_sensor_age"] = None if age is None else int(age)

        self._coasted = coasted
        current = self._arbitration_mode()
        # Monotonic delta, not derived from `now` above (wall-clock, used for
        # display/freshness elsewhere) — an NTP step must not perturb dwell.
        elapsed = time.monotonic() - self._last_mode_change_ts
        allowed = elapsed >= self.hysteresis
        engaged = any(e in (MODE_COOL, MODE_HEAT) for e in engages)
        grant = arbitrate(
            arb_demands,
            engaged=engaged,
            current=current,
            allowed=allowed,
            resting=self._resting_bias(),
        )
        state, by_votes = grant.direction, grant.read_only
        if (
            not by_votes and self._mode_by_votes and state != current
            and not any(c in (MODE_COOL, MODE_HEAT) for c in (*demands, *engages))
        ):
            by_votes = True
        self._floor_settle(floors, state)
        calls = [*demands, *votes, *floors]
        plan.update(
            {
                "state": state,
                "zones": [
                    {
                        "name": zone.name,
                        "demand": demands[i],
                        "engage": engages[i],
                        "temp": plan[f"{zone.slug}_temp"],
                        "target": zone.target,
                        # A dual-setpoint room's Heat to and Cool to (its
                        # target above is their midpoint). A single-target
                        # row has no such keys.
                        **(
                            {"target_low": zone.target_low, "target_high": zone.target_high}
                            if zone.dual else {}
                        ),
                        "enabled": zone.enable,
                        "drift": self.zone_drift(zone),
                        # The drift in use below / above target (the same as
                        # "drift" unless uneven drift is set).
                        "drift_heat": sides[i][0],
                        "drift_cool": sides[i][1],
                        **starts[i],
                        "fan_hold": not self.fan_auto_is_on(zone.climate_id),
                        "sensor_health": healths[i],
                        "sensor_age": None if ages[i] is None else int(ages[i]),
                        "control_reasons": self._control_reasons(zone),
                        "hands_off": self._hands_off_phase(zone),
                        "hands_off_vote": votes[i],
                        "safety_priority": self._zone_safety(zone),
                        "floor_low": zone.floor_low,
                        "floor_high": zone.floor_high,
                        **self._floor_attributes(zone),
                        "floor_want": floors[i],
                        **self.command_attributes(zone.climate_id),
                    }
                    for i, zone in enumerate(self.zones)
                ],
                # Both directions physically asked of the one outdoor unit,
                # a hands-off head's and a floor's included.
                "standoff": MODE_COOL in calls and MODE_HEAT in calls,
                "mode_by_hands_off": by_votes,
                "mode_by_safety": grant.by_safety,
                "mode_by_floor": state in floors,
                "sensors_ok": all_ok,
                "seconds_since_mode_change": int(elapsed),
                "mode_change_allowed": allowed,
                "inhibited": self.inhibited,
                "idle_action": self.idle_action,
            }
        )
        return plan

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
        if problem := self._fan_restore_problem.get(zone.climate_id):
            add(problem, "Reported speed is the conservative fallback; turn Fan auto ON to hand back control.")
        if zone.climate_id in self._fan_on_pending:
            add("Explicit Fan auto ON is waiting for a usable speed report.",
                "Wait for the report, or turn Fan auto OFF to cancel the handback.")
        if not self.data or not self.last_update_success:
            add("Current plan unavailable.",
                "Check the integration error and restore its inputs, then recompute.")
        if self._head_native_limits(zone.climate_id) is None:
            if self._head_safe_band(zone.climate_id) is not None:
                add("Native head limits unavailable; using the reported state band.",
                    "Restore native limit metadata; displayed limits can be rounded.")
            else:
                add("Head limits unavailable; using configured temperature clamps.",
                    "Restore valid head limits; configured clamps do not prove hardware acceptance.")
        observed = self.command_attributes(zone.climate_id)
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
        conf = {**entry.data, **entry.options}
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
        if health == HEALTH_HEALTHY:
            self._fresh_deadline[zone.slug] = now - evidence_age + max_age
        elif health == HEALTH_AWAITING:
            self._fresh_deadline[zone.slug] = grace_until
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
        self._cancel_fresh_timer()
        if self._fresh_retired or not self._fresh_deadline:
            return

        @callback
        def _fire(_now: Any) -> None:
            self._fresh_timer = None
            if self._fresh_retired:
                return  # cancelling cannot recall a callback already in flight
            self.hass.async_create_task(self.async_request_refresh())

        # The boundary itself, with no allowance added: the deadline IS the
        # third due instant, and a room that is stale at it must not keep
        # steering the house for another second by construction. The comparison
        # is half-open, so the refresh this fires evaluates the boundary as
        # stale — and a report that landed at the boundary has already moved
        # its own deadline, so re-reading is what lets it win.
        remaining = min(self._fresh_deadline.values()) - dt_util.utcnow().timestamp()
        self._fresh_timer = async_call_later(self.hass, max(remaining, 0.0), _fire)

    def _cancel_fresh_timer(self) -> None:
        if self._fresh_timer is not None:
            self._fresh_timer()
            self._fresh_timer = None

    async def async_request_refresh(self) -> None:
        """Retain current inputs independently of HA's debounce timer."""
        if self._retired:
            return
        if self._deliveries:
            # New intent must progress even while another head is still held.
            self._delivery_followup = True
        await self._async_request_ordinary_refresh()

    async def _async_request_ordinary_refresh(self) -> None:
        """Retain ordinary head reports without promoting echoes to new intent."""
        if self._retired:
            return
        if self._plan_lock.locked():
            self._refresh_wake.set()
        if self._delivery_followup or self._needs_legacy_refresh_guard and (
            self._plan_lock.locked() or self._debounced_refresh._execute_lock.locked()
        ):
            self._refresh_pending = True
            self._refresh_wake.set()
            if not self._plan_lock.locked():
                self._sync_refresh_followup()
            return
        await super().async_request_refresh()

    async def _async_update_data(self) -> dict[str, Any]:
        """Serialize plan decisions on every supported HA version."""
        async with self._plan_lock:
            self._refresh_generation += 1
            self._delivery_error = None
            if self._refresh_followup_timer is not None or self._needs_legacy_refresh_guard:
                self._cancel_refresh_followup()
            self._refresh_pending = False
            self._delivery_followup = False
            self._refresh_wake.clear()
            return await self._async_update_data_once()

    async def _async_update_data_once(self) -> dict[str, Any]:
        """Recompute and apply one plan."""
        plan = self._compute()
        # Read the request ordinal this plan was computed against before any
        # head service is awaited: a selection landing while those services are
        # in flight is newer than this plan.
        selection_seq = self._selection_seq
        self._applying_plan = plan
        # This is computed intent, not delivery confirmation. Per-head software
        # status accompanies it, and in-flight action ownership remains separate.
        self.data = plan
        # Resolve an existing coast wakeup as soon as the decision resolves it;
        # an unrelated pending transport must not postpone its cancellation.
        if not self._coasted:
            self._cancel_coast_timer()
        try:
            await self._apply(plan, selection_seq)
            # _apply is what settles the manual-fan latch (it reads each head's
            # observed fan_mode), so re-stamp fan_hold from the post-apply state —
            # otherwise the plan's diagnostic would lag the latch by a cycle. It
            # is the same truth the Fan auto switch shows, pending restore first.
            for zone_view, zone in zip(plan.get("zones", ()), self.zones):
                zone_view["fan_hold"] = not self.fan_auto_is_on(zone.climate_id)
                zone_view["control_reasons"] = self._control_reasons(zone)
            return plan
        finally:
            self._applying_plan = None
            self._sync_dwell_timer(plan)
            self._sync_coast_timer()
            self._sync_freshness_timer()
            if self._refresh_pending or self._needs_legacy_refresh_guard:
                self._sync_refresh_followup()

    def _sync_refresh_followup(self) -> None:
        """Arm exactly one current-input refresh requested during this one."""
        if self._retired:
            self._refresh_pending = False
            self._cancel_refresh_followup()
            return
        if not self._refresh_pending:
            return
        self._refresh_pending = False
        self._cancel_refresh_followup()

        generation = self._refresh_generation

        async def _refresh_current() -> None:
            # A direct refresh already waiting on the plan lock consumes this
            # request. Check after it publishes, not when the timer is picked.
            async with self._plan_lock:
                if self._retired or self._refresh_generation != generation:
                    return
            if self._delivery_followup:
                self._delivery_followup = False
                await self.async_refresh()
            elif self._deliveries:
                await self._async_request_ordinary_refresh()
            else:
                await self.async_request_refresh()

        @callback
        def _fire(_now: Any) -> None:
            self._refresh_followup_timer = None
            if self._retired:
                return
            self.hass.async_create_task(_refresh_current())

        if self._needs_legacy_refresh_guard:
            self._refresh_followup_timer = async_call_later(self.hass, 0, _fire)
        else:
            async def _run() -> None:
                self._refresh_followup_timer = None
                await _refresh_current()

            task = self.hass.async_create_task(_run(), eager_start=False)
            self._refresh_followup_timer = task.cancel

    def _cancel_refresh_followup(self) -> None:
        """Cancel the one queued current-input follow-up, if any."""
        if self._refresh_followup_timer is not None:
            self._refresh_followup_timer()
            self._refresh_followup_timer = None

    # -- mode-dwell wakeup ---------------------------------------------------
    def _sync_dwell_timer(self, plan: dict[str, Any]) -> None:
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
        remaining = self.hysteresis - (time.monotonic() - self._last_mode_change_ts)
        if (
            self._dwell_retired
            or not self.coordinator_enable
            or self._parked_by_standby()
        ):
            self._cancel_dwell_timer()
            return
        if remaining <= 0 and plan["mode_change_allowed"]:
            self._cancel_dwell_timer()  # this plan already had its free hand
            return
        current = self._arbitration_mode()
        views = plan.get("zones", ())
        engages = [plan[f"{zone.slug}_engage"] for zone in self.zones]
        engaged = any(e in (MODE_COOL, MODE_HEAT) for e in engages)
        arb_demands: list[Demand] = []
        for i, (zone, view) in enumerate(zip(self.zones, views)):
            arb_demands.extend(
                self._zone_arbitration_pair(
                    demand=plan[f"{zone.slug}_demand"],
                    vote=view.get("hands_off_vote"),
                    safety=bool(view.get("safety_priority")),
                    index=i,
                    floor=view.get("floor_want"),
                )
            )
        would_flip_to = arbitrate(
            arb_demands, engaged=engaged, current=current, allowed=True, resting=self._resting_bias(),
        ).direction
        if would_flip_to == current:
            self._cancel_dwell_timer()  # nothing is waiting on this dwell
            return
        self._arm_dwell_timer(remaining)

    def _arm_dwell_timer(self, remaining: float) -> None:
        """Replace the pending wakeup with one for this dwell's expiry.

        ``remaining <= 0`` is a dwell that ran out while the last decision was
        being applied: that one wakes up at the first opportunity.
        """
        self._cancel_dwell_timer()
        if self._dwell_retired:
            return

        @callback
        def _fire(_now: Any) -> None:
            self._dwell_timer = None
            if self._dwell_retired:
                return  # cancelling cannot recall a callback already in flight
            self.hass.async_create_task(self.async_request_refresh())

        delay = remaining + 1 if remaining > 0 else 0
        self._dwell_timer = async_call_later(self.hass, delay, _fire)

    def _cancel_dwell_timer(self) -> None:
        if self._dwell_timer is not None:
            self._dwell_timer()
            self._dwell_timer = None

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
            self._coast_retired
            or not self.coordinator_enable
            or self._parked_by_standby()
            or not self._coasted
        ):
            self._cancel_coast_timer()
            return
        self._arm_coast_timer()

    def _arm_coast_timer(self) -> None:
        """Replace the pending follow-up with one for this coast."""
        self._cancel_coast_timer()
        if self._coast_retired:
            return

        @callback
        def _fire(_now: Any) -> None:
            self._coast_timer = None
            if self._coast_retired:
                return  # cancelling cannot recall a callback already in flight
            self.hass.async_create_task(self.async_request_refresh())

        self._coast_timer = async_call_later(self.hass, COAST_FOLLOWUP_DELAY, _fire)

    def _cancel_coast_timer(self) -> None:
        if self._coast_timer is not None:
            self._coast_timer()
            self._coast_timer = None

    # -- actuator (mirrors script.mxz_coordinate) ---------------------------
    async def _apply(self, plan: dict[str, Any], selection_seq: int) -> None:
        """Schedule each head's ordered plan writes; idempotent.

        ``selection_seq`` is the explicit-request ordinal read when this plan
        was computed; the writeback at the end skips itself if a person has
        asked for a direction since.
        """
        # A hand-back (P3) is for this apply only: one skipped (Coordinator
        # off, a standby park, no plan yet) leaves the head as MXZ left it.
        handbacks, self._floor_handback = self._floor_handback, {}
        if self._retired:
            return
        if not self.coordinator_enable:
            return  # kill-switch: leave the heads untouched

        # External standby hold (grid-down / load-shed): a fixed-mode park is a
        # no-plan short-circuit; the `eco` hold falls through to the normal plan
        # with eco forced on (see _eco_active) so protection extremes still run.
        if self._parked_by_standby():
            await self._park_heads(
                MODE_OFF
                if self.inhibit_action == INHIBIT_ACTION_OFF
                else MODE_FAN_ONLY
            )
            return

        state = plan["state"]
        valid_eng = (MODE_COOL, MODE_HEAT, ENGAGE_SATISFIED, MODE_OFF)
        if state not in (MODE_COOL, MODE_HEAT):
            return  # plan not ready
        engages = [plan[f"{zone.slug}_engage"] for zone in self.zones]
        if any(engage not in valid_eng for engage in engages):
            return

        floors = [view.get("floor_want") for view in plan.get("zones", ())]
        tasks = []
        for zone, engage, floor in zip(self.zones, engages, floors):
            if zone.climate_id in self._vane_kicks:
                continue  # mid vane-kick: leave the head alone until it finishes
            if (
                self._hands_off_phase(zone) in ("parking", "released")
                and not self.inhibited
            ):
                # (A floor run skips all of this: the head is MXZ's for the run.)
                # (Any standby hold, eco included, holds it off like any
                # disabled room: the fixed parks never reach here.)
                if zone.slug in self._hands_off_released:
                    head = self.hass.states.get(zone.climate_id)
                    back = handbacks.get(zone.slug)
                    if back is not None and head is not None and (
                        head.state not in UNAVAILABLE_STATES
                    ):
                        # A floor run ended on a head a person was already
                        # running that way: theirs again, as they had it (P3).
                        # A retry goes only over MXZ's own command, never
                        # over a change made since.
                        theirs, attempt, left = back
                        seen = _head_setting(head)
                        if left is None or seen == left:
                            task = self._queue_delivery(zone.climate_id, *theirs)
                            if task is not None:
                                tasks.append(task)
                                # A delivery superseded before it reaches the
                                # head ends with neither accepted nor rejected,
                                # so this entry stays until the next hand-back
                                # overwrites it. Harmless: it is matched only
                                # by its delivery's identity, never acted on.
                                self._floor_handing_back[zone.slug] = (
                                    theirs, attempt, seen,
                                    self._deliveries[zone.climate_id],
                                )
                    continue  # its off landed: the head is someone else's now
                head = self.hass.states.get(zone.climate_id)
                if (
                    head is not None and head.state == MODE_OFF
                    and zone.climate_id not in self._deliveries
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
            idle = self._idle_act_for(zone.climate_id)
            if not zone.enable:
                # A disabled room never idles in fan_only: a floor waiting out
                # the mode is held off. (Without a floor, engage is off and
                # decides first.)
                idle = MODE_OFF
            act = head_action(
                engage=engage,
                mode=state,
                eco=self._eco_active(),
                idle=idle,
            )
            heat_target, cool_target = self.zone_side_targets(zone)
            low, high = setpoints(
                mode=state,
                # A floor runs the head to its clear line, never the eco band.
                target=(
                    self._floor_clear(zone, floor) if floor else float(zone.target)
                ),
                # Heat to while the shared mode is heat, Cool to while it's
                # cool (the one target, twice, in a single-target room). A
                # floor's clear line is its own target.
                heat_target=None if floor else float(heat_target),
                cool_target=None if floor else float(cool_target),
                eco=self._eco_active() and not floor,
                clamp_min=self.clamp_min,
                clamp_max=self.clamp_max,
                band=self.setpoint_band,
                step=self.target_step,
                eco_cool=self.eco_cool,
                eco_heat=self.eco_heat,
            )
            taken = self._floor_prior.get(zone.slug) if floor == state else None
            if taken is not None:
                # A run taken over from a person's own heat keeps their
                # setpoints where those are warmer than the clear line's
                # (cooler, for a ceiling over their cool) (P10): their latest,
                # if they changed them during the run (P15).
                keep = max if floor == MODE_HEAT else min
                low, high = keep(low, taken[1]), keep(high, taken[2])
            shown = plan[f"{zone.slug}_temp"]
            # How far the room sits outside its range (off its target, for a
            # single target, where the range is the target itself).
            delta = 0.0 if shown is None else range_delta(
                temp=shown, low=float(heat_target), high=float(cool_target)
            )
            # A floor delivery takes the fan-free path (engage None, as the
            # standby park does): mode and setpoint only, so a manual fan pick
            # is kept and MXZ never writes this head's fan.
            task = self._queue_delivery(
                zone.climate_id, act, low, high, delta, None if floor else engage
            )
            if task is not None:
                tasks.append(task)
                if taken is not None:
                    self._floor_expect_command(zone, state, low, high)
        await self._wait_deliveries(tasks)

        # Stamp the flip only on a real mode change (cool<->heat) — and never
        # over a person's request that arrived while the head services above
        # were in flight. This plan was computed before that request, so its
        # direction is the older one; the request keeps the selector and the
        # clock it already set, and the refresh it queued computes next.
        # A flip only hands-off heads asked for is not one the coordinator
        # caused, so it starts no dwell (see logic.arbitrate()). Once a
        # coordinated room runs in that mode, the mode is ours: stamp it then,
        # as if that room had flipped it, so the next reversal is dwelled.
        # The vote's undoing (back to the resting bias) starts no dwell either,
        # but leaves a mode that owes the vote nothing: a run there is no flip.
        if selection_seq != self._selection_seq:
            return
        if state != self.current_shared_mode:
            self.current_shared_mode = state
            self._mode_by_votes = (
                bool(plan.get("mode_by_hands_off")) and state != self._resting_bias()
            )
            if not plan.get("mode_by_hands_off"):
                self._last_mode_change_ts = time.monotonic()
            self.async_update_listeners()  # let the shared-mode select re-render
        elif self._mode_by_votes and state in (
            *engages, *(plan[f"{zone.slug}_demand"] for zone in self.zones)
        ):
            self._mode_by_votes = False
            self._last_mode_change_ts = time.monotonic()

    def _queue_delivery(
        self, climate_id: str, act: str, low: float, high: float,
        delta: float = 0.0, engage: str | None = None,
    ) -> asyncio.Task | None:
        """One real operation per head, with at most one current-input follow-up.

        The registry lives with HA, so reload cannot forget an older handler.
        A waiting new incarnation inherits no old intent or fan provenance.
        """
        intent = (act, low, high, delta, engage, self.inhibited, self.fan_auto_is_on(climate_id))
        if active := self._deliveries.get(climate_id):
            changed = (
                active["owner"] is not self or active["retired"] or active["manual"]
                or active["intent"] != intent
            )
            active["next"] = self if changed else None
            active["superseded"] = changed
            if changed:
                self._publish_delivery()
            return None
        active = {
            "owner": self, "act": act, "intent": intent, "next": None,
            "superseded": False, "retired": False, "manual": False, "locked": False,
        }
        self._deliveries[climate_id] = active
        task = self.hass.async_create_task(
            self._deliver_head(climate_id, active, act, low, high, delta, engage),
            eager_start=False,
        )
        active["task"] = task
        task.add_done_callback(partial(self._finish_delivery, climate_id, active))
        return task

    async def _wait_deliveries(self, tasks: list[asyncio.Task]) -> None:
        """Wait for issued work; deferred work or new inputs release the plan."""
        if not tasks:
            return
        wake = asyncio.create_task(self._refresh_wake.wait())
        pending = set(tasks)
        try:
            while pending and not wake.done():
                done, _ = await asyncio.wait(pending | {wake}, return_when=asyncio.FIRST_COMPLETED)
                pending -= done
        finally:
            wake.cancel()
            await asyncio.gather(wake, return_exceptions=True)
        if self._delivery_error is not None:
            raise self._delivery_error

    def _delivery_owned(self, active: dict[str, Any]) -> bool:
        return (
            not self._retired and self.coordinator_enable
            and not active["superseded"] and not active["retired"] and not active["manual"]
        )

    async def _deliver_head(
        self, climate_id: str, active: dict[str, Any], act: str,
        low: float, high: float, delta: float, engage: str | None,
    ) -> None:
        """Preserve the existing per-head sequence; never follow obsolete work."""
        try:
            # Own the complete per-head sequence, including fan observation.
            # A queued manual command runs before the next sequence inspects its
            # fan token; no pre-lock guess can overwrite that manual choice.
            lock = self._head_locks.setdefault(climate_id, asyncio.Lock())
            if lock.locked() or climate_id in self._vane_cleanup_pending:
                # This sequence has issued nothing. Keep it queued behind the
                # real handler/cleanup, but do not hold its creating service or
                # entry setup open until an unrelated input wakes the plan.
                self._refresh_wake.set()
                self._publish_delivery()
            async with lock:
                await self._wait_vane_cleanup(climate_id)
                active["locked"] = True
                await self._deliver_head_steps(climate_id, active, act, low, high, delta, engage)
        except HomeAssistantError as err:
            _LOGGER.error(
                "MXZ: applying %s to %s failed (zone degraded, others continue): %s",
                act, climate_id, err,
            )
            self._floor_rejected(climate_id, err, active)
        except Exception as err:
            # Unexpected dependency failures retain the existing global failure
            # and restore-channel semantics, even if this head outlives a plan.
            _LOGGER.exception("MXZ: unexpected command failure for %s", climate_id)
            self._delivery_error = err
            self.last_update_success = False
            self._publish_delivery()
            # A global failure still updates every coordinator entity, even
            # when the failing delivery has outlived its creating refresh.
            self.async_update_listeners()

    @callback
    def _finish_delivery(
        self, climate_id: str, active: dict[str, Any], _task: asyncio.Task,
    ) -> None:
        """Release ownership after the call's queued state callbacks run."""
        if self._deliveries.get(climate_id) is active:
            self._deliveries.pop(climate_id)
            self._publish_delivery()
        successor = active["next"]
        if successor is not None and not successor._retired:
            successor._refresh_pending = True
            successor._delivery_followup = True
            successor._sync_refresh_followup()

    async def _deliver_head_steps(
        self, climate_id: str, active: dict[str, Any], act: str,
        low: float, high: float, delta: float, engage: str | None,
    ) -> None:
        if not self._delivery_owned(active):
            return
        if engage is None:  # fixed standby park or a floor, no fan machinery
            await self._apply_head(climate_id, act, low, high)
            self._floor_accepted(climate_id, active)
            return
        if act in (MODE_COOL, MODE_HEAT):
            self._last_active[climate_id] = (act, time.monotonic())
        if act != MODE_FAN_ONLY:
            self._cancel_dry_timer(climate_id)
        cur = self.hass.states.get(climate_id)
        handback = (
            act == MODE_OFF and self.idle_action != IDLE_ACTION_FAN_ONLY
            and engage != MODE_OFF and not self._eco_active() and not self.inhibited
            and cur is not None and cur.state not in (MODE_OFF, *UNAVAILABLE_STATES)
        )
        if handback:
            await self._apply_fan(climate_id, MODE_FAN_ONLY, delta)
            if not self._delivery_owned(active):
                return
        await self._apply_head(climate_id, act, low, high)
        self._floor_accepted(climate_id, active)
        if not self._delivery_owned(active):
            return
        if handback:
            self._fan_idx.pop(climate_id, None)
        elif not self.inhibited:
            await self._apply_fan(climate_id, act, delta)

    def _retire_deliveries(self) -> None:
        """Disown follow-ons without pretending to interrupt external work."""
        for climate_id, active in self._deliveries.items():
            if active["owner"] is self:
                active["retired"] = True
                active["superseded"] = True
                observed = self._command_observations.get(climate_id, {})
                if observed.get("_owner") is self:
                    observed.update(
                        command_retired_at=dt_util.utcnow().isoformat(), command_ownership_retired=True,
                    )
            if active["next"] is self:
                active["next"] = None
        for observed in self._command_observations.values():
            if observed.get("_owner") is self and observed.get("command_status") == "pending":
                observed["command_retired_at"] = dt_util.utcnow().isoformat()
                observed["command_ownership_retired"] = True
        self._refresh_wake.set()
        self._publish_delivery()

    async def async_head_service(
        self, climate_id: str, service: str, payload: dict[str, Any],
        *, manual: bool = False, vane: bool = False,
    ) -> None:
        """Serialize ordinary climate handlers even across entry incarnations."""
        active = self._deliveries.get(climate_id)
        if manual and active:
            # A facade gesture supersedes the old sequence before it yields,
            # while the actual manual handler still waits for the old return.
            active["manual"] = True
        if active and active.get("task") is asyncio.current_task() and active["locked"]:
            await self._wait_vane_cleanup(climate_id)
            if self._delivery_owned(active):
                await self._call_head_service(climate_id, service, payload)
            return
        async with self._head_locks.setdefault(climate_id, asyncio.Lock()):
            await self._wait_vane_cleanup(climate_id)
            if self._retired:
                return
            if vane and not self._owns_vane_kick(climate_id):
                return
            if vane and service == "set_hvac_mode" and payload.get("hvac_mode") == MODE_FAN_ONLY:
                # Ownership starts at issuance, not while waiting for a lock.
                self._vane_kick_woken.add(climate_id)
            await self._call_head_service(climate_id, service, payload)

    async def _wait_vane_cleanup(self, climate_id: str) -> None:
        """A guarded cleanup may outlive its wake; ordinary work waits for both."""
        while pending := self._vane_cleanup_pending.get(climate_id):
            await pending.wait()

    async def _call_head_service(
        self, climate_id: str, service: str, payload: dict[str, Any],
        *, observation: dict[str, Any] | None = None,
    ) -> None:
        """Observe one software call, never claim physical receipt."""
        observed = observation if observation is not None else self._command_observations.setdefault(climate_id, {})
        if observation is None:
            # Ordinary issuance has waited for both the old handler and its
            # guarded cleanup. That cleanup is no longer a current obstacle.
            observed.pop("vane_retirement_cleanup", None)
            observed.update(_owner=self, _task=asyncio.current_task(), command_ownership_retired=False)
        observed.update(command_status="pending", command_attempted_at=dt_util.utcnow().isoformat())
        self._publish_delivery()
        try:
            await self.hass.services.async_call(
                "climate", service, {"entity_id": climate_id, **payload}, blocking=True,
            )
        except asyncio.CancelledError:
            observed["command_status"] = "cancelled; outcome unknown"
            raise
        except Exception as err:
            observed.update(
                command_status="rejected", command_failed_at=dt_util.utcnow().isoformat(),
                command_error=str(err),
            )
            raise
        else:
            observed.update(command_status="returned", command_returned_at=dt_util.utcnow().isoformat())
        finally:
            self._publish_delivery()

    def command_attributes(self, climate_id: str) -> dict[str, Any]:
        """Software observations, with honest absence rather than invented dates."""
        observed = self._command_observations.get(climate_id, {})
        active = self._deliveries.get(climate_id)
        if active and active["owner"] is not self:
            observed = active["owner"]._command_observations.get(climate_id, {})
        return {
            **{key: observed.get(key, "not yet recorded") for key in (
                "command_attempted_at", "command_returned_at", "command_failed_at", "command_retired_at",
            )},
            "command_status": observed.get("command_status", "not yet recorded"),
            "command_error": observed.get("command_error"),
            "command_ownership_retired": observed.get("command_ownership_retired", False),
            "vane_retirement_cleanup": dict(observed.get("vane_retirement_cleanup", {})),
            "command_deferred": bool(active and (active["next"] is self or not active["locked"])),
            "command_timestamp_basis": "Home Assistant software observations; not physical receipt",
            "plan_target_basis": "computed intent; command return does not prove physical application",
        }

    def room_details(self) -> list[dict[str, Any]]:
        """Existing room diagnostics remain useful even without a current plan.

        Only the plan carries the head's ``last_updated``, as of this publish.
        The room thermostat re-renders on every head event, so a live copy
        there would turn each head attribute-only update into a state event and
        recorder row; the head entity already shows its own time.
        """
        views = (self.data or {}).get("zones", ())
        heads = [self.hass.states.get(zone.climate_id) for zone in self.zones]
        return [
            {
                **(views[i] if i < len(views) else {"name": zone.name}),
                **self.command_attributes(zone.climate_id),
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
        views = (self.data or {}).get("zones", ())
        view = next(
            (views[i] for i, known in enumerate(self.zones)
             if known is zone and i < len(views)),
            {},
        )
        return {key: view.get(key) for key in START_POINT_KEYS}

    @callback
    def async_add_delivery_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Observe software delivery without a refresh-completion notification."""
        self._delivery_listeners.add(listener)
        return partial(self._delivery_listeners.discard, listener)

    def _publish_delivery(self) -> None:
        """Publish changed diagnostics immediately, without another decision."""
        for plan in (self.data, self._applying_plan):
            if plan:
                for view, zone in zip(plan.get("zones", ()), self.zones):
                    view.update(self.command_attributes(zone.climate_id))
                    view.update(self._floor_attributes(zone))
                    view["control_reasons"] = self._control_reasons(zone)
        for listener in tuple(self._delivery_listeners):
            listener()
        successor = getattr(self.config_entry, "runtime_data", None)
        if isinstance(successor, MXZCoordinator) and successor is not self and not successor._retired:
            # The old handler may return after unload; publish that software
            # observation on the current incarnation's existing room surfaces.
            successor._publish_delivery()

    def _parked_by_standby(self) -> bool:
        """Whether an inhibit hold is parking every head at one fixed mode.

        The `eco` hold is not a park: it falls through to the normal plan.
        """
        return self.inhibited and self.inhibit_action in (
            INHIBIT_ACTION_OFF,
            INHIBIT_ACTION_FAN_ONLY,
        )

    def _eco_active(self) -> bool:
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
        remaining = self.coil_dry_seconds - (time.monotonic() - rec[1])
        if remaining <= 0:
            return MODE_OFF
        self._arm_dry_timer(climate_id, remaining)
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
            rec = (MODE_COOL, time.monotonic())
            self._last_active[climate_id] = rec
            return rec
        return None

    def _arm_dry_timer(self, climate_id: str, remaining: float) -> None:
        """One-shot refresh nudge at dwell expiry (idempotent per head)."""
        if climate_id in self._dry_timers:
            return  # the stamped timestamp doesn't move while idle

        @callback
        def _fire(_now: Any) -> None:
            self._dry_timers.pop(climate_id, None)
            self.hass.async_create_task(self.async_request_refresh())

        self._dry_timers[climate_id] = async_call_later(
            self.hass, remaining + 1, _fire
        )

    def _cancel_dry_timer(self, climate_id: str) -> None:
        cancel = self._dry_timers.pop(climate_id, None)
        if cancel is not None:
            cancel()

    def _planned_act_for(self, entity_id: str) -> str | None:
        """The action owning this head's writes, else the current plan's action.

        A newer decision may already be published while this head still reports
        an older operation. Lets the off-drift self-heal tell a plan-parked off head (idle_action
        off, eco) from a genuine wall-remote off during an active call — the
        former must not be "healed" back awake.
        """
        if active := self._deliveries.get(entity_id):
            return active["act"]
        data = self._applying_plan if self._applying_plan is not None else self.data or {}
        state = data.get("state")
        if state not in (MODE_COOL, MODE_HEAT):
            return None
        for zone in self.zones:
            if zone.climate_id == entity_id:
                engage = data.get(f"{zone.slug}_engage")
                if engage not in (MODE_COOL, MODE_HEAT, ENGAGE_SATISFIED, MODE_OFF):
                    return None
                idle = self._idle_act_for(entity_id)
                return head_action(
                    engage=engage,
                    mode=state,
                    eco=self._eco_active(),
                    idle=idle if zone.enable else MODE_OFF,  # as _apply
                )
        return None

    async def _park_heads(self, mode: str) -> None:
        """Standby: drive every coordinated head to a fixed mode (off/fan_only).

        Mode-only — never the fan-boost/latch machinery — so a manual fan hold
        survives the hold (reconciled on release, see _reseed_fan_after_standby).
        Same per-zone isolation as _apply, and skips a head mid vane-kick.
        """
        tasks = []
        for zone in self.zones:
            if self._retired:
                return
            if zone.climate_id in self._vane_kicks:
                continue
            task = self._queue_delivery(zone.climate_id, mode, 0.0, 0.0)
            if task is not None:
                tasks.append(task)
        await self._wait_deliveries(tasks)

    # -- per-head operating band (native-unit safe clamp, #10) --------------
    def _head_entity(self, climate_id: str) -> Any:
        """The head's live climate entity object, or None if not reachable."""
        component = (self.hass.data.get(DATA_INSTANCES) or {}).get("climate")
        return component.get_entity(climate_id) if component else None

    def _head_native_limits(
        self, climate_id: str
    ) -> tuple[float, float, str] | None:
        """This head's UNROUNDED native (min, max, unit), or None if unknown.

        Read from the live entity object — the same values HA's own
        set_temperature validator range-checks against — because the head's
        min_temp/max_temp STATE ATTRIBUTES are display-rounded to the system
        unit's precision and so can read ABOVE the true native ceiling (a
        26.0 °C max shows as 79 °F but rejects 79 °F). Best-effort: any missing
        entity / odd unit / inverted band yields None and the caller falls back.
        """
        entity = self._head_entity(climate_id)
        if entity is None:
            return None
        try:
            nmin = float(entity.min_temp)
            nmax = float(entity.max_temp)
            nunit = entity.temperature_unit
        except (TypeError, ValueError, AttributeError):
            return None
        if nunit not in (
            UnitOfTemperature.CELSIUS,
            UnitOfTemperature.FAHRENHEIT,
        ) or nmax <= nmin:
            return None
        return (nmin, nmax, nunit)

    def _head_safe_band(self, climate_id: str) -> tuple[float, float] | None:
        """The head's operating band as system-unit edges it will ACCEPT.

        Convert the head's native limits into the system unit and snap the
        ceiling DOWN / the floor UP to our step, so every edge round-trips back
        through HA's unit conversion still inside the native band (78.8 °F ->
        78 °F, which converts to 25.56 °C <= the 26.0 °C native max). For a head
        already native to the system unit this is a no-op on whole values. Falls
        back to the display-rounded state attributes when the entity object
        isn't reachable (keeps the common same-unit case correct).
        """
        native = self._head_native_limits(climate_id)
        if native is not None:
            nmin, nmax, nunit = native
            lo = TemperatureConverter.convert(nmin, nunit, self.temp_unit)
            hi = TemperatureConverter.convert(nmax, nunit, self.temp_unit)
        else:
            st = self.hass.states.get(climate_id)
            if st is None:
                return None
            lo = _as_float(st.attributes.get("min_temp"))
            hi = _as_float(st.attributes.get("max_temp"))
            if lo is None or hi is None:
                return None
        step = self.target_step or 1.0
        # +/-1e-9 absorbs float noise so an exact 26.0/0.5 doesn't fall a step.
        safe_low = math.ceil(lo / step - 1e-9) * step
        safe_high = math.floor(hi / step + 1e-9) * step
        if native is not None and nunit != self.temp_unit:
            # Verify each snapped edge round-trips through the SAME conversion
            # HA's validator performs: the noise epsilon can promote an edge a
            # float-ulp past the true native limit (a 78.8 °F native max in a
            # °C system snaps to 26.0 °C, which converts back to 78.800…01).
            # One extra step inward always clears a 1-ulp overshoot.
            if TemperatureConverter.convert(safe_high, self.temp_unit, nunit) > nmax:
                safe_high -= step
            if TemperatureConverter.convert(safe_low, self.temp_unit, nunit) < nmin:
                safe_low += step
        if safe_low > safe_high:
            return None  # degenerate band (narrower than a step) -> don't clamp
        return (safe_low, safe_high)

    def _clamp_to_head_band(
        self, climate_id: str, low: float, high: float
    ) -> tuple[float, float]:
        """Clamp (low, high) into the head's accept-able band; pass through if unknown."""
        band = self._head_safe_band(climate_id)
        if band is None:
            return (low, high)
        safe_low, safe_high = band
        return (
            min(max(low, safe_low), safe_high),
            min(max(high, safe_low), safe_high),
        )

    def head_target_bounds(self, climate_id: str) -> tuple[float, float]:
        """UI setpoint bounds for a zone: [clamp_min, clamp_max] narrowed to the
        head's own accept-able band, so the number/thermostat facade never offers
        a target the head would reject (#10). If the head band is unknown or
        disjoint from the clamp band, fall back to the plain clamp band.
        """
        lo, hi = float(self.clamp_min), float(self.clamp_max)
        band = self._head_safe_band(climate_id)
        if band is None:
            return (lo, hi)
        safe_low, safe_high = band
        lo, hi = max(lo, safe_low), min(hi, safe_high)
        if lo > hi:
            return (float(self.clamp_min), float(self.clamp_max))
        return (lo, hi)

    def _head_already_at(
        self, climate_id: str, reported: float | None, desired: float
    ) -> bool:
        """True if sending ``desired`` would leave the head's setpoint unchanged.

        A head can only hold setpoints on its own native grid. A °C-native
        0.5 °C head (ESPHome CN105) in a °F system latches 18.0 °C for a
        commanded 64 °F (17.78 °C) and reports it back as 64.4 °F, displayed
        64.5 °F. That is the closest it can get to 64 °F, so comparing whole °F
        never matches and every cycle re-sends a value that changes nothing.

        So compare on the head's grid: convert ``desired`` to the native unit
        and snap it to the native step (the value the head will latch); the
        head already holds it when ``reported`` converts back to within half a
        native step. The reported value's display rounding (at most 0.14 °C at
        0.5 °F precision) and the raw commanded value the head publishes just
        before its echo (at most 0.22 °C from the grid for a whole °F) both
        stay inside that half step; a head one native step off does not. With
        no known native unit/step, fall back to half our own step.

        Known limits: this assumes the head rounds to the nearest step, as the
        CN105 does; a head that truncated to its grid would still re-send. It
        also assumes the entity displays at 0.5 °F or finer; at whole-°F
        display precision a one-step move on a 0.5 °C head can read as already
        there (the old whole-°F check had the same blind spot).
        """
        if reported is None:
            return False
        entity = self._head_entity(climate_id)
        try:
            step = float(entity.target_temperature_step)
            unit = entity.temperature_unit
        except (TypeError, ValueError, AttributeError):
            step, unit = 0.0, None
        if step <= 0 or unit not in (
            UnitOfTemperature.CELSIUS,
            UnitOfTemperature.FAHRENHEIT,
        ):
            return abs(reported - desired) < self.target_step / 2
        # Half up, as the CN105 firmware rounds (round() is half to even).
        latched = math.floor(
            TemperatureConverter.convert(desired, self.temp_unit, unit) / step + 0.5
        ) * step
        held = TemperatureConverter.convert(reported, self.temp_unit, unit)
        return abs(held - latched) < step / 2

    async def _apply_head(
        self, climate_id: str, act: str, low: float, high: float
    ) -> None:
        """Issue (or skip, if already correct) the command for one head."""
        state = self.hass.states.get(climate_id)
        cur_mode = state.state if state else None

        if act in (MODE_COOL, MODE_HEAT):
            # Clamp each edge to what THIS head will actually accept. The global
            # clamp_min/clamp_max can exceed an individual head's operating band,
            # and HA validates set_temperature by converting our (system-unit)
            # value into the head's NATIVE unit and range-checking there — so a
            # °C-native head whose native max is 26.0 °C rejects 79 °F (26.11 °C)
            # even though it REPORTS max_temp = 79 °F (78.8 rounded up). Clamping
            # in the head's native band (rounded toward the safe interior) keeps
            # a head-exceeding target from erroring every cycle / degrading the
            # zone (#10) — it lands on the head's real ceiling/floor instead.
            low, high = self._clamp_to_head_band(climate_id, low, high)
            features = (
                int(state.attributes.get("supported_features") or 0) if state else 0
            )
            # ClimateEntityFeature.TARGET_TEMPERATURE_RANGE == 2. Heads without
            # it (single-setpoint — common on MXZ indoor units) reject
            # target_temp_low/high, so send the one setpoint they accept (#6):
            # the clamped room target (= high edge in cool, low edge in heat).
            if not features & 2:
                setpoint = high if act == MODE_COOL else low
                cur = _as_float(state.attributes.get("temperature")) if state else None
                if cur_mode == act and self._head_already_at(climate_id, cur, setpoint):
                    return  # idempotent
                await self.async_head_service(
                    climate_id, "set_temperature",
                    {
                        "hvac_mode": act,
                        "temperature": setpoint,
                    },
                )
                return
            cur_low = _as_float(state.attributes.get("target_temp_low")) if state else None
            cur_high = (
                _as_float(state.attributes.get("target_temp_high")) if state else None
            )
            if (
                cur_mode == act
                and self._head_already_at(climate_id, cur_low, low)
                and self._head_already_at(climate_id, cur_high, high)
            ):
                return  # idempotent
            await self.async_head_service(
                climate_id, "set_temperature",
                {
                    "hvac_mode": act,
                    "target_temp_low": low,
                    "target_temp_high": high,
                },
            )
            return

        # fan_only / off -> set_hvac_mode only (a bare temperature throws)
        if cur_mode == act:
            return
        await self.async_head_service(
            climate_id, "set_hvac_mode",
            {"hvac_mode": act},
        )

    async def _apply_fan(self, climate_id: str, act: str, delta: float) -> None:
        """Drive the head's fan speed from ``delta`` (°F off-target). Idempotent.

        Only active when fan boost is enabled. An actively-conditioning head
        (cool/heat, not eco-idle) gets a ladder speed proportional to how far the
        room is off target; a satisfied/fan_only head (or eco-idle) is returned to
        the firmware's own "auto"; an off head is left alone.

        A manual pick latches (see ``_fan_latched``): while a head is latched the
        coordinator makes NO fan writes to it, so a user's chosen speed survives
        every apply cycle. The latch is evaluated first because it also suppresses
        the return-to-"auto" branch; it releases only on an observed "auto".
        """
        if not self.fan_boost_enable:
            return

        state = self.hass.states.get(climate_id)
        if state is None:
            return
        modes = state.attributes.get("fan_modes")
        if not modes or FAN_AUTO not in modes:
            # No fan control, or no "auto" to release the latch (or return a
            # satisfied head to) -> leave the head's fan entirely alone. This
            # supersedes the old best-effort ladder writes to auto-less heads,
            # which could only ratchet the fan up with no way back down.
            return

        observed = state.attributes.get("fan_mode")
        if observed is None:
            # Fan control is advertised but no current speed is reported (a
            # head still loading, or one that publishes its speed late). There
            # is nothing to reconcile a restored hold or the latch against, and
            # a write now would override a speed nobody can see — so no fan
            # decision this cycle. Restored truth stays pending and a standing
            # hold stays standing until the head reports a speed.
            return

        # Evaluate the manual-fan latch from what the head is actually reporting.
        held = self._observe_fan_latch(climate_id, observed, act, delta)
        self._publish_fan_state(climate_id)
        if held:
            return  # latched: leave the user's fan pick untouched

        if act in (MODE_COOL, MODE_HEAT) and not self.eco_idle:
            max_idx = self._fan_max_idx()
            idx = fan_for_delta(
                delta=delta,
                cur_idx=self._fan_idx.get(climate_id, 0),
                up_at=self.fan_up_at,
                down_at=self.fan_down_at,
                max_idx=max_idx,
            )
            self._fan_idx[climate_id] = idx
            token = FAN_LADDER[idx]
        elif act == MODE_FAN_ONLY or self.eco_idle:
            self._fan_idx.pop(climate_id, None)
            token = FAN_AUTO
        else:  # MODE_OFF
            self._fan_idx.pop(climate_id, None)
            return

        if token not in modes:
            return  # head lacks this ladder token -> skip safely
        if observed == token:
            return  # idempotent
        await self._write_fan(climate_id, token)

    def _fan_max_idx(self) -> int:
        """Ladder index the boost is allowed to reach, respecting fan_boost_max."""
        return (
            FAN_LADDER.index(self.fan_boost_max)
            if self.fan_boost_max in FAN_LADDER
            else len(FAN_LADDER) - 1
        )

    def _observe_fan_latch(
        self, climate_id: str, observed: str, act: str, delta: float
    ) -> bool:
        """Update and return the manual-fan latch for a head from its state.

        Called once per apply per head, guaranteed the head has fan_modes with an
        "auto" token and reports a current speed (``observed``). Latch
        transitions off that OBSERVED fan_mode:

        * a pending restore (the first speed observed after a restart, reload or
          standby release) is reconciled by ``_reconcile_restored_hold``:
          restored truth beats token guessing
        * observed "auto"                 -> released (user handed control back)
        * observed token != BOTH the last- and prior-commanded token -> latched
          (a user departure; the double-token check absorbs the echo race where
          the head still reports the token we wrote one cycle ago)
        * no _fan_cmd memory yet (first compute with nothing restored): a
          non-"auto" reading seeds LATCHED, mirroring the engage-latch's "resume
          from the head's own state" — a manual pick that predates the restart
          is honored — UNLESS the reading is a speed the ladder would hold at
          the current delta, which is our own boost speed echoing back (see
          ``_seed_matches_boost``), or our own fan_only idle on an established
          entry (see ``_seed_is_own_idle``).

        A hold ends ONLY on a gesture: the Fan-auto switch, or an observed "auto".
        Room drift, target changes, and slider moves between speeds never release
        one, at any speed including max.

        History, because this rule got simpler twice: through v2.17.0 a slider-set
        hold at the head's top token ALSO merged back into auto on any cycle where
        the ladder would have commanded max anyway — a hold releasing itself with
        no user gesture. Through v2.18.0 a *departure* to that top token was
        likewise read as "hand it back", because HomeKit's fan slider has no
        "auto" stop and a user who latched a zone from Apple Home had no on-slider
        way out. The per-zone Fan-auto switch bridges to HomeKit as a plain toggle
        and is that way out, so both readings of "max means give it back" are
        gone: max now holds like any other speed.

        Because the coordinator only sees observed state per cycle — not events —
        a re-gesture to the SAME token the head already reports is invisible; that
        limitation is accepted.
        """
        if climate_id in self._fan_on_pending:
            self._fan_on_pending.remove(climate_id)
            self._fan_restore.pop(climate_id, None)
            self._adopt_fan_speed(climate_id, observed)
            return False
        restored = self._fan_restore.pop(climate_id, None)
        if restored is not None:
            return self._reconcile_restored_hold(climate_id, observed, restored, act)

        if observed == FAN_AUTO:
            # Observed auto releases everything.
            self._fan_latched[climate_id] = False
            if climate_id not in self._fan_cmd:
                # Baseline stamp: without it a head idle-at-auto since startup
                # keeps an empty command memory (the return-to-auto write is
                # idempotency-skipped), so a LATER live pick would arrive as an
                # ambiguous seed instead of a clean departure.
                self._fan_prev[climate_id] = FAN_AUTO
                self._fan_cmd[climate_id] = FAN_AUTO
            return False

        seeding = climate_id not in self._fan_cmd
        departed = not seeding and observed not in (
            self._fan_cmd.get(climate_id),
            self._fan_prev.get(climate_id),
        )
        if not (seeding or departed):
            # A standing hold: no gesture this cycle, so nothing changes.
            return self._fan_latched.get(climate_id, False)

        # A seed at exactly the speed the ladder would command right now is our
        # own boost speed echoing back, not a manual pick -> adopt it and keep
        # driving. A DEPARTURE never adopts: every slider move is a hold.
        if seeding:
            if self._seed_is_own_idle(observed, act):
                # Baseline stamped so a slow echo of this token isn't a
                # departure; the return-to-auto write then proceeds.
                self._fan_latched[climate_id] = False
                self._fan_prev[climate_id] = observed
                self._fan_cmd[climate_id] = observed
                return False
            idx = self._seed_matches_boost(climate_id, observed, act, delta)
            if idx is not None:
                self._adopt_fan_speed(climate_id, observed, idx)
                return False

        # Seed latched (not at auto, not our rung); a departure always latches.
        self._hold_at(climate_id, observed)
        return True

    def _reconcile_restored_hold(
        self, climate_id: str, observed: str, held: bool, act: str
    ) -> bool:
        """Consume a restored hold truth against the first speed the head reports.

        The Fan-auto switch restores the held/not-held bool and, for a zone that
        was not held, the previous incarnation's command/observation baselines
        (already in ``_fan_cmd``/``_fan_prev``, see ``restore_fan_hold``). The
        token itself is always taken from the OBSERVED head.

        * observed "auto": released — including a hold the user let go of during
          the outage. Restored command memory stays, so a late report of a
          pre-restart token is still read as our own echo, not a departure.
        * held: still held, at the observed token (same token: the hold simply
          survived; different non-auto token: the user moved the hold during the
          outage — theirs either way).
        * not held: boost was driving. A token boost could never have written —
          outside the ladder, above the ceiling — holds, even if remembered:
          memory can contain user picks. An in-ceiling ladder token is ours
          when there is no memory, when it matches last/prior, or when the
          room is idling in fan_only under idle_action fan_only (#25): such a
          head can report a speed of its own while idling, and a hold there never
          self-corrects because the room never reads a clean "auto". Otherwise
          (an active room, another idle action) a nonmatching token
          conservatively holds; a stale dump can cause a false hold, so this is
          NOT proof of a manual gesture. The documented edge: a pick made while
          HA itself was down is indistinguishable from residue wherever the
          token is read as ours. Never a fixed-point guess: a mere change of
          delta is not a manual gesture. An active room resumes the ladder from
          the observed rung.
        """
        if observed == FAN_AUTO:
            self._fan_latched[climate_id] = False
            if climate_id not in self._fan_cmd:
                self._fan_prev[climate_id] = FAN_AUTO
                self._fan_cmd[climate_id] = FAN_AUTO
            return False
        if held:
            self._hold_at(climate_id, observed)
            return True
        last = self._fan_cmd.get(climate_id)
        remembered = observed in (last, self._fan_prev.get(climate_id))
        ours = (
            observed in FAN_LADDER
            and FAN_LADDER.index(observed) <= self._fan_max_idx()
            and (
                last is None
                or remembered
                or (act == MODE_FAN_ONLY and self.idle_action == IDLE_ACTION_FAN_ONLY)
            )
        )
        if not ours:
            self._hold_at(climate_id, observed)
            return True
        self._fan_latched[climate_id] = False
        if not remembered:
            # Baseline stamped so a slow echo of this token isn't a departure.
            self._fan_prev[climate_id] = observed
            self._fan_cmd[climate_id] = observed
        if act in (MODE_COOL, MODE_HEAT) and observed in FAN_LADDER:
            self._fan_idx[climate_id] = min(
                FAN_LADDER.index(observed), self._fan_max_idx()
            )
        return False

    def _hold_at(self, climate_id: str, observed: str) -> None:
        """Latch the head at the token it reports, as a STANDING hold.

        Records the held token as the command baseline so the zone stays held
        next cycle (the same trick async_set_fan_auto OFF uses). Without this
        the identical reading re-reads as a fresh seed/departure every cycle and
        the latch decision is re-litigated forever — which is how the v2.18.0
        "standing merge" removal was defeated for slider holds: the departure
        branch kept re-running the max handback, so a max hold still released
        itself on drift.
        """
        self._fan_latched[climate_id] = True
        self._fan_prev[climate_id] = observed
        self._fan_cmd[climate_id] = observed

    def _adopt_fan_speed(
        self, climate_id: str, observed: str, idx: int | None = None
    ) -> None:
        """Release the latch and adopt the max token into the boost's memory.

        Rolls _fan_prev, records the observed token as the last command, and pins
        _fan_idx so the ladder continues from where the head actually is: the
        natural ramp proceeds idempotently under DOWN_AT hysteresis, and the head
        doesn't read as a fresh departure and re-latch once the ladder steps off
        that token. ``idx`` is the ladder index to resume from — the seed adopt
        passes the index it matched; the Fan-auto switch passes none and resumes
        from the top, which only ever ramps down from there.
        """
        self._fan_latched[climate_id] = False
        self._fan_prev[climate_id] = self._fan_cmd.get(climate_id, observed)
        self._fan_cmd[climate_id] = observed
        self._fan_idx[climate_id] = self._fan_max_idx() if idx is None else idx

    def restore_fan_hold(
        self,
        climate_id: str,
        held: bool,
        *,
        last: str | None = None,
        prior: str | None = None,
        on_pending: bool = False,
    ) -> None:
        """Record the Fan-auto switch's restored pre-restart latch truth.

        Called from the switch's async_added_to_hass (RestoreEntity) — which
        runs during platform setup, BEFORE the coordinator's first compute —
        and consumed once by the seed, which reconciles it against the
        observed head state. Stale restores (older than the entry) are
        filtered by the switch and never reach here. With fan boost disabled
        the data is simply never consumed (the fan machinery is inert).

        ``last``/``prior`` are command/adopted-observation baselines from the
        previous incarnation, when the restored record carried them. For a
        zone that was NOT held they re-arm the echo tolerance across the
        restart, so a head that reports a pre-restart token late — after a
        provisional ``auto``, or once its missing speed arrives — is read as
        our own echo, not as a departure. A held zone's memory is the held
        token itself: the seed records it from the observed head, so nothing
        is carried for it (a hold released during the outage stays released,
        and a later non-auto report on it is a new hold either way).
        """
        self._fan_restore[climate_id] = held
        self._fan_restore_problem.pop(climate_id, None)
        self._fan_on_pending.discard(climate_id)
        # Only a strictly parsed fresh not-held record can carry this explicit
        # instruction. It adopts ONE first usable report, including a user
        # token above the ceiling, just like live ON; it proves no own echo.
        if on_pending is True and held is False:
            self._fan_on_pending.add(climate_id)
        if not held and last is not None:
            # Memory includes adopted user tokens, not just our commands.
            # S11b wins even when an impossible token is reported AFTER a
            # provisional auto. Such a token cannot arm restored echo tolerance.
            def eligible(token: str) -> str:
                if token in FAN_LADDER and FAN_LADDER.index(token) <= self._fan_max_idx():
                    return token
                return FAN_AUTO

            self._fan_cmd[climate_id] = eligible(last)
            self._fan_prev[climate_id] = eligible(last if prior is None else prior)

    def fan_command_memory(self, climate_id: str) -> tuple[str | None, str | None]:
        """The last two command/adopted-observation baselines: ``(last, prior)``.

        ``None`` where no baseline exists. Restore filters tokens outside the
        current ceiling/ladder to auto; this is echo-tolerance state, NOT an
        audit of sent or delivered commands. Published by the Fan-auto switch
        so the memory rides a restart (see ``restore_fan_hold``).
        """
        return (self._fan_cmd.get(climate_id), self._fan_prev.get(climate_id))

    def _publish_fan_state(self, climate_id: str) -> None:
        """Keep the existing clean-state channel current before yielding."""
        if writer := self._fan_state_writers.get(climate_id):
            writer()

    # -- fan-auto switch (the discoverable manual-hold handback) --------------
    def fan_auto_is_on(self, climate_id: str) -> bool:
        """True when boost drives this head's fan (zone NOT manually held).

        The switch is a live mirror of the latch: ON = auto/boost in charge,
        OFF = a manual speed is being held. The latch machinery is the single
        source of truth while running; across restarts the switch restores the
        held/not-held bool and hands it back via ``restore_fan_hold``.
        """
        if climate_id in self._fan_restore:
            return not self._fan_restore[climate_id]
        return not self._fan_latched.get(climate_id, False)

    async def async_set_fan_auto(self, climate_id: str, on: bool) -> None:
        """Drive the fan-auto switch: ON hands control back, OFF holds the speed.

        ON  -> release the latch and ADOPT the head's current observed token into
               the boost memory (the same trick as the max handback), so the
               still-at-manual-speed head doesn't read as a fresh departure and
               immediately re-latch; then recompute so boost reasserts promptly.
        OFF -> latch at whatever the head is doing right now (a deliberate hold).
               Latching "at auto" is meaningless, so an observed ``auto`` is a
               no-op (the switch stays ON) — documented, pinned in a test. We
               seed _fan_cmd/_fan_prev with the observed token so the observation
               path treats the hold as already-accounted-for, not a new departure.
               The hold sticks at any speed, max included, until the switch (or
               an observed ``auto``) releases it.

        With fan boost disabled the fan machinery is inert: OFF still records the
        latch (it simply has no effect until boost is re-enabled), and ON still
        clears it — the switch stays honest either way.
        """
        # With boost disabled, restored truth stays pending even with a live head.
        # A switch gesture supersedes that pending truth.
        state = self.hass.states.get(climate_id)
        observed = state.attributes.get("fan_mode") if state is not None else None
        if on:
            self._fan_restore_problem.pop(climate_id, None)
            self._fan_restore.pop(climate_id, None)
            self._fan_on_pending.discard(climate_id)
            if observed is not None:
                # Adopt the current speed so boost resumes from it without a
                # spurious re-latch (rolls _fan_prev, pins _fan_idx to max for a
                # clean DOWN_AT ramp-down).
                self._adopt_fan_speed(climate_id, observed)
            else:
                self._fan_latched[climate_id] = False
                self._fan_on_pending.add(climate_id)
            self._publish_fan_state(climate_id)
            await self.async_request_refresh()
            return
        # OFF: hold at the current speed. Auto is nothing to hold onto.
        if observed is None or observed == FAN_AUTO:
            if observed is None and climate_id in self._fan_on_pending:
                # Cancel the explicit waiting ON without guessing a token or
                # issuing a write. First non-auto report becomes the held
                # speed; observed auto still releases as usual. Known held
                # truth without a pending ON is left intact.
                self._fan_on_pending.discard(climate_id)
                self._fan_restore[climate_id] = True
                self._fan_latched[climate_id] = True
                self._publish_fan_state(climate_id)
                await self.async_request_refresh()
                return
            if observed == FAN_AUTO:
                self._fan_restore.pop(climate_id, None)
            _LOGGER.debug(
                "fan-auto OFF for %s ignored: head at %s (nothing to hold)",
                climate_id,
                observed,
            )
            return
        self._fan_restore.pop(climate_id, None)
        self._fan_prev[climate_id] = self._fan_cmd.get(climate_id, observed)
        self._fan_cmd[climate_id] = observed
        self._fan_latched[climate_id] = True
        self._fan_on_pending.discard(climate_id)
        self._publish_fan_state(climate_id)
        await self.async_request_refresh()

    def _seed_matches_boost(
        self, climate_id: str, observed: str, act: str, delta: float
    ) -> int | None:
        """Ladder index if a seed reading is our own boost speed, else ``None``.

        The latch seeds from whatever the head reports on the first compute after
        a restart, and a head boost had been driving is reporting OUR speed — not
        a manual pick. Latching that parks the zone silently (Fan auto reads OFF,
        boost stops driving it) until a human hands it back, after EVERY restart
        that catches a room conditioning — including the weekly update.

        So: while actively conditioning, if the observed token is a rung the
        ladder would STAY on at the current delta — a fixed point under the
        UP_AT/DOWN_AT hysteresis — it's a speed the boost could legitimately
        have parked the head at, so it's ours: adopt it and keep driving. The
        hysteresis makes this a BAND, not a single value (as a room closes in,
        DOWN_AT holds the fan a rung or two above what a cold read would pick;
        that is the boost's normal resting state, not a manual hold). Checking
        only the cold read (cur_idx=0) — as v2.19.0/beta.18 did — matched just
        the bottom rung of that band, so a restart mid-ramp-down still parked
        the head as "held".

        A real manual hold that predates the restart reads as a token OUTSIDE
        the band — above the boost ceiling, or past a hysteresis edge — so it
        still latches. The accepted tradeoff: a hold deliberately placed inside
        the band is indistinguishable from the boost's own speed and is lost to
        adoption. Only reachable on a seed — a departure is always a hold.
        """
        if act not in (MODE_COOL, MODE_HEAT) or self.eco_idle:
            return None
        if observed not in FAN_LADDER:
            return None
        obs_idx = FAN_LADDER.index(observed)
        if obs_idx > self._fan_max_idx():
            return None  # above the boost ceiling: never ours
        idx = fan_for_delta(
            delta=delta,
            cur_idx=obs_idx,
            up_at=self.fan_up_at,
            down_at=self.fan_down_at,
            max_idx=self._fan_max_idx(),
        )
        return obs_idx if idx == obs_idx else None

    def _seed_is_own_idle(self, observed: str, act: str) -> bool:
        """True if a seed with no restore data is our own fan_only idle (#25).

        idle_action="fan_only" drives a satisfied head to "auto" every cycle,
        yet a real head can report a concrete token while idling that way.
        Seeding that as a hold never self-corrects: unlike an active seed, the
        zone never leaves fan_only for a clean "auto" reading. So on a restart
        of an ESTABLISHED coordinator — this entry's own coordinator switch
        restored ON (it was running before), and the entry at least
        ``IDLE_SEED_MIN_ENTRY_AGE`` old — an in-ceiling ladder token boost
        could have written is read as ours. A first-ever compute (fresh
        install or re-added entry: nothing restored ON; or a first live
        enable, however long after install) keeps honoring a pre-install
        manual pick (S10). The accepted edge: a pick made while the zone
        idled in fan_only, followed by a restart that loses the Fan-auto
        restore, is indistinguishable and is not held.
        """
        if (
            act != MODE_FAN_ONLY
            or self.idle_action != IDLE_ACTION_FAN_ONLY
            or not self._restored_coordinator_on
            or observed not in FAN_LADDER
            or FAN_LADDER.index(observed) > self._fan_max_idx()
        ):
            return False
        created = self.config_entry.created_at
        return created is not None and dt_util.utcnow() - created >= timedelta(
            seconds=IDLE_SEED_MIN_ENTRY_AGE
        )

    async def _write_fan(self, climate_id: str, token: str) -> None:
        """Issue a fan_mode write and remember it (last + prior, for the echo race).

        A REWRITE of the token already commanded (the idempotency path retrying
        a write a slow head hasn't applied yet) must NOT roll the memory: prev
        keeps the pre-write token for as long as the head lags, so the stale
        reading stays echo-tolerated instead of counting as a user departure.
        Rolling it was #14 — at the satisfied auto-handback, a compute burst
        rewrote auto until prev==cmd==auto and the head's own stale boost token
        latched as a manual hold with no user anywhere near it.

        prev therefore survives until the next DISTINCT command — the original
        design's invariant. The cost is unchanged from what the README already
        documents: a user re-pick of the token prev remembers is invisible.
        """
        if token != self._fan_cmd.get(climate_id):
            self._fan_prev[climate_id] = self._fan_cmd.get(climate_id, token)
            self._fan_cmd[climate_id] = token
        self._publish_fan_state(climate_id)
        await self.async_head_service(
            climate_id, "set_fan_mode",
            {"fan_mode": token},
        )

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
        if self._retired:
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
            # The service path records wake ownership immediately before
            # issuance, after it has acquired this head's delivery lock.
            await self.async_head_service(
                climate_id, "set_hvac_mode",
                {"hvac_mode": MODE_FAN_ONLY}, vane=True,
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
            await self.async_head_service(
                climate_id, "set_hvac_mode",
                {"hvac_mode": MODE_OFF}, vane=True,
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

    def _owns_vane_kick(self, climate_id: str) -> bool:
        """True while the calling task still owns this head's live kick."""
        return (
            not self._retired
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
            observed = self._command_observations.get(climate_id, {})
            if observed.get("_task") is task and observed.get("command_status") == "pending":
                observed.update(
                    command_retired_at=dt_util.utcnow().isoformat(), command_ownership_retired=True,
                )
        self._publish_delivery()
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
            pending = asyncio.Event()
            self._vane_cleanup_pending[climate_id] = pending
            try:
                # Keep this exception inside the own-wake/reported-mode guard.
                # Its return must not overwrite an outstanding wake's status.
                observed = self._command_observations.setdefault(climate_id, {})
                cleanup = observed.setdefault("vane_retirement_cleanup", {})
                await self._call_head_service(
                    climate_id, "set_hvac_mode",
                    {"hvac_mode": MODE_OFF}, observation=cleanup,
                )
            except HomeAssistantError as err:
                _LOGGER.error(
                    "MXZ: parking %s after its vane kick failed "
                    "(zone degraded, others continue): %s",
                    climate_id,
                    err,
                )
            finally:
                self._vane_cleanup_pending.pop(climate_id, None)
                pending.set()

    async def _select_option(self, vane_id: str, option: str) -> None:
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": vane_id, "option": option},
            blocking=True,
        )

    # -- triggers -----------------------------------------------------------
    @callback
    def _on_input_change(self, event: Event) -> None:
        """A temp sensor changed -> observe the write, then recompute.

        Every write that can carry a new sample marker lands here: a marker
        that moved is an attribute that changed, whatever the temperature did.
        """
        self._observe_input(event)
        generation = self._refresh_generation

        async def _request_current_input() -> None:
            # A decision since this receipt already read the current sensor
            # states. Do not turn the rest of that burst into a delivery retry.
            if self._refresh_generation == generation:
                await self.async_request_refresh()

        self.hass.async_create_task(_request_current_input(), eager_start=False)

    @callback
    def _on_input_report(self, event: Event) -> None:
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
    def _observe_input(self, event: Event) -> None:
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
        self._unsubs.append(
            async_call_later(self.hass, STARTUP_RECOVER_DELAY, self._on_startup_timer)
        )

    @callback
    def _on_startup_timer(self, _now: Any) -> None:
        # Startup has settled: this recompute logs any room still unusable.
        self._startup_quiet = False
        self.hass.async_create_task(self.async_request_refresh())

    # -- self-heal A (band drift) -------------------------------------------
    @callback
    def _on_head_change(self, event: Event) -> None:
        """Track head inputs and detect banned-mode/off-while-enabled drift."""
        entity_id: str = event.data["entity_id"]
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        mode = new_state.state if new_state else None
        refresh_needed = False
        pending = next(
            (zone for zone in self.zones
             if zone.climate_id == entity_id and zone.slug in self._startup_resume),
            None,
        )
        if pending is not None:
            if mode == MODE_OFF and self._planned_act_for(entity_id) != MODE_OFF:
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
            active = self._deliveries.get(entity_id)
            if active is None or active["act"] == MODE_OFF:
                for zone in self.zones:
                    if (
                        zone.climate_id == entity_id
                        and self._hands_off_phase(zone) == "parking"
                    ):
                        self._hands_off_released.add(zone.slug)
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
        ):
            for zone in self.zones:
                if (
                    zone.climate_id == entity_id
                    and self._hands_off_phase(zone) == "floor"
                    and mode != self._planned_act_for(entity_id)
                ):
                    self._floor_yield_to_person(zone, dt_util.utcnow().timestamp())
                    refresh_needed = True
        self._arm_or_cancel(
            entity_id, "band",
            mode in BANNED_MODES and not self._hands_off_head(entity_id),
            BAND_DRIFT_DELAY,
        )
        # Never heal a head the standby hold parked off (nor a wall override
        # mid-hold — a human turning a parked head on during an outage wins),
        # and never heal a head the PLAN itself parked off (idle_action) — but
        # a wall-remote off during an active call or a coil-dry dwell is still
        # drift (the plan wants that head awake).
        off_drift = (
            mode == MODE_OFF
            and self._enable_for(entity_id)
            and not self.eco_idle
            and not self.inhibited
            and self._planned_act_for(entity_id) != MODE_OFF
        )
        self._arm_or_cancel(entity_id, "off", off_drift, OFF_WHILE_ENABLED_DELAY)

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
        if refresh_needed or new_fan != old_fan or capabilities_changed or availability_changed:
            if (
                self._needs_legacy_refresh_guard
                and self._plan_lock.locked()
                and not self._debounced_refresh._execute_lock.locked()
                and not self._retired
            ):
                # A direct refresh leaves HA's debouncer free. Its tracked
                # task waits for the plan, keeping this echo in the same drain.
                self._refresh_wake.set()
                self.hass.async_create_task(super().async_request_refresh())
            else:
                self.hass.async_create_task(self._async_request_ordinary_refresh())

    @callback
    def _arm_or_cancel(
        self, entity_id: str, kind: str, condition: bool, delay: int
    ) -> None:
        """(Re)arm a debounce timer for a drift condition, or cancel it if cleared."""
        key = (entity_id, kind)
        if (existing := self._heal_timers.pop(key, None)) is not None:
            existing()
        if condition:
            self._heal_timers[key] = async_call_later(
                self.hass, delay, partial(self._on_heal_timer, key)
            )

    @callback
    def _on_heal_timer(self, key: tuple[str, str], _now: Any) -> None:
        """A drift condition persisted past its debounce window."""
        self._heal_timers.pop(key, None)
        entity_id, kind = key
        state = self.hass.states.get(entity_id)
        mode = state.state if state else None
        if kind == "band":
            still = mode in BANNED_MODES and not self._hands_off_head(entity_id)
        else:
            still = (
                mode == MODE_OFF
                and self._enable_for(entity_id)
                and not self.eco_idle
                and self._planned_act_for(entity_id) != MODE_OFF
            )
        if still and self.coordinator_enable and not self.inhibited:
            self.hass.async_create_task(self._heal_and_notify())

    async def _heal_and_notify(self) -> None:
        await self.async_request_refresh()
        await self._notify(
            "A head was off its coordinated mode (drift or just re-enabled) - "
            "re-applied via the coordinator."
        )

    async def _notify(self, message: str) -> None:
        if not self.notify_service:
            return
        domain, _, service = self.notify_service.partition(".")
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
    def _hands_off_phase(self, zone: Zone) -> str | None:
        """None unless the room is opted in AND disabled; then parking|released,
        or floor while its tripped floor has MXZ running the head."""
        if not zone.hands_off or zone.enable:
            return None
        if self.floor_tripped(zone.slug):
            return "floor"
        return "released" if zone.slug in self._hands_off_released else "parking"

    def _hands_off_head(self, climate_id: str) -> bool:
        """True when this head belongs to a released hands-off room.

        A person's dry or auto on a floor-run head is not healed either: the
        yield in :meth:`_on_head_change` releases the room before the band
        check reads this.
        """
        return any(
            zone.climate_id == climate_id and self._hands_off_phase(zone) == "released"
            for zone in self.zones
        )

    def _hands_off_vote(self, zone: Zone) -> str | None:
        """What a released hands-off head is running, if it is heat or cool.

        Before the off lands, the head's mode is ours or stale, so it has no
        vote. Every other reported state (fan_only, dry, auto, heat_cool, off,
        unavailable) asks nothing of the outdoor unit's direction.
        """
        if self._hands_off_phase(zone) != "released" or self.inhibited:
            return None  # (a standby hold is holding it off)
        head = self.hass.states.get(zone.climate_id)
        if head is not None and head.state in (MODE_COOL, MODE_HEAT):
            return head.state
        return None

    @staticmethod
    def _zone_arbitration_pair(
        *, demand: str, vote: str | None, safety: bool, index: int,
        floor: str | None = None,
    ) -> tuple[Demand, ...]:
        """One zone's (demand, vote[, floor]) as :class:`Demand` entries.

        Shared by ``_compute`` (live) and ``_sync_dwell_timer`` (replayed from
        the already-computed plan) so the priority-tier math lives in exactly
        one place. A tripped floor is a third, commanding entry: a freeze
        floor's heat at PRIORITY_FLOOR_HEAT, a ceiling's cool at
        PRIORITY_SAFETY (see :func:`logic.arbitrate`). It is independent of
        the room's own Safety priority sensor.
        """
        tier = PRIORITY_SAFETY if safety else None
        pair = (
            Demand(
                direction=demand if demand in (MODE_COOL, MODE_HEAT) else None,
                priority=(tier if tier is not None else PRIORITY_DEMAND, index),
            ),
            Demand(
                direction=vote,
                priority=(tier if tier is not None else PRIORITY_VOTE, index),
                read_only=True,
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

    def restore_hands_off(self, slug: str) -> None:
        """The enable switch restored a released hands-off room (see switch.py)."""
        self._hands_off_released.add(slug)

    def hands_off_released(self, slug: str) -> bool:
        """Whether this room's park landed (what the enable switch persists)."""
        return slug in self._hands_off_released

    # -- freeze floor / overheat ceiling (opt-in, disabled rooms) -------------
    def floor_tripped(self, slug: str) -> bool:
        """Whether this room's floor is tripped and wants its direction now."""
        run = self._floor_runs.get(slug)
        return run is not None and not run.stalled

    def _floor_clear(self, zone: Zone, direction: str) -> float:
        """The clear line of a run: the room is fine again from here."""
        if direction == MODE_HEAT:
            return float(zone.floor_low) + self._floor_margin
        return float(zone.floor_high) - self._floor_margin

    def _floor_want(self, zone: Zone, temp: float, ok: bool, now: float) -> str | None:
        """Advance this room's floor by one compute; return its want, if any.

        Only a disabled room with a floor or ceiling configured can have one.
        No usable reading (invalid, missing or stale: ``ok`` is the demand
        path's own test) or an unavailable head ends a run at once and casts
        nothing: a floor never acts blind (decision 4). A run that MXZ has
        driven for the cap without a step of new best reading is ``blind`` as
        well, until the room clears or reaches a new low (decision 11). The
        clock is charged only for time the LAST compute found the head being
        driven (see :meth:`_floor_settle`), so a wait, a standoff loss,
        Coordinator off or Home Assistant downtime costs nothing.
        """
        slug = zone.slug
        # Driven time is counted on the monotonic clock, so a wall-clock step
        # cannot stall a run early or hold one back. ``now`` (wall clock)
        # stays for the yield, which is stored and shown as a time.
        clock = time.monotonic()
        driven_for = (
            clock - self._floor_eval_ts[slug]
            if self._floor_driving.get(slug) and slug in self._floor_eval_ts
            else 0.0
        )
        self._floor_eval_ts[slug] = clock
        self._floor_driving[slug] = False
        configured = zone.floor_low is not None or zone.floor_high is not None
        if not self._floor_live:
            # The seed compute: nothing is restored yet, so nothing is decided.
            self._floor_view[slug] = ("armed" if configured else "none", None)
            return None
        self._floor_flush_handback(zone, self.hass.states.get(zone.climate_id))
        if not configured or zone.enable:
            self._floor_end(slug)
            self._floor_yield.pop(slug, None)
            self._floor_blind_noticed.discard(slug)
            self._floor_view[slug] = ("enabled" if configured else "none", None)
            return None
        head = self.hass.states.get(zone.climate_id)
        # Before anything ends the run: the end gives back their latest.
        self._floor_follow_person(zone, head)
        if not ok:
            self._floor_end(slug, hand_back=True)
            self._floor_blind(
                zone, "Room sensor unavailable, invalid or stale: no floor "
                "protection until it reports a usable reading."
            )
            return None
        if head is None or head.state in UNAVAILABLE_STATES:
            self._floor_end(slug, hand_back=True)
            self._floor_blind(
                zone, f"Head {zone.climate_id} unavailable: no floor protection "
                "until it reports again."
            )
            return None
        if (yielded := self._floor_yield.get(slug)) is not None:
            direction, since = yielded
            cleared = floor_call(
                temp=temp, sensor_ok=True, low=zone.floor_low, high=zone.floor_high,
                prior=direction, margin=self._floor_margin,
            ) != direction
            if not cleared and now - since < FLOOR_YIELD_SECONDS:
                self._floor_blind_noticed.discard(slug)
                rearm = dt_util.utc_from_timestamp(since + FLOOR_YIELD_SECONDS)
                self._floor_view[slug] = ("yielded", (
                    "A person changed the head during a floor run, so MXZ left "
                    "it to them. The floor re-arms when the room reaches its "
                    f"clear line or at {rearm.isoformat(timespec='minutes')}."
                ))
                return None
            self._floor_yield.pop(slug)
        run = self._floor_runs.get(slug)
        was_stalled = run is not None and run.stalled
        if run is not None:
            run = floor_progress(
                run, temp=temp, driven_for=driven_for,
                step=self.target_step, cap=FLOOR_STALL_SECONDS,
            )
        want = floor_call(
            temp=temp, sensor_ok=True, low=zone.floor_low, high=zone.floor_high,
            prior=run.direction if run is not None else None,
            margin=self._floor_margin,
        )
        if run is not None and want != run.direction:
            # Cleared (or crossed straight to the other trip).
            self._floor_end(slug, hand_back=True)
            run = None
        if run is None:
            if want is None:
                self._floor_blind_noticed.discard(slug)
                self._floor_view[slug] = ("armed", None)
                return None
            run = FloorRun(direction=want, mark=temp, worst=temp)
            self._floor_notify_trip(zone, want, temp)
        elif was_stalled and not run.stalled:
            self._floor_notify_trip(zone, run.direction, temp)  # a new low re-armed it
        elif not run.stalled and (held := self._floor_trip_held.get(slug)) is not None:
            if self._floor_send(zone, "trip", held):  # the hour is up (P8)
                del self._floor_trip_held[slug]
        self._floor_runs[slug] = run
        if run.stalled:
            self._floor_hand_back(slug)
            step = f"{self.target_step:g} {self._unit_symbol()}"
            self._floor_blind(
                zone, f"Stalled: {FLOOR_STALL_SECONDS // 3600} h of driving the head "
                f"without {step} of progress. Re-arms when the room reaches its "
                f"clear line or a new {'low' if run.direction == MODE_HEAT else 'high'}.",
                kind="stalled",
            )
            return None
        self._floor_blind_noticed.discard(slug)
        if zone.hands_off:
            # The run is MXZ's: the room parks again when it ends (decision 5),
            # unless MXZ took the head over a person's own run that way (P3).
            if slug in self._hands_off_released:
                self._floor_take_over(zone, run.direction, head)
            self._hands_off_released.discard(slug)
        self._floor_view[slug] = (run.direction, None)  # settled after arbitration
        return run.direction

    def _floor_settle(self, floors: list[str | None], state: str) -> None:
        """After arbitration: is each tripped floor driven now, or held?

        Driven means the plan runs the head in the floor's direction and the
        apply will send it: Coordinator on, no fixed standby park. Only then
        does the no-progress clock run until the next compute.
        """
        active = self.coordinator_enable and not self._parked_by_standby()
        for zone, want in zip(self.zones, floors):
            if want is None:
                continue
            driving = active and want == state
            self._floor_driving[zone.slug] = driving
            if driving:
                continue
            if not self.coordinator_enable:
                reason = "Coordinator is off: MXZ sends nothing."
            elif self._parked_by_standby():
                reason = "A standby hold parks every head."
            else:
                reason = (
                    f"Waiting: the shared mode is {state} (the mode-change wait, or "
                    "a higher-ranked want). The head is held off meanwhile."
                )
            self._floor_view[zone.slug] = ("held", reason)

    def _floor_end(self, slug: str, *, hand_back: bool = False) -> None:
        """End this room's run, if any. A hands-off room then parks
        (decision 5), unless ``hand_back`` and the run was taken over from a
        person: see :meth:`_floor_hand_back`. Without ``hand_back`` (the room
        was enabled, the floor removed, a person changed the head) the
        person's earlier run is forgotten."""
        self._floor_runs.pop(slug, None)
        self._floor_trip_held.pop(slug, None)
        self._floor_reject_noticed.discard(slug)
        self._floor_rejecting.pop(slug, None)
        self._floor_expect.pop(slug, None)
        if hand_back:
            self._floor_hand_back(slug)
        else:
            self._floor_prior.pop(slug, None)

    def _floor_take_over(self, zone: Zone, direction: str, head: Any) -> None:
        """A hands-off run starts on a released room. If the person is
        running the head in the floor's direction already, remember their
        mode and setpoint to give back (P3). A head with no setpoint to read
        gets the clear line's."""
        if head.state != direction:
            return  # off, another mode: decision 5 (back to off)
        if (setting := _floor_setting(head)) is None:
            setting = self._floor_line(zone, direction)
        self._floor_prior[zone.slug] = (direction, *setting)

    def _floor_line(self, zone: Zone, direction: str) -> tuple[float, float]:
        """The setpoints of a run's clear line, as :meth:`_apply` computes
        them before it keeps a person's warmer (cooler) ones."""
        return setpoints(
            mode=direction, target=self._floor_clear(zone, direction), eco=False,
            clamp_min=self.clamp_min, clamp_max=self.clamp_max,
            band=self.setpoint_band, step=self.target_step,
        )

    def _floor_as_sent(
        self, zone: Zone, head: Any, mode: str, low: float, high: float
    ) -> tuple[float, float]:
        """(low, high) as the head should show them once MXZ sends them:
        clamped to the head's band, and its one setpoint on a single-setpoint
        head, as :meth:`_apply_head` sends them."""
        low, high = self._clamp_to_head_band(zone.climate_id, low, high)
        if head is not None and not (
            int(head.attributes.get("supported_features") or 0) & 2
        ):
            low = high = high if mode == MODE_COOL else low
        return (low, high)

    def _floor_expect_command(
        self, zone: Zone, mode: str, low: float, high: float
    ) -> None:
        """MXZ queued (low, high) for a head it runs over a person's own run:
        remember what the head should show once it lands
        (:meth:`_floor_as_sent`) and what it showed until then. Neither is a
        person's change (P15). The same command to a head already showing it
        replaces nothing, so what it showed before stays."""
        head = self.hass.states.get(zone.climate_id)
        low, high = self._floor_as_sent(zone, head, mode, low, high)
        setting = None if head is None else _floor_setting(head)
        known = self._floor_expect.get(zone.slug)
        if known is not None and known[0] == (low, high) and (
            self._floor_shows(zone.climate_id, setting, (low, high))
        ):
            return
        self._floor_expect[zone.slug] = ((low, high), setting)

    def _floor_shows(
        self, climate_id: str, setting: tuple | None, want: tuple[float, float]
    ) -> bool:
        """Whether a head showing ``setting`` already holds ``want``, read on
        its own grid as the idempotent check reads it."""
        return setting is not None and all(
            self._head_already_at(climate_id, seen, edge)
            for seen, edge in zip(setting, want)
        )

    def _floor_follow_person(self, zone: Zone, head: Any) -> None:
        """A person changed only the setpoints of a head MXZ runs over their
        own run: that is their setting now. The run keeps it where it protects
        more (P10), and it is what they get back at the end (P15).

        It is a person's change only if the head is in the run's mode, MXZ has
        nothing in flight to it and is not kicking its vanes, and it shows
        neither MXZ's last command nor the run's clear line (on the head's own
        grid, as the idempotent check reads it), nor what it showed before
        that last command (a late report from before it landed). MXZ may have
        sent the clear line at any point in the run, so a late report of it
        is never theirs (P17), even after they changed it since. Until MXZ
        has commanded the head in this run (after a restart, its first
        apply), nothing is taken.
        """
        slug = zone.slug
        prior, expect = self._floor_prior.get(slug), self._floor_expect.get(slug)
        if (
            prior is None or expect is None or head is None
            or head.state != prior[0]
            or zone.climate_id in self._deliveries
            or zone.climate_id in self._vane_kicks
        ):
            return
        sent, before = expect
        setting = _floor_setting(head)
        line = self._floor_as_sent(
            zone, head, prior[0], *self._floor_line(zone, prior[0])
        )
        if setting is None or setting in (before, prior[1:]) or any(
            self._floor_shows(zone.climate_id, setting, ours)
            for ours in (sent, line)
        ):
            return
        self._floor_prior[slug] = (prior[0], *setting)
        _LOGGER.info(
            "MXZ floor: %s's setpoint was changed by hand during a floor run; "
            "MXZ keeps it where it protects the room and gives it back at the "
            "end", zone.name,
        )

    def _floor_hand_back(self, slug: str) -> None:
        """A hands-off run that MXZ took over from a person's own run in the
        floor's direction ended without that person changing the head's mode
        (cleared, blind or stalled): it is theirs again at once, and the next
        apply sends it their mode and latest setpoint, never off (P3, P15)."""
        self._floor_expect.pop(slug, None)
        if (prior := self._floor_prior.pop(slug, None)) is not None:
            self._hands_off_released.add(slug)
            self._floor_handback[slug] = (prior, 1, None)

    def _floor_blind(self, zone: Zone, reason: str, kind: str = "blind") -> None:
        """Show the room ``blind``; notify once per episode (decision 7).

        A configured room can be blind before anything tripped: it has no
        protection either way. While Home Assistant starts, an unusable sensor
        or head may only be loading, so the notice waits for the post-start
        recompute, which sends it if the room is still blind. A notice the
        rate limit holds back is tried again at each compute while the room
        stays blind, so it goes out once the hour is up.
        """
        self._floor_view[zone.slug] = ("blind", reason)
        if zone.slug in self._floor_blind_noticed or self._startup_quiet:
            return
        if self._floor_send(
            zone, kind, f"{zone.name}: the freeze/overheat floor is blind. {reason}"
        ):
            self._floor_blind_noticed.add(zone.slug)

    def _floor_notify_trip(self, zone: Zone, direction: str, temp: float) -> None:
        """Notify a trip (decision 7). One the hourly limit holds back is kept
        and sent at the first compute after the hour while the run is still
        going (P8); a run that ends or stalls first never sends it."""
        unit = self._unit_symbol()
        if direction == MODE_HEAT:
            what = f"below its freeze floor of {zone.floor_low:g} {unit}"
            act = "heating"
        else:
            what = f"above its overheat ceiling of {zone.floor_high:g} {unit}"
            act = "cooling"
        message = (
            f"{zone.name} is at {temp:g} {unit}, {what}. MXZ is {act} this disabled "
            f"room until it reaches {self._floor_clear(zone, direction):g} {unit}."
        )
        if self._floor_send(zone, "trip", message):
            self._floor_trip_held.pop(zone.slug, None)
        else:
            self._floor_trip_held[zone.slug] = message

    def _floor_attributes(self, zone: Zone) -> dict[str, str | None]:
        """The room's floor state as the plan shows it. A driven floor whose
        head is rejecting MXZ's commands is held, not running."""
        state, reason = self._floor_view[zone.slug]
        err = self._floor_rejecting.get(zone.slug)
        if state in (MODE_HEAT, MODE_COOL) and err is not None:
            state, reason = "held", (
                f"{zone.climate_id} rejected MXZ's floor command ({err}). MXZ "
                "keeps trying at each update."
            )
        return {"floor_state": state, "floor_reason": reason}

    def _floor_rejected(
        self, climate_id: str, err: Exception, active: dict[str, Any]
    ) -> None:
        """A command to a head whose floor is tripped was rejected: the plan
        shows it held (republished as the delivery ends), and it is notified
        once per run (decision 7); the next ordinary update tries again. A
        notice the rate limit holds back goes out at the first rejection
        after the hour is up. A refused hand-back: see
        :meth:`_floor_handback_refused`."""
        for zone in self.zones:
            sending = self._floor_handing_back.get(zone.slug)
            if sending is not None and sending[3] is active:
                del self._floor_handing_back[zone.slug]
                self._floor_handback_refused(zone, sending, err)
                continue
            if (
                zone.climate_id == climate_id
                and not zone.enable
                and self.floor_tripped(zone.slug)
            ):
                self._floor_rejecting[zone.slug] = str(err)
                if zone.slug in self._floor_reject_noticed:
                    continue
                if self._floor_send(
                    zone, "rejected",
                    f"{zone.name}: the floor's command to {climate_id} was rejected "
                    f"({err}). MXZ keeps trying at each update.",
                ):
                    self._floor_reject_noticed.add(zone.slug)

    def _floor_accepted(self, climate_id: str, active: dict[str, Any]) -> None:
        """A command to this head returned (or was not needed): it is no
        longer rejecting MXZ's floor, and a hand-back it carried landed."""
        for zone in self.zones:
            if zone.climate_id == climate_id:
                self._floor_rejecting.pop(zone.slug, None)
                sending = self._floor_handing_back.get(zone.slug)
                if sending is not None and sending[3] is active:
                    del self._floor_handing_back[zone.slug]

    def _floor_handback_refused(
        self, zone: Zone, sending: tuple, err: Exception
    ) -> None:
        """The head refused the command handing it back to its person (P3).
        It is tried once more at the next apply, if the room is still theirs
        and the head still shows MXZ's command; refused again, a floor notice
        says so (NIT-1), and MXZ sends that head nothing more for it."""
        (mode, low, high), attempt, seen, _ = sending
        if attempt == 1:
            self._floor_handback[zone.slug] = ((mode, low, high), 2, seen)
            return
        unit = self._unit_symbol()
        setting = f"{low:g} {unit}" if low == high else f"{low:g}-{high:g} {unit}"
        message = (
            f"{zone.name}: the floor run is over, but {zone.climate_id} refused "
            f"MXZ's command giving it back to {mode} at {setting}, twice ({err}). "
            "It keeps MXZ's floor setting; MXZ sends it nothing more for this."
        )
        self._floor_handback_note[zone.slug] = (message, seen)
        self._floor_flush_handback(zone, self.hass.states.get(zone.climate_id))

    def _floor_flush_handback(self, zone: Zone, head: Any) -> None:
        """Send a refused hand-back's notice; one the hourly limit holds back
        goes out at the first compute after the hour (P5, P8), but only while
        the room is still released and the head still shows MXZ's command."""
        if (held := self._floor_handback_note.get(zone.slug)) is None:
            return
        message, seen = held
        if self._hands_off_phase(zone) != "released" or head is None or (
            _head_setting(head) != seen
        ):
            del self._floor_handback_note[zone.slug]  # no longer true
        elif self._floor_send(zone, "handback", message):
            del self._floor_handback_note[zone.slug]

    def _floor_send(self, zone: Zone, kind: str, message: str) -> bool:
        """Log one floor notice and send it; return whether it went out.

        At most one floor notice per room per hour, whatever its kind (trip,
        blind, stalled, rejected), so a flapping sensor or head cannot flood
        anyone (P5). A held-back notice is logged at WARNING, once, saying so.
        It goes to the integration's notification service; with none set, one
        that names no service, or one Home Assistant does not have (a removed
        app, a typo), it becomes a Home Assistant notification instead (P2,
        P11), one per room and kind, so a repeat replaces the last.
        """
        slug = zone.slug
        now = time.monotonic()
        last = self._floor_notice_at.get(slug)
        if last is not None and now - last < FLOOR_NOTICE_SECONDS:
            held = self._floor_held_back.setdefault(slug, set())
            if message not in held:
                held.add(message)
                _LOGGER.warning(
                    "MXZ floor: %s (not sent: rate-limited to one floor notice "
                    "per room per hour)", message,
                )
            return False
        _LOGGER.warning("MXZ floor: %s", message)
        if self._retired:
            return False
        self._floor_notice_at[slug] = now
        self._floor_held_back.pop(slug, None)
        domain, _, service = (self.notify_service or "").partition(".")

        async def _send() -> None:
            try:
                if domain and service:
                    try:
                        await self._notify(message)
                        return
                    except ServiceNotFound:
                        pass  # not registered: a Home Assistant notification
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "HVAC Update",
                        "message": message,
                        "notification_id": (
                            f"{DOMAIN}_{self.config_entry.entry_id}_floor_{slug}_{kind}"
                        ),
                    },
                    blocking=False,
                )
            except HomeAssistantError as err:
                _LOGGER.error("MXZ floor: the notice could not be sent: %s", err)

        self.hass.async_create_task(_send())
        return True

    def _unit_symbol(self) -> str:
        return "°C" if self.celsius else "°F"

    def _floor_yield_to_person(self, zone: Zone, now: float) -> None:
        """A person changed a hands-off head mid-run: MXZ stands down (8b).

        The room is released at once (the head is theirs), and the floor
        re-arms when the room reaches its clear line or after a while.
        """
        run = self._floor_runs.get(zone.slug)
        if run is None:
            return
        self._floor_end(zone.slug)  # their earlier run is not handed back
        self._floor_yield[zone.slug] = (run.direction, now)
        self._hands_off_released.add(zone.slug)
        # A command of ours still waiting to be sent (an off during the wait)
        # would land over theirs: disown it, as a superseded one is. One
        # already sent cannot be called back.
        active = self._deliveries.get(zone.climate_id)
        if active is not None and active["owner"] is self:
            active["superseded"] = True
            self._publish_delivery()
        _LOGGER.info(
            "MXZ floor: %s's head was changed by hand during a floor run; "
            "MXZ leaves it to that person until the floor re-arms", zone.name,
        )

    def floor_record(self, slug: str) -> dict[str, Any]:
        """What the enable switch persists for this room's floor (may be empty).

        A record exists only while a run or a yield does, so a room with no
        floor stores exactly what it stored before floors existed.
        """
        record: dict[str, Any] = {}
        if (run := self._floor_runs.get(slug)) is not None:
            record["floor_run"] = {
                "direction": run.direction, "mark": run.mark, "worst": run.worst,
                "driven": run.driven, "stalled": run.stalled,
                "window": [[at, seen] for at, seen in run.recent],
            }
        if (prior := self._floor_prior.get(slug)) is not None:
            record["floor_prior"] = {"mode": prior[0], "low": prior[1], "high": prior[2]}
        if (yielded := self._floor_yield.get(slug)) is not None:
            record["floor_yield"] = {"direction": yielded[0], "at": yielded[1]}
        return record

    def restore_floor(self, slug: str, data: dict[str, Any]) -> None:
        """The enable switch restored a disabled room's floor record.

        Only a well-formed record for a floor still configured is taken; any
        other is dropped, and the room's first compute decides afresh (a
        hands-off room then parks, since a run had un-released it). Downtime
        is not charged to the no-progress clock. A run without a readable
        ``window`` (as 3.7.0 betas before P9 stored it) restarts its re-arm
        window from the next reading.
        """
        zone = next((z for z in self.zones if z.slug == slug), None)
        if zone is None:
            return

        def configured(direction: Any) -> bool:
            return (direction == MODE_HEAT and zone.floor_low is not None) or (
                direction == MODE_COOL and zone.floor_high is not None
            )

        run = data.get("floor_run")
        if isinstance(run, dict) and configured(run.get("direction")):
            numbers = [_finite(run.get(key)) for key in ("mark", "worst", "driven")]
            if None not in numbers and isinstance(run.get("stalled"), bool):
                mark, worst, driven = numbers
                self._floor_runs[slug] = FloorRun(
                    direction=run["direction"], mark=mark, worst=worst,
                    driven=max(driven, 0.0), stalled=run["stalled"],
                    recent=_floor_window(run.get("window")),
                )
                prior = data.get("floor_prior")
                if isinstance(prior, dict) and prior.get("mode") == run["direction"]:
                    low, high = _finite(prior.get("low")), _finite(prior.get("high"))
                    if low is not None and high is not None:
                        self._floor_prior[slug] = (run["direction"], low, high)
        yielded = data.get("floor_yield")
        if isinstance(yielded, dict) and configured(yielded.get("direction")):
            since = _finite(yielded.get("at"))
            if since is not None:
                self._floor_yield[slug] = (yielded["direction"], since)

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
        return min(max(float(zone.drift), lo), hi)

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
        offers = self._range_offers.pop(zone.slug, {})
        bounds = self.head_target_bounds(zone.climate_id)
        fresh = zone.handoff is not None and zone.handoff.get("seed_range") is True
        if not fresh and offers.get(True) is not None and offers.get(False) is not None:
            low, high = offers[True], offers[False]
        else:
            try:
                low, high = shift_range(
                    width=self.dual_min_gap, center=zone.target,
                    bounds=bounds, step=self.target_step,
                )
            except RangeError:
                low, high = bounds  # a head band narrower than the gap
        zone.target_low, zone.target_high = float(low), float(high)
        self.finish_range_restore(zone)
        self.async_update_listeners()

    def finish_range_restore(self, zone: Zone) -> None:
        """Re-check a dual room's range once both of its numbers have restored.

        Each edge is clamped into the head's current bounds. If that leaves
        the pair narrower than the minimum gap, the range is re-centered at
        the minimum gap around its midpoint (sliding inside the bounds), and
        the room's detail says so once, until the range is next written.
        """
        if zone.target_low is None or zone.target_high is None:
            return
        lo, hi = self.head_target_bounds(zone.climate_id)
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
        if slug in self._startup_resume or not self.coordinator_enable or not any(
            zone.slug == slug and zone.enable for zone in self.zones
        ):
            self._startup_resume.pop(slug, None)
            self._engage_latch[slug] = ""
        else:
            self._engage_latch.pop(slug, None)

    async def async_user_changed(self) -> None:
        """A helper entity changed by the user -> recompute + act."""
        if not self.coordinator_enable:
            self._retire_deliveries()
            await self._async_retire_vane_kicks()
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
        # Count the request before anything can await: an apply already in
        # flight must not write its older direction back over it.
        self._selection_seq += 1
        if mode != self.current_shared_mode:
            self.current_shared_mode = mode
            self._mode_by_votes = False
            self._last_mode_change_ts = time.monotonic()
        await self.async_request_refresh()

    # -- seasonal changeover (optional, from local weather) -----------------
    @callback
    def _on_changeover_change(self, _event: Event) -> None:
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
        if entity_id.startswith("weather."):
            try:
                resp = await self.hass.services.async_call(
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
    def _on_inhibit_change(self, _event: Event) -> None:
        """The watched inhibit entity changed -> re-evaluate the hold."""
        self.hass.async_create_task(self._evaluate_inhibit())

    @callback
    def _on_safety_change(self, _event: Event) -> None:
        """A zone's safety-priority sensor changed -> recompute now."""
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
            self._reseed_fan_after_standby()
        await self.async_request_refresh()

    def _reseed_fan_after_standby(self) -> None:
        """Release edge: carry each head's pre-hold latch truth into the restore
        slot and clear the per-head command memory, so the next fan observation
        reconciles restored-truth-vs-observed (the same path a restart takes)
        instead of reading the fan the hold left behind as a fresh user
        departure and latching a phantom hold.
        """
        for zone in self.zones:
            cid = zone.climate_id
            # An UNCONSUMED startup restore outranks the latch: a restart during
            # the hold seeds _fan_restore before any fan write can consume it
            # (fan writes are frozen while held), and the latch is empty at that
            # point — overwriting the slot here would drop a pre-restart manual
            # hold on release. Only fill the slot when it is empty.
            if cid not in self._fan_restore:
                self._fan_restore[cid] = self._fan_latched.get(cid, False)
            self._fan_cmd.pop(cid, None)
            self._fan_prev.pop(cid, None)
            self._fan_latched.pop(cid, None)
            self._fan_idx.pop(cid, None)
            self._publish_fan_state(cid)


def _as_float(value: Any) -> float | None:
    """Best-effort float() of a state attribute for idempotency comparison."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _head_setting(head: Any) -> tuple:
    """What a head shows a person: its mode and setpoints."""
    return (head.state, *(
        head.attributes.get(key)
        for key in ("target_temp_low", "target_temp_high", "temperature")
    ))


def _floor_setting(head: Any) -> tuple[float, float] | None:
    """A head's setpoints as (low, high); a single-setpoint head's one
    setpoint is both. None if it shows none."""
    if int(head.attributes.get("supported_features") or 0) & 2:
        low = _as_float(head.attributes.get("target_temp_low"))
        high = _as_float(head.attributes.get("target_temp_high"))
    else:
        low = high = _as_float(head.attributes.get("temperature"))
    return None if low is None or high is None else (low, high)


def _floor_window(value: Any) -> tuple[tuple[float, float], ...]:
    """A stored re-arm window: [[driven, reading], ...], else empty."""
    if not isinstance(value, list):
        return ()
    pairs = [
        (_finite(item[0]), _finite(item[1]))
        for item in value if isinstance(item, list) and len(item) == 2
    ]
    if len(pairs) != len(value) or any(None in pair for pair in pairs):
        return ()
    return tuple(pairs)


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


def _drift_side(value: Any, low: float, high: float) -> float | None:
    """A stored uneven-drift side clamped into ``[low, high]``, or None (blank).

    Read like Allowed drift (a numeric string counts), but anything that isn't
    a finite number is blank rather than an error.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return min(max(number, low), high)


def _finite(value: Any) -> float | None:
    """A stored number as a finite float, else None (a bool is not a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None
