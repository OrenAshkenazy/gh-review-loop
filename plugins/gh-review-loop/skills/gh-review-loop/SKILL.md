---
name: gh-review-loop
description: Use after a GitHub PR is opened or to handle AI reviewer feedback (Codex, CodeRabbit, Copilot, or any reviewer bot) - fix reviewer comments, sweep sibling instances, verify, push, request re-review. Needs an open PR and authenticated gh; asks which bot if none is configured; stops at the cap.
---

# AI Reviewer PR Review Loop

Run the full GitHub PR loop: wait for the configured AI reviewer, fetch unresolved actionable review threads, acknowledge, fix, verify against the repo's own checks, push, request re-review — capped. Prefer thread-aware review data over flat PR comments (threads preserve `isResolved`, `isOutdated`, paths, line anchors, diff hunks).

## Precedence and preconditions

Explicit user instructions override every default here and in the references (cap, severity filter, auto-resolve, reviewer choice); "never X without an explicit request" means an explicit request unlocks X. Say in one line when you override a default. This file wins over any reference file — don't stop to reconcile them, follow this file.

Requires `python3` (3.10+) and an authenticated `gh` CLI with GitHub access. If either is missing the script prints `error: ...` — relay it and stop. Never substitute raw API calls, unauthenticated requests, or guesses about review state.

## Reference files (load on demand)

This file holds the every-cycle happy path. Read the referenced file **at the point of need**, not up front:

| File | Load when |
|---|---|
| `references/reviewer-selection.md` | No persisted reviewer; discovery returns `none_configured`/`partial`/`confirmation_required`; reviewer switch/reset |
| `references/verification-profile.md` | First run for a repo (edit gate blocks until a profile decision); customize/un-skip/re-detect |
| `references/sweep-internals.md` | Receipt shows a multi-site pattern (`count >= 2`), a multi-line ranged finding, or a `Clustering:` advisory |
| `references/write-safety.md` | Resolving beyond the auto-resolve set; write constraints; is a push safe; what the bundled hooks enforce |
| `references/judge-eval.md` | `judge_mode` ≠ `off`, the user mentions judge/eval, or the one-time tip is due |
| `references/receipts-and-metrics.md` | `--post-receipt`/`--sticky-receipt`, `--semantic-risk`, `--stats`, the `--record-run` flag catalog |
| `references/terminal-report.md` | Terminal `[loop] Summary` has `remaining_actionable > 0` |
| `references/resume-and-recovery.md` | Re-invocation at the cap; new pushes after the loop stopped; resumed/compacted session |
| `references/variations.md` | Phrasing isn't the plain default loop (severity filters, audit-only, cap changes, judge modes, stats) |
| `references/script-usage.md` | Any invocation not shown inline (wait catalog + outcome handling, discovery, read-only, dry-run, JSON, history) |

## Reviewer Selection

One configured reviewer bot per PR; returning runs reuse the persisted selection silently. Anything else — no persisted reviewer, `none_configured`, `partial: true`, `confirmation_required: true`, or a switch/reset — load `references/reviewer-selection.md` and run its prompt flow before any edit or re-review request. Never wait on a reviewer the user did not choose; never hand-write a trigger mention for an unknown bot.

## Thread States

- **`RESOLVED`** / **`OUTDATED`** — skip (outdated is auto-resolved by the script).
- **`ADDRESSED_BY_REPLY`** — a maintainer's substantive reply deferred it: do not fix again; auto-resolved next pass (details: `references/write-safety.md`).
- **`UNRESOLVED`** — actionable; drives the next fix attempt. Ordered `critical → high → medium → low → unknown`, parsed from the reviewer's severity badges.

## Cycle Counting

A **cycle** is one agent-posted re-review request after the reviewer's first review (cycle 0, free). Cap = `max_rereview_requests` (prefs file, default 3); after cycle N, hard stop. Replies and pushes without a re-review request do not count — only the agent's own requests do (login auto-detected; `--agent-login`/`--no-agent-filter` to override). The cap blocks new fix cycles and re-review requests, not stale-thread cleanup, metrics, terminal classification, or recording terminal state.

## Pattern → Sweep → Converge

Bot reviewers are LLMs: fixing only the flagged sites of a pattern teaches the bot to flag more next cycle. Collapse that into one cycle. The receipt's `Patterns (N):` section clusters findings — reason about patterns, not a flat list. For each multi-site pattern (`count >= 2`) and each multi-line ranged finding: load `references/sweep-internals.md`, run `sweep_siblings.py` over the PR's **changed files only**, print the sweep report first (report-then-go — never edit unflagged code silently), then fix the cluster plus reported siblings this cycle. Honor the script's `status` (`too_few_sites`/`pattern_too_thin`/`no_source` → do not sweep; `truncated: true` → show the report, ask first). Mark swept patterns with `--swept-pattern <sig>` alongside `--fixed-finding`.

