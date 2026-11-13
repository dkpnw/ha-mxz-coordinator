"""No raw Home Assistant timer in custom_components/mxz_coordinator (docs/ARCHITECTURE-RULES.md).

Every wait MXZ arms goes through its clock, ``coord.clock.call_later`` (clock.py), the
one module that calls ``homeassistant.helpers.event.async_call_later``. Any other module
that names it fails: an import, an alias, an attribute (``event.async_call_later``) or a
call, at module level or inside a function. A name built at run time (``getattr`` with
a string) is not seen.

`python3 tools/raw_timers.py` prints each violation; exit 0 clean, 1 otherwise.
tests/test_raw_timers.py runs the same check in every suite job.
"""

import ast
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "custom_components" / "mxz_coordinator"
BANNED = "async_call_later"
ALLOWED = "clock"

WHY = (
    "Do not call async_call_later in product code. Schedule the wait on "
    "coord.clock.call_later so it is cancelled on retire, advances in tests "
    "without patching Home Assistant, and a wait that must survive a reload "
    "is persisted (see the mode-change hold, 180ee6e / PR #64). Adding "
    "another raw timer will reset on reload, share a dict with an unrelated "
    "wait, or miss a retire, as the heal/adopt timers and the anti-flip "
    "hold already did."
)


def _names(node: ast.alias | ast.Attribute | ast.Name) -> set[str | None]:
    """The names ``node`` binds or reads: an import and its alias, an attribute, a name."""
    if isinstance(node, ast.alias):
        return {node.name.split(".")[-1], node.asname}
    return {node.attr} if isinstance(node, ast.Attribute) else {node.id}


def _uses(tree: ast.AST) -> Iterator[int]:
    """The line of each place ``tree`` names the banned timer."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.alias, ast.Attribute, ast.Name)) and BANNED in _names(node):
            yield node.lineno


def violations(sources: Mapping[str, str]) -> list[str]:
    """Every use of the banned timer outside the clock, given each module's source by name."""
    found = [] if ALLOWED in sources else [f"tools/raw_timers.py allows {ALLOWED}.py, which has no file"]
    for module in sorted(set(sources) - {ALLOWED}):
        tree = ast.parse(sources[module], filename=f"{module}.py")
        found += [f"{module}.py:{line}: {WHY}" for line in sorted(set(_uses(tree)))]
    return found


def read_sources(folder: Path = SOURCE) -> dict[str, str]:
    return {path.stem: path.read_text() for path in sorted(folder.glob("*.py"))}


def main(folder: Path = SOURCE) -> int:
    found = violations(read_sources(folder))
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
