"""Reviewer discovery and persisted reviewer record helpers."""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any


import review_vendors


KNOWN_TRIGGERS = {
    login: vendor.mention for login, vendor in review_vendors.KNOWN_VENDORS.items()
}

KNOWN_DISPLAY_NAMES = {
    login: vendor.display_name for login, vendor in review_vendors.KNOWN_VENDORS.items()
}


@dataclass(frozen=True)
class Candidate:
    login: str
    display_name: str
    review_trigger: str | None
    trigger_kind: str = "comment"
    # Where discovery saw this reviewer: "thread" (inline review comment),
    # "review" (a submitted review with no inline comments), or "requested"
    # (a pending review request that has not produced a review yet).
    source: str = "thread"

    def to_dict(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "display_name": self.display_name,
            "review_trigger": self.review_trigger,
            "trigger_kind": self.trigger_kind,
            "source": self.source,
        }


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def display_name_for(login: str) -> str:
    vendor = review_vendors.vendor_for(login)
    if vendor:
        return vendor.display_name
    words = re.split(r"[-_\s]+", login.replace("[bot]", " bot"))
    return " ".join(word.capitalize() for word in words if word) or login


def trigger_for(login: str) -> str | None:
    """The @mention that re-reviews this vendor, or None when there is none.

    A review_request vendor has a handle (``@copilot``) but no comment the loop
    could post to trigger it, so it has no trigger here.
    """
    vendor = review_vendors.vendor_for(login)
    if vendor is None or vendor.trigger_kind != "comment":
        return None
    return vendor.mention


def phrase_for(login: str) -> str | None:
    """Return the exact re-review comment a known vendor accepts.

    Vendors differ: Gemini reads any sentence that mentions it, Codex matches
    ``@codex review`` exactly. Unknown reviewers have no phrase, so callers
    fall back to the generic mention-plus-sentence form. Review-request
    vendors have no phrase at all.
    """
    vendor = review_vendors.vendor_for(login)
    if vendor is None or vendor.trigger_kind != "comment":
        return None
    return vendor.rereview_phrase


def trigger_kind_for(login: str) -> str:
    """"comment" (post an @mention) or "review_request" (requested-reviewers API)."""
    vendor = review_vendors.vendor_for(login)
    return vendor.trigger_kind if vendor else "comment"


def request_login_for(login: str) -> str | None:
    """The login to pass to the requested-reviewers API, for review_request vendors."""
    vendor = review_vendors.vendor_for(login)
    if vendor is None or vendor.trigger_kind != "review_request":
        return None
    return vendor.request_login


def auto_reviews(login: str) -> bool:
    """Whether the reviewer reviews a new PR without being pinged.

    Unknown reviewers are assumed self-starting, which preserves the loop's
    original wait-then-fetch behavior for bots we have no record of.
    """
    vendor = review_vendors.vendor_for(login)
    return vendor.auto_reviews if vendor else True


def normalize_request_event(event: Any) -> dict[str, Any] | None:
    """One shape for a review-request event from either GitHub API.

    Accepts a GraphQL ``ReviewRequestedEvent`` node (``createdAt``,
    ``actor.login``, ``requestedReviewer.login``) or a REST timeline event
    (``event == "review_requested"``, ``created_at``, ``actor.login``,
    ``requested_reviewer.login``). Team requests, other event types, and
    non-dicts normalize to None.
    """
    if not isinstance(event, dict):
        return None
    if "event" in event or "requested_reviewer" in event or "requested_team" in event:
        if event.get("event") != "review_requested":
            return None
        requested = event.get("requested_reviewer")
        if not isinstance(requested, dict):
            return None
        return {
            "created_at": event.get("created_at") or "",
            "actor_login": (event.get("actor") or {}).get("login") or "",
            "reviewer_login": requested.get("login") or "",
        }
    if event.get("__typename") not in (None, "ReviewRequestedEvent"):
        return None
    requested = event.get("requestedReviewer")
    if not isinstance(requested, dict):
        return None
    return {
        "created_at": event.get("createdAt") or "",
        "actor_login": (event.get("actor") or {}).get("login") or "",
        "reviewer_login": requested.get("login") or "",
    }


def first_completed_review_at(
    reviews: Any, vendor: review_vendors.ReviewVendor
) -> str | None:
    """Earliest submitted, non-PENDING review by ``vendor``; None when there is none.

    Accepts GraphQL review nodes (``author.login``, ``submittedAt``) or REST
    reviews (``user.login``, ``submitted_at``). This is the cycle-0 boundary:
    a review request made before it is the initial request and is free.
    """
    earliest: str | None = None
    for review in reviews or []:
        if not isinstance(review, dict) or review.get("state") == "PENDING":
            continue
        author = review.get("author") or review.get("user") or {}
        if review_vendors.vendor_for(author.get("login")) is not vendor:
            continue
        submitted = review.get("submittedAt") or review.get("submitted_at") or ""
        if submitted and (earliest is None or submitted < earliest):
            earliest = submitted
    return earliest


