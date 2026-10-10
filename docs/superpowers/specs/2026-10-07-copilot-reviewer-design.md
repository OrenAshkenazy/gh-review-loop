# Copilot as a First-Class Reviewer — Design

**Date:** 2026-10-07, amended 2026-10-10 (correctness review: P0 1–5, P1 6–10)
**Status:** Design amended; LV-1/2/3/5/6 answered by public-PR observation (§9). No remaining design gate. The exact POST under the user's token is verified on the first live run.
**Problem:** GitHub Copilot code review is listed in the README as a "configurable"
reviewer, but the loop cannot actually drive it: Copilot does not react to an
`@copilot` comment, so the only re-review trigger the loop knows how to post is
a no-op for it, and its severities are not parsed. A user whose repo has Copilot
reviewing on request (not on push) gets cycle 0 and nothing after.

Scope is unchanged from the approved direction: one vendor added, the existing
`review_vendors` / `request_rereview.py` / `fetch_gemini_threads.py` architecture
kept, no multi-reviewer aggregation, no new provider abstraction beyond a
`trigger_kind` on the vendor record. Gemini and Codex behavior is untouched
except where a line says "all vendors".

## 1. Verified facts (live, 2026-10-07, public PRs)

Observed on `block/schemabot#1669`, `haacked/dotfiles#235`, and the REST bot account:

| Fact | Value |
|---|---|
| GraphQL `author.login` on reviews / thread comments | `copilot-pull-request-reviewer`, `__typename: Bot` |
| REST `user.login` on reviews | `copilot-pull-request-reviewer[bot]` |
| REST timeline `requested_reviewer.login` | `Copilot` (type `Bot`) |
| GraphQL `ReviewRequestedEvent.requestedReviewer.login` | `copilot-pull-request-reviewer`; `actor.login` is the requester; `createdAt` present |
| Review `commit.oid` (GraphQL) / `commit_id` (REST) | present on every Copilot review; identifies the head it covered |
| Overview body | begins `<!-- ccr-overview-v2 -->`; heading `### 🟢 Approval recommended` or `### 🟡 Changes recommended`; `**0 open findings**` (clean) or `<summary><strong>N open findings</strong></summary>`; each finding is a list item with `<img … alt="High severity">` and a link `[title](#discussion_r<id>)` |
| Inline thread comments | plain prose, **no** severity marker |
| Thread comment `url` | `…/pull/N#discussion_r<id>` — the same id the overview links |
| Request API (documented) | `POST /repos/{o}/{r}/pulls/{n}/requested_reviewers` body `{"reviewers":["copilot-pull-request-reviewer[bot]"]}`; GitHub changelog 2026-10-02 announces REST/GraphQL support; community reports say it works with a user token and fails under GitHub Actions tokens |

What is **not** verified — and must be before the request path ships — is in §9.

## 2. Vendor record (`review_vendors.py`)

- `ReviewVendor` gains `trigger_kind: str = "comment"` (`"comment"` | `"review_request"`)
  and `request_login: str = ""`.
- `COPILOT = ReviewVendor(login="copilot-pull-request-reviewer", display_name="Copilot",
  mention="@copilot", rereview_phrase="", auto_reviews=False,
  trigger_kind="review_request", request_login="copilot-pull-request-reviewer[bot]")`.
- Aliases resolve `Copilot`, `copilot`, `@copilot`, `copilot-pull-request-reviewer`,
  `copilot-pull-request-reviewer[bot]` to `COPILOT` (the existing lowercase
  mention/login alias map already covers all five).
- `auto_reviews=False`. A repo ruleset may also review on push; the loop cannot read
  rulesets, so it never assumes it. The request state machine (§3) makes a redundant
  request impossible when a push-triggered review already covers the head.
- `SUGGESTED_VENDOR` stays Codex.

## 3. Request-side state machine (`request_rereview.py`)

Reads: one GraphQL query for state — `headRefOid`, `reviews(last:100) { author state
submittedAt commit { oid } }`, `reviewRequests(first:50) { requestedReviewer }` — and
REST `GET issues/{n}/timeline --paginate` for the cap (`review_requested` events:
`actor`, `requested_reviewer`, `created_at`). If any read fails → state `UNKNOWN`.
The POST itself stays REST (§1). **REST `requested_reviewers` is not used**: observed
live (§9, LV-3) it is empty while a Copilot request is pending; only GraphQL
`reviewRequests` lists the bot, and it clears once the review is submitted.

