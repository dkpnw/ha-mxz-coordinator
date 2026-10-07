#!/usr/bin/env bash
# Fixed supervisor table. A native negative remains negative in its own receipt.
set -euo pipefail
refuse() { echo "ISSUE25_REFUSED=$1" >&2; exit 1; }
test "$#" -eq 0 || refuse arguments
# Inspect every supplied control key. No externally supplied child selector is sanitized.
bad_keys=()
for key in "${!ISSUE25_@}"; do
  case "$key" in ISSUE25_MODE) ;; *) bad_keys+=("$key") ;; esac
done
if test "${#bad_keys[@]}" -ne 0; then
  printf 'ISSUE25_REFUSED=control-key:%s\n' "${bad_keys[@]}" >&2
  exit 1
fi
for key in PYTHONPATH PYTHONHOME PYTEST_ADDOPTS PYTEST_PLUGINS PYTEST_DISABLE_PLUGIN_AUTOLOAD; do
  test "${!key+x}" != x || refuse "override-$key"
done
case "${ISSUE25_MODE:-}" in admission|pack) ;; *) refuse mode ;; esac
test "${GITHUB_EVENT_NAME:-}" = push || refuse event
case "${GITHUB_REF:-}" in
  refs/heads/ci/issue25-harness-admission) stage=A3; required_mode=admission ;;
  refs/heads/ci/issue25-harness-replacement) stage=R2; required_mode=pack ;;
  refs/heads/ci/issue25-harness-repeat) stage=I1; required_mode=pack ;;
  *) refuse ref ;;
