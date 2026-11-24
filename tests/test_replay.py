"""The deterministic sweep replay must not drift silently.

evals/replay/ is published evidence: it is what someone reads to check that the
sibling sweep does what the README says. If a change to the clusterer, the
sweep, or the fixtures alters that output, this test fails and the new output
has to be looked at and committed deliberately.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIR = REPO_ROOT / "evals" / "replay"
REPLAY = REPLAY_DIR / "replay.py"
GOLDEN = REPLAY_DIR / "expected_output.txt"


def run_replay() -> str:
    proc = subprocess.run(
        [sys.executable, str(REPLAY)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"replay.py failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


class TestReplayIsStable:
    def test_output_matches_the_committed_golden(self):
        actual = run_replay()
        expected = GOLDEN.read_text(encoding="utf-8")
        assert actual == expected, (
            "evals/replay output changed. If the change is intended, refresh it "
            "with: python3 evals/replay/replay.py > evals/replay/expected_output.txt"
        )

    def test_replay_is_deterministic_across_invocations(self):
        assert run_replay() == run_replay()

    def test_no_machine_specific_paths_in_output(self):
        """The golden has to mean the same thing on someone else's machine."""
        actual = run_replay()
        for marker in ("/Users/", "/home/", "/private/", "/var/folders"):
            assert marker not in actual, f"absolute path {marker!r} leaked into output"


class TestReplayShowsWhatTheReadmeClaims:
    """Pin the two claims the README makes, independently of exact formatting."""

    def test_run1_merges_into_one_cluster_and_sweeps(self):
        out = run_replay()
        run1 = out.split("run2:")[0]
        assert "Patterns (2):" in run1, "prose alone must split the two findings"
        assert "Patterns (1):" in run1, "shape must merge them into one"
        assert "sig: shape:" in run1, "the merged cluster carries a shape signature"
        assert "siblings:  3 unflagged site(s) match the same shape" in run1

    def test_run2_is_one_site_and_correctly_does_not_sweep(self):
        out = run_replay()
        run2 = out.split("run2:")[1].split("codex:")[0]
        assert "1 site " in run2
        assert "nothing to sweep" in run2
        assert "[sweep]" not in run2

    def test_codex_capture_merges_two_failures_into_one_shape_and_sweeps(self):
        """The README demo is built from this capture; pin what it shows."""
        out = run_replay()
        codex = out.split("codex:")[1]
        assert "Patterns (2):" in codex, "prose alone must split the two findings"
        assert "[medium]" in codex, "Codex's P2 badge must be read as medium"
        assert "2 sites   (sig: shape:" in codex, "shape must merge them into one"
        assert "siblings:  3 unflagged site(s) match the same shape" in codex


class TestFixturesAreRealCapturedData:
    def test_fixtures_carry_provenance_and_verbatim_bodies(self):
        for name, count in (("run1", 2), ("run2", 1)):
            payload = json.loads(
                (REPLAY_DIR / "fixtures" / f"{name}.json").read_text(encoding="utf-8")
            )
            assert payload["source"].endswith("/pull/67")
            assert len(payload["commit"]) == 40
            assert len(payload["threads"]) == count
            for thread in payload["threads"]:
                comment = thread["comments"][0]
                assert comment["author"]["login"] == "sourcery-ai"
                assert comment["body"].strip(), "body must be kept verbatim, not elided"
                assert comment["url"].startswith("https://github.com/")

    def test_codex_fixture_carries_provenance_and_verbatim_bodies(self):
        payload = json.loads(
            (REPLAY_DIR / "fixtures" / "codex.json").read_text(encoding="utf-8")
        )
        assert payload["source"].endswith("/pull/129")
        assert len(payload["commit"]) == 40
        assert len(payload["threads"]) == 2
        for thread in payload["threads"]:
            comment = thread["comments"][0]
            assert comment["author"]["login"] == "chatgpt-codex-connector"
            assert "P2 Badge" in comment["body"], "the severity badge must be kept verbatim"
            assert comment["url"].startswith("https://github.com/")

    def test_vendored_source_is_present_so_replay_needs_no_branch(self):
        """The demo branches may be reset or deleted; replay must still work."""
        for root in ("src", "src-codex"):
            for name in ("profiles.py", "bundle.py"):
                src = REPLAY_DIR / "fixtures" / root / "loaders" / name
                assert src.exists()
                assert 'json.loads(path.read_text(encoding="utf-8"))' in src.read_text(
                    encoding="utf-8"
                )
