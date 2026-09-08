"""Vane-kick tests: applying a vane change while a head is OFF briefly runs the
head in fan_only so the louvre physically moves, then hands it back to the plan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.select import SelectEntity
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, callback
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ACTIVE_STATE,
    CONF_INHIBIT_ENTITY,
    CONF_ZONES,
    DOMAIN,
    INHIBIT_ACTION_OFF,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
    ZONE_VANE_VERTICAL,
)

from .test_drive import MockHead, _eid, _set_temp

VANE_OPTIONS = ["AUTO", "↑↑", "↑", "—", "↓", "↓↓", "SWING"]

# How long a blocked phase may take to report back before the test gives up.
# Bounded so an assertion failure fails the test instead of hanging the suite.
STEP_TIMEOUT = 10.0


class MockVane(SelectEntity):
    """A vane select that records every option it is commanded to."""

    _attr_should_poll = False
    _attr_options = VANE_OPTIONS

    def __init__(self, suffix: str = "a") -> None:
        self._attr_unique_id = f"mock_vane_{suffix}"
        self._attr_name = f"Mock Vane {suffix.upper()}"
        self._attr_current_option = "AUTO"
        self.history: list[str] = []

    async def async_select_option(self, option: str) -> None:
        self.history.append(option)
        self._attr_current_option = option
        self.async_write_ha_state()


async def _setup(
    hass: HomeAssistant,
    *,
    extra_data: dict[str, Any] | None = None,
    second_vane: bool = False,
):
    """Two mock heads + one vane select, zone 0 wired to the vane."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [MockHead("a"), MockHead("b")]
    vane = MockVane()
    vane_b = MockVane("b") if second_vane else None

    async def _climate(hass, config, async_add_entities, discovery_info=None):
        async_add_entities(heads)

    async def _select(hass, config, async_add_entities, discovery_info=None):
        async_add_entities([vane, *([vane_b] if vane_b is not None else [])])

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=_climate))
    mock_platform(hass, "test.select", MockPlatform(async_setup_platform=_select))
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    assert await async_setup_component(hass, "select", {"select": {"platform": "test"}})
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="MXZ Coordinator",
        data={
            **(extra_data or {}),
            CONF_ZONES: [
                {
                    ZONE_NAME: "A",
                    ZONE_CLIMATE: heads[0].entity_id,
                    ZONE_SENSOR: "sensor.room_a_temp",
                    ZONE_VANE_VERTICAL: vane.entity_id,
                },
                {
                    ZONE_NAME: "B",
                    ZONE_CLIMATE: heads[1].entity_id,
                    ZONE_SENSOR: "sensor.room_b_temp",
                    **(
                        {ZONE_VANE_VERTICAL: vane_b.entity_id}
                        if vane_b is not None
                        else {}
                    ),
                },
            ]
        },
    )
    entry.add_to_hass(hass)
    await _set_temp(hass, "sensor.room_a_temp", 70)
    await _set_temp(hass, "sensor.room_b_temp", 70)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # no real sleeping in tests
    entry.runtime_data._vane_kick_spinup = 0
    entry.runtime_data._vane_kick_apply = 0
    if vane_b is not None:
        return entry, heads, vane, vane_b
    return entry, heads, vane


async def _enable_all(hass, entry):
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()


async def _park_head_a_off(hass, entry, heads) -> None:
    """Eco-idle both rooms so head A is parked off and a vane change kicks."""
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_eco_idle")}, blocking=True
    )
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(heads[0].entity_id).state == "off"


async def _bounded(awaitable, what: str) -> Any:
    """Await with a cap, so a broken invariant fails loudly instead of hanging."""
    try:
        return await asyncio.wait_for(asyncio.ensure_future(awaitable), STEP_TIMEOUT)
    except TimeoutError:
        raise AssertionError(f"timed out after {STEP_TIMEOUT}s waiting for {what}") from None


