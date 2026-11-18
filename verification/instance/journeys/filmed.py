"""What every filmed demo journey (scenes.py) does the same way.

The model goes on before the story, the start step sets the start temperatures
in one recorded step that compose.py lines the rows up on, and the window ends
a fixed time after it. A coordinated journey also waits, before its start, until
MXZ has every head off, so no clip opens on the coil dry setup-and-enable leaves.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from expect import watching

if TYPE_CHECKING:
    from journeys.drive import Drive

# Dry the coil after cooling runs 10 minutes by default; setup-and-enable ends inside one.
PARKED = 12 * 60


async def model_on(drive: Drive) -> None:
    await drive.client.call("invented_house", "model", running=True)
    drive.record("Driver", "turn on the demo's outdoor unit and room model")


async def parked(drive: Drive) -> None:
    await drive.until("before the start, MXZ has every head off", watching(*drive.heads)(
        lambda states: all(states[head]["state"] == "off" for head in drive.heads)), PARKED)


async def start(drive: Drive, temperatures: dict[str, float], step: str) -> float:
    """Set the start temperatures in one step; return the window's start, on the loop clock."""
    for key, temperature in temperatures.items():
        await drive.client.call("invented_house", "room", room=key, temperature=temperature)
    drive.record("Room", step)
    return asyncio.get_running_loop().time()


def left(began: float, window: float) -> float:
    """Seconds until the window ends; 0 once it has."""
    return max(0.0, began + window - asyncio.get_running_loop().time())


async def window_ends(drive: Drive, began: float, window: float) -> None:
    await asyncio.sleep(left(began, window))
    drive.record("Time", f"the window ends {window:g} s after the start")
