"""loop_status.py: where the loop is, from local state only, and the one next command.

The resume ladder in references/resume-and-recovery.md used to be prose the
agent applied by hand. ``decide_next`` implements it; these tests pin every
row of the table, the PR selection, the git facts, and the CLI contract.
"""

from __future__ import annotations

import json
import os
import shlex
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
            (dict(phase="waiting", run=run("waiting", wait={"after": T, "last_snapshot": {"status": "settling", "next_wait_seconds": 300}})), "continue the wait (settling)", f"--wait --after {T} --wait-chunk-seconds 300 --timeout 1800"),
            (dict(phase="summarized", run=run("summarized", cap_reached=True), git={**GIT, "ahead": 1}), "push the fixes (1", "git push"),
            (dict(phase="summarized", run=run("summarized", last_head_oid="aaaaaaa1")), "nothing pushed since the fetch", "--cycle-summary"),
            (dict(phase="summarized", run=run("summarized", last_head_oid="0000000")), "request the re-review", "--expect-head"),
            (dict(phase="waiting", run=run("waiting", requested_at=T, requested_head="aaaaaaa1", wait={"after": T, "last_snapshot": {"status": "timed_out"}}), git={**GIT, "ahead": 1}), "push (1 local", "git push"),
            (dict(phase="waiting", run=run("waiting", requested_at=T, requested_head="bbbbbbb2", wait={"after": T, "last_snapshot": {"status": "waiting"}})), "head moved", "request_rereview.py"),
            (dict(phase="fetched", run=run("fetched", open_actionable=0, clean_blocked_by=None)), "record the run as clean", "--record-run --outcome clean"),
            (dict(phase="fetched", run=run("fetched", open_actionable=0, clean_blocked_by="no_review_for_head")), "head has no review yet: request one", "request_rereview.py"),
            (dict(phase="fetched", run=run("fetched", open_actionable=0, clean_blocked_by="overview_unparseable")), "overview blocks clean (overview_unparseable)", f"--pr {URL}"),
            (dict(phase="summarized", run=run("summarized", cap_used=3, cap_limit=5, cap_reached=False)), "request the re-review", "--json --max-rereview-requests 5"),
            (dict(phase="summarized", run=run("summarized", open_actionable=0, clean_blocked_by=None, last_head_oid="aaaaaaa1")), "record the run as clean", "--outcome clean"),
            (dict(phase="summarized", run=run("summarized", open_actionable=0, clean_blocked_by=None, cap_reached=True)), "record the run as clean", "--outcome clean"),
            (dict(phase="fetched", run=run("fetched", open_actionable=0, clean_blocked_by=None, ci_failing=["tests", "lint"])), "2 head check(s) failed (tests, lint)", "--outcome ci_failed --outcome-reason tests "),
            (dict(phase="fetched", run=run("fetched", open_actionable=0, clean_blocked_by=None, ci_pending=2)), "checks are still running", f"--pr {URL}"),
            (dict(phase="fetched", run=run("fetched", open_actionable=1, ci_failing=["tests"])), "cycle summary", "--cycle-summary"),
            (dict(phase="waiting", run=run("waiting", requested_at="2026-11-12T12:00:00Z", wait={"after": T, "last_snapshot": {"status": "settling", "next_wait_seconds": 300}})), "did not finish", "--wait --after 2026-11-12T12:00:00Z --timeout 1800"),
            (dict(phase="waiting", run=run("waiting", requested_at=T)), "did not finish", f"--wait --after {T} --timeout 1800"),
            (dict(phase="bogus", run=run("bogus")), "unknown phase", f"--pr {URL}"),
            (dict(phase="summarized", run=run("summarized", open_actionable=0, clean_blocked_by=None, last_head_oid="0000000")), "request the re-review", "request_rereview.py"),
            (dict(phase="fetched", run=run("fetched", open_actionable=0, clean_blocked_by=None, last_head_oid="0000000")), "HEAD moved since (0000000 -> aaaaaaa)", "--cycle-summary"),
            (dict(phase="fetched", run=run("fetched", open_actionable=0, clean_blocked_by=None, last_head_oid="aaaaaaa1")), "record the run as clean", "--outcome clean"),
            (dict(phase="summarized", run=run("summarized", open_actionable=0, ci_failing=["tests"], last_head_oid="0000000")), "request the re-review", "request_rereview.py"),
            (dict(phase="fetched", run=run("fetched", open_actionable=2, no_progress=True, last_head_oid="aaaaaaa1")), "(no_progress)", "--record-run --outcome no_progress"),
            (dict(phase="summarized", run=run("summarized", open_actionable=2, no_progress=True, last_head_oid="aaaaaaa1")), "(no_progress)", "--outcome no_progress"),
            (dict(phase="fetched", run=run("fetched", open_actionable=2, no_progress=True, last_head_oid="0000000")), "fix the findings", "--cycle-summary"),
            (dict(phase="fetched", run=run("fetched", open_actionable=0, clean_blocked_by=None, no_progress=True, last_head_oid="aaaaaaa1")), "record the run as clean", "--outcome clean"),
            (dict(phase="fetched", run=run("fetched", open_actionable=0, clean_blocked_by="no_review_for_head", cap_reached=True)), "the cap is reached: record the run as capped", "--record-run --outcome capped"),
            (dict(phase="summarized", run=run("summarized", open_actionable=0, clean_blocked_by="no_review_for_head", cap_reached=True, last_head_oid="aaaaaaa1")), "review_pending", "--outcome capped"),
            (dict(phase="fetched", run=run("fetched", open_actionable=2, session_cycle=2, cycle_new_findings=2, cycle_fix_induced_fps=["a", "b"], last_head_oid="aaaaaaa1")), "churn: all 2 new finding(s)", "--record-run --outcome churn"),
            (dict(phase="summarized", run=run("summarized", open_actionable=1, session_cycle=3, cycle_new_findings=1, cycle_fix_induced_fps=["a"], last_head_oid="aaaaaaa1")), "ignore churn", "--outcome churn"),
            (dict(phase="fetched", run=run("fetched", open_actionable=1, session_cycle=1, cycle_new_findings=1, cycle_fix_induced_fps=["a"])), "fix the findings", "--cycle-summary"),
            (dict(phase="fetched", run=run("fetched", open_actionable=2, session_cycle=2, cycle_new_findings=2, cycle_fix_induced_fps=["a"])), "fix the findings", "--cycle-summary"),
            (dict(phase="fetched", run=run("fetched", open_actionable=2, session_cycle=2, cycle_new_findings=2, cycle_fix_induced_fps=["a", "b"], last_head_oid="0000000")), "fix the findings", "--cycle-summary"),
        ],
        ids=[
            "no-reviewer", "reservation", "no-run", "last-run", "fetched-no-profile",
            "fetched-capped", "fetched", "summarized-capped", "summarized-ahead",
            "summarized-pushed", "requested-ahead", "requested-head-moved",
            "requested-wait", "requested-no-anchor", "waiting-quota", "waiting-withdrawn",
            "waiting-timed-out", "waiting-settling", "waiting-no-snapshot", "unknown",
            "summarized-capped-but-unpushed", "summarized-backstop-nothing-pushed",
            "summarized-pushed-since-fetch", "waiting-ahead", "waiting-head-moved",
            "fetched-clean", "fetched-no-head-review", "fetched-overview-blocked", "summarized-cap-forwarded",
            "summarized-after-backstop-clean", "summarized-clean-beats-cap", "fetched-ci-failed",
            "fetched-ci-pending", "fetched-ci-failed-with-threads", "waiting-stale-anchor",
            "summarized-clean-but-pushed-since", "fetched-clean-but-head-moved",
            "fetched-clean-same-head", "summarized-ci-failed-but-pushed-since",
            "fetched-no-progress", "summarized-no-progress", "fetched-no-progress-but-head-moved",
            "fetched-clean-beats-no-progress", "fetched-no-head-review-capped",
            "summarized-no-head-review-capped", "fetched-churn", "summarized-churn",
            "churn-needs-cycle-2", "churn-needs-every-new-finding", "churn-but-head-moved",
        ],
    )
    def test_table(self, over, action, command):
        nxt = loop_status.decide_next(facts(**over))
        assert action in nxt["action"], nxt
        assert command in (nxt["command"] or ""), nxt

    def test_commands_bind_git_to_the_selected_checkout(self):
        bound = loop_status.commands(REPO, 159, "/work/b")
        assert bound["push"] == "git -C /work/b push"
        assert "$(git -C /work/b rev-parse HEAD)" in bound["request"]
        assert "$(git -C /work/b rev-parse --show-toplevel)" in bound["detect_profile"]
        assert loop_status.commands(REPO, 159)["push"] == "git push"

    def test_a_checkout_path_is_shell_quoted(self):
        bound = loop_status.commands(REPO, 159, '/work/my "b" $HOME')
        assert bound["push"] == "git -C '/work/my \"b\" $HOME' push"

    def test_a_check_name_with_a_quote_survives_the_shell(self):
        nxt = loop_status.decide_next(facts(
            phase="fetched",
            run=run("fetched", open_actionable=0, clean_blocked_by=None, ci_failing=["Don't regress"]),
        ))
        reason = nxt["command"].split("--outcome-reason ", 1)[1].rsplit(" --verification", 1)[0]
        proc = subprocess.run(["sh", "-c", f"printf %s {reason}"], capture_output=True, text=True, check=True)
        assert proc.stdout == "Don't regress"

    @pytest.mark.parametrize("phase, extra", [
        ("fetched", {}),
        ("fetched", {"open_actionable": 0, "clean_blocked_by": None, "ci_pending": 1}),
        ("fetched", {"cap_reached": True}),
        ("summarized", {"last_head_oid": "aaaaaaa1"}),
        ("summarized", {}),
        ("requested", {"requested_at": T}),
        ("waiting", {"wait": {"after": T, "last_snapshot": {"status": "timed_out"}}}),
    ])
    def test_every_script_command_carries_the_runs_cap(self, phase, extra):
        """Without it the request script refuses as capped, and a fetch,
        summary, wait or record writes the preference back over the run's
        --max-rereview-requests override."""
        nxt = loop_status.decide_next(facts(
            phase=phase, run=run(phase, cap_used=3, cap_limit=5, **extra),
        ))
        assert "--max-rereview-requests 5" in nxt["command"], nxt

    def test_a_wrong_checkout_never_gets_a_push_or_request(self):
        mismatch = "the checkout is on other, not the PR's branch fix/widget"
        for over in (
            dict(phase="summarized", run=run("summarized"), git={**GIT, "ahead": 2}),
            dict(phase="summarized", run=run("summarized")),
            dict(phase="requested", run=run("requested", requested_at=T, requested_head="bbbbbbb2")),
            dict(reservation_open=True, phase="summarized"),
        ):
            nxt = loop_status.decide_next(facts(checkout_mismatch=mismatch, **over))
            assert nxt["action"].startswith(mismatch + ": re-run status from the PR's checkout"), nxt
            assert "loop_status.py" in nxt["command"] and "--cwd <the PR's checkout>" in nxt["command"]
        # A command that does not act on the checkout is unaffected.
        nxt = loop_status.decide_next(facts(
            checkout_mismatch=mismatch, phase="requested", run=run("requested", requested_at=T),
        ))
        assert "--wait --after" in nxt["command"]


