"""Invalid room-sensor inputs never participate in automatic demand."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfTemperature
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (
    CONF_ECO_COOL_MAX,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)
from custom_components.mxz_coordinator.coordinator import _read_temp, read_room_temp
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    MockHead,
    MockHeadC,
    _eid,
    _recompute,
    _setup_mock_heads,
)


async def _set_sensor(
    hass: HomeAssistant, entity_id: str, value: str | float
) -> None:
    hass.states.async_set(
        entity_id,
        str(value),
        {ATTR_UNIT_OF_MEASUREMENT: hass.config.units.temperature_unit},
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("unit_system", "head_class", "eco_cool_max", "valid_primary", "healthy_heat"),
    [
        (US_CUSTOMARY_SYSTEM, MockHead, 65.0, 60.0, 45.0),
        # 5.0 °C, not 10.0: the metric eco_heat_min default IS 10.0 and
        # room_call compares strictly (temp < eco_heat_min), so a 10.0 °C
        # neighbor is neutral, not the cold heat-caller this control needs.
        (METRIC_SYSTEM, MockHeadC, 18.0, 16.0, 5.0),
    ],
    ids=("fahrenheit", "celsius"),
)
async def test_custom_eco_invalid_priority_zone_does_not_outvote_healthy_neighbor(
    hass: HomeAssistant,
    unit_system,
    head_class,
    eco_cool_max: float,
    valid_primary: float,
    healthy_heat: float,
) -> None:
    """The 70 °F fallback must not COOL when the custom eco maximum is 65 °F."""
    hass.config.units = unit_system
    head_a, head_b = await _setup_mock_heads(hass, cls=head_class)
    await _set_sensor(hass, SENSOR_A, valid_primary)
    await _set_sensor(hass, SENSOR_B, healthy_heat)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_ECO_COOL_MAX: eco_cool_max,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data._last_mode_change_ts = 0.0

    for suffix in (
        "_primary_enable",
        "_secondary_enable",
        "_coordinator_enable",
        "_eco_idle",
    ):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": _eid(hass, entry, suffix)},
            blocking=True,
        )
    invalid_values = (
        "unknown",
        "unavailable",
        "not-a-number",
        "nan",
        "inf",
        "+inf",
        "-inf",
    )
    for invalid in invalid_values:
        await _set_sensor(hass, SENSOR_A, invalid)
        entry.runtime_data._last_mode_change_ts = 0.0
        await _recompute(hass, entry)

        plan = hass.states.get(_eid(hass, entry, "_plan"))
        assert plan.attributes["primary_demand"] == "neutral", invalid
        assert plan.attributes["primary_engage"] == "satisfied", invalid
        assert plan.attributes["secondary_demand"] == "heat", invalid
        assert plan.attributes["standoff"] is False, invalid
        assert plan.attributes["sensors_ok"] is False, invalid
        # No invented room temperature is displayed while the reading is
        # rejected; the healthy neighbor still publishes its real one.
        assert plan.attributes["zones"][0]["temp"] is None, invalid
        assert plan.attributes["zones"][1]["temp"] == pytest.approx(
            healthy_heat
        ), invalid
        assert plan.state == "heat", invalid
        assert hass.states.get(head_a).state == "off", invalid
        assert hass.states.get(head_b).state == "heat", invalid

    hass.states.async_remove(SENSOR_A)
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["primary_demand"] == "neutral"
    assert plan.attributes["secondary_demand"] == "heat"
    assert plan.attributes["zones"][0]["temp"] is None
    assert hass.states.get(head_a).state == "off"
    assert hass.states.get(head_b).state == "heat"

    recovery_value = (
        80.0
        if hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
        else 30.0
    )
    await _set_sensor(hass, SENSOR_A, recovery_value)
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["primary_demand"] == "cool"
    assert plan.attributes["sensors_ok"] is True
    assert plan.attributes["zones"][0]["temp"] == pytest.approx(recovery_value)
    assert hass.states.get(head_a).state == "cool"
    # The recovered primary wins the standoff; the cold secondary is a standoff
    # LOSER (engage heat, denied), not eco-satisfied, so head_action parks it at
    # the idle action — fan_only by default. Only eco-satisfied heads go off.
    assert hass.states.get(head_b).state == "fan_only"


async def test_preexisting_invalid_observation_parks_by_ordinary_idle_rules(
    hass: HomeAssistant,
) -> None:
    """An invalid state already present at setup is neutral and later recovers."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_sensor(hass, SENSOR_A, "unknown")
    await _set_sensor(hass, SENSOR_B, 75)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data._last_mode_change_ts = 0.0
    for suffix in (
        "_primary_enable",
        "_secondary_enable",
        "_coordinator_enable",
    ):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": _eid(hass, entry, suffix)},
            blocking=True,
        )
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["primary_demand"] == "neutral"
    assert plan.attributes["secondary_demand"] == "cool"
    assert hass.states.get(head_a).state == "fan_only"
    assert hass.states.get(head_b).state == "cool"

    await _set_sensor(hass, SENSOR_A, 60)
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["primary_demand"] == "heat"
    assert plan.attributes["sensors_ok"] is True
    assert hass.states.get(head_a).state == "heat"

    await _set_sensor(hass, SENSOR_A, "nan")
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["primary_demand"] == "neutral"
    assert plan.attributes["secondary_demand"] == "cool"
    assert hass.states.get(head_a).state == "fan_only"
    assert hass.states.get(head_b).state == "cool"


