"""A hands-off room's vote by action: it counts only while its head runs.

Per room, off by default (``hands_off_vote_basis``: ``mode``, stored as no
key, or ``action``). With ``action`` a released hands-off head still votes the
heat or cool it is set to, but only while its own ``hvac_action`` says it is
running that way (``heating`` in heat, ``cooling`` in cool), and for a
10-minute hold after it stops. Reading votes from what heads are observed
doing, rather than from what they were last told, is @csingley's idea
(ha-climate-zone-sequencer f911e4a, "derive zone votes from observations, not
event recency"; MIT, see NOTICE); the action filter is MXZ's extension.

Covered here, against fake heads that report ``hvac_action`` as real cn105
heads do (off, idle, heating, cooling, fan):

* ``mode`` (and no key, and any other stored value) is today's vote exactly;
* running votes, idle holds, the hold ends at 10 minutes (half-open), and a
  wakeup ends it with nothing else happening;
* the head's action lags its mode on real hardware (off + heating,
  fan_only + cooling, cool + heating): a contradiction never creates,
  extends, or refreshes a vote or a hold;
* the other lag (hw2, 2026-10-28): running reported in the new mode just
  after a mode change, carried over from the run before, is unconfirmed for
  a 15 s settle window: no vote, no hold, and it counts at the window's end
  (one wakeup, at once) only if the head is still running then;
* a head never seen reporting hvac_action votes by its mode, and says so;
  one that has reported it gets no mode vote from a report without it; MXZ's
  own thermostat's derived action never counts;
* unavailable blips keep 3.11.0's lost-head rule (the last vote, stale,
  until the time limit);
* a restart restores no hold: the first sighting decides;
* Safety priority votes its mode while idle; an enabled room's demand still
  wins; MXZ never writes to the head;
* the room page's selector, stored only when ``action``.

Fake heads and sensors only. Clocks are aged on the coordinator's own
monotonic stamps, as in ``tests/clocks.py``.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import pytest
import voluptuous as vol

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import HVACAction
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.mxz_coordinator.const import (
    CONF_RESTING_MODE_BIAS,
    CONF_ZONES,
    MODE_COOL,
    MODE_HEAT,
)
from custom_components.mxz_coordinator.logic import HeadSight
from tests.clocks import age_lost_vote
from tests.flow_pages import default, marker_of, open_options_page, prefilled, submit
from tests.test_drive import SENSOR_A, SENSOR_B, _eid, _recompute, _set_temp
from tests.test_dwell_wakeup import _count_refresh_requests, _fire_due, _quiesce
from tests.test_hands_off import _plan, _remote, _s, _setup
from tests.test_startup_continuity import _head, _shutdown, _startup, _switch

# Stored key, values and hold, written out independently of const.py.
BASIS = "hands_off_vote_basis"
HOLD = 600
SETTLE = 15
TTL = 30 * 60
SAFETY_B = "binary_sensor.secondary_safety"


# --- helpers ------------------------------------------------------------------


def _row(hass: HomeAssistant, entry, room: int) -> dict[str, Any]:
    return _plan(hass, entry).attributes["zones"][room]


async def _report(hass: HomeAssistant, head: str, mode: str, action: str | None) -> None:
    """The head reports a mode and its own hvac_action (None: it reports none)."""
    entity = _head(hass, head)
    entity._attr_hvac_mode = mode
    entity._attr_hvac_action = action
    entity.async_write_ha_state()
    await hass.async_block_till_done()
    state = hass.states.get(head)
    assert state.state == mode
    assert state.attributes.get("hvac_action") == action


async def _lose(hass: HomeAssistant, head: str) -> None:
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


def _age_hold(coord: Any, head: str, seconds: float) -> None:
    """Make the moment MXZ first saw ``head`` stopped ``seconds`` older."""
    sight = coord._sight[head]
    assert sight.stopped_at is not None, f"{head} is not in a hold"
    sight.stopped_at -= seconds


def _settled(coord: Any, head: str) -> None:
    """Make ``head``'s last mode change a settle window older. A real run
    starts 10 s or more after a mode change, so a run reported from here on
    counts as soon as it is seen."""
    sight = coord._sight[head]
    assert sight.changed_at is not None, f"{head} has not changed mode"
    sight.changed_at -= SETTLE


def _climate_calls(hass: HomeAssistant, head: str) -> list[str]:
    """Every climate service aimed at ``head`` from here on."""
    seen: list[str] = []

    @callback
    def listen(event: Any) -> None:
        data = event.data
        if data.get("domain") != "climate":
            return
        ids = data.get("service_data", {}).get("entity_id")
        ids = [ids] if isinstance(ids, str) else list(ids or [])
        if head in ids:
            seen.append(data["service"])

    hass.bus.async_listen("call_service", listen)
    return seen


async def _released(hass: HomeAssistant, basis: str | None = "action", **extra: Any):
    """Primary enabled at target (asks nothing); Secondary disabled, hands-off,
    parked and released, with ``basis`` stored (None: no key). Resting bias
    cool, so a heat plan can only come from Secondary's vote."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        vote_basis=(None, basis), **{CONF_RESTING_MODE_BIAS: "cool"}, **extra,
    )
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    assert _row(hass, entry, 1)["hands_off"] == "released"
    return entry, head_a, head_b


async def _vote_after(hass: HomeAssistant, entry, head: str, mode: str, action: str | None):
    await _report(hass, head, mode, action)
    await _recompute(hass, entry)
    return _row(hass, entry, 1)["hands_off_vote"]


async def _run_after(hass: HomeAssistant, entry, head: str, mode: str, action: str):
    """The head reports running and still is when the settle window ends."""
    await _report(hass, head, mode, action)
    _settled(entry.runtime_data, head)
    await _recompute(hass, entry)
    return _row(hass, entry, 1)["hands_off_vote"]


# --- the record itself (logic.HeadSight) --------------------------------------


def test_sight_running_votes_then_holds_then_drops_half_open() -> None:
    sight = HeadSight(0.0, 0.0)
    sight.observe("heat", "idle", 10.0)
    assert sight.vote(10.0, TTL) == "heat"  # by mode: as always
    assert sight.action_vote(10.0, TTL, HOLD) is None  # never seen running
    sight.observe("heat", "heating", 20.0)
    assert sight.action_vote(20.0, TTL, HOLD) == "heat"
    assert sight.hold_ends_in(20.0, HOLD) is None  # running: no hold
    sight.observe("heat", "idle", 100.0)  # stopped: the hold starts here
    sight.observe("heat", "idle", 200.0)  # still idle: the hold doesn't move
    assert sight.stopped_at == 100.0
    assert sight.action_vote(699.999, TTL, HOLD) == "heat"
    assert sight.hold_ends_in(400.0, HOLD) == 300.0
    assert sight.action_vote(700.0, TTL, HOLD) is None  # over AT the hold
    assert sight.hold_ends_in(700.0, HOLD) is None
    sight.observe("heat", "heating", 800.0)  # runs again: votes again
    assert sight.action_vote(800.0, TTL, HOLD) == "heat"
    assert sight.stopped_at is None


@pytest.mark.parametrize(
    ("mode", "action"),
    [
        ("off", "heating"),
        ("off", "cooling"),
        ("fan_only", "cooling"),
        ("fan_only", "heating"),
        ("cool", "heating"),
        ("heat", "cooling"),
        ("dry", "cooling"),
        ("heat_cool", "heating"),
        ("auto", "cooling"),
        ("heat", "fan"),
        ("cool", "off"),
        ("heat", None),
        # No action at all is never a run either, in any mode (a missing
        # action once matched a mode with no running action: off, fan_only).
        ("off", None),
        ("fan_only", None),
        ("dry", None),
        ("unknown", None),
        ("heat", 5),
        ("cool", ["cooling"]),
    ],
)
def test_sight_a_contradiction_never_makes_a_vote(mode: str, action: Any) -> None:
    """The action lags a mode change on real heads: none of these is a run."""
    sight = HeadSight(0.0, 0.0)
    sight.observe(mode, action, 10.0)
    assert sight.action_vote(10.0, TTL, HOLD) is None
    assert (sight.ran, sight.stopped_at) == (None, None)
    assert sight.hold_ends_in(10.0, HOLD) is None


