"""Publishing safeguards exercised with real local Git repositories."""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

import publish_changes as pc
import request_rereview as rr
from run_profile import command_argv


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    remote = tmp_path / "remote.git"
    root.mkdir()
    pc.git(root, "init", "--initial-branch=main")
    pc.git(root, "config", "user.name", "Test User")
    pc.git(root, "config", "user.email", "test@example.invalid")
    pc.git(root, "config", "core.autocrlf", "false")
    (root / "fix.txt").write_text("before\n")
    pc.git(root, "add", "fix.txt")
    pc.git(root, "commit", "-m", "initial")
    pc.git(root, "checkout", "-b", "fix")
    pc.git(root, "init", "--bare", str(remote))
    pc.git(root, "remote", "add", "origin", str(remote))
    pc.git(root, "push", "origin", "fix")
    head = pc.git(root, "rev-parse", "HEAD").strip()
    (root / "fix.txt").write_text("after\n")
    info = {"state": "open", "base": {"ref": "main"}, "head": {"sha": head, "ref": "fix", "repo": {"html_url": "https://github.com/owner/fork", "default_branch": "main"}}}
    monkeypatch.setattr(pc, "pr_info", lambda *args: copy.deepcopy(info))
    original_git = pc.git

    def local_git(root, *args):
        if args[:4] == ("remote", "get-url", "--push", "--all"):
            return "https://github.com/owner/fork.git\n"
        if args[:3] == ("ls-remote", "--refs", "https://github.com/owner/fork.git"):
            return original_git(root, *args[:2], str(remote), *args[3:])
        return original_git(root, *args)

    monkeypatch.setattr(pc, "git", local_git)
    profile = {"source": "confirmed", "checks": [{"name": "tests", "required": True, "command": [sys.executable, "-c", "assert open('fix.txt').read() == 'after\\n'"]}]}
    monkeypatch.setattr(pc.judge, "get_profile", lambda repo: copy.deepcopy(profile))
    state = {"owner/repo#1": {"run": {"update_seq": 1, "last_summary_seq": 0}}}
    monkeypatch.setattr(rr, "load_state", lambda: state)
    def emit_receipt(*args, **kwargs):
        state["owner/repo#1"]["run"]["last_summary_seq"] = state["owner/repo#1"]["run"]["update_seq"]
        return {"output": "[loop] receipt", "warnings": ""}
    monkeypatch.setattr(pc.loop_lifecycle, "emit_receipt", emit_receipt)
    monkeypatch.setattr(rr, "read_persisted_reviewer", lambda *args: {"login": "coderabbitai", "review_trigger": "@coderabbitai"})
    monkeypatch.setattr(rr, "gh_login", lambda: "agent")
    monkeypatch.setattr(rr, "count_agent_pings", lambda *args: 0)
    monkeypatch.setattr(rr, "effective_cap", lambda *args: 3)
    monkeypatch.setattr(rr, "request_rereview", lambda *args: {"posted": True, "created_at": "2026-10-12T20:00:00Z"})
    return root, remote, head, info, profile


def publish(checkout, **kwargs):
    return pc.publish("owner/repo", 1, checkout[0], ["fix.txt"], "fix review", **kwargs)


def assert_not_pushed(checkout):
    root, remote, head, _, _ = checkout
    assert pc.git(root, "--git-dir", str(remote), "rev-parse", "refs/heads/fix").strip() == head


def test_publishes_only_verified_changes(checkout):
    result = publish(checkout)
    assert result["published"] is True
    root, remote, head, _, _ = checkout
    assert result["head"] != head
    assert pc.git(root, "--git-dir", str(remote), "rev-parse", "refs/heads/fix").strip() == result["head"]
    assert pc.git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("fetch_has_branch", [False, True])
