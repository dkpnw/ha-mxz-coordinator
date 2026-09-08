"""Freshness edges: what counts as EVIDENCE, and exactly when the deadline runs.

The companion module (`test_sensor_freshness.py`) covers the policy's ordinary
path with a contracted HA-write source. This one covers the parts a duration
alone cannot buy:

* the evidence BASIS is independent of the cadence and defaults to unknown, so
  a configured interval on an untrusted source enforces nothing;
* a trusted sample marker only counts while it ADVANCES — a cache flush that
  re-writes the same sample is not a report;
* the deadline callback fires at the boundary itself, and re-reads evidence
  there, so a report at the boundary wins and a report after it does not erase
  the episode it already missed;
* an unchanged report is what a provisional or unhealthy room is waiting for,
  and a healthy room's unchanged reports wake nothing.

The event-driven tests here recompute NOTHING on the coordinator's behalf: they
move the frozen clock, fire HA's timers at the exact instant under test, and
read what the product did by itself.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed_exact,
)

from custom_components.mxz_coordinator.const import (
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_ZONES,
    DEMAND_NEUTRAL,
    EVIDENCE_HA_WRITE,
    EVIDENCE_SAMPLE_TIMESTAMP,
    EVIDENCE_UNKNOWN,
    HEALTH_AWAITING,
    HEALTH_CADENCE_UNKNOWN,
    HEALTH_HEALTHY,
    HEALTH_INVALID,
    HEALTH_STALE,
    IDLE_ACTION_OFF_AFTER_DRY,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
    ZONE_EVIDENCE_BASIS,
    ZONE_MAX_AGE,
    ZONE_REPORT_INTERVAL,
    ZONE_SAMPLE_SEQUENCE_ATTR,
    ZONE_SAMPLE_TIMESTAMP_ATTR,
    ZONE_STARTUP_GRACE,
)
from custom_components.mxz_coordinator.coordinator import MXZCoordinator, _read_marker
from custom_components.mxz_coordinator.logic import evidence_contract, sample_evidence

from .test_drive import SENSOR_A, SENSOR_B, _eid, _recompute
from .test_sensor_freshness import (
    HOT,
    INTERVAL_MIN,
    NEUTRAL,
    WINDOW,
    _advance,
    _health,
    _report,
    _settle,
    _setup_fresh,
)

# The entity attributes these tests use as trusted markers. A real source names
# its own; nothing in the product knows these strings.
SAMPLE_ATTR = "last_sample_time"
SEQUENCE_ATTR = "sample_sequence"
# A reading that makes room A call HEAT (Fahrenheit rooms).
COLD = 65.0


# --- the pure evidence contract ----------------------------------------------


def test_the_basis_defaults_to_unknown() -> None:
    """No declared basis is the honest default: nothing to trust, nothing to enforce."""
    assert (
        evidence_contract(
            basis=None, sample_timestamp_attribute=None, sample_sequence_attribute=None
        )
        is None
    )
    assert (
        evidence_contract(
            basis=EVIDENCE_UNKNOWN,
            sample_timestamp_attribute=None,
            sample_sequence_attribute=None,
        )
        is None
    )


@pytest.mark.parametrize("basis", ["HA_STATE_WRITE", "ha state write", "", 1, True, []])
def test_an_unrecognised_basis_is_unknown(basis: Any) -> None:
    """A basis nobody implements is not a basis; it never becomes a cutoff."""
    assert (
        evidence_contract(
            basis=basis,
            sample_timestamp_attribute=SAMPLE_ATTR,
            sample_sequence_attribute=None,
        )
        is None
    )


def test_the_contracted_ha_write_basis_needs_no_marker() -> None:
    """The source contract IS the evidence there: every write is a reading."""
    assert evidence_contract(
        basis=EVIDENCE_HA_WRITE,
        sample_timestamp_attribute=None,
        sample_sequence_attribute=None,
    ) == (EVIDENCE_HA_WRITE, None, False)


def test_a_marker_basis_resolves_one_named_marker() -> None:
    """A sample time is a time; a sample sequence is not."""
    assert evidence_contract(
        basis=EVIDENCE_SAMPLE_TIMESTAMP,
        sample_timestamp_attribute=SAMPLE_ATTR,
        sample_sequence_attribute=None,
    ) == (EVIDENCE_SAMPLE_TIMESTAMP, SAMPLE_ATTR, True)
    assert evidence_contract(
        basis=EVIDENCE_SAMPLE_TIMESTAMP,
        sample_timestamp_attribute=None,
        sample_sequence_attribute=SEQUENCE_ATTR,
    ) == (EVIDENCE_SAMPLE_TIMESTAMP, SEQUENCE_ATTR, False)


@pytest.mark.parametrize(
    ("stamp", "sequence"),
    [
        (None, None),  # a marker basis with no marker to read
        (SAMPLE_ATTR, SEQUENCE_ATTR),  # two markers: which one is the evidence?
        ("", ""),
        (5, None),  # not an attribute name
    ],
    ids=["neither", "both", "empty", "not-a-name"],
)
def test_an_unusable_marker_declaration_enforces_nothing(
    stamp: Any, sequence: Any
) -> None:
    """An ambiguous or missing marker is not trustworthy evidence."""
    assert (
        evidence_contract(
            basis=EVIDENCE_SAMPLE_TIMESTAMP,
            sample_timestamp_attribute=stamp,
            sample_sequence_attribute=sequence,
        )
        is None
    )


# --- the pure marker rule -----------------------------------------------------


def test_an_advancing_sample_time_is_its_own_evidence_time() -> None:
    """The deadline runs from when the sample was TAKEN, not when HA wrote it."""
    assert sample_evidence(
        marker=1000.0, last=900.0, now=1010.0, receipt=1010.0, marker_is_a_time=True
    ) == (1000.0, 1000.0)


@pytest.mark.parametrize("marker", [900.0, 899.0])
def test_a_repeated_or_older_sample_time_moves_nothing(marker: float) -> None:
    """A cache flush re-writing the same sample is not a new report."""
    assert sample_evidence(
        marker=marker, last=900.0, now=1010.0, receipt=1010.0, marker_is_a_time=True
    ) == (900.0, None)


def test_a_future_sample_time_is_not_evidence() -> None:
    """A sample from the future is a clock fault, and would grant a free window."""
    assert sample_evidence(
        marker=1100.0, last=900.0, now=1010.0, receipt=1010.0, marker_is_a_time=True
    ) == (900.0, None)


def test_a_missing_marker_moves_nothing() -> None:
    assert sample_evidence(
        marker=None, last=900.0, now=1010.0, receipt=1010.0, marker_is_a_time=True
    ) == (900.0, None)


def test_the_first_sequence_is_a_baseline_not_an_advance() -> None:
    """Nothing has advanced yet, so nothing has been proved yet."""
    assert sample_evidence(
        marker=42.0, last=None, now=1010.0, receipt=1000.0, marker_is_a_time=False
    ) == (42.0, None)


def test_an_advancing_sequence_dates_from_its_receipt() -> None:
    """A sequence has no wall-clock meaning: HA's receipt time is the evidence time."""
    assert sample_evidence(
        marker=43.0, last=42.0, now=1010.0, receipt=1000.0, marker_is_a_time=False
    ) == (43.0, 1000.0)


