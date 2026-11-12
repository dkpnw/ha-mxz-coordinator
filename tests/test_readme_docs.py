"""Doc contracts for README and CHANGELOG lines that drift from their sources.

- Configuration's settings table names each room page field and section by the label
  the page shows (strings.json is the oracle).
- The feature guide states the Mushroom climate card's +/- wait as Mushroom v5.2.3 ships it:
  ``DEFAULT_DEBOUCE_TIME = 2000`` (milliseconds) in ``src/shared/input-number.ts``,
  overridable by a theme through ``--mush-input-number-debounce``
  (``src/utils/theme.ts``). The tests can't fetch Mushroom, so the value is pinned
  here with its source.
- The README has one What's new section, for the newest release.
- Until its version is released, every README mention of a setting a beta added says so.
- CHANGELOG 3.10.0 says CONTRIBUTING.md replaced "3.9.0's stance"; the 3.9.0 notes
  must state that stance.
- Every issue form has what GitHub requires, and the generic head report asks for what
  a report needs. Tested by users lists only reports with a public GitHub link.
- RELEASING.md states the release cadence, and its step references point at the right steps.
- Each screenshot the docs show is in the theme SETUP.md's note says (the pixels are the oracle).
- The Quick start installs from the Marketplace (Home Assistant 2026.11 or newer) and links to
  SETUP.md's HACS steps for older versions, which keep search, Download, restart and Add
  integration. The README's main badge opens the Marketplace.
"""

import json
import re
from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageStat

ROOM_STEP = json.loads(
    Path("custom_components/mxz_coordinator/strings.json").read_text()
)["options"]["step"]["room"]


def _rooms_rows(configuration: str) -> str:
    """The setting cells of the Rooms rows in Configuration's table: the **Rooms** row and those under it."""
    lines = configuration.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("| **Rooms**")]
    assert len(starts) == 1, starts
    cells = [lines[starts[0]].split("|")[2]]
    for line in lines[starts[0] + 1 :]:
        if not line.startswith("| |"):
            break
        cells.append(line.split("|")[2])
    return "\n".join(cells)


def _room_page_fields() -> list[str]:
    return list(ROOM_STEP["data"].values())


def _room_page_sections() -> list[str]:
    return [section["name"] for section in ROOM_STEP["sections"].values()]


def _missing_room_page_names(rows: str) -> list[str]:
    """Fields named in a cell, and sections as the cell's italic `*Section:*` prefix."""
    missing = [name for name in _room_page_fields() if name not in rows]
    return missing + [name for name in _room_page_sections() if f"*{name}:*" not in rows]


def test_room_page_names_come_from_the_page() -> None:
    """The oracle reads the real page: the hands-off switch and floor section are in it."""
    names = _room_page_fields() + _room_page_sections()
    assert "Let its own remote run it while disabled" in names
    assert "Freeze floor and overheat ceiling" in names
    assert len(names) == len(set(names)) >= 10


def test_configuration_rooms_rows_name_every_room_page_field_and_section() -> None:
    rows = _rooms_rows(Path("docs/CONFIGURATION.md").read_text())
    assert _missing_room_page_names(rows) == []


def test_rooms_rows_check_reports_a_dropped_name() -> None:
    """Negative: rows without the hands-off switch or the floor section are caught."""
    rows = _rooms_rows(Path("docs/CONFIGURATION.md").read_text())
    dropped = rows.replace("Let its own remote run it while disabled", "Hands-off")
    assert _missing_room_page_names(dropped) == ["Let its own remote run it while disabled"]
    dropped = rows.replace("*Freeze floor and overheat ceiling:*", "*Floors:*")
    assert _missing_room_page_names(dropped) == ["Freeze floor and overheat ceiling"]


def test_rooms_rows_stop_at_the_next_page() -> None:
    """Negative: a row of the next page doesn't count as a Rooms row."""
    table = "| **Rooms** | Head type | x |\n| | Allow Dry mode | x |\n| **Other** | Mine | x |\n"
    assert _rooms_rows(table).split("\n") == [" Head type ", " Allow Dry mode "]


# lovelace-mushroom v5.2.3: src/shared/input-number.ts:20 and src/utils/theme.ts:78.
MUSHROOM_DEFAULT_WAIT_MS = 2000
MUSHROOM_THEME_VARIABLE = "mush-input-number-debounce"


def _mushroom_wait_bullet(readme: str) -> str:
    section = readme.split("## Dashboard cards", 1)[1].split("\n## ", 1)[0]
    bullets = [b for b in section.split("\n- ") if "+ or -" in b]
    assert len(bullets) == 1, bullets
    return " ".join(bullets[0].split())


