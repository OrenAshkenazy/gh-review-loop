"""Fresh review data, bounded commands, and post-write head uncertainty."""
import copy
import json
import subprocess
import sys

import pytest

import fetch_gemini_threads as fgt
import publish_changes as pc
import request_rereview as rr
from conftest import stub_cap_check_known_under_cap


HEAD = "a" * 40
BOT = {"author": {"login": "coderabbitai", "__typename": "Bot"}, "body": "Fix this", "createdAt": "2026-10-15T10:00:00Z"}
DEFER = {"author": {"login": "maintainer", "__typename": "User"}, "authorAssociation": "COLLABORATOR", "body": "/review-loop defer", "createdAt": "2026-10-15T11:00:00Z"}


@pytest.mark.parametrize("last, deferred", [(DEFER, True), ({**BOT, "createdAt": "2026-10-15T12:00:00Z"}, False)])
def test_full_thread_comments_determine_latest_deferral(monkeypatch, last, deferred):
    thread = {"id": "T1", "comments": {"nodes": [BOT], "pageInfo": {"hasNextPage": True, "endCursor": "C1"}}}
    pull = {"reviewThreads": {"nodes": [thread]}}
    pages = iter([{"data": {"repository": {"pullRequest": pull}}},
                  {"data": {"node": {"comments": {"nodes": [copy.deepcopy(last)], "pageInfo": {"hasNextPage": False, "endCursor": "C2"}}}}}])
    calls = []

    def read(args):
        calls.append(args)
        return next(pages)

    monkeypatch.setattr(fgt, "run_gh", read)
    fgt.fetch_threads(fgt.PullRequest("owner", "repo", 1))
    assert fgt.is_addressed_by_reply(thread, "coderabbitai") is deferred
    assert len(calls) == 2 and "after=C1" in calls[1]
    assert "authorAssociation" in fgt.THREAD_COMMENTS_QUERY and "updatedAt" in fgt.THREAD_COMMENTS_QUERY
    assert fgt.pagination_warnings(pull) == []


@pytest.mark.parametrize("problem", ["missing_cursor", "repeated_cursor", "cap", "malformed", "error"])
def test_partial_thread_history_never_authorizes_deferral(monkeypatch, problem):
    thread = {"id": "T1", "comments": {"nodes": [BOT, DEFER], "pageInfo": {"hasNextPage": True, "endCursor": None if problem == "missing_cursor" else "C1"}}}
    pull = {"reviewThreads": {"nodes": [thread]}}
    calls = []

    def read(args):
        calls.append(args)
        if len(calls) == 1:
            return {"data": {"repository": {"pullRequest": pull}}}
        if problem == "error":
            raise RuntimeError("page unavailable")
        if problem == "malformed":
            return {"data": {"node": None}}
        return {"data": {"node": {"comments": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "C1" if problem == "repeated_cursor" else f"C{len(calls)}"}}}}}

    monkeypatch.setattr(fgt, "run_gh", read)
    fgt.fetch_threads(fgt.PullRequest("owner", "repo", 1))
    assert thread["comments"]["pageInfo"]["hasNextPage"] is True
    assert not fgt.is_addressed_by_reply(thread, "coderabbitai")
    assert fgt.pagination_warnings(pull)
    assert len(calls) <= 21


def test_completed_long_thread_has_no_false_pagination_warning():
    pull = {"reviewThreads": {"nodes": [{"comments": {"nodes": [BOT] * 80, "pageInfo": {"hasNextPage": False}}}]}}
    assert fgt.pagination_warnings(pull) == []


