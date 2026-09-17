#!/usr/bin/env python3
"""Fetch thread-aware AI reviewer comments for a GitHub PR."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import hashlib
import json
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make the script's own directory importable so `from judge import ...` works
# when this script is invoked directly (e.g. `python3 .../fetch_gemini_threads.py`).
# Under `/plugin install` both files live in the same scripts/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics  # noqa: E402 — sibling module, pure/stdlib-only
import cluster_findings  # noqa: E402 — sibling module, pure/stdlib-only
import reviewer_resolver  # noqa: E402 — sibling module, pure/stdlib-only
import review_vendors  # noqa: E402 — sibling module, pure/stdlib-only
from loop_color import color_loop, colors_enabled  # noqa: E402 — sibling module
from loop_state import (  # noqa: E402 — sibling module, stdlib-only
    any_active_run,
    clear_sentinel,
    find_active_run,  # noqa: F401 — re-exported; hooks/tests import via this module
    load_sticky_state,
    migrate_legacy_state_dir,
    resolve_current_repo,
    save_sticky_state,
    sentinel_path,
    state_dir,
    sticky_state_path,  # noqa: F401 — re-exported; hooks/tests import via this module
    summary_is_stale,  # noqa: F401 — re-exported; hooks/tests import via this module
    touch_sentinel,
)
from loop_state import safe_int as _safe_int  # noqa: E402


# There is no default reviewer. An unconfigured PR resolves to "none configured"
# and the loop asks; see resolve_reviewer_selection(). These name the reviewer
# the loop may *offer*, which is a ping-first vendor so that accepting the offer
# starts a real cycle instead of a wait.
SUGGESTED_PROVIDER_NAME = review_vendors.SUGGESTED_VENDOR.display_name
SUGGESTED_AUTHOR = review_vendors.SUGGESTED_VENDOR.login
SUGGESTED_REVIEW_TRIGGER_MENTION = review_vendors.SUGGESTED_VENDOR.mention

# Back-compat aliases: --author's argparse default and the re-review trigger
# regex still need a concrete value to fall back on.
DEFAULT_PROVIDER_NAME = SUGGESTED_PROVIDER_NAME
DEFAULT_AUTHOR = SUGGESTED_AUTHOR
DEFAULT_REVIEW_TRIGGER_MENTION = SUGGESTED_REVIEW_TRIGGER_MENTION
DEFAULT_REREVIEW_LIMIT = 3


def _review_trigger_re(mention: Any) -> re.Pattern[str]:
    if not isinstance(mention, str):
        mention = DEFAULT_REVIEW_TRIGGER_MENTION
    mention = mention.strip() or DEFAULT_REVIEW_TRIGGER_MENTION
    if not mention.startswith("@"):
        mention = f"@{mention}"
    return re.compile(
        rf"{re.escape(mention)}(?![\w-]).*\breview\b",
        re.IGNORECASE | re.DOTALL,
    )


REREVIEW_TRIGGER_RE = _review_trigger_re(DEFAULT_REVIEW_TRIGGER_MENTION)

# Gemini prefixes inline review comments with a priority image whose alt text
# is the severity. Example: ![high](https://www.gstatic.com/codereviewagent/high-priority.svg)
SEVERITY_RE = re.compile(r"!\[(critical|high|medium|low)\]", re.IGNORECASE)

# Codex marks findings with a shields.io priority badge whose alt text is the
# priority. Example: ![P2 Badge](https://img.shields.io/badge/P2-yellow...)
CODEX_PRIORITY_RE = re.compile(r"!\[(P[0-3])(?:\s+Badge)?\]", re.IGNORECASE)
CODEX_PRIORITY_TO_SEVERITY = {
    "p0": "critical",
    "p1": "high",
    "p2": "medium",
    "p3": "low",
}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}

# Page sizes embedded in QUERY below. Used by the pagination guard to detect
# when we may be silently dropping data.
PAGE_LIMIT_REVIEW_THREADS = 100
PAGE_LIMIT_REVIEWS = 100
PAGE_LIMIT_PR_COMMENTS = 100
PAGE_LIMIT_THREAD_COMMENTS = 50

# Minimum reply length to count a non-bot comment as a substantive reply.
# Filters out token acks like "ok", "ack", "thanks", "👍".
ADDRESSED_BY_REPLY_MIN_CHARS = 30


@dataclass(frozen=True)
class PullRequest:
    owner: str
    repo: str
    number: int
    url: str | None = None


GH_NOT_FOUND_MESSAGE = (
    "GitHub CLI (`gh`) not found on PATH. This skill needs `gh` installed and "
    "authenticated (`gh auth login`); it does not fall back to raw API calls."
)


def run_gh(args: list[str], cwd: str | None = None) -> Any:
    try:
        proc = subprocess.run(
            ["gh", *args],
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        # A missing binary must surface as the same clean `error: ...` line the
        # rest of the script prints, not a traceback -- reviewers running the
        # skill in a sandbox without gh should get one actionable sentence.
        # subprocess raises the same exception for a missing cwd, so only
        # attribute it to gh when the OS names gh as the missing file.
        missing = str(exc.filename or "")
        if missing == "gh" or missing.endswith("/gh"):
            raise RuntimeError(GH_NOT_FOUND_MESSAGE) from exc
        raise RuntimeError(f"gh {' '.join(args)} failed: {exc}") from exc
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"gh {' '.join(args)} failed: {message}")
    output = proc.stdout.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def parse_pr_url(value: str) -> PullRequest | None:
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$", value)
    if not match:
        return None
    owner, repo, number = match.groups()
    return PullRequest(owner=owner, repo=repo, number=int(number), url=value)


def resolve_current_pr() -> PullRequest:
    run_gh(["auth", "status"])
    view = run_gh(["pr", "view", "--json", "number,url"])
    if not isinstance(view, dict) or "url" not in view or "number" not in view:
        raise RuntimeError("Could not resolve the current branch PR with gh pr view.")

    parsed = parse_pr_url(view["url"])
    if not parsed:
        raise RuntimeError(f"Could not parse PR URL: {view['url']}")
    return PullRequest(parsed.owner, parsed.repo, int(view["number"]), view["url"])


def resolve_pr(value: str | None) -> PullRequest:
    if not value:
        return resolve_current_pr()

    parsed = parse_pr_url(value)
    if parsed:
        return parsed

    shorthand = re.match(r"([^/\s]+)/([^#\s]+)#(\d+)$", value)
    if shorthand:
        owner, repo, number = shorthand.groups()
        return PullRequest(owner=owner, repo=repo, number=int(number))

    raise RuntimeError("Use a PR URL like https://github.com/OWNER/REPO/pull/123 or OWNER/REPO#123.")


def select_stats_records(
    records: list[dict[str, Any]], *, repo: str, window: int, all_repos: bool
) -> list[dict[str, Any]]:
    if not all_repos:
        repo_key = repo.casefold()
        records = [
            record
            for record in records
            if isinstance(record.get("repo"), str)
            and record["repo"].casefold() == repo_key
        ]

    return records[-window:] if window > 0 else records


QUERY = """
query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      number
      url
      title
      headRefOid
      baseRepository { nameWithOwner }
      headRepository { nameWithOwner }
      comments(last:100) {
        nodes {
          id
          author { login __typename }
          body
          createdAt
          url
        }
      }
      reviews(last:100) {
        nodes {
          id
          author { login __typename }
          body
          state
          submittedAt
          url
        }
      }
      reviewThreads(first:100) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          startLine
          originalLine
          originalStartLine
          diffSide
          comments(first:50) {
            nodes {
              id
              author { login __typename }
              body
              createdAt
              updatedAt
              url
              path
              line
              startLine
              originalLine
              originalStartLine
              diffHunk
            }
          }
        }
      }
    }
  }
}
"""

REVIEWER_DISCOVERY_QUERY = """
query($owner:String!, $repo:String!, $number:Int!, $threadsAfter:String) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      number
      url
      reviewThreads(first:100, after:$threadsAfter) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          comments(first:100) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              author { login __typename }
            }
          }
        }
      }
    }
  }
}
"""

THREAD_COMMENTS_DISCOVERY_QUERY = """
query($threadId:ID!, $commentsAfter:String) {
  node(id:$threadId) {
    ... on PullRequestReviewThread {
      comments(first:100, after:$commentsAfter) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          author { login __typename }
        }
      }
    }
  }
}
"""


RESOLVE_THREAD_MUTATION = """
mutation($threadId:ID!) {
  resolveReviewThread(input:{threadId:$threadId}) {
    thread {
      id
      isResolved
    }
  }
}
"""


def fetch_threads(pr: PullRequest) -> dict[str, Any]:
    result = run_gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={QUERY}",
            "-F",
            f"owner={pr.owner}",
            "-F",
            f"repo={pr.repo}",
            "-F",
            f"number={pr.number}",
        ]
    )
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected gh GraphQL response.")
    try:
        return result["data"]["repository"]["pullRequest"]
    except KeyError as exc:
        raise RuntimeError(f"Unexpected gh GraphQL shape: missing {exc}") from exc


def fetch_remaining_discovery_comments(
    thread_id: str,
    *,
    after: str | None,
    max_pages: int,
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    warnings: list[str] = []
    partial = False
    cursor = after
    for _ in range(max_pages):
        gh_args = [
            "api",
            "graphql",
            "-f",
            f"query={THREAD_COMMENTS_DISCOVERY_QUERY}",
            "-F",
            f"threadId={thread_id}",
        ]
        if cursor:
            gh_args.extend(["-F", f"commentsAfter={cursor}"])
        result = run_gh(gh_args)
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected gh GraphQL response.")
        try:
            comments = result["data"]["node"]["comments"]
        except KeyError as exc:
            raise RuntimeError(f"Unexpected gh GraphQL shape: missing {exc}") from exc
        page_nodes = comments.get("nodes") or []
        nodes.extend(comment for comment in page_nodes if isinstance(comment, dict))
        page_info = comments.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return {"nodes": nodes, "partial": partial, "warnings": warnings}
        cursor = page_info.get("endCursor")
        if not cursor:
            partial = True
            warnings.append(
                f"thread {thread_id} comments pagination stopped without an end cursor"
            )
            break
    else:
        partial = True
        warnings.append(f"thread {thread_id} comments hit discovery page cap")
    return {"nodes": nodes, "partial": partial, "warnings": warnings}


def fetch_reviewer_discovery(pr: PullRequest, *, max_pages: int = 20) -> dict[str, Any]:
    """Fetch review-thread comment authors for reviewer discovery.

    Normal loop fetches intentionally stay single-page for compatibility. This
    discovery-only path paginates review threads so a high-activity PR does not
    falsely appear to have no AI reviewer candidates.
    """
    all_threads: list[dict[str, Any]] = []
    warnings: list[str] = []
    after: str | None = None
    partial = False
    for _ in range(max_pages):
        gh_args = [
            "api",
            "graphql",
            "-f",
            f"query={REVIEWER_DISCOVERY_QUERY}",
            "-F",
            f"owner={pr.owner}",
            "-F",
            f"repo={pr.repo}",
            "-F",
            f"number={pr.number}",
        ]
        if after:
            gh_args.extend(["-F", f"threadsAfter={after}"])
        result = run_gh(gh_args)
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected gh GraphQL response.")
        if "errors" in result:
            raise RuntimeError(f"gh GraphQL errors: {result['errors']}")
        try:
            pull_request = result["data"]["repository"]["pullRequest"]
            if not isinstance(pull_request, dict):
                raise TypeError("pullRequest is not a dictionary")
            review_threads = pull_request.get("reviewThreads") or {}
            if not isinstance(review_threads, dict):
                raise TypeError("reviewThreads is not a dictionary")
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Unexpected gh GraphQL shape: missing or invalid data ({exc})"
            ) from exc
        nodes = review_threads.get("nodes") or []
        for thread in nodes:
            if not isinstance(thread, dict):
                continue
            comments = thread.get("comments") or {}
            page_info = comments.get("pageInfo") or {}
            comment_nodes = [
                comment
                for comment in (comments.get("nodes") or [])
                if isinstance(comment, dict)
            ]
            if page_info.get("hasNextPage"):
                thread_id = thread.get("id")
                if isinstance(thread_id, str) and thread_id:
                    remaining = fetch_remaining_discovery_comments(
                        thread_id,
                        after=page_info.get("endCursor"),
                        max_pages=max_pages,
                    )
                    comment_nodes.extend(remaining["nodes"])
                    partial = partial or bool(remaining["partial"])
                    warnings.extend(remaining["warnings"])
                else:
                    partial = True
                    warnings.append("(unknown) thread comments pagination missing thread id")
            merged_thread = dict(thread)
            merged_thread["comments"] = {"nodes": comment_nodes}
            all_threads.append(merged_thread)
        page_info = review_threads.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            merged = dict(pull_request)
            merged["reviewThreads"] = {"nodes": all_threads}
            return {"pull_request": merged, "partial": partial, "warnings": warnings}
        after = page_info.get("endCursor")
        if not after:
            partial = True
            warnings.append("reviewer discovery pagination stopped without an end cursor")
            break
    else:
        partial = True
        warnings.append("reviewer discovery hit page cap")
    return {
        "pull_request": {
            "number": pr.number,
            "url": pr.url,
            "reviewThreads": {"nodes": all_threads},
        },
        "partial": partial,
        "warnings": warnings,
    }


def resolve_thread(thread_id: str, *, dry_run: bool = False, label: str = "thread") -> None:
    if dry_run:
        print(f"[dry-run] would resolve {label} {thread_id}", file=sys.stderr)
        return
    run_gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={RESOLVE_THREAD_MUTATION}",
            "-F",
            f"threadId={thread_id}",
        ]
    )


def is_addressed_by_reply(thread: dict[str, Any], bot_author: str) -> bool:
    """An unresolved thread where a non-bot human posted a substantive reply.

    The reply is treated as a deliberate deferral (defer/wontfix/explanation).
    The loop should not retry fixing the same thread cycle after cycle.
    """
    if thread.get("isResolved") or thread.get("isOutdated"):
        return False
    for comment in (thread.get("comments") or {}).get("nodes") or []:
        login = (comment.get("author") or {}).get("login") or ""
        if not login or login == bot_author or login.endswith("[bot]"):
            continue
        body = (comment.get("body") or "").strip()
        if len(body) >= ADDRESSED_BY_REPLY_MIN_CHARS:
            return True
    return False


def filter_threads(
    pull_request: dict[str, Any],
    author: str,
    include_resolved: bool,
    include_outdated: bool,
    include_addressed_by_reply: bool = False,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for thread in pull_request.get("reviewThreads", {}).get("nodes", []):
        if thread.get("isResolved") and not include_resolved:
            continue
        if thread.get("isOutdated") and not include_outdated:
            continue
        if not include_addressed_by_reply and is_addressed_by_reply(thread, author):
            continue

        comments = thread.get("comments", {}).get("nodes", [])
        matching_comments = [
            comment
            for comment in comments
            if (comment.get("author") or {}).get("login") == author
        ]
        if not matching_comments:
            continue

        copied = dict(thread)
        copied["comments"] = matching_comments
        selected.append(copied)
    return selected


def outdated_unresolved_threads(pull_request: dict[str, Any], author: str) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    for thread in pull_request.get("reviewThreads", {}).get("nodes", []):
        if thread.get("isResolved") or not thread.get("isOutdated"):
            continue
        comments = thread.get("comments", {}).get("nodes", [])
        if any((comment.get("author") or {}).get("login") == author for comment in comments):
            threads.append(thread)
    return threads


def addressed_by_reply_threads(pull_request: dict[str, Any], author: str) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    for thread in pull_request.get("reviewThreads", {}).get("nodes", []):
        if not is_addressed_by_reply(thread, author):
            continue
        comments = thread.get("comments", {}).get("nodes", [])
        if any((comment.get("author") or {}).get("login") == author for comment in comments):
            threads.append(thread)
    return threads


def resolve_outdated_threads(
    pull_request: dict[str, Any], author: str, *, dry_run: bool = False
) -> int:
    threads = outdated_unresolved_threads(pull_request, author)
    for thread in threads:
        resolve_thread(thread["id"], dry_run=dry_run, label="outdated")
    return len(threads)


def resolve_addressed_by_reply(
    pull_request: dict[str, Any], author: str, *, dry_run: bool = False
) -> int:
    threads = addressed_by_reply_threads(pull_request, author)
    for thread in threads:
        resolve_thread(thread["id"], dry_run=dry_run, label="addressed-by-reply")
    return len(threads)


def _iter_comments(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """Return thread comments regardless of pre/post-filter shape."""
    comments = thread.get("comments")
    if isinstance(comments, list):
        return comments
    if isinstance(comments, dict):
        return comments.get("nodes", []) or []
    return []


def thread_severity(thread: dict[str, Any]) -> str:
    """Return reviewer-assigned severity (critical/high/medium/low) or 'unknown'.

    Gemini marks severity with a priority image; Codex uses a P0-P3 badge.
    Both are unambiguous, so both are read regardless of selected reviewer.
    """
    for comment in _iter_comments(thread):
        body = comment.get("body") or ""
        match = SEVERITY_RE.search(body)
        if match:
            return match.group(1).lower()
        codex_match = CODEX_PRIORITY_RE.search(body)
        if codex_match:
            return CODEX_PRIORITY_TO_SEVERITY[codex_match.group(1).lower()]
    return "unknown"


def sort_by_severity(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort threads by severity, highest first; stable for equal severities."""
    return sorted(
        threads,
        key=lambda t: SEVERITY_ORDER.get(thread_severity(t), SEVERITY_ORDER["unknown"]),
    )


