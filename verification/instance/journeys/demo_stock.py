"""The README demo's stock arm: no MXZ, a person works the heads, and Studio waits.

A control on the model, not a product claim. It shows the one rule the model
takes from Mitsubishi's manual, and nothing about how long a real head waits.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from expect import head_calls, watching
from journeys.demo import START, TARGET, WINDOW, model_on, start

if TYPE_CHECKING:
    from journeys.drive import Drive

FEATURE = "demo-stock"
JOURNEY = "studio-waits-behind-heat"
SETTLE = 30


async def run(drive: Drive) -> None:
    client = drive.client
    drive.check("no MXZ entry: the house as it was before an install", not await client.entries("mxz_coordinator"))
    studio, loft, den = drive.heads
    await model_on(drive)
    for key in ("loft", "den"):
        await drive.room(f"{drive.names[key]} reads {START[key]}", key, temperature=START[key])
        await drive.at_head(f"this morning: {drive.names[key]}'s head set to heat at {TARGET}", key,
                            hvac_mode="heat", target_temp_low=TARGET)
    await drive.until("Loft and Den sit idle in heat at their setpoint; the outdoor unit runs heat",
                      watching(loft, den, "sensor.outdoor_unit")(lambda states: all(
                          states[head]["state"] == "heat" and states[head]["attributes"].get("hvac_action") == "idle"
                          for head in (loft, den)) and states["sensor.outdoor_unit"]["state"] == "heat"), SETTLE)

    began = await start(drive)
    await drive.at_head(f"Studio's head set to cool at {TARGET}", "studio", hvac_mode="cool", target_temp_high=TARGET)
    await drive.until("Studio's head shows cool and waits in standby behind heat", watching(studio)(
        lambda states: states[studio]["state"] == "cool" and states[studio]["attributes"].get("standby") is True),
        SETTLE)
    marked = drive.stream.mark()
    await asyncio.sleep(max(0.0, began + WINDOW - asyncio.get_running_loop().time()))
    drive.record("Time", f"the window ends {WINDOW} s after the start")

    seen = drive.stream.states
    reading = float(seen["sensor.studio_temperature"]["state"])
    drive.check("at the end Studio still reads its start temperature, 76", reading >= START["studio"] - 0.1, reading)
    drive.check("Studio's head still waits in standby", seen[studio]["attributes"].get("standby") is True,
                seen[studio]["attributes"])
    drive.check("the outdoor unit still runs heat", seen["sensor.outdoor_unit"]["state"] == "heat",
                seen["sensor.outdoor_unit"]["state"])
    changed = [e for e in drive.stream.since(marked) if e["event_type"] == "state_changed"
               and e["data"]["entity_id"] == studio and e["data"]["new_state"]["attributes"].get("standby") is False]
    drive.check("negative control: Studio's head never left standby in the window", not changed, len(changed))
    calls = head_calls(drive.stream.events, drive.heads)
    drive.check("no climate service call reached a head: only the person at the remote changed them",
                not calls, [e["data"] for e in calls])
