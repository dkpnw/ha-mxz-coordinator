"""The plan one compute decides, and the attributes it is shown as.

``Plan`` and ``RoomPlan`` are the decision record the apply, the mode's
writeback, the dwell wakeup and the room thermostat read back. ``to_attributes``
renders them as the plan sensor, the room rows and diagnostics show them; key
order is part of what they show (``tests/plan_attributes.json`` records it).

A room row also carries what other controllers show for the room (its delivery,
head, floor, Dry and door/window state, its start points and vote basis). Each
of those is the dict its controller renders, kept as rendered, and a few of
them are refreshed while a plan's deliveries run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .cloud import CLOUD_NOT_CONTROLLABLE


@dataclass(slots=True)
class RoomPlan:
    """One room's part of a plan, in the order its row shows it."""

    slug: str
    name: str
    demand: str
    engage: str
    # The reading as shown: None when it was rejected (a stale one is shown).
    temp: float | None
    target: float
    # A dual-setpoint room's Heat to and Cool to; None for a single target.
    target_range: tuple[float | None, float | None] | None
    enabled: bool
    drift: float
    drift_heat: float
    drift_cool: float
    start_points: dict[str, Any]
    sensor_health: str
    sensor_age: int | None
    hands_off: str | None
    hands_off_vote: str | None
    vote_status: str
    lost_since: str | None
    vote_basis: dict[str, Any]
    safety_priority: bool
    comfort_priority_entity: str | None
    comfort_priority: float
    comfort_priority_status: str
    floor_low: float | None
    floor_high: float | None
    floor_want: str | None
    dry: dict[str, Any]
    opening: dict[str, Any]
    # Refreshed by the coordinator as the plan's deliveries run.
    fan_hold: bool
    control_reasons: list[dict[str, str]]
    floor: dict[str, Any]
    delivery: dict[str, Any]
    head: dict[str, Any]

    @property
    def cloud_out(self) -> bool:
        """Whether the row shows a cloud head out of MXZ's control."""
        return self.head.get("cloud_status") == CLOUD_NOT_CONTROLLABLE

    @property
    def dry_state(self) -> str | None:
        """An Allow Dry room's dry state as the row shows it; None for any other room."""
        return self.dry.get("dry")

    @property
    def dry_vote(self) -> str | None:
        """An Allow Dry room's dry vote as the row shows it; None for any other room."""
        return self.dry.get("dry_vote")

    def to_attributes(self) -> dict[str, Any]:
        """The room's row, as the plan sensor and the room thermostat show it."""
        return {
            "name": self.name,
            "demand": self.demand,
            "engage": self.engage,
            "temp": self.temp,
            "target": self.target,
            **(
                {"target_low": self.target_range[0], "target_high": self.target_range[1]}
                if self.target_range is not None else {}
            ),
            "enabled": self.enabled,
            "drift": self.drift,
            "drift_heat": self.drift_heat,
            "drift_cool": self.drift_cool,
            **self.start_points,
            "fan_hold": self.fan_hold,
            "sensor_health": self.sensor_health,
            "sensor_age": self.sensor_age,
            "control_reasons": self.control_reasons,
            "hands_off": self.hands_off,
            "hands_off_vote": self.hands_off_vote,
            "vote_status": self.vote_status,
            "lost_since": self.lost_since,
            **self.vote_basis,
            "safety_priority": self.safety_priority,
            "comfort_priority_entity": self.comfort_priority_entity,
            "comfort_priority": self.comfort_priority,
            "comfort_priority_status": self.comfort_priority_status,
            "floor_low": self.floor_low,
            "floor_high": self.floor_high,
            **self.floor,
            "floor_want": self.floor_want,
            **self.delivery,
            **self.head,
            **self.dry,
            **self.opening,
        }


@dataclass(slots=True)
class Plan:
    """One compute's decision: the shared mode, why, and each room's part."""

    rooms: list[RoomPlan]
    state: str
    standoff: bool
    mode_by_hands_off: bool
    mode_by_safety: bool
    mode_by_comfort_priority: bool
    mode_by_floor: bool
    sensors_ok: bool
    seconds_since_mode_change: int | None
    mode_change_allowed: bool
    inhibited: bool
    idle_action: str
    # Only an entry with a door/window pause has the list.
    paused_rooms: list[str] | None = field(default=None)

    def room(self, slug: str) -> RoomPlan | None:
        """The room with this slug, if the plan has it."""
        return next((room for room in self.rooms if room.slug == slug), None)

    def to_attributes(self) -> dict[str, Any]:
        """The plan as the plan sensor and diagnostics show it."""
        attributes: dict[str, Any] = {}
        for room in self.rooms:
            attributes[f"{room.slug}_demand"] = room.demand
            attributes[f"{room.slug}_engage"] = room.engage
            attributes[f"{room.slug}_temp"] = room.temp
            attributes[f"{room.slug}_sensor_health"] = room.sensor_health
            attributes[f"{room.slug}_sensor_age"] = room.sensor_age
        attributes.update(
            {
                "state": self.state,
                "zones": [room.to_attributes() for room in self.rooms],
                "standoff": self.standoff,
                "mode_by_hands_off": self.mode_by_hands_off,
                "mode_by_safety": self.mode_by_safety,
                "mode_by_comfort_priority": self.mode_by_comfort_priority,
                "mode_by_floor": self.mode_by_floor,
                "sensors_ok": self.sensors_ok,
                "seconds_since_mode_change": self.seconds_since_mode_change,
                "mode_change_allowed": self.mode_change_allowed,
                "inhibited": self.inhibited,
                "idle_action": self.idle_action,
            }
        )
        if self.paused_rooms is not None:
            attributes["paused_rooms"] = self.paused_rooms
        return attributes
