"""Mode-dwell expiry wakeup: a flip the dwell deferred is reconsidered when the
dwell runs out, not at the next sensor event or the 15-min heartbeat.

The hysteresis gate is a timestamp comparison, so before this the plan only
re-decided when something else woke it — a 10-min dwell could take 25. The
wakeup is one cancellable ``async_call_later`` (the per-head dry-dwell nudge is
the model) that carries NO decision: it asks for a recompute of the inputs as
they are when it fires, so a manual choice made in between wins.

Time idiom, as in ``test_idle_dry``: firing HA's time machinery does not move
``dt_util.utcnow``, so the dwell clock is aged by rewinding the coordinator's
own stamp and the timer is then fired for real.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mxz_coordinator import coordinator as coordinator_module
from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DEFAULT_HEAT_LOCKOUT_FLOOR,
    DEFAULT_MODE_HYSTERESIS,
    DEMAND_NEUTRAL,
    DOMAIN,
    INHIBIT_ACTION_OFF,
    MODE_COOL,
    MODE_HEAT,
    MODE_OFF,
    STARTUP_RECOVER_DELAY,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from tests.test_drive import (
    INHIBIT,
    SENSOR_A,
    SENSOR_B,
    MockHead,
    MockHeadC,
    _eid,
    _recompute,
    _set_hold,
    _set_target,
    _set_temp,
    _setup_mock_heads,
)
from tests.test_idle_action import (
    _head_calls,
    _record_calls,
    _settle_requested_refreshes,
)

# Room readings that make the primary call heat / cool, per unit system.
COLD = {False: 60.0, True: 15.0}
NEUTRAL = {False: 70.0, True: 21.0}
HOT = {False: 75.0, True: 26.0}
# Heat setpoint edges the flip must produce: (target, target + band).
HEAT_EDGES = {False: (70.0, 72.0), True: (21.0, 22.0)}


async def _setup_dwell(
    hass: HomeAssistant, *, celsius: bool = False, **extra: Any
) -> tuple[MockConfigEntry, str, str]:
    """Two heads on a DEFAULT-dwell entry; coordinator and both rooms enabled.

    Unlike the other fixtures this one does NOT pin ``mode_hysteresis`` to 0 —
    the dwell is the subject. Fan boost is off so the only head writes in a
    measured window are the mode/setpoint ones.
    """
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(
        hass, cls=MockHeadC if celsius else MockHead
    )
    await _set_temp(hass, SENSOR_A, NEUTRAL[celsius])
    await _set_temp(hass, SENSOR_B, NEUTRAL[celsius])
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: False,
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    return entry, head_a, head_b


def _plan(hass: HomeAssistant, entry: MockConfigEntry):
    return hass.states.get(_eid(hass, entry, "_plan"))


async def _quiesce(hass: HomeAssistant, coord: MXZCoordinator) -> None:
    """Drain the setup burst so only the dwell wakeup is left pending.

    Setup arms the post-start recompute (``STARTUP_RECOVER_DELAY``) and the
    enable switches leave debounced refreshes behind. A test that measured a
    dwell window without draining them would be watching its own setup wake the
    coordinator up.
    """
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=STARTUP_RECOVER_DELAY + 1)
    )
    await hass.async_block_till_done()
    await _settle_requested_refreshes(hass, coord)


async def _fire_due(hass: HomeAssistant, seconds: float) -> None:
    """Run every HA timer due within ``seconds``, then the refresh it asked for.

    A fired wakeup requests a refresh through the coordinator's request-refresh
    debouncer, so the recompute itself lands one cooldown later.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(seconds=REQUEST_REFRESH_DEFAULT_COOLDOWN + 1),
    )
    await hass.async_block_till_done()


def _age_out_dwell(coord: MXZCoordinator) -> None:
    """Age the hysteresis clock so the dwell has run out (the suite's rewind)."""
    coord._last_mode_change_ts -= coord.hysteresis


@contextmanager
def _count_refresh_requests(coord: MXZCoordinator):
    """Count refresh REQUESTS — one per wakeup that actually fired.

    Counting refreshes would not do: the coordinator's debouncer coalesces two
    requests into one refresh, which is exactly what a leaked second timer
    would hide behind.
    """
    original = coord.async_request_refresh
    requests: list[str] = []

    async def counting() -> None:
        requests.append("request")
        await original()

    coord.async_request_refresh = counting
    try:
        yield requests
    finally:
        coord.async_request_refresh = original


