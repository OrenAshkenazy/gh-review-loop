# Verification profile detail

Load this on a first run for a repo (no saved profile — the edit gate will name this moment), or when the user asks to customize, un-skip, or re-detect a profile. The every-cycle path (relay planned-verification block, run `run_profile.py`, feed `--verification`) is in SKILL.md.

Profiles are stored in `~/.config/gh-review-loop/preferences.json` under `profiles["owner/repo"]`.

## First-run flow (before the first fix attempt)

A `PreToolUse` hook blocks edits until a profile decision is saved. After fetching findings, before any edit:

1. Run `detect_profile.py <repo_root>` → `{stack, confidence, reasons, candidate_checks, presets}`. `presets` is the code-built option list — do not hand-roll the menu.
2. `stack == "unknown"` → do not prompt, but still **record the decision**: `judge.save_profile(repo, source="skipped", detected_stack="unknown")`, then use ad-hoc verification for diagnosis. Publishing requires saving a customized profile with at least one required meaningful check. The marker is what distinguishes "decided: ad hoc" from "not decided yet" — without it the edit gate never clears and every later fetch re-reports a first run.
3. Reconcile against repo docs (`CLAUDE.md`, `CONTRIBUTING`, `README`). If docs pin a non-standard invocation, surface it as a note beside the menu; never auto-persist an absolute path from prose.
4. Prompt once, using each `presets[i].label` verbatim as an option.
5. Persist via `judge.save_profile(...)`: `customize == true` → free-form customize path, `source="customized"`; otherwise persist `preset["checks"]` with `source=preset["source"]`. Every persisted check is `required: true`.
6. The fetch that preceded this decision could not describe a profile that did not exist, so it emitted a regenerate notice instead of the usual blocks (`humanBlocks.profileBlocksProvisional: true`, empty `plannedVerification`). Do not relay that notice as the intro — regenerate both blocks from the saved profile with `--profile-intro --repo <owner/repo>` and `--planned-verification --repo <owner/repo>`, and relay those.

## Gate semantics

Verify fails iff any `required` check fails or times out. Route all verification through `run_profile.py` when a profile is confirmed — never call the test runner directly; the runner times checks, captures structured output, and sets the exit code. Empty or optional-only checks fail verification. On `skipped`/unknown profiles, use ad-hoc checks for diagnosis, then save a customized profile before publication. `publish_changes.py` rejects skipped profiles and reruns required checks on the exact files it commits. Commands may be literal argv arrays (preferred for portable quoting) or strings parsed according to the host platform.

## Customizing / un-skipping

`skipped` suppresses automatic prompts only. Explicit user intent overrides: "add mypy to the checks" → `save_profile(..., source="customized")`; "set up a verification profile" → re-run detect → menu → save even over a `skipped` marker.
