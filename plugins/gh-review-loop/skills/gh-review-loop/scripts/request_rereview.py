#!/usr/bin/env python3
"""Post an AI reviewer re-review request comment for a PR."""

from __future__ import annotations

import argparse
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


def record_rereview_posted(repo: str, pr: int) -> None:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Request an AI reviewer re-review by posting a PR comment."
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
