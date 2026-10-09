"""Manual pre-tag check: every outside contributor since the last tag is in README Credits.

Run from the checkout with an authenticated `gh`: `python3 tools/credits_check.py`.
Not run in CI (CI has no gh token for issues). Exit 0 all credited, 1 missing, 2 on
git/gh/README error.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "dkpnw/ha-mxz-coordinator"
OWNER = "dkpnw"
SECTION = "## Credits & prior art"
SKIP_EXACT = {OWNER, "codex", "m150", "github-actions", "dependabot"}

NOREPLY = re.compile(r"^(?:\d+\+)?([A-Za-z0-9][A-Za-z0-9-]*)@users\.noreply\.github\.com$", re.IGNORECASE)
PROFILE_LINK = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/?(?=[)\s>\]\"']|$)")
MENTION = re.compile(r"(?<![\w.@/])@([A-Za-z0-9][A-Za-z0-9-]*)")
TRAILER = re.compile(r"^co-authored-by:\s*(.*?)\s*<([^>]*)>\s*$", re.IGNORECASE | re.MULTILINE)


# Pure parsing ---------------------------------------------------------------

def credits_section(readme):
    """Text of the Credits section, up to the next level-2 heading."""
    lines = readme.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == SECTION)
    except StopIteration:
        raise ValueError(f"README has no {SECTION!r} heading") from None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1:end])


def credited_handles(section):
    """Lower-cased handles from github.com/<handle> profile links and @handle mentions."""
    return {h.lower() for h in PROFILE_LINK.findall(section) + MENTION.findall(section)}


def noreply_login(email):
    match = NOREPLY.match((email or "").strip())
    return match.group(1) if match else None


def coauthors(message):
    """(name, email) for every Co-authored-by trailer, key case-insensitive."""
    return TRAILER.findall(message)


def is_skipped(*identities):
    """Owner, agents and bots: Codex, M150, Claude/Anthropic, Actions, Dependabot, [bot]."""
    for identity in identities:
        value = (identity or "").strip().lower()
        if not value:
            continue
        value = value.removeprefix("app/")
        if (value in SKIP_EXACT or value.endswith("[bot]") or value.startswith("claude")
                or "anthropic" in value):
            return True
    return False


def parse_log(raw):
    """`git log --format=%H%x1f%an%x1f%ae%x1f%B%x1e` into (sha, name, email, message)."""
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if record:
            sha, name, email, message = record.split("\x1f", 3)
            commits.append((sha, name, email, message))
    return commits


def closed_since(items, since):
    """Issues/PRs closed at or after `since` (aware datetime), by outside authors."""
    found = []
    for item in items:
        author = item.get("author") or {}
        login = author.get("login") or "(no author)"  # Deleted account: shown, never dropped.
        closed = item.get("closedAt")
        if not closed or author.get("is_bot") or is_skipped(login):
            continue
        if datetime.fromisoformat(closed.replace("Z", "+00:00")) >= since:
            found.append((login, f"#{item['number']}"))
    return found


def missing(candidates, credited):
    """{handle: [sources]} for candidates whose handle is not credited."""
    report = {}
    for handle, source in candidates:
        if handle.lower() not in credited:
            report.setdefault(handle, []).append(source)
    return report


def report_lines(candidates, credited):
    grouped = {}
    for handle, source in candidates:
        grouped.setdefault(handle, []).append(source)
    return [f"{'credited' if h.lower() in credited else 'MISSING '}  {h}  ({', '.join(s)})"
            for h, s in sorted(grouped.items(), key=lambda kv: kv[0].lower())]


# I/O ------------------------------------------------------------------------

def run(*args):
    return subprocess.run(args, cwd=ROOT, check=True, capture_output=True, text=True).stdout


def commit_login(repo, sha, name, email):
    login = noreply_login(email)
    if login or is_skipped(name, email):
        return login or name
    login = run("gh", "api", f"repos/{repo}/commits/{sha}", "-q", ".author.login").strip()
    return None if login in ("", "null") else login


def collect(repo, tag):
    candidates = []
    log = run("git", "log", f"{tag}..HEAD", "--format=%H%x1f%an%x1f%ae%x1f%B%x1e")
    for sha, name, email, message in parse_log(log):
        login = commit_login(repo, sha, name, email)
        if not is_skipped(login, name, email):
            candidates.append((login or f"{name} <{email}>", f"commit {sha[:7]}"))
        for co_name, co_email in coauthors(message):
            handle = noreply_login(co_email) or co_name
            if not is_skipped(handle, co_name, co_email):
                candidates.append((handle, f"co-author {sha[:7]}"))
    since = datetime.fromisoformat(run("git", "log", "-1", "--format=%cI", f"{tag}^{{commit}}").strip())
    for kind in ("issue", "pr"):
        items = json.loads(run("gh", kind, "list", "--repo", repo, "--state", "closed",
                               "--limit", "1000", "--json", "number,author,closedAt"))
        candidates += closed_since(items, since)
    return candidates


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="tag to compare from (default: latest tag)")
    parser.add_argument("--repo", default=REPO)
    args = parser.parse_args(argv)
    try:
        tag = args.since or run("git", "describe", "--tags", "--abbrev=0").strip()
        credited = credited_handles(credits_section((ROOT / "README.md").read_text()))
        candidates = collect(args.repo, tag)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as err:
        detail = getattr(err, "stderr", None) or err
        print(f"credits_check: error: {detail}".rstrip(), file=sys.stderr)
        return 2
    print(f"Contributors since {tag} (commits {tag}..HEAD, issues/PRs closed since the tag):")
    print("\n".join(report_lines(candidates, credited)) or "  none outside the owner and agents")
    absent = missing(candidates, credited)
    if absent:
        print(f"{len(absent)} missing from README '{SECTION}': {', '.join(sorted(absent))}")
        return 1
    print("All credited.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
