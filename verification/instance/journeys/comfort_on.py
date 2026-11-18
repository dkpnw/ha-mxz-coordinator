"""The comfort-priority scene with Den occupied: Den's Comfort priority wins the standoff."""

from __future__ import annotations

from typing import TYPE_CHECKING

from journeys import comfort

if TYPE_CHECKING:
    from journeys.drive import Drive

FEATURE = "comfort-on"
JOURNEY = "den-wins-by-comfort-priority"


async def run(drive: Drive) -> None:
    await comfort.run(drive, occupied=True)
