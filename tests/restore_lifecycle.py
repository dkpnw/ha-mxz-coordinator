"""Observe Home Assistant's real restore lifecycle without standing in for it.

``tests/test_restore_contract.py`` checks HA's restore store and getters through
these helpers. Each check that cannot be reached fails with ``UNKNOWN``, so a missing
read never passes as a good one. ``trace`` prints one ``RESTORE {json}`` line per
observation, and ``dependency_provenance`` binds a run to the installed HA restore
code and the test plugin's helpers by their sha256.
"""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from unittest.mock import patch

from homeassistant.helpers import restore_state as rs

from tests.restore_store import find_stored_state


def trace(event, **values):
    print("RESTORE " + json.dumps({"event": event, **values}, default=str, sort_keys=True))


# tests/ha_test.py takes the helpers from pytest_homeassistant_custom_component where
# it is installed, so that plugin comes first. Home Assistant ships its own plugin for cores
# the custom-component one no longer follows: pytest-homeassistant, named
# pytest-home-assistant before 2026.11.0b4, both with the same module.
_TEST_PLUGINS = (
    ("pytest-homeassistant-custom-component", "pytest_homeassistant_custom_component/common.py"),
    ("pytest-homeassistant", "pytest_homeassistant/common.py"),
    ("pytest-home-assistant", "pytest_homeassistant/common.py"),
)


def _test_plugin():
    """The installed Home Assistant pytest plugin and its helper module."""
    for plugin, helpers in _TEST_PLUGINS:
        try:
            distribution(plugin)
        except PackageNotFoundError:
            continue
        return plugin, helpers
    raise PackageNotFoundError(" or ".join(plugin for plugin, _ in _TEST_PLUGINS))


def dependency_provenance():
    """Bind the later executed contract to installed files, never guessed sources."""
    plugin, helpers = _test_plugin()
    for package, paths in (
        ("homeassistant", ("homeassistant/helpers/restore_state.py",
                           "homeassistant/helpers/storage.py",
                           "homeassistant/helpers/entity_platform.py")),
        (plugin, (helpers,)),
    ):
        installed = distribution(package)
        for path in paths:
            raw = Path(installed.locate_file(path)).read_bytes()
            trace("dependency-file", package=package, version=installed.version,
                  path=path, sha256=sha256(raw).hexdigest())


def require_restore_record(entity, loaded, record, created_at):
    """Check a real loaded record against the serialized shutdown evidence."""
    assert loaded is not None, f"UNKNOWN: no loaded restore record for {entity}"
    assert loaded.state.entity_id == entity == record["state"]["entity_id"], (
        "UNKNOWN: restore entity mismatch")
    stamp = datetime.fromisoformat(record["state"]["last_updated"])
    assert stamp >= created_at, "UNKNOWN: prior-incarnation store"
    assert loaded.state.state == record["state"]["state"], "UNKNOWN: serialized/reloaded mismatch"
    assert loaded.state.last_updated == stamp, "UNKNOWN: restored timestamp changed"
    assert (loaded.extra_data.as_dict() if loaded.extra_data else None) == record.get("extra_data"), (
        "UNKNOWN: serialized/reloaded extra data mismatch")


def require_restore_read(result, expected, entity, kind):
    """Observe the original HA getter; never manufacture its returned truth."""
    assert result is expected, f"UNKNOWN: {entity} {kind} bypassed loaded restore record"


class LifecycleReads:
    """Read-through observation; all validation happens outside entity setup."""

    def __init__(self, heads=()):
        self.calls = []
        self.heads = heads

    def wrap(self, original, kind, relevant=lambda entity: True):
        async def observed(entity):
            if not relevant(entity):
                return await original(entity)
            call = {"entity": entity, "kind": kind, "complete": False,
                    "result": None, "exception": None,
                    "heads": tuple(head.hass.states.get(head.entity_id) for head in self.heads)}
            self.calls.append(call)
            try:
                result = await original(entity)
            except BaseException as error:
                call["exception"] = error
                raise
            call["result"] = result
            call["complete"] = True
            return result
        return observed

    def validate(self, supplied, extra_required):
        expected = {(sid, kind) for sid in supplied for kind in ("state", "add")}
        expected.update((sid, "extra") for sid in extra_required)
        actual = [(c["entity"].entity_id, c["kind"]) for c in self.calls]
        assert len(actual) == len(set(actual)), "UNKNOWN: duplicate lifecycle read"
        assert set(actual) == expected, "UNKNOWN: missing/unexpected lifecycle read"
        entities = {}
        for call in self.calls:
            sid, kind = call["entity"].entity_id, call["kind"]
            assert call["complete"] and call["exception"] is None, "UNKNOWN: incomplete lifecycle"
            entity = entities.setdefault(sid, call["entity"])
            assert entity is call["entity"], "UNKNOWN: mixed entity incarnation"
            if kind != "add":
                loaded = supplied[sid]
                answer = getattr(loaded, "state" if kind == "state" else "extra_data") if loaded else None
                require_restore_read(call["result"], answer, sid, kind)


def observe_lifecycle(stack, observer, entity_class, relevant=lambda entity: True):
    for cls, method, kind in (
        (rs.RestoreEntity, "async_get_last_state", "state"),
        (rs.RestoreEntity, "async_get_last_extra_data", "extra"),
        (entity_class, "async_added_to_hass", "add"),
    ):
        stack.enter_context(patch.object(cls, method, new=observer.wrap(getattr(cls, method), kind, relevant)))


async def load_after_removal(restore, raw, entities, created_at):
    """Check the actual Store call and reconstructed records after removal."""
    removed = {sid: find_stored_state(restore.hass, sid) for sid in entities}
    real_load = restore.store.async_load
    loads = []

    async def observed_load():
        call = {"complete": False, "result": None}
        loads.append(call)
        value = await real_load()
        call.update(complete=True, result=value)
        return value

    with patch.object(restore.store, "async_load", new=observed_load):
        await restore.async_load()
    assert len(loads) == 1 and loads[0]["complete"], "UNKNOWN: missing/duplicate/incomplete Store load"
    assert loads[0]["result"] == raw["data"], "UNKNOWN: post-unload load did not read serialized shutdown store"
    records = {r["state"]["entity_id"]: r for r in raw["data"]}
    assert len(records) == len(raw["data"]), "UNKNOWN: duplicate serialized entity"
    loaded = {sid: find_stored_state(restore.hass, sid) for sid in entities}
    for sid in entities:
        assert loaded[sid] is not removed[sid], "UNKNOWN: unload-time memory record survived load"
        require_restore_record(sid, loaded[sid], records[sid], created_at)
    return loaded
