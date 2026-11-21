"""The verification instance's invented head uses the climate temperature names its core reads.

HA 2026.11 renamed ClimateEntity's temperature members native_*; it logs a deprecation for a
class that still provides an old name, once, when the class is defined. HA 2024.12 to 2026.10
read only the old names. ``verification/instance/invented_house/climate.py`` runs as its own
custom component, so it reads which names its core has from HA itself; ``tests/simlab_parity.py``
runs its heads inside Home Assistant on every release and compares what they show.
"""

from __future__ import annotations

import json
import subprocess
import sys
from functools import cache
from pathlib import Path

from homeassistant.components.climate import ClimateEntity

ROOT = Path(__file__).parents[1]
NATIVE = hasattr(ClimateEntity, "native_current_temperature")
OLD = {"_attr_temperature_unit", "target_temperature_low", "target_temperature_high"}
NEW = {"_attr_native_temperature_unit", "native_target_temperature_low", "native_target_temperature_high"}

# The component imported fresh, as Home Assistant loads it, then a control class defined after
# it that still provides an old name: 2026.11 and later report that one.
DEFINE = '''
import json, logging
logging.basicConfig(format="%(name)s: %(message)s")
from homeassistant.components.climate import ClimateEntity
from invented_house.climate import InventedHead
logging.getLogger("define").warning("IMPORTED")
class Control(ClimateEntity):
    _attr_temperature_unit = "°F"
print(json.dumps(sorted(vars(InventedHead))))
'''


@cache
def _define() -> tuple[set[str], str, str]:
    result = subprocess.run(
        [sys.executable, "-c", DEFINE],
        cwd=ROOT, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "verification" / "instance")},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    imported, control = result.stderr.split("define: IMPORTED\n")
    return set(json.loads(result.stdout)), imported, control


def test_the_head_provides_only_the_names_its_core_reads() -> None:
    names, _, _ = _define()
    assert names & (OLD | NEW) == (NEW if NATIVE else OLD)


def test_defining_the_head_logs_no_deprecation() -> None:
    _, imported, control = _define()
    assert imported == ""
    if not NATIVE:
        assert control == ""
        return
    [reported] = control.splitlines()
    assert reported.startswith("__main__: __main__::Control provides the deprecated _attr_temperature_unit,")
