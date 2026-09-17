"""Unit tests for pure functions in fetch_gemini_threads.py.

These tests intentionally avoid network/gh calls — they exercise only the
pure helpers that operate on already-fetched GraphQL payloads.
"""

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import cluster_findings
import fetch_gemini_threads as fgt
import metrics

from fetch_gemini_threads import (
    ADDRESSED_BY_REPLY_MIN_CHARS,
    PAGE_LIMIT_REVIEW_THREADS,
    PAGE_LIMIT_THREAD_COMMENTS,
    STICKY_RECEIPT_MARKER,
    PullRequest,
    accumulate_judge_results,
    addressed_by_reply_threads,
    _derive_outcome,
    clear_run_tracking,
    derive_record_fields,
    detect_no_progress,
    format_judge_thread_table,
    format_judge_verdict_summary,
    merge_judge_results,
    any_active_run,
    classify_remaining_finding_states,
    count_likely_fixed_remaining,
    effective_rereview_limit,
    filter_by_min_severity,
    find_active_run,
    stamp_summary_emitted,
    summary_is_stale,
    filter_threads,
    is_addressed_by_reply,
    load_preferences_with_fallback,
    load_sticky_state,
    nonnegative_int,
    pagination_warnings,
    parse_pr_url,
    read_run_tracking,
    record_cycle,
    render_receipt,
    rereview_requests,
    review_activity_fingerprint,
    save_sticky_state,
    select_stats_records,
    severity_counts,
    sort_by_severity,
    sticky_state_path,
    thread_fingerprint,
    thread_severity,
    update_run_tracking,
    finding_fingerprint,
    track_finding_fingerprints,
    prior_finding_fingerprints,
    wait_for_stable_review,
    resolve_judge_phase,
)


BOT = "gemini-code-assist"


def test_review_query_fetches_range_start_fields_at_both_levels():
    assert fgt.QUERY.count("startLine") == 2
    assert fgt.QUERY.count("originalStartLine") == 2


CODEX = "chatgpt-codex-connector"


def make_thread(*, resolved=False, outdated=False, comments):
    """Build a GraphQL-shaped thread for tests."""
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": "src/x.py",
        "line": 10,
        "comments": {"nodes": comments},
    }


def bot_comment(body="Use snake_case here."):
    return {"author": {"login": BOT}, "body": body}


def human_comment(body, login="alice"):
    return {"author": {"login": login}, "body": body}


# ---------------------------------------------------------------------------
# parse_pr_url
# ---------------------------------------------------------------------------

class TestParsePrUrl:
    def test_canonical_url(self):
        pr = parse_pr_url("https://github.com/o/r/pull/42")
        assert pr.owner == "o" and pr.repo == "r" and pr.number == 42

    def test_url_with_trailing_path(self):
        pr = parse_pr_url("https://github.com/o/r/pull/42/files")
        assert pr is not None and pr.number == 42

    def test_url_with_query(self):
        pr = parse_pr_url("https://github.com/o/r/pull/42?foo=bar")
        assert pr is not None and pr.number == 42

    def test_invalid_returns_none(self):
        assert parse_pr_url("not-a-url") is None
        assert parse_pr_url("https://example.com/foo") is None


# ---------------------------------------------------------------------------
# is_addressed_by_reply
# ---------------------------------------------------------------------------

class TestIsAddressedByReply:
    def test_no_reply_returns_false(self):
        t = make_thread(comments=[bot_comment()])
        assert is_addressed_by_reply(t, BOT) is False

    def test_token_ack_returns_false(self):
        t = make_thread(comments=[bot_comment(), human_comment("ok")])
        assert is_addressed_by_reply(t, BOT) is False

    def test_substantive_reply_returns_true(self):
        long = "Wontfix: existing convention in this module is camelCase."
        t = make_thread(comments=[bot_comment(), human_comment(long)])
        assert is_addressed_by_reply(t, BOT) is True

    def test_bot_reply_does_not_count(self):
        long = "Some other bot is chiming in with a long-enough comment here."
        t = make_thread(comments=[bot_comment(), human_comment(long, login="github-actions[bot]")])
        assert is_addressed_by_reply(t, BOT) is False

    def test_resolved_returns_false(self):
        long = "Wontfix: existing convention in this module is camelCase."
        t = make_thread(resolved=True, comments=[bot_comment(), human_comment(long)])
        assert is_addressed_by_reply(t, BOT) is False

    def test_outdated_returns_false(self):
        long = "Wontfix: existing convention in this module is camelCase."
        t = make_thread(outdated=True, comments=[bot_comment(), human_comment(long)])
        assert is_addressed_by_reply(t, BOT) is False

    def test_exactly_min_chars_returns_true(self):
        body = "x" * ADDRESSED_BY_REPLY_MIN_CHARS
        t = make_thread(comments=[bot_comment(), human_comment(body)])
        assert is_addressed_by_reply(t, BOT) is True

    def test_one_below_min_chars_returns_false(self):
        body = "x" * (ADDRESSED_BY_REPLY_MIN_CHARS - 1)
        t = make_thread(comments=[bot_comment(), human_comment(body)])
        assert is_addressed_by_reply(t, BOT) is False


# ---------------------------------------------------------------------------
# addressed_by_reply_threads + filter_threads interaction
# ---------------------------------------------------------------------------

def _pr_with_threads(threads):
    return {"reviewThreads": {"nodes": threads}}


class TestThreadFiltering:
    def test_addressed_by_reply_threads_isolation(self):
        long = "Wontfix: existing convention in this module is camelCase."
        threads = [
            make_thread(comments=[bot_comment()]),  # actionable
            make_thread(comments=[bot_comment(), human_comment(long)]),  # addressed
            make_thread(resolved=True, comments=[bot_comment()]),  # resolved
            make_thread(outdated=True, comments=[bot_comment()]),  # outdated
        ]
        assert len(addressed_by_reply_threads(_pr_with_threads(threads), BOT)) == 1

    def test_filter_threads_default_excludes_addressed_by_reply(self):
        long = "Wontfix: existing convention in this module is camelCase."
        threads = [
            make_thread(comments=[bot_comment()]),
            make_thread(comments=[bot_comment(), human_comment(long)]),
        ]
        result = filter_threads(_pr_with_threads(threads), author=BOT,
                                 include_resolved=False, include_outdated=False)
        assert len(result) == 1

    def test_filter_threads_include_addressed_by_reply(self):
        long = "Wontfix: existing convention in this module is camelCase."
        threads = [
            make_thread(comments=[bot_comment()]),
            make_thread(comments=[bot_comment(), human_comment(long)]),
        ]
        result = filter_threads(_pr_with_threads(threads), author=BOT,
                                 include_resolved=False, include_outdated=False,
                                 include_addressed_by_reply=True)
        assert len(result) == 2

    def test_filter_threads_drops_threads_without_author_comments(self):
        threads = [make_thread(comments=[human_comment("hello there friend, this is a long enough comment", login="bob")])]
        result = filter_threads(_pr_with_threads(threads), author=BOT,
                                 include_resolved=False, include_outdated=False)
        assert result == []


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

class TestSeverity:
    @pytest.mark.parametrize("alt,expected", [
        ("critical", "critical"), ("high", "high"),
        ("medium", "medium"), ("low", "low"),
        ("HIGH", "high"), ("Critical", "critical"),
    ])
    def test_extracts_severity_from_alt_text(self, alt, expected):
        body = f"![{alt}](https://www.gstatic.com/codereviewagent/{alt.lower()}-priority.svg) Use X."
        t = make_thread(comments=[bot_comment(body)])
        assert thread_severity(t) == expected

    def test_no_marker_returns_unknown(self):
        assert thread_severity(make_thread(comments=[bot_comment("plain comment")])) == "unknown"

    @pytest.mark.parametrize("priority,expected", [
        ("P0", "critical"), ("P1", "high"), ("P2", "medium"), ("P3", "low"),
    ])
    def test_normalizes_codex_priority_badges(self, priority, expected):
        # Real Codex shape, captured from a live review comment.
        body = (
            f"**<sub><sub>![{priority} Badge]"
            f"(https://img.shields.io/badge/{priority}-yellow?style=flat)</sub></sub>"
            "  Add a timeout to direct HarvestAPI requests**\n\nIf HarvestAPI ..."
        )
        t = make_thread(comments=[{"author": {"login": CODEX}, "body": body}])
        assert thread_severity(t) == expected

    def test_handles_post_filter_flat_shape(self):
        # filter_threads flattens comments to a plain list; severity must still work
        flat = {"comments": [{"author": {"login": BOT}, "body": "![low](x)"}]}
        assert thread_severity(flat) == "low"

    def test_sort_orders_by_severity_descending(self):
        def t(s):
            return make_thread(comments=[bot_comment(f"![{s}](x)" if s else "x")])
        unsorted = [t("low"), t("critical"), t(None), t("medium"), t("high")]
        sevs = [thread_severity(x) for x in sort_by_severity(unsorted)]
        assert sevs == ["critical", "high", "medium", "low", "unknown"]

    def test_sort_is_stable_within_severity(self):
        def t(s, marker):
            body = f"![{s}](x) {marker}"
            return make_thread(comments=[bot_comment(body)])
        items = [t("high", "A"), t("high", "B"), t("high", "C")]
        out = sort_by_severity(items)
        markers = [o["comments"]["nodes"][0]["body"].split()[-1] for o in out]
        assert markers == ["A", "B", "C"]

    def test_counts(self):
        def t(s):
            return make_thread(comments=[bot_comment(f"![{s}](x)" if s else "x")])
        counts = severity_counts([t("high"), t("high"), t("low"), t(None)])
        assert counts == {"high": 2, "low": 1, "unknown": 1}


# ---------------------------------------------------------------------------
# rereview_requests
# ---------------------------------------------------------------------------

class TestRereviewRequests:
    @staticmethod
    def _pr(comments):
        return {"comments": {"nodes": comments}}

    def test_counts_matching_trigger(self):
        pr = self._pr([
            {"author": {"login": "a"}, "body": "@codex review"},
            {"author": {"login": "b"}, "body": "unrelated"},
        ])
        assert len(rereview_requests(pr)) == 1

    def test_counts_configured_reviewer_trigger(self):
        pr = self._pr([
            {"author": {"login": "a"}, "body": "@coderabbitai please review"},
            {"author": {"login": "b"}, "body": "@codex review"},
        ])
        result = rereview_requests(pr, review_trigger_mention="@coderabbitai")
        assert len(result) == 1
        assert result[0]["author"]["login"] == "a"

    def test_counts_configured_bot_suffix_trigger(self):
        pr = self._pr([
            {"author": {"login": "a"}, "body": "@renovate[bot] please review"},
        ])

        result = rereview_requests(pr, review_trigger_mention="@renovate[bot]")

        assert len(result) == 1

    def test_bot_suffix_trigger_does_not_match_a_longer_login(self):
        pr = self._pr([
            {"author": {"login": "a"}, "body": "@renovate[bot]extra please review"},
        ])

        result = rereview_requests(pr, review_trigger_mention="@renovate[bot]")

        assert result == []

    def test_filter_by_agent_login(self):
        pr = self._pr([
            {"author": {"login": "agent"}, "body": "@codex review"},
            {"author": {"login": "human"}, "body": "@codex can you review again?"},
        ])
        assert len(rereview_requests(pr, agent_login="agent")) == 1
        assert len(rereview_requests(pr, agent_login="human")) == 1
        assert len(rereview_requests(pr, agent_login="nobody")) == 0
        assert len(rereview_requests(pr)) == 2  # no filter

    def test_ignores_comment_without_review_word(self):
        pr = self._pr([{"author": {"login": "a"}, "body": "@codex hi"}])
        assert rereview_requests(pr) == []


# ---------------------------------------------------------------------------
# re-review cap preference
# ---------------------------------------------------------------------------

class TestRereviewLimit:
    def test_cli_value_wins_over_preferences(self):
        assert effective_rereview_limit(2, {"max_rereview_requests": 5}) == 2

    def test_preferences_used_when_cli_absent(self):
        assert effective_rereview_limit(None, {"max_rereview_requests": 5}) == 5

    def test_invalid_preference_falls_back_to_default(self):
        assert effective_rereview_limit(None, {"max_rereview_requests": -1}) == 3
        assert effective_rereview_limit(None, {"max_rereview_requests": True}) == 3
        assert effective_rereview_limit(None, {"max_rereview_requests": "five"}) == 3

    def test_string_preference_is_coerced(self):
        assert effective_rereview_limit(None, {"max_rereview_requests": "5"}) == 5
        assert effective_rereview_limit(None, {"max_rereview_requests": " 6 "}) == 6


class TestReviewerRefusal:
    """A reviewer that declines outright must end the wait, not run out the clock."""

    AFTER = "2026-08-04T12:45:54Z"

    @staticmethod
    def _pr(*comments):
        return {"comments": {"nodes": list(comments)}}

    @staticmethod
    def _comment(login, body, created_at="2026-08-04T12:46:06Z"):
        return {
            "author": {"login": login},
            "body": body,
            "createdAt": created_at,
            "url": "https://github.com/o/r/pull/5#issuecomment-9",
        }

    # Verbatim from a live Codex reply.
    CODEX_LIMIT = (
        "You have reached your Codex usage limits for code reviews. "
        "Your limits reset at 3:00 PM."
    )
    # Verbatim shape of Gemini's shutdown notice.
    GEMINI_SUNSET = (
        "> [!CAUTION]\n> The consumer version of Gemini Code Assist on GitHub "
        "has been sunset. All code review activity has officially ended."
    )

    def test_detects_a_usage_limit_refusal(self):
        refusal = fgt.reviewer_refusal(
            self._pr(self._comment(CODEX, self.CODEX_LIMIT)), CODEX, after_iso=self.AFTER
        )

        assert refusal is not None
        assert refusal["reason"].startswith("You have reached your Codex usage limits")
        assert refusal["created_at"] == "2026-08-04T12:46:06Z"
        assert refusal["url"].endswith("#issuecomment-9")

    def test_a_usage_limit_is_flagged_as_recoverable_quota(self):
        # Recoverable: the user can lift the cap, so the loop must ask them.
        refusal = fgt.reviewer_refusal(
            self._pr(self._comment(CODEX, self.CODEX_LIMIT)), CODEX, after_iso=self.AFTER
        )

        assert refusal["kind"] == fgt.REFUSAL_QUOTA

    def test_detects_a_service_sunset_notice(self):
        refusal = fgt.reviewer_refusal(
            self._pr(self._comment(BOT, self.GEMINI_SUNSET)), BOT, after_iso=self.AFTER
        )

        assert refusal is not None
        assert "sunset" in refusal["reason"]

    def test_a_sunset_notice_is_not_recoverable(self):
        refusal = fgt.reviewer_refusal(
            self._pr(self._comment(BOT, self.GEMINI_SUNSET)), BOT, after_iso=self.AFTER
        )

        assert refusal["kind"] == fgt.REFUSAL_WITHDRAWN

    def test_ignores_a_refusal_posted_before_the_anchor(self):
        stale = self._comment(CODEX, self.CODEX_LIMIT, created_at="2026-08-04T12:00:00Z")

        assert fgt.reviewer_refusal(self._pr(stale), CODEX, after_iso=self.AFTER) is None

    def test_ignores_refusal_wording_from_someone_else(self):
        human = self._comment("alice", self.CODEX_LIMIT)

        assert fgt.reviewer_refusal(self._pr(human), CODEX, after_iso=self.AFTER) is None

    def test_ordinary_reviewer_comments_are_not_refusals(self):
        chatter = self._comment(CODEX, "Reviewing now — one moment.")

        assert fgt.reviewer_refusal(self._pr(chatter), CODEX, after_iso=self.AFTER) is None

    def test_discussing_rate_limits_in_prose_is_not_a_refusal(self):
        # The reviewer talking *about* rate limiting must not end the wait.
        prose = self._comment(
            CODEX,
            "The new client retries on HTTP 429; consider whether the rate limit "
            "backoff should be exponential rather than fixed.",
        )

        assert fgt.reviewer_refusal(self._pr(prose), CODEX, after_iso=self.AFTER) is None


class TestReviewBodyFindings:
    """Codex sometimes puts a priority-badged finding in the review body itself."""

    @staticmethod
    def _pr(*reviews):
        return {"reviews": {"nodes": list(reviews)}}

    @staticmethod
    def _review(body, submitted_at="2026-08-04T09:57:39Z", review_id="R_1"):
        return {
            "id": review_id,
            "author": {"login": CODEX},
            "body": body,
            "submittedAt": submitted_at,
            "url": "https://github.com/o/r/pull/5#pullrequestreview-1",
        }

    BODY = (
        "\n### 💡 Codex Review\n\nHere are some automated review suggestions.\n\n"
        "**Reviewed commit:** `90a23ce28d`\n\n"
        "https://github.com/o/r/blob/90a23ce28d/src/enrich.js#L42\n\n"
        "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)"
        "</sub></sub>  Guard the quota refusal path**\n\nIf the provider refuses ...\n\n"
        "https://github.com/o/r/blob/90a23ce28d/src/map.js#L7\n\n"
        "**<sub><sub>![P3 Badge](https://img.shields.io/badge/P3-blue?style=flat)"
        "</sub></sub>  Rename the mapping helper**\n\nMinor readability nit ...\n"
    )

    def test_surfaces_each_badged_body_finding_with_its_file_anchor(self):
        findings = fgt.review_body_findings(self._pr(self._review(self.BODY)), CODEX)

        assert [(f["path"], f["line"]) for f in findings] == [
            ("src/enrich.js", 42),
            ("src/map.js", 7),
        ]
        assert [thread_severity(f) for f in findings] == ["high", "low"]
        assert all(f["isResolved"] is False for f in findings)

    def test_body_findings_have_stable_distinct_ids(self):
        findings = fgt.review_body_findings(self._pr(self._review(self.BODY)), CODEX)
        again = fgt.review_body_findings(self._pr(self._review(self.BODY)), CODEX)

        ids = [f["id"] for f in findings]
        assert len(set(ids)) == 2
        assert ids == [f["id"] for f in again]

    def test_review_body_without_badges_yields_nothing(self):
        body = "\n### 💡 Codex Review\n\nNo major issues found.\n"
        assert fgt.review_body_findings(self._pr(self._review(body)), CODEX) == []

    def test_only_the_latest_review_body_is_active(self):
        stale = self._review(self.BODY, submitted_at="2026-08-04T09:57:39Z", review_id="R_1")
        latest = self._review(
            "\n### 💡 Codex Review\n\nNo major issues found.\n",
            submitted_at="2026-08-04T11:37:37Z",
            review_id="R_2",
        )

        assert fgt.review_body_findings(self._pr(stale, latest), CODEX) == []

    def test_other_authors_review_bodies_are_ignored(self):
        review = dict(self._review(self.BODY), author={"login": "sourcery-ai"})

        assert fgt.review_body_findings(self._pr(review), CODEX) == []


