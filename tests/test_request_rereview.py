"""Tests for the script-owned re-review request helper."""

from __future__ import annotations

import json
import subprocess

import pytest

import request_rereview
from conftest import stub_cap_check_known_under_cap
import review_vendors


CREATED_AT = "2026-06-09T07:17:16Z"


@pytest.fixture(autouse=True)
def _cap_check_is_inert_by_default(monkeypatch):
    """Neutralize the cap check for tests that are not about the cap.

    See conftest.stub_cap_check_known_under_cap for why "inert" is a known
    count of zero rather than no login. TestCapEnforcement overrides this with
    its own stubs.
    """
    stub_cap_check_known_under_cap(monkeypatch)


def _successful_post(**overrides):
    payload = {"created_at": CREATED_AT, **overrides}
    return subprocess.CompletedProcess(
        args=["gh"],
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )


def test_post_rereview_uses_argv_list_and_extracts_created_at():
    captured = {}

    def runner(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _successful_post()

    result = request_rereview.post_rereview(
        "OrenAshkenazy/AegisLocal",
        11,
        request_rereview.DEFAULT_PHRASE,
        runner=runner,
    )

    assert result["created_at"] == CREATED_AT
    assert isinstance(captured["argv"], list)
    assert captured["kwargs"].get("shell") is not True
    assert captured["argv"] == [
        "gh",
        "api",
        "repos/OrenAshkenazy/AegisLocal/issues/11/comments",
        "--method",
        "POST",
        "--raw-field",
        f"body={request_rereview.DEFAULT_PHRASE}",
    ]


def test_json_stdout_parses_as_json_and_has_no_ansi(monkeypatch, capsys):
    monkeypatch.setattr(
        request_rereview,
        "post_rereview",
        lambda repo, pr, phrase, **kwargs: {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        },
    )

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--json",
    ])

    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed == {
        "created_at": CREATED_AT,
        "repo": "OrenAshkenazy/AegisLocal",
        "pr": 11,
        "phrase": request_rereview.DEFAULT_PHRASE,
    }
    assert "\033[" not in captured.out
    assert captured.err == ""


def test_human_mode_includes_timestamp(monkeypatch, capsys):
    monkeypatch.setattr(
        request_rereview,
        "post_rereview",
        lambda repo, pr, phrase, **kwargs: {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        },
    )

    rc = request_rereview.main(["--repo", "OrenAshkenazy/AegisLocal", "--pr", "11"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == f"[loop] Re-review requested at {CREATED_AT}"


def test_invalid_repo_format_fails(capsys):
    rc = request_rereview.main(["--repo", "OrenAshkenazy/AegisLocal/extra", "--pr", "11"])

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "OWNER/REPO" in captured.err


def test_invalid_pr_fails(capsys):
    rc = request_rereview.main(["--repo", "OrenAshkenazy/AegisLocal", "--pr", "0"])

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "positive integer" in captured.err


def test_gh_failure_exits_nonzero_and_writes_clear_stderr(capsys):
    def runner(argv, **kwargs):  # noqa: ARG001
        return subprocess.CompletedProcess(
            args=argv,
            returncode=1,
            stdout="",
            stderr="permission denied",
        )

    original = request_rereview.post_rereview

    def failing_post(repo, pr, phrase, **kwargs):
        return original(repo, pr, phrase, runner=runner)

    request_rereview.post_rereview = failing_post
    try:
        rc = request_rereview.main(["--repo", "OrenAshkenazy/AegisLocal", "--pr", "11"])
    finally:
        request_rereview.post_rereview = original

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "gh api failed" in captured.err
    assert "permission denied" in captured.err


def test_default_phrase_is_used_when_omitted(monkeypatch, capsys):
    captured = {}

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        }

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main(["--repo", "OrenAshkenazy/AegisLocal", "--pr", "11"])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == request_rereview.DEFAULT_PHRASE


def test_reviewer_mention_builds_default_phrase(monkeypatch, capsys):
    captured = {}

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        }

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--reviewer-mention",
        "coderabbitai",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@coderabbitai please review the latest changes."


def test_review_trigger_mention_alias_builds_default_phrase(monkeypatch, capsys):
    captured = {}

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        }

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--review-trigger-mention",
        "@coderabbitai",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@coderabbitai please review the latest changes."


