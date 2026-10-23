"""A lost head's vote: live, then stale for a time limit, then expired.

Adapted from @csingley's ha-climate-zone-sequencer, commit f911e4a
(``models.py``: ``Zone.observe`` / ``lost`` / ``status`` / ``vote``; MIT
License, see NOTICE). A head MXZ loses sight of (unavailable, or gone from
Home Assistant) keeps the vote it last showed, marked stale, for "Keep a lost
head's vote for" (30 minutes by default). Then the vote expires:

* a released hands-off head's heat or cool stops counting (before this, it
  stopped counting the moment the head went unavailable);
* an enabled room stops holding the shared mode with its demand or its run,
  so a sibling can be served (before this, it held it for as long as the head
  was gone, forever included).

A head seen again is live at once. A quiet head (one that reports nothing new)
is in sight; so is one reading ``unknown``. A head not seen since MXZ started
is ``unknown``: it has no last vote, and is timed from that start. Kept as in
3.10.0: a room's Safety priority want, and a tripped floor or ceiling.

Monotonic clock, as for the dwell: tests age the stamp (``tests/clocks.py``)
and fire HA's timers for real. Fake heads and sensors only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

import custom_components.mxz_coordinator as component
from custom_components.mxz_coordinator import coordinator as coordinator_module
from custom_components.mxz_coordinator.const import (
    CONF_RESTING_MODE_BIAS,
    MODE_COOL,
    MODE_HEAT,
)
from custom_components.mxz_coordinator.logic import HeadSight
from tests.clocks import age_lost_vote, jump_wall_clock
from tests.dwell import expire_dwell
from tests.flow_pages import (
    default,
    marker_of,
    open_options_page,
    prefilled,
    press,
    sections,
    setup_tuning,
    submit,
    suggested,
)
from tests.test_configure_menu import HEADS, SENSORS, _entry, _setup_review
from tests.test_configure_menu import _head as _flow_head
from tests.test_drive import SENSOR_A, SENSOR_B, _recompute, _set_temp
from tests.test_dwell_wakeup import _count_refresh_requests, _fire_due, _quiesce
from tests.test_floor import _setup as _floor_setup
from tests.test_hands_off import _plan, _remote, _s, _setup
from tests.test_startup_continuity import _head

# Stored key and default, written out independently of const.py.
TTL = "lost_vote_ttl"
DEFAULT_SECONDS = 30 * 60
SAFETY_A = "binary_sensor.primary_safety"
SAFETY_B = "binary_sensor.secondary_safety"


def _row(hass: HomeAssistant, entry, room: int) -> dict[str, Any]:
    return _plan(hass, entry).attributes["zones"][room]


def _vote(hass: HomeAssistant, entry, room: int) -> tuple[Any, Any, Any]:
    """(hands_off_vote, vote_status, lost_since) of one room's plan row."""
    row = _row(hass, entry, room)
    return row["hands_off_vote"], row["vote_status"], row["lost_since"]


async def _lose(hass: HomeAssistant, head: str) -> None:
    """The head drops off, as a real one does: unavailable, takes no commands."""
    entity = _head(hass, head)
    entity._attr_available = False
    entity.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(head).state == "unavailable"


async def _back(hass: HomeAssistant, head: str) -> None:
    entity = _head(hass, head)
    entity._attr_available = True
    entity.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(head).state != "unavailable"


def _since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    return parsed


