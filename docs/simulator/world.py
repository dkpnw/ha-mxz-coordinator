"""The simulator's world: the same three invented rooms twice, stock and MXZ, on one clock.

Both rows follow the invented house's three rules (``invented_house/model.py``): one outdoor
unit runs one direction, a head in that direction moves its room toward its setpoint at an
invented 1 °F a minute, and a room no head moves drifts toward an invented 76 °F. Neither row
measures real equipment.

- The stock row's heads are set by a person and nothing else. It follows only the rule from
  Mitsubishi's manual for heads on one outdoor unit: "the unit selected last goes into standby
  mode." It is not stock AUTO.
- The MXZ row is MXZ Coordinator's own code (``mxz_row.MxzRow``) at its default settings.

Each room moves on its own 5-second tick, a second apart from the room before it, so each room's
sensor reports on its own, as real sensors do (``mxz_row.MxzRow.world_changed`` says why that
matters). The page reads ``presets()`` for its scenario buttons, starts a ``World`` from one,
then calls ``advance`` as time passes and one method per control, and draws ``view()``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from invented_house import devices as dv
from invented_house import model
from mxz_row import InventedHeads, MxzRow, OutOfRange, climate_id, plan_view

# The wall clock at the simulation's start: 2026-11-19 15:00 UTC. Only differences matter.
WALL = 1_795_100_400.0
PHASE = 1.0  # seconds between one room's tick and the next room's


@dataclass(frozen=True)
class Preset:
    """A starting point: each room's temperature, each stock head as a person set it, and
    what MXZ's store holds at its start (its targets and the last heat/cool change).

    ``title`` is the scenario's one name; the page's buttons read it. ``more`` puts the
    scenario after the three that compare the rows. ``remote`` is the room whose wall remote
    ``try_this`` asks for, so the page opens it."""

    title: str
    try_this: str
    temps: dict[str, float]
    stock_heads: dict[str, dict[str, Any]]
    stock_outdoor: str | None
    mode: str
    changed_ago: float | None = None  # seconds since MXZ's last heat/cool change; None: none
    targets: dict[str, float] = field(default_factory=lambda: {"studio": 70.0, "loft": 70.0, "den": 70.0})
    more: bool = False
    remote: str | None = None


def _set(mode: str, to: float = 70.0, fan: str = "auto") -> dict[str, Any]:
    return {"hvac_mode": mode, "target_temp_low": to, "target_temp_high": to, "fan_mode": fan}


PRESETS: dict[str, Preset] = {
    "mixed": Preset(
        title="One room wants cool",
        try_this="Watch Studio at 76 °F. Stock leaves it waiting in standby because the outdoor unit "
                 "is running heat. MXZ switches the shared mode to cool, steps the fan down as Studio "
                 "nears 70 °F, then dries the coil.",
        temps={"studio": 76.0, "loft": 70.0, "den": 70.0},
        stock_heads={"studio": _set("cool"), "loft": _set("heat"), "den": _set("heat")},
        stock_outdoor="heat", mode="heat", changed_ago=7200.0,
    ),
    "satisfied": Preset(
        title="Every room reaches target",
        try_this="Each row heats all three rooms to 70 °F. Then the stock heads stay in heat and "
                 "idle, and MXZ turns every head off.",
        temps={"studio": 66.0, "loft": 66.0, "den": 66.0},
        stock_heads={key: _set("heat") for key in ("studio", "loft", "den")},
        stock_outdoor="heat", mode="heat", changed_ago=7200.0,
    ),
    "standoff": Preset(
        title="Opposite modes (standoff)",
        try_this="Studio, first in the room order, wants cool, and Den wants heat. MXZ cools Studio "
                 "until it is within 3 °F of its target. Turn on Den's Comfort priority before then "
                 "to switch to heat right away.",
        temps={"studio": 79.0, "loft": 70.0, "den": 66.0},
        stock_heads={"studio": _set("cool"), "loft": _set("off"), "den": _set("heat")},
        stock_outdoor="cool", mode="cool", changed_ago=7200.0,
    ),
    "mode_wait": Preset(
        title="10-minute mode-switch wait",
        try_this="MXZ switched to heat 1 minute ago. Studio wants cool, so MXZ waits out the "
                 "10-minute minimum between mode switches. Watch the countdown.",
        temps={"studio": 76.0, "loft": 70.0, "den": 70.0},
        stock_heads={"studio": _set("cool"), "loft": _set("heat"), "den": _set("heat")},
        stock_outdoor="heat", mode="heat", changed_ago=60.0, more=True,
    ),
    "fan_hold": Preset(
        title="Manual fan speed",
        try_this="MXZ cools Studio with the fan on high. Pick a fan speed on Studio's wall remote in "
                 "the MXZ row: MXZ keeps it until you turn Fan auto back on.",
        temps={"studio": 76.0, "loft": 70.0, "den": 70.0},
        stock_heads={"studio": _set("cool"), "loft": _set("off"), "den": _set("off")},
        stock_outdoor="cool", mode="cool", changed_ago=7200.0, more=True, remote="studio",
    ),
    "restart": Preset(
        title="Restart during the wait",
        try_this="MXZ has just switched to cool, which starts the 10-minute wait. Pick a fan speed "
                 "on Studio's wall remote, then press Restart Home Assistant: the fan speed and the "
                 "wait both carry over.",
        temps={"studio": 76.0, "loft": 70.0, "den": 70.0},
        stock_heads={"studio": _set("cool"), "loft": _set("heat"), "den": _set("heat")},
        stock_outdoor="heat", mode="heat", changed_ago=7200.0, more=True, remote="studio",
    ),
}


def presets() -> str:
    """The scenarios in order, for the page's buttons: name, title and whether it is one more."""
    return json.dumps([{"name": name, "title": preset.title, "more": preset.more}
                       for name, preset in PRESETS.items()])


