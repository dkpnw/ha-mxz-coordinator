#!/usr/bin/env bash
# Two pack invocations, no retries. Both use the same installed floor and harness.
set -euo pipefail
root=$PWD
result=0
python tools/check_lock.py requirements/constraints-py312-ha2024.12.0.txt \
  0d0031ddca8bb870560ca4d4bcafd34233e23e9dd6f0470dc31f7bd888eb7403 3.12.14 \
  > "$RUNNER_TEMP/issue25-inventory-before.txt"
cat "$RUNNER_TEMP/issue25-inventory-before.txt"
for base in released main; do
  case "$base" in
    released) commit=3a9863896f8affb6f71cbd1e495df21b17a69ff3 ;;
    main) commit=009b6b42326252ee633f2288573a8052f4e8c1e8 ;;
  esac
  target="$RUNNER_TEMP/issue25-$base"
  mkdir "$target"
  git archive "$commit" | tar -x -C "$target"
  mkdir -p "$target/tools/issue25"
  cp tools/pytest_phases.py "$target/tools/"
  cp tools/issue25/test_restore.py tools/issue25/conftest.py "$target/tools/issue25/"
  (
    ulimit -f 2048
    echo "BASE=$base"
    git rev-parse "$commit" "$commit^{tree}"
    git ls-tree -r "$commit"
    sha256sum tools/pytest_phases.py tools/issue25/test_restore.py tools/issue25/conftest.py
    cd "$target"
    sha256sum tools/pytest_phases.py tools/issue25/test_restore.py tools/issue25/conftest.py
    find custom_components -type f -print0 | sort -z | xargs -0 sha256sum > "$RUNNER_TEMP/$base-before.sha256"
    find custom_components -type f -printf '%m %P\n' | sort > "$RUNNER_TEMP/$base-before.modes"
    set +e
    PYTHONDONTWRITEBYTECODE=1 timeout --signal=TERM --kill-after=5s 180s \
      python -m pytest -p tools.pytest_phases --confcutdir=tools/issue25 \
      tools/issue25/test_restore.py -q -s -p no:cacheprovider
    code=$?
    set -e
    echo "PACK_EXIT=$code"
    sha256sum --check --strict "$RUNNER_TEMP/$base-before.sha256"
    find custom_components -type f -print0 | sort -z | xargs -0 sha256sum > "$RUNNER_TEMP/$base-after.sha256"
    cmp "$RUNNER_TEMP/$base-before.sha256" "$RUNNER_TEMP/$base-after.sha256"
    find custom_components -type f -printf '%m %P\n' | sort > "$RUNNER_TEMP/$base-after.modes"
    cmp "$RUNNER_TEMP/$base-before.modes" "$RUNNER_TEMP/$base-after.modes"
    echo "PACK_COMPLETE=$base"
    exit "$code"
  ) > "$RUNNER_TEMP/$base.log" 2>&1 || result=1
  # An ordinary assertion red permits the other base. A fixture/phase/watchdog
  # failure stops the pack sequence; omitted cells stay UNKNOWN.
  if ! grep -qx 'PHASES_VALID=true' "$RUNNER_TEMP/$base.log" \
    || grep -q '"category": "unknown"' "$RUNNER_TEMP/$base.log" \
    || ! grep -Eq '^PACK_EXIT=[01]$' "$RUNNER_TEMP/$base.log"; then
    result=1
    break
  fi
  cd "$root"
  python tools/check_lock.py requirements/constraints-py312-ha2024.12.0.txt \
    0d0031ddca8bb870560ca4d4bcafd34233e23e9dd6f0470dc31f7bd888eb7403 3.12.14 \
    > "$RUNNER_TEMP/issue25-inventory-after.txt"
  cmp "$RUNNER_TEMP/issue25-inventory-before.txt" "$RUNNER_TEMP/issue25-inventory-after.txt"
done
exit "$result"
