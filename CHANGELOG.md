# Changelog

All notable changes to this project will be documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **New plugin `glg-questionnaire` (0.1.0).** A second skill in this
  marketplace for GLG expert-network members. Triages GLG project invitations
  and follow-up questions from Gmail (`parse_glg_email.py` turns one email into
  JSON: kind, project, cpid, rate, client type, flags, link expiry from the
  `_session` JWT, and the Q/A pairs GLG records on acceptance), keeps a local
  append-only answer bank of the member's own answers (`answer_bank.py`, seeded
  from "Accept Confirmation" emails, fuzzy lookup by normalized question), and
  drives the member's signed-in browser to fill and submit the questionnaire
  only after the member confirms a per-row-sourced answer sheet. Hard rules:
  no invented answers, no submit or decline without an explicit go, employer
  and conflict questions always go to the member, paid surveys out of scope.
  Stdlib only, nothing transmitted. Registered in both marketplace files; CI
  compiles its scripts and checks manifest drift.

### Fixed

- **GraphQL re-requests confirm selected-reviewer registration.** A mutation returning the PR ID alone cannot record success or consume a completed request; unconfirmed registration retains an in-flight marker and blocks duplicate requests until reconciliation.

- **Publication checks use the validated push URL.** Separate fetch and push destinations no longer reject fork-only branches or falsely report a successful push as unconfirmed.

- **Displayed comment-review capacity includes local receipts.** Collection and publication share comment-ID union logic, including full GitHub database IDs, while incomplete history and truncated comment pages are reported as incomplete counts.

- **Event-confirmed uncertain requests record GitHub's timestamp.** Immediate native-request confirmation uses the matched timeline event time in the result and saved receipt, preserving cap accounting when the local clock differs.

- **Copilot re-reviews require readable PR and bot IDs.** Missing or ambiguous IDs stop before stale-request cancellation, and refreshed IDs are checked again before posting; completed reviews never silently fall back to the initial REST request endpoint.

- **Public receipts keep verification commands local.** GitHub comments show check names and results without machine-specific command paths or private arguments; detailed local verification records are retained.

- **Native request recovery survives later PR pushes.** A fresh matching request-event ID records the prior accepted request at GitHub's timestamp even after the head changes; legacy completion evidence keeps its head checks. The README distinguishes initial REST requests from GraphQL re-requests.

- **Review writes confirm registration and recover without duplicate requests.** Initial Copilot REST success is checked against the selected reviewer, with bounded GraphQL registration reads and completed-review/request-event evidence for fast reviews. Actor lookup precedes stale cancellation, and refreshed PR state and implicit head guards protect its replacement. Definite comment failures clear in-flight markers; uncertain writes retain them and can be recorded once using `--reconcile` and new GitHub comment/event IDs without relying on the local clock. Windows state paths use the native user profile instead of MSYS `HOME`.

- **Concurrent local runs cannot race publication and re-review.** A per-PR lock covers verification, push, and the publisher's automatic review request; standalone requests share it. State mutations serialize and replace the file atomically, preserve other PRs, and refuse corrupt existing state. Failed requests after push retain the confirmed publication result.

- **Copilot re-requests work for PR authors through GraphQL `rerequestReviews`.** The loop reads PR and bot IDs from GitHub and preserves its head, pending-review, cap, and uncertain-result guards. Copilot findings embedded in review summaries are actionable, and publication honors per-run cap overrides and rejects non-regular files before reading them.

- **The scratch directory is no longer a predictable path in `/tmp`.** The
  per-user path introduced for cross-call stability could be pre-created — or
  symlinked — by another local account before the first run, after which
  `mkdir -p` succeeded and email bodies carrying `_session` tokens were written
  there. It now lives under `$XDG_RUNTIME_DIR` (else `$HOME/.cache`), which
  only this account can write, and the prelude refuses a symlink or a directory
  it does not own before using it.

- **`handled()` keys by consultation id.** Two consultations can share a
  project title; keying on the title let the newer row hide the older, and
  triage — told to match by title — could mark a never-handled consultation as
  done. The id identifies the work, with the title as the fallback for records
  that have none.

- **One bad line no longer hides the whole answer bank.** `load_entries()`
  called `json.loads()` unconditionally, so an interrupted append or a hand
  edit of the documented file made every earlier answer unreachable. Malformed
  and non-object lines are counted, reported on stderr, and skipped.

