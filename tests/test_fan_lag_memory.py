"""F1: a slow local head's report of an older fan write of MXZ's isn't a person's pick.

The coordinator keeps its older fan writes, per head, until the head's reports pass them
(``FanEcho.older``). A speed MXZ didn't recently write is still a person's
pick, held at once and across a start. docs/formal/PARKED-FINDINGS.md has the finding
and docs/formal/README.md the rule. All rooms and heads are invented.
"""

from __future__ import annotations

from itertools import groupby
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import FAN_LADDER, FAN_OLDER_WRITES
from custom_components.mxz_coordinator.fan_hold import FanEcho
from custom_components.mxz_coordinator.switch import FanHoldRestoreData
from tests.property_rig import TARGET, Rig, open_rig

HIGH_RUNG = TARGET + 6
MEDIUM_RUNG = TARGET + 2.2
LOW_RUNG = TARGET + 0.7


async def _lagging_walk(hass: HomeAssistant, freezer: Any, **kwargs: Any) -> Rig:
    """The finding's walk: the head reports high, then two writes go unreported."""
    rig = await open_rig(hass, freezer, temps=(HIGH_RUNG, TARGET), lags=True, **kwargs)
    await rig.report_fan()
    await rig.temp(0, MEDIUM_RUNG)
    await rig.temp(0, LOW_RUNG)
    head = rig.heads[0]
    assert head.fan == "high"
    assert list(dict.fromkeys(head.fan_writes)) == ["high", "medium", "low"]  # rewrites aside
    return rig


async def _queued_walk(hass: HomeAssistant, freezer: Any, *temps: float) -> Rig:
    """A head showing auto, with every write below still queued on it and nothing reported."""
    rig = await open_rig(hass, freezer, temps=(TARGET, TARGET), lags=True)
    for temp in temps:
        await rig.temp(0, temp)
    assert rig.heads[0].fan == "auto"
    return rig


async def _report_each_write_in_order(rig: Rig) -> list[str]:
    """The head works through its queue, and no report of it may ever read as a pick.
    Returns the speeds it showed, rewrites aside (boost rewrites a speed until it's reported)."""
    shown = []
    for _ in range(100):
        if not await rig.report_next_fan():
            return [token for token, _ in groupby(shown)]
        shown.append(rig.heads[0].fan)
        assert rig.fan_held() is False, shown
        assert rig.fan_auto_switch() == "on", shown
    raise AssertionError(f"the head never got through its queue: {shown}")


async def _walk_back_to_auto(hass: HomeAssistant, freezer: Any) -> Rig:
    """The head has reported high. Auto, medium, low and auto again are queued on it. MXZ only
    writes auto to a head not showing it, so this is how auto comes to be written twice."""
    rig = await _queued_walk(hass, freezer, HIGH_RUNG)
    await _report_each_write_in_order(rig)
    for temp in (TARGET, MEDIUM_RUNG, LOW_RUNG, TARGET):
        await rig.temp(0, temp)
    head = rig.heads[0]
    assert head.fan == "high"
    assert [token for token, _ in groupby(head.fan_writes)] == ["high", "auto", "medium", "low", "auto"]
    return rig


def _switch_attrs(rig: Rig) -> dict[str, Any]:
    return rig.hass.states.get(rig.eid("_primary_fan_auto")).attributes


def _restore_data(rig: Rig) -> dict[str, Any]:
    switch = rig.hass.data["entity_components"]["switch"].get_entity(rig.eid("_primary_fan_auto"))
    return switch.extra_restore_state_data.as_dict()


async def test_the_findings_walk_holds_nothing_and_boost_keeps_driving(hass: HomeAssistant, freezer: Any) -> None:
    """Each step of PARKED-FINDINGS.md's table, with the hold it used to show gone."""
    rig = await _lagging_walk(hass, freezer)
    head = rig.heads[0].entity_id

    def memory() -> tuple[Any, ...]:
        return (*rig.coord.fan.command_memory(head), rig.coord.fan.older_commands(head))

    assert memory() == ("low", "medium", ["high"])
    assert rig.fan_held() is False
    assert rig.fan_auto_switch() == "on"
    await rig.report_fan()
    assert rig.heads[0].fan == "low"
    assert memory() == ("low", "medium", [])
    assert rig.fan_held() is False
    writes = len(rig.heads[0].fan_writes)
    await rig.temp(0, HIGH_RUNG)
    assert set(rig.heads[0].fan_writes[writes:]) == {"high"}  # rewritten until it reports
    assert memory() == ("high", "low", [])
    assert rig.fan_held() is False
    await rig.close()


