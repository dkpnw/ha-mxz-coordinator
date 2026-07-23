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
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.const import UnitOfTemperature
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_start
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    BAND_DRIFT_DELAY,
    BANNED_MODES,
    CHANGEOVER_INTERVAL_MINUTES,
    CONF_CHANGEOVER_COOL_BELOW,
    CONF_CHANGEOVER_ENTITY,
    CONF_CHANGEOVER_HEAT_ABOVE,
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    CONF_COOL_LOCKOUT_CEILING,
    CONF_DEMAND_THRESHOLD,
    CONF_ECO_COOL_MAX,
    CONF_ECO_HEAT_MIN,
    CONF_ENGAGE_DEADBAND,
    CONF_FAN_BOOST_ENABLE,
    CONF_FAN_BOOST_MAX,
    CONF_HEAT_LOCKOUT_FLOOR,
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
    DEFAULT_FAN_BOOST_ENABLE,
    DEFAULT_FAN_BOOST_MAX,
    DEFAULT_INHIBIT_ACTION,
    DEFAULT_INHIBIT_ACTIVE_STATE,
    DEFAULT_MODE_HYSTERESIS,
    DEFAULT_RESTING_MODE_BIAS,
    DEMAND_NEUTRAL,
    DOMAIN,
    ENGAGE_SATISFIED,
    EVENT_RECOMPUTE,
    FAN_AUTO,
    FAN_LADDER,
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
    VANE_KICK_SPINUP,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
    ZONE_STAGE_SENSOR,
    ZONE_VANE_HORIZONTAL,
    ZONE_VANE_VERTICAL,
    unit_profile,
    zone_slug,
)
from .logic import (
    engage_with_latch,
    fan_for_delta,
    head_action,
    room_call,
    season_lockouts,
    setpoints,
    shared_mode,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


def _read_temp(state: Any, fallback: float) -> tuple[bool, float]:
    """Return (ok, value) for a temperature sensor state; ``fallback`` on dropout."""
    if state is None or state.state in UNAVAILABLE_STATES:
        return (False, fallback)
    try:
        return (True, float(state.state))
    except (ValueError, TypeError):
        return (False, fallback)


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
        # _fan_idx: _fan_cmd is the last token WE wrote; _fan_prev the one before
        # it (an echo of a just-written token can briefly still read as the prior
        # value — a mismatch is only a user departure if it differs from BOTH).
        self._fan_cmd: dict[str, str] = {}
        self._fan_prev: dict[str, str] = {}
        self._fan_latched: dict[str, bool] = {}
        # Pre-restart latch truth restored by the Fan-auto switch
        # (RestoreEntity), consumed once at the first seed observation per
        # head. Only the held/not-held bool matters — reconciliation always
        # reads the token from the OBSERVED head state. Absent or stale
        # restore data -> plain seeding (below).
        self._fan_restore: dict[str, bool] = {}

        # Engage latch (decision state, like _fan_idx): "" = coasting, cool|heat
        # = mid-run toward target (the head may still be parked in fan_only by a
        # shared-mode mismatch; the run resumes when the mode returns). Seeded
        # lazily from the head's own mode on the first compute so an in-flight
        # run resumes across restarts.
        self._engage_latch: dict[str, str] = {}

        # Vane-kick bookkeeping: heads mid-kick are skipped by _apply so the
        # plan doesn't turn them back off while the louvre is still traveling.
        self._vane_kicks: set[str] = set()
        self._vane_pending: dict[str, tuple[str, str]] = {}
        self._vane_kick_spinup: float = VANE_KICK_SPINUP
        self._vane_kick_apply: float = VANE_KICK_APPLY

        # Helper values (owned by the switch/select entities; seeded on
        # restore, mutated on user action). Kill-switch defaults OFF for safety.
        # Per-zone target/enable live on the Zone objects.
        self.coordinator_enable: bool = False
        self.eco_idle: bool = False
        self.heat_lockout: bool = False
        self.cool_lockout: bool = False
        self.current_shared_mode: str = MODE_COOL  # restored by the select entity

        # Hysteresis is armed from startup: a mode flip must wait out the dwell
        # even right after setup/restart (#6 — 0.0 made the first flip always
        # allowed and the plan sensor report a ~56,000-year dwell).
        self._last_mode_change_ts: float = dt_util.utcnow().timestamp()
        self._unsubs: list[Any] = []
        self._heal_timers: dict[tuple[str, str], Any] = {}
        # Seed data so entities have something to read before the first refresh.
        self.data = self._compute()

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
        """Cancel every listener and pending self-heal timer."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in self._heal_timers.values():
            cancel()
        self._heal_timers.clear()

    # -- decision (mirrors sensor.mxz_plan) ---------------------------------
    def _compute(self) -> dict[str, Any]:
        """Recompute the plan dict from current inputs. Commands nothing.

        The one piece of state it touches is the per-zone engage latch
        (decision memory, like ``_fan_idx``) — seeded on a zone's first
        compute and advanced with each result.

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
        plan: dict[str, Any] = {}
        demands: list[str] = []
        engages: list[str] = []
        all_ok = True
        for zone in self.zones:
            ok, temp = _read_temp(
                self.hass.states.get(zone.sensor_id), self.target_default
            )
            all_ok = all_ok and ok
            demand = room_call(
                temp=temp, target=zone.target, enabled=zone.enable,
                sensor_ok=ok, band=self.demand_threshold, neutral=DEMAND_NEUTRAL,
                **common,
            )
            if zone.slug not in self._engage_latch:
                # First compute for this zone: resume a run the head was
                # already commanded into (cool/heat survive restarts).
                head = self.hass.states.get(zone.climate_id)
                self._engage_latch[zone.slug] = (
                    head.state
                    if head is not None and head.state in (MODE_COOL, MODE_HEAT)
                    else ""
                )
            engage = engage_with_latch(
                prior=self._engage_latch[zone.slug] or None,
                temp=temp, target=zone.target, enabled=zone.enable,
                sensor_ok=ok, band=self.engage_deadband, neutral=ENGAGE_SATISFIED,
                **common,
            )
            self._engage_latch[zone.slug] = (
                engage if engage in (MODE_COOL, MODE_HEAT) else ""
            )
            demands.append(demand)
            engages.append(engage)
            plan[f"{zone.slug}_demand"] = demand
            plan[f"{zone.slug}_engage"] = engage
            plan[f"{zone.slug}_temp"] = temp

        current = (
            self.current_shared_mode
            if self.current_shared_mode in (MODE_COOL, MODE_HEAT)
            else MODE_COOL
        )
        elapsed = dt_util.utcnow().timestamp() - self._last_mode_change_ts
        allowed = elapsed >= self.hysteresis
        # "last" (or anything not cool|heat) -> resting=None keeps the last-mode behavior.
        resting = (
            self.resting_mode_bias
            if self.resting_mode_bias in (MODE_COOL, MODE_HEAT)
            else None
        )
        state = shared_mode(
            demands=demands,
            current=current,
            allowed=allowed,
            resting=resting,
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
                        "fan_hold": self._fan_latched.get(zone.climate_id, False),
                    }
                    for i, zone in enumerate(self.zones)
                ],
                "standoff": MODE_COOL in demands and MODE_HEAT in demands,
                "sensors_ok": all_ok,
                "seconds_since_mode_change": int(elapsed),
                "mode_change_allowed": allowed,
                "inhibited": self.inhibited,
            }
        )
        return plan

    async def _async_update_data(self) -> dict[str, Any]:
        """Refresh entry point: recompute, then act on the new plan."""
        plan = self._compute()
        await self._apply(plan)
        # _apply is what settles the manual-fan latch (it reads each head's
        # observed fan_mode), so re-stamp fan_hold from the post-apply state —
        # otherwise the plan's diagnostic would lag the latch by a cycle.
        for zone_view, zone in zip(plan.get("zones", ()), self.zones):
            zone_view["fan_hold"] = self._fan_latched.get(zone.climate_id, False)
        return plan

    # -- actuator (mirrors script.mxz_coordinate) ---------------------------
    async def _apply(self, plan: dict[str, Any]) -> None:
        """Drive the heads toward the plan. Sole head-writer; idempotent."""
        if not self.coordinator_enable:
            return  # kill-switch: leave the heads untouched

        # External standby hold (grid-down / load-shed): a fixed-mode park is a
        # no-plan short-circuit; the `eco` hold falls through to the normal plan
        # with eco forced on (see _eco_active) so protection extremes still run.
        if self.inhibited and self.inhibit_action in (
            INHIBIT_ACTION_OFF,
            INHIBIT_ACTION_FAN_ONLY,
        ):
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

        for zone, engage in zip(self.zones, engages):
            if zone.climate_id in self._vane_kicks:
                continue  # mid vane-kick: leave the head alone until it finishes
            act = head_action(engage=engage, mode=state, eco=self._eco_active())
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
            # Per-zone isolation: one head rejecting a command degrades THAT
            # zone (logged), never the whole coordinator (#6).
            try:
                await self._apply_head(zone.climate_id, act, low, high)
                # No fan writes while held (the `eco` hold reaches here): the
                # fan-boost/latch machinery stays frozen so standby residue
                # can't be read as a manual hold on release — it is reseeded
                # via _reseed_fan_after_standby on the release edge.
                if not self.inhibited:
                    await self._apply_fan(
                        zone.climate_id,
                        act,
                        abs(plan[f"{zone.slug}_temp"] - float(zone.target)),
                    )
            except HomeAssistantError as err:
                _LOGGER.error(
                    "MXZ: applying %s to %s failed (zone degraded, others continue): %s",
                    act,
                    zone.climate_id,
                    err,
                )

        # Stamp the flip only on a real mode change (cool<->heat).
        if state != self.current_shared_mode:
            self.current_shared_mode = state
            self._last_mode_change_ts = dt_util.utcnow().timestamp()
            self.async_update_listeners()  # let the shared-mode select re-render

    def _eco_active(self) -> bool:
        """Whether the eco protection band is in effect: the user's eco-idle
        switch, OR an external inhibit hold configured to hold at the eco band."""
        return self.eco_idle or (
            self.inhibited and self.inhibit_action == INHIBIT_ACTION_ECO
        )

    async def _park_heads(self, mode: str) -> None:
        """Standby: drive every coordinated head to a fixed mode (off/fan_only).

        Mode-only — never the fan-boost/latch machinery — so a manual fan hold
        survives the hold (reconciled on release, see _reseed_fan_after_standby).
        Same per-zone isolation as _apply, and skips a head mid vane-kick.
        """
        for zone in self.zones:
            if zone.climate_id in self._vane_kicks:
                continue
            try:
                await self._apply_head(zone.climate_id, mode, 0.0, 0.0)
            except HomeAssistantError as err:
                _LOGGER.error(
                    "MXZ: standby-parking %s to %s failed "
                    "(zone degraded, others continue): %s",
                    zone.climate_id,
                    mode,
                    err,
                )

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
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": climate_id,
                        "hvac_mode": act,
                        "temperature": setpoint,
                    },
                    blocking=True,
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
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": climate_id,
                    "hvac_mode": act,
                    "target_temp_low": low,
                    "target_temp_high": high,
                },
                blocking=True,
            )
            return

        # fan_only / off -> set_hvac_mode only (a bare temperature throws)
        if cur_mode == act:
            return
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": climate_id, "hvac_mode": act},
            blocking=True,
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

        # Evaluate the manual-fan latch from what the head is actually reporting.
        if self._observe_fan_latch(climate_id, state, act, delta, modes):
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
        if state.attributes.get("fan_mode") == token:
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
        self,
        climate_id: str,
        state: Any,
        act: str,
        delta: float,
        modes: list[str],
    ) -> bool:
        """Update and return the manual-fan latch for a head from its state.

        Called once per apply per head, guaranteed the head has fan_modes with an
        "auto" token. Latch transitions off the OBSERVED fan_mode:

        * observed "auto"                 -> released (user handed control back)
        * observed token != BOTH the last- and prior-commanded token -> latched
          (a user departure; the double-token check absorbs the echo race where
          the head still reports the token we wrote one cycle ago)
        * no _fan_cmd memory yet (first compute / post-restart seed): a non-"auto"
          reading seeds LATCHED, mirroring the engage-latch's "resume from the
          head's own state" — a manual pick that predates the restart is honored —
          UNLESS the reading is a speed the ladder would hold at the current
          delta, which is our own boost speed echoing back (see
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
        observed = state.attributes.get("fan_mode")
        if observed == FAN_AUTO:
            # Observed auto releases everything — including a restored hold the
            # user let go of during the outage. Consume any restore data.
            self._fan_restore.pop(climate_id, None)
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

        # A post-restart seed at exactly the speed the ladder would command right
        # now is our own boost speed echoing back, not a manual pick -> adopt it
        # and keep driving. A DEPARTURE never adopts: every slider move is a hold.
        if seeding and observed is not None:
            # Reconcile: restored pre-restart truth beats token guessing.
            restored = self._fan_restore.pop(climate_id, None)
            if restored is not None:
                held = restored
                if held:
                    # Still held — at the observed token (same token: the hold
                    # simply survived; different non-auto token: the user moved
                    # the hold during the outage — theirs either way).
                    self._fan_latched[climate_id] = True
                    self._fan_prev[climate_id] = observed
                    self._fan_cmd[climate_id] = observed
                    return True
                # Restored NOT held: boost was driving. An active seed still
                # goes through the fixed-point check below; a satisfied/eco/off
                # seed at a token boost could have written is residue of the
                # interrupted satisfied->auto handback -> don't latch, let the
                # return-to-auto write proceed (baseline stamped so a slow echo
                # of the residue token isn't a fresh departure). A token boost
                # could NEVER have written (outside the ladder / above the
                # ceiling) appeared by hand during the outage -> hold.
                if (act not in (MODE_COOL, MODE_HEAT) or self.eco_idle) and (
                    observed in FAN_LADDER
                    and FAN_LADDER.index(observed) <= self._fan_max_idx()
                ):
                    self._fan_latched[climate_id] = False
                    self._fan_cmd[climate_id] = observed
                    self._fan_prev[climate_id] = observed
                    return False
            idx = self._seed_matches_boost(climate_id, observed, act, delta)
            if idx is not None:
                self._adopt_fan_speed(climate_id, observed, idx)
                return False

        # Seed latched iff the head isn't at auto; a departure always latches.
        self._fan_latched[climate_id] = observed is not None
        if observed is not None:
            # Record the held token as the baseline so the zone becomes a
            # STANDING hold next cycle (the same trick async_set_fan_auto OFF
            # uses). Without this the identical reading re-reads as a fresh
            # seed/departure every cycle and the latch decision is re-litigated
            # forever — which is how the v2.18.0 "standing merge" removal was
            # defeated for slider holds: the departure branch kept re-running
            # the max handback, so a max hold still released itself on drift.
            self._fan_prev[climate_id] = observed
            self._fan_cmd[climate_id] = observed
        return self._fan_latched.get(climate_id, False)

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

    def restore_fan_hold(self, climate_id: str, held: bool) -> None:
        """Record the Fan-auto switch's restored pre-restart latch truth.

        Called from the switch's async_added_to_hass (RestoreEntity) — which
        runs during platform setup, BEFORE the coordinator's first compute —
        and consumed once by the seed, which reconciles it against the
        observed head state. Stale restores (older than the entry) are
        filtered by the switch and never reach here. With fan boost disabled
        the data is simply never consumed (the fan machinery is inert).
        """
        self._fan_restore[climate_id] = held

    # -- fan-auto switch (the discoverable manual-hold handback) --------------
    def fan_auto_is_on(self, climate_id: str) -> bool:
        """True when boost drives this head's fan (zone NOT manually held).

        The switch is a live mirror of the latch: ON = auto/boost in charge,
        OFF = a manual speed is being held. The latch machinery is the single
        source of truth while running; across restarts the switch restores the
        held/not-held bool and hands it back via ``restore_fan_hold``.
        """
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
        state = self.hass.states.get(climate_id)
        observed = state.attributes.get("fan_mode") if state is not None else None
        if on:
            if observed is not None:
                # Adopt the current speed so boost resumes from it without a
                # spurious re-latch (rolls _fan_prev, pins _fan_idx to max for a
                # clean DOWN_AT ramp-down).
                self._adopt_fan_speed(climate_id, observed)
            else:
                self._fan_latched[climate_id] = False
            await self.async_request_refresh()
            return
        # OFF: hold at the current speed. Auto is nothing to hold onto.
        if observed is None or observed == FAN_AUTO:
            _LOGGER.debug(
                "fan-auto OFF for %s ignored: head at %s (nothing to hold)",
                climate_id,
                observed,
            )
            return
        self._fan_prev[climate_id] = self._fan_cmd.get(climate_id, observed)
        self._fan_cmd[climate_id] = observed
        self._fan_latched[climate_id] = True

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
        """Issue a fan_mode write and remember it (last + prior, for the echo race)."""
        self._fan_prev[climate_id] = self._fan_cmd.get(climate_id, token)
        self._fan_cmd[climate_id] = token
        await self.hass.services.async_call(
            "climate",
            "set_fan_mode",
            {"entity_id": climate_id, "fan_mode": token},
            blocking=True,
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
        self._vane_kicks.add(climate_id)
        self.hass.async_create_task(self._vane_kick(climate_id))

    async def _vane_kick(self, climate_id: str) -> None:
        """fan_only -> apply pending vane option(s) -> off -> re-assert plan."""
        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": climate_id, "hvac_mode": MODE_FAN_ONLY},
                blocking=True,
            )
            await asyncio.sleep(self._vane_kick_spinup)
            while (pending := self._vane_pending.pop(climate_id, None)) is not None:
                await self._select_option(*pending)
                await asyncio.sleep(self._vane_kick_apply)
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": climate_id, "hvac_mode": MODE_OFF},
                blocking=True,
            )
        finally:
            self._vane_kicks.discard(climate_id)
            self._vane_pending.pop(climate_id, None)
        await self.async_request_refresh()

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
        """A temp sensor changed -> recompute."""
        self.hass.async_create_task(self.async_request_refresh())

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
        """Detect a head drifting into a banned mode or off-while-enabled."""
        entity_id: str = event.data["entity_id"]
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        mode = new_state.state if new_state else None

        self._arm_or_cancel(
            entity_id, "band", mode in BANNED_MODES, BAND_DRIFT_DELAY
        )
        # Never heal a head the standby hold parked off (nor a wall override
        # mid-hold — a human turning a parked head on during an outage wins).
        off_drift = (
            mode == MODE_OFF
            and self._enable_for(entity_id)
            and not self.eco_idle
            and not self.inhibited
        )
        self._arm_or_cancel(entity_id, "off", off_drift, OFF_WHILE_ENABLED_DELAY)

        # A fan_mode change is a latch-relevant observation (a manual pick, an
        # `auto` handback, or our own write's echo): refresh promptly so the
        # hold engages and the Fan auto switch mirrors it now, not at the next
        # heartbeat. Idempotent writes + the echo-tolerant departure check make
        # a refresh on our own echo harmless.
        new_fan = new_state.attributes.get("fan_mode") if new_state else None
        old_fan = old_state.attributes.get("fan_mode") if old_state else None
        if new_fan != old_fan:
            self.hass.async_create_task(self.async_request_refresh())

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
    def reset_engage_latch(self, slug: str) -> None:
        """Forget a zone's engage latch (its target changed -> fresh decision).

        The next compute re-seeds from the head's actual mode, so an in-flight
        run toward the same direction continues seamlessly to the new target,
        while a direction change re-evaluates immediately instead of wasting a
        cycle disengaging a stale latch.
        """
        self._engage_latch.pop(slug, None)

    async def async_user_changed(self) -> None:
        """A helper entity changed by the user -> recompute + act."""
        await self.async_request_refresh()

    async def async_select_shared_mode(self, mode: str) -> None:
        """Manual override of the shared mode (stamps the hysteresis clock)."""
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
        if released:
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
            self._fan_restore[cid] = self._fan_latched.get(cid, False)
            self._fan_cmd.pop(cid, None)
            self._fan_prev.pop(cid, None)
            self._fan_latched.pop(cid, None)
            self._fan_idx.pop(cid, None)


def _as_float(value: Any) -> float | None:
    """Best-effort float() of a state attribute for idempotency comparison."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
