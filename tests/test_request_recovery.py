"""Ambiguous writes are recoverable without posting or consuming a second slot."""
from __future__ import annotations

import json
import subprocess

import pytest

import request_rereview as rr
import loop_state
import review_vendors
from conftest import stub_cap_check_known_under_cap


HEAD = "a" * 40
AT = "2026-10-12T20:00:00Z"
LOGIN = review_vendors.COPILOT.request_login
KEY = "owner/repo#1"


def result(cmd, data, code=0, error=""):
    return subprocess.CompletedProcess(cmd, code, json.dumps(data), error)


def registration(login=LOGIN, head=HEAD):
    return {"data": {"repository": {"pullRequest": {"headRefOid": head,
            "reviewRequests": {"nodes": [{"requestedReviewer": {"login": login}}]}}}}}


@pytest.mark.parametrize("rest", [{}, [], {"requested_reviewers": []}, {"requested_reviewers": [{"login": "other"}]}])
def test_rest_success_without_selected_registration_remains_uncertain(monkeypatch, rest):
    calls = []
    monkeypatch.setattr(rr.time, "sleep", lambda seconds: None)

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return result(cmd, rest if "POST" in cmd else registration("other"))

    with pytest.raises(rr.ReviewRequestRejected) as info:
        rr.request_review("owner/repo", 1, LOGIN, runner=runner, registration_head=HEAD)
    assert info.value.outcome_unknown and not info.value.allow_timeline_confirmation
    assert sum("POST" in cmd for cmd in calls) == 1
    assert len(calls) == 3


@pytest.mark.parametrize("read", [registration("other"), {}, registration(head="b" * 40)])
def test_rest_registration_recheck_is_bounded_and_can_observe_delayed_registration(monkeypatch, read):
    calls = []
    monkeypatch.setattr(rr.time, "sleep", lambda seconds: None)
    responses = iter([{"requested_reviewers": []}, read, registration()])

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return result(cmd, next(responses))

    payload = rr.request_review("owner/repo", 1, LOGIN, runner=runner, registration_head=HEAD)
    assert payload["posted"] is True
    assert sum("POST" in cmd for cmd in calls) == 1 and len(calls) == 3


def test_old_pending_after_cancellation_does_not_certify_or_post_a_replacement(monkeypatch):
    stub_cap_check_known_under_cap(monkeypatch)
    monkeypatch.setattr(rr, "request_state", lambda *a: {
        "state": "STALE_PENDING", "head": HEAD, "pending_since": AT, "first_completed_review_at": None,
    })
    monkeypatch.setattr(rr, "cancel_review_request", lambda *a, **k: True)
    monkeypatch.setattr(rr, "request_review", lambda *a, **k: pytest.fail("old pending must disappear first"))
    payload = rr._locked_request(rr.build_parser().parse_args(["--repo", "owner/repo", "--pr", "1", "--reviewer-login", "copilot"]))
    assert payload["status"] == "stale_request_not_replaced" and payload["posted"] is False
    assert not rr.load_state().get(KEY, {}).get("review_request_in_flight")


@pytest.mark.parametrize("failure,uncertain", [("HTTP 403", False), ("HTTP 422", False), ("HTTP 503", True), ("connection reset", True), ("malformed", True), ("head", False)])
def test_comment_failures_keep_only_uncertain_markers_and_never_repost(monkeypatch, failure, uncertain):
    stub_cap_check_known_under_cap(monkeypatch)
    monkeypatch.setattr(rr, "current_head", lambda *a: HEAD)
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if "POST" not in cmd:
            return result(cmd, {"state": "open", "head": {"sha": "b" * 40 if failure == "head" else HEAD}})
        return result(cmd, [] if failure == "malformed" else {}, 0 if failure == "malformed" else 1, failure)

    monkeypatch.setattr(rr.subprocess, "run", runner)
    args = rr.build_parser().parse_args(["--repo", "owner/repo", "--pr", "1", "--reviewer-login", "codex",
                                        "--phrase", "@codex review", "--expect-head", HEAD])
    payload = rr._locked_request(args)
    assert payload["posted"] is (None if uncertain else False)
    assert bool(rr.load_state()[KEY].get("review_request_in_flight")) is uncertain
    if uncertain:
        assert rr._locked_request(args)["status"] == "request_in_flight"
    assert sum("POST" in cmd for cmd in calls) == (0 if failure == "head" else 1)


def reconcile_args(*extra):
    return rr.build_parser().parse_args(["--repo", "owner/repo", "--pr", "1", "--reconcile", *extra])


def native_marker():
    return {"reviewer": LOGIN, "at": AT, "actor": "agent", "kind": "review_request", "head": HEAD, "initial_review": True}


