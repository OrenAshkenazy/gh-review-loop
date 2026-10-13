"""Exercise cooperative locks and state transactions with real processes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import loop_lock
import loop_state
import request_rereview as rr

SCRIPTS = Path(rr.__file__).parent


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))


def child(tmp_path, code, *args):
    prelude = f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
    env = {**os.environ, "GGRL_STATE_DIR": str(tmp_path), "PYTHONUTF8": "1"}
    return subprocess.Popen([sys.executable, "-c", prelude + code, *map(str, args)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")


def wait_file(path):
    deadline = time.monotonic() + 10
    while not path.exists():
        assert time.monotonic() < deadline, f"child did not create {path}"
        time.sleep(0.01)


def finish(process):
    try:
        out, err = process.communicate(timeout=20)
        assert process.returncode == 0, (out, err)
        return out
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_process_lock_is_released_after_owner_dies(tmp_path):
    ready = tmp_path / "ready"
    process = child(tmp_path, """
import loop_lock, time
from pathlib import Path
with loop_lock.pr_lock('OWNER/Repo', 1):
    Path(sys.argv[1]).touch()
    time.sleep(20)
""", ready)
    try:
        wait_file(ready)
        with pytest.raises(TimeoutError):
            with loop_lock.pr_lock("owner/repo", 1):
                pytest.fail("must not enter another process's lock")
    finally:
        process.kill()
        process.communicate()
    with loop_lock.pr_lock("owner/repo", 1):
        with loop_lock.pr_lock("OWNER/REPO", 1):
            assert list((tmp_path / "locks").glob("*.lock"))


def test_concurrent_state_writers_preserve_both_prs(tmp_path):
    code = """
import request_rereview as rr, time
real_load = rr.load_state
def delayed_load():
    state = real_load()
    time.sleep(.02)
    return state
rr.load_state = delayed_load
for _ in range(8):
    rr.record_rereview_posted('owner/repo', int(sys.argv[1]), reviewer='copilot-pull-request-reviewer', requested_at='2026-10-12T20:00:00Z')
"""
    processes = [child(tmp_path, code, pr) for pr in (1, 2)]
    for process in processes:
        finish(process)
    state = loop_state.load_sticky_state(strict=True)
    for pr in (1, 2):
        assert state[f"owner/repo#{pr}"]["run"]["rereviews_posted"] == 8
        assert len(state[f"owner/repo#{pr}"]["review_requests"]) == 8


@pytest.mark.parametrize("vendor", ["coderabbitai", "copilot-pull-request-reviewer"])
def test_two_processes_cannot_consume_one_cap_slot_twice(tmp_path, vendor):
    code = """
import request_rereview as rr, time, json
from pathlib import Path
vendor, start, ready, log = sys.argv[1:]
start, ready, log = map(Path, (start, ready, log))
rr.resolve_reviewer_login = lambda **kw: vendor
rr.resolve_review_trigger = lambda **kw: ('@coderabbitai', vendor)
rr.gh_login = lambda: 'agent'
def count(*a, **kw):
    return len(log.read_text().splitlines()) if log.exists() else 0
rr.count_agent_pings = count
rr.count_agent_review_requests = count
rr._snapshot_request_event_ids = lambda *a: []
rr._snapshot_comment_ids = lambda *a: []
rr.request_state = lambda *a: {'head': 'abc', 'state': 'ABSENT', 'pending_since': None, 'first_completed_review_at': '2026-10-10T00:00:00Z'}
def post(*a, **kw):
    time.sleep(.2)
    with log.open('a') as stream:
        stream.write('posted\\n')
    return {'created_at': '2026-10-12T20:00:00Z', 'posted': True}
rr.post_rereview = post
rr.request_review = post
ready.touch()
deadline = time.monotonic() + 10
while not start.exists():
    assert time.monotonic() < deadline
    time.sleep(.01)
