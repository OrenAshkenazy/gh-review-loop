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


# The bank holds employment, customer-relationship and conflict answers, so
# the directory and its files are the member's alone: 0700 / 0600 regardless of
# the process umask, and an existing looser mode is repaired on use. mkdir's
# mode argument is masked by the umask, so the chmod is the actual guarantee.
DIR_MODE = 0o700
FILE_MODE = 0o600


def _restrict(path: Path, mode: int) -> None:
    """Best-effort owner-only permissions; never fail the operation over it."""
    try:
        if (path.stat().st_mode & 0o777) != mode:
            path.chmod(mode)
    except OSError:
        pass


def state_dir() -> Path:
    override = os.environ.get("GLGQ_STATE_DIR")
    base = Path(override) if override else Path.home() / ".config" / "glg-questionnaire"
    base.mkdir(parents=True, exist_ok=True)
    _restrict(base, DIR_MODE)
    return base


def answers_path() -> Path:
    return state_dir() / "answers.jsonl"


def log_path() -> Path:
    return state_dir() / "submissions.jsonl"


class NotReusable(ValueError):
    """Raised for a question whose answer must never be reused from the bank."""


# Per-project attestations: current employer, conflicts, recording consent and
# blinded-client acknowledgements. SKILL.md hard rule 3 sends these to the
# member every time, and PRIVACY.md states they are not stored. A stored copy
# would come back from lookup() as a BANK suggestion, which is exactly the
# answer the member must give afresh — so the store refuses them rather than
# relying on the agent to remember.
# The list mirrors references/compliance.md, "Questions that always go to the
# member", class for class. A class missing here is an answer the bank would
# offer back as BANK for a compliance attestation — the one place a stale
# answer is not merely unhelpful but wrong.
ATTESTATION_PATTERNS = [
    # Current employment, and contacting it.
    re.compile(r"\bcurrent(ly)?\b[^.?]*\bemploy", re.IGNORECASE),
    re.compile(r"\bemploy(ed|er|ment)\b[^.?]*\b(now|current|present|today)", re.IGNORECASE),
    re.compile(r"\bwho is your (current )?employer\b", re.IGNORECASE),
    re.compile(r"\b(may|can) we contact\b", re.IGNORECASE),
    re.compile(r"\bdo you work for any of these\b", re.IGNORECASE),
    # Conflicts and non-public information.
    re.compile(r"\bconflict(s)? of interest\b|\bhave a conflict\b", re.IGNORECASE),
    re.compile(r"\b(material )?non[- ]public information\b", re.IGNORECASE),
    re.compile(r"\binside information\b", re.IGNORECASE),
    # Recording consent.
    re.compile(r"\brecord(ed|ing)\b[^.?]*\b(consent|permission|agree|call)\b", re.IGNORECASE),
    re.compile(r"\bconsent\b[^.?]*\brecord", re.IGNORECASE),
    # Blinded clients.
    re.compile(r"\bsingle[- ]blind(ed)?\b", re.IGNORECASE),
    re.compile(r"\bblinded client\b", re.IGNORECASE),
    # Pending trials and litigation.
    re.compile(r"\bpending\b[^.?]*\b(trial|litigation|lawsuit|proceeding)", re.IGNORECASE),
    re.compile(r"\b(trial|litigation|lawsuit)\b[^.?]*\b(pending|ongoing|involved)", re.IGNORECASE),
    # Government and public-sector roles.
    re.compile(r"\bgovernment\b[^.?]*\b(role|official|position|employee|service)", re.IGNORECASE),
    re.compile(r"\bpublic[- ]sector\b[^.?]*\b(role|position|employ)", re.IGNORECASE),
    re.compile(r"\bpublic official\b", re.IGNORECASE),
    # Public-company employment within the last six months.
    re.compile(r"\bpublicly[- ]traded\b", re.IGNORECASE),
    re.compile(r"\bpublic company\b[^.?]*\b(employ|work|officer|board)", re.IGNORECASE),
    re.compile(r"\b(six|6) months\b[^.?]*\b(employ|work|left|depart)", re.IGNORECASE),
    re.compile(r"\b(employ|work|left|depart)[^.?]*\b(six|6) months\b", re.IGNORECASE),
    # Other GLG projects, in either word order: "which other GLG projects",
    # "projects completed through GLG", "consultations with GLG".
    re.compile(r"\bother GLG\b|\bGLG (projects?|consultations?|engagements?)\b", re.IGNORECASE),
    re.compile(
        r"\b(project|consultation|engagement|call)s?\b[^.?]*\b(through|via|with|for|at) GLG\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(through|via|with|for) GLG\b[^.?]*\b(project|consultation|engagement)", re.IGNORECASE),
]


def is_attestation(question: str) -> bool:
    """True when this question is the member's to answer on every project."""
    text = " ".join((question or "").split())
    return any(pattern.search(text) for pattern in ATTESTATION_PATTERNS)


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
    # Created owner-only before the first byte is written, so an answer is
    # never world-readable even briefly.
    if not path.exists():
        path.touch(mode=FILE_MODE)
    _restrict(path, FILE_MODE)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def add_entry(
    question: str,
    value: Any,
    note: str = "",
    source: str = "member",
    project: str | None = None,
) -> dict[str, Any]:
    if is_attestation(question):
        raise NotReusable(
            "this question is a per-project attestation (employer, conflict, "
            "recording consent, blinded client): ask the member every time, "
            "do not store the answer"
        )
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
    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in parsed.get("recorded_answers", []):
        question = item["question"]
        if is_attestation(question):
            # Reported, never stored: the agent must see that GLG recorded an
            # answer here and still ask the member for this project's.
            skipped.append({"question": " ".join(question.split()), "reason": "attestation"})
            continue
        key = (normalize(question), json.dumps(item["value"], sort_keys=True))
        if key in known:
            continue
        known.add(key)
        added.append(
            add_entry(
                question,
                item["value"],
                note=item.get("note", ""),
                source="glg_accept_confirmation",
                project=parsed.get("project_title"),
            )
        )
    return {"added": added, "skipped": skipped}


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


def read_text_arg(value: str | None, path: Path | None) -> str:
    """One question, from a file (or stdin with ``-``) rather than a shell word.

    Questionnaire questions come from a web page the member did not write. A
    documented command template that interpolates one into an argument runs
    whatever ``$(...)`` or backticks it contains, since double quotes do not
    suppress command substitution. Reading the text from a file removes the
    shell from the path entirely.
    """
    if path is not None:
        if str(path) == "-":
            return sys.stdin.read().strip()
        return path.read_text(encoding="utf-8").strip()
    if value is None:
        raise ValueError("pass --question or --question-file")
    return value


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
    add.add_argument("--question")
    add.add_argument("--question-file", type=Path, help="read the question from a file, or - for stdin")
    add.add_argument("--answer", required=True, help="JSON or plain text")
    add.add_argument("--note", default="")
    add.add_argument("--project")

    look = sub.add_parser("lookup", help="find stored answers for a question")
    look.add_argument("--question")
    look.add_argument("--question-file", type=Path, help="read the question from a file, or - for stdin")
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
        result: Any = seed_from_parsed(parsed)
    elif args.command == "add":
        try:
            result = add_entry(
                read_text_arg(args.question, args.question_file),
                _parse_value(args.answer),
                args.note,
                project=args.project,
            )
        except NotReusable as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 3
    elif args.command == "lookup":
        result = lookup(
            read_text_arg(args.question, args.question_file),
            args.limit,
            args.min_score,
        )
    elif args.command == "list":
        result = load_entries()
    else:
        result = log_submission(args.project, args.cpid, args.action, args.detail)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
