# Resuming after the cap, missed triggers, and post-stop pushes

## First: ask the script where the loop is

`loop_status.py` reads local state only (state.json, the PR's provenance files, the saved verification profile, the git checkout) and prints the phase, the cap, any warnings and the one next command. It never calls `gh`, so it is free to run on a resumed or compacted session, offline, or a day later.

```bash
# The PR is picked from the checkout's active run (else its newest run, else its last recorded run).
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/loop_status.py"
# A specific PR; --format json for the same answer as a machine-readable object.
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/loop_status.py" --pr <URL>
```

Relay the `[loop] status` block verbatim and run its `Next:` command. Local git facts count only when the checkout is on the PR's branch (recorded by the fetch); from another branch's checkout it names the mismatch and asks to re-run with `--cwd <the PR's checkout>` instead of a push or request. Every fetch-script command it prints carries the run's `--max-rereview-requests`. What it decides, in order: no persisted reviewer → select one; an open unconfirmed re-review reservation → `request_rereview.py` (it settles it first); then the phase:

| Phase | Next |
|---|---|
| none | fetch; after `--record-run` the entry keeps `last_run` (outcome, time) and the next fetch starts a new loop |
| `fetched` | no thread at the fetch and HEAD unchanged since → record (clean, `ci_failed`) or request the missing head review; the fetch reported `no_progress` and HEAD unchanged since → `--record-run --outcome no_progress`; no saved profile → the profile decision; cap reached → `--record-run --outcome capped` or raise the cap; else fix, verify, `--cycle-summary` |
| `summarized` | local commits ahead of the upstream → `git push`; no thread or `no_progress` at the fetch and HEAD unchanged since → record as for `fetched`; HEAD still the fetched head → fix, verify, `--cycle-summary`; else `request_rereview.py --expect-head` |
| `requested` | commits after the request → push, then request again; head moved since the request → request again; else `--wait --after <requested_at>` |
| `waiting` | kept `timed_out` → re-run the wait or record `--gemini-unconfirmed`; `refused` → record as human (quota: ask the user first); mid-wait → continue the chunked wait |

## Resuming after the cap

Re-invoking the loop on a PR already at the cap is usually a **resume signal**, not an instant stop. `loop_status.py` applies this order — first match wins:

| Priority | Condition | How it is detected | Action |
|----------|-----------|--------------------|--------|
| 1 (highest) | User increased the cap | `max_rereview_requests` in preferences.json is above the stored `cap_used`; the status stops reporting capped before any fetch | **Continue** from the next cycle |
| 2 | Interrupted local work **not pushed** | `git rev-parse HEAD` ahead of `@{u}` | **Finish the push** — no new cycle consumed |
| 3 | **Pushed** but no re-review request for that SHA | phase `summarized`, or `requested` with a `requested_head` older than HEAD | **Request review** for that SHA — no new cycle consumed |
| 4 (lowest) | No new local work **and** no higher cap | none of the above | **Hard stop** (Stopping Condition 1) |

A bumped cap (priority 1) wins even when unpushed work also exists. Cases 2 and 3 complete an already-started cycle and do **not** consume a new one.

## Recovery: missed initial trigger

The skill auto-triggers after `gh pr create`. If that was missed, invoke retroactively at the next opportunity:

- **Assume cycle 0 already happened.** At session start / skill load, check whether the PR has *any* review activity from the configured reviewer.
- Activity exists → do **not** wait for an initial review; fetch threads and run the cycle.
- No activity anywhere on the PR → trigger the first review ourselves (cycle 0's opening ping) and then wait. For a reviewer with `auto_reviews: false` this is the normal path, not recovery.
- Recovery clause only — skip entirely if no reviewer is configured for the repo.

## Recovery: resumed session or compacted context

Run `loop_status.py` first: it says which phase the loop was in and what to do,
and its warnings name a missing or stale loop-active marker (the hooks are off
until the next fetch). Then, before acting on findings, re-establish the delta
baseline. The fetch collapses threads that are unchanged since the previous
cycle to one-line stubs (delta mode). That is safe only while the earlier full
render is still in your context. On a **resumed session** or after **context
compaction**, the persisted baseline survives but your context does not — the
stubs would point at content you can no longer see. Re-establish it with a
single full fetch:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --full
```

`--full` renders every thread completely (diff hunks keep the standard
truncation cap) and recommits the baseline, so the next fetch collapses
correctly again. The sticky PR receipt (findings + URLs) is the out-of-context
backup if you only need anchors, not bodies.

## Follow-up pushes after the loop stops

New commits pushed to the PR branch after the loop stopped:

- Any commit touches files with `UNRESOLVED` or `ADDRESSED_BY_REPLY` threads → automatically resume the loop (subject to the cap).
- Otherwise stay stopped. A self-reviewing bot picks up the new commit unattended; a ping-only bot will not review again until asked — the intended stop.
- Doc-only commits (README, CLAUDE.md, comments) never resume the loop on their own.
