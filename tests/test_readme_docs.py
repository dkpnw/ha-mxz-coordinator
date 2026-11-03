"""Doc contracts for README and CHANGELOG lines that drift from their sources.

- The README's Configure table names each room page field and section by the label
  the page shows (strings.json is the oracle).
- The README states the Mushroom climate card's +/- wait as Mushroom v5.2.3 ships it:
  ``DEFAULT_DEBOUCE_TIME = 2000`` (milliseconds) in ``src/shared/input-number.ts``,
  overridable by a theme through ``--mush-input-number-debounce``
  (``src/utils/theme.ts``). The tests can't fetch Mushroom, so the value is pinned
  here with its source.
- CHANGELOG 3.10.0 says CONTRIBUTING.md replaced "3.9.0's stance"; the 3.9.0 notes
  must state that stance.
"""

import json
import re
from pathlib import Path

import pytest

ROOM_STEP = json.loads(
    Path("custom_components/mxz_coordinator/strings.json").read_text()
)["options"]["step"]["room"]


def _rooms_row(readme: str) -> str:
    rows = [line for line in readme.splitlines() if line.startswith("| **Rooms** |")]
    assert len(rows) == 1, rows
    return rows[0]


def _room_page_names() -> list[str]:
    fields = list(ROOM_STEP["data"].values())
    sections = [section["name"] for section in ROOM_STEP["sections"].values()]
    return fields + sections


def _missing_room_page_names(row: str) -> list[str]:
    return [name for name in _room_page_names() if f"**{name}**" not in row]


def test_room_page_names_come_from_the_page() -> None:
    """The oracle reads the real page: the hands-off switch and floor section are in it."""
    names = _room_page_names()
    assert "Let its own remote run it while disabled" in names
    assert "Freeze floor and overheat ceiling" in names
    assert len(names) == len(set(names)) >= 10


def test_rooms_row_names_every_room_page_field_and_section() -> None:
    row = _rooms_row(Path("README.md").read_text())
    assert _missing_room_page_names(row) == []


def test_rooms_row_check_reports_a_dropped_name() -> None:
    """Negative: a row without the hands-off switch or the floor section is caught."""
    row = _rooms_row(Path("README.md").read_text())
    for name in (
        "Let its own remote run it while disabled",
        "Freeze floor and overheat ceiling",
    ):
        assert _missing_room_page_names(row.replace(f"**{name}**", name)) == [name]


# lovelace-mushroom v5.2.3: src/shared/input-number.ts:20 and src/utils/theme.ts:78.
MUSHROOM_DEFAULT_WAIT_MS = 2000
MUSHROOM_THEME_VARIABLE = "mush-input-number-debounce"


def _mushroom_wait_bullet(readme: str) -> str:
    section = readme.split("## Dashboard cards", 1)[1].split("\n## ", 1)[0]
    bullets = [b for b in section.split("\n- ") if "+ or -" in b]
    assert len(bullets) == 1, bullets
    return " ".join(bullets[0].split())


def test_mushroom_wait_matches_mushroom_source() -> None:
    bullet = _mushroom_wait_bullet(Path("README.md").read_text())
    seconds = MUSHROOM_DEFAULT_WAIT_MS // 1000
    assert f"{seconds} seconds by default" in bullet
    assert f"`{MUSHROOM_THEME_VARIABLE}`" in bullet
    assert "each tap starts the wait again" in bullet
    assert "about" not in bullet
    assert "not MXZ's" in bullet


def _changelog_section(changelog: str, version: str) -> str:
    match = re.search(
        rf"^## {re.escape(version)}\b.*?(?=^## )", changelog, re.MULTILINE | re.DOTALL
    )
    assert match, version
    return match.group(0)


def test_changelog_390_states_the_stance_310_replaced() -> None:
    changelog = Path("CHANGELOG.md").read_text()
    assert "3.9.0's stance" in _changelog_section(changelog, "3.10.0")
    notes = " ".join(_changelog_section(changelog, "3.9.0").split())
    assert (
        "[CONTRIBUTING.md](CONTRIBUTING.md) said MXZ wouldn't add a dual setpoint"
        in notes
    )
    assert "3.10.0 replaced that with a per-room opt-in" in notes


def test_changelog_section_finder_rejects_a_missing_version() -> None:
    """Negative: the section finder fails on a version the changelog doesn't have."""
    with pytest.raises(AssertionError):
        _changelog_section("## 1.0.0\n\n- x\n\n## 0.9.0\n", "3.9.0")


def _dropped_previous_section(readme: str, changelog: str) -> str | None:
    """The x.y.0 release just below the newest What's new section, if its section is gone.

    Only x.y.0 counts: patch releases usually have no section of their own.
    """
    sections = re.findall(r"^## What's new in (\d+\.\d+\.\d+)\b", readme, re.MULTILINE)
    versions = re.findall(r"^## (\d+\.\d+\.\d+)\b", changelog, re.MULTILINE)
    earlier = versions[versions.index(sections[0]) + 1 :]
    previous = next(version for version in earlier if version.endswith(".0"))
    return None if previous in sections else previous


def test_readme_keeps_the_previous_release_section() -> None:
    """A release pushes the previous What's new section down; it doesn't replace it."""
    assert (
        _dropped_previous_section(
            Path("README.md").read_text(), Path("CHANGELOG.md").read_text()
        )
        is None
    )


@pytest.mark.parametrize(
    ("sections", "dropped"),
    [
        (["2.1.0", "2.0.0", "1.9.0"], None),
        (["2.1.0", "2.0.0"], None),
        (["2.0.1", "2.0.0"], None),
        (["2.1.0", "1.9.0"], "2.0.0"),
        (["2.1.0"], "2.0.0"),
        (["2.0.1", "1.9.0"], "2.0.0"),
        (["2.1.0 (unreleased)", "1.9.0"], "2.0.0"),
    ],
)
def test_previous_section_check_reports_a_dropped_section(
    sections: list[str], dropped: str | None
) -> None:
    """Negative included: 2.0.1 has no section of its own, but 2.0.0 must keep its."""
    changelog = "## 2.1.0 (x)\n\n## 2.0.1 (x)\n\n## 2.0.0 (x)\n\n## 1.9.0 (x)\n"
    readme = "\n".join(f"## What's new in {section}\n" for section in sections)
    assert _dropped_previous_section(readme, changelog) == dropped
