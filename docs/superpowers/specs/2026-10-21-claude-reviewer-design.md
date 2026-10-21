# Claude Code as a First-Class Reviewer — Design

**Date:** 2026-10-21
**Status:** Implemented in the same change. Live facts below were read from
public PRs and the documented products; the custom-CI shape from the
requesting user's own organization.
**Problem:** Claude reviews GitHub PRs through the Claude GitHub App in three
ways, and the loop knew none of them: no vendor record, so discovery offered
the `claude` login with a guessed mention and no severity, no clean gate, and
no way to drive a CI job that posts Claude's review under `github-actions[bot]`.

Scope mirrors the Copilot work (`2026-10-07-copilot-reviewer-design.md`): one
vendor added, the `review_vendors` / `request_rereview.py` /
`fetch_gemini_threads.py` architecture kept, two new vendor flags instead of a
provider abstraction, one new selection option (`--reviewer-vendor`). Gemini,
Codex and Copilot behavior is unchanged except where a line says "all vendors".

## 1. Verified facts

### 1.1 Hosted Code Review (Claude GitHub App), live 2026-10-21

Observed on `anthropics/claude-agent-sdk-python#1418` and `#1422`.

| Fact | Value |
|---|---|
| GraphQL `author.login` on reviews / thread comments | `claude`, `__typename: Bot` |
| REST `user.login` | `claude[bot]` (app bot id 209825114; the action's `bot_name` default is `claude[bot]`) |
| Review per head | `state: COMMENTED`, `commit.oid` = the head reviewed; a push produced a second review of the new head 3 min later |
| Review body | prose summary, optional `<details><summary>Extended reasoning...</summary>`, trailer `<!-- bhrv:<16 hex> -->`; re-reviews add `**No new blocking issues.**`, `**Still open from earlier reviews** (N):` and a machine block `<!-- bughunter:open-items:v1 count=N` / `r<discussion id> <nit\|normal\|pre_existing> <line> <path>` / `-->` |
| Inline comments | open with the severity marker: `🔴 …` (Important), `🟡 nit (optional): …` / `🟡 (optional) …` (Nit), `🟣` (Pre-existing); optional `<details><summary>Why this was flagged</summary>` |
| Clean pass | a review body "**Nothing blocking.** … I found no bugs in this version." on the head; check run **Claude Code Review** titled "Code review found no issues", details end `<!-- bughunter-clean -->` / `<!-- bughunter-severity: {"nit":0,"normal":0,"pre_existing":0} -->` |
| Thread lifecycle | the next review resolves its own earlier threads it judges fixed (docs); humans resolved both #1418 threads before the push |
| Re-review | comment command `@claude review` as a top-level PR comment, first line, by a user with write access (bots' comments do not start reviews); a request while a review is running on the head does not start a second one; `@claude review` after a push gives a full review, a push-triggered one a narrower re-review |
| When reviews run | per-repository setting: once after PR creation, after every push, or manual |

Source for the documented rows: code.claude.com/docs/en/code-review and
/code-review-pull-requests, read 2026-10-21.

### 1.2 claude-code-action (`anthropics/claude-code-action@v1`)

| Fact | Value |
|---|---|
| Posting identity | the Claude GitHub App (`claude[bot]`) unless the workflow passes `github_token: ${{ secrets.GITHUB_TOKEN }}`, then `github-actions[bot]` |
| Review workflow (quick setup, docs example) | `prompt: "/code-review:code-review --comment …"` on `pull_request` opened/synchronize/ready_for_review/reopened; posts "an inline comment on each issue it finds or one summary comment when it finds none"; skips drafts and PRs that already have a comment from Claude |
| Inline comment tool | `mcp__github_inline_comment__create_inline_comment` — each comment is its own body-less review carrying `commit_id` |
| Clean note (skill text) | `No issues found. Checked for bugs and CLAUDE.md compliance.` via `gh pr comment` |
| Interactive trigger | `trigger_phrase` default `@claude` in an issue/PR comment |
| Tracking comment (interactive / `track_progress`) | created at start: `Claude Code is working… <img …>` + "I'll analyze this and get back to you.", edited in place |
| Severity | none in the skill's inline comments |

### 1.3 Custom CI job (the requesting organization, `qizsecurity/qiz#2885`)

`claude-code-review.yml`: `anthropics/claude-code-action@v1` with
`github_token: GITHUB_TOKEN` and an org skill; the review is written to a file
and posted by a later step with `gh pr comment`, so it is a **top-level PR
comment by `github-actions[bot]`**, no review, no thread. Two shapes seen on
one PR: `# Code Review: PR #N - …` with `## Findings` / `### Minor: <title>` /
`` **File**: `path:line` `` / `**Severity**: Minor (…)` / `## Verdict` /
`**APPROVED** ✅`, and `## Code review — PR #N` with `No issues found.` and
`**Verdict:** 0 Critical, 0 Warnings, 0 Suggestions — Approve`. Both end with
`<!-- review:complete -->` (the step appends a truncation banner instead when
that trailer is missing). The same file has an `interactive` job on
`issue_comment` containing `@claude`, so the loop's ping re-runs the review.

## 2. Vendor record (`review_vendors.py`)

- `ReviewVendor` gains `head_reviews: bool = False` (reviews carry the head
  they cover; the clean gate and the request-side dedupe apply) and
  `summary_comments: bool = False` (the verdict may be a top-level comment).
  `COPILOT` sets `head_reviews=True`; nothing else about it changes.
- `CLAUDE = ReviewVendor(login="claude", display_name="Claude Code",
  mention="@claude", rereview_phrase="@claude review", auto_reviews=False,
  head_reviews=True, summary_comments=True)`. Aliases: `claude`, `claude[bot]`,
  `@claude`, `claude-code`, `claude-code-review`.
- `auto_reviews=False`: whether a review runs unprompted is a repository
  setting the loop cannot read. Pinging first costs nothing on the hosted
  service (a request during a running review is dropped) and is the only way
  a manual-mode repository or an interactive-only action setup ever reviews.
  With `auto_reviews=True` those would wait out the whole timeout.
- `SUGGESTED_VENDOR` stays Codex (pinned by test).
- `CLAUDE_SUMMARY_SIGNAL_RE` names a finished Claude review in a comment:
  `<!-- bhrv:`, `<!-- bughunter`, `<!-- review:complete -->`, "Checked for
  bugs and CLAUDE.md compliance", "Code review found no issues".
  `CLAUDE_IN_PROGRESS_RE` ("Claude Code is working") excludes the action's
  tracking comment. `summary_review_vendor(body)` applies both.

## 3. Login binding (`--reviewer-vendor`)

A login the registry does not know can be bound to a known vendor for the
process: `review_vendors.bind_login(login, vendor)`; `vendor_for` consults the
bindings first. Invariants:

- **B1** `canonical_login` keeps a bound login's own spelling (minus `[bot]`),
  because review, thread and comment filters compare authors exactly and the
  reviews really carry that login. Copilot's alias canonicalization is
  unchanged.
- **B2** A vendor's own login cannot be bound to another vendor
  (`chatgpt-codex-connector` → `claude` is a `ValueError`, exit 2); naming
  the vendor whose login it already is binds nothing.
- **B3** The persisted reviewer record carries `vendor: "claude"` only when
  bound; `reviewer_resolver.bind_from_record` restores it in
  `reviewer_selection_state` (fetch) and `read_persisted_reviewer` (request).
  An unknown vendor name in an old record leaves the login unknown.
- **B4** `display_name_for` names both: `Claude Code (as github-actions)`.
- Discovery (`discover_candidates`) gains a fourth source, `comment`: a bot
  comment whose body carries a vendor's summary signal. The vendor's own
  login is a plain candidate; another login is offered with
  `vendor: <key>`, the vendor's trigger, and the `(as <login>)` display name.
  Another vendor's own login carrying the signal is skipped. The discovery
  query now reads `comments(last:100) { author body }`.
- The conftest resets bindings around every test; they are process state.

## 4. Fetch side (`fetch_gemini_threads.py`)

### 4.1 Severity (all vendors, additive)

`severity_in_text` is the one reader behind `thread_severity`: leading plain
`[P0]`–`[P3]`, Gemini image, Codex badge, then Claude's leading 🔴 → `high`,
🟡 / 🟣 → `low`, then `**Severity**: <word>` with `SEVERITY_WORDS`
(critical/blocker → critical; high/major/important → high; medium/moderate/
warning → medium; low/minor/nit/suggestion/trivial/info → low). A marker that
does not lead the comment is not a grade.

### 4.2 Overview per vendor

`parse_reviewer_overview(body, author)` dispatches: Copilot →
`parse_copilot_overview` (unchanged), Claude → `parse_claude_overview`, else
None. `copilot_overview_severities`, `latest_reviewer_overview` and
`reviewer_overview_for` go through it. `parse_claude_overview`:

- marker (`<!-- bhrv:` or `<!-- bughunter`) absent → None; **empty body →
  complete, 0 open** (the action's body-less inline-comment reviews are
  reviews of their head whose findings are the threads);
- `open_findings` = the open-items `count=N`, else 0: Claude's findings are
  its threads, which are live state, and the block lists what an earlier
  review left open; `findings` carry the `r<id>` discussion ids and the
  check run's severity words (`normal` → high, `nit`/`pre_existing` → low),
  so `annotate_reviewer_severity` grades threads exactly as for Copilot;
- `assessment` = first sentence of the first non-empty line, markdown and
  `<details>` stripped; `approving` from `CLAUDE_APPROVING_RE` ("no (new)
  (blocking) issues/bugs", "nothing blocking", "found no bugs", a
  `**Verdict:** … Approve` line, `**APPROVED**`). Informational, as for
  Copilot.

### 4.3 Summary comments

`summary_review_comments(pr, author)` — the author's comments carrying the
vendor's signal, for `summary_comments` vendors only.
`_current_summary_comment` is the newest one unless a later review by the
author supersedes it. From it:

- `parse_summary_comment(body)`: graded sections (`### <Grade>: <title>` with
  a `**File**: \`path:line\`` field or a blob link inside; a section ends at
  the next heading) or, failing that and absent approval, the code-review
  skill's numbered list (one finding per full-sha blob link, title = the
  item's last line). `open_findings` = number of findings, or 0 when the body
  approves, else **None / `complete: False`** so the agent reads it rather
  than trusting a guessed zero.
- `summary_comment_findings` → synthetic threads `pr-comment:<comment
  id>:<n>` (`isReviewBodyFinding` + `isSummaryCommentFinding`, path/line from
  the section, `reviewerSeverity` from the grade). Appended after
  `review_body_findings`; their ids join `known_finding_ids` so one filtered
  out by `--min-severity` reads as dispositioned, like a Copilot thread.
- `summary_comment_overview` → an overview record with `source: "comment"`,
  `commit: null`, `covers_head` = created at or after the head commit's
  `committedDate` (`commits(last:1)` added to the main query, all vendors);
  a payload without the date takes the newest summary as current, which the
  timestamp-anchored wait already guarantees for the loop's own cycles.
- `reviewer_overview_for` now applies to `head_reviews` vendors and returns
  the newer of the head-review overview and the summary overview.

### 4.4 Wait

`review_activity_fingerprint` counts the author's summary comments (after
`after_iso`) alongside reviews and threads; the tracking comment never counts.
Claude keeps the timestamp-anchored wait; head coverage is checked by the
clean gate on the following fetch (`no_review_for_head` → ping or wait again),
which is where the Copilot design put the decision too. `_latest_submitted_after`
reports the summary's time for the settle payload.

### 4.5 Clean gate

`clean_blocked_by(..., head_review_vendor=...)` (keyword renamed from
`review_request_vendor`; call sites and tests updated) applies to Copilot and
Claude with the Copilot rules unchanged: no head review or summary →
`no_review_for_head`; incomplete → `overview_unparseable`; open items with no
known thread or synthetic id → `overview_reports_open_findings`.
`review_request_vendor` remains, for the timeline-count pagination warnings
that are specific to review-request vendors.

## 5. Request side (`request_rereview.py`)

- Comment path, `head_reviews` vendors: `head_already_reviewed` reads the head
  and the paginated reviews; a standing (not PENDING/DISMISSED) vendor review
  of the head → `already_reviewed_head`, nothing posted, no stamp. An
  unreadable head or review list posts the ping as before (one extra ping at
  most; the hosted service drops a request during a running review). Codex
  and Gemini never take these reads (pinned). `--dry-run` skips the dedupe.
- `--reviewer-vendor` (needs `--reviewer-login`) binds for the run;
  `read_persisted_reviewer` restores a stored binding.
- The cap is unchanged: the agent's own `@claude` comments, counted by mention.

## 6. Behavior the user sees

| Setup | Cycle 0 | Re-review | Clean |
|---|---|---|---|
| Hosted Code Review | discovery from threads/review; ping `@claude review` if nothing yet (dropped if already reviewing) | ping; `already_reviewed_head` when a push-triggered review beat it | head review with the `bhrv` marker, zero threads, open items all known |
| Action `/code-review --comment` | same | ping (action's `@claude` trigger) | newer of: body-less head reviews with no open threads, or the "No issues found" comment posted after the head commit |
| CI job as `github-actions[bot]` | `--reviewer github-actions --reviewer-vendor claude`; summary findings are the threads | ping (interactive job) | a new summary with no findings / an approving verdict, posted after the head commit |

Known limits, stated in the docs: a repository running both the hosted
service and the action on the default `@claude` phrase starts both on one
ping (Anthropic's own advice is to change the action's `trigger_phrase`); a
summary comment whose shape the parser does not know is `overview_unparseable`
and is left to the agent; the action's own `/code-review` inline comments
carry no grade and stay `unknown`.

## 7. Tests

`tests/test_claude_review.py` (unit: vendor, binding, markers, both parsers,
summary findings, wait fingerprint, overview choice, discovery, request-side
dedupe and binding) and `tests/test_acceptance_claude_cycle.py` over
`tests/fixtures/claude_cycle.json` (hosted cycle: fetch → ping → head-anchored
re-fetch; clean / stale / unseen-open-item / dispositioned; the action's clean
note after body-less reviews; the tracking comment alone timing out; the CI
summary cycle under `github-actions` with the binding restored from the
record; a filtered summary finding not blocking clean; binding Codex's login
refused). The Copilot, Codex and Gemini suites pass unmodified apart from the
`head_review_vendor` keyword rename.