async def test_a_pick_mxz_never_wrote_is_held_while_the_head_lags(hass: HomeAssistant, freezer: Any) -> None:
    """The ring widens nothing but MXZ's own writes: this speed isn't one, so it holds."""
    rig = await _lagging_walk(hass, freezer)
    head = rig.heads[0]
    assert "quiet" not in head.fan_writes
    writes = len(head.fan_writes)
    await rig.person_fan("quiet")
    assert rig.fan_held() is True
    assert rig.fan_auto_switch() == "off"
    await rig.temp(0, HIGH_RUNG)
    assert head.fan == "quiet"
    assert head.fan_writes[writes:] == []
    await rig.close()


@pytest.mark.parametrize("start", ["reload", "restart"])
async def test_a_pick_mxz_never_wrote_survives_a_start(hass: HomeAssistant, freezer: Any, start: str) -> None:
    rig = await _lagging_walk(hass, freezer)
    head = rig.heads[0]
    await rig.person_fan("quiet")
    writes = len(head.fan_writes)
    await (rig.reload() if start == "reload" else rig.restart())
    assert rig.fan_held() is True
    assert rig.fan_auto_switch() == "off"
    await rig.temp(0, HIGH_RUNG)
    assert head.fan == "quiet"
    assert head.fan_writes[writes:] == []
    await rig.close()


async def test_a_head_caught_up_reads_an_older_write_as_a_pick(hass: HomeAssistant, freezer: Any) -> None:
    """The memory is only as wide as the lag. A head that reports at once walks the same
    speeds, and a pick of the oldest is then a person's: it holds, and isn't written over."""
    rig = await open_rig(hass, freezer, temps=(HIGH_RUNG, TARGET))
    head = rig.heads[0]
    await rig.temp(0, MEDIUM_RUNG)
    await rig.temp(0, LOW_RUNG)
    assert head.fan_writes == ["high", "medium", "low"]
    assert rig.coord.fan.older_commands(head.entity_id) == []
    writes = len(head.fan_writes)
    await rig.person_fan("high")
    assert rig.fan_held() is True
    assert head.fan == "high"
    assert head.fan_writes[writes:] == []
    await rig.close()


async def test_a_lagging_heads_report_of_the_last_write_ends_the_tolerance(hass: HomeAssistant, freezer: Any) -> None:
    """The same pick as the walk's echo, one report later, is a hold."""
    rig = await _lagging_walk(hass, freezer)
    head = rig.heads[0]
    assert rig.coord.fan.older_commands(head.entity_id) == ["high"]
    await rig.report_fan()
    assert rig.coord.fan.older_commands(head.entity_id) == []
    await rig.person_fan("high")
    assert rig.fan_held() is True
    assert head.fan == "high"
    await rig.close()


@pytest.mark.parametrize("start", ["reload", "restart"])
async def test_the_memory_rides_a_start_with_the_head_still_behind(hass: HomeAssistant, freezer: Any, start: str) -> None:
    """The head still shows the speed before the last two writes when MXZ starts again."""
    rig = await _lagging_walk(hass, freezer)
    assert _switch_attrs(rig)["older_fan_commands"] == ["high"]
    await (rig.reload() if start == "reload" else rig.restart())
    head = rig.heads[0]
    assert rig.coord.fan.command_memory(head.entity_id) == ("low", "medium")
    assert rig.coord.fan.older_commands(head.entity_id) == ["high"]
    assert rig.fan_held() is False
    assert rig.fan_auto_switch() == "on"
    await rig.report_fan()
    assert rig.fan_held() is False
    assert "older_fan_commands" not in _switch_attrs(rig)
    await rig.close()


async def test_a_record_without_the_memory_still_holds_conservatively(hass: HomeAssistant, freezer: Any) -> None:
    """A record from before this field: ``FanHold._reconcile_restored_hold``'s documented false hold,
    unchanged. It is what the memory above is for, so a start with it lost must show it."""
    rig = await _lagging_walk(hass, freezer)
    rig.coord.fan.restore(rig.heads[0].entity_id, False, last="low", prior="medium")
    await rig.settle()
    assert rig.fan_held() is True
    await rig.close()


