"""3.4.1: a °C-native head's snapped setpoint echo must not cause a write loop.

Drew's house (HA in °F) runs ESPHome CN105 Mitsubishi heads: °C-native, a
0.5 °C setpoint grid, range-capable, shown in HA at 0.5 °F display precision.
MXZ commands a whole-°F edge (64 °F = 17.78 °C); the head latches the nearest
0.5 °C (18.0 °C) and reports it back as 64.4 °F, displayed 64.5 °F. The old
"already set?" check compared 64.5 with 64 against half a °F step and never
matched, so MXZ re-sent set_temperature on every coordinator cycle (~10 s) for
as long as the room ran: 120–196 writes/h observed at target 62, 145/h at 64.

The mock head below models that firmware end to end through HA's real climate
service: HA converts the °F command into °C, the head publishes the commanded
value, then its 0.5 °C-snapped echo, and HA displays it in °F at 0.5 °F.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import PRECISION_HALVES, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)

from .test_drive import MockHead, _eid, _set_temp

SENSORS = ("sensor.room_a_temp", "sensor.room_b_temp")
CYCLES = 12  # ~2 minutes of coordinator cycles at the observed ~10 s cadence


def _cn105_snap(celsius: float) -> float:
    """The setpoint a CN105 head latches: the nearest 0.5 °C (half up)."""
    return math.floor(celsius * 2 + 0.5) / 2


class CN105Head(MockHead):
    """An ESPHome CN105 head: °C-native, 0.5 °C grid, range-capable.

    Records every set_temperature it receives (already converted to °C by HA's
    climate service), publishes the commanded value first and then the value
    the head actually latched, exactly as ESPHome does ~0.1–1 s apart.
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_precision = PRECISION_HALVES
    _attr_min_temp = 10.0
    _attr_max_temp = 31.0

    def __init__(self, suffix: str) -> None:
        super().__init__(suffix)
        self.writes: list[dict[str, Any]] = []

    async def async_set_temperature(self, **kwargs: Any) -> None:
        self.writes.append(dict(kwargs))
        self._take(kwargs, lambda value: value)  # the commanded value ...
        self.async_write_ha_state()
        self._take(kwargs, _cn105_snap)  # ... then the head's snapped echo
        self.async_write_ha_state()

    def _take(self, kwargs: dict[str, Any], latch) -> None:
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = mode
        for key, attr in (
            ("target_temp_low", "_attr_target_temperature_low"),
            ("target_temp_high", "_attr_target_temperature_high"),
            ("temperature", "_attr_target_temperature"),
        ):
            if (value := kwargs.get(key)) is not None:
                setattr(self, attr, latch(value))


class CN105SingleHead(CN105Head):
    """The same head without TARGET_TEMPERATURE_RANGE (single `temperature`)."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, suffix: str) -> None:
        super().__init__(suffix)
        self._attr_target_temperature = 21.0


class RecordingHead(MockHead):
    """A head native to the system unit that stores exactly what it is told."""

    def __init__(self, suffix: str) -> None:
        super().__init__(suffix)
        self.writes: list[dict[str, Any]] = []

    async def async_set_temperature(self, **kwargs: Any) -> None:
        self.writes.append(dict(kwargs))
        await super().async_set_temperature(**kwargs)


class WholeFahrenheitHead(RecordingHead):
    """A °F-native head with an explicit whole-degree step."""

    _attr_target_temperature_step = 1.0


class HalfCelsiusHead(RecordingHead):
    """A °C-native 0.5 °C head in a °C system (values land on its grid)."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_precision = PRECISION_TENTHS


