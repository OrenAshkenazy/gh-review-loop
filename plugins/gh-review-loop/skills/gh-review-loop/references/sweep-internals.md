# Sweep internals (`sweep_siblings.py`)

Load this when the cycle receipt shows a multi-site pattern (`count >= 2`), a single finding anchored to a multi-line range, or a `Clustering:` singleton advisory. The core rules (report-then-go, changed files only, `status` handling, `--swept-pattern` marking) are in SKILL.md — this file covers how the sweep actually matches, so reports can be interpreted correctly.

## Clustering

Clustering runs with the repository root supplied, so findings anchored to spans of the same code shape merge even when the reviewer worded them differently ("exception-wrap" at one site, "add error checks" at another). It unions tokens from every code line in a review range, and falls back to the end line when a reviewer supplies no range start. Those clusters carry a `shape:` signature. Without the merge, each site looks like a singleton and never reaches the sweep's two-site minimum.

## Invocation

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/sweep_siblings.py" \
  --signature <sig> --label "<label>" \
  --site <path:line> --site <path:line> \
  --changed-file <path> [--changed-file <path> ...] --json
```

A single multi-line finding uses its range from the receipt: `--site <path:start-end>`.

Pass every site from the cluster and every file in the PR's diff. The script intersects the tokens of the flagged lines and reports only lines containing all of them — a candidate must match what the flagged sites have in *common*. At least two shared tokens must contain letters; punctuation constrains candidates but cannot qualify a pattern by itself. It reports; it never edits.

## Token vs mirror candidates

Multi-line ranges also enable an exact duplicate-block sweep from one flagged site: the script fingerprints the block and searches the changed files for the same sequence. Those are `mirror` candidates; ordinary intersection hits are `token` candidates. `mirror` describes the evidence source, not a path relationship — it does not infer filename twins. A mirror can return `ok` from one ranged site; token candidates still require two sites.

## Mirror match modes (`matchMode`)

- **`exact`** — default for every language. Blocks match only when their text is identical. No comment stripping, no whitespace collapsing, no per-language knowledge.
- **`normalized`** — explicitly supported languages only, currently **Python alone**. Comments are removed with Python's own `tokenize` module, so blocks differing only in comments still match. Whitespace is *not* collapsed (indentation is semantic). Tool directives are *kept* — they change what mypy, flake8, bandit, Cython or coverage do — matched by **shape, not a tool list**: a lowercase leading word followed by `:` or `=` (`# cython: boundscheck=False`, `# type: ignore[arg-type]`), plus a few bare ones like `# noqa`. Case separates directive from prose (`# Note: …` stays a comment). Lowercase prose shaped like a directive is treated as meaningful — losing a duplicate rather than inventing one.

The modes never mix, and a block is only compared against files of its own language family (`.ts` against `.js`, `.yml` against `.yaml`; never `build.sh` against `Makefile`). A Python file that does not tokenize falls back to `exact`.

**Normalized matching never crosses files.** Once comments are dropped, identical text can still mean different things in two files (a `.pyi` stub vs a runtime module, differing shebangs, source encodings, `from __future__` state). Within one file every such property is equal by construction. Cross-file duplicates are still reported — by raw byte-exact matching, which claims only that the bytes repeat. So a Python file carries **two** indexes: comment-blind within itself (`normalized`), byte-exact across its family (`exact`). The raw index keeps the tokenizer's string tags, so a docstring's body cannot match the code it quotes; a `.py` file that does not tokenize is never compared against one that does.

This is a deliberate precision-over-recall trade: an advisory report that fires falsely stops being read. **A duplicate in an unsupported language that differs only by a comment will not be reported** — expected, not a bug. Adding a language means adding a real tokenizer for it.

Two accepted limitations, both in `exact` mode: a block can match text in a different lexical context (e.g. inside a JavaScript template literal — Python is unaffected because `tokenize` identifies string bodies), and line endings are not compared (an LF block matches a CRLF block).

## Path safety

The changed-file scope is enforced, not assumed: a path from the PR may be a symlink, spell `../`, or be absolute. Each is refused before any read, so a PR cannot make the sweep quote a file it never touched.

## Manual sweep for singleton clusters

When `Clustering:` warns that three or more findings all form singleton clusters (likely prose-hash fallback signatures), run this changed-files-only recovery **before editing**:

```bash
# Use the same selected PR URL passed to --pr, even from another checkout.
PR_URL='https://github.com/OWNER/REPO/pull/123'

# Establish the complete, reviewable scope for that PR.
gh pr diff "$PR_URL" --name-only

# For each singleton, choose one stable identifier or code fragment from its
# body/anchor, then pass only paths printed by the command above.
rg -n --fixed-strings -e '<stable identifier or code fragment>' -- \
  path/from/changed-list another/path/from/changed-list
```

Repeat the `rg` search for every singleton and report (a) the complete changed file list inspected, (b) each fragment searched, and (c) every suspected sibling before editing. Do not combine unrelated singleton sites as inputs to `sweep_siblings.py`; its two-site intersection is reserved for actual multi-site clusters.

The same `Clustering:` guard appears on the initial thread fetch and repeats in the publisher's cycle receipt. JSON fetches expose it under `clustering` as `clusterCount`, `maxClusterSize`, and `advisory`.

## Advisory receipt lines

The receipt's `Clustering:` and `Convergence:` lines are advisory. Three or more singleton clusters → `Clustering:` warns the signatures are likely prose-hash fallbacks and requires the manual singleton sweep procedure above before fixing. A `⚠ … RECURRED after sweep` means the sweep missed a variant — decide whether to refine, stop, or continue. Neither line changes control flow; the cap remains the only hard stop.
