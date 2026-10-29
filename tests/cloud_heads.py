"""Invented cloud heads for the cloud-head (beta) tests, and their setup.

Two fakes, each modeling only the behavior MXZ has to cope with. Neither is
a copy of its integration's code; what each models, and where it came from:

* ``ComfortHead``, like a Kumo Cloud head through jjustinwilson/comfort_HA
  (platform ``kumo_cloud``, read 2026-10-29): it ignores the hvac_mode HA
  passes inside set_temperature (@mw1029's forum report) and applies a lone
  ``temperature`` to the mode it is in now, so a setpoint sent while it is in
  fan_only or off is lost. One setpoint as MXZ drives it, though it advertises
  the low/high range, as comfort_HA does when the unit can heat. 1 °F step, no
  hvac_action. ``delayed`` heads report only when polled.
* ``MelcloudHead``, like a MELCloud head (platform ``melcloud``): °C native,
  0.5 °C step, reports only when polled (60 s in the real integration; here a
  test calls ``poll``), silently drops writes while ``rate_limited``, and can
  go unavailable.

Both use only HA's native_* temperature names (see ``tests/head_entity.py``).
The fake's ``device`` is what the cloud holds; HA's state is what the fake
last reported. A test reads the device as the oracle of what landed.
"""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.setup import async_setup_component
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
    CONF_ZONES,
    DOMAIN,
    IDLE_ACTION_FAN_ONLY,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
)
from tests.head_entity import HeadEntity
from tests.test_drive import EVENT_CALL_SERVICE, _eid

COMFORT = "kumo_cloud"
MELCLOUD = "melcloud"
ESPHOME = "esphome"
SENSORS = ("sensor.room_a_temp", "sensor.room_b_temp", "sensor.room_c_temp")
HEAD_WRITES = ("set_hvac_mode", "set_temperature", "set_fan_mode")


class _CloudFake(HeadEntity):
    """What both fakes share: a device in the cloud and a reported state."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _enable_turn_on_off_backwards_compatibility = False
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [
        HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.DRY,
        HVACMode.FAN_ONLY, HVACMode.HEAT_COOL,
    ]
    delayed = True

    def __init__(self, suffix: str, *, mode: str = HVACMode.OFF, temperature: float = 70.0) -> None:
        self._attr_unique_id = f"{type(self).__name__.lower()}_{suffix}"
        self._attr_name = f"{type(self).__name__} {suffix}"
        # A head that advertises a range must have one to show; MXZ never sets it.
        self._attr_native_target_temperature_low = None
        self._attr_native_target_temperature_high = None
        self.device: dict[str, Any] = {"mode": mode, "temperature": temperature, "fan": "auto"}
        # Every service call that reached the device, applied or dropped.
        self.received: list[tuple[str, dict[str, Any]]] = []
        self._show(self.device)

    def _show(self, values: dict[str, Any]) -> None:
        self._attr_hvac_mode = values["mode"]
        self._attr_native_target_temperature = values["temperature"]
        self._attr_fan_mode = values["fan"]

    @callback
    def poll(self) -> None:
        """One poll: report what the device holds, changed or not."""
        self._show(self.device)
        self.async_write_ha_state()

    @callback
    def report(self, **values: Any) -> None:
        """A report of some other state (a stale poll), the device untouched."""
        self._show({**self.device, **values})
        self.async_write_ha_state()

    def _landed(self) -> None:
        if not self.delayed:
            self.poll()

    def _accepts(self) -> bool:
        return True

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self.received.append(("set_hvac_mode", {"hvac_mode": hvac_mode}))
        if self._accepts():
            self.device["mode"] = hvac_mode
        self._landed()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self.received.append(("set_fan_mode", {"fan_mode": fan_mode}))
        if self._accepts():
            self.device["fan"] = fan_mode
        self._landed()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        self.received.append(("set_temperature", dict(kwargs)))
        # The mode HA passes in here is dropped, and a lone setpoint lands
        # only on a mode that has one: comfort_HA builds no command at all
        # for fan_only or off.
        temperature = kwargs.get("temperature")
        if (
            self._accepts() and temperature is not None
            and self.device["mode"] in (HVACMode.HEAT, HVACMode.COOL)
        ):
            self.device["temperature"] = temperature
        self._landed()


class ComfortHead(_CloudFake):
    """A Kumo Cloud head as comfort_HA presents it (see the module docstring)."""

    _attr_native_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_target_temperature_step = 1.0
    _attr_min_temp = 61.0
    _attr_max_temp = 86.0
    _attr_fan_modes: ClassVar[list[str]] = ["auto", "quiet", "low", "medium", "high", "powerful"]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    # comfort_HA writes its command into its own cache and reports it at
    # once; a delayed one reports only when polled.
    delayed = False


class MelcloudHead(_CloudFake):
    """A MELCloud head (see the module docstring)."""

    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 10.0
    _attr_max_temp = 31.0
    _attr_fan_modes: ClassVar[list[str]] = ["auto", "1", "2", "3", "4", "5"]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    rate_limited = False

    def __init__(self, suffix: str, *, mode: str = HVACMode.OFF, temperature: float = 21.0) -> None:
        super().__init__(suffix, mode=mode, temperature=temperature)

    def _accepts(self) -> bool:
        return not self.rate_limited

    @callback
    def go_unavailable(self, unavailable: bool = True) -> None:
        self._attr_available = not unavailable
        self.async_write_ha_state()


async def register(hass: HomeAssistant, heads: dict[str, list[Any]]) -> None:
    """Register each platform's heads, as their own integrations would."""

    def _adder(group: list[Any]):
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


