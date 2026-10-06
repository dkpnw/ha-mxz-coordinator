#!/usr/bin/env bash
# Two pack invocations, no retries. Both use the same installed floor and harness.
set -euo pipefail
root=$PWD
result=0
test "$(git rev-parse HEAD)" = "$GITHUB_SHA"
git diff --exit-code HEAD -- .
python tools/check_lock.py requirements/constraints-py312-ha2024.12.0.txt \
  0d0031ddca8bb870560ca4d4bcafd34233e23e9dd6f0470dc31f7bd888eb7403 3.12.14 \
  > "$RUNNER_TEMP/issue25-inventory-before.txt"
cat "$RUNNER_TEMP/issue25-inventory-before.txt"
for base in released main; do
  case "$base" in
    released)
      commit=3a9863896f8affb6f71cbd1e495df21b17a69ff3
      tree=238636de6d067c8e05b59993819cc5187c2bf9df ;;
    main)
      commit=009b6b42326252ee633f2288573a8052f4e8c1e8
      tree=63b5af3d2f421ea5673862e90ac2cfdf8686abaf ;;
  esac
  target="$RUNNER_TEMP/issue25-$base"
  mkdir "$target"
  git archive "$commit" | tar -x -C "$target"
  mkdir -p "$target/tools/issue25"
  cp tools/pytest_phases.py "$target/tools/"
  cp tools/issue25/test_restore.py tools/issue25/conftest.py "$target/tools/issue25/"
  if bash tools/issue25/pack.sh "$root" "$target" "$base" "$commit" "$tree" \
    "$RUNNER_TEMP/issue25-inventory-before.txt" > "$RUNNER_TEMP/$base.log" 2>&1; then
    code=0
  else
    code=$?
    result=1
  fi
  # No process-wide file limit: only the complete log is budgeted.
  if test "$(wc -c < "$RUNNER_TEMP/$base.log")" -gt 2097152; then
    echo "LOG_BUDGET_EXCEEDED=$base"
    result=1
    break
  fi
  # Only ordinary ownership-red permits the other base. All gates must finish.
  if ! grep -qx "PACK_COMPLETE=$base" "$RUNNER_TEMP/$base.log" \
    || ! grep -qx 'PHASES_VALID=true' "$RUNNER_TEMP/$base.log" \
    || grep -q '"category": "unknown"' "$RUNNER_TEMP/$base.log" \
    || ! grep -qx "PACK_EXIT=$code" "$RUNNER_TEMP/$base.log" \
    || test "$code" -gt 1; then
    result=1
    break
  fi
done
exit "$result"