class TestReviewerSelectionCli:
    PR = PullRequest(owner="o", repo="r", number=5)

    @staticmethod
    def _thread(login, thread_id):
        return {
            "id": thread_id,
            "isResolved": False,
            "isOutdated": False,
            "path": "src/x.py",
            "line": 10,
            "comments": {
                "nodes": [
                    {
                        "author": {"login": login, "__typename": "Bot"},
                        "body": "review note",
                        "createdAt": "2026-06-28T12:00:00Z",
                    }
                ]
            },
        }

    def _pull_request(self):
        return {
            "number": 5,
            "url": "https://github.com/o/r/pull/5",
            "comments": {"nodes": []},
            "reviews": {"nodes": []},
            "reviewThreads": {
                "nodes": [
                    self._thread("chatgpt-codex-connector", "T_default"),
                    self._thread("coderabbitai", "T_code_rabbit"),
                ]
            },
        }

    def _patch_common(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "resolve_pr", lambda arg: self.PR)
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: self._pull_request())
        monkeypatch.setattr(fgt, "gh_authenticated_login", lambda: "codex-agent")

    def test_default_author_is_marked_unconfirmed_in_json(
        self, tmp_path, monkeypatch, capsys
    ):
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_gemini_threads.py",
                "--pr",
                "https://github.com/o/r/pull/5",
                "--format",
                "json",
                "--no-resolve-outdated",
                "--no-resolve-addressed-by-reply",
            ],
        )

        assert fgt.main() == 0

        payload = json.loads(capsys.readouterr().out)
        selection = payload["reviewerSelection"]
        # No reviewer is assumed. The loop reports the gap and offers one.
        assert selection["source"] == "none_configured"
        assert selection["configured"] is False
        assert selection["confirmation_required"] is True
        assert selection["unconfirmed"] is True
        assert selection["suggestion"]["login"] == "chatgpt-codex-connector"
        # The offer must be a ping-first vendor, or accepting it starts a wait.
        assert selection["suggestion"]["auto_reviews"] is False
        assert "Do not wait on an unconfirmed reviewer" in selection["message"]
        assert [thread["id"] for thread in payload["threads"]] == ["T_default"]

    def test_markdown_fetch_warns_before_fixing_degenerate_clusters(
        self, tmp_path, monkeypatch, capsys
    ):
        self._patch_common(monkeypatch, tmp_path)
        pull_request = self._pull_request()
        pull_request["reviewThreads"]["nodes"] = [
            {
                **self._thread("chatgpt-codex-connector", f"T{i}"),
                "path": f"src/file{i}.py",
                "comments": {
                    "nodes": [{
                        "author": {
                            "login": "chatgpt-codex-connector",
                            "__typename": "Bot",
                        },
                        "body": body,
                        "createdAt": "2026-06-28T12:00:00Z",
                    }]
                },
            }
            for i, body in enumerate([
                "Validate the configuration before use",
                "Document the public return value",
                "Avoid rebuilding this cache repeatedly",
            ])
        ]
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: pull_request)
        monkeypatch.setattr(fgt, "repo_root", lambda: None)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_gemini_threads.py",
                "--pr", "https://github.com/o/r/pull/5",
                "--author", "chatgpt-codex-connector",
                "--judge-mode", "off",
                "--no-agent-filter",
                "--no-resolve-outdated",
                "--no-resolve-addressed-by-reply",
            ],
        )

        assert fgt.main() == 0

        out = capsys.readouterr().out
        assert "# Codex Threads for PR #5" in out
        assert "Patterns (3):" in out
        assert "manual sweep is required before fixing" in out
        assert out.index("manual sweep is required before fixing") > out.index(
            "Avoid rebuilding this cache repeatedly"
        )
        assert "[loop] Cycle receipt" not in out
        assert "Verification:" not in out

    def test_json_fetch_serializes_degenerate_clustering_guard(
        self, tmp_path, monkeypatch, capsys
    ):
        self._patch_common(monkeypatch, tmp_path)
        pull_request = self._pull_request()
        pull_request["reviewThreads"]["nodes"] = [
            {
                **self._thread("chatgpt-codex-connector", f"T{i}"),
                "path": f"src/file{i}.py",
                "comments": {
                    "nodes": [{
                        "author": {
                            "login": "chatgpt-codex-connector",
                            "__typename": "Bot",
                        },
                        "body": body,
                        "createdAt": "2026-06-28T12:00:00Z",
                    }]
                },
            }
            for i, body in enumerate([
                "Validate the configuration before use",
                "Document the public return value",
                "Avoid rebuilding this cache repeatedly",
            ])
        ]
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: pull_request)
        monkeypatch.setattr(fgt, "repo_root", lambda: None)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_gemini_threads.py",
                "--pr", "https://github.com/o/r/pull/5",
                "--author", "chatgpt-codex-connector",
                "--format", "json",
                "--judge-mode", "off",
                "--no-agent-filter",
                "--no-resolve-outdated",
                "--no-resolve-addressed-by-reply",
            ],
        )

        assert fgt.main() == 0

        clustering = json.loads(capsys.readouterr().out)["clustering"]
        assert clustering["clusterCount"] == 3
        assert clustering["maxClusterSize"] == 1
        assert "manual sweep is required before fixing" in clustering["advisory"]

    def test_persisted_reviewer_is_reused_without_prompt(
        self, tmp_path, monkeypatch, capsys
    ):
        self._patch_common(monkeypatch, tmp_path)
        fgt.save_reviewer_selection(
            self.PR,
            {
                "login": "coderabbitai",
                "display_name": "CodeRabbit",
                "review_trigger": "@coderabbitai",
                "source": "confirmed",
                "selected_at": "2026-06-28T12:00:00Z",
            },
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_gemini_threads.py",
                "--pr",
                "https://github.com/o/r/pull/5",
                "--format",
                "json",
                "--no-resolve-outdated",
                "--no-resolve-addressed-by-reply",
            ],
        )

        assert fgt.main() == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["reviewerSelection"]["source"] == "persisted"
        assert payload["reviewerSelection"]["confirmation_required"] is False
        assert payload["reviewerSelection"]["login"] == "coderabbitai"
        assert [thread["id"] for thread in payload["threads"]] == ["T_code_rabbit"]

    def test_codex_selection_reports_its_trigger_and_that_it_needs_a_ping(
        self, tmp_path, monkeypatch, capsys
    ):
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_gemini_threads.py",
                "--pr",
                "https://github.com/o/r/pull/5",
                "--reviewer",
                "chatgpt-codex-connector",
                "--format",
                "json",
                "--no-resolve-outdated",
                "--no-resolve-addressed-by-reply",
            ],
        )

        assert fgt.main() == 0

        selection = json.loads(capsys.readouterr().out)["reviewerSelection"]
        assert selection["display_name"] == "Codex"
        assert selection["review_trigger"] == "@codex"
        assert selection["auto_reviews"] is False

    def test_legacy_codex_record_without_a_trigger_is_healed(
        self, tmp_path, monkeypatch, capsys
    ):
        """State written before Codex was known persisted review_trigger: null."""
        self._patch_common(monkeypatch, tmp_path)
        fgt.save_reviewer_selection(
            self.PR,
            {
                "login": "chatgpt-codex-connector",
                "display_name": "Chatgpt Codex Connector",
                "review_trigger": None,
                "source": "confirmed",
                "selected_at": "2026-08-04T11:24:51Z",
            },
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_gemini_threads.py",
                "--pr",
                "https://github.com/o/r/pull/5",
                "--format",
                "json",
                "--no-resolve-outdated",
                "--no-resolve-addressed-by-reply",
            ],
        )

        assert fgt.main() == 0

        selection = json.loads(capsys.readouterr().out)["reviewerSelection"]
        assert selection["review_trigger"] == "@codex"
        assert selection["display_name"] == "Codex"
        assert selection["auto_reviews"] is False

    def test_unknown_reviewer_is_assumed_to_review_on_its_own(
        self, tmp_path, monkeypatch, capsys
    ):
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_gemini_threads.py",
                "--pr",
                "https://github.com/o/r/pull/5",
                "--reviewer",
                "coderabbitai",
                "--format",
                "json",
                "--no-resolve-outdated",
                "--no-resolve-addressed-by-reply",
            ],
        )

        assert fgt.main() == 0

        selection = json.loads(capsys.readouterr().out)["reviewerSelection"]
        assert selection["auto_reviews"] is True

    def test_codex_body_findings_reach_the_fetched_thread_list(
        self, tmp_path, monkeypatch, capsys
    ):
        pull_request = self._pull_request()
        pull_request["reviewThreads"]["nodes"] = []
        pull_request["reviews"]["nodes"] = [
            {
                "id": "R_1",
                "author": {"login": "chatgpt-codex-connector"},
                "submittedAt": "2026-08-04T09:57:39Z",
                "url": "https://github.com/o/r/pull/5#pullrequestreview-1",
                "body": (
                    "### 💡 Codex Review\n\n"
                    "https://github.com/o/r/blob/90a23ce28d/src/enrich.js#L42\n\n"
                    "**![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)"
                    "  Guard the quota refusal path**\n\nIf the provider refuses ...\n"
                ),
            }
        ]
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "resolve_pr", lambda arg: self.PR)
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: pull_request)
        monkeypatch.setattr(fgt, "gh_authenticated_login", lambda: "codex-agent")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_gemini_threads.py",
                "--pr",
                "https://github.com/o/r/pull/5",
                "--reviewer",
                "chatgpt-codex-connector",
                "--format",
                "json",
                "--no-resolve-outdated",
                "--no-resolve-addressed-by-reply",
            ],
        )

        assert fgt.main() == 0

        threads = json.loads(capsys.readouterr().out)["threads"]
        assert [t["path"] for t in threads] == ["src/enrich.js"]
        assert threads[0]["isReviewBodyFinding"] is True

    def test_explicit_reviewer_persists_selection(self, tmp_path, monkeypatch, capsys):
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_gemini_threads.py",
                "--pr",
                "https://github.com/o/r/pull/5",
                "--reviewer",
                "coderabbitai",
                "--reviewer-name",
                "CodeRabbit",
                "--review-trigger-mention",
                "@coderabbitai",
                "--format",
                "json",
                "--no-resolve-outdated",
                "--no-resolve-addressed-by-reply",
            ],
        )

        assert fgt.main() == 0

        capsys.readouterr()
        record = fgt.read_reviewer_selection(self.PR)
        assert record["source"] == "explicit"
        assert record["login"] == "coderabbitai"
        assert record["display_name"] == "CodeRabbit"
        assert record["review_trigger"] == "@coderabbitai"

    def test_confirmed_reviewer_source_persists_selection(
        self, tmp_path, monkeypatch, capsys
    ):
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_gemini_threads.py",
                "--pr",
                "https://github.com/o/r/pull/5",
                "--reviewer",
                "coderabbitai",
                "--reviewer-source",
                "confirmed",
                "--reviewer-name",
                "CodeRabbit",
                "--review-trigger-mention",
                "@coderabbitai",
                "--format",
                "json",
                "--no-resolve-outdated",
                "--no-resolve-addressed-by-reply",
            ],
        )

        assert fgt.main() == 0

        payload = json.loads(capsys.readouterr().out)
        record = fgt.read_reviewer_selection(self.PR)
        assert record["source"] == "confirmed"
        assert payload["reviewerSelection"]["source"] == "confirmed"
        assert payload["reviewerSelection"]["confirmation_required"] is False

    def test_list_reviewers_prints_candidates_without_mutating_state(
        self, tmp_path, monkeypatch, capsys
    ):
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            fgt,
            "fetch_reviewer_discovery",
            lambda pr: {
                "pull_request": self._pull_request(),
                "partial": True,
                "warnings": ["reviewer discovery hit page cap"],
            },
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_gemini_threads.py",
                "--pr",
                "https://github.com/o/r/pull/5",
                "--list-reviewers",
                "--format",
                "json",
            ],
        )

        assert fgt.main() == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["partial"] is True
        assert [candidate["login"] for candidate in payload["reviewers"]] == [
            "chatgpt-codex-connector",
            "coderabbitai",
        ]
        assert fgt.read_reviewer_selection(self.PR) is None


class TestReviewerDiscoveryFetch:
    PR = PullRequest(owner="o", repo="r", number=5)

    @pytest.mark.parametrize("mention", [None, 42, False, {"mention": "@bot"}])
    def test_review_trigger_re_falls_back_for_non_string_mentions(self, mention):
        trigger = fgt._review_trigger_re(mention)

        assert trigger.search("@codex review")

    def test_fetch_reviewer_discovery_paginates_review_threads(self, monkeypatch):
        calls = []

        def page(thread_id, login, *, has_next, end_cursor=None):
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "number": 5,
                            "url": "https://github.com/o/r/pull/5",
                            "reviewThreads": {
                                "pageInfo": {
                                    "hasNextPage": has_next,
                                    "endCursor": end_cursor,
                                },
                                "nodes": [
                                    {
                                        "id": thread_id,
                                        "comments": {
                                            "pageInfo": {
                                                "hasNextPage": False,
                                                "endCursor": None,
                                            },
                                            "nodes": [
                                                {
                                                    "author": {
                                                        "login": login,
                                                        "__typename": "Bot",
                                                    }
                                                }
                                            ],
                                        },
                                    }
                                ],
                            },
                        }
                    }
                }
            }

        responses = iter([
            page("T1", "gemini-code-assist", has_next=True, end_cursor="CURSOR1"),
            page("T2", "coderabbitai", has_next=False),
        ])

        def fake_run_gh(args):
            calls.append(args)
            return next(responses)

        monkeypatch.setattr(fgt, "run_gh", fake_run_gh)

        result = fgt.fetch_reviewer_discovery(self.PR)

        threads = result["pull_request"]["reviewThreads"]["nodes"]
        assert result["partial"] is False
        assert [thread["id"] for thread in threads] == ["T1", "T2"]
        assert any("threadsAfter=CURSOR1" in arg for arg in calls[1])

    def test_fetch_reviewer_discovery_surfaces_graphql_errors(self, monkeypatch):
        monkeypatch.setattr(
            fgt,
            "run_gh",
            lambda args: {"errors": [{"message": "Resource not accessible"}]},
        )

        with pytest.raises(RuntimeError, match="gh GraphQL errors.*Resource not accessible"):
            fgt.fetch_reviewer_discovery(self.PR)

    def test_fetch_reviewer_discovery_rejects_null_repository(self, monkeypatch):
        monkeypatch.setattr(
            fgt,
            "run_gh",
            lambda args: {"data": {"repository": None}},
        )

        with pytest.raises(RuntimeError, match="missing or invalid data"):
            fgt.fetch_reviewer_discovery(self.PR)

    def test_fetch_reviewer_discovery_paginates_thread_comments(self, monkeypatch):
        calls = []

        def fake_run_gh(args):
            calls.append(args)
            if any("threadId=T1" in arg for arg in args):
                return {
                    "data": {
                        "node": {
                            "comments": {
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                                "nodes": [
                                    {
                                        "author": {
                                            "login": "coderabbitai",
                                            "__typename": "Bot",
                                        }
                                    }
                                ],
                            }
                        }
                    }
                }
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "number": 5,
                            "url": "https://github.com/o/r/pull/5",
                            "reviewThreads": {
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "comments": {
                                            "pageInfo": {
                                                "hasNextPage": True,
                                                "endCursor": "COMMENT_CURSOR",
                                            },
                                            "nodes": [
                                                {
                                                    "author": {
                                                        "login": "gemini-code-assist",
                                                        "__typename": "Bot",
                                                    }
                                                }
                                            ],
                                        },
                                    }
                                ],
                            },
                        }
                    }
                }
            }

        monkeypatch.setattr(fgt, "run_gh", fake_run_gh)

        result = fgt.fetch_reviewer_discovery(self.PR)

        comments = result["pull_request"]["reviewThreads"]["nodes"][0]["comments"]["nodes"]
        assert result["partial"] is False
        assert result["warnings"] == []
        assert [comment["author"]["login"] for comment in comments] == [
            "gemini-code-assist",
            "coderabbitai",
        ]
        assert any("commentsAfter=COMMENT_CURSOR" in arg for arg in calls[1])

    def test_fetch_reviewer_discovery_marks_partial_when_comment_page_cap_is_reached(
        self, monkeypatch
    ):
        def fake_run_gh(args):
            if any("threadId=T1" in arg for arg in args):
                return {
                    "data": {
                        "node": {
                            "comments": {
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "COMMENT_CURSOR_2",
                                },
                                "nodes": [],
                            }
                        }
                    }
                }
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "number": 5,
                            "url": "https://github.com/o/r/pull/5",
                            "reviewThreads": {
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "comments": {
                                            "pageInfo": {
                                                "hasNextPage": True,
                                                "endCursor": "COMMENT_CURSOR",
                                            },
                                            "nodes": [],
                                        },
                                    }
                                ],
                            },
                        }
                    }
                }
            }

        monkeypatch.setattr(fgt, "run_gh", fake_run_gh)

        result = fgt.fetch_reviewer_discovery(self.PR, max_pages=1)

        assert result["partial"] is True
        assert result["warnings"] == ["thread T1 comments hit discovery page cap"]


