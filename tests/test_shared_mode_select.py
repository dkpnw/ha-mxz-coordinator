"""Shared-mode selector: offered choices, restore, manual precedence, migration.

The selector used to offer four options. Two of them were never directions the
plan could run: arbitration resolves anything that is not cool|heat to cool
(``coordinator._compute``), so ``fan_only`` and ``off`` left the coordinator
running in cool — which could park a head that had been heating — and, on the
enabled non-parked apply path, wrote cool back over the selection. These tests
pin the two supported directions, the migration of the two dropped values, and
the fact that no choice here ever meant "stop the system" — the kill-switch and
the standby hold do that.

Requires pytest-homeassistant-custom-component.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    async_mock_restore_state_shutdown_restart,
    mock_integration,
    mock_platform,
    mock_restore_cache,
)

from custom_components.mxz_coordinator import select as mxz_select
from custom_components.mxz_coordinator.const import (
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)
from custom_components.mxz_coordinator.logic import shared_mode

SENSOR_A = "sensor.room_a_temp"
SENSOR_B = "sensor.room_b_temp"

# The full option list this selector offered up to and including v3.3.0.
LEGACY_OPTIONS = ["cool", "heat", "fan_only", "off"]

# The Repairs issue key is part of the user-visible contract, so it is written
# out here rather than imported: renaming it in the module must fail a test, not
# quietly rename the expectation too.
ISSUE_LEGACY_OPTION = "shared_mode_legacy_option"


class MockHead(ClimateEntity):
    """A minimal dual-setpoint head that records what it's told."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
    ]
    _attr_fan_modes: ClassVar[list[str]] = ["auto", "low", "medium", "high", "quiet"]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, suffix: str) -> None:
        self._attr_unique_id = f"mock_head_{suffix}"
        self._attr_name = f"Mock Head {suffix}"
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature_low = None
        self._attr_target_temperature_high = None
        self._attr_fan_mode = "auto"

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._attr_fan_mode = fan_mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = mode
        if (low := kwargs.get("target_temp_low")) is not None:
            self._attr_target_temperature_low = low
        if (high := kwargs.get("target_temp_high")) is not None:
            self._attr_target_temperature_high = high
        self.async_write_ha_state()


async def _setup_mock_heads(hass: HomeAssistant) -> tuple[str, str]:
    """Register two mock climate heads and return their entity_ids."""
    heads = [MockHead("a"), MockHead("b")]

    async def _async_setup_platform(
        hass, config, async_add_entities, discovery_info=None
    ):
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(
        hass, "test.climate", MockPlatform(async_setup_platform=_async_setup_platform)
    )
    assert await async_setup_component(
        hass, "climate", {"climate": {"platform": "test"}}
    )
    await hass.async_block_till_done()
    return heads[0].entity_id, heads[1].entity_id


