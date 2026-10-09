"""Configure as a page menu: what one page save may and may not touch.

Each Configure page saves on its own. The hazards this file pins are the ones
a naive split introduces: a page that does not show a field must not read the
field's absence as "cleared", a room page must not rewrite the other rooms,
and no flow-only key (section keys, the generic room-page keys) may reach the
stored entry. Fake heads and sensors only.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import DOMAIN, unit_profile
from tests.flow_pages import (
    FLOW_ONLY_KEYS,
    GLOBAL_PAGES,
    PAGE_FIELDS,
    SECTION_FIELDS,
    fields,
    marker_of,
    open_options_page,
    prefilled,
    press,
    sections,
    submit,
)

HEADS = ("climate.rec_room", "climate.bedroom", "climate.office")
SENSORS = ("sensor.rec_room_temp", "sensor.bedroom_temp", "sensor.office_temp")
ALL_MODES = ["off", "cool", "heat", "fan_only"]
HOLD = "binary_sensor.grid_down"
WEATHER = "weather.home"

# Stored zone keys, written out independently of const.py.
_ZONE_KEYS = frozenset(
    {
        "name",
        "climate",
        "sensor",
        "vane_vertical",
        "vane_horizontal",
        "stage_sensor",
        "report_interval",
        "max_age",
        "startup_grace",
        "evidence_basis",
        "sample_timestamp_attribute",
        "sample_sequence_attribute",
    }
)
# The 3.4.0 per-slot flow keys, which were never stored flat either.
_SLOT_KEYS = frozenset(
    f"{slug}_{suffix}"
    for slug in ("primary", "secondary", *(f"zone_{n}" for n in range(3, 9)))
    for suffix in (
        "vane_vertical",
        "vane_horizontal",
        "stage",
        "report_interval",
        "max_age",
        "startup_grace",
        "evidence_basis",
        "sample_timestamp_attribute",
        "sample_sequence_attribute",
    )
)
_TUNABLE_KEYS = frozenset(key for page in PAGE_FIELDS.values() for key in page)


def _head(hass: HomeAssistant, entity_id: str, modes: list[str] | None = None) -> None:
    hass.states.async_set(
        entity_id,
        "off",
        {
            "friendly_name": entity_id.split(".")[1].replace("_", " ").title(),
            "hvac_modes": modes or ALL_MODES,
            "supported_features": int(ClimateEntityFeature.FAN_MODE),
            "fan_modes": ["auto", "quiet", "low", "medium", "middle", "high"],
        },
    )


@pytest.fixture(autouse=True)
def _house(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    for head in HEADS:
        _head(hass, head)
    for sensor in SENSORS:
        hass.states.async_set(
            sensor, "70", {"device_class": "temperature", "unit_of_measurement": "°F"}
        )


def _wired_zones() -> list[dict[str, Any]]:
    """Three rooms, each with distinct vanes, airflow and a full profile."""
    zones = []
    for index, (head, sensor) in enumerate(zip(HEADS, SENSORS, strict=True)):
        slug = head.split(".")[1]
        zones.append(
            {
                "name": slug.replace("_", " ").title(),
                "climate": head,
                "sensor": sensor,
                "vane_vertical": f"select.{slug}_vane",
                "vane_horizontal": f"select.{slug}_wide_vane",
                "stage_sensor": f"sensor.{slug}_stage",
                "report_interval": 2.0 + index,
                "max_age": 6.0 + index,
                "startup_grace": 4.0 + index,
                "evidence_basis": "sample_timestamp",
                "sample_timestamp_attribute": f"{slug}_sampled_at",
            }
        )
    return zones


def _entry(
    hass: HomeAssistant,
    *,
    options: dict[str, Any] | None = None,
    options_own_zones: bool = False,
) -> MockConfigEntry:
    zones = _wired_zones()
    stored_options = {
        "demand_threshold": 3.0,
        "inhibit_entity": HOLD,
        "idle_action": "fan_only",
        **(options or {}),
    }
    if options_own_zones:
        stored_options["zones"] = deepcopy(zones)
        data_zones = deepcopy(zones)
        for zone in data_zones:
            zone["report_interval"] = 99.0  # shadowed by the options copy
    else:
        data_zones = zones
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="MXZ Coordinator",
        unique_id="|".join(HEADS),
        data={"zones": data_zones, **stored_options},
        options=stored_options,
    )
    entry.add_to_hass(hass)
    return entry


def _snapshot(entry: MockConfigEntry) -> tuple[dict, dict]:
    return deepcopy(dict(entry.data)), deepcopy(dict(entry.options))


def _effective_zones(entry: MockConfigEntry) -> list[dict[str, Any]]:
    return deepcopy({**entry.data, **entry.options}["zones"])


# --- §6 item 1: a page that does not show the hold entity keeps it ----------


@pytest.mark.parametrize("page", [*[p for p in GLOBAL_PAGES if p != "standby"], "room_1"])
async def test_saving_a_page_without_the_hold_field_keeps_the_hold_entity(
    hass: HomeAssistant, page: str
) -> None:
    """Hazard 1a: absence means "cleared" only on the page that shows the field.

    3.4.0 read a missing inhibit_entity as a clear because its one form always
    rendered it. Comfort, Fan and idle, Seasons, Away and setpoint limits and
    every room page never render it, so saving them must leave the configured
    standby hold exactly where it was, in options and in the data mirror.
    """
    entry = _entry(hass)
    result = await open_options_page(hass, entry, page)
    assert "inhibit_entity" not in fields(result)

    result = await submit(hass.config_entries.options, result, prefilled(result))

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert entry.options["inhibit_entity"] == HOLD
    assert entry.data["inhibit_entity"] == HOLD


async def test_standby_page_still_clears_the_hold_entity(hass: HomeAssistant) -> None:
    """Control for 1a: on the page that shows it, an emptied field clears it."""
    entry = _entry(hass)
    result = await open_options_page(hass, entry, "standby")
    submission = prefilled(result)
    assert submission.pop("inhibit_entity") == HOLD

    result = await submit(hass.config_entries.options, result, submission)

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert entry.options["inhibit_entity"] is None
    assert entry.data["inhibit_entity"] is None


# The weather source follows the same rule on Seasons, the page that shows it.


@pytest.mark.parametrize("page", [*[p for p in GLOBAL_PAGES if p != "seasons"], "room_1"])
async def test_saving_a_page_without_the_weather_field_keeps_the_weather_source(
    hass: HomeAssistant, page: str
) -> None:
    entry = _entry(hass, options={"changeover_entity": WEATHER})
    result = await open_options_page(hass, entry, page)
    assert "changeover_entity" not in fields(result)

    result = await submit(hass.config_entries.options, result, prefilled(result))

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert entry.options["changeover_entity"] == WEATHER
    assert entry.data["changeover_entity"] == WEATHER


async def test_untouched_seasons_save_keeps_the_weather_source(hass: HomeAssistant) -> None:
    entry = _entry(hass, options={"changeover_entity": WEATHER})
    result = await open_options_page(hass, entry, "seasons")
    assert prefilled(result)["changeover_entity"] == WEATHER

    result = await submit(hass.config_entries.options, result, prefilled(result))

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert entry.options["changeover_entity"] == WEATHER
    assert entry.data["changeover_entity"] == WEATHER


async def test_seasons_page_clears_the_weather_source(hass: HomeAssistant) -> None:
    """Clearing Weather source on Seasons clears it in both stores, and the
    coordinator then runs without forecast lockouts. The hold entity, which
    Seasons does not show, is kept."""
    from custom_components.mxz_coordinator.coordinator import MXZCoordinator

    entry = _entry(hass, options={"changeover_entity": WEATHER})
    result = await open_options_page(hass, entry, "seasons")
    submission = prefilled(result)
    assert submission.pop("changeover_entity") == WEATHER
    submission["changeover_heat_above"] = 72.0

    result = await submit(hass.config_entries.options, result, submission)

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert entry.options["changeover_entity"] is None
    assert entry.data["changeover_entity"] is None
    assert entry.options["changeover_heat_above"] == 72.0
    assert entry.options["inhibit_entity"] == HOLD
    assert MXZCoordinator(hass, entry).changeover_entity is None

    result = await open_options_page(hass, entry, "seasons")
    assert "changeover_entity" not in prefilled(result)


async def test_setup_seasons_page_clears_a_weather_source_entered_earlier(
    hass: HomeAssistant,
) -> None:
    """Setup keeps page answers until Save; emptying the picker on a second
    visit leaves no weather source in the created entry."""
    manager = hass.config_entries.flow
    result = await press(manager, await _setup_review(hass), "tuning")
    result = await press(manager, result, "seasons")
    result = await submit(manager, result, {**prefilled(result), "changeover_entity": WEATHER})
    assert result["step_id"] == "tuning", result
    result = await press(manager, result, "seasons")
    submission = prefilled(result)
    assert submission.pop("changeover_entity") == WEATHER
    result = await submit(manager, result, submission)
    assert result["step_id"] == "tuning", result

    result = await press(manager, await press(manager, result, "review"), "finish")

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert not result["options"].get("changeover_entity")
    assert not result["result"].data.get("changeover_entity")


# --- §6 item 2: one room's save leaves every other room alone ---------------


@pytest.mark.parametrize("options_own_zones", [False, True], ids=["data", "options"])
async def test_saving_one_room_keeps_every_other_rooms_wiring(
    hass: HomeAssistant, options_own_zones: bool
) -> None:
    """Hazard 1b: the zone fold applies to the room being edited, only.

    3.4.0 folded every room's fields on every save and read a missing field as
    "cleared". A room page shows one room, so applying that fold to all of them
    would wipe the other rooms' vanes, airflow sensor and freshness profile.
    The edited room keeps everything it was not asked to change.
    """
    entry = _entry(hass, options_own_zones=options_own_zones)
    before = _effective_zones(entry)
    result = await open_options_page(hass, entry, "room_2")
    submission = prefilled(result)
    # The page shows exactly room 2's current wiring, ready to resubmit.
    assert submission["vane_vertical"] == before[1]["vane_vertical"]
    assert submission["report_interval"] == before[1]["report_interval"]
    submission["stage"] = "sensor.bedroom_new_stage"

    result = await submit(hass.config_entries.options, result, submission)

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    expected = deepcopy(before)
    expected[1]["stage_sensor"] = "sensor.bedroom_new_stage"
    assert entry.data["zones"] == expected
    if options_own_zones:
        assert entry.options["zones"] == expected
    else:
        assert "zones" not in entry.options


async def test_a_room_page_follows_its_room_through_a_reorder(
    hass: HomeAssistant,
) -> None:
    """The open page edits the room it showed, even if Reconfigure moved it.

    Reconfigure can reorder rooms while a Configure room page is open. The
    submit must land on the same head's record, not on whichever room now
    sits in the slot the page was opened from.
    """
    entry = _entry(hass)
    result = await open_options_page(hass, entry, "room_2")
    assert result["description_placeholders"] == {"room": "Bedroom"}
    submission = {**prefilled(result), "stage": "sensor.bedroom_new_stage"}
    swapped = deepcopy(entry.data["zones"])
    swapped[0], swapped[1] = swapped[1], swapped[0]
    hass.config_entries.async_update_entry(entry, data={**entry.data, "zones": swapped})

    result = await submit(hass.config_entries.options, result, submission)

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    expected = deepcopy(swapped)
    expected[0]["stage_sensor"] = "sensor.bedroom_new_stage"
    assert entry.data["zones"] == expected


async def test_a_room_removed_while_its_page_is_open_saves_nothing(
    hass: HomeAssistant,
) -> None:
    """A room Reconfigure removed meanwhile is not resurrected or misfiled."""
    entry = _entry(hass)
    result = await open_options_page(hass, entry, "room_3")
    submission = prefilled(result)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "zones": entry.data["zones"][:2]}
    )
    before = _snapshot(entry)

    result = await submit(hass.config_entries.options, result, submission)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "rooms"
    assert result["menu_options"] == ["room_1", "room_2", "init"]
    assert _snapshot(entry) == before


# --- §6 item 3: flow-only keys never reach storage -------------------------


@pytest.mark.parametrize("options_own_zones", [False, True], ids=["data", "options"])
async def test_no_section_or_room_page_key_is_ever_stored(
    hass: HomeAssistant, options_own_zones: bool
) -> None:
    """Section keys and generic room keys are flow input, never stored keys."""
    entry = _entry(hass, options_own_zones=options_own_zones)
    pages = [*GLOBAL_PAGES, "room_1", "room_2", "room_3"]
    for page in pages:
        result = await open_options_page(hass, entry, page)
        result = await submit(hass.config_entries.options, result, prefilled(result))
        assert result["type"] is FlowResultType.CREATE_ENTRY, (page, result)

    for store in (entry.data, entry.options):
        leaked = set(store) & (FLOW_ONLY_KEYS | _SLOT_KEYS)
        assert not leaked, leaked
        for zone in store.get("zones", []):
            assert set(zone) <= _ZONE_KEYS, set(zone) - _ZONE_KEYS
    assert set(entry.options) <= _TUNABLE_KEYS | {"zones"}, set(entry.options)


# --- §6 item 4: an error inside a section is shown on the section ---------


@pytest.mark.parametrize(
    ("page", "submission", "errors", "section_key"),
    [
        ("comfort", {"mode_hysteresis": -1}, {"advanced": "must_not_be_negative"}, "advanced"),
        (
            "limits",
            {"clamp_min": 90.0, "clamp_max": 80.0},
            {"setpoint_range": "clamp_inverted"},
            "setpoint_range",
        ),
        (
            "seasons",
            {"heat_lockout_floor": 90.0, "cool_lockout_ceiling": 80.0},
            {"safety": "lockout_inverted"},
            "safety",
        ),
        (
            "room_1",
            {"report_interval": 5.0, "max_age": 1.0, "evidence_basis": "ha_state_write"},
            {"base": "freshness_profile_invalid"},
            "freshness",
        ),
    ],
    ids=["hysteresis", "clamp", "lockout", "freshness"],
)
async def test_an_error_inside_a_section_is_keyed_to_it_and_opens_it(
    hass: HomeAssistant,
    page: str,
    submission: dict[str, Any],
    errors: dict[str, str],
    section_key: str,
) -> None:
    """Hazard 3: the frontend drops field errors inside a section.

    The error is reported on the section key (or base, for the freshness
    profile's one diagnostic) and that section re-renders expanded, with the
    rejected values still in it. Nothing is saved.
    """
    entry = _entry(hass)
    before = _snapshot(entry)
    result = await open_options_page(hass, entry, page)
    assert sections(result)[section_key].options["collapsed"] is True

    result = await submit(
        hass.config_entries.options, result, {**prefilled(result), **submission}
    )

    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == ("room" if page.startswith("room_") else page)
    assert result["errors"] == errors
    assert sections(result)[section_key].options["collapsed"] is False
    shown = prefilled(result)
    for field, value in submission.items():
        assert shown[field] == value, field
    assert _snapshot(entry) == before


async def test_a_top_level_pair_error_stays_on_its_fields(hass: HomeAssistant) -> None:
    """Control for item 4: fields outside a section keep their own errors."""
    entry = _entry(hass)
    before = _snapshot(entry)
    result = await open_options_page(hass, entry, "limits")
    result = await submit(
        hass.config_entries.options,
        result,
        {**prefilled(result), "eco_heat_min": 90.0, "eco_cool_max": 80.0},
    )

    assert result["errors"] == {
        "eco_heat_min": "eco_band_inverted",
        "eco_cool_max": "eco_band_inverted",
    }
    assert sections(result)["setpoint_range"].options["collapsed"] is True
    assert _snapshot(entry) == before


# --- §6 item 5: a head capability problem blocks every page ----------------


async def test_a_head_problem_found_on_a_page_blocks_and_saves_nothing(
    hass: HomeAssistant,
) -> None:
    """A head that loses its capabilities after the menu opens still blocks.

    The page shows the blocking form instead of its fields, a submit cannot
    save, and once the head is back the same dialog returns to the menu.
    """
    entry = _entry(hass)
    before = _snapshot(entry)
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    hass.states.async_remove(HEADS[1])

    result = await press(manager, result, "comfort")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "head_capabilities_unavailable"}
    assert fields(result) == []
    result = await manager.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "head_capabilities_unavailable"}
    assert _snapshot(entry) == before

    _head(hass, HEADS[1])
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert _snapshot(entry) == before


# --- §6 item 6: the menu names stored values that now fail validation -------


async def test_menu_attention_names_stored_problems_and_their_pages(
    hass: HomeAssistant,
) -> None:
    """Option A: pages validate only themselves, so the menu says what's wrong.

    A stored idle action the heads cannot do, a stored inverted pair and a
    stored profile that would not pass a save are each named with the page
    that fixes them. Other pages still save, and the stored values are kept
    exactly until the user changes them.
    """
    for head in HEADS:
        _head(hass, head, ["off", "cool", "heat"])
    entry = _entry(
        hass, options={"idle_action": "off_after_dry", "clamp_min": 90.0, "clamp_max": 80.0}
    )
    zones = deepcopy(entry.data["zones"])
    zones[2]["evidence_basis"] = "ha_state_write"  # keeps its timestamp marker
    hass.config_entries.async_update_entry(entry, data={**entry.data, "zones": zones})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    attention = result["description_placeholders"]["attention"]
    assert attention.startswith("**Needs attention:**"), attention
    assert "Off after drying" in attention
    assert "Fan and idle" in attention
    assert "lowest setpoint" in attention
    assert "Away and setpoint limits" in attention
    assert "Office" in attention and "Rooms" in attention
    for raw in ("off_after_dry", "clamp_min", "climate."):
        assert raw not in attention, raw

    result = await submit(
        hass.config_entries.options,
        await press(hass.config_entries.options, result, "comfort"),
        {"demand_threshold": 4.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert entry.options["demand_threshold"] == 4.0
    assert entry.options["clamp_min"] == 90.0
    assert entry.options["clamp_max"] == 80.0
    assert entry.options["idle_action"] == "off_after_dry"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (
            {"eco_heat_min": 80.0, "eco_cool_max": 60.0},
            "the saved away low limit is above the away high limit."
            " Open Away and setpoint limits to fix it.",
        ),
        (
            {"clamp_min": 90.0, "clamp_max": 80.0},
            "the saved lowest setpoint is above the highest setpoint."
            " Open Away and setpoint limits to fix it.",
        ),
        (
            {"heat_lockout_floor": 75.0, "cool_lockout_ceiling": 70.0},
            'the saved "Heat anyway below" is above "Cool anyway above".'
            " Open Seasons to fix it.",
        ),
        (
            {"changeover_cool_below": 70.0, "changeover_heat_above": 60.0},
            "the saved cool lockout temperature isn't below the heat lockout"
            " temperature. Open Seasons to fix it.",
        ),
        (
            {"changeover_cool_below": 60.0, "changeover_heat_above": 60.0},
            "the saved cool lockout temperature isn't below the heat lockout"
            " temperature. Open Seasons to fix it.",
        ),
        (
            {"coil_dry_minutes": -5.0},
            'the saved "Coil drying time" is negative. Open Fan and idle to fix it.',
        ),
        (
            {"demand_threshold": float("nan")},
            'the saved "Mode switch threshold" isn\'t a number. Open Comfort to fix it.',
        ),
        (
            {"engage_deadband": "warm"},
            'the saved "Allowed drift" isn\'t a number. Open Comfort to fix it.',
        ),
    ],
    ids=[
        "eco",
        "clamp",
        "lockout",
        "changeover",
        "changeover-equal",
        "negative",
        "nan",
        "not-a-number",
    ],
)
async def test_menu_attention_names_each_bad_stored_value_and_its_page(
    hass: HomeAssistant, stored: dict[str, Any], expected: str
) -> None:
    """Every validation the pages run is also run on the stored values, and
    the line names the page that shows the field."""
    entry = _entry(hass, options=stored)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["description_placeholders"]["attention"] == (
        f"**Needs attention:** {expected}"
    )


async def test_menu_attention_is_empty_when_nothing_is_wrong(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["description_placeholders"]["attention"] == ""
    assert result["menu_options"] == [*GLOBAL_PAGES, "rooms"]


# --- §6 item 7: setup reuses the pages ------------------------------------


async def _setup_review(hass: HomeAssistant) -> dict[str, Any]:
    manager = hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await manager.async_configure(result["flow_id"], {"heads": list(HEADS[:2])})
    result = await manager.async_configure(result["flow_id"], {})
    result = await manager.async_configure(
        result["flow_id"], {"sensor_1": SENSORS[0], "sensor_2": SENSORS[1]}
    )
    assert result["step_id"] == "review", result
    return result


@pytest.mark.parametrize("celsius", [False, True], ids=["fahrenheit", "celsius"])
async def test_setup_defaults_are_the_union_of_the_page_defaults(
    hass: HomeAssistant, celsius: bool
) -> None:
    """Skipping advanced, submitting every page untouched, and the pages' own
    rendered defaults are one option set, in both units."""
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    manager = hass.config_entries.flow
    result = await press(manager, await _setup_review(hass), "finish")
    skipped = dict(result["options"])
    await hass.config_entries.async_remove(result["result"].entry_id)

    result = await press(manager, await _setup_review(hass), "tuning")
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [*GLOBAL_PAGES, "review"]
    rendered: dict[str, Any] = {}
    for page in GLOBAL_PAGES:
        result = await press(manager, result, page)
        assert set(fields(result)) == set(PAGE_FIELDS[page]), page
        rendered.update(prefilled(result))
        result = await submit(manager, result, {})
        assert result["step_id"] == "tuning", result
    result = await press(manager, await press(manager, result, "review"), "finish")

    assert result["options"] == skipped
    assert rendered == skipped
    assert skipped["demand_threshold"] == unit_profile(celsius)["defaults"]["demand_threshold"]


async def test_setup_forced_idle_choice_lands_on_fan_and_idle(hass: HomeAssistant) -> None:
    """M12: no head supports the default idle, so Save opens Fan and idle."""
    for head in HEADS:
        _head(hass, head, ["off", "cool", "heat"])
    manager = hass.config_entries.flow
    result = await press(manager, await _setup_review(hass), "finish")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "fan_idle"
    assert result["errors"] == {"idle_action": "idle_action_unsupported"}
    _, idle_selector = marker_of(result, "idle_action")
    assert list(idle_selector.config["options"]) == ["off"]
    assert hass.config_entries.async_entries(DOMAIN) == []

    result = await submit(manager, result, {"idle_action": "off"})
    assert result["step_id"] == "tuning"
    result = await press(manager, await press(manager, result, "review"), "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["idle_action"] == "off"
    assert set(result["options"]) == _TUNABLE_KEYS - {"changeover_entity", "inhibit_entity"}


def test_page_map_partitions_every_tunable() -> None:
    """The oracle itself: every tunable sits on exactly one page, and every
    section field on the page that owns its section."""
    seen = [key for page in PAGE_FIELDS.values() for key in page]
    assert len(seen) == len(set(seen)) == 20
    for key, owned in SECTION_FIELDS.items():
        if key != "freshness":
            assert sum(set(owned) <= set(page) for page in PAGE_FIELDS.values()) == 1


# --- the copy each page renders ---------------------------------------------


def _strings() -> dict[str, Any]:
    import json
    from pathlib import Path

    import custom_components.mxz_coordinator as component

    folder = Path(component.__file__).parent
    raw = (folder / "strings.json").read_bytes()
    assert raw == (folder / "translations" / "en.json").read_bytes()
    return json.loads(raw.decode("utf-8"))


def _labels(step: dict[str, Any], result: dict[str, Any]) -> dict[str, str]:
    """Each rendered field's label, read where the frontend looks for it."""
    found: dict[str, str] = {}
    owner = {
        inner: key
        for key, page_section in sections(result).items()
        for inner in [marker.schema for marker in page_section.schema.schema]
    }
    for field in fields(result):
        if field in owner:
            found[field] = step["sections"][owner[field]]["data"][field]
        else:
            found[field] = step["data"][field]
    return found


async def test_every_rendered_field_and_section_has_floor_safe_copy(
    hass: HomeAssistant,
) -> None:
    """Every field and section a page renders has a label, in setup and
    Configure alike, and none uses a placeholder: field labels and section
    names take no placeholders before HA 2025.5, and 2024.12 is supported."""
    strings = _strings()
    entry = _entry(hass)
    for page in [*GLOBAL_PAGES, "room_1"]:
        result = await open_options_page(hass, entry, page)
        step = strings["options"]["step"][result["step_id"]]
        labels = _labels(step, result)
        for key in sections(result):
            labels[f"section {key}"] = step["sections"][key]["name"]
        assert all(labels.values()), labels
        assert not [text for text in labels.values() if "{" in text], labels
        hass.config_entries.options.async_abort(result["flow_id"])
        if page in PAGE_FIELDS:
            assert strings["config"]["step"][page] == step, page

    menus = {
        ("options", "init"): [*GLOBAL_PAGES, "rooms"],
        ("options", "rooms"): [*(f"room_{n}" for n in range(1, 9)), "init"],
        ("config", "tuning"): [*GLOBAL_PAGES, "review"],
    }
    for (flow, step_id), options in menus.items():
        assert list(strings[flow]["step"][step_id]["menu_options"]) == options


def test_attention_and_idle_labels_match_the_shipped_copy() -> None:
    """The code-side names the user reads match the labels they see."""
    from custom_components.mxz_coordinator.capabilities import IDLE_ACTION_LABELS
    from custom_components.mxz_coordinator.config_flow import (
        _FIELD_LABELS,
        _PAGE_NAMES,
    )

    strings = _strings()
    shown: dict[str, str] = {}
    for page, step in strings["options"]["step"].items():
        if page in PAGE_FIELDS:
            shown.update(step["data"])
            for page_section in step.get("sections", {}).values():
                shown.update(page_section["data"])
    for field, label in _FIELD_LABELS.items():
        assert shown[field] == label, field
    assert _PAGE_NAMES == {
        page: strings["options"]["step"]["init"]["menu_options"][page]
        for page in PAGE_FIELDS
    }
    selector = strings["selector"]["idle_action"]["options"]
    for action, label in IDLE_ACTION_LABELS.items():
        assert selector[action].removesuffix(" (default)") == label