async def _defer_a_heat_flip(
    hass: HomeAssistant, entry: MockConfigEntry, *, celsius: bool = False
) -> MXZCoordinator:
    """Quiesce, then leave the primary calling heat with the dwell holding it."""
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    await _set_temp(hass, SENSOR_A, COLD[celsius])
    await _recompute(hass, entry)

    plan = _plan(hass, entry)
    assert plan.attributes["primary_demand"] == MODE_HEAT
    assert plan.state == MODE_COOL  # the flip is deferred...
    assert plan.attributes["mode_change_allowed"] is False  # ...by the dwell
    assert coord._dwell_timer is not None  # so a wakeup is armed
    return coord


# --- the defect this node closes -----------------------------------------------


@pytest.mark.parametrize("celsius", [False, True], ids=["fahrenheit", "celsius"])
async def test_dwell_expiry_alone_reconsiders_the_deferred_flip(
    hass: HomeAssistant, celsius: bool
) -> None:
    """Dwell expiry alone flips the mode — no sensor event, no heartbeat.

    Unit-symmetric: the same run in °F and °C, each with its own profile's
    reading, target and setpoint band.
    """
    entry, head_a, _b = await _setup_dwell(hass, celsius=celsius)
    coord = await _defer_a_heat_flip(hass, entry, celsius=celsius)
    assert hass.states.get(head_a).state != MODE_HEAT

    sensor_stamp = hass.states.get(SENSOR_A).last_updated
    _age_out_dwell(coord)
    await _fire_due(hass, coord.hysteresis + 5)

    # Nothing else woke the coordinator: the room sensor never changed, and the
    # 15-min heartbeat is still 5 minutes out.
    assert hass.states.get(SENSOR_A).last_updated == sensor_stamp
    plan = _plan(hass, entry)
    assert plan.state == MODE_HEAT
    assert plan.attributes["mode_change_allowed"] is True
    head = hass.states.get(head_a)
    assert head.state == MODE_HEAT
    low, high = HEAT_EDGES[celsius]
    assert head.attributes["target_temp_low"] == low  # normal clamped edges
    assert head.attributes["target_temp_high"] == high
    assert coord._dwell_timer is None  # nothing left to wait for


async def test_wakeup_lands_at_expiry_and_not_before(hass: HomeAssistant) -> None:
    """The wakeup is scheduled for the dwell's expiry, not some sooner tick."""
    entry, _a, _b = await _setup_dwell(hass)
    coord = await _defer_a_heat_flip(hass, entry)
    assert coord.hysteresis == DEFAULT_MODE_HYSTERESIS == 600

    _age_out_dwell(coord)  # the decision is ready; only the clock is not
    await _fire_due(hass, coord.hysteresis / 2)
    assert _plan(hass, entry).state == MODE_COOL  # wakeup not due yet
    assert coord._dwell_timer is not None

    await _fire_due(hass, coord.hysteresis + 5)
    assert _plan(hass, entry).state == MODE_HEAT


async def test_configured_dwell_option_drives_the_wakeup(hass: HomeAssistant) -> None:
    """A shorter configured dwell wakes up sooner; the default is untouched."""
    entry, _a, _b = await _setup_dwell(hass, **{CONF_MODE_HYSTERESIS: 300})
    coord = await _defer_a_heat_flip(hass, entry)
    assert coord.hysteresis == 300

    _age_out_dwell(coord)
    await _fire_due(hass, 305)
    assert _plan(hass, entry).state == MODE_HEAT


# --- arming, replacement and no-ops --------------------------------------------


async def test_no_wakeup_while_nothing_is_deferred(hass: HomeAssistant) -> None:
    """A settled house arms nothing; the wakeup exists only for a held flip."""
    entry, _a, _b = await _setup_dwell(hass)
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    assert coord._dwell_timer is None  # both rooms satisfied

    await _set_temp(hass, SENSOR_A, HOT[False])  # calls cool — already cooling
    await _recompute(hass, entry)
    assert _plan(hass, entry).attributes["primary_demand"] == MODE_COOL
    assert coord._dwell_timer is None  # no flip is being held back

    await _set_temp(hass, SENSOR_A, COLD[False])  # now a flip IS held back
    await _recompute(hass, entry)
    assert coord._dwell_timer is not None

    await _set_temp(hass, SENSOR_A, NEUTRAL[False])  # ...and the demand goes away
    await _recompute(hass, entry)
    assert coord._dwell_timer is None