def store(preset: Preset, keys: list[str]) -> dict[str, Any]:
    """The room state store's record MXZ starts from: the Coordinator on, every room on at
    its target, and the last heat/cool change, if any."""
    record: dict[str, Any] = {
        "saved_at": WALL - 1.0,
        "unit": "°F",
        "switches": {"coordinator_enable": True},
        "rooms": {climate_id(key): {"target": preset.targets[key], "enable": True} for key in keys},
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
        for key, head in self.preset.stock_heads.items():
            self.stock.heads[key] = dv.Head(**head)
        self.stock.outdoor = self.preset.stock_outdoor
        self.heads = InventedHeads(self.house, self.mxz, modelled=True)
        self.heads.changed()
        self.row = MxzRow(self.heads, start=0.0, wall=WALL)
        self.row.start(store(self.preset, self.keys), names=self.names)
        self.restarts = 0
        # A person pressed a wall remote while MXZ's start was quiet, so the page says why
        # MXZ did not take it. On the first start nothing else needs that said.
        self.pressed_while_quiet = False

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
        """Time passes to ``until``: MXZ's timers and each room's tick, in time order."""
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

    # -- controls --------------------------------------------------------------------------
    def set_temperature(self, key: str, value: float) -> None:
        """Both rows' room jumps to ``value``, as if the weather changed it."""
        for devices in (self.stock, self.mxz):
            devices.rooms[key].temperature = float(value)
        self.row.world_changed()

    def stock_head(self, key: str, mode: str, to: float, fan: str) -> None:
        """A person sets a stock head: its mode, the temperature it runs to, and its fan."""
        self.stock.heads[key] = dv.Head(**_set(mode, float(to), fan))
        self.stock.outdoor = model.direction(self.stock, self.stock.outdoor)

    def mxz_target(self, key: str, value: float) -> str | None:
        """A person sets an MXZ room's target number. Returns why Home Assistant refused it,
        or None."""
        try:
            self.row.set_target(key, float(value))
        except OutOfRange:
            low, high = self.row.target_bounds()
            return f"Target must be {low:g} to {high:g} °F. Home Assistant refused {float(value):g} °F."
        return None

    def mxz_enable(self, key: str, on: bool) -> None:
        self.row.set_enable(key, bool(on))

    def mxz_comfort(self, key: str, on: bool) -> None:
        self.row.set_comfort(key, bool(on))

    def mxz_fan_auto(self, key: str, on: bool) -> None:
        self.row.set_fan_auto(key, bool(on))

    def mxz_remote(self, key: str, mode: str | None = None, to: float | None = None,
                   fan: str | None = None) -> None:
        """A person changes an MXZ room's head at its remote: its mode, the setpoint its mode
        runs to (the low one in heat, the high one in cool, as on the invented head), or its fan."""
        head = self.mxz.heads[key]
        fields: dict[str, Any] = {}
        if mode is not None:
            fields["hvac_mode"] = mode
        if to is not None:
            running = mode or head.hvac_mode
            fields["target_temp_low" if running == "heat" else "target_temp_high"] = float(to)
        if fan is not None:
            fields["fan_mode"] = fan
        if self.row.starting:
            self.pressed_while_quiet = True
        self.row.press(key, **fields)

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
            "preset": {"title": self.preset.title, "try_this": self.preset.try_this,
                       "remote": self.preset.remote},
            # The range of an invented head's setpoints, which its remote offers.
            "head_range": [self.house["head"]["min_temp"], self.house["head"]["max_temp"]],
            "stock": {"outdoor": self.stock.outdoor, "rooms": []},
            "mxz": {
                "outdoor": self.mxz.outdoor,
                "mode": row.mode,
                "target_bounds": list(row.target_bounds()),
                "hold_left": row.hold_left(),
                "standoff": None if plan is None else plan["standoff"],
                "by_comfort": None if plan is None else plan["mode_by_comfort_priority"],
                # While MXZ's start is quiet it takes no change made at a head
                # (timing.startup_quiet); its startup wakeup ends that. The page shows it after
                # a restart, or once a person has pressed a wall remote in it.
                "quiet_for": row.pending_waits().get("startup None") if row.starting else None,
                "quiet_shown": bool(self.restarts or self.pressed_while_quiet),
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
