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


def completed_review():
    return {"user": {"type": "Bot", "login": LOGIN}, "state": "COMMENTED", "commit_id": HEAD, "submitted_at": AT}


def request_event():
    return {"id": 100, "event": "review_requested", "actor": {"login": "agent"}, "requested_reviewer": {"login": LOGIN}, "created_at": AT}


@pytest.mark.parametrize("clock_skew", [False, True])
@pytest.mark.parametrize("event_is_new", [False, True])
def test_fast_completed_initial_review_requires_new_event_and_records_once(monkeypatch, clock_skew, event_is_new):
    stub_cap_check_known_under_cap(monkeypatch)
    monkeypatch.setattr(rr, "_snapshot_request_event_ids", lambda *a: [] if event_is_new else [100])
    monkeypatch.setattr(rr, "utc_now_iso", lambda: AT)
    monkeypatch.setattr(rr, "request_state", lambda *a: {
        "state": "ABSENT", "head": HEAD, "pending_since": None, "first_completed_review_at": None,
    })
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if "POST" in cmd:
            return result(cmd, {"requested_reviewers": []})
        if cmd[:3] == ["gh", "api", "graphql"]:
            data = registration()
            data["data"]["repository"]["pullRequest"]["reviewRequests"]["nodes"] = []
            return result(cmd, data)
        if any("/reviews" in argument for argument in cmd):
            review = completed_review()
            if clock_skew:
                review["submitted_at"] = "2026-10-12T19:59:59Z"
            return result(cmd, [review])
        if any("/timeline" in argument for argument in cmd):
            event = request_event()
            if clock_skew:
                event["created_at"] = "2026-10-12T19:59:59Z"
            return result(cmd, [event])
        return result(cmd, {"state": "open", "head": {"sha": HEAD}})

    monkeypatch.setattr(rr.subprocess, "run", runner)
    args = rr.build_parser().parse_args(["--repo", "owner/repo", "--pr", "1", "--reviewer-login", "copilot", "--expect-head", HEAD])
    payload = rr._locked_request(args)
    assert payload["posted"] is (True if event_is_new else None) and sum("POST" in cmd for cmd in calls) == 1
    entry = rr.load_state()[KEY]
    if not event_is_new:
        assert entry["review_request_in_flight"]["request_event_ids"] == [100]
        assert entry.get("run", {}).get("rereviews_posted", 0) == 0
        assert rr._locked_request(args)["status"] == "request_in_flight"
        assert sum("POST" in cmd for cmd in calls) == 1
        return
    assert "review_request_in_flight" not in entry and entry["run"]["rereviews_posted"] == 1
    assert entry["review_requests"][0]["initial_review"] is True
    assert rr.loop_request_times("owner/repo", 1, review_vendors.COPILOT) == []


@pytest.mark.parametrize("problem", ["draft", "old_head", "wrong_bot", "alias_bot", "human", "missing_state", "missing_time", "old_review", "old_event", "wrong_actor", "no_event", "reviews_unreadable", "timeline_unreadable"])
def test_fast_completion_requires_new_selected_bot_and_request_provenance(problem):
    review = completed_review()
    event = request_event()
    if problem == "draft":
        review["state"] = "PENDING"
    elif problem == "old_head":
        review["commit_id"] = "b" * 40
    elif problem in ("wrong_bot", "alias_bot"):
        review["user"]["login"] = "other[bot]" if problem == "wrong_bot" else "copilot[bot]"
    elif problem == "human":
        review["user"]["type"] = "User"
    elif problem == "missing_state":
        review.pop("state")
    elif problem == "missing_time":
        review.pop("submitted_at")
    elif problem == "old_review":
        review["submitted_at"] = "2026-10-12T19:59:59Z"
    elif problem == "wrong_actor":
        event["actor"]["login"] = "other"
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "api", "graphql"]:
            data = registration()
            data["data"]["repository"]["pullRequest"]["reviewRequests"]["nodes"] = []
            return result(cmd, data)
        if any("/reviews" in argument for argument in cmd):
            return result(cmd, [review], 1 if problem == "reviews_unreadable" else 0)
        assert any("/timeline" in argument for argument in cmd)
        return result(cmd, [] if problem == "no_event" else [event], 1 if problem == "timeline_unreadable" else 0)

    assert rr._registered_review("owner/repo", 1, LOGIN, HEAD, runner, actor="agent", request_event_ids=[100] if problem == "old_event" else []) is False
    assert all("POST" not in cmd and "DELETE" not in cmd for cmd in calls)


