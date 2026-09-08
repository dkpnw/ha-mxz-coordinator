"""Coast follow-up recompute: a room that coasts through an engage-latch
direction flip is re-evaluated on every head, not only on heads that echo a fan
mode back.

The anti-whiplash rule in ``engage_with_latch`` disengages a room before it may
run the other way, so the compute that sees the flip parks the head at the idle
action and the room is re-engaged by the NEXT compute. Nothing scheduled that
next compute. On a head with a fan mode the coordinator's own fan write echoed
back and asked for a refresh; a valid dual-setpoint head with no ``FAN_MODE``
capability emits no echo, so the room stayed parked until an unrelated sensor
event, the 15-min heartbeat, an ``mxz_recompute`` event or a manual call.

The follow-up is one cancellable ``async_call_later`` (the dwell-expiry wakeup
in ``test_dwell_wakeup`` is the model) that carries NO decision: it asks for a
recompute of the inputs as they are when it fires.

Time idiom, as in ``test_dwell_wakeup``: firing HA's time machinery does not
move ``dt_util.utcnow``, so a window is measured by firing the timers due in it
and then the debounced refresh they asked for.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, callback
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
    DOMAIN,
    ENGAGE_SATISFIED,
    FAN_AUTO,
    FAN_HIGH,
    INHIBIT_ACTION_OFF,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
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
    _set_temp,
    _setup_mock_heads,
)
from tests.test_dwell_wakeup import (
    COLD,
    HOT,
    NEUTRAL,
    _count_refresh_requests,
    _plan,
    _quiesce,
)
from tests.test_idle_action import (
    _head_calls,
    _record_calls,
    _settle_requested_refreshes,
)

# Setpoint edges the re-engaged heat run must produce, per unit system.
HEAT_EDGES = {False: (70.0, 72.0), True: (21.0, 22.0)}


class NoFanHead(MockHead):
    """A valid dual-setpoint head with no FAN_MODE capability.

    Real heads of this shape exist; they never echo a fan mode back, so the
    coordinator's own park write produces no state change it listens to.
    """

    _attr_fan_modes = None
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, suffix: str) -> None:
        super().__init__(suffix)
        self._attr_fan_mode = None


class NoFanHeadC(NoFanHead, MockHeadC):
    """The metric no-fan head (°C system)."""


async def _setup_coast(
    hass: HomeAssistant,
    *,
    cls: type[MockHead] = NoFanHead,
    celsius: bool = False,
    **extra: Any,
) -> tuple[MockConfigEntry, str, str]:
    """Two heads, coordinator and both rooms enabled, dwell pinned to 0.

    The mode dwell is not the subject here — it is pinned off so the shared-mode
    flip that accompanies the room's direction flip is never itself deferred,
    and so no dwell wakeup is armed alongside the follow-up. Fan boost is off so
    the only head writes in a measured window are the mode/setpoint ones.
    """
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass, cls=cls)
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
            CONF_MODE_HYSTERESIS: 0,
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


async def _drain(hass: HomeAssistant, coord: MXZCoordinator) -> None:
    """Run the refreshes already requested, until the coordinator is idle.

    With the request-refresh cooldown clear, the next sensor event refreshes
    immediately, so a window is measured from the compute under test and not
    from a leftover timer. Never called while a follow-up is armed: these
    windows are each longer than one.
    """
    await _settle_requested_refreshes(hass, coord)


async def _advance(hass: HomeAssistant, seconds: float) -> None:
    """Run every HA timer due within ``seconds`` of the window's start, only.

    Unlike ``_settle`` this does NOT then expire the request-refresh cooldown,
    so it can stop between two deadlines a second apart. ``dt_util.utcnow`` does
    not move when timers fire, so every call is an absolute offset from the
    compute under test. HA's own helper adds half a second, so ``seconds`` is a
    floor and not a ceiling: 10 reaches 10.5, which is short of 11.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def _fan_tokens(hass: HomeAssistant, head: str) -> list[str | None]:
    """Every fan_mode value one head publishes, in order, as it CHANGES.

    A fan echo is a change, not a capability: this is what ``_on_head_change``
    actually keys on, so it is the honest precondition for calling a setup an
    echoing one.
    """
    tokens: list[str | None] = []

    @callback
    def _seen(event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.entity_id != head:
            return
        old_state = event.data.get("old_state")
        new_fan = new_state.attributes.get("fan_mode")
        old_fan = old_state.attributes.get("fan_mode") if old_state else None
        if new_fan != old_fan:
            tokens.append(new_fan)

    hass.bus.async_listen(EVENT_STATE_CHANGED, _seen)
    return tokens


def _count_coast_fires(coord: MXZCoordinator) -> list[Any]:
    """Count every ACTUAL firing of the coast wakeup's callback.

    The interception is scoped to the body of ``_arm_coast_timer``, so only the
    coast's own ``async_call_later`` is wrapped and a dwell, heal or debounce
    timer can never be miscounted as a coast wakeup. The delay, the returned
    cancel handle and the callback's own work are all unchanged.
    """
    fires: list[Any] = []
    real_arm = coord._arm_coast_timer
    real_call_later = coordinator_module.async_call_later

    def _arm() -> None:
        def _spy(hass: HomeAssistant, delay: float, action: Any) -> Any:
            @callback
            def _counted(now: Any) -> None:
                fires.append(now)
                action(now)

            return real_call_later(hass, delay, _counted)

        with patch.object(coordinator_module, "async_call_later", _spy):
            real_arm()

    coord._arm_coast_timer = _arm
    return fires


async def _settle(hass: HomeAssistant, seconds: float) -> None:
    """Run every HA timer due within ``seconds``, then the refresh it asked for."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(seconds=REQUEST_REFRESH_DEFAULT_COOLDOWN + 1),
    )
    await hass.async_block_till_done()


async def _engage_then_flip(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    head_b: str,
    *,
    celsius: bool = False,
) -> MXZCoordinator:
    """Latch the secondary room into a cool run, then flip its direction.

    Both steps go through the ordinary sensor-event path — a real state write
    on the room's own sensor — not a forced refresh.
    """
    coord = entry.runtime_data
    await _quiesce(hass, coord)

    await _set_temp(hass, SENSOR_B, HOT[celsius])  # secondary runs cool
    assert coord._engage_latch["secondary"] == MODE_COOL
    assert hass.states.get(head_b).state == MODE_COOL
    await _drain(hass, coord)

    await _set_temp(hass, SENSOR_B, COLD[celsius])  # the direction flip
    plan = _plan(hass, entry)
    assert plan.attributes["secondary_demand"] == MODE_HEAT  # the room wants heat
    assert plan.attributes["secondary_engage"] == ENGAGE_SATISFIED  # ...but coasts
    assert coord._engage_latch["secondary"] == ""  # the latch let go
    assert hass.states.get(head_b).state == MODE_FAN_ONLY  # parked at the idle action
    return coord


# --- the defect this node closes -----------------------------------------------


@pytest.mark.parametrize("celsius", [False, True], ids=["fahrenheit", "celsius"])
async def test_a_no_echo_head_is_recommanded_after_the_coast(
    hass: HomeAssistant, celsius: bool
) -> None:
    """A head that emits no fan echo is commanded after the coast anyway.

    No sensor event, no heartbeat, no echo: only the follow-up the coast armed.
    Unit-symmetric, each unit system with its own readings and setpoint band.
    """
    entry, _a, head_b = await _setup_coast(
        hass, cls=NoFanHeadC if celsius else NoFanHead, celsius=celsius
    )
    coord = await _engage_then_flip(hass, entry, head_b, celsius=celsius)
    assert coord._coast_timer is not None  # the coast armed its own follow-up

    sensor_stamp = hass.states.get(SENSOR_B).last_updated
    await _settle(hass, coordinator_module.COAST_FOLLOWUP_DELAY + 1)

    # Nothing else woke the coordinator: neither room's sensor changed, the
    # 15-min heartbeat is a quarter of an hour out, and this head has no fan
    # mode to echo.
    assert hass.states.get(SENSOR_B).last_updated == sensor_stamp
    assert hass.states.get(head_b).attributes.get("fan_mode") is None
    head = hass.states.get(head_b)
    assert head.state == MODE_HEAT
    low, high = HEAT_EDGES[celsius]
    assert head.attributes["target_temp_low"] == low  # normal clamped edges
    assert head.attributes["target_temp_high"] == high
    assert coord._engage_latch["secondary"] == MODE_HEAT
    assert coord._coast_timer is None  # nothing left to wait for


async def test_the_coast_itself_never_engages_the_other_direction(
    hass: HomeAssistant,
) -> None:
    """The follow-up re-evaluates later; it does not flip on the same compute.

    This is the anti-whiplash invariant the follow-up must not buy its way
    around: the compute that sees the direction flip parks the head, and the
    opposite direction is only reached by a LATER compute.
    """
    entry, _a, head_b = await _setup_coast(hass)
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    await _set_temp(hass, SENSOR_B, HOT[False])
    await _drain(hass, coord)

    calls = _record_calls(hass)
    await _set_temp(hass, SENSOR_B, COLD[False])

    # The flip's own compute wrote fan_only and nothing else to this head.
    assert [c for _s, c in _head_calls(calls, head_b)] == [MODE_FAN_ONLY]
    assert hass.states.get(head_b).state == MODE_FAN_ONLY
    assert coord._coast_timer is not None


# --- an echo-emitting head is unchanged ----------------------------------------


async def test_an_echo_head_is_still_commanded_exactly_once(
    hass: HomeAssistant,
) -> None:
    """A head that really echoes must not get a second command.

    Fan capability alone does not produce an echo: the echo is a fan_mode STATE
    CHANGE, so it needs a fan write, so it needs fan boost enabled. This is that
    configuration — the cool run boosts the fan up the ladder, and parking the
    coasting head releases it to ``auto``. That released token is the echo.

    ``_on_head_change`` turns it into a debounced refresh due one cooldown (10 s)
    after the park; the coast wakeup is due a second later (11 s). So the window
    below stops between them: the echo's compute — which coasts nothing — runs,
    re-commands the head, and drops the pending wakeup, which therefore never
    fires at all. The head is commanded into the new direction exactly once.
    """
    entry, _a, head_b = await _setup_coast(
        hass, cls=MockHead, **{CONF_FAN_BOOST_ENABLE: True}
    )
    fires = _count_coast_fires(entry.runtime_data)
    tokens = _fan_tokens(hass, head_b)
    coord = await _engage_then_flip(hass, entry, head_b)

    # PRECONDITION, asserted so this can never silently become a second no-echo
    # case: the fan token really did move, ladder -> auto, across the coast.
    assert tokens[-2:] == [FAN_HIGH, FAN_AUTO]
    assert coord._coast_timer is not None

    calls = _record_calls(hass)
    await _advance(hass, REQUEST_REFRESH_DEFAULT_COOLDOWN)  # past 10 s, not past 11

    # The echo's own refresh did the work, before the coast's deadline.
    assert hass.states.get(head_b).state == MODE_HEAT
    assert coord._coast_timer is None  # that compute dropped the wakeup...
    assert fires == []  # ...so it never fired

    # And its deadline passing changes nothing.
    await _advance(hass, coordinator_module.COAST_FOLLOWUP_DELAY + 5)
    assert fires == []

    heat_commands = [
        (service, extra)
        for service, extra in _head_calls(calls, head_b)
        if extra == MODE_HEAT
    ]
    assert len(heat_commands) == 1
    assert hass.states.get(head_b).state == MODE_HEAT
    assert coord._coast_timer is None


async def test_a_fan_capable_head_with_the_boost_off_still_needs_the_wakeup(
    hass: HomeAssistant,
) -> None:
    """The no-echo control on a FAN-CAPABLE head: boost off means no echo.

    ``_apply_fan`` returns immediately when the boost is disabled, so this head
    is never written a fan mode, its token never changes, and nothing echoes —
    exactly like the no-fan head, for a different reason. Only the coast wakeup
    re-commands it, and it fires exactly once. The one-command oracle is checked
    here too, so a duplicate command cannot hide on this path either.
    """
    entry, _a, head_b = await _setup_coast(hass, cls=MockHead)  # boost off
    fires = _count_coast_fires(entry.runtime_data)
    tokens = _fan_tokens(hass, head_b)
    coord = await _engage_then_flip(hass, entry, head_b)

    assert tokens == []  # no fan write at all, so no echo
    assert coord._coast_timer is not None

    calls = _record_calls(hass)
    await _advance(hass, REQUEST_REFRESH_DEFAULT_COOLDOWN)
    assert fires == []  # nothing is due in the echo's window
    assert hass.states.get(head_b).state == MODE_FAN_ONLY  # still parked

    await _settle(hass, coordinator_module.COAST_FOLLOWUP_DELAY + 1)
    assert len(fires) == 1  # the coast wakeup, exactly once

    heat_commands = [
        (service, extra)
        for service, extra in _head_calls(calls, head_b)
        if extra == MODE_HEAT
    ]
    assert len(heat_commands) == 1
    assert hass.states.get(head_b).state == MODE_HEAT
    assert coord._coast_timer is None


# --- arming, replacement and no-ops --------------------------------------------


async def test_nothing_is_armed_while_no_room_coasts(hass: HomeAssistant) -> None:
    """The control: an ordinary engage arms no follow-up."""
    entry, _a, head_b = await _setup_coast(hass)
    coord = entry.runtime_data
    await _quiesce(hass, coord)
    assert coord._coast_timer is None

    await _set_temp(hass, SENSOR_B, HOT[False])
    assert hass.states.get(head_b).state == MODE_COOL
    assert coord._coast_timer is None


async def test_a_second_coast_replaces_the_pending_followup(
    hass: HomeAssistant,
) -> None:
    """Consecutive coasts share one handle: exactly one wakeup is ever alive.

    Both rooms are running cool. The secondary's direction flips and it coasts;
    then the primary reaches its target and coasts too, while the first
    follow-up is still pending. The second coast must CANCEL the first handle,
    not leave it armed alongside its own. The second compute is forced so that
    it lands with the first follow-up still pending; the trigger path is not
    what this test measures.
    """
    entry, head_a, head_b = await _setup_coast(hass)
    coord = entry.runtime_data
    await _quiesce(hass, coord)

    armed: list[Any] = []
    cancelled: list[Any] = []
    original_call_later = coordinator_module.async_call_later

    def capturing(hass_: HomeAssistant, delay: float, action: Any) -> Any:
        cancel = original_call_later(hass_, delay, action)
        if "_arm_coast_timer" not in getattr(action, "__qualname__", ""):
            return cancel
        armed.append(action)

        def cancel_and_record() -> None:
            cancelled.append(action)
            cancel()

        return cancel_and_record

    with patch.object(coordinator_module, "async_call_later", capturing):
        await _set_temp(hass, SENSOR_A, HOT[False])
        await _set_temp(hass, SENSOR_B, HOT[False])
        await _drain(hass, coord)
        assert coord._engage_latch["primary"] == MODE_COOL
        assert coord._engage_latch["secondary"] == MODE_COOL
        assert coord._coast_timer is None

        await _set_temp(hass, SENSOR_B, COLD[False])  # the flip: coast one
        assert coord._engage_latch["secondary"] == ""
        first = coord._coast_timer
        assert first is not None
        assert len(armed) == 1
        assert cancelled == []

        await _set_temp(hass, SENSOR_A, NEUTRAL[False])  # primary satisfied...
        await _recompute(hass, entry)  # ...coast two, first follow-up still pending
        assert coord._engage_latch["primary"] == ""
        assert hass.states.get(head_a).state == MODE_FAN_ONLY

    assert len(armed) == 2  # the second coast armed its own...
    assert cancelled == [armed[0]]  # ...and cancelled the first
    assert coord._coast_timer is not first

    with _count_refresh_requests(coord) as requests:
        await _settle(hass, coordinator_module.COAST_FOLLOWUP_DELAY + 4)
    assert len(requests) == 1  # one wakeup fired in that window, not two
    assert hass.states.get(head_b).state == MODE_HEAT  # and the flip was resolved


async def test_an_input_event_before_the_followup_resolves_it(
    hass: HomeAssistant,
) -> None:
    """The follow-up is dropped by whatever re-evaluated the room first.

    The flipped room drifts back onto its target, and the refresh that event
    asks for arrives before the follow-up is due. That compute coasts nothing,
    so the follow-up is cancelled: the head is not commanded into the direction
    the room wanted at the coast, and no second recompute is left behind.
    """
    entry, _a, head_b = await _setup_coast(hass)
    coord = await _engage_then_flip(hass, entry, head_b)

    await _set_temp(hass, SENSOR_B, NEUTRAL[False])  # back on target
    calls = _record_calls(hass)
    with _count_refresh_requests(coord) as requests:
        await _settle(hass, coordinator_module.COAST_FOLLOWUP_DELAY + 1)

    assert requests == []  # the replaced follow-up never fired
    assert hass.states.get(head_b).state == MODE_FAN_ONLY
    assert [e for _s, e in _head_calls(calls, head_b) if e == MODE_HEAT] == []
    assert coord._engage_latch["secondary"] == ""
    assert coord._coast_timer is None


# --- cancellation: kill switch, standby, unload, reload -------------------------


async def test_kill_switch_cancels_the_followup(hass: HomeAssistant) -> None:
    """Disabled coordination arms nothing — a recompute could command nothing."""
    entry, _a, head_b = await _setup_coast(hass)
    coord = await _engage_then_flip(hass, entry, head_b)
    assert coord._coast_timer is not None

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _eid(hass, entry, "_coordinator_enable")},
        blocking=True,
    )
    await _recompute(hass, entry)
    assert coord.coordinator_enable is False
    assert coord._coast_timer is None


async def test_standby_hold_cancels_the_followup(hass: HomeAssistant) -> None:
    """An inhibit park drops the follow-up: every head is held at one mode."""
    hass.states.async_set(INHIBIT, "off")
    await hass.async_block_till_done()
    entry, _a, head_b = await _setup_coast(
        hass,
        **{CONF_INHIBIT_ENTITY: INHIBIT, CONF_INHIBIT_ACTION: INHIBIT_ACTION_OFF},
    )
    coord = await _engage_then_flip(hass, entry, head_b)
    assert coord._coast_timer is not None

    await _set_hold(hass, "on")
    await _recompute(hass, entry)
    assert coord.inhibited is True
    assert coord._coast_timer is None


async def test_unload_cancels_the_followup(hass: HomeAssistant) -> None:
    """Unload leaves no handle, no post-retirement recompute and no head write."""
    entry, _a, head_b = await _setup_coast(hass)
    coord = await _engage_then_flip(hass, entry, head_b)
    assert coord._coast_timer is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coord._coast_timer is None

    calls = _record_calls(hass)
    with _count_refresh_requests(coord) as requests:
        await _settle(hass, coordinator_module.COAST_FOLLOWUP_DELAY + 1)
    assert requests == []
    assert _head_calls(calls, head_b) == []
    assert hass.states.get(head_b).state == MODE_FAN_ONLY


async def test_reload_cancels_the_old_followup(hass: HomeAssistant) -> None:
    """Reload retires the old coordinator's follow-up with it."""
    entry, _a, head_b = await _setup_coast(hass)
    old = await _engage_then_flip(hass, entry, head_b)
    assert old._coast_timer is not None

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data is not old
    assert old._coast_timer is None

    with _count_refresh_requests(old) as requests:
        await _settle(hass, coordinator_module.COAST_FOLLOWUP_DELAY + 1)
    assert requests == []


async def test_a_followup_firing_after_retirement_asks_for_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a handle cannot recall a callback the loop already picked up."""
    entry, _a, head_b = await _setup_coast(hass)
    armed: list[Any] = []
    original_call_later = coordinator_module.async_call_later

    def capturing(hass_: HomeAssistant, delay: float, action: Any) -> Any:
        if "_arm_coast_timer" in getattr(action, "__qualname__", ""):
            armed.append(action)
        return original_call_later(hass_, delay, action)

    monkeypatch.setattr(coordinator_module, "async_call_later", capturing)
    coord = await _engage_then_flip(hass, entry, head_b)
    assert armed  # the coast armed a follow-up...
    fire = armed[-1]  # ...and the last one armed owns the live handle

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coord._coast_timer is None

    calls = _record_calls(hass)
    with _count_refresh_requests(coord) as requests:
        fire(dt_util.utcnow())  # the callback the loop had already taken
        await hass.async_block_till_done()
        await _settle(hass, coordinator_module.COAST_FOLLOWUP_DELAY + 1)
    assert requests == []
    assert coord._coast_timer is None
    assert _head_calls(calls, head_b) == []
