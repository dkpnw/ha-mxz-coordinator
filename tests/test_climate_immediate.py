"""A room thermostat shows its room's new enable, target, range or Dry at once.

Dashboard cards (Mushroom, tile) render the thermostat's own state. It used
to re-render only when the plan that follows a change finished, 0.2 s to 10 s
later on a real install, so a card showed the old mode or setpoint and
invited a second tap that undid the first.

Each test records the thermostat's state at the moment the change asks for
its recompute, before any plan has run (no new plan has been published
then), and then passes the request on unchanged. Here the first
request runs its plan at once; a later one inside the request cooldown waits,
and the test may end before it runs. The at-once checks read only the record,
so they don't depend on it; a check of what the plan does after calls the
recompute itself. The heads are recorded too: only the plan writes them, so
they have not moved yet.

The enable, single-target and Dry checks failed before the fix. A range
write (on the tile, or on the Heat to, Cool to or target number of a range
room) already redrew at once, through ``set_zone_range``; those checks guard
it.
"""

from __future__ import annotations

import weakref
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN

from custom_components.mxz_coordinator.const import (
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    DRY_PICK_WAIT,
    INHIBIT_ACTION_OFF,
)
from tests.peek import debug_view
from tests.test_dry_entity import LateHead
from tests.test_dry_mode import INHIBIT, DryHead
from tests.test_dry_mode import _setup as _dry_setup
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
from tests.test_startup_continuity import _head


class _Plans:
    """How many plans have run, read from what the coordinator publishes.

    Each plan is a new ``coord.data``, set as its compute runs and handed to
    every listener when it ends; a re-render with no plan hands over the same
    one. So each plan is counted once, whatever else redraws the entities.
    """

    def __init__(self, coord: Any) -> None:
        self._coord = coord
        self._last = coord.data
        self._count = 0
        coord.async_add_listener(self._look)

    def _look(self) -> None:
        if self._coord.data is not self._last:
            self._last = self._coord.data
            self._count += 1

    @property
    def count(self) -> int:
        self._look()
        return self._count


_COUNTERS: weakref.WeakKeyDictionary[Any, _Plans] = weakref.WeakKeyDictionary()


def _plans(entry) -> _Plans:
    """The plan counter of the entry's coordinator, made the first time it is asked for."""
    coord = entry.runtime_data
    if coord not in _COUNTERS:
        _COUNTERS[coord] = _Plans(coord)
    return _COUNTERS[coord]


def _at_refresh(
    hass: HomeAssistant, entry, monkeypatch, *entity_ids: str
) -> list[tuple[int, dict[str, State | None]]]:
    """Record (plans run so far, states) whenever a recompute is requested.

    The real request runs right after the record, so the plan is exactly
    what it would be without this.
    """
    coord = entry.runtime_data
    plans = _plans(entry)
    seen: list[tuple[int, dict[str, State | None]]] = []
    real = coord.async_request_refresh

    async def _request() -> None:
        seen.append((plans.count, {eid: hass.states.get(eid) for eid in entity_ids}))
        await real()

    monkeypatch.setattr(coord, "async_request_refresh", _request)
    return seen


def _first(entry, seen, generation: int) -> dict[str, State | None]:
    """The states at the first request, which no plan had yet followed."""
    assert seen, "the change never asked for a recompute"
    at, states = seen[0]
    assert at == generation, "a plan ran before the change asked for one"
    return states


