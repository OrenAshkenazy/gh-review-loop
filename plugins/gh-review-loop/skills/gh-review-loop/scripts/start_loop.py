#!/usr/bin/env python3
"""Check the fetched PR and saved verification decision before applying fixes."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import judge
import loop_lifecycle
import publish_changes as pc
import request_rereview as rr


def start(repo: str, pr: int, root: Path) -> dict:
    root = root.resolve()
    if Path(pc.git(root, "rev-parse", "--show-toplevel").strip()).resolve() != root:
        raise RuntimeError("repo-root must be the checkout root")
    if loop_lifecycle.active_run(repo, pr) is None:
        raise RuntimeError("fetch this PR before starting fixes")
    if not rr.read_persisted_reviewer(repo, pr):
        raise RuntimeError("select a reviewer for this PR before starting fixes")
    profile = judge.get_profile(repo)
    if not isinstance(profile, dict) or not isinstance(profile.get("source"), str) or profile["source"] not in judge.PROFILE_SOURCES:
        raise RuntimeError("save a verification profile decision before starting fixes; run detect_profile.py and save the user's choice")
    if profile["source"] != "skipped":
        pc.validate_profile(profile, root)
    return {"status": "ready", "repo": repo, "pr": pr, "profile_source": profile["source"], "profile_allows_publication": profile["source"] != "skipped"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=rr.positive_pr)
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = start(args.repo, args.pr, args.repo_root)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
