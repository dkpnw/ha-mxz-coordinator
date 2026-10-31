"""3.16.0: safer defaults for NEW rooms (DESIGN.md "Tests to add").

A room created by setup or Reconfigure stores a 45 °F (7 °C) freeze floor, a
360 min maximum age and the ha_state_write evidence basis, as ordinary values.
Nothing supplies them at runtime: a room stored before 3.16.0 has no keys and
keeps none, whatever page is saved. Every behaviour test here runs on an entry
the real flow created, so it fails if the flow stops writing the defaults.

Entities and readings are invented; head commands are observed as service
calls on mock heads. Nothing here talks to a real device.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.mxz_coordinator.const import (
    CONF_DEMAND_THRESHOLD,
    CONF_IDLE_ACTION,
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    ZONE_CLIMATE,
    ZONE_EVIDENCE_BASIS,
    ZONE_FLOOR_HIGH,
    ZONE_FLOOR_LOW,
    ZONE_MAX_AGE,
    ZONE_NAME,
    ZONE_REPORT_INTERVAL,
    ZONE_SENSOR,
    ZONE_STARTUP_GRACE,
)
from tests.flow_pages import (
    GLOBAL_PAGES,
    default,
    open_options_page,
    prefilled,
    setup_tuning,
    submit,
    suggested,
)
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    MockHead,
    MockHeadC,
    _eid,
    _recompute,
    _set_target,
    _set_temp,
    _setup_mock_heads,
    _user_set_fan,
)
from tests.test_dry_mode import DryHead
from tests.test_floor import Floor, _tick
from tests.test_hands_off import _remote, _s
from tests.test_startup_continuity import (
    _fan_hold,
    _fan_writes,
    _shutdown,
    _startup,
    _switch,
)

NOTIFY = "notify.floor_test"
SAFETY_A = "binary_sensor.primary_safety"
SPARE = "climate.spare"
SPARE_SENSOR = "sensor.spare_temp"
# Independent oracles, written out: what DESIGN.md says a new room stores.
FLOOR = {False: 45.0, True: 7.0}
MAX_AGE = 360.0
SIX_HOURS = timedelta(hours=6)
# The keys only the new-room defaults put in a room record.
DEFAULT_KEYS = (ZONE_FLOOR_LOW, ZONE_MAX_AGE, ZONE_EVIDENCE_BASIS)
FRESHNESS_KEYS = (ZONE_REPORT_INTERVAL, ZONE_MAX_AGE, ZONE_STARTUP_GRACE, ZONE_EVIDENCE_BASIS)


async def _install(
    hass: HomeAssistant,
    *,
    metric: bool = False,
    head_cls: type[MockHead] | None = None,
    enable: tuple[str, ...] = ("_primary_enable", "_coordinator_enable"),
) -> Floor:
    """Install two mock heads through the real setup flow, the way a person
    does (heads, rooms, sensors, review, Save and finish).

    The only answers changed from the defaults are a zero mode hysteresis and
    the fan-only idle action, so a test's mode flip lands in one compute and
    a parked head reads ``fan_only``. Then the rooms in ``enable`` are
    switched on at their target; every other room is disabled.
    """
    hass.config.units = METRIC_SYSTEM if metric else US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(
        hass, cls=head_cls or (MockHeadC if metric else MockHead)
    )
    target = 21 if metric else 70
    for sensor in (SENSOR_A, SENSOR_B):
        await _set_temp(hass, sensor, target)
    notes = async_mock_service(hass, "notify", "floor_test")
    pinned = async_mock_service(hass, "persistent_notification", "create")
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await flow.async_configure(
        result["flow_id"], {"heads": [head_a, head_b], "notify_service": NOTIFY}
    )
    assert result["step_id"] == "rooms", result
    result = await flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "sensors", result
    result = await flow.async_configure(
        result["flow_id"], {"sensor_1": SENSOR_A, "sensor_2": SENSOR_B}
    )
    assert result["step_id"] == "review", result
    result = await setup_tuning(
        hass, result, {"mode_hysteresis": 0, CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY}
    )
    assert result["step_id"] == "review", result
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    entry = result["result"]
    for suffix in enable:
        await _switch(hass, _eid(hass, entry, suffix), True)
    for suffix in ("_primary_target", "_secondary_target"):
        await _set_target(hass, _eid(hass, entry, suffix), target)
    await _recompute(hass, entry)
    return Floor(hass, entry, head_a, head_b, notes, pinned)


def _zones(entry) -> list[dict[str, Any]]:
    return [dict(zone) for zone in {**entry.data, **entry.options}[CONF_ZONES]]


def _safety_keys(zone: dict[str, Any]) -> dict[str, Any]:
    """A room's floor and freshness keys, absent ones left out."""
    return {k: zone[k] for k in (*FRESHNESS_KEYS, ZONE_FLOOR_LOW, ZONE_FLOOR_HIGH) if k in zone}


