"""Final-save recheck and cleared-answer retention (M28 confirmation r1).

Every case here comes from the independent M28 confirmation probes, folded into
the retained suite unchanged in substance: the same fixtures, the same
navigation and the same assertions, including the ones that already passed.
Two of them are the regressions the confirmation found — the final save
committing an idle action the selected heads no longer support (M28-C1), and
back-navigation restoring a notification target the user had cleared (M28-C2) —
and the rest are the controls that pin the behaviour around them.

Fake heads and fake sensors only: this is an in-process Home Assistant, and
nothing here says anything about hardware.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import DOMAIN

ALPHA = "climate.sandbox_alpha"
BETA = "climate.sandbox_beta"
GAMMA = "climate.sandbox_gamma"
HEADS = [ALPHA, BETA]
SENSORS = ["sensor.sandbox_alpha", "sensor.sandbox_beta"]
ALL_MODES = ["heat", "cool", "fan_only", "off"]
NOTIFY = "notify.sandbox_review"


def _head(hass: HomeAssistant, entity_id: str, modes: list[str] | None = None) -> None:
    """Publish a fake head advertising exactly ``modes``."""
    hass.states.async_set(
        entity_id,
        "off",
        {
            "friendly_name": entity_id.split(".")[-1],
            "hvac_modes": modes or ALL_MODES,
        },
    )


@pytest.fixture(autouse=True)
def devices(hass: HomeAssistant) -> None:
    """Two selectable heads, a spare, their sensors and a notify service."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    for entity_id in [*HEADS, GAMMA]:
        _head(hass, entity_id)
    for sensor in SENSORS:
        hass.states.async_set(
            sensor, "68", {"device_class": "temperature", "unit_of_measurement": "°F"}
        )
    hass.services.async_register("notify", "sandbox_review", lambda call: None)


