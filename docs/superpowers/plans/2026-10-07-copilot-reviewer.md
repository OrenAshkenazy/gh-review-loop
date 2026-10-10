# Copilot First-Class Reviewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GitHub Copilot code review a built-in reviewer the loop can drive end to end: discover it, request and re-request its review through the GitHub review-request API, count those requests toward the cap, and read its per-finding severities from the review overview.

**Architecture:** The vendor registry grows a `trigger_kind` so one vendor can be re-reviewed by a review request instead of a comment. `request_rereview.py` gets a second posting path for that kind (REST `requested_reviewers`, REST timeline for the cap, REST reviews for head dedupe). The fetch script learns to count `ReviewRequestedEvent`s, to parse Copilot's overview Markdown into per-thread severities through the existing `thread_severity` chokepoint, to emit a `reviewerOverview` block, and to discover reviewers from review authors and pending requests. Docs follow.

**Tech Stack:** Python 3.9+ stdlib only (same as all existing scripts), pytest at `/opt/homebrew/bin/pytest`.

**Spec:** `docs/superpowers/specs/2026-10-07-copilot-reviewer-design.md`

## Global Constraints

- Scripts live in `plugins/gh-review-loop/skills/gh-review-loop/scripts/` (abbreviated `$S` below). Run them from the repo root.
- Every touched file already has `from __future__ import annotations`; keep subscripted generics (`dict[str, Any]`, `str | None`). Never "fix" them to bare `dict`.
- Run tests with `/opt/homebrew/bin/pytest`; bare `python3 -m pytest` does not work here.
- Tests are hermetic: `tests/conftest.py` raises if any test runs the real `gh`. Pass an explicit `runner=` or monkeypatch the helper.
- Per-user state: tests get `GGRL_STATE_DIR` pointed at a temp dir automatically; set it explicitly when a test must read the state file back.
- JSON mode stdout is machine-only (enforced by `tests/test_json_stdout_discipline.py`). Human-facing lines go to stdout only in human mode; warnings go to stderr.
- Doc budgets (`tests/test_doc_budgets.py` against `tests/budgets.json`): SKILL.md ≤ 15000 bytes / 150 lines (currently 14778 / 143); each reference file ≤ 7000 bytes. Do not raise a budget in this PR.
- Branch: `feat/copilot-reviewer`. Conventional commits. Every commit ends with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01B7mNKza2aXXa8qspkmhbj4
  ```

## Review Focus

Inputs the spec implies but that are easy to get wrong; each has a pinned test in the task named.

1. REST timeline reports the requested reviewer as login `Copilot` (type Bot), not the GraphQL login. The cap counter must resolve it to the same vendor. → Task 3 (`test_count_agent_review_requests_paginated_rest`).
2. A clean overview writes `**0 open findings**` (Markdown bold); a non-clean one writes `<summary><strong>2 open findings</strong></summary>`. Both must parse. → Task 6 (`test_clean_overview_is_approving_with_zero_findings`, `test_parses_assessment_findings_and_count`).
3. A reviewer record persisted *before* this change by the "configurable" path may carry `review_trigger: "@copilot"`. It must still be re-reviewed by request, never by comment. → Task 3 (`test_persisted_copilot_with_a_stale_mention_trigger_still_requests_a_review`).
4. A human clicking "Request review" in the GitHub UI creates the same timeline event the agent does. Only the agent's events may consume the cap. → Task 8 (acceptance asserts `reReviewRequests == 0` after the human's request).
5. GitHub rejects the request (Copilot not enabled, caller lacks write, PR closed) with a 422. The loop must stop with a controlled status and post nothing else. → Task 3 (`test_rejected_422_is_validation_and_not_stamped`).

---

### Task 1: Copilot vendor record

**Files:**
- Modify: `$S/review_vendors.py`
- Test: `tests/test_review_vendors.py`

**Interfaces:**
- Produces: `ReviewVendor.trigger_kind: str` (`"comment"` | `"review_request"`), `ReviewVendor.request_login: str`, module constant `COPILOT`, and `vendor_for("Copilot") is COPILOT`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_vendors.py`:

```python
def test_copilot_vendor_resolves_from_every_login_spelling():
    """GraphQL says copilot-pull-request-reviewer, REST reviews say ...[bot],
    the REST timeline says Copilot, and the handle is @copilot."""
    for name in (
        "copilot-pull-request-reviewer",
        "copilot-pull-request-reviewer[bot]",
        "Copilot",
        "copilot",
        "@copilot",
    ):
        assert review_vendors.vendor_for(name) is review_vendors.COPILOT, name


def test_copilot_is_re_reviewed_by_request_not_by_comment():
    copilot = review_vendors.COPILOT

    assert copilot.display_name == "Copilot"
    assert copilot.trigger_kind == "review_request"
    assert copilot.request_login == "copilot-pull-request-reviewer[bot]"
    assert copilot.rereview_phrase == ""
    assert copilot.auto_reviews is False


def test_comment_vendors_default_to_the_comment_trigger_kind():
    assert review_vendors.CODEX.trigger_kind == "comment"
    assert review_vendors.GEMINI.trigger_kind == "comment"
    assert review_vendors.CODEX.request_login == ""


def test_adding_copilot_did_not_change_the_suggested_vendor():
    assert review_vendors.SUGGESTED_VENDOR is review_vendors.CODEX
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_review_vendors.py -v`
Expected: 4 new tests FAIL (`AttributeError: ... has no attribute 'COPILOT'` / `trigger_kind`).

- [ ] **Step 3: Implement**

In `$S/review_vendors.py`, extend the dataclass (keep existing fields and their order; new fields have defaults):

```python
@dataclass(frozen=True)
class ReviewVendor:
    login: str
    display_name: str
    mention: str
    rereview_phrase: str
    auto_reviews: bool
    consumer_tier_retired: bool = False
    tier_note: str = ""
    # How the loop asks this vendor for another look. "comment" vendors read
    # an @mention posted as a PR comment (``rereview_phrase``). A
    # "review_request" vendor ignores comments and is re-reviewed through
    # GitHub's requested-reviewers API using ``request_login``.
    trigger_kind: str = "comment"
    request_login: str = ""
```

