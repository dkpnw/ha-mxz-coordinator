"""Allow Dry mode (opt-in, per room): which rooms are in dry, and what MXZ does about it.

A person can run a room's head in dry, from its own remote or from the room's
thermostat. While the room offers Dry, MXZ leaves the head there ("held"), or
parks it at its idle action when another room wins heat ("parked", with one
notice, and Dry is never sent back). Everything here is in memory: after a
restart a head still in dry is simply held again.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .capabilities import head_lists_dry, state_lists_dry
from .const import (
    DRY_PICK_WAIT,
    MODE_DRY,
    MODE_HEAT,
    UNAVAILABLE_STATES,
    ZONE_ALLOW_DRY,
    ZONE_CLIMATE,
)
from .host import ControllerHost, HeadCommands

if TYPE_CHECKING:
    from .cloud import CloudHead
    from .coordinator import Zone

_LOGGER = logging.getLogger(__package__)


class DryHost(ControllerHost, Protocol):
    """The coordinator members Dry reads or calls, beyond every controller's.

    Read: the kill switch, the standby hold, and a cloud head's write record
    (whether it answers, its confirm wait, and whether a dry report can be
    older than its last batch). Called: the delivery queue, for the head service
    a Dry pick sends.
    """

    coordinator_enable: bool
    inhibited: bool

    def cloud_head(self, climate_id: str) -> CloudHead | None: ...
    @property
    def deliveries(self) -> HeadCommands: ...


class DryMode:
    """Each Allow Dry room's dry state, its thermostat's Dry pick, and the Dry notices.

    The coordinator calls it from the compute (``in_dry``, ``settle``,
    ``attributes``), the apply (``notice_parked``), the heals (``allowed``), a
    head's report (``head_reported``), and its teardown (``cancel_timers``).
    The thermostat calls ``offered``, ``in_dry``, ``state``, ``pick_refusal``,
    ``start_pick``, ``async_send`` and ``release``.
    """

    def __init__(self, host: DryHost, zones: list[Zone]) -> None:
        """Nothing in dry yet.

        Per room: held or parked, as the last compute found it; whether it was
        told about its park, until its head reports a mode other than dry; the
        wakeup of a Dry picked on its thermostat that its head hasn't reported
        yet (DRY_PICK_WAIT); and whether a person took it out of dry on that
        thermostat (Heat/Cool or Off), until its head reports another mode.
        """
        self._host = host
        self._hass = host.hass
        self._zones = zones
        self._rooms: dict[str, str] = {}
        self._noticed: set[str] = set()
        self._heat_for: str | None = None  # the room heat is for, by name
        self._picks: dict[str, Any] = {}
        self._released: set[str] = set()

    def offered(self, zone: Zone) -> bool:
        """Whether this room offers Dry: Allow Dry on, and its head lists dry now.

        The gate for the room thermostat's Dry and for every dry MXZ keeps.
        A cool lockout doesn't close it (Dry just can't win the mode then).
        """
        return zone.allow_dry and head_lists_dry(self._hass, zone.climate_id)

    def allowed(self, climate_id: str, mode: str | None) -> bool:
        """Whether this report is a head in dry that its room lets stay there.

        The heals read this: an enabled room that offers Dry is never put
        back from dry (a park for heat goes through the plan instead), unless
        a person took it out of dry on its thermostat. Any other room in dry
        is healed exactly as before the option existed.
        """
        return mode == MODE_DRY and any(
            zone.climate_id == climate_id and self.offered(zone) and zone.enable
            and zone.slug not in self._released
            for zone in self._zones
        )

    def in_dry(self, zone: Zone) -> bool:
        """Whether a person has this room's head in dry, and MXZ honors it now.

        The plan's dry input for this room, and what its thermostat shows as
        Dry: read live, so a pick, Heat/Cool or Off shows at once, before the
        plan that follows settles the room held or parked.

        Only while the room offers Dry and is enabled, no standby hold is on
        (a hold parks every head, a dry one too), and no person took it out
        of dry on its thermostat. A cloud head's report of dry that may be
        older than MXZ's last batch (inside its confirm wait), or from a head
        out of MXZ's control, isn't one: that report isn't a person's choice
        MXZ can act on. A Dry just picked on the thermostat is one until the
        head reports it, or the pick's wait runs out.
        """
        if (
            not self.offered(zone) or not zone.enable or self._host.inhibited
            or zone.slug in self._released
        ):
            return False
        head = self._hass.states.get(zone.climate_id)
        if head is None or head.state in UNAVAILABLE_STATES:
            return False
        cloud = self._host.cloud_head(zone.climate_id)
        if cloud is not None and cloud.unresponsive:
            return False
        if head.state != MODE_DRY:
            return zone.slug in self._picks
        return cloud is None or not cloud.stale(self._host.clock(), mode=MODE_DRY)

    def state(self, slug: str) -> str | None:
        """held or parked while this room's head is in dry it may keep; else None."""
        return self._rooms.get(slug)

    def attributes(self, zone: Zone, vote: str | None) -> dict[str, Any]:
        """An Allow Dry room's dry state and vote; nothing for a room without it.

        A room that never opted in keeps exactly the row it had before.
        """
        if not zone.allow_dry:
            return {}
        return {
            "allow_dry": True,
            "dry": self._rooms.get(zone.slug),
            "dry_vote": vote,
        }

    def diagnostics(self, slug: str) -> dict[str, Any]:
        """This room's Dry state, pick and release, as diagnostics shows them."""
        return {
            "dry": self._rooms.get(slug),
            "dry_pick_pending": slug in self._picks,
            "dry_released": slug in self._released,
        }

    def settle(
        self,
        drys: list[bool],
        state: str,
        demands: list[str],
        engages: list[str],
        votes: list[str | None],
    ) -> None:
        """Settle each dry room as held or parked for this plan.

        Parked while the shared mode is heat and some room wants heat: a room
        calling or running heat (as arbitration counts it), a hands-off head
        heating, or a tripped freeze floor (a tripped floor is its room's
        engage, so ``engages`` carries it). The outdoor unit can't run a dry
        head then; it would only wait in standby. Heat that is only the
        resting mode, with no room wanting it, parks nothing: the dry head's
        cool vote turns the mode once the mode-change wait allows.
        """
        heat_for = (
            next(
                (
                    zone.name
                    for zone, *wants in zip(self._zones, demands, engages, votes)
                    if MODE_HEAT in wants
                ),
                None,
            )
            if state == MODE_HEAT else None
        )
        self._heat_for = heat_for
        self._rooms = {
            zone.slug: "held" if heat_for is None else "parked"
            for zone, dry in zip(self._zones, drys)
            if dry
        }

    def notice_parked(self, zone: Zone) -> None:
        """Tell people once that a dry head was parked for heat (no retry, no restore)."""
        if zone.slug in self._noticed:
            return
        self._noticed.add(zone.slug)
        message = (
            f"{zone.name} Dry paused: the outdoor unit is heating for "
            f"{self._heat_for or 'another room'}. MXZ won't turn Dry back on; "
            "choose it again when you want it."
        )
        _LOGGER.info("MXZ: %s", message)
        self._hass.async_create_task(self._host.notify(message))

    def head_reported(
        self, climate_id: str, old_state: Any, new_state: Any, *, stale: bool
    ) -> bool:
        """A head's report; return whether it ends a room's Dry, so the plan reruns now.

        One notice per park: a head seen in another mode has left dry
        (straight from dry, or back from out of sight), so a later dry parked
        for heat is a new notice. Read from the report, not the next compute:
        a person can pick dry again before that runs. A room taken out of dry
        on its thermostat is out of it now. A report of dry lands a Dry
        picked on the thermostat. ``stale`` is a cloud head's report that can
        be older than its last batch: none of this.
        """
        mode = new_state.state if new_state else None
        if stale:
            return False
        if mode not in (None, *UNAVAILABLE_STATES, MODE_DRY):
            for zone in self._zones:
                if zone.climate_id == climate_id:
                    self._noticed.discard(zone.slug)
                    self._released.discard(zone.slug)
        ended = False
        if mode == MODE_DRY:
            for zone in self._zones:
                if zone.climate_id != climate_id:
                    continue
                self._cancel_pick(zone.slug)
                if (
                    self.state(zone.slug) is not None
                    and state_lists_dry(old_state) and not state_lists_dry(new_state)
                ):
                    # The head dropped dry from its list while in it: the room
                    # offers Dry no more and the plan has the head again.
                    ended = True
                    self._ended_notice(zone, "its head no longer offers Dry")
        return ended

    def pick_refusal(self, zone: Zone) -> str | None:
        """Why Dry can't be picked on this room's thermostat now, as a translation key."""
        if not self.offered(zone):
            return "dry_not_offered"
        if not self._host.coordinator_enable:
            return "dry_coordinator_off"
        if self._host.inhibited:
            return "dry_standby_hold"
        return None

    def start_pick(self, zone: Zone) -> bool:
        """Dry picked on the thermostat: count the head as in dry until it says so.

        Returns whether the head still has to be sent dry. A head already
        reporting dry needs nothing sent; the room is just enabled.
        """
        self._released.discard(zone.slug)
        head = self._hass.states.get(zone.climate_id)
        cloud = self._host.cloud_head(zone.climate_id)
        if head is not None and head.state == MODE_DRY and not (
            cloud is not None and cloud.stale(self._host.clock(), mode=MODE_DRY)
        ):
            return False
        self._cancel_pick(zone.slug)
        wait = max(DRY_PICK_WAIT, cloud.confirm_wait if cloud is not None else 0)
        self._picks[zone.slug] = async_call_later(
            self._hass, wait, partial(self._pick_expired, zone)
        )
        return True

    async def async_send(self, zone: Zone) -> None:
        """Send the head dry, once: the one write a Dry pick makes."""
        try:
            await self._host.deliveries.async_send(
                zone.climate_id, "set_hvac_mode", {"hvac_mode": MODE_DRY}, manual=True,
            )
        except HomeAssistantError:
            self._cancel_pick(zone.slug)
            self._host.redraw_rooms()  # Dry off the thermostat now, not when the plan runs
            await self._host.async_request_refresh()
            raise

    def release(self, zone: Zone) -> None:
        """Heat/Cool or Off picked on the thermostat: out of dry, the plan's again."""
        if zone.slug in self._picks or self.in_dry(zone):
            self._cancel_pick(zone.slug)
            self._released.add(zone.slug)

    def option_turned_off(self, raw_zones: Any) -> None:
        """Before a reload: tell people once for each room an options save took out of Dry."""
        if not isinstance(raw_zones, list):
            return
        kept = {
            z.get(ZONE_CLIMATE) for z in raw_zones
            if isinstance(z, dict) and z.get(ZONE_ALLOW_DRY) is True
        }
        present = {z.get(ZONE_CLIMATE) for z in raw_zones if isinstance(z, dict)}
        for zone in self._zones:
            if (
                self.state(zone.slug) is not None
                and zone.climate_id in present and zone.climate_id not in kept
            ):
                self._ended_notice(zone, "Allow Dry mode was turned off")

    def cancel_timers(self) -> None:
        """Teardown: no pick wakes a retired coordinator."""
        for cancel in self._picks.values():
            cancel()
        self._picks.clear()

    def _ended_notice(self, zone: Zone, why: str) -> None:
        """Tell people once why a room's Dry ended and the plan has its head again."""
        message = f"{zone.name} Dry ended: {why}. MXZ is back on the room's temperature goal."
        _LOGGER.info("MXZ: %s", message)
        self._hass.async_create_task(self._host.notify(message))

    def _cancel_pick(self, slug: str) -> None:
        if (cancel := self._picks.pop(slug, None)) is not None:
            cancel()

    @callback
    def _pick_expired(self, zone: Zone, _now: Any) -> None:
        """The head never reported the Dry picked for it: the plan has it again."""
        self._picks.pop(zone.slug, None)
        _LOGGER.warning(
            "MXZ: %s was sent dry but its head didn't report it in time; "
            "back to the room's plan", zone.name,
        )
        self._host.redraw_rooms()  # Dry off the thermostat now, not when the plan runs
        self._hass.async_create_task(self._host.async_request_refresh())
