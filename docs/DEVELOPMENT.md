# Developer setup and tested support

For contributors. Users only need the [README](../README.md). Before you open a pull
request, read [CONTRIBUTING.md](../CONTRIBUTING.md); to cut a release, follow
[RELEASING.md](RELEASING.md).

## Tested support

The declared HA floor is 2024.12.0. The full test suite runs on these exact targets with
invented entities; it covers software behavior, not physical behavior on equipment.

| Python | Home Assistant | Dependency setup |
| --- | --- | --- |
| 3.12.14 | 2024.12.0 | `requirements/constraints-py312-ha2024.12.0.txt` |
| 3.13.16 | 2026.2.3 | `requirements/constraints-py313-ha2026.2.3.txt` |
| 3.14.8 | 2026.9.0 | `requirements/constraints-py314-ha2026.9.0.txt` |
| 3.14.8 | 2026.10.0 | [Exact generated stable test environment](../requirements/ha2026.10/README.md) |

- The first three locks belong to the existing CI lanes. Each lane, and the .10
  qualification, also runs the full suite a second time with `-p tools.fakeboot` and
  `FAKE_UPTIME=120`, on a clock that reads as if the runner had booted two minutes ago.
  The mode-change wait, the coil-dry wait, and the floor's own clocks are timed on the
  monotonic clock, which counts from boot, so a test that only passes on a long-running
  host fails there.
- The .10 recipe uses public immutable core/plugin inputs, a pinned Python archive, and
  actual locally built artifact hashes. Public plugin `0.13.370` requires beta4;
  installing it by version alone does not reproduce the stable .10 environment.
- Use the linked recipe on Linux x86-64 with glibc 2.41 or newer. Its local evidence
  does not establish genuine hosted qualification.
- The form screenshots were captured on HA 2026.10.0 only; the floor's rendering of the
  forms was not captured.
- This source does not implement the separate HA 2026.11 restore changes, and no .11
  support claim follows from .10 results.

## Run the 2024.12.0 floor locally

For a small local floor environment, use a new work directory outside the checkout
and an installed Python 3.12.14. This command is for your development machine; it does
not impersonate GitHub runner context or run the CI-only installer.

```bash
work="$HOME/mxz-floor-work"
mkdir -p "$work/home" "$work/tmp" "$work/cache"
export HOME="$work/home" TMPDIR="$work/tmp" XDG_CACHE_HOME="$work/cache"
export PYTHONDONTWRITEBYTECODE=1 PIP_CONFIG_FILE=/dev/null
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_TRUSTED_HOST PYTHONPATH
python3.12 -m venv "$work/venv"
"$work/venv/bin/python" -m pip install --index-url https://pypi.org/simple \
  -c requirements/constraints-py312-ha2024.12.0.txt \
  -r requirements/constraints-py312-ha2024.12.0.txt -r requirements_test.txt
"$work/venv/bin/python" -m pip check
"$work/venv/bin/python" tools/check_lock.py \
  requirements/constraints-py312-ha2024.12.0.txt \
  0d0031ddca8bb870560ca4d4bcafd34233e23e9dd6f0470dc31f7bd888eb7403 3.12.14
"$work/venv/bin/python" -m pytest -p tools.pytest_phases tests/ -q -s -p no:cacheprovider
FAKE_UPTIME=120 "$work/venv/bin/python" -m pytest -p tools.fakeboot -p tools.pytest_phases \
  tests/ -q -s -p no:cacheprovider
"$work/venv/bin/ruff" check --no-cache custom_components/ tests/ tools/
```

Use the floor when your platform cannot run the exact .10 bundle, and label the result
with its actual floor versions. It does not validate .10. Do not disable ordinary HA
plugin loading, replace failed phases with collection, or infer hardware results from
mock entities.


## Restore, flow schemas, and abort text

Restore uses normal `RestoreEntity` state/extra data; flow schemas use Voluptuous. MXZ
raises its own `reconfigure_successful` and `already_configured` aborts without HA's
central translation domain (the floor has no such parameter), so both strings ship
locally in `strings.json`/`translations/en.json`. Tests resolve every flow reason
through HA's translation loader on each lane.

On HA 2026.10.0 (frontend 20260930.2) the real frontend rendered the local text,
“Reconfiguration was successful.” with its trailing period, not HA's central text,
which has none ([screenshot](../images/mxz-3.4.0-reconfigure-complete-c1007a.png)). The
floor's rendering of local abort text was not captured, and `already_configured` was
not rendered.
