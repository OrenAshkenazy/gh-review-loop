"""CI on the head and churn: two things the thread filter never sees.

- a failing or pending check on the head (``ci_status`` → ``[loop] CI``);
- new findings on lines the loop's own fixes changed
  (``track_head_and_fix_induced`` → ``[loop] churn``).

Plus the Codex clean receipt, found live while running the loop on PR #123.
No network: payloads are built inline, git runs on a throwaway repository,
and the sticky receipt write is stubbed where main() is driven.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import fetch_gemini_threads as fgt
import metrics
from fetch_gemini_threads import (
    PullRequest,
    changed_line_ranges,
    churn_detected,
    ci_status,
    finding_fingerprint,
    fix_induced_findings,
    parse_changed_line_ranges,
    read_run_tracking,
    track_head_and_fix_induced,
)

BOT = "gemini-code-assist"
AGENT = "loop-agent"
PR = PullRequest(owner="acme", repo="widget", number=7)


def _check(name, conclusion, *, status="COMPLETED", required=False, url=None):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": url,
        "isRequired": required,
    }


def _status(context, state, *, required=False, url=None):
    return {
        "__typename": "StatusContext",
        "context": context,
        "state": state,
        "targetUrl": url,
        "isRequired": required,
    }


def _pr_with_checks(*contexts, state="FAILURE"):
    return {
        "commits": {"nodes": [{"commit": {
            "committedDate": "2026-11-06T10:00:00Z",
            "statusCheckRollup": {"state": state, "contexts": {"nodes": list(contexts)}},
        }}]},
    }


# ---------------------------------------------------------------------------
# CI on the head
# ---------------------------------------------------------------------------

class TestCiStatus:
    def test_query_asks_for_check_names_conclusions_and_required(self):
        assert "conclusion" in fgt.QUERY
        assert "detailsUrl" in fgt.QUERY
        assert fgt.QUERY.count("isRequired(pullRequestNumber:$number)") == 2

    def test_no_rollup_reads_as_no_checks(self):
        assert ci_status({}) == {
            "state": None, "total": 0, "pending": 0, "failing": [], "truncated": False,
        }

    def test_failing_check_run_carries_a_log_command(self):
        url = "https://github.com/acme/widget/actions/runs/123456/job/7890"
        ci = ci_status(_pr_with_checks(
            _check("tests", "FAILURE", required=True, url=url),
            _check("lint", "SUCCESS"),
        ))
        assert ci["total"] == 2 and ci["pending"] == 0
        assert ci["failing"] == [{
            "name": "tests", "state": "failure", "required": True, "url": url,
            "log_command": "gh run view 123456 --repo acme/widget --log-failed",
        }]

    def test_cancelled_skipped_and_neutral_are_not_failures(self):
        # A cancelled job is usually a superseded run; Claude Code Review's
        # own check never concludes as a failure.
        ci = ci_status(_pr_with_checks(
            _check("superseded", "CANCELLED"),
            _check("docs", "SKIPPED"),
            _check("Claude Code Review", "NEUTRAL"),
        ))
        assert ci["failing"] == [] and ci["pending"] == 0 and ci["total"] == 3

    def test_in_progress_and_queued_count_as_pending(self):
        ci = ci_status(_pr_with_checks(
            _check("tests", None, status="IN_PROGRESS"),
            _check("build", None, status="QUEUED"),
        ))
        assert ci["pending"] == 2 and ci["failing"] == []

    def test_status_contexts_fail_and_pend_like_check_runs(self):
        ci = ci_status(_pr_with_checks(
            _status("ci/circleci", "FAILURE", url="https://circleci.com/gh/acme/widget/1"),
            _status("ci/deploy", "PENDING"),
            _status("ci/other", "ERROR", required=True),
        ))
        assert [c["name"] for c in ci["failing"]] == ["ci/circleci", "ci/other"]
        assert ci["failing"][0]["log_command"] is None  # not GitHub Actions
        assert ci["failing"][1]["required"] is True
        assert ci["pending"] == 1

    def test_full_contexts_page_is_flagged_truncated(self):
        ci = ci_status(_pr_with_checks(*[_check(f"c{i}", "SUCCESS") for i in range(50)]))
        assert ci["truncated"] is True


class TestCiBlock:
    def test_green_head_prints_nothing(self):
        ci = ci_status(_pr_with_checks(_check("tests", "SUCCESS"), state="SUCCESS"))
        assert metrics.format_ci_block(ci) == ""

    def test_failing_block_names_the_check_its_log_and_the_stop(self):
        url = "https://github.com/acme/widget/actions/runs/5/job/6"
        ci = ci_status(_pr_with_checks(_check("tests", "FAILURE", required=True, url=url)))
        block = metrics.format_ci_block(ci)
        assert block.startswith("[loop] CI: 1 failing check(s) on the head, 1 required (1 total).")
        assert "  - tests [required]: failure" in block
        assert "log: gh run view 5 --repo acme/widget --log-failed" in block
        assert "does not fix it" in block
        assert "--outcome ci_failed" in block

    def test_pending_only_warns_that_the_reviewer_may_not_have_seen_it(self):
        ci = ci_status(_pr_with_checks(_check("tests", None, status="IN_PROGRESS")))
        assert metrics.format_ci_block(ci) == (
            "[loop] CI: 1 check(s) still running on the head — a reviewer that "
            "waits for CI has not seen it yet."
        )

    def test_incomplete_ci_results_are_named_not_read_as_green(self):
        # Codex, PR #123: a full page of 50 green contexts can hide the red one.
        full_page = _pr_with_checks(*[_check(f"c{i}", "SUCCESS") for i in range(50)], state="FAILURE")
        block = metrics.format_ci_block(ci_status(full_page))
        assert block.startswith("[loop] CI: results incomplete — the head rollup state is FAILURE")
        assert "page of 50 full" in block
        assert "do not read this as green" in block
        odd = ci_status(_pr_with_checks(_check("c", "SUCCESS"), state="PENDING"))
        assert "results incomplete" in metrics.format_ci_block(odd)
        assert metrics.format_ci_block(ci_status({})) == ""

    def test_ci_failed_and_churn_are_valid_outcomes_with_next_options(self):
        assert "ci_failed" in metrics.VALID_OUTCOMES
        assert "churn" in metrics.VALID_OUTCOMES
        assert "ci_failed" in metrics.FAILED_OUTCOMES
        assert "log" in metrics.format_next_options("ci_failed", False, 0)
        assert "lines this loop changed" in metrics.format_next_options("churn", False, 0)

    def test_run_summary_names_failing_checks(self):
        record = metrics.build_record(
            repo="acme/widget", pr=7, provider=BOT, findings_fetched=1, fixed_count=0,
            observed_fixed_count=0, remaining_actionable=1, needs_human=0,
            addressed_by_reply=0, cycles_used=0, cycle_cap=3, verification="skipped",
            verification_details=None, outcome="ci_failed", outcome_reason="",
            started_at=None, finding_paths=[], judge=None,
            ci={"state": "FAILURE", "total": 2, "pending": 0,
                "failing": [{"name": "tests", "required": True}]},
            fix_induced=2,
        )
        assert record["ci"]["failing"] == [{"name": "tests", "required": True}]
        assert record["fix_induced"] == 2
        summary = metrics.format_run_summary(record)
        assert "CI failing on head: tests" in summary
        assert "Fix-induced findings (on lines this loop changed): 2" in summary


# ---------------------------------------------------------------------------
# Churn: fix-induced findings
# ---------------------------------------------------------------------------

DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -10,2 +10,3 @@ def f():
-old
+new
+new2
@@ -40 +41,0 @@
-gone
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,5 @@
+a
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,3 +0,0 @@
-x
"""


