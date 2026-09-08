"""Room-sensor freshness: a sensor that stops reporting stops steering the house.

The approved policy is per SENSOR and evidence-based. A room whose sensor
carries a documented reporting interval (or an explicit maximum age) goes
`stale` at the third due instant — two missed reports tolerated, no more — and
is then excluded from automatic demand and parked at the idle action, while
every healthy room carries on. A room with no configured cadence keeps an
unknown cadence: its age is shown and never enforced. There is no universal
cutoff anywhere in this file, and none in the product.

Time idiom: freshness compares wall clocks (``dt_util.utcnow`` against the
entity's ``last_reported``), so these tests move a REAL frozen clock with
``freezer`` and then fire HA's timer machinery — unlike the dwell tests, which
rewind the coordinator's own stamp because they measure an interval the
coordinator itself stamped.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    REQUEST_REFRESH_DEFAULT_COOLDOWN,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import (
    METRIC_SYSTEM,
    US_CUSTOMARY_SYSTEM,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_INHIBIT_ACTION,
    CONF_INHIBIT_ACTIVE_STATE,
    CONF_INHIBIT_ENTITY,
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    CONF_ZONES,
    DEMAND_NEUTRAL,
    DOMAIN,
    ENGAGE_SATISFIED,
    EVIDENCE_HA_WRITE,
    HEALTH_AWAITING,
    HEALTH_CADENCE_UNKNOWN,
    HEALTH_HEALTHY,
    HEALTH_INVALID,
    HEALTH_STALE,
    IDLE_ACTION_OFF,
    INHIBIT_ACTION_FAN_ONLY,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_OFF,
    ZONE_CLIMATE,
    ZONE_EVIDENCE_BASIS,
    ZONE_MAX_AGE,
    ZONE_NAME,
    ZONE_REPORT_INTERVAL,
    ZONE_SENSOR,
    ZONE_STARTUP_GRACE,
)
from custom_components.mxz_coordinator.coordinator import (
    MXZCoordinator,
)
from custom_components.mxz_coordinator.logic import freshness_window, sensor_health

from .test_drive import (
    SENSOR_A,
    SENSOR_B,
    MockHead,
    MockHeadC,
    _eid,
    _recompute,
    _set_temp,
    _setup_mock_heads,
    _user_set_fan,
)
from .test_idle_action import _head_calls, _record_calls

# --- the pure freshness math -------------------------------------------------


def test_reporting_interval_derives_three_missed_reports() -> None:
    """P alone -> 3P and an equal grace: stale at the third due instant."""
    assert freshness_window(report_interval=5, max_age=None, startup_grace=None) == (
        15.0,
        15.0,
    )


def test_explicit_maximum_age_overrides_the_derivation() -> None:
    """An explicit maximum age wins over 3P, and still seeds the grace."""
    assert freshness_window(report_interval=5, max_age=20, startup_grace=None) == (
        20.0,
        20.0,
    )
    # ...and is enough on its own: a documented max age needs no cadence.
    assert freshness_window(report_interval=None, max_age=20, startup_grace=None) == (
        20.0,
        20.0,
    )


def test_a_maximum_age_shorter_than_one_report_is_no_profile() -> None:
    """A window narrower than the sensor's own cadence would park a healthy room.

    It is not raised to the interval either: a profile nobody wrote is not a
    cutoff. The invalid profile is rejected whole — equal to the interval is
    the shortest maximum age there is.
    """
    assert freshness_window(report_interval=10, max_age=4, startup_grace=None) is None
    assert freshness_window(report_interval=10, max_age=10, startup_grace=None) == (
        10.0,
        10.0,
    )


@pytest.mark.parametrize(
    ("interval", "max_age", "grace"),
    [
        (0, 20, None),
        (-1, 20, None),
        (float("nan"), 20, None),
        (float("inf"), 20, None),
        ("5", 20, None),
        (True, 20, None),
        (5, 0, None),
        (5, -15, None),
        (5, float("nan"), None),
        (5, float("inf"), None),
        (5, "15", None),
        (5, None, 0),
        (5, None, -1),
        (5, None, float("inf")),
        (5, None, "5"),
        (5, 4.999, None),
    ],
    ids=[
        "zero-interval", "negative-interval", "nan-interval", "inf-interval",
        "text-interval", "bool-interval", "zero-max-age", "negative-max-age",
        "nan-max-age", "inf-max-age", "text-max-age", "zero-grace",
        "negative-grace", "inf-grace", "text-grace", "max-age-below-interval",
    ],
)
def test_an_invalid_profile_is_rejected_whole(
    interval: Any, max_age: Any, grace: Any
) -> None:
    """One bad duration beside good ones is an invalid profile, not a partial one.

    Every row here has enough VALID material to enforce something (a usable
    interval or maximum age); the parser must not read past the bad value and
    build a cutoff out of the rest.
    """
    assert (
        freshness_window(report_interval=interval, max_age=max_age, startup_grace=grace)
        is None
    )


def test_startup_grace_overrides_the_equal_default() -> None:
    assert freshness_window(report_interval=5, max_age=None, startup_grace=2) == (
        15.0,
        2.0,
    )


@pytest.mark.parametrize(
    "interval",
    [None, 0, -1, float("nan"), float("inf"), "5", True, [], {}],
)
def test_an_unusable_interval_is_unset_not_a_cutoff(interval: Any) -> None:
    """Nothing but a finite positive number is a duration; the rest enforce nothing.

    ``True`` is in this list on purpose: ``isinstance(True, int)`` is true, and a
    checkbox that leaked into a duration field must not become a 3-minute cutoff.
    """
    assert (
        freshness_window(report_interval=interval, max_age=None, startup_grace=None)
        is None
    )


def test_a_grace_alone_configures_nothing() -> None:
    """A grace with no window to grant is not an enforcement configuration."""
    assert (
        freshness_window(report_interval=None, max_age=None, startup_grace=10) is None
    )


def test_invalid_precedes_every_age_question() -> None:
    """M13's value handling comes first: freshness cannot make a bad value good."""
    assert (
        sensor_health(valid=False, max_age=900, evidence_age=0.0, in_grace=True)
        == HEALTH_INVALID
    )
    assert (
        sensor_health(valid=False, max_age=None, evidence_age=None, in_grace=False)
        == HEALTH_INVALID
    )


