#!/usr/bin/env python3
"""Turn the plain text of one GLG email into structured JSON.

Reads the email body (and optionally its subject) and reports what kind of
GLG message it is, which project it concerns, the action links it carries,
when those links expire, and, for an "Accept Confirmation" email, the exact
question and answer pairs GLG recorded. Stdlib only. Nothing is sent anywhere.

Usage:
    parse_glg_email.py --subject "..." < body.txt
    parse_glg_email.py --file body.txt --subject "..."
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MEMBERS_HOST = "members.glgresearch.com"

LINK_RE = re.compile(r"https?://[^\s\]>\"']+")
URL_FIELD_RE = re.compile(r"[?&](cpid|cpId|consultationIds)=([^&\s]+)", re.IGNORECASE)
PROJECT_ID_RE = re.compile(r"Project ID\s*:?\s*(\d{5,})", re.IGNORECASE)
RATE_RE = re.compile(r"hourly rate for GLG\s+consultations is\s*\$\s*([\d,]+)", re.IGNORECASE)
CLIENT_RE = re.compile(
    r"One of our clients,\s*(an?|the)\s+(.+?),\s*would\s+like", re.IGNORECASE | re.DOTALL
)
MANAGER_RE = re.compile(r"^\s*([A-Za-z][\w.+-]*@glgroup\.com)\s*\|", re.MULTILINE)
QA_RE = re.compile(r"^Q:\s*(.+?)\nA:\s*(.+?)(?=\nQ:|\nTerms and Conditions|\Z)", re.MULTILINE | re.DOTALL)

SUBJECT_TITLE_PATTERNS = [
    re.compile(r"^(?:re:\s*)?GLG:\s*Accept Confirmation\s*[–-]\s*(.+)$", re.IGNORECASE),
    re.compile(r"^(?:re:\s*)?GLG:\s*Follow up on your acceptance for\s+(.+)$", re.IGNORECASE),
    re.compile(r"^(?:re:\s*)?GLG:\s*Call requested on\s+'(.+?)'", re.IGNORECASE),
    re.compile(r"^(?:re:\s*)?GLG Consultation with our client on\s+'(.+?)'", re.IGNORECASE),
    re.compile(r"^(?:re:\s*)?GLG\s*-\s*\*?URGENT\*?\s*new project on\s+(.+)$", re.IGNORECASE),
    re.compile(r"^(?:re:\s*)?GLG\s*-\s*We Think You.re a Fit to Discuss\s*\"?(.+?)\"?$", re.IGNORECASE),
    re.compile(r"^(?:re:\s*)?(.+?)\s+consultation\s*-\s*GLG\s*$", re.IGNORECASE),
]

SUBJECT_KIND_HINTS = [
    ("survey", "survey"),
    ("payment", "payment"),
    ("invoice", "payment"),
    ("reminder: your glg call", "call_reminder"),
    ("call requested", "call_request"),
    ("glg consultation with our client", "call"),
    ("accept confirmation", "accept_confirmation"),
    ("follow up on your acceptance", "followup"),
    ("just tried giving you a call", "invitation"),
    ("in case you missed it", "invitation"),
    ("open glg projects waiting", "digest"),
]


def _jwt_expiry(token: str) -> str | None:
    """Expiry of the _session JWT as ISO-8601 UTC, read without verifying."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = int(data["exp"])
    except (ValueError, KeyError, TypeError):
        return None
    return datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()


