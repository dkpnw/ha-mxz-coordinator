"""Allow Dry mode: a per-room opt-in, off by default (3.15.0).

Drew's decisions (Oct 30, 2026), each pinned by a behavior test here:

* Off (the default, and every entry stored before the option existed): a head
  put in dry is put back after the band wait, with the same notice, exactly as
  before.
* On, while the room is enabled: a person's dry is left alone. MXZ sends that
  head nothing (no heal, no mode, setpoint, fan or vane write), the room's
  temperature goal pauses, and the head is a cool vote at hands-off rank:
  below any enabled room's call, a safety want and the freeze floor, and
  blocked by a cool lockout. The room's own Safety priority sensor never lifts
  that vote.
* When another room wins heat, the dry head is parked at its idle action and
  one notice goes out; MXZ never puts dry back (it never sends dry at all).
* Leaving dry starts the coil-dry run, as leaving cool does.
* A restart leaves a dry head alone and gives its vote back; a manual fan
  pick is kept throughout.
* Cloud heads follow the same rules, and a cloud report of dry that may be
  older than MXZ's last batch is not a person's dry.
* The room page shows the option only for a head that lists dry.
* The coil-dry idle choice reads "Dry the coil (fan only), then off"; its
  stored key stays off_after_dry.

The heads are invented (CN105-like on the esphome platform, plus the cloud
fakes in tests/cloud_heads.py); nothing here is a hardware result.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.mxz_coordinator.capabilities import IDLE_ACTION_LABELS
from custom_components.mxz_coordinator.const import (
    BAND_DRIFT_DELAY,
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_ZONES,
    DOMAIN,
    HEAD_TYPE_CLOUD,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
    INHIBIT_ACTION_OFF,
    ZONE_ALLOW_DRY,
    ZONE_CLIMATE,
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
    ZONE_HEAD_TYPE,
    ZONE_NAME,
    ZONE_SAFETY_PRIORITY_ENTITY,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from custom_components.mxz_coordinator.logic import PRIORITY_SAFETY, PRIORITY_VOTE
from tests.clocks import age_coil_dry
from tests.cloud_heads import (
    COMFORT,
    ComfortHead,
    past_startup,
    past_window,
    register,
    setup_rooms,
)
from tests.cloud_heads import (
    record as record_calls,
)
from tests.cloud_heads import (
    writes as head_writes,
)
from tests.dwell import expire_dwell
from tests.flow_pages import default, fields, open_options_page, prefilled, submit
from tests.test_drive import MockHead, _eid, _set_target, _set_temp
from tests.test_startup_continuity import _head, _shutdown, _startup

SENSORS = ("sensor.room_a_temp", "sensor.room_b_temp", "sensor.room_c_temp")
NAMES = ("Rec room", "Bedroom", "Office")
NOTIFY = "dry_test"
SAFETY = "binary_sensor.safety"
INHIBIT = "binary_sensor.grid_down"
NOT_DRY = "Dry the coil (fan only), then off"
PAUSED = (
    "Bedroom Dry paused: the outdoor unit is heating for {}. "
    "MXZ won't turn Dry back on; choose it again when you want it."
)


class DryHead(MockHead):
    """A CN105-like head that also lists dry, as the Bedroom head does."""

    _attr_hvac_modes: ClassVar[list[HVACMode]] = [
        HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.DRY,
    ]


class Rig:
    """One entry over invented heads, and what it sent."""

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry, heads: list[str], notices) -> None:
        self.hass = hass
        self.entry = entry
        self.heads = heads
        self._notices = notices
        self.calls = record_calls(hass)

    @property
    def coord(self) -> MXZCoordinator:
        return self.entry.runtime_data

    def notices(self) -> list[str]:
        return [call.data["message"] for call in self._notices]

    def paused(self) -> list[str]:
        return [m for m in self.notices() if "Dry paused" in m]

    def sent(self, index: int) -> list[tuple[str, dict[str, Any]]]:
        """Every climate write to one head since the last ``forget``."""
        return head_writes(self.calls, self.heads[index])

    def modes_sent(self, index: int) -> list[str]:
        return [
            payload["hvac_mode"] for _service, payload in self.sent(index)
            if "hvac_mode" in payload
        ]

    def forget(self) -> None:
        self.calls.clear()

    def row(self, index: int) -> dict[str, Any]:
        return self.hass.states.get(_eid(self.hass, self.entry, "_plan")).attributes["zones"][index]

    def plan(self):
        return self.hass.states.get(_eid(self.hass, self.entry, "_plan"))

    def state(self, index: int) -> str:
        return self.hass.states.get(self.heads[index]).state

    def eid(self, suffix: str) -> str:
        return _eid(self.hass, self.entry, suffix)

    async def recompute(self) -> None:
        await self.coord.async_refresh()
        await self.hass.async_block_till_done()

    async def person(self, index: int, mode: str) -> None:
        """A person sets the head's mode on its remote; not recorded as MXZ's."""
        head = _head(self.hass, self.heads[index])
        head._attr_hvac_mode = mode
        head.async_write_ha_state()
        await self.hass.async_block_till_done()

    async def person_fan(self, index: int, fan: str) -> None:
        head = _head(self.hass, self.heads[index])
        head._attr_fan_mode = fan
        head.async_write_ha_state()
        await self.hass.async_block_till_done()

    async def switch(self, suffix: str, on: bool) -> None:
        await self.hass.services.async_call(
            "switch", "turn_on" if on else "turn_off", {"entity_id": self.eid(suffix)},
            blocking=True,
        )
        await self.hass.async_block_till_done()

    async def shared(self, mode: str) -> None:
        await self.hass.services.async_call(
            "select", "select_option",
            {"entity_id": self.eid("_shared_mode"), "option": mode}, blocking=True,
        )
        await self.hass.async_block_till_done()

    async def later(self, seconds: float) -> None:
        async_fire_time_changed(self.hass, dt_util.utcnow() + timedelta(seconds=seconds))
        await self.hass.async_block_till_done()


async def _setup(
    hass: HomeAssistant,
    *,
    zones: tuple[dict[str, Any], ...] = ({}, {ZONE_ALLOW_DRY: True}),
    head_cls: type[MockHead] = DryHead,
    idle: str = IDLE_ACTION_FAN_ONLY,
    hysteresis: int = 0,
    enabled: tuple[bool, ...] | None = None,
    temps: tuple[float, ...] | None = None,
    **options: Any,
) -> Rig:
    """Rooms (Rec room, Bedroom[, Office]) at 70 / target 70, one head each.

    ``zones`` holds each room's extra stored keys (Bedroom opts in to Allow Dry
    mode by default); every room is enabled unless ``enabled`` says otherwise.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [head_cls(chr(ord("a") + i)) for i in range(len(zones))]
    await register(hass, {"esphome": heads})
    for sensor, value in zip(SENSORS, temps or (70,) * len(zones)):
        await _set_temp(hass, sensor, value)
    notices = async_mock_service(hass, "notify", NOTIFY)
    stored = [
        {ZONE_NAME: NAMES[i], ZONE_CLIMATE: head.entity_id, ZONE_SENSOR: SENSORS[i], **extra}
        for i, (head, extra) in enumerate(zip(heads, zones))
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_ZONES: stored,
            CONF_IDLE_ACTION: idle,
            CONF_MODE_HYSTERESIS: hysteresis,
            CONF_NOTIFY_SERVICE: f"notify.{NOTIFY}",
            **options,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    slugs = ("primary", "secondary", "zone_3")[: len(zones)]
    enabled = enabled or (True,) * len(zones)
    for slug, on in zip(slugs, enabled):
        if on:
            await _set_target(hass, _eid(hass, entry, f"_{slug}_target"), 70)
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": _eid(hass, entry, f"_{slug}_enable")},
                blocking=True,
            )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_coordinator_enable")},
        blocking=True,
    )
    await hass.async_block_till_done()
    rig = Rig(hass, entry, [head.entity_id for head in heads], notices)
    await rig.recompute()
    rig.forget()
    return rig


