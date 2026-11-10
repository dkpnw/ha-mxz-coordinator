"""Restart fan-hold semantics: persist + reconcile (the shape-4 fix).

The manual-fan latch cannot tell boost residue from a deliberate idle hold by
token alone (four shipped bug shapes proved it), so the Fan-auto switch now
restores one bool — held or not — and the seed reconciles it against the
OBSERVED head state. This file is the scenario matrix that chose the design,
promoted to regression tests: every assertion is the decided-correct outcome
for that scenario's user, including the two documented edges (S6, S9).

Each restart is a real one (tests/restart.py): MXZ keeps nothing in memory,
and the Fan auto switch restores what it wrote at the stop. What happens while
Home Assistant is down (a pick at the wall remote, restore data that is stale
or other than what the switch wrote) is a step inside ``stopped``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import restore_state as rs
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.mxz_coordinator.const import (
    CONF_IDLE_ACTION,
    FAN_LADDER,
    IDLE_ACTION_FAN_ONLY,
    INHIBIT_ACTION_OFF,
)
from tests.restart import restart, stopped
from tests.restore_store import replace_stored_state
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_fan_auto,
    _set_hold,
    _set_target,
    _set_temp,
    _setup_fan_boost,
    _setup_inhibit,
    _setup_mock_heads,
    _user_set_fan,
)


def _fan_hold(hass, entry) -> bool:
    return hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][0]["fan_hold"]


def _simulated_restart(coord, restore: dict | None = None) -> None:
    """Wipe per-head decision memory as a restart would; inject the switch's restore.

    Only test_s12_gestures_while_unresolved still uses it; its docstring says why.
    """
    coord._fan_cmd.clear()
    coord._fan_prev.clear()
    coord._fan_older.clear()
    coord._fan_latched.clear()
    coord._fan_idx.clear()
    coord._engage_latch.clear()
    if restore:
        for cid, held in restore.items():
            coord.restore_fan_hold(cid, held)


def _plant(hass, entity_id: str, state: str, when: datetime | None = None) -> None:
    """Make ``state`` at ``when`` (default now) the record a start restores, with no attributes."""
    when = when or dt_util.utcnow()
    replace_stored_state(hass, entity_id, rs.StoredState(
        State(entity_id, state, last_changed=when, last_updated=when), None, when,
    ))


def _plant_stale_fan_auto(hass, entry, state: str) -> None:
    """A Fan auto record from before this entry existed: the switch ignores it."""
    _plant(hass, _eid(hass, entry, "_primary_fan_auto"), state, entry.created_at - timedelta(seconds=1))


def _expect(hass, entry, head, *, hold: bool, fan: str | None = None) -> None:
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is hold
    if fan is not None:
        assert hass.states.get(head).attributes["fan_mode"] == fan


async def _std(hass):
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    return head_a, head_b, entry


async def _drive_to_quiet_active(hass, entry) -> None:
    """Boost the primary up then walk it down to 'quiet' while still cooling."""
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 63.4)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 62.4)
    await _recompute(hass, entry)


async def test_s1_restart_mid_conditioning_hysteresis_rung(hass: HomeAssistant):
    head_a, _b, entry = await _std(hass)
    await _set_temp(hass, SENSOR_A, 65)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 63.5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"
    await restart(hass, entry)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="medium")  # adopted, not held
    await _set_temp(hass, SENSOR_A, 62.4)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="quiet")  # ramp continued


async def test_s1b_restart_mid_ramp_not_held_restore_adopts_off_fixed_point(
    hass: HomeAssistant,
):
    """Hypothesis 1 (R4 cell 06), restored truth without command memory: the room
    cooled during the outage, so 'high' is not the ladder's rung at the new
    delta. Restored NOT held means boost wrote it: adopt, step down, keep driving.
    A mere change of delta is never a manual gesture."""
    head_a, _b, entry = await _std(hass)
    await _set_temp(hass, SENSOR_A, 67)  # delta 5 -> high
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"
    async with stopped(hass, entry):
        await _set_temp(hass, SENSOR_A, 64)  # delta 2 at the seed: high is no fixed point
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="medium")  # ours, stepped down
    await _set_temp(hass, SENSOR_A, 63)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="low")  # ramp continues


async def test_s1c_active_seed_above_ceiling_still_holds(hass: HomeAssistant):
    """Ceiling=medium; restored NOT held, but the head reports 'high' while
    actively cooling. Boost could never have written high: a hand did, so the
    guard that S11b pins for an idle seed holds for an active one too."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b, fan_boost_max="medium")
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"  # the ceiling
    async with stopped(hass, entry):
        await _user_set_fan(hass, head_a, "high")  # wall remote during the outage
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="high")


