"""Hypothesis properties of the manual fan hold, on the real coordinator.

The rule (docs/formal/README.md): a speed a person picks is never written over
until that person hands it back, across any readings, time, door/window pauses,
reloads and restarts. MXZ never holds a speed nobody picked.

The documented exception is pinned here too, as what it is: a pick of one of the
last two speeds MXZ commanded reads as MXZ's own echo
(``MXZCoordinator._observe_fan_latch``, the cloud1104 PARKED.md "A pick
of the previous fan speed is overwritten", docs/formal/README.md). It is a passing
test on purpose: CI's phase plugin (tools/pytest_phases.py) refuses xfails, and a
test that pins it still fails the day the behavior changes.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from hypothesis import assume, example, given
from hypothesis import strategies as st

from custom_components.mxz_coordinator.const import (
    FAN_AUTO,
    FAN_LADDER,
    HEAD_TYPE_CLOUD,
    ZONE_HEAD_TYPE,
)
from tests.cloud_heads import (
    SENSORS,
    ComfortHead,
    past_startup,
    past_window,
    recompute,
    register,
    row,
    set_room,
    setup_rooms,
)
from tests.property_rig import CLOSE_DELAY, OPEN_DELAY, SEQUENCES, TARGET, open_rig

COOLING = (TARGET + 0.7, TARGET + 1.2, TARGET + 2.2, TARGET + 3.2, TARGET + 4.2, TARGET + 6)
HEATING = (TARGET - 0.7, TARGET - 2.2, TARGET - 4.2, TARGET - 6)
TEMPS = (*COOLING, *HEATING, TARGET)
TOKENS = (FAN_AUTO, *FAN_LADDER)


def _steps(*, lag: bool, window: bool) -> st.SearchStrategy[list[tuple[Any, ...]]]:
    choices: list[st.SearchStrategy[tuple[Any, ...]]] = [
        st.tuples(st.just("temp"), st.sampled_from(TEMPS)),
        st.tuples(st.just("later"), st.sampled_from((1, 60, 600, 3600))),
        st.tuples(st.just("pick"), st.sampled_from(TOKENS)),
        st.tuples(st.just("fan_auto"), st.booleans()),
        st.tuples(st.just("reload")),
        st.tuples(st.just("restart")),
    ]
    if lag:
        choices.append(st.tuples(st.just("report")))
    if window:
        choices.append(st.tuples(st.just("window"), st.sampled_from(("on", "off"))))
        choices.append(st.tuples(st.just("later"), st.sampled_from((OPEN_DELAY + 1, CLOSE_DELAY + 1))))
    return st.lists(st.one_of(choices), min_size=1, max_size=14)


class PickOracle:
    """What a person has asked for, from their gestures and MXZ's writes alone."""

    def __init__(self, rig: Any) -> None:
        self.rig = rig
        self.head = rig.heads[0]
        self.standing: str | None = None
        self.writes_at_pick = 0
        # Every speed the fan's memory can hold: MXZ's writes, the auto a head
        # starts at, and a speed MXZ was handed or held. A pick outside this set
        # can't be read as an echo of anything, so the rule binds it with no
        # exception. Inside, see test_the_exception_is_exactly_the_last_two.
        self.remembered = {FAN_AUTO}

    def _stand(self, token: str) -> None:
        self.standing = token
        self.remembered.add(token)
        self.writes_at_pick = len(self.head.fan_writes)

    def before_pick(self, token: str) -> None:
        self.remembered.update(self.head.fan_writes)
        shown = self.head.fan
        if token == FAN_AUTO:
            self.standing = None  # handed back
        elif token == shown:
            pass  # the same speed: nothing changed, nothing can be read
        elif self.standing is not None or token not in self.remembered:
            self._stand(token)
        else:
            self.standing = "unknown"  # the exception's ground: settled in after_pick

    def after_pick(self) -> None:
        if self.standing == "unknown":
            # The exception may or may not apply; either way a hold is then a hold.
            if self.rig.fan_held():
                self._stand(self.head.fan)
            else:
                self.standing = None

    def fan_auto(self, on: bool) -> None:
        self.remembered.update(self.head.fan_writes)
        if on:
            self.standing = None
            self.remembered.add(self.head.fan)  # adopted into the memory
        elif self.head.fan != FAN_AUTO:
            self._stand(self.head.fan)

    def check(self) -> None:
        self.remembered.update(self.head.fan_writes)
        if self.standing is not None:
            assert self.head.fan == self.standing, "MXZ moved a held speed"
            assert self.head.fan_writes[self.writes_at_pick:] == [], "MXZ wrote over a held speed"
            assert self.rig.fan_held() is True
            assert self.rig.fan_auto_switch() == "off"
        else:
            assert self.rig.fan_held() is False, "a hold nobody asked for"
            assert self.rig.fan_auto_switch() == "on"


