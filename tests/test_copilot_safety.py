"""Copilot review writes retain the fork's publication and head safeguards."""
from __future__ import annotations

import json
import subprocess

import pytest

import request_rereview as rr
import review_vendors


HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def stub_request_state(monkeypatch, state="ABSENT"):
    monkeypatch.setattr(rr, "request_state", lambda *args: {
        "state": state, "head": HEAD, "pending_since": "2026-10-07T15:50:00Z",
        "first_completed_review_at": "2026-10-07T16:00:00Z",
    })
    monkeypatch.setattr(rr, "gh_login", lambda: "agent")
    monkeypatch.setattr(rr, "effective_cap", lambda *args: 3)
    monkeypatch.setattr(rr, "count_agent_pings", lambda *args: pytest.fail("Copilot must not post or count mention comments"))


def request():
    return rr.main([
        "--repo", "acme/widget", "--pr", "159", "--reviewer-login", "copilot",
        "--expect-head", HEAD, "--json",
    ])


@pytest.mark.parametrize("state", ["ABSENT", "STALE_PENDING"])
@pytest.mark.parametrize("change", ["head", "closed", "unreadable"])
def test_copilot_rechecks_open_pr_after_cap_reads(monkeypatch, capsys, state, change):
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
    assert request() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["posted"] is False
    assert payload["status"] == ("stale_request_not_replaced" if state == "STALE_PENDING" else "review_request_rejected")
    assert len(calls) == 1
    assert not rr.state_path().exists()


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
    assert request() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["posted"] is False
    assert payload["reason"] == "head_mismatch"
    assert writes == ["DELETE"]
    assert not rr.state_path().exists()


def test_copilot_matching_head_posts_native_request_and_records_provenance(monkeypatch, capsys):
    stub_request_state(monkeypatch)
    monkeypatch.setattr(rr, "count_agent_review_requests", lambda *args: 0)
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"state": "open", "head": {"sha": HEAD}}), "")

    monkeypatch.setattr(rr.subprocess, "run", runner)
    assert request() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["posted"] is True and payload["kind"] == "review_request"
    assert calls == [
        ["gh", "api", "repos/acme/widget/pulls/159"],
        ["gh", "api", "repos/acme/widget/pulls/159/requested_reviewers", "--method", "POST",
         "--raw-field", "reviewers[]=copilot-pull-request-reviewer[bot]"],
    ]
    assert rr.loop_request_times("acme/widget", 159, review_vendors.COPILOT) == [payload["created_at"]]