## Verification Profile

Each repo can have a code-derived verification profile — the checks the verify step runs. **First run for a repo** (a `PreToolUse` hook blocks edits until a profile decision is saved): load `references/verification-profile.md` and run its detect → menu → save flow before any edit. **Subsequent runs**: no prompt — the fetch output ends with the profile intro and planned-verification blocks; relay the intro from there, then for `confirmed`/`customized` run `run_profile.py <owner/repo> <repo_root>` — never call the test runner directly — feeding its `verification` field into `--verification` and its JSON into `--verification-details`. Verify fails iff any `required` check fails or times out. On `skipped`/unknown, relay the fallback intro and use ad-hoc narrowest-meaningful checks.

## Receipts: per-cycle and terminal

- **`--cycle-summary`** — read-only mid-loop receipt from accumulated run state. Safe every cycle.
- **`--record-run`** — terminal: appends one record to local `runs.jsonl` and clears the run accumulator. Call exactly once, at loop end (flag catalog: `references/receipts-and-metrics.md`).

**Emit a receipt at the end of every cycle.** Non-terminal → `--cycle-summary` right after verify, REQUIRED even when fixes were small. Terminal → `--record-run` only (never both on one cycle; never twice).

**Single-channel delivery.** Both deliver the FULL receipt to the sticky PR comment (one per PR, edited in place) and print a one-line `[loop]` pointer to stdout. Relay the pointer verbatim; do NOT reprint the full receipt in chat. On comment-write failure (or `--dry-run`) the script prints the full receipt to stdout — relay that fallback verbatim instead.

**Script-owned human blocks** — relay verbatim, never paraphrase unless the script fails: profile intro, planned verification, judge table/skip line, receipt pointer (or fallback receipt), semantic-risk note, `Next options:`, wait heartbeat. Before the reviewer confirms a re-review use the script's fixed-pending wording, never `Remaining valid actionable`. `--json`/`--format json` stdout is machine JSON — parse, don't relay.

## Progress Narration

<HARD-GATE>
Before `git push` on a non-terminal cycle: run `--cycle-summary` and relay its printed `[loop]` pointer line. For a terminal cycle **that follows a final push**: request re-review, capture `REREVIEW_AT`, wait with `--wait --after "$REREVIEW_AT"`, and set the terminal reviewer-confirmation flag from that wait result before `--record-run`. Terminal paths that publish nothing (already clean, cap reached, human decision, regression, no progress) record directly — no push, re-review, or wait. Enforced mechanically on hook runtimes: `loop_summary_gate.py` blocks `git push` while the summary is stale (exit 2 names the exact fix); `--record-run` is exempt. Violating the letter violates the spirit.
</HARD-GATE>

Emit one-line status updates at each phase transition. **N** = session cycle: copy it from the script (`[loop] session cycle N`, `loopStatus.sessionCycle`, or the receipt header) — never count it yourself. It exists only after a fetch, so the pre-fetch line omits it. **M/K** = cap consumed.

| Phase | Narration line |
|---|---|
| Before fetch | `[loop] fetching threads from PR #<num>...` (no ordinal yet — the fetch is what reports it) |
| After fetch | `[loop] session cycle N — re-review cap: M/K consumed. <J> actionable thread(s) (severity: <breakdown>). Fixing.` + judge tip/block if due |
| After fixes | `[loop] session cycle N — fixes applied. Verifying via profile runner.` |
| After verify | `[loop] session cycle N — verified (<test summary>).` |
| Before push | `--cycle-summary` + relay pointer (HARD GATE), then `[loop] session cycle N — committing and pushing <sha>...` |
| After push | `[loop] session cycle N — pushed. Requesting reviewer re-review. Cap now M/K.` |
| Reviewer wait | Background task (primary) or chunked heartbeats (fallback) — Workflow step 8 |
| Stop | `[loop] STOP — <stop-condition>: <one-line explanation>.` |
| Done | `[loop] DONE — 0 actionable threads remaining. Re-review requests used: M/K.` (cap consumption, not N — a clean PR ends at `0/K`) + relay the `--record-run` pointer; `remaining_actionable > 0` → `references/terminal-report.md` |

Skip narration only in pure non-interactive batch mode. User stepping away → pair with `--sticky-receipt`.

## Optional Judge Eval

An opt-in, read-only OpenAI judge can classify findings. **Off by default; nothing is sent to OpenAI unless the user opts in.** When the saved mode ≠ `off`, the user mentions judge eval, or the one-time tip is due, load `references/judge-eval.md`.