async def _hands_off_heat(hass: HomeAssistant, **extra: Any):
    """Primary enabled at target; Secondary a released hands-off head a person
    runs in heat, against a cool resting bias: the shared mode is heat by its
    vote alone (probe 1's setup)."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None, **{CONF_RESTING_MODE_BIAS: "cool"}, **extra,
    )
    coord = entry.runtime_data
    await _recompute(hass, entry)
    expire_dwell(coord)
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_HEAT
    assert plan.attributes["mode_by_hands_off"] is True
    assert _vote(hass, entry, 1) == (MODE_HEAT, "live", None)
    return entry, head_a, head_b


async def _standoff(hass: HomeAssistant, *, hysteresis: int | None = None, **extra: Any):
    """Both rooms enabled: Primary calls cool, Secondary heat (probe 2's
    setup). Primary is room 0, so it wins the standoff: cool."""
    entry, head_a, head_b = await _setup(hass, hysteresis=hysteresis, **extra)
    await _set_temp(hass, SENSOR_A, 76)
    await _set_temp(hass, SENSOR_B, 64)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_COOL
    assert plan.attributes["standoff"] is True
    assert _row(hass, entry, 0)["vote_status"] == "live"
    return entry, head_a, head_b


# --- the record itself (logic.HeadSight) --------------------------------------


def test_sight_live_stale_expired_with_a_half_open_boundary() -> None:
    sight = HeadSight(100.0, 5000.0)
    sight.observe("heat")
    assert sight.status(200.0, 60.0) == "live"
    assert sight.vote(200.0, 60.0) == "heat"
    assert sight.expires_in(200.0, 60.0) is None
    assert (sight.lost_at, sight.lost_wall) == (None, None)

    sight.lose(1000.0, 9000.0)
    sight.lose(1030.0, 9030.0)  # still lost: the limit runs from the first call
    assert (sight.lost_at, sight.lost_wall) == (1000.0, 9000.0)
    assert sight.status(1059.999, 60.0) == "stale"
    assert sight.vote(1059.999, 60.0) == "heat"
    assert sight.expires_in(1045.0, 60.0) == 15.0
    # Expired AT the limit, not after it (like a sensor's freshness cutoff).
    assert sight.status(1060.0, 60.0) == "expired"
    assert sight.vote(1060.0, 60.0) is None
    assert sight.expires_in(1060.0, 60.0) is None

    sight.observe("cool")  # seen again: live at once, the clock is gone
    assert sight.status(5000.0, 60.0) == "live"
    assert sight.vote(5000.0, 60.0) == "cool"


def test_sight_never_seen_is_unknown_and_timed_from_the_start() -> None:
    sight = HeadSight(100.0, 5000.0)
    assert (sight.lost_at, sight.lost_wall) == (100.0, 5000.0)
    sight.lose(150.0, 5050.0)  # out of sight since the start, not since now
    assert sight.status(159.0, 60.0) == "unknown"
    assert sight.vote(159.0, 60.0) is None  # no last vote to keep
    assert sight.expires_in(150.0, 60.0) == 10.0
    assert sight.status(160.0, 60.0) == "expired"


@pytest.mark.parametrize("mode", ["off", "fan_only", "dry", "auto", "heat_cool", "unknown"])
def test_sight_only_heat_and_cool_are_votes_live_or_stale(mode: str) -> None:
    sight = HeadSight(0.0, 0.0)
    sight.observe(mode)
    assert sight.vote(1.0, 60.0) is None
    sight.lose(2.0, 2.0)
    assert sight.status(3.0, 60.0) == "stale"
    assert sight.vote(3.0, 60.0) is None


# --- a released hands-off head (probe 1) --------------------------------------


async def test_probe1_a_hands_off_heat_vote_goes_stale_then_expires(
    hass: HomeAssistant,
) -> None:
    """Probe 1, as a real test. On 3.10.0 the heat vote vanished the moment
    the head went unavailable and the shared mode fell back to cool. Now it
    is kept, stale, for the time limit, and only then expires."""
    entry, head_a, head_b = await _hands_off_heat(hass)
    coord = entry.runtime_data
    before = dt_util.utcnow()
    hass.states.async_set(head_b, "unavailable")  # exactly as the probe did
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_HEAT
    vote, status, since = _vote(hass, entry, 1)
    assert (vote, status) == (MODE_HEAT, "stale")
    assert before <= _since(since) <= dt_util.utcnow()

    # Five seconds short of the default 30 minutes: still stale.
    age_lost_vote(coord, head_b, DEFAULT_SECONDS - 5)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_HEAT
    assert _vote(hass, entry, 1) == (MODE_HEAT, "stale", since)

    # The rest of it: expired, and the mode goes back to resting cool.
    age_lost_vote(coord, head_b, 5)
    expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == MODE_COOL
    assert _vote(hass, entry, 1) == (None, "expired", since)
    assert plan.attributes["standoff"] is False
    assert _s(hass, head_a) == "fan_only"
    assert _s(hass, head_b) == "unavailable"  # nothing was sent to it


async def test_the_last_vote_is_what_the_head_showed_just_before_it_was_lost(
    hass: HomeAssistant,
) -> None:
    """A person turns the head off and it drops off before any recompute ran:
    its last vote is that off (none), not the heat MXZ last computed with."""
    entry, _head_a, head_b = await _hands_off_heat(hass)
    coord = entry.runtime_data
    expire_dwell(coord)
    entity = _head(hass, head_b)
    entity._attr_hvac_mode = "off"
    entity.async_write_ha_state()
    await hass.async_block_till_done()
    assert coord.data["zones"][1]["hands_off_vote"] == MODE_HEAT  # not recomputed
    await _lose(hass, head_b)
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1)[:2] == (None, "stale")
    assert _plan(hass, entry).state == MODE_COOL


async def test_a_hands_off_head_seen_again_is_live_at_once(hass: HomeAssistant) -> None:
    entry, _head_a, head_b = await _hands_off_heat(hass)
    coord = entry.runtime_data
    await _lose(hass, head_b)
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1)[:2] == (MODE_HEAT, "stale")
    assert coord._lost_timer is not None

    await _back(hass, head_b)
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1) == (MODE_HEAT, "live", None)
    assert coord._lost_timer is None  # nothing left to expire

    # And from expired, too.
    await _lose(hass, head_b)
    await _recompute(hass, entry)
    age_lost_vote(coord, head_b, DEFAULT_SECONDS)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1)[:2] == (None, "expired")
    assert _plan(hass, entry).state == MODE_COOL
    await _back(hass, head_b)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1) == (MODE_HEAT, "live", None)
    assert _plan(hass, entry).state == MODE_HEAT


async def test_a_missing_head_is_lost_like_an_unavailable_one(hass: HomeAssistant) -> None:
    """Gone from Home Assistant altogether: MXZ can neither read nor steer it."""
    entry, _head_a, head_b = await _hands_off_heat(hass)
    coord = entry.runtime_data
    hass.states.async_remove(head_b)
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert hass.states.get(head_b) is None
    assert _vote(hass, entry, 1)[:2] == (MODE_HEAT, "stale")
    assert _plan(hass, entry).state == MODE_HEAT
    age_lost_vote(coord, head_b, DEFAULT_SECONDS)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1)[:2] == (None, "expired")
    assert _plan(hass, entry).state == MODE_COOL


@pytest.mark.parametrize("gone", ["unavailable", "missing"])
async def test_the_compute_itself_sees_a_head_lost_without_an_event(
    hass: HomeAssistant, gone: str
) -> None:
    """A loss the head listener never saw (before setup subscribes it, say):
    the compute reads the head itself and starts the clock there."""
    entry, _head_a, head_b = await _hands_off_heat(hass)
    coord = entry.runtime_data
    if gone == "missing":
        hass.states.async_remove(head_b)
    else:
        hass.states.async_set(head_b, "unavailable")
    await hass.async_block_till_done()
    coord._sight[head_b].observe(MODE_HEAT)  # as if no event had been seen
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1)[:2] == (MODE_HEAT, "stale")
    assert coord._sight[head_b].lost_at is not None


# --- an enabled room (probe 2) ------------------------------------------------


async def test_probe2_an_enabled_room_with_a_long_lost_head_lets_its_sibling_run(
    hass: HomeAssistant,
) -> None:
    """Probe 2, as a real test. On 3.10.0 the primary kept its cool demand in
    arbitration with its head gone for hours, and the secondary's heat waited
    forever. Now it holds while stale, then lets go."""
    entry, head_a, head_b = await _standoff(hass)
    coord = entry.runtime_data
    await _lose(hass, head_a)
    expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    row = _row(hass, entry, 0)
    assert (row["vote_status"], row["demand"]) == ("stale", MODE_COOL)
    assert plan.state == MODE_COOL  # stale: it still holds
    assert plan.attributes["standoff"] is True
    since = row["lost_since"]

    age_lost_vote(coord, head_a, 3 * 3600)  # three hours, as in the probe
    expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    row = _row(hass, entry, 0)
    assert plan.state == MODE_HEAT
    assert plan.attributes["standoff"] is False
    # The room's own call is still shown; it just no longer counts.
    assert (row["vote_status"], row["lost_since"], row["demand"]) == (
        "expired", since, MODE_COOL,
    )
    assert plan.attributes["primary_demand"] == MODE_COOL
    assert _row(hass, entry, 1)["vote_status"] == "live"
    assert _s(hass, head_b) == MODE_HEAT
    assert _s(hass, head_a) == "unavailable"

    # Back: live at once, its demand counts again (the dwell then decides).
    await _back(hass, head_a)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "live"
    assert _row(hass, entry, 0)["lost_since"] is None
    assert plan.attributes["standoff"] is True
    assert plan.state == MODE_HEAT  # the flip to heat just started the dwell
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_COOL


async def test_an_enabled_room_holds_until_exactly_the_time_limit(
    hass: HomeAssistant,
) -> None:
    entry, head_a, _head_b = await _standoff(hass)
    coord = entry.runtime_data
    await _lose(hass, head_a)
    await _recompute(hass, entry)
    age_lost_vote(coord, head_a, DEFAULT_SECONDS - 2)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_COOL
    assert _row(hass, entry, 0)["vote_status"] == "stale"
    age_lost_vote(coord, head_a, 2)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_HEAT
    assert _row(hass, entry, 0)["vote_status"] == "expired"


async def test_an_expired_room_no_longer_holds_its_run_against_a_vote(
    hass: HomeAssistant,
) -> None:
    """An enabled room mid-run (engaged, back inside its demand band) keeps a
    hands-off vote from taking the mode. Once its head's vote expires, that
    run holds nothing, and the vote is served as a vote's flip."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None,
    )
    coord = entry.runtime_data
    expire_dwell(coord)
    await _set_temp(hass, SENSOR_A, 66)  # Primary calls heat
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_HEAT
    await _set_temp(hass, SENSOR_A, 68.5)  # no demand, still running to 70
    await _remote(hass, head_b, "cool")
    expire_dwell(coord)
    await _recompute(hass, entry)
    row = _row(hass, entry, 0)
    assert (row["demand"], row["engage"]) == ("neutral", MODE_HEAT)
    assert _vote(hass, entry, 1)[0] == MODE_COOL
    assert _plan(hass, entry).state == MODE_HEAT  # the run holds the mode

    await _lose(hass, head_a)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "stale"
    assert _plan(hass, entry).state == MODE_HEAT  # stale: the run still holds

    age_lost_vote(coord, head_a, DEFAULT_SECONDS)
    stamp = expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    row = _row(hass, entry, 0)
    assert row["vote_status"] == "expired"
    assert row["engage"] == MODE_HEAT  # its own latch, untouched
    assert plan.state == MODE_COOL
    assert plan.attributes["mode_by_hands_off"] is True
    assert coord._last_mode_change_ts == stamp  # a vote's flip starts no dwell