async def _holds(hass: HomeAssistant, entry) -> list[dict[str, Any]]:
    """Each room's holds, as the diagnostics download shows them."""
    return [room["holds"] for room in (await debug_view(hass, entry))["rooms"].values()]


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

    seen = _at_refresh(hass, entry, monkeypatch, tile, head_a)
    gen = _plans(entry).count
    await _call(hass, "climate", "set_hvac_mode", {"entity_id": tile, "hvac_mode": "off"})
    states = _first(entry, seen, gen)
    assert states[tile].state == "off"
    assert states[head_a].state == "cool"  # the plan, not the tile, moves the head
    assert hass.states.get(tile).state == "off"

    seen.clear()
    gen = _plans(entry).count
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

    seen = _at_refresh(hass, entry, monkeypatch, tile, switch, head_b)
    gen = _plans(entry).count
    await _call(hass, "switch", "turn_off", {"entity_id": switch})
    states = _first(entry, seen, gen)
    assert states[switch].state == "off"
    assert states[tile].state == "off"
    assert states[head_b].state == "cool"  # untouched until the plan runs
    await _run(hass, entry, 70, 75)
    assert hass.states.get(head_b).state == "off"  # and the plan still parks it

    seen.clear()
    gen = _plans(entry).count
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
    assert _attrs(hass.states.get(single))["temperature"] != 73
    assert _pair(entry) == (69, 71)

    seen = _at_refresh(hass, entry, monkeypatch, single, dual)
    gen = _plans(entry).count
    await _call(hass, "climate", "set_temperature", {"entity_id": single, "temperature": 73})
    assert _attrs(_first(entry, seen, gen)[single])["temperature"] == 73

    seen.clear()
    gen = _plans(entry).count
    await _range(hass, entry, "primary", 66, 76)
    tile = _attrs(_first(entry, seen, gen)[dual])
    assert (tile["target_temp_low"], tile["target_temp_high"]) == (66, 76)

    # A range with the mode in the same call: both show at once.
    await _call(hass, "climate", "set_hvac_mode", {"entity_id": dual, "hvac_mode": "off"})
    seen.clear()
    gen = _plans(entry).count
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
    seen = _at_refresh(hass, entry, monkeypatch, single, dual)

    gen = _plans(entry).count
    await _set(hass, entry, "secondary_target", 74)
    assert _attrs(_first(entry, seen, gen)[single])["temperature"] == 74

    seen.clear()
    gen = _plans(entry).count
    await _set(hass, entry, "primary_target_low", 67)
    tile = _attrs(_first(entry, seen, gen)[dual])
    assert (tile["target_temp_low"], tile["target_temp_high"]) == (67, 71)

    seen.clear()
    gen = _plans(entry).count
    await _set(hass, entry, "primary_target_high", 76)
    tile = _attrs(_first(entry, seen, gen)[dual])
    assert (tile["target_temp_low"], tile["target_temp_high"]) == (67, 76)

    # The target number of a range room moves the whole range there.
    seen.clear()
    gen = _plans(entry).count
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


# ---------------------------------------------------------------------------
# Dry (Allow Dry mode): the Bedroom offers it, the Rec room beside it does not
# ---------------------------------------------------------------------------
BEDROOM = "_secondary_thermostat"


async def _mode(hass: HomeAssistant, tile: str, mode: str) -> None:
    await _call(hass, "climate", "set_hvac_mode", {"entity_id": tile, "hvac_mode": mode})


@pytest.mark.parametrize("start", ["off", "heat_cool"])
async def test_a_dry_pick_shows_before_the_plan(
    hass: HomeAssistant, monkeypatch, start: str
) -> None:
    """(5) Dry picked on the tile is its state at once, from Off or Heat/Cool.

    The plan has not run, so it has settled nothing and the head has been
    sent nothing yet; the plan that follows holds the head in dry.
    """
    rig = await _dry_setup(hass, enabled=(True, start == "heat_cool"))
    await rig.recompute()
    tile = rig.eid(BEDROOM)
    assert hass.states.get(tile).state == start
    seen = _at_refresh(hass, rig.entry, monkeypatch, tile, rig.heads[1])
    gen = _plans(rig.entry).count
    rig.forget()

    await _mode(hass, tile, "dry")
    states = _first(rig.entry, seen, gen)
    assert states[tile].state == "dry"
    assert states[rig.heads[1]].state != "dry"  # the pick's one write comes after
    await rig.recompute()
    assert rig.modes_sent(1) == ["dry"] and rig.state(1) == "dry"
    assert rig.row(1)["dry"] == "held"
    assert hass.states.get(tile).state == "dry"


@pytest.mark.parametrize(
    ("leave", "shown", "head_after"),
    [
        (("set_hvac_mode", "heat_cool"), "heat_cool", "fan_only"),
        (("set_hvac_mode", "off"), "off", "off"),
        (("turn_off", None), "off", "off"),
    ],
)
async def test_leaving_dry_shows_before_the_plan(
    hass: HomeAssistant, monkeypatch, leave: tuple[str, str | None], shown: str, head_after: str
) -> None:
    """(6) Heat/Cool or Off from a held Dry leaves dry on the tile at once.

    The head is still in dry then: only the plan that follows takes it out
    (Heat/Cool to the coil-dry idle, Off to off), as before.
    """
    rig = await _dry_setup(hass)
    tile = rig.eid(BEDROOM)
    await _mode(hass, tile, "dry")
    await rig.recompute()
    assert (rig.row(1)["dry"], rig.state(1), hass.states.get(tile).state) == (
        "held", "dry", "dry",
    )
    seen = _at_refresh(hass, rig.entry, monkeypatch, tile, rig.heads[1])
    gen = _plans(rig.entry).count
    rig.forget()

    service, mode = leave
    data = {"entity_id": tile} if mode is None else {"entity_id": tile, "hvac_mode": mode}
    await _call(hass, "climate", service, data)
    states = _first(rig.entry, seen, gen)
    assert states[tile].state == shown
    assert states[rig.heads[1]].state == "dry"  # untouched until the plan runs
    await rig.recompute()
    assert rig.modes_sent(1) == [head_after] and rig.state(1) == head_after
    assert rig.row(1)["dry"] is None
    assert hass.states.get(tile).state == shown


