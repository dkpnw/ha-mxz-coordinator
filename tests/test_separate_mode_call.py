"""The mode call for heads off the ESPHome platform (forum report by @mw1029).

Kumo Cloud heads reached through comfort_HA drop the hvac_mode HA passes
inside climate.set_temperature. MXZ parks a head with set_hvac_mode, so it
could park them in fan_only or off but never wake them. A head on any
platform other than esphome now gets set_hvac_mode first, then
set_temperature with only its setpoint(s). An ESPHome (CN105) head keeps
its one combined call, and so does a head with no registry entry.

The cloud heads here are invented fakes on an invented platform, not a copy
of comfort_HA. They model only the reported behavior: hvac_mode inside
set_temperature is dropped. Each fake's own reported state is the oracle,
because only a real set_hvac_mode can wake it.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
)
from tests.test_drive import (
    EVENT_CALL_SERVICE,
    SENSOR_A,
    SENSOR_B,
    MockHead,
    _eid,
    _recompute,
    _set_temp,
    _user_set_fan,
)

# Invented: any platform name other than esphome takes the separate call.
CLOUD = "cloud_fake"
ESPHOME = "esphome"
PARK = {IDLE_ACTION_FAN_ONLY: "fan_only", IDLE_ACTION_OFF: "off"}
# The room sensor reading that makes each mode run against the 70 °F target.
DRIVE = {"cool": 75, "heat": 65}
WRITES = ("set_hvac_mode", "set_temperature")


class Head(MockHead):
    """The CN105 test head, plus the two cloud behaviors these tests need."""

    # comfort_HA drops the hvac_mode HA passes inside set_temperature.
    drops_mode = False
    # A cloud call that fails: only waking modes, so the head can still park.
    refuse_wake = False

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if self.refuse_wake and hvac_mode in (HVACMode.COOL, HVACMode.HEAT):
            raise HomeAssistantError("invented cloud rejection")
        await super().async_set_hvac_mode(hvac_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if self.drops_mode:
            kwargs.pop("hvac_mode", None)
        if (temperature := kwargs.get("temperature")) is not None:
            self._attr_native_target_temperature = temperature
        await super().async_set_temperature(**kwargs)


class SingleHead(Head):
    """A head with one setpoint and no low/high pair."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )


def _head(cls: type[Head], suffix: str, platform: str) -> Head:
    head = cls(suffix)
    head.drops_mode = platform != ESPHOME
    return head


async def _register(hass: HomeAssistant, heads: dict[str, list[Head]]) -> list[Head]:
    """Register heads per platform; return them in entity order."""
    hass.config.units = US_CUSTOMARY_SYSTEM

    def _adder(group: list[Head]):
        async def _setup_platform(hass, config, async_add_entities, discovery_info=None):
            async_add_entities(group)

        return _setup_platform

    for platform, group in heads.items():
        mock_integration(hass, MockModule(platform))
        mock_platform(
            hass, f"{platform}.climate", MockPlatform(async_setup_platform=_adder(group))
        )
    assert await async_setup_component(
        hass, "climate", {"climate": [{"platform": platform} for platform in heads]}
    )
    await hass.async_block_till_done()
    return sorted(
        (head for group in heads.values() for head in group),
        key=lambda head: head.unique_id,
    )


async def _setup(
    hass: HomeAssistant,
    heads: dict[str, list[Head]],
    idle_action: str = IDLE_ACTION_FAN_ONLY,
) -> MockConfigEntry:
    """Two rooms at target on these heads; coordinator and rooms enabled."""
    ordered = await _register(hass, heads)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: ordered[0].entity_id,
            CONF_SECONDARY_CLIMATE: ordered[1].entity_id,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: True,
            CONF_IDLE_ACTION: idle_action,
            CONF_MODE_HYSTERESIS: 0,
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
    return entry


def _record(hass: HomeAssistant) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    hass.bus.async_listen(
        EVENT_CALL_SERVICE, callback(lambda e: calls.append(dict(e.data)))
    )
    return calls


def _writes(calls: list[dict[str, Any]], head: str) -> list[tuple[str, dict[str, Any]]]:
    """Every mode/setpoint call to one head, with its full service data."""
    return [
        (c["service"], dict(c["service_data"]))
        for c in calls
        if c["domain"] == "climate"
        and c["service"] in WRITES
        and c["service_data"].get("entity_id") == head
    ]


def _setpoints(state: Any) -> dict[str, Any]:
    keys = ("temperature",) if state.attributes.get("target_temp_low") is None else (
        "target_temp_low", "target_temp_high",
    )
    return {key: state.attributes[key] for key in keys}


