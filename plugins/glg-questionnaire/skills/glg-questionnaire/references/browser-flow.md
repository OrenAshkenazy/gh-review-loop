# Browser flow

The questionnaire lives behind the member's GLG login, so it is filled in a browser that already holds that session. Pick in this order and load the matching skill before the first call:

1. **Claude in Chrome** (`mcp__claude-in-chrome__*`): the member's real Chrome with their GLG cookies. Preferred.
2. **Built-in browser** (`mcp__Claude_Browser__*` or `mcp__remote-devices__Claude_Browser__*`): works if the member signed in to members.glgresearch.com there before.
3. Neither available: stop after the answer sheet draft and tell the member what is ready and what is missing. Do not try to fetch the link with a plain HTTP tool; the `_session` token would sign the agent in from a server the member never approved.

## Steps

1. **Open** the `accept` or `followup` URL from the parser in a new tab. If it lands on a login page the link expired; ask the member to sign in, then navigate to Opportunities and find the project by title.
2. **Read before touching.** Read the page as text and list every question, its shape and its options verbatim. Compare against the email: the project title and rate on the page should match the parser output. A mismatch means the wrong project, stop and say so.
3. **Build the sheet** (format in SKILL.md), resolving each row to BANK, PROFILE, MEMBER or NEEDS MEMBER. Ask for all NEEDS MEMBER rows in one message, not one at a time.
4. **Fill.** For grids, select per row and re-read the row after selecting; GLG grids sometimes drop a selection when the next row is clicked. For free text, paste exactly what the sheet shows. Leave attestation checkboxes untouched until the member answers them explicitly.
5. **Screenshot** the whole filled form (scroll and capture each section) and attach it to the sheet.
6. **Wait** for the go that names the project.
7. **Submit.** Click once. Watch for validation errors (required rows, unchecked attestations); fix only from the sheet, re-screenshot and re-confirm if anything changed.
8. **Verify.** Look for confirmation text, or open Opportunities and confirm the project shows as accepted or the follow-up as answered. Report what you saw, not what you expect.

## Decline

The `decline` link opens the opportunities list for that consultation. Pick the reason the member gave (common ones: not my area, conflict with current employer, rate, no availability), show the selection, wait for the go, click, verify the project leaves the open list, then log it.

## Dialogs and side effects

- Never accept a terms-and-conditions or compliance-tutorial prompt on the member's behalf; report it and stop. GLG requires those annually and they are the member's signature.
- Do not open the "Download GLG for Experts" links or calendar links; they are not part of the questionnaire.
- Do not use the browser's saved form data or autofill suggestions; everything comes from the sheet.
