"""Cooperative process/thread locks for one local review-loop state directory."""
from __future__ import annotations

import errno
import hashlib
import os
import threading
import time
from time import monotonic as _monotonic
from contextlib import contextmanager
from pathlib import Path

_registry_lock = threading.Lock()
_thread_locks: dict[tuple[int, str], threading.RLock] = {}
_held = threading.local()


class LockBusy(TimeoutError):
    pass


def _lock_fd(fd: int, unlock: bool = False) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def file_lock(path: Path, timeout: float = 30):
    """Lock a stable file; nested calls in the same thread share ownership.

    Never unlink lock files: another process may already have the inode open.
    File descriptors are non-inheritable; closing them/process death releases
    the OS lock. Contention times out before any guarded operation executes.
    """
    path = path.resolve()
    key = (os.getpid(), os.path.normcase(str(path)))
    deadline = _monotonic() + timeout
    with _registry_lock:
        thread_lock = _thread_locks.setdefault(key, threading.RLock())
    if not thread_lock.acquire(timeout=max(0, deadline - _monotonic())):
        raise LockBusy(f"review-loop lock busy: {path}")
    held = getattr(_held, "keys", set())
    _held.keys = held
    fd = None
    acquired = False
    try:
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        while True:
            try:
                _lock_fd(fd)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if _monotonic() >= deadline:
                    raise LockBusy(f"review-loop lock busy: {path}") from exc
                time.sleep(min(0.05, max(0, deadline - _monotonic())))
        held.add(key)
        yield
    finally:
        try:
            if fd is not None:
                try:
                    if acquired:
                        held.remove(key)
                        _lock_fd(fd, unlock=True)
                finally:
                    os.close(fd)
        finally:
            thread_lock.release()


def pr_lock(repo: str, pr: int):
    """Fail fast when another publisher/request owns this PR locally."""
    from loop_state import state_dir
    identity = hashlib.sha256(f"{repo.casefold()}#{pr}".encode("utf-8")).hexdigest()
    return file_lock(state_dir() / "locks" / f"pr-{identity}.lock", timeout=0)
