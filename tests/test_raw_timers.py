"""No raw Home Assistant timer outside the clock (tools/raw_timers.py, docs/ARCHITECTURE-RULES.md).

The first test is the gate on the real tree. Each refusal below adds code to a real
module's source and expects exactly the violation it makes, with the message that
points at the clock.
"""

import pytest

from tools import raw_timers
from tools.raw_timers import WHY, read_sources, violations

SOURCES = read_sources()


def added(module, code):
    """The real sources with ``code`` appended to ``module``, and its first line number."""
    sources = dict(SOURCES)
    text = sources[module]
    if not text.endswith("\n"):
        text += "\n"
    sources[module] = text + code
    return sources, text.count("\n") + 1


def test_the_integration_arms_every_wait_on_the_clock():
    assert violations(SOURCES) == []


def test_every_module_on_disk_is_checked():
    assert set(SOURCES) == {path.stem for path in raw_timers.SOURCE.glob("*.py")}
    assert {"clock", "coordinator", "issues", "openings", "dry", "heads", "delivery"} <= set(SOURCES)


def test_the_clock_is_the_one_caller(monkeypatch):
    """Not vacuous: without its allowance the clock itself is refused, and nothing else is."""
    monkeypatch.setattr(raw_timers, "ALLOWED", "nothing")
    found = violations(SOURCES)
    assert found[0] == "tools/raw_timers.py allows nothing.py, which has no file"
    assert found[1:] and all(line.startswith("clock.py:") for line in found[1:])


def test_the_message_points_at_the_clock():
    assert WHY.startswith("Do not call async_call_later in product code.")
    assert "coord.clock.call_later" in WHY
    assert "cancelled on retire" in WHY
    assert "180ee6e" in WHY


@pytest.mark.parametrize(("module", "code", "lines"), [
    ("coordinator", "from homeassistant.helpers.event import async_call_later\n", [0]),
    ("openings", "from homeassistant.helpers.event import async_call_later as later\n", [0]),
    ("issues", "from homeassistant.helpers.event import (\n    async_track_time_interval,\n    async_call_later,\n)\n", [2]),
    ("dry", "from homeassistant.helpers import event\n\n\ndef arm(hass):\n    event.async_call_later(hass, 5, print)\n", [4]),
    ("heads", "import homeassistant.helpers.event as ev\n\nCANCEL = ev.async_call_later\n", [2]),
    ("delivery", "def arm(hass, later=None):\n    later = later or async_call_later\n    return later(hass, 0, print)\n", [1]),
    ("diagnostics", ("def arm(hass):\n    from homeassistant.helpers.event import async_call_later\n"
                     "    return async_call_later(hass, 1, print)\n"), [1, 2]),
])
def test_a_raw_timer_outside_the_clock_is_refused(module, code, lines):
    sources, first = added(module, code)
    assert violations(sources) == [f"{module}.py:{first + line}: {WHY}" for line in lines]


def test_the_clock_may_call_it():
    sources, _ = added("clock", "def again(hass):\n    return async_call_later(hass, 1, print)\n")
    assert violations(sources) == []


def test_other_timers_are_not_this_ban():
    sources, _ = added(
        "coordinator",
        "from homeassistant.helpers.event import async_track_time_interval\n"
        "WHY = 'async_call_later is named in a string'\n",
    )
    assert violations(sources) == []


def test_without_the_clock_module_the_allowance_fails():
    sources = {name: text for name, text in SOURCES.items() if name != raw_timers.ALLOWED}
    assert violations(sources) == ["tools/raw_timers.py allows clock.py, which has no file"]


def test_the_command_line_exits_1_on_a_violation(tmp_path, capsys):
    (tmp_path / "clock.py").write_text("")
    (tmp_path / "late.py").write_text("from homeassistant.helpers.event import async_call_later\n")
    assert raw_timers.main(tmp_path) == 1
    assert capsys.readouterr().out == f"late.py:1: {WHY}\n"
    (tmp_path / "late.py").write_text("")
    assert raw_timers.main(tmp_path) == 0
