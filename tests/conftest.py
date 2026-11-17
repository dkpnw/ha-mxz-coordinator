"""Pytest fixtures and configuration.

The pure-logic tests (``test_logic.py``) need no Home Assistant. The integration
tests need a Home Assistant test plugin (``tests/ha_test.py``) and the ``hass`` fixture; for
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
        request.getfixturevalue("mxz_clock")
    yield


@pytest.fixture
def mxz_clock(monkeypatch: pytest.MonkeyPatch):
    """The time every coordinator set up in this test reads on ``coord.clock``.

    Each coordinator's clock (``tests.clocks.Clock``) reads the production
    clock plus whatever time the test makes pass, so a test that never does
    sees exactly the clock a real coordinator reads, and it logs every wait.
    """
    from functools import partial

    from custom_components.mxz_coordinator import coordinator
    from tests.clocks import Clock, Time

    time = Time()
    monkeypatch.setattr(coordinator, "Clock", partial(Clock, time=time))
    return time
