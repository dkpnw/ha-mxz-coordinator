"""Open finding F1 (docs/formal/PARKED-FINDINGS.md): not fixed, on purpose.

A head two different fan writes behind reads as a person's pick, so MXZ latches a
hold nobody asked for and stops driving that fan. The rule is the one
tests/test_properties_fan.py holds for a head one write behind.

This file is outside tests/ because tools/pytest_phases.py, which every CI suite
pass runs under, fails a run on any xfail. Run it with:

    python -m pytest verification/findings -q -p no:cacheprovider

When the finding is fixed, the strict xfail turns this red: move the property into
tests/test_properties_fan.py, and fold NoFalseHoldTwoBehind into NoFalseHold in
docs/formal/tla/MXZCoordinator.tla.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from hypothesis import example, given
from hypothesis import strategies as st

from tests.property_rig import SEQUENCES, TARGET, open_rig

COOLING = (TARGET + 0.7, TARGET + 1.2, TARGET + 2.2, TARGET + 3.2, TARGET + 4.2, TARGET + 6)


@pytest.mark.xfail(strict=True, reason="PARKED: F1-fan-two-writes-behind see docs/formal/PARKED-FINDINGS.md")
@SEQUENCES
@given(temps=st.lists(st.sampled_from(COOLING), min_size=1, max_size=6))
@example(temps=[TARGET + 2.2, TARGET + 0.7])
async def test_no_person_no_hold_however_far_a_head_lags(
    hass: HomeAssistant, freezer: Any, temps: list[float]
) -> None:
    """Nobody touches the fan and the head reports only at the start: no hold, ever."""
    rig = await open_rig(hass, freezer, temps=(TARGET + 6, TARGET), lags=True)
    try:
        await rig.report_fan()
        for temp in temps:
            await rig.temp(0, temp)
            assert rig.fan_held() is False
        await rig.report_fan()
        assert rig.fan_held() is False
    finally:
        await rig.close()
