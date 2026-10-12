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
| CodeRabbit | `coderabbitai` | exactly `@coderabbitai review` | `Critical` / `Major` / `Minor` | yes |
| GitHub Copilot | `copilot-pull-request-reviewer` | none — a review request via the GitHub API (`request_rereview.py` does it) | overview `alt="High severity"` list, per head | **no** |

Codex priorities normalize to the shared scale (P0→critical … P3→low), so `--min-severity` works unchanged. Codex may put findings in the review body with no inline thread; the fetcher surfaces those and drops them when a newer Codex review supersedes them. Gemini's consumer app shut down 2026-07-17 — if a user picks Gemini and no threads ever arrive, say that instead of waiting out the timeout. Copilot has no comment trigger: `request_rereview.py` posts a reviewer request for `copilot-pull-request-reviewer[bot]` and reads the PR first, so it never double-asks. Its non-posting statuses: `review_pending` (a request is open; wait with `--wait --after <pending_since>` instead), `already_reviewed_head` (the current head already has a Copilot review; fetch, do not re-ask), `head_mismatch` (`--expect-head` differs from the PR head; refetch), `review_request_rejected` (`reason`: `permission` → the token cannot request reviewers, stop and tell the user; `rate_limited`/`transient` → retry once later; `validation`/`not_found` → stop). One status means unknown rather than unposted: `review_request_uncertain` (no HTTP status and no matching timeline event yet, so the POST may have landed) — do not ask again now; re-read the state in a minute and only request again if no request from this agent appears. A human's request, the initial request and any refused request cost no cap. On fetch, `loopStatus.cleanBlockedBy` is set when zero threads do **not** mean clean: `no_review_for_head` (request a review if none is pending, then wait), `overview_unparseable` (Copilot reviewed the head but its overview is unreadable — decide from the threads and the review, never report clean on this signal), `overview_reports_open_findings` (the head overview lists findings with no thread in this fetch — page limit or deleted thread; check the review). Findings whose threads were resolved, outdated or addressed without a push do not block: thread state is live, the overview is a snapshot. `reviewerOverview` carries Copilot's verdict (`approving`, `open_findings`, `covers_head`) and `reReviewCountComplete: false` means the cap count is a lower bound (timeline page full). Copilot shows as `Copilot` in the PR's reviewer list but as `copilot-pull-request-reviewer` on its comments; both resolve.

Unknown discovered bots work, but the loop only re-asks them when a safe mention is supplied via `--review-trigger-mention`; never guess `@login` for an unknown bot.
