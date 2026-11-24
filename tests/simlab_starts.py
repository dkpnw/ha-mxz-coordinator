"""Starting points the simulator page does not list, for the tests that need them.

Each one shows MXZ behavior the tests pin (a coil drying, the 10-minute wait, a held fan, a
restart) and no difference from stock that a listed scenario does not already show, so
``world.PRESETS`` leaves them out. A test script run in the page's own process appends this
directory to ``sys.path`` after the bundle tree that holds ``world``.
"""

from __future__ import annotations

import json

import world
from world import Setting, Start

UNLISTED: dict[str, Start] = {
    # Every room starts cold and set to heat: both rows heat them all to 70 °F.
    "satisfied": Start(
        temps={"studio": 66.0, "loft": 66.0, "den": 66.0},
        settings={key: Setting("heat") for key in ("studio", "loft", "den")},
        stock_outdoor="heat", mode="heat", changed_ago=7200.0,
    ),
    # MXZ switched to heat a minute ago, so the 10-minute minimum holds it there.
    "mode_wait": Start(
        temps={"studio": 76.0, "loft": 70.0, "den": 70.0},
        settings={"studio": Setting("cool"), "loft": Setting("heat"), "den": Setting("heat")},
        stock_outdoor="heat", mode="heat", changed_ago=60.0,
    ),
    # Studio alone is on, and cools on a boosted fan.
    "fan_hold": Start(
        temps={"studio": 76.0, "loft": 70.0, "den": 70.0},
        settings={"studio": Setting("cool"), "loft": Setting("off"), "den": Setting("off")},
        stock_outdoor="cool", mode="cool", changed_ago=7200.0,
    ),
    # Home Assistant restarts at RESTART_AT, inside the wait after MXZ's first switch to cool.
    "restart": Start(
        temps={"studio": 76.0, "loft": 70.0, "den": 70.0},
        settings={"studio": Setting("cool"), "loft": Setting("heat"), "den": Setting("heat")},
        stock_outdoor="heat", mode="heat", changed_ago=7200.0,
    ),
}
RESTART_AT = 300.0


class RestartingWorld(world.World):
    """A world in which Home Assistant restarts once, at ``RESTART_AT``. The heads and rooms
    carry on. ``view`` adds how often it has restarted."""

    restarts = 0

    def advance(self, until: float) -> None:
        if self.now < RESTART_AT <= until:
            super().advance(RESTART_AT)
            self.row.restart()
            self.restarts += 1
        super().advance(until)

    def view(self) -> str:
        out = json.loads(super().view())
        out["mxz"]["restarts"] = self.restarts
        return json.dumps(out)


def load(name: str) -> world.World:
    """A world at the start of ``name``, listed or not."""
    if name in world.PRESETS:
        return world.load(name)
    return (RestartingWorld if name == "restart" else world.World)(UNLISTED[name])