class TestPreferencesFallback:
    """``load_preferences_with_fallback`` must keep working when ``judge`` is missing."""

    # Keys callers may read on the returned dict without guarding against KeyError.
    _EXPECTED_KEYS = {
        "schema_version", "judge_mode", "judge_model",
        "judge_tip_shown", "max_rereview_requests", "set_at",
    }

    def test_falls_back_to_direct_json_when_judge_import_fails(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text(
            '{"max_rereview_requests": 7}', encoding="utf-8"
        )
        # Force the lazy `from judge import load_preferences` to fail.
        monkeypatch.setitem(sys.modules, "judge", None)

        prefs = load_preferences_with_fallback()

        # Loaded value wins; defaults fill the rest so downstream callers can
        # read documented keys like `judge_model` without KeyError handling.
        assert prefs["max_rereview_requests"] == 7
        assert self._EXPECTED_KEYS.issubset(prefs.keys())
        # User must see *why* the canonical loader was skipped.
        assert "judge" in capsys.readouterr().err

    def test_fallback_returns_defaults_when_file_missing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setitem(sys.modules, "judge", None)
        prefs = load_preferences_with_fallback()
        assert self._EXPECTED_KEYS.issubset(prefs.keys())
        assert prefs["judge_mode"] == "off"

    def test_fallback_writes_defaults_on_first_run(
        self, tmp_path, monkeypatch
    ):
        """File must be created on first invocation so users can discover and edit it."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setitem(sys.modules, "judge", None)
        assert not (tmp_path / "preferences.json").exists()

        load_preferences_with_fallback()

        assert (tmp_path / "preferences.json").exists()
        saved = json.loads((tmp_path / "preferences.json").read_text())
        assert saved["judge_mode"] == "off"
        assert saved["max_rereview_requests"] == 3

    def test_fallback_returns_defaults_on_corrupt_json(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text("{ not json", encoding="utf-8")
        monkeypatch.setitem(sys.modules, "judge", None)

        prefs = load_preferences_with_fallback()
        assert self._EXPECTED_KEYS.issubset(prefs.keys())
        assert "could not read" in capsys.readouterr().err

    def test_fallback_returns_defaults_when_not_object(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text("[1, 2, 3]", encoding="utf-8")
        monkeypatch.setitem(sys.modules, "judge", None)

        prefs = load_preferences_with_fallback()
        assert self._EXPECTED_KEYS.issubset(prefs.keys())
        assert "JSON object" in capsys.readouterr().err

    def test_fallback_composes_with_effective_rereview_limit(
        self, tmp_path, monkeypatch
    ):
        """Persistent cap setting takes effect even without the judge module."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text(
            '{"max_rereview_requests": "5"}', encoding="utf-8"
        )
        monkeypatch.setitem(sys.modules, "judge", None)

        prefs = load_preferences_with_fallback()
        assert effective_rereview_limit(None, prefs) == 5


class TestNonnegativeInt:
    def test_valid_value(self):
        assert nonnegative_int("4") == 4

    def test_zero_is_allowed(self):
        assert nonnegative_int("0") == 0

    def test_negative_raises_argparse_error(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError, match=">= 0"):
            nonnegative_int("-1")

    def test_non_integer_raises_argparse_error(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError, match="invalid int value"):
            nonnegative_int("abc")


# ---------------------------------------------------------------------------
# pagination_warnings
# ---------------------------------------------------------------------------

class TestPaginationWarnings:
    def test_under_limit_no_warnings(self):
        pr = {"reviewThreads": {"nodes": [{"comments": {"nodes": []}} for _ in range(5)]},
              "reviews": {"nodes": []}, "comments": {"nodes": []}}
        assert pagination_warnings(pr) == []

    def test_review_threads_at_cap_warns(self):
        pr = {"reviewThreads": {"nodes": [{"id": str(i), "comments": {"nodes": []}}
                                            for i in range(PAGE_LIMIT_REVIEW_THREADS)]},
              "reviews": {"nodes": []}, "comments": {"nodes": []}}
        assert any("reviewThreads hit page limit" in w for w in pagination_warnings(pr))

    def test_per_thread_comments_at_cap_warns(self):
        deep = {"id": "t1", "comments": {"nodes": [bot_comment("x") for _ in range(PAGE_LIMIT_THREAD_COMMENTS)]}}
        pr = {"reviewThreads": {"nodes": [deep]}, "reviews": {"nodes": []}, "comments": {"nodes": []}}
        assert any("comments hit page limit" in w for w in pagination_warnings(pr))


# ---------------------------------------------------------------------------
# thread_fingerprint
# ---------------------------------------------------------------------------

class TestThreadFingerprint:
    def test_stable_across_runs(self):
        threads = [make_thread(comments=[bot_comment("hi")])]
        a = thread_fingerprint([{"path": t["path"], "line": t["line"],
                                 "comments": t["comments"]["nodes"]} for t in threads])
        b = thread_fingerprint([{"path": t["path"], "line": t["line"],
                                 "comments": t["comments"]["nodes"]} for t in threads])
        assert a == b

    def test_changes_when_body_changes(self):
        a = thread_fingerprint([{"path": "p", "line": 1,
                                  "comments": [{"author": {"login": BOT}, "body": "X"}]}])
        b = thread_fingerprint([{"path": "p", "line": 1,
                                  "comments": [{"author": {"login": BOT}, "body": "Y"}]}])
        assert a != b


# ---------------------------------------------------------------------------
# review_activity_fingerprint + after_iso anchor
# ---------------------------------------------------------------------------

def _pr_with_review(submitted_at: str) -> dict:
    """Minimal pull_request fixture with one Gemini review at submitted_at."""
    return {
        "reviewThreads": {"nodes": []},
        "reviews": {"nodes": [
            {"author": {"login": BOT}, "submittedAt": submitted_at, "state": "COMMENTED"},
        ]},
        "comments": {"nodes": []},
    }


def _pr_with_thread_comment(created_at: str) -> dict:
    """Minimal pull_request fixture with one Gemini thread comment at created_at."""
    return {
        "reviewThreads": {"nodes": [
            {
                "isResolved": False,
                "isOutdated": False,
                "path": "src/x.py",
                "line": 1,
                "comments": {"nodes": [
                    {"author": {"login": BOT}, "body": "![medium](x) fix", "createdAt": created_at},
                ]},
            },
        ]},
        "reviews": {"nodes": []},
        "comments": {"nodes": []},
    }


class TestReviewActivityFingerprint:
    # --- no after_iso (existing behaviour) ---

    def test_none_when_no_activity(self):
        pr = {"reviewThreads": {"nodes": []}, "reviews": {"nodes": []}, "comments": {"nodes": []}}
        assert review_activity_fingerprint(pr, BOT) is None

    def test_non_none_when_review_present(self):
        pr = _pr_with_review("2026-06-07T10:00:00Z")
        assert review_activity_fingerprint(pr, BOT) is not None

    def test_different_review_bodies_produce_different_fingerprints(self):
        pr1 = _pr_with_review("2026-06-07T10:00:00Z")
        pr2 = _pr_with_review("2026-06-07T11:00:00Z")
        assert review_activity_fingerprint(pr1, BOT) != review_activity_fingerprint(pr2, BOT)

    # --- after_iso filters out old activity ---

    def test_none_when_review_before_anchor(self):
        pr = _pr_with_review("2026-06-07T09:00:00Z")
        assert review_activity_fingerprint(pr, BOT, after_iso="2026-06-07T10:00:00Z") is None

    def test_none_when_review_exactly_at_anchor(self):
        # Exclusive lower bound: activity AT the anchor timestamp is treated as
        # "not new" (the re-review comment itself has that same timestamp).
        pr = _pr_with_review("2026-06-07T10:00:00Z")
        assert review_activity_fingerprint(pr, BOT, after_iso="2026-06-07T10:00:00Z") is None

    def test_non_none_when_review_after_anchor(self):
        pr = _pr_with_review("2026-06-07T10:00:01Z")
        assert review_activity_fingerprint(pr, BOT, after_iso="2026-06-07T10:00:00Z") is not None

    def test_none_when_thread_comment_before_anchor(self):
        pr = _pr_with_thread_comment("2026-06-07T09:59:00Z")
        assert review_activity_fingerprint(pr, BOT, after_iso="2026-06-07T10:00:00Z") is None

    def test_non_none_when_thread_comment_after_anchor(self):
        pr = _pr_with_thread_comment("2026-06-07T10:00:01Z")
        assert review_activity_fingerprint(pr, BOT, after_iso="2026-06-07T10:00:00Z") is not None

    def test_mixed_only_new_review_counts(self):
        # One old review (should be filtered), one new review (should count).
        pr = {
            "reviewThreads": {"nodes": []},
            "reviews": {"nodes": [
                {"author": {"login": BOT}, "submittedAt": "2026-06-07T09:00:00Z", "state": "COMMENTED"},
                {"author": {"login": BOT}, "submittedAt": "2026-06-07T11:00:00Z", "state": "COMMENTED"},
            ]},
            "comments": {"nodes": []},
        }
        anchor = "2026-06-07T10:00:00Z"
        # Without anchor: sees both reviews
        fp_all = review_activity_fingerprint(pr, BOT)
        # With anchor: only the new review
        _pr_old_only = _pr_with_review("2026-06-07T09:00:00Z")
        pr_new_only = _pr_with_review("2026-06-07T11:00:00Z")
        fp_anchored = review_activity_fingerprint(pr, BOT, after_iso=anchor)
        fp_new_only = review_activity_fingerprint(pr_new_only, BOT, after_iso=anchor)
        assert fp_anchored is not None
        assert fp_anchored == fp_new_only  # anchored result matches "new review only"
        assert fp_anchored != fp_all       # differs from the unanchored result

    def test_other_author_not_counted(self):
        pr = _pr_with_review("2026-06-07T11:00:00Z")
        # A different author than BOT should not match
        assert review_activity_fingerprint(pr, "other-bot", after_iso="2026-06-07T10:00:00Z") is None


# ---------------------------------------------------------------------------
# render_receipt
# ---------------------------------------------------------------------------

class TestRenderReceipt:
    def test_includes_all_metrics(self):
        pr_obj = PullRequest(owner="o", repo="r", number=1, url=None)
        threads = [make_thread(comments=[bot_comment("![high](x) fix me")])]
        receipt = render_receipt(
            pr_obj, _pr_with_threads([]), threads,
            author=BOT, resolved_outdated=2, resolved_addressed_by_reply=1,
            rereview_count=1, rereview_limit=3,
        )
        assert "1 / 3" in receipt
        assert "| 2 |" in receipt  # outdated resolved
        assert "high=1" in receipt
        assert "actionable threads remaining" in receipt

    def test_cap_row_is_labeled_as_re_review_requests(self):
        # The public receipt must name the counter the cap actually bounds.
        # "cycles" invited readers to compare it with the session-cycle
        # ordinal in the header and conclude the two disagreed.
        pr_obj = PullRequest(owner="o", repo="r", number=1, url=None)
        receipt = render_receipt(
            pr_obj, _pr_with_threads([]), [],
            author=BOT, resolved_outdated=0, resolved_addressed_by_reply=0,
            rereview_count=1, rereview_limit=3,
        )
        assert "| re-review requests used (cap) | 1 / 3 |" in receipt
        assert "cycles used" not in receipt


class TestRunGhWithoutBinary:
    def test_missing_gh_surfaces_as_runtime_error_not_traceback(self, monkeypatch):
        def _no_gh(cmd, *args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "gh")

        monkeypatch.setattr(subprocess, "run", _no_gh)
        with pytest.raises(RuntimeError) as excinfo:
            fgt.run_gh(["api", "user"])
        msg = str(excinfo.value)
        assert "GitHub CLI" in msg
        assert "gh auth login" in msg
        assert "does not fall back" in msg


# ---------------------------------------------------------------------------
# filter_by_min_severity
# ---------------------------------------------------------------------------

def _sev_thread(sev: str | None):
    body = (f"![{sev}](https://www.gstatic.com/codereviewagent/{sev}-priority.svg) fix me"
            if sev else "plain comment with no marker")
    return {
        "isResolved": False,
        "isOutdated": False,
        "comments": {"nodes": [{"author": {"login": BOT}, "body": body}]},
    }


class TestFilterByMinSeverity:
    def test_high_drops_medium_and_low(self):
        items = [_sev_thread(s) for s in ("critical", "high", "medium", "low")]
        out = filter_by_min_severity(items, "high")
        assert [thread_severity(t) for t in out] == ["critical", "high"]

    def test_medium_keeps_medium_and_above(self):
        items = [_sev_thread(s) for s in ("critical", "high", "medium", "low")]
        out = filter_by_min_severity(items, "medium")
        assert [thread_severity(t) for t in out] == ["critical", "high", "medium"]

    def test_low_keeps_everything_severity_typed(self):
        items = [_sev_thread(s) for s in ("critical", "high", "medium", "low")]
        out = filter_by_min_severity(items, "low")
        assert len(out) == 4

    def test_critical_keeps_only_critical(self):
        items = [_sev_thread(s) for s in ("critical", "high", "medium", "low")]
        out = filter_by_min_severity(items, "critical")
        assert [thread_severity(t) for t in out] == ["critical"]

    def test_unknown_kept_by_default(self):
        items = [_sev_thread("low"), _sev_thread(None), _sev_thread("critical")]
        out = filter_by_min_severity(items, "high")
        # low dropped; critical kept; unknown kept (default keep_unknown=True)
        assert {thread_severity(t) for t in out} == {"critical", "unknown"}

    def test_keep_unknown_false_drops_unmarked(self):
        items = [_sev_thread("critical"), _sev_thread(None), _sev_thread("low")]
        out = filter_by_min_severity(items, "high", keep_unknown=False)
        assert [thread_severity(t) for t in out] == ["critical"]

    def test_drop_unknown_only(self):
        items = [_sev_thread("critical"), _sev_thread(None), _sev_thread("low")]
        out = filter_by_min_severity(items, None, keep_unknown=False)
        assert [thread_severity(t) for t in out] == ["critical", "low"]

    def test_drop_unknown_only_with_none_min_severity(self):
        # --drop-unknown-severity used alone: keep everything tagged, drop unknown.
        items = [_sev_thread("critical"), _sev_thread(None), _sev_thread("low")]
        out = filter_by_min_severity(items, None, keep_unknown=False)
        assert [thread_severity(t) for t in out] == ["critical", "low"]

    def test_none_min_severity_keeps_all_by_default(self):
        # min_severity=None with keep_unknown=True is a pure no-op.
        items = [_sev_thread(s) for s in ("critical", "high", "medium", "low", None)]
        out = filter_by_min_severity(items, None)
        assert len(out) == 5

    def test_preserves_input_order(self):
        # Stable: don't sort, don't shuffle. main() sorts separately.
        items = [_sev_thread("low"), _sev_thread("critical"), _sev_thread("high")]
        out = filter_by_min_severity(items, "high")
        assert [thread_severity(t) for t in out] == ["critical", "high"]

    def test_empty_input(self):
        assert filter_by_min_severity([], "high") == []


# ---------------------------------------------------------------------------
# Sticky receipt — state file + render variants
# ---------------------------------------------------------------------------

class TestStickyReceiptState:
    def test_state_path_honors_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert sticky_state_path() == tmp_path / "state.json"

    def test_load_returns_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert load_sticky_state() == {}

    def test_load_returns_empty_when_corrupt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "state.json").write_text("{ not json")
        assert load_sticky_state() == {}

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#1": {"comment_id": 42}})
        assert load_sticky_state() == {"o/r#1": {"comment_id": 42}}

    def test_load_returns_empty_when_valid_json_but_not_dict(
        self, tmp_path, monkeypatch
    ):
        # A valid-but-non-dict payload (list/scalar) from corruption or
        # hand-editing must not reach callers that do .values()/.items().
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "state.json").write_text("[1, 2, 3]")
        assert load_sticky_state() == {}

    def test_save_creates_parent_dir(self, tmp_path, monkeypatch):
        nested = tmp_path / "nested" / "dir"
        monkeypatch.setenv("GGRL_STATE_DIR", str(nested))
        save_sticky_state({"o/r#1": {"comment_id": 1}})
        assert (nested / "state.json").exists()


class TestStickyReceiptRender:
    @staticmethod
    def _empty_pr_payload():
        return {"reviewThreads": {"nodes": []}}

    def test_status_appears_in_header(self):
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        body = render_receipt(
            pr, self._empty_pr_payload(), [],
            author=BOT, resolved_outdated=0, resolved_addressed_by_reply=0,
            rereview_count=0, rereview_limit=3, status="RUNNING",
        )
        assert "### gh-review-loop receipt — RUNNING" in body

    def test_sticky_embeds_marker(self):
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        body = render_receipt(
            pr, self._empty_pr_payload(), [],
            author=BOT, resolved_outdated=0, resolved_addressed_by_reply=0,
            rereview_count=0, rereview_limit=3, status="RUNNING", sticky=True,
        )
        assert STICKY_RECEIPT_MARKER in body
        assert "Last updated:" in body
        assert "edits in place" in body

    def test_non_sticky_does_not_embed_marker(self):
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        body = render_receipt(
            pr, self._empty_pr_payload(), [],
            author=BOT, resolved_outdated=0, resolved_addressed_by_reply=0,
            rereview_count=0, rereview_limit=3,
        )
        assert STICKY_RECEIPT_MARKER not in body
        assert "Generated by" in body

    def test_status_none_omits_header_suffix(self):
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        body = render_receipt(
            pr, self._empty_pr_payload(), [],
            author=BOT, resolved_outdated=0, resolved_addressed_by_reply=0,
            rereview_count=0, rereview_limit=3, status=None,
        )
        # Header line ends after "receipt" with no " — " suffix
        assert body.splitlines()[0] == "### gh-review-loop receipt"


def _thread(path, body, line=1):
    return {"path": path, "line": line, "comments": [{"body": body, "url": f"https://gh/{path}"}]}


