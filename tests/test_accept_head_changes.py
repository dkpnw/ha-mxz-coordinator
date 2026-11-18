"""Accept changes made at the head: a setpoint or on/off set at a head is adopted by its room.

From @BHSPitMonkey's fork (Discussion #103), rebuilt on the fan memory's rule (F1): a report
MXZ's own writes can explain is never a person, however late it comes. On by default, for
entries with no key too. All rooms, heads and values are invented, and the invented people
change a head directly, as its remote does, never through a service MXZ could see.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from hypothesis import example, given
from hypothesis import strategies as st

from custom_components.mxz_coordinator.const import (
    ADOPT_DELAY,
    BAND_DRIFT_DELAY,
    CONF_ACCEPT_HEAD_CHANGES,
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_ZONES,
    DEFAULT_ACCEPT_HEAD_CHANGES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    INHIBIT_ACTION_ECO,
    INHIBIT_ACTION_FAN_ONLY,
    INHIBIT_ACTION_OFF,
    OFF_WHILE_ENABLED_DELAY,
    STARTUP_RECOVER_DELAY,
    ZONE_CLIMATE,
    ZONE_DRIVE_OFFSET,
    ZONE_DUAL_SETPOINT,
    ZONE_FLOOR_HIGH,
    ZONE_FLOOR_LOW,
    ZONE_HANDS_OFF,
    ZONE_HEAD_TYPE,
    ZONE_NAME,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.logic import HeadWriteMemory
from custom_components.mxz_coordinator.room_state import SECTION_HEAD_WRITES
from tests.cloud_heads import (
    COMFORT,
    ComfortHead,
    past_startup,
    past_window,
    recompute,
    record,
    register,
    setup_rooms,
    writes,
)
from tests.flow_pages import open_options_page, prefilled, submit
from tests.ha_test import (
    MockConfigEntry,
    async_fire_time_changed,
)
from tests.peek import armed, cloud_record
from tests.property_rig import (
    CLOSE_DELAY,
    HOLD,
    OPEN_DELAY,
    PURE,
    SEQUENCES,
    TARGET,
    Rig,
    open_rig,
)
from tests.restart import stopped
from tests.room_state_store import saved_section
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
)
from tests.test_setpoint_echo import CN105Head
from tests.test_vane_kick import _enable_all as _vane_enable_all
from tests.test_vane_kick import _setup as _vane_setup

HOT = TARGET + 5
COLD = TARGET - 5
HELD = ADOPT_DELAY + 1
INTEGRATION = Path(__file__).parents[1] / "custom_components/mxz_coordinator"


def _target(rig: Rig) -> float:
    return float(rig.hass.states.get(rig.eid("_primary_target")).state)


def _enabled(rig: Rig) -> bool:
    return rig.hass.states.get(rig.eid("_primary_enable")).state == "on"


def _edges(rig: Rig) -> tuple[float | None, float | None]:
    state = rig.hass.states.get(rig.heads[0].entity_id)
    return state.attributes["target_temp_low"], state.attributes["target_temp_high"]


def _armed(rig: Rig) -> bool:
    return armed(rig.coord, "heal", (rig.heads[0].entity_id, "adopt"))


async def _cooling(hass: HomeAssistant, freezer: Any, **kwargs: Any) -> Rig:
    """The first room is hot, so its head is cooling to the target with a 2 °F band."""
    rig = await open_rig(hass, freezer, temps=(HOT, TARGET), **kwargs)
    assert rig.heads[0].mode == "cool"
    assert _edges(rig) == (TARGET - 2, TARGET)
    return rig


async def _time_passes(rig: Rig) -> None:
    """Long enough for a hold and the debounced refresh after it, with no compute forced."""
    for _ in range(2):
        rig.freezer.tick(timedelta(seconds=HELD))
        async_fire_time_changed(rig.hass, dt_util.utcnow())
        await rig.hass.async_block_till_done()


async def _press(rig: Rig, room: int = 0, **change: Any) -> None:
    """A person at the room's head, with no compute after it."""
    rig.heads[room].person_setpoint(**change)
    await rig.hass.async_block_till_done()


# ---------------------------------------------------------------------------
# The option: on by default, for every entry, and off makes MXZ the only writer
# ---------------------------------------------------------------------------


def test_the_default_is_on() -> None:
    assert DEFAULT_ACCEPT_HEAD_CHANGES is True


async def test_an_entry_from_before_the_option_has_it_on(hass: HomeAssistant, freezer: Any) -> None:
    """No key stored, as on every entry saved before this: adopted, not overwritten."""
    rig = await _cooling(hass, freezer)
    assert CONF_ACCEPT_HEAD_CHANGES not in {**rig.entry.data, **rig.entry.options}
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == 66
    await rig.close()


async def test_option_off_overwrites(hass: HomeAssistant, freezer: Any) -> None:
    """The negative of everything below: MXZ is the only writer, and the room never moves."""
    rig = await _cooling(hass, freezer, data={CONF_ACCEPT_HEAD_CHANGES: False})
    await _press(rig, low=64, high=66)
    assert not _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == TARGET
    assert _edges(rig) == (TARGET - 2, TARGET)
    rig.heads[0].person_mode("off")
    await hass.async_block_till_done()
    await rig.later(OFF_WHILE_ENABLED_DELAY + 1)
    assert _enabled(rig) is True
    assert rig.heads[0].mode == "cool"
    await rig.close()


async def test_configure_shows_it_on_and_turning_it_off_sticks(hass: HomeAssistant, freezer: Any) -> None:
    """On the Comfort page, shown on for an entry with no key; a save of off reaches the plan."""
    rig = await _cooling(hass, freezer)
    form = await open_options_page(hass, rig.entry, "comfort")
    assert prefilled(form)[CONF_ACCEPT_HEAD_CHANGES] is True
    await submit(hass.config_entries.options, form, {**prefilled(form), CONF_ACCEPT_HEAD_CHANGES: False})
    await hass.async_block_till_done()
    assert {**rig.entry.data, **rig.entry.options}[CONF_ACCEPT_HEAD_CHANGES] is False
    assert rig.coord.accept_head_changes is False
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == TARGET
    await rig.close()


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json"])
@pytest.mark.parametrize("flow", ["config", "options"])
def test_the_help_says_on_is_the_default(name: str, flow: str) -> None:
    """The fork's text said off, and this default is the one change from it."""
    comfort = json.loads((INTEGRATION / name).read_text())[flow]["step"]["comfort"]
    assert comfort["data"][CONF_ACCEPT_HEAD_CHANGES] == "Accept changes made at the head"
    assert comfort["data_description"][CONF_ACCEPT_HEAD_CHANGES].startswith("On by default.")


# ---------------------------------------------------------------------------
# What a person's change at a head does
# ---------------------------------------------------------------------------


