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
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import judge
import review_vendors
import reviewer_resolver

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
      headRefOid
      reviewRequests(first:50) {
        nodes { requestedReviewer { __typename ... on Bot { login } ... on User { login } } }
      }
    }
  }
}
"""


class ReviewRequestRejected(RuntimeError):
    """The requested-reviewers POST did not succeed.

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
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.reason = reason
        self.http_status = http_status
        self.retryable = retryable
        self.requested_at = requested_at


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
        "message": (
            f"The review request for `{reviewer}` returned no HTTP status ({detail}), "
            "and no matching request was visible on the PR timeline yet, so whether "
            "GitHub accepted it is unknown. Do not request again immediately: the "
            "timeline can lag by seconds. Re-read the PR state in a minute and "
            "request again only if no review request from this agent appears."
        ),
    }


def rejected_payload(repo: str, pr: int, reviewer: str, exc: ReviewRequestRejected) -> dict[str, Any]:
    if exc.reason == "uncertain":
        return uncertain_payload(repo, pr, reviewer, exc)
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


def state_key_for(state: dict[str, Any], repo: str, pr: int) -> str:
    """The key this PR's state already lives under, whatever its casing.

    GitHub accepts `--repo owner/Repo` for a PR the fetch resolved as
    `owner/repo`, but the shared state key is built from the raw string. A
    case-mismatched write lands in a second entry, so the fetch never sees it
    — the re-review stamp goes missing and the session ordinal sticks (#111
    review). Prefer the existing entry; fall back to the literal key.
    """
    target = f"{repo}#{pr}".casefold()
    for key in state:
        if isinstance(key, str) and key.casefold() == target:
            return key
    return f"{repo}#{pr}"


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
    entry = state.get(state_key_for(state, repo, pr))
    requests = entry.get("review_requests") if isinstance(entry, dict) else None
    if not isinstance(requests, list):
        return []
    out = []
    for item in requests:
        if not isinstance(item, dict):
            continue
        if review_vendors.vendor_for(item.get("reviewer")) is not vendor:
            continue
        at = item.get("at")
        if isinstance(at, str) and at:
            out.append(at)
    return sorted(out)


