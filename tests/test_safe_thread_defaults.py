import pytest

import fetch_gemini_threads as fgt
from run_profile import run_profile


def thread(*comments):
    return {"comments": {"nodes": list(comments)}}


BOT = {"author": {"login": "coderabbitai", "__typename": "Bot"}, "body": "Please fix this", "createdAt": "2026-10-15T10:00:00Z"}


def reply(body, association="COLLABORATOR", kind="User"):
    return {"author": {"login": "alice", "__typename": kind}, "authorAssociation": association, "body": body, "createdAt": "2026-10-15T11:00:00Z"}


@pytest.mark.parametrize("body", ["This is still broken; please fix the reported problem.", "> /review-loop defer", "Some text\n/review-loop defer", "`/review-loop defer`", "\n/review-loop defer", "  /review-loop defer"])
def test_ordinary_or_quoted_replies_do_not_defer(body):
    assert not fgt.is_addressed_by_reply(thread(BOT, reply(body)), "coderabbitai")


@pytest.mark.parametrize("association", ["MEMBER", "CONTRIBUTOR", "NONE", None])
def test_marker_without_repository_write_association_does_not_defer(association):
    assert not fgt.is_addressed_by_reply(thread(BOT, reply("/review-loop defer", association)), "coderabbitai")


@pytest.mark.parametrize("association", ["OWNER", "COLLABORATOR"])
def test_explicit_marker_after_review_defers(association):
    assert fgt.is_addressed_by_reply(thread(BOT, reply("/review-loop defer\nExplanation", association)), "coderabbitai")


def test_stale_marker_before_a_new_bot_finding_does_not_defer():
    assert not fgt.is_addressed_by_reply(thread(reply("/review-loop defer"), BOT), "coderabbitai")
    assert not fgt.is_addressed_by_reply(thread(BOT, reply("/review-loop defer"), BOT), "coderabbitai")
    assert not fgt.is_addressed_by_reply(thread(BOT, reply("/review-loop defer", kind="Bot")), "coderabbitai")


def test_later_maintainer_objection_reopens_the_finding():
    assert not fgt.is_addressed_by_reply(thread(BOT, reply("/review-loop defer"), reply("This is still broken; please fix it.")), "coderabbitai")


@pytest.mark.parametrize("updated", ["2026-10-15T11:00:00Z", "2026-10-15T12:00:00Z", "invalid"])
def test_edited_reviewer_feedback_invalidates_a_stale_deferral(updated):
    finding = thread({**BOT, "updatedAt": updated}, reply("/review-loop defer"))
    pr = {"reviewThreads": {"nodes": [finding]}}
    assert not fgt.is_addressed_by_reply(finding, "coderabbitai")
    assert fgt.addressed_by_reply_threads(pr, "coderabbitai") == []
    assert len(fgt.filter_threads(pr, "coderabbitai", False, False)) == 1


def test_a_fresh_reply_can_defer_edited_feedback():
    finding = thread({**BOT, "updatedAt": "2026-10-15T12:00:00Z"}, {**reply("/review-loop defer"), "createdAt": "2026-10-15T13:00:00Z"})
    assert fgt.is_addressed_by_reply(finding, "coderabbitai")


@pytest.mark.parametrize("missing", ["reviewer", "authorization"])
def test_missing_comment_timestamps_do_not_authorize_deferral(missing):
    bot, authorization = dict(BOT), reply("/review-loop defer")
    (bot if missing == "reviewer" else authorization).pop("createdAt")
    assert not fgt.is_addressed_by_reply(thread(bot, authorization), "coderabbitai")


def test_edits_to_earlier_reviewer_comments_also_invalidate_deferral():
    finding = thread({**BOT, "updatedAt": "2026-10-15T12:00:00Z"}, {**BOT, "createdAt": "2026-10-15T10:30:00Z"}, reply("/review-loop defer"))
    assert not fgt.is_addressed_by_reply(finding, "coderabbitai")


def test_incomplete_comments_do_not_defer_or_hide_the_thread(monkeypatch):
    finding = thread(BOT, reply("/review-loop defer"))
    finding["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "more-replies"}
    pr = {"reviewThreads": {"nodes": [finding]}}

    def graphql_response(args):
        query = next(arg for arg in args if arg.startswith("query="))
        assert "pageInfo { hasNextPage }" in query.split("comments(first:50) {", 1)[1]
        return {"data": {"repository": {"pullRequest": pr}}}

    monkeypatch.setattr(fgt, "run_gh", graphql_response)
    pr = fgt.fetch_threads(fgt.PullRequest("owner", "repo", 1))
    assert not fgt.is_addressed_by_reply(finding, "coderabbitai")
    assert len(fgt.filter_threads(pr, "coderabbitai", False, False)) == 1
    assert fgt.addressed_by_reply_threads(pr, "coderabbitai") == []


@pytest.mark.parametrize("label,expected", [("Critical", "critical"), ("Major", "high"), ("Minor", "low")])
def test_coderabbit_header_maps_severity(label, expected):
    comment = {"author": {"login": "coderabbitai[bot]"}, "body": f"_⚠️ Potential issue_ | _🟠 {label}_\n\nPlease fix this."}
    assert fgt.thread_severity(thread(comment)) == expected
    comment["author"]["login"] = "alice"
    assert fgt.thread_severity(thread(comment)) == "unknown"


def test_generic_coderabbit_prose_does_not_assign_severity():
    assert fgt.thread_severity(thread({"author": {"login": "coderabbitai"}, "body": "This is a major concern with minor edge cases."})) == "unknown"


def test_optional_only_checks_do_not_verify_a_change(tmp_path):
    assert run_profile({"checks": [{"name": "optional", "command": ["not-an-executable"], "required": False}]}, tmp_path).verification == "failed"