async def test_a_setpoint_in_cool_follows_the_high_edge(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == 66
    assert rig.heads[0].mode == "cool"
    assert _edges(rig) == (64, 66)
    await rig.close()


async def test_a_setpoint_in_heat_follows_the_low_edge(hass: HomeAssistant, freezer: Any) -> None:
    rig = await open_rig(hass, freezer, temps=(COLD, TARGET))
    await rig.select_mode("heat")
    assert rig.heads[0].mode == "heat"
    assert _edges(rig) == (TARGET, TARGET + 2)
    await _press(rig, low=74, high=76)
    await rig.later(HELD)
    assert _target(rig) == 74
    assert rig.heads[0].mode == "heat"
    await rig.close()


class SingleHead(MockHead):
    """One setpoint, as most MXZ indoor units have (#6)."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, suffix: str) -> None:
        super().__init__(suffix)
        self._attr_native_target_temperature = None

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = mode
        if (value := kwargs.get("temperature")) is not None:
            self._attr_native_target_temperature = value
        self.async_write_ha_state()


async def _mock_rooms(
    hass: HomeAssistant, cls: type[MockHead], temps: tuple[float, float], target: float,
) -> tuple[str, MockConfigEntry]:
    """An entry over two heads of ``cls``, whichever unit the system is in."""
    head_a, head_b = await _setup_mock_heads(hass, cls=cls)
    await _set_temp(hass, SENSOR_A, temps[0])
    await _set_temp(hass, SENSOR_B, temps[1])
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_ZONES: [
                {ZONE_NAME: "Study", ZONE_CLIMATE: head_a, ZONE_SENSOR: SENSOR_A},
                {ZONE_NAME: "Den", ZONE_CLIMATE: head_b, ZONE_SENSOR: SENSOR_B},
            ],
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_MODE_HYSTERESIS: HOLD,
        },
        version=2,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for slug in ("primary", "secondary"):
        await _set_target(hass, _eid(hass, entry, f"_{slug}_target"), target)
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await _recompute(hass, entry)
    return head_a, entry


async def _elapse(hass: HomeAssistant, seconds: float) -> None:
    await hass.async_block_till_done()  # the head's report arms its timer first, on every core
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_a_single_setpoint_heads_temperature_is_the_target(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    head, entry = await _mock_rooms(hass, SingleHead, (HOT, TARGET), TARGET)
    assert hass.states.get(head).attributes["temperature"] == TARGET
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": head, "temperature": 66}, blocking=True
    )
    await _elapse(hass, HELD)
    assert float(hass.states.get(_eid(hass, entry, "_primary_target")).state) == 66
    await _recompute(hass, entry)
    assert hass.states.get(head).attributes["temperature"] == 66
    assert hass.states.get(head).state == "cool"


async def test_head_off_disables_the_room_and_stays_off(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    writes_before = list(rig.heads[0].mode_writes)
    await rig.person_mode("off")
    assert _enabled(rig) is True  # not yet: it has to hold
    await rig.later(HELD)
    assert _enabled(rig) is False
    await rig.later(OFF_WHILE_ENABLED_DELAY + 1)
    assert rig.heads[0].mode == "off"
    assert rig.heads[0].mode_writes == writes_before  # never healed back on
    await rig.close()


async def test_head_on_enables_the_room_with_the_setpoint_it_was_given(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await rig.switch("_primary_enable", False)
    assert rig.heads[0].mode == "off"
    await _press(rig, mode="cool", low=62, high=64)
    await rig.later(HELD)
    assert _enabled(rig) is True
    assert _target(rig) == 64
    assert rig.heads[0].mode == "cool"
    assert _edges(rig) == (62, 64)
    await rig.close()


async def test_a_setpoint_alone_doesnt_enable_a_disabled_room(hass: HomeAssistant, freezer: Any) -> None:
    """The head hasn't shown the off MXZ sent it yet, and a press there isn't turning it on."""
    rig = await _cooling(hass, freezer)
    rig.heads[0].lags_commands = True
    await rig.switch("_primary_enable", False)
    assert rig.heads[0].mode == "cool"
    await _press(rig, low=64, high=66)
    assert not _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is False
    assert _target(rig) == TARGET
    await rig.close()


async def test_a_flip_alone_doesnt_read_a_setpoint_mxz_has_forgotten(hass: HomeAssistant, freezer: Any) -> None:
    """Memory lost, head behind the target: the mode moved, the setpoint it was on didn't."""
    rig = await _cooling(hass, freezer)
    rig.heads[0].lags_commands = True
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    rig.coord._head_writes.clear()
    rig.heads[0].lags_commands = False
    await _press(rig, mode="heat")
    assert rig.coord._head_adoption(rig.heads[0].entity_id, {"mode"}) == {}  # a person's, nothing in it
    assert not _armed(rig)  # so nothing held (Discussion #103)
    await rig.later(HELD)
    assert _target(rig) == 72
    await rig.close()


async def test_a_head_coming_back_from_unavailable_is_no_person(hass: HomeAssistant, freezer: Any) -> None:
    """It may come back on anything, and that is the head reloading."""
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    head._attr_available = False
    head.async_write_ha_state()
    await hass.async_block_till_done()
    head._attr_available = True
    head.person_setpoint(mode="off", low=64, high=66)
    await hass.async_block_till_done()
    assert not _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is True
    assert _target(rig) == TARGET
    await rig.close()


async def test_head_on_with_its_old_setpoint_enables_without_moving_the_target(
    hass: HomeAssistant, freezer: Any,
) -> None:
    """Only what moved counts: the setpoint this head was already on isn't a new target."""
    rig = await _cooling(hass, freezer)
    await rig.switch("_primary_enable", False)
    await rig.person_mode("cool")
    await rig.later(HELD)
    assert _enabled(rig) is True
    assert _target(rig) == TARGET
    await rig.close()


async def test_the_heat_cool_choice_is_not_adopted(hass: HomeAssistant, freezer: Any) -> None:
    """A flip to heat with a new low edge: the target follows, the mode does not."""
    rig = await _cooling(hass, freezer)
    await _press(rig, mode="heat", low=72, high=74)
    await rig.later(HELD)
    assert _target(rig) == 72  # heat reads the low edge
    assert rig.coord.current_shared_mode == "cool"
    assert rig.heads[0].mode == "cool"  # the room is 75 against 72: the plan cools
    assert _edges(rig) == (70, 72)
    await rig.close()


async def test_a_flip_alone_moves_nothing_and_goes_back_by_itself(hass: HomeAssistant, freezer: Any) -> None:
    """No new setpoint, so no new target, and no hold: the next compute puts the head back.

    Before Discussion #103 an empty hold armed here, and its end computed; now nothing
    holds the head off the plan, as with the option off.
    """
    rig = await _cooling(hass, freezer)
    await _time_passes(rig)  # nothing left over from the setup to compute for it
    await _press(rig, mode="heat")
    assert not _armed(rig)  # nothing to adopt, so nothing held
    assert rig.coord._head_change_settling(rig.heads[0].entity_id) is False
    await _time_passes(rig)
    await rig.settle()
    assert rig.heads[0].mode == "cool"
    assert _target(rig) == TARGET
    assert rig.coord.current_shared_mode == "cool"
    await rig.close()


@pytest.mark.parametrize("mode", ["heat_cool", "auto", "dry"])
async def test_a_banned_mode_is_healed_not_adopted(hass: HomeAssistant, freezer: Any, mode: str) -> None:
    rig = await _cooling(hass, freezer)
    await _press(rig, mode=mode, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == TARGET
    assert _enabled(rig) is True
    await rig.later(BAND_DRIFT_DELAY)
    assert rig.heads[0].mode == "cool"
    assert _edges(rig) == (TARGET - 2, TARGET)
    assert _target(rig) == TARGET
    await rig.close()


# ---------------------------------------------------------------------------
# The hold
# ---------------------------------------------------------------------------


async def test_a_change_must_hold_and_isnt_overwritten_while_it_does(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await _press(rig, low=64, high=66)
    await rig.later(ADOPT_DELAY - 1)  # a compute lands inside the hold
    assert _target(rig) == TARGET
    assert _edges(rig) == (64, 66)
    await rig.later(2)
    assert _target(rig) == 66
    await rig.close()


async def test_each_press_starts_the_hold_over(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await _press(rig, low=66, high=68)
    await rig.later(ADOPT_DELAY - 1)
    await _press(rig, low=64, high=66)
    await rig.later(ADOPT_DELAY - 1)
    assert _target(rig) == TARGET
    await rig.later(2)
    assert _target(rig) == 66
    await rig.close()


async def test_two_heads_pressed_inside_one_hold_are_each_adopted(hass: HomeAssistant, freezer: Any) -> None:
    """The first head's adoption neither writes over the second head's hold nor ends it."""
    rig = await open_rig(hass, freezer, temps=(HOT, HOT))
    assert [head.mode for head in rig.heads] == ["cool", "cool"]
    await _press(rig, low=64, high=66)
    await rig.later(2)
    await _press(rig, 1, low=66, high=68)
    await rig.later(ADOPT_DELAY - 1)  # the Study's hold is over, the Den's is not
    assert _target(rig) == 66
    assert float(hass.states.get(rig.eid("_secondary_target")).state) == TARGET
    den = hass.states.get(rig.heads[1].entity_id)
    assert (den.attributes["target_temp_low"], den.attributes["target_temp_high"]) == (66, 68)
    await rig.later(2)
    assert float(hass.states.get(rig.eid("_secondary_target")).state) == 68
    assert _target(rig) == 66
    await rig.close()


async def test_a_room_enabled_at_its_head_inside_the_mode_wait_waits_for_it(
    hass: HomeAssistant, freezer: Any,
) -> None:
    """Adopting cool at a disabled room's head takes its target, not the shared mode."""
    rig = await open_rig(hass, freezer, temps=(COLD, TARGET))
    study = rig.eid("_primary_thermostat")
    assert hass.states.get(study).attributes["mode_change_hold_remaining"] == HOLD
    await rig.switch("_secondary_enable", False)
    await rig.temp(0, TARGET)
    await _press(rig, 1, mode="cool", low=64, high=66)
    await rig.later(HELD)
    assert hass.states.get(rig.eid("_secondary_enable")).state == "on"
    assert float(hass.states.get(rig.eid("_secondary_target")).state) == 66
    assert hass.states.get(rig.eid("_plan")).state == "heat"
    assert hass.states.get(study).attributes["mode_change_hold_remaining"] > 0
    assert rig.heads[1].mode != "cool"
    await rig.later(HOLD)
    assert hass.states.get(rig.eid("_plan")).state == "cool"
    assert rig.heads[1].mode == "cool"
    assert float(hass.states.get(rig.eid("_secondary_target")).state) == 66
    await rig.close()


async def test_a_change_undone_inside_the_hold_is_nothing(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await _press(rig, low=64, high=66)
    await rig.later(2)
    await _press(rig, low=TARGET - 2, high=TARGET)
    assert not _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == TARGET
    await rig.close()


async def test_eco_during_the_hold_lets_the_plan_write_at_once(hass: HomeAssistant, freezer: Any) -> None:
    """The hold only protects a change that would still count."""
    rig = await _cooling(hass, freezer)
    await _press(rig, low=64, high=66)
    await rig.switch("_eco_idle", True)
    assert rig.heads[0].mode != "cool"  # the room is inside the eco band: not cooling now
    await rig.later(HELD)
    assert _target(rig) == TARGET
    await rig.close()


async def test_a_head_put_back_inside_the_hold_gets_what_the_hold_held_off(
    hass: HomeAssistant, freezer: Any,
) -> None:
    """The hold is over, so the next compute writes what the skipped one wanted."""
    rig = await _cooling(hass, freezer)
    await _press(rig, low=64, high=66)
    await rig.temp(0, TARGET)  # a compute in the hold wants this head parked, and is skipped
    assert rig.heads[0].mode == "cool"
    await _press(rig, low=TARGET - 2, high=TARGET)
    assert not _armed(rig)
    await rig.settle()
    assert rig.heads[0].mode == "fan_only"
    await rig.close()


async def test_a_fan_report_arms_nothing_and_a_manual_fan_survives_adoption(
    hass: HomeAssistant, freezer: Any,
) -> None:
    """#99's F1 is untouched: a pick is held at once, and adoption leaves it held."""
    rig = await _cooling(hass, freezer, data={CONF_FAN_BOOST_ENABLE: True})
    await rig.person_fan("quiet")
    assert not _armed(rig)
    assert rig.fan_held() is True
    writes = len(rig.heads[0].fan_writes)
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == 66
    assert rig.fan_held() is True
    assert rig.fan_auto_switch() == "off"
    assert rig.heads[0].fan == "quiet"
    assert rig.heads[0].fan_writes[writes:] == []
    await rig.close()


# ---------------------------------------------------------------------------
# Never adopted
# ---------------------------------------------------------------------------


async def test_a_plan_parked_fan_only_head_is_no_request(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await rig.temp(0, TARGET)
    assert rig.heads[0].mode == "fan_only"
    await rig.later(HELD)
    assert _enabled(rig) is True
    assert _target(rig) == TARGET
    await rig.close()


async def test_a_plan_parked_off_head_is_no_request(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer, data={CONF_IDLE_ACTION: IDLE_ACTION_OFF})
    await rig.temp(0, TARGET)
    assert rig.heads[0].mode == "off"
    await rig.later(HELD)
    assert _enabled(rig) is True
    # And the same off from a person can't be told apart: nothing the plan wants is undone.
    await rig.temp(0, HOT)
    assert rig.heads[0].mode == "cool"
    await rig.temp(0, TARGET)
    await rig.later(HELD)
    assert _enabled(rig) is True
    await rig.close()


async def test_the_plans_off_is_no_request_even_with_the_memory_lost(hass: HomeAssistant, freezer: Any) -> None:
    """A restart loses the memory, but the plan wants that off, so it counts for nothing."""
    rig = await _cooling(hass, freezer, data={CONF_IDLE_ACTION: IDLE_ACTION_OFF})
    head = rig.heads[0]
    head.lags_commands = True
    await rig.temp(0, TARGET)
    assert head.mode == "cool"
    rig.coord._head_writes.clear()
    assert await rig.report_next_command()
    assert head.mode == "off"
    assert not _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is True
    await rig.close()


async def test_a_person_off_while_parked_in_fan_only_is_a_request(hass: HomeAssistant, freezer: Any) -> None:
    """The other side of the two above: off isn't the plan's fan_only, so it is a person's."""
    rig = await _cooling(hass, freezer)
    await rig.temp(0, TARGET)
    await rig.person_mode("off")
    await rig.later(HELD)
    assert _enabled(rig) is False
    await rig.close()


async def test_a_person_in_fan_only_is_not_adopted(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await rig.person_mode("fan_only")
    await rig.later(HELD)
    assert _enabled(rig) is True
    assert _target(rig) == TARGET
    assert rig.heads[0].mode == "cool"
    await rig.close()


async def test_eco_is_not_adopted_and_its_band_isnt_read_back(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await rig.switch("_eco_idle", True)
    await _press(rig, mode="cool", low=60, high=62)
    await rig.later(HELD)
    assert _target(rig) == TARGET
    await rig.person_mode("off")
    await rig.later(HELD)
    assert _enabled(rig) is True
    await rig.switch("_eco_idle", False)
    await rig.later(HELD)
    assert _target(rig) == TARGET
    assert _enabled(rig) is True
    assert rig.heads[0].mode == "cool"
    assert _edges(rig) == (TARGET - 2, TARGET)
    await rig.close()


@pytest.mark.parametrize("action", [INHIBIT_ACTION_OFF, INHIBIT_ACTION_FAN_ONLY, INHIBIT_ACTION_ECO])
async def test_a_standby_hold_is_not_adopted(hass: HomeAssistant, freezer: Any, action: str) -> None:
    hold = "binary_sensor.property_hold"
    hass.states.async_set(hold, "off")
    rig = await _cooling(hass, freezer, data={CONF_INHIBIT_ENTITY: hold, CONF_INHIBIT_ACTION: action})
    hass.states.async_set(hold, "on")
    await hass.async_block_till_done()
    await rig.settle()
    assert rig.coord.inhibited is True
    await _press(rig, mode="cool", low=60, high=62)
    await rig.later(HELD)
    assert _target(rig) == TARGET
    assert _enabled(rig) is True
    await rig.person_mode("off")
    await rig.later(HELD)
    assert _enabled(rig) is True
    hass.states.async_set(hold, "off")
    await hass.async_block_till_done()
    await rig.later(HELD)
    assert _target(rig) == TARGET
    assert _enabled(rig) is True
    await rig.close()


async def test_the_kill_switch_is_not_adopted_and_leaves_the_head(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await rig.switch("_coordinator_enable", False)
    await _press(rig, low=64, high=66)
    assert not _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == TARGET
    assert _edges(rig) == (64, 66)  # hands off the heads, as ever
    await rig.person_mode("off")
    await rig.later(HELD)
    assert _enabled(rig) is True
    await rig.switch("_coordinator_enable", True)
    await rig.later(HELD)
    assert _target(rig) == TARGET
    assert _enabled(rig) is True
    assert rig.heads[0].mode == "cool"
    assert _edges(rig) == (TARGET - 2, TARGET)
    await rig.close()


async def test_a_hands_off_rooms_head_stays_the_persons(hass: HomeAssistant, freezer: Any) -> None:
    """A room left to its own remote: its head coming on must not take it back."""
    rig = await _cooling(hass, freezer, first_room={ZONE_HANDS_OFF: True})
    await rig.switch("_primary_enable", False)
    assert rig.heads[0].mode == "off"
    await _press(rig, mode="cool", low=62, high=64)
    await rig.later(HELD)
    assert _enabled(rig) is False
    assert _target(rig) == TARGET
    assert rig.heads[0].mode == "cool"
    assert _edges(rig) == (62, 64)
    await rig.close()


async def test_an_enabled_hands_off_rooms_off_hands_it_to_its_remote(hass: HomeAssistant, freezer: Any) -> None:
    """Off disables it like any room, which for this one means released: on stays the person's."""
    rig = await _cooling(hass, freezer, first_room={ZONE_HANDS_OFF: True})
    await rig.person_mode("off")
    await rig.later(HELD)
    assert _enabled(rig) is False
    assert rig.coord.hands_off_released("primary") is True
    await _press(rig, mode="cool", low=62, high=64)
    await rig.later(HELD)
    assert _enabled(rig) is False
    assert _target(rig) == TARGET
    assert rig.heads[0].mode == "cool"
    await rig.close()


async def test_a_paused_rooms_head_keeps_its_own_rule(hass: HomeAssistant, freezer: Any) -> None:
    """The pause's off isn't a request, and a person's takeover doesn't change the room."""
    rig = await open_rig(hass, freezer, temps=(HOT, TARGET), window="off")
    await rig.window("on")
    await rig.later(OPEN_DELAY)
    assert rig.heads[0].mode == "off"
    await rig.later(HELD)
    assert _enabled(rig) is True
    await _press(rig, mode="cool", low=64, high=66)
    await rig.later(HELD)
    assert rig.heads[0].mode == "cool"  # the person's takeover, as before
    assert _enabled(rig) is True
    assert _target(rig) == TARGET  # a takeover, not a new target
    await rig.window("off")
    await rig.later(CLOSE_DELAY)
    assert _enabled(rig) is True
    await rig.close()


async def test_while_home_assistant_is_starting_nothing_is_adopted(hass: HomeAssistant, freezer: Any) -> None:
    """Not even a person's press: a head still loading can report anything."""
    rig = await _cooling(hass, freezer)
    rig.coord._startup_quiet = True
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == TARGET
    await rig.close()


async def test_a_vane_kick_is_not_a_person(hass: HomeAssistant) -> None:
    """A kick runs a disabled room's off head in fan_only and back: MXZ's writes, no request."""
    entry, heads, vane = await _vane_setup(hass)
    await _vane_enable_all(hass, entry)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _eid(hass, entry, "_primary_enable")}, blocking=True,
    )
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    head = heads[0].entity_id
    assert hass.states.get(head).state == "off"
    seen: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda event: seen.append(event.data["new_state"].state) if event.data["entity_id"] == head else None,
    )
    await hass.services.async_call(
        "climate", "set_swing_mode",
        {"entity_id": _eid(hass, entry, "_primary_thermostat"), "swing_mode": "—"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert vane.history == ["—"]
    assert "fan_only" in seen
    assert hass.states.get(head).state == "off"
    await _elapse(hass, HELD)
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "off"
    assert not armed(entry.runtime_data, "heal", (head, "adopt"))


# ---------------------------------------------------------------------------
# MXZ's own writes, however late they show
# ---------------------------------------------------------------------------


async def test_an_own_write_is_never_adopted(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    assert _edges(rig) == (70, 72)
    assert not _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == 72
    await rig.close()


async def test_a_head_two_writes_behind_is_not_adopted(hass: HomeAssistant, freezer: Any) -> None:
    """The case a comparison with the target alone gets wrong: 72 is no longer the target."""
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    head.lags_commands = True
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    await _set_target(hass, rig.eid("_primary_target"), 74)
    await rig.settle()
    assert _edges(rig) == (TARGET - 2, TARGET)  # neither has shown yet
    assert await rig.report_next_command()
    assert _edges(rig) == (70, 72)
    assert not _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == 74
    while await rig.report_next_command():
        assert not _armed(rig)
        await rig.later(HELD)
        assert _target(rig) == 74
    assert _edges(rig) == (72, 74)
    assert _enabled(rig) is True
    await rig.close()


async def test_a_late_off_of_mxz_is_not_a_person_and_the_plan_runs_on(hass: HomeAssistant, freezer: Any) -> None:
    """MXZ parks the head off, then the room needs it again, and the off shows only now."""
    rig = await _cooling(hass, freezer, data={CONF_IDLE_ACTION: IDLE_ACTION_OFF})
    head = rig.heads[0]
    head.lags_commands = True
    await rig.temp(0, TARGET)
    assert head.mode == "cool"  # the off hasn't shown
    await rig.temp(0, HOT)
    assert await rig.report_next_command()
    assert head.mode == "off"
    assert not _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is True
    assert rig.hass.states.get(rig.eid("_plan")).attributes["zones"][0]["engage"] == "cool"
    await rig.close()


async def test_the_eco_band_showing_late_is_not_adopted(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    head.lags_commands = True
    await rig.switch("_eco_idle", True)
    await rig.switch("_eco_idle", False)
    assert head.mode == "cool"
    while await rig.report_next_command():
        await rig.later(HELD)
        assert _target(rig) == TARGET
        assert _enabled(rig) is True
    await rig.settle()
    while await rig.report_next_command():
        pass
    assert _edges(rig) == (TARGET - 2, TARGET)
    await rig.close()


async def test_a_rewrite_doesnt_forget_what_the_head_still_shows(hass: HomeAssistant, freezer: Any) -> None:
    """#14's shape: a compute burst rewrites the new value while the head shows the old."""
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    head.lags_commands = True
    await _set_target(hass, rig.eid("_primary_target"), 72)
    for _ in range(5):
        await rig.settle()
    memory = rig.coord._head_writes[head.entity_id]["target_temp_high"]
    assert memory.values == (TARGET, 72)
    while await rig.report_next_command():
        assert not _armed(rig)
    assert _edges(rig) == (70, 72)
    assert memory.values == (72,)
    await rig.later(HELD)
    assert _target(rig) == 72
    await rig.close()


async def test_a_head_that_blinks_back_inside_the_hold_moves_nothing(hass: HomeAssistant, freezer: Any) -> None:
    """A report of the old value, past the memory, has to hold to count, and this one doesn't."""
    rig = await _cooling(hass, freezer)
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    await _press(rig, low=TARGET - 2, high=TARGET)
    assert _armed(rig) is True
    await rig.later(2)
    await _press(rig, low=70, high=72)
    assert not _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == 72
    await rig.close()


async def test_a_press_mxz_never_wrote_is_adopted_while_the_head_lags(hass: HomeAssistant, freezer: Any) -> None:
    """The memory widens nothing but MXZ's own writes."""
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    head.lags_commands = True
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    await _press(rig, low=64, high=66)
    assert _armed(rig) is True
    await rig.later(HELD)
    assert _target(rig) == 66
    await rig.close()


async def test_the_lagging_write_after_an_adopted_press_is_still_mxz_s(hass: HomeAssistant, freezer: Any) -> None:
    """The press takes the head's last shown place, and the write it overtook stays remembered."""
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    head.lags_commands = True
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == 66
    assert await rig.report_next_command()  # the 72 it had queued
    assert _edges(rig)[1] == 72
    await rig.later(HELD)
    assert _target(rig) == 66
    await rig.close()


async def _rewritten_while_lagging(rig: Rig) -> None:
    """MXZ writes 72 on two applies while the head still shows 70: two copies queued.

    How many applies one settle runs depends on Home Assistant's refresh scheduling:
    on 2024.12.5 the first settle runs two refreshes and queues both copies, on 2026.x
    it runs one. Settle until two are queued, never past it.
    """
    head = rig.heads[0]
    head.lags_commands = True
    await _set_target(rig.hass, rig.eid("_primary_target"), 72)
    for _ in range(2):
        if len(head.unreported_commands) >= 2:
            break
        await rig.settle()
    assert [command["target_temp_high"] for command in head.unreported_commands] == [72, 72]


async def test_a_rewrite_still_queued_is_not_adopted_over_a_later_press(hass: HomeAssistant, freezer: Any) -> None:
    """docs/STATE-MACHINES.md SM-10, a rewrite kept once: the second 72 lands after the press."""
    rig = await _cooling(hass, freezer)
    await _rewritten_while_lagging(rig)
    assert await rig.report_next_command()
    assert _edges(rig)[1] == 72
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == 66
    while await rig.report_next_command():
        assert not _armed(rig)
        await rig.later(HELD)
        assert _target(rig) == 66
    assert _edges(rig) == (64, 66)
    await rig.close()


async def test_a_press_back_to_a_rewritten_value_reads_as_mxzs_once(hass: HomeAssistant, freezer: Any) -> None:
    """The cost of keeping each copy (F1), and its end.

    The head took both copies of 72, the second with nothing new to show, so MXZ still
    counts one. A person's 66 is adopted; a press back to 72 then reads as MXZ's copy and
    the head goes back to 66. Once the head shows that write, a press of 72 is a press.
    """
    rig = await _cooling(hass, freezer)
    await _rewritten_while_lagging(rig)
    rig.heads[0].lags_commands = False
    await rig.report_next_command()
    await rig.report_next_command()
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == 66
    await _press(rig, low=70, high=72)
    assert not _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == 66
    assert _edges(rig) == (64, 66)
    await _press(rig, low=70, high=72)
    assert _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == 72
    await rig.close()


async def test_known_cost_a_press_of_the_queued_value_at_a_lagging_head_reads_as_mxzs(
    hass: HomeAssistant, freezer: Any,
) -> None:
    """Known cost, docs/STATE-MACHINES.md SM-10 path (b), accepted (the owner's decision,
    Nov 17, 2026): MXZ's 72 is still queued at a head that shows writes late. A person
    presses 72 at the remote, then 66. MXZ's 72 lands after the 66 and is adopted over it.

    MXZ sees only the head's reports: 72 shown, a person's 66, 72 again. They are the
    reports of test_up_and_back_are_both_adopted, where that last report is a person's press
    and must be adopted. No rule that reads only reports can give both answers."""
    rig = await _cooling(hass, freezer)
    rig.heads[0].lags_commands = True
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    assert [command["target_temp_high"] for command in rig.heads[0].unreported_commands] == [72]
    await _press(rig, low=70, high=72)
    await rig.later(HELD)
    assert _target(rig) == 72
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == 66
    assert await rig.report_next_command()  # MXZ's 72, late
    assert _edges(rig)[1] == 72
    assert _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == 72
    await rig.close()


async def test_known_cost_a_press_of_an_older_queued_value_reads_as_mxzs(
    hass: HomeAssistant, freezer: Any,
) -> None:
    """Known cost, docs/STATE-MACHINES.md SM-10 path (c), accepted (the owner's decision,
    Nov 17, 2026): MXZ's 72 and then 74 are queued at a head that shows writes late. A
    person presses 72, MXZ writes 74, the person presses 74, then 66. MXZ's 72 lands after
    the 66 and is adopted over it.

    MXZ sees only the head's reports: 72 and 74, both values it wrote, then a person's 66,
    then 72 again. To MXZ the head has shown its 72 and passed it, as in
    test_a_press_of_a_write_the_head_has_passed_is_adopted (72 shown, 74 shown, then 72),
    where that last 72 is a person's press and must be adopted. The 66 between doesn't tell
    the two apart. No rule that reads only reports can give both answers."""
    rig = await _cooling(hass, freezer)
    rig.heads[0].lags_commands = True
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    await _press(rig, low=70, high=72)
    await rig.later(HELD)
    await _set_target(hass, rig.eid("_primary_target"), 74)
    await rig.settle()
    assert [command["target_temp_high"] for command in rig.heads[0].unreported_commands][:2] == [72, 74]
    await _press(rig, low=72, high=74)
    await rig.later(HELD)
    assert _target(rig) == 74
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == 66
    assert await rig.report_next_command()  # MXZ's 72, late
    assert _edges(rig)[1] == 72
    assert _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == 72
    await rig.close()


async def test_known_cost_a_write_of_the_shown_value_landing_after_a_press_is_adopted(
    hass: HomeAssistant, freezer: Any,
) -> None:
    """Known cost, docs/STATE-MACHINES.md SM-10, accepted (the owner's decision, Nov 17,
    2026), as on the base: a setpoint write also sends the mode the head shows, "cool". MXZ
    counts no copy of that "cool", since the head shows nothing new for it. Here the head
    shows writes late, so the command is still queued when a person turns the head off. It
    lands after the off, and its "cool" is adopted: the room is enabled again.

    A head that took the command at once sends the same reports as one that still has it
    queued. Kept, that copy would refuse the person's next "cool" at every head, lagging or
    not (test_off_and_on_at_the_head_count_after_target_changes). This is the one write
    that test_no_write_still_queued_is_forgotten_but_by_a_press_of_its_value doesn't check."""
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    head.lags_commands = True
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    assert [command.get("hvac_mode") for command in head.unreported_commands] == ["cool"]
    await rig.person_mode("off")
    await rig.later(HELD)
    assert _enabled(rig) is False
    assert await rig.report_next_command()  # MXZ's "cool" with its 72, late
    assert head.mode == "cool"
    assert _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is True
    await rig.close()


def test_known_cost_a_late_write_of_the_shown_value_passes_the_writes_queued_after_it() -> None:
    """Known cost, docs/STATE-MACHINES.md SM-10 path (c), accepted (the owner's decision,
    Nov 17, 2026), shown on HeadWriteMemory alone. The head shows MXZ's 1. MXZ writes 1
    again, which counts no copy (the value the head shows, with nothing else queued), then
    0, then 1, all still queued at a head that shows writes late. A person presses 5. The
    first 1 lands late. The memory takes it as the later 1 and ends the 0 queued between
    them, so when the 0 lands it reads as a person's press. If that 0 is adopted, the
    memory forgets the later 1 too, so it also reads as a person's when it lands.

    MXZ sees only the head's reports: its 1, a person's 5, then 1 again, a value it wrote
    after the 0. A report of a later write ends the writes before it
    (test_a_report_of_the_last_write_ends_all_the_others), and a press of a value the head
    has passed is a person's (test_a_press_of_a_write_the_head_has_passed_is_adopted).
    Nothing in the reports tells the late first 1 from the later 1: a head that took the
    first 1 at once showed nothing for it. No rule that reads only reports can give both
    answers. It is the second case that
    test_no_write_still_queued_is_forgotten_but_by_a_press_of_its_value doesn't check."""
    memory = HeadWriteMemory(8)
    memory.wrote(1, 0)
    memory.caught_up(1)  # the head shows MXZ's 1
    assert memory.wrote(1, 1) is False  # the value it shows, nothing else queued: no copy
    memory.wrote(0, 1)
    memory.wrote(1, 1)
    memory.seen(5)  # a person's press
    assert memory.own(0) and memory.own(1)
    memory.caught_up(1)  # the first 1, late
    assert not memory.own(0)
    assert memory.own(1)
    memory.seen(0)  # the 0 lands and is adopted as a person's
    assert not memory.own(1)


def test_a_rewrite_of_the_shown_value_behind_another_write_is_kept() -> None:
    """docs/STATE-MACHINES.md SM-10, shape B (fix round 5 of smfix1111): path (a)'s case
    reached by another write order. The head shows MXZ's 1. MXZ writes 0, then 1, then 1
    again, all still queued at a head that shows writes late. The last 1 is a write of the
    value the head shows, but the 0 is still to come, so the head will show something new
    for it. It counts a copy. After the 0 and the first 1 land, a person presses 5, and the
    last 1, still queued, stays MXZ's: when it lands it isn't read as a person's press.

    The oracle's example:
    [('write', 1), ('land', None), ('write', 0), ('write', 1), ('write', 1), ('land', None),
    ('land', None), ('press', 5)]"""
    memory = HeadWriteMemory(8)
    memory.wrote(1, 0)
    memory.caught_up(1)  # the head shows MXZ's 1
    memory.wrote(0, 1)
    memory.wrote(1, 1)
    assert memory.wrote(1, 1) is True  # the 0 is still to come: the head will show this 1
    memory.caught_up(0)
    memory.caught_up(1)  # the first 1
    memory.seen(5)  # a person's press
    assert memory.own(1)  # the last 1 is still queued
    memory.caught_up(1)  # it lands
    assert memory.values == (1,)


def test_known_cost_a_late_write_of_the_shown_value_uses_up_the_later_writes_copy() -> None:
    """Known cost, docs/STATE-MACHINES.md SM-10 path (c), shape A, accepted under the
    overnight release mandate (Nov 17, 2026), shown on HeadWriteMemory alone. The head shows
    MXZ's 1. MXZ writes 1 again, which counts no copy (the value the head shows, and all
    that is remembered). A person presses 5. MXZ writes 1 again, which counts a copy. Both
    1s are still queued at a head that shows writes late. The first 1 lands late. The memory
    takes it as the later 1 and uses up that write's copy. A person presses 5 again, and the
    memory forgets the later 1, which is still queued: when it lands, it reads as a person's.

    MXZ sees only the head's reports: its 1, a person's 5, 1 again, a person's 5. A head
    that took the first 1 at once showed nothing for it and sends the same reports, but
    there the 1 is the later write, now shown, and a person's next press of 1 must be
    adopted. No rule that reads only reports can give both answers. It is the case
    test_no_write_still_queued_is_forgotten_but_by_a_press_of_its_value stops checking at,
    until the queue is empty."""
    memory = HeadWriteMemory(8)
    memory.wrote(1, 0)
    memory.caught_up(1)  # the head shows MXZ's 1
    assert memory.wrote(1, 1) is False  # the value it shows, all that is remembered: no copy
    memory.seen(5)  # a person's press
    assert memory.wrote(1, 5) is True
    memory.caught_up(1)  # the first 1, late
    assert memory.record() == [[1, 0]]  # the later 1's copy is used up
    memory.seen(5)  # a person's press
    assert not memory.own(1)  # the later 1, still queued, is forgotten


def test_known_cost_a_rewrite_of_the_shown_value_behind_another_write_takes_a_press_back_once() -> None:
    """Known cost (F1's, docs/formal/PARKED-FINDINGS.md), accepted under the overnight
    release mandate (Nov 17, 2026), shown on HeadWriteMemory alone. It is the price of
    test_a_rewrite_of_the_shown_value_behind_another_write_is_kept. The head shows "cool".
    MXZ sends it off and then on, and a setpoint write then sends "cool" once more, all
    before the head shows the off. That last "cool" rewrites the value the head shows,
    with MXZ's off queued before it, so it counts a copy. The head shows the off, then the
    on, and shows nothing new for the rewrite, so no report counts that copy off. A person
    then turns the head off and on again at its remote: the memory takes the person's "on"
    as MXZ's, once. The next off and on are a person's.

    This is the cost test_a_press_back_to_a_rewritten_value_reads_as_mxzs_once pins, where
    the head showed another value while MXZ wrote twice. It is not free: that one press of
    the person's is not adopted. It is narrow: it needs MXZ's own rewrite of the shown value
    with another value of MXZ's queued before it, and it takes only a later person's press
    of that same value, once."""
    memory = HeadWriteMemory(8)
    memory.wrote("off", "cool")
    memory.wrote("cool", "cool")
    assert memory.wrote("cool", "cool") is True  # MXZ's off is queued before it: a copy
    memory.caught_up("off")
    memory.caught_up("cool")  # the head shows the on, and nothing new for the rewrite
    assert memory.record() == [["cool", 1]]
    memory.seen("off")  # a person's off at the head
    assert memory.own("cool")  # the person's on reads as MXZ's
    memory.caught_up("cool")
    memory.seen("off")  # the person's next off
    assert not memory.own("cool")  # and the next on is a person's


def test_a_rewrite_of_the_shown_last_write_after_a_restart_adds_no_copy() -> None:
    """Review 6 of smfix1111, on HeadWriteMemory: a restart restores the head's "off" and
    MXZ's one queued "cool". The head showed that "cool" while MXZ was down, and MXZ writes
    "cool" again before the head reports anything. It is the last value remembered and no
    value before it counts a copy, so nothing of MXZ's is still to come: it counts none.
    After the head's report, a person's off and then on are both a person's."""
    memory = HeadWriteMemory.restored(8, [["off", 0], ["cool", 1]])
    assert memory is not None
    assert memory.wrote("cool", "cool") is False
    memory.caught_up("cool")  # the head's first report after the start
    assert memory.record() == [["cool", 0]]
    memory.seen("off")  # a person's off
    assert not memory.own("cool")  # the person's on is a person's


def _refuses_the_first_setpoint(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The first head refuses MXZ's first setpoint call (a cloud timeout, say), then takes them."""
    head = rig.heads[0]
    set_temperature = type(head).async_set_temperature
    refused: list[dict[str, Any]] = []

    async def write(**kwargs: Any) -> None:
        if not refused:
            refused.append(kwargs)
            raise HomeAssistantError("invented cloud timeout")
        await set_temperature(head, **kwargs)

    monkeypatch.setattr(head, "async_set_temperature", write)
    return refused


async def _refused_then_shown(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """MXZ's 72 is refused once, and the next apply's 72 shows at a head that doesn't lag."""
    refused = _refuses_the_first_setpoint(rig, monkeypatch)
    await _set_target(rig.hass, rig.eid("_primary_target"), 72)
    for _ in range(3):
        if _edges(rig) == (70, 72):
            break
        await rig.settle()
    assert [call["target_temp_high"] for call in refused] == [72]
    assert _edges(rig) == (70, 72)


async def _press_away_and_back(rig: Rig) -> None:
    """A person presses 66, then 72 again: both are adopted, and the head keeps the 72."""
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert _target(rig) == 66
    await _press(rig, low=70, high=72)
    assert _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == 72
    assert _edges(rig) == (70, 72)


async def test_a_refused_write_leaves_no_copy_to_take_a_later_press(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 3 of smfix1111: a head that refused a write never takes it, so no report can
    count that copy off. Kept, it read the press back to 72 as MXZ's and drove the head to 66."""
    rig = await _cooling(hass, freezer)
    await _refused_then_shown(rig, monkeypatch)
    await _press_away_and_back(rig)
    await rig.close()


async def test_a_refused_write_leaves_no_copy_in_the_store(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory a restart resumes holds no copy of the refused call either."""
    rig = await _cooling(hass, freezer)
    await _refused_then_shown(rig, monkeypatch)
    saved = await saved_section(hass, rig.entry, SECTION_HEAD_WRITES)
    assert saved[rig.heads[0].entity_id]["target_temp_high"] == [[72, 0]]
    await rig.restart()
    await rig.later(STARTUP_RECOVER_DELAY + 1)
    await _press_away_and_back(rig)
    await rig.close()


async def test_a_press_of_a_write_the_head_has_passed_is_adopted(hass: HomeAssistant, freezer: Any) -> None:
    """The memory is only as wide as the lag."""
    rig = await _cooling(hass, freezer)
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    await _set_target(hass, rig.eid("_primary_target"), 74)
    await rig.settle()
    assert rig.coord._head_writes[rig.heads[0].entity_id]["target_temp_high"].values == (74,)
    await _press(rig, low=70, high=72)
    await rig.later(HELD)
    assert _target(rig) == 72
    await rig.close()


async def test_up_and_back_are_both_adopted(hass: HomeAssistant, freezer: Any) -> None:
    """The press replaces the write before it, so a press back to that value is a press too."""
    rig = await _cooling(hass, freezer)
    await _press(rig, low=70, high=72)
    await rig.later(HELD)
    assert _target(rig) == 72
    await _press(rig, low=TARGET - 2, high=TARGET)
    assert _armed(rig) is True
    await rig.later(HELD)
    assert _target(rig) == TARGET
    await rig.close()


async def test_off_and_on_and_off_at_the_head_each_count(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await rig.person_mode("off")
    await rig.later(HELD)
    assert _enabled(rig) is False
    await rig.person_mode("cool")
    await rig.later(HELD)
    assert _enabled(rig) is True
    await rig.person_mode("off")
    await rig.later(HELD)
    assert _enabled(rig) is False
    await rig.close()


async def test_off_and_on_at_the_head_count_after_target_changes(hass: HomeAssistant, freezer: Any) -> None:
    """Each setpoint write also sends the mode the head shows; that mode is no write to wait for."""
    rig = await _cooling(hass, freezer)
    for target in (72, 73, 72):
        await _set_target(hass, rig.eid("_primary_target"), target)
        await rig.settle()
        assert _edges(rig) == (target - 2, target)
    await rig.person_mode("off")
    await rig.later(HELD)
    assert _enabled(rig) is False
    await rig.person_mode("cool")
    await rig.later(HELD)
    assert _enabled(rig) is True
    assert rig.heads[0].mode == "cool"
    await rig.close()


async def test_the_memory_rides_a_reload_with_the_head_still_behind(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    head.lags_commands = True
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    await _set_target(hass, rig.eid("_primary_target"), 74)
    await rig.settle()
    await rig.reload()
    while await rig.report_next_command():
        assert not _armed(rig)
        await rig.later(HELD)
        assert _target(rig) == 74
    await rig.close()


async def test_a_late_own_write_inside_startup_quiet_is_not_adopted(
    hass: HomeAssistant, freezer: Any,
) -> None:
    """Startup quiet and the stored memory both keep it out; the next test is past the window."""
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    head.lags_commands = True
    await _set_target(hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    await _set_target(hass, rig.eid("_primary_target"), 74)
    await rig.settle()
    await rig.restart()
    started = rig.now()
    assert await rig.report_next_command()
    assert _edges(rig)[1] == 72
    await rig.later(HELD)
    assert _target(rig) == 74
    while await rig.report_next_command():
        pass
    await rig.later(HELD)
    assert _target(rig) == 74
    assert rig.now() - started < STARTUP_RECOVER_DELAY
    await rig.close()


async def _two_writes_queued_then_restart(rig: Rig, wait: float) -> None:
    """MXZ writes 72, then 74, to a lagging head; a restart; ``wait`` seconds pass."""
    rig.heads[0].lags_commands = True
    await _set_target(rig.hass, rig.eid("_primary_target"), 72)
    await rig.settle()
    await _set_target(rig.hass, rig.eid("_primary_target"), 74)
    await rig.settle()
    await rig.restart()
    await rig.later(wait)


@pytest.mark.parametrize(
    "wait", [STARTUP_RECOVER_DELAY + 1, 3600], ids=["after_startup_quiet", "an_hour_later"],
)
async def test_a_pre_restart_write_shown_late_is_not_adopted(
    hass: HomeAssistant, freezer: Any, wait: float,
) -> None:
    """docs/STATE-MACHINES.md SM-1: the room state store keeps MXZ's memory of its writes."""
    rig = await _cooling(hass, freezer)
    await rig.person_fan("quiet")
    assert rig.fan_held() is True
    await _two_writes_queued_then_restart(rig, wait)
    assert await rig.report_next_command()
    assert _edges(rig)[1] == 72
    assert not _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == 74
    while await rig.report_next_command():
        assert not _armed(rig)
        await rig.later(HELD)
        assert _target(rig) == 74
    assert _edges(rig)[1] == 74
    assert rig.fan_held() is True
    assert rig.fan_auto_switch() == "off"
    assert rig.heads[0].fan == "quiet"
    await rig.close()


async def test_a_press_after_a_restart_is_adopted(hass: HomeAssistant, freezer: Any) -> None:
    """The stored memory is MXZ's writes only: a person's press after the start is a press."""
    rig = await _cooling(hass, freezer)
    await _two_writes_queued_then_restart(rig, STARTUP_RECOVER_DELAY + 1)
    await _press(rig, low=64, high=66)
    assert _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == 66
    while await rig.report_next_command():
        await rig.later(HELD)
        assert _target(rig) == 66
    await rig.close()


async def _on_shown_while_mxz_was_down(rig: Rig, copies: int = 1) -> None:
    """A person's off is adopted. MXZ then turns the room on and sends "cool" ``copies``
    times to a head that shows writes late, and the head shows the first "cool" while MXZ
    is down. After the start, a target of 73 is one setpoint write, which sends "cool" too,
    before the head has reported anything. The queue drains, the head shows MXZ's writes
    at once from then on, and startup quiet ends.

    The switch's own compute sends one "cool". A second takes more applies, and how many
    one settle runs depends on Home Assistant's refresh scheduling
    (``_rewritten_while_lagging``): settle until two are queued, never past it."""
    head = rig.heads[0]
    await rig.person_mode("off")
    await rig.later(HELD)
    assert _enabled(rig) is False
    head.lags_commands = True
    await rig.hass.services.async_call(
        "switch", "turn_on", {"entity_id": rig.eid("_primary_enable")}, blocking=True,
    )
    await rig.hass.async_block_till_done()  # the switch's own compute, and no other
    for _ in range(2):
        if len(head.unreported_commands) >= copies:
            break
        await rig.settle()
    assert [command.get("hvac_mode") for command in head.unreported_commands] == ["cool"] * copies
    saved = await saved_section(rig.hass, rig.entry, SECTION_HEAD_WRITES)
    assert saved[head.entity_id]["hvac_mode"] == [["off", 0], ["cool", copies]]
    async with stopped(rig.hass, rig.entry):
        assert head.report_next_command()  # the head shows MXZ's first "cool"
    assert head.mode == "cool"
    queued = len(head.unreported_commands)
    await _set_target(rig.hass, rig.eid("_primary_target"), 73)
    assert [
        (command.get("hvac_mode"), command.get("target_temp_high")) for command in head.unreported_commands[queued:]
    ] == [("cool", 73)]
    while await rig.report_next_command():
        pass
    head.lags_commands = False
    await rig.later(STARTUP_RECOVER_DELAY + 1)
    assert _enabled(rig) is True
    assert head.mode == "cool"


async def test_after_a_restart_a_rewrite_of_the_shown_mode_takes_no_later_press(
    hass: HomeAssistant, freezer: Any,
) -> None:
    """docs/STATE-MACHINES.md SM-10 and SM-1, review 6 of smfix1111: MXZ's "cool" was its
    last write, and the head showed it while MXZ was down. The setpoint write after the
    start sends "cool" again, the value the head shows, with nothing else of MXZ's still to
    come. It counts no copy. A person's off and then on at the head are both adopted.

    Fix round 5's rule counted a copy there, because the memory still held the "off" the
    head showed before. The head showed nothing new for it, so the person's "on" read as
    MXZ's: the room stayed off and MXZ turned the head back off."""
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    await _on_shown_while_mxz_was_down(rig)
    await rig.person_mode("off")
    assert _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is False
    assert head.mode == "off"
    await rig.person_mode("cool")
    assert _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is True
    assert head.mode == "cool"
    await rig.close()


async def test_known_cost_two_copies_of_the_mode_across_a_restart_take_a_press_back_once(
    hass: HomeAssistant, freezer: Any,
) -> None:
    """Known cost (F1's, docs/formal/PARKED-FINDINGS.md), the one
    test_a_press_back_to_a_rewritten_value_reads_as_mxzs_once pins for a setpoint. It
    predates fix round 6 of smfix1111: this test passes unchanged on fix round 4's and
    fix round 5's wrote(). Review 6 named it.

    As in test_after_a_restart_a_rewrite_of_the_shown_mode_takes_no_later_press, but MXZ
    sent "cool" twice before the restart. The head shows the first while MXZ is down and
    the second after the start, with nothing new to show for it, so MXZ still counts that
    copy. (The setpoint write after the start counts none: the shown "cool" is the last
    value remembered, with nothing before it still to come.) A person's off at the head is
    adopted. Their "on" reads as MXZ's copy, once: the room stays off and MXZ turns the
    head back off. Their next "on" is adopted. It is not free: that one press of the
    person's is not adopted."""
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    await _on_shown_while_mxz_was_down(rig, copies=2)
    await rig.person_mode("off")
    assert _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is False
    assert head.mode == "off"
    await rig.person_mode("cool")
    assert not _armed(rig)  # read as MXZ's copy
    await rig.later(HELD)
    assert _enabled(rig) is False
    assert head.mode == "off"  # MXZ turned it back off
    await rig.person_mode("cool")
    assert _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is True
    assert head.mode == "cool"
    await rig.close()


@given(
    steps=st.lists(
        st.one_of(
            st.tuples(st.just("target"), st.sampled_from([66.0, 68.0, 70.0, 72.0, 74.0])),
            st.tuples(st.just("temp"), st.sampled_from([TARGET, HOT, COLD])),
            st.tuples(st.just("eco"), st.booleans()),
            st.tuples(st.just("report"), st.integers(1, 3)),
        ),
        max_size=10,
    ),
)
@SEQUENCES
async def test_no_person_no_adoption_however_a_head_lags(
    hass: HomeAssistant, freezer: Any, steps: list[tuple[str, Any]],
) -> None:
    """Whatever MXZ writes and however late the head shows it in order, the room never moves."""
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    head.lags_commands = True
    target = TARGET
    for kind, value in steps:
        if kind == "target":
            target = value
            await _set_target(hass, rig.eid("_primary_target"), value)
            await rig.settle()
        elif kind == "temp":
            await rig.temp(0, value)
        elif kind == "eco":
            await rig.switch("_eco_idle", value)
        else:
            for _ in range(value):
                await rig.report_next_command()
        assert not _armed(rig), steps
        await rig.later(HELD)
        assert _target(rig) == target, steps
        assert _enabled(rig) is True, steps
    await rig.switch("_eco_idle", False)
    while await rig.report_next_command():
        await rig.later(HELD)
    assert _target(rig) == target, steps
    assert _enabled(rig) is True, steps
    await rig.close()


# The memory itself, directly: the rule, with no entry around it.
_WRITES = st.lists(st.integers(0, 6), max_size=12)


@given(
    actions=st.lists(st.one_of(st.integers(0, 6).map(lambda v: ("write", v)), st.just(("report", None))), max_size=30),
)
@PURE
def test_every_report_of_a_head_showing_writes_in_order_reads_as_own(actions: list[tuple[str, Any]]) -> None:
    """The invariant against an independent queue: the head shows its writes in order."""
    memory = HeadWriteMemory(64)
    shown = 100
    queue: list[int] = []
    for kind, value in actions:
        if kind == "write":
            if not memory.values or memory.values[-1] != value:
                queue.append(value)
            memory.wrote(value, shown)
        elif queue:
            shown = queue.pop(0)
            assert memory.own(shown), (actions, memory.values)
            memory.caught_up(shown)
            assert memory.own(shown)
        assert memory.own(shown) or not memory.values


@given(writes=_WRITES)
@PURE
def test_a_value_never_written_or_shown_is_not_own(writes: list[int]) -> None:
    memory = HeadWriteMemory(64)
    for value in writes:
        memory.wrote(value, value - 1 if value else None)
    assert not memory.own(99)


@given(writes=st.lists(st.integers(0, 6), min_size=1, unique=True))
@PURE
def test_a_report_of_the_last_write_ends_all_the_others(writes: list[int]) -> None:
    memory = HeadWriteMemory(64)
    for value in writes:
        memory.wrote(value)
    memory.caught_up(writes[-1])
    assert memory.values == (writes[-1],)


def test_a_rewrite_rolls_nothing_and_a_write_twice_stays_until_ruled_out() -> None:
    memory = HeadWriteMemory(64)
    for value in (1, 2, 2, 2, 3, 1):
        memory.wrote(value)
    assert memory.values == (1, 2, 3, 1)
    memory.caught_up(1)  # the earliest 1: the writes after it may still come
    assert memory.values == (1, 2, 3, 1)
    memory.caught_up(3)
    assert memory.values == (3, 1)
    memory.caught_up(1)
    assert memory.values == (1,)


def test_a_person_replaces_what_the_head_last_showed_not_what_is_still_coming() -> None:
    memory = HeadWriteMemory(64)
    memory.wrote(1, 0)
    assert memory.values == (0, 1)
    memory.seen(9)
    assert memory.values == (9, 1)
    assert not memory.own(0)
    memory.wrote(2, 8)  # shown isn't remembered: the same again
    assert memory.values == (8, 1, 2)


@given(
    actions=st.lists(
        st.one_of(
            st.integers(0, 4).map(lambda v: ("write", v)),
            st.just(("land", None)),
            st.integers(5, 9).map(lambda v: ("press", v)),
        ),
        max_size=30,
    ),
)
@example(actions=[("write", 1), ("land", None), ("write", 1), ("write", 0), ("write", 1), ("press", 5), ("land", None)])
@example(actions=[("write", 1), ("land", None), ("write", 1), ("press", 5), ("write", 1), ("land", None), ("press", 5)])
@example(actions=[("write", 2), ("land", None), ("write", 2), ("write", 0), ("write", 0), ("write", 2), ("write", 0),
                  ("press", 5), ("land", None), ("land", None)])
@example(actions=[("write", 1), ("land", None), ("write", 1), ("press", 5), ("write", 0), ("write", 1), ("land", None),
                  ("land", None), ("write", 0), ("press", 5)])
@example(actions=[("write", 1), ("land", None), ("write", 0), ("write", 1), ("write", 1), ("land", None), ("land", None),
                  ("press", 5)])
@PURE
def test_no_write_still_queued_is_forgotten_but_by_a_press_of_its_value(actions: list[tuple[str, Any]]) -> None:
    """SM-10 as far as a report can tell: against an independent queue, a head shows its
    writes in order, a copy of the value it shows lands with nothing new to show, and a
    person presses only values MXZ never wrote (a press of a queued value is SM-10 path (b)).

    Some queued writes are not checked. All of them are part of SM-10's accepted known
    cost, path (c) (the owner's decision, Nov 17, 2026, and the overnight release mandate,
    Nov 17, 2026):

    - A write of the value the head shows, with only that value queued. A head that takes
      it at once shows nothing for it, so a kept copy would never be counted off and would
      take a person's next press of that value (review 2 of smfix1111). If it lands late,
      after a person's press, it reads as a person's:
      test_known_cost_a_write_of_the_shown_value_landing_after_a_press_is_adopted.
    - Every write still queued when such a write lands late, and every write made after
      that, until the queue is empty. "Late" means it lands after a person's press, off
      the value the head shows, while a later write of the same value is still queued. The
      memory takes it as that later write: it ends the writes queued between them, as a
      report of a later write must (test_a_report_of_the_last_write_ends_all_the_others),
      and it uses up that later write's copy. From then on the memory and the head's queue
      are out of step until the queue drains. Those writes can read as a person's when
      they land:
      test_known_cost_a_late_write_of_the_shown_value_passes_the_writes_queued_after_it and
      test_known_cost_a_late_write_of_the_shown_value_uses_up_the_later_writes_copy.

    Every other queued write is checked. The examples, in order: the writes between the late
    write and the later one (fix round 3); the later write itself (shape A, fix round 4); two
    that seed 12 found in fix round 4, with writes still queued and made after the late
    landing; and shape B, which no exemption covers
    (test_a_rewrite_of_the_shown_value_behind_another_write_is_kept). Random runs found each
    of them only under some seeds."""
    memory = HeadWriteMemory(64)
    shown = 100
    queue: list[tuple[int, bool]] = []  # each write, and whether a report can count it off
    adrift = False  # from a late landing of an unchecked write until the queue is empty
    for kind, value in actions:
        if kind == "write":
            unseen = adrift or (value == shown and all(queued == shown for queued, _ in queue))
            memory.wrote(value, shown)
            queue.append((value, not unseen))
        elif kind == "land" and queue:
            landed, checked = queue.pop(0)
            if not checked and landed != shown and landed in (queued for queued, _ in queue):
                adrift = True
                queue[:] = [(queued, False) for queued, _ in queue]
            adrift = adrift and bool(queue)
            if landed != shown:
                memory.caught_up(landed)
                shown = landed
        elif kind == "press" and value != shown:
            memory.seen(value)
            shown = value
        assert all(memory.own(queued) for queued, checked in queue if checked), (actions, memory.values, queue)


def test_a_write_of_what_the_head_shows_adds_no_copy() -> None:
    """Every setpoint write sends the mode the head shows too; no report will count that off."""
    memory = HeadWriteMemory(8)
    memory.wrote("cool", "cool")
    memory.wrote("cool", "cool")
    assert memory.record() == [["cool", 0]]
    memory.seen("off")
    assert not memory.own("cool")
    memory = HeadWriteMemory(8)
    memory.wrote(70, 72)
    memory.wrote(72, 72)  # behind the 70 still to come, the head shows it again
    assert memory.record() == [[72, 0], [70, 1], [72, 1]]
    memory = HeadWriteMemory(8)
    memory.wrote(72, 70)
    memory.wrote(72, 70)  # the head shows 70: both copies of 72 are still to come
    assert memory.record() == [[70, 0], [72, 2]]


def test_a_report_that_moves_nothing_shows_no_copy() -> None:
    """A fan report, say, while the head shows MXZ's write: that copy may still be coming."""
    memory = HeadWriteMemory(8)
    memory.wrote(1)
    memory.caught_up(1, moved=False)
    memory.seen(9)
    assert memory.values == (9, 1)
    memory = HeadWriteMemory(8)
    memory.wrote(1)
    memory.caught_up(1)
    memory.seen(9)
    assert memory.values == (9,)


def test_the_memory_is_bounded() -> None:
    memory = HeadWriteMemory(3)
    for value in range(10):
        memory.wrote(value)
    assert memory.values == (7, 8, 9)
    assert not memory.own(6)


def test_a_setpoint_compares_on_the_heads_grid() -> None:
    near = lambda reported, written: abs(reported - written) < 0.5
    memory = HeadWriteMemory(8)
    memory.wrote(64.0, None, near)
    memory.wrote(66.0, 64.2, near)
    assert memory.values == (64.0, 66.0)  # 64.2 is 64 on this grid: nothing new to remember
    memory.caught_up(66.3, near)
    assert memory.values == (66.0,)
    assert not memory.own(65.0, near)


# ---------------------------------------------------------------------------
# Safety floors and ceilings, and the limits of a setpoint
# ---------------------------------------------------------------------------


async def test_the_setpoint_limits_win_over_a_press(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    low, high = rig.coord.heads.grid.target_bounds(rig.heads[0].entity_id)
    await rig.temp(0, high + 5)  # still cooling at the highest target, so it is written
    await _press(rig, low=high + 3, high=high + 5)
    await rig.later(HELD)
    assert _target(rig) == high
    assert _edges(rig)[1] == high
    await _press(rig, low=low - 5, high=low - 3)
    await rig.later(HELD)
    assert _target(rig) == low
    assert _edges(rig)[1] == low
    await rig.close()


@pytest.mark.parametrize(
    ("key", "limit", "reading", "mode", "edge"),
    [(ZONE_FLOOR_LOW, 50.0, 45.0, "heat", 0), (ZONE_FLOOR_HIGH, 90.0, 95.0, "cool", 1)],
    ids=["freeze-floor", "overheat-ceiling"],
)
async def test_a_safety_limit_still_runs_a_room_turned_off_at_its_head(
    hass: HomeAssistant, freezer: Any, key: str, limit: float, reading: float, mode: str, edge: int,
) -> None:
    """Off at the head disables the room; its limit runs anyway, and that run isn't a person."""
    rig = await _cooling(hass, freezer, first_room={key: limit})
    await rig.person_mode("off")
    await rig.later(HELD)
    assert _enabled(rig) is False
    await rig.temp(0, reading)
    await rig.later(HOLD)
    head = rig.heads[0]
    assert head.mode == mode
    assert rig.coord.floor.tripped("primary") is True
    clear_line = _edges(rig)[edge]
    assert not _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is False  # MXZ's run didn't re-enable the room
    assert _target(rig) == TARGET
    # A person at the head during the run moves nothing either: the head is MXZ's until it ends.
    await _press(rig, low=clear_line + 6, high=clear_line + 8)
    assert not _armed(rig)
    await rig.later(HELD)
    assert _target(rig) == TARGET
    assert _enabled(rig) is False
    assert _edges(rig)[edge] == clear_line
    await _press(rig, mode="cool" if mode == "heat" else "heat")
    assert not _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is False
    assert head.mode == mode
    await rig.close()


# ---------------------------------------------------------------------------
# Dual-setpoint rooms, °F and °C, a head's grid
# ---------------------------------------------------------------------------


def _range(rig: Rig) -> tuple[float, float]:
    states = rig.hass.states
    return (
        float(states.get(rig.eid("_primary_target_low")).state),
        float(states.get(rig.eid("_primary_target_high")).state),
    )


async def test_a_dual_room_moves_cool_to_in_cool_pushing_heat_to_at_the_gap(
    hass: HomeAssistant, freezer: Any,
) -> None:
    rig = await open_rig(hass, freezer, temps=(HOT, TARGET), first_room={ZONE_DUAL_SETPOINT: True})
    assert rig.heads[0].mode == "cool"
    low, high = _range(rig)
    gap = rig.coord.dual_min_gap
    await _press(rig, low=low - 8, high=low - 6)
    await rig.later(HELD)
    assert _range(rig) == (low - 6 - gap, low - 6)
    assert rig.heads[0].mode == "cool"
    assert high != low - 6
    await rig.close()


async def test_a_dual_room_moves_heat_to_in_heat(hass: HomeAssistant, freezer: Any) -> None:
    rig = await open_rig(hass, freezer, temps=(COLD, TARGET), first_room={ZONE_DUAL_SETPOINT: True})
    await rig.select_mode("heat")
    assert rig.heads[0].mode == "heat"
    low, high = _range(rig)
    await _press(rig, low=low - 2, high=low)
    await rig.later(HELD)
    assert _range(rig) == (low - 2, high)
    await rig.close()


async def test_a_celsius_system_adopts_on_its_half_degree_step(hass: HomeAssistant) -> None:
    """°C in, °C on the number: the target snaps to the step of the unit it is saved in."""
    hass.config.units = METRIC_SYSTEM
    head, entry = await _mock_rooms(hass, MockHeadC, (26.0, 21.0), 21.0)
    assert hass.states.get(head).state == "cool"
    assert hass.states.get(head).attributes["target_temp_high"] == 21.0
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": head, "target_temp_low": 22.0, "target_temp_high": 23.3}, blocking=True,
    )
    await _elapse(hass, HELD)
    assert float(hass.states.get(_eid(hass, entry, "_primary_target")).state) == 23.5
    await _recompute(hass, entry)
    assert hass.states.get(head).attributes["target_temp_high"] == 23.5


async def test_a_unit_switch_keeps_the_target_and_adopts_nothing(hass: HomeAssistant, freezer: Any) -> None:
    """The head's report in the new unit differs from its last one. No person touched it."""
    rig = await _cooling(hass, freezer)
    assert await rig.hass.config.async_update(unit_system="metric") is None
    await hass.async_block_till_done()
    await rig.settle()
    assert rig.coord.temp_unit == UnitOfTemperature.CELSIUS
    target = _target(rig)
    assert target == 21
    await rig.later(HELD)
    assert _target(rig) == target
    assert _enabled(rig) is True
    # What's remembered in °F isn't compared with a report in °C (and see the next test).
    remembered = (await saved_section(hass, rig.entry, SECTION_HEAD_WRITES))[rig.heads[0].entity_id]
    assert remembered["target_temp_high"]
    assert not {TARGET - 2, TARGET} & {
        value for name, entries in remembered.items() if name != "hvac_mode" for value, _ in entries
    }
    await rig.close()


async def test_writes_remembered_in_one_unit_dont_resume_in_the_other(hass: HomeAssistant, freezer: Any) -> None:
    """The store keeps the memory in °F; a start in °C begins without it. The Coordinator is
    off, so nothing written after the start hides what resumed."""
    rig = await _cooling(hass, freezer)
    head = rig.heads[0].entity_id
    await rig.switch("_coordinator_enable", False)
    await rig.reload()
    saved = await saved_section(hass, rig.entry, SECTION_HEAD_WRITES)
    assert [value for value, _ in saved[head]["target_temp_high"]] == [TARGET]
    assert await rig.hass.config.async_update(unit_system="metric") is None
    await hass.async_block_till_done()
    assert rig.coord.temp_unit == UnitOfTemperature.CELSIUS
    assert await saved_section(hass, rig.entry, SECTION_HEAD_WRITES) == {}
    await rig.close()


async def test_a_head_on_its_own_grid_echoes_its_write_and_a_press_a_step_away_is_adopted(
    hass: HomeAssistant,
) -> None:
    """A °C CN105 in a °F system: 64 °F comes back as 64.4 °F, and that isn't a person."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head, entry = await _mock_rooms(hass, CN105Head, (HOT, TARGET), 64)
    await _recompute(hass, entry)
    shown = hass.states.get(head).attributes["target_temp_high"]
    assert shown == 64.5  # on its grid and displayed at half a °F
    await _elapse(hass, HELD)
    assert float(hass.states.get(_eid(hass, entry, "_primary_target")).state) == 64
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": head, "target_temp_low": 62.6, "target_temp_high": 65.3}, blocking=True,
    )
    await _elapse(hass, HELD)
    assert float(hass.states.get(_eid(hass, entry, "_primary_target")).state) == 65


# ---------------------------------------------------------------------------
# Restart continuity and honest state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("start", ["reload", "restart"])
async def test_an_adopted_change_survives_a_start(hass: HomeAssistant, freezer: Any, start: str) -> None:
    rig = await _cooling(hass, freezer)
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    await rig.person_mode("off")
    await rig.later(HELD)
    assert (_target(rig), _enabled(rig)) == (66, False)
    await (rig.reload() if start == "reload" else rig.restart())
    assert (_target(rig), _enabled(rig)) == (66, False)
    assert rig.heads[0].mode == "off"
    await rig.later(HELD)
    assert (_target(rig), _enabled(rig)) == (66, False)
    await rig.close()


@pytest.mark.parametrize("start", ["reload", "restart"])
async def test_a_start_inside_the_hold_drops_the_change(hass: HomeAssistant, freezer: Any, start: str) -> None:
    """Nothing about a hold is saved: the head goes back on the plan, the room hasn't moved."""
    rig = await _cooling(hass, freezer)
    await _press(rig, low=64, high=66)
    await (rig.reload() if start == "reload" else rig.restart())
    await rig.later(HELD)
    assert _target(rig) == TARGET
    assert _edges(rig) == (TARGET - 2, TARGET)
    await rig.close()


async def test_a_target_set_in_home_assistant_during_the_hold_wins(hass: HomeAssistant, freezer: Any) -> None:
    rig = await _cooling(hass, freezer)
    await _press(rig, low=64, high=66)
    await _set_target(hass, rig.eid("_primary_target"), 72)
    assert not _armed(rig)
    await rig.settle()
    assert _edges(rig) == (70, 72)
    await rig.later(HELD)
    assert _target(rig) == 72
    await rig.close()


@pytest.mark.parametrize("enabled", [True, False])
async def test_a_room_switched_in_home_assistant_during_the_hold_wins(
    hass: HomeAssistant, freezer: Any, enabled: bool,
) -> None:
    """A head off, or a disabled room's head on, then the room's own switch the other way."""
    rig = await _cooling(hass, freezer)
    if not enabled:
        await rig.switch("_primary_enable", False)
    await _press(rig, mode="off" if enabled else "cool")
    assert _armed(rig) is True
    await rig.switch("_primary_enable", enabled)
    assert not _armed(rig)
    await rig.later(HELD)
    assert _enabled(rig) is enabled
    assert rig.heads[0].mode == ("cool" if enabled else "off")
    await rig.close()


async def test_the_rooms_public_state_shows_the_adopted_change(hass: HomeAssistant, freezer: Any) -> None:
    """The thermostat and the plan sensor say what the room now is."""
    rig = await _cooling(hass, freezer)
    await _press(rig, low=64, high=66)
    await rig.later(HELD)
    assert rig.row()["target"] == 66
    room = hass.states.get(rig.eid("_primary_thermostat"))
    assert room.attributes["temperature"] == 66
    await rig.close()


# ---------------------------------------------------------------------------
# Cloud and generic heads
# ---------------------------------------------------------------------------


async def _cloud(hass: HomeAssistant, *, offset: float | None) -> tuple[ComfortHead, MockConfigEntry]:
    hass.config.units = US_CUSTOMARY_SYSTEM
    head = ComfortHead("a", mode=HVACMode.COOL, temperature=68.0)
    other = MockHead("b")
    await register(hass, {COMFORT: [head], "esphome": [other]})
    extra: dict[str, Any] = {ZONE_HEAD_TYPE: "cloud"}
    if offset is not None:
        extra[ZONE_DRIVE_OFFSET] = offset
    entry = await setup_rooms(hass, [(head, extra), (other, {})], temps=(HOT, TARGET))
    await _set_target(hass, _eid(hass, entry, "_primary_target"), TARGET)
    await past_startup(hass, entry)
    return head, entry


def _cloud_target(hass: HomeAssistant, entry: MockConfigEntry) -> float:
    return float(hass.states.get(_eid(hass, entry, "_primary_target")).state)


async def test_a_cloud_heads_driven_setpoint_is_mxz_s_but_its_off_is_adopted(hass: HomeAssistant) -> None:
    """Its setpoint runs past the target by the drive offset, so a press there stays MXZ's."""
    head, entry = await _cloud(hass, offset=None)
    assert head.device["temperature"] == TARGET - 2
    past_window(entry, head.entity_id)
    head.device["temperature"] = 74.0
    head.poll()
    await hass.async_block_till_done()
    await _elapse(hass, HELD)
    assert _cloud_target(hass, entry) == TARGET
    past_window(entry, head.entity_id)
    await recompute(hass, entry)
    assert head.device["temperature"] == TARGET - 2
    past_window(entry, head.entity_id)
    head.device["mode"] = HVACMode.OFF
    head.poll()
    await hass.async_block_till_done()
    await _elapse(hass, HELD)
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "off"


async def test_a_cloud_head_with_no_offset_adopts_like_any_other(hass: HomeAssistant) -> None:
    head, entry = await _cloud(hass, offset=0.0)
    assert head.device["temperature"] == TARGET
    past_window(entry, head.entity_id)
    head.device["temperature"] = 66.0
    head.poll()
    await hass.async_block_till_done()
    await _elapse(hass, HELD)
    assert _cloud_target(hass, entry) == 66


async def test_a_cloud_heads_startup_reports_are_not_adopted(hass: HomeAssistant) -> None:
    """In a new coordinator's confirm wait any report may be a poll from before it."""
    head, entry = await _cloud(hass, offset=0.0)
    assert await hass.config_entries.async_reload(entry.entry_id)  # a new coordinator's wait
    await hass.async_block_till_done()
    assert HELD < cloud_record(entry.runtime_data, head.entity_id).confirm_wait
    head.device["mode"] = HVACMode.OFF
    head.poll()
    await hass.async_block_till_done()
    await _elapse(hass, HELD)
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "on"


async def test_a_cloud_heads_older_mode_in_its_window_is_not_adopted(hass: HomeAssistant) -> None:
    head, entry = await _cloud(hass, offset=0.0)
    calls = record(hass)
    await _set_temp(hass, SENSOR_A, TARGET)
    past_window(entry, head.entity_id)
    await recompute(hass, entry)
    assert ("set_hvac_mode", {"hvac_mode": "fan_only"}) in writes(calls, head.entity_id)
    head.report(mode=HVACMode.OFF)  # never MXZ's: a real change, window or not
    await hass.async_block_till_done()
    await _elapse(hass, HELD)
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "off"


async def _queued_behind_a_gesture(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, *, fan_mode: str = "low", target: float | None = None,
) -> Callable[[], Awaitable[None]]:
    """A fan pick on the room's thermostat holds the head's handler open, and a plan's
    command to the head queues behind it, for a new ``target`` set in Home Assistant if
    one is given. Returns what lets the handler finish."""
    hass, head = rig.hass, rig.heads[0]
    entered, release = asyncio.Event(), asyncio.Event()
    fan = type(head).async_set_fan_mode

    async def slow(fan_mode: str) -> None:
        entered.set()
        await release.wait()
        await fan(head, fan_mode)

    monkeypatch.setattr(head, "async_set_fan_mode", slow)
    gesture = hass.async_create_task(hass.services.async_call(
        "climate", "set_fan_mode",
        {"entity_id": rig.eid("_primary_thermostat"), "fan_mode": fan_mode}, blocking=True,
    ))
    while not entered.is_set():
        await asyncio.sleep(0)
    if target is not None:
        await hass.services.async_call(
            "number", "set_value", {"entity_id": rig.eid("_primary_target"), "value": target},
            blocking=True,
        )
    refresh = hass.async_create_task(rig.coord.async_refresh())
    for _ in range(20):
        await asyncio.sleep(0)
    assert refresh.done()

    async def done() -> None:
        release.set()
        await gesture
        await hass.async_block_till_done()

    return done


async def test_a_press_while_mxzs_command_waits_for_the_head_is_adopted(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command was planned before the press: it waits, and the press wins (SM-6)."""
    rig = await _cooling(hass, freezer)
    done = await _queued_behind_a_gesture(rig, monkeypatch)
    rig.heads[0].person_setpoint(low=64, high=66)
    await done()
    assert _edges(rig) == (64, 66)
    await rig.later(HELD)
    assert _target(rig) == 66
    assert _edges(rig) == (64, 66)
    await rig.close()


async def test_a_flip_alone_while_mxzs_command_waits_is_put_back_by_it(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative: a change with nothing to adopt holds nothing, so the waiting command still goes."""
    rig = await _cooling(hass, freezer)
    done = await _queued_behind_a_gesture(rig, monkeypatch)
    rig.heads[0].person_setpoint(mode="heat")
    await done()
    assert rig.heads[0].mode == "cool"
    assert _target(rig) == TARGET
    await rig.close()


async def _settle_without_a_compute(rig: Rig, seconds: int) -> None:
    """Time passes one second at a time, and only Home Assistant's own timers run."""
    for _ in range(seconds):
        rig.freezer.tick(1)
        async_fire_time_changed(rig.hass, dt_util.utcnow())
        await rig.hass.async_block_till_done()


async def _taken_in(rig: Rig) -> None:
    """Let the head's report reach MXZ without waiting on the held handler."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize("taken_back", ["before the command's turn", "after the command's turn", None])
async def test_a_press_taken_back_in_its_hold_leaves_the_room_target_to_reach_the_head(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, taken_back: str | None,
) -> None:
    """SM-6's disown, undone: a press taken back leaves nothing adopted, so the plan owes the head
    the room's new target. No compute is forced once the command is queued, so MXZ must wake itself.

    ``None`` is the negative: a press that stays is adopted, and the waiting command never goes.
    It also shows the press arms the hold in this setup, so the two cases above take it back.
    """
    rig = await _cooling(hass, freezer)
    head = rig.heads[0]
    new = TARGET + 4
    # Past any refresh cooldown, so no deferred refresh is left to wake MXZ by chance.
    await _settle_without_a_compute(rig, 60)
    done = await _queued_behind_a_gesture(rig, monkeypatch, fan_mode=head.fan, target=new)
    assert rig.row()["command_deferred"], "no command is waiting for the head"
    assert _edges(rig) == (TARGET - 2, TARGET)
    head.person_setpoint(low=TARGET - 6, high=TARGET - 4)
    await _taken_in(rig)
    if taken_back == "before the command's turn":
        head.person_setpoint(low=TARGET - 2, high=TARGET)
        await _taken_in(rig)
    await done()
    if taken_back == "after the command's turn":
        assert _edges(rig) == (TARGET - 6, TARGET - 4)  # the disowned command sent nothing
        head.person_setpoint(low=TARGET - 2, high=TARGET)
        await hass.async_block_till_done()
    await _settle_without_a_compute(rig, 300)
    if taken_back is None:
        assert _target(rig) == TARGET - 4
        assert _edges(rig) == (TARGET - 6, TARGET - 4)
    else:
        assert _target(rig) == new
        assert _edges(rig) == (new - 2, new)
    await rig.close()