After the `CODEX` record add:

```python
# Copilot does not react to an @copilot comment. It reviews when it is added
# as a requested reviewer — the same REST call for the first request and every
# re-request (official since GitHub's 2026-10-02 changelog). Its login differs
# by API: GraphQL ``copilot-pull-request-reviewer``, REST reviews
# ``copilot-pull-request-reviewer[bot]``, REST timeline ``Copilot``. A repo
# ruleset may also make it review on push; the loop cannot read rulesets, so
# it always re-requests and lets request_rereview's head dedupe skip the
# redundant ones.
COPILOT = ReviewVendor(
    login="copilot-pull-request-reviewer",
    display_name="Copilot",
    mention="@copilot",
    rereview_phrase="",
    auto_reviews=False,
    trigger_kind="review_request",
    request_login="copilot-pull-request-reviewer[bot]",
)

KNOWN_VENDORS = {vendor.login: vendor for vendor in (GEMINI, CODEX, COPILOT)}
```

(Replace the existing `KNOWN_VENDORS = {... (GEMINI, CODEX)}` line.) The `_ALIASES` map already lowercases mention and login, so `Copilot` resolves through `copilot`; no further change.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_review_vendors.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-review-loop/skills/gh-review-loop/scripts/review_vendors.py tests/test_review_vendors.py
git commit -m "feat(vendors): add Copilot as a review-request vendor"
```

---

### Task 2: Resolver and selection-state helpers for the new kind

**Files:**
- Modify: `$S/reviewer_resolver.py` (`Candidate`, `trigger_for`, `phrase_for`, `make_reviewer_record`; add `trigger_kind_for`, `request_login_for`)
- Modify: `$S/fetch_gemini_threads.py` `reviewer_selection_state` (~line 1224)
- Test: `tests/test_reviewer_resolver.py`, `tests/test_fetch_gemini_threads.py`

**Interfaces:**
- Produces: `reviewer_resolver.trigger_kind_for(login) -> str`, `reviewer_resolver.request_login_for(login) -> str | None`; `Candidate(login, display_name, review_trigger, trigger_kind="comment", source="thread")` with those two keys in `to_dict()`; reviewer records and `reviewerSelection` payloads carry `trigger_kind` and `request_login`.
- Consumes: Task 1's `COPILOT`, `trigger_kind`, `request_login`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reviewer_resolver.py`:

```python
COPILOT = "copilot-pull-request-reviewer"


def test_copilot_has_no_mention_trigger_but_a_request_login():
    assert reviewer_resolver.trigger_for(COPILOT) is None
    assert reviewer_resolver.phrase_for(COPILOT) is None
    assert reviewer_resolver.trigger_kind_for(COPILOT) == "review_request"
    assert reviewer_resolver.request_login_for(COPILOT) == "copilot-pull-request-reviewer[bot]"


def test_unknown_and_comment_vendors_keep_the_comment_kind():
    assert reviewer_resolver.trigger_kind_for("coderabbitai") == "comment"
    assert reviewer_resolver.request_login_for("coderabbitai") is None
    assert reviewer_resolver.trigger_kind_for("chatgpt-codex-connector") == "comment"
    assert reviewer_resolver.trigger_for("chatgpt-codex-connector") == "@codex"


def test_copilot_record_carries_kind_and_request_login():
    record = reviewer_resolver.make_reviewer_record(COPILOT, source="confirmed")

    assert record["display_name"] == "Copilot"
    assert record["review_trigger"] is None
    assert record["trigger_kind"] == "review_request"
    assert record["request_login"] == "copilot-pull-request-reviewer[bot]"
    assert record["auto_reviews"] is False


def test_codex_record_still_carries_its_mention_and_the_comment_kind():
    record = reviewer_resolver.make_reviewer_record("chatgpt-codex-connector", source="confirmed")

    assert record["review_trigger"] == "@codex"
    assert record["trigger_kind"] == "comment"
    assert record["request_login"] is None


def test_candidate_dict_carries_kind_and_source():
    candidate = reviewer_resolver.Candidate(
        login=COPILOT,
        display_name="Copilot",
        review_trigger=None,
        trigger_kind="review_request",
        source="requested",
    )

    assert candidate.to_dict() == {
        "login": COPILOT,
        "display_name": "Copilot",
        "review_trigger": None,
        "trigger_kind": "review_request",
        "source": "requested",
    }
```

Append to `tests/test_fetch_gemini_threads.py` (it already imports `fetch_gemini_threads`; check the top of the file for the alias it uses — the existing tests import names directly via `from fetch_gemini_threads import (...)`; add `reviewer_selection_state` to that import list):

```python
class TestReviewRequestSelectionState:
    def test_copilot_selection_keeps_its_display_name_without_a_mention_trigger(self):
        state = reviewer_selection_state(
            {"login": "copilot-pull-request-reviewer", "display_name": "Copilot Pull Request Reviewer"},
            source="persisted",
        )

        assert state["display_name"] == "Copilot"
        assert state["review_trigger"] is None
        assert state["trigger_kind"] == "review_request"
        assert state["request_login"] == "copilot-pull-request-reviewer[bot]"
        assert state["auto_reviews"] is False
        assert state["configured"] is True

    def test_a_stale_persisted_mention_for_copilot_is_dropped(self):
        """Records saved by the old 'configurable' path carry review_trigger
        '@copilot'. A comment never triggers Copilot, so the projection must
        not advertise one."""
        state = reviewer_selection_state(
            {"login": "copilot-pull-request-reviewer", "review_trigger": "@copilot"},
            source="persisted",
        )

        assert state["review_trigger"] is None
        assert state["trigger_kind"] == "review_request"

    def test_codex_selection_is_unchanged(self):
        state = reviewer_selection_state({"login": "chatgpt-codex-connector"}, source="persisted")

        assert state["review_trigger"] == "@codex"
        assert state["trigger_kind"] == "comment"
        assert state["request_login"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_reviewer_resolver.py tests/test_fetch_gemini_threads.py -k "copilot or comment_kind or candidate_dict or codex_record or SelectionState" -v`