def test_mushroom_wait_matches_mushroom_source() -> None:
    bullet = _mushroom_wait_bullet(Path("docs/FEATURES.md").read_text())
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


def _whats_new_problems(readme: str, changelog: str) -> list[str]:
    """What is wrong with the README's What's new: it has one section, for the newest release.

    The section is a released version (a beta waits, docs/RELEASING.md), and no
    released x.y.0 is newer. A patch release may keep its x.y.0's section.
    """
    sections = re.findall(r"^## What's new in (.+?)\s*$", readme, re.MULTILINE)
    released = re.findall(r"^## (\d+\.\d+\.\d+) \(", changelog, re.MULTILINE)
    if len(sections) != 1:
        return [f"{len(sections)} What's new sections, not one"]
    if sections[0] not in released:
        return [f"What's new in {sections[0]}: not a released version"]
    newer = released[: released.index(sections[0])]
    return [f"{version} is newer" for version in newer if version.endswith(".0")]


def test_readme_has_one_whats_new_for_the_newest_release() -> None:
    assert _whats_new_problems(Path("README.md").read_text(), Path("CHANGELOG.md").read_text()) == []


@pytest.mark.parametrize(
    ("sections", "problems"),
    [
        (["2.1.0"], []),
        (["2.0.0"], ["2.1.0 is newer"]),
        (["2.1.0", "2.0.0"], ["2 What's new sections, not one"]),
        ([], ["0 What's new sections, not one"]),
        (["2.2.0"], ["What's new in 2.2.0: not a released version"]),
        (["2.2.0b1"], ["What's new in 2.2.0b1: not a released version"]),
    ],
)
def test_whats_new_check_reports_a_wrong_section(sections: list[str], problems: list[str]) -> None:
    """Negative included: an old section, two sections, none, or an unreleased one."""
    changelog = "## Unreleased\n\n## 2.1.0 (x)\n\n## 2.0.1 (x)\n\n## 2.0.0 (x)\n"
    readme = "\n".join(f"## What's new in {section}\n" for section in sections)
    assert _whats_new_problems(readme, changelog) == problems


def test_a_patch_release_may_keep_its_minor_s_section() -> None:
    changelog = "## 2.1.1 (x)\n\n## 2.1.0 (x)\n"
    assert _whats_new_problems("## What's new in 2.1.0\n", changelog) == []


# Settings a beta added: the version that ships them. The label is the Comfort page's own.
BETA_SETTINGS = {"accept_head_changes": "3.22.0"}
COMFORT_DATA = json.loads(
    Path("custom_components/mxz_coordinator/strings.json").read_text()
)["options"]["step"]["comfort"]["data"]


def _markdown_blocks(text: str) -> list[str]:
    """Paragraphs, list items and table rows, each joined onto one line."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip():
            blocks.append([])
        elif line.startswith(("- ", "* ", "| ")) or not blocks:
            blocks.append([line])
        else:
            blocks[-1].append(line)
    return [" ".join(" ".join(block).split()) for block in blocks if block]


def _unlabelled_beta_settings(readme: str, changelog: str) -> list[str]:
    """README blocks that name a setting from an unreleased version without saying it's beta."""
    released = re.findall(r"^## (\d+\.\d+\.\d+) \(", changelog, re.MULTILINE)
    problems = []
    for key, version in BETA_SETTINGS.items():
        if version in released:
            continue
        label = COMFORT_DATA[key]
        problems += [
            block for block in _markdown_blocks(readme)
            if label in block and f"{version} beta" not in block and f"{version} is in beta" not in block
        ]
    return problems


def test_readme_labels_every_mention_of_a_beta_setting() -> None:
    """Accept changes made at the head is not in 3.21.0 (STORY 1.4): each mention says 3.22.0 beta."""
    readme = Path("README.md").read_text()
    assert COMFORT_DATA["accept_head_changes"] in readme
    assert _unlabelled_beta_settings(readme, Path("CHANGELOG.md").read_text()) == []


def test_beta_label_check_reports_an_unlabelled_row_and_relaxes_on_release() -> None:
    """Negative: a table row naming the setting bare is reported; a labelled one, a bullet
    whose label is on its first line, or the same row once 3.22.0 is released, is not."""
    label = COMFORT_DATA["accept_head_changes"]
    bare = f"| **Comfort** | Resting mode, {label}, and more. |\n"
    labelled = f"| **Comfort** | Resting mode, {label} (3.22.0 beta), and more. |\n"
    bullet = f"- **Or use the head's own remote** (3.22.0 beta). To stop it,\n  turn off **{label}**.\n"
    unreleased = "## Unreleased\n\n## 3.21.0 (x)\n"
    assert _unlabelled_beta_settings(bare + "\n" + labelled, unreleased) == [" ".join(bare.split())]
    assert _unlabelled_beta_settings(bullet + labelled, unreleased) == []
    assert _unlabelled_beta_settings(bare, "## 3.22.0 (x)\n\n" + unreleased) == []


