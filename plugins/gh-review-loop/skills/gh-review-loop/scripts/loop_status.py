#!/usr/bin/env python3
"""Where the review loop is, from local state alone, and the one next command.

Reads state.json (the run block's phase, cap counters and wait snapshot, the
persisted reviewer, the last recorded run), the PR's provenance files, the
saved verification profile and the local git checkout. Never calls ``gh``:
the answer must be free to ask and must work offline, after a resumed or
compacted session, or a day later.

The resume ladder that used to be prose in ``references/resume-and-recovery.md``
is what ``decide_next`` implements: an unpushed commit after a summary means
push; a pushed head with no request means request; a request older than the
head means request again; a wait that ended means re-run it or record.

    python3 loop_status.py [--pr <url|number>] [--format json]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import loop_state  # noqa: E402 — sibling module, stdlib-only
from hook_runtime import python_script_command  # noqa: E402
from loop_color import color_loop, colors_enabled  # noqa: E402

ORIGIN_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$", re.IGNORECASE
)
PR_URL_RE = re.compile(r"github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)")
# The skill's own wait for a re-review.
WAIT_TIMEOUT_SECONDS = 1800


# ---------------------------------------------------------------------------
# Local facts
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: str | None) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def parse_origin_url(url: str | None) -> str | None:
    """``owner/repo`` from an https, ssh or scp-style GitHub remote URL."""
    if not isinstance(url, str):
        return None
    match = ORIGIN_RE.search(url.strip())
    return f"{match.group('owner')}/{match.group('repo')}" if match else None


def repo_from_origin(cwd: str | None = None) -> str | None:
    return parse_origin_url(_git(["remote", "get-url", "origin"], cwd))


def local_git(cwd: str | None = None) -> dict[str, Any]:
    """HEAD, its upstream, and how far apart they are; None where unknown."""
    head = _git(["rev-parse", "HEAD"], cwd)
    upstream = _git(["rev-parse", "@{u}"], cwd) if head else None
    ahead = behind = None
    if upstream:
        ahead = _count(_git(["rev-list", "--count", "@{u}..HEAD"], cwd))
        behind = _count(_git(["rev-list", "--count", "HEAD..@{u}"], cwd))
    return {"head": head or None, "upstream": upstream or None, "ahead": ahead, "behind": behind}


def _count(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def parse_pr_arg(value: str | None, repo_hint: str | None) -> tuple[str, int] | None:
    """``(repo, number)`` from a PR URL, or from a bare number plus the repo
    the checkout points at."""
    if not value:
        return None
    match = PR_URL_RE.search(value)
    if match:
        return f"{match.group('owner')}/{match.group('repo')}", int(match.group("number"))
    if value.isdigit() and repo_hint:
        return repo_hint, int(value)
    return None


def read_preferences() -> dict[str, Any]:
    """preferences.json as saved, or {}.

    Read straight from the file: ``judge.load_preferences`` writes the
    defaults when the file is absent, and a status check must not write.
    """
    path = loop_state.state_dir() / "preferences.json"
    try:
        prefs = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return prefs if isinstance(prefs, dict) else {}


def preferred_cap(prefs: dict[str, Any]) -> int | None:
    """``max_rereview_requests`` as saved, when it is a usable number."""
    value = prefs.get("max_rereview_requests")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def profile_source(repo: str, prefs: dict[str, Any] | None = None) -> str | None:
    """The saved verification profile's ``source`` for ``repo``, or None."""
    prefs = read_preferences() if prefs is None else prefs
    profiles = prefs.get("profiles")
    if not isinstance(profiles, dict):
        return None
    wanted = repo.casefold()
    for key, profile in profiles.items():
        if isinstance(key, str) and key.casefold() == wanted and isinstance(profile, dict):
            source = profile.get("source")
            return source if isinstance(source, str) else "unknown"
    return None


def sentinel_state(now: float | None = None) -> str:
    if not loop_state.sentinel_path().exists():
        return "absent"
    return "stale" if loop_state.sentinel_is_stale(now) else "fresh"


# ---------------------------------------------------------------------------
# Which PR
# ---------------------------------------------------------------------------


def _entries_for(state: dict[str, Any], repo: str) -> list[tuple[str, int, dict[str, Any]]]:
    prefix = f"{repo}#".casefold()
    out: list[tuple[str, int, dict[str, Any]]] = []
    for key, entry in state.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        if not key.casefold().startswith(prefix):
            continue
        number = key[len(prefix):]
        if number.isdigit():
            out.append((key, int(number), entry))
    return out