async def _start(hass: HomeAssistant, heads: list, units=US_CUSTOMARY_SYSTEM) -> MockConfigEntry:
    """Two zones on ``heads``, coordinator and both rooms enabled."""
    hass.config.units = units

    async def _climate(hass, config, async_add_entities, discovery_info=None):
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=_climate))
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: heads[0].entity_id,
            CONF_SECONDARY_CLIMATE: heads[1].entity_id,
            CONF_PRIMARY_SENSOR: SENSORS[0],
            CONF_SECONDARY_SENSOR: SENSORS[1],
        },
    )
    entry.add_to_hass(hass)
    room = 21 if units is METRIC_SYSTEM else 70
    for sensor in SENSORS:
        await _set_temp(hass, sensor, room)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    return entry


async def _cycle(hass: HomeAssistant, entry: MockConfigEntry, room: float) -> None:
    """One coordinator cycle on a fresh room reading (a sensor tick)."""
    for sensor in SENSORS:
        await _set_temp(hass, sensor, room)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def _engage(
    hass: HomeAssistant, entry: MockConfigEntry, mode: str, target: float, off: float = 1.0
) -> float:
    """Shared ``mode``, both rooms at ``target``, engaged and still running.

    A room must drift PAST the engage deadband (one ``off``) to engage, then the
    run-to-target latch keeps it running while it is ``off`` from target — the
    observed state (room 1 °F above a cool target). Returns that running room
    reading so later cycles keep the room there.
    """
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": _eid(hass, entry, "_shared_mode"), "option": mode},
        blocking=True,
    )
    for suffix in ("_primary_target", "_secondary_target"):
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": _eid(hass, entry, suffix), "value": target},
            blocking=True,
        )
    sign = 1 if mode == "cool" else -1
    await _cycle(hass, entry, target + sign * 2 * off)  # engage
    room = target + sign * off
    await _cycle(hass, entry, room)  # still engaged, closing on target
    return room


async def _quiet_cycles(hass: HomeAssistant, entry: MockConfigEntry, room: float) -> None:
    """~2 minutes of cycles with a sensor that jitters by a tenth, as real ones do."""
    for i in range(CYCLES):
        await _cycle(hass, entry, room + (0.1 if i % 2 else 0.0))


def _edges(mode: str, target: float) -> tuple[float, float]:
    """The °F band MXZ commands: cool (t-2, t), heat (t, t+2), floor 59 °F."""
    if mode == "cool":
        return (max(target - 2, 59), target)
    return (target, target + 2)


def _echo(fahrenheit: float) -> float:
    """What HA shows for a CN105 commanded ``fahrenheit``: snapped °C, back in °F at 0.5."""
    latched = _cn105_snap((fahrenheit - 32) * 5 / 9)
    return round((latched * 9 / 5 + 32) * 2) / 2


# ---------------------------------------------------------------------------
# Headline: the exact observed scenario.
# ---------------------------------------------------------------------------
async def test_target_64_cool_writes_once_not_every_cycle(hass: HomeAssistant) -> None:
    """°F system, CN105 head, target 64, room 65, shared cool: ONE write, not ~12.

    The head echoes target_temp_high 64.5 (18.0 °C) for the 64 it was sent;
    that is the only value a 0.5 °C head can hold for 64 °F, so re-sending 64
    can never change anything on the head.
    """
    heads = [CN105Head("a"), CN105Head("b")]
    entry = await _start(hass, heads)
    room = await _engage(hass, entry, "cool", 64)

    a = hass.states.get(heads[0].entity_id)
    assert a.state == "cool"
    assert a.attributes["target_temp_high"] == 64.5  # the real echo
    assert a.attributes["target_temp_low"] == 61.5  # 62 -> 16.5 °C -> 61.7
    engaged = [len(head.writes) for head in heads]
    assert all(engaged)

    await _quiet_cycles(hass, entry, room)
    resent = [len(head.writes) - n for head, n in zip(heads, engaged)]
    assert resent == [0, 0], f"{resent} set_temperature re-sends in {CYCLES} cycles"
    assert [len(head.writes) for head in heads] == [1, 1]


