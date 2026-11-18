"""One temperature sensor serving two rooms (#145), on the real integration.

Two heads in one space both read one thermostat's sensor. Each head stays its
own room, with its own target, enable switch, freshness profile, floor guard
and fan hold. Every head and sensor here is invented.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_ZONES,
    DOMAIN,
    EVIDENCE_HA_WRITE,
    HEALTH_HEALTHY,
    HEALTH_STALE,
    IDLE_ACTION_FAN_ONLY,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    ZONE_CLIMATE,
    ZONE_COMFORT_PRIORITY_ENTITY,
    ZONE_EVIDENCE_BASIS,
    ZONE_NAME,
    ZONE_OPENING_ENTITY,
    ZONE_OPENING_PAUSE,
    ZONE_REPORT_INTERVAL,
    ZONE_SAFETY_PRIORITY_ENTITY,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from tests.ha_test import MockConfigEntry, async_fire_time_changed
from tests.restart import restart
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_target,
    _set_temp,
    _setup_mock_heads,
    _user_set_fan,
)
from tests.test_floor import _setup as _floor_setup
from tests.test_hands_off import _s
from tests.test_startup_continuity import _switch

SAFETY = "binary_sensor.shared_safety"
COMFORT = "binary_sensor.shared_occupied"
DOOR = "binary_sensor.shared_door"


class Rig:
    """One loaded entry with two rooms, read back through what a person sees."""

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry, a: str, b: str) -> None:
        self.hass, self.entry, self.a, self.b = hass, entry, a, b

    @property
    def coord(self) -> MXZCoordinator:
        return self.entry.runtime_data

    @property
    def plan(self) -> Any:
        return self.hass.states.get(_eid(self.hass, self.entry, "_plan"))

    def room(self, index: int) -> dict[str, Any]:
        return self.plan.attributes["zones"][index]

    async def target(self, suffix: str, value: float) -> None:
        await _set_target(self.hass, _eid(self.hass, self.entry, suffix), value)


async def _setup(
    hass: HomeAssistant,
    *,
    sensors: tuple[str, str] = (SENSOR_A, SENSOR_A),
    rooms: tuple[dict[str, Any], dict[str, Any]] = ({}, {}),
    fan_boost: bool = False,
) -> Rig:
    """Two enabled rooms, by default both on SENSOR_A, at 70 °F."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    for sensor in set(sensors):
        await _set_temp(hass, sensor, 70)
    for entity_id in (SAFETY, COMFORT, DOOR):
        hass.states.async_set(entity_id, "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        version=2,
        data={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_FAN_BOOST_ENABLE: fan_boost,
            CONF_MODE_HYSTERESIS: 0,
            CONF_ZONES: [
                {ZONE_NAME: name, ZONE_CLIMATE: head, ZONE_SENSOR: sensor, **more}
                for name, head, sensor, more in zip(
                    ("Living", "Den"), (head_a, head_b), sensors, rooms, strict=True
                )
            ],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await _switch(hass, _eid(hass, entry, suffix), True)
    rig = Rig(hass, entry, head_a, head_b)
    for suffix in ("_primary_target", "_secondary_target"):
        await rig.target(suffix, 70)
    await _recompute(hass, entry)
    return rig


# -- both rooms follow one write ------------------------------------------------


async def test_one_write_moves_both_rooms_and_each_head_follows_its_own_target(
    hass: HomeAssistant,
) -> None:
    rig = await _setup(hass)
    await rig.target("_primary_target", 68)
    await rig.target("_secondary_target", 66)

    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, rig.entry)

    assert (rig.room(0)["temp"], rig.room(1)["temp"]) == (75.0, 75.0)
    assert (rig.room(0)["demand"], rig.room(1)["demand"]) == (MODE_COOL, MODE_COOL)
    assert (_s(hass, rig.a), _s(hass, rig.b)) == (MODE_COOL, MODE_COOL)
    assert hass.states.get(rig.a).attributes["target_temp_high"] == 68
    assert hass.states.get(rig.b).attributes["target_temp_high"] == 66


async def test_opposite_needs_on_one_sensor_resolve_by_room_order(
    hass: HomeAssistant,
) -> None:
    """The existing standoff rule: the first room picks the mode, the other waits."""
    rig = await _setup(hass)
    await rig.target("_primary_target", 64)
    await rig.target("_secondary_target", 76)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, rig.entry)

    plan = rig.plan
    assert (rig.room(0)["demand"], rig.room(1)["demand"]) == (MODE_COOL, MODE_HEAT)
    assert plan.state == MODE_COOL
    assert plan.attributes["standoff"] is True
    assert (_s(hass, rig.a), _s(hass, rig.b)) == (MODE_COOL, MODE_FAN_ONLY)

    await rig.target("_primary_target", 70)
    await _recompute(hass, rig.entry)
    assert rig.plan.state == MODE_HEAT
    assert rig.plan.attributes["standoff"] is False
    assert _s(hass, rig.b) == MODE_HEAT