def _eid(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str:
    """Resolve an mxz entity_id by its unique_id suffix."""
    reg = er.async_get(hass)
    for ent in reg.entities.values():
        if ent.config_entry_id == entry.entry_id and ent.unique_id.endswith(suffix):
            return ent.entity_id
    raise AssertionError(f"no mxz entity ending in {suffix}")


async def _set_temp(hass: HomeAssistant, entity_id: str, value: float) -> None:
    hass.states.async_set(entity_id, str(value))
    await hass.async_block_till_done()


async def _recompute(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Force a deterministic compute+apply (production path is debounced)."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def _entry(hass: HomeAssistant, head_a: str, head_b: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)  # created FIRST -> a restore injected after is fresh
    return entry


async def _start(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _enable_all(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()


async def _select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": option},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _base(hass: HomeAssistant) -> tuple[MockConfigEntry, str, str, str]:
    """Two heads, both rooms neutral at 70 °F, entry set up but nothing enabled."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = _entry(hass, head_a, head_b)
    await _start(hass, entry)
    return entry, head_a, head_b, _eid(hass, entry, "_shared_mode")


# ---------------------------------------------------------------------------
# UI state: which choices the selector offers, and what it currently reads.
# ---------------------------------------------------------------------------
async def test_selector_offers_only_the_two_shared_directions(
    hass: HomeAssistant,
) -> None:
    """cool and heat are the whole list; fan_only and off are gone."""
    entry, _, _, sel = await _base(hass)

    state = hass.states.get(sel)
    assert state.attributes["options"] == ["cool", "heat"]
    assert state.state == "cool"  # cold start
    assert entry.runtime_data.current_shared_mode == "cool"


async def test_dropped_options_are_rejected_by_home_assistant(
    hass: HomeAssistant,
) -> None:
    """select.select_option refuses fan_only/off and changes nothing.

    Independent oracle: HA's own option validation, not our code path.
    """
    entry, _, _, sel = await _base(hass)

    for gone in ("fan_only", "off"):
        with pytest.raises(ServiceValidationError):
            await _select(hass, sel, gone)
        assert hass.states.get(sel).state == "cool"
        assert entry.runtime_data.current_shared_mode == "cool"


# ---------------------------------------------------------------------------
# Restore: across a config-entry reload, and across a storage-backed restart.
# ---------------------------------------------------------------------------
async def test_manual_choice_survives_an_entry_reload(hass: HomeAssistant) -> None:
    """Reload rebuilds the coordinator; the chosen direction comes back."""
    entry, _, _, sel = await _base(hass)
    await _select(hass, sel, "heat")
    assert hass.states.get(sel).state == "heat"

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    sel = _eid(hass, entry, "_shared_mode")
    assert hass.states.get(sel).state == "heat"
    assert entry.runtime_data.current_shared_mode == "heat"


async def test_manual_choice_survives_a_storage_backed_restart(
    hass: HomeAssistant,
) -> None:
    """The choice goes out through the real restore-state store and comes back.

    ``async_mock_restore_state_shutdown_restart`` runs HA's own shutdown dump,
    so the value is serialized by RestoreStateData rather than injected. The
    storage layer underneath is the test harness's, not a physical disk.
    """
    entry, _head_a, _head_b, sel = await _base(hass)
    await _select(hass, sel, "heat")

    await async_mock_restore_state_shutdown_restart(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _start(hass, entry)

    sel = _eid(hass, entry, "_shared_mode")
    assert hass.states.get(sel).state == "heat"
    assert entry.runtime_data.current_shared_mode == "heat"


# ---------------------------------------------------------------------------
# Manual selection precedence within the existing mode-flip dwell.
# ---------------------------------------------------------------------------
async def test_manual_choice_beats_automatic_arbitration_for_the_dwell(
    hass: HomeAssistant,
) -> None:
    """A person picks heat while a room is demanding cool: heat runs.

    Bounded honestly: the request is handled by the pre-existing arbitration,
    which holds the chosen direction for the mode-flip dwell. Durable manual
    authority is not implemented or asserted here, and this test says nothing
    about what happens once the dwell elapses.
    """
    entry, head_a, _head_b, sel = await _base(hass)
    await _enable_all(hass, entry)

    await _set_temp(hass, SENSOR_A, 75)  # hot room -> demands cool
    await _recompute(hass, entry)
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "cool"
    assert hass.states.get(head_a).state == "cool"

    await _select(hass, sel, "heat")
    await _recompute(hass, entry)
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "heat"
    assert hass.states.get(sel).state == "heat"
    # The room that wanted cool loses and parks. It is never sent the opposite
    # conditioning, and it does not keep cooling against the chosen direction.
    assert hass.states.get(head_a).state == "fan_only"


async def test_no_time_based_release_of_a_selection_is_introduced(
    hass: HomeAssistant,
) -> None:
    """Negative control: this change gives a selection no clock of its own.

    A selection is a request, handled by the arbitration that was already
    there. Two things are asserted, neither of them a rule invented here:
    re-picking the direction already showing stamps nothing (no renewal), and
    once the dwell elapses the plan is whatever ``logic.shared_mode`` returns
    for the same demands — computed here independently instead of hard-coded,
    so a release (or retention) rule of the selector's own would fail it.

    That post-dwell flip is the pre-existing behaviour for a changing
    selection. It is recorded here as pre-existing, not endorsed as durable
    manual authority; persistence until an explicit resume is not implemented
    or asserted here.
    """
    entry, _head_a, _head_b, sel = await _base(hass)
    await _enable_all(hass, entry)
    coord = entry.runtime_data
    # The oracle below passes resting=None, which is what _compute derives from
    # this default; state the assumption instead of assuming it.
    assert coord.resting_mode_bias not in ("cool", "heat")

    await _set_temp(hass, SENSOR_A, 75)  # hot room -> standing cool demand
    await _recompute(hass, entry)
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "cool"

    await _select(hass, sel, "heat")
    await _recompute(hass, entry)
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "heat"
    stamped = coord._last_mode_change_ts

    # No renewal: re-picking the direction already showing buys no clock.
    await _select(hass, sel, "heat")
    await _recompute(hass, entry)
    assert coord._last_mode_change_ts == stamped
    assert hass.states.get(sel).state == "heat"

    # Dwell elapsed: the answer is arbitration's own, on the same demands.
    coord._last_mode_change_ts = 0.0
    expected = shared_mode(
        demands=[coord.data[f"{zone.slug}_demand"] for zone in coord.zones],
        current="heat",
        allowed=True,
        resting=None,
    )
    assert expected == "cool", "the standing cool demand must make this non-trivial"
    await _recompute(hass, entry)
    assert hass.states.get(_eid(hass, entry, "_plan")).state == expected


async def test_a_newer_selection_survives_an_older_in_flight_apply(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person selects while an automatic apply is awaiting a head service.

    No coordinator decision and no service outcome is replaced: the fake head's own
    ``async_set_temperature`` is held open until the selection has landed, then
    released, so the older automatic plan finishes and reaches its writeback
    after the newer request. That writeback must not undo the request.
    """
    entry, head_a, _head_b, sel = await _base(hass)
    await _enable_all(hass, entry)
    coord = entry.runtime_data
    # Move the input directly: a sensor write would start its own debounced
    # refresh before the barrier is in place.
    coord.zones[0].target = 60  # room A at 70 -> automatic cool

    entered, release = asyncio.Event(), asyncio.Event()
    original = MockHead.async_set_temperature
    blocked = False

    async def delayed(self: MockHead, **kwargs: Any) -> None:
        nonlocal blocked
        if self.entity_id == head_a and not blocked:
            blocked = True
            entered.set()
            await release.wait()
        await original(self, **kwargs)

    monkeypatch.setattr(MockHead, "async_set_temperature", delayed)

    refresh = hass.async_create_task(coord.async_refresh())
    selection = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Not async_block_till_done: this barrier deliberately holds a task.
        selection = hass.async_create_task(
            hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": sel, "option": "heat"},
                blocking=True,
            )
        )
        for _ in range(100):
            await asyncio.sleep(0)
            if coord.current_shared_mode == "heat":
                break
        assert coord.current_shared_mode == "heat"
        assert hass.states.get(sel).state == "heat"
    finally:
        release.set()
        await asyncio.wait_for(refresh, 5)
        if selection is not None:
            await asyncio.wait_for(selection, 5)
    await hass.async_block_till_done()

    assert coord.current_shared_mode == "heat", (
        "an older automatic apply erased the newer explicit selection"
    )
    assert hass.states.get(sel).state == "heat"
    # And the next refresh computes from the request, not from the older plan.
    await _recompute(hass, entry)
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "heat"


async def test_choice_does_not_enable_the_coordinator_or_a_disabled_room(
    hass: HomeAssistant,
) -> None:
    """The choice picks a direction. It starts nothing."""
    entry, head_a, head_b, sel = await _base(hass)
    # Everything defaults OFF: kill-switch and both rooms.
    await _set_temp(hass, SENSOR_A, 75)
    await _select(hass, sel, "heat")
    await _recompute(hass, entry)

    assert hass.states.get(_eid(hass, entry, "_coordinator_enable")).state == "off"
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "off"
    assert hass.states.get(head_a).state == "off"  # never commanded
    assert hass.states.get(head_b).state == "off"

    # Now with only the kill-switch on, the disabled rooms stay out of it.
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _eid(hass, entry, "_coordinator_enable")},
        blocking=True,
    )
    await _select(hass, sel, "heat")
    await _recompute(hass, entry)
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "off"
    assert hass.states.get(head_a).state != "heat"  # a disabled room is not woken


# ---------------------------------------------------------------------------
# Migration of each legacy stored value.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stored", ["off", "fan_only"])
async def test_legacy_stored_value_migrates_to_cool_and_explains_itself(
    hass: HomeAssistant, stored: str
) -> None:
    """Each dropped value comes back as the cool it was already running as."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = _entry(hass, head_a, head_b)
    mock_restore_cache(
        hass, [State("select.mxz_coordinator_shared_mode", stored)]
    )
    await _start(hass, entry)

    sel = _eid(hass, entry, "_shared_mode")
    assert hass.states.get(sel).state == "cool"
    assert entry.runtime_data.current_shared_mode == "cool"

    assert mxz_select.ISSUE_LEGACY_OPTION == ISSUE_LEGACY_OPTION
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{entry.entry_id}_{ISSUE_LEGACY_OPTION}"
    )
    assert issue is not None
    assert issue.translation_placeholders == {"stored": stored, "current": "cool"}


@pytest.mark.parametrize("stored", ["cool", "heat"])
async def test_supported_stored_value_restores_untouched_and_silently(
    hass: HomeAssistant, stored: str
) -> None:
    """Negative control for the migration: it fires only for the dropped values."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = _entry(hass, head_a, head_b)
    mock_restore_cache(
        hass, [State("select.mxz_coordinator_shared_mode", stored)]
    )
    await _start(hass, entry)

    assert hass.states.get(_eid(hass, entry, "_shared_mode")).state == stored
    assert entry.runtime_data.current_shared_mode == stored
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{entry.entry_id}_{ISSUE_LEGACY_OPTION}"
        )
        is None
    )


