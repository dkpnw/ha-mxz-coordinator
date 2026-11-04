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
- Every issue form has what GitHub requires, and the generic head report asks for what
  a report needs. Tested by users lists only reports with a public GitHub link.
- RELEASING.md states the release cadence, and its step references point at the right steps.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

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


def _diagnostics_section(readme: str) -> str:
    section = readme.split("### Download diagnostics for a bug report", 1)[1]
    return " ".join(re.split(r"^##+ ", section, maxsplit=1, flags=re.MULTILINE)[0].split())


def test_readme_says_how_to_download_diagnostics_and_what_is_removed() -> None:
    section = _diagnostics_section(Path("README.md").read_text())
    assert "Settings → Devices & services → MXZ Coordinator" in section
    assert "**Download diagnostics**" in section
    removed, kept = section.split("**Kept:**")
    for name in (
        "tokens", "passwords", "user names", "e-mail addresses", "MAC addresses",
        "serial numbers", "IP addresses", "latitude and longitude",
        "cloud account identifiers",
    ):
        assert name in removed, name
    # It must not promise more than the redaction does.
    assert "room names" in kept
    assert "entity IDs" in kept


def test_diagnostics_section_finder_stops_at_the_next_heading() -> None:
    """Negative: another section's words don't count as this section's."""
    readme = "### Download diagnostics for a bug report\n\nmine\n\n## Next\n\ntheirs\n"
    assert _diagnostics_section(readme) == "mine"


def test_bug_report_asks_for_the_diagnostics_file() -> None:
    template = Path(".github/ISSUE_TEMPLATE/bug_report.yml").read_text()
    field = template.split("id: diagnostics_file", 1)[1].split("- type:", 1)[0]
    assert "label: Diagnostics file" in field
    assert "Download diagnostics" in field
    assert "Settings → Devices & services → MXZ Coordinator" in field
    assert "#download-diagnostics-for-a-bug-report" in field


def test_changelog_notes_this_under_unreleased_with_no_version_of_its_own() -> None:
    changelog = Path("CHANGELOG.md").read_text()
    unreleased = changelog.split("## Unreleased", 1)[1].split("\n## ", 1)[0]
    assert "Download diagnostics" in unreleased
    assert "repair" in unreleased


ISSUE_FORMS = sorted(
    path for path in Path(".github/ISSUE_TEMPLATE").glob("*.yml") if path.name != "config.yml"
)
GENERIC_REPORT = Path(".github/ISSUE_TEMPLATE/generic-head-report.yml")
GENERIC_REPORT_REQUIRED = {
    "brand", "model", "integration", "ha_version", "mxz_version", "worked", "did_not_work",
}


def _form_problems(form: dict) -> list[str]:
    """What GitHub's issue form schema would refuse in one form."""
    problems = [key for key in ("name", "description", "body") if not form.get(key)]
    fields = [item for item in form.get("body") or [] if item.get("type") != "markdown"]
    ids = [item.get("id") for item in fields]
    if not fields:
        problems.append("no input field")
    if len(ids) != len(set(ids)):
        problems.append("duplicate id")
    problems += [
        f"no label: {item.get('id')}" for item in fields
        if not item.get("attributes", {}).get("label")
    ]
    return problems


def _generic_report_gaps(form: dict) -> list[str]:
    """The fields a generic head report must require and doesn't."""
    required = {
        item.get("id") for item in form["body"]
        if item.get("validations", {}).get("required") is True
    }
    return sorted(GENERIC_REPORT_REQUIRED - required)


def test_there_are_issue_forms_to_check() -> None:
    assert {path.name for path in ISSUE_FORMS} >= {
        "bug_report.yml", "cloud_beta_report.yml", GENERIC_REPORT.name,
    }


@pytest.mark.parametrize("path", ISSUE_FORMS, ids=lambda path: path.name)
def test_issue_form_has_what_github_requires(path: Path) -> None:
    assert _form_problems(yaml.safe_load(path.read_text())) == []


def test_form_check_reports_a_broken_form() -> None:
    """Negative: the check above can fail."""
    field = {"type": "input", "id": "a", "attributes": {"label": "A"}}
    good = {"name": "n", "description": "d", "body": [field]}
    assert _form_problems(good) == []
    assert _form_problems({**good, "description": ""}) == ["description"]
    assert _form_problems({**good, "body": [field, field]}) == ["duplicate id"]
    assert _form_problems({**good, "body": [{"type": "input", "id": "a"}]}) == ["no label: a"]
    assert "no input field" in _form_problems({**good, "body": [{"type": "markdown"}]})