async def test_the_dwell_wakeup_sees_the_expiry(hass: HomeAssistant) -> None:
    """With the dwell holding the flip back, the wakeup is armed for it: the
    replay counts an expired room exactly as the compute did."""
    entry, head_a, _head_b = await _standoff(hass)
    coord = entry.runtime_data
    await _lose(hass, head_a)
    await _recompute(hass, entry)
    assert coord._dwell_timer is None  # stale: cool holds, nothing to wait for
    age_lost_vote(coord, head_a, DEFAULT_SECONDS)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "expired"
    assert plan.state == MODE_COOL  # deferred by the dwell...
    assert plan.attributes["mode_change_allowed"] is False
    assert coord._dwell_timer is not None  # ...and woken when it runs out


async def test_a_wall_clock_step_neither_shortens_nor_stretches_the_limit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, head_a, _head_b = await _standoff(hass)
    coord = entry.runtime_data
    await _lose(hass, head_a)
    await _recompute(hass, entry)
    jump_wall_clock(monkeypatch, 3 * 3600)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "stale"
    jump_wall_clock(monkeypatch, -6 * 3600)
    age_lost_vote(coord, head_a, DEFAULT_SECONDS)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "expired"
    assert _plan(hass, entry).state == MODE_HEAT


# --- the expiry wakeup --------------------------------------------------------


