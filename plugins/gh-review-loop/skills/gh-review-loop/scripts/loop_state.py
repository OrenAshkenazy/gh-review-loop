"""Shared local state, transactions, and receipt-freshness helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from functools import wraps
from pathlib import Path
from typing import Any

import loop_lock

# The plugin was renamed gh-gemini-review-loop -> gh-review-loop. The state
# dir follows the new name; the legacy dir keeps working two ways: unmigrated
# installs resolve to it via the fallback below, and migrated installs leave a
# legacy -> new symlink behind so *older plugin versions* (whose scripts and
# hook guards hardcode the legacy path) still read the same state.
STATE_DIR_NAME = "gh-review-loop"
LEGACY_STATE_DIR_NAME = "gh-gemini-review-loop"


def _config_root() -> Path:
    home = os.environ.get("HOME") if sys.platform != "win32" else None
    return (Path(home) if home else Path.home()) / ".config"


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


def load_sticky_state(*, strict: bool = False) -> dict[str, Any]:
    path = sticky_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        if strict:
            raise RuntimeError(f"cannot update unreadable review-loop state: {path}") from exc
        return {}
    # state.json must be a JSON object. A valid-but-non-dict payload (a list or
    # scalar from corruption or hand-editing) would crash callers that do
    # .values()/.items() on the result. Guard centrally here so every caller —
    # any_active_run, find_active_run, and the rest — is safe.
    if not isinstance(data, dict) and strict:
        raise RuntimeError(f"cannot update non-object review-loop state: {path}")
    return data if isinstance(data, dict) else {}


def state_key_for(state: dict[str, Any], repo: str, pr: int) -> str:
    """Find this PR's existing state key regardless of repository-name casing."""
    target = f"{repo}#{pr}".casefold()
    for key in state:
        if isinstance(key, str) and key.casefold() == target:
            return key
    return f"{repo}#{pr}"


def state_transaction(function):
    """Serialize the complete local read/modify/write operation, never network I/O."""
    @wraps(function)
    def locked(*args, **kwargs):
        with loop_lock.file_lock(sticky_state_path().with_suffix(".json.lock")):
            load_sticky_state(strict=True)
            return function(*args, **kwargs)
    return locked


@state_transaction
def save_sticky_state(state: dict[str, Any]) -> None:
    path = sticky_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(state, indent=2, sort_keys=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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


# Returned inside the record list when the log cannot be trusted as complete.
INCOMPLETE_LOG_RECORD: dict[str, Any] = {"incomplete": True}


def incomplete_marker_path(repo: str, pr: int) -> Path:
    return pr_scoped_path(repo, pr, "provenance", ".incomplete")


def mark_provenance_incomplete(repo: str, pr: int) -> bool:
    """Durably note that a posted request could not be logged; True once on disk.

    Lives beside the provenance log, not in state.json: that file is
    rewritten whole by unlocked writers, and a fetch holding an older
    snapshot would erase a flag written there. Nothing rewrites this file.
    """
    path = incomplete_marker_path(repo, pr)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text("", encoding="utf-8")
    except (OSError, ValueError):
        return False
    return True


def unconfirmed_request_path(repo: str, pr: int) -> Path:
    """The record of a request whose outcome is not confirmed yet (a
    reservation). Beside the PR's lock file, apart from state.json."""
    return pr_scoped_path(repo, pr, "locks", ".unconfirmed.json")


def cap_count_uncertain(repo: str, pr: int) -> bool:
    """Is the local request history not a complete count right now?

    True for an explicitly incomplete history, and while a reservation is
    open: that request may already be on GitHub but is not in the log until
    it is settled.
    """
    state = load_sticky_state()
    entry = state.get(state_key_for(state, repo, pr))
    legacy_pending = isinstance(entry, dict) and bool(entry.get("review_request_in_flight"))
    return legacy_pending or provenance_incomplete(repo, pr) or unconfirmed_request_path(repo, pr).exists()


def provenance_incomplete(repo: str, pr: int) -> bool:
    """Was a posted request explicitly left unrecorded (failed append, torn log,
    or the legacy state.json flag)?"""
    if incomplete_marker_path(repo, pr).exists():
        return True
    logged = read_request_log(repo, pr) or []
    if any(isinstance(item, dict) and item.get("incomplete") for item in logged):
        return True
    state = load_sticky_state()
    target = f"{repo}#{pr}".casefold()
    for key, entry in state.items():
        if isinstance(key, str) and key.casefold() == target:
            return bool(isinstance(entry, dict) and entry.get("review_requests_incomplete"))
    return False


def read_request_log(repo: str, pr: int) -> list[dict[str, Any]] | None:
    """The PR's appended request records; None when there is no log yet.

    An unreadable log, or any malformed line (a crash can tear the last
    append), yields ``[INCOMPLETE_LOG_RECORD]``: dropping a torn record would
    make the rest look complete and undercount the cap.
    """
    if incomplete_marker_path(repo, pr).exists():
        return [dict(INCOMPLETE_LOG_RECORD)]
    try:
        text = request_log_path(repo, pr).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return [dict(INCOMPLETE_LOG_RECORD)]
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            return [dict(INCOMPLETE_LOG_RECORD)]
        if not isinstance(item, dict):
            return [dict(INCOMPLETE_LOG_RECORD)]
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

    Repo-agnostic and network-free; retained for state consumers.
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
    recorded/never-started loop reads as inactive. This compatibility helper
    only reads local state. Explicit lifecycle commands target an exact PR.
    If several PRs in the
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