# ---------------------------------------------------------------------------
# Off (the default): dry is put back, exactly as before
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "stored", [None, False, "yes", 1], ids=["no_key", "false", "string", "int"]
)
async def test_option_off_dry_is_still_healed(hass: HomeAssistant, stored: Any) -> None:
    """No key (every entry before 3.15.0), False, or anything but True: healed in 20 s."""
    rig = await _setup(hass, zones=({}, {} if stored is None else {ZONE_ALLOW_DRY: stored}))
    assert [zone.allow_dry for zone in rig.coord.zones] == [False, False]
    await rig.person(1, "dry")
    assert (rig.heads[1], "band") in rig.coord._heal_timers
    await rig.later(BAND_DRIFT_DELAY + 1)
    assert rig.state(1) == "fan_only"  # the plan's idle action, re-applied
    assert rig.notices() == [
        (
            "A head was off its coordinated mode (drift or just re-enabled) - "
            "re-applied via the coordinator."
        )
    ]
    # A room that never opted in keeps exactly the row it had before.
    assert not {"allow_dry", "dry", "dry_vote"} & set(rig.row(1))
    assert not {"allow_dry", "dry", "dry_vote"} & set(rig.row(0))


async def test_option_off_restart_in_dry_is_put_back(hass: HomeAssistant) -> None:
    """Opted out, a head found in dry after a restart is driven back by the plan."""
    rig = await _setup(hass, zones=({}, {}))
    await rig.person(1, "dry")
    await _shutdown(hass, rig.entry)
    await _startup(hass, rig.entry)
    await rig.recompute()
    assert rig.state(1) == "fan_only"


