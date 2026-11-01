"""Dry on the MXZ room thermostat (3.16.0), for a room with Allow Dry mode on.

Drew's design (Oct 31, 2026, /root/mxz-work/dry-entity/DESIGN.md) and his two
decisions, each pinned by a behavior test here:

* Dry shows in the room thermostat's modes only with the room's Allow Dry mode
  on AND its head listing dry now. Every other room, a room stored before
  3.16.0 included, offers exactly Off and Heat/Cool, as before.
* Picking Dry enables the room and sends the head dry once: no fan, vane or
  setpoint write, and a manual fan pick is kept. From then on it is the 3.15.0
  person's dry (a cool vote at hands-off rank, parked with one notice if heat
  wins, never put back).
* The thermostat shows Dry whenever MXZ keeps the head in dry, however it got
  there: drying while held, idle while parked.
* Heat/Cool from Dry takes the head back to the plan; Off disables the room.
  Leaving dry owes the coil-dry run as leaving cool does.
* Allow Dry turned off, or the head dropping dry from its list, while in Dry:
  Dry leaves the modes, the plan has the head back inside the band wait, the
  thermostat shows Heat/Cool, and one notice says why.
* A restart in Dry: held, shown as Dry, no writes.
* Drew, Q1: a stale room sensor keeps Dry held. Q2: a cool lockout still
  offers Dry (it just can't win the mode).

The heads are invented (CN105-like on the esphome platform); nothing here is a
hardware result.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.mxz_coordinator.const import (
    BAND_DRIFT_DELAY,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_ZONES,
    DRY_PICK_WAIT,
    EVIDENCE_HA_WRITE,
    IDLE_ACTION_OFF_AFTER_DRY,
    INHIBIT_ACTION_OFF,
    ZONE_ALLOW_DRY,
    ZONE_EVIDENCE_BASIS,
    ZONE_HANDS_OFF,
    ZONE_MAX_AGE,
)
from tests.clocks import age_coil_dry
from tests.cloud_heads import record as record_calls
from tests.cloud_heads import writes as head_writes
from tests.flow_pages import open_options_page, prefilled, submit
from tests.test_drive import MockHead, _eid, _set_temp
from tests.test_dry_mode import (
    INHIBIT,
    PAUSED,
    SENSORS,
    DryHead,
    Rig,
    StubbornHead,
    _cloud,
    _setup,
)
from tests.test_floor import _tick
from tests.test_startup_continuity import _head, _shutdown, _startup

TILE = ("_primary_thermostat", "_secondary_thermostat")
TWO = ["off", "heat_cool"]
THREE = ["off", "heat_cool", "dry"]
DRY = [("set_hvac_mode", {"hvac_mode": "dry"})]
ENDED = "Bedroom Dry ended: {}. MXZ is back on the room's temperature goal."


def tile(rig: Rig, index: int = 1):
    return rig.hass.states.get(rig.eid(TILE[index]))


async def pick(rig: Rig, mode: str, index: int = 1) -> None:
    """A person picks a mode on the room's MXZ thermostat."""
    await rig.hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": rig.eid(TILE[index]), "hvac_mode": mode},
        blocking=True,
    )
    await rig.hass.async_block_till_done()


def other_writes(rig: Rig) -> list[tuple[str, str]]:
    """Every service call since ``forget`` that isn't a room switch or a tile call."""
    return [
        (c["domain"], c["service"]) for c in rig.calls
        if c["domain"] not in ("switch", "notify")
        and c["service_data"].get("entity_id") not in (rig.eid(TILE[0]), rig.eid(TILE[1]))
    ]


class LateHead(DryHead):
    """An ESPHome-like head whose report of a mode write arrives later, once ``late``."""

    late = False
    asked: str | None = None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if not self.late:
            await super().async_set_hvac_mode(hvac_mode)
            return
        self.asked = hvac_mode

    def report(self) -> None:
        self._attr_hvac_mode = self.asked
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# When Dry shows (and every room without it is unchanged)
# ---------------------------------------------------------------------------
async def test_dry_is_offered_only_with_the_option_on_and_a_head_listing_dry(
    hass: HomeAssistant,
) -> None:
    """The positive case; both rooms' tiles otherwise unchanged."""
    rig = await _setup(hass)
    assert tile(rig, 1).attributes["hvac_modes"] == THREE
    assert tile(rig, 0).attributes["hvac_modes"] == TWO  # the room beside it: no option
    assert (tile(rig, 1).state, tile(rig, 0).state) == ("heat_cool", "heat_cool")


