"""Journey configuration: the suite's own pytest setup, plus the evidence writer.

The journeys live outside ``tests/`` so CI's ``pytest tests/`` never collects
them, which also means ``tests/conftest.py`` is not loaded for them. Its hook
and fixture are imported here instead, so the journeys run exactly as the
suite does: pytest-asyncio in auto mode, and the custom integration enabled
for every test that asks for ``hass``.

Run from the repository root with ``python -m pytest`` so ``tests.*`` and
``custom_components.*`` import (see ../SKILL.md).
"""

from __future__ import annotations

import pytest

from tests.conftest import auto_enable_custom_integrations, pytest_configure

__all__ = ["auto_enable_custom_integrations", "pytest_configure"]


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Keep each phase's report on the item, so a transcript records the outcome."""
    report = yield
    setattr(item, f"report_{report.when}", report)
    return report


@pytest.fixture
def journal(request: pytest.FixtureRequest):
    """Open a transcript for this journey; it is written even when the journey fails."""
    from mxz_journey import Transcript

    opened: list[Transcript] = []

    def _open(hass, entry, feature: str, *, heads: list[str]) -> Transcript:
        transcript = Transcript(hass, entry, feature, request.node.name, heads=heads)
        opened.append(transcript)
        return transcript

    yield _open
    call = getattr(request.node, "report_call", None)
    outcome = "not run" if call is None else call.outcome
    for transcript in opened:
        transcript.save(outcome)
