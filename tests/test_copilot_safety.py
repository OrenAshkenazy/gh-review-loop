"""Copilot review writes retain the fork's publication and head safeguards."""
from __future__ import annotations

import json
import subprocess

import pytest

import request_rereview as rr
import review_vendors


HEAD = "a" * 40
OTHER_HEAD = "b" * 40


@pytest.mark.parametrize("initial_event", [True, False])
def test_legacy_initial_receipt_is_free_only_with_github_evidence(monkeypatch, initial_event):
    initial = "2026-10-07T15:30:00Z"
    later = "2026-10-07T16:20:00Z"
    boundary = "2026-10-07T16:00:00Z"
    entry = {"review_requests": [{"reviewer": "copilot", "at": at} for at in (initial, later)]}
    monkeypatch.setattr(rr, "load_state", lambda: {"acme/widget#159": entry})
    events = [{"event": "review_requested", "actor": {"login": "agent"}, "requested_reviewer": {"login": "copilot-pull-request-reviewer[bot]"}, "created_at": at} for at in ((initial, later) if initial_event else (later,))]
    monkeypatch.setattr(rr, "_timeline_events", lambda *args: events)
    assert rr.count_agent_review_requests("acme/widget", 159, review_vendors.COPILOT, "agent", boundary) == (1 if initial_event else None)


def test_clock_skew_across_first_review_boundary_still_counts_rerequest(monkeypatch):
    entry = {"review_requests": [{"reviewer": "copilot", "at": "2026-10-07T15:59:59Z"}]}
    monkeypatch.setattr(rr, "load_state", lambda: {"acme/widget#159": entry})
    event = {"event": "review_requested", "actor": {"login": "agent"}, "requested_reviewer": {"login": "copilot-pull-request-reviewer[bot]"}, "created_at": "2026-10-07T16:00:01Z"}
    monkeypatch.setattr(rr, "_timeline_events", lambda *args: [event])
    assert rr.count_agent_review_requests("acme/widget", 159, review_vendors.COPILOT, "agent", "2026-10-07T16:00:00Z") == 1


def test_explicit_rereview_receipt_cannot_match_an_initial_event(monkeypatch):
    import fetch_gemini_threads as fgt

    entry = {"review_requests": [{"reviewer": "copilot", "at": "2026-10-07T15:59:59Z", "initial_review": False}]}
    monkeypatch.setattr(rr, "load_state", lambda: {"acme/widget#159": entry})
    event = {"event": "review_requested", "actor": {"login": "agent"}, "requested_reviewer": {"login": "copilot"}, "created_at": "2026-10-07T15:59:58Z"}
    monkeypatch.setattr(rr, "_timeline_events", lambda *args: [event])
    assert rr.count_agent_review_requests("acme/widget", 159, review_vendors.COPILOT, "agent", "2026-10-07T16:00:00Z") == 1
    pr = {"timelineItems": {"nodes": [event]}, "reviews": {"nodes": [{"user": {"login": "copilot"}, "submitted_at": "2026-10-07T16:00:00Z", "state": "COMMENTED"}]}}
    counted = fgt.rereview_requests(pr, "agent", reviewer_login="copilot", loop_request_times=[entry["review_requests"][0]["at"]], comment_receipts=entry["review_requests"])
    assert len(counted) == 1 and not counted[0].get("provenance_unknown")


@pytest.mark.parametrize("at", [None, "invalid", ""])
def test_malformed_native_receipt_does_not_create_free_capacity(monkeypatch, at):
    import fetch_gemini_threads as fgt

    entry = {"review_requests": [{"reviewer": "copilot", "at": at}]}
    monkeypatch.setattr(rr, "load_state", lambda: {"acme/widget#159": entry})
    monkeypatch.setattr(rr, "_timeline_events", lambda *args: [])
    monkeypatch.setattr(fgt, "load_sticky_state", lambda: {"acme/widget#159": entry})
    assert rr.count_agent_review_requests("acme/widget", 159, review_vendors.COPILOT, "agent", "2026-10-07T16:00:00Z") is None
    assert fgt.loop_request_times(fgt.PullRequest("acme", "widget", 159), "copilot") is None
    assert not fgt.review_request_count_complete({}, loop_request_times=None, counted_requests=0, comment_receipts=entry["review_requests"])


def stub_request_state(monkeypatch, state="ABSENT"):
    monkeypatch.setattr(rr, "request_state", lambda *args: {
        "state": state, "head": HEAD, "pending_since": "2026-10-07T15:50:00Z",
        "first_completed_review_at": "2026-10-07T16:00:00Z",
        "rerequest_ids": ("PR_target", "BOT_copilot"),
    })
    monkeypatch.setattr(rr, "gh_login", lambda: "agent")
    monkeypatch.setattr(rr, "effective_cap", lambda *args: 3)
    monkeypatch.setattr(rr, "_snapshot_request_event_ids", lambda *args: [])
    monkeypatch.setattr(rr, "count_agent_pings", lambda *args: pytest.fail("Copilot must not post or count mention comments"))


