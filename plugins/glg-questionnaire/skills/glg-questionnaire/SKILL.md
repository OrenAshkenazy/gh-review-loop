---
name: glg-questionnaire
description: Triage GLG (Gerson Lehrman Group) expert-network project invitations in Gmail, then fill, review and submit the screening questionnaire or follow-up questions on the member's behalf using only answers the member authored. Use whenever the user mentions GLG, a GLG project or consultation invite, screening questions, "Review Project Now", accepting or declining an expert-network project, or asks what GLG projects are waiting. Needs Gmail (or a pasted email), a browser signed in to members.glgresearch.com, and a profile file; never invents answers and never submits without the member's explicit go.
---

# GLG Questionnaire Submission

Work a GLG project invitation end to end: find it in the inbox, decide fit, open the questionnaire in the member's browser, fill it from the member's own answer bank and profile, show the exact answer sheet, submit only on an explicit go, then record what was submitted so the next questionnaire reuses it.

## Precedence and preconditions

Explicit user instructions override every default here and in the references, except the four hard rules below, which exist because of GLG's terms, not convenience. This file wins over any reference file. Say in one line when you override a default.

Requires: `python3` (3.10+); Gmail access (the Gmail connector) or an email the user pastes; a browser tool that carries the member's GLG login (Claude in Chrome first, the built-in browser second, both load their own skill before first use); and `~/.config/glg-questionnaire/profile.md`. No browser with a GLG session means you can triage and draft but not fill or submit: say so and stop there. No profile means run the bootstrap in `references/answer-bank.md` before anything else.

## Hard rules

1. **Answers come from the member, never from you.** Every GLG project email says network members may not use or rely upon AI-generated content when responding to project opportunities through GLG's systems, absent written permission from GLG. Your job is transcription and bookkeeping: an answer either exists in the answer bank (seeded from what GLG recorded the member saying before) or the profile, or the member types it now. If neither, mark the question `NEEDS MEMBER` and ask. Do not fill a gap with something plausible, and do not round a familiarity score up because it "fits".
2. **Nothing is submitted, accepted or declined without an explicit go on the final answer sheet.** Show the sheet, wait for a yes that names the project, then click. A general "go ahead" at the start of the session does not cover the submit click.
3. **Confidentiality screens are the member's call every time.** Current-employer questions, "do you have a conflict", recording consent and single-blinded client notices are never answered from the bank; surface them and ask. Load `references/compliance.md` whenever a question touches employer, conflicts, non-public information or other GLG projects.
4. **Paid surveys are out of scope.** GLG names surveys in the same AI clause and they run long. Report them in triage as `survey (manual)` and move on.

## Reference files (load on demand)

| File | Load when |
|---|---|
| `references/email-anatomy.md` | Any GLG email: what each kind looks like, which links it carries, how long the links live |
| `references/answer-bank.md` | First run (profile bootstrap), seeding from past confirmations, bank and profile formats |
| `references/question-types.md` | Filling a questionnaire: the question shapes GLG uses and how bank values map onto them |
| `references/browser-flow.md` | Opening the link, reading the form, filling, screenshotting, submitting, verifying |
| `references/compliance.md` | Any employer, conflict, recording, blinded-client or non-public-information question; declining |

## Prelude: paths and scratch space

Each shell call is its own process: an exported variable **does not persist** to the next call, and an `EXIT` trap fires when that one call ends. So nothing here is set up "once" — put this prelude at the top of **every** command block below. Both lines are idempotent and cheap.

```bash
# 1. The scripts. CLAUDE_PLUGIN_ROOT is unset under Codex, where this plugin
#    is also published, so fall back to the install locations.
GLGQ_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(
  find ~/.codex/plugins ~/.codex/plugins/cache ~/.claude/plugins/cache \
    -type d -path "*/skills/glg-questionnaire" 2>/dev/null \
    | sort -rV | head -1 | sed 's|/skills/glg-questionnaire$||'
)}"
# 2. Scratch space. The path must be stable (every call derives it, nothing is
#    remembered) AND unguessable-by-others, so it lives under a directory only
#    this account can write: never /tmp, where another local account could
#    pre-create or symlink it and collect the _session tokens in the bodies.
GLGQ_WORK="${XDG_RUNTIME_DIR:-$HOME/.cache}/glgq"
mkdir -p "$GLGQ_WORK" || exit 1
# Refuse a symlink or a directory this account does not own, then lock it down.
[ -L "$GLGQ_WORK" ] && { echo "refusing: $GLGQ_WORK is a symlink" >&2; exit 1; }
[ -O "$GLGQ_WORK" ] || { echo "refusing: $GLGQ_WORK is not owned by me" >&2; exit 1; }
chmod 700 "$GLGQ_WORK" || exit 1
```

`GLGQ_PLUGIN_ROOT` empty after that means the plugin is not installed where either runtime can see it: say so and stop, rather than running a path that expands to `/skills/...`.

