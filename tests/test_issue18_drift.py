"""Per-room drift (#18): a room's re-engage band, per zone, automatable."""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM  # noqa: E402

from tests.test_drive import (  # noqa: E402
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_temp,
    _setup_fan_boost,
    _setup_mock_heads,
)


async def _setup(hass):
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 62)   # primary target is 62 in the harness
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    return head_a, head_b, entry


async def _set_drift(hass, entry, value: float) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _eid(hass, entry, "_primary_drift"), "value": value},
        blocking=True,
    )
    await hass.async_block_till_done()


def _zone0(hass, entry):
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    return plan.attributes["zones"][0]


async def test_default_drift_matches_global_and_tracks_it(
    hass: HomeAssistant,
) -> None:
    """Untouched room: drift == the global engage_deadband, live, not a copy."""
    head_a, _b, entry = await _setup(hass)
    coord = entry.runtime_data
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == coord.engage_deadband
    # A live change to the global flows through (zone.drift is None).
    coord.engage_deadband = 2.0
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 2.0


async def test_widened_room_coasts_where_default_would_engage(
    hass: HomeAssistant,
) -> None:
    """Drift 4: a room 3° past target coasts; at default 1 it would re-engage."""
    head_a, _b, entry = await _setup(hass)
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["engage"] == "satisfied"  # at target, coasting

    await _set_drift(hass, entry, 4.0)
    await _set_temp(hass, SENSOR_A, 65)  # 3° past target 62
    await _recompute(hass, entry)
    z = _zone0(hass, entry)
    assert z["drift"] == 4.0
    assert z["engage"] == "satisfied"  # within its own band: still coasting
    assert z["demand"] == "neutral"    # and NOT steering the compressor

    await _set_temp(hass, SENSOR_A, 66.5)  # 4.5° past: beyond its band
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["engage"] == "cool"  # now it runs — to target


async def test_demand_respects_the_rooms_own_drift(hass: HomeAssistant) -> None:
    """A wide-tolerance room must not win a standoff inside its own band.

    Secondary calls heat; primary sits 3.5° warm with drift 4 — over the
    global demand threshold (3) but inside its own tolerance. The shared mode
    must follow the room that actually wants service.
    """
    head_a, head_b, entry = await _setup(hass)
    await _set_drift(hass, entry, 4.0)
    await _set_temp(hass, SENSOR_A, 65.5)  # 3.5° past cool target: in-band
    # Secondary: target 62 default? the harness sets primary target 62 only;
    # secondary target defaults to 70 -> drive it cold to call heat.
    await _set_temp(hass, SENSOR_B, 65)    # 5° below its 70 target -> heat
    entry.runtime_data._last_mode_change_ts = 0.0  # clear the 600 s flip gate
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["demand"] == "neutral"  # in-band: no vote
    assert plan.state == "heat"  # the caller wins; no standoff manufactured


async def test_tightening_reengages_immediately(hass: HomeAssistant) -> None:
    """The walk-in snap-back: occupied -> tight band -> conditioning resumes."""
    head_a, _b, entry = await _setup(hass)
    await _set_drift(hass, entry, 4.0)
    await _set_temp(hass, SENSOR_A, 65)  # 3° past target, coasting in-band
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["engage"] == "satisfied"

    await _set_drift(hass, entry, 1.0)   # presence detected: tighten
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["engage"] == "cool"  # re-engaged, runs to 62


async def test_drift_is_per_room(hass: HomeAssistant) -> None:
    """Widening one room leaves the other room's band untouched."""
    head_a, head_b, entry = await _setup(hass)
    await _set_drift(hass, entry, 4.0)
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["drift"] == 4.0
    assert (
        plan.attributes["zones"][1]["drift"]
        == entry.runtime_data.engage_deadband
    )


async def test_widening_mid_run_does_not_truncate_the_approach(
    hass: HomeAssistant,
) -> None:
    """An ENGAGED room runs to EXACTLY the target even if its drift widens.

    The band gates re-engagement only; widening it mid-run must not stop a
    room short of the number the user set (and must not reset the latch).
    """
    head_a, _b, entry = await _setup(hass)
    await _set_temp(hass, SENSOR_A, 68)  # 6° past target 62: engages
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["engage"] == "cool"

    await _set_drift(hass, entry, 5.0)   # vacation tier while mid-run
    await _set_temp(hass, SENSOR_A, 64)  # inside the new band, above target
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["engage"] == "cool"  # still running to 62

    await _set_temp(hass, SENSOR_A, 62)  # target reached
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["engage"] == "satisfied"


async def _restore_setup(hass, restored):
    """Set up a fresh entry with a seeded restore cache for the primary drift."""
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        mock_restore_cache_with_extra_data,
    )

    from custom_components.mxz_coordinator.const import (
        CONF_FAN_BOOST_ENABLE,
        CONF_PRIMARY_CLIMATE,
        CONF_PRIMARY_SENSOR,
        CONF_SECONDARY_CLIMATE,
        CONF_SECONDARY_SENSOR,
        DOMAIN,
    )

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
    entry.add_to_hass(hass)  # created FIRST -> fresh restore
    value, attrs = restored
    eid = "number.mxz_coordinator_primary_drift"
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(eid, str(value), attributes=attrs),
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


async def test_restored_drift_survives_and_clamps(hass: HomeAssistant) -> None:
    """A restored per-room override lands (clamped into the profile bounds)."""
    entry = await _restore_setup(hass, (9.0, {"override": True}))
    assert entry.runtime_data.zones[0].drift == 5.0  # clamped, not 9


async def test_untouched_room_restore_does_not_freeze_the_global(
    hass: HomeAssistant,
) -> None:
    """Restart must not turn yesterday's global into a per-room override.

    RestoreNumber saves the DISPLAYED value even for a room the user never
    touched. Only a state marked ``override`` may restore; otherwise the room
    keeps following the live global — and displays it truthfully.
    """
    entry = await _restore_setup(hass, (1.0, {"override": False}))
    coord = entry.runtime_data
    assert coord.zones[0].drift is None  # NOT frozen at the saved 1.0
    coord.engage_deadband = 2.0
    await _recompute(hass, entry)
    assert _zone0(hass, entry)["drift"] == 2.0  # follows the new global live
