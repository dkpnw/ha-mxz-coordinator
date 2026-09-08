"""Returning a room's drift to the global band (the way back from #18).

A room's drift number follows the global while the room has no override, and
every write to that number makes one. These cases pin the way back: the room's
follow-global button drops the override, and following is the ABSENCE of an
override rather than a copy of today's global number.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.mxz_coordinator.const import (
    CONF_ENGAGE_DEADBAND,
    CONF_FAN_BOOST_ENABLE,
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
    ZONE_ENTITY_SUFFIXES,
)
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_temp,
    _setup_fan_boost,
    _setup_mock_heads,
)
from tests.test_dwell_wakeup import _quiesce, _setup_dwell
from tests.test_idle_action import _settle_requested_refreshes
from tests.test_issue18_drift import _set_drift, _zone0
from tests.test_registry_lifecycle import _restart
from tests.test_room_rename_reorder import _reconfigure, _setup_rooms

# The primary room's two drift entities, by unique_id suffix. The button's
# suffix extends the number's, so it is resolved by the longer one first.
DRIFT = "_primary_drift"
FOLLOW = "_primary_drift_follow_global"


async def _setup(hass: HomeAssistant, *, celsius: bool = False):
    """Two rooms, coordinator on, primary target 62 °F (21 °C in metric)."""
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 17 if celsius else 62)
    await _set_temp(hass, SENSOR_B, 21 if celsius else 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    return head_a, head_b, entry


async def _press_follow_global(hass: HomeAssistant, entry, suffix=FOLLOW) -> None:
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _eid(hass, entry, suffix)},
        blocking=True,
    )
    await hass.async_block_till_done()


def _drift_state(hass: HomeAssistant, entry, suffix=DRIFT):
    return hass.states.get(_eid(hass, entry, suffix))


# -- the repro: today there is no way back ----------------------------------


async def test_return_to_global_makes_the_room_follow(hass: HomeAssistant) -> None:
    """Override -> press -> the room follows the live global again.

    Fails on the base for want of the affordance: no entity of this entry can
    clear a room's drift override, so a room that took one keeps it forever.
    """
    _a, _b, entry = await _setup(hass)
    coord = entry.runtime_data
    await _set_drift(hass, entry, 4.0)
    assert coord.zones[0].drift == 4.0
    assert _drift_state(hass, entry).attributes["override"] is True

    await _press_follow_global(hass, entry)

    # Stored as following, not as a copy of the global number.
    assert coord.zones[0].drift is None
    shown = _drift_state(hass, entry)
    assert float(shown.state) == coord.engage_deadband
    assert shown.attributes["override"] is False

    # And the next compute plans the room on the global band.
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == coord.engage_deadband

    # A later global change reaches it, which is what following means.
    coord.engage_deadband = 2.0
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 2.0


async def test_returned_room_demands_on_the_global_band(hass: HomeAssistant) -> None:
    """The returned room's demand vote uses the global band, not its old one.

    3° past a 62° target is inside a 4° override (no vote) and outside the 1°
    global (a cool vote). The vote is the observable difference.
    """
    _a, _b, entry = await _setup(hass)
    coord = entry.runtime_data
    await _set_drift(hass, entry, 4.0)
    await _set_temp(hass, SENSOR_A, 65)
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["engage"] == "satisfied"  # coasting in its band

    await _press_follow_global(hass, entry)
    await _recompute(hass, entry)
    z = _zone0(hass, entry)
    assert z["drift"] == coord.engage_deadband == 1.0
    assert z["engage"] == "cool"  # 3° past target is outside the global band


async def test_press_alone_recomputes(hass: HomeAssistant) -> None:
    """No other trigger: the press itself is what re-plans the room."""
    _a, _b, entry = await _setup(hass)
    coord = entry.runtime_data
    await _set_drift(hass, entry, 4.0)
    await _set_temp(hass, SENSOR_A, 65)
    await _recompute(hass, entry)
    await _settle_requested_refreshes(hass, coord)
    assert _zone0(hass, entry)["engage"] == "satisfied"

    await _press_follow_global(hass, entry)
    # Only the debounced refresh the press asked for runs here: no recompute
    # call, no sensor event, no timer of the test's own.
    assert await _settle_requested_refreshes(hass, coord) >= 1
    assert _zone0(hass, entry)["engage"] == "cool"


# -- following is not a number ----------------------------------------------


async def test_an_override_equal_to_the_global_is_still_an_override(
    hass: HomeAssistant,
) -> None:
    """Typing today's global into a room does NOT make the room follow it."""
    _a, _b, entry = await _setup(hass)
    coord = entry.runtime_data
    await _set_drift(hass, entry, coord.engage_deadband)  # 1.0, the global
    assert coord.zones[0].drift == 1.0
    assert _drift_state(hass, entry).attributes["override"] is True

    coord.engage_deadband = 2.0
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 1.0  # kept its own copy
    assert _zone0(hass, entry)["drift"] != coord.engage_deadband

    # The button is the difference: after it, the same room tracks 2.0.
    await _press_follow_global(hass, entry)
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 2.0


