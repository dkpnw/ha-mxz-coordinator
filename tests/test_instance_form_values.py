"""The throwaway-instance driver's form_values on MXZ's real room page.

verification/instance configures a room the way a person does in the browser:
it submits every field as the page shows it, plus the one change it means. Here
the page is MXZ's real Configure, Rooms, (room) page, served as Home Assistant's
own flow view serializes it for the REST API, so any field the driver would
drop or alter shows up as a changed room record. Every room and entity here is
invented.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.data_entry_flow import FlowManagerIndexView
from homeassistant.helpers.json import json_dumps

from custom_components.mxz_coordinator.const import ZONE_COMFORT_PRIORITY_ENTITY
from tests.flow_pages import open_options_page
from tests.test_comfort_priority import OCCUPIED, _setup, _zones


def _load_client() -> ModuleType:
    path = Path(__file__).parents[1] / "verification" / "instance" / "client.py"
    spec = importlib.util.spec_from_file_location("mxz_instance_client_form", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = _load_client()


async def _served_room_page(hass: HomeAssistant, entry: Any, page: str) -> dict[str, Any]:
    """The room page as GET/POST on /api/config/config_entries/options/flow returns it."""
    result = await open_options_page(hass, entry, page)
    view = FlowManagerIndexView(hass.config_entries.options)
    return json.loads(json_dumps(view._prepare_result_json(result)))


async def _submit(hass: HomeAssistant, page: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json_dumps(client.form_values(page["data_schema"], changes)))
    result = await hass.config_entries.options.async_configure(page["flow_id"], payload)
    await hass.async_block_till_done()  # an option change reloads the entry
    return result


async def test_one_change_saves_and_every_other_room_setting_stays(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup(hass, comfort=(None, None))
    before = [dict(zone) for zone in _zones(entry)]
    page = await _served_room_page(hass, entry, "room_2")
    assert any(field.get("type") == "expandable" for field in page["data_schema"])  # sections are exercised
    result = await _submit(hass, page, {ZONE_COMFORT_PRIORITY_ENTITY: OCCUPIED})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    after = _zones(entry)
    assert after[0] == before[0]
    assert after[1] == {**before[1], ZONE_COMFORT_PRIORITY_ENTITY: OCCUPIED}
    assert entry.runtime_data.zones[1].comfort_priority_entity == OCCUPIED


async def test_negative_an_untouched_page_changes_no_room(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup(hass, comfort=(None, OCCUPIED))
    before = [dict(zone) for zone in _zones(entry)]
    for page_name in ("room_1", "room_2"):
        page = await _served_room_page(hass, entry, page_name)
        assert (await _submit(hass, page, {}))["type"] is FlowResultType.CREATE_ENTRY
    assert _zones(entry) == before


async def test_negative_a_field_the_page_does_not_have_is_refused(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup(hass, comfort=(None, None))
    page = await _served_room_page(hass, entry, "room_2")
    with pytest.raises(client.ApiError, match="no field"):
        client.form_values(page["data_schema"], {"comfort_priority": OCCUPIED})
