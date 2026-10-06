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
from email.parser import BytesParser
import math
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
        require(known(key) and isinstance(version, str) and known(version), "invalid distribution")
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
    require(type(record["install_exit"]) is int and type(record["pip_check_exit"]) is int
            and record["install_exit"] == 0 and record["pip_check_exit"] == 0, "install or pip check failed")
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
    tools = build["isolated_tools"]
    associations = build["source_environments"]
    built = {normalized(row[0]) for row in build["wheels"]}
    require(sources <= built, "missing built wheel")
    require(set(associations) == built and len(set(associations.values())) == len(associations),
            "missing per-source build attribution")
    require(set(tools) == set(associations.values()), "missing isolated build tools")
    for rows in tools.values():
        distribution_map(rows)
    require(all(len(row) == 4 and row[1].endswith(".whl") and int(row[2]) > 0
                and re.fullmatch(r"[0-9a-f]{64}", row[3]) for row in build["wheels"]), "missing wheel provenance")
    for item in downloads:
        name = normalized(item["metadata"]["name"])
        if name in sources:
            require(build["source_urls"][name].split("#", 1)[0] ==
                    item["download_info"]["url"].split("#", 1)[0], "source build URL mismatch")
    require(build["log_sha256"] == record["install_log_sha256"], "build log mismatch")
    for name in ("install_recorder_exit", "pip_check_recorder_exit"):
        require(type(record[name]) is int and record[name] == 0, "failed install recorder")


def check_phases(record, expected, phase):
    require(record["phase"] == phase and type(record["exit"]) is int and record["exit"] == 0, "missing/failed outer exit")
    require(type(record["recorder_exit"]) is int and record["recorder_exit"] == 0, "failed phase recorder")
    require(type(record["elapsed"]) in (int, float) and math.isfinite(record["elapsed"]), "invalid elapsed duration")
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
                    and event["child_pids"] == [] and event["observed"] is True
                    and event["unknown"] == [], "fixture cleanup incomplete")
        else:
            require(event["kind"] == "report" and event["nodeid"] in phases, "collection error/deselection/unknown outcome")
            require(event["outcome"] == "passed" and event["xfail"] is False, "skip/error/xfail/xpass")
            require(type(event["duration"]) in (int, float) and math.isfinite(event["duration"])
                    and event["duration"] >= 0, "missing/invalid report duration")
            phases[event["nodeid"]].append(event["when"])
    require(all(value == ["setup", "call", "teardown"] for value in phases.values()), "missing/duplicate test phase")
    require(set(cleanup) == set(ids), "missing cleanup")
    require(record["counts"] == {"passed": len(ids), "error": 0, "skip": 0,
                                "xfail": 0, "xpass": 0, "deselected": 0}, "nonordinary result counts")


def verify(record, expected, receipt):
    """Pure validator. Receipt is external evidence, not a provider lookup."""
    require(record["schema"] == SCHEMA and record["complete"] is True, "incomplete record")
    require(record["status"] == "success" and "unknown" not in record, "failed/unknown record status")
    require(record["plan"] == expected, "source/lock/helper/fixture/workflow identity mismatch")
    require(record["before"] == expected["files"] == record["after"], "source immutability mismatch")
    context = record["context"]
    require(context["sha"] == expected["commit"] and context["workflow_sha"] == expected["commit"], "wrong workflow/checkout SHA")
    require(context["attempt"] == "1" and context["event"] == "push"
            and context["ref"] == "refs/heads/ci/locked-runtime-evidence", "wrong run/ref/attempt")
    require(bool(re.fullmatch(r"[1-9][0-9]*", context["run_id"])) and bool(re.fullmatch(
        "https://github.com/dkpnw/ha-mxz-coordinator/actions/runs/" + context["run_id"] + r"/job/[1-9][0-9]*",
        receipt["job_url"])), "missing job identity")
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
    check_retained(record)


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
    tool_metadata = {}
    for path in sorted((root / "tmp").glob("pip-build-env-*/**/*.dist-info/METADATA")):
        tool_metadata[str(path)] = retained_bytes(path.read_bytes())
    build = build_record(log, tool_metadata)
    bootstrap = read(root / "bootstrap.json")
    return {"runtime": runtime(), "inventory": inventory(), "bootstrap": bootstrap["inventory"],
            "bootstrap_runtime": bootstrap["runtime"], "bootstrap_installer": bootstrap["installer"], "final_pip": installer(),
            "install_exit": int((root / "install.exit").read_text()),
            "pip_check_exit": int((root / "pip-check.exit").read_text()),
            "install_report": report, "install_log_sha256": digest(log),
            "install_recorder_exit": int((root / "install.recorder.exit").read_text()),
            "pip_check_recorder_exit": int((root / "pip-check.recorder.exit").read_text()),
            "build_provenance": build}


