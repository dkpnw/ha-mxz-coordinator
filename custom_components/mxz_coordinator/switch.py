"""On/off helpers: per-zone enables, eco-idle, lockouts, and the kill-switch.

Replaces the input_boolean.* helpers from the YAML package. All default OFF on a
fresh install (matching the package, where `initial` was omitted). Each shows and sets
a coordinator value that the room state store keeps across restarts; for one release
they also keep their restore data, which a start takes where it is newer. The per-zone
Fan auto switches render live from the coordinator's latch, whose held-or-not bool is
how the seed tells boost residue from a deliberate hold after a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .capabilities import head_has_fan_auto
from .const import (
    KEY_COOL_LOCKOUT,
    KEY_COORDINATOR_ENABLE,
    KEY_ECO_IDLE,
    KEY_HEAT_LOCKOUT,
)
from .coordinator import MXZCoordinator, Zone
from .entity import MXZEntity
from .fan_hold import (
    ATTR_FAN_ON_PENDING,
    ATTR_LAST_FAN_COMMAND,
    ATTR_OLDER_FAN_COMMANDS,
    ATTR_PRIOR_FAN_COMMAND,
    FanHoldRestoreData,
    older_tokens,
    pending_handback,
    stored_token,
)

_GLOBAL_ICONS = {
    KEY_COORDINATOR_ENABLE: "mdi:hvac",
    KEY_ECO_IDLE: "mdi:leaf",
    KEY_HEAT_LOCKOUT: "mdi:fire-off",
    KEY_COOL_LOCKOUT: "mdi:snowflake-off",
}
_ZONE_ICONS = ("mdi:bed", "mdi:sofa")  # legacy zone-0/1 icons; generic beyond


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the per-zone enables and the coordinator-level switches."""
    coordinator: MXZCoordinator = entry.runtime_data
    entities: list[SwitchEntity] = [
        MXZZoneEnableSwitch(coordinator, zone) for zone in coordinator.zones
    ]
    entities.extend(
        MXZZoneFanAutoSwitch(coordinator, zone) for zone in coordinator.zones
    )
    entities.extend(MXZSwitch(coordinator, key) for key in _GLOBAL_ICONS)
    entities.extend(
        MXZWindowOverrideSwitch(coordinator, zone)
        for zone in coordinator.zones
        if zone.opening_active
    )
    async_add_entities(entities)


