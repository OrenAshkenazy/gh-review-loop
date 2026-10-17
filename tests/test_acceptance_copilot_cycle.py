"""Acceptance test: one full Copilot review cycle over recorded PR payloads.

Copilot differs from Codex in three ways this test pins: it is re-reviewed by
a review request (REST), not a comment; the cap counts ReviewRequestedEvents
the agent caused after the first completed review, not mentions; and
severities live in the review overview, not in the inline comments. The wait
is anchored on the head commit. Only the `gh` boundary is stubbed.
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
from fetch_gemini_threads import PullRequest, rereview_requests, thread_severity


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "copilot_cycle.json").read_text(encoding="utf-8")
)
PR = PullRequest(owner="acme", repo="widget", number=159)
AGENT = "loop-agent"
COPILOT = "copilot-pull-request-reviewer"
COPILOT_REQUEST = "copilot-pull-request-reviewer[bot]"
REREVIEW_AT = "2026-10-07T16:20:05Z"
OLD_HEAD = "f4a55e30000000000000000000000000000000f4"
NEW_HEAD = "d7efa4900000000000000000000000000000000d7"


@pytest.fixture(autouse=True)
def _cap_check_is_inert(monkeypatch):
    stub_cap_check_known_under_cap(monkeypatch)


@pytest.fixture
def loop(tmp_path, monkeypatch):
    """The loop with only the gh boundary replaced."""
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(fgt, "resolve_pr", lambda arg: PR)
    monkeypatch.setattr(fgt, "gh_authenticated_login", lambda: AGENT)
    monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _seconds: None)

    state = {"payload": FIXTURE["initial"]}
    monkeypatch.setattr(fgt, "fetch_threads", lambda pr: state["payload"])
    monkeypatch.setattr(
        fgt,
        "fetch_reviewer_discovery",
        lambda pr, **kwargs: {"pull_request": state["payload"], "partial": False, "warnings": []},
    )
    return state


def run_fetch(monkeypatch, capsys, *extra_args):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fetch_gemini_threads.py",
            "--pr", "https://github.com/acme/widget/pull/159",
            "--format", "json",
            "--no-resolve-outdated",
            "--no-resolve-addressed-by-reply",
            *extra_args,
        ],
    )
    assert fgt.main() == 0
    return json.loads(capsys.readouterr().out)


def list_reviewers(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv",
        ["fetch_gemini_threads.py", "--pr", "https://github.com/acme/widget/pull/159",
         "--list-reviewers", "--format", "json"],
    )
    assert fgt.main() == 0
    return json.loads(capsys.readouterr().out)["reviewers"]


def test_the_free_first_request_prints_no_cap_warning(loop, monkeypatch, capsys):
    """Before Copilot's first completed review the next request is the free
    cycle 0. `limitReached` already said so; the stderr warning still used a
    raw `used >= cap` and told a cap-0 user the cap was reached before the
    initial review (PR #116 review)."""
    loop["payload"] = FIXTURE["requested_only"]
    monkeypatch.setattr(
        sys, "argv",
        ["fetch_gemini_threads.py", "--pr", "https://github.com/acme/widget/pull/159",
         "--format", "json", "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
         "--reviewer", COPILOT, "--max-rereview-requests", "0"],
    )
    assert fgt.main() == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["loopStatus"]["limitReached"] is False
    assert "configured loop cap" not in err


def test_a_requested_but_silent_copilot_is_discovered(loop, monkeypatch, capsys):
    loop["payload"] = FIXTURE["requested_only"]

    reviewers = list_reviewers(monkeypatch, capsys)

    assert [(r["login"], r["source"]) for r in reviewers] == [(COPILOT, "requested")]
    assert reviewers[0]["display_name"] == "Copilot"
    assert reviewers[0]["trigger_kind"] == "review_request"
    assert reviewers[0]["review_trigger"] is None


def test_a_copilot_that_left_findings_is_discovered_from_its_threads(loop, monkeypatch, capsys):
    reviewers = list_reviewers(monkeypatch, capsys)

    assert [(r["login"], r["source"]) for r in reviewers] == [(COPILOT, "thread")]


def test_full_copilot_cycle_fetches_requests_waits_and_rechecks(loop, monkeypatch, capsys):
    # 1. Select Copilot and fetch its first review. The human's click that
    #    started the review is not an agent re-review request.
    first = run_fetch(monkeypatch, capsys, "--reviewer", COPILOT, "--reviewer-source", "confirmed")

    selection = first["reviewerSelection"]
    assert selection["review_trigger"] is None
    assert selection["trigger_kind"] == "review_request"
    assert selection["request_login"] == COPILOT_REQUEST
    assert selection["auto_reviews"] is False
    assert [t["path"] for t in first["threads"]] == ["src/provider_test.js", "src/mapping_test.js"]
    assert [thread_severity(t) for t in first["threads"]] == ["high", "medium"]
    assert [t["reviewerSeverity"] for t in first["threads"]] == ["high", "medium"]
    assert first["threads"][0]["reviewerSeveritySource"] == {
        "review_url": "https://github.com/acme/widget/pull/159#pullrequestreview-1",
        "commit": OLD_HEAD,
    }
    assert first["reviewerOverview"] == {
        "assessment": "Changes recommended",
        "approving": False,
        "open_findings": 2,
        "complete": True,
        "covers_head": True,
        "commit": OLD_HEAD,
        "submitted_at": "2026-10-07T15:46:14Z",
        "url": "https://github.com/acme/widget/pull/159#pullrequestreview-1",
        "finding_ids": ["4208919854", "4208919952"],
        "unlinked_findings": 0,
        "warnings": [],
    }
    assert first["loopStatus"]["reReviewRequests"] == 0
    assert first["loopStatus"]["reReviewCountComplete"] is True
    assert first["loopStatus"]["cleanBlockedBy"] is None
    assert first["loopStatus"]["severityCounts"] == {"high": 1, "medium": 1}

    # 2. Ask Copilot to review the pushed fixes, from the persisted selection
    #    alone: a review request, never a comment. The state read is stubbed
    #    at the gh boundary with the ABSENT shape the GraphQL query yields.
    monkeypatch.setattr(
        request_rereview, "post_rereview",
        lambda *a, **k: pytest.fail("Copilot must not be pinged with a comment"),
    )
    monkeypatch.setattr(
        request_rereview, "request_state",
        lambda *a, **k: {"state": "ABSENT", "head": NEW_HEAD, "pending_since": None,
                         "first_completed_review_at": "2026-10-07T15:46:14Z"},
    )
    monkeypatch.setattr(request_rereview, "count_agent_review_requests", lambda *a, **k: 0)
    monkeypatch.setattr(request_rereview, "adopt_unlogged_requests", lambda *a, **k: True)
    monkeypatch.setattr(request_rereview, "timeline_snapshot", lambda *a, **k: [])
    requested = {}

    def fake_request(repo, pr, request_login, **kwargs):
        requested["login"] = request_login
        return {"created_at": REREVIEW_AT, "repo": repo, "pr": pr,
                "kind": "review_request", "reviewer": request_login, "posted": True, "status": "posted"}

    monkeypatch.setattr(request_rereview, "request_review", fake_request)
    monkeypatch.setattr(request_rereview, "request_registered", lambda *a, **k: True)
    assert request_rereview.main(["--repo", "acme/widget", "--pr", "159", "--json"]) == 0
    posted = json.loads(capsys.readouterr().out)

    assert requested["login"] == COPILOT_REQUEST
    assert posted["kind"] == "review_request"
    assert posted["status"] == "posted"
    assert posted["created_at"] == REREVIEW_AT
    assert posted["state"] == "ABSENT"

    # 3. Wait. While the request is pending the old review (previous head)
    #    must not end the wait; the review on the new head does, even though
    #    it was submitted in the same second as the request.
    seen = {"n": 0}

    def payload_sequence(pr):
        seen["n"] += 1
        return FIXTURE["pending_rereview"] if seen["n"] < 3 else FIXTURE["after_rereview"]

    monkeypatch.setattr(fgt, "fetch_threads", payload_sequence)
    settled = fgt.wait_for_stable_review(
        PR, author=COPILOT, timeout_seconds=60, interval_seconds=1,
        quiet_seconds=0, after_iso=REREVIEW_AT,
    )

    assert seen["n"] >= 3
    assert settled["reviews"]["nodes"][-1]["commit"]["oid"] == NEW_HEAD

    # 4. Re-fetch: only the new finding is current, graded from the new
    #    overview; the agent's request is now one consumed cap slot.
    loop["payload"] = FIXTURE["after_rereview"]
    monkeypatch.setattr(fgt, "fetch_threads", lambda pr: FIXTURE["after_rereview"])
    second = run_fetch(monkeypatch, capsys)

    assert second["reviewerSelection"]["login"] == COPILOT
    assert [t["id"] for t in second["threads"]] == ["PRRT_t3"]
    assert thread_severity(second["threads"][0]) == "low"
    assert second["threads"][0]["reviewerSeveritySource"]["commit"] == NEW_HEAD
    assert second["reviewerOverview"]["open_findings"] == 1
    assert second["reviewerOverview"]["covers_head"] is True
    assert second["loopStatus"]["reReviewRequests"] == 1
    assert second["loopStatus"]["limitReached"] is False


def test_a_clean_copilot_review_on_the_head_is_clean(loop, monkeypatch, capsys):
    loop["payload"] = FIXTURE["clean"]

    result = run_fetch(monkeypatch, capsys, "--reviewer", COPILOT, "--reviewer-source", "confirmed")

    assert result["threads"] == []
    assert result["reviewerOverview"]["approving"] is True
    assert result["reviewerOverview"]["open_findings"] == 0
    assert result["reviewerOverview"]["covers_head"] is True
    assert result["loopStatus"]["cleanBlockedBy"] is None


def test_an_approving_overview_for_a_stale_commit_does_not_make_the_pr_clean(loop, monkeypatch, capsys):
    loop["payload"] = FIXTURE["clean_stale"]

    result = run_fetch(monkeypatch, capsys, "--reviewer", COPILOT, "--reviewer-source", "confirmed")

    assert result["threads"] == []
    assert result["reviewerOverview"]["approving"] is True
    assert result["reviewerOverview"]["covers_head"] is False
    assert result["loopStatus"]["cleanBlockedBy"] == "no_review_for_head"


def test_an_unparseable_overview_keeps_threads_listed_as_unknown(loop, monkeypatch, capsys):
    loop["payload"] = FIXTURE["unparseable"]

    result = run_fetch(monkeypatch, capsys, "--reviewer", COPILOT, "--reviewer-source", "confirmed")

    assert [t["id"] for t in result["threads"]] == ["PRRT_t1", "PRRT_t2"]
    assert [thread_severity(t) for t in result["threads"]] == ["unknown", "unknown"]
    assert all("reviewerSeverity" not in t for t in result["threads"])
    assert result["reviewerOverview"]["complete"] is False
    assert result["reviewerOverview"]["open_findings"] is None
    # Threads exist, so the overview gate does not apply; the threads drive.
    assert result["loopStatus"]["cleanBlockedBy"] is None


def test_an_unparseable_overview_with_no_threads_blocks_clean(loop, monkeypatch, capsys):
    payload = copy.deepcopy(FIXTURE["unparseable"])
    payload["reviewThreads"] = {"nodes": []}
    loop["payload"] = payload

    result = run_fetch(monkeypatch, capsys, "--reviewer", COPILOT, "--reviewer-source", "confirmed")

    assert result["threads"] == []
    assert result["loopStatus"]["cleanBlockedBy"] == "overview_unparseable"


def test_every_finding_dispositioned_without_a_push_is_clean(loop, monkeypatch, capsys):
    # The agent (or a human in the UI) resolved both of Copilot's threads as
    # won't-fix. No push, so the head overview still says 2 open findings.
    # Thread state is live; the overview is a snapshot. Clean, like Gemini.
    payload = copy.deepcopy(FIXTURE["initial"])
    for thread in payload["reviewThreads"]["nodes"]:
        thread["isResolved"] = True
    loop["payload"] = payload

    result = run_fetch(monkeypatch, capsys, "--reviewer", COPILOT, "--reviewer-source", "confirmed")

    assert result["threads"] == []
    assert result["reviewerOverview"]["open_findings"] == 2
    assert result["reviewerOverview"]["covers_head"] is True
    assert result["loopStatus"]["cleanBlockedBy"] is None


def test_a_finding_the_fetch_cannot_see_blocks_clean(loop, monkeypatch, capsys):
    payload = copy.deepcopy(FIXTURE["initial"])
    payload["reviewThreads"]["nodes"][0]["isResolved"] = True
    del payload["reviewThreads"]["nodes"][1]
    loop["payload"] = payload

    result = run_fetch(monkeypatch, capsys, "--reviewer", COPILOT, "--reviewer-source", "confirmed")

    assert result["threads"] == []
    assert result["loopStatus"]["cleanBlockedBy"] == "overview_reports_open_findings"


def test_a_head_review_without_a_readable_overview_blocks_as_unparseable(loop, monkeypatch, capsys):
    payload = copy.deepcopy(FIXTURE["clean"])
    payload["reviews"]["nodes"][0]["body"] = "Looks good to me."
    loop["payload"] = payload

    result = run_fetch(monkeypatch, capsys, "--reviewer", COPILOT, "--reviewer-source", "confirmed")

    assert result["reviewerOverview"]["covers_head"] is True
    assert result["reviewerOverview"]["complete"] is False
    assert result["loopStatus"]["cleanBlockedBy"] == "overview_unparseable"


def test_gemini_loop_status_never_claims_a_lower_bound_count(loop, monkeypatch, capsys):
    payload = copy.deepcopy(FIXTURE["after_rereview"])
    payload["timelineItems"]["nodes"] = payload["timelineItems"]["nodes"] * 50
    loop["payload"] = payload

    result = run_fetch(monkeypatch, capsys, "--reviewer", "gemini-code-assist", "--reviewer-source", "confirmed")

    assert result["loopStatus"]["reReviewCountComplete"] is True
    assert not any("review-request events" in w for w in result.get("warnings", []))


def test_only_the_agents_post_review_requests_count(loop):
    by_agent = rereview_requests(
        FIXTURE["after_rereview"], AGENT, review_trigger_mention=None, reviewer_login=COPILOT
    )
    by_anyone = rereview_requests(
        FIXTURE["after_rereview"], None, review_trigger_mention=None, reviewer_login=COPILOT
    )

    # The human's request predates the first completed review: initial, free.
    assert len(by_agent) == 1
    assert len(by_anyone) == 1

    with_human_rerequest = copy.deepcopy(FIXTURE["after_rereview"])
    with_human_rerequest["timelineItems"]["nodes"].append({
        "__typename": "ReviewRequestedEvent", "createdAt": "2026-10-07T16:30:00Z",
        "actor": {"login": "a-human"},
        "requestedReviewer": {"__typename": "Bot", "login": COPILOT},
    })
    assert len(rereview_requests(with_human_rerequest, AGENT, review_trigger_mention=None, reviewer_login=COPILOT)) == 1
    assert len(rereview_requests(with_human_rerequest, None, review_trigger_mention=None, reviewer_login=COPILOT)) == 2


def test_human_mode_prints_the_overview_line(loop, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv",
        ["fetch_gemini_threads.py", "--pr", "https://github.com/acme/widget/pull/159",
         "--reviewer", COPILOT, "--reviewer-source", "confirmed",
         "--no-resolve-outdated", "--no-resolve-addressed-by-reply", "--no-color"],
    )

    assert fgt.main() == 0

    out = capsys.readouterr().out
    assert "[loop] reviewer overview: Changes recommended — 2 open findings (covers head)" in out
    assert "[high]" in out and "[medium]" in out
    assert "not clean" not in out


def test_human_mode_names_the_clean_blocker(loop, monkeypatch, capsys):
    loop["payload"] = FIXTURE["clean_stale"]
    monkeypatch.setattr(
        sys, "argv",
        ["fetch_gemini_threads.py", "--pr", "https://github.com/acme/widget/pull/159",
         "--reviewer", COPILOT, "--reviewer-source", "confirmed",
         "--no-resolve-outdated", "--no-resolve-addressed-by-reply", "--no-color"],
    )

    assert fgt.main() == 0

    out = capsys.readouterr().out
    assert "[loop] not clean: no_review_for_head" in out
    assert "(stale: f4a55e3)" in out


def test_copilot_vendor_facts_used_by_this_cycle():
    assert review_vendors.COPILOT.request_login == COPILOT_REQUEST
    assert review_vendors.vendor_for("Copilot") is review_vendors.COPILOT
