# Churn: findings on lines this loop changed

Load when the fetch prints `[loop] Fix-induced: …` or `[loop] churn: …`.

## What the script measures

Every real fetch records the PR head it saw. On the next fetch the script diffs the previous head against the current one (`git diff -U0`, in a checkout of the PR's repository that has both commits) and checks each **new** finding — one whose fingerprint no earlier cycle saw — against the new-side line ranges of that diff. A new finding whose anchor (its line, or its `startLine..line` range) overlaps a changed range is **fix-induced**: the reviewer is commenting on code the loop itself wrote last cycle. The receipt tags such findings `· on lines this loop changed`, counts them (`Fix-induced findings`), and the run record stores the cumulative count (`fix_induced`). JSON: `loopStatus.fixInduced` (`count`, `newFindings`, `compared`, `churn`).

`compared: false` means no comparison was possible (first fetch of the run, head unchanged, no local checkout of this repository, or a commit not fetched yet) — nothing is flagged, not "nothing was fix-induced". A human pushing between cycles widens the diff; their lines count as changed too.

## `[loop] Fix-induced: K of N` — information

Some new findings are on your fixes, some are not. Fix all of them as usual, but read the fix-induced ones first: they are the reviewer's verdict on last cycle's change, and a second attempt should address the cause, not re-flow the same lines. If the pattern sweep applied last cycle, check whether the sweep introduced the issue at every site before fixing one.

## `[loop] churn: …` — stop condition 7

Fires on cycle 2 or later when **every** new finding this cycle is fix-induced. The reviewer is now reviewing the loop, not the PR: another round most likely produces another round. Stop with:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --record-run --outcome churn --outcome-reason 'all N new findings are on lines this loop changed'
```

Do not push or re-request first. The terminal `Next options:` offers the three honest exits: the user reviews the fix-induced findings themselves, the loop's fixes move to their own PR, or the user says *ignore churn* — then continue the cycle (fix, verify, push, re-request) with the cap as the only brake, and do not stop on churn again this session.

Churn is not no-progress: no-progress means the open set did not change at all; churn means it changed into findings the loop caused. A carried-over finding that still stands is neither.
