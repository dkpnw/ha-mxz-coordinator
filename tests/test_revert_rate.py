"""The revert-rate report: what counts as a revert, what counts as a landing, which month.

Pure rules are checked on invented commits. The command is run on throwaway repositories
built in tmp_path with fixed dates and no user or system git config.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from tools import revert_rate
from tools.revert_rate import Commit, is_revert, landings, render, reverted_shas

FULL = "0123456789abcdef0123456789abcdef01234567"
NOON = datetime.fromisoformat("2026-10-15T19:00:00+00:00")


def commit(sha: str, *parents: str, message: str = "chore: x\n", when: datetime = NOON) -> Commit:
    return Commit(sha, parents, when, message.split("\n", 1)[0], message)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ('Revert "fix: keep the fan hold (#12)"', True),
        ('Revert "Revert "fix: x""', True),
        ("Revert: fan hold", True),
        ("Reverted the fan default", False),
        ("Reverts #12", False),
        ("revert: fan hold", False),
        ("fix: revert the fan hold", False),
        ("docs: Revert button", False),
    ],
)
def test_subject_rule(subject: str, expected: bool) -> None:
    assert is_revert(commit("a", message=f"{subject}\n")) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (f'Revert "x"\n\nThis reverts commit {FULL}.\n', (FULL,)),
        ("Revert \"Merge x\"\n\nThis reverts commit aaaaaaa, reversing\nchanges made to bbbbbbb.\n", ("aaaaaaa",)),
        ("fix: x (#9)\n\n* This reverts commit 1111111.\n\nThis reverts commit 2222222.\nThis reverts commit 1111111.\n", ("2222222", "1111111")),
        ("fix: x\n\nThe old code would revert the fan; this reverts commit abcdef1 in spirit only.\n", ()),
        ("fix: x\n\nWe revert to auto when the hold ends.\n", ()),
        ("fix: x\n\nThis reverts commit abc12.\n", ()),
    ],
)
def test_trailer_rule(message: str, expected: tuple[str, ...]) -> None:
    assert reverted_shas(message) == expected
    assert is_revert(commit("a", message=message)) is bool(expected)


@pytest.mark.parametrize(
    "raw",
    [
        f"No signature\n{FULL}\x1f\x1f2026-10-15T12:00:00-07:00\x1ffeat: x\x1ffeat: x\n\0",
        f"{FULL}\x1f2026-10-15T12:00:00-07:00\x1ffeat: x\0",
    ],
)
def test_a_record_that_does_not_parse_is_an_error_not_an_empty_report(raw: str) -> None:
    with pytest.raises(revert_rate.GitError, match="unexpected git log output"):
        revert_rate.parse_log(raw)


def test_incidental_revert_in_body_is_not_a_revert() -> None:
    message = "fix: fan hold\n\nRevert to the person's speed after a restart.\nthis reverts commit abcdef1\n"
    assert not is_revert(commit("a", message=message))


def test_merge_counts_once_and_carries_the_revert_it_brought_in() -> None:
    root = commit("r")
    landed = commit("a", "r")
    reverting = commit("f1", "a", message="Revert \"feat: a\"\n\nThis reverts commit aaaaaaa.\n")
    branch_tip = commit("f2", "f1")
    merge = commit("m", "a", "f2", message="Merge pull request #5 from o/revert-4\n\nRevert feat\n")
    squash = commit("s", "m")
    log = [squash, merge, branch_tip, reverting, landed, root]

    first_parent = landings(log, "s")
    assert [landing.commit.sha for landing in first_parent] == ["s", "m", "a", "r"]
    assert [landing.commit.sha for landing in first_parent if landing.is_revert] == ["m"]
    assert first_parent[1].reverted == ("aaaaaaa",)

    every = landings(log, "s", first_parent=False)
    assert [landing.commit.sha for landing in every] == ["s", "m", "f2", "f1", "a", "r"]
    assert [landing.commit.sha for landing in every if landing.is_revert] == ["f1"]


def test_merge_does_not_recount_a_revert_already_on_main() -> None:
    root = commit("r")
    landed_revert = commit("a", "r", message="Revert \"feat: r\"\n\nThis reverts commit 1234567.\n")
    branch = commit("f1", "r")
    back_merge = commit("f2", "f1", "a", message="Merge branch 'main' into feature\n")
    merge = commit("m", "a", "f2", message="Merge pull request #6 from o/feature\n")
    log = [merge, back_merge, branch, landed_revert, root]

    result = landings(log, "m")
    assert [(landing.commit.sha, landing.is_revert) for landing in result] == [
        ("m", False),
        ("a", True),
        ("r", False),
    ]


def rows(report: str) -> dict[str, list[str]]:
    table = report.split("\n\n")[1].splitlines()[1:]
    return {line.split()[0]: line.split()[1:] for line in table}


def test_months_are_los_angeles_and_only_the_current_one_is_partial() -> None:
    last_of_october = commit("a", when=datetime.fromisoformat("2026-11-01T06:00:00+00:00"))
    first_of_november = commit(
        "b", "a", message="Revert \"x\"\n", when=datetime.fromisoformat("2026-11-01T08:00:00+00:00")
    )
    log = [first_of_november, last_of_october]
    now = datetime.fromisoformat("2026-11-15T12:00:00-08:00")

    report = render(landings(log, "b"), "main", True, now)
    assert rows(report) == {
        "2026-10": ["1", "0", "0.00%"],
        "2026-11": ["1", "1", "100.00%", "(partial)"],
        "All": ["2", "1", "50.00%"],
    }

    a_month_later = datetime.fromisoformat("2026-12-01T12:00:00-08:00")
    assert "(partial)" not in render(landings(log, "b"), "main", True, a_month_later)


# Throwaway repositories ----------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CEILING_DIRECTORIES"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "repo"
    path.mkdir()
    git(path, None, "init", "-q", "-b", "main")
    return path


def git(path: Path, when: str | None, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="Invented", GIT_AUTHOR_EMAIL="invented@example.com",
               GIT_COMMITTER_NAME="Invented", GIT_COMMITTER_EMAIL="invented@example.com")
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    return subprocess.run(
        ["git", "-C", str(path), *args], env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def land(path: Path, when: str, message: str) -> str:
    (path / f"{when[:10]}.txt").write_text(message)
    git(path, None, "add", "-A")
    git(path, when, "commit", "-q", "-m", message)
    return git(path, None, "rev-parse", "HEAD")


def snapshot(path: Path) -> dict[str, str]:
    return {
        str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted((path / ".git").rglob("*"))
        if item.is_file()
    }


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = revert_rate.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_report_on_a_repository(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    land(repo, "2025-03-01T07:30:00+00:00", "feat: one")
    fix = land(repo, "2025-03-12T18:00:00+00:00", "fix: two")
    git(repo, "2025-03-13T18:00:00+00:00", "revert", "--no-edit", fix)
    revert_on_main = git(repo, None, "rev-parse", "HEAD")

    git(repo, None, "checkout", "-q", "-b", "feature")
    feature = land(repo, "2025-04-02T18:00:00+00:00", "feat: three")
    git(repo, None, "checkout", "-q", "main")
    git(repo, "2025-04-03T18:00:00+00:00", "merge", "-q", "--no-ff", "-m", "Merge pull request #4 from o/feature", "feature")

    git(repo, None, "checkout", "-q", "-b", "revert-4")
    git(repo, "2025-04-04T18:00:00+00:00", "revert", "--no-edit", feature)
    git(repo, None, "checkout", "-q", "main")
    git(repo, "2025-04-05T18:00:00+00:00", "merge", "-q", "--no-ff", "-m", "Merge pull request #5 from o/revert-4", "revert-4")
    revert_merge = git(repo, None, "rev-parse", "HEAD")
    before = snapshot(repo)

    code, out, err = run(capsys, "--repo", str(repo))
    assert (code, err) == (0, "")
    assert out.startswith("Revert rate on main (first-parent landings")
    assert rows(out) == {
        "2025-02": ["1", "0", "0.00%"],
        "2025-03": ["2", "1", "50.00%"],
        "2025-04": ["2", "1", "50.00%"],
        "All": ["5", "2", "40.00%"],
    }
    listed = out.split("\n\n")[2].splitlines()[1:]
    assert [line.split()[0] for line in listed] == [revert_merge[:10], revert_on_main[:10]]
    assert [line.rsplit("->", 1)[1].split() for line in listed] == [[feature], [fix]]

    code, out, _ = run(capsys, "--repo", str(repo), "--all-parents")
    assert code == 0
    assert out.startswith("Revert rate on main (every commit")
    assert rows(out) == {
        "2025-02": ["1", "0", "0.00%"],
        "2025-03": ["2", "1", "50.00%"],
        "2025-04": ["4", "1", "25.00%"],
        "All": ["7", "2", "28.57%"],
    }
    assert snapshot(repo) == before


def test_zero_rate_exits_zero_and_origin_main_comes_first(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = land(repo, "2025-05-10T18:00:00+00:00", "feat: one")
    land(repo, "2025-06-10T18:00:00+00:00", "feat: two")

    code, out, _ = run(capsys, "--repo", str(repo))
    assert code == 0
    assert out.startswith("Revert rate on main ")
    assert rows(out)["All"] == ["2", "0", "0.00%"]
    assert out.rstrip().endswith("Reverts\n(none)")

    git(repo, None, "update-ref", "refs/remotes/origin/main", first)
    code, out, _ = run(capsys, "--repo", str(repo))
    assert code == 0
    assert out.startswith("Revert rate on origin/main ")
    assert rows(out) == {"2025-05": ["1", "0", "0.00%"], "All": ["1", "0", "0.00%"]}

    code, out, _ = run(capsys, "--repo", str(repo), "--ref", "main")
    assert rows(out)["All"] == ["2", "0", "0.00%"]


def test_a_show_signature_setting_does_not_reach_the_parse(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "signing-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    land(repo, "2025-05-10T18:00:00+00:00", "feat: one")
    git(repo, None, "config", "gpg.format", "ssh")
    git(repo, None, "config", "user.signingkey", str(key))
    git(repo, None, "config", "commit.gpgsign", "true")
    land(repo, "2025-05-11T18:00:00+00:00", 'Revert "feat: one"')
    git(repo, None, "config", "log.showSignature", "true")
    assert "No signature" in git(repo, None, "log", "-1", "--format=%H")

    code, out, _ = run(capsys, "--repo", str(repo))
    assert code == 0
    assert rows(out) == {"2025-05": ["2", "1", "50.00%"], "All": ["2", "1", "50.00%"]}


def test_a_shallow_clone_says_so(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    land(repo, "2025-05-10T18:00:00+00:00", "feat: one")
    land(repo, "2025-06-10T18:00:00+00:00", "feat: two")
    code, _, err = run(capsys, "--repo", str(repo))
    assert (code, err) == (0, "")

    clone = tmp_path / "clone"
    git(tmp_path, None, "clone", "-q", "--depth", "1", f"file://{repo}", str(clone))
    code, out, err = run(capsys, "--repo", str(clone))
    assert code == 0
    assert rows(out)["All"] == ["1", "0", "0.00%"]
    assert err == "revert_rate: shallow clone; history before the clone depth is not counted\n"


@pytest.mark.parametrize("ref", ["no-such-branch", "--output=written.txt"])
def test_a_bad_ref_exits_two_and_writes_nothing(
    repo: Path, capsys: pytest.CaptureFixture[str], ref: str
) -> None:
    land(repo, "2025-05-10T18:00:00+00:00", "feat: one")
    before = snapshot(repo)

    code, out, err = run(capsys, "--repo", str(repo), f"--ref={ref}")
    assert (code, out) == (2, "")
    assert err.startswith("revert_rate: ")
    assert ref in err
    assert snapshot(repo) == before
    assert sorted(item.name for item in repo.iterdir()) == [".git", "2025-05-10.txt"]


def test_not_a_checkout_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    code, out, err = run(capsys, "--repo", str(tmp_path))
    assert (code, out) == (2, "")
    assert "not a git repository" in err