async def test_s2_boost_residue_satisfied_at_seed(hass: HomeAssistant):
    """THE LIVE DEFECT. Head 'cool fan=quiet' at restart, room satisfied."""
    head_a, _b, entry = await _std(hass)
    await _drive_to_quiet_active(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"
    assert hass.states.get(head_a).state == "cool"
    async with stopped(hass, entry):
        await _set_temp(hass, SENSOR_A, 61.5)  # satisfied-ness materializes at restart
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="auto")  # residue cleared
    await _set_temp(hass, SENSOR_A, 67)  # later: room drifts, zone activates
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="high")  # boost drives again


async def test_s3_active_hold_out_of_band(hass: HomeAssistant):
    head_a, _b, entry = await _std(hass)
    await _set_temp(hass, SENSOR_A, 63.5)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "high")  # delta 1.5: out of band -> hold
    await _recompute(hass, entry)
    await restart(hass, entry)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="high")
    await _user_set_fan(hass, head_a, "auto")  # gesture must still release
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False)


async def _idle_hold(hass, entry, head_a, token: str) -> None:
    await _set_temp(hass, SENSOR_A, 61.5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"
    await _user_set_fan(hass, head_a, token)
    await _recompute(hass, entry)
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is True  # mid-session idle pick latches


async def test_s4_idle_hold_high_across_restart(hass: HomeAssistant):
    head_a, _b, entry = await _std(hass)
    await _idle_hold(hass, entry, head_a, "high")
    await restart(hass, entry)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="high")
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="high")


async def test_s5_idle_hold_quiet_across_restart(hass: HomeAssistant):
    """Token-identical to S2 from the seed's point of view. The crux."""
    head_a, _b, entry = await _std(hass)
    await _idle_hold(hass, entry, head_a, "quiet")
    await restart(hass, entry)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="quiet")  # token-identical to S2: restore disambiguates
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="quiet")


async def test_s5b_held_hold_moved_during_outage(hass: HomeAssistant):
    """Held at 'quiet' before the outage, moved to 'medium' via wall remote
    while HA was down: still held — at the NEW token — and it must be a
    STANDING hold next cycle, not re-litigated on drift."""
    head_a, _b, entry = await _std(hass)
    await _idle_hold(hass, entry, head_a, "quiet")
    async with stopped(hass, entry):
        await _user_set_fan(hass, head_a, "medium")  # moved during the outage
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="medium")
    await _set_temp(hass, SENSOR_A, 67)  # activates; boost must not reclaim
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="medium")


async def test_s6_gesture_during_outage_not_held_before(hass: HomeAssistant):
    head_a, _b, entry = await _std(hass)
    await _set_temp(hass, SENSOR_A, 61.5)
    await _recompute(hass, entry)  # satisfied, fan auto, not held
    async with stopped(hass, entry):  # pre-outage truth: not held
        await _user_set_fan(hass, head_a, "medium")  # wall remote during the outage
    await _recompute(hass, entry)
    # DOCUMENTED EDGE: an in-ladder pick made while HA was DOWN, on a zone that
    # was not held before, is indistinguishable from residue and is cleared.
    _expect(hass, entry, head_a, hold=False, fan="auto")
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False)  # boost drives thereafter


async def test_s7_release_to_auto_during_outage(hass: HomeAssistant):
    head_a, _b, entry = await _std(hass)
    await _idle_hold(hass, entry, head_a, "quiet")
    async with stopped(hass, entry):
        await _user_set_fan(hass, head_a, "auto")  # released via remote during outage
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False)
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="high")


async def test_s8_eco_seed_with_non_auto_token(hass: HomeAssistant):
    head_a, _b, entry = await _std(hass)
    await _set_temp(hass, SENSOR_A, 65)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 63.5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"
    async with stopped(hass, entry):
        _plant(hass, _eid(hass, entry, "_eco_idle"), "on")  # eco on as the restart finds it
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="auto")  # residue cleared under eco too


async def test_s9_stale_restore_falls_back(hass: HomeAssistant):
    """Entry re-added: restore data stale -> the switch hands nothing over.
    Must equal current-code behavior."""
    head_a, _b, entry = await _std(hass)
    await _idle_hold(hass, entry, head_a, "quiet")
    async with stopped(hass, entry):
        _plant_stale_fan_auto(hass, entry, "off")
    await _recompute(hass, entry)
    # DOCUMENTED EDGE: no restore data (entry re-added / first post-upgrade
    # restart) falls back to observed-state seeding — which latches. Right for
    # this hold; the residue shape can recur exactly once in that window.
    _expect(hass, entry, head_a, hold=True, fan="quiet")


