"""Doctor: is this harness worth driving? Read-only; writes no evidence.

* The harness blocks the network: opening a TCP connection to an address
  outside the loopback (192.0.2.1, TEST-NET-1, never a real host) raises
  pytest-socket's error inside a ``hass`` test, so no journey can reach a LAN
  device or a real Home Assistant.
* The integration this checkout ships is the one that loads: its manifest
  version, and a config entry set up over the suite's invented heads.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
import pytest_socket
from homeassistant.core import HomeAssistant

import custom_components.mxz_coordinator as component
from tests.test_drive import _setup_mock_heads
from tests.test_dwell_wakeup import _setup_dwell


async def test_the_network_is_blocked(hass: HomeAssistant) -> None:
    with (
        pytest.raises(pytest_socket.SocketBlockedError),
        socket.create_connection(("192.0.2.1", 80), timeout=1),
    ):
        pass
    # Newer plugins count blocked attempts and fail the test at teardown if any
    # were made; this one was the point, so it is not counted. Older plugins
    # keep no count.
    if (counted := getattr(pytest_socket.SocketBlockedError, "instances", None)) is not None:
        counted.clear()


async def test_this_checkout_s_integration_loads(hass: HomeAssistant) -> None:
    manifest = json.loads((Path(component.__file__).parent / "manifest.json").read_text())
    entry, head_a, head_b = await _setup_dwell(hass)
    assert entry.state.recoverable and entry.runtime_data is not None
    assert hass.states.get(head_a) is not None and hass.states.get(head_b) is not None
    print(f"DOCTOR_INTEGRATION={manifest['domain']} {manifest['version']}")


def test_mock_heads_come_from_the_suite() -> None:
    assert _setup_mock_heads.__module__ == "tests.test_drive"
