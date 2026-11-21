"""Whether the parity suites check the Home Assistant release they run on, or skip it.

``tests/test_timing_parity.py`` and ``tests/test_arms_parity.py`` compare the real coordinator
with recordings captured on each supported release. A Home Assistant dev build (the ha-canary
lane) has none, and none is ever committed: its version moves with every nightly. Both suites
skip there, for the reason this gives, and the rest of the suite still runs on it. Every other
release keeps the hard check, a beta or a stable release not yet recorded too: capture it.

No Home Assistant is loaded here.
"""

from __future__ import annotations

from packaging.version import Version


def skip_reason(release: str, releases: tuple[str, ...], *, capture: bool) -> str | None:
    """Why both parity suites skip on ``release``, or None when they check it.

    Only an unrecorded dev build skips, and not while ``MXZ_PARITY_CAPTURE=1`` records it.
    A version Home Assistant could not have (``packaging``'s ``InvalidVersion``) raises.
    """
    if capture or release in releases or not Version(release).is_devrelease:
        return None
    return (
        f"no parity recordings for Home Assistant {release}, an unrecorded dev build; "
        "the canary checks the rest of the suite; capture on the stable release"
    )
