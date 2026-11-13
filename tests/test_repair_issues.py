"""Repair issues: raised only after a sustained fault, cleared with it, and never in control.

Every room, head, and sensor here is invented.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mxz_coordinator import issues as issues_module
from custom_components.mxz_coordinator.const import (
    CONF_CHANGEOVER_ENTITY,
    CONF_INHIBIT_ENTITY,
    CONF_ZONES,
    DOMAIN,
    HEAD_TYPE_GENERIC,
    ZONE_CLIMATE,
    ZONE_COMFORT_PRIORITY_ENTITY,
    ZONE_HEAD_TYPE,
    ZONE_NAME,
    ZONE_OPENING_ENTITY,
    ZONE_OPENING_PAUSE,
    ZONE_SAFETY_PRIORITY_ENTITY,
    ZONE_SENSOR,
    ZONE_STAGE_SENSOR,
    ZONE_VANE_HORIZONTAL,
    ZONE_VANE_VERTICAL,
)
from custom_components.mxz_coordinator.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.mxz_coordinator.issues import (
    FAULT_DELETED,
    FAULT_HEAD,
    FAULT_SENSOR,
    LEARN_MORE_URL,
    issue_id,
)
from tests.test_door_window import _setup as _setup_rig
from tests.test_startup_continuity import _head

HEAD = "climate.study"
OTHER_HEAD = "climate.den"
SENSOR = "sensor.study_temp"
OTHER_SENSOR = "sensor.den_temp"
WINDOW = "binary_sensor.study_window"
VANE = "select.study_vane"

# The approved wait, written out so a changed constant fails here.
WAIT = 15 * 60
JUST_BEFORE = WAIT - 1
JUST_AFTER = WAIT + 1


def _mxz_issues(hass: HomeAssistant) -> set[str]:
    """Every MXZ issue in the registry: tests compare the whole set, so a double is seen."""
    return {known for domain, known in ir.async_get(hass).issues if domain == DOMAIN}


def _entry_of(hass: HomeAssistant) -> MockConfigEntry:
    return hass.config_entries.async_entries(DOMAIN)[0]


def _id(hass: HomeAssistant, fault: str, entity_id: str) -> str:
    return issue_id(fault, _entry_of(hass).entry_id, entity_id)


async def _later(hass: HomeAssistant, seconds: float) -> None:
    """Fire whatever is due this far from now (a timer's own clock does not advance)."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _setup(
    hass: HomeAssistant,
    *,
    study: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    states: dict[str, str] | None = None,
) -> MockConfigEntry:
    """Study and Den, disabled, on plain states with no registry entries."""
    for entity_id, value in {
        HEAD: "off",
        OTHER_HEAD: "off",
        SENSOR: "21",
        OTHER_SENSOR: "21",
        **(states or {}),
    }.items():
        hass.states.async_set(entity_id, value, {"unit_of_measurement": "°C"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        version=2,
        minor_version=2,
        data={
            CONF_ZONES: [
                {ZONE_NAME: "Study", ZONE_CLIMATE: HEAD, ZONE_SENSOR: SENSOR, **(study or {})},
                {ZONE_NAME: "Den", ZONE_CLIMATE: OTHER_HEAD, ZONE_SENSOR: OTHER_SENSOR},
            ],
            **(options or {}),
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _remove_state(hass: HomeAssistant, entity_id: str) -> None:
    hass.states.async_remove(entity_id)
    await hass.async_block_till_done()


async def _set(hass: HomeAssistant, entity_id: str, value: str) -> None:
    hass.states.async_set(entity_id, value, {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()


async def _delete(hass: HomeAssistant, entity_id: str) -> None:
    """Delete a registry-backed entity: it leaves the registry, then the state machine."""
    er.async_get(hass).async_remove(entity_id)
    await hass.async_block_till_done()
    if hass.states.get(entity_id) is not None:
        await _remove_state(hass, entity_id)


def _register(hass: HomeAssistant, entity_id: str) -> None:
    domain, name = entity_id.split(".")
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(domain, "invented", name, suggested_object_id=name)
    assert entry.entity_id == entity_id


class _Timers:
    """Stands in for ``async_call_later`` with a clock that adds up.

    ``async_fire_time_changed`` is not cumulative: a timer armed after a jump is
    still timed from the loop's real now, so the jump counts against it too. Telling
    a restarted wait from one that kept running needs a clock that really moves.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.pending: list[list[Any]] = []

    def call_later(self, _hass: HomeAssistant, delay: timedelta, job: Any) -> Any:
        timer = [self.now + delay.total_seconds(), job]
        self.pending.append(timer)
        return lambda: self.pending.remove(timer)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for timer in [t for t in self.pending if t[0] <= self.now]:
            self.pending.remove(timer)
            timer[1].target(None)


# -- a. a head, missing or unavailable ------------------------------------------
@pytest.mark.parametrize("gone", ["unavailable", "missing"])
async def test_a_head_raises_only_after_the_full_wait(hass: HomeAssistant, gone: str) -> None:
    await _setup(hass)
    if gone == "missing":
        await _remove_state(hass, HEAD)
    else:
        await _set(hass, HEAD, "unavailable")

    await _later(hass, JUST_BEFORE)
    assert _mxz_issues(hass) == set()

    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _id(hass, FAULT_HEAD, HEAD))
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == FAULT_HEAD
    assert issue.learn_more_url == LEARN_MORE_URL
    assert issue.translation_placeholders == {
        "entity": HEAD, "uses": "Study: head", "minutes": "15",
    }


async def test_a_head_blip_raises_nothing(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _set(hass, HEAD, "unavailable")
    await _later(hass, JUST_BEFORE)
    await _set(hass, HEAD, "cool")

    await _later(hass, WAIT * 3)

    assert _mxz_issues(hass) == set()


async def test_a_flapping_head_raises_nothing_and_each_recovery_restarts_the_wait(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)
    timers = _Timers()
    entry = _entry_of(hass)
    # Restart the monitor on the clock that adds up.
    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch.object(issues_module, "async_call_later", timers.call_later):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Down 80% of the wait, up a moment, for many times the wait in total.
        for _ in range(6):
            await _set(hass, HEAD, "unavailable")
            timers.advance(WAIT * 0.8)
            assert _mxz_issues(hass) == set()
            await _set(hass, HEAD, "cool")
            assert timers.pending == []
            timers.advance(1)
        assert _mxz_issues(hass) == set()

        # The wait that does count starts at the last failure, not any earlier one.
        await _set(hass, HEAD, "unavailable")
        timers.advance(JUST_BEFORE)
        assert _mxz_issues(hass) == set()
        timers.advance(2)
        assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}


async def test_unavailable_then_missing_is_one_unbroken_wait(hass: HomeAssistant) -> None:
    """Still not there, only in a different way: that is no recovery."""
    await _setup(hass)
    timers = _Timers()
    entry = _entry_of(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch.object(issues_module, "async_call_later", timers.call_later):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await _set(hass, HEAD, "unavailable")
        timers.advance(WAIT * 0.6)
        await _remove_state(hass, HEAD)
        timers.advance(WAIT * 0.3)
        assert _mxz_issues(hass) == set()
        timers.advance(WAIT * 0.2)
        assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}


async def test_a_head_issue_clears_when_it_recovers(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _set(hass, HEAD, "unavailable")
    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}

    await _set(hass, HEAD, "cool")

    assert _mxz_issues(hass) == set()


async def test_a_head_that_recovers_and_fails_again_waits_again(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _set(hass, HEAD, "unavailable")
    await _later(hass, JUST_AFTER)
    await _set(hass, HEAD, "cool")
    assert _mxz_issues(hass) == set()

    await _set(hass, HEAD, "unavailable")
    assert _mxz_issues(hass) == set()
    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}


@pytest.mark.parametrize("value", ["off", "cool", "heat", "fan_only", "unknown"])
async def test_a_head_with_a_state_is_no_fault(hass: HomeAssistant, value: str) -> None:
    await _setup(hass)
    await _set(hass, HEAD, value)

    await _later(hass, WAIT * 3)

    assert _mxz_issues(hass) == set()


async def test_a_head_on_plain_states_needs_no_registry_entry(hass: HomeAssistant) -> None:
    """Negative: a YAML-style entity with no registry entry is not missing."""
    await _setup(hass)
    assert er.async_get(hass).async_get(HEAD) is None

    await _later(hass, WAIT * 3)

    assert _mxz_issues(hass) == set()


async def test_an_entity_mxz_does_not_use_is_ignored(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    await _setup(hass)
    await _set(hass, "climate.not_ours", "cool")
    await _set(hass, "climate.not_ours", "unavailable")
    await _remove_state(hass, "climate.not_ours")

    await _later(hass, WAIT * 3)

    assert _mxz_issues(hass) == set()
    assert "Error" not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("gone", ["unavailable", "missing"])
async def test_a_generic_head_keeps_its_own_issue_and_gets_no_second(
    hass: HomeAssistant, freezer, gone: str
) -> None:
    """A generic head already has generic_head_unresponsive; one fault, one issue."""
    entry = await _setup(hass, study={ZONE_HEAD_TYPE: HEAD_TYPE_GENERIC})
    if gone == "missing":
        await _remove_state(hass, HEAD)
    else:
        await _set(hass, HEAD, "unavailable")

    freezer.tick(timedelta(seconds=JUST_AFTER))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _mxz_issues(hass) == {
        f"cloud_head_unresponsive_{entry.entry_id}_{HEAD}"
    }


async def test_a_deleted_generic_head_is_still_reported(hass: HomeAssistant) -> None:
    _register(hass, HEAD)
    await _setup(hass, study={ZONE_HEAD_TYPE: HEAD_TYPE_GENERIC})
    await _delete(hass, HEAD)

    await _later(hass, JUST_AFTER)

    assert _id(hass, FAULT_DELETED, HEAD) in _mxz_issues(hass)


# -- b. a room sensor, missing --------------------------------------------------
@pytest.mark.parametrize(
    ("sensor", "uses"),
    [
        (SENSOR, "Study: temperature sensor"),
        (WINDOW, "Study: door or window sensor"),
    ],
)
async def test_a_room_sensor_raises_only_after_the_full_wait(
    hass: HomeAssistant, sensor: str, uses: str
) -> None:
    await _setup(
        hass,
        study={ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_PAUSE: True},
        states={WINDOW: "off"},
    )
    await _remove_state(hass, sensor)

    await _later(hass, JUST_BEFORE)
    assert _mxz_issues(hass) == set()

    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_SENSOR, sensor)}
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _id(hass, FAULT_SENSOR, sensor))
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == FAULT_SENSOR
    assert issue.learn_more_url == LEARN_MORE_URL
    assert issue.translation_placeholders["uses"] == uses


@pytest.mark.parametrize("sensor", [SENSOR, WINDOW])
async def test_a_room_sensor_blip_raises_nothing(hass: HomeAssistant, sensor: str) -> None:
    await _setup(
        hass,
        study={ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_PAUSE: True},
        states={WINDOW: "off"},
    )
    await _remove_state(hass, sensor)
    await _later(hass, JUST_BEFORE)
    await _set(hass, sensor, "off" if sensor == WINDOW else "21")

    await _later(hass, WAIT * 3)

    assert _mxz_issues(hass) == set()


@pytest.mark.parametrize("sensor", [SENSOR, WINDOW])
async def test_a_room_sensor_issue_clears_when_it_returns(hass: HomeAssistant, sensor: str) -> None:
    await _setup(
        hass,
        study={ZONE_OPENING_ENTITY: WINDOW, ZONE_OPENING_PAUSE: True},
        states={WINDOW: "off"},
    )
    await _remove_state(hass, sensor)
    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_SENSOR, sensor)}

    await _set(hass, sensor, "off" if sensor == WINDOW else "21")

    assert _mxz_issues(hass) == set()


@pytest.mark.parametrize("value", ["unavailable", "unknown"])
async def test_an_offline_room_sensor_is_not_missing(hass: HomeAssistant, value: str) -> None:
    """Negative: it is there, offline, and sensor_health already says so."""
    await _setup(hass)
    await _set(hass, SENSOR, value)

    await _later(hass, WAIT * 3)

    assert _mxz_issues(hass) == set()


async def test_a_disabled_room_sensor_counts_as_missing(hass: HomeAssistant) -> None:
    """In the registry with no state: a person can fix that, and it isn't deleted."""
    _register(hass, SENSOR)
    await _setup(hass)
    er.async_get(hass).async_update_entity(SENSOR, disabled_by=er.RegistryEntryDisabler.USER)
    await _remove_state(hass, SENSOR)

    await _later(hass, JUST_AFTER)

    assert _mxz_issues(hass) == {_id(hass, FAULT_SENSOR, SENSOR)}


async def test_a_window_sensor_with_the_pause_off_is_only_checked_for_deletion(
    hass: HomeAssistant,
) -> None:
    """It does nothing while the pause is off, so disabling it is no fault."""
    _register(hass, WINDOW)
    await _setup(hass, study={ZONE_OPENING_ENTITY: WINDOW}, states={WINDOW: "off"})
    er.async_get(hass).async_update_entity(WINDOW, disabled_by=er.RegistryEntryDisabler.USER)
    await _remove_state(hass, WINDOW)

    await _later(hass, WAIT * 3)

    assert _mxz_issues(hass) == set()


# -- c. a configured entity that was deleted ------------------------------------
_OPTIONAL = [
    (ZONE_VANE_VERTICAL, VANE, "Study: vertical vane"),
    (ZONE_VANE_HORIZONTAL, "select.study_vane_h", "Study: horizontal vane"),
    (ZONE_STAGE_SENSOR, "sensor.study_stage", "Study: airflow sensor"),
    (ZONE_SAFETY_PRIORITY_ENTITY, "binary_sensor.study_safety", "Study: Safety priority sensor"),
    (ZONE_COMFORT_PRIORITY_ENTITY, "binary_sensor.study_comfort", "Study: Comfort priority"),
    (ZONE_OPENING_ENTITY, WINDOW, "Study: door or window sensor"),
]


@pytest.mark.parametrize(("role", "entity_id", "uses"), _OPTIONAL)
async def test_each_optional_reference_raises_when_deleted(
    hass: HomeAssistant, role: str, entity_id: str, uses: str
) -> None:
    _register(hass, entity_id)
    await _setup(hass, study={role: entity_id}, states={entity_id: "off"})
    await _delete(hass, entity_id)

    await _later(hass, JUST_BEFORE)
    assert _mxz_issues(hass) == set()

    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_DELETED, entity_id)}
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _id(hass, FAULT_DELETED, entity_id))
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == FAULT_DELETED
    assert issue.learn_more_url == LEARN_MORE_URL
    assert issue.translation_placeholders["uses"] == uses


@pytest.mark.parametrize(
    ("option", "entity_id", "uses"),
    [
        (CONF_CHANGEOVER_ENTITY, "weather.invented", "Seasons: weather source"),
        (CONF_INHIBIT_ENTITY, "binary_sensor.invented_hold", "Standby hold: hold signal"),
    ],
)
async def test_each_global_reference_raises_when_deleted(
    hass: HomeAssistant, option: str, entity_id: str, uses: str
) -> None:
    _register(hass, entity_id)
    await _setup(hass, options={option: entity_id}, states={entity_id: "off"})
    await _delete(hass, entity_id)

    await _later(hass, JUST_AFTER)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, _id(hass, FAULT_DELETED, entity_id))
    assert issue.translation_placeholders["uses"] == uses


@pytest.mark.parametrize("entity_id", [HEAD, SENSOR])
async def test_a_deleted_head_or_sensor_raises_only_the_deleted_issue(
    hass: HomeAssistant, entity_id: str
) -> None:
    _register(hass, entity_id)
    await _setup(hass)
    await _delete(hass, entity_id)

    await _later(hass, JUST_AFTER)

    assert _mxz_issues(hass) == {_id(hass, FAULT_DELETED, entity_id)}


async def test_deleting_a_real_head_entity_raises_it_end_to_end(hass: HomeAssistant, freezer) -> None:
    """A real entity on a real platform: HA itself removes the state with the registry entry."""
    rig = await _setup_rig(hass, freezer, study={})
    head = rig.heads[1]

    er.async_get(hass).async_remove(head)
    await hass.async_block_till_done()
    assert hass.states.get(head) is None
    await rig.later(JUST_AFTER)

    assert _mxz_issues(hass) == {_id(hass, FAULT_DELETED, head)}


async def test_a_deleted_blip_raises_nothing(hass: HomeAssistant) -> None:
    """An integration that removes and re-adds an entity is no fault."""
    _register(hass, VANE)
    await _setup(hass, study={ZONE_VANE_VERTICAL: VANE}, states={VANE: "auto"})
    await _delete(hass, VANE)
    await _later(hass, JUST_BEFORE)

    _register(hass, VANE)
    await _set(hass, VANE, "auto")
    await _later(hass, WAIT * 3)

    assert _mxz_issues(hass) == set()


async def test_a_deleted_issue_clears_when_the_entity_returns(hass: HomeAssistant) -> None:
    _register(hass, VANE)
    await _setup(hass, study={ZONE_VANE_VERTICAL: VANE}, states={VANE: "auto"})
    await _delete(hass, VANE)
    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_DELETED, VANE)}

    _register(hass, VANE)
    await _set(hass, VANE, "auto")

    assert _mxz_issues(hass) == set()


