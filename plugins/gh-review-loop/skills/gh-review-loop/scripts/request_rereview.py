#!/usr/bin/env python3
"""Request an AI reviewer re-review for a PR: a comment, or a review request.

Comment vendors (Gemini, Codex) read an @mention posted as a PR comment.
Review-request vendors (Copilot) ignore comments and are re-reviewed through
GitHub's requested-reviewers API; GitHub does not deduplicate those requests,
so this script resolves the PR's state first and only posts from ABSENT or
STALE_PENDING (see docs/superpowers/specs/2026-10-07-copilot-reviewer-design.md §3).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import hashlib
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator


import judge
import review_vendors
import reviewer_resolver

import loop_lock
import loop_state  # noqa: E402 — sibling module, stdlib-only


DEFAULT_REVIEWER_MENTION = review_vendors.DEFAULT_VENDOR.mention
DEFAULT_REVIEWER_LOGIN = review_vendors.DEFAULT_VENDOR.login


def build_default_phrase(reviewer_mention: str = DEFAULT_REVIEWER_MENTION) -> str:
    reviewer_mention = reviewer_mention.strip() or DEFAULT_REVIEWER_MENTION
    if not reviewer_mention.startswith("@"):
        reviewer_mention = f"@{reviewer_mention}"
    return f"{reviewer_mention} please review the latest changes."


DEFAULT_PHRASE = review_vendors.DEFAULT_VENDOR.rereview_phrase


def no_safe_trigger_payload(reviewer_login: str) -> dict[str, Any]:
    reviewer = reviewer_login.strip() or "unknown reviewer"
    return {
        "status": "no_safe_trigger",
        "posted": False,
        "reviewer": reviewer,
        "message": (
            f"No safe re-review trigger known for `{reviewer}`; "
            "pass --review-trigger-mention to enable re-review requests."
        ),
    }


# A pending review request older than this with no review for the head is
# treated as abandoned and re-requested. Observed Copilot latency on public
# PRs is 4 s to ~8 min (spec §9, LV-5).
STALE_REQUEST_SECONDS = 1200

# The head sha and the pending request, which only GraphQL can answer. REST
# `requested_reviewers` is deliberately not used: it is empty while a Copilot
# request is pending (spec §9, LV-3); only GraphQL `reviewRequests` lists the
# bot. Reviews and review-request events are NOT read here — a single GraphQL
# page silently truncates both on a busy PR, and the two facts the cap and the
# dedupe rest on (the vendor's first completed review, and whether it already
# reviewed this head) must be complete, so they come from the paginated REST
# APIs below.
STATE_QUERY = """
query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      id
      headRefOid
      reviewRequests(first:50) {
        nodes { requestedReviewer { __typename ... on Bot { login } ... on User { login } } }
      }
    }
  }
}
"""


class ReviewRequestRejected(RuntimeError):
    """The review-request mutation did not succeed.

    ``reason`` classifies it (spec §6): permission, validation, not_found,
    rate_limited, transient, or uncertain (no HTTP status — the request may
    have landed). ``requested_at`` is the pre-POST timestamp an uncertain
    outcome is reconciled against.
    """

    def __init__(
        self,
        detail: str,
        *,
        reason: str = "uncertain",
        http_status: int | None = None,
        retryable: bool = False,
        requested_at: str | None = None,
        outcome_unknown: bool | None = None,
        allow_timeline_confirmation: bool = True,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.reason = reason
        self.http_status = http_status
        self.retryable = retryable
        self.requested_at = requested_at
        self.outcome_unknown = (reason == "uncertain" or (http_status is not None and http_status >= 500)) if outcome_unknown is None else outcome_unknown
        self.allow_timeline_confirmation = allow_timeline_confirmation


_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


def classify_gh_failure(stderr: str | None) -> tuple[str, int | None, bool]:
    """(reason, http_status, retryable) for a failed ``gh api`` call.

    No ``HTTP <code>`` in the output means the request may or may not have
    reached GitHub: that is ``uncertain`` and never auto-retried.
    """
    text = stderr or ""
    match = _HTTP_STATUS_RE.search(text)
    if not match:
        return ("uncertain", None, False)
    code = int(match.group(1))
    if code == 429 or (code == 403 and "rate limit" in text.lower()):
        return ("rate_limited", code, True)
    if code in (401, 403):
        return ("permission", code, False)
    if code == 404:
        return ("not_found", code, False)
    if code >= 500:
        return ("transient", code, True)
    return ("validation", code, False)


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _parse_iso(value: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def already_reviewed_payload(repo: str, pr: int, reviewer: str, head_sha: str) -> dict[str, Any]:
    return {
        "status": "already_reviewed_head",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "reviewer": reviewer,
        "head": head_sha,
        "message": (
            f"`{reviewer}` has already reviewed the current head {head_sha[:7]}; "
            "no new request was posted. Fetch that review instead of waiting."
        ),
    }


def review_pending_payload(repo: str, pr: int, reviewer: str, pending_since: str | None) -> dict[str, Any]:
    since = f" since {pending_since}" if pending_since else ""
    return {
        "status": "review_pending",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "reviewer": reviewer,
        "pending_since": pending_since,
        "message": (
            f"A review by `{reviewer}` is already pending{since}; no new request was "
            "posted (a second request would produce a second review). Wait for it."
        ),
    }


def head_mismatch_payload(repo: str, pr: int, expected_head: str, head: str) -> dict[str, Any]:
    return {
        "status": "head_mismatch",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "expected_head": expected_head,
        "head": head,
        "message": (
            f"The PR head on GitHub is {head[:7]} but --expect-head is {expected_head[:7]}; "
            "the push has not landed yet. Nothing was posted; retry shortly."
        ),
    }


def uncertain_payload(repo: str, pr: int, reviewer: str, exc: ReviewRequestRejected) -> dict[str, Any]:
    """The POST may have landed: report the outcome as unknown, not as unposted.

    ``gh`` losing its connection after GitHub accepted the POST but before
    returning a status leaves no HTTP code, and the timeline reconciliation can
    run before the new event is visible. Saying "nothing was posted" and
    inviting a re-run is how the duplicate review this state machine prevents
    gets created, so the honest answer is unknown plus a delayed state check.
    """
    detail = (exc.detail or "").strip() or "no detail from GitHub"
    settle = f"{REGISTRATION_SETTLE_SECONDS // 60} minutes"
    if exc.reason == "registration_pending":
        message = (
            f"GitHub accepted the review request for `{reviewer}` (HTTP {exc.http_status}), but it is "
            "not on the PR yet. It may still appear, so nothing was recorded as posted. "
            f"Do not request again now: re-run this script after {settle}. It reports "
            "review_pending if the request landed, or not_enabled if it never appeared."
        )
    elif exc.reason == "settling":
        message = (
            f"An earlier review request for `{reviewer}` (at {exc.requested_at}) is not "
            f"confirmed yet and is still inside its {settle} settle time. Nothing was "
            "posted. Re-run this script after that; it settles the earlier request first."
        )
    elif exc.reason == "settle_unreadable":
        message = (
            f"An earlier review request for `{reviewer}` (at {exc.requested_at}) is not "
            f"confirmed, and reading the PR to settle it failed ({detail}). Nothing was "
            "posted. Re-run this script in a minute."
        )
    elif exc.reason == "transient":
        message = (
            f"GitHub answered HTTP {exc.http_status} to the review request for `{reviewer}` "
            f"({detail}). A server error can follow a request GitHub did register, and no "
            "matching request is visible on the timeline yet, so the outcome is unknown. "
            f"It was not retried. Re-run this script after {settle}; it posts a fresh "
            "request only if the earlier one never landed."
        )
    elif exc.reason == "registration_unknown":
        # GitHub answered 201, so the POST landed; what failed is reading back
        # whether the reviewer was added (spec §6.1). Recording it could stamp
        # a request that does not exist; denying it could invite a duplicate.
        message = (
            f"GitHub accepted the review request for `{reviewer}` (HTTP 201) but reading "
            f"the PR's requested reviewers back failed ({detail}), so whether `{reviewer}` "
            "was added is unknown. Nothing was recorded. Do not request again immediately: "
            "re-run this script in a minute — its state check reports review_pending if "
            "the request landed, and posts a fresh request only if GitHub dropped it."
        )
    else:
        message = (
            f"The review request for `{reviewer}` returned no HTTP status ({detail}), "
            "and no matching request was visible on the PR timeline yet, so whether "
            "GitHub accepted it is unknown. Do not request again immediately: the "
            "timeline can lag by seconds. Re-read the PR state in a minute and "
            "request again only if no review request from this agent appears."
        )
    return {
        "status": "review_request_uncertain",
        "posted": None,
        "outcome": "unknown",
        "repo": repo,
        "pr": pr,
        "reviewer": reviewer,
        "reason": exc.reason,
        "http_status": exc.http_status,
        "retryable": exc.retryable,
        "requested_at": exc.requested_at,
        "message": message,
    }


def not_enabled_payload(repo: str, pr: int, reviewer: str, exc: ReviewRequestRejected) -> dict[str, Any]:
    """GitHub answered 201 but added nobody: Copilot code review is not available here.

    Observed on the first live run (spec §6.1): without a plan that includes
    code review, the POST succeeds, `requested_reviewers` stays empty, no
    request is pending and no timeline event is written. Nothing to wait for,
    nothing to count.
    """
    return {
        "status": "review_request_rejected",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "reviewer": reviewer,
        "reason": "not_enabled",
        "http_status": exc.http_status,
        "retryable": False,
        "requested_at": exc.requested_at,
        "message": (
            f"GitHub accepted the review request for `{reviewer}` (HTTP 201) but did not add "
            "it as a reviewer, and no review followed. That is what GitHub does when Copilot "
            f"code review is not available for {repo}: the account needs a Copilot plan that "
            "includes code review (Pro or above), or an organization that enables it for the "
            "repository. Nothing is pending and the cap is unchanged; stop and tell the user."
        ),
    }


def rejected_payload(repo: str, pr: int, reviewer: str, exc: ReviewRequestRejected) -> dict[str, Any]:
    if exc.outcome_unknown:
        return uncertain_payload(repo, pr, reviewer, exc)
    if exc.reason == "not_enabled":
        return not_enabled_payload(repo, pr, reviewer, exc)
    detail = (exc.detail or "").strip() or "no detail from GitHub"
    hints = {
        "permission": "Check that `gh` is authenticated as a user with write access to the PR.",
        "validation": "Check that this reviewer is enabled for the repository (Copilot code review needs a Pro, Pro+, Max, Business or Enterprise plan).",
        "not_found": "Check the repository and PR number, and that the token can see the repository.",
        "rate_limited": "GitHub rate limit; retry in a few minutes.",
        "transient": "GitHub-side error; retry.",
    }
    return {
        "status": "review_request_rejected",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "reviewer": reviewer,
        "reason": exc.reason,
        "http_status": exc.http_status,
        "retryable": exc.retryable,
        "message": (
            f"GitHub did not accept the review request for `{reviewer}` ({exc.reason}"
            f"{', HTTP ' + str(exc.http_status) if exc.http_status else ''}): {detail}. "
            f"{hints.get(exc.reason, '')} Nothing was posted."
        ).strip(),
    }


def state_path() -> Path:
    return loop_state.state_dir() / "state.json"


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


state_key_for = loop_state.state_key_for


# How far after the local pre-POST timestamp a timeline event may appear and
# still be the same request. GitHub stamps the event server-side; the gap is
# normally under a second.
REQUEST_MATCH_WINDOW_SECONDS = 180


def loop_request_times(repo: str, pr: int, vendor: review_vendors.ReviewVendor) -> list[str]:
    """When this loop posted a review request for ``vendor`` on this PR.

    The cap bounds what the loop posts, not what the member clicks. Both act
    under the same `gh` login, so the timeline's actor cannot tell them apart —
    three manual Copilot requests would otherwise make a first loop run stop as
    `capped`. These records are written at the moment of posting, so they are
    the loop's own provenance.
    """
    state = load_state()
    return reviewer_resolver.loop_request_times_from_entry(
        state.get(state_key_for(state, repo, pr)), vendor, loop_state.read_request_log(repo, pr)
    )


def record_rereview_posted(
    repo: str,
    pr: int,
    *,
    reviewer: str | None = None,
    requested_at: str | None = None,
    count_cycle: bool = True,
    countable: bool = True,
    request_id: str | None = None,
    comment_id: int | None = None,
    initial_review: bool = False,
) -> bool:
    """Stamp the cycle transition this re-review request completes.

    ``count_cycle=False`` records only the request's provenance: used when an
    earlier run's unconfirmed request turns out to have landed, so the cap
    counts it without advancing this run's cycle ordinal.

    The session-cycle ordinal (#104) needs to know that a cycle actually
    pushed and asked for a re-review. Deriving that from the PR's comments was
    unreliable in three separate ways (#111 review): the count is not
    monotonic under ``comments(last:100)`` on busy PRs, it silently includes
    other people's pings whenever the agent login cannot be resolved, and it
    reflects history that outlives ``clear_run_tracking()``. This write is the
    event itself — local, script-owned, exactly once per posted request.

    Fails open: a state write failure must never block the re-review that was
    already posted. The ordinal simply holds instead of drifting.
    """
    if initial_review:
        countable = False
    if request_id and any(item.get("request_id") == request_id for item in (loop_state.read_request_log(repo, pr) or [])):
        stamp_cycle(repo, pr, request_id, count=count_cycle)
        return True
    logged = True
    marked = False
    if reviewer:
        # The cap's provenance goes to the PR's append-only log, which no
        # state.json writer can overwrite. Outside entry["run"] as well,
        # which run tracking clears: it must outlive a single loop run.
        logged = loop_state.append_request_log(repo, pr, {
            "reviewer": reviewer,
            "at": requested_at or utc_now_iso(),
            # A re-review (cap-countable) or the free first request: known
            # now, so never re-derived later from clock comparisons.
            "countable": countable,
            **({"comment_id": comment_id} if comment_id is not None else {}),
            # The reservation this request was made under, so settling a
            # leftover reservation after a crash never records it twice.
            **({"request_id": request_id} if request_id else {}),
        })
        if not logged:
            # The request was posted but is unrecorded, so the local history
            # is no longer complete. Say so durably, beside the log, so the
            # cap falls back to counting by actor; nothing rewrites that file.
            print("warning: could not record the review request for the cap", file=sys.stderr)
            marked = loop_state.mark_provenance_incomplete(repo, pr)
    stamp_cycle(repo, pr, request_id if count_cycle else None, count=count_cycle)
    # Durable once the log took the record, or the incomplete marker did.
    return logged or marked


@loop_state.state_transaction
def stamp_cycle(repo: str, pr: int, request_id: str | None, *, count: bool = True) -> None:
    """Advance the run's cycle ordinal for a posted re-review, once per request.

    The request id is kept with the stamp so a replay (a crash between the
    log append and this write) can restore a missing stamp without doubling
    one that landed. ``count=False`` touches the entry without advancing.
    Fails open: the ordinal holds rather than drifting.
    """
    state = load_state()
    key = state_key_for(state, repo, pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    if count:
        stamped = run.get("stamped_request_ids")
        stamped = list(stamped) if isinstance(stamped, list) else []
        if request_id and request_id in stamped:
            return
        posted = run.get("rereviews_posted")
        posted = posted if isinstance(posted, int) and not isinstance(posted, bool) else 0
        run["rereviews_posted"] = posted + 1
        if request_id:
            run["stamped_request_ids"] = stamped + [request_id]
        entry["run"] = run
    state[key] = entry
    try:
        loop_state.save_sticky_state(state)
    except OSError as exc:
        print(f"warning: could not record the re-review stamp: {exc}", file=sys.stderr)


def cycle_stamped(repo: str, pr: int, request_id: str) -> bool:
    state = load_state()
    entry = state.get(state_key_for(state, repo, pr))
    run = entry.get("run") if isinstance(entry, dict) else None
    stamped = run.get("stamped_request_ids") if isinstance(run, dict) else None
    return isinstance(stamped, list) and request_id in stamped


# How long an unconfirmed review request may take to show up on the PR before
# its absence counts as proof that it did not land. The read-back right after
# a POST lasts seconds; GitHub's API lag is not bounded that tightly.
REGISTRATION_SETTLE_SECONDS = 120


def unconfirmed_path(repo: str, pr: int) -> Path:
    """The unconfirmed-request record sits next to the PR's lock file, apart
    from state.json, so a state file that cannot be written does not take
    this guard down with it."""
    return loop_state.unconfirmed_request_path(repo, pr)


def record_unconfirmed_request(
    repo: str, pr: int, *, reviewer: str, at: str | None, kind: str, countable: bool = True,
    request_id: str | None = None, prior: dict[str, Any] | None = None,
) -> bool:
    """Remember a request whose outcome this run could not confirm; True if saved.

    ``kind`` is ``accepted`` (GitHub accepted the call, the read-back did not see
    it yet) or ``maybe`` (5xx or no HTTP status: it may or may not have
    landed). ``countable`` says whether it was a re-review. The next run
    settles it instead of posting a possible duplicate.
    """
    path = unconfirmed_path(repo, pr)
    record = {
        "reviewer": reviewer, "at": at or utc_now_iso(), "kind": kind, "countable": countable,
        "id": request_id or uuid.uuid4().hex,
        # Vendor activity already on the PR at reservation time; settling
        # excludes it (see request_landed_since).
        "prior": prior or {"events": [], "reviews": []},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError) as exc:
        print(f"warning: could not record the unconfirmed request: {exc}", file=sys.stderr)
        return False
    return True


def read_unconfirmed_request(repo: str, pr: int) -> dict[str, Any] | None:
    """The saved unconfirmed request, None when there is none.

    A record that exists but cannot be read is ``{"unreadable": True}``, not
    None: it describes a request that may have landed, and treating it as
    absent could post a duplicate.
    """
    try:
        marker = json.loads(unconfirmed_path(repo, pr).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"unreadable": True}
    if not isinstance(marker, dict) or not isinstance(marker.get("at"), str):
        return {"unreadable": True}
    return marker


def clear_unconfirmed_request(repo: str, pr: int) -> None:
    try:
        unconfirmed_path(repo, pr).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"warning: could not clear the unconfirmed request: {exc}", file=sys.stderr)


def _remembered(payload: dict[str, Any], saved: bool) -> dict[str, Any]:
    """Say whether the unconfirmed request was saved for the next run to settle."""
    payload["remembered"] = saved
    if not saved:
        payload["message"] = (
            f"{payload.get('message', '').rstrip()} Saving a record of this request failed, "
            "so the next run cannot settle it: check the PR for a pending review request "
            "before running this again."
        )
    return payload


def read_persisted_reviewer(repo: str, pr: int) -> dict[str, Any] | None:
    state = load_state()
    entry = state.get(state_key_for(state, repo, pr))
    reviewer = entry.get("reviewer") if isinstance(entry, dict) else None
    if not isinstance(reviewer, dict):
        return None
    login = reviewer.get("login")
    if not isinstance(login, str) or not login:
        return None
    return reviewer


def resolve_reviewer_login(
    *,
    repo: str,
    pr: int,
    reviewer_login: str | None,
    reviewer_mention: str | None,
) -> str | None:
    """Which reviewer this run is about: explicit login, else the explicit
    mention, else persisted state.

    An explicit mention outranks persisted state, the same precedence
    ``resolve_review_trigger()`` uses. Reading persisted state first made the
    dispatch ignore the override in both directions: persisted Copilot plus
    ``--review-trigger-mention @codex`` requested a Copilot review, and
    persisted Codex plus ``@copilot`` posted a comment Copilot never reads.
    """
    if reviewer_login:
        return reviewer_login
    if reviewer_mention:
        return reviewer_mention.lstrip("@")
    persisted = read_persisted_reviewer(repo, pr)
    if persisted:
        return str(persisted["login"])
    return None


def resolve_review_trigger(
    *,
    repo: str,
    pr: int,
    reviewer_login: str | None,
    reviewer_mention: str | None,
) -> tuple[str | None, str]:
    if reviewer_mention:
        login = reviewer_login or reviewer_mention.lstrip("@")
        return reviewer_mention, login

    persisted = read_persisted_reviewer(repo, pr)
    if persisted:
        login = str(persisted["login"])
        trigger = persisted.get("review_trigger")
        if isinstance(trigger, str) and trigger:
            return trigger, login
        return reviewer_resolver.trigger_for(login), login

    if reviewer_login:
        return reviewer_resolver.trigger_for(reviewer_login), reviewer_login

    return DEFAULT_REVIEWER_MENTION, DEFAULT_REVIEWER_LOGIN


DEFAULT_REREVIEW_LIMIT = 3


_MENTION_RE = re.compile(r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")


def _trigger_re(mention: str) -> re.Pattern[str]:
    """Match the reviewer mention as a whole word, case-insensitively."""
    return re.compile(rf"(?<![\w/-]){re.escape(mention.strip())}(?![\w-])", re.IGNORECASE)


def mention_in(text: str) -> str | None:
    """Extract the @mention from a re-review phrase.

    The cap counts pings to a *reviewer*, so it must match on the mention, not
    on the exact sentence. Two cycles that worded the request differently are
    still two pings, and counting the full phrase would miss the earlier one and
    under-count the cap.
    """
    match = _MENTION_RE.search(text or "")
    return match.group(0) if match else None


def parse_paginated_json(stdout: str | None) -> list[Any] | None:
    """Parse `gh api --paginate` output into one list.

    Without --slurp, gh emits one JSON document per page, concatenated, so a PR
    with more than one page of comments does not parse as a single array.
    Decoding documents in sequence handles both shapes without depending on a
    gh version that has --slurp. Returns None when the output is not a sequence
    of JSON arrays, so the caller can tell "could not count" from "counted zero".
    """
    text = (stdout or "").strip()
    if not text:
        return []
    decoder = json.JSONDecoder()
    items: list[Any] = []
    index = 0
    length = len(text)
    while index < length:
        try:
            document, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            return None
        if not isinstance(document, list):
            return None
        items.extend(document)
        index = end
        while index < length and text[index].isspace():
            index += 1
    return items


def effective_cap(cli_value: int | None) -> int:
    """Resolve the re-review cap: --max-rereview-requests, else saved prefs."""
    if cli_value is not None:
        return cli_value
    try:
        prefs = judge.load_preferences()
    except Exception:  # noqa: BLE001 — a bad prefs file must not lift the cap
        return DEFAULT_REREVIEW_LIMIT
    value = prefs.get("max_rereview_requests", DEFAULT_REREVIEW_LIMIT)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_REREVIEW_LIMIT
    return value


def gh_login(runner: Any = None) -> str | None:
    """Return the gh-authenticated login, or None if it cannot be resolved."""
    if runner is None:
        runner = subprocess.run
    try:
        result = runner(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=120,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    login = (result.stdout or "").strip()
    return login or None


def count_agent_pings(
    repo: str,
    pr: int,
    trigger: str,
    agent_login: str | None,
    runner: Any = None,
) -> int | None:
    """Count existing re-review pings on the PR authored by ``agent_login``.

    Returns None when the count cannot be established. A ping posted by a human
    never counts: the cap bounds what the agent does, not what you do.
    """
    if not trigger or not agent_login:
        return None
    if runner is None:
        runner = subprocess.run
    owner, repo_name = parse_repo(repo)
    try:
        result = runner(
            ["gh", "api", "--paginate",
             f"repos/{owner}/{repo_name}/issues/{pr}/comments"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=120,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    comments = parse_paginated_json(result.stdout)
    if comments is None:
        return None
    pattern = _trigger_re(trigger)
    matching_comments = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if (comment.get("user") or {}).get("login") != agent_login:
            continue
        if pattern.search(comment.get("body") or ""):
            matching_comments.append(comment)
    state = load_state()
    entry = state.get(state_key_for(state, repo, pr))
    entry = entry if isinstance(entry, dict) else {}
    logged = loop_state.read_request_log(repo, pr)
    if entry.get("review_requests_incomplete") or entry.get("review_request_in_flight") or any(item.get("incomplete") for item in (logged or [])):
        return None
    receipts = (entry.get("review_requests") or []) + (logged or [])
    return len(reviewer_resolver.merge_comment_receipts(matching_comments, trigger, receipts))


def _gh_json(cmd: list[str], runner: Any) -> Any:
    """Run a gh command and parse its JSON stdout; None on any failure."""
    try:
        result = runner(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=120,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    if "--paginate" in cmd:
        if not (result.stdout or "").strip():
            return None
        return parse_paginated_json(result.stdout)
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return None


def _review_author(review: Any) -> str | None:
    """The review's author login, from either API shape."""
    if not isinstance(review, dict):
        return None
    author = review.get("user") or review.get("author") or {}
    return author.get("login") if isinstance(author, dict) else None


def _review_commit(review: dict[str, Any]) -> str:
    """The sha the review was submitted against, from either API shape."""
    commit = review.get("commit")
    if isinstance(commit, dict):
        return commit.get("oid") or ""
    return review.get("commit_id") or ""


def _review_submitted_at(review: dict[str, Any]) -> str:
    return review.get("submittedAt") or review.get("submitted_at") or ""


def _all_reviews(repo: str, pr: int, runner: Any) -> list[dict[str, Any]] | None:
    """Every submitted review on the PR, paginated; None when unreadable.

    GraphQL's ``reviews(last:100)`` can omit the vendor's first review (which
    moves the cap's cycle-0 boundary forward, making earlier agent requests
    look free) and its review of the current head (which allows a redundant
    request). The REST list paginates, so neither guarantee depends on a PR
    having fewer than 100 reviews.
    """
    owner, repo_name = parse_repo(repo)
    reviews = _gh_json(
        ["gh", "api", "--paginate", f"repos/{owner}/{repo_name}/pulls/{pr}/reviews"],
        runner,
    )
    if not isinstance(reviews, list):
        return None
    return [review for review in reviews if isinstance(review, dict)]


def current_head(repo: str, pr: int, runner: Any = None) -> str | None:
    """The PR's head sha on GitHub, or None when it cannot be read.

    One cheap read so `--expect-head` means the same thing on both paths: a
    ping that lands before the push is visible buys a review of the previous
    commit, which the loop then reads as this cycle's verdict.
    """
    if runner is None:
        runner = subprocess.run
    owner, repo_name = parse_repo(repo)
    # Read the PR object, not `--jq .head.sha`: that prints a bare sha, which
    # is not JSON, so the parse would fail and every head read would look
    # unreadable.
    data = _gh_json(["gh", "api", f"repos/{owner}/{repo_name}/pulls/{pr}"], runner)
    if not isinstance(data, dict):
        return None
    head = (data.get("head") or {}).get("sha")
    if isinstance(head, str) and head.strip():
        return head.strip()
    return None


def request_state(
    repo: str,
    pr: int,
    vendor: review_vendors.ReviewVendor,
    runner: Any = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any] | None:
    """Resolve the review-request state machine for ``vendor`` on the PR (spec §3).

    Returns ``{"state", "head", "pending_since", "first_completed_review_at"}``
    with ``state`` one of COMPLETED_HEAD (a submitted vendor review covers the
    head), PENDING (requested, no review for the head yet), STALE_PENDING
    (pending for at least STALE_REQUEST_SECONDS), ABSENT (nothing pending,
    head unreviewed). None when the PR cannot be read.
    """
    if runner is None:
        runner = subprocess.run
    owner, repo_name = parse_repo(repo)
    data = _gh_json(
        ["gh", "api", "graphql", "-f", f"query={STATE_QUERY}",
         "-F", f"owner={owner}", "-F", f"repo={repo_name}", "-F", f"number={pr}"],
        runner,
    )
    try:
        pull = data["data"]["repository"]["pullRequest"]
    except (KeyError, TypeError):
        return None
    if not isinstance(pull, dict):
        return None
    head = pull.get("headRefOid") or ""
    if not head:
        return None

    reviews = _all_reviews(repo, pr, runner)
    raw_events = _timeline_events(repo, pr, runner)
    if reviews is None or raw_events is None:
        # Either read failing leaves the cycle-0 boundary or the head check
        # unknown. Reporting ABSENT on a partial read would post a duplicate
        # request or treat a capped request as free, so refuse instead.
        return None

    vendor_reviews = [
        review for review in reviews
        if review_vendors.vendor_for(_review_author(review)) is vendor
    ]
    # Submitted reviews answered a request (pairing below). A dismissed one
    # was withdrawn by a maintainer, so it no longer covers the head.
    completed = [review for review in vendor_reviews if review.get("state") != "PENDING"]
    covering = [review for review in completed if review.get("state") != "DISMISSED"]
    bot_ids = {
        user["node_id"] for review in completed
        if (user := review.get("user") or {}).get("type") == "Bot"
        and isinstance(user.get("node_id"), str) and user["node_id"]
    }
    completed_head = any(_review_commit(review) == head for review in covering)

    requests = (pull.get("reviewRequests") or {}).get("nodes") or []
    listed = any(
        isinstance(node, dict)
        and review_vendors.vendor_for((node.get("requestedReviewer") or {}).get("login")) is vendor
        for node in requests
    )
    draft = any(review.get("state") == "PENDING" for review in vendor_reviews)

    events = [reviewer_resolver.normalize_request_event(node) for node in raw_events]
    vendor_events = sorted(
        (event for event in events
         if event and review_vendors.vendor_for(event["reviewer_login"]) is vendor),
        key=lambda event: event["created_at"],
    )
    completed_times = sorted(_review_submitted_at(review) or "" for review in completed)
    # Pair each completed review with the earliest open request before it;
    # what is left unpaired is still waiting. Overlapping requests keep their
    # own ages, and a listed request whose event the timeline does not show
    # yet has an unknown age instead of borrowing an old, answered event's.
    unanswered = [event["created_at"] for event in vendor_events]
    for submitted in completed_times:
        for index, requested in enumerate(unanswered):
            if requested <= submitted:
                unanswered.pop(index)
                break
    pending_since = unanswered[-1] if unanswered else None
    now = now or _dt.datetime.now(_dt.timezone.utc)
    since_dt = _parse_iso(pending_since) if pending_since else None
    # Right after Copilot submits, GraphQL can drop the request before the
    # REST reviews read shows the review. A fresh unanswered request event is
    # therefore still pending; only one older than the stale threshold, the
    # kind this script withdraws, reads as absent once it is not listed.
    fresh_unanswered = since_dt is not None and (
        (now - since_dt).total_seconds() < STALE_REQUEST_SECONDS
    )
    pending = listed or draft or fresh_unanswered
    head_reviewed_at = max(
        (_review_submitted_at(review) or "" for review in covering if _review_commit(review) == head),
        default="",
    )
    # An unanswered request newer than the head's review is a later cycle on
    # the same head: wait for it if it is listed, and if it was withdrawn
    # (the stale path, then a failed replacement) the old review still does
    # not stand for the head, so a replacement may be posted (ABSENT).
    # ``>=``: GitHub stamps to the second, and pairing already gave the head
    # review its own request, so an unpaired one in the same second is newer.
    # A listed request with no unpaired event is newer too: GitHub drops a
    # reviewer from the list when it submits, so the head review cannot have
    # answered it; its event has just not reached the timeline yet.
    newer_request = (pending_since is not None and pending_since >= head_reviewed_at) or (
        listed and pending_since is None
    )

    if completed_head and not newer_request:
        state = "COMPLETED_HEAD"
    elif pending:
        state = "PENDING"
        if since_dt is not None:
            if (now - since_dt).total_seconds() >= STALE_REQUEST_SECONDS:
                state = "STALE_PENDING"
    else:
        state = "ABSENT"
    return {
        "rerequest_ids": (pull["id"], next(iter(bot_ids))) if isinstance(pull.get("id"), str) and pull["id"] and len(bot_ids) == 1 else None,
        "state": state,
        "head": head,
        "pending_since": pending_since,
        "first_completed_review_at": reviewer_resolver.first_completed_review_at(reviews, vendor),
        # The vendor activity already on the PR, saved with a reservation so
        # settling never mistakes it for the new request landing.
        "vendor_event_times": [event["created_at"] for event in vendor_events],
        "vendor_review_times": [_review_submitted_at(review) or "" for review in completed],
    }


def _timeline_events(repo: str, pr: int, runner: Any) -> list[Any] | None:
    owner, repo_name = parse_repo(repo)
    events = _gh_json(
        ["gh", "api", "--paginate", f"repos/{owner}/{repo_name}/issues/{pr}/timeline"],
        runner,
    )
    return events if isinstance(events, list) else None


def provenance_incomplete(repo: str, pr: int) -> bool:
    """Was a posted request explicitly left unrecorded? See loop_state."""
    return loop_state.provenance_incomplete(repo, pr)


def count_agent_review_requests(
    repo: str,
    pr: int,
    vendor: review_vendors.ReviewVendor,
    agent_login: str | None,
    first_completed_review_at: str | None,
    runner: Any = None,
) -> int | None:
    """Count the agent's review requests for ``vendor`` that consume the cap.

    The REST timeline is the complete record of who clicked or called
    "request review" (paginated, unlike the fetch's GraphQL page). The rule
    itself lives in ``reviewer_resolver.counted_review_requests`` so the fetch
    and this script can never disagree. None when it cannot be established.
    """
    state = load_state()
    entry = state.get(state_key_for(state, repo, pr))
    if isinstance(entry, dict) and (entry.get("review_requests_incomplete") or entry.get("review_request_in_flight")):
        return None
    if not agent_login:
        return None
    if runner is None:
        runner = subprocess.run
    events = _timeline_events(repo, pr, runner)
    if events is None:
        return None
    recorded = loop_request_times(repo, pr, vendor)
    counted = reviewer_resolver.counted_review_requests(
        events,
        agent_login=agent_login,
        vendor=vendor,
        first_completed_review_at=first_completed_review_at,
        loop_request_times=recorded,
        match_window_seconds=REQUEST_MATCH_WINDOW_SECONDS,
    )
    if loop_state.cap_count_uncertain(repo, pr):
        # A posted request could not be logged. Its event may not have reached
        # the timeline yet, so even zero matching events is not zero used.
        return None
    if recorded is None and isinstance(entry, dict) and isinstance(entry.get("review_requests"), list):
        return None
    if recorded is None and counted:
        # No local record, yet requests under this login exist after the
        # vendor's first review. They may be the loop's (a cleared state dir)
        # or the member's own clicks, and the two findings this resolves pull
        # opposite ways: charging the member's clicks to the loop stops a first
        # run as capped, while assuming zero would let every pushed head post
        # another request past the cap. So the count is unknown and the caller
        # refuses to post — `--no-cap-check` is the deliberate override.
        return None
    if recorded is not None and len(recorded) > len(counted):
        # A countable request is durably logged but its event has not
        # surfaced on the timeline yet. It still consumed the cap, so the
        # record wins over the lagging timeline.
        return len(recorded)
    return len(counted)


class _Multiset:
    """Timestamps saved with a reservation, consumed one occurrence at a time."""

    def __init__(self, values: Any) -> None:
        self._left: dict[str, int] = {}
        for value in values or []:
            if isinstance(value, str):
                self._left[value] = self._left.get(value, 0) + 1

    def take(self, value: str) -> bool:
        """Consume one saved occurrence of ``value``; False when none is left."""
        if self._left.get(value, 0) <= 0:
            return False
        self._left[value] -= 1
        return True


def confirm_posted_after_uncertain(
    repo: str,
    pr: int,
    vendor: review_vendors.ReviewVendor,
    agent_login: str | None,
    requested_at: str | None,
    runner: Any = None,
    prior: dict[str, Any] | None = None,
) -> bool:
    """After an uncertain POST, did the agent's request land on the timeline?

    ``prior`` is the activity snapshot saved with the reservation: an event
    already on the PR before the POST, even in the same second, is not it.

    ``>=`` on the timestamp: GitHub stamps to the second, so a request that
    landed within the same second as the local pre-POST time still counts.
    """
    if not agent_login or not requested_at:
        return False
    if runner is None:
        runner = subprocess.run
    events = _timeline_events(repo, pr, runner)
    if events is None:
        return False
    old_events = _Multiset((prior or {}).get("events") or [])
    for raw in events:
        event = reviewer_resolver.normalize_request_event(raw)
        if event is None:
            continue
        if review_vendors.vendor_for(event["reviewer_login"]) is not vendor:
            continue
        if old_events.take(event["created_at"]):
            continue
        if reviewer_resolver.same_login(event["actor_login"], agent_login) and event["created_at"] >= requested_at:
            return True
    return False


def request_landed_since(
    repo: str,
    pr: int,
    vendor: review_vendors.ReviewVendor,
    agent_login: str | None,
    since: str,
    runner: Any = None,
    prior: dict[str, Any] | None = None,
) -> bool | None:
    """Did a request posted at ``since`` ever land? None when the reads fail.

    ``prior`` is the vendor activity already on the PR when the request was
    reserved (``events``/``reviews`` timestamps). The skew allowance reaches
    back before ``since``, so that activity is excluded by identity.

    Evidence is a request event for ``vendor`` by this agent at or after
    ``since``, or a submitted ``vendor`` review at or after it on any commit
    (the head may have moved on since).
    """
    if not agent_login:
        # Without the actor, a teammate's manual request is indistinguishable
        # from the loop's; attributing it would charge a free request to the cap.
        return None
    if runner is None:
        runner = subprocess.run
    events = _timeline_events(repo, pr, runner)
    if events is None:
        return None
    # ``since`` is the local clock; event and review times are GitHub's.
    since = reviewer_resolver.shift_iso(since, -reviewer_resolver.PROVENANCE_SKEW_SECONDS)
    prior = prior if isinstance(prior, dict) else {}
    # One prior occurrence hides one current occurrence of the same
    # second-precision timestamp, never all of them: a new event in the same
    # second as a saved one is still new.
    old_events = _Multiset(prior.get("events") or [])
    old_reviews = _Multiset(prior.get("reviews") or [])
    someone_else_asked = False
    for raw in events:
        event = reviewer_resolver.normalize_request_event(raw)
        if event is None or review_vendors.vendor_for(event["reviewer_login"]) is not vendor:
            continue
        if event["created_at"] < since or old_events.take(event["created_at"]):
            continue
        if reviewer_resolver.same_login(event["actor_login"], agent_login):
            return True
        someone_else_asked = True
    reviews = _all_reviews(repo, pr, runner)
    if reviews is None:
        return None
    for review in reviews:
        if review_vendors.vendor_for(_review_author(review)) is not vendor:
            continue
        submitted = _review_submitted_at(review) or ""
        if old_reviews.take(submitted):
            continue
        if review.get("state") != "PENDING" and submitted >= since:
            # A review with no request event of ours is evidence only when no
            # one else asked meanwhile; otherwise it may answer their request.
            return None if someone_else_asked else True
    return False


def request_registered(
    repo: str,
    pr: int,
    vendor: review_vendors.ReviewVendor,
    requested_at: str | None,
    runner: Any = None,
    attempts: int = 3,
    sleep: Any = None,
) -> bool | None:
    """After a 201, is ``vendor`` actually a requested reviewer on the PR?

    GitHub answers 201 and adds nobody when Copilot code review is not
    available for the repository (spec §6.1, first live run), and REST
    ``requested_reviewers`` is empty for Copilot either way (LV-3), so the
    only proof is GraphQL ``reviewRequests`` listing the vendor — or the
    review itself already sitting on the head, submitted at or after the
    request. Read back up to ``attempts`` times a second apart. None when the
    reads fail (unknown), never a guess. False means only "not seen within
    this window"; the caller treats it as unconfirmed, not as dropped.
    """
    if runner is None:
        runner = subprocess.run
    if sleep is None:
        sleep = time.sleep
    owner, repo_name = parse_repo(repo)
    for attempt in range(max(1, attempts)):
        if attempt:
            sleep(1)
        data = _gh_json(
            ["gh", "api", "graphql", "-f", f"query={STATE_QUERY}",
             "-F", f"owner={owner}", "-F", f"repo={repo_name}", "-F", f"number={pr}"],
            runner,
        )
        try:
            pull = data["data"]["repository"]["pullRequest"]
        except (KeyError, TypeError):
            return None
        if not isinstance(pull, dict):
            return None
        requests = (pull.get("reviewRequests") or {}).get("nodes") or []
        if any(
            isinstance(node, dict)
            and review_vendors.vendor_for((node.get("requestedReviewer") or {}).get("login")) is vendor
            for node in requests
        ):
            return True
        head = pull.get("headRefOid") or ""
        reviews = _all_reviews(repo, pr, runner)
        if reviews is None:
            return None
        for review in reviews:
            if review_vendors.vendor_for(_review_author(review)) is not vendor:
                continue
            if review.get("state") == "PENDING" or _review_commit(review) != head:
                continue
            if requested_at and (_review_submitted_at(review) or "") >= requested_at:
                return True
    return False


def capped_payload(repo: str, pr: int, used: int, cap: int) -> dict[str, Any]:
    return {
        "status": "capped",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "rereviews_used": used,
        "rereview_limit": cap,
        "message": (
            f"Re-review cap reached: {used} of {cap} requests already posted by "
            "this agent on this PR. Raise max_rereview_requests or pass "
            "--max-rereview-requests to continue."
        ),
    }


def state_unreadable_payload(repo: str, pr: int, reviewer: str) -> dict[str, Any]:
    """The PR's request state could not be read, so nothing was posted.

    Deliberately offers no `--no-cap-check` advice: that flag is consulted
    after this point, so the suggested retry takes the same path and still
    posts nothing. The dedupe reads are not the cap; they are what keeps a
    second request off a head that already has a review.
    """
    return {
        "status": "uncountable_state",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "reviewer": reviewer,
        "message": (
            f"The review state of {repo}#{pr} could not be read (the PR, review or "
            "timeline query failed), so a request for `" + reviewer + "` would be "
            "neither deduplicated nor counted. Nothing was posted. Check `gh auth "
            "status` and network access, then retry."
        ),
    }


def uncountable_count_payload(repo: str, pr: int, cap: int) -> dict[str, Any]:
    return {
        "status": "uncountable_count",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "rereview_limit": cap,
        "message": (
            "Cannot count prior re-review requests on this PR (gh login "
            "unresolved, or the comments query failed), so the cap of "
            f"{cap} cannot be enforced. Nothing was posted. Check `gh auth "
            "status` and retry, or pass --no-cap-check to bypass the cap."
        ),
    }


def parse_repo(value: str) -> tuple[str, str]:
    """Validate and split an ``OWNER/REPO`` value."""
    if not isinstance(value, str):
        raise ValueError("--repo must be a string")
    if value.strip() != value or value.count("/") != 1:
        raise ValueError("--repo must be in OWNER/REPO format")
    owner, repo = value.split("/", 1)
    if not owner or not repo or any(ch.isspace() for ch in value):
        raise ValueError("--repo must be in OWNER/REPO format")
    return owner, repo


def positive_pr(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("--pr must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--pr must be a positive integer")
    return parsed


def post_rereview(
    repo: str,
    pr: int,
    phrase: str,
    runner: Any = None,
    dry_run: bool = False,
    expect_head: str | None = None,
) -> dict[str, Any]:
    """Post the top-level PR comment and return the normalized result payload.

    With ``dry_run`` set, validate the arguments and report the exact comment
    body that would be posted without calling ``gh``. This is the only write
    the loop makes to someone else's PR, so it needs a preview like every
    other write in the skill.
    """
    if runner is None:
        runner = subprocess.run
    owner, repo_name = parse_repo(repo)
    if not isinstance(pr, int) or pr <= 0:
        raise ValueError("--pr must be a positive integer")

    if dry_run:
        return {
            "created_at": None,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
            "dry_run": True,
            "posted": False,
        }

    if expect_head is not None:
        try:
            ensure_expected_head(repo, pr, expect_head, runner)
        except RuntimeError as exc:
            raise ReviewRequestRejected(str(exc), reason="head_mismatch") from exc

    requested_at = utc_now_iso()
    cmd = [
        "gh",
        "api",
        f"repos/{owner}/{repo_name}/issues/{pr}/comments",
        "--method",
        "POST",
        "--raw-field",
        f"body={phrase}",
    ]
    try:
        result = runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=120)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ReviewRequestRejected(f"could not run gh api: {exc}", requested_at=requested_at) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        reason, http_status, retryable = classify_gh_failure(detail)
        raise ReviewRequestRejected(detail or "gh api failed", reason=reason, http_status=http_status,
                                    retryable=retryable, requested_at=requested_at)

    try:
        response = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ReviewRequestRejected("gh api returned invalid JSON", requested_at=requested_at) from exc
    if not isinstance(response, dict):
        raise ReviewRequestRejected("gh api returned non-object JSON", requested_at=requested_at)

    created_at = response.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise ReviewRequestRejected("gh api response missing created_at", requested_at=requested_at)

    payload = {
        "created_at": created_at,
        "repo": repo,
        "pr": pr,
        "phrase": phrase,
    }
    if type(response.get("id")) is int:
        payload["comment_id"] = response["id"]
    return confirm_posted_head(payload, repo, pr, expect_head, runner)


def request_review(
    repo: str,
    pr: int,
    request_login: str,
    runner: Any = None,
    dry_run: bool = False,
    expect_head: str | None = None,
    rerequest_ids: tuple[str, str] | None = None,
    registration_head: str | None = None,
    request_actor: str | None = None,
    request_event_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Add ``request_login`` as a requested reviewer and return the result payload.

    This is how review_request vendors (Copilot) are asked for another look:
    REST for the first request, GraphQL when prior bot IDs are known. ``created_at``
    is local UTC taken just before the POST (the response is the PR object
    and carries no event timestamp); it is narration and the reconciliation
    anchor for an uncertain outcome, never what the waiter keys on.
    """
    if runner is None:
        runner = subprocess.run
    owner, repo_name = parse_repo(repo)
    if not isinstance(pr, int) or pr <= 0:
        raise ValueError("--pr must be a positive integer")
    if not request_login:
        raise ValueError("review_request vendor has no request login")

    if dry_run:
        return {
            "created_at": None,
            "repo": repo,
            "pr": pr,
            "kind": "review_request",
            "reviewer": request_login,
            "dry_run": True,
            "posted": False,
        }

    if expect_head is not None:
        try:
            ensure_expected_head(repo, pr, expect_head, runner)
        except RuntimeError as exc:
            raise ReviewRequestRejected(str(exc), reason="head_mismatch", retryable=False) from exc
    requested_at = utc_now_iso()
    cmd = [
        "gh",
        "api",
        f"repos/{owner}/{repo_name}/pulls/{pr}/requested_reviewers",
        "--method",
        "POST",
        "--raw-field",
        f"reviewers[]={request_login}",
    ]
    if rerequest_ids:
        pull_id, bot_id = rerequest_ids
        query = "mutation($pr:ID!, $bot:ID!) { rerequestReviews(input:{pullRequestId:$pr, botIds:[$bot]}) { pullRequest { id } } }"
        cmd = ["gh", "api", "graphql", "-f", "query=" + query, "-f", "pr=" + pull_id, "-f", "bot=" + bot_id]
    try:
        result = runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=120)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ReviewRequestRejected(
            f"could not run gh api: {exc}", reason="uncertain", requested_at=requested_at
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        reason, http_status, retryable = classify_gh_failure(detail)
        raise ReviewRequestRejected(
            detail or "gh api failed",
            reason=reason,
            http_status=http_status,
            retryable=retryable,
            requested_at=requested_at,
        )
    registered = False
    if rerequest_ids:
        try:
            data = json.loads(result.stdout)
            pull = ((data.get("data") or {}).get("rerequestReviews") or {}).get("pullRequest") or {}
            if data.get("errors") or pull.get("id") != rerequest_ids[0]:
                raise ValueError("missing mutation result or GraphQL errors")
        except (ValueError, AttributeError, TypeError) as exc:
            raise ReviewRequestRejected("GraphQL re-request outcome uncertain", reason="uncertain", requested_at=requested_at) from exc
    else:
        try:
            data = json.loads(result.stdout)
            registered = any(isinstance(reviewer, dict)
                             and reviewer_resolver.canonical_login(reviewer.get("login")) == reviewer_resolver.canonical_login(request_login)
                             for reviewer in data["requested_reviewers"])
        except (ValueError, KeyError, TypeError, AttributeError):
            registered = False
    if not registered:
        for attempt in range(2):
            if attempt:
                time.sleep(1)
            if _registered_review(repo, pr, request_login, registration_head or expect_head, runner, actor=request_actor, request_event_ids=request_event_ids):
                registered = True
                break
    if not registered:
        transport = "GraphQL re-request" if rerequest_ids else "REST"
        raise ReviewRequestRejected(f"{transport} accepted the call but selected reviewer registration is unconfirmed",
                                    reason="registration_pending", http_status=200 if rerequest_ids else 201,
                                    requested_at=requested_at, outcome_unknown=True, allow_timeline_confirmation=False)
    payload = {
        "created_at": requested_at,
        "repo": repo,
        "pr": pr,
        "kind": "review_request",
        "reviewer": request_login,
        "posted": True,
        "registered": True,
    }
    return confirm_posted_head(payload, repo, pr, expect_head or registration_head, runner)


def cancel_review_request(
    repo: str,
    pr: int,
    request_login: str,
    runner: Any = None,
    expect_head: str | None = None,
) -> bool:
    """Withdraw an open review request for ``request_login``; True on success.

    A request that has been pending past ``STALE_REQUEST_SECONDS`` is probably
    abandoned, but age cannot prove it: GraphQL still lists it, and GitHub does
    not dedupe, so posting a second request while the first is live produces
    two reviews (spec §3, LV-4). Replacing it means withdrawing it first. Never
    raises — the caller must be able to refuse to post when this fails.
    """
    if runner is None:
        runner = subprocess.run
    owner, repo_name = parse_repo(repo)
    if not request_login:
        return False
    if expect_head is not None:
        try:
            ensure_expected_head(repo, pr, expect_head, runner)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return False
    cmd = [
        "gh",
        "api",
        f"repos/{owner}/{repo_name}/pulls/{pr}/requested_reviewers",
        "--method",
        "DELETE",
        "--raw-field",
        f"reviewers[]={request_login}",
    ]
    try:
        result = runner(cmd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", check=False, timeout=120)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def stale_not_replaced_payload(repo: str, pr: int, reviewer: str, pending_since: str | None) -> dict[str, Any]:
    since = f" since {pending_since}" if pending_since else ""
    return {
        "status": "stale_request_not_replaced",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "reviewer": reviewer,
        "pending_since": pending_since,
        "message": (
            f"A review request for `{reviewer}` has been pending{since} and could not "
            "be withdrawn, so no second request was posted (two live requests produce "
            "two reviews). Wait for the pending review, or remove the reviewer on the "
            "PR and run this again."
        ),
    }


# How long a run waits for another run's state-check/POST on the same PR.
LOCK_WAIT_SECONDS = 30


lock_path = loop_lock.pr_lock_path








@contextlib.contextmanager
def pr_request_lock(repo: str, pr: int) -> Iterator[bool | None]:
    manager = loop_lock.file_lock(lock_path(repo, pr), timeout=0)
    try:
        manager.__enter__()
    except loop_lock.LockBusy:
        yield False
        return
    except (OSError, ValueError):
        yield None
        return
    try:
        yield True
    finally:
        manager.__exit__(*sys.exc_info())


def unconfirmed_unreadable_payload(repo: str, pr: int, reviewer: str) -> dict[str, Any]:
    path = unconfirmed_path(repo, pr)
    return {
        "status": "unconfirmed_unreadable",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "reviewer": reviewer,
        "path": str(path),
        "message": (
            f"The record of an earlier, unconfirmed review request on {repo}#{pr} cannot "
            f"be read ({path}). That request may have landed, so nothing was posted. "
            "Check the PR for a pending or completed review request, then delete that "
            "file and run this again."
        ),
    }


def state_unwritable_payload(repo: str, pr: int, reviewer: str) -> dict[str, Any]:
    return {
        "status": "state_unwritable",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "reviewer": reviewer,
        "message": (
            f"Could not save a reservation for the review request on {repo}#{pr}, so "
            "nothing was posted: a request that is not recorded first can push the "
            "loop past its re-review cap. Fix the state directory (GGRL_STATE_DIR) "
            "and run this again."
        ),
    }


def lock_unavailable_payload(repo: str, pr: int, reviewer: str) -> dict[str, Any]:
    return {
        "status": "lock_unavailable",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "reviewer": reviewer,
        "message": (
            f"Could not take the per-PR request lock for {repo}#{pr} (state directory "
            "not writable, or no file locking on this platform), so nothing was posted: "
            "without it two runs could post duplicate review requests. Fix the state "
            "directory (GGRL_STATE_DIR) and run this again."
        ),
    }


def request_in_progress_payload(repo: str, pr: int, reviewer: str) -> dict[str, Any]:
    return {
        "status": "request_in_progress",
        "posted": False,
        "repo": repo,
        "pr": pr,
        "reviewer": reviewer,
        "message": (
            f"Another run is requesting a review from `{reviewer}` on {repo}#{pr} "
            f"right now (lock held past {LOCK_WAIT_SECONDS}s). Nothing was posted. "
            "Let that run finish, then check the PR before requesting again."
        ),
    }


def _note_withdrawal(payload: dict[str, Any]) -> dict[str, Any]:
    """Mark a refusal that followed a successful withdrawal of a stale request."""
    payload["withdrew_stale_request"] = True
    payload["message"] = (
        f"{payload.get('message', '').rstrip()} The stale pending request was "
        "withdrawn before this attempt, so nothing is pending now. Running this "
        "again requests a fresh review."
    ).strip()
    return payload


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"[loop] {payload['message']}")


def _record_settled_request(repo: str, pr: int, reviewer: str, marker: dict[str, Any]) -> None:
    """Record a reservation's request as landed, once, then release it.

    A crash after the log append but before the reservation was cleared
    leaves both; the reservation id in the log makes the replay a no-op.
    The reservation is released only once the record is durable.
    """
    request_id = marker.get("id")
    logged = loop_state.read_request_log(repo, pr) or []
    countable = marker.get("countable") is not False
    if request_id and any(item.get("request_id") == request_id for item in logged):
        # The append happened; the cycle stamp after it may not have. It is
        # idempotent per request id, so restoring it here cannot double one.
        if countable and not cycle_stamped(repo, pr, request_id):
            stamp_cycle(repo, pr, request_id)
        clear_unconfirmed_request(repo, pr)
        return
    if record_rereview_posted(
        repo, pr, reviewer=marker.get("reviewer") or reviewer,
        requested_at=marker["at"], count_cycle=countable, countable=countable,
        request_id=request_id,
    ):
        clear_unconfirmed_request(repo, pr)


def _settle_unconfirmed_request(
    args: argparse.Namespace, vendor: review_vendors.ReviewVendor, marker: dict[str, Any]
) -> dict[str, Any] | None:
    """Settle an earlier unconfirmed request while the PR shows none pending.

    Returns a payload to emit (and post nothing), or None to continue with a
    normal request. Inside the settle time: refuse. Past it: if the request
    landed, record its provenance and continue; if it never appeared, an accepted call
    is the definite not_enabled signal, and a 5xx/no-status one simply did not
    land, so a fresh request is safe.
    """
    reviewer = vendor.request_login
    at = marker["at"]
    sent = _parse_iso(at)
    now = _parse_iso(utc_now_iso())
    if sent is None or now is None or (now - sent).total_seconds() < REGISTRATION_SETTLE_SECONDS:
        return uncertain_payload(
            args.repo, args.pr, reviewer,
            ReviewRequestRejected("earlier request not settled", reason="settling", requested_at=at),
        )
    landed = request_landed_since(args.repo, args.pr, vendor, gh_login(), at, prior=marker.get("prior"))
    if landed is None:
        return uncertain_payload(
            args.repo, args.pr, reviewer,
            ReviewRequestRejected(
                "could not read the PR to settle the earlier request",
                reason="settle_unreadable", requested_at=at,
            ),
        )
    if landed:
        _record_settled_request(args.repo, args.pr, reviewer, marker)
        if read_unconfirmed_request(args.repo, args.pr) is not None:
            return uncertain_payload(
                args.repo, args.pr, reviewer,
                ReviewRequestRejected("earlier request could not be recorded durably", reason="state_unwritable", requested_at=at),
            )
        return None
    clear_unconfirmed_request(args.repo, args.pr)
    if marker.get("kind") == "accepted":
        return rejected_payload(
            args.repo, args.pr, reviewer,
            ReviewRequestRejected(
                "GitHub accepted the call but the reviewer was never added",
                reason="not_enabled", http_status=(marker.get("prior") or {}).get("http_status", 201), retryable=False, requested_at=at,
            ),
        )
    return None


def run_review_request(args: argparse.Namespace, vendor: review_vendors.ReviewVendor) -> int:
    payload = _locked_request(args)
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif "message" in payload:
        _emit(payload, json_output=False)
    else:
        print(f"[loop] {vendor.display_name} review requested at {payload.get('created_at')}")
    return 0


def _run_review_request_locked(args: argparse.Namespace, vendor: review_vendors.ReviewVendor) -> dict[str, Any]:
    """Resolve state, dedupe, cap, POST, stamp — called with the PR lock held."""
    reviewer = vendor.request_login
    if args.phrase is not None or args.reviewer_mention:
        print(
            f"warning: {vendor.display_name} is re-reviewed through a review "
            "request, not a comment; ignoring --phrase/--review-trigger-mention.",
            file=sys.stderr,
        )
    state = request_state(args.repo, args.pr, vendor)
    if state is None:
        return state_unreadable_payload(args.repo, args.pr, reviewer)
    marker = read_unconfirmed_request(args.repo, args.pr)
    if marker is not None and marker.get("unreadable"):
        return unconfirmed_unreadable_payload(args.repo, args.pr, reviewer)
    if marker is not None and state["state"] != "ABSENT" and not args.dry_run:
        # A request is listed or the head is reviewed, so the state machine
        # below sees it. The marker is forgotten only once its provenance is
        # visible (then recorded for the cap): GraphQL can list the request
        # before the timeline shows it, and a not-yet-visible request is not
        # an absent one. Otherwise it waits for the settle logic.
        if request_landed_since(args.repo, args.pr, vendor, gh_login(), marker["at"], prior=marker.get("prior")):
            _record_settled_request(args.repo, args.pr, reviewer, marker)
    elif marker is not None and not args.dry_run:
        settled = _settle_unconfirmed_request(args, vendor, marker)
        if settled is not None:
            return settled
        # Settling took reads and time; the earlier request may have become
        # visible meanwhile. Never post on the snapshot taken before it.
        state = request_state(args.repo, args.pr, vendor)
        if state is None:
            return state_unreadable_payload(args.repo, args.pr, reviewer)
    if args.expect_head and args.expect_head != state["head"]:
        return head_mismatch_payload(args.repo, args.pr, args.expect_head, state["head"])
    original_state = state["state"]
    if state["state"] == "COMPLETED_HEAD":
        return already_reviewed_payload(args.repo, args.pr, reviewer, state["head"])
    if state["state"] == "PENDING":
        return review_pending_payload(args.repo, args.pr, reviewer, state["pending_since"])
    # Cycle 0 is free, so it is not gated on the cap — nor on the reads the cap
    # needs. With no completed vendor review there is nothing to count: zero
    # would mean "no first review", not "no re-reviews", and a cap of 0 or an
    # unresolvable `gh api user` would block the very first request.
    if not args.no_cap_check and state["first_completed_review_at"]:
        cap = effective_cap(args.max_rereview_requests)
        used = count_agent_review_requests(
            args.repo, args.pr, vendor, gh_login(), state["first_completed_review_at"]
        )
        if used is None:
            return uncountable_count_payload(args.repo, args.pr, cap)
        if used >= cap:
            return capped_payload(args.repo, args.pr, used, cap)
    # The open request is withdrawn only once the cap has cleared, so a cap
    # refusal never leaves the PR with nothing pending. A dry run withdraws
    # nothing. If the replacement POST then fails, a retryable failure gets
    # one more POST (it re-creates the request); a definite failure is
    # reported as a withdrawal, never as "the old request is still there".
    if state["first_completed_review_at"] and not state.get("rerequest_ids"):
        return missing_rerequest_ids_payload(args.repo, args.pr, reviewer)
    actor = gh_login() if not args.dry_run else None
    if not args.dry_run and (state["first_completed_review_at"] or state["state"] == "STALE_PENDING") and not actor:
        return {"status": "uncountable_actor", "posted": False, "repo": args.repo, "pr": args.pr, "message": "Cannot establish the actor; nothing was cancelled or posted."}
    event_ids = _snapshot_request_event_ids(args.repo, args.pr) if not args.dry_run else None
    if not args.dry_run and (state["first_completed_review_at"] or state["state"] == "STALE_PENDING"):
        if event_ids is None:
            return {"status": "uncountable_provenance", "posted": False, "repo": args.repo, "pr": args.pr, "message": "Cannot establish request event IDs; nothing was cancelled or posted."}
    # A re-review once the vendor has completed a review; before that it is
    # the free first request. Stored with every record of this request.
    countable = bool(state["first_completed_review_at"])
    # Write-ahead reservation: once GitHub accepts the POST the request
    # exists, so its record must already be on disk. A crash before the log
    # append leaves this reservation, and the next run settles it. Taken
    # before any withdrawal, so a failed reservation leaves the PR untouched.
    request_id = uuid.uuid4().hex
    prior = {
        "events": list(state.get("vendor_event_times") or []),
        "reviews": list(state.get("vendor_review_times") or []),
        "request_event_ids": event_ids, "actor": actor, "head": state["head"],
    }
    if not args.dry_run and not record_unconfirmed_request(
        args.repo, args.pr, reviewer=reviewer, at=utc_now_iso(), kind="maybe", countable=countable,
        request_id=request_id, prior=prior,
    ):
        return state_unwritable_payload(args.repo, args.pr, reviewer)
    withdrew = False
    if state["state"] == "STALE_PENDING" and not args.dry_run:
        if not cancel_review_request(args.repo, args.pr, reviewer, expect_head=args.expect_head or state["head"]):
            clear_unconfirmed_request(args.repo, args.pr)
            return stale_not_replaced_payload(args.repo, args.pr, reviewer, state["pending_since"])
        withdrew = True
        refreshed = request_state(args.repo, args.pr, vendor)
        if refreshed is None or (args.expect_head and refreshed["head"] != args.expect_head):
            clear_unconfirmed_request(args.repo, args.pr)
            return _note_withdrawal(state_unreadable_payload(args.repo, args.pr, reviewer) if refreshed is None else head_mismatch_payload(args.repo, args.pr, args.expect_head, refreshed["head"]))
        if refreshed["state"] != "ABSENT":
            clear_unconfirmed_request(args.repo, args.pr)
            return already_reviewed_payload(args.repo, args.pr, reviewer, refreshed["head"]) if refreshed["state"] == "COMPLETED_HEAD" else stale_not_replaced_payload(args.repo, args.pr, reviewer, refreshed["pending_since"])
        if refreshed["first_completed_review_at"] and not refreshed.get("rerequest_ids"):
            clear_unconfirmed_request(args.repo, args.pr)
            return missing_rerequest_ids_payload(args.repo, args.pr, reviewer)
        if not args.no_cap_check and refreshed["first_completed_review_at"] and refreshed["first_completed_review_at"] != state["first_completed_review_at"]:
            used = count_agent_review_requests(args.repo, args.pr, vendor, actor, refreshed["first_completed_review_at"])
            if used is None or used >= effective_cap(args.max_rereview_requests):
                clear_unconfirmed_request(args.repo, args.pr)
                return uncountable_count_payload(args.repo, args.pr, effective_cap(args.max_rereview_requests)) if used is None else capped_payload(args.repo, args.pr, used, effective_cap(args.max_rereview_requests))
        state = refreshed
        countable = bool(state["first_completed_review_at"])
        prior.update(events=list(state.get("vendor_event_times") or []), reviews=list(state.get("vendor_review_times") or []), head=state["head"])
        if not record_unconfirmed_request(args.repo, args.pr, reviewer=reviewer, at=utc_now_iso(), kind="maybe", countable=countable, request_id=request_id, prior=prior):
            return _note_withdrawal(state_unwritable_payload(args.repo, args.pr, reviewer))
    try:
        payload = request_review(args.repo, args.pr, reviewer, dry_run=args.dry_run, expect_head=args.expect_head or state["head"], rerequest_ids=state.get("rerequest_ids"), registration_head=state["head"], request_actor=actor, request_event_ids=event_ids)
    except ReviewRequestRejected as exc:
        # A 5xx is not a clean refusal: GitHub can register the request and
        # still fail the response, and it does not dedupe. Never replay it;
        # reconcile it like a missing status (PR #116 review).
        maybe_landed = exc.reason in ("uncertain", "transient", "registration_pending")
        event = _new_request_event(args.repo, args.pr, vendor, actor, event_ids, subprocess.run) if maybe_landed and exc.allow_timeline_confirmation and actor and event_ids is not None else None
        if event is not None or (maybe_landed and exc.allow_timeline_confirmation and confirm_posted_after_uncertain(
            args.repo, args.pr, vendor, gh_login(), exc.requested_at, prior=prior
        )):
            payload = {
                "created_at": event["created_at"] if event is not None else exc.requested_at,
                "repo": args.repo,
                "pr": args.pr,
                "kind": "review_request",
                "reviewer": reviewer,
                "posted": True,
                "confirmed_after_uncertain": True,
                # The timeline event is the registration.
                "registered": True,
            }
        else:
            if maybe_landed:
                if exc.reason == "registration_pending":
                    prior["http_status"] = exc.http_status
                saved = record_unconfirmed_request(
                    args.repo, args.pr, reviewer=reviewer, at=exc.requested_at,
                    kind="accepted" if exc.reason == "registration_pending" else "maybe", countable=countable, request_id=request_id, prior=prior,
                )
                refused = _remembered(uncertain_payload(args.repo, args.pr, reviewer, exc), saved)
            else:
                clear_unconfirmed_request(args.repo, args.pr)
                refused = rejected_payload(args.repo, args.pr, reviewer, exc)
            if withdrew:
                refused = _note_withdrawal(refused)
            return refused
    except ValueError:
        if not args.dry_run:
            clear_unconfirmed_request(args.repo, args.pr)
        raise
    if not payload.get("dry_run") and "registered" not in payload:
        # A 201 is not proof: without Copilot code review GitHub accepts the
        # request and adds nobody (spec §6.1). Confirm before stamping, so a
        # dropped request neither consumes the cap nor starts a wait.
        registered = request_registered(args.repo, args.pr, vendor, payload.get("created_at"))
        if registered is False:
            # Not seen within the read-back window is not proof it was dropped:
            # remember it and let the next run settle it (not_enabled only once
            # the settle time has passed with nothing on the PR).
            saved = record_unconfirmed_request(
                args.repo, args.pr, reviewer=reviewer, at=payload.get("created_at"),
                kind="accepted", countable=countable, request_id=request_id, prior=prior,
            )
            unseen = uncertain_payload(
                args.repo, args.pr, reviewer,
                ReviewRequestRejected(
                    "GitHub returned 201 but the reviewer is not on the PR yet",
                    reason="registration_pending", http_status=201, retryable=False,
                    requested_at=payload.get("created_at"),
                ),
            )
            return _remembered(unseen, saved)
        if registered is None:
            saved = record_unconfirmed_request(
                args.repo, args.pr, reviewer=reviewer, at=payload.get("created_at"),
                kind="accepted", countable=countable, request_id=request_id, prior=prior,
            )
            # The read-back failed: unknown, not posted and not rejected. It is
            # not recorded as posted; the next run settles it.
            return _remembered(uncertain_payload(
                    args.repo, args.pr, reviewer,
                    ReviewRequestRejected(
                        "the requested-reviewers read-back failed",
                        reason="registration_unknown", http_status=201, retryable=False,
                        requested_at=payload.get("created_at"),
                    ),
                ), saved)
        payload["registered"] = True
    if payload.get("confirmed_after_uncertain"):
        payload = confirm_posted_head(payload, args.repo, args.pr, args.expect_head or state["head"], subprocess.run)
    payload["state"] = original_state
    payload["replaced_stale_request"] = original_state == "STALE_PENDING"
    if payload.get("dry_run"):
        payload["would_post"] = True
    else:
        try:
            if record_rereview_posted(args.repo, args.pr, reviewer=reviewer, requested_at=payload.get("created_at"),
                                     count_cycle=countable, countable=countable, request_id=request_id):
                clear_unconfirmed_request(args.repo, args.pr)
            else:
                raise RuntimeError("request log and incomplete marker could not be saved")
        except (RuntimeError, OSError) as exc:
            payload.update(status="state_record_failed", posted=True, recording_error=str(exc),
                           message="Review was posted, but recording failed. Reconcile its reservation before requesting again.")
    return payload


def _review_request(args: argparse.Namespace, vendor: review_vendors.ReviewVendor) -> dict[str, Any]:
    return _run_review_request_locked(args, vendor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Request an AI reviewer re-review by posting a PR comment or a review request."
    )
    parser.add_argument("--repo", required=True, help="GitHub repository in OWNER/REPO format.")
    parser.add_argument("--pr", required=True, type=positive_pr, help="Positive PR number.")
    parser.add_argument(
        "--phrase",
        default=None,
        help=f"Review request phrase. Default: {DEFAULT_PHRASE!r}",
    )
    parser.add_argument(
        "--reviewer-mention",
        "--review-trigger-mention",
        dest="reviewer_mention",
        default=None,
        help=(
            "Reviewer mention used when --phrase is omitted. "
            f"Default: {DEFAULT_REVIEWER_MENTION!r}."
        ),
    )
    parser.add_argument(
        "--reviewer-login",
        default=None,
        help="Reviewer login used in controlled stop messages.",
    )
    parser.add_argument(
        "--max-rereview-requests",
        type=int,
        default=None,
        help=(
            "Cap on re-review requests this agent may post on one PR. "
            "Defaults to max_rereview_requests from preferences, else 3."
        ),
    )
    parser.add_argument(
        "--no-cap-check",
        action="store_true",
        help="Skip the cap check. Only for callers that enforce the cap themselves.",
    )
    parser.add_argument(
        "--expect-head",
        default=None,
        help=(
            "The commit sha the PR head must be at (your pushed HEAD). A mismatch "
            "means the push has not landed; nothing is posted. Honored on both the "
            "review-request and the comment path."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the comment that would be posted without posting it.",
    )
    parser.add_argument(
        "--no-safe-trigger",
        action="store_true",
        help="Do not post; emit a controlled no_safe_trigger result instead.",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Confirm and record an in-flight request from GitHub evidence, without posting again.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print JSON only on stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        payload = _locked_request(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif "message" in payload:
        _emit(payload, json_output=False)
    elif payload.get("kind") == "review_request":
        if payload.get("dry_run"):
            print(f"[loop] dry run — would request a review from {payload['reviewer']} on {args.repo}#{args.pr} (state {payload['state']})")
        else:
            vendor = review_vendors.vendor_for(payload["reviewer"])
            print(f"[loop] {vendor.display_name} review requested at {payload['created_at']}")
    elif payload.get("dry_run"):
        print(f"[loop] dry run — would post to {payload['repo']}#{payload['pr']}: {payload['phrase']}")
    else:
        print(f"[loop] Re-review requested at {payload['created_at']}")
    return 0


@loop_state.state_transaction
def _mark_request_in_flight(repo: str, pr: int, pending: dict[str, Any] | None) -> None:
    state = load_state()
    key = state_key_for(state, repo, pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    if pending is None:
        entry.pop("review_request_in_flight", None)
    else:
        if entry.get("review_request_in_flight"):
            raise RuntimeError("reconcile the previous review request before requesting again")
        entry["review_request_in_flight"] = pending
    state[key] = entry
    loop_state.save_sticky_state(state)

def _record_posted_payload(args: argparse.Namespace, payload: dict[str, Any], reviewer: str | None = None, *, initial_review: bool = False) -> None:
    try:
        state = load_state()
        entry = state.get(state_key_for(state, args.repo, args.pr), {})
        marker = entry.get("review_request_in_flight") or {}
        initial_review = initial_review or marker.get("initial_review") is True
        request_id = marker.get("id") or "legacy-" + hashlib.sha256(json.dumps(marker, sort_keys=True).encode()).hexdigest()
        if not record_rereview_posted(args.repo, args.pr, reviewer=reviewer, requested_at=payload.get("created_at"), comment_id=payload.get("comment_id"),
                                     initial_review=initial_review, countable=not initial_review, count_cycle=not initial_review, request_id=request_id):
            raise RuntimeError("request log and incomplete marker could not be saved")
        _mark_request_in_flight(args.repo, args.pr, None)
    except (RuntimeError, OSError) as exc:
        payload.update(status="state_record_failed", posted=True, recording_error=str(exc),
                       message="Review was posted, but local recording failed. Reconcile its in-flight marker before requesting again.")

def _snapshot_comment_ids(repo: str, pr: int, runner: Any = None) -> list[int] | None:
    owner, repo_name = parse_repo(repo)
    comments = _gh_json(["gh", "api", "--paginate", f"repos/{owner}/{repo_name}/issues/{pr}/comments"], runner or subprocess.run)
    if not isinstance(comments, list):
        return None
    if any(not isinstance(comment, dict) or type(comment.get("id")) is not int or comment["id"] <= 0 for comment in comments):
        return None
    return sorted({comment["id"] for comment in comments})

def _snapshot_request_event_ids(repo: str, pr: int, runner: Any = None) -> list[int] | None:
    events = _timeline_events(repo, pr, runner or subprocess.run)
    if events is None:
        return None
    ids = []
    for event in events:
        if not isinstance(event, dict):
            return None
        if event.get("event") == "review_requested":
            if type(event.get("id")) is not int or event["id"] <= 0:
                return None
            ids.append(event["id"])
    return sorted(set(ids))

def _new_request_event(repo: str, pr: int, vendor: review_vendors.ReviewVendor, actor: str, baseline: Any, runner: Any) -> dict[str, Any] | None:
    if not isinstance(baseline, list) or any(type(event_id) is not int or event_id <= 0 for event_id in baseline):
        return None
    events = _timeline_events(repo, pr, runner)
    if events is None:
        return None
    prior = set(baseline)
    for raw in events:
        if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] <= 0 or raw["id"] in prior:
            continue
        event = reviewer_resolver.normalize_request_event(raw)
        if event and event["actor_login"] == actor and review_vendors.vendor_for(event["reviewer_login"]) is vendor and _parse_iso(event["created_at"]) is not None:
            return event
    return None

def _completed_registration(reviews: list[dict[str, Any]], request_login: str, head: str, after: str) -> bool:
    anchor = _parse_iso(after)
    if anchor is None:
        return False
    return any(isinstance(review.get("user"), dict) and review["user"].get("type") == "Bot"
               and review.get("state") in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
               and isinstance(review["user"].get("login"), str)
               and review["user"]["login"].casefold().removesuffix("[bot]") == request_login.casefold().removesuffix("[bot]")
               and _review_commit(review) == head
               and (submitted := _parse_iso(_review_submitted_at(review))) is not None and submitted >= anchor
               for review in reviews if isinstance(review, dict))

def ensure_expected_head(repo: str, pr: int, expect_head: str, runner: Any) -> None:
    """Best-effort check immediately before writing; GitHub has no atomic head guard."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expect_head):
        raise ValueError("--expect-head must be a full commit SHA")
    owner, repo_name = parse_repo(repo)
    info = _gh_json(["gh", "api", f"repos/{owner}/{repo_name}/pulls/{pr}"], runner)
    if not isinstance(info, dict) or info.get("state") != "open" or not isinstance(info.get("head"), dict) or info["head"].get("sha") != expect_head:
        raise RuntimeError("PR head changed or could not be established; no re-review posted")

def confirm_posted_head(payload: dict[str, Any], repo: str, pr: int, expect_head: str | None, runner: Any) -> dict[str, Any]:
    """A successful write remains posted even if its head cannot be reconfirmed."""
    if expect_head is not None:
        info = _gh_json(["gh", "api", f"repos/{repo}/pulls/{pr}"], runner)
        head_info = info.get("head") if isinstance(info, dict) else None
        head = head_info.get("sha") if isinstance(head_info, dict) else None
        if not isinstance(info, dict) or info.get("state") != "open" or head != expect_head:
            payload.update(status="review_head_unconfirmed", posted=True, expected_head=expect_head, observed_head=head,
                           message="Review request was posted, but the open PR head could not be reconfirmed. Fetch current feedback; do not automatically request another review.")
    return payload

def _registered_review(repo: str, pr: int, request_login: str, expect_head: str | None, runner: Any, *, actor: str | None = None, request_event_ids: list[int] | None = None) -> bool:
    owner, repo_name = parse_repo(repo)
    data = _gh_json(["gh", "api", "graphql", "-f", f"query={STATE_QUERY}",
                     "-F", f"owner={owner}", "-F", f"repo={repo_name}", "-F", f"number={pr}"], runner)
    try:
        pull = data["data"]["repository"]["pullRequest"]
        if data.get("errors") or (expect_head is not None and pull.get("headRefOid") != expect_head):
            return False
        if any(isinstance(node, dict) and isinstance(reviewer := node.get("requestedReviewer"), dict)
               and reviewer_resolver.canonical_login(reviewer.get("login")) == reviewer_resolver.canonical_login(request_login)
               for node in pull["reviewRequests"]["nodes"]):
            return True
    except (KeyError, TypeError, AttributeError):
        return False
    head = expect_head or pull.get("headRefOid")
    vendor = review_vendors.vendor_for(request_login)
    if not actor or request_event_ids is None or vendor is None or not isinstance(head, str) or not head:
        return False
    event = _new_request_event(repo, pr, vendor, actor, request_event_ids, runner)
    if event is None:
        return False
    reviews = _all_reviews(repo, pr, runner)
    if reviews is None:
        return False
    return _completed_registration(reviews, request_login, head, event["created_at"])

def missing_rerequest_ids_payload(repo: str, pr: int, reviewer: str) -> dict[str, Any]:
    return {"status": "uncountable_rerequest_ids", "posted": False, "repo": repo, "pr": pr, "reviewer": reviewer,
            "message": "Cannot establish PR and bot IDs for re-review; no request was posted. Restore readable, unambiguous IDs before retrying."}

def _request(args: argparse.Namespace) -> dict[str, Any]:
    if args.no_safe_trigger:
        reviewer_login = args.reviewer_login or (
            args.reviewer_mention.lstrip("@") if args.reviewer_mention else "unknown reviewer"
        )
        payload = no_safe_trigger_payload(reviewer_login)
        return payload

    # Review-request vendors (Copilot) never get a comment; resolve the vendor
    # from the login before the comment path's no-safe-trigger logic runs.
    reviewer_login = resolve_reviewer_login(
        repo=args.repo,
        pr=args.pr,
        reviewer_login=args.reviewer_login,
        reviewer_mention=args.reviewer_mention,
    )
    vendor = review_vendors.vendor_for(reviewer_login)
    if vendor is not None and vendor.trigger_kind == "review_request":
        return _review_request(args, vendor)

    # The comment path honors --expect-head too: the ping is what buys the
    # review, so it must not be posted against a head GitHub has not seen yet.
    if args.expect_head:
        head = current_head(args.repo, args.pr)
        if head is None:
            return state_unreadable_payload(args.repo, args.pr, reviewer_login or "the reviewer")
        if head != args.expect_head:
            return head_mismatch_payload(args.repo, args.pr, args.expect_head, head)

    trigger = None
    if args.phrase is not None:
        phrase = args.phrase
        trigger = args.reviewer_mention
    else:
        trigger, reviewer_login = resolve_review_trigger(
            repo=args.repo,
            pr=args.pr,
            reviewer_login=args.reviewer_login,
            reviewer_mention=args.reviewer_mention,
        )
        if trigger is None:
            payload = no_safe_trigger_payload(reviewer_login)
            return payload
        # Known vendors accept an exact phrase (Codex matches "@codex
        # review" literally); everything else gets the generic sentence.
        phrase = (
            reviewer_resolver.phrase_for(reviewer_login)
            or reviewer_resolver.phrase_for(trigger)
            or build_default_phrase(trigger)
        )
    # The cap is the loop's only guarantee that it cannot spam a PR, so it
    # is enforced here at the write itself rather than trusted to a caller.
    if not args.no_cap_check:
        cap = effective_cap(args.max_rereview_requests)
        # Count by mention, never by the full sentence: a ping worded
        # differently in an earlier cycle is still a ping.
        countable = trigger or mention_in(phrase)
        if countable is None:
            # No mention to count by. Refuse rather than post uncounted:
            # an uncountable write is an uncapped write.
            payload = {
                "status": "uncountable_trigger",
                "posted": False,
                "repo": args.repo,
                "pr": args.pr,
                "message": (
                    "Cannot count prior re-review requests: --phrase contains "
                    "no @mention to match on. Pass --reviewer-mention so the "
                    "cap can be enforced, or --no-cap-check to bypass it."
                ),
            }
            return payload
        used = count_agent_pings(args.repo, args.pr, countable, gh_login())
        if used is None:
            # The count could not be established (gh login unresolved, the
            # comments query failed, or its output was unparseable). Posting
            # anyway would make the cap advisory exactly when it matters, so
            # refuse for the same reason as an uncountable trigger.
            payload = uncountable_count_payload(args.repo, args.pr, cap)
            return payload
        if used >= cap:
            payload = capped_payload(args.repo, args.pr, used, cap)
            return payload
    actor = gh_login() if not args.dry_run else None
    if not args.dry_run and not actor:
        return {"status": "uncountable_actor", "posted": False, "repo": args.repo, "pr": args.pr,
                "message": "Cannot establish the authenticated actor for request recovery; nothing was posted."}
    comment_ids = _snapshot_comment_ids(args.repo, args.pr) if not args.dry_run else None
    if not args.dry_run and comment_ids is None:
        return {"status": "uncountable_provenance", "posted": False, "repo": args.repo, "pr": args.pr,
                "message": "Cannot establish comment IDs for recovery; nothing was posted."}
    if not args.dry_run:
        _mark_request_in_flight(args.repo, args.pr, {"reviewer": trigger or mention_in(phrase) or reviewer_login,
                                                  "at": utc_now_iso(), "actor": actor, "kind": "comment", "phrase": phrase, "id": uuid.uuid4().hex,
                                                  "comment_ids": comment_ids})
    try:
        payload = post_rereview(args.repo, args.pr, phrase, dry_run=args.dry_run, expect_head=args.expect_head)
    except ReviewRequestRejected as exc:
        if not exc.outcome_unknown and not args.dry_run:
            _mark_request_in_flight(args.repo, args.pr, None)
        return rejected_payload(args.repo, args.pr, reviewer_login or trigger or "the reviewer", exc)
    # A dry run posts nothing, so it closes no cycle.
    if payload.get("posted") is not False and not payload.get("dry_run"):
        _record_posted_payload(args, payload, trigger or mention_in(phrase) or reviewer_login)
    elif not args.dry_run:
        _mark_request_in_flight(args.repo, args.pr, None)
    return payload

def request_rereview(repo: str, pr: int, expect_head: str, max_rereview_requests: int | None = None) -> dict[str, Any]:
    """Structured API for publishing callers; shares the standalone PR lock."""
    argv = ["--repo", repo, "--pr", str(pr), "--expect-head", expect_head]
    if max_rereview_requests is not None:
        argv.extend(["--max-rereview-requests", str(max_rereview_requests)])
    return _locked_request(build_parser().parse_args(argv))

def _reconcile_request(args: argparse.Namespace, marker: Any) -> dict[str, Any]:
    payload = {"status": "request_in_flight", "posted": None, "repo": args.repo, "pr": args.pr,
               "message": "No matching GitHub evidence yet; the marker is retained. Reconcile again later; do not repost."}
    if not isinstance(marker, dict) or not marker:
        payload.update(status="no_request_in_flight", posted=False, message="No in-flight request to reconcile; nothing was posted.")
        return payload
    reviewer, at, actor = marker.get("reviewer"), marker.get("at"), marker.get("actor")
    if not isinstance(reviewer, str) or _parse_iso(at) is None or not isinstance(actor, str) or not actor:
        return payload
    vendor = review_vendors.vendor_for(reviewer)
    confirmed = {"created_at": at, "repo": args.repo, "pr": args.pr, "reviewer": reviewer,
                 "posted": True, "reconciled": True}
    if marker.get("kind") == "review_request" and vendor is not None:
        if "request_event_ids" in marker:
            event = _new_request_event(args.repo, args.pr, vendor, actor, marker["request_event_ids"], subprocess.run)
            if event is None:
                return payload
            confirmed["created_at"] = event["created_at"]
        else:
            head = marker.get("head")
            if not isinstance(head, str) or (args.expect_head and args.expect_head != head):
                return payload
            state = request_state(args.repo, args.pr, vendor)
            if state is None or state["head"] != head:
                return payload
            if not confirm_posted_after_uncertain(args.repo, args.pr, vendor, actor, at):
                return payload
            if state["state"] not in ("PENDING", "STALE_PENDING"):
                reviews = _all_reviews(args.repo, args.pr, subprocess.run)
                if reviews is None:
                    return payload
                completed = any(review_vendors.vendor_for(_review_author(review)) is vendor
                                and review.get("state") != "PENDING" and _review_commit(review) == head
                                and _review_submitted_at(review) >= at for review in reviews)
                if not completed:
                    return payload
        confirmed["kind"] = "review_request"
    elif marker.get("kind") == "comment" and isinstance(marker.get("phrase"), str):
        baseline = marker.get("comment_ids")
        if "comment_ids" in marker and (not isinstance(baseline, list) or any(type(comment_id) is not int or comment_id <= 0 for comment_id in baseline)):
            return payload
        owner, repo_name = parse_repo(args.repo)
        comments = _gh_json(["gh", "api", "--paginate", f"repos/{owner}/{repo_name}/issues/{args.pr}/comments"], subprocess.run)
        if not isinstance(comments, list):
            return payload
        match = next((comment for comment in comments if isinstance(comment, dict)
                      and isinstance(comment.get("user"), dict) and comment["user"].get("login") == actor
                      and comment.get("body") == marker["phrase"] and _parse_iso(comment.get("created_at")) is not None
                      and type(comment.get("id")) is int and comment["id"] > 0
                      and (comment["id"] not in baseline if "comment_ids" in marker else comment["created_at"] >= at)), None)
        if match is None:
            return payload
        confirmed.update(created_at=match["created_at"], comment_id=match["id"], phrase=marker["phrase"])
    else:
        return payload
    if args.dry_run:
        confirmed.update(posted=False, dry_run=True, would_record=True, message="GitHub confirms the request; dry run recorded nothing.")
        return confirmed
    _record_posted_payload(args, confirmed, reviewer, initial_review=marker.get("initial_review") is True)
    if confirmed.get("status") != "state_record_failed":
        confirmed.update(status="request_reconciled", message="Confirmed the previous request from GitHub and recorded it once; no new request was posted.")
    return confirmed

def _locked_request(args: argparse.Namespace) -> dict[str, Any]:
    parse_repo(args.repo)
    try:
        with pr_request_lock(args.repo, args.pr) as held:
            if held is None:
                return lock_unavailable_payload(args.repo, args.pr, args.reviewer_login or "the reviewer")
            if not held:
                return request_in_progress_payload(args.repo, args.pr, args.reviewer_login or "the reviewer")
            state = loop_state.load_sticky_state(strict=True)
            entry = state.get(state_key_for(state, args.repo, args.pr))
            if args.reconcile:
                marker = entry.get("review_request_in_flight") if isinstance(entry, dict) else None
                if marker:
                    return _reconcile_request(args, marker)
                reservation = read_unconfirmed_request(args.repo, args.pr)
                if reservation:
                    vendor = review_vendors.vendor_for(reservation.get("reviewer"))
                    prior = reservation.get("prior") or {}
                    event = _new_request_event(args.repo, args.pr, vendor, prior.get("actor"), prior.get("request_event_ids"), subprocess.run) if vendor and prior.get("actor") and isinstance(prior.get("request_event_ids"), list) else None
                    if not reservation.get("unreadable") and vendor and (event is not None or request_landed_since(args.repo, args.pr, vendor, gh_login(), reservation["at"], prior=prior)):
                        confirmed_at = event["created_at"] if event else reservation["at"]
                        if args.dry_run:
                            return {"posted": False, "dry_run": True, "would_record": True, "created_at": confirmed_at, "repo": args.repo, "pr": args.pr,
                                    "message": "GitHub confirms the request; dry run recorded nothing."}
                        try:
                            _record_settled_request(args.repo, args.pr, vendor.request_login, {**reservation, "at": confirmed_at})
                            if read_unconfirmed_request(args.repo, args.pr) is not None:
                                raise RuntimeError("the prior reservation could not be recorded durably")
                        except (RuntimeError, OSError) as exc:
                            return {"status": "state_record_failed", "posted": True, "created_at": confirmed_at, "repo": args.repo, "pr": args.pr,
                                    "message": f"GitHub confirms the prior request, but recording failed: {exc}. Reconcile again; do not repost."}
                        return {"status": "request_reconciled", "posted": True, "created_at": confirmed_at, "repo": args.repo, "pr": args.pr, "message": "Recorded the prior request; no new request was posted."}
                    return {"status": "request_in_flight", "posted": None, "repo": args.repo, "pr": args.pr, "message": "The previous reservation remains unconfirmed; no new request was posted."}
                return _reconcile_request(args, None)
            if isinstance(entry, dict) and entry.get("review_request_in_flight"):
                return {"status": "request_in_flight", "posted": None, "repo": args.repo, "pr": args.pr,
                        "message": "A previous review request is not fully recorded. Reconcile its in-flight marker before requesting again."}
            return _request(args)
    except loop_lock.LockBusy:
        return {"status": "busy", "posted": False, "repo": args.repo, "pr": args.pr,
                "message": "Another local publish/re-review owns this PR; retry after it finishes."}

if __name__ == "__main__":
    from loop_color import configure_utf8_output
    configure_utf8_output()
    raise SystemExit(main())
