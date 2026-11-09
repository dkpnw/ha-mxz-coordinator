"""The pre-tag credits check, with invented handles only. Parsing is pure; the
collection tests answer every git and gh call from invented data."""

import json
from datetime import UTC, datetime

import pytest

from tools import credits_check
from tools.credits_check import (
    closed_since,
    coauthor_login,
    coauthors,
    credited_handles,
    credits_section,
    is_skipped,
    missing,
    noreply_login,
    parse_log,
    report_lines,
)

README = """# Title

See [@notcredited](https://github.com/notcredited) in the intro.

## Credits & prior art

- [@alice-a](https://github.com/alice-a) found a bug ([#1](https://github.com/o/r/issues/1)).
- Thanks to @Bob99 for testing, and https://github.com/carol/ too.
- Mail dave@example.com, not a mention; [repo](https://github.com/erin/some-repo).

### Sub heading stays inside

- @frank

## License

[@after](https://github.com/after)
"""


def test_section_runs_to_next_level_two_heading():
    section = credits_section(README)
    assert "@alice-a" in section and "@frank" in section
    assert "notcredited" not in section and "after" not in section


def test_section_at_end_of_file_and_missing_heading():
    assert credits_section("## Credits & prior art\n- @zed\n") == "- @zed"
    with pytest.raises(ValueError):
        credits_section("## Credits\n- @zed\n")


def test_handles_from_profile_links_and_mentions():
    handles = credited_handles(credits_section(README))
    assert handles == {"alice-a", "bob99", "carol", "frank"}
    # Emails are not mentions; repo and issue links are not profiles.
    assert not {"example", "erin", "o", "dave"} & handles


@pytest.mark.parametrize(("email", "login"), [
    ("123+Some-User@users.noreply.github.com", "Some-User"),
    ("some-user@users.noreply.github.com", "some-user"),
    (" 9+x@USERS.NOREPLY.GITHUB.COM ", "x"),
    ("some-user@example.com", None),
    ("noreply@anthropic.com", None),
    ("123+a@users.noreply.github.com.evil", None),
    ("", None),
    (None, None),
])
def test_noreply_email_to_login(email, login):
    assert noreply_login(email) == login


def test_coauthor_trailers_any_case():
    message = ("Fix it\n\nBody mentions co-authored-by: inline <x> text.\n\n"
               "Co-authored-by: Ann <1+ann@users.noreply.github.com>\n"
               "CO-AUTHORED-BY: Claude Opus <noreply@anthropic.com>\n"
               "co-authored-by:  Ben Two  <ben@example.com>\n")
    assert coauthors(message) == [
        ("Ann", "1+ann@users.noreply.github.com"),
        ("Claude Opus", "noreply@anthropic.com"),
        ("Ben Two", "ben@example.com"),
    ]
    assert coauthors("No trailers here\n") == []


@pytest.mark.parametrize("identity", [
    "dkpnw", "DKPNW", "Codex", "M150", "Claude", "claude-bot", "Claude Opus 5.5",
    "noreply@anthropic.com", "AnthropicBot", "github-actions", "dependabot",
    "app/dependabot", "app/github-actions", "renovate[bot]", "Some[BOT]",
])
def test_owner_agents_and_bots_skipped(identity):
    assert is_skipped(identity)


@pytest.mark.parametrize("identity", [
    "alice", "dkpnw2", "codex-fan", "m1500", "my-claude", "actions", "botany", "", None,
])
def test_outside_people_not_skipped(identity):
    assert not is_skipped(identity)


def test_any_identity_skips():
    assert is_skipped(None, "Claude", "x@example.com")
    assert not is_skipped(None, "Alice", "alice@example.com")


def test_parse_log_records():
    raw = ("a" * 40 + "\x1fAnn\x1fann@example.com\x1fSubject\n\nBody\n\x1e\n"
           + "b" * 40 + "\x1fBen\x1fben@example.com\x1fOnly\x1f subject\n\x1e\n")
    assert parse_log(raw) == [
        ("a" * 40, "Ann", "ann@example.com", "Subject\n\nBody"),
        ("b" * 40, "Ben", "ben@example.com", "Only\x1f subject"),
    ]
    assert parse_log("") == []


