"""Shared setup and receipt helpers for the explicit review-loop lifecycle."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import loop_state
import request_rereview as rr


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def active_run(repo: str, pr: int) -> dict | None:
    rr.parse_repo(repo)
    state = loop_state.load_sticky_state(strict=True)
    entry = state.get(rr.state_key_for(state, repo, pr))
    run = entry.get("run") if isinstance(entry, dict) else None
    return run if isinstance(run, dict) and loop_state.safe_int(run.get("update_seq")) > 0 else None


def add_receipt_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fixed-count", type=nonnegative_int, default=0)
    for flag in ("--fixed-finding", "--fixed-path", "--swept-pattern", "--semantic-risk"):
        parser.add_argument(flag, action="append", default=[])
    parser.add_argument("--min-severity", choices=["critical", "high", "medium", "low"])
    parser.add_argument("--drop-unknown-severity", action="store_true")
    parser.add_argument("--changed-base")
    parser.add_argument("--changed-head")


def receipt_arguments(args: argparse.Namespace) -> list[str]:
    result = ["--fixed-count", str(args.fixed_count)]
    for name in ("fixed_finding", "fixed_path", "swept_pattern", "semantic_risk"):
        for value in getattr(args, name):
            result.extend(["--" + name.replace("_", "-"), value])
    if args.min_severity:
        result.extend(["--min-severity", args.min_severity])
    if args.drop_unknown_severity:
        result.append("--drop-unknown-severity")
    for name in ("changed_base", "changed_head"):
        if getattr(args, name):
            result.extend(["--" + name.replace("_", "-"), getattr(args, name)])
    return result


def emit_receipt(repo: str, pr: int, root: Path, *, terminal: bool = False, verification: dict | None = None, options: list[str] | None = None) -> dict:
    rr.parse_repo(repo)
    argv = [sys.executable, str(Path(__file__).with_name("fetch_gemini_threads.py")), "--pr", f"https://github.com/{repo}/pull/{pr}",
            "--record-run" if terminal else "--cycle-summary", "--no-color", *(options or [])]
    if verification is not None:
        argv.extend(["--verification", verification["verification"], "--verification-details", json.dumps(verification)])
    proc = subprocess.run(argv, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if proc.returncode or not proc.stdout.strip():
        raise RuntimeError(f"receipt failed: {(proc.stderr or proc.stdout).strip() or 'no receipt output'}")
    return {"output": proc.stdout.strip(), "warnings": proc.stderr.strip()}