class TestFindingFingerprint:
    def test_same_content_same_fingerprint(self):
        a = _thread("a.py", "![high](x) Fix the off-by-one")
        b = _thread("a.py", "![high](x) Fix the off-by-one")
        assert finding_fingerprint(a) == finding_fingerprint(b)

    def test_severity_image_ignored(self):
        # Same suggestion re-posted with a different severity image still matches.
        a = _thread("a.py", "![high](sev-high.svg) Fix the off-by-one")
        b = _thread("a.py", "![medium](sev-medium.svg) Fix the off-by-one")
        assert finding_fingerprint(a) == finding_fingerprint(b)

    def test_different_path_differs(self):
        a = _thread("a.py", "![high](x) Fix it")
        b = _thread("b.py", "![high](x) Fix it")
        assert finding_fingerprint(a) != finding_fingerprint(b)

    def test_different_body_differs(self):
        a = _thread("a.py", "![high](x) Fix the parser")
        b = _thread("a.py", "![high](x) Fix the renderer")
        assert finding_fingerprint(a) != finding_fingerprint(b)


class TestCarriedOverTracking:
    def _pr(self):
        return PullRequest(owner="o", repo="r", number=1)

    def test_cycle_1_has_empty_prior(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        result = track_finding_fingerprints(pr, {"fp1", "fp2"})
        assert result["prior"] == set()
        assert result["new"] == {"fp1", "fp2"}
        assert prior_finding_fingerprints(pr) == set()

    def test_cycle_2_carries_prior_and_flags_new(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        track_finding_fingerprints(pr, {"fp1", "fp2"})       # cycle 1
        result = track_finding_fingerprints(pr, {"fp2", "fp3"})  # cycle 2: fp2 carried, fp3 new
        assert result["prior"] == {"fp1", "fp2"}
        assert result["new"] == {"fp3"}
        # The read-only receipt path sees cycle-1's union as "prior".
        assert prior_finding_fingerprints(pr) == {"fp1", "fp2"}


class TestWaitCycleOneNoSettle:
    def _pr(self):
        return PullRequest(owner="o", repo="r", number=1)

    def test_cycle1_returns_on_first_detection_without_settle(self, monkeypatch):
        # after_iso=None (cycle 1): activity present → return immediately, no sleep.
        pull = _pr_with_review("2026-06-07T10:00:00Z")
        monkeypatch.setattr("fetch_gemini_threads.fetch_threads", lambda pr: pull)

        def _no_sleep(_):
            raise AssertionError("cycle 1 must not settle/sleep")

        monkeypatch.setattr("fetch_gemini_threads.time.sleep", _no_sleep)
        out = wait_for_stable_review(
            self._pr(), author=BOT, timeout_seconds=10, interval_seconds=1,
            quiet_seconds=45, after_iso=None,
        )
        assert out is pull

    def test_cycle2_waits_for_settle(self, monkeypatch):
        # after_iso set (cycle 2+): first detection must NOT return; it sleeps to settle.
        pull = _pr_with_review("2026-06-07T10:00:05Z")
        monkeypatch.setattr("fetch_gemini_threads.fetch_threads", lambda pr: pull)
        slept = {"n": 0}

        def _count_sleep(_):
            slept["n"] += 1
            raise RuntimeError("stop after first sleep")  # break the loop deterministically

        monkeypatch.setattr("fetch_gemini_threads.time.sleep", _count_sleep)
        with pytest.raises(RuntimeError, match="stop after first sleep"):
            wait_for_stable_review(
                self._pr(), author=BOT, timeout_seconds=10, interval_seconds=1,
                quiet_seconds=45, after_iso="2026-06-07T10:00:00Z",
            )
        assert slept["n"] == 1  # it sleeps rather than returning on first detection


class TestRunTracking:
    def _pr(self):
        return PullRequest(owner="o", repo="r", number=1)

    def test_first_update_sets_started_at_and_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(self._pr(), [("t1", "a.py"), ("t2", "b.py")])
        run = read_run_tracking(self._pr())
        assert "started_at" in run
        assert run["finding_ids"] == ["t1", "t2"]
        assert run["finding_paths"] == ["a.py", "b.py"]

    def test_second_update_unions_and_preserves_started_at(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        first_started = read_run_tracking(pr)["started_at"]
        update_run_tracking(pr, [("t1", "a.py"), ("t2", "b.py")])
        run = read_run_tracking(pr)
        assert run["started_at"] == first_started
        assert run["finding_ids"] == ["t1", "t2"]

    def test_first_fetch_starts_session_cycle_one(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(self._pr(), [("t1", "a.py")])
        assert read_run_tracking(self._pr())["session_cycle"] == 1

    def test_refetch_without_summary_stays_in_same_cycle(self, tmp_path, monkeypatch):
        # A recovery re-fetch mid-cycle must not advance the ordinal (#104).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        update_run_tracking(pr, [("t1", "a.py"), ("t2", "b.py")])
        assert read_run_tracking(pr)["session_cycle"] == 1

    def _post_rereview(self, pr):
        """Mimic request_rereview.py's local cycle-transition stamp."""
        from fetch_gemini_threads import load_sticky_state, save_sticky_state, _state_key
        state = load_sticky_state()
        run = state.setdefault(_state_key(pr), {}).setdefault("run", {})
        run["rereviews_posted"] = run.get("rereviews_posted", 0) + 1
        save_sticky_state(state)

    def test_fetch_after_summary_and_rereview_starts_next_cycle(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        stamp_summary_emitted(pr)
        self._post_rereview(pr)
        update_run_tracking(pr, [("t2", "b.py")])
        assert read_run_tracking(pr)["session_cycle"] == 2

    def test_recovery_fetch_after_summary_before_rereview_stays_in_cycle(
        self, tmp_path, monkeypatch
    ):
        # A session interrupted after --cycle-summary but before its push /
        # re-review resumes with a --full recovery fetch: that fetch is still
        # finishing the SAME cycle, so the ordinal must hold (#111 review).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        stamp_summary_emitted(pr)
        update_run_tracking(pr, [("t1", "a.py")])
        assert read_run_tracking(pr)["session_cycle"] == 1
        self._post_rereview(pr)
        update_run_tracking(pr, [("t2", "b.py")])
        assert read_run_tracking(pr)["session_cycle"] == 2

    def test_second_rereview_without_a_new_summary_does_not_advance_twice(
        self, tmp_path, monkeypatch
    ):
        # `last_summary_seq > 0` was permanently true once any summary had
        # been emitted, so a second re-review arriving before the new cycle
        # wrote its own summary advanced the ordinal off a stale one (#111
        # review). The summary must be consumed when it advances a cycle.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        stamp_summary_emitted(pr)
        self._post_rereview(pr)
        update_run_tracking(pr, [("t2", "b.py")])
        assert read_run_tracking(pr)["session_cycle"] == 2
        # Cycle 2 has NOT emitted its own summary yet.
        self._post_rereview(pr)
        update_run_tracking(pr, [("t3", "c.py")])
        assert read_run_tracking(pr)["session_cycle"] == 2
        # Once cycle 2 summarizes and transitions, cycle 3 begins.
        stamp_summary_emitted(pr)
        self._post_rereview(pr)
        update_run_tracking(pr, [("t4", "d.py")])
        assert read_run_tracking(pr)["session_cycle"] == 3

    def test_preexisting_pr_history_cannot_advance_the_ordinal(
        self, tmp_path, monkeypatch
    ):
        # Evidence is local, so a PR carrying many historical @codex pings
        # cannot advance a fresh run's ordinal (#111 review).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        stamp_summary_emitted(pr)
        update_run_tracking(pr, [("t1", "a.py")])
        assert read_run_tracking(pr)["session_cycle"] == 1

    def test_summary_reruns_do_not_advance_session_cycle(self, tmp_path, monkeypatch):
        # --cycle-summary / --record-run pass bump_session_cycle=False: a
        # read-only summary re-run after the stamp must not start a new cycle.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        stamp_summary_emitted(pr)
        update_run_tracking(pr, [("t1", "a.py")], bump_session_cycle=False)
        update_run_tracking(pr, [("t1", "a.py")], bump_session_cycle=False)
        assert read_run_tracking(pr)["session_cycle"] == 1

    def test_full_three_cycle_sequence(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        for expected in (1, 2, 3):
            update_run_tracking(pr, [("t", "a.py")])
            assert read_run_tracking(pr)["session_cycle"] == expected
            # cycle-summary invocation: tracking update without a bump, then stamp
            update_run_tracking(pr, [("t", "a.py")], bump_session_cycle=False)
            stamp_summary_emitted(pr)
            self._post_rereview(pr)  # the cycle pushes and requests a re-review

    def test_clear_removes_run_but_keeps_other_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        from fetch_gemini_threads import load_sticky_state, save_sticky_state, _state_key
        save_sticky_state({_state_key(pr): {"commentId": 42}})
        update_run_tracking(pr, [("t1", "a.py")])
        clear_run_tracking(pr)
        assert read_run_tracking(pr) == {}
        assert load_sticky_state()[_state_key(pr)]["commentId"] == 42

    def test_changed_files_in_range_reads_git_diff(self, tmp_path):
        import subprocess
        from fetch_gemini_threads import changed_files_in_range
        repo = tmp_path / "repo"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        def git(*args):
            subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                           capture_output=True)
        git("init", "-q")
        (repo / "a.py").write_text("x = 1\n")
        git("add", "a.py")
        git("commit", "-qm", "base")
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=env,
                              capture_output=True, text=True).stdout.strip()
        (repo / "a.py").write_text("x = 2\n")
        (repo / "b.py").write_text("y = 1\n")
        git("add", "a.py", "b.py")
        git("commit", "-qm", "fix")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=env,
                              capture_output=True, text=True).stdout.strip()
        changed = changed_files_in_range(base, head, cwd=str(repo))
        assert changed == {"a.py", "b.py"}

    def test_changed_files_in_range_guarded_on_error(self, tmp_path):
        from fetch_gemini_threads import changed_files_in_range
        # Non-existent revs / not a repo must not raise — best-effort empty set.
        assert changed_files_in_range("nope", "nope2", cwd=str(tmp_path)) == set()

    def test_changed_files_in_range_guarded_when_git_raises(self, monkeypatch):
        import fetch_gemini_threads as fgt

        def _raise(*args, **kwargs):
            raise OSError("git missing")

        monkeypatch.setattr(fgt.subprocess, "run", _raise)
        assert fgt.changed_files_in_range("base", "head") == set()

    def test_changed_files_in_range_empty_when_no_refs(self):
        from fetch_gemini_threads import changed_files_in_range
        assert changed_files_in_range(None, None) == set()

    def test_accumulate_fixed_markers_persists_and_unions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        from fetch_gemini_threads import accumulate_fixed_markers, read_fixed_markers
        pr = self._pr()
        accumulate_fixed_markers(pr, fingerprints=["fp1"], paths=["a.py"])
        accumulate_fixed_markers(pr, fingerprints=["fp2"], paths=["b.py"])
        markers = read_fixed_markers(pr)
        assert markers["fingerprints"] == {"fp1", "fp2"}
        assert markers["paths"] == {"a.py", "b.py"}

    def test_read_fixed_markers_empty_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        from fetch_gemini_threads import read_fixed_markers
        markers = read_fixed_markers(self._pr())
        assert markers == {"fingerprints": set(), "paths": set()}

    def test_accumulate_fixed_markers_survives_alongside_run_tracking(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        from fetch_gemini_threads import accumulate_fixed_markers, read_fixed_markers
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        accumulate_fixed_markers(pr, fingerprints=["fp1"], paths=[])
        assert read_run_tracking(pr)["finding_ids"] == ["t1"]
        assert read_fixed_markers(pr)["fingerprints"] == {"fp1"}

    def test_record_cycle_appends_timed_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        cycle = record_cycle(
            pr,
            started_at="2026-06-05T09:12:10Z",
            finished_at="2026-06-05T09:20:24Z",
            finding_count=3,
            outcome="continued",
        )
        assert cycle["duration_seconds"] == 494   # 8m14s of active work
        run = read_run_tracking(pr)
        assert run["cycles"] == [cycle]
        assert run["started_at"]  # cycle recording preserves run start

    def test_record_cycle_accumulates_across_cycles(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        record_cycle(pr, started_at="2026-06-05T09:00:00Z",
                     finished_at="2026-06-05T09:02:00Z", finding_count=2, outcome="continued")
        record_cycle(pr, started_at="2026-06-05T09:10:00Z",
                     finished_at="2026-06-05T09:13:00Z", finding_count=1, outcome="clean")
        cycles = read_run_tracking(pr)["cycles"]
        assert [c["duration_seconds"] for c in cycles] == [120, 180]
        assert [c["outcome"] for c in cycles] == ["continued", "clean"]

    def test_record_cycle_tolerates_corrupt_state(self, tmp_path, monkeypatch):
        # A corrupt/hand-edited state (run is a string, or cycles is an int)
        # must not crash record_cycle's dict()/list() casts.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        from fetch_gemini_threads import save_sticky_state, _state_key
        save_sticky_state({_state_key(pr): {"run": "corrupt-not-a-dict"}})
        cycle = record_cycle(pr, started_at="2026-06-05T09:00:00Z",
                             finished_at="2026-06-05T09:01:00Z", finding_count=1, outcome="clean")
        assert cycle["duration_seconds"] == 60
        assert read_run_tracking(pr)["cycles"] == [cycle]  # reset cleanly, appended

    def test_record_cycle_tolerates_non_dict_state(self, tmp_path, monkeypatch):
        # load_sticky_state returning a non-dict (corrupt state.json) must not
        # AttributeError on state.get(key).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt
        monkeypatch.setattr(fgt, "load_sticky_state", lambda: ["not", "a", "dict"])
        captured = {}
        monkeypatch.setattr(fgt, "save_sticky_state", lambda s: captured.update(state=s))
        cycle = fgt.record_cycle(self._pr(), started_at="2026-06-05T09:00:00Z",
                                 finished_at="2026-06-05T09:01:00Z",
                                 finding_count=1, outcome="clean")
        assert cycle["duration_seconds"] == 60
        assert isinstance(captured["state"], dict)  # rebuilt clean, not crashed

    def test_record_cycle_cli_warns_and_exits_zero_on_io_error(
        self, tmp_path, monkeypatch, capsys
    ):
        # A failed metrics write must not break the loop: warn, exit 0.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(fgt, "save_sticky_state", _boom)
        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py",
            "--pr", "https://github.com/o/r/pull/9",
            "--record-cycle", "--cycle-started-at", "2026-06-05T09:00:00Z",
            "--finding-count", "2", "--cycle-outcome", "continued",
        ])
        rc = fgt.main()
        assert rc == 0
        assert "could not record cycle timing" in capsys.readouterr().err


class TestRemainingFindingStateAdapter:
    def _thread(self, thread_id="t1", path="a.py", body="Use the helper here."):
        return {
            "id": thread_id,
            "path": path,
            "line": 10,
            "comments": [{"author": {"login": BOT}, "body": body}],
        }

    def test_fixed_marker_and_changed_path_counts_as_likely_fixed(self):
        thread = self._thread()
        fp = finding_fingerprint(thread)
        states = classify_remaining_finding_states(
            [thread],
            fixed_fingerprints={fp},
            fixed_paths=set(),
            changed_paths={"a.py"},
            prior_fingerprints=set(),
            judge_results={},
            cap_reached=True,
        )
        assert states == {"t1": "fixed_pending_confirmation"}
        assert count_likely_fixed_remaining(states) == 1

    def test_needs_human_is_not_likely_fixed(self):
        thread = self._thread()
        fp = finding_fingerprint(thread)
        states = classify_remaining_finding_states(
            [thread],
            fixed_fingerprints={fp},
            fixed_paths={"a.py"},
            changed_paths={"a.py"},
            prior_fingerprints=set(),
            judge_results={"t1": {"verdict": "needs_human"}},
            cap_reached=True,
        )
        assert states == {"t1": "needs_human"}
        assert count_likely_fixed_remaining(states) == 0


class TestJudgeAccumulation:
    def _pr(self):
        return PullRequest(owner="o", repo="r", number=1)

    def test_accumulate_persists_verdicts_and_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        accumulate_judge_results(pr, {"t1": {"verdict": "valid_actionable"}})
        run = read_run_tracking(pr)
        assert run["judge_ran"] is True
        assert run["judge_results"] == {"t1": {"verdict": "valid_actionable"}}

    def test_accumulate_unions_and_later_cycle_supersedes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        accumulate_judge_results(pr, {"t1": {"verdict": "needs_human"},
                                      "t2": {"verdict": "valid_actionable"}})
        # A later cycle re-judges t1 -> the newer verdict wins; t2 preserved.
        accumulate_judge_results(pr, {"t1": {"verdict": "valid_actionable"}})
        run = read_run_tracking(pr)
        assert run["judge_results"]["t1"] == {"verdict": "valid_actionable"}
        assert run["judge_results"]["t2"] == {"verdict": "valid_actionable"}

    def test_accumulate_empty_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        accumulate_judge_results(pr, {})
        assert read_run_tracking(pr) == {}  # no run entry created

    def test_accumulate_preserves_findings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        started = read_run_tracking(pr)["started_at"]
        accumulate_judge_results(pr, {"t1": {"verdict": "valid_actionable"}})
        run = read_run_tracking(pr)
        assert run["finding_ids"] == ["t1"]       # findings preserved
        assert run["started_at"] == started        # started_at preserved
        assert run["judge_results"]["t1"]["verdict"] == "valid_actionable"

    def test_clear_removes_accumulated_judge_results(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        accumulate_judge_results(pr, {"t1": {"verdict": "valid_actionable"}})
        clear_run_tracking(pr)
        assert read_run_tracking(pr) == {}

    def test_merge_current_supersedes_accumulated(self):
        accumulated = {"t1": {"verdict": "needs_human"},
                       "t2": {"verdict": "valid_actionable"}}
        current = {"t1": {"verdict": "valid_actionable"}}
        merged = merge_judge_results(accumulated, current)
        assert merged["t1"] == {"verdict": "valid_actionable"}  # current wins
        assert merged["t2"] == {"verdict": "valid_actionable"}  # accumulated kept

    def test_merge_handles_none_and_empty(self):
        assert merge_judge_results(None, None) == {}
        assert merge_judge_results({"t1": {"verdict": "valid_actionable"}}, None) == {
            "t1": {"verdict": "valid_actionable"}
        }
        assert merge_judge_results(None, {"t2": {"verdict": "duplicate"}}) == {
            "t2": {"verdict": "duplicate"}
        }


def _sticky_post_unavailable(fgt, monkeypatch):
    """Make the receipt-comment write fail, exercising the stdout fallback
    channel (#86) — the hermetic suite has no GitHub to post to, and the
    fallback must carry the identical full receipt so nothing is ever lost."""

    def _fail(pr, body, dry_run=False):
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(fgt, "post_or_update_sticky_receipt", _fail)


class TestRecordRunIntegration:
    """Drive main() --record-run with the GitHub seams stubbed, proving the
    eval accumulated across cycles lands in the record at terminal 'clean'.

    No real network/gh calls — every GitHub-touching function is monkeypatched.
    """

    def _stub_record_run_fetch(self, fgt, monkeypatch, pr, threads, rereviews):
        monkeypatch.setattr(fgt, "resolve_pr", lambda spec: pr)
        monkeypatch.setattr(fgt, "fetch_threads", lambda p: {"stub": True})
        monkeypatch.setattr(fgt, "filter_threads", lambda *a, **k: threads)
        monkeypatch.setattr(fgt, "sort_by_severity", lambda fetched: fetched)
        monkeypatch.setattr(fgt, "rereview_requests", lambda *a, **k: rereviews)
        monkeypatch.setattr(fgt, "addressed_by_reply_threads", lambda *a, **k: [])
        monkeypatch.setattr(fgt, "pagination_warnings", lambda pull_request: [])
        _sticky_post_unavailable(fgt, monkeypatch)

    def test_clean_record_run_reports_accumulated_eval(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt
        from metrics import runs_log_path

        pr = PullRequest(owner="o", repo="r", number=7)

        # Cycle 1: fetched two findings and judged them; verdicts persisted.
        update_run_tracking(pr, [("t1", "a.py"), ("t2", "b.py")])
        accumulate_judge_results(pr, {
            "t1": {"verdict": "valid_actionable", "recommended_action": "fix"},
            "t2": {"verdict": "false_positive", "recommended_action": "ignore"},
        })

        # Terminal pass: the PR is now clean (both findings resolved). Stub the
        # GitHub seams so filter_threads yields zero actionable threads.
        self._stub_record_run_fetch(fgt, monkeypatch, pr, [], ["c1"])

        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--record-run",
            "--judge-mode", "off",  # no OpenAI: the eval comes from accumulation
            "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "2", "--verification", "passed", "--outcome", "clean",
        ])

        rc = fgt.main()
        assert rc == 0

        out = capsys.readouterr().out
        assert "Ignored by judge: 1" in out      # false_positive counts as ignored
        # needs_human == 0 -> the judge needs-human line is omitted (receipt, not dashboard)
        assert "Needs human by judge" not in out

        record = json.loads(runs_log_path().read_text().strip().splitlines()[-1])
        assert record["judge"]["enabled"] is True
        assert record["judge"]["verdicts"]["valid_actionable"] == 1
        assert record["judge"]["verdicts"]["false_positive"] == 1
        assert record["findings_fetched"] == 2
        assert record["observed_fixed_count"] == 2     # both resolved this run
        # run state is cleared at record end -> no leakage into the next run
        assert read_run_tracking(pr) == {}

    def test_record_run_derives_fixed_pending_from_marker_flags(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt
        from metrics import runs_log_path

        pr = PullRequest(owner="o", repo="r", number=7)
        thread = {
            "id": "t1",
            "path": "a.py",
            "line": 10,
            "comments": [{"author": {"login": BOT}, "body": "Use the helper here."}],
        }
        fp = finding_fingerprint(thread)
        self._stub_record_run_fetch(fgt, monkeypatch, pr, [thread], ["c1", "c2", "c3"])

        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--record-run",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "1", "--verification", "passed",
            "--fixed-finding", fp, "--fixed-path", "a.py",
            "--gemini-unconfirmed",
        ])

        rc = fgt.main()
        assert rc == 0
        assert "Outcome: fixed_pending_confirmation" in capsys.readouterr().out
        record = json.loads(runs_log_path().read_text().strip().splitlines()[-1])
        assert record["outcome"] == "fixed_pending_confirmation"
        assert record["remaining_actionable"] == 1

    def test_record_run_outcome_override_remains_authoritative(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt
        from metrics import runs_log_path

        pr = PullRequest(owner="o", repo="r", number=7)
        thread = {
            "id": "t1",
            "path": "a.py",
            "line": 10,
            "comments": [{"author": {"login": BOT}, "body": "Use the helper here."}],
        }
        fp = finding_fingerprint(thread)
        self._stub_record_run_fetch(fgt, monkeypatch, pr, [thread], ["c1", "c2", "c3"])

        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--record-run",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "1", "--verification", "passed",
            "--fixed-finding", fp, "--fixed-path", "a.py",
            "--gemini-unconfirmed", "--outcome", "human",
            "--outcome-reason", "explicit override",
        ])

        rc = fgt.main()
        assert rc == 0
        assert "Outcome: human" in capsys.readouterr().out
        record = json.loads(runs_log_path().read_text().strip().splitlines()[-1])
        assert record["outcome"] == "human"
        assert record["outcome_reason"] == "explicit override"


class TestCycleSummary:
    """Drive main() --cycle-summary with the GitHub seams stubbed: it must print
    the [loop] Summary block from accumulated state WITHOUT writing a record or
    clearing the accumulator, so it is safe to call every cycle."""

    def test_cycle_summary_prints_block_without_recording_or_clearing(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt
        from metrics import runs_log_path

        pr = PullRequest(owner="o", repo="r", number=7)

        # A prior cycle fetched two findings and judged them.
        update_run_tracking(pr, [("t1", "a.py"), ("t2", "b.py")])
        accumulate_judge_results(pr, {
            "t1": {"verdict": "valid_actionable", "recommended_action": "fix"},
            "t2": {"verdict": "false_positive", "recommended_action": "ignore"},
        })

        # This cycle: t2 still actionable. Stub the GitHub seams.
        thread = {"id": "t2", "path": "b.py"}
        monkeypatch.setattr(fgt, "resolve_pr", lambda spec: pr)
        monkeypatch.setattr(fgt, "fetch_threads", lambda p: {"stub": True})
        monkeypatch.setattr(fgt, "filter_threads", lambda *a, **k: [thread])
        monkeypatch.setattr(fgt, "sort_by_severity", lambda threads: threads)
        monkeypatch.setattr(fgt, "rereview_requests", lambda *a, **k: ["c1"])
        monkeypatch.setattr(fgt, "addressed_by_reply_threads", lambda *a, **k: [])
        monkeypatch.setattr(fgt, "pagination_warnings", lambda pull_request: [])
        _sticky_post_unavailable(fgt, monkeypatch)

        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--cycle-summary",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "1", "--verification", "passed",
        ])

        rc = fgt.main()
        assert rc == 0

        out = capsys.readouterr().out
        assert "[loop] Cycle receipt" in out      # mid-loop header, not terminal [loop] Summary
        assert "Findings fetched: 2" in out      # t1 + t2 accumulated
        assert "Fixed locally: 1" in out
        assert "Ignored by judge: 1" in out      # t2 false_positive, from accumulation

        # Read-only: no record written, accumulator NOT cleared.
        assert not runs_log_path().exists()
        run = read_run_tracking(pr)
        assert set(run.get("finding_ids", [])) == {"t1", "t2"}
        assert run.get("judge_results")  # verdicts still present for next cycle

    def test_cycle_summary_renders_semantic_risk_block(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt

        pr = PullRequest(owner="o", repo="r", number=7)
        update_run_tracking(pr, [("t1", "a.py")])
        thread = {"id": "t1", "path": "a.py"}
        monkeypatch.setattr(fgt, "resolve_pr", lambda spec: pr)
        monkeypatch.setattr(fgt, "fetch_threads", lambda p: {"stub": True})
        monkeypatch.setattr(fgt, "filter_threads", lambda *a, **k: [thread])
        monkeypatch.setattr(fgt, "sort_by_severity", lambda threads: threads)
        monkeypatch.setattr(fgt, "rereview_requests", lambda *a, **k: ["c1"])
        monkeypatch.setattr(fgt, "addressed_by_reply_threads", lambda *a, **k: [])
        monkeypatch.setattr(fgt, "pagination_warnings", lambda pull_request: [])
        _sticky_post_unavailable(fgt, monkeypatch)

        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--cycle-summary",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "1", "--verification", "passed",
            "--semantic-risk", "get_user() now returns one row instead of a list",
        ])

        rc = fgt.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert "[loop] Semantic risk note (manual / heuristic)" in out
        assert "- get_user() now returns one row instead of a list" in out

    def test_cycle_summary_renders_patterns_and_convergence_blocks(
        self, tmp_path, monkeypatch, capsys
    ):
        """Wiring guard: two findings of the same KIND on different sites must
        cluster (count>=2) and render the Patterns/Convergence blocks on stdout.
        Unit tests cover the formatters; this proves the prints aren't gated out."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt

        pr = PullRequest(owner="o", repo="r", number=7)
        update_run_tracking(pr, [("t1", "a.py"), ("t2", "b.py")])

        # Same KIND of finding (dict-before-.get guard) on two different paths,
        # differing only by an inline-code identifier that cluster() strips —
        # so both normalize to one signature and form a multi-site cluster.
        threads = [
            {
                "id": "t1",
                "path": "a.py",
                "line": 10,
                "comments": [{"body": "Validate that `source` is a dict before `.get`"}],
            },
            {
                "id": "t2",
                "path": "b.py",
                "line": 20,
                "comments": [{"body": "Validate that `provenance` is a dict before `.get`"}],
            },
            {
                "id": "t3",
                "path": "c.py",
                "line": 30,
                "comments": [{"body": "Add a docstring to this helper function"}],
            },
        ]
        monkeypatch.setattr(fgt, "resolve_pr", lambda spec: pr)
        monkeypatch.setattr(fgt, "fetch_threads", lambda p: {"stub": True})
        monkeypatch.setattr(fgt, "filter_threads", lambda *a, **k: threads)
        monkeypatch.setattr(fgt, "sort_by_severity", lambda ts: ts)
        monkeypatch.setattr(fgt, "rereview_requests", lambda *a, **k: ["c1"])
        monkeypatch.setattr(fgt, "addressed_by_reply_threads", lambda *a, **k: [])
        monkeypatch.setattr(fgt, "pagination_warnings", lambda pull_request: [])
        _sticky_post_unavailable(fgt, monkeypatch)

        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--cycle-summary",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "1", "--verification", "passed",
            "--no-color",
        ])

        rc = fgt.main()
        assert rc == 0

        out = capsys.readouterr().out
        assert "Patterns (" in out
        assert "Convergence:" in out
        # The clustered dict-guard pattern appears as a multi-site cluster.
        assert "2 sites" in out


class TestSingleChannelReceiptDelivery:
    """#86: the full receipt goes to the sticky PR comment exactly once; chat
    (stdout) carries one [loop] pointer line with counts + verification + the
    receipt URL."""

    def _stub(self, fgt, monkeypatch, pr, threads, posted, rereviews=None):
        monkeypatch.setattr(fgt, "resolve_pr", lambda spec: pr)
        monkeypatch.setattr(fgt, "fetch_threads", lambda p: {"stub": True})
        monkeypatch.setattr(fgt, "filter_threads", lambda *a, **k: threads)
        monkeypatch.setattr(fgt, "sort_by_severity", lambda ts: ts)
        monkeypatch.setattr(fgt, "rereview_requests", lambda *a, **k: rereviews or ["c1"])
        monkeypatch.setattr(fgt, "addressed_by_reply_threads", lambda *a, **k: [])
        monkeypatch.setattr(fgt, "pagination_warnings", lambda pull_request: [])

        def _post(pr_arg, body, dry_run=False):
            posted.append(body)
            return 99

        monkeypatch.setattr(fgt, "post_or_update_sticky_receipt", _post)

    def test_cycle_ordinal_is_independent_of_agent_filtering(
        self, tmp_path, monkeypatch, capsys
    ):
        # The ordinal must advance the same way under `--no-agent-filter`.
        # An earlier design derived it from the PR's re-review comments, where
        # the filtering mode changed the answer and pinned narration at
        # session cycle 1 while the displayed cap climbed (#111 review).
        # Evidence is now the local stamp, so filtering cannot affect it.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt
        import request_rereview

        pr = PullRequest(owner="o", repo="r", number=7)
        posted: list = []
        argv = [
            "fetch_gemini_threads.py", "--judge-mode", "off",
            "--no-agent-filter", "--no-resolve-outdated",
            "--no-resolve-addressed-by-reply", "--no-color",
        ]

        self._stub(fgt, monkeypatch, pr, [], posted, rereviews=["c1"])
        monkeypatch.setattr(sys, "argv", argv)
        assert fgt.main() == 0
        assert read_run_tracking(pr)["session_cycle"] == 1

        # The cycle summarizes, pushes, and requests a re-review.
        stamp_summary_emitted(pr)
        request_rereview.record_rereview_posted("o/r", 7)

        self._stub(fgt, monkeypatch, pr, [], posted, rereviews=["c1", "c2"])
        monkeypatch.setattr(sys, "argv", argv)
        assert fgt.main() == 0
        assert read_run_tracking(pr)["session_cycle"] == 2

    def test_cycle_summary_posts_full_receipt_and_prints_pointer(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt

        pr = PullRequest(owner="o", repo="r", number=7)
        update_run_tracking(pr, [("t1", "a.py"), ("t2", "b.py")])
        thread = {
            "id": "t2", "path": "b.py", "line": 4,
            "comments": [{
                "author": {"login": BOT},
                "body": "Guard this lookup.",
                "url": "https://github.com/o/r/pull/7#discussion_r1",
            }],
        }
        posted: list = []
        self._stub(fgt, monkeypatch, pr, [thread], posted)
        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--cycle-summary",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "1", "--verification", "passed", "--no-color",
        ])

        assert fgt.main() == 0

        out = capsys.readouterr().out
        # Chat: one pointer line, not the full receipt. The header carries the
        # script-owned session-cycle ordinal (#104).
        assert "[loop] Cycle receipt (session cycle 1):" in out
        assert "verification passed" in out
        assert "https://github.com/o/r/pull/7#issuecomment-99" in out
        assert "Findings fetched:" not in out
        assert "Re-review requests used:" not in out
        # PR comment: the full receipt, delivered once.
        assert len(posted) == 1
        body = posted[0]
        assert "Findings fetched: 2" in body
        assert "Fixed locally: 1" in body
        assert "RUNNING" in body
        assert "https://github.com/o/r/pull/7#discussion_r1" in body
        assert fgt.STICKY_RECEIPT_MARKER in body

    # --- denominators and carried-over classification (#90 review) --------

    CARRIED_THREAD = {
        "id": "t4", "path": "d.py", "line": 12,
        "comments": [{
            "author": {"login": BOT},
            "body": "Validate this bound before the query runs.",
            "url": "https://github.com/o/r/pull/7#discussion_r4",
        }],
    }

    def _two_cycles_one_survivor(self, fgt, pr):
        """Cycle 1 found 4 findings; by cycle 2 only the 4th is still open."""
        update_run_tracking(pr, [
            ("t1", "a.py"), ("t2", "b.py"), ("t3", "c.py"), ("t4", "d.py"),
        ])
        survivor_fp = fgt.finding_fingerprint(self.CARRIED_THREAD)
        track_finding_fingerprints(pr, {"fp-t1", "fp-t2", "fp-t3", survivor_fp})
        track_finding_fingerprints(pr, {survivor_fp})

    def test_pointer_keeps_cumulative_and_current_counts_apart(
        self, tmp_path, monkeypatch, capsys
    ):
        """findings_fetched is cumulative; new/carried describe the open set.

        With 3 of 4 findings resolved the old line read
        "findings 4 (0 new, 1 carried over)" — two populations, one bracket.
        """
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt

        pr = PullRequest(owner="o", repo="r", number=7)
        self._two_cycles_one_survivor(fgt, pr)
        posted: list = []
        self._stub(fgt, monkeypatch, pr, [self.CARRIED_THREAD], posted)
        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--cycle-summary",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "3", "--verification", "passed", "--no-color",
        ])

        assert fgt.main() == 0

        out = capsys.readouterr().out
        assert "findings 4 seen this run" in out
        assert "open 1 (0 new, 1 carried over)" in out
        # The cumulative count must never carry the current split.
        assert "findings 4 (" not in out
        # The PR comment counts the open set the same way.
        assert "Findings (1): 0 new, 1 carried over" in posted[0]

    def test_terminal_receipt_keeps_carried_over_after_cleanup(
        self, tmp_path, monkeypatch, capsys
    ):
        """--record-run clears the run block that holds the prior fingerprints.

        Reading them after that cleanup marked every still-open finding on a
        capped/human/stopped receipt as "new".
        """
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt

        pr = PullRequest(owner="o", repo="r", number=7)
        self._two_cycles_one_survivor(fgt, pr)
        posted: list = []
        self._stub(fgt, monkeypatch, pr, [self.CARRIED_THREAD], posted)
        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--record-run",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "3", "--verification", "passed",
            "--outcome", "human", "--no-color",
        ])

        assert fgt.main() == 0

        out = capsys.readouterr().out
        assert "open 1 (0 new, 1 carried over)" in out
        assert "1 new" not in out
        assert "Findings (1): 0 new, 1 carried over" in posted[0]
        # Terminal cleanup still happens — the fix is ordering, not skipping it.
        assert read_run_tracking(pr) == {}

    def test_record_run_posts_terminal_receipt_with_done_status(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt
        from metrics import runs_log_path

        pr = PullRequest(owner="o", repo="r", number=7)
        update_run_tracking(pr, [("t1", "a.py")])
        posted: list = []
        self._stub(fgt, monkeypatch, pr, [], posted)
        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--record-run",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "1", "--verification", "passed",
            "--outcome", "clean", "--no-color",
        ])

        assert fgt.main() == 0

        out = capsys.readouterr().out
        assert "[loop] Summary (session cycle 1):" in out
        assert "outcome clean" in out
        assert "#issuecomment-99" in out
        assert "Time to clean PR" not in out  # full receipt stays off stdout
        body = posted[0]
        assert "DONE" in body
        assert "[loop] Summary" in body
        # Terminal semantics unchanged: record written, accumulator cleared.
        assert runs_log_path().exists()
        assert read_run_tracking(pr) == {}

    def test_record_run_non_clean_outcome_posts_stopped_status(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt

        pr = PullRequest(owner="o", repo="r", number=7)
        update_run_tracking(pr, [("t1", "a.py")])
        posted: list = []
        self._stub(fgt, monkeypatch, pr, [], posted)
        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--record-run",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "0", "--verification", "skipped",
            "--outcome", "human", "--no-color",
        ])

        assert fgt.main() == 0
        assert "STOPPED" in posted[0]

    def test_auto_snapshot_backstop_never_posts(self, tmp_path, monkeypatch, capsys):
        """The Stop-hook backstop stays a chat-only lapse-catcher (#86) — it
        must not write PR comments from a Stop hook."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt

        pr = PullRequest(owner="o", repo="r", number=7)
        update_run_tracking(pr, [("t1", "a.py")])
        posted: list = []
        self._stub(fgt, monkeypatch, pr, [], posted)
        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--cycle-summary", "--auto-snapshot",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "0", "--verification", "skipped", "--no-color",
        ])

        assert fgt.main() == 0
        assert posted == []
        assert "[loop] Summary (auto" in capsys.readouterr().out

    def test_dry_run_falls_back_to_stdout_receipt(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt

        pr = PullRequest(owner="o", repo="r", number=7)
        update_run_tracking(pr, [("t1", "a.py")])
        posted: list = []
        self._stub(fgt, monkeypatch, pr, [], posted)
        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--cycle-summary", "--dry-run",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "0", "--verification", "skipped", "--no-color",
        ])

        assert fgt.main() == 0
        assert posted == []
        out = capsys.readouterr().out
        assert "[loop] Cycle receipt" in out
        assert "Findings fetched: 1" in out


