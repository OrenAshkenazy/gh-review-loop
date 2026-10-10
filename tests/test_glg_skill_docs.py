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


def test_script_commands_resolve_the_plugin_root_for_both_runtimes():
    """The plugin ships a .codex-plugin manifest, and Codex does not set
    CLAUDE_PLUGIN_ROOT. A bare ${CLAUDE_PLUGIN_ROOT} expands to nothing there
    and the command fails before triage begins (PR #116 review)."""
    text = SKILL_MD.read_text(encoding="utf-8")

    assert "GLGQ_PLUGIN_ROOT" in text
    # The resolver must offer a non-Claude path: the Codex install location.
    assert ".codex/plugins" in text
    for line in text.splitlines():
        if "scripts/parse_glg_email.py" in line or "scripts/answer_bank.py" in line:
            assert "CLAUDE_PLUGIN_ROOT" not in line, line
            assert "GLGQ_PLUGIN_ROOT" in line, line


def test_privacy_policy_covers_this_plugin():
    """The Codex manifest links the repo policy; that policy must actually
    disclose what this plugin reads and stores (PR #116 review)."""
    import json as _json

    manifest = _json.loads(
        (SKILL_DIR.parent.parent / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    url = _json.dumps(manifest)
    assert "PRIVACY" in url

    policy = (REPO_ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    assert "glg-questionnaire" in policy
    for needle in ("Gmail", "~/.config/glg-questionnaire", "answers.jsonl", "submissions.jsonl"):
        assert needle in policy, needle


def test_untrusted_text_is_never_interpolated_into_a_shell_command():
    """Email subjects and questionnaire questions are attacker-influenced text.
    `--subject "<subject>"` in a copied command runs `$(...)` and backticks in
    the local shell, since double quotes do not suppress command substitution
    (PR #116 review). Such values must travel by file or stdin."""
    text = SKILL_MD.read_text(encoding="utf-8")

    for banned in ('--subject "<subject>"', '--question "<question text>"'):
        assert banned not in text, banned
    assert "--subject-file" in text
    assert "--question-file" in text


def test_triage_does_not_group_messages_without_a_consultation_id():
    """Surveys, payments, reminders and digests all parse with `cpid: null`;
    grouping on it alone collapses them into one row and the hard rules require
    reporting them (PR #116 review)."""
    text = SKILL_MD.read_text(encoding="utf-8")

    assert "cpid" in text
    lowered = text.lower().replace("`", "")
    assert "without a cpid" in lowered or "no cpid" in lowered
    assert "non-empty" in lowered or "nonempty" in lowered
    assert "thread" in lowered