Derived facts:

- `head` — `headRefOid`. With `--expect-head <sha>` (the agent passes
  `git rev-parse HEAD` after its push), `head != sha` → status `head_mismatch`,
  nothing posted (the push has not landed; retry later).
- `completed_head` — a vendor review with `commit.oid == head` and `state != PENDING`.
- `pending` — `vendor_for(requestedReviewer.login) is vendor` for some node of
  `reviewRequests`. A `state: PENDING` vendor review, if ever visible, also counts as
  pending and never as completed.
- `pending_since` — `created_at` of the newest vendor `review_requested` event
  (any actor) with no later completed vendor review; `None` when not pending.
- `stale` — `pending` and `now - pending_since >= STALE_REQUEST_SECONDS` (1200).

States and the single transition each allows:

| State | Condition | Action | Status emitted |
|---|---|---|---|
| `COMPLETED_HEAD` | `completed_head` | none | `already_reviewed_head` (posted: false; "fetch instead of waiting") |
| `PENDING` | not completed, `pending`, not `stale` | none | `review_pending` (posted: false; carries `pending_since`; "wait, do not re-request") |
| `STALE_PENDING` | not completed, `pending`, `stale` | POST | success payload with `replaced_stale_request: true` |
| `ABSENT` | not completed, not pending | POST | success payload |
| `UNKNOWN` | a read failed | none | `uncountable_count` (existing status; nothing posted) |

Invariants:

- **I1** A POST happens only from `ABSENT` or `STALE_PENDING`. `PENDING` never POSTs.
- **I2** The cap check (§4) runs after state resolution and before the POST; it is
  skipped entirely in `COMPLETED_HEAD` / `PENDING` since nothing would be posted.
- **I3** `record_rereview_posted` (the local session-cycle stamp) fires only after a
  POST that returned HTTP 2xx, or after an uncertain failure that was confirmed
  posted (§6). Never on dry-run, dedupe, rejection, or cap stop.
- **I4** A `--review-trigger-mention` / `--phrase` given for a review_request vendor
  is ignored with a stderr warning; no comment is ever posted for this vendor.
- **I5** `created_at` in the success payload is local UTC taken immediately before
  the POST, kept for narration and for the comment-vendor-shaped SKILL flow. The
  Copilot waiter (§5) does **not** depend on it.

Dry run (`--dry-run`) performs the reads and reports the state it would act on
(`would_post: bool`, the state name) without posting.

## 4. Cap contract (cycle 0 free) — fetch side and request side

A counted re-review request is a `review_requested` event on the PR such that:

1. `requested_reviewer` resolves (via `review_vendors.vendor_for`) to the selected vendor;
2. `actor.login == agent_login` (the gh-authenticated user; humans never count);
3. `created_at > first_completed_review_at`, where `first_completed_review_at` is the
   earliest `submittedAt` of a non-PENDING vendor review on the PR. An event with no
   completed vendor review before it is the **initial request** (cycle 0) and is free.

Consequences that satisfy requirement 4 without special cases: a failed POST creates
no event; a dedupe creates no event; a human's request fails rule 2; the agent's
first request on a never-reviewed PR fails rule 3.

- **I6** Both scripts compute the count with one shared pure function,
  `reviewer_resolver.counted_review_requests(events, *, agent_login, vendor,
  first_completed_review_at) -> list[dict]`, over events normalized to
  `{created_at, actor_login, reviewer_login}` by `reviewer_resolver.normalize_request_event`
  (accepts the GraphQL node shape and the REST event shape). There is no second
  implementation.
- **I7** Request side reads the timeline with `--paginate`; the count is complete.
  Fetch side reads `timelineItems(last:100, itemTypes:[REVIEW_REQUESTED_EVENT])`
  in the main GraphQL query. When 100 nodes come back the count is reported as a
  lower bound: `loopStatus.reReviewCountComplete: false` plus a `pageLimitWarnings`
  entry, and `limitReached` is computed from the lower bound (it can only
  under-report, and the request side — the only writer — has the complete count).
