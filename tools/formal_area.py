"""One subsystem's formal checks, as its skill under .claude/skills/ runs them. Standard library only.

    tools/formal_area.py list             each subsystem and what it runs
    tools/formal_area.py check AREA       its TLC configs pass every rule they list, its
                                          reachability goals and open findings still break,
                                          and its tests pass
    tools/formal_area.py mutants AREA     each broken copy of the spec and of the code that
                                          guards it is caught

The spec, its configs and its broken copies are tools/formal_tlc.py's; the broken copies of
the code are tools/code_mutants.py's. This file only says which of them belong to which
subsystem, so a change to one subsystem can be checked without the whole model. The mode-change
hold is .claude/skills/verify-mxz-formal's, with its own model and runner (verification/run).

Needs TLA2TOOLS (and JAVA, if java isn't on PATH) for TLC, and MXZ_PYTHON, a lane venv's python
with hypothesis in it, for the tests. Ends AREA_OK only if every case came out as stated.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # run as a script, the repository isn't on the path
from tools import code_mutants, formal_tlc


@dataclass(frozen=True)
class Area:
    skill: str  # the skill under .claude/skills/ that runs it
    configs: tuple[str, ...]  # its configs in docs/formal/tla/; their goals, findings and spec mutants come along
    tests: tuple[str, ...]  # pytest paths: every test its code mutants name is in one of these
    code: tuple[str, ...]  # its broken copies of the code, by id in tools/code_mutants.py


AREAS: dict[str, Area] = {
    "fan": Area(
        "verify-mxz-fan-hold",
        ("ci-fan", "ci-fan-crash", "ci-fan-lag"),
        ("tests/test_properties_fan.py", "tests/test_properties_restart.py", "tests/test_fan_lag_memory.py",
         "tests/test_fan_hold_restore.py"),
        ("C8", "C9", "C10", "C11", "C12", "C13", "C22", "C23", "C31", "C32", "C33", "C34", "C35",
         "C36", "C37", "C38", "C39", "C40", "C41"),
    ),
    "window": Area(
        "verify-mxz-door-window",
        ("ci-window", "ci-window-fan", "ci-arbitration-window"),
        ("tests/test_properties_window.py", "tests/test_properties_restart.py", "tests/test_door_window_logic.py",
         "tests/test_door_window.py"),
        ("C14", "C15", "C16", "C17", "C18", "C19", "C27", "C28", "C29", "C30"),
    ),
    "adopt": Area(
        "verify-mxz-head-changes",
        ("ci-adopt", "ci-adopt-restart"),
        ("tests/test_accept_head_changes.py",),
        ("C44", "C45", "C46", "C47", "C48"),
    ),
    "apply": Area(
        "verify-mxz-mode-record",
        ("ci-apply",),
        ("tests/test_head_fault.py",),
        ("C42", "C43"),
    ),
}


def spec_mutants(area: Area) -> list[str]:
    """The spec's broken copies checked on one of the area's configs."""
    return [name for name, (*_rest, config, _rule) in formal_tlc.MUTANTS.items() if config in area.configs]


def goals(area: Area) -> list[str]:
    """The reachability goals and open findings whose config is one of the area's."""
    return [rule for rule, config in {**formal_tlc.COVERS, **formal_tlc.FINDINGS}.items() if config in area.configs]


def python() -> str:
    found = os.environ.get("MXZ_PYTHON")
    if not found:
        sys.exit("set MXZ_PYTHON to a lane venv's python with hypothesis in it")
    return found


def run_tests(area: Area, out: Path) -> bool:
    done = subprocess.run(
        [python(), "-m", "pytest", "-p", "tools.pytest_phases", "-p", "no:cacheprovider", "-q",
         "--show-capture=no", "--tb=line", *area.tests],
        cwd=ROOT, capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    (out / "pytest.out").write_text(done.stdout + done.stderr)
    tail = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    good = done.returncode == 0
    print(f"{'OK  ' if good else 'FAIL'} tests {' '.join(area.tests)}: {tail}", flush=True)
    return good


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=("list", "check", "mutants"))
    parser.add_argument("area", nargs="?", choices=sorted(AREAS))
    parser.add_argument("--out", type=Path, help="where each run's output is kept (default: a temp folder)")
    args = parser.parse_args()
    if args.what == "list":
        for name, area in AREAS.items():
            print(f"{name} ({area.skill}): configs {' '.join(area.configs)}; goals {' '.join(goals(area))}; "
                  f"spec mutants {' '.join(spec_mutants(area))}; tests {' '.join(area.tests)}; "
                  f"code mutants {' '.join(area.code)}")
        return 0
    if args.area is None:
        parser.error("name an area")
    area = AREAS[args.area]
    out = args.out or Path(tempfile.mkdtemp(prefix=f"mxz-area-{args.area}-"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"output in {out}", flush=True)
    if args.what == "check":
        good = formal_tlc.run_check(out, area.configs)
        good = formal_tlc.run_cover(out, area.configs) and good
        good = run_tests(area, out) and good
    else:
        good = formal_tlc.run_mutants(out, [], area.configs)
        mxz = python()
        for name in area.code:
            good = code_mutants.run(name, mxz, out) and good
    print("AREA_OK" if good else "AREA_FAILED", flush=True)
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
