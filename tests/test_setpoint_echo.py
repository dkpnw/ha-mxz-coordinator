"""3.4.1: a °C-native head's snapped setpoint echo must not cause a write loop.

Drew's house (HA in °F) runs ESPHome CN105 Mitsubishi heads: °C-native, a
0.5 °C setpoint grid, range-capable, shown in HA at 0.5 °F display precision.
MXZ commands a whole-°F edge (64 °F = 17.78 °C); the head latches the nearest
0.5 °C (18.0 °C) and reports it back as 64.4 °F, displayed 64.5 °F. The old
"already set?" check compared 64.5 with 64 against half a °F step and never
matched, so MXZ re-sent set_temperature on every coordinator cycle (~10 s) for
as long as the room ran: 120–196 writes/h observed at target 62, 145/h at 64.

The mock heads below model that firmware end to end through HA's real climate
service: HA converts the °F command into °C, the head publishes the commanded
value, then its 0.5 °C-snapped echo, and HA displays it in °F at 0.5 °F.
``HouseCN105Head`` is the house as observed: only the active edge is snapped,
the other edge keeps the raw commanded value. ``CN105Head`` snaps both edges,
a stricter head the fix must also settle.
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


def _cn105_snap(celsius: float, step: float = 0.5) -> float:
    """The setpoint a CN105 head latches: the nearest ``step`` °C (half up)."""
    return math.floor(celsius / step + 0.5) * step


class CN105Head(MockHead):
    """An ESPHome CN105 head: °C-native, 0.5 °C grid, range-capable.

    Records every set_temperature it receives (already converted to °C by HA's
    climate service), publishes the commanded value first and then the value
    the head actually latched, exactly as ESPHome does ~0.1–1 s apart. This
    model latches BOTH range edges; see ``HouseCN105Head`` for the real one.
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
        self._take(kwargs, self._latch)  # ... then the head's snapped echo
        self.async_write_ha_state()

    def _latch(self, celsius: float) -> float:
        return _cn105_snap(celsius, self._attr_target_temperature_step)

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


class HouseCN105Head(CN105Head):
    """The CN105 head as the house shows it: only the ACTIVE edge is snapped.

    Oct 7 snapshot, Rec room cool 64: target_temp_high 64.5 (snapped) and
    target_temp_low 62.0 (raw, as commanded). The head latches the edge it runs
    on (high in cool, low in heat) and keeps the other exactly as sent, which is
    up to 0.22 °C off its 0.5 °C grid for a whole °F.
    """

    async def async_set_temperature(self, **kwargs: Any) -> None:
        self.writes.append(dict(kwargs))
        self._take(kwargs, lambda value: value)
        self.async_write_ha_state()
        mode = kwargs.get("hvac_mode") or self._attr_hvac_mode
        active = "target_temp_high" if mode == "cool" else "target_temp_low"
        self._take({active: kwargs.get(active)}, self._latch)
        self.async_write_ha_state()


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


class WholeCelsiusHead(CN105Head):
    """The same firmware on a 1 °C grid (still rounds half up)."""

    _attr_target_temperature_step = 1.0


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


def _shown(head: type, mode: str, low: float, high: float) -> tuple[float, float]:
    """The (low, high) HA shows after the °F band (low, high) is sent to ``head``."""
    if head is HouseCN105Head:  # the inactive edge stays raw
        return (low, _echo(high)) if mode == "cool" else (_echo(low), high)
    return (_echo(low), _echo(high))


def _band(hass: HomeAssistant, entity_id: str) -> tuple[float, float]:
    a = hass.states.get(entity_id).attributes
    return (a["target_temp_low"], a["target_temp_high"])


HEADS = {"house": HouseCN105Head, "both-edges": CN105Head}


# ---------------------------------------------------------------------------
# Headline: the exact observed scenario.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("head", "shown"),
    [(HouseCN105Head, (62.0, 64.5)), (CN105Head, (61.5, 64.5))],
    ids=["house", "both-edges"],
)
async def test_target_64_cool_writes_once_not_every_cycle(
    hass: HomeAssistant, head: type, shown: tuple[float, float]
) -> None:
    """°F system, CN105 head, target 64, room 65, shared cool: ONE write, not ~12.

    The head echoes target_temp_high 64.5 (18.0 °C) for the 64 it was sent;
    that is the only value a 0.5 °C head can hold for 64 °F, so re-sending 64
    can never change anything on the head. The house head shows (62.0, 64.5),
    exactly the Oct 7 snapshot; the both-edges head also snaps 62 to 61.5.
    """
    heads = [head("a"), head("b")]
    entry = await _start(hass, heads)
    room = await _engage(hass, entry, "cool", 64)

    eid = heads[0].entity_id
    assert hass.states.get(eid).state == "cool"
    assert _band(hass, eid) == shown
    engaged = [len(h.writes) for h in heads]
    assert all(engaged)

    await _quiet_cycles(hass, entry, room)
    resent = [len(h.writes) - n for h, n in zip(heads, engaged)]
    assert resent == [0, 0], f"{resent} set_temperature re-sends in {CYCLES} cycles"
    assert [len(h.writes) for h in heads] == [1, 1]
    assert _band(hass, eid) == shown