# ---------------------------------------------------------------------------
# Sweep: every whole-°F target whose band edge lands off a 0.5 °F display mark.
# ---------------------------------------------------------------------------
# Affected = at least one commanded edge echoes 0.5 °F away. The brief's list
# (62-65, 71, 72) loops in both modes; the 2 °F band also makes cool 66/67 loop
# through their LOW edge (64/65) and heat 60/61/69/70 through their HIGH edge.
AFFECTED_COOL = [62, 63, 64, 65, 66, 67, 71, 72]
AFFECTED_HEAT = [60, 61, 62, 63, 64, 65, 69, 70, 71, 72]
CONTROL_COOL = [60, 68, 69, 70]  # both edges echo exactly
CONTROL_HEAT = [59, 66, 67, 68]
RANGE_CASES = (
    [("cool", t, "affected") for t in AFFECTED_COOL]
    + [("heat", t, "affected") for t in AFFECTED_HEAT]
    + [("cool", t, "control") for t in CONTROL_COOL]
    + [("heat", t, "control") for t in CONTROL_HEAT]
)


def test_sweep_classification_matches_the_echo_arithmetic() -> None:
    """The affected/control split above is what the head arithmetic says."""
    for mode, target, kind in RANGE_CASES:
        missed = [e for e in _edges(mode, target) if _echo(e) != e]
        assert bool(missed) == (kind == "affected"), (mode, target, missed)


@pytest.mark.parametrize(
    ("mode", "target", "kind"), RANGE_CASES, ids=[f"{m}-{t}-{k}" for m, t, k in RANGE_CASES]
)
async def test_range_head_no_rewrite_after_first(
    hass: HomeAssistant, mode: str, target: int, kind: str
) -> None:
    """After the first write, further cycles issue ZERO set_temperature calls."""
    heads = [CN105Head("a"), CN105Head("b")]
    entry = await _start(hass, heads)
    room = await _engage(hass, entry, mode, target)

    a = hass.states.get(heads[0].entity_id)
    low, high = _edges(mode, target)
    assert a.state == mode
    assert (a.attributes["target_temp_low"], a.attributes["target_temp_high"]) == (
        _echo(low), _echo(high),
    )
    first = list(heads[0].writes)
    assert first, "engaging must write the head"

    await _quiet_cycles(hass, entry, room)
    assert heads[0].writes == first
    assert len(heads[1].writes) == len(first)


SINGLE_CASES = (
    [(m, t, "affected") for m in ("cool", "heat") for t in (62, 63, 64, 65, 71, 72)]
    + [(m, t, "control") for m in ("cool", "heat") for t in (60, 66, 68, 70)]
)


@pytest.mark.parametrize(
    ("mode", "target", "kind"), SINGLE_CASES, ids=[f"{m}-{t}-{k}" for m, t, k in SINGLE_CASES]
)
async def test_single_setpoint_head_no_rewrite_after_first(
    hass: HomeAssistant, mode: str, target: int, kind: str
) -> None:
    """The `temperature` path (no RANGE feature) has the same check and the same fix."""
    assert (_echo(target) != target) == (kind == "affected")
    heads = [CN105SingleHead("a"), CN105SingleHead("b")]
    entry = await _start(hass, heads)
    room = await _engage(hass, entry, mode, target)

    a = hass.states.get(heads[0].entity_id)
    assert a.state == mode
    assert a.attributes["temperature"] == _echo(target)
    first = list(heads[0].writes)
    assert first, "engaging must write the head"

    await _quiet_cycles(hass, entry, room)
    assert heads[0].writes == first
    assert len(heads[1].writes) == len(first)


