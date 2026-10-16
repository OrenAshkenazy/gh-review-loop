"""Copilot writes severities into its review overview, not into the inline
comments. These tests use the overview markup recorded from live Copilot
reviews (block/schemabot#1669 and haacked/dotfiles#235, 2026-10-07), with the
prose shortened."""

from __future__ import annotations

import cluster_findings
from fetch_gemini_threads import (
    annotate_reviewer_severity,
    clean_blocked_by,
    copilot_overview_severities,
    discussion_ids_on_pr,
    finding_fingerprint,
    latest_reviewer_overview,
    parse_copilot_overview,
    thread_render_fingerprint,
    thread_severity,
)


COPILOT = "copilot-pull-request-reviewer"
PR_URL = "https://github.com/acme/widget/pull/159"

OVERVIEW = """<!-- ccr-overview-v2 -->

### 🟡 Changes recommended

The integration test exceeds the repository's timeout limit.

<details open>
<summary><strong>2 open findings</strong></summary>

- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Test exceeds 30-second limit](#discussion_r4208919854) · New
- <picture><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> [Test bypasses production parser](#discussion_r4208919952) · New
</details>

<details>
<summary><strong>What changed in this PR</strong></summary>

Updates the dependency and removes obsolete configuration passthroughs.
</details>

🧠 **Review effort:** Balanced
"""

CLEAN_OVERVIEW = """<!-- ccr-overview-v2 -->

### 🟢 Approval recommended

The status logic is consistent across bundled copies.

**0 open findings**

<details>
<summary><strong>What changed in this PR</strong></summary>

Refactors the status helper.
</details>
"""

MARKER_ONLY = "<!-- ccr-overview-v2 -->\n\nSomething unexpected that is neither a heading nor a count.\n"


def _review(body, submitted_at, commit="f4a55e3000", url=PR_URL + "#pullrequestreview-1", author=COPILOT):
    return {
        "author": {"login": author, "__typename": "Bot"},
        "body": body,
        "state": "COMMENTED",
        "submittedAt": submitted_at,
        "commit": {"oid": commit},
        "url": url,
    }


def _pr(*reviews, head="f4a55e3000"):
    return {"headRefOid": head, "reviews": {"nodes": list(reviews)}}


def _thread(thread_id, discussion_id, body="plain prose, no marker", path="src/a.py"):
    return {
        "id": thread_id,
        "path": path,
        "line": 7,
        "comments": [{
            "author": {"login": COPILOT},
            "body": body,
            "url": f"{PR_URL}#discussion_r{discussion_id}",
            "path": path,
            "line": 7,
        }],
    }


def test_parses_assessment_findings_and_count():
    parsed = parse_copilot_overview(OVERVIEW)

    assert parsed["assessment"] == "Changes recommended"
    assert parsed["approving"] is False
    assert parsed["open_findings"] == 2
    assert parsed["complete"] is True
    assert parsed["findings"] == [
        {"severity": "high", "discussion_id": "4208919854", "title": "Test exceeds 30-second limit"},
        {"severity": "medium", "discussion_id": "4208919952", "title": "Test bypasses production parser"},
    ]
    assert parsed["unlinked_findings"] == 0


def test_clean_overview_is_approving_with_zero_findings():
    parsed = parse_copilot_overview(CLEAN_OVERVIEW)

    assert parsed["assessment"] == "Approval recommended"
    assert parsed["approving"] is True
    assert parsed["open_findings"] == 0
    assert parsed["complete"] is True
    assert parsed["findings"] == []


def test_non_copilot_bodies_are_not_overviews():
    assert parse_copilot_overview("![P1 Badge](https://img.shields.io/badge/P1-red) fix this") is None
    assert parse_copilot_overview("### Looks good\n\n**0 open findings**") is None
    assert parse_copilot_overview("") is None
    assert parse_copilot_overview(None) is None


def test_marker_without_heading_or_count_is_incomplete():
    parsed = parse_copilot_overview(MARKER_ONLY)

    assert parsed is not None
    assert parsed["complete"] is False
    assert parsed["open_findings"] is None
    assert parsed["approving"] is False
    assert parsed["findings"] == []


def test_an_unlinked_finding_is_a_warning_not_a_finding():
    body = OVERVIEW.replace(
        "[Test bypasses production parser](#discussion_r4208919952)",
        "Test bypasses production parser",
    )

    parsed = parse_copilot_overview(body)

    assert len(parsed["findings"]) == 1
    assert parsed["unlinked_findings"] == 1


