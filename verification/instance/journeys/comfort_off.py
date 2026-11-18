"""The comfort-priority scene with Den unoccupied: Studio, first in room order, wins the standoff."""

from __future__ import annotations

from typing import TYPE_CHECKING

from journeys import comfort

if TYPE_CHECKING:
    from journeys.drive import Drive

FEATURE = "comfort-off"
JOURNEY = "studio-wins-by-room-order"


async def run(drive: Drive) -> None:
    await comfort.run(drive, occupied=False)
