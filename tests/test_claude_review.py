"""Claude Code as a reviewer: vendor record, severity markers, review bodies,
summary comments, login binding, discovery.

Markup is recorded from live Claude Code reviews (hosted Code Review on
anthropics/claude-agent-sdk-python#1418 and #1422, 2026-10-21) and from a CI
job running Claude Code's code-review skill that posts its summary as
github-actions[bot] (qizsecurity/qiz#2885, 2026-10-21), prose shortened.
"""

from __future__ import annotations

import json

import pytest

import fetch_gemini_threads as fgt
import request_rereview
import review_vendors
import reviewer_resolver
from fetch_gemini_threads import (
    clean_blocked_by,
    copilot_overview_severities,
    parse_claude_overview,
    parse_reviewer_overview,
    parse_summary_comment,
    review_activity_fingerprint,
    reviewer_overview_for,
    severity_in_text,
    summary_comment_findings,
    summary_review_comments,
    thread_severity,
)


CLAUDE = "claude"
PR_URL = "https://github.com/acme/widget/pull/159"


@pytest.fixture(autouse=True)
def _cap_check_is_inert(monkeypatch):
    from conftest import stub_cap_check_known_under_cap

    stub_cap_check_known_under_cap(monkeypatch)
OLD_HEAD = "ff99634a0000000000000000000000000000ff99"
NEW_HEAD = "e5d1c2c00000000000000000000000000000e5d1"

FIRST_REVIEW = (
    "Beyond the inline findings, I also checked two things: no reference to the "
    "old token survives anywhere in the repo.\n\n\n<details>\n<summary>Extended "
    "reasoning...</summary>\n\nThe change moves the upload to OIDC.\n\n</details>\n\n"
    "Findings marked 🟡 are optional suggestions and need no follow-up push.\n\n"
    "<!-- bhrv:87bf3c3ca48109a6 -->"
)
REREVIEW_WITH_OPEN_ITEMS = (
    "**No new blocking issues.** 2 optional suggestions (nits or notes on "
    "pre-existing code) were found and not posted. Nothing in this review needs "
    "a push before merging.\n\n**Still open from earlier reviews** (2):\n"
    "- _Unresolved: 2 minor or pre-existing._\n\n"
    "<!-- bughunter:open-items:v1 count=2\n"
    "r4331815546 nit 1175 src/claude_agent_sdk/_internal/sessions.py\n"
    "r4331815536 normal 1187 src/claude_agent_sdk/_internal/sessions.py\n"
    "-->\n\n<!-- bhrv:5575f596c666a4d8 -->"
)
CLEAN_REVIEW = (
    "**Nothing blocking.** The follow-up commit addresses the concern from my "
    "earlier review. I found no bugs in this version.\n\n<!-- bhrv:244bee2cbc900d30 -->"
)
ACTION_CLEAN_NOTE = "No issues found. Checked for bugs and CLAUDE.md compliance."
WORKING_COMMENT = (
    "Claude Code is working… <img src=\"https://github.com/user-attachments/assets/"
    "5ac382c7-e004-429b-8e35-7feb3e8f9c6f\" width=\"14px\" />\n\n"
    "I'll analyze this and get back to you."
)
CI_SUMMARY = (
    "# Code Review: PR #2885 - Device walks resume after a restart\n\n"
    "## Owners\n- **Author**: omri\n\n"
    "## Findings\n\n"
    "### Minor: Unchecked error in test code\n\n"
    "**File**: `wasm/modules/crowdstrike/resume_durable_test.go:19`\n"
    "**Severity**: Minor (test code only)\n\n"
    "The `url.Parse` error is ignored.\n\n"
    "### High: Cursor decode trusts the count\n\n"
    "**File**: `wasm/modules/crowdstrike/resume.go:54`\n\n"
    "A negative count is accepted.\n\n"
    "## Analysis\n\n### Correctness ✓\n\nFine.\n\n"
    "## Verdict\n\n**APPROVED** ✅\n\n<!-- review:complete -->"
)
CI_SUMMARY_CLEAN = (
    "## Code review — PR #2885\n\n**What changed:**\n```text\ncollector ──▶ backend\n```\n\n"
    "No issues found.\n\n**Verdict:** 0 Critical, 0 Warnings, 0 Suggestions — Approve\n"
    "<!-- review:complete -->"
)
SKILL_LIST_SUMMARY = (
    "## Code review\n\nFound 2 issues:\n\n"
    "1. Missing error handling for OAuth callback (CLAUDE.md says \"Always handle OAuth errors\")\n\n"
    "https://github.com/acme/widget/blob/abc123abc123abc123abc123abc123abc123abc1/src/auth.ts#L67-L72\n\n"
    "2. Memory leak: OAuth state not cleaned up\n\n"
    "https://github.com/acme/widget/blob/abc123abc123abc123abc123abc123abc123abc1/src/auth.ts#L88-L95\n\n"
    "<!-- review:complete -->"
)


def _review(body, submitted_at, commit=OLD_HEAD, url=PR_URL + "#pullrequestreview-1", author=CLAUDE):
    return {
        "author": {"login": author, "__typename": "Bot"},
        "body": body,
        "state": "COMMENTED",
        "submittedAt": submitted_at,
        "commit": {"oid": commit},
        "url": url,
    }