class TestDeriveRecordFields:
    def test_observed_fixed_and_findings_and_needs_human(self):
        # baseline saw t1,t2,t3,t4; now t1 still actionable, t2 addressed-by-reply,
        # t3 & t4 gone (presumed fixed). judge off, outcome human.
        fields = derive_record_fields(
            baseline_ids={"t1", "t2", "t3", "t4"},
            current_actionable_ids={"t1"},
            addressed_by_reply_ids={"t2"},
            outcome="human",
            judge_ran=False,
            judge_results={},
        )
        assert fields["findings_fetched"] == 4
        assert fields["observed_fixed_count"] == 2          # t3, t4
        assert fields["remaining_actionable"] == 1          # t1
        assert fields["addressed_by_reply"] == 1            # t2
        assert fields["needs_human"] == 1                   # outcome human -> remaining_actionable

    def test_needs_human_from_judge_when_judge_ran(self):
        fields = derive_record_fields(
            baseline_ids={"t1"},
            current_actionable_ids={"t1"},
            addressed_by_reply_ids=set(),
            outcome="clean",
            judge_ran=True,
            judge_results={"t1": {"verdict": "needs_human", "recommended_action": "escalate"}},
        )
        assert fields["needs_human"] == 1

    def test_needs_human_zero_when_not_human_and_no_judge(self):
        fields = derive_record_fields(
            baseline_ids={"t1"},
            current_actionable_ids=set(),
            addressed_by_reply_ids=set(),
            outcome="clean",
            judge_ran=False,
            judge_results={},
        )
        assert fields["needs_human"] == 0


