"""Account for hosted CI evidence; never install, fetch, repair, or run tests.

CLI: plan LOCK SELECTOR; begin LOCK SELECTOR; bootstrap; capture NAME;
     environment; phases NAME; finish STATUS; extract LOG OUTPUT;
     verify RECORD EXPECTED RECEIPT; auxiliary-begin FAMILY; auxiliary FAMILY STATUS.
MXZ_EVIDENCE is an owned job scratch directory outside the checkout. Pytest loads
this module with -p and MXZ_PHASE=sentinel|suite. JSON evidence is version 1.

A workflow success is NOT admission. verify requires an independently frozen
plan and a driver receipt from complete public platform logs/settings. Missing
facts reject as UNKNOWN. No receipt, action SHA, or provider setting is inferred
from a record-shaped control or an in-job digest. Platform/action log ceilings
must also be checked after retrieval; a cancelled job may have no final marker.
"""
from __future__ import annotations

import ast
import base64
import asyncio
from collections import Counter
import hashlib
import importlib.metadata as metadata
import itertools
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys

SCHEMA = 1
MIB = 1024 * 1024
SENTINEL = "tests/test_changeover.py::test_changeover_from_temperature_sensor"
ACTIONS = ["actions/checkout@v4", "actions/setup-python@v5"]
LOCKS = {
    "3.12.14": ("constraints-py312-ha2024.12.0.txt", "0d0031ddca8bb870560ca4d4bcafd34233e23e9dd6f0470dc31f7bd888eb7403"),
    "3.13": ("constraints-py313-ha2026.2.3.txt", "bac319038f592319b79da98347c6e2fcd6c3630be47cfa3272959c2954d568c6"),
    "3.14": ("constraints-py314-ha2026.9.0.txt", "1609b598b44434b633b0d29b000c7650f9abd0058337821764a5a36b53ff8600"),
}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def known(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value) and not value.upper().startswith("UNKNOWN")
    if isinstance(value, dict):
        return bool(value) and all(known(v) for v in value.values())
    if isinstance(value, list):
        return bool(value) and all(known(v) for v in value)
    return True


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def decode(data):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "duplicate JSON key")
            value[key] = item
        return value

    def invalid_constant(value):
        raise ValueError("nonfinite JSON number: " + value)

    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)


def read(path):
    return decode(Path(path).read_text())


def write(path, value):
    Path(path).write_bytes(encoded(value) + b"\n")


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def distribution_map(rows):
    require(bool(rows), "empty inventory")
    result = {}
    for name, version in rows:
        key = normalized(name)
        require(bool(key) and isinstance(version, str) and bool(version), "invalid distribution")
        require(key not in result, "duplicate normalized distribution: " + key)
        result[key] = version
    return result


def lock_map(path):
    rows = []
    for line in Path(path).read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", line)), "non-exact lock row")
            rows.append(line.split("=="))
    return distribution_map(rows)


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def snapshot():
    files = {}
    for row in git("ls-files", "--stage").splitlines():
        entry, name = row.split("\t")
        mode, _, stage = entry.split()
        path = Path(name)
        require(stage == "0" and path.is_file() and not path.is_symlink(), "source file missing or unsupported")
        actual_mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
        files[name] = {"mode": actual_mode, "sha256": digest(path.read_bytes())}
        require(mode == actual_mode, "source mode changed: " + name)
    require(bool(files), "empty source")
    # No ignored caches are silently excluded. Scratch lives outside this tree.
    extra = git("ls-files", "--others").splitlines()
    require(not extra, "unaccounted checkout files: " + repr(extra))
    return files