def _comment(body, created_at, author=CLAUDE, comment_id="IC_1"):
    return {
        "id": comment_id,
        "author": {"login": author, "__typename": "Bot"},
        "body": body,
        "createdAt": created_at,
        "url": PR_URL + f"#issuecomment-{comment_id}",
    }


def _thread(thread_id, discussion_id, body, path="src/a.py", author=CLAUDE):
    return {
        "id": thread_id,
        "isResolved": False,
        "isOutdated": False,
        "path": path,
        "line": 10,
        "comments": {"nodes": [{
            "id": f"C_{thread_id}",
            "author": {"login": author, "__typename": "Bot"},
            "body": body,
            "createdAt": "2026-10-20T18:35:06Z",
            "url": f"{PR_URL}#discussion_r{discussion_id}",
        }]},
    }


def _pr(*, reviews=(), comments=(), threads=(), head=OLD_HEAD, committed=None, checks=()):
    pr = {
        "headRefOid": head,
        "reviews": {"nodes": list(reviews)},
        "comments": {"nodes": list(comments)},
        "reviewThreads": {"nodes": list(threads)},
    }
    if committed or checks:
        commit = {"committedDate": committed}
        if checks:
            commit["statusCheckRollup"] = {"contexts": {"nodes": list(checks)}}
        pr["commits"] = {"nodes": [{"commit": commit}]}
    return pr


# --- vendor record -----------------------------------------------------------


def test_claude_is_a_known_comment_vendor_with_the_hosted_review_command():
    vendor = review_vendors.vendor_for("claude")

    assert vendor is review_vendors.CLAUDE
    assert vendor.display_name == "Claude Code"
    assert vendor.mention == "@claude"
    assert vendor.rereview_phrase == "@claude review"
    assert vendor.trigger_kind == "comment"
    assert vendor.auto_reviews is False, "whether a review runs unprompted is a repo setting the loop cannot read"
    assert vendor.head_reviews is True
    assert vendor.summary_comments is True


@pytest.mark.parametrize("name", ["claude", "claude[bot]", "@claude", "Claude", "claude-code", "Claude Code"])
def test_claude_resolves_from_every_spelling(name):
    assert review_vendors.vendor_for(name) is review_vendors.CLAUDE


def test_adding_claude_changed_nothing_about_the_other_vendors():
    assert review_vendors.SUGGESTED_VENDOR is review_vendors.CODEX
    assert review_vendors.COPILOT.head_reviews is True
    assert review_vendors.COPILOT.summary_comments is False
    assert review_vendors.CODEX.head_reviews is False
    assert review_vendors.GEMINI.summary_comments is False
    assert reviewer_resolver.phrase_for("claude") == "@claude review"
    assert reviewer_resolver.trigger_for("claude") == "@claude"
    assert reviewer_resolver.head_reviews_for("chatgpt-codex-connector") is False


# --- login binding (a CI job posting Claude's review as github-actions) ------


def test_a_bound_login_borrows_the_vendor_but_keeps_its_own_spelling():
    review_vendors.bind_login("github-actions", "claude")

    assert review_vendors.vendor_for("github-actions") is review_vendors.CLAUDE
    assert review_vendors.vendor_for("github-actions[bot]") is review_vendors.CLAUDE
    assert reviewer_resolver.canonical_login("github-actions") == "github-actions"
    assert reviewer_resolver.canonical_login("github-actions[bot]") == "github-actions"
    assert reviewer_resolver.display_name_for("github-actions") == "Claude Code (as github-actions)"
    assert reviewer_resolver.trigger_for("github-actions") == "@claude"
    assert reviewer_resolver.head_reviews_for("github-actions") is True
    # Unbound again, the login is the unknown reviewer it always was.
    review_vendors.unbind_all()
    assert review_vendors.vendor_for("github-actions") is None
    assert reviewer_resolver.display_name_for("github-actions") == "Github Actions"


def test_binding_refuses_another_vendors_own_login_and_unknown_vendors():
    with pytest.raises(ValueError):
        review_vendors.bind_login("chatgpt-codex-connector", "claude")
    with pytest.raises(ValueError):
        review_vendors.bind_login("github-actions", "coderabbitai")
    assert review_vendors.vendor_for("github-actions") is None


def test_a_record_made_with_a_vendor_persists_the_binding_and_restores_it():
    record = reviewer_resolver.make_reviewer_record(
        "github-actions[bot]", source="confirmed", vendor="claude"
    )

    assert record["login"] == "github-actions"
    assert record["vendor"] == "claude"
    assert record["display_name"] == "Claude Code (as github-actions)"
    assert record["review_trigger"] == "@claude"
    assert record["trigger_kind"] == "comment"
    assert record["auto_reviews"] is False

    review_vendors.unbind_all()
    assert review_vendors.vendor_for("github-actions") is None
    reviewer_resolver.bind_from_record(record)
    assert review_vendors.vendor_for("github-actions") is review_vendors.CLAUDE


