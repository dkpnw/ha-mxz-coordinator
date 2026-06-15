"""Pytest fixtures and configuration.

The pure-logic tests (``test_logic.py``) need no Home Assistant. The integration
tests need ``pytest-homeassistant-custom-component`` and the ``hass`` fixture; for
those we enable loading the custom integration. We only pull in
``enable_custom_integrations`` for tests that actually request ``hass`` so the pure
tests still run on a bare ``pytest`` (and don't spin up Home Assistant needlessly).

``pytest_configure`` defaults pytest-asyncio to auto-mode so the HA async fixtures
(``hass`` et al.) resolve without contributors needing a separate pytest.ini.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Run async tests/fixtures without per-test markers (pytest-asyncio auto-mode)."""
    if hasattr(config.option, "asyncio_mode"):
        config.option.asyncio_mode = "auto"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    """Enable the custom component, but only for tests that use ``hass``."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")
    yield
