"""The fan-hold scene: a fan speed a person picks at the head is kept for the rest of the run.

Needs setup-and-enable (5.1) to have run. The hook's afternoon: Studio reads 76
against 70, Loft and Den 70. MXZ cools Studio with the fan on high; then a
person picks quiet on Studio's remote. From then on MXZ sends Studio no fan
speed: the room's Fan auto switch turns off and the head stays on quiet to the
end, through the coil dry. Every beat is checked on the run.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from expect import head_calls, watching
from journeys import filmed
from journeys.demo import START, START_STEP, TARGET

if TYPE_CHECKING:
    from journeys.drive import Drive

FEATURE = "fan-hold"
JOURNEY = "a-picked-fan-speed-is-kept"
WINDOW = 450
SETTLE = 60
PICK = "quiet"
# Seconds after MXZ's high before the person picks: inside MXZ's Confirm wait (90 s).
LATER = 30
# The pick is noticed when the Confirm wait of MXZ's fan write ends.
NOTICED = 90 + SETTLE


def sends_fan(event: dict[str, Any]) -> bool:
    data = event["data"]
    return data["service"] == "set_fan_mode" or "fan_mode" in data.get("service_data", {})


async def run(drive: Drive) -> None:
    ids = await drive.existing_entry()
    plan, studio, fan_auto = ids["plan"], drive.heads[0], ids["primary_fan_auto"]
    drive.check("Coordinator enable is on", drive.stream.states[ids["coordinator_enable"]]["state"] == "on")
    drive.check("Studio's Fan auto is on", drive.stream.states[fan_auto]["state"] == "on")
    await filmed.model_on(drive)
    await filmed.parked(drive)

    began = await filmed.start(drive, START, START_STEP)
    await drive.until("the shared mode is cool, and Studio's head cools with the fan on high",
                      watching(plan, studio)(lambda states: states[plan]["state"] == "cool"
                                             and states[studio]["state"] == "cool"
                                             and states[studio]["attributes"].get("fan_mode") == "high"), SETTLE)
    drive.record("MXZ", "shared mode cool; Studio cools, fan high")
    await asyncio.sleep(LATER)
    picked = drive.stream.mark()
    await drive.at_head(f"pick {PICK} on Studio's remote", "studio", fan_mode=PICK)
    await drive.until("MXZ holds the pick: Studio's Fan auto turns off and its row shows a fan hold",
                      watching(fan_auto, plan)(lambda states: states[fan_auto]["state"] == "off"
                                               and drive.row("Studio").get("fan_hold") is True), NOTICED)
    drive.record("MXZ", f"Studio's fan held at {PICK}")
    await drive.until(f"Studio reaches {TARGET}", watching("sensor.studio_temperature")(
        lambda states: float(states["sensor.studio_temperature"]["state"]) <= TARGET), filmed.left(began, WINDOW - 30))
    # The model then lets the room drift up a few thousandths a tick, so the park is its own beat.
    await drive.until("then its head stops cooling and dries the coil with the fan", watching(studio)(
        lambda states: states[studio]["state"] == "fan_only"), SETTLE)
    drive.record("MXZ", "Studio at target; its head dries the coil")
    await filmed.window_ends(drive, began, WINDOW)

    before = [e["data"] for e in head_calls(drive.stream.events[:picked], [studio], by_person=False) if sends_fan(e)]
    drive.check("negative control: before the pick, MXZ did send Studio's head a fan speed", bool(before), before)
    after = [e["data"] for e in head_calls(drive.stream.since(picked), [studio], by_person=False) if sends_fan(e)]
    drive.check("after the pick, MXZ sends Studio's head no fan speed", not after, after)
    fans = sorted({e["data"]["new_state"]["attributes"].get("fan_mode") for e in drive.stream.since(picked)
                   if e["event_type"] == "state_changed" and e["data"]["entity_id"] == studio})
    drive.check(f"from the pick to the end, Studio's head shows only {PICK}", fans == [PICK], fans)
    seen = drive.stream.states
    drive.check("Studio's Fan auto is still off at the end", seen[fan_auto]["state"] == "off", seen[fan_auto]["state"])