- **Project titles and detail text travel by file.** `log --project
  "<title>"` was the last shell-interpolation sink: titles come from email
  subjects, so recording an outcome could execute `$(...)` from one.
  `--project-file` and `--detail-file` take them from a file (`-` for stdin);
  the inline flags remain for text the member typed.

- **Triage reads the submission history.** Outcomes were written to
  `submissions.jsonl` for reuse but never read back, so a project finished in
  an earlier session was offered again — and an accept confirmation usually has
  no `cpid` to tie it to the invitation. `answer_bank.py handled` returns one
  row per project (newest outcome, with its `cpid`) and triage matches on both
  keys before suggesting anything.

- **A follow-up reports its own deadline.** The top-level
  `accept_link_expires_at` always preferred the accept link, so a follow-up
  quoting the earlier accept URL showed that token's expiry — often already
  passed — for work that was still open. The link matching the detected kind
  owns the deadline.

- **The fetch propagates unknown request provenance.** The write side already
  refused to guess; the fetch still treated a missing or incomplete local
  record as an authoritative empty list, so the loop could display zero cap
  usage and run another fix/push cycle before the write refused. Unknown
  provenance now falls back to the actor filter and sets
  `reReviewCountComplete: false` — unless there is nothing to attribute, where
  zero is exact.

- **The GLG docs no longer rely on shell state between tool calls.** Each
  agent shell call is its own process, so the "resolve it once" plugin-root
  block left `GLGQ_PLUGIN_ROOT` unset for every later command, and the scratch
  directory's `EXIT` trap deleted it the moment the setup call returned. Both
  are now a two-line prelude at the top of every command block, with a derived
  per-user scratch path (`$TMPDIR/glgq-$(id -u)`, `chmod 700`) and an explicit
  `rm -rf` at the end of the session.

- **Attestations are excluded from `PROFILE` sourcing too.** "Everything in
  the profile is a `PROFILE` source" let the stored employer answer a
  current-employer screen that hard rule 3 sends to the member every time; if
  employment changed since bootstrap that is a false compliance answer. Those
  profile fields are for triage only, and such rows are `NEEDS MEMBER`.

- **A fuzzy bank match must agree on the subject.** Boilerplate dominates the
  similarity score — "current paying customer of Datadog" against "... of
  Splunk" scores about 0.79, well over the 0.55 threshold — so `lookup()`
  offered one vendor's answer for another. Decisive (non-boilerplate) tokens
  must now overlap.

- **Reviewer discovery paginates review authors.** A clean Copilot pass leaves
  no thread, so its review is the only evidence it reviewed; on a PR with more
  than 100 reviews that review fell outside the unpaginated page and discovery
  reported no Copilot candidate. Review authors now come from their own
  paginated query.

- **Missing request provenance fails closed.** An empty local record was
  indistinguishable from a lost or unwritable one, and the counter treats any
  list as authoritative — zero used, so every pushed head could post another
  request past the cap. Unavailable history is now `None`: with no record and
  no candidate requests the count is a verified zero, but with candidate
  requests under the same login it is unknown and nothing is posted
  (`--no-cap-check` is the deliberate override). A state write that fails after
  a successful POST marks the history unusable rather than silently short.

- **The seeding example passes its subject by file too.** The earlier shell
  fix covered SKILL.md but left `references/answer-bank.md` instructing the
  agent to substitute `<subject>` inside a double-quoted argument.

- **Prior-project questions are refused in either word order.** The filter
  matched "other GLG" and "GLG project(s)" only, so "Which previous projects
  have you completed through GLG?" was stored and could come back as a `BANK`
  suggestion for a question GLG's confidentiality section sends to the member
  every time.

- **A survey or payment subject outranks a quoted link.** A paid-survey email
  that quotes an earlier GLG thread carries that thread's follow-up URL, and
  routing on the link sent a survey into questionnaire automation instead of
  the mandatory `survey (manual)` row.

- **GLG scratch files are owner-only, outside any checkout, and removed.**
  SKILL.md had the agent write `subject.txt`, `question.txt`, `body.txt` and
  parsed JSON into the working directory — mode `0644` under the usual umask,
  committable by accident, and able to clobber same-named project files. They
  now live in a per-session `mktemp -d` directory created under `umask 077` and
  removed on exit.

