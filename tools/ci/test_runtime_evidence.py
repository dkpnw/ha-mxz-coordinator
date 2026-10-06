"""Stdlib-only invented controls; these never attest remote settings or run HA.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tools/ci -v
The specimen is handwritten independently of the producer. Mutations re-sign
its digest, so a stale digest cannot accidentally satisfy an unrelated control.
A valid recovery is the original complete specimen, never a relaxed validator.
"""
import base64
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
    record = {"schema": 1, "complete": True, "plan": copy.deepcopy(expected),
              "before": copy.deepcopy(files), "after": copy.deepcopy(files),
              "context": context, "runner": runner, "checkout_credentials_persisted": False,
              "cleanup": {"owned_scratch_removed": True, "vm_disposal": "platform-owned"},
              "environment": {"runtime": {"micro": "3.12.14", "build": ["invented", "date"],
                               "implementation": "CPython", "executable": "/invented/python", "sha256": HASH,
                               "prefix": "/invented/venv", "base_prefix": "/invented/base"},
                              "inventory": list(map(list, dependencies.items())) + [["pip", "25.1"]],
                              "bootstrap": [["pip", "25.1"]], "install_exit": 0, "pip_check_exit": 0,
                              "install_report": {"version": "1", "install": downloads},
                              "install_log_sha256": HASH,
                              "build_provenance": {"complete": True, "log_sha256": HASH,
                                                   "isolated_tools": {"invented/site-packages": [["setuptools", "80.0"]]},
                                                   "wheels": [["sample-pkg", "sample_pkg-1.0-py3-none-any.whl", "123", HASH]]}}}
    record["environment"]["bootstrap_runtime"] = copy.deepcopy(record["environment"]["runtime"])
    for name in ["bootstrap_installer", "final_pip"]:
        record["environment"][name] = {"version": "25.1", "entrypoint": "/invented/venv/pip/__main__.py",
                                     "sha256": HASH, "metadata_sha256": HASH}
    for phase, cases, limit in [("sentinel", {SENTINEL: {}}, 60),
                                ("suite", {SENTINEL: {}, SAMPLE + "[east]": {"room": 0}, SAMPLE + "[west]": {"room": 1}}, 180)]:
        barrier = "123:1:py3.12-ha2024.12.0:" + phase
        events = [{"kind": "collection", "nodeids": list(cases), "cases": cases}]
        for node in cases:
            for when in ["setup", "call", "teardown"]:
                events.append({"kind": "report", "nodeid": node, "when": when, "outcome": "passed", "xfail": False})
            events.append({"kind": "cleanup", "nodeid": node, "pending_tasks": 0,
                           "pending_timers": 0, "child_pids": [], "observed": True})
        events.append({"kind": "complete", "exit": 0})
        for seq, row in enumerate(events):
            row.update(seq=seq, barrier=barrier)
        record[phase] = {"phase": phase, "barrier": barrier, "exit": 0, "elapsed": 1.5,
                         "deadline": limit, "events": events,
                         "counts": {"passed": len(cases), "error": 0, "skip": 0, "xfail": 0, "xpass": 0, "deselected": 0}}
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