def test_separate_fetch_and_push_urls_validate_the_push_destination(checkout, tmp_path, fetch_has_branch):
    root, push_remote, head, _, _ = checkout
    fetch_remote = tmp_path / "fetch.git"
    pc.git(root, "init", "--bare", str(fetch_remote))
    if fetch_has_branch:
        pc.git(root, "push", str(fetch_remote), "HEAD:refs/heads/fix")
    pc.git(root, "remote", "set-url", "origin", str(fetch_remote))
    pc.git(root, "remote", "set-url", "--push", "origin", str(push_remote))
    result = publish(checkout)
    assert result["published"] is True and result["head"] != head
    assert pc.git(root, "--git-dir", str(push_remote), "rev-parse", "refs/heads/fix").strip() == result["head"]
    fetch_tip = pc.git(root, "ls-remote", "--refs", str(fetch_remote), "refs/heads/fix").strip().split()
    assert fetch_tip == ([head, "refs/heads/fix"] if fetch_has_branch else [])


def test_dry_run_verifies_without_staging_committing_or_pushing(checkout):
    result = publish(checkout, dry_run=True)
    assert result["verification"]["verification"] == "passed"
    assert result["published"] is False
    assert pc.git(checkout[0], "rev-parse", "HEAD").strip() == checkout[2]
    assert pc.git(checkout[0], "diff", "--cached", "--name-only") == ""
    assert_not_pushed(checkout)


@pytest.mark.parametrize("dry_run", [False, True])
def test_non_utf8_text_is_verified_and_preserved(checkout, dry_run):
    root, remote, head, _, profile = checkout
    content = b"after caf\xe9\n"
    (root / "fix.txt").write_bytes(content)
    profile["checks"][0]["command"] = [sys.executable, "-c", f"from pathlib import Path; assert Path('fix.txt').read_bytes() == {content!r}"]
    diff, _ = pc.snapshot(root, ["fix.txt"])
    assert b"+after caf\xe9\n" in diff.encode("utf-8", "surrogateescape")

    result = publish(checkout, dry_run=dry_run)

    assert result["verification"]["verification"] == "passed"
    assert result["published"] is not dry_run
    if dry_run:
        assert pc.git(root, "rev-parse", "HEAD").strip() == head
        assert pc.git(root, "diff", "--cached", "--name-only") == ""
        assert_not_pushed(checkout)
    else:
        assert pc.git(root, "--git-dir", str(remote), "show", f"{result['head']}:fix.txt").encode("utf-8", "surrogateescape") == content
        assert pc.git(root, "status", "--porcelain") == ""


def test_publish_holds_pr_lock_through_request_and_forwards_head_cap(checkout, monkeypatch):
    import concurrent.futures
    import loop_lock

    calls = []

    def contender():
        with loop_lock.pr_lock("OWNER/REPO", 1):
            pytest.fail("the publisher must still own the PR lock")

    def request(repo, pr, head, cap):
        assert pc.git(checkout[0], "--git-dir", str(checkout[1]), "rev-parse", "refs/heads/fix").strip() == head
        with concurrent.futures.ThreadPoolExecutor() as executor:
            with pytest.raises(TimeoutError):
                executor.submit(contender).result()
        # Nested request calls in the publishing thread must be reentrant.
        with loop_lock.pr_lock(repo, pr):
            calls.append((repo, pr, head, cap))
        return {"posted": True, "created_at": "2026-10-12T20:00:00Z"}

    monkeypatch.setattr(rr, "request_rereview", request)
    result = publish(checkout, max_rereview_requests=6)
    assert calls == [("owner/repo", 1, result["head"], 6)]
    assert result["review_request"]["posted"] is True


def test_dry_run_never_requests_review(checkout, monkeypatch):
    monkeypatch.setattr(rr, "request_rereview", lambda *args: pytest.fail("dry run cannot request review"))
    assert "review_request" not in publish(checkout, dry_run=True)


