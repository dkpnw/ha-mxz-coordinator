#!/usr/bin/env bash
# One ordinary public install transaction in an owned, disposable venv.
set -euo pipefail
lock=$1
expected_hash=$2
python_lane=$3
test "$(git rev-parse HEAD)" = "$GITHUB_SHA"
git diff --exit-code HEAD -- .
printf '%s  %s\n' "$expected_hash" "$lock" | sha256sum --check --strict
printf 'SOURCE\n'
git rev-parse HEAD HEAD^{tree}
git ls-tree -r HEAD
sha256sum requirements_test.txt "$lock" tools/ci-install.sh tools/pytest_phases.py tools/check_lock.py
# Print only a Boolean result, never Git credential configuration or the environment.
if git config --local --get-regexp 'http\..*extraheader|credential\.' >/dev/null; then
  echo 'FAIL: persistent checkout credential configuration'
  exit 1
fi
printf 'CHECKOUT_CREDENTIALS_PERSIST=false\n'
printf 'RUN=%s ATTEMPT=%s EVENT=%s REF=%s IMAGE=%s/%s ARCH=%s\n' \
  "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$GITHUB_EVENT_NAME" "$GITHUB_REF" \
  "$ImageOS" "$ImageVersion" "$RUNNER_ARCH"
python -VV
python -m venv "$RUNNER_TEMP/mxz-venv"
source "$RUNNER_TEMP/mxz-venv/bin/activate"
export PIP_CONFIG_FILE=/dev/null PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_TRUSTED_HOST PYTHONPATH
python -m pip --version
# Both -r and -c are intentional: every locked distribution must be present.
python -m pip install --index-url https://pypi.org/simple -v \
  --report "$RUNNER_TEMP/install.json" -c "$lock" -r "$lock" -r requirements_test.txt
cat "$RUNNER_TEMP/install.json"
python -m pip check
python -m pip list --format=freeze > "$RUNNER_TEMP/inventory.txt"
cat "$RUNNER_TEMP/inventory.txt"
python tools/check_lock.py "$lock" "$expected_hash" "$python_lane"
printf 'INSTALL_COMPLETE\n'