def _count_lost_wakeups(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record each lost-vote wakeup that actually runs (its delay), from here on.

    Counted at the wakeup itself, not as refresh requests: on HA 2024.12 the
    refresh guard queues its own follow-up request when the recompute's head
    commands echo back, which says nothing about this wakeup.
    """
    fired: list[float] = []
    real = coordinator_module.async_call_later

    def arming(hass: HomeAssistant, delay: float, action: Any) -> Any:
        if "_sync_lost_timer" in getattr(action, "__qualname__", ""):
            inner = action

            @callback
            def action(now: Any) -> None:
                fired.append(delay)
                inner(now)

        return real(hass, delay, action)

    monkeypatch.setattr(coordinator_module, "async_call_later", arming)
    return fired


async def test_the_expiry_wakes_a_recompute_by_itself(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No unrelated event needed: one wakeup at the limit (a one-minute limit
    here, so neither the heartbeat nor anything else is due)."""
    entry, head_a, head_b = await _standoff(hass, hysteresis=0, **{TTL: 1})
    coord = entry.runtime_data
    assert coord.lost_vote_ttl == 60
    fired = _count_lost_wakeups(monkeypatch)
    await _lose(hass, head_a)
    await _recompute(hass, entry)
    await _quiesce(hass, coord)
    assert coord._lost_timer is not None
    assert fired == []
    age_lost_vote(coord, head_a, 60)
    assert _row(hass, entry, 0)["vote_status"] == "stale"  # nothing recomputed
    with _count_refresh_requests(coord) as requests:
        await _fire_due(hass, 61)
    assert len(fired) == 1  # one wakeup ran, not two
    assert 0 < fired[0] <= 60
    assert requests[0] == "request"  # and it asked for the recompute
    assert _row(hass, entry, 0)["vote_status"] == "expired"
    assert _plan(hass, entry).state == MODE_HEAT
    assert _s(hass, head_b) == MODE_HEAT
    assert coord._lost_timer is None  # nothing left to expire


async def test_unload_cancels_the_expiry_wakeup(hass: HomeAssistant) -> None:
    entry, head_a, _head_b = await _standoff(hass, hysteresis=0, **{TTL: 1})
    coord = entry.runtime_data
    await _lose(hass, head_a)
    await _recompute(hass, entry)
    assert coord._lost_timer is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coord._lost_timer is None
    assert coord._lost_retired is True
    coord._sync_lost_timer()  # a late re-sync arms nothing on a retired one
    assert coord._lost_timer is None
    with _count_refresh_requests(coord) as requests:
        await _fire_due(hass, 120)
    assert requests == []


async def test_reload_retires_the_old_wakeup_and_starts_unknown(
    hass: HomeAssistant,
) -> None:
    """Lost-vote memory is in memory only: after a reload (or a restart) the
    head is unknown until it is seen, its limit timed from that start."""
    entry, head_a, _head_b = await _standoff(hass, hysteresis=0)
    old = entry.runtime_data
    await _lose(hass, head_a)
    await _recompute(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "stale"
    before = dt_util.utcnow()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coord = entry.runtime_data
    assert coord is not old
    assert old._lost_timer is None and old._lost_retired is True
    await _recompute(hass, entry)
    row = _row(hass, entry, 0)
    assert row["vote_status"] == "unknown"
    assert _since(row["lost_since"]) >= before
    # Unknown, within the limit: its demand still counts, exactly as on
    # 3.10.0 while Home Assistant boots and heads come up.
    assert _plan(hass, entry).attributes["standoff"] is True
    assert _plan(hass, entry).state == MODE_COOL
    assert coord._lost_timer is not None
    age_lost_vote(coord, head_a, DEFAULT_SECONDS)
    await _recompute(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "expired"
    assert _plan(hass, entry).state == MODE_HEAT
    await _back(hass, head_a)
    await _recompute(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "live"


# --- what is not lost ---------------------------------------------------------


async def test_a_quiet_head_is_not_lost(hass: HomeAssistant, freezer) -> None:
    """Hours with no new report from either head: both stay live, the
    hands-off vote keeps counting, and nothing is armed to expire."""
    entry, _head_a, head_b = await _hands_off_heat(hass)
    coord = entry.runtime_data
    for _ in range(3):
        freezer.tick(timedelta(hours=1))
        expire_dwell(coord)
        await _recompute(hass, entry)
    assert _vote(hass, entry, 1) == (MODE_HEAT, "live", None)
    assert _row(hass, entry, 0)["vote_status"] == "live"
    assert _plan(hass, entry).state == MODE_HEAT
    assert coord._sight[head_b].lost_at is None
    assert coord._lost_timer is None


async def test_a_quiet_enabled_room_keeps_holding(hass: HomeAssistant, freezer) -> None:
    entry, _head_a, _head_b = await _standoff(hass)
    coord = entry.runtime_data
    for _ in range(3):
        freezer.tick(timedelta(hours=1))
        expire_dwell(coord)
        await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["standoff"]) == (MODE_COOL, True)
    assert [row["vote_status"] for row in plan.attributes["zones"]] == ["live", "live"]


async def test_a_head_reading_unknown_is_in_sight(hass: HomeAssistant) -> None:
    """unknown is the head there, without a mode: MXZ can still command it.
    It asks for no direction (as on 3.10.0) and never starts the clock."""
    entry, _head_a, head_b = await _hands_off_heat(hass)
    coord = entry.runtime_data
    hass.states.async_set(head_b, "unknown")
    await hass.async_block_till_done()
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1) == (None, "live", None)
    assert _plan(hass, entry).state == MODE_COOL
    assert coord._sight[head_b].lost_at is None


async def test_an_enabled_room_whose_head_reads_unknown_keeps_holding(
    hass: HomeAssistant,
) -> None:
    entry, head_a, _head_b = await _standoff(hass)
    coord = entry.runtime_data
    hass.states.async_set(head_a, "unknown")
    await hass.async_block_till_done()
    expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "live"
    assert (plan.state, plan.attributes["standoff"]) == (MODE_COOL, True)
    assert coord._sight[head_a].lost_at is None


# unknown and unavailable both read as unavailable to the rest of the head
# listener, so these move between them with no other recompute: only the
# refresh the head change itself asks for (and the wakeup) may update the plan.


async def test_unknown_then_unavailable_recomputes_and_arms_the_wakeup(
    hass: HomeAssistant,
) -> None:
    """heat -> unknown -> unavailable: going out of sight from unknown starts
    the clock, publishes it, and arms the expiry wakeup at once."""
    entry, head_a, head_b = await _standoff(hass, hysteresis=0, **{TTL: 1})
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    # Only the state moves: the same attributes throughout, so nothing but
    # the state change itself can ask for a refresh. The head takes no
    # commands from here on, as a real one that drops off.
    attrs = dict(hass.states.get(head_a).attributes)
    _head(hass, head_a)._attr_available = False
    hass.states.async_set(head_a, "unknown", attrs)
    await hass.async_block_till_done()
    await _fire_due(hass, 1)
    assert _row(hass, entry, 0)["vote_status"] == "live"
    assert coord._lost_timer is None

    before = dt_util.utcnow()
    hass.states.async_set(head_a, "unavailable", attrs)
    await hass.async_block_till_done()
    await _fire_due(hass, 1)
    row = _row(hass, entry, 0)
    assert row["vote_status"] == "stale"
    assert before <= _since(row["lost_since"]) <= dt_util.utcnow()
    assert coord._sight[head_a].lost_at is not None
    assert coord._lost_timer is not None
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["standoff"]) == (MODE_COOL, True)

    # The wakeup it armed ends the hold at the limit, by itself.
    age_lost_vote(coord, head_a, 60)
    await _fire_due(hass, 61)
    assert _row(hass, entry, 0)["vote_status"] == "expired"
    assert _plan(hass, entry).state == MODE_HEAT
    assert _s(hass, head_b) == MODE_HEAT
    assert coord._lost_timer is None


async def test_a_hands_off_head_unavailable_then_unknown_drops_its_stale_vote(
    hass: HomeAssistant,
) -> None:
    """unavailable -> unknown: the head is back in sight with no mode, so its
    stale heat ends at once (vote none, as on 3.10.0) and the wakeup goes."""
    entry, _head_a, head_b = await _hands_off_heat(hass)
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    hass.states.async_set(head_b, "unavailable")
    await hass.async_block_till_done()
    await _fire_due(hass, 1)
    vote, status, since = _vote(hass, entry, 1)
    assert (vote, status) == (MODE_HEAT, "stale")
    assert since is not None
    assert coord._lost_timer is not None
    assert _plan(hass, entry).state == MODE_HEAT

    hass.states.async_set(head_b, "unknown")
    await hass.async_block_till_done()
    await _fire_due(hass, 1)
    assert _vote(hass, entry, 1) == (None, "live", None)
    assert coord._sight[head_b].lost_at is None
    assert coord._lost_timer is None
    assert _plan(hass, entry).state == MODE_COOL


async def test_a_head_never_seen_since_start_holds_no_one(hass: HomeAssistant) -> None:
    """A released hands-off head that is unavailable when MXZ starts has no
    last vote: it holds no one, whatever it was doing before (as on 3.10.0)."""
    entry, _head_a, head_b = await _hands_off_heat(hass)
    await _lose(hass, head_b)
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1)[:2] == (MODE_HEAT, "stale")
    before = dt_util.utcnow()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coord = entry.runtime_data
    expire_dwell(coord)
    await _recompute(hass, entry)
    vote, status, since = _vote(hass, entry, 1)
    assert (vote, status) == (None, "unknown")
    assert _since(since) >= before
    assert _row(hass, entry, 1)["hands_off"] == "released"  # restored as before
    assert _plan(hass, entry).state == MODE_COOL
    await _back(hass, head_b)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1) == (MODE_HEAT, "live", None)
    assert _plan(hass, entry).state == MODE_HEAT


