# Claude Code as the reviewer

Load when the selected reviewer is Claude Code. Selection itself (discovery, confirming, persisting) is in `references/reviewer-selection.md`.

## Surfaces and selection

One vendor (`claude`, REST `claude[bot]`) covers the hosted Code Review service, the claude-code-action's `/code-review --comment` workflow, and a CI job posting Claude's summary as a PR comment, often as `github-actions[bot]`. Discovery lists the CI job as `Claude Code (as github-actions)`, `vendor: claude`; persist it with `--reviewer github-actions --reviewer-vendor claude --reviewer-source confirmed` (stored; the login keeps its spelling in filters). The re-review trigger is exactly `@claude review`.

## Findings

- **Inline comments** grade by their leading 🔴 Important / 🟡 Nit / 🟣 Pre-existing.
- **Summary comments** (the newest one, unless a later review supersedes it) become synthetic threads (`isSummaryCommentFinding`), graded by the summary. Three shapes are read: `### <Grade>: title` sections with a `**File**:` field; paragraphs or list items that open with their anchor and grade (`` `path:line` — **Warning:** … ``, `` **Warning:** `path:line` — … ``, `` 1. `path:line` **Suggestion:** … ``); and a numbered list whose items end in full-sha blob links. One summary can mix them; each finding is read once (a blob link under a graded item is its location), and beside sections or linked items a graded line with no location is a rating (`**Low**:` under `### Risks`), not a finding. Grades map to the shared scale (Critical → critical, Warning → medium, Suggestion/Nit/Minor → low), so `--min-severity` works unchanged.
- **The verdict's count is the summary's own total.** `**Verdict:** 0 Critical, 1 Warning, 2 Suggestions — Approve` claims three findings. Fewer read than counted, or neither findings nor approval phrasing, leaves the overview incomplete (`cleanBlockedBy: overview_unparseable` once no thread is left): open the summary (`reviewerOverview.url`) and read it. An approving verdict never hides a listed finding.

## Clean gate

Claude's reviews name the head they cover, so it has Copilot's clean gate: `cleanBlockedBy: no_review_for_head` means request or wait, never DONE. A summary comment names no commit; it covers the head when posted after the head reached the PR (the head's first check suite or status, else its commit time).

## Re-review statuses specific to Claude

- `already_reviewed_head` — the selected login's review, or a summary posted after the head reached the PR, already covers it. Nothing is posted: fetch, do not wait. A covering summary that came unasked after an earlier one (the push asked for it) is recorded once per head as the cycle's request, like a wait below; the PR's opening review stays free.
- `review_pending` with `reason: reviews_on_push` — the login's summaries arrived unasked at least twice (no trigger comment since the summary before, or only one the action already answered with its own finished reply), so the repository reviews every push. A ping would only start a second Claude run beside the push review (the action's `@claude` job). Nothing is posted; wait with `--wait --after <pending_since>` (when the head reached the PR). The wait is recorded once per head in the PR's request log and counts toward the cap like a ping, and the fetch's `reReviewRequests` includes it. A retry for a head already waited on pings instead once every check on the head has finished (the job failed, was cancelled or skipped) or the wait is over 30 minutes old (the loop's own `--timeout 1800`); the first request for a head always waits. With a torn local log the script pings instead, as before.

A "Claude finished @user's task" comment comes from the action's `@claude` job, not a summary: the loop does not read it. If one appears on the PR, read it yourself.