def filter_by_min_severity(
    threads: list[dict[str, Any]], min_severity: str | None, *, keep_unknown: bool = True
) -> list[dict[str, Any]]:
    """Drop threads with severity strictly lower than min_severity.

    If `min_severity` is None, no minimum-severity threshold is enforced — the
    function then acts only on the `keep_unknown` axis, which lets callers use
    `--drop-unknown-severity` independently of `--min-severity`.

    `unknown` threads (no Gemini priority marker) are kept by default — dropping
    them on a strict filter would silently swallow every thread Gemini didn't
    annotate, which is the wrong default for a fast-feedback loop. Pass
    keep_unknown=False to drop them anyway.
    """
    cap = SEVERITY_ORDER[min_severity] if min_severity else None
    out: list[dict[str, Any]] = []
    for t in threads:
        sev = thread_severity(t)
        if sev == "unknown":
            if keep_unknown:
                out.append(t)
            continue
        if cap is None or SEVERITY_ORDER[sev] <= cap:
            out.append(t)
    return out


def severity_counts(threads: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for thread in threads:
        sev = thread_severity(thread)
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def derive_record_fields(
    *,
    baseline_ids: set[str],
    current_actionable_ids: set[str],
    addressed_by_reply_ids: set[str],
    outcome: str,
    judge_ran: bool,
    judge_results: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """Compute the script-derived metric counts for a run record."""
    findings_fetched = len(baseline_ids | current_actionable_ids)
    observed_fixed_count = len(
        baseline_ids - current_actionable_ids - addressed_by_reply_ids
    )
    remaining_actionable = len(current_actionable_ids)
    addressed_by_reply = len(addressed_by_reply_ids)
    if judge_ran:
        needs_human = sum(
            1
            for tid, r in judge_results.items()
            if tid in current_actionable_ids and r.get("verdict") == "needs_human"
        )
    elif outcome == "human":
        needs_human = remaining_actionable
    else:
        needs_human = 0
    return {
        "findings_fetched": findings_fetched,
        "observed_fixed_count": observed_fixed_count,
        "remaining_actionable": remaining_actionable,
        "addressed_by_reply": addressed_by_reply,
        "needs_human": needs_human,
    }


def _derive_outcome(
    remaining_actionable: int,
    verification: str,
    cap_reached: bool,
    *,
    gemini_confirmed: bool = True,
    likely_fixed_remaining: int = 0,
) -> str:
    """Best-effort outcome when the agent does not pass --outcome explicitly.

    Produces the outcomes inferable from script-visible state plus the two
    agent-supplied signals that disambiguate the terminal cycle:

    - ``gemini_confirmed`` — the final re-review actually responded after the
      re-review request (False when the final wait timed out). Defaults True so
      legacy callers are unaffected; ``clean`` is only ever returned when True.
    - ``likely_fixed_remaining`` — count of still-open threads that are
      multi-signal "likely fixed" (see metrics.classify_finding_state).

    Logic (corrections folded in):
      - verification failed                                  -> verification_failed
      - unconfirmed final wait + everything resolved/likely-fixed
                                                             -> fixed_pending_confirmation
      - cap reached (genuine unfixed remaining)              -> capped
      - no remaining + passed + gemini_confirmed             -> clean
      - else                                                 -> human

    ``regression`` / ``no_progress`` encode an agent judgment the script cannot
    see, so the agent must pass them via --outcome.
    """
    if verification == "failed":
        return "verification_failed"

    # Everything still open is multi-signal "likely fixed".
    likely_all_fixed = (
        remaining_actionable > 0 and likely_fixed_remaining >= remaining_actionable
    )
    # No open threads and the suite passed — it *looks* resolved.
    looks_resolved = remaining_actionable == 0 and verification == "passed"

    # Final wait timed out (Gemini never re-confirmed) but state otherwise looks
    # fixed: never guess clean or capped — report pending confirmation.
    if not gemini_confirmed and (likely_all_fixed or looks_resolved):
        return "fixed_pending_confirmation"

    if looks_resolved and gemini_confirmed:
        return "clean"
    if cap_reached:
        return "capped"
    return "human"


def pagination_warnings(pull_request: dict[str, Any]) -> list[str]:
    """Return human-readable warnings when any GraphQL page hit its limit.

    Hitting the limit means the script may be silently dropping data — the
    loop should surface this so the user knows to paginate or scope the PR.
    """
    warnings: list[str] = []
    pr_comments = (pull_request.get("comments") or {}).get("nodes") or []
    reviews = (pull_request.get("reviews") or {}).get("nodes") or []
    threads = (pull_request.get("reviewThreads") or {}).get("nodes") or []
    if len(pr_comments) >= PAGE_LIMIT_PR_COMMENTS:
        warnings.append(
            f"PR comments hit page limit ({PAGE_LIMIT_PR_COMMENTS}); older comments may be missing."
        )
    if len(reviews) >= PAGE_LIMIT_REVIEWS:
        warnings.append(
            f"PR reviews hit page limit ({PAGE_LIMIT_REVIEWS}); older reviews may be missing."
        )
    if len(threads) >= PAGE_LIMIT_REVIEW_THREADS:
        warnings.append(
            f"reviewThreads hit page limit ({PAGE_LIMIT_REVIEW_THREADS}); newer threads may be missing."
        )
    for thread in threads:
        comments = (thread.get("comments") or {}).get("nodes") or []
        if len(comments) >= PAGE_LIMIT_THREAD_COMMENTS:
            thread_id = thread.get("id") or "(unknown)"
            warnings.append(
                f"thread {thread_id} comments hit page limit "
                f"({PAGE_LIMIT_THREAD_COMMENTS}); newer replies may be missing."
            )
    return warnings


def filter_reviews(pull_request: dict[str, Any], author: str) -> list[dict[str, Any]]:
    return [
        review
        for review in pull_request.get("reviews", {}).get("nodes", [])
        if (review.get("author") or {}).get("login") == author
    ]


# A reviewer can decline outright — quota exhausted, service withdrawn. That is
# a top-level PR comment, not a review, so the activity fingerprint never sees
# it and the wait would otherwise burn its whole timeout on a reviewer that
# already answered. Patterns are deliberately narrow so a review that merely
# discusses rate limiting is not mistaken for a refusal.
#
# The two kinds differ in what the user can do about it: a quota cap is
# recoverable (upgrade the account or add credits, then retry the same wait),
# a withdrawn service is not. Only the recoverable kind is worth interrupting
# the user for.
REFUSAL_QUOTA = "quota_exhausted"
REFUSAL_WITHDRAWN = "withdrawn"

REVIEWER_REFUSAL_RES = (
    (REFUSAL_QUOTA, re.compile(r"reached your\b.{0,60}\blimits?\b", re.IGNORECASE | re.DOTALL)),
    (REFUSAL_QUOTA, re.compile(r"usage limits?\b.{0,40}\bcode review", re.IGNORECASE | re.DOTALL)),
    (REFUSAL_WITHDRAWN, re.compile(r"\bhas been sunset\b", re.IGNORECASE)),
    (
        REFUSAL_WITHDRAWN,
        re.compile(r"review\w*\b.{0,40}\bno longer (available|supported)\b", re.IGNORECASE | re.DOTALL),
    ),
)


def print_reviewer_refusal(
    refusal: dict[str, Any],
    *,
    author: str,
    json_output: bool,
    color_enabled: bool,
) -> None:
    """Emit the terminal stop block for a reviewer that declined to review."""
    if json_output:
        fields = {k: v for k, v in refusal.items() if k != "pull_request" and v is not None}
        print(json.dumps({"wait": fields}, indent=2, sort_keys=True))
        return
    quota = refusal.get("kind") == REFUSAL_QUOTA
    lines = [
        f"[loop] STOP — {author} refused the review: {refusal.get('reason', '')}",
        "Waiting cannot help; the reviewer already answered.",
    ]
    if refusal.get("url"):
        lines.append(f"Comment: {refusal['url']}")
    if quota:
        # Recoverable: the cap lifts the moment the user pays for it, so the
        # decision is theirs and it has to be asked now — not after a timeout.
        lines.extend(
            [
                "The cap is a billing limit, not a verdict — ask the user NOW how to proceed:",
                "  1. Stop the loop — record with: --record-run --outcome human "
                "--outcome-reason 'reviewer quota exhausted' --gemini-unconfirmed",
                f"  2. Upgrade the {reviewer_resolver.display_name_for(author) or author} "
                "account or add credits, then retry — "
                "once the user confirms, re-run this same wait command.",
                "Do not wait, do not re-ping, and do not burn a cycle re-requesting the review.",
            ]
        )
    else:
        lines.append(
            "Record the run with: --record-run --outcome human "
            "--outcome-reason 'reviewer refused the review' --gemini-unconfirmed"
        )
    print(color_loop_block("\n".join(lines), enabled=color_enabled))


class ReviewerRefused(Exception):
    """The reviewer declined to review; waiting any longer cannot help."""

    def __init__(self, refusal: dict[str, Any]) -> None:
        super().__init__(refusal.get("reason", "reviewer refused"))
        self.refusal = refusal


class WaitTimedOut(Exception):
    """A blocking ``--wait`` exhausted its ``--timeout`` budget.

    Typed so the CLI can print the deterministic timed-out heartbeat instead
    of a traceback — the blocking wait is designed to run as a background
    task, and its captured output must end in a relayable line, not a stack.
    A bare RuntimeError cannot be used: ``run_gh`` failures raise that too,
    and a network error must not masquerade as a timeout.
    """

    def __init__(self, elapsed_seconds: int) -> None:
        super().__init__(
            f"Timed out waiting for stable review activity after {elapsed_seconds} seconds."
        )
        self.elapsed_seconds = elapsed_seconds


def reviewer_refusal(
    pull_request: dict[str, Any],
    author: str,
    after_iso: str | None = None,
) -> dict[str, Any] | None:
    """Return the reviewer's refusal to review, if it posted one.

    Only the reviewer's own top-level comments count, and only those newer than
    ``after_iso`` — a refusal from a previous cycle must not end this wait.
    """
    for comment in (pull_request.get("comments") or {}).get("nodes") or []:
        if (comment.get("author") or {}).get("login") != author:
            continue
        created_at = comment.get("createdAt") or ""
        if after_iso and created_at <= after_iso:
            continue
        body = comment.get("body") or ""
        kind = next((label for label, pattern in REVIEWER_REFUSAL_RES if pattern.search(body)), None)
        if kind is None:
            continue
        return {
            "kind": kind,
            "reason": " ".join(body.replace(">", " ").split())[:200],
            "created_at": created_at,
            "url": comment.get("url"),
        }
    return None


BLOB_ANCHOR_RE = re.compile(
    r"https://github\.com/[^/\s]+/[^/\s]+/blob/[0-9a-f]+/(\S+?)#L(\d+)",
    re.IGNORECASE,
)


def review_body_findings(
    pull_request: dict[str, Any], author: str
) -> list[dict[str, Any]]:
    """Surface priority-badged findings that live in the review body.

    Codex usually publishes inline review comments, but it can put a finding
    directly in the review body, where no thread exists to fetch. Only the
    newest review counts: a later re-review supersedes older body findings, so
    these synthetic entries go stale the same way threads do.
    """
    reviews = filter_reviews(pull_request, author)
    if not reviews:
        return []
    latest = max(reviews, key=lambda review: review.get("submittedAt") or "")
    body = latest.get("body") or ""
    badges = list(CODEX_PRIORITY_RE.finditer(body))
    if not badges:
        return []

    review_id = latest.get("id") or hashlib.sha1(body.encode()).hexdigest()[:16]
    findings = []
    for index, badge in enumerate(badges):
        start = badges[index - 1].end() if index else 0
        end = badges[index + 1].start() if index + 1 < len(badges) else len(body)
        # The file anchor Codex prints just above a badge locates the finding.
        anchors = list(BLOB_ANCHOR_RE.finditer(body, start, badge.start()))
        anchor = anchors[-1] if anchors else None
        path = anchor.group(1) if anchor else "(review body)"
        line = int(anchor.group(2)) if anchor else None
        findings.append({
            "id": f"review-body:{review_id}:{index + 1}",
            "isResolved": False,
            "isOutdated": False,
            "isReviewBodyFinding": True,
            "path": path,
            "line": line,
            "startLine": None,
            "originalLine": line,
            "originalStartLine": None,
            "comments": [{
                "id": f"{review_id}:{index + 1}",
                "author": {"login": author},
                "body": body[badge.start():end].strip(),
                "createdAt": latest.get("submittedAt"),
                "updatedAt": latest.get("submittedAt"),
                "url": latest.get("url"),
                "path": path,
                "line": line,
                "startLine": None,
                "originalLine": line,
                "originalStartLine": None,
                "diffHunk": "",
            }],
        })
    return findings


def rereview_requests(
    pull_request: dict[str, Any],
    agent_login: str | None = None,
    *,
    review_trigger_mention: str | None = DEFAULT_REVIEW_TRIGGER_MENTION,
) -> list[dict[str, Any]]:
    """Count comments that ping the configured reviewer for a re-review.

    When agent_login is provided, only comments authored by that login count.
    This prevents arbitrary humans pinging Gemini from consuming the loop cap.
    """
    if not review_trigger_mention:
        return []
    out = []
    trigger_re = _review_trigger_re(review_trigger_mention)
    for comment in pull_request.get("comments", {}).get("nodes", []):
        if not trigger_re.search(comment.get("body") or ""):
            continue
        if agent_login is not None:
            login = (comment.get("author") or {}).get("login") or ""
            if login != agent_login:
                continue
        out.append(comment)
    return out


def gh_authenticated_login() -> str | None:
    """Return the gh-authenticated user login, or None if it can't be resolved."""
    try:
        result = run_gh(["api", "user", "--jq", ".login"])
    except RuntimeError:
        return None
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as err:
        raise argparse.ArgumentTypeError(
            f"invalid int value: {value!r}"
        ) from err
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def effective_rereview_limit(cli_value: int | None, prefs: dict[str, Any]) -> int:
    if cli_value is not None:
        return cli_value
    value = prefs.get("max_rereview_requests", DEFAULT_REREVIEW_LIMIT)
    if isinstance(value, bool):
        return DEFAULT_REREVIEW_LIMIT
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return DEFAULT_REREVIEW_LIMIT


def _direct_preferences_path() -> Path:
    """Mirror ``judge.prefs_path()`` for the fallback path when ``judge`` is unavailable."""
    return state_dir() / "preferences.json"


# Defaults that match the canonical ``judge.load_preferences()`` contract. The
# fallback path layers loaded values on top of this so downstream callers can
# read any documented key (``judge_mode``, ``judge_model``, etc.) without
# guarding against KeyError when the optional ``judge`` module is missing.
_FALLBACK_PREFS_DEFAULTS: dict[str, Any] = {
    "schema_version": 2,
    "judge_mode": "off",
    "judge_model": "gpt-4o-mini",
    "judge_tip_shown": False,
    "max_rereview_requests": DEFAULT_REREVIEW_LIMIT,
    "profiles": {},
    "set_at": "",
}


def load_preferences_with_fallback() -> dict[str, Any]:
    """Load user preferences, falling back to a direct JSON read.

    Primary path: ``judge.load_preferences()`` (canonical loader with full
    schema validation). If the optional ``judge`` module cannot be imported
    (minimal install, broken install, vendored layout), read
    ``preferences.json`` directly so persistent settings like
    ``max_rereview_requests`` still take effect. Any failure is surfaced to
    stderr with a hint rather than silently dropped.

    The returned dict is always populated with the documented preference keys
    (defaults from ``_FALLBACK_PREFS_DEFAULTS`` overlaid by saved values), so
    callers can read keys like ``judge_model`` without KeyError handling.
    """
    try:
        from judge import load_preferences  # noqa: PLC0415
    except ImportError:
        print(
            "warning: optional 'judge' module not importable; reading "
            "preferences.json directly. Reinstall gh-review-loop "
            "to restore full judge-eval support.",
            file=sys.stderr,
        )
    else:
        try:
            return load_preferences()
        except Exception as err:
            print(
                f"warning: judge.load_preferences() failed ({err!s}); "
                "falling back to direct preferences.json load.",
                file=sys.stderr,
            )

    path = _direct_preferences_path()
    if not path.exists():
        prefs = dict(_FALLBACK_PREFS_DEFAULTS)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(prefs, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError:
            pass
        return prefs
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        print(
            f"warning: could not read {path} ({err!s}); ignoring saved preferences.",
            file=sys.stderr,
        )
        return dict(_FALLBACK_PREFS_DEFAULTS)
    if not isinstance(data, dict):
        print(
            f"warning: {path} did not contain a JSON object; ignoring saved preferences.",
            file=sys.stderr,
        )
        return dict(_FALLBACK_PREFS_DEFAULTS)
    return {**_FALLBACK_PREFS_DEFAULTS, **data}


def post_pr_comment(pr: PullRequest, body: str, *, dry_run: bool = False) -> None:
    """Post a comment on the PR via gh."""
    pr_ref = f"{pr.owner}/{pr.repo}#{pr.number}"
    if dry_run:
        print(f"[dry-run] would post receipt to {pr_ref}:\n{body}", file=sys.stderr)
        return
    proc = subprocess.run(
        ["gh", "pr", "comment", str(pr.number), "--repo", f"{pr.owner}/{pr.repo}", "--body", body],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"gh pr comment failed: {message}")


# ---------------------------------------------------------------------------
# Sticky receipt: a single PR comment that gets edited in place across
# multiple loop invocations on the same PR. Provides background visibility
# in the GitHub UI without spamming new comments per cycle.
# ---------------------------------------------------------------------------

# Marker embedded in the rendered body so a sticky receipt can be identified
# even if the local state file is wiped. Used as a fallback discovery key.
STICKY_RECEIPT_MARKER = "<!-- gh-review-loop:sticky-receipt -->"


# State-file primitives (sticky_state_path/load/save, active-run queries,
# the loop-active sentinel) live in loop_state so the hook gates can import
# them without paying this module's full import cost. Re-exported above.


def _state_key(pr: PullRequest) -> str:
    return f"{pr.owner}/{pr.repo}#{pr.number}"


def read_reviewer_selection(pr: PullRequest) -> dict[str, Any] | None:
    entry = load_sticky_state().get(_state_key(pr), {})
    reviewer = entry.get("reviewer") if isinstance(entry, dict) else None
    if not isinstance(reviewer, dict):
        return None
    login = reviewer.get("login")
    if not isinstance(login, str) or not login:
        return None
    return dict(reviewer)


def save_reviewer_selection(pr: PullRequest, reviewer: dict[str, Any]) -> None:
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry["reviewer"] = dict(reviewer)
    state[key] = entry
    save_sticky_state(state)


def clear_reviewer_selection(pr: PullRequest) -> bool:
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    if not isinstance(entry, dict) or "reviewer" not in entry:
        return False
    del entry["reviewer"]
    state[key] = entry
    save_sticky_state(state)
    return True


def read_render_baseline(pr: PullRequest, author: str) -> dict[str, str]:
    """thread_id → render_fp emitted on the previous cycle, per (pr, author).

    The delta collapse (#95) compares against the *immediately previous* cycle,
    never a seen-ever set — that is what makes an A→B→A thread render full on
    the third cycle. Empty when never saved (cycle 1 → everything full).
    """
    entry = load_sticky_state().get(_state_key(pr), {})
    if not isinstance(entry, dict):
        return {}
    baselines = entry.get("render_baseline")
    if not isinstance(baselines, dict):
        return {}
    fps = baselines.get(author)
    if not isinstance(fps, dict):
        return {}
    return {k: v for k, v in fps.items() if isinstance(k, str) and isinstance(v, str)}


def save_render_baseline(pr: PullRequest, author: str, fps: dict[str, str]) -> None:
    """Replace the (pr, author) render baseline with this cycle's fingerprints.

    Call ONLY after the rendered output was successfully emitted — persisting
    before emission would collapse threads the agent never saw. Replacement
    (not merge) keeps the baseline scoped to the previous cycle.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    baselines = entry.get("render_baseline")
    baselines = dict(baselines) if isinstance(baselines, dict) else {}
    baselines[author] = dict(fps)
    entry["render_baseline"] = baselines
    state[key] = entry
    save_sticky_state(state)


def reviewer_selection_state(record: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Project a persisted reviewer record into the loop's selection state.

    Vendor facts are re-derived from the login rather than trusted from the
    record, so selections persisted before a vendor was known (Codex records
    written with ``review_trigger: null``) heal instead of permanently
    disabling re-review requests for that PR.
    """
    login = record["login"]
    known_display_name = reviewer_resolver.display_name_for(login)
    display_name = record.get("display_name") or known_display_name
    if reviewer_resolver.trigger_for(login):
        display_name = known_display_name
    return {
        "login": login,
        "display_name": display_name,
        "review_trigger": record.get("review_trigger") or reviewer_resolver.trigger_for(login),
        "auto_reviews": reviewer_resolver.auto_reviews(login),
        "source": source,
        "confirmation_required": source in {"default_unconfirmed", "none_configured"},
        # True whenever an actual choice backs the selection. A suggestion the
        # user has not accepted is not a configuration.
        "configured": source not in {"default_unconfirmed", "none_configured"},
        "candidates_partial": False,
    }


def resolve_reviewer_selection(args: argparse.Namespace, pr: PullRequest) -> dict[str, Any]:
    login = args.reviewer or args.author
    if login:
        source = args.reviewer_source if args.reviewer else "explicit"
        record = reviewer_resolver.make_reviewer_record(
            login,
            display_name=args.reviewer_name,
            review_trigger=args.review_trigger_mention,
            source=source,
        )
        save_reviewer_selection(pr, record)
        return reviewer_selection_state(record, source=source)

    persisted = read_reviewer_selection(pr)
    if persisted:
        return reviewer_selection_state(persisted, source="persisted")

    # Nothing discovered, nothing persisted. Do not guess a vendor. Any default
    # is wrong for someone: assuming a bot that reviews on its own turns "no
    # reviewer here" into a full-timeout wait, and assuming a ping-first bot
    # posts a mention on the user's PR for a reviewer they never chose. Report
    # the state and let the caller ask.
    #
    # SUGGESTED_* is what the caller may *offer*, not what it may assume. It is
    # a ping-first vendor precisely so that accepting the offer produces a real
    # cycle 0 instead of a wait.
    record = reviewer_resolver.make_reviewer_record(
        SUGGESTED_AUTHOR,
        display_name=SUGGESTED_PROVIDER_NAME,
        review_trigger=SUGGESTED_REVIEW_TRIGGER_MENTION,
        source="none_configured",
    )
    state = reviewer_selection_state(record, source="none_configured")
    state["configured"] = False
    state["unconfirmed"] = True
    state["suggestion"] = {
        "login": SUGGESTED_AUTHOR,
        "display_name": SUGGESTED_PROVIDER_NAME,
        "review_trigger": SUGGESTED_REVIEW_TRIGGER_MENTION,
        "auto_reviews": reviewer_resolver.auto_reviews(SUGGESTED_AUTHOR),
    }
    state["message"] = (
        "No reviewer bot has commented on this PR and none is configured. "
        "Ask the user to pick one, or to ping "
        f"{SUGGESTED_REVIEW_TRIGGER_MENTION} to start a review. Do not wait on "
        "an unconfirmed reviewer."
    )
    return state


def update_run_tracking(
    pr: PullRequest,
    findings: list[tuple[str, str | None]],
    *,
    bump_session_cycle: bool = True,
) -> None:
    """Merge this invocation's findings into the run's tracking state.

    ``findings`` is a list of (thread_id, path) pairs. Sets ``started_at`` on
    the first call of a run; unions ids/paths on every call. Stored under the
    existing ``owner/repo#number`` key so it rides alongside sticky state.
    ``bump_session_cycle=False`` (the --cycle-summary path) keeps the
    session-cycle ordinal frozen: a read-only summary re-run must not start a
    new cycle.

    The session-cycle ordinal advances on evidence that the previous cycle
    actually *transitioned* (pushed and asked for a re-review), not merely
    that it emitted a summary — see the rules below.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key, {})
    run = entry.get("run", {})
    if "started_at" not in run:
        run["started_at"] = _now_iso()
    # Monotonic counter bumped on every fetch. The loop's Stop-hook backstop
    # compares it against last_summary_seq to tell whether the run advanced
    # since the agent last emitted a summary (see summary_is_stale).
    prev_seq = _safe_int(run.get("update_seq", 0))
    run["update_seq"] = prev_seq + 1
    # Script-owned session-cycle ordinal (#104): 1 on the run's first fetch,
    # +1 only once the previous cycle has demonstrably ENDED. Narration copies
    # this number; the model must never compute it — model-tracked counters
    # drift across context compaction.
    #
    # A cycle ends when it emitted its summary AND transitioned through a new
    # re-review request. The re-review count is the transition evidence: a
    # session interrupted after `--cycle-summary` but before the push/re-review
    # resumes with a `--full` recovery fetch, which is still *finishing the
    # same cycle*. Treating the summary alone as the boundary mislabeled that
    # fetch (and every later narration line and metric) as a new cycle (#111
    # review). Summary staleness is deliberately NOT part of this test: a
    # recovery fetch advances update_seq past last_summary_seq, which would
    # then block the legitimate bump for the rest of the run.
    # Transition evidence is LOCAL: `request_rereview.py` bumps
    # `rereviews_posted` exactly once per request it actually posts. Deriving
    # it from the PR's comments failed three different ways (#111 review) —
    # `comments(last:100)` makes the count non-monotonic on busy PRs, the
    # all-user fallback count leaks other people's pings when the agent login
    # cannot be resolved, and PR history outlives `clear_run_tracking()`.
    #
    # The summary is CONSUMED when it advances a cycle. `last_summary_seq > 0`
    # was a permanently-true flag once any summary had been emitted, so a
    # second re-review arriving before the new cycle wrote its own summary
    # advanced the ordinal twice off one stale summary (#111 review).
    posted = _safe_int(run.get("rereviews_posted", 0))
    posted_baseline = _safe_int(run.get("rereviews_posted_at_cycle_start", 0))
    summary_seq = _safe_int(run.get("last_summary_seq", 0))
    summary_consumed = _safe_int(run.get("summary_consumed_seq", 0))
    if "session_cycle" not in run:
        run["session_cycle"] = 1
        run["rereviews_posted_at_cycle_start"] = posted
    elif (
        bump_session_cycle
        and summary_seq > summary_consumed
        and posted > posted_baseline
    ):
        run["session_cycle"] = _safe_int(run.get("session_cycle")) + 1
        run["rereviews_posted_at_cycle_start"] = posted
        run["summary_consumed_seq"] = summary_seq
    ids = set(run.get("finding_ids", []))
    paths = set(run.get("finding_paths", []))
    for thread_id, path in findings:
        if thread_id:
            ids.add(thread_id)
        if path:
            paths.add(path)
    run["finding_ids"] = sorted(ids)
    run["finding_paths"] = sorted(paths)
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)
    # Freshen the loop-active sentinel so the hooks.json shell guard lets the
    # gates run for the duration of this loop (and the 24h TTL restarts).
    # Without the marker the guard exits before Python starts, so the gates are
    # off even though this run is active — never let that happen silently.
    if not touch_sentinel():
        print(
            f"warning: could not write the loop-active sentinel at {sentinel_path()}. "
            "The loop continues, but its edit gate, push gate, and Stop-hook "
            "summary backstop stay disabled until the sentinel can be created.",
            file=sys.stderr,
        )


def read_run_tracking(pr: PullRequest) -> dict[str, Any]:
    return load_sticky_state().get(_state_key(pr), {}).get("run", {})


def clear_run_tracking(pr: PullRequest) -> None:
    """Tear down the run accumulator and the delta baseline for this PR.

    The render baseline (#95) is only meaningful while the full render it
    fingerprints is still in the agent's context. Terminal completion ends that
    context, so leaving the baseline behind would collapse threads a *later*
    session has never seen down to URL-only stubs on its very first fetch.
    Clearing both together makes the next run start from a full render.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    if isinstance(entry, dict):
        stale = [field for field in ("run", "render_baseline") if field in entry]
        for field in stale:
            del entry[field]
        if stale:
            save_sticky_state(state)
    # Drop the sentinel only when no loop remains anywhere — another repo's
    # concurrent loop still needs the hooks live.
    if not any_active_run():
        clear_sentinel()


def begin_wait_chunk(pr: PullRequest, after_iso: str | None) -> dict[str, Any]:
    """Open one wait chunk: load cross-chunk wait state, applying the reset rule.

    Reset rule (prevents cross-cycle leakage): if the stored anchor differs
    from ``after_iso``, all wait progress (started_at, checks, settle state)
    belongs to a previous cycle's wait and is discarded. ``checks`` counts
    chunk invocations, incremented once per call. Fails open: state I/O
    errors yield a fresh single-chunk state rather than crashing the wait.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    wait = run.get("wait")
    wait = dict(wait) if isinstance(wait, dict) else {}
    if wait.get("after") != after_iso:
        wait = {"after": after_iso}
    if not isinstance(wait.get("started_at"), str) or not wait.get("started_at"):
        wait["started_at"] = metrics.now_iso()
    wait["checks"] = _safe_int(wait.get("checks")) + 1
    run["wait"] = wait
    entry["run"] = run
    state[key] = entry
    try:
        save_sticky_state(state)
    except OSError as exc:
        print(f"warning: could not persist wait state: {exc}", file=sys.stderr)
    return wait


def read_wait_state(pr: PullRequest) -> dict[str, Any]:
    run = read_run_tracking(pr)
    wait = run.get("wait") if isinstance(run, dict) else None
    return dict(wait) if isinstance(wait, dict) else {}


def _update_wait_state(pr: PullRequest, updates: dict[str, Any]) -> None:
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    wait = run.get("wait")
    wait = dict(wait) if isinstance(wait, dict) else {}
    wait.update(updates)
    run["wait"] = wait
    entry["run"] = run
    state[key] = entry
    try:
        save_sticky_state(state)
    except OSError as exc:
        print(f"warning: could not persist wait state: {exc}", file=sys.stderr)


def save_wait_settle(pr: PullRequest, fingerprint: str, since_iso: str) -> None:
    """Persist the settle phase so a chunk boundary never restarts the quiet period."""
    _update_wait_state(pr, {"stable_fingerprint": fingerprint, "stable_since": since_iso})


def save_wait_snapshot(pr: PullRequest, snapshot: dict[str, Any]) -> None:
    """Persist the last non-ready chunk result for --wait-heartbeat rendering."""
    _update_wait_state(pr, {"last_snapshot": dict(snapshot)})


def clear_wait_state(pr: PullRequest) -> None:
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("run"), dict) and "wait" in entry["run"]:
        del entry["run"]["wait"]
        try:
            save_sticky_state(state)
        except OSError as exc:
            print(f"warning: could not clear wait state: {exc}", file=sys.stderr)


WAIT_FIRST_CHUNK_SECONDS = 60
WAIT_SECOND_CHUNK_SECONDS = 300
WAIT_LATER_CHUNK_SECONDS = 900

# Cadence of the one-line liveness heartbeat a blocking --wait writes to
# stderr while polling — the signal a user inspects when the wait runs as a
# background task.
WAIT_HEARTBEAT_INTERVAL_SECONDS = 60


def _parse_iso_utc(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except ValueError:
        return None


def wait_elapsed_seconds(
    wait: dict[str, Any],
    after_iso: str | None,
    now: _dt.datetime | None = None,
) -> int:
    """Total wait elapsed, robust to state loss.

    ``max(now - started_at, now - after)``: if sticky state is corrupted or
    deleted, a fresh started_at cannot silently restart the --timeout budget —
    the --after anchor still bounds the total. Cycle 1 has no anchor and falls
    back to started_at alone (acceptable: the cycle-1 fast path returns on
    first detected activity).
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    candidates = []
    for value in (wait.get("started_at"), after_iso):
        parsed = _parse_iso_utc(value)
        if parsed is not None:
            candidates.append((now - parsed).total_seconds())
    return max(0, int(max(candidates))) if candidates else 0


def suggested_next_wait_seconds(checks: int) -> int:
    """Duration for the NEXT chunk, given how many chunks have already run.

    Schedule: 60s → 300s → 900s. Chunked waits are the fallback for runtimes
    without background-task completion notifications; every chunk costs a full
    agent turn. One early heartbeat shows the loop is alive, one mid-length
    chunk catches fast reviewers, then chunks stretch to 900s so a 30-minute
    reviewer wait costs ~4 turns instead of ~8 (#102). Tradeoff: staler
    liveness signal late in a long wait.

    ``checks`` follows the persisted counter in ``begin_wait_chunk``, which
    increments at the START of each chunk — so at the end of the first chunk
    ``checks == 1``. That chunk has already served the 60s heartbeat, so the
    next one is 300s. Reading ``checks <= 1`` as "still the first chunk"
    silently produced 60s → 60s → 300s → 900s and burned an extra turn on
    every long wait (#111 review).
    """
    if checks <= 0:
        return WAIT_FIRST_CHUNK_SECONDS
    if checks == 1:
        return WAIT_SECOND_CHUNK_SECONDS
    return WAIT_LATER_CHUNK_SECONDS


def _latest_submitted_after(
    pull_request: dict[str, Any], author: str, after_iso: str | None
) -> str | None:
    """Newest review submittedAt past the anchor, for the settling JSON payload."""
    try:
        times = [
            r.get("submittedAt")
            for r in filter_reviews(pull_request, author)
            if isinstance(r.get("submittedAt"), str)
        ]
    except (AttributeError, TypeError):
        return None
    if after_iso:
        times = [t for t in times if t > after_iso]
    return max(times) if times else None


def run_wait_chunk(
    pr: PullRequest,
    author: str,
    *,
    timeout_seconds: int,
    interval_seconds: int,
    quiet_seconds: int,
    after_iso: str | None,
    chunk_seconds: int,
) -> dict[str, Any]:
    """One bounded foreground wait chunk; the cross-chunk state machine's step.

    Returns a dict with ``status`` in {waiting, settling, ready, timed_out}.
    ``ready`` carries ``pull_request`` (proceed into the fetch path exactly as
    the legacy wait would); the others carry heartbeat fields and persist a
    snapshot so ``--wait-heartbeat`` can render the human block later.
    The quiet period is measured against the PERSISTED ``stable_since`` so a
    chunk boundary never restarts settling.
    """
    wait = begin_wait_chunk(pr, after_iso)
    chunk_deadline = time.monotonic() + chunk_seconds
    stable_fingerprint = wait.get("stable_fingerprint")
    stable_since = _parse_iso_utc(wait.get("stable_since"))

    while True:
        pull_request = fetch_threads(pr)
        refusal = reviewer_refusal(pull_request, author, after_iso=after_iso)
        if refusal is not None:
            clear_wait_state(pr)
            return {"status": "refused", "author": author, "pull_request": None, **refusal}
        fingerprint = review_activity_fingerprint(pull_request, author, after_iso=after_iso)
        now_dt = _dt.datetime.now(_dt.timezone.utc)
        elapsed = wait_elapsed_seconds(wait, after_iso, now=now_dt)

        if fingerprint is not None and after_iso is None:
            # Cycle 1 fast path: same semantics as the legacy wait.
            clear_wait_state(pr)
            return {"status": "ready", "pull_request": pull_request}

        quiet_remaining: int | None = None
        if fingerprint is not None:
            if fingerprint != stable_fingerprint or stable_since is None:
                stable_fingerprint = fingerprint
                stable_since = now_dt
                save_wait_settle(pr, fingerprint, now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
            quiet_elapsed = (now_dt - stable_since).total_seconds() if stable_since else 0.0
            quiet_remaining = max(0, int(quiet_seconds - quiet_elapsed))
            if quiet_remaining <= 0:
                clear_wait_state(pr)
                return {"status": "ready", "pull_request": pull_request}

        if elapsed >= timeout_seconds:
            snapshot = {
                "status": "timed_out",
                "author": author,
                "elapsed_seconds": elapsed,
                "checks": wait["checks"],
            }
            save_wait_snapshot(pr, snapshot)
            return {**snapshot, "pull_request": None}

        if time.monotonic() >= chunk_deadline:
            next_wait = suggested_next_wait_seconds(wait["checks"])
            snapshot = {
                "status": "settling" if fingerprint is not None else "waiting",
                "author": author,
                "elapsed_seconds": elapsed,
                "checks": wait["checks"],
                "next_wait_seconds": next_wait,
            }
            if fingerprint is not None:
                snapshot["quiet_period_remaining_seconds"] = quiet_remaining
                snapshot["next_wait_seconds"] = max(
                    1, min(next_wait, quiet_remaining or next_wait)
                )
                submitted = _latest_submitted_after(pull_request, author, after_iso)
                if submitted:
                    snapshot["submitted_at"] = submitted
            save_wait_snapshot(pr, snapshot)
            return {**snapshot, "pull_request": None}

        time.sleep(min(interval_seconds, max(0.0, chunk_deadline - time.monotonic())))


def accumulate_fixed_markers(
    pr: PullRequest,
    *,
    fingerprints: list[str] | None = None,
    paths: list[str] | None = None,
) -> None:
    """Merge agent-supplied "fixed locally" markers into run tracking.

    The agent passes ``--fixed-finding <fp>`` (finding fingerprints) and/or
    fixed file paths as it remediates each cycle. They accumulate (union) under
    the run entry so the terminal classification can read which findings the
    agent claims to have fixed — one of the multi-signal inputs to
    metrics.classify_finding_state. Stored alongside, never replacing, the
    existing run-tracking ids/paths.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    fps_val = run.get("fixed_finding_fps")
    fps = {x for x in fps_val if isinstance(x, str)} if isinstance(fps_val, list) else set()
    fpaths_val = run.get("fixed_paths")
    fpaths = {x for x in fpaths_val if isinstance(x, str)} if isinstance(fpaths_val, list) else set()
    for fp in fingerprints or []:
        if fp:
            fps.add(fp)
    for p in paths or []:
        if p:
            fpaths.add(p)
    run["fixed_finding_fps"] = sorted(fps)
    run["fixed_paths"] = sorted(fpaths)
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)


def read_fixed_markers(pr: PullRequest) -> dict[str, set[str]]:
    """Return the accumulated fixed markers as ``{"fingerprints", "paths"}`` sets.

    Empty sets when none were recorded or the state is missing/corrupt.
    """
    run = read_run_tracking(pr)
    if not isinstance(run, dict):
        run = {}
    fps = run.get("fixed_finding_fps", [])
    fpaths = run.get("fixed_paths", [])
    return {
        "fingerprints": {x for x in fps if isinstance(x, str)} if isinstance(fps, list) else set(),
        "paths": {x for x in fpaths if isinstance(x, str)} if isinstance(fpaths, list) else set(),
    }


def accumulate_swept_patterns(pr: PullRequest, signatures: list[str]) -> None:
    """Union agent-supplied --swept-pattern signatures into run tracking.

    Twin of accumulate_fixed_markers. A pattern the agent reports as swept this
    cycle is matched against later cycles' findings to flag recurrence.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    val = run.get("swept_pattern_sigs")
    sigs = {x for x in val if isinstance(x, str)} if isinstance(val, list) else set()
    for s in signatures or []:
        if s:
            sigs.add(s)
    run["swept_pattern_sigs"] = sorted(sigs)
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)


def read_swept_patterns(pr: PullRequest) -> set[str]:
    """Return the accumulated swept pattern signatures, or empty set."""
    run = read_run_tracking(pr)
    val = run.get("swept_pattern_sigs", []) if isinstance(run, dict) else []
    return {x for x in val if isinstance(x, str)} if isinstance(val, list) else set()


def swept_pattern_sets(
    pr: PullRequest, history_swept: set[str]
) -> tuple[set[str], set[str]]:
    """Split swept signatures into ``(this_run, ever)``.

    These answer different questions and must not be conflated. ``this_run`` is
    what the agent reported via ``--swept-pattern`` on this run alone; it drives
    the receipt's "Swept N patterns" and is what gets persisted, so a run that
    swept nothing reports zero. ``ever`` folds in signatures recorded by earlier
    runs of the same PR and drives recurrence detection only, because a pattern
    swept in an earlier cycle that reappears now is exactly what the convergence
    advisory exists to flag.

    Persisting ``this_run`` rather than ``ever`` is what keeps the count
    honest: ``metrics.pattern_history_for_pr`` already unions across records, so
    writing the union back into each new record would compound it and the
    reported count could never fall again.
    """
    this_run = read_swept_patterns(pr)
    return this_run, this_run | history_swept


def build_convergence(
    pr: PullRequest, clusters: list[Any], history: dict[str, set[str]]
) -> dict[str, Any]:
    """Decide every pattern-related receipt field in one place.

    The caller renders and persists what this returns; it must not recompute
    any of it. That is the point: the swept counts were previously derived at
    one site in ``main()`` and rendered at another ~85 lines away, so the two
    could disagree and no test could reach either. Keeping the decision here
    means a test of this function is a test of what the receipt actually says.

    Returns ``stats`` (for the record's rates), ``line`` (the rendered
    Convergence advisory), and ``swept_count`` / ``swept`` (this run only).
    """
    # One signature entry per finding (repeat per cluster member) so the
    # recurrence rate is over findings, not distinct patterns.
    current_sigs = [c.signature for c in clusters for _ in range(c.count)]
    this_run, ever = swept_pattern_sets(pr, history["swept"])
    stats = cluster_findings.recurrence_stats(
        current_sigs,
        prior_sigs=prior_pattern_signatures(pr) | history["seen"],
        swept_sigs=ever,
    )
    return {
        "stats": stats,
        "line": metrics.format_convergence_line(stats, swept_count=len(this_run)),
        "swept_count": len(this_run),
        "swept": sorted(this_run),
    }


def _run_git(
    args: list[str], *, cwd: str | Path | None = None
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _git_stdout(args: list[str], *, cwd: str | Path | None = None) -> str | None:
    result = _run_git(args, cwd=cwd)
    if result is None or result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def repo_root(cwd: str | None = None) -> Path | None:
    """Return the working tree root, or None when it cannot be resolved.

    Clustering reads the source lines findings anchor to, and thread paths are
    repo-relative. Fails OPEN: any git error yields None, and the caller falls
    back to prose-only clustering rather than reading the wrong files.
    """
    top = _git_stdout(["rev-parse", "--show-toplevel"], cwd=cwd)
    return Path(top) if top else None


def canonical_repo_full(pull_request: dict[str, Any], pr: PullRequest) -> str:
    """``owner/repo`` as GitHub spells it, for use as an exact-match store key.

    ``--pr OWNER/REPO#NUMBER`` keeps whatever casing the caller typed, and the
    GraphQL lookup is case-insensitive, so the fetch succeeds. Verification
    profiles and run metrics are keyed by an *exact* string, though: a
    noncanonical spelling silently misses the existing entry and reads as "no
    profile saved" / "no prior runs" (#107). The API's own ``nameWithOwner`` is
    the authority; fall back to the input spelling only when it is absent.
    """
    base = pull_request.get("baseRepository")
    name = base.get("nameWithOwner") if isinstance(base, dict) else None
    if isinstance(name, str) and name:
        return name
    return f"{pr.owner}/{pr.repo}"


def _canonical_pr_repositories(pull_request: dict[str, Any]) -> set[str]:
    repositories = set()
    for key in ("baseRepository", "headRepository"):
        repository = pull_request.get(key)
        name = repository.get("nameWithOwner") if isinstance(repository, dict) else None
        if isinstance(name, str) and name:
            repositories.add(name.casefold())
    return repositories


def _checkout_repository(root: Path) -> str | None:
    remote = _git_stdout(["remote", "get-url", "origin"], cwd=root)
    if remote is None:
        return None
    match = re.search(r"github\.com(?::|/)([^/]+)/([^/#]+?)(?:\.git)?/?$", remote)
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}".casefold()


def _worktree_is_clean(root: Path) -> bool:
    result = _run_git(["status", "--porcelain", "--untracked-files=all"], cwd=root)
    return (
        result is not None
        and result.returncode == 0
        and not (result.stdout or "").strip()
    )


def repo_root_for_pr(
    pull_request: dict[str, Any], *, cwd: str | None = None
) -> Path | None:
    """Return the root only when this checkout is the selected PR head."""
    expected_head = pull_request.get("headRefOid")
    if not isinstance(expected_head, str) or not expected_head:
        return None
    root = repo_root(cwd)
    if root is None or _git_stdout(["rev-parse", "HEAD"], cwd=root) != expected_head:
        return None
    if not _worktree_is_clean(root):
        return None
    checkout_repo = _checkout_repository(root)
    selected_repositories = _canonical_pr_repositories(pull_request)
    return root if checkout_repo in selected_repositories else None


def changed_files_in_range(
    base: str | None, head: str | None, *, cwd: str | None = None
) -> set[str]:
    """Best-effort set of files changed in ``base..head`` via git.

    Used to compute the ``file_changed`` signal: did the finding's path actually
    change in the fixing commits? Fails OPEN — any git error (not a repo, bad
    revs, git missing) yields an empty set rather than crashing the loop.
    """
    if not base or not head:
        return set()
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base}..{head}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


LIKELY_FIXED_FINDING_STATES = {
    "fixed_pushed_awaiting_review",
    "fixed_pending_confirmation",
    "stale_already_fixed",
}


def classify_remaining_finding_states(
    threads: list[dict[str, Any]],
    *,
    fixed_fingerprints: set[str],
    fixed_paths: set[str],
    changed_paths: set[str],
    prior_fingerprints: set[str],
    judge_results: dict[str, dict[str, Any]],
    cap_reached: bool,
) -> dict[str, str]:
    """Classify the still-actionable findings for terminal outcome derivation.

    The state machine lives in ``metrics.classify_finding_state``. This adapter
    only builds its explicit signal booleans from script-visible state and
    agent-supplied fixed markers.
    """
    states: dict[str, str] = {}
    for index, thread in enumerate(threads):
        if not isinstance(thread, dict):
            continue
        fingerprint = finding_fingerprint(thread)
        path_val = thread.get("path")
        path = path_val if isinstance(path_val, str) else ""
        fixed_by_marker = fingerprint in fixed_fingerprints or path in fixed_paths
        file_changed = path in changed_paths or path in fixed_paths
        thread_id = thread.get("id")
        result_key = thread_id if isinstance(thread_id, str) else fingerprint
        judge_result = judge_results.get(result_key, {}) if result_key else {}
        state = metrics.classify_finding_state(
            {
                "judge_needs_human": judge_result.get("verdict") == "needs_human",
                "carried_over": fingerprint in prior_fingerprints,
                "fixed_locally": fixed_by_marker,
                "file_changed": file_changed,
                "gemini_confirmed": False,
                "cap_reached": cap_reached,
            }
        )
        states[result_key or f"finding-{index}"] = state
    return states


def count_likely_fixed_remaining(states: dict[str, str]) -> int:
    return sum(1 for state in states.values() if state in LIKELY_FIXED_FINDING_STATES)


def track_finding_fingerprints(pr: PullRequest, current_fps: set[str]) -> dict[str, set[str]]:
    """Snapshot the prior cycle's finding fingerprints, then union the current.

    Call ONLY on a real agent fetch (not ``--cycle-summary`` / ``--record-run``),
    exactly once per cycle. It moves the running union into
    ``prior_seen_finding_fps`` *before* folding in this cycle's fingerprints, so
    a later read-only ``--cycle-summary`` can ask "was this finding present in a
    previous cycle?" without the current cycle's own fetch poisoning the answer.

    Returns ``{"prior": <prior union>, "new": <current − prior>}``. Fails open:
    any state I/O error yields an empty prior (everything reads as new).
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key, {})
    if not isinstance(entry, dict):
        entry = {}
    run = entry.get("run", {})
    if not isinstance(run, dict):
        run = {}
    prior_val = run.get("seen_finding_fps", [])
    prior = {x for x in prior_val if isinstance(x, str)} if isinstance(prior_val, list) else set()
    run["prior_seen_finding_fps"] = sorted(prior)
    run["seen_finding_fps"] = sorted(prior | current_fps)
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)
    return {"prior": prior, "new": current_fps - prior}


def prior_finding_fingerprints(pr: PullRequest) -> set[str]:
    """Finding fingerprints seen in cycles *before* the current one.

    Read by the receipt path to mark a current finding as carried-over. Empty
    on cycle 1 (no prior cycle) — so every cycle-1 finding reads as new.
    """
    state = load_sticky_state()
    entry = state.get(_state_key(pr), {})
    run = entry.get("run", {}) if isinstance(entry, dict) else {}
    value = run.get("prior_seen_finding_fps", []) if isinstance(run, dict) else []
    return {x for x in value if isinstance(x, str)} if isinstance(value, list) else set()


def track_pattern_signatures(pr: PullRequest, current_sigs: set[str]) -> dict[str, set[str]]:
    """Pattern-granularity twin of track_finding_fingerprints.

    Moves the running union into ``prior_seen_pattern_sigs`` BEFORE folding in
    this cycle's signatures, so a later read can ask "was this pattern present
    in a previous cycle?" without the current fetch poisoning the answer.
    Returns ``{"prior": <prior union>, "new": <current − prior>}``. Fails open.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key, {})
    if not isinstance(entry, dict):
        entry = {}
    run = entry.get("run", {})
    if not isinstance(run, dict):
        run = {}
    prior_val = run.get("seen_pattern_sigs", [])
    prior = {x for x in prior_val if isinstance(x, str)} if isinstance(prior_val, list) else set()
    run["prior_seen_pattern_sigs"] = sorted(prior)
    run["seen_pattern_sigs"] = sorted(prior | current_sigs)
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)
    return {"prior": prior, "new": current_sigs - prior}


def prior_pattern_signatures(pr: PullRequest) -> set[str]:
    """Pattern signatures seen in cycles before the current one. Empty on cycle 1."""
    state = load_sticky_state()
    entry = state.get(_state_key(pr), {})
    run = entry.get("run", {}) if isinstance(entry, dict) else {}
    value = run.get("prior_seen_pattern_sigs", []) if isinstance(run, dict) else []
    return {x for x in value if isinstance(x, str)} if isinstance(value, list) else set()


def detect_no_progress(pr: PullRequest, current_fingerprint: str) -> bool:
    """True when the actionable thread fingerprint is unchanged from last cycle.

    Call AFTER ``update_run_tracking`` so ``update_seq`` reflects the current
    fetch. Reads the fingerprint stored by the previous call, stores the new
    one, and returns True only when both conditions hold:

    - ``update_seq >= 2``: at least two fetches have run (one per-cycle fix
      attempt has already been made without clearing the set of open threads).
    - The fingerprint is identical to the one stored on the previous call.

    A True return means no observable code change resolved any of the threads
    the agent was supposed to fix. The agent should stop with
    ``--record-run --outcome no_progress``.

    Fails OPEN: any state read/write error allows the push (correctness aid
    must never wedge the loop).
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key, {})
    if not isinstance(entry, dict):
        entry = {}
    run = entry.get("run", {})
    if not isinstance(run, dict):
        run = {}
    prev_fingerprint = run.get("thread_fingerprint")
    update_seq = _safe_int(run.get("update_seq", 0))
    run["thread_fingerprint"] = current_fingerprint
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)
    return bool(
        prev_fingerprint
        and prev_fingerprint == current_fingerprint
        and update_seq >= 2
    )


def stamp_summary_emitted(pr: PullRequest) -> None:
    """Record that a summary covering the current run state was just emitted.

    Sets ``last_summary_seq`` to the current ``update_seq`` so the Stop-hook
    backstop won't re-emit a summary the agent already showed. A no-op when no
    run is being tracked, so it never resurrects a cleared/absent run block.
    """
    state = load_sticky_state()
    if not isinstance(state, dict):
        return
    key = _state_key(pr)
    entry = state.get(key)
    if not isinstance(entry, dict):
        return
    run = entry.get("run")
    if not isinstance(run, dict) or "update_seq" not in run:
        return
    seq = _safe_int(run.get("update_seq"))
    if seq <= 0:
        return  # corrupt/non-numeric update_seq — nothing meaningful to stamp
    run["last_summary_seq"] = seq
    state[key]["run"] = run
    save_sticky_state(state)


def accumulate_judge_results(
    pr: PullRequest, judge_results: dict[str, dict[str, Any]]
) -> None:
    """Persist this cycle's judge verdicts into the run accumulator.

    Verdicts are keyed by thread id so a later cycle's re-judgement of the same
    thread supersedes an earlier one. Stored alongside the run's finding ids /
    paths so the terminal ``--record-run`` can build a complete judge block even
    after the findings themselves have been resolved. A no-op when there are no
    results, so a run that never judged keeps no ``judge_ran`` flag.
    """
    if not judge_results:
        return
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    if not isinstance(entry, dict):
        entry = {}
    else:
        entry = dict(entry)
    run = entry.get("run")
    if not isinstance(run, dict):
        run = {}
    else:
        run = dict(run)
    stored = run.get("judge_results")
    stored_dict = dict(stored) if isinstance(stored, dict) else {}
    stored_dict.update(judge_results)
    run["judge_results"] = stored_dict
    run["judge_ran"] = True
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)


def merge_judge_results(
    accumulated: dict[str, dict[str, Any]] | None,
    current: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Union accumulated and current verdicts; current supersedes per thread id."""
    merged = dict(accumulated or {})
    merged.update(current or {})
    return merged


def resolve_judge_phase(explicit_phase: str | None, *, record_run: bool) -> str:
    """Infer the judge phase from invocation context when unset.

    The agent need not remember to pass --judge-phase: a terminal --record-run
    invocation is the loop's completion, everything else is a per-cycle fetch.
    An explicit flag always wins.
    """
    if explicit_phase is not None:
        return explicit_phase
    return "complete" if record_run else "cycle"


def record_cycle(
    pr: PullRequest,
    started_at: str,
    finding_count: int,
    outcome: str,
    finished_at: str | None = None,
) -> dict[str, Any]:
    """Append one active-cycle timing entry to the run accumulator.

    A *cycle* is one active remediation attempt — fetch, analyze, edit, verify,
    prepare/post the response. It measures active work only: the agent passes
    ``started_at`` as the moment active work for this cycle began, so waits
    between cycles (ScheduleWakeup, polling, rate-limit sleeps, idle time) are
    excluded by construction. ``outcome`` is "continued" for a non-terminal
    cycle or the terminal outcome label for the last one. Returns the entry.
    """
    finished_at = finished_at or _now_iso()
    duration = metrics._duration_seconds(started_at, finished_at)
    cycle = {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration,
        "finding_count": int(finding_count),
        "outcome": outcome,
    }
    state = load_sticky_state()
    # The state file can be hand-edited or corrupt; verify each level is the
    # expected type before use (a non-dict state would AttributeError on .get,
    # and dict()/list() on a string/int would raise), mirroring
    # accumulate_judge_results.
    state = state if isinstance(state, dict) else {}
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    cycles = run.get("cycles")
    cycles = list(cycles) if isinstance(cycles, list) else []
    cycles.append(cycle)
    run["cycles"] = cycles
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)
    return cycle


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_existing_sticky_comment(pr: PullRequest) -> int | None:
    """Look up the sticky receipt for a PR by scanning issue comments for the marker.

    Used as a recovery path when the local state file is missing — the marker
    embedded in the receipt body is the source of truth on GitHub.
    """
    # `gh api --paginate` runs --jq on each page independently, so a per-page
    # aggregator like `[...] | last` returns the last id PER PAGE — not the
    # global last. Emit one id per line across all pages and pick the last
    # valid one in Python.
    result = run_gh(
        [
            "api",
            f"repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments",
            "--paginate",
            "--jq",
            f'.[] | select(.body != null and (.body | contains("{STICKY_RECEIPT_MARKER}"))) | .id',
        ]
    )
    if isinstance(result, int):
        return result
    if isinstance(result, str):
        ids = [int(line.strip()) for line in result.splitlines() if line.strip().isdigit()]
        if ids:
            return ids[-1]
    return None


