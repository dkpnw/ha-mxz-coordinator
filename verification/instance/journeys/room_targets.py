"""Feature map 5.3: one temperature per room; a run goes to the target, then coasts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from expect import head_calls, watching

if TYPE_CHECKING:
    from journeys.drive import Drive

FEATURE = "room-targets"
JOURNEY = "studio-cooling-run"
HEAD = "climate.studio_head"
SETTLE = 30
QUIET = 20


def head_is(mode: str | tuple[str, ...], low: float | None = None, high: float | None = None) -> Any:
    modes = (mode,) if isinstance(mode, str) else mode

    @watching(HEAD)
    def check(states: dict[str, Any]) -> bool:
        head = states.get(HEAD)
        if head is None or head["state"] not in modes:
            return False
        attributes = head["attributes"]
        return (low is None or attributes.get("target_temp_low") == low) and (
            high is None or attributes.get("target_temp_high") == high
        )

    return check


def studio_row(drive: Drive) -> dict[str, Any]:
    zones = drive.stream.states[drive.mxz("plan")]["attributes"].get("zones", [])
    return next(row for row in zones if row.get("name") == "Studio")


async def run(drive: Drive) -> None:
    ids = await drive.existing_entry()
    drive.check("Coordinator enable is on", drive.stream.states[ids["coordinator_enable"]]["state"] == "on")
    thermostat, target, enable = ids["primary_thermostat"], ids["primary_target"], ids["primary_enable"]

    await drive.person("set Studio's target to 72", "number", "set_value", entity_id=target, value=72)
    await drive.room("Studio reads 72", "studio", temperature=72)
    await drive.until("Studio is satisfied at 72", watching(ids["plan"])(
        lambda _: studio_row(drive).get("engage") == "satisfied"), SETTLE)

    await drive.person("set Studio's thermostat to 70", "climate", "set_temperature",
                       entity_id=thermostat, temperature=70)
    await drive.until("Studio's target number reads 70", watching(target)(
        lambda states: float(states[target]["state"]) == 70), SETTLE)
    await drive.until("the head cools with 68 and 70 (target - 2 to target)", head_is("cool", 68, 70), SETTLE)
    drive.check("Studio's row engages cool", studio_row(drive).get("engage") == "cool", studio_row(drive))

    await drive.room("Studio reads 70.5", "studio", temperature=70.5)
    await asyncio.sleep(5)
    drive.record("Time", "5 s pass")
    drive.check("still running: the head is cool", head_is("cool")(drive.stream.states),
                drive.stream.states[HEAD]["state"])

    await drive.room("Studio reads 70", "studio", temperature=70)
    await drive.until("Studio is satisfied and its head parked", watching(HEAD, ids["plan"])(
        lambda states: studio_row(drive).get("engage") == "satisfied"
        and states[HEAD]["state"] in ("fan_only", "off")), SETTLE)

    await drive.quiet()
    mark = drive.stream.mark()
    await drive.room("Studio reads 70.5", "studio", temperature=70.5)
    await asyncio.sleep(QUIET)
    drive.record("Time", f"{QUIET} s pass inside the drift")
    sent = head_calls(drive.stream.since(mark), [HEAD], by_person=False)
    drive.check("inside the 1 °F drift: no run, and MXZ sends that head nothing", not sent,
                [event["data"] for event in sent])

    await drive.person("set Studio's target number to 69", "number", "set_value", entity_id=target, value=69)
    await drive.until("the thermostat shows 69", watching(thermostat)(
        lambda states: states[thermostat]["attributes"].get("temperature") == 69), SETTLE)
    await drive.until("a run starts: the head cools with 67 and 69", head_is("cool", 67, 69), SETTLE)

    await drive.person("turn Studio's thermostat off", "climate", "set_hvac_mode",
                       entity_id=thermostat, hvac_mode="off")
    await drive.until("Studio's enable switch is off and its head is off", watching(enable, HEAD)(
        lambda states: states[enable]["state"] == "off" and states[HEAD]["state"] == "off"), SETTLE)
    row = studio_row(drive)
    drive.check("Studio's row has no start points", row.get("heat_start") is None and row.get("cool_start") is None,
                row)

    await drive.person("set Studio's thermostat back to heat_cool", "climate", "set_hvac_mode",
                       entity_id=thermostat, hvac_mode="heat_cool")
    await drive.until("the enable switch is on and the run resumes", watching(enable, HEAD)(
        lambda states: states[enable]["state"] == "on" and states[HEAD]["state"] == "cool"), SETTLE)

    mxz_calls = head_calls(drive.stream.events, drive.heads, by_person=False)
    person_calls = head_calls(drive.stream.events, drive.heads, by_person=True)
    drive.transcript.notes["context_control"] = {"mxz_head_calls": len(mxz_calls), "person_head_calls": len(person_calls)}
    drive.check("negative control: MXZ's head calls carry no user, and the person made none",
                bool(mxz_calls) and not person_calls, {"mxz": len(mxz_calls), "person": len(person_calls)})

    await drive.room("Studio reads 70", "studio", temperature=70)
    await drive.person("set Studio's target back to 70", "number", "set_value", entity_id=target, value=70)
    await drive.until("Studio is satisfied again", watching(ids["plan"])(
        lambda _: studio_row(drive).get("engage") == "satisfied"), SETTLE)
