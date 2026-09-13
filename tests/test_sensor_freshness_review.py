"""Freshness regressions for cached reports, safety controls, and reloads.

A cached valid return with an unchanged marker cannot end invalidity; a
sequence advance is dated from its event; and a future timestamp rejected at
write time remains rejected during an unrelated refresh. Safety controls keep
their authority across unhealthy states, each room's cadence expires on its
own clock, and neutral room demands park both rooms while the shared cool mode
remains retained. A new write whose previously future marker has become valid
can restore health, while restored metadata never selects an evidence basis
and a write receipt extends only its own window.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ACTIVE_STATE,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_ZONES,
    HEALTH_AWAITING,
    HEALTH_CADENCE_UNKNOWN,
    HEALTH_HEALTHY,
    HEALTH_STALE,
    IDLE_ACTION_OFF_AFTER_DRY,
    INHIBIT_ACTION_FAN_ONLY,
    ZONE_EVIDENCE_BASIS,
    ZONE_REPORT_INTERVAL,
)
from custom_components.mxz_coordinator.logic import sample_evidence

from .test_drive import SENSOR_B, _eid, _recompute, _user_set_fan
from .test_idle_action import _head_calls, _record_calls
from .test_sensor_freshness import (
    HOT,
    SENSOR_A,
    _advance,
    _health,
    _report,
    _settle,
    _setup_fresh,
)
from .test_sensor_freshness_edges import (
    SAMPLE_ATTR,
    SEQUENCE_ATTR,
    _drain_startup,
    _exact,
    _iso,
    _report_marker,
    _sample_room,
    _sequence_room,
)

# --- cached reports and event markers ----------------------------------------


@pytest.mark.parametrize("basis", ["timestamp", "sequence"])
async def test_cached_valid_return_cannot_close_invalid_episode(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture, basis: str
) -> None:
    profile = _sample_room() if basis == "timestamp" else _sequence_room()
    attr = SAMPLE_ATTR if basis == "timestamp" else SEQUENCE_ATTR
    entry, head, _ = await _setup_fresh(hass, freezer, primary=profile)
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    marker = _iso() if basis == "timestamp" else 42
    await _report_marker(hass, HOT[False], marker, attribute=attr)
    await _settle(hass, freezer)
    if basis == "sequence":
        marker = 43
        await _report_marker(hass, HOT[False], marker, attribute=attr)
        await _settle(hass, freezer)
    assert _health(c) == "healthy"
    evidence = c._evidence_ts["primary"]
    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        hass.states.async_set(SENSOR_A, "unavailable")
        await hass.async_block_till_done()
        await _settle(hass, freezer)
        assert _health(c) == "invalid"
        assert hass.states.get(head).state == "fan_only"
        caplog.clear()
        await _report_marker(hass, HOT[False], marker, attribute=attr)
        await _settle(hass, freezer)
        observed = {
            "health": _health(c),
            "demand": c.data["primary_demand"],
            "head": hass.states.get(head).state,
            "marker": c._sample_marker["primary"],
            "evidence_unchanged": c._evidence_ts["primary"] == evidence,
            "recovery": [
                r.message for r in caplog.records if "reporting again" in r.message
            ],
        }
        # A new marker is a real recovery control, unlike the cached return.
        fresh_marker = _iso() if basis == "timestamp" else marker + 1
        await _report_marker(hass, HOT[False], fresh_marker, attribute=attr)
        await _settle(hass, freezer)
        assert _health(c) == "healthy"
        assert hass.states.get(head).state == "cool"
        assert c._evidence_ts["primary"] > evidence
        assert observed["evidence_unchanged"]
        assert observed["demand"] == "neutral", observed
        assert observed["head"] == "fan_only", observed
        assert not observed["recovery"], observed


async def test_sequence_advance_uses_event_receipt_before_cached_rewrite(
    hass: HomeAssistant, freezer: Any
) -> None:
    entry, head, _ = await _setup_fresh(
        hass, freezer, primary=_sequence_room(**{ZONE_REPORT_INTERVAL: 1})
    )
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report_marker(hass, HOT[False], 42, attribute=SEQUENCE_ATTR)
    await _settle(hass, freezer)
    await _report_marker(hass, HOT[False], 43, attribute=SEQUENCE_ATTR)
    await _settle(hass, freezer)
    assert _health(c) == "healthy"
    # Enter the normal request-refresh cooldown, without editing product state.
    await c.async_request_refresh()
    await hass.async_block_till_done()
    await _exact(hass, freezer, timedelta(seconds=1))
    advance_at = dt_util.utcnow().timestamp()
    await _report_marker(hass, HOT[False], 44, attribute=SEQUENCE_ATTR)
    await _exact(hass, freezer, timedelta(seconds=1))
    await _report_marker(hass, HOT[False], 44, attribute=SEQUENCE_ATTR)
    await _settle(hass, freezer)
    observed = {
        "advance_at": advance_at,
        "evidence_at": c._evidence_ts["primary"],
        "deadline": c._fresh_deadline["primary"],
    }
    await _exact(
        hass,
        freezer,
        timedelta(seconds=advance_at + 180 - dt_util.utcnow().timestamp()),
    )
    observed.update(at_true_deadline=_health(c), head=hass.states.get(head).state)
    assert c._sample_marker["primary"] == 44
    assert observed["evidence_at"] == advance_at, observed
    assert observed["deadline"] == advance_at + 180, observed


async def test_future_rejected_write_does_not_recover_without_new_report(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    entry, head, _ = await _setup_fresh(
        hass, freezer, primary=_sample_room(**{ZONE_REPORT_INTERVAL: 1})
    )
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report_marker(hass, HOT[False], _iso())
    await _settle(hass, freezer)
    await _exact(hass, freezer, timedelta(seconds=190))
    await _settle(hass, freezer)
    assert _health(c) == "stale"
    await _report_marker(hass, HOT[False], _iso(60))
    await _settle(hass, freezer)
    assert _health(c) == "stale"
    rejected_write = hass.states.get(SENSOR_A).last_reported
    await _exact(hass, freezer, timedelta(seconds=60))
    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        # An unrelated normal refresh sees exactly the same rejected HA write.
        await c.async_request_refresh()
        await hass.async_block_till_done()
        await _settle(hass, freezer)
        observed = {
            "health": _health(c),
            "head": hass.states.get(head).state,
            "same_write": hass.states.get(SENSOR_A).last_reported == rejected_write,
            "recovery": [
                r.message for r in caplog.records if "reporting again" in r.message
            ],
        }
        assert observed["same_write"]
        assert observed["health"] == "stale", observed
        assert observed["head"] == "fan_only", observed
        assert not observed["recovery"], observed


# --- safety controls and cadence guards --------------------------------------


async def test_kill_switch_stays_zero_write_across_stale_and_recovery(
    hass: HomeAssistant, freezer: Any
) -> None:
    entry, _head, _ = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _settle(hass, freezer)
    assert _health(c) == "healthy"
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _eid(hass, entry, "_coordinator_enable")},
        blocking=True,
    )
    await _settle(hass, freezer)
    calls = _record_calls(hass)
    await _exact(hass, freezer, timedelta(seconds=190))
    await _settle(hass, freezer)
    assert _health(c) == "stale"
    assert not calls
    await _report(hass, SENSOR_A, HOT[False])
    await _settle(hass, freezer)
    assert _health(c) == "healthy"
    assert not calls
    assert c.coordinator_enable is False


async def test_stale_priority_loss_preserves_opposite_mode_dwell(
    hass: HomeAssistant, freezer: Any
) -> None:
    entry, a, b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: 1},
        **{CONF_MODE_HYSTERESIS: 600},
    )
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _report(hass, SENSOR_B, 65)
    await _settle(hass, freezer)
    assert c.data["primary_demand"] == "cool"
    assert c.data["secondary_demand"] == "heat"
    assert c.data["state"] == "cool"
    await _exact(hass, freezer, timedelta(seconds=190))
    await _settle(hass, freezer)
    assert _health(c) == "stale"
    assert c.data["primary_demand"] == "neutral"
    assert c.data["secondary_demand"] == "heat"
    assert c.data["state"] == "cool"
    assert c.data["mode_change_allowed"] is False
    assert hass.states.get(a).state == "fan_only"
    assert hass.states.get(b).state != "heat"
    await _exact(hass, freezer, timedelta(seconds=610))
    await _settle(hass, freezer)
    assert c.data["state"] == "heat"
    assert hass.states.get(b).state == "heat"


async def test_different_cadences_then_all_unhealthy_park(
    hass: HomeAssistant, freezer: Any
) -> None:
    entry, a, b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: 1},
        secondary={ZONE_REPORT_INTERVAL: 10},
    )
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _report(hass, SENSOR_B, HOT[False])
    t0 = dt_util.utcnow().timestamp()
    await _settle(hass, freezer)
    await _exact(
        hass, freezer, timedelta(seconds=t0 + 180 - dt_util.utcnow().timestamp())
    )
    await _settle(hass, freezer)
    assert _health(c) == "stale"
    assert _health(c, "secondary") == "healthy"
    assert hass.states.get(a).state == "fan_only"
    assert hass.states.get(b).state == "cool"
    await _exact(
        hass, freezer, timedelta(seconds=t0 + 1800 - dt_util.utcnow().timestamp())
    )
    await _settle(hass, freezer)
    assert _health(c) == _health(c, "secondary") == "stale"
    assert c.data["primary_demand"] == c.data["secondary_demand"] == "neutral"
    assert c.data["state"] == "cool"
    assert hass.states.get(a).state == hass.states.get(b).state == "fan_only"


# --- retained freshness regressions ------------------------------------------


async def test_unknown_source_contract_never_enforces(
    hass: HomeAssistant, freezer: Any
) -> None:
    # A cadence alone does not select an evidence basis.
    entry, _head, _ = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: 1, "evidence_basis": "unknown"},
    )
    await _advance(hass, freezer, timedelta(minutes=4))
    await _settle(hass, freezer)
    c = entry.runtime_data
    assert _health(c) == HEALTH_CADENCE_UNKNOWN
    assert c.data["primary_demand"] == "cool"


async def test_first_unchanged_report_exits_provisional_without_external_refresh(
    hass: HomeAssistant, freezer: Any
) -> None:
    entry, _, _ = await _setup_fresh(hass, freezer, primary={ZONE_REPORT_INTERVAL: 1})
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    assert _health(c) == HEALTH_AWAITING
    await _report(hass, SENSOR_A, HOT[False])
    await _settle(hass, freezer)
    assert _health(c) == HEALTH_HEALTHY


async def test_third_due_instant_requests_refresh_without_one_second_allowance(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _head, _ = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    deadline = hass.states.get(SENSOR_A).last_reported.timestamp() + 180
    await _advance(hass, freezer, timedelta(seconds=30))
    calls = []
    orig = c.async_request_refresh

    async def record():
        calls.append(dt_util.utcnow().timestamp() - deadline)
        await orig()

    monkeypatch.setattr(c, "async_request_refresh", record)
    await _exact(
        hass, freezer, timedelta(seconds=deadline - dt_util.utcnow().timestamp())
    )
    assert calls, "No freshness reconsideration was scheduled at the third due time"


async def test_half_second_late_report_does_not_hide_missed_deadline(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    entry, _head, _ = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        await _exact(hass, freezer, timedelta(seconds=180))
        await _exact(hass, freezer, timedelta(seconds=0.5))
        await _report(hass, SENSOR_A, HOT[False])
        await _advance(hass, freezer, timedelta(seconds=2))
        await _settle(hass, freezer)
        loss = [r.message for r in caplog.records if "past its" in r.message]
        assert len(loss) == 1, (
            "A report after the third due time hid the entire unhealthy episode"
        )


async def test_invalid_first_episode_logs_loss_and_recovery_once(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    entry, _, _ = await _setup_fresh(hass, freezer, primary={ZONE_REPORT_INTERVAL: 1})
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        hass.states.async_set(SENSOR_A, "unavailable")
        await hass.async_block_till_done()
        await _recompute(hass, entry)
        loss = [
            r.message
            for r in caplog.records
            if r.name.startswith("custom_components.mxz_coordinator")
            and SENSOR_A in r.message
        ]
        caplog.clear()
        await _report(hass, SENSOR_A, HOT[False])
        await _recompute(hass, entry)
        recovery = [r.message for r in caplog.records if "reporting again" in r.message]
        assert len(loss) == len(recovery) == 1


async def test_options_override_data_per_sensor(
    hass: HomeAssistant, freezer: Any
) -> None:
    entry, _, _ = await _setup_fresh(hass, freezer)
    zones = [dict(z) for z in entry.data[CONF_ZONES]]
    zones[0][ZONE_REPORT_INTERVAL] = 2
    hass.config_entries.async_update_entry(entry, options={CONF_ZONES: zones})
    await hass.async_block_till_done()
    c = entry.runtime_data
    assert c._freshness == {"primary": (360.0, 360.0)}
    assert _health(c) == HEALTH_AWAITING
    assert _health(c, "secondary") == HEALTH_CADENCE_UNKNOWN


async def test_stale_off_after_dry_preserves_existing_cooling_dwell(
    hass: HomeAssistant, freezer: Any
) -> None:
    entry, head, _ = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: 1},
        **{CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY, "coil_dry_minutes": 10},
    )
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    assert hass.states.get(head).state == "cool"
    await _advance(hass, freezer, timedelta(seconds=182))
    await _settle(hass, freezer)
    assert _health(entry.runtime_data) == HEALTH_STALE
    assert hass.states.get(head).state == "fan_only"
    await _advance(hass, freezer, timedelta(seconds=610))
    await _settle(hass, freezer)
    assert hass.states.get(head).state == "off"


async def test_first_unchanged_report_replaces_a_long_grace_with_its_deadline(
    hass: HomeAssistant, freezer: Any
) -> None:
    entry, head, _ = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1, "startup_grace": 10}
    )
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    assert _health(c) == HEALTH_AWAITING
    await _report(hass, SENSOR_A, HOT[False])
    await _settle(hass, freezer)
    await _exact(hass, freezer, timedelta(seconds=200))
    await _settle(hass, freezer)
    assert _health(c) == HEALTH_STALE, (
        "The first witnessed report must replace startup grace with its own "
        "maximum-age deadline"
    )
    assert hass.states.get(head).state == "fan_only"


# --- recovery and write-window regressions -----------------------------------


async def test_new_unchanged_report_reconsiders_previously_future_marker(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    entry, head, _ = await _setup_fresh(
        hass, freezer, primary=_sample_room(**{ZONE_REPORT_INTERVAL: 1})
    )
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report_marker(hass, HOT[False], _iso())
    await _settle(hass, freezer)
    await _exact(hass, freezer, timedelta(seconds=190))
    await _settle(hass, freezer)
    assert _health(c) == "stale"
    marker = _iso(60)
    await _report_marker(hass, HOT[False], marker)
    await _settle(hass, freezer)
    assert _health(c) == "stale"
    rejected_receipt = hass.states.get(SENSOR_A).last_reported.timestamp()
    last_accepted = c._sample_marker["primary"]
    await _exact(hass, freezer, timedelta(seconds=70))
    await c.async_request_refresh()
    await _settle(hass, freezer)
    assert _health(c) == "stale", "the rejected snapshot alone must not recover"
    now = dt_util.utcnow().timestamp()
    sample_time = dt_util.parse_datetime(marker).timestamp()
    assert sample_time > last_accepted and 0 <= now - sample_time < 180
    assert sample_evidence(
        marker=sample_time, last=last_accepted, now=now, receipt=now, marker_is_a_time=True
    ) == (sample_time, sample_time)
    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        await _report_marker(hass, HOT[False], marker)
        await _settle(hass, freezer)
        observed = {
            "health": _health(c),
            "demand": c.data["primary_demand"],
            "head": hass.states.get(head).state,
            "accepted": c._sample_marker["primary"],
            "marker": sample_time,
            "new_write": hass.states.get(SENSOR_A).last_reported.timestamp()
            > rejected_receipt,
            "recovery": sum("reporting again" in r.message for r in caplog.records),
        }
    # A changed marker is an independent positive recovery control.
    await _report_marker(hass, HOT[False], _iso())
    await _settle(hass, freezer)
    assert _health(c) == "healthy" and hass.states.get(head).state == "cool"
    assert observed["new_write"]
    assert observed["health"] == "healthy", observed
    assert observed["head"] == "cool" and observed["recovery"] == 1, observed


@pytest.mark.parametrize("guard", ["fan", "eco", "inhibit", "kill"])
@pytest.mark.parametrize(
    "subtype", ["stale", "missing", "unavailable", "unknown", "text", "nan", "inf", "unit"]
)
async def test_guards_across_unhealthy_subtypes(
    hass: HomeAssistant, freezer: Any, guard: str, subtype: str
) -> None:
    extra: dict[str, Any] = {CONF_FAN_BOOST_ENABLE: True}
    if guard == "inhibit":
        extra.update(
            {
                CONF_INHIBIT_ENTITY: "binary_sensor.review_grid",
                CONF_INHIBIT_ACTIVE_STATE: "on",
                CONF_INHIBIT_ACTION: INHIBIT_ACTION_FAN_ONLY,
            }
        )
    entry, head, head_b = await _setup_fresh(
        hass,
        freezer,
        primary=_sequence_room(**{ZONE_REPORT_INTERVAL: 1}),
        secondary={ZONE_REPORT_INTERVAL: 10},
        **extra,
    )
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report_marker(hass, HOT[False], 42, attribute=SEQUENCE_ATTR)
    await _report_marker(hass, HOT[False], 43, attribute=SEQUENCE_ATTR)
    await _report(hass, SENSOR_B, HOT[False])
    await _settle(hass, freezer)
    assert _health(c) == _health(c, "secondary") == "healthy"
    if guard == "fan":
        await _user_set_fan(hass, head, "quiet")
    elif guard in ("eco", "kill"):
        await hass.services.async_call(
            "switch",
            "turn_on" if guard == "eco" else "turn_off",
            {
                "entity_id": _eid(
                    hass, entry, "_eco_idle" if guard == "eco" else "_coordinator_enable"
                )
            },
            blocking=True,
        )
    else:
        hass.states.async_set("binary_sensor.review_grid", "on")
    await _settle(hass, freezer)
    calls = _record_calls(hass)
    if subtype == "stale":
        await _exact(hass, freezer, timedelta(seconds=190))
    elif subtype == "missing":
        hass.states.async_remove(SENSOR_A)
    elif subtype == "unit":
        hass.states.async_set(
            SENSOR_A, str(HOT[False]), {SEQUENCE_ATTR: 44, "unit_of_measurement": []}
        )
    else:
        hass.states.async_set(
            SENSOR_A, {"text": "not-a-number"}.get(subtype, subtype), {SEQUENCE_ATTR: 44}
        )
    await hass.async_block_till_done()
    await _settle(hass, freezer)
    assert _health(c) == ("stale" if subtype == "stale" else "invalid")
    assert c.data["primary_demand"] == "neutral"
    assert c.data["primary_temp"] == (HOT[False] if subtype == "stale" else None)
    assert c.data["secondary_temp"] == HOT[False]
    assert _health(c, "secondary") == "healthy"
    assert c.data["secondary_demand"] == ("neutral" if guard == "eco" else "cool")
    before_recovery = list(_head_calls(calls, head))
    if guard == "fan":
        assert not c.fan_auto_is_on(head)
        assert hass.states.get(head).attributes["fan_mode"] == "quiet"
        assert hass.states.get(head).state == "fan_only"
        assert hass.states.get(head_b).state == "cool"
    elif guard == "eco":
        assert hass.states.get(head).state == "off"
    elif guard == "inhibit":
        assert c.inhibited and hass.states.get(head).state == "fan_only"
    else:
        assert not before_recovery
    # Invalid-to-stale stays in the same spent episode, with the same baseline.
    if subtype != "stale":
        await _report_marker(hass, HOT[False], 43, attribute=SEQUENCE_ATTR)
        await _settle(hass, freezer)
        assert _health(c) == "stale" and c.data["primary_demand"] == "neutral"
    await _report_marker(hass, HOT[False], 45, attribute=SEQUENCE_ATTR)
    await _settle(hass, freezer)
    assert _health(c) == "healthy"
    if guard in ("fan", "inhibit"):
        assert not [call for call in _head_calls(calls, head) if call[0] == "set_fan_mode"]
    if guard == "kill":
        assert not _head_calls(calls, head) and not _head_calls(calls, head_b)


@pytest.mark.parametrize("basis", ["time", "sequence", "ha_write"])
async def test_reload_existing_sample_or_sequence_mid_episode(
    hass: HomeAssistant, freezer: Any, basis: str
) -> None:
    profile = (
        _sample_room()
        if basis == "time"
        else _sequence_room()
        if basis == "sequence"
        else {}
    )
    entry, _head, _ = await _setup_fresh(
        hass, freezer, primary={**profile, ZONE_REPORT_INTERVAL: 1}
    )
    old = entry.runtime_data
    await _drain_startup(hass, freezer)
    if basis == "ha_write":
        await _report(hass, SENSOR_A, HOT[False])
    else:
        await _report_marker(
            hass,
            HOT[False],
            _iso() if basis == "time" else 42,
            attribute=SAMPLE_ATTR if basis == "time" else SEQUENCE_ATTR,
        )
        if basis == "sequence":
            await _report_marker(hass, HOT[False], 43, attribute=SEQUENCE_ATTR)
    await _settle(hass, freezer)
    assert _health(old) == "healthy"
    evidence = old._evidence_ts.get("primary")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _drain_startup(hass, freezer)
    c = entry.runtime_data
    assert c is not old and old._fresh_retired and old._fresh_timer is None
    if basis == "time":
        assert _health(c) == "healthy" and c._evidence_ts["primary"] == evidence
    else:
        assert _health(c) == "awaiting_report" and "primary" not in c._evidence_ts
    await _exact(hass, freezer, timedelta(seconds=190))
    await _settle(hass, freezer)
    assert _health(c) == "stale"
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _drain_startup(hass, freezer)
    c = entry.runtime_data
    assert _health(c) == "awaiting_report", (
        "accepted no-retention tradeoff grants provisional grace"
    )
    assert c.data["primary_demand"] == "cool"


@pytest.mark.parametrize("baseline", [2**53, 2**63])
async def test_finite_integer_sequence_advance_is_preserved(
    hass: HomeAssistant, freezer: Any, baseline: int
) -> None:
    entry, head, _ = await _setup_fresh(
        hass, freezer, primary=_sequence_room(**{ZONE_REPORT_INTERVAL: 1})
    )
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report_marker(hass, HOT[False], baseline, attribute=SEQUENCE_ATTR)
    await _settle(hass, freezer)
    await _exact(hass, freezer, timedelta(seconds=190))
    await _settle(hass, freezer)
    assert _health(c) == "stale"
    await _report_marker(hass, HOT[False], baseline + 1, attribute=SEQUENCE_ATTR)
    await _settle(hass, freezer)
    observed = {
        "health": _health(c),
        "head": hass.states.get(head).state,
        "source": hass.states.get(SENSOR_A).attributes[SEQUENCE_ATTR],
        "accepted": c._sample_marker["primary"],
        "baseline": baseline,
    }
    # A larger increase survives float conversion and proves the recovery path.
    await _report_marker(hass, HOT[False], baseline + 4096, attribute=SEQUENCE_ATTR)
    await _settle(hass, freezer)
    assert _health(c) == "healthy" and hass.states.get(head).state == "cool"
    assert observed["source"] == baseline + 1
    assert observed["health"] == "healthy", observed
    assert observed["accepted"] == baseline + 1, observed


# --- restore metadata and receipt windows ------------------------------------


@pytest.mark.parametrize("basis", ["unknown", "ha_state_write"])
async def test_restored_attribute_does_not_select_an_evidence_basis(
    hass: HomeAssistant, freezer: Any, basis: str
) -> None:
    entry, head, _ = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1, ZONE_EVIDENCE_BASIS: basis}
    )
    c = entry.runtime_data
    await _drain_startup(hass, freezer)
    assert _health(c) == ("cadence_unknown" if basis == "unknown" else "awaiting_report")
    attrs = {"unit_of_measurement": "°F", "restored": True}
    hass.states.async_set(SENSOR_A, str(HOT[False]), attrs)
    await _settle(hass, freezer)
    assert _health(c) == ("cadence_unknown" if basis == "unknown" else "healthy")
    assert not c._evidence_ts
    if basis == "ha_state_write":
        first = c._fresh_deadline["primary"]
        await _exact(hass, freezer, timedelta(seconds=60))
        hass.states.async_set(SENSOR_A, str(HOT[False]), attrs)
        report_time = hass.states.get(SENSOR_A).last_reported.timestamp()
        await _settle(hass, freezer)
        # Healthy writes need not trigger compute; the old timer re-reads them.
        await _exact(
            hass, freezer, timedelta(seconds=first - dt_util.utcnow().timestamp())
        )
        await _settle(hass, freezer)
        assert _health(c) == "healthy" and c._fresh_deadline["primary"] == report_time + 180
        await _exact(
            hass,
            freezer,
            timedelta(seconds=report_time + 180 - dt_util.utcnow().timestamp()),
        )
        await _settle(hass, freezer)
        assert _health(c) == "stale" and hass.states.get(head).state == "fan_only"
    else:
        await _exact(hass, freezer, timedelta(seconds=500))
        await _settle(hass, freezer)
        assert _health(c) == "cadence_unknown" and hass.states.get(head).state == "cool"
