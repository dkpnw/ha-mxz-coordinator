"""The simulator's MXZ row: MXZ Coordinator's own decisions over the simulated heads.

Every decision, every wakeup and every order of steps here but one comes from a product function
in ``custom_components/mxz_coordinator`` that imports no Home Assistant (``tools/import_bounds.py``
``STDLIB_ONLY``): ``planner``, ``timing``, ``sim_timing``, ``grid``, ``head_writes``,
``fan_latch``, ``resume`` and ``logic``. This module maps the page's controls to those functions'
inputs and performs the steps they return, each with the call its docstring names. The one order
of its own is where an adopt hold's end goes on after its write started a compute
(``MxzRow._go_on``).
It follows the per-event list in planner1118d's WRITE-SUMMARY.md, with the hold-end wakeup armed
at a start (``MXZCoordinator.async_setup``). Where a step belongs to a feature the simulated house
does not have (a door/window pause, Dry, a floor, a hands-off room, a head going out of sight),
it raises ``NotModeled`` instead of doing something else.

The heads are the verification instance's (``verification/instance/invented_house``):
a °F range head on the ``invented_house`` platform, which takes a mode change and its setpoints
as two calls. ``InventedHeads.shown`` renders a head the way Home Assistant shows that entity.

What this does not model, as planner1118d's review 3 lists it: deliveries run head by head in
the order they were queued, inside their compute (Home Assistant interleaves heads at each
await); a delivery a newer plan supersedes; the shared-mode select. Timing follows
``sim_timing``, a model of Home Assistant 2025.11 and later.

Nothing here is a measurement of real equipment.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Hashable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from invented_house import devices as dv
from invented_house import model

from custom_components.mxz_coordinator import fan_latch as fl
from custom_components.mxz_coordinator import grid as gr
from custom_components.mxz_coordinator import head_writes as hw
from custom_components.mxz_coordinator import planner as pl
from custom_components.mxz_coordinator import resume as rs
from custom_components.mxz_coordinator import sim_timing as st
from custom_components.mxz_coordinator import timing as tm
from custom_components.mxz_coordinator.const import (
    ACTION_VOTE_HOLD,
    ACTION_VOTE_SETTLE,
    DEFAULT_ACCEPT_HEAD_CHANGES,
    DEFAULT_COIL_DRY_MINUTES,
    DEFAULT_COMFORT_STALE_HOURS,
    DEFAULT_DEMAND_THRESHOLD,
    DEFAULT_ENGAGE_DEADBAND,
    DEFAULT_FAN_BOOST_ENABLE,
    DEFAULT_FAN_BOOST_MAX,
    DEFAULT_IDLE_ACTION,
    DEFAULT_LOST_VOTE_TTL,
    DEFAULT_MODE_HYSTERESIS,
    FAN_LADDER,
    HEAD_WRITES_REMEMBERED,
    KEY_COOL_LOCKOUT,
    KEY_COORDINATOR_ENABLE,
    KEY_ECO_IDLE,
    KEY_HEAT_LOCKOUT,
    MODE_OFF,
    unit_profile,
    zone_slug,
)
from custom_components.mxz_coordinator.logic import (
    HeadSight,
    HeadWriteMemory,
    comfort_priority,
    sensor_health,
)
from custom_components.mxz_coordinator.plan import Plan, RoomPlan

FAHRENHEIT = "°F"
PLATFORM = "invented_house"
# Home Assistant's climate features of a head: a range, a fan, on and off.
RANGE_HEAD_FEATURES = 2 | 8 | 128 | 256
COMFORT_DOMAIN = "input_boolean"


class NotModeled(RuntimeError):
    """A product step for a feature the simulated house does not have."""


class OutOfRange(ValueError):
    """A value Home Assistant's number entity refuses (``out_of_range``)."""


def climate_id(key: str) -> str:
    return f"climate.{key}_head"


def sensor_id(key: str) -> str:
    return f"sensor.{key}_temperature"


def comfort_id(key: str) -> str:
    return f"{COMFORT_DOMAIN}.{key}_comfort"


@dataclass(frozen=True)
class Settings:
    """The entry's settings: MXZ's defaults for a °F install, as a new entry has them."""

    hold: float = float(DEFAULT_MODE_HYSTERESIS)
    idle_action: str = DEFAULT_IDLE_ACTION
    coil_dry_seconds: float = 60.0 * DEFAULT_COIL_DRY_MINUTES
    fan_boost: bool = DEFAULT_FAN_BOOST_ENABLE
    accept_head_changes: bool = DEFAULT_ACCEPT_HEAD_CHANGES
    demand_threshold: float = DEFAULT_DEMAND_THRESHOLD
    drift: float = DEFAULT_ENGAGE_DEADBAND
    lost_vote_ttl: float = 60.0 * DEFAULT_LOST_VOTE_TTL
    comfort_stale_after: float | None = 3600.0 * DEFAULT_COMFORT_STALE_HOURS

    def __post_init__(self) -> None:
        profile = unit_profile(False)
        defaults = profile["defaults"]
        object.__setattr__(self, "_profile", profile)
        object.__setattr__(self, "clamp_min", float(defaults["clamp_min"]))
        object.__setattr__(self, "clamp_max", float(defaults["clamp_max"]))

    @property
    def profile(self) -> dict[str, Any]:
        return self._profile  # type: ignore[attr-defined,no-any-return]

    @property
    def limits(self) -> pl.CallLimits:
        defaults = self.profile["defaults"]
        return pl.CallLimits(
            eco=False,
            eco_cool_max=defaults["eco_cool_max"],
            eco_heat_min=defaults["eco_heat_min"],
            heat_lockout=False,
            heat_lockout_floor=defaults["heat_lockout_floor"],
            cool_lockout=False,
            cool_lockout_ceiling=defaults["cool_lockout_ceiling"],
        )

    @property
    def setpoints(self) -> pl.SetpointSettings:
        profile = self.profile
        return pl.SetpointSettings(
            clamp_min=self.clamp_min,  # type: ignore[attr-defined]
            clamp_max=self.clamp_max,  # type: ignore[attr-defined]
            band=profile["setpoint_band"],
            step=profile["target_step"],
            eco_cool=profile["eco_cool"],
            eco_heat=profile["eco_heat"],
        )

    @property
    def ceiling(self) -> int:
        return FAN_LADDER.index(DEFAULT_FAN_BOOST_MAX)