async def test_a_renamed_reference_is_reported_under_its_old_id(hass: HomeAssistant) -> None:
    """A rename leaves the config pointing at nothing, and it does not follow."""
    _register(hass, VANE)
    await _setup(hass, study={ZONE_VANE_VERTICAL: VANE}, states={VANE: "auto"})

    er.async_get(hass).async_update_entity(VANE, new_entity_id="select.study_vane_new")
    await _remove_state(hass, VANE)
    await _set(hass, "select.study_vane_new", "auto")
    await _later(hass, JUST_AFTER)

    assert _mxz_issues(hass) == {_id(hass, FAULT_DELETED, VANE)}


async def test_deleting_a_disabled_sensor_changes_the_issue_with_no_state_event(
    hass: HomeAssistant,
) -> None:
    """A disabled entity has no state to change, so only the registry says it went."""
    _register(hass, SENSOR)
    await _setup(hass)
    er.async_get(hass).async_update_entity(SENSOR, disabled_by=er.RegistryEntryDisabler.USER)
    await _remove_state(hass, SENSOR)
    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_SENSOR, SENSOR)}

    er.async_get(hass).async_remove(SENSOR)
    await hass.async_block_till_done()

    assert _mxz_issues(hass) == {_id(hass, FAULT_DELETED, SENSOR)}