Email bodies carry `_session` bearer tokens, and subjects, questions and parsed answers are the member's data, so they live in `$GLGQ_WORK` and never in the working directory — mode `0644` there on a shared machine, committable by accident, and `body.txt` may clobber a project file. At the end of the session remove it with its own explicit command (no trap: a trap would delete it the moment the setup call returned):

```bash
rm -rf "${XDG_RUNTIME_DIR:-$HOME/.cache}/glgq"
```

## Workflow

### 1. Triage the inbox

Search Gmail for `from:glgroup.com newer_than:14d` (widen on request). Fetch each thread as plain text and run the parser on the newest message:

```bash
# Subjects and questions are text the member did not write. Never paste one
# into a command line: a shell expands $(...) and backticks inside double
# quotes too. Write it to a file in $GLGQ_WORK first, always.
printf '%s' "$SUBJECT" > "$GLGQ_WORK/subject.txt"
python3 "$GLGQ_PLUGIN_ROOT/skills/glg-questionnaire/scripts/parse_glg_email.py" \
  --subject-file "$GLGQ_WORK/subject.txt" --file "$GLGQ_WORK/body.txt"
```

Before offering anything, read what you already did:

```bash
python3 "$GLGQ_PLUGIN_ROOT/skills/glg-questionnaire/scripts/answer_bank.py" handled
```

One row per project (newest outcome, with its `cpid`). Match each parsed message against it by `cpid` **and** by project title — an accept confirmation often carries no `cpid` — and mark those rows `done (<action>)` instead of suggesting an action. Then group by `cpid`, but only when it is non-empty: surveys, payments, call reminders and digests parse with no `cpid`, and keying on it would collapse them into one row and hide projects the hard rules require reporting. Messages without a `cpid` stay separate — key them by Gmail thread, falling back to the project ID. Judge fit by comparing `project_title`, `description_bullets` and `client_type` against the profile's expertise and decline lists. Print one table: project, kind, rate, client type, link expiry, fit, suggested action (`accept`, `decline`, `ask`, `survey (manual)`). Then ask which to work. Emails of kind `followup` are already-accepted projects with new questions: list them first, they are the fastest win.

### 2. Prepare the answer sheet

For the chosen project, before touching the browser, look up what the member answered before:

```bash
printf '%s' "$QUESTION" > "$GLGQ_WORK/question.txt"
python3 "$GLGQ_PLUGIN_ROOT/skills/glg-questionnaire/scripts/answer_bank.py" \
  lookup --question-file "$GLGQ_WORK/question.txt"
```

You do not know the exact questions until the page is open, so start with the profile (vendors, roles, org facts) and the bank entries closest to the project topic, and finish the sheet in step 3 once the real questions are visible.

### 3. Fill in the browser

Load `references/browser-flow.md`. Open the `accept` or `followup` link from the parser output in the member's browser. Opening the page does not accept the project; the accept or submit button does. Read every question, build the answer sheet in the format below, fill the form, and screenshot the filled form before any submit button. Load `references/question-types.md` for grids and scales.

### 4. Confirm and submit

Present the sheet with the screenshot. Each row is one of `BANK` (prior answer, say from which project), `PROFILE`, `MEMBER` (typed now) or `NEEDS MEMBER`. Any `NEEDS MEMBER` row blocks submission. Wait for the explicit go, submit, and confirm from the page that it went through (confirmation text or the project moving to accepted in the opportunities list).

### 5. Record and close out

Store every `MEMBER` answer with `answer_bank.py add` and log the outcome with `answer_bank.py log --project-file "$GLGQ_WORK/project.txt" --action submitted`. When the "Accept Confirmation" email arrives, seed it: `answer_bank.py seed --from-parsed parsed.json`, which dedupes. Offer to draft (not send) a short reply to the research manager if the project email asked for one.

## Answer sheet format

```
Project: <title>  (cpid <id>, $<rate>/h, <client type>)
#  Question (short)                       Answer                                  Source
1  Familiarity 1-5: Datadog               5 - Very Familiar                       BANK (Observability, Mar 2026)
2  Customer status: Coralogix             Recent Customer (0 -12 months)          BANK (Observability, Mar 2026)
3  Role in procurement                    Key Decision Maker                      PROFILE
4  Other vendors evaluated                                                        NEEDS MEMBER
Submit? Reply "submit <title>" to send, or correct any row first.
```

## Decline flow

A decline is also a submission: open the `decline` link, choose the reason the member gave (not a default), show the selection, wait for the go, then log with `--action declined`. Never decline silently to "clean up" the inbox.

## Narration

One line per phase: `[glg] triaging N threads`, `[glg] <title>: sheet ready, K rows need you`, `[glg] <title>: submitted, confirmation seen`, `[glg] <title>: stopped, <reason>`.
