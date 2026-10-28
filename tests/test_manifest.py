"""Manifest guards.

These are cheap static checks on ``manifest.json`` — no Home Assistant needed —
that pin the metadata the HA frontend keys off when it decides *where* to file
this integration in the UI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MANIFEST_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "mxz_coordinator"
    / "manifest.json"
)

# The integrations dashboard (/config/integrations/dashboard) subscribes to
# config entries filtered by integration type -- see the HA frontend's
# ha-config-integrations.ts:
#
#     subscribeConfigEntries(..., { type: ["device", "hub", "service", "hardware"] })
#
# The Helpers tab subscribes with { type: ["helper"] }. So an entry declaring
# integration_type "helper" is filed under Helpers and is *absent* from the
# integrations dashboard, while still being reachable from its device page --
# exactly the symptom reported in issue #19. Our README tells users to manage
# this integration from Settings -> Devices & Services, so the type must stay
# in the dashboard-visible set.
DASHBOARD_VISIBLE_TYPES = {"device", "hub", "service", "hardware"}


@pytest.fixture(name="manifest", scope="module")
def manifest_fixture() -> dict:
    """The parsed integration manifest."""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_integration_type_is_visible_on_integrations_dashboard(manifest: dict) -> None:
    """The entry must appear on the integrations dashboard, not the Helpers tab (#19)."""
    assert manifest["integration_type"] in DASHBOARD_VISIBLE_TYPES


def test_manifest_has_required_metadata(manifest: dict) -> None:
    """Fields HACS/hassfest and the frontend expect are present and non-empty."""
    for key in (
        "domain",
        "name",
        "codeowners",
        "documentation",
        "iot_class",
        "issue_tracker",
        "version",
    ):
        assert manifest.get(key), f"manifest.json is missing {key!r}"

    assert manifest["config_flow"] is True


def test_manifest_declares_no_category_yet(manifest: dict) -> None:
    """``categories`` must stay out until the stable hassfest accepts it.

    Home Assistant 2026.11 adds integration categories, but CI's hassfest runs
    the current stable core, which rejects the key ("not a valid option at
    categories"). Add back ``["climate.heat_pumps_air_conditioning"]`` once the
    stable hassfest accepts it (HA 2026.11+), and flip this test to pin it.
    """
    assert "categories" not in manifest