def test_thread_gets_its_severity_and_source_from_the_overview():
    pr = _pr(_review(OVERVIEW, "2026-10-07T15:46:14Z"))
    flagged = _thread("T1", "4208919854")
    unlisted = _thread("T2", "999")

    severities, warnings = copilot_overview_severities(pr, COPILOT)
    annotate_reviewer_severity([flagged, unlisted], severities)

    assert thread_severity(flagged) == "high"
    assert flagged["reviewerSeveritySource"] == {
        "review_url": PR_URL + "#pullrequestreview-1", "commit": "f4a55e3000",
    }
    assert thread_severity(unlisted) == "unknown"
    assert "reviewerSeverity" not in unlisted
    assert warnings == []


def test_newer_overview_wins_and_records_its_commit():
    pr = _pr(
        _review(OVERVIEW, "2026-10-07T15:46:14Z"),
        _review(
            OVERVIEW.replace('alt="High severity"', 'alt="Low severity"'),
            "2026-10-07T16:20:00Z", commit="d7efa49000", url=PR_URL + "#pullrequestreview-2",
        ),
        head="d7efa49000",
    )
    flagged = _thread("T1", "4208919854")

    severities, _warnings = copilot_overview_severities(pr, COPILOT)
    annotate_reviewer_severity([flagged], severities)

    assert thread_severity(flagged) == "low"
    assert flagged["reviewerSeveritySource"]["commit"] == "d7efa49000"


def test_unparseable_overview_leaves_threads_listed_as_unknown():
    pr = _pr(_review(MARKER_ONLY, "2026-10-07T15:46:14Z"))
    thread = _thread("T1", "4208919854")

    severities, warnings = copilot_overview_severities(pr, COPILOT)
    annotate_reviewer_severity([thread], severities)

    assert severities == {}
    assert warnings == []
    assert thread_severity(thread) == "unknown"


def test_unlinked_findings_surface_as_a_warning_naming_the_review():
    body = OVERVIEW.replace("(#discussion_r4208919952)", "")
    pr = _pr(_review(body, "2026-10-07T15:46:14Z"))

    _severities, warnings = copilot_overview_severities(pr, COPILOT)

    assert len(warnings) == 1
    assert "1 finding(s)" in warnings[0]
    assert "#pullrequestreview-1" in warnings[0]


def test_other_authors_overviews_are_ignored():
    pr = _pr(_review(OVERVIEW, "2026-10-07T15:46:14Z", author="someone-else"))

    assert copilot_overview_severities(pr, COPILOT) == ({}, [])
    assert latest_reviewer_overview(pr, COPILOT) is None


def test_latest_reviewer_overview_reports_covers_head():
    pr = _pr(
        _review(OVERVIEW, "2026-10-07T15:46:14Z"),
        _review(CLEAN_OVERVIEW, "2026-10-07T16:20:00Z", commit="6f03c25000", url=PR_URL + "#pullrequestreview-2"),
        head="6f03c25000",
    )

    overview = latest_reviewer_overview(pr, COPILOT)

    assert overview == {
        "assessment": "Approval recommended",
        "approving": True,
        "open_findings": 0,
        "complete": True,
        "covers_head": True,
        "commit": "6f03c25000",
        "submitted_at": "2026-10-07T16:20:00Z",
        "url": PR_URL + "#pullrequestreview-2",
        "finding_ids": [],
        "unlinked_findings": 0,
    }
    stale = latest_reviewer_overview({**pr, "headRefOid": "9999999000"}, COPILOT)
    assert stale["covers_head"] is False


def test_a_head_review_that_is_not_a_readable_overview_is_still_the_head_review():
    # Copilot reviewed the head but the body has no overview we can parse
    # (markup changed, or an empty body). That is "unparseable", not "no
    # review for head": the waiter is already satisfied, so saying "wait"
    # would send the loop into a dead end.
    pr = _pr(
        _review(OVERVIEW, "2026-10-07T15:46:14Z"),
        _review("Looks fine.", "2026-10-07T16:20:00Z", commit="6f03c25000", url=PR_URL + "#pullrequestreview-2"),
        head="6f03c25000",
    )

    overview = latest_reviewer_overview(pr, COPILOT)

    assert overview["covers_head"] is True
    assert overview["complete"] is False
    assert overview["open_findings"] is None
    assert overview["url"] == PR_URL + "#pullrequestreview-2"
    assert overview["commit"] == "6f03c25000"
    assert clean_blocked_by([], overview, review_request_vendor=True) == "overview_unparseable"


def test_discussion_ids_on_pr_cover_every_thread_regardless_of_state():
    pr = {"reviewThreads": {"nodes": [
        {**_thread("T1", "11"), "isResolved": True},
        {**_thread("T2", "22"), "isResolved": False, "isOutdated": True},
        {"id": "T3", "comments": {"nodes": [{"url": PR_URL + "#discussion_r33"}]}},
        {"id": "T4", "comments": {"nodes": [{"url": None}]}},
    ]}}

    assert discussion_ids_on_pr(pr) == {"11", "22", "33"}
    assert discussion_ids_on_pr({}) == set()


