from cluster_findings import Cluster
import metrics
from metrics import (
    RECORD_SCHEMA_VERSION,
    build_record,
    format_convergence_line,
    format_degenerate_clustering_advisory,
    format_patterns_block,
    pattern_history_for_pr,
)


class TestHelpers:
    def test_runs_log_path_honors_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert metrics.runs_log_path() == tmp_path / "runs.jsonl"

    def test_top_dir(self):
        assert metrics.top_dir("tests/test_auth.py") == "tests"
        assert metrics.top_dir("src/auth/login.py") == "src"
        assert metrics.top_dir("") == "(unknown)"

    def test_runs_log_path_default(self, monkeypatch, tmp_path):
        # Fresh HOME so the resolver can't fall back to a real legacy dir on
        # the machine running the tests — a fresh install writes the new name.
        monkeypatch.delenv("GGRL_STATE_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        result = metrics.runs_log_path()
        assert result.name == "runs.jsonl"
        assert "gh-review-loop" in str(result)
        assert "gh-gemini-review-loop" not in str(result)

    def test_runs_log_path_unmigrated_legacy(self, monkeypatch, tmp_path):
        # Pre-rename state dir, not yet migrated: existing data stays readable.
        monkeypatch.delenv("GGRL_STATE_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".config" / "gh-gemini-review-loop").mkdir(parents=True)
        assert "gh-gemini-review-loop" in str(metrics.runs_log_path())

    def test_format_duration(self):
        assert metrics.format_duration(48) == "48s"
        assert metrics.format_duration(720) == "12m"
        assert metrics.format_duration(3840) == "1h 4m"
        assert metrics.format_duration(0) == "0s"


class TestPersistence:
    def test_append_then_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        metrics.append_record({"schema_version": 1, "repo": "a/b", "pr": 1})
        metrics.append_record({"schema_version": 1, "repo": "a/b", "pr": 2})
        records, skipped = metrics.load_records()
        assert skipped == 0
        assert [r["pr"] for r in records] == [1, 2]

    def test_load_missing_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert metrics.load_records() == ([], 0)

    def test_load_skips_corrupt_and_future_version(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        path = tmp_path / "runs.jsonl"
        path.write_text(
            '{"schema_version": 1, "pr": 1}\n'
            "not json at all\n"
            '{"schema_version": 999, "pr": 2}\n'
            "\n"
            '{"schema_version": 1, "pr": 3}\n'
        )
        records, skipped = metrics.load_records()
        assert [r["pr"] for r in records] == [1, 3]
        assert skipped == 2  # corrupt line + future version; blank line ignored

    def test_load_returns_empty_on_oserror(self, tmp_path, monkeypatch):
        path = tmp_path / "runs.jsonl"
        path.write_text('{"schema_version": 1, "pr": 1}\n')
        def _raise(*a, **kw):
            raise OSError("Permission denied")
        monkeypatch.setattr(metrics.Path, "read_text", _raise)
        records, skipped = metrics.load_records(path)
        assert records == []
        assert skipped == 0


class TestBuildJudgeBlock:
    def test_disabled_when_not_run(self):
        assert metrics.build_judge_block(False, {}) == {"enabled": False}

    def test_counts_verdicts_and_actions(self):
        results = {
            "t1": {"verdict": "valid_actionable", "recommended_action": "fix"},
            "t2": {"verdict": "false_positive", "recommended_action": "ignore"},
            "t3": {"verdict": "false_positive", "recommended_action": "ignore"},
            "t4": {"verdict": "needs_human", "recommended_action": "escalate"},
        }
        block = metrics.build_judge_block(True, results)
        assert block["enabled"] is True
        assert block["verdicts"]["false_positive"] == 2
        assert block["verdicts"]["needs_human"] == 1
        assert block["verdicts"]["duplicate"] == 0
        assert block["recommended_actions"]["ignore"] == 2
        assert block["recommended_actions"]["escalate"] == 1


class TestBuildRecord:
    def _kwargs(self, **over):
        base = dict(
            repo="o/r", pr=23, provider="gemini-code-assist",
            findings_fetched=7, fixed_count=4, observed_fixed_count=4,
            remaining_actionable=1, needs_human=1, addressed_by_reply=2,
            cycles_used=2, cycle_cap=3, verification="passed",
            verification_details={}, outcome="clean",
            outcome_reason="0 actionable threads remaining",
            started_at="2026-06-04T18:10:11Z", ts="2026-06-04T18:22:11Z",
            finding_paths=["tests/test_auth.py", "src/auth/login.py"],
            judge={"enabled": False},
        )
        base.update(over)
        return base

    def test_full_record_shape_and_derived_fields(self):
        rec = metrics.build_record(**self._kwargs())
        assert rec["schema_version"] == 1
        assert rec["duration_seconds"] == 720
        assert rec["finding_areas"] == ["tests", "src"]
        assert rec["finding_paths"] == ["tests/test_auth.py", "src/auth/login.py"]
        assert rec["verification_details"] == {}
        assert rec["judge"] == {"enabled": False}

    def test_missing_started_at_falls_back_to_ts(self):
        rec = metrics.build_record(**self._kwargs(started_at=None))
        assert rec["started_at"] == rec["ts"]
        assert rec["duration_seconds"] == 0

    def test_all_outcomes_accepted(self):
        for outcome in metrics.VALID_OUTCOMES:
            rec = metrics.build_record(**self._kwargs(outcome=outcome))
            assert rec["outcome"] == outcome

    def test_non_list_cycles_coerced_to_empty(self):
        # A non-list `cycles` (corrupt accumulator) must not crash list().
        rec = metrics.build_record(**self._kwargs(cycles=5))
        assert rec["cycles"] == []


class TestFormatRunSummary:
    def _rec(self, **over):
        base = metrics.build_record(
            repo="o/r", pr=23, provider="gemini-code-assist",
            findings_fetched=7, fixed_count=4, observed_fixed_count=4,
            remaining_actionable=1, needs_human=1, addressed_by_reply=0,
            cycles_used=2, cycle_cap=3, verification="passed",
            verification_details={}, outcome="clean", outcome_reason="ok",
            started_at="2026-06-04T18:10:11Z", ts="2026-06-04T18:22:11Z",
            finding_paths=["tests/x.py"], judge={"enabled": False},
        )
        base.update(over)
        return base

    def test_judge_off_default_receipt(self):
        # remaining_actionable=1, needs_human=1 → valid_actionable_remaining=0,
        # so only "Human decision required" shows (no "Remaining valid actionable").
        # observed_fixed_count == fixed_count, so no "Observed fixed" line.
        out = metrics.format_run_summary(self._rec())
        assert out.splitlines() == [
            "[loop] Summary",
            "Findings fetched: 7",
            "Fixed: 4",
            "Human decision required: 1",
            "Re-review requests used: 2/3 (cap)",
            "Verification: passed",
            "Outcome: clean",
            "Time to clean PR: 12m",
        ]

    def test_cycle_receipt_header_when_not_terminal(self):
        # --cycle-summary path (terminal=False) uses a distinct header so users
        # can tell mid-loop receipts from the final [loop] Summary.
        out = metrics.format_run_summary(self._rec(), terminal=False)
        lines = out.splitlines()
        assert lines[0] == "[loop] Cycle receipt"
        assert lines[1] == "Findings fetched: 7"

    def test_terminal_header_explicit(self):
        out = metrics.format_run_summary(self._rec(), terminal=True)
        assert out.splitlines()[0] == "[loop] Summary"

    def test_observed_fixed_shown_when_differs_from_fixed(self):
        out = metrics.format_run_summary(
            self._rec(fixed_count=2, observed_fixed_count=1)
        ).splitlines()
        assert out[2] == "Fixed: 2"
        assert out[3] == "Observed fixed: 1"

    def test_observed_fixed_omitted_when_equal_to_fixed(self):
        assert "Observed fixed" not in metrics.format_run_summary(self._rec())

    def test_outcome_line_present(self):
        assert "Outcome: clean" in metrics.format_run_summary(self._rec())

    def test_time_label_is_spent_when_outcome_not_clean(self):
        out = metrics.format_run_summary(self._rec(outcome="human"))
        assert "Time spent: 12m" in out
        assert "Time to clean PR" not in out

    def test_addressed_by_reply_line_omitted_when_zero(self):
        assert "Addressed by reply" not in metrics.format_run_summary(self._rec())

    def test_addressed_by_reply_line_shown_when_nonzero(self):
        out = metrics.format_run_summary(self._rec(addressed_by_reply=2))
        assert "Addressed by reply: 2" in out

    def test_failed_check_line_shown_when_verification_failed(self):
        out = metrics.format_run_summary(
            self._rec(
                verification="failed",
                verification_details={"failed_check": "lint"},
                outcome="verification_failed",
            )
        ).splitlines()
        i = out.index("Verification: failed")
        assert out[i + 1] == "Failed check: lint"
        assert out[i + 2] == "Outcome: verification_failed"

    def test_failed_check_line_omitted_when_verification_passed(self):
        assert "Failed check" not in metrics.format_run_summary(self._rec())

    def test_judge_on_inserts_renamed_lines_after_fixed(self):
        judge = {
            "enabled": True,
            "verdicts": {
                "valid_actionable": 3, "false_positive": 1, "duplicate": 1,
                "already_addressed": 1, "explanation_only": 0, "needs_human": 1,
            },
            "recommended_actions": {"fix": 3, "reply": 1, "ignore": 2, "escalate": 1},
        }
        # remaining_actionable=1, needs_human=1 → valid_actionable_remaining=0 →
        # only "Human decision required" line; "Needs human by judge" is removed.
        out = metrics.format_run_summary(self._rec(judge=judge)).splitlines()
        assert out[2] == "Fixed: 4"
        assert out[3] == "Ignored by judge: 3"   # false_positive+duplicate+already_addressed+explanation_only
        assert out[4] == "Human decision required: 1"
        assert "Needs human by judge" not in out

    def test_judge_lines_omitted_when_zero(self):
        judge = {
            "enabled": True,
            "verdicts": {v: 0 for v in metrics.JUDGE_VERDICTS},
            "recommended_actions": {a: 0 for a in metrics.JUDGE_ACTIONS},
        }
        out = metrics.format_run_summary(self._rec(judge=judge))
        assert "Ignored by judge" not in out
        assert "Needs human by judge" not in out  # label removed entirely

    def test_terminal_summary_uses_terminal_breakdown_labels(self):
        out = metrics.format_run_summary(
            self._rec(
                findings_fetched=21,
                fixed_count=21,
                observed_fixed_count=20,
                remaining_actionable=1,
                needs_human=0,
                terminal_breakdown={
                    "confirmed_fixed_outdated": 20,
                    "fixed_pending_confirmation": 1,
                    "remaining_valid_actionable": 0,
                    "needs_human": 0,
                },
                outcome="fixed_pending_confirmation",
            )
        )
        assert "Confirmed fixed/outdated: 20" in out
        assert "Fixed but awaiting review confirmation: 1" in out
        assert "Remaining valid actionable: 0" in out
        assert "Remaining valid actionable: 1" not in out

    def test_cycle_summary_uses_pre_push_wording_for_local_fixes(self):
        out = metrics.format_run_summary(
            self._rec(
                fixed_count=7,
                observed_fixed_count=0,
                remaining_actionable=7,
                needs_human=0,
                valid_actionable_remaining=7,
            ),
            terminal=False,
        )
        assert "Fixed locally: 7" in out
        assert "Awaiting push/re-review confirmation: 7" in out
        assert "Remaining valid actionable: 7" not in out


class TestTerminalBreakdown:
    def test_renders_all_terminal_buckets(self):
        out = metrics.format_terminal_breakdown(
            confirmed_fixed_outdated=20,
            fixed_pending_confirmation=1,
            remaining_valid_actionable=0,
            needs_human=0,
        ).splitlines()
        assert out == [
            "Confirmed fixed/outdated: 20",
            "Fixed but awaiting review confirmation: 1",
            "Remaining valid actionable: 0",
            "Human decision required: 0",
        ]


class TestProfileIntroBlock:
    def _profile(self, checks):
        return {"source": "confirmed", "working_directory": ".", "checks": checks}

    def test_present_profile(self):
        out = metrics.format_profile_intro_block(
            self._profile([
                {"name": "root", "command": "uv run pytest", "required": True}
            ]),
            "OrenAshkenazy/AegisLocal",
        ).splitlines()
        assert out == [
            "[loop] Repo-aware verification profile",
            "Profile: OrenAshkenazy/AegisLocal",
            "Checks:",
            "1. root — uv run pytest (cwd: ., required)",
        ]

    def test_no_profile(self):
        assert metrics.format_profile_intro_block(None, "o/r") == (
            "[loop] Verification profile: none saved, using ad hoc verification."
        )

    def test_skipped_profile(self):
        assert metrics.format_profile_intro_block({"source": "skipped"}, "o/r") == (
            "[loop] Verification profile: skipped for this repo, using ad hoc verification."
        )

    def test_malformed_or_empty_checks(self):
        assert metrics.format_profile_intro_block({"source": "confirmed", "checks": []}, "o/r") == (
            "[loop] Verification profile: missing or malformed, using ad hoc verification."
        )
        assert metrics.format_profile_intro_block("garbage", "o/r") == (
            "[loop] Verification profile: missing or malformed, using ad hoc verification."
        )


class TestPlannedVerificationBlock:
    def test_one_check(self):
        out = metrics.format_planned_verification_block({
            "source": "confirmed",
            "working_directory": ".",
            "checks": [{"name": "root", "command": "uv run pytest", "required": True}],
        }).splitlines()
        assert out == [
            "[loop] Verification suite",
            "Running 1 required repo-aware check:",
            "1. root — uv run pytest (cwd: .)",
        ]

    def test_multiple_checks(self):
        out = metrics.format_planned_verification_block({
            "source": "confirmed",
            "working_directory": ".",
            "checks": [
                {"name": "root", "command": "uv run pytest", "required": True},
                {"name": "lint", "command": "ruff check .", "required": True},
                {"name": "typecheck", "command": "mypy .", "required": True},
            ],
        }).splitlines()
        assert out == [
            "[loop] Verification suite",
            "Running 3 required repo-aware checks:",
            "1. root — uv run pytest (cwd: .)",
            "2. lint — ruff check . (cwd: .)",
            "3. typecheck — mypy . (cwd: .)",
        ]

    def test_no_checks(self):
        assert metrics.format_planned_verification_block({"source": "confirmed", "checks": []}) == (
            "[loop] Verification suite\n"
            "No required repo-aware checks saved; use ad hoc verification."
        )


class TestJudgeSkip:
    def test_formats_reason(self):
        assert metrics.format_judge_skip("no API key") == "[loop] judge eval skipped: no API key"


class TestNextOptions:
    def test_capped(self):
        out = metrics.format_next_options("capped", cap_reached=True, needs_human=0)
        assert 'Bump cap and continue: "run the review loop with cap 6"' in out

    def test_human(self):
        out = metrics.format_next_options("human", cap_reached=False, needs_human=1)
        assert "Ask Claude to implement option A" in out
        assert "reply to the thread with rationale" in out

    def test_fixed_pending_confirmation(self):
        out = metrics.format_next_options(
            "fixed_pending_confirmation", cap_reached=True, needs_human=0
        )
        assert "Bump cap and run one confirmation cycle" in out

    def test_clean_empty(self):
        assert metrics.format_next_options("clean", cap_reached=False, needs_human=0) == ""


class TestSemanticRiskBlock:
    def test_empty_returns_empty(self):
        assert metrics.format_semantic_risk_block([]) == ""
        assert metrics.format_semantic_risk_block(None) == ""

    def test_non_empty_renders_manual_heuristic_block(self):
        out = metrics.format_semantic_risk_block([
            "hash_password(password) -> hash_password(password, salt)",
            "get_user() now returns one row instead of a list",
        ])
        assert "[loop] Semantic risk note (manual / heuristic)" in out
        assert "- hash_password(password) -> hash_password(password, salt)" in out
        assert "- get_user() now returns one row instead of a list" in out
        assert "Verification passed, but this may require human review." in out


class TestFormatSuiteBlock:
    def test_empty_when_no_details(self):
        assert metrics.format_suite_block(None) == ""
        assert metrics.format_suite_block({}) == ""
        assert metrics.format_suite_block({"checks": []}) == ""

    def test_renders_one_line_per_check(self):
        details = {"checks": [
            {"name": "root", "command": "uv run pytest", "required": True, "status": "passed"},
            {"name": "web", "command": "npm test", "required": False, "status": "skipped"},
        ]}
        out = metrics.format_suite_block(details).splitlines()
        assert out[0] == "Verification suite:"
        assert "uv run pytest" in out[1] and "(root, required)" in out[1] and "passed" in out[1]
        assert "npm test" in out[2] and "(web, optional)" in out[2] and "skipped" in out[2]

    def test_ignores_non_dict_checks(self):
        details = {"checks": ["junk", {"name": "root", "command": "pytest", "required": True, "status": "passed"}]}
        out = metrics.format_suite_block(details)
        assert "pytest" in out and "junk" not in out


class TestFormatFindingsBlock:
    def test_empty_when_no_findings(self):
        assert metrics.format_findings_block([]) == ""

    def test_counts_new_vs_carried(self):
        findings = [
            {"path": "a.py", "line": 10, "severity": "high", "carried": False},
            {"path": "b.py", "line": 20, "severity": "medium", "carried": True},
            {"path": "c.py", "line": 30, "severity": "high", "carried": False},
        ]
        out = metrics.format_findings_block(findings)
        head = out.splitlines()[0]
        assert head == "Findings (3): 2 new, 1 carried over"
        assert "carried over from a prior cycle" in out  # only the carried one is tagged
        assert out.count("carried over from a prior cycle") == 1

    def test_includes_url_and_location(self):
        findings = [{"path": "a.py", "line": 10, "severity": "high",
                     "url": "https://github.com/o/r/pull/1#discussion_r99", "carried": False}]
        out = metrics.format_findings_block(findings)
        assert "a.py:10 [high]" in out
        assert "https://github.com/o/r/pull/1#discussion_r99" in out


class TestFormatAutoSnapshot:
    """The Stop-hook backstop can't know agent-only facts (fixed count,
    verification, terminal outcome), so its summary must show ONLY
    GitHub-observable state and be clearly labelled as automatic — never
    present a guessed Fixed/Verification/Outcome as if authoritative."""

    def _rec(self, **over):
        base = metrics.build_record(
            repo="o/r", pr=23, provider="gemini-code-assist",
            findings_fetched=7, fixed_count=0, observed_fixed_count=4,
            remaining_actionable=1, needs_human=1, addressed_by_reply=0,
            cycles_used=2, cycle_cap=3, verification="skipped",
            verification_details={}, outcome="human", outcome_reason="x",
            started_at="2026-06-04T18:10:11Z", ts="2026-06-04T18:22:11Z",
            finding_paths=["tests/x.py"], judge={"enabled": False},
        )
        base.update(over)
        return base

    def test_auto_snapshot_is_single_line(self):
        # One line so Claude Code doesn't collapse it behind "ctrl+o to expand"
        # — a backstop summary you must expand isn't actually surfaced.
        assert "\n" not in metrics.format_auto_snapshot(self._rec())

    def test_auto_snapshot_content(self):
        assert metrics.format_auto_snapshot(self._rec()) == (
            "[loop] Summary (auto, agent didn't post one): "
            "7 seen, 4 resolved, 1 open · re-review requests 2/3"
        )

    def test_auto_snapshot_appends_degenerate_clustering_warning(self):
        out = metrics.format_auto_snapshot(self._rec(patterns={
            "distinct_patterns": 3,
            "max_cluster_size": 1,
        }))

        assert "likely prose-hash fallbacks" in out
        assert "manual sweep required before fixing" in out
        assert "\n" not in out

    def test_auto_snapshot_hides_agent_only_fields(self):
        out = metrics.format_auto_snapshot(self._rec())
        for jargon in ("Fixed:", "Observed fixed", "Verification:", "Outcome:"):
            assert jargon not in out


class TestAggregate:
    def _rec(self, **over):
        base = dict(
            schema_version=1, repo="o/r", pr=1, provider="gemini-code-assist",
            findings_fetched=5, observed_fixed_count=4, needs_human=1,
            addressed_by_reply=1, cycles_used=2, duration_seconds=600,
            finding_areas=["tests"], judge={"enabled": False},
        )
        base.update(over)
        return base

    def test_empty_returns_count_zero(self):
        assert metrics.aggregate([]) == {"count": 0}

    def test_basic_aggregation(self):
        recs = [
            self._rec(cycles_used=2, duration_seconds=600, observed_fixed_count=4, findings_fetched=5),
            self._rec(cycles_used=1, duration_seconds=1200, observed_fixed_count=3, findings_fetched=4),
        ]
        agg = metrics.aggregate(recs)
        assert agg["count"] == 2
        assert agg["avg_cycles"] == 1.5
        assert agg["avg_duration"] == 900.0
        assert agg["total_fixed"] == 7
        assert agg["total_fetched"] == 9
        assert agg["top_provider"] == "gemini-code-assist"
        assert agg["top_area"] == "tests"

    def test_duration_zero_included_in_average(self):
        # A valid 0-second run must NOT be silently dropped (explicit None check,
        # not truthiness).
        recs = [self._rec(duration_seconds=0), self._rec(duration_seconds=600)]
        assert metrics.aggregate(recs)["avg_duration"] == 300.0

    def test_duration_none_excluded_but_zero_kept(self):
        recs = [
            self._rec(duration_seconds=None),
            self._rec(duration_seconds=0),
            self._rec(duration_seconds=900),
        ]
        # None dropped; 0 and 900 averaged.
        assert metrics.aggregate(recs)["avg_duration"] == 450.0

    def test_false_positives_only_over_judged_runs(self):
        judged = self._rec(judge={"enabled": True, "verdicts": {"false_positive": 3}})
        unjudged = self._rec(judge={"enabled": False})
        agg = metrics.aggregate([judged, unjudged])
        assert agg["judged_count"] == 1
        assert agg["false_positives_avoided"] == 3

    def test_elapsed_split_by_outcome(self):
        recs = [
            self._rec(outcome="clean", duration_seconds=600),
            self._rec(outcome="clean", duration_seconds=200),
            self._rec(outcome="capped", duration_seconds=1000),
            self._rec(outcome="verification_failed", duration_seconds=300),
            self._rec(outcome="regression", duration_seconds=500),
            self._rec(outcome="human", duration_seconds=999),
        ]
        agg = metrics.aggregate(recs)
        assert agg["avg_duration"] == (600 + 200 + 1000 + 300 + 500 + 999) / 6
        assert agg["avg_duration_clean"] == 400.0           # (600+200)/2
        assert agg["avg_duration_capped"] == 1000.0
        assert agg["avg_duration_failed"] == 400.0          # (300+500)/2, regression+verification_failed
        # human is in the overall average but not in any named split.

    def test_elapsed_split_none_when_no_matching_outcome(self):
        agg = metrics.aggregate([self._rec(outcome="clean", duration_seconds=600)])
        assert agg["avg_duration_clean"] == 600.0
        assert agg["avg_duration_capped"] is None
        assert agg["avg_duration_failed"] is None

    def test_active_cycle_metrics(self):
        recs = [
            self._rec(cycles=[
                {"duration_seconds": 100, "finding_count": 3, "outcome": "continued"},
                {"duration_seconds": 200, "finding_count": 1, "outcome": "clean"},
            ]),
            self._rec(cycles=[
                {"duration_seconds": 400, "finding_count": 2, "outcome": "capped"},
            ]),
        ]
        agg = metrics.aggregate(recs)
        assert agg["avg_active_cycle_time"] == (100 + 200 + 400) / 3   # over all cycles
        assert agg["avg_active_time_per_run"] == (300 + 400) / 2       # run totals averaged
        assert agg["avg_cycles_per_run"] == 1.5                        # (2 + 1) / 2

    def test_active_metrics_none_without_cycle_data(self):
        agg = metrics.aggregate([self._rec(), self._rec()])  # no "cycles" key
        assert agg["avg_active_cycle_time"] is None
        assert agg["avg_active_time_per_run"] is None
        assert agg["avg_cycles_per_run"] is None

    def test_aggregate_tolerates_corrupt_scalar_fields(self):
        # Hand-edited record: cycles_used non-numeric, judge not a dict,
        # finding_areas/provider wrong types. Must not crash.
        recs = [
            self._rec(cycles_used="x", judge=True, finding_areas=5, provider=123,
                      duration_seconds=600),
            self._rec(cycles_used=2, duration_seconds=300),  # default judge/area/provider
        ]
        agg = metrics.aggregate(recs)            # must not raise
        assert agg["avg_cycles"] == 1.0          # "x" -> 0, plus 2, over 2 records
        assert agg["avg_duration"] == 450.0
        assert agg["judged_count"] == 0          # judge=True is not a dict
        assert agg["top_provider"] == "gemini-code-assist"  # 123 excluded
        assert agg["top_area"] == "tests"        # 5 excluded

    def test_elapsed_ignores_non_numeric_duration(self):
        # A corrupt/hand-edited record with a non-numeric (or bool) duration
        # must be excluded from the average rather than crash summation.
        recs = [
            self._rec(duration_seconds="oops"),
            self._rec(duration_seconds=True),   # bool is not a real duration
            self._rec(duration_seconds=600),
        ]
        assert metrics.aggregate(recs)["avg_duration"] == 600.0

    def test_active_metrics_skip_non_dict_and_non_numeric_cycles(self):
        recs = [self._rec(cycles=[
            True,                                       # non-dict element
            {"duration_seconds": 100, "outcome": "continued"},
            {"duration_seconds": "bad", "outcome": "clean"},   # non-numeric
        ])]
        agg = metrics.aggregate(recs)
        assert agg["avg_active_cycle_time"] == 100.0   # only the numeric dict
        assert agg["avg_active_time_per_run"] == 100.0
        assert agg["avg_cycles_per_run"] == 2.0        # two dict cycles, non-dict skipped

    def test_malformed_or_empty_cycles_excluded_without_crash(self):
        # A non-list truthy "cycles" (corrupt/hand-edited record) must not be
        # iterated/len()'d (TypeError); an empty list is "no recorded cycles".
        recs = [
            self._rec(cycles=True),      # non-list truthy
            self._rec(cycles="oops"),    # non-list truthy
            self._rec(cycles=[]),        # empty -> excluded
        ]
        agg = metrics.aggregate(recs)    # must not raise
        assert agg["avg_active_cycle_time"] is None
        assert agg["avg_active_time_per_run"] is None
        assert agg["avg_cycles_per_run"] is None

    def test_active_cycle_time_skips_none_cycle_duration(self):
        recs = [self._rec(cycles=[
            {"duration_seconds": None, "finding_count": 1, "outcome": "continued"},
            {"duration_seconds": 300, "finding_count": 1, "outcome": "clean"},
        ])]
        agg = metrics.aggregate(recs)
        assert agg["avg_active_cycle_time"] == 300.0   # None cycle skipped
        assert agg["avg_cycles_per_run"] == 2.0        # both cycles still counted


class TestFormatWaitHeartbeat:
    def test_waiting_status(self):
        out = metrics.format_wait_heartbeat(
            "waiting",
            author="gemini-code-assist",
            elapsed_seconds=90,
            checks=2,
            next_wait_seconds=90,
        )
        assert out == (
            "[loop] waiting for gemini-code-assist — 90s elapsed, "
            "2 checks done, next check in 90s"
        )

    def test_waiting_singular_check(self):
        out = metrics.format_wait_heartbeat(
            "waiting",
            author="gemini-code-assist",
            elapsed_seconds=60,
            checks=1,
            next_wait_seconds=90,
        )
        assert "1 check done" in out
        assert "checks" not in out.replace("1 check done", "")

    def test_settling_status(self):
        out = metrics.format_wait_heartbeat(
            "settling",
            author="gemini-code-assist",
            elapsed_seconds=120,
            checks=3,
            next_wait_seconds=30,
            quiet_period_remaining_seconds=30,
        )
        assert out == (
            "[loop] Reviewer responded — waiting for review threads to settle, "
            "30s quiet period remaining"
        )

    def test_timed_out_status(self):
        out = metrics.format_wait_heartbeat(
            "timed_out",
            author="gemini-code-assist",
            elapsed_seconds=905,
            checks=11,
        )
        assert out == (
            "[loop] wait timed out after 15m — The reviewer did not confirm; "
            "record with --gemini-unconfirmed"
        )

    def test_unknown_status_returns_empty(self):
        assert metrics.format_wait_heartbeat("ready", author="x", elapsed_seconds=1, checks=1) == ""
        assert metrics.format_wait_heartbeat("", author="x", elapsed_seconds=1, checks=1) == ""

    def test_corrupt_counts_clamped(self):
        out = metrics.format_wait_heartbeat(
            "waiting",
            author="gemini-code-assist",
            elapsed_seconds=-5,
            checks="bogus",
            next_wait_seconds=None,
        )
        assert "0s elapsed" in out
        assert "0 checks done" in out
        assert "next check in 0s" in out


class TestFormatStats:
    def test_empty_message(self):
        out = metrics.format_stats("o/r", {"count": 0})
        assert "No review loop runs recorded yet" in out

    def test_full_output_with_judge_footnote(self):
        agg = {
            "count": 10, "avg_cycles": 1.8, "avg_duration": 540.0,
            "total_fixed": 32, "total_fetched": 41, "needs_human": 6,
            "addressed_by_reply": 9, "judged_count": 6,
            "false_positives_avoided": 14, "top_provider": "gemini-code-assist",
            "top_area": "tests",
        }
        out = metrics.format_stats("OrenAshkenazy/gh-review-loop", agg)
        assert "Last 10 runs" in out
        assert "Average re-review requests used: 1.8" in out
        assert "Average elapsed time to terminal outcome: 9m" in out
        assert "Findings fixed: 32 of 41" in out
        assert "False positives avoided: 14   (across 6 of 10 judged runs)" in out
        assert "Most repeated finding area: tests" in out

    def test_judge_line_omitted_when_no_judged_runs(self):
        agg = {
            "count": 2, "avg_cycles": 1.0, "avg_duration": None,
            "total_fixed": 1, "total_fetched": 2, "needs_human": 0,
            "addressed_by_reply": 0, "judged_count": 0,
            "false_positives_avoided": 0, "top_provider": "gemini-code-assist",
            "top_area": None,
        }
        out = metrics.format_stats("o/r", agg)
        assert "False positives avoided" not in out
        assert "Average elapsed time to terminal outcome" not in out  # avg_duration None

    def test_elapsed_split_lines_rendered(self):
        agg = {
            "count": 5, "avg_cycles": 2.0, "avg_duration": 700.0,
            "avg_duration_clean": 600.0, "avg_duration_capped": 1000.0,
            "avg_duration_failed": 300.0,
            "total_fixed": 5, "total_fetched": 8, "needs_human": 0,
            "addressed_by_reply": 0, "judged_count": 0,
            "false_positives_avoided": 0, "top_provider": None, "top_area": None,
        }
        out = metrics.format_stats("o/r", agg)
        assert "Average elapsed time to terminal outcome: 11m" in out   # 700s floors to 11m
        assert "Average elapsed time to clean PR: 10m" in out           # 600s
        assert "Average elapsed time to capped run: 16m" in out         # 1000s floors to 16m
        assert "Average elapsed time to failed run: 5m" in out          # 300s

    def test_elapsed_split_lines_omitted_when_none(self):
        agg = {
            "count": 1, "avg_cycles": 1.0, "avg_duration": 600.0,
            "avg_duration_clean": 600.0, "avg_duration_capped": None,
            "avg_duration_failed": None,
            "total_fixed": 1, "total_fetched": 1, "needs_human": 0,
            "addressed_by_reply": 0, "judged_count": 0,
            "false_positives_avoided": 0, "top_provider": None, "top_area": None,
        }
        out = metrics.format_stats("o/r", agg)
        assert "Average elapsed time to clean PR: 10m" in out
        assert "Average elapsed time to capped run" not in out
        assert "Average elapsed time to failed run" not in out

    def test_active_metrics_rendered(self):
        agg = {
            "count": 3, "avg_cycles": 2.0, "avg_duration": 600.0,
            "avg_active_cycle_time": 233.0, "avg_active_time_per_run": 350.0,
            "avg_cycles_per_run": 1.5,
            "total_fixed": 3, "total_fetched": 4, "needs_human": 0,
            "addressed_by_reply": 0, "judged_count": 0,
            "false_positives_avoided": 0, "top_provider": None, "top_area": None,
        }
        out = metrics.format_stats("o/r", agg)
        assert "Average active cycle time: 3m" in out
        assert "Average active time per run: 5m" in out
        assert "Average cycles per run: 1.5" in out

    def test_active_metrics_omitted_and_legacy_renders_safely(self):
        # An agg dict from older records (no active-metric keys at all) must
        # render without KeyError and without the active lines.
        agg = {
            "count": 2, "avg_cycles": 1.0, "avg_duration": 300.0,
            "total_fixed": 1, "total_fetched": 2, "needs_human": 0,
            "addressed_by_reply": 0, "judged_count": 0,
            "false_positives_avoided": 0, "top_provider": None, "top_area": None,
        }
        out = metrics.format_stats("o/r", agg)  # must not raise
        assert "Average active cycle time" not in out
        assert "Average active time per run" not in out
        assert "Average cycles per run" not in out

    def test_skipped_footnote(self):
        agg = {
            "count": 1, "avg_cycles": 1.0, "avg_duration": None,
            "total_fixed": 0, "total_fetched": 0, "needs_human": 0,
            "addressed_by_reply": 0, "judged_count": 0,
            "false_positives_avoided": 0, "top_provider": None, "top_area": None,
        }
        out = metrics.format_stats("o/r", agg, skipped=2)
        assert "(2 unreadable records skipped)" in out

def test_format_patterns_block_orders_and_lists_sites():
    clusters = [
        Cluster(signature="a1b2c3d4", label="tab-vs-space indent detection",
                severity="high", sites=["config_parser.py:68"], count=1),
        Cluster(signature="e5f6a7b8", label="missing isinstance guard",
                severity="medium",
                sites=[f"f{i}.py:{i}" for i in range(8)], count=8),
    ]
    block = format_patterns_block(clusters)
    assert "Patterns (2):" in block
    assert "[high]" in block and "[medium]" in block
    assert "sig: a1b2c3d4" in block
    assert "1 site" in block and "8 sites" in block
    assert "+" in block and "more" in block


def test_format_patterns_block_empty():
    assert format_patterns_block([]) == ""


def test_degenerate_clustering_advisory_fires_for_three_singletons():
    clusters = [
        Cluster(signature=f"sig-{i}", label=f"finding {i}", severity="medium",
                sites=[f"src/file{i}.py:1"], count=1)
        for i in range(3)
    ]

    advisory = format_degenerate_clustering_advisory(clusters)

    assert "prose-hash fallbacks" in advisory
    assert "manual sweep is required before fixing" in advisory


def test_degenerate_clustering_advisory_omitted_for_multi_site_cluster():
    clusters = [
        Cluster(signature="shared", label="shared finding", severity="medium",
                sites=["src/a.py:1", "src/b.py:1"], count=2),
        Cluster(signature="single", label="single finding", severity="low",
                sites=["src/c.py:1"], count=1),
        Cluster(signature="another", label="another finding", severity="low",
                sites=["src/d.py:1"], count=1),
    ]

    assert format_degenerate_clustering_advisory(clusters) == ""


def test_degenerate_clustering_advisory_omitted_below_three_findings():
    clusters = [
        Cluster(signature=f"sig-{i}", label=f"finding {i}", severity="medium",
                sites=[f"src/file{i}.py:1"], count=1)
        for i in range(2)
    ]

    assert format_degenerate_clustering_advisory(clusters) == ""


def test_format_convergence_line_plain():
    line = format_convergence_line(
        {"distinct_patterns": 2, "recurrence_rate": 0.0, "recurred_after_sweep": []},
        swept_count=1,
    )
    assert "Convergence:" in line
    assert "2 distinct patterns" in line
    assert "Swept 1 pattern" in line
    assert "⚠" not in line


def test_format_convergence_line_recurred_after_sweep_warns():
    line = format_convergence_line(
        {"distinct_patterns": 1, "recurrence_rate": 1.0, "recurred_after_sweep": ["e5f6a7b8"]},
        swept_count=1,
    )
    assert "⚠" in line
    assert "RECURRED after sweep" in line

def _min_record_kwargs():
    return dict(
        repo="o/r", pr=46, provider="gemini-code-assist",
        findings_fetched=3, fixed_count=3, observed_fixed_count=3,
        remaining_actionable=0, needs_human=0, addressed_by_reply=0,
        cycles_used=2, cycle_cap=5, verification="passed",
        verification_details=None, outcome="clean", outcome_reason="",
        started_at=None, finding_paths=[], judge=None,
    )


def test_build_record_includes_patterns_block_when_passed():
    rec = build_record(**_min_record_kwargs(), patterns={
        "distinct_patterns": 4, "max_cluster_size": 14,
        "pattern_recurrence_rate": 0.0, "swept_count": 1,
    })
    assert rec["patterns"]["max_cluster_size"] == 14
    assert rec["patterns"]["swept_count"] == 1


def test_build_record_patterns_defaults_to_empty_dict():
    rec = build_record(**_min_record_kwargs())
    assert rec["patterns"] == {}

def _write_run(path, repo, pr, signatures, swept):
    import json
    rec = {
        "schema_version": RECORD_SCHEMA_VERSION, "repo": repo, "pr": pr,
        "patterns": {"signatures": signatures, "swept": swept},
    }
    with path.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_pattern_history_unions_signatures_and_swept_for_matching_pr(tmp_path):
    log = tmp_path / "runs.jsonl"
    _write_run(log, "o/r", 46, ["type-guard", "io-decode-guard"], ["type-guard"])
    _write_run(log, "o/r", 46, ["io-decode-guard", "yaml-scalar-parse"], ["io-decode-guard"])
    _write_run(log, "o/r", 99, ["other-pattern"], ["other-pattern"])  # different PR, ignored
    hist = pattern_history_for_pr("o/r", 46, path=log)
    assert hist["seen"] == {"type-guard", "io-decode-guard", "yaml-scalar-parse"}
    assert hist["swept"] == {"type-guard", "io-decode-guard"}
    assert "other-pattern" not in hist["seen"]


def test_pattern_history_empty_when_no_log(tmp_path):
    hist = pattern_history_for_pr("o/r", 46, path=tmp_path / "missing.jsonl")
    assert hist == {"seen": set(), "swept": set()}


class TestFormatCompactReceiptLine:
    """The one-line chat pointer that replaces the full stdout receipt (#86)."""

    RECORD = {
        "findings_fetched": 4,
        "fixed_count": 3,
        "remaining_actionable": 1,
        "cycles_used": 2,
        "cycle_cap": 3,
        "verification": "passed",
        "outcome": "clean",
    }
    URL = "https://github.com/o/r/pull/7#issuecomment-99"

    def test_cycle_line_is_one_line_with_counts_and_url(self):
        out = metrics.format_compact_receipt_line(
            self.RECORD, terminal=False, receipt_url=self.URL,
            findings_new=1, findings_carried=0,
        )
        assert "\n" not in out
        assert out.startswith("[loop] Cycle receipt: ")
        assert "findings 4 seen this run" in out
        assert "fixed locally 3" in out
        assert "open 1 (1 new, 0 carried over)" in out
        assert "re-review requests 2/3" in out
        assert "verification passed" in out
        assert out.endswith(self.URL)
        assert "outcome" not in out  # cycle lines carry no terminal outcome

    def test_terminal_line_carries_outcome(self):
        out = metrics.format_compact_receipt_line(
            self.RECORD, terminal=True, receipt_url=self.URL,
        )
        assert out.startswith("[loop] Summary: ")
        assert "fixed 3" in out
        assert "outcome clean" in out
        assert "(" not in out  # no new/carried split without the counts

    def test_session_cycle_renders_in_header(self):
        # Script-owned narration ordinal (#104): the agent copies N from here.
        record = dict(self.RECORD, session_cycle=2)
        out = metrics.format_compact_receipt_line(
            record, terminal=False, receipt_url=self.URL,
        )
        assert out.startswith("[loop] Cycle receipt (session cycle 2): ")

    def test_missing_session_cycle_keeps_bare_header(self):
        out = metrics.format_compact_receipt_line(
            self.RECORD, terminal=False, receipt_url=self.URL,
        )
        assert out.startswith("[loop] Cycle receipt: ")

    def test_zero_remaining_omits_open(self):
        record = dict(self.RECORD, remaining_actionable=0)
        out = metrics.format_compact_receipt_line(
            record, terminal=True, receipt_url=self.URL,
        )
        assert "open" not in out

    def test_corrupt_counts_render_as_zero(self):
        out = metrics.format_compact_receipt_line(
            {"verification": "skipped"}, terminal=False, receipt_url=self.URL,
        )
        assert "findings 0 seen this run" in out
        assert "re-review requests 0/0" in out

    # --- shared-denominator guard (#90 review) ---------------------------

    def test_cumulative_total_never_carries_the_current_split(self):
        # cycle 1 found 4; the next review leaves 1 open. The split describes
        # the open set, so it must not hang off the cumulative "4".
        out = metrics.format_compact_receipt_line(
            self.RECORD, terminal=False, receipt_url=self.URL,
            findings_new=0, findings_carried=1,
        )
        assert "findings 4 (" not in out
        assert "findings 4 seen this run" in out
        assert "open 1 (0 new, 1 carried over)" in out

    def test_clean_terminal_run_has_no_empty_split(self):
        # The old formatter rendered "findings 4 (0 new, 0 carried over)" here.
        record = dict(self.RECORD, remaining_actionable=0)
        out = metrics.format_compact_receipt_line(
            record, terminal=True, receipt_url=self.URL,
            findings_new=0, findings_carried=0,
        )
        assert "0 new" not in out
        assert "carried over" not in out
        assert "findings 4 seen this run" in out

    def test_split_is_dropped_when_it_does_not_account_for_the_open_set(self):
        # Guard against a future caller passing counts from another population.
        out = metrics.format_compact_receipt_line(
            self.RECORD, terminal=False, receipt_url=self.URL,
            findings_new=1, findings_carried=3,  # 4 != remaining_actionable 1
        )
        assert "carried over" not in out
        assert "open 1" in out
