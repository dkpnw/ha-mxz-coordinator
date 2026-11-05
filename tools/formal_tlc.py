"""Run TLC on docs/formal/tla/ and prove its rules aren't vacuous. Standard library only.

    tools/formal_tlc.py check      every config passes every rule it lists
    tools/formal_tlc.py cover      every reachability goal has a path (TLC must break it)
    tools/formal_tlc.py mutants    every broken copy of the spec breaks its named rule
    tools/formal_tlc.py code       every broken copy of the code fails its named property
    tools/formal_tlc.py all        check, cover and mutants (what .github/workflows/tla.yml runs)

Set TLA2TOOLS to a tla2tools.jar and, if `java` isn't on PATH, JAVA. A jar whose sha256
isn't TLA2TOOLS_SHA256 is refused. `code` also needs MXZ_PYTHON, a lane venv's python with
hypothesis in it. It breaks a scratch copy of the checkout, never the checkout. The exit
status is 0 only if every case came out as stated. docs/formal/README.md explains the rules.
"""

import argparse
import hashlib
import os
import re
import shutil
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
}

# An open finding's rule, which must keep breaking until the finding is fixed and the
# rule moves into its config's INVARIANTS (docs/formal/PARKED-FINDINGS.md). None is open.
FINDINGS: dict[str, str] = {}

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
}

