"""The replays outside ``tests/`` still load what they borrow from the suite.

The verify-mxz journeys and the parked findings' replays run outside
``tests/``, so ``pytest tests/`` never collects them, and a change to the
suite's conftest or helpers can break them while every lane stays green. This
runs none of them (they stay off CI); it checks only that each one's conftest
carries every hook and fixture ``tests/conftest.py`` defines, since an
autouse fixture there may ask for any of the others, and that every name an
outside file imports from ``tests`` still exists.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
OUTSIDE = (ROOT / ".claude/skills/verify-mxz/journeys", ROOT / "verification/findings")


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            return True
    return False


def suite_exports() -> set[str]:
    """Every hook and fixture ``tests/conftest.py`` defines."""
    tree = ast.parse((ROOT / "tests/conftest.py").read_text())
    return {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (node.name.startswith("pytest_") or _is_fixture(node))
    }


def missing_exports(conftest: str) -> set[str]:
    """What an outside conftest leaves out of the suite's hooks and fixtures."""
    imported = {
        alias.name
        for node in ast.walk(ast.parse(conftest))
        if isinstance(node, ast.ImportFrom) and node.module == "tests.conftest"
        for alias in node.names
    }
    return suite_exports() - imported


def unresolved_imports(root: Path) -> list[str]:
    """Each ``from tests.x import name`` under ``root`` whose name ``tests.x`` lacks."""
    missing = []
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tests.")):
                continue
            module = importlib.import_module(node.module)
            missing += [
                f"{path.name}: {node.module}.{alias.name}"
                for alias in node.names if not hasattr(module, alias.name)
            ]
    return missing


def test_the_suite_conftest_has_the_clock_and_the_autouse_fixture() -> None:
    assert {"pytest_configure", "auto_enable_custom_integrations", "mxz_clock"} <= suite_exports()


@pytest.mark.parametrize("root", OUTSIDE, ids=lambda root: root.parent.name)
def test_each_outside_conftest_carries_every_suite_hook_and_fixture(root: Path) -> None:
    assert missing_exports((root / "conftest.py").read_text()) == set()


def test_a_conftest_without_the_clock_is_refused() -> None:
    conftest = "from tests.conftest import auto_enable_custom_integrations, pytest_configure\n"
    assert missing_exports(conftest) == {"mxz_clock"}


@pytest.mark.parametrize("root", OUTSIDE, ids=lambda root: root.parent.name)
def test_every_suite_name_an_outside_replay_imports_exists(root: Path) -> None:
    assert list(root.glob("test_*.py"))
    assert unresolved_imports(root) == []


def test_an_import_of_a_removed_helper_is_refused(tmp_path: Path) -> None:
    (tmp_path / "test_journey.py").write_text(
        "from tests.clocks import pass_time\nfrom tests.clocks import age_lost_vote\n"
    )
    assert unresolved_imports(tmp_path) == ["test_journey.py: tests.clocks.age_lost_vote"]
