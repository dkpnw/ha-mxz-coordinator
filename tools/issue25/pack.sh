#!/usr/bin/env bash
# One exported product; every evidence gate terminates explicitly on failure.
set -euo pipefail
export LC_ALL=C
root=$1
target=$2
base=$3
commit=$4
tree=$5
inventory=$6
cd "$root" || exit 1
test "$(git rev-parse --verify "$commit^{commit}")" = "$commit" || exit 1
test "$(git rev-parse --verify "$commit^{tree}")" = "$tree" || exit 1
echo "BASE=$base"
git ls-tree -r "$commit" || exit 1
printf 'COMMIT=%s TREE=%s\n' "$commit" "$tree"
git ls-tree -r --name-only "$commit" -- custom_components > "$RUNNER_TEMP/$base-paths" || exit 1
test -s "$RUNNER_TEMP/$base-paths" || exit 1
git ls-tree -r "$commit" -- custom_components > "$RUNNER_TEMP/$base-tree" || exit 1
sha256sum tools/pytest_phases.py tools/issue25/test_restore.py tools/issue25/conftest.py \
  > "$RUNNER_TEMP/$base-harness.sha256" || exit 1
cat "$RUNNER_TEMP/$base-harness.sha256" || exit 1
cd "$target" || exit 1
test "$(pwd -P)" = "$target" || exit 1
test "$target" != "$root" || exit 1

check_product() {
  test -d custom_components && test ! -L custom_components || return 1
  find custom_components -mindepth 1 ! -type d -print | sort \
    > "$RUNNER_TEMP/$base-actual-paths" || return 1
  cmp "$RUNNER_TEMP/$base-paths" "$RUNNER_TEMP/$base-actual-paths" || return 1
  while read -r mode kind blob path; do
    test "$kind" = blob && test -f "$path" && test ! -L "$path" || return 1
    permissions=$(stat -c '%a' "$path") || return 1
    case "$mode" in
      100644) test "$((8#$permissions & 0100))" -eq 0 || return 1 ;;
      100755) test "$((8#$permissions & 0100))" -ne 0 || return 1 ;;
      *) return 1 ;;
    esac
    test "$(git hash-object --no-filters "$path")" = "$blob" || return 1
  done < "$RUNNER_TEMP/$base-tree"
  sha256sum --check --strict "$RUNNER_TEMP/$base-harness.sha256" || return 1
}

check_inventory() {
  python "$root/tools/check_lock.py" "$root/requirements/constraints-py312-ha2024.12.0.txt" \
    0d0031ddca8bb870560ca4d4bcafd34233e23e9dd6f0470dc31f7bd888eb7403 3.12.14 \
    > "$RUNNER_TEMP/$base-inventory.txt" || return 1
  cat "$RUNNER_TEMP/$base-inventory.txt" || return 1
  cmp "$inventory" "$RUNNER_TEMP/$base-inventory.txt" || return 1
}

check_product || exit 1
check_inventory || exit 1
# Capture only pytest's status. Integrity gates never share its allowed red exit.
set +e
ISSUE25_BASE="$base" PYTHONDONTWRITEBYTECODE=1 timeout --signal=TERM --kill-after=5s 180s \
  python -m pytest -p tools.pytest_phases --confcutdir=tools/issue25 \
  tools/issue25/test_restore.py -q -s -p no:cacheprovider
code=$?
set -e
echo "PACK_EXIT=$code"
check_product || exit 1
check_inventory || exit 1
echo "PACK_COMPLETE=$base"
exit "$code"