# ---------------------------------------------------------------------------
# Guards: a genuinely wrong head is still corrected.
# ---------------------------------------------------------------------------
async def _wall_set(hass: HomeAssistant, entity_id: str, **data: Any) -> None:
    """Someone else (wall remote, app) commands the head directly."""
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": entity_id, **data}, blocking=True
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("mode", "target", "edge", "moved_to"),
    [
        ("cool", 62, "target_temp_high", 63),  # 1 °F: one 0.5 °C step (17.0 °C)
        ("cool", 64, "target_temp_high", 66),
        ("cool", 64, "target_temp_high", 65),
        ("cool", 64, "target_temp_low", 61),  # the low edge alone is wrong
        ("heat", 64, "target_temp_low", 65),
        ("heat", 62, "target_temp_high", 63),  # 64 -> 63
    ],
)
async def test_range_head_changed_by_someone_is_resent(
    hass: HomeAssistant, mode: str, target: int, edge: str, moved_to: float
) -> None:
    """°F system, CN105: a setpoint moved by >= 1 °F is re-sent on the next cycle."""
    heads = [CN105Head("a"), CN105Head("b")]
    entry = await _start(hass, heads)
    room = await _engage(hass, entry, mode, target)
    low, high = _edges(mode, target)
    eid = heads[0].entity_id

    moved = {"target_temp_low": low, "target_temp_high": high, edge: moved_to}
    await _wall_set(hass, eid, **moved)
    assert hass.states.get(eid).attributes[edge] == _echo(moved_to)
    heads[0].writes.clear()

    await _cycle(hass, entry, room)
    assert len(heads[0].writes) == 1
    a = hass.states.get(eid)
    assert (a.attributes["target_temp_low"], a.attributes["target_temp_high"]) == (
        _echo(low), _echo(high),
    )
    await _quiet_cycles(hass, entry, room)
    assert len(heads[0].writes) == 1


@pytest.mark.parametrize(("target", "moved_to"), [(62, 63), (64, 66), (64, 65), (65, 64)])
async def test_single_head_changed_by_someone_is_resent(
    hass: HomeAssistant, target: int, moved_to: int
) -> None:
    """Same guard on the single-setpoint path."""
    heads = [CN105SingleHead("a"), CN105SingleHead("b")]
    entry = await _start(hass, heads)
    room = await _engage(hass, entry, "cool", target)
    eid = heads[0].entity_id

    await _wall_set(hass, eid, temperature=moved_to)
    heads[0].writes.clear()
    await _cycle(hass, entry, room)
    assert len(heads[0].writes) == 1
    assert hass.states.get(eid).attributes["temperature"] == _echo(target)


@pytest.mark.parametrize("cls", [CN105Head, CN105SingleHead])
async def test_head_in_wrong_mode_is_resent(hass: HomeAssistant, cls) -> None:
    """Right setpoint, wrong mode -> MXZ re-sends."""
    heads = [cls("a"), cls("b")]
    entry = await _start(hass, heads)
    room = await _engage(hass, entry, "cool", 64)
    eid = heads[0].entity_id

    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": eid, "hvac_mode": "heat"}, blocking=True
    )
    await hass.async_block_till_done()
    heads[0].writes.clear()
    await _cycle(hass, entry, room)
    assert len(heads[0].writes) == 1
    assert hass.states.get(eid).state == "cool"


async def test_celsius_system_half_degree_drift_is_resent(hass: HomeAssistant) -> None:
    """°C system, 0.5 °C head: exact match skips; a full 0.5 °C step off re-sends."""
    heads = [HalfCelsiusHead("a"), HalfCelsiusHead("b")]
    entry = await _start(hass, heads, METRIC_SYSTEM)
    room = await _engage(hass, entry, "cool", 21.0, off=0.5)
    eid = heads[0].entity_id
    a = hass.states.get(eid)
    assert (a.attributes["target_temp_low"], a.attributes["target_temp_high"]) == (20.0, 21.0)
    first = list(heads[0].writes)
    assert first

    await _quiet_cycles(hass, entry, room)
    assert heads[0].writes == first  # exact: skipped

    for moved in ({"target_temp_low": 20.0, "target_temp_high": 21.5},
                  {"target_temp_low": 19.5, "target_temp_high": 21.0}):
        await _wall_set(hass, eid, **moved)
        heads[0].writes.clear()
        await _cycle(hass, entry, room)
        assert len(heads[0].writes) == 1, moved
        a = hass.states.get(eid)
        assert (a.attributes["target_temp_low"], a.attributes["target_temp_high"]) == (20.0, 21.0)