SHOWN_IMAGE = re.compile(r"images/(mxz-[\w.-]+\.png)")


def _shown_images() -> set[str]:
    """Every mxz-* screenshot the README and docs show."""
    pages = [Path("README.md"), *sorted(Path("docs").glob("*.md"))]
    return {name for page in pages for name in SHOWN_IMAGE.findall(page.read_text())}


def _stated_theme(name: str) -> str:
    """Setup's "About the screenshots": -dark/-light copies as named; mxz-3.12-* dark; retakes light."""
    if name.endswith(("-dark.png", "-light.png")):
        return name.rsplit("-", 1)[1].removesuffix(".png")
    return "dark" if name.startswith("mxz-3.12-") else "light"


def _wrong_themes(paths: list[Path]) -> list[str]:
    """Images whose pixels disagree with the theme their name states."""
    wrong = []
    for path in paths:
        with Image.open(path) as image:
            brightness = ImageStat.Stat(image.convert("L")).mean[0]
        if ("light" if brightness > 128 else "dark") != _stated_theme(path.name):
            wrong.append(path.name)
    return wrong


def test_setup_note_states_each_screenshot_s_theme() -> None:
    """The pixels are the oracle for SETUP.md's note on which screenshots are dark."""
    shown = _shown_images()
    assert {_stated_theme(name) for name in shown} == {"light", "dark"}
    assert any(name.startswith("mxz-3.12-") for name in shown)
    assert _wrong_themes([Path("images") / name for name in sorted(shown)]) == []


def test_theme_check_reports_a_screenshot_in_the_wrong_theme(tmp_path: Path) -> None:
    """Negative: a light 3.12 shot, a dark retake and a light -dark copy are reported."""
    white, black = (255, 255, 255), (0, 0, 0)
    shots = {
        "mxz-3.12-setup-heads.png": white, "mxz-setup-heads.png": black,
        "mxz-dashboard-dark.png": white, "mxz-3.12-reconfigure-heads.png": black,
        "mxz-configure-room.png": white, "mxz-dashboard-light.png": white,
    }
    for name, colour in shots.items():
        Image.new("RGB", (8, 8), colour).save(tmp_path / name)
    assert _wrong_themes([tmp_path / name for name in shots]) == [
        "mxz-3.12-setup-heads.png", "mxz-setup-heads.png", "mxz-dashboard-dark.png",
    ]


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


def test_changelog_notes_this_in_the_release_that_shipped_it() -> None:
    section = _changelog_section(Path("CHANGELOG.md").read_text(), "3.21.0")
    assert "Download diagnostics" in section
    assert "repair" in section


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
    assert "\n### Generic heads (beta)\n" in readme
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


# Home Assistant 2026.11 replaced HACS with the built-in Marketplace (release notes,
# Nov 4, 2026). The Quick start defaults to the Marketplace and links to the HACS steps,
# which SETUP.md keeps for older versions.
HACS_SECTION_HEADING = "### Before Home Assistant 2026.11 (HACS)"
HACS_SECTION_LINK = "docs/SETUP.md#before-home-assistant-202611-hacs"
MY_LINK = "https://my.home-assistant.io/redirect/{}/?owner=dkpnw&repository=ha-mxz-coordinator&category=integration"
MARKETPLACE_LINK = MY_LINK.format("marketplace_repository")
HACS_LINK = MY_LINK.format("hacs_repository")


def _quick_start(readme: str) -> str:
    return readme.split("\n## Quick start\n", 1)[1].split("\n## ", 1)[0]


def _hacs_section(setup: str) -> str:
    return re.split(r"^#{1,3} ", setup.split(f"\n{HACS_SECTION_HEADING}\n", 1)[1], maxsplit=1, flags=re.MULTILINE)[0]


def _numbered_steps(section: str) -> list[str]:
    steps = re.findall(r"^\d+\. (.*?)(?=^\d+\. |^\S|\Z)", section, flags=re.MULTILINE | re.DOTALL)
    return [" ".join(step.split()) for step in steps]