# id: (what breaks, file under custom_components/mxz_coordinator, exact text once, replacement,
#      property test that must fail). Each is the code-side twin of a rule in the spec.
CODE_MUTANTS = {
    "C1": ("the hold is ignored", "coordinator.py", "allowed = elapsed is None or elapsed >= self.hysteresis\n        engaged",
           "allowed = True\n        engaged", "tests/test_properties_mode.py::test_no_flip_inside_the_hold_and_no_wait_after_it"),
    "C2": ("the hold runs long", "coordinator.py", "allowed = elapsed is None or elapsed >= self.hysteresis\n        engaged",
           "allowed = elapsed is None or elapsed >= self.hysteresis * 2\n        engaged",
           "tests/test_properties_mode.py::test_no_flip_inside_the_hold_and_no_wait_after_it"),
    "C3": ("a flip isn't stamped", "coordinator.py", "if not plan.get(\"mode_by_hands_off\"):\n                self._stamp_mode_change()",
           "if False:\n                self._stamp_mode_change()",
           "tests/test_properties_mode.py::test_no_flip_inside_the_hold_and_no_wait_after_it"),
    "C4": ("a start restarts the hold", "coordinator.py", "ago = max(0.0, dt_util.utcnow().timestamp() - stored)", "ago = 0.0",
           "tests/test_properties_restart.py::test_a_start_changes_nothing_anyone_can_see"),
    "C5": ("arbitrate ignores the gate", "logic.py", "if proposed != current and allowed and (standoff or not still):",
           "if proposed != current and (standoff or not still):",
           "tests/test_properties_mode.py::test_arbitrate_never_changes_the_mode_while_the_hold_holds"),
    "C6": ("a start ignores the record", "coordinator.py", "mode, by_votes = record.mode, record.by_votes\n        else:",
           "mode, by_votes = restored or MODE_COOL, False\n        else:",
           "tests/test_properties_mode.py::test_a_start_resumes_the_recorded_mode"),
    "C7": ("Resting mode doesn't wait", "logic.py", "if running:\n        resting = None", "if False:\n        resting = None",
           "tests/test_properties_mode.py::test_a_run_in_the_shared_mode_keeps_it_from_resting_mode"),
    "C8": ("a departure isn't a hold", "coordinator.py", "if not (seeding or departed):", "if not seeding:",
           "tests/test_properties_fan.py::test_a_pick_is_never_written_over_until_it_is_handed_back"),
    "C9": ("a hold doesn't stop the write", "coordinator.py", "if held:\n            return  # latched: leave the user's fan pick untouched",
           "if False:\n            return  # latched: leave the user's fan pick untouched",
           "tests/test_properties_fan.py::test_a_pick_is_never_written_over_until_it_is_handed_back"),
    "C10": ("a restored hold isn't kept", "coordinator.py", "if held:\n            self._hold_at(climate_id, observed)\n            return True\n        last =",
            "if False:\n            self._hold_at(climate_id, observed)\n            return True\n        last =",
            "tests/test_properties_restart.py::test_a_start_changes_nothing_anyone_can_see"),
    "C11": ("the echo rule widens to the whole ladder", "coordinator.py", "departed = not seeding and observed not in (\n            self._fan_cmd.get(climate_id),\n            self._fan_prev.get(climate_id),\n        )",
            "departed = not seeding and observed not in FAN_LADDER",
            "tests/test_properties_fan.py::test_the_exception_is_exactly_the_last_two"),
    "C12": ("the echo exception is gone", "coordinator.py", "departed = not seeding and observed not in (\n            self._fan_cmd.get(climate_id),\n            self._fan_prev.get(climate_id),\n        )",
            "departed = not seeding and observed != self._fan_cmd.get(climate_id)",
            "tests/test_properties_fan.py::test_the_exception_can_lose_the_pick"),
    "C13": ("a cloud head's hold doesn't stop the write", "coordinator.py", "if held:\n            return None\n        if fan_act in",
            "if False:\n            return None\n        if fan_act in",
            "tests/test_properties_fan.py::test_a_cloud_heads_pick_is_never_written_over"),
    "C14": ("a paused head isn't sent off", "coordinator.py", "task = self._queue_delivery(zone.climate_id, MODE_OFF, 0.0, 0.0)\n                if task is not None:\n                    tasks.append(task)\n                continue",
            "continue",
            "tests/test_properties_window.py::test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes"),
    "C15": ("a paused room still votes", "coordinator.py", "if paused:\n                demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED",
            "if False:\n                demand, engage = DEMAND_NEUTRAL, ENGAGE_SATISFIED",
            "tests/test_properties_window.py::test_a_paused_room_never_keeps_or_turns_the_shared_mode"),
    "C16": ("a person's change isn't a takeover", "coordinator.py", "self._openings[zone.slug][\"override\"] = \"manual\"", "pass",
            "tests/test_properties_window.py::test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes"),
    "C17": ("a closed reading doesn't end an override", "coordinator.py", "if raw == CLOSED_READ and record[\"override\"] is not None:",
            "if False:",
            "tests/test_properties_window.py::test_a_pause_stops_the_head_and_a_takeover_holds_until_the_window_closes"),
    "C18": ("a pause isn't stored", "coordinator.py", "self._openings_store.async_delay_save(lambda: data, 0)", "pass",
            "tests/test_properties_restart.py::test_a_start_changes_nothing_anyone_can_see"),
    "C19": ("opening_state pauses on no reading", "logic.py", "return OPENING_PAUSED if paused else OPENING_CLOSED",
            "return OPENING_PAUSED",
            "tests/test_properties_window.py::test_no_reading_never_starts_a_pause_and_never_ends_one"),
    "C20": ("a target isn't converted on a unit switch", "cloud.py", "if set_in == UNIT_F and unit == UNIT_C:\n        return snap_to_step((value - 32) * 5 / 9, step)",
            "if False:\n        return snap_to_step((value - 32) * 5 / 9, step)",
            "tests/test_properties_restart.py::test_a_target_is_converted_on_a_unit_switch"),
    "C22": ("only the last command is an echo", "coordinator.py", "departed = not seeding and observed not in (\n            self._fan_cmd.get(climate_id),\n            self._fan_prev.get(climate_id),\n        )",
            "departed = not seeding and observed != self._fan_cmd.get(climate_id)",
            "tests/test_properties_fan.py::test_a_slow_heads_own_echo_is_not_a_hold_one_write_behind"),
    "C23": ("the echo exception is gone, on a cloud head", "coordinator.py", "departed = not seeding and observed not in (\n            self._fan_cmd.get(climate_id),\n            self._fan_prev.get(climate_id),\n        )",
            "departed = not seeding and observed != self._fan_cmd.get(climate_id)",
            "tests/test_properties_fan.py::test_the_exception_on_a_cloud_head"),
    "C24": ("a start ignores the time since the change", "coordinator.py", "ago = max(0.0, dt_util.utcnow().timestamp() - stored)", "ago = 0.0",
            "tests/test_properties_mode.py::test_a_start_keeps_what_is_left_of_the_hold_and_never_more"),
    "C25": ("no change on record still holds", "coordinator.py", "if stored is None:\n        return None\n    ago", "if stored is None:\n        return time.monotonic()\n    ago",
            "tests/test_properties_mode.py::test_no_stored_change_holds_nothing"),
    "C26": ("an infinite stored time reads as a time", "coordinator.py", "return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)",
            "return isinstance(value, (int, float)) and not isinstance(value, bool)",
            "tests/test_properties_mode.py::test_any_stored_record_loads_as_a_readable_record"),
    "C27": ("an override can be paused", "logic.py", "if raw == OPEN_READ or paused or prior == OPENING_OVERRIDDEN:\n            return OPENING_OVERRIDDEN",
            "if raw == OPEN_READ or paused or prior == OPENING_OVERRIDDEN:\n            return OPENING_PAUSED",
            "tests/test_properties_window.py::test_an_override_is_never_paused_and_a_closed_reading_ends_it"),
    "C28": ("the open delay is ignored", "logic.py", "return OPENING_PAUSED if now - since >= open_delay else OPENING_OPENING", "return OPENING_PAUSED",
            "tests/test_properties_window.py::test_the_delays_decide_a_pause_and_a_resume"),
    "C29": ("a close delay has no wakeup", "logic.py", "if state == OPENING_CLOSING:\n        return since + close_delay\n    return None", "return None",
            "tests/test_properties_window.py::test_only_a_running_delay_has_a_wakeup"),
    "C30": ("unavailable reads as closed", "logic.py", "if state == STATE_OFF:\n        return CLOSED_READ", "if state in (STATE_OFF, STATE_UNAVAILABLE):\n        return CLOSED_READ",
            "tests/test_properties_window.py::test_only_on_and_off_are_readings"),
    "C21": ("a live unit switch doesn't reload", "coordinator.py", "self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)", "pass",
            "tests/test_properties_restart.py::test_a_live_unit_switch_reloads_with_a_held_fan_and_the_hold_kept"),
    "C31": ("a slow head's older write reads as a pick", "coordinator.py", "if departed and observed in self.fan_older_commands(climate_id):", "if False:",
            "tests/test_properties_fan.py::test_no_person_no_hold_however_far_a_head_lags"),
    "C32": ("older writes still count once the head has caught up", "coordinator.py", "memory = self._fan_older.get(climate_id)\n        if not memory or observed not in memory:", "memory = None\n        if not memory or observed not in memory:",
            "tests/test_properties_fan.py::test_the_exception_is_exactly_the_last_two"),
    "C33": ("the whole ladder reads as an older write", "coordinator.py", "if departed and observed in self.fan_older_commands(climate_id):", "if departed and observed in FAN_LADDER:",
            "tests/test_properties_fan.py::test_a_lagging_heads_pick_of_a_speed_mxz_never_wrote_is_held"),
    "C34": ("any report of the last write reads as caught up, a late one too", "coordinator.py", "        if observed == last and len(memory) == 1:", "        if observed == last:",
            "tests/test_properties_fan.py::test_no_person_no_hold_when_a_head_reports_every_write_in_order"),
    "C35": ("a start forgets where a repeated write came first", "switch.py", "if older := self.coordinator.fan_older_record(self._zone.climate_id):", "if older := self.coordinator.fan_older_commands(self._zone.climate_id):",
            "tests/test_fan_lag_memory.py::test_a_start_keeps_where_a_repeated_write_came_first"),
    "C36": ("a report drops nothing before it", "coordinator.py", "            while memory[0] != observed:\n                memory.popleft()\n", "",
            "tests/test_fan_lag_memory.py::test_a_report_drops_the_writes_before_it_and_no_others"),
    "C37": ("a report of the last write doesn't start the memory afresh", "coordinator.py", "        if observed == last and len(memory) == 1:", "        if False:",
            "tests/test_fan_lag_memory.py::test_the_findings_walk_holds_nothing_and_boost_keeps_driving"),
    "C39": ("a start's record loses a queued write of auto", "coordinator.py", "        while record and record[0] == FAN_AUTO:\n            del record[0]\n", "        record = [token for token in record if token != FAN_AUTO]\n",
            "tests/test_properties_fan.py::test_no_person_no_hold_when_a_head_reports_every_write_in_order"),
    "C38": ("a speed written twice is kept only once", "coordinator.py", "        memory.append(token)\n", "        if token not in memory:\n            memory.append(token)\n",
            "tests/test_fan_lag_memory.py::test_a_later_write_of_a_speed_outlives_a_report_past_its_first"),
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
            [*java(), "-cp", jar(), "tlc2.TLC", "-workers", "auto", "-cleanup", "-metadir", meta,
             "-config", f"{config}.cfg", f"{SPEC}.tla"],
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


def run_code(out: Path, only: list[str]) -> bool:
    python = os.environ.get("MXZ_PYTHON")
    if not python:
        sys.exit("set MXZ_PYTHON to a lane venv's python with hypothesis in it")
    good = True
    for name, (what, filename, old, new, test) in CODE_MUTANTS.items():
        if only and name not in only:
            continue
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "repo"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", ".hypothesis"))
            target = copy / "custom_components/mxz_coordinator" / filename
            text = target.read_text()
            if text.count(old) != 1:
                print(f"FAIL code {name}: its text is not in {filename} exactly once")
                good = False
                continue
            target.write_text(text.replace(old, new))
            done = subprocess.run(
                [python, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--show-capture=no", "--tb=line", test],
                cwd=copy, capture_output=True, text=True, check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        (out / f"code-{name}.out").write_text(done.stdout + done.stderr)
        tail = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
        passed = done.returncode == 1 and "failed" in tail
        good = good and passed
        print(f"{'OK  ' if passed else 'FAIL'} code {name} ({what}): exit {done.returncode}, {tail}", flush=True)
    return good


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=("check", "cover", "mutants", "code", "all"))
    parser.add_argument("only", nargs="*", help="mutant ids, for mutants or code")
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
    if args.what == "code":
        good = run_code(out, args.only) and good
    print("FORMAL_OK" if good else "FORMAL_FAILED", flush=True)
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
