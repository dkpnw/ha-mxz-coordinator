"""Per-zone setpoint target numbers (replaces input_number.hvac_*_target)."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MXZCoordinator, Zone
from .entity import MXZEntity
from .logic import RangeError, resolve_range, shift_range


def range_error(coordinator: MXZCoordinator, zone: Zone, err: RangeError) -> ServiceValidationError:
    """A refused dual-setpoint write as a translated ServiceValidationError."""
    lo, hi = coordinator.head_target_bounds(zone.climate_id)
    unit = coordinator.temp_unit
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=err.reason,
        translation_placeholders={
            "zone": zone.name,
            "min": f"{lo:g}{unit}",
            "max": f"{hi:g}{unit}",
            "gap": f"{coordinator.dual_min_gap:g}{unit}",
        },
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one target number and one drift number per zone.

    A dual-setpoint room also gets its heat-to and cool-to numbers, added
    after every target (entities without update_before_add are added in list
    order), so a range seeded from the target reads the restored target.
    """
    coordinator: MXZCoordinator = entry.runtime_data
    entities: list[RestoreNumber] = [
        (MXZRangeTargetNumber if zone.dual else MXZTargetNumber)(coordinator, zone)
        for zone in coordinator.zones
    ]
    entities.extend(
        MXZDriftNumber(coordinator, zone) for zone in coordinator.zones
    )
    entities.extend(
        MXZRangeNumber(coordinator, zone, low)
        for zone in coordinator.zones if zone.dual
        for low in (True, False)
    )
    async_add_entities(entities)


class MXZTargetNumber(MXZEntity, RestoreNumber):
    """A restorable comfort-target setpoint, bounded to the firmware band."""

    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermostat"

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"{zone.slug}_target")
        self._zone = zone
        # Every zone uses the generic translated name with its own name
        # substituted in ("Bedroom target").
        self._attr_translation_key = "zone_target"
        self._attr_translation_placeholders = {"zone": zone.name}
        # Track the HA system temperature unit + resolution (°F: whole degrees;
        # °C: 0.5° steps). Match the climate tile: bound the target to the
        # firmware operating band [clamp_min, clamp_max].
        self._attr_native_unit_of_measurement = coordinator.temp_unit
        self._attr_native_step = coordinator.target_step
        lo, hi = coordinator.head_target_bounds(zone.climate_id)
        self._attr_native_value = min(max(coordinator.target_default, lo), hi)

    # [clamp_min, clamp_max] narrowed to what THIS head will actually accept
    # (its native operating band), so a rejectable target can't be entered from
    # the UI / HomeKit / voice in the first place (#10). LIVE properties, not
    # frozen at init: a head whose integration loads after ours would otherwise
    # keep the wide fallback bounds until a reload. Validation reads these at
    # set-time, so it is always against the head's real band; the UI slider
    # refreshes its cached bounds on the entity's next state write.
    @property
    def native_min_value(self) -> float:
        return self.coordinator.head_target_bounds(self._zone.climate_id)[0]

    @property
    def native_max_value(self) -> float:
        return self.coordinator.head_target_bounds(self._zone.climate_id)[1]

    async def async_added_to_hass(self) -> None:
        """Restore the last setpoint and seed the coordinator's zone.

        On a FRESH install (nothing to restore) the target seeds from the
        head's current setpoint instead of a hard default, so enabling the
        coordinator never plans against a temperature nobody chose (#6 —
        a 70 °F default vs a 66 °F room planned heat in July).

        A room just switched back from a dual setpoint takes the options
        flow's handoff instead: the range's snapped midpoint.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if (handoff := self._zone.handoff) is not None and "target" in handoff:
            self._attr_native_value = handoff["target"]
        elif (
            not self._restored_state_is_stale(last_state)
            and (last := await self.async_get_last_number_data())
            and last.native_value is not None
        ):
            # Clamp the restored value into the current bounds: a pre-narrowing
            # install can hand back a target the head would reject (a restored
            # 79 °F on a 26.0 °C-native head, #10), and it would otherwise sit
            # in zone.target steering the plan until the user touches the
            # slider. Best-effort — if the head's integration hasn't loaded
            # yet the bounds are still the wide clamp band, and the apply-time
            # head-band clamp in _apply_head backstops whatever gets through.
            self._attr_native_value = min(
                max(last.native_value, self.native_min_value), self.native_max_value
            )
        elif (seed := self._head_setpoint()) is not None:
            self._attr_native_value = seed
        self._zone.target = self._attr_native_value

    def _head_setpoint(self) -> float | None:
        """The head's current setpoint, clamped and snapped to our resolution."""
        state = self.hass.states.get(self._zone.climate_id)
        if state is None:
            return None
        attrs = state.attributes
        raw = attrs.get("temperature")
        if raw is None and attrs.get("target_temp_low") is not None:
            try:
                raw = (
                    float(attrs["target_temp_low"])
                    + float(attrs.get("target_temp_high", attrs["target_temp_low"]))
                ) / 2
            except (TypeError, ValueError):
                raw = None
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        step = self.coordinator.target_step or 1.0
        value = round(value / step) * step
        return min(max(value, self.native_min_value), self.native_max_value)

    async def async_set_native_value(self, value: float) -> None:
        """User changed the target -> persist, reset the latch, recompute."""
        self._attr_native_value = value
        self._zone.target = value
        self.coordinator.reset_engage_latch(self._zone.slug)
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()


