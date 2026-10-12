# Terminal thread breakdown (when `remaining_actionable > 0`)

After printing the `[loop] Summary` block, when remaining actionable threads exist, render them in **three separate buckets** — never a single mixed "for human review" table.

**Always reference a thread by its GitHub comment URL** (e.g. `https://github.com/<owner>/<repo>/pull/<n>#discussion_r3374837147`) — the receipt's `Findings` block prints one per finding. Never surface a bare `discussion_r…` / GraphQL node token; add `file:line` when useful.

**Classification (in priority order):**

1. Judge verdict `needs_human` → bucket 1.
2. Thread from a prior reviewer pass where you applied a code fix at that file/line this session → bucket 3.
3. All other `valid_actionable` threads (new from latest review, or no fix attempted) → bucket 2.

**Omit any bucket with zero entries** — never print an empty bucket header.

## Bucket 1 — Human decision required

Product/format/design calls, not code changes.

```
Human decision required

1. <file>:<line> · <GitHub comment URL>
   Finding: <what the reviewer flagged, verbatim or closely paraphrased>
   Why human: <concrete reason — format consistency, security policy, product behavior tradeoff>
   The agent did not auto-fix this because <specific reason>.
   Options:
   - <option A>
   - <option B>
```

Example:

```
Human decision required

1. main.py:828 · https://github.com/Owner/Repo/pull/9#discussion_r3369882171
   Finding: OWASP tags appear in console and JSON reports, but not in Markdown.
   Why human: this changes report format behavior. Both choices are valid.
   The agent did not auto-fix this because it is a product decision, not a safe mechanical fix.
   Options:
   - Add OWASP tags to Markdown output for consistency.
   - Keep Markdown simpler; document that OWASP tags are JSON/console only.
```

## Bucket 2 — Remaining because cap was reached

`valid_actionable` findings the loop ran out of cycles for. **Do not label these "human review"**, do not downgrade their severity, and do not say "low priority" unless the judge, reviewer, or user did.

```
Remaining because cap was reached

1. <file>:<line> · <GitHub comment URL>
   Finding: <one-sentence description>
   Judge: valid_actionable (conf <N>)
   Reason not fixed: cap reached
   Suggested handling: fix in next PR, or bump cap and re-run the loop
```

## Bucket 3 — Already fixed but still unresolved on GitHub

Fix applied this loop, thread still UNRESOLVED. Await reviewer confirmation; an OUTDATED anchor alone does not establish that the finding was fixed.

```
Already fixed but still unresolved on GitHub

1. <file>:<line> · <GitHub comment URL>
   Finding: <what the reviewer originally flagged>
   Status: fix applied in this session
   Why still shown: the anchor may become OUTDATED; reviewer confirmation is still required
```

## After the buckets

Use the deterministic `Next options:` section emitted by the terminal summary formatter. Do not hand-write the options unless the script fails.
