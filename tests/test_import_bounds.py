"""The integration's import boundaries (tools/import_bounds.py, docs/ARCHITECTURE-RULES.md).

The first test is the gate on the real tree. Each refusal below adds one line to a real
module's source and expects exactly the violation that line makes.
"""

import pytest

from tools import import_bounds
from tools.import_bounds import read_sources, violations

SOURCES = read_sources()
LOWER = "; only lower layers may be imported"


def added(module, code):
    """The real sources with ``code`` appended to ``module``, and its first line number."""
    sources = dict(SOURCES)
    text = sources.get(module, "")
    if text and not text.endswith("\n"):
        text += "\n"
    sources[module] = text + code
    return sources, text.count("\n") + 1


def test_the_integration_keeps_its_import_bounds():
    assert violations(SOURCES) == []


def test_every_module_on_disk_is_checked():
    files = {path.stem for path in import_bounds.SOURCE.glob("*.py")}
    assert set(SOURCES) == files
    assert {"coordinator", "openings", "__init__"} <= files


def test_without_its_exceptions_the_tree_shows_exactly_the_four_known_edges(monkeypatch):
    monkeypatch.setattr(import_bounds, "EXCEPTIONS", {})
    assert violations(SOURCES) == [
        "climate.py:66: climate (layer surfaces) imports number (layer surfaces)" + LOWER,
        "dry.py:33: dry (layer lower controllers) type-only imports coordinator (layer coordinator)" + LOWER,
        "heads.py:72: heads (layer head drivers) type-only imports coordinator (layer coordinator)" + LOWER,
        "openings.py:47: openings (layer controllers) type-only imports coordinator (layer coordinator)" + LOWER,
    ]


@pytest.mark.parametrize(("module", "code", "message"), [
    ("logic", "from .coordinator import Zone\n",
     "logic (layer helpers) imports coordinator (layer coordinator)"),
    ("coordinator", "from .climate import MXZClimate\n",
     "coordinator (layer coordinator) imports climate (layer surfaces)"),
    ("const", "from .logic import finite_number\n",
     "const (layer constants) imports logic (layer helpers)"),
    ("logic", "from .capabilities import head_lists_dry\n",
     "logic (layer helpers) imports capabilities (layer helpers)"),
    ("climate", "from .switch import MXZSwitch\n",
     "climate (layer surfaces) imports switch (layer surfaces)"),
    ("entity", "from .sensor import MXZPlanSensor\n",
     "entity (layer entity base) imports sensor (layer surfaces)"),
    ("openings", "def late():\n    from .coordinator import read_room_temp\n",
     "openings (layer controllers) imports coordinator (layer coordinator)"),
    ("openings", "from custom_components.mxz_coordinator.coordinator import Zone\n",
     "openings (layer controllers) imports coordinator (layer coordinator)"),
    ("dry", "from .openings import OpeningPause\n",
     "dry (layer lower controllers) imports openings (layer controllers)"),
    ("host", "from .dry import DryMode\n",
     "host (layer helpers) imports dry (layer lower controllers)"),
    ("fan_hold", "from .openings import OpeningPause\n",
     "fan_hold (layer lower controllers) imports openings (layer controllers)"),
    ("fan_hold", "from .dry import DryMode\n",
     "fan_hold (layer lower controllers) imports dry (layer lower controllers)"),
    ("fan_hold", "if TYPE_CHECKING:\n    from .coordinator import Zone\n",
     "fan_hold (layer lower controllers) type-only imports coordinator (layer coordinator)"),
    ("delivery", "from .openings import OpeningPause\n",
     "delivery (layer controllers) imports openings (layer controllers)"),
    ("delivery", "if TYPE_CHECKING:\n    from .coordinator import MXZCoordinator\n",
     "delivery (layer controllers) type-only imports coordinator (layer coordinator)"),
    ("dry", "from .delivery import DeliveryQueue\n",
     "dry (layer lower controllers) imports delivery (layer controllers)"),
    ("fan_hold", "if TYPE_CHECKING:\n    from .delivery import DeliveryQueue\n",
     "fan_hold (layer lower controllers) type-only imports delivery (layer controllers)"),
    ("delivery", "if TYPE_CHECKING:\n    from .heads import HeadDrivers\n",
     "delivery (layer controllers) type-only imports heads (layer head drivers)"),
    ("openings", "from .heads import HeadDrivers\n",
     "openings (layer controllers) imports heads (layer head drivers)"),
    ("heads", "from .coordinator import Zone\n",
     "heads (layer head drivers) imports coordinator (layer coordinator)"),
    ("fan_hold", "from .heads import HeadGrid\n",
     "fan_hold (layer lower controllers) imports heads (layer head drivers)"),
    ("dry", "if TYPE_CHECKING:\n    from .heads import HeadDrivers\n",
     "dry (layer lower controllers) type-only imports heads (layer head drivers)"),
    ("cloud", "import custom_components.mxz_coordinator.coordinator\n",
     "cloud (layer helpers) imports coordinator (layer coordinator)"),
    ("cloud", "from . import coordinator\n",
     "cloud (layer helpers) imports coordinator (layer coordinator)"),
    ("logic", "from . import DOMAIN\n",
     "logic (layer helpers) imports __init__ (layer setup)"),
    ("logic", "from custom_components import mxz_coordinator\n",
     "logic (layer helpers) imports __init__ (layer setup)"),
    ("logic", "if TYPE_CHECKING:\n    from .coordinator import MXZCoordinator\n",
     "logic (layer helpers) type-only imports coordinator (layer coordinator)"),
    ("diagnostics", "from .nowhere import thing\n",
     "diagnostics (layer surfaces) imports nowhere (no layer)"),
])
def test_an_upward_or_sideways_import_is_refused(module, code, message):
    sources, line = added(module, code)
    if code.startswith(("def ", "if ")):
        line += 1
    assert violations(sources) == [f"{module}.py:{line}: {message}{LOWER}"]