@pytest.mark.parametrize(
    ("zones", "head_cls"),
    [
        (({}, {}), DryHead),  # option off (no key: every room stored before 3.15.0)
        (({}, {ZONE_ALLOW_DRY: False}), DryHead),  # option off, stored False
        (({}, {ZONE_ALLOW_DRY: True}), MockHead),  # option on, head lists no dry
    ],
    ids=["no_key", "stored_false", "head_lists_no_dry"],
)
async def test_without_the_option_or_a_dry_head_the_tile_is_unchanged(
    hass: HomeAssistant, zones: tuple[dict[str, Any], ...], head_cls: type[MockHead]
) -> None:
    """Off and Heat/Cool only; Dry refused with nothing written; a remote dry
    is still shown as Heat/Cool and healed in the band wait, as in 3.15.0."""
    rig = await _setup(hass, zones=zones, head_cls=head_cls)
    before = dict(tile(rig).attributes)
    for i in (0, 1):
        assert tile(rig, i).attributes["hvac_modes"] == TWO
    with pytest.raises(ServiceValidationError):
        await pick(rig, "dry")
    assert rig.sent(1) == [] and other_writes(rig) == []
    assert dict(tile(rig).attributes) == before
    if head_cls is DryHead:
        await rig.person(1, "dry")
        assert tile(rig).state == "heat_cool"
        assert tile(rig).attributes["hvac_modes"] == TWO
        await rig.later(BAND_DRIFT_DELAY + 1)
        assert rig.state(1) == "fan_only"
        assert rig.paused() == [] and not [m for m in rig.notices() if "Dry ended" in m]


async def test_a_pre_3_16_room_with_the_option_gains_only_the_dry_mode(
    hass: HomeAssistant,
) -> None:
    """A 3.15.0 room with Allow Dry on: Dry added to its modes, every other
    attribute exactly as the same room without the option shows it."""
    rig = await _setup(hass, zones=({}, {ZONE_ALLOW_DRY: True}))
    on = dict(tile(rig, 1).attributes)
    off = dict(tile(rig, 0).attributes)
    assert on.pop("hvac_modes") == THREE and off.pop("hvac_modes") == TWO
    # The rooms differ only by name and their own sensor's diagnostics.
    assert set(on) == set(off)
    for key in ("supported_features", "fan_modes", "min_temp", "max_temp", "temperature"):
        assert on[key] == off[key], key


async def test_a_cool_lockout_still_offers_dry(hass: HomeAssistant) -> None:
    """Drew, Q2: Dry stays in the modes and can be picked; it just has no vote,
    and heat for another room parks it with the one notice."""
    rig = await _setup(hass)
    await rig.shared("heat")
    await rig.switch("_cool_lockout", True)
    assert tile(rig).attributes["hvac_modes"] == THREE
    rig.forget()
    await pick(rig, "dry")
    assert rig.sent(1) == DRY
    await rig.recompute()
    assert (rig.row(1)["dry"], rig.row(1)["dry_vote"]) == ("held", None)
    assert rig.plan().state == "heat"
    assert (tile(rig).state, tile(rig).attributes["hvac_action"]) == ("dry", HVACAction.DRYING)
    await _set_temp(hass, SENSORS[0], 65)  # Rec room needs heat
    await rig.recompute()
    assert rig.state(1) == "fan_only"
    assert rig.paused() == [PAUSED.format("Rec room")]


