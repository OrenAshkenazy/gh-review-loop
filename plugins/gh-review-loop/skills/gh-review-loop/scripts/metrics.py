"""Local, identity-free run metrics for the AI reviewer loop.

Pure module: no network, no imports from fetch_gemini_threads. Owns the
runs.jsonl schema, append/load, run-summary formatting, and aggregation.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import Counter
from pathlib import Path
from typing import Any

import loop_state  # noqa: E402 — sibling module, stdlib-only

RECORD_SCHEMA_VERSION = 1
DEFAULT_WINDOW = 10

JUDGE_VERDICTS = (
    "valid_actionable",
    "false_positive",
    "needs_human",
    "explanation_only",
    "duplicate",
    "already_addressed",
)
JUDGE_ACTIONS = ("fix", "reply", "ignore", "escalate")
VALID_OUTCOMES = (
    "clean",
    "capped",
    "human",
    "regression",
    "no_progress",
    "verification_failed",
    "fixed_pending_confirmation",
)
VALID_VERIFICATION = ("passed", "failed", "skipped")
# Terminal outcomes counted as a "failed run" in the elapsed-by-outcome split.
FAILED_OUTCOMES = ("verification_failed", "regression")

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def runs_log_path() -> Path:
    """Path to runs.jsonl, beside state.json; override via GGRL_STATE_DIR."""
    return loop_state.state_dir() / "runs.jsonl"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime(_TS_FMT)


def top_dir(path: str) -> str:
    """First path segment of a repo-relative path, or '(unknown)' if empty."""
    if not path:
        return "(unknown)"
    parts = Path(path).parts
    return parts[0] if parts else "(unknown)"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, _sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def append_record(record: dict[str, Any], path: Path | None = None) -> None:
    """Append one record as a JSON line. Caller wraps for failure isolation."""
    path = path or runs_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def load_records(path: Path | None = None) -> tuple[list[dict[str, Any]], int]:
    """Return (records, skipped). Skips blank lines silently; counts corrupt
    lines and records whose schema_version this code does not understand."""
    path = path or runs_log_path()
    try:
        if not path.exists():
            return [], 0
        content = path.read_text(encoding="utf-8")
    except OSError:
        return [], 0
    records: list[dict[str, Any]] = []
    skipped = 0
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(rec, dict) or rec.get("schema_version") != RECORD_SCHEMA_VERSION:
            skipped += 1
            continue
        records.append(rec)
    return records, skipped


def pattern_history_for_pr(
    repo: str, pr_number: int, path: Path | None = None
) -> dict[str, set[str]]:
    """Union of pattern signatures (seen + swept) from prior recorded runs of a PR.

    The live per-run accumulator is cleared by --record-run, so a resumed loop
    would otherwise see an empty prior set and report recurrence as 0. Reading
    the append-only runs.jsonl lets convergence detection survive across resumes:
    a pattern recorded in an earlier run of this same repo+PR still counts as
    "seen before". Returns ``{"seen": set, "swept": set}``; empty on any error.

    Repository keys are compared case-insensitively for backward compatibility:
    older versions stored the caller's spelling, while new writes use GitHub's
    canonical ``owner/repo`` casing.
    """
    seen: set[str] = set()
    swept: set[str] = set()
    try:
        records, _ = load_records(path)
    except (OSError, ValueError):
        return {"seen": seen, "swept": swept}
    repo_key = repo.casefold()
    for rec in records:
        recorded_repo = rec.get("repo")
        if not isinstance(recorded_repo, str) or recorded_repo.casefold() != repo_key:
            continue
        try:
            if int(rec.get("pr") or 0) != pr_number:
                continue
        except (TypeError, ValueError):
            continue
        patterns = rec.get("patterns")
        if not isinstance(patterns, dict):
            continue
        for key, bucket in (("signatures", seen), ("swept", swept)):
            values = patterns.get(key)
            if isinstance(values, list):
                bucket.update(v for v in values if isinstance(v, str))
    return {"seen": seen, "swept": swept}


def build_judge_block(judge_ran: bool, judge_results: dict[str, Any]) -> dict[str, Any]:
    if not judge_ran:
        return {"enabled": False}
    verdicts = {v: 0 for v in JUDGE_VERDICTS}
    actions = {a: 0 for a in JUDGE_ACTIONS}
    for result in judge_results.values():
        verdict = result.get("verdict")
        if verdict in verdicts:
            verdicts[verdict] += 1
        action = result.get("recommended_action")
        if action in actions:
            actions[action] += 1
    return {"enabled": True, "verdicts": verdicts, "recommended_actions": actions}


# Per-finding states produced by classify_finding_state. These are NOT the
# terminal run outcomes (VALID_OUTCOMES); they describe one finding's status so
# the terminal breakdown can label it accurately instead of lumping every
# open thread into "remaining valid actionable".
FINDING_STATES = (
    "new_valid_actionable",
    "carried_over",
    "stale_already_fixed",
    "fixed_locally",
    "fixed_pushed_awaiting_review",
    "fixed_pending_confirmation",
    "confirmed_outdated",
    "needs_human",
    "capped_unfixed",
)


def classify_finding_state(signals: dict[str, Any]) -> str:
    """Classify one finding from explicit, multi-signal booleans.

    ``signals`` carries six independent flags (any absent key defaults to
    False — no hidden magic):

    - ``judge_needs_human``  : the judge flagged a human decision.
    - ``carried_over``       : prior-cycle fingerprint match (still present).
    - ``fixed_locally``      : agent marker says it fixed this finding.
    - ``file_changed``       : the finding's path appears in a fixing-commit diff.
    - ``gemini_confirmed``   : the final review cleared / outdated this finding.
    - ``cap_reached``        : the re-review cap was reached.

    No single signal decides ``fixed_pending_confirmation``; it requires the
    *combination* fixed_locally + file_changed at cap with no reviewer
    confirmation.

    Precedence (first matching rule wins, highest-confidence first):

    1. gemini_confirmed                              -> confirmed_outdated
    2. judge_needs_human                             -> needs_human
    3. fixed_locally & file_changed & cap_reached    -> fixed_pending_confirmation
    4. fixed_locally & file_changed                  -> fixed_pushed_awaiting_review
    5. fixed_locally                                 -> fixed_locally
    6. carried_over & file_changed                   -> stale_already_fixed
    7. carried_over & cap_reached                    -> capped_unfixed
    8. carried_over                                  -> carried_over
    9. cap_reached                                   -> capped_unfixed
    10. (default)                                    -> new_valid_actionable
    """
    judge_needs_human = bool(signals.get("judge_needs_human"))
    carried_over = bool(signals.get("carried_over"))
    fixed_locally = bool(signals.get("fixed_locally"))
    file_changed = bool(signals.get("file_changed"))
    gemini_confirmed = bool(signals.get("gemini_confirmed"))
    cap_reached = bool(signals.get("cap_reached"))

    if gemini_confirmed:
        return "confirmed_outdated"
    if judge_needs_human:
        return "needs_human"
    if fixed_locally and file_changed and cap_reached:
        return "fixed_pending_confirmation"
    if fixed_locally and file_changed:
        return "fixed_pushed_awaiting_review"
    if fixed_locally:
        return "fixed_locally"
    if carried_over and file_changed:
        return "stale_already_fixed"
    if carried_over and cap_reached:
        return "capped_unfixed"
    if carried_over:
        return "carried_over"
    if cap_reached:
        return "capped_unfixed"
    return "new_valid_actionable"


def _duration_seconds(started_at: str, ts: str) -> int:
    try:
        start = _dt.datetime.strptime(started_at, _TS_FMT)
        end = _dt.datetime.strptime(ts, _TS_FMT)
    except (TypeError, ValueError):
        return 0
    return max(0, int((end - start).total_seconds()))


def format_auto_snapshot(record: dict[str, Any]) -> str:
    """Lean receipt for the Stop-hook backstop (agent didn't post a summary).

    The hook can't know agent-only facts — fixed count, verification result,
    terminal outcome — so this shows ONLY GitHub-observable state and is
    explicitly labelled automatic. It never prints a guessed Fixed/
    Verification/Outcome, which is what made the full receipt misleading when
    fired without agent inputs.
    """
    # One line on purpose: Claude Code collapses multi-line hook output behind
    # "ctrl+o to expand", so a multi-line backstop summary wouldn't actually be
    # visible to someone watching the chat.
    snapshot = (
        "[loop] Summary (auto, agent didn't post one): "
        f"{record['findings_fetched']} seen, "
        f"{record.get('observed_fixed_count', 0)} resolved, "
        f"{record['remaining_actionable']} open · "
        f"re-review requests {record['cycles_used']}/{record['cycle_cap']}"
    )
    patterns = record.get("patterns")
    if (
        isinstance(patterns, dict)
        and patterns.get("distinct_patterns", 0) >= 3
        and patterns.get("max_cluster_size") == 1
    ):
        snapshot += (
            " · ⚠ likely prose-hash fallbacks; manual sweep required before fixing"
        )
    return snapshot


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def format_terminal_breakdown(
    *,
    confirmed_fixed_outdated: int = 0,
    fixed_pending_confirmation: int = 0,
    remaining_valid_actionable: int = 0,
    needs_human: int = 0,
) -> str:
    """Render terminal finding buckets with unambiguous labels."""
    return "\n".join(
        [
            f"Confirmed fixed/outdated: {_count(confirmed_fixed_outdated)}",
            (
                "Fixed but awaiting review confirmation: "
                f"{_count(fixed_pending_confirmation)}"
            ),
            f"Remaining valid actionable: {_count(remaining_valid_actionable)}",
            f"Human decision required: {_count(needs_human)}",
        ]
    )


def _profile_checks(profile: Any) -> list[dict[str, Any]]:
    if not isinstance(profile, dict):
        return []
    checks = profile.get("checks")
    if not isinstance(checks, list):
        return []
    profile_cwd = profile.get("working_directory")
    if not isinstance(profile_cwd, str) or not profile_cwd.strip():
        profile_cwd = "."

    normalized: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        command = check.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        name = check.get("name")
        if not isinstance(name, str) or not name.strip():
            name = "check"
        cwd = check.get("working_directory")
        if not isinstance(cwd, str) or not cwd.strip():
            cwd = profile_cwd
        normalized.append(
            {
                "name": name.strip(),
                "command": command.strip(),
                "cwd": cwd.strip(),
                "required": bool(check.get("required", True)),
            }
        )
    return normalized


def format_profile_intro_block(profile: Any, repo: str) -> str:
    """Deterministic intro for the saved repo-aware verification profile."""
    if profile is None:
        return "[loop] Verification profile: none saved, using ad hoc verification."
    if not isinstance(profile, dict):
        return "[loop] Verification profile: missing or malformed, using ad hoc verification."
    if profile.get("source") == "skipped":
        return "[loop] Verification profile: skipped for this repo, using ad hoc verification."

    checks = _profile_checks(profile)
    if not checks:
        return "[loop] Verification profile: missing or malformed, using ad hoc verification."

    lines = [
        "[loop] Repo-aware verification profile",
        f"Profile: {repo}",
        "Checks:",
    ]
    for index, check in enumerate(checks, 1):
        scope = "required" if check["required"] else "optional"
        lines.append(
            f"{index}. {check['name']} — {check['command']} "
            f"(cwd: {check['cwd']}, {scope})"
        )
    return "\n".join(lines)


def format_planned_verification_block(profile: Any) -> str:
    """Render the required repo-aware checks the loop plans to run."""
    checks = [check for check in _profile_checks(profile) if check["required"]]
    if not checks:
        return (
            "[loop] Verification suite\n"
            "No required repo-aware checks saved; use ad hoc verification."
        )

    plural = "check" if len(checks) == 1 else "checks"
    lines = [
        "[loop] Verification suite",
        f"Running {len(checks)} required repo-aware {plural}:",
    ]
    for index, check in enumerate(checks, 1):
        lines.append(f"{index}. {check['name']} — {check['command']} (cwd: {check['cwd']})")
    return "\n".join(lines)


def format_pending_profile_block(repo: str, *, script: str) -> str:
    """First-run notice standing in for the fetch-carried profile blocks (#107).

    On a first run the profile is decided *after* this fetch, so an intro or
    planned suite rendered now would describe "none saved / ad hoc" while the
    agent is about to save a real profile and run it. Rather than ship a block
    that is stale by the time it is relayed, say so and name the regeneration
    commands.

    ``script`` is the caller's own resolved path. The scripts directory is not
    on ``PATH`` under a plugin install, so a bare filename here would print a
    command that fails with ``command not found`` — quote the path the caller
    was actually invoked from.
    """
    invocation = f'python3 "{script}"'
    return (
        "[loop] Verification profile: none saved for "
        f"{repo} yet — this is a first run.\n"
        "Decide the profile now (detect_profile.py → menu → save_profile), "
        "before the first fix.\n"
        "These blocks render from the saved profile, so regenerate them after "
        "saving instead of relaying this one:\n"
        f"  {invocation} --repo {repo} --profile-intro\n"
        f"  {invocation} --repo {repo} --planned-verification"
    )


def format_judge_skip(reason: str) -> str:
    reason = reason.strip() if isinstance(reason, str) else ""
    return f"[loop] judge eval skipped: {reason or 'unknown reason'}"


def format_compact_receipt_line(
    record: dict[str, Any],
    *,
    terminal: bool,
    receipt_url: str,
    findings_new: int | None = None,
    findings_carried: int | None = None,
) -> str:
    """One-line chat pointer for a receipt that lives in the sticky PR comment.

    The full receipt (verification suite, findings list, severity breakdown)
    is delivered once, on the PR; chat carries only counts + verification +
    the link (#86). Everything here must fit one line — this is the whole
    happy-path chat footprint of a cycle.

    Two different denominators appear here and must not be mixed.
    ``findings_fetched`` is **cumulative** for the run (baseline ∪ currently
    actionable), so it is labelled "seen this run". ``findings_new`` /
    ``findings_carried`` describe only the **currently open** set, so they
    ride on ``open`` where they share its denominator. Attaching the split to
    the cumulative count produced nonsense like "findings 4 (1 new, 0 carried
    over)" once earlier findings were resolved, and "findings 4 (0 new, 0
    carried over)" on a clean terminal run.
    """
    header = "[loop] Summary" if terminal else "[loop] Cycle receipt"
    # Script-owned session-cycle ordinal (#104): narration copies this value
    # instead of model-tracking a counter that drifts across compaction.
    session_cycle = _count(record.get("session_cycle"))
    if session_cycle:
        header += f" (session cycle {session_cycle})"
    parts = [
        f"findings {_count(record.get('findings_fetched'))} seen this run",
        f"{'fixed' if terminal else 'fixed locally'} {_count(record.get('fixed_count'))}",
    ]
    remaining = _count(record.get("remaining_actionable"))
    if remaining:
        open_part = f"open {remaining}"
        # Render the split only when it accounts for exactly the open set —
        # a caller that passes counts from any other population would silently
        # reintroduce the mismatch this guard exists to prevent.
        if (
            findings_new is not None
            and findings_carried is not None
            and _count(findings_new) + _count(findings_carried) == remaining
        ):
            open_part += (
                f" ({_count(findings_new)} new, "
                f"{_count(findings_carried)} carried over)"
            )
        parts.append(open_part)
    parts.append(
        f"re-review requests {_count(record.get('cycles_used'))}/{_count(record.get('cycle_cap'))}"
    )
    parts.append(f"verification {record.get('verification', 'skipped')}")
    if terminal:
        parts.append(f"outcome {record.get('outcome', '')}")
    return f"{header}: " + " · ".join(parts) + f" — full receipt: {receipt_url}"


def format_wait_heartbeat(
    status: str,
    *,
    author: str,
    elapsed_seconds: Any = 0,
    checks: Any = 0,
    next_wait_seconds: Any = 0,
    quiet_period_remaining_seconds: Any = None,
) -> str:
    """Deterministic human heartbeat for one chunked-wait status.

    Returns "" for statuses that have no heartbeat (``ready`` proceeds into a
    fetch; unknown statuses render nothing rather than guessing).
    """
    elapsed = _count(elapsed_seconds)
    checks_n = _count(checks)
    if status == "waiting":
        plural = "check" if checks_n == 1 else "checks"
        return (
            f"[loop] waiting for {author} — {elapsed}s elapsed, "
            f"{checks_n} {plural} done, next check in {_count(next_wait_seconds)}s"
        )
    if status == "settling":
        return (
            "[loop] Reviewer responded — waiting for review threads to settle, "
            f"{_count(quiet_period_remaining_seconds)}s quiet period remaining"
        )
    if status == "timed_out":
        return (
            f"[loop] wait timed out after {format_duration(elapsed)} — "
            "The reviewer did not confirm; record with --gemini-unconfirmed"
        )
    return ""


def format_next_options(outcome: str, cap_reached: bool, needs_human: int) -> str:
    """Deterministic next-step menu for non-clean terminal outcomes."""
    if outcome == "clean":
        return ""
    if outcome == "fixed_pending_confirmation":
        options = [
            "Bump cap and run one confirmation cycle",
            "Manually inspect and resolve the remaining GitHub thread",
            "Leave it for follow-up",
        ]
    elif outcome == "capped":
        options = [
            'Bump cap and continue: "run the review loop with cap 6"',
            "Manually inspect and resolve the remaining GitHub thread",
            "Leave it for a follow-up PR",
        ]
    elif outcome == "human" or _count(needs_human):
        options = [
            "Ask Claude to implement option A",
            "Ask Claude to implement option B",
            "Leave the behavior unchanged and reply to the thread with rationale",
        ]
    elif cap_reached:
        options = [
            'Bump cap and continue: "run the review loop with cap 6"',
            "Manually inspect and resolve the remaining GitHub thread",
            "Leave it for a follow-up PR",
        ]
    else:
        options = [
            "Inspect the remaining GitHub thread",
            "Adjust the fix and rerun verification",
            "Leave it for a follow-up PR",
        ]
    return "\n".join(["Next options:"] + [f"{i}. {option}" for i, option in enumerate(options, 1)])


def format_semantic_risk_block(changes: list[str] | None) -> str:
    """Render an explicit manual/heuristic semantic-risk note."""
    if not isinstance(changes, list):
        return ""
    cleaned = [change.strip() for change in changes if isinstance(change, str) and change.strip()]
    if not cleaned:
        return ""
    lines = [
        "[loop] Semantic risk note (manual / heuristic)",
        "This cycle changed public behavior:",
    ]
    lines.extend(f"- {change}" for change in cleaned)
    lines.extend(
        [
            "",
            "Verification passed, but this may require human review.",
        ]
    )
    return "\n".join(lines)


def format_run_summary(record: dict[str, Any], *, terminal: bool = True) -> str:
    """Human-readable receipt for one loop run.

    A receipt, not a dashboard: a small fixed core, plus optional lines that
    appear only when they carry signal (observed != fixed, judge verdicts > 0,
    addressed-by-reply > 0, a named failed check).

    ``terminal=True`` (default) is the ``--record-run`` path; the header reads
    ``[loop] Summary``. ``terminal=False`` is the mid-loop ``--cycle-summary``
    path; the header reads ``[loop] Cycle receipt`` so users can distinguish
    per-cycle snapshots from the final terminal receipt.
    """
    header = "[loop] Summary" if terminal else "[loop] Cycle receipt"
    session_cycle = _count(record.get("session_cycle"))
    if session_cycle:
        header += f" (session cycle {session_cycle})"
    fixed_label = "Fixed" if terminal else "Fixed locally"
    lines = [
        header,
        f"Findings fetched: {record['findings_fetched']}",
        f"{fixed_label}: {record['fixed_count']}",
    ]
    observed = record.get("observed_fixed_count")
    if observed is not None and observed != record["fixed_count"]:
        lines.append(f"Observed fixed: {observed}")
    judge = record.get("judge") or {}
    if judge.get("enabled"):
        verdicts = judge.get("verdicts", {})
        ignored = (
            verdicts.get("false_positive", 0)
            + verdicts.get("duplicate", 0)
            + verdicts.get("already_addressed", 0)
            + verdicts.get("explanation_only", 0)
        )
        if ignored:
            lines.append(f"Ignored by judge: {ignored}")
    valid_remaining = record.get("valid_actionable_remaining", record["remaining_actionable"])
    needs_human = record.get("needs_human", 0)
    terminal_breakdown = record.get("terminal_breakdown")
    if terminal and isinstance(terminal_breakdown, dict) and terminal_breakdown:
        lines.extend(
            format_terminal_breakdown(
                confirmed_fixed_outdated=terminal_breakdown.get("confirmed_fixed_outdated", 0),
                fixed_pending_confirmation=terminal_breakdown.get("fixed_pending_confirmation", 0),
                remaining_valid_actionable=terminal_breakdown.get("remaining_valid_actionable", 0),
                needs_human=terminal_breakdown.get("needs_human", 0),
            ).splitlines()
        )
    elif not terminal and record.get("fixed_count") and valid_remaining:
        lines.append(f"Awaiting push/re-review confirmation: {valid_remaining}")
        if needs_human:
            lines.append(f"Human decision required: {needs_human}")
    else:
        if valid_remaining:
            lines.append(f"Remaining valid actionable: {valid_remaining}")
        if needs_human:
            lines.append(f"Human decision required: {needs_human}")
    if record.get("addressed_by_reply"):
        lines.append(f"Addressed by reply: {record['addressed_by_reply']}")
    lines.append(f"Re-review requests used: {record['cycles_used']}/{record['cycle_cap']} (cap)")
    lines.append(f"Verification: {record['verification']}")
    if record["verification"] == "failed":
        details = record.get("verification_details")
        failed_check = details.get("failed_check") if isinstance(details, dict) else None
        if failed_check:
            lines.append(f"Failed check: {failed_check}")
    lines.append(f"Outcome: {record['outcome']}")
    label = "Time to clean PR" if record["outcome"] == "clean" else "Time spent"
    lines.append(f"{label}: {format_duration(record['duration_seconds'])}")
    if terminal:
        options = format_next_options(
            record.get("outcome", ""),
            record.get("cycles_used", 0) >= record.get("cycle_cap", 0),
            record.get("needs_human", 0),
        )
        if options:
            lines.extend(options.splitlines())
    return "\n".join(lines)


def format_suite_block(verification_details: dict[str, Any] | None) -> str:
    """One line per detected verification check, or '' when none.

    Surfaces the repo-aware test toolset (e.g. ``uv run pytest``) that
    ``run_profile.py`` detected, so it is visible in the chat receipt instead
    of buried in the collapsed profile-runner JSON. ``verification_details`` is
    the parsed ``ProfileRunResult.to_details()`` dict.
    """
    if not isinstance(verification_details, dict):
        return ""
    checks = verification_details.get("checks")
    if not isinstance(checks, list) or not checks:
        return ""
    lines = ["Verification suite:"]
    for check in checks:
        if not isinstance(check, dict):
            continue
        command = check.get("command", "?")
        name = check.get("name", "?")
        scope = "required" if check.get("required") else "optional"
        status = check.get("status", "?")
        lines.append(f"  - {command}  ({name}, {scope}) → {status}")
    return "\n".join(lines) if len(lines) > 1 else ""


def format_findings_block(findings: list[dict[str, Any]]) -> str:
    """Deterministic list of the current actionable findings, or '' when empty.

    Each finding dict carries ``path``, ``line``, ``severity``, optional ``url``
    (the GitHub comment permalink), and ``carried`` (True when the same finding
    was already seen in a prior cycle). The header counts new vs carried-over so
    a re-posted finding is never mistaken for a fresh one — e.g. a cycle that
    fetches 4 threads where 1 repeats a prior fix reads ``3 new, 1 carried
    over``.
    """
    valid_findings = [f for f in findings if isinstance(f, dict)]
    if not valid_findings:
        return ""
    new_count = sum(1 for f in valid_findings if not f.get("carried"))
    carried_count = len(valid_findings) - new_count
    lines = [f"Findings ({len(valid_findings)}): {new_count} new, {carried_count} carried over"]
    for idx, finding in enumerate(valid_findings, 1):
        loc = finding.get("path") or "?"
        line = finding.get("line")
        if line is not None:
            loc = f"{loc}:{line}"
        severity = finding.get("severity") or "unknown"
        tag = " · carried over from a prior cycle" if finding.get("carried") else ""
        lines.append(f"  {idx}. {loc} [{severity}]{tag}")
        url = finding.get("url")
        if url:
            lines.append(f"     {url}")
    return "\n".join(lines)


def format_patterns_block(clusters: list[Any]) -> str:
    """Clustered view of the current findings, or '' when none.

    ``clusters`` is a list of cluster_findings.Cluster. Renders above the
    per-finding Findings block: one stanza per pattern with severity, label,
    site count, signature token (for --swept-pattern), and up to 4 sites.
    """
    valid = [c for c in clusters if c is not None]
    if not valid:
        return ""
    lines = [f"Patterns ({len(valid)}):"]
    for c in valid:
        plural = "site" if c.count == 1 else "sites"
        lines.append(f"  [{c.severity}] {c.label} — {c.count} {plural}   (sig: {c.signature})")
        shown = c.sites[:4]
        suffix = f", +{len(c.sites) - 4} more" if len(c.sites) > 4 else ""
        lines.append(f"           {', '.join(shown)}{suffix}")
    return "\n".join(lines)


def format_degenerate_clustering_advisory(clusters: list[Any]) -> str:
    """Warn when every finding became its own pattern, or return ``''``."""
    valid = [cluster for cluster in clusters if cluster is not None]
    if len(valid) < 3 or max(cluster.count for cluster in valid) != 1:
        return ""
    return (
        "Clustering: ⚠ every finding produced a singleton pattern. "
        "Signatures are likely prose-hash fallbacks; a manual sweep is required "
        "before fixing."
    )


def format_convergence_line(stats: dict[str, Any], *, swept_count: int) -> str:
    """One-line advisory convergence summary, or '' when no patterns this cycle."""
    distinct = stats.get("distinct_patterns", 0)
    if not distinct:
        return ""
    recurred = stats.get("recurred_after_sweep") or []
    swept_plural = "pattern" if swept_count == 1 else "patterns"
    if recurred:
        names = ", ".join(recurred)
        return (
            f"Convergence: ⚠ pattern(s) {names} RECURRED after sweep. "
            "Sweep missed a variant or the reviewer keeps re-flagging."
        )
    return (
        f"Convergence: {distinct} distinct patterns this cycle, "
        f"0 recurred. Swept {swept_count} {swept_plural}."
    )


def _mode(items: list[Any]) -> Any | None:
    if not items:
        return None
    return Counter(items).most_common(1)[0][0]


def _avg(values: list[float]) -> float | None:
    """Mean of values, or None when empty. Values are pre-filtered by caller."""
    return (sum(values) / len(values)) if values else None


def _is_number(x: Any) -> bool:
    """True for a real numeric duration: int/float but not bool.

    Records come from a local JSONL log that can be hand-edited or corrupted,
    so a duration could be a string, None, or bool. Validating the type keeps
    aggregation from crashing on summation. ``bool`` is excluded because
    True/False are not meaningful durations even though they are ``int``.
    """
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _elapsed_for(records: list[dict[str, Any]], outcomes: tuple[str, ...]) -> float | None:
    """Average duration_seconds over records whose outcome is in ``outcomes``.

    Durations are validated numeric (an explicit check that also keeps a valid
    0-second run, which a truthiness test would drop).
    """
    vals = [
        r["duration_seconds"]
        for r in records
        if r.get("outcome") in outcomes and _is_number(r.get("duration_seconds"))
    ]
    return _avg(vals)


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    if n == 0:
        return {"count": 0}
    # Every field below is type-validated: records come from a local,
    # hand-editable JSONL log, so a corrupt scalar/list must be skipped rather
    # than crash summation or iteration.
    cycles = [
        r["cycles_used"] if _is_number(r.get("cycles_used")) else 0 for r in records
    ]
    # Validate numeric (keeps a valid 0-second run, which a truthiness test
    # would drop; rejects corrupt string/bool durations that would crash sum()).
    durations = [
        r["duration_seconds"] for r in records if _is_number(r.get("duration_seconds"))
    ]
    judged = [
        r for r in records
        if isinstance(r.get("judge"), dict) and r["judge"].get("enabled")
    ]
    false_pos = 0
    for r in judged:
        verdicts = r["judge"].get("verdicts")
        if isinstance(verdicts, dict) and _is_number(verdicts.get("false_positive")):
            false_pos += verdicts["false_positive"]
    providers = [r.get("provider") for r in records if isinstance(r.get("provider"), str)]
    areas = [
        a
        for r in records
        if isinstance(r.get("finding_areas"), list)
        for a in r["finding_areas"]
        if isinstance(a, str)
    ]

    # Active-cycle metrics: only runs that recorded per-cycle timing. Legacy
    # records without a "cycles" list are excluded from active metrics (but
    # still contribute to the elapsed metrics above).
    # Require a non-empty list: a non-list truthy value (corrupt record) would
    # TypeError on iteration/len(); an empty list means "no recorded cycles".
    runs_with_cycles = [
        r for r in records if isinstance(r.get("cycles"), list) and r["cycles"]
    ]
    # Cycle entries can be corrupt too: keep only dicts, and only numeric
    # durations, so a malformed entry never crashes aggregation.
    all_cycles = [
        c for r in runs_with_cycles for c in r["cycles"] if isinstance(c, dict)
    ]
    cycle_durations = [
        c["duration_seconds"]
        for c in all_cycles
        if _is_number(c.get("duration_seconds"))
    ]
    run_active_totals = [
        sum(
            c["duration_seconds"]
            for c in r["cycles"]
            if isinstance(c, dict) and _is_number(c.get("duration_seconds"))
        )
        for r in runs_with_cycles
    ]
    cycle_counts = [
        sum(1 for c in r["cycles"] if isinstance(c, dict)) for r in runs_with_cycles
    ]

    return {
        "count": n,
        "avg_cycles": sum(cycles) / n,
        # Elapsed (wall-clock) metrics — user-visible latency.
        "avg_duration": _avg(durations),
        "avg_duration_clean": _elapsed_for(records, ("clean",)),
        "avg_duration_capped": _elapsed_for(records, ("capped",)),
        "avg_duration_failed": _elapsed_for(records, FAILED_OUTCOMES),
        # Active-cycle metrics — agent/loop processing efficiency.
        "avg_active_cycle_time": _avg(cycle_durations),
        "avg_active_time_per_run": _avg(run_active_totals),
        "avg_cycles_per_run": _avg(cycle_counts),
        "total_fetched": sum(r.get("findings_fetched", 0) for r in records),
        "total_fixed": sum(r.get("observed_fixed_count", 0) for r in records),
        "needs_human": sum(r.get("needs_human", 0) for r in records),
        "addressed_by_reply": sum(r.get("addressed_by_reply", 0) for r in records),
        "judged_count": len(judged),
        "false_positives_avoided": false_pos,
        "top_provider": _mode(providers),
        "top_area": _mode(areas),
    }


def format_stats(repo: str, stats: dict[str, Any], skipped: int = 0) -> str:
    if stats.get("count", 0) == 0:
        msg = (
            "No review loop runs recorded yet for this repo. "
            "Run the loop once and stats will appear here."
        )
        if skipped:
            msg += f"\n\n({skipped} unreadable record{'s' if skipped != 1 else ''} skipped)"
        return msg
    lines = [f"Review loop stats — {repo}", f"Last {stats['count']} runs", ""]
    lines.append(f"Average re-review requests used: {stats['avg_cycles']:.1f}")

    def _elapsed_line(label: str, key: str) -> None:
        val = stats.get(key)
        if val is not None:
            lines.append(f"{label}: {format_duration(round(val))}")

    # Elapsed (wall-clock) latency — what the user waited, including review
    # latency, polling, and idle gaps. Split by terminal outcome.
    _elapsed_line("Average elapsed time to terminal outcome", "avg_duration")
    _elapsed_line("Average elapsed time to clean PR", "avg_duration_clean")
    _elapsed_line("Average elapsed time to capped run", "avg_duration_capped")
    _elapsed_line("Average elapsed time to failed run", "avg_duration_failed")

    # Active-cycle time — agent/loop processing efficiency, excluding waits.
    # Absent on legacy records (pre-cycle-tracking); omitted then.
    if stats.get("avg_active_cycle_time") is not None:
        lines.append(
            f"Average active cycle time: "
            f"{format_duration(round(stats['avg_active_cycle_time']))}"
        )
    if stats.get("avg_active_time_per_run") is not None:
        lines.append(
            f"Average active time per run: "
            f"{format_duration(round(stats['avg_active_time_per_run']))}"
        )
    if stats.get("avg_cycles_per_run") is not None:
        lines.append(f"Average cycles per run: {stats['avg_cycles_per_run']:.1f}")

    lines.append(f"Findings fixed: {stats['total_fixed']} of {stats['total_fetched']}")
    lines.append(f"Human decisions needed: {stats['needs_human']}")
    if stats["addressed_by_reply"]:
        lines.append(f"Addressed by reply: {stats['addressed_by_reply']}")
    if stats["judged_count"] > 0:
        lines.append(
            f"False positives avoided: {stats['false_positives_avoided']}   "
            f"(across {stats['judged_count']} of {stats['count']} judged runs)"
        )
    if stats["top_provider"]:
        lines.append(f"Most common provider: {stats['top_provider']}")
    if stats["top_area"]:
        lines.append(f"Most repeated finding area: {stats['top_area']}")
    out = "\n".join(lines)
    if skipped:
        out += f"\n\n({skipped} unreadable record{'s' if skipped != 1 else ''} skipped)"
    return out


def build_record(
    *,
    repo: str,
    pr: int,
    provider: str,
    findings_fetched: int,
    fixed_count: int,
    observed_fixed_count: int,
    remaining_actionable: int,
    needs_human: int,
    addressed_by_reply: int,
    cycles_used: int,
    cycle_cap: int,
    verification: str,
    verification_details: dict[str, Any] | None,
    outcome: str,
    outcome_reason: str,
    session_cycle: int | None = None,
    started_at: str | None,
    finding_paths: list[str],
    judge: dict[str, Any] | None,
    cycles: list[dict[str, Any]] | None = None,
    terminal_breakdown: dict[str, Any] | None = None,
    patterns: dict[str, Any] | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    ts = ts or now_iso()
    if not started_at:
        started_at = ts
        duration = 0
    else:
        duration = _duration_seconds(started_at, ts)
    paths = list(finding_paths or [])
    valid_actionable_remaining = max(0, remaining_actionable - needs_human)
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "ts": ts,
        "repo": repo,
        "pr": pr,
        "provider": provider,
        "findings_fetched": findings_fetched,
        "fixed_count": fixed_count,
        "observed_fixed_count": observed_fixed_count,
        "remaining_actionable": remaining_actionable,
        "valid_actionable_remaining": valid_actionable_remaining,
        "needs_human": needs_human,
        "addressed_by_reply": addressed_by_reply,
        "cycles_used": cycles_used,
        "cycle_cap": cycle_cap,
        "verification": verification,
        "verification_details": verification_details or {},
        "outcome": outcome,
        "outcome_reason": outcome_reason,
        "session_cycle": session_cycle,
        "started_at": started_at,
        "duration_seconds": duration,
        # Per-cycle active-work timing (excludes waits/idle). Empty on legacy
        # runs and any run that did not record cycle timing. Guard the type: a
        # corrupt accumulator value must not crash list().
        "cycles": list(cycles) if isinstance(cycles, list) else [],
        "finding_areas": [top_dir(p) for p in paths],
        "finding_paths": paths,
        "judge": judge or {"enabled": False},
        "terminal_breakdown": dict(terminal_breakdown) if isinstance(terminal_breakdown, dict) else {},
        "patterns": dict(patterns) if isinstance(patterns, dict) else {},
    }