def test_naming_the_vendor_whose_login_it_already_is_changes_nothing():
    record = reviewer_resolver.make_reviewer_record("claude[bot]", source="explicit", vendor="claude")

    assert record["login"] == "claude"
    assert "vendor" not in record
    assert review_vendors.bound_login("claude") is None


def test_a_record_with_an_unknown_vendor_name_is_left_unbound():
    reviewer_resolver.bind_from_record({"login": "github-actions", "vendor": "sourcery"})
    assert review_vendors.vendor_for("github-actions") is None


# --- severity markers --------------------------------------------------------


@pytest.mark.parametrize("body, expected", [
    ("🔴 If PyPI's trusted publishers are set up as described, every upload fails.", "high"),
    ("🟡 nit (optional): the new rule has no automated check.", "low"),
    ("🟡 (optional) Callers that resumed a session now get the discarded results.", "low"),
    ("🟣 This bug exists on main too: the retry loop never backs off.", "low"),
    ("**🔴 Bold marker** still leads the comment.", "high"),
    ("  \n🔴 leading whitespace is fine", "high"),
    ("**Severity**: Minor (test code only)", "low"),
    ("**Severity:** Critical", "critical"),
    ("**Severity**: Important", "high"),
    ("**Severity**: Warning", "medium"),
    ("The marker 🔴 in the middle of prose is not a grade.", "unknown"),
    ("plain prose, no marker", "unknown"),
])
def test_claude_markers_normalize_to_the_shared_scale(body, expected):
    assert severity_in_text(body) == expected
    assert thread_severity(_thread("t", "1", body)) == expected


def test_a_leading_plain_priority_still_wins_over_a_later_claude_marker():
    assert severity_in_text("[P0] 🟡 the bracket grade is the reviewer's") == "critical"


# --- hosted review bodies ----------------------------------------------------


def test_a_first_review_body_is_complete_with_its_findings_in_the_threads():
    parsed = parse_claude_overview(FIRST_REVIEW)

    assert parsed["complete"] is True
    assert parsed["open_findings"] == 0
    assert parsed["findings"] == []
    assert parsed["approving"] is False
    assert parsed["assessment"].startswith("Beyond the inline findings")
    assert "Extended reasoning" not in parsed["assessment"]


def test_a_rereview_lists_what_is_still_open_with_the_check_runs_severity_words():
    parsed = parse_claude_overview(REREVIEW_WITH_OPEN_ITEMS)

    assert parsed["approving"] is True
    assert parsed["assessment"] == "No new blocking issues."
    assert parsed["open_findings"] == 2
    assert parsed["findings"] == [
        {"severity": "low", "discussion_id": "4331815546",
         "title": "src/claude_agent_sdk/_internal/sessions.py:1175"},
        {"severity": "high", "discussion_id": "4331815536",
         "title": "src/claude_agent_sdk/_internal/sessions.py:1187"},
    ]
    assert parsed["unlinked_findings"] == 0


def test_a_clean_review_body_approves_with_zero_open():
    parsed = parse_claude_overview(CLEAN_REVIEW)

    assert parsed == {
        "assessment": "Nothing blocking.", "approving": True, "open_findings": 0,
        "findings": [], "unlinked_findings": 0, "complete": True,
    }


def test_a_body_less_review_is_a_complete_review_of_its_head():
    """Each inline comment the claude-code-action posts is its own review
    with an empty body; its finding is the thread, which is live state."""
    assert parse_claude_overview("")["complete"] is True
    assert parse_claude_overview("  \n")["open_findings"] == 0


def test_bodies_without_claudes_marker_are_not_claude_overviews():
    assert parse_claude_overview("Looks good to me.") is None
    assert parse_claude_overview(None) is None
    assert parse_reviewer_overview("<!-- ccr-overview-v2 -->\n### 🟢 Approval recommended\n\n**0 open findings**", CLAUDE) is None
    assert parse_reviewer_overview(CLEAN_REVIEW, "copilot-pull-request-reviewer") is None
    assert parse_reviewer_overview(CLEAN_REVIEW, "chatgpt-codex-connector") is None
    assert parse_reviewer_overview(CLEAN_REVIEW, CLAUDE)["approving"] is True


def test_open_items_grade_threads_and_the_inline_marker_agrees():
    pr = _pr(
        reviews=[_review(REREVIEW_WITH_OPEN_ITEMS, "2026-10-21T01:13:22Z", commit=NEW_HEAD)],
        threads=[_thread("t1", "4331815546", "🟡 (optional) sibling grouping"),
                 _thread("t2", "4331815536", "plain prose from an older format")],
        head=NEW_HEAD,
    )
    severities, warnings = copilot_overview_severities(pr, CLAUDE)

    assert {k: v["severity"] for k, v in severities.items()} == {"4331815546": "low", "4331815536": "high"}
    assert warnings == []
    threads = fgt.filter_threads(pr, CLAUDE, include_resolved=False, include_outdated=False)
    fgt.annotate_reviewer_severity(threads, severities)
    assert [thread_severity(t) for t in threads] == ["low", "high"]


# --- summary comments --------------------------------------------------------


