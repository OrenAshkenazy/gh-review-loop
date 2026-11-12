"""loop_status.py: where the loop is, from local state only, and the one next command.

The resume ladder in references/resume-and-recovery.md used to be prose the
agent applied by hand. ``decide_next`` implements it; these tests pin every
row of the table, the PR selection, the git facts, and the CLI contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

import loop_state
import loop_status

REPO = "acme/widget"
KEY = "acme/widget#159"
URL = "https://github.com/acme/widget/pull/159"
REVIEWER = {
    "login": "chatgpt-codex-connector", "display_name": "Codex",
    "auto_reviews": False, "trigger_kind": "comment",
}
GIT = {"head": "aaaaaaa1", "upstream": "aaaaaaa1", "ahead": 0, "behind": 0}
T = "2026-11-12T10:00:00Z"


def facts(**over):
    base = {
        "commands": loop_status.commands(REPO, 159),
        "reviewer": REVIEWER, "phase": None, "run": {}, "git": GIT,
        "profile": "confirmed", "reservation_open": False, "last_run": None,
    }
    base.update(over)
    return base


def run(phase, **fields):
    return {"phase": phase, "phase_at": T, "update_seq": 1, "last_summary_seq": 1, **fields}


class TestDecideNext:
    @pytest.mark.parametrize(
        "over, action, command",
        [
            (dict(reviewer=None), "select a reviewer", "--list-reviewers"),
            (dict(reservation_open=True, phase="summarized"), "settle the earlier unconfirmed", "request_rereview.py"),
            (dict(), "fetch the reviewer's threads", f"--pr {URL}"),
            (dict(last_run={"outcome": "clean", "recorded_at": T}), "last one ended clean", f"--pr {URL}"),
            (dict(phase="fetched", profile=None), "verification profile", "detect_profile.py"),
            (dict(phase="fetched", run=run("fetched", cap_reached=True)), "cap reached", "--record-run --outcome capped"),
            (dict(phase="fetched", run=run("fetched")), "cycle summary", "--cycle-summary"),
            (dict(phase="summarized", run=run("summarized", cap_reached=True)), "cap reached", "--outcome capped"),
            (dict(phase="summarized", run=run("summarized"), git={**GIT, "ahead": 2}), "push the fixes (2", "git push"),
            (dict(phase="summarized", run=run("summarized")), "request the re-review", '--expect-head "$(git rev-parse HEAD)"'),
            (dict(phase="requested", run=run("requested", requested_at=T), git={**GIT, "ahead": 1}), "push (1 local", "git push"),
            (dict(phase="requested", run=run("requested", requested_at=T, requested_head="bbbbbbb2")), "head moved", "request_rereview.py"),
            (dict(phase="requested", run=run("requested", requested_at=T, requested_head="aaaaaaa1")), "wait for the review that answers", f"--wait --after {T} --timeout 1800"),
            (dict(phase="requested", run=run("requested")), "request time is not recorded", "--after <request time>"),
            (dict(phase="waiting", run=run("waiting", wait={"after": T, "last_snapshot": {"status": "refused", "kind": "quota_exhausted"}})), "out of quota", "--outcome human --outcome-reason 'reviewer quota exhausted'"),
            (dict(phase="waiting", run=run("waiting", wait={"after": T, "last_snapshot": {"status": "refused", "kind": "withdrawn"}})), "refused the review", "--outcome human --outcome-reason 'reviewer refused the review'"),
            (dict(phase="waiting", run=run("waiting", wait={"after": T, "last_snapshot": {"status": "timed_out"}})), "timed out", f"--wait --after {T} --timeout 1800"),
            (dict(phase="waiting", run=run("waiting", wait={"after": T, "last_snapshot": {"status": "settling", "next_wait_seconds": 300}})), "continue the wait (settling)", f"--wait --after {T} --wait-chunk-seconds 300"),
            (dict(phase="waiting", run=run("waiting", requested_at=T)), "did not finish", f"--wait --after {T} --timeout 1800"),
            (dict(phase="bogus", run=run("bogus")), "unknown phase", f"--pr {URL}"),
        ],
        ids=[
            "no-reviewer", "reservation", "no-run", "last-run", "fetched-no-profile",
            "fetched-capped", "fetched", "summarized-capped", "summarized-ahead",
            "summarized-pushed", "requested-ahead", "requested-head-moved",
            "requested-wait", "requested-no-anchor", "waiting-quota", "waiting-withdrawn",
            "waiting-timed-out", "waiting-settling", "waiting-no-snapshot", "unknown",
        ],
    )
    def test_table(self, over, action, command):
        nxt = loop_status.decide_next(facts(**over))
        assert action in nxt["action"], nxt
        assert command in (nxt["command"] or ""), nxt

    def test_reviewer_outranks_everything_and_reservation_outranks_phase(self):
        assert "select a reviewer" in loop_status.decide_next(
            facts(reviewer=None, reservation_open=True, phase="waiting")
        )["action"]
        assert "settle" in loop_status.decide_next(
            facts(reservation_open=True, phase="fetched", profile=None)
        )["action"]


class TestSelectEntry:
    def test_active_run_beats_a_bare_run_block_beats_a_last_run(self):
        state = {
            "acme/widget#1": {"last_run": {"outcome": "clean", "recorded_at": "2026-11-12T09:00:00Z"}},
            "acme/widget#2": {"run": {"phase": "requested", "phase_at": "2026-11-12T09:30:00Z"}},
            "acme/widget#3": {"run": {"update_seq": 2, "started_at": "2026-11-12T08:00:00Z"}},
            "other/repo#9": {"run": {"update_seq": 5, "started_at": "2026-11-12T11:00:00Z"}},
        }
        assert loop_status.select_entry(state, REPO)[1] == 3
        del state["acme/widget#3"]
        assert loop_status.select_entry(state, REPO)[1] == 2
        del state["acme/widget#2"]
        assert loop_status.select_entry(state, REPO)[1] == 1
        del state["acme/widget#1"]
        assert loop_status.select_entry(state, REPO) is None

    def test_pr_override_matches_the_key_case_insensitively(self):
        state = {"Acme/Widget#159": {"run": {"phase": "fetched"}}}
        key, number, entry = loop_status.select_entry(state, "acme/widget", 159)
        assert (key, number, entry["run"]["phase"]) == ("Acme/Widget#159", 159, "fetched")
        assert loop_status.select_entry(state, REPO, 7) == ("acme/widget#7", 7, {})


class TestLocalFacts:
    @pytest.mark.parametrize("url", [
        "https://github.com/Acme/Widget.git", "https://github.com/Acme/Widget",
        "git@github.com:Acme/Widget.git", "ssh://git@github.com/Acme/Widget.git",
    ])
    def test_origin_url_forms(self, url):
        assert loop_status.parse_origin_url(url) == "Acme/Widget"

    def test_parse_pr_arg(self):
        assert loop_status.parse_pr_arg(URL, None) == (REPO, 159)
        assert loop_status.parse_pr_arg("159", REPO) == (REPO, 159)
        assert loop_status.parse_pr_arg("159", None) is None
        assert loop_status.parse_pr_arg("nope", REPO) is None

    def test_git_facts_from_a_real_checkout(self, tmp_path):
        checkout = _checkout(tmp_path)
        assert loop_status.repo_from_origin(str(checkout)) == REPO
        before = loop_status.local_git(str(checkout))
        assert before["head"] and before["upstream"] is None and before["ahead"] is None
        _track_upstream(tmp_path, checkout)
        tracked = loop_status.local_git(str(checkout))
        assert tracked["ahead"] == 0 and tracked["behind"] == 0
        _git(checkout, "commit", "-q", "--allow-empty", "-m", "fix")
        assert loop_status.local_git(str(checkout))["ahead"] == 1

    def test_profile_and_cap_come_from_preferences_without_writing_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert loop_status.profile_source(REPO) is None
        assert not (tmp_path / "preferences.json").exists()  # read-only: never created
        (tmp_path / "preferences.json").write_text(json.dumps({
            "max_rereview_requests": "4",
            "profiles": {"Acme/Widget": {"source": "skipped"}},
        }))
        prefs = loop_status.read_preferences()
        assert loop_status.profile_source(REPO, prefs) == "skipped"
        assert loop_status.preferred_cap(prefs) == 4


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _checkout(tmp_path):
    """A git checkout whose origin is this PR's repo on GitHub (never fetched)."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "t")
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "init")
    _git(checkout, "remote", "add", "origin", "https://github.com/acme/widget.git")
    return checkout