def test_unknown_cadence_is_never_stale() -> None:
    """No configured window -> no cutoff, however old the last write is."""
    assert (
        sensor_health(valid=True, max_age=None, evidence_age=7200.0, in_grace=False)
        == HEALTH_CADENCE_UNKNOWN
    )


def test_health_boundary_is_half_open_at_the_third_due_instant() -> None:
    """`age < max_age` is healthy; the deadline instant itself is stale."""
    assert (
        sensor_health(valid=True, max_age=900, evidence_age=899.999, in_grace=False)
        == HEALTH_HEALTHY
    )
    assert (
        sensor_health(valid=True, max_age=900, evidence_age=900.0, in_grace=False)
        == HEALTH_STALE
    )


def test_fresh_means_the_evidence_deadline_is_still_in_the_future() -> None:
    """An already-expired report recovers nothing, even arriving this second.

    A cached integration flushing a backlog delivers real, advancing evidence
    that is nonetheless older than the window. Recovering on it would log a
    recovery and a failure per flush; it is simply not fresh.
    """
    assert (
        sensor_health(valid=True, max_age=900, evidence_age=1800.0, in_grace=False)
        == HEALTH_STALE
    )


def test_a_write_stamped_in_the_future_is_not_evidence() -> None:
    assert (
        sensor_health(valid=True, max_age=900, evidence_age=-30.0, in_grace=False)
        == HEALTH_STALE
    )


def test_grace_is_provisional_not_fresh() -> None:
    """No witnessed report yet: eligible during grace, stale the moment it ends."""
    assert (
        sensor_health(valid=True, max_age=900, evidence_age=None, in_grace=True)
        == HEALTH_AWAITING
    )
    assert (
        sensor_health(valid=True, max_age=900, evidence_age=None, in_grace=False)
        == HEALTH_STALE
    )


def test_evidence_beats_grace() -> None:
    """A witnessed fresh report ends the provisional state immediately."""
    assert (
        sensor_health(valid=True, max_age=900, evidence_age=1.0, in_grace=True)
        == HEALTH_HEALTHY
    )


