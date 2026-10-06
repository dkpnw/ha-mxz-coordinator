"""Identical invented issue-25 schedules for the two immutable product bases.

Run explicitly; this diagnostic is not part of the ordinary tests/ suite.
No helpers or expected values are imported from either product's tests.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import json
from time import monotonic
from typing import ClassVar
from unittest.mock import patch

import pytest
from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers import entity_registry as er, restore_state as rs
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    async_mock_restore_state_shutdown_restart,
    mock_integration,
    mock_platform,
)

SCHEDULES = (
    "01-clean-twice",
    "02-unavailable-twice",
    "03-manual-twin",
    "04-options-reload",
    "05-missing-restore-negative",
    "06-changed-active-demand",
    "07-provisional-auto-old-echo",
    "08-missing-speed-recovery",
)
SENSORS = ("sensor.invented_a", "sensor.invented_b")


class OwnershipFailure(AssertionError):
    """A completed schedule violated the independent ownership oracle."""


def trace(event, **values):
    print("ISSUE25 " + json.dumps({"event": event, **values}, default=str, sort_keys=True))


def completed_fan(calls, start, token):
    """An earlier handler barrier cannot prove delivery after this intervention."""
    old_gates = {id(c[key]) for c in calls[:start] for key in ("entered", "returned")}
    return any(
        c["number"] == index and c["service"] == "fan" and c["args"] == {"fan_mode": token}
        and c["entered"] is not c["returned"]
        and id(c["entered"]) not in old_gates and id(c["returned"]) not in old_gates
        and c["entered"].is_set() and c["returned"].is_set()
        and c.get("returned_at", -1) >= c["at"]
        for index, c in enumerate(calls[start:], start=start)
    )


def require_state(state, expected, label):
    """An absent/unavailable prerequisite must stop, never become ownership-red."""
    assert state is not None and state.state == expected, f"UNKNOWN: {label} unreached"


def require_cooling(head, plan, room):
    require_state(head, "cool", "changed-demand B cooling")
    require_state(plan, "cool", "changed-demand shared cooling plan")
    zones = plan.attributes.get("zones")
    assert isinstance(zones, list) and len(zones) > room, "UNKNOWN: cooling plan room missing"
    assert zones[room].get("engage") == "cool", "UNKNOWN: B cooling engagement unreached"


def require_restore_record(entity, loaded, record, created_at):
    """Check a real loaded record against the serialized shutdown evidence."""
    assert loaded is not None, f"UNKNOWN: no loaded restore record for {entity}"
    assert loaded.state.entity_id == entity == record["state"]["entity_id"], (
        "UNKNOWN: restore entity mismatch")
    stamp = datetime.fromisoformat(record["state"]["last_updated"])
    assert stamp >= created_at, "UNKNOWN: prior-incarnation store"
    assert loaded.state.state == record["state"]["state"], "UNKNOWN: serialized/reloaded mismatch"
    assert loaded.state.last_updated == stamp, "UNKNOWN: restored timestamp changed"
    assert (loaded.extra_data.as_dict() if loaded.extra_data else None) == record.get("extra_data"), (
        "UNKNOWN: serialized/reloaded extra data mismatch")


def require_restore_read(result, expected, entity, kind):
    """Observe the original HA getter; never manufacture its returned truth."""
    assert result is expected, f"UNKNOWN: {entity} {kind} bypassed loaded restore record"


class Head(ClimateEntity):
    """Actual HA service handlers; command completion and reports are separate."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes: ClassVar = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY]
    _attr_fan_modes: ClassVar = ["auto", "quiet", "low", "medium", "middle", "high"]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, name):
        self._attr_name = f"Invented {name}"
        self._attr_unique_id = f"issue25_invented_{name}"
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_fan_mode = "auto"
        self._attr_target_temperature_low = None
        self._attr_target_temperature_high = None
        self.calls = []
        self.delay_auto = False
        self.fail_temperature = False
        self.pending = []

    async def deliver(self, service, args):
        # Each invocation owns fresh gates, never a reused setup/completion flag.
        call = {"number": len(self.calls), "service": service, "args": deepcopy(args),
                "entered": asyncio.Event(), "returned": asyncio.Event(), "at": monotonic()}
        self.calls.append(call)
        call["entered"].set()
        trace("handler-entry", head=self.entity_id, **{k: v for k, v in call.items()
                                                     if k not in ("entered", "returned")})
        await asyncio.sleep(0)  # Exercise an awaited handler with a live event loop.
        if service == "temperature" and self.fail_temperature:
            trace("handler-error", head=self.entity_id, number=call["number"],
                  error="invented dependency failure")
            raise RuntimeError("invented dependency failure")
        if service == "fan":
            token = args["fan_mode"]
            if token == "auto" and self.delay_auto:
                self.pending.append(token)
            else:
                self._attr_fan_mode = token
        elif service == "mode":
            self._attr_hvac_mode = args["hvac_mode"]
        else:
            if "hvac_mode" in args:
                self._attr_hvac_mode = args["hvac_mode"]
            self._attr_target_temperature_low = args.get("target_temp_low")
            self._attr_target_temperature_high = args.get("target_temp_high")
        self.async_write_ha_state()
        call["returned"].set()
        call["returned_at"] = monotonic()
        trace("handler-return", head=self.entity_id, number=call["number"],
              report=self._attr_fan_mode, at=monotonic())

    async def async_set_hvac_mode(self, hvac_mode):
        await self.deliver("mode", {"hvac_mode": hvac_mode})

    async def async_set_temperature(self, **kwargs):
        await self.deliver("temperature", kwargs)

    async def async_set_fan_mode(self, fan_mode):
        await self.deliver("fan", {"fan_mode": fan_mode})

    def report(self, token):
        trace("synthetic-report", head=self.entity_id, fan_mode=token)
        self._attr_fan_mode = token
        self.async_write_ha_state()