async def test_a_disabled_room_is_held_off_even_with_the_option(hass: HomeAssistant) -> None:
    """The option acts only while the room is enabled: a disabled room is held off."""
    rig = await _setup(hass, enabled=(True, False))
    await rig.person(1, "dry")
    await rig.recompute()
    assert rig.state(1) == "off"
    assert rig.modes_sent(1) == ["off"]
    assert rig.row(1)["dry"] is None


async def test_an_own_remote_rooms_dry_still_has_no_vote(hass: HomeAssistant) -> None:
    """Disabled and left to its remote, a room's dry is the person's, and asks no direction."""
    rig = await _setup(
        hass, zones=({}, {ZONE_ALLOW_DRY: True, ZONE_HANDS_OFF: True}), enabled=(True, False)
    )
    await rig.shared("heat")
    await rig.recompute()
    assert rig.state(1) == "off"  # released
    rig.forget()
    await rig.person(1, "dry")
    await rig.recompute()
    assert (rig.heads[1], "band") not in rig.coord._heal_timers
    assert (rig.row(1)["dry"], rig.row(1)["dry_vote"], rig.row(1)["hands_off_vote"]) == (
        None, None, None,
    )
    assert rig.plan().state == "heat"
    assert rig.state(1) == "dry" and rig.sent(1) == []


async def test_away_eco_leaves_a_dry_head_alone(hass: HomeAssistant) -> None:
    """Eco idle doesn't change it: the dry head is held and votes."""
    rig = await _setup(hass)
    await rig.switch("_eco_idle", True)
    await rig.recompute()
    rig.forget()
    await rig.person(1, "dry")
    await rig.recompute()
    assert (rig.row(1)["dry"], rig.row(1)["dry_vote"]) == ("held", "cool")
    assert rig.state(1) == "dry" and rig.sent(1) == []


async def test_a_standby_hold_parks_a_dry_head(hass: HomeAssistant) -> None:
    """A standby hold parks every head, a dry one too."""
    hass.states.async_set(INHIBIT, "off")
    rig = await _setup(
        hass, **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_OFF}
    )
    await rig.person(1, "dry")
    await rig.recompute()
    assert rig.state(1) == "dry"
    hass.states.async_set(INHIBIT, "on")
    await hass.async_block_till_done()
    await rig.recompute()
    assert rig.state(1) == "off"
    assert rig.row(1)["dry"] is None


# ---------------------------------------------------------------------------
# On: a person's dry is left alone, and the room's goal pauses
# ---------------------------------------------------------------------------
async def test_allow_dry_leaves_a_persons_dry_alone(hass: HomeAssistant) -> None:
    """No heal, no writes of any kind, and the room's call pauses while in dry."""
    rig = await _setup(hass)
    assert [zone.allow_dry for zone in rig.coord.zones] == [False, True]
    await rig.person(1, "dry")
    assert (rig.heads[1], "band") not in rig.coord._heal_timers
    await rig.later(BAND_DRIFT_DELAY + 30)  # past the band wait and any refresh cooldown
    assert rig.row(1)["dry"] == "held"  # the head's change asked for a recompute
    for temp in (80, 60, 70):  # a cool call, a heat call, at target: all paused
        await _set_temp(hass, SENSORS[1], temp)
        await rig.recompute()
        row = rig.row(1)
        assert (row["demand"], row["engage"]) == ("neutral", "satisfied"), temp
        assert (row["dry"], row["dry_vote"]) == ("held", "cool"), temp
    assert rig.state(1) == "dry"
    assert [c for c in rig.calls if c["service_data"].get("entity_id") == rig.heads[1]] == []
    assert rig.notices() == []
    # The room tile says what the head is doing, not idle.
    tile = hass.states.get(rig.eid("_secondary_thermostat"))
    assert tile.attributes["hvac_action"] == HVACAction.DRYING


