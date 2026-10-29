"""The test heads' base serves the native_ temperature names on every core.

HA 2026.11 added native_* temperature names to ClimateEntity; older cores read
only the old ones. tests/head_entity.py lets a head use only the native_ names
anywhere. Without it, on an older core a native_ write would just set an
unread attribute, so every head must stand on it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.climate import ClimateEntity

from tests.head_entity import NAMES, NATIVE, HeadEntity


def _read_by_core(entity: ClimateEntity, name: str):
    """The value HA itself reads for ``name``: the native_ property where it has one."""
    return getattr(entity, f"native_{name}" if NATIVE else name)


@pytest.mark.parametrize("name", NAMES)
def test_a_native_write_is_what_the_core_reads(name: str) -> None:
    """Each write, also a second one after the core cached the first, reaches HA."""

    class Head(HeadEntity):
        pass

    head = Head()
    setattr(head, f"_attr_native_{name}", 61)
    assert _read_by_core(head, name) == 61
    setattr(head, f"_attr_native_{name}", 63)
    assert _read_by_core(head, name) == 63
    assert getattr(head, f"_attr_native_{name}") == 63


def test_a_native_class_unit_is_what_the_core_reads() -> None:
    """A class-level unit, also one a subclass overrides."""

    class HeadF(HeadEntity):
        _attr_native_temperature_unit = "°F"

    class HeadC(HeadF):
        _attr_native_temperature_unit = "°C"

    assert _read_by_core(HeadF(), "temperature_unit") == "°F"
    assert _read_by_core(HeadC(), "temperature_unit") == "°C"


def test_without_the_base_an_older_core_ignores_a_native_write() -> None:
    """Control: on a core before 2026.11 a plain ClimateEntity's write is lost."""

    class Plain(ClimateEntity):
        pass

    plain = Plain()
    plain._attr_native_current_temperature = 61
    assert (_read_by_core(plain, "current_temperature") == 61) is NATIVE


def _climate_bases(path: Path) -> dict[str, list[str]]:
    """Each class in ``path`` that names ClimateEntity among its bases."""
    return {
        node.name: [ast.unparse(base) for base in node.bases]
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ClassDef)
        and any(ast.unparse(base).endswith("ClimateEntity") for base in node.bases)
    }


def test_every_test_head_stands_on_the_base() -> None:
    """Only HeadEntity itself subclasses ClimateEntity in tests/ (plain test above aside)."""
    tests = Path(__file__).parent
    found = {
        f"{path.name}::{name}": bases
        for path in sorted(tests.glob("*.py"))
        for name, bases in _climate_bases(path).items()
    }
    assert found == {
        "head_entity.py::HeadEntity": ["ClimateEntity"],
        "test_head_entity.py::Plain": ["ClimateEntity"],
    }
