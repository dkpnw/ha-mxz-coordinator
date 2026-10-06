"""Stdlib-only invented controls; these never attest remote settings or run HA.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tools/ci -v
The specimen is handwritten independently of the producer. Mutations re-sign
its digest, so a stale digest cannot accidentally satisfy an unrelated control.
A valid recovery is the original complete specimen, never a relaxed validator.
"""
import base64
import asyncio
from contextlib import redirect_stdout
import io
import os
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import runtime_evidence as evidence


SHA = "a" * 40
HASH = "b" * 64
SENTINEL = "tests/test_changeover.py::test_changeover_from_temperature_sensor"
SAMPLE = "tests/test_invented.py::test_room"


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def raw_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def blob(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "base64": base64.b64encode(data).decode()}


def retain_specimen(record, install_log):
    """Independent serialization of every file the workflow must retain."""
    env = record["environment"]
    raw = {"begin.json": raw_json({key: record[key] for key in
           ("plan", "before", "context", "runtime_before", "checkout_credentials_persisted", "runner")}),
           "environment.json": raw_json(env), "install.json": raw_json(env["install_report"]),
           "bootstrap.json": raw_json({"runtime": env["bootstrap_runtime"], "inventory": env["bootstrap"],
                                       "installer": env["bootstrap_installer"]})}
    for name in ("install", "pip-check", "sentinel", "suite"):
        log = install_log if name == "install" else b"invented ordinary output\n"
        raw[name + ".log"] = log
        raw[name + ".capture.json"] = raw_json({"bytes": len(log), "sha256": hashlib.sha256(log).hexdigest(), "truncated": False})
        raw[name + ".exit"] = b"0\n"
        raw[name + ".recorder.exit"] = b"0\n"
    for name in ("sentinel", "suite"):
        raw[name + ".seconds"] = b"1.5\n"
        raw[name + ".jsonl"] = b"".join(raw_json(row) for row in record[name]["events"])
    record["retained"] = {name: blob(data) for name, data in raw.items()}