def test_sight_a_contradiction_neither_extends_nor_refreshes_a_hold() -> None:
    sight = HeadSight(0.0, 0.0)
    sight.observe("heat", "heating", 0.0)
    sight.observe("heat", "idle", 100.0)
    sight.observe("heat", "cooling", 400.0)  # heat with cooling: not a run
    assert sight.stopped_at == 100.0  # not refreshed
    assert sight.action_vote(699.0, TTL, HOLD) == "heat"
    assert sight.action_vote(700.0, TTL, HOLD) is None  # not extended
    # A run that ends in a contradicting sighting starts its hold there.
    sight.observe("cool", "cooling", 1000.0)
    sight.observe("cool", "heating", 1100.0)
    assert (sight.ran, sight.stopped_at) == ("cool", 1100.0)
    assert sight.action_vote(1699.0, TTL, HOLD) == "cool"
    assert sight.action_vote(1700.0, TTL, HOLD) is None


@pytest.mark.parametrize(
    ("mode", "action"),
    [("off", "heating"), ("fan_only", "heating"), ("cool", "heating"), ("off", "off")],
)
def test_sight_leaving_the_mode_ends_the_run_and_its_hold(mode: str, action: str) -> None:
    """Switched away (the action still lagging, or not): the run is over, and
    coming back to heat later is not a held vote."""
    sight = HeadSight(0.0, 0.0)
    sight.observe("heat", "heating", 0.0)
    sight.observe(mode, action, 10.0)
    assert sight.action_vote(10.0, TTL, HOLD) is None
    assert (sight.ran, sight.stopped_at) == (None, None)
    sight.observe("heat", "idle", 20.0)
    assert sight.action_vote(20.0, TTL, HOLD) is None


def test_sight_lost_keeps_the_vote_it_had_as_last_seen() -> None:
    """3.11.0's lost-head rule, with the action vote: whatever counted when
    the head was lost (running or held) stays, stale, until the time limit."""
    running = HeadSight(0.0, 0.0)
    running.observe("cool", "cooling", 0.0)
    running.lose(50.0, 50.0)
    assert running.action_vote(50.0 + TTL - 1, TTL, HOLD) == "cool"
    assert running.action_vote(50.0 + TTL, TTL, HOLD) is None  # expired

    held = HeadSight(0.0, 0.0)
    held.observe("heat", "heating", 0.0)
    held.observe("heat", "idle", 100.0)
    held.lose(400.0, 400.0)  # 300 s into the hold
    assert held.hold_ends_in(400.0, HOLD) is None  # timed by the limit now
    assert held.action_vote(400.0 + HOLD, TTL, HOLD) == "heat"  # hold "over", still kept
    assert held.action_vote(400.0 + TTL - 1, TTL, HOLD) == "heat"
    assert held.action_vote(400.0 + TTL, TTL, HOLD) is None

    lapsed = HeadSight(0.0, 0.0)
    lapsed.observe("heat", "heating", 0.0)
    lapsed.observe("heat", "idle", 100.0)
    lapsed.lose(100.0 + HOLD, 0.0)  # lost just as its hold ended
    assert lapsed.action_vote(100.0 + HOLD + 1, TTL, HOLD) is None

    idle = HeadSight(0.0, 0.0)
    idle.observe("heat", "idle", 0.0)
    idle.lose(10.0, 10.0)
    assert idle.vote(20.0, TTL) == "heat"  # by mode it would be kept
    assert idle.action_vote(20.0, TTL, HOLD) is None  # by action it had none


def test_sight_never_seen_has_no_action_vote() -> None:
    sight = HeadSight(100.0, 0.0)
    assert sight.action_vote(110.0, TTL, HOLD) is None
    assert sight.hold_ends_in(110.0, HOLD) is None


def test_sight_remembers_a_head_that_reports_an_action() -> None:
    """Once seen reporting an action, a report without one is no run (and a
    run it ended starts its hold); the head is still one that reports."""
    sight = HeadSight(0.0, 0.0)
    sight.observe("heat", None, 0.0)
    assert sight.reports_action is False
    sight.observe("heat", "heating", 10.0)
    assert sight.reports_action is True
    sight.observe("heat", None, 20.0)
    assert (sight.action, sight.reports_action) == (None, True)
    assert (sight.ran, sight.stopped_at) == ("heat", 20.0)
    sight.lose(30.0, 30.0)
    sight.observe("heat", None, 40.0)
    assert sight.reports_action is True
    assert HeadSight(0.0, 0.0).reports_action is False  # a new start forgets


def test_sight_mode_only_observe_is_unchanged() -> None:
    """Called with the state alone, as 3.11.0 did: the mode vote is untouched
    and no run is recorded."""
    sight = HeadSight(0.0, 0.0)
    sight.observe("heat")
    assert sight.vote(1.0, TTL) == "heat"
    assert (sight.action, sight.ran, sight.stopped_at) == (None, None, None)


def test_sight_observe_without_now_leaves_no_stale_hold() -> None:
    """With no time, a sighting still ends a run it contradicts. A stop it
    can't time is no hold (so no vote), never a run that goes on; a hold
    already running keeps its own start; a run needs no time to begin."""
    left = HeadSight(0.0, 0.0)
    left.observe("heat", "heating", 0.0)
    left.observe("heat", "idle", 100.0)  # holding from 100
    left.observe("off")  # switched off: the run and its hold are over
    assert (left.ran, left.stopped_at) == (None, None)
    left.observe("heat", "idle", 200.0)  # back in heat, idle: nothing held
    assert left.action_vote(200.0, TTL, HOLD) is None
    assert left.hold_ends_in(200.0, HOLD) is None

    stopped = HeadSight(0.0, 0.0)
    stopped.observe("heat", "heating", 0.0)
    stopped.observe("heat", "idle")  # stopped, with no time to hold from
    assert (stopped.ran, stopped.stopped_at) == (None, None)
    assert stopped.action_vote(10.0, TTL, HOLD) is None
    stopped.observe("heat", "idle", 20.0)  # a later timed sighting holds nothing
    assert stopped.action_vote(20.0, TTL, HOLD) is None

    held = HeadSight(0.0, 0.0)
    held.observe("heat", "heating", 0.0)
    held.observe("heat", "idle", 100.0)
    held.observe("heat", "idle")  # still stopped: the hold doesn't move
    assert held.stopped_at == 100.0
    assert held.action_vote(699.0, TTL, HOLD) == "heat"
    assert held.action_vote(700.0, TTL, HOLD) is None

    running = HeadSight(0.0, 0.0)
    running.observe("cool", "cooling")
    assert (running.ran, running.stopped_at) == ("cool", None)
    assert running.action_vote(1.0, TTL, HOLD) == "cool"
    running.observe("cool", "heating")  # a contradiction, untimed: no hold
    assert running.action_vote(2.0, TTL, HOLD) is None


# --- mode, the default: today's vote exactly ---------------------------------


@pytest.mark.parametrize("stored", [None, "mode", "Action", "", 1, True])
async def test_mode_basis_votes_while_idle_as_always(hass: HomeAssistant, stored: Any) -> None:
    """No key (every entry before this option), "mode", or anything that is
    not exactly "action": an idle head in heat votes heat, the row gains no
    keys, no hold timer arms, and a head's action change asks for no
    recompute."""
    entry, _a, head_b = await _released(hass, stored)
    coord = entry.runtime_data
    assert coord.zones[1].vote_basis == "mode"
    assert await _vote_after(hass, entry, head_b, "heat", "idle") == MODE_HEAT
    assert _plan(hass, entry).state == MODE_HEAT
    row = _row(hass, entry, 1)
    assert "vote_basis" not in row and "vote_hold_until" not in row
    assert coord._lost_timer is None
    requested: list[str] = []
    original = coord._async_request_ordinary_refresh

    async def counting() -> None:
        requested.append("request")
        await original()

    coord._async_request_ordinary_refresh = counting
    computes = _computes(coord)  # nor an immediate one, past the request path
    await _report(hass, head_b, "heat", "heating")
    await _report(hass, head_b, "heat", "idle")
    assert requested == []
    assert computes == []


