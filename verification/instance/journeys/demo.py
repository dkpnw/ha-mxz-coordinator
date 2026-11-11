"""What both arms of the README demo share (feature map section 6).

One afternoon shown twice. Loft and Den sit at their 70 °F targets after a
morning of heating, and the sun has warmed Studio to 76 °F. The stock arm and
the coordinated arm start from these numbers, run the same model (model.py) on
the same real clock, and are filmed for the same window from their start step.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from journeys.drive import Drive

TARGET = 70
START = {"studio": 76, "loft": 70, "den": 70}
# Long enough for Studio to cool 6 °F at the model's 1 °F a minute, with MXZ's
# own delays and a held last frame.
WINDOW = 450
START_STEP = "start: Studio reads 76, Loft and Den read 70"


async def model_on(drive: Drive) -> None:
    await drive.client.call("invented_house", "model", running=True)
    drive.record("Driver", "turn on the demo's outdoor unit and room model")


async def start(drive: Drive) -> float:
    """Set the start temperatures in one step; return the window's start, on the loop clock."""
    for key, temperature in START.items():
        await drive.client.call("invented_house", "room", room=key, temperature=temperature)
    drive.record("Room", START_STEP)
    return asyncio.get_running_loop().time()