esac
test "$ISSUE25_MODE" = "$required_mode" || refuse mode-ref
test "${GITHUB_RUN_ATTEMPT:-}" = 1 || refuse attempt
test "${REPOSITORY_PRIVATE:-}" = false || refuse repository
test "${RUNNER_ENVIRONMENT:-}" = github-hosted || refuse runner
test "${RUNNER_ARCH:-}" = X64 || refuse architecture
[[ "${GITHUB_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || refuse source
test "$(git rev-parse HEAD)" = "$GITHUB_SHA" || refuse source
[[ "${GITHUB_RUN_ID:-}" =~ ^[1-9][0-9]*$ ]] || refuse run-id
test "${GITHUB_REPOSITORY:-}" = dkpnw/ha-mxz-coordinator || refuse repository-name
test -z "${GITHUB_HEAD_REF:-}" && test -z "${GITHUB_BASE_REF:-}" || refuse pr-context
root=$(pwd -P)
test "$root" = "$PWD" || refuse checkout
scratch=$(cd "$RUNNER_TEMP" && pwd -P)
test "$scratch" = "$RUNNER_TEMP" && test ! -L "$RUNNER_TEMP" || refuse runner-temp
git diff --exit-code HEAD -- . || refuse source-drift
source_tree=$(git rev-parse HEAD^{tree})
workflow_blob=$(git rev-parse HEAD:.github/workflows/ci.yml)
job="$RUNNER_TEMP/issue25-$stage-$GITHUB_RUN_ID-a1"
mkdir "$job" || refuse preexisting-job
: > "$job/supervisor.jsonl"
"$RUNNER_TEMP/mxz-venv/bin/python" tools/check_lock.py requirements/constraints-py312-ha2024.12.0.txt \
  0d0031ddca8bb870560ca4d4bcafd34233e23e9dd6f0470dc31f7bd888eb7403 3.12.14 \
  > "$job/inventory.before.txt"
cat "$job/inventory.before.txt"
package_snapshot() {
  /usr/bin/python3 - "$job" "$1" <<'PY_SNAPSHOT'
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

job, phase = sys.argv[1:]
site = Path(os.environ['RUNNER_TEMP']) / 'mxz-venv/lib/python3.12/site-packages'
selected = {'homeassistant': ['homeassistant/helpers/restore_state.py', 'homeassistant/helpers/storage.py',
                              'homeassistant/helpers/entity_platform.py'],
            'pytest-homeassistant-custom-component': ['pytest_homeassistant_custom_component/common.py'],
            'pytest': ['_pytest/config/__init__.py', '_pytest/main.py', '_pytest/runner.py', '_pytest/python.py', '_pytest/fixtures.py'],
            'pytest-asyncio': []}
rows = []
seen = set()
for dist in importlib.metadata.distributions(path=[str(site)]):
    name = dist.metadata['Name']
    if name not in selected:
        continue
    assert name not in seen, 'UNKNOWN_DUPLICATE_DISTRIBUTION'
    seen.add(name)
    membership = {str(p) for p in dist.files or []}
    paths = list(selected[name])
    for point in dist.entry_points:
        if point.group == 'pytest11':
            module = point.value.split(':')[0].replace('.', '/')
            matches = [p for p in (module + '.py', module + '/__init__.py') if p in membership]
            assert len(matches) == 1, 'UNKNOWN_PLUGIN_MEMBERSHIP'
            paths.extend(matches)
    for relative in sorted(set(paths)):
        assert relative in membership, 'UNKNOWN_FILE_MEMBERSHIP'
        p = Path(dist.locate_file(relative))
        assert p.is_file() and not p.is_symlink(), 'UNKNOWN_PACKAGE_FILE'
        rows.append({'path': str(p), 'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                     'distribution': name, 'version': dist.version})
assert seen == set(selected) and len(rows) == 11, 'UNKNOWN_PACKAGE_CARDINALITY'
snapshot = {'files': sorted(rows, key=lambda r: r['path']), 'interpreter': {'executable': sys.executable, 'version': sys.version, 'sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()}}
raw = (json.dumps(snapshot, sort_keys=True, separators=(',', ':')) + '\n').encode()
assert len(raw) <= 8192, 'UNKNOWN_PACKAGE_SNAPSHOT_BUDGET'
Path(job, 'package-files.' + phase + '.json').write_bytes(raw)
if phase == 'after':
    assert raw == Path(job, 'package-files.before.json').read_bytes(), 'UNKNOWN_PACKAGE_DRIFT'
PY_SNAPSHOT
}
package_snapshot before
result=0
if test "$ISSUE25_MODE" = admission; then
  cases=(P-release P-main N-missing N-origin N-config N-fixture)
else
  cases=(released main)
fi
for case_id in "${cases[@]}"; do
  injection=
  case "$case_id" in
    P-main|main) base=main ;;
    *) base=released ;;
  esac
  case "$case_id" in N-*) injection=${case_id#N-} ;; esac
  case "$base" in
    released) commit=3a9863896f8affb6f71cbd1e495df21b17a69ff3; tree=238636de6d067c8e05b59993819cc5187c2bf9df ;;
    main) commit=4f47f164848f10135b1b1ea3151f60ad3f415b69; tree=a01029ab25c40e299a58ddd2067568e88d6808a1 ;;
    *) refuse base ;;
  esac
  test "$(git rev-parse --verify "$commit^{commit}")" = "$commit" || refuse commit
  test "$(git rev-parse --verify "$commit^{tree}")" = "$tree" || refuse tree
  child="$job/$case_id"
  mkdir "$child"
  target="$child/export"
  mkdir "$target"
  git ls-tree -r "$commit" > "$child/base.tree"
  git ls-tree -r "$commit" -- custom_components > "$child/product.tree"
  git ls-tree -r --name-only "$commit" -- custom_components > "$child/product.paths"
  git archive "$commit" | tar -x -C "$target"
  mkdir -p "$target/tools/issue25"
  cp tools/pytest_phases.py "$target/tools/"
  cp tools/issue25/test_restore.py tools/issue25/conftest.py tools/issue25/test_setup_admission.py "$target/tools/issue25/"
  /usr/bin/python3 - "$root" "$child" "$stage" "$case_id" "$base" "$commit" "$tree" "$source_tree" "$workflow_blob" <<'PY_PREPARE'
import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
from pathlib import Path

root, child, stage, case, base, commit, tree, source_tree, workflow_blob = sys.argv[1:]
d = Path(child)
export = d / 'export'
site = Path(os.environ['RUNNER_TEMP']) / 'mxz-venv/lib/python3.12/site-packages'
selected = {'homeassistant': ['homeassistant/helpers/restore_state.py', 'homeassistant/helpers/storage.py',
                              'homeassistant/helpers/entity_platform.py'],
            'pytest-homeassistant-custom-component': ['pytest_homeassistant_custom_component/common.py'],
            'pytest': ['_pytest/config/__init__.py', '_pytest/main.py', '_pytest/runner.py', '_pytest/python.py', '_pytest/fixtures.py'],
            'pytest-asyncio': []}
versions = {'homeassistant': '2024.12.0', 'pytest-homeassistant-custom-component': '0.13.190',
            'pytest': '8.3.3', 'pytest-asyncio': '0.24.0'}
files, entrypoints, plugin_files = [], [], []
seen = set()
for dist in importlib.metadata.distributions(path=[str(site)]):
    name = dist.metadata['Name']
    if name not in selected:
        continue
    assert name not in seen and dist.version == versions[name], 'UNKNOWN_DISTRIBUTION'
    seen.add(name)
    membership = {str(p) for p in dist.files or []}
    paths = list(selected[name])
    for point in dist.entry_points:
        if point.group == 'pytest11':
            module = point.value.split(':')[0]
            candidates = [module.replace('.', '/') + '.py', module.replace('.', '/') + '/__init__.py']
            matches = [p for p in candidates if p in membership]
            assert len(matches) == 1, 'UNKNOWN_PLUGIN_MEMBERSHIP'
            paths.extend(matches)
            plugin_files.append(str(dist.locate_file(matches[0])))
            entrypoints.append({'distribution': name, 'name': point.name, 'value': point.value})
    for relative in sorted(set(paths)):
        assert relative in membership, 'UNKNOWN_FILE_MEMBERSHIP'
        path = Path(dist.locate_file(relative))
        assert path.is_file() and not path.is_symlink(), 'UNKNOWN_DEPENDENCY_FILE'
        files.append({'distribution': name, 'version': dist.version, 'path': str(path),
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
assert seen == set(selected), 'UNKNOWN_DISTRIBUTION_MISSING'
assert len(files) == 11, 'UNKNOWN_PACKAGE_CARDINALITY'
fixture = {'files': files, 'plugin_files': sorted(plugin_files), 'entry_points': entrypoints}
fixture_raw = (json.dumps(fixture, sort_keys=True, separators=(',', ':')) + '\n').encode()
assert len(fixture_raw) <= 8192, 'UNKNOWN_FIXTURE_MANIFEST_BUDGET'
(d / 'fixture-files.json').write_bytes(fixture_raw)
rows = {'P-release': ('released', None, 0, None, None), 'P-main': ('main', None, 0, None, None),
        'N-missing': ('released', 'missing', 4, 'ModuleNotFoundError', 'UNKNOWN_COLLECTION_CONTRACT'),
        'N-origin': ('released', 'origin', 4, 'AssertionError', 'UNKNOWN_COLLECTION_ORIGIN'),
        'N-config': ('released', 'config', 4, 'UsageError', 'UNKNOWN_CONFIG_PATH'),
        'N-fixture': ('released', 'fixture', 2, 'AssertionError', 'UNKNOWN_FIXTURE_DEFINITION')}
mode = 'admission' if stage == 'A3' else 'pack'
row = rows[case] if mode == 'admission' else (base, None, None, None, None)
assert row[0] == base
injection = row[1]
config = export / 'tools/issue25/pytest.ini'
config_hash = hashlib.sha256(b'[pytest]\nasyncio_mode = auto\n').hexdigest()
alternate = str(d / 'alternate/pytest.ini') if injection == 'config' else None
decoy = str(d / 'decoy/custom_components/mxz_coordinator') if injection == 'origin' else None
if decoy:
    shutil.copytree(export / 'custom_components/mxz_coordinator', decoy, copy_function=shutil.copy2)
    (d / 'decoy.tree').write_bytes((d / 'product.tree').read_bytes())
    for line in (d / 'product.tree').read_text().splitlines():
        mode_bits, kind, blob, relative = line.split(None, 3)
        assert kind == 'blob' and mode_bits in ('100644', '100755'), 'UNKNOWN_DECOY_TYPE'
        path = d / 'decoy' / relative
        raw = path.read_bytes()
        assert hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == blob, 'UNKNOWN_DECOY_BYTES'
        assert bool(path.stat().st_mode & 0o100) == (mode_bits == '100755'), 'UNKNOWN_DECOY_MODE'
overlay = {name: hashlib.sha256((Path(root) / name).read_bytes()).hexdigest() for name in (
    'tools/pytest_phases.py', 'tools/issue25/test_restore.py', 'tools/issue25/conftest.py',
    'tools/issue25/test_setup_admission.py')}
(d / 'harness.sha256').write_text(''.join(f'{sha}  {name}\n' for name, sha in overlay.items()))
(d / 'config.sha256').write_text(f'{config_hash}  {config}\n' + (f'{config_hash}  {alternate}\n' if alternate else ''))


def record(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


data = {'schema_version': 1, 'stage': stage, 'run_id': os.environ['GITHUB_RUN_ID'], 'attempt': '1',
        'event': 'push', 'ref': os.environ['GITHUB_REF'], 'source_commit': os.environ['GITHUB_SHA'],
        'source_tree': source_tree, 'workflow_blob': workflow_blob, 'case_id': case, 'mode': mode,
        'base': base, 'base_commit': commit, 'base_tree': tree, 'checkout': root, 'export': str(export),
        'temp': str(d / 'tmp'), 'expected_config': str(config), 'actual_config': alternate or str(config),
        'python': str(Path(os.environ['RUNNER_TEMP']) / 'mxz-venv/bin/python'),
        'base_manifest': record(d / 'base.tree'), 'product_manifest': record(d / 'product.tree'),
        'product_paths': record(d / 'product.paths'), 'overlay': overlay, 'config_sha256': config_hash,
        'fixture_manifest': record(d / 'fixture-files.json'), 'inventory': record(d.parent / 'inventory.before.txt'),
        'phases': ['setup', 'call', 'teardown'], 'deadline': '60s' if mode == 'admission' else '180s',
        'injection': injection, 'exception': row[3], 'reason': row[4], 'exit': row[2],
        'fault_paths': {'decoy': decoy, 'alternate_config': alternate}}
data['items'] = ['tools/issue25/test_setup_admission.py::test_exported_setup'] if mode == 'admission' else [
    'tools/issue25/test_restore.py::test_restore_schedule[' + name + ']' for name in (
        '01-clean-twice', '02-unavailable-twice', '03-manual-twin', '04-options-reload',
        '05-missing-restore-negative', '06-changed-active-demand', '07-provisional-auto-old-echo',
        '08-missing-speed-recovery')]
raw = (json.dumps(data, sort_keys=True, separators=(',', ':')) + '\n').encode()
(d / 'expected.json').write_bytes(raw)
(d / 'expected.sha256').write_text(hashlib.sha256(raw).hexdigest() + '\n')
PY_PREPARE
  code=0
  if test -n "$injection"; then
    ISSUE25_CASE_ID="$case_id" ISSUE25_INJECTION="$injection" bash tools/issue25/pack.sh \
      "$root" "$target" "$base" "$commit" "$tree" "$job/inventory.before.txt" "$child/expected.json" \
      > "$child/output.log" 2>&1 || code=$?
  else
    ISSUE25_CASE_ID="$case_id" bash tools/issue25/pack.sh \
      "$root" "$target" "$base" "$commit" "$tree" "$job/inventory.before.txt" "$child/expected.json" \
      > "$child/output.log" 2>&1 || code=$?
  fi
  if ! /usr/bin/python3 - "$child" "$case_id" "$ISSUE25_MODE" "$code" <<'PY_RESULT'
import hashlib
import json
import re
import sys
from pathlib import Path

child, case, mode, status = sys.argv[1:]
d = Path(child)
raw = (d / 'output.log').read_bytes()
assert len(raw) <= (262144 if mode == 'admission' else 2097152), 'UNKNOWN_LOG_BUDGET'
lines = raw.decode().splitlines()
code = int(status)
assert (d / 'exit.txt').is_file(), 'UNKNOWN_PROCESS_EXIT'
assert (d / 'exit.txt').read_text() == str(code) + '\n', 'UNKNOWN_PROCESS_EXIT'


def records(prefix):
    return [line[len(prefix):] for line in lines if line.startswith(prefix)]


def one(prefix):
    values = records(prefix)
    assert len(values) == 1, 'UNKNOWN_RECORD: ' + prefix
    return values[0]


marker = 'CHILD' if mode == 'admission' else 'PACK'
assert one(marker + '_EXIT=') == str(code), 'UNKNOWN_EXIT'
assert one(marker + '_COMPLETE=') == case, 'UNKNOWN_COMPLETION'
pack_items = ['tools/issue25/test_restore.py::test_restore_schedule[' + name + ']' for name in (
    '01-clean-twice', '02-unavailable-twice', '03-manual-twin', '04-options-reload',
    '05-missing-restore-negative', '06-changed-active-demand', '07-provisional-auto-old-echo',
    '08-missing-speed-recovery')]
admission_items = ['tools/issue25/test_setup_admission.py::test_exported_setup']
observations = [json.loads(value) for value in records('ADMISSION ')]
reports = [value for value in observations if value['event'] == 'collect-report']
phases = [json.loads(value) for value in records('PHASE ')]
negative = mode == 'admission' and case.startswith('N-')
expected_data = json.loads((d / 'expected.json').read_bytes())
configure = [r for r in observations if r['event'] == 'observation' and r['stage'] == 'configure']
assert len(configure) == 1, 'UNKNOWN_CONFIG_OBSERVATION'
assert configure[0]['root'] == str(d / 'export'), 'UNKNOWN_OBSERVED_ROOT'
assert configure[0]['inipath'] == expected_data['actual_config'], 'UNKNOWN_OBSERVED_CONFIG'
assert configure[0]['requested_injection'] == expected_data['injection'], 'UNKNOWN_INJECTION_OBSERVATION'
if case != 'N-config':
    injections = [r for r in observations if r['event'] == 'injection']
    assert injections == [{'event': 'injection', 'value': expected_data['injection']}], 'UNKNOWN_INJECTION_OBSERVATION'
if negative:
    expected_code = 2 if case == 'N-fixture' else 4
    assert code == expected_code, 'UNKNOWN_NEGATIVE_CODE'
    assert not any(p['phase'] == 'call' for p in phases), 'UNKNOWN_NEGATIVE_CALL'
    reason = {'N-missing': 'UNKNOWN_COLLECTION_CONTRACT', 'N-origin': 'UNKNOWN_COLLECTION_ORIGIN',
              'N-config': 'UNKNOWN_CONFIG_PATH', 'N-fixture': 'UNKNOWN_FIXTURE_DEFINITION'}[case]
    assert reason in raw.decode(), 'UNKNOWN_NEGATIVE_REASON'
    assert not any(line.startswith('ISSUE25_REFUSED=') for line in lines), 'UNKNOWN_EARLIER_GUARD'
    if case in ('N-missing', 'N-origin'):
        assert json.loads(one('COLLECTED ')) == [], 'UNKNOWN_NEGATIVE_COLLECTION'
        assert one('PHASES_VALID=') == 'false', 'UNKNOWN_NEGATIVE_PHASES'
        assert reports and [r['ordinal'] for r in reports] == list(range(len(reports))), 'UNKNOWN_COLLECT_REPORTS'
        # Ordered retained native reports, never stdout/stderr interleaving.
        first = reports[0]['longrepr']
        expected_exception = 'ModuleNotFoundError' if case == 'N-missing' else 'AssertionError'
        assert all(expected_exception in report['longrepr'] for report in reports), 'UNKNOWN_EXTRA_COLLECTION_EXCEPTION'
        if case == 'N-missing':
            assert "ModuleNotFoundError" in first and "custom_components.mxz_coordinator" in first, 'UNKNOWN_NATIVE_EXCEPTION'
        else:
            assert 'AssertionError' in first and reason in first and 'ModuleNotFoundError' not in first, 'UNKNOWN_NATIVE_EXCEPTION'
    elif case == 'N-config':
        assert not records('COLLECTED ') and not phases, 'UNKNOWN_CONFIG_STAGE'
        assert any(r['event'] == 'observation' and r['stage'] == 'configure' for r in observations), 'UNKNOWN_CONFIG_OBSERVATION'
    else:
        assert json.loads(one('COLLECTED ')) == admission_items and not reports, 'UNKNOWN_FIXTURE_COLLECTION'
        assert [p['phase'] for p in phases] == ['setup', 'teardown'], 'UNKNOWN_FIXTURE_PHASES'
        assert phases[0]['outcome'] == 'failed' and phases[1]['outcome'] == 'passed', 'UNKNOWN_FIXTURE_CLEANUP'
        assert one('PHASES_VALID=') == 'false', 'UNKNOWN_FIXTURE_VALIDITY'
        selected = [r['fixture'] for r in observations if r['event'] == 'observation' and r['stage'] == 'fixture'
                    and r['fixture']['name'] == 'enable_custom_integrations']
        assert len(selected) == 1 and selected[0]['path'] == str(d / 'export/tools/issue25/conftest.py'), 'UNKNOWN_WRONG_FIXTURE_OBSERVATION'
        assert selected[0]['sha256'] == expected_data['overlay']['tools/issue25/conftest.py'], 'UNKNOWN_WRONG_FIXTURE_OBSERVATION'
    print('CONTROL_REJECTION_EXPECTED=' + case)
else:
    assert any(r['event'] == 'observation' and r['stage'] == 'initial-add-complete' and r['class_identity'] is not None
               for r in observations), 'UNKNOWN_INITIAL_ADD_OBSERVATION'
    items = admission_items if mode == 'admission' else pack_items
    assert json.loads(one('COLLECTED ')) == items and not reports, 'UNKNOWN_COLLECTION'
    assert '"category": "unknown"' not in raw.decode(), 'UNKNOWN_CATEGORY'
    assert one('PHASES_VALID=') == 'true', 'UNKNOWN_PHASE_VALIDITY'
    completion = json.loads(one('PHASES_COMPLETE '))
    assert completion['errors'] == [] and completion['exit'] == code, 'UNKNOWN_PHASE_COMPLETION'
    assert code in ((0,) if mode == 'admission' else (0, 1)), 'UNKNOWN_PROCESS_EXIT'
    assert len(phases) == len(items) * 3, 'UNKNOWN_PHASE_COUNT'
    for index, item in enumerate(items):
        group = phases[index * 3:index * 3 + 3]
        assert [p['node'] for p in group] == [item] * 3, 'UNKNOWN_PHASE_NODE'
        assert [p['phase'] for p in group] == ['setup', 'call', 'teardown'], 'UNKNOWN_PHASE_ORDER'
        assert group[0]['outcome'] == group[2]['outcome'] == 'passed', 'UNKNOWN_SETUP_TEARDOWN'
        assert all(p['category'] == 'green' for p in (group[0], group[2])), 'UNKNOWN_SETUP_CATEGORY'
        assert group[1]['outcome'] in ('passed', 'failed'), 'UNKNOWN_CALL_OUTCOME'
        assert group[1]['category'] == ('green' if group[1]['outcome'] == 'passed' else 'ownership-red'), 'UNKNOWN_CALL_CATEGORY'
    assert not re.search(r'\b(?:skipped|xfailed|xpassed|deselected)\b', raw.decode()), 'UNKNOWN_SPECIAL_RESULT'
    summaries = [line for line in lines if re.search(r'=+ .*\b(?:passed|failed)\b.*=+', line)]
    assert len(summaries) == 1, 'UNKNOWN_NATIVE_SUMMARY'
    totals = {kind: int(count) for count, kind in re.findall(r'(\d+) (passed|failed)', summaries[0])}
    failed = sum(p['phase'] == 'call' and p['outcome'] == 'failed' for p in phases)
    assert totals.get('failed', 0) == failed and sum(totals.values()) == len(items), 'UNKNOWN_NATIVE_SUMMARY'
    assert code == int(failed != 0), 'UNKNOWN_CALL_EXIT'
    wanted_classes = {p['node']: ['ownership-red'] for p in phases if p['phase'] == 'call' and p['outcome'] == 'failed'}
    assert completion['classes'] == wanted_classes, 'UNKNOWN_COMPLETION_CLASSES'
ledger = {'case': case, 'exit': code, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
          'expected_sha256': (d / 'expected.sha256').read_text().strip(),
          'control': 'expected-rejection' if negative else 'completed',
          'command_records': records('CHILD_COMMAND ')}
with (d.parent / 'supervisor.jsonl').open('a') as file:
    file.write(json.dumps(ledger, sort_keys=True) + '\n')
assert (d.parent / 'supervisor.jsonl').stat().st_size <= 65536, 'UNKNOWN_LEDGER_BUDGET'
PY_RESULT
  then result=1; break; fi
  if test "$ISSUE25_MODE" = pack && test "$code" -ne 0; then result=1; fi
done
package_snapshot after
exit "$result"
