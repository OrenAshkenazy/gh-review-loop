"""Reuse expensive judge calls without caching live GitHub feedback."""
import copy
import sys
from types import SimpleNamespace

import pytest

import fetch_gemini_threads as fgt
import judge


@pytest.fixture
def evaluation():
    pr = fgt.PullRequest("owner", "repo", 1)
    fgt.update_run_tracking(pr, [("t1", "fix.py")])
    client = SimpleNamespace(model="test-model", base_url="https://judge.invalid/v1", temperature=0.0, calls=[])
    result = judge.JudgeResult("valid_actionable", 0.9, "none", "fix", "test verdict")

    def evaluate(finding):
        client.calls.append(copy.deepcopy(finding))
        return result

    client.judge = evaluate
    client.is_ready = lambda: (True, None)
    thread = {"id": "t1", "path": "fix.py", "line": 1, "comments": [{"body": "Check the return value", "diffHunk": "@@ -1 +1 @@"}]}
    return pr, client, {"headRefOid": "a" * 40}, [thread]


def evaluate(data):
    pr, client, pull_request, threads = data
    status = {"judged_count": 0, "errors": 0}
    results = fgt.judge_findings(pr, "gemini-code-assist", pull_request, threads, client, status)
    return results, status


def test_identical_judge_input_reuses_successful_result(evaluation):
    first, status = evaluate(evaluation)
    second, reused = evaluate(evaluation)
    assert first == second and len(evaluation[1].calls) == 1
    assert status["judged_count"] == 1 and reused["judged_count"] == 0 and reused["reused_count"] == 1


@pytest.mark.parametrize("change", ["head", "model", "endpoint", "temperature", "prompt", "body", "path", "line", "diff"])
def test_changed_judge_context_requires_new_evaluation(evaluation, monkeypatch, change):
    evaluate(evaluation)
    _, client, pull_request, threads = evaluation
    if change == "head":
        pull_request["headRefOid"] = "b" * 40
    elif change in {"model", "endpoint", "temperature"}:
        setattr(client, {"endpoint": "base_url"}.get(change, change), 0.5 if change == "temperature" else "changed")
    elif change == "prompt":
        monkeypatch.setattr(judge, "SYSTEM_PROMPT", judge.SYSTEM_PROMPT + "\nChanged instruction.")
    elif change in {"body", "diff"}:
        threads[0]["comments"][0]["diffHunk" if change == "diff" else "body"] = "changed"
    else:
        threads[0][change] = 2 if change == "line" else "other.py"
    _, status = evaluate(evaluation)
    assert len(client.calls) == 2 and status["reused_count"] == 0


@pytest.mark.parametrize("failure", ["skipped", "error"])
def test_unsuccessful_judge_result_is_retried(evaluation, failure):
    client = evaluation[1]
    original = client.judge

    def fail(finding):
        client.calls.append(finding)
        if failure == "error":
            raise judge.JudgeError("temporary error")
        return judge.skipped_result("temporarily unavailable")

    client.judge = fail
    evaluate(evaluation)
    client.judge = original
    _, status = evaluate(evaluation)
    assert len(client.calls) == 2 and status["judged_count"] == 1 and status["reused_count"] == 0


@pytest.mark.parametrize("transition", ["clear", "advance"])
def test_judge_does_not_cache_into_a_changed_run(evaluation, transition):
    pr, client, _, _ = evaluation
    original = client.judge

    def change_run(finding):
        if transition == "clear":
            fgt.clear_run_tracking(pr)
        else:
            fgt.update_run_tracking(pr, [])
        return original(finding)

    client.judge = change_run
    evaluate(evaluation)
    assert "judge_cache" not in fgt.read_run_tracking(pr)


def test_missing_head_does_not_reuse_judge_result(evaluation):
    evaluation[2].clear()
    evaluate(evaluation)
    evaluate(evaluation)
    assert len(evaluation[1].calls) == 2


def test_receipts_fetch_fresh_feedback_and_reuse_only_unchanged_judgments(evaluation, monkeypatch, capsys):
    pr, client, pull_request, threads = evaluation
    raw_thread = {**threads[0], "comments": {"nodes": [{**threads[0]["comments"][0], "author": {"login": "gemini-code-assist"}}]}}
    pull_request["reviewThreads"] = {"nodes": [raw_thread]}
    reads = []
    monkeypatch.setattr(fgt, "fetch_threads", lambda pr: reads.append(True) or copy.deepcopy(pull_request))
    monkeypatch.setattr(fgt, "repo_root_for_pr", lambda *args: None)
    monkeypatch.setattr(judge, "JudgeClient", lambda **kwargs: client)

    def invoke(*flags):
        monkeypatch.setattr(sys, "argv", ["fetch_gemini_threads.py", "--pr", "owner/repo#1", "--reviewer", "gemini-code-assist", "--judge-mode", "once", "--no-agent-filter", "--dry-run", "--no-color", *flags])
        assert fgt.main() == 0
        capsys.readouterr()

    invoke()
    sequence = fgt.read_run_tracking(pr)["update_seq"]
    invoke("--cycle-summary")
    assert len(reads) == 2 and len(client.calls) == 1
    assert fgt.read_run_tracking(pr)["update_seq"] == sequence + 1
    assert fgt.read_run_tracking(pr)["last_summary_seq"] == sequence + 1
    raw_thread["comments"]["nodes"][0]["body"] = "New feedback on the same commit"
    invoke("--cycle-summary")
    assert len(reads) == 3 and len(client.calls) == 2
    assert client.calls[-1]["body"] == "New feedback on the same commit"
    invoke("--record-run")
    assert len(reads) == 4 and len(client.calls) == 2
    assert fgt.read_run_tracking(pr) == {}


def test_malformed_cached_verdict_is_evaluated_again(evaluation):
    evaluate(evaluation)
    pr = evaluation[0]
    state = fgt.load_sticky_state()
    state["owner/repo#1"]["run"]["judge_cache"]["t1"]["result"].pop("verdict")
    fgt.save_sticky_state(state)
    evaluate(evaluation)
    assert len(evaluation[1].calls) == 2
    assert fgt.read_run_tracking(pr)["judge_cache"]["t1"]["result"]["verdict"] == "valid_actionable"
