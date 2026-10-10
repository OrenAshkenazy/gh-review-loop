# Profile and answer bank

Both live under `~/.config/glg-questionnaire/` (override with `GLGQ_STATE_DIR`). Nothing in this directory is transmitted anywhere; the scripts only read and append locally.

The scripts create that directory `0700` and their own files `0600`, and tighten a looser mode they find. `profile.md` is written by you rather than by a script, so create it the same way (`umask 077` before the write, or `chmod 600` straight after): it names the member's employer and vendor relationships, and on a shared machine the default `022` umask would leave it readable by every local account. None of these files may be committed.

## profile.md (member-written, bootstrap on first run)

The profile is the member's standing facts, written in their words. On first run, when the file is missing, interview the member and write the file from their answers, then show it and ask them to confirm it line by line. Do not prefill from LinkedIn or guesswork. Sections:

```markdown
# GLG profile

## Identity and employment
Name, current employment status (employed / founder / independent), current employer name if any.
Why it matters: the member may not consult about a current employer, and GLG asks.

## Standing rate
USD per hour as agreed with GLG network member support.

## Expertise (strong / working / avoid)
One line per area, e.g. "observability platforms: strong (buyer and operator)".

## Vendors (name: status, since, role)
e.g. "Datadog: current paying customer, 6 years, decision maker".
Status vocabulary matches GLG's grids: Current Paying Customer, Recent Customer (0 -12 months), Former Customer, Evaluated, Familiar, Not Familiar.

## Procurement roles
Which role the member claims and when: Key Decision Maker, Influencer, End User, Not Involved.

## Organisation facts
Headcount band, industry, region, last RFP dates, other vendors evaluated.

## Decline rules
Topics, client types or conditions the member always declines (e.g. "recorded calls: ask me", "single-blinded: decline", "anything about <current employer>").
```

Everything in the profile is a `PROFILE` source on the answer sheet **except the attestation classes** — current employer, conflicts, recording consent, blinded clients, pending trials, government roles, public-company employment within six months, other GLG projects. Those profile fields exist for triage (judging fit, applying the decline rules) and never fill a questionnaire row: employment changes, and a stale answer there is a false compliance attestation. On the sheet they are `NEEDS MEMBER` every time, exactly as if the profile did not hold them. If a question needs a fact the profile lacks, ask, then offer to add it to the profile.

## answers.jsonl (append-only, one JSON object per line)

```json
{"question": "Please highlight your familiarity as a customer of these platforms.",
 "normalized": "highlight familiarity customer platforms",
 "value": {"Datadog": ["Current Paying Customer"], "Grafana": ["Current Paying Customer"]},
 "note": "Groundcover - design partner",
 "source": "glg_accept_confirmation",
 "project": "Observability Software Platforms | Customer Perspective",
 "recorded_at": "2026-03-30T09:20:00+00:00"}
```

`source` is `glg_accept_confirmation` (seeded from what GLG recorded) or `member` (typed in chat during a session). Both are the member's own words; the distinction is only for the audit trail.

## Seeding from past confirmations

Search Gmail for `from:glgroup.com subject:"Accept Confirmation"`, fetch each thread as plain text, parse the first message, save the JSON and seed:

```bash
# In $GLGQ_WORK (see SKILL.md): owner-only, outside the repo, removed after use.
python3 .../scripts/parse_glg_email.py \
  --subject-file "$GLGQ_WORK/subject.txt" --file "$GLGQ_WORK/body.txt" > "$GLGQ_WORK/parsed.json"
python3 .../scripts/answer_bank.py seed --from-parsed "$GLGQ_WORK/parsed.json"
```

Seeding dedupes on normalized question plus value, so re-running is safe. Do this once on first run and again whenever a new confirmation arrives.

## Lookup semantics

`lookup --question-file <file>` (write the question to a file; never paste questionnaire text into a command line) normalizes both sides (lowercase, alphanumeric tokens, stopwords removed) and ranks by sequence similarity, default threshold 0.55, newest first on ties. A grid question matches as a whole; take the per-vendor values from `value` and only reuse rows whose vendor appears in the new grid. A vendor the bank has never seen is `NEEDS MEMBER`, not a guess.

## submissions.jsonl

`answer_bank.py log --project "<title>" --cpid <id> --action submitted|declined|accepted|skipped --detail "..."` appends one line per outcome. Read it at the start of triage so a project already handled in an earlier session is shown as done rather than offered again.

## Per-project attestations are refused, not stored

`add` raises and the CLI exits 3 for a question about current employment, a conflict of interest, recording consent, a blinded client or non-public information; `seed` leaves such an answer out of the bank and reports it under `skipped` so you can see GLG recorded one and still ask the member for this project's. This is enforced in the script, not left to memory: a stored copy would come back from `lookup` as a `BANK` suggestion for exactly the questions the member must answer every time.