async def test_a_head_issue_turns_into_the_deleted_one_at_once(hass: HomeAssistant) -> None:
    _register(hass, HEAD)
    await _setup(hass)
    await _set(hass, HEAD, "unavailable")
    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}

    await _delete(hass, HEAD)

    assert _mxz_issues(hass) == {_id(hass, FAULT_DELETED, HEAD)}


async def test_a_deletion_is_still_recognized_after_a_reload(hass: HomeAssistant) -> None:
    """The evidence is the registry's own record, not this monitor's memory."""
    _register(hass, SENSOR)
    entry = await _setup(hass)
    await _delete(hass, SENSOR)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _later(hass, JUST_AFTER)

    assert _mxz_issues(hass) == {_id(hass, FAULT_DELETED, SENSOR)}


async def test_a_never_registered_sensor_is_missing_not_deleted(hass: HomeAssistant) -> None:
    """Negative for the deleted evidence: no registry record, so nothing says deleted."""
    await _setup(hass)
    await _remove_state(hass, SENSOR)

    await _later(hass, JUST_AFTER)

    assert _mxz_issues(hass) == {_id(hass, FAULT_SENSOR, SENSOR)}


async def test_an_entity_used_twice_is_one_issue_naming_both_uses(hass: HomeAssistant) -> None:
    _register(hass, SENSOR)
    await _setup(hass, study={ZONE_STAGE_SENSOR: SENSOR})
    await _delete(hass, SENSOR)

    await _later(hass, JUST_AFTER)

    assert _mxz_issues(hass) == {_id(hass, FAULT_DELETED, SENSOR)}
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _id(hass, FAULT_DELETED, SENSOR))
    assert issue.translation_placeholders["uses"] == (
        "Study: temperature sensor; Study: airflow sensor"
    )