@pytest.mark.parametrize("first_completed", [None, "2026-10-12T19:00:00Z"])
def test_unknown_actor_prevents_stale_request_cancellation(monkeypatch, first_completed):
    monkeypatch.setattr(rr, "request_state", lambda *a: {
        "state": "STALE_PENDING", "head": HEAD, "pending_since": AT, "first_completed_review_at": first_completed,
        "rerequest_ids": ("PR_target", "BOT_copilot"),
    })
    monkeypatch.setattr(rr, "gh_login", lambda: None)
    monkeypatch.setattr(rr, "count_agent_review_requests", lambda *a: 0)
    monkeypatch.setattr(rr, "cancel_review_request", lambda *a, **kw: pytest.fail("unknown actor must not DELETE"))
    monkeypatch.setattr(rr, "request_review", lambda *a, **kw: pytest.fail("unknown actor must not POST"))
    args = rr.build_parser().parse_args(["--repo", "owner/repo", "--pr", "1", "--reviewer-login", "copilot"])
    payload = rr._locked_request(args)
    assert payload["status"] == "uncountable_actor" and payload["posted"] is False
    assert not rr.load_state().get(KEY, {}).get("review_request_in_flight")


@pytest.mark.parametrize("snapshot", [None, [], [{"event": "review_requested"}], [{"id": True, "event": "review_requested"}]])
def test_request_event_snapshot_refuses_unreadable_or_missing_ids(monkeypatch, snapshot):
    monkeypatch.setattr(rr, "_timeline_events", lambda *a: snapshot)
    assert rr._snapshot_request_event_ids("owner/repo", 1) == ([] if snapshot == [] else None)


def test_unknown_provenance_prevents_stale_cancellation_and_post(monkeypatch):
    monkeypatch.setattr(rr, "request_state", lambda *a: {
        "state": "STALE_PENDING", "head": HEAD, "pending_since": AT, "first_completed_review_at": None,
    })
    monkeypatch.setattr(rr, "gh_login", lambda: "agent")
    monkeypatch.setattr(rr, "_snapshot_request_event_ids", lambda *a: None)
    monkeypatch.setattr(rr, "cancel_review_request", lambda *a, **kw: pytest.fail("unknown snapshot must not DELETE"))
    monkeypatch.setattr(rr, "request_review", lambda *a, **kw: pytest.fail("unknown snapshot must not POST"))
    args = rr.build_parser().parse_args(["--repo", "owner/repo", "--pr", "1", "--reviewer-login", "copilot"])
    payload = rr._locked_request(args)
    assert payload["status"] == "uncountable_provenance" and payload["posted"] is False
    assert not rr.load_state().get(KEY, {}).get("review_request_in_flight")


@pytest.mark.parametrize("local_time", ["2026-10-12T19:00:00Z", "2026-10-12T21:00:00Z"])
@pytest.mark.parametrize("initial_review", [False, True])
def test_event_confirmed_uncertain_request_records_server_timestamp(monkeypatch, local_time, initial_review):
    stub_cap_check_known_under_cap(monkeypatch)
    monkeypatch.setattr(rr, "utc_now_iso", lambda: local_time)
    monkeypatch.setattr(rr, "request_state", lambda *a: {
        "state": "ABSENT", "head": HEAD, "pending_since": None,
        "first_completed_review_at": None if initial_review else "2026-10-12T18:00:00Z",
        "rerequest_ids": None if initial_review else ("PR_target", "BOT_copilot"),
    })
    monkeypatch.setattr(rr, "count_agent_review_requests", lambda *a: 0)
    monkeypatch.setattr(rr, "_snapshot_request_event_ids", lambda *a: [99])
    monkeypatch.setattr(rr, "_timeline_events", lambda *a: [request_event()])
    calls = []

    def post(*a, **kw):
        calls.append(kw)
        raise rr.ReviewRequestRejected("connection lost", requested_at=local_time)

    monkeypatch.setattr(rr, "request_review", post)
    args = rr.build_parser().parse_args(["--repo", "owner/repo", "--pr", "1", "--reviewer-login", "copilot"])
    payload = rr._locked_request(args)
    assert payload["posted"] is True and payload["confirmed_after_uncertain"] is True
    assert payload["created_at"] == AT
    entry = rr.load_state()[KEY]
    assert len(calls) == 1 and "review_request_in_flight" not in entry
    assert len(entry["review_requests"]) == 1 and entry["review_requests"][0]["at"] == AT
    assert entry["review_requests"][0].get("initial_review", False) is initial_review
    assert entry["run"]["rereviews_posted"] == 1
    assert rr.loop_request_times("owner/repo", 1, review_vendors.COPILOT) == ([] if initial_review else [AT])