async def test_a_new_deferral_replaces_the_pending_wakeup(
    hass: HomeAssistant,
) -> None:
    """Re-deciding replaces the wakeup: exactly one timer is ever alive."""
    entry, _a, _b = await _setup_dwell(hass)
    coord = await _defer_a_heat_flip(hass, entry)

    # The deferral changes hands: the primary settles, the secondary now calls
    # heat. Each recompute re-syncs the wakeup.
    await _set_temp(hass, SENSOR_A, NEUTRAL[False])
    await _set_temp(hass, SENSOR_B, COLD[False])
    await _recompute(hass, entry)
    assert _plan(hass, entry).attributes["secondary_demand"] == MODE_HEAT
    assert coord._dwell_timer is not None

    # Fire the window WITHOUT ageing the clock: every live wakeup asks for a
    # refresh, and nothing else in this window does.
    with _count_refresh_requests(coord) as requests:
        await _fire_due(hass, coord.hysteresis + 5)
    assert len(requests) == 1  # one wakeup fired, not two

    assert _plan(hass, entry).state == MODE_COOL  # dwell had not really elapsed
    assert coord._dwell_timer is not None  # re-armed for the remaining dwell


async def test_expiry_already_handled_is_a_no_op(hass: HomeAssistant) -> None:
    """A flip that already happened leaves no wakeup and no second recompute."""
    entry, head_a, _b = await _setup_dwell(hass)
    coord = await _defer_a_heat_flip(hass, entry)

    # A sensor event arrives after the dwell has run out: the flip happens here.
    _age_out_dwell(coord)
    await _set_temp(hass, SENSOR_A, COLD[False] - 1)
    await _recompute(hass, entry)
    assert _plan(hass, entry).state == MODE_HEAT
    assert coord._dwell_timer is None

    calls = _record_calls(hass)
    with _count_refresh_requests(coord) as requests:
        await _fire_due(hass, coord.hysteresis + 5)
    assert requests == []  # the retired wakeup does not fire
    assert _head_calls(calls, head_a) == []  # and no head is touched
    assert _plan(hass, entry).state == MODE_HEAT


# --- cancellation: kill switch, standby, unload, reload -------------------------


async def test_kill_switch_cancels_the_wakeup(hass: HomeAssistant) -> None:
    """Disabled coordination arms nothing — a recompute could command nothing."""
    entry, _a, _b = await _setup_dwell(hass)
    coord = await _defer_a_heat_flip(hass, entry)

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _eid(hass, entry, "_coordinator_enable")},
        blocking=True,
    )
    await _recompute(hass, entry)
    assert coord.coordinator_enable is False
    assert coord._dwell_timer is None


async def test_standby_hold_cancels_the_wakeup(hass: HomeAssistant) -> None:
    """An inhibit park drops the wakeup, and releasing the hold re-arms it."""
    hass.states.async_set(INHIBIT, "off")
    await hass.async_block_till_done()
    entry, _a, _b = await _setup_dwell(
        hass,
        **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_OFF},
    )
    coord = await _defer_a_heat_flip(hass, entry)

    await _set_hold(hass, "on")
    await _recompute(hass, entry)
    assert coord.inhibited is True
    assert coord._dwell_timer is None

    await _set_hold(hass, "off")
    await _recompute(hass, entry)
    assert coord._dwell_timer is not None