@pytest.mark.parametrize(
    "value",
    ("unknown", "unavailable", "not-a-number", "nan", "inf", "+inf", "-inf"),
)
def test_read_temp_rejects_invalid_and_non_finite_values(value: str) -> None:
    state = State(
        "sensor.room",
        value,
        {ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.FAHRENHEIT},
    )
    assert _read_temp(state, 70.0, UnitOfTemperature.FAHRENHEIT) == (False, 70.0)


@pytest.mark.parametrize(
    "unit", ("%", "W", "kWh", UnitOfTemperature.FAHRENHEIT.lower(), "degrees")
)
def test_read_temp_rejects_an_explicitly_unsupported_unit(unit: str) -> None:
    """A unit that is not °C/°F/K is not a temperature, whatever it parses to."""
    state = State("sensor.room", "20", {ATTR_UNIT_OF_MEASUREMENT: unit})
    assert read_room_temp(state, UnitOfTemperature.FAHRENHEIT) is None
    assert _read_temp(state, 70.0, UnitOfTemperature.FAHRENHEIT) == (False, 70.0)


@pytest.mark.parametrize(
    "unit", ([], {}, 0, b"\xc2\xb0F"), ids=("list", "dict", "int", "bytes")
)
def test_read_temp_rejects_a_malformed_unit_attribute_without_raising(
    unit: object,
) -> None:
    """A non-string unit is invalid the same way, and must never raise.

    HA's state machine stores whatever attribute a source publishes. A list or
    dict is not hashable, so testing it against ``VALID_UNITS`` before checking
    its type raised ``TypeError`` out of the reader — and, from a coordinator
    listener, out of the whole refresh.
    """
    for system_unit in (UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS):
        state = State("sensor.room", "20", {ATTR_UNIT_OF_MEASUREMENT: unit})
        assert read_room_temp(state, system_unit) is None
        assert _read_temp(state, 70.0, system_unit) == (False, 70.0)


@pytest.mark.parametrize(
    ("system_unit", "value"),
    (
        (UnitOfTemperature.FAHRENHEIT, 68.0),
        (UnitOfTemperature.CELSIUS, 20.0),
    ),
)
def test_read_temp_keeps_a_unitless_sensor_as_a_system_unit_value(
    system_unit: str, value: float
) -> None:
    """Compatibility: a sensor declaring NO unit reads as the system unit.

    Unitless template sensors worked before this change and must keep working;
    only an explicitly wrong unit is rejected. Both attribute-absent and an
    explicit ``None`` attribute are the same case.
    """
    for attributes in ({}, {ATTR_UNIT_OF_MEASUREMENT: None}):
        state = State("sensor.room", str(value), attributes)
        assert read_room_temp(state, system_unit) == pytest.approx(value)
        ok, actual = _read_temp(state, 70.0, system_unit)
        assert ok is True
        assert actual == pytest.approx(value)


