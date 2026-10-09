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

import logging
import sys

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Run async tests/fixtures without per-test markers (pytest-asyncio auto-mode)."""
    if hasattr(config.option, "asyncio_mode"):
        config.option.asyncio_mode = "auto"
    # pytest-homeassistant-custom-component calls ``logging.basicConfig(level=INFO)``, so
    # under CI's ``-s`` every integration setup writes ~4 KB of HA INFO to the suite log.
    # Only that stderr stream moves to WARNING; logger levels stay, so caplog and the
    # "Captured log" section of a failure report still get INFO.
    for handler in logging.getLogger().handlers:
        if type(handler) is logging.StreamHandler:
            handler.setLevel(logging.WARNING)
    # CI redirects ``-s`` stdout and stderr into one log. Block-buffered stdout keeps
    # the newline of the 130 KB phase-helper COLLECTED line back while HA logs to
    # stderr, so the JSON evidence line gains a log record. Flush stdout per line.
    if config.option.capture == "no":
        sys.stdout.reconfigure(line_buffering=True)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    """Enable the custom component, but only for tests that use ``hass``."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")
    yield