class TestChangedLineRanges:
    def test_new_side_ranges_per_path(self):
        assert parse_changed_line_ranges(DIFF) == {
            "src/x.py": [(10, 12), (41, 41)],
            "new.py": [(1, 5)],
        }

    def test_git_quoted_paths_are_decoded(self):
        # Codex, PR #123: git C-quotes non-ASCII paths by default.
        assert fgt.unquote_git_path("plain.py") == "plain.py"
        assert fgt.unquote_git_path('"b/caf\\303\\251.py"') == "b/café.py"
        assert fgt.unquote_git_path('"a\\"b\\\\c\\t.py"') == 'a"b\\c\t.py'
        diff = (
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1 +1,2 @@\n-x\n+y\n+z\n'
        )
        assert parse_changed_line_ranges(diff) == {"café.py": [(1, 2)]}

    def test_git_diff_between_two_commits(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(tmp_path),
               "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"}

        def git(*a):
            return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,
                                  text=True, env=env).stdout.strip()

        git("init", "-q")
        (repo / "a.py").write_text("1\n2\n3\n4\n")
        git("add", ".")
        git("commit", "-q", "-m", "one")
        first = git("rev-parse", "HEAD")
        (repo / "a.py").write_text("1\nchanged\n3\n4\n5\n")
        git("add", ".")
        git("commit", "-q", "-m", "two")
        second = git("rev-parse", "HEAD")

        assert changed_line_ranges(first, second, cwd=repo) == {"a.py": [(2, 2), (5, 5)]}
        assert changed_line_ranges(first, first, cwd=repo) is None
        assert changed_line_ranges("0" * 40, second, cwd=repo) is None  # unknown object
        assert changed_line_ranges(first, second, cwd=None) is None