class MXZBaseSwitch(MXZEntity, SwitchEntity, RestoreEntity):
    """A restorable on/off helper: a view of a coordinator value, which it sets."""

    def __init__(self, coordinator: MXZCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        """Show the value the start resumed, or restore it from this entity's record.

        The room state store resumed it unless this entity's restore record is
        newer (``MXZCoordinator.store_resumes``). Restores from a PREVIOUS entry
        incarnation are ignored (#7) — the dangerous case is a deleted entry's
        kill-switch ON resurrecting onto a freshly re-added entry. A rename's
        re-add neither restores nor seeds: seeding as restored would mark the
        coordinator and its rooms as resuming from an HA start (see
        ``_readded``).
        """
        await super().async_added_to_hass()
        if self._readded:
            return
        last = await self.async_get_last_state()
        if self.coordinator.store_resumes(*self._stored_as, self._recorded_at(last)):
            self._attr_is_on = self._resumed()
            return
        if last is not None and not self._restored_state_is_stale(last):
            self._attr_is_on = last.state == "on"
        await self._async_restore(last)

    @property
    def _stored_as(self) -> tuple[str, str]:
        """How ``MXZCoordinator.store_resumes`` names this value."""
        raise NotImplementedError

    def _resumed(self) -> bool:
        """The value the start resumed from the store."""
        raise NotImplementedError

    async def _async_restore(self, last: State | None) -> None:
        """Hand ``_attr_is_on``, restored from ``last`` or the default, to the coordinator."""
        raise NotImplementedError

    def _set_value(self, on: bool) -> None:
        """A person turned it on or off."""
        raise NotImplementedError

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        self._attr_is_on = value
        self._set_value(value)
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()


class MXZSwitch(MXZBaseSwitch):
    """A coordinator-level flag (kill-switch, eco-idle, lockouts)."""

    def __init__(self, coordinator: MXZCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._key = key
        self._attr_icon = _GLOBAL_ICONS[key]

    @property
    def _stored_as(self) -> tuple[str, str]:
        return ("switch", self._key)

    def _resumed(self) -> bool:
        return cast("bool", getattr(self.coordinator, self._key))

    async def _async_restore(self, last: State | None) -> None:
        self.coordinator.restore_switch(self._key, cast("bool", self._attr_is_on))

    def _set_value(self, on: bool) -> None:
        self.coordinator.set_switch(self._key, on)


@dataclass(frozen=True)
class ZoneEnableRestoreData(ExtraStoredData):
    """What a room's enable switch carries across a restart beside its state.

    ``hands_off_released``: the room is opted in to hands-off, disabled, and
    its off had landed — so after a restart the head is still left alone
    rather than parked again. Absent (an older store) or anything but True is
    not released, and that room parks, as a newly disabled room does.

    ``floor``: a disabled room's floor run and/or hands-off yield, as
    :meth:`floor.FloorGuard.record` gives it. Stored only while one
    exists, so a room with no floor stores exactly what it stored before.
    """

    hands_off_released: bool = False
    floor: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the restore store."""
        return {"hands_off_released": self.hands_off_released, **(self.floor or {})}


class MXZZoneEnableSwitch(MXZBaseSwitch):
    """One zone's enable flag (seeds Zone.enable)."""

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"{zone.slug}_enable")
        self._zone = zone
        self._attr_icon = (
            _ZONE_ICONS[zone.index]
            if zone.index < len(_ZONE_ICONS)
            else "mdi:home-thermometer-outline"
        )
        self._attr_translation_key = "zone_enable"
        self._attr_translation_placeholders = {"zone": zone.name}

    @property
    def _stored_as(self) -> tuple[str, str]:
        return ("enable", self._zone.slug)

    def _resumed(self) -> bool:
        return self._zone.enable

    async def _async_restore(self, last: State | None) -> None:
        """Restore the room's enable, then a hands-off room's landed park and a
        disabled room's floor run.

        Only for a room restored disabled from THIS entry (#7), still opted in
        (hands-off) or still setting that floor: anything else parks, as a
        newly disabled room does, and its floor decides afresh.
        """
        data: dict[str, Any] = {}
        floor = self._zone.floor_low is not None or self._zone.floor_high is not None
        if (
            not self._attr_is_on
            and (self._zone.hands_off or floor)
            and last is not None
            and not self._restored_state_is_stale(last)
            and last.state == "off"
            and (extra := await self.async_get_last_extra_data()) is not None
        ):
            data = extra.as_dict()
        self.coordinator.restore_room_enable(self._zone, cast("bool", self._attr_is_on), data)

    @property
    def extra_restore_state_data(self) -> ZoneEnableRestoreData:
        """Read live at every restore dump, so it needs no state write."""
        return ZoneEnableRestoreData(
            self.coordinator.hands_off_released(self._zone.slug),
            self.coordinator.floor.record(self._zone.slug) or None,
        )

    def _set_value(self, on: bool) -> None:
        self.coordinator.set_room_enable(self._zone, on)


class MXZZoneFanAutoSwitch(
    MXZEntity, CoordinatorEntity[MXZCoordinator], SwitchEntity, RestoreEntity
):
    """Per-zone "Fan auto" toggle — a live mirror of the manual-fan latch.

    ON  = boost/auto drives this head's fan (zone not held).
    OFF = a manual speed is being held.

    The switch renders live from the latch (CoordinatorEntity re-renders every
    cycle) — but it also RESTORES its last state across restarts, and hands
    that one bool to the coordinator before the first compute. That bool is
    what lets the seed tell boost residue from a deliberate hold: without it,
    a head still carrying boost's last fan token at restart is
    indistinguishable from a hold the user placed while the head idled (the
    token value cannot separate them — four shipped bug shapes proved it).
    Reconciliation takes the TOKEN from the observed head. Known holds survive;
    not-held restores use bounded echo tolerance, with an indistinguishable
    remembered-token edge and conservative false holds for stale memory; a
    room idling in fan_only under that idle action reads any speed boost could
    have set as its own (#25). Turning the
    switch ON hands control back to boost; OFF pins the head's current speed.
    Apple's Home app renders only a climate service's fixed characteristics —
    there's no room for a custom control inside the climate tile — so this
    rides alongside it as a plain toggle and doubles as a visible
    who's-driving-the-fan indicator.
    """

    _attr_icon = "mdi:fan-auto"

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        MXZEntity.__init__(self, coordinator, f"{zone.slug}_fan_auto")
        CoordinatorEntity.__init__(self, coordinator)
        self._zone = zone
        self._attr_translation_key = "zone_fan_auto"
        self._attr_translation_placeholders = {"zone": zone.name}

    async def async_added_to_hass(self) -> None:
        """Hand the restored pre-restart hold truth to the coordinator.

        Runs during platform setup, before the coordinator's first compute
        (async_setup_entry awaits the platforms; STARTUP_RECOVER_DELAY adds
        margin on HA start). A stale restore — older than the config entry —
        belongs to a previous incarnation (#7) and is ignored. An older version
        may have a valid bool-only state: it retains hold truth but has no echo
        memory, so auto followed by a late rung remains indistinguishable from
        a new manual pick and conservatively holds.

        A clean on/off state is the answer whenever there is one, and its
        attributes carry the coordinator's echo memory for the head. When
        there is not — the head was missing at shutdown, so HA persisted the
        switch as ``unavailable``, with no attributes — the same truth and
        memory rode along in extra restore data, which HA stores whatever the
        state says. Either way what comes back is the pre-restart hold, so a
        hold the user released restores as released and is never resurrected.

        A rename's re-add hands nothing over (see ``_readded``): the
        coordinator's live hold and echo memory are current, and a restored
        hold would be reconciled again as if after a restart.

        The room state store resumed the same record at the start; this
        record replaces it only where it is newer
        (``MXZCoordinator.store_resumes``). Where the store holds none, a
        record that can't be used is a problem the room reports.
        """
        await super().async_added_to_hass()
        cid = self._zone.climate_id
        last = None if self._readded else await self.async_get_last_state()
        stored: FanHoldRestoreData | None = None
        if last is not None and not self._restored_state_is_stale(last):
            if last.state in ("on", "off"):
                stored = FanHoldRestoreData(
                    last.state == "off",
                    stored_token(last.attributes.get(ATTR_LAST_FAN_COMMAND)),
                    stored_token(last.attributes.get(ATTR_PRIOR_FAN_COMMAND)),
                    pending_handback(last.attributes, last.state == "off"),
                    older_tokens(last.attributes.get(ATTR_OLDER_FAN_COMMANDS)),
                )
            elif (extra := await self.async_get_last_extra_data()) is not None:
                stored = FanHoldRestoreData.from_dict(extra.as_dict())
        recorded_at = last.last_updated if stored is not None and last is not None else None
        if not self.coordinator.store_resumes("fan", cid, recorded_at):
            if stored is not None:
                self.coordinator.fan.restore_record(cid, stored)
            elif last is not None and self._restored_state_is_stale(last):
                self.coordinator.fan.restore_failed(cid, "Previous fan hold belongs to an older entry.")
            elif last is not None:
                self.coordinator.fan.restore_failed(cid, "Previous fan hold is missing or malformed.")
        self.async_on_remove(self.coordinator.fan.attach(cid, self.async_write_ha_state))
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._zone.climate_id], self._handle_head_change
            )
        )

    @callback
    def _handle_head_change(self, _event: Event[Any]) -> None:
        """Refresh availability when the head publishes new capabilities."""
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Echo baselines and a reason when advertised control awaits a speed.

        Published on each ownership/memory change, before any service await.
        Restore filters impossible tokens to auto;
        these attributes are not a command log. An unavailable state has no
        attributes; extra restore data covers its ownership snapshot.
        """
        last, prior = self.coordinator.fan.command_memory(self._zone.climate_id)
        head = self.hass.states.get(self._zone.climate_id)
        reason = None
        if head is not None and head.attributes.get("fan_mode") is None:
            reason = (
                "Current fan speed unavailable; waiting for a head report. "
                "Fan auto intent is retained; no fan command is sent until speed returns."
            )
        attributes: dict[str, Any] = {
            ATTR_LAST_FAN_COMMAND: last, ATTR_PRIOR_FAN_COMMAND: prior,
            "fan_control_reason": reason,
        }
        if self.coordinator.fan.on_pending(self._zone.climate_id):
            attributes[ATTR_FAN_ON_PENDING] = True
        if older := self.coordinator.fan.older_record(self._zone.climate_id):
            attributes[ATTR_OLDER_FAN_COMMANDS] = older
        return attributes

    @property
    def extra_restore_state_data(self) -> FanHoldRestoreData:
        """Persist the hold truth and echo memory beside the state, available or not."""
        return self.coordinator.fan.record(self._zone.climate_id)

    @property
    def available(self) -> bool:
        """Expose handback only while the head advertises the exact auto token."""
        return self.coordinator.last_update_success and head_has_fan_auto(
            self.hass, self._zone.climate_id
        )

    @property
    def is_on(self) -> bool:
        """Mirror the latch: ON when boost drives, OFF when a manual hold is active."""
        return self.coordinator.fan.auto_is_on(self._zone.climate_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Release the zone's latch and let boost reassert."""
        await self.coordinator.fan.async_set_auto(self._zone.climate_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Latch the zone at the head's current fan speed (no-op if it's at auto)."""
        await self.coordinator.fan.async_set_auto(self._zone.climate_id, False)
        self.async_write_ha_state()


class MXZWindowOverrideSwitch(MXZEntity, CoordinatorEntity[MXZCoordinator], SwitchEntity):
    """A room's Window Override: run the room although its door/window is open.

    Only a room with a door/window pause has one. On: the open door/window is
    ignored, so a paused room resumes at once. It turns itself off when the
    door/window reads closed, so the next opening pauses as usual. It also
    reads on while a person's own change on the paused head has taken the
    room over (the same until-closed override); turning it off then pauses
    the room again. Kept with the pause across a restart (the coordinator's
    door/window record), so no restore of its own.
    """

    _attr_icon = "mdi:window-open-variant"

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        MXZEntity.__init__(self, coordinator, f"{zone.slug}_window_override")
        CoordinatorEntity.__init__(self, coordinator)
        self._zone = zone
        self._attr_translation_key = "zone_window_override"
        self._attr_translation_placeholders = {"zone": zone.name}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_delivery_listener(self.async_write_ha_state)
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.openings.override_on(self._zone.slug)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.openings.async_set_override(self._zone, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.openings.async_set_override(self._zone, False)
        self.async_write_ha_state()
