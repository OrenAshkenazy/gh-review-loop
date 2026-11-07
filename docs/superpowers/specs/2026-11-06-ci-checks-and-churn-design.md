# CI checks and churn

**Date:** 2026-11-06 · **Status:** implemented (PR #123)

## Why

A survey of the review-loop tools shipping in late 2026 found two things
the adopted ones do that this loop did not: notice a red or still-running
check on the head, and notice when the reviewer has started reviewing the
loop's own fixes. Two further ideas from that survey (resuming from state
embedded in the PR receipt, and listing threads a person opened) were built
and then dropped from the PR as speculative: no user had reported either
problem, and both added weight to the fetch script (#101) for an unproven
gain. The write-safety invariants are unchanged: the loop never resolves an
unresolved thread, never submits a review, never merges.

## Design

**CI.** The existing GraphQL query already fetched `statusCheckRollup` for
timestamps; it now also asks for `name`, `status`, `conclusion`,
`detailsUrl` and `isRequired(pullRequestNumber:)` on check runs (and the
status-context equivalents). `ci_status()` reduces that to counts plus a
list of failing checks, each with a `gh run view … --log-failed` command
derived from the Actions details URL. `CANCELLED`/`SKIPPED`/`NEUTRAL` are
not failures. An incomplete rollup (non-success state with no failing
check fetched, or a full page of 50 contexts) is named, not read as green.
Every non-ready wait exit carries the head's checks, because a reviewer
that runs only after CI never arrives on a red head. The block is
script-owned; the loop reports a red check and never fixes it. When a red
check is all that is left the run ends as `ci_failed`.

**Churn.** `track_head_and_fix_induced()` stores `last_head_oid` per fetch
and, on the next, diffs `prev..head` with `-U0` in a checkout of the PR's
repository that has both objects (`repo_root_for_history`, which unlike
`repo_root_for_pr` tolerates a dirty tree and a different HEAD). A new
finding whose RIGHT-side anchor overlaps a new-side hunk is fix-induced;
LEFT-side anchors are base-file coordinates and never compared. Git's
C-quoted paths are decoded so the diff's key matches the thread's path. The
threshold for `churn` — every new finding on cycle 2+ — is agent-reviews'
"findings on lines the earlier fixes changed" rule applied after the first
push rather than after the second; the cap stays the hard brake and the
user can say "ignore churn".

**Codex's clean receipt.** Found by running the loop on the PR itself:
Codex answers a re-review that found nothing with a top-level comment
(`<!-- codex-review-receipt:v1 -->`), not a review, so the waiter timed
out. Codex now posts summary comments in the vendor table and the receipt
is read as its finished review.

## Not done

Replies on fixed threads, confirmed-fixed resolution, edit-aware waiting,
flaky-CI classification with a rerun budget, repo-committed config,
suggestion blocks, check-run annotations. These are the next gaps from the
same survey, in that order; the fetch-script split (#101) comes first.