# --- kept as on 3.10.0: safety, floor and ceiling -----------------------------


async def test_a_safety_priority_room_keeps_holding_after_expiry(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(SAFETY_A, "on")
    entry, head_a, _head_b = await _standoff(hass, safety=(SAFETY_A, None))
    coord = entry.runtime_data
    await _lose(hass, head_a)
    await _recompute(hass, entry)
    age_lost_vote(coord, head_a, 3 * 3600)
    expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "expired"
    assert (plan.state, plan.attributes["mode_by_safety"]) == (MODE_COOL, True)
    assert plan.attributes["standoff"] is True

    # The control: with its safety sensor off, the same room lets go.
    hass.states.async_set(SAFETY_A, "off")
    await hass.async_block_till_done()
    expire_dwell(coord)
    await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["standoff"]) == (MODE_HEAT, False)


async def test_a_hands_off_safety_vote_is_stale_then_expires(hass: HomeAssistant) -> None:
    """A Safety priority hands-off vote is the head's own vote: kept while
    stale, and at expiry it drops, which is what 3.10.0 did at once."""
    hass.states.async_set(SAFETY_B, "on")
    entry, _head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        hysteresis=None, safety=(None, SAFETY_B),
    )
    coord = entry.runtime_data
    expire_dwell(coord)
    await _set_temp(hass, SENSOR_A, 76)  # Primary calls cool
    await _remote(hass, head_b, "heat")
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_HEAT  # the safety vote outranks it
    await _lose(hass, head_b)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1)[:2] == (MODE_HEAT, "stale")
    assert _plan(hass, entry).state == MODE_HEAT
    age_lost_vote(coord, head_b, DEFAULT_SECONDS)
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _vote(hass, entry, 1)[:2] == (None, "expired")
    assert _plan(hass, entry).state == MODE_COOL