async def test_a_band_wait_armed_before_the_room_was_enabled_heals_nothing(
    hass: HomeAssistant,
) -> None:
    """Dry seen while the room was disabled arms the band wait; enabled before
    it runs out, the wait finds a dry it may keep: no heal, no notice."""
    rig = await _setup(hass, enabled=(True, False))
    await rig.switch("_coordinator_enable", False)
    await rig.person(1, "dry")
    assert (rig.heads[1], "band") in rig.coord._heal_timers
    await rig.switch("_secondary_enable", True)
    await rig.switch("_coordinator_enable", True)
    await rig.recompute()
    assert rig.row(1)["dry"] == "held"
    rig.forget()
    await rig.later(BAND_DRIFT_DELAY + 1)
    assert rig.state(1) == "dry"
    assert rig.sent(1) == []
    assert rig.notices() == []


async def test_dry_pause_ends_when_the_head_leaves_dry(hass: HomeAssistant) -> None:
    """Out of dry the room's goal is back: a warm room calls and runs cool again."""
    rig = await _setup(hass)
    await rig.person(1, "dry")
    await _set_temp(hass, SENSORS[1], 76)
    await rig.recompute()
    assert rig.row(1)["demand"] == "neutral"
    await rig.person(1, "fan_only")
    await rig.recompute()
    assert (rig.row(1)["demand"], rig.row(1)["dry"]) == ("cool", None)
    assert rig.state(1) == "cool"


async def test_a_manual_fan_pick_in_dry_is_kept(hass: HomeAssistant) -> None:
    """A fan speed picked in dry gets no fan write, in dry or after a park for heat."""
    rig = await _setup(hass)
    await rig.person(1, "dry")
    await rig.person_fan(1, "low")
    await rig.recompute()
    assert rig.sent(1) == []
    await _set_temp(hass, SENSORS[0], 65)  # Rec room needs heat
    await rig.recompute()
    assert rig.state(1) == "fan_only"
    assert [s for s, _ in rig.sent(1)] and all(s != "set_fan_mode" for s, _ in rig.sent(1))
    assert hass.states.get(rig.heads[1]).attributes["fan_mode"] == "low"
    assert hass.states.get(rig.eid("_secondary_fan_auto")).state == "off"  # held
    assert rig.row(1)["fan_hold"] is True


# ---------------------------------------------------------------------------
# The cool vote, at hands-off rank
# ---------------------------------------------------------------------------
async def test_dry_votes_cool_when_nobody_calls(hass: HomeAssistant) -> None:
    """Resting in heat with no call: a dry head turns the shared mode to cool."""
    rig = await _setup(hass)
    await rig.shared("heat")
    await rig.recompute()
    assert rig.plan().state == "heat"
    await rig.person(1, "dry")
    await rig.recompute()
    assert rig.plan().state == "cool"
    # A vote moved it, not a room's run: that flip starts no mode-change wait.
    assert rig.coord._mode_by_votes is True
    assert rig.row(1)["dry"] == "held"
    assert rig.sent(1) == []


async def test_dry_vote_loses_to_an_enabled_rooms_heat_call(hass: HomeAssistant) -> None:
    """An enabled room calling heat wins the shared mode over a dry vote."""
    rig = await _setup(hass)
    await rig.person(1, "dry")
    await rig.recompute()
    assert rig.plan().state == "cool"
    await _set_temp(hass, SENSORS[0], 65)
    await rig.recompute()
    assert rig.plan().state == "heat"
    assert rig.state(0) == "heat"
    assert rig.state(1) == "fan_only"  # parked at the idle action
    assert rig.paused() == [PAUSED.format("Rec room")]


async def test_cool_lockout_blocks_the_dry_vote(hass: HomeAssistant) -> None:
    """With cool locked out the dry head asks nothing; still nobody wants heat, so it stays."""
    rig = await _setup(hass)
    await rig.shared("heat")
    await rig.switch("_cool_lockout", True)
    await rig.person(1, "dry")
    await rig.recompute()
    assert rig.plan().state == "heat"
    assert (rig.row(1)["dry"], rig.row(1)["dry_vote"]) == ("held", None)
    assert rig.state(1) == "dry" and rig.sent(1) == []
    await rig.switch("_cool_lockout", False)
    await rig.recompute()
    assert rig.plan().state == "cool"
    assert rig.row(1)["dry_vote"] == "cool"


async def test_resting_heat_inside_the_wait_parks_nothing(hass: HomeAssistant) -> None:
    """Heat held only by the mode-change wait isn't a room wanting heat.

    The dry head stays, and the wait's wakeup is armed for the vote (the
    replay counts the dry vote as the live compute does); once the wait is
    over the mode turns to cool.
    """
    rig = await _setup(hass, hysteresis=600)
    await rig.shared("heat")
    await rig.recompute()
    await rig.person(1, "dry")
    await rig.recompute()
    assert rig.plan().state == "heat"
    assert rig.row(1)["dry"] == "held"
    assert rig.state(1) == "dry" and rig.sent(1) == []
    assert rig.coord._dwell_timer is not None
    expire_dwell(rig.coord)
    await rig.recompute()
    assert rig.plan().state == "cool"


