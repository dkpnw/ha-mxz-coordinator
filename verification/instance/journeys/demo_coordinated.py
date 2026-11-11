"""The README demo's coordinated arm: MXZ at its defaults turns the system to cool for Studio.

Needs setup-and-enable (5.1) to have run: one entry, every target 70, every room
and Coordinator enable on, nothing else changed. Every beat below is checked on
the run; one that does not happen is a finding, never a caption to soften.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from expect import commanded_mode, head_calls, watching
from journeys.demo import START, TARGET, WINDOW, model_on, start

if TYPE_CHECKING:
    from journeys.drive import Drive

FEATURE = "demo-coordinated"
JOURNEY = "studio-cools-while-loft-and-den-rest"
MORNING = 66
# The model heats 4 °F at 1 °F a minute; MXZ's own delays come on top.
HEATED = 8 * 60
# Minimum time between mode switches, at its default, plus margin.
WAIT = 600 + 60
SETTLE = 60
LADDER = ["high", "middle", "medium", "low", "quiet"]


def runs(values: list[str]) -> list[str]:
    """``values`` with each run of repeats kept once."""
    return [value for index, value in enumerate(values) if index == 0 or values[index - 1] != value]


async def run(drive: Drive) -> None:
    ids = await drive.existing_entry()
    plan, studio_head = ids["plan"], drive.heads[0]
    names = [drive.names[key] for key in drive.rooms]
    for slug in ("primary", "secondary", "zone_3"):
        drive.check(f"{slug} target is {TARGET} and the room is enabled",
                    float(drive.stream.states[ids[f"{slug}_target"]]["state"]) == TARGET
                    and drive.stream.states[ids[f"{slug}_enable"]]["state"] == "on")
    drive.check("Coordinator enable is on", drive.stream.states[ids["coordinator_enable"]]["state"] == "on")
    await model_on(drive)

    # Before the clip: a cold morning, so the system has been heating.
    for key in ("loft", "den"):
        await drive.room(f"this morning {drive.names[key]} reads {MORNING}", key, temperature=MORNING)
    await drive.until("the plan turns to heat for Loft and Den", watching(plan)(
        lambda states: states[plan]["state"] == "heat"), WAIT)
    await drive.until("Loft and Den reach 70 and MXZ turns their heads off (idle after heating)", watching(
        *drive.heads[1:], *drive.sensors[1:])(lambda states: all(
            float(states[sensor]["state"]) >= TARGET and states[head]["state"] == "off"
            for head, sensor in zip(drive.heads[1:], drive.sensors[1:], strict=True))), HEATED)
    await drive.until("the mode-change wait since the turn to heat is over", watching(plan)(
        lambda states: states[plan]["attributes"].get("mode_change_allowed") is True), WAIT)

    began = await start(drive)
    marked = drive.stream.mark()
    await drive.until("the shared mode changes from heat to cool, and Studio's head cools with the fan on high",
                      watching(plan, studio_head)(lambda states: states[plan]["state"] == "cool"
                                                  and states[studio_head]["state"] == "cool"
                                                  and states[studio_head]["attributes"].get("fan_mode") == "high"),
                      SETTLE)
    drive.record("MXZ", "shared mode cool; Studio cools, fan high")
    await drive.until(f"Studio reaches {TARGET} and its head stops cooling (dries the coil with the fan)",
                      watching("sensor.studio_temperature", studio_head)(
                          lambda states: float(states["sensor.studio_temperature"]["state"]) <= TARGET
                          and states[studio_head]["state"] == "fan_only"),
                      max(0.0, began + WINDOW - 10 - asyncio.get_running_loop().time()))
    drive.record("MXZ", "Studio at target; its head dries the coil")
    await asyncio.sleep(max(0.0, began + WINDOW - asyncio.get_running_loop().time()))
    drive.record("Time", f"the window ends {WINDOW} s after the start")

    window = drive.stream.since(marked)
    fans = runs([e["data"]["new_state"]["attributes"].get("fan_mode") for e in window
                 if e["event_type"] == "state_changed" and e["data"]["entity_id"] == studio_head
                 and e["data"]["new_state"]["state"] == "cool"])
    drive.transcript.notes["studio_fan_while_cooling"] = fans
    # The mode goes out before the fan, so the first cool report can carry the old speed.
    boosted = fans[fans.index("high"):] if "high" in fans else fans
    drive.check("from high, Studio's fan steps down middle, medium, low, quiet as it closes in",
                boosted == LADDER, fans)
    sent = {commanded_mode(e) for e in head_calls(window, drive.heads[1:], by_person=False)} - {None}
    drive.check("Loft's and Den's heads are never sent heat or cool in the window", not sent & {"heat", "cool"}, sent)
    for head in drive.heads[1:]:
        drive.check(f"{head} stays off", drive.stream.states[head]["state"] == "off", drive.stream.states[head]["state"])
    rows = {row["name"]: row for row in drive.stream.states[plan]["attributes"].get("zones", [])}
    drive.check("the plan lists the three rooms", list(rows) == names, list(rows))
    readings = {key: float(drive.stream.states[f"sensor.{key}_temperature"]["state"]) for key in START}
    drive.transcript.notes["end_readings"] = readings