def select_entry(
    state: dict[str, Any], repo: str, pr: int | None = None
) -> tuple[str, int, dict[str, Any]] | None:
    """The PR to report on: the one asked for, else the repo's active run,
    else its newest run block, else its most recently recorded run."""
    if pr is not None:
        wanted = f"{repo}#{pr}".casefold()
        for key, entry in state.items():
            if isinstance(key, str) and key.casefold() == wanted:
                return key, pr, entry if isinstance(entry, dict) else {}
        return f"{repo}#{pr}", pr, {}
    entries = _entries_for(state, repo)
    if not entries:
        return None

    def run_of(entry: dict[str, Any]) -> dict[str, Any]:
        run = entry.get("run")
        return run if isinstance(run, dict) else {}

    active = [e for e in entries if loop_state.safe_int(run_of(e[2]).get("update_seq")) > 0]
    if active:
        return max(active, key=lambda e: str(run_of(e[2]).get("started_at", "")))
    with_run = [e for e in entries if run_of(e[2])]
    if with_run:
        return max(with_run, key=lambda e: str(run_of(e[2]).get("phase_at", "")))
    recorded = [e for e in entries if isinstance(e[2].get("last_run"), dict)]
    if recorded:
        return max(recorded, key=lambda e: str(e[2]["last_run"].get("recorded_at", "")))
    return entries[0]


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def commands(repo: str, pr: int, cwd: str | None = None) -> dict[str, str]:
    """The commands ``Next:`` can name. With ``cwd`` (the ``--cwd`` checkout
    the git facts came from) every git invocation is bound to it, so a
    command copied from the block acts on the checkout that was judged."""
    fetch = python_script_command("fetch_gemini_threads.py")
    request = python_script_command("request_rereview.py")
    detect = python_script_command("detect_profile.py")
    git = f'git -C "{cwd}"' if cwd else "git"
    url = f"https://github.com/{repo}/pull/{pr}"
    return {
        "url": url,
        "fetch": f"{fetch} --pr {url}",
        "list_reviewers": f"{fetch} --pr {url} --list-reviewers --format json",
        "summary": (
            f"{fetch} --pr {url} --cycle-summary --fixed-count <n> "
            "--verification <passed|failed|skipped>"
        ),
        "request": f'{request} --repo {repo} --pr {pr} --expect-head "$({git} rev-parse HEAD)" --json',
        "detect_profile": f'{detect} "$({git} rev-parse --show-toplevel)"',
        "record": f"{fetch} --pr {url} --record-run",
        "wait": f"{fetch} --pr {url} --wait",
        "push": f"{git} push",
    }