Expected: FAIL (`AttributeError: trigger_kind_for`, unexpected keyword `trigger_kind`, missing keys).

- [ ] **Step 3: Implement the resolver changes**

In `$S/reviewer_resolver.py`:

```python
@dataclass(frozen=True)
class Candidate:
    login: str
    display_name: str
    review_trigger: str | None
    trigger_kind: str = "comment"
    # Where discovery saw this reviewer: "thread" (inline review comment),
    # "review" (a submitted review with no inline comments), or "requested"
    # (a pending review request that has not produced a review yet).
    source: str = "thread"

    def to_dict(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "display_name": self.display_name,
            "review_trigger": self.review_trigger,
            "trigger_kind": self.trigger_kind,
            "source": self.source,
        }
```

Replace `trigger_for` and `phrase_for`, and add the two new helpers right after them:

```python
def trigger_for(login: str) -> str | None:
    """The @mention that re-reviews this vendor, or None when there is none.

    A review_request vendor has a handle (``@copilot``) but no comment the loop
    could post to trigger it, so it has no trigger here.
    """
    vendor = review_vendors.vendor_for(login)
    if vendor is None or vendor.trigger_kind != "comment":
        return None
    return vendor.mention


def phrase_for(login: str) -> str | None:
    """Return the exact re-review comment a known vendor accepts.

    Vendors differ: Gemini reads any sentence that mentions it, Codex matches
    ``@codex review`` exactly. Unknown reviewers have no phrase, so callers
    fall back to the generic mention-plus-sentence form. Review-request
    vendors have no phrase at all.
    """
    vendor = review_vendors.vendor_for(login)
    if vendor is None or vendor.trigger_kind != "comment":
        return None
    return vendor.rereview_phrase


def trigger_kind_for(login: str) -> str:
    """"comment" (post an @mention) or "review_request" (requested-reviewers API)."""
    vendor = review_vendors.vendor_for(login)
    return vendor.trigger_kind if vendor else "comment"


def request_login_for(login: str) -> str | None:
    """The login to pass to the requested-reviewers API, for review_request vendors."""
    vendor = review_vendors.vendor_for(login)
    if vendor is None or vendor.trigger_kind != "review_request":
        return None
    return vendor.request_login
```

Extend `make_reviewer_record`'s returned dict with two keys (keep everything else):

```python
    return {
        "login": login,
        "display_name": display_name or display_name_for(login),
        "review_trigger": review_trigger if review_trigger is not None else trigger_for(login),
        "trigger_kind": trigger_kind_for(login),
        "request_login": request_login_for(login),
        "auto_reviews": auto_reviews(login),
        "source": source,
        "selected_at": selected_at or now_iso(),
    }
```

- [ ] **Step 4: Implement the selection-state projection**

In `$S/fetch_gemini_threads.py`, replace the body of `reviewer_selection_state` (keep the docstring):

```python
    login = record["login"]
    known_display_name = reviewer_resolver.display_name_for(login)
    display_name = record.get("display_name") or known_display_name
    if review_vendors.vendor_for(login):
        display_name = known_display_name
    trigger_kind = reviewer_resolver.trigger_kind_for(login)
    # A record saved before the vendor was known may carry a mention for a
    # vendor that is re-reviewed by request; a comment never triggers it, so
    # the mention is dropped rather than advertised.
    review_trigger = None
    if trigger_kind == "comment":
        review_trigger = record.get("review_trigger") or reviewer_resolver.trigger_for(login)
    return {
        "login": login,
        "display_name": display_name,
        "review_trigger": review_trigger,
        "trigger_kind": trigger_kind,
        "request_login": reviewer_resolver.request_login_for(login),
        "auto_reviews": reviewer_resolver.auto_reviews(login),
        "source": source,
        "confirmation_required": source in {"default_unconfirmed", "none_configured"},
        # True whenever an actual choice backs the selection. A suggestion the
        # user has not accepted is not a configuration.
        "configured": source not in {"default_unconfirmed", "none_configured"},
        "candidates_partial": False,
    }
```