async def _drive(rig: Any, oracle: PickOracle, step: tuple[Any, ...]) -> None:
    kind = step[0]
    if kind == "temp":
        await rig.temp(0, step[1])
    elif kind == "later":
        await rig.later(step[1])
    elif kind == "pick":
        oracle.before_pick(step[1])
        await rig.person_fan(step[1])
        oracle.after_pick()
    elif kind == "fan_auto":
        oracle.fan_auto(step[1])
        await rig.switch("_primary_fan_auto", step[1])
    elif kind == "report":
        await rig.report_fan()
    elif kind == "window":
        await rig.window(step[1])
    elif kind == "reload":
        await rig.reload()
    else:
        await rig.restart()


@SEQUENCES
@given(
    first=st.sampled_from(TEMPS),
    fan_max=st.sampled_from(("medium", "high")),
    steps=_steps(lag=False, window=True),
)
async def test_a_pick_is_never_written_over_until_it_is_handed_back(
    hass: HomeAssistant, freezer: Any, first: float, fan_max: str, steps: list[tuple[Any, ...]]
) -> None:
    rig = await open_rig(hass, freezer, temps=(first, TARGET), window="off", fan_max=fan_max)
    try:
        oracle = PickOracle(rig)
        for step in steps:
            await _drive(rig, oracle, step)
            oracle.check()
    finally:
        await rig.close()


@SEQUENCES
@given(ladder=st.lists(st.sampled_from(COOLING), min_size=1, max_size=6))
async def test_the_exception_is_exactly_the_last_two(
    hass: HomeAssistant, freezer: Any, ladder: list[float]
) -> None:
    """Known exception, cloud1104 PARKED item 3. MXZ walks the ladder, then a person
    picks each speed in turn from a fresh walk: one of the last two distinct speeds
    MXZ commanded reads as its own echo (not held); any other is a hold at once."""
    for token in FAN_LADDER:
        rig = await open_rig(hass, freezer, temps=(TARGET + 6, TARGET))
        try:
            for temp in ladder:
                await rig.temp(0, temp)
            head = rig.heads[0]
            distinct = [FAN_AUTO]  # the head's starting speed is the first baseline
            for sent in head.fan_writes:
                if sent != distinct[-1]:
                    distinct.append(sent)
            last_two = distinct[-2:]
            assert head.fan == distinct[-1]
            if token == head.fan:
                continue  # picking what it shows changes nothing to read
            await rig.person_fan(token)
            assert rig.fan_held() is (token not in last_two)
        finally:
            await rig.close()


@SEQUENCES
@given(walk=st.lists(st.sampled_from(COOLING), max_size=4))
async def test_the_exception_can_lose_the_pick(
    hass: HomeAssistant, freezer: Any, walk: list[float]
) -> None:
    """Known exception, cloud1104 PARKED item 3, in full: a pick of the previous speed
    isn't held, and the same compute writes the ladder's speed back over it."""
    rig = await open_rig(hass, freezer, temps=(TARGET + 6, TARGET))
    try:
        for temp in (TARGET + 2.2, *walk):
            await rig.temp(0, temp)  # the top, then a step down: two speeds on record
        head = rig.heads[0]
        distinct = [FAN_AUTO]
        for sent in head.fan_writes:
            if sent != distinct[-1]:
                distinct.append(sent)
        assert len(distinct) >= 3
        wanted, previous = distinct[-1], distinct[-2]
        writes = len(head.fan_writes)
        await rig.person_fan(previous)
        assert rig.fan_held() is False
        assert head.fan_writes[writes:] == [wanted]
        assert head.fan == wanted
    finally:
        await rig.close()


