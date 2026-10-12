"""answer_bank.py: local, append-only store of member-authored answers."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "glg-questionnaire" / "skills" / "glg-questionnaire" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import answer_bank as bank  # noqa: E402


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path / "glgq"))
    return tmp_path / "glgq"


GRID_Q = "Please highlight your familiarity as a customer of these platforms."
GRID_V = {"Datadog": ["Current Paying Customer"], "Grafana": ["Current Paying Customer"]}


def test_state_dir_honours_override(state):
    assert bank.state_dir() == state
    assert state.is_dir()


def test_add_then_lookup_exact(state):
    bank.add_entry(GRID_Q, GRID_V, note="design partner", project="Observability")
    hits = bank.lookup(GRID_Q)
    assert len(hits) == 1
    assert hits[0]["score"] == 1.0
    assert hits[0]["value"] == GRID_V
    assert hits[0]["source"] == "member"
    assert (state / "answers.jsonl").read_text().count("\n") == 1


def test_lookup_matches_reworded_question():
    bank.add_entry(GRID_Q, GRID_V)
    hits = bank.lookup("Highlight your familiarity as a customer of the following platforms")
    assert hits and hits[0]["value"] == GRID_V


def test_lookup_rejects_unrelated_question():
    bank.add_entry(GRID_Q, GRID_V)
    assert bank.lookup("What is your preferred call window next week?") == []


def test_lookup_orders_by_score_then_newest():
    bank.add_entry("Were you involved in the procurement process?", ["Influencer"])
    bank.add_entry("Were you involved in the procurement process?", ["Key Decision Maker"])
    hits = bank.lookup("Were you involved in the procurement process?", limit=1)
    assert hits[0]["value"] == ["Key Decision Maker"]


def test_seed_from_parsed_dedupes(state):
    parsed = {
        "kind": "accept_confirmation",
        "project_title": "Observability | Customers",
        "recorded_answers": [
            {"question": GRID_Q, "value": GRID_V, "note": ""},
            {"question": "Role?", "value": ["Key Decision Maker"], "note": "team"},
        ],
    }
    first = bank.seed_from_parsed(parsed)
    second = bank.seed_from_parsed(parsed)
    assert len(first["added"]) == 2
    assert second["added"] == []
    assert first["skipped"] == [] and second["skipped"] == []
    assert all(e["source"] == "glg_accept_confirmation" for e in bank.load_entries())
    assert bank.load_entries()[0]["project"] == "Observability | Customers"


def test_seed_rejects_wrong_kind():
    with pytest.raises(ValueError):
        bank.seed_from_parsed({"kind": "invitation", "recorded_answers": []})


def test_log_submission(state):
    bank.log_submission("Observability", "123", "submitted", "4 rows")
    lines = (state / "submissions.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["action"] == "submitted"


def test_cli_add_lookup_list_log(capsys):
    assert bank.main(["add", "--question", GRID_Q, "--answer", json.dumps(GRID_V)]) == 0
    capsys.readouterr()
    assert bank.main(["add", "--question", "Free text?", "--answer", "plain words"]) == 0
    capsys.readouterr()
    assert bank.main(["lookup", "--question", GRID_Q, "--limit", "1"]) == 0
    hit = json.loads(capsys.readouterr().out)[0]
    assert hit["value"] == GRID_V
    assert bank.main(["list"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert bank.main(["log", "--project", "P", "--action", "declined"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "declined"


def test_cli_seed(tmp_path, capsys):
    parsed = tmp_path / "parsed.json"
    parsed.write_text(json.dumps({
        "kind": "accept_confirmation",
        "project_title": "P",
        "recorded_answers": [{"question": GRID_Q, "value": GRID_V, "note": ""}],
    }))
    assert bank.main(["seed", "--from-parsed", str(parsed)]) == 0
    assert len(json.loads(capsys.readouterr().out)["added"]) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only permission bits")
def test_state_dir_and_data_files_are_owner_only(tmp_path, monkeypatch):
    """The bank holds employment, customer and conflict answers. On a shared
    box with the usual 022 umask these landed as 0644 in a 0755 directory,
    readable by every local user (PR #116 review)."""
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("os.umask", lambda mask: 0o022)

    bank.add_entry("Role in procurement", "Key Decision Maker")
    bank.log_submission(project="P", cpid="1", action="submitted")

    assert bank.state_dir().stat().st_mode & 0o777 == 0o700
    assert bank.answers_path().stat().st_mode & 0o777 == 0o600
    assert bank.log_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only permission bits")
def test_existing_loose_permissions_are_tightened(tmp_path, monkeypatch):
    base = tmp_path / "state"
    base.mkdir(mode=0o755)
    answers = base / "answers.jsonl"
    answers.write_text("", encoding="utf-8")
    answers.chmod(0o644)
    monkeypatch.setenv("GLGQ_STATE_DIR", str(base))

    bank.add_entry("Vendors evaluated", "Datadog")

    assert base.stat().st_mode & 0o777 == 0o700
    assert answers.stat().st_mode & 0o777 == 0o600


ATTESTATIONS = [
    "Are you currently employed by any of the companies listed above?",
    "Do you have a conflict of interest with the client for this project?",
    "Do you consent to this call being recorded?",
    "Please acknowledge the single-blinded client notice for this consultation.",
    "Who is your current employer?",
]


@pytest.mark.parametrize("question", ATTESTATIONS)
def test_attestations_cannot_be_added_to_the_bank(question, tmp_path, monkeypatch):
    """SKILL.md hard rule 3 and PRIVACY.md both say these go to the member every
    time. A stored copy would come back as a BANK suggestion (PR #116 review)."""
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))

    with pytest.raises(bank.NotReusable):
        bank.add_entry(question, "No")

    assert bank.load_entries() == []


