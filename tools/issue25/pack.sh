#!/usr/bin/env bash
# One export, one child, no fallback or retry.
set -euo pipefail
refuse() { echo "ISSUE25_REFUSED=$1" >&2; exit 1; }
# Reject selectors before touching arguments, Git or inventory on either pack ref.
case "${GITHUB_REF:-}" in
  refs/heads/ci/issue25-harness-replacement|refs/heads/ci/issue25-harness-repeat)
    test "${ISSUE25_INJECTION+x}" != x || refuse injection ;;
esac
test "$#" -eq 7 || refuse arguments
root=$1 target=$2 base=$3 commit=$4 tree=$5 inventory=$6 expected=$7
# Validators always use the real system stdlib interpreter, never the fake child.
validation=$(/usr/bin/python3 - "$@" <<'PY_INPUT'
import hashlib
import json
import os
import re
import sys
from pathlib import Path


def refuse(reason):
    raise SystemExit('ISSUE25_REFUSED=' + reason)


def require(condition, reason):
    if not condition:
        refuse(reason)


def canonical(value, reason):
    p = Path(value)
    require(p.is_absolute() and str(p.resolve()) == value, reason)
    require(not any(q.is_symlink() for q in (p, *p.parents)), reason)
    return p


def unique(pairs):
    out = {}
    for key, value in pairs:
        require(key not in out, 'duplicate-json-key')
        out[key] = value
    return out


root, target, base, commit, tree, inventory, filename = sys.argv[1:]
require(base in ('released', 'main'), 'base')
mode = os.environ.get('ISSUE25_MODE')
require(mode in ('admission', 'pack'), 'mode')
refs = {'refs/heads/ci/issue25-harness-admission': ('A3', 'admission'),
        'refs/heads/ci/issue25-harness-replacement': ('R2', 'pack'),
        'refs/heads/ci/issue25-harness-repeat': ('I1', 'pack')}
ref = os.environ.get('GITHUB_REF')
require(ref in refs, 'ref')
require(refs[ref][1] == mode, 'mode-ref')
require(os.environ.get('GITHUB_EVENT_NAME') == 'push', 'event')
require(os.environ.get('GITHUB_RUN_ATTEMPT') == '1', 'attempt')
require(re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID', '')), 'run-id')
for key, value, reason in (('REPOSITORY_PRIVATE', 'false', 'repository'),
                           ('RUNNER_ENVIRONMENT', 'github-hosted', 'runner'),
                           ('RUNNER_ARCH', 'X64', 'architecture'),
                           ('GITHUB_REPOSITORY', 'dkpnw/ha-mxz-coordinator', 'repository-name')):
    require(os.environ.get(key) == value, reason)
require(not os.environ.get('GITHUB_HEAD_REF') and not os.environ.get('GITHUB_BASE_REF'), 'pr-context')
require(re.fullmatch(r'[0-9a-f]{40}', os.environ.get('GITHUB_SHA', '')), 'source')
require(mode != 'pack' or 'ISSUE25_INJECTION' not in os.environ, 'injection')
for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD'):
    require(key not in os.environ, 'override-' + key)
allowed = {'ISSUE25_MODE', 'ISSUE25_CASE_ID', 'ISSUE25_INJECTION'}
require(not {k for k in os.environ if k.startswith('ISSUE25_')} - allowed, 'control-key')
case = os.environ.get('ISSUE25_CASE_ID')
require(bool(case), 'case')
p = canonical(filename, 'expected-path')
require(p.name == 'expected.json' and p.is_file(), 'expected')
raw = p.read_bytes()
sha_file = canonical(str(p.with_name('expected.sha256')), 'expected-digest')
promised = sha_file.read_bytes()
require(re.fullmatch(rb'[0-9a-f]{64}\n', promised), 'expected-digest')
require(hashlib.sha256(raw).hexdigest().encode() + b'\n' == promised, 'expected-digest')
try:
    data = json.loads(raw, object_pairs_hook=unique)
except (ValueError, UnicodeError):
    refuse('malformed-expected')
fields = {'schema_version', 'stage', 'run_id', 'attempt', 'event', 'ref', 'source_commit',
          'source_tree', 'workflow_blob', 'case_id', 'mode', 'base', 'base_commit', 'base_tree',
          'checkout', 'export', 'temp', 'expected_config', 'actual_config', 'python',
          'base_manifest', 'product_manifest', 'product_paths', 'overlay', 'config_sha256',
          'fixture_manifest', 'inventory', 'items', 'phases', 'deadline', 'injection',
          'exception', 'reason', 'exit', 'fault_paths'}
require(type(data) is dict and set(data) == fields, 'expected-schema')
require(type(data['schema_version']) is int and data['schema_version'] == 1, 'expected-schema')
for key, value, reason in (('base_commit', commit, 'base-commit'), ('base_tree', tree, 'base-tree'),
                          ('checkout', root, 'checkout'), ('export', target, 'destination'),
                          ('base', base, 'base'), ('mode', mode, 'mode'), ('case_id', case, 'case'),
                          ('ref', ref, 'ref'), ('stage', refs[ref][0], 'stage'),
                          ('source_commit', os.environ['GITHUB_SHA'], 'source'),
                          ('run_id', os.environ['GITHUB_RUN_ID'], 'run-id'), ('attempt', '1', 'attempt'),
                          ('event', 'push', 'event')):
    require(data[key] == value, reason)
require(re.fullmatch(r'[0-9a-f]{40}', commit) and re.fullmatch(r'[0-9a-f]{40}', tree), 'base-identity')
require(re.fullmatch(r'[0-9a-f]{40}', data['source_tree']) and re.fullmatch(r'[0-9a-f]{40}', data['workflow_blob']), 'source-identity')
source = canonical(root, 'checkout')
export = canonical(target, 'destination')
require(export == p.parent / 'export' and source != export and export.is_dir(), 'destination')
require(data['temp'] == str(p.parent / 'tmp') and not Path(data['temp']).exists(), 'preexisting-child-tmp')
require(data['expected_config'] == str(export / 'tools/issue25/pytest.ini'), 'expected-config')
require(not Path(data['expected_config']).exists() and not Path(data['expected_config']).is_symlink(), 'preexisting-config')
config_bytes = b'[pytest]\nasyncio_mode = auto\n'
require(data['config_sha256'] == hashlib.sha256(config_bytes).hexdigest(), 'config-bytes')
rows = {'P-release': ('released', None, 0, None, None), 'P-main': ('main', None, 0, None, None),
        'N-missing': ('released', 'missing', 4, 'ModuleNotFoundError', 'UNKNOWN_COLLECTION_CONTRACT'),
        'N-origin': ('released', 'origin', 4, 'AssertionError', 'UNKNOWN_COLLECTION_ORIGIN'),
        'N-config': ('released', 'config', 4, 'UsageError', 'UNKNOWN_CONFIG_PATH'),
        'N-fixture': ('released', 'fixture', 2, 'AssertionError', 'UNKNOWN_FIXTURE_DEFINITION')}
if mode == 'admission':
    require(case in rows, 'case')
    wanted = rows[case]
else:
    require(case == base, 'case')
    wanted = (base, None, None, None, None)
require(tuple(data[k] for k in ('base', 'injection', 'exit', 'exception', 'reason')) == wanted, 'case-contract')
injection = data['injection']
if injection is None:
    require('ISSUE25_INJECTION' not in os.environ, 'injection')
else:
    require(os.environ.get('ISSUE25_INJECTION') == injection, 'injection')
require(data['fault_paths'] == {
    'decoy': str(p.parent / 'decoy/custom_components/mxz_coordinator') if injection == 'origin' else None,
    'alternate_config': str(p.parent / 'alternate/pytest.ini') if injection == 'config' else None}, 'fault-paths')
require(data['actual_config'] == (data['fault_paths']['alternate_config'] or data['expected_config']), 'actual-config')
require(data['deadline'] == ('60s' if mode == 'admission' else '180s'), 'deadline')
require(data['phases'] == ['setup', 'call', 'teardown'], 'phases')
items = ['tools/issue25/test_setup_admission.py::test_exported_setup'] if mode == 'admission' else [
    'tools/issue25/test_restore.py::test_restore_schedule[' + name + ']' for name in (
        '01-clean-twice', '02-unavailable-twice', '03-manual-twin', '04-options-reload',
        '05-missing-restore-negative', '06-changed-active-demand', '07-provisional-auto-old-echo',
        '08-missing-speed-recovery')]
require(data['items'] == items, 'items')
for name, basename in (('base_manifest', 'base.tree'), ('product_manifest', 'product.tree'),
                       ('product_paths', 'product.paths'), ('fixture_manifest', 'fixture-files.json')):
    entry = data[name]
    require(type(entry) is dict and set(entry) == {'path', 'sha256'}, name)
    require(entry['path'] == str(p.parent / basename), name)
    file = canonical(entry['path'], name)
    require(file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest() == entry['sha256'], name)
require(data['inventory']['path'] == inventory, 'inventory')
require(hashlib.sha256(canonical(inventory, 'inventory').read_bytes()).hexdigest() == data['inventory']['sha256'], 'inventory')
overlay = ('tools/pytest_phases.py', 'tools/issue25/test_restore.py',
           'tools/issue25/conftest.py', 'tools/issue25/test_setup_admission.py')
require(set(data['overlay']) == set(overlay), 'harness-manifest')
for name in overlay:
    require(hashlib.sha256(canonical(str(source / name), 'harness').read_bytes()).hexdigest() == data['overlay'][name], 'harness-manifest')
    require(hashlib.sha256(canonical(str(export / name), 'harness').read_bytes()).hexdigest() == data['overlay'][name], 'harness-manifest')
python = str(canonical(os.environ['RUNNER_TEMP'], 'runner-temp') / 'mxz-venv/bin/python')
require(data['python'] == python, 'python')
harness_bytes = ''.join(data['overlay'][name] + '  ' + name + '\n' for name in overlay).encode()
require((p.parent / 'harness.sha256').read_bytes() == harness_bytes, 'harness-manifest')
config_manifest = (data['config_sha256'] + '  ' + data['expected_config'] + '\n')
if injection == 'config':
    config_manifest += data['config_sha256'] + '  ' + data['actual_config'] + '\n'
require((p.parent / 'config.sha256').read_bytes() == config_manifest.encode(), 'config-manifest')
for value in (mode, case, promised.decode().strip(), data['actual_config'], data['deadline'],
              items[0].split('::')[0], injection or ''):
    print(value)
for name in ('base.tree', 'product.tree', 'product.paths', 'fixture-files.json', 'harness.sha256', 'config.sha256'):
    print(hashlib.sha256((p.parent / name).read_bytes()).hexdigest())
print(data['inventory']['sha256'])
PY_INPUT
) || exit 1
mapfile -t fields <<< "$validation"
mode=${fields[0]} case_id=${fields[1]} expected_hash=${fields[2]}
config=${fields[3]} deadline=${fields[4]} item=${fields[5]}
child=${expected%/expected.json}
cd "$root" || refuse checkout
test "$(git rev-parse --verify "$commit^{commit}")" = "$commit" || refuse commit
test "$(git rev-parse --verify "$commit^{tree}")" = "$tree" || refuse tree
# Compare independently supplied complete manifests against actual Git objects.
test "$(git ls-tree -r "$commit")" = "$(cat "$child/base.tree")" || refuse base-manifest
test "$(git ls-tree -r "$commit" -- custom_components)" = "$(cat "$child/product.tree")" || refuse product-manifest
printf '[pytest]\nasyncio_mode = auto\n' > "$target/tools/issue25/pytest.ini"
if test "${ISSUE25_INJECTION:-}" = config; then
  mkdir "$child/alternate"
  printf '[pytest]\nasyncio_mode = auto\n' > "$child/alternate/pytest.ini"
fi
mkdir "$child/tmp" || refuse preexisting-child-tmp
cd "$target" || refuse destination
check_inputs() {
  local actual index=7 name
  actual=$(sha256sum "$expected") || return 1
  test "${actual%% *}" = "$expected_hash" || return 1
  test "$(cat "$child/expected.sha256")" = "$expected_hash" || return 1
  test "$(wc -c < "$child/expected.sha256")" -eq 65 || return 1
  for name in base.tree product.tree product.paths fixture-files.json harness.sha256 config.sha256; do
    actual=$(sha256sum "$child/$name") || return 1
    test "${actual%% *}" = "${fields[$index]}" || return 1
    index=$((index + 1))
  done
  actual=$(sha256sum "$inventory") || return 1
  test "${actual%% *}" = "${fields[13]}" || return 1
}
check_decoy() (
  test "${ISSUE25_INJECTION:-}" = origin || return 0
  cd "$child/decoy" || return 1
  cmp "$child/product.tree" "$child/decoy.tree" || return 1
  test "$(find custom_components -mindepth 1 ! -type d -print | sort)" = "$(cat "$child/product.paths")" || return 1
  while read -r mode kind blob path; do
    test "$kind" = blob && test -f "$path" && test ! -L "$path" || return 1
    permissions=$(stat -c '%a' "$path") || return 1
    case "$mode" in
      100644) test "$((8#$permissions & 0100))" -eq 0 || return 1 ;;
      100755) test "$((8#$permissions & 0100))" -ne 0 || return 1 ;;
      *) return 1 ;;
    esac
    test "$(git hash-object --no-filters "$path")" = "$blob" || return 1
  done < "$child/product.tree"
)
check_product() {
  local phase=$1 permissions mode kind blob path
  test -d custom_components && test ! -L custom_components || return 1
  find custom_components -mindepth 1 ! -type d -print | sort > "$child/product.$phase.txt" || return 1
  cmp "$child/product.paths" "$child/product.$phase.txt" || return 1
  while read -r mode kind blob path; do
    test "$kind" = blob && test -f "$path" && test ! -L "$path" || return 1
    permissions=$(stat -c '%a' "$path") || return 1
    case "$mode" in
      100644) test "$((8#$permissions & 0100))" -eq 0 || return 1 ;;
      100755) test "$((8#$permissions & 0100))" -ne 0 || return 1 ;;
      *) return 1 ;;
    esac
    test "$(git hash-object --no-filters "$path")" = "$blob" || return 1
  done < "$child/product.tree"
  sha256sum --check --strict "$child/harness.sha256" || return 1
  sha256sum --check --strict "$child/config.sha256" || return 1
}
check_inventory() {
  "$RUNNER_TEMP/mxz-venv/bin/python" "$root/tools/check_lock.py" \
    "$root/requirements/constraints-py312-ha2024.12.0.txt" \
    0d0031ddca8bb870560ca4d4bcafd34233e23e9dd6f0470dc31f7bd888eb7403 3.12.14 \
    > "$child/inventory.$1.txt" || return 1
  cat "$child/inventory.$1.txt" || return 1
  cmp "$inventory" "$child/inventory.$1.txt" || return 1
}
check_inputs || refuse input-pre
check_decoy || refuse decoy-pre
check_product pre || refuse product-pre
check_inventory pre || refuse inventory-pre
child_env=("PATH=$RUNNER_TEMP/mxz-venv/bin:/usr/bin:/bin" LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC
  "TMPDIR=$child/tmp" PYTHONDONTWRITEBYTECODE=1 "ISSUE25_MODE=$mode" "ISSUE25_BASE=$base"
  "ISSUE25_CASE_ID=$case_id" "ISSUE25_EXPECTED=$expected" "ISSUE25_EXPECTED_SHA256=$expected_hash"
  GITHUB_REPOSITORY=dkpnw/ha-mxz-coordinator GITHUB_EVENT_NAME=push "GITHUB_REF=$GITHUB_REF"
  GITHUB_HEAD_REF= GITHUB_BASE_REF= "GITHUB_RUN_ID=$GITHUB_RUN_ID" GITHUB_RUN_ATTEMPT=1
  "GITHUB_SHA=$GITHUB_SHA" REPOSITORY_PRIVATE=false RUNNER_ENVIRONMENT=github-hosted RUNNER_ARCH=X64)