async def test_mode_and_action_rows_differ_only_by_the_new_keys(hass: HomeAssistant) -> None:
    """A running head, the same inputs: the same vote and plan either way;
    action adds vote_basis and vote_hold_until to its own row, nothing else."""
    entry, _a, head_b = await _released(hass, None)
    coord = entry.runtime_data
    await _report(hass, head_b, "heat", "heating")
    await _recompute(hass, entry)  # by mode nothing computes on the report
    _settled(coord, head_b)

    async def snapshot(basis: str) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        coord.zones[1].vote_basis = basis
        await _recompute(hass, entry)
        plan = dict(_plan(hass, entry).attributes)
        rows = [dict(row) for row in plan.pop("zones")]
        for row in rows:
            row.pop("sensor_age", None)
        plan.pop("seconds_since_mode_change", None)
        return _plan(hass, entry).state, plan, rows

    mode_state, mode_plan, mode_rows = await snapshot("mode")
    action_state, action_plan, action_rows = await snapshot("action")
    assert mode_state == action_state == MODE_HEAT
    assert mode_plan == action_plan
    assert action_rows[0] == mode_rows[0]
    extra = {k: v for k, v in action_rows[1].items() if k not in mode_rows[1]}
    assert extra == {"vote_basis": "action", "vote_hold_until": None}
    assert {k: v for k, v in action_rows[1].items() if k in mode_rows[1]} == mode_rows[1]
    assert await _vote_after(hass, entry, head_b, "heat", "idle") == MODE_HEAT  # held
    coord.zones[1].vote_basis = "mode"
    await _recompute(hass, entry)
    assert "vote_basis" not in _row(hass, entry, 1)


# --- action: running, the hold, and its end -----------------------------------


async def test_running_votes_idle_holds_then_drops(hass: HomeAssistant) -> None:
    entry, _a, head_b = await _released(hass)
    coord = entry.runtime_data
    calls = _climate_calls(hass, head_b)
    assert await _vote_after(hass, entry, head_b, "heat", "idle") is None
    assert _plan(hass, entry).state == MODE_COOL  # rest: the idle head asks nothing
    assert await _run_after(hass, entry, head_b, "heat", "heating") == MODE_HEAT
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_by_hands_off"]) == (MODE_HEAT, True)
    assert _row(hass, entry, 1)["vote_basis"] == "action"
    assert _row(hass, entry, 1)["vote_hold_until"] is None

    before = dt_util.utcnow()
    assert await _vote_after(hass, entry, head_b, "heat", "idle") == MODE_HEAT
    until = dt_util.parse_datetime(_row(hass, entry, 1)["vote_hold_until"])
    assert abs((until - before).total_seconds() - HOLD) <= 2
    assert _plan(hass, entry).state == MODE_HEAT

    _age_hold(coord, head_b, HOLD - 1)
    await _recompute(hass, entry)
    assert (_row(hass, entry, 1)["hands_off_vote"], _plan(hass, entry).state) == (
        MODE_HEAT, MODE_HEAT,
    )
    _age_hold(coord, head_b, 1)  # the hold is over AT ten minutes
    await _recompute(hass, entry)
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_hold_until"]) == (None, None)
    assert _plan(hass, entry).state == MODE_COOL
    assert _s(hass, head_b) == "heat"  # the person's head is left as it is
    assert calls == []  # MXZ never wrote to it


async def test_cool_runs_and_holds_the_same_way(hass: HomeAssistant) -> None:
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        vote_basis=(None, "action"), **{CONF_RESTING_MODE_BIAS: "heat"},
    )
    await _recompute(hass, entry)
    coord = entry.runtime_data
    assert await _vote_after(hass, entry, head_b, "cool", "idle") is None
    assert _plan(hass, entry).state == MODE_HEAT
    assert await _run_after(hass, entry, head_b, "cool", "cooling") == MODE_COOL
    assert _plan(hass, entry).state == MODE_COOL
    assert await _vote_after(hass, entry, head_b, "cool", "idle") == MODE_COOL
    _age_hold(coord, head_b, HOLD)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off_vote"] is None
    assert _plan(hass, entry).state == MODE_HEAT


async def test_the_hold_end_wakes_a_recompute_by_itself(hass: HomeAssistant) -> None:
    """A stopped head that stays quiet emits nothing more: one wakeup at the
    hold's end recomputes (a one-minute hold here, so nothing else is due)."""
    entry, _a, head_b = await _released(hass)
    coord = entry.runtime_data
    coord.action_vote_hold = 60.0
    await _run_after(hass, entry, head_b, "heat", "heating")
    assert coord._lost_timer is None  # running: nothing to time
    await _vote_after(hass, entry, head_b, "heat", "idle")
    await _quiesce(hass, coord)
    assert coord._lost_timer is not None
    assert _row(hass, entry, 1)["hands_off_vote"] == MODE_HEAT
    # HA's timers run on the faked time, the hold on the monotonic clock:
    # move both a minute on.
    _age_hold(coord, head_b, 60)
    with _count_refresh_requests(coord) as requests:
        await _fire_due(hass, 61)
    assert requests and requests[0] == "request"
    assert _row(hass, entry, 1)["hands_off_vote"] is None
    assert _plan(hass, entry).state == MODE_COOL
    assert coord._lost_timer is None  # nothing left to time


async def test_an_action_change_recomputes_without_any_other_event(
    hass: HomeAssistant,
) -> None:
    """The head starting to run is a new vote now, not at the next sensor report."""
    entry, _a, head_b = await _released(hass)
    await _vote_after(hass, entry, head_b, "heat", "idle")
    await _quiesce(hass, entry.runtime_data)
    assert _plan(hass, entry).state == MODE_COOL
    _settled(entry.runtime_data, head_b)
    await _report(hass, head_b, "heat", "heating")  # no _recompute
    await _fire_due(hass, 0)
    assert _row(hass, entry, 1)["hands_off_vote"] == MODE_HEAT
    assert _plan(hass, entry).state == MODE_HEAT


def _computes(coord: Any) -> list[str]:
    """Every plan computed from here on, whatever asked for it."""
    seen: list[str] = []
    original = coord._compute

    def counting() -> dict[str, Any]:
        seen.append("compute")
        return original()

    coord._compute = counting
    return seen


async def test_an_action_change_recomputes_only_once_released(hass: HomeAssistant) -> None:
    """Parking (the coordinator is off, so the off can't be sent): the head's
    mode is ours or stale and votes nothing, so its action changing asks for
    no recompute. Released, the same changes each recompute."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), vote_basis=(None, "action"),
        **{CONF_RESTING_MODE_BIAS: "cool"},
    )
    coord = entry.runtime_data
    await _remote(hass, head_b, "heat")
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), False)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off"] == "parking"
    await _quiesce(hass, coord)
    computes = _computes(coord)
    for action in ("heating", "idle", "heating"):
        await _report(hass, head_b, "heat", action)
        await _fire_due(hass, 0)
    assert computes == []

    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), True)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "off"
    assert _row(hass, entry, 1)["hands_off"] == "released"
    await _report(hass, head_b, "heat", "idle")  # a person turns it on
    await _quiesce(hass, coord)
    _settled(coord, head_b)
    computes.clear()
    await _report(hass, head_b, "heat", "heating")
    assert computes == ["compute"]  # at once: no time has passed
    assert _row(hass, entry, 1)["hands_off_vote"] == MODE_HEAT
    await _quiesce(hass, coord)
    computes.clear()
    await _report(hass, head_b, "heat", "idle")  # stopped: its hold starts
    await _fire_due(hass, 0)
    assert computes
    assert _row(hass, entry, 1)["vote_hold_until"] is not None


@pytest.mark.parametrize("wait", [0, 11])
async def test_the_off_landing_ends_mxz_s_own_run(hass: HomeAssistant, wait: int) -> None:
    """MXZ ran the head in heat while the room was enabled. Disabled, MXZ
    parks it off, and that off landing is the release: the run MXZ drove
    ends there, with no compute needed to see it. A person then turning the
    head to heat, idle, has started nothing, so it has no vote and no hold,
    at once or after the 10 s request cooldown."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), vote_basis=(None, "action"),
        **{CONF_RESTING_MODE_BIAS: "cool"},
    )
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_B, 66)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"  # MXZ runs it for the enabled room
    await _report(hass, head_b, "heat", "heating")
    _settled(coord, head_b)
    await _recompute(hass, entry)
    assert coord._sight[head_b].ran == MODE_HEAT
    await _quiesce(hass, coord)

    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await hass.async_block_till_done()
    assert _s(hass, head_b) == "off"  # MXZ's off landed
    assert coord._hands_off_phase(coord.zones[1]) == "released"
    assert (coord._sight[head_b].ran, coord._sight[head_b].stopped_at) == (None, None)
    if wait:
        await _fire_due(hass, wait)
    calls = _climate_calls(hass, head_b)
    assert await _vote_after(hass, entry, head_b, "heat", "idle") is None
    row = _row(hass, entry, 1)
    assert (row["hands_off"], row["vote_hold_until"]) == ("released", None)
    assert _plan(hass, entry).state == MODE_COOL
    # A run of their own still counts, and its stop still holds.
    assert await _run_after(hass, entry, head_b, "heat", "heating") == MODE_HEAT
    assert await _vote_after(hass, entry, head_b, "heat", "idle") == MODE_HEAT
    assert _row(hass, entry, 1)["vote_hold_until"] is not None
    assert calls == []  # the person's head is left as it is


