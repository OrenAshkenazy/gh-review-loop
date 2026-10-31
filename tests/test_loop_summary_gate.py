"""Unit tests for the summary-before-push PreToolUse gate decision.

During an active Gemini loop the agent must emit --cycle-summary before
pushing new commits. This gate enforces that mechanically: git push is
blocked while a loop is active for the repo and summary_is_stale() is True
(update_seq > last_summary_seq). Running --cycle-summary stamps
last_summary_seq and clears the gate.
"""

import io
import json
import os
import time

from fetch_gemini_threads import save_sticky_state
from loop_state import SENTINEL_TTL_SECONDS, sentinel_path, touch_sentinel
import pytest

from loop_summary_gate import main as summary_gate_main
from loop_summary_gate import push_target_dir, stale_summary_for_push


class _UnreadableStdin:
    def read(self):
        raise ValueError("invalid input")


def _active_stale(
    repo: str,
    number: int = 1,
    *,
    update_seq: int = 2,
    last_summary_seq: int = 0,
) -> None:
    """Write an active run with update_seq > last_summary_seq (stale summary)."""
    run: dict = {
        "started_at": "2026-06-06T10:00:00Z",
        "update_seq": update_seq,
    }
    if last_summary_seq:
        run["last_summary_seq"] = last_summary_seq
    save_sticky_state({f"{repo}#{number}": {"run": run}})


def _active_current(repo: str, number: int = 1) -> None:
    """Write an active run where summary is already current."""
    save_sticky_state({
        f"{repo}#{number}": {
            "run": {
                "started_at": "2026-06-06T10:00:00Z",
                "update_seq": 2,
                "last_summary_seq": 2,
            },
        },
    })


class TestStaleSummaryForPush:
    def test_main_fails_open_when_stdin_read_raises_value_error(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", _UnreadableStdin())
        assert summary_gate_main() == 0

    def test_main_fails_open_when_stdin_is_none(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", None)
        assert summary_gate_main() == 0

    def test_none_when_no_active_loop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert stale_summary_for_push("o/r") is None

    def test_none_when_update_seq_zero(self, tmp_path, monkeypatch):
        # A run with started_at but no update_seq has no fetched data yet —
        # it's legacy/pre-feature state, not a stale mid-loop summary.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#1": {"run": {"started_at": "t"}}})
        assert stale_summary_for_push("o/r") is None

    def test_none_when_summary_current(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_current("o/r")
        assert stale_summary_for_push("o/r") is None

    def test_none_for_other_repo_active_loop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("other/repo")
        assert stale_summary_for_push("o/r") is None

    def test_returns_info_when_stale(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("o/r", number=7, update_seq=3, last_summary_seq=1)
        info = stale_summary_for_push("o/r")
        assert info is not None
        assert info["pr_number"] == 7
        assert info["update_seq"] == 3
        assert info["last_summary_seq"] == 1

    def test_returns_info_when_never_summarized(self, tmp_path, monkeypatch):
        # last_summary_seq absent (first cycle, never called --cycle-summary).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("o/r", number=42, update_seq=1, last_summary_seq=0)
        info = stale_summary_for_push("o/r")
        assert info is not None
        assert info["pr_number"] == 42
        assert info["last_summary_seq"] == 0

    def test_clears_after_summary_stamped(self, tmp_path, monkeypatch):
        # After the agent calls --cycle-summary, last_summary_seq catches up
        # and the gate must clear.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("o/r", number=3, update_seq=2)
        # Simulate stamp_summary_emitted patching last_summary_seq to match.
        save_sticky_state({
            "o/r#3": {"run": {
                "started_at": "2026-06-06T10:00:00Z",
                "update_seq": 2,
                "last_summary_seq": 2,
            }},
        })
        assert stale_summary_for_push("o/r") is None

    def test_info_is_trimmed_to_fix_instruction_fields(self, tmp_path, monkeypatch):
        # #86: the block message is just the fix instruction — no embedded
        # state snapshot, so no snapshot fields in the info dict.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "o/r#5": {"run": {
                "started_at": "2026-06-07T08:00:00Z",
                "update_seq": 3,
                "finding_ids": ["id1", "id2"],
                "finding_paths": ["src/foo.py", "src/bar.py"],
            }},
        })
        info = stale_summary_for_push("o/r")
        assert info == {"pr_number": 5, "update_seq": 3, "last_summary_seq": 0}