class InventedHeads:
    """The heads and rooms (``invented_house``), and what each head shows.

    ``devices`` is the shared physical state: each head's mode, setpoints and fan, and each
    room's temperature. ``modelled`` is whether the demo model runs (then a head shows what it
    is doing, as ``invented_house.climate.InventedHead`` does).
    """

    def __init__(self, house: Mapping[str, Any], devices: dv.Devices, *, modelled: bool) -> None:
        self.house = house
        self.devices = devices
        self.modelled = modelled
        self.keys: tuple[str, ...] = tuple(room["key"] for room in house["rooms"])
        spec = house["head"]
        self._capabilities = {
            "hvac_modes": list(spec["hvac_modes"]),
            "min_temp": round(spec["min_temp"]),
            "max_temp": round(spec["max_temp"]),
            "fan_modes": list(spec["fan_modes"]),
        }
        self.native = (float(spec["min_temp"]), float(spec["max_temp"]), FAHRENHEIT)

    def changed(self) -> None:
        """A head or room changed: with the model on, rule 1 picks the outdoor direction again
        (``invented_house.Bench.changed``)."""
        if self.modelled:
            self.devices.outdoor = model.direction(self.devices, self.devices.outdoor)

    def doing(self, key: str) -> str:
        return model.action(self.devices.heads[key], self.devices.rooms[key], self.devices.outdoor)

    def shown(self, key: str) -> tuple[str, dict[str, Any]]:
        """The head's state and attributes as Home Assistant shows it.

        Temperatures show at whole degrees (a °F system's precision), as Home Assistant's
        climate entity renders them.
        """
        head = self.devices.heads[key]
        state = head.hvac_mode if head.available else "unavailable"
        if not head.available:
            return state, {}
        attributes: dict[str, Any] = {
            **self._capabilities,
            "current_temperature": None,
            "target_temp_high": _whole(head.target_temp_high),
            "target_temp_low": _whole(head.target_temp_low),
            "fan_mode": head.fan_mode,
        }
        if self.modelled:
            attributes["hvac_action"] = _HVAC_ACTION[self.doing(key)]
            attributes["standby"] = self.doing(key) == "standby"
        attributes["supported_features"] = RANGE_HEAD_FEATURES
        return state, attributes

    def reading(self, key: str) -> float:
        """What the room's sensor reports (``invented_house.devices.reading``)."""
        return dv.reading(self.devices.rooms[key].temperature)

    def call(self, key: str, service: str, data: Mapping[str, Any]) -> None:
        """A climate service call to the head, as ``InventedHead`` takes it."""
        head = self.devices.heads[key]
        if service == "set_hvac_mode":
            head.hvac_mode = str(data["hvac_mode"])
        elif service == "set_fan_mode":
            head.fan_mode = str(data["fan_mode"])
        elif service == "set_temperature":
            if (mode := data.get("hvac_mode")) is not None:
                head.hvac_mode = str(mode)
            if (low := data.get("target_temp_low")) is not None:
                head.target_temp_low = low
            if (high := data.get("target_temp_high")) is not None:
                head.target_temp_high = high
        else:
            raise NotModeled(f"climate.{service}")
        self.changed()


_HVAC_ACTION = {
    "off": "off", "fan": "fan", "standby": "idle", "heating": "heating", "cooling": "cooling",
    "idle": "idle",
}


def _whole(value: float | None) -> int | None:
    return None if value is None else round(value)


@dataclass
class Room:
    """One room as MXZ keeps it between calls."""

    key: str
    name: str
    slug: str
    target: float
    enable: bool
    comfort: bool = False
    comfort_at: float = 0.0
    latch: fl.FanLatch = field(default_factory=lambda: fl.FanLatch(echo=fl.FanEcho()))
    memories: dict[str, HeadWriteMemory] = field(default_factory=dict)
    last_run: tuple[str, float] | None = None
    pending: set[str] = field(default_factory=set)
    queued: pl.HeadCommand | None = None
    active: str | None = None
    disowned: bool = False
    sight: HeadSight | None = None
    temp: float | None = None
    # What the head and the sensor showed at their last report.
    shown: tuple[str, dict[str, Any]] | None = None

    @property
    def climate_id(self) -> str:
        return climate_id(self.key)


@dataclass
class Log:
    """What the row did, for the page and the tests: each call to a head and each compute."""

    calls: list[tuple[float, str, str, dict[str, Any]]] = field(default_factory=list)
    computes: list[tuple[float, str]] = field(default_factory=list)