# -- lifecycle -------------------------------------------------------------------
async def test_unloading_clears_issues_and_stops_watching(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _set(hass, HEAD, "unavailable")
    await _later(hass, JUST_AFTER)
    await _remove_state(hass, SENSOR)
    assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _mxz_issues(hass) == set()

    await _later(hass, WAIT * 3)
    assert _mxz_issues(hass) == set()


async def test_unloading_drops_the_listener_the_monitor_added(hass: HomeAssistant) -> None:
    """One cycle with no monitor is the oracle: one more listener while loaded, none after."""
    def _state_listeners() -> int:
        return hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)

    with patch(
        "custom_components.mxz_coordinator.async_start_repair_monitor", lambda *_a: None
    ):
        entry = await _setup(hass)
        loaded_without = _state_listeners()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        unloaded_without = _state_listeners()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _state_listeners() == loaded_without + 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _state_listeners() == unloaded_without


async def test_removing_the_entry_clears_its_issues(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _set(hass, HEAD, "unavailable")
    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _mxz_issues(hass) == set()


async def test_removing_an_entry_that_is_not_loaded_clears_what_it_left(hass: HomeAssistant) -> None:
    """A previous run's issue in the registry, with no monitor left to clear it."""
    entry = MockConfigEntry(domain=DOMAIN, title="MXZ Coordinator", data={CONF_ZONES: []})
    entry.add_to_hass(hass)
    left = issue_id(FAULT_HEAD, entry.entry_id, HEAD)
    ir.async_create_issue(
        hass, DOMAIN, left, is_fixable=False, severity=ir.IssueSeverity.WARNING,
        translation_key=FAULT_HEAD,
    )
    unrelated = "shared_mode_legacy_option_kept"
    ir.async_create_issue(
        hass, DOMAIN, unrelated, is_fixable=False, severity=ir.IssueSeverity.WARNING,
        translation_key="shared_mode_legacy_option",
    )

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _mxz_issues(hass) == {unrelated}


async def test_unloading_one_entry_leaves_the_others_issues(hass: HomeAssistant) -> None:
    theirs_head = "climate.theirs"
    entry = await _setup(hass)
    hass.states.async_set("sensor.theirs_temp", "21", {"unit_of_measurement": "°C"})
    hass.states.async_set(theirs_head, "off")
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Other",
        version=2,
        minor_version=2,
        data={
            CONF_ZONES: [
                {ZONE_NAME: "Theirs", ZONE_CLIMATE: theirs_head, ZONE_SENSOR: "sensor.theirs_temp"},
            ],
        },
    )
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    await _set(hass, HEAD, "unavailable")
    await _set(hass, theirs_head, "unavailable")
    await _later(hass, JUST_AFTER)
    ours = issue_id(FAULT_HEAD, entry.entry_id, HEAD)
    theirs = issue_id(FAULT_HEAD, other.entry_id, theirs_head)
    assert _mxz_issues(hass) == {ours, theirs}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert _mxz_issues(hass) == {theirs}


