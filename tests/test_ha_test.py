"""The HA test helpers come from one module, and a lane without a plugin fails, never skips.

``tests/ha_test.py`` is loaded here as a fresh copy against invented plugins in a
temporary path, with the real ones hidden. The plugin module names are written out
here, not read from ``ha_test``, so it is not its own oracle.
"""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HA_TEST = ROOT / "tests/ha_test.py"
CUSTOM = "pytest_homeassistant_custom_component"
OFFICIAL = "pytest_homeassistant"
PLUGINS = (CUSTOM, OFFICIAL)
# Copied into checkouts of old tags and run there, where tests/ha_test.py does not exist.
OLD_TAG_SCRIPTS = ROOT / "tests/fixtures/migration"
JOURNEYS = ROOT / ".claude/skills/verify-mxz/journeys"


def invented(package):
    return f"def __getattr__(name):\n    return f'invented {package} {{name}}'\n"


@pytest.fixture
def load(tmp_path, monkeypatch):
    """Load ha_test where only the given invented plugins exist; restore sys.modules after."""
    def plugin(name):
        return name.partition(".")[0] in PLUGINS

    for name in [name for name in sys.modules if plugin(name)]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(tmp_path))

    def run(**commons):
        for package in PLUGINS:
            if package in commons:
                (tmp_path / package).mkdir()
                (tmp_path / package / "__init__.py").write_text("")
                (tmp_path / package / "common.py").write_text(commons[package])
            else:
                monkeypatch.setitem(sys.modules, package, None)
        spec = importlib.util.spec_from_file_location("ha_test_under_test", HA_TEST)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    yield run
    for name in [name for name in sys.modules if plugin(name)]:
        del sys.modules[name]


def test_with_neither_plugin_the_import_fails_naming_both(load):
    with pytest.raises(ModuleNotFoundError) as caught:
        load()
    assert caught.value.name == OFFICIAL
    assert caught.value.__context__.name == CUSTOM


def test_the_official_plugin_serves_when_the_custom_one_is_absent(load):
    ha_test = load(**{OFFICIAL: invented(OFFICIAL)})
    assert ha_test.common.__name__ == f"{OFFICIAL}.common"
    assert ha_test.MockConfigEntry == f"invented {OFFICIAL} MockConfigEntry"
    assert ha_test.async_fire_time_changed == f"invented {OFFICIAL} async_fire_time_changed"


def test_the_custom_plugin_comes_first_when_both_are_installed(load):
    ha_test = load(**{CUSTOM: invented(CUSTOM), OFFICIAL: invented(OFFICIAL)})
    assert ha_test.common.__name__ == f"{CUSTOM}.common"
    assert ha_test.mock_restore_cache == f"invented {CUSTOM} mock_restore_cache"


def test_a_broken_custom_plugin_raises_its_own_error_and_never_falls_back(load):
    broken = "import invented_missing_dependency\n"
    with pytest.raises(ModuleNotFoundError) as caught:
        load(**{CUSTOM: broken, OFFICIAL: invented(OFFICIAL)})
    assert caught.value.name == "invented_missing_dependency"


def test_the_lanes_helpers_are_the_plugin_modules_own():
    from tests import ha_test

    assert ha_test.common.__name__ in {f"{package}.common" for package in PLUGINS}
    helpers = [name for name in vars(ha_test) if name[0].isalpha() and name != "common"]
    assert len(helpers) == 13
    assert all(getattr(ha_test, name) is getattr(ha_test.common, name) for name in helpers)


def plugin_import_violations(source, path="invented.py"):
    """A file-level importorskip, or an import of a plugin module, anywhere in ``source``."""
    tree = ast.parse(source)
    errors = []
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(statement):
            called = getattr(node, "func", None)
            if "importorskip" in (getattr(called, "attr", None), getattr(called, "id", None)):
                errors.append(f"{path}:{node.lineno}: file-level importorskip")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module]
        else:
            continue
        errors += [f"{path}:{node.lineno}: imports {name}" for name in names
                   if name.partition(".")[0] in PLUGINS]
    return errors


def checked_files():
    files = [path for path in (ROOT / "tests").rglob("*.py")
             if path != HA_TEST and OLD_TAG_SCRIPTS not in path.parents]
    return sorted(files + list(JOURNEYS.glob("*.py")))


def test_every_test_takes_the_helpers_from_ha_test_and_none_skips_at_import():
    files = checked_files()
    assert ROOT / "tests/test_climate.py" in files
    assert JOURNEYS / "mxz_journey.py" in files
    errors = []
    for path in files:
        errors += plugin_import_violations(path.read_text(), path.relative_to(ROOT))
    assert errors == []


def test_ha_test_itself_is_what_the_check_would_refuse():
    assert plugin_import_violations(HA_TEST.read_text()) == [
        f"invented.py:14: imports {CUSTOM}",
        f"invented.py:18: imports {OFFICIAL}",
    ]


@pytest.mark.parametrize(("source", "expected"), [
    ('import pytest\n\npytest.importorskip("homeassistant")\n',
     ["invented.py:3: file-level importorskip"]),
    ('import pytest\nnumpy = pytest.importorskip("numpy")\n',
     ["invented.py:2: file-level importorskip"]),
    ("from pytest import importorskip\nif True:\n    importorskip('homeassistant')\n",
     ["invented.py:3: file-level importorskip"]),
    (f"from {CUSTOM}.common import MockConfigEntry\n", [f"invented.py:1: imports {CUSTOM}.common"]),
    (f"from {OFFICIAL} import common\n", [f"invented.py:1: imports {OFFICIAL}"]),
    (f"import {OFFICIAL}.common as plugin\n", [f"invented.py:1: imports {OFFICIAL}.common"]),
    (f"def test():\n    from {CUSTOM}.common import flush_store\n",
     [f"invented.py:2: imports {CUSTOM}.common"]),
    ("def test():\n    import pytest\n    pytest.importorskip('numpy')\n", []),
    ("from tests.ha_test import MockConfigEntry, common\n", []),
    (f"import {OFFICIAL}_lookalike\nfrom .{CUSTOM} import common\n", []),
])
def test_the_check_refuses_exactly_plugin_imports_and_file_level_skips(source, expected):
    assert plugin_import_violations(source) == expected