@pytest.mark.parametrize("state,confirmed", [("PENDING", True), ("COMPLETED_HEAD", True), ("ABSENT", False)])
def test_native_reconciliation_stamps_once_without_any_post(monkeypatch, state, confirmed):
    rr._mark_request_in_flight("owner/repo", 1, native_marker())
    monkeypatch.setattr(rr, "request_state", lambda *a: {"state": state, "head": HEAD})
    monkeypatch.setattr(rr, "confirm_posted_after_uncertain", lambda *a: True)
    monkeypatch.setattr(rr, "_all_reviews", lambda *a: [{"user": {"login": LOGIN}, "state": "COMMENTED",
                                                       "commit_id": HEAD, "submitted_at": AT}] if state == "COMPLETED_HEAD" else [])
    payload = rr._locked_request(reconcile_args())
    assert payload["posted"] is (True if confirmed else None)
    entry = rr.load_state()[KEY]
    if confirmed:
        assert entry["run"]["rereviews_posted"] == 1
        assert entry["review_requests"][0]["initial_review"] is True
        assert "review_request_in_flight" not in entry
        assert rr._locked_request(reconcile_args())["status"] == "no_request_in_flight"
        assert rr.load_state()[KEY]["run"]["rereviews_posted"] == 1
    else:
        assert entry["review_request_in_flight"] and "run" not in entry


@pytest.mark.parametrize("problem", ["head", "event", "older_review", "dry_run"])
def test_native_reconciliation_does_not_clear_unproven_or_dry_run_markers(monkeypatch, problem):
    rr._mark_request_in_flight("owner/repo", 1, native_marker())
    monkeypatch.setattr(rr, "request_state", lambda *a: {"state": "COMPLETED_HEAD", "head": "b" * 40 if problem == "head" else HEAD})
    monkeypatch.setattr(rr, "confirm_posted_after_uncertain", lambda *a: problem != "event")
    monkeypatch.setattr(rr, "_all_reviews", lambda *a: [{"user": {"login": LOGIN}, "state": "COMMENTED",
                                                       "commit_id": HEAD, "submitted_at": "2026-10-12T19:00:00Z" if problem == "older_review" else AT}])
    payload = rr._locked_request(reconcile_args(*(["--dry-run"] if problem == "dry_run" else [])))
    assert payload["posted"] is (False if problem == "dry_run" else None)
    assert rr.load_state()[KEY]["review_request_in_flight"]
    assert "run" not in rr.load_state()[KEY]


@pytest.mark.parametrize("problem", [None, "actor", "phrase", "time"])
def test_comment_reconciliation_matches_exact_evidence_and_records_comment_id(monkeypatch, problem):
    rr._mark_request_in_flight("owner/repo", 1, {"reviewer": "@codex", "at": AT, "actor": "agent", "kind": "comment", "phrase": "@codex review"})
    comment = {"id": 123, "user": {"login": "someone" if problem == "actor" else "agent"},
               "body": "@codex another review" if problem == "phrase" else "@codex review",
               "created_at": "2026-10-12T19:00:00Z" if problem == "time" else AT}
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return result(cmd, [comment])

    monkeypatch.setattr(rr.subprocess, "run", runner)
    payload = rr._locked_request(reconcile_args())
    assert payload["posted"] is (True if problem is None else None)
    assert all("POST" not in cmd and "DELETE" not in cmd for cmd in calls)
    if problem is None:
        assert rr.load_state()[KEY]["review_requests"][0]["comment_id"] == 123
    else:
        assert rr.load_state()[KEY]["review_request_in_flight"]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("older_incomplete", [False, True])
def test_failed_receipt_save_can_be_reconciled_without_poisoning_complete_history(monkeypatch, native, older_incomplete):
    loop_state.save_sticky_state({KEY: {"review_requests_incomplete": True} if older_incomplete else {}})
    marker = native_marker() if native else {"reviewer": "@codex", "at": AT, "actor": "agent", "kind": "comment", "phrase": "@codex review"}
    marker["initial_review"] = False
    rr._mark_request_in_flight("owner/repo", 1, marker)
    real_save = loop_state.save_sticky_state
    attempts = []

    def fail_once(state):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("simulated atomic replacement failure")
        real_save(state)

    monkeypatch.setattr(loop_state, "save_sticky_state", fail_once)
    payload = {"posted": True, "created_at": AT}
    rr._record_posted_payload(reconcile_args(), payload, marker["reviewer"])
    assert payload["status"] == "state_record_failed" and payload["posted"] is True
    assert rr.load_state()[KEY]["review_request_in_flight"]
    assert bool(rr.load_state()[KEY].get("review_requests_incomplete")) is older_incomplete
    monkeypatch.setattr(rr, "request_state", lambda *a: {"state": "PENDING", "head": HEAD})
    monkeypatch.setattr(rr, "confirm_posted_after_uncertain", lambda *a: True)
    monkeypatch.setattr(rr, "_gh_json", lambda *a: [{"id": 123, "user": {"login": "agent"}, "body": "@codex review", "created_at": AT}])
    assert rr._locked_request(reconcile_args())["status"] == "request_reconciled"
    entry = rr.load_state()[KEY]
    assert "review_request_in_flight" not in entry and entry["run"]["rereviews_posted"] == 1
    assert bool(entry.get("review_requests_incomplete")) is older_incomplete
    if native:
        assert rr.loop_request_times("owner/repo", 1, review_vendors.COPILOT) == (None if older_incomplete else [AT])
    else:
        assert rr.count_agent_pings("owner/repo", 1, "@codex", "agent", runner=lambda cmd, **kw: result(cmd, [])) == (None if older_incomplete else 1)