@SEQUENCES
@given(steps=st.lists(st.one_of(
    st.tuples(st.just("temp"), st.sampled_from(COOLING)),
    st.tuples(st.just("report")),
    st.tuples(st.just("later"), st.sampled_from((1, 60))),
), min_size=1, max_size=10))
async def test_a_slow_heads_own_echo_is_not_a_hold_one_write_behind(
    hass: HomeAssistant, freezer: Any, steps: list[tuple[Any, ...]]
) -> None:
    """With no person anywhere, a head reporting the speed before MXZ's last write is
    read as MXZ's echo. The head here reports each write before the next is decided."""
    rig = await open_rig(hass, freezer, temps=(TARGET + 6, TARGET), lags=True)
    try:
        oracle = PickOracle(rig)
        await rig.report_fan()
        for step in steps:
            await _drive(rig, oracle, step)
            assert rig.fan_held() is False
            await rig.report_fan()
            assert rig.fan_held() is False
    finally:
        await rig.close()


@SEQUENCES
@given(temps=st.lists(st.sampled_from(COOLING), min_size=1, max_size=6))
@example(temps=[TARGET + 2.2, TARGET + 0.7])
async def test_no_person_no_hold_however_far_a_head_lags(
    hass: HomeAssistant, freezer: Any, temps: list[float]
) -> None:
    """F1, in docs/formal/PARKED-FINDINGS.md. Nobody touches the fan and the head reports
    only at the start, then only at the end: no hold, ever, and boost keeps the fan."""
    rig = await open_rig(hass, freezer, temps=(TARGET + 6, TARGET), lags=True)
    try:
        await rig.report_fan()
        for temp in temps:
            await rig.temp(0, temp)
            assert rig.fan_held() is False
            assert rig.fan_auto_switch() == "on"
        await rig.report_fan()
        assert rig.fan_held() is False
    finally:
        await rig.close()


@SEQUENCES
@given(
    steps=st.lists(
        st.one_of(
            st.tuples(st.just("temp"), st.sampled_from((*COOLING, TARGET))),
            st.sampled_from((("report",), ("reload",), ("restart",))),
        ),
        min_size=1,
        max_size=10,
    ),
)
@example(steps=[("temp", TARGET + 6), ("temp", TARGET + 2.2), ("temp", TARGET + 0.7), ("temp", TARGET + 6)])
@example(steps=[("temp", TARGET + 1.2), ("temp", TARGET + 2.2), ("temp", TARGET + 0.7), ("reload",), ("temp", TARGET + 1.2)])
@example(
    steps=[
        ("temp", TARGET + 6), ("report",), ("report",),
        ("temp", TARGET), ("temp", TARGET + 2.2), ("temp", TARGET + 0.7), ("temp", TARGET),
    ]
)
async def test_no_person_no_hold_when_a_head_reports_every_write_in_order(
    hass: HomeAssistant, freezer: Any, steps: list[tuple[Any, ...]]
) -> None:
    """F1 again, with a head that applies its writes one at a time and shows each, late and in
    order, starting from auto. A late report can equal the last write while others are still
    coming. No hold, ever, and with the queue done no older write is left."""
    rig = await open_rig(hass, freezer, temps=(TARGET, TARGET), lags=True)
    try:
        head = rig.heads[0]

        async def nobody_touched_it() -> None:
            assert rig.fan_held() is False
            assert rig.fan_auto_switch() == "on"

        for step in steps:
            if step[0] == "temp":
                await rig.temp(0, step[1])
            elif step[0] == "report":
                await rig.report_next_fan()
            else:
                await (rig.reload() if step[0] == "reload" else rig.restart())
            await nobody_touched_it()
        for _ in range(100):
            if not await rig.report_next_fan():
                break
            await nobody_touched_it()
        else:
            raise AssertionError("the head never got through its queue")
        assert rig.coord.fan_older_commands(head.entity_id) == []
    finally:
        await rig.close()


