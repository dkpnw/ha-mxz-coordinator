"""The Home Assistant test helpers, from the test plugin this lane installs.

Lanes up to HA 2026.10 install ``pytest-homeassistant-custom-component``. From 2026.11 the
plugin is Home Assistant's own ``pytest-homeassistant``. Both ship the same helpers in a
``common`` module and register their fixtures through their own pytest entry point. Tests
import the helpers from here, so a lane with neither plugin fails at import, never skips.

The custom-component plugin comes first, as in ``tests/restore_lifecycle.py``. The
official one is used only when the custom-component module itself is absent; any other
import error is raised as is.
"""

try:
    from pytest_homeassistant_custom_component import common
except ModuleNotFoundError as missing:
    if missing.name != "pytest_homeassistant_custom_component":
        raise
    from pytest_homeassistant import common

MockConfigEntry = common.MockConfigEntry
MockModule = common.MockModule
MockPlatform = common.MockPlatform
async_fire_time_changed = common.async_fire_time_changed
async_fire_time_changed_exact = common.async_fire_time_changed_exact
async_mock_load_restore_state_from_storage = common.async_mock_load_restore_state_from_storage
async_mock_restore_state_shutdown_restart = common.async_mock_restore_state_shutdown_restart
async_mock_service = common.async_mock_service
flush_store = common.flush_store
mock_integration = common.mock_integration
mock_platform = common.mock_platform
mock_restore_cache = common.mock_restore_cache
mock_restore_cache_with_extra_data = common.mock_restore_cache_with_extra_data