- **I8** The cap is a function of PR history and `agent_login` only. The local
  `rereviews_posted` stamp drives the session-cycle *ordinal* (narration), never the
  cap, exactly as today. Session restarts therefore cannot change what the cap sees.
- `agent_login` unresolvable → request side refuses (`uncountable_count`, existing);
  fetch side falls back to counting all actors with the existing warning.
- Codex/Gemini: the mention-counting path is byte-for-byte unchanged, including the
  fact that today a Codex initial `@codex review` ping counts (a separate issue; out
  of scope here).

## 5. Fetch-side changes (`fetch_gemini_threads.py`)

### 5.1 GraphQL query (all vendors, additive)

`QUERY` gains `reviews.nodes.commit { oid }` and the `timelineItems` block above.
`REVIEWER_DISCOVERY_QUERY` gains `reviews { nodes { author } }` and
`reviewRequests { nodes { requestedReviewer } }`.

### 5.2 Waiter for review_request vendors

For a vendor with `trigger_kind == "review_request"`, both `wait_for_stable_review`
and `run_wait_chunk` replace the activity predicate:

- **ready** when a vendor review exists with `commit.oid == pullRequest.headRefOid`
  and `state != PENDING`. The quiet-period settle then applies as today (threads
  can trail the review by a few seconds).
- `after_iso` is **ignored** for this vendor (I5). This removes the same-second and
  clock-skew failure modes: the anchor is the head commit, not a timestamp.
- The heartbeat line names the head being waited on: `Waiting for Copilot review of
  <sha7>…`.
- Gemini and Codex keep the timestamp fingerprint path unchanged.

### 5.3 Clean criterion (requirement 5)

The loop may call a Copilot PR clean only when **all** hold:

1. zero actionable threads after the normal classification (unchanged rule);
2. a completed vendor review covers `headRefOid` (`reviewerOverview.covers_head: true`);
3. that review's overview parsed (`complete: true`) and reports `open_findings == 0`;
4. the normal verification profile and stopping criteria.

`approving` is informational. If (1) holds but (2) or (3) fails, the fetch emits
`loopStatus.cleanBlockedBy` ∈ {`no_review_for_head`, `overview_unparseable`,
`overview_reports_open_findings`} and SKILL.md treats that as "not clean: wait or
investigate", never as DONE. If the overview says `0 open findings` but actionable
threads exist, threads win (the overview is never a reason to drop a thread).

### 5.4 Severity from the overview (requirement 6)

- `parse_copilot_overview(body)` → `None` when the `<!-- ccr-overview` marker is
  absent. With the marker: `{assessment, approving, open_findings: int | None,
  findings: [{severity, discussion_id, title}], unlinked_findings: int, complete: bool}`.
  `complete` is false when neither a heading nor an open-findings count could be
  read; `open_findings` is `None` in that case.
- Severity for a thread: from the **newest** vendor review whose overview lists the
  thread's discussion id. Each review is read with its `commit.oid`; the annotation
  records provenance: `thread["reviewerSeverity"]` and
  `thread["reviewerSeveritySource"] = {"review_url", "commit"}`. Thread identity is
  the discussion id, which is stable across heads, so an older overview may still
  grade a thread that a newer overview no longer lists (the thread is still open).
- Parsing failure of any kind leaves the thread as it was: still listed, severity
  `unknown`, kept by `--min-severity` (unknown is kept by default) — exactly the
  behavior any unparsed bot has today. No thread is ever dropped because of the
  overview.
- Findings an overview lists without a `discussion_r` link are reported in
  `reviewerOverview.warnings`, not synthesized as threads.