class Trial:
    def __init__(self, hass, storage, heads, entry):
        self.hass, self.storage, self.heads, self.entry = hass, storage, heads, entry
        self.errors = []
        self.unknown = []
        self.cycle = 0

    def eid(self, suffix):
        matches = [e.entity_id for e in er.async_get(self.hass).entities.values()
                   if e.config_entry_id == self.entry.entry_id and e.unique_id.endswith(suffix)]
        assert len(matches) == 1, f"UNKNOWN: missing/ambiguous identity {suffix}: {matches}"
        return matches[0]

    def switch(self, room):
        return self.eid(("_primary", "_secondary")[room] + "_fan_auto")

    async def service(self, domain, service, entity, **values):
        trace("public-service", domain=domain, service=service, entity=entity, values=values)
        await self.hass.services.async_call(domain, service, {"entity_id": entity, **values},
                                            blocking=True)
        await self.hass.async_block_till_done()

    async def refresh(self):
        await self.entry.runtime_data.async_refresh()
        await self.hass.async_block_till_done()

    async def temperatures(self, a, b):
        for sensor, value in zip(SENSORS, (a, b), strict=True):
            self.hass.states.async_set(sensor, str(value), {"unit_of_measurement": "°F"})
        await self.hass.async_block_till_done()
        await self.refresh()

    def snapshot(self, label):
        co = self.entry.runtime_data
        trace("snapshot", label=label, cycle=self.cycle, entry=self.entry.entry_id,
              created_at=self.entry.created_at, data=dict(self.entry.data),
              options=dict(self.entry.options), plan=self.hass.states.get(self.eid("_plan")).as_dict(),
              rooms=[{"switch": self.hass.states.get(self.switch(i)).as_dict(),
                      "logical_on": co.fan_auto_is_on(h.entity_id),
                      "head": (s.as_dict() if (s := self.hass.states.get(h.entity_id)) else None),
                      "pending_restore": co._fan_restore.get(h.entity_id),
                      "restore_present": h.entity_id in co._fan_restore,
                      "last_command": co._fan_cmd.get(h.entity_id),
                      "prior_command": co._fan_prev.get(h.entity_id)}
                     for i, h in enumerate(self.heads)])
        trace("controlled-report-queues", label=label,
              pending=[list(h.pending) for h in self.heads])

    def ownership(self, held_a=False, label="recovered"):
        """Public switch and public plan are independent of private latch readings."""
        self.snapshot(label)
        plan = self.hass.states.get(self.eid("_plan"))
        assert plan.state not in ("unknown", "unavailable"), "UNKNOWN: no usable public plan"
        for i, held in enumerate((held_a, False)):
            state = self.hass.states.get(self.switch(i)).state
            expected = "off" if held else "on"
            if state in ("unknown", "unavailable"):
                self.unknown.append(f"{label}/cycle{self.cycle}/room{i}: switch {state}")
            elif state != expected or plan.attributes["zones"][i]["fan_hold"] is not held:
                self.errors.append(f"{label}/cycle{self.cycle}/room{i}: expected {expected}, "
                                   f"hold={held}; got {state}, {plan.attributes['zones'][i]}")

    async def residue(self):
        """Public handback is repeated BEFORE each automatic-ownership cycle."""
        for h in self.heads:
            h.delay_auto = False
            trace("discard-interrupted-reports", head=h.entity_id, tokens=list(h.pending))
            h.pending.clear()
        for i in range(2):
            await self.service("switch", "turn_on", self.switch(i))
        await self.temperatures(75, 75)
        assert all(h._attr_fan_mode == "high" for h in self.heads), "UNKNOWN: no owned high rung"
        self.ownership(label="owned-active")
        before = len(self.heads[0].calls)
        self.heads[0].delay_auto = True
        await self.temperatures(70, 75)
        assert completed_fan(self.heads[0].calls, before, "auto"), (
            "UNKNOWN: delayed auto handback handler did not complete")
        assert self.hass.states.get(self.heads[0].entity_id).state == "fan_only"
        assert self.heads[0]._attr_fan_mode == "high"
        assert self.hass.states.get(self.heads[1].entity_id).state == "cool"
        self.ownership(label="idle-residue-active-control")
        assert not self.errors and not self.unknown, "UNKNOWN: cycle did not begin genuinely not held"

    async def restart(self, unavailable=False, variant=None):
        self.cycle += 1
        original = self.entry.runtime_data
        identity = (self.entry.entry_id, self.entry.created_at, self.switch(0), self.switch(1))
        self.snapshot("pre-shutdown")
        if unavailable:
            # Same external fault on both bases. No coordinator/switch truth is set.
            b = self.heads[1]
            b.fail_temperature = True
            b._attr_target_temperature_high = None
            b.async_write_ha_state()
            await self.refresh()
            for h in self.heads:
                self.hass.states.async_remove(h.entity_id)
            await self.hass.async_block_till_done()
            self.snapshot("after-dependency-failure-and-head-removal")
            for i in range(2):
                if self.hass.states.get(self.switch(i)).state != "unavailable":
                    self.unknown.append(f"cycle{self.cycle}/room{i}: unavailable shutdown unreached")
        before = [self.hass.states.get(self.switch(i)).state for i in range(2)]
        await async_mock_restore_state_shutdown_restart(self.hass)
        raw = deepcopy(self.storage["core.restore_state"])
        trace("serialized-store", cycle=self.cycle, key="core.restore_state", record=raw)
        records = {r["state"]["entity_id"]: r for r in raw["data"]}
        for i in range(2):
            sid = self.switch(i)
            assert records[sid]["state"]["state"] == before[i], "UNKNOWN: shutdown state not persisted"
        assert await self.hass.config_entries.async_unload(self.entry.entry_id)
        await self.hass.async_block_till_done()
        # Load after unload so any removal-time cache updates precede this
        # read. Observe the real Store load result rather than supplying one.
        restore = rs.async_get(self.hass)
        unloaded = {sid: restore.last_states.get(sid) for sid in identity[2:]}
        real_load = restore.store.async_load
        loads = []

        async def observed_load():
            value = await real_load()
            loads.append(deepcopy(value))
            return value

        with patch.object(restore.store, "async_load", new=observed_load):
            await restore.async_load()
        assert loads == [raw["data"]], "UNKNOWN: post-unload load did not read serialized shutdown store"
        for sid in identity[2:]:
            loaded = restore.last_states.get(sid)
            assert loaded is not unloaded[sid], "UNKNOWN: unload-time memory record survived load"
            require_restore_record(sid, loaded, records[sid], self.entry.created_at)
            trace("reloaded-after-unload", cycle=self.cycle, entity=sid,
                  state=loaded.state.as_dict(),
                  extra=(loaded.extra_data.as_dict() if loaded.extra_data else None))
        for h in self.heads:
            h.fail_temperature = False
            h.async_write_ha_state()
        if variant == "missing":
            # Explicitly labelled negative, never decisive real-store evidence.
            for i in range(2):
                rs.async_get(self.hass).last_states.pop(self.switch(i), None)
            trace("negative-only-injection", kind="remove-both-fan-restore-records")
        elif variant == "demand":
            self.hass.states.async_set(SENSORS[1], "71", {"unit_of_measurement": "°F"})
        elif variant == "provisional":
            self.heads[0].report("auto")
        elif variant == "missing-speed":
            self.heads[0].report(None)
        supplied = {sid: restore.last_states.get(sid) for sid in identity[2:]}
        reads = set()
        get_state = rs.RestoreEntity.async_get_last_state
        get_extra = rs.RestoreEntity.async_get_last_extra_data

        async def observed_state(entity):
            result = await get_state(entity)
            if entity.entity_id in supplied:
                loaded = supplied[entity.entity_id]
                require_restore_read(result, loaded.state if loaded else None, entity.entity_id, "state")
                reads.add(entity.entity_id)
                room = identity[2:].index(entity.entity_id)
                head = self.hass.states.get(self.heads[room].entity_id)
                trace("restore-input", cycle=self.cycle, entity=entity.entity_id, room=room,
                      observed_head=head.as_dict() if head else None,
                      expected_active_fixed_point=(room == 1 and variant != "demand"),
                      state=result.as_dict() if result else None,
                      clean_state_answer=result.state if result and result.state in ("on", "off") else None,
                      loaded_extra=(loaded.extra_data.as_dict() if loaded and loaded.extra_data else None),
                      missing_negative=variant == "missing")
            return result

        async def observed_extra(entity):
            result = await get_extra(entity)
            if entity.entity_id in supplied:
                loaded = supplied[entity.entity_id]
                require_restore_read(result, loaded.extra_data if loaded else None, entity.entity_id, "extra")
                trace("restore-extra-input", cycle=self.cycle, entity=entity.entity_id,
                      extra=result.as_dict() if result else None)
            return result

        # These observers always await and return the original methods. Their
        # assertions identify the actual inputs supplied to the product.
        with patch.object(rs.RestoreEntity, "async_get_last_state", new=observed_state), patch.object(
            rs.RestoreEntity, "async_get_last_extra_data", new=observed_extra
        ):
            assert await self.hass.config_entries.async_setup(self.entry.entry_id)
            await self.hass.async_block_till_done()
        assert reads == set(supplied), "UNKNOWN: product restore read missing"
        assert self.entry.runtime_data is not original, "UNKNOWN: coordinator not recreated"
        assert identity == (self.entry.entry_id, self.entry.created_at, self.switch(0), self.switch(1))
        self.snapshot("after-setup")
        await self.refresh()

    async def delivery(self, held=False):
        offsets = [len(h.calls) for h in self.heads]
        self.heads[0].delay_auto = False
        await self.temperatures(73, 73)  # high -> middle, a fresh real fan handler on both rooms
        self.ownership(held, "later-demand")
        for i, h in enumerate(self.heads):
            fan = [c for c in h.calls[offsets[i]:] if c["service"] == "fan"]
            if i == 0 and held:
                if fan:
                    self.errors.append("manual twin received automatic fan writes")
            elif not completed_fan(h.calls, offsets[i], "middle"):
                self.errors.append(f"room{i}: no completed middle fan handler after demand")

    def finish(self):
        trace("schedule-result", errors=self.errors, unknown=self.unknown)
        assert not self.unknown, f"UNKNOWN: {self.unknown}"
        if self.errors:
            raise OwnershipFailure(str(self.errors))