class TestCheckoutMismatch:
    PR_RUN = {"head_ref": "fix/widget", "head_repo": "acme/widget"}

    @pytest.mark.parametrize("git, expected", [
        ({"branch": "fix/widget", "upstream_branch": "fix/widget", "upstream_repo": "Acme/Widget"}, None),
        ({"branch": "local-name", "upstream_branch": "fix/widget", "upstream_repo": "acme/widget"}, None),
        ({"branch": "fix/widget", "upstream_branch": None, "upstream_repo": None}, None),
        ({"branch": "other", "upstream_branch": "other", "upstream_repo": "acme/widget"}, "tracks acme/widget:other"),
        ({"branch": "fix/widget", "upstream_branch": "fix/widget", "upstream_repo": "someone/widget"}, "tracks someone/widget:fix/widget"),
        ({"branch": "other", "upstream_branch": None, "upstream_repo": None}, "is on other"),
        ({"branch": None}, "HEAD is detached"),
    ])
    def test_branch_and_repo(self, git, expected):
        got = loop_status.checkout_mismatch({"head": "h", **git}, self.PR_RUN)
        if expected is None:
            assert got is None
        else:
            assert expected in got and "acme/widget:fix/widget" in got

    def test_unknown_without_a_recorded_branch(self):
        assert loop_status.checkout_mismatch({"head": "h", "branch": "other"}, {}) is None

    def test_local_git_reads_the_branch_and_what_it_tracks(self, tmp_path):
        checkout = _checkout(tmp_path)
        _track_upstream(tmp_path, checkout)
        facts_ = loop_status.local_git(str(checkout))
        assert facts_["branch"] == "main" and facts_["upstream_branch"] == "main"
        assert facts_["upstream_repo"] is None  # the bare remote is not on GitHub
        _git(checkout, "checkout", "-q", "--detach")
        assert loop_status.local_git(str(checkout))["branch"] is None

    def test_another_prs_branch_skips_the_git_facts(self, tmp_path):
        checkout = _checkout(tmp_path)
        _track_upstream(tmp_path, checkout)
        _git(checkout, "commit", "-q", "--allow-empty", "-m", "unrelated")
        status = loop_status.build_status(
            REPO, 159, {"reviewer": REVIEWER, "run": run("summarized", head_ref="fix/widget")},
            git=loop_status.local_git(str(checkout)), git_repo=REPO,
            profile="confirmed", sentinel="absent", reservation_open=False, provenance_incomplete=False,
        )
        assert status["git"]["ahead"] is None
        assert status["checkoutMismatch"] == "the checkout's branch main tracks main, not the PR's branch fix/widget"
        assert "git facts skipped: the checkout's branch main tracks main" in " ".join(status["warnings"])
        assert "re-run status from the PR's checkout" in status["next"]["action"]

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

    def test_a_fork_checkout_qualifies_through_its_upstream_remote(self, tmp_path):
        checkout = _checkout(tmp_path)
        _git(checkout, "remote", "set-url", "origin", "https://github.com/someone/widget.git")
        _git(checkout, "remote", "add", "upstream", "git@github.com:Acme/Widget.git")
        assert loop_status.repo_from_origin(str(checkout)) == "someone/widget"
        assert loop_status.repos_from_remotes(str(checkout)) == {"someone/widget", "acme/widget"}
        status = loop_status.build_status(
            REPO, 159, {"reviewer": REVIEWER, "run": run("summarized")},
            git={"head": "h", "upstream": "u", "ahead": 1, "behind": 0},
            git_repo="someone/widget", git_repos=loop_status.repos_from_remotes(str(checkout)),
            profile=None, sentinel="absent", reservation_open=False, provenance_incomplete=False,
        )
        assert status["git"]["ahead"] == 1
        assert status["next"]["action"].startswith("push the fixes (1")
        assert not any("git facts skipped" in w for w in status["warnings"])

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
        assert lines[-1] == f"  git -C {shlex.quote(str(checkout))} push"
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

    def test_a_preference_at_or_below_the_stored_cap_is_not_a_change(self, tmp_path, monkeypatch, capsys):
        """The fetch may have run with --max-rereview-requests 5 while the
        preference still says 3; the override won then and must not be
        undone by status, which would turn cap_used=3 into capped."""
        self._seed(tmp_path, monkeypatch, {
            "reviewer": REVIEWER,
            "run": run("fetched", cap_used=3, cap_limit=5, cap_reached=False, cap_count_complete=True),
        })
        (tmp_path / "preferences.json").write_text(json.dumps({
            "max_rereview_requests": 3, "profiles": {REPO: {"source": "confirmed"}},
        }))
        _, out, _ = self._run(capsys, "--pr", URL)
        assert "3/5 re-review requests used" in out
        assert "cap was raised" not in out
        assert "Next: fix the findings" in out

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