def source_tests(root=Path("tests")):
    """Independent source multiplicities, without importing test/HA code.

    Collected parameter IDs are preserved verbatim and checked against these
    per-function cardinalities. Only the source forms used here are supported;
    a new dynamic generator is UNKNOWN until independently reviewed.
    """
    result = {}
    for path in sorted(root.glob("test_*.py")):
        tree = ast.parse(path.read_text())
        assignments = {n.targets[0].id: n.value for n in tree.body
                       if isinstance(n, ast.Assign) and len(n.targets) == 1
                       and isinstance(n.targets[0], ast.Name)}

        def size(node):
            if isinstance(node, (ast.List, ast.Tuple)):
                require(not any(isinstance(x, ast.Starred) for x in node.elts), "dynamic parameters")
                return len(node.elts)
            if isinstance(node, ast.Dict):
                require(None not in node.keys, "dynamic parameters")
                return len(node.keys)
            if isinstance(node, ast.Name) and node.id in assignments:
                return size(assignments[node.id])
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "tuple" and len(node.args) == 1):
                return size(node.args[0])
            raise ValueError("unreviewed parameter source: " + str(path))

        for node in tree.body:
            require(not (isinstance(node, ast.ClassDef) and node.name.startswith("Test")), "unreviewed test class")
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            require(node.name != "pytest_generate_tests", "dynamic test generation")
            if not node.name.startswith("test_"):
                continue
            dimensions = []
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr == "parametrize"):
                    require(len(decorator.args) >= 2, "unknown parameters")
                    names = ast.literal_eval(decorator.args[0])
                    names = [part.strip() for part in names.split(",")] if isinstance(names, str) else names
                    count = size(decorator.args[1])
                    require(count > 0, "empty parameter set")
                    dimensions.append([{name: index for name in names} for index in range(count)])
            result[path.as_posix() + "::" + node.name] = [
                {key: value for part in combo for key, value in part.items()}
                for combo in itertools.product(*dimensions)]
    require(bool(result), "empty source test inventory")
    return result


def plan(lock, selector):
    filename, expected_hash = LOCKS[selector]
    require(Path(lock).name == filename and digest(Path(lock).read_bytes()) == expected_hash, "wrong lock")
    files = snapshot()
    return {"schema": SCHEMA, "commit": git("rev-parse", "HEAD"),
            "tree": git("rev-parse", "HEAD^{tree}"), "files": files,
            "lock": lock, "lock_sha256": expected_hash, "selector": selector,
            "expected_distributions": lock_map(lock), "source_tests": source_tests(),
            "actions": ACTIONS, "runner": "ubuntu-24.04", "arch": "X64",
            "commands": {"sentinel": SENTINEL, "suite": "tests/"},
            "deadlines": {"sentinel": 60, "suite": 180}, "job_minutes": 25}


def inventory(paths=None):
    distributions = metadata.distributions() if paths is None else metadata.distributions(path=paths)
    return sorted([[d.metadata["Name"], d.version] for d in distributions])


def installer():
    dist = metadata.distribution("pip")
    entry = Path(dist.locate_file("pip/__main__.py"))
    return {"version": dist.version, "entrypoint": str(entry),
            "sha256": digest(entry.read_bytes()),
            "metadata_sha256": digest(dist.read_text("METADATA").encode())}


def runtime():
    executable = Path(sys.executable).resolve()
    return {"micro": platform.python_version(), "build": list(platform.python_build()),
            "implementation": platform.python_implementation(), "executable": str(executable),
            "sha256": digest(executable.read_bytes()), "prefix": sys.prefix,
            "base_prefix": sys.base_prefix}


