"""Tests for the known-reviewer vendor registry."""

from __future__ import annotations

import review_vendors


def test_codex_vendor_is_known_by_its_graphql_login():
    vendor = review_vendors.vendor_for("chatgpt-codex-connector")

    assert vendor is not None
    assert vendor.display_name == "Codex"
    assert vendor.mention == "@codex"


def test_codex_rereview_phrase_is_the_exact_trigger_codex_accepts():
    vendor = review_vendors.vendor_for("chatgpt-codex-connector")

    assert vendor.rereview_phrase == "@codex review"


def test_bot_suffixed_login_resolves_to_the_same_vendor():
    assert review_vendors.vendor_for("chatgpt-codex-connector[bot]") is review_vendors.CODEX
    assert review_vendors.vendor_for("gemini-code-assist[bot]") is review_vendors.GEMINI


def test_gemini_keeps_its_sentence_shaped_rereview_phrase():
    vendor = review_vendors.vendor_for("gemini-code-assist")

    assert vendor.mention == "@gemini-code-assist"
    assert vendor.rereview_phrase == "@gemini-code-assist please review the latest changes."


def test_vendor_resolves_from_its_mention_too():
    assert review_vendors.vendor_for("@codex") is review_vendors.CODEX
    assert review_vendors.vendor_for("codex") is review_vendors.CODEX


def test_unknown_reviewer_login_has_no_vendor_record():
    assert review_vendors.vendor_for("coderabbitai") is None


def test_codex_does_not_review_until_it_is_pinged():
    assert review_vendors.CODEX.auto_reviews is False
    assert review_vendors.GEMINI.auto_reviews is True


def test_there_is_no_assumed_default_only_a_suggestion():
    """Any assumed vendor is wrong for someone, so the loop asks instead."""
    assert review_vendors.SUGGESTED_VENDOR.auto_reviews is False, (
        "an auto-reviewing suggestion turns 'accept the offer' into a wait"
    )
    assert not review_vendors.is_consumer_tier_retired(
        review_vendors.SUGGESTED_VENDOR.login
    ), "the offered reviewer must be installable on every tier"


def test_gemini_remains_a_fully_supported_reviewer():
    """Only the consumer app was shut down; enterprise tenants are unaffected."""
    gemini = review_vendors.vendor_for("gemini-code-assist")
    assert gemini is not None
    assert gemini.login in review_vendors.KNOWN_VENDORS
    assert gemini.mention == "@gemini-code-assist"
    assert gemini.rereview_phrase
    assert gemini.auto_reviews is True
    assert review_vendors.vendor_for("@gemini-code-assist") is gemini


def test_copilot_vendor_resolves_from_every_login_spelling():
    """GraphQL says copilot-pull-request-reviewer, REST reviews say ...[bot],
    the REST timeline says Copilot, and the handle is @copilot."""
    for name in (
        "copilot-pull-request-reviewer",
        "copilot-pull-request-reviewer[bot]",
        "Copilot",
        "copilot",
        "@copilot",
    ):
        assert review_vendors.vendor_for(name) is review_vendors.COPILOT, name


def test_copilot_is_re_reviewed_by_request_not_by_comment():
    copilot = review_vendors.COPILOT

    assert copilot.display_name == "Copilot"
    assert copilot.trigger_kind == "review_request"
    assert copilot.request_login == "copilot-pull-request-reviewer[bot]"
    assert copilot.rereview_phrase == ""
    assert copilot.auto_reviews is False


def test_comment_vendors_default_to_the_comment_trigger_kind():
    assert review_vendors.CODEX.trigger_kind == "comment"
    assert review_vendors.GEMINI.trigger_kind == "comment"
    assert review_vendors.CODEX.request_login == ""


def test_adding_copilot_did_not_change_the_suggested_vendor():
    assert review_vendors.SUGGESTED_VENDOR is review_vendors.CODEX