def test_seed_skips_attestations_and_reports_them(tmp_path, monkeypatch):
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))
    parsed = {
        "kind": "accept_confirmation",
        "project_title": "Observability Platforms",
        "recorded_answers": [
            {"question": "Please highlight your familiarity with these platforms.",
             "value": {"Datadog": ["Current Paying Customer"]}, "note": ""},
            {"question": "Do you have a conflict of interest with the client?",
             "value": "No", "note": ""},
            {"question": "Do you consent to this call being recorded?",
             "value": "Yes", "note": ""},
        ],
    }

    result = bank.seed_from_parsed(parsed)

    added = result["added"] if isinstance(result, dict) else result
    skipped = result["skipped"] if isinstance(result, dict) else []
    assert [entry["value"] for entry in added] == [{"Datadog": ["Current Paying Customer"]}]
    assert len(skipped) == 2
    assert all("conflict" in s["question"].lower() or "recorded" in s["question"].lower() for s in skipped)
    assert len(bank.load_entries()) == 1


def test_cli_add_refuses_an_attestation_with_exit_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))

    rc = bank.main(["add", "--question", "Do you have a conflict of interest?", "--answer", '"No"'])

    out = capsys.readouterr()
    assert rc == 3
    assert "member" in (out.err + out.out).lower()
    assert bank.load_entries() == []


def test_an_ordinary_question_is_still_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))

    bank.add_entry("Which observability vendors have you evaluated?", ["Datadog", "Grafana"])

    assert len(bank.load_entries()) == 1


COMPLIANCE_QUESTIONS = [
    "Do you hold, or have you held, any government or public-sector role?",
    "Are you involved in any pending trial or litigation?",
    "Have you been employed by a publicly traded company within the last six months?",
    "Which other GLG projects have you participated in?",
    "Do you have access to material non-public information about this company?",
    "May we contact your current employer?",
]


@pytest.mark.parametrize("question", COMPLIANCE_QUESTIONS)
def test_every_class_in_compliance_md_is_refused(question, tmp_path, monkeypatch):
    """references/compliance.md lists the questions that always go to the
    member. The predicate must cover all of them, not just the first four: a
    reused answer here produces a false compliance attestation (PR #116
    review)."""
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))

    assert bank.is_attestation(question), question
    with pytest.raises(bank.NotReusable):
        bank.add_entry(question, "No")


def test_topical_questions_are_not_swept_up_by_the_compliance_patterns():
    """The guard must not refuse ordinary project questions; an over-broad
    predicate would push every answer to NEEDS MEMBER."""
    for question in (
        "Which observability vendors have you evaluated in the last two years?",
        "How many engineers does your organisation employ?",
        "What drove the decision to move off your previous government cloud vendor?",
        "Please rate your familiarity with these public cloud platforms.",
    ):
        assert not bank.is_attestation(question), question


@pytest.mark.parametrize("question", [
    "Which previous projects have you completed through GLG?",
    "Have you worked on similar projects via GLG before?",
    "How many consultations have you completed with GLG this year?",
    "Please list the prior engagements you have done for GLG.",
])
def test_prior_project_questions_are_refused_in_either_word_order(question, tmp_path, monkeypatch):
    """The first pass only matched "other GLG" and "GLG project(s)". GLG's
    confidentiality section covers the question however it is phrased (PR #116
    review)."""
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))

    assert bank.is_attestation(question), question
    with pytest.raises(bank.NotReusable):
        bank.add_entry(question, "None")


