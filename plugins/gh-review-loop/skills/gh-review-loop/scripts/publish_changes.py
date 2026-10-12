#!/usr/bin/env python3
"""Verify an explicit set of PR fixes, commit it, and push without force."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import judge
import loop_state
import request_rereview as rr
import review_vendors
from run_profile import command_argv, run_profile


class PublishError(RuntimeError):
    pass


def run(argv: list[str], root: Path) -> str:
    result = subprocess.run(argv, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="strict", check=False)
    if result.returncode:
        raise PublishError(f"{argv[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def git(root: Path, *args: str) -> str:
    return run(["git", *args], root)


def pr_info(repo: str, pr: int, root: Path) -> dict:
    rr.parse_repo(repo)
    info = json.loads(run(["gh", "api", f"repos/{repo}/pulls/{pr}"], root))
    if not isinstance(info, dict) or info.get("state") != "open":
        raise PublishError("PR must be open")
    return info


def remote_identity(url: str) -> tuple[str, str]:
    """Normalize HTTPS/SSH Git URLs without accepting credentials or local paths."""
    if "://" not in url:
        match = re.fullmatch(r"git@([^:/]+):([^\s]+)", url)
        if not match:
            raise PublishError("push remote must identify a GitHub repository")
        host, path = match.groups()
    else:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or parsed.query or parsed.fragment or parsed.password or parsed.port:
            raise PublishError("unsupported push remote URL")
        if parsed.username and (parsed.scheme != "ssh" or parsed.username != "git"):
            raise PublishError("credentials in push remote URL are not supported")
        host, path = parsed.hostname, parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", path):
        raise PublishError("invalid repository path in push remote URL")
    return host.casefold(), path.casefold()


def approved_paths(root: Path, paths: list[str]) -> list[str]:
    if not paths or len(set(paths)) != len(paths):
        raise PublishError("provide a nonempty list of distinct file paths")
    for raw in paths:
        path = PurePosixPath(raw)
        if not raw or "\\" in raw or ":" in raw or "\0" in raw or path.is_absolute() or path.as_posix() != raw or any(part in {"..", ".git"} for part in path.parts):
            raise PublishError(f"invalid repository-relative file path: {raw!r}")
        target = root / raw
        if target.is_symlink() or not target.resolve().is_relative_to(root) or target.is_dir():
            raise PublishError(f"file path escapes the checkout or is not a regular file: {raw!r}")
    return paths


def snapshot(root: Path, paths: list[str]) -> tuple[str, tuple]:
    if git(root, "diff", "--cached", "--name-only", "-z"):
        raise PublishError("index must be empty; preserve or unstage existing staged changes first")
    changed = set(git(root, "diff", "--name-only", "--no-renames", "-z").split("\0")) - {""}
    untracked = set(git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""}
    if changed | untracked != set(paths):
        raise PublishError("approved paths must exactly match all changed and untracked files")
    files = []
    for raw in approved_paths(root, paths):
        path = root / raw
        files.append((raw, path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest()) if path.exists() else (raw, None, None))
    diff = git(root, "--literal-pathspecs", "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", "--", *paths)
    return diff, tuple(files)


def validate_profile(profile: object, root: Path) -> dict:
    if not isinstance(profile, dict) or profile.get("source") not in {"confirmed", "customized"}:
        raise PublishError("save a confirmed/customized verification profile before publishing")
    checks = profile.get("checks")
    if not isinstance(checks, list) or not checks:
        raise PublishError("verification profile must contain checks")
    if not all(isinstance(c, dict) and type(c.get("required")) is bool for c in checks) or not any(c["required"] for c in checks):
        raise PublishError("verification profile must have boolean required flags and at least one required check")
    for check in checks:
        if not command_argv(check.get("command")):
            raise PublishError("verification check has an empty command")
        directory = check.get("working_directory") or profile.get("working_directory", ".")
        if not isinstance(directory, str) or not (root / directory).resolve().is_relative_to(root) or not (root / directory).is_dir():
            raise PublishError("verification working directories must exist inside the checkout")
    return profile


def verified_blobs(root: Path, paths: list[str]) -> dict[str, tuple[str, frozenset[str]]]:
    """Expected staged blobs, applying Git's configured clean/EOL conversion."""
    modes = {}
    for entry in git(root, "--literal-pathspecs", "ls-files", "--stage", "-z", "--", *paths).split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            modes[path] = metadata.split()[0]
    filemode = git(root, "config", "--bool", "core.filemode").strip() == "true"
    blobs = {}
    for raw in paths:
        path = root / raw
        if path.exists():
            mode = ("100755" if path.stat().st_mode & 0o111 else "100644") if filemode else modes.get(raw, "100644")
            hashes = {git(root, "hash-object", f"--path={raw}", "--", raw).strip()}
            # Git add preserves CRLF already present in the index, whereas
            # hash-object may normalize it. Both represent verified bytes.
            attribute = git(root, "check-attr", "-z", "filter", "--", raw).split("\0")[-2]
            if attribute in {"unspecified", "unset"}:
                hashes.add(git(root, "hash-object", "--no-filters", "--", raw).strip())
            blobs[raw] = (mode, frozenset(hashes))
    return blobs