@pytest.mark.parametrize("new_event", [False, True])
def test_reconciliation_uses_persisted_event_baseline_despite_clock_skew(monkeypatch, new_event):
    marker = native_marker()
    marker["request_event_ids"] = [99]
    rr._mark_request_in_flight("owner/repo", 1, marker)
    monkeypatch.setattr(rr, "request_state", lambda *a: {"state": "COMPLETED_HEAD", "head": HEAD})
    event = request_event()
    event.update(id=100 if new_event else 99, created_at="2026-10-12T19:59:59Z")
    monkeypatch.setattr(rr, "_timeline_events", lambda *a: [event])
    review = completed_review()
    review["submitted_at"] = event["created_at"]
    monkeypatch.setattr(rr, "_all_reviews", lambda *a: [review])
    payload = rr._locked_request(reconcile_args())
    assert payload["posted"] is (True if new_event else None)
    assert bool(rr.load_state()[KEY].get("review_request_in_flight")) is not new_event


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


@pytest.mark.parametrize("used", [0, 3, None])
@pytest.mark.parametrize("uncertain", [False, True])
def test_stale_replacement_carries_refreshed_state_and_cap_into_post_and_recovery(monkeypatch, used, uncertain):
    stub_cap_check_known_under_cap(monkeypatch)
    monkeypatch.setattr(rr, "utc_now_iso", lambda: AT)
    new_head = "b" * 40
    old = {"state": "STALE_PENDING", "head": HEAD, "pending_since": AT,
           "first_completed_review_at": None, "rerequest_ids": ("OLD_PR", "OLD_BOT")}
    refreshed = {"state": "ABSENT", "head": new_head, "pending_since": None,
                 "first_completed_review_at": AT, "rerequest_ids": ("NEW_PR", "NEW_BOT")}
    states = iter([old, refreshed])
    monkeypatch.setattr(rr, "request_state", lambda *a: next(states))
    cancelled, counted, posted = [], [], []
    monkeypatch.setattr(rr, "cancel_review_request", lambda *a, **kw: cancelled.append(kw["expect_head"]) or True)
    monkeypatch.setattr(rr, "count_agent_review_requests", lambda *a: counted.append(a[3:]) or used)
    monkeypatch.setattr(rr, "_new_request_event", lambda *a: None)

    def post(*args, **kwargs):
        posted.append(kwargs)
        marker = rr.load_state()[KEY]["review_request_in_flight"]
        assert marker["head"] == new_head and marker["initial_review"] is False
        if uncertain:
            raise rr.ReviewRequestRejected("connection lost", requested_at=AT)
        return {"posted": True, "created_at": AT}

    monkeypatch.setattr(rr, "request_review", post)
    args = rr.build_parser().parse_args(["--repo", "owner/repo", "--pr", "1", "--reviewer-login", "copilot", "--max-rereview-requests", "3"])
    payload = rr._locked_request(args)
    assert cancelled == [HEAD] and counted == [("agent", AT)]
    if used != 0:
        assert payload["status"] == ("capped" if used == 3 else "uncountable_count")
        assert not posted and not rr.load_state().get(KEY, {}).get("review_request_in_flight")
        return
    assert posted[0]["expect_head"] == new_head and posted[0]["registration_head"] == new_head
    assert posted[0]["rerequest_ids"] == ("NEW_PR", "NEW_BOT")
    if uncertain:
        assert payload["posted"] is None
        monkeypatch.setattr(rr, "request_state", lambda *a: {"state": "PENDING", "head": new_head})
        monkeypatch.setattr(rr, "_new_request_event", lambda *a: request_event())
        assert rr._locked_request(reconcile_args())["status"] == "request_reconciled"
    else:
        assert payload["state"] == "STALE_PENDING" and payload["replaced_stale_request"] is True
    entry = rr.load_state()[KEY]
    assert len(posted) == 1 and "review_request_in_flight" not in entry
    assert not entry["review_requests"][0].get("initial_review") and entry["run"]["rereviews_posted"] == 1
    assert rr.loop_request_times("owner/repo", 1, review_vendors.COPILOT) == [AT]


