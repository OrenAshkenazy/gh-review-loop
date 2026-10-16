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


import datetime as _dt  # noqa: E402
import re  # noqa: E402


COPILOT_LOGIN = "copilot-pull-request-reviewer"
COPILOT_REQUEST = "copilot-pull-request-reviewer[bot]"
T_FIRST_REVIEW = "2026-10-07T15:46:14Z"
T_REQUEST = "2026-10-07T16:20:05Z"


def _completed(cmd, payload, rc=0, stderr=""):
    return subprocess.CompletedProcess(
        args=cmd, returncode=rc,
        stdout=json.dumps(payload) if payload is not None else "", stderr=stderr,
    )


def _gql_pr(head, pending=()):
    """The GraphQL half of the state read: head sha and pending bot requests.

    Reviews and review-request events are read from the paginated REST APIs,
    because a GraphQL page can silently omit the two facts the state machine
    depends on (PR #116 review).
    """
    return {"data": {"repository": {"pullRequest": {
        "headRefOid": head,
        "reviewRequests": {"nodes": [
            {"requestedReviewer": {"__typename": "Bot", "login": login}} for login in pending
        ]},
    }}}}


def _rest_reviews(reviews):
    return [
        {"user": {"login": a}, "state": s, "submitted_at": t, "commit_id": c}
        for (a, s, t, c) in reviews
    ]


def _rest_request_events(events, reviewer="Copilot"):
    return [
        {"event": "review_requested", "actor": {"login": a}, "created_at": t,
         "requested_reviewer": {"login": reviewer, "type": "Bot"}}
        for (a, t) in events
    ]


def _state_runner(payload, reviews=(), events=(), rc=0, reviews_rc=0, timeline_rc=0,
                  review_pages=None):
    """Serve the three reads request_state makes, by command shape.

    ``review_pages`` passes pre-split REST pages verbatim, so a test can place
    a review beyond the first page.
    """
    def runner(cmd, **kwargs):
        if cmd[:3] == ["gh", "api", "graphql"]:
            return _completed(cmd, payload if rc == 0 else None, rc=rc, stderr="boom" if rc else "")
        assert cmd[:3] == ["gh", "api", "--paginate"], cmd
        if cmd[3].endswith("/reviews"):
            if reviews_rc:
                return _completed(cmd, None, rc=reviews_rc, stderr="boom")
            pages = review_pages if review_pages is not None else [_rest_reviews(reviews)]
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="\n".join(json.dumps(page) for page in pages), stderr="",
            )
        assert cmd[3].endswith("/timeline"), cmd
        if timeline_rc:
            return _completed(cmd, None, rc=timeline_rc, stderr="boom")
        return _completed(cmd, _rest_request_events(events))
    return runner


def _at(iso):
    return _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)


OLD_REVIEW = (COPILOT_LOGIN, "COMMENTED", T_FIRST_REVIEW, "f4a55e3000")


class TestRequestState:
    def test_request_state_completed_head(self):
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(
                _gql_pr("d7efa49000"),
                reviews=[OLD_REVIEW, (COPILOT_LOGIN, "COMMENTED", "2026-10-07T16:21:40Z", "d7efa49000")],
            ),
        )

        assert state["state"] == "COMPLETED_HEAD"
        assert state["head"] == "d7efa49000"
        assert state["first_completed_review_at"] == T_FIRST_REVIEW

    def test_a_newer_request_on_a_reviewed_head_is_pending(self):
        """GitHub drops a reviewer from the requested list once it submits, so
        a listed request newer than the head review is a later cycle. Reporting
        COMPLETED_HEAD would let the loop read the old clean overview and stop
        before the new review arrives (PR #116 review)."""
        head_review = (COPILOT_LOGIN, "COMMENTED", "2026-10-07T16:21:40Z", "d7efa49000")
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(
                _gql_pr("d7efa49000", pending=[COPILOT_LOGIN]),
                reviews=[OLD_REVIEW, head_review],
                events=[("agent", T_REQUEST), ("a-human", "2026-10-07T16:30:00Z")],
            ),
            now=_at("2026-10-07T16:31:00Z"),
        )

        assert state["state"] == "PENDING"
        assert state["pending_since"] == "2026-10-07T16:30:00Z"

    def test_a_listed_request_keeps_its_age_after_an_overlapping_review(self):
        """Two overlapping requests: a review answers the first, the second is
        still listed. Its age must stay known, or it can never go stale and
        be replaced if it is abandoned (PR #116 review)."""
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(
                _gql_pr("d7efa49000", pending=[COPILOT_LOGIN]),
                reviews=[OLD_REVIEW, (COPILOT_LOGIN, "COMMENTED", "2026-10-07T16:25:00Z", "c0ffee0000")],
                events=[("agent", "2026-10-07T16:10:00Z"), ("agent", "2026-10-07T16:20:00Z")],
            ),
            now=_at("2026-10-07T17:00:00Z"),
        )

        assert state["pending_since"] == "2026-10-07T16:20:00Z"
        assert state["state"] == "STALE_PENDING"

    def test_a_listed_request_without_its_own_event_has_unknown_age(self):
        """GraphQL lists the new request before the REST timeline shows it.
        The only visible event is an old one that a review already answered,
        so it must not age the new request into STALE_PENDING (PR #116 review)."""
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(
                _gql_pr("d7efa49000", pending=[COPILOT_LOGIN]),
                reviews=[(COPILOT_LOGIN, "COMMENTED", "2026-10-07T15:10:00Z", "c0ffee0000")],
                events=[("agent", "2026-10-07T15:00:00Z")],
            ),
            now=_at("2026-10-07T17:00:00Z"),
        )

        assert state["pending_since"] is None
        assert state["state"] == "PENDING"

    def test_a_same_second_listed_request_after_a_head_review_is_pending(self):
        """GitHub times are second-precision. A listed, unpaired request in the
        same second as the head review is still a newer cycle (PR #116 review)."""
        same = "2026-10-07T16:21:40Z"
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(
                _gql_pr("d7efa49000", pending=[COPILOT_LOGIN]),
                reviews=[OLD_REVIEW, (COPILOT_LOGIN, "COMMENTED", same, "d7efa49000")],
                events=[("agent", T_REQUEST), ("agent", same)],
            ),
            now=_at("2026-10-07T16:22:00Z"),
        )

        assert state["state"] == "PENDING"
        assert state["pending_since"] == same

    def test_a_withdrawn_newer_request_supersedes_the_old_head_review(self):
        """The stale path withdrew a same-head request and its replacement
        failed. The unanswered newer event still supersedes the old review,
        so the next run may post the replacement instead of reporting
        already_reviewed_head forever (PR #116 review)."""
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(
                _gql_pr("d7efa49000"),
                reviews=[OLD_REVIEW, (COPILOT_LOGIN, "COMMENTED", "2026-10-07T16:21:40Z", "d7efa49000")],
                events=[("agent", T_REQUEST), ("agent", "2026-10-07T16:30:00Z")],
            ),
            now=_at("2026-10-07T17:00:00Z"),
        )

        assert state["state"] == "ABSENT"

    def test_a_dismissed_head_review_does_not_cover_the_head(self):
        """A dismissed review is withdrawn by a maintainer; it must not block a
        replacement request until the next push (PR #116 review)."""
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(
                _gql_pr("d7efa49000"),
                reviews=[OLD_REVIEW, (COPILOT_LOGIN, "DISMISSED", "2026-10-07T16:21:40Z", "d7efa49000")],
                events=[("agent", T_REQUEST)],
            ),
            now=_at("2026-10-07T17:00:00Z"),
        )

        assert state["state"] == "ABSENT"

    def test_request_state_pending_fresh(self):
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(
                _gql_pr("d7efa49000", pending=[COPILOT_LOGIN]), reviews=[OLD_REVIEW],
                events=[("a-human", "2026-10-07T15:42:27Z"), ("agent", T_REQUEST)],
            ),
            now=_at("2026-10-07T16:23:05Z"),
        )

        assert state["state"] == "PENDING"
        assert state["pending_since"] == T_REQUEST

    def test_request_state_pending_with_unknown_age_stays_pending(self):
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(_gql_pr("d7efa49000", pending=[COPILOT_LOGIN]), reviews=[OLD_REVIEW]),
            now=_at("2026-10-08T16:23:05Z"),
        )

        assert state["state"] == "PENDING"
        assert state["pending_since"] is None

    def test_request_state_stale_pending(self):
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(
                _gql_pr("d7efa49000", pending=[COPILOT_LOGIN]), reviews=[OLD_REVIEW],
                events=[("agent", T_REQUEST)],
            ),
            now=_at("2026-10-07T16:45:05Z"),
        )

        assert state["state"] == "STALE_PENDING"

    def test_request_state_absent_with_stale_review(self):
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(
                _gql_pr("d7efa49000"), reviews=[OLD_REVIEW],
                events=[("a-human", "2026-10-07T15:42:27Z")],
            ),
        )

        assert state["state"] == "ABSENT"
        assert state["first_completed_review_at"] == T_FIRST_REVIEW

    def test_request_state_absent_on_a_never_reviewed_pr(self):
        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(_gql_pr("f4a55e3000")),
        )

        assert state["state"] == "ABSENT"
        assert state["first_completed_review_at"] is None

    def test_request_state_none_when_query_fails(self):
        assert request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(None, rc=1),
        ) is None

    def test_request_state_none_when_reviews_cannot_be_read(self):
        """An unreadable review history leaves both the cap boundary and the
        head check unknown; the caller must stop, not assume ABSENT."""
        assert request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(_gql_pr("d7efa49000"), reviews_rc=1),
        ) is None

    def test_request_state_none_when_the_timeline_cannot_be_read(self):
        assert request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(_gql_pr("d7efa49000"), reviews=[OLD_REVIEW], timeline_rc=1),
        ) is None

    def test_reviews_beyond_one_page_still_bound_the_cap_and_the_head_check(self):
        """On a PR with more than 100 reviews, a single GraphQL page can omit
        Copilot's first review (moving the cycle-0 boundary forward and making
        earlier agent requests look free) and its review of the current head
        (allowing a redundant request). The REST review list is paginated, so
        both facts survive (PR #116 review)."""
        filler = [
            ("a-human", "COMMENTED", f"2026-10-07T15:{minute:02d}:00Z", "f4a55e3000")
            for minute in range(0, 60)
        ]
        page1 = _rest_reviews([OLD_REVIEW, *filler])
        page2 = _rest_reviews([
            *filler,
            (COPILOT_LOGIN, "COMMENTED", "2026-10-07T17:02:00Z", "d7efa49000"),
        ])

        state = request_rereview.request_state(
            "acme/widget", 159, review_vendors.COPILOT,
            runner=_state_runner(_gql_pr("d7efa49000"), review_pages=[page1, page2]),
        )

        assert len(page1) + len(page2) > 100
        assert state["state"] == "COMPLETED_HEAD"
        assert state["first_completed_review_at"] == T_FIRST_REVIEW