@pytest.fixture
async def trial(hass, hass_storage, enable_custom_integrations):
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [Head("A"), Head("B")]

    async def setup_platform(hass, config, async_add_entities, discovery_info=None):
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=setup_platform))
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    await hass.async_block_till_done()
    for sensor in SENSORS:
        hass.states.async_set(sensor, "75", {"unit_of_measurement": "°F"})
    entry = MockConfigEntry(domain="mxz_coordinator", title="Invented issue25", version=2,
                            data={"zones": [
                                {"name": "Invented A", "climate": heads[0].entity_id, "sensor": SENSORS[0]},
                                {"name": "Invented B", "climate": heads[1].entity_id, "sensor": SENSORS[1]}],
                                "fan_boost_enable": True, "fan_boost_max": "high", "idle_action": "fan_only"})
    entry.add_to_hass(hass)
    t = Trial(hass, hass_storage, heads, entry)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for room in ("primary", "secondary"):
            await t.service("number", "set_value", t.eid(f"_{room}_target"), value=70)
            await t.service("switch", "turn_on", t.eid(f"_{room}_enable"))
        await t.service("switch", "turn_on", t.eid("_coordinator_enable"))
        await t.refresh()
        yield t
    finally:
        for h in heads:
            h.delay_auto = False
            h.fail_temperature = False
            h.pending.clear()
        # Public entry unload must retire its own work; no private timer clearing.
        if entry.state.value == "loaded":
            assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        trace("cleanup", entry=entry.entry_id, state=entry.state.value,
              pending_reports=[h.pending for h in heads])


