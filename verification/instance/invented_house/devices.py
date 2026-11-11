"""What each invented device holds, kept in a file that outlives Home Assistant.

A real head keeps its mode, setpoints and fan while Home Assistant is down, so
every change is written at once and read back at start. No Home Assistant
import: the verification suite tests this module directly.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HOUSE = Path(__file__).with_name("house.json")
DEVICE_FILE = "invented_house.json"


@dataclass
class Head:
    hvac_mode: str = "off"
    target_temp_low: float | None = None
    target_temp_high: float | None = None
    fan_mode: str = "auto"
    available: bool = True


@dataclass
class Room:
    temperature: float
    window_open: bool = False


@dataclass
class Devices:
    heads: dict[str, Head] = field(default_factory=dict)
    rooms: dict[str, Room] = field(default_factory=dict)

    @classmethod
    def fresh(cls, house: dict[str, Any]) -> Devices:
        return cls(
            heads={room["key"]: Head() for room in house["rooms"]},
            rooms={room["key"]: Room(float(room["temperature"])) for room in house["rooms"]},
        )

    @classmethod
    def load(cls, path: Path, house: dict[str, Any]) -> Devices:
        """The stored devices, or the house's starting state when none is stored."""
        devices = cls.fresh(house)
        if not path.exists():
            return devices
        stored = json.loads(path.read_text())
        for key, head in stored["heads"].items():
            devices.heads[key] = Head(**head)
        for key, room in stored["rooms"].items():
            devices.rooms[key] = Room(**room)
        return devices

    def save(self, path: Path) -> None:
        """Replace the file in one rename, so a kill never leaves half a file."""
        scratch = path.with_suffix(".tmp")
        scratch.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")
        os.replace(scratch, path)


def load_house(path: Path = HOUSE) -> dict[str, Any]:
    return json.loads(path.read_text())
