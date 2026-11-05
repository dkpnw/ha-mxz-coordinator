"""Open findings run outside tests/: CI's phase plugin refuses an xfail there."""

from tests.conftest import auto_enable_custom_integrations, pytest_configure

__all__ = ["auto_enable_custom_integrations", "pytest_configure"]