def staged_gate(root: Path, paths: list[str], blobs: dict[str, tuple[str, frozenset[str]]]) -> None:
    staged = set(git(root, "diff", "--cached", "--name-only", "--no-renames", "-z").split("\0")) - {""}
    entries = {}
    for entry in git(root, "--literal-pathspecs", "ls-files", "--stage", "-z", "--", *paths).split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            mode, oid, stage = metadata.split()
            if stage != "0" or mode not in {"100644", "100755"}:
                raise PublishError("unverified index entry; staged changes preserved, no commit")
            entries[path] = (mode, oid)
    if staged != set(paths) or set(entries) != set(blobs) or any(mode != blobs[path][0] or oid not in blobs[path][1] for path, (mode, oid) in entries.items()):
        raise PublishError("staged bytes differ from verified changes; staged changes preserved, no commit")


def loop_gate(repo: str, pr: int, max_rereview_requests: int | None = None) -> None:
    state = rr.load_state()
    entry = state.get(rr.state_key_for(state, repo, pr))
    tracking = entry.get("run") if isinstance(entry, dict) else None
    if not isinstance(tracking, dict) or loop_state.safe_int(tracking.get("update_seq")) <= 0 or loop_state.summary_is_stale(tracking):
        raise PublishError("fetch this PR, then emit and relay --cycle-summary before publishing")
    reviewer = rr.read_persisted_reviewer(repo, pr)
    if not reviewer:
        raise PublishError("confirm a reviewer for this PR before publishing")
    login = rr.gh_login()
    vendor = review_vendors.vendor_for(reviewer.get("login"))
    if vendor is not None and vendor.trigger_kind == "review_request":
        request = rr.request_state(repo, pr, vendor)
        first_review = request.get("first_completed_review_at") if request else None
        used = rr.count_agent_review_requests(repo, pr, vendor, login, first_review) if first_review and login else None
    else:
        trigger, _ = rr.resolve_review_trigger(repo=repo, pr=pr, reviewer_login=None, reviewer_mention=None)
        used = rr.count_agent_pings(repo, pr, trigger, login) if trigger and login else None
    if used is None:
        raise PublishError("re-review cap cannot be established; publishing blocked")
    if used >= rr.effective_cap(max_rereview_requests):
        raise PublishError("re-review cap reached; publishing blocked")


