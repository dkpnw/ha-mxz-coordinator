"""Missing ordinary results must never become a passing diagnostic."""

import pytest

from tools.pytest_phases import phase_errors

GOOD = [("setup", "passed", False), ("call", "passed", False), ("teardown", "passed", False)]


def test_complete_phases():
    assert phase_errors(["invented"], {"invented": GOOD}) == []


@pytest.mark.parametrize("phases", [
    GOOD[:2], GOOD[1:], GOOD[::2], GOOD + [GOOD[-1]], list(reversed(GOOD)),
    [("setup", "passed", False), ("call", "skipped", False), GOOD[-1]],
    [("setup", "passed", False), ("call", "passed", True), GOOD[-1]],
])
def test_incomplete_or_special_phases_reject(phases):
    assert phase_errors(["invented"], {"invented": phases})


def test_missing_and_extra_cells_reject():
    assert phase_errors([], {})
    assert phase_errors(["invented"], {})
    assert phase_errors(["invented", "invented"], {"invented": GOOD})
    assert phase_errors(["invented"], {"invented": GOOD, "unexpected": GOOD})
