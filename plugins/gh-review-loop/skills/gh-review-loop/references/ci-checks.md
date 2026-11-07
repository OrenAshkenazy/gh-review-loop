# Head checks (CI)

Load when the fetch prints a `[loop] CI:` block. A red or still-running check on the head is feedback the thread filter never sees: a reviewer that waits for CI never arrives, and a loop that only watches threads would wait out its whole timeout. The script surfaces it; the loop never fixes it.

## What the block says

The fetch reads the head commit's `statusCheckRollup` and prints one block when any check failed or is still running (nothing when green). JSON: `loopStatus.ci` (`state`, `total`, `pending`, `failing[]`, `truncated`). A failing check lists its name, `[required]` when branch protection requires it, its details URL and, for a GitHub Actions check, a `log:` command (`gh run view <id> --repo … --log-failed`). Cancelled, skipped and neutral checks are not failures (Claude Code Review's own check run never concludes as one).

## What to do

**Report, do not fix.** Relay the block verbatim. Fix the reviewer threads as usual. If a failing check is all that is left (no `UNRESOLVED` actionable thread), stop with `--record-run --outcome ci_failed --outcome-reason '<check name>'`. Never push a CI "fix" the user did not ask for, and never request a re-review just to turn a check green. If the user asks you to fix the check, that is ordinary work outside the loop: run the printed `log:` command, read the failing step, and fix it on the branch; the loop's next fetch sees the new head.

**Pending checks.** `[loop] CI: N check(s) still running` is informational: a reviewer that waits for CI (a CI job running Claude Code, Copilot after required checks) has not seen the head yet. Wait before calling the PR clean; do not re-request.

**Incomplete results.** When the rollup's own state is not `SUCCESS` (or the 50-context page is full) but no failing or pending check was fetched, the block says `results incomplete` instead of staying silent: open the PR's checks tab before calling the PR clean.

**During a wait.** Every non-ready wait exit (`waiting`, `settling`, `timed_out`, in both wait modes) prints the same CI block after the heartbeat (JSON: `wait.ci`), and the blocking wait's stderr heartbeat names failing checks. Read the block before re-running the wait.