def check_environment(record, expected):
    rt = record["runtime"]
    selector = expected["selector"]
    require(rt["micro"] == selector if selector == "3.12.14" else
            rt["micro"].startswith(selector + "."), "wrong Python lane")
    require(known(rt), "unknown runtime identity")
    require(rt["implementation"] == "CPython" and all(rt["build"])
            and re.fullmatch(r"[0-9a-f]{64}", rt["sha256"])
            and rt["executable"] and rt["prefix"] != rt["base_prefix"], "missing Python build/venv identity")
    installed = distribution_map(record["inventory"])
    bootstrap = distribution_map(record["bootstrap"])
    require(known(bootstrap), "unknown bootstrap provenance")
    require(set(bootstrap) == {"pip"}, "unaccounted bootstrap extra")
    require("pip" in installed and known(installed["pip"]), "missing pip provenance")
    pip_version = installed.pop("pip")
    for name, version in [("bootstrap_installer", bootstrap["pip"]), ("final_pip", pip_version)]:
        info = record[name]
        require(info["version"] == version and known(info["entrypoint"])
                and re.fullmatch(r"[0-9a-f]{64}", info["sha256"])
                and re.fullmatch(r"[0-9a-f]{64}", info["metadata_sha256"]), "missing installer provenance")
    require(record["bootstrap_runtime"] == rt, "runtime changed during install")
    require(installed == expected["expected_distributions"], "locked inventory mismatch")
    require(record["install_exit"] == 0 and record["pip_check_exit"] == 0, "install or pip check failed")
    require(record["install_report"]["version"] == "1", "missing install report")
    downloads = record["install_report"]["install"]
    requested = distribution_map([[x["metadata"]["name"], x["metadata"]["version"]] for x in downloads])
    require(requested == expected["expected_distributions"], "install provenance inventory mismatch")
    for item in downloads:
        info = item["download_info"]
        require(info["url"].startswith("https://files.pythonhosted.org/")
                and re.fullmatch(r"[0-9a-f]{64}", info["archive_info"]["hashes"]["sha256"]), "missing public source/download hash")
    build = record["build_provenance"]
    require(build["complete"] is True, "incomplete build provenance")
    sources = {normalized(x["metadata"]["name"]) for x in downloads
               if not x["download_info"]["url"].split("?", 1)[0].endswith(".whl")}
    if sources:
        require(bool(build["isolated_tools"]) and all(build["isolated_tools"].values()), "missing isolated build tools")
        for rows in build["isolated_tools"].values():
            distribution_map(rows)
        require(sources <= {normalized(row[0]) for row in build["wheels"]}, "missing built wheel")
        require(all(len(row) == 4 and row[1].endswith(".whl") and int(row[2]) > 0
                    and re.fullmatch(r"[0-9a-f]{64}", row[3]) for row in build["wheels"]), "missing wheel provenance")
    require(record["build_provenance"]["log_sha256"] == record["install_log_sha256"], "build log mismatch")


def check_phases(record, expected, phase):
    require(record["phase"] == phase and record["exit"] == 0, "missing/failed outer exit")
    require(record["deadline"] == expected["deadlines"][phase] and
            0 <= record["elapsed"] < record["deadline"], "timeout")
    events = record["events"]
    require(len(events) >= 3, "empty phase ledger")
    require([e["seq"] for e in events] == list(range(len(events))), "truncated/reordered ledger")
    require(all(e["barrier"] == record["barrier"] for e in events) and bool(record["barrier"]), "stale handler barrier")
    require(events[0]["kind"] == "collection" and events[-1]["kind"] == "complete"
            and events[-1]["exit"] == 0, "incomplete phases")
    ids = events[0]["nodeids"]
    require(bool(ids) and len(ids) == len(set(ids)), "empty/duplicate collection")
    counts = Counter(node.split("[", 1)[0] for node in ids)
    wanted = {SENTINEL: [{}]} if phase == "sentinel" else expected["source_tests"]
    require(counts == {key: len(value) for key, value in wanted.items()}, "source/collection inventory mismatch")
    cases = events[0]["cases"]
    require(set(cases) == set(ids), "missing parameter identities")
    actual_cases = Counter((node.split("[", 1)[0], encoded(cases[node])) for node in ids)
    expected_cases = Counter((node, encoded(case)) for node, values in wanted.items() for case in values)
    require(actual_cases == expected_cases, "source parameter inventory mismatch")
    phases = {node: [] for node in ids}
    cleanup = {}
    for event in events[1:-1]:
        if event["kind"] == "cleanup":
            require(event["nodeid"] not in cleanup, "duplicate cleanup")
            cleanup[event["nodeid"]] = event
            require(event["pending_tasks"] == 0 and event["pending_timers"] == 0
                    and event["child_pids"] == [] and event["observed"] is True, "fixture cleanup incomplete")
        else:
            require(event["kind"] == "report" and event["nodeid"] in phases, "collection error/deselection/unknown outcome")
            require(event["outcome"] == "passed" and event["xfail"] is False, "skip/error/xfail/xpass")
            phases[event["nodeid"]].append(event["when"])
    require(all(value == ["setup", "call", "teardown"] for value in phases.values()), "missing/duplicate test phase")
    require(set(cleanup) == set(ids), "missing cleanup")
    require(record["counts"] == {"passed": len(ids), "error": 0, "skip": 0,
                                "xfail": 0, "xpass": 0, "deselected": 0}, "nonordinary result counts")