# Readings that make room A call COOL and leave room B neutral, per unit system.
HOT = {False: 75.0, True: 26.0}
NEUTRAL = {False: 70.0, True: 21.0}
# The cadence under test: 5 minutes -> a 15-minute window (three due reports).
INTERVAL_MIN = 5
WINDOW = timedelta(minutes=3 * INTERVAL_MIN)


async def _setup_fresh(
    hass: HomeAssistant,
    freezer: Any,
    *,
    celsius: bool = False,
    primary: dict[str, Any] | None = None,
    secondary: dict[str, Any] | None = None,
    **extra: Any,
) -> tuple[MockConfigEntry, str, str]:
    """Two rooms, room A hot (calling cool) and room B neutral.

    ``primary``/``secondary`` carry that room's freshness configuration, on top
    of a contracted `ha_state_write` basis: the sensor these rooms model is a
    periodic one whose own documentation says every write it makes is a current
    reading. A test that wants the PRODUCT default (an unknown basis) passes
    ``evidence_basis`` itself, and a room given no duration keeps an unknown
    cadence — exactly like every entry written before this feature existed. Fan
    boost is off and the dwell is 0 so the only head writes in a measured
    window are the mode/setpoint ones this test caused.
    """
    contracted = {ZONE_EVIDENCE_BASIS: EVIDENCE_HA_WRITE}
    hass.config.units = METRIC_SYSTEM if celsius else US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(
        hass, cls=MockHeadC if celsius else MockHead
    )
    await _set_temp(hass, SENSOR_A, HOT[celsius])
    await _set_temp(hass, SENSOR_B, NEUTRAL[celsius])
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_ZONES: [
                {
                    ZONE_NAME: "Room A",
                    ZONE_CLIMATE: head_a,
                    ZONE_SENSOR: SENSOR_A,
                    **contracted,
                    **(primary or {}),
                },
                {
                    ZONE_NAME: "Room B",
                    ZONE_CLIMATE: head_b,
                    ZONE_SENSOR: SENSOR_B,
                    **contracted,
                    **(secondary or {}),
                },
            ],
            CONF_FAN_BOOST_ENABLE: False,
            CONF_MODE_HYSTERESIS: 0,
            **extra,
        },
    )
    # Step the clock BEFORE the coordinator exists, so every reading written
    # above is unambiguously older than this incarnation: under a frozen clock
    # they would otherwise share the startup instant.
    freezer.tick(timedelta(seconds=1))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    return entry, head_a, head_b


def _health(coord: MXZCoordinator, slug: str = "primary") -> str:
    return coord.data[f"{slug}_sensor_health"]


async def _advance(hass: HomeAssistant, freezer: Any, delta: timedelta) -> None:
    """Move the real clock and let every timer that came due actually fire."""
    freezer.tick(delta)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def _settle(hass: HomeAssistant, freezer: Any) -> None:
    """Let a refresh the coordinator asked for itself actually run.

    ``async_request_refresh`` is debounced, so a wakeup or a report listener
    only QUEUES the recompute. Expiring that cooldown is what turns the
    product's own reaction into an observable one — no test here recomputes on
    the coordinator's behalf when the point is that it woke up by itself.
    """
    await _advance(hass, freezer, timedelta(seconds=REQUEST_REFRESH_DEFAULT_COOLDOWN + 1))


