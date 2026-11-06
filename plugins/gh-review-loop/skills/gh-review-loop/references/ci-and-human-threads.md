# Head checks (CI) and human review threads

Load when the fetch prints a `[loop] CI:` block or a `[loop] Human review threads` block. Both are feedback the thread filter never sees: a red or still-running check on the head, and a thread a person opened. The script surfaces them; this file says what to do.

## `[loop] CI:` — checks on the head

The fetch reads the head commit's `statusCheckRollup` and prints one block when any check failed or is still running (nothing when green). JSON: `loopStatus.ci` (`state`, `total`, `pending`, `failing[]`, `truncated`) and `loopStatus.fixCi`. A failing check lists its name, `[required]` when branch protection requires it, its details URL and, for a GitHub Actions check, a `log:` command (`gh run view <id> --repo … --log-failed`) — use that command, never guess a run id. Cancelled, skipped and neutral checks are not failures (Claude Code Review's own check run never concludes as one).

**Default: `fix_ci: off` — report, do not fix.** Relay the block verbatim. Fix the reviewer threads as usual. If a failing check is all that is left (no `UNRESOLVED` actionable thread), stop with `--record-run --outcome ci_failed --outcome-reason '<check name>'`; the terminal `Next options:` offers the opt-in. Never push a CI "fix" the user did not ask for, and never request a re-review just to turn a check green.

**Opt-in: `fix_ci: on`.** The user says *fix CI* (pass `--fix-ci` on every fetch this session) or sets `fix_ci: true` in `~/.config/gh-review-loop/preferences.json`. Then each failing check is a finding for this cycle: run the printed `log:` command, read the failing step, fix the cause in the PR's changed files, verify through the profile runner, and push it with the cycle's other fixes. It consumes no extra cap — the cycle's re-review request is the same one. A failure outside the PR's changes (flaky infra, a secret missing from the fork, a required check that never ran) is a human decision: say so and stop with `--outcome human`.

**Pending checks.** `[loop] CI: N check(s) still running` is informational: a reviewer that waits for CI (a CI job running Claude Code, Copilot after required checks) has not seen the head yet. Wait before calling the PR clean; do not re-request.

A `truncated: true` rollup (50+ contexts) may hide a check; say so when reporting clean.

## `[loop] Human review threads` — threads a person opened

Unresolved, current threads whose first comment is from a person — not the configured reviewer, not a `[bot]` login, not the agent's own login. JSON: `humanThreads[]` (`author`, `path`, `line`, `url`, `excerpt`, `reply_count`, `awaiting_reply`) and `loopStatus.humanThreadCount`. `awaiting_reply` is true until the agent's login has the last word in the thread. Threads the agent's login opened are skipped on purpose, so the loop never reads its own replies as new feedback.

**Never auto-fix.** A teammate's comment is a conversation, not a reviewer finding. For each thread awaiting a reply, draft a one-paragraph reply in chat (what you would change, or why not) and ask the user before editing code or posting. The user approving a draft unlocks the edit for that thread only; `--fixed-finding` and the sweep do not apply. Never resolve a human thread.

These threads are excluded from `remaining_actionable`, the no-progress fingerprint and the cap; a PR with only human threads open is reported as `Human review threads (not auto-fixed): N` in the receipt, with outcome `human` when the user did not answer them in this session.

The reviewer-thread rule is unchanged: a human's substantive reply *on a reviewer thread* is `ADDRESSED_BY_REPLY` (`references/write-safety.md`), not a human thread.
