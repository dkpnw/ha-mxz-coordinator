"""Run TLC on docs/formal/tla/ and prove its rules aren't vacuous. Standard library only.

    tools/formal_tlc.py check      every config passes every rule it lists
    tools/formal_tlc.py cover      every reachability goal has a path (TLC must break it)
    tools/formal_tlc.py mutants    every broken copy of the spec breaks its named rule
    tools/formal_tlc.py all        check, cover and mutants (what .github/workflows/tla.yml runs)

Set TLA2TOOLS to a tla2tools.jar and, if `java` isn't on PATH, JAVA. A jar whose sha256
isn't TLA2TOOLS_SHA256 is refused. TLC_WORKERS sets TLC's -workers (default auto). The
exit status is 0 only if every case came out as stated. docs/formal/README.md explains
the rules. The broken copies of the code that are these rules' twins are in
tools/code_mutants.py.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TLA = ROOT / "docs/formal/tla"
SPEC = "MXZCoordinator"
TLA2TOOLS_VERSION = "1.7.4"
TLA2TOOLS_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"

# The configs, each passing every rule in its INVARIANTS line.
CONFIGS = (
    "ci-fan", "ci-fan-crash", "ci-fan-lag", "ci-window", "ci-window-fan",
    "ci-arbitration", "ci-arbitration-rest-heat", "ci-arbitration-window",
    "ci-apply", "ci-adopt", "ci-adopt-restart",
)

# Each goal must break in its config: a path to the behavior exists.
COVERS = {
    "NeverFlips": "ci-arbitration",
    "NeverHoldBlocksACall": "ci-arbitration",
    "NeverStrictHoldAcrossAStart": "ci-fan",
    "NeverKnownExceptionLosesAPick": "ci-fan",
    "NeverPaused": "ci-window",
    "NeverOverrideRunning": "ci-window",
    "NeverPauseRestored": "ci-window",
    "NeverLagEcho": "ci-fan-lag",
    "NeverTwoBehindEcho": "ci-fan-lag",
    "NeverRefused": "ci-apply",
    "NeverCutShort": "ci-apply",
    "NeverAdopts": "ci-adopt",
    "NeverLateOwnWriteInQuiet": "ci-adopt-restart",
}

# An open finding's rule, which must keep breaking until the finding is fixed and the
# rule moves into its config's INVARIANTS (docs/formal/PARKED-FINDINGS.md).
FINDINGS: dict[str, str] = {
    "StartWriteNeverAdopted": "ci-adopt-restart",
    "QueuedWriteNeverAdopted": "ci-adopt",
}

# id: (what breaks, exact text once in the spec, its replacement, config, rule that must break).
MUTANTS = {
    "S1": ("the hold is ignored", "allowed == stamp = NoTime \\/ real - stamp >= H",
           "allowed == TRUE", "ci-arbitration", "NoFlipInDwell"),
    "S2": ("the hold runs a tick long", "allowed == stamp = NoTime \\/ real - stamp >= H",
           "allowed == stamp = NoTime \\/ real - stamp >= H + 1", "ci-arbitration", "NoExtraWait"),
    "S3": ("a change isn't stored", "/\\ storedStamp' = IF changed THEN real ELSE storedStamp",
           "/\\ storedStamp' = storedStamp", "ci-arbitration", "RestartRestores"),
    "S4": ("a start loses the mode", "newMode == IF storedMode = None THEN \"cool\" ELSE storedMode",
           "newMode == \"cool\"", "ci-arbitration", "RestartRestores"),
    "S5": ("a start restarts the hold", "/\\ stamp' = storedStamp", "/\\ stamp' = real",
           "ci-arbitration", "HoldClockKept"),
    "S6": ("a head isn't parked for the mode",
           "ELSE IF nl[r] = state THEN state ELSE \"fan_only\"]",
           "ELSE IF nl[r] \\in Modes THEN nl[r] ELSE \"fan_only\"]", "ci-arbitration", "HeadsFollowMode"),
    "F1": ("the hold doesn't stop the write", "IN  IF ob.held THEN [f |-> g, token |-> None]",
           "IN  IF FALSE THEN [f |-> g, token |-> None]", "ci-fan", "ManualFanKept"),
    "F2": ("a departure isn't read", "ELSE IF o \\notin {f.cmd, f.prev} \\cup f.older\n    THEN [f |-> HoldAt(f, o), held |-> TRUE]",
           "ELSE IF FALSE\n    THEN [f |-> HoldAt(f, o), held |-> TRUE]", "ci-fan", "ManualFanKept"),
    "F3": ("a hold doesn't survive a start", "ELSE IF restoredHeld THEN [f |-> HoldAt(f, o), held |-> TRUE]",
           "ELSE IF FALSE THEN [f |-> HoldAt(f, o), held |-> TRUE]", "ci-fan", "ManualFanKept"),
    "F4": ("a stop writes no restore data", "LET record == IF crash THEN saved ELSE Dumped",
           "LET record == NoDump", "ci-fan", "ManualFanShown"),
    "F5": ("only the last command is an echo", "ELSE IF o \\notin {f.cmd, f.prev} \\cup f.older",
           "ELSE IF o # f.cmd", "ci-fan-lag", "NoFalseHold"),
    "F6": ("a rewrite rolls the memory, with no older writes to fall back on",
           ("IF t = None \\/ t = f.cmd THEN f\n    ELSE [f EXCEPT !.prev = IF f.cmd = None THEN t ELSE f.cmd, !.cmd = t,\n"
            "                   !.older = (f.older \\cup ({f.prev} \\cap RungSet)) \\ {t, f.cmd}]"),
           "IF t = None THEN f\n    ELSE [f EXCEPT !.prev = IF f.cmd = None THEN t ELSE f.cmd, !.cmd = t, !.older = {}]",
           "ci-fan-lag", "NoFalseHold"),
    "F7": ("older writes aren't remembered", "ELSE IF o \\notin {f.cmd, f.prev} \\cup f.older",
           "ELSE IF o \\notin {f.cmd, f.prev}", "ci-fan-lag", "NoFalseHoldTwoBehind"),
    "F8": ("older writes still count once the head has caught up", "/\\ fanOlder' = IF lands THEN {} ELSE rolled.older",
           "/\\ fanOlder' = rolled.older", "ci-fan", "ManualFanKept"),
    "F9": ("a start forgets the older writes", "/\\ fanOlder' = IF carries THEN {t \\in record.older : InCeiling(t)} \\ {fanCmd', fanPrev'} ELSE {}",
           "/\\ fanOlder' = {}", "ci-fan-lag", "NoFalseHoldTwoBehind"),
    "W1": ("a pause leaves the head running", "IF r = 1 /\\ isPaused THEN \"off\"\n                  ELSE IF nl[r]",
           "IF FALSE THEN \"off\"\n                  ELSE IF nl[r]", "ci-window", "PauseStopsHead"),
    "W2": ("a pause writes the fan", "IN [f |-> ob.f, token |-> None]",
           "IN [f |-> ob.f, token |-> IF fanShown = \"auto\" THEN None ELSE \"auto\"]", "ci-window-fan", "PauseStopsHead"),
    "W3": ("a paused room still calls", "dem == [r \\in RoomIds |-> IF r = 1 /\\ isPaused THEN None ELSE Demand(pos[r])]",
           "dem == [r \\in RoomIds |-> Demand(pos[r])]", "ci-window", "PauseStopsHead"),
    "W4": ("a pause isn't stored", "/\\ storedWin' = newWin /\\ storedOverride' = override0",
           "/\\ storedOverride' = override0 /\\ UNCHANGED storedWin", "ci-window", "RestartRestores"),
    "W5": ("an override is forgotten by a start", "/\\ winState' = storedWin /\\ winOverride' = storedOverride",
           "/\\ winState' = storedWin /\\ winOverride' = None", "ci-window", "RestartRestores"),
    "W6": ("a takeover doesn't hold", "THEN IF raw = \"open\" \\/ wasPaused \\/ prior = Overridden THEN Overridden ELSE Closed",
           "THEN Paused", "ci-window", "OverrideHolds"),
    "W7": ("a takeover ends with the window still open", "override0 == IF winRead = \"closed\" THEN None ELSE winOverride",
           "override0 == IF winRead # \"open\" THEN None ELSE winOverride", "ci-window", "OverrideHolds"),
    "P1": ("the mode isn't recorded before the heads are sent it",
           "/\\ storedMode' = IF changed THEN state ELSE storedMode",
           "/\\ storedMode' = IF changed /\\ ~WithSplit THEN state ELSE storedMode", "ci-apply", "RecordLeadsHeads"),
    "P2": ("an apply an input overtook counts as a decision", "/\\ decided' = (left' = {} /\\ fresh)",
           "/\\ decided' = (left' = {})", "ci-apply", "HeadsFollowMode"),
    "A1": ("a setpoint write isn't remembered", "/\\ spMem' = Wrote(spMem, target, spShown)", "/\\ spMem' = spMem",
           "ci-adopt", "OwnWriteNeverAdopted"),
    "A2": ("a hold doesn't stop the write", "/\\ WithAdopt /\\ ~holding /\\ spShown # target",
           "/\\ WithAdopt /\\ spShown # target", "ci-adopt", "HoldBlocksWrite"),
    "A3": ("a reload loses the memory, as a process start does", "Reload == WithReload /\\ Start(FALSE, FALSE, fanShown)",
           "Reload == WithReload /\\ Start(TRUE, FALSE, fanShown)", "ci-adopt", "StartWriteNeverAdopted"),
}

def java() -> list[str]:
    return [os.environ.get("JAVA", "java"), "-XX:+UseParallelGC", "-Xmx4g"]


def jar() -> str:
    path = os.environ.get("TLA2TOOLS")
    if not path:
        sys.exit("set TLA2TOOLS to a tla2tools.jar")
    with open(path, "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != TLA2TOOLS_SHA256:
        sys.exit(f"{path}: sha256 {digest}, expected {TLA2TOOLS_SHA256}")
    return path


def tlc(folder: Path, config: str, out: Path) -> tuple[str, str]:
    """Run one config in ``folder``: ("pass", states), or ("broke", rule), or ("error", text)."""
    with tempfile.TemporaryDirectory() as meta, out.open("w") as log:
        done = subprocess.run(
            [*java(), "-cp", jar(), "tlc2.TLC", "-workers", os.environ.get("TLC_WORKERS") or "auto",
             "-cleanup", "-metadir", meta, "-config", f"{config}.cfg", f"{SPEC}.tla"],
            cwd=folder, stdout=log, stderr=subprocess.STDOUT, check=False,
        )
    text = out.read_text()
    broke = re.search(r"Error: Invariant (\w+) is violated", text)
    if broke:
        return "broke", broke.group(1)
    states = re.search(r"([\d,]+) states generated, ([\d,]+) distinct states found, 0 states left", text)
    if done.returncode == 0 and states and "Model checking completed. No error" in text:
        return "pass", states.group(2)
    return "error", f"exit {done.returncode}; see {out}"


def scratch_spec(root: Path, text: str, configs: list[tuple[str, str]]) -> None:
    (root / f"{SPEC}.tla").write_text(text)
    for name, config in configs:
        (root / f"{name}.cfg").write_text(config)


def only_rule(config: str, rule: str) -> str:
    """The config with its INVARIANTS line replaced by one rule."""
    return re.sub(r"^INVARIANTS .*$", f"INVARIANTS {rule}", config, count=1, flags=re.MULTILINE)


def run_check(out: Path) -> bool:
    good = True
    for config in CONFIGS:
        started = time.monotonic()
        result, detail = tlc(TLA, config, out / f"tlc-{config}.out")
        good = good and result == "pass"
        print(f"{'OK  ' if result == 'pass' else 'FAIL'} check {config}: {result} {detail} "
              f"({time.monotonic() - started:.0f} s)", flush=True)
    return good


def run_cover(out: Path) -> bool:
    """Each goal, and each open finding's rule, must break in its config."""
    good = True
    spec = (TLA / f"{SPEC}.tla").read_text()
    for goal, config in {**COVERS, **FINDINGS}.items():
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scratch_spec(root, spec, [("c", only_rule((TLA / f"{config}.cfg").read_text(), goal))])
            result, detail = tlc(root, "c", out / f"cover-{goal}.out")
        passed = result == "broke" and detail == goal
        good = good and passed
        print(f"{'OK  ' if passed else 'FAIL'} cover {goal} in {config}: {result} {detail}", flush=True)
    return good


