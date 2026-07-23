"""Restart fan-hold semantics: persist + reconcile (the shape-4 fix).

The manual-fan latch cannot tell boost residue from a deliberate idle hold by
token alone (four shipped bug shapes proved it), so the Fan-auto switch now
restores one bool — held or not — and the seed reconciles it against the
OBSERVED head state. This file is the scenario matrix that chose the design,
promoted to regression tests: every assertion is the decided-correct outcome
for that scenario's user, including the two documented edges (S6, S9).

Restart = clear the coordinator's decision memory (the existing tests' idiom)
and inject what the switch would have restored.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM  # noqa: E402

from custom_components.mxz_coordinator.const import (  # noqa: E402
    FAN_LADDER,
    INHIBIT_ACTION_OFF,
)
from tests.test_drive import (  # noqa: E402
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_fan_auto,
    _set_hold,
    _set_temp,
    _setup_fan_boost,
    _setup_inhibit,
    _setup_mock_heads,
    _user_set_fan,
)


def _fan_hold(hass, entry) -> bool:
    return hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][0]["fan_hold"]


def _restart(coord, restore: dict | None = None) -> None:
    """Wipe per-head decision memory like a HA restart; inject the switch's restore."""
    coord._fan_cmd.clear()
    coord._fan_prev.clear()
    coord._fan_latched.clear()
    coord._fan_idx.clear()
    coord._engage_latch.clear()
    if restore:
        for cid, held in restore.items():
            coord.restore_fan_hold(cid, held)


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
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 65)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 63.5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"
    _restart(coord, {head_a: False})
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="medium")  # adopted, not held
    await _set_temp(hass, SENSOR_A, 62.4)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="quiet")  # ramp continued


async def test_s2_boost_residue_satisfied_at_seed(hass: HomeAssistant):
    """THE LIVE DEFECT. Head 'cool fan=quiet' at restart, room satisfied."""
    head_a, _b, entry = await _std(hass)
    coord = entry.runtime_data
    await _drive_to_quiet_active(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"
    assert hass.states.get(head_a).state == "cool"
    await _set_temp(hass, SENSOR_A, 61.5)  # satisfied-ness materializes at restart
    _restart(coord, {head_a: False})
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="auto")  # residue cleared
    await _set_temp(hass, SENSOR_A, 67)  # later: room drifts, zone activates
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="high")  # boost drives again


async def test_s3_active_hold_out_of_band(hass: HomeAssistant):
    head_a, _b, entry = await _std(hass)
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 63.5)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "high")  # delta 1.5: out of band -> hold
    await _recompute(hass, entry)
    _restart(coord, {head_a: True})
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
    coord = entry.runtime_data
    await _idle_hold(hass, entry, head_a, "high")
    _restart(coord, {head_a: True})
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="high")
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="high")


async def test_s5_idle_hold_quiet_across_restart(hass: HomeAssistant):
    """Token-identical to S2 from the seed's point of view. The crux."""
    head_a, _b, entry = await _std(hass)
    coord = entry.runtime_data
    await _idle_hold(hass, entry, head_a, "quiet")
    _restart(coord, {head_a: True})
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
    coord = entry.runtime_data
    await _idle_hold(hass, entry, head_a, "quiet")
    _restart(coord, {head_a: True})
    await _user_set_fan(hass, head_a, "medium")  # moved during the outage
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="medium")
    await _set_temp(hass, SENSOR_A, 67)  # activates; boost must not reclaim
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="medium")


async def test_s6_gesture_during_outage_not_held_before(hass: HomeAssistant):
    head_a, _b, entry = await _std(hass)
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 61.5)
    await _recompute(hass, entry)  # satisfied, fan auto, not held
    _restart(coord, {head_a: False})  # pre-outage truth: not held
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
    coord = entry.runtime_data
    await _idle_hold(hass, entry, head_a, "quiet")
    _restart(coord, {head_a: True})
    await _user_set_fan(hass, head_a, "auto")  # released via remote during outage
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False)
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="high")


