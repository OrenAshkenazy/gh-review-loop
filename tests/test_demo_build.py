"""The README demo is generated from the shipping formatters, not hand-written.

docs/demo/build_demo.py renders the GIF with Chrome and Pillow, which CI does
not have. Its transcript, though, is plain Python over the replay fixtures, so
it is built here: a formatter change that breaks the demo fails this test
instead of surfacing at the next rebuild.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_demo", REPO_ROOT / "docs/demo/build_demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_transcript_builds_from_the_real_formatters():
    builder = _load_builder()
    output = builder.loop_output()

    # The headline claim: two flagged sites, three unflagged siblings.
    assert output["severity"] == "2 medium", "Codex's P2 badges must be read"
    assert output["threads"] == 2
    assert output["siblings"] == 3
    assert any("3 unflagged site(s)" in line for line in output["sweep"])
    assert output["summary_pointer"].startswith("[loop] Summary")
    assert "outcome clean" in output["summary_pointer"]

    frames = builder.timeline(output)
    assert len(frames) > 20
    assert all(frame["ms"] > 0 for frame in frames)
    transcript = "".join(frames[-3]["items"])
    assert "3 unflagged site(s)" in transcript
    assert "Cycle receipt" in transcript
