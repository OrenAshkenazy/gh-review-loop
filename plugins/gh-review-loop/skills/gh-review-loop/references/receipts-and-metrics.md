# Receipts, run metrics, and PR audit comments

The per-cycle discipline (`--cycle-summary` every non-terminal cycle, one terminal `--record-run`, single-channel delivery to the sticky PR comment with a one-line chat pointer) is in SKILL.md. This file covers the terminal flag catalog, the PR-comment machinery, what metrics are stored, the semantic-risk note, and `--stats`.

## `--record-run` flag catalog

A bare `--record-run` silently defaults every field (`fixed_count` 0, no outcome, no markers) — always pass what the run actually did:

- `--fixed-count <n>` — findings fixed across the run.
- `--verification <passed|failed|skipped>` and `--verification-details '<json>'` — from `run_profile.py` output.
- `--outcome <clean|capped|human|regression|no_progress|verification_failed|fixed_pending_confirmation>` plus `--outcome-reason '<text>'`.
- `--gemini-confirmed` / `--gemini-unconfirmed` — reviewer re-review confirmation from the final wait result.
- `--fixed-finding <fp>` (repeatable) — per-finding fingerprint markers.
- `--swept-pattern <sig>` (repeatable) — the `sig:` token from the Patterns receipt, for each swept pattern.

## PR receipt comments

**Automatic (the default since #86).** Every `--cycle-summary` and `--record-run` writes the full receipt into the sticky comment — one comment per PR, edited in place — and prints the one-line chat pointer. Status header: `RUNNING` per cycle, `DONE` at a clean terminal, `STOPPED` otherwise. No flag needed.

- The comment id is stored in `~/.config/gh-review-loop/state.json` (override dir with `GGRL_STATE_DIR`); subsequent invocations PATCH the same comment — no comment accretion.
- Discovery fallback: if local state is missing, the script finds the receipt by its embedded marker and re-attaches.
- Delivery fallback: if the comment write fails (network, permissions) or `--dry-run` is set, the full receipt prints to stdout instead — a receipt is never lost.

**One-shot receipt (`--post-receipt`).** Leaves one standalone audit-trail comment: re-review requests used (against the cap), threads resolved (outdated + addressed-by-reply), threads still pending, severity breakdown. Preview with `--dry-run --post-receipt`. Right for scripted/batch contexts where each invocation is independent.

**Standalone sticky status (`--sticky-receipt`).** Posts/updates the same sticky comment outside the cycle/record path (e.g. a status-only invocation). `--receipt-status {running,done,stopped}` sets the header.

## Run metrics (`runs.jsonl`)

`--record-run` appends one JSON record per completed loop to `~/.config/gh-review-loop/runs.jsonl` (append-only; never transmitted). Each record holds **counts only**: findings fetched/fixed/needs-human/addressed-by-reply, re-review requests used, verification result, outcome, duration, finding areas/paths, repo + PR number, and a judge-derived breakdown only when judge mode was on. **No identity is recorded** — no git author, no login — so the data cannot become a productivity score.

The run's start timestamp and per-finding accumulation reuse the same per-PR key in `state.json`; no extra state file.

## Semantic risk note

`--semantic-risk` is a manual/heuristic v1 signal — pass it (repeatable) when a fix changes behavior that passing tests may not fully cover: public signatures, return shapes, auth/security behavior, database queries, exception behavior, public APIs.

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --cycle-summary --fixed-count <n> --verification passed \
    --semantic-risk "hash_password(password) -> hash_password(password, salt)" \
    --semantic-risk "get_user() now returns one row instead of a list"
```

Relay the resulting `[loop] Semantic risk note (manual / heuristic)` block verbatim; do not present it as deterministic detection.

## `--stats` (read-only)

Aggregated stats for the current repo from `runs.jsonl`; never touches GitHub.

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --stats
```

Options: `--stats-window N` (default 10 most-recent runs), `--stats-all-repos`, `--format json`.

Run metrics and `--stats` are local-only — stored under `~/.config/gh-review-loop/`, never posted to GitHub, no identity.