async def test_unload_cancels_the_wakeup(hass: HomeAssistant) -> None:
    """Unload leaves no timer and no post-retirement recompute or head write."""
    entry, head_a, _b = await _setup_dwell(hass)
    coord = await _defer_a_heat_flip(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coord._dwell_timer is None

    calls = _record_calls(hass)
    _age_out_dwell(coord)
    with _count_refresh_requests(coord) as requests:
        await _fire_due(hass, coord.hysteresis + 5)
    assert requests == []
    assert _head_calls(calls, head_a) == []
    assert coord.current_shared_mode == MODE_COOL  # no stale flip after retirement


async def test_reload_cancels_the_old_wakeup(hass: HomeAssistant) -> None:
    """Reload retires the old coordinator's wakeup with it."""
    entry, _a, _b = await _setup_dwell(hass)
    old = await _defer_a_heat_flip(hass, entry)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data is not old
    assert old._dwell_timer is None

    _age_out_dwell(old)
    with _count_refresh_requests(old) as requests:
        await _fire_due(hass, old.hysteresis + 5)
    assert requests == []
    assert old.current_shared_mode == MODE_COOL


# --- human intent outranks a pending wakeup ------------------------------------


async def test_manual_room_hold_survives_the_dwell_expiring(
    hass: HomeAssistant,
) -> None:
    """A room the user held off is not heated when the dwell it armed expires."""
    entry, head_a, _b = await _setup_dwell(hass)
    coord = await _defer_a_heat_flip(hass, entry)

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _eid(hass, entry, "_primary_enable")},
        blocking=True,
    )
    await hass.async_block_till_done()
    # The user's hold is decision input like any other: it re-syncs the wakeup
    # on the spot, and there is no longer a flip to wait for.
    assert coord.zones[0].enable is False
    assert _plan(hass, entry).attributes["primary_demand"] == MODE_OFF
    assert coord._dwell_timer is None

    calls = _record_calls(hass)
    _age_out_dwell(coord)
    await _fire_due(hass, coord.hysteresis + 5)

    assert _head_calls(calls, head_a) == []  # the held room is never commanded
    assert _plan(hass, entry).state == MODE_COOL
    assert coord.current_shared_mode == MODE_COOL
    assert hass.states.get(head_a).state != MODE_HEAT


async def test_wakeup_decides_on_the_inputs_at_expiry_not_at_arming(
    hass: HomeAssistant,
) -> None:
    """A wakeup that fires on a moved decision recomputes it, never replays it.

    The number entity sets a zone's target and then asks for a refresh, so
    between those two steps the coordinator holds a plan its own inputs have
    already outgrown. Firing the wakeup there is the stale-callback case: the
    target now sits BELOW the room, so heat is no longer wanted, and a callback
    that applied the mode it captured when it was armed would heat anyway.
    """
    entry, head_a, _b = await _setup_dwell(hass)
    coord = await _defer_a_heat_flip(hass, entry)

    coord.zones[0].target = COLD[False] - 5  # 60 °F room, target now 55 °F
    assert coord.data["primary_demand"] == MODE_HEAT  # the plan that armed it
    _age_out_dwell(coord)
    await _fire_due(hass, coord.hysteresis + 5)

    plan = _plan(hass, entry)
    assert plan.attributes["primary_demand"] == MODE_COOL  # recomputed, not replayed
    assert plan.state == MODE_COOL
    assert hass.states.get(head_a).state != MODE_HEAT
    assert coord._dwell_timer is None


async def test_manual_target_change_before_expiry_governs_the_wakeup(
    hass: HomeAssistant,
) -> None:
    """A target the user sets before the wakeup fires decides what it computes."""
    entry, head_a, _b = await _setup_dwell(hass)
    coord = await _defer_a_heat_flip(hass, entry)

    # 60 °F room, target lowered to 60: the room no longer asks for heat.
    await _set_target(hass, _eid(hass, entry, "_primary_target"), COLD[False])
    assert _plan(hass, entry).attributes["primary_demand"] == DEMAND_NEUTRAL
    assert coord._dwell_timer is None  # so the wakeup is dropped

    _age_out_dwell(coord)
    await _fire_due(hass, coord.hysteresis + 5)

    plan = _plan(hass, entry)
    assert plan.state == MODE_COOL
    assert hass.states.get(head_a).state != MODE_HEAT