- `reviewerOverview` (JSON, selected vendor's newest overview or `null`):
  `{assessment, approving, open_findings, complete, covers_head, commit, submitted_at,
  url, warnings}`. Human mode prints one `[loop] reviewer overview: …` line that
  includes `(covers head)` or `(stale: <sha7>)`.

### 5.5 Discovery sources

`discover_candidates` adds review authors (`source: "review"`) and pending requests
(`source: "requested"`) after thread authors (`source: "thread"`), first-seen order
within each; a login keeps the first source it was seen in. `--list-reviewers` prints
the source for non-thread candidates.

### 5.6 Integration with existing machinery (requirement 9)

- `thread_severity(thread)` returns `thread["reviewerSeverity"]` when present and
  valid, else the Gemini/Codex regex path. Every consumer (sort, min-severity filter,
  severity counts, header rendering, judge payload, metrics findings view, cluster
  severity) already routes through it.
- `finding_fingerprint` and `cluster_findings.pattern_signature` hash path + body only
  → carried-over identity and pattern clustering are unaffected by the annotation
  (pinned by test).
- `thread_render_fingerprint` hashes the rendered block, which includes the severity
  tag → a thread whose grade appears or changes renders **full** that cycle, not
  collapsed. Intended; pinned by test.
- Thread-state classification (`isResolved`, `isOutdated`, addressed-by-reply) does
  not read severity; untouched.
- The JSON `threads[*]` payload gains `reviewerSeverity` / `reviewerSeveritySource`.

## 6. Error taxonomy for the POST (requirement 8)

`gh api` failures are classified from the exit status and stderr (`HTTP <code>` and
GitHub's `message`):

| Class | Signal | `reason` | Retryable | Side effects |
|---|---|---|---|---|
| permission | HTTP 401 / 403 without rate-limit text | `permission` | no | none |
| validation | HTTP 422 | `validation` (message quoted: reviewer not eligible / Copilot not enabled) | no | none |
| not found | HTTP 404 | `not_found` | no | none |
| rate limited | HTTP 403/429 with `rate limit` in message | `rate_limited` | yes, later | none |
| transient | HTTP 5xx | `transient` | yes | none |
| uncertain | no HTTP status (timeout, connection reset, `gh` killed) | `uncertain` | **no automatic retry** | see below |

All map to one JSON status `review_request_rejected` with `reason`, `retryable`,
`http_status`, GitHub's message, `posted: false`. The loop stops and relays.

Uncertain outcome: the request may have landed. Before reporting, re-read the
timeline once; an agent `review_requested` event for the vendor with
`created_at >= requested_at` (local UTC; ≥ absorbs same-second) means it **did** post:
emit the normal success payload with `confirmed_after_uncertain: true` and stamp (I3).
Otherwise emit `review_request_rejected` / `uncertain` and tell the user to re-run,
which will land in `PENDING` or `ABSENT` correctly because the state machine re-reads
GitHub (**I9**: a re-run after any failure cannot double-post, because the only path
to a POST goes through state resolution).

## 7. Persisted record / selection state

- `trigger_for(login)` returns `None` for review_request vendors; `make_reviewer_record`
  and `reviewer_selection_state` emit `trigger_kind` and `request_login`. "Known
  vendor" checks use `vendor_for`, not `trigger_for`. A record persisted earlier with
  `review_trigger: "@copilot"` projects to `review_trigger: None` (the stale mention
  is dropped, pinned by test).

## 8. Tests (requirement 10) — in addition to the plan's unit tests

Each row is a named regression test; the owning file is in parentheses.

| # | Scenario | Expected |
|---|---|---|
| T1 | Pending: Copilot in GraphQL `reviewRequests`, no review for head, request 3 min old | `review_pending`, no POST, no stamp (test_request_rereview) |
| T2 | Pending review object (GraphQL): vendor review `state: PENDING` with `commit.oid == head`, no completed one | waiter stays not-ready; `reviewerOverview.covers_head` false (test_acceptance_copilot_cycle) |
| T3 | Stale pending: request 25 min old, no review for head | POST, `replaced_stale_request: true` (test_request_rereview) |
| T4 | Current-head dedupe: completed vendor review `commit_id == head` | `already_reviewed_head`, no POST, no cap read (test_request_rereview) |
| T5 | Stale review: completed vendor review for an older commit only, nothing pending | `ABSENT` → POST (test_request_rereview) |
| T6 | `--expect-head` mismatch | `head_mismatch`, no POST (test_request_rereview) |
| T7 | Fast same-second response: Copilot review `submittedAt == created_at`, `commit.oid == head` | waiter returns ready (test_acceptance_copilot_cycle) |
| T8 | Review arrives for the **previous** head after the request | waiter keeps waiting; a later head review ends it (test_acceptance_copilot_cycle) |
| T9 | Fetch timeline page full (100 events) | `reReviewCountComplete: false`, warning present, `limitReached` uses lower bound (test_fetch_gemini_threads) |
| T10 | Request-side paginated timeline (two REST pages) | all events counted (test_request_rereview, via `parse_paginated_json`) |
| T11 | Unparseable overview: marker present, no heading, no count | threads listed with `unknown`; `reviewerOverview.complete == false`; `cleanBlockedBy == overview_unparseable` when no threads (test_copilot_overview, acceptance) |
| T12 | Initial-review cap exemption: agent requested before any completed review, then re-requested once after | fetch and request counts both `== 1` (test_reviewer_resolver for the shared function; acceptance for both call sites) |
| T13 | Human request after first review | not counted (shared function test) |
| T14 | Failed POST (422) | `review_request_rejected`, `reason: validation`, no stamp; subsequent run re-resolves state (test_request_rereview) |
| T15 | Uncertain failure, timeline shows the agent's event at `>= requested_at` | success payload, `confirmed_after_uncertain: true`, stamped once (test_request_rereview) |
| T16 | Uncertain failure, timeline shows nothing | `reason: uncertain`, `retryable: false`, no stamp (test_request_rereview) |
| T17 | Rate limit 403 with "rate limit" message | `reason: rate_limited`, `retryable: true` (test_request_rereview) |
| T18 | Overview says `0 open findings`, two unresolved threads exist | threads returned, not clean (acceptance) |
| T19 | Zero threads, approving overview for a **stale** commit | `cleanBlockedBy: no_review_for_head` (acceptance) |
| T20 | Annotation does not change `finding_fingerprint` or `pattern_signature`; does change `thread_render_fingerprint` (test_copilot_overview) |
| T21 | Regrade: older overview High, newer overview Low for same discussion id, both recorded with their commits | `low`, `reviewerSeveritySource.commit` is the newer commit (test_copilot_overview) |
| T22 | Codex acceptance and Gemini fixtures unchanged | existing suites pass unmodified |

## 9. Live GitHub validation (requirements 1–3)

Public PRs are read-only for us, but Copilot's behavior there is observable: request
events, review commits, pending requests, and timings. The table records what the
2026-10-10 scan established. The one thing observation cannot show — that our
exact POST body under the user's token is accepted — is checked on the first live
run (plan Task 9) and would fail loudly as `review_request_rejected`.

| # | Question | Pass criterion | Result (2026-10-10, public-PR observation) |
|---|---|---|---|
| LV-1 | Does a programmatic review request start a **second** review after a completed first review **and a new push**? | New vendor review with `commit.oid == new head`, `state != PENDING`, after a `review_requested` event by the caller | **Observed** on 11 of 25 recent public PRs: repeated request events by one actor, each followed by a Copilot review on the new head (e.g. `bytechefhq/bytechef#6141`: 9 requests → 9 reviews on 9 distinct commits; `AreAArseth/git-subrepo#6`: 5 → 4 so far). `vanillagreencom/kendex#4656` requests were made by `github-actions`, i.e. programmatically, and produced reviews. **Residual**: our exact POST body under the user's token — verified on the first live run (Task 9), no longer a design gate |
| LV-2 | Same POST on an **unchanged**, already-reviewed head | Record outcome | **Observed**: produces another review. `Portocolom-Studio/potocolom#713`: two requests 13 s apart on head `07f149b` → two reviews on `07f149b`; `yeelam-gordon/intelligent-terminal#44`: four reviews on `17432b7`. GitHub does not dedupe server-side → `COMPLETED_HEAD` must be enforced client-side (§3 does) |
| LV-3 | What signals "review in progress"? | Observable signal during the review window | **Observed** on 5 PRs probed 60–120 s after a request and before the review: GraphQL `reviewRequests` lists `copilot-pull-request-reviewer`; REST `requested_reviewers` is `[]` in all five. After the review lands, `reviewRequests` is empty (`haacked/dotfiles#235`, `block/schemabot#1669`). No `state: PENDING` review object was visible. → §3 uses GraphQL `reviewRequests` |
| LV-4 | Review `commit.oid` ≠ head at request time? | Record | Not observed in the sample; each review's commit matched a head the PR had. Waiter anchors on the current head regardless (§5.2) |
| LV-5 | Request → review latency | Sanity for `STALE_REQUEST_SECONDS` | **Observed** 4 s (`potocolom#713`) to ~8 min (`bytechef#6141` 09:37:33 → 09:45:01). 1200 s default stands |
| LV-6 | POST while a request is already pending | Record | **Observed** (`potocolom#713`): second request while the first was pending → **two** reviews. Confirms `PENDING` must never POST (I1) |

Raw query output is kept at `docs/superpowers/specs/evidence/2026-10-10-copilot-live-scan.txt`.

Protocol (owner runs, or the agent after SSO), on PR 37:

```bash
R=qizsecurity/k8s-deploy; N=37
gh api repos/$R/pulls/$N --jq '{head:.head.sha, requested:[.requested_reviewers[].login]}'
gh api --paginate repos/$R/pulls/$N/reviews --jq '.[]|select(.user.type=="Bot")|{login:.user.login,state,commit:.commit_id[0:7],at:.submitted_at}'
# push a trivial commit to the PR branch, then:
date -u +%Y-%m-%dT%H:%M:%SZ
gh api -X POST repos/$R/pulls/$N/requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]' --jq '{requested:[.requested_reviewers[].login]}'
# every 30s for up to 10 min (LV-3 while it runs, LV-1 at the end):
gh api repos/$R/pulls/$N --jq '{head:.head.sha[0:7], requested:[.requested_reviewers[].login]}'
gh api graphql -f query='query{repository(owner:"qizsecurity",name:"k8s-deploy"){pullRequest(number:37){headRefOid reviews(last:5){nodes{author{login} state submittedAt commit{abbreviatedOid}}} timelineItems(last:10,itemTypes:[REVIEW_REQUESTED_EVENT]){nodes{... on ReviewRequestedEvent{createdAt actor{login} requestedReviewer{... on Bot{login}}}}}}}}'
# LV-2: repeat the POST without pushing; LV-6: POST twice within 10s.
```

Paste the outputs into the PR description; they are the evidence for LV-1..6.

## 10. Decisions amended since the grilling session

| Grilling decision | Amendment | Why |
|---|---|---|
| Q5 "stale pending request → re-request anyway" | `PENDING` never POSTs; only `STALE_PENDING` (≥ 20 min) does | requirement 2 |
| Q11/Q23 "approving + 0 findings = early DONE" | Overview corroborates only; clean needs a completed head review with a parsed overview reporting 0 and zero threads; `cleanBlockedBy` otherwise | requirement 5 |
| Q17 "no fetch query change" | `reviews.nodes.commit.oid` and `timelineItems` added to the main query | requirements 3, 7 |
| Q18 "wait unchanged" | Copilot waiter anchors on `commit.oid == headRefOid`, ignores `after_iso`; Gemini/Codex unchanged | requirements 1, 3; same-second case |
| Q19/Q4 "count events by agent" | plus "after first completed review" and one shared counting function for both scripts | requirements 4, 7 |
| Q10 "one rejected status" | one status, classified `reason` + `retryable`; uncertain outcomes reconciled against the timeline before reporting | requirement 8 |
| Q20 "local timestamp as REREVIEW_AT" | kept for narration only; no correctness depends on it | requirement 3 |

## 11. Docs

README table row and severity caveat; `references/reviewer-selection.md` row plus the
statuses `review_pending`, `already_reviewed_head`, `head_mismatch`,
`review_request_rejected`; SKILL.md step 4 "(Codex, Copilot)", step 6 clean criterion
pointer, step 8 status list. All within existing budgets (no budget raise).

## 12. Out of scope

Minimizing outdated overviews, `--review-effort`, body-only (suppressed) Copilot
findings as synthetic threads, inferring `auto_reviews` from rulesets, head-oid
checks for Gemini/Codex waits, fixing the Codex initial-ping cap accounting.
