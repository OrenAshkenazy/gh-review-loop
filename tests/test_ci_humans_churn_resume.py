"""CI on the head, human review threads, receipt-embedded run state, churn.

Four things the thread filter never sees, each surfaced by the fetch:

- a failing or pending check on the head (``ci_status`` → ``[loop] CI``);
- an unresolved thread a person opened (``human_review_threads``);
- the run state another machine needs to resume (``embed_run_state`` in the
  sticky receipt, adopted by ``hydrate_run_from_receipt``);
- new findings on lines the loop's own fixes changed (``track_head_and_fix_induced``
  → ``[loop] churn``).

No network: the GraphQL payloads are built inline, git runs on a throwaway
repository, and the sticky receipt write is stubbed where main() is driven.
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
    STICKY_RECEIPT_MARKER,
    build_receipt_comment_body,
    changed_line_ranges,
    churn_detected,
    ci_status,
    embed_run_state,
    finding_fingerprint,
    fix_induced_findings,
    human_review_threads,
    hydrate_run_from_receipt,
    parse_changed_line_ranges,
    parse_embedded_run_state,
    read_run_tracking,
    track_head_and_fix_induced,
    update_run_tracking,
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
# 1. CI on the head
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

    def test_failing_block_names_the_check_its_log_and_the_default_stop(self):
        url = "https://github.com/acme/widget/actions/runs/5/job/6"
        ci = ci_status(_pr_with_checks(_check("tests", "FAILURE", required=True, url=url)))
        block = metrics.format_ci_block(ci)
        assert block.startswith("[loop] CI: 1 failing check(s) on the head, 1 required (1 total).")
        assert "  - tests [required]: failure" in block
        assert "log: gh run view 5 --repo acme/widget --log-failed" in block
        assert "fix_ci: off" in block
        assert "--outcome ci_failed" in block
        assert "fix_ci: on" not in block

    def test_fix_ci_mode_tells_the_agent_to_fix_it_this_cycle(self):
        ci = ci_status(_pr_with_checks(_check("tests", "FAILURE")))
        block = metrics.format_ci_block(ci, fix_ci=True)
        assert "fix_ci: on" in block
        assert "ci_failed" not in block

    def test_pending_only_warns_that_the_reviewer_may_not_have_seen_it(self):
        ci = ci_status(_pr_with_checks(_check("tests", None, status="IN_PROGRESS")))
        block = metrics.format_ci_block(ci)
        assert block == (
            "[loop] CI: 1 check(s) still running on the head — a reviewer that "
            "waits for CI has not seen it yet."
        )

    def test_ci_failed_and_churn_are_valid_outcomes_with_next_options(self):
        assert "ci_failed" in metrics.VALID_OUTCOMES
        assert "churn" in metrics.VALID_OUTCOMES
        assert "ci_failed" in metrics.FAILED_OUTCOMES
        ci_options = metrics.format_next_options("ci_failed", False, 0)
        assert "fix CI" in ci_options
        churn_options = metrics.format_next_options("churn", False, 0)
        assert "lines this loop changed" in churn_options

    def test_run_summary_names_failing_checks(self):
        record = metrics.build_record(
            repo="acme/widget", pr=7, provider=BOT, findings_fetched=1, fixed_count=0,
            observed_fixed_count=0, remaining_actionable=1, needs_human=0,
            addressed_by_reply=0, cycles_used=0, cycle_cap=3, verification="skipped",
            verification_details=None, outcome="ci_failed", outcome_reason="",
            started_at=None, finding_paths=[], judge=None,
            ci={"state": "FAILURE", "total": 2, "pending": 0,
                "failing": [{"name": "tests", "required": True}]},
            fix_induced=2, human_threads=1,
        )
        assert record["ci"]["failing"] == [{"name": "tests", "required": True}]
        assert record["fix_induced"] == 2 and record["human_threads"] == 1
        summary = metrics.format_run_summary(record)
        assert "CI failing on head: tests" in summary
        assert "Fix-induced findings (on lines this loop changed): 2" in summary
        assert "Human review threads (not auto-fixed): 1" in summary


class TestFixCiPreference:
    def test_fix_ci_defaults_off_and_round_trips(self, tmp_path, monkeypatch):
        import judge

        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert judge.load_preferences()["fix_ci"] is False
        judge.prefs_path().write_text(json.dumps({"schema_version": 2, "fix_ci": True}))
        assert judge.load_preferences()["fix_ci"] is True
        # Saving an unrelated judge setting keeps it.
        assert judge.save_preferences("off")["fix_ci"] is True

    def test_only_a_json_true_turns_it_on(self, tmp_path, monkeypatch):
        import judge

        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        judge.prefs_path().write_text(json.dumps({"schema_version": 2, "fix_ci": "yes"}))
        assert judge.load_preferences()["fix_ci"] is False


# ---------------------------------------------------------------------------
# 2. Human review threads
# ---------------------------------------------------------------------------

def _thread(tid, comments, *, path="src/x.py", line=10, resolved=False, outdated=False):
    return {
        "id": tid, "isResolved": resolved, "isOutdated": outdated,
        "path": path, "line": line,
        "comments": {"nodes": comments},
    }


def _c(login, body, *, typename="User", url=None):
    return {"author": {"login": login, "__typename": typename}, "body": body, "url": url}


class TestHumanReviewThreads:
    def test_person_opened_threads_are_listed_bot_and_own_are_not(self):
        pr = {"reviewThreads": {"nodes": [
            _thread("h1", [_c("alice", "Why not reuse the helper here?  It exists.",
                               url="https://github.com/acme/widget/pull/7#discussion_r1")]),
            _thread("b1", [_c(BOT, "![high] Use snake_case.", typename="Bot")]),
            _thread("b2", [_c("github-actions", "summary", typename="Bot")]),
            _thread("b3", [_c("claude[bot]", "🔴 bug")]),
            _thread("own", [_c(AGENT, "note to self")]),
            _thread("done", [_c("bob", "fixed?")], resolved=True),
            _thread("old", [_c("bob", "stale")], outdated=True),
        ]}}
        items = human_review_threads(pr, BOT, AGENT)
        assert [t["id"] for t in items] == ["h1"]
        item = items[0]
        assert item["author"] == "alice"
        assert item["path"] == "src/x.py" and item["line"] == 10
        assert item["excerpt"] == "Why not reuse the helper here? It exists."
        assert item["url"].endswith("#discussion_r1")
        assert item["awaiting_reply"] is True and item["reply_count"] == 0

    def test_agents_last_word_marks_the_thread_replied(self):
        pr = {"reviewThreads": {"nodes": [
            _thread("h1", [_c("alice", "Rename this."), _c(AGENT, "Done in abc123.")]),
            _thread("h2", [_c("alice", "Rename this."), _c(AGENT, "Done."), _c("alice", "Not quite.")]),
        ]}}
        items = human_review_threads(pr, BOT, AGENT)
        assert [(t["id"], t["awaiting_reply"], t["reply_count"]) for t in items] == [
            ("h1", False, 1), ("h2", True, 2),
        ]

    def test_without_an_agent_login_every_human_thread_awaits(self):
        pr = {"reviewThreads": {"nodes": [_thread("h1", [_c("alice", "x")])]}}
        assert human_review_threads(pr, BOT, None)[0]["awaiting_reply"] is True

    def test_long_bodies_are_cut_to_an_excerpt(self):
        pr = {"reviewThreads": {"nodes": [_thread("h1", [_c("alice", "word " * 100)])]}}
        excerpt = human_review_threads(pr, BOT, AGENT)[0]["excerpt"]
        assert len(excerpt) <= fgt.HUMAN_THREAD_EXCERPT_CHARS and excerpt.endswith("…")

    def test_block_is_empty_without_threads_and_names_the_rule_with_them(self):
        assert metrics.format_human_threads_block([]) == ""
        block = metrics.format_human_threads_block([
            {"path": "a.py", "line": 3, "author": "alice", "url": "u", "excerpt": "Why?",
             "awaiting_reply": True},
            {"path": "b.py", "line": None, "author": "bob", "url": None, "excerpt": "",
             "awaiting_reply": False},
        ])
        assert block.startswith("[loop] Human review threads (2, 1 awaiting your reply)")
        assert "never auto-fix" in block
        assert "  1. a.py:3 · @alice · awaiting reply\n     Why?\n     u" in block
        assert "  2. b.py · @bob · replied" in block


# ---------------------------------------------------------------------------
# 3. Run state embedded in the sticky receipt
# ---------------------------------------------------------------------------

class TestEmbeddedRunState:
    def test_round_trip_keeps_only_run_keys(self):
        run = {
            "started_at": "2026-11-06T10:00:00Z", "session_cycle": 2, "update_seq": 4,
            "seen_finding_fps": ["aa", "bb"], "fixed_paths": ["weird -- > path.py"],
            "comment_id": 99, "judge_results": {"t1": {}}, "wait": {"x": 1},
        }
        block = embed_run_state(run)
        assert block.startswith(fgt.STICKY_STATE_MARKER) and block.endswith(" -->")
        assert "-->" not in block[len(fgt.STICKY_STATE_MARKER):-4]
        parsed = parse_embedded_run_state("header\n" + block + "\ntrailer")
        assert parsed == {
            "started_at": "2026-11-06T10:00:00Z", "session_cycle": 2, "update_seq": 4,
            "seen_finding_fps": ["aa", "bb"], "fixed_paths": ["weird -- > path.py"],
        }

    def test_no_run_means_no_block(self):
        assert embed_run_state({}) == ""
        assert embed_run_state({"session_cycle": 1}) == ""

    def test_unreadable_blocks_parse_to_none(self):
        assert parse_embedded_run_state(None) is None
        assert parse_embedded_run_state("no marker") is None
        assert parse_embedded_run_state(fgt.STICKY_STATE_MARKER + "@@@ -->") is None
        assert parse_embedded_run_state(fgt.STICKY_STATE_MARKER + "bm90anNvbg== -->") is None
        # JSON without started_at is not a run.
        import base64
        junk = base64.urlsafe_b64encode(b'{"session_cycle": 3}').decode()
        assert parse_embedded_run_state(fgt.STICKY_STATE_MARKER + junk + " -->") is None

    def test_receipt_body_carries_the_state_after_the_marker(self):
        body = build_receipt_comment_body(
            ["[loop] Cycle receipt"], status="RUNNING",
            state_block=embed_run_state({"started_at": "2026-11-06T10:00:00Z"}),
        )
        assert body.index(STICKY_RECEIPT_MARKER) < body.index(fgt.STICKY_STATE_MARKER)
        assert parse_embedded_run_state(body) == {"started_at": "2026-11-06T10:00:00Z"}

    def _pr_with_receipt(self, status, run):
        body = build_receipt_comment_body(
            ["[loop] Cycle receipt"], status=status, state_block=embed_run_state(run),
        )
        return {"comments": {"nodes": [
            {"author": {"login": "someone"}, "body": "unrelated"},
            {"author": {"login": AGENT}, "body": body},
        ]}}

    def test_running_receipt_hydrates_an_empty_state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        run = {
            "started_at": "2026-11-06T10:00:00Z", "session_cycle": 2, "update_seq": 5,
            "seen_finding_fps": ["aa"], "prior_seen_finding_fps": ["aa"],
            "fixed_finding_fps": ["aa"], "swept_pattern_sigs": ["s1"],
            "last_head_oid": "f" * 40, "fix_induced_fps": [],
        }
        adopted = hydrate_run_from_receipt(PR, self._pr_with_receipt("RUNNING", run))
        assert adopted is not None
        local = read_run_tracking(PR)
        assert local["session_cycle"] == 2
        assert local["seen_finding_fps"] == ["aa"]
        assert local["last_head_oid"] == "f" * 40
        # The receipt was the last summary; the next fetch makes it stale.
        assert local["last_summary_seq"] == 5
        # The next real fetch continues the run instead of starting cycle 1.
        update_run_tracking(PR, [("t9", "z.py")])
        after = read_run_tracking(PR)
        assert after["session_cycle"] == 2
        assert after["update_seq"] == 6
        assert after["started_at"] == "2026-11-06T10:00:00Z"

    def test_a_finished_receipt_is_not_adopted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        run = {"started_at": "2026-11-06T10:00:00Z", "session_cycle": 3}
        for status in ("DONE", "STOPPED"):
            assert hydrate_run_from_receipt(PR, self._pr_with_receipt(status, run)) is None
        assert "started_at" not in read_run_tracking(PR)

    def test_local_state_wins_over_the_receipt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(PR, [("t1", "a.py")])
        run = {"started_at": "2020-01-01T00:00:00Z", "session_cycle": 9}
        assert hydrate_run_from_receipt(PR, self._pr_with_receipt("RUNNING", run)) is None
        assert read_run_tracking(PR)["session_cycle"] == 1

    def test_a_receipt_without_a_state_block_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = {"comments": {"nodes": [{"body": build_receipt_comment_body([], status="RUNNING")}]}}
        assert hydrate_run_from_receipt(PR, pr) is None


# ---------------------------------------------------------------------------
# 4. Churn: fix-induced findings
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

    def test_git_diff_between_two_commits(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        git = lambda *a: subprocess.run(  # noqa: E731
            ["git", *a], cwd=repo, check=True, capture_output=True, text=True,
            env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"},
        ).stdout.strip()
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
    def _t(self, tid, path, line, body, start=None):
        t = {"id": tid, "path": path, "line": line,
             "comments": [{"author": {"login": BOT}, "body": body}]}
        if start is not None:
            t["startLine"] = start
        return t

    def test_only_new_findings_inside_changed_ranges_count(self):
        inside = self._t("t1", "src/x.py", 11, "inside the hunk")
        outside = self._t("t2", "src/x.py", 30, "outside")
        carried = self._t("t3", "src/x.py", 12, "carried over from before")
        ranged = self._t("t4", "src/x.py", 15, "range crosses the hunk", start=12)
        other = self._t("t5", "y.py", 11, "other file")
        new = {finding_fingerprint(t) for t in (inside, outside, ranged, other)}
        ranges = parse_changed_line_ranges(DIFF)
        induced = fix_induced_findings(
            [inside, outside, carried, ranged, other], new_fingerprints=new, ranges=ranges,
        )
        assert induced == {finding_fingerprint(inside), finding_fingerprint(ranged)}

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
        # Cycle 1 has no previous head to compare to; all findings are new.
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
# main(): the fetch surfaces all four, in JSON and in the markdown blocks
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
        "reviewThreads": {"nodes": [
            _thread("b1", [{"author": {"login": BOT, "__typename": "Bot"},
                            "body": "![high] Use snake_case here.", "url": "https://x/1",
                            "path": "src/x.py", "line": 10}]),
            _thread("h1", [_c("alice", "Could this reuse the helper?", url="https://x/2")]),
        ]},
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


class TestFetchSurfacesAll:
    def test_json_carries_ci_human_threads_and_fix_induced(self, driven, monkeypatch, capsys):
        out = json.loads(_run(monkeypatch, capsys, "--format", "json").out)
        status = out["loopStatus"]
        assert status["ci"]["failing"][0]["name"] == "tests"
        assert status["ci"]["failing"][0]["log_command"] == "gh run view 77 --repo acme/widget --log-failed"
        assert status["fixCi"] is False
        assert status["humanThreadCount"] == 1
        assert [t["id"] for t in out["humanThreads"]] == ["h1"]
        assert status["fixInduced"] == {"count": 0, "newFindings": 1, "compared": False, "churn": False}
        assert status["resumedFromReceipt"] is False
        # The reviewer thread is still the only actionable one.
        assert [t["id"] for t in out["threads"]] == ["b1"]

    def test_fix_ci_flag_and_preference_switch_the_mode(self, driven, monkeypatch, capsys):
        assert json.loads(_run(monkeypatch, capsys, "--format", "json", "--fix-ci").out)["loopStatus"]["fixCi"] is True
        import judge
        judge.prefs_path().write_text(json.dumps({"schema_version": 2, "fix_ci": True}))
        assert json.loads(_run(monkeypatch, capsys, "--format", "json").out)["loopStatus"]["fixCi"] is True

    def test_markdown_prints_the_ci_and_human_blocks(self, driven, monkeypatch, capsys):
        out = _run(monkeypatch, capsys).out
        assert "[loop] CI: 1 failing check(s) on the head, 1 required (1 total)." in out
        assert "[loop] Human review threads (1, 1 awaiting your reply)" in out
        assert "src/x.py:10 · @alice · awaiting reply" in out
        assert "[loop] churn" not in out

    def test_a_running_receipt_on_the_pr_resumes_the_run(self, driven, monkeypatch, capsys):
        run = {"started_at": "2026-11-06T10:00:00Z", "session_cycle": 2, "update_seq": 3,
               "last_head_oid": "c" * 40}
        driven["comments"]["nodes"].append({
            "author": {"login": AGENT}, "createdAt": "2026-11-06T10:05:00Z",
            "body": build_receipt_comment_body(["x"], status="RUNNING", state_block=embed_run_state(run)),
        })
        result = _run(monkeypatch, capsys, "--format", "json")
        out = json.loads(result.out)
        assert out["loopStatus"]["resumedFromReceipt"] is True
        assert out["loopStatus"]["sessionCycle"] == 2
        assert "resumed run state from the PR receipt (session cycle 2)" in result.err

    def test_cycle_summary_receipt_embeds_state_and_the_ci_block(self, driven, monkeypatch, capsys):
        _run(monkeypatch, capsys, "--format", "json")
        bodies = []

        def fake_post(pr, body, *, dry_run=False):
            bodies.append(body)
            return 4242

        monkeypatch.setattr(fgt, "post_or_update_sticky_receipt", fake_post)
        result = _run(monkeypatch, capsys, "--cycle-summary", "--verification", "passed")
        assert "[loop] Cycle receipt" in result.out
        assert "[loop] CI: 1 failing check(s)" in result.out  # action-changing: stays in chat
        body = bodies[-1]
        state = parse_embedded_run_state(body)
        assert state is not None and state["session_cycle"] == 1
        assert state["last_head_oid"] == "c" * 40
        assert "[loop] CI: 1 failing check(s)" in body
        assert "[loop] Human review threads (1" in body

    def test_terminal_receipt_carries_no_state(self, driven, monkeypatch, capsys):
        _run(monkeypatch, capsys, "--format", "json")
        bodies = []
        monkeypatch.setattr(
            fgt, "post_or_update_sticky_receipt",
            lambda pr, body, *, dry_run=False: bodies.append(body) or 1,
        )
        result = _run(monkeypatch, capsys, "--record-run", "--outcome", "ci_failed")
        assert "outcome ci_failed" in result.out
        assert parse_embedded_run_state(bodies[-1]) is None
        assert "CI failing on head: tests" in bodies[-1]