async def _report(
    hass: HomeAssistant, entity_id: str, value: float, *, unit: str | None = None
) -> None:
    """One sensor report: a state write, changed or unchanged.

    Unchanged writes are the interesting half — they advance ``last_reported``
    without a state change, which is exactly what a periodic sensor sitting on a
    steady temperature does all day.
    """
    hass.states.async_set(
        entity_id,
        str(value),
        {ATTR_UNIT_OF_MEASUREMENT: unit or hass.config.units.temperature_unit},
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize("celsius", [False, True], ids=["fahrenheit", "celsius"])
async def test_stale_at_the_third_due_instant(
    hass: HomeAssistant, freezer: Any, celsius: bool
) -> None:
    """The core deadline: healthy one second short of 3P, stale AT 3P.

    Both unit systems, because the window is a duration and must not acquire a
    temperature unit's rounding by accident.
    """
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, celsius=celsius, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_A, HOT[celsius])  # the witnessed report at T0
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY
    assert coord.data["primary_demand"] == MODE_COOL
    assert hass.states.get(head_a).state == MODE_COOL

    await _advance(hass, freezer, WINDOW - timedelta(seconds=1))
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY, "one second short of the third due time"
    assert hass.states.get(head_a).state == MODE_COOL

    await _advance(hass, freezer, timedelta(seconds=1))
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_STALE
    assert coord.data["primary_demand"] == DEMAND_NEUTRAL
    assert coord.data["primary_engage"] == ENGAGE_SATISFIED
    assert hass.states.get(head_a).state == MODE_FAN_ONLY, "parked at the idle action"


async def test_the_deadline_timer_notices_a_silent_sensor(
    hass: HomeAssistant, freezer: Any
) -> None:
    """No event ever arrives from a dead sensor: the wakeup is what finds it."""
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY

    # Nothing but the clock moves — this test never recomputes for it.
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE
    assert hass.states.get(head_a).state == MODE_FAN_ONLY


