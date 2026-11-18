"""The mode-wait scene: right after a heat run, Studio wants cool and waits out the mode-switch wait.

Needs setup-and-enable (5.1) to have run; every option at its default, so the
Minimum time between mode switches is 600 seconds and is not shortened. Before
the clip, Loft and Den read 66 and MXZ turns the shared mode to heat, heats them
to 70 and turns their heads off. The clip starts when Studio reads 76. The
shared mode stays heat until 600 seconds after the turn to heat, then turns to
cool, and Studio cools. Every beat is checked on the run.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from expect import commanded_mode, head_calls, watching
from journeys import filmed

if TYPE_CHECKING:
    from journeys.drive import Drive

FEATURE = "mode-wait"
JOURNEY = "studio-waits-out-the-mode-switch-wait"
TARGET = 70
MORNING = 66
HOT = 76
START_STEP = f"start: Studio reads {HOT}"
WAIT = 600  # Minimum time between mode switches, at its default
# Long enough for what is left of the wait after the heat run, plus Studio's first minutes of cooling.
WINDOW = 600
SETTLE = 60
HEATED = 8 * 60
# The clip needs some of the wait left to show; less than this is a finding about the setup.
SHOWN = 120
# The gate's end is published in whole seconds.
SLACK = 1.5


def when(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def first(events: list[dict[str, Any]], entity: str, state: str) -> dict[str, Any] | None:
    return next((e for e in events if e["event_type"] == "state_changed" and e["data"]["entity_id"] == entity
                 and e["data"]["new_state"]["state"] == state), None)


async def run(drive: Drive) -> None:
    ids = await drive.existing_entry()
    plan, thermostat, studio = ids["plan"], ids["primary_thermostat"], drive.heads[0]
    drive.check("Coordinator enable is on", drive.stream.states[ids["coordinator_enable"]]["state"] == "on")
    await filmed.model_on(drive)
    await filmed.parked(drive)

    for key in ("loft", "den"):
        await drive.room(f"{drive.names[key]} reads {MORNING}", key, temperature=MORNING)
    await drive.until("the shared mode turns to heat for Loft and Den", watching(plan)(
        lambda states: states[plan]["state"] == "heat"), SETTLE)
    await drive.until(f"Loft and Den reach {TARGET} and MXZ turns their heads off", watching(
        *drive.heads[1:], *drive.sensors[1:])(lambda states: all(
            float(states[sensor]["state"]) >= TARGET and states[head]["state"] == "off"
            for head, sensor in zip(drive.heads[1:], drive.sensors[1:], strict=True))), HEATED)
    hold = drive.stream.states[thermostat]["attributes"]
    drive.check(f"the wait since the turn to heat still has over {SHOWN} s to run",
                (hold.get("mode_change_hold_remaining") or 0) > SHOWN, hold.get("mode_change_hold_remaining"))
    ends = when(hold["mode_change_hold_until"])

    began = await filmed.start(drive, {"studio": HOT}, START_STEP)
    began_wall = time.time()
    marked = drive.stream.mark()
    await drive.until("Studio's row asks for cool", watching(plan)(
        lambda _: drive.row("Studio").get("demand") == "cool"), SETTLE)
    drive.check("the shared mode is still heat, and the plan says a mode change is not yet allowed",
                drive.stream.states[plan]["state"] == "heat"
                and drive.stream.states[plan]["attributes"].get("mode_change_allowed") is False,
                drive.stream.states[plan]["attributes"].get("mode_change_allowed"))
    await drive.until("once the wait is over, the shared mode turns to cool", watching(plan)(
        lambda states: states[plan]["state"] == "cool"), max(0.0, ends - time.time()) + SETTLE)
    drive.record("MXZ", "shared mode cool")
    await drive.until("Studio's head cools with the fan on high", watching(studio)(
        lambda states: states[studio]["state"] == "cool"
        and states[studio]["attributes"].get("fan_mode") == "high"), SETTLE)
    drive.record("MXZ", "Studio cools, fan high")
    await filmed.window_ends(drive, began, WINDOW)

    window = drive.stream.since(marked)
    turned = first(window, plan, "cool")
    drive.check(f"the shared mode stays heat until {WAIT} s after the turn to heat",
                turned is not None and when(turned["time_fired"]) >= ends - SLACK,
                {"turned": turned and turned["time_fired"], "wait_ends": hold["mode_change_hold_until"]})
    early = [e["data"] for e in head_calls(window, drive.heads, by_person=False)
             if commanded_mode(e) == "cool" and when(e["time_fired"]) < ends - SLACK]
    drive.check("MXZ sends no head cool before the wait is over", not early, early)
    shown = when(turned["time_fired"]) - began_wall
    drive.transcript.notes["seconds_from_start_to_cool"] = round(shown, 1)
    drive.check("the clip shows the turn to cool and a minute of cooling after it", shown <= WINDOW - SETTLE, shown)
