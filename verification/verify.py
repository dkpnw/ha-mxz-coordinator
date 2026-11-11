"""Run the mode-hold verification, or prove each check can fail. Stdlib only.

    verification/run            # check: TLC configs, property tests, finding replays
    verification/run mutants    # break the code or the model in a scratch copy, each check must catch it

See .claude/skills/verify-mxz-formal/SKILL.md. Needs MXZ_PYTHON (a lane venv
python), JAVA (Java 11 or later) and TLA2TOOLS (tla2tools.jar v1.7.4, checked
against its sha256 below). TLC_WORKERS sets TLC's -workers (default auto).
Output goes to $MXZ_FORMAL_OUT, by default ~/verify-mxz-formal/<UTC stamp>/,
outside the checkout.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MODEL = "verification/modehold"
SPEC = "MXZModeHold.tla"
TLA2TOOLS_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"

# Every config must pass (None) or break exactly one named rule (an open
# finding). F1 to F3 are fixed: their finding-* configs, the shortest path to
# each, now must pass, and their rules are in the ci-* configs.
CONFIGS = {
    "ci-restarts": None,
    "ci-restarts-rest-cool": None,
    "ci-restarts-rest-heat": None,
    "ci-votes": None,
    "ci-votes-rest-heat": None,
    "ci-votes-restarts": None,
    "ci-votes-restarts-rest-heat": None,
    "ci-crash": None,
    "ci-crash-rest-heat": None,
    "finding-resting": None,
    "finding-reload-vote": None,
    "finding-crash": None,
    # The door/window pause (WithOpenings): a paused room never votes, a
    # pause never moves the hold, an override is never paused.
    "ci-openings": None,
    "ci-openings-restarts": None,
}
PROPS = "tests/test_modehold_properties.py"
FINDINGS = "tests/test_modehold_findings.py"
PROMISES = "tests/test_hold_promises.py"
# The door/window pause on the real logic and the real integration.
OPENINGS_LOGIC = "tests/test_door_window_logic.py"
OPENINGS = "tests/test_door_window.py"
# The replays of fixed findings: the finding case and its control must pass.
REPLAYS = {
    f"{FINDINGS}::test_f1_resting_mode_takes_the_mode_from_a_room_still_running": "F1",
    f"{FINDINGS}::test_f2_a_reload_forgets_that_a_room_now_runs_in_the_voted_mode": "F2",
    f"{FINDINGS}::test_f3_a_crash_soon_after_a_change_restores_the_old_mode_inside_the_hold": "F3",
}

TLA = f"{MODEL}/{SPEC}"


def _code_mutants() -> Any:
    """tools/code_mutants.py, loaded from its path (this runs isolated, with -I)."""
    spec = importlib.util.spec_from_file_location("code_mutants", ROOT / "tools/code_mutants.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


code_mutants = _code_mutants()
# The code's broken copies that are the mode-hold rules' twins.
CODE_MUTANTS = [name for name in code_mutants.MUTANTS if name.startswith("H")]


# The model's broken copies: (id, what breaks, old text, new text, config, rule
# that must break). The code's broken copies that are these rules' twins are the
# H mutants in tools/code_mutants.py, which `mutants` runs after these.
MODEL_MUTANTS = [
    ("M1", "model: the gate reads the wall clock",
     "Mono == real - boot", "Mono == real + skew",
     "ci-restarts", "HoldKept"),
    ("M2", "model: no clamp for a stored time in the future",
     "IF rec = NoTime THEN NoTime ELSE monoNow - Max0(wallNow - rec)",
     "IF rec = NoTime THEN NoTime ELSE monoNow - (wallNow - rec)",
     "ci-restarts", "NeverLonger"),
    ("M3", "model: a reload or restart starts a full hold (#64's bug)",
     "IF rec = NoTime THEN NoTime ELSE monoNow - Max0(wallNow - rec)",
     "IF rec = NoTime THEN NoTime ELSE monoNow",
     "ci-restarts", "NoExtraWait"),
    ("M4", "model: no record on file holds a full window",
     "IF rec = NoTime THEN NoTime ELSE monoNow - Max0(wallNow - rec)",
     "IF rec = NoTime THEN monoNow ELSE monoNow - Max0(wallNow - rec)",
     "ci-restarts", "FreshNoWait"),
    ("M5", "model: a room's run in the voted mode is not stamped",
     "doStamp  == IF state # mode THEN ~byV ELSE byVotes /\\ runs",
     "doStamp  == IF state # mode THEN ~byV ELSE FALSE",
     "ci-votes", "HoldKept"),
    ("M6", "model: votes count while a room is mid-run",
     "gateOpen == Len(cmd) = 0 /\\ ~engaged",
     "gateOpen == Len(cmd) = 0",
     "ci-votes", "PausedRunKept"),
    ("M7", "model: the hold no longer gates a change",
     "IF proposed # current /\\ allowed /\\ (standoff \\/ ~still)",
     "IF proposed # current /\\ (standoff \\/ ~still)",
     "ci-restarts", "HoldKept"),
    ("M8", "model: Resting mode takes the mode from a run (F1)",
     "rest == IF Resting \\in Modes /\\ ~running THEN Resting ELSE current",
     "rest == IF Resting \\in Modes THEN Resting ELSE current",
     "finding-resting", "PausedRunKept"),
    ("M9", "model: a start forgets that votes chose the mode (F2)",
     "/\\ byVotes' = IF fromRec THEN storedByV ELSE FALSE",
     "/\\ byVotes' = FALSE",
     "finding-reload-vote", "HoldKept"),
    ("M10", "model: a start takes the select's restore data over the record (F3)",
     "LET fromRec == storedMode \\in Modes",
     "LET fromRec == FALSE /\\ storedMode \\in Modes",
     "finding-crash", "HoldKept"),
    ("M11", "model: a paused room still votes",
     "        dem      == [i \\in RoomIds |-> IF paused[i] THEN None ELSE Demand(pos[i])]",
     "        dem      == [i \\in RoomIds |-> Demand(pos[i])]",
     "ci-openings", "PausedNeverVotes"),
    ("M12", "model: a pause restamps the hold",
     "    /\\ stamp' = stamp        \\* a pause is not a mode change",
     "    /\\ stamp' = Mono",
     "ci-openings", "PauseKeepsHold"),
    ("M13", "model: an overridden room pauses again before a close",
     "    /\\ ~paused[i] /\\ ~overridden[i]",
     "    /\\ ~paused[i]",
     "ci-openings", "OverrideNeverPaused"),
    ("M14", "model: a pause turns the room Off",
     "    /\\ enabled' = enabled    \\* nor does it touch the room's own mode",
     "    /\\ enabled' = [enabled EXCEPT ![i] = FALSE]",
     "ci-openings-restarts", "PauseKeepsMode"),
]


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"verify-mxz-formal: set {name} (see the skill's Tools section)")
    return value


def _tools() -> tuple[str, str]:
    jar, java = _env("TLA2TOOLS"), _env("JAVA")
    digest = hashlib.sha256(Path(jar).read_bytes()).hexdigest()
    if digest != TLA2TOOLS_SHA256:
        sys.exit(f"verify-mxz-formal: {jar} sha256 {digest}, want {TLA2TOOLS_SHA256}")
    return java, jar


def tlc(root: Path, config: str, out: Path) -> tuple[str, str]:
    """Run TLC on one config; return (verdict, the rule broken or '')."""
    java, jar = _tools()
    meta = tempfile.mkdtemp(prefix="tlc-meta-")
    try:
        run = subprocess.run(
            [java, "-XX:+UseParallelGC", "-cp", jar, "tlc2.TLC",
             "-workers", os.environ.get("TLC_WORKERS") or "auto",
             "-metadir", meta, "-config", f"{config}.cfg", SPEC],
            cwd=root / MODEL, capture_output=True, text=True, check=False,
        )
    finally:
        shutil.rmtree(meta, ignore_errors=True)
    out.write_text(run.stdout + run.stderr)
    if broken := re.search(r"Invariant (\w+) is violated", run.stdout):
        return "breaks", broken.group(1)
    if "Model checking completed. No error has been found." in run.stdout:
        states = re.search(r"([\d,]+) distinct states found, 0 states left on queue", run.stdout)
        return "passes", states.group(1) if states else ""
    return "error", ""


def pytest(root: Path, args: list[str], out: Path, fakeboot: bool = False) -> int:
    python = _env("MXZ_PYTHON")
    cmd = [python, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider"]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    if fakeboot:
        cmd += ["-p", "tools.fakeboot"]
        env["FAKE_UPTIME"] = "120"
    run = subprocess.run(cmd + args, cwd=root, capture_output=True, text=True, env=env, check=False)
    out.write_text(run.stdout + run.stderr)
    return run.returncode


def _outcomes(log: str) -> dict[str, str]:
    return {m.group(2): m.group(1) for m in re.finditer(r"^(PASSED|FAILED|ERROR) (\S+)", log, re.MULTILINE)}


def check(out: Path) -> int:
    ok = True
    rev = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=False,
    ).stdout.strip()
    print(f"revision {rev}")
    for config, want in CONFIGS.items():
        start = time.monotonic()
        verdict, detail = tlc(ROOT, config, out / f"tlc-{config}.out")
        took = time.monotonic() - start
        good = (verdict, detail) == ("breaks", want) if want else verdict == "passes"
        ok &= good
        shown = f"{verdict} {detail}".strip()
        label = f"finding, must break {want}" if want else "must pass"
        print(f"{'ok ' if good else 'BAD'} tlc {config:28} {shown:30} ({label}, {took:.0f}s)")
    for clock in (False, True):
        name = "fakeboot" if clock else "host"
        code = pytest(ROOT, [PROPS], out / f"props-{name}.log", fakeboot=clock)
        tail = (out / f"props-{name}.log").read_text().strip().splitlines()[-1]
        ok &= code == 0
        print(f"{'ok ' if code == 0 else 'BAD'} props {name:9} {tail}")
        code = pytest(ROOT, [FINDINGS, PROMISES], out / f"findings-{name}.log", fakeboot=clock)
        log = (out / f"findings-{name}.log").read_text()
        seen = _outcomes(log)
        for node, tag in REPLAYS.items():
            finding, control = seen.get(f"{node}[finding]"), seen.get(
                next((k for k in seen if k.startswith(f"{node}[control")), ""))
            good = finding == "PASSED" and control == "PASSED"
            ok &= good
            print(f"{'ok ' if good else 'BAD'} replay {name:8} {tag}: finding {finding}, control {control}"
                  f"{'' if good else '  (a fixed finding is back, or the replay broke)'}")
        ok &= code == 0
        tail = log.strip().splitlines()[-1]
        print(f"{'ok ' if code == 0 else 'BAD'} promises {name:6} {tail}")
        code = pytest(ROOT, [OPENINGS_LOGIC, OPENINGS], out / f"openings-{name}.log", fakeboot=clock)
        tail = (out / f"openings-{name}.log").read_text().strip().splitlines()[-1]
        ok &= code == 0
        print(f"{'ok ' if code == 0 else 'BAD'} openings {name:6} {tail}")
    print(f"evidence {out}")
    print("VERIFY_OK" if ok else "VERIFY_FAILED")
    return 0 if ok else 1


def mutants(out: Path, only: list[str]) -> int:
    """The model's broken copies, each with only its rule, then the code's (the H mutants)."""
    ok = True
    for mid, what, old, new, config, rule in MODEL_MUTANTS:
        if only and mid not in only:
            continue
        scratch = Path(tempfile.mkdtemp(prefix=f"mxz-mutant-{mid}-"))
        try:
            shutil.copytree(ROOT / MODEL, scratch / MODEL)
            target = scratch / TLA
            text = target.read_text()
            if text.count(old) != 1:
                print(f"BAD {mid} the text to break is not in {TLA} exactly once: re-anchor it")
                ok = False
                continue
            target.write_text(text.replace(old, new))
            # Only the rule under test, so TLC's first stop is that rule.
            cfg = scratch / MODEL / f"{config}.cfg"
            head = cfg.read_text().split("INVARIANTS")[0]
            cfg.write_text(f"{head}INVARIANTS\n  {rule}\n")
            verdict, broke = tlc(scratch, config, out / f"mutant-{mid}.log")
            caught = (verdict, broke) == ("breaks", rule)
            print(f"{'caught' if caught else 'MISSED'} {mid:4} {what}: TLC {config}: {verdict} {broke}".rstrip())
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        ok &= caught
    for mid in CODE_MUTANTS:
        if not only or mid in only:
            ok &= code_mutants.run(mid, _env("MXZ_PYTHON"), out)
    print(f"evidence {out}")
    print("MUTANTS_ALL_CAUGHT" if ok else "MUTANTS_MISSED")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(os.environ.get("MXZ_FORMAL_OUT") or Path.home() / "verify-mxz-formal" / stamp)
    out.mkdir(parents=True, exist_ok=True)
    if argv[:1] == ["mutants"]:
        return mutants(out, argv[1:])
    if argv in ([], ["check"]):
        return check(out)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