async def test_a_restored_speed_boost_could_never_write_is_no_echo(hass: HomeAssistant, freezer: Any) -> None:
    """Above the ceiling, or not a ladder speed: not taken back, so it holds (S11b)."""
    rig = await open_rig(hass, freezer, temps=(MEDIUM_RUNG, TARGET), fan_max="medium", lags=True)
    head = rig.heads[0]
    await rig.report_fan()
    head.person_fan("high")
    await hass.async_block_till_done()
    rig.coord.fan.restore(head.entity_id, False, last="low", prior="quiet", older=("high", "diffuse", "medium"))
    assert rig.coord.fan.older_commands(head.entity_id) == ["medium"]
    await rig.settle()
    assert rig.fan_held() is True
    await rig.close()


async def test_a_hold_and_a_hand_back_leave_no_older_writes(hass: HomeAssistant, freezer: Any) -> None:
    """A hold's memory is the held speed alone, and handing it back starts fresh."""
    rig = await _lagging_walk(hass, freezer)
    head = rig.heads[0]
    await rig.switch("_primary_fan_auto", False)
    assert rig.coord.fan.older_commands(head.entity_id) == []
    await rig.switch("_primary_fan_auto", True)
    assert rig.coord.fan.older_commands(head.entity_id) == []
    await rig.close()

    rig = await _lagging_walk(hass, freezer)
    await rig.person_fan("quiet")
    assert rig.coord.fan.older_commands(rig.heads[0].entity_id) == []
    await rig.close()


async def test_the_memory_is_bounded_and_per_head(hass: HomeAssistant, freezer: Any) -> None:
    """Past the bound the oldest goes, and a report of it reads as a pick again."""
    rig = await open_rig(hass, freezer)
    coord = rig.coord
    head, other = rig.heads[0].entity_id, rig.heads[1].entity_id
    tokens = [f"speed{i}" for i in range(FAN_OLDER_WRITES + 6)]
    coord.fan._echo[head] = FanEcho("auto", "auto")
    for token in tokens:
        coord.fan._echo[head].wrote(token)
    older = coord.fan.older_commands(head)
    assert len(older) == FAN_OLDER_WRITES >= len(FAN_LADDER)
    assert older == tokens[-(FAN_OLDER_WRITES + 2):-2]
    assert coord.fan.older_commands(other) == []

    assert coord.fan._observe_latch(head, older[0], "cool", 1.0) is False
    assert coord.fan._observe_latch(head, tokens[0], "cool", 1.0) is True
    await rig.close()


async def test_a_rewrite_and_a_repeat_keep_the_memory_exact(hass: HomeAssistant, freezer: Any) -> None:
    """No speed twice, none of the last two, no auto, and a rewrite rolls nothing (#14)."""
    rig = await open_rig(hass, freezer)
    coord, head = rig.coord, rig.heads[0].entity_id
    coord.fan._echo[head] = FanEcho("auto", "auto")
    for token in ("high", "medium", "medium", "low", "high", "low"):
        coord.fan._echo[head].wrote(token)
    assert coord.fan.command_memory(head) == ("low", "high")
    assert coord.fan.older_commands(head) == ["medium"]
    await rig.close()


async def test_a_late_report_equal_to_the_last_write_keeps_the_writes_after_it(hass: HomeAssistant, freezer: Any) -> None:
    """The review's stream. MXZ wrote high first and high last, so a report of high can be
    either one. It is read as the first until the head has shown what came between."""
    rig = await _queued_walk(hass, freezer, HIGH_RUNG, MEDIUM_RUNG, LOW_RUNG, HIGH_RUNG)
    head = rig.heads[0]
    assert list(dict.fromkeys(head.fan_writes)) == ["high", "medium", "low"]
    assert rig.coord.fan.command_memory(head.entity_id) == ("high", "low")
    assert rig.coord.fan.older_commands(head.entity_id) == ["medium"]

    shown = await _report_each_write_in_order(rig)

    assert shown == ["high", "medium", "low", "high"]
    assert rig.coord.fan.older_commands(head.entity_id) == []
    await rig.close()


async def test_a_later_write_of_a_speed_outlives_a_report_past_its_first(hass: HomeAssistant, freezer: Any) -> None:
    """The head has passed medium's first write and low, but medium's second is still queued,
    and medium is neither of the last two by then."""
    rig = await _queued_walk(hass, freezer, HIGH_RUNG, MEDIUM_RUNG, LOW_RUNG, MEDIUM_RUNG, LOW_RUNG + 0.5, HIGH_RUNG)
    head = rig.heads[0]
    assert [token for token, _ in groupby(head.fan_writes)] == ["high", "medium", "low", "medium", "low", "high"]
    assert rig.coord.fan.command_memory(head.entity_id) == ("high", "low")
    assert rig.coord.fan.older_commands(head.entity_id) == ["medium"]
    assert await _report_each_write_in_order(rig) == ["high", "medium", "low", "medium", "low", "high"]
    assert rig.coord.fan.older_commands(head.entity_id) == []
    await rig.close()