@pytest.mark.parametrize(("mode", "action", "bias"), [
    (MODE_HEAT, "heating", MODE_COOL), (MODE_COOL, "cooling", MODE_HEAT),
])
async def test_a_run_start_counts_on_its_own_report(
    hass: HomeAssistant, mode: str, action: str, bias: str
) -> None:
    """On hardware a new vote came up to 4 s late: a person's mode change
    asked for a refresh, and the head's first running report seconds later
    fell inside that refresh's request cooldown. A run starting recomputes
    at once: the vote counts on that same state event, with no time moved,
    and MXZ still sends the head nothing."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        vote_basis=(None, "action"), **{CONF_RESTING_MODE_BIAS: bias},
    )
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off"] == "released"
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    calls = _climate_calls(hass, head_b)
    computes = _computes(coord)
    await _report(hass, head_b, mode, "idle")  # the mode change: computed now
    assert computes == ["compute"]  # so the request cooldown is running
    assert (_row(hass, entry, 1)["hands_off_vote"], _plan(hass, entry).state) == (None, bias)
    _settled(coord, head_b)
    await _report(hass, head_b, mode, action)  # inside that cooldown
    assert computes == ["compute", "compute"]
    assert (_row(hass, entry, 1)["hands_off_vote"], _plan(hass, entry).state) == (mode, mode)
    await _fire_due(hass, 0)
    assert _plan(hass, entry).state == mode
    assert calls == []  # MXZ never wrote to it


async def _running_in_a_cooldown(hass: HomeAssistant):
    """Secondary released and cooling (resting bias heat, so a cool plan can
    only come from its vote), then a room sensor report: its debounced
    refresh computes at once and opens HA's 10 s request cooldown, as room
    reports do every few seconds on hardware. No time moves after that."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), enable=("_primary_enable", "_coordinator_enable"),
        vote_basis=(None, "action"), **{CONF_RESTING_MODE_BIAS: MODE_HEAT},
    )
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off"] == "released"
    coord = entry.runtime_data
    await _report(hass, head_b, MODE_COOL, "idle")
    _settled(coord, head_b)
    await _report(hass, head_b, MODE_COOL, "cooling")
    await _quiesce(hass, coord)
    assert (_row(hass, entry, 1)["hands_off_vote"], _plan(hass, entry).state) == (
        MODE_COOL, MODE_COOL,
    )
    calls = _climate_calls(hass, head_b)
    computes = _computes(coord)
    await _set_temp(hass, SENSOR_B, 70.25)
    assert computes == ["compute"]
    assert coord._debounced_refresh._timer_task is not None  # the cooldown runs
    computes.clear()
    return entry, head_b, calls, computes


@pytest.mark.parametrize(("mode", "action"), [
    ("fan_only", "cooling"),  # cn105's lagging action, a poll after the switch
    ("fan_only", "fan"),
    ("heat", "idle"),  # a mode change: not the mode it ran in
    ("off", "off"),
])
async def test_a_run_ending_drops_the_vote_on_its_own_report(
    hass: HomeAssistant, mode: str, action: str
) -> None:
    """On hardware (7211fba) a cooling head switched to fan_only kept its
    vote ~5 s: the report fell inside a request cooldown and waited it out.
    A released head leaving its run recomputes at once, on that same state
    event, with no time moved; MXZ still sends the head nothing. A report
    that changes neither mode nor action asks for nothing at once."""
    entry, head_b, calls, computes = await _running_in_a_cooldown(hass)
    await _report(hass, head_b, mode, action)
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_hold_until"]) == (None, None)
    assert _plan(hass, entry).state == MODE_HEAT  # back to rest
    assert computes == ["compute"]
    coord = entry.runtime_data
    assert not coord._debounced_refresh._execute_at_end_of_timer
    _head(hass, head_b)._attr_native_current_temperature = 71
    with _count_refresh_requests(coord) as requests:
        await _report(hass, head_b, mode, action)
    assert computes == ["compute"]
    # No compute at once, no request, and none left waiting on a cooldown.
    assert requests == []
    assert not coord._debounced_refresh._execute_at_end_of_timer
    assert calls == []  # MXZ never wrote to it


async def test_a_run_stopping_starts_its_hold_on_its_own_report(hass: HomeAssistant) -> None:
    """On hardware (7211fba) a cooling head going idle published its hold
    ~5 s late, for the same cooldown. It now starts on that report: the
    vote holds, the hold's end is published and its wakeup armed, and MXZ
    still sends the head nothing."""
    entry, head_b, calls, computes = await _running_in_a_cooldown(hass)
    coord = entry.runtime_data
    before = dt_util.utcnow()
    await _report(hass, head_b, MODE_COOL, "idle")
    row = _row(hass, entry, 1)
    assert row["vote_hold_until"] is not None
    assert row["hands_off_vote"] == MODE_COOL
    assert computes == ["compute"]
    until = dt_util.parse_datetime(row["vote_hold_until"])
    assert abs((until - before).total_seconds() - HOLD) <= 2
    assert coord._lost_timer is not None
    assert _plan(hass, entry).state == MODE_COOL  # held
    assert calls == []  # MXZ never wrote to it


# --- the settle window: running reported just after a mode change ---------------


def test_sight_a_run_just_after_a_mode_change_waits_out_the_window() -> None:
    """hw2's head, on the record's own clock: the stale cooling right after
    the mode change is no run and leaves no hold; real cooling later counts
    at once. The window is half-open, like the hold: over AT ``settle``."""
    sight = HeadSight(0.0, 0.0, SETTLE)
    sight.observe("heat", "heating", 0.0)  # first sighting: no change seen yet
    assert sight.action_vote(0.0, TTL, HOLD) == "heat"
    sight.observe("off", "off", 100.0)  # MXZ's own off: a mode change
    assert sight.changed_at == 100.0
    sight.observe("cool", "off", 102.8)  # a remote's cool
    sight.observe("cool", "cooling", 102.9)  # stale, 0.1 s on
    assert sight.action_vote(102.9, TTL, HOLD) is None
    assert (sight.ran, sight.stopped_at) == (None, None)
    assert sight.settles_in(102.9) == pytest.approx(SETTLE - 0.1)
    sight.observe("cool", "idle", 108.2)  # 5.3 s later: no hold
    assert sight.action_vote(108.2, TTL, HOLD) is None
    assert sight.hold_ends_in(108.2, HOLD) is None
    assert sight.settles_in(108.2) is None  # nothing left to confirm
    sight.observe("cool", "cooling", 203.5)  # real cooling, long after
    assert sight.action_vote(203.5, TTL, HOLD) == "cool"
    assert sight.settles_in(203.5) is None

    edge = HeadSight(0.0, 0.0, SETTLE)
    edge.observe("off", "off", 0.0)
    edge.observe("heat", "heating", 10.0)
    edge.observe("heat", "heating", 10.0 + SETTLE - 0.001)  # not yet
    assert edge.action_vote(10.0 + SETTLE - 0.001, TTL, HOLD) is None
    assert edge.settles_in(10.0 + SETTLE - 0.001) == pytest.approx(0.001)
    assert edge.settles_in(10.0 + SETTLE + 5) == 0.0  # past it: a sighting is due
    edge.observe("heat", "heating", 10.0 + SETTLE)  # AT the window's end
    assert edge.action_vote(10.0 + SETTLE, TTL, HOLD) == "heat"
    assert edge.settles_in(10.0 + SETTLE) is None