async def test_scheduling_jitter_moves_the_deadline_with_the_report(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A late report is still a report: the window runs from IT, not from due time.

    Two of the three reports in the window arrive a minute late. Nothing is
    stale at the original deadline — jitter costs nothing until the sensor
    actually stops.
    """
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)

    await _advance(hass, freezer, timedelta(minutes=6))  # one minute late
    await _report(hass, SENSOR_A, HOT[False])
    await _advance(hass, freezer, timedelta(minutes=6))  # and again
    await _report(hass, SENSOR_A, HOT[False])
    await _advance(hass, freezer, timedelta(minutes=4))  # T0 + 16 min
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY, "past T0+3P, but 4 min past a real report"
    assert hass.states.get(head_a).state == MODE_COOL

    await _advance(hass, freezer, WINDOW)  # now it really stops
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE


async def test_startup_grace_is_provisional_then_expires(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A value present at startup buys one bounded grace, never a health claim.

    The reading is real and is used, so a restart does not park the house — but
    it is labelled `awaiting_report`, not `healthy`, because this incarnation
    did not witness the write that produced it.
    """
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_AWAITING
    assert coord.data["primary_demand"] == MODE_COOL, "provisionally eligible"
    assert hass.states.get(head_a).state == MODE_COOL, "and driven, not parked"

    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE, "the grace is bounded, and it is over"
    assert hass.states.get(head_a).state == MODE_FAN_ONLY


async def test_a_witnessed_report_ends_the_grace_early(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The first real report replaces provisional eligibility with evidence."""
    entry, _head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    assert _health(coord) == HEALTH_AWAITING

    await _advance(hass, freezer, timedelta(minutes=1))
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY
    # ...and the window now runs from that report, not from startup.
    await _advance(hass, freezer, WINDOW - timedelta(seconds=1))
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY


async def test_an_explicit_grace_is_shorter_than_the_window(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A per-sensor grace override is honoured on its own clock."""
    entry, head_a, _head_b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN, ZONE_STARTUP_GRACE: 2},
    )
    coord: MXZCoordinator = entry.runtime_data
    assert _health(coord) == HEALTH_AWAITING
    await _advance(hass, freezer, timedelta(minutes=2, seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE
    assert hass.states.get(head_a).state == MODE_FAN_ONLY


async def test_recovery_needs_one_fresh_valid_report(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A stale room comes back on evidence, and resumes its ordinary call."""
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE

    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY
    assert coord.data["primary_demand"] == MODE_COOL
    assert hass.states.get(head_a).state == MODE_COOL


async def test_an_already_expired_report_does_not_recover(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Evidence older than the window is not fresh, however it arrives.

    A backlog flush hands over a genuine report whose own deadline has already
    passed. Recovering on it would open and close an episode per flush.
    """
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_A, HOT[False])
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE

    # The state is re-published with its ORIGINAL report time, the way a
    # restored or replayed value arrives: real evidence, far too old.
    stale_state = hass.states.get(SENSOR_A)
    hass.states.async_set(
        SENSOR_A,
        stale_state.state,
        dict(stale_state.attributes),
        force_update=True,
    )
    hass.states.get(SENSOR_A).last_reported = stale_state.last_reported
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_STALE
    assert hass.states.get(head_a).state == MODE_FAN_ONLY


async def test_an_unchanged_report_keeps_a_periodic_sensor_fresh(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A steady temperature is not a silent sensor.

    The value never changes across four reporting intervals, so no state CHANGE
    ever fires. The room stays healthy because the writes themselves are the
    evidence.
    """
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    for _ in range(4):
        await _report(hass, SENSOR_A, HOT[False])  # same value every time
        await _advance(hass, freezer, timedelta(minutes=INTERVAL_MIN))
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY
    assert coord.data["primary_demand"] == MODE_COOL
    assert hass.states.get(head_a).state == MODE_COOL


async def test_an_unchanged_report_recovers_a_stale_room(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Recovery arrives on an unchanged write — the only kind a steady room sends.

    Nothing in this test asks for a recompute after the sensor comes back: the
    report listener is what notices, and it is subscribed only because this
    entry configured a cadence.
    """
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_A, HOT[False])
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE
    assert hass.states.get(head_a).state == MODE_FAN_ONLY

    await _report(hass, SENSOR_A, HOT[False])  # same value, new report
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_HEALTHY
    assert hass.states.get(head_a).state == MODE_COOL


async def test_an_unknown_cadence_is_never_cut_off(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A change-only sensor may be silent for hours and still steer its room.

    This is also every entry that predates the feature: configure nothing,
    behave exactly as before.
    """
    entry, head_a, _head_b = await _setup_fresh(hass, freezer)  # no cadence anywhere
    coord: MXZCoordinator = entry.runtime_data
    await _advance(hass, freezer, timedelta(hours=2))
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_CADENCE_UNKNOWN
    assert coord.data["primary_demand"] == MODE_COOL
    assert hass.states.get(head_a).state == MODE_COOL
    # The age is visible even though nothing enforces it.
    assert coord.data["primary_sensor_age"] >= 7200


async def test_a_healthy_neighbour_is_unaffected(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Per-room isolation: one dead sensor parks one head."""
    entry, head_a, head_b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN},
        secondary={ZONE_REPORT_INTERVAL: INTERVAL_MIN},
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_B, HOT[False])  # room B is hot AND reporting
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _report(hass, SENSOR_B, HOT[False])
    await _recompute(hass, entry)

    assert _health(coord, "primary") == HEALTH_STALE
    assert _health(coord, "secondary") == HEALTH_HEALTHY
    assert coord.data["secondary_demand"] == MODE_COOL
    assert hass.states.get(head_a).state == MODE_FAN_ONLY
    assert hass.states.get(head_b).state == MODE_COOL


async def test_a_stale_room_keeps_showing_its_last_reading(
    hass: HomeAssistant, freezer: Any
) -> None:
    """No substituted temperature, and no silent switch to another sensor.

    The room loses its vote, not its measurement: the last real reading is
    still published, with its health and its age beside it.
    """
    entry, _head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE
    assert coord.data["primary_temp"] == HOT[False]
    assert coord.data["primary_sensor_age"] >= int(WINDOW.total_seconds())
    zone_view = coord.data["zones"][0]
    assert zone_view["sensor_health"] == HEALTH_STALE
    assert zone_view["temp"] == HOT[False]
    # The neighbour's reading was never borrowed to fill the gap.
    assert coord.data["secondary_temp"] == NEUTRAL[False]


async def test_an_invalid_reading_is_invalid_not_stale(
    hass: HomeAssistant, freezer: Any
) -> None:
    """M13 still runs first, and it runs immediately — no window to wait out."""
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _advance(hass, freezer, timedelta(minutes=1))
    hass.states.async_set(SENSOR_A, "unavailable")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_INVALID, "not stale: the value is the problem"
    assert coord.data["primary_temp"] is None
    assert coord.data["primary_demand"] == DEMAND_NEUTRAL
    assert hass.states.get(head_a).state == MODE_FAN_ONLY


async def test_a_manual_fan_hold_survives_staleness(hass: HomeAssistant, freezer: Any) -> None:
    """Recognized human intent is authoritative: staleness warns, it never overrides.

    The room's automatic demand is suspended and its head is parked by the
    ordinary idle path — but the fan speed the person chose is not touched, and
    the hold does not release.
    """
    entry, head_a, _head_b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN},
        **{CONF_FAN_BOOST_ENABLE: True},
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "quiet")
    await _recompute(hass, entry)
    assert coord.fan_auto_is_on(head_a) is False

    calls = _record_calls(hass)
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE
    assert coord.fan_auto_is_on(head_a) is False, "the hold survives the episode"
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"
    assert not [
        c for c in _head_calls(calls, head_a) if c[0] == "set_fan_mode"
    ], "no fan write on a held surface"


