"""The satisfied scene's coordinated arm: MXZ heats every room to 70, then turns every head off.

Needs setup-and-enable (5.1) to have run: one entry, every target 70, every room
and Coordinator enable on, nothing else changed. Every beat below is checked on
the run; one that does not happen is a finding, never a caption to soften.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from expect import commanded_mode, head_calls, watching
from journeys import filmed
from journeys.satisfied import SETTLE, START, START_STEP, TARGET, WINDOW

if TYPE_CHECKING:
    from journeys.drive import Drive

FEATURE = "satisfied-coordinated"
JOURNEY = "every-head-off-once-satisfied"
OUTDOOR = "sensor.outdoor_unit"


async def run(drive: Drive) -> None:
    ids = await drive.existing_entry()
    plan = ids["plan"]
    for slug in ("primary", "secondary", "zone_3"):
        drive.check(f"{slug} target is {TARGET} and the room is enabled",
                    float(drive.stream.states[ids[f"{slug}_target"]]["state"]) == TARGET
                    and drive.stream.states[ids[f"{slug}_enable"]]["state"] == "on")
    drive.check("Coordinator enable is on", drive.stream.states[ids["coordinator_enable"]]["state"] == "on")
    await filmed.model_on(drive)
    await filmed.parked(drive)

    began = await filmed.start(drive, START, START_STEP)
    marked = drive.stream.mark()
    await drive.until("the shared mode is heat and every head heats its room", watching(plan, *drive.heads)(
        lambda states: states[plan]["state"] == "heat"
        and all(states[head]["state"] == "heat" and states[head]["attributes"].get("hvac_action") == "heating"
                for head in drive.heads)), SETTLE)
    drive.record("MXZ", "shared mode heat; every head heats")
    await drive.until(f"every room reaches {TARGET} and MXZ turns its head off", watching(*drive.heads, *drive.sensors)(
        lambda states: all(float(states[sensor]["state"]) >= TARGET and states[head]["state"] == "off"
                           for head, sensor in zip(drive.heads, drive.sensors, strict=True))),
        filmed.left(began, WINDOW - 30))
    drive.record("MXZ", "every room at target; every head off")
    off = drive.stream.mark()
    await filmed.window_ends(drive, began, WINDOW)

    seen = drive.stream.states
    for head in drive.heads:
        drive.check(f"{head} is still off", seen[head]["state"] == "off", seen[head]["state"])
    drive.check("with no head set to heat or cool, the model's outdoor unit has no direction",
                seen[OUTDOOR]["state"] == "none", seen[OUTDOOR]["state"])
    sent = [commanded_mode(e) for e in head_calls(drive.stream.since(off), drive.heads, by_person=False)]
    drive.check("once every head is off, MXZ sends no head heat or cool for the rest of the window",
                not {"heat", "cool"} & set(sent), sent)
    cooled = {commanded_mode(e) for e in head_calls(drive.stream.since(marked), drive.heads, by_person=False)}
    drive.check("MXZ never sends cool in the window", "cool" not in cooled, sorted(map(str, cooled)))
    drive.transcript.notes["end_readings"] = {key: float(seen[f"sensor.{key}_temperature"]["state"]) for key in START}
    drive.transcript.notes["end_plan"] = seen[plan]["state"]