async def test_a_following_room_tracks_a_global_options_change(
    hass: HomeAssistant,
) -> None:
    """A follower shows a new global as soon as the options change lands."""
    _a, _b, entry = await _setup(hass)
    assert float(_drift_state(hass, entry).state) == 1.0

    hass.config_entries.async_update_entry(entry, options={CONF_ENGAGE_DEADBAND: 2.5})
    await hass.async_block_till_done()

    coord = entry.runtime_data
    assert coord.engage_deadband == 2.5
    assert coord.zones[0].drift is None
    shown = _drift_state(hass, entry)
    assert float(shown.state) == 2.5
    assert shown.attributes["override"] is False
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 2.5


async def test_an_override_ignores_a_global_options_change(
    hass: HomeAssistant,
) -> None:
    """The mirror case: an explicit override is never moved by the global."""
    _a, _b, entry = await _setup(hass)
    await _set_drift(hass, entry, 4.0)

    hass.config_entries.async_update_entry(entry, options={CONF_ENGAGE_DEADBAND: 2.5})
    await hass.async_block_till_done()

    coord = entry.runtime_data
    assert coord.engage_deadband == 2.5
    assert coord.zones[0].drift == 4.0
    shown = _drift_state(hass, entry)
    assert float(shown.state) == 4.0
    assert shown.attributes["override"] is True


# -- reload and restart ------------------------------------------------------


async def test_following_survives_a_reload(hass: HomeAssistant) -> None:
    """Override, return, reload: the room comes back following."""
    _a, _b, entry = await _setup(hass)
    await _set_drift(hass, entry, 4.0)
    await _press_follow_global(hass, entry)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    coord = entry.runtime_data
    assert coord.zones[0].drift is None
    assert _drift_state(hass, entry).attributes["override"] is False
    coord.engage_deadband = 2.0
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 2.0


async def test_an_override_survives_a_reload(hass: HomeAssistant) -> None:
    """Control for the reload case: an untouched override comes back."""
    _a, _b, entry = await _setup(hass)
    await _set_drift(hass, entry, 4.0)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    coord = entry.runtime_data
    assert coord.zones[0].drift == 4.0
    assert _drift_state(hass, entry).attributes["override"] is True