def test_a_ci_summary_yields_graded_findings_with_their_file_and_line():
    parsed = parse_summary_comment(CI_SUMMARY)

    assert parsed["complete"] is True
    assert parsed["approving"] is True
    assert parsed["open_findings"] == 2
    assert [(f["severity"], f["path"], f["line"], f["title"]) for f in parsed["findings"]] == [
        ("low", "wasm/modules/crowdstrike/resume_durable_test.go", 19, "Unchecked error in test code"),
        ("high", "wasm/modules/crowdstrike/resume.go", 54, "Cursor decode trusts the count"),
    ]
    assert parsed["findings"][0]["body"].startswith("### Minor: Unchecked error in test code")
    assert "url.Parse" in parsed["findings"][0]["body"]
    assert "Correctness" not in parsed["findings"][1]["body"], "the section ends at the next heading"
    assert parsed["assessment"].startswith("Code Review: PR #2885")


def test_a_clean_ci_summary_and_the_actions_clean_note_approve_with_zero_findings():
    for body in (CI_SUMMARY_CLEAN, ACTION_CLEAN_NOTE, "Code review found no issues"):
        parsed = parse_summary_comment(body)
        assert parsed["approving"] is True, body
        assert parsed["open_findings"] == 0
        assert parsed["complete"] is True
        assert parsed["findings"] == []


def test_the_code_review_skills_numbered_list_yields_a_finding_per_blob_link():
    parsed = parse_summary_comment(SKILL_LIST_SUMMARY)

    assert parsed["approving"] is False
    assert [(f["path"], f["line"], f["severity"]) for f in parsed["findings"]] == [
        ("src/auth.ts", 67, "unknown"), ("src/auth.ts", 88, "unknown"),
    ]
    assert parsed["findings"][0]["title"].startswith("Missing error handling for OAuth callback")
    assert parsed["findings"][1]["title"].startswith("Memory leak")


def test_numbered_findings_survive_an_approving_verdict():
    """A summary can approve and still list a finding; the verdict is not
    evidence of a zero-finding review (PR #121 review)."""
    parsed = parse_summary_comment(SKILL_LIST_SUMMARY.replace("## Code review", "## Code review\n\n**APPROVED** ✅"))

    assert parsed["approving"] is True
    assert parsed["open_findings"] == 2
    assert [f["line"] for f in parsed["findings"]] == [67, 88]


def test_a_blob_link_in_prose_is_not_a_finding():
    body = (
        "## Code review\n\n**What changed:** the session builder at\n"
        "https://github.com/acme/widget/blob/abc123abc123abc123abc123abc123abc123abc1/src/auth.ts#L67-L72\n"
        "now recovers results.\n\nNo issues found.\n\n<!-- review:complete -->"
    )
    parsed = parse_summary_comment(body)

    assert parsed["findings"] == []
    assert parsed["approving"] is True
    assert parsed["open_findings"] == 0


def test_a_summary_with_neither_findings_nor_approval_is_incomplete_not_zero():
    parsed = parse_summary_comment("## Code review\n\nFound 3 issues, see the job log.\n\n<!-- review:complete -->")

    assert parsed["findings"] == []
    assert parsed["open_findings"] is None
    assert parsed["complete"] is False


def test_the_actions_tracking_comment_is_not_a_review():
    assert parse_summary_comment(WORKING_COMMENT) is None
    assert review_vendors.summary_review_vendor(WORKING_COMMENT) is None
    assert review_vendors.summary_review_vendor("<!-- linear-linkback -->") is None
    assert review_vendors.summary_review_vendor(CI_SUMMARY) is review_vendors.CLAUDE
    assert review_vendors.summary_review_vendor(ACTION_CLEAN_NOTE) is review_vendors.CLAUDE
    assert review_vendors.summary_review_vendor(CLEAN_REVIEW) is review_vendors.CLAUDE


def test_summary_comments_are_read_for_claude_only_and_only_its_own():
    comments = [
        _comment(WORKING_COMMENT, "2026-10-20T18:38:41Z", comment_id="IC_working"),
        _comment(ACTION_CLEAN_NOTE, "2026-10-20T18:42:10Z", comment_id="IC_clean"),
        _comment(CI_SUMMARY, "2026-10-20T18:43:00Z", author="github-actions", comment_id="IC_other"),
    ]
    pr = _pr(comments=comments)

    assert [c["id"] for c in summary_review_comments(pr, CLAUDE)] == ["IC_clean"]
    assert summary_review_comments(pr, "github-actions") == []
    assert summary_review_comments(pr, "chatgpt-codex-connector") == []
    review_vendors.bind_login("github-actions", "claude")
    assert [c["id"] for c in summary_review_comments(pr, "github-actions")] == ["IC_other"]