async def test_s8_eco_seed_with_non_auto_token(hass: HomeAssistant):
    head_a, _b, entry = await _std(hass)
    coord = entry.runtime_data
    await _set_temp(hass, SENSOR_A, 65)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 63.5)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "medium"
    coord.eco_idle = True  # eco flips on right before the restart
    _restart(coord, {head_a: False})
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="auto")  # residue cleared under eco too


async def test_s9_stale_restore_falls_back(hass: HomeAssistant):
    """Entry re-added: restore data stale -> switch never injects it (simulated
    by NOT calling restore_fan_hold). Must equal current-code behavior."""
    head_a, _b, entry = await _std(hass)
    coord = entry.runtime_data
    await _idle_hold(hass, entry, head_a, "quiet")
    _restart(coord)  # no restore injection = stale/absent
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
    coord = entry.runtime_data
    await _idle_hold(hass, entry, head_a, "high")
    _restart(coord, {head_a: True})
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
    coord = entry.runtime_data
    await _recompute(hass, entry)  # satisfied, fan auto, not held
    _restart(coord, {head_a: False})
    await _user_set_fan(hass, head_a, "high")  # wall remote during the outage
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="high")


async def test_s12_gestures_while_unresolved(hass: HomeAssistant):
    """H2 discriminator: satisfied ambiguous seed, then (a) Fan-auto switch OFF
    -> must become a firm hold; separately (b) token change -> firm hold."""
    head_a, _b, entry = await _std(hass)
    coord = entry.runtime_data
    await _drive_to_quiet_active(hass, entry)
    await _set_temp(hass, SENSOR_A, 61.5)
    _restart(coord, {head_a: False})
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
    coord = entry.runtime_data
    await _drive_to_quiet_active(hass, entry)
    await _set_temp(hass, SENSOR_A, 61.5)
    _restart(coord, {head_a: False})
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "middle")  # gesture after the ambiguous seed
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=True, fan="middle")


async def test_s14_restart_during_vane_kick_wake(hass: HomeAssistant):
    """Restart while a head was awake in fan_only for a vane kick: kick memory is
    gone; head is fan_only carrying whatever fan token it had."""
    head_a, _b, entry = await _std(hass)
    coord = entry.runtime_data
    await _drive_to_quiet_active(hass, entry)  # boost residue 'quiet'
    # Simulate the kick's parked state surviving the restart: fan_only + quiet.
    st = hass.states.get(head_a)
    hass.states.async_set(head_a, "fan_only", dict(st.attributes))
    await _set_temp(hass, SENSOR_A, 61.5)
    _restart(coord, {head_a: False})
    await _recompute(hass, entry)
    _expect(hass, entry, head_a, hold=False, fan="auto")  # kick-wake residue cleared


async def test_switch_restore_path_end_to_end(hass: HomeAssistant) -> None:
    """The REAL plumbing: a restored switch state reaches the seed.

    Everything above injects restore_fan_hold() directly; this test goes
    through mock_restore_cache -> RestoreEntity -> async_added_to_hass ->
    reconciliation. Entry added FIRST so the restore is fresh (the stale
    guard is pinned in test_issue7's idiom and in the switch docstring).
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
    await _set_temp(hass, SENSOR_A, 63.5)  # delta 6.5 -> actively cooling
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
    await _set_temp(hass, SENSOR_A, 63.5)  # boost drives the fan, no manual pick
    await _recompute(hass, entry)
    assert _fan_hold(hass, entry) is False
    assert hass.states.get(head_a).attributes["fan_mode"] in FAN_LADDER

    await _set_hold(hass, "on")  # park off (fan frozen at the boost speed)
    await _recompute(hass, entry)
    await _set_hold(hass, "off")  # reseed + reconcile
    await _recompute(hass, entry)
    assert _fan_hold(hass, entry) is False  # residue not latched
    assert hass.states.get(head_a).attributes["fan_mode"] in FAN_LADDER  # boost still driving