@pytest.mark.parametrize("value", ("nan", "inf", "+inf", "-inf", "-nan"))
def test_read_room_temp_rejects_non_finite_values_the_facade_would_publish(
    value: str,
) -> None:
    """The shared reader is what keeps ``nan`` out of HA's display rounding."""
    state = State(
        "sensor.room", value, {ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.FAHRENHEIT}
    )
    assert read_room_temp(state, UnitOfTemperature.FAHRENHEIT) is None


@pytest.mark.parametrize(
    ("value", "state_unit", "system_unit", "expected"),
    (
        (68.0, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.FAHRENHEIT, 68.0),
        (20.0, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT, 68.0),
        (293.15, UnitOfTemperature.KELVIN, UnitOfTemperature.CELSIUS, 20.0),
    ),
)
def test_read_temp_converts_each_supported_state_unit_once(
    value: float, state_unit: str, system_unit: str, expected: float
) -> None:
    state = State(
        "sensor.room",
        str(value),
        {ATTR_UNIT_OF_MEASUREMENT: state_unit},
    )
    ok, actual = _read_temp(state, 70.0, system_unit)
    assert ok is True
    assert actual == pytest.approx(expected)


@pytest.mark.parametrize("value", (-1000.0, 1_000_000.0))
def test_read_temp_does_not_invent_a_room_climate_range(value: float) -> None:
    state = State(
        "sensor.room",
        str(value),
        {ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.FAHRENHEIT},
    )
    assert _read_temp(state, 70.0, UnitOfTemperature.FAHRENHEIT) == (True, value)


