"""DryMode on its own: a head report that can be an older state ends nothing.

A cloud head's report inside its confirm wait can be a poll from before MXZ's last batch
(``CloudHead.stale``). The coordinator passes that as ``stale``. Such a report must not undo
a person's Heat/Cool pick on the thermostat, re-arm a park notice already sent, or land a
Dry pick. The same report, when it is current, does each of those; that is the control.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, State

from custom_components.mxz_coordinator.coordinator import Zone
from custom_components.mxz_coordinator.dry import DryMode

HEAD = "climate.den"
MODES = ["off", "heat", "cool", "dry", "fan_only", "heat_cool"]


class _Sender:
    """The delivery queue's ``async_send``, sending nothing."""

    async def async_send(self, *args: Any, **kwargs: Any) -> None:
        pass


class _Host:
    """The members DryHost names, with nothing behind them but a record of notices."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.retired = False
        self.coordinator_enable = True
        self.inhibited = False
        self.deliveries = _Sender()
        self.notices: list[str] = []

    def clock(self) -> float:
        return 0.0

    async def notify(self, message: str, target: str | None = None) -> None:
        self.notices.append(message)

    def redraw_rooms(self) -> None:
        pass

    async def async_refresh_now(self) -> None:
        pass

    async def async_request_refresh(self) -> None:
        pass

    def cloud_head(self, climate_id: str) -> Any:
        return None


def _head(mode: str) -> State:
    return State(HEAD, mode, {"hvac_modes": MODES})


async def _dry(hass: HomeAssistant, mode: str) -> tuple[DryMode, _Host, Zone]:
    hass.states.async_set(HEAD, mode, {"hvac_modes": MODES})
    host = _Host(hass)
    zone = Zone(
        index=0, slug="den", name="Den", climate_id=HEAD, sensor_id="sensor.den",
        enable=True, allow_dry=True,
    )
    return DryMode(host, [zone]), host, zone


async def test_a_stale_report_keeps_a_release_and_a_sent_park_notice(hass: HomeAssistant) -> None:
    dry, host, zone = await _dry(hass, "dry")
    assert dry.in_dry(zone)
    dry.release(zone)  # Heat/Cool picked on the thermostat
    dry.notice_parked(zone)
    await hass.async_block_till_done()
    assert (dry.in_dry(zone), len(host.notices)) == (False, 1)

    assert dry.head_reported(HEAD, _head("dry"), _head("cool"), stale=True) is False
    dry.notice_parked(zone)
    await hass.async_block_till_done()
    assert (dry.in_dry(zone), len(host.notices)) == (False, 1)

    # The control: the same report, current, clears both.
    assert dry.head_reported(HEAD, _head("dry"), _head("cool"), stale=False) is False
    dry.notice_parked(zone)
    await hass.async_block_till_done()
    assert (dry.in_dry(zone), len(host.notices)) == (True, 2)


async def test_a_stale_report_of_dry_does_not_land_a_pick(hass: HomeAssistant) -> None:
    dry, _host, zone = await _dry(hass, "cool")
    assert dry.start_pick(zone) is True  # the head still has to be sent dry
    assert dry.in_dry(zone)  # the pick counts until the head reports it

    assert dry.head_reported(HEAD, _head("cool"), _head("dry"), stale=True) is False
    assert dry.in_dry(zone)

    # The control: a current report of dry lands it, so the pick no longer counts.
    assert dry.head_reported(HEAD, _head("cool"), _head("dry"), stale=False) is False
    assert not dry.in_dry(zone)
    dry.cancel_timers()