- **The fetch-side cap count admits when it is a lower bound.**
  `review_request_count_complete()` checked only the timeline page, while the
  free-cycle boundary comes from the reviews page: a full page can hide the
  vendor's first completed review, making later requests look free and letting
  the loop run another fix/push cycle before the request script refuses the
  write. Both pages are now checked.

- **`--expect-head` is honored on the comment path, and documented in the
  canonical command.** It guarded only the review-request path, so a Codex ping
  could land before GitHub saw the push and buy a review of the previous
  commit, which the loop would read as this cycle's verdict. The comment path
  now reads the head and posts nothing on a mismatch (`head_mismatch`) or an
  unreadable head (`uncountable_state`), and workflow step 8 passes
  `$(git rev-parse HEAD)`.

- **Every compliance class in the reference is refused by the bank.** The
  attestation predicate covered employer, conflict, recording and blinded-client
  questions but not government or public-sector roles, pending trials and
  litigation, public-company employment within six months, or "which other GLG
  projects have you done" — all four listed in `references/compliance.md` as
  questions that always go to the member. A reused answer there is a false
  compliance attestation, so the predicate now mirrors that list class for
  class, with a test that ordinary project questions are not swept up.

- **An invalid JWT expiry no longer aborts parsing.** An out-of-range `exp`
  made `datetime.fromtimestamp()` raise `OverflowError` (`OSError` on some
  platforms) after URL validation, taking down an email that may also carry a
  valid invitation. The link is kept with an unknown expiry.

- **Actionable signals outrank quoted Q/A text.** A reply that quotes an
  earlier confirmation carries `Q:`/`A:` lines, and `detect_kind()` returned
  `accept_confirmation` on sight of them — seeding stale answers and hiding an
  open follow-up or a `survey (manual)` row. A follow-up link, an explicit
  survey/payment/confirmation subject and a live accept link are all checked
  first.

- **The free initial Copilot request is not gated on the cap.** With
  `max_rereview_requests: 0`, or with `gh api user` unresolvable, a PR Copilot
  had never reviewed could not get its first request: the count was correct
  (zero) but `used >= cap` still reported `capped`, and the cap's reads ran for
  a request that needs no accounting. The branch is skipped while the vendor has
  no completed review.

- **Untrusted text never reaches a shell word.** The GLG skill's documented
  commands interpolated an email subject and a questionnaire question into
  `--subject "<subject>"` / `--question "<question text>"`; double quotes do
  not suppress `$(...)` or backticks, so following the template with a crafted
  subject ran it locally. `parse_glg_email.py` takes `--subject-file` and
  `answer_bank.py` takes `--question-file` (`-` for stdin), and the docs write
  the value to a file first.

- **Per-project attestations are refused by the store, not by memory.** An
  Accept Confirmation carrying a current-employer, conflict, recording-consent
  or blinded-client answer was seeded into the bank, where `lookup()` could
  offer it as reusable — against SKILL.md hard rule 3 and against PRIVACY.md's
  statement that these are never stored. `add` now raises `NotReusable` (CLI
  exit 3) and `seed` reports them under `skipped` instead of storing them.

- **Only `https` GLG links are actionable.** An `http://members.glgresearch.com`
  accept or follow-up URL carries the `_session` bearer token in its query
  string, so opening one would put the member's session on the wire in the
  clear. Plaintext links classify as `other`.

- **Triage no longer collapses messages without a consultation ID.** Surveys,
  payments, call reminders and digests all parse with `cpid: null`; grouping on
  it alone merged them into one row and could hide a paid survey the hard rules
  require reporting. Only non-empty IDs group; the rest key by Gmail thread,
  falling back to the project ID.

- **A pending review never covers the head.** `_reviews_oldest_first()` fed
  `PENDING` reviews into the overview and severity helpers, which
  `head_review()` already excluded: a draft reported as covering the head sent
  the loop to `overview_unparseable` instead of waiting, and a zero-finding
  draft could clear the clean gate before submission.

- **The cap counts the loop's own requests, not yours.** Loop and member act
  under the same `gh` login, so the timeline's actor could not tell a
  loop-issued Copilot request from one clicked in the GitHub UI — three manual
  requests could stop a first run as `capped`. Requests are now recorded
  locally as they are posted (outside the per-run block, so they outlive run
  tracking) and the cap counts those, with the fetch reading the same record so
  the displayed and enforced counts agree.