class NormalizedTemperatureSensor(SensorEntity):
    """A normal HA temperature sensor that reports native Celsius."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_unique_id = "normalized_temperature"
    _attr_name = "Normalized Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_value = 20.0


async def test_normal_ha_temperature_conversion_is_not_applied_twice(
    hass: HomeAssistant,
) -> None:
    """HA's native °C -> state °F conversion remains 68 °F after reading."""
    hass.config.units = US_CUSTOMARY_SYSTEM

    async def _setup_platform(hass, config, async_add_entities, discovery_info=None):
        async_add_entities([NormalizedTemperatureSensor()])

    mock_integration(hass, MockModule("normalized_temp"))
    mock_platform(
        hass,
        "normalized_temp.sensor",
        MockPlatform(async_setup_platform=_setup_platform),
    )
    assert await async_setup_component(
        hass,
        "sensor",
        {"sensor": {"platform": "normalized_temp"}},
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.normalized_temperature")
    assert state is not None
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTemperature.FAHRENHEIT
    assert float(state.state) == pytest.approx(68.0)
    ok, value = _read_temp(state, 70.0, UnitOfTemperature.FAHRENHEIT)
    assert ok is True
    assert value == pytest.approx(68.0)


async def _setup_two_zones(
    hass: HomeAssistant,
    head_class: type[MockHead] = MockHead,
    **extra: object,
) -> tuple[MockConfigEntry, str, str]:
    """Two enabled zones on mock heads, ready for a deterministic recompute."""
    head_a, head_b = await _setup_mock_heads(hass, cls=head_class)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    # Let the shared mode settle on the rooms' own demand instead of the
    # startup-armed hysteresis; these tests are about the sensor, not dwell.
    entry.runtime_data._last_mode_change_ts = 0.0
    return entry, head_a, head_b


async def _refreshes_to_command(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    entity_id: str,
    want: str,
    limit: int = 4,
) -> int:
    """How many refreshes the head needed to reach ``want``, counting from one.

    The caller has already run the refresh that carries the new inputs, so 1
    means that refresh commanded the head; each further recompute costs one
    more. Returns ``limit + 1`` if it never gets there, so a caller's bound
    fails instead of this helper looping.
    """
    for tick in range(1, limit + 1):
        if hass.states.get(entity_id).state == want:
            return tick
        await _recompute(hass, entry)
    return limit + 1


@pytest.mark.parametrize(
    ("unit_system", "head_class", "hot", "recovered"),
    [
        (US_CUSTOMARY_SYSTEM, MockHead, 80.0, 75.0),
        (METRIC_SYSTEM, MockHeadC, 27.0, 24.0),
    ],
    ids=("fahrenheit", "celsius"),
)
@pytest.mark.parametrize("bad", ("nan", "-nan", "inf", "+inf", "-inf"))
async def test_non_finite_sensor_never_breaks_publication_or_neighbors(
    hass: HomeAssistant,
    caplog,
    unit_system,
    head_class,
    hot: float,
    recovered: float,
    bad: str,
) -> None:
    """A non-finite reading must not take the refresh — or any other room — down.

    ``float("nan")`` PARSES, so a naive facade publishes it and HA's own
    ``display_temp`` round() raises inside ``async_write_ha_state``; because the
    tile writes from a coordinator listener, that exception used to escape the
    whole refresh and leave every zone undriven.
    """
    hass.config.units = unit_system
    await _set_sensor(hass, SENSOR_A, hot)
    await _set_sensor(hass, SENSOR_B, hot)
    entry, head_a, head_b = await _setup_two_zones(hass, head_class)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"  # control: both rooms driven

    caplog.clear()
    await _set_sensor(hass, SENSOR_A, bad)
    await _recompute(hass, entry)

    assert entry.runtime_data.last_update_success is True
    assert "Unexpected error fetching" not in caplog.text
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["sensors_ok"] is False
    assert plan.attributes["primary_demand"] == "neutral"
    assert plan.attributes["zones"][0]["temp"] is None
    # The healthy neighbour is still arbitrated and still driven.
    assert plan.attributes["secondary_demand"] == "cool"
    assert plan.attributes["zones"][1]["temp"] == pytest.approx(hot)
    assert plan.state == "cool"
    assert hass.states.get(head_b).state == "cool"
    assert hass.states.get(head_a).state == "fan_only"

    tile = hass.states.get(_eid(hass, entry, "_primary_thermostat"))
    assert tile.state != "unavailable"
    assert tile.attributes.get("current_temperature") is None

    # ...and it recovers on the next valid reading.
    await _set_sensor(hass, SENSOR_A, recovered)
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["sensors_ok"] is True
    assert plan.attributes["zones"][0]["temp"] == pytest.approx(recovered)
    assert hass.states.get(head_a).state == "cool"
    tile = hass.states.get(_eid(hass, entry, "_primary_thermostat"))
    assert tile.attributes["current_temperature"] == pytest.approx(recovered)


@pytest.mark.parametrize(
    ("unit_system", "head_class", "hot", "cold"),
    [
        (US_CUSTOMARY_SYSTEM, MockHead, 80.0, 60.0),
        (METRIC_SYSTEM, MockHeadC, 27.0, 16.0),
    ],
    ids=("fahrenheit", "celsius"),
)
@pytest.mark.parametrize("unit", ([], {}, 0), ids=("list", "dict", "int"))
async def test_malformed_unit_never_breaks_publication_or_neighbor_commands(
    hass: HomeAssistant,
    caplog,
    unit_system,
    head_class,
    hot: float,
    cold: float,
    unit: object,
) -> None:
    """A malformed unit attribute must not take the refresh — or a neighbor — down.

    A ``[]`` or ``{}`` ``unit_of_measurement`` used to raise ``TypeError`` inside
    the shared reader's membership test. That failed the entire refresh: the
    thermostat published unavailable and a healthy neighbor whose room had
    changed never received its new command. The neighbor here changes from hot
    to cold WHILE the malformed attribute is present, so a passing test proves
    its head was actually re-commanded, not merely left where it was.
    """
    hass.config.units = unit_system
    await _set_sensor(hass, SENSOR_A, hot)
    await _set_sensor(hass, SENSOR_B, hot)
    entry, head_a, head_b = await _setup_two_zones(hass, head_class)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"  # control: both rooms driven
    assert hass.states.get(head_b).state == "cool"

    caplog.clear()
    hass.states.async_set(SENSOR_A, str(hot), {ATTR_UNIT_OF_MEASUREMENT: unit})
    await _set_sensor(hass, SENSOR_B, cold)
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)

    assert entry.runtime_data.last_update_success is True
    assert "Unexpected error fetching" not in caplog.text
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["sensors_ok"] is False
    assert plan.attributes["primary_demand"] == "neutral"
    assert plan.attributes["zones"][0]["temp"] is None
    # The healthy neighbour's NEW demand is arbitrated and its head re-commanded.
    assert plan.attributes["secondary_demand"] == "heat"
    assert plan.attributes["zones"][1]["temp"] == pytest.approx(cold)
    assert plan.state == "heat"
    assert hass.states.get(head_a).state == "fan_only"

    # The invalid room's own publication, asserted on THIS refresh — before any
    # helper below is allowed to run another one. The tile must stay available
    # and must show unknown rather than a fabricated room temperature, whatever
    # the neighbour's engage latch is doing at this instant.
    tile = hass.states.get(_eid(hass, entry, "_primary_thermostat"))
    assert tile.state != "unavailable"
    assert tile.attributes.get("current_temperature") is None

    # The neighbour was running COOL, so this reading flips its engaged
    # direction, and `engage_with_latch` deliberately coasts one compute at
    # `satisfied` before re-engaging the other way (the anti-whiplash rule
    # pinned by test_latch_never_whiplashes_on_overshoot). Whether that coast
    # is already spent when this refresh returns is decided by Home Assistant,
    # not by this integration: through HA 2026.2 a state-change listener ran
    # one event-loop iteration AFTER the write, so the coordinator's own
    # fan-mode echo re-entered a second refresh inside this await; from HA
    # 2026.9 it runs inside the write, so the same request is debounced. Bound
    # the refresh instead of a version, and see
    # test_malformed_unit_costs_the_neighbour_no_extra_refresh: the malformed
    # unit itself costs this head nothing.
    assert await _refreshes_to_command(hass, entry, head_b, "heat") <= 2

    # Still unknown after however many refreshes that took: the malformed unit
    # is still present, so the room must not have acquired a temperature.
    tile = hass.states.get(_eid(hass, entry, "_primary_thermostat"))
    assert tile.state != "unavailable"
    assert tile.attributes.get("current_temperature") is None

    # ...and a valid unit on the next reading recovers the room.
    await _set_sensor(hass, SENSOR_A, cold)
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["sensors_ok"] is True
    assert plan.attributes["primary_demand"] == "heat"
    assert plan.attributes["zones"][0]["temp"] == pytest.approx(cold)
    assert hass.states.get(head_a).state == "heat"
    tile = hass.states.get(_eid(hass, entry, "_primary_thermostat"))
    assert tile.attributes["current_temperature"] == pytest.approx(cold)