class TestReviewRequestHelpers:
    def test_count_agent_review_requests_paginated_rest(self):
        page1 = [
            {"event": "review_requested", "actor": {"login": "agent"}, "created_at": "2026-10-07T15:42:27Z",
             "requested_reviewer": {"login": "Copilot", "type": "Bot"}},
            {"event": "review_requested", "actor": {"login": "agent"}, "created_at": T_REQUEST,
             "requested_reviewer": {"login": "Copilot", "type": "Bot"}},
            {"event": "review_requested", "actor": {"login": "a-human"}, "created_at": "2026-10-07T16:30:00Z",
             "requested_reviewer": {"login": "Copilot", "type": "Bot"}},
            {"event": "commented", "actor": {"login": "agent"}},
        ]
        page2 = [
            {"event": "review_requested", "actor": {"login": "agent"}, "created_at": "2026-10-07T17:00:00Z",
             "requested_reviewer": {"login": "Copilot", "type": "Bot"}},
            {"event": "review_requested", "actor": {"login": "agent"}, "created_at": "2026-10-07T17:05:00Z",
             "requested_team": {"slug": "core"}},
        ]

        def runner(cmd, **kwargs):
            assert cmd[:3] == ["gh", "api", "--paginate"]
            assert cmd[3] == "repos/acme/widget/issues/159/timeline"
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=json.dumps(page1) + "\n" + json.dumps(page2), stderr="")

        # Two of the agent-actored events are the loop's own (recorded as it
        # posted them). The pre-boundary event is free, the human's click and
        # the team request never counted.
        request_rereview.record_rereview_posted(
            "acme/widget", 159, reviewer=COPILOT_LOGIN, requested_at=T_REQUEST
        )
        request_rereview.record_rereview_posted(
            "acme/widget", 159, reviewer=COPILOT_LOGIN, requested_at="2026-10-07T17:00:00Z"
        )

        used = request_rereview.count_agent_review_requests(
            "acme/widget", 159, review_vendors.COPILOT, "agent", T_FIRST_REVIEW, runner=runner
        )

        assert used == 2

    def test_count_agent_review_requests_is_none_when_unknowable(self):
        failing = lambda cmd, **kwargs: _completed(cmd, None, rc=1, stderr="boom")  # noqa: E731
        assert request_rereview.count_agent_review_requests(
            "acme/widget", 159, review_vendors.COPILOT, "agent", T_FIRST_REVIEW, runner=failing
        ) is None
        assert request_rereview.count_agent_review_requests(
            "acme/widget", 159, review_vendors.COPILOT, None, T_FIRST_REVIEW, runner=failing
        ) is None

    @pytest.mark.parametrize("stderr, expected", [
        ('gh: Bad credentials (HTTP 401)', ("permission", 401, False)),
        ('gh: Resource not accessible by integration (HTTP 403)', ("permission", 403, False)),
        ('gh: Not Found (HTTP 404)', ("not_found", 404, False)),
        ('{"message":"Reviews may only be requested from collaborators."}\ngh: Validation Failed (HTTP 422)', ("validation", 422, False)),
        ('gh: You have exceeded a secondary rate limit. (HTTP 403)', ("rate_limited", 403, True)),
        ('gh: API rate limit exceeded (HTTP 429)', ("rate_limited", 429, True)),
        ('gh: Server Error (HTTP 502)', ("transient", 502, True)),
        ('read tcp: connection reset by peer', ("uncertain", None, False)),
        ('', ("uncertain", None, False)),
    ])
    def test_classify_gh_failure_table(self, stderr, expected):
        assert request_rereview.classify_gh_failure(stderr) == expected

    def test_request_review_posts_to_requested_reviewers(self):
        captured = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _completed(cmd, {"requested_reviewers": [{"login": COPILOT_REQUEST}]})

        result = request_rereview.request_review("acme/widget", 159, COPILOT_REQUEST, runner=runner)

        assert captured["cmd"] == [
            "gh", "api", "repos/acme/widget/pulls/159/requested_reviewers",
            "--method", "POST", "--raw-field", f"reviewers[]={COPILOT_REQUEST}",
        ]
        assert captured["kwargs"].get("shell") is not True
        assert result["kind"] == "review_request"
        assert result["reviewer"] == COPILOT_REQUEST
        assert result["posted"] is True
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", result["created_at"])

    def test_request_review_rejection_carries_reason(self):
        def runner(cmd, **kwargs):
            return _completed(cmd, None, rc=1, stderr='{"message":"Reviews may only be requested from collaborators."}\ngh: Validation Failed (HTTP 422)')

        with pytest.raises(request_rereview.ReviewRequestRejected) as info:
            request_rereview.request_review("acme/widget", 159, COPILOT_REQUEST, runner=runner)

        assert info.value.reason == "validation"
        assert info.value.http_status == 422
        assert info.value.retryable is False
        assert "collaborators" in info.value.detail

    def test_request_review_dry_run_calls_nothing(self):
        def runner(cmd, **kwargs):
            raise AssertionError("gh must not run on a dry run")

        result = request_rereview.request_review("acme/widget", 159, COPILOT_REQUEST, runner=runner, dry_run=True)

        assert result["dry_run"] is True and result["posted"] is False
        assert result["reviewer"] == COPILOT_REQUEST

    def test_confirm_posted_after_uncertain_same_second_counts(self):
        events = [{"event": "review_requested", "actor": {"login": "agent"}, "created_at": T_REQUEST,
                   "requested_reviewer": {"login": "Copilot", "type": "Bot"}}]
        runner = lambda cmd, **kwargs: _completed(cmd, events)  # noqa: E731

        assert request_rereview.confirm_posted_after_uncertain(
            "acme/widget", 159, review_vendors.COPILOT, "agent", T_REQUEST, runner=runner
        ) is True
        assert request_rereview.confirm_posted_after_uncertain(
            "acme/widget", 159, review_vendors.COPILOT, "agent", "2026-10-07T16:20:06Z", runner=runner
        ) is False
        assert request_rereview.confirm_posted_after_uncertain(
            "acme/widget", 159, review_vendors.COPILOT, "someone-else", T_REQUEST, runner=runner
        ) is False

    def test_resolve_reviewer_login_prefers_explicit_login_then_mention_then_persisted(self, monkeypatch):
        """An explicit mention is a per-run override and outranks persisted
        state, matching resolve_review_trigger(). Otherwise a persisted Copilot
        plus `--review-trigger-mention @codex` dispatches to the review-request
        path and silently ignores the override (PR #116 review)."""
        monkeypatch.setattr(request_rereview, "read_persisted_reviewer", lambda repo, pr: {"login": "persisted-bot"})
        assert request_rereview.resolve_reviewer_login(repo="acme/widget", pr=159, reviewer_login="explicit-bot", reviewer_mention="@m") == "explicit-bot"
        assert request_rereview.resolve_reviewer_login(repo="acme/widget", pr=159, reviewer_login=None, reviewer_mention="@m") == "m"
        assert request_rereview.resolve_reviewer_login(repo="acme/widget", pr=159, reviewer_login=None, reviewer_mention=None) == "persisted-bot"
        monkeypatch.setattr(request_rereview, "read_persisted_reviewer", lambda repo, pr: None)
        assert request_rereview.resolve_reviewer_login(repo="acme/widget", pr=159, reviewer_login=None, reviewer_mention="@m") == "m"
        assert request_rereview.resolve_reviewer_login(repo="acme/widget", pr=159, reviewer_login=None, reviewer_mention=None) is None


