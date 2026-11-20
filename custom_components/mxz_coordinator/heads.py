"""Each room's head driver: how one delivery's steps reach its head.

A local head (ESPHome/CN105, or any head not set as cloud or generic) is sent
each command at once, skipped where it already shows it. A cloud head (beta) is
written in batches through its ``cloud.CloudHead`` record, and a generic head
(beta) takes the same path with ``generic`` set (see ``cloud``). Both read what
the head accepts, its native band and its setpoint grid, through ``HeadGrid``.
The delivery queue runs a delivery through ``HeadDrivers.async_deliver``; the
coordinator reads a cloud head's record, control and stale reports through it.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast

from homeassistant.components.climate import ClimateEntity
from homeassistant.const import STATE_UNAVAILABLE, UnitOfTemperature
from homeassistant.core import Event, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.event import async_track_state_report_event
from homeassistant.util import dt as dt_util

from .cloud import CLOUD_PENDING, CloudHead, drive_setpoint
from .const import (
    CLOUD_SILENT_SECONDS,
    CLOUD_STALE_SECONDS,
    CLOUD_STEP_C,
    CLOUD_STEP_F,
    CONFIRM_WAIT_BOUNDS,
    DEFAULT_CONFIRM_WAIT,
    DEFAULT_DRIVE_OFFSET_C,
    DEFAULT_DRIVE_OFFSET_F,
    DEFAULT_WRITE_INTERVAL,
    DOMAIN,
    DRIVE_OFFSET_MAX_C,
    DRIVE_OFFSET_MAX_F,
    GENERIC_CONFIRM_WAIT,
    GENERIC_WRITE_INTERVAL,
    HEAD_TYPE_GENERIC,
    HEAD_TYPE_LOCAL,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
    OFFSET_HEAD_TYPES,
    UNAVAILABLE_STATES,
    VOTE_BASIS_MODE,
    WRITE_INTERVAL_BOUNDS,
)
from .grid import Grid, convert, local_calls
from .host import ControllerHost, Effect
from .logic import as_float, bounded
from .planner import DeliverySteps, delivery_steps
from .timing import Arm, CancelAll, CloudWaits, cloud_arm
from .units import clamp, offset_in_unit, snap_to_step

if TYPE_CHECKING:
    from homeassistant.core import EventStateReportedData, State

    from .coordinator import Zone
    from .delivery import Delivery, DeliveryQueue, FloorReports
    from .fan_hold import FanHold

_LOGGER = logging.getLogger(__package__)

# HA 2026.11 renamed ClimateEntity.temperature_unit native_temperature_unit. The
# old name answers until 2027.11, but reading it logs a deprecation against this
# integration. Both return the same value.
_UNIT_MEMBER = (
    "native_temperature_unit"
    if hasattr(ClimateEntity, "native_temperature_unit")
    else "temperature_unit"
)


def _native_unit(entity: Any) -> Any:
    """A head entity object's own temperature unit (AttributeError if it has none)."""
    return getattr(entity, _UNIT_MEMBER)


class HeadsHost(ControllerHost, Protocol):
    """The coordinator members the drivers read or call, beyond every controller's.

    Read: the unit, step and clamp band, the standby hold, the idle action and
    eco (for the fan's hand-back), the delivery queue and the fan hold. Called:
    the publication of the plan's rows, the floor's record of an accepted
    command, the coil-dry dwell's note of what a head is being sent
    (``planner.delivery_steps`` says what it counts), and the arming of a wakeup.
    """

    temp_unit: str
    celsius: bool
    target_step: float
    clamp_min: float
    clamp_max: float
    inhibited: bool
    idle_action: str
    deliveries: DeliveryQueue
    fan: FanHold

    @property
    def floor(self) -> FloorReports: ...

    def eco_active(self) -> bool: ...
    def publish_delivery(self) -> None: ...
    def note_head_act(self, climate_id: str, steps: DeliverySteps, act: str, at: float) -> None: ...
    def perform_arm(self, arm: Arm | CancelAll) -> None: ...


