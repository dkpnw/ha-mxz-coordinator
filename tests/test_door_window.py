"""Door/window pause, per room (opt-in, off by default).

Idea from @mikepell007's MIT blueprint mxz_coordinator_blueprint (60 s open,
120 s close). Decided by Drew, Nov 3 2026, each pinned by a test here:

* Off by default; per room, one sensor or one group; open delay 60 s and
  close delay 120 s, both adjustable.
* A paused room's head goes straight to off (no coil-dry run), the room never
  votes while paused, and neither a pause nor a resume touches the
  mode-change hold.
* The room's own MXZ mode is never rewritten; it shows paused, why, and when
  it resumes. A room a person turns off during a pause stays off.
* A person's change on the paused head takes the room over until the window
  closes; so does the room's Window Override switch, which clears itself on
  close so the next opening pauses again.
* A resume runs the head in the shared mode as it is now, with its fan and
  vanes as they were (a fan or vane picked while paused is kept).
* A pause outlives a reload or restart; no reading never starts one.
* Notices are optional, off by default, and rate limited.

Every room, head and sensor is invented; nothing here is a hardware result.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_RESTING_MODE_BIAS,
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF_AFTER_DRY,
    MODE_COOL,
    MODE_HEAT,
    OPENING_NOTIFY_SECONDS,
    ZONE_ALLOW_DRY,
    ZONE_CLIMATE,
    ZONE_FLOOR_LOW,
    ZONE_NAME,
    ZONE_OPENING_CLOSE_DELAY,
    ZONE_OPENING_ENTITY,
    ZONE_OPENING_NOTIFY,
    ZONE_OPENING_OPEN_DELAY,
    ZONE_OPENING_PAUSE,
    ZONE_SENSOR,
    ZONE_VANE_VERTICAL,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from tests.clocks import pass_time
from tests.cloud_heads import record as record_calls
from tests.cloud_heads import register
from tests.cloud_heads import writes as head_writes
from tests.dwell import expire_dwell
from tests.flow_pages import (
    ROOM_SECTION_FIELDS,
    default,
    fields,
    nest,
    open_options_page,
    prefilled,
    sections,
    submit,
    suggested,
)
from tests.restart import restart, stopped
from tests.test_drive import MockHead, _eid, _set_target, _set_temp
from tests.test_dry_mode import DryHead
from tests.test_startup_continuity import _head

WINDOW = "binary_sensor.study_window"
VANE = "select.study_vane"
VANE_OPTIONS = ["auto", "1", "2", "3", "swing"]
SENSORS = ("sensor.study_temp", "sensor.den_temp")
NAMES = ("Study", "Den")
NOTIFY = "door_test"
OPEN = 60
CLOSE = 120
PAUSE_NOTICE = "Study paused: Study window open for 1 min. Heat/cool off in this room."
RESUME_NOTICE = "Study resumed: closed for 2 min."
OVERRIDE_NOTICE = "Study: window open, manual override."
WINDOW_ON = {ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_PAUSE: True}


class Rig:
    """One entry over two invented heads (Study first, then Den) and what it sent."""

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry, heads, freezer, notices) -> None:
        self.hass = hass
        self.entry = entry
        self.heads = heads
        self.freezer = freezer
        self._notices = notices
        self.calls = record_calls(hass)

    @property
    def coord(self) -> MXZCoordinator:
        return self.entry.runtime_data

    def eid(self, suffix: str) -> str:
        return _eid(self.hass, self.entry, suffix)

    def plan(self):
        return self.hass.states.get(self.eid("_plan"))

    def thermostat(self, slug: str = "primary"):
        return self.hass.states.get(self.eid(f"_{slug}_thermostat"))

    def head(self, index: int = 0) -> str:
        return self.hass.states.get(self.heads[index]).state

    def sent(self, index: int = 0) -> list[tuple[str, dict[str, Any]]]:
        return head_writes(self.calls, self.heads[index])

    def modes_sent(self, index: int = 0) -> list[str]:
        return [p["hvac_mode"] for _s, p in self.sent(index) if "hvac_mode" in p]

    def notices(self) -> list[str]:
        return [call.data["message"] for call in self._notices]

    def forget(self) -> None:
        self.calls.clear()

    async def recompute(self) -> None:
        await self.coord.async_refresh()
        await self.hass.async_block_till_done()

    async def later(self, seconds: float) -> None:
        """Time passes; any wakeup that falls due fires on its own."""
        self.freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(self.hass, dt_util.utcnow())
        await self.hass.async_block_till_done()
        await self.hass.async_block_till_done()

    async def window(self, state: str) -> None:
        self.hass.states.async_set(WINDOW, state, {"friendly_name": "Study window"})
        await self.hass.async_block_till_done()

    async def open_and_pause(self) -> None:
        await self.window("on")
        await self.later(OPEN + 1)

    async def person(self, mode: str, index: int = 0) -> None:
        """A person sets the head's mode on its remote (not MXZ's)."""
        head = _head(self.hass, self.heads[index])
        head._attr_hvac_mode = mode
        head.async_write_ha_state()
        await self.hass.async_block_till_done()

    async def person_fan(self, fan: str, index: int = 0) -> None:
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

    def opening(self, slug: str = "primary") -> dict[str, Any]:
        attrs = self.thermostat(slug).attributes
        return {
            key: attrs.get(key)
            for key in ("opening_state", "paused_reason", "window_override", "resumes_at")
        }


async def _setup(
    hass: HomeAssistant,
    freezer,
    *,
    study: dict[str, Any] | None = None,
    temps: tuple[float, float] = (76, 70),
    hysteresis: int = 0,
    head_cls: type[MockHead] = MockHead,
    window: str = "off",
    **options: Any,
) -> Rig:
    """Study (window, highest priority) and Den, both enabled, target 70."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [head_cls("a"), head_cls("b")]
    await register(hass, {"esphome": heads})
    hass.states.async_set(WINDOW, window, {"friendly_name": "Study window"})
    hass.states.async_set(VANE, "auto", {"options": VANE_OPTIONS})
    for sensor, value in zip(SENSORS, temps):
        await _set_temp(hass, sensor, value)
    notices = async_mock_service(hass, "notify", NOTIFY)
    zones = [
        {ZONE_NAME: NAMES[0], ZONE_CLIMATE: heads[0].entity_id, ZONE_SENSOR: SENSORS[0],
         **(WINDOW_ON if study is None else study)},
        {ZONE_NAME: NAMES[1], ZONE_CLIMATE: heads[1].entity_id, ZONE_SENSOR: SENSORS[1]},
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_ZONES: zones,
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_MODE_HYSTERESIS: hysteresis,
            CONF_NOTIFY_SERVICE: f"notify.{NOTIFY}",
            **options,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for slug in ("primary", "secondary"):
        await _set_target(hass, _eid(hass, entry, f"_{slug}_target"), 70)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, f"_{slug}_enable")}, blocking=True,
        )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_coordinator_enable")}, blocking=True,
    )
    await hass.async_block_till_done()
    rig = Rig(hass, entry, [h.entity_id for h in heads], freezer, notices)
    await rig.recompute()
    rig.forget()
    return rig


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "study",
    [
        {},  # never set
        {ZONE_OPENING_ENTITY: WINDOW},  # a sensor kept, the pause off
    ],
    ids=["no_sensor", "pause_off"],
)
async def test_off_by_default_nothing_pauses_and_nothing_new_shows(
    hass: HomeAssistant, freezer, study
) -> None:
    rig = await _setup(hass, freezer, study=study)
    assert rig.head() == MODE_COOL
    await rig.open_and_pause()
    await rig.later(3600)
    await rig.recompute()
    assert rig.head() == MODE_COOL  # still cooling
    assert rig.modes_sent() == []
    # No new entity, attribute or plan key: the room is exactly as before.
    with pytest.raises(AssertionError):
        rig.eid("_primary_window_override")
    with pytest.raises(AssertionError):
        rig.eid("_primary_paused_open")
    assert "opening_state" not in rig.thermostat().attributes
    assert "paused_rooms" not in rig.plan().attributes
    assert all("opening_state" not in row for row in rig.plan().attributes["zones"])


