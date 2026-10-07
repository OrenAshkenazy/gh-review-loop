# Question shapes GLG uses

Observed on GLG screening pages and follow-ups. Map each to a bank or profile value; the rules for what counts as a source are in SKILL.md.

| Shape | Looks like | Fill from | Watch for |
|---|---|---|---|
| Familiarity grid | rows = vendors, columns = `1 - Not Familiar` to `5 - Very Familiar` | bank grid value for the same question family, else profile vendor lines | a vendor absent from both is `NEEDS MEMBER`; "Others (please specify below)" needs the free-text box filled too |
| Customer-status grid | rows = vendors, columns = `Current Paying Customer`, `Recent Customer (0 -12 months)`, `Former Customer`, `Evaluated`, `Familiar` | profile vendor status | statuses age: a "Recent Customer" answer older than 12 months in the bank is stale, ask |
| Single select | "Were you involved in the procurement / evaluation process? Indicate the role" | profile procurement role | some pages add a free-text "please elaborate" under the select |
| Free-text group | one heading, several sub-questions each with a text box (use cases, time using the platform, decision factors) | bank sub-question values when the wording matches, else member | the bank stores these as `{"Sub question": ["answer"]}`; match sub-questions individually |
| Numeric fact | headcount, years, last RFP month | profile organisation facts | GLG shows `(MM/YY)` hints; keep the member's format if they gave one |
| Yes / No attestations | current employer, public-company employment, conflicts, consent to recording | member, every time (see compliance.md) | never from the bank |
| Availability | preferred call windows, time zone | member | out of scope for the bank; ask in chat |

## Mapping rules

- Match on the question family, not exact wording. "Please rank your familiarity on a scale of 1-5" and "How familiar are you with the following vendors" are the same family; the bank's normalized lookup handles most of this, and you check the top hit makes sense before using it.
- Grid values are per row. Reuse a row only when the vendor name matches (case-insensitive, ignoring punctuation). Do not transfer a score from one vendor to another.
- Scale labels must be copied exactly as the page shows them, including GLG's spacing quirks like `Recent Customer (0 -12 months)`.
- When the page offers "Others (please specify)" and the profile lists vendors not in the grid, propose them for the free-text box and mark the row `PROFILE`; the member confirms on the sheet.
- A free-text answer in the bank that mentions a timeframe ("6 months ago") drifts; show the stored text with its `recorded_at` and let the member adjust.
