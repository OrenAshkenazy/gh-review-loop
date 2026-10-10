"""answer_bank.py: local, append-only store of member-authored answers."""
from __future__ import annotations

import json
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
    assert len(first) == 2
    assert second == []
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
