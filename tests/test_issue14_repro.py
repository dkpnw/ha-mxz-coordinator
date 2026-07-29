"""Repro for #14: idempotent auto-rewrites roll the echo memory -> self-latch."""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM  # noqa: E402

from tests.test_drive import (  # noqa: E402
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_temp,
    _setup_fan_boost,
    _setup_mock_heads,
)


def _head_obj(hass, entity_id):
    return hass.data["entity_components"]["climate"].get_entity(entity_id)


async def test_lagging_auto_handback_must_not_self_latch(
    hass: HomeAssistant,
) -> None:
    """#14: a head that is slow to APPLY the satisfied fan=auto write must not
    have its stale boost token read as a manual hold.

    The coordinator writes fan=auto at the satisfied handback (prev=quiet,
    cmd=auto). A real CN105/ESP head takes a beat to apply; every recompute in
    that window sees the stale `quiet` and idempotently REWRITES auto — and the
    rewrite rolled the echo memory (prev=auto, cmd=auto), so the next
    observation of `quiet` counted as a user departure and latched. Fan auto
    flipped OFF with no user anywhere near it (the field evidence in #14: a
    post-reload compute burst, 8 s after an options save).
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    # Ease boost to quiet while still cooling, then reach target: the
    # coordinator writes fan=auto (MockHead applies instantly).
    await _set_temp(hass, SENSOR_A, 65)
    await _recompute(hass, entry)
    await _set_temp(hass, SENSOR_A, 62.4)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"
    # The ESP is slow: the satisfied handback's auto write (and the burst's
    # rewrites) are accepted on the wire but not applied yet — the head keeps
    # REPORTING quiet the whole time. Swallow writes BEFORE the handback so
    # the head never confirms.
    head = _head_obj(hass, head_a)

    async def _swallow(fan_mode):  # firmware busy: accepts, applies nothing
        return None

    real_set = head.async_set_fan_mode
    head.async_set_fan_mode = _swallow

    await _set_temp(hass, SENSOR_A, 61.9)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"  # lagging

    # The burst: each recompute observes stale quiet and rewrites auto.
    for _ in range(3):
        await _recompute(hass, entry)
    head.async_set_fan_mode = real_set

    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False, (
        "the coordinator latched its own auto-handback residue as a manual hold"
    )
    # The head finally applies the write; everything settles on auto, driven.
    await _recompute(hass, entry)  # real writes flow again -> auto lands
    await hass.async_block_till_done()
    plan = hass.states.get(_eid(hass, entry, "_plan"))
    assert plan.attributes["zones"][0]["fan_hold"] is False


async def test_options_save_reloads_the_entry_exactly_once(
    hass: HomeAssistant,
) -> None:
    """#14 trigger hygiene: one options save = one reload.

    The flow wrote the data mirror and the options as two separate entry
    updates, so the update listener fired twice and every options save ran two
    full back-to-back reloads — the compute-burst window the field report's
    latch formed in. The combined update makes the second write a no-change.
    """
    from unittest.mock import patch

    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)

    reloads = 0
    real_reload = hass.config_entries.async_reload

    async def _counting_reload(entry_id):
        nonlocal reloads
        reloads += 1
        return await real_reload(entry_id)

    with patch.object(
        hass.config_entries, "async_reload", side_effect=_counting_reload
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"demand_threshold": 4.0}
        )
        await hass.async_block_till_done()

    assert result["type"].value == "create_entry"
    assert reloads == 1, f"one options save ran {reloads} reloads"
