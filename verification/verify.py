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

# Every config must pass (None) or break exactly one named rule (a finding).
CONFIGS = {
    "ci-restarts": None,
    "ci-restarts-rest-cool": None,
    "ci-restarts-rest-heat": None,
    "ci-votes": None,
    "ci-votes-rest-heat": None,
    "finding-resting": "PausedRunKept",
    "finding-reload-vote": "HoldKept",
    "finding-crash": "HoldKept",
}
PROPS = "tests/test_modehold_properties.py"
FINDINGS = "verification/findings/test_findings.py"
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
    ("C8", "an unreadable record holds nothing (coordinator.async_load_last_mode_change)", COORD,
     "    return dt_util.utcnow().timestamp()\n\n\nasync def async_remove_last_mode_change",
     "    return None\n\n\nasync def async_remove_last_mode_change",
     ("pytest", f"{PROPS}::test_a_start_reads_the_record_or_holds_the_full_window")),
    ("C9", "a room's run in the voted mode is not stamped (coordinator._apply)", COORD,
     ("            self._mode_by_votes = False\n            self._stamp_mode_change()\n\n"
      "    def _queue_delivery"),
     "            self._mode_by_votes = False\n\n    def _queue_delivery",
     ("pytest", (f"{FINDINGS}::test_f2_a_reload_forgets_that_a_room_now_runs_in_the_voted_mode"
                 "[control_no_reload]"))),
    ("C10", "Resting mode always heat (logic.shared_mode)", LOGIC,
     "rest = resting if resting in (MODE_COOL, MODE_HEAT) else current",
     "rest = MODE_HEAT",
     ("pytest", (f"{FINDINGS}::test_f1_resting_mode_takes_the_mode_from_a_room_still_running"
                 "[control_last_used]"))),
    ("C11", "the select does not restore the mode (select.async_added_to_hass)", SELECT,
     ("                if last.state in _OPTIONS:\n"
      "                    self._attr_current_option = last.state"),
     "                if last.state in _OPTIONS:\n                    pass",
     ("pytest", (f"{FINDINGS}::test_f3_a_crash_soon_after_a_change_restores_the_old_mode_inside_the_hold"
                 "[control_clean_stop]"))),
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
        states = re.search(r"([\d,]+) distinct states found", run.stdout)
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
        print(f"{'ok ' if good else 'BAD'} tlc {config:24} {shown:30} ({label}, {took:.0f}s)")
    for clock in (False, True):
        name = "fakeboot" if clock else "host"
        code = pytest(ROOT, [PROPS], out / f"props-{name}.log", fakeboot=clock)
        tail = (out / f"props-{name}.log").read_text().strip().splitlines()[-1]
        ok &= code == 0
        print(f"{'ok ' if code == 0 else 'BAD'} props {name:9} {tail}")
        code = pytest(ROOT, [FINDINGS], out / f"findings-{name}.log", fakeboot=clock)
        seen = _outcomes((out / f"findings-{name}.log").read_text())
        for node, tag in REPLAYS.items():
            finding, control = seen.get(f"{node}[finding]"), seen.get(
                next((k for k in seen if k.startswith(f"{node}[control")), ""))
            good = finding == "FAILED" and control == "PASSED"
            ok &= good
            print(f"{'ok ' if good else 'BAD'} replay {name:8} {tag}: finding {finding}, control {control}"
                  f"{'' if good else '  (a finding that passes: the code changed, re-check it)'}")
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