if test "${ISSUE25_INJECTION+x}" = x; then child_env+=("ISSUE25_INJECTION=$ISSUE25_INJECTION"); fi
printf 'CHILD_COMMAND'
printf ' %q' /usr/bin/env -i "${child_env[@]}" /usr/bin/timeout --signal=TERM --kill-after=5s "$deadline" \
  "$RUNNER_TEMP/mxz-venv/bin/python" -m pytest -p tools.pytest_phases --import-mode=prepend \
  "--rootdir=$target" -c "$config" "--confcutdir=$target/tools/issue25" "$item" -s -p no:cacheprovider
printf '\n'
code=0
/usr/bin/env -i "${child_env[@]}" /usr/bin/timeout --signal=TERM --kill-after=5s "$deadline" \
  "$RUNNER_TEMP/mxz-venv/bin/python" -m pytest -p tools.pytest_phases --import-mode=prepend \
  "--rootdir=$target" -c "$config" "--confcutdir=$target/tools/issue25" "$item" -s -p no:cacheprovider || code=$?
printf '%s\n' "$code" > "$child/exit.txt"
if test "$mode" = admission; then echo "CHILD_EXIT=$code"; else echo "PACK_EXIT=$code"; fi
check_inputs || refuse input-post
check_decoy || refuse decoy-post
check_product post || refuse product-post
check_inventory post || refuse inventory-post
if test "$mode" = admission; then echo "CHILD_COMPLETE=$case_id"; else echo "PACK_COMPLETE=$base"; fi
exit "$code"