@pytest.mark.parametrize(
    ("guest", "want"), [(45, MODE_HEAT), (95, MODE_COOL)], ids=["floor", "ceiling"]
)
async def test_a_floor_or_ceiling_is_untouched_by_another_rooms_expiry(
    hass: HomeAssistant, guest: float, want: str
) -> None:
    """The enabled primary's head is lost and expires; the disabled guest
    room's tripped floor (or ceiling) runs exactly as before. (A floor run on
    a head that itself goes unavailable already ends at once on 3.10.0: a
    floor never acts blind. That is unchanged and has its own tests.)"""
    f = await _floor_setup(hass, temps=(76 if want == MODE_HEAT else 64, guest))
    coord = f.entry.runtime_data
    expire_dwell(coord)
    await f.compute()
    assert f.zone(1)["floor_want"] == want
    assert f.plan.state == want
    await _lose(hass, f.a)
    expire_dwell(coord)
    await f.compute()
    stale = (f.plan.state, f.zone(1)["floor_want"], f.plan.attributes["mode_by_floor"])
    age_lost_vote(coord, f.a, DEFAULT_SECONDS)
    expire_dwell(coord)
    await f.compute()
    assert f.zone(0)["vote_status"] == "expired"
    assert (f.plan.state, f.zone(1)["floor_want"], f.plan.attributes["mode_by_floor"]) == stale
    assert stale == (want, want, True)