async def test_a_dry_rooms_safety_sensor_never_lifts_its_vote(hass: HomeAssistant) -> None:
    """Bedroom (dry, its Safety priority sensor on) after Rec room run by its own remote in heat.

    Rec room's heat vote comes first by room order. Were the dry vote lifted
    to the safety tier it would win cool; it stays at hands-off rank, so heat
    wins and the dry head is parked.
    """
    hass.states.async_set(SAFETY, "on")
    rig = await _setup(
        hass,
        zones=({ZONE_HANDS_OFF: True}, {ZONE_ALLOW_DRY: True, ZONE_SAFETY_PRIORITY_ENTITY: SAFETY}),
        enabled=(False, True),
    )
    await rig.recompute()
    assert rig.state(0) == "off"  # released
    await rig.person(0, "heat")
    await rig.person(1, "dry")
    await rig.recompute()
    assert rig.plan().state == "heat"
    assert rig.state(1) == "fan_only"
    assert rig.paused() == [PAUSED.format("Rec room")]


async def test_safety_priority_beats_dry(hass: HomeAssistant) -> None:
    """Bedroom (dry) comes first; Office runs heat on its own remote.

    By room order the dry vote wins cool. With Office's Safety priority
    sensor on, its heat outranks the dry vote: heat, and the dry head parks.
    """
    hass.states.async_set(SAFETY, "off")
    rig = await _setup(
        hass,
        zones=(
            {},
            {ZONE_ALLOW_DRY: True},
            {ZONE_HANDS_OFF: True, ZONE_SAFETY_PRIORITY_ENTITY: SAFETY},
        ),
        enabled=(True, True, False),
    )
    await rig.recompute()
    await rig.person(2, "heat")
    await rig.person(1, "dry")
    await rig.recompute()
    assert rig.plan().state == "cool"
    assert rig.row(1)["dry"] == "held"
    hass.states.async_set(SAFETY, "on")
    await hass.async_block_till_done()
    await rig.recompute()
    assert rig.plan().state == "heat"
    assert rig.plan().attributes["mode_by_safety"] is True
    assert rig.state(1) == "fan_only"
    assert rig.paused() == [PAUSED.format("Office")]


async def test_freeze_floor_beats_dry(hass: HomeAssistant) -> None:
    """A tripped freeze floor's heat wins over the dry vote, and the dry head parks."""
    rig = await _setup(
        hass,
        zones=({ZONE_FLOOR_LOW: 50.0}, {ZONE_ALLOW_DRY: True}),
        enabled=(False, True),
    )
    await rig.person(1, "dry")
    await rig.recompute()
    assert rig.plan().state == "cool"
    assert rig.row(1)["dry"] == "held"
    await _set_temp(hass, SENSORS[0], 45)
    await rig.recompute()
    assert rig.plan().state == "heat"
    assert rig.plan().attributes["mode_by_floor"] is True
    assert rig.state(1) == "fan_only"
    assert rig.paused() == [PAUSED.format("Rec room")]


def test_the_dry_vote_is_always_hands_off_rank() -> None:
    """The arbitration entry for a dry head: a read-only vote at PRIORITY_VOTE, safety or not."""
    for safety in (False, True):
        entries = MXZCoordinator._zone_arbitration_pair(
            demand="neutral", vote=None, safety=safety, index=1, dry="cool"
        )
        dry = entries[-1]
        assert (dry.direction, dry.priority, dry.read_only) == ("cool", (PRIORITY_VOTE, 1), True)
        assert entries[0].priority[0] == (PRIORITY_SAFETY if safety else 0)
    assert len(MXZCoordinator._zone_arbitration_pair(
        demand="neutral", vote=None, safety=False, index=1,
    )) == 2


