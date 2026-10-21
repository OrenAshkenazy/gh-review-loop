"""Known-reviewer vendor records shared by discovery, fetch, and re-review.

Reviewer discovery works for any bot, but the loop can only drive a reviewer
end-to-end when it knows how to ping it. This registry holds that per-vendor
knowledge: the display name, the mention, the exact re-review phrase the vendor
accepts, and whether it reviews a new PR on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewVendor:
    login: str
    display_name: str
    mention: str
    rereview_phrase: str
    auto_reviews: bool
    # Set when a vendor's *free or individual* tier no longer performs reviews
    # while a paid or enterprise tier still does. Such a vendor stays fully
    # selectable and fully supported; it is only disqualified from being the
    # default, because a default applies to every user and most users are on
    # the tier that no longer works.
    consumer_tier_retired: bool = False
    tier_note: str = ""
    # How the loop asks this vendor for another look. "comment" vendors read
    # an @mention posted as a PR comment (``rereview_phrase``). A
    # "review_request" vendor ignores comments and is re-reviewed through
    # GitHub's requested-reviewers API using ``request_login``.
    trigger_kind: str = "comment"
    request_login: str = ""
    # The vendor's reviews are submitted against a specific head commit
    # (``commit.oid`` on every review) and its review body is an overview the
    # fetch can read. The loop then calls a PR clean only once a review covers
    # the current head (``loopStatus.cleanBlockedBy``), and the re-review
    # request is skipped when the head is already reviewed.
    head_reviews: bool = False
    # The vendor's verdict can arrive as a top-level PR comment rather than a
    # review: a "no issues found" note, or a whole review summary posted by a
    # CI job. The waiter and the clean gate then read those comments too.
    summary_comments: bool = False


# Google shut down the *consumer* Gemini Code Assist GitHub app on 2026-07-17;
# new consumer installs were blocked from 2026-06-18. The enterprise app is
# unaffected and still posts reviews, so Gemini remains a fully supported
# reviewer here. It is only disqualified as the *default*, since a new user on
# the consumer tier can no longer install the app at all.
# https://developers.google.com/gemini-code-assist/docs/deprecations/consumer-code-review
GEMINI = ReviewVendor(
    login="gemini-code-assist",
    display_name="Gemini Code Assist",
    mention="@gemini-code-assist",
    rereview_phrase="@gemini-code-assist please review the latest changes.",
    auto_reviews=True,
    consumer_tier_retired=True,
    tier_note=(
        "Gemini Code Assist reviews on GitHub now require the enterprise app. "
        "The consumer app was shut down on 2026-07-17. If you are on the "
        "enterprise app this works normally; if not, pick another reviewer."
    ),
)

# Codex answers to ``@codex``; its GraphQL author login is the connector app.
# It only reviews when pinged, so the loop must post the trigger itself for
# cycle 0 instead of waiting for a review that never arrives.
CODEX = ReviewVendor(
    login="chatgpt-codex-connector",
    display_name="Codex",
    mention="@codex",
    rereview_phrase="@codex review",
    auto_reviews=False,
)

# Copilot does not react to an @copilot comment. It reviews when it is added
# as a requested reviewer — the same REST call for the first request and every
# re-request (GitHub changelog 2026-10-02; behaviour confirmed on public PRs,
# see docs/superpowers/specs/evidence/2026-10-10-copilot-live-scan.txt). Its
# login differs by API: GraphQL ``copilot-pull-request-reviewer``, REST reviews
# ``copilot-pull-request-reviewer[bot]``, REST timeline ``Copilot``. GitHub
# does not dedupe requests server-side (two requests on one head produce two
# reviews), so request_rereview.py enforces head/pending dedupe before posting.
COPILOT = ReviewVendor(
    login="copilot-pull-request-reviewer",
    display_name="Copilot",
    mention="@copilot",
    rereview_phrase="",
    auto_reviews=False,
    trigger_kind="review_request",
    request_login="copilot-pull-request-reviewer[bot]",
    head_reviews=True,
)

# Claude Code reviews reach a PR through the Claude GitHub App, whose GraphQL
# login is ``claude`` (REST: ``claude[bot]``), from three surfaces that the
# loop treats as one vendor (docs/superpowers/specs/2026-10-21-claude-reviewer-design.md):
# the hosted Code Review service (inline 🔴/🟡/🟣 findings, a review per head
# commit with a ``<!-- bhrv:… -->`` summary), the claude-code-action
# ``/code-review --comment`` workflow (inline comments, or one "No issues found"
# PR comment), and custom CI workflows that post a review summary as a PR
# comment — sometimes as ``github-actions[bot]``; ``bind_login`` covers those.
# ``@claude review`` is the hosted service's exact comment command and also
# trips the action's default ``@claude`` trigger phrase. Whether a review runs
# unprompted is a per-repository setting the loop cannot read (once, every
# push, or manual), so the loop pings first, as it does for Codex; the hosted
# service does not start a second review while one is running on the head.
CLAUDE = ReviewVendor(
    login="claude",
    display_name="Claude Code",
    mention="@claude",
    rereview_phrase="@claude review",
    auto_reviews=False,
    head_reviews=True,
    summary_comments=True,
)

KNOWN_VENDORS = {vendor.login: vendor for vendor in (GEMINI, CODEX, COPILOT, CLAUDE)}

# The loop has no default reviewer: with nothing configured it asks rather than
# assumes. This is the reviewer it may *offer*. It is a ping-first vendor so
# that accepting the offer starts a review immediately, where offering an
# auto-reviewing vendor would only start a wait.
SUGGESTED_VENDOR = CODEX

# Back-compat alias for callers that still import the old name.
DEFAULT_VENDOR = SUGGESTED_VENDOR


def is_consumer_tier_retired(name: str | None) -> bool:
    """True when the vendor still reviews, but only on a paid or enterprise tier.

    This is a defaulting signal, not a support signal. Such a vendor works
    exactly as well as any other for the users who have it.
    """
    vendor = vendor_for(name)
    return bool(vendor and vendor.consumer_tier_retired)


def tier_note_for(name: str | None) -> str:
    """Return the vendor's tier caveat, or "" when it has none."""
    vendor = vendor_for(name)
    return vendor.tier_note if vendor else ""