class HeadGrid:
    """What each head accepts, read off its live climate entity (#10), as a ``grid.Grid``."""

    def __init__(self, host: HeadsHost) -> None:
        self._host = host

    def entity(self, climate_id: str) -> Any:
        """The head's live climate entity object, or None if not reachable."""
        component = (self._host.hass.data.get(DATA_INSTANCES) or {}).get("climate")
        return component.get_entity(climate_id) if component else None

    def native_limits(
        self, climate_id: str
    ) -> tuple[float, float, str] | None:
        """This head's UNROUNDED native (min, max, unit), or None if unknown."""
        return _native_limits(self.entity(climate_id))

    def read(self, climate_id: str) -> Grid:
        """This head's grid as its entity shows it now.

        The native band is read from the live entity object, the same values HA's own
        set_temperature validator range-checks against, because the head's min_temp/max_temp
        STATE ATTRIBUTES are display-rounded to the system unit's precision and so can read
        ABOVE the true native ceiling (a 26.0 °C max shows as 79 °F but rejects 79 °F). Only
        when that isn't known (no entity, an odd unit, an inverted band) are the state
        attributes read.
        """
        host = self._host
        entity = self.entity(climate_id)
        native = _native_limits(entity)
        shown_band = None
        if native is None and (st := host.hass.states.get(climate_id)) is not None:
            shown_band = (
                as_float(st.attributes.get("min_temp")), as_float(st.attributes.get("max_temp")),
            )
        try:
            step = float(entity.target_temperature_step)
            unit = _native_unit(entity)
        except (TypeError, ValueError, AttributeError):
            step, unit = 0.0, None
        return Grid(
            system_unit=host.temp_unit,
            target_step=host.target_step,
            clamp_min=host.clamp_min,
            clamp_max=host.clamp_max,
            native=native,
            shown_band=shown_band,
            native_step=step,
            native_unit=unit,
        )

    def safe_band(self, climate_id: str) -> tuple[float, float] | None:
        """The head's operating band as system-unit edges it will accept (``Grid.safe_band``)."""
        return self.read(climate_id).safe_band()

    def clamp(
        self, climate_id: str, low: float, high: float
    ) -> tuple[float, float]:
        """Clamp (low, high) into the head's accept-able band (``Grid.clamp``)."""
        return self.read(climate_id).clamp(low, high)

    def target_bounds(self, climate_id: str) -> tuple[float, float]:
        """UI setpoint bounds for a zone (``Grid.target_bounds``)."""
        return self.read(climate_id).target_bounds()

    def already_at(
        self, climate_id: str, reported: float | None, desired: float
    ) -> bool:
        """True if sending ``desired`` would leave the head's setpoint unchanged (``Grid.already_at``)."""
        return self.read(climate_id).already_at(reported, desired)


def _native_limits(entity: Any) -> tuple[float, float, str] | None:
    """An entity object's UNROUNDED native (min, max, unit), or None if unknown.

    Best-effort: a missing entity, an odd unit or an inverted band yields None and the caller
    falls back.
    """
    if entity is None:
        return None
    try:
        nmin = float(entity.min_temp)
        nmax = float(entity.max_temp)
        nunit = _native_unit(entity)
    except (TypeError, ValueError, AttributeError):
        return None
    if nunit not in (
        UnitOfTemperature.CELSIUS,
        UnitOfTemperature.FAHRENHEIT,
    ) or nmax <= nmin:
        return None
    return (nmin, nmax, nunit)


class HeadDriver(Protocol):
    """One head's driver: one delivery's steps at its head.

    Called once the delivery holds the head's turn and is still owned.
    ``engage`` None is a fixed standby park or a floor, with no fan machinery.
    """

    climate_id: str

    async def async_deliver(
        self, delivery: Delivery, act: str,
        low: float, high: float, delta: float, engage: str | None,
    ) -> None: ...


def _steps(host: HeadsHost, act: str, engage: str | None, state: State | None) -> DeliverySteps:
    """What this delivery does at its head besides the mode and setpoints (``planner.delivery_steps``)."""
    return delivery_steps(
        act, engage,
        idle_action=host.idle_action,
        eco=host.eco_active(),
        inhibited=host.inhibited,
        head_state=state.state if state is not None else None,
    )