raise SystemExit(rr.main(['--repo', 'owner/repo', '--pr', '1', '--max-rereview-requests', '1', '--json']))
"""
    start, log = tmp_path / "start", tmp_path / "requests"
    ready = [tmp_path / f"ready-{i}" for i in range(2)]
    processes = [child(tmp_path, code, vendor, start, marker, log) for marker in ready]
    for marker in ready:
        wait_file(marker)
    start.touch()
    results = [json.loads(finish(process)) for process in processes]
    assert log.read_text().splitlines() == ["posted"]
    assert sum(result["posted"] is True for result in results) == 1
    assert next(result for result in results if result["posted"] is False)["status"] in {"busy", "capped"}
    assert loop_state.load_sticky_state()["owner/repo#1"]["run"]["rereviews_posted"] == 1


@pytest.mark.parametrize("contents", ["broken json", "[]", "null"])
def test_mutation_refuses_corrupt_state_without_erasing_it(tmp_path, contents):
    path = tmp_path / "state.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(RuntimeError):
        rr.record_rereview_posted("owner/repo", 1)
    assert path.read_text(encoding="utf-8") == contents


def test_atomic_replace_failure_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"original": true}', encoding="utf-8")

    def fail(*args):
        raise OSError("replacement failed")

    monkeypatch.setattr(loop_state.os, "replace", fail)
    with pytest.raises(OSError):
        loop_state.save_sticky_state({"new": True})
    assert path.read_text(encoding="utf-8") == '{"original": true}'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "state.json.lock"]


def test_run_teardown_cannot_remove_another_runs_sentinel(tmp_path):
    import fetch_gemini_threads as fetch

    fetch.update_run_tracking(fetch.PullRequest("owner", "repo", 1), [])
    ready = tmp_path / "clearing"
    clear = child(tmp_path, """
import fetch_gemini_threads as f, time
from pathlib import Path
real_active = f.any_active_run
def delayed_active():
    active = real_active()
    Path(sys.argv[1]).touch()
    time.sleep(.3)
    return active
f.any_active_run = delayed_active
f.clear_run_tracking(f.PullRequest('owner', 'repo', 1))
""", ready)
    wait_file(ready)
    create = child(tmp_path, """
