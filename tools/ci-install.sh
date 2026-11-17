#!/usr/bin/env bash
# Install one lane's lock into a new venv at $RUNNER_TEMP/mxz-venv, then check that
# exactly the locked distributions are installed, on the lane's Python.
set -euo pipefail
lock=$1
python_lane=$2
python -m venv "$RUNNER_TEMP/mxz-venv"
# shellcheck source=/dev/null
source "$RUNNER_TEMP/mxz-venv/bin/activate"
export PIP_CONFIG_FILE=/dev/null PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_TRUSTED_HOST PYTHONPATH
# Both -r and -c are intentional: every locked distribution must be present.
python -m pip install --index-url https://pypi.org/simple -c "$lock" -r "$lock" -r requirements_test.txt
python -m pip check
python tools/check_lock.py "$lock" "$python_lane"