async def _save_room(hass: HomeAssistant, entry, room: int, changes: dict[str, Any],
                     drop: tuple[str, ...] = ()) -> None:
    """Open a room page and submit it as shown, with ``changes`` made and the
    fields in ``drop`` cleared (an emptied box is not submitted)."""
    result = await open_options_page(hass, entry, f"room_{room}")
    values = {k: v for k, v in prefilled(result).items() if k not in drop}
    result = await submit(hass.config_entries.options, result, {**values, **changes})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()


async def _report(hass: HomeAssistant, sensor: str, value: float) -> None:
    """One write by the sensor's integration, changed or not."""
    hass.states.async_set(
        sensor, str(value), {"unit_of_measurement": hass.config.units.temperature_unit}
    )
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Stored at creation: new install, F and C
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("metric", [False, True], ids=["fahrenheit", "celsius"])
async def test_new_install_stores_the_defaults_in_every_room(
    hass: HomeAssistant, metric: bool
) -> None:
    """Setup stores floor 45 °F / 7 °C, max_age 360 and ha_state_write in each
    room, no ceiling and no startup grace; the review screen said so; the
    coordinator reads them as the room's own floor and freshness window."""
    f = await _install(hass, metric=metric)
    for zone in _zones(f.entry):
        assert zone[ZONE_FLOOR_LOW] == FLOOR[metric]
        assert zone[ZONE_MAX_AGE] == MAX_AGE
        assert zone[ZONE_EVIDENCE_BASIS] == "ha_state_write"
        assert ZONE_FLOOR_HIGH not in zone
        assert ZONE_STARTUP_GRACE not in zone and ZONE_REPORT_INTERVAL not in zone
    assert [(z.floor_low, z.floor_high) for z in f.coord.zones] == [(FLOOR[metric], None)] * 2
    assert f.coord._freshness == {"primary": (21600.0, 21600.0), "secondary": (21600.0, 21600.0)}
    # Nothing flat: the defaults live only in the room records.
    stored = {**f.entry.data, **f.entry.options}
    assert not [k for k in stored if k in (*DEFAULT_KEYS, ZONE_FLOOR_HIGH)]
    assert f.entry.version == 2


@pytest.mark.parametrize("metric", [False, True], ids=["fahrenheit", "celsius"])
async def test_setup_review_shows_the_new_room_defaults(
    hass: HomeAssistant, metric: bool
) -> None:
    """The review screen states what Save and finish will store for each room."""
    hass.config.units = METRIC_SYSTEM if metric else US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass, cls=MockHeadC if metric else MockHead)
    for sensor in (SENSOR_A, SENSOR_B):
        await _set_temp(hass, sensor, 20)
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await flow.async_configure(result["flow_id"], {"heads": [head_a, head_b]})
    result = await flow.async_configure(result["flow_id"], {})
    result = await flow.async_configure(
        result["flow_id"], {"sensor_1": SENSOR_A, "sensor_2": SENSOR_B}
    )
    summary = result["description_placeholders"]["summary"]
    unit = "°C" if metric else "°F"
    assert summary.count(
        f"Freeze floor: {FLOOR[metric]:g} {unit} — heats this room while it is turned off."
    ) == 2
    assert summary.count("maximum age 360 min (explicit)") == 2
    assert "Cadence: unknown" not in summary