async def test_a_reload_times_a_fault_again(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _set(hass, HEAD, "unavailable")
    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _mxz_issues(hass) == set()

    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}


async def test_a_fault_already_there_at_setup_raises_after_the_wait(hass: HomeAssistant) -> None:
    await _setup(hass, states={HEAD: "unavailable"})

    await _later(hass, JUST_BEFORE)
    assert _mxz_issues(hass) == set()

    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}


async def test_the_wait_starts_only_once_home_assistant_has_started(hass: HomeAssistant) -> None:
    """Everything is missing while HA boots, however long that takes."""
    hass.set_state(CoreState.not_running)
    await _setup(hass, states={HEAD: "unavailable"})

    await _later(hass, WAIT * 3)
    assert _mxz_issues(hass) == set()

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await _later(hass, JUST_AFTER)
    assert _mxz_issues(hass) == {_id(hass, FAULT_HEAD, HEAD)}


async def test_diagnostics_list_the_standing_issues(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _set(hass, HEAD, "unavailable")
    await _later(hass, JUST_AFTER)

    document = await async_get_config_entry_diagnostics(hass, entry)

    assert [issue["issue_id"] for issue in document["repair_issues"]] == [
        _id(hass, FAULT_HEAD, HEAD)
    ]
    assert document["repair_issues"][0]["severity"] == "warning"


# -- it stays out of control -----------------------------------------------------
_TRANSCRIPTS: dict[str, list[Any]] = {}


def _plan_view(rig: Any) -> dict[str, Any]:
    plan = rig.coord.data
    return {
        "state": plan.state,
        "standoff": plan.standoff,
        "rooms": [
            (room.name, room.demand, room.engage, room.vote_status, room.sensor_health)
            for room in plan.rooms
        ],
    }


@pytest.mark.parametrize("monitor", ["off", "on"])
async def test_control_is_identical_with_and_without_the_monitor(
    hass: HomeAssistant, freezer, monitor: str
) -> None:
    """The same faults, played with no monitor and with one: every call and plan match.

    The ``off`` run is the oracle, and runs first. The ``on`` run must also really
    have raised issues, or the comparison proves nothing.
    """
    if monitor == "off":
        patcher = patch(
            "custom_components.mxz_coordinator.async_start_repair_monitor", lambda *_a: None
        )
    else:
        patcher = patch(
            "custom_components.mxz_coordinator.async_start_repair_monitor",
            issues_module.async_start_repair_monitor,
        )
    transcript: list[Any] = []
    raised: list[set[str]] = []
    with patcher:
        rig = await _setup_rig(hass, freezer, study={}, temps=(76, 70))

        async def step() -> None:
            await rig.recompute()
            transcript.append((_plan_view(rig), list(rig.sent(0)), list(rig.sent(1))))
            raised.append(_mxz_issues(hass))
            rig.forget()

        await step()
        den = _head(hass, rig.heads[1])
        den._attr_available = False
        den.async_write_ha_state()
        await hass.async_block_till_done()
        await step()
        await rig.later(JUST_AFTER)
        await step()
        den._attr_available = True
        den.async_write_ha_state()
        await hass.async_block_till_done()
        await step()
        hass.states.async_remove("sensor.den_temp")
        await hass.async_block_till_done()
        await step()
        await rig.later(JUST_AFTER)
        await step()
        hass.states.async_set(
            "sensor.den_temp", "60", {"unit_of_measurement": hass.config.units.temperature_unit}
        )
        await hass.async_block_till_done()
        await step()

    if monitor == "off":
        assert not any(raised)
        _TRANSCRIPTS["off"] = transcript
        return
    assert any(raised), "the monitor raised nothing, so this compared nothing"
    assert transcript == _TRANSCRIPTS["off"]


async def test_a_monitor_that_cannot_start_costs_only_the_notices(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    with patch.object(issues_module, "_references", side_effect=RuntimeError("broken")):
        entry = await _setup(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.plan_ready
    assert "repair issue monitor did not start" in caplog.text
    assert await hass.config_entries.async_unload(entry.entry_id)


# -- the words -------------------------------------------------------------------
_COMPONENT = Path("custom_components/mxz_coordinator")


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json"])
@pytest.mark.parametrize("fault", [FAULT_HEAD, FAULT_SENSOR, FAULT_DELETED])
def test_every_fault_has_its_words(name: str, fault: str) -> None:
    text = json.loads((_COMPONENT / name).read_text(encoding="utf-8"))["issues"][fault]

    assert set(text) == {"title", "description"}
    placeholders = set(re.findall(r"{(\w+)}", text["title"] + text["description"]))
    assert placeholders == {"entity", "uses", "minutes"}
    assert "{entity}" in text["title"]
    assert "{uses}" in text["description"]


async def test_each_raised_issue_supplies_every_placeholder_its_words_use(
    hass: HomeAssistant,
) -> None:
    strings = json.loads((_COMPONENT / "strings.json").read_text(encoding="utf-8"))["issues"]
    _register(hass, VANE)
    await _setup(hass, study={ZONE_VANE_VERTICAL: VANE}, states={VANE: "auto"})
    await _set(hass, HEAD, "unavailable")
    await _remove_state(hass, SENSOR)
    await _delete(hass, VANE)
    await _later(hass, JUST_AFTER)

    registry = ir.async_get(hass)
    raised = [
        registry.async_get_issue(DOMAIN, known) for known in _mxz_issues(hass)
    ]
    assert {issue.translation_key for issue in raised} == {FAULT_HEAD, FAULT_SENSOR, FAULT_DELETED}
    for issue in raised:
        text = strings[issue.translation_key]
        used = set(re.findall(r"{(\w+)}", text["title"] + text["description"]))
        assert used <= set(issue.translation_placeholders)


def test_the_learn_more_link_lands_on_a_real_heading() -> None:
    url, anchor = LEARN_MORE_URL.split("#")
    manifest = json.loads((_COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    assert url == manifest["documentation"]
    headings = re.findall(r"^#+ (.+)$", Path("README.md").read_text(encoding="utf-8"), re.MULTILINE)
    assert anchor in [re.sub(r"[^\w -]", "", h).strip().lower().replace(" ", "-") for h in headings]


def test_issue_ids_are_stable_and_distinct() -> None:
    """A stored or dismissed issue must keep matching across versions."""
    assert issue_id(FAULT_HEAD, "E1", HEAD) == "head_unavailable_E1_climate.study"
    assert issue_id(FAULT_SENSOR, "E1", SENSOR) == "sensor_missing_E1_sensor.study_temp"
    assert issue_id(FAULT_DELETED, "E1", VANE) == "entity_deleted_E1_select.study_vane"
    assert issue_id(FAULT_HEAD, "E1", HEAD) != issue_id(FAULT_HEAD, "E2", HEAD)
    assert issue_id(FAULT_HEAD, "E1", HEAD) != issue_id(FAULT_DELETED, "E1", HEAD)
