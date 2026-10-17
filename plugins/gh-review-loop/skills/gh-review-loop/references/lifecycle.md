## Explicit lifecycle commands

No tool or Stop hooks are installed. All commands use Python directly on Windows and POSIX hosts.

| Command | Responsibility |
|---|---|
| `start_loop.py` | Requires a fetched run for the exact PR, a selected reviewer, and a saved profile decision before fixes. Explicit Skip allows editing, but never publication. |
| `publish_changes.py` | Runs full verification once, generates the cycle receipt, then rechecks summary freshness, file contents, profile, checkout, head, remote, and cap before publication. Returns receipt output for the agent to relay. |
| `finish_loop.py` | Emits the terminal receipt and records the exact PR's active run without rerunning verification. An already-finished run is a no-op; an existing record with uncleared tracking blocks another append. |

The commands do not intercept arbitrary edits, direct Git pushes, or conversation shutdown. The skill explicitly calls start before editing and finish on each terminal path. The publisher and re-review helper retain their own safety gates. If finish reports incomplete recording, inspect its error and local run state before retrying. No background end-of-turn network call runs.

## Lifecycle invocation

Use installed absolute script paths; Windows uses `python` and POSIX hosts can use `python3` with the same arguments:

```text
python "<plugin-root>/skills/gh-review-loop/scripts/start_loop.py" --repo owner/repo --pr 42 --repo-root "<checkout-root>"
python "<plugin-root>/skills/gh-review-loop/scripts/publish_changes.py" --repo owner/repo --pr 42 --repo-root "<checkout-root>" --paths src/fix.py tests/test_fix.py --message "Fix reviewer findings" --fixed-count 2
python "<plugin-root>/skills/gh-review-loop/scripts/finish_loop.py" --repo owner/repo --pr 42 --repo-root "<checkout-root>" --verification-details '<publisher verification JSON>' --outcome clean --gemini-confirmed
```

Finish follows the final reviewer wait. On no-publication paths, report the actual failed/skipped verification result. Pass the publisher's `verification` object directly; setup and reporting never run checks. Publisher `--dry-run` verifies without a receipt or publication; finish `--dry-run` only checks whether an active run exists.

Publisher and finish accept `--fixed-count`, repeatable `--fixed-finding`, `--fixed-path`, `--swept-pattern`, and `--semantic-risk`, plus severity filters and `--changed-base`/`--changed-head`. Pass the actual fix/sweep metadata and cap override. Finish also accepts terminal outcome/reason and reviewer-confirmation flags.