- **An unreadable PR state is no longer reported as a counting failure.** It
  emitted `uncountable_count`, whose message suggested retrying with
  `--no-cap-check` — a flag consulted after that branch, so the retry took the
  same path and still posted nothing. It is now `uncountable_state`, with no
  bypass advice.

- **A still-pending Copilot request is never doubled.** `STALE_PENDING` fell
  through to a second POST while GraphQL still listed the first request as
  pending, and the design evidence records that producing two reviews. Age
  alone cannot prove abandonment, so the open request is now explicitly
  withdrawn (`cancel_review_request()`) after the cap clears and before the
  repost; a withdrawal that fails posts nothing and reports
  `stale_request_not_replaced`. A dry run withdraws nothing.

- **`glg-questionnaire` resolves its plugin root on both runtimes.** The skill
  addressed its scripts through `${CLAUDE_PLUGIN_ROOT}`, which is unset under
  Codex, where the plugin is also published — the command expanded to
  `/skills/glg-questionnaire/...` and failed before triage. It now resolves
  `GLGQ_PLUGIN_ROOT` from the Codex and Claude install locations, like the
  other dual-runtime plugin.

- **The loop's workflow routes `review_request_uncertain`.** Step 8 said every
  status means nothing was posted and then captured `created_at`, which the
  uncertain payload deliberately lacks — an agent following it would build a
  broken wait or re-request a review GitHub may have accepted. The uncertain
  status now routes to the delayed state re-read, and `created_at` is captured
  only from a posted result.

- **A malformed URL no longer aborts GLG triage.** `urlsplit()` raises
  `ValueError` on input like `https://[foo` or an out-of-range port, so one
  broken link in a forwarded email took down parsing of a message that also
  carried a valid invitation. Such URLs classify as `other`.

- **The clean gate blocks when the overview count exceeds the parsed links.**
  `open_findings` is authoritative and `finding_ids` is only what the parser
  recognized; two reported findings with one parsed link read as clean once
  that link was known. Fewer links than findings now keeps
  `overview_reports_open_findings`.

- **Questionnaire answers are stored owner-only.** `answers.jsonl` and
  `submissions.jsonl` were created `0644` in a `0755` directory under the usual
  `022` umask, readable by every local account on a shared machine, and they
  hold employment, customer and conflict answers. The directory is now `0700`
  and the files `0600`, a looser existing mode is repaired, and the reference
  tells the agent to create `profile.md` the same way.

- **The marketplace privacy policy covers `glg-questionnaire`.** The Codex
  manifest linked a policy describing only `gh-review-loop`, GitHub data and
  `~/.config/gh-review-loop`. PRIVACY.md now discloses what the GLG plugin
  reads (Gmail messages from GLG, the questionnaire page in the member's own
  browser) and what it stores under `~/.config/glg-questionnaire`, with its own
  removal controls. Existing text was added to, not rewritten.

- **GLG action links are trusted by host, not by substring.** `classify_link()`
  matched `members.glgresearch.com` anywhere in the URL, so a crafted or
  forwarded email could present
  `https://members.glgresearch.com.attacker.example/accept/accept` as a
  legitimate accept link that the skill then opens in the member's browser. The
  URL is now parsed and the normalized hostname must equal the GLG host, with an
  http(s) scheme; `consultationIds` is read from the query string rather than
  the raw URL.

- **Copilot aliases canonicalize before any filtering.** `--reviewer copilot`,
  `@copilot`, `Copilot` or `copilot-pull-request-reviewer[bot]` kept that
  spelling in the persisted record and in the loop's selection state, while
  GraphQL reports `copilot-pull-request-reviewer`. The loop requested the right
  reviewer and then matched none of its reviews or threads, timing out or
  reporting no feedback. `reviewer_resolver.canonical_login()` now resolves a
  known vendor's alias to its GraphQL login on both the persist and the project
  path; unknown logins pass through untouched.

