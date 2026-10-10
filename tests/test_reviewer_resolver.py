"""Tests for reviewer selection/discovery helpers."""

from __future__ import annotations

import reviewer_resolver


def _thread(*comments):
    return {
        "comments": {
            "nodes": list(comments),
        },
    }


def _comment(login, typename="Bot", body="review note"):
    return {
        "author": {
            "login": login,
            "__typename": typename,
        },
        "body": body,
    }


def _pr(*threads):
    return {
        "reviewThreads": {
            "nodes": list(threads),
        },
    }


def test_discovers_distinct_bot_comment_authors_in_first_thread_order():
    pull_request = _pr(
        _thread(_comment("coderabbitai"), _comment("alice", "User")),
        _thread(_comment("gemini-code-assist")),
        _thread(_comment("coderabbitai")),
    )

    candidates = reviewer_resolver.discover_candidates(
        pull_request,
        self_login="codex-agent",
    )

    assert [candidate.login for candidate in candidates] == [
        "coderabbitai",
        "gemini-code-assist",
    ]
    assert candidates[1].display_name == "Gemini Code Assist"
    assert candidates[1].review_trigger == "@gemini-code-assist"


def test_sorts_same_thread_candidates_by_login():
    pull_request = _pr(
        _thread(_comment("zeta-bot"), _comment("alpha-bot")),
        _thread(_comment("middle-bot")),
    )

    candidates = reviewer_resolver.discover_candidates(
        pull_request,
        self_login="codex-agent",
    )

    assert [candidate.login for candidate in candidates] == [
        "alpha-bot",
        "zeta-bot",
        "middle-bot",
    ]


def test_excludes_humans_and_self_login():
    pull_request = _pr(
        _thread(_comment("codex-agent"), _comment("alice", "User")),
        _thread(_comment("qodo-merge-pro")),
    )

    candidates = reviewer_resolver.discover_candidates(
        pull_request,
        self_login="codex-agent",
    )

    assert [candidate.login for candidate in candidates] == ["qodo-merge-pro"]


def test_accepts_bot_suffix_when_typename_is_absent():
    pull_request = _pr(
        _thread({"author": {"login": "github-actions[bot]"}, "body": "review"}),
        _thread({"author": {"login": "plain-human"}, "body": "review"}),
    )

    candidates = reviewer_resolver.discover_candidates(
        pull_request,
        self_login=None,
    )

    assert [candidate.login for candidate in candidates] == ["github-actions[bot]"]
    assert candidates[0].review_trigger is None


def test_accepts_bot_suffix_even_when_typename_is_user():
    pull_request = _pr(
        _thread(_comment("renovate[bot]", "User")),
    )

    candidates = reviewer_resolver.discover_candidates(
        pull_request,
        self_login=None,
    )

    assert [candidate.login for candidate in candidates] == ["renovate[bot]"]


def test_make_reviewer_record_fills_known_defaults_and_timestamp():
    record = reviewer_resolver.make_reviewer_record(
        "gemini-code-assist",
        source="confirmed",
        selected_at="2026-06-28T12:00:00Z",
    )

    assert record == {
        "login": "gemini-code-assist",
        "display_name": "Gemini Code Assist",
        "review_trigger": "@gemini-code-assist",
        "trigger_kind": "comment",
        "request_login": None,
        "auto_reviews": True,
        "source": "confirmed",
        "selected_at": "2026-06-28T12:00:00Z",
    }


def test_discovered_codex_gets_its_vendor_name_and_trigger():
    pull_request = _pr(_thread(_comment("chatgpt-codex-connector")))

    candidates = reviewer_resolver.discover_candidates(pull_request, self_login=None)

    assert candidates[0].display_name == "Codex"
    assert candidates[0].review_trigger == "@codex"


def test_trigger_for_knows_codex():
    assert reviewer_resolver.trigger_for("chatgpt-codex-connector") == "@codex"


def test_phrase_for_returns_the_exact_vendor_phrase():
    assert reviewer_resolver.phrase_for("chatgpt-codex-connector") == "@codex review"
    assert (
        reviewer_resolver.phrase_for("gemini-code-assist")
        == "@gemini-code-assist please review the latest changes."
    )


def test_phrase_for_unknown_reviewer_is_none():
    assert reviewer_resolver.phrase_for("coderabbitai") is None


def test_make_reviewer_record_marks_codex_as_not_self_starting():
    record = reviewer_resolver.make_reviewer_record(
        "chatgpt-codex-connector",
        source="confirmed",
        selected_at="2026-08-04T12:00:00Z",
    )

    assert record["auto_reviews"] is False
    assert record["display_name"] == "Codex"
    assert record["review_trigger"] == "@codex"


def test_unknown_reviewers_are_assumed_to_review_on_their_own():
    record = reviewer_resolver.make_reviewer_record(
        "coderabbitai",
        source="confirmed",
        selected_at="2026-08-04T12:00:00Z",
    )

    assert record["auto_reviews"] is True


COPILOT = "copilot-pull-request-reviewer"


def test_copilot_has_no_mention_trigger_but_a_request_login():
    assert reviewer_resolver.trigger_for(COPILOT) is None
    assert reviewer_resolver.phrase_for(COPILOT) is None
    assert reviewer_resolver.trigger_kind_for(COPILOT) == "review_request"
    assert reviewer_resolver.request_login_for(COPILOT) == "copilot-pull-request-reviewer[bot]"


