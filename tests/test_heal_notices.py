"""Heal notices with "Accept changes made at the head" on: no more than with it off (Discussion #103).

@BHSPitMonkey reported many more "head de-sync" HVAC Update notices after 3.22.0b1 turned the
option on. These hold MXZ to the rule that the option adds no heal notice: a heat/cool flip
with nothing to adopt holds nothing and is put back at the next compute, an off adopted as
"disable the room" owes no off-while-enabled heal, a cloud head's late report of an older
setpoint is no person, and a heal is logged every time but notified at most once per head per
hour, naming its room and head. The option-off tests are 3.21.0's behavior, kept.

All rooms, heads and values are invented, and the invented people change a head directly, as
its remote does.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.mxz_coordinator.cloud import CloudHead
from custom_components.mxz_coordinator.const import (
    BAND_DRIFT_DELAY,
    CONF_ACCEPT_HEAD_CHANGES,
    CONF_NOTIFY_SERVICE,
    OFF_WHILE_ENABLED_DELAY,
)
from tests.clocks import age_heal_notice
from tests.cloud_heads import past_window, recompute, record, writes
from tests.property_rig import TARGET, Rig, open_rig
from tests.test_accept_head_changes import (
    HELD,
    HOT,
    _armed,
    _cloud,
    _cloud_target,
    _edges,
    _elapse,
    _enabled,
    _target,
    _time_passes,
)
from tests.test_drive import _eid, _set_target

NOTIFY = "heal_test"
HOUR = 3600  # one heal notice per head per hour, as the floor notices are limited per room
HEAD = "climate.property_head_a"
OTHER = "climate.property_head_b"


async def _rig(
    hass: HomeAssistant, freezer: Any, *, accept: bool = True, **kwargs: Any,
) -> tuple[Rig, list[ServiceCall]]:
    """Both rooms on, the first one cooling, and a notification service to count notices on."""
    calls = async_mock_service(hass, "notify", NOTIFY)
    data = {CONF_NOTIFY_SERVICE: f"notify.{NOTIFY}", CONF_ACCEPT_HEAD_CHANGES: accept}
    rig = await open_rig(hass, freezer, temps=(HOT, TARGET), data={**data, **kwargs.pop("data", {})}, **kwargs)
    assert rig.heads[0].entity_id == HEAD
    await rig.later(60)  # nothing left over from the setup to compute for
    await _time_passes(rig)
    assert rig.heads[0].mode == "cool"
    calls.clear()
    return rig, calls


def _heals(calls: list[ServiceCall]) -> list[str]:
    """Every heal notice sent so far."""
    return [call.data["message"] for call in calls if "re-applying via the coordinator" in call.data["message"]]


async def _quietly(rig: Rig, seconds: int) -> None:
    """Time passes with no compute: only MXZ's own timers fire, one second at a time."""
    for _ in range(seconds):
        await rig.hass.async_block_till_done()
        rig.freezer.tick(timedelta(seconds=1))
        async_fire_time_changed(rig.hass, dt_util.utcnow())
        await rig.hass.async_block_till_done()


def _until_hour(rig: Rig) -> float:
    """Seconds left before this head's last heal notice is an hour old."""
    return HOUR - (time.monotonic() - rig.coord._heal_notice_at[HEAD])


def _off_timer(rig: Rig) -> bool:
    return (HEAD, "off") in rig.coord._heal_timers


