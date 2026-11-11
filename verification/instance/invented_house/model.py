"""The demo's outdoor unit and rooms: three invented rules, and no more.

Off unless a journey turns it on (feature map section 6), so the proof journeys
keep setting exact temperatures. With it on, every room temperature after the
start follows from what the heads were told:

1. One outdoor unit runs one direction. A head set to the other direction waits
   in standby until no head is set to the running direction. This is the rule
   Mitsubishi's manual states for heads on one outdoor unit: "the unit selected
   last goes into standby mode."
2. A head in the running direction moves its room toward its setpoint at a fixed
   rate, then idles in its mode. Heat uses the low setpoint, cool the high one.
3. A room no head is moving drifts toward a fixed ambient at a slower fixed rate.

It measures nothing: the rates are invented, the fan does not change them, and
nothing here is a model of real equipment. No Home Assistant import: the
verification suite tests this module directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .devices import Devices, Head, Room

HEAT = "heat"
COOL = "cool"
DIRECTIONS = (HEAT, COOL)


@dataclass(frozen=True)
class Climate:
    """The invented numbers, from house.json."""

    ambient: float
    run_rate: float  # degrees per minute a running head moves its room
    drift_rate: float  # degrees per minute an unattended room moves toward ambient
    tick: float  # seconds between steps


def direction(devices: Devices, running: str | None) -> str | None:
    """Rule 1: the direction the outdoor unit runs after the heads' latest change.

    It keeps its direction while any head is set to it. Otherwise it takes the
    direction of the first head, in house order, set to heat or cool.
    """
    asked = [head.hvac_mode for head in devices.heads.values() if head.available and head.hvac_mode in DIRECTIONS]
    if running in asked:
        return running
    return asked[0] if asked else None


def setpoint(head: Head) -> float | None:
    """The setpoint a head runs to in its mode: the low one in heat, the high one in cool."""
    if head.hvac_mode == HEAT:
        return head.target_temp_low
    if head.hvac_mode == COOL:
        return head.target_temp_high
    return None


def action(head: Head, room: Room, running: str | None) -> str:
    """What a head is doing: off, fan, standby, heating, cooling or idle."""
    if head.hvac_mode == "off" or not head.available:
        return "off"
    if head.hvac_mode not in DIRECTIONS:
        return "fan"
    if head.hvac_mode != running:
        return "standby"
    target = setpoint(head)
    if target is not None and head.hvac_mode == HEAT and room.temperature < target:
        return "heating"
    if target is not None and head.hvac_mode == COOL and room.temperature > target:
        return "cooling"
    return "idle"


def _toward(value: float, goal: float, most: float) -> float:
    return goal if abs(goal - value) <= most else value + most * (1 if goal > value else -1)


def step(devices: Devices, climate: Climate, minutes: float) -> None:
    """Move every room ``minutes`` forward under rules 2 and 3, after rule 1."""
    devices.outdoor = direction(devices, devices.outdoor)
    for key, room in devices.rooms.items():
        head = devices.heads[key]
        target = setpoint(head)
        if target is not None and action(head, room, devices.outdoor) in ("heating", "cooling"):
            room.temperature = _toward(room.temperature, target, climate.run_rate * minutes)
        else:
            room.temperature = _toward(room.temperature, climate.ambient, climate.drift_rate * minutes)
        room.temperature = round(room.temperature, 3)
