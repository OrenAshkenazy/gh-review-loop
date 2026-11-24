# Deterministic sweep replay

```bash
python3 evals/replay/replay.py
```

No network, no arguments, stdlib only. Anyone can reproduce both results.

## What is here

Reviewer findings captured from [PR #67](https://github.com/OrenAshkenazy/gh-review-loop/pull/67)
and PR #129, and the `loaders/` source they were written against, vendored into
`fixtures/src/` so the replay does not depend on the demo branch still existing.

| File | Contents |
|---|---|
| `fixtures/run1.json` | Sourcery's review of `b391162` — 2 findings |
| `fixtures/run2.json` | Sourcery's review of `67551bd` — 1 finding |
| `fixtures/src/loaders/` | The two reviewed files, at that commit |
| `fixtures/codex.json` | Codex's two findings on [PR #129](https://github.com/OrenAshkenazy/gh-review-loop/pull/129) |
| `fixtures/src-codex/loaders/` | The two files Codex reviewed, at `24d2025` |
| `expected_output.txt` | Committed output of `replay.py`; `tests/test_replay.py` asserts it |

Bodies, paths, line anchors and author login are copied verbatim from the
GitHub REST API for the recorded comment IDs (`3721865840`, `3721865850`,
`3731481473`). The `login` recorded is `sourcery-ai`, which is what the GraphQL
API and the fetcher report; the REST API names the same bot `sourcery-ai[bot]`.
Run 1's first body ends with a `✅ Addressed in 08c5a02…` line that Sourcery
appended after the finding was fixed — it is part of the stored comment and is
kept rather than trimmed.

`loaders/profiles.py` and `loaders/bundle.py` each contain three instances of
`data = json.loads(path.read_text(encoding="utf-8"))` followed by a
`return data.get(...)`, six in total.

The Codex capture is two comments (`4687098217`, `4687119806`) read from the
GraphQL `reviewThreads` of PR #129, with their bodies, two-line ranges and
`P2` badges as posted.

## The three results

**Run 1 produces one shape-merged cluster and a sweep.** Its two findings are
worded differently, so clustering on prose alone yields two patterns of one
site each. Clustering with the source root yields one pattern of two sites
carrying a `shape:` signature, which reaches the two-site minimum, and the
sweep reports 3 unflagged sites matching the same shape.

**Run 2 produces one cluster of one site and correctly no sweep.** One finding
is one pattern at one site, below the two-site minimum, so the sweep does not
run.

**The Codex capture produces one shape-merged cluster and the same sweep.**
Codex flagged a different failure in each file: a missing file in
`profiles.py:19-20`, a legacy list-shaped JSON in `bundle.py:18-19`. Prose
alone keeps them apart. Both anchor to the same two lines of code, so they
merge into one pattern of two sites, and the sweep reports 3 unflagged sites.
This capture is what the README demo is built from
(`docs/demo/build_demo.py`).

How the Codex capture came about, since it is not one clean review:

- Asked to review PR #67 as it stood, Codex reported no issues. The six
  loaders had no stated contract to violate.
- PR #129 is the same six loaders with the contract written into the
  docstrings. Reviewing `eb662b6`, Codex posted one finding, on the first
  loader, and named the other five in prose.
- `24d2025` gave the bundle readers a second, different contract. Codex's
  review of that commit added the `bundle.py` finding.

The fixture holds the two open findings a loop run on PR #129 would fetch.
The sweep does not report the sixth site, `read_lockfile`: it returns
`data.get("pinned", [])`, which does not share the `{})` token.

## Why the fixtures are two runs and not one

The same reviewer produced both, against identical file contents — `67551bd` is
`b391162` re-pushed after a rebase. It returned two findings the first time and
one the second. Finding prose is therefore not a stable key for grouping
findings; the code the findings anchor to is.

Run 2 is also the reason this directory exists rather than a screenshot of a
live loop: a live run shows whichever sample the reviewer happened to emit.