async def _submit(
    hass: HomeAssistant, result: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def _press(
    hass: HomeAssistant, result: dict[str, Any], step: str
) -> dict[str, Any]:
    assert result["type"] is FlowResultType.MENU
    return await _submit(hass, result, {"next_step_id": step})


def _suggestion(result: dict[str, Any], key: str) -> Any:
    """What the rendered form actually offers for ``key``."""
    marker = next(m for m in result["data_schema"].schema if m.schema == key)
    if marker.description is not None:
        return marker.description.get("suggested_value")
    return marker.default()


async def _review(
    hass: HomeAssistant,
    entry: MockConfigEntry | None = None,
    notify: str = "omit",
) -> dict[str, Any]:
    """Walk heads -> rooms -> sensors and stop on the review menu."""
    context = (
        {"source": config_entries.SOURCE_USER}
        if entry is None
        else {
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        }
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context=context)
    payload: dict[str, Any] = {"heads": HEADS}
    if notify != "omit":
        payload["notify_service"] = notify
    result = await _submit(hass, result, payload)
    assert result["step_id"] == ("rooms" if entry is None else "reconfigure_rooms")
    result = await _submit(hass, result, {"room_name_1": "Alpha", "room_name_2": "Beta"})
    result = await _submit(hass, result, dict(zip(("sensor_1", "sensor_2"), SENSORS)))
    assert result["step_id"] == ("review" if entry is None else "reconfigure_review")
    return result


def _entry(hass: HomeAssistant, notify: str | None = NOTIFY) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Before",
        version=2,
        unique_id="|".join(HEADS),
        data={
            "zones": [
                {"climate": head, "sensor": sensor, "name": name}
                for head, sensor, name in zip(HEADS, SENSORS, ["Alpha", "Beta"])
            ],
            "notify_service": notify,
        },
        options={"idle_action": "fan_only", "demand_threshold": 3.0},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(
    "action,lost",
    [("fan_only", "fan_only"), ("off", "off"), ("off_after_dry", "fan_only")],
)
async def test_final_save_rechecks_selected_idle(
    hass: HomeAssistant, action: str, lost: str
) -> None:
    """M28-C1: a head losing the chosen parking mode must refuse the save."""
    result = await _review(hass)
    result = await _press(hass, result, "tuning")
    result = await _submit(hass, result, {"idle_action": action})
    assert result["step_id"] == "review"
    assert not hass.config_entries.async_entries(DOMAIN)
    _head(hass, HEADS[0], [mode for mode in ALL_MODES if mode != lost])
    result = await _press(hass, result, "finish")
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.FORM, (
        "Save committed an idle action the selected head no longer supports"
    )
    assert "idle_action_unsupported" in result["errors"].values()
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_changed_heads_rechecks_cached_tuning(hass: HomeAssistant) -> None:
    """M28-C1: swapping a head after advanced revalidates the cached choice."""
    result = await _review(hass)
    result = await _press(hass, result, "tuning")
    result = await _submit(hass, result, {"idle_action": "off"})
    result = await _press(hass, result, "user")
    new_heads = [HEADS[0], GAMMA]
    _head(hass, new_heads[1], ["heat", "cool", "fan_only"])
    result = await _submit(hass, result, {"heads": new_heads})
    result = await _submit(
        hass, result, {"room_name_1": "Alpha", "room_name_2": "Gamma"}
    )
    result = await _submit(hass, result, dict(zip(("sensor_1", "sensor_2"), SENSORS)))
    result = await _press(hass, result, "finish")
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.FORM, (
        "New head cannot honor the retained advanced idle choice"
    )
    assert "idle_action_unsupported" in result["errors"].values()


@pytest.mark.parametrize("back", [False, True], ids=["direct-control", "back"])
async def test_cleared_notify_stays_cleared(hass: HomeAssistant, back: bool) -> None:
    """M28-C2: an explicitly cleared notification target survives going back."""
    entry = _entry(hass)
    before = deepcopy(dict(entry.data))
    result = await _review(hass, entry)
    assert dict(entry.data) == before
    assert "Drift alerts: none" in result["description_placeholders"]["summary"]
    if back:
        result = await _press(hass, result, "reconfigure")
        shown = _suggestion(result, "notify_service")
        payload: dict[str, Any] = {
            "heads": HEADS,
            "entry_title": _suggestion(result, "entry_title"),
        }
        if shown:
            payload["notify_service"] = shown
        result = await _submit(hass, result, payload)
        result = await _submit(hass, result, {})
        result = await _submit(
            hass, result, dict(zip(("sensor_1", "sensor_2"), SENSORS))
        )
    result = await _press(hass, result, "reconfigure_finish")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["notify_service"] is None, (
        "Back navigation restored a notification target the user cleared"
    )


async def test_reconfigure_first_form_suggests_the_stored_notification(
    hass: HomeAssistant,
) -> None:
    """The other half of C2: an unanswered form still offers what is stored."""
    entry = _entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["step_id"] == "reconfigure"
    assert _suggestion(result, "notify_service") == NOTIFY
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert entry.data["notify_service"] == NOTIFY


async def test_lost_heat_cool_is_reported_before_a_lost_sensor(
    hass: HomeAssistant,
) -> None:
    """The save point still asks about the head first, then the sensor.

    The C1 re-check runs after the plain capability check and, through the
    shared helper, would also catch a head that dropped heat and cool. That
    must not become the way the flow reports it: a head which cannot heat or
    cool makes its room's sensor moot, so the head picker is where the user is
    sent, with the head's own error, even when a sensor is missing too.
    """
    result = await _review(hass)
    result = await _press(hass, result, "tuning")
    result = await _submit(hass, result, {"idle_action": "off"})
    _head(hass, HEADS[1], ["off", "fan_only"])
    hass.states.async_remove(SENSORS[0])

    result = await _press(hass, result, "finish")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"heads": "head_missing_heat_cool"}
    assert result["description_placeholders"]["unsupported_heads"] == HEADS[1]
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_back_keeps_names_sensors_title_and_tuning(hass: HomeAssistant) -> None:
    """Control: every answer given survives the review screen's back option."""
    result = await _review(hass, notify=NOTIFY)
    result = await _press(hass, result, "tuning")
    result = await _submit(hass, result, {"mode_hysteresis": 777, "idle_action": "off"})
    result = await _press(hass, result, "user")
    assert _suggestion(result, "heads") == HEADS
    assert _suggestion(result, "notify_service") == NOTIFY
    result = await _submit(
        hass,
        result,
        {"heads": HEADS, "entry_title": "Revised title", "notify_service": NOTIFY},
    )
    assert [_suggestion(result, f"room_name_{i}") for i in (1, 2)] == ["Alpha", "Beta"]
    result = await _submit(hass, result, {})
    assert [_suggestion(result, f"sensor_{i}") for i in (1, 2)] == SENSORS
    result = await _submit(hass, result, dict(zip(("sensor_1", "sensor_2"), SENSORS)))
    assert not hass.config_entries.async_entries(DOMAIN)
    result = await _press(hass, result, "tuning")
    assert result["data_schema"]({})["mode_hysteresis"] == 777
    result = await _submit(hass, result, {})
    assert not hass.config_entries.async_entries(DOMAIN)
    result = await _press(hass, result, "finish")
    assert result["title"] == "Revised title"
    assert result["options"]["mode_hysteresis"] == 777
    assert result["options"]["idle_action"] == "off"
    await hass.async_block_till_done()


