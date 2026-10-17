"""Explicit lifecycle setup, terminal recording, and portable receipt dispatch."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import finish_loop
import judge
import loop_lifecycle
import loop_state
import metrics
import start_loop


def save_run(repo="owner/repo", pr=1):
    state = loop_state.load_sticky_state()
    state[f"{repo}#{pr}"] = {"run": {"update_seq": 2, "started_at": "2026-10-17T10:00:00Z"}}
    loop_state.save_sticky_state(state)


@pytest.fixture
def setup_start(tmp_path, monkeypatch):
    monkeypatch.setattr(start_loop.pc, "git", lambda *args: str(tmp_path))
    monkeypatch.setattr(start_loop.rr, "read_persisted_reviewer", lambda *args: {"login": "coderabbitai"})
    save_run()
    return tmp_path


def test_start_requires_profile_decision_without_running_checks(setup_start, monkeypatch):
    monkeypatch.setattr(start_loop.pc, "run_profile", lambda *args: pytest.fail("start must not run verification"))
    with pytest.raises(RuntimeError, match="profile decision"):
        start_loop.start("owner/repo", 1, setup_start)
    judge.save_profile("owner/repo", source="skipped")
    result = start_loop.start("owner/repo", 1, setup_start)
    assert result["status"] == "ready" and result["profile_allows_publication"] is False


def test_start_accepts_confirmed_profile_and_exact_case_insensitive_pr(setup_start):
    judge.save_profile("owner/repo", source="confirmed", checks=[{"name": "tests", "command": [sys.executable, "-V"], "required": True}])
    assert start_loop.start("OWNER/REPO", 1, setup_start)["profile_allows_publication"] is True
    with pytest.raises(RuntimeError, match="fetch this PR"):
        start_loop.start("owner/repo", 2, setup_start)


def test_start_rejects_missing_reviewer(setup_start, monkeypatch):
    monkeypatch.setattr(start_loop.rr, "read_persisted_reviewer", lambda *args: None)
    with pytest.raises(RuntimeError, match="select a reviewer"):
        start_loop.start("owner/repo", 1, setup_start)


@pytest.mark.parametrize("dry_run", [False, True])
def test_finish_absent_run_never_calls_receipt(tmp_path, monkeypatch, dry_run):
    save_run(pr=2)
    monkeypatch.setattr(loop_lifecycle, "emit_receipt", lambda *a, **k: pytest.fail("wrong PR cannot finish"))
    assert finish_loop.finish("owner/repo", 1, tmp_path, dry_run=dry_run)["recorded"] is False
    assert loop_lifecycle.active_run("owner/repo", 2) is not None


def test_finish_runs_once_and_never_verifies(tmp_path, monkeypatch):
    save_run()
    calls = []
    def receipt(repo, pr, root, **kwargs):
        calls.append(kwargs)
        loop_state.save_sticky_state({})
        return {"output": "[loop] terminal receipt", "warnings": ""}
    monkeypatch.setattr(loop_lifecycle, "emit_receipt", receipt)
    monkeypatch.setattr(start_loop.pc, "run_profile", lambda *args: pytest.fail("finish must not rerun verification"))
    details = {"verification": "passed", "checks": [{"name": "tests", "status": "passed"}]}
    assert finish_loop.finish("owner/repo", 1, tmp_path, verification=details)["recorded"] is True
    assert finish_loop.finish("owner/repo", 1, tmp_path)["status"] == "already_finished"
    assert len(calls) == 1 and calls[0]["terminal"] is True and calls[0]["verification"] == details


def test_finish_dry_run_preserves_active_run(tmp_path, monkeypatch):
    save_run()
    monkeypatch.setattr(loop_lifecycle, "emit_receipt", lambda *a, **k: pytest.fail("dry run cannot finish"))
    assert finish_loop.finish("owner/repo", 1, tmp_path, dry_run=True)["status"] == "ready_to_finish"
    assert loop_lifecycle.active_run("owner/repo", 1) is not None


def test_finish_detects_failed_clear_and_blocks_duplicate_record(tmp_path, monkeypatch):
    save_run()
    calls = []
    def receipt(*args, **kwargs):
        calls.append(True)
        metrics.append_record({"schema_version": metrics.RECORD_SCHEMA_VERSION, "repo": "owner/repo", "pr": 1, "started_at": "2026-10-17T10:00:00Z"})
        return {"output": "[loop] recorded", "warnings": "could not clear tracking"}
    monkeypatch.setattr(loop_lifecycle, "emit_receipt", receipt)
    with pytest.raises(RuntimeError, match="did not clear"):
        finish_loop.finish("owner/repo", 1, tmp_path)
    with pytest.raises(RuntimeError, match="already recorded"):
        finish_loop.finish("OWNER/REPO", 1, tmp_path)
    assert calls == [True]


@pytest.mark.parametrize("returncode,output", [(1, ""), (0, "")])
def test_receipt_failure_is_not_treated_as_emitted(tmp_path, monkeypatch, returncode, output):
    monkeypatch.setattr(loop_lifecycle.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], returncode, output, "failure"))
    with pytest.raises(RuntimeError, match="receipt failed"):
        loop_lifecycle.emit_receipt("owner/repo", 1, tmp_path)


def test_receipt_uses_current_python_literal_argv_and_preserves_fallback(tmp_path, monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "[loop] full fallback receipt\n", "comment failed")
    monkeypatch.setattr(loop_lifecycle.subprocess, "run", run)
    details = {"verification": "passed", "checks": [{"command": [r"C:\path with spaces\python.exe", "-V"]}]}
    result = loop_lifecycle.emit_receipt("owner/repo", 1, tmp_path, verification=details, options=["--fixed-count", "2"])
    argv, kwargs = calls[0]
    assert argv[:2] == [sys.executable, str(Path(loop_lifecycle.__file__).with_name("fetch_gemini_threads.py"))]
    assert "--cycle-summary" in argv and "--auto-snapshot" not in argv and "--reviewer" not in argv
    assert json.loads(argv[argv.index("--verification-details") + 1]) == details
    assert kwargs["cwd"] == tmp_path and kwargs["timeout"] == 120 and not kwargs.get("shell")
    assert result == {"output": "[loop] full fallback receipt", "warnings": "comment failed"}