# ---------------------------------------------------------------------------
# Heat wins on the shared outdoor unit: park once, notify once, never restore
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("idle", "parked"),
    [
        (IDLE_ACTION_FAN_ONLY, "fan_only"),
        (IDLE_ACTION_OFF, "off"),
        (IDLE_ACTION_OFF_AFTER_DRY, "fan_only"),  # the coil-dry run first
    ],
)
async def test_heat_parks_the_dry_head_once_and_never_restores_dry(
    hass: HomeAssistant, idle: str, parked: str
) -> None:
    """Three rooms on one outdoor unit: Rec room needs heat while Bedroom is in dry."""
    rig = await _setup(hass, zones=({}, {ZONE_ALLOW_DRY: True}, {}), idle=idle)
    await rig.person(1, "dry")
    await rig.recompute()
    assert (rig.plan().state, rig.row(1)["dry"]) == ("cool", "held")
    await _set_temp(hass, SENSORS[0], 65)
    await rig.recompute()
    assert rig.plan().state == "heat"
    assert rig.state(0) == "heat"
    assert rig.state(1) == parked
    assert rig.modes_sent(1) == [parked]  # one park, straight to the idle action
    assert rig.paused() == [PAUSED.format("Rec room")]
    for _ in range(3):  # later computes, Rec room still heating: no second notice
        await rig.recompute()
    assert rig.paused() == [PAUSED.format("Rec room")]
    # Rec room reaches its target; heat stays as the resting mode. Dry is not
    # put back, then or ever: MXZ never sends dry.
    await _set_temp(hass, SENSORS[0], 70)
    await rig.recompute()
    await rig.recompute()
    assert rig.state(1) != "dry"
    assert rig.row(1)["dry"] is None
    assert "dry" not in rig.modes_sent(1)
    assert all("dry" not in rig.modes_sent(i) for i in range(3))
    assert rig.modes_sent(1) == [parked]
    assert rig.paused() == [PAUSED.format("Rec room")]


async def test_a_new_dry_pick_while_heat_runs_is_parked_and_told_again(
    hass: HomeAssistant,
) -> None:
    """One notice per park: a person picking dry again is a new one."""
    rig = await _setup(hass)
    await rig.person(1, "dry")
    await _set_temp(hass, SENSORS[0], 65)
    await rig.recompute()
    assert rig.paused() == [PAUSED.format("Rec room")]
    await rig.person(1, "dry")
    await rig.recompute()
    assert rig.state(1) == "fan_only"
    assert rig.paused() == [PAUSED.format("Rec room")] * 2