@pytest.mark.parametrize("cls", [RecordingHead, WholeFahrenheitHead])
async def test_fahrenheit_native_head_unchanged(hass: HomeAssistant, cls) -> None:
    """°F system, °F whole-degree head (step unknown or 1): exact skips, 1 °F off re-sends."""
    heads = [cls("a"), cls("b")]
    entry = await _start(hass, heads)
    room = await _engage(hass, entry, "cool", 64)
    eid = heads[0].entity_id
    first = list(heads[0].writes)
    assert first

    await _quiet_cycles(hass, entry, room)
    assert heads[0].writes == first

    await _wall_set(hass, eid, target_temp_low=62, target_temp_high=65)
    heads[0].writes.clear()
    await _cycle(hass, entry, room)
    assert len(heads[0].writes) == 1
    assert hass.states.get(eid).attributes["target_temp_high"] == 64


# ---------------------------------------------------------------------------
# Guards: MXZ's own deliberate setpoint changes still go out promptly.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cls", [CN105Head, CN105SingleHead])
@pytest.mark.parametrize(("start", "new"), [(64, 65), (64, 63), (62, 61), (71, 72)])
async def test_target_change_is_written_promptly(
    hass: HomeAssistant, cls, start: int, new: int
) -> None:
    """Each whole-°F target is a distinct 0.5 °C step, so a 1 °F change always writes."""
    heads = [cls("a"), cls("b")]
    entry = await _start(hass, heads)
    await _engage(hass, entry, "cool", start)
    eid = heads[0].entity_id
    room = max(start, new) + 2  # a target change resets the latch: past the deadband
    await _cycle(hass, entry, room)
    heads[0].writes.clear()

    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, entry, "_primary_target"), "value": new},
        blocking=True,
    )
    await _cycle(hass, entry, room)
    assert len(heads[0].writes) == 1
    attr = "target_temp_high" if cls is CN105Head else "temperature"
    assert hass.states.get(eid).attributes[attr] == _echo(new)

    await _quiet_cycles(hass, entry, room)
    assert len(heads[0].writes) == 1


async def test_eco_edges_are_written_and_released(hass: HomeAssistant) -> None:
    """Eco edges replace the room band and the room band comes back, each once."""
    heads = [CN105Head("a"), CN105Head("b")]
    entry = await _start(hass, heads)
    await _engage(hass, entry, "cool", 64)
    eid = heads[0].entity_id
    eco = _eid(hass, entry, "_eco_idle")

    await hass.services.async_call("switch", "turn_on", {"entity_id": eco}, blocking=True)
    heads[0].writes.clear()
    await _cycle(hass, entry, 80)  # above the 78 °F eco ceiling -> eco cooling
    a = hass.states.get(eid)
    assert a.state == "cool"
    assert (a.attributes["target_temp_low"], a.attributes["target_temp_high"]) == (76, 78)
    assert len(heads[0].writes) == 1
    await _quiet_cycles(hass, entry, 80)
    assert len(heads[0].writes) == 1

    await hass.services.async_call("switch", "turn_off", {"entity_id": eco}, blocking=True)
    heads[0].writes.clear()
    await _cycle(hass, entry, 65)
    a = hass.states.get(eid)
    assert (a.attributes["target_temp_low"], a.attributes["target_temp_high"]) == (61.5, 64.5)
    assert len(heads[0].writes) == 1
    await _quiet_cycles(hass, entry, 65)
    assert len(heads[0].writes) == 1