# ---------------------------------------------------------------------------
# The pause itself
# ---------------------------------------------------------------------------
async def test_open_for_the_open_delay_pauses_straight_to_off(hass: HomeAssistant, freezer) -> None:
    """off_after_dry would run a just-cooling head in fan_only first; a pause doesn't."""
    rig = await _setup(hass, freezer, **{CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY})
    assert rig.head() == MODE_COOL
    await rig.window("on")
    await rig.later(OPEN - 1)
    assert rig.head() == MODE_COOL  # a short open is nothing yet
    assert rig.opening()["opening_state"] == "opening"
    await rig.later(2)  # the open delay's wakeup fires on its own
    assert rig.head() == "off"
    assert rig.modes_sent() == ["off"]  # straight to off, no fan_only first
    state = rig.thermostat()
    assert state.state == HVACMode.HEAT_COOL  # the room's own mode is untouched
    assert state.attributes["hvac_action"] == HVACAction.OFF
    assert rig.opening() == {
        "opening_state": "paused", "paused_reason": "door/window open",
        "window_override": False, "resumes_at": None,
    }
    assert state.attributes["paused_since"] is not None
    assert rig.hass.states.get(rig.eid("_primary_enable")).state == "on"
    assert rig.hass.states.get(rig.eid("_primary_paused_open")).state == "on"
    assert rig.plan().attributes["paused_rooms"] == ["Study"]
    assert rig.plan().attributes["zones"][0]["opening_state"] == "paused"


async def test_a_short_open_never_pauses(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer)
    await rig.window("on")
    await rig.later(OPEN - 5)
    await rig.window("off")
    await rig.later(OPEN * 3)
    await rig.recompute()
    assert rig.head() == MODE_COOL
    assert rig.modes_sent() == []
    assert rig.opening()["opening_state"] == "closed"
    assert rig.notices() == []


@pytest.mark.parametrize("reading", ["unavailable", "unknown"])
async def test_no_reading_never_starts_a_pause_and_never_ends_one(
    hass: HomeAssistant, freezer, reading
) -> None:
    rig = await _setup(hass, freezer)
    await rig.window(reading)
    await rig.later(3600)
    await rig.recompute()
    assert rig.head() == MODE_COOL
    assert rig.opening()["opening_state"] == "closed"
    # Paused, then the sensor drops out: still paused, however long.
    await rig.open_and_pause()
    assert rig.head() == "off"
    await rig.window(reading)
    await rig.later(3600)
    await rig.recompute()
    assert rig.head() == "off"
    assert rig.opening()["opening_state"] == "paused"


