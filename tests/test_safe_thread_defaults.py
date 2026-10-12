import pytest

import fetch_gemini_threads as fgt
from run_profile import run_profile


def thread(*comments):
    return {"comments": {"nodes": list(comments)}}


BOT = {"author": {"login": "coderabbitai", "__typename": "Bot"}, "body": "Please fix this"}


def reply(body, association="COLLABORATOR", kind="User"):
    return {"author": {"login": "alice", "__typename": kind}, "authorAssociation": association, "body": body}


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