class MxzRow:
    """MXZ Coordinator over the simulated heads, driven by the page."""

    def __init__(
        self,
        heads: InventedHeads,
        *,
        settings: Settings | None = None,
        start: float = 0.0,
        wall: float = 0.0,
    ) -> None:
        self.heads = heads
        self.settings = settings or Settings()
        self.now = start
        self.wall0 = wall - start
        self.created = wall
        self.log = Log()
        self.timers: st.Timers | None = None
        self.running = False
        self.rooms: list[Room] = []
        self._computing = False
        self._dispatch: list[tm.Refresh] | None = None
        self._holds: list[tuple[Room, deque[object]]] = []

    # -- the clock -------------------------------------------------------------------------
    @property
    def wall(self) -> float:
        return self.wall0 + self.now

    def due(self) -> float:
        """When MXZ's next timer runs (``sim_timing.due``)."""
        return st.due(self._timers())

    def _timers(self) -> st.Timers:
        if self.timers is None:
            raise RuntimeError("MXZ is not running")
        return self.timers

    def waiting(self, name: str, key: Hashable = None) -> bool:
        """Whether this wakeup is armed now, the ones a running compute armed included."""
        if self._computing:
            return (name, key) in self._waits
        return self.timers is not None and (name, key) in self.timers.waits

    # -- event 1: a start ------------------------------------------------------------------
    def start(
        self, saved: Mapping[str, Any] | None, *, names: Mapping[str, str],
        comfort: Mapping[str, tuple[bool, float]] | None = None,
    ) -> None:
        """MXZ starts with Home Assistant, from the room state store's record ``saved``.

        ``names`` gives each room's name by key, in house order. ``comfort`` gives each room's
        Comfort priority entity as Home Assistant shows it at the start: whether it is on, and
        the wall time it last changed. That entity is not MXZ's, so a restart does not reset it;
        a room missing from ``comfort`` has it off since the clock's start. Follows
        ``MXZCoordinator.__init__``, ``_resume``, the shared-mode select's restore and
        ``async_setup``: the sights, the mode record and its stamp, every stored value through
        ``resume``, the startup seeds, the fan records, the startup quiet, the hold-end wakeup,
        then the first compute.
        """
        s = self.settings
        stored = dict(saved or {})
        state = rs.SavedState(
            saved_at=stored.get("saved_at"),
            unit=stored.get("unit"),
            sections={
                key: value for key, value in stored.items()
                if key not in ("saved_at", "unit") and isinstance(value, dict)
            },
        )
        self.rooms = []
        for i, key in enumerate(self.heads.keys):
            room = Room(
                key=key, name=names[key], slug=zone_slug(i),
                target=float(s.profile["target_default"]), enable=False,
            )
            room.sight = HeadSight(self.now, self.wall, ACTION_VOTE_SETTLE)
            self.rooms.append(room)
        record, _unreadable = rs.mode_record(state.section(rs.SECTION_MODE), self.wall)
        self.stamp_at = pl.mode_change_stamp(record.changed_at, self.now, self.wall)
        self.stamp_wall = record.changed_at
        self.mode_data = record.stored()
        self.starting = tm.startup_quiet(running=False, startup_ran=False)
        self.latch: dict[str, str] = {}
        self.startup: dict[str, str | None] = {}
        self.coasted = False
        self.plan: Plan | None = None
        self._applying: Plan | None = None
        self._computing = False
        self._waits: set[tuple[str, Hashable]] = set()
        self._armed: list[tm.Arm | tm.CancelAll] = []
        self._asked: list[tm.Refresh] = []
        self._dispatch: list[tm.Refresh] | None = None
        self._holds: list[tuple[Room, deque[object]]] = []
        switches = {KEY_COORDINATOR_ENABLE: False, KEY_ECO_IDLE: False, KEY_HEAT_LOCKOUT: False,
                    KEY_COOL_LOCKOUT: False}
        resumed = rs.resume(
            state,
            [rs.RoomSlot(room.climate_id, room.slug, self.grid().target_bounds()) for room in self.rooms],
            unit=FAHRENHEIT, step=s.profile["target_step"], celsius=False,
            engage_bounds=s.profile["engage_bounds"],
        )
        switches.update(resumed.switches)
        self.coordinator = switches[KEY_COORDINATOR_ENABLE]
        self.restored_coordinator_on = self.coordinator
        if switches[KEY_ECO_IDLE] or switches[KEY_HEAT_LOCKOUT] or switches[KEY_COOL_LOCKOUT]:
            raise NotModeled("eco and the season lockouts")
        for room, saved_room in zip(self.rooms, resumed.rooms, strict=True):
            if saved_room.target is not None:
                room.target = saved_room.target
            if saved_room.target_range is not None or saved_room.drift is not None:
                raise NotModeled("a dual setpoint or a room's own drift")
            if saved_room.enable is not None:
                seed = rs.resumed_enable(
                    room.slug, saved_room.enable, False, saved_room.enable_record,
                    self.latch, self.startup,
                )
                room.enable = saved_room.enable
                self.latch, self.startup = seed.latch, seed.startup
            if saved_room.fan is not None:
                fan = saved_room.fan
                room.latch = fl.restored(
                    room.latch, fan.held, last=fan.last, prior=fan.prior, older=fan.older,
                    on_pending=fan.on_pending, ceiling=s.ceiling,
                )
            room.memories.update(saved_room.head_writes)
            room.shown = self.heads.shown(room.key)
            room.temp = self.heads.reading(room.key)
            room.comfort, room.comfort_at = (comfort or {}).get(room.key, (False, self.wall0))
        self.mode, self.by_votes = rs.resumed_mode(record, None, None)
        self.running = True
        self.timers, did = st.start(self.now)
        self._step(self._hold_end_arm())
        self._settle(did)

    def _hold_end_arm(self) -> tm.Arm:
        """The hold-end wakeup for the hold left now (``MXZCoordinator._arm_hold_end_timer``)."""
        elapsed = None if self.stamp_at is None else self.now - self.stamp_at
        remaining = 0.0 if elapsed is None else max(0.0, self.settings.hold - elapsed)
        return tm.hold_end_arm(remaining, retired=False)

    # -- the record a stop leaves, and a restart -------------------------------------------
    def saved(self) -> dict[str, Any]:
        """What the room state store holds at a stop (``MXZCoordinator``'s ``_*_record``)."""
        return {
            "saved_at": self.wall,
            "unit": FAHRENHEIT,
            rs.SECTION_ROOMS: {
                room.climate_id: {
                    "target": room.target, "target_low": None, "target_high": None,
                    "drift": None, "enable": room.enable, "hands_off_released": False,
                }
                for room in self.rooms
            },
            rs.SECTION_SWITCHES: {
                KEY_COORDINATOR_ENABLE: self.coordinator, KEY_ECO_IDLE: False,
                KEY_HEAT_LOCKOUT: False, KEY_COOL_LOCKOUT: False,
            },
            rs.SECTION_FAN: {
                room.climate_id: fl.fan_record(room.latch, room.shown[1].get("fan_mode") if room.shown else None).as_dict()
                for room in self.rooms
            },
            **({rs.SECTION_MODE: self.mode_data} if self.mode_data is not None else {}),
            rs.SECTION_HEAD_WRITES: {
                room.climate_id: {name: memory.record() for name, memory in room.memories.items()}
                for room in self.rooms if room.memories
            },
        }

    def restart(self) -> dict[str, Any]:
        """Home Assistant stops and starts: the store is written, MXZ's memory goes, and it
        starts from the store. The heads keep what they show. Returns the record."""
        record = self.saved()
        names = {room.key: room.name for room in self.rooms}
        comfort = {room.key: (room.comfort, room.comfort_at) for room in self.rooms}
        self.timers = None
        self.running = False
        self.start(record, names=names, comfort=comfort)
        return record

    # -- the plan's act for a head ---------------------------------------------------------
    def _idle(self, room: Room) -> str:
        park = pl.idle_for(
            room.climate_id, self.settings.idle_action, room.last_run, self._mode(room),
            self.settings.coil_dry_seconds, self.now, dry_waiting=self.waiting("dry", room.climate_id),
        )
        for step in park.steps:
            self._perform(room, step)
        return park.idle

    def _planned(self, room: Room) -> str | None:
        plan = self._applying if self._applying is not None else self.plan
        row = None if plan is None else plan.room(room.slug)
        return pl.planned_act(
            active=room.active, paused=False, state=None if plan is None else plan.state,
            engage=None if row is None else row.engage, enabled=room.enable, eco=False,
            idle=lambda: self._idle(room),
        )

    def _mode(self, room: Room) -> str | None:
        return None if room.shown is None else room.shown[0]

    def _attributes(self, room: Room) -> dict[str, Any]:
        return {} if room.shown is None else room.shown[1]

    def grid(self) -> gr.Grid:
        """A head's grid (``HeadGrid.read``): its native band, no setpoint step."""
        s = self.settings
        return gr.Grid(
            system_unit=FAHRENHEIT, target_step=s.profile["target_step"],
            clamp_min=s.clamp_min, clamp_max=s.clamp_max,  # type: ignore[attr-defined]
            native=self.heads.native, native_step=0.0, native_unit=None,
        )

    def _gates(self) -> pl.AdoptGates:
        return pl.AdoptGates(
            accept=self.settings.accept_head_changes, retired=False, coordinator=self.coordinator,
            inhibited=False, eco=False, starting=self.starting, hands_off=False, floor=False,
            paused=False,
        )

    def _adoption(self, room: Room, moved: set[str]) -> pl.Adoption | None:
        return pl.adopt_head_change(pl.head_change(
            moved, state=self._mode(room), attributes=self._attributes(room), cloud=False,
            gates=self._gates(), enabled=room.enable, heat_target=room.target,
            cool_target=room.target, driven=False, step=self.settings.profile["target_step"],
            memories=room.memories, grid=self.grid(), mode_stale=lambda _m: False,
            setpoint_stale=lambda _s: False, planned=lambda: self._planned(room),
        ))

    def _fan_limits(self) -> fl.FanLimits:
        s = self.settings
        return fl.FanLimits(
            boost=s.fan_boost, ceiling=s.ceiling, up_at=s.profile["fan_up_at"],
            down_at=s.profile["fan_down_at"], eco_idle=False, idle_action=s.idle_action,
            resumed=self.restored_coordinator_on, age=timedelta(seconds=self.wall - self.created),
        )

    # -- performing a step -----------------------------------------------------------------
    def _arm(self, arm: tm.Arm | tm.CancelAll) -> None:
        if self._computing:
            self._armed.append(arm)
            if isinstance(arm, tm.CancelAll):
                self._waits = {wait for wait in self._waits if wait[0] != arm.name}
            elif arm.delay is None:
                self._waits.discard((arm.name, arm.key))
            else:
                self._waits.add((arm.name, arm.key))
        else:
            self._settle(self._step(arm))

    def _ask(self, refresh: tm.Refresh | None) -> None:
        if refresh is None:
            return
        if self._computing:
            self._asked.append(refresh)
        elif self._dispatch is not None:
            self._dispatch.append(refresh)
        else:
            self._settle(self._step(st.Ask(refresh)))

    def _perform(self, room: Room, step: object, old: tuple[str, dict[str, Any]] | None = None) -> None:
        """Do what one returned step says, with the call its docstring names."""
        match step:
            case pl.LastRun(run):
                room.last_run = run
            case tm.Arm() | tm.CancelAll():
                self._arm(step)
            case pl.Queue(command, _taken):
                room.queued = command
            case pl.FanShown(fan):
                room.latch.echo.caught_up(fan)
            case pl.WritesShown():
                assert old is not None
                mode, attributes = self._mode(room), self._attributes(room)
                hw.writes_caught_up(
                    room.memories, old_mode=old[0], old_attributes=old[1], mode=mode,
                    attributes=attributes, grid=self.grid(),
                )
            case pl.SeedCancelled():
                self.startup[room.slug] = ""
            case pl.HeadReport(pending, hold, disown, _replan):
                room.pending = set() if pending is None else set(pending)
                self._arm(hold)
                if disown:
                    room.queued = None
                    room.disowned = room.active is not None
            case tm.Refresh():
                self._ask(step)
            case fl.KeepLatch(latch):
                room.latch = latch
            case fl.WroteFan(token):
                room.latch.echo.wrote(token)
            case fl.SendFan(token):
                self._send(room, "set_fan_mode", {"fan_mode": token})
            case fl.EchoCaughtUp():
                pass  # the product publishes the Fan auto switch here
            case pl.Pressed(fields):
                hw.writes_pressed(
                    room.memories, fields, mode=self._mode(room), attributes=self._attributes(room),
                    grid=self.grid(),
                )
            case pl.SetTarget(number, value):
                if number != "target":
                    raise NotModeled(f"a write to {number}")
                self.set_target(room.key, value)
            case pl.SetEnable(on):
                self.set_enable(room.key, on)
            case pl.Latch(entry):
                if entry is None:
                    self.latch.pop(room.slug, None)
                else:
                    self.latch[room.slug] = entry
            case pl.SeedDropped():
                self.startup.pop(room.slug, None)
            case pl.ReleaseEnded():
                pass  # no room here is a released hands-off room
            case pl.OffHealRecheck():
                cancel = pl.off_heal_recheck(
                    room.climate_id, self.waiting("heal", (room.climate_id, "off")), self._mode(room),
                    enabled=room.enable, eco_idle=False, planned=lambda: self._planned(room),
                )
                if cancel is not None:
                    self._arm(cancel)
            case _:
                raise NotModeled(type(step).__name__)

    # -- a call to a head, and the heads' reports ------------------------------------------
    def _send(self, room: Room, service: str, data: Mapping[str, Any]) -> None:
        """One call to a head (``head_writes.issue_steps``): remember, then send."""
        counted: Mapping[str, Any] = {}
        for step in hw.issue_steps(data):
            match step:
                case hw.RememberWrites(payload):
                    counted = hw.remember_writes(
                        room.memories, payload, mode=self._mode(room),
                        attributes=self._attributes(room), grid=self.grid(),
                        size=HEAD_WRITES_REMEMBERED,
                    )
                case hw.SendCall():
                    self.log.calls.append((self.now, room.key, service, dict(data)))
                    self.heads.call(room.key, service, data)
                    self.world_changed()
        del counted  # a simulated head refuses no call (``writes_refused`` is never needed)

    def world_changed(self) -> None:
        """The house wrote its entities: each head, then its room's sensor, in house
        order (``invented_house.Bench.changed`` dispatches every room's entities with the model
        on). A head whose state or attributes changed reports (event 6); a sensor whose reading
        changed asks for a compute (event 3).

        Home Assistant runs each entity's state-change callbacks as it is written, and the
        compute a callback asks for in a task it creates, after the dispatch. So the reports
        and readings of one dispatch are all taken before its ask. A dispatch that asks more
        than once outside a compute raises ``NotModeled``: then the coordinator's reading
        handler drops a later ask once a compute has read the readings
        (``MXZCoordinator._on_input_change``), a rule no product function returns. The page's
        rooms report one at a time (``world.World``), so it never happens there.
        """
        outside = not self._computing and self._dispatch is None
        if outside:
            self._dispatch = []
        try:
            for room in self.rooms:
                shown = self.heads.shown(room.key)
                if shown != room.shown:
                    old, room.shown = room.shown, shown
                    self._report(room, old)
                reading = self.heads.reading(room.key)
                if reading != room.temp:
                    room.temp = reading
                    self._ask(tm.Refresh.REQUEST)
        finally:
            asks, self._dispatch = (self._dispatch, None) if outside else ([], self._dispatch)
        if len(asks) > 1:
            raise NotModeled(
                "several entities asking for a compute in one dispatch "
                "(MXZCoordinator._on_input_change drops a later reading's ask)"
            )
        for refresh in asks:
            self._ask(refresh)

    def _report(self, room: Room, old: tuple[str, dict[str, Any]] | None) -> None:
        """Event 6: a head reports a new state (``planner.report_steps``)."""
        old_mode, old_attributes = (None, {}) if old is None else old
        report = pl.ReportedHead(
            key=room.climate_id, mode=self._mode(room), attributes=self._attributes(room),
            old_mode=old_mode, old_attributes=old_attributes,
            accept=self.settings.accept_head_changes, cloud=False, sight=True,
            released_by_action=False, startup=room.slug in self.startup,
            owned_off=room.active is None or room.active == MODE_OFF, parking=False,
            kicking=False, floor_run=False, paused=False, stale=False, allow_dry=False,
            dry_allowed=False, enabled=room.enable, eco_idle=False, inhibited=False,
            unresponsive=False, pending=room.pending,
            holding=self.waiting("heal", (room.climate_id, "adopt")),
            hands_off_head=lambda: False, dry_reported=lambda: False,
            planned=lambda: self._planned(room), clock=lambda: self.now,
            adoption=lambda moved: self._adoption(room, moved),
        )
        for step in pl.report_steps(report):
            self._perform(room, step, (old_mode, old_attributes))

    # -- event 5: a delivery to a local head -----------------------------------------------
    def _deliver(self, room: Room) -> None:
        c, room.queued = room.queued, None
        assert c is not None
        room.active, room.disowned = c.mode, False
        s = self.settings
        steps = pl.delivery_steps(
            c.mode, c.fan, idle_action=s.idle_action, eco=False, inhibited=False,
            head_state=self._mode(room),
        )

        def boost(act: str) -> None:
            for step in fl.fan_boost(
                lambda: room.latch, self._attributes(room).get("fan_mode"), act, c.delta,
                boost=s.fan_boost, modes=self._attributes(room).get("fan_modes"),
                limits=self._fan_limits,
            ):
                self._perform(room, step)

        try:
            for name in steps.order:
                if room.disowned:
                    return
                if name == "note":
                    for step in steps.note(room.climate_id, c.mode, self.now):
                        self._perform(room, step)
                elif name == "handback":
                    boost("fan_only")  # the hand-back is the boost step for fan_only
                elif name == "setpoints":
                    for call in gr.local_calls(
                        c.mode, c.low, c.high, mode=self._mode(room),
                        attributes=self._attributes(room), grid=self.grid(), combined=False,
                    ):
                        self._send(room, call.service, call.data)
                        if room.disowned:
                            return
                elif (fan := steps.fan(inhibited=False)) == "drop_rung":
                    room.latch = fl.FanLatch(
                        echo=room.latch.echo, held=room.latch.held, restored=room.latch.restored,
                        on_pending=room.latch.on_pending, rung=None,
                    )
                elif fan == "boost":
                    boost(c.mode)
        finally:
            room.active = None

    # -- event 4: a compute ----------------------------------------------------------------
    def _compute(self) -> st.Computed:
        s = self.settings
        self._computing, self._armed, self._asked = True, [], []
        self._waits = set(self._timers().waits)
        inputs: list[pl.RoomInput] = []
        statuses: list[str] = []
        comforts: list[tuple[float, str]] = []
        for room in self.rooms:
            sight = room.sight
            assert sight is not None
            mode, attributes = self._mode(room), self._attributes(room)
            if mode is None or mode == "unavailable":
                raise NotModeled("a head out of sight")
            sight.observe(mode, attributes.get("hvac_action"), self.now)
            status = sight.status(self.now, s.lost_vote_ttl)
            comfort = comfort_priority(
                COMFORT_DOMAIN, "on" if room.comfort else "off",
                age=self.wall - room.comfort_at, stale_after=s.comfort_stale_after,
            )
            comforts.append(comfort)
            statuses.append(status)
            assert room.temp is not None
            inputs.append(pl.RoomInput(
                slug=room.slug, enabled=room.enable, temp=room.temp, sensor_ok=True,
                target=room.target, heat_target=room.target, cool_target=room.target,
                heat_drift=s.drift, cool_drift=s.drift, head=mode, paused=False, dry=False,
                floor=None, safety=False, vote=None, holds=pl.holds(status, False),
                boost=comfort[0],
            ))
        elapsed = None if self.stamp_at is None else self.now - self.stamp_at
        decision = pl.decide(
            inputs,
            pl.Shared(
                limits=s.limits, demand_threshold=s.demand_threshold, mode=self.mode,
                elapsed=elapsed, hold=s.hold, resting=None, by_votes=self.by_votes,
                resume=self.coordinator and self.restored_coordinator_on,
                accept_head_changes=s.accept_head_changes,
            ),
            self.latch, self.startup,
        )
        self.latch, self.startup, self.coasted = decision.latch, decision.startup, decision.coasted
        plan = Plan(
            rooms=[self._row(room, inputs[i], decision.rooms[i], statuses[i], comforts[i])
                   for i, room in enumerate(self.rooms)],
            state=decision.state, standoff=decision.standoff,
            mode_by_hands_off=decision.by_votes, mode_by_safety=decision.by_safety,
            mode_by_comfort_priority=decision.by_comfort, mode_by_floor=decision.by_floor,
            sensors_ok=True, seconds_since_mode_change=None if elapsed is None else int(elapsed),
            mode_change_allowed=decision.allowed, inhibited=False, idle_action=s.idle_action,
        )
        self._applying = plan
        self.plan = plan
        sent = False
        try:
            shared = pl.ApplyShared(
                retired=False, coordinator=self.coordinator, standby=None, inhibited=False,
                eco=False, idle_action=s.idle_action, coil_dry_seconds=s.coil_dry_seconds,
                clock=self.now, settings=s.setpoints, mode=self.mode, by_votes=self.by_votes,
                resting=None,
            )
            gate = pl.apply_gate(plan, shared)
            if gate in (None, "standby"):
                rooms = iter(self.rooms)
                queued: list[Room] = []
                for part in pl.apply_plan(plan, self._apply_heads(), shared):
                    if isinstance(part, pl.ModeWriteBack):
                        self._write_back(part)
                        continue
                    room = next(rooms)
                    for step in part.steps:
                        self._perform(room, step)
                    if room.queued is not None:
                        queued.append(room)
                sent = bool(queued)
                for room in queued:
                    if room.queued is not None:
                        self._deliver(room)
            for room, row in zip(self.rooms, plan.rooms, strict=True):
                row.fan_hold = room.latch.holds
        finally:
            self._applying = None
            self._armed.extend(tm.arms(self._wakeups(plan)))
            self._computing = False
        return st.Computed(sent=sent, asked=tuple(self._asked), armed=tuple(self._armed))

    def _apply_heads(self) -> Iterator[pl.ApplyHead]:
        """Each room's head as the apply reads it when it reaches it (``_apply_head``)."""
        for room in self.rooms:
            yield pl.ApplyHead(
                climate_id=room.climate_id, vane_kick=False,
                settling=lambda room=room: pl.holds_change(
                    self.waiting("heal", (room.climate_id, "adopt")),
                    lambda: self._adoption(room, room.pending),
                ),
                paused=False, head=self._mode(room), hands_off=None, released=False, busy=False,
                startup=room.slug in self.startup, enabled=room.enable, target=room.target,
                heat_target=room.target, cool_target=room.target, floor_clear=None,
                taken_over=None,
                coil_dry=lambda room=room: pl.CoilDryMemory(
                    room.last_run, self.waiting("dry", room.climate_id),
                ),
            )

    def _write_back(self, part: pl.ModeWriteBack) -> None:
        """The plan's mode becomes the shared mode, then the stamp, then the record
        (``MXZCoordinator._write_back_mode``)."""
        self.mode, self.by_votes = part.mode, part.by_votes
        if part.stamp:
            stamp = pl.stamp_mode_change(lambda: self.now, lambda: self.wall, retired=False, hold=self.settings.hold)
            self.stamp_at, self.stamp_wall = stamp.at, stamp.wall
            if stamp.hold_end is not None:
                self._arm(stamp.hold_end)
        if part.record:
            data: dict[str, Any] = {rs.MODE_KEY: self.mode, rs.MODE_AT_KEY: self.wall,
                                    rs.MODE_BY_VOTES_KEY: self.by_votes}
            if self.stamp_wall is not None:
                data[rs.MODE_CHANGE_KEY] = self.stamp_wall
            self.mode_data = data

    def _row(
        self, room: Room, given: pl.RoomInput, decided: pl.RoomDecision, status: str,
        comfort: tuple[float, str],
    ) -> RoomPlan:
        """The room's plan row from ``decide``'s result and what the compute read.

        The parts other controllers render (the delivery, the head, start points, the vote
        basis, the reasons) are left empty: the page does not show them.
        """
        return RoomPlan(
            slug=room.slug, name=room.name, demand=decided.demand, engage=decided.engage,
            temp=given.temp, target=room.target, target_range=None, enabled=room.enable,
            drift=self.settings.drift, drift_heat=given.heat_drift, drift_cool=given.cool_drift,
            start_points={}, sensor_health=sensor_health(valid=True, max_age=None, evidence_age=None, in_grace=False),
            sensor_age=None, hands_off=None, hands_off_vote=None, vote_status=status,
            lost_since=None, vote_basis={}, safety_priority=False,
            comfort_priority_entity=comfort_id(room.key), comfort_priority=comfort[0],
            comfort_priority_status=comfort[1], floor_low=None, floor_high=None, floor_want=None,
            dry={}, opening={}, fan_hold=room.latch.holds, control_reasons=[], floor={},
            delivery={}, head={},
        )

    def _wakeups(self, plan: Plan) -> tm.Wakeups:
        """What the wakeups armed after a compute read (``MXZCoordinator._wakeups``)."""
        s = self.settings
        elapsed = None if self.stamp_at is None else self.now - self.stamp_at
        sights = [room.sight for room in self.rooms if room.sight is not None]
        return tm.Wakeups(
            retired=False, clock_retired=False, coordinator=self.coordinator, standby=False,
            clock=self.now, wall=self.wall,
            remaining=float("-inf") if elapsed is None else s.hold - elapsed,
            allowed=plan.mode_change_allowed,
            held_back=lambda: pl.held_back(
                plan.rooms, current=pl.arbitration_mode(self.mode), resting=None,
            ),
            coasted=self.coasted, fresh=[], sights=sights,
            rooms=[tm.Watched(sight=sight, by_action=False, released=False) for sight in sights],
            lost_ttl=s.lost_vote_ttl, action_hold=float(ACTION_VOTE_HOLD),
            comfort_stale_after=s.comfort_stale_after,
            comfort=[
                (COMFORT_DOMAIN, "on" if room.comfort else "off", self.wall - room.comfort_at)
                for room in self.rooms
            ],
            clouds=[], openings=[],
        )

    # -- the timers ------------------------------------------------------------------------
    def _step(self, event: st.Event) -> st.Step:
        timers, did = st.step(self._timers(), event, self.now)
        self.timers = timers
        return did

    def _settle(self, did: st.Step, *, ended: bool = False) -> None:
        """Do what one ``sim_timing`` step handed back, in its order: the wakeup it ran, then
        the compute it started, if any. The compute runs whole; then each timer still due at
        this instant runs inside it, one ``Tick`` each; then ``Computed``, and a compute that
        starts runs the same way (``sim_timing``'s module docstring). ``ended``: the step was a
        ``Computed``."""
        for ran in did.ran:
            if isinstance(ran, st.Fire):
                self._fired(ran)
        if did.compute is None:
            return
        self.log.computes.append((self.now, str(did.compute)))
        computed = self._compute()
        if ended:
            self._go_on()
        while self.due() <= self.now:
            self._settle(self._step(st.Tick()))
        self._settle(self._step(computed), ended=True)

    def advance(self, until: float) -> bool:
        """Time passes to ``until``: each ``Tick`` runs MXZ's next timer at its due time, and
        what it hands back is done (``_settle``).

        Returns whether one ran.
        """
        ran = False
        while self.running and self.due() <= until:
            self.now = max(self.now, self.due())
            ran = True
            self._settle(self._step(st.Tick()))
        self.now = max(self.now, until)
        return ran

    def _fired(self, wakeup: st.Fire) -> None:
        """What a wakeup does besides the refresh ``timing.WAKEUPS`` names, which the step
        already asked for (``MXZCoordinator._on_heal_timer``, ``_on_startup_timer``)."""
        if wakeup.name == "startup":
            self.starting = tm.startup_quiet(running=False, startup_ran=True)
        elif wakeup.name == "heal":
            head, kind = wakeup.key  # type: ignore[misc]
            room = next(room for room in self.rooms if room.climate_id == head)
            if kind == "adopt":
                self._hold_end(room)
            else:
                mode = self._mode(room)
                self._ask(pl.heal_due(
                    kind, mode, hands_off_head=lambda: False, dry_allowed=lambda: False,
                    enabled=room.enable, eco_idle=False, planned=lambda: self._planned(room),
                    stale=lambda: False, coordinator=self.coordinator, inhibited=False,
                ))

    def _hold_end(self, room: Room) -> None:
        """Event 7: the adopt hold ends (``MXZCoordinator._async_adopt_head_change``): the
        change is judged, its steps done in order, then the compute it asks for last."""
        moved, room.pending = room.pending, set()
        adoption = self._adoption(room, moved)
        mode = self._mode(room)
        end = pl.hold_end(
            moved, adoption, mode=mode,
            field=None if mode is None else pl.setpoint_field(
                mode, features=self._attributes(room).get("supported_features"), cloud=False,
            ),
            dual=False,
        )
        left: deque[object] = deque(end.steps)
        if end.refresh is not None:
            left.append(end.refresh)
        self._holds.append((room, left))
        try:
            while left:
                self._perform(room, left.popleft())
        finally:
            self._holds.pop()

    def _go_on(self) -> None:
        """The row's one ordering rule of its own: a hold end whose write to its room started a
        compute goes on with its other steps once that compute has ended, inside the compute
        its end started.

        In Home Assistant the hold end writes the room's target or enable with a blocking
        service call, which waits on the compute the write asks for; the delivery queue starts
        its follow-up before that wait returns (2025.11 and later). No product function returns
        that, so the row keeps the steps each hold end has left (``_holds``) and does them here,
        after the follow-up's own apply. Of the scripts in tests/test_simulator.py it changes
        only those where two hold ends fall together with no compute running
        (``two_holds_off``, ``two_holds_off_then_reading``, ``TWO_SETPOINTS``). The page and
        docs/simulator/README.md name it.
        """
        for room, left in list(self._holds):
            while left:
                self._perform(room, left.popleft())

    # -- event 3: a person's write to a room, and the room's readings ----------------------
    def _room(self, key: str) -> Room:
        return next(room for room in self.rooms if room.key == key)

    def target_bounds(self) -> tuple[float, float]:
        """The range the room's target number offers (``Grid.target_bounds``)."""
        return self.grid().target_bounds()

    def set_target(self, key: str, value: float) -> None:
        """A person sets the room's target number (``number.set_value``).

        Home Assistant refuses a value outside the number's range before MXZ sees it; so does
        this, with ``OutOfRange``.
        """
        room = self._room(key)
        low, high = self.target_bounds()
        if not low <= float(value) <= high:
            raise OutOfRange(f"{room.name}'s target {value:g} °F is outside {low:g} to {high:g} °F")
        room.target = float(value)
        for step in pl.target_written(
            room.climate_id, startup=room.slug in self.startup, coordinator=self.coordinator,
            enabled=room.enable,
        ):
            self._perform(room, step)
        self._ask(tm.Refresh.REQUEST)

    def set_enable(self, key: str, on: bool) -> None:
        """A person turns the room's enable switch on or off."""
        room = self._room(key)
        was_on, room.enable = room.enable, on
        for step in pl.enable_written(room.climate_id, was_on=was_on, on=on):
            self._perform(room, step)
        self._ask(tm.Refresh.REQUEST)

    def set_comfort(self, key: str, on: bool) -> None:
        """The room's Comfort priority entity turns on or off (``_on_comfort_change``)."""
        room = self._room(key)
        if room.comfort == on:
            return
        room.comfort, room.comfort_at = on, self.wall
        self._ask(tm.Refresh.REQUEST)

    def set_fan_auto(self, key: str, on: bool) -> None:
        """A person turns the room's Fan auto switch on or off (``FanHold.async_set_auto``)."""
        room = self._room(key)
        latch, acted = fl.fan_auto(
            room.latch, self._attributes(room).get("fan_mode"), on, self.settings.ceiling,
        )
        room.latch = latch
        if acted:
            self._ask(tm.Refresh.REQUEST)

    def press(self, key: str, **fields: Any) -> None:
        """A person changes the head at its remote (``invented_house``'s ``remote`` service)."""
        head = self.heads.devices.heads[key]
        for name in ("hvac_mode", "target_temp_low", "target_temp_high", "fan_mode"):
            if name in fields:
                setattr(head, name, fields[name])
        self.heads.changed()
        self.world_changed()

    # -- what the page and the tests read --------------------------------------------------
    def hold_left(self) -> float:
        """Seconds the mode-change hold still holds a flip back (``mode_hold_remaining``)."""
        elapsed = None if self.stamp_at is None else self.now - self.stamp_at
        return 0.0 if elapsed is None else max(0.0, self.settings.hold - elapsed)

    def pending_waits(self) -> dict[str, float]:
        """Each armed wakeup, by name and key, with the seconds until it runs."""
        timers = self._timers()
        return {
            f"{name} {key}": round(due.at - self.now, 6)
            for (name, key), due in sorted(timers.waits.items(), key=lambda item: str(item[0]))
        }


    def rooms_view(self) -> list[dict[str, Any]]:
        out = []
        for room in self.rooms:
            out.append({
                "key": room.key, "target": room.target, "enable": room.enable,
                "comfort": room.comfort, "fan_held": room.latch.holds,
                "adopting": self.waiting("heal", (room.climate_id, "adopt")),
                "drying": self.waiting("dry", room.climate_id),
            })
        return out


def plan_view(plan: Plan | None) -> dict[str, Any] | None:
    """The plan's decision as the page shows it and the tests compare it."""
    if plan is None:
        return None
    return {
        "state": plan.state,
        "standoff": plan.standoff,
        "mode_by_hands_off": plan.mode_by_hands_off,
        "mode_by_safety": plan.mode_by_safety,
        "mode_by_comfort_priority": plan.mode_by_comfort_priority,
        "mode_by_floor": plan.mode_by_floor,
        "sensors_ok": plan.sensors_ok,
        "seconds_since_mode_change": plan.seconds_since_mode_change,
        "mode_change_allowed": plan.mode_change_allowed,
        "idle_action": plan.idle_action,
        "rooms": [
            {
                "slug": room.slug, "demand": room.demand, "engage": room.engage,
                "temp": room.temp, "target": room.target, "enabled": room.enabled,
                "fan_hold": room.fan_hold, "sensor_health": room.sensor_health,
                "vote_status": room.vote_status, "comfort_priority": room.comfort_priority,
                "comfort_priority_status": room.comfort_priority_status,
            }
            for room in plan.rooms
        ],
    }

