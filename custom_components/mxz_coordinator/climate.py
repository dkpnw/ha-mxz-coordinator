"""Native single-target room thermostats (HomeKit/Google-facing facade).

Each room (primary, secondary) is exposed as a single-setpoint thermostat that
HomeKit/Google render as a clean Tesla/Nest-style "Auto" tile: one number, the
system picks heat vs cool. These entities own NO state of their own. They READ the
coordinator's plan/targets for display and WRITE by driving the integration's own
``number.*_target`` / ``switch.*_enable`` entities (the single source of truth +
restore layer) -- exactly as the echavet proxy's ``coordinator_single_target`` mode
drives the legacy ``input_number``/``input_boolean`` helpers, redirected to this
integration's entities. The coordinator stays the sole writer to the real heads.

Optional vane ``select`` entities are mirrored as swing modes so the native tile
keeps vane control, removing the need for the proxy entirely.

An optional per-zone "airflow" (``stage``) sensor keeps the tile's fan display
HONEST while the coordinator has handed the fan back to the firmware's own
``auto`` ramp: the head then reports the token ``auto`` and the last real speed
freezes, so nobody upstream (a HomeKit slider especially) knows the physical
airflow. When such a sensor is wired, the tile mirrors the firmware's decoded
actual blower stage as the matching settable rung — display ONLY; it never
changes what the coordinator commands or what the latch machinery reads.

Every room's thermostat is a :class:`MXZRangeRoomClimate`. While its room is
opted in to a dual setpoint (Configure, Rooms, the room, Dual setpoint), it
offers the range in place of the single target, ``target_temp_low`` as its
Heat to and ``target_temp_high`` as its Cool to, still in ``heat_cool``,
still with MXZ choosing the direction. While it isn't, every override hands
straight back to :class:`MXZRoomClimate`, so the room is exactly as it was.
The same entity switches in place, so it never goes unavailable on the way.
"""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .capabilities import head_fan_modes
from .const import (
    DOMAIN,
    FAN_AUTO,
    MODE_COOL,
    MODE_HEAT,
    STAGE_TO_FAN,
    UNAVAILABLE_STATES,
)
from .coordinator import MXZCoordinator, Zone, read_room_temp
from .entity import MXZEntity
from .logic import RangeError, resolve_range
from .number import range_error

# What a head is doing, from its mode, when it reports no hvac_action itself.
_ACTION_FOR_MODE = {
    HVACMode.OFF: HVACAction.OFF,
    HVACMode.HEAT: HVACAction.HEATING,
    HVACMode.COOL: HVACAction.COOLING,
    HVACMode.FAN_ONLY: HVACAction.FAN,
    HVACMode.DRY: HVACAction.DRYING,
}

# Independent horizontal swing was added in HA 2024.12; guard for older cores.
_HAS_HORIZONTAL_SWING = hasattr(ClimateEntityFeature, "SWING_HORIZONTAL_MODE")

