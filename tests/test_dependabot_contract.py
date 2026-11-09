"""Dependabot leaves the HA test locks alone and keeps updating everything else.

The locks are regenerated deliberately and pinned by sha256 in CI. GitHub documents
``ignore`` as also applying to security updates for the matching ecosystem and
directory, and ``exclude-paths`` and ``open-pull-requests-limit`` as version-updates
only. Whether GitHub then opens no pull request can only be seen on GitHub; this test
holds the configuration that the documentation describes.
"""

from collections import Counter
from copy import deepcopy
from importlib.metadata import requires
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).parents[1]
LOCKS = "/requirements"
LOCK_EXCLUDE = "requirements/**"


def load(path):
    # BaseLoader keeps every scalar a string, as in test_ci_contract.py.
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def lock_directories(root):
    """/requirements, and each directory under it that holds a .txt file."""
    return {LOCKS} | {"/" + path.parent.relative_to(root).as_posix()
                      for path in (root / "requirements").rglob("*.txt")}


def entry_directories(entry):
    directories = entry.get("directories", [])
    if "directory" in entry:
        directories = [*directories, entry["directory"]]
    return ["/" + directory.strip("/") for directory in directories]


def is_blanket_ignore(rule):
    # The documented wildcard matches zero or more characters, so "**" means "*" too.
    return not rule.get("dependency-name", "x").strip("*")


def violations(config, locks):
    errors = []
    updates = config.get("updates", [])
    pip = [e for e in updates if e.get("package-ecosystem") == "pip"]
    for directory in sorted(locks):
        owners = [e for e in pip if directory in entry_directories(e)]
        if not owners:
            errors.append(f"no pip entry covers {directory}")
        for entry in owners:
            # A narrowed rule (versions, update-types) would let other updates through.
            if {"dependency-name": "*"} not in entry.get("ignore", []):
                errors.append(f"{directory} lacks ignore *")
            # Documented: with target-branch, an entry's options stop applying to security updates.
            if "target-branch" in entry:
                errors.append(f"{directory} has target-branch")
    root = [e for e in pip if "/" in entry_directories(e)]
    if not any(LOCK_EXCLUDE in e.get("exclude-paths", []) for e in root):
        errors.append("/ pip entry lacks requirements/** exclude")
    actions = [e for e in updates if e.get("package-ecosystem") == "github-actions"]
    if not actions:
        errors.append("no github-actions entry")
    for name, entries in (("/ pip", root), ("github-actions", actions)):
        if any(is_blanket_ignore(rule) for e in entries for rule in e.get("ignore", [])):
            errors.append(f"{name} entry has a blanket ignore")
    # Documented: blocks for one ecosystem and target branch must not repeat a directory.
    counts = Counter(d for e in pip for d in entry_directories(e))
    errors += [f"pip directory {d} defined twice" for d, count in sorted(counts.items()) if count > 1]
    return errors


@pytest.fixture
def config():
    return load(ROOT / ".github/dependabot.yml")


def entry(config, ecosystem, directory):
    return next(e for e in config["updates"]
                if e["package-ecosystem"] == ecosystem and e.get("directory") == directory)


def test_declared_dependabot_boundary(config):
    assert violations(config, lock_directories(ROOT)) == []


def test_every_lane_lock_is_in_a_directory_the_contract_requires():
    """The lane matrix is an independent list of the locks: none may sit outside the scan."""
    workflow = load(ROOT / ".github/workflows/ci.yml")
    include = workflow["jobs"]["pytest"]["strategy"]["matrix"]["include"]
    parents = {"/" + Path(lane["constraints"]).parent.as_posix() for lane in include}
    assert LOCKS in parents
    assert parents <= lock_directories(ROOT)


def _drop_lock_entry(c):
    c["updates"].remove(entry(c, "pip", LOCKS))


def _set(ecosystem, directory, key, value):
    return lambda c: entry(c, ecosystem, directory).update({key: value})


def _pop(ecosystem, directory, key):
    return lambda c: entry(c, ecosystem, directory).pop(key)


def _merge_locks_into_root(c):
    _drop_lock_entry(c)
    root = entry(c, "pip", "/")
    root["directories"] = ["/requirements", root.pop("directory")]
    root["ignore"] = [{"dependency-name": "*"}]


def _duplicate_lock_directory(c):
    c["updates"].append({"package-ecosystem": "pip", "directory": LOCKS,
                         "ignore": [{"dependency-name": "*"}], "schedule": {"interval": "weekly"}})