class StubbornHead(DryHead):
    """A head that ignores every mode write while it is in dry."""

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if self._attr_hvac_mode != HVACMode.DRY:
            await super().async_set_hvac_mode(hvac_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if self._attr_hvac_mode == HVACMode.DRY:
            kwargs.pop("hvac_mode", None)
        await super().async_set_temperature(**kwargs)


async def test_a_refused_park_is_retried_with_no_second_notice(hass: HomeAssistant) -> None:
    """The plan shows the room parked while the head stays in dry; one notice only."""
    rig = await _setup(hass, head_cls=StubbornHead)
    await rig.person(1, "dry")
    await _set_temp(hass, SENSORS[0], 65)
    for _ in range(3):
        await rig.recompute()
        assert rig.state(1) == "dry"
        assert rig.row(1)["dry"] == "parked"
        assert rig.row(1)["dry_vote"] == "cool"  # still asked; heat outranks it
    assert rig.modes_sent(1).count("fan_only") >= 3  # re-sent each compute
    assert rig.paused() == [PAUSED.format("Rec room")]


async def test_coordinator_off_parks_nothing_and_tells_nothing(hass: HomeAssistant) -> None:
    """With the Coordinator off MXZ writes no head, so it parks none and sends no notice."""
    rig = await _setup(hass)
    await rig.person(1, "dry")
    await rig.switch("_coordinator_enable", False)
    await _set_temp(hass, SENSORS[0], 65)
    await rig.recompute()
    assert rig.state(1) == "dry"
    assert rig.paused() == []


# ---------------------------------------------------------------------------
# Leaving dry starts the coil-dry run
# ---------------------------------------------------------------------------
async def test_leaving_dry_starts_the_coil_dry_run(hass: HomeAssistant) -> None:
    """Turned off from dry, the head dries its coil in fan_only, then goes off."""
    rig = await _setup(hass, idle=IDLE_ACTION_OFF_AFTER_DRY)
    assert rig.state(1) == "off"  # never ran: no coil-dry owed
    await rig.person(1, "dry")
    await rig.recompute()
    # Dry runs on long past the last compute: the coil-dry run is owed from
    # when the head leaves dry, not from when MXZ last looked.
    age_coil_dry(rig.coord, rig.heads[1], rig.coord.coil_dry_seconds + 5)
    await rig.person(1, "off")
    await rig.recompute()
    assert rig.state(1) == "fan_only"
    age_coil_dry(rig.coord, rig.heads[1], rig.coord.coil_dry_seconds + 5)
    await rig.recompute()
    assert rig.state(1) == "off"


async def test_option_off_dry_owes_no_coil_dry(hass: HomeAssistant) -> None:
    """The control: opted out, the dry head is put straight back to off, as before."""
    rig = await _setup(hass, zones=({}, {}), idle=IDLE_ACTION_OFF_AFTER_DRY)
    await rig.person(1, "dry")
    await rig.recompute()
    assert rig.state(1) == "off"
    assert rig.coord._last_active.get(rig.heads[1]) is None


# ---------------------------------------------------------------------------
# Restart continuity
# ---------------------------------------------------------------------------
async def test_restart_leaves_a_dry_head_alone_and_keeps_its_vote(hass: HomeAssistant) -> None:
    """After a restart: no write to the dry head, its vote back, its fan pick kept."""
    rig = await _setup(hass)
    await rig.person(1, "dry")
    await rig.person_fan(1, "low")
    await rig.recompute()
    assert hass.states.get(rig.eid("_secondary_fan_auto")).state == "off"
    await rig.shared("heat")  # resting heat: the vote has to turn it back
    await rig.person(1, "dry")
    await rig.recompute()
    await _shutdown(hass, rig.entry)
    rig.forget()
    await _startup(hass, rig.entry)
    await rig.recompute()
    await rig.later(BAND_DRIFT_DELAY + 30)
    assert rig.state(1) == "dry"
    assert rig.sent(1) == []
    assert (rig.row(1)["dry"], rig.row(1)["dry_vote"]) == ("held", "cool")
    assert rig.plan().state == "cool"
    assert hass.states.get(rig.heads[1]).attributes["fan_mode"] == "low"
    assert hass.states.get(rig.eid("_secondary_fan_auto")).state == "off"
    assert rig.notices() == []


# ---------------------------------------------------------------------------
# Cloud heads (beta): the same rules; a late report of dry is not a person
# ---------------------------------------------------------------------------
async def _cloud(hass: HomeAssistant, *, allow: bool = True):
    hass.config.units = US_CUSTOMARY_SYSTEM
    a, b = ComfortHead("a"), ComfortHead("b")
    await register(hass, {COMFORT: [a, b]})
    notices = async_mock_service(hass, "notify", NOTIFY)
    entry = await setup_rooms(
        hass,
        [
            (a, {ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD}),
            (b, {ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD, **({ZONE_ALLOW_DRY: True} if allow else {})}),
        ],
        options={CONF_NOTIFY_SERVICE: f"notify.{NOTIFY}"},
    )
    await past_startup(hass, entry)
    for head in (a, b):
        past_window(entry, head.entity_id)
    return entry, a, b, notices


async def test_cloud_dry_is_left_alone_then_parked_for_heat(hass: HomeAssistant) -> None:
    entry, a, b, notices = await _cloud(hass)
    coord = entry.runtime_data
    calls = record_calls(hass)
    b.device["mode"] = "dry"  # its own remote
    b.poll()
    await hass.async_block_till_done()
    assert (b.entity_id, "band") not in coord._heal_timers
    await coord.async_refresh()
    await hass.async_block_till_done()
    row = hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][1]
    assert (row["dry"], row["dry_vote"], row["demand"]) == ("held", "cool", "neutral")
    assert head_writes(calls, b.entity_id) == []
    await _set_temp(hass, SENSORS[0], 65)
    past_window(entry, a.entity_id)
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert b.device["mode"] == "fan_only"
    assert ("set_hvac_mode", {"hvac_mode": "fan_only"}) in head_writes(calls, b.entity_id)
    paused = [c.data["message"] for c in notices if "Dry paused" in c.data["message"]]
    assert paused == [
        (
            "Room 2 Dry paused: the outdoor unit is heating for Room 1. "
            "MXZ won't turn Dry back on; choose it again when you want it."
        )
    ]
    # Inside the park's confirm wait a late poll still shows dry: not a
    # person's dry, so no heal, no hold, no second park or notice.
    calls.clear()
    b.report(mode="dry")
    await hass.async_block_till_done()
    assert (b.entity_id, "band") not in coord._heal_timers
    await coord.async_refresh()
    await hass.async_block_till_done()
    row = hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][1]
    assert (row["dry"], row["dry_vote"]) == (None, None)
    assert head_writes(calls, b.entity_id) == []
    assert len([c for c in notices if "Dry paused" in c.data["message"]]) == 1


async def test_cloud_dry_without_the_option_is_healed(hass: HomeAssistant) -> None:
    entry, _a, b, _notices = await _cloud(hass, allow=False)
    b.device["mode"] = "dry"
    b.poll()
    await hass.async_block_till_done()
    assert (b.entity_id, "band") in entry.runtime_data._heal_timers


