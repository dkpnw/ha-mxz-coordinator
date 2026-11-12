"""What every controller split out of the coordinator reaches it for.

Each controller declares its own Protocol on top of this one, naming the rest of
the coordinator members it uses. mypy checks that the coordinator satisfies each.
``HeadCommands`` is the part of the delivery queue a lower controller sends through.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from homeassistant.core import HomeAssistant


class ControllerHost(Protocol):
    """Home Assistant, the clock, and the coordinator's notices, redraws and recomputes.

    ``retired``: the coordinator was unloaded or replaced, so it writes nothing.
    ``redraw_rooms`` re-renders the room surfaces with no decision.
    ``async_refresh_now`` recomputes at once; ``async_request_refresh`` asks for
    the ordinary debounced recompute.
    """

    hass: HomeAssistant
    clock: Callable[[], float]
    retired: bool

    async def notify(self, message: str, target: str | None = None) -> None: ...
    def redraw_rooms(self) -> None: ...
    async def async_refresh_now(self) -> None: ...
    async def async_request_refresh(self) -> None: ...


class HeadCommands(Protocol):
    """Sends a head one climate service call, in turn with MXZ's own commands to it.

    ``manual``: a person's command, which disowns what MXZ's running command
    to that head has not sent yet. A head's refusal raises ``HomeAssistantError``.
    """

    async def async_send(
        self, climate_id: str, service: str, payload: dict[str, Any], *, manual: bool = False,
    ) -> None: ...