def test_captured_gh_json_is_explicit_utf8(monkeypatch):
    original = subprocess.run
    payload = json.dumps({"body": "Zażółć ✓ 日本語"}, ensure_ascii=False).encode()

    def run(argv, **kwargs):
        assert kwargs["encoding"] == "utf-8" and kwargs["timeout"] == 120
        return original([sys.executable, "-c", f"import sys; sys.stdout.buffer.write({payload!r})"], **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert fgt.run_gh(["api", "test"])["body"] == "Zażółć ✓ 日本語"


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("gh", 120), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")])
def test_bad_or_stalled_gh_output_returns_clean_error(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="UTF-8"):
        fgt.run_gh(["api", "test"])


def test_publisher_commands_have_bounded_timeout(tmp_path, monkeypatch):
    def fail(argv, **kwargs):
        assert kwargs["timeout"] == 120 and not kwargs.get("shell")
        raise subprocess.TimeoutExpired(argv, 120)
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(pc.PublishError, match="timed out"):
        pc.run(["git", "status"], tmp_path)


@pytest.mark.parametrize("after", [{"state": "open", "head": {"sha": "b" * 40}}, {"state": "closed", "head": {"sha": HEAD}}, {"state": "open", "head": []}, None])
def test_head_change_after_comment_keeps_posted_result(monkeypatch, after):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "POST" in argv:
            data = {"id": 99, "created_at": "2026-10-15T12:00:00Z"}
        else:
            data = {"state": "open", "head": {"sha": HEAD}} if len(calls) == 1 else after
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    payload = rr.post_rereview("owner/repo", 1, "@coderabbitai review", runner=run, expect_head=HEAD)
    assert payload["posted"] is True and payload["status"] == "review_head_unconfirmed"
    assert payload["comment_id"] == 99 and payload["expected_head"] == HEAD
    assert sum("POST" in call for call in calls) == 1
    stub_cap_check_known_under_cap(monkeypatch)
    monkeypatch.setattr(rr, "current_head", lambda *args: HEAD)
    monkeypatch.setattr(rr, "post_rereview", lambda *a, **k: dict(payload))
    args = rr.build_parser().parse_args(["--repo", "owner/repo", "--pr", "1", "--reviewer-mention", "@coderabbitai", "--expect-head", HEAD])
    result = rr._locked_request(args)
    assert result["posted"] is True and result["status"] == "review_head_unconfirmed"
    assert len(rr.loop_state.read_request_log("owner/repo", 1)) == 1


def test_request_read_timeout_fails_closed(monkeypatch):
    def fail(argv, **kwargs):
        assert kwargs["timeout"] == 120
        raise subprocess.TimeoutExpired(argv, 120)
    assert rr.gh_login(runner=fail) is None
    assert rr.current_head("owner/repo", 1, runner=fail) is None


def test_native_request_keeps_posted_result_when_final_head_read_times_out():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "POST" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"requested_reviewers": [{"login": "copilot-pull-request-reviewer"}]}), "")
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    payload = rr.request_review("owner/repo", 1, "copilot-pull-request-reviewer", runner=run, registration_head=HEAD)
    assert payload["posted"] is True and payload["status"] == "review_head_unconfirmed"
    assert payload["expected_head"] == HEAD and payload["observed_head"] is None
    assert len(calls) == 2 and sum("POST" in call for call in calls) == 1


@pytest.mark.parametrize("native", [False, True])
def test_write_timeout_keeps_outcome_uncertain(native):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(rr.ReviewRequestRejected) as failure:
        if native:
            rr.request_review("owner/repo", 1, "copilot-pull-request-reviewer", runner=run)
        else:
            rr.post_rereview("owner/repo", 1, "@coderabbitai review", runner=run)
    assert failure.value.outcome_unknown


def test_profile_metacharacters_are_literal_arguments(tmp_path):
    arguments = ["$(touch stolen)", "`whoami`", "& echo stolen", "; echo stolen", "%USERNAME%"]
    command = [sys.executable, "-c", f"import sys; assert sys.argv[1:] == {arguments!r}", *arguments]
    assert pc.run_profile({"checks": [{"command": command, "required": True}]}, tmp_path).verification == "passed"
    assert list(tmp_path.iterdir()) == []