def test_a_fuzzy_match_needs_the_same_entity(tmp_path, monkeypatch):
    """Boilerplate dominates the similarity score: "current paying customer of
    Datadog" vs "... of Splunk" scores ~0.79, well over the 0.55 threshold, so
    an answer about one vendor was offered as BANK data for another (PR #116
    review)."""
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))
    bank.add_entry("Are you a current paying customer of Datadog?", "Yes")

    assert bank.lookup("Are you a current paying customer of Splunk?") == []
    assert bank.lookup("Are you currently using Azure?") == []

    hits = bank.lookup("Are you a current paying customer of Datadog?")
    assert hits and hits[0]["value"] == "Yes"


def test_an_overlapping_entity_still_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))
    bank.add_entry("How familiar are you with Datadog and Grafana?", ["both"])

    hits = bank.lookup("How familiar are you with Datadog?")

    assert hits and hits[0]["value"] == ["both"]


def test_log_submission_accepts_its_text_from_files(tmp_path, monkeypatch, capsys):
    """Project titles come from email subjects, so `--project "<title>"` is the
    same shell-interpolation sink as the subject and question ones (PR #116
    review)."""
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))
    project = tmp_path / "project.txt"
    project.write_text('Observability $(whoami) `id`\n', encoding="utf-8")
    detail = tmp_path / "detail.txt"
    detail.write_text("4 rows, 1 NEEDS MEMBER\n", encoding="utf-8")

    rc = bank.main([
        "log", "--project-file", str(project), "--detail-file", str(detail),
        "--cpid", "123", "--action", "submitted",
    ])

    assert rc == 0
    record = json.loads(capsys.readouterr().out)
    assert record["project"] == "Observability $(whoami) `id`"
    assert record["detail"] == "4 rows, 1 NEEDS MEMBER"


def test_handled_lists_projects_already_submitted_or_declined(tmp_path, monkeypatch):
    """Triage must be able to ask what it already did: an accept confirmation
    often has no cpid, so grouping mail by cpid cannot associate it with the
    earlier invitation and the same project gets offered again (PR #116
    review)."""
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))
    bank.log_submission(project="Observability Platforms", cpid="111", action="submitted")
    bank.log_submission(project="Cloud Security", cpid="222", action="declined")
    bank.log_submission(project="Observability Platforms", cpid="111", action="submitted")

    handled = bank.handled()

    assert {entry["project"] for entry in handled} == {"Observability Platforms", "Cloud Security"}
    by_cpid = {entry["cpid"]: entry for entry in handled}
    assert by_cpid["111"]["action"] == "submitted"
    assert by_cpid["222"]["action"] == "declined"
    # One row per project, newest wins, so triage can match on either key.
    assert len(handled) == 2


def test_handled_is_empty_without_a_log(tmp_path, monkeypatch):
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))
    assert bank.handled() == []


def test_handled_keys_by_consultation_id_when_present(tmp_path, monkeypatch):
    """Two consultations can share a project title. Keying by title made the
    newer one hide the older, and triage — told to match by title — could mark
    a never-handled consultation as done (PR #116 review)."""
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))
    bank.log_submission(project="Observability Platforms", cpid="111", action="submitted")
    bank.log_submission(project="Observability Platforms", cpid="222", action="declined")
    bank.log_submission(project="Observability Platforms", cpid="111", action="submitted")

    handled = bank.handled()

    by_cpid = {entry["cpid"]: entry["action"] for entry in handled}
    assert by_cpid == {"111": "submitted", "222": "declined"}


def test_handled_falls_back_to_the_title_without_an_id(tmp_path, monkeypatch):
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))
    bank.log_submission(project="Cloud Security", cpid=None, action="declined")
    bank.log_submission(project="Cloud Security", cpid=None, action="submitted")

    handled = bank.handled()

    assert len(handled) == 1
    assert handled[0]["project"] == "Cloud Security"
    assert handled[0]["action"] == "submitted"


def test_a_truncated_bank_line_does_not_hide_the_valid_ones(tmp_path, monkeypatch, capsys):
    """The bank is append-only and the file is documented, so an interrupted
    write or a hand edit can leave one bad line. That must not make every
    earlier answer unreadable (PR #116 review)."""
    monkeypatch.setenv("GLGQ_STATE_DIR", str(tmp_path))
    bank.add_entry("Which observability vendors have you evaluated?", ["Datadog"])
    with bank.answers_path().open("a", encoding="utf-8") as handle:
        handle.write('{"question": "truncated", "value":\n')
        handle.write('"a bare string, not an object"\n')

    entries = bank.load_entries()

    assert [entry["value"] for entry in entries] == [["Datadog"]]
    assert "skipped" in capsys.readouterr().err.lower()
    # And lookup still works over the surviving records.
    assert bank.lookup("Which observability vendors have you evaluated?")