async def set_room(hass: HomeAssistant, sensor: str, value: float) -> None:
    hass.states.async_set(
        sensor, str(value), {"unit_of_measurement": hass.config.units.temperature_unit}
    )
    await hass.async_block_till_done()


async def setup_rooms(
    hass: HomeAssistant,
    rooms: list[tuple[Any, dict[str, Any]]],
    *,
    temps: tuple[float, ...] | None = None,
    options: dict[str, Any] | None = None,
    enable: bool = True,
) -> MockConfigEntry:
    """An entry over ``rooms`` ((head, extra zone keys), in priority order).

    Every room starts at ``temps`` (default: at the unit's 70 °F / 21 °C
    target); the coordinator and every room are enabled unless ``enable`` is
    False.
    """
    default = 21.0 if hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS else 70.0
    temps = temps or (default,) * len(rooms)
    for sensor, value in zip(SENSORS, temps):
        await set_room(hass, sensor, value)
    zones = [
        {
            ZONE_NAME: f"Room {index + 1}",
            ZONE_CLIMATE: head.entity_id,
            ZONE_SENSOR: SENSORS[index],
            **extra,
        }
        for index, (head, extra) in enumerate(rooms)
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_ZONES: zones,
            CONF_FAN_BOOST_ENABLE: True,
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_MODE_HYSTERESIS: 0,
            **(options or {}),
        },
        version=2,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if enable:
        slugs = ("primary", "secondary", "zone_3")[: len(rooms)]
        for suffix in (*(f"_{slug}_enable" for slug in slugs), "_coordinator_enable"):
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
            )
        await hass.async_block_till_done()
    return entry


def record(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every service call from here on (domain, service, service_data)."""
    calls: list[dict[str, Any]] = []
    hass.bus.async_listen(EVENT_CALL_SERVICE, callback(lambda e: calls.append(dict(e.data))))
    return calls


def writes(calls: list[dict[str, Any]], head: str) -> list[tuple[str, dict[str, Any]]]:
    """Every climate write to one head, its entity_id left out."""
    return [
        (
            call["service"],
            {k: v for k, v in call["service_data"].items() if k != "entity_id"},
        )
        for call in calls
        if call["domain"] == "climate" and call["service"] in HEAD_WRITES
        and call["service_data"].get("entity_id") == head
    ]


def age_cloud(coordinator: Any, head: str, seconds: float) -> None:
    """Make everything a cloud head's record timed ``seconds`` older.

    The record runs on ``time.monotonic()``, like every MXZ wait, so a test
    moves its stamps back rather than writing a time (see tests/clocks.py).
    """
    cloud = coordinator._cloud[head]
    cloud.window_until -= seconds
    for name in ("last_batch", "fan_at", "release_at"):
        if (stamp := getattr(cloud, name)) is not None:
            setattr(cloud, name, stamp - seconds)


async def recompute(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """One deterministic compute and apply (production is debounced)."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def row(hass: HomeAssistant, entry: MockConfigEntry, index: int) -> dict[str, Any]:
    """One room's row on the plan sensor."""
    return hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][index]


async def past_startup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """End every cloud head's startup confirm wait, then recompute once.

    A new coordinator holds its cloud heads for one confirm wait (a batch a
    moment before a reload may still be pending at the head).
    """
    coordinator = entry.runtime_data
    for head, cloud in coordinator._cloud.items():
        age_cloud(coordinator, head, cloud.confirm_wait)
    await recompute(hass, entry)


def past_window(entry: MockConfigEntry, head: str, seconds: float | None = None) -> None:
    """Age one cloud head's record past its confirm wait (or by ``seconds``)."""
    coordinator = entry.runtime_data
    age_cloud(coordinator, head, coordinator._cloud[head].confirm_wait if seconds is None else seconds)
