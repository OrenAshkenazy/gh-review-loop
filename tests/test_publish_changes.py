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
        return original_git(root, *args)

    monkeypatch.setattr(pc, "git", local_git)
    profile = {"source": "confirmed", "checks": [{"name": "tests", "required": True, "command": [sys.executable, "-c", "assert open('fix.txt').read() == 'after\\n'"]}]}
    monkeypatch.setattr(pc.judge, "get_profile", lambda repo: profile)
    monkeypatch.setattr(rr, "load_state", lambda: {"owner/repo#1": {"run": {"update_seq": 1, "last_summary_seq": 1}}})
    monkeypatch.setattr(rr, "read_persisted_reviewer", lambda *args: {"login": "coderabbitai", "review_trigger": "@coderabbitai"})
    monkeypatch.setattr(rr, "gh_login", lambda: "agent")
    monkeypatch.setattr(rr, "count_agent_pings", lambda *args: 0)
    monkeypatch.setattr(rr, "effective_cap", lambda *args: 3)
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


def test_dry_run_verifies_without_staging_committing_or_pushing(checkout):
    result = publish(checkout, dry_run=True)
    assert result["verification"]["verification"] == "passed"
    assert result["published"] is False
    assert pc.git(checkout[0], "rev-parse", "HEAD").strip() == checkout[2]
    assert pc.git(checkout[0], "diff", "--cached", "--name-only") == ""
    assert_not_pushed(checkout)


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


@pytest.mark.parametrize("state", [{}, {"owner/repo#1": {"run": {"update_seq": 2, "last_summary_seq": 1}}}, {"owner/repo#2": {"run": {"update_seq": 1, "last_summary_seq": 1}}}])
def test_missing_stale_or_other_pr_summary_blocks(checkout, monkeypatch, state):
    monkeypatch.setattr(rr, "load_state", lambda: state)
    with pytest.raises(pc.PublishError, match="cycle-summary"):
        publish(checkout)
    assert_not_pushed(checkout)


@pytest.mark.parametrize("count", [None, 3])
def test_unknown_or_exhausted_cap_blocks(checkout, monkeypatch, count):
    monkeypatch.setattr(rr, "count_agent_pings", lambda *args: count)
    with pytest.raises(pc.PublishError, match="cap"):
        publish(checkout)
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


def test_failed_push_preserves_local_commit(checkout, monkeypatch):
    original_git = pc.git

    def fail_push(root, *args):
        if args[:1] == ("push",):
            raise pc.PublishError("push failed")
        return original_git(root, *args)

    monkeypatch.setattr(pc, "git", fail_push)
    with pytest.raises(pc.PublishError, match="push failed"):
        publish(checkout)
    assert pc.git(checkout[0], "rev-parse", "HEAD").strip() != checkout[2]
    assert_not_pushed(checkout)


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
