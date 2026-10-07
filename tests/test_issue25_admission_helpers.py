"""Actual admission helpers, with no HA or lifecycle-oracle imports."""

from types import SimpleNamespace

import pytest


# D rows import only the diagnostic helper module (stdlib + pytest). Hooks are
# never registered by this import, and no product/dependency probe is performed.
@pytest.mark.parametrize('case', range(1, 56), ids=[f'D{i:02d}' for i in range(1, 56)])
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
                             pluginmanager=SimpleNamespace(hasplugin=lambda name: True, list_name_plugin=list),
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
    if case in (53, 54, 55):
        import os

        # pytest.main sets this before configuration; use the running dependency's
        # actual metadata without starting another pytest or importing HA.
        assert os.environ['PYTEST_VERSION'] == pytest.__version__
        env['PYTEST_VERSION'] = os.environ['PYTEST_VERSION']
        env['INVENTED_DEPENDENCY_METADATA'] = 'benign'
        assert admission.require_input(env) == data
        if case == 54:
            for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS',
                        'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'ISSUE25_UNKNOWN', 'ISSUE25_INJECTION'):
                for value in ('', 'invented'):
                    with pytest.raises(AssertionError, match='UNKNOWN'):
                        admission.require_input({**env, key: value})
        elif case == 55:
            for key in env.keys() - {'PYTEST_VERSION', 'INVENTED_DEPENDENCY_METADATA'}:
                missing = dict(env)
                del missing[key]
                with pytest.raises(AssertionError, match='UNKNOWN'):
                    admission.require_input(missing)
                with pytest.raises(AssertionError, match='UNKNOWN'):
                    admission.require_input({**env, key: 'changed-required-input'})
        return
    target = partial(admission.require_input, env)
    positive = case in (41, 42, 43, 44, 45, 46, 49, 52)
    if case in (1, 2, 3):
        if case == 1:
            del env['ISSUE25_MODE']
        else:
            env['ISSUE25_MODE'] = '' if case == 2 else 'unknown'
    elif case in (4, 5, 6):
        env['ISSUE25_MODE'] = 'pack' if case == 4 else 'admission'
        env['GITHUB_REF'] = 'refs/heads/ci/issue25-harness-' + {4: 'admission', 5: 'replacement', 6: 'repeat'}[case]
    elif case in (7, 8):
        if case == 7:
            del env['ISSUE25_BASE']
        else:
            env['ISSUE25_BASE'] = 'unknown'
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
        else:
            p.write_text(p.read_text() + ' ')
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
        else:
            config.pluginmanager.hasplugin = lambda name: False
        target = partial(admission.require_configuration, config, data)
    elif case == 22:
        target = partial(admission.fixture_record, None)
    elif case in (23, 24):
        if case == 23:
            record['symbol'] = 'no_op'
        else:
            fixture_path.write_text('{"files":[],"plugin_files":[]}')
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


@pytest.mark.parametrize('revert_flush', [False, True], ids=['D56', 'D57'])
def test_admission_record_shared_stream(tmp_path, monkeypatch, revert_flush):
    import ast
    import inspect
    import io
    import json
    import os
    import sys
    from types import CodeType, FunctionType

    from tools.issue25 import conftest as admission

    emitter = admission.emit
    if revert_flush:
        # Execute the actual emitter with just its flush keyword removed. This
        # is the prior behavior, not a second hand-written idealized emitter.
        source = ast.parse(inspect.getsource(emitter))
        for node in ast.walk(source):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'print':
                node.keywords = [kw for kw in node.keywords if kw.arg != 'flush']
        code = compile(source, '<reverted-emitter-flush>', 'exec')
        emitter = FunctionType(next(part for part in code.co_consts if isinstance(part, CodeType)), vars(admission))

    error = b'ERROR: independent stderr-style write\n'

    def require_integrity(captured, payload):
        lines = captured.splitlines(keepends=True)
        assert len(lines) == 2
        assert lines[0].startswith(b'ADMISSION ') and lines[0].endswith(b'\n')
        assert json.loads(lines[0][len(b'ADMISSION '):]) == {'event': 'observation', 'detail': payload}
        assert lines[1] == error

    for size in (16384, 128):
        payload = 'x' * size
        path = tmp_path / f'shared-{size}.log'
        with (
            path.open('w+b', buffering=0) as raw,
            io.TextIOWrapper(io.BufferedWriter(raw, buffer_size=8192), encoding='utf-8',
                             line_buffering=False, write_through=False) as output,
        ):
            with monkeypatch.context() as patch:
                patch.setattr(sys, 'stdout', output)
                emitter('observation', detail=payload)
            # Like 2>&1, this bypasses stdout's buffer but shares its offset.
            assert os.write(raw.fileno(), error) == len(error)
            output.flush()
        captured = path.read_bytes()

        if revert_flush:
            with pytest.raises((AssertionError, json.JSONDecodeError)):
                require_integrity(captured, payload)
        else:
            require_integrity(captured, payload)


