"""One real exported trial, without the ownership or restart schedules."""

import pytest
import test_restore as restore_harness

trial = restore_harness.trial


@pytest.mark.usefixtures("trial")
def test_exported_setup(request):
    subject = request.getfixturevalue("trial")
    plan = subject.hass.states.get(subject.eid("_plan"))
    for room in range(2):
        restore_harness.require_state(subject.hass.states.get(subject.switch(room)), "on", "initial fan-auto")
        assert restore_harness.public_hold(plan, room) is False, "UNKNOWN: initial public hold"
        assert subject.heads[room].calls, "UNKNOWN: no initial real handler"
    assert not subject.errors and not subject.unknown, "UNKNOWN: initial prerequisites"