# ---------------------------------------------------------------------------
# Sweep: every whole-°F target in the house's range, on both head models.
# ---------------------------------------------------------------------------
# Before 3.4.1 a target looped when HA showed any commanded edge 0.5 °F off.
# The house head snaps only the active edge, i.e. the target itself, so 62-65,
# 71 and 72 loop in both modes and 59-61, 66-70 do not: exactly what the house
# showed. The both-edges head over-predicts: its snapped inactive edge also
# makes cool 66/67 loop (low edge 64/65) and heat 60/61/69/70 (high edge
# 62/63/71/72). The house's raw inactive edge is up to 0.22 °C off the 0.5 °C
# grid and must still read as already set.
TARGETS = range(59, 73)
LOOPED = {
    ("house", "cool"): [62, 63, 64, 65, 71, 72],
    ("house", "heat"): [62, 63, 64, 65, 71, 72],
    ("both-edges", "cool"): [62, 63, 64, 65, 66, 67, 71, 72],
    ("both-edges", "heat"): [60, 61, 62, 63, 64, 65, 69, 70, 71, 72],
}
RANGE_CASES = [
    (name, mode, t, "affected" if t in looped else "control")
    for (name, mode), looped in LOOPED.items()
    for t in TARGETS
]


def test_sweep_classification_matches_the_echo_arithmetic() -> None:
    """The looped lists above are what each head model's arithmetic says."""
    for name, mode, target, kind in RANGE_CASES:
        sent = _edges(mode, target)
        missed = _shown(HEADS[name], mode, *sent) != sent
        assert missed == (kind == "affected"), (name, mode, target)


@pytest.mark.parametrize(
    ("name", "mode", "target", "kind"), RANGE_CASES, ids=["-".join(map(str, c)) for c in RANGE_CASES]
)
async def test_range_head_no_rewrite_after_first(
    hass: HomeAssistant, name: str, mode: str, target: int, kind: str
) -> None:
    """After the first write, further cycles issue ZERO set_temperature calls."""
    head = HEADS[name]
    heads = [head("a"), head("b")]
    entry = await _start(hass, heads)
    room = await _engage(hass, entry, mode, target)

    eid = heads[0].entity_id
    shown = _shown(head, mode, *_edges(mode, target))
    assert hass.states.get(eid).state == mode
    assert _band(hass, eid) == shown
    first = list(heads[0].writes)
    assert first, "engaging must write the head"

    await _quiet_cycles(hass, entry, room)
    assert heads[0].writes == first
    assert len(heads[1].writes) == len(first)
    assert _band(hass, eid) == shown  # the house's raw inactive edge held throughout


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


@pytest.mark.parametrize("name", list(HEADS))
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
    hass: HomeAssistant, name: str, mode: str, target: int, edge: str, moved_to: float
) -> None:
    """°F system, CN105: a setpoint moved by >= 1 °F is re-sent on the next cycle.

    On the house head the inactive edge (cool low, heat high) is shown raw, so a
    raw edge 1 °F off must still be told apart from the raw edge MXZ sent.
    """
    head = HEADS[name]
    heads = [head("a"), head("b")]
    entry = await _start(hass, heads)
    room = await _engage(hass, entry, mode, target)
    low, high = _edges(mode, target)
    eid = heads[0].entity_id

    moved = {"target_temp_low": low, "target_temp_high": high, edge: moved_to}
    await _wall_set(hass, eid, **moved)
    assert _band(hass, eid) == _shown(head, mode, *moved.values())
    heads[0].writes.clear()

    await _cycle(hass, entry, room)
    assert len(heads[0].writes) == 1
    assert _band(hass, eid) == _shown(head, mode, low, high)
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


async def test_whole_celsius_head_latches_half_up(hass: HomeAssistant) -> None:
    """°C system, 1 °C head, target 20.5 °C: the head latches 21 °C (half up).

    Round-half-to-even would expect 20 °C, see the head one step off and
    re-send every cycle. A real one-step move is still re-sent.
    """
    heads = [WholeCelsiusHead("a"), WholeCelsiusHead("b")]
    entry = await _start(hass, heads, METRIC_SYSTEM)
    room = await _engage(hass, entry, "cool", 20.5, off=0.5)
    eid = heads[0].entity_id
    assert _band(hass, eid) == (20.0, 21.0)  # sent (19.5, 20.5)
    first = list(heads[0].writes)
    assert first

    await _quiet_cycles(hass, entry, room)
    assert heads[0].writes == first

    await _wall_set(hass, eid, target_temp_low=20.0, target_temp_high=22.0)
    heads[0].writes.clear()
    await _cycle(hass, entry, room)
    assert len(heads[0].writes) == 1
    assert _band(hass, eid) == (20.0, 21.0)


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

    heads[0].writes.clear()  # the toggle's own refresh may write: count it
    await hass.services.async_call("switch", "turn_on", {"entity_id": eco}, blocking=True)
    await _cycle(hass, entry, 80)  # above the 78 °F eco ceiling -> eco cooling
    a = hass.states.get(eid)
    assert a.state == "cool"
    assert (a.attributes["target_temp_low"], a.attributes["target_temp_high"]) == (76, 78)
    assert len(heads[0].writes) == 1
    await _quiet_cycles(hass, entry, 80)
    assert len(heads[0].writes) == 1

    heads[0].writes.clear()  # the toggle's own refresh may write: count it
    await hass.services.async_call("switch", "turn_off", {"entity_id": eco}, blocking=True)
    await _cycle(hass, entry, 65)
    a = hass.states.get(eid)
    assert (a.attributes["target_temp_low"], a.attributes["target_temp_high"]) == (61.5, 64.5)
    assert len(heads[0].writes) == 1
    await _quiet_cycles(hass, entry, 65)
    assert len(heads[0].writes) == 1
