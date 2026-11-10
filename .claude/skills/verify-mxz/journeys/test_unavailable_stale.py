"""Unavailable heads and stale room sensors.

* A head drops off (its integration reports it ``unavailable``). Its room's
  call keeps counting, ``stale``, for the lost-vote time limit (default 30
  minutes) because the head may still be running that call; at the limit it
  ``expired`` and the sibling room gets the outdoor unit. Back in sight, it is
  ``live`` again at once.
* A room sensor with a documented 5-minute reporting interval goes ``stale``
  at the third due report (15 minutes of silence): the room stops calling and
  its head parks, while its last reading stays shown. One fresh report
  recovers it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from tests.clocks import pass_time
from tests.dwell import expire_dwell
from tests.test_drive import SENSOR_A, _recompute
from tests.test_lost_vote import DEFAULT_SECONDS, _back, _lose, _standoff
from tests.test_sensor_freshness import (
    HOT,
    INTERVAL_MIN,
    WINDOW,
    ZONE_REPORT_INTERVAL,
    _advance,
    _report,
    _setup_fresh,
)

FEATURE = "unavailable-stale-heads"


async def test_a_lost_head_holds_while_stale_then_lets_the_sibling_run(
    hass: HomeAssistant, journal
) -> None:
    entry, head_a, head_b = await _standoff(hass)  # Room 1 calls cool, Room 2 heat
    coord = entry.runtime_data
    log = journal(hass, entry, FEATURE, heads=[head_a, head_b])
    shown = log.step("setup: Room 1 (76) calls cool and wins; Room 2 (64) calls heat")
    assert (shown["plan"]["state"], shown["plan"]["zones"][0]["vote_status"]) == ("cool", "live")
    # The mode-change wait runs out before the loss, so it is not what holds
    # the mode below, and no time passes on the loss but what each step says.
    expire_dwell(coord)

    await _lose(hass, head_a)
    await _recompute(hass, entry)
    shown = log.step("head: Room 1's head becomes unavailable")
    row = shown["plan"]["zones"][0]
    assert (row["vote_status"], row["demand"]) == ("stale", "cool")
    assert row["lost_since"] is not None
    assert shown["plan"]["state"] == "cool"  # stale: its call still holds the mode
    assert shown["heads"][head_b]["state"] == "fan_only"
    assert shown["heads"][head_a]["state"] == "unavailable"

    pass_time(coord, DEFAULT_SECONDS - 2)  # 2 s short of 30 minutes
    await _recompute(hass, entry)
    shown = log.step("time: 29 min 58 s without Room 1's head")
    assert shown["plan"]["zones"][0]["vote_status"] == "stale"
    assert shown["plan"]["state"] == "cool"

    pass_time(coord, 2)  # exactly the limit: half-open, so expired
    assert coord.mode_hold_remaining() == 0  # no wait: the limit alone lets go
    await _recompute(hass, entry)
    shown = log.step("time: 30 min without Room 1's head (the limit)")
    row = shown["plan"]["zones"][0]
    assert (row["vote_status"], row["demand"]) == ("expired", "cool")  # shown, not counted
    assert shown["plan"]["state"] == "heat"
    assert shown["heads"][head_b]["state"] == "heat"  # the sibling runs

    await _back(hass, head_a)
    await _recompute(hass, entry)
    shown = log.step("head: Room 1's head reports again")
    row = shown["plan"]["zones"][0]
    assert (row["vote_status"], row["lost_since"]) == ("live", None)
    assert shown["plan"]["standoff"] is True


async def test_a_stale_sensor_stops_calling_and_one_fresh_report_recovers(
    hass: HomeAssistant, freezer: Any, journal
) -> None:
    entry, head_a, head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    log = journal(hass, entry, FEATURE, heads=[head_a, head_b])
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    shown = log.step("sensor: Room A reports 75 (5-minute interval contracted)")
    row = shown["plan"]["zones"][0]
    assert (row["sensor_health"], row["demand"]) == ("healthy", "cool")
    assert shown["heads"][head_a]["state"] == "cool"

    await _advance(hass, freezer, WINDOW - timedelta(seconds=1))
    await _recompute(hass, entry)
    shown = log.step("time: 14 min 59 s with no report from Room A's sensor")
    assert shown["plan"]["zones"][0]["sensor_health"] == "healthy"
    assert shown["heads"][head_a]["state"] == "cool"

    await _advance(hass, freezer, timedelta(seconds=1))
    await _recompute(hass, entry)
    shown = log.step("time: 15 min with no report (the third due report missed)")
    row = shown["plan"]["zones"][0]
    assert (row["sensor_health"], row["demand"], row["engage"]) == (
        "stale", "neutral", "satisfied",
    )
    assert row["temp"] == HOT[False]  # the last reading is still shown
    assert shown["heads"][head_a]["state"] == "fan_only"  # parked at the idle action

    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    shown = log.step("sensor: Room A reports 75 again")
    row = shown["plan"]["zones"][0]
    assert (row["sensor_health"], row["demand"]) == ("healthy", "cool")
    assert shown["heads"][head_a]["state"] == "cool"