# ---------------------------------------------------------------------------
# A room added later (Reconfigure) gets them; existing rooms stay blank
# ---------------------------------------------------------------------------
def _blank_entry(hass: HomeAssistant, head_a: str, head_b: str) -> MockConfigEntry:
    """An entry stored before 3.16.0 (v2.2): two rooms, no floor, no freshness."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_ZONES: [
                {ZONE_NAME: "Living", ZONE_CLIMATE: head_a, ZONE_SENSOR: SENSOR_A},
                {ZONE_NAME: "Office", ZONE_CLIMATE: head_b, ZONE_SENSOR: SENSOR_B},
            ],
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
        },
        options={CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY},
        unique_id=f"{head_a}|{head_b}",
        version=2,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    return entry


async def _reconfigure(hass: HomeAssistant, entry, heads: list[str], sensors: list[str]):
    flow = hass.config_entries.flow
    result = await flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await flow.async_configure(result["flow_id"], {"heads": heads})
    assert result["step_id"] == "reconfigure_rooms", result
    result = await flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "reconfigure_sensors", result
    review = await flow.async_configure(
        result["flow_id"],
        {f"sensor_{i + 1}": sensor for i, sensor in enumerate(sensors)},
    )
    assert review["step_id"] == "reconfigure_review", review
    done = await flow.async_configure(review["flow_id"], {"next_step_id": "reconfigure_finish"})
    assert done["reason"] == "reconfigure_successful", done
    await hass.async_block_till_done()
    return review["description_placeholders"]["summary"]


@pytest.mark.parametrize("metric", [False, True], ids=["fahrenheit", "celsius"])
async def test_a_room_added_by_reconfigure_gets_the_defaults_and_old_rooms_stay_blank(
    hass: HomeAssistant, metric: bool
) -> None:
    """Reconfigure adds a third head to a pre-3.16.0 entry: the new room stores
    the defaults (and the review said so); the two existing rooms are byte for
    byte what they were, and still have no floor and no cutoff."""
    hass.config.units = METRIC_SYSTEM if metric else US_CUSTOMARY_SYSTEM
    cls = MockHeadC if metric else MockHead
    head_a, head_b = await _setup_mock_heads(hass, cls=cls)
    hass.states.async_set(
        SPARE, "off",
        {"hvac_modes": ["off", "cool", "heat", "fan_only"], "fan_modes": ["auto", "low"],
         "supported_features": 8},
    )
    for sensor in (SENSOR_A, SENSOR_B, SPARE_SENSOR):
        await _set_temp(hass, sensor, 20)
    entry = _blank_entry(hass, head_a, head_b)
    old = deepcopy(entry.data[CONF_ZONES])

    summary = await _reconfigure(
        hass, entry, [head_a, head_b, SPARE], [SENSOR_A, SENSOR_B, SPARE_SENSOR]
    )
    zones = entry.data[CONF_ZONES]
    assert zones[:2] == old
    new = zones[2]
    assert new[ZONE_CLIMATE] == SPARE
    assert (new[ZONE_FLOOR_LOW], new[ZONE_MAX_AGE], new[ZONE_EVIDENCE_BASIS]) == (
        FLOOR[metric], MAX_AGE, "ha_state_write",
    )
    assert ZONE_FLOOR_HIGH not in new and ZONE_STARTUP_GRACE not in new
    # The review: the old rooms unknown cadence and no floor, the new one both.
    rooms = summary.split("Priority ")[1:]
    unit = "°C" if metric else "°F"
    for room in rooms[:2]:
        assert "Cadence: unknown" in room and "Freeze floor" not in room
    assert "maximum age 360 min (explicit)" in rooms[2]
    assert f"Freeze floor: {FLOOR[metric]:g} {unit}" in rooms[2]


async def test_reconfigure_keeping_the_same_rooms_adds_no_defaults(hass: HomeAssistant) -> None:
    """Reconfigure over the same heads (a rename, a sensor swap) is not a new
    room: nothing is added to either record."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    for sensor in (SENSOR_A, SENSOR_B, SPARE_SENSOR):
        await _set_temp(hass, sensor, 70)
    entry = _blank_entry(hass, head_a, head_b)
    await _reconfigure(hass, entry, [head_b, head_a], [SENSOR_B, SPARE_SENSOR])
    zones = entry.data[CONF_ZONES]
    assert [z[ZONE_CLIMATE] for z in zones] == [head_b, head_a]
    assert zones[1][ZONE_SENSOR] == SPARE_SENSOR
    for zone in zones:
        assert not [k for k in (*DEFAULT_KEYS, *FRESHNESS_KEYS, ZONE_FLOOR_HIGH) if k in zone]