def checkout_gate(root: Path, repo: str, pr: int, remote: str, expected_head: str | None = None) -> tuple[str, str]:
    info = pr_info(repo, pr, root)
    head, base = info.get("head", {}), info.get("base", {})
    head_repo = head.get("repo") or {}
    branch, sha = head.get("ref"), head.get("sha")
    if not branch or not sha or not head_repo.get("html_url") or not head_repo.get("default_branch"):
        raise PublishError("PR head repository metadata is incomplete")
    if branch in {base.get("ref"), head_repo["default_branch"]}:
        raise PublishError("publishing from a base/default branch is blocked")
    if git(root, "branch", "--show-current").strip() != branch or git(root, "rev-parse", "HEAD").strip() != sha:
        raise PublishError("checkout branch and HEAD must match the current PR head")
    if expected_head and sha != expected_head:
        raise PublishError("PR head changed during verification")
    urls = git(root, "remote", "get-url", "--push", "--all", remote).splitlines()
    if len(urls) != 1 or remote_identity(urls[0]) != remote_identity(head_repo["html_url"]):
        raise PublishError("push remote must match the PR head repository exactly")
    tip = git(root, "ls-remote", "--refs", remote, f"refs/heads/{branch}").strip().split()
    if len(tip) != 2 or tip[0] != sha:
        raise PublishError("remote branch must match the PR head before publishing")
    return branch, sha


def publish(repo: str, pr: int, root: Path, paths: list[str], message: str, remote: str = "origin", dry_run: bool = False, max_rereview_requests: int | None = None) -> dict:
    root = root.resolve()
    if not message.strip() or remote.startswith("-") or not remote:
        raise PublishError("provide a commit message and a named Git remote")
    if Path(git(root, "rev-parse", "--show-toplevel").strip()).resolve() != root:
        raise PublishError("repo-root must be the checkout root")
    branch, head = checkout_gate(root, repo, pr, remote)
    loop_gate(repo, pr, max_rereview_requests)
    before = snapshot(root, paths)
    blobs = verified_blobs(root, paths)
    if snapshot(root, paths) != before:
        raise PublishError("files changed while computing expected Git content")
    profile = validate_profile(judge.get_profile(repo), root)
    result = run_profile(profile, root)
    if result.verification != "passed":
        raise PublishError(f"required verification failed: {', '.join(result.failed_required)}")
    if snapshot(root, paths) != before or verified_blobs(root, paths) != blobs or judge.get_profile(repo) != profile:
        raise PublishError("files or verification profile changed during verification; verify again")
    checkout_gate(root, repo, pr, remote, expected_head=head)
    loop_gate(repo, pr, max_rereview_requests)
    if snapshot(root, paths) != before:
        raise PublishError("files changed before staging; verify again")
    payload = {"status": "verified", "published": False, "dry_run": dry_run, "head": head, "verification": result.to_details()}
    if dry_run:
        return payload
    git(root, "--literal-pathspecs", "add", "--", *paths)
    staged_gate(root, paths, blobs)
    tree = git(root, "write-tree").strip()
    git(root, "commit", "-m", message)
    commit = git(root, "rev-parse", "HEAD").strip()
    if git(root, "rev-parse", "HEAD^").strip() != head or git(root, "rev-parse", "HEAD^{tree}").strip() != tree or git(root, "status", "--porcelain"):
        raise PublishError("commit hooks changed the verified tree; local commit preserved, no push")
    info = pr_info(repo, pr, root)
    if info.get("head", {}).get("sha") != head:
        raise PublishError("PR head changed before push; local commit preserved, no push")
    urls = git(root, "remote", "get-url", "--push", "--all", remote).splitlines()
    if len(urls) != 1 or remote_identity(urls[0]) != remote_identity((info.get("head", {}).get("repo") or {}).get("html_url", "")):
        raise PublishError("push remote changed; local commit preserved, no push")
    loop_gate(repo, pr, max_rereview_requests)
    git(root, "push", remote, f"HEAD:refs/heads/{branch}")
    tip = git(root, "ls-remote", "--refs", remote, f"refs/heads/{branch}").strip().split()
    if len(tip) != 2 or tip[0] != commit:
        raise PublishError("push could not be confirmed; local commit preserved")
    payload.update(status="published", published=True, head=commit)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=rr.positive_pr)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--paths", required=True, nargs="+")
    parser.add_argument("--message", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-rereview-requests", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        payload = publish(args.repo, args.pr, args.repo_root, args.paths, args.message, args.remote, args.dry_run, args.max_rereview_requests)
    except (PublishError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "blocked", "published": False, "error": str(exc)}))
        return 1
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
