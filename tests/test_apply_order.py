"""Where the coil-dry wakeup armed from the head-change gate falls among the apply's writes.

From the independent review of planner1118d (finding 1): ``apply_plan`` once decided every head
before any was done, which moved this wakeup ahead of the first head's command. Runs on the real
integration over invented heads.

A person presses off at the second room's head while MXZ's delivery to it is still running (a
slow fan write). The plan is then applied inside the adopt hold. On the base, the first room's
head is commanded before the second head's gate is read, so its queue call comes before the
coil-dry wakeup that reading the gate arms for the second head.
"""
import asyncio
from typing import Any

from homeassistant.components.climate import HVACMode
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT

from custom_components.mxz_coordinator.clock import Clock
from custom_components.mxz_coordinator.const import (
    CONF_COIL_DRY_MINUTES,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    IDLE_ACTION_OFF_AFTER_DRY,
)
from custom_components.mxz_coordinator.delivery import DeliveryQueue
from tests.apply_parity import Applies
from tests.test_drive import MockHead
from tests.test_startup_continuity import _head


class SlowHead(MockHead):
    gate: asyncio.Event | None = None
    entered = False

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if SlowHead.gate is not None:
            SlowHead.entered = True
            await SlowHead.gate.wait()
        await super().async_set_fan_mode(fan_mode)


async def test_the_gates_coil_dry_wakeup_comes_after_the_earlier_heads_command(
    hass: Any, freezer: Any, monkeypatch: Any
) -> None:
    house = Applies(hass, freezer, monkeypatch)
    await house.setup(
        [{}, {}], head_classes=[MockHead, SlowHead],
        **{CONF_MODE_HYSTERESIS: 0, CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY, CONF_COIL_DRY_MINUTES: 10},
    )
    coord = house.coord
    h0, h1 = house.heads[0].entity_id, house.heads[1].entity_id
    await house.temp(0, 76)
    assert hass.states.get(h0).state == "cool"

    # The second room starts cooling; its head's fan write stays in flight.
    SlowHead.gate, SlowHead.entered = asyncio.Event(), False
    hass.states.async_set(
        house.sensors[1], "77", {ATTR_UNIT_OF_MEASUREMENT: hass.config.units.temperature_unit}
    )
    refresh = hass.loop.create_task(coord.async_refresh())
    for _ in range(200):
        await asyncio.sleep(0)
        if SlowHead.entered:
            break
    assert SlowHead.entered and coord.deliveries.busy(h1)
    assert hass.states.get(h1).state == "cool"

    seq: list[tuple[Any, ...]] = []
    call_later, queue = Clock.call_later, DeliveryQueue.queue

    def _call_later(clock: Clock, delay: float, action: Any, *, name: str, key: Any = None, **kw: Any) -> Any:
        if name == "dry":
            seq.append(("arm", name, key))
        return call_later(clock, delay, action, name=name, key=key, **kw)

    def _queue(deliveries: DeliveryQueue, climate_id: str, *args: Any, **kwargs: Any) -> Any:
        seq.append(("queue", climate_id))
        return queue(deliveries, climate_id, *args, **kwargs)

    monkeypatch.setattr(Clock, "call_later", _call_later)
    monkeypatch.setattr(DeliveryQueue, "queue", _queue)

    # A person presses off at the second head while that delivery runs.
    head = _head(hass, h1)
    head._attr_hvac_mode = HVACMode.OFF
    head.async_write_ha_state()
    await asyncio.sleep(0)
    assert coord.clock.waiting("heal", (h1, "adopt")), "the adopt hold is armed"
    assert not coord.clock.waiting("dry", h1), "no coil-dry wakeup yet: the delivery owned the head"
    assert ("arm", "dry", h1) not in seq

    gate, SlowHead.gate = SlowHead.gate, None
    gate.set()
    await refresh
    await hass.async_block_till_done()
    # The plan is applied again inside the hold (any input does it; here the first room's reading).
    await house.temp(0, 75)
    assert coord.clock.waiting("heal", (h1, "adopt")), "still inside the adopt hold"
    assert ("arm", "dry", h1) in seq, "reading the gate armed the second head's coil-dry wakeup"
    gate_arm = seq.index(("arm", "dry", h1))
    assert ("queue", h0) in seq[:gate_arm], (
        f"the first head is commanded before the second head's gate is read: {seq}"
    )