class TestDeriveOutcome:
    def test_confirmed_clean_even_when_cap_reached(self):
        # Hitting the cap does not make a verified, Gemini-confirmed clean PR capped.
        assert _derive_outcome(0, "passed", cap_reached=True) == "clean"

    def test_verification_failed(self):
        assert _derive_outcome(0, "failed", cap_reached=False) == "verification_failed"

    def test_clean_when_no_remaining_and_passed(self):
        assert _derive_outcome(0, "passed", cap_reached=False) == "clean"

    def test_human_fallback_when_remaining(self):
        assert _derive_outcome(2, "passed", cap_reached=False) == "human"

    def test_human_fallback_when_verification_skipped(self):
        # skipped verification + no remaining is not "clean" (clean requires passed)
        assert _derive_outcome(0, "skipped", cap_reached=False) == "human"

    def test_cap_with_all_likely_fixed_unconfirmed_is_fixed_pending_confirmation(self):
        # Cap reached, threads remain, but every one is multi-signal "likely
        # fixed" and Gemini never re-confirmed → fixed_pending_confirmation,
        # never guessed as capped or clean.
        assert (
            _derive_outcome(
                2,
                "passed",
                cap_reached=True,
                gemini_confirmed=False,
                likely_fixed_remaining=2,
            )
            == "fixed_pending_confirmation"
        )

    def test_cap_with_genuine_unfixed_remaining_is_capped(self):
        # Cap reached with some remaining threads not classified likely-fixed.
        assert (
            _derive_outcome(
                3,
                "passed",
                cap_reached=True,
                gemini_confirmed=False,
                likely_fixed_remaining=1,
            )
            == "capped"
        )

    def test_cap_with_likely_fixed_but_gemini_confirmed_is_capped(self):
        # If Gemini did respond at cap, we don't claim pending-confirmation.
        assert (
            _derive_outcome(
                2,
                "passed",
                cap_reached=True,
                gemini_confirmed=True,
                likely_fixed_remaining=2,
            )
            == "capped"
        )

    def test_verification_failed_beats_cap(self):
        # A failed verification is surfaced even when the cap was also reached.
        assert (
            _derive_outcome(0, "failed", cap_reached=True) == "verification_failed"
        )

    def test_clean_requires_gemini_confirmed(self):
        # No remaining + passed but final wait timed out (unconfirmed) is not
        # clean — it resolves to fixed_pending_confirmation.
        assert (
            _derive_outcome(
                0, "passed", cap_reached=False, gemini_confirmed=False
            )
            == "fixed_pending_confirmation"
        )


# ---------------------------------------------------------------------------
# select_stats_records
# ---------------------------------------------------------------------------

class TestSelectStatsRecords:
    def _rec(self, repo, pr):
        return {"schema_version": 1, "repo": repo, "pr": pr}

    def test_filters_to_repo_and_takes_window(self):
        recs = [
            self._rec("o/r", 1), self._rec("x/y", 2),
            self._rec("o/r", 3), self._rec("o/r", 4),
        ]
        out = select_stats_records(recs, repo="o/r", window=2, all_repos=False)
        assert [r["pr"] for r in out] == [3, 4]   # last 2 for o/r, file order

    def test_all_repos_keeps_everything_in_window(self):
        recs = [self._rec("o/r", 1), self._rec("x/y", 2), self._rec("o/r", 3)]
        out = select_stats_records(recs, repo="o/r", window=2, all_repos=True)
        assert [r["pr"] for r in out] == [2, 3]


class TestFindActiveRun:
    """find_active_run is the cheap, network-free gate the loop hooks use to
    decide whether a Gemini loop is in flight for the current repo before
    spending a GitHub fetch on a summary."""

    def test_returns_none_when_no_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert find_active_run("o/r") is None

    def test_returns_none_when_run_missing_update_seq(self, tmp_path, monkeypatch):
        # A `run` block that never bumped update_seq (legacy/pre-feature cruft,
        # or cleared) is NOT an active loop, even if it has started_at.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#5": {"run": {"started_at": "2026-06-06T10:00:00Z"}}})
        assert find_active_run("o/r") is None

    def test_returns_none_when_no_run_block(self, tmp_path, monkeypatch):
        # A sticky-receipt-only entry (no `run`) is not an active loop.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#5": {"sticky_comment_id": 123}})
        assert find_active_run("o/r") is None

    def test_returns_pr_and_run_for_active(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        run = {"started_at": "2026-06-06T10:00:00Z", "update_seq": 1, "finding_ids": ["a"]}
        save_sticky_state({"o/r#7": {"run": run}})
        assert find_active_run("o/r") == (7, run)

    def test_ignores_other_repos(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"other/repo#1": {"run": {"started_at": "2026-06-06T10:00:00Z", "update_seq": 1}}})
        assert find_active_run("o/r") is None

    def test_does_not_prefix_match_similar_repo(self, tmp_path, monkeypatch):
        # "o/r2" must not be treated as belonging to "o/r".
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r2#1": {"run": {"started_at": "2026-06-06T10:00:00Z", "update_seq": 1}}})
        assert find_active_run("o/r") is None

    def test_picks_most_recently_started_when_multiple_active(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "o/r#1": {"run": {"started_at": "2026-06-06T09:00:00Z", "update_seq": 1}},
            "o/r#2": {"run": {"started_at": "2026-06-06T11:00:00Z", "update_seq": 1}},
        })
        num, _ = find_active_run("o/r")
        assert num == 2


class TestAnyActiveRun:
    """Fast, repo-agnostic gate: lets the Stop hook skip git/repo resolution
    entirely when no loop is in flight anywhere."""

    def test_false_when_no_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert any_active_run() is False

    def test_false_when_only_sticky_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#1": {"sticky_comment_id": 9}})
        assert any_active_run() is False

    def test_false_when_run_block_lacks_update_seq(self, tmp_path, monkeypatch):
        # Legacy/pre-feature run (started_at but no update_seq) is not active —
        # this is what kept stale cruft from any repo looking "active" forever.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#1": {"run": {"started_at": "2026-06-06T10:00:00Z"}}})
        assert any_active_run() is False

    def test_true_when_any_active_run_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "o/r#1": {"sticky_comment_id": 9},
            "x/y#3": {"run": {"started_at": "2026-06-06T10:00:00Z", "update_seq": 1}},
        })
        assert any_active_run() is True


class TestStateCorruptionTolerance:
    """The hooks read a local state.json that can be corrupted or hand-edited.
    Reading it must never crash — bad values are treated as "not active /
    not stale", never raised."""

    def test_summary_is_stale_tolerates_non_numeric_seq(self):
        assert summary_is_stale({"update_seq": "garbage", "last_summary_seq": 1}) is False
        assert summary_is_stale({"update_seq": None, "last_summary_seq": None}) is False

    def test_summary_is_stale_still_works_with_one_corrupt_field(self):
        # Valid update_seq, corrupt last_summary_seq -> treat corrupt as 0.
        assert summary_is_stale({"update_seq": 5, "last_summary_seq": "x"}) is True

    def test_any_active_run_ignores_non_numeric_update_seq(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#1": {"run": {"update_seq": "garbage"}}})
        assert any_active_run() is False

    def test_find_active_run_tolerates_mixed_started_at_types(self, tmp_path, monkeypatch):
        # A corrupted non-str started_at must not crash the sort across candidates.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "o/r#1": {"run": {"started_at": "2026-06-06T09:00:00Z", "update_seq": 1}},
            "o/r#2": {"run": {"started_at": 12345, "update_seq": 1}},
        })
        result = find_active_run("o/r")  # must not raise
        assert result is not None

    def test_stamp_summary_emitted_tolerates_corrupt_update_seq(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        save_sticky_state({"o/r#1": {"run": {"update_seq": "garbage"}}})
        stamp_summary_emitted(pr)  # must not raise


class TestSummaryStaleness:
    """The loop's Stop-hook backstop fires a summary only when the run has
    advanced (a new fetch bumped update_seq) since the last emitted summary.
    This dedups against the agent's own per-cycle summary without timing
    guesswork."""

    def test_update_run_tracking_bumps_update_seq(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        update_run_tracking(pr, [("t1", "a.py")])
        assert read_run_tracking(pr)["update_seq"] == 1
        update_run_tracking(pr, [("t2", "b.py")])
        assert read_run_tracking(pr)["update_seq"] == 2

    def test_fresh_run_with_no_updates_is_not_stale(self):
        # started_at but never fetched: nothing to summarize yet.
        assert summary_is_stale({"started_at": "2026-06-06T10:00:00Z"}) is False

    def test_run_with_updates_and_no_summary_is_stale(self):
        assert summary_is_stale({"update_seq": 1}) is True

    def test_run_summarized_at_current_seq_is_not_stale(self):
        assert summary_is_stale({"update_seq": 2, "last_summary_seq": 2}) is False

    def test_run_advanced_since_last_summary_is_stale(self):
        assert summary_is_stale({"update_seq": 3, "last_summary_seq": 2}) is True

    def test_stamp_summary_emitted_marks_current_seq(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        update_run_tracking(pr, [("t1", "a.py")])
        assert summary_is_stale(read_run_tracking(pr)) is True
        stamp_summary_emitted(pr)
        assert summary_is_stale(read_run_tracking(pr)) is False

    def test_stamp_then_new_fetch_is_stale_again(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        update_run_tracking(pr, [("t1", "a.py")])
        stamp_summary_emitted(pr)
        update_run_tracking(pr, [("t2", "b.py")])  # next cycle's fetch
        assert summary_is_stale(read_run_tracking(pr)) is True

    def test_stamp_is_noop_when_no_active_run(self, tmp_path, monkeypatch):
        # Must not crash or create a run block when there's nothing tracked.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        stamp_summary_emitted(pr)
        assert read_run_tracking(pr) == {}


def test_resolve_judge_phase_infers_complete_for_record_run():
    assert resolve_judge_phase(None, record_run=True) == "complete"


def test_resolve_judge_phase_infers_cycle_for_normal_fetch():
    assert resolve_judge_phase(None, record_run=False) == "cycle"


def test_resolve_judge_phase_explicit_flag_wins():
    assert resolve_judge_phase("cycle", record_run=True) == "cycle"
    assert resolve_judge_phase("complete", record_run=False) == "complete"


class TestFormatterCommands:
    def _profile(self):
        return {
            "source": "confirmed",
            "working_directory": ".",
            "checks": [
                {"name": "root", "command": "uv run pytest", "required": True}
            ],
        }

    def test_profile_intro_cli_outputs_deterministic_text(self, monkeypatch, capsys):
        import fetch_gemini_threads as fgt

        monkeypatch.setattr(fgt, "load_profile_for_repo", lambda repo: self._profile())
        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py",
            "--profile-intro",
            "--repo",
            "OrenAshkenazy/AegisLocal",
            "--no-color",
        ])

        rc = fgt.main()

        assert rc == 0
        assert capsys.readouterr().out.splitlines() == [
            "[loop] Repo-aware verification profile",
            "Profile: OrenAshkenazy/AegisLocal",
            "Checks:",
            "1. root — uv run pytest (cwd: ., required)",
        ]

    def test_planned_verification_cli_outputs_deterministic_text(self, monkeypatch, capsys):
        import fetch_gemini_threads as fgt

        monkeypatch.setattr(fgt, "load_profile_for_repo", lambda repo: self._profile())
        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py",
            "--planned-verification",
            "--repo",
            "OrenAshkenazy/AegisLocal",
            "--no-color",
        ])

        rc = fgt.main()

        assert rc == 0
        assert capsys.readouterr().out.splitlines() == [
            "[loop] Verification suite",
            "Running 1 required repo-aware check:",
            "1. root — uv run pytest (cwd: .)",
        ]


class TestFormatJudgeVerdictSummary:
    def _results(self, verdicts: dict[str, int]) -> dict[str, dict]:
        """Build fake judge_results keyed by thread id."""
        results = {}
        i = 0
        for verdict, count in verdicts.items():
            for _ in range(count):
                results[f"id{i}"] = {"status": "ok", "verdict": verdict}
                i += 1
        return results

    def test_single_verdict_type(self):
        results = self._results({"valid_actionable": 3})
        out = format_judge_verdict_summary(results, "cycle")
        assert "[loop] judge (cycle):" in out
        assert "3 thread(s) evaluated" in out
        assert "valid_actionable: 3" in out

    def test_multiple_verdict_types_sorted_by_count(self):
        results = self._results({"false_positive": 1, "valid_actionable": 2})
        out = format_judge_verdict_summary(results, "complete")
        assert "[loop] judge (complete):" in out
        # valid_actionable (2) should come before false_positive (1) by count.
        assert out.index("valid_actionable") < out.index("false_positive")

    def test_skipped_threads_excluded_from_count(self):
        results = {
            "id0": {"status": "ok", "verdict": "valid_actionable"},
            "id1": {"status": "skipped", "verdict": None},
        }
        out = format_judge_verdict_summary(results, "cycle")
        # Only 1 thread evaluated (the skipped one doesn't count).
        assert "1 thread(s) evaluated" in out

    def test_all_skipped_shows_fallback(self):
        results = {
            "id0": {"status": "skipped", "skip_reason": "no key"},
            "id1": {"status": "skipped", "skip_reason": "no key"},
        }
        out = format_judge_verdict_summary(results, "cycle")
        assert "all skipped" in out

    def test_phase_label_in_output(self):
        results = self._results({"needs_human": 1})
        assert "cycle" in format_judge_verdict_summary(results, "cycle")
        assert "complete" in format_judge_verdict_summary(results, "complete")


