"""Slim loop-state helpers shared by the hook gates and the main script.

The PreToolUse/Stop hooks must decide "is a review loop active?" on every
matching tool call, so this module keeps that decision cheap: stdlib-only
imports, no GraphQL machinery, no argparse tree. Importing it costs
milliseconds where importing ``fetch_gemini_threads`` costs ~100ms.

It also owns the loop-active *sentinel file*: an empty marker whose existence
lets the hooks.json shell guard skip spawning Python entirely on the idle
path. Lifecycle:

- ``touch_sentinel()`` on every real fetch (loop start / each cycle). It
  returns False when the marker could not be written — which disables the
  gates for the run, so callers warn instead of ignoring it.
- ``clear_sentinel()`` when the terminal ``--record-run`` leaves no active
  run behind.
- A sentinel older than ``SENTINEL_TTL_SECONDS`` (24h) is stale — a crashed
  or abandoned loop must not keep the hooks live forever. The shell guard
  only checks existence (`[ -f ]`, the cheapest possible test — #103); the
  Python gates it spawns call ``reap_stale_sentinel()`` to delete a stale
  marker and stand down, so staleness costs nothing on the idle path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SENTINEL_TTL_SECONDS = 24 * 60 * 60

SENTINEL_NAME = "loop-active"

# The plugin was renamed gh-gemini-review-loop -> gh-review-loop. The state
# dir follows the new name; the legacy dir keeps working two ways: unmigrated
# installs resolve to it via the fallback below, and migrated installs leave a
# legacy -> new symlink behind so *older plugin versions* (whose scripts and
# hook guards hardcode the legacy path) still read the same state.
STATE_DIR_NAME = "gh-review-loop"
LEGACY_STATE_DIR_NAME = "gh-gemini-review-loop"


def _config_root() -> Path:
    return Path(os.path.expanduser("~/.config"))


def state_dir() -> Path:
    """Return the per-user state directory (XDG-ish, GGRL_STATE_DIR override).

    Resolution: explicit ``GGRL_STATE_DIR`` always wins; otherwise the new
    ``~/.config/gh-review-loop`` when it exists (or when there is nothing to
    fall back to); otherwise the legacy ``~/.config/gh-gemini-review-loop``
    left by a pre-rename install that has not been migrated yet.
    """
    env = os.environ.get("GGRL_STATE_DIR")
    if env:
        return Path(env)
    new = _config_root() / STATE_DIR_NAME
    legacy = _config_root() / LEGACY_STATE_DIR_NAME
    # is_dir(), not exists(): a stray *file* at the new path must not shadow
    # a legacy directory full of user data, and mkdir(exist_ok=True) would
    # crash on it anyway (exist_ok does not cover non-directories).
    if new.is_dir() or not legacy.is_dir():
        return new
    return legacy


def migrate_legacy_state_dir() -> bool:
    """One-time move of the legacy state dir to the new name. True if migrated.

    Renames ``~/.config/gh-gemini-review-loop`` to ``~/.config/gh-review-loop``
    and leaves a symlink at the legacy path, so plugin versions installed
    before the rename (their scripts and hook guards hardcode the legacy path)
    keep reading and writing the same state. Idempotent; a no-op under
    ``GGRL_STATE_DIR``, when there is nothing to migrate, or when the new dir
    already exists (never merges two dirs). Fails open: any OSError leaves the
    legacy dir in place, and ``state_dir()`` keeps resolving to it — including
    when the rename succeeds but the symlink cannot be created, which is rolled
    back rather than left as a move older installs cannot follow.
    """
    if os.environ.get("GGRL_STATE_DIR"):
        return False
    new = _config_root() / STATE_DIR_NAME
    legacy = _config_root() / LEGACY_STATE_DIR_NAME
    if new.exists() or legacy.is_symlink() or not legacy.is_dir():
        return False
    try:
        legacy.rename(new)
    except OSError:
        return False
    try:
        legacy.symlink_to(new)
    except OSError:
        # A move without the compatibility symlink would strand the state where
        # pre-rename installs cannot find it, so undo it: the legacy dir keeps
        # resolving via the fallback, and the next run retries.
        try:
            new.rename(legacy)
            return False
        except OSError:
            # Rolled forward and cannot roll back. New-version scripts are fine
            # (state_dir() finds the new dir); only stale pre-rename installs
            # lose sight of the state. Say so rather than fail.
            print(
                f"warning: state moved to {new} but the compatibility symlink "
                f"at {legacy} could not be created; plugin versions older than "
                "the rename will no longer see this state.",
                file=sys.stderr,
            )
    return True


def sticky_state_path() -> Path:
    """Return the path to the sticky-receipt/run state file.

    Overridable via ``GGRL_STATE_DIR`` (useful for tests). Defaults to
    ``~/.config/gh-review-loop/state.json`` per XDG conventions.
    """
    return state_dir() / "state.json"


def load_sticky_state() -> dict[str, Any]:
    path = sticky_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    # state.json must be a JSON object. A valid-but-non-dict payload (a list or
    # scalar from corruption or hand-editing) would crash callers that do
    # .values()/.items() on the result. Guard centrally here so every caller —
    # any_active_run, find_active_run, and the rest — is safe.
    return data if isinstance(data, dict) else {}


def save_sticky_state(state: dict[str, Any]) -> None:
    path = sticky_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def pr_scoped_path(repo: str, pr: int, subdir: str, suffix: str) -> Path:
    """A per-PR file under the state dir. Owner and repo names are
    case-insensitive on GitHub, so the name is case-folded."""
    safe = re.sub(r"[^a-z0-9._-]", "_", f"{repo}#{pr}".casefold())
    return state_dir() / subdir / f"{safe}{suffix}"


def request_log_path(repo: str, pr: int) -> Path:
    return pr_scoped_path(repo, pr, "provenance", ".jsonl")


def append_request_log(repo: str, pr: int, record: dict[str, Any]) -> bool:
    """Append one review-request record; True once it is on disk.

    The cap's provenance lives here, not in state.json: state.json is
    rewritten whole by many writers that do not lock each other, and a writer
    holding an older snapshot would erase a record appended meanwhile. An
    appended line is never rewritten by anyone.
    """
    path = request_log_path(repo, pr)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, ValueError):
        return False
    return True


def read_request_log(repo: str, pr: int) -> list[dict[str, Any]] | None:
    """The PR's appended request records; None when there is no log yet."""
    try:
        text = request_log_path(repo, pr).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    records = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue  # a torn last line from a crash mid-append
        if isinstance(item, dict):
            records.append(item)
    return records


