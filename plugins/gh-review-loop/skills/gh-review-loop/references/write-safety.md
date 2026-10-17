# GitHub write safety

Load this when deciding whether a push is safe or when the user requests thread resolution. Never submit approve/request-changes reviews unless explicitly asked.

## Resolution policy

- Resolution is off by default for every state. An outdated location does not establish that a defect was fixed.
- **ADDRESSED_BY_REPLY** requires a reply whose first line is `/review-loop defer`, after the latest selected-reviewer comment, from a GitHub `OWNER` or `COLLABORATOR`. Ordinary objections, acknowledgments, quoted markers, organization membership alone, bots, and missing association data do not defer a finding.
- A user choosing "Defer and record on GitHub" authorizes that reply in the identified thread, not thread resolution. Follow [User decisions](user-decisions.md#recording-a-deferral) to ask, post the marker, and verify recognition; a chat answer alone does not set `ADDRESSED_BY_REPLY`.
- Explicit user opt-in enables `--resolve-outdated` or `--resolve-addressed-by-reply`. `--dry-run` previews intended resolutions without GraphQL writes.
- **UNRESOLVED** threads are never resolved without an explicit user request.

## Verified publication on every runtime

Use `publish_changes.py` instead of separate Git add/commit/push commands. It requires an open PR, the exact PR head branch and commit, a push remote matching the head repository, a fetched run, a confirmed reviewer, and a known cap count below the limit. Base/default branches are blocked.

A confirmed/customized profile must contain at least one boolean `required: true` check. Empty, skipped, malformed, optional-only, or failed required checks block publication. The helper runs the profile once, emits the cycle receipt with those results, and refuses if file contents, the profile, the PR head, or checkout identity changed. Use literal argv arrays for verification commands where possible.

Pass every changed file explicitly via `--paths`; the index must initially be empty, and unrelated tracked or untracked files block publication. No automatic stashing, cleanup, reset, force push, or retry. The helper stages only those paths, checks that commit hooks preserved the verified tree, pushes the PR branch normally, and confirms the remote SHA. On failure, inspect local state; an existing local commit is preserved. `--dry-run` runs the checks and validates publication without staging, committing, or pushing.

The publisher holds a cooperative per-PR lock from verification through push and its automatic re-review request. Standalone requests use the same lock. Contention fails immediately before work starts; retry after the owning operation finishes. Locks cover processes using the same local state directory, not other machines, manual Git, or user-supplied checks/hooks. GitHub head reads and writes remain separate operations.

Read `review_request` in the publisher's JSON instead of requesting again. A request refusal or error after push preserves `published: true` and the published `head`; an uncertain request must be reconciled before retrying. `--dry-run` requests nothing. Shared state mutations use a brief global lock and atomic replacement; unreadable existing state blocks mutation instead of being overwritten. Network calls never hold that global lock. Native requests reserve a per-PR `.unconfirmed.json` record before writing and append confirmed provenance to a per-PR `.jsonl` log. Legacy state markers remain blocking until reconciled. Comment requests retain their state marker. Before a request, a reservation or in-flight marker is saved; an unknown result or failed durable record leaves it in place and blocks another publish/request. Run `request_rereview.py --repo OWNER/REPO --pr N --reconcile --json` after a minute: it reads GitHub evidence and records a confirmed request once without another POST. Missing evidence retains the marker; `--dry-run` previews recording. Do not erase it to bypass the cap. Concurrent fetches can update the summary/reviewer, so use one collector per PR.

`publication_unconfirmed` preserves both SHAs and `published: null`; see [recovery](resume-and-recovery.md).

REST success for an initial Copilot request must register the selected reviewer, directly or through bounded GraphQL checks on the saved head. If the review already finished, paginated reviews plus a new matching request event can confirm registration: the selected Bot must have submitted on that head at or after the event's GitHub timestamp. An actor and full request-event ID snapshot are required before cancellation or a re-review POST; the marker stores IDs for later matching. Comment triggers likewise snapshot comment IDs and require a new ID with the exact actor and phrase during recovery. Blank or malformed snapshot reads block writes; older markers retain timestamp-based recovery. An empty success response is uncertain until registration is confirmed. Stale replacement requires cancellation to become visible first, uses refreshed PR state, and rechecks a changed first-review cap boundary. Native cancellation and POST bind to their respective saved heads even without `--expect-head`. Comment failures before POST or definite HTTP rejections clear their markers; transport failures, 5xx responses, and malformed success results retain them.

ID-based native reconciliation records proven requests at GitHub's timestamp after later head moves; legacy recovery retains head checks.

## Explicit lifecycle commands

No tool or Stop hooks are installed. Setup, receipt generation, and terminal recording use explicit start/publish/finish commands. See `references/lifecycle.md` for commands, metadata, dry-run behavior, and recovery.

`--expect-head <sha>` is a best-effort guard on both re-review paths: pass the full published SHA. Immediately before a guarded comment, native request, or stale-request cancellation, the script rechecks that the PR is open at that head. A mismatch or unreadable state blocks the write. GitHub offers no atomic expected-head condition for these writes. After a successful request, a second read can report `review_head_unconfirmed` with `posted: true`; the request remains recorded and charged. Fetch current feedback and inspect the heads; do not automatically repost. A trigger that lands before GitHub sees the push buys a review of the previous commit.

A confirmed log record consumes capacity even while the GitHub timeline lags. Free initial requests use `countable: false`; legacy `initial_review: true` records remain free. Incomplete logs, unresolved reservations, and durable incomplete markers make capacity unknown. Native reservations use the target branch's prior-activity snapshots for settlement; explicit `--reconcile` records evidence without posting another request.