def test_summary_findings_become_synthetic_threads_graded_by_the_summary():
    review_vendors.bind_login("github-actions", "claude")
    pr = _pr(comments=[_comment(CI_SUMMARY, "2026-10-21T07:09:01Z", author="github-actions", comment_id="IC_review1")])

    findings = summary_comment_findings(pr, "github-actions")

    assert [f["id"] for f in findings] == ["pr-comment:IC_review1:1", "pr-comment:IC_review1:2"]
    assert [(f["path"], f["line"]) for f in findings] == [
        ("wasm/modules/crowdstrike/resume_durable_test.go", 19),
        ("wasm/modules/crowdstrike/resume.go", 54),
    ]
    assert [thread_severity(f) for f in findings] == ["low", "high"]
    assert all(f["isReviewBodyFinding"] and f["isSummaryCommentFinding"] for f in findings)
    assert findings[0]["comments"][0]["author"] == {"login": "github-actions"}
    assert findings[0]["comments"][0]["url"] == PR_URL + "#issuecomment-IC_review1"
    assert fgt.sort_by_severity(list(findings))[0]["line"] == 54


def test_only_the_newest_summary_counts_and_a_later_review_supersedes_it():
    review_vendors.bind_login("github-actions", "claude")
    old = _comment(CI_SUMMARY, "2026-10-21T07:09:01Z", author="github-actions", comment_id="IC_review1")
    new = _comment(CI_SUMMARY_CLEAN, "2026-10-21T07:51:54Z", author="github-actions", comment_id="IC_review2")

    assert summary_comment_findings(_pr(comments=[old, new]), "github-actions") == []
    assert len(summary_comment_findings(_pr(comments=[old]), "github-actions")) == 2

    later_review = _review("", "2026-10-21T08:00:00Z", author="github-actions")
    assert summary_comment_findings(_pr(comments=[old], reviews=[later_review]), "github-actions") == []


# --- the wait -----------------------------------------------------------------


def test_a_clean_note_posted_as_a_comment_ends_the_wait_for_claude_only():
    note = _comment(ACTION_CLEAN_NOTE, "2026-10-20T18:42:10Z")
    pr = _pr(comments=[_comment(WORKING_COMMENT, "2026-10-20T18:38:41Z", comment_id="IC_w"), note])

    assert review_activity_fingerprint(pr, CLAUDE) is not None
    assert review_activity_fingerprint(pr, CLAUDE, after_iso="2026-10-20T18:38:30Z") is not None
    assert review_activity_fingerprint(pr, CLAUDE, after_iso="2026-10-20T18:42:10Z") is None
    # The tracking comment alone is not activity.
    working_only = _pr(comments=[_comment(WORKING_COMMENT, "2026-10-20T18:38:41Z")])
    assert review_activity_fingerprint(working_only, CLAUDE) is None
    # Other vendors keep reading reviews and threads only.
    codex_note = _pr(comments=[_comment(ACTION_CLEAN_NOTE, "2026-10-20T18:42:10Z", author="chatgpt-codex-connector")])
    assert review_activity_fingerprint(codex_note, "chatgpt-codex-connector") is None


def test_the_fingerprint_changes_when_the_summary_is_edited_in_place():
    first = _pr(comments=[_comment(CI_SUMMARY, "2026-10-20T18:42:10Z")])
    edited = _pr(comments=[_comment(CI_SUMMARY_CLEAN, "2026-10-20T18:42:10Z")])

    assert review_activity_fingerprint(first, CLAUDE) != review_activity_fingerprint(edited, CLAUDE)


def test_the_settle_payload_reports_the_summary_comment_time():
    pr = _pr(comments=[_comment(ACTION_CLEAN_NOTE, "2026-10-20T18:42:10Z")])

    assert fgt._latest_submitted_after(pr, CLAUDE, "2026-10-20T18:38:30Z") == "2026-10-20T18:42:10Z"


# --- the overview and the clean gate ------------------------------------------


def test_the_newer_of_head_review_and_summary_comment_is_the_verdict():
    review = _review("", "2026-10-20T18:35:06Z", commit=OLD_HEAD)
    note = _comment(ACTION_CLEAN_NOTE, "2026-10-20T18:42:10Z")
    pr = _pr(reviews=[review], comments=[note], head=NEW_HEAD, committed="2026-10-20T18:37:46Z")

    overview = reviewer_overview_for(pr, CLAUDE)

    assert overview["source"] == "comment"
    assert overview["approving"] is True
    assert overview["open_findings"] == 0
    assert overview["covers_head"] is True
    assert overview["commit"] is None
    assert overview["submitted_at"] == "2026-10-20T18:42:10Z"
    assert clean_blocked_by([], overview, head_review_vendor=True, known_thread_ids=set()) is None

    # A review newer than the note is the verdict instead.
    newer_review = _review(CLEAN_REVIEW, "2026-10-20T18:50:00Z", commit=NEW_HEAD, url=PR_URL + "#pullrequestreview-2")
    pr["reviews"]["nodes"].append(newer_review)
    overview = reviewer_overview_for(pr, CLAUDE)
    assert "source" not in overview
    assert overview["commit"] == NEW_HEAD
    assert overview["approving"] is True


def test_a_summary_older_than_the_head_commit_does_not_cover_the_head():
    stale_note = _comment(ACTION_CLEAN_NOTE, "2026-10-20T18:30:00Z")
    pr = _pr(comments=[stale_note], head=NEW_HEAD, committed="2026-10-20T18:37:46Z")

    overview = reviewer_overview_for(pr, CLAUDE)

    assert overview["covers_head"] is False
    assert clean_blocked_by([], overview, head_review_vendor=True) == "no_review_for_head"
    # Without the head commit's date the newest summary is taken as current.
    assert reviewer_overview_for(_pr(comments=[stale_note], head=NEW_HEAD), CLAUDE)["covers_head"] is True


