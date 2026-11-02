"""Entity ID renames, in place (HA 2027.11) and by remove + re-add (before it).

When a user changes an entity ID, Home Assistant up to 2026.10 removes the
entity and adds it again under the new ID. HA 2026.11 added the lifecycle hook
``Entity.async_entity_id_changed`` and renames entities in place instead:
the entity object stays added, ``self.entity_id`` changes under it, core moves
its own bookkeeping (platform, entity sources, registry tracker, restore
state), the old state is removed and the state is written under the new ID.
Neither ``async_added_to_hass`` nor ``async_will_remove_from_hass`` runs.

Until 2027.11 a custom integration stays on the remove + re-add fallback
unless it implements the hook, and every MXZ class defines
``async_added_to_hass`` without implementing it, so today HA re-adds every
MXZ entity on a rename. The fallback is decided in one place,
``homeassistant.helpers.entity._entity_class_requires_readd``; 2027.11 deletes
it and the branch that calls it, which leaves exactly the in-place branch.
The ``in_place`` path below patches that function to return False, which is
what HA 2027.11 runs. It is collected only where HA has the hook (2026.11 and
later); older HA has no in-place rename to run. The ``readd`` path is HA's
own, unpatched fallback, collected wherever HA still has it.

Both paths assert the same contract for every entity a normal setup creates
(two rooms, one with a dual setpoint, so both target-number variants and the
range numbers are included): the state is written under the new ID only, the
entity is found under the new ID, and every control still acts on its own room
through the new ID — including the lookups MXZ makes itself (the thermostat
resolving its sibling number and switch, the follow-global button tracking
its drift number, the coordinator driving a lockout switch). Nothing logs a
warning or an error, and the renamed values survive a restart.

The in-place path is the one that can catch a 2027.11 break: an add hook that
caches ``self.entity_id`` for a later lookup goes stale when the ID changes in
place, while the fallback re-runs the hook and refreshes the cache.

Two tests also assert that a rename changes nothing else, and the fallback
fails both, so they are strict xfails on the paths where the gap exists:

* gap A, HA before 2026.11 only: restore data is stored under the old entity
  ID, so the re-added target number finds none and re-seeds the room's target
  from the head's setpoint.
* gap B, every HA that re-adds MXZ (until 2027.11): the re-added enable
  switches seed as restored, which marks the rooms as resuming from startup,
  and a run parked by a shared-mode mismatch is re-seeded from the parked head
  and forgotten.

The in-place path has neither: the add hooks do not run again.

The plugin ``tools.pytest_phases`` counts any skip as a failed run, so a path
HA does not have is not collected rather than skipped;
``test_rename_paths_match_this_home_assistant`` fails if the paths collected
ever stop matching the running HA.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from awesomeversion import AwesomeVersion
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity as entity_helper
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state as rs
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mxz_coordinator.const import (
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
    SERVICE_RECOMPUTE,
)
from tests.dwell import expire_dwell
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _recompute,
    _set_temp,
    _setup_mock_heads,
    _user_set_fan,
)
from tests.test_dual_setpoint import _call, _setup, _start, _zone
from tests.test_registry_lifecycle import _restart

HAS_HOOK = hasattr(Entity, "async_entity_id_changed")
# HA before 2026.11 always re-adds; 2026.11 up to 2027.11 re-adds a custom
# integration that has not opted in through this function.
_REQUIRES_READD = getattr(entity_helper, "_entity_class_requires_readd", None)
HAS_READD = not HAS_HOOK or _REQUIRES_READD is not None
PATHS = [p for p, has in (("readd", HAS_READD), ("in_place", HAS_HOOK)) if has]
# HA before 2026.11 stores a removed entity's restore data under its entity ID,
# so an entity re-added under a new ID finds none. 2026.11 keys it by registry
# entry (RestoreStateData.async_get_stored_state).
RESTORE_BY_ENTITY_ID = not hasattr(rs.RestoreStateData, "async_get_stored_state")

# Gaps of the remove + re-add fallback, found by these tests and left for a
# product change (this module changes no product code). Strict: a fix turns
# them into XPASS, which fails until the mark is removed.
GAP_A = pytest.mark.xfail(
    strict=True,
    reason=(
        "Rename gap A: on HA before 2026.11 the re-added target number finds no "
        "restore data under its new ID and re-seeds the room's target from the "
        "head's setpoint"
    ),
)
GAP_B = pytest.mark.xfail(
    strict=True,
    reason=(
        "Rename gap B: the re-added enable switches seed as restored, which marks "
        "every room as resuming from startup, and a run parked by a shared-mode "
        "mismatch is re-seeded from the parked head and forgotten"
    ),
)


def _params(gap: pytest.MarkDecorator, has_gap) -> list:
    """PATHS, with ``gap`` marked on each path ``has_gap(path)`` is true for."""
    return [pytest.param(p, marks=gap) if has_gap(p) else p for p in PATHS]


# Every entity a setup with these two rooms (Living dual, Bedroom single) owns:
# unique ID suffix -> domain. A missing or extra entity fails the test, so a
# new entity type cannot slip past it.
ROOMS = ("primary", "secondary")
EXPECTED = {
    "coordinator_enable": "switch",
    "eco_idle": "switch",
    "heat_lockout": "switch",
    "cool_lockout": "switch",
    "shared_mode": "select",
    "plan": "sensor",
    **{
        f"{room}_{suffix}": domain
        for room in ROOMS
        for suffix, domain in (
            ("target", "number"),
            ("drift", "number"),
            ("drift_follow_global", "button"),
            ("enable", "switch"),
            ("fan_auto", "switch"),
            ("thermostat", "climate"),
        )
    },
    "primary_target_low": "number",
    "primary_target_high": "number",
}


def test_rename_paths_match_this_home_assistant() -> None:
    """The paths collected are the ones this HA has: none is silently dropped."""
    version = AwesomeVersion(HA_VERSION)
    assert HAS_HOOK == (version >= AwesomeVersion("2026.11.0.dev0")), HA_VERSION
    if version < AwesomeVersion("2026.11.0.dev0"):
        assert PATHS == ["readd"]
    elif HAS_READD:
        assert PATHS == ["readd", "in_place"]
    else:
        assert PATHS == ["in_place"]
    assert RESTORE_BY_ENTITY_ID == (version < AwesomeVersion("2026.11.0.dev0"))


class _Spy:
    """Counts the lifecycle calls HA makes on each MXZ entity during a rename."""

    def __init__(self) -> None:
        self.calls: Counter[tuple[str, str]] = Counter()
        self.hook_old_ids: dict[str, str] = {}

    def attach(self, monkeypatch: pytest.MonkeyPatch, entity: Entity, key: str) -> None:
        added = entity.async_added_to_hass
        removed = entity.async_will_remove_from_hass

        async def _added() -> None:
            self.calls["added", key] += 1
            await added()

        async def _removed() -> None:
            self.calls["removed", key] += 1
            await removed()

        monkeypatch.setattr(entity, "async_added_to_hass", _added)
        monkeypatch.setattr(entity, "async_will_remove_from_hass", _removed)
        if HAS_HOOK:
            changed = entity.async_entity_id_changed

            def _changed(old_entity_id: str) -> None:
                self.calls["changed", key] += 1
                self.hook_old_ids[key] = old_entity_id
                changed(old_entity_id)

            monkeypatch.setattr(entity, "async_entity_id_changed", _changed)


def _entity(hass: HomeAssistant, entity_id: str) -> Entity | None:
    component = hass.data["entity_components"][entity_id.split(".", 1)[0]]
    return component.get_entity(entity_id)


def _ids(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    """Unique ID suffix -> current entity ID, for every entity of the entry."""
    prefix = f"{entry.entry_id}_"
    return {
        ent.unique_id.removeprefix(prefix): ent.entity_id
        for ent in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


async def _setup_renamable(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, path: str
) -> tuple[MockConfigEntry, tuple[str, str]]:
    """Both rooms and the coordinator on, Living with a dual setpoint, on ``path``."""
    _use_path(monkeypatch, path)
    entry, heads = await _setup(hass, dual=(0,), temps=(68, 72))
    await _start(hass, entry)
    ids = _ids(hass, entry)
    assert {key: eid.split(".", 1)[0] for key, eid in ids.items()} == EXPECTED
    return entry, heads


def _use_path(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    if path == "in_place" and HAS_READD:
        # HA 2027.11: the fallback and its check are gone, every entity takes
        # the in-place branch of _async_process_registry_update_or_remove.
        monkeypatch.setattr(entity_helper, "_entity_class_requires_readd", lambda cls: False)


async def _rename_all(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, str], dict[str, str], _Spy]:
    """Rename every entity of the entry through the registry, one by one."""
    old = _ids(hass, entry)
    spy = _Spy()
    for key, eid in old.items():
        entity = _entity(hass, eid)
        if HAS_HOOK and HAS_READD:
            # MXZ has not opted in (it does not implement the hook), so this HA
            # re-adds it unless patched: the premise of both paths here.
            assert _REQUIRES_READD(type(entity)), key
        spy.attach(monkeypatch, entity, key)
    registry = er.async_get(hass)
    for eid in old.values():
        domain, object_id = eid.split(".", 1)
        registry.async_update_entity(eid, new_entity_id=f"{domain}.renamed_{object_id}")
        await hass.async_block_till_done()
    new = _ids(hass, entry)
    assert new == {key: f"{eid.split('.', 1)[0]}.renamed_{eid.split('.', 1)[1]}"
                   for key, eid in old.items()}
    return old, new, spy


def _assert_path(path: str, spy: _Spy, old: dict[str, str]) -> None:
    """The rename went through ``path`` for every entity, and only that."""
    for key, old_id in old.items():
        if path == "in_place":
            assert spy.calls["added", key] == 0, key
            assert spy.calls["removed", key] == 0, key
            assert spy.calls["changed", key] == 1, key
            assert spy.hook_old_ids[key] == old_id
        else:
            assert spy.calls["added", key] == 1, key
            assert spy.calls["removed", key] == 1, key
            assert spy.calls["changed", key] == 0, key


def _assert_only_new_ids(
    hass: HomeAssistant, old: dict[str, str], new: dict[str, str]
) -> None:
    for key, old_id in old.items():
        new_id = new[key]
        assert hass.states.get(old_id) is None, old_id
        assert _entity(hass, old_id) is None, old_id
        assert hass.states.get(new_id) is not None, new_id
        assert _entity(hass, new_id).entity_id == new_id


def _problems(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        f"{r.levelname} {r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize("path", PATHS)
async def test_rename_keeps_every_entity_working(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: str,
) -> None:
    """Rename every MXZ entity, then drive each through its new ID."""
    entry, (head_a, head_b) = await _setup_renamable(hass, monkeypatch, path)
    coordinator = entry.runtime_data
    living, bedroom = _zone(entry, 0), _zone(entry, 1)
    caplog.clear()

    old, new, spy = await _rename_all(hass, entry, monkeypatch)

    _assert_path(path, spy, old)
    _assert_only_new_ids(hass, old, new)

    # A single target, set on the number and through the room's thermostat,
    # which resolves its sibling number by unique ID.
    await _call(hass, "number", "set_value", {"entity_id": new["secondary_target"], "value": 66})
    assert bedroom.target == 66
    assert float(hass.states.get(new["secondary_target"]).state) == 66
    await _call(hass, "climate", "set_temperature",
                {"entity_id": new["secondary_thermostat"], "temperature": 67})
    assert bedroom.target == 67
    assert float(hass.states.get(new["secondary_target"]).state) == 67

    # A range, set on one edge and through the thermostat; both edges redraw.
    low, high = living.target_low, living.target_high
    await _call(hass, "number", "set_value",
                {"entity_id": new["primary_target_low"], "value": low - 2})
    assert (living.target_low, living.target_high) == (low - 2, high)
    assert float(hass.states.get(new["primary_target_low"]).state) == low - 2
    await _call(hass, "climate", "set_temperature",
                {"entity_id": new["primary_thermostat"],
                 "target_temp_low": 64, "target_temp_high": 76})
    assert (living.target_low, living.target_high) == (64, 76)
    assert float(hass.states.get(new["primary_target_low"]).state) == 64
    assert float(hass.states.get(new["primary_target_high"]).state) == 76
    assert float(hass.states.get(new["primary_target"]).state) == 70
    assert bedroom.target == 67

    # A drift override, and the button that hands it back. The button tracks
    # the drift number's state by unique ID: a change written under the old ID
    # is not its owner's, one under the new ID is.
    await _call(hass, "number", "set_value", {"entity_id": new["secondary_drift"], "value": 3})
    assert bedroom.drift == 3
    assert living.drift is None
    assert hass.states.get(new["secondary_drift"]).attributes["override"] is True
    button = new["secondary_drift_follow_global"]
    assert hass.states.get(button).state != STATE_UNAVAILABLE
    hass.states.async_set(old["secondary_drift"], STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get(button).state != STATE_UNAVAILABLE
    hass.states.async_remove(old["secondary_drift"])
    drift_state = hass.states.get(new["secondary_drift"])
    hass.states.async_set(new["secondary_drift"], STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get(button).state == STATE_UNAVAILABLE
    hass.states.async_set(new["secondary_drift"], drift_state.state, drift_state.attributes)
    await hass.async_block_till_done()
    assert hass.states.get(button).state != STATE_UNAVAILABLE
    await _call(hass, "button", "press", {"entity_id": button})
    assert bedroom.drift is None
    assert hass.states.get(new["secondary_drift"]).attributes["override"] is False

    # A room's enable, through the thermostat (sibling switch by unique ID)
    # and on the switch itself; the other room is untouched.
    await _call(hass, "climate", "set_hvac_mode",
                {"entity_id": new["secondary_thermostat"], "hvac_mode": "off"})
    assert bedroom.enable is False
    assert living.enable is True
    assert hass.states.get(new["secondary_enable"]).state == "off"
    await _call(hass, "switch", "turn_on", {"entity_id": new["secondary_enable"]})
    assert bedroom.enable is True
    assert hass.states.get(new["secondary_enable"]).state == "on"

    # Fan auto: the switch acts on its own head, and redraws on the head's
    # changes (a state tracker keyed on the head, not on its own ID).
    await _call(hass, "switch", "turn_off", {"entity_id": new["secondary_fan_auto"]})
    assert coordinator.fan_auto_is_on(head_b) is False
    assert coordinator.fan_auto_is_on(head_a) is True
    assert hass.states.get(new["secondary_fan_auto"]).state == "off"
    await _call(hass, "switch", "turn_on", {"entity_id": new["secondary_fan_auto"]})
    assert coordinator.fan_auto_is_on(head_b) is True
    assert hass.states.get(new["secondary_fan_auto"]).state == "on"
    await _user_set_fan(hass, head_a, "low")
    assert hass.states.get(new["primary_thermostat"]).attributes["fan_mode"] == "low"

    # Global switches: the kill-switch by hand, a lockout by the coordinator,
    # which looks the switch up by unique ID.
    await _call(hass, "switch", "turn_off", {"entity_id": new["coordinator_enable"]})
    assert coordinator.coordinator_enable is False
    await _call(hass, "switch", "turn_on", {"entity_id": new["coordinator_enable"]})
    assert coordinator.coordinator_enable is True
    await coordinator._drive_lockout("heat_lockout", True)
    await hass.async_block_till_done()
    assert coordinator.heat_lockout is True
    assert hass.states.get(new["heat_lockout"]).state == "on"
    await coordinator._drive_lockout("heat_lockout", False)
    await hass.async_block_till_done()
    assert coordinator.heat_lockout is False

    # Shared mode, picked by hand and moved by the coordinator.
    await _call(hass, "select", "select_option",
                {"entity_id": new["shared_mode"], "option": "heat"})
    assert coordinator.current_shared_mode == "heat"
    coordinator.current_shared_mode = "cool"
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(new["shared_mode"]).state == "cool"

    # The domain service recomputes (a debounced request), and the plan
    # publishes under its new ID.
    await _call(hass, DOMAIN, SERVICE_RECOMPUTE, {})
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)
    )
    await hass.async_block_till_done()
    plan = hass.states.get(new["plan"])
    assert plan.state in ("cool", "heat")
    assert plan.attributes["zones"][1]["target"] == 67
    # The thermostats redraw on that refresh (a coordinator listener).
    assert hass.states.get(new["secondary_thermostat"]).attributes["temperature"] == 67
    assert hass.states.get(new["primary_thermostat"]).attributes["target_temp_low"] == 64

    _assert_only_new_ids(hass, old, new)
    assert _problems(caplog) == []


@pytest.mark.parametrize(
    "path", _params(GAP_A, lambda p: p == "readd" and RESTORE_BY_ENTITY_ID)
)
async def test_rename_keeps_every_value(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: str,
) -> None:
    """A rename changes the ID and nothing else: every state, every room value."""
    entry, _ = await _setup_renamable(hass, monkeypatch, path)
    coordinator = entry.runtime_data

    def rooms() -> list[tuple]:
        return [(z.target, z.target_low, z.target_high, z.drift, z.enable)
                for z in coordinator.zones]

    before = {key: hass.states.get(eid) for key, eid in _ids(hass, entry).items()}
    rooms_before = rooms()
    caplog.clear()

    old, new, spy = await _rename_all(hass, entry, monkeypatch)

    _assert_path(path, spy, old)
    _assert_only_new_ids(hass, old, new)
    assert rooms() == rooms_before
    for key, state in before.items():
        after = hass.states.get(new[key])
        assert (after.state, dict(after.attributes)) == (
            state.state, dict(state.attributes)
        ), key
    assert _problems(caplog) == []


@pytest.mark.parametrize("path", _params(GAP_B, lambda p: p == "readd"))
async def test_rename_keeps_a_parked_run(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: str,
) -> None:
    """A run parked by a shared-mode flip still resumes after a rename.

    The scenario of test_drive.test_engage_latch_survives_shared_mode_flip,
    with every entity renamed while the primary room's cool run is parked.
    """
    _use_path(monkeypatch, path)
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 67)
    await _set_temp(hass, SENSOR_B, 63)
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
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ids = _ids(hass, entry)
    for key in ("primary_enable", "secondary_enable", "coordinator_enable"):
        await _call(hass, "switch", "turn_on", {"entity_id": ids[key]})
    for key in ("primary_target", "secondary_target"):
        await _call(hass, "number", "set_value", {"entity_id": ids[key], "value": 63})
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 63.5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"  # mid-run, latched
    await _set_temp(hass, SENSOR_B, 55)
    expire_dwell(entry.runtime_data)
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == "heat"
    assert hass.states.get(head_a).state == "fan_only"  # parked
    assert hass.states.get(ids["plan"]).attributes["primary_engage"] == "cool"
    caplog.clear()

    old, new, spy = await _rename_all(hass, entry, monkeypatch)
    _assert_path(path, spy, old)

    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"
    assert hass.states.get(new["plan"]).attributes["primary_engage"] == "cool"
    # The mode returns: the primary room, still inside its deadband, resumes
    # its run, and ends it at the target.
    await _set_temp(hass, SENSOR_B, 67)
    expire_dwell(entry.runtime_data)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    await _set_temp(hass, SENSOR_A, 63.0)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"
    _assert_only_new_ids(hass, old, new)
    assert _problems(caplog) == []


@pytest.mark.parametrize("path", PATHS)
async def test_renamed_values_survive_a_restart(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: str,
) -> None:
    """What is set under the new IDs is stored under them and restored."""
    entry, (_, head_b) = await _setup_renamable(hass, monkeypatch, path)
    caplog.clear()
    old, new, spy = await _rename_all(hass, entry, monkeypatch)
    _assert_path(path, spy, old)

    await _call(hass, "number", "set_value", {"entity_id": new["secondary_target"], "value": 66})
    await _call(hass, "number", "set_value", {"entity_id": new["secondary_drift"], "value": 3})
    await _call(hass, "climate", "set_temperature",
                {"entity_id": new["primary_thermostat"],
                 "target_temp_low": 64, "target_temp_high": 76})
    await _call(hass, "switch", "turn_off", {"entity_id": new["secondary_enable"]})
    await _call(hass, "switch", "turn_off", {"entity_id": new["secondary_fan_auto"]})
    await _call(hass, "select", "select_option",
                {"entity_id": new["shared_mode"], "option": "heat"})

    await _restart(hass, entry)

    assert _ids(hass, entry) == new
    _assert_only_new_ids(hass, old, new)
    coordinator = entry.runtime_data
    living, bedroom = _zone(entry, 0), _zone(entry, 1)
    assert bedroom.target == 66
    assert bedroom.drift == 3
    assert (living.target_low, living.target_high) == (64, 76)
    assert bedroom.enable is False
    assert living.enable is True
    assert coordinator.fan_auto_is_on(head_b) is False
    assert coordinator.current_shared_mode == "heat"
    assert coordinator.coordinator_enable is True
    assert _problems(caplog) == []
