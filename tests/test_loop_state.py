"""Sentinel-file fast path for the bundled hooks (#83).

The hooks.json commands are shell guards that exit before spawning Python
unless the loop-active sentinel exists (and is younger than the 24h TTL).
These tests cover the sentinel lifecycle in Python (created on fetch,
cleared by the terminal --record-run path, TTL semantics) and the actual
guard commands shipped in hooks.json, run through `sh` with a stub python3
so we can observe whether the interpreter would have been spawned.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import loop_state
from fetch_gemini_threads import (
    PullRequest,
    clear_run_tracking,
    update_run_tracking,
)
from loop_state import (
    SENTINEL_TTL_SECONDS,
    any_active_run,
    clear_sentinel,
    reap_stale_sentinel,
    sentinel_is_stale,
    sentinel_path,
    touch_sentinel,
)

HOOKS_JSON = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "gh-review-loop" / "hooks" / "hooks.json"
)
PLUGIN_ROOT = HOOKS_JSON.parent.parent
SCRIPTS = PLUGIN_ROOT / "skills" / "gh-review-loop" / "scripts"


def _pr(owner: str = "o", repo: str = "r", number: int = 1) -> PullRequest:
    return PullRequest(owner=owner, repo=repo, number=number)


class TestSentinelLifecycle:
    def test_touch_creates_sentinel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert not sentinel_path().exists()
        touch_sentinel()
        assert sentinel_path().exists()

    def test_touch_refreshes_mtime(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        touch_sentinel()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(sentinel_path(), (old, old))
        touch_sentinel()
        assert not sentinel_is_stale()

    def test_clear_missing_sentinel_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        clear_sentinel()  # must not raise

    def test_fetch_creates_sentinel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(_pr(), [("t1", "a.py")])
        assert sentinel_path().exists()

    def test_record_run_clears_sentinel_when_last_loop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(_pr(), [("t1", "a.py")])
        clear_run_tracking(_pr())
        assert not sentinel_path().exists()

    def test_record_run_keeps_sentinel_while_other_loop_active(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(_pr("o", "r", 1), [("t1", "a.py")])
        update_run_tracking(_pr("other", "repo", 9), [("t2", "b.py")])
        clear_run_tracking(_pr("o", "r", 1))
        assert sentinel_path().exists()
        clear_run_tracking(_pr("other", "repo", 9))
        assert not sentinel_path().exists()


class TestStateDirRename:
    """gh-gemini-review-loop -> gh-review-loop: resolution and migration."""

    @staticmethod
    def _home(monkeypatch, tmp_path):
        monkeypatch.delenv("GGRL_STATE_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        return tmp_path / ".config"

    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path / "custom"))
        assert loop_state.state_dir() == tmp_path / "custom"

    def test_fresh_install_resolves_to_new_name(self, tmp_path, monkeypatch):
        cfg = self._home(monkeypatch, tmp_path)
        assert loop_state.state_dir() == cfg / "gh-review-loop"

    def test_unmigrated_install_resolves_to_legacy(self, tmp_path, monkeypatch):
        cfg = self._home(monkeypatch, tmp_path)
        (cfg / "gh-gemini-review-loop").mkdir(parents=True)
        assert loop_state.state_dir() == cfg / "gh-gemini-review-loop"

    def test_new_dir_wins_when_both_exist(self, tmp_path, monkeypatch):
        cfg = self._home(monkeypatch, tmp_path)
        (cfg / "gh-gemini-review-loop").mkdir(parents=True)
        (cfg / "gh-review-loop").mkdir()
        assert loop_state.state_dir() == cfg / "gh-review-loop"

    def test_migration_renames_and_leaves_symlink(self, tmp_path, monkeypatch, symlinks_available):
        cfg = self._home(monkeypatch, tmp_path)
        legacy = cfg / "gh-gemini-review-loop"
        legacy.mkdir(parents=True)
        (legacy / "state.json").write_text("{}")

        assert loop_state.migrate_legacy_state_dir() is True

        new = cfg / "gh-review-loop"
        assert (new / "state.json").exists()
        # Old plugin versions hardcode the legacy path — it must still work.
        assert legacy.is_symlink()
        assert (legacy / "state.json").read_text() == "{}"
        assert loop_state.state_dir() == new

    def test_migration_is_idempotent(self, tmp_path, monkeypatch, symlinks_available):
        cfg = self._home(monkeypatch, tmp_path)
        (cfg / "gh-gemini-review-loop").mkdir(parents=True)
        assert loop_state.migrate_legacy_state_dir() is True
        assert loop_state.migrate_legacy_state_dir() is False  # symlink now

    def test_migration_never_merges_into_existing_new_dir(self, tmp_path, monkeypatch):
        cfg = self._home(monkeypatch, tmp_path)
        legacy = cfg / "gh-gemini-review-loop"
        legacy.mkdir(parents=True)
        (legacy / "state.json").write_text('{"old": true}')
        new = cfg / "gh-review-loop"
        new.mkdir()
        (new / "state.json").write_text('{"new": true}')

        assert loop_state.migrate_legacy_state_dir() is False
        assert json.loads((new / "state.json").read_text()) == {"new": True}
        assert not legacy.is_symlink()

    def test_migration_noop_without_legacy_dir(self, tmp_path, monkeypatch):
        self._home(monkeypatch, tmp_path)
        assert loop_state.migrate_legacy_state_dir() is False

    def test_migration_noop_under_env_override(self, tmp_path, monkeypatch):
        cfg = tmp_path / ".config"
        (cfg / "gh-gemini-review-loop").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path / "custom"))
        assert loop_state.migrate_legacy_state_dir() is False
        assert not (cfg / "gh-gemini-review-loop").is_symlink()

    def test_migration_rolls_back_when_the_symlink_cannot_be_created(
        self, tmp_path, monkeypatch
    ):
        # Filesystems that refuse symlinks must not strand the state where
        # pre-rename installs cannot see it: undo the rename and keep resolving
        # to the legacy dir, which is what "fails open" promises.
        cfg = self._home(monkeypatch, tmp_path)
        legacy = cfg / "gh-gemini-review-loop"
        legacy.mkdir(parents=True)
        (legacy / "state.json").write_text('{"old": true}')

        def _no_symlinks(self, target, target_is_directory=False):
            raise OSError("symlinks unsupported")

        monkeypatch.setattr(Path, "symlink_to", _no_symlinks)

        assert loop_state.migrate_legacy_state_dir() is False
        assert not (cfg / "gh-review-loop").exists()
        assert legacy.is_dir() and not legacy.is_symlink()
        assert json.loads((legacy / "state.json").read_text()) == {"old": True}
        assert loop_state.state_dir() == legacy

    def test_all_state_files_resolve_through_the_shared_dir(
        self, tmp_path, monkeypatch
    ):
        # judge/key_resolver/metrics/request_rereview and the fetch script's
        # judge-unavailable prefs fallback must not keep their own copies of the
        # path logic — one resolver, six consumers.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads
        import judge
        import key_resolver
        import metrics
        import request_rereview
        assert judge.prefs_path() == tmp_path / "preferences.json"
        assert key_resolver.dotenv_path() == tmp_path / ".env"
        assert metrics.runs_log_path() == tmp_path / "runs.jsonl"
        assert request_rereview.state_path() == tmp_path / "state.json"
        assert (
            fetch_gemini_threads._direct_preferences_path()
            == tmp_path / "preferences.json"
        )

    def test_direct_prefs_fallback_reads_the_unmigrated_legacy_dir(
        self, tmp_path, monkeypatch
    ):
        # When migration fails open the prefs stay in the legacy dir; the
        # judge-unavailable fallback must follow them there rather than read a
        # nonexistent new dir and silently return defaults.
        cfg = self._home(monkeypatch, tmp_path)
        legacy = cfg / "gh-gemini-review-loop"
        legacy.mkdir(parents=True)
        import fetch_gemini_threads
        assert (
            fetch_gemini_threads._direct_preferences_path()
            == legacy / "preferences.json"
        )


class TestSentinelWriteFailureIsObservable:
    """A sentinel that cannot be written turns the gates OFF, not on.

    The shell guard exits 0 when the marker is absent, so a swallowed OSError
    would silently drop the edit gate, the push gate and the Stop backstop
    while state.json still says a run is active. The failure must be reported.
    """

    @staticmethod
    def _unwritable(monkeypatch, tmp_path) -> Path:
        # A regular file where the state dir should be: mkdir(parents=True)
        # raises NotADirectoryError (an OSError) on every call.
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        monkeypatch.setenv("GGRL_STATE_DIR", str(blocker / "state"))
        return blocker

    def test_touch_returns_true_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert touch_sentinel() is True

    def test_touch_returns_false_when_it_cannot_write(self, tmp_path, monkeypatch):
        self._unwritable(monkeypatch, tmp_path)
        assert touch_sentinel() is False
        assert not sentinel_path().exists()

    def test_fetch_warns_when_the_sentinel_cannot_be_written(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr("fetch_gemini_threads.touch_sentinel", lambda: False)

        update_run_tracking(_pr(), [("t1", "a.py")])

        err = capsys.readouterr().err
        assert "loop-active sentinel" in err
        # Names the concrete guarantees that are gone, not just "warning".
        assert "push gate" in err
        assert "backstop" in err

    def test_fetch_is_silent_when_the_sentinel_is_written(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(_pr(), [("t1", "a.py")])
        assert "sentinel" not in capsys.readouterr().err

    def test_run_state_still_persists_when_the_sentinel_fails(
        self, tmp_path, monkeypatch
    ):
        # Fail-open: the loop keeps working, only the gates go quiet.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr("fetch_gemini_threads.touch_sentinel", lambda: False)
        update_run_tracking(_pr(), [("t1", "a.py")])
        assert any_active_run() is True


class TestSentinelTtl:
    def test_missing_sentinel_is_not_stale(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert not sentinel_is_stale()

    def test_fresh_sentinel_is_not_stale(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        touch_sentinel()
        assert not sentinel_is_stale()

    def test_sentinel_past_ttl_is_stale(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        touch_sentinel()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(sentinel_path(), (old, old))
        assert sentinel_is_stale()

    def test_reap_removes_stale_sentinel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        touch_sentinel()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(sentinel_path(), (old, old))
        assert reap_stale_sentinel()
        assert not sentinel_path().exists()

    def test_reap_keeps_fresh_sentinel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        touch_sentinel()
        assert not reap_stale_sentinel()
        assert sentinel_path().exists()

    def test_reap_on_missing_sentinel_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert not reap_stale_sentinel()

    def test_reap_finds_stale_legacy_sentinel_in_split_dir_state(
        self, tmp_path, monkeypatch
    ):
        # PR #110 review: both config dirs exist, marker only under legacy.
        # The shell guard arms from the legacy marker, so the reap must
        # inspect it too — not just state_dir()'s preferred new dir.
        monkeypatch.delenv("GGRL_STATE_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        new_dir = tmp_path / ".config" / "gh-review-loop"
        legacy_dir = tmp_path / ".config" / "gh-gemini-review-loop"
        new_dir.mkdir(parents=True)
        legacy_dir.mkdir(parents=True)
        legacy = legacy_dir / "loop-active"
        legacy.touch()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(legacy, (old, old))

        assert reap_stale_sentinel()
        assert not legacy.exists()

    def test_reap_keeps_gates_armed_when_new_sentinel_is_fresh(
        self, tmp_path, monkeypatch
    ):
        # A stale legacy twin is cleaned up, but the fresh new-dir sentinel
        # means a loop is active — the gate must NOT stand down.
        monkeypatch.delenv("GGRL_STATE_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        new_dir = tmp_path / ".config" / "gh-review-loop"
        legacy_dir = tmp_path / ".config" / "gh-gemini-review-loop"
        new_dir.mkdir(parents=True)
        legacy_dir.mkdir(parents=True)
        (new_dir / "loop-active").touch()
        legacy = legacy_dir / "loop-active"
        legacy.touch()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(legacy, (old, old))

        assert not reap_stale_sentinel()
        assert not legacy.exists(), "stale legacy twin should still be cleaned"
        assert (new_dir / "loop-active").exists()

    def test_reap_stands_down_when_unlink_fails(self, tmp_path, monkeypatch):
        # PR #110 review: a read-only state dir must not wedge the gates on a
        # dead run — a positively identified stale sentinel stands the gate
        # down even when deletion fails (the old shell guard's rm -f was
        # equally fail-open).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        touch_sentinel()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(sentinel_path(), (old, old))

        def deny_unlink(self, missing_ok=False):
            raise OSError("read-only state dir")

        monkeypatch.setattr(Path, "unlink", deny_unlink)
        assert reap_stale_sentinel()

    def test_reap_ignores_fresh_unselected_legacy_sentinel(
        self, tmp_path, monkeypatch
    ):
        # PR #110 review, inverse split: the guard selects the NEW marker
        # (stale); a fresh legacy marker it did not select must not keep the
        # gates armed for the dead run — the fresh loop re-arms them on the
        # next hook fire once the stale marker is gone.
        monkeypatch.delenv("GGRL_STATE_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        new_dir = tmp_path / ".config" / "gh-review-loop"
        legacy_dir = tmp_path / ".config" / "gh-gemini-review-loop"
        new_dir.mkdir(parents=True)
        legacy_dir.mkdir(parents=True)
        stale_new = new_dir / "loop-active"
        stale_new.touch()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(stale_new, (old, old))
        (legacy_dir / "loop-active").touch()

        assert reap_stale_sentinel()
        assert not stale_new.exists()
        assert (legacy_dir / "loop-active").exists()


class TestSlimImports:
    def test_hook_entrypoints_do_not_import_fetch_module(self, tmp_path):
        """The gates exist to be cheap; importing the 3,800-line main module
        at hook time was the bulk of the ~110ms idle cost (#83)."""
        code = (
            "import loop_summary_gate, loop_profile_gate, loop_summary_hook, sys; "
            "assert 'fetch_gemini_threads' not in sys.modules, "
            "'hook entrypoints must not import fetch_gemini_threads'"
        )
        env = dict(os.environ, GGRL_STATE_DIR=str(tmp_path))
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=SCRIPTS,
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr


def _hook_commands() -> list[tuple[str, str]]:
    data = json.loads(HOOKS_JSON.read_text())
    commands = []
    for event, entries in data["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                commands.append((event, hook["command"]))
    return commands


@pytest.fixture
def guard_env(tmp_path):
    """Env for running a hooks.json command with a stub python3 on PATH.

    The stub appends to a marker file instead of executing the real gate, so
    a test can assert whether the guard would have spawned the interpreter.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    marker = tmp_path / "python-ran"
    shim = shim_dir / "python3"
    shim.write_text(f'#!/bin/sh\necho ran >> "{marker}"\n')
    shim.chmod(0o755)
    env = {
        "PATH": f"{shim_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "GGRL_STATE_DIR": str(state_dir),
        "GGRL_PLUGIN_ROOT": str(PLUGIN_ROOT),
    }
    return {"env": env, "state_dir": state_dir, "marker": marker}


def _run_guard(command: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "-c", command],
        input="{}",
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.skipif(os.name == "nt", reason="Claude hooks require a POSIX shell")
class TestHooksJsonGuard:
    @pytest.mark.parametrize(("event", "command"), _hook_commands())
    def test_no_sentinel_skips_python(self, guard_env, event, command):
        proc = _run_guard(command, guard_env["env"])
        assert proc.returncode == 0, proc.stderr
        assert not guard_env["marker"].exists(), "python3 spawned on idle path"

    @pytest.mark.parametrize(("event", "command"), _hook_commands())
    def test_fresh_sentinel_spawns_python(self, guard_env, event, command):
        (guard_env["state_dir"] / "loop-active").touch()
        proc = _run_guard(command, guard_env["env"])
        assert proc.returncode == 0, proc.stderr
        assert guard_env["marker"].exists(), "python3 not spawned during loop"

    @pytest.mark.parametrize(("event", "command"), _hook_commands())
    def test_stale_sentinel_still_spawns_python_for_reaping(
        self, guard_env, event, command
    ):
        # #103: the guard is existence-only; staleness moved into the Python
        # gates (reap_stale_sentinel), which only run when the file exists.
        sentinel = guard_env["state_dir"] / "loop-active"
        sentinel.touch()
        old = time.time() - SENTINEL_TTL_SECONDS - 3600
        os.utime(sentinel, (old, old))
        proc = _run_guard(command, guard_env["env"])
        assert proc.returncode == 0, proc.stderr
        assert guard_env["marker"].exists(), "python3 not spawned to reap stale sentinel"

    @pytest.mark.parametrize(("event", "command"), _hook_commands())
    def test_default_state_dir_uses_new_home_path(self, guard_env, event, command):
        env = dict(guard_env["env"])
        del env["GGRL_STATE_DIR"]
        home = Path(env["HOME"])
        sentinel = home / ".config" / "gh-review-loop" / "loop-active"
        sentinel.parent.mkdir(parents=True)
        sentinel.touch()
        proc = _run_guard(command, env)
        assert proc.returncode == 0, proc.stderr
        assert guard_env["marker"].exists(), "python3 not spawned via $HOME new path"

    @pytest.mark.parametrize(("event", "command"), _hook_commands())
    def test_default_state_dir_falls_back_to_legacy_home_path(
        self, guard_env, event, command
    ):
        # Pre-rename install that has not been migrated: sentinel only exists
        # under the old dir name. The guard must still arm the gates.
        env = dict(guard_env["env"])
        del env["GGRL_STATE_DIR"]
        home = Path(env["HOME"])
        sentinel = home / ".config" / "gh-gemini-review-loop" / "loop-active"
        sentinel.parent.mkdir(parents=True)
        sentinel.touch()
        proc = _run_guard(command, env)
        assert proc.returncode == 0, proc.stderr
        assert guard_env["marker"].exists(), "python3 not spawned via legacy fallback"

    @pytest.mark.parametrize(("event", "command"), _hook_commands())
    def test_explicit_state_dir_skips_legacy_fallback(self, guard_env, event, command):
        # GGRL_STATE_DIR is authoritative: a legacy sentinel must not arm the
        # gates when the caller pointed the state somewhere else.
        env = dict(guard_env["env"])  # GGRL_STATE_DIR set, its dir empty
        home = Path(env["HOME"])
        legacy = home / ".config" / "gh-gemini-review-loop" / "loop-active"
        legacy.parent.mkdir(parents=True)
        legacy.touch()
        proc = _run_guard(command, env)
        assert proc.returncode == 0, proc.stderr
        assert not guard_env["marker"].exists(), "legacy fallback overrode GGRL_STATE_DIR"

    def test_guard_is_existence_check_only(self):
        """#103: the always-on shell guard must stay a bare [ -f ] test — no
        find/stat subprocesses on the per-tool-call hot path. Staleness lives
        in the Python gates (reap_stale_sentinel)."""
        for _event, command in _hook_commands():
            assert "find" not in command
            assert "mmin" not in command

    def test_stop_hook_timeout_is_bounded(self):
        """#103: a stuck Stop hook must not stall session end for 2+ minutes.
        The 70s budget covers the summary subprocess's own 60s timeout."""
        data = json.loads(HOOKS_JSON.read_text())
        (stop_entry,) = data["hooks"]["Stop"]
        (stop_hook,) = stop_entry["hooks"]
        assert stop_hook["timeout"] <= 70

    def test_no_status_messages(self):
        """statusMessage rendered on every matching tool call in every project;
        the fix removes it outright (#83)."""
        assert "statusMessage" not in HOOKS_JSON.read_text()


def test_a_stray_file_at_the_new_path_does_not_shadow_legacy_data(tmp_path, monkeypatch):
    """A file named gh-review-loop must not win resolution over a legacy dir.

    exists() is true for files, so the old check routed every read to a path
    that cannot hold state and every write into an uncaught FileExistsError,
    while the user's real data sat untouched in the legacy directory.
    """
    monkeypatch.delenv("GGRL_STATE_DIR", raising=False)
    monkeypatch.setattr(loop_state, "_config_root", lambda: tmp_path)
    legacy = tmp_path / loop_state.LEGACY_STATE_DIR_NAME
    legacy.mkdir()
    (legacy / "preferences.json").write_text("{}")
    (tmp_path / loop_state.STATE_DIR_NAME).write_text("not a directory")

    assert loop_state.state_dir() == legacy