# ---------------------------------------------------------------------------
# Migration: a 2.2 entry loads unchanged, and no options save adds a default
# ---------------------------------------------------------------------------
async def test_a_2_2_entry_with_blank_rooms_loads_unchanged_and_no_save_adds_defaults(
    hass: HomeAssistant,
) -> None:
    """No migration and no runtime fallback: the entry loads byte for byte,
    the coordinator has no floor and no cutoff for either room (a disabled
    room at 30 °F stays off), and saving every Configure page, and each room
    page untouched, adds none of the keys."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    for sensor in (SENSOR_A, SENSOR_B):
        await _set_temp(hass, sensor, 70)
    entry = _blank_entry(hass, head_a, head_b)
    before = (deepcopy(dict(entry.data)), deepcopy(dict(entry.options)))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert (dict(entry.data), dict(entry.options)) == before
    assert (entry.version, entry.minor_version) == (2, 2)
    coord = entry.runtime_data
    assert [(z.floor_low, z.max_age, z.evidence_basis) for z in coord.zones] == [(None, None, None)] * 2
    assert coord._freshness == {}
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), True)
    await _set_temp(hass, SENSOR_B, 30)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert [z["floor_state"] for z in plan.attributes["zones"]] == ["none", "none"]

    old_zones = deepcopy(before[0][CONF_ZONES])
    for page in (*GLOBAL_PAGES, "room_1", "room_2"):
        result = await open_options_page(hass, entry, page)
        if page.startswith("room_"):
            assert suggested(result, "floor_low") is None
            assert suggested(result, "max_age") is None
            assert default(result, "evidence_basis") == "unknown"
        result = await submit(hass.config_entries.options, result, prefilled(result))
        assert result["type"] is FlowResultType.CREATE_ENTRY, (page, result)
        await hass.async_block_till_done()
        assert _zones(entry) == old_zones, page
        assert entry.data[CONF_ZONES] == old_zones, page
        stored = {**entry.data, **entry.options}
        assert not [k for k in stored if k in (*DEFAULT_KEYS, ZONE_FLOOR_HIGH)], page
    assert (entry.version, entry.minor_version) == (2, 2)


# ---------------------------------------------------------------------------
# Ordinary values: shown, editable, clearable; a clear survives save + restart
# ---------------------------------------------------------------------------
async def test_new_room_page_shows_the_defaults_and_an_untouched_save_keeps_them(
    hass: HomeAssistant,
) -> None:
    """The room page pre-fills the stored defaults; saving it unchanged keeps
    them exactly; an edit stores the new value."""
    f = await _install(hass)
    result = await open_options_page(hass, f.entry, "room_2")
    assert suggested(result, "floor_low") == 45.0
    assert suggested(result, "floor_high") is None
    assert suggested(result, "max_age") == 360.0
    assert suggested(result, "startup_grace") is None
    assert default(result, "evidence_basis") == "ha_state_write"
    before = _zones(f.entry)
    await _save_room(hass, f.entry, 2, {})
    assert [_safety_keys(z) for z in _zones(f.entry)] == [_safety_keys(z) for z in before]
    await _save_room(hass, f.entry, 2, {"floor_low": 50, "max_age": 120})
    zone = _zones(f.entry)[1]
    assert (zone[ZONE_FLOOR_LOW], zone[ZONE_MAX_AGE], zone[ZONE_EVIDENCE_BASIS]) == (
        50.0, 120, "ha_state_write",
    )
    assert _safety_keys(_zones(f.entry)[0]) == _safety_keys(before[0])


async def test_cleared_defaults_stay_cleared_after_save_and_restart(
    hass: HomeAssistant, freezer
) -> None:
    """Room 1 clears its floor, room 2 its freshness. Each clear removes only
    its keys, survives another page's save and a restart, and the cleared
    room then acts as blank: room 1 at 40 °F disabled stays off, room 2 is
    never timed out."""
    f = await _install(hass, enable=("_coordinator_enable",))
    await _save_room(hass, f.entry, 1, {}, drop=("floor_low",))
    await _save_room(hass, f.entry, 2, {"evidence_basis": "unknown"}, drop=("max_age",))
    room1, room2 = _zones(f.entry)
    assert ZONE_FLOOR_LOW not in room1
    assert (room1[ZONE_MAX_AGE], room1[ZONE_EVIDENCE_BASIS]) == (MAX_AGE, "ha_state_write")
    assert room2[ZONE_FLOOR_LOW] == 45.0
    assert not [k for k in FRESHNESS_KEYS if k in room2]

    # Another page's save, then a restart: nothing comes back.
    result = await open_options_page(hass, f.entry, "comfort")
    result = await submit(hass.config_entries.options, result, prefilled(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await _shutdown(hass, f.entry)
    await _startup(hass, f.entry)
    assert _zones(f.entry) == [room1, room2]
    assert [z.floor_low for z in f.coord.zones] == [None, 45.0]
    assert set(f.coord._freshness) == {"primary"}
    result = await open_options_page(hass, f.entry, "room_1")
    assert suggested(result, "floor_low") is None
    result = await open_options_page(hass, f.entry, "room_2")
    assert (suggested(result, "max_age"), default(result, "evidence_basis")) == (None, "unknown")

    await _switch(hass, _eid(hass, f.entry, "_coordinator_enable"), True)
    await _report(hass, SENSOR_A, 40)
    await _report(hass, SENSOR_B, 50)
    await f.compute()
    assert (_s(hass, f.a), f.floor(0)) == ("off", ("none", None))
    await _tick(hass, freezer, SIX_HOURS + timedelta(minutes=1))
    await f.compute()
    assert f.zone(1)["sensor_health"] == "cadence_unknown"
    assert f.zone(0)["sensor_health"] == "stale"


# ---------------------------------------------------------------------------
# A change-only sensor in a steady room
# ---------------------------------------------------------------------------
async def test_change_only_sensor_steady_six_hours_goes_stale_floor_blind_then_recovers(
    hass: HomeAssistant, freezer
) -> None:
    """A sensor that writes only on a change, in a steady disabled room: still
    healthy at 5 h 59 m; stale past 6 h, its floor blind with one notice that
    names the 6 h and both fixes; the next write ends it, and a write below
    the floor trips the floor again."""
    f = await _install(hass)
    await _report(hass, SENSOR_A, 70)
    await _report(hass, SENSOR_B, 50)  # its last change: the room holds 50 °F
    await f.compute()
    assert (f.zone(1)["sensor_health"], f.floor(1)) == ("healthy", ("armed", None))
    await _tick(hass, freezer, SIX_HOURS - timedelta(minutes=1))
    await _report(hass, SENSOR_A, 70)  # the other room keeps reporting
    await f.compute()
    assert (f.zone(1)["sensor_health"], f.floor(1)[0]) == ("healthy", "armed")
    assert f.notes == []
    await _tick(hass, freezer, timedelta(minutes=2))
    await _report(hass, SENSOR_A, 70)
    await f.compute()
    assert f.zone(1)["sensor_health"] == "stale"
    assert f.zone(0)["sensor_health"] == "healthy"
    state, reason = f.floor(1)
    assert state == "blind"
    assert reason == (
        f"Room sensor {SENSOR_B} sent no report in 6 h, its freshness limit, so it "
        "is stale: no floor protection until it reports again. A sensor that "
        "reports only when its reading changes goes quiet in a steady room: clear "
        "Maximum age (Configure → Rooms → Mock Head b → Sensor freshness), or use "
        "a sensor that reports periodically."
    )
    assert f.notes == [f"Mock Head b: the freeze/overheat floor is blind. {reason}"]
    await f.compute()
    assert len(f.notes) == 1  # one notice per episode
    await _report(hass, SENSOR_B, 50)  # the next write, unchanged reading
    await f.compute()
    assert (f.zone(1)["sensor_health"], f.floor(1)) == ("healthy", ("armed", None))
    await _tick(hass, freezer, timedelta(hours=1))  # the hourly notice limit
    await _report(hass, SENSOR_B, 44)
    await f.compute()
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")


async def test_a_stale_notice_after_restart_names_the_startup_grace(
    hass: HomeAssistant, freezer
) -> None:
    """After a restart with no report witnessed, the notice names the window
    that actually ran out: the startup grace (6 h by default, as max_age)."""
    f = await _install(hass)
    await _shutdown(hass, f.entry)
    await _tick(hass, freezer, timedelta(minutes=5))
    await _startup(hass, f.entry)
    await f.compute()
    assert f.zone(1)["sensor_health"] == "awaiting_report"
    await _tick(hass, freezer, SIX_HOURS + timedelta(seconds=1))
    await f.compute()
    assert f.zone(1)["sensor_health"] == "stale"
    assert "sent no report in 6 h, its freshness limit" in f.floor(1)[1]


# ---------------------------------------------------------------------------
# The default floor heats a disabled room, also under the heat lockout
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("metric", [False, True], ids=["fahrenheit", "celsius"])
async def test_disabled_new_room_below_the_default_floor_heats_even_under_heat_lockout(
    hass: HomeAssistant, metric: bool
) -> None:
    """Heat lockout on. The disabled new room just above its default floor is
    left off; just below it, it heats to floor + demand threshold and then
    goes off. The enabled room's own floor never acts for it."""
    f = await _install(hass, metric=metric)
    await f.switch("_heat_lockout", True)
    assert f.coord.heat_lockout is True
    floor = FLOOR[metric]
    margin = 1.5 if metric else 3.0
    assert f.coord._floor_margin == margin
    await _report(hass, SENSOR_A, floor - 0.5)  # the ENABLED room, below its floor
    await f.temp(SENSOR_B, floor + 0.5)
    assert (_s(hass, f.b), f.floor(1)) == ("off", ("armed", None))
    await f.temp(SENSOR_B, floor - 0.5)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("heat", "heat")
    assert f.plan.attributes["mode_by_floor"] is True
    unit = "°C" if metric else "°F"
    assert len(f.notes) == 1
    assert f"below its freeze floor of {floor:g} {unit}" in f.notes[0]
    assert f"until it reaches {floor + margin:g} {unit}" in f.notes[0]
    # The enabled room: no floor want of its own at any reading.
    assert (f.floor(0), f.zone(0)["floor_want"]) == (("enabled", None), None)
    await f.temp(SENSOR_B, floor + margin)
    assert (_s(hass, f.b), f.floor(1)[0]) == ("off", "armed")
    assert (f.floor(0), f.zone(0)["floor_want"]) == (("enabled", None), None)
    assert len(f.notes) == 1