`review_vendors` is already imported at the top of the fetch script.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_reviewer_resolver.py tests/test_fetch_gemini_threads.py tests/test_acceptance_codex_cycle.py -v`
Expected: all PASS (the Codex acceptance test still sees `review_trigger == "@codex"`).

- [ ] **Step 6: Commit**

```bash
git add plugins/gh-review-loop/skills/gh-review-loop/scripts/reviewer_resolver.py plugins/gh-review-loop/skills/gh-review-loop/scripts/fetch_gemini_threads.py tests/test_reviewer_resolver.py tests/test_fetch_gemini_threads.py
git commit -m "feat(resolver): expose trigger_kind and request_login for review-request vendors"
```

---

### Task 3: Shared request counting + review-request posting path (`request_rereview.py`)

Spec: §3 (state machine, I1–I5), §4 (cap), §6 (error taxonomy, I9).

**Files:**
- Modify: `$S/reviewer_resolver.py` (shared counting helpers)
- Modify: `$S/request_rereview.py`
- Test: `tests/test_reviewer_resolver.py`, `tests/test_request_rereview.py`

**Interfaces:**
- Produces (reviewer_resolver):
  - `normalize_request_event(event) -> dict | None` → `{created_at, actor_login, reviewer_login}`; accepts a GraphQL `ReviewRequestedEvent` node (`createdAt`, `actor.login`, `requestedReviewer.login`) or a REST timeline event (`event == "review_requested"`, `created_at`, `actor.login`, `requested_reviewer.login`); `None` for anything else (team requests, other event types, non-dicts).
  - `first_completed_review_at(reviews, vendor) -> str | None` — earliest `submittedAt`/`submitted_at` of a non-`PENDING` review whose author (`author.login` or `user.login`) resolves to `vendor`.
  - `counted_review_requests(events, *, agent_login, vendor, first_completed_review_at) -> list[dict]` — normalized events where the reviewer resolves to `vendor`, `actor_login == agent_login` (skipped when `agent_login is None`), and `created_at > first_completed_review_at`; empty when `first_completed_review_at` is `None` (nothing is a re-review before the first review).
- Produces (request_rereview):
  - `STALE_REQUEST_SECONDS = 1200`
  - `STATE_QUERY` (GraphQL): `headRefOid`, `reviews(last:100){author{login} state submittedAt commit{oid}}`, `reviewRequests(first:50){requestedReviewer{__typename ... on Bot{login} ... on User{login}}}`, `timelineItems(last:100, itemTypes:[REVIEW_REQUESTED_EVENT]){nodes{__typename ... on ReviewRequestedEvent{createdAt actor{login} requestedReviewer{__typename ... on Bot{login} ... on User{login}}}}}`
  - `request_state(repo, pr, vendor, runner=None, now=None) -> dict | None` → `{"state": "COMPLETED_HEAD"|"PENDING"|"STALE_PENDING"|"ABSENT", "head": sha, "pending_since": iso|None, "first_completed_review_at": iso|None}`; `None` when the query fails (`UNKNOWN`). `pending_since` = newest vendor request event with no completed vendor review submitted after it; a pending request with no event → `PENDING` (age unknown is treated as fresh).
  - `count_agent_review_requests(repo, pr, vendor, agent_login, first_completed_review_at, runner=None) -> int | None` — REST `GET issues/{n}/timeline --paginate` → `counted_review_requests`; `None` on failure or when `agent_login` is falsy.
  - `classify_gh_failure(stderr: str) -> tuple[str, int | None, bool]` → `(reason, http_status, retryable)` per spec §6 table; no `HTTP <code>` in stderr → `("uncertain", None, False)`.
  - `class ReviewRequestRejected(RuntimeError)` with attributes `reason`, `http_status`, `retryable`, `detail`.
  - `request_review(repo, pr, request_login, runner=None, dry_run=False) -> dict` — REST POST; raises `ReviewRequestRejected` on non-zero exit; success `{created_at (local UTC pre-POST), repo, pr, kind: "review_request", reviewer}`.
  - `confirm_posted_after_uncertain(repo, pr, vendor, agent_login, requested_at, runner=None) -> bool` — REST timeline re-read; any agent event for the vendor with `created_at >= requested_at`.
  - payload builders: `already_reviewed_payload`, `review_pending_payload`, `head_mismatch_payload`, `rejected_payload(repo, pr, reviewer, exc)`; JSON statuses `already_reviewed_head`, `review_pending`, `head_mismatch`, `review_request_rejected`.
  - `run_review_request(args, vendor) -> int`; `main()` routes to it when `vendor_for(resolve_reviewer_login(...)).trigger_kind == "review_request"`.
  - New CLI flag `--expect-head <sha>`.
- Consumes: Task 1 `COPILOT`, Task 2 `trigger_kind_for`.

**Decision flow in `run_review_request`** (each line is a test):
1. `--phrase`/`--reviewer-mention` present → stderr warning "ignoring", continue.
2. `state = request_state(...)`; `None` → `uncountable_count` payload, exit 0.
3. `--expect-head` set and `!= state["head"]` → `head_mismatch`, exit 0.
4. `COMPLETED_HEAD` → `already_reviewed_head`, exit 0 (no cap read).
5. `PENDING` → `review_pending` (carries `pending_since`), exit 0 (no cap read).
6. Unless `--no-cap-check`: `used = count_agent_review_requests(..., state["first_completed_review_at"])`; `None` → `uncountable_count`; `>= cap` → `capped`.
7. `request_review(..., dry_run=args.dry_run)`. Dry run → preview payload `{dry_run: true, posted: false, would_post: true, state}` and exit 0, no stamp.
8. `ReviewRequestRejected` with `reason == "uncertain"` → `confirm_posted_after_uncertain(...)`; true → success payload `+ confirmed_after_uncertain: true` and stamp; false → `review_request_rejected` (`reason: uncertain`, `retryable: false`).
9. Other `ReviewRequestRejected` → `review_request_rejected` with `reason`, `http_status`, `retryable`, GitHub message; exit 0; no stamp.
10. Success → `record_rereview_posted`, payload `+ state` (`ABSENT` or `STALE_PENDING`) and `replaced_stale_request: state == "STALE_PENDING"`.

**Tests** (names fixed; each RED before GREEN):
- resolver: `test_normalize_request_event_accepts_graphql_and_rest_shapes`, `test_normalize_request_event_rejects_team_requests_and_other_events`, `test_first_completed_review_at_ignores_pending_and_other_authors`, `test_counted_requests_exclude_initial_human_and_other_vendor` (T12/T13: agent event before first review not counted, human after not counted, agent after counted), `test_counted_requests_is_empty_before_any_completed_review`.
- request_rereview helpers: `test_request_state_completed_head` (T4), `test_request_state_pending_fresh` (T1: request 3 min old), `test_request_state_stale_pending` (T3: 25 min), `test_request_state_absent_with_stale_review` (T5), `test_request_state_none_when_query_fails`, `test_count_agent_review_requests_paginated_rest` (T10: two concatenated JSON pages via `parse_paginated_json`; REST `requested_reviewer.login == "Copilot"`), `test_classify_gh_failure_table` (401/403/404/422/429/403+rate limit/500/no-status), `test_request_review_posts_to_requested_reviewers`, `test_request_review_rejection_carries_reason`, `test_request_review_dry_run_calls_nothing`, `test_confirm_posted_after_uncertain_same_second_counts`.
- main: `test_copilot_login_requests_a_review_instead_of_commenting`, `test_persisted_copilot_with_a_stale_mention_trigger_still_requests_a_review`, `test_pending_state_posts_nothing_and_skips_the_cap`, `test_already_reviewed_head_posts_nothing`, `test_head_mismatch_posts_nothing` (T6), `test_stale_pending_reposts_and_flags_it` (T3), `test_rejected_422_is_validation_and_not_stamped` (T14), `test_uncertain_failure_confirmed_by_timeline_is_a_success_and_stamped_once` (T15), `test_uncertain_failure_without_event_is_not_retryable_and_not_stamped` (T16), `test_rate_limit_is_retryable` (T17), `test_cap_applies_to_review_requests_too`, `test_uncountable_review_requests_refuse_to_post`, `test_mention_passed_for_copilot_is_ignored_with_a_warning`, `test_dry_run_previews_and_stamps_nothing`, `test_human_mode_names_the_reviewer_and_the_timestamp`.
- Existing comment-path tests and `test_acceptance_codex_cycle.py` unchanged and green.

Runner stubs dispatch on argv: GraphQL when `cmd[:3] == ["gh","api","graphql"]`; timeline when `"--paginate" in cmd and cmd[-1].endswith("/timeline")`; POST when `"--method" in cmd`. `gh api user` is stubbed by the autouse cap fixture.

- [ ] Steps: write resolver tests → RED → implement → GREEN → commit `feat(resolver): shared review-request counting with cycle-0 exemption`; write request_rereview helper tests → RED → implement → GREEN; write main tests → RED → implement → GREEN; `/opt/homebrew/bin/pytest tests/test_request_rereview.py tests/test_reviewer_resolver.py tests/test_acceptance_codex_cycle.py -q` → all pass; commit `feat(rereview): Copilot review requests with pending/head dedupe and classified failures`.

---

### Task 4: Fetch-side cap count for review-request vendors

Spec: §4 (I6–I8), §5.1.

**Files:**
- Modify: `$S/fetch_gemini_threads.py` — `QUERY`, `PAGE_LIMIT_TIMELINE = 100`, `rereview_requests`, its three call sites, `loopStatus`, page warnings
- Test: `tests/test_fetch_gemini_threads.py`

**Interfaces:**
- `QUERY` gains `reviews.nodes.commit { oid }` and the `timelineItems` block (same text as `STATE_QUERY`'s).
- `rereview_requests(pull_request, agent_login=None, *, review_trigger_mention=DEFAULT, reviewer_login=None)`: review_request vendor → `reviewer_resolver.counted_review_requests(timeline nodes, agent_login=…, vendor=…, first_completed_review_at=reviewer_resolver.first_completed_review_at(reviews nodes, vendor))`; else the unchanged mention path.
- `review_request_count_complete(pull_request) -> bool` — `len(timelineItems.nodes) < PAGE_LIMIT_TIMELINE`.
- `loopStatus.reReviewCountComplete: bool` (always `true` for comment vendors); when `false`, a `pageLimitWarnings` entry `"review-request events hit page limit (100); the re-review count is a lower bound."` and `limitReached` computed from the lower bound.
- Call sites pass `reviewer_login=args.author` / `reviewer_login=author`.

**Tests:** `test_copilot_requests_by_the_agent_after_the_first_review_count` (agent event before first review excluded, after included, human excluded), `test_without_an_agent_login_every_post_review_copilot_request_counts`, `test_comment_vendors_still_count_mentions_not_events`, `test_missing_timeline_is_zero_not_an_error`, `test_full_timeline_page_marks_the_count_incomplete` (T9: 100 nodes → `reReviewCountComplete false`, warning present), `test_the_fetch_query_asks_for_review_requested_events_and_review_commits`.

- [ ] Steps: tests RED → implement → GREEN → `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py tests/test_acceptance_codex_cycle.py tests/test_json_stdout_discipline.py -q` → commit `feat(fetch): count Copilot review requests toward the cap after the first review`.

---

### Task 5: Head-anchored waiter for review-request vendors

Spec: §5.2.

**Files:**
- Modify: `$S/fetch_gemini_threads.py` — `wait_for_stable_review`, `run_wait_chunk`, heartbeat text
- Test: `tests/test_fetch_gemini_threads.py`

**Interfaces:**
- `head_review(pull_request, author) -> dict | None` — a non-`PENDING` review by `author` with `commit.oid == headRefOid`.
- `wait_activity_fingerprint(pull_request, author, after_iso) -> str | None` — for `trigger_kind_for(author) == "review_request"`: `None` unless `head_review(...)` exists, else `review_activity_fingerprint(pull_request, author, after_iso=None)`; for other vendors: `review_activity_fingerprint(pull_request, author, after_iso=after_iso)` (unchanged). Both waiters call this instead of `review_activity_fingerprint` directly. `after_iso` keeps its role as the settle-mode switch (cycle 1 fast path vs cycle 2+ quiet period) but is never a timestamp filter for review_request vendors.
- Heartbeat for review_request vendors: `Waiting for {author} review of {head7}...`.

**Tests:** `test_copilot_wait_is_not_ready_for_a_review_of_the_previous_head` (T8), `test_copilot_wait_is_ready_on_a_same_second_head_review` (T7: `submittedAt == after_iso`), `test_copilot_wait_ignores_a_pending_review_object` (T2), `test_copilot_wait_settles_with_quiet_period_on_cycle_two`, `test_gemini_wait_fingerprint_is_unchanged` (same inputs → same hash as `review_activity_fingerprint`), `test_run_wait_chunk_copilot_waiting_then_ready`.

- [ ] Steps: tests RED → implement → GREEN → `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py tests/test_acceptance_codex_cycle.py -q` → commit `feat(wait): anchor Copilot waits on the head commit, not a timestamp`.

---

### Task 6: Copilot overview parser, severity provenance, `reviewerOverview`, clean criterion

Spec: §5.3, §5.4, §5.6.

**Files:**
- Modify: `$S/fetch_gemini_threads.py` — constants next to `SEVERITY_RE`, `thread_severity`, new functions after `review_body_findings`, wiring in `main()` after `threads.extend(review_body_findings(...))`, JSON + human output
- Create: `tests/test_copilot_overview.py`

**Interfaces:**
- `parse_copilot_overview(body) -> dict | None`: `None` without `<!-- ccr-overview`; else `{assessment: str, approving: bool, open_findings: int | None, findings: [{severity, discussion_id, title}], unlinked_findings: int, complete: bool}`. `complete = bool(heading) and open_findings is not None`; `open_findings` is `None` when no count line (`**N open finding(s)**` or `<strong>N open finding(s)</strong>`) parses.
- `copilot_overview_severities(pull_request, author) -> tuple[dict[str, dict], list[str]]` — `discussion_id → {"severity", "review_url", "commit"}` over all vendor overviews oldest→newest (newest wins); warnings for unlinked findings.
- `annotate_reviewer_severity(threads, severities) -> None` — sets `thread["reviewerSeverity"]` and `thread["reviewerSeveritySource"] = {"review_url", "commit"}` by `#discussion_r<id>` suffix of any comment URL.
- `latest_reviewer_overview(pull_request, author) -> dict | None` — `{assessment, approving, open_findings, complete, covers_head, commit, submitted_at, url}`; `covers_head = commit == headRefOid`.
- `clean_blocked_by(threads, overview, *, review_request_vendor: bool) -> str | None` — `None` when threads are non-empty or the vendor is a comment vendor; else `no_review_for_head` (overview `None` or not `covers_head`), `overview_unparseable` (not `complete`), `overview_reports_open_findings` (`open_findings > 0`), else `None`.
- `thread_severity` honors a valid `reviewerSeverity` first.
- JSON: top-level `reviewerOverview` (dict `+ warnings`, or `null`), `loopStatus.cleanBlockedBy`. Human: `[loop] reviewer overview: <assessment> — <N> open finding(s) (covers head|stale: <sha7>|unparsed)`, and when `cleanBlockedBy` is set with zero threads: `[loop] not clean: <reason>`.