class MXZDriftNumber(MXZEntity, RestoreNumber):
    """A room's re-engage drift band — per-room, automatable (#18).

    How far this room may wander past its target before conditioning resumes.
    Defaults to the global drift; write it from automations to widen an empty
    room's tolerance (presence tiers) and tighten it on arrival — tightening
    re-engages on the next compute. The zone's demand vote respects this band
    too, so a wide-tolerance room never steers the shared compressor inside
    its own comfort window. The room's follow-global button hands the band
    back. CONFIG category: on the device page and writable by automations, out
    of auto-populated dashboards and voice.
    """

    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:arrow-expand-vertical"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"{zone.slug}_drift")
        self._zone = zone
        self._last_published_state: tuple[float, bool, float, float] | None = None
        self._attr_translation_key = "zone_drift"
        self._attr_translation_placeholders = {"zone": zone.name}
        self._attr_native_unit_of_measurement = coordinator.temp_unit
        lo, hi = coordinator._profile["engage_bounds"]
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        # Same grain as the global drift's options selector, so any value the
        # global can hold can be copied into a room verbatim.
        self._attr_native_step = lo / 2

    # LIVE, not a frozen _attr: an untouched room (zone.drift is None) must
    # DISPLAY the global drift it actually follows — including a new global
    # after an options change — not the value seeded at construction.
    @property
    def native_value(self) -> float:
        return self.coordinator.zone_drift(self._zone)

    # ``override`` is persisted alongside the state so restore can tell a room
    # the user set apart from one merely displaying the global (RestoreNumber
    # saves the displayed value either way). ``drift_heat`` / ``drift_cool``
    # are read-only: the drift in use per side, which differs from the value
    # whenever uneven drift is set. Restore never reads them.
    @property
    def extra_state_attributes(self) -> dict[str, bool | float]:
        heat, cool = self.coordinator.zone_drift_sides(self._zone)
        return {
            "override": self._zone.drift is not None,
            "drift_heat": heat,
            "drift_cool": cool,
        }

    async def async_added_to_hass(self) -> None:
        """Restore the last per-room drift; absent/stale/not set -> the global.

        Only a state marked ``override`` restores: an untouched room's saved
        state is just yesterday's global, and writing it back would freeze the
        room on a copy. A restored value is clamped into the profile bounds
        (hand-edited or pre-upgrade values bypass the UI). zone.drift stays
        None otherwise, so the room follows the GLOBAL drift live.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if (
            not self._restored_state_is_stale(last_state)
            and last_state is not None
            and last_state.attributes.get("override")
            and (last := await self.async_get_last_number_data())
            and last.native_value is not None
        ):
            self._zone.drift = min(
                max(last.native_value, self._attr_native_min_value),
                self._attr_native_max_value,
            )
        self._last_published_state = self._state_signature()
        self.async_on_remove(self.coordinator.async_add_listener(self._sync))

    def _state_signature(self) -> tuple[float, bool, float, float]:
        """Return the displayed value, the override flag, and the sides in use."""
        return (
            self.native_value,
            self._zone.drift is not None,
            *self.coordinator.zone_drift_sides(self._zone),
        )

    @callback
    def _sync(self) -> None:
        """Redraw a FOLLOWER, whose displayed value this entity doesn't own.

        A follower shows the global drift, which moves under it: the room's
        follow-global button hands the room back to it, and an options change
        replaces it. An override's value only ever changes when this entity is
        written, and that write already publishes its own state. The sides in
        use are in the signature too, so a change to them is redrawn as well.
        """
        state = self._state_signature()
        if state == self._last_published_state:
            return
        self._last_published_state = state
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """An automation (or user) set this room's drift -> recompute.

        Tightening below the room's current wander re-engages on this very
        compute — the walk-in snap-back. The engage latch is deliberately NOT
        reset: a band change redraws the coast window, it does not invalidate
        a run already headed for the (unchanged) target.
        """
        self._zone.drift = value
        self._last_published_state = self._state_signature()
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()


class MXZRangeTargetNumber(MXZTargetNumber):
    """A dual-setpoint room's target number: the range's midpoint, and its mover.

    Kept so presence automations and scripts that write a room's target still
    work after the room switches to a range. It shows the midpoint (which can
    be a half step, the center the range is built around), and writing it
    moves the whole range there, keeping its width and sliding back inside
    the bounds. It restores like any target; the range numbers then restore
    and set the midpoint from the pair.
    """

    @property
    def native_value(self) -> float | None:
        return self._zone.target

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self._sync))

    @callback
    def _sync(self) -> None:
        """Redraw the midpoint when the range moved under it."""
        current = self.hass.states.get(self.entity_id)
        if current is None or current.state != str(self.state):
            self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Move the range so it's centered on ``value``, keeping its width."""
        zone = self._zone
        if zone.target_low is None or zone.target_high is None:
            return  # not restored yet (never reachable once added)
        try:
            low, high = shift_range(
                width=zone.target_high - zone.target_low, center=value,
                bounds=self.coordinator.head_target_bounds(zone.climate_id),
                step=self.coordinator.target_step,
            )
        except RangeError as err:
            raise range_error(self.coordinator, zone, err) from err
        self._attr_native_value = (low + high) / 2
        self.coordinator.set_zone_range(zone, low, high)
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()