class TestFormatJudgeThreadTable:
    def _thread(self, tid, path="src/x.py", line=10, sev_comment=""):
        body = f"![{sev_comment}](url)" if sev_comment else "issue here"
        return {
            "id": tid,
            "isResolved": False,
            "isOutdated": False,
            "path": path,
            "line": line,
            "comments": [{"author": {"login": "gemini-code-assist"}, "body": body}],
        }

    def test_header_format(self):
        threads = [self._thread("t1")]
        results = {"t1": {"status": "ok", "verdict": "valid_actionable",
                           "recommended_action": "fix", "confidence": 0.91}}
        out = format_judge_thread_table(threads, results, "cycle")
        assert out.startswith("[loop] judge eval (cycle): 1 thread(s)")

    def test_per_thread_row_contains_path_verdict_action_conf(self):
        threads = [self._thread("t1", path="src/foo.py", line=42, sev_comment="high")]
        results = {"t1": {"status": "ok", "verdict": "valid_actionable",
                           "recommended_action": "fix", "confidence": 0.91}}
        out = format_judge_thread_table(threads, results, "cycle")
        assert "src/foo.py:42" in out
        assert "valid_actionable" in out
        assert "fix" in out
        assert "0.91" in out

    def test_skipped_thread_shows_reason(self):
        threads = [self._thread("t1")]
        results = {"t1": {"status": "skipped", "skip_reason": "no API key"}}
        out = format_judge_thread_table(threads, results, "cycle")
        assert "skipped" in out
        assert "no API key" in out

    def test_not_evaluated_thread(self):
        threads = [self._thread("t1")]
        results = {}  # no entry for t1
        out = format_judge_thread_table(threads, results, "complete")
        assert "not evaluated" in out

    def test_multiple_threads_indexed(self):
        threads = [self._thread("t1"), self._thread("t2", path="src/bar.py", line=5)]
        results = {
            "t1": {"status": "ok", "verdict": "valid_actionable",
                   "recommended_action": "fix", "confidence": 0.9},
            "t2": {"status": "ok", "verdict": "needs_human",
                   "recommended_action": "escalate", "confidence": 0.8},
        }
        out = format_judge_thread_table(threads, results, "cycle")
        lines = out.splitlines()
        assert lines[0].startswith("[loop] judge eval (cycle): 2 thread(s)")
        assert "  1 " in lines[1]
        assert "  2 " in lines[2]

    def test_phase_label_in_header(self):
        threads = [self._thread("t1")]
        results = {"t1": {"status": "ok", "verdict": "valid_actionable",
                           "recommended_action": "fix", "confidence": 0.85}}
        assert "complete" in format_judge_thread_table(threads, results, "complete")
        assert "cycle" in format_judge_thread_table(threads, results, "cycle")

    def test_confidence_non_numeric_does_not_crash(self):
        threads = [self._thread("t1")]
        results = {"t1": {"status": "ok", "verdict": "valid_actionable",
                           "recommended_action": "fix", "confidence": "not-a-number"}}
        out = format_judge_thread_table(threads, results, "cycle")
        assert "?" in out  # fallback for bad confidence


class TestDetectNoProgress:
    def _pr(self):
        return PullRequest(owner="o", repo="r", number=1, url=None)

    def test_false_on_first_call(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        # Seed update_seq = 1 (as if update_run_tracking ran once).
        save_sticky_state({"o/r#1": {"run": {"started_at": "t", "update_seq": 1}}})
        assert detect_no_progress(pr, "fp_a") is False

    def test_false_when_fingerprint_changes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        save_sticky_state({"o/r#1": {"run": {"started_at": "t", "update_seq": 2,
                                              "thread_fingerprint": "fp_a"}}})
        assert detect_no_progress(pr, "fp_b") is False

    def test_true_when_fingerprint_unchanged_and_seq_ge_2(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        save_sticky_state({"o/r#1": {"run": {"started_at": "t", "update_seq": 2,
                                              "thread_fingerprint": "fp_same"}}})
        assert detect_no_progress(pr, "fp_same") is True

    def test_false_when_seq_is_1_even_if_fingerprint_matches(self, tmp_path, monkeypatch):
        # update_seq == 1 means only one fetch has run; no prior cycle to compare.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        save_sticky_state({"o/r#1": {"run": {"started_at": "t", "update_seq": 1,
                                              "thread_fingerprint": "fp_same"}}})
        assert detect_no_progress(pr, "fp_same") is False

    def test_stores_fingerprint_for_next_call(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        save_sticky_state({"o/r#1": {"run": {"started_at": "t", "update_seq": 1}}})
        detect_no_progress(pr, "fp_stored")
        run = read_run_tracking(pr)
        assert run.get("thread_fingerprint") == "fp_stored"

    def test_false_with_no_active_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        assert detect_no_progress(pr, "fp_any") is False



class TestWaitChunkState:
    PR = PullRequest(owner="o", repo="r", number=5)

    def test_first_chunk_initializes_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        wait = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        assert wait["after"] == "2026-06-11T12:00:00Z"
        assert wait["checks"] == 1
        assert isinstance(wait["started_at"], str) and wait["started_at"]

    def test_second_chunk_same_anchor_accumulates(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        first = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        second = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        assert second["checks"] == 2
        assert second["started_at"] == first["started_at"]

    def test_anchor_change_resets_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        fgt.save_wait_settle(self.PR, "fp-1", "2026-06-11T12:01:00Z")
        wait = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:30:00Z")
        assert wait["after"] == "2026-06-11T12:30:00Z"
        assert wait["checks"] == 1
        assert "stable_fingerprint" not in wait

    def test_settle_persists_across_chunks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        fgt.save_wait_settle(self.PR, "fp-1", "2026-06-11T12:01:00Z")
        wait = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        assert wait["stable_fingerprint"] == "fp-1"
        assert wait["stable_since"] == "2026-06-11T12:01:00Z"

    def test_clear_wait_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        fgt.clear_wait_state(self.PR)
        assert fgt.read_wait_state(self.PR) == {}

    def test_clear_preserves_other_run_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#5": {"run": {"started_at": "2026-06-11T10:00:00Z", "update_seq": 3}}})
        fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        fgt.clear_wait_state(self.PR)
        run = load_sticky_state()["o/r#5"]["run"]
        assert run["update_seq"] == 3
        assert "wait" not in run

    def test_corrupt_state_fails_open(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#5": {"run": {"wait": "not-a-dict"}}})
        wait = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        assert wait["checks"] == 1


class TestWaitElapsedAndDecay:
    def test_elapsed_from_started_at(self):
        wait = {"started_at": "2026-06-11T12:00:00Z"}
        now = datetime.datetime(2026, 6, 11, 12, 2, 30, tzinfo=datetime.timezone.utc)
        assert fgt.wait_elapsed_seconds(wait, None, now=now) == 150

    def test_after_floor_dominates_when_state_lost(self):
        # Fresh started_at (state was wiped) must not restart the budget:
        # the --after anchor bounds total elapsed.
        wait = {"started_at": "2026-06-11T12:09:00Z"}
        now = datetime.datetime(2026, 6, 11, 12, 10, 0, tzinfo=datetime.timezone.utc)
        assert fgt.wait_elapsed_seconds(wait, "2026-06-11T12:00:00Z", now=now) == 600

    def test_missing_started_at_uses_after(self):
        now = datetime.datetime(2026, 6, 11, 12, 5, 0, tzinfo=datetime.timezone.utc)
        assert fgt.wait_elapsed_seconds({}, "2026-06-11T12:00:00Z", now=now) == 300

    def test_no_inputs_returns_zero(self):
        now = datetime.datetime(2026, 6, 11, 12, 5, 0, tzinfo=datetime.timezone.utc)
        assert fgt.wait_elapsed_seconds({}, None, now=now) == 0
        assert fgt.wait_elapsed_seconds({"started_at": "garbage"}, "also-garbage", now=now) == 0

    def test_decay_schedule_follows_persisted_checks_semantics(self):
        # `checks` is incremented at the START of each chunk by
        # begin_wait_chunk, so at the end of the first chunk checks == 1 and
        # the 60s heartbeat has already been served — the next chunk is 300s.
        # Reading `checks <= 1` as "still the first chunk" produced
        # 60 -> 60 -> 300 -> 900 and burned an extra turn (#111 review).
        assert fgt.suggested_next_wait_seconds(0) == 60
        assert fgt.suggested_next_wait_seconds(1) == 300
        assert fgt.suggested_next_wait_seconds(2) == 900
        assert fgt.suggested_next_wait_seconds(10) == 900

    def test_thirty_minute_wait_costs_at_most_four_chunks(self):
        # The #102 acceptance shape: a 30-minute wait must not burn ~8 turns.
        # Mirrors the real loop: chunk k runs for the duration suggested after
        # k-1 chunks have completed.
        elapsed, chunks = 0, 0
        while elapsed < 1800:
            elapsed += fgt.suggested_next_wait_seconds(chunks)
            chunks += 1
        assert chunks <= 4


class TestRunWaitChunk:
    PR = fgt.PullRequest(owner="o", repo="r", number=5)

    @staticmethod
    def _recent_after(seconds_ago: int = 5) -> str:
        return (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=seconds_ago)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _patch(self, monkeypatch, tmp_path, fingerprints, reviews=None):
        """fingerprints: sequence returned by successive fingerprint calls."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        fps = iter(fingerprints)
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: {"reviews": reviews or []})
        monkeypatch.setattr(
            fgt,
            "review_activity_fingerprint",
            lambda pull_request, author, after_iso=None: next(fps),
        )
        monkeypatch.setattr(fgt.time, "sleep", lambda s: None)

    def test_waiting_when_no_activity(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path, [None, None, None])
        clock = iter([0.0, 0.0, 1.0, 2.0, 100.0, 100.0, 100.0])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: next(clock))
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=45,
            after_iso=self._recent_after(), chunk_seconds=60,
        )
        assert result["status"] == "waiting"
        assert result["checks"] == 1
        # The 60s heartbeat chunk just ran; the next one steps up to 300s.
        # This assertion previously encoded the 60 -> 60 off-by-one (#111).
        assert result["next_wait_seconds"] == 300
        assert result["pull_request"] is None

    def test_settling_when_activity_not_yet_stable(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path, ["fp-1", "fp-1"])
        clock = iter([0.0, 0.0, 1.0, 100.0, 100.0, 100.0])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: next(clock))
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=4500,
            after_iso=self._recent_after(), chunk_seconds=60,
        )
        assert result["status"] == "settling"
        assert result["quiet_period_remaining_seconds"] > 0
        # settle state persisted for the next chunk
        wait = fgt.read_wait_state(self.PR)
        assert wait["stable_fingerprint"] == "fp-1"

    def test_settle_survives_chunk_boundary(self, tmp_path, monkeypatch):
        # Chunk 1 sees fp-1 and persists settle state with a stable_since that
        # already satisfies the 45s quiet period. The anchor must be recent
        # (timeout floor) while stable_since is older than quiet_seconds.
        after = self._recent_after(seconds_ago=120)
        stable_since = self._recent_after(seconds_ago=60)
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        fgt.begin_wait_chunk(self.PR, after)
        fgt.save_wait_settle(self.PR, "fp-1", stable_since)
        # Chunk 2 sees the same fingerprint; quiet period (45s) already elapsed
        # relative to the persisted stable_since → ready immediately, without
        # restarting the quiet period.
        self._patch(monkeypatch, tmp_path, ["fp-1"])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: 0.0)
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=45,
            after_iso=after, chunk_seconds=60,
        )
        assert result["status"] == "ready"
        assert result["pull_request"] is not None
        assert fgt.read_wait_state(self.PR) == {}  # cleared on ready

    def test_cycle1_fast_path_no_anchor(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path, ["fp-1"])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: 0.0)
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=45,
            after_iso=None, chunk_seconds=60,
        )
        assert result["status"] == "ready"

    def test_timed_out_via_after_floor_with_lost_state(self, tmp_path, monkeypatch):
        # State was never written before; --after is 20 minutes ago, timeout 900s.
        old_after = self._recent_after(seconds_ago=1200)  # 20 minutes ago
        self._patch(monkeypatch, tmp_path, [None])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: 0.0)
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=45,
            after_iso=old_after, chunk_seconds=60,
        )
        assert result["status"] == "timed_out"
        assert result["elapsed_seconds"] >= 1100

    def test_snapshot_persisted_for_heartbeat(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path, [None, None])
        clock = iter([0.0, 0.0, 100.0, 100.0, 100.0])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: next(clock))
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=45,
            after_iso=self._recent_after(), chunk_seconds=60,
        )
        snapshot = fgt.read_wait_state(self.PR)["last_snapshot"]
        assert snapshot["status"] == result["status"] == "waiting"
        assert snapshot["author"] == "gemini-code-assist"


class TestWaitStopsOnRefusal:
    AFTER = "2026-08-04T12:45:54Z"
    PR = PullRequest(owner="o", repo="r", number=5)

    def _refusing_pr(self):
        return {
            "comments": {
                "nodes": [
                    {
                        "author": {"login": CODEX},
                        "body": "You have reached your Codex usage limits for code reviews.",
                        "createdAt": "2026-08-04T12:46:06Z",
                        "url": "https://github.com/o/r/pull/5#issuecomment-9",
                    }
                ]
            },
            "reviews": {"nodes": []},
            "reviewThreads": {"nodes": []},
        }

    def test_chunked_wait_reports_refused_instead_of_waiting(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: self._refusing_pr())
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)

        chunk = fgt.run_wait_chunk(
            self.PR,
            CODEX,
            timeout_seconds=900,
            interval_seconds=1,
            quiet_seconds=45,
            after_iso=self.AFTER,
            chunk_seconds=60,
        )

        assert chunk["status"] == "refused"
        assert "usage limits" in chunk["reason"]
        assert chunk["url"].endswith("#issuecomment-9")

    def test_blocking_wait_raises_instead_of_burning_the_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: self._refusing_pr())
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)

        with pytest.raises(fgt.ReviewerRefused) as excinfo:
            fgt.wait_for_stable_review(
                self.PR,
                author=CODEX,
                timeout_seconds=900,
                interval_seconds=1,
                quiet_seconds=45,
                after_iso=self.AFTER,
            )

        assert "usage limits" in excinfo.value.refusal["reason"]

    def test_cli_prints_a_stop_line_naming_the_refusal(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "resolve_pr", lambda arg: self.PR)
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: self._refusing_pr())
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
             "--reviewer", CODEX, "--wait", "--after", self.AFTER,
             "--wait-chunk-seconds", "60"],
        )

        assert fgt.main() == 0

        out = capsys.readouterr().out
        assert "[loop] STOP" in out
        assert "refused" in out
        assert "usage limits" in out

    def test_cli_json_reports_refused_status(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "resolve_pr", lambda arg: self.PR)
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: self._refusing_pr())
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
             "--reviewer", CODEX, "--wait", "--after", self.AFTER,
             "--wait-chunk-seconds", "60", "--format", "json"],
        )

        assert fgt.main() == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["wait"]["status"] == "refused"
        assert "usage limits" in payload["wait"]["reason"]
        assert payload["wait"]["kind"] == fgt.REFUSAL_QUOTA

    def test_quota_stop_block_tells_the_operator_to_ask_the_user(self, capsys):
        fgt.print_reviewer_refusal(
            {
                "kind": fgt.REFUSAL_QUOTA,
                "reason": "You have reached your Codex usage limits for code reviews.",
                "url": "https://github.com/o/r/pull/5#issuecomment-9",
            },
            author=CODEX,
            json_output=False,
            color_enabled=False,
        )

        out = capsys.readouterr().out
        assert "ask the user NOW" in out
        assert "Stop the loop" in out
        assert "add credits" in out
        assert "Do not wait" in out

    def test_withdrawn_stop_block_offers_no_retry(self, capsys):
        fgt.print_reviewer_refusal(
            {
                "kind": fgt.REFUSAL_WITHDRAWN,
                "reason": "The consumer version has been sunset.",
            },
            author=BOT,
            json_output=False,
            color_enabled=False,
        )

        out = capsys.readouterr().out
        assert "add credits" not in out
        assert "--outcome-reason 'reviewer refused the review'" in out


class TestBlockingWaitAsBackgroundTask:
    """The blocking --wait is the primary, background-run wait (#85): its
    captured output must show liveness while polling and must end in a
    deterministic relayable line — never a traceback — on timeout."""

    PR = fgt.PullRequest(owner="o", repo="r", number=5)
    AFTER = "2026-06-11T12:00:00Z"

    def _quiet_pr(self) -> dict:
        return {
            "reviewThreads": {"nodes": []},
            "reviews": {"nodes": []},
            "comments": {"nodes": []},
        }

    def test_timeout_raises_typed_exception(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: self._quiet_pr())
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)
        clock = iter([0.0] * 3 + [1000.0] * 10)
        monkeypatch.setattr("fetch_gemini_threads.time.monotonic", lambda: next(clock))

        with pytest.raises(fgt.WaitTimedOut) as excinfo:
            fgt.wait_for_stable_review(
                self.PR, author=CODEX, timeout_seconds=900,
                interval_seconds=1, quiet_seconds=45, after_iso=self.AFTER,
            )
        assert excinfo.value.elapsed_seconds >= 900

    def test_heartbeat_lines_while_polling(self, tmp_path, monkeypatch, capsys):
        """Inspecting the running background task must show elapsed liveness,
        emitted at most once per WAIT_HEARTBEAT_INTERVAL_SECONDS."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: self._quiet_pr())
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)
        # Three polls at t=0/10/70: heartbeats at t=0 and t=70, none at t=10.
        clock = iter([0.0, 0.0, 0.0, 10.0, 10.0, 70.0, 70.0, 70.0, 2000.0, 2000.0])
        monkeypatch.setattr("fetch_gemini_threads.time.monotonic", lambda: next(clock))

        with pytest.raises(fgt.WaitTimedOut):
            fgt.wait_for_stable_review(
                self.PR, author=CODEX, timeout_seconds=900,
                interval_seconds=1, quiet_seconds=45, after_iso=self.AFTER,
            )
        err = capsys.readouterr().err
        heartbeats = [line for line in err.splitlines() if "elapsed" in line]
        # t=0 (first poll), t=70 (past the 60s cadence), t=2000 (final poll,
        # just before the timeout raise). The t=10 polls stay silent.
        assert len(heartbeats) == 3
        assert "(10s elapsed" not in err
        assert "(70s elapsed" in err
        assert f"Waiting for {CODEX} review activity" in heartbeats[0]
        assert "timeout 900s" in heartbeats[0]

    def test_cli_timeout_prints_relayable_line_and_exits_zero(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "resolve_pr", lambda arg: self.PR)
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: self._quiet_pr())
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)
        clock = iter([0.0] * 3 + [1000.0] * 10)
        monkeypatch.setattr("fetch_gemini_threads.time.monotonic", lambda: next(clock))
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
             "--reviewer", CODEX, "--wait", "--after", self.AFTER],
        )

        assert fgt.main() == 0

        out = capsys.readouterr().out
        assert "[loop] wait timed out" in out
        assert "--gemini-unconfirmed" in out

    def test_cli_timeout_json_reports_status(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(fgt, "resolve_pr", lambda arg: self.PR)
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: self._quiet_pr())
        monkeypatch.setattr("fetch_gemini_threads.time.sleep", lambda _s: None)
        clock = iter([0.0] * 3 + [1000.0] * 10)
        monkeypatch.setattr("fetch_gemini_threads.time.monotonic", lambda: next(clock))
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
             "--reviewer", CODEX, "--wait", "--after", self.AFTER,
             "--format", "json"],
        )

        assert fgt.main() == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["wait"]["status"] == "timed_out"
        assert payload["wait"]["elapsed_seconds"] >= 900