def test_open_findings_whose_threads_exist_on_the_pr_do_not_block_clean():
    # The overview is a snapshot at review time; thread state is live. When
    # every finding it links to has a thread on the PR (resolved, outdated,
    # addressed, or filtered), the normal thread classification decided the
    # PR's state and the snapshot count must not deadlock the loop.
    head = {"approving": False, "open_findings": 2, "complete": True, "covers_head": True,
            "finding_ids": ["11", "22"], "unlinked_findings": 0}

    assert clean_blocked_by([], head, review_request_vendor=True, known_thread_ids={"11", "22", "99"}) is None
    # A linked finding the fetch never saw (page cap, deleted thread): block.
    assert clean_blocked_by([], head, review_request_vendor=True, known_thread_ids={"11"}) == "overview_reports_open_findings"
    # A finding with no thread link at all cannot be dispositioned: block.
    assert clean_blocked_by([], {**head, "unlinked_findings": 1}, review_request_vendor=True, known_thread_ids={"11", "22"}) == "overview_reports_open_findings"
    # Without thread knowledge the gate stays conservative.
    assert clean_blocked_by([], head, review_request_vendor=True) == "overview_reports_open_findings"


def test_clean_blocked_by_table():
    approving_head = {"approving": True, "open_findings": 0, "complete": True, "covers_head": True}
    threads = [_thread("T1", "1")]

    assert clean_blocked_by(threads, approving_head, review_request_vendor=True) is None
    assert clean_blocked_by([], approving_head, review_request_vendor=True) is None
    assert clean_blocked_by([], None, review_request_vendor=True) == "no_review_for_head"
    assert clean_blocked_by([], {**approving_head, "covers_head": False}, review_request_vendor=True) == "no_review_for_head"
    assert clean_blocked_by([], {**approving_head, "complete": False, "open_findings": None}, review_request_vendor=True) == "overview_unparseable"
    assert clean_blocked_by([], {**approving_head, "open_findings": 2}, review_request_vendor=True) == "overview_reports_open_findings"
    # Approval wording alone never matters: a complete head review reporting 0 is clean even if the heading is odd.
    assert clean_blocked_by([], {**approving_head, "approving": False}, review_request_vendor=True) is None
    # Comment vendors have no overview gate.
    assert clean_blocked_by([], None, review_request_vendor=False) is None


def test_codex_and_gemini_severity_parsing_is_untouched():
    gemini = {"comments": [{"body": "![high](https://www.gstatic.com/codereviewagent/high-priority.svg) fix"}]}
    codex = {"comments": [{"body": "![P2 Badge](https://img.shields.io/badge/P2-yellow) fix"}]}

    assert thread_severity(gemini) == "high"
    assert thread_severity(codex) == "medium"


def test_an_annotated_severity_beats_the_regexes():
    thread = {
        "reviewerSeverity": "low",
        "comments": [{"body": "![high](https://www.gstatic.com/codereviewagent/high-priority.svg)"}],
    }

    assert thread_severity(thread) == "low"


def test_bogus_annotation_is_ignored():
    assert thread_severity({"reviewerSeverity": "urgent", "comments": []}) == "unknown"
    assert thread_severity({"reviewerSeverity": "unknown", "comments": []}) == "unknown"
    assert thread_severity({"reviewerSeverity": 3, "comments": []}) == "unknown"


def test_annotation_leaves_finding_fingerprint_and_pattern_signature_unchanged_but_changes_render_fingerprint():
    before = _thread("T1", "4208919854", body="Avoid shadowing the builtin `id` in this helper.")
    after = _thread("T1", "4208919854", body="Avoid shadowing the builtin `id` in this helper.")
    annotate_reviewer_severity([after], {"4208919854": {"severity": "high", "review_url": "u", "commit": "c"}})

    assert finding_fingerprint(before) == finding_fingerprint(after)
    assert cluster_findings.pattern_signature(before) == cluster_findings.pattern_signature(after)
    assert thread_render_fingerprint(before) != thread_render_fingerprint(after)


def test_fewer_parsed_links_than_the_reported_count_blocks_clean():
    """`open_findings` is authoritative; `finding_ids` is what the parser could
    recognize. When Copilot reports two findings and only one link parses (a
    markup change dropping a severity icon, say), one finding is unaccounted
    for even though every parsed link is known — the gate must stay closed
    (PR #116 review)."""
    head = {"approving": False, "open_findings": 2, "complete": True, "covers_head": True,
            "finding_ids": ["11"], "unlinked_findings": 0}

    assert clean_blocked_by(
        [], head, review_request_vendor=True, known_thread_ids={"11", "22"}
    ) == "overview_reports_open_findings"

    # The count and the links agreeing is still clean.
    agreed = {**head, "open_findings": 1}
    assert clean_blocked_by(
        [], agreed, review_request_vendor=True, known_thread_ids={"11", "22"}
    ) is None