# Exactly one boundary changed each time: a guard that never matches cannot hide behind another.
@pytest.mark.parametrize(("change", "expected"), [
    (_drop_lock_entry, ["no pip entry covers /requirements"]),
    (_set("pip", LOCKS, "directory", "/requirements/**"), ["no pip entry covers /requirements"]),
    (_set("pip", LOCKS, "package-ecosystem", "npm"), ["no pip entry covers /requirements"]),
    (_pop("pip", LOCKS, "ignore"), ["/requirements lacks ignore *"]),
    (_set("pip", LOCKS, "ignore", [{"dependency-name": "pillow"}]), ["/requirements lacks ignore *"]),
    (_set("pip", LOCKS, "ignore", [{"dependency-name": "*", "update-types": ["version-update:semver-major"]}]),
     ["/requirements lacks ignore *"]),
    (_set("pip", LOCKS, "target-branch", "invented-branch"), ["/requirements has target-branch"]),
    (_pop("pip", "/", "exclude-paths"), ["/ pip entry lacks requirements/** exclude"]),
    (_set("pip", "/", "exclude-paths", ["requirements/*.txt"]), ["/ pip entry lacks requirements/** exclude"]),
    (_set("pip", "/", "ignore", [{"dependency-name": "*"}]), ["/ pip entry has a blanket ignore"]),
    (_set("pip", "/", "ignore", [{"dependency-name": "**"}]), ["/ pip entry has a blanket ignore"]),
    (_set("pip", "/", "ignore", [{"dependency-name": "*", "update-types": ["version-update:semver-major"]}]),
     ["/ pip entry has a blanket ignore"]),
    (_set("github-actions", "/", "ignore", [{"dependency-name": "*"}]), ["github-actions entry has a blanket ignore"]),
    (_merge_locks_into_root, ["/ pip entry has a blanket ignore"]),
    (_duplicate_lock_directory, ["pip directory /requirements defined twice"]),
])
def test_weakened_dependabot_boundary_rejects(config, change, expected):
    bad = deepcopy(config)
    change(bad)
    assert violations(bad, lock_directories(ROOT)) == expected


def test_a_missing_root_or_actions_entry_rejects(config):
    bad = deepcopy(config)
    bad["updates"].remove(entry(bad, "pip", "/"))
    assert violations(bad, lock_directories(ROOT)) == ["/ pip entry lacks requirements/** exclude"]
    bad = deepcopy(config)
    bad["updates"].remove(entry(bad, "github-actions", "/"))
    assert violations(bad, lock_directories(ROOT)) == ["no github-actions entry"]


def test_a_named_ignore_on_a_shippable_entry_is_allowed(config):
    """Only a blanket ignore takes updates away from every shippable dependency."""
    entry(config, "pip", "/")["ignore"] = [{"dependency-name": "invented-*"}]
    entry(config, "github-actions", "/")["ignore"] = [{"dependency-name": "actions/invented"}]
    assert violations(config, lock_directories(ROOT)) == []


def _tree(root, *names):
    for name in names:
        path = root / "requirements" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("invented\n")
    return lock_directories(root)


def test_a_subdirectory_lock_needs_its_own_covered_directory(config, tmp_path):
    locks = _tree(tmp_path, "constraints-top.txt", "invented/lock.txt", "deep/er/constraints.txt")
    assert locks == {LOCKS, "/requirements/invented", "/requirements/deep/er"}
    assert violations(config, locks) == ["no pip entry covers /requirements/deep/er",
                                         "no pip entry covers /requirements/invented"]
    lock_entry = entry(config, "pip", LOCKS)
    del lock_entry["directory"]
    lock_entry["directories"] = sorted(locks)
    assert violations(config, locks) == []
    # Covered by a second entry that does not ignore everything: still refused.
    lock_entry["directories"] = [LOCKS, "/requirements/deep/er"]
    config["updates"].append({"package-ecosystem": "pip", "directory": "/requirements/invented"})
    assert violations(config, locks) == ["/requirements/invented lacks ignore *"]


def test_a_subdirectory_without_a_txt_requires_nothing(config, tmp_path):
    """As requirements/ha2026.10 is today: its .lock and .json files are not pip manifests."""
    assert _tree(tmp_path, "bundle/artifacts.lock", "bundle/bundle.json", "bundle/README.md") == {LOCKS}
    assert violations(config, {LOCKS}) == []


PLUGIN = "pytest-homeassistant-custom-component"


def plugin_exact_pins(plugin_requires):
    """Names the HA test plugin pins with ==, read from its own metadata."""
    pins = set()
    for line in plugin_requires:
        requirement = Requirement(line)
        if [spec.operator for spec in requirement.specifier] == ["=="]:
            pins.add(canonicalize_name(requirement.name))
    return pins


