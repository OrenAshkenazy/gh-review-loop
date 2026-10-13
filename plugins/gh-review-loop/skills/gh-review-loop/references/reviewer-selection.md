# Reviewer selection detail

Load this on a first run for a PR (no persisted reviewer), when discovery returns anything but a single confirmed candidate, or when the user asks to switch/reset the reviewer. The happy path (persisted reviewer reused silently) is in SKILL.md.

## First-run prompt flow

One configured reviewer bot per PR. First run with no persisted reviewer is prompt-first:

1. Run `fetch_gemini_threads.py --list-reviewers --format json`.
2. If candidates are returned, ask the user to confirm/choose, then persist with
   `--reviewer <login> --reviewer-source confirmed [--reviewer-name <name>] [--review-trigger-mention <mention>]`.
3. Zero candidates on a fresh PR → the selection returns `source: "none_configured"`, `configured: false`. Do not treat the accompanying `suggestion` as a choice already made. Offer: **Ping the suggested reviewer** (`@codex review` — reviews only on request, so accepting starts cycle 0), **Pick another reviewer** (any known vendor, including Gemini Code Assist, or a bot whose login + mention the user supplies), or **None** (stop with "No AI reviewer threads found on this PR."). Never wait on a reviewer the user did not choose — an uninstalled reviewer is indistinguishable from a slow one.
4. `partial: true` → do not claim no reviewer exists; ask the user or retry discovery.

Switch with `--reviewer`; rediscover with `--reset-reviewer`.

When a JSON fetch reports `reviewerSelection.confirmation_required: true`, stop before edits or re-review requests and run the prompt flow. `source: default_unconfirmed` is not a confirmed selection.

## Known reviewers

| Reviewer | Login | Trigger posted | Priority format | Reviews unprompted |
|---|---|---|---|---|
| Gemini Code Assist | `gemini-code-assist` | `@gemini-code-assist please review the latest changes.` | `![high]` alt text | yes |
| Codex | `chatgpt-codex-connector` | exactly `@codex review` | `![P0]`–`![P3]` badges | **no** |

Codex priorities normalize to the shared scale (P0→critical … P3→low), so `--min-severity` works unchanged. Reviewers using leading plain-text `[P0]`–`[P3]` prefixes are also normalized to the same scale; unknown formats remain `unknown` and are retained by default. Codex may put findings in the review body with no inline thread; the fetcher surfaces those and drops them when a newer Codex review supersedes them. Gemini's consumer app shut down 2026-07-17 — if a user picks Gemini and no threads ever arrive, say that instead of waiting out the timeout. Other discovered bots work, but the loop only re-asks them when a safe mention is supplied via `--review-trigger-mention`; never guess `@login` for an unknown bot.