**Tests** (`tests/test_copilot_overview.py`, markup from the live captures): `test_parses_assessment_findings_and_count`, `test_clean_overview_is_approving_with_zero_findings` (bold form), `test_non_copilot_bodies_are_not_overviews`, `test_marker_without_heading_or_count_is_incomplete` (T11), `test_an_unlinked_finding_is_a_warning_not_a_finding`, `test_thread_gets_its_severity_and_source_from_the_overview`, `test_newer_overview_wins_and_records_its_commit` (T21), `test_unparseable_overview_leaves_threads_listed_as_unknown` (T11), `test_latest_reviewer_overview_reports_covers_head`, `test_clean_blocked_by_table` (T18/T19 + comment vendor → `None`), `test_codex_and_gemini_severity_parsing_is_untouched`, `test_bogus_annotation_is_ignored`, `test_annotation_leaves_finding_fingerprint_and_pattern_signature_unchanged_but_changes_render_fingerprint` (T20).

- [ ] Steps: tests RED → implement → GREEN → `/opt/homebrew/bin/pytest tests/test_copilot_overview.py tests/test_fetch_gemini_threads.py tests/test_acceptance_codex_cycle.py tests/test_json_stdout_discipline.py tests/test_delta_render.py -q` → commit `feat(fetch): Copilot severities from the review overview, with provenance and a clean gate`.