def verify(record, expected, receipt):
    """Pure validator. Receipt is external evidence, not a provider lookup."""
    require(record["schema"] == SCHEMA and record["complete"] is True, "incomplete record")
    require(record["plan"] == expected, "source/lock/helper/fixture/workflow identity mismatch")
    require(record["before"] == expected["files"] == record["after"], "source immutability mismatch")
    context = record["context"]
    require(context["sha"] == expected["commit"] and context["workflow_sha"] == expected["commit"], "wrong workflow/checkout SHA")
    require(context["attempt"] == "1" and context["event"] == "push"
            and context["ref"] == "refs/heads/ci/locked-runtime-evidence", "wrong run/ref/attempt")
    require(context["run_id"].isdigit() and receipt["job_url"].startswith(
        "https://github.com/dkpnw/ha-mxz-coordinator/actions/runs/" + context["run_id"] + "/job/"), "missing job identity")
    require(record["checkout_credentials_persisted"] is False, "persistent checkout credentials")
    require(known(record["runner"]), "unknown runner identity")
    require(record["runner"]["os"] == "Linux" and record["runner"]["arch"] == "X64"
            and record["runner"]["image_os"] == "ubuntu24" and record["runner"]["image_version"], "missing/wrong runner image")
    require(receipt["context"] == context and receipt["runner"] == record["runner"], "missing runner receipt")
    require(receipt["permissions"] == {"contents": "read"}, "missing/broader token permission receipt")
    require(receipt["public"] is True and receipt["standard_free_runner"] is True, "unknown public/free boundary")
    for key in ("private_connections", "injected_credentials", "extra_triggers", "cache_saves", "artifact_uploads", "paid_api"):
        require(receipt[key] is False, "unsafe/unknown settings: " + key)
    require(receipt["checkout_credentials_persisted"] is False, "persistent checkout receipt")
    require(receipt["static_review_commit"] == expected["commit"], "missing exact static review")
    require(receipt["actions"].keys() == set(expected["actions"]), "missing resolved actions")
    for value in receipt["actions"].values():
        require(bool(re.fullmatch(r"[0-9a-f]{40}", value)), "unknown action identity")
    require(receipt["jobs"] == 7 and receipt["job_minutes"] == [25, 25, 25, 10, 10, 10, 10]
            and 0 <= receipt["observation_minutes"] <= 90, "cost/time envelope")
    require(len(receipt["log_bytes"]) == 7 and all(type(n) is int and 0 < n <= cap * MIB
            for n, cap in zip(receipt["log_bytes"], [16, 16, 16, 2, 2, 2, 2]))
            and sum(receipt["log_bytes"]) <= 56 * MIB, "log budget/truncation")
    require(receipt["all_completion_markers"] is True and receipt["logs_untruncated"] is True,
            "missing/truncated platform logs")
    require(len(receipt["log_sha256"]) == 7 and all(re.fullmatch(r"[0-9a-f]{64}", h)
            for h in receipt["log_sha256"]), "missing retained log digests")
    require(re.fullmatch(r"[0-9a-f]{64}", receipt["settings_sha256"])
            and known(receipt["settings_observed_at"]), "missing current settings provenance")
    validations = {"hacs/action@main", "home-assistant/actions/hassfest@master"}
    require(set(receipt["validation_actions"]) == validations and
            all(re.fullmatch(r"[0-9a-f]{40}", h) for h in receipt["validation_actions"].values()), "missing live validation action identities")
    require(set(receipt["container_images"]) == validations and
            all(re.fullmatch(r"sha256:[0-9a-f]{64}", h) for h in receipt["container_images"].values()), "missing actual validation images")
    require(receipt["record_sha256"] == digest(encoded(record)), "retained record digest mismatch")
    require(record["cleanup"] == {"owned_scratch_removed": True, "vm_disposal": "platform-owned"}, "missing cleanup result")
    check_environment(record["environment"], expected)
    lane = "py" + ".".join(expected["selector"].split(".")[:2]) + "-ha" + expected["expected_distributions"]["homeassistant"]
    for phase in ("sentinel", "suite"):
        require(record[phase]["barrier"] == context["run_id"] + ":1:" + lane + ":" + phase, "stale run barrier")
        check_phases(record[phase], expected, phase)


