"""Export-only admission gates; importing helpers does not install these hooks."""

import hashlib
import importlib.machinery
import json
import os
import re
import sys
from pathlib import Path

import pytest

REFS = {
    "refs/heads/ci/issue25-harness-admission": ("A3", "admission"),
    "refs/heads/ci/issue25-harness-replacement": ("R2", "pack"),
    "refs/heads/ci/issue25-harness-repeat": ("I1", "pack"),
}
BASES = {
    "released": ("3a9863896f8affb6f71cbd1e495df21b17a69ff3", "238636de6d067c8e05b59993819cc5187c2bf9df"),
    "main": ("009b6b42326252ee633f2288573a8052f4e8c1e8", "63b5af3d2f421ea5673862e90ac2cfdf8686abaf"),
}
PACK_ITEMS = [f"tools/issue25/test_restore.py::test_restore_schedule[{name}]" for name in (
    "01-clean-twice", "02-unavailable-twice", "03-manual-twin", "04-options-reload",
    "05-missing-restore-negative", "06-changed-active-demand",
    "07-provisional-auto-old-echo", "08-missing-speed-recovery",
)]
ADMISSION_ITEMS = ["tools/issue25/test_setup_admission.py::test_exported_setup"]
CONFIG_BYTES = b"[pytest]\nasyncio_mode = auto\n"
_FIELDS = {
    "schema_version", "stage", "run_id", "attempt", "event", "ref", "source_commit",
    "source_tree", "workflow_blob", "case_id", "mode", "base", "base_commit", "base_tree",
    "checkout", "export", "temp", "expected_config", "actual_config", "python",
    "base_manifest", "product_manifest", "product_paths", "overlay", "config_sha256",
    "fixture_manifest", "inventory", "items", "phases", "deadline", "injection",
    "exception", "reason", "exit", "fault_paths",
}
_CONTROL_KEYS = {"ISSUE25_MODE", "ISSUE25_BASE", "ISSUE25_CASE_ID", "ISSUE25_EXPECTED",
                 "ISSUE25_EXPECTED_SHA256", "ISSUE25_INJECTION"}
_REPORTS = []


