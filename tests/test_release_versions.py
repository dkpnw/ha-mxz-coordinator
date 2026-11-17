"""The manifest, the CHANGELOG and the README name the same release.

Checked on every pull request, so a disagreement is caught before a tag exists. The
release-provenance workflow still checks the tag against the manifest when a release is
published.

- A release version ``X.Y.Z``: the CHANGELOG's newest release section is ``X.Y.Z``, and
  the README's newest "What's new" section is for the same minor, at or below ``X.Y.Z``
  (a patch may keep ``X.Y.0``'s section).
- A beta ``X.Y.ZbN``: its notes stay under "Unreleased", so neither the CHANGELOG nor the
  README has a section at or above ``X.Y.Z`` yet.
"""

import json
import re
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).parents[1]


def version_errors(manifest_version, changelog, readme):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(b\d+)?", manifest_version)
    if not match:
        return ["manifest version is neither X.Y.Z nor X.Y.ZbN"]
    major, minor, patch, beta = match.groups()
    base = Version(f"{major}.{minor}.{patch}")
    released = re.findall(r"^## (\d+\.\d+\.\d+(?:b\d+)?) \(", changelog, re.MULTILINE)
    whats_new = re.findall(r"^## What's new in (\d+\.\d+\.\d+)\s*$", readme, re.MULTILINE)
    if not released or not whats_new:
        return ["no release section in the CHANGELOG or the README"]
    newest, shown = Version(released[0]), Version(whats_new[0])
    errors = []
    if beta:
        if newest >= base:
            errors.append(f"CHANGELOG has {newest}, which beta {manifest_version} has not reached")
        if shown >= base:
            errors.append(f"README's What's new is {shown}, which beta {manifest_version} has not reached")
        return errors
    if released[0] != manifest_version:
        errors.append(f"CHANGELOG's newest release is {released[0]}, the manifest is {manifest_version}")
    if shown.release[:2] != base.release[:2] or shown > base:
        errors.append(f"README's What's new is {shown}, the manifest is {manifest_version}")
    return errors


def _repository():
    manifest = json.loads((ROOT / "custom_components/mxz_coordinator/manifest.json").read_text())
    return manifest["version"], (ROOT / "CHANGELOG.md").read_text(), (ROOT / "README.md").read_text()


def test_the_manifest_changelog_and_readme_agree():
    assert version_errors(*_repository()) == []


def _changelog(*versions):
    return "# Changelog\n\n## Unreleased\n\n" + "".join(f"## {v} (November 1, 2026)\n\n- Invented.\n\n"
                                                       for v in versions)


def _readme(*versions):
    return "# Invented\n\n" + "".join(f"## What's new in {v}\n\nInvented.\n\n" for v in versions)


@pytest.mark.parametrize(("manifest", "changelog", "readme"), [
    ("3.22.0", ("3.22.0", "3.21.0"), ("3.22.0", "3.21.0")),
    # A patch keeps its minor's What's new (3.13.2) or writes its own (3.11.1).
    ("3.13.2", ("3.13.2", "3.13.1"), ("3.13.0",)),
    ("3.11.1", ("3.11.1", "3.11.0"), ("3.11.1", "3.11.0")),
    # A beta's notes stay under Unreleased (3.22.0b1).
    ("3.22.0b1", ("3.21.0",), ("3.21.0",)),
])
def test_releases_as_they_were_tagged_are_accepted(manifest, changelog, readme):
    assert version_errors(manifest, _changelog(*changelog), _readme(*readme)) == []


@pytest.mark.parametrize(("manifest", "changelog", "readme", "expected"), [
    # v3.14.0 as tagged: the README still showed 3.13.0.
    ("3.14.0", ("3.14.0", "3.13.2"), ("3.13.0",), ["README's What's new is 3.13.0, the manifest is 3.14.0"]),
    # A manifest left behind its release, as v3.12.1's was at 3.12.0. (There the CHANGELOG
    # was behind too, and only the tag disagreed; the provenance workflow checks the tag.)
    ("3.12.0", ("3.12.1", "3.12.0"), ("3.12.0",), ["CHANGELOG's newest release is 3.12.1, the manifest is 3.12.0"]),
    ("3.22.0", ("3.21.0",), ("3.22.0",), ["CHANGELOG's newest release is 3.21.0, the manifest is 3.22.0"]),
    ("3.22.0", ("3.22.0",), ("3.23.0",), ["README's What's new is 3.23.0, the manifest is 3.22.0"]),
    ("3.13.1", ("3.13.1",), ("3.13.2",), ["README's What's new is 3.13.2, the manifest is 3.13.1"]),
    ("3.22.0b1", ("3.22.0",), ("3.21.0",), ["CHANGELOG has 3.22.0, which beta 3.22.0b1 has not reached"]),
    ("3.22.0b1", ("3.21.0",), ("3.22.0",), ["README's What's new is 3.22.0, which beta 3.22.0b1 has not reached"]),
    ("3.22", ("3.22.0",), ("3.22.0",), ["manifest version is neither X.Y.Z nor X.Y.ZbN"]),
    ("3.22.0-beta.1", ("3.21.0",), ("3.21.0",), ["manifest version is neither X.Y.Z nor X.Y.ZbN"]),
    ("3.22.0", (), ("3.22.0",), ["no release section in the CHANGELOG or the README"]),
    ("3.22.0", ("3.22.0",), (), ["no release section in the CHANGELOG or the README"]),
])
def test_a_disagreement_is_refused(manifest, changelog, readme, expected):
    assert version_errors(manifest, _changelog(*changelog), _readme(*readme)) == expected