def test_sight_the_window_never_holds_back_a_same_mode_run() -> None:
    """No mode change, no window: a run that stops and starts again in the
    mode it ran in counts at once, hold or not, however soon."""
    sight = HeadSight(0.0, 0.0, SETTLE)
    sight.observe("off", "off", 0.0)
    sight.observe("cool", "idle", 1.0)
    sight.observe("cool", "cooling", 1.0 + SETTLE)
    assert sight.action_vote(1.0 + SETTLE, TTL, HOLD) == "cool"
    sight.observe("cool", "idle", 20.0)  # its hold starts
    sight.observe("cool", "cooling", 20.1)  # and it runs again at once
    assert (sight.ran, sight.stopped_at) == ("cool", None)
    assert sight.settles_in(20.1) is None
    # Back in sight in the same mode is no mode change either.
    sight.lose(30.0, 30.0)
    sight.observe("cool", "cooling", 31.0)
    assert sight.action_vote(31.0, TTL, HOLD) == "cool"
    assert sight.changed_at == 1.0


def test_sight_an_unconfirmed_run_lost_has_no_vote_and_no_wakeup() -> None:
    sight = HeadSight(0.0, 0.0, SETTLE)
    sight.observe("off", "off", 0.0)
    sight.observe("heat", "heating", 1.0)
    sight.lose(2.0, 2.0)
    assert sight.settles_in(2.0) is None  # out of sight: nothing to confirm
    assert sight.action_vote(3.0, TTL, HOLD) is None
    assert sight.vote(3.0, TTL) == "heat"  # by mode it is kept, as always


async def _hw2(hass: HomeAssistant):
    """hw2 step G up to the release: MXZ runs Secondary's head in heat for
    the enabled room, then the room is disabled and MXZ's off lands. Resting
    bias heat, so a cool plan can only come from Secondary's vote."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), vote_basis=(None, "action"),
        **{CONF_RESTING_MODE_BIAS: MODE_HEAT},
    )
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_B, 66)
    await _recompute(hass, entry)
    assert _s(hass, head_b) == "heat"  # MXZ runs it for the enabled room
    await _report(hass, head_b, "heat", "heating")
    _settled(coord, head_b)
    await _recompute(hass, entry)
    assert coord._sight[head_b].ran == MODE_HEAT
    await _quiesce(hass, coord)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await hass.async_block_till_done()
    assert _s(hass, head_b) == "off"  # MXZ's off landed: released
    assert coord._hands_off_phase(coord.zones[1]) == "released"
    assert coord._sight[head_b].changed_at is not None  # that off is a change
    await _quiesce(hass, coord)
    return entry, coord, head_b


@pytest.mark.parametrize("cool_off_first", [True, False])
async def test_hw2_stale_cooling_after_mxz_s_heat_run_votes_nothing(
    hass: HomeAssistant, cool_off_first: bool
) -> None:
    """hw2 (2026-10-28 18:30, 45a730d): 3 s after MXZ's off, a remote set
    cool; the head read cooling 0.29 s after the call (stale, from the heat
    run), idle 5.3 s later, and really cooled only 100 s on. MXZ counted a
    cool vote and started a 10-minute hold. Now neither: no vote, no hold,
    the plan stays at rest, until the real cooling counts on its own report.
    MXZ sends the head nothing throughout."""
    entry, coord, head_b = await _hw2(hass)
    calls = _climate_calls(hass, head_b)
    computes = _computes(coord)
    if cool_off_first:
        await _report(hass, head_b, MODE_COOL, "off")
    await _report(hass, head_b, MODE_COOL, "cooling")  # stale, no time moved
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_hold_until"]) == (None, None)
    assert _plan(hass, entry).state == MODE_HEAT
    assert computes == ["compute"] * (2 if cool_off_first else 1)  # seen at once
    assert coord._settle_timer is not None  # the window's end is armed

    coord._sight[head_b].changed_at -= 5.3  # 5.3 s on, still inside the window
    await _report(hass, head_b, MODE_COOL, "idle")
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_hold_until"]) == (None, None)
    assert _plan(hass, entry).state == MODE_HEAT
    assert coord._lost_timer is None  # no hold to end
    assert coord._settle_timer is None  # nothing left to confirm
    computes.clear()
    await _fire_due(hass, SETTLE)  # the old window's end passes quietly
    assert computes == []
    assert _row(hass, entry, 1)["hands_off_vote"] is None

    coord._sight[head_b].changed_at -= 100  # the real cooling, long after
    await _report(hass, head_b, MODE_COOL, "cooling")
    assert computes == ["compute"]  # on its own report, at once
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_hold_until"]) == (MODE_COOL, None)
    assert _plan(hass, entry).state == MODE_COOL
    assert coord._settle_timer is None
    assert calls == []  # MXZ never wrote to it


async def _unconfirmed(hass: HomeAssistant):
    """Released, then the head set to heat reports heating at once: inside
    the window, so no vote yet and the window's end armed."""
    entry, _a, head_b = await _released(hass)
    coord = entry.runtime_data
    await _report(hass, head_b, "heat", "idle")
    await _report(hass, head_b, "heat", "heating")
    assert _row(hass, entry, 1)["hands_off_vote"] is None
    assert _plan(hass, entry).state == MODE_COOL
    assert coord._settle_timer is not None
    await hass.async_block_till_done()
    return entry, coord, head_b


async def test_a_run_still_going_at_the_window_end_counts_from_the_wakeup(
    hass: HomeAssistant,
) -> None:
    """A real run that started inside the window reports nothing new: the
    wakeup at the window's end recomputes at once, not through the request
    debouncer, and the run counts then. MXZ sends the head nothing."""
    entry, coord, head_b = await _unconfirmed(hass)
    calls = _climate_calls(hass, head_b)
    computes = _computes(coord)
    _settled(coord, head_b)  # the monotonic clock, a window on
    with _count_refresh_requests(coord) as requests:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SETTLE))
        await hass.async_block_till_done()
    assert requests == []  # not a debounced request
    assert computes == ["compute"]  # but one compute, from the wakeup itself
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_hold_until"]) == (MODE_HEAT, None)
    assert _plan(hass, entry).state == MODE_HEAT
    assert coord._settle_timer is None  # confirmed: nothing left to time
    assert calls == []


async def test_a_wakeup_short_of_the_window_rearms_for_the_rest(hass: HomeAssistant) -> None:
    """The window is half-open on the monotonic clock: a wakeup whose compute
    still sees it open counts nothing and re-arms for what is left."""
    entry, coord, head_b = await _unconfirmed(hass)
    coord._sight[head_b].changed_at -= SETTLE - 5  # 5 s left
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off_vote"] is None
    assert coord._settle_timer is not None
    assert coord._sight[head_b].settles_in(time.monotonic()) == pytest.approx(5, abs=1)


async def test_a_fired_wakeup_short_of_the_window_rearms_for_the_rest(
    hass: HomeAssistant,
) -> None:
    """The same, driven by HA's own timer: the armed wakeup fires while the
    monotonic window still has 5 s left. Its compute counts nothing and
    re-arms, and the re-armed wakeup counts the run once the window is over.
    Neither goes through the request debouncer; MXZ sends the head nothing."""
    entry, coord, head_b = await _unconfirmed(hass)
    calls = _climate_calls(hass, head_b)
    computes = _computes(coord)
    coord._sight[head_b].changed_at -= SETTLE - 5  # 5 s left
    first = coord._settle_timer
    with _count_refresh_requests(coord) as requests:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SETTLE))
        await hass.async_block_till_done()
        assert computes == ["compute"]  # the wakeup fired and computed
        assert _row(hass, entry, 1)["hands_off_vote"] is None
        assert coord._settle_timer is not None
        assert coord._settle_timer is not first  # re-armed, not the old one
        assert coord._sight[head_b].settles_in(time.monotonic()) == pytest.approx(5, abs=1)

        coord._sight[head_b].changed_at -= 5  # the rest of the window
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SETTLE + 10))
        await hass.async_block_till_done()
    assert requests == []
    assert computes == ["compute", "compute"]
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_hold_until"]) == (MODE_HEAT, None)
    assert _plan(hass, entry).state == MODE_HEAT
    assert coord._settle_timer is None
    assert calls == []