def named_ignore_violations(config, test_requirements, plugin_requires):
    """The / pip entry ignores exactly the test requirements the HA test plugin caps.

    Each plugin release pins pytest and pytest-asyncio exactly and pins homeassistant
    exactly, so the minimum-HA lane cannot install a raised floor for any of them
    (Dependabot PR #108). An extra name would take updates from a dependency nothing
    caps; a missing one lets Dependabot open a pull request that cannot install.
    """
    pinned = plugin_exact_pins(plugin_requires)
    capped = {canonicalize_name(Requirement(line.split("#", 1)[0]).name)
              for line in test_requirements.splitlines() if line.split("#", 1)[0].strip()} & pinned
    if canonicalize_name("homeassistant") in pinned:
        capped.add(PLUGIN)
    errors = []
    ignored = set()
    for e in config.get("updates", []):
        if e.get("package-ecosystem") != "pip" or "/" not in entry_directories(e):
            continue
        for rule in e.get("ignore", []):
            name = canonicalize_name(rule.get("dependency-name", ""))
            ignored.add(name)
            # A narrowed rule (versions, update-types) would still let a bump through.
            if set(rule) != {"dependency-name"}:
                errors.append(f"/ pip ignore of {name} is narrowed")
    errors += [f"/ pip entry ignores {name}, which the HA test plugin does not pin"
               for name in sorted(ignored - capped)]
    errors += [f"/ pip entry does not ignore {name}" for name in sorted(capped - ignored)]
    return errors


@pytest.fixture
def test_requirements():
    return (ROOT / "requirements_test.txt").read_text()


@pytest.fixture
def plugin_requires():
    return requires(PLUGIN)


def test_the_root_entry_ignores_what_the_ha_test_plugin_pins(config, test_requirements, plugin_requires):
    assert named_ignore_violations(config, test_requirements, plugin_requires) == []
    # The independent oracle: the installed plugin really pins these three.
    assert {"pytest", "pytest-asyncio", "homeassistant"} <= plugin_exact_pins(plugin_requires)


def _root_ignore(c):
    return entry(c, "pip", "/")["ignore"]


@pytest.mark.parametrize(("change", "expected"), [
    (lambda c: _root_ignore(c).append({"dependency-name": "hypothesis"}),
     ["/ pip entry ignores hypothesis, which the HA test plugin does not pin"]),
    (lambda c: _root_ignore(c).append({"dependency-name": "invented-*"}),
     ["/ pip entry ignores invented-*, which the HA test plugin does not pin"]),
    (lambda c: _root_ignore(c).remove({"dependency-name": "pytest"}), ["/ pip entry does not ignore pytest"]),
    (lambda c: _root_ignore(c).remove({"dependency-name": PLUGIN}), [f"/ pip entry does not ignore {PLUGIN}"]),
    (lambda c: entry(c, "pip", "/").pop("ignore"),
     ["/ pip entry does not ignore pytest", "/ pip entry does not ignore pytest-asyncio",
      f"/ pip entry does not ignore {PLUGIN}"]),
    (lambda c: _root_ignore(c)[0].update({"update-types": ["version-update:semver-major"]}),
     ["/ pip ignore of pytest is narrowed"]),
    # Names compare normalized, as Dependabot and pip do.
    (lambda c: _root_ignore(c)[1].update({"dependency-name": "Pytest_Asyncio"}), []),
])
def test_a_wrong_named_ignore_on_the_root_entry_is_refused(config, test_requirements, plugin_requires,
                                                            change, expected):
    assert [rule["dependency-name"] for rule in _root_ignore(config)] == [
        "pytest", "pytest-asyncio", PLUGIN]
    change(config)
    assert named_ignore_violations(config, test_requirements, plugin_requires) == expected


def test_an_ignore_outlives_the_pin_that_justified_it(config, test_requirements):
    """If a plugin release stopped pinning pytest, the pytest ignore would have to go."""
    invented = ["pytest-asyncio==0.24.0", "pytest>=8", "homeassistant==2024.12.5"]
    assert named_ignore_violations(config, test_requirements, invented) == [
        "/ pip entry ignores pytest, which the HA test plugin does not pin"]
    # Nor does the plugin itself stay ignored once it stops pinning homeassistant.
    invented = ["pytest-asyncio==0.24.0", "pytest==8.3.3", "homeassistant>=2024.12"]
    assert named_ignore_violations(config, test_requirements, invented) == [
        f"/ pip entry ignores {PLUGIN}, which the HA test plugin does not pin"]
