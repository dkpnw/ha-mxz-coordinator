"""The throwaway-instance driver's pure parts (verification/instance/).

The driver itself runs a real Home Assistant and stays off CI. What decides a
pass or keeps the instance isolated is plain code, tested here: the rendered
configuration, the device file that outlives Home Assistant, the checks every
journey runs, the deadline on an expectation, and the staleness digest.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

INSTANCE = Path(__file__).parents[1] / "verification" / "instance"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"mxz_instance_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


home = _load("home", INSTANCE / "home.py")
expect = _load("expect", INSTANCE / "expect.py")
devices = _load("devices", INSTANCE / "invented_house" / "devices.py")
HOUSE = devices.load_house()
HEADS = ["climate.studio_head", "climate.loft_head"]


def _call(service: str, head: str, user: str | None = None, **data: object) -> dict:
    return {
        "event_type": "call_service",
        "time_fired": "t",
        "context": {"user_id": user},
        "data": {"domain": "climate", "service": service, "service_data": {"entity_id": head, **data}},
    }


def test_the_configuration_loads_only_what_the_proof_needs() -> None:
    config = yaml.safe_load(home.render_configuration(HOUSE["site"]))
    assert set(config) == {"homeassistant", "invented_house", *home.INTEGRATIONS}
    assert "default_config" not in config
    assert config["http"] == {"server_host": "127.0.0.1", "server_port": home.PORT}
    assert home.PORT != 8123
    assert config["homeassistant"]["unit_system"] == "us_customary"


def test_the_site_is_invented() -> None:
    site = yaml.safe_load(home.render_configuration(HOUSE["site"]))["homeassistant"]
    assert site["name"] == "Invented house"
    assert (site["latitude"], site["longitude"]) == (0.0, 0.0)


def test_a_head_keeps_its_state_across_a_stop_and_start(tmp_path: Path) -> None:
    path = tmp_path / devices.DEVICE_FILE
    before = devices.Devices.load(path, HOUSE)
    before.heads["studio"].hvac_mode = "cool"
    before.heads["studio"].target_temp_high = 73.0
    before.rooms["loft"].window_open = True
    before.save(path)
    after = devices.Devices.load(path, HOUSE)
    assert after.heads["studio"].hvac_mode == "cool"
    assert after.heads["studio"].target_temp_high == 73.0
    assert after.rooms["loft"].window_open is True
    assert not path.with_suffix(".tmp").exists()


def test_with_no_device_file_the_house_starts_at_its_defaults(tmp_path: Path) -> None:
    fresh = devices.Devices.load(tmp_path / devices.DEVICE_FILE, HOUSE)
    assert list(fresh.heads) == ["studio", "loft", "den"]
    assert {head.hvac_mode for head in fresh.heads.values()} == {"off"}
    assert {room.temperature for room in fresh.rooms.values()} == {70.0}


def test_opposite_directions_from_mxz_are_found() -> None:
    events = [_call("set_hvac_mode", HEADS[0], hvac_mode="heat"),
              _call("set_temperature", HEADS[1], hvac_mode="cool", target_temp_low=68)]
    assert expect.opposite_directions(events, HEADS)


def test_a_person_s_opposite_pick_and_a_sequential_change_are_not_mxz_s() -> None:
    by_person = [_call("set_hvac_mode", HEADS[0], hvac_mode="heat"),
                 _call("set_hvac_mode", HEADS[1], user="someone", hvac_mode="cool")]
    in_turn = [_call("set_hvac_mode", HEADS[0], hvac_mode="heat"),
               _call("turn_off", HEADS[0]),
               _call("set_hvac_mode", HEADS[1], hvac_mode="cool")]
    assert not expect.opposite_directions(by_person, HEADS)
    assert not expect.opposite_directions(in_turn, HEADS)


@pytest.mark.parametrize("mode", ["auto", "heat_cool"])
def test_mxz_never_sends_auto_or_heat_cool(mode: str) -> None:
    assert expect.forbidden_modes([_call("set_hvac_mode", HEADS[0], hvac_mode=mode)], HEADS)
    assert not expect.forbidden_modes([_call("set_hvac_mode", HEADS[0], user="p", hvac_mode=mode)], HEADS)


def test_dry_goes_only_where_allow_dry_is_on() -> None:
    events = [_call("set_hvac_mode", HEADS[0], hvac_mode="dry")]
    assert expect.forbidden_modes(events, HEADS)
    assert not expect.forbidden_modes(events, HEADS, dry_allowed=[HEADS[0]])


def test_log_errors_keeps_mxz_errors_with_their_traceback_only() -> None:
    log = (
        "2026-11-10 16:00:00.000 ERROR (MainThread) [custom_components.mxz_coordinator.coordinator] boom\n"
        "Traceback (most recent call last):\n"
        "  ValueError: x\n"
        "2026-11-10 16:00:01.000 ERROR (MainThread) [homeassistant.setup] Setup failed for 'camera'\n"
        "2026-11-10 16:00:02.000 WARNING (MainThread) [custom_components.mxz_coordinator] fine\n"
    )
    found = expect.log_errors(log, ("mxz_coordinator",))
    assert len(found) == 1
    assert found[0].endswith("ValueError: x")
    assert not expect.log_errors(log, ("invented_house",))


# Home Assistant 2026.9.4 logs an exception from an integration's listener under
# homeassistant.core (core.py, "Error running job") and one from its task or timer
# under homeassistant (runner.py, "Error doing job"). Only the traceback names MXZ.
_LISTENER = (
    "2026-11-10 16:00:03.000 ERROR (MainThread) [homeassistant.core] Error running job: "
    "<Job listen_event HassJobType.Callback <bound method MXZCoordinator._on_head_state of <x>>>\n"
    "Traceback (most recent call last):\n"
    '  File "/srv/env/lib/python3.14/site-packages/homeassistant/core.py", line 1678, in async_fire_internal\n'
    "    self._hass.async_run_hass_job(job, event)\n"
    '  File "/srv/config/custom_components/mxz_coordinator/coordinator.py", line 5001, in _on_head_state\n'
    "    raise KeyError(entity_id)\n"
    "KeyError: 'climate.studio_head'"
)
_TASK = (
    "2026-11-10 16:00:04.000 ERROR (MainThread) [homeassistant] Error doing job: "
    "Task exception was never retrieved (task: <Task finished name='Task-812'>)\n"
    "Traceback (most recent call last):\n"
    '  File "/srv/config/custom_components/mxz_coordinator/coordinator.py", line 2790, in _apply\n'
    "    await self._send(zone)\n"
    "RuntimeError: x"
)
_TIMER = (
    "2026-11-10 16:00:05.000 CRITICAL (MainThread) [homeassistant] Error doing job: "
    "Exception in callback (task: None)\n"
    "Traceback (most recent call last):\n"
    '  File "/srv/config/custom_components/invented_house/climate.py", line 60, in _report\n'
    "    self.async_write_ha_state()\n"
    "ValueError: y"
)
_SETUP = (
    "2026-11-10 16:00:06.000 ERROR (MainThread) [homeassistant.setup] "
    "Setup failed for custom integration 'mxz_coordinator': Integration failed to initialize."
)
_CAMERA = (
    "2026-11-10 16:00:07.000 ERROR (MainThread) [homeassistant.setup] Setup failed for 'camera': "
    "Unable to import component: No module named 'turbojpeg'\n"
    "Traceback (most recent call last):\n"
    '  File "/srv/env/lib/python3.14/site-packages/homeassistant/loader.py", line 1100, in async_get_component\n'
    "ModuleNotFoundError: No module named 'turbojpeg'"
)
_WARNING = (
    "2026-11-10 16:00:08.000 WARNING (MainThread) [homeassistant.core] Something slow\n"
    '  File "/srv/config/custom_components/mxz_coordinator/coordinator.py", line 1, in x'
)
NAMES = ("mxz_coordinator", "invented_house")


@pytest.mark.parametrize("block", [_LISTENER, _TASK, _TIMER, _SETUP], ids=["listener", "task", "timer", "setup"])
def test_log_errors_counts_home_assistant_s_own_error_that_names_mxz_or_the_equipment(block: str) -> None:
    log = f"{_CAMERA}\n{block}\n{_WARNING}\n"
    assert expect.log_errors(log, NAMES) == [block]


def test_log_errors_leaves_out_the_camera_failure_and_a_warning() -> None:
    assert not expect.log_errors(f"{_CAMERA}\n{_WARNING}\n", NAMES)


def _state(entity_id: str, state: str) -> dict:
    return {"event_type": "state_changed", "time_fired": "t", "context": {},
            "data": {"entity_id": entity_id, "new_state": {"state": state, "attributes": {}}}}


def test_an_expectation_passes_when_the_state_arrives_in_time() -> None:
    async def scenario() -> object:
        stream = expect.Stream()
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, stream.feed, _state("climate.studio_head", "cool"))
        return await stream.until("cool", lambda s: s.get("climate.studio_head", {}).get("state") == "cool", 2)

    assert asyncio.run(scenario()) is True


def test_a_missed_deadline_reports_the_last_state_seen() -> None:
    async def scenario() -> None:
        stream = expect.Stream()
        stream.feed(_state("climate.studio_head", "fan_only"))
        check = expect.watching("climate.studio_head")(
            lambda s: s["climate.studio_head"]["state"] == "cool")
        await stream.until("cool", check, 0.05)

    with pytest.raises(expect.Missed) as missed:
        asyncio.run(scenario())
    assert missed.value.seen == {"climate.studio_head": {"state": "fan_only"}}


def test_an_edit_after_up_makes_the_instance_stale(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "__pycache__").mkdir()
    built = home.digest(tmp_path)
    (tmp_path / "__pycache__" / "a.pyc").write_bytes(b"cache")
    assert home.digest(tmp_path) == built
    (tmp_path / "a.py").write_text("x = 2\n")
    assert home.digest(tmp_path) != built


def test_the_equipment_never_ships_with_the_product() -> None:
    manifest = json.loads((INSTANCE / "invented_house" / "manifest.json").read_text())
    root = Path(__file__).parents[1]
    assert not (root / "custom_components" / manifest["domain"]).exists()
    assert manifest["domain"] != "esphome"
