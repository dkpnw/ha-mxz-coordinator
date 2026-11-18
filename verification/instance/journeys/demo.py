"""What both arms of the README demo share (feature map section 6).

One afternoon shown twice. Loft and Den sit at their 70 °F targets after a
morning of heating, and the sun has warmed Studio to 76 °F. The stock arm and
the coordinated arm start from these numbers, run the same model (model.py) on
the same real clock, and are filmed for the same window from their start step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from journeys import filmed
from journeys.filmed import model_on

if TYPE_CHECKING:
    from journeys.drive import Drive

__all__ = ["START", "START_STEP", "TARGET", "WINDOW", "model_on", "start"]

TARGET = 70
START = {"studio": 76, "loft": 70, "den": 70}
# Long enough for Studio to cool 6 °F at the model's 1 °F a minute, with MXZ's
# own delays and a held last frame.
WINDOW = 450
START_STEP = "start: Studio reads 76, Loft and Den read 70"


async def start(drive: Drive) -> float:
    """Set the start temperatures in one step; return the window's start, on the loop clock."""
    return await filmed.start(drive, START, START_STEP)