async def test_manual_shared_mode_choice_restarts_the_dwell(
    hass: HomeAssistant,
) -> None:
    """A manual mode choice stamps its own dwell; the old wakeup can't jump it."""
    entry, _a, _b = await _setup_dwell(hass)
    coord = await _defer_a_heat_flip(hass, entry)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": _eid(hass, entry, "_shared_mode"), "option": MODE_HEAT},
        blocking=True,
    )
    await _recompute(hass, entry)
    assert coord.current_shared_mode == MODE_HEAT

    # The room the user is heating goes warm again: automatic would go back to
    # cool, but the human's fresh dwell governs when that may happen.
    await _set_temp(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    assert _plan(hass, entry).attributes["primary_demand"] == MODE_COOL
    assert coord._dwell_timer is not None

    await _fire_due(hass, coord.hysteresis + 5)
    plan = _plan(hass, entry)
    assert plan.state == MODE_HEAT  # the human's choice still stands
    assert plan.attributes["mode_change_allowed"] is False


# --- equipment protections stay in charge --------------------------------------


async def test_heat_lockout_leaves_nothing_for_the_wakeup(
    hass: HomeAssistant,
) -> None:
    """A locked-out heat call is not a deferred flip: no wakeup, no flip."""
    entry, head_a, _b = await _setup_dwell(hass)
    coord = entry.runtime_data
    await _quiesce(hass, coord)

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _eid(hass, entry, "_heat_lockout")},
        blocking=True,
    )
    await _set_temp(hass, SENSOR_A, COLD[False])  # 60 °F, above the 58 °F floor
    await _recompute(hass, entry)
    assert DEFAULT_HEAT_LOCKOUT_FLOOR == 58.0
    assert coord.heat_lockout is True
    assert _plan(hass, entry).attributes["primary_demand"] == DEMAND_NEUTRAL
    assert coord._dwell_timer is None

    _age_out_dwell(coord)
    await _fire_due(hass, coord.hysteresis + 5)
    assert _plan(hass, entry).state == MODE_COOL
    assert hass.states.get(head_a).state != MODE_HEAT

    # The safety floor is untouched: below it the room gets heat anyway, and
    # with the dwell already elapsed that flip needs no wakeup at all.
    await _set_temp(hass, SENSOR_A, DEFAULT_HEAT_LOCKOUT_FLOOR - 5)
    await _recompute(hass, entry)
    assert _plan(hass, entry).attributes["primary_demand"] == MODE_HEAT
    assert _plan(hass, entry).state == MODE_HEAT


# --- the dwell runs out while a head command is still in flight -----------------
#
# A head command is an await on a real device, and the clock keeps running
# inside it. These cases hold one real climate service call open on a barrier
# and move the dwell — or retire the entry — while the coordinator is parked in
# `_apply`, so the `finally` that re-syncs the wakeup runs in a world that has
# changed underneath it.


def _dwell_left(coord: MXZCoordinator, seconds: float) -> None:
    """Leave ``seconds`` of dwell on the clock (the suite's stamp rewind)."""
    coord._last_mode_change_ts = (
        dt_util.utcnow().timestamp() - coord.hysteresis + seconds
    )


def _defer_a_flip_without_a_pending_refresh(coord: MXZCoordinator) -> None:
    """Primary calls heat, secondary calls cool — with nothing else armed.

    The number entity sets a room's target and THEN asks for a refresh; setting
    the two targets the way it does leaves no debounced refresh behind to wake
    the coordinator up for us, so the only trigger in the measured window is
    the one the coordinator armed for itself. Both rooms read 70 °F.
    """
    coord.zones[0].target = 80.0  # primary calls heat, and outranks...
    coord.zones[1].target = 60.0  # ...the secondary calling cool


@contextmanager
def _held_head_command(monkeypatch: pytest.MonkeyPatch, head: str):
    """Hold the FIRST setpoint write to ``head`` open on a barrier.

    The call is the suite's own ``MockHead.async_set_temperature``, reached
    through the real climate service; only its completion is delayed. Nothing
    about the decision, the plan or the clock is replaced.
    """
    entered = asyncio.Event()
    release = asyncio.Event()
    original = MockHead.async_set_temperature
    held = False

    async def delayed(self: MockHead, **kwargs: Any) -> None:
        nonlocal held
        if self.entity_id == head and not held:
            held = True
            entered.set()
            await release.wait()
        await original(self, **kwargs)

    monkeypatch.setattr(MockHead, "async_set_temperature", delayed)
    try:
        yield entered, release
    finally:
        release.set()  # never leave a refresh parked on the barrier