class TestFixInducedFindings:
    def _t(self, tid, path, line, body, start=None, side=None):
        t = {"id": tid, "path": path, "line": line,
             "comments": [{"author": {"login": BOT}, "body": body}]}
        if start is not None:
            t["startLine"] = start
        if side is not None:
            t["diffSide"] = side
        return t

    def test_only_new_findings_inside_changed_ranges_count(self):
        inside = self._t("t1", "src/x.py", 11, "inside the hunk")
        outside = self._t("t2", "src/x.py", 30, "outside")
        carried = self._t("t3", "src/x.py", 12, "carried over from before")
        ranged = self._t("t4", "src/x.py", 15, "range crosses the hunk", start=12)
        other = self._t("t5", "y.py", 11, "other file")
        new = {finding_fingerprint(t) for t in (inside, outside, ranged, other)}
        induced = fix_induced_findings(
            [inside, outside, carried, ranged, other], new_fingerprints=new,
            ranges=parse_changed_line_ranges(DIFF),
        )
        assert induced == {finding_fingerprint(inside), finding_fingerprint(ranged)}

    def test_left_side_threads_are_never_fix_induced(self):
        # Codex, PR #123: a LEFT anchor is a base-file line number.
        left = self._t("t1", "src/x.py", 11, "deleted line", side="LEFT")
        right = self._t("t2", "src/x.py", 11, "added line", side="RIGHT")
        new = {finding_fingerprint(left), finding_fingerprint(right)}
        induced = fix_induced_findings([left, right], new_fingerprints=new,
                                       ranges={"src/x.py": [(10, 12)]})
        assert induced == {finding_fingerprint(right)}

    def test_churn_needs_a_second_cycle_and_every_new_finding(self):
        assert churn_detected(fix_induced=2, new_findings=2, session_cycle=2)
        assert not churn_detected(fix_induced=1, new_findings=2, session_cycle=2)
        assert not churn_detected(fix_induced=2, new_findings=2, session_cycle=1)
        assert not churn_detected(fix_induced=0, new_findings=0, session_cycle=3)

    def test_churn_line_wording(self):
        assert metrics.format_churn_line(fix_induced=0, new_findings=3, session_cycle=2) == ""
        info = metrics.format_churn_line(fix_induced=1, new_findings=3, session_cycle=2)
        assert info == "[loop] Fix-induced: 1 of 3 new finding(s) land on lines this loop changed."
        churn = metrics.format_churn_line(fix_induced=3, new_findings=3, session_cycle=2)
        assert churn.startswith("[loop] churn: ")
        assert "--record-run --outcome churn" in churn
        assert not metrics.format_churn_line(fix_induced=3, new_findings=3, session_cycle=1).startswith("[loop] churn")

    def test_findings_block_tags_fix_induced(self):
        block = metrics.format_findings_block([
            {"path": "a.py", "line": 1, "severity": "high", "carried": False, "fix_induced": True},
        ])
        assert "a.py:1 [high] · on lines this loop changed" in block


