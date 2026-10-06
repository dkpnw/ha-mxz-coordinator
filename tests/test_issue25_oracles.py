"""A setup-era or uncompleted barrier cannot certify later fan delivery."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize("case", [
    "exact", "no-extra", "absent-record", "copy-state", "copy-extra", "wrong-state", "wrong-extra",
    "missing-state", "missing-extra", "duplicate-state", "unexpected-extra", "getter-error",
    "add-error", "extra-error", "incomplete-add", "missing-add", "duplicate-add",
    "ownership-precedence", "mixed-entity",
])
async def test_transparent_lifecycle_observers_validate_after_caller(case):
    from tools.issue25.test_restore import LifecycleReads, Trial

    entity = SimpleNamespace(entity_id="switch.invented")
    loaded = SimpleNamespace(state={"state": "unavailable"}, extra_data={"token": 1})
    absent = case == "absent-record"
    supplied = {entity.entity_id: None if absent else loaded}
    expected_extra = set() if case == "no-extra" else {entity.entity_id}
    observer = LifecycleReads()
    exception = RuntimeError("invented original exception")
    trailing = []
    result_token = object()

    async def state_getter(entity):
        if case == "getter-error":
            raise exception
        if case == "copy-state":
            return deepcopy(loaded.state)
        if case == "wrong-state":
            return object()
        return None if absent else loaded.state

    async def extra_getter(entity):
        if case == "extra-error":
            raise exception
        if case == "copy-extra":
            return deepcopy(loaded.extra_data)
        if case == "wrong-extra":
            return object()
        return None if absent else loaded.extra_data

    async def add(entity):
        if case != "missing-state":
            await observer.wrap(state_getter, "state")(entity)
        if case == "duplicate-state":
            await observer.wrap(state_getter, "state")(entity)
        if case not in ("no-extra", "missing-extra"):
            other = SimpleNamespace(entity_id=entity.entity_id) if case == "mixed-entity" else entity
            await observer.wrap(extra_getter, "extra")(other)
        if case == "add-error":
            raise exception
        trailing.append("original caller completed")
        return result_token

    if case in ("getter-error", "extra-error", "add-error"):
        with pytest.raises(RuntimeError) as caught:
            await observer.wrap(add, "add")(entity)
        assert caught.value is exception
        assert not trailing
    else:
        assert await observer.wrap(add, "add")(entity) is result_token
        assert trailing == ["original caller completed"]
    if case == "unexpected-extra":
        expected_extra = set()
    if case == "missing-add":
        observer.calls = [c for c in observer.calls if c["kind"] != "add"]
    if case == "duplicate-add":
        observer.calls.append(next(c for c in observer.calls if c["kind"] == "add"))
    if case in ("incomplete-add", "ownership-precedence"):
        next(c for c in observer.calls if c["kind"] == "add")["complete"] = False
    if case in ("exact", "no-extra", "absent-record"):
        observer.validate(supplied, expected_extra)
    else:
        with pytest.raises(AssertionError, match="UNKNOWN"):
            # A previous ownership error cannot hide the invalid next lifecycle.
            trial = Trial(None, None, [], None)
            trial.errors.append("earlier ownership mismatch")
            observer.validate(supplied, expected_extra)
            trial.finish()


@pytest.mark.parametrize("zones", [None, {}, [], [None], [{}], [{"fan_hold": None}],
                                  [{"fan_hold": 0}], [{"fan_hold": "false"}]],
                         ids=["absent", "mapping", "empty", "no-room", "no-hold", "null-hold", "integer", "string"])
def test_missing_or_malformed_public_plan_is_unknown(zones):
    from tools.issue25.test_restore import public_hold

    with pytest.raises(AssertionError, match="UNKNOWN"):
        public_hold(SimpleNamespace(state="cool", attributes={"zones": zones}), 0)
    assert public_hold(SimpleNamespace(state="cool", attributes={"zones": [{"fan_hold": False}]}), 0) is False


@pytest.mark.parametrize("case", ["zero", "two", "wrong-entry", "one"])
def test_registry_identity_has_no_guessed_fallback(monkeypatch, case):
    from tools.issue25 import test_restore as restore

    rows = [] if case == "zero" else [SimpleNamespace(entity_id="switch.invented", unique_id="x_primary_fan_auto",
                                                     config_entry_id="other" if case == "wrong-entry" else "entry")]
    if case == "two":
        rows.append(SimpleNamespace(entity_id="switch.duplicate", unique_id="y_primary_fan_auto", config_entry_id="entry"))
    monkeypatch.setattr(restore.er, "async_get", lambda hass: SimpleNamespace(entities=dict(enumerate(rows))))
    trial = restore.Trial(None, None, [], SimpleNamespace(entry_id="entry"))
    if case == "one":
        assert trial.switch(0) == "switch.invented"
    else:
        with pytest.raises(AssertionError, match="UNKNOWN"):
            trial.switch(0)


@pytest.mark.parametrize("case", ["missing", "duplicate", "incomplete", "wrong-data", "old-record"])
async def test_post_removal_load_contract_rejects(case):
    from tools.issue25.test_restore import load_after_removal

    store = SimpleNamespace()
    old = object()
    restore = SimpleNamespace(store=store, last_states={"sensor.invented": old})
    raw = {"data": []}

    async def load():
        if case == "incomplete":
            raise RuntimeError("invented load failure")
        return [{}] if case == "wrong-data" else []

    async def reload():
        if case == "missing":
            return
        await store.async_load()
        if case == "duplicate":
            await store.async_load()

    store.async_load = load
    restore.async_load = reload
    with pytest.raises((AssertionError, RuntimeError)):
        await load_after_removal(restore, raw, ["sensor.invented"], None)