@pytest.mark.parametrize(
    ("unit_system", "head_class", "hot", "cold"),
    [
        (US_CUSTOMARY_SYSTEM, MockHead, 80.0, 60.0),
        (METRIC_SYSTEM, MockHeadC, 27.0, 16.0),
    ],
    ids=("fahrenheit", "celsius"),
)
async def test_malformed_unit_costs_the_neighbour_no_extra_refresh(
    hass: HomeAssistant,
    unit_system,
    head_class,
    hot: float,
    cold: float,
) -> None:
    """A malformed unit on one room delays no other room by a single refresh.

    The neighbour above is commanded within two refreshes, and how many it
    actually takes is decided by the engage latch's anti-whiplash coast, which
    owes nothing to the invalid sensor. This measures that directly: the SAME
    room flip, once with both sensors healthy and once with a malformed unit on
    the other room, from the same latch state both times. The two counts must be
    equal, so a malformed unit can never buy itself an extra cycle of the
    neighbour's time — the property the single-refresh assertion was reaching
    for, without depending on when HA dispatches a state-change listener.
    """
    hass.config.units = unit_system
    await _set_sensor(hass, SENSOR_A, hot)
    await _set_sensor(hass, SENSOR_B, hot)
    entry, head_a, head_b = await _setup_two_zones(hass, head_class)
    coord = entry.runtime_data
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == "cool"  # control: the room is driven
    assert coord._engage_latch["secondary"] == "cool"

    # Control: the neighbour flips hot -> cold while the other room's sensor is
    # HEALTHY and reads its own target, so that room is neutral exactly as the
    # rejected reading makes it neutral below. (A healthy room still calling
    # cool would be a standoff, which parks the neighbour for a different and
    # entirely correct reason.)
    await _set_sensor(hass, SENSOR_A, coord.zones[0].target)
    await _set_sensor(hass, SENSOR_B, cold)
    coord._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    healthy_ticks = await _refreshes_to_command(hass, entry, head_b, "heat")

    # Put it back on a cool run, so the second flip starts where the first did.
    await _set_sensor(hass, SENSOR_A, hot)
    await _set_sensor(hass, SENSOR_B, hot)
    coord._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    assert await _refreshes_to_command(hass, entry, head_b, "cool") <= 2
    assert coord._engage_latch["secondary"] == "cool"

    # The same flip, now with a malformed unit on the OTHER room's sensor.
    hass.states.async_set(SENSOR_A, str(hot), {ATTR_UNIT_OF_MEASUREMENT: []})
    await _set_sensor(hass, SENSOR_B, cold)
    coord._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    malformed_ticks = await _refreshes_to_command(hass, entry, head_b, "heat")

    assert coord.data["sensors_ok"] is False  # the malformed unit was in play
    assert coord.data["standoff"] is False  # ...and neither run was a standoff
    assert hass.states.get(head_a).state == "fan_only"
    assert malformed_ticks == healthy_ticks
    assert malformed_ticks <= 2