- **The re-review cap and head dedupe no longer depend on one GraphQL page.**
  `request_state()` read `reviews(last:100)`, which on a PR with more than 100
  reviews can omit the vendor's first completed review (moving the cycle-0
  boundary forward, so capped requests look free) and its review of the current
  head (allowing a redundant request). Reviews and review-request events now
  come from the paginated REST APIs, and an unreadable read refuses instead of
  reporting `ABSENT`.

- **An explicit mention outranks the persisted reviewer.**
  `resolve_reviewer_login()` preferred persisted state, so persisted Copilot
  plus `--review-trigger-mention @codex` sent a Copilot review request, and
  persisted Codex plus `@copilot` posted a comment Copilot never reads. The
  precedence is now explicit login, explicit mention, then persisted state —
  matching `resolve_review_trigger()`.

- **An uncertain review request is reported as unknown, not as unposted.** When
  `gh` loses its connection after GitHub accepts the POST, and the timeline
  reconciliation runs before the event is visible, the result ended in "Nothing
  was posted" and told the caller to re-run — creating the duplicate review the
  state machine exists to prevent. That case is now
  `review_request_uncertain` (`posted: null`, `outcome: "unknown"`) and advises
  a delayed state check.

- **State directory renamed to `~/.config/gh-review-loop`, with automatic
  migration.** `~/.config/gh-gemini-review-loop` was the last remnant of the old
  plugin name, hardcoded independently in six modules and the three hooks.json
  shell guards. `loop_state.state_dir()` is now the single resolver
  (`GGRL_STATE_DIR` wins; else the new dir when it exists; else the unmigrated
  legacy dir), and the first run after upgrading renames the legacy dir and
  leaves a symlink at the old path so plugin versions installed before the
  rename keep reading the same state. Settings, verification profiles, run
  history, and the judge key are untouched: nothing is copied, merged, or
  transmitted. Migration is idempotent and fails open — if either the rename or
  the symlink cannot be created, the legacy dir stays and keeps resolving.

## [0.2.43] - 2026-08-17

Three of the four token/latency issues landed here at once. #88 and #89 were
merged into their stacked base branches rather than into `main`, so their
content reached `main` only when #90 (the stack tip) was merged — see the note
under 0.2.41/0.2.42.

### Changed

- **Skill loads on demand instead of all at once.** `SKILL.md` split from 1,195
  lines to 242, with the deeper material moved to seven `references/*.md` files
  the agent opens only when a run needs them (sweep internals, judge eval,
  receipts, terminal report, resume/recovery, variations, script usage). Worst
  case — every reference opened — is still smaller than the previous
  unconditional load. Closes #84.
- **Reviewer waits cost O(1) agent turns.** A blocking `--wait` run as a
  background task is now the primary mode; the chunked fallback decays 60s →
  300s instead of 60s → 90s. A 30-minute reviewer wait costs 2 turns
  (background) or 7 (fallback) instead of 21. Adds a 60s stderr heartbeat for
  liveness and a typed `WaitTimedOut` so a timeout ends in a relayable block
  rather than a traceback. Closes #85.
- **One receipt, one channel.** The full receipt is written to the sticky PR
  comment (`RUNNING` / `DONE` / `STOPPED`, edited in place) and chat gets a
  single `[loop]` pointer line — roughly 1,700 characters per cycle down to
  ~230. The HARD-GATE verbatim reprint and the push-gate state snapshot are
  gone; the Stop-hook backstop is unchanged. If the comment write fails, or
  under `--dry-run`, the full receipt still prints to stdout, so a receipt is
  never lost. Closes #86.

### Fixed

- The compact receipt line mixed two populations: `findings_fetched` is
  cumulative for the run, but the new/carried split describes only the
  currently-open findings, so a cycle that resolved earlier findings could read
  `findings 4 (1 new, 0 carried over)`, and a clean terminal run
  `findings 4 (0 new, 0 carried over)`. The cumulative count is now labelled
  "seen this run" and the split rides on `open`, where it shares a denominator;
  it is dropped entirely if the counts do not account for the open set.
- Terminal receipts lost their carried-over classification: `--record-run`
  called `clear_run_tracking()` before `prior_finding_fingerprints()` read the
  run block it had just deleted, so every still-open finding on a capped,
  human, or stopped receipt rendered as new. The fingerprints are now captured
  before the terminal branch.

## [0.2.42] - 2026-08-17

