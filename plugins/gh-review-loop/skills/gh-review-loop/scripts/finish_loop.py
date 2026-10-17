#!/usr/bin/env python3
"""Emit the terminal receipt and record the exact PR's active run once."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import loop_lifecycle
import loop_lock
import metrics
import request_rereview as rr


def finish(repo: str, pr: int, root: Path, *, verification: dict | None = None, options: list[str] | None = None, dry_run: bool = False) -> dict:
    with loop_lock.pr_lock(repo, pr):
        run = loop_lifecycle.active_run(repo, pr)
        if run is None:
            return {"status": "already_finished", "recorded": False}
        if dry_run:
            return {"status": "ready_to_finish", "recorded": False}
        log = metrics.runs_log_path()
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()] if log.exists() else []
        if any(not isinstance(record, dict) for record in records):
            raise RuntimeError("cannot establish terminal recording history from non-object metrics records")
        if run.get("started_at") and any(str(record.get("repo", "")).casefold() == repo.casefold() and record.get("pr") == pr
                                         and record.get("started_at") == run["started_at"] for record in records):
            raise RuntimeError("this run is already recorded but its tracking remains; inspect local state instead of recording it again")
        receipt = loop_lifecycle.emit_receipt(repo, pr, root.resolve(), terminal=True, verification=verification, options=options)
        if loop_lifecycle.active_run(repo, pr) is not None:
            raise RuntimeError("terminal recording did not clear the active run; inspect receipt warnings before retrying")
        return {"status": "finished", "recorded": True, "receipt": receipt}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=rr.positive_pr)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--verification", choices=["passed", "failed", "skipped"], default="skipped")
    parser.add_argument("--verification-details")
    parser.add_argument("--outcome")
    parser.add_argument("--outcome-reason")
    confirmation = parser.add_mutually_exclusive_group()
    confirmation.add_argument("--gemini-confirmed", action="store_true")
    confirmation.add_argument("--gemini-unconfirmed", action="store_true")
    parser.add_argument("--max-rereview-requests", type=loop_lifecycle.nonnegative_int)
    parser.add_argument("--dry-run", action="store_true")
    loop_lifecycle.add_receipt_arguments(parser)
    args = parser.parse_args(argv)
    try:
        verification = json.loads(args.verification_details) if args.verification_details else {"verification": args.verification}
        if not isinstance(verification, dict) or verification.get("verification") not in ("passed", "failed", "skipped"):
            raise ValueError("verification details must contain a passed/failed/skipped verification result")
        options = loop_lifecycle.receipt_arguments(args)
        for name in ("outcome", "outcome_reason", "max_rereview_requests"):
            value = getattr(args, name)
            if value is not None:
                options.extend(["--" + name.replace("_", "-"), str(value)])
        for name in ("gemini_confirmed", "gemini_unconfirmed"):
            if getattr(args, name):
                options.append("--" + name.replace("_", "-"))
        result = finish(args.repo, args.pr, args.repo_root, verification=verification, options=options, dry_run=args.dry_run)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "blocked", "recorded": None, "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