def classify_link(url: str) -> str:
    """The role of one URL, or "other" when it is not a GLG members link.

    The host is parsed and compared whole, never searched for as a substring:
    the links this parser reports are opened in the member's browser, so
    ``members.glgresearch.com.attacker.example`` (or an ``@``-userinfo trick,
    or the host repeated in a path) must not come back as an accept link.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme.lower() not in {"http", "https"} or host != MEMBERS_HOST:
        return "other"
    path = parts.path
    if "/accept/accept" in path:
        return "accept"
    if "cm-accept-followup" in path:
        return "followup"
    if "/opportunities" in path and "consultationIds=" in parts.query:
        return "decline"
    return "members"


def extract_links(body: str) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in LINK_RE.findall(body):
        url = raw.rstrip(".,;)")
        if url in seen:
            continue
        seen.add(url)
        role = classify_link(url)
        if role == "other":
            continue
        entry: dict[str, Any] = {"role": role, "url": url}
        for key, value in URL_FIELD_RE.findall(url):
            entry[key if key != "cpId" else "cpid"] = value
        session = re.search(r"[?&]_session=([^&\s]+)", url)
        if session:
            entry["expires_at"] = _jwt_expiry(session.group(1))
        links.append(entry)
    return links


def _title_from_subject(subject: str) -> str | None:
    subject = subject.strip()
    for pattern in SUBJECT_TITLE_PATTERNS:
        match = pattern.match(subject)
        if match:
            return match.group(1).strip().strip("\"'")
    return None


def _title_from_body(body: str) -> str | None:
    patterns = [
        r"looking to speak with customers of\s+(.+?)\s*\.",
        r"accepting our project\s+(.+?)\s*\.",
        r"project my client is researching on\s+(.+?)\s*\.",
        r"A client is researching\s+(.+?)\s+from",
    ]
    for pattern in patterns:
        match = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
        if match:
            return " ".join(match.group(1).split())
    return None


def _flags(body: str, subject: str) -> dict[str, bool]:
    low = body.lower()
    return {
        "single_blinded": "single-blinded" in low or "single blinded" in low,
        "recorded": "elected to record" in low,
        "urgent": "urgent" in subject.lower(),
        "ai_use_prohibited": "may not use or rely upon ai-generated" in low,
        "client_terms_appended": "asked that the following comment be" in low,
    }


def _description_bullets(body: str) -> list[str]:
    bullets: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if re.match(r"^\s*[*•-]\s+", line):
            if current:
                bullets.append(" ".join(current))
            current = [re.sub(r"^\s*[*•-]\s+", "", line).strip()]
        elif current and line.startswith("  ") and line.strip():
            current.append(line.strip())
        elif current:
            bullets.append(" ".join(current))
            current = []
    if current:
        bullets.append(" ".join(current))
    return bullets


def _parse_answer(raw: str) -> dict[str, Any]:
    """Split GLG's recorded answer into structured value plus free-text note."""
    raw = raw.strip()
    json_part, note = raw, ""
    for opener, closer in (("{", "}"), ("[", "]")):
        if raw.startswith(opener):
            depth, end = 0, -1
            for index, char in enumerate(raw):
                if char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        end = index
                        break
            if end >= 0:
                json_part, note = raw[: end + 1], raw[end + 1 :]
            break
    try:
        value = json.loads(json_part)
    except json.JSONDecodeError:
        return {"value": raw, "note": ""}
    note = note.strip().lstrip("-–").strip()
    return {"value": value, "note": note}


def extract_recorded_answers(body: str) -> list[dict[str, Any]]:
    answers = []
    for question, answer in QA_RE.findall(body):
        parsed = _parse_answer(answer)
        answers.append({"question": " ".join(question.split()), **parsed})
    return answers


def detect_kind(subject: str, links: list[dict[str, Any]], answers: list) -> str:
    low = subject.lower()
    if answers:
        return "accept_confirmation"
    roles = {link["role"] for link in links}
    if "followup" in roles:
        return "followup"
    if "accept" in roles:
        return "invitation"
    for needle, kind in SUBJECT_KIND_HINTS:
        if needle in low:
            return kind
    if low.endswith("- glg") or "glg -" in low:
        return "invitation"
    return "other"


def parse(body: str, subject: str = "") -> dict[str, Any]:
    links = extract_links(body)
    answers = extract_recorded_answers(body)
    kind = detect_kind(subject, links, answers)
    client = CLIENT_RE.search(body)
    rate = RATE_RE.search(body)
    manager = MANAGER_RE.search(body)
    project_id = PROJECT_ID_RE.search(subject) or PROJECT_ID_RE.search(body)
    accept = next((link for link in links if link["role"] == "accept"), None)
    followup = next((link for link in links if link["role"] == "followup"), None)
    decline = next((link for link in links if link["role"] == "decline"), None)
    return {
        "kind": kind,
        "subject": subject.strip(),
        "project_title": _title_from_subject(subject) or _title_from_body(body),
        "project_id": project_id.group(1) if project_id else None,
        "cpid": (accept or followup or {}).get("cpid"),
        "consultation_ids": (decline or {}).get("consultationIds"),
        "hourly_rate_usd": int(rate.group(1).replace(",", "")) if rate else None,
        "client_type": " ".join(client.group(2).split()) if client else None,
        "research_manager_email": manager.group(1) if manager else None,
        "flags": _flags(body, subject),
        "description_bullets": _description_bullets(body),
        "links": links,
        "accept_link_expires_at": (accept or followup or {}).get("expires_at"),
        "recorded_answers": answers,
    }


def main(argv: list[str] | None = None) -> int:
    # Step 1: read the email text from a file or stdin
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, help="plain-text body (default: stdin)")
    parser.add_argument("--subject", default="", help="the email subject line")
    args = parser.parse_args(argv)
    body = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
    if not body.strip():
        print("error: empty email body", file=sys.stderr)
        return 2

    # Step 2: parse and emit JSON for the agent to read
    print(json.dumps(parse(body, args.subject), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
