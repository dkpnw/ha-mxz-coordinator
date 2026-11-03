"""A room thermostat shows its room's new enable, target or range at once.

Dashboard cards (Mushroom, tile) render the thermostat's own state. It used
to re-render only when the plan that follows a change finished, 0.2 s to 10 s
later on a real install, so a card showed the old mode or setpoint and
invited a second tap that undid the first.

Each test records the thermostat's state at the moment the change asks for
its recompute, before any plan has run (``coord._refresh_generation`` is
unchanged then), and lets the real recompute run after. The heads are
recorded too: only the plan writes them, so they have not moved yet.

The enable and single-target checks failed before the fix. A range write
(on the tile, or on the Heat to, Cool to or target number of a range room)
already redrew at once, through ``set_zone_range``; those checks guard it.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError

from tests.test_dual_setpoint import (
    _call,
    _eid,
    _pair,
    _range,
    _run,
    _set,
    _setup,
    _start,
    _state,
)


def _at_refresh(
    hass: HomeAssistant, entry, monkeypatch, *entity_ids: str
) -> list[tuple[int, dict[str, State | None]]]:
    """Record (plan generation, states) whenever a recompute is requested.

    The real request runs right after the record, so the plan is exactly
    what it would be without this.
    """
    coord = entry.runtime_data
    seen: list[tuple[int, dict[str, State | None]]] = []
    real = coord.async_request_refresh

    async def _request() -> None:
        seen.append(
            (coord._refresh_generation, {eid: hass.states.get(eid) for eid in entity_ids})
        )
        await real()

    monkeypatch.setattr(coord, "async_request_refresh", _request)
    return seen


def _first(entry, seen, generation: int) -> dict[str, State | None]:
    """The states at the first request, which no plan had yet followed."""
    assert seen, "the change never asked for a recompute"
    at, states = seen[0]
    assert at == generation, "a plan ran before the change asked for one"
    return states


def _attrs(state: State | None) -> dict[str, Any]:
    assert state is not None
    return dict(state.attributes)


async def test_set_hvac_mode_shows_before_the_plan(hass: HomeAssistant, monkeypatch) -> None:
    """(1) climate.set_hvac_mode heat_cool / off is the tile's state at once."""
    entry, (head_a, _) = await _setup(hass)
    await _start(hass, entry)
    await _run(hass, entry, 75, 70)  # Living hot: its head cools
    tile = _eid(hass, entry, "primary_thermostat")
    assert hass.states.get(tile).state == "heat_cool"
    assert hass.states.get(head_a).state == "cool"
    coord = entry.runtime_data

    seen = _at_refresh(hass, entry, monkeypatch, tile, head_a)
    gen = coord._refresh_generation
    await _call(hass, "climate", "set_hvac_mode", {"entity_id": tile, "hvac_mode": "off"})
    states = _first(entry, seen, gen)
    assert states[tile].state == "off"
    assert states[head_a].state == "cool"  # the plan, not the tile, moves the head
    assert hass.states.get(tile).state == "off"

    seen.clear()
    gen = coord._refresh_generation
    await _call(hass, "climate", "set_hvac_mode", {"entity_id": tile, "hvac_mode": "heat_cool"})
    assert _first(entry, seen, gen)[tile].state == "heat_cool"
    assert hass.states.get(tile).state == "heat_cool"


async def test_enable_switch_shows_on_the_tile_at_once(hass: HomeAssistant, monkeypatch) -> None:
    """(2) switch turn_off / turn_on on the room's enable is the tile's state at once."""
    entry, (_, head_b) = await _setup(hass)
    await _start(hass, entry)
    await _run(hass, entry, 70, 75)  # Bedroom hot: its head cools
    tile = _eid(hass, entry, "secondary_thermostat")
    switch = _eid(hass, entry, "secondary_enable")
    assert hass.states.get(head_b).state == "cool"
    coord = entry.runtime_data

    seen = _at_refresh(hass, entry, monkeypatch, tile, switch, head_b)
    gen = coord._refresh_generation
    await _call(hass, "switch", "turn_off", {"entity_id": switch})
    states = _first(entry, seen, gen)
    assert states[switch].state == "off"
    assert states[tile].state == "off"
    assert states[head_b].state == "cool"  # untouched until the plan runs
    await _run(hass, entry, 70, 75)
    assert hass.states.get(head_b).state == "off"  # and the plan still parks it

    seen.clear()
    gen = coord._refresh_generation
    await _call(hass, "switch", "turn_on", {"entity_id": switch})
    states = _first(entry, seen, gen)
    assert states[switch].state == "on"
    assert states[tile].state == "heat_cool"