async def test_a_parking_room_arms_no_settle_wakeup(hass: HomeAssistant) -> None:
    """Only a released room's run waits on the window. Parking (the
    coordinator is off, so MXZ's off can't be sent), a head turned to heat
    that reports heating at once is inside a window by its own record, but
    its room has no vote to confirm, so no wakeup is armed and none fires."""
    entry, _a, head_b = await _setup(
        hass, hands_off=(False, True), vote_basis=(None, "action"),
        **{CONF_RESTING_MODE_BIAS: "cool"},
    )
    coord = entry.runtime_data
    await _remote(hass, head_b, "cool")
    await _switch(hass, _eid(hass, entry, "_coordinator_enable"), False)
    await _switch(hass, _eid(hass, entry, "_secondary_enable"), False)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off"] == "parking"
    await _report(hass, head_b, "heat", "idle")  # a mode change
    await _report(hass, head_b, "heat", "heating")  # running at once
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off"] == "parking"
    assert coord._sight[head_b].settles_in(time.monotonic()) is not None
    assert coord._settle_timer is None
    computes = _computes(coord)
    await _fire_due(hass, SETTLE)
    assert computes == []


async def test_a_settle_wakeup_in_flight_at_unload_computes_nothing(
    hass: HomeAssistant,
) -> None:
    """Cancelling cannot recall a wakeup HA has already picked to run. Here
    the coordinator is retired with its wakeup still armed, which is that
    case: the wakeup fires and asks for no recompute."""
    entry, coord, head_b = await _unconfirmed(hass)
    assert coord._settle_timer is not None
    asked: list[str] = []

    async def refresh_now() -> None:
        asked.append("refresh_now")

    coord._async_refresh_now = refresh_now
    coord._retired = True  # as the retire step sets it, before any cancel
    _settled(coord, head_b)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SETTLE))
    await hass.async_block_till_done()
    assert coord._settle_timer is None  # it fired
    assert asked == []


async def test_a_stop_or_a_new_mode_change_cancels_or_moves_the_wakeup(
    hass: HomeAssistant,
) -> None:
    entry, coord, head_b = await _unconfirmed(hass)
    computes = _computes(coord)
    await _report(hass, head_b, "heat", "idle")  # stopped inside the window
    assert computes == ["compute"]  # a stop is still seen at once
    assert coord._settle_timer is None
    assert _row(hass, entry, 1)["vote_hold_until"] is None  # and holds nothing
    computes.clear()
    await _fire_due(hass, SETTLE)
    assert computes == []  # no wakeup left behind

    coord._sight[head_b].changed_at -= 10
    await _report(hass, head_b, "cool", "cooling")  # another mode change
    assert coord._settle_timer is not None
    left = coord._sight[head_b].settles_in(time.monotonic())
    assert left == pytest.approx(SETTLE, abs=1)  # timed from the new change
    assert _row(hass, entry, 1)["hands_off_vote"] is None


async def test_unload_cancels_the_settle_wakeup(hass: HomeAssistant) -> None:
    entry, coord, head_b = await _unconfirmed(hass)
    computes = _computes(coord)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coord._settle_timer is None
    _settled(coord, head_b)
    await _fire_due(hass, SETTLE)
    assert computes == []


async def test_a_same_mode_run_counts_on_its_own_report(hass: HomeAssistant) -> None:
    """The window is only for a run right after a mode change: a head set to
    cool long ago, or one already counted in cool, starts and stops at once
    on each report, with no wakeup."""
    entry, _a, head_b = await _released(hass)
    coord = entry.runtime_data
    await _report(hass, head_b, MODE_COOL, "idle")
    _settled(coord, head_b)
    await _quiesce(hass, coord)
    computes = _computes(coord)
    await _report(hass, head_b, MODE_COOL, "cooling")
    assert computes == ["compute"]
    assert _row(hass, entry, 1)["hands_off_vote"] == MODE_COOL
    assert coord._settle_timer is None
    await _report(hass, head_b, MODE_COOL, "idle")  # its hold starts
    assert _row(hass, entry, 1)["vote_hold_until"] is not None
    await _report(hass, head_b, MODE_COOL, "cooling")  # and runs again: at once
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_hold_until"]) == (MODE_COOL, None)
    assert computes == ["compute"] * 3
    assert coord._settle_timer is None


async def test_safety_priority_never_waits_on_the_window(hass: HomeAssistant) -> None:
    """A safety room votes its mode on the very report of the mode change,
    running or idle; only when the sensor clears does the window apply."""
    hass.states.async_set(SAFETY_B, "on")
    entry, _a, head_b = await _released(hass, safety=(None, SAFETY_B))
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    computes = _computes(coord)
    await _report(hass, head_b, "heat", "heating")
    assert computes == ["compute"]
    plan = _plan(hass, entry)
    assert _row(hass, entry, 1)["hands_off_vote"] == MODE_HEAT
    assert (plan.state, plan.attributes["mode_by_safety"]) == (MODE_HEAT, True)
    hass.states.async_set(SAFETY_B, "off")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off_vote"] is None  # unconfirmed by action
    assert _plan(hass, entry).state == MODE_COOL


@pytest.mark.parametrize("stored", [None, "mode"])
async def test_mode_basis_never_waits_on_the_window(hass: HomeAssistant, stored: Any) -> None:
    """By mode, the head votes its mode on the first compute after the
    change, running or not, and no settle wakeup is ever armed."""
    entry, _a, head_b = await _released(hass, stored)
    coord = entry.runtime_data
    assert await _vote_after(hass, entry, head_b, "heat", "heating") == MODE_HEAT
    assert _plan(hass, entry).state == MODE_HEAT
    assert coord._sight[head_b].settles_in(time.monotonic()) is not None  # seen
    assert coord._settle_timer is None
    assert await _vote_after(hass, entry, head_b, "cool", "cooling") == MODE_COOL
    assert coord._settle_timer is None


# --- the lag on real heads: a contradiction is never a run ---------------------


async def test_lagging_action_never_creates_a_vote(hass: HomeAssistant) -> None:
    """Seen on Drew's cn105 heads: off with heating, fan_only with cooling,
    for a poll after a mode change. None of them votes, and none leaves a
    hold behind for the next sighting."""
    entry, _a, head_b = await _released(hass)
    for mode, action in (
        ("off", "heating"), ("fan_only", "cooling"), ("cool", "heating"),
        ("heat", "cooling"), ("dry", "cooling"), ("off", "cooling"),
    ):
        assert await _vote_after(hass, entry, head_b, mode, action) is None, (mode, action)
        assert _plan(hass, entry).state == MODE_COOL
        assert _row(hass, entry, 1)["vote_hold_until"] is None
        assert entry.runtime_data._lost_timer is None  # no hold to time
    assert await _vote_after(hass, entry, head_b, "heat", "idle") is None


async def test_switching_off_with_a_lagging_action_drops_the_vote(hass: HomeAssistant) -> None:
    """Heating, then a person turns it off: the head still reads heating for a
    poll. The vote goes with the mode; back in heat while idle is no vote."""
    entry, _a, head_b = await _released(hass)
    assert await _run_after(hass, entry, head_b, "heat", "heating") == MODE_HEAT
    assert await _vote_after(hass, entry, head_b, "off", "heating") is None
    assert _plan(hass, entry).state == MODE_COOL
    assert await _vote_after(hass, entry, head_b, "off", "off") is None
    assert await _vote_after(hass, entry, head_b, "heat", "idle") is None
    assert _plan(hass, entry).state == MODE_COOL


