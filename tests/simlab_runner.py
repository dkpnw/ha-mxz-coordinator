"""Run a simulator parity script on the page's MXZ row, as the page loads it.

    python -I tests/simlab_runner.py <bundle tree>  < script.json  > snapshots.json

``<bundle tree>`` is ``tools/simulator_bundle.py --tree``'s output: the page's Python and the
integration's standard-library modules under an ``__init__.py`` that imports nothing. Every
``homeassistant`` import is refused, as the page has none. The script and the snapshots have the
shape ``tests/simlab_parity.py`` documents; that module runs the same script on the real
coordinator and compares.
"""

from __future__ import annotations

import importlib.abc
import json
import sys
from typing import Any


class _NoHomeAssistant(importlib.abc.MetaPathFinder):
    def find_spec(self, name: str, path: Any, target: Any = None) -> None:
        if name == "homeassistant" or name.startswith("homeassistant."):
            raise ImportError(f"the simulator page has no Home Assistant ({name})")


def main() -> int:
    sys.meta_path.insert(0, _NoHomeAssistant())
    sys.path.insert(0, sys.argv[1])
    import mxz_row
    from invented_house import devices as dv

    script = json.load(sys.stdin)
    house = dv.load_house()
    devices = dv.Devices.fresh(house)
    for key, temperature in script["temps"].items():
        devices.rooms[key].temperature = temperature
    for key, head in script["heads"].items():
        devices.heads[key] = dv.Head(**head)
    heads = mxz_row.InventedHeads(house, devices, modelled=True)
    settings = mxz_row.Settings(**script.get("page_settings", {}))
    row = mxz_row.MxzRow(heads, settings=settings, start=0.0, wall=script["wall"])
    names = {room["key"]: room["name"] for room in house["rooms"]}
    seen = {"calls": 0, "computes": 0}
    snapshots = []

    def snap(label: str, saved: dict[str, Any] | None = None) -> None:
        calls = row.log.calls[seen["calls"]:]
        computes = row.log.computes[seen["computes"]:]
        seen["calls"], seen["computes"] = len(row.log.calls), len(row.log.computes)
        plan = mxz_row.plan_view(row.plan)
        snapshots.append({
            "do": label,
            "t": round(row.now, 6),
            "plan": None if plan is None else {
                **{key: value for key, value in plan.items() if key != "rooms"},
                "rooms": [
                    {key: value for key, value in room.items() if key != "slug"}
                    for room in plan["rooms"]
                ],
            },
            "calls": [[key, service, data] for _t, key, service, data in calls],
            "computes": [[round(t, 6), why] for t, why in computes],
            "waits": row.pending_waits(),
            "mode": row.mode,
            "hold": round(row.hold_left(), 3),
            "heads": {
                key: [h.hvac_mode, h.target_temp_low, h.target_temp_high, h.fan_mode]
                for key, h in devices.heads.items()
            },
            "rooms": {
                room.key: {"target": room.target, "enable": room.enable, "fan_held": room.latch.holds}
                for room in row.rooms
            },
            **({"saved": saved} if saved is not None else {}),
        })

    row.start(script["saved"], names=names)
    snap("start")
    for step in script["steps"]:
        kind, *args = step
        saved = None
        if kind == "until":
            row.advance(args[0])
        elif kind == "reading":
            devices.rooms[args[0]].temperature = args[1]
            heads.changed()
            row.world_changed()
        elif kind == "readings":
            for key, temperature in args[0].items():
                devices.rooms[key].temperature = temperature
            heads.changed()
            row.world_changed()
        elif kind == "press":
            row.press(args[0], **args[1])
        elif kind == "target":
            row.set_target(args[0], args[1])
        elif kind == "enable":
            row.set_enable(args[0], args[1])
        elif kind == "comfort":
            row.set_comfort(args[0], args[1])
        elif kind == "fan_auto":
            row.set_fan_auto(args[0], args[1])
        elif kind == "restart":
            saved = row.restart()
        else:
            raise ValueError(f"unknown step {step!r}")
        snap(" ".join(str(arg) for arg in step), saved)
    json.dump(snapshots, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