async def test_parking_a_stale_room_keeps_the_eco_override(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Freshness parks through the EXISTING idle mapping, guards and all.

    Under eco a satisfied head goes off regardless of the fan_only idle policy;
    a stale room must inherit that, not command fan_only around it.
    """
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_eco_idle")}, blocking=True
    )
    await hass.async_block_till_done()
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE
    assert hass.states.get(head_a).state == MODE_OFF, "eco-satisfied beats idle policy"


async def test_a_standby_hold_still_owns_the_head_while_stale(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An inhibit park is a no-plan short circuit; freshness does not reach past it."""
    entry, head_a, _head_b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN},
        **{
            CONF_INHIBIT_ENTITY: "binary_sensor.grid",
            CONF_INHIBIT_ACTIVE_STATE: "on",
            CONF_INHIBIT_ACTION: INHIBIT_ACTION_FAN_ONLY,
        },
    )
    coord: MXZCoordinator = entry.runtime_data
    hass.states.async_set("binary_sensor.grid", "on")
    await hass.async_block_till_done()
    assert coord.inhibited is True

    calls = _record_calls(hass)
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE
    assert hass.states.get(head_a).state == MODE_FAN_ONLY
    assert not [
        c for c in _head_calls(calls, head_a) if c[0] == "set_fan_mode"
    ], "the hold keeps the fan machinery frozen"


async def test_idle_off_parking_hands_the_fan_back_first(
    hass: HomeAssistant, freezer: Any
) -> None:
    """With idle_action off, a stale room takes the ordinary handback-then-off path."""
    entry, head_a, _head_b = await _setup_fresh(
        hass,
        freezer,
        primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN},
        **{CONF_IDLE_ACTION: IDLE_ACTION_OFF, CONF_FAN_BOOST_ENABLE: True},
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == MODE_COOL

    calls = _record_calls(hass)
    await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
    await _settle(hass, freezer)
    assert _health(coord) == HEALTH_STALE
    assert hass.states.get(head_a).state == MODE_OFF
    assert ("set_fan_mode", "auto") in _head_calls(calls, head_a), "fan handed back"


async def test_one_log_line_per_transition(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """One warning opening the episode, one info closing it — not one per tick."""
    entry, _head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)

    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        await _advance(hass, freezer, WINDOW + timedelta(seconds=2))
        await _settle(hass, freezer)
        for _ in range(4):  # the heartbeat, an event, anything: still one line
            await _recompute(hass, entry)
        assert _health(coord) == HEALTH_STALE
        assert sum("past its" in r.message for r in caplog.records) == 1

        # An unhealthy SUBTYPE change inside the same episode is not a new failure.
        hass.states.async_set(SENSOR_A, "unavailable")
        await hass.async_block_till_done()
        await _recompute(hass, entry)
        assert _health(coord) == HEALTH_INVALID
        assert sum("past its" in r.message for r in caplog.records) == 1

        caplog.clear()
        await _report(hass, SENSOR_A, HOT[False])
        await _recompute(hass, entry)
        for _ in range(3):
            await _recompute(hass, entry)
        assert _health(coord) == HEALTH_HEALTHY
        assert sum("reporting again" in r.message for r in caplog.records) == 1