class LocalDriver:
    """A local head: each command sent at once, skipped where the head shows it already.

    The mode and setpoints first, then the fan, or the fan's hand-back to auto
    first on the way to off.
    """

    def __init__(self, climate_id: str, host: HeadsHost, grid: HeadGrid) -> None:
        self.climate_id = climate_id
        self._host = host
        self._grid = grid

    async def async_deliver(
        self, delivery: Delivery, act: str,
        low: float, high: float, delta: float, engage: str | None,
    ) -> None:
        """One delivery's steps at this head, in the order ``DeliverySteps.order`` gives."""
        host = self._host
        climate_id = self.climate_id
        at = host.clock()
        steps = _steps(host, act, engage, host.hass.states.get(climate_id))
        for step in steps.order:
            if step == "note":
                host.note_head_act(climate_id, steps, act, at)
                continue
            if step == "handback":
                await host.fan.async_apply(climate_id, MODE_FAN_ONLY, delta)
            elif step == "setpoints":
                await self._async_apply(act, low, high)
                host.floor.accepted(climate_id, delivery)
            else:
                # The standby hold as it reads now, after the write: a hold that started
                # or ended during the write decides the fan step.
                fan = steps.fan(inhibited=host.inhibited)
                if fan == "drop_rung":
                    host.fan.drop_rung(climate_id)
                elif fan == "boost":
                    await host.fan.async_apply(climate_id, act, delta)
            if not host.deliveries.owned(delivery):
                return

    async def _async_apply(self, act: str, low: float, high: float) -> None:
        """Send this head what ``grid.local_calls`` says for the command, in order; or nothing."""
        host = self._host
        climate_id = self.climate_id
        state = host.hass.states.get(climate_id)
        entry = er.async_get(host.hass).async_get(climate_id)
        for service, data in local_calls(
            act, low, high,
            mode=state.state if state else None,
            attributes=state.attributes if state else None,
            grid=self._grid.read(climate_id),
            combined=entry is None or entry.platform == "esphome",
        ):
            await host.deliveries.async_send(climate_id, service, data)