@pytest.mark.parametrize(('when', 'outcome', 'progress'), [
    ('setup', 'failed', 'test.py '),
    ('call', 'passed', 'test.py '),
    ('teardown', 'passed', 'E'),
    ('teardown', 'failed', '.'),
], ids=['setup-error', 'call-pass', 'teardown-pass', 'teardown-error'])
def test_diagnostic_phase_line_boundary(tmp_path, monkeypatch, when, outcome, progress):
    import io
    import json
    import sys
    from types import ModuleType

    from tools import pytest_phases
    from tools.issue25 import conftest as admission

    node = 'issue25/test_invented.py::test_boundary'
    report = pytest.TestReport(node, ('test_invented.py', 0, 'test_boundary'), {},
                               outcome, 'invented failure' if outcome == 'failed' else None,
                               when, duration=0.25)

    class ReportProvider:
        def pytest_runtest_makereport(self, item, call):
            return report

    for separator in (False, True):
        manager = pytest.PytestPluginManager()
        manager.register(pytest_phases)
        manager.register(ReportProvider())
        if separator:
            plugin = ModuleType('diagnostic_boundary')
            plugin.pytest_runtest_makereport = admission.pytest_runtest_makereport
            manager.register(plugin)
        config = SimpleNamespace(option=SimpleNamespace())
        pytest_phases.pytest_configure(config)
        session = SimpleNamespace(shouldstop=False)
        item = SimpleNamespace(config=config, session=session, nodeid=node,
                               path=tmp_path / 'issue25/test_invented.py')
        call = SimpleNamespace(when=when, excinfo=None)
        path = tmp_path / f'boundary-{separator}.log'
        with (
            path.open('w+b', buffering=0) as raw,
            io.TextIOWrapper(io.BufferedWriter(raw), encoding='utf-8') as output,
            monkeypatch.context() as patch,
        ):
            patch.setattr(sys, 'stdout', output)
            output.write(progress)
            returned = manager.hook.pytest_runtest_makereport(item=item, call=call)
            assert returned is report
            # The boundary flush must reach the real file even while the phase
            # plugin's subsequent print is still buffered.
            expected_prefix = (progress + '\n').encode() if separator else b''
            assert path.read_bytes() == expected_prefix
        lines = path.read_text().splitlines()
        phases = [json.loads(line.removeprefix('PHASE ')) for line in lines if line.startswith('PHASE ')]
        expected = {'node': node, 'phase': when, 'outcome': outcome,
                    'category': 'unknown' if outcome == 'failed' else 'green', 'seconds': 0.25}
        # Removing just the separator reproduces the parent's lost record.
        assert phases == ([expected] if separator else [])
        assert config._ordinary_reports == {node: [(when, outcome, False)]}
        assert bool(session.shouldstop) == (outcome == 'failed')


def external_method(class_name, method_name):
    """Read only the named harness method; no HA/product/module imports."""
    import ast
    from pathlib import Path

    path = Path(__file__).parents[1] / 'tools/issue25/test_restore.py'
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    return next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == method_name)


