"""Per-room uneven drift: a room's own "Let it get cooler by" / "Let it get warmer by".

A power user can set either side for one room on Configure > Rooms > (room),
in its collapsed "Uneven drift" section. Per side, the first of these that is
set wins:

1. the room's own side (``room_cooler_by`` / ``room_warmer_by``);
2. the room's drift number, only once it was written (``room``); a room that
   merely shows the global while following is not set;
3. the global "Let it get cooler by" / "Let it get warmer by" (``cooler_by`` /
   ``warmer_by``);
4. Allowed drift (``allowed_drift``).

``drift_source`` is ``room`` when either side comes from the room. Every run
still ends at the target. Fake heads and sensors only; every expected number is
worked out by hand from the stated settings unless a comment says it is pinned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

import custom_components.mxz_coordinator as component
from custom_components.mxz_coordinator.const import (
    CONF_ENGAGE_DEADBAND,
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
)
from tests.flow_pages import (
    marker_of,
    open_options_page,
    prefilled,
    sections,
    submit,
    suggested,
)
from tests.restart import restart
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_target,
    _set_temp,
    _setup_mock_heads,
)
from tests.test_issue18_drift import _set_drift
from tests.test_m29_follow_global import _press_follow_global
from tests.test_room_rename_reorder import _reconfigure, _setup_rooms

# Written out independently of const.py and config_flow.py.
COOLER = "drift_cooler_by"  # global option key, and the room's zone-record key
WARMER = "drift_warmer_by"
FIELD_COOLER = "room_cooler_by"  # the room page's flow fields
FIELD_WARMER = "room_warmer_by"
SECTION = "drift"
# The start-point keys the plan row and the thermostat both carry.
KEYS = (
    "drift_source", "drift_heat_from", "drift_cool_from",
    "heat_start", "cool_start", "heat_demand_below", "cool_demand_above",
    "start_points_mode", "start_points_lockouts",
)
# The five every precedence case asserts on both surfaces.
SHOWN = ("drift_source", "drift_heat_from", "drift_cool_from", "heat_start", "cool_start")

TARGET = 62.0  # the primary room's target in every °F case


async def _setup(
    hass: HomeAssistant,
    *,
    rooms: tuple[dict[str, Any], dict[str, Any]] = ({}, {}),
    stored: dict[str, Any] | None = None,
    celsius: bool = False,
    restore: tuple[float, dict[str, Any]] | None = None,
) -> MockConfigEntry:
    """Two rooms in a zones-list entry, coordinator and both rooms on.

    ``rooms`` adds keys to each stored room record. °F: primary target 62 at
    62, secondary target 70 at 70. °C: primary target 20 at 20, secondary at
    the °C default. ``restore`` seeds the primary drift number's saved state
    (value, attributes) as a previous run left it.
    """
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 20 if celsius else TARGET)
    await _set_temp(hass, SENSOR_B, 21 if celsius else 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="MXZ Coordinator",
        data={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_FAN_BOOST_ENABLE: True,
            "zones": [
                {"name": "Primary", "climate": head_a, "sensor": SENSOR_A, **rooms[0]},
                {"name": "Secondary", "climate": head_b, "sensor": SENSOR_B, **rooms[1]},
            ],
            **(stored or {}),
        },
    )
    entry.add_to_hass(hass)
    if restore is not None:
        value, attrs = restore
        mock_restore_cache_with_extra_data(
            hass,
            [
                (
                    State("number.mxz_coordinator_primary_drift", str(value), attrs),
                    {
                        "native_max_value": 5.0,
                        "native_min_value": 0.5,
                        "native_step": 0.25,
                        "native_unit_of_measurement": "°F",
                        "native_value": value,
                    },
                )
            ],
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 20 if celsius else TARGET)
    if not celsius:
        await _set_target(hass, _eid(hass, entry, "_secondary_target"), 70)
    await _recompute(hass, entry)
    return entry


def _row(hass: HomeAssistant, entry, index: int = 0) -> dict[str, Any]:
    return hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][index]


def _tile(hass: HomeAssistant, entry, slug: str = "primary") -> dict[str, Any]:
    return dict(hass.states.get(_eid(hass, entry, f"_{slug}_thermostat")).attributes)


def _number(hass: HomeAssistant, entry, slug: str = "primary") -> State:
    return hass.states.get(_eid(hass, entry, f"_{slug}_drift"))


def _expect(
    heat: tuple[float, str], cool: tuple[float, str], target: float = TARGET
) -> dict[str, Any]:
    """The five shown values for (drift, source) per side, by hand."""
    room = {"room", "room_cooler_by", "room_warmer_by"}
    return {
        "drift_source": "room" if {heat[1], cool[1]} & room else "global",
        "drift_heat_from": heat[1],
        "drift_cool_from": cool[1],
        "heat_start": target - heat[0],
        "cool_start": target + cool[0],
    }


def _assert_shown(
    hass: HomeAssistant, entry, expected: dict[str, Any], index: int = 0
) -> None:
    """The plan row and the room's thermostat both show ``expected``, and
    agree on every start-point key."""
    slug = ("primary", "secondary")[index]
    row, tile = _row(hass, entry, index), _tile(hass, entry, slug)
    assert {key: row[key] for key in SHOWN} == expected, row
    assert {key: tile[key] for key in SHOWN} == expected, tile
    assert {key: tile[key] for key in KEYS} == {key: row[key] for key in KEYS}


async def _settle(hass: HomeAssistant, entry) -> None:
    await hass.async_block_till_done()
    await _recompute(hass, entry)


# --- the precedence matrix, per side ----------------------------------------

# Distinct values, so every level is identifiable from the number alone.
ALLOWED = 2.0  # stored Allowed drift (the default is 1.0)
ROOM_DRIFT = 3.0
GLOBAL = {"heat": 1.5, "cool": 2.5}
ROOM_SIDE = {"heat": 4.0, "cool": 4.5}


def _oracle(side: str, room_side: bool, room_drift: bool, global_side: bool,
            allowed: bool) -> tuple[float, str]:
    """The stated rule, one level at a time, for one side."""
    if room_side:
        return ROOM_SIDE[side], {"heat": "room_cooler_by", "cool": "room_warmer_by"}[side]
    if room_drift:
        return ROOM_DRIFT, "room"
    if global_side:
        return GLOBAL[side], {"heat": "cooler_by", "cool": "warmer_by"}[side]
    return (ALLOWED if allowed else 1.0), "allowed_drift"


_BOOL = (False, True)


@pytest.mark.parametrize("allowed", _BOOL, ids=["allowed-default", "allowed-2"])
@pytest.mark.parametrize("room_drift", _BOOL, ids=["drift-unset", "drift-set"])
@pytest.mark.parametrize(
    ("heat_room", "heat_global"),
    [(a, b) for a in _BOOL for b in _BOOL],
    ids=["heat-none", "heat-global", "heat-room", "heat-both"],
)
@pytest.mark.parametrize(
    ("cool_room", "cool_global"),
    [(a, b) for a in _BOOL for b in _BOOL],
    ids=["cool-none", "cool-global", "cool-room", "cool-both"],
)
async def test_precedence_matrix_per_side(
    hass: HomeAssistant,
    allowed: bool,
    room_drift: bool,
    heat_room: bool,
    heat_global: bool,
    cool_room: bool,
    cool_global: bool,
) -> None:
    """Every combination of the four levels on each side (the room drift
    number and Allowed drift are shared by both sides)."""
    room: dict[str, Any] = {}
    if heat_room:
        room[COOLER] = ROOM_SIDE["heat"]
    if cool_room:
        room[WARMER] = ROOM_SIDE["cool"]
    stored: dict[str, Any] = {}
    if allowed:
        stored[CONF_ENGAGE_DEADBAND] = ALLOWED
    if heat_global:
        stored[COOLER] = GLOBAL["heat"]
    if cool_global:
        stored[WARMER] = GLOBAL["cool"]
    entry = await _setup(hass, rooms=(room, {}), stored=stored)
    if room_drift:
        await _set_drift(hass, entry, ROOM_DRIFT)
        await _recompute(hass, entry)
    heat = _oracle("heat", heat_room, room_drift, heat_global, allowed)
    cool = _oracle("cool", cool_room, room_drift, cool_global, allowed)
    _assert_shown(hass, entry, _expect(heat, cool))
    row = _row(hass, entry)
    assert (row["drift_heat"], row["drift_cool"]) == (heat[0], cool[0])
    # The demand band stays max(threshold 3, side drift).
    assert row["heat_demand_below"] == TARGET - max(3.0, heat[0])
    assert row["cool_demand_above"] == TARGET + max(3.0, cool[0])
    # The drift number still shows its own value or Allowed drift, unchanged.
    assert float(_number(hass, entry).state) == (
        ROOM_DRIFT if room_drift else (ALLOWED if allowed else 1.0)
    )
    # The other room has no room sides and no drift override: global levels.
    other = _row(hass, entry, 1)
    assert other["drift_source"] == "global"
    assert (other["drift_heat"], other["drift_cool"]) == (
        _oracle("heat", False, False, heat_global, allowed)[0],
        _oracle("cool", False, False, cool_global, allowed)[0],
    )


# --- Maintainer cases: configured vs inherited room drift --------------------

GLOBAL_SIDES = {COOLER: 3.0, WARMER: 5.0}


async def test_unset_room_drift_lets_the_global_sides_win(hass: HomeAssistant) -> None:
    """(a) A fresh room shows Allowed drift on its number, but that is not a
    setting: the global sides win on both sides."""
    entry = await _setup(hass, stored=GLOBAL_SIDES)
    coord = entry.runtime_data
    assert coord.zones[0].drift is None
    shown = _number(hass, entry)
    assert float(shown.state) == 1.0
    assert shown.attributes["override"] is False
    _assert_shown(hass, entry, _expect((3.0, "cooler_by"), (5.0, "warmer_by")))


async def test_set_room_drift_beats_the_global_sides(hass: HomeAssistant) -> None:
    entry = await _setup(hass, stored=GLOBAL_SIDES)
    await _set_drift(hass, entry, 2.0)
    await _recompute(hass, entry)
    _assert_shown(hass, entry, _expect((2.0, "room"), (2.0, "room")))


async def test_room_drift_set_to_allowed_drift_still_counts_as_set(
    hass: HomeAssistant,
) -> None:
    """An explicit write of exactly Allowed drift (1.0) is a choice someone
    made, so it still beats the global sides."""
    entry = await _setup(hass, stored=GLOBAL_SIDES)
    coord = entry.runtime_data
    await _set_drift(hass, entry, coord.engage_deadband)
    await _recompute(hass, entry)
    assert coord.zones[0].drift == 1.0
    assert _number(hass, entry).attributes["override"] is True
    _assert_shown(hass, entry, _expect((1.0, "room"), (1.0, "room")))
    # Follow global drift is the way back.
    await _press_follow_global(hass, entry)
    await _recompute(hass, entry)
    _assert_shown(hass, entry, _expect((3.0, "cooler_by"), (5.0, "warmer_by")))


@pytest.mark.parametrize(
    ("drift", "fallback"),
    [
        (None, None),  # unset: the other side takes the global side
        (2.0, (2.0, "room")),  # set: the other side takes the room drift
        (1.0, (1.0, "room")),  # set to exactly Allowed drift: still the room drift
    ],
    ids=["drift-unset", "drift-set", "drift-set-to-allowed"],
)
@pytest.mark.parametrize("side", ["heat", "cool"])
async def test_a_room_side_wins_its_side_only(
    hass: HomeAssistant, side: str, drift: float | None, fallback: tuple | None
) -> None:
    room = {COOLER: 4.0} if side == "heat" else {WARMER: 4.5}
    entry = await _setup(hass, rooms=(room, {}), stored=GLOBAL_SIDES)
    if drift is not None:
        await _set_drift(hass, entry, drift)
        await _recompute(hass, entry)
    if side == "heat":
        expected = _expect((4.0, "room_cooler_by"), fallback or (5.0, "warmer_by"))
    else:
        expected = _expect(fallback or (3.0, "cooler_by"), (4.5, "room_warmer_by"))
    _assert_shown(hass, entry, expected)


@pytest.mark.parametrize(
    ("room", "stored", "heat", "cool"),
    [
        # The blank side falls to the global side when there is one...
        ({WARMER: 4.0}, {COOLER: 3.0}, (3.0, "cooler_by"), (4.0, "room_warmer_by")),
        # ...else to Allowed drift.
        ({WARMER: 4.0}, {}, (1.0, "allowed_drift"), (4.0, "room_warmer_by")),
        ({COOLER: 2.0}, {CONF_ENGAGE_DEADBAND: 1.5}, (2.0, "room_cooler_by"),
         (1.5, "allowed_drift")),
        # Both room sides set: neither global side is used.
        ({COOLER: 0.5, WARMER: 5.0}, GLOBAL_SIDES, (0.5, "room_cooler_by"),
         (5.0, "room_warmer_by")),
    ],
)
async def test_the_blank_room_side_falls_through_on_its_own(
    hass: HomeAssistant, room, stored, heat, cool
) -> None:
    entry = await _setup(hass, rooms=(room, {}), stored=stored)
    _assert_shown(hass, entry, _expect(heat, cool))


# --- the room side really moves the start, and runs still end at target ------


async def test_a_room_side_starts_runs_on_its_own_drift_and_runs_to_target(
    hass: HomeAssistant,
) -> None:
    """Room warmer by 4 under a global warmer by 1.5: cool starts past 66 (not
    63.5), demand is max(3, 4) = 66, and the run still ends at 62."""
    entry = await _setup(hass, rooms=({WARMER: 4.0}, {}), stored={WARMER: 1.5})

    async def at(temp: float) -> dict[str, Any]:
        await _set_temp(hass, SENSOR_A, temp)
        await _recompute(hass, entry)
        return _row(hass, entry)

    row = await at(65.75)
    assert (row["engage"], row["demand"]) == ("satisfied", "neutral")
    row = await at(66.25)
    assert (row["engage"], row["demand"]) == ("cool", "cool")
    row = await at(62.5)
    assert row["engage"] == "cool"  # still running
    row = await at(62.0)
    assert row["engage"] == "satisfied"  # ends at the target, not at 66
    # The secondary room (target 70) keeps the global 1.5: cool past 71.5.
    await _set_temp(hass, SENSOR_B, 71.75)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["engage"] == "cool"


# --- both room sides blank: 3.8.0 plus #41, pinned --------------------------

# Captured from main at 97eceaf (3.8.0 plus #41) with this file's _setup,
# before any room side existed, and kept as data: (global options, the value
# written to the primary room's drift number or None, then per room (drift,
# drift_heat, drift_cool, drift_source, drift_heat_from, drift_cool_from,
# heat_start, cool_start, heat_demand_below, cool_demand_above, the drift
# number's state), then the primary room's (engage, demand) at SWEEP, each read
# from a coasting room: h heat, c cool, s satisfied, n neutral).
SWEEP = (58.5, 59.5, 60.5, 61.25, 62.75, 63.5, 64.5, 65.5, 66.5, 67.5)
PINNED = [
    ({}, None, (1.0, 1.0, 1.0, 'global', 'allowed_drift', 'allowed_drift', 61.0, 63.0, 59.0, 65.0, 1.0),
     (1.0, 1.0, 1.0, 'global', 'allowed_drift', 'allowed_drift', 69.0, 71.0, 67.0, 73.0, 1.0),
     "hh hn hn sn sn cn cn cc cc cc"),
    ({COOLER: 3.0}, None, (1.0, 3.0, 1.0, 'global', 'cooler_by', 'allowed_drift', 59.0, 63.0, 59.0, 65.0, 1.0),
     (1.0, 3.0, 1.0, 'global', 'cooler_by', 'allowed_drift', 67.0, 71.0, 67.0, 73.0, 1.0),
     "hh sn sn sn sn cn cn cc cc cc"),
    ({WARMER: 4.0}, None, (1.0, 1.0, 4.0, 'global', 'allowed_drift', 'warmer_by', 61.0, 66.0, 59.0, 66.0, 1.0),
     (1.0, 1.0, 4.0, 'global', 'allowed_drift', 'warmer_by', 69.0, 74.0, 67.0, 74.0, 1.0),
     "hh hn hn sn sn sn sn sn cc cc"),
    ({COOLER: 3.0, WARMER: 5.0}, None, (1.0, 3.0, 5.0, 'global', 'cooler_by', 'warmer_by', 59.0, 67.0, 59.0, 67.0, 1.0),
     (1.0, 3.0, 5.0, 'global', 'cooler_by', 'warmer_by', 67.0, 75.0, 67.0, 75.0, 1.0),
     "hh sn sn sn sn sn sn sn sn cc"),
    ({CONF_ENGAGE_DEADBAND: 2.0}, None, (2.0, 2.0, 2.0, 'global', 'allowed_drift', 'allowed_drift', 60.0, 64.0, 59.0, 65.0, 2.0),
     (2.0, 2.0, 2.0, 'global', 'allowed_drift', 'allowed_drift', 68.0, 72.0, 67.0, 73.0, 2.0),
     "hh hn sn sn sn sn cn cc cc cc"),
    ({CONF_ENGAGE_DEADBAND: 2.0, COOLER: 1.5}, None, (2.0, 1.5, 2.0, 'global', 'cooler_by', 'allowed_drift', 60.5, 64.0, 59.0, 65.0, 2.0),
     (2.0, 1.5, 2.0, 'global', 'cooler_by', 'allowed_drift', 68.5, 72.0, 67.0, 73.0, 2.0),
     "hh hn sn sn sn sn cn cc cc cc"),
    ({COOLER: 1.5, WARMER: 0.5}, None, (1.0, 1.5, 0.5, 'global', 'cooler_by', 'warmer_by', 60.5, 62.5, 59.0, 65.0, 1.0),
     (1.0, 1.5, 0.5, 'global', 'cooler_by', 'warmer_by', 68.5, 70.5, 67.0, 73.0, 1.0),
     "hh hn sn sn cn cn cn cc cc cc"),
    ({}, 2.0, (2.0, 2.0, 2.0, 'room', 'room', 'room', 60.0, 64.0, 59.0, 65.0, 2.0),
     (1.0, 1.0, 1.0, 'global', 'allowed_drift', 'allowed_drift', 69.0, 71.0, 67.0, 73.0, 1.0),
     "hh hn sn sn sn sn cn cc cc cc"),
    ({COOLER: 3.0}, 2.0, (2.0, 2.0, 2.0, 'room', 'room', 'room', 60.0, 64.0, 59.0, 65.0, 2.0),
     (1.0, 3.0, 1.0, 'global', 'cooler_by', 'allowed_drift', 67.0, 71.0, 67.0, 73.0, 1.0),
     "hh hn sn sn sn sn cn cc cc cc"),
    ({WARMER: 4.0}, 2.0, (2.0, 2.0, 2.0, 'room', 'room', 'room', 60.0, 64.0, 59.0, 65.0, 2.0),
     (1.0, 1.0, 4.0, 'global', 'allowed_drift', 'warmer_by', 69.0, 74.0, 67.0, 74.0, 1.0),
     "hh hn sn sn sn sn cn cc cc cc"),
    ({COOLER: 3.0, WARMER: 5.0}, 2.0, (2.0, 2.0, 2.0, 'room', 'room', 'room', 60.0, 64.0, 59.0, 65.0, 2.0),
     (1.0, 3.0, 5.0, 'global', 'cooler_by', 'warmer_by', 67.0, 75.0, 67.0, 75.0, 1.0),
     "hh hn sn sn sn sn cn cc cc cc"),
    ({CONF_ENGAGE_DEADBAND: 2.0}, 2.0, (2.0, 2.0, 2.0, 'room', 'room', 'room', 60.0, 64.0, 59.0, 65.0, 2.0),
     (2.0, 2.0, 2.0, 'global', 'allowed_drift', 'allowed_drift', 68.0, 72.0, 67.0, 73.0, 2.0),
     "hh hn sn sn sn sn cn cc cc cc"),
    ({CONF_ENGAGE_DEADBAND: 2.0, COOLER: 1.5}, 2.0, (2.0, 2.0, 2.0, 'room', 'room', 'room', 60.0, 64.0, 59.0, 65.0, 2.0),
     (2.0, 1.5, 2.0, 'global', 'cooler_by', 'allowed_drift', 68.5, 72.0, 67.0, 73.0, 2.0),
     "hh hn sn sn sn sn cn cc cc cc"),
    ({COOLER: 1.5, WARMER: 0.5}, 2.0, (2.0, 2.0, 2.0, 'room', 'room', 'room', 60.0, 64.0, 59.0, 65.0, 2.0),
     (1.0, 1.5, 0.5, 'global', 'cooler_by', 'warmer_by', 68.5, 70.5, 67.0, 73.0, 1.0),
     "hh hn sn sn sn sn cn cc cc cc"),
    ({}, 1.0, (1.0, 1.0, 1.0, 'room', 'room', 'room', 61.0, 63.0, 59.0, 65.0, 1.0),
     (1.0, 1.0, 1.0, 'global', 'allowed_drift', 'allowed_drift', 69.0, 71.0, 67.0, 73.0, 1.0),
     "hh hn hn sn sn cn cn cc cc cc"),
    ({COOLER: 3.0}, 1.0, (1.0, 1.0, 1.0, 'room', 'room', 'room', 61.0, 63.0, 59.0, 65.0, 1.0),
     (1.0, 3.0, 1.0, 'global', 'cooler_by', 'allowed_drift', 67.0, 71.0, 67.0, 73.0, 1.0),
     "hh hn hn sn sn cn cn cc cc cc"),
    ({WARMER: 4.0}, 1.0, (1.0, 1.0, 1.0, 'room', 'room', 'room', 61.0, 63.0, 59.0, 65.0, 1.0),
     (1.0, 1.0, 4.0, 'global', 'allowed_drift', 'warmer_by', 69.0, 74.0, 67.0, 74.0, 1.0),
     "hh hn hn sn sn cn cn cc cc cc"),
    ({COOLER: 3.0, WARMER: 5.0}, 1.0, (1.0, 1.0, 1.0, 'room', 'room', 'room', 61.0, 63.0, 59.0, 65.0, 1.0),
     (1.0, 3.0, 5.0, 'global', 'cooler_by', 'warmer_by', 67.0, 75.0, 67.0, 75.0, 1.0),
     "hh hn hn sn sn cn cn cc cc cc"),
    ({CONF_ENGAGE_DEADBAND: 2.0}, 1.0, (1.0, 1.0, 1.0, 'room', 'room', 'room', 61.0, 63.0, 59.0, 65.0, 1.0),
     (2.0, 2.0, 2.0, 'global', 'allowed_drift', 'allowed_drift', 68.0, 72.0, 67.0, 73.0, 2.0),
     "hh hn hn sn sn cn cn cc cc cc"),
    ({CONF_ENGAGE_DEADBAND: 2.0, COOLER: 1.5}, 1.0, (1.0, 1.0, 1.0, 'room', 'room', 'room', 61.0, 63.0, 59.0, 65.0, 1.0),
     (2.0, 1.5, 2.0, 'global', 'cooler_by', 'allowed_drift', 68.5, 72.0, 67.0, 73.0, 2.0),
     "hh hn hn sn sn cn cn cc cc cc"),
    ({COOLER: 1.5, WARMER: 0.5}, 1.0, (1.0, 1.0, 1.0, 'room', 'room', 'room', 61.0, 63.0, 59.0, 65.0, 1.0),
     (1.0, 1.5, 0.5, 'global', 'cooler_by', 'warmer_by', 68.5, 70.5, 67.0, 73.0, 1.0),
     "hh hn hn sn sn cn cn cc cc cc"),
    ({}, 4.0, (4.0, 4.0, 4.0, 'room', 'room', 'room', 58.0, 66.0, 58.0, 66.0, 4.0),
     (1.0, 1.0, 1.0, 'global', 'allowed_drift', 'allowed_drift', 69.0, 71.0, 67.0, 73.0, 1.0),
     "sn sn sn sn sn sn sn sn cc cc"),
    ({COOLER: 3.0}, 4.0, (4.0, 4.0, 4.0, 'room', 'room', 'room', 58.0, 66.0, 58.0, 66.0, 4.0),
     (1.0, 3.0, 1.0, 'global', 'cooler_by', 'allowed_drift', 67.0, 71.0, 67.0, 73.0, 1.0),
     "sn sn sn sn sn sn sn sn cc cc"),
    ({WARMER: 4.0}, 4.0, (4.0, 4.0, 4.0, 'room', 'room', 'room', 58.0, 66.0, 58.0, 66.0, 4.0),
     (1.0, 1.0, 4.0, 'global', 'allowed_drift', 'warmer_by', 69.0, 74.0, 67.0, 74.0, 1.0),
     "sn sn sn sn sn sn sn sn cc cc"),
    ({COOLER: 3.0, WARMER: 5.0}, 4.0, (4.0, 4.0, 4.0, 'room', 'room', 'room', 58.0, 66.0, 58.0, 66.0, 4.0),
     (1.0, 3.0, 5.0, 'global', 'cooler_by', 'warmer_by', 67.0, 75.0, 67.0, 75.0, 1.0),
     "sn sn sn sn sn sn sn sn cc cc"),
    ({CONF_ENGAGE_DEADBAND: 2.0}, 4.0, (4.0, 4.0, 4.0, 'room', 'room', 'room', 58.0, 66.0, 58.0, 66.0, 4.0),
     (2.0, 2.0, 2.0, 'global', 'allowed_drift', 'allowed_drift', 68.0, 72.0, 67.0, 73.0, 2.0),
     "sn sn sn sn sn sn sn sn cc cc"),
    ({CONF_ENGAGE_DEADBAND: 2.0, COOLER: 1.5}, 4.0, (4.0, 4.0, 4.0, 'room', 'room', 'room', 58.0, 66.0, 58.0, 66.0, 4.0),
     (2.0, 1.5, 2.0, 'global', 'cooler_by', 'allowed_drift', 68.5, 72.0, 67.0, 73.0, 2.0),
     "sn sn sn sn sn sn sn sn cc cc"),
    ({COOLER: 1.5, WARMER: 0.5}, 4.0, (4.0, 4.0, 4.0, 'room', 'room', 'room', 58.0, 66.0, 58.0, 66.0, 4.0),
     (1.0, 1.5, 0.5, 'global', 'cooler_by', 'warmer_by', 68.5, 70.5, 67.0, 73.0, 1.0),
     "sn sn sn sn sn sn sn sn cc cc"),
]
_SHOWN_PIN = (
    "drift", "drift_heat", "drift_cool", "drift_source", "drift_heat_from",
    "drift_cool_from", "heat_start", "cool_start", "heat_demand_below",
    "cool_demand_above",
)
_CALL = {"h": "heat", "c": "cool", "s": "satisfied", "n": "neutral"}


@pytest.mark.parametrize(("stored", "drift", "primary", "secondary", "sweep"), PINNED)
async def test_blank_room_sides_reproduce_main(
    hass: HomeAssistant, stored: dict[str, Any], drift: float | None,
    primary: tuple, secondary: tuple, sweep: str,
) -> None:
    entry = await _setup(hass, stored=stored)
    if drift is not None:
        await _set_drift(hass, entry, drift)
        await _recompute(hass, entry)
    for index, (slug, pinned) in enumerate((("primary", primary), ("secondary", secondary))):
        row = _row(hass, entry, index)
        seen = (
            *(row[key] for key in _SHOWN_PIN),
            float(_number(hass, entry, slug).state),
        )
        assert seen == pinned, slug
        assert (row["start_points_mode"], row["start_points_lockouts"]) == ("comfort", [])
        assert {key: _tile(hass, entry, slug)[key] for key in KEYS} == {
            key: row[key] for key in KEYS
        }
    seen_sweep = []
    for temp in SWEEP:
        await _set_temp(hass, SENSOR_A, TARGET)
        await _recompute(hass, entry)
        await _set_temp(hass, SENSOR_A, temp)
        await _recompute(hass, entry)
        row = _row(hass, entry)
        seen_sweep.append((row["engage"], row["demand"]))
    assert seen_sweep == [(_CALL[pair[0]], _CALL[pair[1]]) for pair in sweep.split()]


# --- restart and restore -------------------------------------------------------


async def _restart_and_settle(hass: HomeAssistant, entry) -> None:
    await restart(hass, entry)
    await _recompute(hass, entry)


async def test_restart_of_a_never_touched_room_restores_no_override(
    hass: HomeAssistant,
) -> None:
    """(b) The saved state is the displayed global; it does not come back as
    a setting, so the global sides still win."""
    entry = await _setup(hass, stored=GLOBAL_SIDES)
    await _restart_and_settle(hass, entry)
    assert entry.runtime_data.zones[0].drift is None
    assert _number(hass, entry).attributes["override"] is False
    _assert_shown(hass, entry, _expect((3.0, "cooler_by"), (5.0, "warmer_by")))


@pytest.mark.parametrize("value", [1.0, 2.0], ids=["equal-to-allowed", "other"])
async def test_restart_of_a_set_room_restores_it_as_set(
    hass: HomeAssistant, value: float
) -> None:
    """(c) A written drift comes back as written, even when it equals
    Allowed drift, and still beats the global sides."""
    entry = await _setup(hass, stored=GLOBAL_SIDES)
    await _set_drift(hass, entry, value)
    await _restart_and_settle(hass, entry)
    assert entry.runtime_data.zones[0].drift == value
    assert _number(hass, entry).attributes["override"] is True
    _assert_shown(hass, entry, _expect((value, "room"), (value, "room")))


async def test_restart_after_follow_global_stays_unset(hass: HomeAssistant) -> None:
    """(d) Set, then Follow global drift, then restart: still following."""
    entry = await _setup(hass, stored=GLOBAL_SIDES)
    await _set_drift(hass, entry, 2.0)
    await _press_follow_global(hass, entry)
    await _restart_and_settle(hass, entry)
    assert entry.runtime_data.zones[0].drift is None
    assert _number(hass, entry).attributes["override"] is False
    _assert_shown(hass, entry, _expect((3.0, "cooler_by"), (5.0, "warmer_by")))


@pytest.mark.parametrize(
    ("value", "attrs", "configured"),
    [
        (3.0, {}, False),  # (f) legacy state, no override attribute
        (1.0, {}, False),  # (f) legacy state equal to Allowed drift
        (3.0, {"override": False}, False),  # a following room's saved state
        (3.0, {"override": True}, True),  # control: a set room's saved state
    ],
    ids=["legacy", "legacy-equal-to-allowed", "following", "set-control"],
)
async def test_a_saved_state_counts_as_set_only_with_the_override_flag(
    hass: HomeAssistant, value: float, attrs: dict[str, Any], configured: bool
) -> None:
    entry = await _setup(hass, stored=GLOBAL_SIDES, restore=(value, attrs))
    assert entry.runtime_data.zones[0].drift == (value if configured else None)
    if configured:
        expected = _expect((value, "room"), (value, "room"))
    else:
        expected = _expect((3.0, "cooler_by"), (5.0, "warmer_by"))
    _assert_shown(hass, entry, expected)


async def test_room_sides_persist_across_a_restart_beside_a_restored_drift(
    hass: HomeAssistant,
) -> None:
    """The room side is config, so it survives a restart, and the restored
    drift number still fills the other side."""
    entry = await _setup(hass, rooms=({WARMER: 4.5}, {}), stored=GLOBAL_SIDES)
    await _set_drift(hass, entry, 2.0)
    _assert_shown(hass, entry, _expect((2.0, "room"), (4.5, "room_warmer_by")))
    await _restart_and_settle(hass, entry)
    coord = entry.runtime_data
    assert (coord.zones[0].drift, coord.zones[0].drift_warmer_by) == (2.0, 4.5)
    _assert_shown(hass, entry, _expect((2.0, "room"), (4.5, "room_warmer_by")))
    # Following again: the heat side drops to the global side, cool stays.
    await _press_follow_global(hass, entry)
    await _restart_and_settle(hass, entry)
    _assert_shown(hass, entry, _expect((3.0, "cooler_by"), (4.5, "room_warmer_by")))


async def _save_page(hass: HomeAssistant, entry, page: str, **changes: Any) -> dict:
    """Save one Configure page as a person would (the entry reloads)."""
    result = await open_options_page(hass, entry, page)
    result = await submit(
        hass.config_entries.options, result, {**prefilled(result), **changes}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await _settle(hass, entry)
    return result


async def test_an_unset_room_follows_a_new_allowed_drift(hass: HomeAssistant) -> None:
    """(e) Comfort's Allowed drift changes; an unset room follows it on its
    number and in its plan row, and a room side still wins its side."""
    entry = await _setup(hass, rooms=({WARMER: 4.5}, {}))
    _assert_shown(hass, entry, _expect((1.0, "allowed_drift"), (4.5, "room_warmer_by")))
    await _save_page(hass, entry, "comfort", **{CONF_ENGAGE_DEADBAND: 2.5})
    assert entry.runtime_data.zones[0].drift is None
    assert float(_number(hass, entry).state) == 2.5
    _assert_shown(hass, entry, _expect((2.5, "allowed_drift"), (4.5, "room_warmer_by")))


# --- stored values are clamped on load, like the global sides ----------------


@pytest.mark.parametrize(
    ("room", "expected"),
    [
        ({COOLER: 0.0, WARMER: 40.0}, (0.5, 5.0)),
        ({COOLER: -2.0, WARMER: 5.5}, (0.5, 5.0)),
        ({COOLER: 2.5, WARMER: 4.0}, (2.5, 4.0)),
        ({COOLER: 0.5, WARMER: 5.0}, (0.5, 5.0)),
        ({COOLER: "3", WARMER: 7}, (3.0, 5.0)),  # read like Allowed drift
        # Not a number: blank (the side falls through), never a failed setup.
        ({COOLER: "warm", WARMER: float("nan")}, (None, None)),
        ({COOLER: float("inf"), WARMER: "-inf"}, (None, None)),
        ({COOLER: None}, (None, None)),
    ],
)
async def test_fahrenheit_room_sides_are_clamped_on_load(
    hass: HomeAssistant, room: dict[str, Any], expected: tuple
) -> None:
    entry = await _setup(hass, rooms=(room, {}), stored={COOLER: 1.5, WARMER: 2.5})
    zone = entry.runtime_data.zones[0]
    assert (zone.drift_cooler_by, zone.drift_warmer_by) == expected
    heat = (1.5, "cooler_by") if expected[0] is None else (expected[0], "room_cooler_by")
    cool = (2.5, "warmer_by") if expected[1] is None else (expected[1], "room_warmer_by")
    _assert_shown(hass, entry, _expect(heat, cool))


@pytest.mark.parametrize(
    ("room", "expected"),
    [
        ({COOLER: 0.1, WARMER: 9.0}, (0.25, 2.5)),
        ({COOLER: 1.5, WARMER: 2.0}, (1.5, 2.0)),
        ({COOLER: 0.25, WARMER: 2.5}, (0.25, 2.5)),
        ({COOLER: "x"}, (None, None)),
        ({}, (None, None)),
    ],
)
async def test_celsius_room_sides_clamp_to_celsius_bounds(
    hass: HomeAssistant, room: dict[str, Any], expected: tuple
) -> None:
    entry = await _setup(hass, rooms=(room, {}), celsius=True)
    coord = entry.runtime_data
    zone = coord.zones[0]
    assert (zone.drift_cooler_by, zone.drift_warmer_by) == expected
    # Blank: the °C Allowed drift default, 0.5.
    in_use = tuple(0.5 if side is None else side for side in expected)
    assert coord.zone_drift_sides(zone) == in_use
    heat = (in_use[0], "allowed_drift" if expected[0] is None else "room_cooler_by")
    cool = (in_use[1], "allowed_drift" if expected[1] is None else "room_warmer_by")
    _assert_shown(hass, entry, _expect(heat, cool, target=20.0))


# --- rooms are independent -----------------------------------------------------


async def test_rooms_are_independent(hass: HomeAssistant) -> None:
    entry = await _setup(
        hass, rooms=({COOLER: 4.0}, {WARMER: 0.5}), stored=GLOBAL_SIDES
    )
    _assert_shown(hass, entry, _expect((4.0, "room_cooler_by"), (5.0, "warmer_by")))
    _assert_shown(
        hass, entry, _expect((3.0, "cooler_by"), (0.5, "room_warmer_by"), target=70.0), 1
    )
    # A drift written to the primary room moves only the primary room.
    await _set_drift(hass, entry, 2.0)
    await _recompute(hass, entry)
    _assert_shown(hass, entry, _expect((4.0, "room_cooler_by"), (2.0, "room")))
    _assert_shown(
        hass, entry, _expect((3.0, "cooler_by"), (0.5, "room_warmer_by"), target=70.0), 1
    )


# --- the room page -------------------------------------------------------------


def _stores(entry) -> tuple[dict[str, Any], dict[str, Any]]:
    return dict(entry.data), dict(entry.options)


def _assert_globals_kept(entry) -> None:
    """The global sides are still stored (a room page must not scrub them),
    and no room-page flow key was stored flat."""
    data, options = _stores(entry)
    assert ({**data, **options}[COOLER], {**data, **options}[WARMER]) == (3.0, 5.0)
    assert (data[COOLER], data[WARMER]) == (3.0, 5.0)
    for store in (data, options):
        assert not {FIELD_COOLER, FIELD_WARMER, SECTION} & set(store)


def _room_record(entry, index: int = 0) -> dict[str, Any]:
    conf = {**entry.data, **entry.options}
    return conf["zones"][index]


@pytest.mark.parametrize(
    ("celsius", "bounds", "unit"),
    [(False, (0.5, 5.0), "°F"), (True, (0.25, 2.5), "°C")],
    ids=["fahrenheit", "celsius"],
)
async def test_room_page_shows_both_blank_with_the_drift_bounds(
    hass: HomeAssistant, celsius: bool, bounds: tuple, unit: str
) -> None:
    entry = await _setup(hass, celsius=celsius)
    result = await open_options_page(hass, entry, "room_1")
    assert result["step_id"] == "room"
    drift = sections(result)[SECTION]
    assert drift.options["collapsed"] is True
    assert [marker.schema for marker in drift.schema.schema] == [FIELD_COOLER, FIELD_WARMER]
    top = [marker.schema for marker in result["data_schema"].schema]
    # In the comfort part of the page: after the hands-off switch, before the
    # sensor and floor sections.
    assert top.index("hands_off") < top.index(SECTION) < top.index("freshness")
    comfort = await open_options_page(hass, entry, "comfort")
    _, allowed = marker_of(comfort, CONF_ENGAGE_DEADBAND)
    for key in (FIELD_COOLER, FIELD_WARMER):
        assert suggested(result, key) is None
        assert key not in prefilled(result)
        _, box = marker_of(result, key)
        assert (box.config["min"], box.config["max"]) == bounds
        assert box.config["unit_of_measurement"] == unit
        assert box.config == allowed.config  # the same box as Allowed drift


async def test_an_untouched_room_save_stores_no_side(hass: HomeAssistant) -> None:
    entry = await _setup(hass, stored=GLOBAL_SIDES)
    before = _room_record(entry)
    await _save_page(hass, entry, "room_1")
    assert _room_record(entry) == before
    _assert_globals_kept(entry)


async def test_room_sides_through_the_real_options_flow(hass: HomeAssistant) -> None:
    """Set, change, and clear a room's sides on its page: the stored record,
    the plan row, and the thermostat follow each save, the global sides stay
    stored, and the other room is untouched."""
    entry = await _setup(hass, stored=GLOBAL_SIDES)
    other_before = _room_record(entry, 1)

    await _save_page(hass, entry, "room_1", **{FIELD_WARMER: 4.0})
    record = _room_record(entry)
    assert record[WARMER] == 4.0 and COOLER not in record
    _assert_shown(hass, entry, _expect((3.0, "cooler_by"), (4.0, "room_warmer_by")))
    _assert_shown(hass, entry, _expect((3.0, "cooler_by"), (5.0, "warmer_by"), 70.0), 1)
    row = _row(hass, entry)
    assert (row["drift_heat"], row["drift_cool"]) == (3.0, 4.0)

    await _save_page(hass, entry, "room_1", **{FIELD_COOLER: 0.5, FIELD_WARMER: 2.0})
    record = _room_record(entry)
    assert (record[COOLER], record[WARMER]) == (0.5, 2.0)
    _assert_shown(hass, entry, _expect((0.5, "room_cooler_by"), (2.0, "room_warmer_by")))

    # The page suggests what is stored; emptying one box clears that side.
    result = await open_options_page(hass, entry, "room_1")
    assert (suggested(result, FIELD_COOLER), suggested(result, FIELD_WARMER)) == (0.5, 2.0)
    shown = prefilled(result)
    del shown[FIELD_COOLER]
    result = await submit(hass.config_entries.options, result, shown)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await _settle(hass, entry)
    record = _room_record(entry)
    assert COOLER not in record and record[WARMER] == 2.0
    _assert_shown(hass, entry, _expect((3.0, "cooler_by"), (2.0, "room_warmer_by")))

    assert _room_record(entry, 1) == other_before
    _assert_globals_kept(entry)


async def test_a_room_save_keeps_a_room_drift_override_and_its_sides(
    hass: HomeAssistant,
) -> None:
    """The page reloads the entry; the drift number's override comes back,
    and the new room side sits beside it."""
    entry = await _setup(hass, stored=GLOBAL_SIDES)
    await _set_drift(hass, entry, 2.0)
    await _save_page(hass, entry, "room_1", **{FIELD_COOLER: 4.0})
    assert entry.runtime_data.zones[0].drift == 2.0
    _assert_shown(hass, entry, _expect((4.0, "room_cooler_by"), (2.0, "room")))


@pytest.mark.parametrize(
    ("celsius", "value"),
    [
        (False, 0.25),
        (False, 5.5),
        (False, -1.0),
        (False, float("inf")),
        (True, 0.2),
        (True, 2.6),
    ],
)
@pytest.mark.parametrize("key", [FIELD_COOLER, FIELD_WARMER])
async def test_out_of_range_room_sides_are_refused(
    hass: HomeAssistant, celsius: bool, value: float, key: str
) -> None:
    entry = await _setup(hass, celsius=celsius)
    before = _stores(entry)
    result = await open_options_page(hass, entry, "room_1")
    with pytest.raises(vol.Invalid):
        await submit(
            hass.config_entries.options, result, {**prefilled(result), key: value}
        )
    assert _stores(entry) == before


@pytest.mark.parametrize(
    ("celsius", "value"), [(False, 0.5), (False, 5.0), (True, 0.25), (True, 2.5)]
)
async def test_the_bounds_themselves_are_accepted_on_the_room_page(
    hass: HomeAssistant, celsius: bool, value: float
) -> None:
    entry = await _setup(hass, celsius=celsius)
    await _save_page(
        hass, entry, "room_1", **{FIELD_COOLER: value, FIELD_WARMER: value}
    )
    record = _room_record(entry)
    assert record[COOLER] == record[WARMER] == value


@pytest.mark.parametrize("key", [FIELD_COOLER, FIELD_WARMER])
async def test_nan_is_refused_on_the_room_drift_section(
    hass: HomeAssistant, key: str
) -> None:
    entry = await _setup(hass)
    before = _stores(entry)
    result = await open_options_page(hass, entry, "room_1")
    result = await submit(
        hass.config_entries.options, result, {**prefilled(result), key: float("nan")}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {SECTION: "not_a_number"}
    assert sections(result)[SECTION].options["collapsed"] is False
    assert _stores(entry) == before


# --- Needs attention -------------------------------------------------------------


@pytest.mark.parametrize(
    ("room", "expected"),
    [
        (
            {COOLER: "warm"},
            (
                'the saved "Let it get cooler by" for Primary isn\'t a number.'
                " Open Rooms, then Primary, to fix it."
            ),
        ),
        (
            {WARMER: float("nan")},
            (
                'the saved "Let it get warmer by" for Primary isn\'t a number.'
                " Open Rooms, then Primary, to fix it."
            ),
        ),
        (
            {WARMER: -1.0},
            (
                'the saved "Let it get warmer by" for Primary is negative.'
                " Open Rooms, then Primary, to fix it."
            ),
        ),
    ],
    ids=["text", "nan", "negative"],
)
async def test_menu_attention_names_a_bad_room_side(
    hass: HomeAssistant, room: dict[str, Any], expected: str
) -> None:
    """Setup did not fail on it (the side reads as blank), and the menu says
    which room and where to fix it."""
    entry = await _setup(hass, rooms=(room, {}))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"]["attention"] == (
        f"**Needs attention:** {expected}"
    )


async def test_menu_attention_lists_each_bad_side_and_room(hass: HomeAssistant) -> None:
    entry = await _setup(hass, rooms=({COOLER: "a", WARMER: "b"}, {WARMER: "c"}))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"]["attention"] == (
        "**Needs attention:**\n"
        '- the saved "Let it get cooler by" for Primary isn\'t a number.'
        " Open Rooms, then Primary, to fix it.\n"
        '- the saved "Let it get warmer by" for Primary isn\'t a number.'
        " Open Rooms, then Primary, to fix it.\n"
        '- the saved "Let it get warmer by" for Secondary isn\'t a number.'
        " Open Rooms, then Secondary, to fix it."
    )


@pytest.mark.parametrize(
    "room", [{}, {COOLER: 0.5, WARMER: 5.0}, {COOLER: 40.0}, {COOLER: "3"}],
    ids=["blank", "bounds", "clamped", "numeric-text"],
)
async def test_menu_attention_is_quiet_for_usable_room_sides(
    hass: HomeAssistant, room: dict[str, Any]
) -> None:
    """Negative control: a value load can use (even one it clamps, like the
    global sides) is not named."""
    entry = await _setup(hass, rooms=(room, {}))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"]["attention"] == ""


# --- the drift number shows the sides in use, read-only -------------------------


async def test_the_drift_number_lists_the_sides_in_use(hass: HomeAssistant) -> None:
    entry = await _setup(hass, rooms=({WARMER: 4.5}, {}), stored=GLOBAL_SIDES)
    shown = _number(hass, entry)
    # The value is still Allowed drift (following); the sides are separate.
    assert float(shown.state) == 1.0
    assert (shown.attributes["drift_heat"], shown.attributes["drift_cool"]) == (3.0, 4.5)
    assert shown.attributes["override"] is False

    await _set_drift(hass, entry, 2.0)
    await _recompute(hass, entry)
    shown = _number(hass, entry)
    assert float(shown.state) == 2.0
    assert (shown.attributes["drift_heat"], shown.attributes["drift_cool"]) == (2.0, 4.5)
    row = _row(hass, entry)
    assert (row["drift_heat"], row["drift_cool"]) == (2.0, 4.5)

    await _press_follow_global(hass, entry)
    shown = _number(hass, entry)
    assert (shown.attributes["drift_heat"], shown.attributes["drift_cool"]) == (3.0, 4.5)
    assert shown.attributes["override"] is False
    # The other room has no room sides: the global sides.
    other = _number(hass, entry, "secondary")
    assert (other.attributes["drift_heat"], other.attributes["drift_cool"]) == (3.0, 5.0)


async def test_restore_ignores_the_side_attributes(hass: HomeAssistant) -> None:
    """Only ``override`` decides a restore; stale side values are not read."""
    entry = await _setup(
        hass, restore=(3.0, {"override": False, "drift_heat": 4.0, "drift_cool": 4.0})
    )
    assert entry.runtime_data.zones[0].drift is None
    shown = _number(hass, entry)
    assert (shown.attributes["drift_heat"], shown.attributes["drift_cool"]) == (1.0, 1.0)


# --- the words on the room page ---------------------------------------------------


def _strings(name: str) -> dict[str, Any]:
    return json.loads((Path(component.__file__).parent / name).read_text("utf-8"))


ROOM_SECTION = {
    "name": "Uneven drift",
    "description": (
        "For each side, MXZ uses the first of these that's set: this page, this"
        " room's drift number (once you or an automation set it; Follow global"
        " drift clears it), Comfort's \"Let it get cooler by\" or \"Let it get"
        " warmer by\", and then Allowed drift. The drift number's drift_heat and"
        " drift_cool attributes show the drift in use on each side."
    ),
    "data": {
        FIELD_COOLER: "Let it get cooler by",
        FIELD_WARMER: "Let it get warmer by",
    },
    "data_description": {
        FIELD_COOLER: (
            "How far below target this room can get before heating starts."
            " 0.5-5 °F (0.25-2.5 °C)."
        ),
        FIELD_WARMER: (
            "How far above target this room can get before cooling starts."
            " 0.5-5 °F (0.25-2.5 °C)."
        ),
    },
}


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json"])
def test_the_room_page_words(name: str) -> None:
    options = _strings(name)["options"]
    assert options["step"]["room"]["sections"][SECTION] == ROOM_SECTION
    # The page's error is the one Comfort already shows.
    assert options["error"]["not_a_number"] == "Enter a number."
    # The labels are the global ones, word for word.
    advanced = options["step"]["comfort"]["sections"]["advanced"]["data"]
    assert ROOM_SECTION["data"] == {
        FIELD_COOLER: advanced[COOLER], FIELD_WARMER: advanced[WARMER],
    }


async def test_the_readme_example(hass: HomeAssistant) -> None:
    """Per-room uneven drift's example: Comfort cooler by 3 and warmer by 5,
    the room's own warmer by 2, then 1.5 written to its drift number."""
    entry = await _setup(hass, rooms=({WARMER: 2.0}, {}), stored=GLOBAL_SIDES)
    _assert_shown(hass, entry, _expect((3.0, "cooler_by"), (2.0, "room_warmer_by")))
    await _set_drift(hass, entry, 1.5)
    await _recompute(hass, entry)
    _assert_shown(hass, entry, _expect((1.5, "room"), (2.0, "room_warmer_by")))