@pytest.mark.parametrize(
    ("value", "attributes", "expected"),
    (
        ("20.0", {ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.FAHRENHEIT}, 20.0),
        ("20.0", {ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.CELSIUS}, 68.0),
        ("293.15", {ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.KELVIN}, 68.0),
        ("20.0", {}, 20.0),
        ("20.0", {ATTR_UNIT_OF_MEASUREMENT: "%"}, None),
        ("20.0", {ATTR_UNIT_OF_MEASUREMENT: "W"}, None),
    ),
    ids=("system-unit", "celsius", "kelvin", "unitless", "percent", "watts"),
)
async def test_tile_shows_exactly_what_the_plan_acted_on(
    hass: HomeAssistant, value: str, attributes: dict, expected: float | None
) -> None:
    """The facade and the demand path share one reader — and one conversion.

    Each state is read by both surfaces on a °F system; whatever the declared
    unit, the tile can never show a value the coordinator rejected, a value in
    a unit other than the °F this entity declares, or a twice-converted one.
    HA converts a climate entity's ``current_temperature`` out of the unit the
    entity declares, so matching the expected °F number also proves the tile
    declares °F. Expected values are whole °F: HA rounds the displayed
    temperature to the system precision.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set(SENSOR_A, value, attributes)
    await _set_sensor(hass, SENSOR_B, 70.0)
    await hass.async_block_till_done()
    entry, _head_a, _head_b = await _setup_two_zones(hass)
    await _recompute(hass, entry)

    tile = hass.states.get(_eid(hass, entry, "_primary_thermostat"))
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    if expected is None:
        assert tile.attributes.get("current_temperature") is None
        assert plan.attributes["zones"][0]["temp"] is None
        assert plan.attributes["sensors_ok"] is False
        assert plan.attributes["primary_demand"] == "neutral"
    else:
        assert tile.attributes["current_temperature"] == pytest.approx(expected)
        assert plan.attributes["zones"][0]["temp"] == pytest.approx(expected)
        assert plan.attributes["sensors_ok"] is True
    # Both surfaces agree, always: the tile IS the plan's value.
    assert tile.attributes.get("current_temperature") == plan.attributes["zones"][0]["temp"]


async def test_invalid_sensor_leaves_the_fan_boost_arithmetic_safe(
    hass: HomeAssistant,
) -> None:
    """No displayed temperature still means safe actuator arithmetic.

    ``_apply`` derives the fan-boost delta from the room's plan temperature; a
    rejected reading publishes ``None`` there, and this proves the apply path
    neither raises on it nor invents a distance-off-target.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    await _set_sensor(hass, SENSOR_A, 82)  # far off target -> boost climbs
    await _set_sensor(hass, SENSOR_B, 70)
    entry, head_a, _head_b = await _setup_two_zones(hass)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    boosted = hass.states.get(head_a).attributes["fan_mode"]
    assert boosted != "auto"  # control: the ladder really did climb

    await _set_sensor(hass, SENSOR_A, "nan")
    await _recompute(hass, entry)

    assert entry.runtime_data.last_update_success is True
    assert hass.states.get(head_a).state == "fan_only"
    # Parked, and the boost handed back to the firmware's own auto.
    assert hass.states.get(head_a).attributes["fan_mode"] == "auto"
    assert entry.runtime_data._fan_idx.get(head_a) is None