async def _park_then_drive(
    hass: HomeAssistant, entry: MockConfigEntry, heads: list[str], park: str, act: str,
) -> list[dict[str, Any]]:
    """Park every head, then make both rooms want ``act``; return the calls."""
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": heads, "hvac_mode": park}, blocking=True
    )
    await _recompute(hass, entry)
    for head in heads:  # the plan agrees: a satisfied room stays parked
        assert hass.states.get(head).state == park
    calls = _record(hass)
    await _set_temp(hass, SENSOR_A, DRIVE[act])
    await _set_temp(hass, SENSOR_B, DRIVE[act])
    await _recompute(hass, entry)
    return calls


@pytest.mark.parametrize("cls", [Head, SingleHead])
@pytest.mark.parametrize("act", ["heat", "cool"])
@pytest.mark.parametrize("idle_action", [IDLE_ACTION_FAN_ONLY, IDLE_ACTION_OFF])
async def test_cloud_head_wakes_and_esphome_head_keeps_one_call(
    hass: HomeAssistant, idle_action: str, act: str, cls: type[Head],
) -> None:
    """One install, one plan: each head gets the call its platform honors."""
    entry = await _setup(
        hass, {ESPHOME: [_head(cls, "a", ESPHOME)], CLOUD: [_head(cls, "b", CLOUD)]},
        idle_action,
    )
    local, cloud = "climate.mock_head_a", "climate.mock_head_b"
    calls = await _park_then_drive(hass, entry, [local, cloud], PARK[idle_action], act)

    local_state, cloud_state = hass.states.get(local), hass.states.get(cloud)
    # The cloud head woke: it dropped any hvac_mode in set_temperature, so
    # only its own set_hvac_mode could have done it.
    assert cloud_state.state == act
    assert local_state.state == act
    # Both rooms are alike, so both heads hold the same setpoints.
    setpoints = _setpoints(local_state)
    assert _setpoints(cloud_state) == setpoints
    if act == "cool":
        assert setpoints.get("target_temp_high", setpoints.get("temperature")) == 70
    # ESPHome: today's single combined call, payload in today's key order.
    local_writes = _writes(calls, local)
    assert local_writes == [
        ("set_temperature", {"entity_id": local, "hvac_mode": act, **setpoints}),
    ]
    assert list(local_writes[0][1]) == ["entity_id", "hvac_mode", *setpoints]
    # Cloud: the mode first, then the same setpoints with no hvac_mode.
    assert _writes(calls, cloud) == [
        ("set_hvac_mode", {"entity_id": cloud, "hvac_mode": act}),
        ("set_temperature", {"entity_id": cloud, **setpoints}),
    ]


@pytest.mark.parametrize("cls", [Head, SingleHead])
async def test_esphome_only_install_sends_no_mode_call(
    hass: HomeAssistant, cls: type[Head],
) -> None:
    """An all-CN105 install wakes both heads with one combined call each."""
    entry = await _setup(
        hass, {ESPHOME: [_head(cls, "a", ESPHOME), _head(cls, "b", ESPHOME)]},
    )
    heads = ["climate.mock_head_a", "climate.mock_head_b"]
    calls = await _park_then_drive(hass, entry, heads, "fan_only", "heat")
    for head in heads:
        state = hass.states.get(head)
        assert state.state == "heat"
        assert _writes(calls, head) == [
            ("set_temperature", {"entity_id": head, "hvac_mode": "heat", **_setpoints(state)}),
        ]


async def test_cloud_fake_drops_the_combined_mode(hass: HomeAssistant) -> None:
    """The fake reproduces the report: the combined call leaves it parked."""
    await _register(hass, {CLOUD: [_head(Head, "a", CLOUD)], ESPHOME: [_head(Head, "b", ESPHOME)]})
    for head in ("climate.mock_head_a", "climate.mock_head_b"):
        await hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": head, "hvac_mode": "fan_only"}, blocking=True,
        )
        await hass.services.async_call(
            "climate", "set_temperature",
            {"entity_id": head, "hvac_mode": "cool", "target_temp_low": 66, "target_temp_high": 70},
            blocking=True,
        )
    await hass.async_block_till_done()
    cloud, local = hass.states.get("climate.mock_head_a"), hass.states.get("climate.mock_head_b")
    assert cloud.state == "fan_only"
    assert cloud.attributes["target_temp_high"] == 70
    assert local.state == "cool"


async def test_cloud_head_already_in_mode_gets_only_the_setpoint_call(
    hass: HomeAssistant,
) -> None:
    """No mode change, no mode call: a setpoint move is today's one call."""
    entry = await _setup(
        hass, {CLOUD: [_head(Head, "a", CLOUD), _head(Head, "b", CLOUD)]},
    )
    head = "climate.mock_head_a"
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head).state == "cool"

    calls = _record(hass)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, entry, "_primary_target"), "value": 68}, blocking=True,
    )
    await _recompute(hass, entry)
    state = hass.states.get(head)
    assert state.state == "cool"
    assert state.attributes["target_temp_high"] == 68
    assert [service for service, _ in _writes(calls, head)] == ["set_temperature"]