async def _restart_with(hass: HomeAssistant, value, attrs) -> MockConfigEntry:
    """Fresh entry whose primary drift number restores ``(value, attrs)``.

    The same seeding ``test_issue18_drift`` uses, so a state written by a
    release that has never heard of this button is restored verbatim.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: True,
        },
    )
    entry.add_to_hass(hass)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("number.mxz_coordinator_primary_drift", str(value), attributes=attrs),
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
    return entry


async def test_restart_restores_a_returned_room_as_following(
    hass: HomeAssistant,
) -> None:
    """A returned room saved ``override: False`` and restores as a follower."""
    entry = await _restart_with(hass, 1.0, {"override": False})
    coord = entry.runtime_data
    assert coord.zones[0].drift is None
    coord.engage_deadband = 2.0
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 2.0


async def test_restart_restores_an_override_as_an_override(
    hass: HomeAssistant,
) -> None:
    """Control: ``override: True`` still restores as an override."""
    entry = await _restart_with(hass, 4.0, {"override": True})
    coord = entry.runtime_data
    assert coord.zones[0].drift == 4.0
    coord.engage_deadband = 2.0
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 4.0


@pytest.mark.parametrize(
    ("value", "attrs", "expected"),
    [
        (4.0, {"override": True}, 4.0),      # pre-M29 override
        (1.0, {"override": False}, None),    # pre-M29 untouched room
        (9.0, {"override": True}, 5.0),      # pre-M29 override, clamped
        (1.0, {}, None),                     # pre-#18 state: no attribute at all
    ],
)
async def test_pre_m29_saved_states_restore_exactly_as_before(
    hass: HomeAssistant, value, attrs, expected
) -> None:
    """No new restore key: a state saved before this change loads unchanged."""
    entry = await _restart_with(hass, value, attrs)
    assert entry.runtime_data.zones[0].drift == expected


# -- per room, and nothing else ---------------------------------------------


async def test_press_on_an_untouched_room_is_a_consistent_no_op(
    hass: HomeAssistant,
) -> None:
    """Pressing a room that already follows leaves it following, truthfully."""
    _a, _b, entry = await _setup(hass)
    coord = entry.runtime_data
    before = _drift_state(hass, entry)
    assert before.attributes["override"] is False

    await _press_follow_global(hass, entry)

    assert coord.zones[0].drift is None
    after = _drift_state(hass, entry)
    assert float(after.state) == coord.engage_deadband
    assert after.attributes["override"] is False
    # Still following, not frozen on the value it was displaying.
    coord.engage_deadband = 2.0
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 2.0


async def test_press_leaves_the_other_room_alone(hass: HomeAssistant) -> None:
    """Per room only: one room's return does not touch its neighbour."""
    _a, _b, entry = await _setup(hass)
    coord = entry.runtime_data
    await _set_drift(hass, entry, 4.0)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _eid(hass, entry, "_secondary_drift"), "value": 3.0},
        blocking=True,
    )
    await hass.async_block_till_done()

    await _press_follow_global(hass, entry)

    assert coord.zones[0].drift is None
    assert coord.zones[1].drift == 3.0
    assert _drift_state(hass, entry, "_secondary_drift").attributes["override"] is True
    await _recompute(hass, entry)
    zones = hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"]
    assert [zones[0]["drift"], zones[1]["drift"]] == [coord.engage_deadband, 3.0]


async def test_press_does_not_reset_the_engage_latch(hass: HomeAssistant) -> None:
    """An engaged room still runs to EXACTLY its target after the press.

    The mirror of ``test_widening_mid_run_does_not_truncate_the_approach``:
    changing the band is not a reason to abandon a run already under way.
    """
    _a, _b, entry = await _setup(hass)
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 68)  # 6° past target 62: engages
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["engage"] == "cool"

    await _set_drift(hass, entry, 5.0)
    latched = dict(coord._engage_latch)
    assert latched[coord.zones[0].slug] == "cool"
    await _press_follow_global(hass, entry)
    # The latch itself, not just its effect: a target change clears it, a band
    # change does not, and returning to the global is a band change.
    assert dict(coord._engage_latch) == latched

    await _set_temp(hass, SENSOR_A, 62.5)  # past the 1° global, short of target
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["engage"] == "cool"  # still running to 62

    await _set_temp(hass, SENSOR_A, 62)
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["engage"] == "satisfied"


async def test_press_changes_no_other_setting(hass: HomeAssistant) -> None:
    """Target, room enable, fan hold and the shared mode are untouched."""
    _a, _b, entry = await _setup(hass)
    coord = entry.runtime_data
    await _set_drift(hass, entry, 4.0)
    await _recompute(hass, entry)
    before = {
        "target": coord.zones[0].target,
        "enable": coord.zones[0].enable,
        "fan_auto": coord.fan_auto_is_on(coord.zones[0].climate_id),
        "mode": coord.current_shared_mode,
        "target_state": hass.states.get(_eid(hass, entry, "_primary_target")).state,
    }

    await _press_follow_global(hass, entry)
    await _recompute(hass, entry)

    assert {
        "target": coord.zones[0].target,
        "enable": coord.zones[0].enable,
        "fan_auto": coord.fan_auto_is_on(coord.zones[0].climate_id),
        "mode": coord.current_shared_mode,
        "target_state": hass.states.get(_eid(hass, entry, "_primary_target")).state,
    } == before


# -- units, clamps and registry identity ------------------------------------


async def test_metric_room_returns_to_the_metric_global(hass: HomeAssistant) -> None:
    """°C symmetry: the same press, the °C global, the °C profile bounds."""
    entry, _a, _b = await _setup_dwell(hass, celsius=True)
    coord = entry.runtime_data
    assert coord.engage_deadband == 0.5
    await _set_drift(hass, entry, 2.0)
    assert coord.zones[0].drift == 2.0

    await _press_follow_global(hass, entry)

    assert coord.zones[0].drift is None
    shown = _drift_state(hass, entry)
    assert float(shown.state) == 0.5
    assert shown.attributes["override"] is False
    assert (shown.attributes["min"], shown.attributes["max"]) == (0.25, 2.5)
    coord.engage_deadband = 1.5
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 1.5