def record_rereview_posted(
    repo: str,
    pr: int,
    *,
    reviewer: str | None = None,
    requested_at: str | None = None,
) -> None:
    """Stamp the cycle transition this re-review request completes.

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
    path = state_path()
    state = load_state()
    key = state_key_for(state, repo, pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    posted = run.get("rereviews_posted")
    posted = posted if isinstance(posted, int) and not isinstance(posted, bool) else 0
    run["rereviews_posted"] = posted + 1
    entry["run"] = run
    if reviewer:
        # Outside entry["run"], which run tracking clears: provenance for the
        # cap must outlive a single loop run on the PR.
        requests = entry.get("review_requests")
        requests = list(requests) if isinstance(requests, list) else []
        requests.append({"reviewer": reviewer, "at": requested_at or utc_now_iso()})
        entry["review_requests"] = requests
    state[key] = entry
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not record the re-review stamp: {exc}", file=sys.stderr)


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
            errors="replace", check=False,
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
            errors="replace", check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    comments = parse_paginated_json(result.stdout)
    if comments is None:
        return None
    pattern = _trigger_re(trigger)
    count = 0
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if (comment.get("user") or {}).get("login") != agent_login:
            continue
        if pattern.search(comment.get("body") or ""):
            count += 1
    return count


def _gh_json(cmd: list[str], runner: Any) -> Any:
    """Run a gh command and parse its JSON stdout; None on any failure."""
    try:
        result = runner(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    if "--paginate" in cmd:
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
    completed = [review for review in vendor_reviews if review.get("state") != "PENDING"]
    completed_head = any(_review_commit(review) == head for review in completed)

    requests = (pull.get("reviewRequests") or {}).get("nodes") or []
    pending = any(
        isinstance(node, dict)
        and review_vendors.vendor_for((node.get("requestedReviewer") or {}).get("login")) is vendor
        for node in requests
    ) or any(review.get("state") == "PENDING" for review in vendor_reviews)

    events = [reviewer_resolver.normalize_request_event(node) for node in raw_events]
    vendor_events = sorted(
        (event for event in events
         if event and review_vendors.vendor_for(event["reviewer_login"]) is vendor),
        key=lambda event: event["created_at"],
    )
    completed_times = [_review_submitted_at(review) for review in completed]
    pending_since = None
    if vendor_events:
        newest = vendor_events[-1]["created_at"]
        if not any(submitted > newest for submitted in completed_times):
            pending_since = newest

    if completed_head:
        state = "COMPLETED_HEAD"
    elif pending:
        state = "PENDING"
        since = _parse_iso(pending_since) if pending_since else None
        if since is not None:
            now = now or _dt.datetime.now(_dt.timezone.utc)
            if (now - since).total_seconds() >= STALE_REQUEST_SECONDS:
                state = "STALE_PENDING"
    else:
        state = "ABSENT"
    return {
        "state": state,
        "head": head,
        "pending_since": pending_since,
        "first_completed_review_at": reviewer_resolver.first_completed_review_at(reviews, vendor),
    }


def _timeline_events(repo: str, pr: int, runner: Any) -> list[Any] | None:
    owner, repo_name = parse_repo(repo)
    events = _gh_json(
        ["gh", "api", "--paginate", f"repos/{owner}/{repo_name}/issues/{pr}/timeline"],
        runner,
    )
    return events if isinstance(events, list) else None


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
    if not agent_login:
        return None
    if runner is None:
        runner = subprocess.run
    events = _timeline_events(repo, pr, runner)
    if events is None:
        return None
    return len(reviewer_resolver.counted_review_requests(
        events,
        agent_login=agent_login,
        vendor=vendor,
        first_completed_review_at=first_completed_review_at,
        loop_request_times=loop_request_times(repo, pr, vendor),
        match_window_seconds=REQUEST_MATCH_WINDOW_SECONDS,
    ))


def confirm_posted_after_uncertain(
    repo: str,
    pr: int,
    vendor: review_vendors.ReviewVendor,
    agent_login: str | None,
    requested_at: str | None,
    runner: Any = None,
) -> bool:
    """After an uncertain POST, did the agent's request land on the timeline?

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
    for raw in events:
        event = reviewer_resolver.normalize_request_event(raw)
        if event is None:
            continue
        if review_vendors.vendor_for(event["reviewer_login"]) is not vendor:
            continue
        if event["actor_login"] == agent_login and event["created_at"] >= requested_at:
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
        result = runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not run gh api: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"gh api failed{suffix}")

    try:
        response = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh api returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise RuntimeError("gh api returned non-object JSON")

    created_at = response.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise RuntimeError("gh api response missing created_at")

    return {
        "created_at": created_at,
        "repo": repo,
        "pr": pr,
        "phrase": phrase,
    }


def request_review(
    repo: str,
    pr: int,
    request_login: str,
    runner: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Add ``request_login`` as a requested reviewer and return the result payload.

    This is how review_request vendors (Copilot) are asked for another look:
    the same call for the first request and every re-request. ``created_at``
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
    try:
        result = runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
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
    return {
        "created_at": requested_at,
        "repo": repo,
        "pr": pr,
        "kind": "review_request",
        "reviewer": request_login,
        "posted": True,
    }


def cancel_review_request(
    repo: str,
    pr: int,
    request_login: str,
    runner: Any = None,
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
                        errors="replace", check=False)
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


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"[loop] {payload['message']}")