def request():
    return rr.main([
        "--repo", "acme/widget", "--pr", "159", "--reviewer-login", "copilot",
        "--expect-head", HEAD, "--json",
    ])


@pytest.mark.parametrize("state", ["ABSENT", "STALE_PENDING"])
@pytest.mark.parametrize("change", ["head", "closed", "unreadable"])
@pytest.mark.parametrize("explicit_head", [False, True])
def test_copilot_rechecks_open_pr_after_cap_reads(monkeypatch, capsys, state, change, explicit_head):
    stub_request_state(monkeypatch, state)
    remote = {"state": "open", "head": {"sha": HEAD}}
    calls = []

    def count(*args):
        if change == "head":
            remote["head"]["sha"] = OTHER_HEAD
        elif change == "closed":
            remote["state"] = "closed"
        else:
            remote.clear()
        return 0

    def runner(cmd, **kwargs):
        calls.append(cmd)
        assert "--method" not in cmd, "no POST or DELETE may follow a failed head check"
        assert cmd == ["gh", "api", "repos/acme/widget/pulls/159"]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(remote), "")

    monkeypatch.setattr(rr, "count_agent_review_requests", count)
    monkeypatch.setattr(rr.subprocess, "run", runner)
    argv = ["--repo", "acme/widget", "--pr", "159", "--reviewer-login", "copilot", "--json"]
    assert rr.main(argv + (["--expect-head", HEAD] if explicit_head else [])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["posted"] is False
    assert payload["status"] == ("stale_request_not_replaced" if state == "STALE_PENDING" else "review_request_rejected")
    assert len(calls) == 1
    assert not rr.load_state().get("acme/widget#159", {})


def test_implicit_head_guard_blocks_a_push_between_marker_and_post(monkeypatch, capsys):
    stub_request_state(monkeypatch)
    monkeypatch.setattr(rr, "count_agent_review_requests", lambda *a: 0)
    remote = {"state": "open", "head": {"sha": HEAD}}
    real_mark = rr._mark_request_in_flight
    calls = []

    def mark(*args):
        real_mark(*args)
        if args[-1] is not None:
            remote["head"]["sha"] = OTHER_HEAD

    def runner(cmd, **kwargs):
        calls.append(cmd)
        assert "--method" not in cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps(remote), "")

    monkeypatch.setattr(rr, "_mark_request_in_flight", mark)
    monkeypatch.setattr(rr.subprocess, "run", runner)
    assert rr.main(["--repo", "acme/widget", "--pr", "159", "--reviewer-login", "copilot", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["posted"] is False and len(calls) == 1
    assert not rr.load_state()["acme/widget#159"].get("review_request_in_flight")


def test_copilot_rechecks_head_after_cancelling_stale_request(monkeypatch, capsys):
    stub_request_state(monkeypatch, "STALE_PENDING")
    monkeypatch.setattr(rr, "count_agent_review_requests", lambda *args: 0)
    remote = {"state": "open", "head": {"sha": HEAD}}
    writes = []

    def runner(cmd, **kwargs):
        if "--method" in cmd:
            writes.append(cmd[cmd.index("--method") + 1])
            assert writes == ["DELETE"], "a changed head must prevent the subsequent POST"
            remote["head"]["sha"] = OTHER_HEAD
        return subprocess.CompletedProcess(cmd, 0, json.dumps(remote), "")

    monkeypatch.setattr(rr.subprocess, "run", runner)
    monkeypatch.setattr(rr, "request_state", lambda *args: {
        "state": "STALE_PENDING" if not writes else "ABSENT", "head": remote["head"]["sha"],
        "pending_since": "2026-10-07T15:50:00Z", "first_completed_review_at": "2026-10-07T16:00:00Z",
        "rerequest_ids": ("PR_target", "BOT_copilot"),
    })
    assert request() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["posted"] is False
    assert payload["status"] == "head_mismatch"
    assert writes == ["DELETE"]
    assert not rr.load_state().get("acme/widget#159", {})


def test_copilot_matching_head_posts_native_request_and_records_provenance(monkeypatch, capsys):
    stub_request_state(monkeypatch)
    monkeypatch.setattr(rr, "count_agent_review_requests", lambda *args: 0)
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if "graphql" in cmd:
            if rr.STATE_QUERY in cmd[4]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"data": {"repository": {"pullRequest": {
                    "headRefOid": HEAD, "reviewRequests": {"nodes": [{"requestedReviewer": {"login": review_vendors.COPILOT.request_login}}]},
                }}}}), "")
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"data": {"rerequestReviews": {"pullRequest": {"id": "PR_target"}}}}), "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"state": "open", "head": {"sha": HEAD},
                                                             "requested_reviewers": [{"login": review_vendors.COPILOT.request_login}]}), "")

    monkeypatch.setattr(rr.subprocess, "run", runner)
    assert request() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["posted"] is True and payload["kind"] == "review_request"
    assert calls[:2] == [
        ["gh", "api", "repos/acme/widget/pulls/159"],
        ["gh", "api", "graphql", "-f",
         "query=mutation($pr:ID!, $bot:ID!) { rerequestReviews(input:{pullRequestId:$pr, botIds:[$bot]}) { pullRequest { id } } }",
         "-f", "pr=PR_target", "-f", "bot=BOT_copilot"],
    ]
    assert len(calls) == 3 and rr.STATE_QUERY in calls[2][4]
    assert rr.loop_request_times("acme/widget", 159, review_vendors.COPILOT) == [payload["created_at"]]


