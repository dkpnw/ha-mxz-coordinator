"""The plan sensor publishes only a plan computed from the room's restored inputs.

On every reload (an options save, say) the coordinator is rebuilt. It used to
seed a plan in its constructor, before any helper entity had restored, and the
plan sensor published that seed as soon as it was added: a shared mode of
``cool`` (the constructor's default) for the few milliseconds until setup's
first compute published the real one. Nothing was commanded from it, but the
state history and any automation watching the plan saw a mode MXZ never chose.

Fake heads and sensors only.
"""

from __future__ import annotations

from itertools import groupby
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.mxz_coordinator.const import MODE_COOL, MODE_HEAT
from tests.test_drive import SENSOR_A, _eid, _recompute, _set_temp
from tests.test_hands_off import _plan, _setup


def _published(hass: HomeAssistant, entity_id: str) -> list[str | None]:
    """Every state the plan sensor writes from here on (None: removed)."""
    seen: list[str | None] = []

    @callback
    def listen(event: Any) -> None:
        new = event.data.get("new_state")
        seen.append(None if new is None else new.state)

    async_track_state_change_event(hass, [entity_id], listen)
    return seen


@pytest.mark.parametrize(("temp", "mode"), [(66, MODE_HEAT), (74, MODE_COOL)])
async def test_a_reload_publishes_no_mode_but_the_computed_one(
    hass: HomeAssistant, temp: float, mode: str
) -> None:
    """Heat (the seed's default is cool, so the old seed showed it) and cool
    (where the seed happened to agree): either way, from the moment the old
    sensor goes until the reload is done, the only mode published is the one
    the first compute chose (on 3.13.0: unavailable, cool, heat)."""
    entry, _a, _b = await _setup(hass)
    await _set_temp(hass, SENSOR_A, temp)
    await _recompute(hass, entry)
    plan_id = _eid(hass, entry, "_plan")
    assert _plan(hass, entry).state == mode
    seen = _published(hass, plan_id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _plan(hass, entry).state == mode
    # The old sensor goes unavailable, stays so until the first compute, and
    # then shows its plan: no other mode, and no unknown, in between. (HA
    # writes unavailable twice: the registry's restored placeholder at unload,
    # then the new sensor, with fewer attributes and the same last_changed.)
    assert [state for state, _ in groupby(seen)] == ["unavailable", mode], seen