@pytest.mark.parametrize("celsius", [False, True])
@pytest.mark.parametrize(
    "unit,value,expected",
    [("K", "293.15", 20), ("absent", "20", 20), (None, "20", 20), ("%", "20", None)],
)
async def test_units_at_flow_and_reader(
    hass: HomeAssistant,
    celsius: bool,
    unit: str | None,
    value: str,
    expected: int | None,
) -> None:
    """Control: the flow and the reader agree on Kelvin, absent and bad units."""
    from custom_components.mxz_coordinator.coordinator import read_room_temp

    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    attrs: dict[str, Any] = {"device_class": "temperature"}
    if unit != "absent":
        attrs["unit_of_measurement"] = unit
    hass.states.async_set(SENSORS[0], value, attrs)
    result = await _review(hass) if expected is not None else None
    actual = read_room_temp(
        hass.states.get(SENSORS[0]), str(hass.config.units.temperature_unit)
    )
    if expected is None:
        from custom_components.mxz_coordinator.config_flow import _sensor_problem

        assert _sensor_problem(hass, SENSORS)[0] == "sensor_unit_unsupported"
        assert actual is None
    else:
        wanted = 68 if unit == "K" and not celsius else 20
        assert actual == pytest.approx(wanted)
        assert result is not None
        summary = result["description_placeholders"]["summary"]
        assert f"{wanted} " + str(hass.config.units.temperature_unit) in summary
        hass.config_entries.flow.async_abort(result["flow_id"])


async def test_final_sensor_recheck_and_correction(hass: HomeAssistant) -> None:
    """Control: a sensor lost at review is refused, then fixed in place."""
    result = await _review(hass)
    hass.states.async_remove(SENSORS[0])
    result = await _press(hass, result, "finish")
    assert result["step_id"] == "sensors"
    assert result["errors"] == {"base": "sensor_missing"}
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.states.async_set(
        SENSORS[0],
        "293.15",
        {"device_class": "temperature", "unit_of_measurement": "K"},
    )
    result = await _submit(hass, result, dict(zip(("sensor_1", "sensor_2"), SENSORS)))
    assert result["step_id"] == "review"
    assert "68 °F" in result["description_placeholders"]["summary"]
    result = await _press(hass, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


@pytest.mark.parametrize("version", [1, 2])
async def test_migration_exact_and_reload(hass: HomeAssistant, version: int) -> None:
    """Control: v1 migrates to exactly this dictionary, and v2 is untouched."""
    old = {
        "primary_climate": HEADS[0],
        "secondary_climate": HEADS[1],
        "primary_sensor": SENSORS[0],
        "secondary_sensor": SENSORS[1],
        "primary_vane_vertical": "select.sandbox_vane",
        "secondary_stage": "sensor.sandbox_stage",
        "notify_service": None,
        "demand_threshold": 4.0,
        "extra_existing": "retained",
    }
    zones = [
        {
            "name": "Primary",
            "climate": HEADS[0],
            "sensor": SENSORS[0],
            "vane_vertical": "select.sandbox_vane",
            "vane_horizontal": None,
            "stage_sensor": None,
        },
        {
            "name": "Secondary",
            "climate": HEADS[1],
            "sensor": SENSORS[1],
            "vane_vertical": None,
            "vane_horizontal": None,
            "stage_sensor": "sensor.sandbox_stage",
        },
    ]
    expected = {**old, "zones": zones}
    data = old if version == 1 else expected
    options = {
        "demand_threshold": 4.0,
        "idle_action": "fan_only",
        "extra_option": "unchanged",
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Migration fixture",
        entry_id="sandbox_m28_migration",
        unique_id="|".join(HEADS),
        version=version,
        data=deepcopy(data),
        options=deepcopy(options),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert dict(entry.data) == expected
    assert dict(entry.options) == options
    assert entry.unique_id == "|".join(HEADS)
    assert entry.version == 2
    registry = er.async_get(hass)

    def snapshot() -> list[tuple[str, str]]:
        return sorted(
            (item.entity_id, item.unique_id)
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        )

    before = snapshot()
    assert before
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert snapshot() == before
    assert dict(entry.data) == expected
    assert dict(entry.options) == options
    assert entry.unique_id == "|".join(HEADS)
    assert entry.version == 2