def counted_review_requests(
    events: Any,
    *,
    agent_login: str | None,
    vendor: review_vendors.ReviewVendor,
    first_completed_review_at: str | None,
) -> list[dict[str, Any]]:
    """The review requests that consume the loop cap, in timeline order.

    An event counts when it asks ``vendor``, was made by ``agent_login`` (any
    actor when None), and came after the vendor's first completed review. So
    the initial request, a human's click, a failed POST (no event) and a
    deduplicated request (no POST) all cost nothing. This is the single
    counting rule shared by the fetch (GraphQL nodes) and the request script
    (REST events).
    """
    if not first_completed_review_at:
        return []
    out: list[dict[str, Any]] = []
    for raw in events or []:
        event = normalize_request_event(raw)
        if event is None:
            continue
        if review_vendors.vendor_for(event["reviewer_login"]) is not vendor:
            continue
        if agent_login is not None and event["actor_login"] != agent_login:
            continue
        if event["created_at"] <= first_completed_review_at:
            continue
        out.append(event)
    return out


def canonical_login(login: str) -> str:
    """The login GraphQL reports for this reviewer, for a known vendor's alias.

    Callers select a reviewer by whatever spelling they have — ``copilot``,
    ``@copilot``, ``Copilot``, ``copilot-pull-request-reviewer[bot]`` — while
    review and thread filters compare authors exactly against the stored
    login. Storing an alias would request the right reviewer and then never
    match its review. Unknown logins pass through untouched.
    """
    vendor = review_vendors.vendor_for(login)
    return vendor.login if vendor else login


def make_reviewer_record(
    login: str,
    *,
    display_name: str | None = None,
    review_trigger: str | None = None,
    source: str,
    selected_at: str | None = None,
) -> dict[str, Any]:
    login = canonical_login(login)
    return {
        "login": login,
        "display_name": display_name or display_name_for(login),
        "review_trigger": review_trigger if review_trigger is not None else trigger_for(login),
        "trigger_kind": trigger_kind_for(login),
        "request_login": request_login_for(login),
        "auto_reviews": auto_reviews(login),
        "source": source,
        "selected_at": selected_at or now_iso(),
    }


def _iter_comments(thread: dict[str, Any]) -> list[dict[str, Any]]:
    comments = thread.get("comments")
    if isinstance(comments, list):
        return [comment for comment in comments if isinstance(comment, dict)]
    if isinstance(comments, dict):
        nodes = comments.get("nodes")
        if isinstance(nodes, list):
            return [comment for comment in nodes if isinstance(comment, dict)]
    return []


def _is_bot_author(author: dict[str, Any]) -> bool:
    typename = author.get("__typename")
    login = author.get("login")
    if typename == "Bot":
        return True
    if isinstance(login, str) and login.endswith("[bot]"):
        return True
    return False


_SOURCE_RANK = {"thread": 0, "review": 1, "requested": 2}


def discover_candidates(
    pull_request: dict[str, Any],
    *,
    self_login: str | None,
) -> list[Candidate]:
    """Bot reviewers visible on the PR: threads first, then reviews, then pending requests.

    Thread comment authors are the strongest signal (they left findings). A
    review with no inline comments (a clean Copilot pass) only shows up as a
    review author. A reviewer that was requested but has not answered yet only
    shows up as a pending request. Within a source, first-seen order; a login
    seen in an earlier source keeps that source.
    """
    seen: dict[str, tuple[int, int, str]] = {}

    def consider(author: Any, index: int, source: str) -> None:
        if not isinstance(author, dict):
            return
        login = author.get("login")
        if not isinstance(login, str) or not login:
            return
        if self_login and login == self_login:
            return
        if login in seen or not _is_bot_author(author):
            return
        seen[login] = (_SOURCE_RANK[source], index, source)

    threads = (pull_request.get("reviewThreads") or {}).get("nodes") or []
    for thread_index, thread in enumerate(threads):
        if not isinstance(thread, dict):
            continue
        for comment in _iter_comments(thread):
            consider(comment.get("author"), thread_index, "thread")
    reviews = (pull_request.get("reviews") or {}).get("nodes") or []
    for review_index, review in enumerate(reviews):
        if isinstance(review, dict):
            consider(review.get("author"), review_index, "review")
    requests = (pull_request.get("reviewRequests") or {}).get("nodes") or []
    for request_index, request in enumerate(requests):
        if isinstance(request, dict):
            consider(request.get("requestedReviewer"), request_index, "requested")

    return [
        Candidate(
            login=login,
            display_name=display_name_for(login),
            review_trigger=trigger_for(login),
            trigger_kind=trigger_kind_for(login),
            source=source,
        )
        for login, (_rank, _index, source) in sorted(
            seen.items(), key=lambda item: (item[1][0], item[1][1], item[0])
        )
    ]