No functional change. Release automation fires on PR merge, and #89 was merged
into its stacked base branch rather than into `main`, so this tag is a version
bump over unchanged content. The work it is named for shipped in 0.2.43.

## [0.2.41] - 2026-08-17

No functional change, for the same reason as 0.2.42 (#88 merged into its base
branch). The work it is named for shipped in 0.2.43.

## [0.2.40] - 2026-08-17

### Changed

- **Hooks stop paying for an idle session.** The three bundled hooks are now
  shell guards that exit before spawning Python unless a `loop-active` sentinel
  exists and is younger than a 24h TTL. Measured on an idle session, the two
  PreToolUse gates went from ~277ms to ~12ms per tool call. The gates import a
  new stdlib-only `loop_state.py` rather than the 3,800-line main script, so the
  active path got faster too. `statusMessage` removed from all three hooks — an
  idle session no longer flashes loop status. Closes #83.

### Fixed

- `touch_sentinel()` swallowed `OSError` while its comment claimed the hooks
  would degrade to always spawning Python. The shell guard does the opposite:
  an absent sentinel exits 0, so a run active in `state.json` would silently
  lose its edit gate, push gate and Stop backstop. It now returns `False` on
  failure and the caller warns, naming the guarantees that go quiet. Failing
  open for the loop itself is kept deliberately — a marker that cannot be
  written must never wedge a run.

### Fixed

- Sibling sweeps now fingerprint a single flagged multi-line range and search
  only the PR's changed files for exact normalized copies. These hits are
  reported as `mirror` candidates, distinct from token-intersection matches.
  Closes #77.
- Mirror normalization now respects language-specific comment syntax, preserves
  string contents and Python indentation, and excludes copies overlapping any
  already-flagged line.
- Shape clustering now reads every code line in a reviewer's anchored range,
  with single-line fallback for reviewers that omit range starts. Token-based
  clustering and sibling sweeps require identifier-like shared tokens, so
  punctuation such as `!` and `&&` still constrains matches without making a
  pattern sweepable. Closes #74, #75, and #76.
- Degenerate review clustering now emits an explicit advisory when three or
  more findings all become singleton patterns, making prose-hash fallback
  failures visible before fixes begin in both Markdown and structured JSON
  output and in automatic snapshots. Shape clustering now reads source only
  when the current checkout matches the selected PR repository and head. The
  skill documents a safe manual recovery scoped to the explicitly selected PR.
  Closes #73.

### Changed

- Span-clustering and duplicate-block sweep regressions now use small,
  repository-owned synthetic corpora.
- **Bulletproof judge eval.** The `--judge-mode` path no longer depends on the `openai` SDK or on `OPENAI_API_KEY` being exported in a shell rc file — the two failure modes that wasted the most setup time (broken Homebrew Python, GUI-launched Claude Code not inheriting `~/.zshenv`, `pip install openai` blocked by externally-managed-environment).
  - `judge.py` now POSTs to `chat.completions` via stdlib `urllib`. Zero install step; works on any Python 3.10+. Self-hosted gateways still work via `OPENAI_BASE_URL`. HTTP error bodies surface verbatim so 401s show "Incorrect API key" instead of a generic message.
  - New `key_resolver.py` with tiered key lookup: env var → `~/.config/gh-gemini-review-loop/.env` (chmod 600) → macOS Keychain → Linux `secret-tool`. New CLI: `--set` (interactive or `--from-stdin`), `--print-source` (redacted), `--clear`. Storage default is the OS keystore — keys never sit in shell rc files or `ps` output.
  - `judge_doctor.py` updated: the `[2/5] openai SDK` check is replaced by `[2/5] network reachability` (a TCP probe to `api.openai.com:443`, no key needed). The `[3/5]` key check uses the tiered resolver and reports which source produced the key.
  - README + SKILL.md rewrite the "Setting your `OPENAI_API_KEY`" section around `key_resolver.py --set` as the recommended path. The env-var path stays documented as the escape hatch for CI / power users. Closes #23.

### Added

