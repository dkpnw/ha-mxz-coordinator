"""Report the monthly revert rate of landings on main, from git history.

A landing is a first-parent commit on the ref (default ``origin/main``, then
``main``), so a squash and a merge each count once. ``--all-parents`` counts
every commit on the ref instead.

A commit is a revert if its subject starts with the word ``Revert`` (git revert
and GitHub's default) or a line of its message starts with ``This reverts
commit <sha>``. Other uses of the word do not count. A merge landing is a
revert if it, or a commit it brought in, is one.

The rate for a month is reverts / landings, by committer date in
America/Los_Angeles. The current month is marked partial and a month with no
landings is left out. Only ``git rev-parse`` and ``git log`` are run. Exit 0
on a report, including a zero rate; 2 on a git error.

    python3 tools/revert_rate.py
    python3 tools/revert_rate.py --repo ../other-checkout --ref main --all-parents
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Los_Angeles")
DEFAULT_REFS = ("origin/main", "main")
REVERT_SUBJECT = re.compile(r"Revert\b")
REVERTS_LINE = re.compile(r"^\s*This reverts commit ([0-9a-f]{7,64})\b", re.MULTILINE)
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
LOG_FORMAT = "%H%x1f%P%x1f%cI%x1f%s%x1f%B"


class GitError(Exception):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    when: datetime
    subject: str
    message: str


@dataclass(frozen=True)
class Landing:
    commit: Commit
    reverts: tuple[Commit, ...]

    @property
    def is_revert(self) -> bool:
        return bool(self.reverts)

    @property
    def reverted(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(sha for commit in self.reverts for sha in reverted_shas(commit.message)))

    @property
    def month(self) -> str:
        return month_of(self.commit.when)


def month_of(when: datetime) -> str:
    return when.astimezone(ZONE).strftime("%Y-%m")


def reverted_shas(message: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(REVERTS_LINE.findall(message)))


def is_revert(commit: Commit) -> bool:
    return bool(REVERT_SUBJECT.match(commit.subject) or reverted_shas(commit.message))


def parse_log(raw: str) -> list[Commit]:
    """Commits from ``git log -z --format=LOG_FORMAT``; GitError on a record that does not fit."""
    commits = []
    for record in raw.split("\0"):
        if not record.strip():
            continue
        fields = record.lstrip("\n").split("\x1f", 4)
        if len(fields) != 5 or not SHA.fullmatch(fields[0]):
            raise GitError(f"unexpected git log output: {record[:80]!r}")
        sha, parents, when, subject, message = fields
        commits.append(Commit(sha, tuple(parents.split()), datetime.fromisoformat(when), subject, message))
    return commits


def landings(commits: Sequence[Commit], tip: str, first_parent: bool = True) -> list[Landing]:
    """Landings newest first. A first-parent landing carries the commits it brought in."""
    if not first_parent:
        return [Landing(commit, (commit,) if is_revert(commit) else ()) for commit in commits]
    by_sha = {commit.sha: commit for commit in commits}
    chain = []
    sha: str | None = tip
    while sha in by_sha:
        commit = by_sha[sha]
        chain.append(commit)
        sha = commit.parents[0] if commit.parents else None
    on_main: set[str] = set()
    result = []
    for commit in reversed(chain):
        carried = [commit, *_brought_in(commit, by_sha, on_main)]
        on_main.update(each.sha for each in carried)
        result.append(Landing(commit, tuple(each for each in carried if is_revert(each))))
    return result[::-1]


def _brought_in(merge: Commit, by_sha: dict[str, Commit], on_main: set[str]) -> list[Commit]:
    found: list[Commit] = []
    seen: set[str] = set()
    stack = list(merge.parents[1:])
    while stack:
        sha = stack.pop()
        if sha in seen or sha in on_main or sha not in by_sha:
            continue
        seen.add(sha)
        found.append(by_sha[sha])
        stack.extend(by_sha[sha].parents)
    return found


def _rate(reverts: int, landed: int) -> str:
    return f"{100 * reverts / landed:.2f}%" if landed else "n/a"


def render(landed: Sequence[Landing], ref: str, first_parent: bool, now: datetime) -> str:
    per_month = Counter(landing.month for landing in landed)
    reverts = [landing for landing in landed if landing.is_revert]
    reverts_per_month = Counter(landing.month for landing in reverts)
    current = month_of(now)
    counted = "first-parent landings" if first_parent else "every commit"
    lines = [
        f"Revert rate on {ref} ({counted}, months in America/Los_Angeles)",
        f"As of {now.astimezone(ZONE):%Y-%m-%d %H:%M %Z}",
        "",
        f"{'Month':<10} {'Landed':>7} {'Reverts':>8} {'Rate':>8}",
    ]
    for month in sorted(per_month):
        partial = "  (partial)" if month == current else ""
        lines.append(
            f"{month:<10} {per_month[month]:>7} {reverts_per_month[month]:>8} "
            f"{_rate(reverts_per_month[month], per_month[month]):>8}{partial}"
        )
    lines.append(f"{'All':<10} {len(landed):>7} {len(reverts):>8} {_rate(len(reverts), len(landed)):>8}")
    lines += ["", "Reverts"]
    for landing in reverts:
        when = landing.commit.when.astimezone(ZONE)
        targets = " ".join(landing.reverted) or "(no sha named)"
        lines.append(f"{landing.commit.sha[:10]}  {when:%Y-%m-%d %H:%M %Z}  {landing.commit.subject}  ->  {targets}")
    if not reverts:
        lines.append("(none)")
    return "\n".join(lines) + "\n"


# Read-only git ------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise GitError(f"cannot run git: {exc}") from exc
    if proc.returncode:
        raise GitError(proc.stderr.strip() or f"git {args[0]} exited {proc.returncode}")
    return proc.stdout


def resolve_ref(repo: Path, ref: str | None) -> tuple[str, str]:
    """The ref's name and commit sha; without ``ref``, the first of origin/main, main."""
    candidates = (ref,) if ref else DEFAULT_REFS
    error = GitError("no ref to try")
    for name in candidates:
        try:
            return name, _git(repo, "rev-parse", "--verify", "--end-of-options", f"{name}^{{commit}}").strip()
        except GitError as exc:
            error = exc
    raise GitError(f"{error} (tried {', '.join(candidates)})")


def read_log(repo: Path, tip: str) -> list[Commit]:
    return parse_log(_git(repo, "log", "-z", "--no-show-signature", f"--format={LOG_FORMAT}", tip))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("--repo", type=Path, default=Path("."), help="git checkout to read (default: here)")
    parser.add_argument("--ref", help="ref to walk (default: origin/main, then main)")
    parser.add_argument(
        "--all-parents", action="store_true", help="count every commit on the ref, not only first-parent landings"
    )
    args = parser.parse_args(argv)
    first_parent = not args.all_parents
    try:
        ref, tip = resolve_ref(args.repo, args.ref)
        commits = read_log(args.repo, tip)
        shallow = _git(args.repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    except GitError as exc:
        print(f"revert_rate: {exc}", file=sys.stderr)
        return 2
    if shallow:
        print("revert_rate: shallow clone; history before the clone depth is not counted", file=sys.stderr)
    sys.stdout.write(render(landings(commits, tip, first_parent), ref, first_parent, datetime.now(ZONE)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