async def test_delays_are_adjustable(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(
        hass, freezer,
        study={**WINDOW_ON, ZONE_OPENING_OPEN_DELAY: 5, ZONE_OPENING_CLOSE_DELAY: 30},
    )
    await rig.window("on")
    await rig.later(6)
    assert rig.head() == "off"
    await rig.window("off")
    await rig.later(29)
    assert rig.head() == "off"
    assert rig.opening()["opening_state"] == "closing"
    await rig.later(2)
    assert rig.head() == MODE_COOL


async def test_close_delay_shows_the_resume_time_and_reopening_restarts_it(
    hass: HomeAssistant, freezer
) -> None:
    rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    await rig.window("off")
    closed_at = dt_util.utcnow()
    await rig.recompute()
    view = rig.opening()
    assert view["opening_state"] == "closing"
    assert view["paused_reason"] == "door/window open"
    assert view["resumes_at"] == (closed_at + timedelta(seconds=CLOSE)).isoformat(timespec="seconds")
    await rig.later(CLOSE - 20)
    await rig.window("on")  # opened again inside the close delay
    await rig.later(1)
    assert rig.opening()["opening_state"] == "paused"
    await rig.window("off")
    await rig.later(CLOSE - 1)
    assert rig.head() == "off"  # the close delay started over
    await rig.later(2)
    assert rig.head() == MODE_COOL
    assert rig.opening()["opening_state"] == "closed"
    assert rig.thermostat().attributes["hvac_action"] == HVACAction.COOLING


# ---------------------------------------------------------------------------
# Votes and the mode-change hold
# ---------------------------------------------------------------------------
async def test_a_paused_room_never_votes(hass: HomeAssistant, freezer) -> None:
    """Study (first in order) calls cool, Den calls heat: Study wins until it pauses."""
    rig = await _setup(hass, freezer, temps=(76, 64))
    assert rig.plan().state == MODE_COOL
    await rig.open_and_pause()
    plan = rig.plan()
    assert plan.state == MODE_HEAT  # Study's cool no longer counts
    assert plan.attributes["zones"][0]["demand"] == "neutral"
    assert plan.attributes["zones"][0]["engage"] == "satisfied"
    assert plan.attributes["standoff"] is False
    assert (rig.head(0), rig.head(1)) == ("off", MODE_HEAT)


async def test_a_pause_and_a_resume_never_touch_the_hold(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer, hysteresis=600)
    coord = rig.coord
    stamp, at = coord._last_mode_change_ts, coord._last_mode_change_at
    await rig.open_and_pause()
    assert rig.head() == "off"
    assert (coord._last_mode_change_ts, coord._last_mode_change_at) == (stamp, at)
    await rig.window("off")
    await rig.later(CLOSE + 1)
    assert rig.head() == MODE_COOL
    assert (coord._last_mode_change_ts, coord._last_mode_change_at) == (stamp, at)


async def test_a_paused_room_cannot_flip_the_mode_inside_the_hold(
    hass: HomeAssistant, freezer
) -> None:
    """Den calls heat; Study's cool held the mode. The pause drops Study's vote,
    but the hold since the last change still holds the flip back."""
    rig = await _setup(hass, freezer, temps=(76, 64), hysteresis=600)
    coord = rig.coord
    coord._stamp_mode_change()  # a change just now: the hold runs
    stamp = coord._last_mode_change_ts
    await rig.open_and_pause()
    plan = rig.plan()
    assert plan.state == MODE_COOL
    assert plan.attributes["mode_change_allowed"] is False
    assert coord._last_mode_change_ts == stamp
    expire_dwell(coord)
    await rig.recompute()
    assert rig.plan().state == MODE_HEAT  # the flip waited for the hold, nothing else


async def test_a_resume_waits_out_the_hold_like_any_room(hass: HomeAssistant, freezer) -> None:
    """Paused, Den's heat took the mode. On resume Study calls cool again, and
    first in order it would win, but only once the hold since heat allows it."""
    rig = await _setup(hass, freezer, temps=(76, 64), hysteresis=600)
    expire_dwell(rig.coord)
    await rig.open_and_pause()
    assert rig.plan().state == MODE_HEAT  # a real change: the hold starts now
    stamp = rig.coord._last_mode_change_ts
    await rig.window("off")
    await rig.later(CLOSE + 1)
    plan = rig.plan()
    assert rig.opening()["opening_state"] == "closed"
    assert plan.attributes["zones"][0]["demand"] == MODE_COOL  # voting again
    assert plan.state == MODE_HEAT  # held, as any hold loser is
    assert plan.attributes["mode_change_allowed"] is False
    assert rig.head(0) == "fan_only"  # parked by the plan, not by the pause
    assert rig.coord._last_mode_change_ts == stamp
    expire_dwell(rig.coord)
    await rig.recompute()
    assert (rig.plan().state, rig.head(0)) == (MODE_COOL, MODE_COOL)


async def test_every_room_paused_rests_by_the_resting_rule(hass: HomeAssistant, freezer) -> None:
    """Both rooms share one window and call cool; Resting mode is heat."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    rig = await _setup(hass, freezer, temps=(76, 76), **{CONF_RESTING_MODE_BIAS: MODE_HEAT})
    assert rig.plan().state == MODE_COOL
    zones = [dict(z) for z in rig.entry.data[CONF_ZONES]]
    zones[1].update(WINDOW_ON)
    hass.config_entries.async_update_entry(rig.entry, data={**rig.entry.data, CONF_ZONES: zones})
    await hass.config_entries.async_reload(rig.entry.entry_id)
    await hass.async_block_till_done()
    await rig.recompute()
    await rig.open_and_pause()
    assert rig.plan().attributes["paused_rooms"] == ["Study", "Den"]
    assert rig.plan().state == MODE_HEAT  # nothing calls: Resting mode
    assert (rig.head(0), rig.head(1)) == ("off", "off")


# ---------------------------------------------------------------------------
# The room's own mode
# ---------------------------------------------------------------------------
async def test_the_rooms_mode_is_never_rewritten_and_off_stays_off(
    hass: HomeAssistant, freezer
) -> None:
    rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    assert rig.thermostat().state == HVACMode.HEAT_COOL
    # Changing the room's MXZ mode is not an override: still paused.
    await hass.services.async_call(
        "climate", "set_hvac_mode",
        {"entity_id": rig.eid("_primary_thermostat"), "hvac_mode": HVACMode.OFF}, blocking=True,
    )
    await hass.async_block_till_done()
    assert rig.opening()["window_override"] is False
    assert rig.coord.openings.state_of("primary") == "paused"
    await rig.window("off")
    await rig.later(CLOSE + 1)
    await rig.recompute()
    assert rig.thermostat().state == HVACMode.OFF  # the person's Off stands
    assert rig.head() == "off"


async def test_a_disabled_rooms_freeze_floor_still_runs_with_the_window_open(
    hass: HomeAssistant, freezer
) -> None:
    """The pause is for an enabled room; a floor's safety run is never paused."""
    rig = await _setup(hass, freezer, study={**WINDOW_ON, ZONE_FLOOR_LOW: 45})
    await rig.switch("_primary_enable", False)
    await _set_temp(hass, SENSORS[0], 40)
    await rig.open_and_pause()
    await rig.recompute()
    assert rig.head() == MODE_HEAT  # the floor heats it, window or not
    assert rig.coord.openings.state_of("primary") == "paused"  # the window's own state
    assert rig.opening()["paused_reason"] is None  # not off for the window
    assert rig.hass.states.get(rig.eid("_primary_paused_open")).state == "off"


# ---------------------------------------------------------------------------
# A person's change on the head
# ---------------------------------------------------------------------------
async def test_a_manual_change_holds_until_the_window_closes(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    rig.forget()
    await rig.person(MODE_COOL)  # the head's own remote
    await rig.later(30)
    await rig.recompute()
    assert rig.head() == MODE_COOL  # left running: not turned back off
    assert "off" not in rig.modes_sent()
    assert rig.opening() == {
        "opening_state": "overridden", "paused_reason": "window open, manual override",
        "window_override": True, "resumes_at": None,
    }
    assert rig.hass.states.get(rig.eid("_primary_window_override")).state == "on"
    assert rig.hass.states.get(rig.eid("_primary_paused_open")).state == "off"
    assert rig.plan().attributes["zones"][0]["demand"] == MODE_COOL  # it votes again
    await rig.later(3600)  # open all along: no re-pause
    await rig.recompute()
    assert rig.head() == MODE_COOL
    # Closed: the override ends at once; the next opening pauses again.
    await rig.window("off")
    await rig.recompute()
    assert rig.opening()["opening_state"] == "closed"
    assert rig.hass.states.get(rig.eid("_primary_window_override")).state == "off"
    await rig.open_and_pause()
    assert rig.head() == "off"


async def test_a_change_through_the_head_entity_is_a_takeover_too(
    hass: HomeAssistant, freezer
) -> None:
    rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": rig.heads[0], "hvac_mode": MODE_HEAT},
        blocking=True,
    )
    await hass.async_block_till_done()
    await rig.recompute()
    assert rig.opening()["opening_state"] == "overridden"


async def test_mxz_own_off_is_not_a_takeover(hass: HomeAssistant, freezer) -> None:
    """The pause's own off, and the head reporting it, leave the room paused."""
    rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    await rig.later(600)
    await rig.recompute()
    assert rig.opening()["opening_state"] == "paused"
    assert rig.modes_sent() == ["off"]


# ---------------------------------------------------------------------------
# Window Override
# ---------------------------------------------------------------------------
async def test_window_override_runs_the_room_and_clears_itself_on_close(
    hass: HomeAssistant, freezer
) -> None:
    rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    assert rig.head() == "off"
    await rig.switch("_primary_window_override", True)  # one tap
    assert rig.head() == MODE_COOL  # back on at once
    assert rig.opening()["opening_state"] == "overridden"
    assert rig.hass.states.get(rig.eid("_primary_window_override")).state == "on"
    await rig.later(3600)
    await rig.recompute()
    assert rig.head() == MODE_COOL
    await rig.window("off")
    await rig.recompute()
    assert rig.hass.states.get(rig.eid("_primary_window_override")).state == "off"
    await rig.open_and_pause()  # the next opening pauses as usual
    assert rig.head() == "off"


async def test_window_override_off_pauses_again(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    await rig.switch("_primary_window_override", True)
    assert rig.head() == MODE_COOL
    await rig.switch("_primary_window_override", False)
    assert rig.head() == "off"
    assert rig.opening()["opening_state"] == "paused"


async def test_window_override_does_nothing_while_closed(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer)
    await rig.switch("_primary_window_override", True)
    assert rig.hass.states.get(rig.eid("_primary_window_override")).state == "off"
    await rig.open_and_pause()
    assert rig.head() == "off"


# ---------------------------------------------------------------------------
# What a resume restores
# ---------------------------------------------------------------------------
async def test_resume_runs_in_the_shared_mode_as_it_is_now(hass: HomeAssistant, freezer) -> None:
    """Cooling before the pause; heat took the mode meanwhile; Study now wants heat."""
    rig = await _setup(hass, freezer, temps=(76, 70))
    await rig.open_and_pause()
    await _set_temp(hass, SENSORS[0], 64)
    await _set_temp(hass, SENSORS[1], 64)
    await rig.recompute()
    assert (rig.plan().state, rig.head(0), rig.head(1)) == (MODE_HEAT, "off", MODE_HEAT)
    await rig.window("off")
    await rig.later(CLOSE + 1)
    assert rig.head(0) == MODE_HEAT


async def test_a_satisfied_room_resumes_idle(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer, temps=(70, 70))
    assert rig.head() == "fan_only"
    await rig.open_and_pause()
    assert rig.head() == "off"
    await rig.window("off")
    await rig.later(CLOSE + 1)
    assert rig.head() == "fan_only"  # the plan's idle, not forced on


async def test_a_held_fan_and_a_fan_picked_while_paused_are_kept(
    hass: HomeAssistant, freezer
) -> None:
    rig = await _setup(hass, freezer, **{CONF_FAN_BOOST_ENABLE: True})
    await rig.person_fan("quiet")  # a manual hold before the window opens
    await rig.recompute()
    assert rig.hass.states.get(rig.eid("_primary_fan_auto")).state == "off"
    await rig.open_and_pause()
    assert rig.head() == "off"
    assert rig.modes_sent() == ["off"]
    await rig.person_fan("high")  # picked on the off head
    await rig.recompute()
    assert rig.head() == "off"  # a fan pick is not a takeover
    assert rig.opening()["opening_state"] == "paused"
    assert rig.hass.states.get(rig.eid("_primary_fan_auto")).state == "off"
    rig.forget()
    await rig.window("off")
    await rig.later(CLOSE + 1)
    assert rig.head() == MODE_COOL
    attrs = rig.hass.states.get(rig.heads[0]).attributes
    assert attrs["fan_mode"] == "high"
    assert not [p for s, p in rig.sent() if s == "set_fan_mode"]  # no fan write at all
    assert rig.hass.states.get(rig.eid("_primary_fan_auto")).state == "off"


async def test_vanes_picked_while_paused_wait_for_the_resume(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer, study={**WINDOW_ON, ZONE_VANE_VERTICAL: VANE})
    selects = async_mock_service(hass, "select", "select_option")
    await rig.open_and_pause()
    rig.forget()
    await hass.services.async_call(
        "climate", "set_swing_mode",
        {"entity_id": rig.eid("_primary_thermostat"), "swing_mode": "swing"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert selects == []  # no select write ...
    assert rig.modes_sent() == []  # ... and no vane kick running the head
    await rig.window("off")
    await rig.later(CLOSE + 1)
    assert rig.head() == MODE_COOL
    assert [c.data for c in selects] == [{"entity_id": VANE, "option": "swing"}]


async def test_vanes_moved_while_paused_are_put_back(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer, study={**WINDOW_ON, ZONE_VANE_VERTICAL: VANE})
    hass.states.async_set(VANE, "2", {"options": VANE_OPTIONS})
    selects = async_mock_service(hass, "select", "select_option")
    await rig.open_and_pause()
    hass.states.async_set(VANE, "auto", {"options": VANE_OPTIONS})  # the head reset it
    await rig.window("off")
    await rig.later(CLOSE + 1)
    assert [c.data for c in selects] == [{"entity_id": VANE, "option": "2"}]


async def test_a_dry_found_by_the_pause_is_put_back(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(
        hass, freezer, temps=(70, 70), head_cls=DryHead, study={**WINDOW_ON, ZONE_ALLOW_DRY: True},
    )
    await rig.person("dry")
    await rig.recompute()
    assert rig.thermostat().state == HVACMode.DRY
    await rig.open_and_pause()
    assert rig.head() == "off"
    assert rig.plan().attributes["zones"][0]["dry_vote"] is None  # no Dry vote while paused
    rig.forget()
    await rig.window("off")
    await rig.later(CLOSE + 1)
    assert rig.modes_sent() == ["dry"]
    assert rig.head() == "dry"
    assert rig.thermostat().state == HVACMode.DRY


# ---------------------------------------------------------------------------
# Restart continuity
# ---------------------------------------------------------------------------
async def test_a_pause_outlives_a_restart(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer, study={**WINDOW_ON, ZONE_OPENING_NOTIFY: True})
    await rig.open_and_pause()
    rig.forget()
    await restart(rig.hass, rig.entry)
    await rig.recompute()
    assert rig.head() == "off"
    assert rig.sent() == []  # already off: nothing re-sent
    assert rig.opening()["opening_state"] == "paused"
    assert rig.hass.states.get(rig.eid("_primary_paused_open")).state == "on"
    await rig.later(3600)
    assert rig.head() == "off"
    # The resume after the restart still sends the notice its pause earned.
    await rig.window("off")
    await rig.later(CLOSE + 1)
    assert rig.head() == MODE_COOL
    assert rig.notices() == [PAUSE_NOTICE, RESUME_NOTICE]


async def test_a_pause_is_stored_as_the_record_a_start_reads(
    hass: HomeAssistant, freezer, hass_storage
) -> None:
    """The store holds each room's state, override, pause start, snapshot and
    notice flag, by head, and nothing else."""
    rig = await _setup(hass, freezer, study={**WINDOW_ON, ZONE_OPENING_NOTIFY: True})
    before = dt_util.utcnow().timestamp()
    await rig.open_and_pause()
    after = dt_util.utcnow().timestamp()
    async with stopped(rig.hass, rig.entry):
        stored = hass_storage[f"{DOMAIN}.{rig.entry.entry_id}.openings"]
        assert stored["version"] == 1
        assert list(stored["data"]) == ["rooms"]
        assert list(stored["data"]["rooms"]) == [rig.heads[0]]
        room = stored["data"]["rooms"][rig.heads[0]]
        assert set(room) == {"state", "override", "paused_at", "snapshot", "notified"}
        assert (room["state"], room["override"], room["notified"]) == ("paused", None, True)
        assert before <= room["paused_at"] <= after
        assert set(room["snapshot"]) == {"hvac_mode", "fan_mode", "fan_held", "vanes", "dry"}
        assert (room["snapshot"]["hvac_mode"], room["snapshot"]["dry"]) == (MODE_COOL, False)


async def test_a_stored_pause_written_by_hand_resumes_as_written(
    hass: HomeAssistant, freezer, hass_storage
) -> None:
    """A record in the stored shape, written while Home Assistant is down (as an
    earlier release wrote it), is the room's pause from the first plan on."""
    rig = await _setup(hass, freezer, study={**WINDOW_ON, ZONE_OPENING_NOTIFY: True})
    assert rig.head() == MODE_COOL
    paused_at = float(int(dt_util.utcnow().timestamp()) - 600)
    key = f"{DOMAIN}.{rig.entry.entry_id}.openings"
    async with stopped(rig.hass, rig.entry):
        await rig.window("on")
        hass_storage[key] = {
            "version": 1, "minor_version": 1, "key": key,
            "data": {"rooms": {rig.heads[0]: {
                "state": "paused", "override": None, "paused_at": paused_at,
                "snapshot": {
                    "hvac_mode": MODE_COOL, "fan_mode": "auto", "fan_held": False,
                    "vanes": {}, "dry": False,
                },
                "notified": True,
            }}},
        }
    await rig.recompute()
    assert rig.opening()["opening_state"] == "paused"
    assert rig.head() == "off"
    assert rig.thermostat().attributes["paused_since"] == (
        dt_util.utc_from_timestamp(paused_at).isoformat(timespec="seconds")
    )
    await rig.window("off")
    await rig.later(CLOSE + 1)
    assert rig.head() == MODE_COOL
    assert rig.notices() == [RESUME_NOTICE]  # the stored flag says its pause notice went out


async def test_closed_during_a_restart_waits_the_full_close_delay(
    hass: HomeAssistant, freezer
) -> None:
    rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    await rig.window("off")
    await rig.later(CLOSE - 10)  # nearly done when HA stops
    await restart(rig.hass, rig.entry)
    await rig.recompute()
    assert rig.opening()["opening_state"] == "closing"
    await rig.later(CLOSE - 1)  # timed from the start, not from the close
    assert rig.head() == "off"
    await rig.later(2)
    assert rig.head() == MODE_COOL


async def test_an_override_outlives_a_reload(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    await rig.switch("_primary_window_override", True)
    registry = er.async_get(hass)
    ids = {s: registry.async_get(rig.eid(s)).id for s in ("_primary_window_override", "_primary_paused_open")}
    await hass.config_entries.async_reload(rig.entry.entry_id)
    await hass.async_block_till_done()
    await rig.recompute()
    assert rig.opening()["opening_state"] == "overridden"
    assert rig.head() == MODE_COOL
    # The setup prune keeps the room's switch and indicator (no new records).
    assert {s: registry.async_get(rig.eid(s)).id for s in ids} == ids


async def test_turning_the_pause_off_forgets_an_old_pause(hass: HomeAssistant, freezer) -> None:
    """Paused, then the pause turned off: turned on again later, the room is not
    still paused from back then (the window has since closed)."""
    rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    for pause in (False, True):
        zones = [dict(z) for z in rig.entry.data[CONF_ZONES]]
        zones[0][ZONE_OPENING_PAUSE] = pause
        await rig.window("off" if pause else "on")
        hass.config_entries.async_update_entry(rig.entry, data={**rig.entry.data, CONF_ZONES: zones})
        await hass.config_entries.async_reload(rig.entry.entry_id)
        await hass.async_block_till_done()
        await rig.later(1)
    await rig.recompute()
    assert rig.opening()["opening_state"] == "closed"
    assert rig.head() == MODE_COOL


async def test_a_restart_restarts_the_open_delay(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer)
    await rig.window("on")
    await rig.later(OPEN - 10)
    await restart(rig.hass, rig.entry)
    await rig.recompute()
    await rig.later(OPEN - 1)
    assert rig.head() == MODE_COOL  # a restart only ever lengthens a delay
    await rig.later(2)
    assert rig.head() == "off"


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------
async def test_no_notice_unless_asked(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    assert rig.head() == "off"  # it did pause
    await rig.window("off")
    await rig.later(CLOSE + 1)
    assert rig.head() == MODE_COOL  # and resume
    assert rig.notices() == []


async def test_nothing_resumes_while_the_close_delay_runs(hass: HomeAssistant, freezer) -> None:
    """A closed reading starts the close delay; the resume, its notice and its
    restores come only at the end of it."""
    rig = await _setup(
        hass, freezer, temps=(70, 70), head_cls=DryHead,
        study={**WINDOW_ON, ZONE_ALLOW_DRY: True, ZONE_OPENING_NOTIFY: True},
    )
    await rig.person("dry")
    await rig.recompute()
    await rig.open_and_pause()
    since = rig.thermostat().attributes["paused_since"]
    rig.forget()
    await rig.window("off")
    await rig.recompute()
    await rig.later(CLOSE - 1)
    assert rig.opening()["opening_state"] == "closing"
    assert rig.thermostat().attributes["paused_since"] == since
    assert rig.notices() == [PAUSE_NOTICE]
    assert rig.sent() == []  # no Dry, no resume of any kind yet
    assert rig.head() == "off"
    await rig.later(2)
    assert rig.notices() == [PAUSE_NOTICE, RESUME_NOTICE]
    assert rig.modes_sent() == ["dry"]
    assert rig.thermostat().attributes["paused_since"] is None


@pytest.mark.parametrize(
    ("open_delay", "close_delay", "open_for", "closed_for"),
    [
        (0, 90, "0 s", "90 s"),
        (45, 3540, "45 s", "59 min"),
        (1800, 3600, "30 min", "1 h"),
    ],
)
async def test_notices_say_each_delay_in_seconds_minutes_or_hours(
    hass: HomeAssistant, freezer, open_delay, close_delay, open_for, closed_for
) -> None:
    rig = await _setup(
        hass, freezer,
        study={
            **WINDOW_ON, ZONE_OPENING_NOTIFY: True,
            ZONE_OPENING_OPEN_DELAY: open_delay, ZONE_OPENING_CLOSE_DELAY: close_delay,
        },
    )
    await rig.window("on")
    await rig.later(open_delay + 1)
    await rig.window("off")
    await rig.later(close_delay + 1)
    assert rig.notices() == [
        f"Study paused: Study window open for {open_for}. Heat/cool off in this room.",
        f"Study resumed: closed for {closed_for}.",
    ]


async def test_notices_are_rate_limited(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer, study={**WINDOW_ON, ZONE_OPENING_NOTIFY: True})
    for _ in range(2):  # a door that flaps: one pair in ten minutes
        await rig.open_and_pause()
        await rig.window("off")
        await rig.later(CLOSE + 1)
    assert rig.notices() == [PAUSE_NOTICE, RESUME_NOTICE]
    pass_time(rig.coord, OPENING_NOTIFY_SECONDS)
    await rig.open_and_pause()
    await rig.switch("_primary_window_override", True)
    assert rig.notices() == [PAUSE_NOTICE, RESUME_NOTICE, PAUSE_NOTICE, OVERRIDE_NOTICE]


async def test_a_notice_that_fails_never_holds_up_the_pause(
    hass: HomeAssistant, freezer, caplog
) -> None:
    rig = await _setup(
        hass, freezer,
        study={**WINDOW_ON, ZONE_OPENING_NOTIFY: True, "opening_notify_service": "notify.gone"},
    )
    await rig.open_and_pause()
    assert rig.head() == "off"
    assert "door/window notice not sent" in caplog.text
    assert rig.notices() == []  # nothing went to the entry's service instead


# ---------------------------------------------------------------------------
# The room page
# ---------------------------------------------------------------------------
async def test_room_page_section_is_off_by_default_and_stores_nothing_untouched(
    hass: HomeAssistant, freezer
) -> None:
    rig = await _setup(hass, freezer, study={})
    entry = rig.entry
    before = [dict(zone) for zone in entry.data[CONF_ZONES]]
    result = await open_options_page(hass, entry, "room_1")
    assert sections(result)["opening"].options["collapsed"] is True
    assert default(result, ZONE_OPENING_PAUSE) is False
    assert default(result, ZONE_OPENING_NOTIFY) is False
    assert suggested(result, ZONE_OPENING_OPEN_DELAY) == 60
    assert suggested(result, ZONE_OPENING_CLOSE_DELAY) == 120
    assert suggested(result, ZONE_OPENING_ENTITY) is None
    result = await submit(hass.config_entries.options, result, prefilled(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_ZONES] == before


async def test_room_page_turns_the_pause_on_and_off(hass: HomeAssistant, freezer) -> None:
    rig = await _setup(hass, freezer, study={})
    entry = rig.entry
    manager = hass.config_entries.options
    before = [dict(zone) for zone in entry.data[CONF_ZONES]]
    result = await open_options_page(hass, entry, "room_1")
    result = await submit(manager, result, {
        **prefilled(result), ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_PAUSE: True,
        ZONE_OPENING_OPEN_DELAY: 30, ZONE_OPENING_NOTIFY: True,
        "opening_notify_service": f"notify.{NOTIFY}",
    })
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_ZONES] == [
        {**before[0], ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_PAUSE: True,
         ZONE_OPENING_OPEN_DELAY: 30.0, ZONE_OPENING_NOTIFY: True,
         "opening_notify_service": f"notify.{NOTIFY}"},
        before[1],
    ]
    for store in (entry.data, entry.options):  # never flat
        assert not {ZONE_OPENING_ENTITY, ZONE_OPENING_PAUSE, "opening"} & set(store)
    zone = entry.runtime_data.zones[0]
    assert (zone.opening_active, zone.opening_open_delay, zone.opening_close_delay) == (True, 30, 120)
    assert hass.states.get(rig.eid("_primary_window_override")).state == "off"
    assert hass.states.get(rig.eid("_primary_paused_open")).state == "off"
    # The pause off: the sensor stays, the room's switch and indicator go.
    result = await open_options_page(hass, entry, "room_1")
    assert default(result, ZONE_OPENING_PAUSE) is True
    result = await submit(manager, result, {**prefilled(result), ZONE_OPENING_PAUSE: False})
    await hass.async_block_till_done()
    assert ZONE_OPENING_PAUSE not in entry.data[CONF_ZONES][0]
    assert entry.data[CONF_ZONES][0][ZONE_OPENING_ENTITY] == WINDOW
    with pytest.raises(AssertionError):
        rig.eid("_primary_window_override")
    with pytest.raises(AssertionError):
        rig.eid("_primary_paused_open")


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ({ZONE_OPENING_ENTITY: "binary_sensor.nowhere", ZONE_OPENING_PAUSE: True},
         "opening_entity_invalid"),
        ({ZONE_OPENING_PAUSE: True}, "opening_entity_required"),
        # NaN passes a number box's bounds; the page's own check refuses it.
        ({ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_OPEN_DELAY: float("nan")},
         "opening_delay_invalid"),
        ({ZONE_OPENING_ENTITY: WINDOW, "opening_notify_service": "notify.gone"},
         "opening_notify_service_invalid"),
        ({ZONE_OPENING_ENTITY: WINDOW, "opening_notify_service": "light.turn_on"},
         "opening_notify_service_invalid"),
    ],
    ids=["missing", "pause_no_sensor", "nan_delay", "no_service", "not_notify"],
)
async def test_room_page_refuses_a_bad_section(hass: HomeAssistant, freezer, values, error) -> None:
    rig = await _setup(hass, freezer, study={})
    entry = rig.entry
    before = [dict(zone) for zone in entry.data[CONF_ZONES]]
    result = await open_options_page(hass, entry, "room_1")
    flat = {**prefilled(result), **values}
    # A raw-API submission, so the boxes' own ranges don't stop it first.
    result = await hass.config_entries.options.async_configure(result["flow_id"], nest(result, flat))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"opening": error}
    assert sections(result)["opening"].options["collapsed"] is False
    assert entry.data[CONF_ZONES] == before


@pytest.mark.parametrize(
    "values",
    [
        {ZONE_OPENING_ENTITY: SENSORS[0], ZONE_OPENING_PAUSE: True},  # not a binary_sensor
        {ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_OPEN_DELAY: 1801},
        {ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_CLOSE_DELAY: 3601},
        {ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_CLOSE_DELAY: -1},
    ],
    ids=["not_binary", "open_delay", "close_delay", "negative"],
)
async def test_room_page_selectors_refuse_out_of_range(hass: HomeAssistant, freezer, values) -> None:
    rig = await _setup(hass, freezer, study={})
    entry = rig.entry
    before = [dict(zone) for zone in entry.data[CONF_ZONES]]
    result = await open_options_page(hass, entry, "room_1")
    assert set(values) <= set(fields(result))  # the page shows these fields
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], nest(result, {**prefilled(result), **values})
        )
    assert entry.data[CONF_ZONES] == before


def test_room_page_copy_is_translated_and_credits_the_idea() -> None:
    import json
    from pathlib import Path

    folder = Path("custom_components/mxz_coordinator")
    strings = json.loads((folder / "strings.json").read_text())
    room = strings["options"]["step"]["room"]["sections"]["opening"]
    assert set(room["data"]) == set(ROOM_SECTION_FIELDS["opening"])
    assert "@mikepell007" in room["description"]
    errors = strings["options"]["error"]
    for key in ("opening_entity_invalid", "opening_entity_required", "opening_delay_invalid",
                "opening_notify_service_invalid"):
        assert key in errors
    assert strings["entity"]["switch"]["zone_window_override"]["name"] == "{zone} window override"
    assert strings["entity"]["binary_sensor"]["zone_paused_open"]["name"] == "{zone} paused open"