def test_missing_safe_trigger_returns_json_stop_without_posting(monkeypatch, capsys):
    def fail_post(repo, pr, phrase):  # noqa: ARG001
        raise AssertionError("post_rereview should not be called")

    monkeypatch.setattr(request_rereview, "post_rereview", fail_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--reviewer-login",
        "unknown-bot",
        "--no-safe-trigger",
        "--json",
    ])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert captured.err == ""
    assert payload == {
        "status": "no_safe_trigger",
        "posted": False,
        "reviewer": "unknown-bot",
        "message": (
            "No safe re-review trigger known for `unknown-bot`; "
            "pass --review-trigger-mention to enable re-review requests."
        ),
    }


def test_unknown_reviewer_login_without_trigger_does_not_post(monkeypatch, capsys):
    def fail_post(repo, pr, phrase):  # noqa: ARG001
        raise AssertionError("post_rereview should not be called")

    monkeypatch.setattr(request_rereview, "post_rereview", fail_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--reviewer-login",
        "unknown-bot",
        "--json",
    ])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["status"] == "no_safe_trigger"
    assert payload["posted"] is False
    assert payload["reviewer"] == "unknown-bot"


def test_persisted_review_trigger_is_used_when_mention_is_omitted(
    tmp_path, monkeypatch, capsys
):
    captured = {}
    state = {
        "OrenAshkenazy/AegisLocal#11": {
            "reviewer": {
                "login": "coderabbitai",
                "display_name": "CodeRabbit",
                "review_trigger": "@coderabbitai",
                "source": "confirmed",
                "selected_at": "2026-06-28T12:00:00Z",
            }
        }
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        }

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--json",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@coderabbitai please review the latest changes."


def test_persisted_codex_reviewer_without_trigger_posts_the_exact_codex_phrase(
    tmp_path, monkeypatch, capsys
):
    """Reviewer records written before Codex was known carry review_trigger: null."""
    captured = {}
    state = {
        "ExampleOrg/example-repo#159": {
            "reviewer": {
                "login": "chatgpt-codex-connector",
                "display_name": "Chatgpt Codex Connector",
                "review_trigger": None,
                "source": "confirmed",
                "selected_at": "2026-08-04T11:24:51Z",
            }
        }
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "ExampleOrg/example-repo",
        "--pr",
        "159",
        "--json",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@codex review"


def test_codex_reviewer_login_posts_the_exact_codex_phrase(monkeypatch, capsys):
    captured = {}

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "ExampleOrg/example-repo",
        "--pr",
        "159",
        "--reviewer-login",
        "chatgpt-codex-connector",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@codex review"


def test_codex_mention_does_not_get_the_gemini_sentence_form(monkeypatch, capsys):
    captured = {}

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "ExampleOrg/example-repo",
        "--pr",
        "159",
        "--review-trigger-mention",
        "@codex",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@codex review"


def test_custom_phrase_is_preserved_exactly(monkeypatch, capsys):
    captured = {}
    phrase = "@gemini-code-assist please review the latest changes. Extra context: keep JSON clean."

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        }

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--phrase",
        phrase,
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == phrase


def test_dry_run_does_not_call_gh_and_reports_the_exact_body():
    """The only write to someone else's PR must be previewable."""
    calls = []

    def runner(*args, **kwargs):  # pragma: no cover - must never run
        calls.append(args)
        raise AssertionError("dry run must not invoke gh")

    payload = request_rereview.post_rereview(
        "acme/widget", 159, "@codex review", runner=runner, dry_run=True
    )

    assert calls == []
    assert payload["posted"] is False
    assert payload["dry_run"] is True
    assert payload["phrase"] == "@codex review"
    assert payload["created_at"] is None


def test_dry_run_still_validates_its_arguments():
    with pytest.raises(ValueError):
        request_rereview.post_rereview("not-a-repo", 1, "@codex review", dry_run=True)
    with pytest.raises(ValueError):
        request_rereview.post_rereview("acme/widget", 0, "@codex review", dry_run=True)


def test_default_reviewer_is_installable_on_every_tier():
    """The default applies to every user, so it cannot need a paid tier.

    Gemini still reviews normally for enterprise tenants; it is disqualified as
    a default only because a consumer-tier user can no longer install the app.
    """
    assert not review_vendors.is_consumer_tier_retired(
        request_rereview.DEFAULT_REVIEWER_LOGIN
    )
    assert review_vendors.is_consumer_tier_retired("gemini-code-assist")