# ---------------------------------------------------------------------------
# Choosing Dry
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("start", ["off", "heat_cool"])
async def test_picking_dry_enables_the_room_and_sends_dry_once(
    hass: HomeAssistant, start: str
) -> None:
    """One set_hvac_mode dry to the head, no fan, vane or setpoint write, from
    a disabled room or a running one; a manual fan pick is kept."""
    rig = await _setup(hass, enabled=(True, start == "heat_cool"))
    await rig.recompute()
    assert tile(rig).state == start
    await rig.person_fan(1, "low")  # a person's speed, held
    await rig.recompute()
    assert hass.states.get(rig.eid("_secondary_fan_auto")).state == "off"
    rig.forget()
    await pick(rig, "dry")
    await rig.recompute()
    await rig.later(BAND_DRIFT_DELAY + 30)
    assert rig.sent(1) == DRY
    assert other_writes(rig) == [("climate", "set_hvac_mode")]
    assert hass.states.get(rig.eid("_secondary_enable")).state == "on"
    assert rig.state(1) == "dry"
    assert (rig.row(1)["dry"], rig.row(1)["dry_vote"]) == ("held", "cool")
    assert (tile(rig).state, tile(rig).attributes["hvac_action"]) == ("dry", HVACAction.DRYING)
    assert hass.states.get(rig.heads[1]).attributes["fan_mode"] == "low"
    assert hass.states.get(rig.eid("_secondary_fan_auto")).state == "off"
    assert rig.notices() == []


async def test_picking_dry_on_a_head_already_in_dry_sends_nothing(hass: HomeAssistant) -> None:
    """A disabled own-remote room whose head a person already runs in dry: Dry
    just enables the room, and MXZ keeps the head as it is."""
    rig = await _setup(
        hass, zones=({}, {ZONE_ALLOW_DRY: True, ZONE_HANDS_OFF: True}), enabled=(True, False)
    )
    await rig.recompute()
    assert rig.state(1) == "off"  # released to its remote
    await rig.person(1, "dry")
    await rig.recompute()
    assert (rig.state(1), tile(rig).state) == ("dry", "off")
    rig.forget()
    await pick(rig, "dry")
    await rig.recompute()
    assert rig.sent(1) == []
    assert (rig.state(1), tile(rig).state) == ("dry", "dry")


async def test_a_dry_pick_counts_before_the_head_reports_it(hass: HomeAssistant) -> None:
    """The head's report comes after the room's own compute: that compute must
    not send the head the plan over the Dry just picked."""
    rig = await _setup(hass, head_cls=LateHead, enabled=(True, False))
    head = _head(hass, rig.heads[1])
    assert rig.state(1) == "off"
    head.late = True
    rig.forget()
    await pick(rig, "dry")
    await rig.recompute()
    assert rig.sent(1) == DRY and rig.state(1) == "off"  # asked, not yet reported
    assert rig.row(1)["dry"] == "held"
    # Shown as Dry; the action is what the head is doing now.
    assert (tile(rig).state, tile(rig).attributes["hvac_action"]) == ("dry", HVACAction.OFF)
    assert rig.coord._last_active.get(rig.heads[1]) is None  # no wet coil yet
    head.report()
    await hass.async_block_till_done()
    await rig.recompute()
    assert rig.sent(1) == DRY and rig.state(1) == "dry"
    assert rig.coord._dry_picks == {}
    assert tile(rig).attributes["hvac_action"] == HVACAction.DRYING


async def test_a_dry_pick_the_head_never_reports_ends_in_the_wait(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """No dry seen within DRY_PICK_WAIT: the plan has the head again, shown as
    Heat/Cool. The room's cool call waits out the pick, then runs."""
    rig = await _setup(hass, head_cls=LateHead)
    head = _head(hass, rig.heads[1])
    await rig.recompute()
    head.late = True
    rig.forget()
    await pick(rig, "dry")
    await _set_temp(hass, SENSORS[1], 76)  # the room would call cool
    await rig.recompute()
    assert rig.row(1)["demand"] == "neutral"  # paused while the pick counts
    assert rig.sent(1) == DRY and tile(rig).state == "dry"
    await rig.later(DRY_PICK_WAIT - 5)
    assert rig.sent(1) == DRY
    await rig.later(DRY_PICK_WAIT + 1)
    await rig.later(DRY_PICK_WAIT + REQUEST_REFRESH_DEFAULT_COOLDOWN + 2)
    assert rig.coord._dry_picks == {}
    assert (rig.row(1)["dry"], rig.row(1)["demand"]) == (None, "cool")
    assert rig.modes_sent(1)[0] == "dry" and "cool" in rig.modes_sent(1)[1:]  # the plan again
    assert tile(rig).state == "heat_cool"
    assert "was sent dry but its head didn't report it in time" in caplog.text


@pytest.mark.parametrize("why", ["coordinator_off", "standby_hold"])
async def test_dry_is_refused_with_the_coordinator_off_or_a_standby_hold(
    hass: HomeAssistant, why: str
) -> None:
    """Refused before anything is written: the room stays disabled, the head untouched."""
    hass.states.async_set(INHIBIT, "off")
    rig = await _setup(
        hass, enabled=(True, False),
        **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_OFF},
    )
    if why == "coordinator_off":
        await rig.switch("_coordinator_enable", False)
    else:
        hass.states.async_set(INHIBIT, "on")
        await hass.async_block_till_done()
        await rig.recompute()
    assert tile(rig).attributes["hvac_modes"] == THREE
    rig.forget()
    with pytest.raises(ServiceValidationError) as err:
        await pick(rig, "dry")
    assert err.value.translation_key == f"dry_{why}"
    assert rig.sent(1) == [] and other_writes(rig) == []
    assert hass.states.get(rig.eid("_secondary_enable")).state == "off"


