"""The simulator's world: the same three invented rooms twice, stock and MXZ, on one clock.

Both rows follow the invented house's three rules (``invented_house/model.py``): one outdoor
unit runs one direction, a head in that direction moves its room toward its setpoint at an
invented 1 °F a minute, and a room no head moves drifts toward an invented 76 °F. Neither row
measures real equipment.

- The stock row's heads are set by a person and nothing else. It follows only the rule from
  Mitsubishi's manual for heads on one outdoor unit: "the unit selected last goes into standby
  mode." It is not stock AUTO.
- The MXZ row is MXZ Coordinator's own code (``mxz_row.MxzRow``) at its default settings.

One set of controls drives both rows. A person sets each room's mode, set-to and fan; the
stock head takes them as they are. On the MXZ row, off or on is the room's switch, the set-to
is its target number, and the fan is pressed at the room's remote, where a speed is held and
auto hands the fan back (``fan_latch.observe_fan``). A speed the head already shows is held
with the room's Fan auto off, because pressing it changes nothing MXZ sees. A pick of the
speed MXZ set one step earlier is not held: MXZ reads it as its own write (``FanEcho.own``) and
puts back its own speed, as it does in Home Assistant.

Each room moves on its own 5-second tick, a second apart from the room before it, so each room's
sensor reports on its own, as real sensors do (``mxz_row.MxzRow.world_changed`` says why that
matters). The page reads ``presets()`` for its scenario buttons, starts a ``World`` from one,
then calls ``advance`` as time passes and one method per control, and draws ``view()``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from invented_house import devices as dv
from invented_house import model
from mxz_row import InventedHeads, MxzRow, OutOfRange, climate_id, plan_view

# The wall clock at the simulation's start: 2026-11-19 15:00 UTC. Only differences matter.
WALL = 1_795_100_400.0
PHASE = 1.0  # seconds between one room's tick and the next room's


@dataclass(frozen=True)
class Setting:
    """What a person set for one room: its mode, the temperature it runs to, and its fan."""

    mode: str
    to: float = 70.0
    fan: str = "auto"


@dataclass(frozen=True)
class Preset:
    """A starting point: each room's temperature, what a person set for each room, and the
    MXZ row's shared mode and its last heat/cool change.

    ``settings`` is each room's mode, set-to and fan. The stock head starts at it; MXZ's
    stored room starts with the set-to as its target, and on unless the mode is off.
    ``title`` is the scenario's one name; the page's buttons read it. ``more`` puts the
    scenario after the three that compare the rows. ``restart_at`` is when Home Assistant
    restarts in this scenario, if it does."""

    title: str
    try_this: str
    temps: dict[str, float]
    settings: dict[str, Setting]
    stock_outdoor: str | None
    mode: str
    changed_ago: float | None = None  # seconds since MXZ's last heat/cool change; None: none
    more: bool = False
    restart_at: float | None = None


PRESETS: dict[str, Preset] = {
    "mixed": Preset(
        title="One room wants cool",
        try_this="Stock leaves Studio in standby while the outdoor unit heats. MXZ switches to cool, "
                 "steps the fan down as Studio nears 70 °F, then dries the coil.",
        temps={"studio": 76.0, "loft": 70.0, "den": 70.0},
        settings={"studio": Setting("cool"), "loft": Setting("heat"), "den": Setting("heat")},
        stock_outdoor="heat", mode="heat", changed_ago=7200.0,
    ),
    "satisfied": Preset(
        title="Every room reaches target",
        try_this="Both rows heat every room to 70 °F. Stock heads then idle in heat; MXZ turns them off.",
        temps={"studio": 66.0, "loft": 66.0, "den": 66.0},
        settings={key: Setting("heat") for key in ("studio", "loft", "den")},
        stock_outdoor="heat", mode="heat", changed_ago=7200.0,
    ),
    "standoff": Preset(
        title="Opposite modes (standoff)",
        try_this="Stock keeps cooling Studio while Den waits in standby. MXZ cools Studio to within "
                 "3 °F of its target, then heats Den.",
        temps={"studio": 79.0, "loft": 70.0, "den": 66.0},
        settings={"studio": Setting("cool"), "loft": Setting("off"), "den": Setting("heat")},
        stock_outdoor="cool", mode="cool", changed_ago=7200.0,
    ),
    "mode_wait": Preset(
        title="10-minute mode-switch wait",
        try_this="MXZ switched to heat a minute ago. It cools Studio once the 10-minute minimum "
                 "between mode switches ends.",
        temps={"studio": 76.0, "loft": 70.0, "den": 70.0},
        settings={"studio": Setting("cool"), "loft": Setting("heat"), "den": Setting("heat")},
        stock_outdoor="heat", mode="heat", changed_ago=60.0, more=True,
    ),
    "fan_hold": Preset(
        title="Manual fan speed",
        try_this="Pick a fan speed for Studio. Stock keeps it, and MXZ holds it unless it is the speed "
                 "MXZ set one step earlier. MXZ reads that pick as its own write and puts back its own "
                 "speed. Auto gives the fan back to MXZ.",
        temps={"studio": 76.0, "loft": 70.0, "den": 70.0},
        settings={"studio": Setting("cool"), "loft": Setting("off"), "den": Setting("off")},
        stock_outdoor="cool", mode="cool", changed_ago=7200.0, more=True,
    ),
    "restart": Preset(
        title="Restart during the wait",
        try_this="Home Assistant restarts at 5:00. Pick a fan speed for Studio first: MXZ keeps a held "
                 "speed and the 10-minute wait through the restart.",
        temps={"studio": 76.0, "loft": 70.0, "den": 70.0},
        settings={"studio": Setting("cool"), "loft": Setting("heat"), "den": Setting("heat")},
        stock_outdoor="heat", mode="heat", changed_ago=7200.0, more=True, restart_at=300.0,
    ),
}


def presets() -> str:
    """The scenarios in order, for the page's buttons: name, title and whether it is one more."""
    return json.dumps([{"name": name, "title": preset.title, "more": preset.more}
                       for name, preset in PRESETS.items()])


