"""Pytest fixtures.

The pure-logic tests (``test_logic.py``) need no Home Assistant. The integration
tests need ``pytest-homeassistant-custom-component``; when that plugin is installed we
auto-enable custom integrations for every test, otherwise we stay out of the way so the
pure tests still run on a bare ``pytest``.
"""

from __future__ import annotations

import pytest

try:  # pragma: no cover - import guard
    import pytest_homeassistant_custom_component  # noqa: F401

    _HAS_HA = True
except ImportError:  # pragma: no cover
    _HAS_HA = False


if _HAS_HA:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Allow loading the custom component in every HA test."""
        yield
