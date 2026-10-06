"""Version locks reject missing, extra, changed and duplicate distributions."""

import pytest

from tools.check_lock import identity_errors, inventory_errors

EXPECTED = [("invented-core", "1.2.3"), ("invented-plugin", "4.5.6")]
ACTUAL = [*EXPECTED, ("pip", "25.0")]


def test_exact_inventory_and_named_bootstrap():
    assert inventory_errors(EXPECTED, ACTUAL) == []
    assert inventory_errors(EXPECTED, [("invented_core", "1.2.3"), *ACTUAL[1:]]) == []


@pytest.mark.parametrize("actual", [
    [], ACTUAL[:1], ACTUAL[1:], EXPECTED,
    [*ACTUAL, ("invented.extra", "1")],
    [*ACTUAL, ("invented_core", "1.2.3")],
    [("invented-core", "1-2-3"), *ACTUAL[1:]],
    [*ACTUAL, ("setuptools", "1")],
])
def test_wrong_inventory_rejects(actual):
    assert inventory_errors(EXPECTED, actual)


def test_empty_or_duplicate_lock_rejects():
    assert inventory_errors([], ACTUAL)
    assert inventory_errors([*EXPECTED, ("invented_core", "1.2.3")], ACTUAL)


def test_wrong_or_missing_lock_and_python_identity():
    digest = "a" * 64  # Invented identity; no package or runtime lookup.
    assert identity_errors(digest, digest, "3.12.14", "3.12.14") == []
    assert identity_errors("b" * 64, digest, "3.12.14", "3.12.14")
    assert identity_errors(None, None, "3.12.14", "3.12.14")
    assert identity_errors(digest, digest, "3.13.1", "3.12.14")
    assert identity_errors(digest, digest, None, "3.12.14")
    assert identity_errors(digest, digest, "3.12.14", "unknown")
