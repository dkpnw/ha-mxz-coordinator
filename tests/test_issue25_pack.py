"""Exercise actual pack gates with invented files and a non-HA Python stand-in."""

import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import zlib
from pathlib import Path

import pytest

PACK = Path(__file__).parents[1] / "tools/issue25/pack.sh"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip()


# Independent invented inputs. No candidate helper is run to construct expectations.
OVERLAY = ('tools/pytest_phases.py', 'tools/issue25/test_restore.py',
           'tools/issue25/conftest.py', 'tools/issue25/test_setup_admission.py')
PACK_IDS = ['tools/issue25/test_restore.py::test_restore_schedule[' + name + ']' for name in (
    '01-clean-twice', '02-unavailable-twice', '03-manual-twin', '04-options-reload',
    '05-missing-restore-negative', '06-changed-active-demand', '07-provisional-auto-old-echo',
    '08-missing-speed-recovery')]
STAGES = {'A3': 'admission', 'R2': 'replacement', 'I1': 'repeat'}


def blob(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def loose_repository(root, paths):
    """Write canonical Git loose objects in-process; zero setup subprocesses."""
    objects = root / '.git/objects'
    objects.mkdir(parents=True)
    (root / '.git/refs/heads').mkdir(parents=True)
    (root / '.git/HEAD').write_text('ref: refs/heads/invented\n')
    (root / '.git/config').write_text('[core]\nrepositoryformatversion = 0\nbare = false\n')

    def put(kind, raw):
        full = kind.encode() + b' ' + str(len(raw)).encode() + b'\0' + raw
        identity = hashlib.sha1(full).hexdigest()
        path = objects / identity[:2] / identity[2:]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(zlib.compress(full))
        return identity

    def tree(directory, members):
        entries = {}
        for name in members:
            first, _, rest = name.partition('/')
            entries.setdefault(first, []).append(rest)
        raw = b''
        for name in sorted(entries, key=lambda n: n + ('/' if entries[n] != [''] else '')):
            rest = entries[name]
            if rest == ['']:
                path = directory / name
                mode = b'100755' if path.stat().st_mode & 0o100 else b'100644'
                identity = put('blob', path.read_bytes())
            else:
                mode = b'40000'
                identity = tree(directory / name, rest)
            raw += mode + b' ' + name.encode() + b'\0' + bytes.fromhex(identity)
        return put('tree', raw)

    tree_id = tree(root, paths)
    commit = put('commit', (f'tree {tree_id}\nauthor Invented <invented@example.invalid> 1 +0000\n'
                            'committer Invented <invented@example.invalid> 1 +0000\n\ninvented\n').encode())
    (root / '.git/refs/heads/invented').write_text(commit + '\n')
    return commit, tree_id


def expected_inputs(root, child, commit, tree, inventory, stage='R2', case='released', base='released'):
    mode = 'admission' if stage == 'A3' else 'pack'
    ref = 'refs/heads/ci/issue25-harness-' + STAGES[stage]
    source_paths = sorted(str(p.relative_to(root)) for p in root.rglob('*')
                          if p.is_file() and '.git' not in p.relative_to(root).parts)
    manifest = ''.join(f'{"100755" if (root / p).stat().st_mode & 0o100 else "100644"} blob '
                       f'{blob((root / p).read_bytes())}\t{p}\n' for p in source_paths)
    (child / 'base.tree').write_text(manifest)
    product_lines = [line for line in manifest.splitlines(keepends=True) if '\tcustom_components/' in line]
    (child / 'product.tree').write_text(''.join(product_lines))
    (child / 'product.paths').write_text(''.join(line.split('\t')[1] for line in product_lines))
    export = child / 'export'
    overlay = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in OVERLAY}
    (child / 'harness.sha256').write_text(''.join(f'{sha}  {name}\n' for name, sha in overlay.items()))
    (child / 'fixture-files.json').write_text('{"files":[],"plugin_files":[],"entry_points":[]}\n')
    config = export / 'tools/issue25/pytest.ini'
    config_sha = hashlib.sha256(b'[pytest]\nasyncio_mode = auto\n').hexdigest()
    injection = case[2:] if case.startswith('N-') else None
    alternate = str(child / 'alternate/pytest.ini') if injection == 'config' else None
    (child / 'config.sha256').write_text(f'{config_sha}  {config}\n' + (f'{config_sha}  {alternate}\n' if alternate else ''))

    def record(path):
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    data = {'schema_version': 1, 'stage': stage, 'run_id': '123', 'attempt': '1', 'event': 'push',
            'ref': ref, 'source_commit': commit, 'source_tree': tree,
            'workflow_blob': blob((root / '.github/workflows/ci.yml').read_bytes()),
            'case_id': case, 'mode': mode, 'base': base, 'base_commit': commit, 'base_tree': tree,
            'checkout': str(root), 'export': str(export), 'temp': str(child / 'tmp'),
            'expected_config': str(config), 'actual_config': alternate or str(config),
            'python': str(child.parent.parent / 'mxz-venv/bin/python'),
            'base_manifest': record(child / 'base.tree'), 'product_manifest': record(child / 'product.tree'),
            'product_paths': record(child / 'product.paths'), 'overlay': overlay, 'config_sha256': config_sha,
            'fixture_manifest': record(child / 'fixture-files.json'), 'inventory': record(inventory),
            'items': ['tools/issue25/test_setup_admission.py::test_exported_setup'] if mode == 'admission' else PACK_IDS,
            'phases': ['setup', 'call', 'teardown'], 'deadline': '60s' if mode == 'admission' else '180s',
            'injection': injection, 'exception': {'missing': 'ModuleNotFoundError', 'origin': 'AssertionError',
                                                'config': 'UsageError', 'fixture': 'AssertionError'}.get(injection),
            'reason': {'missing': 'UNKNOWN_COLLECTION_CONTRACT', 'origin': 'UNKNOWN_COLLECTION_ORIGIN',
                       'config': 'UNKNOWN_CONFIG_PATH', 'fixture': 'UNKNOWN_FIXTURE_DEFINITION'}.get(injection),
            'exit': (2 if injection == 'fixture' else 4) if injection else (0 if mode == 'admission' else None),
            'fault_paths': {'decoy': str(child / 'decoy/custom_components/mxz_coordinator') if injection == 'origin' else None,
                            'alternate_config': alternate}}
    write_expected(child, data)
    return data