def test_a_summary_posted_before_the_head_was_pushed_does_not_cover_it():
    """Committed at 09:00, pushed at 11:00 (the head's first check suite):
    a clean note at 10:00 reviewed the previous head (PR #121 review)."""
    checks = [
        {"__typename": "CheckRun", "startedAt": "2026-10-20T11:00:40Z",
         "checkSuite": {"createdAt": "2026-10-20T11:00:05Z"}},
        {"__typename": "StatusContext", "createdAt": "2026-10-20T11:02:00Z"},
    ]
    between = _comment(ACTION_CLEAN_NOTE, "2026-10-20T10:00:00Z")
    after = _comment(ACTION_CLEAN_NOTE, "2026-10-20T11:30:00Z", comment_id="IC_2")

    assert fgt.head_arrived_at(_pr(committed="2026-10-20T09:00:00Z", checks=checks)) == "2026-10-20T11:00:05Z"
    assert reviewer_overview_for(
        _pr(comments=[between], head=NEW_HEAD, committed="2026-10-20T09:00:00Z", checks=checks), CLAUDE
    )["covers_head"] is False
    assert reviewer_overview_for(
        _pr(comments=[between, after], head=NEW_HEAD, committed="2026-10-20T09:00:00Z", checks=checks), CLAUDE
    )["covers_head"] is True
    # A check older than the commit (a rebased head keeps no such check, but
    # a payload could) never moves the boundary backwards.
    early = [{"__typename": "StatusContext", "createdAt": "2026-10-20T08:00:00Z"}]
    assert fgt.head_arrived_at(_pr(committed="2026-10-20T09:00:00Z", checks=early)) == "2026-10-20T09:00:00Z"


def test_claude_has_the_copilot_clean_gate_and_codex_does_not():
    head_review = _review(CLEAN_REVIEW, "2026-10-20T18:41:00Z", commit=NEW_HEAD)
    assert reviewer_overview_for(_pr(reviews=[head_review], head=NEW_HEAD), CLAUDE)["covers_head"] is True
    assert reviewer_overview_for(_pr(reviews=[head_review], head=NEW_HEAD), "chatgpt-codex-connector") is None

    stale = _pr(reviews=[_review(CLEAN_REVIEW, "2026-10-20T18:35:06Z", commit=OLD_HEAD)], head=NEW_HEAD)
    overview = reviewer_overview_for(stale, CLAUDE)
    assert overview["covers_head"] is False
    assert clean_blocked_by([], overview, head_review_vendor=True) == "no_review_for_head"


def test_open_items_the_fetch_cannot_see_block_clean_but_known_ones_do_not():
    pr = _pr(reviews=[_review(REREVIEW_WITH_OPEN_ITEMS, "2026-10-21T01:13:22Z", commit=NEW_HEAD)], head=NEW_HEAD)
    overview = reviewer_overview_for(pr, CLAUDE)

    assert overview["open_findings"] == 2
    assert overview["finding_ids"] == ["4331815546", "4331815536"]
    assert clean_blocked_by([], overview, head_review_vendor=True,
                            known_thread_ids={"4331815546", "4331815536"}) is None
    assert clean_blocked_by([], overview, head_review_vendor=True,
                            known_thread_ids={"4331815546"}) == "overview_reports_open_findings"


# --- discovery ---------------------------------------------------------------


def test_a_ci_job_posting_claudes_summary_is_discovered_bound_to_claude():
    pr = _pr(comments=[
        _comment("<!-- linear-linkback -->", "2026-10-21T06:56:00Z", author="linear", comment_id="IC_l"),
        _comment(CI_SUMMARY, "2026-10-21T07:09:01Z", author="github-actions", comment_id="IC_r"),
    ])

    candidates = reviewer_resolver.discover_candidates(pr, self_login="loop-agent")

    assert [c.to_dict() for c in candidates] == [{
        "login": "github-actions",
        "display_name": "Claude Code (as github-actions)",
        "review_trigger": "@claude",
        "trigger_kind": "comment",
        "source": "comment",
        "vendor": "claude",
    }]


def test_claudes_own_clean_note_is_discovered_without_a_binding():
    pr = _pr(comments=[_comment(ACTION_CLEAN_NOTE, "2026-10-20T18:42:10Z")])

    candidates = reviewer_resolver.discover_candidates(pr, self_login=None)

    assert [(c.login, c.source, c.vendor) for c in candidates] == [("claude", "comment", None)]
    assert "vendor" not in candidates[0].to_dict()


def test_a_login_seen_as_a_review_author_keeps_its_source_and_gains_the_vendor():
    """A job that posts an inline review and a summary under one login is
    still Claude's reviewer; the earlier, stronger source stays (PR #121 review)."""
    pr = _pr(
        reviews=[_review("", "2026-10-21T07:09:00Z", author="github-actions")],
        comments=[_comment(CI_SUMMARY, "2026-10-21T07:09:01Z", author="github-actions", comment_id="IC_r")],
    )

    candidates = reviewer_resolver.discover_candidates(pr, self_login=None)

    assert [(c.login, c.source, c.vendor, c.display_name, c.review_trigger) for c in candidates] == [
        ("github-actions", "review", "claude", "Claude Code (as github-actions)", "@claude"),
    ]