def specimen():
    files = {"tests/test_invented.py": {"mode": "100644", "sha256": HASH},
             "requirements_test.txt": {"mode": "100644", "sha256": "c" * 64},
             "tools/ci/runtime_evidence.py": {"mode": "100644", "sha256": "d" * 64}}
    dependencies = {"homeassistant": "2024.12.0", "pytest-homeassistant-custom-component": "0.13.190", "sample-pkg": "1.0"}
    expected = {"schema": 1, "commit": SHA, "tree": "e" * 40, "files": files,
                "lock": "requirements/invented.txt", "lock_sha256": HASH, "selector": "3.12.14",
                "expected_distributions": dependencies,
                "source_tests": {SENTINEL: [{}], SAMPLE: [{"room": 0}, {"room": 1}]},
                "actions": ["actions/checkout@v4", "actions/setup-python@v5"],
                "runner": "ubuntu-24.04", "arch": "X64", "job_minutes": 25,
                "commands": {"sentinel": SENTINEL, "suite": "tests/"},
                "deadlines": {"sentinel": 60, "suite": 180}}
    context = {"sha": SHA, "workflow_sha": SHA, "run_id": "123", "attempt": "1",
               "event": "push", "ref": "refs/heads/ci/locked-runtime-evidence"}
    runner = {"os": "Linux", "arch": "X64", "image_os": "ubuntu24", "image_version": "20261001.1"}
    downloads = [{"metadata": {"name": name, "version": version},
                  "download_info": {"url": "https://files.pythonhosted.org/invented/" + name + (".tar.gz" if name == "sample-pkg" else ".whl"),
                                    "archive_info": {"hashes": {"sha256": HASH}}}}
                 for name, version in dependencies.items()]
    record = {"schema": 1, "complete": True, "status": "success",
              "external": "UNKNOWN: invented external receipt required", "plan": copy.deepcopy(expected),
              "before": copy.deepcopy(files), "after": copy.deepcopy(files),
              "context": context, "runner": runner, "checkout_credentials_persisted": False,
              "cleanup": {"owned_scratch_removed": True, "vm_disposal": "platform-owned"},
              "environment": {"runtime": {"micro": "3.12.14", "build": ["invented", "date"],
                               "implementation": "CPython", "executable": "/invented/python", "sha256": HASH,
                               "prefix": "/invented/venv", "base_prefix": "/invented/base"},
                              "inventory": list(map(list, dependencies.items())) + [["pip", "25.1"]],
                              "bootstrap": [["pip", "25.1"]], "install_exit": 0, "pip_check_exit": 0,
                              "install_recorder_exit": 0, "pip_check_recorder_exit": 0,
                              "install_report": {"version": "1", "install": downloads},
                              "install_log_sha256": HASH}}
    record["environment"]["bootstrap_runtime"] = copy.deepcopy(record["environment"]["runtime"])
    record["runtime_before"] = copy.deepcopy(record["environment"]["runtime"])
    record["runtime_before"]["prefix"] = "/invented/base"
    env_path = "/invented/tmp/pip-build-env-sample"
    source_url = "https://files.pythonhosted.org/invented/sample-pkg.tar.gz"
    tool_text = b"Name: setuptools\nVersion: 80.0\n"
    install_log = (f"Added sample-pkg==1.0 from {source_url} to build tracker '/invented/tracker'\n"
                   f"Created temporary directory: {env_path}\n"
                   f"Removed sample-pkg==1.0 from {source_url} from build tracker '/invented/tracker'\n"
                   f"Created wheel for sample-pkg: filename=sample_pkg-1.0-py3-none-any.whl size=123 sha256={HASH}\n").encode()
    record["environment"]["install_log_sha256"] = hashlib.sha256(install_log).hexdigest()
    record["environment"]["build_provenance"] = {
        "complete": True, "unknown": [], "log_sha256": hashlib.sha256(install_log).hexdigest(),
        "source_environments": {"sample-pkg": env_path}, "source_urls": {"sample-pkg": source_url},
        "isolated_tools": {env_path: [["setuptools", "80.0"]]},
        "tool_metadata": {env_path + "/overlay/lib/python3.12/site-packages/setuptools-80.0.dist-info/METADATA": blob(tool_text)},
        "wheels": [["sample-pkg", "sample_pkg-1.0-py3-none-any.whl", "123", HASH]],
        "limit": "Observed pip log and isolation metadata; not build purity or reproducibility."}
    for name in ["bootstrap_installer", "final_pip"]:
        record["environment"][name] = {"version": "25.1", "entrypoint": "/invented/venv/pip/__main__.py",
                                     "sha256": HASH, "metadata_sha256": HASH}
    for phase, cases, limit in [("sentinel", {SENTINEL: {}}, 60),
                                ("suite", {SENTINEL: {}, SAMPLE + "[east]": {"room": 0}, SAMPLE + "[west]": {"room": 1}}, 180)]:
        barrier = "123:1:py3.12-ha2024.12.0:" + phase
        events = [{"kind": "collection", "nodeids": list(cases), "cases": cases}]
        for node in cases:
            for when in ["setup", "call", "teardown"]:
                events.append({"kind": "report", "nodeid": node, "when": when, "outcome": "passed", "xfail": False, "duration": 0.01})
            events.append({"kind": "cleanup", "nodeid": node, "pending_tasks": 0,
                           "pending_timers": 0, "child_pids": [], "observed": True, "unknown": []})
        events.append({"kind": "complete", "exit": 0})
        for seq, row in enumerate(events):
            row.update(seq=seq, barrier=barrier)
        record[phase] = {"phase": phase, "barrier": barrier, "exit": 0, "recorder_exit": 0, "elapsed": 1.5,
                         "deadline": limit, "events": events,
                         "counts": {"passed": len(cases), "error": 0, "skip": 0, "xfail": 0, "xpass": 0, "deselected": 0}}
    retain_specimen(record, install_log)
    receipt = {"context": copy.deepcopy(context), "runner": copy.deepcopy(runner),
               "job_url": "https://github.com/dkpnw/ha-mxz-coordinator/actions/runs/123/job/456",
               "permissions": {"contents": "read"}, "public": True, "standard_free_runner": True,
               "private_connections": False, "injected_credentials": False, "extra_triggers": False,
               "cache_saves": False, "artifact_uploads": False, "paid_api": False,
               "checkout_credentials_persisted": False, "static_review_commit": SHA,
               "actions": {"actions/checkout@v4": "f" * 40, "actions/setup-python@v5": "1" * 40},
               "jobs": 7, "job_minutes": [25, 25, 25, 10, 10, 10, 10], "observation_minutes": 20,
               "log_bytes": [1000] * 7, "all_completion_markers": True, "logs_untruncated": True,
               "log_sha256": [HASH] * 7, "settings_sha256": HASH,
               "settings_observed_at": "2026-10-06T00:00:00Z (invented)",
               "validation_actions": {"hacs/action@main": SHA, "home-assistant/actions/hassfest@master": SHA},
               "container_images": {"hacs/action@main": "sha256:" + HASH, "home-assistant/actions/hassfest@master": "sha256:" + HASH},
               "record_sha256": sha(record)}
    return record, expected, receipt


def set_path(obj, path, value):
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


def drop_path(obj, path):
    for key in path[:-1]:
        obj = obj[key]
    del obj[path[-1]]


