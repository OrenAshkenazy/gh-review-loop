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


def make_reviewer_record(
    login: str,
    *,
    display_name: str | None = None,
    review_trigger: str | None = None,
    source: str,
    selected_at: str | None = None,
) -> dict[str, Any]:
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


def discover_candidates(
    pull_request: dict[str, Any],
    *,
    self_login: str | None,
) -> list[Candidate]:
    first_seen: dict[str, int] = {}
    threads = (pull_request.get("reviewThreads") or {}).get("nodes") or []
    for thread_index, thread in enumerate(threads):
        if not isinstance(thread, dict):
            continue
        for comment in _iter_comments(thread):
            author = comment.get("author")
            if not isinstance(author, dict):
                continue
            login = author.get("login")
            if not isinstance(login, str) or not login:
                continue
            if self_login and login == self_login:
                continue
            if login in first_seen or not _is_bot_author(author):
                continue
            first_seen[login] = thread_index
    return [
        Candidate(
            login=login,
            display_name=display_name_for(login),
            review_trigger=trigger_for(login),
        )
        for login, _thread_index in sorted(
            first_seen.items(), key=lambda item: (item[1], item[0])
        )
    ]
