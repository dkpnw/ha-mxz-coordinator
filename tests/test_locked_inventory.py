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


@pytest.mark.parametrize("field", ["name", "version"])
def test_actual_checker_rejects_missing_distribution_metadata(tmp_path, monkeypatch, field):
    from hashlib import sha256
    from types import SimpleNamespace

    from tools import check_lock

    raw = b"invented-core==1.2.3\n"
    lock = tmp_path / "lock.txt"
    lock.write_bytes(raw)
    monkeypatch.setattr(check_lock.sys, "argv", ["check", str(lock), sha256(raw).hexdigest(), "3.12.14"])
    monkeypatch.setattr(check_lock.sys, "version", "3.12.14")
    item = SimpleNamespace(metadata={"Name": None if field == "name" else "invented-core"},
                           version=None if field == "version" else "1.2.3")
    monkeypatch.setattr(check_lock, "distributions", lambda: [item])
    with pytest.raises(AssertionError, match="incomplete distribution metadata"):
        check_lock.main()


@pytest.mark.parametrize("args", [[], ["one"], ["one", "two"], ["one", "two", "three", "four"]])
def test_actual_checker_rejects_bad_arguments(monkeypatch, args):
    from tools import check_lock

    monkeypatch.setattr(check_lock.sys, "argv", ["check", *args])
    with pytest.raises(ValueError):
        check_lock.main()
