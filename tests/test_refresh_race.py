"""Refresh requests made while a plan is being applied are never lost.

The service barrier reproduces the HA 2024.12 debouncer lock window from the
M29 review.  The old plan is computed first and held while writing the second
head.  A room-band change then requests a newer plan.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.mxz_coordinator import coordinator as coordinator_module
from custom_components.mxz_coordinator.const import MODE_COOL
from custom_components.mxz_coordinator.coordinator import MXZCoordinator
from tests.test_drive import SENSOR_A, SENSOR_B, MockHead, _eid, _set_temp
from tests.test_dwell_wakeup import _quiesce, _setup_dwell
from tests.test_idle_action import _settle_requested_refreshes
from tests.test_issue18_drift import _zone0


async def _set_band(
    hass: HomeAssistant,
    entry,
    action: str,
    value: float,
) -> None:
    """Change the primary band without waiting for the deliberately held task."""
    domain = "number" if action == "number" else "button"
    service = "set_value" if action == "number" else "press"
    data = {"entity_id": _eid(hass, entry, "_primary_drift")}
    if action == "number":
        data["value"] = value
    else:
        data["entity_id"] = _eid(hass, entry, "_primary_drift_follow_global")
    await hass.services.async_call(domain, service, data, blocking=True)


async def _prepare_race(hass: HomeAssistant, celsius: bool):
    """Leave the primary satisfied on a wide band and the secondary cooling."""
    entry, head_a, head_b = await _setup_dwell(hass, celsius=celsius)
    coord: MXZCoordinator = entry.runtime_data
    await _quiesce(hass, coord)

    wide = 2.0 if celsius else 4.0
    narrow = 0.5 if celsius else 1.0
    await _set_band(hass, entry, "number", wide)
    await _settle_requested_refreshes(hass, coord)
    await _set_temp(hass, SENSOR_A, coord.zones[0].target + wide * 0.75)
    await _set_temp(hass, SENSOR_B, coord.zones[1].target + wide * 1.5)
    await _settle_requested_refreshes(hass, coord)

    old = _zone0(hass, entry)
    assert old["drift"] == wide
    assert old["engage"] == "satisfied"
    assert hass.states.get(head_a).state == "fan_only"
    assert hass.states.get(head_b).state == MODE_COOL

    # Make the next old-plan apply issue a real set_temperature service call.
    # This is fixture state only and requests no refresh of its own.
    coord.zones[1].target += coord.target_step
    return entry, head_a, head_b, wide, narrow


@pytest.mark.parametrize("celsius", [False, True], ids=["fahrenheit", "celsius"])
@pytest.mark.parametrize("action", ["number", "press"])
@pytest.mark.parametrize("origin", ["direct", "debounced"])
async def test_band_change_during_service_delivery_publishes_newer_plan_last(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    action: str,
    celsius: bool,
) -> None:
    """The active old refresh finishes, then exactly one current-input refresh wins."""
    entry, head_a, head_b, wide, narrow = await _prepare_race(hass, celsius)
    coord: MXZCoordinator = entry.runtime_data
    entered = asyncio.Event()
    release = asyncio.Event()
    held = False
    original = MockHead.async_set_temperature

    async def hold_first_secondary(self, **kwargs):
        nonlocal held
        if self.entity_id == head_b and not held:
            held = True
            entered.set()
            await release.wait()
        await original(self, **kwargs)

    monkeypatch.setattr(MockHead, "async_set_temperature", hold_first_secondary)
    start = coord.async_refresh if origin == "direct" else coord.async_request_refresh
    refresh = hass.async_create_task(start())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert coord._applying_plan is not None
        assert coord._applying_plan["zones"][0]["drift"] == wide
        await _set_band(hass, entry, action, narrow)
    finally:
        release.set()
        await asyncio.wait_for(refresh, 3)

    await _settle_requested_refreshes(hass, coord)
    observed = {
        "origin": origin,
        "action": action,
        "unit": coord.temp_unit,
        "plan_drift": _zone0(hass, entry)["drift"],
        "engage": _zone0(hass, entry)["engage"],
        "head": hass.states.get(head_a).state,
    }
    print("M43_RACE", observed)
    assert observed["plan_drift"] == narrow
    assert observed["engage"] == MODE_COOL
    assert observed["head"] == MODE_COOL


@pytest.mark.parametrize("celsius", [False, True], ids=["fahrenheit", "celsius"])
@pytest.mark.parametrize("action", ["number", "press"])
@pytest.mark.parametrize("origin", ["direct", "debounced"])
async def test_band_change_with_a_pending_head_echo_stays_current(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    action: str,
    celsius: bool,
) -> None:
    """A command accepted before its state echo still leaves the current plan."""
    entry, head_a, _head_b, _wide, narrow = await _prepare_race(hass, celsius)
    coord: MXZCoordinator = entry.runtime_data
    pending_echoes = []

    async def accept_then_echo_later(self, **kwargs):
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = mode
        if (low := kwargs.get("target_temp_low")) is not None:
            self._attr_target_temperature_low = low
        if (high := kwargs.get("target_temp_high")) is not None:
            self._attr_target_temperature_high = high
        pending_echoes.append(self.async_write_ha_state)

    monkeypatch.setattr(MockHead, "async_set_temperature", accept_then_echo_later)
    start = coord.async_refresh if origin == "direct" else coord.async_request_refresh
    await start()
    assert pending_echoes
    await _set_band(hass, entry, action, narrow)
    await _settle_requested_refreshes(hass, coord)
    for echo in pending_echoes:
        echo()
    await hass.async_block_till_done()

    observed = {
        "origin": origin,
        "action": action,
        "unit": coord.temp_unit,
        "plan_drift": _zone0(hass, entry)["drift"],
        "engage": _zone0(hass, entry)["engage"],
        "head": hass.states.get(head_a).state,
    }
    print("M43_PENDING_ECHO", observed)
    assert observed["plan_drift"] == narrow
    assert observed["engage"] == MODE_COOL
    assert observed["head"] == MODE_COOL


async def _hold_next_secondary_setpoint(hass, monkeypatch, coord, head_b):
    """Return a direct refresh held in the next secondary setpoint service."""
    entered = asyncio.Event()
    release = asyncio.Event()
    original = MockHead.async_set_temperature
    held = False

    async def hold(self, **kwargs):
        nonlocal held
        if self.entity_id == head_b and not held:
            held = True
            entered.set()
            await release.wait()
        await original(self, **kwargs)

    monkeypatch.setattr(MockHead, "async_set_temperature", hold)
    refresh = hass.async_create_task(coord.async_refresh())
    await asyncio.wait_for(entered.wait(), 3)
    return refresh, release


async def test_no_mid_refresh_request_means_no_followup(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lone active refresh remains one refresh."""
    entry, _head_a, head_b, _wide, _narrow = await _prepare_race(hass, False)
    coord: MXZCoordinator = entry.runtime_data
    computes = 0
    original = coord._compute

    def count_compute():
        nonlocal computes
        computes += 1
        return original()

    monkeypatch.setattr(coord, "_compute", count_compute)
    refresh, release = await _hold_next_secondary_setpoint(
        hass, monkeypatch, coord, head_b
    )
    release.set()
    await asyncio.wait_for(refresh, 3)
    await hass.async_block_till_done()
    assert computes == 1
    assert coord._refresh_followup_timer is None


