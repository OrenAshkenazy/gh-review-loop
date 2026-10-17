"""Tests for the tiered OpenAI API-key resolver.

The resolver is the ONE place that shells out to ``security`` / ``secret-tool``,
so we cover: tiered precedence, source labelling, dotfile round-trip,
keychain delegation, and CLI exit codes. Subprocess calls are stubbed —
tests never hit a real keyring on the runner.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_SCRIPTS = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "gh-review-loop" / "skills" / "gh-review-loop" / "scripts"
)
sys.path.insert(0, str(PLUGIN_SCRIPTS))

import key_resolver  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    # Default: every keystore reader returns None. Individual tests override
    # specific entries to test precedence without touching real keychains.
    for label in ("macos_keychain", "linux_secret_service"):
        monkeypatch.setitem(key_resolver._READERS, label, lambda: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Also stub `security` / `secret-tool` discovery so store/clear paths
    # fall back to the dotenv writer in the tmp dir. Without this, tests
    # on a developer Mac would write real keys to the real Keychain.
    monkeypatch.setattr(key_resolver.shutil, "which", lambda _cmd: None)


class TestResolutionPrecedence:
    def test_missing_when_no_source(self):
        assert key_resolver.resolve_api_key() == (None, "missing")

    def test_dotenv_is_first_source(self, tmp_path):
        (tmp_path / ".env").write_text('OPENAI_API_KEY="sk-dot"\n', encoding="utf-8")
        assert key_resolver.resolve_api_key() == ("sk-dot", "dotenv")

    def test_env_var_is_last_resort(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        assert key_resolver.resolve_api_key() == ("sk-env", "env")

    def test_dotenv_beats_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-dot\n", encoding="utf-8")
        assert key_resolver.resolve_api_key() == ("sk-dot", "dotenv")

    def test_dotenv_beats_keychain(self, monkeypatch, tmp_path):
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-dot\n", encoding="utf-8")
        monkeypatch.setitem(key_resolver._READERS, "macos_keychain", lambda: "sk-kc")
        assert key_resolver.resolve_api_key() == ("sk-dot", "dotenv")

    def test_keychain_when_others_absent(self, monkeypatch):
        monkeypatch.setitem(key_resolver._READERS, "macos_keychain", lambda: "sk-kc")
        assert key_resolver.resolve_api_key() == ("sk-kc", "macos_keychain")

    def test_secret_tool_when_only_source(self, monkeypatch):
        monkeypatch.setitem(
            key_resolver._READERS, "linux_secret_service", lambda: "sk-st"
        )
        assert key_resolver.resolve_api_key() == ("sk-st", "linux_secret_service")

    def test_reader_exception_does_not_crash_resolver(self, monkeypatch):
        # If keychain throws an unexpected error (corrupt entry, denied
        # by user prompt), the resolver must fall through to the next
        # source rather than propagate. Otherwise one broken backend
        # would brick the judge.
        def _boom():
            raise RuntimeError("denied by user")

        monkeypatch.setitem(key_resolver._READERS, "macos_keychain", _boom)
        monkeypatch.setitem(
            key_resolver._READERS, "linux_secret_service", lambda: "sk-fallback"
        )
        assert key_resolver.resolve_api_key() == ("sk-fallback", "linux_secret_service")


class TestDotenvIO:
    def test_quoted_value_is_parsed(self, tmp_path):
        (tmp_path / ".env").write_text('OPENAI_API_KEY="sk-quoted"\n', encoding="utf-8")
        assert key_resolver._read_dotenv() == "sk-quoted"

    def test_unquoted_value_is_parsed(self, tmp_path):
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-bare\n", encoding="utf-8")
        assert key_resolver._read_dotenv() == "sk-bare"

    def test_comments_and_blanks_skipped(self, tmp_path):
        (tmp_path / ".env").write_text(
            "# comment\n\nOTHER=x\nOPENAI_API_KEY=sk-yes\n", encoding="utf-8"
        )
        assert key_resolver._read_dotenv() == "sk-yes"

    def test_missing_file_returns_none(self):
        assert key_resolver._read_dotenv() is None

    def test_store_dotenv_writes_chmod_600(self, tmp_path):
        key_resolver._store_dotenv("sk-stored")
        path = tmp_path / ".env"
        assert path.exists()
        assert key_resolver._read_dotenv() == "sk-stored"
        # chmod check is best-effort on systems that support it
        if sys.platform != "win32":
            mode = path.stat().st_mode & 0o777
            # Not every OS honors chmod, but where it does, owner-only.
            if mode != 0:
                assert mode == 0o600

    def test_store_dotenv_preserves_other_keys(self, tmp_path):
        (tmp_path / ".env").write_text(
            "OTHER=keep\nOPENAI_API_KEY=old\n", encoding="utf-8"
        )
        key_resolver._store_dotenv("sk-new")
        body = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "OTHER=keep" in body
        assert "sk-new" in body
        assert "old" not in body

    def test_store_dotenv_replaces_spaced_assignment(self, tmp_path):
        # Regression: a plain startswith("OPENAI_API_KEY=") would skip
        # `OPENAI_API_KEY = "old"` (with spaces around =) and leave the
        # stale value behind. The partition-based filter must catch it.
        (tmp_path / ".env").write_text(
            'OPENAI_API_KEY = "old"\n', encoding="utf-8"
        )
        key_resolver._store_dotenv("sk-new")
        body = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "old" not in body
        assert "sk-new" in body
        assert key_resolver._read_dotenv() == "sk-new"


class TestStoreInputValidation:
    def test_non_string_key_raises_typeerror(self):
        # Defensive: bool / int / None from a future plumbing path must
        # raise a clear TypeError instead of crashing inside .strip() with
        # an opaque AttributeError.
        import pytest as _pytest

        with _pytest.raises(TypeError):
            key_resolver.store_api_key(True)
        with _pytest.raises(TypeError):
            key_resolver.store_api_key(None)


class TestSecretToolStorageShape:
    def test_no_trailing_newline_sent_to_secret_tool(self, monkeypatch):
        # secret-tool reads stdin until EOF, so any "\n" we append is
        # persisted as part of the secret. Retrieval would then return
        # "sk-...\n" and break a downstream Bearer header silently.
        captured = {}

        def _fake_run(cmd, **kwargs):  # noqa: ARG001
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")

            class _R:
                returncode = 0
                stdout = ""

            return _R()

        monkeypatch.setattr(key_resolver.subprocess, "run", _fake_run)
        key_resolver._store_linux_secret_service("sk-test-123")
        assert captured["input"] == "sk-test-123"
        assert not captured["input"].endswith("\n")


class TestClear:
    def test_clear_removes_dotenv_key_only(self, tmp_path):
        (tmp_path / ".env").write_text(
            "OTHER=keep\nOPENAI_API_KEY=old\n", encoding="utf-8"
        )
        cleared = key_resolver.clear_api_key()
        assert "dotenv" in cleared
        body = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "OTHER=keep" in body
        assert "OPENAI_API_KEY" not in body

    def test_clear_deletes_dotfile_when_only_key(self, tmp_path):
        (tmp_path / ".env").write_text("OPENAI_API_KEY=old\n", encoding="utf-8")
        key_resolver.clear_api_key()
        assert not (tmp_path / ".env").exists()

    def test_clear_returns_empty_when_nothing(self):
        assert key_resolver.clear_api_key() == []

    def test_clear_handles_spaced_assignment(self, tmp_path):
        # Companion regression to test_store_dotenv_replaces_spaced_assignment:
        # the strict startswith filter would have left this line untouched.
        (tmp_path / ".env").write_text(
            'OTHER=keep\nOPENAI_API_KEY = "old"\n', encoding="utf-8"
        )
        key_resolver.clear_api_key()
        body = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "OTHER=keep" in body
        assert "OPENAI_API_KEY" not in body


class TestRedact:
    def test_short_fully_redacted(self):
        assert key_resolver._redact("abc") == "***"

    def test_long_keeps_prefix_and_suffix(self):
        assert key_resolver._redact("sk-abcdefghijklmnop1234") == "sk-abc...1234"


class TestCLI:
    def test_print_source_missing_returns_1(self, capsys):
        rc = key_resolver._main(["--print-source"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "source: missing" in out

    def test_print_source_dotenv_returns_0(self, tmp_path, capsys):
        (tmp_path / ".env").write_text('OPENAI_API_KEY="sk-cli-dot-abcdef"\n', encoding="utf-8")
        rc = key_resolver._main(["--print-source"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "source: dotenv" in out
        # Redacted, never raw.
        assert "sk-cli-dot-abcdef" not in out

    def test_print_source_env_returns_0(self, monkeypatch, capsys):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-cli-env-abcdef")
        rc = key_resolver._main(["--print-source"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "source: env" in out
        assert "sk-cli-env-abcdef" not in out

    def test_set_from_stdin(self, monkeypatch, capsys, tmp_path):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("sk-from-stdin\n"))
        rc = key_resolver._main(["--set", "--from-stdin"])
        out = capsys.readouterr().out
        assert rc == 0
        # On a CI runner without `security` / `secret-tool`, store falls back
        # to dotenv. Either outcome is acceptable.
        assert "stored in:" in out

    def test_set_empty_returns_2(self, monkeypatch, capsys):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
        rc = key_resolver._main(["--set", "--from-stdin"])
        capsys.readouterr()
        assert rc == 2

    def test_clear_with_nothing_stored(self, capsys):
        rc = key_resolver._main(["--clear"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "nothing to clear" in out
