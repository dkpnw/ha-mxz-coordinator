# Architecture rules

Three rules for changes to `custom_components/mxz_coordinator`. A reviewer holds each PR
to them; the import check below holds the module boundaries in CI, and the timer check holds
every wait on the clock.

1. **Each durable value has one obvious writer.** A stored record, restore data, or an
   option has one module and one function that writes it; everything else reads it or
   asks that owner. For example, only `OpeningPause` in `openings.py` writes and removes
   the door/window store.
2. **New work adds isolated files rather than branches in shared roots.** A new concern
   goes in its own module, which owns its state, its wakeups and its stored record, and
   reaches the coordinator through a `Protocol` it declares (as `openings.py` does with
   `OpeningHost`). Its wakeups are named waits on the clock (`host.clock.call_later`). It does not grow `coordinator.py`, `config_flow.py` or `logic.py` with
   another set of branches. See [How the coordinator is split](DEVELOPMENT.md#how-the-coordinator-is-split).
3. **Exceptions are narrow, explicit, and reviewed as architecture changes.** An exception
   names one importer and one imported module, says whether it is for type hints only,
   and gives its reason. A PR that adds, widens or removes one says so in its description,
   and is reviewed as a change to the architecture, not as a detail of the feature.

## The import check

`tools/import_bounds.py` is the one place the layers and exceptions are written. Each
module of the integration sits in one layer and imports only from layers below its own.
`const.py`, `logic.py`, `cloud.py` and `plan.py` also import only the standard library,
so their math runs without Home Assistant.

The check fails when:

- a module imports from its own layer or a layer above it, by any form of import:
  relative or absolute, inside a function, or under `TYPE_CHECKING`;
- a module has no layer, or a layer names a module that has no file;
- an exception no longer matches an import of its kind, so it is removed or narrowed
  when its reason goes away.

It does not see an import built at run time (`importlib`, `__import__`), and it does not
bound `tests/` or `tools/`.

`tests/test_import_bounds.py` runs it in every full pass of `tests/`: the CI pytest jobs
(ordinary and fresh-boot, on every lane) and the Quality coverage job. A forbidden import
turns those jobs red. To run it alone, with no
Home Assistant installed: `python3 tools/import_bounds.py`.

## The timer check

`tools/raw_timers.py` fails when any module of the integration other than `clock.py` names
Home Assistant's `async_call_later`: an import, an alias, an attribute or a call, at module
level or inside a function. Its message says to schedule the wait on `coord.clock.call_later`
instead. A wait there has a name and is cancelled when the coordinator retires; tests read it
and run it on the coordinator's clock without patching Home Assistant; and a wait that must
outlast a reload is timed from a stored record and armed again at the start, as the mode-change
hold is (PR #64). It does not see a name built at run time (`getattr` with a string), and it
does not bound `tests/` or `tools/`.

`tests/test_raw_timers.py` runs it in the same jobs as the import check. To run it alone:
`python3 tools/raw_timers.py`.

Rules 1 and 2 are not checked by a machine; review holds them.