async def test_set_temperature_carrying_dry_picks_dry_and_stores_the_target(
    hass: HomeAssistant,
) -> None:
    """The mode lands (through the same pick), the target is stored and paused."""
    rig = await _setup(hass, enabled=(True, False))
    rig.forget()
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": rig.eid(TILE[1]), "temperature": 66, "hvac_mode": "dry"}, blocking=True,
    )
    await hass.async_block_till_done()
    await rig.recompute()
    assert rig.sent(1) == DRY
    assert rig.coord.zones[1].target == 66
    assert (tile(rig).state, tile(rig).attributes["temperature"]) == ("dry", 66)
    assert (rig.row(1)["demand"], rig.row(1)["engage"]) == ("neutral", "satisfied")


# ---------------------------------------------------------------------------
# What it shows
# ---------------------------------------------------------------------------
async def test_a_remote_dry_shows_as_dry_and_drying(hass: HomeAssistant) -> None:
    """Honest state: a dry set on the head's own remote shows on the MXZ tile too."""
    rig = await _setup(hass)
    await rig.person(1, "dry")
    await rig.later(REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)
    assert (tile(rig).state, tile(rig).attributes["hvac_action"]) == ("dry", HVACAction.DRYING)
    assert rig.sent(1) == []
    await rig.person(1, "fan_only")  # the remote again: out of dry
    await rig.later(REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)
    assert tile(rig).state == "heat_cool"


async def test_a_parked_dry_shows_as_dry_and_idle_with_one_notice(hass: HomeAssistant) -> None:
    """Heat for another room: shown as Dry, idle, while the park is still on its
    way; once the head has left dry the tile shows Heat/Cool."""
    rig = await _setup(hass, head_cls=StubbornHead)  # it stays in dry: the park pending
    await rig.person(1, "dry")
    await _set_temp(hass, SENSORS[0], 65)
    await rig.recompute()
    assert rig.row(1)["dry"] == "parked"
    assert (tile(rig).state, tile(rig).attributes["hvac_action"]) == ("dry", HVACAction.IDLE)
    await rig.recompute()
    assert rig.paused() == [PAUSED.format("Rec room")]


async def test_a_landed_park_shows_heat_cool(hass: HomeAssistant) -> None:
    rig = await _setup(hass)
    await pick(rig, "dry")
    await _set_temp(hass, SENSORS[0], 65)
    await rig.recompute()
    await rig.later(REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)
    assert rig.state(1) == "fan_only"
    assert tile(rig).state == "heat_cool"
    assert rig.paused() == [PAUSED.format("Rec room")]


# ---------------------------------------------------------------------------
# Leaving Dry
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("how", ["picked", "remote"])
async def test_heat_cool_from_dry_takes_the_head_back_and_dries_the_coil(
    hass: HomeAssistant, how: str
) -> None:
    """Heat/Cool: the plan's idle (dry the coil, then off) and the room's goal again."""
    rig = await _setup(hass, idle=IDLE_ACTION_OFF_AFTER_DRY)
    if how == "picked":
        await pick(rig, "dry")
    else:
        await rig.person(1, "dry")
    await rig.recompute()
    assert tile(rig).state == "dry"
    rig.forget()
    await pick(rig, "heat_cool")
    await rig.recompute()
    assert rig.state(1) == "fan_only"  # the coil-dry run, owed from the dry
    assert tile(rig).state == "heat_cool"
    assert rig.coord._dry_released == set()  # the head reported out of dry
    age_coil_dry(rig.coord, rig.heads[1], rig.coord.coil_dry_seconds + 5)
    await rig.recompute()
    assert rig.state(1) == "off"
    await _set_temp(hass, SENSORS[1], 76)  # the room's goal is back
    await rig.recompute()
    assert (rig.row(1)["demand"], rig.state(1)) == ("cool", "cool")
    assert "dry" not in rig.modes_sent(1)


