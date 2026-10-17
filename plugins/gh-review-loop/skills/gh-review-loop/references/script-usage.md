# Script usage catalog

Resolve the installed root from the selected SKILL.md's absolute path. Bash examples assume `$GGRL_PLUGIN_ROOT` is exported; on Windows use `python` and quoted absolute script paths with the same arguments.

```bash
# Default fetch leaves resolution unchanged and prints current feedback.
# Explicit resolution uses --resolve-outdated / --resolve-addressed-by-reply.
# Delta mode: threads unchanged since the previous cycle collapse to one line;
# any change (new reply, edited body, moved anchor) renders full. The output
# ends with the profile-intro + planned-verification blocks (relay from here).
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py"

# Full render, ignoring the delta baseline. Use once after a resumed session
# or context compaction to re-establish the baseline; diff hunks keep the
# standard truncation cap.
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --full

# Wait for initial reviewer activity; without --after it returns immediately
# once activity appears, without waiting for a settle period.
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --wait

# Wait for a NEW reviewer response after a re-review request (cycle 2+).
# Pass the re-review comment timestamp so prior-cycle activity is ignored.
# WITH --after the wait settles (waits for the new review to stabilize) so a
# fast-forward fetch doesn't catch a half-posted re-review.
# PRIMARY USE: run this as a BACKGROUND Bash task on runtimes with task
# completion notifications (Claude Code) — one turn per wait. It writes a
# one-line liveness heartbeat to stderr every ~60s; refusals and timeouts end
# the captured output with a deterministic relayable block (exit 0).
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --wait --after "$REREVIEW_AT" --timeout 1800

# Chunked wait (fallback for runtimes without background-task completion
# notifications, e.g. Codex): return within the chunk with a deterministic
# status instead of blocking. Relay the one-line heartbeat verbatim, then run
# the next chunk with the suggested next_wait_seconds (60s, then 300s, then 900s).
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --wait --after "$REREVIEW_AT" --wait-chunk-seconds 60

# After a --format json chunk, render the human heartbeat for relay:
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --wait-heartbeat

# Request reviewer re-review via the script-owned helper.
# Parse JSON stdout and capture `created_at` as REREVIEW_AT.
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/request_rereview.py" \
    --repo OWNER/REPO --pr PR_NUMBER \
    --review-trigger-mention "$REVIEW_TRIGGER_MENTION" --expect-head "$PUBLISHED_HEAD" --json
# Copilot (persisted trigger_kind: review_request): same command without the
# mention; it posts a GitHub reviewer request. PUBLISHED_HEAD is the publish
# helper's JSON head; a moved or closed PR blocks the write.

# Render deterministic human-readable formatter blocks for relay.
# Default fetch includes both blocks (JSON: humanBlocks.profileIntro and
# .plannedVerification); with a saved profile these forms are out-of-band.
# They ARE part of the first-run cycle: with no profile saved, the fetch emits
# a regenerate notice instead of the blocks (humanBlocks.profileBlocksProvisional
# is true, plannedVerification is empty), because the profile decision lands
# after that fetch. Save the profile, then run these two to get the real blocks.
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --profile-intro --repo OWNER/REPO
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --planned-verification --repo OWNER/REPO

# Standalone diagnosis only; publish_changes.py already runs this once.
# Do not run the full profile separately before publishing.
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/run_profile.py" owner/repo /path/to/repo

# Read-only fetch (no GraphQL mutations)
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --no-resolve-outdated --no-resolve-addressed-by-reply

# Dry-run all resolutions (logs intended writes to stderr without calling GraphQL)
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --dry-run

# Specific PR URL
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --pr https://github.com/OWNER/REPO/pull/123

# JSON for automation (stdout is machine JSON only; logs go to stderr)
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --format json

# Include outdated, resolved, or addressed-by-reply threads (history investigation)
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --no-resolve-outdated --include-outdated --include-resolved --include-addressed-by-reply

# Discover and persist a reviewer bot
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --list-reviewers --format json
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --reviewer coderabbitai --reviewer-source confirmed --reviewer-name CodeRabbit \
    --review-trigger-mention @coderabbitai

# Reset reviewer selection for this PR
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --reset-reviewer
```

The script emits `warning: ... hit page limit ...` to stderr if any GraphQL page maxes out (review threads, reviews, PR comments, or comments within a thread) — older items may be silently missing.

# Wait outcome handling (both modes)
# Statuses: waiting, settling, ready (same call returns the threads),
# timed_out, refused.
# - refused, kind: withdrawn -> terminal: record --outcome human --gemini-unconfirmed.
# - refused, kind: quota_exhausted -> ask the user now (stop the loop, or
#   upgrade/add credits then re-run the same wait); do NOT re-request the review.
# - timed_out -> record --gemini-unconfirmed; do not guess clean. Say the wait
#   timed out — never invent feedback.

## Explicit lifecycle (all runtimes)

Use installed absolute paths for `start_loop.py`, `publish_changes.py`, and `finish_loop.py`; each accepts `--repo OWNER/REPO --pr N --repo-root ROOT`. Start before editing, publish each fix cycle, and finish after the final reviewer wait or a terminal path with no publication. Full arguments, report metadata, and examples: `references/lifecycle.md`.

The publisher verifies once and generates the cycle receipt. Do not run a separate full profile or cycle summary first. Relay `cycle_summary.output`; pass its `verification` object to finish and relay `receipt.output`.