# ---------------------------------------------------------------------------
# Restart with the defaults: the 6 h grace, ended by a write; a fan pick kept
# ---------------------------------------------------------------------------
async def test_restart_with_defaults_six_hour_grace_a_write_ends_it_and_a_fan_pick_survives(
    hass: HomeAssistant, freezer
) -> None:
    """Both rooms enabled; a person picks fan low on room 1's head. After a
    restart both rooms wait for a report for 6 h (no demand lost meanwhile);
    a write at 5 h 59 m makes room 1 healthy, room 2 with none is stale at
    6 h; the manual fan pick is held throughout and no fan write is sent."""
    f = await _install(hass, enable=("_primary_enable", "_secondary_enable", "_coordinator_enable"))
    await _report(hass, SENSOR_A, 70)
    await _report(hass, SENSOR_B, 70)
    await _user_set_fan(hass, f.a, "low")
    await f.compute()
    assert _fan_hold(hass, f.entry, 0) is True
    await _shutdown(hass, f.entry)
    await _tick(hass, freezer, timedelta(minutes=5))  # Home Assistant is down
    writes = _fan_writes(hass)
    await _startup(hass, f.entry)
    await f.compute()
    started = f.coord._started_ts
    assert f.coord._grace_until == {"primary": started + 21600.0, "secondary": started + 21600.0}
    assert [f.zone(i)["sensor_health"] for i in (0, 1)] == ["awaiting_report"] * 2
    assert _fan_hold(hass, f.entry, 0) is True
    await _tick(hass, freezer, SIX_HOURS - timedelta(minutes=1))
    await f.compute()
    assert [f.zone(i)["sensor_health"] for i in (0, 1)] == ["awaiting_report"] * 2
    await _report(hass, SENSOR_A, 70)  # a fresh write ends room 1's grace
    await f.compute()
    assert [f.zone(i)["sensor_health"] for i in (0, 1)] == ["healthy", "awaiting_report"]
    assert "primary" not in f.coord._grace_until
    await _tick(hass, freezer, timedelta(minutes=1))
    await f.compute()
    assert [f.zone(i)["sensor_health"] for i in (0, 1)] == ["healthy", "stale"]
    assert _fan_hold(hass, f.entry, 0) is True
    assert hass.states.get(f.a).attributes["fan_mode"] == "low"
    assert [w for w in writes if w[0] == f.a] == []


