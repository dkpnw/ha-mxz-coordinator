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


async def test_restored_drift_survives_and_clamps(hass: HomeAssistant) -> None:
    """A restored per-room drift lands (clamped into the profile bounds)."""
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import (
        mock_restore_cache_with_extra_data,
    )

    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)

    from homeassistant.const import UnitOfTemperature
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.mxz_coordinator.const import (
        CONF_FAN_BOOST_ENABLE,
        CONF_PRIMARY_CLIMATE,
        CONF_PRIMARY_SENSOR,
        CONF_SECONDARY_CLIMATE,
        CONF_SECONDARY_SENSOR,
        DOMAIN,
    )

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
    eid = "number.mxz_coordinator_primary_drift"
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(eid, "9.0"),  # above the °F bound (5.0) -> must clamp
                {
                    "native_max_value": 5.0,
                    "native_min_value": 0.5,
                    "native_step": 0.5,
                    "native_unit_of_measurement": UnitOfTemperature.FAHRENHEIT,
                    "native_value": 9.0,
                },
            )
        ],
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.zones[0].drift == 5.0  # clamped, not 9