async def test_set_temperature_shows_before_the_plan(hass: HomeAssistant, monkeypatch) -> None:
    """(3) a single target and a range written on the tile show on it at once."""
    entry, _ = await _setup(hass, dual=(0,))
    await _start(hass, entry)
    single = _eid(hass, entry, "secondary_thermostat")
    dual = _eid(hass, entry, "primary_thermostat")
    coord = entry.runtime_data
    assert _attrs(hass.states.get(single))["temperature"] != 73
    assert _pair(entry) == (69, 71)

    seen = _at_refresh(hass, entry, monkeypatch, single, dual)
    gen = coord._refresh_generation
    await _call(hass, "climate", "set_temperature", {"entity_id": single, "temperature": 73})
    assert _attrs(_first(entry, seen, gen)[single])["temperature"] == 73

    seen.clear()
    gen = coord._refresh_generation
    await _range(hass, entry, "primary", 66, 76)
    tile = _attrs(_first(entry, seen, gen)[dual])
    assert (tile["target_temp_low"], tile["target_temp_high"]) == (66, 76)

    # A range with the mode in the same call: both show at once.
    await _call(hass, "climate", "set_hvac_mode", {"entity_id": dual, "hvac_mode": "off"})
    seen.clear()
    gen = coord._refresh_generation
    await _range(hass, entry, "primary", 67, 75, hvac_mode="heat_cool")
    states = _first(entry, seen, gen)
    assert states[dual].state == "heat_cool"
    tile = _attrs(seen[-1][1][dual])
    assert (tile["target_temp_low"], tile["target_temp_high"]) == (67, 75)


async def test_number_set_value_shows_on_the_tile_at_once(hass: HomeAssistant, monkeypatch) -> None:
    """(4) the room's target, Heat to and Cool to numbers show on the tile at once."""
    entry, _ = await _setup(hass, dual=(0,))
    await _start(hass, entry)
    single = _eid(hass, entry, "secondary_thermostat")
    dual = _eid(hass, entry, "primary_thermostat")
    coord = entry.runtime_data
    seen = _at_refresh(hass, entry, monkeypatch, single, dual)

    gen = coord._refresh_generation
    await _set(hass, entry, "secondary_target", 74)
    assert _attrs(_first(entry, seen, gen)[single])["temperature"] == 74

    seen.clear()
    gen = coord._refresh_generation
    await _set(hass, entry, "primary_target_low", 67)
    tile = _attrs(_first(entry, seen, gen)[dual])
    assert (tile["target_temp_low"], tile["target_temp_high"]) == (67, 71)

    seen.clear()
    gen = coord._refresh_generation
    await _set(hass, entry, "primary_target_high", 76)
    tile = _attrs(_first(entry, seen, gen)[dual])
    assert (tile["target_temp_low"], tile["target_temp_high"]) == (67, 76)

    # The target number of a range room moves the whole range there.
    seen.clear()
    gen = coord._refresh_generation
    await _set(hass, entry, "primary_target", 70)
    tile = _attrs(_first(entry, seen, gen)[dual])
    assert (tile["target_temp_low"], tile["target_temp_high"]) == _pair(entry)
    assert _pair(entry) != (67, 76)


async def test_a_refused_range_shows_nothing_and_asks_for_no_plan(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Negative: a refused write changes nothing, so the tile and plan stay put."""
    entry, _ = await _setup(hass, dual=(0,))
    await _start(hass, entry)
    dual = _eid(hass, entry, "primary_thermostat")
    before = _attrs(_state(hass, entry, "primary_thermostat"))
    seen = _at_refresh(hass, entry, monkeypatch, dual)

    with pytest.raises(ServiceValidationError):
        await _range(hass, entry, "primary", 65, 66, hvac_mode="off")  # too narrow
    with pytest.raises(ServiceValidationError):
        await _set(hass, entry, "primary_target_low", 87)  # pushes Cool to past the bounds
    assert seen == []
    assert _pair(entry) == (69, 71)
    after = hass.states.get(dual)
    assert after.state == "heat_cool"
    assert _attrs(after) == before