class Controls(unittest.TestCase):
    def test_valid_and_recovery(self):
        record, expected, receipt = specimen()
        evidence.verify(record, expected, receipt)
        record["environment"]["inventory"].pop()
        receipt["record_sha256"] = sha(record)
        with self.assertRaisesRegex(ValueError, "missing pip"):
            evidence.verify(record, expected, receipt)
        evidence.verify(*specimen())
        print("CONTROL valid complete + explicit recovery: ACCEPT (invented only)")

    def test_identity_inventory_settings_and_cost(self):
        mutations = [
            ("wrong SHA", "r", ["context", "sha"], "0" * 40, "wrong workflow"),
            ("wrong workflow SHA", "r", ["context", "workflow_sha"], "0" * 40, "wrong workflow"),
            ("wrong tree", "r", ["plan", "tree"], "0" * 40, "identity mismatch"),
            ("wrong lock", "r", ["plan", "lock_sha256"], "0" * 64, "identity mismatch"),
            ("wrong helper", "r", ["plan", "files", "tools/ci/runtime_evidence.py", "sha256"], "0" * 64, "identity mismatch"),
            ("wrong fixture", "r", ["before", "tests/test_invented.py", "sha256"], "0" * 64, "immutability"),
            ("changed source mode", "r", ["after", "tests/test_invented.py", "mode"], "100755", "immutability"),
            ("wrong Python lane", "r", ["environment", "runtime", "micro"], "3.13.9", "Python lane"),
            ("unknown build", "r", ["environment", "runtime", "build"], ["UNKNOWN", "UNKNOWN"], "unknown runtime"),
            ("system site packages", "r", ["environment", "runtime", "prefix"], "/invented/base", "venv identity"),
            ("changed version", "r", ["environment", "inventory", 0, 1], "2024.12.1", "inventory mismatch"),
            ("empty inventory", "r", ["environment", "inventory"], [], "empty inventory"),
            ("unaccounted bootstrap extra", "r", ["environment", "bootstrap"], [["pip", "25.1"], ["wheel", "1.0"]], "bootstrap extra"),
            ("missing installer hash", "r", ["environment", "bootstrap_installer", "sha256"], "UNKNOWN", "installer provenance"),
            ("changed interpreter during install", "r", ["environment", "bootstrap_runtime", "sha256"], "0" * 64, "runtime changed"),
            ("failed install", "r", ["environment", "install_exit"], 1, "install or pip check"),
            ("failed pip check", "r", ["environment", "pip_check_exit"], 1, "install or pip check"),
            ("incomplete build", "r", ["environment", "build_provenance", "complete"], False, "build provenance"),
            ("missing build tools", "r", ["environment", "build_provenance", "isolated_tools"], {}, "isolated build tools"),
            ("missing built wheel", "r", ["environment", "build_provenance", "wheels"], [], "built wheel"),
            ("missing download hash", "r", ["environment", "install_report", "install", 0, "download_info", "archive_info", "hashes", "sha256"], "", "download hash"),
            ("private package route", "r", ["environment", "install_report", "install", 0, "download_info", "url"], "https://invented.invalid/private.whl", "public source"),
            ("missing runner", "r", ["runner", "image_version"], "UNKNOWN", "unknown runner"),
            ("wrong architecture", "r", ["runner", "arch"], "ARM64", "runner image"),
            ("persistent checkout", "r", ["checkout_credentials_persisted"], True, "persistent checkout"),
            ("write token", "p", ["permissions"], {"contents": "write"}, "token permission"),
            ("additional token scope", "p", ["permissions"], {"contents": "read", "id-token": "write"}, "token permission"),
            ("unknown public boundary", "p", ["public"], None, "public/free"),
            ("paid runner", "p", ["standard_free_runner"], False, "public/free"),
            ("private connection", "p", ["private_connections"], True, "settings"),
            ("injected credential", "p", ["injected_credentials"], True, "settings"),
            ("extra triggers", "p", ["extra_triggers"], True, "settings"),
            ("cache save", "p", ["cache_saves"], True, "settings"),
            ("artifact upload", "p", ["artifact_uploads"], True, "settings"),
            ("paid API", "p", ["paid_api"], True, "settings"),
            ("missing action resolution", "p", ["actions", "actions/checkout@v4"], "UNKNOWN", "action identity"),
            ("wrong static review", "p", ["static_review_commit"], "0" * 40, "static review"),
            ("extra job", "p", ["jobs"], 8, "cost/time"),
            ("larger timeout", "p", ["job_minutes", 0], 26, "cost/time"),
            ("wall clock exceeded", "p", ["observation_minutes"], 91, "cost/time"),
            ("truncated log", "p", ["logs_untruncated"], False, "truncated platform"),
            ("missing completion marker", "p", ["all_completion_markers"], False, "truncated platform"),
            ("log over budget", "p", ["log_bytes", 0], 16 * 1024 * 1024 + 1, "log budget"),
            ("auxiliary log over budget", "p", ["log_bytes", 3], 2 * 1024 * 1024 + 1, "log budget"),
            ("unknown settings receipt", "p", ["settings_sha256"], "UNKNOWN", "settings provenance"),
            ("missing platform log hash", "p", ["log_sha256", 0], "UNKNOWN", "log digests"),
            ("unknown live HACS SHA", "p", ["validation_actions", "hacs/action@main"], "UNKNOWN", "validation action"),
            ("unknown hassfest image", "p", ["container_images", "home-assistant/actions/hassfest@master"], "UNKNOWN", "validation images"),
            ("wrong retained digest", "p", ["record_sha256"], "0" * 64, "digest mismatch"),
            ("incomplete record", "r", ["complete"], False, "incomplete record"),
            ("missing scratch cleanup", "r", ["cleanup", "owned_scratch_removed"], False, "cleanup result"),
            ("retry attempt", "r", ["context", "attempt"], "2", "run/ref/attempt"),
            ("wrong event", "r", ["context", "event"], "pull_request", "run/ref/attempt"),
        ]
        for label, target, path, value, reason in mutations:
            with self.subTest(label=label):
                record, expected, receipt = specimen()
                set_path(record if target == "r" else receipt, path, value)
                if target == "r":
                    receipt["record_sha256"] = sha(record)
                with self.assertRaisesRegex(ValueError, reason):
                    evidence.verify(record, expected, receipt)
                print("CONTROL " + label + ": REJECT")
        for label, mutate in [
            ("missing plugin", lambda rows: rows.pop(1)),
            ("extra distribution", lambda rows: rows.append(["unexpected", "1.0"])),
            ("duplicate normalized distribution", lambda rows: rows.append(["Sample_Pkg", "1.0"])),
            ("undeclared final wheel", lambda rows: rows.append(["wheel", "1.0"])),
        ]:
            record, expected, receipt = specimen()
            mutate(record["environment"]["inventory"])
            receipt["record_sha256"] = sha(record)
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "inventory mismatch|duplicate normalized"):
                evidence.verify(record, expected, receipt)
            print("CONTROL " + label + ": REJECT")

    def test_missing_required_evidence(self):
        for target, paths in [
            ("r", [["plan"], ["before"], ["after"], ["context"], ["runner"], ["cleanup"],
                   ["environment", "runtime"], ["environment", "bootstrap"], ["environment", "install_exit"],
                   ["environment", "pip_check_exit"], ["environment", "install_report"],
                   ["environment", "build_provenance"], ["sentinel", "exit"], ["suite", "exit"],
                   ["suite", "counts"], ["suite", "events", 0, "cases"]]),
            ("p", [["permissions"], ["runner"], ["actions"], ["public"], ["standard_free_runner"],
                   ["job_url"], ["record_sha256"], ["log_bytes"], ["static_review_commit"]]),
        ]:
            for path in paths:
                with self.subTest(target=target, path=path):
                    record, expected, receipt = specimen()
                    drop_path(record if target == "r" else receipt, path)
                    if target == "r":
                        receipt["record_sha256"] = sha(record)
                    with self.assertRaises((KeyError, ValueError)):
                        evidence.verify(record, expected, receipt)
                    print("CONTROL missing " + target + ":" + ".".join(map(str, path)) + ": REJECT")

    def test_all_ordinary_phases_and_cleanup(self):
        for phase in ["sentinel", "suite"]:
            for stage in ["setup", "call", "teardown"]:
                record, expected, receipt = specimen()
                events = record[phase]["events"]
                events[:] = [e for e in events if not (e["kind"] == "report" and e["when"] == stage)]
                for seq, row in enumerate(events):
                    row["seq"] = seq
                receipt["record_sha256"] = sha(record)
                with self.subTest(phase=phase, missing=stage), self.assertRaisesRegex(ValueError, "test phase"):
                    evidence.verify(record, expected, receipt)
                print("CONTROL " + phase + " missing " + stage + ": REJECT")
        mutations = [
            ("skip", ["events", 2, "outcome"], "skipped", "skip/error"),
            ("xfail", ["events", 2, "xfail"], True, "skip/error"),
            ("xpass", ["events", 2, "xfail"], True, "skip/error"),
            ("collection error", ["events", 1, "kind"], "collection_error", "collection error"),
            ("deselection", ["events", 1, "kind"], "deselected", "collection error"),
            ("timeout", ["exit"], 124, "outer exit"),
            ("timeout with zero exit", ["elapsed"], 180, "timeout"),
            ("incomplete session", ["events", -1, "kind"], "report", "incomplete phases"),
            ("stale handler barrier", ["events", 2, "barrier"], "previous-run", "stale handler"),
            ("stale entire ledger", ["barrier"], "previous-run", "stale run"),
            ("truncated sequence", ["events", 2, "seq"], 7, "truncated/reordered"),
            ("empty suite", ["events", 0, "nodeids"], [], "empty/duplicate"),
            ("wrong parameter", ["events", 0, "cases", SAMPLE + "[east]", "room"], 1, "parameter inventory"),
            ("pending HA task", ["events", 4, "pending_tasks"], 1, "cleanup incomplete"),
            ("pending debouncer timer", ["events", 4, "pending_timers"], 1, "cleanup incomplete"),
            ("unreaped known child", ["events", 4, "child_pids"], ["1234"], "cleanup incomplete"),
            ("unknown cleanup", ["events", 4, "observed"], False, "cleanup incomplete"),
        ]
        for label, path, value, reason in mutations:
            record, expected, receipt = specimen()
            set_path(record["suite"], path, value)
            if label == "xfail":
                record["suite"]["events"][2]["outcome"] = "skipped"
            receipt["record_sha256"] = sha(record)
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, reason):
                evidence.verify(record, expected, receipt)
            print("CONTROL " + label + ": REJECT")

    def test_malformed_transport(self):
        value = {"fixture": "invented", "complete": False}
        raw = b'{"complete":false,"fixture":"invented"}'
        log = "timestamp MXZ-EVIDENCE-PART 1/1 " + base64.b64encode(raw).decode() + "\n"
        marker = "MXZ-EVIDENCE-COMPLETE sha256=" + hashlib.sha256(raw).hexdigest() + " bytes=" + str(len(raw))
        self.assertEqual(evidence.extract(log + marker), value)
        for label, text in [("missing transport marker", log),
                            ("missing transport part", log.replace("1/1", "1/2") + marker),
                            ("duplicate transport part", log + log + marker),
                            ("corrupt transport digest", log + marker.replace("sha256=", "sha256=0"))]:
            with self.subTest(label=label), self.assertRaises(ValueError):
                evidence.extract(text)
            print("CONTROL " + label + ": REJECT")
        for label, text in [("truncated JSON", '{"complete":'),
                            ("duplicate JSON identity", '{"sha":"first","sha":"second"}'),
                            ("nonfinite deadline", '{"elapsed":NaN}')]:
            with self.subTest(label=label), self.assertRaises(ValueError):
                evidence.decode(text)
            print("CONTROL " + label + ": REJECT")

    def test_independent_source_and_lock_oracles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "test_example.py"
            path.write_text('''import pytest
ROWS = {"east": 1, "west": 2}
@pytest.mark.parametrize("room", tuple(ROWS))
@pytest.mark.parametrize(("enabled", "expected"), [(True, 1), (False, 0)])
def test_rooms(room, enabled, expected): pass

def test_plain(): pass
''')
            actual = evidence.source_tests(root)
            self.assertEqual(actual, {
                path.as_posix() + "::test_rooms": [
                    {"room": 0, "enabled": 0, "expected": 0},
                    {"room": 0, "enabled": 1, "expected": 1},
                    {"room": 1, "enabled": 0, "expected": 0},
                    {"room": 1, "enabled": 1, "expected": 1}],
                path.as_posix() + "::test_plain": [{}]})
            path.write_text('import pytest\n@pytest.mark.parametrize("x", unknown())\ndef test_x(x): pass\n')
            with self.assertRaisesRegex(ValueError, "unreviewed parameter source"):
                evidence.source_tests(root)
            lock = root / "lock.txt"
            lock.write_text("Example_Name==1.0\npytest==8.3.3\n")
            self.assertEqual(evidence.lock_map(lock), {"example-name": "1.0", "pytest": "8.3.3"})
            lock.write_text("Example_Name==1.0\nexample-name==1.0\n")
            with self.assertRaisesRegex(ValueError, "duplicate normalized"):
                evidence.lock_map(lock)
            lock.write_text("pytest>=8\n")
            with self.assertRaisesRegex(ValueError, "non-exact lock"):
                evidence.lock_map(lock)
        print("CONTROL independent source/lock oracle and unknown source forms: ACCEPT/REJECT as specified")