@pytest.mark.parametrize("problem", [None, "old_id", "actor", "phrase", "time", "id", "baseline"])
def test_comment_id_recovery_tolerates_clock_skew_but_rejects_unrelated_evidence(monkeypatch, problem):
    stub_cap_check_known_under_cap(monkeypatch)
    monkeypatch.setattr(rr, "_snapshot_comment_ids", lambda *a: [99])
    monkeypatch.setattr(rr, "utc_now_iso", lambda: AT)
    posted = []

    def post(*a, **kw):
        posted.append(True)
        raise rr.ReviewRequestRejected("connection lost", requested_at=AT)

    monkeypatch.setattr(rr, "post_rereview", post)
    args = rr.build_parser().parse_args(["--repo", "owner/repo", "--pr", "1", "--reviewer-login", "codex"])
    assert rr._locked_request(args)["posted"] is None
    marker = rr.load_state()[KEY]["review_request_in_flight"]
    assert marker["comment_ids"] == [99]
    assert rr._locked_request(args)["status"] == "request_in_flight" and len(posted) == 1
    if problem == "baseline":
        marker["comment_ids"] = [True]
        state = rr.load_state()
        state[KEY]["review_request_in_flight"] = marker
        loop_state.save_sticky_state(state)
    comment = {"id": 99 if problem == "old_id" else True if problem == "id" else 100,
               "user": {"login": "other" if problem == "actor" else "agent"},
               "body": "@codex other" if problem == "phrase" else "@codex review",
               "created_at": "invalid" if problem == "time" else "2026-10-12T19:59:59Z"}
    monkeypatch.setattr(rr, "_gh_json", lambda *a: [comment])
    payload = rr._locked_request(reconcile_args())
    if problem is not None:
        assert payload["status"] == "request_in_flight" and rr.load_state()[KEY]["review_request_in_flight"]
        return
    assert payload["status"] == "request_reconciled" and payload["created_at"] == comment["created_at"]
    entry = rr.load_state()[KEY]
    assert entry["review_requests"][0]["comment_id"] == 100 and entry["run"]["rereviews_posted"] == 1
    assert rr._locked_request(reconcile_args())["status"] == "no_request_in_flight" and len(posted) == 1


@pytest.mark.parametrize("data", [[], [{"id": 2}, {"id": 1}, {"id": 2}], [{"id": True}], [{"id": 0}], [{}], [None], {}])
def test_comment_snapshot_requires_complete_positive_ids(data):
    snapshot = rr._snapshot_comment_ids("owner/repo", 1, runner=lambda cmd, **kw: result(cmd, data))
    assert snapshot == ([] if data == [] else [1, 2] if len(data) == 3 else None)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("stdout", ["", " ", "[]\n[", "{}"])
def test_provenance_snapshot_rejects_blank_or_malformed_paginated_output(native, stdout):
    reader = rr._snapshot_request_event_ids if native else rr._snapshot_comment_ids
    assert reader("owner/repo", 1, runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout, "")) is None