@pytest.mark.parametrize("failure", [False, True])
def test_request_failure_preserves_confirmed_publication(checkout, monkeypatch, failure):
    def request(*args):
        if failure:
            raise RuntimeError("transport failed after push")
        return {"posted": False, "status": "review_pending"}

    monkeypatch.setattr(rr, "request_rereview", request)
    result = publish(checkout)
    assert result["published"] is True
    assert result["review_request"]["status"] == ("request_failed" if failure else "review_pending")
    assert pc.git(checkout[0], "--git-dir", str(checkout[1]), "rev-parse", "refs/heads/fix").strip() == result["head"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX named pipes")
def test_existing_fifo_is_rejected_before_reading(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(pc.PublishError, match="not a regular file"):
        pc.approved_paths(tmp_path, ["pipe"])


def test_missing_path_still_allows_verified_deletion(checkout):
    root = checkout[0]
    (root / "fix.txt").unlink()
    checkout[4]["checks"][0]["command"] = [sys.executable, "-c", "from pathlib import Path; assert not Path('fix.txt').exists()"]
    result = publish(checkout)

    assert result["published"] is True
    assert pc.git(root, "ls-tree", "--name-only", result["head"]).strip() == ""


@pytest.mark.parametrize("profile", [None, {}, {"source": "skipped"}, {"source": "confirmed", "checks": []}, {"source": "confirmed", "checks": [{"required": False, "command": ["python", "-V"]}]}, {"source": "confirmed", "checks": [{"required": "true", "command": ["python", "-V"]}]}])
def test_incomplete_or_optional_only_profile_blocks(checkout, monkeypatch, profile):
    monkeypatch.setattr(pc.judge, "get_profile", lambda repo: profile)
    with pytest.raises(pc.PublishError, match="profile"):
        publish(checkout)
    assert_not_pushed(checkout)


def test_failed_required_check_blocks(checkout):
    checkout[4]["checks"][0]["command"] = [sys.executable, "-c", "raise SystemExit(1)"]
    with pytest.raises(pc.PublishError, match="verification failed"):
        publish(checkout)
    assert_not_pushed(checkout)


@pytest.mark.parametrize("override", ["", None])
def test_empty_check_directory_cannot_hide_profile_outside_checkout(checkout, monkeypatch, override):
    checkout[4]["working_directory"] = ".."
    checkout[4]["checks"][0]["working_directory"] = override
    monkeypatch.setattr(pc, "run_profile", lambda *args: pytest.fail("an outside verification directory must not run"))
    with pytest.raises(pc.PublishError, match="working directories"):
        publish(checkout)
    assert_not_pushed(checkout)


@pytest.mark.parametrize("staged", [False, True])
def test_unrelated_changes_are_preserved_and_block_publish(checkout, staged):
    root = checkout[0]
    (root / "unrelated.txt").write_text("user work")
    if staged:
        pc.git(root, "add", "unrelated.txt")
    with pytest.raises(pc.PublishError, match="index|approved paths"):
        publish(checkout)
    assert (root / "unrelated.txt").read_text() == "user work"
    assert_not_pushed(checkout)


def test_verification_rewriting_files_blocks(checkout):
    checkout[4]["checks"][0]["command"] = [sys.executable, "-c", "open('fix.txt', 'w').write('not verified')"]
    with pytest.raises(pc.PublishError, match="changed during verification"):
        publish(checkout)
    assert_not_pushed(checkout)


@pytest.mark.parametrize("change", ["head", "branch", "base", "closed", "remote"])
def test_wrong_checkout_or_remote_blocks(checkout, monkeypatch, change):
    info = checkout[3]
    if change == "head":
        info["head"]["sha"] = "a" * 40
    elif change == "branch":
        info["head"]["ref"] = "other"
    elif change == "base":
        info["base"]["ref"] = "fix"
    elif change == "closed":
        monkeypatch.setattr(pc, "pr_info", lambda *args: (_ for _ in ()).throw(pc.PublishError("PR must be open")))
    else:
        info["head"]["repo"]["html_url"] = "https://github.com/other/repo"
    with pytest.raises(pc.PublishError):
        publish(checkout)
    assert_not_pushed(checkout)


def test_pr_head_change_during_verification_blocks(checkout, monkeypatch):
    original = pc.run_profile

    def verify(*args):
        result = original(*args)
        checkout[3]["head"]["sha"] = "a" * 40
        return result

    monkeypatch.setattr(pc, "run_profile", verify)
    with pytest.raises(pc.PublishError, match="PR head"):
        publish(checkout)
    assert_not_pushed(checkout)


@pytest.mark.parametrize("state", [{}, {"owner/repo#2": {"run": {"update_seq": 1, "last_summary_seq": 1}}}])
def test_missing_or_other_pr_run_blocks(checkout, monkeypatch, state):
    monkeypatch.setattr(rr, "load_state", lambda: state)
    with pytest.raises(pc.PublishError, match="fetch this PR"):
        publish(checkout)
    assert_not_pushed(checkout)


@pytest.mark.parametrize("dry_run", [False, True])
def test_profile_runs_exactly_once_per_publication(checkout, tmp_path, dry_run):
    counter = tmp_path / "verification-count"
    checkout[4]["checks"][0]["command"] = [sys.executable, "-c", f"from pathlib import Path; p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]
    result = publish(checkout, dry_run=dry_run)
    assert counter.read_text() == "x"
    assert result["verification"]["verification"] == "passed"


def test_publisher_emits_verified_summary_and_forwards_metadata(checkout, monkeypatch):
    original = pc.loop_lifecycle.emit_receipt
    calls = []
    def receipt(repo, pr, root, **kwargs):
        assert pc.git(root, "rev-parse", "HEAD").strip() == checkout[2]
        assert pc.git(root, "diff", "--cached", "--name-only") == ""
        calls.append((repo, pr, kwargs))
        return original(repo, pr, root, **kwargs)
    monkeypatch.setattr(pc.loop_lifecycle, "emit_receipt", receipt)
    result = publish(checkout, max_rereview_requests=6, receipt_options=["--fixed-count", "2", "--semantic-risk", "changed return shape"])
    assert len(calls) == 1
    assert calls[0][:2] == ("owner/repo", 1)
    assert calls[0][2]["verification"] == result["verification"]
    assert calls[0][2]["options"] == ["--fixed-count", "2", "--semantic-risk", "changed return shape", "--max-rereview-requests", "6"]
    assert result["cycle_summary"]["output"] == "[loop] receipt"


def test_dry_run_does_not_emit_or_post_a_receipt(checkout, monkeypatch):
    monkeypatch.setattr(pc.loop_lifecycle, "emit_receipt", lambda *a, **kw: pytest.fail("dry run cannot emit a receipt"))
    assert "cycle_summary" not in publish(checkout, dry_run=True)


@pytest.mark.parametrize("failure", ["error", "unstamped", "files", "profile", "head"])
def test_summary_failure_or_changes_block_staging_and_push(checkout, monkeypatch, failure):
    original = pc.loop_lifecycle.emit_receipt
    def receipt(*args, **kwargs):
        if failure == "error":
            raise RuntimeError("receipt timed out")
        if failure != "unstamped":
            original(*args, **kwargs)
        if failure == "files":
            (checkout[0] / "fix.txt").write_text("changed after verification")
        if failure == "profile":
            checkout[4]["checks"][0]["command"] = ["different"]
        if failure == "head":
            checkout[3]["head"]["sha"] = "b" * 40
        return {"output": "[loop] full fallback receipt", "warnings": ""}
    monkeypatch.setattr(pc.loop_lifecycle, "emit_receipt", receipt)
    with pytest.raises((pc.PublishError, RuntimeError)):
        publish(checkout)
    assert pc.git(checkout[0], "rev-parse", "HEAD").strip() == checkout[2]
    assert pc.git(checkout[0], "diff", "--cached", "--name-only") == ""
    assert_not_pushed(checkout)


@pytest.mark.parametrize("count", [None, 3])
def test_unknown_or_exhausted_cap_blocks(checkout, monkeypatch, count):
    monkeypatch.setattr(rr, "count_agent_pings", lambda *args: count)
    with pytest.raises(pc.PublishError, match="cap"):
        publish(checkout)
    assert_not_pushed(checkout)


@pytest.mark.parametrize("count, cap, blocked", [(1, 1, True), (3, 4, False)])
def test_cli_cap_override_applies_to_every_publication_gate(checkout, monkeypatch, capsys, count, cap, blocked):
    seen_caps = []

    def effective_cap(override):
        seen_caps.append(override)
        return override if override is not None else 3

    monkeypatch.setattr(rr, "effective_cap", effective_cap)
    monkeypatch.setattr(rr, "count_agent_pings", lambda *args: count)
    status = pc.main(["--repo", "owner/repo", "--pr", "1", "--repo-root", str(checkout[0]), "--paths", "fix.txt", "--message", "fix review", "--max-rereview-requests", str(cap)])
    result = json.loads(capsys.readouterr().out)
    if blocked:
        assert status == 1
        assert result["published"] is False
        assert "cap reached" in result["error"]
        assert seen_caps == [cap]
        assert_not_pushed(checkout)
    else:
        assert status == 0
        assert result["published"] is True
        assert seen_caps == [cap, cap, cap, cap]
        assert pc.git(checkout[0], "--git-dir", str(checkout[1]), "rev-parse", "refs/heads/fix").strip() == result["head"]


@pytest.mark.parametrize("count", [0, 2, 3, None])
def test_copilot_publication_uses_native_request_cap(checkout, monkeypatch, count):
    vendor = rr.review_vendors.COPILOT
    first_review = "2026-10-07T16:00:00Z"
    monkeypatch.setattr(rr, "read_persisted_reviewer", lambda *args: {"login": vendor.login})
    monkeypatch.setattr(rr, "request_state", lambda *args: {"first_completed_review_at": first_review})
    monkeypatch.setattr(rr, "count_agent_pings", lambda *args: pytest.fail("Copilot must not count mention comments"))
    counted = []

    def count_requests(repo, pr, selected, login, first_completed):
        counted.append((repo, pr, selected, login, first_completed))
        return count

    monkeypatch.setattr(rr, "count_agent_review_requests", count_requests)
    if count is None or count >= 3:
        with pytest.raises(pc.PublishError, match="cap"):
            publish(checkout, dry_run=True)
    else:
        assert publish(checkout, dry_run=True)["status"] == "verified"
    assert counted and all(call == ("owner/repo", 1, vendor, "agent", first_review) for call in counted)
    assert_not_pushed(checkout)


@pytest.mark.parametrize("review_state", [None, {"first_completed_review_at": None}])
def test_copilot_unknown_review_state_blocks_publication(checkout, monkeypatch, review_state):
    monkeypatch.setattr(rr, "read_persisted_reviewer", lambda *args: {"login": rr.review_vendors.COPILOT.login})
    monkeypatch.setattr(rr, "request_state", lambda *args: review_state)
    monkeypatch.setattr(rr, "count_agent_pings", lambda *args: pytest.fail("Copilot must not count mention comments"))
    with pytest.raises(pc.PublishError, match="cap"):
        publish(checkout, dry_run=True)
    assert_not_pushed(checkout)


def test_commit_hook_changes_are_not_pushed(checkout, monkeypatch):
    original_git = pc.git

    def hook_git(root, *args):
        if args[:1] == ("commit",):
            (root / "fix.txt").write_text("hook changed this")
            original_git(root, "add", "fix.txt")
        return original_git(root, *args)

    monkeypatch.setattr(pc, "git", hook_git)
    with pytest.raises(pc.PublishError, match="hooks changed"):
        publish(checkout)
    assert_not_pushed(checkout)


def test_changes_during_staging_are_not_committed_or_pushed(checkout, monkeypatch):
    original_git = pc.git

    def change_before_add(root, *args):
        if args[:2] == ("--literal-pathspecs", "add"):
            (root / "fix.txt").write_text("changed after verification")
        return original_git(root, *args)

    monkeypatch.setattr(pc, "git", change_before_add)
    with pytest.raises(pc.PublishError, match="staged bytes differ"):
        publish(checkout)
    assert pc.git(checkout[0], "rev-parse", "HEAD").strip() == checkout[2]
    assert_not_pushed(checkout)


def test_normal_git_eol_conversion_can_be_published(checkout):
    pc.git(checkout[0], "config", "core.autocrlf", "true")
    (checkout[0] / "fix.txt").write_bytes(b"after\r\n")
    assert publish(checkout)["published"] is True


@pytest.mark.parametrize("prior_mode,expected_mode", [("120000", "100644"), ("100755", "100755")])
def test_regular_file_uses_regular_mode_without_executable_bit_tracking(checkout, prior_mode, expected_mode):
    root, remote, _, info, profile = checkout
    pc.git(root, "config", "core.filemode", "false")
    pc.git(root, "config", "core.symlinks", "true")
    blob = pc.git(root, "rev-parse", "HEAD:fix.txt").strip()
    pc.git(root, "update-index", "--cacheinfo", prior_mode, blob, "fix.txt")
    pc.git(root, "commit", "-m", "record prior file mode")
    pc.git(root, "push", "origin", "fix")
    head = pc.git(root, "rev-parse", "HEAD").strip()
    info["head"]["sha"] = head

    result = publish((root, remote, head, info, profile))

    assert result["published"] is True
    assert pc.git(root, "--git-dir", str(remote), "ls-tree", result["head"], "fix.txt").split()[0] == expected_mode
    assert pc.git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("flag", ["review_requests_incomplete", "review_request_in_flight"])
def test_explicit_unknown_native_history_blocks_publication_with_empty_timeline(checkout, monkeypatch, flag):
    monkeypatch.setattr(rr, "load_state", lambda: {"owner/repo#1": {
        "run": {"update_seq": 1, "last_summary_seq": 1}, "review_requests": [], flag: True,
    }})
    monkeypatch.setattr(rr, "read_persisted_reviewer", lambda *args: {"login": rr.review_vendors.COPILOT.login})
    monkeypatch.setattr(rr, "request_state", lambda *args: {"first_completed_review_at": "2026-10-07T16:00:00Z"})
    monkeypatch.setattr(rr, "_timeline_events", lambda *args: [])
    with pytest.raises(pc.PublishError, match="incomplete|in-flight"):
        publish(checkout)
    assert_not_pushed(checkout)
    assert pc.git(checkout[0], "rev-parse", "HEAD").strip() == checkout[2]
    assert pc.git(checkout[0], "diff", "--cached", "--name-only") == ""


def test_unverified_staged_mode_is_not_committed(checkout, monkeypatch):
    original_git = pc.git

    def change_mode_after_add(root, *args):
        result = original_git(root, *args)
        if args[:2] == ("--literal-pathspecs", "add"):
            original_git(root, "update-index", "--chmod=+x", "fix.txt")
        return result

    monkeypatch.setattr(pc, "git", change_mode_after_add)
    with pytest.raises(pc.PublishError, match="staged bytes differ"):
        publish(checkout)
    assert pc.git(checkout[0], "rev-parse", "HEAD").strip() == checkout[2]
    assert_not_pushed(checkout)


@pytest.mark.parametrize("push_landed", [False, True])
def test_failed_push_preserves_local_commit(checkout, monkeypatch, push_landed):
    original_git = pc.git
    pushes = []

    def fail_push(root, *args):
        if args[:1] == ("push",):
            pushes.append(args)
            if push_landed:
                original_git(root, *args)
            raise pc.PublishError("push failed")
        return original_git(root, *args)

    monkeypatch.setattr(pc, "git", fail_push)
    monkeypatch.setattr(rr, "request_rereview", lambda *args: pytest.fail("an uncertain push cannot request review"))
    result = publish(checkout)
    assert result["status"] == "publication_unconfirmed" and result["published"] is None
    assert result["head"] == pc.git(checkout[0], "rev-parse", "HEAD").strip() != checkout[2]
    assert result["previous_head"] == checkout[2] and len(pushes) == 1
    tip = pc.git(checkout[0], "--git-dir", str(checkout[1]), "rev-parse", "refs/heads/fix").strip()
    assert tip == (result["head"] if push_landed else checkout[2])


@pytest.mark.parametrize("confirmation", ["error", "different_tip"])
def test_cli_preserves_uncertain_publication_after_successful_push(checkout, monkeypatch, capsys, confirmation):
    original_git = pc.git
    pushes = []

    def fail_confirmation(root, *args):
        if args[:1] == ("push",):
            result = original_git(root, *args)
            pushes.append(args)
            return result
        if pushes and args[:2] == ("ls-remote", "--refs"):
            if confirmation == "error":
                raise pc.PublishError("confirmation failed")
            return checkout[2] + "\trefs/heads/fix\n"
        return original_git(root, *args)

    monkeypatch.setattr(pc, "git", fail_confirmation)
    monkeypatch.setattr(rr, "request_rereview", lambda *args: pytest.fail("unconfirmed publication cannot request review"))
    status = pc.main(["--repo", "owner/repo", "--pr", "1", "--repo-root", str(checkout[0]), "--paths", "fix.txt", "--message", "fix review"])
    result = json.loads(capsys.readouterr().out)
    assert status == 1 and result["status"] == "publication_unconfirmed" and result["published"] is None
    assert result["head"] == pc.git(checkout[0], "--git-dir", str(checkout[1]), "rev-parse", "refs/heads/fix").strip() != checkout[2]
    assert result["previous_head"] == checkout[2] and result["verification"]["verification"] == "passed"
    assert result["error"] and len(pushes) == 1


@pytest.mark.parametrize("path", ["../outside", "/absolute", "a/../fix.txt", ".git/config", "C:/file", "dir\\file", "./fix.txt"])
def test_invalid_file_paths_are_refused(tmp_path, path):
    with pytest.raises(pc.PublishError):
        pc.approved_paths(tmp_path, [path])


def test_literal_argv_preserves_spaces_backslashes_and_quotes(tmp_path):
    command = [sys.executable, "-c", "import sys; assert sys.argv[1:] == ['C:\\\\path with spaces\\\\file', 'a\"b']", "C:\\path with spaces\\file", 'a"b']
    assert pc.run_profile({"checks": [{"command": command, "required": True}]}, tmp_path).verification == "passed"
    if os.name == "nt":
        import subprocess
        assert command_argv(subprocess.list2cmdline(command)) == command


@pytest.mark.parametrize("head", ["a" * 40, None])
def test_rereview_guard_refuses_changed_or_missing_head(head):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return rr.subprocess.CompletedProcess(argv, 0, json.dumps({"state": "open", "head": {"sha": head}}), "")

    with pytest.raises(RuntimeError, match="PR head"):
        rr.post_rereview("owner/repo", 1, "@coderabbitai review", runner=runner, expect_head="b" * 40)
    assert len(calls) == 1
    assert "POST" not in calls[0]


def test_rereview_guard_posts_only_matching_head():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        data = {"created_at": "2026-10-12T00:00:00Z"} if "POST" in argv else {"state": "open", "head": {"sha": "b" * 40}}
        return rr.subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    rr.post_rereview("owner/repo", 1, "@coderabbitai review", runner=runner, expect_head="b" * 40)
    assert len(calls) == 2
    assert "POST" in calls[1]