# --- the plan rows ------------------------------------------------------------


async def test_every_row_publishes_vote_status_and_lost_since(hass: HomeAssistant) -> None:
    entry, head_a, _head_b = await _standoff(hass)
    for row in _plan(hass, entry).attributes["zones"]:
        assert (row["vote_status"], row["lost_since"]) == ("live", None)
    await _lose(hass, head_a)
    await _recompute(hass, entry)
    rows = _plan(hass, entry).attributes["zones"]
    assert rows[0]["vote_status"] == "stale"
    assert isinstance(rows[0]["lost_since"], str)
    _since(rows[0]["lost_since"])
    assert (rows[1]["vote_status"], rows[1]["lost_since"]) == ("live", None)
    # The rows the plan sensor publishes are the coordinator's room details.
    coord = entry.runtime_data
    assert coord.room_details()[0]["vote_status"] == "stale"


# --- the setting: Comfort › Advanced › Keep a lost head's vote for ----------------


@pytest.fixture
def house(hass: HomeAssistant):
    def build() -> None:
        hass.config.units = US_CUSTOMARY_SYSTEM
        for head in HEADS:
            _flow_head(hass, head)
        for sensor in SENSORS:
            hass.states.async_set(
                sensor, "70", {"device_class": "temperature", "unit_of_measurement": "°F"}
            )

    return build


def _stores(entry) -> tuple[dict[str, Any], dict[str, Any]]:
    return dict(entry.data), dict(entry.options)


