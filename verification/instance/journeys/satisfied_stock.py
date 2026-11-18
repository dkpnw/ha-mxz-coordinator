"""The satisfied scene's stock arm: no MXZ; heads set to heat idle in heat at their setpoint.

A control on the model, not a product claim: it shows what rule 1 does once
every room is satisfied, and nothing about a real outdoor unit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from expect import head_calls, watching
from journeys import filmed
from journeys.satisfied import SETTLE, START, START_STEP, TARGET, WINDOW

if TYPE_CHECKING:
    from journeys.drive import Drive

FEATURE = "satisfied-stock"
JOURNEY = "heads-idle-in-heat"
OUTDOOR = "sensor.outdoor_unit"


async def run(drive: Drive) -> None:
    client = drive.client
    drive.check("no MXZ entry: the house as it was before an install", not await client.entries("mxz_coordinator"))
    await filmed.model_on(drive)
    for key in drive.rooms:
        await drive.at_head(f"last night: {drive.names[key]}'s head set to heat at {TARGET}", key,
                            hvac_mode="heat", target_temp_low=TARGET)
    began = await filmed.start(drive, START, START_STEP)
    marked = drive.stream.mark()
    await drive.until("every head heats its room; the outdoor unit runs heat", watching(*drive.heads, OUTDOOR)(
        lambda states: states[OUTDOOR]["state"] == "heat"
        and all(states[head]["attributes"].get("hvac_action") == "heating" for head in drive.heads)), SETTLE)
    await drive.until(f"every room reaches {TARGET} and its head idles in heat",
                      watching(*drive.heads, *drive.sensors)(lambda states: all(
                          float(states[sensor]["state"]) >= TARGET and states[head]["state"] == "heat"
                          and states[head]["attributes"].get("hvac_action") == "idle"
                          for head, sensor in zip(drive.heads, drive.sensors, strict=True))),
                      filmed.left(began, WINDOW - 30))
    await filmed.window_ends(drive, began, WINDOW)

    seen = drive.stream.states
    for head in drive.heads:
        drive.check(f"{head} is still set to heat and idles", seen[head]["state"] == "heat"
                    and seen[head]["attributes"].get("hvac_action") == "idle", seen[head]["attributes"])
    drive.check("the outdoor unit still runs heat", seen[OUTDOOR]["state"] == "heat", seen[OUTDOOR]["state"])
    left_heat = [e["data"]["new_state"]["state"] for e in drive.stream.since(marked)
                 if e["event_type"] == "state_changed" and e["data"]["entity_id"] == OUTDOOR
                 and e["data"]["new_state"]["state"] != "heat"]
    drive.check("negative control: the outdoor unit never left heat in the window", not left_heat, left_heat)
    calls = head_calls(drive.stream.events, drive.heads)
    drive.check("no climate service call reached a head: only the person at the remote changed them",
                not calls, [e["data"] for e in calls])