def test_a_tier_restricted_vendor_is_still_fully_supported():
    gemini = review_vendors.vendor_for("gemini-code-assist")
    assert gemini is not None
    assert gemini.mention == "@gemini-code-assist"
    assert gemini.rereview_phrase
    assert gemini.auto_reviews is True
    assert "enterprise" in review_vendors.tier_note_for("gemini-code-assist")
    assert review_vendors.tier_note_for("@codex") == ""


class TestCapEnforcement:
    """The cap is the loop's only guarantee it cannot spam a PR."""

    @staticmethod
    def _comments(*bodies_by_login):
        nodes = [{"user": {"login": login}, "body": body}
                 for login, body in bodies_by_login]
        return subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=json.dumps(nodes), stderr=""
        )

    def _runner(self, comments):
        def runner(cmd, **kwargs):
            if cmd[:3] == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="agent\n", stderr=""
                )
            return comments
        return runner

    def test_counts_only_the_agents_own_pings(self):
        runner = self._runner(self._comments(
            ("agent", "@codex review"),
            ("a-human", "@codex review"),
            ("agent", "unrelated comment"),
        ))
        used = request_rereview.count_agent_pings(
            "acme/widget", 159, "@codex", "agent", runner=runner
        )
        assert used == 1

    def test_returns_none_when_the_count_cannot_be_established(self):
        def failing(cmd, **kwargs):
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="boom")

        assert request_rereview.count_agent_pings(
            "acme/widget", 159, "@codex", "agent", runner=failing
        ) is None
        assert request_rereview.count_agent_pings(
            "acme/widget", 159, "@codex", None, runner=failing
        ) is None

    def test_mention_matches_as_a_whole_word(self):
        pattern = request_rereview._trigger_re("@codex")
        assert pattern.search("@codex review")
        assert pattern.search("please @codex review the latest changes.")
        assert not pattern.search("@codex-reviewer please look")

    def test_main_refuses_to_post_once_the_cap_is_used(self, monkeypatch, capsys):
        posted = []
        monkeypatch.setattr(request_rereview, "post_rereview",
                            lambda *a, **k: posted.append(a))
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: "agent")
        monkeypatch.setattr(request_rereview, "count_agent_pings",
                            lambda *a, **k: 3)
        monkeypatch.setattr(request_rereview, "effective_cap", lambda *a: 3)

        rc = request_rereview.main(
            ["--repo", "acme/widget", "--pr", "159", "--json"]
        )

        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert posted == []
        assert payload["status"] == "capped"
        assert payload["posted"] is False
        assert payload["rereviews_used"] == 3
        assert payload["rereview_limit"] == 3

    def test_main_posts_while_under_the_cap(self, monkeypatch, capsys):
        posted = []

        def fake_post(repo, pr, phrase, **kwargs):
            posted.append(phrase)
            return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

        monkeypatch.setattr(request_rereview, "post_rereview", fake_post)
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: "agent")
        monkeypatch.setattr(request_rereview, "count_agent_pings", lambda *a, **k: 2)
        monkeypatch.setattr(request_rereview, "effective_cap", lambda *a: 3)

        assert request_rereview.main(["--repo", "acme/widget", "--pr", "159"]) == 0
        capsys.readouterr()
        assert posted == ["@codex review"]

    def test_a_broken_prefs_file_does_not_lift_the_cap(self, monkeypatch):
        monkeypatch.setattr(request_rereview.judge, "load_preferences",
                            lambda: (_ for _ in ()).throw(OSError("unreadable")))
        assert request_rereview.effective_cap(None) == 3