async def test_a_late_report_of_auto_doesnt_end_a_walk_back_to_auto(hass: HomeAssistant, freezer: Any) -> None:
    """The same stream with auto as the repeat. Auto's report is the last write's, or the first's."""
    rig = await _walk_back_to_auto(hass, freezer)
    head = rig.heads[0]
    assert rig.coord.fan.command_memory(head.entity_id) == ("auto", "low")
    assert rig.coord.fan.older_commands(head.entity_id) == ["high", "medium"]

    assert await _report_each_write_in_order(rig) == ["auto", "medium", "low", "auto"]
    assert rig.coord.fan.older_commands(head.entity_id) == []
    await rig.close()


@pytest.mark.parametrize("start", ["reload", "restart"])
async def test_a_start_keeps_where_a_repeated_write_came_first(hass: HomeAssistant, freezer: Any, start: str) -> None:
    """Without the repeat's place in the record, the first late report after a start would
    end the memory and the next would hold."""
    rig = await _queued_walk(hass, freezer, HIGH_RUNG, MEDIUM_RUNG, LOW_RUNG, HIGH_RUNG)
    assert _switch_attrs(rig)["older_fan_commands"] == ["high", "medium"]
    assert _restore_data(rig)["older_fan_commands"] == ["high", "medium"]
    await (rig.reload() if start == "reload" else rig.restart())
    assert rig.coord.fan.older_commands(rig.heads[0].entity_id) == ["medium"]
    assert await _report_each_write_in_order(rig) == ["high", "medium", "low", "high"]
    assert rig.coord.fan.older_commands(rig.heads[0].entity_id) == []
    await rig.close()


@pytest.mark.parametrize("start", ["reload", "restart"])
async def test_a_start_keeps_where_a_repeat_of_auto_came_first(hass: HomeAssistant, freezer: Any, start: str) -> None:
    rig = await _walk_back_to_auto(hass, freezer)
    assert _restore_data(rig)["older_fan_commands"] == ["high", "auto", "medium"]
    await (rig.reload() if start == "reload" else rig.restart())
    await _report_each_write_in_order(rig)
    assert rig.coord.fan.older_commands(rig.heads[0].entity_id) == []
    await rig.close()


@pytest.mark.parametrize("start", ["reload", "restart"])
async def test_known_limit_a_start_on_the_first_of_two_writes_can_still_hold(
    hass: HomeAssistant, freezer: Any, start: str
) -> None:
    """F1's limit, pinned so it fails the day this changes. The head shows the first low of low,
    medium, low when MXZ starts again. It looks just like a prompt head that went back to low,
    whose record is pinned to carry nothing, so the first low's place is lost. Two more writes
    and the rest of the queue then read as a pick, with nobody touching the fan."""
    rig = await _queued_walk(hass, freezer, TARGET + 1.2, MEDIUM_RUNG, LOW_RUNG)
    head = rig.heads[0]
    while head.fan != "low":
        assert await rig.report_next_fan()
    assert rig.coord.fan.command_memory(head.entity_id) == ("low", "medium")
    assert "older_fan_commands" not in _switch_attrs(rig)
    await (rig.reload() if start == "reload" else rig.restart())
    await rig.temp(0, TARGET + 0.2)
    await rig.temp(0, MEDIUM_RUNG)
    assert [token for token, _ in groupby(head.fan_writes)] == ["low", "medium", "low", "quiet", "medium"]
    assert rig.fan_held() is False
    while not rig.fan_held():
        assert await rig.report_next_fan()
    assert head.fan == "low"
    await rig.close()


async def test_without_a_start_that_walk_holds_nothing(hass: HomeAssistant, freezer: Any) -> None:
    """The limit above is the start's alone."""
    rig = await _queued_walk(hass, freezer, TARGET + 1.2, MEDIUM_RUNG, LOW_RUNG)
    head = rig.heads[0]
    while head.fan != "low":
        assert await rig.report_next_fan()
    await rig.temp(0, TARGET + 0.2)
    await rig.temp(0, MEDIUM_RUNG)
    assert await _report_each_write_in_order(rig) == ["low", "medium", "low", "quiet", "medium"]
    assert rig.coord.fan.older_commands(head.entity_id) == []
    await rig.close()