class _CancellationBarrier:
    """Suspend one awaited kick step, and defer the first cancellation.

    ``Task.cancel()`` is a request, not a stop — a real climate/select handler
    in another integration may swallow or defer it. Deferring it here forces
    the ownership guards, rather than lucky prompt cancellation, to be the
    thing that prevents the next head or vane command.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancel_requested = asyncio.Event()
        self.release = asyncio.Event()
        self.armed = True

    async def wait(self) -> None:
        self.armed = False  # one pause per test, on the phase under test
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_requested.set()
            await self.release.wait()


class _KickHarness:
    """Recorded commands, kick tasks and the barrier for one blocked kick."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.kick_tasks: list[asyncio.Task[None]] = []
        self.in_kick = 0
        self.barrier = _CancellationBarrier()
        # Snapshot of ``commands`` taken each time retirement returns, so a
        # command can be attributed to the retiring path rather than to
        # whatever the coordinator happens to do afterwards.
        self.at_retirement: list[list[tuple[str, str]]] = []

    @property
    def kick_task(self) -> asyncio.Task[None]:
        assert self.kick_tasks, "no vane-kick task was created"
        return self.kick_tasks[0]


# Commands the head/vane must have received when each awaited phase is paused.
_EXPECTED_AT_BARRIER = {
    "wake": [("head", "fan_only")],
    "spinup": [("head", "fan_only")],
    "select": [("head", "fan_only"), ("vane", "SWING")],
    "settle": [("head", "fan_only"), ("vane", "SWING")],
    "restore": [("head", "fan_only"), ("vane", "SWING"), ("head", "off")],
    "refresh": [("head", "fan_only"), ("vane", "SWING"), ("head", "off")],
}
# Phases paused with the head still running in the fan_only the kick commanded.
# Retirement owes exactly one more command there: park it again.
_OWES_RESTORE = ("wake", "spinup", "select", "settle")


def _expected_after_retirement(phase: str) -> list[tuple[str, str]]:
    restore = [("head", "off")] if phase in _OWES_RESTORE else []
    return _EXPECTED_AT_BARRIER[phase] + restore


