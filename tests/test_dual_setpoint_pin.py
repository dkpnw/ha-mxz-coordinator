"""Off means 3.9.0: every room without the dual setpoint toggle is byte-for-byte main.

``tests/pin_3_9_0.json`` was captured on main ``abd946e`` (3.9.0) by this test
with ``MXZ_PIN_CAPTURE=1`` (see ``tests/pin_scenario.py`` for what each step
does and records). Replaying it here pins, in °F and °C: every climate
entity's features, hvac modes, and full attribute dict; every plan sensor
attribute and zones row; every number, switch, button, and select; the entity
registry's unique IDs; what the stored entry data and options mean, read as
``{**data, **options}`` (since entry v2.3 each key is kept in one of them),
including after an options-flow pass over both room pages; and every head
service call.

Requires pytest-homeassistant-custom-component (Python 3.12+).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant

from tests.one_home import assert_one_home
from tests.pin_scenario import (
    COMFORT_OFF_ROW,
    climate_units,
    dumps,
    mode_hold,
    run_scenario,
    take_climate_units,
    take_comfort,
    take_entry,
    take_mode_hold,
    take_vote_rows,
)

PIN = Path(__file__).with_name("pin_3_9_0.json")
CAPTURE = os.environ.get("MXZ_PIN_CAPTURE") == "1"


def _pinned(unit: str) -> list[dict]:
    return json.loads(PIN.read_text())[unit]


@pytest.mark.parametrize("unit", ["F", "C"])
async def test_all_single_install_is_3_9_0(hass: HomeAssistant, unit: str) -> None:
    """The whole scripted scenario reproduces 3.9.0's record exactly."""
    steps = run_steps = await run_scenario(hass, unit)
    if CAPTURE:
        record = json.loads(PIN.read_text()) if PIN.exists() else {}
        record[unit] = run_steps
        PIN.write_text(dumps(record))
        pytest.skip("captured")
    pinned = _pinned(unit)
    assert [s["step"] for s in steps] == [s["step"] for s in pinned]
    # Since v2.3 the untouched room pages also leave both stores exactly as they
    # were (3.9.0 mirrored options into data there).
    ran = {s["step"]: s for s in steps}
    assert ran["room_pages"]["data"] == ran["boot"]["data"]
    assert ran["room_pages"]["options"] == ran["boot"]["options"]
    for seen, want in zip(steps, pinned, strict=True):
        # No head is ever lost in this scenario: both rows are live every step.
        seen, votes = take_vote_rows(seen)
        assert votes == [("live", None), ("live", None)], seen["step"]
        # No room has a Comfort priority entity: every row is off, never a boost.
        seen, comfort, by_comfort = take_comfort(seen)
        assert comfort == [COMFORT_OFF_ROW, COMFORT_OFF_ROW], seen["step"]
        assert by_comfort is False, seen["step"]
        seen, units = take_climate_units(seen)
        assert units == climate_units(want, unit), seen["step"]
        seen, holds, since = take_mode_hold(seen)
        want_holds, want_since = mode_hold(want)
        assert (holds, since) == (want_holds, want_since), seen["step"]
        want = take_mode_hold(want)[0]
        # The entry means what 3.9.0's meant; each key is now kept in its one home.
        seen, meant, stores = take_entry(seen)
        want, want_meant, _ = take_entry(want)
        assert meant == want_meant, seen["step"]
        assert_one_home(SimpleNamespace(**stores))
        assert seen == want, seen["step"]


def test_pin_record_covers_the_scenario() -> None:
    """The record itself has what the pin claims (a guard against a thin capture)."""
    for unit in ("F", "C"):
        steps = _pinned(unit)
        names = [s["step"] for s in steps]
        for needed in (
            "boot", "room_pages", "heat", "heat_run", "restart_mid_heat", "cool",
            "cool_run", "idle", "standoff", "fan_auto_off", "fan_auto_on", "eco",
            "heat_lockout_held", "heat_lockout_floor", "cool_lockout_held",
            "cool_lockout_ceiling", "freeze_floor", "hands_off_release",
            "hands_off_vote", "target_write",
        ):
            assert needed in names, (unit, needed)
        by = {s["step"]: s for s in steps}
        # Each direction really reached a head, so the calls are not vacuous.
        services = {
            (c["service"], c["service_data"].get("hvac_mode"))
            for s in steps for c in s["calls"]
        }
        assert ("set_temperature", "heat") in services or ("set_hvac_mode", "heat") in services
        assert ("set_temperature", "cool") in services or ("set_hvac_mode", "cool") in services
        assert any(c["service"] == "set_fan_mode" for s in steps for c in s["calls"])
        # A cool run a few tenths above the target, whose fan step reached the
        # head: a small cool-side boost distance is pinned, not only big ones.
        row = by["cool_run"]["entities"]["plan"]["attributes"]["zones"][0]
        assert row["engage"] == "cool"
        assert 0 < row["temp"] - row["target"] < 0.5, row
        assert any(c["service"] == "set_fan_mode" for c in by["cool_run"]["calls"])
        plan = by["standoff"]["entities"]["plan"]["attributes"]
        assert plan["standoff"] is True
        assert by["eco"]["entities"]["plan"]["attributes"]["zones"][0]["start_points_mode"] == "eco"
        assert by["freeze_floor"]["entities"]["plan"]["attributes"]["zones"][1]["floor_want"] == "heat"
        assert by["hands_off_vote"]["entities"]["plan"]["attributes"]["zones"][0]["hands_off_vote"] == "heat"
        assert by["heat_lockout_held"]["entities"]["plan"]["attributes"]["zones"][0]["start_points_lockouts"] == ["heat"]
        assert by["cool_lockout_held"]["entities"]["plan"]["attributes"]["zones"][0]["start_points_lockouts"] == ["cool"]
        # The untouched room pages added nothing to either room's record (the
        # save mirrors options into data, as 3.9.0 always has).
        assert by["room_pages"]["data"]["zones"] == by["boot"]["data"]["zones"]
        assert by["room_pages"]["options"] == by["boot"]["options"]
        assert by["room_pages"]["data"] == {**by["boot"]["data"], **by["boot"]["options"]}
