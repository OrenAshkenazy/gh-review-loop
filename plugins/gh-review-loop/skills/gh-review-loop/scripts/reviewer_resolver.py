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
    # "review" (a submitted review with no inline comments), "requested" (a
    # pending review request that has not produced a review yet), or
    # "comment" (a review summary posted as a top-level PR comment).
    source: str = "thread"
    # The known vendor this login posts for when the login is not the
    # vendor's own (a CI job running Claude Code posts as github-actions[bot]).
    # Selecting such a candidate persists the login bound to this vendor.
    vendor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "login": self.login,
            "display_name": self.display_name,
            "review_trigger": self.review_trigger,
            "trigger_kind": self.trigger_kind,
            "source": self.source,
        }
        if self.vendor:
            out["vendor"] = self.vendor
        return out


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def display_name_for(login: str) -> str:
    vendor = review_vendors.vendor_for(login)
    if vendor:
        bound = review_vendors.bound_login(login)
        if bound:
            # Name both: the vendor whose review it is, and the login the
            # loop filters on, so narration never hides the binding.
            return f"{vendor.display_name} (as {bound})"
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


def head_reviews_for(login: str) -> bool:
    """Whether the vendor's reviews name the head commit they cover (Copilot, Claude).

    Only then can the loop tell "reviewed and clean" from "not reviewed yet"
    and hold the clean verdict until a review covers the current head.
    """
    vendor = review_vendors.vendor_for(login)
    return bool(vendor and vendor.head_reviews)


def summary_comments_for(login: str) -> bool:
    """Whether the vendor's verdict may arrive as a top-level PR comment (Claude)."""
    vendor = review_vendors.vendor_for(login)
    return bool(vendor and vendor.summary_comments)


def vendor_key_for(login: str) -> str | None:
    """The registry key (vendor login) of ``login``'s vendor, or None when unknown."""
    vendor = review_vendors.vendor_for(login)
    return vendor.login if vendor else None


def bind_from_record(record: Any) -> None:
    """Re-apply a persisted ``vendor`` binding so this process resolves the login.

    A record selected with ``--reviewer-vendor`` stores the bound login and
    the vendor it posts for; every later script invocation starts with an
    empty binding table and must restore it before any author or trigger
    lookup. A record whose login the registry already knows needs nothing.
    """
    if not isinstance(record, dict):
        return
    login = record.get("login")
    vendor = record.get("vendor")
    if not isinstance(login, str) or not isinstance(vendor, str) or not vendor:
        return
    if review_vendors.vendor_for(login) is not None:
        return
    try:
        review_vendors.bind_login(login, vendor)
    except ValueError:
        # An unknown vendor name in an old or hand-edited record: the login
        # stays an unknown reviewer, which is what it was before binding.
        return


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


def normalize_request_removed_event(event: Any) -> dict[str, Any] | None:
    """One shape for a review-request *removal* from either GitHub API.

    Accepts a GraphQL ``ReviewRequestRemovedEvent`` node or a REST timeline
    event with ``event == "review_request_removed"``. Everything else,
    including a plain request event, normalizes to None.
    """
    if not isinstance(event, dict):
        return None
    if "event" in event or "requested_reviewer" in event or "requested_team" in event:
        if event.get("event") != "review_request_removed":
            return None
        requested = event.get("requested_reviewer")
        if not isinstance(requested, dict):
            return None
        return {
            "created_at": event.get("created_at") or "",
            "actor_login": (event.get("actor") or {}).get("login") or "",
            "reviewer_login": requested.get("login") or "",
        }
    if event.get("__typename") != "ReviewRequestRemovedEvent":
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