@pytest.mark.parametrize("state", ["ABSENT", "STALE_PENDING"])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("no_cap_check", [False, True])
def test_missing_rerequest_ids_stop_before_cancellation_or_post(monkeypatch, capsys, state, dry_run, no_cap_check):
    stub_request_state(monkeypatch, state)
    monkeypatch.setattr(rr, "request_state", lambda *args: {
        "state": state, "head": HEAD, "pending_since": None,
        "first_completed_review_at": "2026-10-07T16:00:00Z", "rerequest_ids": None,
    })
    monkeypatch.setattr(rr, "count_agent_review_requests", lambda *args: 0)
    monkeypatch.setattr(rr, "cancel_review_request", lambda *a, **kw: pytest.fail("missing IDs must not cancel a request"))
    monkeypatch.setattr(rr, "request_review", lambda *a, **kw: pytest.fail("missing IDs must not fall back to REST"))
    monkeypatch.setattr(rr, "_mark_request_in_flight", lambda *a: pytest.fail("missing IDs must not create recovery state"))
    argv = ["--repo", "acme/widget", "--pr", "159", "--reviewer-login", "copilot", "--json"]
    assert rr.main(argv + (["--dry-run"] if dry_run else []) + (["--no-cap-check"] if no_cap_check else [])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "uncountable_rerequest_ids" and payload["posted"] is False
    assert not rr.load_state().get("acme/widget#159", {})


@pytest.mark.parametrize("first_completed", [None, "2026-10-07T16:00:00Z"])
def test_refreshed_missing_ids_do_not_fall_back_to_rest(monkeypatch, capsys, first_completed):
    stub_request_state(monkeypatch, "STALE_PENDING")
    states = iter([
        {"state": "STALE_PENDING", "head": HEAD, "pending_since": None,
         "first_completed_review_at": first_completed, "rerequest_ids": ("PR_target", "BOT_copilot")},
        {"state": "ABSENT", "head": HEAD, "pending_since": None,
         "first_completed_review_at": "2026-10-07T16:00:00Z", "rerequest_ids": None},
    ])
    monkeypatch.setattr(rr, "request_state", lambda *args: next(states))
    monkeypatch.setattr(rr, "count_agent_review_requests", lambda *args: 0)
    cancelled = []
    monkeypatch.setattr(rr, "cancel_review_request", lambda *a, **kw: cancelled.append(kw["expect_head"]) or True)
    monkeypatch.setattr(rr, "request_review", lambda *a, **kw: pytest.fail("refreshed missing IDs must not fall back to REST"))
    monkeypatch.setattr(rr, "_mark_request_in_flight", lambda *a: pytest.fail("refreshed missing IDs must not create recovery state"))
    assert request() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "uncountable_rerequest_ids" and payload["posted"] is False
    assert cancelled == [HEAD]
    assert not rr.load_state().get("acme/widget#159", {})


@pytest.mark.parametrize("flag", [None, "review_requests_incomplete", "review_request_in_flight"])
def test_native_counter_preserves_explicit_unknown_history_with_empty_timeline(monkeypatch, flag):
    entry = {"review_requests": []}
    if flag:
        entry[flag] = True
    monkeypatch.setattr(rr, "load_state", lambda: {"acme/widget#159": entry})
    monkeypatch.setattr(rr, "_timeline_events", lambda *args: [])
    count = rr.count_agent_review_requests("acme/widget", 159, review_vendors.COPILOT, "agent", "2026-10-07T16:00:00Z")
    assert count == (0 if flag is None else None)