def _track_upstream(tmp_path, checkout):
    """Give main an upstream on a local bare repo so ahead/behind can be read."""
    bare = tmp_path / "bare.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(checkout, "remote", "add", "bare", str(bare))
    _git(checkout, "push", "-q", "bare", "main")
    _git(checkout, "branch", "--set-upstream-to=bare/main", "main")


class TestCli:
    @staticmethod
    def _seed(tmp_path, monkeypatch, entry, **others):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        loop_state.save_sticky_state({KEY: entry, **others})

    @staticmethod
    def _run(capsys, *args):
        rc = loop_status.main(["--no-color", *args])
        out, err = capsys.readouterr()
        return rc, out, err

    def test_text_block_ends_with_the_one_next_command(self, tmp_path, monkeypatch, capsys):
        checkout = _checkout(tmp_path)
        _track_upstream(tmp_path, checkout)
        _git(checkout, "commit", "-q", "--allow-empty", "-m", "fix")
        self._seed(tmp_path, monkeypatch, {
            "reviewer": REVIEWER,
            "run": run("summarized", session_cycle=2, cap_used=1, cap_limit=3, cap_reached=False,
                       cap_count_complete=True),
        })
        rc, out, _ = self._run(capsys, "--cwd", str(checkout))
        assert rc == 0
        lines = out.rstrip("\n").splitlines()
        assert lines[0] == "[loop] status: acme/widget#159"
        assert "Reviewer: Codex (chatgpt-codex-connector); reviews on request" in lines
        assert any(line.startswith("Phase: summarized since") and "session cycle 2; 1/3 re-review" in line for line in lines)
        assert any(line.startswith("Local: HEAD") and "1 ahead, 0 behind" in line for line in lines)
        assert "Profile: none saved" in lines
        assert lines[-2].startswith("Next: push the fixes (1")
        assert lines[-1] == "  git push"
        assert "\033[" not in out

    def test_json_is_the_same_decision_machine_readable(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path, monkeypatch, {
            "reviewer": REVIEWER, "run": run("requested", requested_at=T, requested_head="aaaaaaa1"),
        })
        rc, out, _ = self._run(capsys, "--pr", URL, "--format", "json")
        assert rc == 0 and "\033[" not in out
        status = json.loads(out)
        assert status["phase"] == "requested"
        assert status["requestedAt"] == T
        assert status["next"]["command"].endswith(f"--wait --after {T} --timeout 1800")
        # Outside a checkout the git facts are skipped, and say so.
        assert status["git"]["head"] is None
        assert any("git facts skipped" in w for w in status["warnings"])

    def test_pr_number_needs_a_checkout_and_a_url_does_not(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        rc, _, err = self._run(capsys, "--pr", "159", "--cwd", str(tmp_path))
        assert rc == 2 and "--pr must be" in err
        rc, out, _ = self._run(capsys, "--pr", URL, "--cwd", str(tmp_path))
        assert rc == 0 and "Next: select a reviewer" in out

    def test_no_state_for_the_checkout_repo(self, tmp_path, monkeypatch, capsys):
        checkout = _checkout(tmp_path)
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path / "state"))
        rc, out, _ = self._run(capsys, "--cwd", str(checkout))
        assert rc == 0
        assert out.startswith("[loop] status: no loop recorded for acme/widget")
        assert "--pr <url>" in out

    def test_last_run_is_reported_when_the_run_block_is_gone(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path, monkeypatch, {
            "reviewer": REVIEWER,
            "last_run": {"outcome": "capped", "recorded_at": T, "session_cycle": 3},
        })
        rc, out, _ = self._run(capsys, "--pr", URL)
        assert "Phase: none; last run ended capped at " + T in out
        assert "Next: start a new loop; the last one ended capped" in out

    def test_an_open_reservation_sends_you_to_the_request_script(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path, monkeypatch, {"reviewer": REVIEWER, "run": run("waiting")})
        path = loop_state.unconfirmed_request_path(REPO, 159)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"at": T, "kind": "maybe"}))
        _, out, _ = self._run(capsys, "--pr", URL)
        assert "Next: settle the earlier unconfirmed re-review request" in out
        assert "request_rereview.py" in out.splitlines()[-1]

    def test_warnings_for_provenance_sentinel_and_stale_summary(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path, monkeypatch, {
            "reviewer": REVIEWER,
            "run": run("requested", update_seq=3, last_summary_seq=2, requested_at=T),
        })
        loop_state.mark_provenance_incomplete(REPO, 159)
        _, out, _ = self._run(capsys, "--pr", URL)
        assert "Warning: re-review provenance is incomplete" in out
        assert "Warning: loop-active marker missing" in out
        assert "Warning: the run advanced since the last cycle summary" in out
        loop_state.touch_sentinel()
        old = time.time() - loop_state.SENTINEL_TTL_SECONDS - 60
        os.utime(loop_state.sentinel_path(), (old, old))
        _, out, _ = self._run(capsys, "--pr", URL)
        assert "Warning: loop-active marker older than 24h" in out

    def test_a_cap_raised_in_preferences_lifts_capped_before_any_fetch(self, tmp_path, monkeypatch, capsys):
        """Resume case 1: the user raised the cap; the stored cap_reached is
        from the last fetch and must not keep saying capped."""
        self._seed(tmp_path, monkeypatch, {
            "reviewer": REVIEWER,
            "run": run("fetched", cap_used=3, cap_limit=3, cap_reached=True, cap_count_complete=True),
        })
        (tmp_path / "preferences.json").write_text(json.dumps({
            "max_rereview_requests": 5, "profiles": {REPO: {"source": "confirmed"}},
        }))
        _, out, _ = self._run(capsys, "--pr", URL)
        assert "3/5 re-review requests used" in out
        assert "Warning: the cap was raised to 5 since the last fetch" in out
        assert "Next: fix the findings, verify, then emit the cycle summary" in out
        assert "Profile: confirmed" in out

    def test_a_kept_refusal_is_shown_and_decides_the_next_step(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path, monkeypatch, {
            "reviewer": REVIEWER,
            "run": run("waiting", wait={"after": T, "last_snapshot": {"status": "refused", "kind": "withdrawn"}}),
        })
        _, out, _ = self._run(capsys, "--pr", URL)
        assert "Wait: refused (withdrawn)" in out
        assert "Next: the reviewer refused the review: record the run as human" in out
        assert out.rstrip().splitlines()[-1].endswith("--gemini-unconfirmed")

    def test_runs_as_a_script_without_the_test_path(self, tmp_path, monkeypatch):
        """The hooks and the skill invoke it by path; it must import its
        siblings on its own."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        proc = subprocess.run(
            [sys.executable, "-I", str(loop_status.__file__), "--pr", URL, "--format", "json"],
            capture_output=True, text=True, env={**os.environ, "GGRL_STATE_DIR": str(tmp_path)},
            cwd=str(tmp_path),
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["next"]["action"].startswith("select a reviewer")