@pytest.mark.parametrize("dry_run", [False, True])
def test_unreadable_comment_snapshot_blocks_post_but_dry_run_skips_snapshot(monkeypatch, dry_run):
    stub_cap_check_known_under_cap(monkeypatch)
    snapshots, posts = [], []
    monkeypatch.setattr(rr, "_snapshot_comment_ids", lambda *a: snapshots.append(True) or None)
    monkeypatch.setattr(rr, "post_rereview", lambda *a, **kw: posts.append(kw) or {"posted": False, "dry_run": True})
    argv = ["--repo", "owner/repo", "--pr", "1", "--reviewer-login", "codex"] + (["--dry-run"] if dry_run else [])
    payload = rr._locked_request(rr.build_parser().parse_args(argv))
    assert len(snapshots) == (0 if dry_run else 1) and len(posts) == (1 if dry_run else 0)
    assert payload.get("status") == (None if dry_run else "uncountable_provenance")
    assert not rr.load_state().get(KEY, {}).get("review_request_in_flight")


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("expected_head", [None, HEAD, "b" * 40])
def test_fresh_native_event_reconciles_after_head_move_without_current_registration(monkeypatch, initial, expected_head):
    marker = native_marker()
    marker.update(request_event_ids=[99], initial_review=initial)
    rr._mark_request_in_flight("owner/repo", 1, marker)
    event = request_event()
    event["created_at"] = "2026-10-12T19:59:59Z"
    monkeypatch.setattr(rr, "request_state", lambda *a: pytest.fail("past request proof is independent of current PR state"))
    monkeypatch.setattr(rr, "_all_reviews", lambda *a: pytest.fail("event proof needs no review-completion read"))
    monkeypatch.setattr(rr, "_timeline_events", lambda *a: [event])
    args = reconcile_args(*(["--expect-head", expected_head] if expected_head else []))
    payload = rr._locked_request(args)
    assert payload["status"] == "request_reconciled" and payload["created_at"] == event["created_at"]
    entry = rr.load_state()[KEY]
    assert bool(entry["review_requests"][0].get("initial_review")) is initial
    assert entry["run"]["rereviews_posted"] == 1 and "review_request_in_flight" not in entry
    assert rr.loop_request_times("owner/repo", 1, review_vendors.COPILOT) == ([] if initial else [event["created_at"]])
    assert rr._locked_request(args)["status"] == "no_request_in_flight"


@pytest.mark.parametrize("problem", ["old_event", "actor", "vendor", "time", "baseline"])
def test_fresh_native_reconciliation_requires_exact_provenance(monkeypatch, problem):
    marker = native_marker()
    marker["request_event_ids"] = [True] if problem == "baseline" else [100] if problem == "old_event" else [99]
    rr._mark_request_in_flight("owner/repo", 1, marker)
    event = request_event()
    if problem == "actor":
        event["actor"]["login"] = "other"
    elif problem == "vendor":
        event["requested_reviewer"]["login"] = "other[bot]"
    elif problem == "time":
        event["created_at"] = "invalid"
    monkeypatch.setattr(rr, "_timeline_events", lambda *a: [event])
    monkeypatch.setattr(rr, "request_state", lambda *a: pytest.fail("do not fall back from malformed ID provenance"))
    assert rr._locked_request(reconcile_args())["status"] == "request_in_flight"
    assert rr.load_state()[KEY]["review_request_in_flight"] == marker


def test_fresh_native_event_retains_marker_on_record_failure_and_records_once_on_retry(monkeypatch):
    marker = native_marker()
    marker.update(request_event_ids=[99], initial_review=False)
    rr._mark_request_in_flight("owner/repo", 1, marker)
    monkeypatch.setattr(rr, "_timeline_events", lambda *a: [request_event()])
    monkeypatch.setattr(rr, "request_state", lambda *a: pytest.fail("new event evidence must not read current head"))
    real_save = loop_state.save_sticky_state

    def fail_save(state):
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(loop_state, "save_sticky_state", fail_save)
    assert rr._locked_request(reconcile_args())["status"] == "state_record_failed"
    assert rr.load_state()[KEY]["review_request_in_flight"] == marker
    monkeypatch.setattr(loop_state, "save_sticky_state", real_save)
    assert rr._locked_request(reconcile_args())["status"] == "request_reconciled"
    assert rr.load_state()[KEY]["run"]["rereviews_posted"] == 1
    assert rr._locked_request(reconcile_args())["status"] == "no_request_in_flight"