def test_landed_check_is_unknown_without_the_agent_login():
    """Without the login, a teammate's manual request cannot be told from the
    loop's, so the answer is unknown, never "landed" (PR #116 review)."""
    def runner(*a, **k):
        pytest.fail("no read is useful without the actor to match")

    assert request_rereview.request_landed_since(
        "acme/widget", 159, review_vendors.COPILOT, None, "2026-10-07T16:00:00Z", runner=runner
    ) is None


def test_a_review_after_someone_elses_request_is_not_the_loops():
    """The loop's request never landed, a teammate asked Copilot by hand, and
    Copilot reviewed. That review answers the teammate; crediting it to the
    loop would charge a free request to the cap (PR #116 review)."""
    runner = _state_runner(
        _gql_pr("d7efa49000"),
        reviews=[(COPILOT_LOGIN, "COMMENTED", "2026-10-07T16:30:00Z", "d7efa49000")],
        events=[("a-human", "2026-10-07T16:25:00Z")],
    )

    assert request_rereview.request_landed_since(
        "acme/widget", 159, review_vendors.COPILOT, "agent", "2026-10-07T16:20:00Z", runner=runner
    ) is None


def test_settling_tolerates_a_fast_local_clock():
    """``since`` is the local pre-POST time; the event is GitHub's. A local
    clock a few seconds ahead must not hide the event (PR #116 review)."""
    runner = _state_runner(_gql_pr("d7efa49000"), events=[("agent", "2026-10-07T16:19:55Z")])

    assert request_rereview.request_landed_since(
        "acme/widget", 159, review_vendors.COPILOT, "agent", "2026-10-07T16:20:00Z", runner=runner
    ) is True


def test_settling_matches_the_login_case_insensitively():
    runner = _state_runner(_gql_pr("d7efa49000"), events=[("Agent", "2026-10-07T16:20:01Z")])

    assert request_rereview.request_landed_since(
        "acme/widget", 159, review_vendors.COPILOT, "agent", "2026-10-07T16:20:00Z", runner=runner
    ) is True


def test_logged_requests_the_timeline_lags_on_still_count(monkeypatch, tmp_path):
    """A countable request durably logged but not yet on the REST timeline
    still consumed the cap; the enforced count must not drop it (PR #116 review)."""
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    request_rereview.record_rereview_posted(
        "acme/widget", 159, reviewer=COPILOT_LOGIN, requested_at="2026-10-07T16:20:05Z")
    request_rereview.record_rereview_posted(
        "acme/widget", 159, reviewer=COPILOT_LOGIN, requested_at="2026-10-07T16:50:05Z")
    runner = _state_runner(_gql_pr("d7efa49000"), events=[("agent", "2026-10-07T16:20:06Z")])

    assert request_rereview.count_agent_review_requests(
        "acme/widget", 159, review_vendors.COPILOT, "agent", T_FIRST_REVIEW, runner=runner
    ) == 2


