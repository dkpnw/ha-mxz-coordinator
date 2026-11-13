"""The settings registry (settings.py) and the strings a person reads stay one thing.

A setting is one row in settings.SETTINGS plus its label and help text in
strings.json. These tests hold that: every row is shown on the page and section
its row names, with a label and a description, in setup's Advanced pages and in
Configure, and every field those pages' strings describe is a row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.mxz_coordinator.settings import (
    NUMBER,
    PAGES,
    ROOM_FIELDS,
    ROOM_SECTIONS,
    SETTINGS,
    STORE_HOOK,
    finite,
    page_layout,
    room_fields,
    room_view,
    store_room,
    tunable_errors,
)

STRINGS = Path(__file__).parents[1] / "custom_components/mxz_coordinator/strings.json"


def _layout_in_strings(flow: str, page: str) -> tuple[list[str], dict[str, list[str]]]:
    step = json.loads(STRINGS.read_text())[flow]["step"][page]
    return list(step["data"]), {
        name: list(section["data"]) for name, section in step.get("sections", {}).items()
    }


@pytest.mark.parametrize("flow", ["config", "options"])
@pytest.mark.parametrize("page", list(PAGES))
def test_each_page_shows_exactly_its_rows_in_order(flow: str, page: str) -> None:
    top, groups = page_layout(page)
    assert _layout_in_strings(flow, page) == (
        list(top), {name: list(fields) for name, fields in groups.items()}
    )


# The fields the shipped copy gives a label and no help text. A new row needs
# help text, or a deliberate place here.
NO_DESCRIPTION = {
    "fan_boost_max", "changeover_heat_above", "clamp_min", "clamp_max", "inhibit_action",
}


@pytest.mark.parametrize("flow", ["config", "options"])
def test_every_row_has_a_label_and_a_description(flow: str) -> None:
    strings = json.loads(STRINGS.read_text())[flow]["step"]
    undescribed = set()
    for setting in SETTINGS:
        step = strings[setting.page]
        where = step["sections"][setting.section] if setting.section else step
        assert where["data"].get(setting.key), setting.key
        if not where.get("data_description", {}).get(setting.key):
            undescribed.add(setting.key)
    assert undescribed == NO_DESCRIPTION


def test_a_row_on_no_page_or_an_unknown_section_is_refused() -> None:
    """Negative: the layout only reads sections a page declares."""
    assert all(setting.page in PAGES for setting in SETTINGS)
    assert all(
        setting.section is None or setting.section in PAGES[setting.page].sections
        for setting in SETTINGS
    )
    keys = [setting.key for setting in SETTINGS]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1, 1.0), ("2.5", 2.5), (True, 1.0), (float("nan"), None), ("inf", None),
     ("-inf", None), ("x", None), (None, None), ([], None)],
)
def test_finite(raw: object, expected: float | None) -> None:
    assert finite(raw) == expected


def test_every_number_is_checked_finite_and_nothing_else_is() -> None:
    numbers = {setting.key for setting in SETTINGS if setting.kind == NUMBER}
    errors = tunable_errors({setting.key: "nan" for setting in SETTINGS})
    assert set(errors) == numbers
    assert set(errors.values()) == {"not_a_number"}


def test_the_room_page_shows_exactly_its_rows_in_order() -> None:
    assert _layout_in_strings("options", "room") == (
        [field.key for field in room_fields(None)],
        {name: [field.key for field in room_fields(name)] for name in ROOM_SECTIONS},
    )


def test_every_room_row_has_a_label() -> None:
    step = json.loads(STRINGS.read_text())["options"]["step"]["room"]
    for field in ROOM_FIELDS:
        where = step["sections"][field.section] if field.section else step
        assert where["data"].get(field.key), field.key


def test_a_room_left_as_shown_stores_what_it_stored() -> None:
    """Saving a page untouched changes no record, whatever it holds (no new keys)."""
    records = [
        {},
        {"hands_off": True, "hands_off_vote_basis": "action", "floor_low": 45.0,
         "drift_cooler_by": 1.5, "report_interval": 5, "evidence_basis": "ha_state_write",
         "dual_setpoint": True, "allow_dry": True, "opening_entity": "binary_sensor.w",
         "opening_pause_enable": True, "opening_open_delay": 30.0, "stage_sensor": "sensor.s"},
    ]
    for record in records:
        zone = dict(record)
        view = room_view(zone)
        store_room(zone, view, dry=True)
        assert zone == record


def test_off_or_blank_stores_no_key() -> None:
    """Negative: every rule but the page's own (hook) removes the key for off/blank."""
    full = {field.zone_key: "x" for field in ROOM_FIELDS}
    zone = dict(full)
    store_room(zone, {}, dry=True)
    assert set(zone) == {field.zone_key for field in ROOM_FIELDS if field.store == STORE_HOOK}


def test_allow_dry_is_left_alone_when_the_page_did_not_show_it() -> None:
    zone = {"allow_dry": True}
    store_room(zone, {}, dry=False)
    assert zone == {"allow_dry": True}
    store_room(zone, {}, dry=True)
    assert zone == {}