def test_a_login_seen_in_a_thread_keeps_that_source():
    pr = _pr(
        threads=[_thread("t1", "1", "🔴 a finding")],
        comments=[_comment(CLEAN_REVIEW, "2026-10-20T18:42:10Z")],
    )

    candidates = reviewer_resolver.discover_candidates(pr, self_login=None)

    assert [(c.login, c.source) for c in candidates] == [("claude", "thread")]


def test_a_human_or_an_unsignalled_bot_comment_is_not_a_candidate():
    pr = _pr(comments=[
        {"id": "IC_h", "author": {"login": "alice", "__typename": "User"}, "body": CI_SUMMARY, "createdAt": "2026-10-21T07:09:01Z"},
        _comment("Deployed preview to https://preview.example", "2026-10-21T07:10:00Z", author="vercel", comment_id="IC_v"),
    ])

    assert reviewer_resolver.discover_candidates(pr, self_login=None) == []


# --- the re-review request ---------------------------------------------------


def test_the_persisted_claude_selection_posts_the_hosted_review_command(monkeypatch, capsys, tmp_path):
    state = {"acme/widget#159": {"reviewer": reviewer_resolver.make_reviewer_record("claude", source="confirmed")}}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(request_rereview, "head_already_reviewed", lambda *a, **k: None)
    posted = {}
    monkeypatch.setattr(request_rereview, "post_rereview", lambda repo, pr, phrase, **k: (
        posted.update(phrase=phrase) or {"created_at": "2026-10-20T18:38:30Z", "repo": repo, "pr": pr, "phrase": phrase}
    ))

    assert request_rereview.main(["--repo", "acme/widget", "--pr", "159", "--json"]) == 0

    assert posted["phrase"] == "@claude review"
    assert json.loads(capsys.readouterr().out)["phrase"] == "@claude review"