class TestStaleSentinelReap:
    def test_stale_sentinel_allows_push_and_is_reaped(self, tmp_path, monkeypatch):
        # #103: the shell guard is existence-only, so the gate must disarm a
        # crashed loop itself — and stand down before any repo resolution
        # (resolve_current_repo would trip the hermetic-gh guard here).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("o/r", number=7)
        touch_sentinel()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(sentinel_path(), (old, old))
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": "git push origin"}}
            )),
        )

        assert summary_gate_main() == 0
        assert not sentinel_path().exists()

    def test_stale_sentinel_reaped_on_non_push_command(self, tmp_path, monkeypatch):
        # PR #110 review finding: the reap must run BEFORE the git-push filter,
        # or ordinary Bash calls keep spawning this gate off a dead loop's
        # sentinel until a push happens.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        touch_sentinel()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(sentinel_path(), (old, old))
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
            )),
        )

        assert summary_gate_main() == 0
        assert not sentinel_path().exists()


class TestBlockMessage:
    def test_block_is_one_instruction_without_snapshot(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("o/r", number=7)
        monkeypatch.setattr("loop_summary_gate.resolve_current_repo", lambda cwd=None: "o/r")
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": "git push origin"}}
            )),
        )

        assert summary_gate_main() == 2

        err = capsys.readouterr().err
        assert "--cycle-summary" in err
        assert "https://github.com/o/r/pull/7" in err
        assert "snapshot" not in err.lower()
        assert "Threads tracked" not in err


class TestPushTarget:
    @pytest.mark.parametrize("command, cwd, expected", [
        ("git push origin HEAD", "/base", None),
        ("cd /work/clone && git add -A && git commit -qm x && git push -q origin HEAD", "/base", "/work/clone"),
        ("git -C /work/clone push origin", "/base", "/work/clone"),
        ("cd sub && git push", "/base", "/base/sub"),
        ('cd "/tmp/my clone" && git push', None, "/tmp/my clone"),
        ("cd /a && make && cd /b && git push", None, "/b"),
        ("(cd /a && git push)", None, "/a"),
        ("git push && cd /a", None, None),
    ])
    def test_the_push_runs_where_the_command_says(self, command, cwd, expected):
        assert push_target_dir(command, cwd) == expected

    def test_a_tilde_target_is_expanded(self):
        assert push_target_dir("cd ~/clone && git push", None) == os.path.expanduser("~/clone")

    def _run(self, monkeypatch, command, cwd, repos):
        seen = []

        def resolve(cwd=None):
            seen.append(cwd)
            return repos.get(cwd, "o/session")

        monkeypatch.setattr("loop_summary_gate.resolve_current_repo", resolve)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
            {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}
        )))
        return summary_gate_main(), seen

    def test_a_push_into_the_looped_repo_from_another_cwd_is_gated(self, tmp_path, monkeypatch, capsys):
        """The session ran from another repo and pushed with `cd <clone> &&
        git push`; the gate resolved the session's repo, found no loop there,
        and let cycles push without a summary."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("o/looped", number=7)

        rc, seen = self._run(
            monkeypatch, "cd /work/clone && git commit -qm fix && git push -q origin HEAD",
            "/work/session", {"/work/clone": "o/looped"},
        )

        assert rc == 2
        assert seen == ["/work/clone"]
        assert "https://github.com/o/looped/pull/7" in capsys.readouterr().err

    def test_git_dash_c_is_a_push_and_is_gated(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("o/looped", number=7)

        rc, seen = self._run(monkeypatch, "git -C /work/clone push", "/work/session", {"/work/clone": "o/looped"})

        assert rc == 2
        assert seen == ["/work/clone"]

    def test_a_push_of_another_repo_is_not_gated_by_the_sessions_loop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("o/session", number=7)

        rc, _ = self._run(monkeypatch, "cd /work/other && git push", "/work/session", {"/work/other": "o/other"})

        assert rc == 0
