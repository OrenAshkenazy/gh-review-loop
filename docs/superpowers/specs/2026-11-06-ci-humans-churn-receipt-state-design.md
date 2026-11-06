# CI checks, human threads, receipt-embedded state, churn

**Date:** 2026-11-06 · **Status:** implemented

## Why

A survey of the review-loop tools shipping in late 2026 (babysit-pr and its
forks, jonazri/review-pr, naeemba/claude-pr-loop, agent-reviews, Greptile's
`/greploop`) found four things every serious competitor does that this loop
did not: act on a red CI check, treat a teammate's comment differently from
a bot's, keep loop state somewhere other than one machine's config dir, and
notice when the reviewer has started reviewing the loop's own fixes. None of
them has a sibling sweep, a test gate, or a cap enforced at the write, so
those stay the pitch; these four close the gaps without changing the write
safety invariants (the loop still never resolves an unresolved thread,
never submits a review, never merges).

## Design

**CI.** The existing GraphQL query already fetched `statusCheckRollup` for
timestamps; it now also asks for `name`, `status`, `conclusion`,
`detailsUrl` and `isRequired(pullRequestNumber:)` on check runs (and the
status-context equivalents). `ci_status()` reduces that to counts plus a
list of failing checks, each with a `gh run view … --log-failed` command
derived from the Actions details URL. `CANCELLED`/`SKIPPED`/`NEUTRAL` are
not failures. The block is script-owned; the agent's behaviour is a
preference: `fix_ci` off (report; `ci_failed` when nothing else is left)
or on (`--fix-ci` / prefs) — the check becomes a finding for the cycle.

**Human threads.** `human_review_threads()` takes unresolved, current
threads whose first comment is a person's — not the reviewer login, not a
`[bot]` suffix or `Bot` typename, not the agent's own login. The last-word
rule gives `awaiting_reply`. They never enter `threads`, so they do not
touch the cap, the no-progress fingerprint or `remaining_actionable`. The
skill rule is "draft, ask, then edit"; the script only surfaces.

**Receipt state.** `embed_run_state()` serialises the per-run keys that
matter for resuming (ordinal, seq, fingerprints, swept patterns, last head,
cycle timings) as base64 JSON inside an HTML comment appended after the
sticky marker. `hydrate_run_from_receipt()` runs on a real fetch only when
no local run exists and the receipt's header says RUNNING; it sets
`last_summary_seq = update_seq` so the push gate behaves as if the receipt
had just been emitted locally. The cap is still counted from GitHub (and
the local request log for Copilot), not from the receipt. Terminal receipts
carry no block, so a finished run is never resumed by accident.

**Churn.** `track_head_and_fix_induced()` stores `last_head_oid` per fetch
and, on the next, diffs `prev..head` with `-U0` in a checkout of the PR's
repository that has both objects (`repo_root_for_history`, which unlike
`repo_root_for_pr` tolerates a dirty tree and a different HEAD). A new
finding whose anchor overlaps a new-side hunk is fix-induced. The threshold
for `churn` — every new finding on cycle 2+ — is agent-reviews' "findings on
lines the earlier fixes changed" rule applied after the first push rather
than after the second; the cap stays the hard brake and the user can say
"ignore churn".

## Not done

Multi-reviewer PRs, applying ```suggestion blocks, GitLab, auto-merge. The
first two are the next gaps from the same survey; the last is deliberately
out of scope.