class CloudDriver:
    """A cloud head (beta), or a generic one: its write record, batches and control.

    ``record`` is what MXZ last sent the head and what it is waiting to see
    (see cloud.CloudHead). ``wake`` re-arms the one wakeup the cloud heads share.
    """

    def __init__(
        self,
        zone: Zone,
        host: HeadsHost,
        grid: HeadGrid,
        record: CloudHead,
        issue_id: str,
        started: float,
        wake: Callable[[], None],
    ) -> None:
        self.climate_id = zone.climate_id
        self.zone = zone
        self.record = record
        self.issue_id = issue_id
        self._host = host
        self._grid = grid
        self._started = started
        self._wake = wake

    def setpoint(self, act: str, low: float, high: float) -> float:
        """The one setpoint a cloud head gets for heat or cool.

        The room's target as a single-setpoint head gets it (the high edge in
        cool, the low one in heat), pushed past by the room's drive offset,
        on the head's own setpoint grid (its target_temp_step in its own
        unit; with none, 1 °F or 0.5 °C), then inside the band the head
        accepts.
        """
        host = self._host
        offset = self.zone.drive_offset or 0.0
        value = drive_setpoint(
            act=act, target=high if act == MODE_COOL else low, offset=offset
        )
        grid = self._grid.read(self.climate_id)
        step, unit = grid.native_step, grid.native_unit
        if step > 0 and math.isfinite(step) and unit in (
            UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT,
        ):
            native = snap_to_step(convert(value, host.temp_unit, unit), step)
            # Two places: a whole °C step back in °F is 69.8, not 69.80000000000001.
            value = round(convert(native, unit, host.temp_unit), 2)
        else:
            value = snap_to_step(value, CLOUD_STEP_C if host.celsius else CLOUD_STEP_F)
        return clamp(value, grid.safe_band())

    def watch(self, wall: float, out: list[Effect]) -> bool:
        """Whether this head is out of MXZ's control now.

        Out once it has been unavailable (or has no state at all, timed from
        this coordinator's start) for CLOUD_STALE_SECONDS, or available but
        sending no report at all, not even an unchanged one, for
        CLOUD_SILENT_SECONDS (never, for a generic head). Each edge is logged
        once, and a repair issue stands while it lasts: its creation or
        deletion goes to ``out``.
        """
        cloud = self.record
        zone = self.zone
        since, limit = self.seen()
        out_of_control = limit is not None and wall - since >= limit
        if out_of_control != cloud.unresponsive:
            cloud.unresponsive = out_of_control
            kind = "generic" if cloud.generic else "cloud"
            if out_of_control:
                minutes = (limit or 0) // 60
                _LOGGER.warning(
                    "MXZ: %s head %s (%s) has been unavailable or silent for "
                    "%d minutes; MXZ stops writing it and its room's call no "
                    "longer counts (unless its Safety priority sensor is on)",
                    kind, zone.climate_id, zone.name, minutes,
                )
                out.append(partial(
                    ir.async_create_issue, self._host.hass, DOMAIN, self.issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=f"{kind}_head_unresponsive",
                    translation_placeholders={
                        "room": zone.name,
                        "head": zone.climate_id,
                        "minutes": str(minutes),
                    },
                ))
            else:
                _LOGGER.info(
                    "MXZ: %s head %s (%s) is reporting again; MXZ controls it again",
                    kind, zone.climate_id, zone.name,
                )
                out.append(partial(ir.async_delete_issue, self._host.hass, DOMAIN, self.issue_id))
        return out_of_control

    def seen(self) -> tuple[float, int | None]:
        """When this head last showed it is there (wall clock), and the limit.

        Its last report, changed or not, while available (CLOUD_SILENT_SECONDS;
        None for a generic head, which silence never puts out); the moment it
        went unavailable while it is, or this coordinator's start if it has no
        state (CLOUD_STALE_SECONDS).
        """
        head = self._host.hass.states.get(self.climate_id)
        if head is None:
            return self._started, CLOUD_STALE_SECONDS
        if head.state == STATE_UNAVAILABLE:
            return head.last_changed.timestamp(), CLOUD_STALE_SECONDS
        reported = getattr(head, "last_reported", None) or head.last_updated
        if self.record.generic:
            return reported.timestamp(), None
        return reported.timestamp(), CLOUD_SILENT_SECONDS

    def stale(
        self, *, mode: str | None = None, fan: str | None = None,
        setpoint: float | None = None,
    ) -> bool:
        """Whether this report of the head can be an older state, not a person.

        A setpoint compares on the head's grid, as its writes do.
        """
        return self.record.stale(
            self._host.clock(), mode=mode, fan=fan, setpoint=setpoint,
            same=partial(self._grid.already_at, self.climate_id),
        )

    def attributes(self) -> dict[str, Any]:
        """The room row's head type, write state and settings."""
        cloud = self.record
        zone = self.zone
        clock = self._host.clock()
        left = cloud.window_until - clock
        status = cloud.status(clock)
        return {
            "head_type": zone.head_type,
            # pending / confirmed / unconfirmed / not_controllable
            "cloud_status": status,
            "cloud_pending_until": (
                dt_util.utc_from_timestamp(round(dt_util.utcnow().timestamp() + left)).isoformat()
                if status == CLOUD_PENDING else None
            ),
            "cloud_confirmed_at": cloud.confirmed_at,
            "drive_offset": zone.drive_offset,
            "write_interval": zone.write_interval,
            "confirm_wait": zone.confirm_wait,
        }

    async def async_deliver(
        self, delivery: Delivery, act: str,
        low: float, high: float, delta: float, engage: str | None,
    ) -> None:
        """One cloud head's write batch, or nothing.

        Nothing while the head is out of control or not reporting a mode,
        nothing in the confirm wait a new coordinator opens, and nothing while
        its last batch carried this same mode and setpoint and its confirm
        wait is still running, except a fan when that batch was heat or cool
        and sent no fan. Otherwise the batch holds only
        what the head doesn't already show: the mode on its own call first
        (comfort_HA drops one inside set_temperature), then one setpoint,
        then the fan (before the mode when handing the fan back to auto on
        the way to off, as a local head gets it). A batch that has to wait
        out the write interval goes out later as whatever the plan wants
        then. A head showing everything wanted, once its window is over, is
        confirmed.
        """
        host = self._host
        climate_id = self.climate_id
        cloud = self.record
        clock = host.clock()
        state = host.hass.states.get(climate_id)
        if cloud.unresponsive or state is None or state.state in UNAVAILABLE_STATES:
            return
        steps = _steps(host, act, engage, state)
        host.note_head_act(climate_id, steps, act, clock)
        setpoint = (
            self.setpoint(act, low, high)
            if act in (MODE_COOL, MODE_HEAT) else None
        )
        waiting = cloud.pending(clock) and (
            cloud.startup
            or cloud.intent is not None and cloud.intent[:2] == (act, setpoint)
        )
        if waiting and (
            cloud.startup or cast("tuple[Any, ...]", cloud.intent)[2] is not None
            or act not in (MODE_COOL, MODE_HEAT)
        ):
            # Sent, and waiting for the head to report it; or just started,
            # when MXZ can't know what it sent before (a batch a moment
            # before a reload is still pending at the head).
            host.floor.accepted(climate_id, delivery)
            return
        # Waiting on a heat or cool batch that sent no fan: the mode and
        # setpoint aren't sent again, but the fan is still decided, so a
        # speed someone picks is read and a boost step that comes due goes
        # out (both limits still apply).
        fan = None
        if steps.fan(inhibited=host.inhibited) is not None:
            fan = host.fan.cloud_token(
                climate_id, cloud, state, MODE_FAN_ONLY if steps.handback else act, delta, clock,
            )
        cur_mode = state.state
        calls: list[tuple[str, dict[str, Any]]] = []
        if not waiting and cur_mode != act:
            calls.append(("set_hvac_mode", {"hvac_mode": act}))
        if not waiting and setpoint is not None and (
            cur_mode != act or not self._grid.already_at(
                climate_id, as_float(state.attributes.get("temperature")), setpoint
            )
        ):
            calls.append(("set_temperature", {"temperature": setpoint}))
        if fan is not None:
            # Before the off of a hand-back to auto, after everything else.
            calls.insert(0 if act == MODE_OFF else len(calls), ("set_fan_mode", {"fan_mode": fan}))
        if not calls:
            host.floor.accepted(climate_id, delivery)
            if not cloud.pending(clock) and not cloud.confirmed:
                cloud.confirmed = True
                cloud.confirmed_at = dt_util.utcnow().isoformat()
                host.publish_delivery()
            return
        if (wait := cloud.wait_for_slot(clock)) > 0:
            cloud.release_at = clock + wait
            _LOGGER.debug(
                "MXZ: cloud head %s waits %.0f s for its write interval", climate_id, wait
            )
            return
        shown_fan = state.attributes.get("fan_mode")
        cloud.sent(
            clock, (act, setpoint, fan), cur_mode,
            shown_fan if isinstance(shown_fan, str) else None,
            as_float(state.attributes.get("temperature")),
        )
        if fan is not None:
            if act in (MODE_COOL, MODE_HEAT):
                cloud.fan_at = clock
            host.fan.sent(climate_id, fan)
        _LOGGER.debug("MXZ: cloud head %s batch %s", climate_id, calls)
        host.publish_delivery()
        try:
            for service, payload in calls:
                if not host.deliveries.owned(delivery):
                    return
                await host.deliveries.async_send(climate_id, service, payload)
        except HomeAssistantError:
            now = host.clock()
            cloud.refused(now)
            # Nothing waits on a refused batch, so nothing else may wake the
            # plan for the next: wake it when the write interval lets that one
            # out, or now if the refusal itself took longer than the interval.
            if cloud.wait_for_slot(now) > 0:
                self._wake()
            elif not host.retired:
                host.hass.async_create_task(host.async_request_refresh())
            raise
        host.floor.accepted(climate_id, delivery)