def test_a_pending_review_never_covers_the_head_or_grades_a_finding():
    """GraphQL can expose an in-progress review as `state: PENDING`.
    `head_review()` already skips those; the overview helpers must too, or a
    draft reports as covering the head — routing the loop to
    overview_unparseable instead of waiting, or clearing the clean gate before
    the review is submitted (PR #116 review)."""
    import fetch_gemini_threads as fgt

    submitted = {
        "author": {"login": "copilot-pull-request-reviewer"},
        "state": "COMMENTED",
        "submittedAt": "2026-10-07T15:46:14Z",
        "commit": {"oid": "aaa111"},
        "body": "## Pull Request Overview\n\nLooks good overall.\n\n**Open findings: 0**\n",
        "url": "https://github.com/o/r/pull/1#pullrequestreview-1",
    }
    pending = {
        "author": {"login": "copilot-pull-request-reviewer"},
        "state": "PENDING",
        "submittedAt": None,
        "commit": {"oid": "bbb222"},
        "body": "",
        "url": "https://github.com/o/r/pull/1#pullrequestreview-2",
    }
    pr = {
        "headRefOid": "bbb222",
        "reviews": {"nodes": [submitted, pending]},
    }

    assert fgt._reviews_oldest_first(pr, "copilot-pull-request-reviewer") == [submitted]

    overview = fgt.latest_reviewer_overview(pr, "copilot-pull-request-reviewer")
    assert overview is None or overview.get("covers_head") is not True


def test_finding_titles_may_contain_brackets():
    """A title like `arr[i]` must not stop the discussion link from matching;
    otherwise the finding counts as unlinked and the clean gate never clears
    (PR #116 review)."""
    body = (
        "<!-- ccr-overview-v2 -->\n\n### 🟡 Changes recommended\n\n"
        "<summary><strong>2 open findings</strong></summary>\n\n"
        '- <picture><img alt="High severity" width="62"></picture> [Guard arr[i] access](#discussion_r111) · New\n'
        '- <picture><img alt="Low severity" width="62"></picture> [Add [Authorize] to route](#discussion_r222) · New\n'
    )
    parsed = parse_copilot_overview(body)

    assert parsed["unlinked_findings"] == 0
    assert parsed["findings"] == [
        {"severity": "high", "discussion_id": "111", "title": "Guard arr[i] access"},
        {"severity": "low", "discussion_id": "222", "title": "Add [Authorize] to route"},
    ]


def test_a_head_overview_older_than_the_newest_request_does_not_cover_the_head():
    """Copilot reviewed the head clean, then was asked again without a push.
    The old overview must not clear the gate while the new review is
    pending (PR #116 review)."""
    pr = _pr(_review(CLEAN_OVERVIEW, "2026-10-07T16:00:00Z"))
    pr["timelineItems"] = {"nodes": [{
        "__typename": "ReviewRequestedEvent", "createdAt": "2026-10-07T16:30:00Z",
        "actor": {"login": "agent"},
        "requestedReviewer": {"__typename": "Bot", "login": COPILOT},
    }]}

    overview = latest_reviewer_overview(pr, COPILOT)

    assert overview["covers_head"] is False
    assert clean_blocked_by([], overview, review_request_vendor=True) == "no_review_for_head"


def test_absolute_discussion_links_are_parsed_too():
    """Live Copilot overview, qizsecurity/local-dev#97 (2026-10-16): the
    finding links are absolute PR URLs, not bare `#discussion_r` fragments.
    Fragment-only matching left every finding unlinked and ungraded."""
    body = (
        "<!-- ccr-overview-v2 -->\n\n### 🟡 Changes recommended\n\n"
        "<summary><strong>2 open findings</strong></summary>\n\n"
        '- <picture><img alt="High severity" width="62"></picture> '
        "[Build and push research-tool with the session image tag]"
        "(https://github.com/qizsecurity/local-dev/pull/97#discussion_r4298740956) · New\n"
        '- <picture><img alt="Medium severity" width="62"></picture> '
        "[Use portable sed output redirection](#discussion_r4298740999) · New\n"
    )
    parsed = parse_copilot_overview(body)

    assert parsed["unlinked_findings"] == 0
    assert [(f["severity"], f["discussion_id"]) for f in parsed["findings"]] == [
        ("high", "4298740956"), ("medium", "4298740999"),
    ]