async def _start_blocked_kick(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    head: MockHead,
    vane: MockVane,
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> _KickHarness:
    """Start a kick through the real facade and pause one awaited phase.

    Head and vane commands are recorded unconditionally, inside Home
    Assistant's own entity-service task — which is a *different* task from the
    kick's. Only the two awaits that really do run in the kick task (the spinup
    and settle sleeps, and the closing refresh) are keyed off task identity.
    """
    coord = entry.runtime_data
    harness = _KickHarness()
    barrier = harness.barrier
    sleep_count = 0
    # Released come what may: an assertion that fires before the test's own
    # release must fail with its message, not deadlock teardown on a parked
    # kick. Runs before the hass fixture tears the loop down.
    request.addfinalizer(barrier.release.set)

    real_kick = coord._vane_kick

    async def _kick(*args: Any) -> None:
        harness.kick_tasks.append(asyncio.current_task())
        harness.in_kick += 1
        try:
            await real_kick(*args)
        finally:
            harness.in_kick -= 1

    monkeypatch.setattr(coord, "_vane_kick", _kick)

    real_retire = coord._async_retire_vane_kicks

    async def _retire_kicks() -> None:
        await real_retire()
        harness.at_retirement.append(list(harness.commands))

    monkeypatch.setattr(coord, "_async_retire_vane_kicks", _retire_kicks)

    real_head = head.async_set_hvac_mode

    async def _head(hvac_mode) -> None:
        await real_head(hvac_mode)
        harness.commands.append(("head", str(hvac_mode)))
        if not (harness.in_kick and barrier.armed):
            return
        if (phase == "wake" and str(hvac_mode) == "fan_only") or (
            phase == "restore" and str(hvac_mode) == "off"
        ):
            await barrier.wait()

    monkeypatch.setattr(head, "async_set_hvac_mode", _head)

    real_vane = vane.async_select_option

    async def _vane(option: str) -> None:
        await real_vane(option)
        harness.commands.append(("vane", option))
        if phase == "select" and harness.in_kick and barrier.armed:
            await barrier.wait()

    monkeypatch.setattr(vane, "async_select_option", _vane)

    real_sleep = asyncio.sleep

    async def _sleep(*args: Any, **kwargs: Any) -> Any:
        nonlocal sleep_count
        if harness.kick_tasks and asyncio.current_task() is harness.kick_tasks[0]:
            sleep_count += 1
            if barrier.armed and (
                (phase == "spinup" and sleep_count == 1)
                or (phase == "settle" and sleep_count == 2)
            ):
                await barrier.wait()
                return None
        return await real_sleep(*args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _sleep)

    real_refresh = coord.async_request_refresh

    async def _refresh(*args: Any, **kwargs: Any) -> Any:
        if (
            phase == "refresh"
            and barrier.armed
            and harness.kick_tasks
            and asyncio.current_task() is harness.kick_tasks[0]
        ):
            await barrier.wait()
        return await real_refresh(*args, **kwargs)

    monkeypatch.setattr(coord, "async_request_refresh", _refresh)

    # The production climate facade owns vane routing. The service returns once
    # it has registered the background kick task; the barrier owns later timing.
    await hass.services.async_call(
        "climate",
        "set_swing_mode",
        {
            "entity_id": _eid(hass, entry, "_primary_thermostat"),
            "swing_mode": "SWING",
        },
        blocking=True,
    )
    await _bounded(barrier.entered.wait(), f"the kick to reach the {phase} phase")
    assert harness.kick_tasks, "the facade did not register a kick task"
    assert harness.commands == _EXPECTED_AT_BARRIER[phase]
    return harness


async def _retire(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> asyncio.Task[Any]:
    """Trigger a production retirement path and return its owning task."""
    if action == "disable":
        return hass.async_create_task(
            hass.services.async_call(
                "switch",
                "turn_off",
                {"entity_id": _eid(hass, entry, "_coordinator_enable")},
                blocking=True,
            )
        )
    if action == "unload":
        return hass.async_create_task(
            hass.config_entries.async_unload(entry.entry_id)
        )
    if action == "reload":
        return hass.async_create_task(
            hass.config_entries.async_reload(entry.entry_id)
        )

    coord = entry.runtime_data
    scheduled = asyncio.Event()
    inhibit_task: asyncio.Task[Any] | None = None
    real_create_task = hass.async_create_task

    def _create_task(
        target: Coroutine[Any, Any, Any], *args: Any, **kwargs: Any
    ) -> asyncio.Task[Any]:
        nonlocal inhibit_task
        task = real_create_task(target, *args, **kwargs)
        if getattr(target, "cr_code", None) is getattr(
            coord._evaluate_inhibit, "__code__", None
        ):
            inhibit_task = task
            scheduled.set()
        return task

    monkeypatch.setattr(hass, "async_create_task", _create_task)
    hass.states.async_set("binary_sensor.grid_hold", "on")
    await _bounded(scheduled.wait(), "the inhibit re-evaluation to be scheduled")
    assert inhibit_task is not None
    return inhibit_task


async def _setup_kickable(
    hass: HomeAssistant, *, inhibitable: bool = False, second_vane: bool = False
):
    """Coordinator enabled, eco on, head A parked off — ready for a vane kick."""
    if inhibitable:
        hass.states.async_set("binary_sensor.grid_hold", "off")
    setup = await _setup(
        hass,
        extra_data={
            CONF_INHIBIT_ENTITY: "binary_sensor.grid_hold",
            CONF_INHIBIT_ACTIVE_STATE: "on",
            CONF_INHIBIT_ACTION: INHIBIT_ACTION_OFF,
        }
        if inhibitable
        else None,
        second_vane=second_vane,
    )
    entry, heads = setup[0], setup[1]
    await _enable_all(hass, entry)
    await _park_head_a_off(hass, entry, heads)
    return setup


@pytest.mark.parametrize("phase", tuple(_EXPECTED_AT_BARRIER))
@pytest.mark.parametrize("action", ("disable", "inhibit", "unload", "reload"))
async def test_retirement_stops_every_awaited_kick_phase(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    action: str,
    phase: str,
) -> None:
    """Given a kick paused at one awaited phase, when the coordinator is
    disabled/inhibited/unloaded/reloaded, then the kick is disowned before it
    can command anything else, the head it woke is parked by the retiring path
    before retirement completes, and no task is left behind."""
    entry, heads, vane = await _setup_kickable(hass, inhibitable=True)
    coord = entry.runtime_data
    head_a = heads[0].entity_id

    service_history: list[dict[str, Any]] = []
    hass.bus.async_listen(
        EVENT_CALL_SERVICE,
        callback(lambda event: service_history.append(dict(event.data))),
    )
    harness = await _start_blocked_kick(
        hass, entry, heads[0], vane, phase, monkeypatch, request
    )
    barrier, kick_task = harness.barrier, harness.kick_task
    assert coord._vane_kicks.get(head_a) is kick_task

    retire_task = await _retire(hass, entry, action, monkeypatch)
    await _bounded(
        barrier.cancel_requested.wait(), "retirement to cancel the live kick"
    )

    # Cancellation was requested and deferred: the kick coroutine is still
    # alive, and retirement is still waiting for it. Ownership is already gone,
    # so the guards — not the cancellation — must stop the next command.
    assert not kick_task.done()
    assert not retire_task.done(), "retirement finished without waiting for the kick"
    assert head_a not in coord._vane_kicks
    assert harness.commands == _EXPECTED_AT_BARRIER[phase]

    barrier.release.set()
    await _bounded(retire_task, f"the {action} path to finish")

    # Exactly one retirement ran, and what it left behind is measured at the
    # moment it returned — not after later plan activity could mask it.
    assert harness.at_retirement == [_expected_after_retirement(phase)]
    assert kick_task.done()
    assert head_a not in coord._vane_kicks
    assert coord._vane_kick_woken == set()
    assert coord._vane_pending == {}

    settled = len(harness.commands)
    await hass.async_block_till_done()
    assert ("head", "fan_only") not in harness.commands[settled:], (
        "the head was woken again after retirement"
    )
    assert hass.states.get(head_a).state == "off"
    assert all(task.done() for task in harness.kick_tasks)

    if action in ("unload", "reload"):
        assert coord._retired is True
        before = len(service_history)
        await coord.async_apply_vane(head_a, vane.entity_id, "AUTO")
        await hass.async_block_till_done()
        assert len(service_history) == before, "a retired coordinator still commanded"
        assert coord._vane_kicks == {}


@pytest.mark.parametrize("action", ("disable", "inhibit", "unload", "reload"))
async def test_retirement_does_not_overwrite_a_later_manual_choice(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    action: str,
) -> None:
    """Given a paused kick whose head someone has since set to heat by hand,
    when the coordinator retires, then it parks nothing — the manual choice
    stands and the head is never forced back to off."""
    entry, heads, vane = await _setup_kickable(hass, inhibitable=True)
    coord = entry.runtime_data
    head_a = heads[0].entity_id

    harness = await _start_blocked_kick(
        hass, entry, heads[0], vane, "spinup", monkeypatch, request
    )
    # An explicit, later choice on the same head, taken directly on the device.
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": head_a, "hvac_mode": "heat"},
        blocking=True,
    )
    assert hass.states.get(head_a).state == "heat"
    taken_over = list(harness.commands)

    retire_task = await _retire(hass, entry, action, monkeypatch)
    await _bounded(
        harness.barrier.cancel_requested.wait(), "retirement to cancel the kick"
    )
    harness.barrier.release.set()
    await _bounded(retire_task, f"the {action} path to finish")

    assert harness.at_retirement == [taken_over], "retirement overrode a manual choice"
    assert coord._vane_kick_woken == set()
    assert coord._vane_kicks == {}
    assert harness.kick_task.done()

    await hass.async_block_till_done()
    if action in ("disable", "unload"):
        # Both leave this coordinator silent afterwards, so the manual choice
        # is still standing at the end of the run.
        assert harness.commands == taken_over
        assert hass.states.get(head_a).state == "heat"
    else:
        # The standby hold (inhibit) and the fresh incarnation (reload) take the
        # head back on their own documented terms. All this test claims is that
        # no command came from retiring the kick.
        assert harness.commands[: len(taken_over)] == taken_over


async def test_retirement_is_bounded_when_a_handler_defers_cancellation(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    caplog,
) -> None:
    """Given a climate handler that swallows cancellation and never returns,
    when the entry is unloaded, then the unload still completes within the
    retirement bound, the disowned kick commands nothing more, and it is
    reported rather than silently leaked."""
    entry, heads, vane = await _setup_kickable(hass)
    coord = entry.runtime_data
    head_a = heads[0].entity_id

    harness = await _start_blocked_kick(
        hass, entry, heads[0], vane, "wake", monkeypatch, request
    )
    coord._vane_kick_retire = 0.05

    with caplog.at_level(logging.WARNING, logger="custom_components.mxz_coordinator"):
        retire_task = await _retire(hass, entry, "unload", monkeypatch)
        # Never released: the handler defers cancellation for the whole unload.
        await _bounded(retire_task, "a bounded unload past a stuck kick handler")

    assert harness.at_retirement == [_expected_after_retirement("wake")]
    assert not harness.kick_task.done(), "this test is only honest while it is stuck"
    assert "did not unwind" in caplog.text
    assert head_a not in coord._vane_kicks
    assert coord._retired is True
    assert hass.states.get(head_a).state == "off"

    # And the stuck task still commands nothing once it finally unwinds.
    settled = len(harness.commands)
    harness.barrier.release.set()
    await _bounded(harness.kick_task, "the deferred kick to unwind")
    await hass.async_block_till_done()
    assert harness.commands[settled:] == []
    assert coord._vane_kicks == {}
    assert coord._vane_kick_woken == set()


async def test_a_disowned_kick_stays_silent_when_the_coordinator_returns(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Given a kick that outlived its bounded retirement, when the kill switch
    is turned back on and the stuck step finally returns, then the superseded
    kick commands nothing — the registry entry, not the enable flag, is what
    says a task still owns a head."""
    entry, heads, vane = await _setup_kickable(hass)
    coord = entry.runtime_data
    head_a = heads[0].entity_id

    harness = await _start_blocked_kick(
        hass, entry, heads[0], vane, "spinup", monkeypatch, request
    )
    coord._vane_kick_retire = 0.05
    retire_task = await _retire(hass, entry, "disable", monkeypatch)
    await _bounded(retire_task, "a bounded disable past a stuck kick")
    assert not harness.kick_task.done(), "this test is only honest while it is stuck"
    assert head_a not in coord._vane_kicks

    # The coordinator comes back before the old kick has unwound, so none of
    # the retired/enabled/inhibited flags can stand in for ownership any more.
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _eid(hass, entry, "_coordinator_enable")},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert coord.coordinator_enable is True
    assert coord._retired is False
    assert coord.inhibited is False

    settled = len(harness.commands)
    harness.barrier.release.set()
    await _bounded(harness.kick_task, "the stuck kick to unwind")
    await hass.async_block_till_done()

    assert vane.history == [], "a superseded kick commanded the vane"
    assert harness.commands[settled:] == [], "a superseded kick commanded the head"
    assert coord._vane_kicks == {}
    assert coord._vane_kick_woken == set()


async def test_retired_coordinator_commands_neither_head_nor_vane(
    hass: HomeAssistant,
) -> None:
    """Given an unloaded entry, when a stale reference asks it to apply a vane
    option — with the head parked or running — then it writes nothing at all,
    including the best-effort select a live coordinator would send."""
    entry, heads, vane = await _setup_kickable(hass)
    coord = entry.runtime_data
    head_a = heads[0].entity_id

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coord._retired is True

    history: list[dict[str, Any]] = []
    hass.bus.async_listen(
        EVENT_CALL_SERVICE, callback(lambda event: history.append(dict(event.data)))
    )
    # Parked head: the path that would otherwise start a kick.
    await coord.async_apply_vane(head_a, vane.entity_id, "SWING")
    await hass.async_block_till_done()
    # Running head: the path that would otherwise send a best-effort select.
    hass.states.async_set(head_a, "cool")
    await coord.async_apply_vane(head_a, vane.entity_id, "AUTO")
    await hass.async_block_till_done()

    assert history == []
    assert vane.history == []
    assert coord._vane_kicks == {}
    assert coord._vane_kick_woken == set()
    assert coord._vane_pending == {}


def _head_snapshot(hass: HomeAssistant, entity_id: str) -> tuple[Any, ...]:
    """Everything about a head this coordinator can write, in one value."""
    state = hass.states.get(entity_id)
    return (
        state.state,
        state.attributes.get("target_temp_low"),
        state.attributes.get("target_temp_high"),
        state.attributes.get("fan_mode"),
    )


def _recorded_calls(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Start recording every service call; the returned list fills in place."""
    history: list[dict[str, Any]] = []
    hass.bus.async_listen(
        EVENT_CALL_SERVICE, callback(lambda event: history.append(dict(event.data)))
    )
    return history


async def _retained_apply(
    hass: HomeAssistant, entry: MockConfigEntry, heads: list[MockHead]
) -> Coroutine[Any, Any, None]:
    """Build an unstarted ``_apply`` coroutine, then let its plan go stale.

    This is a deliberate entry-boundary probe, not a reproduction of a
    production interleaving. The only production caller awaits ``_apply``
    directly (``_async_update_data``, coordinator.py:672), and everything
    between ``_compute`` and that await is synchronous, so an ordinary refresh
    never leaves an apply suspended in front of the guard. Here the coroutine
    object is created by hand and never started, retained across a later
    refresh and across retirement, then invoked through that stale reference —
    the direct entry the top-of-``_apply`` guard exists to refuse. Whether
    ordinary HA dispatch can reach this boundary is not claimed or tested.

    Room A is cooled back to its target after the coroutine is built, so the
    live coordinator parks the head in ``fan_only`` and the retained plan's
    ``cool`` is BOTH stale and divergent — invoking it is a real head command,
    not an idempotent skip. The control test below proves that.
    """
    coord = entry.runtime_data
    await _set_temp(hass, "sensor.room_a_temp", 78)
    await coord.async_refresh()  # production path is debounced
    await hass.async_block_till_done()
    assert hass.states.get(heads[0].entity_id).state == "cool"

    plan = coord._compute()
    assert plan["state"] == "cool"
    assert plan["primary_engage"] == "cool"
    # Not awaited: this only constructs the coroutine object. Its body — and
    # so the retirement guard at the top of it — does not run until a caller
    # below schedules this reference.
    pending = coord._apply(plan, coord._selection_seq)

    await _set_temp(hass, "sensor.room_a_temp", 70)
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(heads[0].entity_id).state == "fan_only"
    return pending


async def test_retired_entry_applies_nothing_when_a_pending_apply_lands(
    hass: HomeAssistant,
) -> None:
    """Given an unstarted ``_apply`` coroutine built before the entry was
    unloaded, when it is invoked directly through that stale reference after
    retirement, then that incarnation writes nothing: no head or vane service
    call, no state change, no timer left armed."""
    entry, heads, vane = await _setup(hass)
    await _enable_all(hass, entry)
    coord = entry.runtime_data
    pending = await _retained_apply(hass, entry, heads)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coord._retired is True

    before = [_head_snapshot(hass, head.entity_id) for head in heads]
    history = _recorded_calls(hass)
    await _bounded(pending, "the retained _apply to return after unload")
    await hass.async_block_till_done()

    assert history == []
    assert vane.history == []
    assert [_head_snapshot(hass, head.entity_id) for head in heads] == before
    assert coord._heal_timers == {}
    assert coord._dry_timers == {}
    assert coord._dwell_timer is None


async def test_retired_entry_applies_nothing_after_a_reload(
    hass: HomeAssistant,
) -> None:
    """Same direct entry across a reload: the fresh incarnation owns the heads,
    and the retired one's stale ``_apply`` reference must not reach past it to
    command them."""
    entry, heads, vane = await _setup(hass)
    await _enable_all(hass, entry)
    coord = entry.runtime_data
    pending = await _retained_apply(hass, entry, heads)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert coord._retired is True
    assert entry.runtime_data is not coord  # a live successor is driving now

    # Measured from after the successor has settled, so what this asserts is
    # the retired coordinator's own silence, not the reload's quiet.
    before = [_head_snapshot(hass, head.entity_id) for head in heads]
    history = _recorded_calls(hass)
    await _bounded(pending, "the retained _apply to return after reload")
    await hass.async_block_till_done()

    assert history == []
    assert vane.history == []
    assert [_head_snapshot(hass, head.entity_id) for head in heads] == before
    assert coord._heal_timers == {}
    assert coord._dry_timers == {}
    assert coord._dwell_timer is None


async def test_a_live_coordinator_still_applies_a_retained_plan(
    hass: HomeAssistant,
) -> None:
    """Symmetric control: the identical retained ``_apply``, on a coordinator
    that was never retired, really does command the head — so the two tests
    above measure retirement rather than an inert probe."""
    entry, heads, _vane = await _setup(hass)
    await _enable_all(hass, entry)
    coord = entry.runtime_data
    pending = await _retained_apply(hass, entry, heads)

    assert coord._retired is False
    history = _recorded_calls(hass)
    await _bounded(pending, "the retained _apply on a live coordinator")
    await hass.async_block_till_done()

    assert hass.states.get(heads[0].entity_id).state == "cool"
    assert (
        "climate",
        "set_temperature",
        heads[0].entity_id,
    ) in [
        (call["domain"], call["service"], call["service_data"].get("entity_id"))
        for call in history
    ]


async def test_vane_kick_wakes_off_head(hass: HomeAssistant) -> None:
    """Eco/away (head off) + a swing change -> fan_only kick, vane applied, back off."""
    entry, heads, vane = await _setup(hass)
    await _enable_all(hass, entry)
    # eco on -> both rooms within the extremes -> heads OFF
    await _park_head_a_off(hass, entry, heads)

    # change swing via the native tile
    await hass.services.async_call(
        "climate",
        "set_swing_mode",
        {"entity_id": _eid(hass, entry, "_primary_thermostat"), "swing_mode": "—"},
        blocking=True,
    )
    await hass.async_block_till_done()

    # the vane was commanded while the head was awake, and the head is off again
    assert vane.history == ["—"]
    assert vane.current_option == "—"
    assert hass.states.get(heads[0].entity_id).state == "off"
    assert entry.runtime_data._vane_kicks == {}
    assert entry.runtime_data._vane_kick_woken == set()
    assert entry.runtime_data._vane_pending == {}


async def test_vane_applies_directly_on_running_head(hass: HomeAssistant) -> None:
    """A running head gets the select write live — no mode change."""
    entry, heads, vane = await _setup(hass)
    await _enable_all(hass, entry)
    # room A hot -> head A actively cooling
    await _set_temp(hass, "sensor.room_a_temp", 75)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(heads[0].entity_id).state == "cool"

    await hass.services.async_call(
        "climate",
        "set_swing_mode",
        {"entity_id": _eid(hass, entry, "_primary_thermostat"), "swing_mode": "SWING"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert vane.history == ["SWING"]
    assert hass.states.get(heads[0].entity_id).state == "cool"  # untouched
    assert entry.runtime_data._vane_kick_woken == set()  # nothing was woken


async def test_vane_kick_respects_kill_switch(hass: HomeAssistant) -> None:
    """Kill-switch off: the head is never touched; best-effort select write only."""
    entry, heads, vane = await _setup(hass)
    # zones enabled but the coordinator disabled; head stays wherever it is (off)
    for suffix in ("_primary_enable", "_secondary_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    assert hass.states.get(heads[0].entity_id).state == "off"

    await hass.services.async_call(
        "climate",
        "set_swing_mode",
        {"entity_id": _eid(hass, entry, "_primary_thermostat"), "swing_mode": "↓"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert vane.history == ["↓"]  # select still written (best effort)
    assert hass.states.get(heads[0].entity_id).state == "off"  # never woken
    assert entry.runtime_data._vane_kicks == {}
    assert entry.runtime_data._vane_kick_woken == set()


async def test_parallel_kicks_keep_per_zone_ownership(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """One delayed zone neither absorbs nor cleans up another zone's kick."""
    entry, heads, vane_a, vane_b = await _setup_kickable(hass, second_vane=True)
    coord = entry.runtime_data

    harness = await _start_blocked_kick(
        hass, entry, heads[0], vane_a, "spinup", monkeypatch, request
    )
    created: list[asyncio.Task[Any]] = []
    real_create_task = hass.async_create_task

    def _capture_task(
        target: Coroutine[Any, Any, Any], *args: Any, **kwargs: Any
    ) -> asyncio.Task[Any]:
        task = real_create_task(target, *args, **kwargs)
        if getattr(target, "cr_code", None) is getattr(
            coord._vane_kick, "__code__", None
        ):
            created.append(task)
        return task

    monkeypatch.setattr(hass, "async_create_task", _capture_task)
    await hass.services.async_call(
        "climate",
        "set_swing_mode",
        {
            "entity_id": _eid(hass, entry, "_secondary_thermostat"),
            "swing_mode": "↓",
        },
        blocking=True,
    )
    assert len(created) == 1
    task_b = created[0]
    await _bounded(task_b, "zone B's kick to finish while zone A is paused")

    # Zone B ran to completion and cleaned up only itself.
    assert vane_b.history == ["↓"]
    assert hass.states.get(heads[1].entity_id).state == "off"
    assert heads[1].entity_id not in coord._vane_kicks
    assert heads[1].entity_id not in coord._vane_kick_woken
    # Zone A is untouched: still owned, still paused, still awake for its kick.
    assert coord._vane_kicks[heads[0].entity_id] is harness.kick_task
    assert heads[0].entity_id in coord._vane_kick_woken
    assert harness.commands == _EXPECTED_AT_BARRIER["spinup"]

    harness.barrier.release.set()
    await _bounded(harness.kick_task, "zone A's kick to finish")
    await hass.async_block_till_done()
    assert vane_a.history == ["SWING"]
    assert harness.commands == [("head", "fan_only"), ("vane", "SWING"), ("head", "off")]
    assert coord._vane_kicks == {}
    assert coord._vane_kick_woken == set()


async def test_kick_preserves_explicit_manual_fan_owner(
    hass: HomeAssistant,
) -> None:
    """A kick moves only the head mode/vane and does not steal a manual fan hold."""
    entry, heads, vane = await _setup(
        hass, extra_data={CONF_FAN_BOOST_ENABLE: True}
    )
    await _enable_all(hass, entry)
    await _set_temp(hass, "sensor.room_a_temp", 75)
    await entry.runtime_data.async_refresh()
    await hass.services.async_call(
        "climate",
        "set_fan_mode",
        {"entity_id": heads[0].entity_id, "fan_mode": "middle"},
        blocking=True,
    )
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data._fan_latched[heads[0].entity_id] is True

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _eid(hass, entry, "_eco_idle")},
        blocking=True,
    )
    await entry.runtime_data.async_refresh()
    assert hass.states.get(heads[0].entity_id).state == "off"

    await hass.services.async_call(
        "climate",
        "set_swing_mode",
        {
            "entity_id": _eid(hass, entry, "_primary_thermostat"),
            "swing_mode": "↑",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert vane.history == ["↑"]
    assert hass.states.get(heads[0].entity_id).state == "off"
    assert hass.states.get(heads[0].entity_id).attributes["fan_mode"] == "middle"
    assert entry.runtime_data._fan_latched[heads[0].entity_id] is True
    assert entry.runtime_data._vane_kicks == {}
