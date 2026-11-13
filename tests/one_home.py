"""Each key an entry stores lives in one store (entry v2.3), written out apart from settings.py.

Data holds what setup and Reconfigure answer (the rooms, the drift-alert service) and the
unit the saved temperatures were last converted to. Options hold every Configure setting.
"""

from __future__ import annotations

from typing import Any

from custom_components.mxz_coordinator.const import (
    CONF_NOTIFY_SERVICE,
    CONF_SAVED_TEMP_UNIT,
    CONF_ZONES,
)

DATA_KEYS = frozenset({CONF_ZONES, CONF_NOTIFY_SERVICE, CONF_SAVED_TEMP_UNIT})


def assert_one_home(entry: Any) -> None:
    """Data holds only the data keys, and options hold none of them."""
    assert set(entry.data) <= DATA_KEYS, sorted(set(entry.data) - DATA_KEYS)
    assert not set(entry.options) & DATA_KEYS, sorted(set(entry.options) & DATA_KEYS)