class TestReviewRequestMain:
    """main() routes a review_request vendor away from the comment path."""

    @staticmethod
    def _stub_reads(monkeypatch, *, state="ABSENT", used=0, head="abc1234def", pending_since=None):
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: "agent")
        monkeypatch.setattr(request_rereview, "count_agent_review_requests", lambda *a, **k: used)
        monkeypatch.setattr(
            request_rereview, "request_state",
            lambda *a, **k: None if state is None else {
                "state": state, "head": head, "pending_since": pending_since,
                "first_completed_review_at": T_FIRST_REVIEW,
            },
        )
        monkeypatch.setattr(
            request_rereview, "post_rereview",
            lambda *a, **k: pytest.fail("a review_request vendor must never get a comment"),
        )
        # GitHub registered the request (the live run showed it can answer 201
        # and register nothing; tests about that stub this to False).
        monkeypatch.setattr(request_rereview, "request_registered", lambda *a, **k: True)

    @staticmethod
    def _fake_request(sink):
        def fake(repo, pr, request_login, **kwargs):
            sink["login"] = request_login
            sink["calls"] = sink.get("calls", 0) + 1
            if kwargs.get("dry_run"):
                return {"created_at": None, "repo": repo, "pr": pr, "kind": "review_request",
                        "reviewer": request_login, "dry_run": True, "posted": False}
            return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "kind": "review_request",
                    "reviewer": request_login, "posted": True}
        return fake

    @staticmethod
    def _run(args):
        return request_rereview.main(["--repo", "acme/widget", "--pr", "159", *args])

    @staticmethod
    def _stamped(tmp_path):
        path = request_rereview.state_path()
        if not path.exists():
            return 0
        return json.loads(path.read_text(encoding="utf-8")).get("acme/widget#159", {}).get("run", {}).get("rereviews_posted", 0)

    @staticmethod
    def _marker():
        return request_rereview.read_unconfirmed_request("acme/widget", 159)

    @staticmethod
    def _seed_marker(*, seconds_ago, kind, countable=True):
        import datetime as dt
        at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert request_rereview.record_unconfirmed_request(
            "acme/widget", 159, reviewer=COPILOT_REQUEST, at=at, kind=kind, countable=countable)
        return at

    def test_copilot_login_requests_a_review_instead_of_commenting(self, monkeypatch, capsys):
        self._stub_reads(monkeypatch)
        requested = {}
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request(requested))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert requested["login"] == COPILOT_REQUEST
        assert payload["kind"] == "review_request"
        assert payload["created_at"] == CREATED_AT
        assert payload["state"] == "ABSENT"
        assert payload["replaced_stale_request"] is False

    def test_persisted_copilot_with_a_stale_mention_trigger_still_requests_a_review(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        request_rereview.state_path().write_text(json.dumps({
            "acme/widget#159": {"reviewer": {"login": COPILOT_LOGIN, "review_trigger": "@copilot"}}
        }), encoding="utf-8")
        self._stub_reads(monkeypatch)
        requested = {}
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request(requested))

        assert self._run(["--json"]) == 0

        assert requested["login"] == COPILOT_REQUEST
        assert json.loads(capsys.readouterr().out)["kind"] == "review_request"

    def test_pending_state_posts_nothing_and_skips_the_cap(self, monkeypatch, capsys):
        self._stub_reads(monkeypatch, state="PENDING", pending_since=T_REQUEST)
        monkeypatch.setattr(request_rereview, "count_agent_review_requests", lambda *a, **k: pytest.fail("no cap read when nothing is posted"))
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("pending: must not request"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "review_pending"
        assert payload["posted"] is False
        assert payload["pending_since"] == T_REQUEST

    def test_already_reviewed_head_posts_nothing(self, monkeypatch, capsys):
        self._stub_reads(monkeypatch, state="COMPLETED_HEAD")
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("head already reviewed"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "already_reviewed_head"
        assert payload["posted"] is False
        assert "abc1234" in payload["message"]

    def test_head_mismatch_posts_nothing(self, monkeypatch, capsys):
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("head mismatch: must not request"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--expect-head", "zzz9999", "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "head_mismatch"
        assert payload["posted"] is False
        assert payload["expected_head"] == "zzz9999" and payload["head"] == "abc1234def"

    def test_stale_pending_cancels_the_open_request_before_reposting(self, monkeypatch, capsys):
        """GraphQL still lists the request, and the design evidence records
        that posting while one is pending produces two reviews. Age alone does
        not prove abandonment, so the open request is explicitly cancelled
        first — never left live alongside a second one (PR #116 review)."""
        self._stub_reads(monkeypatch, state="STALE_PENDING", pending_since="2026-10-07T15:50:00Z")
        cancelled = {}
        monkeypatch.setattr(
            request_rereview, "cancel_review_request",
            lambda repo, pr, login, **kw: cancelled.update(login=login, calls=cancelled.get("calls", 0) + 1) or True,
        )
        requested = {}
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request(requested))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert cancelled == {"login": COPILOT_REQUEST, "calls": 1}
        assert requested["calls"] == 1
        assert payload["state"] == "STALE_PENDING"
        assert payload["replaced_stale_request"] is True

    def test_stale_pending_posts_nothing_when_the_request_cannot_be_cancelled(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch, state="STALE_PENDING", pending_since="2026-10-07T15:50:00Z")
        monkeypatch.setattr(request_rereview, "cancel_review_request", lambda *a, **k: False)
        monkeypatch.setattr(
            request_rereview, "request_review",
            lambda *a, **k: pytest.fail("a still-pending request must not be doubled"),
        )

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "stale_request_not_replaced"
        assert payload["posted"] is False
        assert self._stamped(tmp_path) == 0

    def test_a_5xx_is_never_replayed_blindly(self, monkeypatch, capsys, tmp_path):
        """GitHub can register the request and still answer 5xx, and it does not
        dedupe, so an immediate second POST can create a second review. A 5xx
        is reconciled against the timeline; unseen, it is unknown, remembered,
        and settled by the next run (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch, state="STALE_PENDING", pending_since="2026-10-07T15:50:00Z")
        monkeypatch.setattr(request_rereview, "cancel_review_request", lambda *a, **k: True)
        monkeypatch.setattr(request_rereview, "confirm_posted_after_uncertain", lambda *a, **k: False)
        calls = {"n": 0}

        def fail(*a, **k):
            calls["n"] += 1
            raise request_rereview.ReviewRequestRejected(
                "HTTP 502", reason="transient", http_status=502, retryable=True,
                requested_at=CREATED_AT)

        monkeypatch.setattr(request_rereview, "request_review", fail)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert calls["n"] == 1
        assert payload["status"] == "review_request_uncertain"
        assert payload["posted"] is None
        assert payload["withdrew_stale_request"] is True
        assert {k: v for k, v in self._marker().items() if k != "id"} == {"reviewer": COPILOT_REQUEST, "at": CREATED_AT, "kind": "maybe", "countable": True}
        assert self._stamped(tmp_path) == 0

    def test_a_5xx_the_timeline_shows_landed_counts_as_posted(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "confirm_posted_after_uncertain", lambda *a, **k: True)

        def fail(*a, **k):
            raise request_rereview.ReviewRequestRejected(
                "HTTP 502", reason="transient", http_status=502, retryable=True,
                requested_at=CREATED_AT)

        monkeypatch.setattr(request_rereview, "request_review", fail)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["posted"] is True and payload["confirmed_after_uncertain"] is True
        assert self._marker() is None
        assert self._stamped(tmp_path) == 1

    def test_stale_pending_reports_the_withdrawal_when_the_replacement_fails(self, monkeypatch, capsys, tmp_path):
        """A definite failure after the withdrawal must say nothing is pending
        now, not claim the old request is still there (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch, state="STALE_PENDING", pending_since="2026-10-07T15:50:00Z")
        monkeypatch.setattr(request_rereview, "cancel_review_request", lambda *a, **k: True)

        def refuse(*a, **k):
            raise request_rereview.ReviewRequestRejected(
                "HTTP 422", reason="validation", http_status=422, retryable=False)

        monkeypatch.setattr(request_rereview, "request_review", refuse)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["posted"] is False
        assert payload["withdrew_stale_request"] is True
        assert "nothing is pending" in payload["message"].lower()
        assert self._stamped(tmp_path) == 0

    def test_concurrent_run_holding_the_pr_lock_posts_nothing(self, monkeypatch, capsys, tmp_path):
        """Two loops for one PR can both read ABSENT and both POST; GitHub does
        not dedupe. The state-check/count/POST sequence runs under a per-PR
        lock, and a run that cannot take it posts nothing (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(request_rereview, "LOCK_WAIT_SECONDS", 0)
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(
            request_rereview, "request_review",
            lambda *a, **k: pytest.fail("another run holds the lock: must not request"),
        )
        with request_rereview.pr_request_lock("acme/widget", 159) as held:
            assert held is True
            assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "request_in_progress"
        assert payload["posted"] is False

    def test_lock_key_ignores_repo_casing(self, monkeypatch, capsys, tmp_path):
        """`Acme/Widget` and `acme/widget` are the same PR on GitHub and must
        take the same lock (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        # macOS filenames ignore case, so compare the paths, not just the lock.
        assert request_rereview.lock_path("Acme/Widget", 159) == request_rereview.lock_path("acme/widget", 159)
        monkeypatch.setattr(request_rereview, "LOCK_WAIT_SECONDS", 0)
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("same PR is locked"))
        with request_rereview.pr_request_lock("Acme/Widget", 159) as held:
            assert held is True
            assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["status"] == "request_in_progress"

    @pytest.mark.parametrize("break_lock", ["no_lock_module", "unwritable_dir"])
    def test_no_lock_means_no_post(self, monkeypatch, capsys, tmp_path, break_lock):
        """Without serialization two runs can both read ABSENT and both POST,
        so a run that cannot establish the lock posts nothing (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        if break_lock == "no_lock_module":
            monkeypatch.setattr(request_rereview, "fcntl", None)
            monkeypatch.setattr(request_rereview, "msvcrt", None)
        else:
            monkeypatch.setattr(request_rereview, "lock_path", lambda repo, pr: tmp_path / "missing" / "x" / "\0bad")
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("no lock: must not request"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "lock_unavailable"
        assert payload["posted"] is False

    def test_lock_is_released_after_a_run(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0
        capsys.readouterr()
        with request_rereview.pr_request_lock("acme/widget", 159) as held:
            assert held is True

    def test_stale_pending_dry_run_cancels_nothing(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch, state="STALE_PENDING", pending_since="2026-10-07T15:50:00Z")
        monkeypatch.setattr(
            request_rereview, "cancel_review_request",
            lambda *a, **k: pytest.fail("a dry run must not cancel a real review request"),
        )
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--dry-run", "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True and payload["would_post"] is True
        assert self._stamped(tmp_path) == 0

    def test_unknown_state_refuses_to_post(self, monkeypatch, capsys):
        self._stub_reads(monkeypatch, state=None)
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("unknown state: must not request"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["status"] == "uncountable_state"

    def test_rejected_422_is_validation_and_not_stamped(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)

        def rejecting(*a, **k):
            raise request_rereview.ReviewRequestRejected("Copilot code review is not enabled", reason="validation", http_status=422, retryable=False)

        monkeypatch.setattr(request_rereview, "request_review", rejecting)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "review_request_rejected"
        assert payload["reason"] == "validation"
        assert payload["http_status"] == 422
        assert payload["retryable"] is False
        assert payload["posted"] is False
        assert "not enabled" in payload["message"]
        assert self._stamped(tmp_path) == 0

    def test_an_unseen_accepted_request_is_unknown_not_refused(self, monkeypatch, capsys, tmp_path):
        """A 201 the read-back window does not see yet may still appear: the
        window is seconds, the API lag is not bounded. Report unknown and
        remember the request; `not_enabled` needs the settle time to pass with
        nothing on the PR (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))
        monkeypatch.setattr(request_rereview, "request_registered", lambda *a, **k: False)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "review_request_uncertain"
        assert payload["reason"] == "registration_pending"
        assert payload["http_status"] == 201
        assert payload["posted"] is None
        assert {k: v for k, v in self._marker().items() if k != "id"} == {"reviewer": COPILOT_REQUEST, "at": CREATED_AT, "kind": "accepted", "countable": True}
        assert self._stamped(tmp_path) == 0

    def test_a_settling_request_blocks_a_new_post(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._seed_marker(seconds_ago=10, kind="accepted")
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("still settling: must not post"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "review_request_uncertain"
        assert payload["reason"] == "settling"
        assert payload["posted"] is None
        assert self._marker() is not None

    def test_an_accepted_request_still_absent_after_settling_is_not_enabled(self, monkeypatch, capsys, tmp_path):
        """Live run 2026-10-10 (copilot-review-lab#1, no Copilot plan): 201 and
        nothing ever on the PR. Absence past the settle time is the definite
        signal, and it is reported without posting again."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._seed_marker(seconds_ago=600, kind="accepted")
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_landed_since", lambda *a, **k: False)
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("dropped once: do not post again"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "review_request_rejected"
        assert payload["reason"] == "not_enabled"
        assert payload["posted"] is False
        assert "did not add" in payload["message"] and "plan" in payload["message"]
        assert self._marker() is None
        assert self._stamped(tmp_path) == 0

    def test_a_maybe_request_absent_after_settling_is_posted_fresh(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._seed_marker(seconds_ago=600, kind="maybe")
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_landed_since", lambda *a, **k: False)
        requested = {}
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request(requested))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert requested["calls"] == 1 and payload["posted"] is True
        assert self._marker() is None

    def test_a_settled_request_that_landed_is_recorded_before_posting(self, monkeypatch, capsys, tmp_path):
        """It landed and was reviewed on an older head: it was a real loop
        request, so its provenance is recorded for the cap."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        at = self._seed_marker(seconds_ago=600, kind="accepted")
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_landed_since", lambda *a, **k: True)
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["posted"] is True
        assert self._marker() is None
        times = request_rereview.loop_request_times("acme/widget", 159, review_vendors.COPILOT)
        assert times == sorted([at, CREATED_AT])

    def test_a_marker_that_shows_up_pending_is_recorded_for_the_cap(self, monkeypatch, capsys, tmp_path):
        """The uncertain request became visible before this run: it was a real
        loop request, so its time joins the provenance list before the marker
        is cleared, or later cap checks undercount it (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        at = self._seed_marker(seconds_ago=30, kind="maybe")
        self._stub_reads(monkeypatch, state="PENDING", pending_since=T_REQUEST)
        monkeypatch.setattr(request_rereview, "request_landed_since", lambda *a, **k: True)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["status"] == "review_pending"
        assert self._marker() is None
        assert request_rereview.loop_request_times("acme/widget", 159, review_vendors.COPILOT) == [at]

    def test_an_unreadable_timeline_keeps_the_marker(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._seed_marker(seconds_ago=30, kind="maybe")
        self._stub_reads(monkeypatch, state="PENDING", pending_since=T_REQUEST)
        monkeypatch.setattr(request_rereview, "request_landed_since", lambda *a, **k: None)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["status"] == "review_pending"
        assert self._marker() is not None

    @pytest.mark.parametrize("countable", [True, False])
    def test_a_confirmed_late_request_advances_the_cycle_only_if_it_was_a_re_review(self, monkeypatch, capsys, tmp_path, countable):
        """A re-review that is confirmed a run later is still the cycle
        transition the fetch needs; the free first request is not, and never
        joins the cap provenance (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._seed_marker(seconds_ago=30, kind="maybe", countable=countable)
        self._stub_reads(monkeypatch, state="PENDING", pending_since=T_REQUEST)
        monkeypatch.setattr(request_rereview, "request_landed_since", lambda *a, **k: True)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        capsys.readouterr()
        assert self._stamped(tmp_path) == (1 if countable else 0)
        times = request_rereview.loop_request_times("acme/widget", 159, review_vendors.COPILOT)
        assert len(times) == (1 if countable else 0)

    def test_an_unsaved_marker_is_reported_not_claimed(self, monkeypatch, capsys, tmp_path):
        """If the record of an unconfirmed request cannot be written, the next
        run cannot settle it; the payload must say so instead of implying the
        request was remembered (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))
        monkeypatch.setattr(request_rereview, "request_registered", lambda *a, **k: False)
        saves = iter([True, False])  # the pre-POST reservation lands; the follow-up save fails
        monkeypatch.setattr(request_rereview, "record_unconfirmed_request", lambda *a, **k: next(saves))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "review_request_uncertain"
        assert payload["remembered"] is False
        assert "check the PR" in payload["message"]

    def test_the_first_request_is_recorded_as_not_countable(self, monkeypatch, capsys, tmp_path):
        """Whether a request is a re-review is known when it is posted, so it
        is stored with the record instead of re-derived later by comparing the
        local clock with GitHub's (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(
            request_rereview, "request_state",
            lambda *a, **k: {"state": "ABSENT", "head": "abc1234def", "pending_since": None,
                             "first_completed_review_at": None},
        )
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        capsys.readouterr()
        assert request_rereview.loop_request_times("acme/widget", 159, review_vendors.COPILOT) == []

    def test_a_reservation_is_saved_before_the_post_and_settled_after(self, monkeypatch, capsys, tmp_path):
        """A crash between GitHub accepting the request and the log append
        would leave the cap undercounted. The request is reserved before the
        POST, so a crash leaves a record the next run settles (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        seen = {}
        ok = self._fake_request({})

        def post(repo, pr, login, **kw):
            seen["marker"] = self._marker()
            return ok(repo, pr, login, **kw)

        monkeypatch.setattr(request_rereview, "request_review", post)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["posted"] is True
        assert seen["marker"]["kind"] == "maybe" and seen["marker"]["countable"] is True
        assert self._marker() is None
        assert request_rereview.loop_request_times("acme/widget", 159, review_vendors.COPILOT) == [CREATED_AT]

    def test_an_unreadable_marker_refuses_to_post(self, monkeypatch, capsys, tmp_path):
        """A corrupt record of an unconfirmed request is not an absent one:
        posting could duplicate the request it describes (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        path = request_rereview.unconfirmed_path("acme/widget", 159)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"reviewer": "copilot-pull', encoding="utf-8")
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("corrupt marker: must not post"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "unconfirmed_unreadable"
        assert payload["posted"] is False
        assert str(path) in payload["message"]

    def test_the_reservation_stays_when_nothing_durable_was_recorded(self, monkeypatch, capsys, tmp_path):
        """GitHub accepted the request but neither the log append nor the
        incomplete flag could be saved. The reservation is then the only
        record left, so it must stay for the next run to settle (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))
        monkeypatch.setattr(request_rereview.loop_state, "append_request_log", lambda *a, **k: False)
        real_write = request_rereview.Path.write_text

        def no_state(self, *args, **kwargs):
            if self.name == "state.json":
                raise OSError("read-only file system")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(request_rereview.Path, "write_text", no_state)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["posted"] is True
        assert self._marker() is not None

    def test_a_definite_refusal_releases_the_reservation(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)

        def refuse(*a, **k):
            raise request_rereview.ReviewRequestRejected(
                "HTTP 422", reason="validation", http_status=422, retryable=False)

        monkeypatch.setattr(request_rereview, "request_review", refuse)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["status"] == "review_request_rejected"
        assert self._marker() is None

    def test_no_reservation_means_no_post(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "record_unconfirmed_request", lambda *a, **k: False)
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("unreserved: must not post"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "state_unwritable"
        assert payload["posted"] is False

    def test_a_dry_run_reserves_nothing(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "record_unconfirmed_request", lambda *a, **k: pytest.fail("dry run"))
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--dry-run", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["dry_run"] is True

    def test_the_free_first_request_does_not_advance_the_cycle(self, monkeypatch, capsys, tmp_path):
        """The first request is cycle 0. Stamping it as a cycle transition
        puts the session ordinal one ahead (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(
            request_rereview, "request_state",
            lambda *a, **k: {"state": "ABSENT", "head": "abc1234def", "pending_since": None,
                             "first_completed_review_at": None},
        )
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["posted"] is True
        assert self._stamped(tmp_path) == 0

    def test_settling_an_already_logged_reservation_records_nothing_twice(self, monkeypatch, capsys, tmp_path):
        """A crash after the log append but before the reservation was
        cleared must not record the same request again on the next run
        (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        at = self._seed_marker(seconds_ago=30, kind="maybe")
        marker = self._marker()
        request_rereview.record_rereview_posted(
            "acme/widget", 159, reviewer=COPILOT_REQUEST, requested_at=at, request_id=marker["id"],
        )
        stamped_before = self._stamped(tmp_path)
        self._stub_reads(monkeypatch, state="PENDING", pending_since=T_REQUEST)
        monkeypatch.setattr(request_rereview, "request_landed_since", lambda *a, **k: True)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        capsys.readouterr()
        assert self._marker() is None
        assert self._stamped(tmp_path) == stamped_before
        assert request_rereview.loop_request_times("acme/widget", 159, review_vendors.COPILOT) == [at]

    def test_a_lagging_timeline_keeps_the_marker(self, monkeypatch, capsys, tmp_path):
        """GraphQL can show the request pending before the REST timeline does.
        Not-yet-visible provenance is not absence: the marker stays until the
        request is seen or the settle logic proves it never landed (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._seed_marker(seconds_ago=10, kind="accepted")
        self._stub_reads(monkeypatch, state="PENDING", pending_since=T_REQUEST)
        monkeypatch.setattr(request_rereview, "request_landed_since", lambda *a, **k: False)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["status"] == "review_pending"
        assert self._marker() is not None
        assert request_rereview.loop_request_times("acme/widget", 159, review_vendors.COPILOT) is None

    def test_settling_a_landed_request_rereads_the_state_before_posting(self, monkeypatch, capsys, tmp_path):
        """The first snapshot said ABSENT, but the earlier request became
        visible while it was being settled. Posting on the stale snapshot is
        the duplicate the state machine exists to prevent (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._seed_marker(seconds_ago=600, kind="maybe")
        self._stub_reads(monkeypatch)
        snapshots = iter(["ABSENT", "PENDING"])
        monkeypatch.setattr(
            request_rereview, "request_state",
            lambda *a, **k: {"state": next(snapshots), "head": "abc1234def", "pending_since": T_REQUEST,
                             "first_completed_review_at": T_FIRST_REVIEW},
        )
        monkeypatch.setattr(request_rereview, "request_landed_since", lambda *a, **k: True)
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("now pending: must not post"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["status"] == "review_pending"
        assert self._marker() is None

    def test_a_registered_request_is_posted_and_stamped_once(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))
        seen = {}
        monkeypatch.setattr(
            request_rereview, "request_registered",
            lambda repo, pr, vendor, requested_at, **k: seen.update(requested_at=requested_at) or True,
        )

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["posted"] is True and payload["registered"] is True
        assert seen["requested_at"] == CREATED_AT
        assert self._stamped(tmp_path) == 1

    def test_an_unverifiable_registration_is_uncertain_and_not_stamped(self, monkeypatch, capsys, tmp_path):
        # GitHub answered 201 and the read-back failed: whether Copilot was
        # added is unknown. Stamping would record a request that may not exist
        # (cap drift); claiming nothing was posted would invite a duplicate.
        # Report unknown, record nothing, and let the next state read decide
        # (review_pending if it landed, absent if GitHub dropped it).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))
        monkeypatch.setattr(request_rereview, "request_registered", lambda *a, **k: None)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "review_request_uncertain"
        assert payload["reason"] == "registration_unknown"
        assert payload["http_status"] == 201
        assert payload["posted"] is None and payload["outcome"] == "unknown"
        assert payload["requested_at"] == CREATED_AT
        assert "review_pending" in payload["message"]
        assert self._stamped(tmp_path) == 0

    def test_dry_run_never_checks_registration(self, monkeypatch, capsys):
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))
        monkeypatch.setattr(request_rereview, "request_registered", lambda *a, **k: pytest.fail("dry run posted nothing to confirm"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--dry-run", "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["would_post"] is True

    def test_rate_limit_is_retryable(self, monkeypatch, capsys):
        self._stub_reads(monkeypatch)

        def rejecting(*a, **k):
            raise request_rereview.ReviewRequestRejected("secondary rate limit", reason="rate_limited", http_status=403, retryable=True)

        monkeypatch.setattr(request_rereview, "request_review", rejecting)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["reason"] == "rate_limited" and payload["retryable"] is True

    def test_uncertain_failure_confirmed_by_timeline_is_a_success_and_stamped_once(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)

        def rejecting(*a, **k):
            raise request_rereview.ReviewRequestRejected("connection reset", reason="uncertain", http_status=None, retryable=False, requested_at=CREATED_AT)

        monkeypatch.setattr(request_rereview, "request_review", rejecting)
        monkeypatch.setattr(request_rereview, "confirm_posted_after_uncertain", lambda *a, **k: True)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["kind"] == "review_request"
        assert payload["confirmed_after_uncertain"] is True
        assert payload["created_at"] == CREATED_AT
        assert self._stamped(tmp_path) == 1

    def test_uncertain_failure_without_event_is_not_retryable_and_not_stamped(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)

        def rejecting(*a, **k):
            raise request_rereview.ReviewRequestRejected("connection reset", reason="uncertain", http_status=None, retryable=False, requested_at=CREATED_AT)

        monkeypatch.setattr(request_rereview, "request_review", rejecting)
        monkeypatch.setattr(request_rereview, "confirm_posted_after_uncertain", lambda *a, **k: False)

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "review_request_uncertain"
        assert payload["reason"] == "uncertain"
        assert payload["retryable"] is False
        # The POST may have landed before the connection died: claiming
        # nothing was posted invites the duplicate request this state machine
        # exists to prevent (PR #116 review).
        assert payload["posted"] is None
        assert payload["outcome"] == "unknown"
        assert "Nothing was posted" not in payload["message"]
        assert "unknown" in payload["message"].lower()
        assert self._stamped(tmp_path) == 0

    def test_cap_applies_to_review_requests_too(self, monkeypatch, capsys):
        self._stub_reads(monkeypatch, used=3)
        monkeypatch.setattr(request_rereview, "effective_cap", lambda *a: 3)
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("capped"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "capped" and payload["rereviews_used"] == 3

    def test_uncountable_review_requests_refuse_to_post(self, monkeypatch, capsys):
        self._stub_reads(monkeypatch, used=None)
        monkeypatch.setattr(request_rereview, "request_review", lambda *a, **k: pytest.fail("uncountable"))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        assert json.loads(capsys.readouterr().out)["status"] == "uncountable_count"

    def test_mention_passed_for_copilot_is_ignored_with_a_warning(self, monkeypatch, capsys):
        self._stub_reads(monkeypatch)
        requested = {}
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request(requested))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--review-trigger-mention", "@copilot", "--json"]) == 0

        out = capsys.readouterr()
        assert "ignoring" in out.err
        assert json.loads(out.out)["kind"] == "review_request"
        assert requested["login"] == COPILOT_REQUEST

    def test_dry_run_previews_and_stamps_nothing(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--dry-run", "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True and payload["posted"] is False
        assert payload["would_post"] is True and payload["state"] == "ABSENT"
        assert self._stamped(tmp_path) == 0

    def test_review_request_stamps_the_cycle_transition(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))

        assert self._run(["--reviewer-login", COPILOT_LOGIN, "--json"]) == 0

        capsys.readouterr()
        assert self._stamped(tmp_path) == 1

    def test_human_mode_names_the_reviewer_and_the_timestamp(self, monkeypatch, capsys):
        self._stub_reads(monkeypatch)
        monkeypatch.setattr(request_rereview, "request_review", self._fake_request({}))

        assert self._run(["--reviewer-login", COPILOT_LOGIN]) == 0

        out = capsys.readouterr().out
        assert "[loop]" in out and "Copilot" in out and CREATED_AT in out


class TestExplicitMentionOverridesPersistedVendor:
    """An explicit mention is a per-run override; persisted state must not win.

    Both directions were broken: persisted Copilot ignored `@codex` and sent a
    review request, and persisted Codex answered `@copilot` with a comment
    Copilot never reads (PR #116 review).
    """

    @staticmethod
    def _persist(tmp_path, monkeypatch, login):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        request_rereview.state_path().write_text(
            json.dumps({"acme/widget#159": {"reviewer": {"login": login}}}), encoding="utf-8"
        )

    def test_codex_mention_beats_persisted_copilot_and_posts_a_comment(self, monkeypatch, capsys, tmp_path):
        self._persist(tmp_path, monkeypatch, COPILOT_LOGIN)
        posted = {}

        def fake_post(repo, pr, phrase, **kwargs):
            posted["phrase"] = phrase
            return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

        monkeypatch.setattr(request_rereview, "post_rereview", fake_post)
        monkeypatch.setattr(
            request_rereview, "request_review",
            lambda *a, **k: pytest.fail("an explicit @codex mention must not request a Copilot review"),
        )

        rc = request_rereview.main([
            "--repo", "acme/widget", "--pr", "159", "--review-trigger-mention", "@codex", "--json",
        ])

        assert rc == 0
        assert posted["phrase"] == review_vendors.CODEX.rereview_phrase
        assert json.loads(capsys.readouterr().out)["created_at"] == CREATED_AT

    def test_copilot_mention_beats_persisted_codex_and_requests_a_review(self, monkeypatch, capsys, tmp_path):
        self._persist(tmp_path, monkeypatch, review_vendors.CODEX.login)
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: "agent")
        monkeypatch.setattr(request_rereview, "count_agent_review_requests", lambda *a, **k: 0)
        monkeypatch.setattr(request_rereview, "request_state", lambda *a, **k: {
            "state": "ABSENT", "head": "abc1234def", "pending_since": None,
            "first_completed_review_at": T_FIRST_REVIEW,
        })
        monkeypatch.setattr(
            request_rereview, "post_rereview",
            lambda *a, **k: pytest.fail("an explicit @copilot mention must not post a comment"),
        )
        requested = {}
        monkeypatch.setattr(request_rereview, "request_registered", lambda *a, **k: True)
        monkeypatch.setattr(request_rereview, "request_review", lambda repo, pr, login, **k: (
            requested.update(login=login)
            or {"created_at": CREATED_AT, "repo": repo, "pr": pr, "kind": "review_request",
                "reviewer": login, "posted": True}
        ))

        rc = request_rereview.main([
            "--repo", "acme/widget", "--pr", "159", "--review-trigger-mention", "@copilot", "--json",
        ])

        assert rc == 0
        assert requested["login"] == COPILOT_REQUEST
        assert json.loads(capsys.readouterr().out)["kind"] == "review_request"


class TestUncertainPayload:
    def test_an_uncertain_outcome_is_reported_as_unknown_not_unposted(self):
        exc = request_rereview.ReviewRequestRejected(
            "connection reset by peer", reason="uncertain", requested_at=T_REQUEST
        )

        payload = request_rereview.rejected_payload("acme/widget", 159, COPILOT_REQUEST, exc)

        assert payload["status"] == "review_request_uncertain"
        assert payload["posted"] is None
        assert payload["outcome"] == "unknown"
        assert payload["requested_at"] == T_REQUEST
        assert "Nothing was posted" not in payload["message"]
        assert "unknown" in payload["message"].lower()

    def test_a_definite_rejection_still_says_nothing_was_posted(self):
        exc = request_rereview.ReviewRequestRejected(
            "Copilot code review is not enabled", reason="validation", http_status=422
        )

        payload = request_rereview.rejected_payload("acme/widget", 159, COPILOT_REQUEST, exc)

        assert payload["status"] == "review_request_rejected"
        assert payload["posted"] is False
        assert "Nothing was posted" in payload["message"]


class TestCancelReviewRequest:
    def test_cancel_uses_the_delete_endpoint(self):
        captured = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = cmd
            return _completed(cmd, {"number": 159})

        assert request_rereview.cancel_review_request(
            "acme/widget", 159, COPILOT_REQUEST, runner=runner
        ) is True
        assert captured["cmd"] == [
            "gh", "api", "repos/acme/widget/pulls/159/requested_reviewers",
            "--method", "DELETE", "--raw-field", f"reviewers[]={COPILOT_REQUEST}",
        ]

    def test_cancel_reports_failure_instead_of_raising(self):
        failing = lambda cmd, **kwargs: _completed(cmd, None, rc=1, stderr="gh: Not Found (HTTP 404)")  # noqa: E731

        assert request_rereview.cancel_review_request(
            "acme/widget", 159, COPILOT_REQUEST, runner=failing
        ) is False


class TestRequestProvenance:
    """The cap bounds what the loop posts, not what the member clicks.

    Timeline actor identity cannot tell the two apart — the loop uses the
    member's own `gh` auth — so three manual Copilot requests used to make a
    first loop run stop as `capped` (PR #116 review). Loop-issued requests are
    therefore recorded locally at the moment they are posted, and the cap counts
    those.
    """

    def test_posting_records_the_request_locally(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

        request_rereview.record_rereview_posted(
            "acme/widget", 159, reviewer=COPILOT_LOGIN, requested_at=T_REQUEST
        )

        recorded = request_rereview.loop_request_times("acme/widget", 159, review_vendors.COPILOT)
        assert recorded == [T_REQUEST]
        # A comment vendor's pings are recorded against that vendor only.
        assert request_rereview.loop_request_times("acme/widget", 159, review_vendors.CODEX) == []

    def test_requests_with_no_local_record_are_uncountable_not_free(self, monkeypatch, tmp_path):
        """Requests exist under this login but the loop has no record of
        posting them: they may be the member's clicks or the loop's own after a
        cleared state dir. Charging them to the loop stops a first run as
        capped; calling them zero lets every pushed head post another request
        past the cap. Neither is a guess worth making, so the count is unknown
        and the caller refuses to post (PR #116 review)."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        manual = _rest_request_events([
            ("agent", "2026-10-07T16:00:00Z"),
            ("agent", "2026-10-07T16:05:00Z"),
            ("agent", "2026-10-07T16:10:00Z"),
        ])

        def runner(cmd, **kwargs):
            assert cmd[3].endswith("/timeline")
            return _completed(cmd, manual)

        assert request_rereview.count_agent_review_requests(
            "acme/widget", 159, review_vendors.COPILOT, "agent", T_FIRST_REVIEW, runner=runner
        ) is None

    def test_no_record_and_no_requests_is_a_verified_zero(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

        def runner(cmd, **kwargs):
            return _completed(cmd, _rest_request_events([("a-human", "2026-10-07T16:30:00Z")]))

        assert request_rereview.count_agent_review_requests(
            "acme/widget", 159, review_vendors.COPILOT, "agent", T_FIRST_REVIEW, runner=runner
        ) == 0

    def test_a_recorded_loop_request_is_counted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        request_rereview.record_rereview_posted(
            "acme/widget", 159, reviewer=COPILOT_LOGIN, requested_at="2026-10-07T16:05:00Z"
        )
        events = _rest_request_events([
            ("agent", "2026-10-07T16:00:00Z"),   # manual
            ("agent", "2026-10-07T16:05:02Z"),   # the loop's own, two seconds later
            ("agent", "2026-10-07T16:10:00Z"),   # manual
        ])

        def runner(cmd, **kwargs):
            return _completed(cmd, events)

        used = request_rereview.count_agent_review_requests(
            "acme/widget", 159, review_vendors.COPILOT, "agent", T_FIRST_REVIEW, runner=runner
        )

        assert used == 1


class TestStateReadFailure:
    def test_an_unreadable_state_does_not_advise_the_cap_bypass(self, monkeypatch, capsys):
        """`--no-cap-check` is consulted after this branch, so the advised retry
        takes the same path and still posts nothing (PR #116 review)."""
        monkeypatch.setattr(request_rereview, "request_state", lambda *a, **k: None)
        monkeypatch.setattr(
            request_rereview, "request_review",
            lambda *a, **k: pytest.fail("an unreadable state must not post"),
        )

        rc = request_rereview.main([
            "--repo", "acme/widget", "--pr", "159", "--reviewer-login", COPILOT_LOGIN, "--json",
        ])

        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["status"] == "uncountable_state"
        assert payload["posted"] is False
        assert "--no-cap-check" not in payload["message"]
        assert "could not be read" in payload["message"].lower()


class TestFreeInitialRequest:
    """Cycle 0 is free, so it must not be gated on the cap or on the reads the
    cap needs: with `max_rereview_requests: 0`, or with `gh api user` failing,
    a PR Copilot has never reviewed still gets its first request (PR #116
    review)."""

    @staticmethod
    def _state(**overrides):
        state = {
            "state": "ABSENT", "head": "abc1234def", "pending_since": None,
            "first_completed_review_at": None,
        }
        state.update(overrides)
        return state

    def test_a_cap_of_zero_still_allows_the_first_request(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(request_rereview, "request_state", lambda *a, **k: self._state())
        monkeypatch.setattr(request_rereview, "effective_cap", lambda *a: 0)
        monkeypatch.setattr(
            request_rereview, "count_agent_review_requests",
            lambda *a, **k: pytest.fail("the free initial request needs no cap accounting"),
        )
        requested = {}
        monkeypatch.setattr(request_rereview, "request_registered", lambda *a, **k: True)
        monkeypatch.setattr(request_rereview, "request_review", lambda repo, pr, login, **k: (
            requested.update(login=login)
            or {"created_at": CREATED_AT, "repo": repo, "pr": pr, "kind": "review_request",
                "reviewer": login, "posted": True}
        ))

        rc = request_rereview.main([
            "--repo", "acme/widget", "--pr", "159", "--reviewer-login", COPILOT_LOGIN, "--json",
        ])

        assert rc == 0
        assert requested["login"] == COPILOT_REQUEST
        assert json.loads(capsys.readouterr().out)["kind"] == "review_request"

    def test_an_unresolvable_login_does_not_block_the_first_request(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(request_rereview, "request_state", lambda *a, **k: self._state())
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: None)
        monkeypatch.setattr(request_rereview, "request_registered", lambda *a, **k: True)
        monkeypatch.setattr(request_rereview, "request_review", lambda repo, pr, login, **k: {
            "created_at": CREATED_AT, "repo": repo, "pr": pr, "kind": "review_request",
            "reviewer": login, "posted": True,
        })

        assert request_rereview.main([
            "--repo", "acme/widget", "--pr", "159", "--reviewer-login", COPILOT_LOGIN, "--json",
        ]) == 0
        assert json.loads(capsys.readouterr().out)["posted"] is True

    def test_once_a_review_exists_the_cap_applies_again(self, monkeypatch, capsys):
        monkeypatch.setattr(
            request_rereview, "request_state",
            lambda *a, **k: self._state(first_completed_review_at=T_FIRST_REVIEW),
        )
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: "agent")
        monkeypatch.setattr(request_rereview, "count_agent_review_requests", lambda *a, **k: 3)
        monkeypatch.setattr(request_rereview, "effective_cap", lambda *a: 3)
        monkeypatch.setattr(
            request_rereview, "request_review",
            lambda *a, **k: pytest.fail("capped"),
        )

        assert request_rereview.main([
            "--repo", "acme/widget", "--pr", "159", "--reviewer-login", COPILOT_LOGIN, "--json",
        ]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "capped"


class TestExpectHeadOnTheCommentPath:
    """`--expect-head` guarded only the review-request path, so a Codex ping
    could land before the push was visible and buy a review of the previous
    head (sibling of the PR #116 --expect-head finding)."""

    def test_a_stale_head_posts_no_comment(self, monkeypatch, capsys):
        monkeypatch.setattr(request_rereview, "current_head", lambda *a, **k: "oldhead000")
        monkeypatch.setattr(
            request_rereview, "post_rereview",
            lambda *a, **k: pytest.fail("the push has not landed; nothing may be posted"),
        )

        rc = request_rereview.main([
            "--repo", "acme/widget", "--pr", "159",
            "--reviewer-mention", "@codex", "--expect-head", "newhead111", "--json",
        ])

        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["status"] == "head_mismatch"
        assert payload["posted"] is False

    def test_the_matching_head_posts_normally(self, monkeypatch, capsys):
        monkeypatch.setattr(request_rereview, "current_head", lambda *a, **k: "newhead111")
        posted = {}
        monkeypatch.setattr(request_rereview, "post_rereview", lambda repo, pr, phrase, **k: (
            posted.update(phrase=phrase)
            or {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}
        ))

        assert request_rereview.main([
            "--repo", "acme/widget", "--pr", "159",
            "--reviewer-mention", "@codex", "--expect-head", "newhead111", "--json",
        ]) == 0
        assert posted["phrase"] == review_vendors.CODEX.rereview_phrase

    def test_an_unreadable_head_posts_nothing_rather_than_guessing(self, monkeypatch, capsys):
        monkeypatch.setattr(request_rereview, "current_head", lambda *a, **k: None)
        monkeypatch.setattr(
            request_rereview, "post_rereview",
            lambda *a, **k: pytest.fail("an unverifiable head must not post"),
        )

        assert request_rereview.main([
            "--repo", "acme/widget", "--pr", "159",
            "--reviewer-mention", "@codex", "--expect-head", "newhead111", "--json",
        ]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "uncountable_state"

    def test_without_expect_head_no_extra_read_happens(self, monkeypatch, capsys):
        monkeypatch.setattr(
            request_rereview, "current_head",
            lambda *a, **k: pytest.fail("no head read when --expect-head is absent"),
        )
        monkeypatch.setattr(request_rereview, "post_rereview", lambda repo, pr, phrase, **k: {
            "created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase,
        })

        assert request_rereview.main([
            "--repo", "acme/widget", "--pr", "159", "--reviewer-mention", "@codex", "--json",
        ]) == 0
        assert json.loads(capsys.readouterr().out)["created_at"] == CREATED_AT


class TestCurrentHead:
    """`gh api --jq .head.sha` prints a bare sha, which is not JSON — the first
    version of this helper fed it to json.loads and always reported the head as
    unreadable. Found by running it for real, not by the stubbed tests."""

    def test_the_head_is_read_from_the_pr_object(self):
        captured = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = cmd
            return _completed(cmd, {"number": 159, "head": {"sha": "abc1234def"}})

        assert request_rereview.current_head("acme/widget", 159, runner=runner) == "abc1234def"
        assert "--jq" not in captured["cmd"]
        assert captured["cmd"][:3] == ["gh", "api", "repos/acme/widget/pulls/159"]

    def test_an_unreadable_response_is_none(self):
        for result in (None, {}, {"head": {}}, {"head": {"sha": ""}}, "not json"):
            def runner(cmd, _result=result, **kwargs):
                if _result == "not json":
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="abc123\n", stderr="")
                return _completed(cmd, _result)

            assert request_rereview.current_head("acme/widget", 159, runner=runner) is None

    def test_a_failed_call_is_none(self):
        failing = lambda cmd, **kwargs: _completed(cmd, None, rc=1, stderr="boom")  # noqa: E731
        assert request_rereview.current_head("acme/widget", 159, runner=failing) is None


class TestProvenanceFailsClosed:
    """An empty provenance list is authoritative to the counter, so a missing
    or unwritable record must read as *unknown* (None) and fall back to the
    actor filter — otherwise every pushed head could post another request past
    the hard cap (PR #116 review)."""

    def test_no_state_file_is_unknown_not_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

        assert request_rereview.loop_request_times(
            "acme/widget", 159, review_vendors.COPILOT
        ) is None

    def test_a_pr_entry_without_the_key_is_unknown(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        request_rereview.state_path().write_text(
            json.dumps({"acme/widget#159": {"run": {"session_cycle": 2}}}), encoding="utf-8"
        )

        assert request_rereview.loop_request_times(
            "acme/widget", 159, review_vendors.COPILOT
        ) is None

    def test_a_recorded_history_with_no_match_for_this_vendor_is_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        request_rereview.record_rereview_posted(
            "acme/widget", 159, reviewer=review_vendors.CODEX.login, requested_at=T_REQUEST
        )

        assert request_rereview.loop_request_times(
            "acme/widget", 159, review_vendors.COPILOT
        ) == []

    def test_a_failed_log_append_marks_the_history_unusable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        request_rereview.record_rereview_posted(
            "acme/widget", 159, reviewer=COPILOT_LOGIN, requested_at=T_REQUEST
        )
        assert request_rereview.loop_request_times(
            "acme/widget", 159, review_vendors.COPILOT
        ) == [T_REQUEST]

        # The next POST lands, but appending its record fails.
        monkeypatch.setattr(request_rereview.loop_state, "append_request_log", lambda *a, **k: False)
        request_rereview.record_rereview_posted(
            "acme/widget", 159, reviewer=COPILOT_LOGIN, requested_at="2026-10-07T17:00:00Z"
        )

        # One request is now unrecorded, so the history can no longer be
        # trusted as complete: unknown, not "one request".
        assert request_rereview.loop_request_times(
            "acme/widget", 159, review_vendors.COPILOT
        ) is None

    def test_a_failed_state_write_does_not_lose_provenance(self, monkeypatch, tmp_path):
        """The cap's records live in the append-only log, so a failed
        state.json write (the cycle ordinal) leaves the history complete."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

        def fail(self, *args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(request_rereview.Path, "write_text", fail)
        request_rereview.record_rereview_posted(
            "acme/widget", 159, reviewer=COPILOT_LOGIN, requested_at=T_REQUEST
        )
        monkeypatch.undo()
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

        assert request_rereview.loop_request_times(
            "acme/widget", 159, review_vendors.COPILOT
        ) == [T_REQUEST]

    def test_unknown_provenance_with_candidate_events_refuses_to_count(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        events = _rest_request_events([("agent", T_REQUEST), ("a-human", "2026-10-07T16:30:00Z")])

        def runner(cmd, **kwargs):
            return _completed(cmd, events)

        assert request_rereview.count_agent_review_requests(
            "acme/widget", 159, review_vendors.COPILOT, "agent", T_FIRST_REVIEW, runner=runner
        ) is None


class TestRequestRegistered:
    """After a 201, did GitHub actually add the reviewer?

    Observed live (2026-10-10): on an account without Copilot code review the
    POST for `copilot-pull-request-reviewer[bot]` (and `Copilot`, `copilot`)
    returns 201 with `requested_reviewers: []`, and the PR gains no pending
    request and no timeline event. Only GraphQL `reviewRequests` (or the
    review itself landing on the head) proves registration.
    """

    REQUESTED_AT = "2026-10-10T16:40:01Z"

    def _check(self, runner, sleeps=None, attempts=3):
        return request_rereview.request_registered(
            "acme/widget", 159, review_vendors.COPILOT, self.REQUESTED_AT,
            runner=runner, attempts=attempts,
            sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        )

    def test_true_when_graphql_lists_the_vendor_as_requested(self):
        sleeps = []
        assert self._check(_state_runner(_gql_pr("54845f0400", pending=[COPILOT_LOGIN])), sleeps) is True
        assert sleeps == []

    def test_false_after_retries_when_nothing_is_registered(self):
        sleeps = []
        assert self._check(_state_runner(_gql_pr("54845f0400")), sleeps, attempts=3) is False
        # Registration is read back three times a second apart before giving up.
        assert len(sleeps) == 2 and all(s >= 1 for s in sleeps)

    def test_true_when_the_review_already_landed_on_the_head(self):
        # Copilot's fastest observed turnaround is a few seconds; a review on
        # the head submitted at or after the request is the request succeeding.
        runner = _state_runner(
            _gql_pr("54845f0400"),
            reviews=[(COPILOT_LOGIN, "COMMENTED", "2026-10-10T16:40:05Z", "54845f0400")],
        )
        assert self._check(runner) is True

    def test_an_older_review_of_the_head_does_not_count(self):
        runner = _state_runner(
            _gql_pr("54845f0400"),
            reviews=[(COPILOT_LOGIN, "COMMENTED", "2026-10-10T16:39:00Z", "54845f0400")],
        )
        assert self._check(runner) is False

    def test_none_when_the_read_fails(self):
        assert self._check(_state_runner(None, rc=1)) is None