def external_callable(method, namespace):
    import ast
    from types import CodeType, FunctionType

    code = compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                   '<external-harness-contract>', 'exec')
    body = next(part for part in code.co_consts if isinstance(part, CodeType))
    defaults = tuple(ast.literal_eval(value) for value in method.args.defaults)
    return FunctionType(body, namespace, argdefs=defaults)


def observe_external_shutdown(method, switch_state='unavailable'):
    """Record stimulus up to serialization; never model coordinator/HA behavior.

    The switch input is an independent observation supplied by this control.
    No platform, coordinator, store, restore getter or product oracle runs here.
    """
    import asyncio

    rows = {'head_a': object(), 'head_b': object(),
            'switch_a': SimpleNamespace(state=switch_state),
            'switch_b': SimpleNamespace(state=switch_state)}
    original_rows = dict(rows)
    observations = []
    heads = []
    for entity_id in ('head_a', 'head_b'):
        h = SimpleNamespace(entity_id=entity_id, fail_temperature=False,
                            _attr_target_temperature_high=70, _attr_fan_mode='high',
                            pending=['old-auto'])
        h.async_write_ha_state = lambda h=h: observations.append(
            ('publish', h.entity_id, h.fail_temperature, h._attr_target_temperature_high))
        heads.append(h)

    async def refresh():
        observations.append(('refresh', heads[1].fail_temperature,
                             heads[1]._attr_target_temperature_high))

    async def drained():
        observations.append(('drain',))

    async def origin(stage):
        observations.append(('origin', stage))

    class SerializationBoundary(Exception):
        pass

    async def stop_before_serialization(hass):
        raise SerializationBoundary

    trial = SimpleNamespace(
        heads=heads, entry=SimpleNamespace(runtime_data=object(), entry_id='same-entry', created_at=1),
        cycle=0, unknown=[], switch=lambda i: ('switch_a', 'switch_b')[i],
        snapshot=lambda label: observations.append(('snapshot', label)),
        origin_guard=origin, refresh=refresh,
        hass=SimpleNamespace(states=SimpleNamespace(get=rows.get, async_remove=rows.pop),
                             async_block_till_done=drained))
    restart = external_callable(method, {'async_mock_restore_state_shutdown_restart': stop_before_serialization})
    with pytest.raises(SerializationBoundary):
        asyncio.run(restart(trial, unavailable=True))
    return trial, rows, original_rows, observations


@pytest.mark.parametrize('mutation', ['none', 'remove-head-states', 'omit-external-fault'])
def test_external_fault_survives_to_serialization(mutation):
    import ast

    method = external_method('Trial', 'restart')
    fault = next(n for n in method.body if isinstance(n, ast.If)
                 and isinstance(n.test, ast.Name) and n.test.id == 'unavailable')
    if mutation == 'remove-head-states':
        # The actual R2 regression: state deletion leaves registered handlers alive.
        fault.body.extend(ast.parse('for h in self.heads:\n self.hass.states.async_remove(h.entity_id)').body)
    elif mutation == 'omit-external-fault':
        fault.body = [n for n in fault.body if not (
            isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Attribute)
            and n.targets[0].attr == 'fail_temperature')]
    trial, rows, original, observations = observe_external_shutdown(method)

    def contract():
        # Require the same live rows, persistent fault, unchanged fan reports and
        # queues. A missing row or bypassed handler cannot meet this contract.
        assert rows == original, 'external Head disappeared'
        assert trial.heads[1].fail_temperature, 'external fault absent'
        assert not trial.heads[0].fail_temperature
        assert trial.heads[1]._attr_target_temperature_high is None
        assert ('publish', 'head_b', True, None) in observations
        assert ('refresh', True, None) in observations
        assert observations.index(('publish', 'head_b', True, None)) < observations.index(('refresh', True, None))
        assert all(h._attr_fan_mode == 'high' and h.pending == ['old-auto'] for h in trial.heads)
        assert trial.unknown == []

    if mutation == 'none':
        contract()
    else:
        with pytest.raises(AssertionError, match='external Head disappeared|external fault absent'):
            contract()