class TestWaitChunkCli:
    AFTER = "2026-06-11T12:00:00Z"

    def _patch_common(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("GGRL_NO_COLOR", raising=False)
        monkeypatch.setattr(
            fgt, "resolve_pr", lambda arg: PullRequest(owner="o", repo="r", number=5)
        )
        monkeypatch.setattr(
            fgt,
            "run_wait_chunk",
            lambda *a, **k: {
                "status": "waiting",
                "author": "gemini-code-assist",
                "elapsed_seconds": 90,
                "checks": 2,
                "next_wait_seconds": 90,
                "pull_request": None,
            },
        )

    def test_pending_markdown_prints_purple_heartbeat(self, tmp_path, monkeypatch, capsys):
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
             "--wait", "--after", self.AFTER, "--wait-chunk-seconds", "60"],
        )
        assert fgt.main() == 0
        out = capsys.readouterr().out
        assert "[loop] waiting for chatgpt-codex-connector — 90s elapsed" in out
        assert "\033[95m" in out  # purple

    def test_pending_json_stdout_is_machine_only(self, tmp_path, monkeypatch, capsys):
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
             "--wait", "--after", self.AFTER, "--wait-chunk-seconds", "60",
             "--format", "json"],
        )
        assert fgt.main() == 0
        out = capsys.readouterr().out
        assert "\033[" not in out
        assert "[loop]" not in out
        payload = json.loads(out)
        assert payload["wait"]["status"] == "waiting"
        assert payload["wait"]["next_wait_seconds"] == 90
        assert "pull_request" not in payload["wait"]

    def test_no_chunk_flag_uses_legacy_blocking_wait(self, tmp_path, monkeypatch):
        self._patch_common(monkeypatch, tmp_path)
        called = {}

        def fake_legacy(pr, author=None, timeout_seconds=None, interval_seconds=None, quiet_seconds=None, after_iso=None, **kw):
            called["legacy"] = True
            raise RuntimeError("stop here")  # abort main() after the call we care about

        monkeypatch.setattr(fgt, "wait_for_stable_review", fake_legacy)
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
             "--wait", "--after", self.AFTER],
        )
        # main() catches RuntimeError and returns 1 — we just verify legacy was called
        fgt.main()
        assert called.get("legacy") is True


class TestWaitHeartbeatCommand:
    def test_renders_persisted_snapshot(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("GGRL_NO_COLOR", raising=False)
        pr = PullRequest(owner="o", repo="r", number=5)
        monkeypatch.setattr(fgt, "resolve_pr", lambda arg: pr)
        fgt.begin_wait_chunk(pr, "2026-06-11T12:00:00Z")
        fgt.save_wait_snapshot(pr, {
            "status": "settling",
            "author": "gemini-code-assist",
            "elapsed_seconds": 120,
            "checks": 3,
            "next_wait_seconds": 30,
            "quiet_period_remaining_seconds": 30,
        })
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--wait-heartbeat",
             "--pr", "https://github.com/o/r/pull/5"],
        )
        assert fgt.main() == 0
        out = capsys.readouterr().out
        assert "Reviewer responded — waiting for review threads to settle" in out
        assert "30s quiet period remaining" in out
        assert "\033[95m" in out

    def test_no_wait_in_progress(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(
            fgt, "resolve_pr", lambda arg: PullRequest(owner="o", repo="r", number=5)
        )
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--wait-heartbeat",
             "--pr", "https://github.com/o/r/pull/5"],
        )
        assert fgt.main() == 0
        assert "no reviewer wait in progress" in capsys.readouterr().out


def test_track_pattern_signatures_snapshots_prior(tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    pr = fgt.PullRequest(owner="o", repo="r", number=46)
    out1 = fgt.track_pattern_signatures(pr, {"sigA"})
    assert out1["prior"] == set()
    assert out1["new"] == {"sigA"}
    out2 = fgt.track_pattern_signatures(pr, {"sigA", "sigB"})
    assert out2["prior"] == {"sigA"}
    assert out2["new"] == {"sigB"}
    assert fgt.prior_pattern_signatures(pr) == {"sigA"}


def test_accumulate_and_read_swept_patterns(tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    pr = fgt.PullRequest(owner="o", repo="r", number=46)
    fgt.accumulate_swept_patterns(pr, ["sigA"])
    fgt.accumulate_swept_patterns(pr, ["sigB", "sigA"])
    assert fgt.read_swept_patterns(pr) == {"sigA", "sigB"}


class TestRepoRoot:
    """Clustering reads anchored source lines, so it needs the working tree root.

    Without this wiring the shape-merging capability exists but the loop never
    uses it -- a feature that ships dead.
    """

    def test_returns_the_toplevel_path(self):
        def runner(cmd, **kwargs):
            assert cmd == ["git", "rev-parse", "--show-toplevel"]
            return subprocess.CompletedProcess(cmd, 0, "/srv/acme/widget\n", "")

        import unittest.mock as mock
        with mock.patch.object(subprocess, "run", runner):
            assert fgt.repo_root() == Path("/srv/acme/widget")

    def test_fails_open_when_git_errors(self):
        import unittest.mock as mock

        def failing(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 128, "", "not a git repository")

        with mock.patch.object(subprocess, "run", failing):
            assert fgt.repo_root() is None

        def raising(cmd, **kwargs):
            raise OSError("git not found")

        with mock.patch.object(subprocess, "run", raising):
            assert fgt.repo_root() is None

    def test_blank_output_is_not_a_root(self):
        import unittest.mock as mock

        def blank(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "  \n", "")

        with mock.patch.object(subprocess, "run", blank):
            assert fgt.repo_root() is None


class TestRepoRootForPr:
    PR = PullRequest(owner="acme", repo="widget", number=7)
    ROOT = Path("/srv/acme/widget")

    def _runner(
        self,
        head="abc123",
        remote="git@github.com:acme/widget.git",
        worktree_status="",
    ):
        def run(cmd, **kwargs):
            if cmd == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(cmd, 0, f"{head}\n", "")
            if cmd == ["git", "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(cmd, 0, f"{remote}\n", "")
            if cmd == ["git", "status", "--porcelain", "--untracked-files=all"]:
                return subprocess.CompletedProcess(cmd, 0, worktree_status, "")
            raise AssertionError(cmd)
        return run

    @staticmethod
    def _pr_data(head_repo="acme/widget", base_repo="acme/widget"):
        return {
            "headRefOid": "abc123",
            "headRepository": {"nameWithOwner": head_repo},
            "baseRepository": {"nameWithOwner": base_repo},
        }

    def test_returns_root_only_when_repo_and_head_match(self, monkeypatch):
        monkeypatch.setattr(fgt, "repo_root", lambda cwd=None: self.ROOT)
        monkeypatch.setattr(subprocess, "run", self._runner())

        assert fgt.repo_root_for_pr(self._pr_data()) == self.ROOT

    def test_rejects_checkout_at_another_revision(self, monkeypatch):
        monkeypatch.setattr(fgt, "repo_root", lambda cwd=None: self.ROOT)
        monkeypatch.setattr(subprocess, "run", self._runner(head="stale456"))

        assert fgt.repo_root_for_pr(self._pr_data()) is None

    def test_rejects_checkout_of_another_repository(self, monkeypatch):
        monkeypatch.setattr(fgt, "repo_root", lambda cwd=None: self.ROOT)
        monkeypatch.setattr(
            subprocess,
            "run",
            self._runner(remote="https://github.com/acme/another-repo.git"),
        )

        assert fgt.repo_root_for_pr(self._pr_data()) is None

    def test_rejects_dirty_tracked_worktree(self, monkeypatch):
        monkeypatch.setattr(fgt, "repo_root", lambda cwd=None: self.ROOT)
        monkeypatch.setattr(subprocess, "run", self._runner(worktree_status=" M src/file.py\n"))

        assert fgt.repo_root_for_pr(self._pr_data()) is None

    def test_rejects_untracked_worktree_content(self, monkeypatch):
        monkeypatch.setattr(fgt, "repo_root", lambda cwd=None: self.ROOT)
        monkeypatch.setattr(
            subprocess,
            "run",
            self._runner(worktree_status="?? src/replacement.py\n"),
        )

        assert fgt.repo_root_for_pr(self._pr_data()) is None

    def test_accepts_canonical_repository_after_redirect(self, monkeypatch):
        monkeypatch.setattr(fgt, "repo_root", lambda cwd=None: self.ROOT)
        monkeypatch.setattr(
            subprocess,
            "run",
            self._runner(remote="https://github.com/acme/new-widget.git"),
        )

        data = self._pr_data(head_repo="acme/new-widget", base_repo="acme/new-widget")
        assert fgt.repo_root_for_pr(data) == self.ROOT

    def test_accepts_fork_head_repository(self, monkeypatch):
        monkeypatch.setattr(fgt, "repo_root", lambda cwd=None: self.ROOT)
        monkeypatch.setattr(
            subprocess,
            "run",
            self._runner(remote="git@github.com:contributor/widget.git"),
        )

        data = self._pr_data(head_repo="contributor/widget")
        assert fgt.repo_root_for_pr(data) == self.ROOT

    def test_missing_pr_head_falls_back_to_prose_only(self, monkeypatch):
        monkeypatch.setattr(fgt, "repo_root", lambda cwd=None: self.ROOT)

        assert fgt.repo_root_for_pr({}) is None


class TestSweptPatternLifecycle:
    """The receipt's "Swept N patterns" must describe THIS run, not history.

    Regression: the convergence block unioned the live --swept-pattern
    accumulator with the swept signatures of every prior recorded run of the
    PR, and then wrote that union back into the new record. So one run that
    swept two patterns made every later run of the same PR report "Swept 2
    patterns" forever, even when it swept nothing and passed no
    --swept-pattern at all. This number goes into published evidence.
    """

    PR = PullRequest(owner="acme", repo="widget", number=7)

    @staticmethod
    def _record(swept, patterns_swept_value):
        """A runs.jsonl record for this PR whose patterns.swept is given."""
        return metrics.build_record(
            repo="acme/widget",
            pr=7,
            provider="sourcery-ai",
            findings_fetched=1,
            fixed_count=0,
            observed_fixed_count=0,
            remaining_actionable=1,
            needs_human=0,
            addressed_by_reply=0,
            cycles_used=1,
            cycle_cap=5,
            verification="passed",
            verification_details=None,
            outcome="clean",
            outcome_reason="outcome: clean",
            started_at=None,
            finding_paths=["loaders/profiles.py"],
            judge=None,
            patterns={
                "distinct_patterns": 1,
                "max_cluster_size": 2,
                "pattern_recurrence_rate": 0.0,
                "swept_count": len(swept),
                "signatures": ["type-guard"],
                "swept": sorted(patterns_swept_value),
            },
        )

    def test_a_run_that_swept_nothing_reports_zero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

        # Cycle 1 sweeps two patterns.
        fgt.accumulate_swept_patterns(self.PR, ["exception-wrap", "64250ca5"])
        this_run, _ = fgt.swept_pattern_sets(self.PR, set())
        assert this_run == {"exception-wrap", "64250ca5"}

        # --record-run persists the record and clears the live accumulator.
        metrics.append_record(self._record(this_run, this_run))
        clear_run_tracking(self.PR)

        # Cycle 2 sweeps nothing.
        history = metrics.pattern_history_for_pr("acme/widget", 7)
        assert history["swept"] == {"exception-wrap", "64250ca5"}
        this_run, ever = fgt.swept_pattern_sets(self.PR, history["swept"])

        assert this_run == set(), "this run swept nothing, so its count must be 0"
        assert ever == {"exception-wrap", "64250ca5"}, "history is still available"

        line = metrics.format_convergence_line(
            {"distinct_patterns": 1, "recurred_after_sweep": []},
            swept_count=len(this_run),
        )
        assert line.endswith("Swept 0 patterns.")

    def test_history_still_drives_recurrence_detection(self, tmp_path, monkeypatch):
        """Splitting the sets must not cost us cross-run recurrence."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

        fgt.accumulate_swept_patterns(self.PR, ["type-guard"])
        this_run, _ = fgt.swept_pattern_sets(self.PR, set())
        metrics.append_record(self._record(this_run, this_run))
        clear_run_tracking(self.PR)

        history = metrics.pattern_history_for_pr("acme/widget", 7)
        this_run, ever = fgt.swept_pattern_sets(self.PR, history["swept"])
        assert this_run == set()

        # The pattern swept last run is flagged again this run: that is a
        # recurrence, and it is only detectable via `ever`.
        stats = cluster_findings.recurrence_stats(
            ["type-guard"], prior_sigs={"type-guard"}, swept_sigs=ever
        )
        assert stats["recurred_after_sweep"] == ["type-guard"]

    def test_persisting_the_union_would_compound_across_records(
        self, tmp_path, monkeypatch
    ):
        """Each record must carry only its own sweeps.

        pattern_history_for_pr already unions across records. Writing the union
        back into every new record is what made the count unable to fall.
        """
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

        fgt.accumulate_swept_patterns(self.PR, ["alpha"])
        this_run, _ = fgt.swept_pattern_sets(self.PR, set())
        metrics.append_record(self._record(this_run, this_run))
        clear_run_tracking(self.PR)

        # A later run sweeps nothing; its record must say so.
        history = metrics.pattern_history_for_pr("acme/widget", 7)
        this_run, ever = fgt.swept_pattern_sets(self.PR, history["swept"])
        metrics.append_record(self._record(this_run, this_run))

        records, _ = metrics.load_records()
        swept_per_record = [r["patterns"]["swept"] for r in records]
        assert swept_per_record == [["alpha"], []], (
            "the second record swept nothing and must not inherit the first's"
        )
        assert [r["patterns"]["swept_count"] for r in records] == [1, 0]


class TestConvergenceReceiptWiring:
    """The receipt must use the fix, not merely contain it.

    Regression: the swept sets were derived at one point in main() and rendered
    ~85 lines later at another. Both call sites could be reverted to the union
    with the whole suite still green, because the tests only covered the helper.
    That is the same capability-vs-wiring gap that shipped a dead
    cluster(root=...) in an earlier revision. build_convergence() owns the
    decision so a test of it is a test of what the receipt says, and the source
    check below keeps main() from quietly deciding again.
    """

    PR = PullRequest(owner="acme", repo="widget", number=11)

    @staticmethod
    def _cluster(sig, count=1):
        return cluster_findings.Cluster(
            signature=sig,
            label=sig,
            severity="unknown",
            sites=tuple(f"loaders/profiles.py:{16 + i}" for i in range(count)),
            count=count,
        )

    def test_receipt_says_zero_when_this_run_swept_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

        history = {"seen": {"type-guard"}, "swept": {"exception-wrap", "64250ca5"}}
        result = fgt.build_convergence(self.PR, [self._cluster("type-guard")], history)

        assert result["swept_count"] == 0
        assert result["swept"] == []
        assert result["line"].endswith("Swept 0 patterns."), result["line"]

    def test_receipt_counts_only_this_run_when_it_did_sweep(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        fgt.accumulate_swept_patterns(self.PR, ["alpha"])

        history = {"seen": set(), "swept": {"beta", "gamma"}}
        result = fgt.build_convergence(self.PR, [self._cluster("alpha")], history)

        assert result["swept_count"] == 1, "history must not inflate this run's count"
        assert result["swept"] == ["alpha"]

    def test_recurrence_still_sees_patterns_swept_in_earlier_runs(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

        # Swept in a previous run, flagged again now.
        history = {"seen": {"type-guard"}, "swept": {"type-guard"}}
        result = fgt.build_convergence(self.PR, [self._cluster("type-guard")], history)

        assert result["stats"]["recurred_after_sweep"] == ["type-guard"]
        assert "RECURRED after sweep" in result["line"]
        assert result["swept_count"] == 0, "recurrence is orthogonal to this run's count"

    def test_main_does_not_decide_the_swept_fields_itself(self):
        """Only build_convergence may compute these; main() just reads keys."""
        import ast

        source = Path(fgt.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        owners = {"swept_pattern_sets": [], "format_convergence_line": []}
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                name = (
                    target.attr
                    if isinstance(target, ast.Attribute)
                    else getattr(target, "id", None)
                )
                if name in owners:
                    owners[name].append(func.name)

        assert owners["swept_pattern_sets"] == ["build_convergence"], (
            f"swept_pattern_sets is called from {owners['swept_pattern_sets']}; "
            "only build_convergence may call it"
        )
        assert owners["format_convergence_line"] == ["build_convergence"], (
            f"format_convergence_line is called from "
            f"{owners['format_convergence_line']}; only build_convergence may "
            "render the convergence line"
        )