@pytest.mark.parametrize("marker", [42.0, 41.0])
def test_an_equal_or_older_sequence_moves_nothing(marker: float) -> None:
    assert sample_evidence(
        marker=marker, last=42.0, now=1010.0, receipt=1000.0, marker_is_a_time=False
    ) == (42.0, None)


def test_a_naive_sample_time_is_not_a_sample_time() -> None:
    """A zoneless datetime names no instant, so it cannot date a sample."""
    aware = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
    assert _read_marker(aware.replace(tzinfo=None), marker_is_a_time=True) is None
    assert _read_marker(aware, marker_is_a_time=True) == aware.timestamp()
    assert _read_marker(aware.isoformat(), marker_is_a_time=True) == aware.timestamp()


@pytest.mark.parametrize("baseline", [2**53, 2**63], ids=["2**53", "2**63"])
def test_an_integer_sequence_keeps_its_exact_value(baseline: int) -> None:
    """A count past 2**53 still advances by one: it is never rounded through a float.

    Both floats and integers are finite numbers, and each stays what it is, so
    an advance of one above the float mantissa is still a strict increase. The
    unusable markers M13 already rejects stay rejected.
    """
    marker = _read_marker(baseline + 1, marker_is_a_time=False)
    assert marker == baseline + 1
    assert isinstance(marker, int), "an integer sequence is compared as an integer"
    assert sample_evidence(
        marker=marker, last=baseline, now=1010.0, receipt=1000.0, marker_is_a_time=False
    ) == (baseline + 1, 1000.0)
    assert sample_evidence(
        marker=baseline, last=baseline, now=1010.0, receipt=1000.0, marker_is_a_time=False
    ) == (baseline, None)
    assert _read_marker(43.5, marker_is_a_time=False) == 43.5
    for unusable in (float("nan"), float("inf"), -float("inf"), True, "44", None):
        assert _read_marker(unusable, marker_is_a_time=False) is None


# --- helpers ------------------------------------------------------------------


def _now() -> float:
    """The frozen clock, as the coordinator reads it."""
    return dt_util.utcnow().timestamp()


async def _exact(hass: HomeAssistant, freezer: Any, delta: timedelta) -> None:
    """Move the clock and fire HA's timers at EXACTLY the resulting instant.

    ``async_fire_time_changed`` rounds up into the next second, which is a
    second the boundary tests are measuring; this fires on the instant itself.
    """
    freezer.tick(delta)
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def _drain_startup(hass: HomeAssistant, freezer: Any) -> None:
    """Let the unrelated post-start recovery refresh happen and finish.

    It is armed 40 s after HA starts and has nothing to do with freshness, but
    it would otherwise land inside a boundary measurement and recompute for it.
    """
    await _exact(hass, freezer, timedelta(seconds=60))
    await _settle(hass, freezer)


async def _report_marker(
    hass: HomeAssistant,
    value: float,
    marker: Any,
    *,
    attribute: str = SAMPLE_ATTR,
    entity_id: str = SENSOR_A,
) -> None:
    """One source write carrying its trusted sample marker."""
    hass.states.async_set(
        entity_id,
        str(value),
        {
            ATTR_UNIT_OF_MEASUREMENT: hass.config.units.temperature_unit,
            attribute: marker,
        },
    )
    await hass.async_block_till_done()


def _iso(offset: float = 0.0) -> str:
    """The sample time an integration would publish, as an ISO string."""
    return (dt_util.utcnow() + timedelta(seconds=offset)).isoformat()


def _sample_room(**extra: Any) -> dict[str, Any]:
    """A room whose source publishes a trustworthy sample TIME."""
    return {
        ZONE_REPORT_INTERVAL: INTERVAL_MIN,
        ZONE_EVIDENCE_BASIS: EVIDENCE_SAMPLE_TIMESTAMP,
        ZONE_SAMPLE_TIMESTAMP_ATTR: SAMPLE_ATTR,
        **extra,
    }


def _sequence_room(**extra: Any) -> dict[str, Any]:
    """A room whose source publishes an advancing sample SEQUENCE."""
    return {
        ZONE_REPORT_INTERVAL: INTERVAL_MIN,
        ZONE_EVIDENCE_BASIS: EVIDENCE_SAMPLE_TIMESTAMP,
        ZONE_SAMPLE_SEQUENCE_ATTR: SEQUENCE_ATTR,
        **extra,
    }