# ---------------------------------------------------------------------------
# A flip with nothing to adopt holds nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flip", ["heat", "cool"])
async def test_an_opposite_mode_flip_holds_nothing_and_the_next_compute_puts_it_back(
    hass: HomeAssistant, freezer: Any, flip: str,
) -> None:
    """Nothing to adopt in a heat/cool choice, so nothing to protect: as with the option off."""
    rig, _calls = await _rig(hass, freezer)
    if flip == "cool":
        await rig.temp(0, TARGET - 5)
        await rig.select_mode("heat")
        assert rig.heads[0].mode == "heat"
        flip_from = "heat"
    else:
        flip_from = "cool"
    edges = _edges(rig)
    rig.heads[0].person_mode(flip)
    await hass.async_block_till_done()
    assert not _armed(rig)
    assert rig.coord._head_change_settling(HEAD) is False
    await rig.settle()  # one compute, no time for a hold
    assert rig.heads[0].mode == flip_from
    assert _edges(rig) == edges
    assert _target(rig) == TARGET
    assert _enabled(rig) is True
    await rig.close()


async def test_an_empty_change_never_counts_as_settling(hass: HomeAssistant, freezer: Any) -> None:
    """Even with an adopt timer armed some other way: a change with nothing in it protects nothing."""
    rig, _calls = await _rig(hass, freezer)
    rig.coord.accept_head_changes = False  # the flip lands with nothing computed for it...
    rig.heads[0].person_mode("heat")
    await hass.async_block_till_done()
    rig.coord.accept_head_changes = True  # ...and is read as the option reads it
    assert rig.heads[0].mode == "heat"
    rig.coord._adopt_moved[HEAD] = {"mode"}
    rig.coord._arm_or_cancel(HEAD, "adopt", True, HELD)
    assert rig.coord._head_adoption(HEAD, {"mode"}) == {}
    assert rig.coord._head_change_settling(HEAD) is False
    await rig.close()


async def test_a_flip_with_a_new_setpoint_still_holds(hass: HomeAssistant, freezer: Any) -> None:
    """The negative of the two above: a change with a target in it is protected as before."""
    rig, _calls = await _rig(hass, freezer)
    rig.heads[0].person_setpoint(mode="heat", low=72, high=74)
    await hass.async_block_till_done()
    assert _armed(rig) is True
    assert rig.coord._head_change_settling(HEAD) is True
    await rig.settle()
    assert rig.heads[0].mode == "heat"  # not undone inside the hold
    await rig.later(HELD)
    assert _target(rig) == 72
    assert rig.heads[0].mode == "cool"
    await rig.close()


@pytest.mark.parametrize("mode", ["auto", "dry", "heat_cool"])
async def test_a_flip_then_a_banned_mode_heals_once_after_the_band_delay(
    hass: HomeAssistant, freezer: Any, mode: str,
) -> None:
    """A genuine remote Auto / Dry / heat_cool is still healed in 20 s, as documented, with one notice."""
    rig, calls = await _rig(hass, freezer)
    rig.heads[0].person_mode("heat")
    await _quietly(rig, 2)
    rig.heads[0].person_mode(mode)
    await _quietly(rig, BAND_DRIFT_DELAY - 1)
    assert _heals(calls) == []
    assert rig.heads[0].mode == mode
    await _quietly(rig, 2)
    assert rig.heads[0].mode == "cool"
    assert _heals(calls) == [
        f"Study: {HEAD} was in {mode}, off its coordinated mode - re-applying via the coordinator."
    ]
    assert _target(rig) == TARGET
    assert _enabled(rig) is True
    await rig.close()


async def test_flips_inside_an_off_window_dont_add_a_notice(hass: HomeAssistant, freezer: Any) -> None:
    """Off at the head, then heat and off again: one adopted off, no heal notice at all."""
    rig, calls = await _rig(hass, freezer)
    rig.heads[0].person_mode("off")
    await _quietly(rig, 2)
    rig.heads[0].person_mode("heat")
    await _quietly(rig, 2)
    rig.heads[0].person_mode("off")
    await _quietly(rig, OFF_WHILE_ENABLED_DELAY + 5)
    assert _enabled(rig) is False
    assert _heals(calls) == []
    await rig.close()


# ---------------------------------------------------------------------------
# An off adopted as "disable the room" owes no off-while-enabled heal
# ---------------------------------------------------------------------------


