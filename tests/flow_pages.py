"""Drive the page-menu Configure and setup flows the way the frontend does.

Configure opens on a menu; each page is a form, and some fields sit inside a
collapsed ``section``. The frontend submits a section's fields as a nested
dict under the section key, so these helpers take the flat ``{field: value}``
a test means and nest it for the form actually rendered.

The page and section maps below are written out by hand on purpose: an
independent list, so a field moving page in the flow shows up as a failure
here rather than as two matching mistakes.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section

PAGE_FIELDS: dict[str, tuple[str, ...]] = {
    "comfort": (
        "engage_deadband",
        "demand_threshold",
        "resting_mode_bias",
        "mode_hysteresis",
    ),
    "fan_idle": (
        "fan_boost_enable",
        "fan_boost_max",
        "idle_action",
        "coil_dry_minutes",
    ),
    "seasons": (
        "changeover_entity",
        "changeover_heat_above",
        "changeover_cool_below",
        "heat_lockout_floor",
        "cool_lockout_ceiling",
    ),
    "limits": ("eco_heat_min", "eco_cool_max", "clamp_min", "clamp_max"),
    "standby": ("inhibit_entity", "inhibit_active_state", "inhibit_action"),
}
SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    "advanced": ("mode_hysteresis",),
    "safety": ("heat_lockout_floor", "cool_lockout_ceiling"),
    "setpoint_range": ("clamp_min", "clamp_max"),
    "freshness": (
        "report_interval",
        "max_age",
        "startup_grace",
        "evidence_basis",
        "sample_timestamp_attribute",
        "sample_sequence_attribute",
    ),
}
ROOM_FIELDS: tuple[str, ...] = (
    "stage",
    "vane_vertical",
    "vane_horizontal",
    *SECTION_FIELDS["freshness"],
)
GLOBAL_PAGES = tuple(PAGE_FIELDS)
# Every key that exists only as flow input and must never be stored flat.
FLOW_ONLY_KEYS = frozenset(
    {*SECTION_FIELDS, *ROOM_FIELDS, "next_step_id"}
)


def page_of(field: str) -> str:
    """The global page that owns ``field``."""
    for page, fields in PAGE_FIELDS.items():
        if field in fields:
            return page
    raise AssertionError(f"no page owns {field}")


def _items(result: dict[str, Any]):
    """Yield (marker, selector, section key or None) for every rendered field."""
    schema = result["data_schema"]
    if schema is None:
        return
    for marker, value in schema.schema.items():
        if isinstance(value, section):
            for inner, inner_selector in value.schema.schema.items():
                yield inner, inner_selector, marker.schema
        else:
            yield marker, value, None


def fields(result: dict[str, Any]) -> list[str]:
    """Every field name the form shows, including those inside sections."""
    return [marker.schema for marker, _, _ in _items(result)]


def sections(result: dict[str, Any]) -> dict[str, section]:
    """The form's sections by key."""
    schema = result["data_schema"]
    return {
        marker.schema: value
        for marker, value in schema.schema.items()
        if isinstance(value, section)
    }


def marker_of(result: dict[str, Any], field: str) -> tuple[vol.Marker, Any]:
    """The marker and selector rendering ``field``, wherever it sits."""
    for marker, field_selector, _ in _items(result):
        if marker.schema == field:
            return marker, field_selector
    raise AssertionError(f"missing schema field: {field}")


def suggested(result: dict[str, Any], field: str) -> Any:
    marker, _ = marker_of(result, field)
    return (marker.description or {}).get("suggested_value")


def default(result: dict[str, Any], field: str) -> Any:
    marker, _ = marker_of(result, field)
    return None if marker.default is vol.UNDEFINED else marker.default()


def nest(result: dict[str, Any], flat: dict[str, Any]) -> dict[str, Any]:
    """Shape ``flat`` the way the frontend submits this form.

    Every section key is always present (the frontend always sends it), and a
    field the form does not show stays top level, so a raw-API test can still
    submit an unexpected key.
    """
    owner = {marker.schema: key for marker, _, key in _items(result) if key}
    out: dict[str, Any] = {key: {} for key in sections(result)}
    for field, value in flat.items():
        if field in owner:
            out[owner[field]][field] = value
        else:
            out[field] = value
    return out


def prefilled(result: dict[str, Any]) -> dict[str, Any]:
    """What an untouched form submits: every suggested value and default."""
    values: dict[str, Any] = {}
    for marker, _, _ in _items(result):
        hint = (marker.description or {}).get("suggested_value")
        if hint not in (None, ""):
            values[marker.schema] = hint
        elif marker.default is not vol.UNDEFINED:
            values[marker.schema] = marker.default()
    return values


async def press(manager, result: dict[str, Any], option: str) -> dict[str, Any]:
    """Press one entry on a menu step."""
    assert result["type"] is FlowResultType.MENU, result
    assert option in result["menu_options"], (option, result["menu_options"])
    return await manager.async_configure(result["flow_id"], {"next_step_id": option})


async def submit(manager, result: dict[str, Any], flat: dict[str, Any]) -> dict[str, Any]:
    """Submit one page form with ``flat`` values nested for its sections."""
    assert result["type"] is FlowResultType.FORM, result
    return await manager.async_configure(result["flow_id"], nest(result, flat))


async def open_options_page(
    hass: HomeAssistant, entry, page: str
) -> dict[str, Any]:
    """Open Configure and pick ``page`` (``room_N`` goes through Rooms)."""
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU, result
    assert result["step_id"] == "init", result
    if page.startswith("room_"):
        result = await press(manager, result, "rooms")
        assert result["step_id"] == "rooms", result
    result = await press(manager, result, page)
    assert result["type"] is FlowResultType.FORM, result
    return result


async def save_options_page(
    hass: HomeAssistant, entry, page: str, flat: dict[str, Any]
) -> dict[str, Any]:
    """Open one Configure page and submit ``flat`` on it."""
    result = await open_options_page(hass, entry, page)
    return await submit(hass.config_entries.options, result, flat)


async def save_options(
    hass: HomeAssistant, entry, flat: dict[str, Any]
) -> dict[str, Any]:
    """Save ``flat`` on the one global page that owns all of its keys."""
    pages = {page_of(field) for field in flat}
    assert len(pages) == 1, f"{sorted(flat)} spans pages {sorted(pages)}"
    return await save_options_page(hass, entry, pages.pop(), flat)


async def setup_tuning(
    hass: HomeAssistant, result: dict[str, Any], flat: dict[str, Any]
) -> dict[str, Any]:
    """From the setup review menu, enter ``flat`` page by page, then go back.

    Returns the review menu, or the first page form that refused its input.
    An empty ``flat`` opens and submits every page untouched.
    """
    manager = hass.config_entries.flow
    result = await press(manager, result, "tuning")
    assert result["step_id"] == "tuning", result
    pages = [page for page in GLOBAL_PAGES if not flat or set(PAGE_FIELDS[page]) & set(flat)]
    assert not set(flat) - {f for page in pages for f in PAGE_FIELDS[page]}, flat
    for page in pages:
        result = await press(manager, result, page)
        assert result["step_id"] == page, result
        result = await submit(
            manager,
            result,
            {key: value for key, value in flat.items() if key in PAGE_FIELDS[page]},
        )
        if result["type"] is not FlowResultType.MENU:
            return result
        assert result["step_id"] == "tuning", result
    return await press(manager, result, "review")
