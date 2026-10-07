#!/usr/bin/env bash
# Additional exact target only; the original three-lane workflow stays separate.
set -euo pipefail
mode=${1:?setup, checks, sentinel, suite or export required}
[[ ${GITHUB_RUN_ID:-} =~ ^[0-9]+$ ]]
test "${GITHUB_RUN_ATTEMPT:-}" = 1
[[ ${GITHUB_SHA:-} =~ ^[0-9a-f]{40}$ ]]
test "$(git rev-parse HEAD)" = "$GITHUB_SHA"
git diff --exit-code HEAD -- .
root="$RUNNER_TEMP/ha202610-$GITHUB_RUN_ID-a1"
export HOME="$root/home" TMPDIR="$root/tmp" XDG_CACHE_HOME="$root/cache"
export XDG_CONFIG_HOME="$root/config" XDG_DATA_HOME="$root/data"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$root/bytecode"
export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1
unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH PIP_EXTRA_INDEX_URL PIP_TRUSTED_HOST
python="$root/venv/bin/python"
case "$mode" in
  setup)
    test ! -e "$root"
    mkdir -p "$HOME" "$TMPDIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$PYTHONPYCACHEPREFIX"
    if git config --local --get-regexp 'http\..*extraheader|credential\.' >/dev/null; then
      echo 'FAIL: persistent checkout credential configuration'
      exit 1
    fi
    git rev-parse HEAD HEAD^{tree} > "$root/source.txt"
    git ls-tree -r HEAD >> "$root/source.txt"
    git ls-files -z | xargs -0 sha256sum > "$root/source-files.sha256"
    printf 'RUN=%s ATTEMPT=%s EVENT=%s REF=%s IMAGE=%s/%s ARCH=%s\n' \
      "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$GITHUB_EVENT_NAME" "$GITHUB_REF" \
      "$ImageOS" "$ImageVersion" "$RUNNER_ARCH" > "$root/runner.txt"
    cat /etc/os-release >> "$root/runner.txt"
    python3 -B tools/ha202610.py prepare "$root" > "$root/setup.log" 2>&1
    ;;
  checks)
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$python" -m pytest -p tools.pytest_phases tools/tests/test_ha202610.py -q -s -p no:cacheprovider > "$root/checks.log" 2>&1
    ;;
  sentinel|suite)
    # Keep ordinary original tests, phase helper and timing limits intact.
    if test "$mode" = sentinel; then
      args=(60s tests/test_changeover.py::test_changeover_from_temperature_sensor)
    else
      args=(180s tests/)
    fi
    result=0
    timeout --signal=TERM --kill-after=5s "${args[0]}" "$python" -m pytest \
      -p tools.pytest_phases "${args[1]}" -q -s -p no:cacheprovider > "$root/$mode.log" 2>&1 || result=$?
    printf '%s\n' "$result" > "$root/$mode.exit"
    exit "$result"
    ;;
  export)
    # Failure receipts are exported too; setup failure cannot become a green job.
    python3 -B tools/ha202610.py export "$root"
    git diff --exit-code HEAD -- .
    test -z "$(git ls-files --others --exclude-standard)"
    sha256sum --check --strict "$root/source-files.sha256"
    ;;
  *) echo 'HA202610_REFUSED=mode' >&2; exit 1 ;;
esac