async def test_an_adopted_off_cancels_its_off_heal(hass: HomeAssistant, freezer: Any) -> None:
    rig, calls = await _rig(hass, freezer)
    rig.heads[0].person_mode("off")
    await hass.async_block_till_done()
    assert _off_timer(rig) is True  # held, not yet the room's
    await _quietly(rig, HELD)
    assert _enabled(rig) is False
    assert _off_timer(rig) is False  # the off is the room's now: nothing left to heal
    await _quietly(rig, OFF_WHILE_ENABLED_DELAY)
    assert _heals(calls) == []
    assert rig.heads[0].mode == "off"
    await rig.close()


async def test_a_room_re_enabled_while_its_head_still_shows_the_adopted_off_gets_no_leftover_notice(
    hass: HomeAssistant, freezer: Any,
) -> None:
    """Re-enabled in Home Assistant inside the old 30 s window, with a head slow to show the on.

    The off was the person's and is honoured; the re-enable is new, and its own write is still
    on its way. A leftover timer from the off must not call that a drift.
    """
    rig, calls = await _rig(hass, freezer)
    head = rig.heads[0]
    head.person_mode("off")
    await _quietly(rig, HELD)
    assert _enabled(rig) is False
    head.lags_commands = True
    await rig.switch("_primary_enable", True)
    assert head.mode == "off"  # MXZ's cool hasn't shown yet
    await _quietly(rig, OFF_WHILE_ENABLED_DELAY)
    assert _heals(calls) == []
    head.lags_commands = False
    while head.report_next_command():
        await hass.async_block_till_done()
    assert head.mode == "cool"
    await rig.close()


async def test_an_off_that_isnt_adopted_still_heals_and_notifies(hass: HomeAssistant, freezer: Any) -> None:
    """The negative: while nothing is adopted (Home Assistant starting), off is drift, as in 3.21.0."""
    rig, calls = await _rig(hass, freezer)
    rig.coord._startup_quiet = True
    rig.heads[0].person_mode("off")
    await _quietly(rig, OFF_WHILE_ENABLED_DELAY - 1)
    assert _heals(calls) == []
    rig.coord._startup_quiet = False
    await _quietly(rig, 2)
    assert _enabled(rig) is True
    assert rig.heads[0].mode == "cool"
    assert _heals(calls) == [
        f"Study: {HEAD} was off while its room is enabled - re-applying via the coordinator."
    ]
    await rig.close()


# ---------------------------------------------------------------------------
# With the option off: 3.21.0's notices, and nothing adopted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "delay", "message"),
    [
        ("auto", BAND_DRIFT_DELAY, "was in auto, off its coordinated mode"),
        ("dry", BAND_DRIFT_DELAY, "was in dry, off its coordinated mode"),
        ("heat_cool", BAND_DRIFT_DELAY, "was in heat_cool, off its coordinated mode"),
        ("off", OFF_WHILE_ENABLED_DELAY, "was off while its room is enabled"),
    ],
)
async def test_option_off_heals_and_notifies_as_before(
    hass: HomeAssistant, freezer: Any, mode: str, delay: int, message: str,
) -> None:
    rig, calls = await _rig(hass, freezer, accept=False)
    rig.heads[0].person_setpoint(mode=mode, low=60, high=62)
    await hass.async_block_till_done()
    assert not _armed(rig)
    await _quietly(rig, delay - 1)
    assert _heals(calls) == []
    assert rig.heads[0].mode == mode
    await _quietly(rig, 2)
    assert rig.heads[0].mode == "cool"
    assert _edges(rig) == (TARGET - 2, TARGET)
    assert _heals(calls) == [f"Study: {HEAD} {message} - re-applying via the coordinator."]
    assert _target(rig) == TARGET
    assert _enabled(rig) is True
    await rig.close()