# ---------------------------------------------------------------------------
# Floor ordering, rerun with the default values on a flow-created entry
# ---------------------------------------------------------------------------
async def test_default_floor_beats_an_allow_dry_head(hass: HomeAssistant) -> None:
    """Allow Dry (3.15.0): room 2 opts in on its page and a person puts its
    head in Dry, a cool vote. The disabled room 1 falls below its default
    floor: the floor's heat wins and the Dry head parks at the idle action."""
    f = await _install(
        hass, head_cls=DryHead, enable=("_secondary_enable", "_coordinator_enable")
    )
    await _save_room(hass, f.entry, 2, {"allow_dry": True})
    assert _zones(f.entry)[1][ZONE_FLOOR_LOW] == 45.0  # the page save kept it
    await _remote(hass, f.b, "dry")
    await f.compute()
    assert (f.plan.state, f.zone(1)["dry"]) == ("cool", "held")
    await f.temp(SENSOR_A, 44)
    assert (f.plan.state, f.plan.attributes["mode_by_floor"]) == ("heat", True)
    assert (_s(hass, f.a), f.floor(0)[0]) == ("heat", "heat")
    assert _s(hass, f.b) == "fan_only"


async def test_default_floor_on_a_hands_off_room_takes_the_head_and_hands_it_back(
    hass: HomeAssistant,
) -> None:
    """Own remote / hands-off: room 2 opts in, is disabled and released. Its
    own-remote cool is a low-rank vote. Below its default floor, the floor
    takes the head (heat), and at floor + 3 °F turns it off and releases it."""
    f = await _install(hass)
    await _save_room(hass, f.entry, 2, {"hands_off": True})
    assert _zones(f.entry)[1][ZONE_FLOOR_LOW] == 45.0
    await f.compute()
    assert f.zone(1)["hands_off"] == "released"
    await _remote(hass, f.b, "cool")
    await f.compute()
    assert f.zone(1)["hands_off_vote"] == "cool"
    await f.temp(SENSOR_B, 44)
    assert (f.zone(1)["hands_off"], f.zone(1)["hands_off_vote"]) == ("floor", None)
    await f.compute()
    assert (f.plan.state, _s(hass, f.b), f.floor(1)[0]) == ("heat", "heat", "heat")
    await f.temp(SENSOR_B, 48)
    await f.compute()
    assert (_s(hass, f.b), f.zone(1)["hands_off"]) == ("off", "released")