# ---------------------------------------------------------------------------
# The room page
# ---------------------------------------------------------------------------
async def test_room_page_offers_allow_dry_off_by_default(hass: HomeAssistant) -> None:
    """Shown for a head listing dry, off by default; on stores True, off removes it."""
    rig = await _setup(hass, zones=({}, {}))
    entry = rig.entry
    manager = hass.config_entries.options
    before = [dict(zone) for zone in entry.data[CONF_ZONES]]
    result = await open_options_page(hass, entry, "room_2")
    assert "allow_dry" in fields(result)
    assert default(result, "allow_dry") is False
    result = await submit(manager, result, prefilled(result))
    await hass.async_block_till_done()
    assert entry.data[CONF_ZONES] == before  # an untouched save stores nothing new
    result = await open_options_page(hass, entry, "room_2")
    result = await submit(manager, result, {**prefilled(result), "allow_dry": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_ZONES] == [before[0], {**before[1], ZONE_ALLOW_DRY: True}]
    for store in (entry.data, entry.options):
        assert not {"allow_dry", "secondary_allow_dry"} & set(store)
    assert [z.allow_dry for z in entry.runtime_data.zones] == [False, True]
    result = await open_options_page(hass, entry, "room_2")
    assert default(result, "allow_dry") is True
    result = await submit(manager, result, {**prefilled(result), "allow_dry": False})
    await hass.async_block_till_done()
    assert entry.data[CONF_ZONES] == before
    assert [z.allow_dry for z in entry.runtime_data.zones] == [False, False]


async def test_room_page_hides_allow_dry_without_dry(hass: HomeAssistant) -> None:
    """A head that lists no dry gets no option, and saving its page leaves the key alone."""
    rig = await _setup(hass, zones=({}, {ZONE_ALLOW_DRY: True}), head_cls=MockHead)
    entry = rig.entry
    before = [dict(zone) for zone in entry.data[CONF_ZONES]]
    for page in ("room_1", "room_2"):
        result = await open_options_page(hass, entry, page)
        assert "allow_dry" not in fields(result), page
    result = await submit(hass.config_entries.options, result, prefilled(result))
    await hass.async_block_till_done()
    assert entry.data[CONF_ZONES] == before


def test_room_page_copy_is_translated() -> None:
    root = Path("custom_components/mxz_coordinator")
    copies = []
    for path in (root / "strings.json", root / "translations/en.json"):
        room = json.loads(path.read_text())["options"]["step"]["room"]
        copies.append((room["data"]["allow_dry"], room["data_description"]["allow_dry"]))
    assert copies[0] == copies[1]
    label, description = copies[0]
    assert label == "Allow Dry mode"
    assert description.startswith("Off (the default): if this head is put in Dry, MXZ puts it back")
    assert "doesn't turn Dry back on" in description
    assert "Shown only for a head that offers Dry." in description


# ---------------------------------------------------------------------------
# The coil-dry label
# ---------------------------------------------------------------------------
def test_coil_dry_label_is_renamed_and_the_key_is_kept() -> None:
    """"Dry the coil (fan only), then off" everywhere it is shown; stored as off_after_dry."""
    assert IDLE_ACTION_OFF_AFTER_DRY == "off_after_dry"
    assert IDLE_ACTION_LABELS[IDLE_ACTION_OFF_AFTER_DRY] == NOT_DRY
    root = Path("custom_components/mxz_coordinator")
    for path in (root / "strings.json", root / "translations/en.json"):
        text = path.read_text()
        strings = json.loads(text)
        assert strings["selector"]["idle_action"]["options"]["off_after_dry"] == f"{NOT_DRY} (default)"
        assert "Off after drying" not in text, path
    readme = Path("README.md").read_text()
    assert f"**{NOT_DRY}**" in readme
    assert "This isn't the heads' Dry (dehumidify) mode; see [Allow Dry mode]" in readme
    # The old name appears only where the README says it was renamed.
    old = [line for line in readme.splitlines() if "Off after drying" in line]
    assert old and all("used to read" in line for line in old), old
    # The current guides use the new name. (MIGRATION.md keeps each release's
    # own wording, and a 3.12 screenshot's alt text describes that screenshot.)
    for name in ("FEATURES.md", "SETUP.md", "TROUBLESHOOTING.md"):
        text = (Path("docs") / name).read_text()
        old = [line for line in text.splitlines() if "Off after drying" in line]
        assert all("Until 3.15.0" in line for line in old), (name, old)
        assert NOT_DRY in text, name


async def test_a_stored_off_after_dry_loads_unchanged(hass: HomeAssistant) -> None:
    """Nothing migrates: the stored key is still the idle action in use."""
    rig = await _setup(hass, idle=IDLE_ACTION_OFF_AFTER_DRY)
    assert rig.entry.data[CONF_IDLE_ACTION] == "off_after_dry"
    assert rig.coord.idle_action == "off_after_dry"
    assert rig.plan().attributes["idle_action"] == "off_after_dry"
