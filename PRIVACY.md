# Privacy and Data Handling

**Last updated: 2026-10-17**

## Summary

`gh-review-loop` is a Claude Code plugin that runs locally on your machine.

The plugin author does not operate a backend service, does not receive your PR data, and does not collect telemetry, analytics, crash reports, prompts, repository names, usernames, or usage data.

The plugin interacts with GitHub through your existing `gh` CLI authentication. Optional OpenAI judge eval is off by default and only runs when explicitly enabled by the user.

## Data the plugin may read

When running the Gemini review loop, the plugin may read:

1. GitHub pull request metadata
2. GitHub review threads
3. Gemini Code Assist review comments
4. Review thread state such as resolved or outdated
5. File paths and line numbers
6. Diff hunks attached to review comments
7. Local repository files needed by Claude Code to fix findings
8. Local Git metadata
9. Test and command output produced during verification

## GitHub API usage

The plugin calls GitHub GraphQL and REST APIs through the local `gh` CLI and your existing GitHub authentication.

The plugin may use GitHub APIs to:

1. Fetch pull request metadata
2. Fetch review threads and comments
3. Resolve outdated review threads only when explicitly requested
4. Resolve explicitly deferred owner/collaborator threads only when explicitly requested
5. Post pull request comments
6. Update sticky receipt comments
7. Request Gemini Code Assist re review

The plugin may also push commits when the user authorizes a fix loop. The verified publish helper gates the changed files against required checks on Claude Code and Codex.

The plugin does not merge pull requests, approve reviews, submit GitHub reviews, change repository settings, or grant repository access.

## Optional OpenAI judge eval

Judge eval is off by default.

When explicitly enabled by the user, the plugin may send Gemini Code Assist findings and related PR context to the OpenAI API for classification.

This context may include:

1. Review comment text
2. File paths
3. Line numbers
4. Gemini severity labels
5. Diff hunks
6. Limited surrounding code context needed to classify the finding

The judge classifies findings as valid, false positive, duplicate, already addressed, explanation only, or requiring human decision.

Judge eval is read only. The judge never posts comments, resolves threads, pushes code, or mutates GitHub state.

Normal loop runs do not send PR context to OpenAI. Judge eval is never enabled silently.

Data sent to OpenAI is subject to OpenAI's privacy policy and the terms of the OpenAI account used by the user.

## OpenAI API key handling

Judge eval uses the user's own OpenAI API key.

The plugin reads `OPENAI_API_KEY` from the process environment when judge eval is enabled.

The plugin does not store the OpenAI API key.

The plugin does not write the OpenAI API key to preferences, logs, PR comments, sticky receipts, repository files, or GitHub.

The plugin author has no access to the user's OpenAI API key and no access to data sent from the user's machine to OpenAI.

## Local files written

The plugin may write local state under:

```text
~/.config/gh-review-loop/
```

Installs that predate the plugin's rename used `~/.config/gh-gemini-review-loop/`. On its first run after upgrading, the plugin renames that directory to the new name and leaves a symlink at the old path, so older plugin versions keep reading the same files. No data is copied, duplicated, or transmitted by the migration.

Known files:

```text
~/.config/gh-review-loop/preferences.json
~/.config/gh-review-loop/state.json
~/.config/gh-review-loop/runs.jsonl
~/.config/gh-review-loop/provenance/<owner>_<repo>_<pr>.jsonl
~/.config/gh-review-loop/provenance/<owner>_<repo>_<pr>.incomplete
~/.config/gh-review-loop/locks/<owner>_<repo>_<pr>.lock
~/.config/gh-review-loop/locks/<owner>_<repo>_<pr>.unconfirmed.json
```

`preferences.json` stores user preferences such as judge mode, judge model, and whether the one time judge eval tip was shown.

`state.json` stores local sticky receipt state, such as pull request identifiers and GitHub comment IDs used to update an existing receipt comment instead of posting duplicate comments. It also holds per-run loop tracking (the run start timestamp and the set of finding identifiers seen during a run).

`runs.jsonl` stores one append-only record per completed loop run: workflow counts (findings fetched, fixed, needs-human, addressed-by-reply, cycles used, verification result, outcome, duration), the repository name, and the pull request number. It contains no identity — no git author, GitHub login, or email — and is never transmitted. It exists solely to power the local run summary and the `--stats` command.

`provenance/*.jsonl` stores one append-only line per review request the loop posted for a review-request reviewer (GitHub Copilot): the reviewer bot's login, the request timestamp, and whether it counted toward the re-review cap. It lets the cap tell the loop's own requests from ones you click in GitHub. Deleting it makes the cap fall back to counting every request made under your login. The empty `.incomplete` file beside it appears only if a request could not be written to that log, and has the same effect.

`locks/` holds an empty per-PR lock file that keeps two loop runs from requesting a review at the same time, and, briefly, a record of a request whose outcome GitHub had not confirmed yet (reviewer login, timestamp, and the kind of uncertainty). That record is deleted once the request is confirmed or ruled out.

The directory location is overridable with the `GGRL_STATE_DIR` environment variable.

These files should not contain API keys, full source code, full PR diffs, or OpenAI responses.

## Telemetry and analytics

This plugin does not include telemetry.

It does not send analytics, usage events, crash reports, repository names, usernames, prompts, PR content, or command output to the plugin author.

Detailed run records and the `--stats` aggregate are stored locally. Loop receipt summaries are posted to GitHub and include counts, repository-relative finding paths, verification check names, and results. Verification command lines and runner output remain local because they can contain machine paths or private arguments.

## User controls

To disable judge eval, set judge mode to off or remove the preferences file:

```bash
rm ~/.config/gh-review-loop/preferences.json
```

To remove local sticky receipt and run-tracking state:

```bash
rm ~/.config/gh-review-loop/state.json
```

To remove locally stored run metrics:

```bash
rm ~/.config/gh-review-loop/runs.jsonl
```

To remove all local plugin state:

```bash
rm -rf ~/.config/gh-review-loop
```

To disable or uninstall the plugin, use Claude Code's plugin manager:

```text
/plugin
```

Then use the Installed tab to disable or uninstall `gh-review-loop`.

## Contact

Privacy questions:

```text
oren.as@gmail.com
```
