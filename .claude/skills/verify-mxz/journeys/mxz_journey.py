"""What every journey shares: the evidence transcript and a real restart.

A transcript is one JSON file per journey. Each step records the action a
person or a head took, then what Home Assistant shows after it: the plan
sensor, each room thermostat, each head, the Fan auto switches, and every
service call made since the step before (MXZ's commands to the heads among
them, marked ``to_head``). It is written by the ``journal`` fixture at
teardown, pass or fail, to

    $MXZ_VERIFY_OUT/<clock>/<feature>/<journey>.json

where ``<clock>`` is ``fakeboot`` when the run loaded ``-p tools.fakeboot``
and ``host`` otherwise. ``MXZ_VERIFY_OUT`` defaults to
``~/verify-mxz-evidence/<UTC stamp>``, outside the checkout.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_restore_state_shutdown_restart,
)

_STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
OUT = Path(
    os.environ.get("MXZ_VERIFY_OUT") or Path.home() / "verify-mxz-evidence" / _STAMP
)

# The plan row keys a reader needs to follow a journey; the rest are noise.
ROW_KEYS = (
    "name", "demand", "engage", "temp", "target", "sensor_health", "sensor_age",
    "vote_status", "hands_off_vote", "lost_since", "fan_hold", "dry", "dry_vote",
)
PLAN_KEYS = (
    "standoff", "mode_change_allowed", "seconds_since_mode_change",
    "mode_by_hands_off", "mode_by_safety",
)
TILE_KEYS = ("hvac_action", "mode_change_hold_remaining", "mode_change_hold_until")
HEAD_KEYS = ("fan_mode", "target_temp_low", "target_temp_high", "temperature")
SLUGS = ("primary", "secondary", "zone_3")


def clock() -> str:
    """Which monotonic clock this run is on (tools/fakeboot.py or the host's)."""
    return "fakeboot" if "tools.fakeboot" in sys.modules else "host"


def entity(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str | None:
    """The entity_id of this entry's entity with unique_id ``<entry_id>_<key>``."""
    return er.async_get(hass).async_get_entity_id(
        _domain(key), "mxz_coordinator", f"{entry.entry_id}_{key}"
    )


def _domain(key: str) -> str:
    if key == "plan":
        return "sensor"
    if key.endswith("_thermostat"):
        return "climate"
    if key.endswith(("_enable", "_fan_auto")):
        return "switch"
    if key == "shared_mode":
        return "select"
    return "number"


class Transcript:
    """The steps of one journey, and the service calls between them."""

    def __init__(
        self, hass: HomeAssistant, entry: MockConfigEntry, feature: str, journey: str,
        *, heads: list[str],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.feature = feature
        self.journey = journey
        self.heads = heads
        self.steps: list[dict[str, Any]] = []
        self._calls: list[dict[str, Any]] = []
        hass.bus.async_listen(EVENT_CALL_SERVICE, self._record)

    @callback
    def _record(self, event: Any) -> None:
        data = event.data
        target = data.get("service_data", {}).get("entity_id")
        targets = [target] if isinstance(target, str) else list(target or [])
        self._calls.append(
            {
                "call": f"{data['domain']}.{data['service']}",
                "data": dict(data.get("service_data", {})),
                "to_head": any(t in self.heads for t in targets),
            }
        )

    def step(self, action: str) -> dict[str, Any]:
        """Record ``action`` and what Home Assistant shows right after it."""
        hass, entry = self.hass, self.entry
        shown: dict[str, Any] = {"action": action, "calls": self._calls}
        self._calls = []
        plan_id = entity(hass, entry, "plan")
        plan = hass.states.get(plan_id) if plan_id else None
        if plan is not None:
            shown["plan"] = {
                "state": plan.state,
                **{k: plan.attributes.get(k) for k in PLAN_KEYS},
                "zones": [
                    {k: row.get(k) for k in ROW_KEYS}
                    for row in plan.attributes.get("zones", ())
                ],
            }
        shown["thermostats"] = {}
        shown["fan_auto"] = {}
        for slug in SLUGS:
            tile_id = entity(hass, entry, f"{slug}_thermostat")
            if tile_id and (tile := hass.states.get(tile_id)) is not None:
                shown["thermostats"][tile_id] = {
                    "state": tile.state, **{k: tile.attributes.get(k) for k in TILE_KEYS},
                }
            fan_id = entity(hass, entry, f"{slug}_fan_auto")
            if fan_id and (fan := hass.states.get(fan_id)) is not None:
                shown["fan_auto"][fan_id] = fan.state
        shown["heads"] = {}
        for head_id in self.heads:
            head = hass.states.get(head_id)
            shown["heads"][head_id] = None if head is None else {
                "state": head.state, **{k: head.attributes.get(k) for k in HEAD_KEYS},
            }
        self.steps.append(shown)
        return shown

    def save(self, outcome: str) -> Path:
        path = OUT / clock() / self.feature / f"{self.journey}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "feature": self.feature,
                    "journey": self.journey,
                    "outcome": outcome,
                    "clock": clock(),
                    "home_assistant": HA_VERSION,
                    "python": sys.version.split()[0],
                    "revision": os.environ.get("MXZ_VERIFY_REV", "unknown"),
                    "steps": self.steps,
                },
                indent=2, default=str,
            )
            + "\n"
        )
        return path

    def head_calls(self, head: str, service: str | None = None) -> list[dict[str, Any]]:
        """Calls aimed at ``head`` in every recorded step, plus any not yet stepped."""
        calls = [c for s in self.steps for c in s["calls"]] + self._calls
        return [
            c for c in calls
            if _targets(c, head) and (service is None or c["call"] == service)
        ]


def _targets(call: dict[str, Any], head: str) -> bool:
    target = call["data"].get("entity_id")
    return head == target or (isinstance(target, list) and head in target)


async def restart(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Stop Home Assistant and start it again, in process.

    The restore data is dumped as at a real shutdown, the entry is unloaded,
    and the per-entry store instances are dropped, as a new process starts
    without them, so the next setup reads what was written to storage
    (tests/test_antiflip_persist.py uses the same steps).
    """
    await async_mock_restore_state_shutdown_restart(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.data.pop("mxz_coordinator_mode_change_stores", None)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
