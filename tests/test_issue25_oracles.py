"""A setup-era or uncompleted barrier cannot certify later fan delivery."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tools.issue25.test_restore import completed_fan


def call(number=0, token="middle"):
    entered, returned = asyncio.Event(), asyncio.Event()
    entered.set()
    returned.set()
    return {"number": number, "service": "fan", "args": {"fan_mode": token},
            "entered": entered, "returned": returned, "at": 10, "returned_at": 11}


def test_new_handler_and_arguments_required():
    old, new = call(), call(1)
    assert completed_fan([old, new], 1, "middle")
    assert not completed_fan([old], 1, "middle")
    assert not completed_fan([old, new], 1, "high")
    new["returned"].clear()
    assert not completed_fan([old, new], 1, "middle")


def test_stale_gate_and_timestamp_reject():
    old, new = call(), call(1)
    new["entered"] = old["entered"]
    assert not completed_fan([old, new], 1, "middle")
    new = call(1)
    new["returned_at"] = 9
    assert not completed_fan([old, new], 1, "middle")
    new = call(1)
    new["returned"] = new["entered"]
    assert not completed_fan([old, new], 1, "middle")


def test_prerequisites_cannot_pass_when_absent_or_idle():
    from types import SimpleNamespace

    import pytest

    from tools.issue25.test_restore import require_state

    require_state(SimpleNamespace(state="cool"), "cool", "B cooling")
    require_state(SimpleNamespace(state="off"), "off", "manual OFF")
    for state in (None, "fan_only", "off", "unknown", "unavailable"):
        with pytest.raises(AssertionError, match="UNKNOWN"):
            require_state(None if state is None else SimpleNamespace(state=state), "cool", "B cooling")
    for state in (None, "on", "unknown", "unavailable"):
        with pytest.raises(AssertionError, match="UNKNOWN"):
            require_state(None if state is None else SimpleNamespace(state=state), "off", "manual OFF")


def test_restore_evidence_rejects_wrong_record_and_getter_substitution():
    from copy import deepcopy
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import pytest

    from tools.issue25.test_restore import require_restore_read, require_restore_record

    stamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    entity = "switch.invented_fan_auto"
    record = {"state": {"entity_id": entity, "state": "off", "last_updated": stamp.isoformat()},
              "extra_data": {"held": True}}
    loaded = SimpleNamespace(state=SimpleNamespace(entity_id=entity, state="off", last_updated=stamp),
                             extra_data=SimpleNamespace(as_dict=lambda: {"held": True}))
    require_restore_record(entity, loaded, record, created)
    require_restore_read(loaded.state, loaded.state, entity, "state")
    require_restore_read(None, None, entity, "missing-negative")
    with pytest.raises(AssertionError, match="UNKNOWN"):
        require_restore_read(deepcopy(loaded.state), loaded.state, entity, "state")
    with pytest.raises(AssertionError, match="UNKNOWN"):
        require_restore_read(None, loaded.state, entity, "state")
    with pytest.raises(AssertionError, match="UNKNOWN"):
        require_restore_record(entity, None, record, created)
    for field, value in (("entity_id", "switch.wrong"), ("state", "on"),
                         ("last_updated", "2025-12-01T00:00:00+00:00"),
                         ("last_updated", "2026-01-03T00:00:00+00:00")):
        wrong = deepcopy(record)
        wrong["state"][field] = value
        with pytest.raises(AssertionError, match="UNKNOWN"):
            require_restore_record(entity, loaded, wrong, created)
    wrong = deepcopy(record)
    wrong["extra_data"] = {"held": False}
    with pytest.raises(AssertionError, match="UNKNOWN"):
        require_restore_record(entity, loaded, wrong, created)


def test_changed_demand_requires_current_public_cooling_plan():
    from types import SimpleNamespace

    import pytest

    from tools.issue25.test_restore import require_cooling

    head = SimpleNamespace(state="cool")
    plan = SimpleNamespace(state="cool", attributes={"zones": [{}, {"engage": "cool"}]})
    require_cooling(head, plan, 1)
    for bad in (None, SimpleNamespace(state="unavailable"),
                SimpleNamespace(state="cool", attributes={}),
                SimpleNamespace(state="cool", attributes={"zones": []}),
                SimpleNamespace(state="cool", attributes={"zones": [{}, {}]}),
                SimpleNamespace(state="cool", attributes={"zones": [{}, {"engage": "satisfied"}]})):
        with pytest.raises(AssertionError, match="UNKNOWN"):
            require_cooling(head, bad, 1)
    with pytest.raises(AssertionError, match="UNKNOWN"):
        require_cooling(SimpleNamespace(state="fan_only"), plan, 1)


@pytest.mark.parametrize("case", [
    "exact", "no-extra", "absent-record", "copy-state", "copy-extra", "wrong-state", "wrong-extra",
    "missing-state", "missing-extra", "duplicate-state", "unexpected-extra", "getter-error",
    "add-error", "extra-error", "incomplete-add", "missing-add", "duplicate-add",
    "ownership-precedence", "mixed-entity",
])
async def test_transparent_lifecycle_observers_validate_after_caller(case):
    from tools.issue25.test_restore import LifecycleReads, Trial

    entity = SimpleNamespace(entity_id="switch.invented")
    loaded = SimpleNamespace(state={"state": "unavailable"}, extra_data={"token": 1})
    absent = case == "absent-record"
    supplied = {entity.entity_id: None if absent else loaded}
    expected_extra = set() if case == "no-extra" else {entity.entity_id}
    observer = LifecycleReads()
    exception = RuntimeError("invented original exception")
    trailing = []
    result_token = object()

    async def state_getter(entity):
        if case == "getter-error":
            raise exception
        if case == "copy-state":
            return deepcopy(loaded.state)
        if case == "wrong-state":
            return object()
        return None if absent else loaded.state

    async def extra_getter(entity):
        if case == "extra-error":
            raise exception
        if case == "copy-extra":
            return deepcopy(loaded.extra_data)
        if case == "wrong-extra":
            return object()
        return None if absent else loaded.extra_data

    async def add(entity):
        if case != "missing-state":
            await observer.wrap(state_getter, "state")(entity)
        if case == "duplicate-state":
            await observer.wrap(state_getter, "state")(entity)
        if case not in ("no-extra", "missing-extra"):
            other = SimpleNamespace(entity_id=entity.entity_id) if case == "mixed-entity" else entity
            await observer.wrap(extra_getter, "extra")(other)
        if case == "add-error":
            raise exception
        trailing.append("original caller completed")
        return result_token

    if case in ("getter-error", "extra-error", "add-error"):
        with pytest.raises(RuntimeError) as caught:
            await observer.wrap(add, "add")(entity)
        assert caught.value is exception
        assert not trailing
    else:
        assert await observer.wrap(add, "add")(entity) is result_token
        assert trailing == ["original caller completed"]
    if case == "unexpected-extra":
        expected_extra = set()
    if case == "missing-add":
        observer.calls = [c for c in observer.calls if c["kind"] != "add"]
    if case == "duplicate-add":
        observer.calls.append(next(c for c in observer.calls if c["kind"] == "add"))
    if case in ("incomplete-add", "ownership-precedence"):
        next(c for c in observer.calls if c["kind"] == "add")["complete"] = False
    if case in ("exact", "no-extra", "absent-record"):
        observer.validate(supplied, expected_extra)
    else:
        with pytest.raises(AssertionError, match="UNKNOWN"):
            # A previous ownership error cannot hide the invalid next lifecycle.
            trial = Trial(None, None, [], None)
            trial.errors.append("earlier ownership mismatch")
            observer.validate(supplied, expected_extra)
            trial.finish()


@pytest.mark.parametrize("zones", [None, {}, [], [None], [{}], [{"fan_hold": None}],
                                  [{"fan_hold": 0}], [{"fan_hold": "false"}]],
                         ids=["absent", "mapping", "empty", "no-room", "no-hold", "null-hold", "integer", "string"])
def test_missing_or_malformed_public_plan_is_unknown(zones):
    from tools.issue25.test_restore import public_hold

    with pytest.raises(AssertionError, match="UNKNOWN"):
        public_hold(SimpleNamespace(state="cool", attributes={"zones": zones}), 0)
    assert public_hold(SimpleNamespace(state="cool", attributes={"zones": [{"fan_hold": False}]}), 0) is False


@pytest.mark.parametrize("case", ["zero", "two", "wrong-entry", "one"])
def test_registry_identity_has_no_guessed_fallback(monkeypatch, case):
    from tools.issue25 import test_restore as restore

    rows = [] if case == "zero" else [SimpleNamespace(entity_id="switch.invented", unique_id="x_primary_fan_auto",
                                                     config_entry_id="other" if case == "wrong-entry" else "entry")]
    if case == "two":
        rows.append(SimpleNamespace(entity_id="switch.duplicate", unique_id="y_primary_fan_auto", config_entry_id="entry"))
    monkeypatch.setattr(restore.er, "async_get", lambda hass: SimpleNamespace(entities=dict(enumerate(rows))))
    trial = restore.Trial(None, None, [], SimpleNamespace(entry_id="entry"))
    if case == "one":
        assert trial.switch(0) == "switch.invented"
    else:
        with pytest.raises(AssertionError, match="UNKNOWN"):
            trial.switch(0)


@pytest.mark.parametrize("case", ["missing", "duplicate", "incomplete", "wrong-data", "old-record"])
async def test_post_removal_load_contract_rejects(case):
    from tools.issue25.test_restore import load_after_removal

    store = SimpleNamespace()
    old = object()
    restore = SimpleNamespace(store=store, last_states={"sensor.invented": old})
    raw = {"data": []}

    async def load():
        if case == "incomplete":
            raise RuntimeError("invented load failure")
        return [{}] if case == "wrong-data" else []

    async def reload():
        if case == "missing":
            return
        await store.async_load()
        if case == "duplicate":
            await store.async_load()

    store.async_load = load
    restore.async_load = reload
    with pytest.raises((AssertionError, RuntimeError)):
        await load_after_removal(restore, raw, ["sensor.invented"], None)


# D rows import only the diagnostic helper module (stdlib + pytest). Hooks are
# never registered by this import, and no product/dependency probe is performed.
@pytest.mark.parametrize('case', range(1, 53), ids=[f'D{i:02d}' for i in range(1, 53)])
def test_exported_admission_helper_contracts(tmp_path, case, capsys):
    import hashlib
    import json
    import sys
    from functools import partial
    from pathlib import Path
    from types import ModuleType

    from tests.test_issue25_pack import OVERLAY, expected_inputs, write_expected
    from tools.issue25 import conftest as admission

    root = tmp_path / 'source'
    child = tmp_path / 'issue25-A3-123-a1/P-release'
    export = child / 'export'
    root.mkdir()
    child.mkdir(parents=True)
    for name in (*OVERLAY, '.github/workflows/ci.yml', 'custom_components/mxz_coordinator/__init__.py',
                 'custom_components/mxz_coordinator/switch.py'):
        for parent in (root, export):
            path = parent / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# invented, never imported\n')
    inventory = child.parent / 'inventory.before.txt'
    inventory.write_text('invented inventory\n')
    data = expected_inputs(root, child, 'a' * 40, 'b' * 40, inventory, 'A3', 'P-release')
    data['base_commit'] = '3a9863896f8affb6f71cbd1e495df21b17a69ff3'
    data['base_tree'] = '238636de6d067c8e05b59993819cc5187c2bf9df'
    write_expected(child, data)
    Path(data['expected_config']).write_bytes(b'[pytest]\nasyncio_mode = auto\n')
    env = {'ISSUE25_MODE': 'admission', 'ISSUE25_BASE': 'released', 'ISSUE25_CASE_ID': 'P-release',
           'ISSUE25_EXPECTED': str(child / 'expected.json'),
           'ISSUE25_EXPECTED_SHA256': (child / 'expected.sha256').read_text().strip(),
           'GITHUB_REF': 'refs/heads/ci/issue25-harness-admission', 'GITHUB_EVENT_NAME': 'push',
           'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_ID': '123', 'GITHUB_SHA': 'a' * 40}
    config = SimpleNamespace(rootpath=export, inipath=Path(data['expected_config']),
                             pluginmanager=SimpleNamespace(hasplugin=lambda name: True, list_name_plugin=lambda: []),
                             getoption=lambda name: {'importmode': 'prepend', 'asyncio_mode': 'auto'}[name])
    modules = {}
    for name, filename, locations in (
        ('custom_components', None, [str(export / 'custom_components')]),
        ('custom_components.mxz_coordinator', str(export / 'custom_components/mxz_coordinator/__init__.py'),
         [str(export / 'custom_components/mxz_coordinator')]),
        ('custom_components.mxz_coordinator.switch', str(export / 'custom_components/mxz_coordinator/switch.py'), None),
    ):
        module = ModuleType(name)
        if filename is not None:
            module.__file__ = filename
        if locations is not None:
            module.__path__ = locations
        module.__spec__ = SimpleNamespace(origin=filename, submodule_search_locations=locations)
        modules[name] = module
    entity_class = type('InventedSwitch', (), {})
    modules['custom_components.mxz_coordinator.switch'].MXZZoneFanAutoSwitch = entity_class
    harness = ModuleType('invented_harness')
    harness.MXZZoneFanAutoSwitch = entity_class

    def genuine():
        return None

    defining = tmp_path / 'invented_plugin.py'
    defining.write_text('# invented pinned plugin member\n')
    record = {'name': 'enable_custom_integrations', 'symbol': 'enable_custom_integrations',
              'path': str(defining), 'sha256': hashlib.sha256(defining.read_bytes()).hexdigest(),
              'dependencies': [], 'baseid': ''}
    fixture = {'files': [{'path': str(defining), 'sha256': record['sha256'],
                          'distribution': 'pytest-homeassistant-custom-component'}],
               'plugin_files': [str(defining)]}
    fixture_path = Path(data['fixture_manifest']['path'])
    fixture_path.write_text(json.dumps(fixture))
    data['fixture_manifest']['sha256'] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    write_expected(child, data)
    env['ISSUE25_EXPECTED_SHA256'] = (child / 'expected.sha256').read_text().strip()
    env.update(PATH=str(Path(data['python']).parent) + ':/usr/bin:/bin', LANG='C.UTF-8', LC_ALL='C.UTF-8',
               TZ='UTC', TMPDIR=data['temp'], PYTHONDONTWRITEBYTECODE='1', GITHUB_HEAD_REF='', GITHUB_BASE_REF='',
               GITHUB_REPOSITORY='dkpnw/ha-mxz-coordinator', REPOSITORY_PRIVATE='false',
               RUNNER_ENVIRONMENT='github-hosted', RUNNER_ARCH='X64')
    target = partial(admission.require_input, env)
    positive = case in (41, 42, 43, 44, 45, 46, 49, 52)
    if case in (1, 2, 3):
        if case == 1:
            del env['ISSUE25_MODE']
        else: env['ISSUE25_MODE'] = '' if case == 2 else 'unknown'
    elif case in (4, 5, 6):
        env['ISSUE25_MODE'] = 'pack' if case == 4 else 'admission'
        env['GITHUB_REF'] = 'refs/heads/ci/issue25-harness-' + {4: 'admission', 5: 'replacement', 6: 'repeat'}[case]
    elif case in (7, 8):
        if case == 7:
            del env['ISSUE25_BASE']
        else: env['ISSUE25_BASE'] = 'unknown'
    elif case in (9, 10, 11, 12):
        key = {9: 'base_commit', 10: 'base_tree', 11: 'export', 12: 'case_id'}[case]
        data[key] = '0' * 40 if case in (9, 10) else str(tmp_path / 'wrong')
        write_expected(child, data)
        env['ISSUE25_EXPECTED_SHA256'] = (child / 'expected.sha256').read_text().strip()
    elif case in (13, 14, 15, 16):
        p = child / 'expected.json'
        if case == 13:
            del env['ISSUE25_EXPECTED']
        elif case == 14:
            p.write_text('{')
        elif case == 15:
            p.write_text('{"schema_version":1,"schema_version":1}')
        else: p.write_text(p.read_text() + ' ')
        if case in (14, 15):
            env['ISSUE25_EXPECTED_SHA256'] = hashlib.sha256(p.read_bytes()).hexdigest()
    elif case in (17, 18, 19, 20, 21):
        if case == 17:
            config.rootpath = None
        elif case == 18:
            config.rootpath = tmp_path
        elif case == 19:
            config.inipath = None
        elif case == 20:
            config.inipath = tmp_path / 'alternate.ini'
        else: config.pluginmanager.hasplugin = lambda name: False
        target = partial(admission.require_configuration, config, data)
    elif case == 22:
        target = partial(admission.fixture_record, None)
    elif case in (23, 24):
        if case == 23:
            record['symbol'] = 'no_op'
        else: fixture_path.write_text('{"files":[],"plugin_files":[]}')
        target = partial(admission.require_fixture, record, genuine, data, harness)
    elif 25 <= case <= 31 or case in (35, 44, 49):
        if case == 25:
            del modules['custom_components']
        elif case == 26:
            modules['custom_components'].__spec__ = None
        elif case == 27:
            modules['custom_components'].__spec__.submodule_search_locations = None
        elif case == 28:
            del modules['custom_components.mxz_coordinator']
        elif case in (29, 30):
            path = tmp_path / 'foreign.py'
            path.write_text('# different\n' if case == 29 else '# invented, never imported\n')
            modules['custom_components.mxz_coordinator.switch'].__file__ = str(path)
        elif case == 31:
            alias = tmp_path / 'alias.py'
            alias.symlink_to(export / 'custom_components/mxz_coordinator/switch.py')
            modules['custom_components.mxz_coordinator.switch'].__file__ = str(alias)
        elif case == 35:
            (export / 'custom_components/mxz_coordinator/switch.py').write_text('# drift\n')
        target = partial(admission.require_collection_origin, data, harness, modules)
    elif case in (32, 33, 34):
        calls = [{'complete': True, 'exception': None} for _ in range(2)]
        if case == 34:
            calls[0]['complete'] = False
        target = partial(admission.require_ha_origin, data, None if case == 32 else export / 'custom_components/mxz_coordinator', object if case == 33 else entity_class, entity_class, calls)
    elif case == 36:
        defining.unlink()
        target = partial(admission.digest, str(defining))
    elif case in (37, 38, 39, 40, 41, 42):
        items = admission.ADMISSION_ITEMS[:]
        failures, errors, mode = 0, 0, 'admission'
        if case in (37, 38):
            items = []
        if case in (38, 39):
            failures = errors = 1
        if case == 40:
            items = ['wrong']
        if case == 42:
            items = ['tools/issue25/test_restore.py::test_restore_schedule[' + n + ']' for n in (
            '01-clean-twice', '02-unavailable-twice', '03-manual-twin', '04-options-reload',
            '05-missing-restore-negative', '06-changed-active-demand', '07-provisional-auto-old-echo', '08-missing-speed-recovery')]
            mode = 'pack'
        target = partial(admission.require_collection, items, failures, errors, mode)
    elif case == 43:
        target = partial(admission.require_fixture, record, genuine, data, harness)
    elif case in (45, 46):
        original = "ModuleNotFoundError: No module named 'custom_components.mxz_coordinator'" if case == 45 else 'AssertionError: UNKNOWN_COLLECTION_ORIGIN'
        report = SimpleNamespace(failed=True, nodeid='invented', longrepr=original)
        reports = []
        admission.retain_report(report, reports)
        assert reports == [{'node': 'invented', 'ordinal': 0, 'longrepr': original}]
        assert report.longrepr == original
        return
    elif case == 47:
        del env['ISSUE25_BASE']
    elif case == 48:
        env['ISSUE25_EXTRA'] = 'injected'
    elif case == 50:
        config.inipath = None
        admission.observe(config, 'configure')
        assert '"inipath": null' in capsys.readouterr().out
        target = partial(admission.require_configuration, config, data)
    elif case == 51:
        record['symbol'] = 'no_op'
        admission.observe(config, 'fixture', fixture=record)
        assert 'no_op' in capsys.readouterr().out
        target = partial(admission.require_fixture, record, genuine, data, harness)
    elif case == 52:
        before = dict(sys.modules)
        admission.module_snapshot(modules['custom_components'])
        assert dict(sys.modules) == before
        return
    if positive:
        target()
    else:
        with pytest.raises((AssertionError, pytest.UsageError, ValueError, FileNotFoundError)):
            target()