def store(preset: Preset, keys: list[str]) -> dict[str, Any]:
    """The room state store's record MXZ starts from: the Coordinator on, each room at the
    set-to a person chose and on unless its mode is off, and the last heat/cool change, if any."""
    record: dict[str, Any] = {
        "saved_at": WALL - 1.0,
        "unit": "°F",
        "switches": {"coordinator_enable": True},
        "rooms": {
            climate_id(key): {"target": preset.settings[key].to, "enable": preset.settings[key].mode != "off"}
            for key in keys
        },
    }
    if preset.changed_ago is not None:
        changed = WALL - preset.changed_ago
        record["mode"] = {"mode": preset.mode, "mode_at": changed, "mode_by_votes": False,
                          "last_mode_change": changed}
    return record


class World:
    """Both rows, the rooms' model and the clock."""

    def __init__(self, preset: str) -> None:
        self.preset = PRESETS[preset]
        self.house = dv.load_house()
        self.keys = [room["key"] for room in self.house["rooms"]]
        self.names = {room["key"]: room["name"] for room in self.house["rooms"]}
        self.climate = model.Climate(**self.house["climate"])
        self.now = 0.0
        self.stock = dv.Devices.fresh(self.house)
        self.mxz = dv.Devices.fresh(self.house)
        for devices in (self.stock, self.mxz):
            for key, temperature in self.preset.temps.items():
                devices.rooms[key].temperature = temperature
        for key, setting in self.preset.settings.items():
            self.stock.heads[key] = dv.Head(
                hvac_mode=setting.mode, target_temp_low=setting.to, target_temp_high=setting.to,
                fan_mode=setting.fan,
            )
        self.stock.outdoor = self.preset.stock_outdoor
        self.heads = InventedHeads(self.house, self.mxz, modelled=True)
        self.heads.changed()
        self.row = MxzRow(self.heads, start=0.0, wall=WALL)
        self.row.start(store(self.preset, self.keys), names=self.names)
        self.restarts = 0

    # -- time ------------------------------------------------------------------------------
    def _next_tick(self) -> tuple[float, int]:
        """When the next room moves, and which (by house order)."""
        tick = self.climate.tick
        best: tuple[float, int] | None = None
        for i in range(len(self.keys)):
            offset = i * PHASE
            k = max(0, -(-(self.now - offset) // tick))  # the next tick at or after now
            at = offset + k * tick
            if at <= self.now:
                at += tick
            if best is None or at < best[0]:
                best = (at, i)
        assert best is not None
        return best

    def advance(self, until: float) -> None:
        """Time passes to ``until``, with the scenario's restart on the way if it has one."""
        at = self.preset.restart_at
        if at is not None and self.now < at <= until:
            self._run(at)
            self.restart()
        self._run(until)

    def _run(self, until: float) -> None:
        """MXZ's timers and each room's tick, in time order, up to ``until``."""
        while True:
            at, i = self._next_tick()
            if at > until:
                break
            self.row.advance(at)
            self.now = at
            self._tick(self.keys[i])
        self.row.advance(until)
        self.now = until

    def _tick(self, key: str) -> None:
        """One room moves by the model's rules, in both rows."""
        minutes = self.climate.tick / 60
        for devices in (self.stock, self.mxz):
            one = dv.Devices(heads=devices.heads, rooms={key: devices.rooms[key]}, outdoor=devices.outdoor)
            model.step(one, self.climate, minutes)
            devices.outdoor = one.outdoor
        self.row.world_changed()

    # -- controls: each one changes both rows ----------------------------------------------
    def set_temperature(self, key: str, value: float) -> None:
        """Both rows' room jumps to ``value``, as if the weather changed it."""
        for devices in (self.stock, self.mxz):
            devices.rooms[key].temperature = float(value)
        self.row.world_changed()

    def set_mode(self, key: str, mode: str) -> None:
        """A person sets the room to off, heat or cool: the stock head's mode, and the MXZ
        room's on switch. MXZ picks heat or cool for a room that is on."""
        self._stock(key, hvac_mode=mode)
        self.row.set_enable(key, mode != "off")

    def set_to(self, key: str, value: float) -> str | None:
        """A person sets the temperature the room runs to: the stock head's setpoint and the
        MXZ room's target number. Returns why Home Assistant refused the target, and then
        neither row changes; or None."""
        try:
            self.row.set_target(key, float(value))
        except OutOfRange:
            low, high = self.row.target_bounds()
            return f"Target must be {low:g} to {high:g} °F. Home Assistant refused {float(value):g} °F."
        self._stock(key, target_temp_low=float(value), target_temp_high=float(value))
        return None

    def set_fan(self, key: str, fan: str) -> None:
        """A person sets the room's fan: on the stock head, and at the MXZ head's remote.

        A speed the MXZ head already shows changes nothing the head reports, so MXZ would not
        see it. For that pick the person turns the room's Fan auto off instead, which holds the
        speed the head shows (``fan_latch.fan_auto``)."""
        self._stock(key, fan_mode=fan)
        if fan != "auto" and self.mxz.heads[key].fan_mode == fan:
            self.row.set_fan_auto(key, False)
        else:
            self.row.press(key, fan_mode=fan)

    def _stock(self, key: str, **fields: Any) -> None:
        head = self.stock.heads[key]
        for name, value in fields.items():
            setattr(head, name, value)
        self.stock.outdoor = model.direction(self.stock, self.stock.outdoor)

    def restart(self) -> None:
        """Home Assistant stops and starts. The heads and rooms carry on."""
        self.row.restart()
        self.restarts += 1

    # -- what the page draws ---------------------------------------------------------------
    def view(self) -> str:
        row = self.row
        plan = plan_view(row.plan)
        rooms = {view["key"]: view for view in row.rooms_view()}
        out: dict[str, Any] = {
            "now": self.now,
            "preset": {"title": self.preset.title, "try_this": self.preset.try_this},
            "stock": {"outdoor": self.stock.outdoor, "rooms": []},
            "mxz": {
                "outdoor": self.mxz.outdoor,
                "mode": row.mode,
                "target_bounds": list(row.target_bounds()),
                "hold_left": row.hold_left(),
                "standoff": None if plan is None else plan["standoff"],
                "waits": row.pending_waits(),
                "restarts": self.restarts,
                "calls": [
                    {"t": t, "room": self.names[key], "service": service, "data": data}
                    for t, key, service, data in row.log.calls[-8:]
                ],
                "rooms": [],
            },
        }
        for i, key in enumerate(self.keys):
            for side, devices in (("stock", self.stock), ("mxz", self.mxz)):
                head = devices.heads[key]
                room = devices.rooms[key]
                entry = {
                    "key": key,
                    "name": self.names[key],
                    "temperature": dv.reading(room.temperature),
                    "mode": head.hvac_mode,
                    "low": head.target_temp_low,
                    "high": head.target_temp_high,
                    "fan": head.fan_mode,
                    "doing": model.action(head, room, devices.outdoor),
                }
                if side == "mxz":
                    decided = None if plan is None else plan["rooms"][i]
                    entry.update(rooms[key])
                    entry["engage"] = None if decided is None else decided["engage"]
                    entry["demand"] = None if decided is None else decided["demand"]
                out[side]["rooms"].append(entry)
        return json.dumps(out)
