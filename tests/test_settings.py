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
    SETTINGS,
    finite,
    page_layout,
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