class ProducerControls(unittest.TestCase):
    def test_ruff_auxiliary_source_boundary(self):
        workflow = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
        text = workflow.read_text()
        ruff = text.split("  ruff:\n", 1)[1].split("  pytest:\n", 1)[0]
        self.assertIn("RUFF_CACHE_DIR: ${{ runner.temp }}/mxz-ruff-cache", ruff)
        self.assertIn("- run: ruff check custom_components/ tests/\n", ruff)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, runner_temp = root / "source", root / "runner"
            source.mkdir()
            runner_temp.mkdir()
            (source / "tracked.py").write_text("# invented source\n")
            (source / ".gitignore").write_text(".ruff_cache/\n")
            for command in (["git", "init", "-q", "--template=", str(source)],
                            ["git", "-C", str(source), "add", "tracked.py", ".gitignore"]):
                subprocess.run(command, check=True, capture_output=True, timeout=10)
            previous = Path.cwd()
            try:
                os.chdir(source)
                with patch.dict(os.environ, {"RUNNER_TEMP": str(runner_temp), "GITHUB_STEP_SUMMARY": ""}):
                    evidence.main(["auxiliary-begin", "ruff"])
                    cache = runner_temp / "mxz-ruff-cache"
                    cache.mkdir()
                    (cache / "invented").write_bytes(b"ordinary cache shape")
                    # No actual Ruff, setup-python, or dependency install is run.
                    with patch.object(evidence, "runtime", return_value={"invented": True}), \
                         patch.object(evidence, "git", wraps=evidence.git) as git, \
                         redirect_stdout(io.StringIO()) as output:
                        original_git = evidence.git._mock_wraps
                        git.side_effect = lambda *args: SHA if args[0] == "rev-parse" else original_git(*args)
                        self.assertEqual(evidence.main(["auxiliary", "ruff", "success"]), 0)
                        row = evidence.extract(output.getvalue())
                        self.assertEqual(row["files_before"], row["files_after"])
                    extra = source / ".ruff_cache"
                    extra.mkdir()
                    (extra / "unexpected").write_bytes(b"ignored but forbidden")
                    with self.assertRaisesRegex(ValueError, "unaccounted checkout files"):
                        evidence.snapshot()
            finally:
                os.chdir(previous)
        print("CONTROL Ruff declared external cache + auxiliary producer: ACCEPT; ignored source extra: REJECT")

    def test_teardown_lifecycle_producer(self):
        class ClosingLoop(asyncio.SelectorEventLoop):
            def close(self):
                if not self.is_closed():
                    self.call_later(3600, lambda: None)
                super().close()

        for case in ("cancel-before", "leak-before", "cancel-during", "leak-during", "leak-in-close",
                     "missing-queue", "missing-children", "no-loop"):
            loop = ClosingLoop() if case == "leak-in-close" else asyncio.new_event_loop()
            rows = []
            timer = None
            item = SimpleNamespace(nodeid=SENTINEL, funcargs={} if case == "no-loop" else {"hass": SimpleNamespace(loop=loop)})
            original_call_at = loop.call_at
            try:
                if case in ("cancel-before", "leak-before"):
                    timer = loop.call_later(3600, lambda: None)
                queue = loop._scheduled
                if case == "missing-queue":
                    del loop._scheduled
                with patch.object(evidence, "event", create=True, side_effect=lambda kind, **fields: rows.append({"kind": kind, **fields})), \
                     patch.object(evidence, "child_snapshot", return_value=([], case != "missing-children")):
                    hook = evidence.observe_teardown(item, None)
                    next(hook)
                    if case == "missing-queue":
                        loop._scheduled = queue
                    if case in ("cancel-during", "leak-during"):
                        timer = loop.call_later(3600, lambda: None)
                    if case in ("cancel-before", "cancel-during"):
                        timer.cancel()
                    loop.close()
                    with self.assertRaises(StopIteration):
                        next(hook)
                self.assertEqual(loop.call_at, original_call_at)
                row = rows[0]
                positive = case in ("cancel-before", "cancel-during")
                if case in ("missing-queue", "missing-children", "no-loop"):
                    self.assertFalse(row["observed"])
                    self.assertIsNone(row["pending_timers"])
                    self.assertTrue(row["unknown"])
                else:
                    self.assertTrue(row["observed"])
                    self.assertEqual(row["pending_timers"], 0 if positive else 1)
                record, expected, _ = specimen()
                events = record["sentinel"]["events"]
                events[4].update(row)
                if positive:
                    evidence.check_phases(record["sentinel"], expected, "sentinel")
                else:
                    with self.assertRaisesRegex(ValueError, "fixture cleanup incomplete"):
                        evidence.check_phases(record["sentinel"], expected, "sentinel")
                print("CONTROL actual teardown producer " + case + ": " + ("ACCEPT" if positive else "REJECT"))
            finally:
                if not loop.is_closed():
                    loop.close()

    def test_each_source_build_producer(self):
        for case in ("complete", "one-inventory-missing", "unknown-tool", "missing-attribution", "wrong-source-url"):
            record, expected, _ = specimen()
            env = record["environment"]
            dependencies = {"alpha": "1.0", "beta": "2.0"}
            expected["expected_distributions"] = dependencies
            env["inventory"] = [[*row] for row in dependencies.items()] + [["pip", "25.1"]]
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                logs, downloads = [], []
                for name, version in dependencies.items():
                    url = f"https://files.pythonhosted.org/invented/{name}-{version}.tar.gz"
                    path = root / "tmp" / ("pip-build-env-" + name)
                    tracker = f"{name}=={version} from {url}"
                    logged = tracker.replace(name + "-" + version, "unrelated") if case == "wrong-source-url" and name == "beta" else tracker
                    logs.extend([f"Added {logged} to build tracker '/invented/tracker'",
                                 f"Created temporary directory: {path}",
                                 f"Removed {logged} from build tracker '/invented/tracker'",
                                 f"Created wheel for {name}: filename={name}-{version}-py3-none-any.whl size=123 sha256={HASH}"])
                    if case == "missing-attribution" and name == "beta":
                        logs[-3] = "unrecognized environment observation"
                    if not (case == "one-inventory-missing" and name == "beta"):
                        meta = path / "overlay/lib/python3.12/site-packages/setuptools-80.0.dist-info/METADATA"
                        meta.parent.mkdir(parents=True)
                        tool_version = "UNKNOWN" if case == "unknown-tool" and name == "beta" else "80.0"
                        meta.write_text(f"Name: setuptools\nVersion: {tool_version}\n")
                    downloads.append({"metadata": {"name": name, "version": version},
                                      "download_info": {"url": url, "archive_info": {"hashes": {"sha256": HASH}}}})
                (root / "install.log").write_text("\n".join(logs) + "\n")
                (root / "install.json").write_bytes(raw_json({"version": "1", "install": downloads}))
                (root / "bootstrap.json").write_bytes(raw_json({"runtime": env["runtime"], "inventory": env["bootstrap"], "installer": env["bootstrap_installer"]}))
                for name in ("install.exit", "install.recorder.exit", "pip-check.exit", "pip-check.recorder.exit"):
                    (root / name).write_text("0\n")
                with patch.object(evidence, "scratch", return_value=root), \
                     patch.object(evidence, "runtime", return_value=env["runtime"]), \
                     patch.object(evidence, "installer", return_value=env["final_pip"]), \
                     patch.object(evidence, "inventory", return_value=env["inventory"]):
                    produced = evidence.environment_record()
                if case == "complete":
                    evidence.check_environment(produced, expected)
                    self.assertEqual(set(produced["build_provenance"]["source_environments"]), {"alpha", "beta"})
                else:
                    with self.assertRaisesRegex(ValueError, "incomplete build provenance|source build URL mismatch"):
                        evidence.check_environment(produced, expected)
                print("CONTROL two-source environment producer " + case + ": " + ("ACCEPT" if case == "complete" else "REJECT"))

    def test_workflow_shell_preserves_both_exits(self):
        helper = Path(evidence.__file__).resolve()
        workflow = (helper.parents[2] / ".github/workflows/ci.yml").read_text()
        for name in ("sentinel", "suite"):
            phase_block = re.search(r"        run: \|\n((?:          .*\n)+)", workflow.split(
                "      - name: " + ("Ordinary sentinel" if name == "sentinel" else "Complete unchanged suite"), 1)[1])[1]
            phase_block = "\n".join(line[10:] for line in phase_block.splitlines())
            for upstream, downstream in ((0, 0), (0, 7), (6, 0), (6, 7)):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    record, expected, _ = specimen()
                    for filename in ("begin.json", name + ".jsonl"):
                        (root / filename).write_bytes(base64.b64decode(record["retained"][filename]["base64"]))
                    # Only replace the unauthorized pytest invocation and source
                    # snapshot. The workflow status commands and real recorder/
                    # phase producer/validator run, even with parseable metadata.
                    command = f"(printf 'invented output\\n'; exit {upstream})"
                    recorder = f"(python '{helper}' capture {name}; exit {downstream})"
                    block = re.sub(r"MXZ_PHASE=.*", command + " | " + recorder, phase_block)
                    validator = ("python -c 'import sys; sys.path.insert(0, " + repr(str(helper.parent)).replace("'", '"') + "); "
                                 "import runtime_evidence as e; e.check_phases(e.phase_record(\"" + name + "\"), "
                                 "e.read(e.scratch()/\"begin.json\")[\"plan\"], \"" + name + "\")'")
                    block = block.replace("python tools/ci/runtime_evidence.py phases " + name, validator)
                    env = {**os.environ, "MXZ_EVIDENCE": str(root), "MXZ_BARRIER": "123:1:py3.12-ha2024.12.0", "PYTHONDONTWRITEBYTECODE": "1"}
                    result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", block], env=env,
                                            capture_output=True, timeout=10)
                    self.assertEqual(int((root / (name + ".exit")).read_text()), upstream)
                    self.assertEqual(int((root / (name + ".recorder.exit")).read_text()), downstream)
                    self.assertEqual((root / (name + ".log")).read_bytes(), b"invented output\n")
                    self.assertFalse(json.loads((root / (name + ".capture.json")).read_text())["truncated"])
                    self.assertEqual(result.returncode, 0 if upstream == downstream == 0 else 1, result.stderr.decode())
                    print(f"CONTROL {name} workflow shell exits {upstream}/{downstream}: " + ("ACCEPT" if result.returncode == 0 else "REJECT, logs retained"))

    def test_finish_and_complete_retained_specimen(self):
        record, expected, receipt = specimen()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, value in record["retained"].items():
                (root / name).write_bytes(base64.b64decode(value["base64"]))
            with patch.object(evidence, "scratch", return_value=root), \
                 patch.object(evidence, "snapshot", return_value=expected["files"]), \
                 patch.dict(os.environ, {"MXZ_BARRIER": "123:1:py3.12-ha2024.12.0", "GITHUB_STEP_SUMMARY": ""}), \
                 redirect_stdout(io.StringIO()) as stream:
                result = evidence.finish("success")
            self.assertEqual(result, 0, stream.getvalue())
            produced = evidence.extract(stream.getvalue())
            receipt["record_sha256"] = sha(produced)
            evidence.verify(produced, expected, receipt)
            self.assertEqual(produced["sentinel"], record["sentinel"])
            self.assertEqual(produced["retained"], record["retained"])
            # Invalid UTF-8 is preserved exactly, never replacement-decoded.
            (root / "suite.log").write_bytes(b"\xff\x00\n")
            (root / "suite.capture.json").write_bytes(raw_json({"bytes": 3, "sha256": hashlib.sha256(b"\xff\x00\n").hexdigest(), "truncated": False}))
            with patch.object(evidence, "scratch", return_value=root), \
                 patch.object(evidence, "snapshot", return_value=expected["files"]), \
                 patch.dict(os.environ, {"MXZ_BARRIER": "123:1:py3.12-ha2024.12.0", "GITHUB_STEP_SUMMARY": ""}), \
                 redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(evidence.finish("success"), 0)
            produced = evidence.extract(stream.getvalue())
            self.assertEqual(base64.b64decode(produced["retained"]["suite.log"]["base64"]), b"\xff\x00\n")
        print("CONTROL complete finish → emit → extract → verify producer specimen + exact non-UTF8 bytes: ACCEPT")

    def test_retained_missing_and_contradictions(self):
        record, _, _ = specimen()
        for name in record["retained"]:
            r, expected, receipt = specimen()
            del r["retained"][name]
            receipt["record_sha256"] = sha(r)
            with self.subTest(missing=name), self.assertRaisesRegex(ValueError, "retained evidence"):
                evidence.verify(r, expected, receipt)
            print("CONTROL missing retained " + name + ": REJECT")
        def change_runtime_consistently(r, p):
            r["runtime_before"]["micro"] = "3.14.0"
            begin = json.loads(base64.b64decode(r["retained"]["begin.json"]["base64"]))
            begin["runtime_before"] = r["runtime_before"]
            r["retained"]["begin.json"] = blob(raw_json(begin))

        changes = [
            ("consistent bytes but changed runtime", change_runtime_consistently, "pre-install runtime contradiction"),
            ("empty job suffix", lambda r, p: p.update(job_url=p["job_url"].rsplit("/", 1)[0] + "/"), "job identity"),
            ("nonnumeric job", lambda r, p: p.update(job_url=p["job_url"] + "x"), "job identity"),
            ("failed status", lambda r, p: r.update(status="failure"), "record status"),
            ("unknown status", lambda r, p: r.update(unknown="missing capability"), "record status"),
            ("missing runtime before", lambda r, p: r.pop("runtime_before"), None),
            ("contradictory runtime before", lambda r, p: r["runtime_before"].update(micro="3.14.0"), "retained begin"),
            ("missing duration", lambda r, p: r["suite"]["events"][2].pop("duration"), None),
            ("negative duration", lambda r, p: r["suite"]["events"][2].update(duration=-1), "report duration"),
            ("infinite duration", lambda r, p: r["suite"]["events"][2].update(duration=float("inf")), "report duration"),
            ("absent retained map", lambda r, p: r.pop("retained"), None),
            ("empty retained map", lambda r, p: r.update(retained={}), "retained evidence"),
            ("raw log digest", lambda r, p: r["retained"]["install.log"].update(sha256="0" * 64), "bytes/digest"),
            ("raw log size", lambda r, p: r["retained"]["suite.log"].update(bytes=0), "bytes/digest"),
            ("changed log with fresh digest", lambda r, p: r["retained"].update({"install.log": blob(b"contradiction")}), "install log mismatch"),
            ("contradictory capture", lambda r, p: r["retained"].update({"suite.capture.json": blob(raw_json({"bytes": 0, "sha256": HASH, "truncated": False}))}), "retained capture"),
            ("contradictory ledger", lambda r, p: r["retained"].update({"suite.jsonl": blob(b"{}\n")}), "ledger mismatch"),
            ("contradictory elapsed", lambda r, p: r["retained"].update({"suite.seconds": blob(b"20\n")}), "duration mismatch"),
            ("retained recorder failure", lambda r, p: r["retained"].update({"sentinel.recorder.exit": blob(b"7\n")}), "process failure"),
            ("retained upstream failure", lambda r, p: r["retained"].update({"suite.exit": blob(b"6\n")}), "process failure"),
            ("retained install report", lambda r, p: r["retained"].update({"install.json": blob(b"{}\n")}), "install report mismatch"),
            ("retained bootstrap", lambda r, p: r["retained"].update({"bootstrap.json": blob(b"{}\n")}), "bootstrap mismatch"),
            ("unknown source tool", lambda r, p: r["environment"]["build_provenance"]["isolated_tools"]["/invented/tmp/pip-build-env-sample"][0].__setitem__(1, "UNKNOWN"), "invalid distribution"),
        ]
        for label, mutate, reason in changes:
            r, expected, receipt = specimen()
            mutate(r, receipt)
            receipt["record_sha256"] = sha(r)
            with self.subTest(label=label):
                context = self.assertRaisesRegex(ValueError, reason) if reason else self.assertRaises((KeyError, ValueError))
                with context:
                    evidence.verify(r, expected, receipt)
            print("CONTROL " + label + ": REJECT (fresh transport digest)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