- **Persistent configurable re-review cap.** End users can set `max_rereview_requests` in `~/.config/gh-gemini-review-loop/preferences.json`; `--max-rereview-requests` still overrides it for one manual invocation. Defaults remain 3 cycles.
- **Optional end-user OpenAI judge (`--judge-mode`).** Per-finding labels (`valid_actionable` / `false_positive` / `needs_human` / `explanation_only` / `duplicate` / `already_addressed` + `severity_override` + `recommended_action`) so users can quickly see what's worth fixing on their own PRs. Default mode is `off` — nothing is sent to OpenAI until the user opts in. The script (`fetch_gemini_threads.py`) is the single source of truth: it reads `~/.config/gh-gemini-review-loop/preferences.json` on every invocation and combines that with the `--judge-phase {cycle,complete}` the agent supplies. New flags: `--judge-mode {off,on_cycle,on_complete,once}`, `--judge-phase`, `--judge-model`. New file: `plugins/.../scripts/judge.py` (ships in the install). Judge is read-only — it never resolves threads, posts comments, or pushes. Graceful skip when `OPENAI_API_KEY` / `openai` SDK missing, with a structured `{"status":"skipped","skip_reason":"..."}` in the JSON output. Privacy boundary documented in README + SKILL.md.
- **Maintainer-side Layer B finding-quality eval.** Cross-vendor LLM-as-judge (OpenAI `gpt-4o-mini` by default) rates each Gemini Code Assist finding as `useful` / `false-positive` / `borderline` / `dup` and compares against a hand-labeled corpus. Initial corpus: 11 findings from this repo's own PRs (#6–#9), ~91% useful by hand-label.
  - Maintainer side: `evals/` directory (judge, runner, fixtures, 32 hermetic tests, README).
  - Reporting: weekly CI workflow (`.github/workflows/eval-weekly.yml`) can commit a rendered report to `evals/results/latest.md` + `evals/results/YYYY-QQ.md` (per-quarter history) and update a rolling per-quarter tracking Issue for maintainer notifications.
  - Public README calibration copy is intentionally deferred until the reporting path is ready. End-user installs never ship the maintainer eval or call OpenAI for this maintainer-side workflow.
  - All cycle-3 hardening from the original PR #13 stack already baked in: API try/except → JudgeError, `--samples` validation, `request_timeout` (30s default), per-finding JudgeError tolerance, `majority_judge_label` empty-safe, explicit utf-8 I/O, `--report` parent dir, structured skip when key/SDK missing.

### Changed

- **Listing readiness pass for claudemarketplaces.com.** Added per-plugin `author` field (with email) to `.claude-plugin/marketplace.json` and the plugin's own `plugin.json`. README restructured around the solo-builder / small-team workflow: prerequisites, two-step install, natural-language usage examples, "Why This Plugin", optional judge eval, configuration, and advanced manual invocation. Removed stale top-of-repo `SKILL.md` and `scripts/` artifacts left over from the P2 plugin restructure — they were local-only (never tracked in git) but could confuse a fresh `git clone`.
- **Sharpened `.claude-plugin/marketplace.json` metadata for claudemarketplaces.com indexing.** Marketplace-level and plugin-level descriptions now position the plugin for solo builders and small teams using Claude Code, while retaining the technical differentiators: thread-state awareness, severity filtering, hard 3-cycle cap, stale/addressed-by-reply handling, and sticky receipts. `tags` expanded to 7 (added `sticky-receipt`, `severity-aware`). `keywords` expanded to 9 (added `sticky-receipt`, `severity-aware`, `addressed-by-reply`, `auto-resolve`, `dry-run`) to improve hit-rate against search queries.

### Added