async def test_off_from_dry_disables_the_room_and_owes_the_coil_dry_run(
    hass: HomeAssistant,
) -> None:
    """Off: the room disabled, its head off at once (a disabled room never idles
    in fan_only, as after cooling); the coil-dry run is owed from the dry, so
    the room switched back on inside that time dries the coil first."""
    rig = await _setup(hass, idle=IDLE_ACTION_OFF_AFTER_DRY)
    await pick(rig, "dry")
    await rig.recompute()
    rig.forget()
    await pick(rig, "off")
    await rig.recompute()
    assert rig.modes_sent(1) == ["off"] and rig.state(1) == "off"
    assert (tile(rig).state, tile(rig).attributes["hvac_action"]) == ("off", HVACAction.OFF)
    assert hass.states.get(rig.eid("_secondary_enable")).state == "off"
    assert rig.coord._last_active[rig.heads[1]][0] == "cool"
    await pick(rig, "heat_cool")
    await rig.recompute()
    assert rig.state(1) == "fan_only"


async def test_heat_cool_outside_dry_releases_nothing(hass: HomeAssistant) -> None:
    """Heat/Cool picked while not in dry is just the switch: a later remote dry is held."""
    rig = await _setup(hass)
    await pick(rig, "heat_cool")
    assert rig.coord._dry_released == set()
    await rig.person(1, "dry")
    await rig.recompute()
    assert (rig.row(1)["dry"], rig.state(1), tile(rig).state) == ("held", "dry", "dry")


async def test_turning_the_tile_off_from_dry_leaves_dry(hass: HomeAssistant) -> None:
    rig = await _setup(hass)
    await pick(rig, "dry")
    await hass.services.async_call(
        "climate", "turn_off", {"entity_id": rig.eid(TILE[1])}, blocking=True
    )
    await rig.recompute()
    assert (rig.state(1), tile(rig).state) == ("off", "off")


# ---------------------------------------------------------------------------
# The option turned off, or the head dropping dry, while in Dry
# ---------------------------------------------------------------------------
async def test_the_head_dropping_dry_ends_dry_with_one_notice(hass: HomeAssistant) -> None:
    rig = await _setup(hass)
    await pick(rig, "dry")
    await rig.recompute()
    head = _head(hass, rig.heads[1])
    head._attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY]
    head.async_write_ha_state()
    await hass.async_block_till_done()
    await rig.later(BAND_DRIFT_DELAY + 1)
    assert rig.state(1) == "fan_only"  # the plan's idle, inside the band wait
    assert tile(rig).attributes["hvac_modes"] == TWO
    assert tile(rig).state == "heat_cool"
    assert rig.notices() == [ENDED.format("its head no longer offers Dry")]
    await rig.later(BAND_DRIFT_DELAY + 30)
    assert rig.notices() == [ENDED.format("its head no longer offers Dry")]


async def test_allow_dry_turned_off_in_dry_ends_dry_with_one_notice(hass: HomeAssistant) -> None:
    """Saved on the room page: the reload has the head back on the plan."""
    rig = await _setup(hass)
    await pick(rig, "dry")
    await rig.recompute()
    result = await open_options_page(hass, rig.entry, "room_2")
    await submit(
        hass.config_entries.options, result, {**prefilled(result), "allow_dry": False}
    )
    await hass.async_block_till_done()
    assert ZONE_ALLOW_DRY not in rig.entry.data[CONF_ZONES][1]
    await rig.later(BAND_DRIFT_DELAY + 1)
    assert rig.state(1) == "fan_only"
    assert tile(rig).attributes["hvac_modes"] == TWO
    assert tile(rig).state == "heat_cool"
    assert rig.notices() == [ENDED.format("Allow Dry mode was turned off")]


async def test_allow_dry_turned_off_outside_dry_tells_nothing(hass: HomeAssistant) -> None:
    rig = await _setup(hass)
    result = await open_options_page(hass, rig.entry, "room_2")
    await submit(
        hass.config_entries.options, result, {**prefilled(result), "allow_dry": False}
    )
    await hass.async_block_till_done()
    assert tile(rig).attributes["hvac_modes"] == TWO
    assert rig.notices() == []


