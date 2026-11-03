"""Dry refused by a head.

The Bedroom (Room 2) has Allow Dry mode on, so its MXZ thermostat offers Dry.
A person picks Dry there and MXZ sends the head dry once. Two ways it does
not take:

* the head rejects the write: the thermostat shows Heat/Cool again the
  moment the call returns its error, and MXZ sends no second dry;
* the head accepts the call but never reports dry: when the pick's wait
  (``DRY_PICK_WAIT``, 60 s) ends, the thermostat shows Heat/Cool, the log
  says why, the room's own cool call runs, and dry is still sent only once.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN

from custom_components.mxz_coordinator.const import DRY_PICK_WAIT
from tests.test_climate_immediate import BEDROOM, _RefusingHead
from tests.test_drive import _set_temp
from tests.test_dry_entity import LateHead
from tests.test_dry_mode import SENSORS
from tests.test_dry_mode import _setup as _dry_setup
from tests.test_startup_continuity import _head

FEATURE = "dry-refused-by-head"


async def _pick(hass: HomeAssistant, tile: str, mode: str) -> None:
    """A person picks a mode on the room's MXZ thermostat."""
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": tile, "hvac_mode": mode}, blocking=True
    )
    await hass.async_block_till_done()


def _dry_sent(log, head: str) -> int:
    return sum(c["data"].get("hvac_mode") == "dry" for c in log.head_calls(head))


async def test_a_head_that_rejects_dry_shows_heat_cool_at_once(
    hass: HomeAssistant, journal
) -> None:
    rig = await _dry_setup(hass, head_cls=_RefusingHead)
    await rig.recompute()
    tile = rig.eid(BEDROOM)
    head = rig.heads[1]
    log = journal(hass, rig.entry, FEATURE, heads=rig.heads)
    shown = log.step("setup: Bedroom has Allow Dry mode on; its head rejects dry")
    assert shown["thermostats"][tile]["state"] == "heat_cool"
    assert "dry" in hass.states.get(tile).attributes["hvac_modes"]

    with pytest.raises(HomeAssistantError, match="invented rejection of dry"):
        await _pick(hass, tile, "dry")
    shown = log.step("person: picks Dry on the Bedroom thermostat; the head rejects it")
    assert shown["thermostats"][tile]["state"] == "heat_cool"  # at once, before any plan
    assert shown["heads"][head]["state"] != "dry"
    assert _dry_sent(log, head) == 1

    await rig.later(REQUEST_REFRESH_DEFAULT_COOLDOWN + 1)
    await rig.recompute()
    await rig.recompute()
    shown = log.step("time: the plan the refusal asked for runs, then two more computes")
    assert shown["plan"]["zones"][1]["dry"] is None
    assert shown["thermostats"][tile]["state"] == "heat_cool"
    assert _dry_sent(log, head) == 1  # dry is never sent again


async def test_a_head_that_never_reports_dry_leaves_dry_when_the_wait_ends(
    hass: HomeAssistant, journal, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await _dry_setup(hass, head_cls=LateHead)
    await rig.recompute()
    tile = rig.eid(BEDROOM)
    head = rig.heads[1]
    _head(hass, head).late = True  # it takes the call and never reports it
    log = journal(hass, rig.entry, FEATURE, heads=rig.heads)
    log.step("setup: Bedroom has Allow Dry mode on; its head never reports a mode write")

    await _pick(hass, tile, "dry")
    await _set_temp(hass, SENSORS[1], 76)  # meanwhile the Bedroom warms past its band
    await rig.recompute()
    shown = log.step("person: picks Dry on the Bedroom thermostat; Bedroom reads 76")
    assert shown["thermostats"][tile]["state"] == "dry"  # the pick shows at once
    assert shown["plan"]["zones"][1]["demand"] == "neutral"  # paused while the pick counts
    assert _dry_sent(log, head) == 1

    await rig.later(DRY_PICK_WAIT + 1)
    await rig.later(DRY_PICK_WAIT + REQUEST_REFRESH_DEFAULT_COOLDOWN + 2)
    shown = log.step("time: the 60 s pick wait ends with no dry reported")
    assert shown["thermostats"][tile]["state"] == "heat_cool"
    assert (shown["plan"]["zones"][1]["dry"], shown["plan"]["zones"][1]["demand"]) == (
        None, "cool",
    )
    assert "was sent dry but its head didn't report it in time" in caplog.text
    assert _dry_sent(log, head) == 1
    modes = [c["data"]["hvac_mode"] for c in log.head_calls(head) if "hvac_mode" in c["data"]]
    assert modes[0] == "dry" and "cool" in modes[1:], modes  # the plan runs the room again