def test_a_head_claude_already_reviewed_is_not_pinged_again(monkeypatch, capsys):
    monkeypatch.setattr(request_rereview, "current_head", lambda *a, **k: NEW_HEAD)
    monkeypatch.setattr(
        request_rereview, "_all_reviews",
        lambda *a, **k: [
            {"user": {"login": "claude[bot]"}, "state": "COMMENTED", "commit_id": OLD_HEAD, "submitted_at": "2026-10-20T18:35:06Z"},
            {"user": {"login": "claude[bot]"}, "state": "COMMENTED", "commit_id": NEW_HEAD, "submitted_at": "2026-10-20T18:41:00Z"},
        ],
    )
    monkeypatch.setattr(
        request_rereview, "post_rereview",
        lambda *a, **k: pytest.fail("the head is already reviewed; nothing may be posted"),
    )

    rc = request_rereview.main([
        "--repo", "acme/widget", "--pr", "159", "--reviewer-login", "claude",
        "--expect-head", NEW_HEAD, "--json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "already_reviewed_head"
    assert payload["posted"] is False
    assert payload["head"] == NEW_HEAD


def test_the_dedupe_matches_the_selected_login_not_the_vendor(monkeypatch, capsys):
    """Hosted Claude reviewed the head as claude[bot]; the selected reviewer is
    the CI job bound as github-actions, which has not. The fetch filters by
    that login, so the ping must go out (PR #121 review)."""
    monkeypatch.setattr(request_rereview, "current_head", lambda *a, **k: NEW_HEAD)
    monkeypatch.setattr(
        request_rereview, "_all_reviews",
        lambda *a, **k: [
            {"user": {"login": "claude[bot]"}, "state": "COMMENTED", "commit_id": NEW_HEAD, "submitted_at": "2026-10-20T18:41:00Z"},
        ],
    )
    posted = {}
    monkeypatch.setattr(request_rereview, "post_rereview", lambda repo, pr, phrase, **k: (
        posted.update(phrase=phrase) or {"created_at": "2026-10-20T18:50:00Z", "repo": repo, "pr": pr, "phrase": phrase}
    ))

    assert request_rereview.main([
        "--repo", "acme/widget", "--pr", "159",
        "--reviewer-login", "github-actions", "--reviewer-vendor", "claude", "--json",
    ]) == 0
    assert posted["phrase"] == "@claude review"
    capsys.readouterr()

    # The job's own head review, under either REST spelling, does dedupe.
    for spelling in ("github-actions[bot]", "GitHub-Actions"):
        monkeypatch.setattr(
            request_rereview, "_all_reviews",
            lambda *a, _s=spelling, **k: [
                {"user": {"login": _s}, "state": "COMMENTED", "commit_id": NEW_HEAD, "submitted_at": "2026-10-20T18:41:00Z"},
            ],
        )
        monkeypatch.setattr(request_rereview, "post_rereview", lambda *a, **k: pytest.fail("already reviewed"))
        assert request_rereview.main([
            "--repo", "acme/widget", "--pr", "159",
            "--reviewer-login", "github-actions", "--reviewer-vendor", "claude", "--json",
        ]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "already_reviewed_head"


def test_a_dismissed_or_stale_claude_review_does_not_block_the_ping(monkeypatch, capsys):
    monkeypatch.setattr(request_rereview, "current_head", lambda *a, **k: NEW_HEAD)
    monkeypatch.setattr(
        request_rereview, "_all_reviews",
        lambda *a, **k: [
            {"user": {"login": "claude[bot]"}, "state": "COMMENTED", "commit_id": OLD_HEAD, "submitted_at": "2026-10-20T18:35:06Z"},
            {"user": {"login": "claude[bot]"}, "state": "DISMISSED", "commit_id": NEW_HEAD, "submitted_at": "2026-10-20T18:41:00Z"},
            {"user": {"login": "alice"}, "state": "APPROVED", "commit_id": NEW_HEAD, "submitted_at": "2026-10-20T18:48:52Z"},
        ],
    )
    posted = {}
    monkeypatch.setattr(request_rereview, "post_rereview", lambda repo, pr, phrase, **k: (
        posted.update(phrase=phrase) or {"created_at": "2026-10-20T18:50:00Z", "repo": repo, "pr": pr, "phrase": phrase}
    ))

    assert request_rereview.main([
        "--repo", "acme/widget", "--pr", "159", "--reviewer-login", "claude", "--json",
    ]) == 0
    assert posted["phrase"] == "@claude review"


def test_an_unreadable_head_state_still_pings_rather_than_stalling(monkeypatch, capsys):
    monkeypatch.setattr(request_rereview, "current_head", lambda *a, **k: None)
    posted = {}
    monkeypatch.setattr(request_rereview, "post_rereview", lambda repo, pr, phrase, **k: (
        posted.update(phrase=phrase) or {"created_at": "2026-10-20T18:50:00Z", "repo": repo, "pr": pr, "phrase": phrase}
    ))

    assert request_rereview.main(["--repo", "acme/widget", "--pr", "159", "--reviewer-login", "claude"]) == 0
    assert posted["phrase"] == "@claude review"


def test_codex_pings_never_read_reviews_for_a_head_dedupe(monkeypatch, capsys):
    monkeypatch.setattr(
        request_rereview, "_all_reviews",
        lambda *a, **k: pytest.fail("Codex reviews name no commit; nothing to dedupe on"),
    )
    monkeypatch.setattr(request_rereview, "current_head", lambda *a, **k: pytest.fail("no head read either"))
    posted = {}
    monkeypatch.setattr(request_rereview, "post_rereview", lambda repo, pr, phrase, **k: (
        posted.update(phrase=phrase) or {"created_at": "2026-10-20T18:50:00Z", "repo": repo, "pr": pr, "phrase": phrase}
    ))

    assert request_rereview.main(["--repo", "acme/widget", "--pr", "159", "--reviewer-login", "chatgpt-codex-connector"]) == 0
    assert posted["phrase"] == "@codex review"


def test_a_bound_login_given_on_the_command_line_posts_claudes_command(monkeypatch, capsys):
    monkeypatch.setattr(request_rereview, "head_already_reviewed", lambda *a, **k: None)
    posted = {}
    monkeypatch.setattr(request_rereview, "post_rereview", lambda repo, pr, phrase, **k: (
        posted.update(phrase=phrase) or {"created_at": "2026-10-21T07:39:00Z", "repo": repo, "pr": pr, "phrase": phrase}
    ))

    assert request_rereview.main([
        "--repo", "acme/widget", "--pr", "2885",
        "--reviewer-login", "github-actions", "--reviewer-vendor", "claude", "--json",
    ]) == 0
    assert posted["phrase"] == "@claude review"


def test_the_persisted_binding_is_restored_by_the_request_script(monkeypatch, capsys, tmp_path):
    record = reviewer_resolver.make_reviewer_record("github-actions", source="confirmed", vendor="claude")
    review_vendors.unbind_all()
    state = {"acme/widget#2885": {"reviewer": record}}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(request_rereview, "head_already_reviewed", lambda *a, **k: None)
    posted = {}
    monkeypatch.setattr(request_rereview, "post_rereview", lambda repo, pr, phrase, **k: (
        posted.update(phrase=phrase) or {"created_at": "2026-10-21T07:39:00Z", "repo": repo, "pr": pr, "phrase": phrase}
    ))

    assert request_rereview.main(["--repo", "acme/widget", "--pr", "2885", "--json"]) == 0
    assert posted["phrase"] == "@claude review"


def test_a_vendor_without_a_login_or_an_unknown_vendor_is_an_argument_error(capsys):
    assert request_rereview.main(["--repo", "acme/widget", "--pr", "1", "--reviewer-vendor", "claude"]) == 2
    assert "--reviewer-login" in capsys.readouterr().err
    assert request_rereview.main([
        "--repo", "acme/widget", "--pr", "1", "--reviewer-login", "github-actions", "--reviewer-vendor", "nope",
    ]) == 2
    assert "unknown reviewer vendor" in capsys.readouterr().err