- **Sticky receipt** (`--sticky-receipt`). Maintains one comment per PR that the script edits in place across loop invocations, instead of accreting a new comment per cycle. State persists in `~/.config/gh-gemini-review-loop/state.json` (override with `GGRL_STATE_DIR`). Discovery fallback: if the local state file is missing, the script scans PR comments for an embedded marker and re-attaches to the existing receipt. New `--receipt-status {running,done,stopped}` flag tags the header so PR watchers can see loop phase at a glance. Pairs with the in-chat Progress Narration for end users who want visibility outside the terminal.
- **Release automation** (`.github/workflows/release.yml`). On every PR merge to `main`, the workflow bumps `plugins/gh-gemini-review-loop/.claude-plugin/plugin.json` per the PR's `release:major` / `release:minor` / `release:patch` label (default patch, opt-out via `release:skip`), tags the commit, and creates a GitHub Release whose body is the merged PR's description verbatim. Aborts cleanly if the target tag already exists. Sibling workflow `setup-labels.yml` (manual `workflow_dispatch`) creates the four `release:*` labels in fresh forks.
- **CONTRIBUTING.md** documents the label semantics, release-notes flow (PR body → GitHub Release body), local lint/test commands, and the stacked-PR footgun we hit earlier.
- **Progress Narration** section in SKILL.md. Requires the agent to emit one-line status updates (`[loop] cycle N/3 — <phase>`) at each phase transition during the loop. No code change — pure agent-instruction layer. Eliminates the "silent loop feels broken" UX problem in interactive sessions.
- `--min-severity {critical,high,medium,low}` flag and `filter_by_min_severity()` helper. Drops actionable threads below the chosen severity. By default, threads with no Gemini priority marker (`unknown`) are kept; pass `--drop-unknown-severity` to remove them too.
- `filter_by_min_severity` now accepts `min_severity=None`, enabling `--drop-unknown-severity` to be used independently of `--min-severity` (closes a silent no-op).
- **Variations** section in SKILL.md mapping common user phrasings ("only fix high severity", "audit-only run", "be persistent") to the right script flag combinations. Makes agent dispatch deterministic.
- 8 new pytest cases for `filter_by_min_severity` (covering each severity threshold, unknown handling, stability, empty input). Test count: 36 → 44.

### Changed

- README rewritten around the solo-builder / small-team workflow, with install and natural-language usage up front and configuration details moved lower in the document.

## [0.1.0] - 2026-05-24

First publishable release. Packaged as a Claude Code plugin marketplace.

### Added

- **Plugin packaging.** Repo is now a single-plugin marketplace (`.claude-plugin/marketplace.json`, `plugins/gh-gemini-review-loop/.claude-plugin/plugin.json`). Install via `/plugin marketplace add OrenAshkenazy/gh-gemini-review-loop` then `/plugin install gh-gemini-review-loop@gh-gemini-review-loop`.
- `ADDRESSED_BY_REPLY` detection and auto-resolution. Unresolved threads where a non-bot maintainer posted a substantive (≥30 char) reply are treated as deliberate deferrals; the loop never re-attempts those fixes and resolves the threads via GraphQL on the next pass.
- `--dry-run` flag. All GraphQL mutations (`resolveReviewThread`, `gh pr comment` for receipts) route through a single choke point that logs intended writes to stderr instead of executing.
- Pagination guard. Warns when `reviewThreads` / `reviews` / PR comments hit the 100-item page limit or when per-thread comments hit 50, surfacing silent data loss on large PRs.
- Severity-aware ordering. Parses Gemini's priority image markers (`critical` / `high` / `medium` / `low`) and orders actionable threads with critical first.
- Agent-scoped cycle counter. Auto-detects the agent's GitHub login via `gh api user` and only counts re-review requests posted by that login toward the 3-cycle cap. Humans pinging Gemini no longer consume cycles. Override with `--agent-login NAME`, opt out with `--no-agent-filter`.
- `--post-receipt` flag. Posts a one-comment audit trail to the PR: cycles used, threads resolved, threads pending, severity breakdown.
- Pytest suite (36 tests) covering all pure functions: `parse_pr_url`, `is_addressed_by_reply`, `filter_threads`, `thread_severity`, `sort_by_severity`, `severity_counts`, `rereview_requests`, `pagination_warnings`, `thread_fingerprint`, `render_receipt`.
- GitHub Actions CI: ruff + py_compile + pytest on Python 3.10 / 3.11 / 3.12.
- MIT LICENSE.

### Changed

- `--ignore-loop-limit` renamed to `--resolve-past-cap` (old name was misleading; only re-enabled resolution past the cap, didn't ignore the cap entirely). Old flag retained as a hidden deprecated alias.
- SKILL.md description tightened from ~440 chars to ~270 chars while preserving trigger phrases.
- Script path in SKILL.md uses `$CLAUDE_PLUGIN_ROOT/...` so the same skill works under both plugin install and legacy `~/.claude/skills/` install.

### Fixed

- Markdown renderer now displays the addressed-by-reply count alongside outdated and actionable counts.

[Unreleased]: https://github.com/OrenAshkenazy/gh-review-loop/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OrenAshkenazy/gh-review-loop/releases/tag/v0.1.0