@SEQUENCES
@given(
    temps=st.lists(st.sampled_from(COOLING), min_size=1, max_size=6),
    pick=st.sampled_from(FAN_LADDER),
    start=st.sampled_from(("reload", "restart")),
)
async def test_a_lagging_heads_pick_of_a_speed_mxz_never_wrote_is_held(
    hass: HomeAssistant, freezer: Any, temps: list[float], pick: str, start: str
) -> None:
    """However far the head lags, a speed that isn't MXZ's write is a person's: held at
    once, never written over, and still held after a start."""
    rig = await open_rig(hass, freezer, temps=(TARGET + 6, TARGET), lags=True)
    try:
        await rig.report_fan()
        for temp in temps:
            await rig.temp(0, temp)
        head = rig.heads[0]
        assume(pick != head.fan and pick not in head.fan_writes)
        await rig.person_fan(pick)
        assert rig.fan_held() is True
        writes = len(head.fan_writes)
        await (rig.reload() if start == "reload" else rig.restart())
        await rig.temp(0, TARGET + 6)
        assert rig.fan_held() is True
        assert rig.fan_auto_switch() == "off"
        assert head.fan == pick
        assert head.fan_writes[writes:] == []
    finally:
        await rig.close()


async def _cloud_entry(hass: HomeAssistant, head: ComfortHead) -> Any:
    entry = await setup_rooms(hass, [(head, {ZONE_HEAD_TYPE: HEAD_TYPE_CLOUD})], temps=(TARGET - 10,))
    await past_startup(hass, entry)
    return entry


async def _cloud_step(hass: HomeAssistant, entry: Any, head: ComfortHead, seconds: float = 400) -> None:
    """A fan step and the confirm wait both come due; the head reports what it holds."""
    past_window(entry, head.entity_id, seconds)
    await recompute(hass, entry)
    head.poll()
    await hass.async_block_till_done()


def _fans(head: ComfortHead) -> list[str]:
    return [payload["fan_mode"] for service, payload in head.received if service == "set_fan_mode"]


@SEQUENCES
@given(
    token=st.sampled_from(("middle", "high")),
    temps=st.lists(st.sampled_from(HEATING), min_size=1, max_size=5),
)
async def test_a_cloud_heads_pick_is_never_written_over(
    hass: HomeAssistant, token: str, temps: list[float]
) -> None:
    """A speed MXZ never sent this head, picked on it, is held from the next report,
    and no fan step goes out over it however many come due."""
    key = "property_cloud_head"
    head = hass.data.get(key)
    if head is None:
        hass.config.units = US_CUSTOMARY_SYSTEM
        head = hass.data[key] = ComfortHead("a", temperature=TARGET)
        await register(hass, {"kumo_cloud": [head]})
    head.device.update(mode="off", temperature=TARGET, fan="auto")
    head.received.clear()
    head.poll()
    entry = await _cloud_entry(hass, head)
    try:
        await _cloud_step(hass, entry, head)
        assert token not in _fans(head)
        head.device["fan"] = token
        head.poll()
        await hass.async_block_till_done()
        await _cloud_step(hass, entry, head)
        assert row(hass, entry, 0)["fan_hold"] is True
        sent = len(_fans(head))
        for temp in temps:
            await set_room(hass, SENSORS[0], temp)
            await _cloud_step(hass, entry, head, 3600)
            assert head.device["fan"] == token
            assert row(hass, entry, 0)["fan_hold"] is True
        assert _fans(head)[sent:] == []
    finally:
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize("previous", [True, False], ids=["previous-speed", "never-sent"])
async def test_the_exception_on_a_cloud_head(hass: HomeAssistant, previous: bool) -> None:
    """Known exception, cloud1104 PARKED item 3, on the head type it was found on:
    MXZ steps low then medium, a person picks low, it isn't held, and the next due
    step writes over it. The control: a speed MXZ never sent is held and stays."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head = ComfortHead("a", temperature=TARGET)
    await register(hass, {"kumo_cloud": [head]})
    entry = await _cloud_entry(hass, head)
    await _cloud_step(hass, entry, head)
    await _cloud_step(hass, entry, head)
    assert _fans(head)[-2:] == ["low", "medium"]
    pick = "low" if previous else "high"
    head.device["fan"] = pick
    head.poll()
    await hass.async_block_till_done()
    await _cloud_step(hass, entry, head)
    await _cloud_step(hass, entry, head)
    assert row(hass, entry, 0)["fan_hold"] is (not previous)
    assert (head.device["fan"] == pick) is (not previous)