async def test_option_off_a_flip_is_put_back_at_once(hass: HomeAssistant, freezer: Any) -> None:
    rig, calls = await _rig(hass, freezer, accept=False)
    rig.heads[0].person_mode("heat")
    await hass.async_block_till_done()
    await rig.settle()
    assert rig.heads[0].mode == "cool"
    await _time_passes(rig)
    assert _heals(calls) == []
    await rig.close()


# ---------------------------------------------------------------------------
# Every heal logged; one notice per head per hour, naming its room and head
# ---------------------------------------------------------------------------


async def test_a_heal_is_logged_every_time_and_notified_once_per_head_per_hour(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    rig, calls = await _rig(hass, freezer)
    caplog.set_level(logging.WARNING, logger="custom_components.mxz_coordinator")
    for _ in range(3):
        rig.heads[0].person_mode("auto")
        await _quietly(rig, BAND_DRIFT_DELAY + 1)
        assert rig.heads[0].mode == "cool"  # healed every time
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("MXZ heal:")]
    assert len(lines) == 3
    assert all("room Study" in line and HEAD in line and "band" in line and "auto" in line for line in lines)
    assert "not sent" not in lines[0]
    assert all("not sent: one heal notice per head per hour" in line for line in lines[1:])
    assert len(_heals(calls)) == 1
    # The hour runs on the coordinator's own clock. A heal 5 s short of it is held back...
    age_heal_notice(rig.coord, HEAD, _until_hour(rig) - (BAND_DRIFT_DELAY + 5))
    rig.heads[0].person_mode("dry")
    await _quietly(rig, BAND_DRIFT_DELAY + 1)
    assert rig.heads[0].mode == "cool"
    assert len(_heals(calls)) == 1
    # ...and the next one, past it, is sent.
    rig.heads[0].person_mode("heat_cool")
    await _quietly(rig, BAND_DRIFT_DELAY + 1)
    assert _heals(calls)[1:] == [
        f"Study: {HEAD} was in heat_cool, off its coordinated mode - re-applying via the coordinator."
    ]
    await rig.close()


async def test_the_limit_is_per_head(hass: HomeAssistant, freezer: Any) -> None:
    """Another head's heal in the same hour is its own notice."""
    rig, calls = await _rig(hass, freezer)
    rig.heads[0].person_mode("auto")
    await _quietly(rig, BAND_DRIFT_DELAY + 1)
    rig.heads[1].person_mode("dry")
    await _quietly(rig, BAND_DRIFT_DELAY + 1)
    assert _heals(calls) == [
        f"Study: {HEAD} was in auto, off its coordinated mode - re-applying via the coordinator.",
        f"Den: {OTHER} was in dry, off its coordinated mode - re-applying via the coordinator.",
    ]
    await rig.close()


async def test_a_held_back_notice_still_heals(hass: HomeAssistant, freezer: Any) -> None:
    """The limit is on the notice only: an off inside the hour is put back all the same."""
    rig, calls = await _rig(hass, freezer, accept=False)
    rig.heads[0].person_mode("auto")
    await _quietly(rig, BAND_DRIFT_DELAY + 1)
    rig.heads[0].person_mode("off")
    await _quietly(rig, OFF_WHILE_ENABLED_DELAY + 1)
    assert rig.heads[0].mode == "cool"
    assert len(_heals(calls)) == 1
    await rig.close()


# ---------------------------------------------------------------------------
# A cloud head's late report of an older setpoint is no person
# ---------------------------------------------------------------------------