def phase_record(name):
    root = scratch()
    events = [decode(line) for line in (root / (name + ".jsonl")).read_text().splitlines()]
    reports = [e for e in events if e["kind"] == "report"]
    return {"phase": name, "barrier": os.environ["MXZ_BARRIER"] + ":" + name,
            "exit": int((root / (name + ".exit")).read_text()),
            "recorder_exit": int((root / (name + ".recorder.exit")).read_text()),
            "elapsed": float((root / (name + ".seconds")).read_text()),
            "deadline": 60 if name == "sentinel" else 180, "events": events,
            "counts": {"passed": sum(e["when"] == "call" and e["outcome"] == "passed" and not e["xfail"] for e in reports),
                       "error": sum(e["outcome"] == "failed" and not e["xfail"] for e in reports),
                       "skip": sum(e["outcome"] == "skipped" and not e["xfail"] for e in reports),
                       "xfail": sum(e["outcome"] == "skipped" and e["xfail"] for e in reports),
                       "xpass": sum(e["outcome"] == "passed" and e["xfail"] for e in reports),
                       "deselected": sum(e.get("count", 0) for e in events if e["kind"] == "deselected")}}


def retained_bytes(data):
    # Base64 preserves arbitrary stdout bytes; replacement decoding loses evidence.
    return {"sha256": digest(data), "bytes": len(data), "base64": base64.b64encode(data).decode()}


def retained_data(row):
    data = base64.b64decode(row["base64"], validate=True)
    require(type(row["bytes"]) is int and row["bytes"] == len(data)
            and row["sha256"] == digest(data), "retained bytes/digest mismatch")
    return data


def build_record(log, tool_metadata):
    """Attribute pip's debug tracker intervals to each isolated source build.

    These are observations of ordinary online pip, not an offline toolchain.
    An unfamiliar/missing/ambiguous log or metadata shape stays incomplete.
    """
    tools, associations, urls, stack, errors = {}, {}, {}, [], []
    for line in log.decode(errors="replace").splitlines():
        line = line.strip()
        added = re.fullmatch(r"Added (.+) to build tracker (.+)", line)
        removed = re.fullmatch(r"Removed (.+) from build tracker (.+)", line)
        created = re.fullmatch(r"Created temporary directory: (.+/pip-build-env-[^/]+)", line)
        if added:
            stack.append(added.groups())
        elif removed:
            if not stack or stack.pop() != removed.groups():
                errors.append("unmatched build tracker interval")
        elif created:
            source = re.match(r"([A-Za-z0-9_.-]+)(?:[^ ]*) from (https://files\.pythonhosted\.org/\S+)",
                              stack[-1][0]) if stack else None
            if source is None:
                errors.append("unattributed build environment")
                continue
            name, url = normalized(source[1]), source[2]
            if name in associations or created[1] in associations.values():
                errors.append("ambiguous build environment")
            associations[name], urls[name] = created[1], url
    if stack:
        errors.append("unclosed build tracker interval")
    for path, row in tool_metadata.items():
        match = re.fullmatch(r"(.+/pip-build-env-[^/]+)/(?:normal|overlay)/.+/site-packages/[^/]+\.dist-info/METADATA", path)
        if not match:
            errors.append("unrecognized build metadata path")
            continue
        message = BytesParser().parsebytes(retained_data(row))
        tools.setdefault(match[1], []).append([message.get("Name"), message.get("Version")])
    for rows in tools.values():
        try:
            distribution_map(rows)
            rows.sort()
        except (TypeError, ValueError):
            errors.append("unknown/duplicate build tool identity")
    wheels = [list(row) for row in re.findall(
        r"Created wheel for ([^:]+): filename=(\S+) size=(\d+) sha256=([0-9a-f]{64})", log.decode(errors="replace"))]
    built = [normalized(row[0]) for row in wheels]
    if (len(built) != len(set(built)) or set(built) != set(associations)
            or set(tools) != set(associations.values())):
        errors.append("incomplete per-source tool/wheel provenance")
    return {"complete": not errors, "unknown": errors, "log_sha256": digest(log),
            "source_environments": associations, "source_urls": urls,
            "isolated_tools": tools, "tool_metadata": tool_metadata, "wheels": wheels,
            "limit": "Observed pip log and isolation metadata; not build purity or reproducibility."}