## Stopping Conditions

Stop and report instead of pushing or re-asking when any is true: **1. Cap reached.** **2. All clean** — no `UNRESOLVED` actionable threads after cleanup. **3. Human decision required** — remaining threads are informational, duplicate, contradictory, or need a human call (includes `ADDRESSED_BY_REPLY`). **4. Test regression** — failure not clearly caused by the finding-addressing change. **5. No progress** — the script prints `[loop] no_progress: …` (unchanged actionable fingerprint): stop immediately with `--record-run --outcome no_progress`; do not push or re-request.

At the cap still run cleanup, terminal classification, metrics recording, and the final summary. Re-invocation at the cap is usually a resume signal — load `references/resume-and-recovery.md` before declaring a hard stop.

## Workflow

1. **Trigger by default after PR creation.** "Create the PR", "ship this", "run the review loop" all authorize the full loop.
2. **Resolve the PR** — given URL/number, else `gh pr view --json number,url,headRefName,baseRefName`. No PR → report the blocker.
3. **Select the reviewer** (persisted → continue silently; else see Reviewer Selection).
4. **Wait for the first review.** `reviewerSelection.auto_reviews: false` (Codex) and no review activity → post the trigger via `request_rereview.py` first, then wait with `--after`; waiting without pinging burns the whole timeout. Activity already present → skip the wait and fetch. Timeout → say so; never invent feedback.
5. **Check loop status** — count prior agent re-review requests; at or above the cap → Stopping Conditions.
6. **Fetch, acknowledge, classify.** Default fetch (below). Summarize actionable findings grouped by file/behavior; none → report clean and stop. First run for the repo → profile decision NOW, before any edit (see Verification Profile), then relay the profile intro. Explanation requests get a reply draft, not a forced edit; conflicts or regression risk → stop and surface the tradeoff.
7. **Fix + verify.** Scoped to feedback; read before editing; each change traceable to a feedback cluster; sweep per Pattern → Sweep → Converge. Verify via the profile runner; checks can't run → report why.
8. **Commit, push, re-review, wait, record.** Commit; `--cycle-summary` + relay pointer; push. Within the cap, post the re-review via `request_rereview.py --repo OWNER/REPO --pr N --json` (never hand-write the trigger — it also stamps the cycle boundary, so a hand-written ping leaves the ordinal stuck; `status: no_safe_trigger` → stop and relay exactly), capturing `created_at` as `REREVIEW_AT`. Then wait — primary (runtimes with background-task completion notifications, e.g. Claude Code): a **background** Bash task, one turn per wait:
   ```bash
   python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
     --wait --after "$REREVIEW_AT" --timeout 1800
   ```
   Do not poll it; never invent its result; relay its final output once. Fallback (no completion notifications, e.g. Codex): chunked waits — commands, statuses, refused/timed-out handling in `references/script-usage.md`. After the final wait, `--record-run` exactly once and relay its `[loop] Summary` pointer line.

## Script Usage

Resolve the runtime-neutral plugin root once, as its own Bash call:

```bash
GGRL_PLUGIN_ROOT="${GGRL_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -z "$GGRL_PLUGIN_ROOT" ]; then
  GGRL_PLUGIN_ROOT=$(
    find ~/.codex/plugins ~/.codex/plugins/cache ~/.claude/plugins/cache \
      -type d -path "*/skills/gh-review-loop" 2>/dev/null \
      | sort -rV | head -1 | sed 's|/skills/gh-review-loop$||'
  )
fi
if [ -z "$GGRL_PLUGIN_ROOT" ] && [ -d "$(git rev-parse --show-toplevel 2>/dev/null)/plugins/gh-review-loop" ]; then
  GGRL_PLUGIN_ROOT="$(git rev-parse --show-toplevel)/plugins/gh-review-loop"
fi
export GGRL_PLUGIN_ROOT
```

Default fetch (resolves stale threads, prints current feedback, ends with the profile intro + planned-verification blocks):

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" [--pr <URL>]
```

**Delta mode.** Threads unchanged since the previous cycle collapse to one line (anchor, severity, URL) — not missing data; any change renders full automatically. On a resumed session or after context compaction, run one fetch with `--full` to re-establish the baseline. Full option catalog: `references/script-usage.md`. The script warns on stderr when a GraphQL page limit is hit.

## GitHub Write Safety

Invariants: **never resolve an `UNRESOLVED` thread without an explicit user request; never submit approve/request-changes reviews unless explicitly asked.** Stale threads (outdated, addressed-by-reply) auto-resolve by default; uncertain run → `--dry-run` first. Full policy, publish-stop conditions, and the bundled-hooks table: `references/write-safety.md`.
