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

import logging
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_start
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    BAND_DRIFT_DELAY,
    BANNED_MODES,
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    CONF_DEMAND_THRESHOLD,
    CONF_ECO_COOL_MAX,
    CONF_ECO_HEAT_MIN,
    CONF_ENGAGE_DEADBAND,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_PRIMARY_VANE_HORIZONTAL,
    CONF_PRIMARY_VANE_VERTICAL,
    CONF_RESTING_MODE_BIAS,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    CONF_SECONDARY_VANE_HORIZONTAL,
    CONF_SECONDARY_VANE_VERTICAL,
    DEFAULT_CLAMP_MAX,
    DEFAULT_CLAMP_MIN,
    DEFAULT_DEMAND_THRESHOLD,
    DEFAULT_ECO_COOL_MAX,
    DEFAULT_ECO_HEAT_MIN,
    DEFAULT_ENGAGE_DEADBAND,
    DEFAULT_MODE_HYSTERESIS,
    DEFAULT_RESTING_MODE_BIAS,
    DEMAND_NEUTRAL,
    ENGAGE_SATISFIED,
    EVENT_RECOMPUTE,
    MODE_COOL,
    MODE_HEAT,
    MODE_OFF,
    OFF_WHILE_ENABLED_DELAY,
    STARTUP_RECOVER_DELAY,
    TARGET_DEFAULT,
    UNAVAILABLE_STATES,
)
from .logic import head_action, room_call, setpoints, shared_mode

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


