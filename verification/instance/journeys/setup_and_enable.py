"""Feature map 5.1: a person sets MXZ up, and nothing is commanded until they turn it on."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from expect import head_calls, watching
from journeys.drive import COOLDOWN, head_is

if TYPE_CHECKING:
    from journeys.drive import Drive

FEATURE = "setup-and-enable"
JOURNEY = "setup-flow-then-enable"
QUIET = 60
SETTLE = 30
EVALUATED = COOLDOWN + 10


async def run(drive: Drive) -> None:
    client = drive.client
    drive.check("fresh instance: equipment loaded, no MXZ entry", not await client.entries("mxz_coordinator"))

    flow = await client.flow_start("mxz_coordinator")
    refused = await client.flow_configure(flow["flow_id"], {"heads": [drive.heads[0]]})
    drive.record("Person in Home Assistant", "setup flow with one head, not generic")
    drive.check("one head is refused with need_two_heads",
                refused.get("errors", {}).get("base") == "need_two_heads", refused.get("errors"))
    await client.flow_abort(flow["flow_id"])
    drive.check("the refused flow created no entry", not await client.entries("mxz_coordinator"))

    flow = await client.flow_start("mxz_coordinator")
    pages = [flow]
    pages.append(await client.flow_configure(flow["flow_id"], {"heads": drive.heads}))
    pages.append(await client.flow_configure(flow["flow_id"], {
        f"room_name_{index + 1}": drive.names[key] for index, key in enumerate(drive.rooms)
    }))
    pages.append(await client.flow_configure(flow["flow_id"], {
        f"sensor_{index + 1}": sensor for index, sensor in enumerate(drive.sensors)
    }))
    created_at = drive.stream.mark()
    pages.append(await client.flow_configure(flow["flow_id"], {"next_step_id": "finish"}))
    drive.transcript.attach("flow.json", pages)
    drive.record("Person in Home Assistant",
                 "setup flow: heads Studio, Loft, Den in that order, room names, one sensor each, Save and finish")
    steps = [page.get("step_id") or page.get("type") for page in pages]
    drive.check("the flow walks user, rooms, sensors, review, then creates the entry",
                steps == ["user", "rooms", "sensors", "review", "create_entry"], steps)
    entry = pages[-1]["result"]
    drive.entry_id = entry["entry_id"]
    stored = (await client.entries("mxz_coordinator"))[0]
    drive.check("one entry, loaded", stored["state"] == "loaded", stored["state"])

    ids = await drive.find_entities()
    drive.transcript.attach("entities.json", ids)
    wanted = [f"{slug}_{key}" for slug in ("primary", "secondary", "zone_3") for key in
              ("thermostat", "target", "drift", "enable", "fan_auto")]
    wanted += ["plan", "shared_mode", "coordinator_enable"]
    drive.check("each room has a thermostat, target, drift, enable and Fan auto; the entry has plan, "
                "shared mode and Coordinator enable", not (missing := [k for k in wanted if k not in ids]), missing)
    rows = await drive.until("the plan lists three rooms in the order given", watching(ids["plan"])(
        lambda states: [row.get("name") for row in states.get(ids["plan"], {}).get("attributes", {}).get("zones", [])]
        == [drive.names[key] for key in drive.rooms]), 30)
    drive.check("rooms in priority order", bool(rows))
    plan = drive.stream.states[ids["plan"]]["state"]
    drive.check("the plan reads cool, the fresh-install mode", plan == "cool", plan)

    for slug, key in zip(("primary", "secondary", "zone_3"), drive.rooms, strict=True):
        await drive.person(f"set {drive.names[key]}'s target to 70", "number", "set_value",
                           entity_id=ids[f"{slug}_target"], value=70)
        await drive.person(f"enable {drive.names[key]}", "switch", "turn_on", entity_id=ids[f"{slug}_enable"])
    studio, name, head = drive.rooms[0], drive.names[drive.rooms[0]], drive.heads[0]
    await drive.room(f"{name} reads 72, past its 1 °F drift", studio, temperature=72)
    await drive.until(f"with Coordinator enable off, MXZ evaluates {name} at 72 and wants cool",
                      watching(ids["plan"])(lambda _: drive.row(name).get("temp") == 72
                                            and drive.row(name).get("demand") == "cool"), EVALUATED)
    await drive.quiet()
    sent = head_calls(drive.stream.since(created_at), drive.heads)
    drive.check("from entry creation until Coordinator enable is on, no head gets any call, "
                f"though enabled {name} wants cool", not sent, [e["data"] for e in sent])

    await drive.person("turn on Coordinator enable", "switch", "turn_on", entity_id=ids["coordinator_enable"])
    await drive.until(f"positive control: {name}'s head is sent cool with 68 and 70",
                      head_is(head, "cool", 68, 70), SETTLE)

    await drive.room(f"{name} reads 70", studio, temperature=70)
    await drive.until(f"{name} is satisfied and its head parked", watching(head, ids["plan"])(
        lambda states: drive.row(name).get("engage") == "satisfied"
        and states[head]["state"] in ("fan_only", "off")), SETTLE)
    await drive.quiet()
    at_target = drive.stream.mark()
    await asyncio.sleep(QUIET)
    drive.record("Time", f"{QUIET} s pass with every room at target")
    after = head_calls(drive.stream.since(at_target), drive.heads)
    directed = [e["data"] for e in after
                if e["data"].get("service_data", {}).get("hvac_mode") in ("heat", "cool")]
    drive.check("with every room at target, no head is sent heat or cool", not directed, directed)
    person_users = {e["context"]["user_id"] for e in drive.stream.events
                    if e["event_type"] == "call_service" and e["data"]["domain"] in ("number", "switch")}
    drive.transcript.notes["context_control"] = {
        "person_calls_user_ids": sorted(map(str, person_users)),
        "mxz_head_calls": len(head_calls(drive.stream.events, drive.heads, by_person=False)),
        "person_head_calls": len(head_calls(drive.stream.events, drive.heads, by_person=True)),
    }
    drive.check("negative control: the person's calls carry the instance user",
                person_users == {client.user_id}, sorted(map(str, person_users)))