async def test_many_mid_refresh_requests_coalesce_into_one_followup(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated requests in one active window produce one current-input pass."""
    entry, _head_a, head_b, _wide, _narrow = await _prepare_race(hass, False)
    coord: MXZCoordinator = entry.runtime_data
    computes = 0
    original = coord._compute

    def count_compute():
        nonlocal computes
        computes += 1
        return original()

    monkeypatch.setattr(coord, "_compute", count_compute)
    refresh, release = await _hold_next_secondary_setpoint(
        hass, monkeypatch, coord, head_b
    )
    for _ in range(3):
        await coord.async_request_refresh()
    release.set()
    await asyncio.wait_for(refresh, 3)
    if coord._needs_legacy_refresh_guard:
        await hass.async_block_till_done()
    else:
        async_fire_time_changed(
            hass,
            dt_util.utcnow()
            + timedelta(seconds=REQUEST_REFRESH_DEFAULT_COOLDOWN + 1),
        )
        await hass.async_block_till_done()
    assert computes == 2
    assert coord._refresh_followup_timer is None


async def test_coalesced_followup_preserves_dwell_and_coast_latch(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The follow-up recomputes normally: dwell holds and the latch coasts."""
    entry, head_a, _head_b = await _setup_dwell(hass)
    coord: MXZCoordinator = entry.runtime_data
    await _quiesce(hass, coord)
    await _set_temp(hass, SENSOR_A, coord.zones[0].target + 5)
    await _settle_requested_refreshes(hass, coord)
    assert _zone0(hass, entry)["engage"] == MODE_COOL
    assert hass.states.get(head_a).state == MODE_COOL

    # A target move makes the old cool plan issue a setpoint command. Hold it
    # while the reading crosses all the way to a heat call.
    coord.zones[0].target += coord.target_step
    entered = asyncio.Event()
    release = asyncio.Event()
    original = MockHead.async_set_temperature
    held = False

    async def hold_primary(self, **kwargs):
        nonlocal held
        if self.entity_id == head_a and not held:
            held = True
            entered.set()
            await release.wait()
        await original(self, **kwargs)

    monkeypatch.setattr(MockHead, "async_set_temperature", hold_primary)
    refresh = hass.async_create_task(coord.async_refresh())
    await asyncio.wait_for(entered.wait(), 3)
    dwell_stamp = coord._last_mode_change_ts
    hass.states.async_set(
        SENSOR_A,
        str(coord.zones[0].target - 5),
        {ATTR_UNIT_OF_MEASUREMENT: coord.temp_unit},
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(refresh, 3)
    if coord._needs_legacy_refresh_guard:
        await hass.async_block_till_done()
    else:
        async_fire_time_changed(
            hass,
            dt_util.utcnow()
            + timedelta(seconds=REQUEST_REFRESH_DEFAULT_COOLDOWN + 1),
        )
        await hass.async_block_till_done()

    zone = _zone0(hass, entry)
    assert zone["demand"] == "heat"
    assert zone["engage"] == "satisfied"  # coast; never same-compute whiplash
    assert coord.data["state"] == MODE_COOL  # the original dwell still holds
    assert coord._last_mode_change_ts == dwell_stamp
    assert coord._dwell_timer is not None
    assert coord._coast_timer is not None
    assert hass.states.get(head_a).state == "fan_only"


def _record_computes(coord: MXZCoordinator, monkeypatch: pytest.MonkeyPatch):
    """Return the drift snapshot list populated by each subsequent compute."""
    snapshots = []
    original = coord._compute

    def compute():
        plan = original()
        snapshots.append(plan["zones"][0]["drift"])
        return plan

    monkeypatch.setattr(coord, "_compute", compute)
    return snapshots


async def _drain_refreshes(hass: HomeAssistant, coord: MXZCoordinator) -> None:
    """Run requested work, including the real debouncer cooldown when needed."""
    await _settle_requested_refreshes(hass, coord)
    await hass.async_block_till_done()


@pytest.mark.parametrize("origin", ["direct", "debounced"])
@pytest.mark.parametrize("action", ["number", "press"])
@pytest.mark.parametrize("delivery", ["entity", "service"])
async def test_completion_listener_request_publishes_current_inputs(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    action: str,
    delivery: str,
) -> None:
    """A synchronous publication listener cannot lose an entity band change."""
    entry, head_a, _head_b, wide, narrow = await _prepare_race(hass, False)
    coord: MXZCoordinator = entry.runtime_data
    suffix = (
        "_primary_drift"
        if action == "number"
        else "_primary_drift_follow_global"
    )
    domain = "number" if action == "number" else "button"
    entity = next(
        entity
        for entity in hass.data[domain].entities
        if entity.entity_id == _eid(hass, entry, suffix)
    )
    tasks = []
    windows = []
    fired = False
    original_request = coord.async_request_refresh

    async def request():
        windows.append(
            (
                coord._refresh_lock.locked(),
                coord._debounced_refresh._execute_lock.locked(),
            )
        )
        await original_request()

    monkeypatch.setattr(coord, "async_request_refresh", request)

    @callback
    def on_publish():
        nonlocal fired
        if fired:
            return
        fired = True
        change = (
            entity.async_set_native_value(narrow)
            if action == "number"
            else entity.async_press()
        )
        if delivery == "service":
            change.close()
            change = _set_band(hass, entry, action, narrow)
        tasks.append(hass.async_create_task(change))

    unsub = coord.async_add_listener(on_publish)
    try:
        start = coord.async_refresh if origin == "direct" else coord.async_request_refresh
        await start()
        await asyncio.gather(*tasks)
        await _drain_refreshes(hass, coord)
        observed = (
            coord.zones[0].drift,
            _zone0(hass, entry)["drift"],
            hass.states.get(head_a).state,
        )
        assert fired and windows
        assert (
            False,
            origin == "debounced" or not coord._needs_legacy_refresh_guard,
        ) in windows
        if coord._needs_legacy_refresh_guard and origin == "debounced":
            assert windows[-1] == (False, False)
        expected_override = narrow if action == "number" else None
        assert observed == (expected_override, narrow, MODE_COOL), (
            origin,
            action,
            delivery,
            windows,
            observed,
            wide,
        )
    finally:
        unsub()


@pytest.mark.parametrize("origin", ["direct", "debounced"])
async def test_ready_request_after_application_publishes_current_inputs(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    """A call_soon request between update return and debounce release is retained."""
    entry, head_a, _head_b, _wide, narrow = await _prepare_race(hass, False)
    coord: MXZCoordinator = entry.runtime_data
    original_apply = coord._apply
    release = asyncio.Event()
    entered = asyncio.Event()
    tasks = []
    windows = []
    original_request = coord.async_request_refresh

    async def request():
        windows.append(
            (
                coord._refresh_lock.locked(),
                coord._debounced_refresh._execute_lock.locked(),
            )
        )
        await original_request()

    monkeypatch.setattr(coord, "async_request_refresh", request)
    armed = True

    def launch_change():
        tasks.append(
            hass.async_create_task(_set_band(hass, entry, "number", narrow))
        )

    async def apply(plan, selection_seq):
        nonlocal armed
        await original_apply(plan, selection_seq)
        if armed:
            armed = False
            entered.set()
            await release.wait()
            hass.loop.call_soon(launch_change)

    monkeypatch.setattr(coord, "_apply", apply)
    start = coord.async_refresh if origin == "direct" else coord.async_request_refresh
    first = hass.async_create_task(start())
    await asyncio.wait_for(entered.wait(), 3)
    release.set()
    await asyncio.wait_for(first, 3)
    await hass.async_block_till_done()
    await asyncio.gather(*tasks)
    await _drain_refreshes(hass, coord)

    observed = (
        coord.zones[0].drift,
        _zone0(hass, entry)["drift"],
        hass.states.get(head_a).state,
    )
    assert windows
    if coord._needs_legacy_refresh_guard and origin == "debounced":
        assert (False, True) in windows
        assert windows[-1] == (False, False)
    assert observed == (narrow, narrow, MODE_COOL), (origin, windows, observed)


@pytest.mark.parametrize("origin", ["direct", "debounced"])
async def test_direct_refresh_consumes_redundant_followup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    """A waiting direct refresh replaces, rather than duplicates, a follow-up."""
    entry, head_a, _head_b, wide, narrow = await _prepare_race(hass, False)
    coord: MXZCoordinator = entry.runtime_data
    snapshots = _record_computes(coord, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_apply = coord._apply

    async def apply(plan, selection_seq):
        if len(snapshots) == 1:
            entered.set()
            await release.wait()
        await original_apply(plan, selection_seq)

    monkeypatch.setattr(coord, "_apply", apply)
    start = coord.async_refresh if origin == "direct" else coord.async_request_refresh
    first = hass.async_create_task(start())
    await asyncio.wait_for(entered.wait(), 3)
    await _set_band(hass, entry, "number", narrow)
    second = hass.async_create_task(coord.async_refresh())
    await asyncio.sleep(0)
    assert snapshots == [wide]
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 3)
    await _drain_refreshes(hass, coord)
    assert snapshots == [wide, narrow]
    assert coord._refresh_followup_timer is None
    assert not coord._refresh_pending
    assert hass.states.get(head_a).state == MODE_COOL


@pytest.mark.parametrize("origin", ["direct", "debounced"])
@pytest.mark.parametrize("end", ["cancel", "exception"])
async def test_interrupted_active_refresh_keeps_requested_followup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    end: str,
) -> None:
    """Cancellation and one failed apply preserve one requested retry."""
    entry, head_a, _head_b, wide, narrow = await _prepare_race(hass, False)
    coord: MXZCoordinator = entry.runtime_data
    snapshots = _record_computes(coord, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_apply = coord._apply

    async def apply(plan, selection_seq):
        if len(snapshots) == 1:
            entered.set()
            await release.wait()
            if end == "exception":
                raise RuntimeError("injected apply failure")
        await original_apply(plan, selection_seq)

    monkeypatch.setattr(coord, "_apply", apply)
    start = coord.async_refresh if origin == "direct" else coord.async_request_refresh
    task = hass.async_create_task(start())
    await asyncio.wait_for(entered.wait(), 3)
    for _ in range(12):
        await _set_band(hass, entry, "number", narrow)
    if end == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        await asyncio.wait_for(task, 3)
        assert not coord.last_update_success
    await _drain_refreshes(hass, coord)
    assert snapshots == [wide, narrow]
    assert coord.last_update_success
    assert coord.data["zones"][0]["drift"] == narrow
    assert coord._applying_plan is None
    assert not coord._refresh_lock.locked()
    assert coord._refresh_followup_timer is None
    assert not coord._refresh_pending
    assert hass.states.get(head_a).state == MODE_COOL


@pytest.mark.parametrize("origin", ["direct", "debounced"])
async def test_cancelled_direct_waiter_does_not_consume_followup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    """Cancelling a direct waiter leaves the active window's request intact."""
    entry, _head_a, _head_b, wide, narrow = await _prepare_race(hass, False)
    coord: MXZCoordinator = entry.runtime_data
    snapshots = _record_computes(coord, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_apply = coord._apply

    async def apply(plan, selection_seq):
        if len(snapshots) == 1:
            entered.set()
            await release.wait()
        await original_apply(plan, selection_seq)

    monkeypatch.setattr(coord, "_apply", apply)
    start = coord.async_refresh if origin == "direct" else coord.async_request_refresh
    first = hass.async_create_task(start())
    await asyncio.wait_for(entered.wait(), 3)
    await _set_band(hass, entry, "number", narrow)
    waiter = hass.async_create_task(coord.async_refresh())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await asyncio.wait_for(first, 3)
    await _drain_refreshes(hass, coord)
    assert snapshots == [wide, narrow]


@pytest.mark.parametrize("phase", ["active", "scheduled", "picked"])
@pytest.mark.parametrize("operation", ["unload", "reload"])
async def test_retirement_cancels_completion_followup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    operation: str,
) -> None:
    """An old coordinator neither runs nor rearms a completion follow-up."""
    entry, _head_a, _head_b, wide, narrow = await _prepare_race(hass, False)
    coord: MXZCoordinator = entry.runtime_data
    snapshots = _record_computes(coord, monkeypatch)
    captured = []
    original_later = coordinator_module.async_call_later

    def later(hass, delay, action):
        if delay == 0:
            record = {"action": action, "cancelled": False}
            captured.append(record)

            def cancel():
                record["cancelled"] = True

            return cancel
        return original_later(hass, delay, action)

    monkeypatch.setattr(coordinator_module, "async_call_later", later)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_apply = coord._apply

    async def apply(plan, selection_seq):
        if len(snapshots) == 1:
            entered.set()
            await release.wait()
        await original_apply(plan, selection_seq)

    monkeypatch.setattr(coord, "_apply", apply)
    first = hass.async_create_task(coord.async_refresh())
    await asyncio.wait_for(entered.wait(), 3)
    await _set_band(hass, entry, "number", narrow)
    if phase != "active":
        release.set()
        await asyncio.wait_for(first, 3)
        if coord._needs_legacy_refresh_guard:
            assert len(captured) == 1
    if operation == "unload":
        assert await hass.config_entries.async_unload(entry.entry_id)
    else:
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data is not coord
    if phase == "active":
        release.set()
        await asyncio.wait_for(first, 3)
    if phase == "picked":
        for record in captured:
            record["action"](dt_util.utcnow())
    await hass.async_block_till_done()
    assert coord._retired
    assert not coord._refresh_pending
    assert coord._refresh_followup_timer is None
    assert all(record["cancelled"] for record in captured)
    assert snapshots == [wide]


async def test_modern_debouncer_bypasses_legacy_completion_helpers(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modern HA uses its native owned-lock trailing refresh unchanged."""
    entry, _head_a, _head_b = await _setup_dwell(hass)
    coord: MXZCoordinator = entry.runtime_data
    await _quiesce(hass, coord)
    expected = not hasattr(coord._debounced_refresh, "_execute_lock_owner")
    assert coord._needs_legacy_refresh_guard == expected
    if not expected:

        def forbidden(*args, **kwargs):
            raise AssertionError("modern path entered legacy completion helper")

        monkeypatch.setattr(coord, "_sync_refresh_followup", forbidden)
        monkeypatch.setattr(coord, "_cancel_refresh_followup", forbidden)
        await coord.async_refresh()
        await coord.async_request_refresh()
        await _drain_refreshes(hass, coord)


@pytest.mark.parametrize("origin", ["direct", "debounced"])
async def test_two_active_windows_each_coalesce_once(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    """Bursts in two completed active windows produce one pass per window."""
    entry, _head_a, _head_b, wide, narrow = await _prepare_race(hass, False)
    coord: MXZCoordinator = entry.runtime_data
    snapshots = _record_computes(coord, monkeypatch)
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    original_apply = coord._apply

    async def apply(plan, selection_seq):
        index = len(snapshots) - 1
        if index < 2:
            entered[index].set()
            await release[index].wait()
        await original_apply(plan, selection_seq)

    monkeypatch.setattr(coord, "_apply", apply)
    start = coord.async_refresh if origin == "direct" else coord.async_request_refresh
    first = hass.async_create_task(start())
    await asyncio.wait_for(entered[0].wait(), 3)
    for _ in range(25):
        await _set_band(hass, entry, "number", narrow)
    release[0].set()
    await asyncio.wait_for(first, 3)
    async_fire_time_changed(
        hass,
        dt_util.utcnow()
        + timedelta(seconds=REQUEST_REFRESH_DEFAULT_COOLDOWN + 1),
    )
    await asyncio.wait_for(entered[1].wait(), 3)
    for _ in range(25):
        await _set_band(hass, entry, "number", 2.0)
    release[1].set()
    await hass.async_block_till_done()
    await _drain_refreshes(hass, coord)
    assert snapshots == [wide, narrow, 2.0]
    assert coord.data["zones"][0]["drift"] == 2.0
    assert not coord._refresh_pending
    assert coord._refresh_followup_timer is None


@pytest.mark.parametrize("origin", ["direct", "debounced"])
async def test_plan_state_listener_service_request_stays_current(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    """An ordinary state-change listener retains its completion-time request."""
    from homeassistant.helpers.event import async_track_state_change_event

    entry, head_a, _head_b, _wide, narrow = await _prepare_race(hass, False)
    coord: MXZCoordinator = entry.runtime_data
    tasks = []
    fired = False
    windows = []
    original_request = coord.async_request_refresh

    async def request():
        windows.append(
            (
                coord._refresh_lock.locked(),
                coord._debounced_refresh._execute_lock.locked(),
            )
        )
        await original_request()

    monkeypatch.setattr(coord, "async_request_refresh", request)

    @callback
    def plan_changed(event):
        nonlocal fired
        if fired:
            return
        fired = True
        tasks.append(
            hass.async_create_task(_set_band(hass, entry, "number", narrow))
        )

    unsub = async_track_state_change_event(
        hass, [_eid(hass, entry, "_plan")], plan_changed
    )
    try:
        start = coord.async_refresh if origin == "direct" else coord.async_request_refresh
        await start()
        await hass.async_block_till_done()
        await asyncio.gather(*tasks)
        await _drain_refreshes(hass, coord)
        observed = (
            coord.zones[0].drift,
            _zone0(hass, entry)["drift"],
            hass.states.get(head_a).state,
        )
        assert fired
        assert observed == (narrow, narrow, MODE_COOL), (
            origin,
            windows,
            observed,
        )
    finally:
        unsub()