def _read_temp(state: Any) -> tuple[bool, float]:
    """Return (ok, value) for a temperature sensor state; default 70.0 on dropout."""
    if state is None or state.state in UNAVAILABLE_STATES:
        return (False, float(TARGET_DEFAULT))
    try:
        return (True, float(state.state))
    except (ValueError, TypeError):
        return (False, float(TARGET_DEFAULT))


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
        self.primary_climate_id: str = conf[CONF_PRIMARY_CLIMATE]
        self.secondary_climate_id: str = conf[CONF_SECONDARY_CLIMATE]
        self.primary_sensor_id: str = conf[CONF_PRIMARY_SENSOR]
        self.secondary_sensor_id: str = conf[CONF_SECONDARY_SENSOR]
        self.notify_service: str | None = conf.get(CONF_NOTIFY_SERVICE) or None

        # Optional vane `select` entities exposed through the native thermostats.
        self.primary_vane_vertical_id: str | None = (
            conf.get(CONF_PRIMARY_VANE_VERTICAL) or None
        )
        self.primary_vane_horizontal_id: str | None = (
            conf.get(CONF_PRIMARY_VANE_HORIZONTAL) or None
        )
        self.secondary_vane_vertical_id: str | None = (
            conf.get(CONF_SECONDARY_VANE_VERTICAL) or None
        )
        self.secondary_vane_horizontal_id: str | None = (
            conf.get(CONF_SECONDARY_VANE_HORIZONTAL) or None
        )

        self.demand_threshold: float = conf.get(
            CONF_DEMAND_THRESHOLD, DEFAULT_DEMAND_THRESHOLD
        )
        self.engage_deadband: float = conf.get(
            CONF_ENGAGE_DEADBAND, DEFAULT_ENGAGE_DEADBAND
        )
        self.hysteresis: int = conf.get(CONF_MODE_HYSTERESIS, DEFAULT_MODE_HYSTERESIS)
        self.eco_cool_max: float = conf.get(CONF_ECO_COOL_MAX, DEFAULT_ECO_COOL_MAX)
        self.eco_heat_min: float = conf.get(CONF_ECO_HEAT_MIN, DEFAULT_ECO_HEAT_MIN)
        self.clamp_min: int = int(conf.get(CONF_CLAMP_MIN, DEFAULT_CLAMP_MIN))
        self.clamp_max: int = int(conf.get(CONF_CLAMP_MAX, DEFAULT_CLAMP_MAX))
        self.resting_mode_bias: str = conf.get(
            CONF_RESTING_MODE_BIAS, DEFAULT_RESTING_MODE_BIAS
        )

        # Helper values (owned by the number/switch/select entities; seeded on
        # restore, mutated on user action). Kill-switch defaults OFF for safety.
        self.primary_target: float = float(TARGET_DEFAULT)
        self.secondary_target: float = float(TARGET_DEFAULT)
        self.primary_enable: bool = False
        self.secondary_enable: bool = False
        self.coordinator_enable: bool = False
        self.eco_idle: bool = False
        self.current_shared_mode: str = MODE_COOL  # restored by the select entity

        self._last_mode_change_ts: float = 0.0  # epoch -> first flip always allowed
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
                [self.primary_sensor_id, self.secondary_sensor_id],
                self._on_input_change,
            )
        )
        self._unsubs.append(
            async_track_state_change_event(
                self.hass,
                [self.primary_climate_id, self.secondary_climate_id],
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
        """Recompute the plan dict from current inputs. No side effects."""
        pt_ok, pt = _read_temp(self.hass.states.get(self.primary_sensor_id))
        st_ok, st = _read_temp(self.hass.states.get(self.secondary_sensor_id))

        common = {
            "eco": self.eco_idle,
            "eco_cool_max": self.eco_cool_max,
            "eco_heat_min": self.eco_heat_min,
        }
        pd = room_call(
            temp=pt, target=self.primary_target, enabled=self.primary_enable,
            sensor_ok=pt_ok, band=self.demand_threshold, neutral=DEMAND_NEUTRAL,
            **common,
        )
        sd = room_call(
            temp=st, target=self.secondary_target, enabled=self.secondary_enable,
            sensor_ok=st_ok, band=self.demand_threshold, neutral=DEMAND_NEUTRAL,
            **common,
        )
        p_eng = room_call(
            temp=pt, target=self.primary_target, enabled=self.primary_enable,
            sensor_ok=pt_ok, band=self.engage_deadband, neutral=ENGAGE_SATISFIED,
            **common,
        )
        s_eng = room_call(
            temp=st, target=self.secondary_target, enabled=self.secondary_enable,
            sensor_ok=st_ok, band=self.engage_deadband, neutral=ENGAGE_SATISFIED,
            **common,
        )

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
            primary_demand=pd,
            secondary_demand=sd,
            current=current,
            allowed=allowed,
            resting=resting,
        )
        standoff = (pd == MODE_COOL or sd == MODE_COOL) and (
            pd == MODE_HEAT or sd == MODE_HEAT
        )
        return {
            "state": state,
            "primary_demand": pd,
            "secondary_demand": sd,
            "primary_engage": p_eng,
            "secondary_engage": s_eng,
            "standoff": standoff,
            "sensors_ok": pt_ok and st_ok,
            "seconds_since_mode_change": int(elapsed),
            "mode_change_allowed": allowed,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Refresh entry point: recompute, then act on the new plan."""
        plan = self._compute()
        await self._apply(plan)
        return plan

    # -- actuator (mirrors script.mxz_coordinate) ---------------------------
    async def _apply(self, plan: dict[str, Any]) -> None:
        """Drive the heads toward the plan. Sole head-writer; idempotent."""
        if not self.coordinator_enable:
            return  # kill-switch: leave the heads untouched

        state = plan["state"]
        p_eng = plan["primary_engage"]
        s_eng = plan["secondary_engage"]
        valid_eng = (MODE_COOL, MODE_HEAT, ENGAGE_SATISFIED, MODE_OFF)
        if state not in (MODE_COOL, MODE_HEAT):
            return  # plan not ready
        if p_eng not in valid_eng or s_eng not in valid_eng:
            return

        for climate_id, target, engage in (
            (self.primary_climate_id, self.primary_target, p_eng),
            (self.secondary_climate_id, self.secondary_target, s_eng),
        ):
            act = head_action(engage=engage, mode=state, eco=self.eco_idle)
            low, high = setpoints(
                mode=state,
                target=int(target),
                eco=self.eco_idle,
                clamp_min=self.clamp_min,
                clamp_max=self.clamp_max,
            )
            await self._apply_head(climate_id, act, low, high)

        # Stamp the flip only on a real mode change (cool<->heat).
        if state != self.current_shared_mode:
            self.current_shared_mode = state
            self._last_mode_change_ts = dt_util.utcnow().timestamp()
            self.async_update_listeners()  # let the shared-mode select re-render

    async def _apply_head(
        self, climate_id: str, act: str, low: int, high: int
    ) -> None:
        """Issue (or skip, if already correct) the command for one head."""
        state = self.hass.states.get(climate_id)
        cur_mode = state.state if state else None

        if act in (MODE_COOL, MODE_HEAT):
            cur_low = _as_int(state.attributes.get("target_temp_low")) if state else None
            cur_high = (
                _as_int(state.attributes.get("target_temp_high")) if state else None
            )
            if cur_mode == act and cur_low == low and cur_high == high:
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
        mode = new_state.state if new_state else None

        self._arm_or_cancel(
            entity_id, "band", mode in BANNED_MODES, BAND_DRIFT_DELAY
        )
        off_drift = (
            mode == MODE_OFF and self._enable_for(entity_id) and not self.eco_idle
        )
        self._arm_or_cancel(entity_id, "off", off_drift, OFF_WHILE_ENABLED_DELAY)

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
        if still and self.coordinator_enable:
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
        if climate_id == self.primary_climate_id:
            return self.primary_enable
        return self.secondary_enable

    # -- entity-driven mutations --------------------------------------------
    async def async_user_changed(self) -> None:
        """A helper entity changed by the user -> recompute + act."""
        await self.async_request_refresh()

    async def async_select_shared_mode(self, mode: str) -> None:
        """Manual override of the shared mode (stamps the hysteresis clock)."""
        if mode != self.current_shared_mode:
            self.current_shared_mode = mode
            self._last_mode_change_ts = dt_util.utcnow().timestamp()
        await self.async_request_refresh()


def _as_int(value: Any) -> int | None:
    """Best-effort int() of a state attribute for idempotency comparison."""
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None