async def test_dwell_that_expires_during_a_head_command_is_not_lost(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flip deferred just before expiry survives the head command it waited on.

    The plan is computed with two seconds of dwell left, so the flip is
    deferred; the dwell then runs out while the secondary head's setpoint write
    is still awaiting. The re-sync at the end of that refresh sees an elapsed
    dwell and must reconsider the deferral promptly instead of dropping it for
    the next sensor event or the 15-min heartbeat.
    """
    entry, head_a, head_b = await _setup_dwell(hass)
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    assert coord._dwell_timer is None

    with _held_head_command(monkeypatch, head_b) as (entered, release):
        _defer_a_flip_without_a_pending_refresh(coord)
        _dwell_left(coord, 2)
        sensor_stamp = hass.states.get(SENSOR_A).last_updated
        refresh = hass.async_create_task(coord.async_refresh())
        await asyncio.wait_for(entered.wait(), 5)

        # Parked inside _apply, on a plan that defers the flip to heat.
        assert coord._applying_plan["primary_demand"] == MODE_HEAT
        assert coord._applying_plan["secondary_demand"] == MODE_COOL
        assert coord._applying_plan["state"] == MODE_COOL
        assert coord._applying_plan["mode_change_allowed"] is False

        coord._last_mode_change_ts -= 3  # the dwell runs out during the await
        release.set()
        await asyncio.wait_for(refresh, 5)
    await hass.async_block_till_done()

    # No sensor event and no heartbeat: whatever wakes the coordinator now is
    # something it armed for itself.
    assert hass.states.get(SENSOR_A).last_updated == sensor_stamp
    await _fire_due(hass, 1)
    assert hass.states.get(SENSOR_A).last_updated == sensor_stamp

    plan = _plan(hass, entry)
    assert plan.state == MODE_HEAT
    assert plan.attributes["mode_change_allowed"] is True
    assert hass.states.get(head_a).state == MODE_HEAT
    assert coord._dwell_timer is None  # and nothing is left waiting


@pytest.mark.parametrize("retire", ["unload", "reload"])
async def test_a_refresh_finishing_after_retirement_arms_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, retire: str
) -> None:
    """Retirement during a head command is permanent for that coordinator.

    The entry is unloaded (or reloaded) while a refresh is parked on a head
    command. Shutdown cancels the live wakeup, but that refresh still has its
    re-sync to run: a retired coordinator must not arm a timer, and a timer of
    its must never ask it for a refresh again.
    """
    entry, head_a, head_b = await _setup_dwell(hass)
    coord = entry.runtime_data
    await _quiesce(hass, coord)

    with _held_head_command(monkeypatch, head_b) as (entered, release):
        _defer_a_flip_without_a_pending_refresh(coord)
        refresh = hass.async_create_task(coord.async_refresh())
        await asyncio.wait_for(entered.wait(), 5)
        assert coord._applying_plan["mode_change_allowed"] is False

        if retire == "unload":
            assert await asyncio.wait_for(
                hass.config_entries.async_unload(entry.entry_id), 10
            )
        else:
            await asyncio.wait_for(
                hass.config_entries.async_reload(entry.entry_id), 10
            )
            assert entry.runtime_data is not coord
        assert coord._dwell_timer is None  # shutdown cancelled the live one

        release.set()
        await asyncio.wait_for(refresh, 5)
    await hass.async_block_till_done()
    if retire == "reload":
        await _quiesce(hass, entry.runtime_data)  # the successor's own burst

    # The in-flight refresh has now run its re-sync on a retired coordinator.
    assert coord._dwell_timer is None

    calls = _record_calls(hass)
    _age_out_dwell(coord)
    with _count_refresh_requests(coord) as requests:
        await _fire_due(hass, coord.hysteresis + 5)
    assert coord._dwell_timer is None
    assert requests == []
    assert _head_calls(calls, head_a) == []
    assert _head_calls(calls, head_b) == []
    assert coord.current_shared_mode == MODE_COOL  # no stale flip after retirement


async def test_a_wakeup_firing_after_retirement_asks_for_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a handle cannot recall a callback the loop already picked up.

    So the callback itself is fired here after the entry is unloaded — the one
    interleaving a cancel can never cover — and the retired coordinator has to
    refuse it.
    """
    entry, head_a, _b = await _setup_dwell(hass)
    armed: list[Any] = []
    original_call_later = coordinator_module.async_call_later

    def capturing(hass_: HomeAssistant, delay: float, action: Any) -> Any:
        if "_arm_dwell_timer" in getattr(action, "__qualname__", ""):
            armed.append(action)
        return original_call_later(hass_, delay, action)

    monkeypatch.setattr(coordinator_module, "async_call_later", capturing)
    coord = await _defer_a_heat_flip(hass, entry)
    assert armed  # the deferred flip armed a wakeup...
    fire = armed[-1]  # ...and the last one armed owns the live handle

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coord._dwell_timer is None

    calls = _record_calls(hass)
    _age_out_dwell(coord)
    with _count_refresh_requests(coord) as requests:
        fire(dt_util.utcnow())  # the callback the loop had already taken
        await hass.async_block_till_done()
        await _fire_due(hass, coord.hysteresis + 5)
    assert requests == []
    assert coord._dwell_timer is None
    assert _head_calls(calls, head_a) == []
    assert coord.current_shared_mode == MODE_COOL