@pytest.mark.parametrize(("module", "code", "outside"), [
    ("logic", "from homeassistant.core import HomeAssistant\n", "homeassistant"),
    ("cloud", "import voluptuous as vol\n", "voluptuous"),
    ("const", "if TYPE_CHECKING:\n    from homeassistant.core import State\n", "homeassistant"),
])
def test_a_pure_module_imports_only_the_standard_library(module, code, outside):
    sources, line = added(module, code)
    if code.startswith("if "):
        line += 1
    assert violations(sources) == [
        f"{module}.py:{line}: {module} imports {outside}; it imports only the standard library"]


def test_a_module_outside_the_pure_set_may_import_home_assistant():
    sources, _ = added("capabilities", "from homeassistant.helpers import entity_registry\n")
    assert violations(sources) == []


def test_a_downward_import_is_allowed():
    sources, _ = added("switch", "from .logic import finite_number\nfrom .entity import MXZEntity\n")
    assert violations(sources) == []


def test_a_relative_import_cannot_leave_the_package():
    sources, line = added("climate", "from .. import other\n")
    assert violations(sources) == [f"climate.py:{line}: a relative import leaves the package"]


def test_a_type_only_exception_does_not_admit_a_runtime_import():
    sources, line = added("openings", "from .coordinator import Zone\n")
    assert violations(sources) == [
        f"openings.py:{line}: openings (layer controllers) imports coordinator (layer coordinator){LOWER}"]


def test_a_new_module_must_be_placed_in_a_layer():
    sources, _ = added("driver", "from .const import DOMAIN\n")
    assert violations(sources) == ["driver.py is in no layer of tools/import_bounds.py"]


def test_a_layer_cannot_name_a_missing_module():
    sources = {name: text for name, text in SOURCES.items() if name != "sensor"}
    assert violations(sources) == ["tools/import_bounds.py places sensor, which has no file"]


def test_an_exception_that_no_longer_matches_fails():
    sources = dict(SOURCES)
    sources["climate"] = sources["climate"].replace("from .number import range_error\n", "")
    assert violations(sources) == ["exception climate -> number matches no runtime import; remove or narrow it"]


def test_a_runtime_exception_used_only_by_a_type_only_import_must_be_narrowed():
    sources = dict(SOURCES)
    sources["climate"] = sources["climate"].replace(
        "from .number import range_error\n", "if TYPE_CHECKING:\n    from .number import range_error\n")
    assert violations(sources) == ["exception climate -> number matches no runtime import; remove or narrow it"]


def test_the_command_fails_on_a_violation(tmp_path, capsys):
    for name, text in SOURCES.items():
        (tmp_path / f"{name}.py").write_text(text)
    assert import_bounds.main(tmp_path) == 0
    assert capsys.readouterr().out == ""
    with (tmp_path / "logic.py").open("a") as stream:
        stream.write("from .coordinator import Zone\n")
    assert import_bounds.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert out.startswith("logic.py:") and "imports coordinator" in out