@pytest.mark.parametrize(
    ("unit_system", "head_class", "cold", "eco_heat_min"),
    [
        # Cold values sit strictly BELOW each profile's default eco heat floor
        # (const.py DEFAULT_ECO_HEAT_MIN 50.0 °F / _UNIT_PROFILE_CELSIUS 10.0 °C).
        (US_CUSTOMARY_SYSTEM, MockHead, 45.0, 50.0),
        (METRIC_SYSTEM, MockHeadC, 5.0, 10.0),
    ],
    ids=("fahrenheit", "celsius"),
)
async def test_eco_parks_an_invalid_room_off_while_a_healthy_room_heats(
    hass: HomeAssistant, unit_system, head_class, cold: float, eco_heat_min: float
) -> None:
    """Eco's park is `off`, not the idle action — the README's exact claim.

    The healthy room proves the eco extremes still work on real readings in
    both unit systems while the invalid room abstains.
    """
    hass.config.units = unit_system
    await _set_sensor(hass, SENSOR_A, cold)
    await _set_sensor(hass, SENSOR_B, cold)
    entry, head_a, head_b = await _setup_two_zones(hass, head_class)
    await _recompute(hass, entry)
    # Control: with a valid cold reading BOTH rooms call heat under eco.
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_eco_idle")}, blocking=True
    )
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert entry.runtime_data.eco_heat_min == pytest.approx(eco_heat_min)
    assert plan.attributes["primary_demand"] == "heat"
    assert plan.attributes["secondary_demand"] == "heat"
    assert hass.states.get(head_a).state == "heat"

    await _set_sensor(hass, SENSOR_A, "not-a-number")
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["primary_demand"] == "neutral"
    assert plan.attributes["primary_engage"] == "satisfied"
    assert plan.attributes["zones"][0]["temp"] is None
    assert plan.attributes["secondary_demand"] == "heat"
    assert plan.state == "heat"
    assert hass.states.get(head_a).state == "off"  # eco-satisfied, not idle
    assert hass.states.get(head_b).state == "heat"


@pytest.mark.parametrize(
    ("unit_system", "head_class", "cold"),
    [
        (US_CUSTOMARY_SYSTEM, MockHead, 60.0),
        (METRIC_SYSTEM, MockHeadC, 16.0),
    ],
    ids=("fahrenheit", "celsius"),
)
async def test_a_unitless_sensor_still_drives_its_room(
    hass: HomeAssistant, unit_system, head_class, cold: float
) -> None:
    """Compatibility control for the missing-unit rule, end to end.

    A room sensor with no `unit_of_measurement` — the ordinary unitless
    template sensor — keeps working exactly as it did before this change, in
    both unit systems, while an explicitly wrong unit on the same value parks
    the room.
    """
    hass.config.units = unit_system
    hass.states.async_set(SENSOR_A, str(cold))  # no attributes at all
    await _set_sensor(hass, SENSOR_B, cold)
    await hass.async_block_till_done()
    entry, head_a, _head_b = await _setup_two_zones(hass, head_class)
    await _recompute(hass, entry)

    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["sensors_ok"] is True
    assert plan.attributes["primary_demand"] == "heat"
    assert plan.attributes["zones"][0]["temp"] == pytest.approx(cold)
    assert hass.states.get(head_a).state == "heat"

    # Same number, now declared in a unit that is not a temperature.
    hass.states.async_set(SENSOR_A, str(cold), {ATTR_UNIT_OF_MEASUREMENT: "%"})
    await hass.async_block_till_done()
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["sensors_ok"] is False
    assert plan.attributes["primary_demand"] == "neutral"
    assert plan.attributes["zones"][0]["temp"] is None
    assert hass.states.get(head_a).state == "fan_only"
