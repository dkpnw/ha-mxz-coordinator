"""Invented equipment for the throwaway Home Assistant: heads, room sensors, windows.

Development only. It is copied into a throwaway instance's config directory and
never ships: hassfest, HACS and the release zip see only custom_components/.
A person at a head acts through this integration's own services, the way a
remote changes a head without any climate service MXZ could see.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .devices import DEVICE_FILE, Devices, load_house

DOMAIN = "invented_house"
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
PLATFORMS = ("climate", "sensor", "binary_sensor")


def signal(key: str) -> str:
    return f"{DOMAIN}_{key}"


class Bench:
    """The house's devices and the one place that changes and stores them."""

    def __init__(self, hass: HomeAssistant, house: dict[str, Any], devices: Devices, path: Path) -> None:
        self.hass = hass
        self.house = house
        self.devices = devices
        self._path = path
        self._writing = asyncio.Lock()

    async def changed(self, key: str) -> None:
        async with self._writing:
            await self.hass.async_add_executor_job(self.devices.save, self._path)
        async_dispatcher_send(self.hass, signal(key))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    house = await hass.async_add_executor_job(load_house)
    path = Path(hass.config.path(DEVICE_FILE))
    devices = await hass.async_add_executor_job(Devices.load, path, house)
    bench = Bench(hass, house, devices, path)
    hass.data[DOMAIN] = bench
    await hass.async_add_executor_job(devices.save, path)
    rooms = [room["key"] for room in house["rooms"]]
    head = house["head"]

    async def remote(call: ServiceCall) -> None:
        state = bench.devices.heads[call.data["head"]]
        for field in ("hvac_mode", "target_temp_low", "target_temp_high", "fan_mode"):
            if field in call.data:
                setattr(state, field, call.data[field])
        await bench.changed(call.data["head"])

    async def room(call: ServiceCall) -> None:
        state = bench.devices.rooms[call.data["room"]]
        if "temperature" in call.data:
            state.temperature = call.data["temperature"]
        if "window_open" in call.data:
            state.window_open = call.data["window_open"]
        await bench.changed(call.data["room"])

    async def availability(call: ServiceCall) -> None:
        bench.devices.heads[call.data["head"]].available = call.data["available"]
        await bench.changed(call.data["head"])

    hass.services.async_register(DOMAIN, "remote", remote, vol.Schema({
        vol.Required("head"): vol.In(rooms),
        vol.Optional("hvac_mode"): vol.In(head["hvac_modes"]),
        vol.Optional("target_temp_low"): vol.Coerce(float),
        vol.Optional("target_temp_high"): vol.Coerce(float),
        vol.Optional("fan_mode"): vol.In(head["fan_modes"]),
    }))
    hass.services.async_register(DOMAIN, "room", room, vol.Schema({
        vol.Required("room"): vol.In(rooms),
        vol.Optional("temperature"): vol.Coerce(float),
        vol.Optional("window_open"): cv.boolean,
    }))
    hass.services.async_register(DOMAIN, "availability", availability, vol.Schema({
        vol.Required("head"): vol.In(rooms),
        vol.Required("available"): cv.boolean,
    }))
    for platform in PLATFORMS:
        hass.async_create_task(async_load_platform(hass, platform, DOMAIN, {}, config))
    return True
