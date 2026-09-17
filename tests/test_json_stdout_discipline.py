"""Regression tests for machine-readable JSON stdout paths."""

from __future__ import annotations

import json
import sys

import detect_profile
import fetch_gemini_threads as fgt
import judge
import pytest

import request_rereview
from conftest import stub_cap_check_known_under_cap
from fetch_gemini_threads import PullRequest
from run_profile import main as run_profile_main


def assert_json_stdout(stdout: str) -> dict:
    """stdout must be exactly one JSON document, with no ANSI escapes.

    json.loads over the whole string is the discipline check: any human block
    printed before or after the payload fails the parse. "[loop]" may appear
    *inside* string fields (e.g. humanBlocks, #99) — that is payload, not
    leakage — so there is deliberately no raw substring check for it.
    """
    assert stdout.strip()
    assert "\033[" not in stdout
    return json.loads(stdout)


def test_fetch_threads_json_stdout_is_json_only_with_progress_on_stderr(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("GGRL_NO_COLOR", raising=False)

    pr = PullRequest(owner="o", repo="r", number=7)
    thread = {
        "id": "thread-1",
        "path": "app.py",
        "line": 12,
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {
                    "author": {"login": "chatgpt-codex-connector"},
                    "body": "Please fix this.",
                    "createdAt": "2026-06-09T07:17:16Z",
                    "url": "https://github.example/thread-1",
                }
            ]
        },
    }
    pull_request = {
        "reviewThreads": {"nodes": [thread]},
        "reviews": {"nodes": []},
        "comments": {"nodes": []},
    }

    monkeypatch.setattr(fgt, "resolve_pr", lambda spec: pr)
    monkeypatch.setattr(fgt, "fetch_threads", lambda resolved_pr: pull_request)
    monkeypatch.setattr(fgt, "gh_authenticated_login", lambda: "agent")
    monkeypatch.setattr(sys, "argv", [
        "fetch_gemini_threads.py",
        "--format",
        "json",
        "--judge-mode",
        "off",
    ])

    rc = fgt.main()

    captured = capsys.readouterr()
    payload = assert_json_stdout(captured.out)
    assert rc == 0
    assert payload["loopStatus"]["agentLogin"] == "agent"
    assert payload["threads"][0]["id"] == "thread-1"
    assert "Counting only re-reviews posted by 'agent'" in captured.err


def test_fetch_stats_json_stdout_is_json_only_with_color_enabled(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("GGRL_NO_COLOR", raising=False)
    monkeypatch.setattr(fgt.metrics, "load_records", lambda: ([], 0))
    monkeypatch.setattr(sys, "argv", [
        "fetch_gemini_threads.py",
        "--stats",
        "--stats-all-repos",
        "--format",
        "json",
    ])

    rc = fgt.main()

    captured = capsys.readouterr()
    payload = assert_json_stdout(captured.out)
    assert rc == 0
    assert payload["repo"] == "(all repos)"
    assert captured.err == ""


def test_run_profile_json_stdout_is_json_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    judge.save_profile(
        "o/r",
        source="confirmed",
        checks=[
            {
                "name": "ok",
                "command": 'python3 -c "import sys; sys.exit(0)"',
                "required": True,
            }
        ],
    )

    rc = run_profile_main(["run_profile.py", "o/r", str(tmp_path)])

    captured = capsys.readouterr()
    payload = assert_json_stdout(captured.out)
    assert rc == 0
    assert payload["verification"] == "passed"
    assert captured.err == ""


def test_run_profile_skipped_json_stdout_is_json_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    judge.save_profile("o/r", source="skipped")

    rc = run_profile_main(["run_profile.py", "o/r", str(tmp_path)])

    captured = capsys.readouterr()
    payload = assert_json_stdout(captured.out)
    assert rc == 0
    assert payload["verification"] == "skipped"
    assert captured.err == ""


def test_detect_profile_json_stdout_is_json_only(tmp_path, capsys):
    (tmp_path / "tests").mkdir()

    rc = detect_profile.main(["detect_profile.py", str(tmp_path)])

    captured = capsys.readouterr()
    payload = assert_json_stdout(captured.out)
    assert rc == 0
    assert payload["stack"] == "python"
    assert captured.err == ""


def test_judge_preferences_json_is_serializable_without_stdout(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

    prefs = judge.load_preferences()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "\033[" not in captured.err
    json.loads(json.dumps(prefs))


def test_request_rereview_json_stdout_is_json_only(monkeypatch, capsys):
    monkeypatch.setattr(
        request_rereview,
        "post_rereview",
        lambda repo, pr, phrase, **kwargs: {
            "created_at": "2026-06-09T07:17:16Z",
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        },
    )

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--json",
    ])

    captured = capsys.readouterr()
    payload = assert_json_stdout(captured.out)
    assert rc == 0
    assert payload["phrase"] == request_rereview.DEFAULT_PHRASE
    assert captured.err == ""


def test_request_rereview_json_failure_has_stderr_and_no_stdout(monkeypatch, capsys):
    def fail_post(repo, pr, phrase, **kwargs):  # noqa: ARG001
        raise RuntimeError("gh api failed: permission denied")

    monkeypatch.setattr(request_rereview, "post_rereview", fail_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--json",
    ])

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "gh api failed: permission denied" in captured.err


def test_wait_chunk_json_stdout_is_machine_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("GGRL_NO_COLOR", raising=False)
    monkeypatch.setattr(
        fgt, "resolve_pr", lambda arg: PullRequest(owner="o", repo="r", number=5)
    )
    monkeypatch.setattr(
        fgt,
        "run_wait_chunk",
        lambda *a, **k: {
            "status": "settling",
            "author": "gemini-code-assist",
            "elapsed_seconds": 120,
            "checks": 3,
            "next_wait_seconds": 30,
            "quiet_period_remaining_seconds": 30,
            "submitted_at": "2026-06-11T12:04:27Z",
            "pull_request": None,
        },
    )
    monkeypatch.setattr(
        sys, "argv",
        ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
         "--wait", "--after", "2026-06-11T12:00:00Z",
         "--wait-chunk-seconds", "60", "--format", "json"],
    )
    assert fgt.main() == 0
    payload = assert_json_stdout(capsys.readouterr().out)
    assert payload["wait"]["status"] == "settling"
    assert payload["wait"]["submitted_at"] == "2026-06-11T12:04:27Z"
    assert payload["wait"]["quiet_period_remaining_seconds"] == 30


def test_wait_timed_out_json_stdout_is_machine_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        fgt, "resolve_pr", lambda arg: PullRequest(owner="o", repo="r", number=5)
    )
    monkeypatch.setattr(
        fgt,
        "run_wait_chunk",
        lambda *a, **k: {
            "status": "timed_out",
            "author": "gemini-code-assist",
            "elapsed_seconds": 905,
            "checks": 11,
            "pull_request": None,
        },
    )
    monkeypatch.setattr(
        sys, "argv",
        ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
         "--wait", "--after", "2026-06-11T12:00:00Z",
         "--wait-chunk-seconds", "60", "--format", "json"],
    )
    assert fgt.main() == 0
    payload = assert_json_stdout(capsys.readouterr().out)
    assert payload["wait"]["status"] == "timed_out"


@pytest.fixture(autouse=True)
def _cap_check_is_inert(monkeypatch):
    """See tests/test_request_rereview.py: the cap check needs `gh`, and these
    tests are about stdout discipline and cycle flow, not the cap."""
    stub_cap_check_known_under_cap(monkeypatch)