def scratch():
    path = Path(os.environ["MXZ_EVIDENCE"]).resolve()
    require(Path.cwd().resolve() not in [path, *path.parents], "scratch must be outside source")
    return path


def capture(name):
    """Bound one stdout stream without running its producer; overflow is retained."""
    path = scratch() / (name + ".log")
    total = 0
    hasher = hashlib.sha256()
    with path.open("wb") as stream:
        while chunk := sys.stdin.buffer.read(65536):
            hasher.update(chunk)
            if total < 6 * MIB:
                stream.write(chunk[:6 * MIB - total])
            total += len(chunk)
    write(path.with_suffix(".capture.json"), {"bytes": total, "sha256": hasher.hexdigest(), "truncated": total > 6 * MIB})
    return int(total > 6 * MIB)


def environment_record():
    root = scratch()
    log = (root / "install.log").read_bytes()
    report = read(root / "install.json")
    build_envs = {}
    for site in sorted((root / "tmp").glob("pip-build-env-*/**/site-packages")):
        build_envs[str(site.relative_to(root))] = inventory([str(site)])
    sources = [x for x in report["install"] if not x["download_info"]["url"].split("?", 1)[0].endswith(".whl")]
    wheels = re.findall(r"Created wheel for ([^:]+): filename=(\S+) size=(\d+) sha256=([0-9a-f]{64})", log.decode(errors="replace"))
    built = {normalized(x[0]) for x in wheels}
    complete = all(normalized(x["metadata"]["name"]) in built for x in sources)
    if sources:
        complete = complete and bool(build_envs) and all(build_envs.values())
    bootstrap = read(root / "bootstrap.json")
    return {"runtime": runtime(), "inventory": inventory(), "bootstrap": bootstrap["inventory"],
            "bootstrap_runtime": bootstrap["runtime"], "bootstrap_installer": bootstrap["installer"], "final_pip": installer(),
            "install_exit": int((root / "install.exit").read_text()),
            "pip_check_exit": int((root / "pip-check.exit").read_text()),
            "install_report": report, "install_log_sha256": digest(log),
            "build_provenance": {"complete": complete, "log_sha256": digest(log),
                                 "isolated_tools": build_envs, "wheels": wheels,
                                 "limit": "Observed pip log and retained isolation metadata; not build purity or reproducibility."}}


def phase_record(name):
    root = scratch()
    events = [decode(line) for line in (root / (name + ".jsonl")).read_text().splitlines()]
    reports = [e for e in events if e["kind"] == "report"]
    return {"phase": name, "barrier": os.environ["MXZ_BARRIER"] + ":" + name,
            "exit": int((root / (name + ".exit")).read_text()),
            "elapsed": float((root / (name + ".seconds")).read_text()),
            "deadline": 60 if name == "sentinel" else 180, "events": events,
            "counts": {"passed": sum(e["when"] == "call" and e["outcome"] == "passed" and not e["xfail"] for e in reports),
                       "error": sum(e["outcome"] == "failed" and not e["xfail"] for e in reports),
                       "skip": sum(e["outcome"] == "skipped" and not e["xfail"] for e in reports),
                       "xfail": sum(e["outcome"] == "skipped" and e["xfail"] for e in reports),
                       "xpass": sum(e["outcome"] == "passed" and e["xfail"] for e in reports),
                       "deselected": sum(e.get("count", 0) for e in events if e["kind"] == "deselected")}}