async def test_profile_clamps_are_unchanged_by_the_press(hass: HomeAssistant) -> None:
    """The °F drift number keeps its 0.5–5 band, and a clamp still clamps."""
    _a, _b, entry = await _setup(hass)
    coord = entry.runtime_data
    shown = _drift_state(hass, entry)
    assert (shown.attributes["min"], shown.attributes["max"]) == (0.5, 5.0)

    await _press_follow_global(hass, entry)

    shown = _drift_state(hass, entry)
    assert (shown.attributes["min"], shown.attributes["max"]) == (0.5, 5.0)
    # And an override outside the band is still clamped at read, as before —
    # a hand-edited or pre-upgrade value cannot collapse the coast window.
    coord.zones[0].drift = 9.0
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 5.0


async def test_one_config_category_button_per_room(hass: HomeAssistant) -> None:
    """Exactly one per room, on the device page, not a dashboard control."""
    _a, _b, entry = await _setup(hass)
    reg = er.async_get(hass)
    buttons = [
        ent
        for ent in er.async_entries_for_config_entry(reg, entry.entry_id)
        if ent.domain == "button"
    ]
    assert sorted(ent.unique_id for ent in buttons) == [
        f"{entry.entry_id}_primary_drift_follow_global",
        f"{entry.entry_id}_secondary_drift_follow_global",
    ]
    assert {ent.entity_category for ent in buttons} == {EntityCategory.CONFIG}


async def test_the_button_record_moves_with_its_room_on_a_reorder(
    hass: HomeAssistant,
) -> None:
    """The new per-room entity is in ZONE_ENTITY_SUFFIXES, so a reorder keeps it.

    A per-room record left out of that tuple is parked on the old priority
    slot and pruned on the next setup, losing the room's own name, area and
    disabled flag for that entity.
    """
    assert "drift_follow_global" in ZONE_ENTITY_SUFFIXES
    heads, sensors, entry = await _setup_rooms(hass)
    reg = er.async_get(hass)
    bedroom = _eid(hass, entry, "_primary_drift_follow_global")
    office = _eid(hass, entry, "_secondary_drift_follow_global")
    reg.async_update_entity(bedroom, name="Bedroom back to global")

    await _reconfigure(hass, entry, [heads[1], heads[0]], [sensors[1], sensors[0]])

    # Bedroom is now the SECOND room; its own record went with it, name and all.
    moved = reg.async_get(bedroom)
    assert moved.unique_id == f"{entry.entry_id}_secondary_drift_follow_global"
    assert moved.name == "Bedroom back to global"
    assert reg.async_get(office).unique_id == (
        f"{entry.entry_id}_primary_drift_follow_global"
    )


# -- r1 review corrections -------------------------------------------------


