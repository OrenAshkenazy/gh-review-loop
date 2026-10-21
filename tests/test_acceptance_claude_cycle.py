"""Acceptance test: one full Claude Code review cycle over recorded PR payloads.

Claude is a comment vendor like Codex (``@claude review``), but its reviews
name the head commit they cover, like Copilot's, so the clean verdict waits
for a review of the current head; and its verdict can be a top-level PR
comment — the claude-code-action's "No issues found" note, or a CI job that
posts Claude's whole review summary as ``github-actions[bot]``. Only the
``gh`` boundary is stubbed.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

import fetch_gemini_threads as fgt
import request_rereview
import review_vendors
from conftest import stub_cap_check_known_under_cap
from fetch_gemini_threads import PullRequest, thread_severity


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "claude_cycle.json").read_text(encoding="utf-8")
)
PR = PullRequest(owner="acme", repo="widget", number=159)
CI_PR = PullRequest(owner="acme", repo="widget", number=2885)
AGENT = "loop-agent"
CLAUDE = "claude"
REREVIEW_AT = "2026-10-20T18:38:30Z"
OLD_HEAD = "ff99634a0000000000000000000000000000ff99"
NEW_HEAD = "e5d1c2c00000000000000000000000000000e5d1"


@pytest.fixture(autouse=True)
def _cap_check_is_inert(monkeypatch):
    stub_cap_check_known_under_cap(monkeypatch)


@pytest.fixture
def loop(tmp_path, monkeypatch):
    """The loop with only the gh boundary replaced."""
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(fgt, "gh_authenticated_login", lambda: AGENT)
    monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _seconds: None)

    state = {"payload": FIXTURE["initial"], "pr": PR}
    monkeypatch.setattr(fgt, "resolve_pr", lambda arg: state["pr"])
    monkeypatch.setattr(fgt, "fetch_threads", lambda pr: state["payload"])
    monkeypatch.setattr(
        fgt,
        "fetch_reviewer_discovery",
        lambda pr, **kwargs: {"pull_request": state["payload"], "partial": False, "warnings": []},
    )
    return state


def run_fetch(monkeypatch, capsys, *extra_args, pr=PR):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fetch_gemini_threads.py",
            "--pr", pr.url,
            "--format", "json",
            "--no-resolve-outdated",
            "--no-resolve-addressed-by-reply",
            *extra_args,
        ],
    )
    assert fgt.main() == 0
    return json.loads(capsys.readouterr().out)


def list_reviewers(monkeypatch, capsys, pr=PR):
    monkeypatch.setattr(
        sys, "argv",
        ["fetch_gemini_threads.py", "--pr", pr.url, "--list-reviewers", "--format", "json"],
    )
    assert fgt.main() == 0
    return json.loads(capsys.readouterr().out)["reviewers"]


def test_claude_is_discovered_from_its_inline_findings(loop, monkeypatch, capsys):
    reviewers = list_reviewers(monkeypatch, capsys)

    assert [(r["login"], r["source"], r["display_name"], r["review_trigger"]) for r in reviewers] == [
        (CLAUDE, "thread", "Claude Code", "@claude"),
    ]


def test_full_claude_cycle_fetches_pings_waits_and_rechecks(loop, monkeypatch, capsys):
    # 1. Select Claude and fetch its first review: two inline findings, graded
    #    by their leading marker; the review covers the head.
    first = run_fetch(monkeypatch, capsys, "--reviewer", "claude[bot]", "--reviewer-source", "confirmed")

    selection = first["reviewerSelection"]
    assert selection["login"] == CLAUDE
    assert selection["display_name"] == "Claude Code"
    assert selection["review_trigger"] == "@claude"
    assert selection["trigger_kind"] == "comment"
    assert selection["auto_reviews"] is False
    assert "vendor" not in selection
    assert [t["path"] for t in first["threads"]] == [".github/workflows/build-and-publish.yml", "CLAUDE.md"]
    assert [thread_severity(t) for t in first["threads"]] == ["high", "low"]
    assert first["reviewerOverview"]["covers_head"] is True
    assert first["reviewerOverview"]["commit"] == OLD_HEAD
    assert first["reviewerOverview"]["complete"] is True
    assert first["reviewerOverview"]["open_findings"] == 0
    assert first["reviewerOverview"]["approving"] is False
    assert first["loopStatus"]["cleanBlockedBy"] is None
    assert first["loopStatus"]["reReviewRequests"] == 0
    assert first["loopStatus"]["severityCounts"] == {"high": 1, "low": 1}

    # 2. Ask Claude to review the pushed fixes, from the persisted selection
    #    alone: the hosted service's exact comment command. The head is not
    #    yet reviewed, so the ping is posted.
    monkeypatch.setattr(request_rereview, "current_head", lambda *a, **k: NEW_HEAD)
    monkeypatch.setattr(
        request_rereview, "_all_reviews",
        lambda *a, **k: [
            {"user": {"login": "claude[bot]"}, "state": "COMMENTED", "commit_id": OLD_HEAD,
             "submitted_at": "2026-10-20T18:35:06Z"},
        ],
    )
    posted = {}

    def fake_post(repo, pr, phrase, **kwargs):
        posted["phrase"] = phrase
        return {"created_at": REREVIEW_AT, "repo": repo, "pr": pr, "phrase": phrase, "posted": True, "status": "posted"}

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)
    assert request_rereview.main([
        "--repo", "acme/widget", "--pr", "159", "--expect-head", NEW_HEAD, "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert posted["phrase"] == "@claude review"
    assert payload["created_at"] == REREVIEW_AT

    # 3. Wait, anchored on the ping. The first review predates it and must
    #    not end the wait; the review on the new head does.
    seen = {"n": 0}

    def payload_sequence(pr):
        seen["n"] += 1
        return FIXTURE["initial"] if seen["n"] < 3 else FIXTURE["after_rereview"]

    monkeypatch.setattr(fgt, "fetch_threads", payload_sequence)
    settled = fgt.wait_for_stable_review(
        PR, author=CLAUDE, timeout_seconds=60, interval_seconds=1,
        quiet_seconds=0, after_iso=REREVIEW_AT,
    )

    assert seen["n"] >= 3
    assert settled["reviews"]["nodes"][-1]["commit"]["oid"] == NEW_HEAD

    # 4. Re-fetch: the fixed finding is outdated and gone, the nit Claude
    #    re-listed is still open and graded by the open-items block; the
    #    agent's ping is one consumed cap slot.
    loop["payload"] = FIXTURE["after_rereview"]
    monkeypatch.setattr(fgt, "fetch_threads", lambda pr: FIXTURE["after_rereview"])
    second = run_fetch(monkeypatch, capsys)

    assert second["reviewerSelection"]["login"] == CLAUDE
    assert [t["id"] for t in second["threads"]] == ["PRRT_t2"]
    assert thread_severity(second["threads"][0]) == "low"
    assert second["threads"][0]["reviewerSeveritySource"]["commit"] == NEW_HEAD
    assert second["reviewerOverview"]["covers_head"] is True
    assert second["reviewerOverview"]["open_findings"] == 1
    assert second["reviewerOverview"]["finding_ids"] == ["4328761976"]
    assert second["reviewerOverview"]["approving"] is True
    assert second["loopStatus"]["reReviewRequests"] == 1
    assert second["loopStatus"]["limitReached"] is False
    assert second["loopStatus"]["cleanBlockedBy"] is None


def test_a_head_claude_already_reviewed_is_fetched_not_pinged(loop, monkeypatch, capsys):
    run_fetch(monkeypatch, capsys, "--reviewer", CLAUDE, "--reviewer-source", "confirmed")
    monkeypatch.setattr(request_rereview, "current_head", lambda *a, **k: NEW_HEAD)
    monkeypatch.setattr(
        request_rereview, "_all_reviews",
        lambda *a, **k: [
            {"user": {"login": "claude[bot]"}, "state": "COMMENTED", "commit_id": NEW_HEAD,
             "submitted_at": "2026-10-20T18:41:00Z"},
        ],
    )
    monkeypatch.setattr(
        request_rereview, "post_rereview",
        lambda *a, **k: pytest.fail("a repository reviewing every push already answered"),
    )

    assert request_rereview.main(["--repo", "acme/widget", "--pr", "159", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_reviewed_head"


def test_a_clean_claude_review_on_the_head_is_clean(loop, monkeypatch, capsys):
    loop["payload"] = FIXTURE["clean"]

    result = run_fetch(monkeypatch, capsys, "--reviewer", CLAUDE, "--reviewer-source", "confirmed")

    assert result["threads"] == []
    assert result["reviewerOverview"]["approving"] is True
    assert result["reviewerOverview"]["assessment"] == "Nothing blocking."
    assert result["reviewerOverview"]["open_findings"] == 0
    assert result["reviewerOverview"]["covers_head"] is True
    assert result["loopStatus"]["cleanBlockedBy"] is None


def test_a_clean_review_of_a_stale_commit_does_not_make_the_pr_clean(loop, monkeypatch, capsys):
    loop["payload"] = FIXTURE["clean_stale"]

    result = run_fetch(monkeypatch, capsys, "--reviewer", CLAUDE, "--reviewer-source", "confirmed")

    assert result["threads"] == []
    assert result["reviewerOverview"]["approving"] is True
    assert result["reviewerOverview"]["covers_head"] is False
    assert result["loopStatus"]["cleanBlockedBy"] == "no_review_for_head"


def test_a_finding_claude_still_lists_that_the_fetch_cannot_see_blocks_clean(loop, monkeypatch, capsys):
    payload = copy.deepcopy(FIXTURE["after_rereview"])
    del payload["reviewThreads"]["nodes"][1]
    loop["payload"] = payload

    result = run_fetch(monkeypatch, capsys, "--reviewer", CLAUDE, "--reviewer-source", "confirmed")

    assert result["threads"] == []
    assert result["loopStatus"]["cleanBlockedBy"] == "overview_reports_open_findings"


def test_every_finding_dispositioned_without_a_push_is_clean(loop, monkeypatch, capsys):
    payload = copy.deepcopy(FIXTURE["after_rereview"])
    payload["reviewThreads"]["nodes"][1]["isResolved"] = True
    loop["payload"] = payload

    result = run_fetch(monkeypatch, capsys, "--reviewer", CLAUDE, "--reviewer-source", "confirmed")

    assert result["threads"] == []
    assert result["reviewerOverview"]["open_findings"] == 1
    assert result["loopStatus"]["cleanBlockedBy"] is None


def test_the_actions_clean_note_after_its_inline_findings_clears_the_pr(loop, monkeypatch, capsys):
    """claude-code-action with the code-review skill: each inline finding is
    a body-less review of the old head; after the fix push it posts one
    "No issues found" comment and no review at all."""
    loop["payload"] = FIXTURE["action_clean_note"]

    waited = fgt.wait_for_stable_review(
        PR, author=CLAUDE, timeout_seconds=60, interval_seconds=1,
        quiet_seconds=0, after_iso=REREVIEW_AT,
    )
    assert waited is FIXTURE["action_clean_note"]

    result = run_fetch(monkeypatch, capsys, "--reviewer", CLAUDE, "--reviewer-source", "confirmed")

    assert result["threads"] == []
    assert result["reviewerOverview"]["source"] == "comment"
    assert result["reviewerOverview"]["approving"] is True
    assert result["reviewerOverview"]["open_findings"] == 0
    assert result["reviewerOverview"]["covers_head"] is True
    assert result["loopStatus"]["cleanBlockedBy"] is None


def test_the_tracking_comment_alone_keeps_the_wait_going(loop, monkeypatch):
    payload = copy.deepcopy(FIXTURE["action_clean_note"])
    payload["comments"]["nodes"] = payload["comments"]["nodes"][:2]
    monkeypatch.setattr(fgt, "fetch_threads", lambda pr: payload)

    with pytest.raises(fgt.WaitTimedOut):
        fgt.wait_for_stable_review(
            PR, author=CLAUDE, timeout_seconds=2, interval_seconds=1,
            quiet_seconds=0, after_iso=REREVIEW_AT,
        )


# --- a CI job posting Claude's review summary as github-actions[bot] ----------


def test_the_ci_summary_login_is_discovered_bound_to_claude(loop, monkeypatch, capsys):
    loop["payload"] = FIXTURE["ci_summary"]
    loop["pr"] = CI_PR

    reviewers = list_reviewers(monkeypatch, capsys, pr=CI_PR)

    assert reviewers == [{
        "login": "github-actions",
        "display_name": "Claude Code (as github-actions)",
        "review_trigger": "@claude",
        "trigger_kind": "comment",
        "source": "comment",
        "vendor": "claude",
    }]


def test_a_ci_summary_cycle_surfaces_its_findings_then_clears(loop, monkeypatch, capsys):
    loop["payload"] = FIXTURE["ci_summary"]
    loop["pr"] = CI_PR

    # 1. Select the job's login bound to Claude: the summary's one finding is
    #    the actionable thread, graded by the summary.
    first = run_fetch(
        monkeypatch, capsys, "--reviewer", "github-actions", "--reviewer-vendor", "claude",
        "--reviewer-source", "confirmed", pr=CI_PR,
    )

    selection = first["reviewerSelection"]
    assert selection["login"] == "github-actions"
    assert selection["vendor"] == "claude"
    assert selection["display_name"] == "Claude Code (as github-actions)"
    assert selection["review_trigger"] == "@claude"
    assert [(t["path"], t["line"]) for t in first["threads"]] == [
        ("wasm/modules/crowdstrike/resume_durable_test.go", 19),
    ]
    assert first["threads"][0]["isSummaryCommentFinding"] is True
    assert thread_severity(first["threads"][0]) == "low"
    assert first["reviewerOverview"]["source"] == "comment"
    assert first["reviewerOverview"]["open_findings"] == 1
    assert first["reviewerOverview"]["covers_head"] is True
    assert first["loopStatus"]["cleanBlockedBy"] is None
    assert first["loopStatus"]["severityCounts"] == {"low": 1}

    # 2. A later run restores the binding from the persisted record.
    again = run_fetch(monkeypatch, capsys, pr=CI_PR)
    assert again["reviewerSelection"]["vendor"] == "claude"
    assert again["reviewerSelection"]["review_trigger"] == "@claude"
    assert len(again["threads"]) == 1

    # 3. Ping, wait for the job's new summary, re-fetch: clean.
    monkeypatch.setattr(request_rereview, "head_already_reviewed", lambda *a, **k: None)
    posted = {}
    monkeypatch.setattr(request_rereview, "post_rereview", lambda repo, pr, phrase, **k: (
        posted.update(phrase=phrase)
        or {"created_at": "2026-10-21T07:39:00Z", "repo": repo, "pr": pr, "phrase": phrase, "posted": True}
    ))
    assert request_rereview.main(["--repo", "acme/widget", "--pr", "2885", "--json"]) == 0
    capsys.readouterr()
    assert posted["phrase"] == "@claude review"

    seen = {"n": 0}

    def payload_sequence(pr):
        seen["n"] += 1
        return FIXTURE["ci_summary"] if seen["n"] < 2 else FIXTURE["ci_summary_clean"]

    monkeypatch.setattr(fgt, "fetch_threads", payload_sequence)
    settled = fgt.wait_for_stable_review(
        CI_PR, author="github-actions", timeout_seconds=60, interval_seconds=1,
        quiet_seconds=0, after_iso="2026-10-21T07:39:00Z",
    )
    assert settled["comments"]["nodes"][-1]["id"] == "IC_review2"

    loop["payload"] = FIXTURE["ci_summary_clean"]
    monkeypatch.setattr(fgt, "fetch_threads", lambda pr: FIXTURE["ci_summary_clean"])
    final = run_fetch(monkeypatch, capsys, pr=CI_PR)

    assert final["threads"] == []
    assert final["reviewerOverview"]["approving"] is True
    assert final["reviewerOverview"]["open_findings"] == 0
    assert final["reviewerOverview"]["covers_head"] is True
    assert final["loopStatus"]["cleanBlockedBy"] is None
    assert final["loopStatus"]["reReviewRequests"] == 1


def test_a_summary_finding_filtered_by_severity_does_not_block_clean(loop, monkeypatch, capsys):
    loop["payload"] = FIXTURE["ci_summary"]
    loop["pr"] = CI_PR

    result = run_fetch(
        monkeypatch, capsys, "--reviewer", "github-actions", "--reviewer-vendor", "claude",
        "--min-severity", "high", pr=CI_PR,
    )

    assert result["threads"] == []
    assert result["loopStatus"]["cleanBlockedBy"] is None


def test_binding_codexs_own_login_to_claude_is_refused(loop, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv",
        ["fetch_gemini_threads.py", "--pr", PR.url, "--format", "json",
         "--reviewer", "chatgpt-codex-connector", "--reviewer-vendor", "claude"],
    )

    assert fgt.main() == 2
    assert "cannot be bound" in capsys.readouterr().err
    assert review_vendors.vendor_for("chatgpt-codex-connector") is review_vendors.CODEX
