"""The throwaway-instance driver's pure parts (verification/instance/).

The driver itself runs a real Home Assistant and stays off CI. What decides a
pass or keeps the instance isolated is plain code, tested here: the rendered
configuration, the device file that outlives Home Assistant, the checks every
journey runs, the deadline on an expectation, the staleness digest, the demo's
three-rule room model, and what the instance's environment installs for a browser.
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
model = _load("model", INSTANCE / "invented_house" / "model.py")
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


def test_a_device_file_from_before_the_model_loads_with_no_direction(tmp_path: Path) -> None:
    path = tmp_path / devices.DEVICE_FILE
    stored = devices.Devices.fresh(HOUSE)
    stored.save(path)
    data = json.loads(path.read_text())
    del data["outdoor"]
    path.write_text(json.dumps(data))
    assert devices.Devices.load(path, HOUSE).outdoor is None
    stored.outdoor = "heat"
    stored.save(path)
    assert devices.Devices.load(path, HOUSE).outdoor == "heat"


CLIMATE = model.Climate(**HOUSE["climate"])


def _house(**heads: tuple[str, float | None, float | None]) -> object:
    """Fresh devices with the given heads set: key=(mode, low, high)."""
    house = devices.Devices.fresh(HOUSE)
    for key, (mode, low, high) in heads.items():
        house.heads[key] = devices.Head(hvac_mode=mode, target_temp_low=low, target_temp_high=high)
    return house


def test_the_house_s_model_numbers_are_slower_to_drift_than_to_run() -> None:
    assert 0 < CLIMATE.drift_rate < CLIMATE.run_rate
    assert CLIMATE.tick > 0


def test_rule_1_the_head_selected_last_waits_in_standby() -> None:
    house = _house(loft=("heat", 70.0, None))
    assert model.direction(house, None) == "heat"
    house.heads["studio"] = devices.Head(hvac_mode="cool", target_temp_high=70.0)
    # Studio comes first in house order, but heat is already running and Loft holds it.
    assert model.direction(house, "heat") == "heat"
    assert model.action(house.heads["studio"], house.rooms["studio"], "heat") == "standby"
    house.heads["loft"].hvac_mode = "off"
    assert model.direction(house, "heat") == "cool"


def test_rule_1_with_nothing_running_the_first_head_in_house_order_decides() -> None:
    house = _house(studio=("cool", None, 70.0), den=("heat", 70.0, None))
    assert model.direction(house, None) == "cool"
    assert model.direction(_house(), "heat") is None
    unavailable = _house(studio=("cool", None, 70.0), loft=("heat", 70.0, None))
    unavailable.heads["studio"].available = False
    assert model.direction(unavailable, "cool") == "heat"


def test_rule_1_negative_a_head_in_standby_never_moves_its_room() -> None:
    house = _house(studio=("cool", None, 70.0), loft=("heat", 70.0, None))
    house.outdoor = "heat"
    house.rooms["studio"].temperature = 76.0
    for _ in range(20):
        model.step(house, CLIMATE, 1.0)
    assert house.outdoor == "heat"
    # Ambient is 76, so a waiting Studio stays exactly where the sun put it.
    assert house.rooms["studio"].temperature == CLIMATE.ambient == 76


def test_rule_2_a_running_head_moves_its_room_to_its_setpoint_then_idles() -> None:
    house = _house(studio=("cool", 68.0, 70.0))
    house.rooms["studio"].temperature = 76.0
    model.step(house, CLIMATE, 1.0)
    assert house.outdoor == "cool"
    assert house.rooms["studio"].temperature == 76.0 - CLIMATE.run_rate
    assert model.action(house.heads["studio"], house.rooms["studio"], "cool") == "cooling"
    seen = []
    for _ in range(10):
        model.step(house, CLIMATE, 1.0)
        seen.append(house.rooms["studio"].temperature)
    # Cool runs to the high setpoint and never past it. There it idles, the room
    # drifts back up by one slow step, and the head pulls it down again.
    assert 70.0 in seen
    assert min(seen) == 70.0
    assert max(seen[seen.index(70.0):]) <= 70.0 + CLIMATE.drift_rate
    house.rooms["studio"].temperature = 70.0
    assert model.action(house.heads["studio"], house.rooms["studio"], "cool") == "idle"
    heat = _house(den=("heat", 70.0, 72.0))
    heat.rooms["den"].temperature = 66.0
    model.step(heat, CLIMATE, 2.0)
    assert heat.rooms["den"].temperature == 66.0 + 2 * CLIMATE.run_rate
    assert model.action(heat.heads["den"], heat.rooms["den"], "heat") == "heating"


def test_rule_2_negative_a_head_already_past_its_setpoint_does_not_run() -> None:
    house = _house(loft=("heat", 70.0, None))
    house.rooms["loft"].temperature = 71.0
    model.step(house, CLIMATE, 1.0)
    assert model.action(house.heads["loft"], house.rooms["loft"], "heat") == "idle"
    # Not heated: it drifted toward the 76 ambient by the slower rate only.
    assert house.rooms["loft"].temperature == 71.0 + CLIMATE.drift_rate


def test_rule_3_an_unattended_room_drifts_toward_ambient_without_passing_it() -> None:
    house = _house(studio=("fan_only", None, None))
    house.rooms["studio"].temperature = 70.0
    house.rooms["loft"].temperature = 80.0
    model.step(house, CLIMATE, 1.0)
    assert house.rooms["studio"].temperature == 70.0 + CLIMATE.drift_rate
    assert house.rooms["loft"].temperature == 80.0 - CLIMATE.drift_rate
    assert model.action(house.heads["studio"], house.rooms["studio"], None) == "fan"
    house.rooms["den"].temperature = CLIMATE.ambient - CLIMATE.drift_rate / 2
    model.step(house, CLIMATE, 1.0)
    assert house.rooms["den"].temperature == CLIMATE.ambient


def test_the_browser_requirements_follow_dependencies_once(tmp_path: Path) -> None:
    def component(name: str, requirements: list[str], dependencies: list[str]) -> None:
        (tmp_path / name).mkdir()
        (tmp_path / name / "manifest.json").write_text(
            json.dumps({"domain": name, "requirements": requirements, "dependencies": dependencies}))

    component("frontend", ["home-assistant-frontend==1"], ["api"])
    component("api", [], ["frontend"])  # a cycle ends
    component("ai_task", [], ["conversation"])
    component("conversation", ["hassil==3"], [])
    component("assist_satellite", ["hassil==3"], ["tts"])
    component("tts", ["mutagen==1"], [])
    component("camera", ["PyTurboJPEG==1"], [])
    component("unrelated", ["never==1"], [])
    found = home.ui_requirements(tmp_path)
    assert found == ["home-assistant-frontend==1", "hassil==3", "mutagen==1", "PyTurboJPEG==1"]
    assert "never==1" not in found
    assert home.ui_requirements(tmp_path, ("camera",)) == ["PyTurboJPEG==1"]


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


async def test_an_expectation_passes_when_the_state_arrives_in_time() -> None:
    stream = expect.Stream()
    asyncio.get_running_loop().call_later(0.05, stream.feed, _state("climate.studio_head", "cool"))
    arrived = await stream.until("cool", lambda s: s.get("climate.studio_head", {}).get("state") == "cool", 2)
    assert arrived is True


async def test_a_missed_deadline_reports_the_last_state_seen() -> None:
    stream = expect.Stream()
    stream.feed(_state("climate.studio_head", "fan_only"))
    check = expect.watching("climate.studio_head")(
        lambda s: s["climate.studio_head"]["state"] == "cool")
    with pytest.raises(expect.Missed) as missed:
        await stream.until("cool", check, 0.05)
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
