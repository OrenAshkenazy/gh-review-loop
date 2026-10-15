"""Comment capacity agrees across the collector and publication gate."""
import json
import subprocess
import sys

import pytest

import fetch_gemini_threads as fgt
import loop_state
import request_rereview as rr


REVIEWER = "chatgpt-codex-connector"
PR = fgt.PullRequest(owner="owner", repo="repo", number=1)
AT = "2026-10-15T09:00:00Z"


@pytest.mark.parametrize("observed,receipt_ids,expected", [
    ([], [100], 1),
    ([100], [100], 1),
    ([101], [100], 2),
    ([100], [None], 2),
    ([6129660565], [6129660565], 1),
    ([], [100, 100], 1),
    ([100, 100], [100], 1),
])
def test_comment_capacity_unions_database_ids_in_both_paths(observed, receipt_ids, expected):
    receipts = [{"reviewer": REVIEWER, "at": AT, **({"comment_id": value} if value is not None else {})} for value in receipt_ids]
    receipts.append({"reviewer": "coderabbitai", "at": AT, "comment_id": 200})
    loop_state.save_sticky_state({"Owner/Repo#1": {"review_requests": receipts}})
    rest = [{"id": value, "user": {"login": "agent"}, "body": "@codex review"} for value in observed]
    graphql = [{"id": "opaque-node", "fullDatabaseId": str(value), "author": {"login": "agent"}, "body": "@codex review"} for value in observed]
    rest.append({"id": 300, "user": {"login": "human"}, "body": "@codex review"})
    graphql.append({"id": "human-node", "fullDatabaseId": "300", "author": {"login": "human"}, "body": "@codex review"})
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps(rest), "")

    enforced = rr.count_agent_pings("owner/repo", 1, "@codex", "agent", runner=runner)
    shown = fgt.rereview_requests({"comments": {"nodes": graphql}}, "agent", review_trigger_mention="@codex",
                                reviewer_login=REVIEWER, comment_receipts=fgt.loop_comment_receipts(PR))
    assert enforced == expected and len(shown) == expected


@pytest.mark.parametrize("flag", ["review_request_in_flight", "review_requests_incomplete"])
def test_unknown_comment_history_blocks_enforcement_and_marks_display_incomplete(flag):
    loop_state.save_sticky_state({"owner/repo#1": {flag: True}})
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    assert rr.count_agent_pings("owner/repo", 1, "@codex", "agent", runner=runner) is None
    receipts = fgt.loop_comment_receipts(PR)
    assert receipts is None
    assert not fgt.review_request_count_complete({"comments": {"nodes": []}}, review_request_vendor=False, comment_receipts=receipts)


def test_a_full_comment_page_is_an_incomplete_count():
    pr = {"comments": {"nodes": [{}] * fgt.PAGE_LIMIT_PR_COMMENTS}}
    assert not fgt.review_request_count_complete(pr, review_request_vendor=False, comment_receipts=[])
    warnings = fgt.pagination_warnings(pr, review_request_vendor=False)
    assert any("PR comments hit page limit" in warning for warning in warnings)
    assert not any("review-request events" in warning for warning in warnings)


@pytest.mark.parametrize("output_format", ["json", "markdown"])
@pytest.mark.parametrize("flag", [None, "review_request_in_flight", "review_requests_incomplete"])
def test_main_reports_the_same_comment_capacity(monkeypatch, capsys, output_format, flag):
    entry = {"review_requests": [{"reviewer": REVIEWER, "at": AT, "comment_id": 100}]}
    if flag:
        entry[flag] = True
    loop_state.save_sticky_state({"owner/repo#1": entry})
    payload = {"number": 1, "url": "https://github.com/owner/repo/pull/1", "reviews": {"nodes": []},
               "reviewThreads": {"nodes": []}, "comments": {"nodes": [
                   {"id": "human-node", "fullDatabaseId": "300", "author": {"login": "human"}, "body": "@codex review"},
               ]}}
    monkeypatch.setattr(fgt, "resolve_pr", lambda *a: PR)
    monkeypatch.setattr(fgt, "fetch_threads", lambda *a: payload)
    monkeypatch.setattr(fgt, "gh_authenticated_login", lambda: "agent")
    monkeypatch.setattr(fgt, "repo_root", lambda: None)
    monkeypatch.setattr(sys, "argv", ["fetch_gemini_threads.py", "--pr", payload["url"], "--reviewer", REVIEWER,
                                     "--reviewer-source", "confirmed", "--review-trigger-mention", "@codex",
                                     "--judge-mode", "off", "--max-rereview-requests", "1", "--format", output_format])
    assert fgt.main() == 0
    captured = capsys.readouterr()
    expected = 0 if flag else 1
    if output_format == "json":
        status = json.loads(captured.out)["loopStatus"]
        assert status["reReviewRequests"] == expected
        assert status["reReviewCountComplete"] is (flag is None)
        assert status["limitReached"] is (flag is None)
    else:
        assert f"Re-review requests: {expected}" in captured.out
    if flag:
        assert "local comment-request history is incomplete or in flight" in captured.err