# ---------------------------------------------------------------------------
# Restart, and a stale room sensor
# ---------------------------------------------------------------------------
async def test_restart_in_a_picked_dry_is_held_shown_dry_and_writes_nothing(
    hass: HomeAssistant,
) -> None:
    rig = await _setup(hass)
    await pick(rig, "dry")
    await rig.person_fan(1, "low")
    await rig.recompute()
    await _shutdown(hass, rig.entry)
    rig.forget()
    await _startup(hass, rig.entry)
    await rig.recompute()
    await rig.later(BAND_DRIFT_DELAY + 30)
    assert rig.sent(1) == []
    assert rig.row(1)["dry"] == "held"
    assert (tile(rig).state, tile(rig).attributes["hvac_action"]) == ("dry", HVACAction.DRYING)
    assert tile(rig).attributes["hvac_modes"] == THREE
    assert hass.states.get(rig.heads[1]).attributes["fan_mode"] == "low"
    assert hass.states.get(rig.eid("_secondary_fan_auto")).state == "off"
    assert rig.notices() == []


async def test_a_stale_room_sensor_keeps_dry_held(hass: HomeAssistant, freezer) -> None:
    """Drew, Q1: Dry doesn't use the room sensor, so a stale one doesn't park it."""
    rig = await _setup(
        hass,
        zones=({}, {ZONE_ALLOW_DRY: True, ZONE_MAX_AGE: 10, ZONE_EVIDENCE_BASIS: EVIDENCE_HA_WRITE}),
    )
    await pick(rig, "dry")
    await rig.recompute()
    rig.forget()
    await _tick(hass, freezer, timedelta(minutes=11))
    await _set_temp(hass, SENSORS[0], 70)  # the other room keeps reporting
    await rig.recompute()
    assert rig.row(1)["sensor_health"] == "stale"
    assert rig.row(1)["dry"] == "held"
    assert rig.state(1) == "dry" and rig.sent(1) == []
    assert tile(rig).state == "dry"
    # The control: the same stale sensor out of dry parks the room's call.
    await pick(rig, "heat_cool")
    await rig.recompute()
    assert (rig.row(1)["dry"], rig.row(1)["demand"]) == (None, "neutral")
    assert rig.state(1) == "fan_only"


async def test_a_cloud_dry_pick_waits_out_the_confirm_wait(hass: HomeAssistant) -> None:
    """A polled cloud head: sent dry once, held with no write until its poll
    shows it, for the room's confirm wait (90 s, longer than DRY_PICK_WAIT)."""
    entry, _a, b, _notices = await _cloud(hass)
    coord = entry.runtime_data
    tile_id = _eid(hass, entry, "_secondary_thermostat")
    b.delayed = True  # its report waits for the next poll
    calls = record_calls(hass)
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": tile_id, "hvac_mode": "dry"}, blocking=True
    )
    await hass.async_block_till_done()
    assert b.device["mode"] == "dry" and hass.states.get(b.entity_id).state != "dry"
    assert coord._cloud[b.entity_id].confirm_wait > DRY_PICK_WAIT
    await coord.async_refresh()
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=DRY_PICK_WAIT + 5))
    await hass.async_block_till_done()
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert head_writes(calls, b.entity_id) == DRY
    assert hass.states.get(tile_id).state == "dry"
    b.poll()
    await hass.async_block_till_done()
    await coord.async_refresh()
    await hass.async_block_till_done()
    row = hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][1]
    assert (row["dry"], row["dry_vote"]) == ("held", "cool")
    assert coord._dry_picks == {}
    assert head_writes(calls, b.entity_id) == DRY


# ---------------------------------------------------------------------------
# The thermostat's own copy
# ---------------------------------------------------------------------------
def test_dry_refusals_are_translated() -> None:
    root = Path(__file__).parent.parent / "custom_components" / "mxz_coordinator"
    for name in ("strings.json", "translations/en.json"):
        exceptions = json.loads((root / name).read_text())["exceptions"]
        for key in ("dry_not_offered", "dry_coordinator_off", "dry_standby_hold"):
            assert exceptions[key]["message"].startswith("{zone}: "), (name, key)

