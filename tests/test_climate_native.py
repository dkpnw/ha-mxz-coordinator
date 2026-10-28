"""The room thermostats use the climate temperature names their core reads.

HA 2026.11 renamed ClimateEntity's temperature members native_*
(native_temperature_unit, native_current_temperature, native_target_temperature,
and the _low/_high pair). The old names still work until 2027.11, but HA logs a
deprecation for a subclass that still provides one, when the class is defined, and
for code that reads one, once per call site. The room thermostats provide only the
names their core reads, and the coordinator reads a head's unit through the name
its core has. Every check runs on every core: before 2026.11 HA logs no such
deprecation at all, so the controls below expect silence there.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import ClimateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import deprecation
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.mxz_coordinator.climate import (
    MXZRangeRoomClimate,
    MXZRoomClimate,
)
from tests.pin_scenario import room_page_pass
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    MockHead,
    MockHeadC,
    _eid,
    _recompute,
    _set_temp,
    _setup_fan_boost,
    _setup_mock_heads,
)

# Read from HA itself, not from the integration's own switch.
NATIVE = hasattr(ClimateEntity, "native_current_temperature")
NAMES = {
    MXZRoomClimate: ("current_temperature", "target_temperature"),
    MXZRangeRoomClimate: ("target_temperature_low", "target_temperature_high"),
}
ALL_NAMES = [name for names in NAMES.values() for name in names] + ["temperature_unit"]


@pytest.mark.parametrize("cls", list(NAMES), ids=lambda cls: cls.__name__)
def test_each_thermostat_provides_only_the_names_its_core_reads(cls: type) -> None:
    """Exactly the native_ names on 2026.11 and later, exactly the old ones before."""
    old = set(NAMES[cls])
    native = {f"native_{name}" for name in old}
    provided = set(vars(cls)) & {
        *ALL_NAMES, *(f"native_{name}" for name in ALL_NAMES),
        *(f"_attr_{name}" for name in ALL_NAMES),
        *(f"_attr_native_{name}" for name in ALL_NAMES),
    }
    assert provided == (native if NATIVE else old)


# HA checks a ClimateEntity subclass once, when the class is created, so the
# platform is imported in a new interpreter, as HA loads it. The control class
# defined after it still provides an old name: 2026.11 and later report it.
DEFINE = '''
import logging
logging.basicConfig(format="%(name)s: %(message)s")
import custom_components.mxz_coordinator.climate as climate
logging.getLogger("define").warning("IMPORTED")
class Control(climate.ClimateEntity):
    _attr_temperature_unit = "°C"
'''


def test_defining_the_thermostats_logs_no_deprecation() -> None:
    """Importing the climate platform fresh logs nothing; the control may."""
    result = subprocess.run(
        [sys.executable, "-c", DEFINE],
        cwd=Path(__file__).parents[1], env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "."},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    imported, control = result.stderr.split("define: IMPORTED\n")
    assert imported == ""
    if not NATIVE:
        assert control == ""
        return
    [reported] = control.splitlines()
    assert reported.startswith(
        "__main__: __main__::Control provides the deprecated _attr_temperature_unit,"
    )


class _StoresSetpointsUnderTheCoresName:
    """A test head that stores what it is sent under the names its core reads.

    HA reports a deprecated read or write against the nearest integration on
    the call stack. A real head integration is that nearest frame for its own
    code; test_drive's heads live in tests/, so their own old-name writes, made
    inside a command MXZ sends, would be reported against MXZ. Their class-level
    temperature unit keeps the old name: that is what MXZ reads.
    """

    async def async_set_temperature(self, **kwargs: Any) -> None:
        prefix = "_attr_native_" if NATIVE else "_attr_"
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = mode
        if (low := kwargs.get("target_temp_low")) is not None:
            setattr(self, f"{prefix}target_temperature_low", low)
        if (high := kwargs.get("target_temp_high")) is not None:
            setattr(self, f"{prefix}target_temperature_high", high)
        self.async_write_ha_state()


class OldUnitHeadF(_StoresSetpointsUnderTheCoresName, MockHead):
    """A °F head whose unit uses the old name."""


class OldUnitHeadC(_StoresSetpointsUnderTheCoresName, MockHeadC):
    """A °C head whose unit uses the old name."""


def _deprecations(caplog: pytest.LogCaptureFixture, about: str) -> list[str]:
    """Logged deprecation messages that name ``about`` or come from its logger."""
    return [
        record.getMessage()
        for record in caplog.records
        if "deprecated" in record.getMessage()
        and (about in record.getMessage() or record.name.startswith(about))
    ]


@pytest.mark.parametrize("head", [OldUnitHeadF, OldUnitHeadC], ids=["F-head", "C-head"])
async def test_running_mxz_reads_no_deprecated_member(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    head: type[MockHead],
) -> None:
    """Commands, bounds, the setpoint echo check and both tile shapes log nothing.

    The heads declare their unit under the old name, as a head integration
    not yet migrated does; MXZ reading it must not be reported against MXZ.
    The system is °F, so the °C head takes the unit-converting paths.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    # HA 2026.11 reports each call site once per process; start from none reported.
    monkeypatch.setattr(
        deprecation, "_REPORTED_DEPRECATED_ENTITY_USAGE", set(), raising=NATIVE
    )
    caplog.set_level(logging.WARNING)
    head_a, head_b = await _setup_mock_heads(hass, cls=head)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    coordinator = entry.runtime_data

    # Room A cools to its 62: a head command, its band and the setpoint echo check.
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    await _recompute(hass, entry)
    assert coordinator.head_target_bounds(head_a)
    # A tile write, then the second room as a range tile.
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": _eid(hass, entry, "_primary_thermostat"), "temperature": 64},
        blocking=True,
    )
    await room_page_pass(hass, entry, 2, {"dual_setpoint": True})
    await _recompute(hass, entry)
    secondary = hass.states.get(_eid(hass, entry, "_secondary_thermostat"))
    assert secondary.attributes["target_temp_low"] is not None

    assert _deprecations(caplog, "mxz_coordinator") == []

    # Control: the capture sees such a report where HA makes one. Read from this
    # test, the old name is reported against the head's own module.
    assert coordinator._head_entity(head_a).temperature_unit
    reads = [m for m in _deprecations(caplog, head.__module__) if "reads the" in m]
    if not NATIVE:
        assert reads == []
        return
    assert len(reads) == 1, reads
    assert f"reads the deprecated {head.__name__}.temperature_unit," in reads[0]