def decide_next(facts: dict[str, Any]) -> dict[str, str | None]:
    """The one next action for ``facts`` (see ``build_status`` for the keys).

    Precedence: a reviewer must be selected before anything else; an open
    reservation is settled by the request script before any new request;
    then the phase decides, with the verification profile checked before the
    first edit and the cap checked before any new fix or request.
    """
    cmd = facts["commands"]
    reviewer = facts.get("reviewer")
    phase = facts.get("phase")
    run = facts.get("run") or {}
    git = facts.get("git") or {}
    ahead = git.get("ahead")
    cap_reached = bool(run.get("cap_reached"))

    def answer(action: str, command: str | None) -> dict[str, str | None]:
        return {"action": action, "command": command}

    if not reviewer:
        return answer("select a reviewer: none is persisted for this PR", cmd["list_reviewers"])
    if facts.get("reservation_open"):
        return answer(
            "settle the earlier unconfirmed re-review request; the request script does it first",
            cmd["request"],
        )
    if phase is None:
        last = facts.get("last_run")
        if isinstance(last, dict) and last.get("outcome"):
            return answer(
                f"start a new loop; the last one ended {last['outcome']} at {last.get('recorded_at', '?')}",
                cmd["fetch"],
            )
        return answer("fetch the reviewer's threads", cmd["fetch"])
    if phase == "fetched":
        if facts.get("profile") is None:
            return answer(
                "save a verification profile before editing (the edit gate blocks until then)",
                cmd["detect_profile"],
            )
        if cap_reached:
            return answer(
                "cap reached: record the run, or raise the cap with --max-rereview-requests",
                f"{cmd['record']} --outcome capped",
            )
        return answer("fix the findings, verify, then emit the cycle summary", cmd["summary"])
    head = git.get("head")
    requested_at = run.get("requested_at")
    requested_head = run.get("requested_head")
    if phase == "summarized":
        # Unpushed work is finished before the cap is applied: recording
        # would tear the run down with the fixes unpublished (resume case 2
        # outranks the hard stop).
        if ahead:
            return answer(f"push the fixes ({ahead} local commit(s) not on the upstream)", cmd["push"])
        if cap_reached:
            return answer(
                "cap reached: record the run, or raise the cap with --max-rereview-requests",
                f"{cmd['record']} --outcome capped",
            )
        # The Stop-hook backstop also stamps a summary. With nothing pushed
        # since the fetch (HEAD is still the head the fetch saw) there is no
        # fix to ask a review of; the findings are still to be fixed.
        fetched_head = run.get("last_head_oid")
        if fetched_head and head and ahead == 0 and head == fetched_head:
            return answer(
                "nothing pushed since the fetch: fix the findings, verify, then emit the cycle summary",
                cmd["summary"],
            )
        return answer("request the re-review for the pushed head", cmd["request"])
    if phase in ("requested", "waiting"):
        # A push after the request, or a head the request did not cover,
        # makes the request (and any wait on it) stale, whichever of the two
        # phases the run is in.
        if ahead:
            return answer(
                f"push ({ahead} local commit(s) after the request), then request again",
                cmd["push"],
            )
        if requested_head and head and ahead == 0 and head != requested_head:
            return answer(
                f"the head moved after the request ({requested_head[:7]} -> {head[:7]}): request again",
                cmd["request"],
            )
    if phase == "requested":
        if requested_at:
            return answer(
                "wait for the review that answers the request",
                f"{cmd['wait']} --after {requested_at} --timeout {WAIT_TIMEOUT_SECONDS}",
            )
        return answer(
            "wait for the review; the request time is not recorded, take it from the PR",
            f"{cmd['wait']} --after <request time> --timeout {WAIT_TIMEOUT_SECONDS}",
        )
    if phase == "waiting":
        wait = run.get("wait") if isinstance(run.get("wait"), dict) else {}
        snapshot = wait.get("last_snapshot") if isinstance(wait.get("last_snapshot"), dict) else {}
        after = wait.get("after") or run.get("requested_at")
        anchor = f" --after {after}" if after else ""
        status = snapshot.get("status")
        if status == "refused":
            if snapshot.get("kind") == "quota_exhausted":
                return answer(
                    "the reviewer is out of quota: ask the user to stop, or to add credits and re-run the wait",
                    f"{cmd['record']} --outcome human --outcome-reason 'reviewer quota exhausted' --gemini-unconfirmed",
                )
            return answer(
                "the reviewer refused the review: record the run as human",
                f"{cmd['record']} --outcome human --outcome-reason 'reviewer refused the review' --gemini-unconfirmed",
            )
        if status == "timed_out":
            return answer(
                "the wait timed out: re-run it, or record the run with --gemini-unconfirmed",
                f"{cmd['wait']}{anchor} --timeout {WAIT_TIMEOUT_SECONDS}",
            )
        if status in ("waiting", "settling"):
            chunk = loop_state.safe_int(snapshot.get("next_wait_seconds")) or 60
            # --timeout is the total budget across chunks (the default is
            # 900): a continuation without it would cut the documented 1800
            # in half and could time out at once.
            return answer(
                f"continue the wait ({status})",
                f"{cmd['wait']}{anchor} --wait-chunk-seconds {chunk} --timeout {WAIT_TIMEOUT_SECONDS}",
            )
        return answer(
            "the wait did not finish: re-run it",
            f"{cmd['wait']}{anchor} --timeout {WAIT_TIMEOUT_SECONDS}",
        )
    return answer(f"unknown phase {phase!r}: fetch to re-establish the state", cmd["fetch"])


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _age(iso: Any, now: _dt.datetime) -> str:
    try:
        then = _dt.datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return ""
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours >= 48:
        return f"{hours // 24}d ago"
    return f"{hours}h {minutes}m ago" if hours else f"{minutes}m ago"