class TestTrackHeadAndFixInduced:
    def test_first_fetch_records_the_head_and_compares_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        result = track_head_and_fix_induced(
            PR, {"headRefOid": "a" * 40}, [], new_fingerprints={"x"},
        )
        assert result == {"fix_induced": set(), "new_findings": 1, "compared": False}
        run = read_run_tracking(PR)
        assert run["last_head_oid"] == "a" * 40
        assert run["cycle_fix_induced_fps"] == [] and run["cycle_new_findings"] == 1

    def test_second_fetch_diffs_from_the_previous_head(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        track_head_and_fix_induced(PR, {"headRefOid": "a" * 40}, [], new_fingerprints=set())
        monkeypatch.setattr(fgt, "repo_root_for_history", lambda pull_request, cwd=None: Path("/repo"))
        seen = {}

        def fake_ranges(prev, head, *, cwd):
            seen["args"] = (prev, head, cwd)
            return {"src/x.py": [(10, 12)]}

        monkeypatch.setattr(fgt, "changed_line_ranges", fake_ranges)
        hit = {"id": "t1", "path": "src/x.py", "line": 11,
               "comments": [{"author": {"login": BOT}, "body": "on the fix"}]}
        miss = {"id": "t2", "path": "src/x.py", "line": 99,
                "comments": [{"author": {"login": BOT}, "body": "elsewhere"}]}
        result = track_head_and_fix_induced(
            PR, {"headRefOid": "b" * 40}, [hit, miss],
            new_fingerprints={finding_fingerprint(hit), finding_fingerprint(miss)},
        )
        assert seen["args"] == ("a" * 40, "b" * 40, Path("/repo"))
        assert result["compared"] is True
        assert result["fix_induced"] == {finding_fingerprint(hit)}
        run = read_run_tracking(PR)
        assert run["fix_induced_fps"] == [finding_fingerprint(hit)]
        assert run["last_head_oid"] == "b" * 40

    def test_an_unknown_checkout_compares_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        track_head_and_fix_induced(PR, {"headRefOid": "a" * 40}, [], new_fingerprints=set())
        monkeypatch.setattr(fgt, "repo_root_for_history", lambda pull_request, cwd=None: None)
        result = track_head_and_fix_induced(PR, {"headRefOid": "b" * 40}, [], new_fingerprints={"x"})
        assert result["compared"] is False and result["fix_induced"] == set()


# ---------------------------------------------------------------------------
# main(): the fetch surfaces CI and fix-induced state, in JSON and markdown
# ---------------------------------------------------------------------------

def _fetch_payload():
    url = "https://github.com/acme/widget/actions/runs/77/job/1"
    pr = _pr_with_checks(_check("tests", "FAILURE", required=True, url=url))
    pr.update({
        "number": 7, "url": "https://github.com/acme/widget/pull/7",
        "headRefOid": "c" * 40,
        "baseRepository": {"nameWithOwner": "acme/widget"},
        "headRepository": {"nameWithOwner": "acme/widget"},
        "reviewRequests": {"nodes": []}, "reviews": {"nodes": []},
        "timelineItems": {"nodes": []},
        "comments": {"nodes": []},
        "reviewThreads": {"nodes": [{
            "id": "b1", "isResolved": False, "isOutdated": False, "path": "src/x.py", "line": 10,
            "comments": {"nodes": [{"author": {"login": BOT, "__typename": "Bot"},
                                    "body": "![high] Use snake_case here.", "url": "https://x/1",
                                    "path": "src/x.py", "line": 10}]},
        }]},
    })
    return pr


@pytest.fixture
def driven(tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(fgt, "gh_authenticated_login", lambda: AGENT)
    monkeypatch.setattr(fgt, "resolve_pr", lambda arg: PR)
    payload = _fetch_payload()
    monkeypatch.setattr(fgt, "fetch_threads", lambda pr: payload)
    monkeypatch.setattr(fgt, "resolve_thread", lambda *a, **k: None)
    return payload


def _run(monkeypatch, capsys, *extra):
    monkeypatch.setattr(sys, "argv", [
        "fetch_gemini_threads.py", "--pr", PR.url, "--reviewer", BOT, "--reviewer-source",
        "confirmed", "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
        "--judge-mode", "off", *extra,
    ])
    assert fgt.main() == 0
    return capsys.readouterr()


class TestFetchSurfacesCiAndChurn:
    def test_json_carries_ci_and_fix_induced(self, driven, monkeypatch, capsys):
        out = json.loads(_run(monkeypatch, capsys, "--format", "json").out)
        status = out["loopStatus"]
        assert status["ci"]["failing"][0]["name"] == "tests"
        assert status["ci"]["failing"][0]["log_command"] == "gh run view 77 --repo acme/widget --log-failed"
        assert status["fixInduced"] == {"count": 0, "newFindings": 1, "compared": False, "churn": False}
        assert [t["id"] for t in out["threads"]] == ["b1"]

    def test_markdown_prints_the_ci_block(self, driven, monkeypatch, capsys):
        out = _run(monkeypatch, capsys).out
        assert "[loop] CI: 1 failing check(s) on the head, 1 required (1 total)." in out
        assert "[loop] churn" not in out

    def test_cycle_summary_receipt_carries_the_ci_block(self, driven, monkeypatch, capsys):
        _run(monkeypatch, capsys, "--format", "json")
        bodies = []
        monkeypatch.setattr(
            fgt, "post_or_update_sticky_receipt",
            lambda pr, body, *, dry_run=False: bodies.append(body) or 4242,
        )
        result = _run(monkeypatch, capsys, "--cycle-summary", "--verification", "passed")
        assert "[loop] Cycle receipt" in result.out
        assert "[loop] CI: 1 failing check(s)" in result.out  # action-changing: stays in chat
        assert "[loop] CI: 1 failing check(s)" in bodies[-1]

    def test_terminal_receipt_names_the_failing_check(self, driven, monkeypatch, capsys):
        _run(monkeypatch, capsys, "--format", "json")
        bodies = []
        monkeypatch.setattr(
            fgt, "post_or_update_sticky_receipt",
            lambda pr, body, *, dry_run=False: bodies.append(body) or 1,
        )
        result = _run(monkeypatch, capsys, "--record-run", "--outcome", "ci_failed")
        assert "outcome ci_failed" in result.out
        assert "CI failing on head: tests" in bodies[-1]


# ---------------------------------------------------------------------------
# CI during the wait (Codex, PR #123)
# ---------------------------------------------------------------------------

def _red_payload():
    url = "https://github.com/acme/widget/actions/runs/9/job/1"
    pr = _pr_with_checks(_check("tests", "FAILURE", required=True, url=url))
    pr.update({"reviews": {"nodes": []}, "comments": {"nodes": []},
               "reviewThreads": {"nodes": []}, "headRefOid": "c" * 40})
    return pr


class TestCiDuringTheWait:
    def test_chunked_wait_exits_carry_the_heads_checks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: _red_payload())
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)
        chunk = fgt.run_wait_chunk(
            PR, BOT, timeout_seconds=10**9, interval_seconds=1, quiet_seconds=1,
            after_iso="2026-11-06T10:00:00Z", chunk_seconds=0,
        )
        assert chunk["status"] == "waiting"
        assert chunk["ci"]["failing"][0]["name"] == "tests"
        timed = fgt.run_wait_chunk(
            PR, BOT, timeout_seconds=0, interval_seconds=1, quiet_seconds=1,
            after_iso="2026-11-06T10:00:00Z", chunk_seconds=1,
        )
        assert timed["status"] == "timed_out"
        assert timed["ci"]["failing"][0]["log_command"] == "gh run view 9 --repo acme/widget --log-failed"

    def test_blocking_wait_timeout_surfaces_the_ci_block(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "gh_authenticated_login", lambda: AGENT)
        monkeypatch.setattr(fgt, "resolve_pr", lambda arg: PR)
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: _red_payload())
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)
        base = ["fetch_gemini_threads.py", "--pr", PR.url, "--reviewer", BOT,
                "--reviewer-source", "confirmed", "--wait", "--timeout", "0",
                "--after", "2026-11-06T10:00:00Z"]
        monkeypatch.setattr(sys, "argv", base)
        assert fgt.main() == 0
        out = capsys.readouterr()
        assert "timed out" in out.out.lower()
        assert "[loop] CI: 1 failing check(s) on the head, 1 required (1 total)." in out.out
        assert "log: gh run view 9 --repo acme/widget --log-failed" in out.out
        monkeypatch.setattr(sys, "argv", base + ["--format", "json"])
        assert fgt.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["wait"]["status"] == "timed_out"
        assert payload["wait"]["ci"]["failing"][0]["name"] == "tests"

    def test_wait_heartbeat_names_failing_checks(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: _red_payload())
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)
        with pytest.raises(fgt.WaitTimedOut) as info:
            fgt.wait_for_stable_review(PR, author=BOT, timeout_seconds=0,
                                       interval_seconds=1, quiet_seconds=1,
                                       after_iso="2026-11-06T10:00:00Z")
        assert info.value.pull_request["headRefOid"] == "c" * 40
        assert "CI: 1 failing check(s) on the head (tests)" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Codex's clean receipt ends the wait (live, PR #123)