def _quick_start_problems(quick_start: str) -> list[str]:
    """What is wrong with the Quick start: it opens with the Marketplace line and the HACS link,
    installs from the Marketplace, and keeps MXZ's own setup steps."""
    opening = " ".join(quick_start.strip().split("\n\n", 1)[0].split())
    steps = _numbered_steps(quick_start)
    problems = []
    if "HACS became the Marketplace in Home Assistant 2026.11" not in opening:
        problems.append("no Marketplace line first")
    if f"]({HACS_SECTION_LINK})" not in opening:
        problems.append("no link to the HACS steps")
    if not steps or "**Settings → Marketplace**" not in steps[0]:
        problems.append("step 1 is not the Marketplace")
    problems += [f"step {n} names HACS" for n, step in enumerate(steps, 1) if "HACS" in step]
    problems += [
        f"no {name} step" for name in ("**Heads.**", "**Rooms and sensors.**", "**Save and finish**", "**Coordinator enable**")
        if not any(name in step for step in steps)
    ]
    return problems


def _hacs_section_problems(section: str) -> list[str]:
    """The HACS steps a Home Assistant older than 2026.11 still needs, missing from the section."""
    steps = " ".join(_numbered_steps(section))
    wanted = (
        "Open **HACS**, search for **MXZ Coordinator**, and select **Download**",
        f"]({HACS_LINK})",
        "Restart Home Assistant to load the integration",
        "**Settings → Devices & services → Add integration → MXZ Coordinator**",
        "Complete the setup below",
    )
    return [text for text in wanted if text not in steps]


def test_quick_start_installs_from_the_marketplace_and_links_the_hacs_steps() -> None:
    assert _quick_start_problems(_quick_start(Path("README.md").read_text())) == []


def test_setup_keeps_the_hacs_steps_for_older_home_assistant() -> None:
    setup = Path("docs/SETUP.md").read_text()
    assert _hacs_section_problems(_hacs_section(setup)) == []
    assert setup.index("\n## Install\n") < setup.index(HACS_SECTION_HEADING)


def test_quick_start_check_reports_a_hacs_default_or_a_dropped_link() -> None:
    """Negative: the base Quick start (HACS first), a dropped link and a dropped step are reported."""
    hacs_default = (
        "\nYou need Home Assistant.\n\n1. In HACS, search for **MXZ Coordinator** and select **Download**.\n"
        "2. **Heads.** a\n3. **Rooms and sensors.** b\n4. **Save and finish**\n5. **Coordinator enable**\n"
    )
    assert _quick_start_problems(hacs_default) == [
        "no Marketplace line first", "no link to the HACS steps", "step 1 is not the Marketplace", "step 1 names HACS",
    ]
    good = _quick_start(Path("README.md").read_text())
    assert _quick_start_problems(good.replace(HACS_SECTION_LINK, "docs/SETUP.md#install")) == ["no link to the HACS steps"]
    assert _quick_start_problems(good.replace("**Heads.**", "Heads.")) == ["no **Heads.** step"]


def test_hacs_section_check_reports_a_dropped_step() -> None:
    """Negative: losing Download, the restart or the hacs_repository link is reported, and the
    section stops at the next heading."""
    section = _hacs_section(Path("docs/SETUP.md").read_text())
    assert _hacs_section_problems(section.replace("**Download**", "**Install**")) == [
        "Open **HACS**, search for **MXZ Coordinator**, and select **Download**",
    ]
    assert _hacs_section_problems(section.replace(HACS_LINK, MARKETPLACE_LINK)) == [f"]({HACS_LINK})"]
    assert _hacs_section_problems(section.replace("2. Restart", "2. Reload")) == [
        "Restart Home Assistant to load the integration",
    ]
    setup = f"## Install\n\n{HACS_SECTION_HEADING}\n\n1. Open **HACS**\n\n## Set up\n\n1. Restart Home Assistant to load the integration\n"
    assert "Restart Home Assistant to load the integration" in _hacs_section_problems(_hacs_section(setup))


def _main_badge(readme: str) -> str:
    """The first My Home Assistant link in the README: the one-click badge."""
    return re.search(r'<a href="(https://my\.home-assistant\.io/redirect/[^"]+)"', readme)[1]


def test_main_badge_opens_the_marketplace() -> None:
    readme = Path("README.md").read_text()
    assert _main_badge(readme) == MARKETPLACE_LINK
    assert '<img src="https://my.home-assistant.io/badges/marketplace_repository.svg"' in readme


def test_main_badge_check_reports_the_hacs_badge() -> None:
    """Negative: the base README's HACS badge is not the Marketplace one."""
    assert _main_badge(f'<p><a href="{HACS_LINK}"><img></a> <a href="{MARKETPLACE_LINK}"></a></p>') == HACS_LINK
