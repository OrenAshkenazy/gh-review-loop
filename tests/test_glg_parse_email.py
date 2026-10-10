"""parse_glg_email.py: GLG email text -> structured JSON (synthetic fixtures)."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "glg-questionnaire" / "skills" / "glg-questionnaire" / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "glg"
sys.path.insert(0, str(SCRIPTS))

import parse_glg_email as pge  # noqa: E402


def fake_jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"iat": exp - 1000, "exp": exp}).encode())
    return "hdr." + payload.decode().rstrip("=") + ".sig"


EXP = 1792578603  # 2026-10-21T10:30:03Z


def fixture(name: str) -> str:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return text.replace("__SESSION__", fake_jwt(EXP)).replace("__CONSULT__", "consult.jwt")


class TestInvitation:
    @pytest.fixture
    def parsed(self):
        return pge.parse(fixture("invitation.txt"), "Developer Observability consultation - GLG")

    def test_kind_and_title(self, parsed):
        assert parsed["kind"] == "invitation"
        assert parsed["project_title"] == "Developer Observability"

    def test_links_and_ids(self, parsed):
        roles = {link["role"] for link in parsed["links"]}
        assert roles == {"accept", "decline"}
        assert parsed["cpid"] == "198515470"
        assert parsed["consultation_ids"] == "consult.jwt"
        assert parsed["accept_link_expires_at"] == "2026-10-21T10:30:03+00:00"

    def test_rate_client_manager(self, parsed):
        assert parsed["hourly_rate_usd"] == 400
        assert parsed["client_type"] == "associate at an investment management firm"
        assert parsed["research_manager_email"] == "jexample@glgroup.com"

    def test_flags(self, parsed):
        flags = parsed["flags"]
        assert flags["single_blinded"] is True
        assert flags["recorded"] is True
        assert flags["ai_use_prohibited"] is True
        assert flags["urgent"] is False
        assert flags["client_terms_appended"] is False

    def test_description_bullets_join_wrapped_lines(self, parsed):
        assert parsed["description_bullets"] == [
            "Thoughts on the importance of binaries in an agentic world",
            (
                "Views on what would make the labs build their own registry – "
                "thoughts on why and why not?"
            ),
        ]

    def test_non_glg_links_are_dropped(self, parsed):
        assert all("glginsights.go.link" not in link["url"] for link in parsed["links"])


class TestOtherKinds:
    def test_followup(self):
        parsed = pge.parse(
            fixture("followup.txt"),
            "GLG: Follow up on your acceptance for Cloud Security Platforms | Customers",
        )
        assert parsed["kind"] == "followup"
        assert parsed["cpid"] == "193913672"
        assert parsed["project_title"] == "Cloud Security Platforms | Customers"
        assert parsed["accept_link_expires_at"] is not None

    def test_accept_confirmation_answers(self):
        parsed = pge.parse(
            fixture("accept_confirmation.txt"),
            "GLG: Accept Confirmation – Observability Software Platforms | Customer Perspective",
        )
        assert parsed["kind"] == "accept_confirmation"
        assert parsed["project_title"] == "Observability Software Platforms | Customer Perspective"
        answers = parsed["recorded_answers"]
        assert len(answers) == 4
        assert answers[0]["value"]["Datadog"] == ["5 - Very Familiar"]
        assert answers[1]["note"] == "Groundcover - design partner to customise it for the organisation"
        assert answers[2]["value"] == ["Key Decision Maker"]
        assert answers[2]["note"] == "Part of the decision making team"
        assert answers[3]["value"]["When was your last RFP for these software tools?"] == ["6 months ago"]
        assert parsed["links"] == []

    @pytest.mark.parametrize(
        "subject, kind, title",
        [
            ("GLG - *URGENT* new project on AI Agent Security Software | Customers", "invitation",
             "AI Agent Security Software | Customers"),
            ("Please take this Paid Survey", "survey", None),
            ("GLG: Reminder - Survey in Progress", "survey", None),
            ("Your GLG Payment is on its way", "payment", None),
            ("REMINDER: Your GLG call will begin in 30 minutes", "call_reminder", None),
            ("GLG: Call requested on 'JFrog | Customers'", "call_request", "JFrog | Customers"),
            ("GLG Consultation with our client on 'App Sec' (Project ID 5298597)", "call", "App Sec"),
            ("You have open GLG projects waiting", "digest", None),
            ("In case you missed it, we have a GLG project for you", "invitation", None),
            ("GLG - We Think You’re a Fit to Discuss \"Database Monitoring | Customers\"", "invitation",
             "Database Monitoring | Customers"),
        ],
    )
    def test_subject_only_classification(self, subject, kind, title):
        parsed = pge.parse("Hi Oren, nothing structured here.", subject)
        assert parsed["kind"] == kind
        assert parsed["project_title"] == title

    def test_project_id_from_subject(self):
        parsed = pge.parse("body", "GLG Consultation with our client on 'X' (Project ID 5298597)")
        assert parsed["project_id"] == "5298597"

    def test_urgent_flag_and_body_title_fallback(self):
        body = "Specifically, they're looking to speak with customers of AI Agent Security Software ."
        parsed = pge.parse(body, "GLG - *URGENT* new project")
        assert parsed["flags"]["urgent"] is True
        assert parsed["project_title"] == "AI Agent Security Software"


class TestCli:
    def test_cli_reads_file_and_prints_json(self, tmp_path, capsys):
        body = tmp_path / "body.txt"
        body.write_text(fixture("followup.txt"), encoding="utf-8")
        rc = pge.main(["--file", str(body), "--subject", "GLG: Follow up on your acceptance for X"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["kind"] == "followup"

    def test_cli_rejects_empty_body(self, monkeypatch, capsys):
        import io
        monkeypatch.setattr(sys, "stdin", io.StringIO("   "))
        assert pge.main([]) == 2
        assert "empty" in capsys.readouterr().err

    def test_jwt_without_exp_is_none(self):
        bad = "hdr." + base64.urlsafe_b64encode(b'{"iat": 1}').decode().rstrip("=") + ".sig"
        assert pge._jwt_expiry(bad) is None
        assert pge._jwt_expiry("not-a-jwt") is None


class TestLinkHostTrust:
    """A link is only actionable when its host *is* GLG.

    The parser's links are opened in the member's browser, so a lookalike host
    in a forwarded or spoofed email must never be classified as accept,
    followup or decline (PR #116 review).
    """

    @pytest.mark.parametrize("url", [
        "https://members.glgresearch.com.attacker.example/accept/accept?cpid=1",
        "https://attacker.example/members.glgresearch.com/accept/accept?cpid=1",
        "https://members.glgresearch.com@attacker.example/accept/accept?cpid=1",
        "https://members.glgresearch.com.evil.test/cm-accept-followup/x",
        "https://notmembers.glgresearch.com.co/opportunities?consultationIds=a",
        "ftp://members.glgresearch.com/accept/accept?cpid=1",
    ])
    def test_lookalike_hosts_are_not_actionable(self, url):
        assert pge.classify_link(url) == "other"
        assert pge.extract_links(f"Review Project Now: {url}") == []

    @pytest.mark.parametrize("url, role", [
        ("https://members.glgresearch.com/accept/accept?cpid=1", "accept"),
        ("https://MEMBERS.GLGRESEARCH.COM/accept/accept?cpid=1", "accept"),
        ("https://members.glgresearch.com./accept/accept?cpid=1", "accept"),
        ("https://members.glgresearch.com/cm-accept-followup/abc", "followup"),
        ("https://members.glgresearch.com/opportunities?consultationIds=abc", "decline"),
        ("https://members.glgresearch.com/profile", "members"),
    ])
    def test_real_glg_links_keep_their_role(self, url, role):
        assert pge.classify_link(url) == role

    def test_opportunities_without_consultation_ids_is_not_a_decline(self):
        assert pge.classify_link("https://members.glgresearch.com/opportunities") == "members"


class TestMalformedUrls:
    """One malformed URL must not take the whole email down.

    The link regex hands every URL in the body to classify_link, so a single
    `https://[foo` would raise ValueError out of urlsplit and abort triage for
    an email that also carries a valid accept link (PR #116 review).
    """

    @pytest.mark.parametrize("url", [
        "https://[foo",
        "https://members.glgresearch.com:99999/accept/accept",
        "http://[::1zz]/accept/accept",
    ])
    def test_malformed_urls_classify_as_other(self, url):
        assert pge.classify_link(url) == "other"

    def test_a_valid_link_survives_a_malformed_one_in_the_same_email(self):
        good = "https://members.glgresearch.com/accept/accept?cpid=198515470"
        body = f"Broken: https://[foo\nReview Project Now: {good}\n"

        links = pge.extract_links(body)

        assert [link["role"] for link in links] == ["accept"]
        assert links[0]["url"] == good


class TestPlaintextLinksAreRejected:
    """GLG action URLs carry a `_session` bearer token in the query string, so
    an `http://` variant would leak it on the first request. Only https links
    get an actionable role (PR #116 review)."""

    @pytest.mark.parametrize("path", [
        "/accept/accept?cpid=1",
        "/cm-accept-followup/abc",
        "/opportunities?consultationIds=abc",
        "/profile",
    ])
    def test_http_links_are_not_actionable(self, path):
        assert pge.classify_link(f"http://members.glgresearch.com{path}") == "other"
        assert pge.classify_link(f"https://members.glgresearch.com{path}") != "other"

    def test_a_plaintext_link_is_dropped_from_extract_links(self):
        body = (
            "Old link: http://members.glgresearch.com/accept/accept?cpid=1\n"
            "Review Project Now: https://members.glgresearch.com/accept/accept?cpid=2\n"
        )

        links = pge.extract_links(body)

        assert [link["cpid"] for link in links] == ["2"]


class TestRobustKindAndExpiry:
    def test_an_out_of_range_jwt_expiry_does_not_abort_parsing(self):
        """`datetime.fromtimestamp()` raises OverflowError (OSError on some
        platforms) for an absurd `exp`. The link is still a link; only its
        expiry is unknown (PR #116 review)."""
        huge = base64.urlsafe_b64encode(json.dumps({"exp": 10 ** 20}).encode()).decode().rstrip("=")
        url = f"https://members.glgresearch.com/accept/accept?cpid=7&_session=hdr.{huge}.sig"

        assert pge._jwt_expiry(f"hdr.{huge}.sig") is None

        links = pge.extract_links(f"Review Project Now: {url}")
        assert [link["role"] for link in links] == ["accept"]
        assert links[0]["expires_at"] is None

    def test_a_followup_link_beats_quoted_qa_text(self):
        """A reply quoting an earlier confirmation carries Q:/A: lines. The
        actionable signal is the follow-up link, not the quoted text, or the
        loop seeds stale answers and misses an open follow-up (PR #116
        review)."""
        body = (
            "Please answer these: https://members.glgresearch.com/cm-accept-followup/abc?cpid=5\n"
            "\n> On an earlier project you told us:\n"
            "Q: Familiarity with Datadog?\nA: \"Very Familiar\"\n"
        )

        parsed = pge.parse(body, "GLG: Follow up on your acceptance for Observability")

        assert parsed["kind"] == "followup"

    def test_a_survey_subject_beats_quoted_qa_text(self):
        body = "Q: Familiarity with Datadog?\nA: \"Very Familiar\"\n"

        parsed = pge.parse(body, "GLG: Paid survey opportunity on Observability")

        assert parsed["kind"] == "survey"

    def test_a_real_accept_confirmation_is_still_detected(self):
        parsed = pge.parse(fixture("accept_confirmation.txt"),
                           "GLG: Accept Confirmation – Cloud Security Platforms")

        assert parsed["kind"] == "accept_confirmation"
        assert parsed["recorded_answers"]


class TestSurveyPrecedence:
    def test_a_survey_subject_outranks_a_quoted_followup_link(self):
        """A survey email quoting an earlier GLG thread carries a follow-up
        URL. Routing on that link sends a paid survey into questionnaire
        automation, which hard rule 4 forbids (PR #116 review)."""
        body = (
            "Take our paid survey.\n\n"
            "> Earlier: https://members.glgresearch.com/cm-accept-followup/abc?cpid=5\n"
        )

        parsed = pge.parse(body, "GLG: Paid survey on Observability Platforms")

        assert parsed["kind"] == "survey"

    def test_a_payment_subject_outranks_a_quoted_followup_link(self):
        body = "> https://members.glgresearch.com/cm-accept-followup/abc?cpid=5\n"

        parsed = pge.parse(body, "GLG: Payment for your consultation")

        assert parsed["kind"] == "payment"

    def test_a_real_followup_is_still_a_followup(self):
        body = "Please answer: https://members.glgresearch.com/cm-accept-followup/abc?cpid=5\n"

        parsed = pge.parse(body, "GLG: Follow up on your acceptance for Observability")

        assert parsed["kind"] == "followup"