@pytest.mark.parametrize('state', ['on', 'off'])
def test_unreached_external_fault_retains_unknown(state):
    trial, _, _, _ = observe_external_shutdown(external_method('Trial', 'restart'), state)
    assert trial.unknown == ['cycle1/room0: unavailable shutdown unreached',
                             'cycle1/room1: unavailable shutdown unreached']


def test_external_fault_recovery_after_load_before_setup():
    import ast
    import asyncio
    from copy import deepcopy

    method = external_method('Trial', 'restart')
    recovery = next(n for n in method.body if isinstance(n, ast.For)
                    and isinstance(n.iter, ast.Attribute) and n.iter.attr == 'heads')

    def position(call_name):
        return next(i for i, statement in enumerate(method.body) if any(
            isinstance(n, ast.Call) and (
                isinstance(n.func, ast.Attribute) and n.func.attr == call_name
                or isinstance(n.func, ast.Name) and n.func.id == call_name)
            for n in ast.walk(statement)))

    assert (position('async_mock_restore_state_shutdown_restart') < position('async_unload')
            < position('load_after_removal') < method.body.index(recovery) < position('async_setup'))
    for omit_recovery in (False, True):
        fragment = deepcopy(method)
        fragment.body = [deepcopy(recovery)]
        if omit_recovery:
            fragment.body[0].body = [n for n in fragment.body[0].body if not isinstance(n, ast.Assign)]
        published = []
        heads = [SimpleNamespace(fail_temperature=True, _attr_fan_mode='high', pending=['old-auto'])
                 for _ in range(2)]
        for h in heads:
            h.async_write_ha_state = lambda h=h, published=published: published.append(h.fail_temperature)
        recover = external_callable(fragment, {})
        asyncio.run(recover(SimpleNamespace(heads=heads)))
        assert all(h._attr_fan_mode == 'high' and h.pending == ['old-auto'] for h in heads)
        if omit_recovery:
            with pytest.raises(AssertionError):
                assert published == [False, False]
        else:
            assert published == [False, False]
            assert all(not h.fail_temperature for h in heads)


def test_external_temperature_handler_fault_is_persistent_and_recoverable():
    import asyncio
    from copy import deepcopy
    from time import monotonic

    events, published = [], []
    deliver = external_callable(external_method('Head', 'deliver'), {
        'asyncio': asyncio, 'deepcopy': deepcopy, 'monotonic': monotonic,
        'trace': lambda event, **values: events.append((event, values))})
    head = SimpleNamespace(calls=[], entity_id='invented', fail_temperature=True,
                           delay_auto=False, pending=[], _attr_hvac_mode='cool',
                           _attr_fan_mode='high', _attr_target_temperature_low=68,
                           _attr_target_temperature_high=None,
                           async_write_ha_state=lambda: published.append('publish'))

    async def exercise():
        for _ in range(2):
            args = {'hvac_mode': 'cool', 'target_temp_low': 68, 'target_temp_high': 70,
                    'entity_id': ['invented']}
            with pytest.raises(RuntimeError, match='invented dependency failure'):
                await deliver(head, 'temperature', args)
            args['entity_id'].append('later-mutation')
        assert published == [] and head._attr_target_temperature_high is None
        assert all(c['args']['entity_id'] == ['invented'] and c['entered'].is_set()
                   and not c['returned'].is_set() and 'returned_at' not in c for c in head.calls)
        assert len({id(c[k]) for c in head.calls for k in ('entered', 'returned')}) == 4
        assert [event for event, _ in events] == ['handler-entry', 'handler-error'] * 2
        # A healthy fan command still works; the fault belongs to temperature.
        await deliver(head, 'fan', {'fan_mode': 'middle'})
        assert head._attr_fan_mode == 'middle' and len(published) == 1
        head.fail_temperature = False
        await deliver(head, 'temperature', {'hvac_mode': 'cool', 'target_temp_low': 68,
                                           'target_temp_high': 70})
        assert head._attr_target_temperature_high == 70 and len(published) == 2
        assert head.calls[-1]['returned'].is_set()
        assert head.calls[-1]['returned_at'] >= head.calls[-1]['at']

    asyncio.run(exercise())
