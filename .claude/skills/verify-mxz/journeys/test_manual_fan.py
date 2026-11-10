"""Manual fan choices are preserved.

Fan boost is on: while Room 1 cools hard, MXZ drives its head's fan up. A
person then picks ``quiet`` on the head itself. From that moment MXZ stops
writing that head's fan (its Fan auto switch turns off and the plan shows a
fan hold), through more computes, through the room reaching its target, and
through a Home Assistant restart. Turning Fan auto back on hands the fan back
to MXZ, which writes it again at once: the silence before was the hold, not a
broken boost.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from mxz_journey import entity

from tests.restart import restart
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _recompute,
    _set_temp,
    _setup_fan_boost,
    _setup_mock_heads,
    _user_set_fan,
)

FEATURE = "manual-fan-preserved"


async def test_a_picked_fan_speed_holds_through_a_restart(
    hass: HomeAssistant, journal
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)  # Room 1 target 62
    log = journal(hass, entry, FEATURE, heads=[head_a, head_b])
    fan_auto = entity(hass, entry, "primary_fan_auto")

    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    shown = log.step("sensor: Room 1 reads 67 against its 62 target (cooling hard)")
    assert shown["heads"][head_a]["state"] == "cool"
    assert shown["heads"][head_a]["fan_mode"] == "high"  # the boost
    assert shown["fan_auto"][fan_auto] == "on"

    await _user_set_fan(hass, head_a, "quiet")
    await _recompute(hass, entry)
    await _recompute(hass, entry)
    shown = log.step("person: picks fan 'quiet' on Room 1's head; two computes follow")
    assert shown["heads"][head_a]["fan_mode"] == "quiet"
    assert shown["plan"]["zones"][0]["fan_hold"] is True
    assert shown["fan_auto"][fan_auto] == "off"
    picked = len(log.head_calls(head_a, "climate.set_fan_mode"))  # the person's own call

    await _set_temp(hass, SENSOR_A, 62)
    await _recompute(hass, entry)
    shown = log.step("sensor: Room 1 reaches 62 (its target)")
    assert shown["heads"][head_a]["state"] == "fan_only"
    assert shown["heads"][head_a]["fan_mode"] == "quiet"  # not dragged back to auto

    await _set_temp(hass, SENSOR_A, 66)
    await restart(hass, entry)
    await _recompute(hass, entry)
    shown = log.step("restart: Home Assistant restarts while Room 1 reads 66")
    assert shown["heads"][head_a]["state"] == "cool"
    assert shown["heads"][head_a]["fan_mode"] == "quiet"
    assert shown["plan"]["zones"][0]["fan_hold"] is True
    assert shown["fan_auto"][fan_auto] == "off"
    assert len(log.head_calls(head_a, "climate.set_fan_mode")) == picked  # MXZ wrote none

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": fan_auto}, blocking=True
    )
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    shown = log.step("person: turns Room 1's Fan auto back on")
    assert shown["plan"]["zones"][0]["fan_hold"] is False
    assert shown["heads"][head_a]["fan_mode"] != "quiet"  # MXZ drives the fan again
    assert len(log.head_calls(head_a, "climate.set_fan_mode")) > picked