async def test_s10_fresh_install_pre_existing_manual_speed(hass: HomeAssistant):
    """First compute EVER: owner's pre-install circulation speed on an idle head."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _user_set_fan(hass, head_a, "medium")  # owner's standing pick, pre-install
    await _set_temp(hass, SENSOR_A, 61.5)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)  # install + enable
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="medium")  # pre-install pick honored


async def test_s11_satisfied_seed_above_boost_ceiling(hass: HomeAssistant):
    """Ceiling=medium; head held at 'high' (never a token boost could write),
    restart lands satisfied. Residue is IMPOSSIBLE here -> must stay held."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b, fan_boost_max="medium")
    await _idle_hold(hass, entry, head_a, "high")
    await restart(hass, entry)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="high")  # boost could never write high


async def test_s11b_not_held_seed_above_boost_ceiling(hass: HomeAssistant):
    """Ceiling=medium; restored NOT held, but the head reports 'high' while
    satisfied. Boost could never have written high, so it cannot be residue —
    a hand set it during the outage. The impossible-token guard must hold it
    even against a not-held restore."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 61.5)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b, fan_boost_max="medium")
    await _recompute(hass, entry)  # satisfied, fan auto, not held
    async with stopped(hass, entry):
        await _user_set_fan(hass, head_a, "high")  # wall remote during the outage
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="high")


async def test_s12_gestures_while_unresolved(hass: HomeAssistant):
    """H2 discriminator: satisfied ambiguous seed, then (a) Fan-auto switch OFF
    -> must become a firm hold; separately (b) token change -> firm hold.

    Still on the simulated restart (``_simulated_restart``): on a real one the
    switch restores "quiet" as MXZ's prior write, so the pick of "quiet" below
    reads as MXZ's echo and the first refresh writes "auto" back before the
    switch is turned off. The simulated restart forgets that write, and the
    same coordinator's refresh cooldown keeps the rewrite back until after.
    """
    head_a, _b, entry = await _std(hass)
    coord = entry.runtime_data
    await _drive_to_quiet_active(hass, entry)
    await _set_temp(hass, SENSOR_A, 61.5)
    _simulated_restart(coord, {head_a: False})
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False)  # residue cleared at seed
    # (a) user pins it via the switch while satisfied/pending
    await _user_set_fan(hass, head_a, "quiet")  # ensure a non-auto token to pin
    await _set_fan_auto(hass, _eid(hass, entry, "_primary_fan_auto"), False)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="quiet")
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="quiet")


async def test_s13_departure_while_unresolved(hass: HomeAssistant):
    head_a, _b, entry = await _std(hass)
    await _drive_to_quiet_active(hass, entry)
    async with stopped(hass, entry):
        await _set_temp(hass, SENSOR_A, 61.5)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "middle")  # gesture after the ambiguous seed
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="middle")


async def test_s14_restart_during_vane_kick_wake(hass: HomeAssistant):
    """Restart while a head was awake in fan_only for a vane kick: kick memory is
    gone; head is fan_only carrying whatever fan token it had."""
    head_a, _b, entry = await _std(hass)
    await _drive_to_quiet_active(hass, entry)  # boost residue 'quiet'
    async with stopped(hass, entry):
        # The kick's parked state survives the restart: fan_only + quiet.
        st = hass.states.get(head_a)
        hass.states.async_set(head_a, "fan_only", dict(st.attributes))
        await _set_temp(hass, SENSOR_A, 61.5)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="auto")  # kick-wake residue cleared


async def test_switch_restore_path_end_to_end(hass: HomeAssistant) -> None:
    """The REAL plumbing: a restored switch state reaches the seed.

    The cases above restart a running entry; this one sets a new entry up on
    a planted restore: mock_restore_cache -> RestoreEntity ->
    async_added_to_hass -> reconciliation. Entry added FIRST so the restore is
    fresh (the stale guard is pinned in test_issue7's idiom and in the switch
    docstring).
    """
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        mock_restore_cache,
    )

    from custom_components.mxz_coordinator.const import (
        CONF_FAN_BOOST_ENABLE,
        CONF_PRIMARY_CLIMATE,
        CONF_PRIMARY_SENSOR,
        CONF_SECONDARY_CLIMATE,
        CONF_SECONDARY_SENSOR,
        DOMAIN,
    )

    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 61.5)
    await _set_temp(hass, SENSOR_B, 70)
    # The head sits carrying a non-auto token = the crux ambiguity at seed.
    await _user_set_fan(hass, head_a, "quiet")

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_IDLE_ACTION: IDLE_ACTION_FAN_ONLY,
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: True,
        },
    )
    entry.add_to_hass(hass)  # created FIRST -> the restore below is fresh
    # Pre-restart truth via the switch's restored state: "off" = HELD.
    mock_restore_cache(
        hass, [State("switch.mxz_coordinator_primary_fan_auto", "off")]
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="quiet")  # hold survived



# ---------------------------------------------------------------------------
# S10/S11: a standby (inhibit) hold releases like a restart for the fan latch.
# The hold parks the heads with the fan machinery FROZEN (no fan writes); on
# release _reseed_fan_after_standby carries the pre-hold latch truth into the
# restore slot and clears the command memory, so the fan the hold left behind
# reconciles the same way a restart's does — a real hold is kept, boost residue
# is not mistaken for one.
# ---------------------------------------------------------------------------
async def test_s10_standby_release_preserves_real_hold(hass: HomeAssistant):
    """A manual fan hold placed before the hold survives it (S3, via standby)."""
    entry, head_a, _b = await _setup_inhibit(hass, INHIBIT_ACTION_OFF)
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 62)
    await _set_temp(hass, SENSOR_A, 63.5)  # delta 1.5 -> actively cooling
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "high")  # deliberate out-of-band hold
    await _recompute(hass, entry)
    assert _fan_hold(hass, entry) is True

    await _set_hold(hass, "on")  # grid down -> park off (fan frozen at "high")
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"

    await _set_hold(hass, "off")  # grid restored -> reseed + reconcile
    await _recompute(hass, entry)
    assert _fan_hold(hass, entry) is True  # the real hold is preserved
    assert hass.states.get(head_a).attributes["fan_mode"] == "high"


async def test_s11_standby_release_drops_boost_residue(hass: HomeAssistant):
    """Boost residue left through a hold is NOT read as a manual hold (S2)."""
    entry, head_a, _b = await _setup_inhibit(hass, INHIBIT_ACTION_OFF)
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 62)
    await _set_temp(hass, SENSOR_A, 63.5)  # delta 1.5 -> boost drives, no manual pick
    await _recompute(hass, entry)
    assert _fan_hold(hass, entry) is False
    assert hass.states.get(head_a).attributes["fan_mode"] in FAN_LADDER

    await _set_hold(hass, "on")  # park off (fan frozen at the boost speed)
    await _recompute(hass, entry)
    await _set_hold(hass, "off")  # reseed + reconcile
    await _recompute(hass, entry)
    assert _fan_hold(hass, entry) is False  # residue not latched
    assert hass.states.get(head_a).attributes["fan_mode"] in FAN_LADDER  # boost still driving


async def test_s12_restart_during_standby_keeps_restored_hold(hass: HomeAssistant):
    """HA restarts DURING the hold: the switch's restore must survive release.

    The restart seeds _fan_restore before any fan write can consume it (fan
    writes are frozen while held), so the latch is empty when the hold releases.
    The release reseed must not overwrite that unconsumed restore with the empty
    latch — a pre-restart manual hold would silently dissolve into boost.

    The hold sits at an IN-BAND token on purpose: that is the case only the
    restored truth can decide (token-guessing would adopt it as boost residue),
    so overwriting the restore is observable as the hold releasing itself.
    """
    entry, head_a, _b = await _setup_inhibit(hass, INHIBIT_ACTION_OFF)
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 62)
    await _set_temp(hass, SENSOR_A, 63.5)  # boost drives an in-band token
    await _recompute(hass, entry)
    token = hass.states.get(head_a).attributes["fan_mode"]
    assert token in FAN_LADDER

    await _set_hold(hass, "on")  # grid down -> park off (fan frozen at token)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"

    # Restart mid-outage: the Fan-auto switch restores "held" (the user had
    # latched this zone at that in-band speed before the restart).
    async with stopped(hass, entry):
        _plant(hass, _eid(hass, entry, "_primary_fan_auto"), "off")
    await _set_hold(hass, "off")  # grid restored -> reseed must keep the restore
    await _recompute(hass, entry)
    assert _fan_hold(hass, entry) is True  # pre-restart hold survived
    assert hass.states.get(head_a).attributes["fan_mode"] == token
