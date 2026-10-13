# GitHub write safety

Load this when deciding whether a push is safe or when the user requests thread resolution. Never submit approve/request-changes reviews unless explicitly asked.

## Resolution policy

- Resolution is off by default for every state. An outdated location does not establish that a defect was fixed.
- **ADDRESSED_BY_REPLY** requires a reply whose first line is `/review-loop defer`, after the latest selected-reviewer comment, from a GitHub `OWNER` or `COLLABORATOR`. Ordinary objections, acknowledgments, quoted markers, organization membership alone, bots, and missing association data do not defer a finding.
- A user choosing "Defer and record on GitHub" authorizes that reply in the identified thread, not thread resolution. Follow [User decisions](user-decisions.md#recording-a-deferral) to ask, post the marker, and verify recognition; a chat answer alone does not set `ADDRESSED_BY_REPLY`.
- Explicit user opt-in enables `--resolve-outdated` or `--resolve-addressed-by-reply`. `--dry-run` previews intended resolutions without GraphQL writes.
- **UNRESOLVED** threads are never resolved without an explicit user request.

## Verified publication on every runtime

Use `publish_changes.py` instead of separate Git add/commit/push commands. It requires an open PR, the exact PR head branch and commit, a push remote matching the head repository, an emitted current-cycle summary, a confirmed reviewer, and a known cap count below the limit. Base/default branches are blocked.

A confirmed/customized profile must contain at least one boolean `required: true` check. Empty, skipped, malformed, optional-only, or failed required checks block publication. The helper reruns checks and refuses if file contents, the profile, the PR head, or checkout identity changed. Use literal argv arrays for verification commands where possible.

Pass every changed file explicitly via `--paths`; the index must initially be empty, and unrelated tracked or untracked files block publication. No automatic stashing, cleanup, reset, force push, or retry. The helper stages only those paths, checks that commit hooks preserved the verified tree, pushes the PR branch normally, and confirms the remote SHA. On failure, inspect local state; an existing local commit is preserved. `--dry-run` runs the checks and validates publication without staging, committing, or pushing.

The publisher holds a cooperative per-PR lock from verification through push and its automatic re-review request. Standalone requests use the same lock. Contention fails immediately before work starts; retry after the owning operation finishes. Locks cover processes using the same local state directory, not other machines, manual Git, or user-supplied checks/hooks. GitHub head reads and writes remain separate operations.

Read `review_request` in the publisher's JSON instead of requesting again. A request refusal or error after push preserves `published: true` and the published `head`; an uncertain request must be reconciled before retrying. `--dry-run` requests nothing. Shared state mutations use a brief global lock and atomic replacement; unreadable existing state blocks mutation instead of being overwritten. Network calls never hold that global lock. Before a request, an in-flight marker is saved; an unknown result or failed stamp leaves it in place and blocks another publish/request. Run `request_rereview.py --repo OWNER/REPO --pr N --reconcile --json` after a minute: it reads GitHub evidence and records a confirmed request once without another POST. Missing evidence retains the marker; `--dry-run` previews recording. Do not erase it to bypass the cap. Concurrent fetches can update the summary/reviewer, so use one collector per PR.

REST success for an initial Copilot request must register the selected reviewer, directly or through bounded GraphQL checks on the saved head. If the review already finished, paginated reviews plus a new matching request event can confirm registration: the selected Bot must have submitted on that head at or after the event's GitHub timestamp. Before cancellation or POST, the helper requires an actor and a complete snapshot of request-event IDs. The marker saves that baseline; both confirmation and reconciliation match a new event ID and actor without comparing GitHub time to the local clock. Older markers retain timestamp-based recovery. An empty success response is uncertain until registration is confirmed. Stale replacement requires cancellation to become visible first. Comment failures before POST or definite HTTP rejections clear their markers; transport failures, 5xx responses, and malformed success results retain them.

## Bundled hooks

Claude Code additionally runs three state-gated hooks, which fail open. Codex relies on the verified publish helper for the same publication gate. The re-review cap is independently enforced by `request_rereview.py`; unknown counts refuse to post.

For Copilot, both publication and re-review count native requests matched to the loop's locally recorded timestamps. Manual reviewer requests do not consume the loop's cap. Clearing local state removes that provenance; ambiguous history blocks further publication. Comment vendors continue to count the agent's mention comments.

Copilot re-requests use GraphQL `rerequestReviews` with the PR ID and the bot ID from its completed reviews. Initial requests use the REST requested-reviewers endpoint. These operations can have different permissions: a PR author's re-request can succeed when a REST request or `requestReviewsByLogin` is denied. An uncertain mutation result is reconciled against the timeline before recording success; it is never retried through another transport.

| Event | Script | Guarantee |
|---|---|---|
| `PreToolUse` (`Bash`) | `loop_summary_gate.py` | Blocks Git push with a stale cycle summary. |
| `PreToolUse` (`Edit`/`Write`/`MultiEdit`) | `loop_profile_gate.py` | Blocks edits without a saved profile decision; Skip clears this edit hook but cannot enable publication. |
| `Stop` | `loop_summary_hook.py` | Emits the missing cycle summary. |

`--expect-head <sha>` is honored on both re-review paths: pass the full published SHA. Immediately before a guarded comment, native request, or stale-request cancellation, the script rechecks that the PR is open at that head. A mismatch or unreadable state blocks the write; relay the resulting status and do not retry blindly. A trigger that lands before GitHub sees the push buys a review of the previous commit.