def write_expected(child, data):
    raw = (json.dumps(data, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (child / 'expected.json').write_bytes(raw)
    (child / 'expected.sha256').write_text(hashlib.sha256(raw).hexdigest() + '\n')


def invented_pack(tmp_path, real_git=False, mask='022', stage='R2', case='released', base='released'):
    root = tmp_path / 'source'
    root.mkdir()
    paths = ('custom_components/invented/__init__.py', 'custom_components/invented/executable',
             '.github/workflows/ci.yml', *OVERLAY)
    for name in paths:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# invented control input\n')
    (root / 'custom_components/invented/executable').chmod(0o755)
    if real_git:
        git(root, 'init', '-q')
        git(root, 'add', '.')
        git(root, '-c', 'user.name=Invented', '-c', 'user.email=invented@example.invalid', 'commit', '-qm', 'invented')
        commit, tree = git(root, 'rev-parse', 'HEAD'), git(root, 'rev-parse', 'HEAD^{tree}')
    else:
        commit, tree = loose_repository(root, paths)
    job = tmp_path / f'issue25-{stage}-123-a1'
    child = job / case
    target = child / 'export'
    target.mkdir(parents=True)
    if real_git:
        subprocess.run(['bash', '-euo', 'pipefail', '-c',
                        'umask "$1"; git -C "$2" archive "$3" | tar -x -C "$4"',
                        'export', mask, str(root), commit, str(target)],
                       check=True, capture_output=True, text=True, timeout=10)
    else:
        for name in paths:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, path)
    inventory = job / 'inventory.before.txt'
    inventory.write_text('invented inventory\n')
    data = expected_inputs(root, child, commit, tree, inventory, stage, case, base)
    if case == 'N-origin':
        shutil.copytree(target / 'custom_components', child / 'decoy/custom_components')
        (child / 'decoy.tree').write_bytes((child / 'product.tree').read_bytes())
    executable = tmp_path / 'mxz-venv/bin/python'
    executable.parent.mkdir(parents=True)
    controls = tmp_path / 'endpoint.json'
    controls.write_text('{}')
    marker = tmp_path / 'pytest-entered'
    capture = tmp_path / 'capture.json'
    executable.write_text('''#!/usr/bin/python3
import json
import os
import sys
from pathlib import Path
''' + f'controls = Path({str(controls)!r})\nmarker = Path({str(marker)!r})\ncapture = Path({str(capture)!r})\n' + '''settings = json.loads(controls.read_text())
fault = settings.get('fault')
if sys.argv[1:3] == ['-m', 'pytest']:
    marker.write_text('entered')
    capture.write_text(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd()}))
    path = Path('custom_components/invented/__init__.py')
    if fault == 'post-content': path.write_text('changed')
    if fault == 'post-path': path.with_name('extra.py').write_text('extra')
    if fault == 'post-mode': path.chmod(0o755)
    if fault == 'post-executable': path.with_name('executable').chmod(0o644)
    if fault == 'post-harness': Path('tools/issue25/test_restore.py').write_text('changed')
    print('PHASES_VALID=true')
    sys.exit(settings.get('exit', 0))
if len(sys.argv) > 1 and sys.argv[1].endswith('/tools/check_lock.py'):
    if fault == 'inventory-failure': sys.exit(7)
    if fault == 'inventory-mismatch' or (fault == 'post-inventory' and marker.exists()):
        print('changed')
    else: print('invented inventory')
    sys.exit(0)
os.execv('/usr/bin/python3', ['/usr/bin/python3', *sys.argv[1:]])
''')
    executable.chmod(0o755)
    env = {'PATH': os.defpath, 'RUNNER_TEMP': str(tmp_path), 'ISSUE25_MODE': data['mode'],
           'ISSUE25_CASE_ID': case, 'GITHUB_REF': data['ref'], 'GITHUB_EVENT_NAME': 'push',
           'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_ID': '123', 'GITHUB_SHA': commit,
           'GITHUB_REPOSITORY': 'dkpnw/ha-mxz-coordinator', 'REPOSITORY_PRIVATE': 'false',
           'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_ARCH': 'X64',
           'PACK_MARKER': str(marker), 'PACK_CONTROLS': str(controls), 'PACK_CAPTURE': str(capture)}
    if data['injection'] is not None:
        env['ISSUE25_INJECTION'] = data['injection']
    return root, target, commit, tree, inventory, env


@pytest.fixture
def pack(tmp_path, request):
    return invented_pack(tmp_path, real_git=True, mask=getattr(request, 'param', '022'))


def invoke(pack):
    root, target, commit, tree, inventory, env = pack
    settings = {'fault': env.get('PACK_FAULT'), 'exit': int(env.get('PACK_PYTEST_EXIT', '0'))}
    Path(env['PACK_CONTROLS']).write_text(json.dumps(settings))
    expected = inventory.parent / env['ISSUE25_CASE_ID'] / 'expected.json'
    return subprocess.run(['bash', str(PACK), str(root), str(target), 'released', commit, tree,
                           str(inventory), str(expected)], env=env, check=False,
                          capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize("pytest_exit", [0, 1])
@pytest.mark.parametrize("pack", ["022", "002"], indirect=True)
def test_valid_pack_reaches_pytest_and_preserves_ordinary_red(pack, pytest_exit):
    pack[-1]["PACK_PYTEST_EXIT"] = str(pytest_exit)
    result = invoke(pack)
    assert result.returncode == pytest_exit, result.stdout + result.stderr
    assert Path(pack[-1]["PACK_MARKER"]).exists()
    assert f"PACK_EXIT={pytest_exit}" in result.stdout
    assert "PACK_COMPLETE=released" in result.stdout


@pytest.mark.parametrize("fault", [
    "commit", "missing-commit", "tree", "target", "root", "content", "path", "missing-path",
    "mode", "symlink", "harness", "inventory-failure", "inventory-mismatch", "missing-inventory",
    "executable", "type", "equal-target", "relative-target", "empty-commit",
])
def test_failed_gate_stops_before_pytest(pack, fault):
    values = list(pack)
    root, target, commit, _tree, inventory, env = values
    product = target / "custom_components/invented/__init__.py"
    if fault == "commit":
        values[2] = commit[:8]  # Resolvable but not the exact promised identity.
    elif fault == "missing-commit":
        values[2] = "0" * 40
    elif fault == "tree":
        values[3] = "0" * 40
    elif fault == "target":
        values[1] = target / "missing"
    elif fault == "root":
        values[0] = root / "missing"
    elif fault == "empty-commit":
        values[2] = ""
    elif fault == "equal-target":
        values[1] = root
    elif fault == "relative-target":
        values[1] = Path("../export")
    elif fault == "content":
        product.write_text("changed\n")
    elif fault == "path":
        (product.parent / "extra.py").write_text("extra\n")
    elif fault == "missing-path":
        product.unlink()
    elif fault == "mode":
        product.chmod(0o755)
    elif fault == "executable":
        (target / "custom_components/invented/executable").chmod(0o644)
    elif fault == "type":
        product.unlink()
        product.mkdir()
    elif fault == "symlink":
        product.unlink()
        product.symlink_to(root / "custom_components/invented/__init__.py")
    elif fault == "harness":
        (target / "tools/issue25/test_restore.py").write_text("changed\n")
    elif fault == "missing-inventory":
        inventory.unlink()
    else:
        env["PACK_FAULT"] = fault
    result = invoke(values)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not Path(env["PACK_MARKER"]).exists(), result.stdout
    assert "PACK_EXIT=" not in result.stdout
    assert "PACK_COMPLETE=" not in result.stdout


@pytest.mark.parametrize("fault", ["post-content", "post-path", "post-mode", "post-harness", "post-inventory", "post-executable"])
def test_post_execution_gate_cannot_emit_pack_complete(pack, fault):
    pack[-1]["PACK_FAULT"] = fault
    result = invoke(pack)
    assert result.returncode != 0, result.stdout + result.stderr
    assert Path(pack[-1]["PACK_MARKER"]).exists()
    assert "PACK_EXIT=0" in result.stdout
    assert "PACK_COMPLETE=" not in result.stdout


def test_git_unrecorded_write_bit_is_not_drift(pack):
    product = pack[1] / "custom_components/invented/__init__.py"
    product.chmod(product.stat().st_mode ^ 0o020)
    result = invoke(pack)
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(pack[-1]["PACK_MARKER"]).exists()
    assert "PACK_COMPLETE=released" in result.stdout


def parent_inputs(tmp_path, fault='none', stage='R2'):
    """Finite acquisition/inventory/pack endpoints; the parent validator is real."""
    root = tmp_path / 'source'
    root.mkdir()
    for name in (*OVERLAY, '.github/workflows/ci.yml', 'custom_components/mxz_coordinator/__init__.py'):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('# invented\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    calls = tmp_path / 'calls'
    exports = tmp_path / 'exports'
    phase_ids = repr(PACK_IDS)
    pack_source = '''#!/usr/bin/python3
import json
import os
import sys
from pathlib import Path
'''+f'fault = {fault!r}\ncalls = Path({str(calls)!r})\npack_ids = {phase_ids}\n'+r'''
root, export, base, commit, tree, inventory, expected = sys.argv[1:]
d = Path(expected).parent
case = os.environ['ISSUE25_CASE_ID']
mode = os.environ['ISSUE25_MODE']
with calls.open('a') as file: file.write(case + '\n')
items = ['tools/issue25/test_setup_admission.py::test_exported_setup'] if mode == 'admission' else pack_ids
negative = case.startswith('N-')
injection = case[2:] if negative else None
config = d / 'alternate/pytest.ini' if case == 'N-config' else d / 'export/tools/issue25/pytest.ini'
print('ADMISSION ' + json.dumps({'event': 'observation', 'stage': 'configure', 'root': str(d / 'export'),
                                'inipath': str(config), 'requested_injection': injection}))
if case != 'N-config': print('ADMISSION ' + json.dumps({'event': 'injection', 'value': injection}))
if not negative: print('ADMISSION ' + json.dumps({'event': 'observation', 'stage': 'initial-add-complete', 'class_identity': 1}))
code = (2 if case == 'N-fixture' else 4) if negative else (1 if fault == 'red' else 0)
if fault == 'bad-exit' or (fault == 'wrong-negative-code' and negative): code = 7 if not negative else 1
active_fault = fault if mode == 'pack' or negative else 'none'
if active_fault == 'missing-COLLECTED': collected = None
elif active_fault == 'malformed-COLLECTED': collected = 'bad-json'
elif active_fault == 'reordered-items': collected = json.dumps(list(reversed(items)))
elif active_fault == 'extra-item': collected = json.dumps(items + ['unexpected'])
elif active_fault == 'empty-items': collected = '[]'
elif active_fault == 'forged-one-item-green-pack':
    items = ['tools/issue25/test_setup_admission.py::test_exported_setup']
    collected = json.dumps(items)
else: collected = json.dumps([] if negative and case != 'N-fixture' else items)
if case != 'N-config' and collected is not None:
    print('COLLECTED ' + collected)
    if active_fault == 'duplicate-COLLECTED': print('COLLECTED ' + collected)
phases = []
if not negative:
    for item in items:
        for phase in ('setup', 'call', 'teardown'):
            outcome = 'failed' if phase == 'call' and code else 'passed'
            category = 'ownership-red' if outcome == 'failed' else 'green'
            if active_fault == 'missing-call' and phase == 'call': continue
            if active_fault in ('skipped', 'xfail', 'xpass') and phase == 'call': outcome = active_fault
            if active_fault == 'unknown-category': category = 'unknown'
            phases.append({'node': item, 'phase': phase, 'outcome': outcome, 'category': category})
elif case == 'N-fixture':
    phases = [{'node': items[0], 'phase': 'setup', 'outcome': 'failed', 'category': 'unknown'},
              {'node': items[0], 'phase': 'teardown', 'outcome': 'passed', 'category': 'green'}]
if active_fault == 'duplicate-phase': phases.append(phases[0])
if active_fault == 'negative-produced-positive-call':
    phases.append({'node': items[0], 'phase': 'call', 'outcome': 'passed', 'category': 'green'})
for phase in phases: print('PHASE ' + json.dumps(phase))
if negative:
    reason = {'N-missing': 'UNKNOWN_COLLECTION_CONTRACT', 'N-origin': 'UNKNOWN_COLLECTION_ORIGIN',
              'N-config': 'UNKNOWN_CONFIG_PATH', 'N-fixture': 'UNKNOWN_FIXTURE_DEFINITION'}[case]
    print(reason)
    if case in ('N-missing', 'N-origin'):
        native = "ModuleNotFoundError: No module named 'custom_components.mxz_coordinator'" if case == 'N-missing' else 'AssertionError: UNKNOWN_COLLECTION_ORIGIN expected/export actual/decoy'
        if active_fault == 'wrong-negative-native-exception': native = 'ValueError: different failure'
        print('ADMISSION ' + json.dumps({'event': 'collect-report', 'node': items[0], 'ordinal': 0, 'longrepr': native}))
    if active_fault == 'earlier-guard-negative': print('ISSUE25_REFUSED=expected-input')
if case != 'N-config':
    classes = {p['node']: ['ownership-red'] for p in phases if p['phase'] == 'call' and p['outcome'] == 'failed'}
    print('PHASES_COMPLETE ' + json.dumps({'classes': classes, 'errors': ['negative'] if negative else [], 'exit': code}, sort_keys=True))
    if active_fault != 'missing-phases': print('PHASES_VALID=' + ('false' if negative else 'true'))
if active_fault == 'unknown': print('{"category": "unknown"}')
if active_fault == 'deselected': print('1 deselected')
if not negative: print('================ ' + str(len(items)) + (' failed' if code else ' passed') + ' ================')
marker = 'CHILD' if mode == 'admission' else 'PACK'
if active_fault not in ('missing-process-exit',):
    (d / 'exit.txt').write_text(str(code) + '\n')
if active_fault != 'missing-process-exit': print(marker + '_EXIT=' + str(1 if active_fault in ('wrong-exit', 'conflicting-exit') else code))
if active_fault != 'missing-complete': print(marker + '_COMPLETE=' + case)
if active_fault == 'duplicate-complete': print(marker + '_COMPLETE=' + case)
sys.exit(code)
'''
    # Parent invokes bash explicitly: the shim execs the fixed stdlib interpreter.
    (root / 'tools/issue25/pack.sh').write_text("#!/bin/bash\nexec /usr/bin/python3 - \"$@\" <<'PY_FAKE'\n" + pack_source.split('\n', 1)[1] + '\nPY_FAKE\n')
    product = b'# invented\n'
    tree_line = f'100644 blob {blob(product)}\tcustom_components/mxz_coordinator/__init__.py\n'
    # The archive stream is built here once; the stand-in only replays it. A Bash
    # stand-in starts in ~1 ms, an interpreter in ~20 ms, and the parent calls Git
    # up to 22 times per case.
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w|') as stream:
        info = tarfile.TarInfo('custom_components/mxz_coordinator/__init__.py')
        info.size = len(product)
        info.mode = 0o644
        stream.addfile(info, io.BytesIO(product))
    (tmp_path / 'archive.tar').write_bytes(archive.getvalue())
    git_source = f'''#!/bin/bash
fault={shlex.quote(fault)}
exports={shlex.quote(str(exports))}
tarball={shlex.quote(str(tmp_path / 'archive.tar'))}
line={shlex.quote(tree_line)}
''' + r'''tab=$'\t'
case "$*" in
  'rev-parse HEAD') echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
  'rev-parse HEAD^{tree}') echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ;;
  'rev-parse HEAD:.github/workflows/ci.yml') echo cccccccccccccccccccccccccccccccccccccccc ;;
  'rev-parse --verify '*'^{commit}') printf '%s\n' "${3%"^{commit}"}" ;;
  'rev-parse --verify 3a9863896f8affb6f71cbd1e495df21b17a69ff3^{tree}') echo 238636de6d067c8e05b59993819cc5187c2bf9df ;;
  'rev-parse --verify f760f74690103d8fe12a94a3d8806f206f7dc3ee^{tree}') echo 8c05b4907f9a4a0066361be57267765336e1153c ;;
  'rev-parse --verify '*) exit 1 ;;
  diff|'diff '*) ;;
  'ls-tree '*--name-only*) printf '%s' "${line#*"$tab"}" ;;
  'ls-tree '*) printf '%s' "$line" ;;
  'archive '*)
    printf '%s\n' "$2" >> "$exports"
    if test "$fault" = archive; then exit 9; fi
    cat "$tarball" ;;
  *) exit 9 ;;
esac
'''
    (bin_dir / 'git').write_text(git_source)
    (bin_dir / 'git').chmod(0o755)
    if fault == 'tar':
        (bin_dir / 'tar').write_text('#!/bin/bash\ncat >/dev/null\nexit 9\n')
        (bin_dir / 'tar').chmod(0o755)
    python = tmp_path / 'mxz-venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('#!/usr/bin/python3\nprint("invented inventory")\n')
    python.chmod(0o755)
    site = tmp_path / 'mxz-venv/lib/python3.12/site-packages'
    site.mkdir(parents=True)
    specs = {
        'homeassistant': ('2024.12.0', ['homeassistant/helpers/restore_state.py', 'homeassistant/helpers/storage.py', 'homeassistant/helpers/entity_platform.py'], None),
        'pytest-homeassistant-custom-component': ('0.13.190', ['pytest_homeassistant_custom_component/common.py', 'pytest_homeassistant_custom_component/plugins.py'], 'pytest_homeassistant_custom_component.plugins'),
        'pytest': ('8.3.3', ['_pytest/config/__init__.py', '_pytest/main.py', '_pytest/runner.py', '_pytest/python.py', '_pytest/fixtures.py'], None),
        'pytest-asyncio': ('0.24.0', ['pytest_asyncio/plugin.py'], 'pytest_asyncio.plugin'),
    }
    for name, (version, files, point) in specs.items():
        metadata = site / (name.replace('-', '_') + '-' + version + '.dist-info')
        metadata.mkdir()
        (metadata / 'METADATA').write_text(f'Name: {name}\nVersion: {version}\n')
        (metadata / 'RECORD').write_text(''.join(path + ',,\n' for path in files))
        if point:
            (metadata / 'entry_points.txt').write_text('[pytest11]\ninvented = ' + point + '\n')
        for name in files:
            path = site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# invented package file, never imported\n')
    env = {'PATH': str(bin_dir) + ':' + os.defpath, 'RUNNER_TEMP': str(tmp_path),
           'ISSUE25_MODE': 'admission' if stage == 'A3' else 'pack',
           'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/ci/issue25-harness-' + STAGES[stage],
           'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_ID': '123', 'GITHUB_SHA': 'a' * 40,
           'GITHUB_REPOSITORY': 'dkpnw/ha-mxz-coordinator', 'REPOSITORY_PRIVATE': 'false',
           'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_ARCH': 'X64'}
    return root, env, calls, exports


@pytest.mark.parametrize("fault", ["none", "red", "archive", "tar", "missing-complete", "unknown",
                                    "missing-phases", "wrong-exit", "duplicate-complete", "bad-exit"])
def test_parent_stops_before_next_base_on_export_or_marker_failure(tmp_path, fault):
    root, env, calls_file, exports_file = parent_inputs(tmp_path, fault)
    result = subprocess.run(['bash', str(PACK.with_name('run.sh'))], cwd=root, env=env,
                            check=False, capture_output=True, text=True, timeout=15)
    calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    assert exports_file.is_file(), result
    exports = exports_file.read_text().splitlines()
    if fault in ('none', 'red'):
        assert calls == ['released', 'main'] and len(exports) == 2, result.stdout + result.stderr
        assert result.returncode == (0 if fault == 'none' else 1)
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert len(exports) == 1
        assert calls == ([] if fault in ('archive', 'tar') else ['released'])


B_CASES = (
    'missing-base', 'empty-base', 'unknown-base', 'base-commit-conflict', 'base-tree-conflict',
    'missing-mode', 'empty-mode', 'unknown-mode', 'pack-on-A3', 'admission-on-R2', 'admission-on-I1',
    'missing-case', 'wrong-case', 'missing-expected', 'malformed-expected', 'duplicate-JSON-key',
    'wrong-expected-digest', 'wrong-destination', 'wrong-expected-config', 'missing-harness-manifest',
    'missing-fixture-manifest', 'symlink-expected', 'empty-selector-positive', 'unknown-selector-A3',
    'selector-case-conflict', 'preexisting-child-tmp', 'extra-c', 'extra-p', 'extra-k',
    'extra-positional', 'PYTHONPATH', 'PYTEST_ADDOPTS',
)


@pytest.mark.parametrize('fault', B_CASES, ids=[f'B{i:02d}' for i in range(1, 33)])
def test_exact_pack_input_refusals(tmp_path, fault):
    stage = 'A3' if fault in ('unknown-selector-A3', 'selector-case-conflict') else 'R2'
    values = invented_pack(tmp_path, stage=stage, case='P-release' if stage == 'A3' else 'released')
    root, target, commit, tree, inventory, env = values
    child = target.parent
    expected = child / 'expected.json'
    data = json.loads(expected.read_text())
    args = [str(root), str(target), 'released', commit, tree, str(inventory), str(expected)]
    reason = {
        'missing-base': 'arguments', 'empty-base': 'base', 'unknown-base': 'base',
        'base-commit-conflict': 'base-commit', 'base-tree-conflict': 'base-tree',
        'missing-mode': 'mode', 'empty-mode': 'mode', 'unknown-mode': 'mode',
        'pack-on-A3': 'mode-ref', 'admission-on-R2': 'mode-ref', 'admission-on-I1': 'mode-ref',
        'missing-case': 'case', 'wrong-case': 'case', 'missing-expected': 'expected',
        'malformed-expected': 'malformed-expected', 'duplicate-JSON-key': 'duplicate-json-key',
        'wrong-expected-digest': 'expected-digest', 'wrong-destination': 'destination',
        'wrong-expected-config': 'expected-config', 'missing-harness-manifest': 'harness-manifest',
        'missing-fixture-manifest': 'fixture_manifest', 'symlink-expected': 'expected-path',
        'empty-selector-positive': 'injection', 'unknown-selector-A3': 'injection',
        'selector-case-conflict': 'injection', 'preexisting-child-tmp': 'preexisting-child-tmp',
        'extra-c': 'arguments', 'extra-p': 'arguments', 'extra-k': 'arguments',
        'extra-positional': 'arguments', 'PYTHONPATH': 'override-PYTHONPATH',
        'PYTEST_ADDOPTS': 'override-PYTEST_ADDOPTS',
    }[fault]
    if fault == 'missing-base':
        del args[2]
    elif fault in ('empty-base', 'unknown-base'):
        args[2] = '' if fault == 'empty-base' else 'unknown'
    elif fault == 'base-commit-conflict':
        args[3] = '0' * 40
    elif fault == 'base-tree-conflict':
        args[4] = '0' * 40
    elif fault == 'missing-mode':
        del env['ISSUE25_MODE']
    elif fault in ('empty-mode', 'unknown-mode'):
        env['ISSUE25_MODE'] = '' if fault == 'empty-mode' else 'unknown'
    elif fault == 'pack-on-A3':
        env['GITHUB_REF'] = 'refs/heads/ci/issue25-harness-admission'
    elif fault.startswith('admission-on-'):
        env['ISSUE25_MODE'] = 'admission'
        if fault.endswith('I1'):
            env['GITHUB_REF'] = 'refs/heads/ci/issue25-harness-repeat'
    elif fault == 'missing-case':
        del env['ISSUE25_CASE_ID']
    elif fault == 'wrong-case':
        env['ISSUE25_CASE_ID'] = 'other'
    elif fault == 'missing-expected':
        expected.unlink()
    elif fault in ('malformed-expected', 'duplicate-JSON-key'):
        raw = b'{' if fault == 'malformed-expected' else b'{"schema_version":1,"schema_version":1}'
        expected.write_bytes(raw)
        expected.with_suffix('.sha256').write_text(hashlib.sha256(raw).hexdigest() + '\n')
    elif fault == 'wrong-expected-digest':
        expected.with_suffix('.sha256').write_text('0' * 64 + '\n')
    elif fault == 'wrong-destination':
        args[1] = str(tmp_path / 'other')
    elif fault == 'wrong-expected-config':
        data['expected_config'] = str(child / 'other.ini')
        write_expected(child, data)
    elif fault == 'missing-harness-manifest':
        data['overlay'].pop(OVERLAY[0])
        write_expected(child, data)
    elif fault == 'missing-fixture-manifest':
        (child / 'fixture-files.json').unlink()
    elif fault == 'symlink-expected':
        real = child / 'invented-input'
        expected.rename(real)
        expected.symlink_to(real)
    elif fault in ('empty-selector-positive', 'unknown-selector-A3', 'selector-case-conflict'):
        env['ISSUE25_INJECTION'] = {'empty-selector-positive': '', 'unknown-selector-A3': 'unknown',
                                  'selector-case-conflict': 'origin'}[fault]
    elif fault == 'preexisting-child-tmp':
        (child / 'tmp').mkdir()
    elif fault.startswith('extra-'):
        args.extend(['-' + fault[6:], 'invented'])
    else:
        env[fault] = 'invented'
    result = subprocess.run(['bash', str(PACK), *args], env=env, check=False,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and 'ISSUE25_REFUSED=' + reason in result.stderr, result.stdout + result.stderr
    assert not Path(env['PACK_MARKER']).exists()
    assert '_COMPLETE=' not in result.stdout


@pytest.mark.parametrize('script', ['run.sh', 'pack.sh'])
@pytest.mark.parametrize('stage', ['R2', 'I1'])
@pytest.mark.parametrize('selector', ['missing', 'origin', 'config', 'fixture', '', 'unknown'])
def test_no_pack_selector(tmp_path, script, stage, selector):
    env = {'PATH': os.defpath, 'GITHUB_REF': 'refs/heads/ci/issue25-harness-' + STAGES[stage],
           'ISSUE25_MODE': 'pack', 'ISSUE25_INJECTION': selector}
    result = subprocess.run(['bash', str(PACK.with_name(script))], cwd=tmp_path, env=env,
                            check=False, capture_output=True, text=True, timeout=5)
    reason = 'control-key:ISSUE25_INJECTION' if script == 'run.sh' else 'injection'
    assert result.returncode == 1 and result.stderr.strip() == 'ISSUE25_REFUSED=' + reason
    assert result.stdout == ''


V_ROWS = [('A3', case, 'main' if case == 'P-main' else 'released') for case in (
    'P-release', 'P-main', 'N-missing', 'N-origin', 'N-config', 'N-fixture')]
V_ROWS += [(stage, base, base) for stage in ('R2', 'I1') for base in ('released', 'main')]


@pytest.mark.parametrize('stage,case,base', V_ROWS, ids=[f'V/{s}/{c}' for s, c, _ in V_ROWS])
def test_closed_child_manifest(tmp_path, stage, case, base):
    root, target, commit, tree, inventory, env = invented_pack(tmp_path, stage=stage, case=case, base=base)
    expected = target.parent / 'expected.json'
    promised = expected.with_suffix('.sha256').read_text().strip()
    result = subprocess.run(['bash', str(PACK), str(root), str(target), base, commit, tree,
                             str(inventory), str(expected)], env=env, check=False,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    capture = json.loads(Path(env['PACK_CAPTURE']).read_text())
    config = target.parent / 'alternate/pytest.ini' if case == 'N-config' else target / 'tools/issue25/pytest.ini'
    item = 'tools/issue25/test_setup_admission.py' if stage == 'A3' else 'tools/issue25/test_restore.py'
    assert capture['argv'] == ['-m', 'pytest', '-p', 'tools.pytest_phases', '--import-mode=prepend',
                               '--rootdir=' + str(target), '-c', str(config),
                               '--confcutdir=' + str(target / 'tools/issue25'), item, '-s', '-p', 'no:cacheprovider']
    wanted = {'PATH': str(tmp_path / 'mxz-venv/bin') + ':/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
              'TZ': 'UTC', 'TMPDIR': str(target.parent / 'tmp'), 'PYTHONDONTWRITEBYTECODE': '1',
              'ISSUE25_MODE': 'admission' if stage == 'A3' else 'pack', 'ISSUE25_BASE': base,
              'ISSUE25_CASE_ID': case, 'ISSUE25_EXPECTED': str(expected), 'ISSUE25_EXPECTED_SHA256': promised,
              'GITHUB_REPOSITORY': 'dkpnw/ha-mxz-coordinator', 'GITHUB_EVENT_NAME': 'push',
              'GITHUB_REF': 'refs/heads/ci/issue25-harness-' + STAGES[stage], 'GITHUB_HEAD_REF': '', 'GITHUB_BASE_REF': '',
              'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': commit,
              'REPOSITORY_PRIVATE': 'false', 'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_ARCH': 'X64'}
    if case.startswith('N-'):
        wanted['ISSUE25_INJECTION'] = case[2:]
    assert capture['env'] == wanted
    assert capture['cwd'] == str(target)
    assert expected.with_suffix('.sha256').read_text() == promised + '\n'
    assert hashlib.sha256(expected.read_bytes()).hexdigest() == promised
    assert (target.parent / 'exit.txt').read_text() == '0\n'
    # Fake endpoint success is solely argv/environment evidence, never a runtime negative PASS.


L_CASES = ('missing-COLLECTED', 'duplicate-COLLECTED', 'malformed-COLLECTED', 'reordered-items',
           'extra-item', 'empty-items', 'forged-one-item-green-pack', 'skipped', 'xfail', 'xpass',
           'deselected', 'missing-call', 'duplicate-phase', 'unknown-category', 'missing-process-exit',
           'conflicting-exit', 'wrong-negative-code', 'wrong-negative-native-exception',
           'earlier-guard-negative', 'negative-produced-positive-call')


@pytest.mark.parametrize('fault', L_CASES, ids=[f'L{i:02d}' for i in range(1, 21)])
def test_parent_refuses_incomplete_native_contract(tmp_path, fault):
    stage = 'A3' if fault in L_CASES[16:] else 'R2'
    root, env, calls, _exports = parent_inputs(tmp_path, fault, stage)
    result = subprocess.run(['bash', str(PACK.with_name('run.sh'))], cwd=root, env=env,
                            check=False, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0, result.stdout + result.stderr
    assert calls.is_file(), result
    assert calls.read_text().splitlines() == (['P-release', 'P-main', 'N-missing'] if stage == 'A3' else ['released'])
    assert 'CONTROL_REJECTION_EXPECTED=N-missing' not in result.stdout
    reasons = {
        'missing-COLLECTED': 'UNKNOWN_RECORD: COLLECTED', 'duplicate-COLLECTED': 'UNKNOWN_RECORD: COLLECTED',
        'malformed-COLLECTED': 'JSONDecodeError', 'reordered-items': 'UNKNOWN_COLLECTION',
        'extra-item': 'UNKNOWN_COLLECTION', 'empty-items': 'UNKNOWN_COLLECTION',
        'forged-one-item-green-pack': 'UNKNOWN_COLLECTION', 'skipped': 'UNKNOWN_CALL_OUTCOME',
        'xfail': 'UNKNOWN_CALL_OUTCOME', 'xpass': 'UNKNOWN_CALL_OUTCOME',
        'deselected': 'UNKNOWN_SPECIAL_RESULT', 'missing-call': 'UNKNOWN_PHASE_COUNT',
        'duplicate-phase': 'UNKNOWN_PHASE_COUNT', 'unknown-category': 'UNKNOWN_CATEGORY',
        'missing-process-exit': 'UNKNOWN_PROCESS_EXIT', 'conflicting-exit': 'UNKNOWN_EXIT',
        'wrong-negative-code': 'UNKNOWN_NEGATIVE_CODE', 'wrong-negative-native-exception': 'UNKNOWN_EXTRA_COLLECTION_EXCEPTION',
        'earlier-guard-negative': 'UNKNOWN_EARLIER_GUARD', 'negative-produced-positive-call': 'UNKNOWN_NEGATIVE_CALL',
    }
    assert reasons[fault] in result.stderr, result.stdout + result.stderr
