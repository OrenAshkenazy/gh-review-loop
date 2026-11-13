"""The loop phase recorded on the run block.

Where a loop is (fetched, summarized, requested, waiting) used to live only
in the skill's prose; the hooks inferred "pushed without a summary" from
counter deltas and the resume ladder was prose-detected. These tests drive
the real entry points over the recorded Codex payloads, with only the `gh`
boundary stubbed, and read the phase back from state.json.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import sys
from pathlib import Path

import pytest

import fetch_gemini_threads as fgt
import loop_state
import request_rereview
from conftest import stub_cap_check_known_under_cap
from fetch_gemini_threads import PullRequest

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "codex_cycle.json").read_text(encoding="utf-8")
)
PR = PullRequest(owner="acme", repo="widget", number=159)
KEY = "acme/widget#159"
AGENT = "loop-agent"
CODEX = "chatgpt-codex-connector"
REREVIEW_AT = "2026-08-04T11:30:00Z"


@pytest.fixture
def loop(tmp_path, monkeypatch):
    """The loop with only the gh boundary replaced (see the Codex acceptance test)."""
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(fgt, "resolve_pr", lambda arg: PR)
    monkeypatch.setattr(fgt, "gh_authenticated_login", lambda: AGENT)
    monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _seconds: None)
    state = {"payload": FIXTURE["initial"]}
    monkeypatch.setattr(fgt, "fetch_threads", lambda pr: state["payload"])
    monkeypatch.setattr(request_rereview, "read_summary_pull_request", lambda *a, **k: state["payload"])
    monkeypatch.setattr(
        fgt,
        "fetch_reviewer_discovery",
        lambda pr, **kwargs: {"pull_request": state["payload"], "partial": False, "warnings": []},
    )
    # The receipt goes to the sticky PR comment; no GitHub here.
    monkeypatch.setattr(fgt, "post_or_update_sticky_receipt", lambda *a, **k: 1)
    return state


def run_main(monkeypatch, capsys, *extra_args, fmt="json"):
    argv = [
        "fetch_gemini_threads.py",
        "--pr", "https://github.com/acme/widget/pull/159",
        "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
        *extra_args,
    ]
    if fmt:
        argv += ["--format", fmt]
    monkeypatch.setattr(sys, "argv", argv)
    assert fgt.main() == 0
    out, err = capsys.readouterr()
    return (json.loads(out) if fmt == "json" and out.strip().startswith("{") else out), err


def run_block():
    return fgt.read_run_tracking(PR)


def select_codex(monkeypatch, capsys):
    return run_main(monkeypatch, capsys, "--reviewer", CODEX, "--reviewer-source", "confirmed")[0]


def fake_post(monkeypatch):
    def post(repo, pr, phrase, **kwargs):
        if kwargs.get("dry_run"):
            return {"status": "dry_run", "created_at": None, "repo": repo, "pr": pr,
                    "phrase": phrase, "dry_run": True, "posted": False}
        return {"created_at": REREVIEW_AT, "repo": repo, "pr": pr, "phrase": phrase}

    monkeypatch.setattr(request_rereview, "post_rereview", post)
    stub_cap_check_known_under_cap(monkeypatch)


def _iso(delta_seconds: int) -> str:
    """Now plus ``delta_seconds``, as the scripts spell timestamps. The wait
    budget is measured from the --after anchor as well as from the wait's
    start, so a wait test anchors near the present, after every fixture
    timestamp."""
    now = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=delta_seconds)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def request(capsys, *extra):
    assert request_rereview.main(["--repo", "acme/widget", "--pr", "159", "--json", *extra]) == 0
    out, err = capsys.readouterr()
    return json.loads(out), err


class TestPhaseWriters:
    def test_a_real_fetch_records_fetched_and_the_cap(self, loop, monkeypatch, capsys):
        first = select_codex(monkeypatch, capsys)
        run = run_block()
        assert run["phase"] == "fetched"
        assert run["phase_at"]
        # The counters the fetch computed, so a local read can say "capped"
        # without a GitHub call; they agree with the JSON by construction.
        status = first["loopStatus"]
        assert run["cap_used"] == status["reReviewRequests"]
        assert run["cap_limit"] == status["reReviewLimit"]
        assert run["cap_reached"] == status["limitReached"] is False
        assert run["cap_count_complete"] == status["reReviewCountComplete"] is True
        assert run["open_actionable"] == len(first["threads"]) > 0
        assert run["ci_failing"] == [] and run["ci_pending"] == 0
        assert run["clean_blocked_by"] == status["cleanBlockedBy"]
        assert status["phase"] == "fetched"
        assert status["requestedAt"] is None

    def test_a_fetch_records_the_prs_branch(self, loop, monkeypatch, capsys):
        """loop_status.py checks the checkout is on this branch before it
        trusts local HEAD; a payload without the fields stores None."""
        select_codex(monkeypatch, capsys)
        assert run_block()["head_ref"] is None and run_block()["head_repo"] is None
        loop["payload"] = {
            **copy.deepcopy(FIXTURE["initial"]),
            "headRefName": "fix/widget", "headRepository": {"nameWithOwner": "someone/widget"},
        }
        run_main(monkeypatch, capsys)
        assert run_block()["head_ref"] == "fix/widget"
        assert run_block()["head_repo"] == "someone/widget"

    def test_open_actionable_follows_the_severity_filter(self, loop, monkeypatch, capsys):
        """What the fetch reports as actionable is the filtered set; a resume
        must not be told to fix threads the fetch dropped."""
        first = select_codex(monkeypatch, capsys)
        assert run_block()["open_actionable"] == len(first["threads"]) == 2
        filtered, _ = run_main(monkeypatch, capsys, "--min-severity", "critical", "--drop-unknown-severity")
        assert filtered["threads"] == []
        assert run_block()["open_actionable"] == 0

    @pytest.mark.parametrize("extra", [(), ("--auto-snapshot",)])
    def test_cycle_summary_records_summarized_including_the_backstop(self, loop, monkeypatch, capsys, extra):
        """The Stop-hook backstop runs --cycle-summary --auto-snapshot; the push
        gate already treats it as the summary, so the phase agrees."""
        select_codex(monkeypatch, capsys)
        run_main(monkeypatch, capsys, "--cycle-summary", "--dry-run", *extra, fmt=None)
        assert run_block()["phase"] == "summarized"

    def test_a_posted_ping_records_requested_with_its_time(self, loop, monkeypatch, capsys):
        select_codex(monkeypatch, capsys)
        run_main(monkeypatch, capsys, "--cycle-summary", "--dry-run", fmt=None)
        fake_post(monkeypatch)
        payload, err = request(capsys)
        assert payload["created_at"] == REREVIEW_AT
        run = run_block()
        assert run["phase"] == "requested"
        assert run["requested_at"] == REREVIEW_AT
        assert run["requested_head"] is None  # no --expect-head: unknown, not stale
        assert "loop phase" not in err

    def test_expect_head_is_kept_as_the_requested_head(self, loop, monkeypatch, capsys):
        select_codex(monkeypatch, capsys)
        run_main(monkeypatch, capsys, "--cycle-summary", "--dry-run", fmt=None)
        fake_post(monkeypatch)
        monkeypatch.setattr(request_rereview, "current_head", lambda *a, **k: "abc1234")
        request(capsys, "--expect-head", "abc1234")
        assert run_block()["requested_head"] == "abc1234"

    def test_a_dry_run_request_records_nothing(self, loop, monkeypatch, capsys):
        select_codex(monkeypatch, capsys)
        fake_post(monkeypatch)
        request(capsys, "--dry-run")
        assert run_block()["phase"] == "fetched"

    def test_a_request_straight_after_a_fetch_warns_once(self, loop, monkeypatch, capsys):
        """fetched -> requested means the cycle summary was skipped: the one
        transition that is never right. Recorded anyway; never blocked."""
        select_codex(monkeypatch, capsys)
        fake_post(monkeypatch)
        payload, err = request(capsys)
        assert payload["created_at"] == REREVIEW_AT
        assert err.count("loop phase fetched -> requested") == 1
        assert run_block()["phase"] == "requested"

    def test_the_next_fetch_keeps_the_request_fields(self, loop, monkeypatch, capsys):
        select_codex(monkeypatch, capsys)
        run_main(monkeypatch, capsys, "--cycle-summary", "--dry-run", fmt=None)
        fake_post(monkeypatch)
        request(capsys)
        loop["payload"] = FIXTURE["after_rereview"]
        second, _ = run_main(monkeypatch, capsys)
        assert second["loopStatus"]["phase"] == "fetched"
        assert second["loopStatus"]["requestedAt"] == REREVIEW_AT

    def test_record_run_clears_the_run_and_leaves_last_run(self, loop, monkeypatch, capsys):
        select_codex(monkeypatch, capsys)
        run_main(monkeypatch, capsys, "--record-run", "--outcome", "clean", "--dry-run", fmt=None)
        entry = loop_state.load_sticky_state()[KEY]
        assert "run" not in entry
        assert entry["last_run"]["outcome"] == "clean"
        assert entry["last_run"]["recorded_at"]
        assert entry["last_run"]["session_cycle"] == 1
        assert not loop_state.any_active_run()


class TestWaitPhase:
    @staticmethod
    def _refusing_payload(created_at: str):
        payload = copy.deepcopy(FIXTURE["initial"])
        payload.setdefault("comments", {}).setdefault("nodes", []).append({
            "author": {"login": CODEX},
            "createdAt": created_at,
            "body": "You have reached your usage limits for Codex code review.",
            "url": "https://github.com/acme/widget/pull/159#issuecomment-1",
        })
        return payload

    def test_a_chunked_wait_records_waiting_and_its_snapshot(self, loop, monkeypatch, capsys):
        select_codex(monkeypatch, capsys)
        out, _ = run_main(
            monkeypatch, capsys, "--wait", "--after", _iso(-60),
            "--wait-chunk-seconds", "1", "--timeout", "600",
        )
        assert out["wait"]["status"] == "waiting"
        run = run_block()
        assert run["phase"] == "waiting"
        assert run["wait"]["last_snapshot"]["status"] == "waiting"

    def test_a_timed_out_wait_keeps_its_outcome(self, loop, monkeypatch, capsys):
        select_codex(monkeypatch, capsys)
        out, _ = run_main(monkeypatch, capsys, "--wait", "--after", REREVIEW_AT, "--timeout", "0")
        assert out["wait"]["status"] == "timed_out"
        run = run_block()
        assert run["phase"] == "waiting"
        assert run["wait"]["last_snapshot"]["status"] == "timed_out"

    @pytest.mark.parametrize("mode", ["blocking", "chunked"])
    def test_a_refusal_is_kept_not_cleared(self, loop, monkeypatch, capsys, mode):
        """A refusal is the wait's outcome; a later local read must be able to
        say the reviewer declined, and --wait-heartbeat renders it."""
        select_codex(monkeypatch, capsys)
        after = _iso(-60)
        loop["payload"] = self._refusing_payload(_iso(5))
        extra = ("--wait-chunk-seconds", "1") if mode == "chunked" else ()
        out, _ = run_main(monkeypatch, capsys, "--wait", "--after", after, "--timeout", "600", *extra)
        assert out["wait"]["status"] == "refused"
        assert out["wait"]["kind"] == "quota_exhausted"
        run = run_block()
        assert run["phase"] == "waiting"
        assert run["wait"]["last_snapshot"]["status"] == "refused"
        heartbeat, _ = run_main(monkeypatch, capsys, "--wait-heartbeat", fmt=None)
        assert "refused the review" in heartbeat

    @pytest.mark.parametrize("mode", ["blocking", "chunked"])
    def test_a_phase_write_that_fails_does_not_abort_the_wait(self, loop, monkeypatch, capsys, mode):
        """Read-only or full disk: every state write fails. The phase is only
        a record, so the wait still polls the reviewer and reports."""
        select_codex(monkeypatch, capsys)

        def disk_full(_state):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(loop_state, "save_sticky_state", disk_full)
        extra = ("--wait-chunk-seconds", "1") if mode == "chunked" else ()
        out, err = run_main(monkeypatch, capsys, "--wait", "--after", REREVIEW_AT, "--timeout", "0", *extra)
        assert out["wait"]["status"] == "timed_out"
        assert "could not record the waiting phase" in err

    @pytest.mark.parametrize("earlier", ["refused", "timed_out"])
    def test_a_blocking_wait_that_succeeds_clears_the_earlier_outcome(self, loop, monkeypatch, capsys, earlier):
        """The reviewer answered a later request: the kept refusal or timeout
        is obsolete, and --wait-heartbeat must stop printing it."""
        select_codex(monkeypatch, capsys)
        if earlier == "refused":
            loop["payload"] = self._refusing_payload(_iso(5))
            out, _ = run_main(monkeypatch, capsys, "--wait", "--after", _iso(-60), "--timeout", "600")
        else:
            out, _ = run_main(monkeypatch, capsys, "--wait", "--after", REREVIEW_AT, "--timeout", "0")
        assert out["wait"]["status"] == earlier
        assert run_block()["wait"]["last_snapshot"]["status"] == earlier

        loop["payload"] = FIXTURE["after_rereview"]
        run_main(
            monkeypatch, capsys, "--wait", "--after", REREVIEW_AT,
            "--timeout", "600", "--quiet-period", "0",
        )
        run = run_block()
        assert run["phase"] == "fetched"
        assert "wait" not in run
        heartbeat, _ = run_main(monkeypatch, capsys, "--wait-heartbeat", fmt=None)
        assert "no reviewer wait in progress" in heartbeat


class TestCopilotRequestPhase:
    PENDING = {
        "state": "PENDING", "head": "feed0000", "pending_since": "2026-10-16T10:00:00Z",
        "first_completed_review_at": "2026-10-16T09:00:00Z",
    }

    def _request_pending(self, tmp_path, monkeypatch, capsys, *extra):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: "agent")
        monkeypatch.setattr(request_rereview, "request_state", lambda *a, **k: dict(self.PENDING))
        rc = request_rereview.main([
            "--repo", "acme/widget", "--pr", "159",
            "--reviewer-login", "copilot-pull-request-reviewer", "--json", *extra,
        ])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["status"] == "review_pending"

    def test_a_pending_request_records_requested_with_pending_since(self, tmp_path, monkeypatch, capsys):
        """A request GitHub still lists is this cycle's request: the loop
        waits on it, so the phase says so and the wait anchor rides along."""
        self._request_pending(tmp_path, monkeypatch, capsys)
        run = loop_state.load_sticky_state()[KEY]["run"]
        assert run["phase"] == "requested"
        assert run["requested_at"] == "2026-10-16T10:00:00Z"
        assert run["requested_head"] == "feed0000"

    def test_a_dry_run_against_a_pending_request_records_nothing(self, tmp_path, monkeypatch, capsys):
        """Previewing is read-only: the pending branch returns before the
        posting path's dry-run guard, so it carries its own."""
        self._request_pending(tmp_path, monkeypatch, capsys, "--dry-run")
        assert KEY not in loop_state.load_sticky_state()


class TestMarkPhase:
    def test_creates_a_run_block_the_hooks_still_ignore(self, tmp_path, monkeypatch):
        """A ping-first vendor's opening request lands before any fetch. The
        phase is recorded, but a run with no update_seq is not active."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert loop_state.mark_phase(KEY, "requested", requested_at="t", requested_head=None) is None
        run = loop_state.load_sticky_state()[KEY]["run"]
        assert run["phase"] == "requested"
        assert run["requested_at"] == "t"
        assert run["requested_head"] is None
        assert not loop_state.any_active_run()

    def test_returns_the_previous_phase_and_warns_only_on_the_bad_pair(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert loop_state.mark_phase(KEY, "fetched") is None
        assert loop_state.mark_phase(KEY, "summarized") == "fetched"
        assert loop_state.mark_phase(KEY, "requested") == "summarized"
        assert loop_state.mark_phase(KEY, "waiting") == "requested"
        assert loop_state.mark_phase(KEY, "fetched") == "waiting"
        assert capsys.readouterr().err == ""
        assert loop_state.mark_phase(KEY, "requested") == "fetched"
        assert "loop phase fetched -> requested" in capsys.readouterr().err
