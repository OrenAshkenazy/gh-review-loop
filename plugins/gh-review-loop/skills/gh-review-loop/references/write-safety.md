# GitHub write safety

Load this before resolving threads outside the default auto-resolve set, when the user constrains what the loop may touch, or when deciding whether a push is safe. The hard invariants (never resolve `UNRESOLVED` unasked, never submit reviews) are in SKILL.md.

## Resolution policy

- **OUTDATED** — auto-resolved by the default fetch.
- **ADDRESSED_BY_REPLY** — unresolved, but a maintainer posted a substantive reply (≥30 chars, non-bot, not a token ack). A human decision to defer — do not fix again. Auto-resolved on the next pass; skip if the user said "don't resolve": `--no-resolve-addressed-by-reply`. At stop-condition time this is the "human decision required" bucket, not "no progress".
- **UNRESOLVED** — never resolved without an explicit user request.
- **Reviews (approve/request-changes)** — never submitted unless explicitly asked.
- Uncertain run → `--dry-run` first (logs intended resolutions to stderr, no GraphQL writes).

## Stop before publishing

Do not commit/push/re-review when fixes are ambiguous, tests expose a regression, unrelated local changes make a clean commit unsafe, or the PR is at the cap.

## Bundled hooks (what is enforced mechanically)

Three hooks (`hooks/hooks.json`) make the most-skipped obligations mechanical on runtimes that run plugin hooks (Claude Code). All are gated by local state (free no-ops outside an active loop) and fail open. On runtimes that do not run them (Codex, ChatGPT) the same obligations still apply — follow them yourself. The re-review cap is independent of hooks: `request_rereview.py` counts prior agent pings and refuses past the cap on every runtime. It also refuses when the count cannot be established (`uncountable_count`: gh login unresolved, comments query failed) or when the phrase has no mention to count by (`uncountable_trigger`) — an uncountable write is an uncapped write. `--no-cap-check` is the only bypass. The count is taken at write time under the agent's own login; the loop assumes one agent per PR and does not lock against concurrent runs of the same login.

| Event | Script | Guarantees |
|---|---|---|
| `PreToolUse` (`Bash`) | `loop_summary_gate.py` | Blocks `git push` while a loop is active and the summary is stale; exit 2 names the exact `--cycle-summary` to run. |
| `PreToolUse` (`Edit`/`Write`/`MultiEdit`) | `loop_profile_gate.py` | Blocks edits while a loop is active and no verification profile is saved. Any saved profile — including `Skip` — clears it. |
| `Stop` | `loop_summary_hook.py` | If a loop advanced this turn without a summary, emits the authoritative `--cycle-summary`. Dedup-aware; read-only. |
