"""A head whose integration raises something other than HomeAssistantError.

Its room is degraded and says so; the rest of the plan, the shared mode, its
hold and every MXZ entity go on as for any refused command
(docs/STATE-MACHINES.md SM-2 and SM-4). All heads and rooms are invented.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from tests.property_rig import TARGET, Rig, open_rig

HOT = TARGET + 5
COLD = TARGET - 5
UNAVAILABLE = "unavailable"


def _faulty(rig: Rig, room: int, error: type[Exception], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The room's head raises ``error`` from every mode change and lists what it refused."""
    refused: list[str] = []

    async def refuse(hvac_mode: Any = None, **kwargs: Any) -> None:
        refused.append(str(hvac_mode or kwargs.get("hvac_mode")))
        raise error("invented integration fault")

    head = rig.heads[room]
    monkeypatch.setattr(head, "async_set_hvac_mode", lambda hvac_mode: refuse(hvac_mode))
    monkeypatch.setattr(head, "async_set_temperature", lambda **kwargs: refuse(**kwargs))
    return refused


def _public(rig: Rig) -> dict[str, Any]:
    hass = rig.hass
    thermostat = hass.states.get(rig.eid("_secondary_thermostat"))
    return {
        "select": hass.states.get(rig.eid("_shared_mode")).state,
        "plan": hass.states.get(rig.eid("_plan")).state,
        "study": hass.states.get(rig.eid("_primary_thermostat")).state,
        "den": thermostat.state,
        "hold": thermostat.attributes.get("mode_change_hold_remaining"),
    }


@pytest.mark.parametrize("error", [RuntimeError, HomeAssistantError])
async def test_a_head_that_raises_during_a_mode_change_doesnt_undo_the_change(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, error: type[Exception],
) -> None:
    """The change is made, recorded and held, whatever the failing head raised."""
    rig = await open_rig(hass, freezer, temps=(HOT, TARGET))
    assert (rig.heads[0].mode, _public(rig)["select"]) == ("cool", "cool")
    refused = _faulty(rig, 0, error, monkeypatch)

    await rig.temp(0, TARGET)
    await rig.temp(1, COLD)
    assert refused
    assert rig.heads[0].mode == "cool"
    assert rig.heads[1].mode == "heat"
    seen = _public(rig)
    assert seen["select"] == seen["plan"] == "heat"
    assert UNAVAILABLE not in (seen["study"], seen["den"])
    assert seen["hold"] > 0
    assert rig.row(0)["command_status"] == "rejected"

    rig.freezer.tick(1)
    await rig.temp(1, HOT)
    assert _public(rig)["plan"] == "heat"
    assert rig.heads[1].mode == "fan_only"
    await rig.close()


async def test_a_head_that_raises_while_nothing_changes_degrades_only_its_room(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative: a fault with no mode change records no change and starts no hold."""
    rig = await open_rig(hass, freezer, temps=(HOT, TARGET))
    before = _public(rig)
    refused = _faulty(rig, 1, RuntimeError, monkeypatch)

    await rig.temp(1, HOT)
    assert refused
    assert rig.heads[0].mode == "cool"
    assert _public(rig) == before
    assert rig.row(1)["command_status"] == "rejected"
    await rig.close()



async def test_a_head_is_sent_a_new_mode_only_once_it_is_the_recorded_mode(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record never trails the heads, so nothing a head does can leave it behind (SM-4, SM-8)."""
    rig = await open_rig(hass, freezer, temps=(HOT, TARGET))
    head = rig.heads[1]
    seen: list[dict[str, Any]] = []
    set_mode, set_temperature = type(head).async_set_hvac_mode, type(head).async_set_temperature

    def look(mode: Any) -> None:
        if mode == "heat":
            seen.append(_public(rig))

    async def mode_write(hvac_mode: Any) -> None:
        look(hvac_mode)
        await set_mode(head, hvac_mode)

    async def temperature_write(**kwargs: Any) -> None:
        look(kwargs.get("hvac_mode"))
        await set_temperature(head, **kwargs)

    monkeypatch.setattr(head, "async_set_hvac_mode", mode_write)
    monkeypatch.setattr(head, "async_set_temperature", temperature_write)
    await rig.temp(0, TARGET)
    await rig.temp(1, COLD)
    assert head.mode == "heat"
    assert seen
    assert seen[0]["select"] == "heat"
    assert seen[0]["hold"] > 0
    await rig.close()