class HeadDrivers:
    """Each room's head driver, and the upkeep the cloud heads share.

    Built with the coordinator: it settles each room's head type, and a cloud
    or generic room's settings, before anything reads them. The coordinator
    calls it from the queue (``async_deliver``), the compute (``watch``,
    ``attributes``, ``cloud``), a head's report (``stale_report``, ``cloud``),
    the end of an update (``sync_timer``), its setup (``track_reports``) and
    its teardown (``retire``).
    """

    def __init__(
        self, host: HeadsHost, entry_id: str, zones: list[Zone], started: float,
    ) -> None:
        """``started`` is the coordinator's start, on the wall clock.

        Each cloud head opens with one confirm window: a poll from before this
        start is not a person's change. One wakeup serves them all (see
        ``sync_timer``).
        """
        self._host = host
        self.grid = HeadGrid(host)
        self._drivers: dict[str, HeadDriver] = {}
        # Cloud heads (beta), by head. A local head is never in here.
        self._clouds: dict[str, CloudDriver] = {}
        clock = host.clock()
        for zone in zones:
            self._resolve(zone, entry_id, started, clock)

    def _resolve(self, zone: Zone, entry_id: str, started: float, clock: float) -> None:
        """Settle one room's head type, its settings for a cloud head, and its driver.

        Only a stored cloud or generic opts in. An ESPHome head is the CN105
        path and is always local, whatever is stored. A generic head (beta)
        is a cloud head here in every way but two: its write interval and
        confirm wait default to 30 s, and silence never puts it out of
        control (see CloudDriver.seen). A cloud room gets no vanes (so no
        vane kick), no airflow sensor and no dual setpoint, and a hands-off
        vote by mode: a cloud integration has no vane selects MXZ can kick
        safely, no stage sensor, one setpoint here, and an hvac_action that
        is missing or minutes late. Its numbers are clamped into the room
        page's ranges, so a hand-edited value can't make it write faster.
        """
        host = self._host
        self._drivers[zone.climate_id] = LocalDriver(zone.climate_id, host, self.grid)
        if zone.head_type not in OFFSET_HEAD_TYPES:
            return
        generic = zone.head_type == HEAD_TYPE_GENERIC
        entry = er.async_get(host.hass).async_get(zone.climate_id)
        if entry is not None and entry.platform == "esphome":
            if generic:
                _LOGGER.warning(
                    "MXZ: %s's head %s is on ESPHome, so it stays local; "
                    "generic (beta) is for heads that are not on ESPHome",
                    zone.name, zone.climate_id,
                )
            else:
                _LOGGER.warning(
                    "MXZ: %s's head %s is on ESPHome, so it stays local; cloud "
                    "(beta) is for heads behind a cloud integration",
                    zone.name, zone.climate_id,
                )
            zone.head_type = HEAD_TYPE_LOCAL
            return
        zone.vane_vertical_id = zone.vane_horizontal_id = None
        zone.stage_sensor_id = None
        zone.dual = False
        zone.vote_basis = VOTE_BASIS_MODE
        offset, most = (
            (DEFAULT_DRIVE_OFFSET_C, DRIVE_OFFSET_MAX_C) if host.celsius
            else (DEFAULT_DRIVE_OFFSET_F, DRIVE_OFFSET_MAX_F)
        )
        # A set offset follows a °F/°C switch: converted from the unit it was
        # set in, then held inside this unit's range.
        zone.drive_offset = (
            offset if zone.drive_offset is None
            else min(max(
                offset_in_unit(zone.drive_offset, zone.drive_offset_unit, host.celsius), 0.0,
            ), most)
        )
        zone.write_interval = bounded(
            zone.write_interval,
            GENERIC_WRITE_INTERVAL if generic else DEFAULT_WRITE_INTERVAL,
            WRITE_INTERVAL_BOUNDS,
        )
        zone.confirm_wait = bounded(
            zone.confirm_wait,
            GENERIC_CONFIRM_WAIT if generic else DEFAULT_CONFIRM_WAIT,
            CONFIRM_WAIT_BOUNDS,
        )
        record = CloudHead(
            write_interval=zone.write_interval,
            confirm_wait=zone.confirm_wait,
            generic=generic,
            window_until=clock + zone.confirm_wait,
            # A start counts as a fan step: MXZ can't know when it last
            # stepped this head, so its first boost step waits the full limit.
            fan_at=clock,
        )
        driver = CloudDriver(
            zone, host, self.grid, record,
            f"cloud_head_unresponsive_{entry_id}_{zone.climate_id}",
            started, self.sync_timer,
        )
        self._clouds[zone.climate_id] = driver
        self._drivers[zone.climate_id] = driver

    async def async_deliver(
        self, climate_id: str, delivery: Delivery, act: str,
        low: float, high: float, delta: float, engage: str | None,
    ) -> None:
        """One delivery's steps at its head, once it holds the head's turn and is still owned."""
        await self._drivers[climate_id].async_deliver(delivery, act, low, high, delta, engage)

    def cloud(self, climate_id: str) -> CloudHead | None:
        """This head's cloud write record; None for a local head."""
        driver = self._clouds.get(climate_id)
        return driver.record if driver is not None else None

    def watch(self, zone: Zone, wall: float, out: list[Effect]) -> bool:
        """Whether a cloud room's head is out of MXZ's control now (never a local one).

        A repair issue it opens or closes goes to ``out``.
        """
        driver = self._clouds.get(zone.climate_id)
        return driver is not None and driver.watch(wall, out)

    def stale_report(
        self, climate_id: str, *, mode: str | None = None, fan: str | None = None,
        setpoint: float | None = None,
    ) -> bool:
        """Whether this report of a cloud head can be an older state, not a person.

        Always False for a local head, so no local check changes.
        """
        driver = self._clouds.get(climate_id)
        return driver is not None and driver.stale(mode=mode, fan=fan, setpoint=setpoint)

    def attributes(self, zone: Zone) -> dict[str, Any]:
        """A cloud room's head type, write state and settings; nothing for a local one.

        A local row stays exactly what it was before cloud heads existed (the
        3.9.0 pins hold it), so a row with no head_type is a local head.
        """
        driver = self._clouds.get(zone.climate_id)
        return driver.attributes() if driver is not None else {}

    def cloud_waits(self, clock: float) -> list[CloudWaits]:
        """Each cloud head's deadlines on MXZ's clock read as ``clock`` (``timing.CloudWaits``)."""
        return [
            CloudWaits(
                pending=driver.record.pending(clock),
                window_until=driver.record.window_until,
                release_at=driver.record.release_at,
                unresponsive=driver.record.unresponsive,
                seen=None if driver.record.unresponsive else driver.seen(),
            )
            for driver in self._clouds.values()
        ]

    def sync_timer(self) -> None:
        """Keep one wakeup armed for the nearest cloud head deadline (``timing.cloud_arm``).

        A cloud head that reports nothing new emits nothing, so without this each deadline
        would wait for an unrelated update. A compute arms it with every other wakeup; a
        refused batch arms it here.
        """
        host = self._host
        clock = host.clock()
        host.perform_arm(cloud_arm(
            host.retired, self.cloud_waits(clock), clock, dt_util.utcnow().timestamp(),
        ))

    def track_reports(self) -> Callable[[], None] | None:
        """Listen for every report from a cloud head, changed or not; None with no cloud head.

        An unchanged report fires no state change, and it is the one a quiet
        cloud head that came back sends.
        """
        if not self._clouds:
            return None
        return async_track_state_report_event(
            self._host.hass, list(self._clouds), self._on_report
        )

    @callback
    def _on_report(self, event: Event[EventStateReportedData]) -> None:
        """A cloud head reported, changed or not: one out of control is back."""
        cloud = self.cloud(event.data["entity_id"])
        if cloud is not None and cloud.unresponsive:
            self._host.hass.async_create_task(self._host.async_request_refresh())

    def retire(self) -> None:
        """Clear each cloud head's repair issue (the clock cancels the wakeup).

        A stale head's issue is raised again by the next setup, if it is still
        stale then; a removed entry leaves none behind.
        """
        for driver in self._clouds.values():
            ir.async_delete_issue(self._host.hass, DOMAIN, driver.issue_id)
