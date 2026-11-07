# Resuming after the cap, missed triggers, and post-stop pushes

## Resuming after the cap

Re-invoking the loop on a PR already at the cap is usually a **resume signal**, not an instant stop. Evaluate as a **strict priority order — first match wins**:

| Priority | Condition | Action |
|----------|-----------|--------|
| 1 (highest) | User increased the cap (effective `max_rereview_requests` > cap already consumed) | **Continue** from the next cycle |
| 2 | Interrupted local work **not pushed** (local commits/edits beyond the remote branch HEAD) | **Finish the push** — no new cycle consumed |
| 3 | **Pushed** but no re-review request posted for that pushed SHA | **Request review** for that SHA — no new cycle consumed |
| 4 (lowest) | No new local work **and** no higher cap | **Hard stop** (Stopping Condition 1) |

A bumped cap (priority 1) wins even when unpushed work also exists. Cases 2 and 3 complete an already-started cycle and do **not** consume a new one.

> **Follow-up (deterministic detection).** Cases 2 and 3 are prose-detected for now and MUST become deterministic: case 2 needs a local-vs-remote SHA comparison (`git rev-parse HEAD` vs `@{u}`); case 3 needs a GitHub check that the latest pushed SHA has no following agent-posted re-review comment. Tracked in `docs/superpowers/specs/2026-06-06-pr37-followup-design.md`.

## Recovery: missed initial trigger

The skill auto-triggers after `gh pr create`. If that was missed, invoke retroactively at the next opportunity:

- **Assume cycle 0 already happened.** At session start / skill load, check whether the PR has *any* review activity from the configured reviewer.
- Activity exists → do **not** wait for an initial review; fetch threads and run the cycle.
- No activity anywhere on the PR → trigger the first review ourselves (cycle 0's opening ping) and then wait. For a reviewer with `auto_reviews: false` this is the normal path, not recovery.
- Recovery clause only — skip entirely if no reviewer is configured for the repo.

## Recovery: resumed session or compacted context

The fetch collapses threads that are unchanged since the previous cycle to
one-line stubs (delta mode). That is safe only while the earlier full render
is still in your context. On a **resumed session** or after **context
compaction**, the persisted baseline survives but your context does not — the
stubs would point at content you can no longer see. Re-establish the baseline
with a single full fetch:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --full
```

`--full` renders every thread completely (diff hunks keep the standard
truncation cap) and recommits the baseline, so the next fetch collapses
correctly again. The sticky PR receipt (findings + URLs) is the out-of-context
backup if you only need anchors, not bodies.

## Follow-up pushes after the loop stops

New commits pushed to the PR branch after the loop stopped:

- Any commit touches files with `UNRESOLVED` or `ADDRESSED_BY_REPLY` threads → automatically resume the loop (subject to the cap).
- Otherwise stay stopped. A self-reviewing bot picks up the new commit unattended; a ping-only bot will not review again until asked — the intended stop.
- Doc-only commits (README, CLAUDE.md, comments) never resume the loop on their own.