class TestPaginatedParsing:
    """`gh api --paginate` emits one JSON document per page, concatenated.

    Parsing only the first document made count_agent_pings return None on any
    PR past one page, which silently skipped the cap on long PRs — exactly the
    ones the cap exists for.
    """

    def test_a_single_page_parses(self):
        assert request_rereview.parse_paginated_json('[{"a": 1}]') == [{"a": 1}]

    def test_concatenated_pages_are_flattened_in_order(self):
        stdout = '[{"n": 1}, {"n": 2}][{"n": 3}]\n[{"n": 4}]'
        assert request_rereview.parse_paginated_json(stdout) == [
            {"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}
        ]

    def test_empty_output_is_zero_results_not_a_failure(self):
        assert request_rereview.parse_paginated_json("") == []
        assert request_rereview.parse_paginated_json("   ") == []

    def test_malformed_output_is_unparseable_not_empty(self):
        assert request_rereview.parse_paginated_json("[{") is None
        assert request_rereview.parse_paginated_json("{}") is None
        assert request_rereview.parse_paginated_json('[{"a":1}] garbage') is None

    def test_the_cap_survives_a_multi_page_pr(self):
        page = json.dumps([{"user": {"login": "agent"}, "body": "@codex review"}])

        def runner(cmd, **kwargs):
            if cmd[:3] == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(cmd, 0, "agent\n", "")
            return subprocess.CompletedProcess(cmd, 0, page + page + page, "")

        used = request_rereview.count_agent_pings(
            "acme/widget", 159, "@codex", "agent", runner=runner
        )
        assert used == 3, "concatenated pages must all be counted"


class TestMentionExtraction:
    """The cap counts pings to a reviewer, so it matches the mention.

    Counting the full sentence would miss an earlier cycle that worded the
    request differently, and under-count the cap.
    """

    @pytest.mark.parametrize("phrase,expected", [
        ("@codex review", "@codex"),
        ("@gemini-code-assist please review the latest changes.", "@gemini-code-assist"),
        ("Hey @coderabbitai, take another look", "@coderabbitai"),
        ("please review the latest changes", None),
        ("", None),
    ])
    def test_finds_the_mention(self, phrase, expected):
        assert request_rereview.mention_in(phrase) == expected

    def test_two_differently_worded_pings_both_count(self):
        comments = json.dumps([
            {"user": {"login": "agent"}, "body": "@codex review"},
            {"user": {"login": "agent"}, "body": "@codex please take another look"},
        ])

        def runner(cmd, **kwargs):
            if cmd[:3] == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(cmd, 0, "agent\n", "")
            return subprocess.CompletedProcess(cmd, 0, comments, "")

        assert request_rereview.count_agent_pings(
            "acme/widget", 159, "@codex", "agent", runner=runner
        ) == 2

    def test_a_phrase_with_no_mention_refuses_rather_than_posting_uncapped(
        self, monkeypatch, capsys
    ):
        posted = []
        monkeypatch.setattr(request_rereview, "post_rereview",
                            lambda *a, **k: posted.append(a))

        rc = request_rereview.main([
            "--repo", "acme/widget", "--pr", "159",
            "--phrase", "please review the latest changes", "--json",
        ])

        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert posted == [], "an uncountable write is an uncapped write"
        assert payload["status"] == "uncountable_trigger"

    def test_an_unknown_count_refuses_rather_than_posting_uncapped(self, monkeypatch, capsys):
        # count_agent_pings returns None when the login is unresolved or the
        # comments query fails. Posting anyway would make the cap advisory in
        # exactly the situations where it cannot be checked (Sourcery, #115).
        posted = []

        def fake_post(repo, pr, phrase, **kwargs):
            posted.append(phrase)
            return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

        monkeypatch.setattr(request_rereview, "post_rereview", fake_post)
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: None)
        monkeypatch.setattr(request_rereview, "count_agent_pings", lambda *a, **k: None)
        monkeypatch.setattr(request_rereview, "effective_cap", lambda *a: 3)

        rc = request_rereview.main(["--repo", "acme/widget", "--pr", "159", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert posted == []
        assert payload["status"] == "uncountable_count"
        assert payload["posted"] is False
        assert payload["rereview_limit"] == 3
        assert "gh auth status" in payload["message"]

    def test_no_cap_check_still_bypasses_an_unknown_count(self, monkeypatch, capsys):
        posted = []

        def fake_post(repo, pr, phrase, **kwargs):
            posted.append(phrase)
            return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

        monkeypatch.setattr(request_rereview, "post_rereview", fake_post)
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: None)
        monkeypatch.setattr(request_rereview, "count_agent_pings", lambda *a, **k: None)

        rc = request_rereview.main(
            ["--repo", "acme/widget", "--pr", "159", "--no-cap-check"]
        )
        capsys.readouterr()
        assert rc == 0
        assert posted == ["@codex review"]

    def test_an_explicit_mention_makes_a_custom_phrase_countable(
        self, monkeypatch, capsys
    ):
        posted = []

        def fake_post(repo, pr, phrase, **kwargs):
            posted.append(phrase)
            return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

        monkeypatch.setattr(request_rereview, "post_rereview", fake_post)
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: "agent")
        monkeypatch.setattr(request_rereview, "count_agent_pings", lambda *a, **k: 0)

        rc = request_rereview.main([
            "--repo", "acme/widget", "--pr", "159",
            "--phrase", "please review the latest changes",
            "--reviewer-mention", "@codex",
        ])
        capsys.readouterr()
        assert rc == 0
        assert posted == ["please review the latest changes"]