async def test_switching_heat_to_cool_with_a_lagging_action(hass: HomeAssistant) -> None:
    """cool + heating votes neither way; cool counts once it is cooling."""
    entry, _a, head_b = await _released(hass)
    assert await _run_after(hass, entry, head_b, "heat", "heating") == MODE_HEAT
    assert await _vote_after(hass, entry, head_b, "cool", "heating") is None
    assert _row(hass, entry, 1)["vote_hold_until"] is None
    assert await _run_after(hass, entry, head_b, "cool", "cooling") == MODE_COOL
    assert _plan(hass, entry).state == MODE_COOL


async def test_a_contradiction_does_not_extend_a_running_hold(hass: HomeAssistant) -> None:
    entry, _a, head_b = await _released(hass)
    coord = entry.runtime_data
    await _run_after(hass, entry, head_b, "heat", "heating")
    await _vote_after(hass, entry, head_b, "heat", "idle")
    stopped = coord._sight[head_b].stopped_at
    until = _row(hass, entry, 1)["vote_hold_until"]
    _age_hold(coord, head_b, HOLD - 60)
    assert await _vote_after(hass, entry, head_b, "heat", "cooling") == MODE_HEAT
    assert coord._sight[head_b].stopped_at == stopped - (HOLD - 60)
    assert _row(hass, entry, 1)["vote_hold_until"] != until  # 9 min nearer, not reset
    _age_hold(coord, head_b, 60)
    assert await _vote_after(hass, entry, head_b, "heat", "cooling") is None


# --- no hvac_action: by mode, and shown -----------------------------------------


async def test_a_head_without_hvac_action_votes_by_mode(hass: HomeAssistant) -> None:
    """The head reports no action at all: its room votes by mode, and its row
    says so. MXZ's own thermostat shows an action it derives from the head's
    mode; that is display, never an observation."""
    entry, _a, head_b = await _released(hass)
    assert await _vote_after(hass, entry, head_b, "heat", None) == MODE_HEAT
    row = _row(hass, entry, 1)
    assert (row["vote_basis"], row["vote_hold_until"]) == ("mode", None)
    assert _plan(hass, entry).state == MODE_HEAT
    tile = hass.states.get(_eid(hass, entry, "_secondary_thermostat"))
    assert tile.attributes["hvac_action"] == HVACAction.HEATING  # derived, shown only
    assert entry.runtime_data._sight[head_b].action is None
    # Once it reports an action, it is judged by it.
    assert await _vote_after(hass, entry, head_b, "heat", "idle") is None
    assert _row(hass, entry, 1)["vote_basis"] == "action"
    assert _plan(hass, entry).state == MODE_COOL


async def test_a_dropped_action_report_is_not_a_mode_vote(hass: HomeAssistant) -> None:
    """A head that has reported an action and then, for a report, has none
    (as across a reconnect) stays judged by action: idle stays no vote, and a
    run it ended only holds, never falls back to its mode."""
    entry, _a, head_b = await _released(hass)
    coord = entry.runtime_data
    assert await _vote_after(hass, entry, head_b, "heat", "idle") is None
    assert await _vote_after(hass, entry, head_b, "heat", None) is None
    row = _row(hass, entry, 1)
    assert (row["vote_basis"], row["vote_hold_until"]) == ("action", None)
    assert _plan(hass, entry).state == MODE_COOL
    assert coord._lost_timer is None

    assert await _run_after(hass, entry, head_b, "heat", "heating") == MODE_HEAT
    assert await _vote_after(hass, entry, head_b, "heat", None) == MODE_HEAT  # held
    assert _row(hass, entry, 1)["vote_hold_until"] is not None
    _age_hold(coord, head_b, HOLD)
    assert await _vote_after(hass, entry, head_b, "heat", None) is None
    assert _row(hass, entry, 1)["vote_basis"] == "action"
    assert _plan(hass, entry).state == MODE_COOL


async def test_a_never_seen_head_shows_its_configured_basis(hass: HomeAssistant) -> None:
    entry, _a, head_b = await _released(hass)
    await _report(hass, head_b, "heat", "idle")
    await _lose(hass, head_b)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    row = _row(hass, entry, 1)
    assert (row["vote_status"], row["vote_basis"], row["hands_off_vote"]) == (
        "unknown", "action", None,
    )


# --- unavailable blips: 3.11.0's lost-head rule ---------------------------------


async def test_a_running_head_lost_keeps_its_vote_stale_then_expires(
    hass: HomeAssistant,
) -> None:
    entry, _a, head_b = await _released(hass)
    coord = entry.runtime_data
    await _run_after(hass, entry, head_b, "heat", "heating")
    await _lose(hass, head_b)
    await _recompute(hass, entry)
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_status"]) == (MODE_HEAT, "stale")
    assert _plan(hass, entry).state == MODE_HEAT
    age_lost_vote(coord, head_b, TTL - 1)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off_vote"] == MODE_HEAT
    age_lost_vote(coord, head_b, 1)
    await _recompute(hass, entry)
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_status"]) == (None, "expired")
    assert _plan(hass, entry).state == MODE_COOL


async def test_a_held_head_lost_keeps_its_vote_past_the_hold(hass: HomeAssistant) -> None:
    """Lost mid-hold: the vote it had is kept, stale, for the lost-vote time
    limit; the hold no longer runs out while nobody can see the head."""
    entry, _a, head_b = await _released(hass)
    coord = entry.runtime_data
    await _run_after(hass, entry, head_b, "heat", "heating")
    await _vote_after(hass, entry, head_b, "heat", "idle")
    _age_hold(coord, head_b, HOLD - 60)
    await _lose(hass, head_b)
    await _recompute(hass, entry)
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_status"], row["vote_hold_until"]) == (
        MODE_HEAT, "stale", None,
    )
    # Time passes for both stamps, long past where the hold would have ended.
    _age_hold(coord, head_b, TTL - 1)
    age_lost_vote(coord, head_b, TTL - 1)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off_vote"] == MODE_HEAT
    _age_hold(coord, head_b, 1)
    age_lost_vote(coord, head_b, 1)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off_vote"] is None


async def test_an_idle_head_lost_has_no_vote_to_keep(hass: HomeAssistant) -> None:
    entry, _a, head_b = await _released(hass)
    await _vote_after(hass, entry, head_b, "heat", "idle")
    await _lose(hass, head_b)
    await _recompute(hass, entry)
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_status"]) == (None, "stale")
    assert _plan(hass, entry).state == MODE_COOL


async def test_lost_between_computes_keeps_what_it_showed_last(hass: HomeAssistant) -> None:
    """The head started heating and dropped off before any compute saw it:
    its last state (from the change itself) is its vote, as on 3.11.0."""
    entry, _a, head_b = await _released(hass)
    await _vote_after(hass, entry, head_b, "heat", "idle")
    _settled(entry.runtime_data, head_b)
    entity = _head(hass, head_b)
    entity._attr_hvac_action = "heating"
    entity.async_write_ha_state()
    entity._attr_available = False
    entity.async_write_ha_state()
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    row = _row(hass, entry, 1)
    assert (row["hands_off_vote"], row["vote_status"]) == (MODE_HEAT, "stale")


@pytest.mark.parametrize(
    ("after", "vote", "holding"),
    [("heating", MODE_HEAT, False), ("idle", MODE_HEAT, True), ("cooling", MODE_HEAT, True)],
)
async def test_an_unavailable_blip_while_running(
    hass: HomeAssistant, after: str, vote: str, holding: bool
) -> None:
    """Running, a blip, back: still running votes on; back idle (or with a
    contradicting action) starts the hold from the return, never a drop."""
    entry, _a, head_b = await _released(hass)
    await _run_after(hass, entry, head_b, "heat", "heating")
    await _lose(hass, head_b)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off_vote"] == MODE_HEAT
    await _back(hass, head_b)
    assert await _vote_after(hass, entry, head_b, "heat", after) == vote
    row = _row(hass, entry, 1)
    assert row["vote_status"] == "live"
    assert (row["vote_hold_until"] is not None) is holding
    assert _plan(hass, entry).state == MODE_HEAT


