"""A setup-era or uncompleted barrier cannot certify later fan delivery."""

import asyncio

from tools.issue25.test_restore import completed_fan


def call(number=0, token="middle"):
    entered, returned = asyncio.Event(), asyncio.Event()
    entered.set()
    returned.set()
    return {"number": number, "service": "fan", "args": {"fan_mode": token},
            "entered": entered, "returned": returned, "at": 10, "returned_at": 11}


def test_new_handler_and_arguments_required():
    old, new = call(), call(1)
    assert completed_fan([old, new], 1, "middle")
    assert not completed_fan([old], 1, "middle")
    assert not completed_fan([old, new], 1, "high")
    new["returned"].clear()
    assert not completed_fan([old, new], 1, "middle")


def test_stale_gate_and_timestamp_reject():
    old, new = call(), call(1)
    new["entered"] = old["entered"]
    assert not completed_fan([old, new], 1, "middle")
    new = call(1)
    new["returned_at"] = 9
    assert not completed_fan([old, new], 1, "middle")
    new = call(1)
    new["returned"] = new["entered"]
    assert not completed_fan([old, new], 1, "middle")


def test_prerequisites_cannot_pass_when_absent_or_idle():
    from types import SimpleNamespace

    import pytest

    from tools.issue25.test_restore import require_state

    require_state(SimpleNamespace(state="cool"), "cool", "B cooling")
    require_state(SimpleNamespace(state="off"), "off", "manual OFF")
    for state in (None, "fan_only", "off", "unknown", "unavailable"):
        with pytest.raises(AssertionError, match="UNKNOWN"):
            require_state(None if state is None else SimpleNamespace(state=state), "cool", "B cooling")
    for state in (None, "on", "unknown", "unavailable"):
        with pytest.raises(AssertionError, match="UNKNOWN"):
            require_state(None if state is None else SimpleNamespace(state=state), "off", "manual OFF")


def test_restore_evidence_rejects_wrong_record_and_getter_substitution():
    from copy import deepcopy
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import pytest

    from tools.issue25.test_restore import require_restore_read, require_restore_record

    stamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    entity = "switch.invented_fan_auto"
    record = {"state": {"entity_id": entity, "state": "off", "last_updated": stamp.isoformat()},
              "extra_data": {"held": True}}
    loaded = SimpleNamespace(state=SimpleNamespace(entity_id=entity, state="off", last_updated=stamp),
                             extra_data=SimpleNamespace(as_dict=lambda: {"held": True}))
    require_restore_record(entity, loaded, record, created)
    require_restore_read(loaded.state, loaded.state, entity, "state")
    require_restore_read(None, None, entity, "missing-negative")
    with pytest.raises(AssertionError, match="UNKNOWN"):
        require_restore_read(deepcopy(loaded.state), loaded.state, entity, "state")
    with pytest.raises(AssertionError, match="UNKNOWN"):
        require_restore_read(None, loaded.state, entity, "state")
    with pytest.raises(AssertionError, match="UNKNOWN"):
        require_restore_record(entity, None, record, created)
    for field, value in (("entity_id", "switch.wrong"), ("state", "on"),
                         ("last_updated", "2025-12-01T00:00:00+00:00"),
                         ("last_updated", "2026-01-03T00:00:00+00:00")):
        wrong = deepcopy(record)
        wrong["state"][field] = value
        with pytest.raises(AssertionError, match="UNKNOWN"):
            require_restore_record(entity, loaded, wrong, created)
    wrong = deepcopy(record)
    wrong["extra_data"] = {"held": False}
    with pytest.raises(AssertionError, match="UNKNOWN"):
        require_restore_record(entity, loaded, wrong, created)


def test_changed_demand_requires_current_public_cooling_plan():
    from types import SimpleNamespace

    import pytest

    from tools.issue25.test_restore import require_cooling

    head = SimpleNamespace(state="cool")
    plan = SimpleNamespace(state="cool", attributes={"zones": [{}, {"engage": "cool"}]})
    require_cooling(head, plan, 1)
    for bad in (None, SimpleNamespace(state="unavailable"),
                SimpleNamespace(state="cool", attributes={}),
                SimpleNamespace(state="cool", attributes={"zones": []}),
                SimpleNamespace(state="cool", attributes={"zones": [{}, {}]}),
                SimpleNamespace(state="cool", attributes={"zones": [{}, {"engage": "satisfied"}]})):
        with pytest.raises(AssertionError, match="UNKNOWN"):
            require_cooling(head, bad, 1)
    with pytest.raises(AssertionError, match="UNKNOWN"):
        require_cooling(SimpleNamespace(state="fan_only"), plan, 1)