# --- the basis gate, end to end -----------------------------------------------


async def test_unknown_source_contract_never_enforces(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A cadence on an UNTRUSTED source is not an enforcement configuration.

    The approved configuration table keeps the basis independent of the
    cadence and defaults it to unknown. So this room — a one-minute interval,
    silent for four — shows its age and keeps voting.
    """
    entry, head, _head_b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: 1, ZONE_EVIDENCE_BASIS: EVIDENCE_UNKNOWN},
    )
    await _advance(hass, freezer, timedelta(minutes=4))
    await _settle(hass, freezer)
    coord: MXZCoordinator = entry.runtime_data
    assert _health(coord) == HEALTH_CADENCE_UNKNOWN
    assert coord.data["primary_demand"] == MODE_COOL
    assert hass.states.get(head).state == MODE_COOL
    assert coord.data["primary_sensor_age"] >= 240, "the age is still shown"


async def test_a_duration_alone_is_reported_and_never_enforced(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The half-configured room says so once, rather than acquiring a cutoff."""
    with caplog.at_level(logging.WARNING, logger="custom_components.mxz_coordinator"):
        entry, head, _head_b = await _setup_fresh(
            hass,
            freezer,
            primary={ZONE_MAX_AGE: 4, ZONE_EVIDENCE_BASIS: EVIDENCE_UNKNOWN},
        )
    coord: MXZCoordinator = entry.runtime_data
    assert sum("no trusted evidence basis" in r.message for r in caplog.records) == 1
    assert coord._freshness == {}, "nothing is enforcement-capable here"
    await _advance(hass, freezer, timedelta(hours=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_CADENCE_UNKNOWN
    assert hass.states.get(head).state == MODE_COOL


async def test_a_marker_basis_without_a_usable_marker_enforces_nothing(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Two named markers are ambiguous; an ambiguous marker is not evidence."""
    entry, _head_a, _head_b = await _setup_fresh(
        hass,
        freezer,
        primary=_sample_room(**{ZONE_SAMPLE_SEQUENCE_ATTR: SEQUENCE_ATTR}),
    )
    coord: MXZCoordinator = entry.runtime_data
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_CADENCE_UNKNOWN


async def test_a_generic_write_does_not_upgrade_an_unknown_basis(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A restore-capable source keeps reporting; it still gets no deadline.

    This is the restored-value scenario the policy is most careful about: HA
    stamps the write, so the write age looks perfect. Without a source
    contract that proves acquisition, it stays `cadence_unknown` — and equally,
    it is never cut off.
    """
    entry, head, _head_b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: 1, ZONE_EVIDENCE_BASIS: EVIDENCE_UNKNOWN},
    )
    coord: MXZCoordinator = entry.runtime_data
    for _ in range(3):
        await _report(hass, SENSOR_A, HOT[False])
        await _advance(hass, freezer, timedelta(minutes=5))
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_CADENCE_UNKNOWN, "no write upgrades the basis"
    assert hass.states.get(head).state == MODE_COOL


# --- the sample-marker contract, end to end -----------------------------------


async def test_cache_writes_do_not_refresh_a_sample_deadline(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The room goes stale on its sample clock while HA writes keep arriving.

    An integration polling its cloud every five minutes writes to HA every
    five minutes whether or not the device reported. Only the sample marker
    knows the difference, so only the sample marker moves the deadline.
    """
    entry, head, _head_b = await _setup_fresh(hass, freezer, primary=_sample_room())
    coord: MXZCoordinator = entry.runtime_data
    sample = _iso()
    await _report_marker(hass, HOT[False], sample)
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY

    for _ in range(2):  # two cache flushes carrying the SAME sample
        await _advance(hass, freezer, timedelta(minutes=INTERVAL_MIN))
        await _report_marker(hass, HOT[False], sample)
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY, "still inside the sample's own window"

    # One more cache write, landing on the deadline instant itself: the HA
    # write is as new as a write can be, and the room is stale anyway.
    freezer.tick(WINDOW - timedelta(minutes=2 * INTERVAL_MIN))
    await _report_marker(hass, HOT[False], sample)
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert _health(coord) == HEALTH_STALE, "stale on the sample clock, not the write"
    assert coord.data["primary_sensor_age"] == 0, "though HA wrote this instant"
    assert coord.data["primary_demand"] == DEMAND_NEUTRAL
    assert hass.states.get(head).state == MODE_FAN_ONLY


async def test_an_advancing_sample_time_recovers_a_cached_source(
    hass: HomeAssistant, freezer: Any
) -> None:
    """One newer trustworthy sample, and the room is healthy on its own clock."""
    entry, head, _head_b = await _setup_fresh(hass, freezer, primary=_sample_room())
    coord: MXZCoordinator = entry.runtime_data
    await _report_marker(hass, HOT[False], _iso())
    await _recompute(hass, entry)
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE

    await _report_marker(hass, HOT[False], _iso())
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY
    assert hass.states.get(head).state == MODE_COOL
    # ...and the new deadline runs from the new sample, not from the old one.
    await _advance(hass, freezer, WINDOW - timedelta(minutes=1))
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY


@pytest.mark.parametrize("offset", [300.0, -3600.0], ids=["future", "out-of-order"])
async def test_an_unacceptable_sample_time_cannot_recover(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture, offset: float
) -> None:
    """A future or older sample time is diagnostic only: no deadline, no recovery."""
    entry, head, _head_b = await _setup_fresh(hass, freezer, primary=_sample_room())
    coord: MXZCoordinator = entry.runtime_data
    await _report_marker(hass, HOT[False], _iso())
    await _recompute(hass, entry)
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE

    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        await _report_marker(hass, HOT[False], _iso(offset))
        await _settle(hass, freezer)
        assert _health(coord) == HEALTH_STALE
        assert hass.states.get(head).state == MODE_FAN_ONLY
        assert not [r for r in caplog.records if "reporting again" in r.message]


@pytest.mark.parametrize("marker", ["not a time", "2026-09-08 10:00:00", 1757000000])
async def test_an_unusable_sample_time_is_not_evidence(
    hass: HomeAssistant, freezer: Any, marker: Any
) -> None:
    """A malformed, zoneless or non-time marker proves nothing about the sample.

    HA stores whatever a source publishes. A time with no zone cannot be
    compared against a UTC clock, and a bare number is not a sample time.
    """
    entry, _head_a, _head_b = await _setup_fresh(hass, freezer, primary=_sample_room())
    coord: MXZCoordinator = entry.runtime_data
    await _report_marker(hass, HOT[False], marker)
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE


async def test_an_advancing_sequence_dates_from_its_ha_receipt(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A sequence proves order, not time — so HA's receipt of the advance is the clock."""
    entry, head, _head_b = await _setup_fresh(hass, freezer, primary=_sequence_room())
    coord: MXZCoordinator = entry.runtime_data
    await _report_marker(hass, HOT[False], 42, attribute=SEQUENCE_ATTR)
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_AWAITING, "one sequence is a baseline, not evidence"

    await _advance(hass, freezer, timedelta(minutes=1))
    await _report_marker(hass, HOT[False], 43, attribute=SEQUENCE_ATTR)
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY

    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE, "the window ran from the advance"
    assert hass.states.get(head).state == MODE_FAN_ONLY


@pytest.mark.parametrize("marker", [43, 42, True, "44"], ids=["same", "older", "bool", "text"])
async def test_a_sequence_that_does_not_advance_cannot_recover(
    hass: HomeAssistant, freezer: Any, marker: Any
) -> None:
    """Only a strictly increasing number is an advance; a checkbox is not a sequence."""
    entry, _head_a, _head_b = await _setup_fresh(hass, freezer, primary=_sequence_room())
    coord: MXZCoordinator = entry.runtime_data
    await _report_marker(hass, HOT[False], 42, attribute=SEQUENCE_ATTR)
    await _advance(hass, freezer, timedelta(minutes=1))
    await _report_marker(hass, HOT[False], 43, attribute=SEQUENCE_ATTR)
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE

    await _report_marker(hass, HOT[False], marker, attribute=SEQUENCE_ATTR)
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE


async def test_an_unchanged_rewrite_of_the_accepted_marker_recovers_nothing(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The unchanged-write stream is observed, and the marker rule holds there too.

    A stale room's source re-sends exactly the write already accepted — same
    value, same sample time, so HA fires ``state_reported`` — and the record
    does not move: no evidence, no deadline, no recovery. A newer sample is
    the control that does recover it.
    """
    entry, head, _head_b = await _setup_fresh(
        hass, freezer, primary=_sample_room(**{ZONE_REPORT_INTERVAL: 1})
    )
    coord: MXZCoordinator = entry.runtime_data
    await _drain_startup(hass, freezer)
    sample = _iso()
    await _report_marker(hass, HOT[False], sample)
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY
    evidence = coord._evidence_ts["primary"]
    await _exact(hass, freezer, timedelta(seconds=190))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE

    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        await _report_marker(hass, HOT[False], sample)  # the same write, again
        assert hass.states.get(SENSOR_A).last_changed < hass.states.get(SENSOR_A).last_reported
        await _settle(hass, freezer)
        assert _health(coord) == HEALTH_STALE
        assert hass.states.get(head).state == MODE_FAN_ONLY
        assert coord._evidence_ts["primary"] == evidence, "the receipt did not move"
        assert "primary" not in coord._fresh_deadline
        assert not [r for r in caplog.records if "reporting again" in r.message]

    await _report_marker(hass, HOT[False], _iso())
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY
    assert hass.states.get(head).state == MODE_COOL


async def test_the_reload_notice_counts_the_rooms_that_start_on_their_own_sample_time(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The lifecycle notice says which rooms are provisional and which are not.

    Room A's sensor carries a sample time inside its maximum age when the
    entry reloads, so it dates itself and starts healthy; room B is on the
    contracted HA-write basis and needs a write made after the load. The one
    notice counts them apart. Room A's window then runs from its sample time,
    with no grace after it.
    """
    entry, head_a, head_b = await _setup_fresh(
        hass,
        freezer,
        primary=_sample_room(**{ZONE_REPORT_INTERVAL: 1}),
        secondary={ZONE_REPORT_INTERVAL: 1},
    )
    await _drain_startup(hass, freezer)
    sampled_at = _now()
    await _report_marker(hass, HOT[False], _iso())
    await _report(hass, SENSOR_B, HOT[False])
    await _settle(hass, freezer)
    old: MXZCoordinator = entry.runtime_data
    assert _health(old) == _health(old, "secondary") == HEALTH_HEALTHY

    await _exact(hass, freezer, timedelta(seconds=60))
    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        notices = [r.message for r in caplog.records if "not retained across a reload" in r.message]
    assert len(notices) == 1
    assert "1 room(s) are provisionally eligible" in notices[0]
    assert "1 room(s) start healthy on a sample time" in notices[0]
    coord: MXZCoordinator = entry.runtime_data
    assert coord is not old
    assert _health(coord) == HEALTH_HEALTHY
    assert coord._evidence_ts["primary"] == old._evidence_ts["primary"]
    assert coord._fresh_deadline["primary"] == sampled_at + 180
    assert _health(coord, "secondary") == HEALTH_AWAITING
    assert hass.states.get(head_a).state == hass.states.get(head_b).state == MODE_COOL

    await _exact(hass, freezer, timedelta(seconds=120))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE, "its window ran from the sample, not the load"
    assert hass.states.get(head_a).state == MODE_FAN_ONLY
    assert _health(coord, "secondary") == HEALTH_AWAITING, "B's grace runs from the load"


@pytest.mark.parametrize("invalid", ["missing", "unavailable", "nan"])
async def test_reload_notice_counts_only_eligible_rooms(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture, invalid: str
) -> None:
    """The r4 review's probe, retained: a room invalid at load is not provisional.

    Room A carries a sample time inside its maximum age when the entry
    reloads; room B's reading is missing, ``unavailable`` or NaN. B is invalid
    at once — neutral, parked, with neither a grace nor a deadline — and the
    one notice counts zero provisional rooms, not one.
    """
    entry, _head_a, head_b = await _setup_fresh(
        hass, freezer, primary=_sample_room(), secondary={ZONE_REPORT_INTERVAL: 5}
    )
    await _drain_startup(hass, freezer)
    await _report_marker(hass, HOT[False], _iso())
    await _settle(hass, freezer)
    if invalid == "missing":
        hass.states.async_remove(SENSOR_B)
    else:
        hass.states.async_set(SENSOR_B, invalid, {"unit_of_measurement": "°F"})
    await hass.async_block_till_done()
    await _settle(hass, freezer)
    assert _health(entry.runtime_data, "secondary") == "invalid"
    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        notices = [r.message for r in caplog.records if "not retained across a reload" in r.message]
    c = entry.runtime_data
    assert _health(c) == "healthy"
    assert _health(c, "secondary") == "invalid"
    assert c.data["secondary_demand"] == "neutral"
    assert hass.states.get(head_b).state == "fan_only"
    assert "secondary" not in c._grace_until and "secondary" not in c._fresh_deadline
    assert len(notices) == 1
    assert "1 room(s) start healthy on a sample time" in notices[0]
    assert "0 room(s) are provisionally eligible" in notices[0], notices[0]


async def test_a_restored_sample_does_not_survive_the_startup_grace(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A stored value from before this incarnation is old evidence, not new.

    Its marker is honest and old, so the grace runs out and the room parks —
    the write's HA timestamp never gets to speak for the sample.
    """
    entry, head, _head_b = await _setup_fresh(hass, freezer, primary=_sample_room())
    coord: MXZCoordinator = entry.runtime_data
    await _report_marker(hass, HOT[False], _iso(-3600))  # restored an hour late
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_AWAITING, "provisional, never `healthy`"
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE
    assert hass.states.get(head).state == MODE_FAN_ONLY


# --- the deadline instant -----------------------------------------------------


async def test_stale_at_the_third_due_instant_with_no_forced_recompute(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The event-driven twin of the core deadline case.

    Nothing here recomputes for the coordinator: the wakeup it armed is what
    has to fire at the boundary, and the boundary is the third due instant
    itself — not a second later.
    """
    entry, head, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY
    deadline = hass.states.get(SENSOR_A).last_reported.timestamp() + 180

    await _exact(hass, freezer, timedelta(seconds=deadline - _now() - 1))
    assert _health(coord) == HEALTH_HEALTHY, "one second short of the third due time"
    assert hass.states.get(head).state == MODE_COOL

    await _exact(hass, freezer, timedelta(seconds=1))
    assert _now() == deadline
    assert _health(coord) == HEALTH_STALE, "the boundary instant itself is stale"
    assert coord.data["primary_demand"] == DEMAND_NEUTRAL
    assert hass.states.get(head).state == MODE_FAN_ONLY, "parked at the idle action"


async def test_third_due_instant_requests_refresh_without_one_second_allowance(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wakeup lands ON the deadline; no allowance is added to it."""
    entry, head, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    deadline = hass.states.get(SENSOR_A).last_reported.timestamp() + 180
    await _advance(hass, freezer, timedelta(seconds=30))
    calls: list[float] = []
    orig = coord.async_request_refresh

    async def record() -> None:
        calls.append(_now() - deadline)
        await orig()

    monkeypatch.setattr(coord, "async_request_refresh", record)
    await _exact(hass, freezer, timedelta(seconds=deadline - _now()))
    assert calls, "No freshness reconsideration was scheduled at the third due time"
    assert calls == [0.0], "and none of it is a jitter allowance"
    assert _health(coord) == HEALTH_STALE
    assert hass.states.get(head).state == MODE_FAN_ONLY


async def test_a_report_at_the_deadline_instant_wins_over_expiry(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The callback carries no decision: it re-reads the evidence of its own instant."""
    entry, head, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _settle(hass, freezer)
    deadline = hass.states.get(SENSOR_A).last_reported.timestamp() + 180

    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        freezer.tick(timedelta(seconds=deadline - _now()))
        await _report(hass, SENSOR_A, HOT[False])  # processed AT the boundary
        async_fire_time_changed_exact(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert _health(coord) == HEALTH_HEALTHY
        assert hass.states.get(head).state == MODE_COOL
        assert not [r for r in caplog.records if "past its" in r.message]


async def test_half_second_late_report_does_not_hide_missed_deadline(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A report half a second late is a recovery, not an alibi.

    The episode is opened at the deadline the sensor missed, and the late
    report closes it — one loss, then one recovery, in that order.
    """
    entry, head, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)

    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        await _exact(hass, freezer, timedelta(seconds=180))
        assert (_health(coord), hass.states.get(head).state) == (
            HEALTH_STALE,
            MODE_FAN_ONLY,
        )
        await _exact(hass, freezer, timedelta(seconds=0.5))
        await _report(hass, SENSOR_A, HOT[False])
        await _advance(hass, freezer, timedelta(seconds=2))
        await _settle(hass, freezer)
        loss = [r.message for r in caplog.records if "past its" in r.message]
        recovery = [r.message for r in caplog.records if "reporting again" in r.message]
        assert len(loss) == 1, "A report after the third due time hid the episode"
        assert len(recovery) == 1
        assert _health(coord) == HEALTH_HEALTHY


# --- provisional rooms and the report stream ----------------------------------


async def test_first_unchanged_report_exits_provisional_without_external_refresh(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The report the provisional room is waiting for is often an unchanged one."""
    entry, _head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _drain_startup(hass, freezer)
    assert _health(coord) == HEALTH_AWAITING
    await _report(hass, SENSOR_A, HOT[False])
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY


async def test_first_unchanged_report_replaces_a_long_grace_with_its_deadline(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A generous grace ends when evidence arrives — it is not a licence to run.

    Ten minutes of grace, a three-minute window: after the first witnessed
    report the room lives on its OWN deadline, and 200 s of silence parks it.
    """
    entry, head, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1, ZONE_STARTUP_GRACE: 10}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _drain_startup(hass, freezer)
    assert _health(coord) == HEALTH_AWAITING
    await _report(hass, SENSOR_A, HOT[False])
    await _settle(hass, freezer)
    await _exact(hass, freezer, timedelta(seconds=200))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE, (
        "The first witnessed report must replace startup grace with its own "
        "maximum-age deadline"
    )
    assert hass.states.get(head).state == MODE_FAN_ONLY


async def test_a_healthy_room_ignores_its_own_unchanged_reports(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report stream is a recovery path, not a recompute treadmill.

    A healthy room's periodic write changes nothing it does not already know;
    its deadline is re-read when the wakeup it already armed fires.
    """
    entry, _head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY

    calls: list[float] = []
    orig = coord.async_request_refresh

    async def record() -> None:
        calls.append(_now())
        await orig()

    monkeypatch.setattr(coord, "async_request_refresh", record)
    for _ in range(3):
        await _advance(hass, freezer, timedelta(minutes=1))
        await _report(hass, SENSOR_A, HOT[False])
    assert calls == [], "an unchanged report woke a room that had nothing to decide"
    assert _health(coord) == HEALTH_HEALTHY


# --- unhealthy episodes -------------------------------------------------------


async def test_invalid_first_episode_logs_loss_and_recovery_once(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """An episode that begins with an unavailable sensor is still one episode."""
    entry, _head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    coord: MXZCoordinator = entry.runtime_data
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
        assert _health(coord) == HEALTH_INVALID
        caplog.clear()
        await _report(hass, SENSOR_A, HOT[False])
        await _recompute(hass, entry)
        recovery = [r.message for r in caplog.records if "reporting again" in r.message]
        assert len(loss) == len(recovery) == 1


async def test_an_unknown_cadence_room_recovers_to_cadence_unknown(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A room with no cutoff still has availability episodes — and comes back to unknown."""
    entry, head, _head_b = await _setup_fresh(hass, freezer)
    coord: MXZCoordinator = entry.runtime_data
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_CADENCE_UNKNOWN

    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        hass.states.async_set(SENSOR_A, "unavailable")
        await hass.async_block_till_done()
        await _recompute(hass, entry)
        assert _health(coord) == HEALTH_INVALID
        assert hass.states.get(head).state == MODE_FAN_ONLY, "parked while unusable"
        assert sum("no usable reading" in r.message for r in caplog.records) == 1

        await _report(hass, SENSOR_A, HOT[False])
        await _recompute(hass, entry)
        assert _health(coord) == HEALTH_CADENCE_UNKNOWN, "not `healthy`: nothing proves it"
        recovery = [r for r in caplog.records if "reporting again" in r.message]
        assert len(recovery) == 1
        assert HEALTH_CADENCE_UNKNOWN in recovery[0].message
        assert hass.states.get(head).state == MODE_COOL


async def test_one_episode_across_the_invalid_and_stale_subtypes(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Unavailable, then valid-but-unproved: one failure line, then one recovery."""
    entry, _head_a, _head_b = await _setup_fresh(hass, freezer, primary=_sample_room())
    coord: MXZCoordinator = entry.runtime_data
    await _report_marker(hass, HOT[False], _iso())
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY

    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        hass.states.async_set(SENSOR_A, "unavailable")
        await hass.async_block_till_done()
        await _recompute(hass, entry)
        assert _health(coord) == HEALTH_INVALID

        # The value comes back, but its sample marker is the one that expired:
        # the subtype changes inside the SAME episode, and says nothing new.
        await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
        await _report_marker(hass, HOT[False], _iso(-1800))
        await _recompute(hass, entry)
        assert _health(coord) == HEALTH_STALE
        assert sum("no usable reading" in r.message for r in caplog.records) == 1
        assert not [r for r in caplog.records if "past its" in r.message]
        assert not [r for r in caplog.records if "reporting again" in r.message]

        await _report_marker(hass, HOT[False], _iso())
        await _recompute(hass, entry)
        assert _health(coord) == HEALTH_HEALTHY
        assert sum("reporting again" in r.message for r in caplog.records) == 1


async def test_a_stale_room_that_disappears_does_not_log_twice(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The other direction of the same episode rule."""
    entry, _head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)

    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        await _exact(hass, freezer, timedelta(seconds=180))
        assert _health(coord) == HEALTH_STALE
        hass.states.async_set(SENSOR_A, "unavailable")
        await hass.async_block_till_done()
        await _recompute(hass, entry)
        assert _health(coord) == HEALTH_INVALID
        assert sum("past its" in r.message for r in caplog.records) == 1
        assert not [r for r in caplog.records if "no usable reading" in r.message]


# --- preserved behaviour under the corrected paths ----------------------------


async def test_options_override_data_per_sensor(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The freshness configuration comes through the ordinary options merge."""
    entry, _head_a, _head_b = await _setup_fresh(hass, freezer)
    zones = [dict(zone) for zone in entry.data[CONF_ZONES]]
    zones[0][ZONE_REPORT_INTERVAL] = 2
    hass.config_entries.async_update_entry(entry, options={CONF_ZONES: zones})
    await hass.async_block_till_done()
    coord: MXZCoordinator = entry.runtime_data
    assert coord._freshness == {"primary": (360.0, 360.0)}
    assert _health(coord) == HEALTH_AWAITING
    assert _health(coord, "secondary") == HEALTH_CADENCE_UNKNOWN


async def test_stale_off_after_dry_preserves_existing_cooling_dwell(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Parking is the EXISTING idle mapping: the dry dwell it finds is the one it uses."""
    entry, head, _head_b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: 1},
        **{CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY, "coil_dry_minutes": 10},
    )
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    assert hass.states.get(head).state == MODE_COOL
    await _advance(hass, freezer, timedelta(seconds=182))
    await _settle(hass, freezer)
    assert _health(entry.runtime_data) == HEALTH_STALE
    assert hass.states.get(head).state == MODE_FAN_ONLY
    await _advance(hass, freezer, timedelta(seconds=610))
    await _settle(hass, freezer)
    assert hass.states.get(head).state == MODE_OFF


# --- evidence is observed once, at the write that carried it -------------------


async def test_a_cached_return_after_an_outage_needs_a_new_sample_on_the_ha_write_basis_too(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The contracted basis has no cached case: its every write IS a new sample.

    A sensor that goes unavailable and comes back recovers on that return
    write, because its contract says the write is a current reading — the
    control for the marker rooms, whose same-marker return recovers nothing.
    """
    entry, head, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY
    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        hass.states.async_set(SENSOR_A, "unavailable")
        await hass.async_block_till_done()
        await _settle(hass, freezer)
        assert _health(coord) == HEALTH_INVALID
        caplog.clear()
        await _report(hass, SENSOR_A, HOT[False])
        await _settle(hass, freezer)
        assert _health(coord) == HEALTH_HEALTHY
        assert hass.states.get(head).state == MODE_COOL
        assert sum("reporting again" in r.message for r in caplog.records) == 1


# --- the loss record names what expired ---------------------------------------


async def test_the_loss_record_names_the_expired_sample_and_the_write_age_apart(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A cached source expires on its sample clock while HA writes it this instant.

    The record has to say which: the sample is 900 s old and past its maximum
    age, and Home Assistant's write is 0 s old — given as a diagnostic, not as
    the cause.
    """
    entry, _head_a, _head_b = await _setup_fresh(hass, freezer, primary=_sample_room())
    coord: MXZCoordinator = entry.runtime_data
    sample = _iso()
    await _report_marker(hass, HOT[False], sample)
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY
    with caplog.at_level(logging.WARNING, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        freezer.tick(WINDOW)
        await _report_marker(hass, HOT[False], sample)  # cache write AT the deadline
        async_fire_time_changed_exact(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert _health(coord) == HEALTH_STALE
        loss = [r.message for r in caplog.records if "past its" in r.message]
        assert len(loss) == 1
        assert "its last qualifying report is 900s old, past its 900s maximum age" in loss[0]
        assert "(Home Assistant last wrote the entity 0s ago)" in loss[0]


async def test_the_loss_record_names_an_expired_grace_when_nothing_was_witnessed(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """No report was ever witnessed: the grace expired, not a report's age."""
    entry, _head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: 1}
    )
    coord: MXZCoordinator = entry.runtime_data
    with caplog.at_level(logging.WARNING, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        await _drain_startup(hass, freezer)
        assert _health(coord) == HEALTH_AWAITING
        await _exact(hass, freezer, timedelta(seconds=180 - 60 - 1))
        assert _health(coord) == HEALTH_STALE, "the grace is 180 s from creation"
        loss = [r.message for r in caplog.records if "startup grace" in r.message]
        assert len(loss) == 1
        assert "no report was witnessed within its 180s startup grace" in loss[0]
        assert "Home Assistant last wrote the entity" in loss[0]
        assert not [r for r in caplog.records if "past its" in r.message]


# --- M23 scenarios executed with their exact preconditions --------------------


async def test_recovery_re_enters_arbitration_without_bypassing_the_mode_dwell(
    hass: HomeAssistant, freezer: Any
) -> None:
    """M23 scenario 206, with a real dwell and an opposing vote.

    Given: room A is stale and its reading calls HEAT while the shared mode is
    COOL, room B votes cool, and the 400 s dwell since the mode was set has
    not expired. When: one fresh valid report arrives. Then: room A is healthy
    immediately and its heat vote is back in arbitration (and wins the
    standoff on priority) — but the flip waits for the dwell, and lands the
    moment the dwell expires. The dwell is shorter than two of room A's
    windows so that the recovered vote is still standing when it expires.
    """
    entry, head_a, _head_b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: 1},
        secondary={},
        **{CONF_MODE_HYSTERESIS: 400},
    )
    coord: MXZCoordinator = entry.runtime_data
    started = coord._last_mode_change_ts
    await _report(hass, SENSOR_B, HOT[False])  # room B votes cool throughout
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, COLD)  # room A now calls heat, against the mode
    await _settle(hass, freezer)
    assert (_health(coord), coord.data["primary_demand"]) == (HEALTH_HEALTHY, MODE_HEAT)
    assert coord.data["secondary_demand"] == MODE_COOL
    assert coord.data["state"] == MODE_COOL
    assert coord.data["mode_change_allowed"] is False, "the dwell is holding the flip"

    await _exact(hass, freezer, timedelta(seconds=coord._fresh_deadline["primary"] - _now()))
    assert _health(coord) == HEALTH_STALE
    assert coord.data["primary_demand"] == DEMAND_NEUTRAL
    assert coord.data["state"] == MODE_COOL
    assert _now() < started + 400, "still inside the dwell"

    await _report(hass, SENSOR_A, COLD)  # one fresh valid report, same reading
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY, "recovered immediately"
    assert coord.data["primary_demand"] == MODE_HEAT, "back in arbitration"
    assert coord.data["standoff"] is True
    assert coord.data["state"] == MODE_COOL, "the dwell was not bypassed"
    assert coord.data["mode_change_allowed"] is False
    assert hass.states.get(head_a).state != MODE_HEAT

    await _exact(hass, freezer, timedelta(seconds=started + 400 - _now()))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY, "the recovered vote is still standing"
    assert coord.data["state"] == MODE_HEAT, "and the dwell, once over, lets it through"
    assert hass.states.get(head_a).state == MODE_HEAT


@pytest.mark.parametrize("observed", [MODE_COOL, MODE_FAN_ONLY])
async def test_stale_off_after_dry_uses_the_dwell_reconstructed_after_a_reload(
    hass: HomeAssistant, freezer: Any, observed: str
) -> None:
    """M23 scenario 237: no in-memory cooling record, the head observed mid-dwell.

    Given: a reload builds a coordinator with no cooling record, and room A's
    head is observed in `cool` (the coordinator was disabled across the
    reload, so nothing was written) or in `fan_only` (the old incarnation had
    parked it into its dwell). When: room A goes stale under `off_after_dry`.
    Then: the head is in `fan_only` until the deadline the EXISTING
    reconstruction stamped — ten minutes from the seed, not from the stale
    transition — and `off` after it.
    """
    entry, head_a, _head_b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: 1},
        **{CONF_IDLE_ACTION: IDLE_ACTION_OFF_AFTER_DRY, "coil_dry_minutes": 10},
    )
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == MODE_COOL
    if observed == MODE_FAN_ONLY:
        await _report(hass, SENSOR_A, NEUTRAL[False])  # satisfied: the old dwell begins
        await _recompute(hass, entry)
    else:
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": _eid(hass, entry, "_coordinator_enable")},
            blocking=True,
        )
    await _advance(hass, freezer, timedelta(minutes=1))
    assert hass.states.get(head_a).state == observed

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coord: MXZCoordinator = entry.runtime_data
    reload_at = _now()
    await _drain_startup(hass, freezer)
    assert _health(coord) == HEALTH_AWAITING
    if observed == MODE_COOL:
        assert coord.coordinator_enable is False, "restored off: nothing written"
        assert coord._last_active == {}, "and no dwell reconstructed yet"
        # Re-enable the coordinator after the grace has run out, so the first
        # thing it may write is the stale park itself.
        await _exact(hass, freezer, timedelta(seconds=reload_at + 180 - _now()))
        assert _health(coord) == HEALTH_STALE
        assert hass.states.get(head_a).state == MODE_COOL
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": _eid(hass, entry, "_coordinator_enable")},
            blocking=True,
        )
        await _settle(hass, freezer)
    else:
        await _exact(hass, freezer, timedelta(seconds=reload_at + 180 - _now()))
        assert _health(coord) == HEALTH_STALE
    seeded = coord._last_active[head_a]
    assert seeded[0] == MODE_COOL, "the EXISTING reconstruction: observed = cooling"
    assert seeded[1] >= reload_at, "stamped by this incarnation, not carried over"
    if observed == MODE_FAN_ONLY:
        assert seeded[1] == reload_at, "seeded at the first idle resolution, on reload"
    else:
        assert seeded[1] >= reload_at + 180, "seeded at the park itself"
    assert hass.states.get(head_a).state == MODE_FAN_ONLY
    deadline = seeded[1] + 600

    await _exact(hass, freezer, timedelta(seconds=deadline - 1 - _now()))
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == MODE_FAN_ONLY, "still inside the dwell"
    assert coord._last_active[head_a] == seeded, "the stale transition restarted nothing"
    await _exact(hass, freezer, timedelta(seconds=2))
    await _settle(hass, freezer)
    assert hass.states.get(head_a).state == MODE_OFF, "off at the reconstructed deadline"
    assert _health(coord) == HEALTH_STALE
    if observed == MODE_FAN_ONLY:
        assert deadline < reload_at + 180 + 600, "not ten minutes from going stale"


async def test_every_automatic_room_unhealthy_parks_both_under_a_retained_cool_token(
    hass: HomeAssistant, freezer: Any
) -> None:
    """M23 scenario 284: room A stale, room B unavailable, the mode still says cool.

    The shared-mode token is allowed to keep its history; it is not permission
    to condition. Both heads park, neither room votes, and `sensors_ok` says so.
    """
    entry, head_a, head_b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: 1},
        secondary={ZONE_REPORT_INTERVAL: 1},
    )
    coord: MXZCoordinator = entry.runtime_data
    await _drain_startup(hass, freezer)
    await _report(hass, SENSOR_A, HOT[False])
    await _report(hass, SENSOR_B, HOT[False])
    await _settle(hass, freezer)
    assert coord.data["state"] == MODE_COOL
    assert hass.states.get(head_a).state == hass.states.get(head_b).state == MODE_COOL

    hass.states.async_set(SENSOR_B, "unavailable")
    await hass.async_block_till_done()
    await _exact(hass, freezer, timedelta(seconds=coord._fresh_deadline["primary"] - _now()))
    await _settle(hass, freezer)
    assert _health(coord, "primary") == HEALTH_STALE
    assert _health(coord, "secondary") == HEALTH_INVALID
    assert coord.data["state"] == MODE_COOL, "the token keeps its history"
    assert coord.data["primary_demand"] == coord.data["secondary_demand"] == DEMAND_NEUTRAL
    assert coord.data["sensors_ok"] is False
    assert hass.states.get(head_a).state == hass.states.get(head_b).state == MODE_FAN_ONLY