# -- one write is one dispatch ----------------------------------------------------


class _Spy:
    """Counts what one entity write makes the coordinator do."""

    def __init__(self, coord: MXZCoordinator) -> None:
        self.observed: Counter[str] = Counter()
        self.refreshes = 0
        observe = coord._observe_report
        request = coord.async_request_refresh

        def _observe(zone: Any, *args: Any) -> None:
            self.observed[zone.slug] += 1
            observe(zone, *args)

        async def _request() -> None:
            self.refreshes += 1
            await request()

        coord._observe_report = _observe  # type: ignore[method-assign]
        coord.async_request_refresh = _request  # type: ignore[method-assign]


@pytest.mark.parametrize(
    "sensors", [(SENSOR_A, SENSOR_B), (SENSOR_A, SENSOR_A)], ids=["distinct", "shared"]
)
async def test_one_sensor_write_is_observed_once_per_room(
    hass: HomeAssistant, sensors: tuple[str, str]
) -> None:
    rig = await _setup(hass, sensors=sensors)
    spy = _Spy(rig.coord)

    await _set_temp(hass, SENSOR_A, 72)

    expected = {"primary": 1} if sensors[1] == SENSOR_B else {"primary": 1, "secondary": 1}
    assert dict(spy.observed) == expected
    assert spy.refreshes == 1


@pytest.mark.parametrize(
    "sensors", [(SENSOR_A, SENSOR_B), (SENSOR_A, SENSOR_A)], ids=["distinct", "shared"]
)
async def test_an_unchanged_report_is_observed_once_per_room(
    hass: HomeAssistant, sensors: tuple[str, str]
) -> None:
    """The report stream, which runs only where a room has a cadence."""
    cadence = {ZONE_REPORT_INTERVAL: 5, ZONE_EVIDENCE_BASIS: EVIDENCE_HA_WRITE}
    rig = await _setup(hass, sensors=sensors, rooms=(cadence, cadence))
    spy = _Spy(rig.coord)

    await _set_temp(hass, SENSOR_A, 70)

    expected = {"primary": 1} if sensors[1] == SENSOR_B else {"primary": 1, "secondary": 1}
    assert dict(spy.observed) == expected


@pytest.mark.parametrize(
    ("entity_id", "rooms_key"),
    [
        (SAFETY, ZONE_SAFETY_PRIORITY_ENTITY),
        (COMFORT, ZONE_COMFORT_PRIORITY_ENTITY),
        (DOOR, ZONE_OPENING_ENTITY),
    ],
    ids=["safety_priority", "comfort_priority", "door_window"],
)
@pytest.mark.parametrize("shared", [False, True], ids=["one_room", "both_rooms"])
async def test_a_shared_room_entity_change_requests_one_refresh(
    hass: HomeAssistant, entity_id: str, rooms_key: str, shared: bool
) -> None:
    room = {rooms_key: entity_id}
    if rooms_key == ZONE_OPENING_ENTITY:
        room[ZONE_OPENING_PAUSE] = True
    rig = await _setup(hass, sensors=(SENSOR_A, SENSOR_B), rooms=(room, room if shared else {}))
    spy = _Spy(rig.coord)

    hass.states.async_set(entity_id, "on")
    await hass.async_block_till_done()

    assert spy.refreshes == 1


