#!/usr/bin/env python3
"""PreToolUse gate: enforce --cycle-summary before git push during the loop.

The skill requires --cycle-summary at the end of every non-terminal cycle.
This gate makes that requirement mechanical: if a Gemini review loop is
active for the current repo and the run has a stale summary
(update_seq > last_summary_seq), git push is blocked until the agent
runs --cycle-summary.

Why block at git push rather than at re-review comment posting?
git push is the natural commit point that separates cycles. Blocking here
gives the agent a clear, one-step fix: run --cycle-summary, then push.

The error message is intentionally just the fix instruction (#86): the
receipt content itself is delivered once, via the sticky PR comment that
--cycle-summary writes — embedding a state snapshot here was a third copy
of the same data.

Fails OPEN: any error (not a repo, bad JSON, import failure) allows the push.
A correctness aid must never wedge normal git operations.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from loop_state import (  # noqa: E402 — slim, stdlib-only (see #83)
    any_active_run,
    find_active_run,
    reap_stale_sentinel,
    resolve_current_repo,
    summary_is_stale,
)
from hook_runtime import (  # noqa: E402
    hook_cwd,
    load_hook_payload,
    python_script_command,
    tool_command,
    tool_name,
)

_PATH_ARG = r"""("[^"]*"|'[^']*'|[^\s;&|()]+)"""
_GIT_PUSH_RE = re.compile(r"\bgit\s+(?:-C\s+" + _PATH_ARG + r"\s+)?push\b")
_SEPARATORS = ";&|()\n"


def _simple_commands(command: str, end: int) -> list[list[str]]:
    """The simple commands in ``command[:end]``, each as its shell words.

    Quotes and backslashes are honored, so a quoted argument (a commit
    message saying "fix cd handling") is one word and never starts a command.
    """
    commands: list[list[str]] = []
    words: list[str] = []
    word: list[str] = []
    in_word = False
    quote = ""
    index = 0
    while index < end:
        char = command[index]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"' and index + 1 < end:
                index += 1
                word.append(command[index])
            else:
                word.append(char)
        elif char in "'\"":
            quote, in_word = char, True
        elif char == "\\" and index + 1 < end:
            index += 1
            word.append(command[index])
            in_word = True
        elif char in _SEPARATORS or char.isspace():
            if in_word:
                words.append("".join(word))
                word, in_word = [], False
            if char in _SEPARATORS and words:
                commands.append(words)
                words = []
        else:
            word.append(char)
            in_word = True
        index += 1
    if in_word:
        words.append("".join(word))
    if words:
        commands.append(words)
    return commands


def _cd_target(words: list[str]) -> str | None:
    """The directory a ``cd`` command moves to (options skipped), or None."""
    if not words or words[0] != "cd":
        return None
    args = words[1:]
    while args and args[0].startswith("-") and args[0] != "-":
        args.pop(0)
    return args[0] if args else None


def _move(here: str | None, target: str) -> str:
    if target[:1] in "\"'":
        target = target[1:-1]
    target = os.path.expanduser(target)
    if here and not os.path.isabs(target):
        target = os.path.join(here, target)
    # Logical, as bash's own `cd` is: `plugins/..` is where it started.
    return os.path.normpath(target)


def push_target_dir(command: str, cwd: str | None) -> str | None:
    """The directory the command's ``git push`` runs in, when the command says.

    Every ``cd <dir>`` command before the push (never a quoted argument that
    merely says "cd") moves from where the previous one left (``cd -``
    returns), starting at ``cwd``; ``git -C <dir> push`` then moves
    once more. None means the hook's own directory. Resolving only the hook's
    directory let a session in another repo push the looped repo with ``cd
    <clone> && git push`` past this gate, and resolving only the last ``cd``
    sent ``cd plugins && cd .. && git push`` above the repo.
    """
    push = _GIT_PUSH_RE.search(command)
    if push is None:
        return None
    here, previous, moved = cwd, cwd, False
    for words in _simple_commands(command, push.start()):
        target = _cd_target(words)
        if target is None:
            continue
        if target == "-":
            here, previous = previous, here
        else:
            here, previous = _move(here, target), here
        moved = True
    if push.group(1) is not None:
        here, moved = _move(here, push.group(1)), True
    return here if moved else None

BLOCK_TEMPLATE = (
    "[gh-review-loop] A review loop is active for {repo} and the "
    "per-cycle summary has not been emitted yet (update_seq={update_seq} > "
    "last_summary_seq={last_summary_seq}).\n\n"
    "Run --cycle-summary (it delivers the receipt to the PR comment; relay "
    "the printed [loop] pointer line), then push:\n\n"
    "  {fetch_cmd} \\\n"
    "      --pr {pr_url} \\\n"
    "      --cycle-summary \\\n"
    "      --fixed-count <n> \\\n"
    "      --verification <passed|failed|skipped>"
)


def stale_summary_for_push(repo: str) -> dict[str, Any] | None:
    """Return block info dict if a git push should be blocked, else None.

    A push is blocked when the active loop for ``repo`` has advanced
    (``update_seq > 0``) without a summary being emitted since
    (``last_summary_seq`` lags). Returns a dict with the run fields the
    one-line fix instruction needs.
    """
    result = find_active_run(repo)
    if result is None:
        return None
    pr_number, run = result
    if not summary_is_stale(run):
        return None
    return {
        "pr_number": pr_number,
        "update_seq": run.get("update_seq", "?"),
        "last_summary_seq": run.get("last_summary_seq", 0),
    }


def main() -> int:
    # The shell guard only checks the sentinel exists (#103); a stale one
    # means a crashed/abandoned loop. Reap BEFORE the command filter — most
    # Bash calls are not `git push`, and returning early on them would leave
    # the stale marker spawning this gate on every call until a push.
    if reap_stale_sentinel():
        return 0

    try:
        raw_payload = sys.stdin.read() if sys.stdin is not None else ""
        payload = load_hook_payload(raw_payload)
    except (OSError, ValueError):
        return 0

    # Only intercept Bash tool calls that contain git push.
    if tool_name(payload) != "Bash":
        return 0
    command = tool_command(payload)
    if not _GIT_PUSH_RE.search(command):
        return 0

    try:
        if not any_active_run():
            return 0
        repo = resolve_current_repo(push_target_dir(command, hook_cwd(payload)))
        info = stale_summary_for_push(repo)
    except (RuntimeError, OSError, ValueError, TypeError, AttributeError):
        return 0  # not a gh repo, no remote, or corrupt state -> allow

    if info is None:
        return 0

    pr_url = f"https://github.com/{repo}/pull/{info['pr_number']}"
    print(
        BLOCK_TEMPLATE.format(
            repo=repo,
            update_seq=info["update_seq"],
            last_summary_seq=info["last_summary_seq"],
            pr_url=pr_url,
            fetch_cmd=python_script_command("fetch_gemini_threads.py"),
        ),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
