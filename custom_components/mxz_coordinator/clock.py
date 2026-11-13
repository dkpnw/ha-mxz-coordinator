"""MXZ's clock: the seconds every hold and limit reads, and every wait MXZ schedules.

A coordinator builds one ``Clock`` and every controller reaches it as ``host.clock``.
Reading it gives monotonic seconds, so an NTP step or a clock set by hand never cuts a
hold short or stretches it. ``call_later`` is the only way MXZ waits: each wait has a
name, and a key when it is kept per head or per room, and there is one wait per name and
key, so arming it again replaces the one waiting. ``retire`` cancels them all when the
coordinator is unloaded or replaced, and a callback the loop already took for a wait that
was cancelled, replaced or retired does nothing.

A wait lives in memory only. A wait that must outlast a reload is timed from a stored
record and armed again when the next coordinator starts, as the mode-change hold is
from the mode record.

This is the one module that calls Home Assistant's ``async_call_later``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later


@dataclass(eq=False, slots=True)
class Wait:
    """One armed wait: what it is for, and how long it was set for."""

    name: str
    key: Hashable
    delay: float
    cancel: CALLBACK_TYPE | None = None


class Clock:
    """Monotonic seconds, and the waits one coordinator has armed on them."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._waits: dict[tuple[str, Hashable], Wait] = {}
        self.retired = False

    def __call__(self) -> float:
        return time.monotonic()

    def call_later(
        self,
        delay: float,
        action: Callable[[], object],
        *,
        name: str,
        key: Hashable = None,
        cancel_on_shutdown: bool = False,
    ) -> CALLBACK_TYPE:
        """Run ``action`` once, ``delay`` seconds from now, in place of the wait of this name and key.

        Returns the cancel for this wait only. Once retired, nothing is armed.
        """
        self.cancel(name, key)
        wait = Wait(name, key, delay)
        if self.retired:
            return lambda: None

        @callback
        def fire(_now: datetime) -> None:
            if self._waits.get((name, key)) is not wait:
                return  # cancelled, replaced or retired after the loop took it
            del self._waits[name, key]
            action()

        job = HassJob(fire, f"MXZ {name} wait", cancel_on_shutdown=cancel_on_shutdown or None)
        wait.cancel = self._schedule(wait, job)
        self._waits[name, key] = wait
        return lambda: self._drop(wait)

    def _schedule(self, wait: Wait, job: HassJob[[datetime], None]) -> CALLBACK_TYPE:
        return async_call_later(self._hass, wait.delay, job)

    def waiting(self, name: str, key: Hashable = None) -> bool:
        return (name, key) in self._waits

    def cancel(self, name: str, key: Hashable = None) -> None:
        if (wait := self._waits.get((name, key))) is not None:
            self._drop(wait)

    def cancel_all(self, name: str) -> None:
        for wait in [wait for wait in self._waits.values() if wait.name == name]:
            self._drop(wait)

    def retire(self) -> None:
        """Cancel every wait and arm none again: this coordinator is done."""
        self.retired = True
        for wait in list(self._waits.values()):
            self._drop(wait)

    def _drop(self, wait: Wait) -> None:
        if self._waits.get((wait.name, wait.key)) is wait:
            del self._waits[wait.name, wait.key]
            if wait.cancel is not None:
                wait.cancel()
