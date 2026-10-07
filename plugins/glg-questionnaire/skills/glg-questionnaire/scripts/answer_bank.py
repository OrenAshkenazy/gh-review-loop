#!/usr/bin/env python3
"""Local bank of questionnaire answers the member wrote themselves.

The bank exists so the agent never has to invent an answer: every entry was
either recorded by GLG from the member's own submission (seeded from an
"Accept Confirmation" email) or typed by the member in chat. Lookups match a
new question against stored ones by normalized text similarity, so a vendor
familiarity grid asked in slightly different wording still finds the
member's earlier answer. Stdlib only, stored under ~/.config/glg-questionnaire.

Usage:
    answer_bank.py seed --from-parsed parsed.json
    answer_bank.py add --question "..." --answer '"Key Decision Maker"' [--note ...]
    answer_bank.py lookup --question "..." [--limit 3] [--min-score 0.55]
    answer_bank.py list
    answer_bank.py log --project "..." --cpid 123 --action submitted
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "below", "for", "if", "in", "is",
    "it", "of", "on", "or", "please", "the", "these", "this", "to", "you", "your",
}


def state_dir() -> Path:
    override = os.environ.get("GLGQ_STATE_DIR")
    base = Path(override) if override else Path.home() / ".config" / "glg-questionnaire"
    base.mkdir(parents=True, exist_ok=True)
    return base


def answers_path() -> Path:
    return state_dir() / "answers.jsonl"


def log_path() -> Path:
    return state_dir() / "submissions.jsonl"


def normalize(question: str) -> str:
    words = re.findall(r"[a-z0-9]+", question.lower())
    return " ".join(word for word in words if word not in STOPWORDS)


def load_entries() -> list[dict[str, Any]]:
    path = answers_path()
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def _append(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def add_entry(
    question: str,
    value: Any,
    note: str = "",
    source: str = "member",
    project: str | None = None,
) -> dict[str, Any]:
    record = {
        "question": " ".join(question.split()),
        "normalized": normalize(question),
        "value": value,
        "note": note,
        "source": source,
        "project": project,
        "recorded_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }
    _append(answers_path(), record)
    return record


def score(question: str, entry: dict[str, Any]) -> float:
    return difflib.SequenceMatcher(None, normalize(question), entry["normalized"]).ratio()


def lookup(question: str, limit: int = 3, min_score: float = 0.55) -> list[dict[str, Any]]:
    scored = [
        (score(question, entry), index, entry)
        for index, entry in enumerate(load_entries())
    ]
    scored = [item for item in scored if item[0] >= min_score]
    # Best match first; on a tie the newest line (appended last) wins.
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [{"score": round(s, 3), **entry} for s, _, entry in scored[:limit]]


def seed_from_parsed(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    if parsed.get("kind") != "accept_confirmation":
        raise ValueError("seed needs a parsed accept_confirmation email")
    known = {(e["normalized"], json.dumps(e["value"], sort_keys=True)) for e in load_entries()}
    added = []
    for item in parsed.get("recorded_answers", []):
        key = (normalize(item["question"]), json.dumps(item["value"], sort_keys=True))
        if key in known:
            continue
        added.append(
            add_entry(
                item["question"],
                item["value"],
                note=item.get("note", ""),
                source="glg_accept_confirmation",
                project=parsed.get("project_title"),
            )
        )
    return added


def log_submission(project: str, cpid: str | None, action: str, detail: str = "") -> dict:
    record = {
        "project": project,
        "cpid": cpid,
        "action": action,
        "detail": detail,
        "at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }
    _append(log_path(), record)
    return record


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def main(argv: list[str] | None = None) -> int:
    # Step 1: declare the subcommands
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="import answers GLG recorded from the member")
    seed.add_argument("--from-parsed", type=Path, required=True)

    add = sub.add_parser("add", help="store one answer the member wrote")
    add.add_argument("--question", required=True)
    add.add_argument("--answer", required=True, help="JSON or plain text")
    add.add_argument("--note", default="")
    add.add_argument("--project")

    look = sub.add_parser("lookup", help="find stored answers for a question")
    look.add_argument("--question", required=True)
    look.add_argument("--limit", type=int, default=3)
    look.add_argument("--min-score", type=float, default=0.55)

    sub.add_parser("list", help="print every stored answer")

    log = sub.add_parser("log", help="record an accept, decline or submit")
    log.add_argument("--project", required=True)
    log.add_argument("--cpid")
    log.add_argument("--action", required=True, choices=["accepted", "declined", "submitted", "skipped"])
    log.add_argument("--detail", default="")

    args = parser.parse_args(argv)

    # Step 2: dispatch and print JSON so the agent can relay it
    if args.command == "seed":
        parsed = json.loads(args.from_parsed.read_text(encoding="utf-8"))
        result: Any = {"added": seed_from_parsed(parsed)}
    elif args.command == "add":
        result = add_entry(args.question, _parse_value(args.answer), args.note, project=args.project)
    elif args.command == "lookup":
        result = lookup(args.question, args.limit, args.min_score)
    elif args.command == "list":
        result = load_entries()
    else:
        result = log_submission(args.project, args.cpid, args.action, args.detail)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