class MXZRangeNumber(MXZEntity, RestoreNumber):
    """A dual-setpoint room's Heat to (``low``) or Cool to, restorable.

    The restore layer and automation handle for one edge of the range, as the
    target number is for a single target. Writes follow the range rules
    (:func:`logic.resolve_range`): snapped to the unit's step, inside the
    room's bounds, at least the minimum gap apart, pushing the other edge
    when this one moves too close, and refused, never clamped, otherwise.
    """

    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: MXZCoordinator, zone: Zone, low: bool) -> None:
        super().__init__(coordinator, f"{zone.slug}_target_{'low' if low else 'high'}")
        self._zone = zone
        self._low = low
        self._attr_translation_key = "zone_heat_to" if low else "zone_cool_to"
        self._attr_translation_placeholders = {"zone": zone.name}
        self._attr_icon = "mdi:fire" if low else "mdi:snowflake"
        self._attr_native_unit_of_measurement = coordinator.temp_unit
        self._attr_native_step = coordinator.target_step

    @property
    def native_min_value(self) -> float:
        return self.coordinator.head_target_bounds(self._zone.climate_id)[0]

    @property
    def native_max_value(self) -> float:
        return self.coordinator.head_target_bounds(self._zone.climate_id)[1]

    @property
    def native_value(self) -> float | None:
        return self._zone.target_low if self._low else self._zone.target_high

    async def async_added_to_hass(self) -> None:
        """Restore this edge; the second edge to restore settles the pair.

        A room just switched on takes the options flow's handoff (the range
        seeded around its target). Otherwise a restore that isn't stale is
        offered as it was saved; with none, the pair is seeded around the
        target at the minimum gap. The coordinator then re-checks the pair
        against the current bounds (``finish_range_restore``).
        """
        await super().async_added_to_hass()
        zone = self._zone
        restored: float | None = None
        last_state = await self.async_get_last_state()
        if (
            not self._restored_state_is_stale(last_state)
            and (last := await self.async_get_last_number_data())
            and last.native_value is not None
        ):
            restored = float(last.native_value)
        self.coordinator.offer_range_restore(zone, self._low, restored)
        self.async_on_remove(self.coordinator.async_add_listener(self._sync))

    @callback
    def _sync(self) -> None:
        """Redraw when the range moved under this edge (a push, a shift, a restore)."""
        current = self.hass.states.get(self.entity_id)
        if current is None or current.state != str(self.state):
            self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Write this edge -> validate the resulting pair, store it, recompute."""
        zone = self._zone
        low, high = zone.target_low, zone.target_high
        if low is None or high is None:
            return  # not restored yet (never reachable once added)
        try:
            new = resolve_range(
                low=low, high=high,
                new_low=value if self._low else low,
                new_high=high if self._low else value,
                gap=self.coordinator.dual_min_gap,
                bounds=self.coordinator.head_target_bounds(zone.climate_id),
                step=self.coordinator.target_step,
            )
        except RangeError as err:
            raise range_error(self.coordinator, zone, err) from err
        self.coordinator.set_zone_range(zone, *new)
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()