def test_a_cloud_batch_window_reads_an_older_setpoint_as_stale() -> None:
    head = CloudHead(write_interval=60, confirm_wait=90, window_until=1090.0)
    head.sent(2000.0, ("cool", 72.0, None), "cool", None, 70.0)
    assert head.stale(2010.0, setpoint=70.0)  # what it showed before the batch
    assert not head.stale(2010.0, setpoint=72.0)  # the batch's own: the echo
    assert not head.stale(2010.0, setpoint=66.0)  # never had it: a real change
    assert not head.stale(2090.0, setpoint=70.0)  # after the window, nothing is stale
    # An unconfirmed setpoint becomes an older one when the next batch moves on.
    head.sent(2100.0, ("cool", 74.0, None), "cool", None, 70.0)
    assert head.stale(2110.0, setpoint=72.0)
    assert not head.stale(2110.0, setpoint=74.0)
    # On the head's grid, not by equality.
    near = lambda reported, older: abs(reported - older) < 0.5
    assert head.stale(2110.0, setpoint=70.3, same=near)
    assert not head.stale(2110.0, setpoint=70.3)
    # A batch with no setpoint supersedes none; a confirmed one clears them.
    head.confirmed = True
    head.sent(2200.0, ("fan_only", None, None), "cool", None, 74.0)
    assert head.older_setpoints == set()
    assert not head.stale(2210.0, setpoint=74.0)


async def _cloud_moved_to(hass: HomeAssistant, offset: float | None) -> tuple[Any, Any, float, float]:
    """A cloud head cooling, then its room's target moved to 72: the head shows the new setpoint."""
    head, entry = await _cloud(hass, offset=offset)
    before = head.device["temperature"]
    past_window(entry, head.entity_id)
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 72.0)
    await recompute(hass, entry)
    after = head.device["temperature"]
    assert after != before
    assert hass.states.get(head.entity_id).attributes["temperature"] == after
    assert entry.runtime_data._cloud[head.entity_id].pending(time.monotonic())
    return head, entry, before, after


@pytest.mark.parametrize("offset", [0.0, None], ids=["no_offset", "driven"])
async def test_a_stale_cloud_setpoint_in_its_window_starts_no_adopt(
    hass: HomeAssistant, offset: float | None,
) -> None:
    """A poll from before the batch, shown again inside the confirm wait: no hold, no target."""
    head, entry, before, _after = await _cloud_moved_to(hass, offset)
    coordinator = entry.runtime_data
    head.report(temperature=before)  # the cloud still had the old setpoint on this poll
    await hass.async_block_till_done()
    assert (head.entity_id, "adopt") not in coordinator._heal_timers
    assert coordinator._head_change_settling(head.entity_id) is False
    await _elapse(hass, HELD)
    assert _cloud_target(hass, entry) == 72.0
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "on"


@pytest.mark.parametrize("offset", [0.0, None], ids=["no_offset", "driven"])
async def test_a_stale_cloud_setpoint_doesnt_skip_the_next_apply(
    hass: HomeAssistant, offset: float | None,
) -> None:
    """Once its window ends the head is written back to the plan's setpoint at the next compute."""
    head, entry, before, after = await _cloud_moved_to(hass, offset)
    head.report(temperature=before)
    await hass.async_block_till_done()
    calls = record(hass)
    past_window(entry, head.entity_id)
    await recompute(hass, entry)
    assert ("set_temperature", {"temperature": after}) in writes(calls, head.entity_id)
    assert head.device["temperature"] == after


async def test_a_cloud_setpoint_the_head_never_had_is_still_adopted_in_its_window(hass: HomeAssistant) -> None:
    """The negative: only an older state is stale; a new value is a person's, window or not."""
    head, entry, _before, _after = await _cloud_moved_to(hass, 0.0)
    head.device["temperature"] = 66.0
    head.poll()
    await hass.async_block_till_done()
    assert (head.entity_id, "adopt") in entry.runtime_data._heal_timers
    await _elapse(hass, HELD)
    assert _cloud_target(hass, entry) == 66.0


async def test_an_older_cloud_setpoint_after_the_window_is_a_person(hass: HomeAssistant) -> None:
    """The window is the whole allowance: past it, the old value at the head is a press."""
    head, entry, before, _after = await _cloud_moved_to(hass, 0.0)
    past_window(entry, head.entity_id)
    head.device["temperature"] = before
    head.poll()
    await hass.async_block_till_done()
    await _elapse(hass, HELD)
    assert _cloud_target(hass, entry) == before
