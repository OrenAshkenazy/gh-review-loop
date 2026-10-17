"""Shared state-directory resolution and legacy migration."""

import json
from pathlib import Path

import pytest

import loop_state


class TestStateDirRename:
    """gh-gemini-review-loop -> gh-review-loop: resolution and migration."""

    @staticmethod
    def _home(monkeypatch, tmp_path):
        monkeypatch.delenv("GGRL_STATE_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        return tmp_path / ".config"

    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path / "custom"))
        assert loop_state.state_dir() == tmp_path / "custom"

    @pytest.mark.parametrize("platform", ["win32", "linux"])
    def test_config_root_uses_native_windows_home_and_posix_home(self, tmp_path, monkeypatch, platform):
        profile = tmp_path / "profile"
        monkeypatch.setattr(loop_state.sys, "platform", platform)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: profile))
        monkeypatch.setenv("HOME", "/c/Users/alice")
        expected = profile if platform == "win32" else Path("/c/Users/alice")
        assert loop_state._config_root() == expected / ".config"

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
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
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