# Callers hold either the account login (`chatgpt-codex-connector`) or the
# handle it answers to (`@codex`); both name the same vendor.
_ALIASES = {vendor.mention.lstrip("@").lower(): vendor for vendor in KNOWN_VENDORS.values()}
_ALIASES.update({login.lower(): vendor for login, vendor in KNOWN_VENDORS.items()})
# Names people use for the product rather than the account.
_ALIASES.update({"claude-code": CLAUDE, "claude-code-review": CLAUDE, "claude code": CLAUDE})

# Logins bound to a vendor for this process only (``bind_login``): a CI job
# that runs Claude Code posts as ``github-actions[bot]``, and the loop must
# filter that login while using Claude's trigger and formats. Kept apart from
# the static aliases so ``canonical_login`` can keep the bound login as-is.
_BOUND: dict[str, ReviewVendor] = {}


def _normalize(name: str) -> str:
    name = name.strip().lstrip("@")
    if name.endswith("[bot]"):
        name = name[: -len("[bot]")]
    return name.lower()


def vendor_for(name: str | None) -> ReviewVendor | None:
    """Return the vendor record for a reviewer login or mention, else None.

    GitHub renders bot logins with a ``[bot]`` suffix in some APIs and without
    it in GraphQL's ``author.login``; both spellings resolve to one vendor.
    """
    if not isinstance(name, str):
        return None
    key = _normalize(name)
    return _BOUND.get(key) or _ALIASES.get(key)


def bind_login(login: str, vendor: ReviewVendor | str) -> ReviewVendor:
    """Treat ``login`` as ``vendor`` for this process.

    The login keeps its own spelling everywhere authors are compared (reviews,
    threads, comments); only the vendor knowledge — trigger, formats, clean
    gate — is borrowed. Binding a login the registry already knows under
    another vendor is refused, so ``--reviewer-vendor`` cannot silently turn
    Codex threads into Claude ones. Returns the vendor.
    """
    resolved = vendor if isinstance(vendor, ReviewVendor) else vendor_for(vendor)
    if resolved is None:
        raise ValueError(f"unknown reviewer vendor: {vendor!r}")
    key = _normalize(login)
    if not key:
        raise ValueError("a bound login must not be empty")
    static = _ALIASES.get(key)
    if static is not None and static is not resolved:
        raise ValueError(
            f"{login!r} is {static.display_name}'s own login and cannot be bound to "
            f"{resolved.display_name}"
        )
    if static is None:
        # The vendor's own login needs no binding.
        _BOUND[key] = resolved
    return resolved


def bound_login(name: str | None) -> str | None:
    """``name`` as GraphQL spells it when it is a bound login, else None.

    The ``[bot]`` suffix REST adds is dropped, as review and thread filters
    compare against GraphQL's ``author.login``.
    """
    if not isinstance(name, str):
        return None
    key = _normalize(name)
    if key not in _BOUND:
        return None
    spelled = name.strip().lstrip("@")
    if spelled.endswith("[bot]"):
        spelled = spelled[: -len("[bot]")]
    return spelled


def unbind_all() -> None:
    """Forget every ``bind_login`` (tests)."""
    _BOUND.clear()


# How a finished Claude Code review announces itself in a top-level PR
# comment, across the surfaces listed on ``CLAUDE``. Live and documented
# markers only: the hosted service's review id and check-run markers, the
# claude-code-action code-review skill's clean note, the hosted "no issues"
# note, and the ``<!-- review:complete -->`` trailer custom review skills end
# with. The patterns identify a *review*, so the action's own
# "Claude Code is working…" tracking comment is deliberately not one.
CLAUDE_SUMMARY_SIGNAL_RE = re.compile(
    r"<!--\s*bhrv:|<!--\s*bughunter|<!--\s*review:complete\s*-->"
    r"|Checked for bugs and CLAUDE\.md compliance"
    r"|Code review found no issues",
    re.IGNORECASE,
)
# A comment the action posts while it is still running: never a verdict.
CLAUDE_IN_PROGRESS_RE = re.compile(r"Claude Code is working", re.IGNORECASE)


def summary_review_vendor(body: str | None) -> ReviewVendor | None:
    """The vendor whose finished review summary ``body`` is, else None."""
    if not isinstance(body, str) or CLAUDE_IN_PROGRESS_RE.search(body):
        return None
    if CLAUDE_SUMMARY_SIGNAL_RE.search(body):
        return CLAUDE
    return None