def build_status(
    repo: str,
    pr: int,
    entry: dict[str, Any],
    *,
    git: dict[str, Any],
    git_repo: str | None,
    profile: str | None,
    sentinel: str,
    reservation_open: bool,
    provenance_incomplete: bool,
    cap_limit: int | None = None,
    cwd: str | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Everything the report prints, as one dict (the ``--format json`` body).

    ``cap_limit`` is the cap preferences.json holds now. The run block stores
    the cap the last fetch ran under; a preference raised above it since
    (resume case 1) is applied here, so the status stops saying "capped" the
    moment the user raised it, before any fetch. A preference at or below the
    stored cap is not a change: the fetch may have run with a
    ``--max-rereview-requests`` override, which wins over the preference.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    run = entry.get("run") if isinstance(entry.get("run"), dict) else {}
    run = dict(run)
    cap_used = run.get("cap_used")
    stored_limit = run.get("cap_limit")
    cap_raised = False
    # The stored limit is the cap the fetch ran under, which may be a
    # --max-rereview-requests override above the preference. Only a higher
    # preference is evidence of a change; a lower or equal one is the default
    # the override already beat, and must not turn the run capped.
    if (
        isinstance(cap_limit, int) and isinstance(cap_used, int)
        and isinstance(stored_limit, int) and cap_limit > stored_limit
    ):
        cap_raised = bool(run.get("cap_reached"))
        run["cap_limit"] = cap_limit
        run["cap_reached"] = cap_used >= cap_limit
    reviewer = entry.get("reviewer") if isinstance(entry.get("reviewer"), dict) else None
    last_run = entry.get("last_run") if isinstance(entry.get("last_run"), dict) else None
    phase = run.get("phase") if isinstance(run.get("phase"), str) else None
    active = loop_state.safe_int(run.get("update_seq")) > 0

    # Git facts only describe this PR when the checkout points at its repo.
    git_applies = bool(git_repo) and git_repo.casefold() == repo.casefold()
    git_facts = dict(git) if git_applies else {"head": None, "upstream": None, "ahead": None, "behind": None}

    warnings: list[str] = []
    if not git_applies:
        warnings.append(
            f"git facts skipped: the checkout points at {git_repo or 'no GitHub remote'}, not {repo}"
        )
    elif git.get("head") and not git.get("upstream"):
        warnings.append("no upstream branch: unpushed work cannot be told")
    if provenance_incomplete:
        warnings.append(
            "re-review provenance is incomplete: the cap count is unknown and the request script refuses to post"
        )
    elif run.get("cap_count_complete") is False:
        warnings.append("the re-review count is a bound, not an exact count")
    if active and sentinel == "absent":
        warnings.append("loop-active marker missing: the edit gate, push gate and Stop-hook backstop are off")
    elif sentinel == "stale":
        warnings.append("loop-active marker older than 24h: the next hook fire reaps it and the gates stand down")
    if active and loop_state.summary_is_stale(run) and phase not in (None, "fetched"):
        warnings.append("the run advanced since the last cycle summary; the push gate will block git push")
    if cap_raised:
        warnings.append(
            f"the cap was raised to {cap_limit} since the last fetch; the loop may continue"
        )

    facts = {
        "commands": commands(repo, pr, cwd),
        "reviewer": reviewer,
        "phase": phase,
        "run": run,
        "git": git_facts,
        "profile": profile,
        "reservation_open": reservation_open,
        "last_run": last_run,
    }
    nxt = decide_next(facts)
    wait = run.get("wait") if isinstance(run.get("wait"), dict) else None
    return {
        "repo": repo,
        "pr": pr,
        "url": facts["commands"]["url"],
        "reviewer": reviewer,
        "phase": phase,
        "phaseAt": run.get("phase_at"),
        "phaseAge": _age(run.get("phase_at"), now),
        "active": active,
        "sessionCycle": loop_state.safe_int(run.get("session_cycle")) or None,
        "cap": {
            "used": run.get("cap_used"),
            "limit": run.get("cap_limit"),
            "reached": run.get("cap_reached"),
            "countComplete": run.get("cap_count_complete"),
        },
        "requestedAt": run.get("requested_at"),
        "requestedHead": run.get("requested_head"),
        "wait": wait.get("last_snapshot") if wait else None,
        "waitAfter": wait.get("after") if wait else None,
        "git": git_facts,
        "profile": profile,
        "sentinel": sentinel,
        "reservationOpen": reservation_open,
        "provenanceIncomplete": provenance_incomplete,
        "lastRun": last_run,
        "warnings": warnings,
        "next": nxt,
    }


def format_status(status: dict[str, Any]) -> str:
    lines = [f"[loop] status: {status['repo']}#{status['pr']}"]
    reviewer = status.get("reviewer")
    if reviewer:
        name = reviewer.get("display_name") or reviewer.get("login")
        mode = "reviews on its own" if reviewer.get("auto_reviews") else "reviews on request"
        lines.append(f"Reviewer: {name} ({reviewer.get('login')}); {mode}")
    else:
        lines.append("Reviewer: none persisted")
    phase = status.get("phase")
    if phase:
        when = status.get("phaseAt") or "?"
        age = f" ({status['phaseAge']})" if status.get("phaseAge") else ""
        parts = [f"Phase: {phase} since {when}{age}"]
        if status.get("sessionCycle"):
            parts.append(f"session cycle {status['sessionCycle']}")
        cap = status.get("cap") or {}
        if cap.get("limit") is not None:
            bound = "" if cap.get("countComplete") in (None, True) else " (a bound)"
            parts.append(f"{cap.get('used', '?')}/{cap['limit']} re-review requests used{bound}")
        lines.append("; ".join(parts))
    else:
        last = status.get("lastRun")
        if last:
            lines.append(
                f"Phase: none; last run ended {last.get('outcome', '?')} at {last.get('recorded_at', '?')}"
            )
        else:
            lines.append("Phase: none; no loop recorded for this PR")
    if status.get("requestedAt"):
        head = status.get("requestedHead")
        lines.append(
            f"Request: {status['requestedAt']}" + (f" for head {head[:7]}" if head else "")
        )
    wait = status.get("wait")
    if wait:
        detail = wait.get("status", "?")
        if wait.get("kind"):
            detail += f" ({wait['kind']})"
        lines.append(f"Wait: {detail}")
    git = status.get("git") or {}
    if git.get("head"):
        local = [f"HEAD {git['head'][:7]}"]
        if git.get("upstream"):
            ahead, behind = git.get("ahead") or 0, git.get("behind") or 0
            local.append(f"{ahead} ahead, {behind} behind the upstream")
        lines.append("Local: " + "; ".join(local))
    profile = status.get("profile")
    lines.append(f"Profile: {profile if profile else 'none saved'}")
    for warning in status.get("warnings") or []:
        lines.append(f"Warning: {warning}")
    nxt = status.get("next") or {}
    lines.append(f"Next: {nxt.get('action', '?')}")
    if nxt.get("command"):
        lines.append(f"  {nxt['command']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Where the review loop is, from local state only, and the one next command."
    )
    parser.add_argument("--pr", default=None, help="PR URL, or a number in the current repo.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--cwd", default=None, help="Checkout to read git facts from (default: here).")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors in text output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    git_repo = repo_from_origin(args.cwd)
    target = parse_pr_arg(args.pr, git_repo)
    if args.pr and target is None:
        print("error: --pr must be a PR URL, or a number inside a GitHub checkout.", file=sys.stderr)
        return 2
    state = loop_state.load_sticky_state()
    prefs = read_preferences()
    if target is not None:
        repo, number = target
        selected = select_entry(state, repo, number)
    elif git_repo:
        repo = git_repo
        selected = select_entry(state, repo)
    else:
        print("error: not inside a GitHub checkout; pass --pr <url>.", file=sys.stderr)
        return 2
    if selected is None:
        print(
            color_loop(
                f"[loop] status: no loop recorded for {repo}\n"
                "Next: fetch the reviewer's threads on the PR you want to work on\n"
                f"  {python_script_command('fetch_gemini_threads.py')} --pr <url>",
                enabled=colors_enabled(no_color=args.no_color) and args.format != "json",
            )
            if args.format != "json"
            else json.dumps({"repo": repo, "pr": None, "phase": None, "next": {
                "action": "fetch the reviewer's threads on the PR you want to work on",
                "command": f"{python_script_command('fetch_gemini_threads.py')} --pr <url>",
            }}, indent=2, sort_keys=True)
        )
        return 0
    _key, number, entry = selected
    status = build_status(
        repo,
        number,
        entry,
        git=local_git(args.cwd),
        git_repo=git_repo,
        profile=profile_source(repo, prefs),
        sentinel=sentinel_state(),
        reservation_open=loop_state.unconfirmed_request_path(repo, number).exists(),
        provenance_incomplete=loop_state.provenance_incomplete(repo, number),
        cap_limit=preferred_cap(prefs),
        cwd=args.cwd,
    )
    if args.format == "json":
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(color_loop(format_status(status), enabled=colors_enabled(no_color=args.no_color)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
