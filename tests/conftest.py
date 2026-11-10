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

import pytest

# Harness warnings that repeat on every test and say nothing about MXZ, keyed by logger
# name to the exact unformatted message:
# - asyncio: pytest-homeassistant-custom-component runs each test's loop in debug mode,
#   and a test that moves the clock (``freezer``, ``tests/clocks.py``) makes the step
#   that moved it look "slow", ~1 KB per line.
# - homeassistant.loader: every Home Assistant setup reports that our own integration
#   is a custom one.
STREAM_NOISE = {
    "asyncio": ("Executing %s took %.3f seconds",),
    "homeassistant.loader": (
        (
            "We found a custom integration %s which has not "
            "been tested by Home Assistant. This component might "
            "cause stability problems, be sure to disable it if you "
            "experience issues with Home Assistant"
        ),
    ),
}


def keep_on_stream(record: logging.LogRecord) -> bool:
    """False only for a record in ``STREAM_NOISE``; same text from another logger stays."""
    return record.msg not in STREAM_NOISE.get(record.name, ())


def pytest_configure(config: pytest.Config) -> None:
    """Run async tests/fixtures without per-test markers (pytest-asyncio auto-mode)."""
    if hasattr(config.option, "asyncio_mode"):
        config.option.asyncio_mode = "auto"
    # pytest-homeassistant-custom-component calls ``logging.basicConfig(level=INFO)``, so
    # under CI's ``-s`` every integration setup writes ~4 KB of HA INFO to the suite log.
    # Only that stderr stream moves to WARNING and drops ``STREAM_NOISE``; logger levels
    # and caplog's handler are untouched, so tests and the "Captured log" section of a
    # failure report still get every record.
    for handler in logging.getLogger().handlers:
        if type(handler) is logging.StreamHandler:
            handler.setLevel(logging.WARNING)
            handler.addFilter(keep_on_stream)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    """Enable the custom component, but only for tests that use ``hass``."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")
    yield