async def test_room_sides_move_with_their_room_on_a_reorder(hass: HomeAssistant) -> None:
    """Reconfigure keeps a kept head's record, so its sides stay with it."""
    heads, sensors, entry = await _setup_rooms(hass)
    await _save_page(hass, entry, "room_1", **{FIELD_WARMER: 4.0})  # Bedroom
    await _reconfigure(hass, entry, heads[::-1], sensors[::-1])
    await _recompute(hass, entry)
    office, bedroom = entry.runtime_data.zones
    assert (office.name, bedroom.name) == ("Office", "Bedroom")
    assert (office.drift_warmer_by, bedroom.drift_warmer_by) == (None, 4.0)
    assert _row(hass, entry, 0)["drift_cool_from"] == "allowed_drift"
    assert _row(hass, entry, 1)["drift_cool_from"] == "room_warmer_by"
    assert _row(hass, entry, 1)["drift_source"] == "room"


async def test_a_stray_flat_room_page_key_is_scrubbed_on_save(hass: HomeAssistant) -> None:
    """The room page's flow keys are never stored flat: a stray copy (a hand
    edit, say) is dropped by the next save, and the global sides are kept."""
    entry = await _setup(hass, stored=GLOBAL_SIDES)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, FIELD_COOLER: 4.0, FIELD_WARMER: 4.0, SECTION: {}}
    )
    await _settle(hass, entry)
    await _save_page(hass, entry, "room_1")
    _assert_globals_kept(entry)
    _assert_shown(hass, entry, _expect((3.0, "cooler_by"), (5.0, "warmer_by")))


async def test_the_drift_number_redraws_when_only_the_sides_change(
    hass: HomeAssistant,
) -> None:
    """The sides are in the number's redraw signature, like its value: a
    change to them alone is published once, and an unchanged refresh is not."""
    entry = await _setup(hass)
    coord = entry.runtime_data
    number = hass.data["entity_components"]["number"].get_entity(
        _eid(hass, entry, "_primary_drift")
    )
    with patch.object(number, "async_write_ha_state") as write_state:
        coord.drift_warmer_by = 4.0  # moves a side, not the value or the flag
        coord.async_update_listeners()
        assert write_state.call_count == 1
        for _ in range(5):
            await coord.async_refresh()
        assert write_state.call_count == 1
    assert number.extra_state_attributes["drift_cool"] == 4.0