def check_retained(record):
    """Require producer inputs and compare their bytes to every derived claim."""
    retained = record["retained"]
    required = {"begin.json", "bootstrap.json", "environment.json", "install.json"}
    for name in ("install", "pip-check", "sentinel", "suite"):
        required.update(name + suffix for suffix in (".log", ".capture.json", ".exit", ".recorder.exit"))
    for name in ("sentinel", "suite"):
        required.update((name + ".jsonl", name + ".seconds"))
    require(set(retained) == required, "missing/unexpected retained evidence")
    data = {name: retained_data(row) for name, row in retained.items()}
    begin = decode(data["begin.json"])
    begin_keys = {"plan", "before", "context", "runtime_before", "checkout_credentials_persisted", "runner"}
    require(set(begin) == begin_keys and all(begin[key] == record[key] for key in begin_keys), "retained begin mismatch")
    before, runtime_after = record["runtime_before"], record["environment"]["runtime"]
    runtime_keys = {"micro", "build", "implementation", "executable", "sha256", "prefix", "base_prefix"}
    require(set(before) == set(runtime_after) == runtime_keys and known(before), "missing pre-install runtime identity")
    require(all(before[key] == runtime_after[key] for key in runtime_keys - {"prefix"})
            and before["prefix"] == before["base_prefix"], "pre-install runtime contradiction")
    env = record["environment"]
    require(decode(data["environment.json"]) == env, "retained environment mismatch")
    require(decode(data["install.json"]) == env["install_report"], "retained install report mismatch")
    require(decode(data["bootstrap.json"]) == {"inventory": env["bootstrap"], "runtime": env["bootstrap_runtime"],
            "installer": env["bootstrap_installer"]}, "retained bootstrap mismatch")
    require(digest(data["install.log"]) == env["install_log_sha256"], "retained install log mismatch")
    require(build_record(data["install.log"], env["build_provenance"]["tool_metadata"]) == env["build_provenance"],
            "retained build provenance mismatch")
    for name in ("install", "pip-check", "sentinel", "suite"):
        capture = decode(data[name + ".capture.json"])
        require(capture == {"bytes": len(data[name + ".log"]), "sha256": digest(data[name + ".log"]), "truncated": False}
                and type(capture["bytes"]) is int and capture["truncated"] is False,
                "incomplete/contradictory retained capture")
        require(len(data[name + ".log"]) <= 6 * MIB, "retained log over budget")
        upstream, recorder = int(data[name + ".exit"]), int(data[name + ".recorder.exit"])
        require(upstream == recorder == 0, "retained process failure")
        if name in ("install", "pip-check"):
            prefix = name.replace("-", "_")
            require(upstream == env[prefix + "_exit"] and recorder == env[prefix + "_recorder_exit"], "retained install exit mismatch")
        else:
            phase = record[name]
            require(upstream == phase["exit"] and recorder == phase["recorder_exit"], "retained phase exit mismatch")
            require(float(data[name + ".seconds"]) == phase["elapsed"], "retained phase duration mismatch")
            require([decode(line) for line in data[name + ".jsonl"].splitlines()] == phase["events"], "retained phase ledger mismatch")
    require(isinstance(record["external"], str) and bool(record["external"]), "missing external evidence boundary")


