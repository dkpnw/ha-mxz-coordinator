"""A saved drift that isn't a number: setup survives, and says so once.

Allowed drift used to go straight through ``float()`` on load, so a saved value
that isn't a number (a hand edit, a restored backup, an older version) failed
setup outright. It now reads as the unit's default Allowed drift. The uneven
sides (global and per room) already read such a value as blank; now each of
the three is also named once in the log, so it can be found and fixed. The
options flow refuses a value that isn't a number, so only a stored one can get
this far. Fake heads and sensors only.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import DOMAIN
from tests.flow_pages import open_options_page, prefilled, submit
from tests.test_configure_menu import _entry
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_temp,
    _setup_mock_heads,
)
from tests.test_room_uneven_drift import _setup as _room_setup
from tests.test_uneven_drift import house  # noqa: F401  (a fixture)

# Stored keys and defaults, written out independently of const.py.
ALLOWED = "engage_deadband"
COOLER = "drift_cooler_by"
WARMER = "drift_warmer_by"
DEFAULT_F = 1.0
DEFAULT_C = 0.5

NOT_NUMBERS = ["warm", "", float("nan"), float("inf"), "-inf", [1.0], {"v": 1}]
NOT_NUMBER_IDS = ["text", "empty", "nan", "inf", "-inf-text", "list", "dict"]


async def _setup(
    hass: HomeAssistant, options: dict[str, Any], *, celsius: bool = False
) -> MockConfigEntry:
    """Two rooms; ``options`` is the saved options, where Configure keeps drift."""
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 20 if celsius else 68)
    await _set_temp(hass, SENSOR_B, 20 if celsius else 68)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="MXZ Coordinator",
        data={
            "zones": [
                {"name": "Primary", "climate": head_a, "sensor": SENSOR_A},
                {"name": "Secondary", "climate": head_b, "sensor": SENSOR_B},
            ],
        },
        options={"idle_action": "fan_only", "demand_threshold": 3.0, **options},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _drift_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name.startswith("custom_components.mxz_coordinator")
        and "isn't a number" in r.getMessage()
    ]


def _row(hass: HomeAssistant, entry, index: int = 0) -> dict[str, Any]:
    return hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][index]


@pytest.mark.parametrize("value", NOT_NUMBERS, ids=NOT_NUMBER_IDS)
async def test_allowed_drift_that_isnt_a_number_falls_back_to_the_default(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, value: Any
) -> None:
    entry = await _setup(hass, {ALLOWED: value})
    assert entry.state is ConfigEntryState.LOADED
    coord = entry.runtime_data
    assert coord.engage_deadband == DEFAULT_F
    # In use and shown: the plan row, both sides, and the room's drift number.
    await _recompute(hass, entry)
    row = _row(hass, entry)
    assert (row["drift"], row["drift_heat"], row["drift_cool"]) == (
        DEFAULT_F,
        DEFAULT_F,
        DEFAULT_F,
    )
    assert float(hass.states.get(_eid(hass, entry, "_primary_drift")).state) == DEFAULT_F
    # Named once, with the bad value, and not again on later ticks.
    await _recompute(hass, entry)
    warnings = _drift_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert '"Allowed drift"' in warnings[0]
    assert repr(value) in warnings[0]


async def test_celsius_allowed_drift_falls_back_to_the_celsius_default(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await _setup(hass, {ALLOWED: "warm"}, celsius=True)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.engage_deadband == DEFAULT_C
    assert len(_drift_warnings(caplog)) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2.5, 2.5),
        ("3", 3.0),  # a numeric string still counts
        (40, 5.0),  # out of range is clamped, as before
        (0.0, 0.5),  # zero is a number, not "missing"
        (None, DEFAULT_F),  # blank is the default, with nothing to report
    ],
)
async def test_a_numeric_allowed_drift_is_read_as_before_without_a_warning(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, value: Any, expected: float
) -> None:
    entry = await _setup(hass, {ALLOWED: value})
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.engage_deadband == expected
    assert _drift_warnings(caplog) == []


@pytest.mark.parametrize(
    ("key", "label"),
    [(COOLER, '"Let it get cooler by"'), (WARMER, '"Let it get warmer by"')],
)
async def test_a_global_side_that_isnt_a_number_is_blank_and_named_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, key: str, label: str
) -> None:
    entry = await _setup(hass, {ALLOWED: 2.0, key: "warm"})
    assert entry.state is ConfigEntryState.LOADED
    coord = entry.runtime_data
    assert getattr(coord, key) is None
    await _recompute(hass, entry)
    row = _row(hass, entry)
    assert (row["drift_heat"], row["drift_cool"]) == (2.0, 2.0)
    warnings = _drift_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert label in warnings[0] and "'warm'" in warnings[0]


async def test_numeric_global_sides_log_nothing(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await _setup(hass, {COOLER: 1.5, WARMER: "2"})
    assert (entry.runtime_data.drift_cooler_by, entry.runtime_data.drift_warmer_by) == (
        1.5,
        2.0,
    )
    assert _drift_warnings(caplog) == []


async def test_a_room_side_that_isnt_a_number_is_blank_and_named_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await _room_setup(hass, rooms=({WARMER: "warm"}, {COOLER: 2.0}))
    zones = entry.runtime_data.zones
    assert (zones[0].drift_cooler_by, zones[0].drift_warmer_by) == (None, None)
    assert (zones[1].drift_cooler_by, zones[1].drift_warmer_by) == (2.0, None)
    warnings = _drift_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert '"Let it get warmer by" for Primary' in warnings[0]
    assert "'warm'" in warnings[0]


async def test_the_menu_still_points_at_the_bad_allowed_drift_once_loaded(
    hass: HomeAssistant,
) -> None:
    """Setup surviving is not the fix; Configure still says where to fix it."""
    entry = await _setup(hass, {ALLOWED: "warm"})
    assert entry.state is ConfigEntryState.LOADED
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"]["attention"] == (
        '**Needs attention:** the saved "Allowed drift" isn\'t a number.'
        " Open Comfort to fix it."
    )


async def test_comfort_refuses_an_allowed_drift_that_isnt_a_number(
    hass: HomeAssistant, house  # noqa: F811  (the imported fixture)
) -> None:
    """NaN passes the selector's bounds; the page's own check refuses it."""
    house()
    entry = _entry(hass)
    before = (dict(entry.data), dict(entry.options))
    result = await open_options_page(hass, entry, "comfort")
    result = await submit(
        hass.config_entries.options,
        result,
        {**prefilled(result), ALLOWED: float("nan")},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {ALLOWED: "not_a_number"}
    assert (dict(entry.data), dict(entry.options)) == before
