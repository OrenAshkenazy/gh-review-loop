# GitHub write safety

Load this when deciding whether a push is safe or when the user requests thread resolution. Never submit approve/request-changes reviews unless explicitly asked.

## Resolution policy

- Resolution is off by default for every state. An outdated location does not establish that a defect was fixed.
- **ADDRESSED_BY_REPLY** requires a reply whose first line is `/review-loop defer`, after the latest selected-reviewer comment, from a GitHub `OWNER` or `COLLABORATOR`. Ordinary objections, acknowledgments, quoted markers, organization membership alone, bots, and missing association data do not defer a finding.
- Explicit user opt-in enables `--resolve-outdated` or `--resolve-addressed-by-reply`. `--dry-run` previews intended resolutions without GraphQL writes.
- **UNRESOLVED** threads are never resolved without an explicit user request.

## Verified publication on every runtime

Use `publish_changes.py` instead of separate Git add/commit/push commands. It requires an open PR, the exact PR head branch and commit, a push remote matching the head repository, an emitted current-cycle summary, a confirmed reviewer, and a known cap count below the limit. Base/default branches are blocked.

A confirmed/customized profile must contain at least one boolean `required: true` check. Empty, skipped, malformed, optional-only, or failed required checks block publication. The helper reruns checks and refuses if file contents, the profile, the PR head, or checkout identity changed. Use literal argv arrays for verification commands where possible.

Pass every changed file explicitly via `--paths`; the index must initially be empty, and unrelated tracked or untracked files block publication. No automatic stashing, cleanup, reset, force push, or retry. The helper stages only those paths, checks that commit hooks preserved the verified tree, pushes the PR branch normally, and confirms the remote SHA. On failure, inspect local state; an existing local commit is preserved. `--dry-run` runs the checks and validates publication without staging, committing, or pushing.

These safeguards apply to calls through the helper. Arbitrary manual Git commands, arbitrary user-supplied checks/hooks, and concurrent agent runs are outside its enforcement; run one loop per PR. GitHub head reads and writes are separate operations, so the head guard cannot provide an atomic server-side lock.

## Bundled hooks

Claude Code additionally runs three state-gated hooks, which fail open. Codex relies on the verified publish helper for the same publication gate. The re-review cap is independently enforced by `request_rereview.py`; unknown counts refuse to post.

| Event | Script | Guarantee |
|---|---|---|
| `PreToolUse` (`Bash`) | `loop_summary_gate.py` | Blocks Git push with a stale cycle summary. |
| `PreToolUse` (`Edit`/`Write`/`MultiEdit`) | `loop_profile_gate.py` | Blocks edits without a saved profile decision; Skip clears this edit hook but cannot enable publication. |
| `Stop` | `loop_summary_hook.py` | Emits the missing cycle summary. |
`--expect-head <sha>` is honored on both re-review paths: the script reads the PR's head and posts nothing when it differs (`head_mismatch`) or cannot be read (`uncountable_state`). Pass your pushed HEAD, because a trigger that lands before GitHub sees the push buys a review of the previous commit, which the loop then reads as this cycle's verdict.
