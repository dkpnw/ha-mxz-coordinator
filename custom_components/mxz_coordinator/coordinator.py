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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfTemperature
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
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
    ENGAGE_SATISFIED,
    EVENT_RECOMPUTE,
    EVIDENCE_HA_WRITE,
    FAN_AUTO,
    FAN_LADDER,
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
from .logic import (
    engage_with_latch,
    evidence_contract,
    fan_for_delta,
    freshness_window,
    head_action,
    room_call,
    sample_evidence,
    season_lockouts,
    sensor_health,
    setpoints,
    shared_mode,
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


def _parse_zones(conf: dict[str, Any], target_default: float) -> list[Zone]:
    """Build the ordered Zone list from entry config.

    Prefers the v2 ``zones`` list; falls back to the legacy flat
    primary_*/secondary_* keys (pre-migration entries and old tests). Legacy
    flat vane keys still override zones 0/1 when present (v2.9 options-flow
    overrides live there on migrated entries).
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
            report_interval=z.get(ZONE_REPORT_INTERVAL),
            max_age=z.get(ZONE_MAX_AGE),
            startup_grace=z.get(ZONE_STARTUP_GRACE),
            evidence_basis=z.get(ZONE_EVIDENCE_BASIS),
            sample_timestamp_attribute=z.get(ZONE_SAMPLE_TIMESTAMP_ATTR),
            sample_sequence_attribute=z.get(ZONE_SAMPLE_SEQUENCE_ATTR),
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
        self.zones: list[Zone] = _parse_zones(conf, self.target_default)
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
        # How a satisfied (or standoff-parked) head idles: fan_only (default),
        # off, or off after a coil-dry fan run following active cooling. Airflow
        # only — the refrigerant valve position is the same either way.
        self.idle_action: str = conf.get(CONF_IDLE_ACTION, DEFAULT_IDLE_ACTION)
        self.coil_dry_seconds: float = 60.0 * float(
            conf.get(CONF_COIL_DRY_MINUTES, DEFAULT_COIL_DRY_MINUTES)
        )
        # Per-head memory of the last ACTIVE mode we commanded (mode, utc ts),
        # re-stamped every apply cycle while running — so the timestamp reads
        # as "when conditioning stopped". Drives the off_after_dry dwell.
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
        self._last_mode_change_ts: float = dt_util.utcnow().timestamp()
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
        all_ok = True
        coasted = False
        for zone in self.zones:
            state = self.hass.states.get(zone.sensor_id)
            valid, temp = _read_temp(state, self.target_default, self.temp_unit)
            health, age = self._sensor_health(zone, state, valid, now)
            # A stale room votes exactly like a room with no reading at all:
            # no demand, no engagement, its head parked by the ordinary idle
            # path. The reading itself is still valid, so it is still SHOWN —
            # what the room loses is authority, not its last measurement.
            ok = valid and health != HEALTH_STALE
            all_ok = all_ok and ok
            zdrift = self.zone_drift(zone)
            demand = room_call(
                temp=temp, target=zone.target, enabled=zone.enable,
                # A room's demand vote respects its OWN drift (#18): a room
                # told to tolerate a wide band must not steer the shared
                # compressor inside it. max() reproduces today's behavior
                # exactly whenever drift <= demand_threshold (the default).
                sensor_ok=ok, band=max(self.demand_threshold, zdrift),
                neutral=DEMAND_NEUTRAL,
                **common,
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
                sensor_ok=ok, band=zdrift, neutral=ENGAGE_SATISFIED,
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
            demands.append(demand)
            engages.append(engage)
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
        elapsed = now - self._last_mode_change_ts
        allowed = elapsed >= self.hysteresis
        state = shared_mode(
            demands=demands,
            current=current,
            allowed=allowed,
            resting=self._resting_bias(),
        )
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
                        "enabled": zone.enable,
                        "drift": self.zone_drift(zone),
                        "fan_hold": not self.fan_auto_is_on(zone.climate_id),
                        "sensor_health": healths[i],
                        "sensor_age": None if ages[i] is None else int(ages[i]),
                        "control_reasons": self._control_reasons(zone),
                        **self.command_attributes(zone.climate_id),
                    }
                    for i, zone in enumerate(self.zones)
                ],
                "standoff": MODE_COOL in demands and MODE_HEAT in demands,
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
        if health in HEALTH_UNHEALTHY and not opened:
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
        remaining = self.hysteresis - (
            dt_util.utcnow().timestamp() - self._last_mode_change_ts
        )
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
        would_flip_to = shared_mode(
            demands=[plan[f"{zone.slug}_demand"] for zone in self.zones],
            current=current,
            allowed=True,
            resting=self._resting_bias(),
        )
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

        tasks = []
        for zone, engage in zip(self.zones, engages):
            if zone.climate_id in self._vane_kicks:
                continue  # mid vane-kick: leave the head alone until it finishes
            if zone.slug in self._startup_resume:
                head = self.hass.states.get(zone.climate_id)
                if head is None or head.state in UNAVAILABLE_STATES:
                    # Do not manufacture the missing startup mode with our own
                    # park/write. A known head still follows sensor-failure parking.
                    continue
            act = head_action(
                engage=engage,
                mode=state,
                eco=self._eco_active(),
                idle=self._idle_act_for(zone.climate_id),
            )
            low, high = setpoints(
                mode=state,
                target=float(zone.target),
                eco=self._eco_active(),
                clamp_min=self.clamp_min,
                clamp_max=self.clamp_max,
                band=self.setpoint_band,
                step=self.target_step,
                eco_cool=self.eco_cool,
                eco_heat=self.eco_heat,
            )
            shown = plan[f"{zone.slug}_temp"]
            delta = 0.0 if shown is None else abs(shown - float(zone.target))
            task = self._queue_delivery(zone.climate_id, act, low, high, delta, engage)
            if task is not None:
                tasks.append(task)
        await self._wait_deliveries(tasks)

        # Stamp the flip only on a real mode change (cool<->heat) — and never
        # over a person's request that arrived while the head services above
        # were in flight. This plan was computed before that request, so its
        # direction is the older one; the request keeps the selector and the
        # clock it already set, and the refresh it queued computes next.
        if state != self.current_shared_mode and selection_seq == self._selection_seq:
            self.current_shared_mode = state
            self._last_mode_change_ts = dt_util.utcnow().timestamp()
            self.async_update_listeners()  # let the shared-mode select re-render

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
        if engage is None:  # fixed standby park, no fan machinery
            await self._apply_head(climate_id, act, low, high)
            return
        if act in (MODE_COOL, MODE_HEAT):
            self._last_active[climate_id] = (act, dt_util.utcnow().timestamp())
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
        head = self.hass.states.get(climate_id)
        return {
            "head_state_updated_at": head.last_updated.isoformat() if head else "not yet recorded",
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
        """Existing room diagnostics remain useful even without a current plan."""
        views = (self.data or {}).get("zones", ())
        return [
            {
                **(views[i] if i < len(views) else {"name": zone.name}),
                **self.command_attributes(zone.climate_id),
                "control_reasons": self._control_reasons(zone),
            }
            for i, zone in enumerate(self.zones)
        ]

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

        The dwell is a timestamp comparison — the recompute cycle is the clock,
        so a restart can never strand a head in fan_only. The one-shot nudge
        timer only makes the flip prompt (the 15-min heartbeat would otherwise
        stretch a 10-min dwell).
        """
        rec = self._last_active.get(climate_id)
        if rec is None:
            rec = self._seed_last_active(climate_id)
        if rec is None or rec[0] != MODE_COOL:
            return MODE_OFF  # heat leaves no wet coil; never-ran owes no dwell
        remaining = self.coil_dry_seconds - (
            dt_util.utcnow().timestamp() - rec[1]
        )
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
            rec = (MODE_COOL, dt_util.utcnow().timestamp())
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
                return head_action(
                    engage=engage,
                    mode=state,
                    eco=self._eco_active(),
                    idle=self._idle_act_for(entity_id),
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
        component = (self.hass.data.get(DATA_INSTANCES) or {}).get("climate")
        entity = component.get_entity(climate_id) if component else None
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
            tol = self.target_step / 2  # half a step = already-set (float noise)
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
                if cur_mode == act and cur is not None and abs(cur - setpoint) < tol:
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
                and cur_low is not None
                and cur_high is not None
                and abs(cur_low - low) < tol
                and abs(cur_high - high) < tol
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
          ``_seed_matches_boost``).

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
        * not held: boost was driving. With memory, an in-ceiling ladder token
          matching last/prior is treated as residue, whatever the room is doing
          now. Above-ceiling and non-ladder tokens hold even if remembered:
          memory can contain user picks. A nonmatching token conservatively
          holds; a stale dump can cause a false hold, so this is NOT proof of
          a manual gesture. Without memory,
          any ladder token up to the boost ceiling is one boost could have
          written and is ours (the documented edge: a pick made while HA itself
          was down is indistinguishable from residue); a token boost could never
          have written — outside the ladder, above the ceiling — holds. Never a
          fixed-point guess: a mere change of delta is not a manual gesture. An
          active room resumes the ladder from the observed rung.
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
        if last is None:
            ours = (
                observed in FAN_LADDER
                and FAN_LADDER.index(observed) <= self._fan_max_idx()
            )
        else:
            ours = (
                observed in FAN_LADDER
                and FAN_LADDER.index(observed) <= self._fan_max_idx()
                and observed in (last, self._fan_prev.get(climate_id))
            )
        if not ours:
            self._hold_at(climate_id, observed)
            return True
        self._fan_latched[climate_id] = False
        if last is None:
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

        self._arm_or_cancel(
            entity_id, "band", mode in BANNED_MODES, BAND_DRIFT_DELAY
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
            still = mode in BANNED_MODES
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

    # -- entity-driven mutations --------------------------------------------
    def zone_drift(self, zone: Zone) -> float:
        """The zone's re-engage drift: its own override, else the global.

        Clamped to the unit profile's bounds at READ, same as the global — a
        restored or hand-edited value must not collapse the coast window or
        park a room degrees off target.
        """
        if zone.drift is None:
            return self.engage_deadband
        lo, hi = self._profile["engage_bounds"]
        return min(max(float(zone.drift), lo), hi)

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
            self._last_mode_change_ts = dt_util.utcnow().timestamp()
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