@pytest.mark.parametrize("schedule", SCHEDULES, ids=SCHEDULES)
async def test_restore_schedule(trial, schedule):
    t = trial
    trace("schedule-start", schedule=schedule)
    await t.residue()
    if schedule in SCHEDULES[:3]:
        manual = schedule == SCHEDULES[2]
        if manual:
            # A genuine public OFF gesture pins exactly the automatic twin's token.
            held_from = len(t.heads[0].calls)
            await t.service("switch", "turn_off", t.switch(0))
            require_state(t.hass.states.get(t.switch(0)), "off", "immediate manual OFF")
            await t.refresh()
            t.ownership(True, "manual-gesture-refreshed-plan")
        for cycle in range(2):
            if cycle and not manual:
                # Preserve first-cycle failures while independently recreating ON.
                previous = t.errors[:]
                t.errors.clear()
                unknown = t.unknown[:]
                t.unknown.clear()
                await t.residue()
                t.errors[:0] = previous
                t.unknown[:0] = unknown
            elif cycle:
                t.heads[0].delay_auto = True
                await t.temperatures(70, 75)
                t.ownership(True, "manual-next-cycle-no-reset")
            await t.restart(unavailable=schedule == SCHEDULES[1] or (manual and cycle == 1))
            t.ownership(manual)
            await t.delivery(manual)
        if manual and any(c["service"] == "fan" for c in t.heads[0].calls[held_from:]):
            t.errors.append("manual twin received a fan write after OFF, including during restore")
    elif schedule == SCHEDULES[3]:
        old = t.entry.runtime_data
        t.hass.config_entries.async_update_entry(t.entry, options={"fan_boost_enable": True})
        await t.hass.async_block_till_done()
        assert t.entry.runtime_data is not old, "UNKNOWN: options reload did not happen"
        t.ownership(label="options-reload")
        await t.service("climate", "set_fan_mode", t.heads[0].entity_id, fan_mode="quiet")
        await t.refresh()
        t.ownership(True, "public-manual-pick")
        await t.service("switch", "turn_off", t.switch(0))
        await t.hass.config_entries.async_reload(t.entry.entry_id)
        await t.hass.async_block_till_done()
        t.ownership(True, "held-reload")
        await t.service("switch", "turn_on", t.switch(0))
        await t.refresh()  # quiet report remains while auto command completes
        t.ownership(label="explicit-on-delayed-echo")
        t.heads[0].report("auto")
        await t.refresh()
        await t.delivery()
    elif schedule == SCHEDULES[4]:
        await t.restart(variant="missing")
        t.ownership(True, "negative-conservative-idle-active-fixed-point")
        await t.service("switch", "turn_on", t.switch(0))
        await t.delivery()
    elif schedule == SCHEDULES[5]:
        await t.restart(variant="demand")
        require_state(t.hass.states.get(SENSORS[1]), "71", "changed B demand")
        require_cooling(t.hass.states.get(t.heads[1].entity_id),
                        t.hass.states.get(t.eid("_plan")), 1)
        t.ownership(label="changed-active-demand-idle-control")
        await t.delivery()
    elif schedule == SCHEDULES[6]:
        await t.restart(variant="provisional")
        t.snapshot("provisional-auto")
        t.heads[0].report("high")
        await t.refresh()
        t.ownership(label="delayed-old-token")
        await t.delivery()
    else:
        await t.restart(variant="missing-speed")
        assert t.hass.states.get(t.heads[0].entity_id).attributes.get("fan_mode") is None, (
            "UNKNOWN: missing-speed condition not reached")
        t.ownership(label="missing-speed-pending-truth")
        t.heads[0].report("high")
        await t.refresh()
        t.ownership(label="speed-recovery")
        await t.service("switch", "turn_on", t.switch(0))
        await t.delivery()
    t.finish()