@pytest.mark.parametrize("return_action", ["press", "untouched_control"])
async def test_disabled_drift_restore(
    hass: HomeAssistant, return_action: str
) -> None:
    """A disabled persistence owner makes the action unavailable and durable."""
    _a, _b, entry = await _setup(hass)
    await _set_drift(hass, entry, 4)
    registry = er.async_get(hass)
    drift = _eid(hass, entry, DRIFT)
    button = _eid(hass, entry, FOLLOW)

    registry.async_update_entity(
        drift, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()

    assert hass.states.get(drift) is None
    assert hass.states.get(button).state == STATE_UNAVAILABLE
    assert entry.runtime_data.zones[0].drift == 4
    if return_action == "press":
        # Home Assistant filters unavailable entities before ordinary domain
        # service dispatch. Exercise the entity-service action itself as the
        # forced-invocation negative: its product guard must also refuse.
        entity = hass.data["entity_components"]["button"].get_entity(button)
        with pytest.raises(HomeAssistantError, match="disabled or missing"):
            await entity._async_press_action()
        assert entry.runtime_data.zones[0].drift == 4

    await _restart(hass, entry)
    assert hass.states.get(button).state == STATE_UNAVAILABLE

    registry.async_update_entity(drift, disabled_by=None)
    await hass.async_block_till_done()
    assert hass.states.get(button).state == STATE_UNAVAILABLE

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(button).state != STATE_UNAVAILABLE
    assert entry.runtime_data.zones[0].drift == 4
    assert float(_drift_state(hass, entry).state) == 4.0
    assert _drift_state(hass, entry).attributes["override"] is True


async def test_missing_drift_number_makes_the_button_unavailable(
    hass: HomeAssistant,
) -> None:
    """Removing the persistence owner's registry record closes the action."""
    _a, _b, entry = await _setup(hass)
    await _set_drift(hass, entry, 4)
    registry = er.async_get(hass)
    drift = _eid(hass, entry, DRIFT)
    button = _eid(hass, entry, FOLLOW)

    registry.async_remove(drift)
    await hass.async_block_till_done()

    assert registry.async_get(drift) is None
    assert hass.states.get(button).state == STATE_UNAVAILABLE
    entity = hass.data["entity_components"]["button"].get_entity(button)
    with pytest.raises(HomeAssistantError, match="disabled or missing"):
        await entity.async_press()
    assert entry.runtime_data.zones[0].drift == 4


async def test_press_can_change_shared_mode(hass: HomeAssistant) -> None:
    """Applying the global band can change demand, shared mode and the head."""
    entry, a, _b = await _setup_dwell(hass, **{CONF_MODE_HYSTERESIS: 0})
    coord = entry.runtime_data
    await _set_drift(hass, entry, 4)
    await _set_temp(hass, SENSOR_A, coord.zones[0].target - 3.5)
    await _recompute(hass, entry)
    await _quiesce(hass, coord)
    print(
        "DOC_PRE",
        [(zone.target, zone.drift) for zone in coord.zones],
        coord.data,
        coord.hysteresis,
    )
    assert coord.current_shared_mode == "cool"
    assert _zone0(hass, entry)["engage"] == "satisfied"
    await _press_follow_global(hass, entry)
    await _settle_requested_refreshes(hass, coord)
    print("PRESS_SHARED_MODE", "cool ->", coord.current_shared_mode, coord.data)
    assert coord.current_shared_mode == "heat"
    assert hass.states.get(a).state == "heat"


async def test_unchanged_refreshes_do_not_redraw_drift_numbers(
    hass: HomeAssistant,
) -> None:
    """Only a changed value/flag or a number write publishes drift state."""
    _a, _b, entry = await _setup(hass)
    coord = entry.runtime_data
    drift = _eid(hass, entry, DRIFT)
    entity = hass.data["entity_components"]["number"].get_entity(drift)

    with patch.object(entity, "async_write_ha_state") as write_state:
        for _ in range(10):
            await coord.async_refresh()
        assert write_state.call_count == 0

        coord.engage_deadband = 2.0
        coord.async_update_listeners()
        assert write_state.call_count == 1

        for _ in range(10):
            await coord.async_refresh()
        assert write_state.call_count == 1

        await entity.async_set_native_value(4.0)
        await _settle_requested_refreshes(hass, coord)
        assert write_state.call_count == 2

        for _ in range(10):
            await coord.async_refresh()
        assert write_state.call_count == 2


# -- r2 review corrections -------------------------------------------------


@pytest.mark.parametrize("wait_for_owner", [False, True])
async def test_review_reenable_press_durability(hass, wait_for_owner):
    _a, _b, entry = await _setup(hass)
    await _set_drift(hass, entry, 4.0)
    registry = er.async_get(hass)
    drift = _eid(hass, entry, DRIFT)
    button = _eid(hass, entry, FOLLOW)
    registry.async_update_entity(drift, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    assert hass.states.get(drift) is None
    assert hass.states.get(button).state == STATE_UNAVAILABLE
    registry.async_update_entity(drift, disabled_by=None)
    await hass.async_block_till_done()
    if wait_for_owner:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    number_present = hass.data["entity_components"]["number"].get_entity(drift) is not None
    shown_available = hass.states.get(button).state != STATE_UNAVAILABLE
    await _press_follow_global(hass, entry)
    accepted = entry.runtime_data.zones[0].drift is None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restored = entry.runtime_data.zones[0].drift
    print("REENABLE", {"wait_for_owner": wait_for_owner, "number_present": number_present,
                       "button_available": shown_available, "press_cleared": accepted,
                       "restored_override": restored})
    assert not accepted or restored is None, "Accepted return-to-global was undone on reload"
    assert number_present or not shown_available, "Button advertised before persistence owner returned"


@pytest.mark.parametrize("wait_for_owner", [False, True])
async def test_reenable_requires_a_ready_owner(hass: HomeAssistant, wait_for_owner: bool):
    """The gap refuses both paths; the reloaded owner makes following durable."""
    _a, _b, entry = await _setup(hass)
    await _set_drift(hass, entry, 4.0)
    registry = er.async_get(hass)
    drift = _eid(hass, entry, DRIFT)
    button = _eid(hass, entry, FOLLOW)
    registry.async_update_entity(drift, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    registry.async_update_entity(drift, disabled_by=None)
    await hass.async_block_till_done()
    assert registry.async_get(drift).disabled_by is None
    assert hass.data["entity_components"]["number"].get_entity(drift) is None
    assert hass.states.get(button).state == STATE_UNAVAILABLE

    if wait_for_owner:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.data["entity_components"]["number"].get_entity(drift) is not None
        assert float(hass.states.get(drift).state) == 4.0
        assert hass.states.get(button).state != STATE_UNAVAILABLE
    else:
        entity = hass.data["entity_components"]["button"].get_entity(button)
        coord = entry.runtime_data
        with (
            patch.object(coord, "async_update_listeners") as publish,
            patch.object(coord, "async_user_changed") as recompute,
            pytest.raises(HomeAssistantError, match="has not finished loading"),
        ):
            await entity._async_press_action()
        publish.assert_not_called()
        recompute.assert_not_called()
        assert coord.zones[0].drift == 4.0

    await _press_follow_global(hass, entry)
    assert entry.runtime_data.zones[0].drift == (None if wait_for_owner else 4.0)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.zones[0].drift == (None if wait_for_owner else 4.0)
    assert _drift_state(hass, entry).attributes["override"] is (not wait_for_owner)
    assert hass.states.get(button).state != STATE_UNAVAILABLE
    # Recovery is useful: a normal press now publishes and restores following.
    await _press_follow_global(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.zones[0].drift is None
    assert _drift_state(hass, entry).attributes["override"] is False


async def test_owner_state_lifecycle_updates_button(hass: HomeAssistant):
    """Readiness follows owner publication/removal, including a renamed ID."""
    _a, _b, entry = await _setup(hass)
    await _set_drift(hass, entry, 4.0)
    registry = er.async_get(hass)
    drift = _eid(hass, entry, DRIFT)
    button = _eid(hass, entry, FOLLOW)
    registry.async_update_entity(drift, new_entity_id="number.renamed_drift")
    await hass.async_block_till_done()
    drift = _eid(hass, entry, DRIFT)
    assert drift == "number.renamed_drift"
    owner = hass.data["entity_components"]["number"].get_entity(drift)
    assert owner is not None
    assert hass.states.get(button).state != STATE_UNAVAILABLE
    hass.states.async_remove(drift)
    await hass.async_block_till_done()
    assert hass.states.get(button).state == STATE_UNAVAILABLE
    entity = hass.data["entity_components"]["button"].get_entity(button)
    with pytest.raises(HomeAssistantError):
        await entity.async_press()
    assert entry.runtime_data.zones[0].drift == 4.0
    owner.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(button).state != STATE_UNAVAILABLE
    await owner.async_remove()
    await hass.async_block_till_done()
    assert registry.async_get(drift).disabled_by is None
    assert hass.data["entity_components"]["number"].get_entity(drift) is None
    assert hass.states.get(button).state == STATE_UNAVAILABLE
    with pytest.raises(HomeAssistantError):
        await entity.async_press()
    assert entry.runtime_data.zones[0].drift == 4.0
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(button).state != STATE_UNAVAILABLE
    assert entry.runtime_data.zones[0].drift == 4.0


async def test_button_restores_last_pressed_time_without_owning_drift(hass: HomeAssistant):
    """HA restores the press timestamp; the number restores the later override."""
    _a, _b, entry = await _setup(hass)
    await _press_follow_global(hass, entry)
    button = _eid(hass, entry, FOLLOW)
    pressed_at = hass.states.get(button).state
    assert "T" in pressed_at
    await _set_drift(hass, entry, 4.0)
    owner = hass.data["entity_components"]["number"].get_entity(_eid(hass, entry, DRIFT))
    # Seed a fresh process from the states saved while both entities were live.
    # An unavailable button state has no timestamp to restore in HA.
    saved = [
        (hass.states.get(button), None),
        (_drift_state(hass, entry), owner.extra_restore_state_data.as_dict()),
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    mock_restore_cache_with_extra_data(hass, saved)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(button).state == pressed_at
    assert entry.runtime_data.zones[0].drift == 4.0
    assert _drift_state(hass, entry).attributes["override"] is True