def test_closed_since_filters_date_owner_and_bots():
    since = datetime(2026, 1, 1, 8, tzinfo=UTC)
    items = [
        {"number": 1, "author": {"login": "alice"}, "closedAt": "2026-01-01T08:00:00Z"},
        {"number": 2, "author": {"login": "bob"}, "closedAt": "2026-01-01T07:59:59Z"},
        {"number": 3, "author": {"login": "dkpnw"}, "closedAt": "2026-02-01T00:00:00Z"},
        {"number": 4, "author": {"login": "app/dependabot", "is_bot": True},
         "closedAt": "2026-02-01T00:00:00Z"},
        {"number": 5, "author": {"login": "helper", "is_bot": True},
         "closedAt": "2026-02-01T00:00:00Z"},
        {"number": 6, "author": None, "closedAt": "2026-02-01T00:00:00Z"},
        {"number": 7, "author": {"login": "carol"}, "closedAt": None},
        {"number": 8, "author": {"login": "dan"}, "closedAt": "2026-01-01T00:30:00-08:00"},
    ]
    assert closed_since(items, since) == [("alice", "#1"), ("(no author)", "#6"), ("dan", "#8")]


def test_missing_is_case_insensitive_and_groups_sources():
    candidates = [("Alice", "commit abc1234"), ("bob", "#3"), ("bob", "co-author def5678")]
    assert missing(candidates, {"alice"}) == {"bob": ["#3", "co-author def5678"]}
    assert missing(candidates, {"alice", "bob"}) == {}
    assert missing([], set()) == {}


def test_report_marks_each_handle():
    lines = report_lines([("bob", "#3"), ("Alice", "commit abc1234"), ("bob", "#4")], {"alice"})
    assert lines == ["credited  Alice  (commit abc1234)", "MISSING   bob  (#3, #4)"]


# Co-author resolution --------------------------------------------------------

@pytest.mark.parametrize(("email", "login"), [
    ("7+bo-gh@users.noreply.github.com", "bo-gh"),  # noreply form
    (" Cy@Example.ORG ", "cy-gh"),  # checked-in map, any case
    ("ann@example.com", "ann-x"),  # a commit in the range by that email
    ("dee@example.net", None),  # nobody knows it
    ("", None),
    (None, None),
])
def test_coauthor_email_to_login(email, login):
    known = {"cy@example.org": "cy-gh"}
    assert coauthor_login(email, {"ann@example.com": "ann-x"}, known) == login


def test_noreply_and_map_come_before_the_range():
    known = {"cy@example.org": "cy-gh"}
    ranged = {"cy@example.org": "other", "7+bo-gh@users.noreply.github.com": "other"}
    assert coauthor_login("cy@example.org", ranged, known) == "cy-gh"
    assert coauthor_login("7+bo-gh@users.noreply.github.com", ranged, known) == "bo-gh"


def test_checked_in_map_is_lower_case_email_to_login():
    for email, login in credits_check.KNOWN_LOGINS.items():
        assert email == email.strip().lower() and "@" in email
        assert credits_check.noreply_login(email) is None
        assert credits_check.PROFILE_LINK.fullmatch(f"github.com/{login}")


SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40
TAG_DATE = "2026-01-01T00:00:00+00:00"


def _record(sha, name, email, message):
    return f"{sha}\x1f{name}\x1f{email}\x1f{message}\n\x1e\n"


def fake_git_and_gh(monkeypatch, log, logins, items=()):
    """Answer the check's git and gh calls; ``logins`` is GitHub's author login
    per commit SHA (None: an email linked to no account). Returns the calls."""
    calls = []

    def run(*args):
        calls.append(args)
        if args[:3] == ("git", "log", "v9.0.0..HEAD"):
            return log
        if args[:3] == ("git", "log", "-1"):
            return TAG_DATE + "\n"
        if args[:2] == ("gh", "api"):
            login = logins[args[2].rsplit("/", 1)[1]]
            return ("null" if login is None else login) + "\n"
        if args[:2] in (("gh", "issue"), ("gh", "pr")):
            return json.dumps(list(items))
        raise AssertionError(f"unexpected call {args}")

    monkeypatch.setattr(credits_check, "run", run)
    return calls


TRAILERS = (
    "Co-authored-by: Bo <7+bo-gh@users.noreply.github.com>\n"
    "Co-authored-by: Cy <cy@example.org>\n"
    "Co-authored-by: Ann Example <ann@example.com>\n"
    "Co-authored-by: Dee Unknown <dee@example.net>\n"
    "Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>\n"
    "Co-authored-by: dkpnw <1+dkpnw@users.noreply.github.com>\n"
    "co-authored-by: helper[bot] <helper@example.com>\n"
)