class TestCycleTransitionStamp:
    """The re-review request IS the cycle-boundary event (#111 review).

    The session-cycle ordinal used to infer the boundary from the PR's
    re-review comments, which broke three ways: `comments(last:100)` makes the
    count non-monotonic on busy PRs, the all-user fallback count leaks other
    people's pings when the agent login cannot be resolved, and PR history
    outlives `clear_run_tracking()`. The stamp written here is local and fires
    exactly once per posted request.
    """

    def _run(self, monkeypatch, tmp_path, *, dry_run=False):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(
            request_rereview, "post_rereview",
            lambda repo, pr, phrase, **kw: {
                "created_at": CREATED_AT, "repo": repo, "pr": pr,
                "phrase": phrase, "posted": not dry_run, "dry_run": dry_run,
            },
        )
        argv = ["--repo", "o/r", "--pr", "7", "--reviewer-mention", "@codex", "--json"]
        assert request_rereview.main(argv) == 0

    def _posted(self, tmp_path):
        path = tmp_path / "state.json"
        if not path.exists():
            return 0
        state = json.loads(path.read_text())
        return state.get("o/r#7", {}).get("run", {}).get("rereviews_posted", 0)

    def test_a_posted_request_stamps_exactly_once(
        self, tmp_path, monkeypatch, capsys
    ):
        self._run(monkeypatch, tmp_path)
        capsys.readouterr()
        assert self._posted(tmp_path) == 1
        self._run(monkeypatch, tmp_path)
        capsys.readouterr()
        assert self._posted(tmp_path) == 2

    def test_a_dry_run_closes_no_cycle(self, tmp_path, monkeypatch, capsys):
        self._run(monkeypatch, tmp_path, dry_run=True)
        capsys.readouterr()
        assert self._posted(tmp_path) == 0

    def test_the_stamp_preserves_sibling_state(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "state.json").write_text(
            json.dumps({"o/r#7": {"commentId": 42, "run": {"session_cycle": 3}}})
        )
        self._run(monkeypatch, tmp_path)
        capsys.readouterr()
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["o/r#7"]["commentId"] == 42
        assert state["o/r#7"]["run"]["session_cycle"] == 3
        assert state["o/r#7"]["run"]["rereviews_posted"] == 1


class TestStateKeyCasing:
    """A case-mismatched --repo must not split the shared state (#111 review)."""

    def test_existing_entry_wins_regardless_of_casing(self):
        state = {"OrenAshkenazy/gh-review-loop#7": {"run": {}}}
        assert (
            request_rereview.state_key_for(state, "orenashkenazy/GH-Review-Loop", 7)
            == "OrenAshkenazy/gh-review-loop#7"
        )

    def test_literal_key_when_no_entry_exists(self):
        assert request_rereview.state_key_for({}, "o/r", 7) == "o/r#7"

    def test_a_different_pr_number_is_not_matched(self):
        state = {"o/r#8": {"run": {}}}
        assert request_rereview.state_key_for(state, "o/r", 7) == "o/r#7"

    def test_the_stamp_lands_on_the_fetchs_entry(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "state.json").write_text(
            json.dumps({"OrenAshkenazy/gh-review-loop#7": {"run": {"session_cycle": 1}}})
        )
        monkeypatch.setattr(
            request_rereview, "post_rereview",
            lambda repo, pr, phrase, **kw: {
                "created_at": CREATED_AT, "repo": repo, "pr": pr,
                "phrase": phrase, "posted": True,
            },
        )
        assert request_rereview.main([
            "--repo", "orenashkenazy/GH-Review-Loop", "--pr", "7",
            "--reviewer-mention", "@codex", "--json",
        ]) == 0
        capsys.readouterr()
        state = json.loads((tmp_path / "state.json").read_text())
        assert list(state) == ["OrenAshkenazy/gh-review-loop#7"]
        assert state["OrenAshkenazy/gh-review-loop#7"]["run"]["rereviews_posted"] == 1