def child_snapshot():
    paths = list(Path("/proc/self/task").glob("*/children"))
    return sorted({pid for path in paths for pid in path.read_text().split()}), bool(paths)


def observe_teardown(item, nextitem):
    """Observe existing loops through teardown, including work created in close.

    Wrappers delegate unchanged and are restored. They never cancel/repair work.
    Unavailable queues, loops, or wrapper continuity leave counts UNKNOWN.
    """
    loops = {value for value in item.funcargs.values() if isinstance(value, asyncio.AbstractEventLoop)}
    for value in item.funcargs.values():
        if isinstance(value, asyncio.Runner):
            loops.add(value.get_loop())
    hass = item.funcargs.get("hass")
    if hass is not None:
        loops.add(hass.loop)
    errors, states, patches = [], {}, []
    if not loops:
        errors.append("no observable fixture loop")

    def sample(loop):
        try:
            queue = loop._scheduled
            require(isinstance(queue, list), "scheduled queue unavailable")
            states[loop]["timers"].update(queue)
            states[loop]["tasks"].update(asyncio.all_tasks(loop))
        except (AttributeError, TypeError, ValueError) as exc:
            errors.append(str(exc))

    def wrap(loop, name):
        original = getattr(loop, name)
        previous = loop.__dict__.get(name)
        present = name in loop.__dict__

        def observed(*args, **kwargs):
            if name == "close":
                sample(loop)
            result = original(*args, **kwargs)
            if name == "call_at":
                states[loop]["timers"].add(result)
            elif name == "create_task":
                states[loop]["tasks"].add(result)
            return result

        setattr(loop, name, observed)
        patches.append((loop, name, previous, present, observed))

    try:
        for loop in loops:
            states[loop] = {"timers": set(), "tasks": set()}
            sample(loop)
            try:
                require(not loop.is_closed(), "fixture loop already closed")
                for name in ("call_at", "create_task", "close"):
                    wrap(loop, name)
            except (AttributeError, TypeError, ValueError) as exc:
                errors.append(str(exc))
        yield
    finally:
        for loop in loops:
            sample(loop)
        for loop, name, previous, present, wrapper in reversed(patches):
            if getattr(loop, name) is not wrapper:
                errors.append("loop observer replaced during teardown")
            if present:
                setattr(loop, name, previous)
            else:
                delattr(loop, name)
        try:
            children, observed = child_snapshot()
        except OSError as exc:
            children, observed = None, False
            errors.append(str(exc))
        if not observed:
            errors.append("child-process observation unavailable")
        event("cleanup", nodeid=item.nodeid,
              pending_tasks=None if errors else sum(not task.done() for state in states.values() for task in state["tasks"]),
              pending_timers=None if errors else sum(not handle.cancelled() and handle.when() > loop.time()
                  for loop, state in states.items() for handle in state["timers"]),
              child_pids=children, observed=not errors, unknown=errors)


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

    pytest_runtest_teardown = pytest.hookimpl(hookwrapper=True, tryfirst=True)(observe_teardown)

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
            retained[path.name] = retained_bytes(data)
    record["retained"] = retained
    for name in ("venv", "tmp", "sentinel-tmp", "suite-tmp"):
        path = root / name
        if path.exists():
            shutil.rmtree(path)
    record["cleanup"] = {"owned_scratch_removed": all(not (root / name).exists() for name in ("venv", "tmp", "sentinel-tmp", "suite-tmp")), "vm_disposal": "platform-owned"}
    if record["complete"]:
        try:
            check_retained(record)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            record.update(complete=False, unknown=str(exc))
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