def post_or_update_sticky_receipt(
    pr: PullRequest, body: str, *, dry_run: bool = False
) -> int | None:
    """Post or in-place edit the sticky receipt comment for ``pr``.

    First invocation per PR posts a fresh comment and records its id in the
    state file. Subsequent invocations PATCH the same comment so the user
    sees one live status block on the PR instead of N new comments.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key, {})
    comment_id = entry.get("comment_id")

    if comment_id is None:
        # Fall back to GitHub-side discovery before posting a duplicate.
        comment_id = find_existing_sticky_comment(pr)
        if comment_id and not dry_run:
            entry["comment_id"] = comment_id

    if dry_run:
        verb = "PATCH" if comment_id else "POST"
        print(f"[dry-run] would {verb} sticky receipt on {key}", file=sys.stderr)
        return comment_id

    if comment_id:
        try:
            run_gh(
                [
                    "api",
                    "-X",
                    "PATCH",
                    f"repos/{pr.owner}/{pr.repo}/issues/comments/{comment_id}",
                    "-f",
                    f"body={body}",
                ]
            )
            entry["updated_at"] = _now_iso()
        except RuntimeError as exc:
            # If the sticky comment was deleted on GitHub, PATCH returns 404.
            # Drop the stale id and fall through to POST a fresh comment so
            # the loop recovers without crashing.
            msg = str(exc)
            if "404" in msg or "Not Found" in msg:
                print(
                    f"warning: sticky receipt {comment_id} no longer exists on GitHub "
                    "(deleted?). Posting a new comment.",
                    file=sys.stderr,
                )
                comment_id = None
            else:
                raise

    if not comment_id:
        result = run_gh(
            [
                "api",
                "-X",
                "POST",
                f"repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments",
                "-f",
                f"body={body}",
            ]
        )
        if not isinstance(result, dict) or "id" not in result:
            raise RuntimeError(f"Failed to post sticky receipt: {result}")
        comment_id = int(result["id"])
        entry["comment_id"] = comment_id
        entry["started_at"] = _now_iso()
        entry["updated_at"] = entry["started_at"]

    state[key] = entry
    save_sticky_state(state)
    return comment_id


def render_receipt(
    pr: PullRequest,
    pull_request: dict[str, Any],
    threads: list[dict[str, Any]],
    *,
    author: str,
    resolved_outdated: int,
    resolved_addressed_by_reply: int,
    rereview_count: int,
    rereview_limit: int,
    status: str | None = None,
    sticky: bool = False,
) -> str:
    """Render the markdown receipt body.

    ``status`` (RUNNING / DONE / STOPPED) appears in the header when set.
    ``sticky`` embeds the discovery marker and a "last updated" timestamp.
    """
    counts = severity_counts(threads)
    deferred = len(addressed_by_reply_threads(pull_request, author))
    sev_line = ", ".join(
        f"{k}={counts[k]}" for k in ("critical", "high", "medium", "low", "unknown") if counts.get(k)
    ) or "none"
    header_suffix = f" — {status}" if status else ""
    parts = [
        f"### gh-review-loop receipt{header_suffix}",
        "",
        "| metric | value |",
        "|---|---|",
        f"| re-review requests used (cap) | {rereview_count} / {rereview_limit} |",
        f"| outdated threads resolved | {resolved_outdated} |",
        f"| addressed-by-reply threads resolved | {resolved_addressed_by_reply} |",
        f"| addressed-by-reply still pending | {deferred} |",
        f"| actionable threads remaining | {len(threads)} |",
        f"| severity breakdown (actionable) | {sev_line} |",
        "",
    ]
    if sticky:
        parts.append(f"_Last updated: {_now_iso()}. Receipt edits in place as the loop progresses._")
        parts.append("")
        parts.append(STICKY_RECEIPT_MARKER)
    else:
        parts.append("_Generated by `scripts/fetch_gemini_threads.py --post-receipt`._")
    return "\n".join(parts) + "\n"


def build_receipt_comment_body(
    blocks: list[str], *, status: str, findings_block: str = ""
) -> str:
    """Markdown body carrying the FULL cycle/terminal receipt on the PR.

    This is the single authoritative delivery channel for receipt content
    (#86): chat gets a one-line pointer, this comment gets everything. The
    plaintext blocks ride in a code fence; the findings list stays outside it
    so GitHub auto-links each finding's comment URL.
    """
    parts = [f"### gh-review-loop receipt — {status}", ""]
    content = "\n\n".join(block for block in blocks if block)
    if content:
        parts.extend(["```", content, "```", ""])
    if findings_block:
        parts.extend([findings_block, ""])
    parts.append(
        f"_Last updated: {_now_iso()}. This comment is edited in place as the loop progresses._"
    )
    parts.append("")
    parts.append(STICKY_RECEIPT_MARKER)
    return "\n".join(parts) + "\n"


def thread_fingerprint(threads: list[dict[str, Any]]) -> str:
    payload = []
    for thread in threads:
        comments = thread.get("comments", [])
        payload.append(
            {
                "path": thread.get("path") or first_value(thread, "path"),
                "line": thread.get("line") or thread.get("originalLine") or first_value(thread, "line"),
                "comments": [
                    {
                        "author": (comment.get("author") or {}).get("login"),
                        "body": comment.get("body"),
                    }
                    for comment in comments
                ],
            }
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def finding_fingerprint(thread: dict[str, Any]) -> str:
    """Content identity of a single finding, stable across re-posts.

    Gemini re-posts the same suggestion as a *new* thread (new id) on each
    re-review, so the thread id cannot tell a fresh finding from a carried-over
    one. The path plus the normalized suggestion text can: we strip the leading
    severity image markdown Gemini prepends and collapse whitespace, so cosmetic
    differences (line-number echoes, re-wrapping) don't change the fingerprint.
    """
    if not isinstance(thread, dict):
        return ""
    path_val = thread.get("path")
    path = path_val if path_val is not None else ""
    comments = _iter_comments(thread)
    first_comment = comments[0].get("body") if comments and isinstance(comments[0], dict) else None
    body = first_comment if isinstance(first_comment, str) else ""
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)  # drop severity image
    body = re.sub(r"\s+", " ", body).strip().lower()
    return hashlib.sha1(f"{path}\n{body[:1000]}".encode()).hexdigest()[:16]


def review_activity_fingerprint(
    pull_request: dict[str, Any],
    author: str,
    after_iso: str | None = None,
) -> str | None:
    """Compute a hash of all Gemini review activity in ``pull_request``.

    Returns ``None`` when no matching activity exists (caller keeps polling).

    ``after_iso`` — optional ISO-8601 lower-bound (exclusive).  When set,
    only reviews submitted after that timestamp and only threads whose newest
    comment was created after that timestamp are counted.  Pass the timestamp
    of the re-review request comment so the waiter ignores prior-cycle
    activity and only returns once Gemini has genuinely responded to the new
    cycle's push.
    """
    reviews = filter_reviews(pull_request, author)
    if after_iso:
        reviews = [r for r in reviews if (r.get("submittedAt") or "") > after_iso]

    authored_threads = filter_threads(
        pull_request,
        author=author,
        include_resolved=True,
        include_outdated=True,
    )
    if after_iso:
        # A thread counts as "new" when at least one of its comments was
        # created after the anchor.  Using the newest comment timestamp means
        # a thread where Gemini added a follow-up reply after the re-review
        # request is also captured correctly.
        authored_threads = [
            t for t in authored_threads
            if any(
                (c.get("createdAt") or "") > after_iso
                for c in t.get("comments", [])
            )
        ]

    if not reviews and not authored_threads:
        return None

    payload = {"reviews": reviews, "threads": authored_threads}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def wait_for_stable_review(
    pr: PullRequest,
    author: str,
    timeout_seconds: int,
    interval_seconds: int,
    quiet_seconds: int,
    after_iso: str | None = None,
) -> dict[str, Any]:
    """Poll until ``author``'s review activity on ``pr`` is present and stable.

    ``after_iso`` anchors the wait to a specific point in time (ISO-8601,
    exclusive).  When set, review activity submitted at or before that
    timestamp is ignored — the poller only returns once Gemini has posted
    activity *after* that timestamp.  This prevents a cycle-2 wait from
    returning immediately because cycle-1's review is already stable.

    Typical usage: pass the ``createdAt`` of the re-review request comment
    that triggered the new cycle, so the wait is anchored to that request.

    Designed to run as a background task: while polling it writes a one-line
    heartbeat to stderr every ``WAIT_HEARTBEAT_INTERVAL_SECONDS`` so an
    inspected task shows liveness, and a timeout raises the typed
    ``WaitTimedOut`` so the CLI ends the captured output with a relayable
    line instead of a traceback.
    """
    start = time.monotonic()
    deadline = start + timeout_seconds
    last_fingerprint: str | None = None
    stable_since: float | None = None
    last_heartbeat: float | None = None

    if after_iso:
        print(
            f"Waiting for {author} review activity after {after_iso}...",
            file=sys.stderr,
        )

    while True:
        pull_request = fetch_threads(pr)
        refusal = reviewer_refusal(pull_request, author, after_iso=after_iso)
        if refusal is not None:
            raise ReviewerRefused(refusal)
        fingerprint = review_activity_fingerprint(pull_request, author, after_iso=after_iso)
        now = time.monotonic()

        if fingerprint is None:
            if (
                last_heartbeat is None
                or now - last_heartbeat >= WAIT_HEARTBEAT_INTERVAL_SECONDS
            ):
                print(
                    f"Waiting for {author} review activity... "
                    f"({int(now - start)}s elapsed, timeout {timeout_seconds}s)",
                    file=sys.stderr,
                )
                last_heartbeat = now
        elif after_iso is None:
            # Cycle 1 / initial review: activity is present, so return at once
            # without waiting for it to settle. At the initial review there
            # either are comments or there aren't — the quiet-period settle only
            # earns its keep on cycle 2+ (after_iso set), where a freshly pushed
            # fix needs Gemini's re-review to stabilize before we fetch.
            print(f"Detected {author} review activity.", file=sys.stderr)
            return pull_request
        elif fingerprint != last_fingerprint:
            print(f"Detected {author} review activity; waiting for it to settle...", file=sys.stderr)
            last_fingerprint = fingerprint
            stable_since = now
        elif stable_since is not None and now - stable_since >= quiet_seconds:
            print(f"{author} review activity is stable.", file=sys.stderr)
            return pull_request

        if now >= deadline:
            raise WaitTimedOut(int(now - start))

        time.sleep(min(interval_seconds, max(0.0, deadline - now)))


def format_judge_verdict_summary(
    judge_results: dict[str, dict[str, Any]],
    phase: str,
) -> str:
    """One-line verdict breakdown for agent narration relay.

    Printed after the markdown thread list so the agent can copy it verbatim
    into its text response. Only threads where the judge actually produced a
    verdict (status == "ok") are counted; skipped threads are excluded.

    Output format (intended for literal copy-paste into agent text):
        [loop] judge (cycle): 3 thread(s) — valid_actionable: 2, false_positive: 1
    """
    verdicts: Counter[str] = Counter(
        r["verdict"]
        for r in judge_results.values()
        if r.get("status") == "ok" and r.get("verdict")
    )
    if not verdicts:
        return f"[loop] judge ({phase}): evaluated {len(judge_results)} thread(s) — all skipped/errored"
    total = sum(verdicts.values())
    breakdown = ", ".join(
        f"{verdict}: {count}"
        for verdict, count in sorted(verdicts.items(), key=lambda kv: -kv[1])
    )
    return f"[loop] judge ({phase}): {total} thread(s) evaluated — {breakdown}"


def format_judge_thread_table(
    threads: list[dict[str, Any]],
    judge_results: dict[str, dict[str, Any]],
    phase: str,
) -> str:
    """Per-thread judge decision table for agent narration relay.

    Printed after the markdown thread list so the agent can copy it verbatim
    into its text response. Each row shows the thread location, severity,
    verdict, recommended action, and confidence — all in one place, outside
    the collapsed thread markdown block.

    Format:
        [loop] judge eval (cycle): 3 thread(s)
          1 src/foo.py:42 [high] — valid_actionable · fix · conf 0.91
          2 src/bar.py:15 [medium] — needs_human · escalate · conf 0.84
          3 src/baz.py:7 [low] — skipped (no API key)
    """
    rows = []
    for i, thread in enumerate(threads, 1):
        path = thread.get("path") or "?"
        line = thread.get("line") or thread.get("originalLine") or "?"
        sev = thread_severity(thread)
        tid = thread.get("id", "")
        jr = judge_results.get(tid)
        if jr and jr.get("status") == "ok":
            verdict = jr.get("verdict", "?")
            action = jr.get("recommended_action", "?")
            conf = jr.get("confidence", 0.0)
            try:
                conf_str = f"{float(conf):.2f}"
            except (TypeError, ValueError):
                conf_str = "?"
            rows.append(
                f"  {i} {path}:{line} [{sev}] — {verdict} · {action} · conf {conf_str}"
            )
        elif jr and jr.get("status") == "skipped":
            reason = jr.get("skip_reason", "unknown")
            rows.append(f"  {i} {path}:{line} [{sev}] — skipped ({reason})")
        else:
            rows.append(f"  {i} {path}:{line} [{sev}] — not evaluated")
    n = len(threads)
    header = f"[loop] judge eval ({phase}): {n} thread(s)"
    return "\n".join([header] + rows)


def load_profile_for_repo(repo: str) -> dict[str, Any] | None:
    """Best-effort saved verification profile lookup for formatter commands."""
    try:
        from judge import get_profile  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return get_profile(repo)
    except Exception:
        return None


def judge_eval_requested(judge_status: dict[str, Any]) -> bool:
    if not isinstance(judge_status, dict) or judge_status.get("ran"):
        return False
    mode = judge_status.get("mode")
    reason = str(judge_status.get("skip_reason") or "")
    return mode in {"on_cycle", "on_complete", "once"} and "no-op" not in reason


def color_loop_block(block: str, *, enabled: bool) -> str:
    """Color only human-readable loop-owned blocks."""
    if isinstance(block, str) and block.startswith("[loop]"):
        return color_loop(block, enabled=enabled)
    return block


def print_clustering_blocks(clusters: list[Any]) -> None:
    """Print the shared pattern view and any degenerate-clustering warning."""
    patterns_block = metrics.format_patterns_block(clusters)
    if patterns_block:
        print(patterns_block)
    clustering_advisory = metrics.format_degenerate_clustering_advisory(clusters)
    if clustering_advisory:
        print(clustering_advisory)


# Diff hunks are convenience context, not the source of truth — the agent has
# the checkout and reads files before editing. Cap them so verbose reviewers
# don't dominate the fetch output.
HUNK_MAX_LINES = 10


def truncate_diff_hunk(hunk: str) -> str:
    lines = hunk.rstrip().splitlines()
    if len(lines) <= HUNK_MAX_LINES:
        return "\n".join(lines)
    dropped = len(lines) - HUNK_MAX_LINES
    return "\n".join(lines[:HUNK_MAX_LINES]) + f"\n… (+{dropped} more diff lines)"


def _thread_header_parts(thread: dict[str, Any]) -> tuple[str, Any, str]:
    """(path, line, status_text) shared by the full block and the collapsed line."""
    path = thread.get("path") or first_value(thread, "path") or "(unknown path)"
    line = thread.get("line") or first_value(thread, "line") or thread.get("originalLine") or ""
    status = []
    severity = thread_severity(thread)
    if severity != "unknown":
        status.append(severity)
    if thread.get("isResolved"):
        status.append("resolved")
    if thread.get("isOutdated"):
        status.append("outdated")
    status_text = f" [{' '.join(status)}]" if status else ""
    return path, line, status_text


def render_thread_block(
    thread: dict[str, Any],
    judge_result: dict[str, Any] | None = None,
    *,
    index: int | None = None,
    include_confidence: bool = True,
) -> list[str]:
    """The full rendered block for one thread.

    Two forms from one renderer, so the delta collapse (#95) can never diverge
    from what is actually shown:

    - Output form (``index=N``, ``include_confidence=True``) — what
      ``render_markdown`` emits.
    - Canonical form (``index=None``, ``include_confidence=False``) — what
      ``thread_render_fingerprint`` hashes. Index-free because the positional
      number shifts whenever an earlier thread resolves; confidence-free
      because the judge's confidence decimal jitters across re-runs. Both are
      volatile decoration, not semantic change.
    """
    path, line, status_text = _thread_header_parts(thread)
    judge_line = ""
    if judge_result and judge_result.get("status") == "ok":
        conf = f"conf {judge_result['confidence']:.2f}, " if include_confidence else ""
        judge_line = (
            f"  \n  > **Judge:** `{judge_result['verdict']}` ({conf}"
            f"severity_override={judge_result['severity_override']}, "
            f"action={judge_result['recommended_action']}). {judge_result.get('reason', '')}"
        )
    elif judge_result and judge_result.get("status") == "skipped":
        judge_line = f"  \n  > **Judge:** skipped — {judge_result.get('skip_reason', '')}"
    prefix = f"## {index}. " if index is not None else "## "
    lines = [f"{prefix}{path}:{line}{status_text}{judge_line}"]
    for comment in thread["comments"]:
        body = (comment.get("body") or "").strip()
        url = comment.get("url") or ""
        created = comment.get("createdAt") or ""
        lines.extend(["", f"- {created} {url}".strip(), "", body, ""])
        hunk = comment.get("diffHunk")
        if hunk:
            lines.extend(["```diff", truncate_diff_hunk(hunk), "```", ""])
    return lines


def thread_render_fingerprint(
    thread: dict[str, Any], judge_result: dict[str, Any] | None = None
) -> str:
    """Hash of the canonical rendered block — render first, fingerprint second.

    Distinct from ``finding_fingerprint`` (a deliberately fuzzy identity for
    carried-over tracking): this is an exact content hash whose only job is
    "would this thread's block re-render byte-identical to last cycle's?".
    Any renderer change automatically invalidates stale baselines.
    """
    canonical = "\n".join(
        render_thread_block(thread, judge_result, index=None, include_confidence=False)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def render_collapsed_thread_line(thread: dict[str, Any], *, index: int) -> list[str]:
    """One-line stand-in for a thread whose full block is unchanged (#95).

    Keeps everything needed to act without the body: anchor, severity, and the
    comment URL for recovery after context loss.
    """
    path, line, status_text = _thread_header_parts(thread)
    comments = thread.get("comments") or []
    first = comments[0] if comments and isinstance(comments[0], dict) else {}
    url = first.get("url") or ""
    suffix = f" ({url})" if url else ""
    return [f"## {index}. {path}:{line}{status_text} — unchanged since last cycle{suffix}", ""]


def render_markdown(
    pr: dict[str, Any],
    threads: list[dict[str, Any]],
    author: str,
    *,
    reviewer_name: str = DEFAULT_PROVIDER_NAME,
    review_trigger_mention: str = DEFAULT_REVIEW_TRIGGER_MENTION,
    judge_results: dict[str, dict[str, Any]] | None = None,
    baseline: dict[str, str] | None = None,
    full: bool = False,
) -> str:
    reviews = filter_reviews(pr, author)
    rereviews = rereview_requests(pr, review_trigger_mention=review_trigger_mention)
    outdated = outdated_unresolved_threads(pr, author)
    deferred = addressed_by_reply_threads(pr, author)
    lines = [
        f"# {reviewer_name} Threads for PR #{pr.get('number')}",
        "",
        f"PR: {pr.get('url')}",
        f"Author filter: `{author}`",
        f"Reviews by author: {len(reviews)}",
        f"Re-review requests: {len(rereviews)}",
        f"Unresolved outdated threads: {len(outdated)}",
        f"Addressed-by-reply threads: {len(deferred)}",
        f"Threads: {len(threads)}",
        "",
    ]
    if reviews:
        lines.append("## Reviews")
        for review in reviews:
            state = review.get("state") or "UNKNOWN"
            submitted = review.get("submittedAt") or ""
            url = review.get("url") or ""
            lines.append(f"- {state} {submitted} {url}".strip())
        lines.append("")
    for index, thread in enumerate(threads, start=1):
        # Optional judge annotation — only present when the user opted in via
        # --judge-mode (or saved preference) AND --judge-phase matched.
        jr = judge_results.get(thread.get("id")) if judge_results else None
        thread_id = thread.get("id")
        if (
            not full
            and baseline is not None
            and isinstance(thread_id, str)
            and baseline.get(thread_id) == thread_render_fingerprint(thread, jr)
        ):
            lines.extend(render_collapsed_thread_line(thread, index=index))
            continue
        lines.extend(render_thread_block(thread, jr, index=index))
    return "\n".join(lines).rstrip() + "\n"


def first_value(thread: dict[str, Any], key: str) -> Any:
    for comment in thread.get("comments", []):
        value = comment.get(key)
        if value is not None:
            return value
    return None


def main() -> int:
    # Post-rename housekeeping: move ~/.config/gh-gemini-review-loop to
    # ~/.config/gh-review-loop once, leaving a compatibility symlink for
    # pre-rename plugin versions. No-op after the first run. Done here (the
    # loop's single entry point) so the hook gates never pay for it.
    migrate_legacy_state_dir()
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Verification profiles: the loop detects a per-repo check profile on "
            "first run (pytest/ruff, npm scripts, cargo, go) and stores it in "
            "preferences.json. Say \"set up a verification profile for this repo\" "
            "to configure, or \"skip verification profile\" to opt out."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--pr", help="PR URL or OWNER/REPO#NUMBER. Defaults to current branch PR.")
    parser.add_argument("--repo", default=None, help="OWNER/REPO for formatter-only commands.")
    parser.add_argument("--author", default=None, help=f"Reviewer author login. Default: {DEFAULT_AUTHOR}")
    parser.add_argument(
        "--reviewer",
        default=None,
        help="Reviewer bot login to select and persist for this PR.",
    )
    parser.add_argument(
        "--reviewer-source",
        choices=["explicit", "confirmed"],
        default="explicit",
        help=(
            "Selection source to persist with --reviewer. Use 'confirmed' for "
            "the first-run prompt confirmation path."
        ),
    )
    parser.add_argument(
        "--reviewer-name",
        default=None,
        help=f"Human-readable reviewer name for output. Default: {DEFAULT_PROVIDER_NAME!r}.",
    )
    parser.add_argument(
        "--review-trigger-mention",
        default=None,
        help=(
            "Mention phrase that requests a re-review and counts toward the loop cap. "
            f"Default: {DEFAULT_REVIEW_TRIGGER_MENTION!r}."
        ),
    )
    parser.add_argument(
        "--list-reviewers",
        action="store_true",
        help="Discover AI reviewer bot candidates on this PR and exit without mutating state.",
    )
    parser.add_argument(
        "--reset-reviewer",
        action="store_true",
        help="Clear the persisted reviewer selection for this PR and exit.",
    )
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color for human-readable [loop] blocks.",
    )
    parser.add_argument(
        "--profile-intro",
        action="store_true",
        help="Print the saved verification-profile intro block for --repo and exit.",
    )
    parser.add_argument(
        "--planned-verification",
        action="store_true",
        help="Print the planned repo-aware verification block for --repo and exit.",
    )
    parser.add_argument(
        "--wait-heartbeat",
        action="store_true",
        help=(
            "Print the human heartbeat block for the PR's in-progress chunked "
            "wait (from persisted state) and exit. Use after a --format json "
            "wait chunk so JSON stdout stays machine-only."
        ),
    )
    parser.add_argument("--include-resolved", action="store_true")
    parser.add_argument("--include-outdated", action="store_true")
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Render every thread in full, ignoring the delta baseline. Use after "
            "context loss or compaction to re-establish the baseline. Diff hunks "
            "still use the standard truncation cap."
        ),
    )
    parser.add_argument("--wait", action="store_true", help="Poll until reviewer activity appears and is stable.")
    parser.add_argument("--timeout", type=int, default=900, help="Seconds to wait with --wait. Default: 900.")
    parser.add_argument("--interval", type=int, default=20, help="Polling interval in seconds with --wait. Default: 20.")
    parser.add_argument("--quiet-period", type=int, default=45, help="Stable activity period in seconds with --wait. Default: 45.")
    parser.add_argument(
        "--wait-chunk-seconds",
        type=int,
        default=None,
        help=(
            "With --wait, return after at most this many seconds with a "
            "deterministic waiting/settling/timed_out status instead of "
            "blocking until --timeout. --timeout stays the TOTAL wait budget "
            "across chunks. Omit for legacy blocking behavior."
        ),
    )
    parser.add_argument(
        "--after",
        metavar="ISO8601",
        default=None,
        help=(
            "With --wait, ignore reviewer activity submitted at or before this "
            "ISO-8601 timestamp. Pass the createdAt of the re-review request "
            "comment so the waiter only returns once the reviewer has genuinely "
            "responded to the new cycle's push, not to prior-cycle reviews."
        ),
    )
    parser.add_argument(
        "--resolve-outdated",
        dest="resolve_outdated",
        action="store_true",
        default=True,
        help="Resolve unresolved outdated reviewer threads before printing current feedback. Enabled by default.",
    )
    parser.add_argument(
        "--no-resolve-outdated",
        dest="resolve_outdated",
        action="store_false",
        help="Do not resolve outdated reviewer threads; use for read-only inspection.",
    )
    parser.add_argument(
        "--max-rereview-requests",
        type=nonnegative_int,
        default=None,
        help=(
            "Warn when prior reviewer re-review requests reach this limit. "
            "Overrides max_rereview_requests in ~/.config/gh-review-loop/preferences.json "
            f"(default: {DEFAULT_REREVIEW_LIMIT})."
        ),
    )
    # Cleanup (resolve-outdated, resolve-addressed-by-reply) now always runs
    # regardless of the re-review cap. --resolve-past-cap / --ignore-loop-limit
    # are kept as accepted no-ops for backward compatibility with older scripts.
    parser.add_argument(
        "--resolve-past-cap",
        dest="ignore_loop_limit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--ignore-loop-limit",
        dest="ignore_loop_limit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--agent-login",
        default=None,
        help=(
            "GitHub login of the agent posting re-review requests. If omitted, the script "
            "auto-detects via `gh api user`. Used to count only the agent's own re-reviews "
            "toward the cap (humans pinging the reviewer do not consume cycles)."
        ),
    )
    parser.add_argument(
        "--no-agent-filter",
        action="store_true",
        help="Disable agent-login filtering; count ANY configured reviewer re-review comment.",
    )
    parser.add_argument(
        "--post-receipt",
        action="store_true",
        help=(
            "Post a summary 'loop receipt' comment to the PR after fetch/filter. "
            "Includes re-review requests used, threads resolved, severity breakdown, and remaining actionable count. "
            "Posts a NEW comment each invocation; for one live comment edited in place, use --sticky-receipt."
        ),
    )
    parser.add_argument(
        "--sticky-receipt",
        action="store_true",
        help=(
            "Like --post-receipt, but maintain ONE comment per PR that is edited in place across loop "
            "invocations. State persists in ~/.config/gh-review-loop/state.json "
            "(override with GGRL_STATE_DIR env var). Provides background visibility in the PR UI."
        ),
    )
    parser.add_argument(
        "--receipt-status",
        choices=["running", "done", "stopped"],
        default=None,
        help=(
            "Tag the receipt with a status header (RUNNING / DONE / STOPPED). Used with --sticky-receipt "
            "to communicate loop phase. Defaults to RUNNING for sticky, none for one-shot."
        ),
    )
    parser.add_argument(
        "--resolve-addressed-by-reply",
        dest="resolve_addressed_by_reply",
        action="store_true",
        default=True,
        help=(
            "Resolve unresolved threads where a non-bot maintainer posted a substantive reply "
            "(>=%d chars). Enabled by default." % ADDRESSED_BY_REPLY_MIN_CHARS
        ),
    )
    parser.add_argument(
        "--no-resolve-addressed-by-reply",
        dest="resolve_addressed_by_reply",
        action="store_false",
        help="Leave addressed-by-reply threads unresolved (they remain hidden from actionable output).",
    )
    parser.add_argument(
        "--include-addressed-by-reply",
        action="store_true",
        help="Include addressed-by-reply threads in actionable output (default: hidden).",
    )
    parser.add_argument(
        "--min-severity",
        choices=["critical", "high", "medium", "low"],
        default=None,
        help=(
            "Drop actionable threads below this reviewer-assigned severity. "
            "Threads without a recognized severity marker ('unknown') are kept regardless "
            "(use --drop-unknown-severity to remove them too)."
        ),
    )
    parser.add_argument(
        "--drop-unknown-severity",
        action="store_true",
        help="Drop threads with no severity marker (can be used alone or with --min-severity).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not call any GraphQL mutations; log intended writes to stderr instead.",
    )
    parser.add_argument(
        "--judge-mode",
        choices=["off", "on_cycle", "on_complete", "once"],
        default=None,
        help=(
            "Override the saved OpenAI-judge preference for this invocation. "
            "Without this flag the script reads ~/.config/gh-review-loop/preferences.json "
            "(default: 'off'). Requires an OpenAI API key resolved by key_resolver.py "
            "(env var / dotfile / OS keystore); gracefully skips otherwise."
        ),
    )
    parser.add_argument(
        "--judge-phase",
        choices=["cycle", "complete"],
        default=None,
        help=(
            "Which loop phase this invocation represents (the agent supplies this). "
            "'cycle' = before fixes each cycle; 'complete' = after the loop stops. "
            "Together with --judge-mode, controls whether the judge actually runs."
        ),
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="Override the OpenAI model used by the judge for this invocation.",
    )
    parser.add_argument(
        "--record-run",
        action="store_true",
        help=(
            "Write one run-metrics record to runs.jsonl and print the [loop] Summary. "
            "Use once at loop end. Combine with --fixed-count and --verification."
        ),
    )
    parser.add_argument(
        "--cycle-summary",
        action="store_true",
        help=(
            "Print the [loop] Summary block for the current cycle from accumulated "
            "state, WITHOUT writing a record or clearing run tracking. Safe to call "
            "every cycle (unlike --record-run, which is terminal and destructive)."
        ),
    )
    parser.add_argument(
        "--auto-snapshot",
        action="store_true",
        help=(
            "Render the lean auto-snapshot receipt instead of the full one. For "
            "the Stop-hook backstop: shows only GitHub-observable state (threads "
            "seen/resolved/open, cycles) and omits agent-only fields (fixed "
            "count, verification, outcome) the hook cannot know."
        ),
    )
    parser.add_argument("--fixed-count", type=int, default=0, help="Agent-claimed fixes this run.")
    parser.add_argument(
        "--fixed-finding",
        action="append",
        default=[],
        help=(
            "Finding fingerprint the agent fixed locally. Repeatable; stored in "
            "run tracking and used for terminal classification."
        ),
    )
    parser.add_argument(
        "--fixed-path",
        action="append",
        default=[],
        help=(
            "Path the agent fixed locally. Repeatable; stored in run tracking and "
            "used as path-level fixed/change evidence."
        ),
    )
    parser.add_argument(
        "--swept-pattern",
        action="append",
        default=[],
        metavar="SIG",
        help="Pattern signature (from the Patterns receipt 'sig:' token) the agent "
             "swept across changed files this cycle. Repeatable. Accumulates for "
             "the convergence advisory.",
    )
    parser.add_argument(
        "--changed-base",
        default=None,
        help="Optional git base ref for changed-file evidence (`git diff --name-only base..head`).",
    )
    parser.add_argument(
        "--changed-head",
        default=None,
        help="Optional git head ref for changed-file evidence (`git diff --name-only base..head`).",
    )
    parser.add_argument(
        "--verification",
        choices=["passed", "failed", "skipped"],
        default="skipped",
        help="Result of the verification step this run.",
    )
    parser.add_argument(
        "--verification-details",
        default=None,
        help="Optional JSON object with structured verification context.",
    )
    parser.add_argument(
        "--outcome",
        choices=list(metrics.VALID_OUTCOMES),
        default=None,
        help="Terminal outcome of the loop. If omitted, derived from state.",
    )
    parser.add_argument("--outcome-reason", default=None, help="One-line reason for --outcome.")
    parser.add_argument(
        "--gemini-confirmed",
        dest="gemini_confirmed",
        action="store_true",
        default=True,
        help="The final Gemini wait/re-review completed. Default for legacy compatibility.",
    )
    parser.add_argument(
        "--gemini-unconfirmed",
        "--no-gemini-confirmed",
        dest="gemini_confirmed",
        action="store_false",
        help="The final Gemini wait timed out or otherwise did not confirm the latest fixes.",
    )
    parser.add_argument(
        "--semantic-risk",
        action="append",
        default=[],
        help=(
            "Manual/heuristic semantic risk note for this cycle. Repeatable; "
            "rendered only in cycle/run summary output."
        ),
    )
    parser.add_argument(
        "--record-cycle",
        action="store_true",
        help=(
            "Append one active-cycle timing entry to the run accumulator and exit. "
            "Pass --cycle-started-at (when active work for the cycle began), "
            "--finding-count, and --cycle-outcome. Measures active work only; waits "
            "between cycles are excluded. Does not touch runs.jsonl."
        ),
    )
    parser.add_argument(
        "--cycle-started-at",
        default=None,
        help="ISO-8601 UTC timestamp (YYYY-MM-DDThh:mm:ssZ) when this cycle's active work began.",
    )
    parser.add_argument(
        "--finding-count",
        type=int,
        default=0,
        help="Number of actionable findings handled in this cycle.",
    )
    parser.add_argument(
        "--cycle-outcome",
        default="continued",
        help="Cycle outcome: 'continued' for a non-terminal cycle, else the terminal label.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print local Gemini-loop stats for this repo from runs.jsonl and exit.",
    )
    parser.add_argument(
        "--stats-window",
        type=int,
        default=metrics.DEFAULT_WINDOW,
        help=f"Number of most-recent runs to aggregate. Default: {metrics.DEFAULT_WINDOW}.",
    )
    parser.add_argument(
        "--stats-all-repos",
        action="store_true",
        help="Aggregate across all repos instead of only the current one.",
    )
    args = parser.parse_args()
    color_enabled = colors_enabled(no_color=args.no_color)

    if args.profile_intro or args.planned_verification:
        if not args.repo:
            print("error: --profile-intro/--planned-verification require --repo OWNER/REPO.", file=sys.stderr)
            return 1
        profile = load_profile_for_repo(args.repo)
        blocks = []
        if args.profile_intro:
            blocks.append(metrics.format_profile_intro_block(profile, args.repo))
        if args.planned_verification:
            blocks.append(metrics.format_planned_verification_block(profile))
        print(color_loop_block("\n".join(blocks), enabled=color_enabled))
        return 0

    if args.wait_heartbeat:
        pr = resolve_pr(args.pr)
        snapshot = read_wait_state(pr).get("last_snapshot")
        if not isinstance(snapshot, dict) or not snapshot:
            print(
                color_loop_block(
                    "[loop] no reviewer wait in progress for this PR.",
                    enabled=color_enabled,
                )
            )
            return 0
        print(
            color_loop_block(
                metrics.format_wait_heartbeat(
                    str(snapshot.get("status", "")),
                    author=str(snapshot.get("author", DEFAULT_AUTHOR)),
                    elapsed_seconds=snapshot.get("elapsed_seconds", 0),
                    checks=snapshot.get("checks", 0),
                    next_wait_seconds=snapshot.get("next_wait_seconds", 0),
                    quiet_period_remaining_seconds=snapshot.get(
                        "quiet_period_remaining_seconds"
                    ),
                )
                or "[loop] no reviewer wait in progress for this PR.",
                enabled=color_enabled,
            )
        )
        return 0

    if args.stats:
        try:
            if args.pr:
                pr = resolve_pr(args.pr)
                repo_full = f"{pr.owner}/{pr.repo}"
            elif args.stats_all_repos:
                repo_full = "(all repos)"
            else:
                repo_full = resolve_current_repo()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        records, skipped = metrics.load_records()
        selected = select_stats_records(
            records, repo=repo_full, window=args.stats_window, all_repos=args.stats_all_repos
        )
        agg = metrics.aggregate(selected)
        if args.format == "json":
            print(json.dumps({"repo": repo_full, "stats": agg, "skipped": skipped}, indent=2, sort_keys=True))
        else:
            print(metrics.format_stats(repo_full, agg, skipped=skipped))
        return 0

    if args.record_cycle:
        if not args.cycle_started_at:
            print("error: --record-cycle requires --cycle-started-at.", file=sys.stderr)
            return 1
        try:
            pr = resolve_pr(args.pr)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            cycle = record_cycle(
                pr,
                started_at=args.cycle_started_at,
                finding_count=args.finding_count,
                outcome=args.cycle_outcome,
            )
        except OSError as exc:
            # Metrics state I/O is best-effort: warn and exit 0 so a failed
            # write never breaks the loop. Return here (do not fall through to
            # the success print, which would reference an unbound `cycle`).
            print(f"warning: could not record cycle timing: {exc}", file=sys.stderr)
            return 0
        print(
            color_loop_block(
                f"[loop] recorded cycle: {metrics.format_duration(cycle['duration_seconds'])} "
                f"active ({cycle['finding_count']} finding(s), outcome={cycle['outcome']}).",
                enabled=color_enabled,
            )
        )
        return 0

    try:
        prefs = load_preferences_with_fallback()
        args.max_rereview_requests = effective_rereview_limit(
            args.max_rereview_requests, prefs
        )
        pr = resolve_pr(args.pr)
        if args.reset_reviewer:
            cleared = clear_reviewer_selection(pr)
            status = "cleared" if cleared else "not_set"
            if args.format == "json":
                print(json.dumps({"reviewerSelection": {"status": status}}, indent=2, sort_keys=True))
            else:
                message = (
                    "[loop] Reviewer selection cleared for this PR."
                    if cleared
                    else "[loop] Reviewer selection was not set for this PR."
                )
                print(color_loop_block(message, enabled=color_enabled))
            return 0
        if args.list_reviewers:
            discovery = fetch_reviewer_discovery(pr)
            pull_request_for_discovery = discovery["pull_request"]
            self_login = None if args.no_agent_filter else (args.agent_login or gh_authenticated_login())
            candidates = reviewer_resolver.discover_candidates(
                pull_request_for_discovery,
                self_login=self_login,
            )
            partial = bool(discovery.get("partial"))
            warnings = list(discovery.get("warnings") or [])
            if args.format == "json":
                print(
                    json.dumps(
                        {
                            "reviewers": [candidate.to_dict() for candidate in candidates],
                            "partial": partial,
                            "warnings": warnings,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                for warning in warnings:
                    print(f"warning: {warning}", file=sys.stderr)
                if candidates:
                    for candidate in candidates:
                        trigger = candidate.review_trigger or "(no safe trigger known)"
                        print(f"- {candidate.display_name} `{candidate.login}` trigger={trigger}")
                elif partial:
                    print("[loop] Reviewer discovery incomplete; retry or select a reviewer manually.")
                else:
                    print("[loop] No AI reviewer threads found on this PR.")
            return 0
        reviewer_selection = resolve_reviewer_selection(args, pr)
        args.author = reviewer_selection["login"]
        args.reviewer_name = reviewer_selection["display_name"]
        args.review_trigger_mention = reviewer_selection.get("review_trigger")
        if args.fixed_finding or args.fixed_path:
            try:
                accumulate_fixed_markers(
                    pr,
                    fingerprints=args.fixed_finding,
                    paths=args.fixed_path,
                )
            except OSError as exc:
                print(f"warning: could not persist fixed markers: {exc}", file=sys.stderr)
        if args.swept_pattern:
            try:
                accumulate_swept_patterns(pr, args.swept_pattern)
            except OSError as exc:
                print(f"warning: could not persist swept patterns: {exc}", file=sys.stderr)
        if args.wait and args.wait_chunk_seconds is not None:
            if args.wait_chunk_seconds <= 0:
                parser.error("--wait-chunk-seconds must be a positive integer.")
            chunk = run_wait_chunk(
                pr,
                args.author,
                timeout_seconds=args.timeout,
                interval_seconds=args.interval,
                quiet_seconds=args.quiet_period,
                after_iso=args.after,
                chunk_seconds=args.wait_chunk_seconds,
            )
            if chunk["status"] != "ready":
                wait_fields = {
                    k: v
                    for k, v in chunk.items()
                    if k not in ("pull_request", "author") and v is not None
                }
                if chunk["status"] == "refused":
                    print_reviewer_refusal(
                        chunk,
                        author=args.author,
                        json_output=args.format == "json",
                        color_enabled=color_enabled,
                    )
                    return 0
                if args.format == "json":
                    print(json.dumps({"wait": wait_fields}, indent=2, sort_keys=True))
                else:
                    print(
                        color_loop_block(
                            metrics.format_wait_heartbeat(
                                chunk["status"],
                                author=args.author,
                                elapsed_seconds=chunk.get("elapsed_seconds", 0),
                                checks=chunk.get("checks", 0),
                                next_wait_seconds=chunk.get("next_wait_seconds", 0),
                                quiet_period_remaining_seconds=chunk.get(
                                    "quiet_period_remaining_seconds"
                                ),
                            ),
                            enabled=color_enabled,
                        )
                    )
                return 0
            pull_request = chunk["pull_request"]
        elif args.wait:
            try:
                pull_request = wait_for_stable_review(
                    pr,
                    author=args.author,
                    timeout_seconds=args.timeout,
                    interval_seconds=args.interval,
                    quiet_seconds=args.quiet_period,
                    after_iso=args.after,
                )
            except ReviewerRefused as refused:
                print_reviewer_refusal(
                    {"status": "refused", **refused.refusal},
                    author=args.author,
                    json_output=args.format == "json",
                    color_enabled=color_enabled,
                )
                return 0
            except WaitTimedOut as timed_out:
                # Mirror the chunked-mode timed_out contract: deterministic
                # relayable output, exit 0. Matters most when this wait ran as
                # a background task and this is the tail of its captured log.
                if args.format == "json":
                    print(
                        json.dumps(
                            {"wait": {
                                "status": "timed_out",
                                "elapsed_seconds": timed_out.elapsed_seconds,
                            }},
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    print(
                        color_loop_block(
                            metrics.format_wait_heartbeat(
                                "timed_out",
                                author=args.author,
                                elapsed_seconds=timed_out.elapsed_seconds,
                            ),
                            enabled=color_enabled,
                        )
                    )
                return 0
        else:
            pull_request = fetch_threads(pr)
        resolved_outdated = 0
        resolved_addressed_by_reply = 0
        if args.no_agent_filter:
            agent_login: str | None = None
        else:
            agent_login = args.agent_login or gh_authenticated_login()
            if agent_login:
                print(f"Counting only re-reviews posted by '{agent_login}' toward the cap.", file=sys.stderr)
            else:
                print(
                    "warning: could not detect agent login via `gh api user`; "
                    "falling back to counting all re-review pings.",
                    file=sys.stderr,
                )
        rereviews = rereview_requests(
            pull_request,
            agent_login,
            review_trigger_mention=args.review_trigger_mention,
        )
        _limit_reached = len(rereviews) >= args.max_rereview_requests
        if args.resolve_outdated:
            resolved_outdated = resolve_outdated_threads(
                pull_request, args.author, dry_run=args.dry_run
            )
            if resolved_outdated:
                tag = "[dry-run] would resolve" if args.dry_run else "Resolved"
                print(f"{tag} {resolved_outdated} outdated {args.author} thread(s).", file=sys.stderr)
                if not args.dry_run:
                    pull_request = fetch_threads(pr)
        if args.resolve_addressed_by_reply:
            resolved_addressed_by_reply = resolve_addressed_by_reply(
                pull_request, args.author, dry_run=args.dry_run
            )
            if resolved_addressed_by_reply:
                tag = "[dry-run] would resolve" if args.dry_run else "Resolved"
                print(
                    f"{tag} {resolved_addressed_by_reply} addressed-by-reply "
                    f"{args.author} thread(s).",
                    file=sys.stderr,
                )
                if not args.dry_run:
                    pull_request = fetch_threads(pr)
        threads = filter_threads(
            pull_request,
            author=args.author,
            include_resolved=args.include_resolved,
            include_outdated=args.include_outdated,
            include_addressed_by_reply=args.include_addressed_by_reply,
        )
        # Findings the reviewer wrote into the review body have no thread to
        # fetch, so they are appended as synthetic current feedback.
        threads.extend(review_body_findings(pull_request, args.author))
        threads = sort_by_severity(threads)
        if not args.record_run and not args.stats:
            try:
                update_run_tracking(
                    pr,
                    [(t["id"], t.get("path", "")) for t in threads],
                    bump_session_cycle=not args.cycle_summary,
                )
            except OSError as exc:
                print(f"warning: could not update run tracking: {exc}", file=sys.stderr)
        if args.min_severity or args.drop_unknown_severity:
            before = len(threads)
            threads = filter_by_min_severity(
                threads, args.min_severity, keep_unknown=not args.drop_unknown_severity
            )
            dropped = before - len(threads)
            if dropped:
                if args.min_severity:
                    tag = " (kept unknown-severity)" if not args.drop_unknown_severity else ""
                    msg = f"--min-severity {args.min_severity}: dropped {dropped} thread(s){tag}."
                else:
                    msg = f"--drop-unknown-severity: dropped {dropped} unknown-severity thread(s)."
                print(msg, file=sys.stderr)
        page_warnings = pagination_warnings(pull_request)
        for warning in page_warnings:
            print(f"warning: {warning}", file=sys.stderr)

        # ---- No-progress detection ------------------------------------------
        # Compare the actionable thread fingerprint to the previous cycle's.
        # Only runs during a real fetch (not --cycle-summary / --record-run /
        # --stats), so the comparison reflects actual loop cycles, not
        # mid-cycle read-only calls.
        no_progress_flag = False
        if not args.record_run and not args.cycle_summary and not args.stats:
            current_fp = thread_fingerprint(threads)
            try:
                no_progress_flag = detect_no_progress(pr, current_fp)
            except OSError as exc:
                print(f"warning: could not check progress: {exc}", file=sys.stderr)
            # Snapshot prior-cycle finding fingerprints so the receipt can mark
            # carried-over findings. Real-fetch only, once per cycle.
            try:
                track_finding_fingerprints(
                    pr, {finding_fingerprint(t) for t in threads if isinstance(t, dict)}
                )
            except OSError as exc:
                print(f"warning: could not track finding fingerprints: {exc}", file=sys.stderr)
            try:
                track_pattern_signatures(
                    pr,
                    {cluster_findings.pattern_signature(t) for t in threads if isinstance(t, dict)}
                    - {""},
                )
            except OSError as exc:
                print(f"warning: could not track pattern signatures: {exc}", file=sys.stderr)

        # ---- Judge (optional, opt-in via prefs file or --judge-mode) -------
        # The script is the single source of truth for whether the judge
        # runs. The agent supplies --judge-phase; we read the saved mode
        # from ~/.config/gh-review-loop/preferences.json (or
        # --judge-mode override) and decide.
        try:
            from judge import (  # noqa: PLC0415
                JudgeClient, JudgeError, should_judge_run, skipped_result,
            )
            judge_available = True
        except ImportError:
            judge_available = False
            judge_results: dict[str, dict[str, Any]] = {}
            judge_status = {
                "ran": False,
                "skip_reason": "judge.py not importable (plugin install path issue)",
            }
        if judge_available:
            effective_mode = (
                args.judge_mode
                if args.judge_mode is not None
                else prefs.get("judge_mode", "off")
            )
            effective_phase = resolve_judge_phase(
                args.judge_phase, record_run=bool(args.record_run)
            )
            judge_results = {}
            if should_judge_run(mode=effective_mode, phase=effective_phase):
                client = JudgeClient(model=args.judge_model or prefs["judge_model"])
                ready, skip_reason = client.is_ready()
                if not ready:
                    judge_status = {
                        "ran": False,
                        "mode": effective_mode,
                        "phase": effective_phase,
                        "skip_reason": skip_reason,
                    }
                    print(
                        f"info: judge skipped — {skip_reason}. "
                        "Loop continues unchanged.",
                        file=sys.stderr,
                    )
                else:
                    judge_status = {
                        "ran": True,
                        "mode": effective_mode,
                        "phase": effective_phase,
                        "model": client.model,
                        "judged_count": 0,
                        "errors": 0,
                    }
                    for thread in threads:
                        # Build a single judge input from the first matching
                        # bot comment in this filtered thread.
                        body = (thread.get("comments") or [{}])[0].get("body") or ""
                        finding_payload = {
                            "severity": thread_severity(thread),
                            "path": thread.get("path"),
                            "line": thread.get("line") or thread.get("originalLine"),
                            "body": body,
                            "diff_hunk": (thread.get("comments") or [{}])[0].get("diffHunk") or "",
                        }
                        try:
                            jr = client.judge(finding_payload)
                            judge_results[thread["id"]] = dataclasses.asdict(jr)
                            if jr.status == "ok":
                                judge_status["judged_count"] += 1
                        except JudgeError as exc:
                            judge_results[thread["id"]] = dataclasses.asdict(
                                skipped_result(f"judge error: {exc}")
                            )
                            judge_status["errors"] += 1
            else:
                judge_status = {
                    "ran": False,
                    "mode": effective_mode,
                    "phase": effective_phase,
                    "skip_reason": (
                        f"mode={effective_mode!r} + phase={effective_phase!r} → no-op"
                    ),
                }
        # --------------------------------------------------------------------

        # Persist this cycle's verdicts into the run accumulator so the terminal
        # --record-run can report the eval even after findings are resolved.
        if not args.record_run and not args.stats and judge_results:
            try:
                accumulate_judge_results(pr, judge_results)
            except OSError as exc:
                print(f"warning: could not persist judge results: {exc}", file=sys.stderr)

        rereviews = rereview_requests(
            pull_request,
            agent_login,
            review_trigger_mention=args.review_trigger_mention,
        )
        if len(rereviews) >= args.max_rereview_requests:
            print(
                f"warning: {len(rereviews)} reviewer re-review request(s) already exist; "
                f"the configured loop cap is {args.max_rereview_requests}.",
                file=sys.stderr,
            )
        if args.record_run or args.cycle_summary:
            try:
                # --record-run owns accumulation here because it skips the
                # per-fetch tracking above; --cycle-summary just reads what that
                # path already accumulated this cycle.
                if args.record_run:
                    update_run_tracking(
                        pr,
                        [(t["id"], t.get("path", "")) for t in threads],
                        bump_session_cycle=False,
                    )
                run = read_run_tracking(pr)
            except OSError as exc:
                print(f"warning: could not update or read run tracking: {exc}", file=sys.stderr)
                run = {}
            baseline_ids = set(run.get("finding_ids", []))
            finding_paths = run.get("finding_paths", [])
            current_actionable_ids = {t["id"] for t in threads}
            addressed_by_reply_ids = {
                t["id"] for t in addressed_by_reply_threads(pull_request, args.author)
            }
            # Merge verdicts accumulated across cycles with this invocation's,
            # so the record reflects the whole run even when the terminal pass
            # has no live findings to re-judge. Current invocation supersedes.
            if not isinstance(run, dict):
                run = {}
            accumulated_results = run.get("judge_results")
            if not isinstance(accumulated_results, dict):
                accumulated_results = {}
            merged_judge_results = merge_judge_results(accumulated_results, judge_results)
            judge_ran = bool(run.get("judge_ran")) or bool(judge_status.get("ran"))
            cap_reached = len(rereviews) >= args.max_rereview_requests
            fixed_markers = read_fixed_markers(pr)
            changed_paths = changed_files_in_range(args.changed_base, args.changed_head)
            remaining_states = classify_remaining_finding_states(
                threads,
                fixed_fingerprints=fixed_markers["fingerprints"],
                fixed_paths=fixed_markers["paths"],
                changed_paths=changed_paths,
                prior_fingerprints=prior_finding_fingerprints(pr),
                judge_results=merged_judge_results,
                cap_reached=cap_reached,
            )
            likely_fixed_remaining = count_likely_fixed_remaining(remaining_states)
            outcome = args.outcome or _derive_outcome(
                len(current_actionable_ids),
                args.verification,
                cap_reached,
                gemini_confirmed=args.gemini_confirmed,
                likely_fixed_remaining=likely_fixed_remaining,
            )
            derived = derive_record_fields(
                baseline_ids=baseline_ids,
                current_actionable_ids=current_actionable_ids,
                addressed_by_reply_ids=addressed_by_reply_ids,
                outcome=outcome,
                judge_ran=judge_ran,
                judge_results=merged_judge_results,
            )
            terminal_breakdown = {
                "confirmed_fixed_outdated": derived["observed_fixed_count"],
                "fixed_pending_confirmation": likely_fixed_remaining,
                "remaining_valid_actionable": max(
                    0,
                    derived["remaining_actionable"]
                    - derived["needs_human"]
                    - likely_fixed_remaining,
                ),
                "needs_human": derived["needs_human"],
            }
            verification_details: dict[str, Any] = {}
            if args.verification_details:
                try:
                    verification_details = json.loads(args.verification_details)
                except json.JSONDecodeError:
                    print(
                        "warning: --verification-details is not valid JSON; storing {}.",
                        file=sys.stderr,
                    )
            # Pass the repo root so clustering can merge findings the reviewer
            # worded differently but which anchor to the same code shape. Falls
            # back to prose-only clustering when the root cannot be resolved.
            clusters = cluster_findings.cluster(
                [t for t in threads if isinstance(t, dict)],
                root=repo_root_for_pr(pull_request),
            )
            # Cross-run history: --record-run clears the live accumulator, so a
            # resumed loop folds in pattern signatures recorded by prior runs of
            # this PR (runs.jsonl) — otherwise recurrence resets to 0 on resume.
            # Canonical on both sides: records are matched by exact repo
            # string, so the read key and the write key must agree regardless
            # of how the caller spelled --pr.
            metrics_repo = canonical_repo_full(pull_request, pr)
            history = metrics.pattern_history_for_pr(metrics_repo, pr.number)
            convergence = build_convergence(pr, clusters, history)
            conv_stats = convergence["stats"]
            record = metrics.build_record(
                repo=metrics_repo,
                pr=pr.number,
                provider=args.author,
                fixed_count=args.fixed_count,
                cycles_used=len(rereviews),
                cycle_cap=args.max_rereview_requests,
                verification=args.verification,
                verification_details=verification_details,
                outcome=outcome,
                outcome_reason=args.outcome_reason or f"outcome: {outcome}",
                session_cycle=_safe_int(run.get("session_cycle")) or None,
                started_at=run.get("started_at"),
                finding_paths=finding_paths,
                judge=metrics.build_judge_block(judge_ran, merged_judge_results),
                cycles=run.get("cycles", []),
                terminal_breakdown=terminal_breakdown,
                patterns={
                    "distinct_patterns": conv_stats["distinct_patterns"],
                    "max_cluster_size": max((c.count for c in clusters), default=0),
                    "pattern_recurrence_rate": round(conv_stats["recurrence_rate"], 3),
                    "swept_count": convergence["swept_count"],
                    # Persisted so later runs of this PR can compute cross-run
                    # recurrence after --record-run clears the live accumulator.
                    # Only this run's own sweeps: pattern_history_for_pr unions
                    # across records, so writing the union back here would
                    # compound it into every later record and the count could
                    # never fall again.
                    "signatures": sorted({c.signature for c in clusters}),
                    "swept": convergence["swept"],
                } if clusters else None,
                **derived,
            )
            # Read the carried-over classification BEFORE the terminal branch:
            # --record-run calls clear_run_tracking(), which deletes the very
            # run block prior_finding_fingerprints() reads. Reading it after
            # would mark every finding on a capped/human/stopped receipt "new".
            prior_fps = prior_finding_fingerprints(pr)
            # --cycle-summary is read-only: print the block but never append a
            # record or clear the accumulator, so it is safe to call every cycle.
            if args.record_run:
                try:
                    metrics.append_record(record)
                except OSError as exc:
                    print(f"warning: could not record run metrics: {exc}", file=sys.stderr)
                else:
                    try:
                        clear_run_tracking(pr)
                    except OSError as exc:
                        print(f"warning: could not clear run tracking: {exc}", file=sys.stderr)
            else:
                # --cycle-summary leaves the accumulator intact but stamps that a
                # summary covering the current run state was emitted, so the
                # Stop-hook backstop won't duplicate it.
                try:
                    stamp_summary_emitted(pr)
                except OSError as exc:
                    print(f"warning: could not stamp summary state: {exc}", file=sys.stderr)
            if args.auto_snapshot:
                print(color_loop_block(metrics.format_auto_snapshot(record), enabled=color_enabled))
                return 0
            # Single-channel receipt delivery (#86): the full receipt goes to
            # the sticky PR comment, chat gets a one-line pointer. stdout keeps
            # the full receipt only as the fail-open fallback (dry-run, or the
            # comment write failed) so a receipt can never be lost.
            summary_block = metrics.format_run_summary(record, terminal=bool(args.record_run))
            semantic_risk_block = metrics.format_semantic_risk_block(args.semantic_risk)
            suite_block = metrics.format_suite_block(verification_details)
            patterns_block = metrics.format_patterns_block(clusters)
            clustering_advisory = metrics.format_degenerate_clustering_advisory(clusters)
            convergence_line = convergence["line"]
            findings_view = []
            for thread in threads:
                comments = _iter_comments(thread)
                line_val = thread.get("line")
                findings_view.append({
                    "path": thread.get("path"),
                    "line": line_val if line_val is not None else thread.get("originalLine"),
                    "severity": thread_severity(thread),
                    "url": comments[0].get("url") if comments and isinstance(comments[0], dict) else None,
                    "carried": finding_fingerprint(thread) in prior_fps,
                })
            findings_block = metrics.format_findings_block(findings_view)

            receipt_url = None
            if not args.dry_run:
                receipt_status = "RUNNING" if not args.record_run else (
                    "DONE" if record.get("outcome") == "clean" else "STOPPED"
                )
                body = build_receipt_comment_body(
                    [
                        summary_block,
                        semantic_risk_block,
                        suite_block,
                        patterns_block,
                        clustering_advisory,
                        convergence_line,
                    ],
                    status=receipt_status,
                    findings_block=findings_block,
                )
                try:
                    comment_id = post_or_update_sticky_receipt(pr, body)
                except (RuntimeError, OSError) as exc:
                    comment_id = None
                    print(
                        f"warning: could not deliver the receipt to the PR comment: {exc}. "
                        "Printing the full receipt instead.",
                        file=sys.stderr,
                    )
                if comment_id:
                    receipt_url = (
                        f"https://github.com/{pr.owner}/{pr.repo}/pull/"
                        f"{pr.number}#issuecomment-{comment_id}"
                    )

            if receipt_url:
                new_count = sum(1 for f in findings_view if not f.get("carried"))
                print(
                    color_loop_block(
                        metrics.format_compact_receipt_line(
                            record,
                            terminal=bool(args.record_run),
                            receipt_url=receipt_url,
                            findings_new=new_count,
                            findings_carried=len(findings_view) - new_count,
                        ),
                        enabled=color_enabled,
                    )
                )
                if judge_eval_requested(judge_status):
                    print(
                        color_loop_block(
                            metrics.format_judge_skip(judge_status.get("skip_reason", "")),
                            enabled=color_enabled,
                        )
                    )
                # The clustering advisory changes what the agent must do BEFORE
                # fixing (manual sweep), so it stays in chat alongside the
                # pointer; everything else lives in the PR comment.
                if clustering_advisory:
                    print(clustering_advisory)
            else:
                print(color_loop_block(summary_block, enabled=color_enabled))
                if judge_eval_requested(judge_status):
                    print(
                        color_loop_block(
                            metrics.format_judge_skip(judge_status.get("skip_reason", "")),
                            enabled=color_enabled,
                        )
                    )
                if semantic_risk_block:
                    print(color_loop_block(semantic_risk_block, enabled=color_enabled))
                if suite_block:
                    print(suite_block)
                # Printed directly (like suite_block / findings_block): these
                # start with "Patterns ("/"Convergence:", not "[loop]", so
                # color_loop_block would be a no-op wrap.
                if patterns_block:
                    print(patterns_block)
                if clustering_advisory:
                    print(clustering_advisory)
                if convergence_line:
                    print(convergence_line)
                if findings_block:
                    print(findings_block)
            return 0
        if args.post_receipt or args.sticky_receipt:
            sticky = args.sticky_receipt
            status = args.receipt_status.upper() if args.receipt_status else (
                "RUNNING" if sticky else None
            )
            receipt = render_receipt(
                pr, pull_request, threads,
                author=args.author,
                resolved_outdated=resolved_outdated,
                resolved_addressed_by_reply=resolved_addressed_by_reply,
                rereview_count=len(rereviews),
                rereview_limit=args.max_rereview_requests,
                status=status,
                sticky=sticky,
            )
            if sticky:
                post_or_update_sticky_receipt(pr, receipt, dry_run=args.dry_run)
            else:
                post_pr_comment(pr, receipt, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Compute once for both human and machine fetch output. The advisory is a
    # pre-fix ordering guard, so JSON automation must receive it too.
    fetch_clusters = cluster_findings.cluster(
        [thread for thread in threads if isinstance(thread, dict)],
        root=repo_root_for_pr(pull_request),
    )
    clustering_advisory = metrics.format_degenerate_clustering_advisory(fetch_clusters)

    # Delta collapse baseline (#95): thread_id → render_fp emitted last cycle.
    # Read fails open to {} (everything renders full — the safe direction).
    try:
        render_baseline = read_render_baseline(pr, args.author)
    except OSError as exc:
        print(f"warning: could not read render baseline: {exc}", file=sys.stderr)
        render_baseline = {}
    current_render_fps: dict[str, str] = {}
    for thread in threads:
        thread_id = thread.get("id")
        if isinstance(thread_id, str):
            jr = judge_results.get(thread_id) if judge_results else None
            current_render_fps[thread_id] = thread_render_fingerprint(thread, jr)

    # Profile blocks ride along with the fetch (#99) so the agent doesn't pay
    # two extra script invocations per cycle for --profile-intro and
    # --planned-verification.
    #
    # They can only describe a profile that already exists. On a first run the
    # decision happens after this fetch, so blocks built here would say "none
    # saved / ad hoc" while the agent goes on to save a profile and run it —
    # the relayed text would contradict the suite actually used (#107). Emit
    # the regenerate notice for that path and keep the standalone flags as the
    # post-decision source. A `skipped` or malformed profile is *not* this
    # case: a decision exists, and its fallback wording is the final answer.
    profile_repo = canonical_repo_full(pull_request, pr)
    try:
        saved_profile = load_profile_for_repo(profile_repo)
    except Exception:  # pragma: no cover - load_profile_for_repo already swallows
        saved_profile = None
    profile_blocks_provisional = saved_profile is None
    if profile_blocks_provisional:
        profile_intro_block = metrics.format_pending_profile_block(
            profile_repo, script=str(Path(__file__).resolve())
        )
        planned_verification_block = ""
    else:
        profile_intro_block = metrics.format_profile_intro_block(saved_profile, profile_repo)
        planned_verification_block = metrics.format_planned_verification_block(saved_profile)

    if args.format == "json":
        # The machine path stays lossless: full threads, annotated with the
        # delta bit. It never commits the baseline — only markdown emission
        # (what the agent actually consumed) moves it.
        threads_payload = []
        for thread in threads:
            copied = dict(thread)
            thread_id = thread.get("id")
            copied["changedSinceLastCycle"] = not (
                isinstance(thread_id, str)
                and render_baseline.get(thread_id) == current_render_fps.get(thread_id)
            )
            threads_payload.append(copied)
        print(
            json.dumps(
                {
                    "pullRequest": pull_request,
                    "threads": threads_payload,
                    "clustering": {
                        "clusterCount": len(fetch_clusters),
                        "maxClusterSize": max(
                            (cluster.count for cluster in fetch_clusters), default=0
                        ),
                        "advisory": clustering_advisory,
                    },
                    "reviewerSelection": reviewer_selection,
                    "loopStatus": {
                        # Script-owned narration ordinal (#104): copy it into
                        # "session cycle N" lines; never model-compute it.
                        "sessionCycle": _safe_int(
                            read_run_tracking(pr).get("session_cycle")
                        ) or None,
                        "reReviewRequests": len(rereviews),
                        "reReviewLimit": args.max_rereview_requests,
                        "limitReached": len(rereviews) >= args.max_rereview_requests,
                        "agentLogin": agent_login,
                        "unresolvedOutdatedThreads": len(outdated_unresolved_threads(pull_request, args.author)),
                        "addressedByReplyThreads": len(addressed_by_reply_threads(pull_request, args.author)),
                        "resolvedOutdatedThreads": resolved_outdated,
                        "resolvedAddressedByReplyThreads": resolved_addressed_by_reply,
                        "severityCounts": severity_counts(threads),
                        "pageLimitWarnings": page_warnings,
                        "dryRun": args.dry_run,
                        "actionableThreadFingerprint": thread_fingerprint(threads),
                        "noProgressDetected": no_progress_flag,
                        "judge": judge_status,
                    },
                    "judgeResults": judge_results,
                    "humanBlocks": {
                        "profileIntro": profile_intro_block,
                        # Empty (not a fallback string) while provisional, so
                        # automation cannot relay a suite that has not been
                        # chosen yet.
                        "plannedVerification": planned_verification_block,
                        "profileBlocksProvisional": profile_blocks_provisional,
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        # Script-owned narration ordinal (#104): the agent copies this N into
        # its "session cycle N" lines instead of tracking a counter that
        # drifts across context compaction.
        session_cycle_n = _safe_int(read_run_tracking(pr).get("session_cycle"))
        if session_cycle_n:
            print(
                color_loop_block(
                    f"[loop] session cycle {session_cycle_n}", enabled=color_enabled
                )
            )
        print(
            render_markdown(
                pull_request,
                threads,
                args.author,
                reviewer_name=args.reviewer_name,
                review_trigger_mention=args.review_trigger_mention,
                judge_results=judge_results,
                baseline=render_baseline,
                full=args.full,
            ),
            end="",
        )
        # The clustering warning is an ordering guard, so show it on the
        # initial fetch while the agent is deciding what to fix. Summary-mode
        # output repeats it later as an audit trail.
        print_clustering_blocks(fetch_clusters)
        if judge_status.get("ran") and judge_results:
            print(
                color_loop_block(
                    format_judge_thread_table(
                        threads, judge_results, judge_status.get("phase", "cycle")
                    ),
                    enabled=color_enabled,
                ),
            )
        elif judge_eval_requested(judge_status):
            print(
                color_loop_block(
                    metrics.format_judge_skip(judge_status.get("skip_reason", "")),
                    enabled=color_enabled,
                )
            )
        if no_progress_flag:
            print(
                color_loop_block(
                    "[loop] no_progress: actionable thread set is unchanged since the previous "
                    "cycle — no fix landed. Stop with: "
                    "--record-run --outcome no_progress "
                    "--outcome-reason 'no code change resolved any open thread'",
                    enabled=color_enabled,
                )
            )
        print(color_loop_block(profile_intro_block, enabled=color_enabled))
        if planned_verification_block:
            print(color_loop_block(planned_verification_block, enabled=color_enabled))
        # Commit the delta baseline ONLY after the rendered output has been
        # emitted (#95): render → print → flush → persist. Persisting first
        # would collapse threads the agent never saw if this process dies
        # mid-emission. Save failure fails open — next cycle renders full.
        sys.stdout.flush()
        try:
            save_render_baseline(pr, args.author, current_render_fps)
        except OSError as exc:
            print(f"warning: could not save render baseline: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
