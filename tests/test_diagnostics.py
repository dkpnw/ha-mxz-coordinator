"""Download diagnostics: the shape, the redaction, and that a download never fails.

Every room, head, sensor, and secret here is invented.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pycares
import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator import diagnostics
from custom_components.mxz_coordinator.const import (
    CONF_NOTIFY_SERVICE,
    CONF_ZONES,
    DOMAIN,
    VOTE_STALE,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
)
from custom_components.mxz_coordinator.diagnostics import (
    _is_sensitive,
    _jsonable,
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from tests.test_door_window import Rig, _setup
from tests.test_setup import _setup as _setup_plain
from tests.test_startup_continuity import _head

TOP_LEVEL = {
    "generated_at",
    "integration",
    "home_assistant",
    "config_entry",
    "coordinator",
    "rooms",
    "repair_issues",
}
ROOM = {
    "name",
    "index",
    "head_type",
    "entity_ids",
    "settings",
    "mode",
    "capabilities",
    "votes",
    "holds",
    "manual_fan",
    "window_pause",
    "anti_flip",
    "sensor_health",
    "cloud",
    "last_decision",
}
COORDINATOR = {
    "retired",
    "plan_ready",
    "last_update_success",
    "started_at",
    "enabled",
    "shared_mode",
    "eco_idle",
    "heat_lockout",
    "cool_lockout",
    "inhibited",
    "inhibit_action",
    "idle_action",
    "plan",
    "anti_flip",
    "persistence",
}

# One invented value for each kind of secret, under a spelling a stored key might use.
SECRETS: dict[str, Any] = {
    "access_token": "FAKE-access-token-1111",
    "refresh_token": "FAKE-refresh-token-1112",
    "Password": "FAKE-password-2222",
    "API-Key": "FAKE-api-key-2223",
    "username": "fake_user_3333",
    "email": "person@example.invalid",
    "macAddress": "AA:BB:CC:DD:EE:01",
    "serial_number": "FAKE-SERIAL-4444",
    "ip_address": "192.0.2.55",
    "latitude": 47.60621,
    "longitude": -122.33207,
    "cloud_account_id": "FAKE-cloud-acct-5555",
    "notify_service": "notify.mobile_app_fake_person",
}
FREE_TEXT_ONLY = ("someone.else@example.invalid", "198.51.100.7", "AA:BB:CC:DD:EE:02", "2001:db8::1")
ZONES = [
    {ZONE_NAME: "Study", ZONE_CLIMATE: "climate.study", ZONE_SENSOR: "sensor.study_temp"},
]


def _errors(value: Any, path: str = "") -> list[str]:
    """Where in the document a section reports an error."""
    if isinstance(value, dict):
        found = [path] if set(value) == {"error"} else []
        for key, item in value.items():
            found += _errors(item, f"{path}/{key}")
        return found
    if isinstance(value, list):
        return [e for i, item in enumerate(value) for e in _errors(item, f"{path}/{i}")]
    return []


async def _secret_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A loaded entry whose stored config holds every secret, top level and nested."""
    hass.states.async_set("climate.study", "off")
    hass.states.async_set("sensor.study_temp", "21", {"unit_of_measurement": "°C"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        version=2,
        minor_version=2,
        data={
            CONF_ZONES: [{**ZONES[0], **SECRETS}],
            CONF_NOTIFY_SERVICE: "notify.mobile_app_fake_other",
            **SECRETS,
        },
        options={
            CONF_ZONES: [{**ZONES[0]}],
            "nested": {"deeper": [{"deepest": dict(SECRETS)}, (dict(SECRETS),)]},
            # Secrets under keys that say nothing, in free text.
            "note": "login person@example.invalid from 192.0.2.55 mac AA:BB:CC:DD:EE:01",
            # In free text only, under no sensitive key, so only the patterns can catch them.
            "note_free": f"login {FREE_TEXT_ONLY[0]} from {FREE_TEXT_ONLY[1]} mac {FREE_TEXT_ONLY[2]}",
            "note_v6": f"seen at {FREE_TEXT_ONLY[3]} at 21:49:00",
            "note_known": "token was FAKE-access-token-1111 and FAKE-cloud-acct-5555",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _assert_no_secret(document: Any) -> str:
    text = json.dumps(document, allow_nan=False)
    for value in SECRETS.values():
        assert str(value) not in text
    for value in ("notify.mobile_app_fake_other", *FREE_TEXT_ONLY):
        assert value not in text
    return text


# -- shape ---------------------------------------------------------------------
async def test_shape_of_a_healthy_document(hass: HomeAssistant) -> None:
    entry = await _setup_plain(hass)

    document = await async_get_config_entry_diagnostics(hass, entry)

    assert set(document) == TOP_LEVEL
    assert set(document["coordinator"]) == COORDINATOR
    assert set(document["rooms"]) == {"primary", "secondary"}
    for room in document["rooms"].values():
        assert set(room) == ROOM
    # A renamed coordinator attribute would read as an error here, so none may.
    assert _errors(document) == []
    json.dumps(document, allow_nan=False)


async def test_versions_come_from_the_manifest_and_home_assistant(hass: HomeAssistant) -> None:
    entry = await _setup_plain(hass)
    manifest = json.loads(
        Path("custom_components/mxz_coordinator/manifest.json").read_text(encoding="utf-8")
    )

    document = await async_get_config_entry_diagnostics(hass, entry)

    assert document["integration"] == {"domain": DOMAIN, "version": manifest["version"]}
    assert document["home_assistant"]["version"] == HA_VERSION


async def test_nothing_ordinary_is_redacted(hass: HomeAssistant) -> None:
    """Negative for over-redaction: no MXZ key or time reads as a secret."""
    entry = await _setup_plain(hass)

    document = await async_get_config_entry_diagnostics(hass, entry)

    assert REDACTED not in json.dumps(document)
    room = document["rooms"]["primary"]
    assert room["entity_ids"]["head"] == "climate.primary"
    assert room["entity_ids"]["temperature_sensor"] == "sensor.primary_temp"
    assert datetime.fromisoformat(document["generated_at"])


@pytest.fixture(scope="module")
def pycares_shutdown_thread() -> None:
    """Workaround for the 2024.12 lane's test plugin, not for anything MXZ does.

    The first DNS channel an HTTP client closes starts pycares' one process-wide
    daemon thread (``_run_safe_shutdown_loop``). The plugin on later lanes allows it
    by name; the 2024.12.5 lane's (0.13.195) fails whichever test starts it. Starting
    it here, at a wider scope, puts it in that check's "before" snapshot.
    """
    pycares.Channel().close()


async def test_the_real_download_routes_serve_it(
    hass: HomeAssistant, hass_client, pycares_shutdown_thread: None
) -> None:
    """HA finds the platform and serves both downloads over its own HTTP route."""
    assert await async_setup_component(hass, "diagnostics", {})
    entry = await _setup_plain(hass)
    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]
    client = await hass_client()

    by_entry = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    by_device = await client.get(
        f"/api/diagnostics/config_entry/{entry.entry_id}/device/{device.id}"
    )

    assert by_entry.status == 200
    assert set((await by_entry.json())["data"]) == TOP_LEVEL
    assert by_device.status == 200
    assert set((await by_device.json())["data"]) == TOP_LEVEL | {"device"}


# -- what it reports -----------------------------------------------------------
async def test_a_room_reports_its_head_and_capabilities(hass: HomeAssistant, freezer) -> None:
    rig: Rig = await _setup(hass, freezer)

    room = (await async_get_config_entry_diagnostics(hass, rig.entry))["rooms"]["primary"]

    assert room["name"] == "Study"
    assert room["entity_ids"]["head"] == rig.heads[0]
    assert room["entity_ids"]["door_window"] == "binary_sensor.study_window"
    assert room["mode"]["head_state"] == hass.states.get(rig.heads[0]).state
    assert set(room["capabilities"]["hvac_modes"]) == {"off", "cool", "heat", "fan_only"}
    assert room["capabilities"]["hvac_modes"] == sorted(room["capabilities"]["hvac_modes"])
    assert "quiet" in room["capabilities"]["fan_modes"]
    assert room["last_decision"]["name"] == "Study"
    assert isinstance(room["last_decision"]["control_reasons"], list)


async def test_a_paused_window_shows_in_its_own_room(hass: HomeAssistant, freezer) -> None:
    rig: Rig = await _setup(hass, freezer)

    await rig.open_and_pause()
    rooms = (await async_get_config_entry_diagnostics(hass, rig.entry))["rooms"]

    pause = rooms["primary"]["window_pause"]
    assert pause["active"] is True
    assert pause["opening_state"] == rig.coord.openings.state_of("primary")
    assert pause["paused_reason"] is not None
    assert pause["paused_since"] is not None
    assert rooms["secondary"]["window_pause"] == {"active": False, "record": None}


async def test_a_manual_fan_pick_shows_in_its_own_room(hass: HomeAssistant, freezer) -> None:
    rig: Rig = await _setup(hass, freezer, study={})

    await rig.person_fan("quiet")
    await rig.recompute()
    rooms = (await async_get_config_entry_diagnostics(hass, rig.entry))["rooms"]

    assert rooms["primary"]["manual_fan"]["latched"] is True
    assert rooms["secondary"]["manual_fan"]["latched"] is False


async def test_a_lost_head_shows_its_vote_status(hass: HomeAssistant, freezer) -> None:
    rig: Rig = await _setup(hass, freezer, study={})
    den = _head(hass, rig.heads[1])
    den._attr_available = False
    den.async_write_ha_state()
    await hass.async_block_till_done()
    await rig.recompute()
    rooms = (await async_get_config_entry_diagnostics(hass, rig.entry))["rooms"]

    assert rooms["secondary"]["votes"]["status"] == VOTE_STALE
    assert rooms["primary"]["votes"]["status"] != VOTE_STALE
    assert rooms["secondary"]["votes"]["lost_since"] is not None
    assert rooms["secondary"]["votes"]["seconds_since_lost"] >= 0
    assert rooms["primary"]["votes"]["lost_since"] is None


async def test_a_mode_change_hold_shows_while_it_holds(hass: HomeAssistant, freezer) -> None:
    rig: Rig = await _setup(hass, freezer, study={}, hysteresis=600)
    await rig.recompute()
    # A shared-mode change stamps the hold; read what the coordinator itself says.
    rig.coord._last_mode_change_ts = time.monotonic() - 100
    rig.coord._last_mode_change_at = 1_800_000_000.0

    document = await async_get_config_entry_diagnostics(hass, rig.entry)

    anti_flip = document["coordinator"]["anti_flip"]
    assert anti_flip["minimum_seconds_between_mode_changes"] == 600
    assert 480 <= anti_flip["mode_change_hold_remaining"] <= 500
    assert anti_flip["mode_change_hold_until"] is not None
    assert anti_flip["last_mode_change_at"].startswith("2027-01-15T08:00:00")
    assert document["rooms"]["primary"]["holds"]["mode_change_hold_remaining"] > 0


async def test_each_room_part_reads_its_own_key(hass: HomeAssistant, freezer) -> None:
    """Stores keyed by slug and by head, each as the coordinator keys it, stay with their room."""
    rig: Rig = await _setup(hass, freezer, study={})
    coord = rig.coord
    head = rig.heads[1]
    coord._floor_rejecting["secondary"] = "refused"
    coord._floor_yield["secondary"] = ("heat", 1_800_000_000.0)
    coord.dry._rooms["secondary"] = "held"
    coord.dry._released.add("secondary")
    coord._hands_off_released.add("secondary")
    coord._fan_cmd[head] = "low"
    coord._last_active[head] = ("cool", time.monotonic())

    rooms = (await async_get_config_entry_diagnostics(hass, rig.entry))["rooms"]

    den = rooms["secondary"]
    assert den["holds"]["floor_rejecting"] == "refused"
    assert den["holds"]["floor_yield"] == ["heat", 1_800_000_000.0]
    assert den["holds"]["dry"] == "held"
    assert den["holds"]["dry_released"] is True
    assert den["holds"]["hands_off_released"] is True
    assert den["manual_fan"]["commanded"] == "low"
    assert den["anti_flip"]["last_active_mode"] == "cool"
    study = rooms["primary"]["holds"]
    assert study["floor_rejecting"] is None
    assert study["dry"] is None
    assert study["hands_off_released"] is False


async def test_reading_diagnostics_changes_and_sends_nothing(hass: HomeAssistant, freezer) -> None:
    rig: Rig = await _setup(hass, freezer)
    await rig.open_and_pause()
    rig.forget()
    before = json.dumps(rig.coord.data, default=str, sort_keys=True)

    await async_get_config_entry_diagnostics(hass, rig.entry)
    await hass.async_block_till_done()

    assert rig.calls == []
    assert json.dumps(rig.coord.data, default=str, sort_keys=True) == before


# -- redaction -----------------------------------------------------------------
async def test_no_secret_appears_anywhere(hass: HomeAssistant) -> None:
    entry = await _secret_entry(hass)
    # The fixture is not empty: every secret really is in what the entry stores.
    stored = json.dumps({"d": dict(entry.data), "o": dict(entry.options)}, default=str)
    for value in SECRETS.values():
        assert str(value) in stored

    document = await async_get_config_entry_diagnostics(hass, entry)

    text = _assert_no_secret(document)
    assert REDACTED in text
    assert _errors(document) == []


async def test_redaction_reaches_every_depth(hass: HomeAssistant) -> None:
    entry = await _secret_entry(hass)

    document = await async_get_config_entry_diagnostics(hass, entry)

    config = document["config_entry"]
    deepest = config["options"]["nested"]["deeper"]
    for holder in (config["data"], config["data"][CONF_ZONES][0], deepest[0]["deepest"], deepest[1][0]):
        for key in SECRETS:
            assert holder[key] == REDACTED, key
    # Only the secret goes: what surrounds it in free text stays readable.
    assert config["options"]["note"].startswith("login ")
    assert config["options"]["note_free"].startswith("login ")
    assert config["options"]["note_v6"].endswith("at 21:49:00")


async def test_redaction_goes_through_home_assistants_function(hass: HomeAssistant) -> None:
    entry = await _secret_entry(hass)

    with patch.object(
        diagnostics, "async_redact_data", wraps=diagnostics.async_redact_data
    ) as redact:
        await async_get_config_entry_diagnostics(hass, entry)

    redact.assert_called_once()
    assert diagnostics.TO_REDACT <= set(redact.call_args.args[1])
    assert {"Password", "API-Key", "macAddress", "cloud_account_id"} <= set(redact.call_args.args[1])


async def test_a_secret_in_an_error_message_is_scrubbed(hass: HomeAssistant) -> None:
    entry = await _secret_entry(hass)

    def _broken(_hass: HomeAssistant) -> None:
        raise ValueError("cannot read FAKE-password-2222 for person@example.invalid at 192.0.2.55")

    with patch.object(diagnostics, "_home_assistant", _broken):
        document = await async_get_config_entry_diagnostics(hass, entry)

    assert document["home_assistant"]["error"].startswith("ValueError: cannot read ")
    _assert_no_secret(document)


async def test_a_device_download_holds_no_secret(hass: HomeAssistant) -> None:
    entry = await _secret_entry(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        # Under no stored key, so only leaving these fields out keeps them out.
        identifiers={(DOMAIN, "DEVICE-ONLY-IDENTIFIER")},
        connections={(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:03")},
        name="Invented device",
        serial_number="DEVICE-ONLY-SERIAL",
    )

    document = await async_get_device_diagnostics(hass, entry, device)

    text = _assert_no_secret(document)
    for value in ("DEVICE-ONLY-IDENTIFIER", "DEVICE-ONLY-SERIAL", "aa:bb:cc:dd:ee:03"):
        assert value not in text
    assert document["device"]["name"] == "Invented device"
    assert set(document) == TOP_LEVEL | {"device"}


@pytest.mark.parametrize(
    "key",
    ["token", "ACCESS_TOKEN", "accessToken", "wifi_password", "API-Key", "x-api-key",
     "macAddress", "device_serial", "serialNumber", "IPAddress", "ip", "latitude", "Longitude",
     "cloud_account_id", "cloudAccountId", "account", "user_email", "username", "notify_service",
     "opening_notify_service", "connections", "host"],
)
def test_sensitive_key_spellings(key: str) -> None:
    assert _is_sensitive(key)


@pytest.mark.parametrize(
    "key",
    ["mode", "fan_mode", "head_state", "climate", "sensor", "machine", "macro", "tipping",
     "shipping", "plan", "opening_entity", "name", "target", "serialize", "state", "entry_id"],
)
def test_ordinary_keys_are_not_sensitive(key: str) -> None:
    assert not _is_sensitive(key)


# -- it never fails ------------------------------------------------------------
async def test_an_unloaded_entry_still_downloads(hass: HomeAssistant) -> None:
    entry = await _setup_plain(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    document = await async_get_config_entry_diagnostics(hass, entry)

    assert set(document) == TOP_LEVEL
    assert document["coordinator"]["error"].startswith("LookupError: coordinator not loaded")
    assert "not_loaded" in document["coordinator"]["error"]
    assert document["rooms"] == document["coordinator"]
    assert document["config_entry"]["state"] == "not_loaded"
    assert document["integration"]["version"]


async def test_an_entry_never_set_up_still_downloads(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="MXZ Coordinator", data={CONF_ZONES: ZONES})
    entry.add_to_hass(hass)

    document = await async_get_config_entry_diagnostics(hass, entry)

    assert set(document) == TOP_LEVEL
    assert "error" in document["coordinator"]
    assert document["config_entry"]["data"][CONF_ZONES] == ZONES


async def test_a_loaded_entry_with_no_coordinator_still_downloads(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, title="MXZ Coordinator", data={}, state=ConfigEntryState.LOADED
    )
    entry.add_to_hass(hass)

    document = await async_get_config_entry_diagnostics(hass, entry)

    assert document["coordinator"]["error"] == "LookupError: coordinator not loaded (no runtime data)"
    assert set(document) == TOP_LEVEL


@pytest.mark.parametrize(
    ("section", "reader"),
    [
        ("home_assistant", "_home_assistant"),
        ("config_entry", "_config_entry"),
        ("coordinator", "_coordinator"),
        ("rooms", "_rooms"),
        ("repair_issues", "_repair_issues"),
    ],
)
async def test_a_broken_section_leaves_the_rest(
    hass: HomeAssistant, section: str, reader: str
) -> None:
    entry = await _setup_plain(hass)

    def _broken(*_args: Any) -> None:
        raise RuntimeError("that part is broken")

    with patch.object(diagnostics, reader, _broken):
        document = await async_get_config_entry_diagnostics(hass, entry)

    assert set(document) == TOP_LEVEL
    assert document[section] == {"error": "RuntimeError: that part is broken"}
    assert _errors(document) == [f"/{section}"]


async def test_a_broken_version_lookup_leaves_the_rest(hass: HomeAssistant) -> None:
    entry = await _setup_plain(hass)

    with patch.object(diagnostics, "async_get_integration", side_effect=OSError("no manifest")):
        document = await async_get_config_entry_diagnostics(hass, entry)

    assert document["integration"] == {"error": "OSError: no manifest"}
    assert _errors(document) == ["/integration"]


@pytest.mark.parametrize(
    ("part", "reader"),
    [
        ("entity_ids", "_entity_ids"),
        ("settings", "_settings"),
        ("mode", "_mode"),
        ("capabilities", "_capabilities"),
        ("votes", "_votes"),
        ("holds", "_holds"),
        ("manual_fan", "_manual_fan"),
        ("window_pause", "_window_pause"),
        ("anti_flip", "_anti_flip"),
        ("sensor_health", "_sensor_health"),
    ],
)
async def test_a_broken_room_part_leaves_the_rest_of_the_room(
    hass: HomeAssistant, part: str, reader: str
) -> None:
    entry = await _setup_plain(hass)

    def _broken(*_args: Any) -> None:
        raise RuntimeError("that part is broken")

    with patch.object(diagnostics, reader, _broken):
        document = await async_get_config_entry_diagnostics(hass, entry)

    assert set(document["rooms"]["primary"]) == ROOM
    assert document["rooms"]["primary"][part] == {"error": "RuntimeError: that part is broken"}
    assert _errors(document) == [f"/rooms/primary/{part}", f"/rooms/secondary/{part}"]


async def test_a_broken_room_view_leaves_the_rest_of_the_room(hass: HomeAssistant) -> None:
    entry = await _setup_plain(hass)

    with patch.object(entry.runtime_data, "room_details", side_effect=KeyError("zones")):
        document = await async_get_config_entry_diagnostics(hass, entry)

    assert document["rooms"]["primary"]["last_decision"] == {"error": "KeyError: 'zones'"}
    assert document["rooms"]["primary"]["votes"]["status"] == "live"


async def test_one_broken_room_leaves_the_other(hass: HomeAssistant) -> None:
    entry = await _setup_plain(hass)
    zones = entry.runtime_data.zones

    class _Broken:
        slug = zones[1].slug

        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("this room is broken")

    with patch.object(entry.runtime_data, "zones", [zones[0], _Broken()]):
        document = await async_get_config_entry_diagnostics(hass, entry)

    assert document["rooms"]["primary"]["name"] == "Primary"
    assert document["rooms"]["secondary"] == {"error": "RuntimeError: this room is broken"}


async def test_a_broken_entry_object_still_returns(hass: HomeAssistant) -> None:
    document = await async_get_config_entry_diagnostics(hass, object())

    assert set(document) == TOP_LEVEL
    assert "error" in document["config_entry"]
    assert "error" in document["coordinator"]


async def test_a_failed_redaction_returns_no_data_at_all(hass: HomeAssistant) -> None:
    """Fail closed: if redaction can't run, nothing unredacted may go out, not even a message."""
    entry = await _secret_entry(hass)
    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]

    with patch.object(
        diagnostics, "async_redact_data", side_effect=RuntimeError("FAKE-password-2222")
    ):
        by_entry = await async_get_config_entry_diagnostics(hass, entry)
        by_device = await async_get_device_diagnostics(hass, entry, device)

    assert by_entry == {"error": "RuntimeError"}
    assert by_device == {"error": "RuntimeError"}


# -- plain JSON data -----------------------------------------------------------
class _Color(Enum):
    RED = "red"


@dataclass
class _Record:
    seen: set[str]
    at: datetime


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 11, 3, 21, 48, tzinfo=UTC), "2026-11-03T21:48:00+00:00"),
        (timedelta(minutes=15), 900.0),
        ({"b", "a"}, ["a", "b"]),
        (frozenset({2, 1}), [1, 2]),
        ((1, 2), [1, 2]),
        (_Color.RED, "red"),
        ({_Color.RED: 1, ("a", "b"): 2, 3: 4}, {"red": 1, "['a', 'b']": 2, "3": 4}),
        (float("inf"), "inf"),
        (float("nan"), "nan"),
        (1.5, 1.5),
        (b"abc", "<3 bytes>"),
        (_Record({"x"}, datetime(2026, 1, 1, tzinfo=UTC)),
         {"seen": ["x"], "at": "2026-01-01T00:00:00+00:00"}),
        (_Unprintable(), "<unprintable _Unprintable>"),
    ],
)
def test_jsonable(value: Any, expected: Any) -> None:
    assert _jsonable(value) == expected
    json.dumps(_jsonable(value), allow_nan=False)


def test_jsonable_survives_a_cycle() -> None:
    loop: dict[str, Any] = {}
    loop["self"] = loop

    json.dumps(_jsonable(loop), allow_nan=False)