# Optional pytest hooks: importing this file for stdlib controls imports no pytest.
if os.environ.get("MXZ_PHASE"):
    import pytest

    _sequence = 0
    _phase = os.environ["MXZ_PHASE"]

    def event(kind, **fields):
        global _sequence
        row = {"seq": _sequence, "kind": kind, "barrier": os.environ["MXZ_BARRIER"] + ":" + _phase, **fields}
        with (scratch() / (_phase + ".jsonl")).open("ab") as stream:
            stream.write(encoded(row) + b"\n")
        _sequence += 1

    def pytest_collection_finish(session):
        event("collection", nodeids=[item.nodeid for item in session.items],
              cases={item.nodeid: dict(item.callspec.indices) if hasattr(item, "callspec") else {}
                     for item in session.items})

    def pytest_collectreport(report):
        if report.outcome != "passed":
            event("collection_error", outcome=report.outcome, nodeid=report.nodeid)

    def pytest_deselected(items):
        event("deselected", count=len(items))

    def pytest_runtest_logreport(report):
        event("report", nodeid=report.nodeid, when=report.when, outcome=report.outcome,
              xfail=hasattr(report, "wasxfail"), duration=report.duration)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_teardown(item, nextitem):
        # Hold strong references across fixture teardown/loop closure, so a task
        # disappearing from asyncio's weak registry cannot become clean evidence.
        loops = {value for value in item.funcargs.values() if isinstance(value, asyncio.AbstractEventLoop)}
        for value in item.funcargs.values():
            if isinstance(value, asyncio.Runner):
                loops.add(value.get_loop())
        hass = item.funcargs.get("hass")
        if hass is not None:
            loops.add(hass.loop)
        tasks = {task for loop in loops for task in asyncio.all_tasks(loop)}
        timers = [handle for loop in loops for handle in getattr(loop, "_scheduled", [])]
        yield
        tasks.update(task for loop in loops for task in asyncio.all_tasks(loop))
        timers.extend(handle for loop in loops for handle in getattr(loop, "_scheduled", []))
        children = []
        child_files = list(Path("/proc/self/task").glob("*/children"))
        for path in child_files:
            children.extend(path.read_text().split())
        event("cleanup", nodeid=item.nodeid, pending_tasks=sum(not task.done() for task in tasks),
              pending_timers=sum(not handle.cancelled() and handle.when() > loop.time()
                                 for loop in loops for handle in set(timers)),
              child_pids=sorted(set(children)), observed=bool(child_files) and (hass is None or bool(loops)))

    def pytest_sessionfinish(session, exitstatus):
        event("complete", exit=int(exitstatus))


def emit(record, ceiling):
    # Small base64 lines avoid platform line truncation and log-command injection.
    payload = encoded(record)
    chunks = [base64.b64encode(payload[i:i + 3072]).decode() for i in range(0, len(payload), 3072)]
    lines = [f"MXZ-EVIDENCE-PART {i + 1}/{len(chunks)} {chunk}" for i, chunk in enumerate(chunks)]
    marker = "MXZ-EVIDENCE-COMPLETE sha256=" + digest(payload) + " bytes=" + str(len(payload))
    require(sum(len(line) + 1 for line in [*lines, marker]) <= ceiling, "retained evidence byte budget")
    print("\n".join([*lines, marker]))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
            stream.write(marker + "\nAdmission: UNKNOWN; external driver receipt required.\n")


def extract(log):
    rows = re.findall(r"MXZ-EVIDENCE-PART (\d+)/(\d+) ([A-Za-z0-9+/=]+)", log)
    markers = re.findall(r"MXZ-EVIDENCE-COMPLETE sha256=([0-9a-f]{64}) bytes=(\d+)", log)
    require(bool(rows) and len(markers) == 1, "missing/duplicate completion marker")
    require([int(row[0]) for row in rows] == list(range(1, len(rows) + 1))
            and all(int(row[1]) == len(rows) for row in rows), "truncated/reordered retained parts")
    payload = b"".join(base64.b64decode(row[2], validate=True) for row in rows)
    require(digest(payload) == markers[0][0] and len(payload) == int(markers[0][1]), "retained transport digest mismatch")
    return decode(payload)


