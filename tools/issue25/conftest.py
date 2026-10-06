"""Require exactly the frozen eight cells, in discriminator-first order."""

import pytest

EXPECTED = [f"test_restore.py::test_restore_schedule[{name}]" for name in (
    "01-clean-twice", "02-unavailable-twice", "03-manual-twin", "04-options-reload",
    "05-missing-restore-negative", "06-changed-active-demand",
    "07-provisional-auto-old-echo", "08-missing-speed-recovery",
)]


def pytest_collection_finish(session):
    actual = [item.nodeid.split("tools/issue25/")[-1] for item in session.items]
    if actual != EXPECTED:
        raise pytest.UsageError(f"UNKNOWN: frozen cells changed: {actual!r}")
