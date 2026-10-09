"""Pure parsing in the pre-tag credits check, with invented handles only; no git or gh."""

from datetime import datetime, timezone

import pytest

from tools.credits_check import (
    closed_since,
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
    since = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
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
