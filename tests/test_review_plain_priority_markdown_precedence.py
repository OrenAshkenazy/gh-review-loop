"""Regression cases for GH Review Loop #119 Codex review on commit 16dd2a6.

Covers Markdown indentation semantics and leading plain-text marker precedence.
"""

import pytest

import cluster_findings as cluster_mod
from fetch_gemini_threads import thread_severity, finding_fingerprint


def _thread(*bodies, graph=False):
    comments = [{"body": body} for body in bodies]
    return {
        "path": "src/example.py",
        "line": 12,
        "comments": {"nodes": comments} if graph else comments,
    }


@pytest.mark.parametrize("graph", [False, True])
@pytest.mark.parametrize("code", [
    "    [P0] This is Markdown code, not severity",
    "\t[P1] This is Markdown code, not severity",
    "\n    [P2] Indented code after a blank line",
    "  \n\t[P3] Indented code after a blank line",
])
def test_indented_code_priority_does_not_count(graph, code):
    thread = _thread(code, graph=graph)
    assert thread_severity(thread) == "unknown"
    assert cluster_mod._severity(thread) == "unknown"
    assert cluster_mod.cluster([thread])[0].severity == "unknown"


@pytest.mark.parametrize("graph", [False, True])
@pytest.mark.parametrize("priority,expected", [
    ("P0", "critical"),
    ("P1", "high"),
    ("P2", "medium"),
    ("P3", "low"),
])
def test_leading_plain_marker_outweighs_quoted_image(graph, priority, expected):
    body = f"[{priority}] Real problem; code sample uses ![low](untrusted.svg)"
    thread = _thread(body, graph=graph)
    assert thread_severity(thread) == expected
    assert cluster_mod._severity(thread) == expected
    assert cluster_mod.cluster([thread])[0].severity == expected


@pytest.mark.parametrize("graph", [False, True])
def test_later_comment_leading_priority_over_embedded_codex_badge(graph):
    thread = _thread(
        "No severity marked here",
        "[P0] Dangerous behavior, see ![P3 Badge](https://example.invalid/p3)",
        graph=graph,
    )
    assert thread_severity(thread) == "critical"
    assert cluster_mod._severity(thread) == "critical"
    assert cluster_mod.cluster([thread])[0].severity == "critical"


@pytest.mark.parametrize("graph", [False, True])
def test_up_to_three_spaces_and_blank_lines_are_supported(graph):
    body = "  \n   [p2] Reviewer prefix"
    thread = _thread(body, graph=graph)
    assert thread_severity(thread) == "medium"
    assert cluster_mod._severity(thread) == "medium"


@pytest.mark.parametrize("graph", [False, True])
def test_fingerprints_do_not_erase_indented_code_marker(graph):
    a = _thread("    [P2] Code section", graph=graph)
    b = _thread("    [P3] Code section", graph=graph)
    assert finding_fingerprint(a) != finding_fingerprint(b)
    assert cluster_mod.pattern_signature(a) != cluster_mod.pattern_signature(b)


@pytest.mark.parametrize("graph", [False, True])
def test_fingerprints_ignore_valid_leading_marker_changes(graph):
    a = _thread("[P2] Real issue", graph=graph)
    b = _thread("[P3] Real issue", graph=graph)
    assert finding_fingerprint(a) == finding_fingerprint(b)
    assert cluster_mod.pattern_signature(a) == cluster_mod.pattern_signature(b)


@pytest.mark.parametrize("graph", [False, True])
def test_image_only_priority_still_supported(graph):
    thread = _thread("![high](https://example.invalid/high.svg) Legacy Gemini format", graph=graph)
    assert thread_severity(thread) == "high"
    assert cluster_mod._severity(thread) == "high"