def emit(event, **fields):
    print("ADMISSION " + json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError("UNKNOWN_EXPECTED_INPUT: duplicate key " + key)
        result[key] = value
    return result


def canonical(value):
    path = Path(value)
    assert path.is_absolute() and str(path.resolve()) == value, "UNKNOWN_PATH: noncanonical"
    assert not any(p.is_symlink() for p in (path, *path.parents)), "UNKNOWN_PATH: symlink"
    return path


def digest(path):
    return hashlib.sha256(canonical(str(path)).read_bytes()).hexdigest()


def require_input(env):
    """Strict input channel, also used by the finite ordinary in-process controls."""
    assert not {k for k in env if k.startswith("ISSUE25_")} - _CONTROL_KEYS, "UNKNOWN_CONTROL_KEY"
    assert not {"PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD"}.intersection(env), "UNKNOWN_CHILD_ENVIRONMENT: forbidden override"
    mode = env.get("ISSUE25_MODE")
    assert mode in ("admission", "pack"), "UNKNOWN_MODE"
    assert env.get("ISSUE25_BASE") in BASES, "UNKNOWN_BASE"
    ref = env.get("GITHUB_REF")
    assert ref in REFS and REFS[ref][1] == mode, "UNKNOWN_MODE_REF"
    assert env.get("GITHUB_EVENT_NAME") == "push", "UNKNOWN_EVENT"
    assert env.get("GITHUB_RUN_ATTEMPT") == "1", "UNKNOWN_ATTEMPT"
    assert re.fullmatch(r"[1-9][0-9]*", env.get("GITHUB_RUN_ID", "")), "UNKNOWN_RUN_ID"
    path = canonical(env.get("ISSUE25_EXPECTED", ""))
    raw = path.read_bytes()
    assert re.fullmatch(r"[0-9a-f]{64}", env.get("ISSUE25_EXPECTED_SHA256", "")), "UNKNOWN_EXPECTED_DIGEST"
    assert hashlib.sha256(raw).hexdigest() == env["ISSUE25_EXPECTED_SHA256"], "UNKNOWN_EXPECTED_DIGEST"
    data = json.loads(raw, object_pairs_hook=unique_object)
    assert type(data) is dict and set(data) == _FIELDS, "UNKNOWN_EXPECTED_SCHEMA"
    assert type(data["schema_version"]) is int and data["schema_version"] == 1, "UNKNOWN_EXPECTED_SCHEMA"
    for key, name in (("mode", "ISSUE25_MODE"), ("base", "ISSUE25_BASE"),
                      ("case_id", "ISSUE25_CASE_ID"), ("ref", "GITHUB_REF"),
                      ("event", "GITHUB_EVENT_NAME"), ("run_id", "GITHUB_RUN_ID"),
                      ("attempt", "GITHUB_RUN_ATTEMPT"), ("source_commit", "GITHUB_SHA")):
        assert data[key] == env.get(name), "UNKNOWN_EXPECTED_BINDING: " + key
    assert data["stage"] == REFS[ref][0], "UNKNOWN_STAGE"
    assert (data["base_commit"], data["base_tree"]) == BASES[data["base"]], "UNKNOWN_BASE_BINDING"
    case = data["case_id"]
    if mode == "pack":
        assert case == data["base"] and "ISSUE25_INJECTION" not in env, "UNKNOWN_INJECTION"
        assert data["injection"] is None and data["items"] == PACK_ITEMS, "UNKNOWN_ITEM_CONTRACT"
    else:
        rows = {"P-release": ("released", None), "P-main": ("main", None),
                "N-missing": ("released", "missing"), "N-origin": ("released", "origin"),
                "N-config": ("released", "config"), "N-fixture": ("released", "fixture")}
        assert case in rows and rows[case] == (data["base"], data["injection"]), "UNKNOWN_CASE"
        assert data["items"] == ADMISSION_ITEMS, "UNKNOWN_ITEM_CONTRACT"
        if data["injection"] is None:
            assert "ISSUE25_INJECTION" not in env, "UNKNOWN_INJECTION"
        else:
            assert env.get("ISSUE25_INJECTION") == data["injection"], "UNKNOWN_INJECTION"
    assert data["deadline"] == ("60s" if mode == "admission" else "180s"), "UNKNOWN_DEADLINE"
    assert data["phases"] == ["setup", "call", "teardown"], "UNKNOWN_PHASE_CONTRACT"
    export = canonical(data["export"])
    assert export == path.parent / "export", "UNKNOWN_EXPORT"
    assert canonical(data["expected_config"]) == export / "tools/issue25/pytest.ini", "UNKNOWN_CONFIG_PATH"
    assert canonical(data["temp"]) == path.parent / "tmp", "UNKNOWN_TEMP"
    assert data["config_sha256"] == hashlib.sha256(CONFIG_BYTES).hexdigest(), "UNKNOWN_CONFIG_BYTES"
    for name in ("base_manifest", "product_manifest", "product_paths", "fixture_manifest", "inventory"):
        record = data[name]
        assert set(record) == {"path", "sha256"}, "UNKNOWN_MANIFEST_SCHEMA"
        assert digest(record["path"]) == record["sha256"], "UNKNOWN_MANIFEST_DIGEST: " + name
    wanted_env = {
        "PATH": str(Path(data["python"]).parent) + ":/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "TZ": "UTC", "TMPDIR": data["temp"], "PYTHONDONTWRITEBYTECODE": "1",
        "ISSUE25_MODE": data["mode"], "ISSUE25_BASE": data["base"], "ISSUE25_CASE_ID": data["case_id"],
        "ISSUE25_EXPECTED": str(path), "ISSUE25_EXPECTED_SHA256": hashlib.sha256(raw).hexdigest(),
        "GITHUB_REPOSITORY": "dkpnw/ha-mxz-coordinator", "GITHUB_EVENT_NAME": "push", "GITHUB_REF": data["ref"],
        "GITHUB_HEAD_REF": "", "GITHUB_BASE_REF": "", "GITHUB_RUN_ID": data["run_id"], "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": data["source_commit"], "REPOSITORY_PRIVATE": "false",
        "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_ARCH": "X64",
    }
    if data["injection"] is not None:
        wanted_env["ISSUE25_INJECTION"] = data["injection"]
    # pack.sh and V10 bind the complete forwarded map. Dependencies may add
    # metadata (pytest adds PYTEST_VERSION) after that process boundary.
    assert all(env.get(key) == value for key, value in wanted_env.items()), "UNKNOWN_CHILD_ENVIRONMENT"
    return data


def module_snapshot(module):
    """Only existing dictionaries: never import a parent or use module __getattr__."""
    if module is None:
        return None
    namespace = vars(module)
    spec = namespace.get("__spec__")
    spec_data = vars(spec) if spec is not None else {}
    locations = spec_data.get("submodule_search_locations")
    return {"file": namespace.get("__file__"), "path": list(namespace.get("__path__", [])),
            "spec": None if spec is None else {"origin": spec_data.get("origin"),
                                                  "locations": None if locations is None else list(locations)}}


def observe(config, stage, **extra):
    loaded = {name: module_snapshot(module) for name, module in tuple(sys.modules.items())
              if name == "custom_components.mxz_coordinator" or name.startswith("custom_components.mxz_coordinator.")}
    plugin_records = []
    registered = config.pluginmanager.list_name_plugin() if stage == "configure" else []
    assert len(registered) <= 128, "UNKNOWN_PLUGIN_CARDINALITY"
    for name, plugin in registered:
        if plugin is None:
            plugin_records.append({"name": name, "file": None, "sha256": None})
            continue
        path = vars(plugin).get("__file__")
        plugin_records.append({"name": name, "file": path,
                               "sha256": digest(path) if path is not None else None})
    switch = sys.modules.get("custom_components.mxz_coordinator.switch")
    cls = vars(switch).get("MXZZoneFanAutoSwitch") if switch is not None else None
    emit("observation", stage=stage, root=str(config.rootpath),
         inipath=None if config.inipath is None else str(config.inipath),
         sys_path=list(sys.path),
         parent=module_snapshot(sys.modules.get("custom_components")),
         component=module_snapshot(sys.modules.get("custom_components.mxz_coordinator")),
         loaded=loaded, class_identity=id(cls) if cls is not None else None,
         plugins=plugin_records if stage == "configure" else None, **extra)


def require_configuration(config, data):
    assert str(config.rootpath) == data["export"], "UNKNOWN_ROOT_PATH"
    if config.inipath is None or str(config.inipath) != data["expected_config"]:
        raise pytest.UsageError("UNKNOWN_CONFIG_PATH: " + str(config.inipath))
    assert digest(data["expected_config"]) == data["config_sha256"], "UNKNOWN_CONFIG_BYTES"
    assert config.pluginmanager.hasplugin("tools.pytest_phases"), "UNKNOWN_PHASE_PLUGIN"
    assert config.getoption("importmode") == "prepend", "UNKNOWN_IMPORT_MODE"
    assert config.getoption("asyncio_mode") == "auto", "UNKNOWN_ASYNCIO_MODE"
    assert sys.executable == data["python"], "UNKNOWN_PYTHON_EXECUTABLE"


def require_collection_origin(data, harness, modules=None):
    """Check canonical origins and Git-bound bytes independently of import success."""
    modules = sys.modules if modules is None else modules
    expected = Path(data["export"])
    parent = module_snapshot(modules.get("custom_components"))
    assert parent is not None and parent["spec"] is not None, "UNKNOWN_COLLECTION_ORIGIN: parent/spec absent"
    locations = parent["spec"]["locations"]
    assert locations and str(expected / "custom_components") in locations, "UNKNOWN_COLLECTION_ORIGIN: parent locations"
    assert all(canonical(p) == expected / "custom_components" for p in locations), "UNKNOWN_COLLECTION_ORIGIN: foreign parent"
    assert modules.get("custom_components.mxz_coordinator") is not None, "UNKNOWN_COLLECTION_ORIGIN: component absent"
    component = module_snapshot(modules["custom_components.mxz_coordinator"])
    assert component["spec"] is not None and component["spec"]["locations"], "UNKNOWN_COLLECTION_ORIGIN: component spec"
    assert all(canonical(p) == expected / "custom_components/mxz_coordinator" for p in component["spec"]["locations"]), (
        f"UNKNOWN_COLLECTION_ORIGIN: expected={expected / 'custom_components/mxz_coordinator'} actual={component['spec']['locations']}")
    wanted_class = vars(harness).get("MXZZoneFanAutoSwitch")
    switch = modules.get("custom_components.mxz_coordinator.switch")
    assert switch is not None and vars(switch).get("MXZZoneFanAutoSwitch") is wanted_class and wanted_class is not None, (
        "UNKNOWN_COLLECTION_ORIGIN: class identity")
    manifest = canonical(data["product_manifest"]["path"]).read_text().splitlines()
    blobs = {line.split(None, 3)[3]: line.split(None, 3)[2] for line in manifest}
    for name, module in tuple(modules.items()):
        if name != "custom_components.mxz_coordinator" and not name.startswith("custom_components.mxz_coordinator."):
            continue
        snapshot = module_snapshot(module)
        actual = snapshot["file"] if snapshot else None
        assert actual is not None, "UNKNOWN_COLLECTION_ORIGIN: module file absent " + name
        path = canonical(actual)
        assert path.is_relative_to(expected), f"UNKNOWN_COLLECTION_ORIGIN: expected={expected} actual={path}"
        relative = str(path.relative_to(expected))
        raw = path.read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        assert relative in blobs and blob == blobs[relative], "UNKNOWN_COLLECTION_ORIGIN: changed bytes " + relative
    return wanted_class


def require_collection(items, failures, errors, mode):
    expected = ADMISSION_ITEMS if mode == "admission" else PACK_ITEMS
    if failures or errors or items != expected:
        raise pytest.UsageError(f"UNKNOWN_COLLECTION_CONTRACT: failures={failures} errors={errors} items={items!r}")


def fixture_record(fixturedef):
    assert fixturedef is not None, "UNKNOWN_FIXTURE_DEFINITION: missing fixture"
    function = fixturedef.func
    # Fixture wrappers are explicitly unwrapped via their dictionary, never executed.
    while "__wrapped__" in vars(function):
        function = vars(function)["__wrapped__"]
    path = str(Path(function.__code__.co_filename).absolute())
    return {"name": fixturedef.argname, "symbol": function.__name__, "path": path,
            "sha256": digest(path), "dependencies": list(fixturedef.argnames),
            "baseid": fixturedef.baseid}, function


def require_fixture(record, function, data, harness):
    if record["name"] == "trial":
        expected = vars(harness)["trial"]
        while "__wrapped__" in vars(expected):
            expected = vars(expected)["__wrapped__"]
        assert function is expected, "UNKNOWN_FIXTURE_DEFINITION: trial identity"
        assert record["path"] == data["export"] + "/tools/issue25/test_restore.py", "UNKNOWN_FIXTURE_DEFINITION: trial path"
        return
    files = json.loads(Path(data["fixture_manifest"]["path"]).read_bytes(), object_pairs_hook=unique_object)
    matching = [row for row in files["files"] if row["path"] == record["path"]
                and row["sha256"] == record["sha256"]]
    assert len(matching) == 1, "UNKNOWN_FIXTURE_DEFINITION: absent distribution membership"
    if record["name"] == "enable_custom_integrations":
        assert matching[0]["distribution"] == "pytest-homeassistant-custom-component", "UNKNOWN_FIXTURE_DEFINITION: wrong distribution"
        assert record["symbol"] == "enable_custom_integrations", "UNKNOWN_FIXTURE_DEFINITION: wrong symbol"
        assert record["path"] in files["plugin_files"], "UNKNOWN_FIXTURE_DEFINITION: wrong entry point"


class MissingComponent:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "custom_components.mxz_coordinator" or fullname.startswith("custom_components.mxz_coordinator."):
            raise ModuleNotFoundError("No module named 'custom_components.mxz_coordinator'", name="custom_components.mxz_coordinator")


class DecoyComponent:
    def __init__(self, parent):
        self.parent = parent

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "custom_components.mxz_coordinator":
            return importlib.machinery.PathFinder.find_spec(fullname, [self.parent])
        return None


class WrongFixture:
    @pytest.fixture
    def enable_custom_integrations(self):
        return None


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    _REPORTS.clear()
    observe(config, "configure", requested_injection=os.environ.get("ISSUE25_INJECTION"))
    data = require_input(os.environ)
    config._issue25_expected = data
    config._issue25_fixture_order = []
    config._issue25_runtime_origin = runtime_origin
    config._issue25_ha_origin = require_ha_origin
    require_configuration(config, data)
    injection = data["injection"]
    emit("injection", value=injection)
    if injection in ("missing", "origin"):
        assert "custom_components.mxz_coordinator" not in sys.modules, "INVALID_CONTROL: component already loaded"
        finder = MissingComponent() if injection == "missing" else DecoyComponent(str(Path(data["fault_paths"]["decoy"]).parent))
        sys.meta_path.insert(0, finder)
        config.add_cleanup(lambda: sys.meta_path.remove(finder))
    elif injection == "fixture":
        config.pluginmanager.register(WrongFixture(), "issue25-wrong-fixture")


def selected(collector):
    return isinstance(collector, pytest.Module) and str(collector.path) == (
        collector.config._issue25_expected["export"] + "/" +
        ("tools/issue25/test_setup_admission.py" if collector.config._issue25_expected["mode"] == "admission"
         else "tools/issue25/test_restore.py"))


def pytest_collectstart(collector):
    if selected(collector):
        observe(collector.config, "pre-import")


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makeitem(collector, name, obj):
    if selected(collector) and not getattr(collector, "_issue25_origin_checked", False):
        module = collector.obj
        harness = vars(module).get("restore_harness", module)
        observe(collector.config, "post-import")
        require_collection_origin(collector.config._issue25_expected, harness)
        collector.config._issue25_harness = harness
        collector._issue25_origin_checked = True


def retain_report(report, reports):
    if report.failed:
        record = {"node": report.nodeid, "longrepr": str(report.longrepr), "ordinal": len(reports)}
        reports.append(record)
        emit("collect-report", **record)


def pytest_collectreport(report):
    retain_report(report, _REPORTS)


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session):
    require_collection([item.nodeid for item in session.items], len(_REPORTS), session.testsfailed,
                       session.config._issue25_expected["mode"])


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):
    # Terminal progress (notably setup's E) has no trailing newline. This inner
    # wrapper finishes that line before the unchanged phase wrapper emits.
    yield
    print(flush=True)


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname in ("hass", "hass_storage", "enable_custom_integrations", "trial"):
        record, function = fixture_record(fixturedef)
        request.config._issue25_fixture_order.append(record["name"])
        observe(request.config, "fixture", fixture=record, order=list(request.config._issue25_fixture_order))
        require_fixture(record, function, request.config._issue25_expected, request.config._issue25_harness)