async def test_the_default_is_thirty_minutes(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup(hass)
    assert TTL not in entry.data and TTL not in entry.options
    assert entry.runtime_data.lost_vote_ttl == DEFAULT_SECONDS


@pytest.mark.parametrize(
    ("stored", "seconds"),
    [(45, 2700), (1, 60), (240, 14400), (0, 60), (1000, 14400), ("junk", 1800), (float("nan"), 1800)],
)
async def test_a_stored_value_is_clamped_on_load(
    hass: HomeAssistant, stored: Any, seconds: float
) -> None:
    entry, _a, _b = await _setup(hass, **{TTL: stored})
    assert entry.runtime_data.lost_vote_ttl == seconds


async def test_comfort_advanced_shows_it_blank_in_minutes(hass: HomeAssistant, house) -> None:
    house()
    entry = _entry(hass)
    result = await open_options_page(hass, entry, "comfort")
    advanced = sections(result)["advanced"]
    assert advanced.options["collapsed"] is True
    assert TTL in [marker.schema for marker in advanced.schema.schema]
    assert TTL not in [marker.schema for marker in result["data_schema"].schema]
    assert default(result, TTL) is None
    assert suggested(result, TTL) is None
    assert TTL not in prefilled(result)
    _, box = marker_of(result, TTL)
    assert (box.config["min"], box.config["max"]) == (1, 240)
    assert box.config["unit_of_measurement"] == "min"


async def test_an_untouched_options_pass_changes_no_stored_option(
    hass: HomeAssistant, house
) -> None:
    house()
    entry = _entry(hass)
    before = _stores(entry)
    result = await open_options_page(hass, entry, "comfort")
    result = await submit(hass.config_entries.options, result, prefilled(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data, options = _stores(entry)
    assert TTL not in data and TTL not in options
    for key, value in before[1].items():
        assert options[key] == value, key
    for key, value in before[0].items():
        assert data[key] == value, key


async def test_a_custom_value_is_stored_and_used(hass: HomeAssistant, house) -> None:
    house()
    entry = _entry(hass)
    result = await open_options_page(hass, entry, "comfort")
    result = await submit(hass.config_entries.options, result, {**prefilled(result), TTL: 45})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data, options = _stores(entry)
    assert options[TTL] == data[TTL] == 45
    # Shown again as the stored value.
    result = await open_options_page(hass, entry, "comfort")
    assert suggested(result, TTL) == 45
    assert prefilled(result)[TTL] == 45


async def test_the_custom_value_is_the_limit_the_coordinator_uses(
    hass: HomeAssistant,
) -> None:
    entry, head_a, _head_b = await _standoff(hass, **{TTL: 45})
    coord = entry.runtime_data
    await _lose(hass, head_a)
    await _recompute(hass, entry)
    age_lost_vote(coord, head_a, DEFAULT_SECONDS + 60)  # past the default
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "stale"
    age_lost_vote(coord, head_a, 14 * 60)  # 45 minutes in all
    expire_dwell(coord)
    await _recompute(hass, entry)
    assert _row(hass, entry, 0)["vote_status"] == "expired"


async def test_submitting_blank_clears_a_saved_value(hass: HomeAssistant, house) -> None:
    house()
    entry = _entry(hass, options={TTL: 90})
    before = _stores(entry)
    result = await open_options_page(hass, entry, "comfort")
    shown = prefilled(result)
    assert shown[TTL] == 90
    del shown[TTL]  # the user empties the box
    result = await submit(hass.config_entries.options, result, shown)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data, options = _stores(entry)
    assert TTL not in data and TTL not in options
    for key, value in before[1].items():
        if key != TTL:
            assert options[key] == value, key


async def test_saving_another_page_keeps_a_saved_value(hass: HomeAssistant, house) -> None:
    house()
    entry = _entry(hass, options={TTL: 90})
    result = await open_options_page(hass, entry, "fan_idle")
    result = await submit(hass.config_entries.options, result, prefilled(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data, options = _stores(entry)
    assert options[TTL] == data[TTL] == 90


@pytest.mark.parametrize("value", [0, 0.5, 241, -5, float("inf")])
async def test_out_of_bounds_is_refused(hass: HomeAssistant, house, value: float) -> None:
    house()
    entry = _entry(hass, options={TTL: 90})
    before = _stores(entry)
    result = await open_options_page(hass, entry, "comfort")
    with pytest.raises(vol.Invalid):
        await submit(hass.config_entries.options, result, {**prefilled(result), TTL: value})
    assert _stores(entry) == before


@pytest.mark.parametrize("value", [1, 240])
async def test_the_bounds_themselves_are_accepted(
    hass: HomeAssistant, house, value: int
) -> None:
    house()
    entry = _entry(hass)
    result = await open_options_page(hass, entry, "comfort")
    result = await submit(hass.config_entries.options, result, {**prefilled(result), TTL: value})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[TTL] == value


async def test_nan_is_refused_on_the_advanced_section(hass: HomeAssistant, house) -> None:
    house()
    entry = _entry(hass)
    before = _stores(entry)
    result = await open_options_page(hass, entry, "comfort")
    result = await submit(
        hass.config_entries.options, result, {**prefilled(result), TTL: float("nan")}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"advanced": "not_a_number"}
    assert _stores(entry) == before


async def test_menu_attention_names_a_bad_stored_value(hass: HomeAssistant, house) -> None:
    house()
    entry = _entry(hass, options={TTL: "soon"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"]["attention"] == (
        "**Needs attention:** the saved \"Keep a lost head's vote for\" isn't a"
        " number. Open Comfort to fix it."
    )


async def test_setup_stores_it_only_when_entered(hass: HomeAssistant, house) -> None:
    house()
    review = await _setup_review(hass)
    result = await press(hass.config_entries.flow, review, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert TTL not in result["options"] and TTL not in result["data"]
    await hass.config_entries.async_remove(result["result"].entry_id)

    review = await _setup_review(hass)
    result = await setup_tuning(hass, review, {TTL: 20})
    assert result["step_id"] == "review", result
    result = await press(hass.config_entries.flow, result, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][TTL] == 20


def test_the_label_and_help_are_plain_and_mirrored() -> None:
    base = Path(component.__file__).parent
    strings = json.loads((base / "strings.json").read_text("utf-8"))
    english = json.loads((base / "translations" / "en.json").read_text("utf-8"))
    for flow in ("config", "options"):
        advanced = strings[flow]["step"]["comfort"]["sections"]["advanced"]
        assert advanced["data"][TTL] == "Keep a lost head's vote for"
        help_text = advanced["data_description"][TTL]
        assert "Leave blank for 30 minutes." in help_text
        # What expiry keeps, as built: only an enabled room's Safety priority
        # (a remote-run room's safety vote still expires), and never a floor
        # or ceiling. The old sentence claimed both kinds kept their call.
        assert "An enabled room with Safety priority on keeps its call." in help_text
        assert "A room run from its own remote still lets go, Safety priority or not." in help_text
        assert "A freeze floor or overheat ceiling is never dropped by this." in help_text
        assert "keep their call" not in help_text
        assert advanced == english[flow]["step"]["comfort"]["sections"]["advanced"]
