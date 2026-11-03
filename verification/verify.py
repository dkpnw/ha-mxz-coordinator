"""Run the mode-hold verification, or prove each check can fail. Stdlib only.

    verification/run            # check: TLC configs, property tests, finding replays
    verification/run mutants    # break the code or the model in a scratch copy, each check must catch it

See .claude/skills/verify-mxz-formal/SKILL.md. Needs MXZ_PYTHON (a lane venv
python), JAVA (Java 11 or later) and TLA2TOOLS (tla2tools.jar v1.7.4, checked
against its sha256 below). Output goes to $MXZ_FORMAL_OUT, by default
~/verify-mxz-formal/<UTC stamp>/, outside the checkout.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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
}
PROPS = "tests/test_modehold_properties.py"
FINDINGS = "tests/test_modehold_findings.py"
PROMISES = "tests/test_hold_promises.py"
# The replays of fixed findings: the finding case and its control must pass.
REPLAYS = {
    f"{FINDINGS}::test_f1_resting_mode_takes_the_mode_from_a_room_still_running": "F1",
    f"{FINDINGS}::test_f2_a_reload_forgets_that_a_room_now_runs_in_the_voted_mode": "F2",
    f"{FINDINGS}::test_f3_a_crash_soon_after_a_change_restores_the_old_mode_inside_the_hold": "F3",
}

LOGIC = "custom_components/mxz_coordinator/logic.py"
COORD = "custom_components/mxz_coordinator/coordinator.py"
SELECT = "custom_components/mxz_coordinator/select.py"
TLA = f"{MODEL}/{SPEC}"


def _prop(name: str, table: str = "ROOMS3") -> str:
    return f"{PROPS}::test_arbitration_property[{name}-{table}]"


# (id, what breaks, file, old text, new text, how it must show)
# A check is ("pytest", node id) that must fail, or ("tlc", config, rule)
# that must break with that rule.
MUTANTS = [
    ("C1", "the hold no longer gates a change (logic.shared_mode)", LOGIC,
     "if proposed != current and allowed and (standoff or not still):",
     "if proposed != current and (standoff or not still):",
     ("pytest", _prop("hold_blocks_every_change"))),
    ("C2", "votes count while a room is mid-run (logic.arbitrate gate)", LOGIC,
     "gate_open = commanding_silent and not engaged",
     "gate_open = commanding_silent",
     ("pytest", _prop("vote_never_beats_a_call_or_a_run"))),
    ("C3", "Comfort priority crosses tiers (logic._rank)", LOGIC,
     "return (demand.priority[0], -comfort_boost(demand), demand.priority[1])",
     "return (-comfort_boost(demand), demand.priority[0], demand.priority[1])",
     ("pytest", _prop("highest_rank_decides"))),
    ("C4", "a vote change is exempt with the gate shut (logic.arbitrate)", LOGIC,
     "read_only = full != plain and gate_open",
     "read_only = full != plain",
     ("pytest", _prop("exempt_only_with_the_gate_open"))),
    ("C5", "by_safety ignores the direction (logic.arbitrate)", LOGIC,
     "d.direction == full and d.priority[0] <= PRIORITY_SAFETY for d in active",
     "d.priority[0] <= PRIORITY_SAFETY for d in active",
     ("pytest", _prop("by_safety_means_a_safety_want_agrees"))),
    ("C6", "Resting mode ignored (logic.shared_mode)", LOGIC,
     "rest = resting if resting in (MODE_COOL, MODE_HEAT) else current",
     "rest = current",
     ("pytest", _prop("nothing_calling_rests"))),
    ("C7", "a stored time in the future holds longer (coordinator.mode_change_stamp)", COORD,
     "ago = max(0.0, dt_util.utcnow().timestamp() - stored)",
     "ago = dt_util.utcnow().timestamp() - stored",
     ("pytest", f"{PROPS}::test_a_start_keeps_what_is_left_of_the_hold_and_never_more")),
    ("C8", "an unreadable record holds nothing (coordinator.async_load_mode_record)", COORD,
     "    return ModeRecord(changed_at=dt_util.utcnow().timestamp(), **shared)",
     "    return ModeRecord(changed_at=None, **shared)",
     ("pytest", f"{PROPS}::test_a_start_reads_the_record_or_holds_the_full_window")),
    ("C9", "a room's run in the voted mode is not stamped (coordinator._apply)", COORD,
     ("            self._mode_by_votes = False\n            self._stamp_mode_change()\n"
      "            self._record_mode()\n\n    def _queue_delivery"),
     "            self._mode_by_votes = False\n            self._record_mode()\n\n    def _queue_delivery",
     ("pytest", (f"{FINDINGS}::test_f2_a_reload_forgets_that_a_room_now_runs_in_the_voted_mode"
                 "[control_no_reload]"))),
    ("C10", "Resting mode always heat (logic.shared_mode)", LOGIC,
     "rest = resting if resting in (MODE_COOL, MODE_HEAT) else current",
     "rest = MODE_HEAT",
     ("pytest", (f"{FINDINGS}::test_f1_resting_mode_takes_the_mode_from_a_room_still_running"
                 "[control_last_used]"))),
    ("C11", "the select does not restore the mode (select.async_added_to_hass)", SELECT,
     ("                if last.state in _OPTIONS:\n"
      "                    restored = last.state"),
     "                if last.state in _OPTIONS:\n                    pass",
     ("pytest", (f"{PROMISES}::test_a_start_takes_the_newer_of_the_record_and_the_restore_data"
                 "[record_without_a_mode]"))),
    ("C12", "Resting mode takes the mode from a run (logic.arbitrate)", LOGIC,
     "    if running:\n        resting = None",
     "    if False:\n        resting = None",
     ("pytest", f"{FINDINGS}::test_f1_resting_mode_takes_the_mode_from_a_room_still_running[finding]")),
    ("C13", "the plan never says a room runs in the shared mode (coordinator._compute)", COORD,
     "        running = current in held_engages\n",
     "        running = False\n",
     ("pytest", f"{PROMISES}::test_resting_mode_waits_for_the_run_then_rests")),
    ("C14", "a start forgets that votes chose the mode (coordinator.restore_shared_mode)", COORD,
     "            mode, by_votes = record.mode, record.by_votes\n",
     "            mode, by_votes = record.mode, False\n",
     ("pytest", f"{PROMISES}::test_a_restart_keeps_that_votes_chose_the_mode[2]")),
    ("C15", "a change votes made is not written to the record (coordinator._apply)", COORD,
     ("            self.async_update_listeners()  # let the shared-mode select re-render\n"
      "            self._record_mode()\n"),
     "            self.async_update_listeners()  # let the shared-mode select re-render\n",
     ("pytest", f"{FINDINGS}::test_f2_a_reload_forgets_that_a_room_now_runs_in_the_voted_mode[finding]")),
    ("C16", "the select's restore data always beats the record (coordinator.restore_shared_mode)", COORD,
     "            or record.mode_at >= restored_at\n",
     "            or False\n",
     ("pytest", (f"{FINDINGS}::test_f3_a_crash_soon_after_a_change_restores_the_old_mode_inside_the_hold"
                 "[finding]"))),
    ("C17", "the record always beats newer restore data (coordinator.restore_shared_mode)", COORD,
     "            or record.mode_at >= restored_at\n",
     "            or True\n",
     ("pytest", (f"{PROMISES}::test_a_start_takes_the_newer_of_the_record_and_the_restore_data"
                 "[restore_data_newer]"))),
    ("M1", "model: the gate reads the wall clock", TLA,
     "Mono == real - boot", "Mono == real + skew",
     ("tlc", "ci-restarts", "HoldKept")),
    ("M2", "model: no clamp for a stored time in the future", TLA,
     "IF rec = NoTime THEN NoTime ELSE monoNow - Max0(wallNow - rec)",
     "IF rec = NoTime THEN NoTime ELSE monoNow - (wallNow - rec)",
     ("tlc", "ci-restarts", "NeverLonger")),
    ("M3", "model: a reload or restart starts a full hold (#64's bug)", TLA,
     "IF rec = NoTime THEN NoTime ELSE monoNow - Max0(wallNow - rec)",
     "IF rec = NoTime THEN NoTime ELSE monoNow",
     ("tlc", "ci-restarts", "NoExtraWait")),
    ("M4", "model: no record on file holds a full window", TLA,
     "IF rec = NoTime THEN NoTime ELSE monoNow - Max0(wallNow - rec)",
     "IF rec = NoTime THEN monoNow ELSE monoNow - Max0(wallNow - rec)",
     ("tlc", "ci-restarts", "FreshNoWait")),
    ("M5", "model: a room's run in the voted mode is not stamped", TLA,
     "doStamp  == IF state # mode THEN ~byV ELSE byVotes /\\ runs",
     "doStamp  == IF state # mode THEN ~byV ELSE FALSE",
     ("tlc", "ci-votes", "HoldKept")),
    ("M6", "model: votes count while a room is mid-run", TLA,
     "gateOpen == Len(cmd) = 0 /\\ ~engaged",
     "gateOpen == Len(cmd) = 0",
     ("tlc", "ci-votes", "PausedRunKept")),
    ("M7", "model: the hold no longer gates a change", TLA,
     "IF proposed # current /\\ allowed /\\ (standoff \\/ ~still)",
     "IF proposed # current /\\ (standoff \\/ ~still)",
     ("tlc", "ci-restarts", "HoldKept")),
    ("M8", "model: Resting mode takes the mode from a run (F1)", TLA,
     "rest == IF Resting \\in Modes /\\ ~running THEN Resting ELSE current",
     "rest == IF Resting \\in Modes THEN Resting ELSE current",
     ("tlc", "finding-resting", "PausedRunKept")),
    ("M9", "model: a start forgets that votes chose the mode (F2)", TLA,
     "/\\ byVotes' = IF fromRec THEN storedByV ELSE FALSE",
     "/\\ byVotes' = FALSE",
     ("tlc", "finding-reload-vote", "HoldKept")),
    ("M10", "model: a start takes the select's restore data over the record (F3)", TLA,
     "LET fromRec == storedMode \\in Modes",
     "LET fromRec == FALSE /\\ storedMode \\in Modes",
     ("tlc", "finding-crash", "HoldKept")),
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
            [java, "-XX:+UseParallelGC", "-cp", jar, "tlc2.TLC", "-workers", "auto",
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
    print(f"evidence {out}")
    print("VERIFY_OK" if ok else "VERIFY_FAILED")
    return 0 if ok else 1


def mutants(out: Path, only: list[str]) -> int:
    ok = True
    for mid, what, path, old, new, how in MUTANTS:
        if only and mid not in only:
            continue
        scratch = Path(tempfile.mkdtemp(prefix=f"mxz-mutant-{mid}-"))
        try:
            for part in ("custom_components", "tests", "tools", "verification", "pytest.ini"):
                src = ROOT / part
                (shutil.copytree if src.is_dir() else shutil.copy2)(src, scratch / part)
            target = scratch / path
            text = target.read_text()
            if text.count(old) != 1:
                print(f"BAD {mid} the text to break is not in {path} exactly once: re-anchor it")
                ok = False
                continue
            target.write_text(text.replace(old, new))
            log = out / f"mutant-{mid}.log"
            if how[0] == "tlc":
                # Only the rule under test, so TLC's first stop is that rule.
                cfg = scratch / MODEL / f"{how[1]}.cfg"
                head = cfg.read_text().split("INVARIANTS")[0]
                cfg.write_text(f"{head}INVARIANTS\n  {how[2]}\n")
                verdict, rule = tlc(scratch, how[1], log)
                caught = (verdict, rule) == ("breaks", how[2])
                shown = f"TLC {how[1]}: {verdict} {rule}".strip()
            else:
                code = pytest(scratch, [how[1]], log)
                caught = code != 0 and "FAILED" in log.read_text()
                shown = f"pytest {'FAILED' if code else 'passed'} {how[1].split('::')[-1]}"
            ok &= caught
            print(f"{'caught' if caught else 'MISSED'} {mid:4} {what}: {shown}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
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