async def test_migration_creates_no_hold(hass: HomeAssistant) -> None:
    """The migrated value is an ordinary cool, not a pinned one.

    A hold would show up as the selector refusing to follow the coordinator, so
    this drives a real automatic flip and asserts the selector moves with it.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = _entry(hass, head_a, head_b)
    mock_restore_cache(hass, [State("select.mxz_coordinator_shared_mode", "off")])
    await _start(hass, entry)
    await _enable_all(hass, entry)

    sel = _eid(hass, entry, "_shared_mode")
    await _set_temp(hass, SENSOR_B, 60)  # a genuine heat demand
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)

    assert hass.states.get(_eid(hass, entry, "_plan")).state == "heat"
    assert hass.states.get(sel).state == "heat"  # the migrated value did not pin
    assert entry.runtime_data.current_shared_mode == "heat"


# ---------------------------------------------------------------------------
# Negative control: no whole-system-OFF semantics were invented.
# ---------------------------------------------------------------------------
async def test_no_whole_system_off_semantics_exist_on_this_selector(
    hass: HomeAssistant,
) -> None:
    """Nothing on this selector stops the system — before or after migration.

    A stored ``off`` is the strongest case: if the selector had ever meant
    "stop", an entry restarting on it would come back with the heads parked.
    It comes back coordinating instead. The controls that really stop or hold
    are asserted here so the claim is not just an absence.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = _entry(hass, head_a, head_b)
    mock_restore_cache(hass, [State("select.mxz_coordinator_shared_mode", "off")])
    await _start(hass, entry)
    await _enable_all(hass, entry)

    await _set_temp(hass, SENSOR_A, 75)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"  # coordinating, not stopped
    assert hass.states.get(_eid(hass, entry, "_plan")).state == "cool"
    assert hass.states.get(_eid(hass, entry, "_coordinator_enable")).state == "on"

    # The kill-switch is what stops the coordinator: the head keeps whatever it
    # was last given and is never written again.
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _eid(hass, entry, "_coordinator_enable")},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": head_a, "hvac_mode": "off"},
        blocking=True,
    )
    await _set_temp(hass, SENSOR_A, 80)  # would normally command cool
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"  # left alone

    # And a room's own enable switch is what takes one room out.
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _eid(hass, entry, "_coordinator_enable")},
        blocking=True,
    )
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _eid(hass, entry, "_primary_enable")},
        blocking=True,
    )
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state != "cool"  # disabled room is not driven


async def test_legacy_option_list_is_no_longer_advertised(
    hass: HomeAssistant,
) -> None:
    """Regression pin on the exact list, so a re-addition cannot pass quietly."""
    _, _, _, sel = await _base(hass)
    options = hass.states.get(sel).attributes["options"]
    assert options == ["cool", "heat"]
    assert [gone for gone in LEGACY_OPTIONS if gone not in options] == [
        "fan_only",
        "off",
    ]