# -- freshness is judged per room ---------------------------------------------------


async def test_each_room_judges_the_shared_sensor_by_its_own_profile(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A 5-minute and a 10-minute profile on one sensor: stale at 15 and 30 minutes."""
    rig = await _setup(
        hass,
        rooms=(
            {ZONE_REPORT_INTERVAL: 5, ZONE_EVIDENCE_BASIS: EVIDENCE_HA_WRITE},
            {ZONE_REPORT_INTERVAL: 10, ZONE_EVIDENCE_BASIS: EVIDENCE_HA_WRITE},
        ),
    )

    async def advance(minutes: float) -> None:
        freezer.tick(timedelta(minutes=minutes))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        await _recompute(hass, rig.entry)

    await _set_temp(hass, SENSOR_A, 70)
    await advance(16)
    assert (rig.room(0)["sensor_health"], rig.room(1)["sensor_health"]) == (
        HEALTH_STALE,
        HEALTH_HEALTHY,
    )

    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, rig.entry)
    assert (rig.room(0)["sensor_health"], rig.room(1)["sensor_health"]) == (
        HEALTH_HEALTHY,
        HEALTH_HEALTHY,
    )

    await advance(31)
    assert (rig.room(0)["sensor_health"], rig.room(1)["sensor_health"]) == (
        HEALTH_STALE,
        HEALTH_STALE,
    )


# -- floor guard per head ----------------------------------------------------------


async def test_two_held_off_rooms_on_one_sensor_each_run_their_own_floor(
    hass: HomeAssistant,
) -> None:
    """Floors 50 and 55 °F on one sensor: both trip at 45, clear at 53 and 58."""
    f = await _floor_setup(
        hass,
        floors=((50.0, None), (55.0, None)),
        enable=("_coordinator_enable",),
        sensors=(SENSOR_A, SENSOR_A),
    )
    assert (f.floor(0), f.floor(1)) == (("armed", None), ("armed", None))

    await f.temp(SENSOR_A, 45)
    assert (f.floor(0)[0], f.floor(1)[0]) == (MODE_HEAT, MODE_HEAT)
    assert (_s(hass, f.a), _s(hass, f.b)) == (MODE_HEAT, MODE_HEAT)
    assert len(f.notes) == 2

    await f.temp(SENSOR_A, 53)
    assert (f.floor(0)[0], f.floor(1)[0]) == ("armed", MODE_HEAT)
    assert (_s(hass, f.a), _s(hass, f.b)) == ("off", MODE_HEAT)

    await f.temp(SENSOR_A, 58)
    assert (f.floor(0)[0], f.floor(1)[0]) == ("armed", "armed")
    assert (_s(hass, f.a), _s(hass, f.b)) == ("off", "off")


# -- restart continuity --------------------------------------------------------------


async def test_a_restart_restores_each_rooms_target_and_fan_hold(
    hass: HomeAssistant,
) -> None:
    rig = await _setup(hass, fan_boost=True)
    await rig.target("_primary_target", 68)
    await rig.target("_secondary_target", 72)
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, rig.entry)
    await _user_set_fan(hass, rig.a, "quiet")
    await _recompute(hass, rig.entry)
    assert (rig.room(0)["fan_hold"], rig.room(1)["fan_hold"]) == (True, False)

    await restart(hass, rig.entry)
    await _recompute(hass, rig.entry)

    assert (rig.room(0)["target"], rig.room(1)["target"]) == (68.0, 72.0)
    assert (rig.room(0)["fan_hold"], rig.room(1)["fan_hold"]) == (True, False)
    assert hass.states.get(rig.a).attributes["fan_mode"] == "quiet"
    assert [zone[ZONE_SENSOR] for zone in rig.entry.data[CONF_ZONES]] == [SENSOR_A, SENSOR_A]