def runtime_origin(request, stage, observer=None):
    config = request.config
    observe(config, stage)
    current = dict(os.environ)
    phase = current.pop("PYTEST_CURRENT_TEST", None)
    assert phase in {request.node.nodeid + " (" + name + ")" for name in ("setup", "call", "teardown")}, (
        "UNKNOWN_PYTEST_CURRENT_TEST")
    data = require_input(current)
    cls = require_collection_origin(data, config._issue25_harness)
    if observer is not None:
        adds = [call for call in observer.calls if call["kind"] == "add"]
        assert len(adds) == 2 and all(call["complete"] and call["exception"] is None for call in adds), "UNKNOWN_ADD_COMPLETION"
        assert all(type(call["entity"]) is cls for call in adds), "UNKNOWN_HA_CLASS_IDENTITY"
    return cls


def require_ha_origin(data, loader_path, entity_class, expected_class, calls):
    assert loader_path is not None, "UNKNOWN_HA_LOADER_IDENTITY"
    expected = Path(data["export"]) / "custom_components/mxz_coordinator"
    assert canonical(str(loader_path)) == expected, "UNKNOWN_HA_LOADER_IDENTITY"
    assert entity_class is expected_class, "UNKNOWN_HA_CLASS_IDENTITY"
    assert len(calls) == 2 and all(c["complete"] and c["exception"] is None for c in calls), "UNKNOWN_ADD_COMPLETION"