# HA 2026.11 renamed the temperature members native_* (native_temperature_unit,
# native_current_temperature, ...). It still serves a subclass that provides the
# old names, but logs a deprecation for it, and 2027.11 drops them. So each
# thermostat provides exactly the names its core reads, never both.
_NATIVE_TEMPERATURES = hasattr(ClimateEntity, "native_current_temperature")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one room thermostat per zone (each can switch to a range in place)."""
    coordinator: MXZCoordinator = entry.runtime_data
    async_add_entities(
        MXZRangeRoomClimate(coordinator, zone) for zone in coordinator.zones
    )


class MXZRoomClimate(MXZEntity, CoordinatorEntity[MXZCoordinator], ClimateEntity):
    """A single-target thermostat facade over one zone's helper entities."""

    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.OFF, HVACMode.HEAT_COOL]
    _attr_icon = "mdi:home-thermometer"
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        MXZEntity.__init__(self, coordinator, f"{zone.slug}_thermostat")
        CoordinatorEntity.__init__(self, coordinator)
        self._zone = zone
        self._attr_translation_key = "zone_thermostat"
        self._attr_translation_placeholders = {"zone": zone.name}

        # Report the HA system temperature unit + its resolution (°F: whole
        # degrees; °C: 0.5° steps).
        if _NATIVE_TEMPERATURES:
            self._attr_native_temperature_unit = coordinator.temp_unit
        else:
            self._attr_temperature_unit = coordinator.temp_unit
        self._attr_target_temperature_step = coordinator.target_step

        # Setpoint-slider bounds are derived live from the head (see min_temp /
        # max_temp below): [clamp_min, clamp_max] narrowed to the head's own
        # native operating band, so HomeKit/Google won't offer targets the head
        # can't actually reach (#10).

        # The underlying head this tile passes fan/vane control through to.
        self._head_id = zone.climate_id
        self._vane_vertical_id = zone.vane_vertical_id
        self._vane_horizontal_id = zone.vane_horizontal_id
        # Optional actual-airflow (stage) sensor for honest fan display under
        # firmware `auto` (see module docstring + fan_mode below).
        self._stage_sensor_id = zone.stage_sensor_id

        self._base_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self._vane_vertical_id:
            self._base_features |= ClimateEntityFeature.SWING_MODE
        if self._vane_horizontal_id and _HAS_HORIZONTAL_SWING:
            self._base_features |= ClimateEntityFeature.SWING_HORIZONTAL_MODE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Room explanations, software command observations, and start points.

        The start points are plain read-only attributes from the plan. They
        are never target_temp_low/high: the tile stays a single target.
        """
        return {
            **self.coordinator.command_attributes(self._head_id),
            "control_reasons": self.coordinator._control_reasons(self._zone),
            **self.coordinator.start_points(self._zone),
        }

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Advertise fan control only when the underlying head really does."""
        if head_fan_modes(self.hass, self._head_id) is not None:
            return self._base_features | ClimateEntityFeature.FAN_MODE
        return self._base_features

    async def async_added_to_hass(self) -> None:
        """Re-render when the underlying head changes (fan/vane reflected live)."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_delivery_listener(self.async_write_ha_state)
        )
        # Track the head (fan/vane) and, when wired, the stage sensor so the
        # airflow display is live as the firmware ramps its own `auto` — no
        # coordinator refresh required.
        tracked = [self._head_id]
        if self._stage_sensor_id:
            tracked.append(self._stage_sensor_id)
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, tracked, self._handle_head_change
            )
        )

    @callback
    def _handle_head_change(self, _event: Event) -> None:
        self.async_write_ha_state()

    def _sibling_eid(self, platform: str, key: str) -> str | None:
        """Resolve a sibling number/switch entity_id by its unique_id suffix.

        Resolved lazily (not cached at add-time): platforms are forwarded
        concurrently, so the number/switch entities may not be registered yet
        when this climate entity is added. By the time a write fires, they are.
        """
        reg = er.async_get(self.hass)
        entry_id = self.coordinator.config_entry.entry_id
        return reg.async_get_entity_id(platform, DOMAIN, f"{entry_id}_{key}")

    # -- room-state plumbing ------------------------------------------------
    @property
    def _enabled(self) -> bool:
        return self._zone.enable

    @property
    def _sensor_id(self) -> str:
        return self._zone.sensor_id

    # -- display ------------------------------------------------------------
    def _current_temperature(self) -> float | None:
        """The room sensor the coordinator reads; None when it rejects it.

        Shares the coordinator's own reader, so the tile shows exactly what the
        plan acted on, in the unit this entity declares. A reading the
        coordinator rejected (dropout, non-numeric, ``nan``/``±inf``, an
        unsupported unit) shows as unknown rather than as a fake room
        temperature — and a non-finite one never reaches HA's ``display_temp``,
        whose ``round()`` would raise inside ``async_write_ha_state``.
        """
        return read_room_temp(
            self.hass.states.get(self._sensor_id), self.coordinator.temp_unit
        )

    @property
    def min_temp(self) -> float:
        """Low slider bound: clamp_min narrowed to the head's native floor (#10)."""
        return self.coordinator.head_target_bounds(self._head_id)[0]

    @property
    def max_temp(self) -> float:
        """High slider bound: clamp_max narrowed to the head's native ceiling (#10)."""
        return self.coordinator.head_target_bounds(self._head_id)[1]

    def _target_temperature(self) -> float | None:
        return self._zone.target

    if _NATIVE_TEMPERATURES:
        native_current_temperature = property(lambda self: self._current_temperature())
        native_target_temperature = property(lambda self: self._target_temperature())
    else:
        current_temperature = property(lambda self: self._current_temperature())
        target_temperature = property(lambda self: self._target_temperature())

    @property
    def hvac_mode(self) -> HVACMode:
        """OFF when the room is disabled; otherwise the single-target auto mode.

        Hands-off rooms too: this mode is the room's coordinator switch (OFF =
        the coordinator is not running this room), it is what turning the tile
        on/off writes, and HEAT_COOL is its only other mode. What a person is
        running on the head shows in ``hvac_action`` instead.
        """
        return HVACMode.HEAT_COOL if self._enabled else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction:
        """What this room's head is ACTUALLY doing under the plan.

        A room may be engaged (wants heat) while the shared compressor runs the
        other mode — its head is parked, so the action must read IDLE, not
        HEATING. Only an engagement matching the plan's shared mode is active.

        A disabled hands-off room shows its head as it is: someone may be
        running it on its own remote, and the tile's action must not say off
        while it heats. (hvac_mode stays OFF, so HomeKit's target state does
        too; only its current state follows the head.) So does a disabled room
        whose freeze/overheat floor is tripped: MXZ may be running its head.
        """
        if not self._enabled and (
            self._zone.hands_off or self.coordinator.floor_tripped(self._zone.slug)
        ):
            return self._head_action()
        if not self.coordinator.coordinator_enable or not self._enabled:
            return HVACAction.OFF
        engage = (self.coordinator.data or {}).get(f"{self._zone.slug}_engage")
        shared = (self.coordinator.data or {}).get("state")
        if engage == MODE_COOL and shared == MODE_COOL:
            return HVACAction.COOLING
        if engage == MODE_HEAT and shared == MODE_HEAT:
            return HVACAction.HEATING
        return HVACAction.IDLE  # satisfied / parked (opposite mode) / neutral

    def _head_action(self) -> HVACAction:
        """The head's own hvac_action when it reports one, else from its mode."""
        head = self.hass.states.get(self._head_id)
        if head is None or head.state in UNAVAILABLE_STATES:
            return HVACAction.OFF
        reported = head.attributes.get("hvac_action")
        if reported in HVACAction.__members__.values():
            return HVACAction(reported)
        return _ACTION_FOR_MODE.get(head.state, HVACAction.IDLE)

    # -- vane / swing (optional passthrough to a select entity) -------------
    @property
    def swing_modes(self) -> list[str] | None:
        return self._vane_options(self._vane_vertical_id)

    @property
    def swing_mode(self) -> str | None:
        return self._vane_state(self._vane_vertical_id)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        await self._set_vane(self._vane_vertical_id, swing_mode)

    @property
    def swing_horizontal_modes(self) -> list[str] | None:
        return self._vane_options(self._vane_horizontal_id)

    @property
    def swing_horizontal_mode(self) -> str | None:
        return self._vane_state(self._vane_horizontal_id)

    async def async_set_swing_horizontal_mode(
        self, swing_horizontal_mode: str
    ) -> None:
        await self._set_vane(self._vane_horizontal_id, swing_horizontal_mode)

    def _vane_options(self, vane_id: str | None) -> list[str] | None:
        if not vane_id:
            return None
        state = self.hass.states.get(vane_id)
        return state.attributes.get("options") if state else None

    def _vane_state(self, vane_id: str | None) -> str | None:
        if not vane_id:
            return None
        state = self.hass.states.get(vane_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            return None
        return state.state

    async def _set_vane(self, vane_id: str | None, option: str) -> None:
        if not vane_id:
            return
        # The coordinator applies it — and if the head is OFF (eco/away), it
        # briefly kicks the head into fan_only so the louvre physically moves,
        # then hands the head back to the plan.
        await self.coordinator.async_apply_vane(self._head_id, vane_id, option)

    # -- fan (passthrough to the underlying head) ---------------------------
    @property
    def fan_modes(self) -> list[str] | None:
        return head_fan_modes(self.hass, self._head_id)

    @property
    def fan_mode(self) -> str | None:
        """The head's commanded fan token — with actual airflow shown under `auto`.

        DISPLAY ONLY. When the head's COMMANDED fan is `auto` (the coordinator
        has handed the fan back to the firmware's own ramp) and a stage sensor
        is wired, show the firmware's decoded ACTUAL blower speed mapped to the
        matching settable rung, so the tile/HomeKit slider reflects real airflow
        instead of freezing on the token `auto`. Any other commanded token, an
        unavailable/unmapped stage, or no stage sensor -> the head's real
        commanded token (today's behavior). The latch machinery in the
        coordinator always reads the HEAD's fan_mode, never this mapped value.
        """
        state = self.hass.states.get(self._head_id)
        commanded = state.attributes.get("fan_mode") if state else None
        if commanded != FAN_AUTO or not self._stage_sensor_id:
            return commanded
        stage_state = self.hass.states.get(self._stage_sensor_id)
        if stage_state is None or stage_state.state in UNAVAILABLE_STATES:
            return commanded
        # ESPHome text sensors report uppercase ("GENTLE"); normalize.
        rung = STAGE_TO_FAN.get(stage_state.state.strip().upper())
        # Only surface a rung the tile actually offers (must be in fan_modes so
        # HomeKit renders it); otherwise fall through to the commanded token.
        modes = self.fan_modes
        if rung is not None and (modes is None or rung in modes):
            return rung
        return commanded

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Fan is independent of the coordinator's mode/setpoints -> drive the head.

        Forwarded verbatim to the head (unchanged manual-hold semantics). Note:
        if the tile is currently DISPLAYING a mapped rung (e.g. "low" mirrored
        from stage GENTLE while the firmware runs its own `auto`) and the user
        selects that same "low", this issues set_fan_mode low on the head — a
        genuine manual hold at low, exactly as intended (the coordinator's latch
        picks it up as a deliberate departure from `auto`)."""
        await self.coordinator.async_head_service(
            self._head_id, "set_fan_mode", {"fan_mode": fan_mode}, manual=True,
        )

    # -- write paths (drive the helper entities; never coordinator state) ---
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Single-target write -> drive the room's number entity.

        Honors ``hvac_mode`` when the caller includes it: HA's
        ``climate.set_temperature`` service passes it straight through in
        kwargs, and entities are expected to apply it, so a script that sends
        ``hvac_mode: heat_cool`` + a temperature to a room that is OFF must
        turn the room on rather than silently drop the mode (#17).

        Applied FIRST, through the same enable switch ``async_set_hvac_mode``
        drives (never the head): the mode then still lands if the temperature
        half is a no-op (no ``temperature`` key, or the number entity isn't
        resolvable). Both writes are absorbed by one debounced recompute, so
        the plan sees the new mode and setpoint together.
        """
        if (hvac_mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(hvac_mode)
        temp = kwargs.get(ATTR_TEMPERATURE)
        target_eid = self._sibling_eid("number", f"{self._zone.slug}_target")
        if temp is None or target_eid is None:
            return
        # Snap to the unit resolution (whole °F / 0.5 °C). The number entity
        # seeds the coordinator + recomputes (number.py).
        step = self.coordinator.target_step or 1.0
        value = round(float(temp) / step) * step
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": target_eid, "value": value},
            blocking=True,
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._set_enable(hvac_mode != HVACMode.OFF)

    async def async_turn_on(self) -> None:
        await self._set_enable(True)

    async def async_turn_off(self) -> None:
        await self._set_enable(False)

    async def _set_enable(self, on: bool) -> None:
        enable_eid = self._sibling_eid("switch", f"{self._zone.slug}_enable")
        if enable_eid is None:
            return
        # The switch entity seeds the coordinator + recomputes (switch.py).
        await self.hass.services.async_call(
            "switch",
            "turn_on" if on else "turn_off",
            {"entity_id": enable_eid},
            blocking=True,
        )


class MXZRangeRoomClimate(MXZRoomClimate):
    """A room thermostat that shows Heat to and Cool to while its room is opted in.

    While the room isn't opted in to a dual setpoint, each override below
    hands straight back to :class:`MXZRoomClimate`. While it is, the range
    feature replaces the single target, never both: a feature with no value
    would be dishonest, and HomeKit, Alexa, and Google each handle "both, one
    of them empty" differently. Everything else (modes, bounds, step, action,
    fan, swing, and the start-point attributes) is the room thermostat's own.
    It owns no state: writes land on the coordinator's range, which the
    room's heat-to and cool-to numbers display and restore.

    Turning the option on or off switches this same entity, which stays
    available: its features, range, and target change on one state write.
    """

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = super().supported_features
        if not self._zone.dual:
            return features
        return (
            features & ~ClimateEntityFeature.TARGET_TEMPERATURE
        ) | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE

    def _target_temperature(self) -> float | None:
        return None if self._zone.dual else super()._target_temperature()

    def _target_temperature_low(self) -> float | None:
        return self._zone.target_low if self._zone.dual else None

    def _target_temperature_high(self) -> float | None:
        return self._zone.target_high if self._zone.dual else None

    if _NATIVE_TEMPERATURES:
        native_target_temperature_low = property(lambda self: self._target_temperature_low())
        native_target_temperature_high = property(lambda self: self._target_temperature_high())
    else:
        target_temperature_low = property(lambda self: self._target_temperature_low())
        target_temperature_high = property(lambda self: self._target_temperature_high())

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """A range write: the final pair, checked, then stored in one step.

        A single-target room's write is :class:`MXZRoomClimate`'s. In a dual
        room, HA only reaches here with both edges (or neither: a mode-only
        call), already inside min_temp/max_temp. A HomeKit threshold change
        arrives as a pair with the other edge unchanged, so the push rule
        applies to it; a pair that moves both edges too close together, or
        crosses them, is refused with a translated error. The pair is checked
        before anything is written, so a refused call changes nothing (its
        mode included). Then ``hvac_mode`` goes first, as on the
        single-target tile (#17), and the pair is stored at once, not one
        number at a time, so no recompute sees half a move.
        """
        if not self._zone.dual:
            await super().async_set_temperature(**kwargs)
            return
        new = self._checked_range(kwargs)
        if (hvac_mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(hvac_mode)
        if new is not None:
            self.coordinator.set_zone_range(self._zone, *new)
            await self.coordinator.async_user_changed()

    def _checked_range(self, kwargs: dict[str, Any]) -> tuple[float, float] | None:
        zone = self._zone
        low, high = kwargs.get(ATTR_TARGET_TEMP_LOW), kwargs.get(ATTR_TARGET_TEMP_HIGH)
        if (low is None and high is None) or zone.target_low is None or zone.target_high is None:
            return None
        try:
            return resolve_range(
                low=zone.target_low, high=zone.target_high,
                new_low=zone.target_low if low is None else float(low),
                new_high=zone.target_high if high is None else float(high),
                gap=self.coordinator.dual_min_gap,
                bounds=self.coordinator.head_target_bounds(zone.climate_id),
                step=self.coordinator.target_step,
            )
        except RangeError as err:
            raise range_error(self.coordinator, zone, err) from err
