"""F1: a slow local head's report of an older fan write of MXZ's isn't a person's pick.

The coordinator keeps its older fan writes, per head, while the head hasn't reported
the last one (``MXZCoordinator._fan_older``). A speed MXZ didn't recently write is
still a person's pick, held at once and across a start. docs/formal/PARKED-FINDINGS.md
has the finding and docs/formal/README.md the rule. All rooms and heads are invented.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import FAN_LADDER, FAN_OLDER_WRITES
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


def _switch_attrs(rig: Rig) -> dict[str, Any]:
    return rig.hass.states.get(rig.eid("_primary_fan_auto")).attributes


async def test_the_findings_walk_holds_nothing_and_boost_keeps_driving(hass: HomeAssistant, freezer: Any) -> None:
    """Each step of PARKED-FINDINGS.md's table, with the hold it used to show gone."""
    rig = await _lagging_walk(hass, freezer)
    assert rig.fan_held() is False
    assert rig.fan_auto_switch() == "on"
    await rig.report_fan()
    assert rig.heads[0].fan == "low"
    assert rig.fan_held() is False
    writes = len(rig.heads[0].fan_writes)
    await rig.temp(0, HIGH_RUNG)
    assert set(rig.heads[0].fan_writes[writes:]) == {"high"}  # rewritten until it reports
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
    assert rig.coord.fan_older_commands(head.entity_id) == []
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
    assert rig.coord.fan_older_commands(head.entity_id) == ["high"]
    await rig.report_fan()
    assert rig.coord.fan_older_commands(head.entity_id) == []
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
    assert rig.coord.fan_command_memory(head.entity_id) == ("low", "medium")
    assert rig.coord.fan_older_commands(head.entity_id) == ["high"]
    assert rig.fan_held() is False
    assert rig.fan_auto_switch() == "on"
    await rig.report_fan()
    assert rig.fan_held() is False
    assert "older_fan_commands" not in _switch_attrs(rig)
    await rig.close()


async def test_a_record_without_the_memory_still_holds_conservatively(hass: HomeAssistant, freezer: Any) -> None:
    """A record from before this field: _reconcile_restored_hold's documented false hold,
    unchanged. It is what the memory above is for, so a start with it lost must show it."""
    rig = await _lagging_walk(hass, freezer)
    rig.coord.restore_fan_hold(rig.heads[0].entity_id, False, last="low", prior="medium")
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
    rig.coord.restore_fan_hold(head.entity_id, False, last="low", prior="quiet", older=("high", "diffuse", "medium"))
    assert rig.coord.fan_older_commands(head.entity_id) == ["medium"]
    await rig.settle()
    assert rig.fan_held() is True
    await rig.close()


async def test_a_hold_and_a_hand_back_leave_no_older_writes(hass: HomeAssistant, freezer: Any) -> None:
    """A hold's memory is the held speed alone, and handing it back starts fresh."""
    rig = await _lagging_walk(hass, freezer)
    head = rig.heads[0]
    await rig.switch("_primary_fan_auto", False)
    assert rig.coord.fan_older_commands(head.entity_id) == []
    await rig.switch("_primary_fan_auto", True)
    assert rig.coord.fan_older_commands(head.entity_id) == []
    await rig.close()

    rig = await _lagging_walk(hass, freezer)
    await rig.person_fan("quiet")
    assert rig.coord.fan_older_commands(rig.heads[0].entity_id) == []
    await rig.close()


async def test_the_memory_is_bounded_and_per_head(hass: HomeAssistant, freezer: Any) -> None:
    """Past the bound the oldest goes, and a report of it reads as a pick again."""
    rig = await open_rig(hass, freezer)
    coord = rig.coord
    head, other = rig.heads[0].entity_id, rig.heads[1].entity_id
    tokens = [f"speed{i}" for i in range(FAN_OLDER_WRITES + 6)]
    coord._fan_cmd[head], coord._fan_prev[head] = "auto", "auto"
    for token in tokens:
        coord._remember_fan_write(head, token)
    older = coord.fan_older_commands(head)
    assert len(older) == FAN_OLDER_WRITES >= len(FAN_LADDER)
    assert older == tokens[-(FAN_OLDER_WRITES + 2):-2]
    assert coord.fan_older_commands(other) == []

    assert coord._observe_fan_latch(head, older[0], "cool", 1.0) is False
    assert coord._observe_fan_latch(head, tokens[0], "cool", 1.0) is True
    await rig.close()


async def test_a_rewrite_and_a_repeat_keep_the_memory_exact(hass: HomeAssistant, freezer: Any) -> None:
    """No speed twice, none of the last two, no auto, and a rewrite rolls nothing (#14)."""
    rig = await open_rig(hass, freezer)
    coord, head = rig.coord, rig.heads[0].entity_id
    coord._fan_cmd[head], coord._fan_prev[head] = "auto", "auto"
    for token in ("high", "medium", "medium", "low", "high", "low"):
        coord._remember_fan_write(head, token)
    assert coord.fan_command_memory(head) == ("low", "high")
    assert coord.fan_older_commands(head) == ["medium"]
    await rig.close()


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
