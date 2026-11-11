"""What a journey acts and looks through: named actors, MXZ's entities, expectations.

Knows MXZ's entities and nothing about processes or sockets: the client, the
event stream and the transcript are handed in.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from expect import (
    Missed,
    Stream,
    forbidden_modes,
    head_calls,
    log_errors,
    opposite_directions,
)

if TYPE_CHECKING:
    from client import Client
    from evidence import Transcript

DOMAIN = "mxz_coordinator"
SLUGS = ("primary", "secondary", "zone_3")
ROOM_KEYS = ("thermostat", "target", "drift", "enable", "fan_auto")
ENTRY_KEYS = ("plan", "shared_mode", "coordinator_enable")
ROW_KEYS = (
    "name", "demand", "engage", "temp", "target", "fan_hold", "opening_state",
    "heat_start", "cool_start",
)
PLAN_KEYS = ("standoff", "mode_change_allowed", "paused_rooms")
TILE_KEYS = ("hvac_action", "mode_change_hold_remaining", "mode_change_hold_until")
LOGGERS = ("custom_components.mxz_coordinator", "custom_components.invented_house")


class Drive:
    def __init__(self, client: Client, stream: Stream, transcript: Transcript, house: dict[str, Any]) -> None:
        self.client = client
        self.stream = stream
        self.transcript = transcript
        self.rooms = [room["key"] for room in house["rooms"]]
        self.names = {room["key"]: room["name"] for room in house["rooms"]}
        self.heads = [f"climate.{key}_head" for key in self.rooms]
        self.sensors = [f"sensor.{key}_temperature" for key in self.rooms]
        self.entry_id: str | None = None
        self._ids: dict[str, str] = {}
        self._mark = 0

    async def existing_entry(self) -> dict[str, str]:
        """The one MXZ entry an earlier journey set up, and its entities."""
        entries = await self.client.entries(DOMAIN)
        self.check("journey 5.1 has run: one MXZ entry", len(entries) == 1, len(entries))
        self.entry_id = entries[0]["entry_id"]
        return await self.find_entities()

    async def find_entities(self) -> dict[str, str]:
        """MXZ's entity ids by unique-id key, since entity ids follow names."""
        prefix = f"{self.entry_id}_"
        self._ids = {
            entity["unique_id"].removeprefix(prefix): entity["entity_id"]
            for entity in await self.client.entities()
            if entity["platform"] == DOMAIN and entity["unique_id"].startswith(prefix)
        }
        return self._ids

    def mxz(self, key: str) -> str:
        return self._ids[key]

    def shown(self) -> dict[str, Any]:
        states = self.stream.states

        def attrs(eid: str | None, keys: tuple[str, ...]) -> dict[str, Any] | None:
            if eid is None or eid not in states:
                return None
            attributes = states[eid]["attributes"]
            return {"state": states[eid]["state"], **{k: attributes.get(k) for k in keys if k in attributes}}

        plan = self._ids.get("plan")
        shown: dict[str, Any] = {"heads": {}, "rooms": {}}
        if plan and plan in states:
            attributes = states[plan]["attributes"]
            shown["plan"] = {"state": states[plan]["state"], **{k: attributes.get(k) for k in PLAN_KEYS}}
            shown["plan"]["rows"] = [
                {k: row.get(k) for k in ROW_KEYS} for row in attributes.get("zones", [])
            ]
        for slug, key in zip(SLUGS, self.rooms, strict=False):
            shown["rooms"][key] = {
                "thermostat": attrs(self._ids.get(f"{slug}_thermostat"), TILE_KEYS),
                "enable": attrs(self._ids.get(f"{slug}_enable"), ()),
                "fan_auto": attrs(self._ids.get(f"{slug}_fan_auto"), ()),
                "target": attrs(self._ids.get(f"{slug}_target"), ()),
                "temperature": attrs(f"sensor.{key}_temperature", ()),
            }
        for head in self.heads:
            shown["heads"][head] = attrs(head, ("fan_mode", "target_temp_low", "target_temp_high"))
        return shown

    def _calls(self) -> list[dict[str, Any]]:
        events = self.stream.since(self._mark)
        self._mark = self.stream.mark()
        return [
            {
                "service": f"{e['data']['domain']}.{e['data']['service']}",
                "data": e["data"].get("service_data"),
                "by": "person" if e.get("context", {}).get("user_id") else "mxz",
                "to_head": bool(head_calls([e], self.heads)),
                "time": e["time_fired"],
            }
            for e in events
            if e["event_type"] == "call_service"
        ]

    def record(self, actor: str, action: str) -> None:
        self.transcript.step(actor, action, self.shown(), self._calls())

    async def person(self, action: str, domain: str, service: str, **data: Any) -> None:
        """A person in Home Assistant: a service call on an entity."""
        await self.client.call(domain, service, **data)
        self.record("Person in Home Assistant", action)

    async def room(self, action: str, key: str, **fields: Any) -> None:
        """The room: a temperature report or a door/window reading."""
        await self.client.call("invented_house", "room", room=key, **fields)
        self.record("Room", action)

    async def at_head(self, action: str, key: str, **fields: Any) -> None:
        """A person at the head's remote: the head changes and reports it."""
        await self.client.call("invented_house", "remote", head=key, **fields)
        self.record("Person at the head", action)

    def check(self, what: str, passed: bool, seen: Any = None) -> None:
        if not self.transcript.steps:
            self.record("Driver", "preconditions")
        self.transcript.expect(what, passed, seen)
        if not passed:
            raise Missed(what, seen)

    async def until(self, what: str, check: Any, timeout: float) -> Any:
        if not self.transcript.steps:
            self.record("Driver", "preconditions")
        try:
            result = await self.stream.until(what, check, timeout)
        except Missed as missed:
            self.transcript.expect(what, False, missed.seen)
            raise
        self.transcript.expect(what, True)
        return result

    async def quiet(self, seconds: float = 5, timeout: float = 60) -> None:
        """Wait until MXZ has sent the heads nothing for ``seconds``, so a silence window starts clean."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            mark = self.stream.mark()
            await asyncio.sleep(seconds)
            if not head_calls(self.stream.since(mark), self.heads, by_person=False):
                break
            if loop.time() > deadline:
                self.check(f"MXZ goes quiet for {seconds:g} s within {timeout:g} s", False)
        self.record("Time", f"MXZ sends the heads nothing for {seconds:g} s")

    def every_journey_checks(self, log: str, dry_allowed: tuple[str, ...] = ()) -> None:
        """Section 4's three checks, over the whole recorded window."""
        self.record("Driver", "checks over the whole journey")
        events = self.stream.events
        self.check("MXZ never commands opposite directions", not (found := opposite_directions(events, self.heads)), found)
        self.check("MXZ never sends auto or heat_cool, nor dry without Allow Dry",
                   not (found := forbidden_modes(events, self.heads, dry_allowed)), found)
        self.check("no error or traceback from MXZ or the equipment", not (found := log_errors(log, LOGGERS)), found)