def finish(status):
    root = scratch()
    record = {"schema": SCHEMA, "complete": False, "status": status,
              "external": "UNKNOWN: driver must bind job URL, resolved actions, effective permissions, current settings and complete platform logs."}
    try:
        record.update(read(root / "begin.json"))
        record["after"] = snapshot()
        record["environment"] = read(root / "environment.json")
        for phase in ("sentinel", "suite"):
            record[phase] = phase_record(phase)
        require(status == "success", "job failed")
        require(record["before"] == record["after"], "source changed")
        check_environment(record["environment"], record["plan"])
        for phase in ("sentinel", "suite"):
            check_phases(record[phase], record["plan"], phase)
        record["complete"] = True
    except (OSError, ValueError, KeyError, TypeError) as exc:
        record["unknown"] = str(exc)
    # Retain bounded producer logs and partial ledgers even after a failed step.
    retained = {}
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix in {".log", ".jsonl", ".json", ".exit", ".seconds"}:
            data = path.read_bytes()
            retained[path.name] = {"sha256": digest(data), "bytes": len(data), "text": data.decode(errors="replace")}
    record["retained"] = retained
    for name in ("venv", "tmp", "sentinel-tmp", "suite-tmp"):
        path = root / name
        if path.exists():
            shutil.rmtree(path)
    record["cleanup"] = {"owned_scratch_removed": all(not (root / name).exists() for name in ("venv", "tmp", "sentinel-tmp", "suite-tmp")), "vm_disposal": "platform-owned"}
    require(not any(read(p)["truncated"] for p in root.glob("*.capture.json")), "truncated producer log; no completion marker")
    emit(record, 14 * MIB)
    return 0 if record["complete"] else 1


def main(args):
    command, *args = args
    if command == "plan":
        print(encoded(plan(*args)).decode())
    elif command == "bootstrap":
        print(encoded({"inventory": inventory(), "runtime": runtime(), "installer": installer()}).decode())
    elif command == "capture":
        return capture(*args)
    elif command == "begin":
        root = scratch()
        root.mkdir(exist_ok=False)
        p = plan(*args)
        context = {name: os.environ.get(env, "UNKNOWN") for name, env in {
            "sha": "GITHUB_SHA", "workflow_sha": "GITHUB_WORKFLOW_SHA", "run_id": "GITHUB_RUN_ID",
            "attempt": "GITHUB_RUN_ATTEMPT", "event": "GITHUB_EVENT_NAME", "ref": "GITHUB_REF"}.items()}
        auth = subprocess.run(["git", "config", "--name-only", "--get-regexp", r"extraheader|credential\..*|core\.sshCommand"], capture_output=True, text=True)
        require(auth.returncode in (0, 1), "checkout credential inspection unavailable")
        write(root / "begin.json", {"plan": p, "before": p["files"], "context": context, "runtime_before": runtime(),
              "checkout_credentials_persisted": bool(auth.stdout.strip()),
              "runner": {name: os.environ.get(env, "UNKNOWN") for name, env in {
                  "os": "RUNNER_OS", "arch": "RUNNER_ARCH", "image_os": "ImageOS", "image_version": "ImageVersion"}.items()}})
    elif command == "auxiliary-begin":
        write(Path(os.environ["RUNNER_TEMP"]) / ("mxz-" + args[0] + "-source.json"), snapshot())
    elif command == "auxiliary":
        name, status = args
        data = {"schema": SCHEMA, "family": name, "status": status,
                "commit": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
                "files_before": read(Path(os.environ["RUNNER_TEMP"]) / ("mxz-" + name + "-source.json")),
                "files_after": snapshot(), "runtime": runtime(),
                "run_id": os.environ.get("GITHUB_RUN_ID", "UNKNOWN"),
                "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "UNKNOWN"),
                "runner_image": os.environ.get("ImageVersion", "UNKNOWN"),
                "external": "UNKNOWN: retain complete action logs, resolved SHA/container image and token/settings receipt; 2 MiB total ceiling."}
        require(data["files_before"] == data["files_after"], "auxiliary changed source")
        emit(data, MIB)
        return 0 if status == "success" else 1
    elif command == "environment":
        record = environment_record()
        write(scratch() / "environment.json", record)
        frozen = read(scratch() / "begin.json")["plan"]
        require(snapshot() == frozen["files"], "install changed source")
        check_environment(record, frozen)
    elif command == "phases":
        frozen = read(scratch() / "begin.json")["plan"]
        require(snapshot() == frozen["files"], "tests changed source")
        require(not read(scratch() / (args[0] + ".capture.json"))["truncated"], "truncated test log")
        check_phases(phase_record(args[0]), frozen, args[0])
    elif command == "finish":
        return finish(*args)
    elif command == "extract":
        write(args[1], extract(Path(args[0]).read_text()))
    elif command == "verify":
        verify(*(read(path) for path in args))
        print("Evidence checks complete; independent driver admission still required.")
    else:
        raise ValueError("unknown command")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print("UNKNOWN: " + str(error), file=sys.stderr)
        sys.exit(1)