def run_mutants(out: Path, only: list[str]) -> bool:
    good = True
    spec = (TLA / f"{SPEC}.tla").read_text()
    for name, (what, old, new, config, rule) in MUTANTS.items():
        if only and name not in only:
            continue
        if spec.count(old) != 1:
            print(f"FAIL mutant {name}: its text is not in the spec exactly once")
            good = False
            continue
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scratch_spec(root, spec.replace(old, new), [("m", only_rule((TLA / f"{config}.cfg").read_text(), rule))])
            result, detail = tlc(root, "m", out / f"mutant-{name}.out")
        passed = result == "broke" and detail == rule
        good = good and passed
        print(f"{'OK  ' if passed else 'FAIL'} mutant {name} ({what}): {result} {detail}, wanted {rule}", flush=True)
    return good


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=("check", "cover", "mutants", "all"))
    parser.add_argument("only", nargs="*", help="mutant ids, for mutants")
    parser.add_argument("--out", type=Path, help="where each run's TLC output is kept (default: a temp folder)")
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix="mxz-formal-"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"output in {out}", flush=True)
    good = True
    if args.what in ("check", "all"):
        good = run_check(out) and good
    if args.what in ("cover", "all"):
        good = run_cover(out) and good
    if args.what in ("mutants", "all"):
        good = run_mutants(out, args.only) and good
    print("FORMAL_OK" if good else "FORMAL_FAILED", flush=True)
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