def test_collect_resolves_each_coauthor_and_keeps_the_unknown_one(monkeypatch):
    """Noreply, map and range each resolve a co-author to a login; an email
    nobody knows stays a name and email; owner, agent and bot trailers are
    skipped as before. The range is read before any trailer, so a co-author
    in an earlier commit is resolved by a later commit of their own."""
    monkeypatch.setattr(credits_check, "KNOWN_LOGINS", {"cy@example.org": "cy-gh"})
    log = (_record(SHA_A, "dkpnw", "1+dkpnw@users.noreply.github.com", "Owner work\n\n" + TRAILERS)
           + _record(SHA_B, "Ann Example", "Ann@Example.com", "Ann's fix")
           + _record(SHA_C, "dkpnw", "owner@example.com", "Owner, own email"))
    calls = fake_git_and_gh(monkeypatch, log, {SHA_B: "ann-x"})
    candidates = credits_check.collect("o/r", "v9.0.0")
    assert sorted(candidates) == sorted([
        ("ann-x", "commit bbbbbbb"),
        ("bo-gh", "co-author aaaaaaa"),
        ("cy-gh", "co-author aaaaaaa"),
        ("ann-x", "co-author aaaaaaa"),
        ("Dee Unknown <dee@example.net>", "co-author aaaaaaa"),
    ])
    # One API lookup, for the one outside author without a noreply email.
    assert [c for c in calls if c[:2] == ("gh", "api")] == [
        ("gh", "api", f"repos/o/r/commits/{SHA_B}", "-q", ".author.login")]
    assert credits_check.missing(candidates, {"ann-x", "bo-gh", "cy-gh"}) == {
        "Dee Unknown <dee@example.net>": ["co-author aaaaaaa"]}


def test_an_unlinked_commit_email_resolves_no_coauthor(monkeypatch):
    """GitHub links the commit to no account: the commit and the trailer with
    its email are both reported by name and email, as missing."""
    log = (_record(SHA_A, "Eve", "eve@example.com", "Eve's fix")
           + _record(SHA_B, "dkpnw", "1+dkpnw@users.noreply.github.com",
                     "Pair\n\nCo-authored-by: Eve <eve@example.com>\n"))
    fake_git_and_gh(monkeypatch, log, {SHA_A: None})
    candidates = credits_check.collect("o/r", "v9.0.0")
    assert credits_check.missing(candidates, {"eve"}) == {
        "Eve <eve@example.com>": ["commit aaaaaaa", "co-author bbbbbbb"]}


def test_a_coauthor_name_is_not_taken_for_a_login(monkeypatch):
    """A trailer naming a credited handle with an unknown email is still missing."""
    log = _record(SHA_A, "dkpnw", "1+dkpnw@users.noreply.github.com",
                  "Pair\n\nCo-authored-by: alice <someone@example.com>\n")
    fake_git_and_gh(monkeypatch, log, {})
    candidates = credits_check.collect("o/r", "v9.0.0")
    assert candidates == [("alice <someone@example.com>", "co-author aaaaaaa")]
    assert credits_check.missing(candidates, {"alice"}) == {
        "alice <someone@example.com>": ["co-author aaaaaaa"]}


def test_main_reports_an_unknown_coauthor_missing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(credits_check, "KNOWN_LOGINS", {})
    (tmp_path / "README.md").write_text("## Credits & prior art\n\n- @bo-gh\n")
    monkeypatch.setattr(credits_check, "ROOT", tmp_path)
    log = _record(SHA_A, "dkpnw", "1+dkpnw@users.noreply.github.com",
                  "Pair\n\nCo-authored-by: Bo <7+bo-gh@users.noreply.github.com>\n"
                  "Co-authored-by: Dee Unknown <dee@example.net>\n")
    fake_git_and_gh(monkeypatch, log, {})
    assert credits_check.main(["--since", "v9.0.0", "--repo", "o/r"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert "credited  bo-gh  (co-author aaaaaaa)" in out
    assert "MISSING   Dee Unknown <dee@example.net>  (co-author aaaaaaa)" in out
    assert out[-1] == "1 missing from README '## Credits & prior art': Dee Unknown <dee@example.net>"
