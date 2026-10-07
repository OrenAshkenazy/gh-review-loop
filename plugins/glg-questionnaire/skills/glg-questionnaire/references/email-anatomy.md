# GLG email anatomy

Everything below was observed on real GLG messages to a network member. Subjects and bodies vary by research manager, so the parser keys on links and markers before subjects.

## Kinds the parser reports

| `kind` | How it is recognized | Carries |
|---|---|---|
| `invitation` | an `/accept/accept?cpid=...` link; subjects like `<Topic> consultation - GLG`, `GLG - *URGENT* new project on <Topic>`, `GLG - We Think You're a Fit to Discuss "<Topic>"`, `In case you missed it, we have a GLG project for you`, `GLG - Just tried giving you a call` | accept link, decline link, rate, client type, description bullets, flags |
| `followup` | a `/cm-accept-followup/?cpId=...` link; subject `GLG: Follow up on your acceptance for <Topic>` | one or more extra questions for a project already accepted |
| `accept_confirmation` | `Q:` / `A:` pairs in the body; subject `GLG: Accept Confirmation – <Topic>` | the exact answers GLG recorded; seed the bank from these |
| `digest` | `You have open GLG projects waiting` | pointers only; open the member portal instead |
| `survey` | `Please take this Paid Survey`, `GLG: Reminder - Survey in Progress` | out of scope, report as manual |
| `call_request`, `call`, `call_reminder`, `payment` | scheduling, calendar and invoicing traffic | nothing to answer here |

A reminder ("In case you missed it") carries the same `cpid` as its original; group on `cpid` so each project appears once in triage.

## Links

- **Accept**: `https://members.glgresearch.com/accept/accept?cpid=<id>&_session=<JWT>&outreachId=<n>`. Opens the project page with the screening questions. Acceptance is the explicit button on that page, not the page load.
- **Decline**: `https://members.glgresearch.com/opportunities//projects?consultationIds=<JWT>&_session=<JWT>`. Opens the opportunities list filtered to this consultation with a decline control and reason picker.
- **Follow-up**: `https://members.glgresearch.com/cm-accept-followup/?cpId=<id>&_session=<JWT>`.

The `_session` JWT signs the member in for the link's lifetime. The parser decodes its `exp` (no verification) into `accept_link_expires_at`. Observed lifetimes: 14 days for accept and decline, 5 days for follow-ups. An expired link lands on the login page; the member can still sign in and find the project under Opportunities.

## Fields worth reading in triage

- `hourly_rate_usd` from "your hourly rate for GLG consultations is $N". Absent on some outreach; the profile holds the standing rate.
- `client_type` from "One of our clients, an associate at an investment management firm, would like...". Tells the member who is on the other side when the client is not named.
- `flags.single_blinded`: the client entity stays confidential. The email says to contact a research manager or decline if the member cannot clear conflicts without the name.
- `flags.recorded`: "This client has elected to record this phone consultation. By accepting this consultation, you consent to being recorded." Accepting is consenting; surface it.
- `flags.client_terms_appended`: a client's own representations block is appended (public-company employment, government roles, trial involvement, MNPI). These are attestations the member makes by accepting; show the block, do not summarize it away.
- `flags.ai_use_prohibited`: the standard clause is present (it is on every project email seen so far).
- `description_bullets`: the client's actual question list when the research manager included one. Best single signal for fit.

## Accept Confirmation answer encoding

GLG records answers as JSON-ish text after `A:`:

- grid questions (vendor rows with a scale or status column) as `{"Vendor":["5 - Very Familiar"], ...}`
- single or multi select as `["Key Decision Maker"]`
- free-text sub-questions as `{"Sub question text":["answer"], ...}`
- a trailing ` - note` after the JSON is the member's free-text addendum

The parser returns `value` (parsed JSON when possible) and `note` separately so the bank can match on the structured part.