# ---------------------------------------------------------------------------

class TestCodexCleanReceiptEndsTheWait:
    """Codex answered a re-review request three minutes later with a
    top-level comment and no review, and the wait timed out."""

    RECEIPT = (
        "<!-- codex-review-receipt:v1 -->\n\nCodex Review: Didn't find any major "
        "issues. Already looking forward to the next diff.\n"
    )
    CODEX = "chatgpt-codex-connector"

    def test_the_receipt_is_codex_summary_and_approves(self):
        import review_vendors

        assert review_vendors.summary_review_vendor(self.RECEIPT) is review_vendors.CODEX
        assert review_vendors.CODEX.summary_comments is True
        assert fgt.summary_approves(self.RECEIPT)
        assert fgt.summary_review_comments(
            {"comments": {"nodes": [
                {"author": {"login": self.CODEX}, "body": self.RECEIPT, "createdAt": "2026-11-06T15:47:43Z"},
                {"author": {"login": self.CODEX}, "body": "some other bot note", "createdAt": "2026-11-06T15:48:00Z"},
            ]}},
            self.CODEX,
        )[0]["createdAt"] == "2026-11-06T15:47:43Z"

    def test_wait_ends_on_the_receipt_posted_after_the_request(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)
        payload = {
            "headRefOid": "c" * 40, "reviews": {"nodes": []}, "reviewThreads": {"nodes": []},
            "comments": {"nodes": [
                {"author": {"login": "OrenAshkenazy"}, "body": "@codex review", "createdAt": "2026-11-06T15:44:43Z"},
                {"author": {"login": self.CODEX}, "body": self.RECEIPT, "createdAt": "2026-11-06T15:47:43Z"},
            ]},
        }
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: payload)
        settled = fgt.wait_for_stable_review(
            PR, author=self.CODEX, timeout_seconds=10**9, interval_seconds=1,
            quiet_seconds=0, after_iso="2026-11-06T15:44:43Z",
        )
        assert settled is payload
        assert fgt.wait_activity_fingerprint(payload, self.CODEX, "2026-11-06T15:48:00Z") is None
        assert fgt.summary_comment_findings(payload, self.CODEX) == []