async def test_a_refused_dry_pick_shows_nothing_and_asks_for_no_plan(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Negative: Dry refused under a standby hold changes nothing, so the tile
    keeps Heat/Cool and no plan is asked for."""
    hass.states.async_set(INHIBIT, "off")
    rig = await _dry_setup(
        hass, **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_OFF}
    )
    hass.states.async_set(INHIBIT, "on")
    await hass.async_block_till_done()
    await rig.recompute()
    tile = rig.eid(BEDROOM)
    before = hass.states.get(tile)
    assert before.state == "heat_cool"
    seen = _at_refresh(hass, rig.entry, monkeypatch, tile)
    rig.forget()

    with pytest.raises(ServiceValidationError):
        await _mode(hass, tile, "dry")
    assert seen == []
    assert rig.sent(1) == []
    after = hass.states.get(tile)
    assert (after.state, dict(after.attributes)) == (before.state, dict(before.attributes))


async def test_heat_cool_after_the_head_left_dry_shows_before_the_plan(
    hass: HomeAssistant, monkeypatch
) -> None:
    """(7) The head left dry on its own remote and the plan for that is still
    waiting out the request cooldown: Heat/Cool picked then shows at once too,
    though there is no Dry left to release."""
    rig = await _dry_setup(hass)
    tile = rig.eid(BEDROOM)
    await _mode(hass, tile, "dry")
    await rig.recompute()
    assert (rig.row(1)["dry"], hass.states.get(tile).state) == ("held", "dry")
    # Another room's write runs a plan now and starts the request cooldown.
    await _call(
        hass, "number", "set_value", {"entity_id": rig.eid("_primary_target"), "value": 71}
    )
    gen = _plans(rig.entry).count
    await rig.person(1, "fan_only")  # out of dry on the remote: its plan waits
    assert _plans(rig.entry).count == gen
    assert rig.row(1)["dry"] == "held"  # the last plan's, not yet settled again
    seen = _at_refresh(hass, rig.entry, monkeypatch, tile)

    await _mode(hass, tile, "heat_cool")
    assert _first(rig.entry, seen, gen)[tile].state == "heat_cool"
    assert not any(  # nothing to release: the plan is unchanged
        holds["dry_released"] for holds in await _holds(hass, rig.entry)
    )
    await rig.recompute()
    assert (rig.row(1)["dry"], hass.states.get(tile).state) == (None, "heat_cool")


# A Dry pick that does not hold: refused by the head, or never reported in the
# wait. The thermostat leaves Dry at once, before the plan that follows runs.


class _RefusingHead(DryHead):
    """A head that rejects being sent dry (an invented definite rejection)."""

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.DRY:
            raise HomeAssistantError("invented rejection of dry")
        await super().async_set_hvac_mode(hvac_mode)


async def test_a_dry_pick_the_head_refuses_leaves_dry_at_once(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Negative: the head rejects the pick's one write. The tile is Heat/Cool
    again the moment the service returns its error, while the plan that error
    asked for still waits out the request cooldown (the pick's own enable ran
    one just before). Nothing else was sent to the head in between."""
    rig = await _dry_setup(hass, head_cls=_RefusingHead)
    await rig.recompute()
    tile = rig.eid(BEDROOM)
    assert hass.states.get(tile).state == "heat_cool"
    seen = _at_refresh(hass, rig.entry, monkeypatch, tile)
    rig.forget()

    with pytest.raises(HomeAssistantError, match="invented rejection of dry"):
        await _mode(hass, tile, "dry")
    assert len(seen) == 2  # the enable's request, then the refusal's
    assert seen[0][1][tile].state == "dry"  # the pick showed at once, as before
    refused_at, _states = seen[1]
    assert _plans(rig.entry).count == refused_at  # no plan since the refusal
    assert not any(holds["dry_pick_pending"] for holds in await _holds(hass, rig.entry))
    at_once = hass.states.get(tile)
    assert at_once.state == "heat_cool"
    assert rig.modes_sent(1) == ["dry"]  # the one refused write, nothing more yet

    await rig.later(REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)  # the refusal's plan
    assert _plans(rig.entry).count == refused_at + 1
    assert (rig.row(1)["dry"], hass.states.get(tile).state) == (None, "heat_cool")
    # The action shown at once was already the one that plan settles.
    assert hass.states.get(tile).attributes["hvac_action"] == at_once.attributes["hvac_action"]


async def test_a_dry_pick_the_head_never_reports_leaves_dry_when_it_expires(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Negative: no dry reported within DRY_PICK_WAIT. The tile goes from Dry
    to Heat/Cool inside the callback that ends the pick, before any plan."""
    rig = await _dry_setup(hass, head_cls=LateHead)
    await rig.recompute()
    head = _head(hass, rig.heads[1])
    head.late = True
    tile = rig.eid(BEDROOM)
    coord = rig.coord
    at_expiry: list[tuple[str, str]] = []  # the tile just before and just after
    real = coord.dry._pick_expired

    @callback  # as the real one is: run on the event loop, not in a worker thread
    def _expired(zone, now) -> None:
        before = hass.states.get(tile).state
        real(zone, now)  # a callback: no plan can run inside it
        at_expiry.append((before, hass.states.get(tile).state))

    monkeypatch.setattr(coord.dry, "_pick_expired", _expired)  # before the pick binds it
    await _mode(hass, tile, "dry")
    await rig.recompute()
    assert rig.state(1) != "dry" and hass.states.get(tile).state == "dry"  # asked, unreported

    await rig.later(DRY_PICK_WAIT - 5)
    assert at_expiry == [] and hass.states.get(tile).state == "dry"  # still counting
    await rig.later(DRY_PICK_WAIT + 1)
    assert at_expiry == [("dry", "heat_cool")]
    await rig.later(DRY_PICK_WAIT + REQUEST_REFRESH_DEFAULT_COOLDOWN + 2)
    assert (rig.row(1)["dry"], hass.states.get(tile).state) == (None, "heat_cool")


# The action next to the mode: what the tile shows doing follows the same live
# Dry as its mode, so a pick or a release that lands while the plan waits out
# the request cooldown shows the action that plan then settles, at once.


async def _start_cooldown(hass: HomeAssistant, rig) -> int:
    """Another room's write runs a plan now; the next request waits it out."""
    await _call(
        hass, "number", "set_value", {"entity_id": rig.eid("_primary_target"), "value": 71}
    )
    return _plans(rig.entry).count


async def test_a_dry_pick_shows_drying_before_the_plan(hass: HomeAssistant) -> None:
    """Dry picked inside the cooldown: the head reports dry at once, and the
    tile's action is drying at once, as the plan that follows holds it."""
    rig = await _dry_setup(hass)
    await rig.recompute()
    tile = rig.eid(BEDROOM)
    gen = await _start_cooldown(hass, rig)

    await _mode(hass, tile, "dry")
    assert _plans(rig.entry).count == gen  # the pick's plan still waits
    assert rig.state(1) == "dry"
    shown = hass.states.get(tile)
    assert (shown.state, shown.attributes["hvac_action"]) == ("dry", HVACAction.DRYING)

    await rig.later(REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)
    assert _plans(rig.entry).count > gen
    assert rig.row(1)["dry"] == "held"
    assert hass.states.get(tile).attributes["hvac_action"] == HVACAction.DRYING


async def test_heat_cool_from_dry_drops_drying_before_the_plan(hass: HomeAssistant) -> None:
    """Heat/Cool from a held Dry inside the cooldown: the head is still in dry
    until the plan runs, but the tile is Heat/Cool and no longer drying, at
    once; the action it shows is the one that plan then settles."""
    rig = await _dry_setup(hass)
    tile = rig.eid(BEDROOM)
    await _mode(hass, tile, "dry")
    await rig.recompute()
    assert hass.states.get(tile).attributes["hvac_action"] == HVACAction.DRYING
    await rig.later(REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)
    gen = await _start_cooldown(hass, rig)

    await _mode(hass, tile, "heat_cool")
    assert _plans(rig.entry).count == gen
    assert rig.state(1) == "dry"  # untouched until the plan runs
    at_once = hass.states.get(tile)
    assert at_once.state == "heat_cool"
    assert at_once.attributes["hvac_action"] != HVACAction.DRYING

    await rig.later(REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)
    assert _plans(rig.entry).count > gen and rig.state(1) != "dry"
    assert hass.states.get(tile).attributes["hvac_action"] == at_once.attributes["hvac_action"]
