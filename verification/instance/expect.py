"""Expectations with deadlines over Home Assistant's event stream, and the checks
every journey runs. No Home Assistant import and no I/O: the suite tests this.

An event is the dict Home Assistant's WebSocket sends: ``event_type``,
``data``, ``time_fired`` and ``context`` (whose ``user_id`` is the person's for
a call made through the API, and null for a call MXZ makes itself).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

Event = dict[str, Any]
State = dict[str, Any]
DIRECTIONS = ("heat", "cool")
NEVER_SENT = ("auto", "heat_cool")


class Missed(AssertionError):
    """An expectation's deadline passed; carries what was last seen."""

    def __init__(self, what: str, seen: Any) -> None:
        super().__init__(f"{what}; last seen: {seen}")
        self.what = what
        self.seen = seen


@dataclass
class Stream:
    """Every event as it arrives, and the latest state of each entity."""

    events: list[Event] = field(default_factory=list)
    states: dict[str, State] = field(default_factory=dict)
    _changed: asyncio.Event | None = None

    def feed(self, event: Event) -> None:
        self.events.append(event)
        if event["event_type"] == "state_changed":
            data = event["data"]
            if data.get("new_state") is None:
                self.states.pop(data["entity_id"], None)
            else:
                self.states[data["entity_id"]] = data["new_state"]
        if self._changed is not None:
            self._changed.set()

    def mark(self) -> int:
        return len(self.events)

    def since(self, mark: int) -> list[Event]:
        return self.events[mark:]

    async def until(self, what: str, check: Callable[[dict[str, State]], Any], timeout: float) -> Any:
        """Wait until ``check(states)`` is truthy, or fail with the last state seen."""
        if self._changed is None:
            self._changed = asyncio.Event()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not (result := check(self.states)):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise Missed(f"{what} within {timeout:g} s", _brief(check, self.states))
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError:
                continue
        return result


def _brief(check: Callable[[dict[str, State]], Any], states: dict[str, State]) -> Any:
    entities = getattr(check, "entities", None)
    if not entities:
        return f"{len(states)} entities"
    return {eid: _state_brief(states.get(eid)) for eid in entities}


def _state_brief(state: State | None) -> Any:
    if state is None:
        return None
    return {"state": state["state"], **state.get("attributes", {})}


def watching(*entities: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Name the entities a check reads, so a miss reports their last state."""

    def mark(check: Callable[..., Any]) -> Callable[..., Any]:
        check.entities = entities  # type: ignore[attr-defined]
        return check

    return mark


def head_calls(events: Iterable[Event], heads: Iterable[str], *, by_person: bool | None = None) -> list[Event]:
    """Climate service calls aimed at a head. ``by_person`` False keeps only MXZ's."""
    heads = set(heads)
    calls = []
    for event in events:
        if event["event_type"] != "call_service" or event["data"].get("domain") != "climate":
            continue
        if not heads & set(_targets(event)):
            continue
        person = event.get("context", {}).get("user_id") is not None
        if by_person is None or by_person == person:
            calls.append(event)
    return calls


def _targets(event: Event) -> list[str]:
    target = event["data"].get("service_data", {}).get("entity_id", [])
    return [target] if isinstance(target, str) else list(target)


def commanded_mode(event: Event) -> str | None:
    """The mode a climate call puts a head in, or None for a call that sets no mode."""
    data = event["data"]
    if data["service"] in ("set_hvac_mode", "set_temperature"):
        return data.get("service_data", {}).get("hvac_mode")
    if data["service"] == "turn_off":
        return "off"
    return None


def opposite_directions(events: Iterable[Event], heads: Iterable[str]) -> list[str]:
    """Check 1: MXZ's latest command to one head is never heat while another's is cool."""
    latest: dict[str, str] = {}
    found = []
    for event in head_calls(events, heads, by_person=False):
        mode = commanded_mode(event)
        if mode is None:
            continue
        for head in _targets(event):
            latest[head] = mode
        running = {mode for mode in latest.values() if mode in DIRECTIONS}
        if len(running) > 1:
            found.append(f"{event['time_fired']}: MXZ has {dict(sorted(latest.items()))}")
    return found


def forbidden_modes(events: Iterable[Event], heads: Iterable[str], dry_allowed: Iterable[str] = ()) -> list[str]:
    """Check 2: MXZ never sends auto or heat_cool, and dry only where Allow Dry is on."""
    allowed = set(dry_allowed)
    found = []
    for event in head_calls(events, heads, by_person=False):
        mode = commanded_mode(event)
        for head in _targets(event):
            if mode in NEVER_SENT or (mode == "dry" and head not in allowed):
                found.append(f"{event['time_fired']}: MXZ sent {mode} to {head}")
    return found


_LOGGER_LINE = re.compile(r"^\S+ \S+ (?P<level>[A-Z]+) \((?P<thread>[^)]*)\) \[(?P<logger>[^\]]+)\] ")


def log_errors(text: str, names: Iterable[str]) -> list[str]:
    """Check 3: each ERROR or CRITICAL block, header and traceback, that names one of ``names``.

    The logger alone does not say whose error it is: Home Assistant logs an
    exception raised in an integration's listener under ``homeassistant.core``
    ("Error running job") and one in its task or timer under ``homeassistant``
    ("Error doing job"). The integration's name in the header or in a traceback
    frame (``custom_components/<name>/``) does.
    """
    names = tuple(names)
    blocks: list[str] = []
    inside = False
    for line in text.splitlines():
        if match := _LOGGER_LINE.match(line):
            inside = match["level"] in ("ERROR", "CRITICAL")
            if inside:
                blocks.append(line)
        elif inside:
            blocks[-1] += "\n" + line
    return [block for block in blocks if any(name in block for name in names)]
