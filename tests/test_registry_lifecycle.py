"""Registry identity and restored holds across a reload and a restart.

A room's entities are the handles a household actually builds on: a renamed
"Bedroom target", an area assignment, a drift number a user deliberately
disabled, and the Fan auto hold that says who is driving the fan. None of
those may be spent by an ordinary reload or restart.

Two mechanisms are under test here, and they fail differently:

* The setup-time prune removes every registry record this entry no longer
  provides. A per-zone entity missing from its allowlist is removed and
  recreated on EVERY setup. On HA 2026.x a deleted registry entry carries the
  customizations back, so the loss is invisible; on HA 2024.12 the deleted
  entry keeps only entity_id/unique_id/platform/config_entry_id, so the name,
  the area and the disabled flag are gone. ``_removals`` below is the
  version-independent oracle: a retained room's record must never be removed
  in the first place.
* The Fan auto switch restores one bool, held or not. When the head is missing
  from the state machine at shutdown the switch is unavailable, HA persists
  ``unavailable``, and a state-only restore has no answer to give. The bool
  travels in RestoreEntity extra data instead, which is stored beside the
  state whatever the state says.

"Restart" here means a real store round trip: restore state is dumped and
re-read through the mocked ``core.restore_state`` store, the entry is unloaded
and set up again with a fresh coordinator, and the entity registry is flushed
to ``core.entity_registry`` and read back as JSON. It is not a new Home
Assistant process, and it is not hardware.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state as rs
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_restore_state_shutdown_restart,
    flush_store,
)

from custom_components.mxz_coordinator.const import (
    CONF_ZONES,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
)
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_fan_auto,
    _set_target,
    _set_temp,
    _setup_fan_boost,
    _setup_mock_heads,
    _user_set_fan,
)

# Every entity a retained room owns. A room that survives a reload keeps all
# five records; miss one and that room's customizations are spent silently.
ROOM_SUFFIXES = (
    "_primary_target",
    "_primary_drift",
    "_primary_enable",
    "_primary_fan_auto",
    "_primary_thermostat",
)


def _removals(hass: HomeAssistant) -> list[str]:
    """Collect entity-registry removals from here on (the direct oracle)."""
    removed: list[str] = []

    @callback
    def _record(event: Event) -> None:
        if event.data["action"] == "remove":
            removed.append(event.data["entity_id"])

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _record)
    return removed


async def _customize(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Rename and file every room record, and disable one by hand."""
    area = ar.async_get(hass).async_get_or_create("Upstairs")
    reg = er.async_get(hass)
    for suffix in ROOM_SUFFIXES:
        reg.async_update_entity(
            _eid(hass, entry, suffix), name=f"Custom{suffix}", area_id=area.id
        )
    # A disabled record is the customization HA cannot reconstruct: recreate it
    # and the entity the user switched off comes back on.
    reg.async_update_entity(
        _eid(hass, entry, "_primary_drift"),
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    await hass.async_block_till_done()
    return {s: reg.async_get(_eid(hass, entry, s)) for s in ROOM_SUFFIXES}


def _assert_records_intact(
    hass: HomeAssistant, entry: MockConfigEntry, before: dict, note: str
) -> None:
    reg = er.async_get(hass)
    for suffix, was in before.items():
        now = reg.async_get(_eid(hass, entry, suffix))
        assert now is not None, f"{note}: {suffix} record is gone"
        assert now.entity_id == was.entity_id, f"{note}: {suffix} entity_id moved"
        assert now.id == was.id, f"{note}: {suffix} got a new registry id"
        assert now.unique_id == was.unique_id, f"{note}: {suffix} unique_id changed"
        assert now.name == was.name, f"{note}: {suffix} lost its custom name"
        assert now.area_id == was.area_id, f"{note}: {suffix} lost its area"
        assert now.disabled_by == was.disabled_by, (
            f"{note}: {suffix} lost its disabled flag"
        )


async def _restart(
    hass: HomeAssistant, entry: MockConfigEntry, *, head: str | None = None
) -> None:
    """Shut down through the restore store, then start the entry again.

    ``head`` names a head to take out of the state machine before the dump and
    put back after it, which is what a head integration that is unloaded (or
    still loading) at shutdown looks like to us.
    """
    kept = hass.states.get(head) if head else None
    if head:
        hass.states.async_remove(head)
        await hass.async_block_till_done()
    await async_mock_restore_state_shutdown_restart(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    if kept is not None:
        hass.states.async_set(head, kept.state, dict(kept.attributes))
        await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _fan_hold(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    return plan.attributes["zones"][0]["fan_hold"]


async def _held_room(hass: HomeAssistant) -> tuple[str, MockConfigEntry]:
    """Two heads, the primary cooling and held by hand at 'high'.

    The room then warms until 'high' is exactly the rung the ladder would pick
    anyway. That is the case the restored bool exists for: the token alone
    cannot tell this hold from boost's own speed, so a restart that loses the
    bool adopts the hold as boost's and hands the fan back to the machine.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    await _set_temp(hass, SENSOR_A, 65)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 63.5)  # closing in: boost parks at 'medium'
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"
    await _user_set_fan(hass, head_a, "high")  # a departure: the user's hold
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 67)  # the room warms; 'high' is now the rung
    await _recompute(hass, entry)
    assert _fan_hold(hass, entry) is True
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    assert hass.states.get(_eid(hass, entry, "_primary_fan_auto")).state == "off"
    return head_a, entry


# -- reload ----------------------------------------------------------------


async def test_reload_keeps_every_retained_room_record(hass: HomeAssistant) -> None:
    """A reload must not remove and recreate a retained room's entities."""
    _head_a, entry = await _held_room(hass)
    before = await _customize(hass, entry)

    removed = _removals(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert removed == [], f"a retained room's records were removed: {removed}"
    _assert_records_intact(hass, entry, before, "after reload")


async def test_reload_keeps_the_target_and_the_drift_override(
    hass: HomeAssistant,
) -> None:
    """Control: the values behind those records survive a reload too."""
    _head_a, entry = await _held_room(hass)
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 68)
    drift = _eid(hass, entry, "_primary_drift")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": drift, "value": 4.0}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(drift).attributes["override"] is True

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert float(hass.states.get(_eid(hass, entry, "_primary_target")).state) == 68.0
    after = hass.states.get(_eid(hass, entry, "_primary_drift"))
    assert float(after.state) == 4.0
    assert after.attributes["override"] is True
    assert _fan_hold(hass, entry) is True, "the hold must survive a reload"


# -- restart ---------------------------------------------------------------


async def test_restart_keeps_records_hold_and_values(hass: HomeAssistant) -> None:
    """A store round trip keeps the records, the target and the hold."""
    head_a, entry = await _held_room(hass)
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 68)
    before = await _customize(hass, entry)

    removed = _removals(hass)
    await _restart(hass, entry)

    assert removed == [], f"a retained room's records were removed: {removed}"
    _assert_records_intact(hass, entry, before, "after restart")
    await _recompute(hass, entry)
    assert float(hass.states.get(_eid(hass, entry, "_primary_target")).state) == 68.0
    assert _fan_hold(hass, entry) is True, "the hold must survive a restart"
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"


async def test_restart_keeps_the_records_on_disk(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """The customized records are still in the written registry JSON."""
    _head_a, entry = await _held_room(hass)
    before = await _customize(hass, entry)
    await _restart(hass, entry)

    reg = er.async_get(hass)
    reg.async_schedule_save()
    await flush_store(reg._store)
    stored = {
        item["unique_id"]: item
        for item in hass_storage[er.STORAGE_KEY]["data"]["entities"]
    }
    for suffix, was in before.items():
        assert was.unique_id in stored, f"{suffix} is missing from the stored registry"
        item = stored[was.unique_id]
        assert item["id"] == was.id
        assert item["entity_id"] == was.entity_id
        assert item["name"] == was.name
        assert item["area_id"] == was.area_id
    assert stored[before["_primary_drift"].unique_id]["disabled_by"] == "user"


async def test_hold_survives_a_head_absent_at_shutdown(hass: HomeAssistant) -> None:
    """The reproduction: an unavailable switch still carries its hold home."""
    head_a, entry = await _held_room(hass)

    await _restart(hass, entry, head=head_a)

    await _recompute(hass, entry)
    assert _fan_hold(hass, entry) is True, (
        "a hold placed before the restart was lost because the head was missing "
        "from the state machine at shutdown"
    )
    assert hass.states.get(_eid(hass, entry, "_primary_fan_auto")).state == "off"
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"


# -- negatives and controls ------------------------------------------------


async def test_a_head_still_absent_after_restart_stays_unavailable(
    hass: HomeAssistant,
) -> None:
    """Negative: restoring the bool must not fake availability."""
    head_a, entry = await _held_room(hass)
    hass.states.async_remove(head_a)
    await hass.async_block_till_done()
    await async_mock_restore_state_shutdown_restart(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        hass.states.get(_eid(hass, entry, "_primary_fan_auto")).state == "unavailable"
    )


async def test_a_released_hold_is_never_resurrected(hass: HomeAssistant) -> None:
    """Negative: handing the fan back before shutdown must stay handed back."""
    head_a, entry = await _held_room(hass)
    await _set_fan_auto(hass, _eid(hass, entry, "_primary_fan_auto"), True)
    await _recompute(hass, entry)
    assert _fan_hold(hass, entry) is False

    await _restart(hass, entry, head=head_a)

    await _recompute(hass, entry)
    assert _fan_hold(hass, entry) is False, "a released hold came back"
    assert hass.states.get(_eid(hass, entry, "_primary_fan_auto")).state == "on"


@pytest.mark.parametrize("stale", [True, False])
async def test_a_previous_incarnation_is_still_ignored(
    hass: HomeAssistant, stale: bool
) -> None:
    """Negative: #7 stands — restore data older than the entry is not ours.

    The mock restore cache is written in the shape storage actually loads:
    an ``unavailable`` state (the head was gone at shutdown) carrying the hold
    in extra data. Older than the entry it must be dropped whole; newer than
    the entry, the very same record must seed the latch — so the control
    proves the filter, not an inert path.
    """
    head_a, entry = await _held_room(hass)
    switch_id = _eid(hass, entry, "_primary_fan_auto")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    stamp = entry.created_at + timedelta(days=-3 if stale else 3)
    rs.async_get(hass).last_states = {
        switch_id: rs.StoredState(
            State(switch_id, "unavailable", last_updated=stamp),
            rs.RestoredExtraData({"held": True}),
            stamp,
        )
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    expected = {} if stale else {head_a: True}
    assert entry.runtime_data._fan_restore == expected, (
        "a restore older than this entry incarnation must never seed the latch"
    )


async def test_a_dropped_room_is_pruned_and_stays_gone(hass: HomeAssistant) -> None:
    """Control: retaining records must not keep a removed room alive."""
    head_a, entry = await _held_room(hass)
    reg = er.async_get(hass)
    doomed = {
        suffix: _eid(hass, entry, suffix)
        for suffix in ("_secondary_target", "_secondary_drift", "_secondary_enable")
    }

    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(
        entry,
        data={
            CONF_ZONES: [
                {
                    ZONE_NAME: "Primary",
                    ZONE_CLIMATE: head_a,
                    ZONE_SENSOR: SENSOR_A,
                }
            ]
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for suffix, eid in doomed.items():
        assert reg.async_get(eid) is None, f"dropped room kept {suffix}"
    assert reg.async_get(_eid(hass, entry, "_primary_drift")) is not None

    # And a second setup does not bring it back.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for eid in doomed.values():
        assert reg.async_get(eid) is None