async def test_an_unavailable_blip_while_idle_makes_no_vote(hass: HomeAssistant) -> None:
    entry, _a, head_b = await _released(hass)
    await _vote_after(hass, entry, head_b, "heat", "idle")
    for _ in range(3):
        await _lose(hass, head_b)
        await _recompute(hass, entry)
        assert _row(hass, entry, 1)["hands_off_vote"] is None
        await _back(hass, head_b)
        await _recompute(hass, entry)
        assert _row(hass, entry, 1)["hands_off_vote"] is None
        assert _plan(hass, entry).state == MODE_COOL


async def test_an_unavailable_blip_does_not_restart_a_hold(hass: HomeAssistant) -> None:
    entry, _a, head_b = await _released(hass)
    coord = entry.runtime_data
    await _run_after(hass, entry, head_b, "heat", "heating")
    await _vote_after(hass, entry, head_b, "heat", "idle")
    _age_hold(coord, head_b, HOLD - 30)
    await _lose(hass, head_b)
    await _recompute(hass, entry)
    await _back(hass, head_b)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off_vote"] == MODE_HEAT
    _age_hold(coord, head_b, 30)
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off_vote"] is None


# --- restart: no hold is restored ----------------------------------------------


@pytest.mark.parametrize(("action", "vote"), [("idle", None), ("heating", MODE_HEAT)])
async def test_restart_restores_no_hold(
    hass: HomeAssistant, action: str, vote: str | None
) -> None:
    """Mid-hold, then a restart: the first sighting decides. An idle head has
    no vote (the hold is not restored); a running one votes at once."""
    entry, _a, head_b = await _released(hass)
    await _remote(hass, head_b, "heat")
    await _run_after(hass, entry, head_b, "heat", "heating")
    await _vote_after(hass, entry, head_b, "heat", "idle")
    assert _row(hass, entry, 1)["hands_off_vote"] == MODE_HEAT  # held
    await _shutdown(hass, entry)
    await _report(hass, head_b, "heat", action)
    await _startup(hass, entry)
    for _ in range(2):
        await _recompute(hass, entry)
    row = _row(hass, entry, 1)
    assert (row["hands_off"], row["hands_off_vote"]) == ("released", vote)
    assert row["vote_hold_until"] is None
    assert _s(hass, head_b) == "heat"  # the restart sent it nothing


# --- tiers: safety, and an enabled room's demand --------------------------------


async def test_safety_priority_votes_its_mode_while_idle(hass: HomeAssistant) -> None:
    hass.states.async_set(SAFETY_B, "on")
    entry, _a, head_b = await _released(hass, safety=(None, SAFETY_B))
    assert await _vote_after(hass, entry, head_b, "heat", "idle") == MODE_HEAT
    plan = _plan(hass, entry)
    assert (plan.state, plan.attributes["mode_by_safety"]) == (MODE_HEAT, True)
    assert _row(hass, entry, 1)["safety_priority"] is True
    # The sensor clears: back to the action filter, and idle has no vote.
    hass.states.async_set(SAFETY_B, "off")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _row(hass, entry, 1)["hands_off_vote"] is None


async def test_safety_priority_beats_an_enabled_demand_while_idle(hass: HomeAssistant) -> None:
    """The safety tier never waits on the filter, even against a calling room."""
    hass.states.async_set(SAFETY_B, "on")
    entry, _a, head_b = await _released(hass, safety=(None, SAFETY_B))
    await _set_temp(hass, SENSOR_A, 76)  # the enabled primary calls cool
    assert await _vote_after(hass, entry, head_b, "heat", "idle") == MODE_HEAT
    plan = _plan(hass, entry)
    assert plan.state == MODE_HEAT
    assert plan.attributes["standoff"] is True


async def test_an_enabled_demand_still_beats_a_running_vote(hass: HomeAssistant) -> None:
    entry, head_a, head_b = await _released(hass)
    await _set_temp(hass, SENSOR_A, 76)  # the enabled primary calls cool
    assert await _run_after(hass, entry, head_b, "heat", "heating") == MODE_HEAT
    plan = _plan(hass, entry)
    assert (plan.state, _s(hass, head_a)) == (MODE_COOL, MODE_COOL)
    assert plan.attributes["standoff"] is True
    assert plan.attributes["mode_by_hands_off"] is False


@pytest.mark.parametrize(("basis", "shared"), [(None, MODE_HEAT), ("action", MODE_COOL)])
async def test_an_idle_heat_head_no_longer_blocks_a_running_cool_one(
    hass: HomeAssistant, basis: str | None, shared: str
) -> None:
    """Two hands-off rooms: Primary's head sits idle in heat, Secondary's is
    cooling. By mode, Primary's heat wins on room order; by action, its idle
    vote is gone and the running cool is served."""
    entry, head_a, head_b = await _setup(
        hass, hands_off=(True, True), enable=("_coordinator_enable",),
        vote_basis=(basis, basis), **{CONF_RESTING_MODE_BIAS: "heat"},
    )
    await _recompute(hass, entry)
    assert (_s(hass, head_a), _s(hass, head_b)) == ("off", "off")
    calls = _climate_calls(hass, head_a) + _climate_calls(hass, head_b)
    await _report(hass, head_a, "heat", "idle")
    await _report(hass, head_b, "cool", "cooling")
    await _recompute(hass, entry)  # by mode nothing computes on the report
    _settled(entry.runtime_data, head_b)
    for _ in range(2):
        await _recompute(hass, entry)
    plan = _plan(hass, entry)
    assert plan.state == shared
    assert plan.attributes["mode_by_hands_off"] is (shared == MODE_COOL)
    assert (_s(hass, head_a), _s(hass, head_b)) == ("heat", "cool")
    assert calls == []


# --- the room page ---------------------------------------------------------------


def _zone(entry, room: int) -> dict[str, Any]:
    return entry.data[CONF_ZONES][room]


async def test_room_page_stores_action_only_and_clears_it(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup(hass, hands_off=(False, True))
    before = dict(_zone(entry, 1))
    assert BASIS not in before
    manager = hass.config_entries.options

    result = await open_options_page(hass, entry, "room_2")
    assert default(result, BASIS) == "mode"
    _marker, selector = marker_of(result, BASIS)
    assert selector.config["options"] == ["mode", "action"]
    assert selector.config["translation_key"] == BASIS
    # Saved untouched: the room's record is exactly what it was.
    result = await submit(manager, result, prefilled(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert _zone(entry, 1) == before
    assert BASIS not in entry.options and BASIS not in entry.data

    result = await open_options_page(hass, entry, "room_2")
    result = await submit(manager, result, {**prefilled(result), BASIS: "action"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert _zone(entry, 1) == {**before, BASIS: "action"}
    assert BASIS not in _zone(entry, 0)
    assert BASIS not in entry.options and BASIS not in entry.data  # never flat
    assert entry.runtime_data.zones[1].vote_basis == "action"
    assert entry.runtime_data.zones[0].vote_basis == "mode"

    result = await open_options_page(hass, entry, "room_2")
    assert default(result, BASIS) == "action"
    result = await submit(manager, result, {**prefilled(result), BASIS: "mode"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert _zone(entry, 1) == before
    assert entry.runtime_data.zones[1].vote_basis == "mode"


async def test_room_page_refuses_an_unknown_basis(hass: HomeAssistant) -> None:
    entry, _a, _b = await _setup(hass, hands_off=(False, True))
    before = dict(_zone(entry, 1))
    result = await open_options_page(hass, entry, "room_2")
    with pytest.raises(vol.Invalid):  # the selector's own refusal
        await submit(
            hass.config_entries.options, result, {**prefilled(result), BASIS: "always"}
        )
    assert _zone(entry, 1) == before


def test_strings_name_the_selector_and_both_options() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "custom_components" / "mxz_coordinator"
    for path in (root / "strings.json", *sorted((root / "translations").glob("*.json"))):
        strings = json.loads(path.read_text())
        room = strings["options"]["step"]["room"]
        assert room["data"][BASIS], path
        assert "10 minutes" in room["data_description"][BASIS], path
        assert set(strings["selector"][BASIS]["options"]) == {"mode", "action"}, path
