"""Acceptance test: one full Codex review cycle over recorded PR payloads.

Unit tests cover each helper in isolation; this test drives the loop the way
the skill does — discover the reviewer, fetch findings, request a re-review,
wait for the next review, fetch again — with only the `gh` boundary stubbed.
The payloads in ``fixtures/codex_cycle.json`` are recorded from a live Codex
review, so a change that breaks Codex end-to-end fails here even when every
unit test still passes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import fetch_gemini_threads as fgt
import request_rereview
from conftest import stub_cap_check_known_under_cap
from fetch_gemini_threads import PullRequest, rereview_requests, thread_severity


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "codex_cycle.json").read_text(encoding="utf-8")
)
PR = PullRequest(owner="acme", repo="widget", number=159)
AGENT = "loop-agent"
CODEX = "chatgpt-codex-connector"
REREVIEW_AT = "2026-08-04T11:30:00Z"


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
            "--pr",
            "https://github.com/acme/widget/pull/159",
            "--format",
            "json",
            "--no-resolve-outdated",
            "--no-resolve-addressed-by-reply",
            *extra_args,
        ],
    )
    assert fgt.main() == 0
    return json.loads(capsys.readouterr().out)


def test_discovery_offers_codex_with_a_usable_trigger(loop, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fetch_gemini_threads.py",
            "--pr",
            "https://github.com/acme/widget/pull/159",
            "--list-reviewers",
            "--format",
            "json",
        ],
    )

    assert fgt.main() == 0

    reviewers = json.loads(capsys.readouterr().out)["reviewers"]
    assert [r["login"] for r in reviewers] == [CODEX]
    assert reviewers[0]["display_name"] == "Codex"
    assert reviewers[0]["review_trigger"] == "@codex"


def test_full_codex_cycle_fetches_fixes_re_asks_and_rechecks(loop, monkeypatch, capsys):
    # 1. Select Codex and fetch its first review.
    first = run_fetch(monkeypatch, capsys, "--reviewer", CODEX, "--reviewer-source", "confirmed")

    assert first["reviewerSelection"]["review_trigger"] == "@codex"
    assert first["reviewerSelection"]["auto_reviews"] is False
    assert [t["path"] for t in first["threads"]] == ["src/provider.js", "src/mapping.js"]
    assert [thread_severity(t) for t in first["threads"]] == ["high", "medium"]

    # 2. Ask Codex to review the pushed fixes, using only the persisted selection.
    posted = {}

    def fake_post(repo, pr, phrase, **kwargs):
        posted["phrase"] = phrase
        return {"created_at": REREVIEW_AT, "repo": repo, "pr": pr, "phrase": phrase}

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)
    assert request_rereview.main(["--repo", "acme/widget", "--pr", "159", "--json"]) == 0
    capsys.readouterr()

    assert posted["phrase"] == "@codex review"

    # 3. Wait for the review that answers that request — not the earlier one.
    seen = {"n": 0}

    def payload_sequence(pr):
        seen["n"] += 1
        # The pre-existing review must not end the wait; only the new one does.
        return FIXTURE["initial"] if seen["n"] == 1 else FIXTURE["after_rereview"]

    monkeypatch.setattr(fgt, "fetch_threads", payload_sequence)
    settled = fgt.wait_for_stable_review(
        PR,
        author=CODEX,
        timeout_seconds=60,
        interval_seconds=1,
        quiet_seconds=0,
        after_iso=REREVIEW_AT,
    )

    assert seen["n"] > 1
    assert settled["reviews"]["nodes"][-1]["submittedAt"] == "2026-08-04T11:37:37Z"

    # 4. Re-fetch: only the new finding is current; the answered ones went stale.
    loop["payload"] = FIXTURE["after_rereview"]
    monkeypatch.setattr(fgt, "fetch_threads", lambda pr: FIXTURE["after_rereview"])
    second = run_fetch(monkeypatch, capsys)

    assert second["reviewerSelection"]["login"] == CODEX
    paths = [t["path"] for t in second["threads"]]
    assert paths.count("src/provider.js") == 2  # new inline thread + review-body finding
    assert "src/mapping.js" not in paths
    assert second["loopStatus"]["reReviewRequests"] == 2


def test_codex_rereview_pings_are_counted_toward_the_cap(loop):
    requests = rereview_requests(
        FIXTURE["after_rereview"],
        AGENT,
        review_trigger_mention="@codex",
    )

    assert len(requests) == 2


@pytest.fixture(autouse=True)
def _cap_check_is_inert(monkeypatch):
    """See tests/test_request_rereview.py: the cap check needs `gh`, and these
    tests are about stdout discipline and cycle flow, not the cap."""
    stub_cap_check_known_under_cap(monkeypatch)
