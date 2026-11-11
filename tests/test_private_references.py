"""No household, operator or lab-host reference in a tracked file.

The repository is public. House and operator references were cleaned out of
nine files in e317086 and came back two days later (7227b0a, "Decided by
Drew"); ten more cited DESIGN.md or DONE.md, files that were never in the
repository, so a reader could not follow them.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PRIVATE = {
    "lab host path": re.compile(r"/root/|mxz-work"),
    "operator name": re.compile(r"\bDrew\b"),
    "household equipment": re.compile(r"[Cc]arrier|[Bb]asement"),
    "house LAN address": re.compile(r"\b192\.168\."),
    "document outside the repository": re.compile(r"\b(?:DESIGN|DONE)\.md\b"),
}
# The project-local skills tell an agent on the lab host where its lanes are.
# They are agent notes, not product docs; every other rule still applies there.
ALLOWED = {".claude/skills/": {"lab host path"}}


def leaks(name: str, text: str) -> list[str]:
    allowed = next((kinds for prefix, kinds in ALLOWED.items() if name.startswith(prefix)), set())
    return [
        f"{name}:{number}: {kind}"
        for number, line in enumerate(text.splitlines(), 1)
        for kind, pattern in PRIVATE.items()
        if kind not in allowed and pattern.search(line)
    ]


def test_no_tracked_file_carries_a_private_reference() -> None:
    names = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split()
    found = []
    for name in names:
        path = ROOT / name
        if name == "tests/test_private_references.py" or not path.is_file():
            continue
        try:
            found += leaks(name, path.read_text())
        except UnicodeDecodeError:
            continue
    assert found == []


@pytest.mark.parametrize(("name", "line", "kind"), [
    ("tests/test_dry_entity.py", "Drew's design (Oct 31, 2026, /root/mxz-work/dry-entity/DESIGN.md)", "lab host path"),
    ("tests/test_door_window.py", "120 s close). Decided by Drew, Nov 3 2026", "operator name"),
    ("tests/test_floor.py", 'decision (1-11, DESIGN.md "APPROVED DECISIONS")', "document outside the repository"),
    ("custom_components/mxz_coordinator/const.py", "# e.g. climate.carrier_infinity", "household equipment"),
])
def test_a_past_leak_is_found(name: str, line: str, kind: str) -> None:
    assert f"{name}:1: {kind}" in leaks(name, line)


def test_a_house_lan_address_is_found_even_in_a_skill() -> None:
    # An invented address: a real one must never be written into the repository.
    assert leaks(".claude/skills/verify-mxz/SKILL.md", "on 192.168.0.10") == [
        ".claude/skills/verify-mxz/SKILL.md:1: house LAN address"
    ]


def test_a_skill_may_name_its_lab_lanes_and_nothing_else() -> None:
    assert leaks(".claude/skills/verify-mxz/SKILL.md", "/root/mxz-work/lanes/py314/bin/python") == []
    assert leaks("docs/DEVELOPMENT.md", "/root/mxz-work/lanes/py314/bin/python") == ["docs/DEVELOPMENT.md:1: lab host path"]