def test_generic_head_report_asks_for_what_a_report_needs() -> None:
    form = yaml.safe_load(GENERIC_REPORT.read_text())
    assert _generic_report_gaps(form) == []
    field = next(item for item in form["body"] if item.get("id") == "diagnostics_file")
    description = field["attributes"]["description"]
    assert "Download diagnostics" in description
    assert "Review it before you post it" in description
    assert "#download-diagnostics-for-a-bug-report" in description


def test_generic_report_check_reports_a_dropped_field() -> None:
    """Negative: a field gone, or no longer required, is reported."""
    form = yaml.safe_load(GENERIC_REPORT.read_text())
    dropped = {"body": [item for item in form["body"] if item.get("id") != "brand"]}
    assert _generic_report_gaps(dropped) == ["brand"]
    optional = yaml.safe_load(GENERIC_REPORT.read_text())
    for item in optional["body"]:
        if item.get("id") == "model":
            item["validations"]["required"] = False
    assert _generic_report_gaps(optional) == ["model"]


def _tested_by_users(features: str) -> str:
    return features.split("### Tested by users", 1)[1].split("\n## ", 1)[0]


def _reports_without_a_public_link(section: str) -> list[str]:
    """List rows that don't link to a public issue or discussion of this repository."""
    public = r"https://github\.com/dkpnw/ha-mxz-coordinator/(issues|discussions)/\d+"
    return [
        line for line in section.splitlines()
        if line.startswith(("- ", "* ", "| ")) and not re.search(public, line)
    ]


def test_tested_by_users_is_honest_and_generic_heads_stay_beta() -> None:
    features = Path("docs/FEATURES.md").read_text()
    section = _tested_by_users(features)
    listed = [line for line in section.splitlines() if line.startswith(("- ", "* ", "| "))]
    # Empty says so; one verified report would replace the line.
    assert ("None verified yet." in section) == (not listed)
    assert _reports_without_a_public_link(section) == []
    assert f"template={GENERIC_REPORT.name}" in section
    assert "## Generic heads (beta)" in features
    readme = Path("README.md").read_text()
    assert "## Generic heads (beta)" in readme
    assert f"template={GENERIC_REPORT.name}" in readme
    assert "docs/FEATURES.md#tested-by-users" in readme


def test_tested_by_users_check_reports_an_unverifiable_row() -> None:
    """Negative: a row with no public link, or a link elsewhere, is reported."""
    good = "- Acme X1 ([#7](https://github.com/dkpnw/ha-mxz-coordinator/issues/7))"
    assert _reports_without_a_public_link(f"Intro.\n\n{good}\n") == []
    for row in ("- Acme X1, works great", "- Acme X1 ([post](https://example.com/issues/7))"):
        assert _reports_without_a_public_link(f"{good}\n{row}\n") == [row]


def _release_step(releasing: str, number: int) -> str:
    """The text of one numbered step in RELEASING.md's release order."""
    match = re.search(
        rf"^{number}\. (.*?)(?=^\d+\. |^## )", releasing, flags=re.MULTILINE | re.DOTALL
    )
    assert match, f"no step {number}"
    return match.group(1)


def test_release_cadence_policy_is_stated_and_its_steps_line_up() -> None:
    releasing = Path("docs/RELEASING.md").read_text()
    cadence = releasing.split("## Release cadence", 1)[1].split("\n## ", 1)[0]
    assert "Batch releases" in cadence
    assert "not one release per merge" in cadence
    assert "GitHub pre-releases" in cadence
    assert "house hardware check on the Mitsubishi" in " ".join(cadence.split())
    assert "one-tap OK" in cadence
    assert "--prerelease` (step 8)" in cadence
    assert "(step 6)" in cadence
    assert "House hardware check and owner's OK" in _release_step(releasing, 6)
    assert "**Tag.**" in _release_step(releasing, 7)
    publish = _release_step(releasing, 8)
    assert "**Publish.**" in publish
    assert "--prerelease" in publish


def test_release_step_finder_rejects_a_missing_step() -> None:
    """Negative: a step that isn't there fails, and steps don't run together."""
    with pytest.raises(AssertionError):
        _release_step("1. One\n", 2)
    assert _release_step("1. One\n2. Two\n\n## Next\n", 1) == "One\n"