import fetch_gemini_threads as f
f.update_run_tracking(f.PullRequest('owner', 'repo', 2), [])
""")
    finish(clear)
    finish(create)
    assert loop_state.any_active_run()
    assert loop_state.sentinel_path().exists()


@pytest.mark.parametrize("vendor", ["coderabbitai", "copilot-pull-request-reviewer"])
def test_posted_request_with_stamp_failure_remains_known_and_blocks_retry(tmp_path, monkeypatch, capsys, vendor):
    monkeypatch.setattr(rr, "resolve_reviewer_login", lambda **kw: vendor)
    monkeypatch.setattr(rr, "resolve_review_trigger", lambda **kw: ("@coderabbitai", vendor))
    monkeypatch.setattr(rr, "request_state", lambda *a: {"head": "abc", "state": "ABSENT", "first_completed_review_at": "2026-10-10T00:00:00Z"})
    monkeypatch.setattr(rr, "count_agent_pings", lambda *a: 0)
    monkeypatch.setattr(rr, "count_agent_review_requests", lambda *a: 0)
    monkeypatch.setattr(rr, "gh_login", lambda: "agent")
    monkeypatch.setattr(rr, "_snapshot_request_event_ids", lambda *a: [])
    monkeypatch.setattr(rr, "_snapshot_comment_ids", lambda *a: [])
    calls = []

    def post(*args, **kwargs):
        calls.append(True)
        return {"posted": True, "created_at": "2026-10-12T20:00:00Z"}

    def fail_stamp(*args, **kwargs):
        raise TimeoutError("state transaction unavailable after POST")

    monkeypatch.setattr(rr, "request_review", post)
    monkeypatch.setattr(rr, "post_rereview", post)
    monkeypatch.setattr(rr, "record_rereview_posted", fail_stamp)
    args = ["--repo", "owner/repo", "--pr", "1", "--json"]
    assert rr.main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["posted"] is True
    assert payload["status"] == "state_record_failed"
    assert loop_state.load_sticky_state()["owner/repo#1"]["review_request_in_flight"]
    assert rr.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "request_in_flight"
    assert calls == [True]


def test_local_native_post_consumes_cap_before_timeline_visibility(tmp_path):
    import review_vendors

    rr.record_rereview_posted("owner/repo", 1, reviewer="copilot-pull-request-reviewer", requested_at="2026-10-12T20:00:00Z")
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "[]", "")

    assert rr.count_agent_review_requests("owner/repo", 1, review_vendors.COPILOT, "agent", "2026-10-10T00:00:00Z", runner=runner) == 1


def test_receipt_merges_latest_state_after_network_io(tmp_path, monkeypatch):
    import fetch_gemini_threads as fetch

    pr = fetch.PullRequest("owner", "repo", 1)
    loop_state.save_sticky_state({"owner/repo#1": {"comment_id": 42}})

    def network(*args, **kwargs):
        rr.record_rereview_posted("owner/repo", 1)
        rr.record_rereview_posted("owner/repo", 2)
        return {}

    monkeypatch.setattr(fetch, "run_gh", network)
    assert fetch.post_or_update_sticky_receipt(pr, "body") == 42
    state = loop_state.load_sticky_state()
    assert state["owner/repo#1"]["run"]["rereviews_posted"] == 1
    assert state["owner/repo#2"]["run"]["rereviews_posted"] == 1


@pytest.mark.parametrize("trigger", ["@coderabbitai", "@custom-reviewer"])
def test_comment_cap_unions_historical_remote_and_unmatched_local_posts(tmp_path, trigger):
    rr.record_rereview_posted("owner/repo", 1, reviewer=trigger, requested_at="2026-10-12T20:00:00Z", comment_id=3)
    comments = [{"id": i, "user": {"login": "agent"}, "body": f"{trigger} review"} for i in (1, 2)]

    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps(comments), "")

    assert rr.count_agent_pings("owner/repo", 1, trigger, "agent", runner=runner) == 3
    comments.append({"id": 3, "user": {"login": "agent"}, "body": f"{trigger} review"})
    assert rr.count_agent_pings("owner/repo", 1, trigger, "agent", runner=runner) == 3


@pytest.mark.parametrize("posted_at", ["2026-10-12T11:59:59Z", "2026-10-12T12:00:00Z"])
@pytest.mark.parametrize("visible", [False, True])
def test_native_local_clock_skew_or_same_second_does_not_lose_slot(tmp_path, posted_at, visible):
    import review_vendors

    rr.record_rereview_posted("owner/repo", 1, reviewer="copilot", requested_at=posted_at)
    events = [{"event": "review_requested", "actor": {"login": "agent"}, "requested_reviewer": {"login": "copilot"}, "created_at": "2026-10-12T12:00:02Z"}] if visible else []

    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps(events), "")

    assert rr.count_agent_review_requests("owner/repo", 1, review_vendors.COPILOT, "agent", "2026-10-12T12:00:00Z", runner=runner) == 1


def test_explicit_initial_request_stays_free_after_first_review(tmp_path):
    import review_vendors

    rr.record_rereview_posted("owner/repo", 1, reviewer="copilot", requested_at="2026-10-12T12:00:00Z", initial_review=True)

    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "[]", "")

    assert rr.count_agent_review_requests("owner/repo", 1, review_vendors.COPILOT, "agent", "2026-10-12T12:00:00Z", runner=runner) == 0


def test_fetch_and_request_readers_agree_on_initial_and_in_flight_receipts(tmp_path):
    import fetch_gemini_threads as fetch
    import review_vendors

    pr = fetch.PullRequest("owner", "repo", 1)
    rr.record_rereview_posted("owner/repo", 1, reviewer="copilot", requested_at="2026-10-12T11:00:00Z", initial_review=True)
    rr.record_rereview_posted("owner/repo", 1, reviewer="copilot", requested_at="2026-10-12T12:00:00Z")
    assert fetch.loop_request_times(pr, "copilot") == rr.loop_request_times("owner/repo", 1, review_vendors.COPILOT) == ["2026-10-12T12:00:00Z"]
    rr._mark_request_in_flight("owner/repo", 1, {"at": "2026-10-12T13:00:00Z"})
    assert fetch.loop_request_times(pr, "copilot") is None
    assert rr.loop_request_times("owner/repo", 1, review_vendors.COPILOT) is None
