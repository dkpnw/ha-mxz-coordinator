"""Import boundaries of custom_components/mxz_coordinator (docs/ARCHITECTURE-RULES.md).

Each module sits in one layer of LAYERS and imports only from layers below its own.
STDLIB_ONLY modules also import nothing outside the standard library. Every import
counts: module level, inside a function, and under TYPE_CHECKING; relative and
absolute forms resolve to the same module. An import built at run time (importlib,
__import__) is not seen.

`python3 tools/import_bounds.py` prints each violation; exit 0 clean, 1 otherwise.
tests/test_import_bounds.py runs the same check in every suite job.
"""

import ast
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

PACKAGE = "custom_components.mxz_coordinator"
SOURCE = Path(__file__).resolve().parents[1] / "custom_components" / "mxz_coordinator"

# Lowest first. A new module joins a layer here, in the same change that adds it.
LAYERS = (
    ("constants", {"const"}),
    ("helpers", {"logic", "cloud", "capabilities", "host"}),
    # The plan one compute decides and the attributes it is shown as.
    ("plan", {"plan"}),
    # Concerns split out of the coordinator. Each reaches it through a Protocol it declares.
    # A controller another one calls sits below it: the door/window pause puts back a Dry
    # and snapshots the fan hold.
    ("lower controllers", {"dry", "fan_hold"}),
    ("controllers", {"delivery", "openings"}),
    # Each room's head driver sends through the delivery queue, which reaches it through
    # HeadSteps, a Protocol the queue declares.
    ("head drivers", {"heads"}),
    ("coordinator", {"coordinator"}),
    ("entity base", {"entity"}),
    ("surfaces", {"binary_sensor", "button", "climate", "config_flow", "diagnostics",
                  "issues", "number", "select", "sensor", "switch"}),
    ("setup", {"__init__"}),
)

# Kept free of Home Assistant so their math is tested directly (docs/DEVELOPMENT.md).
STDLIB_ONLY = {"const", "logic", "cloud", "plan"}

# (importer, imported, type_only): why. Each one is an architecture decision. A runtime
# exception also covers type-only imports; one that no longer matches an import of its own
# kind fails, so it is removed or narrowed when its reason goes away.
EXCEPTIONS = {
    ("climate", "number", False):
        "range_error, the refusal both dual-setpoint surfaces raise, is defined in number.py",
    ("openings", "coordinator", True):
        "Zone, the room's configuration, is defined in coordinator.py; "
        "the pause reaches the coordinator itself only through OpeningHost",
    ("dry", "coordinator", True):
        "Zone, the room's configuration, is defined in coordinator.py; "
        "Dry reaches the coordinator itself only through DryHost",
    ("heads", "coordinator", True):
        "Zone, the room's configuration, is defined in coordinator.py; "
        "the head drivers reach the coordinator itself only through HeadsHost",
}


def layer_of(module: str) -> int | None:
    return next((i for i, (_, members) in enumerate(LAYERS) if module in members), None)


def _type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
            or isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _nodes(node: ast.AST, type_only: bool = False) -> Iterator[tuple[ast.Import | ast.ImportFrom, bool]]:
    """Every import statement under ``node``, with whether only a type checker runs it."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        yield node, type_only
        return
    if isinstance(node, ast.If) and _type_checking(node.test):
        for child in node.body:
            yield from _nodes(child, True)
        for child in node.orelse:
            yield from _nodes(child, type_only)
        return
    for child in ast.iter_child_nodes(node):
        yield from _nodes(child, type_only)


def _inside(path: str) -> str | None:
    """The rest of a dotted path inside the package ("" for the package), or None outside it."""
    if path == PACKAGE:
        return ""
    return path[len(PACKAGE) + 1:] if path.startswith(PACKAGE + ".") else None


def _named(rest: str, name: str | None, modules: set[str]) -> str:
    """The module that importing ``name`` from ``rest`` (a path inside the package) reaches.

    ``from .x import y`` reaches module x whatever y is; ``from . import y`` reaches module y,
    or the package itself when y is not a module. ``name`` is None for a plain import.
    """
    if rest:
        return rest.split(".")[0]
    return name if name in modules else "__init__"


def targets(node: ast.Import | ast.ImportFrom, modules: set[str]) -> list[tuple[bool, str]]:
    """(inside the package, module) for each module ``node`` imports, without repeats.

    Inside, the module is a package module (``__init__`` for the package itself);
    outside, it is the top-level name, or ".." for a relative import that leaves the package.
    """
    found = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            rest = _inside(alias.name)
            found.append((False, alias.name.split(".")[0]) if rest is None else (True, _named(rest, None, modules)))
    elif node.level > 1:
        found.append((False, ".."))
    elif node.level == 1:
        found += [(True, _named(node.module or "", alias.name, modules)) for alias in node.names]
    else:
        module = node.module or ""
        rest = _inside(module)
        for alias in node.names:
            whole = _inside(f"{module}.{alias.name}")  # from custom_components import mxz_coordinator
            if rest is not None:
                found.append((True, _named(rest, alias.name, modules)))
            elif whole is not None:
                found.append((True, _named(whole, None, modules)))
            else:
                found.append((False, module.split(".")[0]))
    return list(dict.fromkeys(found))


def violations(sources: Mapping[str, str]) -> list[str]:
    """Every broken bound, given each package module's source by module name."""
    modules = set(sources)
    placed = {m for _, members in LAYERS for m in members}
    found = [f"{m}.py is in no layer of tools/import_bounds.py" for m in sorted(modules - placed)]
    found += [f"tools/import_bounds.py places {m}, which has no file" for m in sorted(placed - modules)]
    used = set()
    for importer in sorted(modules & placed):
        own = layer_of(importer)
        tree = ast.parse(sources[importer], filename=f"{importer}.py")
        for node, type_only in _nodes(tree):
            where = f"{importer}.py:{node.lineno}"
            for inside, target in targets(node, modules):
                if not inside:
                    if target == "..":
                        found.append(f"{where}: a relative import leaves the package")
                    elif importer in STDLIB_ONLY and target not in sys.stdlib_module_names:
                        found.append(f"{where}: {importer} imports {target}; it imports only the standard library")
                    continue
                theirs = layer_of(target)
                if theirs is not None and theirs < own:
                    continue
                if (importer, target, type_only) in EXCEPTIONS or (importer, target, False) in EXCEPTIONS:
                    used.add((importer, target, type_only))
                    continue
                kind = "type-only " if type_only else ""
                there = f"layer {LAYERS[theirs][0]}" if theirs is not None else "no layer"
                found.append(f"{where}: {importer} (layer {LAYERS[own][0]}) {kind}imports {target} ({there}); "
                             "only lower layers may be imported")
    found += [f"exception {importer} -> {target} matches no {'type-only' if type_only else 'runtime'} import; "
              "remove or narrow it" for importer, target, type_only in sorted(EXCEPTIONS.keys() - used)]
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