async def test_default_floor_beats_a_safety_priority_rooms_cool(hass: HomeAssistant) -> None:
    """Safety priority (#31): the enabled room 1 is marked Safety priority and
    calls cool; the disabled room 2's default floor still wins (freeze first)."""
    hass.states.async_set(SAFETY_A, "on")
    f = await _install(hass)
    await _save_room(hass, f.entry, 1, {"safety_priority_entity": SAFETY_A})
    await f.temp(SENSOR_A, 76)
    assert f.zone(0)["safety_priority"] is True
    assert f.plan.state == "cool"
    await f.temp(SENSOR_B, 44)
    assert (f.plan.state, _s(hass, f.b), f.floor(1)[0]) == ("heat", "heat", "heat")
    assert f.plan.attributes["mode_by_floor"] is True


def test_the_floor_margin_oracle_is_the_unit_profile_demand_threshold() -> None:
    """The clear lines the tests above assert (floor + 3 °F, + 1.5 °C) are the
    unit profiles' demand thresholds, so a profile change shows up here."""
    from custom_components.mxz_coordinator.const import unit_profile

    assert float(unit_profile(False)["defaults"][CONF_DEMAND_THRESHOLD]) == 3.0
    assert float(unit_profile(True)["defaults"][CONF_DEMAND_THRESHOLD]) == 1.5