async def test_a_report_drops_the_writes_before_it_and_no_others(hass: HomeAssistant, freezer: Any) -> None:
    """The head has passed high, so high can't be a late report any more: a pick of it holds.
    The tolerance ends write by write, not only at the last."""
    rig = await _queued_walk(hass, freezer, HIGH_RUNG, TARGET + 3.2, MEDIUM_RUNG, LOW_RUNG)
    head = rig.heads[0]
    assert rig.coord.fan.command_memory(head.entity_id) == ("low", "medium")
    assert rig.coord.fan.older_commands(head.entity_id) == ["high", "middle"]
    while head.fan != "high":
        assert await rig.report_next_fan()
    assert rig.coord.fan.older_commands(head.entity_id) == ["high", "middle"]  # it is what's showing
    while head.fan != "middle":
        assert await rig.report_next_fan()
    assert rig.coord.fan.older_commands(head.entity_id) == ["middle"]
    writes = len(head.fan_writes)
    await rig.person_fan("high")
    assert rig.fan_held() is True
    assert rig.fan_auto_switch() == "off"
    await rig.temp(0, LOW_RUNG)
    assert head.fan == "high"
    assert head.fan_writes[writes:] == []
    await rig.close()


async def test_a_head_that_only_reports_the_repeat_costs_one_pick_at_most(hass: HomeAssistant, freezer: Any) -> None:
    """F1's cost where it is widest. This head jumps straight to the last write, and MXZ can't
    tell that from the first high of the stream above, so medium stays an echo with no lag
    left. A pick of it is written over, the report of that ends the memory, and the next holds."""
    rig = await _queued_walk(hass, freezer, HIGH_RUNG, MEDIUM_RUNG, LOW_RUNG, HIGH_RUNG)
    head = rig.heads[0]
    await rig.report_fan()
    assert head.fan == "high"
    assert rig.coord.fan.older_commands(head.entity_id) == ["medium"]

    await rig.person_fan("medium")
    assert rig.fan_held() is False
    assert head.unreported_fan == "high"  # boost is writing over the pick
    await rig.report_fan()
    assert head.fan == "high"
    assert rig.coord.fan.older_commands(head.entity_id) == []

    writes = len(head.fan_writes)
    await rig.person_fan("medium")
    assert rig.fan_held() is True
    await rig.temp(0, HIGH_RUNG)
    assert head.fan == "medium"
    assert head.fan_writes[writes:] == []
    await rig.close()


async def test_a_prompt_head_shows_no_older_writes_after_a_repeat(hass: HomeAssistant, freezer: Any) -> None:
    """The repeat rule is only for a head behind: this one reported every write as it came."""
    rig = await open_rig(hass, freezer, temps=(TARGET, TARGET))
    head = rig.heads[0]
    for temp in (HIGH_RUNG, MEDIUM_RUNG, LOW_RUNG, HIGH_RUNG, MEDIUM_RUNG, HIGH_RUNG):
        await rig.temp(0, temp)
        assert head.fan == head.fan_writes[-1]
        assert rig.coord.fan.older_commands(head.entity_id) == []
        assert "older_fan_commands" not in _switch_attrs(rig)
    await rig.close()


async def _prompt_walk(hass: HomeAssistant, freezer: Any, *temps: float) -> Rig:
    """A head that reports each write before the next, which is then left in flight."""
    rig = await open_rig(hass, freezer, temps=(TARGET, TARGET), lags=True)
    for temp in temps[:-1]:
        await rig.temp(0, temp)
        await rig.report_fan()
    await rig.temp(0, temps[-1])
    return rig