def run_review_request(args: argparse.Namespace, vendor: review_vendors.ReviewVendor) -> int:
    """The review_request path (spec §3): resolve state, dedupe, cap, POST, stamp."""
    reviewer = vendor.request_login
    json_output = args.json_output
    if args.phrase is not None or args.reviewer_mention:
        print(
            f"warning: {vendor.display_name} is re-reviewed through a review "
            "request, not a comment; ignoring --phrase/--review-trigger-mention.",
            file=sys.stderr,
        )
    state = request_state(args.repo, args.pr, vendor)
    if state is None:
        _emit(state_unreadable_payload(args.repo, args.pr, reviewer), json_output=json_output)
        return 0
    if args.expect_head and args.expect_head != state["head"]:
        _emit(head_mismatch_payload(args.repo, args.pr, args.expect_head, state["head"]), json_output=json_output)
        return 0
    if state["state"] == "COMPLETED_HEAD":
        _emit(already_reviewed_payload(args.repo, args.pr, reviewer, state["head"]), json_output=json_output)
        return 0
    if state["state"] == "PENDING":
        _emit(review_pending_payload(args.repo, args.pr, reviewer, state["pending_since"]), json_output=json_output)
        return 0
    if not args.no_cap_check:
        cap = effective_cap(args.max_rereview_requests)
        used = count_agent_review_requests(
            args.repo, args.pr, vendor, gh_login(), state["first_completed_review_at"]
        )
        if used is None:
            _emit(uncountable_count_payload(args.repo, args.pr, cap), json_output=json_output)
            return 0
        if used >= cap:
            _emit(capped_payload(args.repo, args.pr, used, cap), json_output=json_output)
            return 0
    # The open request is withdrawn only once the cap has cleared, so a refusal
    # never leaves the PR with nothing pending. A dry run withdraws nothing.
    if state["state"] == "STALE_PENDING" and not args.dry_run:
        if not cancel_review_request(args.repo, args.pr, reviewer):
            _emit(
                stale_not_replaced_payload(args.repo, args.pr, reviewer, state["pending_since"]),
                json_output=json_output,
            )
            return 0
    try:
        payload = request_review(args.repo, args.pr, reviewer, dry_run=args.dry_run)
    except ReviewRequestRejected as exc:
        if exc.reason == "uncertain" and confirm_posted_after_uncertain(
            args.repo, args.pr, vendor, gh_login(), exc.requested_at
        ):
            payload = {
                "created_at": exc.requested_at,
                "repo": args.repo,
                "pr": args.pr,
                "kind": "review_request",
                "reviewer": reviewer,
                "posted": True,
                "confirmed_after_uncertain": True,
            }
        else:
            _emit(rejected_payload(args.repo, args.pr, reviewer, exc), json_output=json_output)
            return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload["state"] = state["state"]
    payload["replaced_stale_request"] = state["state"] == "STALE_PENDING"
    if payload.get("dry_run"):
        payload["would_post"] = True
    else:
        record_rereview_posted(
            args.repo, args.pr, reviewer=reviewer, requested_at=payload.get("created_at")
        )
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif payload.get("dry_run"):
        print(
            f"[loop] dry run — would request a review from {reviewer} on "
            f"{args.repo}#{args.pr} (state {state['state']})"
        )
    else:
        print(f"[loop] {vendor.display_name} review requested at {payload['created_at']}")
    return 0


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
            "Review-request vendors only: the commit sha the PR head must be at "
            "(your pushed HEAD). A mismatch means the push has not landed; nothing is posted."
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
        "--json",
        action="store_true",
        dest="json_output",
        help="Print JSON only on stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        parse_repo(args.repo)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.no_safe_trigger:
        reviewer_login = args.reviewer_login or (
            args.reviewer_mention.lstrip("@") if args.reviewer_mention else "unknown reviewer"
        )
        payload = no_safe_trigger_payload(reviewer_login)
        if args.json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"[loop] {payload['message']}")
        return 0

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
        return run_review_request(args, vendor)

    try:
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
                if args.json_output:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(f"[loop] {payload['message']}")
                return 0
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
                if args.json_output:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(f"[loop] {payload['message']}")
                return 0
            used = count_agent_pings(args.repo, args.pr, countable, gh_login())
            if used is None:
                # The count could not be established (gh login unresolved, the
                # comments query failed, or its output was unparseable). Posting
                # anyway would make the cap advisory exactly when it matters, so
                # refuse for the same reason as an uncountable trigger.
                payload = uncountable_count_payload(args.repo, args.pr, cap)
                if args.json_output:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(f"[loop] {payload['message']}")
                return 0
            if used >= cap:
                payload = capped_payload(args.repo, args.pr, used, cap)
                if args.json_output:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(f"[loop] {payload['message']}")
                return 0
        payload = post_rereview(args.repo, args.pr, phrase, dry_run=args.dry_run)
        # A dry run posts nothing, so it closes no cycle.
        if payload.get("posted") is not False and not payload.get("dry_run"):
            record_rereview_posted(args.repo, args.pr)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif payload.get("dry_run"):
        print(f"[loop] dry run — would post to {payload['repo']}#{payload['pr']}: {payload['phrase']}")
    else:
        print(f"[loop] Re-review requested at {payload['created_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
