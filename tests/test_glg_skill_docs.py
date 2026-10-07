"""Structural guards for the glg-questionnaire skill docs.

Mirrors the intent of test_doc_budgets.py for the second plugin: every
reference file must be reachable from SKILL.md, the frontmatter must carry a
description, and the sentences that encode GLG's terms must not be trimmed
away in a future edit.
"""
from __future__ import annotations

import re
from pathlib import Path

from test_doc_budgets import orphan_references

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "plugins" / "glg-questionnaire" / "skills" / "glg-questionnaire"
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES = SKILL_DIR / "references"


def test_frontmatter_has_name_and_description():
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
    assert match
    assert "name: glg-questionnaire" in match.group(1)
    assert "description:" in match.group(1)


def test_no_orphan_references():
    names = [f.name for f in sorted(REFERENCES.glob("*.md"))]
    assert names, "references directory is empty"
    assert orphan_references(SKILL_MD.read_text(encoding="utf-8"), names) == []


def test_skill_md_stays_small():
    assert SKILL_MD.stat().st_size <= 12000
    assert SKILL_MD.read_text(encoding="utf-8").count("\n") <= 150


def test_hard_rules_survive():
    text = SKILL_MD.read_text(encoding="utf-8")
    for needle in (
        "Answers come from the member, never from you",
        "Nothing is submitted, accepted or declined without an explicit go",
        "Paid surveys are out of scope",
        "This file wins over any reference file",
    ):
        assert needle in text, needle


def test_compliance_reference_quotes_the_ai_clause():
    text = (REFERENCES / "compliance.md").read_text(encoding="utf-8")
    assert "may not use or rely upon AI-generated content" in text