@pytest.mark.parametrize(
    ("temps", "pick", "written"),
    [
        ((HIGH_RUNG, MEDIUM_RUNG, HIGH_RUNG, LOW_RUNG), "medium", ["high", "medium", "high", "low"]),
        ((HIGH_RUNG, MEDIUM_RUNG, LOW_RUNG), "high", ["high", "medium", "low"]),
    ],
    ids=["the-review-bounce", "no-bounce"],
)
async def test_a_prompt_heads_passed_speed_holds_during_the_next_writes_window(
    hass: HomeAssistant, freezer: Any, temps: tuple[float, ...], pick: str, written: list[str]
) -> None:
    """The head reported every write, so it can't show this speed again. One write is in flight,
    which is no lag at all: a pick of the speed before the last two is a person's, as on base."""
    rig = await _prompt_walk(hass, freezer, *temps)
    head = rig.heads[0]
    assert [token for token, _ in groupby(head.fan_writes)] == written
    assert head.unreported_fan == written[-1]
    assert rig.coord.fan.older_commands(head.entity_id) == []
    assert "older_fan_commands" not in _switch_attrs(rig)
    writes = len(head.fan_writes)
    await rig.person_fan(pick)
    assert rig.fan_held() is True
    assert rig.fan_auto_switch() == "off"
    await rig.temp(0, temps[-1])
    assert head.fan == pick
    assert head.fan_writes[writes:] == []
    await rig.close()


async def test_a_jumping_heads_passed_speed_holds_during_the_next_writes_window(hass: HomeAssistant, freezer: Any) -> None:
    """The findings walk's last row. The head jumped past medium to low, so medium can't come back."""
    rig = await _lagging_walk(hass, freezer)
    head = rig.heads[0]
    await rig.report_fan()
    await rig.temp(0, HIGH_RUNG)
    assert head.unreported_fan == "high"
    writes = len(head.fan_writes)
    await rig.person_fan("medium")
    assert rig.fan_held() is True
    assert head.fan == "medium"
    assert head.fan_writes[writes:] == []
    await rig.close()


@pytest.mark.parametrize("start", ["reload", "restart"])
async def test_known_cost_the_first_write_after_a_start_can_read_one_speed_more(
    hass: HomeAssistant, freezer: Any, start: str
) -> None:
    """F1's cost, pinned so it fails the day this changes. A start can't tell a head that has passed
    the speed before the last write from one still showing it, and the record of a prompt head
    carries nothing. So the first write after a start reads that speed as an echo, and a pick of
    it is written over. The report of that write ends this, and the next pick holds."""
    rig = await _prompt_walk(hass, freezer, HIGH_RUNG, MEDIUM_RUNG)
    head = rig.heads[0]
    await rig.report_fan()
    assert head.fan == "medium"
    assert rig.coord.fan.command_memory(head.entity_id) == ("medium", "high")
    await (rig.reload() if start == "reload" else rig.restart())
    await rig.temp(0, LOW_RUNG)
    assert head.unreported_fan == "low"
    assert rig.coord.fan.command_memory(head.entity_id) == ("low", "medium")
    assert rig.coord.fan.older_commands(head.entity_id) == ["high"]
    await rig.person_fan("high")
    assert rig.fan_held() is False
    assert head.unreported_fan == "low"  # boost is writing over the pick
    await rig.report_fan()
    assert head.fan == "low"
    assert rig.coord.fan.older_commands(head.entity_id) == []
    writes = len(head.fan_writes)
    await rig.person_fan("high")
    assert rig.fan_held() is True
    assert head.fan_writes[writes:] == []
    await rig.close()


def test_a_restored_repeat_comes_back_in_its_place() -> None:
    assert FanHoldRestoreData(False, "high", "low", older=("high", "medium")).as_dict()["older_fan_commands"] == ["high", "medium"]
    record = FanHoldRestoreData.from_dict({"held": False, "last_fan_command": "high", "older_fan_commands": ["high", "medium"]})
    assert record is not None
    assert record.older == ("high", "medium")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"held": False, "last_fan_command": "low", "older_fan_commands": ["high", "medium"]}, ("high", "medium")),
        ({"held": False, "last_fan_command": "low"}, ()),
        ({"held": False, "older_fan_commands": "high"}, ()),
        ({"held": False, "older_fan_commands": ["high", 3]}, ()),
        ({"held": False, "older_fan_commands": {"high": 1}}, ()),
    ],
    ids=["kept", "older-record", "a-string", "a-non-token", "a-dict"],
)
def test_the_restore_record_reads_only_a_clean_list(data: dict[str, Any], expected: tuple[str, ...]) -> None:
    record = FanHoldRestoreData.from_dict(data)
    assert record is not None
    assert record.older == expected


def test_a_record_with_no_older_writes_keeps_its_old_shape() -> None:
    assert FanHoldRestoreData(False, "low", "medium").as_dict() == {
        "held": False, "last_fan_command": "low", "prior_fan_command": "medium",
    }
    assert FanHoldRestoreData(False, "low", "medium", older=("high",)).as_dict()["older_fan_commands"] == ["high"]
