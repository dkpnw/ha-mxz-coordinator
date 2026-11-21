"""Only an unrecorded Home Assistant dev build skips the parity suites (tests/parity_release.py).

No Home Assistant is loaded here.
"""

from __future__ import annotations

import pytest
from packaging.version import InvalidVersion

from tests.parity_release import skip_reason

# The supported releases, written out: tests/timing_parity.py's RELEASES imports Home Assistant.
SUPPORTED = ("2024.12.5", "2026.2.3", "2026.9.4", "2026.10.4", "2026.11.2")


@pytest.mark.parametrize("release", ["2026.12.0.dev0", "2027.1.0.dev20261201"])
def test_an_unrecorded_dev_build_skips_with_its_version_in_the_reason(release: str) -> None:
    assert skip_reason(release, SUPPORTED, capture=False) == (
        f"no parity recordings for Home Assistant {release}, an unrecorded dev build; "
        "the canary checks the rest of the suite; capture on the stable release"
    )


@pytest.mark.parametrize("release", SUPPORTED)
def test_negative_every_supported_release_is_checked(release: str) -> None:
    assert skip_reason(release, SUPPORTED, capture=False) is None


@pytest.mark.parametrize("release", ["2026.12.0b0", "2026.12.0rc1", "2026.12.0", "2026.11.3"])
def test_negative_an_unrecorded_beta_rc_or_stable_release_is_checked(release: str) -> None:
    """It has no recordings either, so its parity tests fail until they are captured."""
    assert skip_reason(release, SUPPORTED, capture=False) is None


def test_negative_a_recorded_dev_build_is_checked() -> None:
    assert skip_reason("2026.12.0.dev0", (*SUPPORTED, "2026.12.0.dev0"), capture=False) is None


def test_negative_a_dev_build_being_captured_is_checked() -> None:
    """``MXZ_PARITY_CAPTURE=1`` runs the scenarios, so they write this build's recordings."""
    assert skip_reason("2026.12.0.dev0", SUPPORTED, capture=True) is None


@pytest.mark.parametrize("release", ["", "dev", "2026.12.0-dev-local"])
def test_a_version_home_assistant_could_not_have_fails(release: str) -> None:
    with pytest.raises(InvalidVersion):
        skip_reason(release, SUPPORTED, capture=False)
