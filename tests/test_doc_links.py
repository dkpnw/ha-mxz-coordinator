"""Every relative link and anchor in the repository's Markdown resolves.

A link to a heading that was renamed, or to a file that moved, fails silently on
GitHub. It shipped twice: README linked `#whats-new-in-3111`, which no heading
made (fixed in 29509ed), and docs/FEATURES.md linked `#cloud-heads-beta` from
#104 until this check found it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
HTML_ANCHOR = re.compile(r"<a\s+(?:id|name)=\"([^\"]+)\"")
FENCE = re.compile(r"^\s*(```|~~~)")


def _prose(text: str) -> list[tuple[int, str]]:
    """Numbered lines outside fenced code blocks."""
    lines, fenced = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            lines.append((number, line))
    return lines


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def anchors(text: str) -> set[str]:
    found: set[str] = set()
    seen: dict[str, int] = {}
    for _, line in _prose(text):
        found.update(HTML_ANCHOR.findall(line))
        if match := HEADING.match(line):
            base = slug(match[1])
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    return found


def broken_links(source: Path, text: str) -> list[str]:
    broken = []
    own = anchors(text)
    for number, line in _prose(text):
        for target in LINK.findall(re.sub(r"`[^`]*`", "", line)):
            if re.match(r"[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                continue
            path, _, anchor = target.partition("#")
            resolved = (source.parent / path).resolve() if path else source
            where = f"{source.relative_to(ROOT)}:{number}: {target}"
            if not resolved.exists():
                broken.append(f"{where} (no such file)")
            elif anchor and resolved.suffix == ".md" and anchor not in (
                own if resolved == source else anchors(resolved.read_text())
            ):
                broken.append(f"{where} (no such heading)")
    return broken


def tracked_markdown() -> list[Path]:
    names = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split()
    return [ROOT / name for name in names]


def test_every_relative_link_and_anchor_resolves() -> None:
    broken = [problem for path in tracked_markdown() for problem in broken_links(path, path.read_text())]
    assert broken == []


@pytest.mark.parametrize(("text", "problem"), [
    ("See [3.11.1](#whats-new-in-3111).\n\n## What's new in 3.11.0\n", "#whats-new-in-3111 (no such heading)"),
    ("[Cloud heads](HOW-IT-WORKS.md#cloud-heads-beta)\n", "HOW-IT-WORKS.md#cloud-heads-beta (no such heading)"),
    ("[gone](NO-SUCH-FILE.md)\n", "NO-SUCH-FILE.md (no such file)"),
])
def test_a_dangling_link_is_found(text: str, problem: str) -> None:
    found = broken_links(ROOT / "docs" / "FEATURES.md", text)
    assert len(found) == 1
    assert found[0].endswith(problem)


def test_headings_html_anchors_and_repeats_resolve() -> None:
    text = "## What's new in 3.22.0\n<a id=\"old-name\"></a>\n## Setup\n## Setup\n```sh\n# not a heading\n```\n"
    assert anchors(text) == {"whats-new-in-3220", "old-name", "setup", "setup-1"}
    assert broken_links(ROOT / "README.md", f"[a](#setup-1) [b](#old-name)\n{text}") == []
