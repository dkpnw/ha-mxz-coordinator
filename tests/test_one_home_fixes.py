"""Two saves that did not do what they said while a key had two homes (fixed by entry v2.3).

Before 2.3 the integration read ``{**data, **options}``, so a key held in options
shadowed the same key in data. Reconfigure wrote the rooms to data only, so on an
entry whose rooms were in options a Reconfigure saved and changed nothing. And
every Configure save scrubbed the 3.x flat vane keys, so saving any page on an
entry with one dropped that room's vane wiring. Both entries below are v2.2
entries of those shapes. Every head and sensor is invented.
"""

from __future__ import annotations

from copy import deepcopy

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import DOMAIN
from tests.flow_pages import open_options_page, prefilled, submit
from tests.test_setup_flow import (
    BEDROOM,
    BEDROOM_SENSOR,
    HALL_SENSOR,
    LIVING,
    LIVING_SENSOR,
    _household,  # noqa: F401  (autouse: the fake heads and sensors)
    _sensor,
)

ROOMS = [
    {"name": "Living Room", "climate": LIVING, "sensor": LIVING_SENSOR},
    {"name": "Bedroom", "climate": BEDROOM, "sensor": BEDROOM_SENSOR},
]
SETTINGS = {"demand_threshold": 3.0, "idle_action": "fan_only"}
STUDY_SENSOR = "sensor.study_temperature"
VANE = "select.living_room_vane"


def _entry(hass: HomeAssistant, data: dict, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        unique_id=f"{LIVING}|{BEDROOM}",
        version=2,
        minor_version=2,
        data=data,
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def _set_up(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_reconfigure_takes_effect_on_an_entry_whose_rooms_were_in_options(
    hass: HomeAssistant,
) -> None:
    _sensor(hass, STUDY_SENSOR, "70.1", "°F")
    in_options = deepcopy(ROOMS)
    in_options[0]["sensor"] = HALL_SENSOR
    entry = _entry(hass, {"zones": deepcopy(ROOMS), **SETTINGS}, {"zones": in_options, **SETTINGS})
    await _set_up(hass, entry)
    assert entry.runtime_data.zones[0].sensor_id == HALL_SENSOR

    flow = hass.config_entries.flow
    result = await flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await flow.async_configure(result["flow_id"], {"heads": [LIVING, BEDROOM]})
    assert result["step_id"] == "reconfigure_rooms", result
    result = await flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "reconfigure_sensors", result
    assert result["description_placeholders"]["rooms"].count(HALL_SENSOR) == 1
    result = await flow.async_configure(
        result["flow_id"], {"sensor_1": STUDY_SENSOR, "sensor_2": BEDROOM_SENSOR}
    )
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "reconfigure_finish"})
    assert result["reason"] == "reconfigure_successful", result
    await hass.async_block_till_done()

    assert [zone.sensor_id for zone in entry.runtime_data.zones] == [STUDY_SENSOR, BEDROOM_SENSOR]


async def test_saving_a_page_keeps_a_room_wired_by_a_flat_vane_key(hass: HomeAssistant) -> None:
    entry = _entry(
        hass, {"zones": deepcopy(ROOMS), **SETTINGS},
        {"primary_vane_vertical": VANE, **SETTINGS},
    )
    await _set_up(hass, entry)
    assert entry.runtime_data.zones[0].vane_vertical_id == VANE

    result = await open_options_page(hass, entry, "comfort")
    result = await submit(hass.config_entries.options, result, prefilled(result))
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()

    assert entry.runtime_data.zones[0].vane_vertical_id == VANE
    page = await open_options_page(hass, entry, "room_1")
    assert prefilled(page)["vane_vertical"] == VANE
