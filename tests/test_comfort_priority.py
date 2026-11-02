"""Comfort priority through the real coordinator and options flow.

Two mock heads on one outdoor unit. The standoff used throughout: the
primary room (first in the room order) is hot and calls cool, the secondary
is cold and calls heat. Today the primary wins; a Comfort priority entity on
the secondary can move it ahead, and nothing else.

Every room and entity here is invented.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator import coordinator as coordinator_module
from custom_components.mxz_coordinator.const import (
    CONF_COMFORT_STALE_HOURS,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    MODE_COOL,
    MODE_HEAT,
    ZONE_CLIMATE,
    ZONE_COMFORT_PRIORITY_ENTITY,
    ZONE_FLOOR_LOW,
    ZONE_NAME,
    ZONE_SAFETY_PRIORITY_ENTITY,
    ZONE_SENSOR,
)
from tests.clocks import jump_wall_clock
from tests.dwell import expire_dwell, record_mode_change
from tests.flow_pages import (
    fields,
    marker_of,
    open_options_page,
    prefilled,
    save_options,
    submit,
)
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_target,
    _set_temp,
    _setup_mock_heads,
)
from tests.test_dwell_wakeup import _count_refresh_requests, _fire_due, _quiesce
from tests.test_room_rename_reorder import _reconfigure, _setup_rooms
from tests.test_startup_continuity import _switch

OCCUPIED = "binary_sensor.den_occupied"
HOUR = 3600.0
ROW_KEYS = ("comfort_priority_entity", "comfort_priority", "comfort_priority_status")


async def _setup(
    hass: HomeAssistant,
    *,
    comfort: tuple[str | None, str | None] = (None, OCCUPIED),
    hysteresis: int | None = 0,
    safety: str | None = None,
    **extra: Any,
) -> tuple[MockConfigEntry, str, str]:
    """Two enabled rooms at 70 / target 70. ``comfort`` is each room's entity.

    ``hysteresis=None`` runs the default wait and stores a mode change just
    before setup, as a restart finds it, so the wait holds from the start.
    ``safety`` is the secondary's Safety priority sensor, if any.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    zones = []
    for name, head, sensor, entity in (
        ("Primary", head_a, SENSOR_A, comfort[0]),
        ("Secondary", head_b, SENSOR_B, comfort[1]),
    ):
        zone = {ZONE_NAME: name, ZONE_CLIMATE: head, ZONE_SENSOR: sensor}
        if entity:
            zone[ZONE_COMFORT_PRIORITY_ENTITY] = entity
        zones.append(zone)
    if safety:
        zones[1][ZONE_SAFETY_PRIORITY_ENTITY] = safety
    data = {CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY, CONF_ZONES: zones, **extra}
    if hysteresis is not None:
        data[CONF_MODE_HYSTERESIS] = hysteresis
    entry = MockConfigEntry(domain=DOMAIN, title="MXZ Coordinator", data=data, version=2)
    entry.add_to_hass(hass)
    if hysteresis is None:
        await record_mode_change(hass, entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await _switch(hass, _eid(hass, entry, suffix), True)
    for suffix in ("_primary_target", "_secondary_target"):
        await _set_target(hass, _eid(hass, entry, suffix), 70)
    await _recompute(hass, entry)
    return entry, head_a, head_b


async def _standoff(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Primary calls cool, secondary calls heat."""
    await _set_temp(hass, SENSOR_A, 76)
    await _set_temp(hass, SENSOR_B, 64)
    await _recompute(hass, entry)


def _plan(hass: HomeAssistant, entry: MockConfigEntry):
    return hass.states.get(_eid(hass, entry, "_plan"))


def _row(hass: HomeAssistant, entry: MockConfigEntry, room: int) -> tuple[Any, ...]:
    row = _plan(hass, entry).attributes["zones"][room]
    return tuple(row[key] for key in ROW_KEYS)


def _zones(entry: MockConfigEntry) -> list[dict[str, Any]]:
    """The room records the coordinator loads (options override data)."""
    return {**entry.data, **entry.options}[CONF_ZONES]


async def _set(hass: HomeAssistant, entity_id: str, state: str) -> None:
    hass.states.async_set(entity_id, state)
    await hass.async_block_till_done()


# --- off by default ----------------------------------------------------------


async def test_no_entity_set_decides_by_room_order_and_says_so(hass: HomeAssistant) -> None:
    entry, head_a, _b = await _setup(hass, comfort=(None, None))
    await _standoff(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_COOL  # the primary wins, as always
    assert hass.states.get(head_a).state == MODE_COOL
    assert plan.attributes["standoff"] is True
    assert plan.attributes["mode_by_comfort_priority"] is False
    assert plan.attributes["mode_by_safety"] is False
    assert plan.attributes["mode_by_hands_off"] is False
    assert [_row(hass, entry, i) for i in (0, 1)] == [(None, 0.0, "not_set")] * 2
    assert all(ZONE_COMFORT_PRIORITY_ENTITY not in z for z in _zones(entry))
    coord = entry.runtime_data
    assert [z.comfort_priority_entity for z in coord.zones] == [None, None]
    assert coord._comfort_timer is None


# --- every accepted domain ---------------------------------------------------


@pytest.mark.parametrize(
    ("entity_id", "state", "boost", "status"),
    [
        ("binary_sensor.den_occupied", "on", 1.0, "on"),
        ("input_boolean.den_guest", "on", 1.0, "on"),
        ("number.den_priority", "2", 2.0, "number"),
        ("input_number.den_priority", "1.5", 1.5, "number"),
        ("sensor.den_people", "3", 3.0, "number"),
    ],
)
async def test_each_accepted_domain_moves_the_room_ahead(
    hass: HomeAssistant, entity_id: str, state: str, boost: float, status: str
) -> None:
    hass.states.async_set(entity_id, state)
    entry, head_a, head_b = await _setup(hass, comfort=(None, entity_id))
    await _standoff(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_HEAT  # the boosted secondary wins
    assert hass.states.get(head_b).state == MODE_HEAT
    assert hass.states.get(head_a).state != MODE_COOL  # the primary's cool is denied
    assert _row(hass, entry, 1) == (entity_id, boost, status)
    assert _row(hass, entry, 0) == (None, 0.0, "not_set")
    assert plan.attributes["mode_by_comfort_priority"] is True


# --- bad readings: no boost, a normal decision ------------------------------


@pytest.mark.parametrize(
    ("entity_id", "state", "status"),
    [
        ("binary_sensor.den_occupied", "unavailable", "unavailable"),
        ("binary_sensor.den_occupied", "unknown", "unknown"),
        ("binary_sensor.den_occupied", None, "missing"),
        ("binary_sensor.den_occupied", "off", "off"),
        ("sensor.den_people", "occupied", "not_a_number"),
        ("sensor.den_people", "nan", "not_a_number"),
        ("number.den_priority", "inf", "not_a_number"),
        ("number.den_priority", "-2", "number"),
        ("input_number.den_priority", "0", "number"),
    ],
)
async def test_a_bad_reading_is_no_boost_and_a_normal_decision(
    hass: HomeAssistant, entity_id: str, state: str | None, status: str
) -> None:
    if state is not None:
        hass.states.async_set(entity_id, state)
    entry, head_a, _b = await _setup(hass, comfort=(None, entity_id))
    await _standoff(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_COOL  # room order, as if nothing were set
    assert hass.states.get(head_a).state == MODE_COOL
    assert _row(hass, entry, 1) == (entity_id, 0.0, status)
    assert plan.attributes["mode_by_comfort_priority"] is False


async def test_a_bad_reading_on_the_top_room_never_pushes_it_down(hass: HomeAssistant) -> None:
    """A negative number on the primary is 0, not last: the primary still wins."""
    hass.states.async_set("number.den_priority", "-5")
    entry, _a, _b = await _setup(hass, comfort=("number.den_priority", None))
    await _standoff(hass, entry)
    assert _plan(hass, entry).state == MODE_COOL
    assert _row(hass, entry, 0) == ("number.den_priority", 0.0, "number")


# --- what beats it -----------------------------------------------------------


async def test_safety_priority_beats_comfort_priority(hass: HomeAssistant) -> None:
    """The primary is boosted; the secondary's Safety priority sensor still wins."""
    hass.states.async_set("number.den_priority", "9")
    hass.states.async_set("binary_sensor.den_safety", "on")
    entry, _a, _b = await _setup(
        hass, comfort=("number.den_priority", None), safety="binary_sensor.den_safety"
    )
    await _standoff(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_HEAT
    assert plan.attributes["mode_by_safety"] is True
    assert plan.attributes["mode_by_comfort_priority"] is False


async def test_a_freeze_floor_beats_a_boosted_cool_demand(hass: HomeAssistant) -> None:
    """A disabled room at its freeze floor heats, however boosted the cool room is."""
    hass.states.async_set("number.den_priority", "9")
    entry, _a, _b = await _setup(hass, comfort=("number.den_priority", None))
    zones = [dict(z) for z in _zones(entry)]
    zones[1][ZONE_FLOOR_LOW] = 50.0
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONES: zones})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_coordinator_enable"):
        await _switch(hass, _eid(hass, entry, suffix), True)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _set_temp(hass, SENSOR_A, 76)
    await _set_temp(hass, SENSOR_B, 45)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.attributes["zones"][1]["floor_want"] == MODE_HEAT
    assert plan.state == MODE_HEAT
    assert plan.attributes["mode_by_floor"] is True
    assert plan.attributes["mode_by_comfort_priority"] is False


# --- reacting at once, and the wait ------------------------------------------


async def test_a_state_change_recomputes_at_once(hass: HomeAssistant) -> None:
    hass.states.async_set(OCCUPIED, "off")
    entry, _a, head_b = await _setup(hass)
    coord = entry.runtime_data
    await _standoff(hass, entry)
    await _quiesce(hass, coord)
    assert _plan(hass, entry).state == MODE_COOL
    with _count_refresh_requests(coord) as requests:
        await _set(hass, OCCUPIED, "on")
    # The entity's own listener asked. (On HA 2024.12 the refresh guard may queue
    # a follow-up request of its own when the recompute's commands echo back.)
    assert requests[:1] == ["request"]
    assert _plan(hass, entry).state == MODE_HEAT  # no _recompute: it already ran
    assert hass.states.get(head_b).state == MODE_HEAT
    assert _row(hass, entry, 1) == (OCCUPIED, 1.0, "on")


async def test_a_boosted_flip_held_by_the_wait_arms_the_dwell_wakeup(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(OCCUPIED, "off")
    entry, _a, _b = await _setup(hass, hysteresis=None)
    coord = entry.runtime_data
    await _standoff(hass, entry)
    assert _plan(hass, entry).state == MODE_COOL
    assert coord._dwell_timer is None  # room order already answers cool: nothing waits

    await _set(hass, OCCUPIED, "on")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_COOL  # the wait holds the boosted heat back
    assert plan.attributes["mode_change_allowed"] is False
    assert plan.attributes["mode_by_comfort_priority"] is False  # it changed nothing yet
    assert coord._dwell_timer is not None  # the wakeup replays the same boost

    await _set(hass, OCCUPIED, "off")
    await _recompute(hass, entry)
    assert coord._dwell_timer is None  # the boost is gone, so is the wait's reason

    await _set(hass, OCCUPIED, "on")
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_HEAT  # once the wait ends, the flip happens

    # That flip started a new wait. While it holds heat, heat would be the answer
    # with or without the boost, so the flag claims nothing.
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_change_allowed"]) == (MODE_HEAT, False)
    assert plan.attributes["mode_by_comfort_priority"] is False
    # Once it ends, room order alone would go back to cool: the boost keeps heat.
    expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_HEAT
    assert plan.attributes["mode_by_comfort_priority"] is True


async def test_restart_with_the_entity_still_loading_keeps_the_restored_hold(
    hass: HomeAssistant,
) -> None:
    """The entity is unavailable at boot and a hold was restored: no flip. Once
    it loads, the boost waits for the hold, then applies."""
    hass.states.async_set(OCCUPIED, "unavailable")
    entry, head_a, _b = await _setup(hass, hysteresis=None)
    coord = entry.runtime_data
    await _standoff(hass, entry)
    assert _plan(hass, entry).state == MODE_COOL
    assert _row(hass, entry, 1) == (OCCUPIED, 0.0, "unavailable")

    await _set(hass, OCCUPIED, "on")  # the entity loads; its listener recomputes
    plan = _plan(hass, entry)
    assert _row(hass, entry, 1) == (OCCUPIED, 1.0, "on")
    assert plan.state == MODE_COOL  # the restored hold still holds
    assert plan.attributes["mode_change_allowed"] is False
    assert hass.states.get(head_a).state == MODE_COOL
    assert coord._dwell_timer is not None

    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_HEAT


# --- mode_by_comfort_priority claims only what it earned ---------------------


async def test_the_flag_is_false_when_the_boosted_room_would_win_anyway(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(OCCUPIED, "on")
    entry, _a, _b = await _setup(hass, comfort=(OCCUPIED, None))
    await _standoff(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_COOL
    assert _row(hass, entry, 0) == (OCCUPIED, 1.0, "on")  # the boost is there...
    assert plan.attributes["mode_by_comfort_priority"] is False  # ...but earned nothing


async def test_the_flag_is_false_for_a_boosted_room_that_wants_nothing(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(OCCUPIED, "on")
    entry, _a, _b = await _setup(hass)
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_COOL
    assert plan.attributes["mode_by_comfort_priority"] is False


# --- the stale "on" age limit -------------------------------------------------


async def test_the_default_limit_is_twelve_hours(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup(hass)
    assert CONF_COMFORT_STALE_HOURS not in entry.data
    assert CONF_COMFORT_STALE_HOURS not in entry.options
    assert entry.runtime_data.comfort_stale_after == 12 * HOUR


@pytest.mark.parametrize(
    ("stored", "seconds"),
    [(0, None), (6, 6 * HOUR), (168, 168 * HOUR), (500, 168 * HOUR), (-3, None),
     ("junk", 12 * HOUR), (float("nan"), 12 * HOUR), (True, 12 * HOUR)],
)
async def test_a_stored_limit_is_read_safely(
    hass: HomeAssistant, stored: Any, seconds: float | None
) -> None:
    entry, _a, _b = await _setup(hass, **{CONF_COMFORT_STALE_HOURS: stored})
    assert entry.runtime_data.comfort_stale_after == seconds


async def test_fresh_just_under_and_just_over_the_limit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass.states.async_set(OCCUPIED, "on")
    entry, _a, head_b = await _setup(hass)
    await _standoff(hass, entry)
    assert _row(hass, entry, 1) == (OCCUPIED, 1.0, "on")  # fresh
    assert _plan(hass, entry).state == MODE_HEAT

    jump_wall_clock(monkeypatch, 12 * HOUR - 60)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1) == (OCCUPIED, 1.0, "on")  # just under
    assert _plan(hass, entry).state == MODE_HEAT

    jump_wall_clock(monkeypatch, 120)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1) == (OCCUPIED, 0.0, "stale")  # just over
    plan = _plan(hass, entry)
    assert plan.state == MODE_COOL  # back to room order, through the normal wait (0 here)
    assert plan.attributes["mode_by_comfort_priority"] is False
    assert entry.runtime_data._comfort_timer is None  # nothing left to go stale

    # Home Assistant stamps a new state from its own clock, not the stepped
    # utcnow above, so step back before writing one.
    monkeypatch.undo()
    await _set(hass, OCCUPIED, "off")
    await _set(hass, OCCUPIED, "on")  # a new "on" counts again
    await _recompute(hass, entry)
    assert _row(hass, entry, 1) == (OCCUPIED, 1.0, "on")
    assert hass.states.get(head_b).state == MODE_HEAT


async def test_zero_means_no_limit(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    hass.states.async_set(OCCUPIED, "on")
    entry, _a, _b = await _setup(hass, **{CONF_COMFORT_STALE_HOURS: 0})
    await _standoff(hass, entry)
    jump_wall_clock(monkeypatch, 1000 * HOUR)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1) == (OCCUPIED, 1.0, "on")
    assert _plan(hass, entry).state == MODE_HEAT
    assert entry.runtime_data._comfort_timer is None


@pytest.mark.parametrize("entity_id", ["number.den_priority", "input_number.den_priority", "sensor.den_people"])
async def test_numbers_never_go_stale(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, entity_id: str
) -> None:
    hass.states.async_set(entity_id, "1")
    entry, _a, _b = await _setup(hass, comfort=(None, entity_id))
    await _standoff(hass, entry)
    assert entry.runtime_data._comfort_timer is None
    jump_wall_clock(monkeypatch, 1000 * HOUR)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1) == (entity_id, 1.0, "number")
    assert _plan(hass, entry).state == MODE_HEAT


def _count_comfort_wakeups(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record each stale-limit wakeup that actually runs (its delay), from here on."""
    fired: list[float] = []
    real = coordinator_module.async_call_later

    def arming(hass: HomeAssistant, delay: float, action: Any) -> Any:
        if "_sync_comfort_timer" in getattr(action, "__qualname__", ""):
            inner = action

            @callback
            def action(now: Any) -> None:
                fired.append(delay)
                inner(now)

        return real(hass, delay, action)

    monkeypatch.setattr(coordinator_module, "async_call_later", arming)
    return fired


async def test_the_stale_moment_wakes_a_recompute_by_itself(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No other event needed: one wakeup at the stale moment drops the boost."""
    hass.states.async_set(OCCUPIED, "on")
    entry, _a, _b = await _setup(hass, **{CONF_COMFORT_STALE_HOURS: 1})
    coord = entry.runtime_data
    fired = _count_comfort_wakeups(monkeypatch)
    await _standoff(hass, entry)
    await _quiesce(hass, coord)
    assert coord._comfort_timer is not None
    assert _plan(hass, entry).state == MODE_HEAT
    jump_wall_clock(monkeypatch, HOUR + 1)
    with _count_refresh_requests(coord) as requests:
        await _fire_due(hass, HOUR + 1)
    assert len(fired) >= 1
    assert all(0 < delay <= HOUR for delay in fired)
    assert "request" in requests
    assert _row(hass, entry, 1) == (OCCUPIED, 0.0, "stale")
    assert _plan(hass, entry).state == MODE_COOL
    assert coord._comfort_timer is None


async def test_unload_cancels_the_stale_wakeup(hass: HomeAssistant) -> None:
    hass.states.async_set(OCCUPIED, "on")
    entry, _a, _b = await _setup(hass)
    coord = entry.runtime_data
    await _standoff(hass, entry)
    assert coord._comfort_timer is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coord._comfort_timer is None
    coord._sync_comfort_timer()  # a late re-sync arms nothing on a retired one
    assert coord._comfort_timer is None


async def test_the_age_survives_a_reload_and_restarts_with_a_new_state(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MXZ stores nothing: the age is Home Assistant's own last_changed.

    A reload keeps the state machine, so a stale "on" is still stale after it.
    A Home Assistant restart writes the entity's state anew at boot (its
    last_changed is then the boot), which is modelled here by removing and
    setting the state: the age counts from there, so the on counts again.
    """
    hass.states.async_set(OCCUPIED, "on")
    entry, _a, _b = await _setup(hass)
    await _standoff(hass, entry)
    jump_wall_clock(monkeypatch, 13 * HOUR)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1) == (OCCUPIED, 0.0, "stale")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _row(hass, entry, 1) == (OCCUPIED, 0.0, "stale")
    assert _plan(hass, entry).state == MODE_COOL

    monkeypatch.undo()  # the boot writes the state on Home Assistant's own clock
    hass.states.async_remove(OCCUPIED)
    await _set(hass, OCCUPIED, "on")
    await _recompute(hass, entry)
    assert _row(hass, entry, 1) == (OCCUPIED, 1.0, "on")
    assert _plan(hass, entry).state == MODE_HEAT


# --- the options flow ---------------------------------------------------------


async def test_the_room_field_appears_saves_and_clears(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup(hass, comfort=(None, None))
    manager = hass.config_entries.options
    result = await open_options_page(hass, entry, "room_2")
    shown = fields(result)
    assert shown.index("comfort_priority_entity") == shown.index("safety_priority_entity") + 1
    result = await submit(manager, result, {**prefilled(result), "comfort_priority_entity": OCCUPIED})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()  # the option change reloads the entry
    assert _zones(entry)[1][ZONE_COMFORT_PRIORITY_ENTITY] == OCCUPIED
    assert entry.runtime_data.zones[1].comfort_priority_entity == OCCUPIED

    result = await open_options_page(hass, entry, "room_2")
    values = prefilled(result)
    assert values["comfort_priority_entity"] == OCCUPIED  # shown as wired
    values.pop("comfort_priority_entity")  # the user clears it
    result = await submit(manager, result, values)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert ZONE_COMFORT_PRIORITY_ENTITY not in _zones(entry)[1]
    assert entry.runtime_data.zones[1].comfort_priority_entity is None


@pytest.mark.parametrize("entity_id", ["switch.den_fan", "climate.den", "light.den", "person.someone"])
async def test_the_selector_rejects_an_unsupported_domain(
    hass: HomeAssistant, entity_id: str
) -> None:
    entry, _a, _b = await _setup(hass, comfort=(None, None))
    before = [dict(z) for z in _zones(entry)]
    result = await open_options_page(hass, entry, "room_2")
    with pytest.raises(vol.Invalid):
        await submit(
            hass.config_entries.options, result,
            {**prefilled(result), "comfort_priority_entity": entity_id},
        )
    assert _zones(entry) == before


@pytest.mark.parametrize(
    "entity_id",
    ["binary_sensor.den_occupied", "input_boolean.den_guest", "number.den_priority",
     "input_number.den_priority", "sensor.den_people"],
)
async def test_the_selector_accepts_every_supported_domain(
    hass: HomeAssistant, entity_id: str
) -> None:
    entry, _a, _b = await _setup(hass, comfort=(None, None))
    result = await open_options_page(hass, entry, "room_1")
    _marker, selector = marker_of(result, "comfort_priority_entity")
    assert selector(entity_id) == entity_id


async def test_an_untouched_room_gains_no_key(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup(hass, comfort=(None, None))
    before = [dict(z) for z in _zones(entry)]
    result = await open_options_page(hass, entry, "room_1")
    result = await submit(hass.config_entries.options, result, prefilled(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert _zones(entry) == before  # byte-for-byte the same records
    assert all(ZONE_COMFORT_PRIORITY_ENTITY not in z for z in _zones(entry))


async def test_a_reorder_carries_the_setting_with_its_room(hass: HomeAssistant) -> None:
    heads, sensors, entry = await _setup_rooms(hass)
    result = await open_options_page(hass, entry, "room_1")
    result = await submit(
        hass.config_entries.options, result,
        {**prefilled(result), "comfort_priority_entity": OCCUPIED},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert _zones(entry)[0][ZONE_COMFORT_PRIORITY_ENTITY] == OCCUPIED

    await _reconfigure(hass, entry, heads[::-1], sensors[::-1])

    zones = _zones(entry)
    assert [z[ZONE_CLIMATE] for z in zones] == heads[::-1]
    assert zones[1][ZONE_COMFORT_PRIORITY_ENTITY] == OCCUPIED  # moved with its head
    assert ZONE_COMFORT_PRIORITY_ENTITY not in zones[0]
    coord = entry.runtime_data
    assert [z.comfort_priority_entity for z in coord.zones] == [None, OCCUPIED]


async def test_the_stale_limit_option_saves_zero_and_blank_clears_it(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup(hass)
    result = await save_options(hass, entry, {CONF_COMFORT_STALE_HOURS: 0})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_COMFORT_STALE_HOURS] == 0
    assert entry.runtime_data.comfort_stale_after is None

    result = await save_options(hass, entry, {CONF_COMFORT_STALE_HOURS: 6})
    await hass.async_block_till_done()
    assert entry.runtime_data.comfort_stale_after == 6 * HOUR

    result = await open_options_page(hass, entry, "comfort")
    values = prefilled(result)
    assert values[CONF_COMFORT_STALE_HOURS] == 6
    values.pop(CONF_COMFORT_STALE_HOURS)  # cleared
    result = await submit(hass.config_entries.options, result, values)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert CONF_COMFORT_STALE_HOURS not in entry.options
    assert CONF_COMFORT_STALE_HOURS not in entry.data
    assert entry.runtime_data.comfort_stale_after == 12 * HOUR


@pytest.mark.parametrize("value", [-1, 169, float("inf"), float("nan")])
async def test_an_out_of_range_stale_limit_is_refused(hass: HomeAssistant, value: float) -> None:
    entry, _a, _b = await _setup(hass)
    before = (dict(entry.data), dict(entry.options))
    result = await open_options_page(hass, entry, "comfort")
    try:
        result = await submit(
            hass.config_entries.options, result,
            {**prefilled(result), CONF_COMFORT_STALE_HOURS: value},
        )
    except vol.Invalid:
        pass
    else:
        assert result["type"] is FlowResultType.FORM  # shown again with an error
        assert result["errors"]
    assert (dict(entry.data), dict(entry.options)) == before


def test_the_wall_clock_helper_moves_utcnow() -> None:
    """Guard for the stale tests above: they lean on jump_wall_clock moving utcnow."""
    mp = pytest.MonkeyPatch()
    try:
        before = dt_util.utcnow()
        jump_wall_clock(mp, HOUR)
        assert dt_util.utcnow() - before >= timedelta(seconds=HOUR - 1)
    finally:
        mp.undo()