---

### Task 7: Discover reviewers from review authors and pending requests

**Files:**
- Modify: `$S/fetch_gemini_threads.py` — `REVIEWER_DISCOVERY_QUERY` (~line 264), `fetch_reviewer_discovery` (~line 393), `--list-reviewers` human output (~line 3403)
- Modify: `$S/reviewer_resolver.py` — `discover_candidates`
- Test: `tests/test_reviewer_resolver.py`, `tests/test_fetch_gemini_threads.py`

**Interfaces:**
- Consumes: Task 2's `Candidate.trigger_kind`, `Candidate.source`, `trigger_kind_for`.
- Produces: candidates from `pull_request["reviews"]["nodes"][*]["author"]` (source `"review"`) and `pull_request["reviewRequests"]["nodes"][*]["requestedReviewer"]` (source `"requested"`), after thread authors (source `"thread"`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reviewer_resolver.py`:

```python
def test_review_authors_and_pending_requests_are_candidates_too():
    """A Copilot review with zero findings has no thread comments, and a
    just-requested Copilot has nothing at all yet; both must be discoverable."""
    pull_request = _pr(_thread(_comment("coderabbitai")))
    pull_request["reviews"] = {"nodes": [
        {"author": {"login": "copilot-pull-request-reviewer", "__typename": "Bot"}},
        {"author": {"login": "alice", "__typename": "User"}},
    ]}
    pull_request["reviewRequests"] = {"nodes": [
        {"requestedReviewer": {"__typename": "Bot", "login": "sourcery-ai"}},
        {"requestedReviewer": {"__typename": "User", "login": "bob"}},
        {"requestedReviewer": None},
    ]}

    candidates = reviewer_resolver.discover_candidates(pull_request, self_login="codex-agent")

    assert [(c.login, c.source) for c in candidates] == [
        ("coderabbitai", "thread"),
        ("copilot-pull-request-reviewer", "review"),
        ("sourcery-ai", "requested"),
    ]
    assert candidates[1].display_name == "Copilot"
    assert candidates[1].trigger_kind == "review_request"
    assert candidates[1].review_trigger is None


def test_a_login_seen_in_a_thread_is_not_duplicated_from_reviews_or_requests():
    pull_request = _pr(_thread(_comment("copilot-pull-request-reviewer")))
    pull_request["reviews"] = {"nodes": [
        {"author": {"login": "copilot-pull-request-reviewer", "__typename": "Bot"}},
    ]}
    pull_request["reviewRequests"] = {"nodes": [
        {"requestedReviewer": {"__typename": "Bot", "login": "copilot-pull-request-reviewer"}},
    ]}

    candidates = reviewer_resolver.discover_candidates(pull_request, self_login=None)

    assert [(c.login, c.source) for c in candidates] == [
        ("copilot-pull-request-reviewer", "thread"),
    ]


def test_discovery_without_reviews_or_requests_keys_still_works():
    pull_request = _pr(_thread(_comment("gemini-code-assist")))

    candidates = reviewer_resolver.discover_candidates(pull_request, self_login=None)

    assert [c.login for c in candidates] == ["gemini-code-assist"]
```

Append to `tests/test_fetch_gemini_threads.py` (`PullRequest` is already imported there; if not, add it to the import list):

```python
class TestReviewerDiscoveryFetch:
    def test_single_page_discovery_keeps_reviews_and_pending_requests(self, monkeypatch):
        import fetch_gemini_threads as fgt

        payload = {"data": {"repository": {"pullRequest": {
            "number": 1,
            "url": "https://github.com/acme/widget/pull/1",
            "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
            "reviews": {"nodes": [
                {"author": {"login": "copilot-pull-request-reviewer", "__typename": "Bot"}},
            ]},
            "reviewRequests": {"nodes": [
                {"requestedReviewer": {"__typename": "Bot", "login": "copilot-pull-request-reviewer"}},
            ]},
        }}}}
        captured = {}

        def fake_run_gh(args, cwd=None):
            captured["query"] = args[3]
            return payload

        monkeypatch.setattr(fgt, "run_gh", fake_run_gh)

        discovery = fgt.fetch_reviewer_discovery(PullRequest(owner="acme", repo="widget", number=1))

        assert "reviews(last:100)" in captured["query"]
        assert "reviewRequests(first:50)" in captured["query"]
        pr = discovery["pull_request"]
        assert pr["reviews"]["nodes"][0]["author"]["login"] == "copilot-pull-request-reviewer"
        assert pr["reviewRequests"]["nodes"][0]["requestedReviewer"]["login"] == "copilot-pull-request-reviewer"
        assert discovery["partial"] is False

    def test_page_capped_discovery_still_carries_the_first_pages_reviews(self, monkeypatch):
        import fetch_gemini_threads as fgt

        def page(cursor):
            return {"data": {"repository": {"pullRequest": {
                "number": 1, "url": "u",
                "reviewThreads": {"pageInfo": {"hasNextPage": True, "endCursor": cursor}, "nodes": []},
                "reviews": {"nodes": [{"author": {"login": "copilot-pull-request-reviewer", "__typename": "Bot"}}]},
                "reviewRequests": {"nodes": []},
            }}}}

        monkeypatch.setattr(fgt, "run_gh", lambda args, cwd=None: page("c1"))

        discovery = fgt.fetch_reviewer_discovery(
            PullRequest(owner="acme", repo="widget", number=1), max_pages=2
        )

        assert discovery["partial"] is True
        assert discovery["pull_request"]["reviews"]["nodes"][0]["author"]["login"] == "copilot-pull-request-reviewer"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_reviewer_resolver.py tests/test_fetch_gemini_threads.py -k "candidates_too or not_duplicated or without_reviews or ReviewerDiscoveryFetch" -v`
Expected: FAIL (candidates missing `source`/review entries; query assertions fail).

- [ ] **Step 3: Implement discovery in the resolver**

Replace `discover_candidates` in `$S/reviewer_resolver.py`:

```python
_SOURCE_RANK = {"thread": 0, "review": 1, "requested": 2}


def discover_candidates(
    pull_request: dict[str, Any],
    *,
    self_login: str | None,
) -> list[Candidate]:
    """Bot reviewers visible on the PR, threads first, then reviews, then pending requests.

    Thread comment authors are the strongest signal (they left findings). A
    review with no inline comments (a clean Copilot pass) only shows up as a
    review author. A reviewer that was requested but has not answered yet only
    shows up as a pending request. Within a source, first-seen order; a login
    seen in an earlier source keeps that source.
    """
    seen: dict[str, tuple[int, int, str]] = {}

    def consider(author: Any, index: int, source: str) -> None:
        if not isinstance(author, dict):
            return
        login = author.get("login")
        if not isinstance(login, str) or not login:
            return
        if self_login and login == self_login:
            return
        if login in seen or not _is_bot_author(author):
            return
        seen[login] = (_SOURCE_RANK[source], index, source)

    threads = (pull_request.get("reviewThreads") or {}).get("nodes") or []
    for thread_index, thread in enumerate(threads):
        if not isinstance(thread, dict):
            continue
        for comment in _iter_comments(thread):
            consider(comment.get("author"), thread_index, "thread")
    reviews = (pull_request.get("reviews") or {}).get("nodes") or []
    for review_index, review in enumerate(reviews):
        if isinstance(review, dict):
            consider(review.get("author"), review_index, "review")
    requests = (pull_request.get("reviewRequests") or {}).get("nodes") or []
    for request_index, request in enumerate(requests):
        if isinstance(request, dict):
            consider(request.get("requestedReviewer"), request_index, "requested")

    return [
        Candidate(
            login=login,
            display_name=display_name_for(login),
            review_trigger=trigger_for(login),
            trigger_kind=trigger_kind_for(login),
            source=source,
        )
        for login, (_rank, _index, source) in sorted(
            seen.items(), key=lambda item: (item[1][0], item[1][1], item[0])
        )
    ]
```

- [ ] **Step 4: Implement the query and fetch changes**

In `REVIEWER_DISCOVERY_QUERY`, after `url` and before `reviewThreads(first:100, after:$threadsAfter) {`, add:

```graphql
      reviews(last:100) {
        nodes {
          author { login __typename }
        }
      }
      reviewRequests(first:50) {
        nodes {
          requestedReviewer {
            __typename
            ... on Bot { login }
            ... on User { login }
          }
        }
      }
```

In `fetch_reviewer_discovery`, before the `for _ in range(max_pages):` loop add `extras: dict[str, Any] = {}`. Right after the `except (KeyError, TypeError)` block (once `pull_request` is validated), add:

```python
        if not extras:
            # reviews and reviewRequests are not paginated with the threads;
            # the first page's copy is the one to keep.
            extras = {
                "reviews": pull_request.get("reviews") or {"nodes": []},
                "reviewRequests": pull_request.get("reviewRequests") or {"nodes": []},
            }
```

In the page-cap / missing-cursor fallback `return {...}` at the end of the function, add `**extras,` inside the `"pull_request": {...}` dict (after `"url": pr.url,`). The single-page exit (`merged = dict(pull_request)`) already carries them.

In the `--list-reviewers` human output replace the candidate line:

```python
                    for candidate in candidates:
                        if candidate.trigger_kind == "review_request":
                            trigger = "review request (GitHub API)"
                        else:
                            trigger = candidate.review_trigger or "(no safe trigger known)"
                        note = " (requested, not yet reviewed)" if candidate.source == "requested" else ""
                        print(f"- {candidate.display_name} `{candidate.login}` trigger={trigger}{note}")
```

- [ ] **Step 5: Run the tests**

Run: `/opt/homebrew/bin/pytest tests/test_reviewer_resolver.py tests/test_fetch_gemini_threads.py tests/test_acceptance_codex_cycle.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add plugins/gh-review-loop/skills/gh-review-loop/scripts/fetch_gemini_threads.py plugins/gh-review-loop/skills/gh-review-loop/scripts/reviewer_resolver.py tests/test_reviewer_resolver.py tests/test_fetch_gemini_threads.py
git commit -m "feat(discovery): surface review authors and pending review requests as candidates"
```

---

### Task 8: Copilot cycle fixture and acceptance test

Spec: §8 rows T2, T7, T8, T11, T12, T18, T19, T22.

**Files:**
- Create: `tests/fixtures/copilot_cycle.json` — payloads shaped like `QUERY` output, markup from the live captures (repo/paths/prose replaced): `requested_only` (pending request, no reviews), `initial` (head `f4a55e3…`, one review on that commit, 2 open findings high+medium, one human request event before it), `pending_rereview` (head `d7efa49…`, only the old review, `reviewRequests` lists Copilot, agent request event at `2026-10-07T16:20:05Z`), `after_rereview` (head `d7efa49…`, second review on it with 1 low finding, old threads resolved+outdated, agent event at `16:20:05Z`, review `submittedAt` **equal** to `16:20:05Z` for T7), `clean` (head `6f03c25…`, approving overview `**0 open findings**` on that commit, no threads), `clean_stale` (zero threads, approving overview on `f4a55e3…`, head `6f03c25…`), `unparseable` (marker-only body, two threads).
- Create: `tests/test_acceptance_copilot_cycle.py` mirroring `test_acceptance_codex_cycle.py`: discovery (`requested` and `thread` sources), full cycle (select → fetch severities/overview/`reReviewRequests == 0` with the human's request → request via stubbed `request_review` with stubbed `request_state` returning `ABSENT` → wait on `pending_rereview` then `after_rereview` (ready only on the head review; T2/T7/T8) → second fetch `reReviewRequests == 1`, `limitReached false`, new thread `low`), `clean` → `cleanBlockedBy null`, `clean_stale` → `no_review_for_head`, `unparseable` → threads `unknown` + `overview_unparseable` not set because threads exist, `test_only_the_agents_post_review_requests_count` (T12), human-mode overview line, vendor facts.

- [ ] Steps: write fixture → write tests → run → fix wiring until green → `/opt/homebrew/bin/pytest -q` all green → commit `test: Copilot acceptance cycle with pending, head-anchored wait, and clean gate`.

---

### Task 9: Documentation

Spec §11. README (prerequisites bullet, table row for Copilot above the configurable row, severity caveat → three conventions); `references/reviewer-selection.md` (Copilot row; paragraph naming `review_pending`, `already_reviewed_head`, `head_mismatch`, `review_request_rejected`, `cleanBlockedBy`); SKILL.md step 4 `(Codex, Copilot)`, step 6 one clause `(`cleanBlockedBy` set → not clean)`, step 8 status list extended. Budgets: SKILL.md ≤ 15000 bytes / 150 lines, reference ≤ 7000 bytes — trim wording rather than raise.

- [ ] Steps: edit → `/opt/homebrew/bin/pytest tests/test_doc_budgets.py -q && /opt/homebrew/bin/pytest -q` → commit `docs: Copilot is a built-in reviewer; document the review-request path`.

---

### Task 10: Live validation (first real run)

Requires a Copilot-enabled repo the `gh` session can write to. Preferred: a scratch repo under `OrenAshkenazy` once a Copilot Pro/Pro+/Max plan is active (same account as the `gh` token, no SSO). Fallback: `qizsecurity/k8s-deploy#37` after SSO authorization. If neither is available, stop and report; do not substitute.

- [ ] Discovery: `fetch_gemini_threads.py --pr <url> --list-reviewers --format json` → Copilot candidate with `trigger_kind: review_request`.
- [ ] `request_rereview.py --repo O/R --pr N --reviewer-login copilot-pull-request-reviewer --dry-run --json` → `would_post` or a state stop.
- [ ] Push a trivial commit; `request_rereview.py … --expect-head "$(git rev-parse HEAD)" --json` → success payload. Immediately re-run → `review_pending`.
- [ ] Background: `fetch_gemini_threads.py --pr <url> --reviewer copilot-pull-request-reviewer --reviewer-source confirmed --wait --after "$REREVIEW_AT" --timeout 900 --format json` → returns on the head review.
- [ ] Fetch → `reviewerOverview.covers_head true`, severities on listed threads, `reReviewRequests == 1`. Re-run the request → `already_reviewed_head`.
- [ ] Record outputs in the PR description. Do not fix findings on the validation repo.

---

## Self-review notes

- Spec coverage: §2 T1; §7 T2; §3/§4/§6 T3; §4/§5.1 T4; §5.2 T5; §5.3/5.4/5.6 T6; §5.5 T7; §8 T8; §11 T9; §9 residual T10.
- Names shared across tasks: `trigger_kind`, `request_login`, `trigger_kind_for`, `request_login_for`, `normalize_request_event`, `first_completed_review_at`, `counted_review_requests`, `request_state`, `request_review`, `count_agent_review_requests`, `ReviewRequestRejected`, `rereview_requests(..., reviewer_login=)`, `head_review`, `wait_activity_fingerprint`, `parse_copilot_overview`, `copilot_overview_severities`, `annotate_reviewer_severity`, `latest_reviewer_overview`, `clean_blocked_by`, `reviewerOverview`, `reviewerSeverity`, `reviewerSeveritySource`, `cleanBlockedBy`, `reReviewCountComplete`, `Candidate.source`.
- Review Focus 1–5 → T3 (`count_agent_review_requests_paginated_rest` uses `Copilot`), T6 (bold/strong forms), T3 (stale mention), T8 (human request), T3 (422).
