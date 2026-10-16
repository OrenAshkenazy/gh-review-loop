"""Regression coverage for reviewer-leading text priorities (PR #119 follow-up)."""

import pytest

import cluster_findings
from fetch_gemini_threads import finding_fingerprint, thread_severity


def make_thread(bodies, graph=False):
    comments = [{"body": body} for body in bodies]
    return {
        "path": "src/runtime.py", "line": 18,
        "comments": {"nodes": comments} if graph else comments,
    }


@pytest.mark.parametrize("graph", [False, True])
@pytest.mark.parametrize("first,second,expected", [
    ("No priority here", "[P1] Protect production writes", "high"),
    ("No priority here", "![P2 Badge](https://img.shields.io/x) Add a guard", "medium"),
    ("No priority here", "![low](https://example.invalid/low.svg) Cosmetic", "low"),
])
def test_later_marked_comment_has_matching_severity(graph, first, second, expected):
    t = make_thread([first, second], graph)
    assert thread_severity(t) == expected
    assert cluster_findings._severity(t) == expected
    assert cluster_findings.cluster([t])[0].severity == expected


@pytest.mark.parametrize("graph", [False, True])
def test_unknown_when_no_comment_has_priority(graph):
    t = make_thread(["Ordinary text", "Mentions [P2] in the middle"], graph)
    assert thread_severity(t) == "unknown"
    assert cluster_findings._severity(t) == "unknown"


@pytest.mark.parametrize("graph", [False, True])
@pytest.mark.parametrize("a,b", [("P0", "P1"), ("P1", "P2"), ("P2", "P3")])
def test_fingerprint_ignores_leading_priority_changes(graph, a, b):
    ta = make_thread([f"[{a}] Retain the safety invariant"], graph)
    tb = make_thread([f"[{b}] Retain the safety invariant"], graph)
    assert finding_fingerprint(ta) == finding_fingerprint(tb)


@pytest.mark.parametrize("graph", [False, True])
def test_fingerprint_preserves_incidental_marker(graph):
    a = make_thread(["Mention [P2] in the explanation"], graph)
    b = make_thread(["Mention [P3] in the explanation"], graph)
    assert finding_fingerprint(a) != finding_fingerprint(b)


def test_no_fingerprint_change_for_codex_or_gemini_badge():
    assert finding_fingerprint(make_thread(["![P1 Badge](https://x/p1) Keep behavior"])) == finding_fingerprint(make_thread(["![P2 Badge](https://x/p2) Keep behavior"]))
    assert finding_fingerprint(make_thread(["![high](https://x/h) Keep behavior"])) == finding_fingerprint(make_thread(["![low](https://x/l) Keep behavior"]))


def test_cluster_malformed_comments_fail_closed():
    assert cluster_findings._severity({"comments": [None, "invalid", {"body": "[P2] Valid"}]}) == "medium"
    assert cluster_findings._severity({"comments": None}) == "unknown"