def safe_int(value: Any) -> int:
    """Coerce a state value to int, returning 0 for missing/corrupt values.

    state.json is local and can be hand-edited or corrupted; the seq fields
    must never raise from a non-numeric value, so a bad value reads as 0.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def any_active_run() -> bool:
    """True if any tracked PR (any repo) has an active loop run.

    Repo-agnostic and network-free, so the Stop hook can skip git/repo
    resolution entirely on the common case of no loop in flight.
    """
    state = load_sticky_state()
    if not isinstance(state, dict):
        return False
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        run = entry.get("run")
        # "Active" means a fetch bumped update_seq under the current code. A
        # started_at without update_seq is legacy/pre-feature cruft (or a
        # cleared run) and must not count, or stale entries from any repo would
        # look active forever.
        if isinstance(run, dict) and safe_int(run.get("update_seq")) > 0:
            return True
    return False


def find_active_run(repo_full: str) -> tuple[int, dict[str, Any]] | None:
    """Return ``(pr_number, run)`` for the active Gemini loop in ``repo_full``.

    A loop is "active" once a cycle has accumulated into its ``run`` block
    (signalled by ``started_at``); ``--record-run`` clears that block, so a
    recorded/never-started loop reads as inactive. This is the cheap,
    network-free gate the loop hooks use to decide whether to spend a GitHub
    fetch on a summary, so it only reads local state. If several PRs in the
    repo look active, the most recently started one wins.
    """
    candidates: list[tuple[str, int, dict[str, Any]]] = []
    state = load_sticky_state()
    if not isinstance(state, dict):
        return None
    for key, entry in state.items():
        prefix, sep, suffix = key.rpartition("#")
        if not sep or prefix != repo_full:
            continue
        try:
            number = int(suffix)
        except ValueError:
            continue
        run = entry.get("run") if isinstance(entry, dict) else None
        # Active = bumped update_seq under current code (see any_active_run);
        # a started_at-only run is legacy cruft and is skipped. Order by
        # started_at so the most recently begun loop wins when several qualify.
        if isinstance(run, dict) and safe_int(run.get("update_seq")) > 0:
            # str() the sort key so a corrupted non-str started_at can't
            # TypeError when sorting candidates of mixed types.
            candidates.append((str(run.get("started_at", "")), number, run))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, number, run = candidates[-1]
    return number, run


def summary_is_stale(run: dict[str, Any]) -> bool:
    """True when the run has advanced since the last emitted summary.

    ``update_seq`` is bumped on every fetch; ``last_summary_seq`` is stamped
    when a summary is emitted. A run that fetched but was never summarized
    (``last_summary_seq`` absent → 0) is stale. A run with ``started_at`` but no
    fetch yet (``update_seq`` 0) has nothing to show and is not stale.
    """
    return safe_int(run.get("update_seq")) > safe_int(run.get("last_summary_seq"))


def resolve_current_repo() -> str:
    """Return 'owner/repo' for the current dir without needing an open PR."""
    proc = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"gh repo view failed: {message}")
    try:
        view = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("Could not resolve the current repo with gh repo view.") from exc
    if not isinstance(view, dict) or "nameWithOwner" not in view:
        raise RuntimeError("Could not resolve the current repo with gh repo view.")
    return view["nameWithOwner"]


# ---------------------------------------------------------------------------
# Loop-active sentinel
# ---------------------------------------------------------------------------


def sentinel_path() -> Path:
    return state_dir() / SENTINEL_NAME


def touch_sentinel() -> bool:
    """Create or freshen the loop-active sentinel. True when the marker exists.

    Refreshed on every real fetch so a long-running loop never crosses the
    TTL mid-run.

    Failure is fail-open *for the loop* but fail-**off** for the gates: the
    hooks.json guard reads an absent sentinel as "no loop is running" and
    exits 0 before spawning Python. So a sentinel that cannot be written
    leaves the loop itself fully functional while silently disabling the
    profile gate, the push gate, and the Stop-hook summary backstop for the
    rest of the run — the state.json run block alone will not revive them.
    That trade is deliberate (a marker we cannot write must never wedge the
    loop), but it is not free, so this returns False instead of swallowing
    the error: callers surface it rather than assume the gates are live.
    """
    try:
        path = sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, None)
    except OSError:
        return False
    return True


def clear_sentinel() -> None:
    try:
        sentinel_path().unlink()
    except OSError:
        pass


def sentinel_is_stale(now: float | None = None) -> bool:
    """True when the sentinel exists but is older than the 24h TTL.

    A missing sentinel is not "stale" — it is simply absent.
    """
    try:
        mtime = sentinel_path().stat().st_mtime
    except OSError:
        return False
    reference = time.time() if now is None else now
    return (reference - mtime) > SENTINEL_TTL_SECONDS


def _reap_candidates() -> list[Path]:
    """Sentinel paths the reap must inspect — the same set the shell guard can
    arm from. With no ``GGRL_STATE_DIR`` the guard falls back from the new
    config dir to the legacy one, so in a split-directory state (both dirs
    exist, marker only under legacy) ``sentinel_path()`` alone would inspect
    a different, absent file and the stale marker would survive forever
    (PR #110 review)."""
    if os.environ.get("GGRL_STATE_DIR"):
        return [sentinel_path()]
    return [
        _config_root() / STATE_DIR_NAME / SENTINEL_NAME,
        _config_root() / LEGACY_STATE_DIR_NAME / SENTINEL_NAME,
    ]


def reap_stale_sentinel(now: float | None = None) -> bool:
    """Reap stale sentinel(s); True when the gate should stand down.

    The hooks.json shell guard spawns a gate on bare existence; each gate
    calls this first so a crashed or abandoned loop disarms itself on the
    next hook fire instead of blocking edits/pushes for a dead run (#103).

    Mirrors the guard's selection exactly (PR #110 review): the first
    candidate that exists is the marker that armed this gate, and only its
    staleness decides the verdict — a fresh sentinel in the *other* config
    dir must not keep the gates armed for the guard-selected dead run, and
    a stale twin being cleaned up must not disarm an active loop. A stale
    selected sentinel stands the gate down even when deletion fails
    (read-only state dir): fail open, like the shell guard's old ``rm -f``.
    Every stale candidate is unlinked best-effort along the way.
    """
    reference = time.time() if now is None else now
    selected_stale: bool | None = None
    for path in _reap_candidates():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        stale = (reference - mtime) > SENTINEL_TTL_SECONDS
        if selected_stale is None:
            selected_stale = stale
        if stale:
            try:
                path.unlink()
            except OSError:
                pass
    return bool(selected_stale)