def unanswered_request_times(
    request_times: list[str],
    review_times: list[str],
    removed_times: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Review-request times no submitted review has answered, oldest first.

    A removal cancels the latest request made strictly before it: the stale
    path withdraws request A and posts B in the same second, and the review
    that answers B must not be paired with A, or B reads as unanswered for
    twenty minutes and then invites a third request (PR #116 review). Each
    submitted review then answers the earliest remaining request made at or
    before it; GitHub stamps both to the second, so a same-second answer
    counts. What is left is still waiting. Comparing only the newest request
    time with the newest review time cannot tell two requests in one second
    from one. Both the request-side state machine and the fetch-side head
    check pair this way so they never disagree about which review is current.
    """
    unanswered = sorted(time for time in request_times if time)
    for removed in sorted(time for time in removed_times if time):
        for index in range(len(unanswered) - 1, -1, -1):
            if unanswered[index] < removed:
                unanswered.pop(index)
                break
    for submitted in sorted(time for time in review_times if time):
        for index, requested in enumerate(unanswered):
            if requested <= submitted:
                unanswered.pop(index)
                break
    return unanswered


# ``posted_at`` is the local clock just before the POST; the event time is
# GitHub's. A local clock running ahead would put every event "before" its
# record, so matching tolerates that much skew in the other direction.
PROVENANCE_SKEW_SECONDS = 120


def _within(event_at: str, posted_at: str, window_seconds: int) -> bool:
    """True when a timeline event is the request posted at ``posted_at``."""
    start = _parse_instant(posted_at)
    seen = _parse_instant(event_at)
    if start is None or seen is None:
        return False
    delta = (seen - start).total_seconds()
    return -PROVENANCE_SKEW_SECONDS <= delta <= window_seconds


def same_login(a: str | None, b: str | None) -> bool:
    """GitHub logins are case-insensitive."""
    return bool(a) and bool(b) and a.casefold() == b.casefold()


def shift_iso(value: str, seconds: int) -> str:
    """``value`` moved by ``seconds``, in GitHub's second-precision format."""
    instant = _parse_instant(value)
    if instant is None:
        return value
    return (instant + _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def loop_request_times_from_entry(
    entry: Any,
    vendor: review_vendors.ReviewVendor,
    logged: list[dict[str, Any]] | None = None,
) -> list[str] | None:
    """The countable request times this loop recorded for ``vendor``, sorted.

    None means unknown, not empty: no record for the PR, or a POST whose
    record could not be written (``review_requests_incomplete``). An empty
    list is authoritative to the counter. Records stored with
    ``countable: False`` are the free first request; whether a request is a
    re-review is known when it is posted, so it is never re-derived by
    comparing the local clock with GitHub's. ``logged`` is the PR's
    append-only request log (``loop_state.read_request_log``); records in
    ``entry`` are the older state.json form, still honored.
    """
    if isinstance(entry, dict) and entry.get("review_requests_incomplete"):
        return None
    if logged and any(isinstance(item, dict) and item.get("incomplete") for item in logged):
        return None
    stored = entry.get("review_requests") if isinstance(entry, dict) else None
    if not isinstance(stored, list) and logged is None:
        return None
    requests = (stored if isinstance(stored, list) else []) + (logged or [])
    out = []
    for item in requests:
        if not isinstance(item, dict) or item.get("countable") is False:
            continue
        if review_vendors.vendor_for(item.get("reviewer")) is not vendor:
            continue
        at = item.get("at")
        if isinstance(at, str) and at:
            out.append(at)
    return sorted(out)


def _parse_instant(value: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except (TypeError, ValueError):
        return None


def counted_review_requests(
    events: Any,
    *,
    agent_login: str | None,
    vendor: review_vendors.ReviewVendor,
    first_completed_review_at: str | None,
    loop_request_times: list[str] | None = None,
    match_window_seconds: int = 180,
) -> list[dict[str, Any]]:
    """The review requests that consume the loop cap, in timeline order.

    An event counts when it asks ``vendor`` and came after the vendor's first
    completed review, so the initial request, a failed POST (no event) and a
    deduplicated request (no POST) all cost nothing.

    Provenance comes from ``loop_request_times`` — the timestamps the loop
    recorded locally as it posted. The actor cannot supply it: the loop uses
    the member's own `gh` credentials, so a request the member clicked in the
    GitHub UI carries the same ``actor_login``, and counting those would let a
    few manual clicks stop a first loop run as capped. Each recorded time
    claims at most one event. ``loop_request_times=None`` means "no local
    record available" and falls back to the actor filter, which bounds the
    loop from above rather than exactly.
    """
    if not first_completed_review_at:
        return []
    candidates: list[dict[str, Any]] = []
    for raw in events or []:
        event = normalize_request_event(raw)
        if event is None:
            continue
        if review_vendors.vendor_for(event["reviewer_login"]) is not vendor:
            continue
        # The actor fallback cannot tell the free first request from a
        # re-review in the first review's second, so it excludes that second.
        # Recorded times already exclude the free request (``countable``), so
        # with them only events strictly before the first review are cycle 0.
        if loop_request_times is None:
            if event["created_at"] <= first_completed_review_at:
                continue
        elif event["created_at"] < first_completed_review_at:
            continue
        candidates.append(event)
    candidates.sort(key=lambda event: event["created_at"])

    if loop_request_times is None:
        if agent_login is None:
            return candidates
        return [event for event in candidates if same_login(event["actor_login"], agent_login)]

    out: list[dict[str, Any]] = []
    unmatched = list(candidates)
    for posted_at in sorted(loop_request_times):
        for index, event in enumerate(unmatched):
            if _within(event["created_at"], posted_at, match_window_seconds):
                out.append(unmatched.pop(index))
                break
    out.sort(key=lambda event: event["created_at"])
    return out


def canonical_login(login: str) -> str:
    """The login GraphQL reports for this reviewer, for a known vendor's alias.

    Callers select a reviewer by whatever spelling they have — ``copilot``,
    ``@copilot``, ``Copilot``, ``copilot-pull-request-reviewer[bot]`` — while
    review and thread filters compare authors exactly against the stored
    login. Storing an alias would request the right reviewer and then never
    match its review. Unknown logins pass through untouched, and so does a
    login bound to a vendor (``review_vendors.bind_login``): it is the login
    the vendor's reviews really carry.
    """
    bound = review_vendors.bound_login(login)
    if bound:
        return bound
    vendor = review_vendors.vendor_for(login)
    return vendor.login if vendor else login


def make_reviewer_record(
    login: str,
    *,
    display_name: str | None = None,
    review_trigger: str | None = None,
    source: str,
    selected_at: str | None = None,
    vendor: str | None = None,
) -> dict[str, Any]:
    """The persisted reviewer selection for ``login``.

    ``vendor`` binds a login the registry does not know to a known vendor's
    trigger and formats (``--reviewer-vendor claude`` for a CI job that posts
    Claude's review as ``github-actions[bot]``). The binding is stored on the
    record so later runs restore it (``bind_from_record``). Naming the
    vendor whose own login this already is changes nothing.
    """
    if vendor:
        # Raises ValueError for an unknown vendor or a login that belongs to
        # another vendor; the CLI reports that as an argument error.
        review_vendors.bind_login(login, vendor)
    login = canonical_login(login)
    record = {
        "login": login,
        "display_name": display_name or display_name_for(login),
        "review_trigger": review_trigger if review_trigger is not None else trigger_for(login),
        "trigger_kind": trigger_kind_for(login),
        "request_login": request_login_for(login),
        "auto_reviews": auto_reviews(login),
        "source": source,
        "selected_at": selected_at or now_iso(),
    }
    if review_vendors.bound_login(login):
        record["vendor"] = vendor_key_for(login)
    return record


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


_SOURCE_RANK = {"thread": 0, "review": 1, "requested": 2, "comment": 3}


def discover_candidates(
    pull_request: dict[str, Any],
    *,
    self_login: str | None,
) -> list[Candidate]:
    """Bot reviewers visible on the PR: threads, then reviews, pending requests, summary comments.

    Thread comment authors are the strongest signal (they left findings). A
    review with no inline comments (a clean Copilot pass) only shows up as a
    review author. A reviewer that was requested but has not answered yet only
    shows up as a pending request. A reviewer whose whole verdict is a
    top-level PR comment (a clean Claude pass, or a CI job posting Claude's
    summary as ``github-actions[bot]``) only shows up as the author of a
    comment carrying a known vendor's review signal; such a login is offered
    bound to that vendor. Within a source, first-seen order; a login seen in
    an earlier source keeps that source.
    """
    seen: dict[str, tuple[int, int, str, str | None]] = {}

    def consider(author: Any, index: int, source: str, vendor: str | None = None) -> None:
        if not isinstance(author, dict):
            return
        login = author.get("login")
        if not isinstance(login, str) or not login:
            return
        if self_login and login == self_login:
            return
        if login in seen or not _is_bot_author(author):
            return
        seen[login] = (_SOURCE_RANK[source], index, source, vendor)

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
    comments = (pull_request.get("comments") or {}).get("nodes") or []
    for comment_index, comment in enumerate(comments):
        if not isinstance(comment, dict):
            continue
        signalled = review_vendors.summary_review_vendor(comment.get("body"))
        if signalled is None:
            continue
        author = comment.get("author") if isinstance(comment.get("author"), dict) else {}
        own = review_vendors.vendor_for(author.get("login"))
        # The vendor's own login needs no binding; another vendor's login
        # carrying this signal is that vendor's comment, not a candidate here.
        if own is not None and own is not signalled:
            continue
        consider(author, comment_index, "comment", None if own else signalled.login)

    candidates = []
    for login, (_rank, _index, source, vendor) in sorted(
        seen.items(), key=lambda item: (item[1][0], item[1][1], item[0])
    ):
        if vendor:
            known = review_vendors.vendor_for(vendor)
            candidates.append(
                Candidate(
                    login=login,
                    display_name=f"{known.display_name} (as {login})" if known else login,
                    review_trigger=known.mention if known and known.trigger_kind == "comment" else None,
                    trigger_kind=known.trigger_kind if known else "comment",
                    source=source,
                    vendor=vendor,
                )
            )
            continue
        candidates.append(
            Candidate(
                login=login,
                display_name=display_name_for(login),
                review_trigger=trigger_for(login),
                trigger_kind=trigger_kind_for(login),
                source=source,
            )
        )
    return candidates