def test_unknown_and_comment_vendors_keep_the_comment_kind():
    assert reviewer_resolver.trigger_kind_for("coderabbitai") == "comment"
    assert reviewer_resolver.request_login_for("coderabbitai") is None
    assert reviewer_resolver.trigger_kind_for("chatgpt-codex-connector") == "comment"
    assert reviewer_resolver.trigger_for("chatgpt-codex-connector") == "@codex"


def test_copilot_record_carries_kind_and_request_login():
    record = reviewer_resolver.make_reviewer_record(COPILOT, source="confirmed")

    assert record["display_name"] == "Copilot"
    assert record["review_trigger"] is None
    assert record["trigger_kind"] == "review_request"
    assert record["request_login"] == "copilot-pull-request-reviewer[bot]"
    assert record["auto_reviews"] is False


def test_codex_record_still_carries_its_mention_and_the_comment_kind():
    record = reviewer_resolver.make_reviewer_record("chatgpt-codex-connector", source="confirmed")

    assert record["review_trigger"] == "@codex"
    assert record["trigger_kind"] == "comment"
    assert record["request_login"] is None


def test_candidate_dict_carries_kind_and_source():
    candidate = reviewer_resolver.Candidate(
        login=COPILOT,
        display_name="Copilot",
        review_trigger=None,
        trigger_kind="review_request",
        source="requested",
    )

    assert candidate.to_dict() == {
        "login": COPILOT,
        "display_name": "Copilot",
        "review_trigger": None,
        "trigger_kind": "review_request",
        "source": "requested",
    }


import review_vendors  # noqa: E402


def _gql_event(actor, created_at, reviewer=COPILOT):
    return {
        "__typename": "ReviewRequestedEvent",
        "createdAt": created_at,
        "actor": {"login": actor},
        "requestedReviewer": {"__typename": "Bot", "login": reviewer},
    }


def _rest_event(actor, created_at, reviewer="Copilot"):
    return {
        "event": "review_requested",
        "created_at": created_at,
        "actor": {"login": actor},
        "requested_reviewer": {"login": reviewer, "type": "Bot"},
    }


def test_normalize_request_event_accepts_graphql_and_rest_shapes():
    assert reviewer_resolver.normalize_request_event(_gql_event("agent", "2026-10-07T16:20:05Z")) == {
        "created_at": "2026-10-07T16:20:05Z", "actor_login": "agent", "reviewer_login": COPILOT,
    }
    assert reviewer_resolver.normalize_request_event(_rest_event("agent", "2026-10-07T16:20:05Z")) == {
        "created_at": "2026-10-07T16:20:05Z", "actor_login": "agent", "reviewer_login": "Copilot",
    }


def test_normalize_request_event_rejects_team_requests_and_other_events():
    assert reviewer_resolver.normalize_request_event(
        {"event": "review_requested", "actor": {"login": "a"}, "requested_team": {"slug": "core"},
         "created_at": "2026-10-07T16:20:05Z"}
    ) is None
    assert reviewer_resolver.normalize_request_event({"event": "commented", "actor": {"login": "a"}}) is None
    assert reviewer_resolver.normalize_request_event({"__typename": "IssueComment"}) is None
    assert reviewer_resolver.normalize_request_event("nope") is None
    assert reviewer_resolver.normalize_request_event(None) is None


def test_first_completed_review_at_ignores_pending_and_other_authors():
    reviews = [
        {"author": {"login": "alice"}, "state": "APPROVED", "submittedAt": "2026-10-07T15:00:00Z"},
        {"author": {"login": COPILOT}, "state": "PENDING", "submittedAt": "2026-10-07T15:10:00Z"},
        {"author": {"login": COPILOT}, "state": "COMMENTED", "submittedAt": "2026-10-07T15:46:14Z"},
        {"user": {"login": "copilot-pull-request-reviewer[bot]"}, "state": "COMMENTED",
         "submitted_at": "2026-10-07T15:30:00Z"},
    ]

    assert reviewer_resolver.first_completed_review_at(reviews, review_vendors.COPILOT) == "2026-10-07T15:30:00Z"
    assert reviewer_resolver.first_completed_review_at(reviews[:2], review_vendors.COPILOT) is None
    assert reviewer_resolver.first_completed_review_at([], review_vendors.COPILOT) is None


def test_counted_requests_exclude_initial_human_and_other_vendor():
    events = [
        _gql_event("agent", "2026-10-07T15:42:27Z"),          # initial request: before the first review
        _rest_event("agent", "2026-10-07T16:20:05Z"),         # re-request: counts (REST spelling)
        _gql_event("a-human", "2026-10-07T16:30:00Z"),        # human: never counts
        _gql_event("agent", "2026-10-07T16:40:00Z", reviewer="gemini-code-assist"),  # other vendor
        _gql_event("agent", "2026-10-07T17:00:00Z"),          # counts
    ]

    counted = reviewer_resolver.counted_review_requests(
        events, agent_login="agent", vendor=review_vendors.COPILOT,
        first_completed_review_at="2026-10-07T15:46:14Z",
    )

    assert [e["created_at"] for e in counted] == ["2026-10-07T16:20:05Z", "2026-10-07T17:00:00Z"]
    everyone = reviewer_resolver.counted_review_requests(
        events, agent_login=None, vendor=review_vendors.COPILOT,
        first_completed_review_at="2026-10-07T15:46:14Z",
    )
    assert len(everyone) == 3


def test_counted_requests_is_empty_before_any_completed_review():
    events = [_gql_event("agent", "2026-10-07T15:42:27Z"), _gql_event("agent", "2026-10-07T15:50:00Z")]

    assert reviewer_resolver.counted_review_requests(
        events, agent_login="agent", vendor=review_vendors.COPILOT, first_completed_review_at=None,
    ) == []