async def test_a_reload_re_derives_health_and_says_so(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Provenance does not survive a reload: a witnessed report becomes a value.

    What is retained is nothing — a reload builds a new coordinator. What is
    re-derived from HA state is the reading's validity and its write age. So
    the room reopens as `awaiting_report` with one fresh bounded grace, and the
    coordinator says that out loud once per incarnation.
    """
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_REPORT_INTERVAL: INTERVAL_MIN}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY

    with caplog.at_level(logging.INFO, logger="custom_components.mxz_coordinator"):
        caplog.clear()
        await _advance(hass, freezer, timedelta(minutes=1))
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert sum("not retained across a reload" in r.message for r in caplog.records) == 1

    reloaded: MXZCoordinator = entry.runtime_data
    assert reloaded is not coord
    assert _health(reloaded) == HEALTH_AWAITING, "the earlier report is not this one's"
    assert reloaded.data["primary_demand"] == MODE_COOL, "provisionally eligible"
    # The new grace is a full window from the reload, not the old deadline.
    await _advance(hass, freezer, WINDOW - timedelta(seconds=2))
    await _recompute(hass, entry)
    assert _health(reloaded) == HEALTH_AWAITING
    await _advance(hass, freezer, timedelta(seconds=4))
    await _settle(hass, freezer)
    assert _health(reloaded) == HEALTH_STALE
    assert hass.states.get(head_a).state == MODE_FAN_ONLY


async def test_an_explicit_maximum_age_needs_no_interval(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The other half of the configuration contract, end to end."""
    entry, head_a, _head_b = await _setup_fresh(
        hass, freezer, primary={ZONE_MAX_AGE: 4}
    )
    coord: MXZCoordinator = entry.runtime_data
    await _report(hass, SENSOR_A, HOT[False])
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_HEALTHY
    await _advance(hass, freezer, timedelta(minutes=4, seconds=2))
    assert _health(coord) == HEALTH_STALE
    assert hass.states.get(head_a).state == MODE_FAN_ONLY


async def test_unusable_configuration_enforces_nothing(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A junk duration is reported and read as unset — never rounded into a cutoff."""
    with caplog.at_level(logging.WARNING, logger="custom_components.mxz_coordinator"):
        entry, head_a, _head_b = await _setup_fresh(
            hass, freezer, primary={ZONE_REPORT_INTERVAL: 0}
        )
    coord: MXZCoordinator = entry.runtime_data
    assert _health(coord) == HEALTH_CADENCE_UNKNOWN
    assert sum("no usable reporting interval" in r.message for r in caplog.records) == 1
    await _advance(hass, freezer, timedelta(hours=3))
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_CADENCE_UNKNOWN
    assert hass.states.get(head_a).state == MODE_COOL


async def test_an_invalid_profile_is_rejected_whole_and_reported_once(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """M23 scenario 19, the parser's half: a maximum age below the interval.

    The room has a trusted basis, a usable interval and a usable maximum
    age — every ingredient of a cutoff — and the two disagree. Nothing is
    raised to fit: the profile is rejected as a whole with one diagnostic, the
    room keeps an unknown cadence, and three hours of silence park nothing.
    Submitting a replacement profile and keeping the last valid one is a
    configuration-surface contract this base has no surface for.
    """
    with caplog.at_level(logging.WARNING, logger="custom_components.mxz_coordinator"):
        entry, head_a, _head_b = await _setup_fresh(
            hass, freezer, primary={ZONE_REPORT_INTERVAL: 5, ZONE_MAX_AGE: 2}
        )
    coord: MXZCoordinator = entry.runtime_data
    assert coord._freshness == {}, "no cutoff was built out of the valid half"
    assert _health(coord) == HEALTH_CADENCE_UNKNOWN
    rejected = [r.message for r in caplog.records if "no usable reporting interval" in r.message]
    assert len(rejected) == 1
    assert "(interval, maximum age, grace = (5, 2, None))" in rejected[0]
    await _advance(hass, freezer, timedelta(hours=3))
    await _recompute(hass, entry)
    assert _health(coord) == HEALTH_CADENCE_UNKNOWN
    assert coord.data["primary_demand"] == MODE_COOL
    assert hass.states.get(head_a).state == MODE_COOL
