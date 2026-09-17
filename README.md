# gh-review-loop

**Your AI reviewer flagged two instances of a bug. There are five. This fixes all five, runs your tests, and stops after three rounds.**

[![CI](https://github.com/OrenAshkenazy/gh-review-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/OrenAshkenazy/gh-review-loop/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/OrenAshkenazy/gh-review-loop?sort=semver)](https://github.com/OrenAshkenazy/gh-review-loop/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

![Terminal demo of gh-review-loop clearing AI reviewer feedback on a PR](docs/gh-review-loop-demo.gif)

A skill for **Claude Code** and **Codex** that clears AI reviewer feedback on your PR without you leaving the terminal. It reads real GitHub review-thread state, fixes what's actionable, **sweeps the sibling instances the reviewer didn't flag**, gates every push on your repo's own tests, and asks for re-review, hard-capped so it can't spam the PR.

Works with whatever AI reviewer assistance your repo already has. MIT, no server, no account, no vendor plan.

## Install

**Claude Code** — two slash commands:

```
/plugin marketplace add OrenAshkenazy/gh-review-loop
/plugin install gh-review-loop@gh-review-loop
```

**Codex:**

```bash
codex plugin marketplace add OrenAshkenazy/gh-review-loop
codex plugin add gh-review-loop@gh-review-loop
```

Then, from a repo with an open PR that a reviewer bot has commented on (the loop asks which bot the first time, and remembers):

> Run the AI reviewer loop

That's the whole interface. [Other install paths](#other-install-paths) · [Prerequisites](#prerequisites)

---

## What's actually implemented

Checkable claims, with the honest scope of each:

| Capability | Scope | Where |
|---|---|---|
| Thread-state filtering | `reviewThreads` GraphQL, `isResolved` / `isOutdated`, plus `ADDRESSED_BY_REPLY` for maintainer *"wontfix"* replies | `fetch_gemini_threads.py` |
| Pattern clustering + sibling sweep | Findings cluster by signature and by the shape of the code they anchor to, so one defect described two ways still counts as one pattern. Two-site token sweeps report lines matching the shared tokens; one-site ranged sweeps report duplicate blocks as `mirror` candidates — text-identical by default in every language, comment-insensitive only for Python via stdlib `tokenize`, never across language families. Refuses generic shapes and never reads outside the changed files | `cluster_findings.py`, `sweep_siblings.py` |
| Verification gate | Auto-detects Python / Node / Rust / Go. Required-check failure flips the run to `verification: failed`. **`Skip` is an offered menu option** — pick it and there is no gate | `detect_profile.py`, `run_profile.py` |
| Cycle cap | Hard count, agent-scoped: only the agent's own pings consume it. Enforced at the write — `request_rereview.py` counts prior pings and refuses past the cap, rather than trusting the agent to stop | `request_rereview.py` |
| Severity ordering | Parsed for **Codex `![P0]`–`![P3]` badges and `![critical]`–`![low]` image alt text only.** A bot using neither convention yields `unknown`, which is kept by default | `thread_severity()` |
| Optional judge | Second-model triage per finding (`false_positive`, `needs_human`, …). Off by default, needs an OpenAI key | `judge.py` |
| Audit trail + local stats | One live-edited PR comment, per-run receipts, per-repo aggregates via `--stats`. Never transmitted | `metrics.py` |
| Zero infrastructure | Stdlib Python + `gh` + `git`. No SDK, no server, no account | all scripts |

**Reproduce the sweep.** [`python3 evals/replay/replay.py`](evals/replay/) runs two captured Sourcery reviews of [PR #67](https://github.com/OrenAshkenazy/gh-review-loop/pull/67) through the clustering and sweep code — no network, no arguments, stdlib only. The fixtures are that reviewer's real payloads, copied verbatim from the GitHub API rather than written for the demo. Run 1 merges two differently-worded findings into one pattern and reports 3 unflagged siblings; run 2 yields one site and correctly no sweep, because the same reviewer returned different findings on identical file contents.

If you're comparing alternatives, the closest is [pbakaus/agent-reviews](https://github.com/pbakaus/agent-reviews) — broader bot coverage and a nicer CLI, no test gate, no hard round cap, no sibling sweep.

---

## Prerequisites

- **Claude Code or Codex** with plugin support.
- **`gh` CLI**, authenticated against the repos you'll run this on.
- **Python 3.10+** and **git**.
- **A reviewer bot that posts GitHub review threads on your PRs.** Codex and Gemini Code Assist ship with full vendor knowledge. CodeRabbit, Copilot, Qodo, Sourcery and others work once you give the loop their author login and re-review mention. There is no assumed default: on a PR with no reviewer configured the loop asks instead of guessing.

> **Note on Gemini Code Assist.** Gemini is a first-class reviewer here and works exactly as it always did, automatic first review included, on the enterprise GitHub app. Google [deprecated the consumer app 2026-06-18 and shut it down 2026-07-17](https://developers.google.com/gemini-code-assist/docs/deprecations/consumer-code-review), so it can no longer be installed on the free tier. Nothing about its support here changed. Select it with `--reviewer gemini-code-assist`.

## Supported reviewers

| Reviewer | Support | Re-review trigger | Severity format |
|---|---|---|---|
| Codex (`chatgpt-codex-connector`) | Built-in, full support. Offered first on an unconfigured PR because it reviews on request, so accepting starts a cycle immediately | `@codex review` | `P0`–`P3`, normalized |
| Gemini Code Assist | Built-in, full support. Needs the **enterprise** GitHub app ([consumer app shut down](https://developers.google.com/gemini-code-assist/docs/deprecations/consumer-code-review)) | `@gemini-code-assist please review…` | `critical` / `high` / `medium` / `low` |
| CodeRabbit, Copilot, Qodo, Sourcery, … | Configurable | Supplied via `--review-trigger-mention` | **Not parsed** — findings carry `unknown` severity |

`--list-reviewers` discovers which bots have commented on your PR; `--reviewer` persists your choice per PR.

**Severity caveat, stated plainly:** only two marker conventions are parsed today — Codex's `![P0]`–`![P3]` and the `![critical]`–`![low]` alt-text form Gemini uses. For any other bot every finding is `unknown`, so `--min-severity high` keeps everything and `--drop-unknown-severity` drops everything. Use severity filtering only with a bot whose format is parsed.

## Why this exists

Bot reviewers expand. You fix the two instances of a pattern it flagged, push, and the next review flags three more in files it hadn't reached yet. Round three finds two more. Each round costs you a context switch, and the bot never tells you it's converging.

Three things in this loop attack that directly:

**It sweeps the class, not the instance.** Findings are clustered by a deterministic pattern signature. When one pattern is flagged at 2+ sites, `sweep_siblings.py` reduces those lines to tokens, intersects them, and reports every other line in the PR's changed files containing all of the shared tokens — the siblings the reviewer hasn't reached yet. A single multi-line finding can also fingerprint its normalized block and report exact copies as `mirror` candidates.

Intersecting across sites is the token sweep's safety property: a candidate has to match what the flagged sites have in *common*, not what any one of them happens to contain. Single-site mirror sweeps require a multi-line range and meaningful identifier-like tokens. The tool reports before anything is edited, refuses a single-line site or a too-generic shape, and never reads a file outside your diff. Swept patterns are tracked across cycles, so if one comes back you see `⚠ RECURRED after sweep` instead of silently re-fixing it.

**Mirror matching optimizes for precision over recall.** By default, in every language, two blocks match only when their text is *identical* — no comment stripping, no whitespace collapsing, no per-language guessing. Comment-insensitive matching is enabled only for languages with a real tokenizer behind it, which today means **Python alone**, via the stdlib `tokenize` module (indentation is preserved, because it is semantic) — and only *within a single file*. Once comments are dropped, identical text can still mean different things in two different files: a `.pyi` stub versus a runtime module, one shebang or source encoding or `from __future__` flag versus another. That list is the semantics of the whole toolchain and does not converge, so rather than enumerate it in the match key, normalized matching stays inside one file, where every such property is equal by construction. Those same files are still matched *across* files by raw matching, which claims only that the bytes repeat — a claim no shebang or encoding can falsify. So two `.py` files sharing a byte-identical block are still reported (as `exact`); two differing only in a comment are not. Python's raw index still carries the tokenizer's string tags, so it never matches a docstring's body against the code that docstring quotes. Blocks are never compared across language families, so identical text in `build.sh` and `Makefile` isn't called a duplicate. The trade is explicit: a duplicate in an unsupported language that differs only by a comment goes unreported. This is an advisory report — one that fires falsely stops being read, while a missed duplicate costs one informational finding.

**It won't push red.** Configure a verification profile once — a one-key menu pick on first run, auto-detected from your `pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod` — and every cycle runs your own tests and linters before the commit goes up. A failed required check flips the run to `verification: failed`. Compare: CodeRabbit's autofix runs a build-verification step and [ships the changes anyway when it fails](https://docs.coderabbit.ai/finishing-touches/autofix).

**It has a brake, not a timeout.** The cycle cap is a hard count of re-review requests, default 3, and it counts *only the agent's own pings* — a human asking the bot to look again doesn't burn a cycle. That's a bound on rounds, not a "stop when the bot goes quiet for ten minutes" heuristic.

Everything else it does — reading `isResolved`/`isOutdated` off the `reviewThreads` GraphQL, honoring a maintainer's *"wontfix"* reply, sorting by severity, keeping one live-edited status comment instead of comment spam — is table stakes done carefully. Correct, but not why you'd pick this.


## Other install paths

### Agent Skills (`npx skills`)

```bash
npx skills add OrenAshkenazy/gh-review-loop -a claude-code codex
```

The `-a` flag selects target agents; omit it for the interactive picker. Requires a recent `skills` CLI. If `codex` doesn't appear in the picker, update the installer or use the Codex plugin flow above.

### Upgrading

**Claude Code:**

```
/plugin update gh-review-loop
```

**Codex** — plugins come from a marketplace *snapshot*, so refresh the snapshot:

```bash
codex plugin marketplace upgrade gh-review-loop
```

Omit the name to refresh every configured marketplace.

### Upgrading from `gh-gemini-review-loop`

Remove the old **marketplace**, not just the plugin — the stale marketplace entry is what pins you to the old name.

**Claude Code:**

```
/plugin marketplace remove gh-gemini-review-loop
/plugin marketplace add OrenAshkenazy/gh-review-loop
/plugin install gh-review-loop@gh-review-loop
```

**Codex:**

```bash
codex plugin remove gh-gemini-review-loop@gh-gemini-review-loop
codex plugin marketplace remove gh-gemini-review-loop
codex plugin marketplace add OrenAshkenazy/gh-review-loop
codex plugin add gh-review-loop@gh-review-loop
```

Settings, verification profiles, and run history are untouched on both — the first run after upgrading moves `~/.config/gh-gemini-review-loop/` to `~/.config/gh-review-loop/` and leaves a symlink at the old path for older installs.

> **Stuck on an old version?** If your runtime insists you are already current while the [releases page](https://github.com/OrenAshkenazy/gh-review-loop/releases) has moved on, the marketplace is pinned to a branch rather than tracking the default one. Both runtimes let you pin — Claude Code stores it as `ref` in `~/.claude/plugins/known_marketplaces.json`; Codex stores it as `ref` under `[marketplaces.<name>]` in `~/.codex/config.toml`, and accepts `owner/repo@ref` or `--ref` when adding. A pinned entry reports that branch's version as "latest" forever, and refreshing it changes nothing. Check with `codex plugin marketplace list` (Codex) or the config file above (Claude Code), then re-add the marketplace with the commands in this section to track the default branch again.

---

## Use it

From a repo with an open GitHub PR:

> Run the AI reviewer loop

The agent will:

1. Wait for the configured reviewer to finish, or ping it first if it only reviews on request.
2. Fetch unresolved, actionable review threads.
3. Skip stale, resolved, duplicate, or already-addressed threads.
4. Cluster findings by pattern and sweep siblings in your changed files.
5. Fix, in severity order.
6. Run your verification profile. Stop here if it fails.
7. Commit and push to the PR branch.
8. Request re-review.
9. Stop when the PR is clean, a human decision is needed, or the cap is used.

More specific phrasings:

| Say this | What happens |
|---|---|
| *"Run the AI reviewer loop"* | Full loop with defaults |
| *"Only fix high-severity reviewer findings"* | Skips lower severities (see the severity caveat above) |
| *"Just audit reviewer comments, don't touch anything"* | Read-only pass |
| *"Show review loop stats for this repo"* | Local aggregates |

See [`SKILL.md`](plugins/gh-review-loop/skills/gh-review-loop/SKILL.md) for the workflow definition, stop conditions, and every phrasing; the deeper material (sweep internals, judge eval, recovery, script reference) sits in [`references/`](plugins/gh-review-loop/skills/gh-review-loop/references/) and is loaded only when a run needs it.

---

## Verification profiles

On the first cycle with actionable findings, the agent scans for build signals (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`) and offers a short preset menu: **All detected · Tests only · Skip · Customize**. Your pick persists under `profiles["owner/repo"]` in `~/.config/gh-review-loop/preferences.json`; later runs are prompt-free.

Every saved check is a required gate — a failure flips the verify step to `--verification failed`. **`Skip` is remembered** and leaves the loop on ad-hoc verification with no gate; saying *"set up a verification profile for this repo"* re-runs detection and overrides it.

```json
{
  "profiles": {
    "owner/repo": {
      "detected_stack": "python",
      "source": "confirmed",
      "working_directory": ".",
      "timeout_seconds": 300,
      "checks": [
        {"name": "tests", "command": "pytest", "required": true},
        {"name": "lint", "command": "ruff check .", "required": true}
      ]
    }
  }
}
```

## Run metrics and local stats

Ask *"Show review loop stats for this repo"* (or run `--stats`). Metrics are local-only (`~/.config/gh-review-loop/runs.jsonl`), contain repo and PR number but no identity, and are never transmitted.

**Per-run receipt.** The full receipt is delivered to the PR, in the same live-edited comment the loop keeps there — status `RUNNING` per cycle, `DONE` or `STOPPED` at the end:

```
[loop] Summary
Findings fetched: 7
Fixed: 4
Human decision required: 1
Re-review requests used: 2/3 (cap)
Verification: passed
Outcome: clean
Time to clean PR: 12m
```

Your terminal gets one line pointing at it, so a long loop doesn't bury the conversation in repeated receipts:

```
[loop] Summary: findings 7 seen this run · fixed 4 · re-review requests 2/3 · verification passed · outcome clean — full receipt: <link>
```

`findings N seen this run` is cumulative across the run; when findings are still open, the split rides on that count — `open 3 (1 new, 2 carried over)`. If the comment can't be written (no network, no permission, `--dry-run`), the full receipt prints to the terminal instead, so a receipt is never lost.

**Aggregated stats** *(illustrative shape — the numbers below are an example, not measured telemetry)*:

```
Review loop stats — owner/repo
Last 10 runs

Average re-review requests used: 1.8
Average elapsed time to terminal outcome: 9m
Average active time per run: 6m
Findings fixed: 32 of 41
Human decisions needed: 6
Addressed by reply: 9
False positives avoided: 14   (across 6 of 10 judged runs)
Most repeated finding area: tests
```

**Elapsed** is wall-clock including bot waits; **active** is loop processing time only.

## Optional: per-finding judge

A second model can label each finding (`valid_actionable`, `false_positive`, `needs_human`, `explanation_only`, `duplicate`, `already_addressed`) before you spend a cycle on it. Off by default; needs an OpenAI-compatible key. Calls go out over stdlib `urllib` — no SDK dependency.

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/judge_doctor.py" --probe
```

See [`references/judge-eval.md`](plugins/gh-review-loop/skills/gh-review-loop/references/judge-eval.md) for the verdict schema, eval modes, and key setup.

## Configuring the skill

Persistent settings live in `~/.config/gh-review-loop/preferences.json`.

### Set the loop cap

Default is 3 re-review requests per PR:

```bash
python3 - <<'PY'
import json
from pathlib import Path

# Resolve like the scripts do: new dir, else the not-yet-migrated legacy dir.
base = Path.home() / ".config" / "gh-review-loop"
legacy = Path.home() / ".config" / "gh-gemini-review-loop"
if not base.exists() and legacy.exists():
    base = legacy
path = base / "preferences.json"
path.parent.mkdir(parents=True, exist_ok=True)
prefs = json.loads(path.read_text()) if path.exists() else {}
prefs["schema_version"] = 2
prefs["max_rereview_requests"] = 4
path.write_text(json.dumps(prefs, indent=2, sort_keys=True) + "\n")
PY
```

Resulting file:

```json
{
  "max_rereview_requests": 4,
  "schema_version": 2
}
```

Judge settings live in the same file; the script preserves `max_rereview_requests` when saving them.

## How it works

The skill drives a set of small stdlib Python scripts under [`plugins/gh-review-loop/skills/gh-review-loop/scripts/`](plugins/gh-review-loop/skills/gh-review-loop/scripts/). Each is independently runnable and `--json`-clean on stdout with progress on stderr, so you can drive the loop by hand or from CI.

Every write the scripts make to your PR supports `--dry-run`, including the re-review request. The `git push` itself is an ordinary git command run by the agent, not a script write.

## Feedback wanted

- Which reviewer bot are you on, and does the sibling sweep find real siblings or noise in your codebase?
- Is a cap of 3 the right default, or does your team converge in 2?
- Where does the verification gate get in your way?

Open an [issue](https://github.com/OrenAshkenazy/gh-review-loop/issues) or a [discussion](https://github.com/OrenAshkenazy/gh-review-loop/discussions).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