async def test_failed_mode_call_sends_no_setpoint_and_the_next_compute_retries(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused wake leaves the head parked as it was; a later compute wakes it."""
    cloud_a = _head(Head, "a", CLOUD)
    entry = await _setup(hass, {CLOUD: [cloud_a, _head(Head, "b", CLOUD)]})
    head = "climate.mock_head_a"
    cloud_a.refuse_wake = True
    calls = await _park_then_drive(hass, entry, [head], "fan_only", "cool")

    # Each compute retried the mode; none followed it with a setpoint.
    writes = _writes(calls, head)
    assert writes
    assert all(
        write == ("set_hvac_mode", {"entity_id": head, "hvac_mode": "cool"}) for write in writes
    )
    state = hass.states.get(head)
    assert state.state == "fan_only"
    assert state.attributes["target_temp_high"] is None
    assert "applying cool to climate.mock_head_a failed" in caplog.text
    # The peer room is unaffected.
    assert hass.states.get("climate.mock_head_b").state == "cool"

    cloud_a.refuse_wake = False
    await _recompute(hass, entry)
    state = hass.states.get(head)
    assert state.state == "cool"
    assert state.attributes["target_temp_high"] == 70


@pytest.mark.parametrize("platform", [ESPHOME, CLOUD])
async def test_manual_fan_pick_survives_the_wake(
    hass: HomeAssistant, platform: str,
) -> None:
    """A person's fan speed holds across park and wake, on either path."""
    entry = await _setup(
        hass, {platform: [_head(Head, "a", platform), _head(Head, "b", platform)]},
    )
    head = "climate.mock_head_a"
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head).state == "cool"
    await _user_set_fan(hass, head, "medium")

    calls = _record(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head).state == "fan_only"
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)

    state = hass.states.get(head)
    assert state.state == "cool"
    assert state.attributes["fan_mode"] == "medium"
    assert not [
        c for c in calls
        if c["service"] == "set_fan_mode" and c["service_data"].get("entity_id") == head
    ]


@pytest.mark.parametrize("platform", [ESPHOME, CLOUD])
async def test_reload_keeps_a_running_head_and_wakes_a_parked_one(
    hass: HomeAssistant, platform: str,
) -> None:
    """A new coordinator over live heads: no mode call to a running head."""
    entry = await _setup(
        hass, {platform: [_head(Head, "a", platform), _head(Head, "b", platform)]},
    )
    running, parked = "climate.mock_head_a", "climate.mock_head_b"
    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(running).state == "cool"
    assert hass.states.get(parked).state == "fan_only"

    await _set_temp(hass, SENSOR_B, 75)  # arrives while MXZ restarts
    calls = _record(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _recompute(hass, entry)

    assert hass.states.get(running).state == "cool"
    assert [service for service, _ in _writes(calls, running)] == []
    state = hass.states.get(parked)
    assert state.state == "cool"
    setpoints = _setpoints(state)
    expected = (
        [("set_temperature", {"entity_id": parked, "hvac_mode": "cool", **setpoints})]
        if platform == ESPHOME
        else [
            ("set_hvac_mode", {"entity_id": parked, "hvac_mode": "cool"}),
            ("set_temperature", {"entity_id": parked, **setpoints}),
        ]
    )
    assert _writes(calls, parked) == expected


async def test_head_without_registry_entry_keeps_the_combined_call(
    hass: HomeAssistant,
) -> None:
    """An unidentifiable head keeps today's path: one combined call."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    calls: list[ServiceCall] = []

    async def _record_call(call: ServiceCall) -> None:
        calls.append(call)

    attrs = {"supported_features": 2, "target_temp_low": 66, "target_temp_high": 74}
    hass.states.async_set("climate.primary", "fan_only", attrs)
    hass.states.async_set("climate.secondary", "fan_only", attrs)
    await _set_temp(hass, SENSOR_A, 75)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: "climate.primary",
            CONF_SECONDARY_CLIMATE: "climate.secondary",
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_MODE_HYSTERESIS: 0,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # After setup: loading MXZ's own thermostats sets up HA's climate services.
    for service in WRITES:
        hass.services.async_register("climate", service, _record_call)
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await _recompute(hass, entry)

    primary = [
        (c.service, dict(c.data)) for c in calls
        if c.service in WRITES and c.data["entity_id"] == "climate.primary"
    ]
    assert primary
    assert {service for service, _ in primary} == {"set_temperature"}
    assert all(data["hvac_mode"] == "cool" for _, data in primary)
