"""Real Windows processes must preserve Unicode when their output is redirected."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import judge


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows redirected-stream encoding")
SCRIPTS = Path(__file__).resolve().parent.parent / "plugins/gh-review-loop/skills/gh-review-loop/scripts"


def env_with_legacy_encoding():
    return {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}


def test_fetch_profile_output_preserves_unicode_in_a_real_process():
    judge.save_profile("owner/repo", source="confirmed", checks=[{"name": "tests ✓", "command": [sys.executable, "-V"], "required": True}])
    result = subprocess.run([sys.executable, str(SCRIPTS / "fetch_gemini_threads.py"), "--planned-verification", "--repo", "owner/repo"], capture_output=True, env=env_with_legacy_encoding(), timeout=20)
    assert result.returncode == 0, result.stderr
    assert "tests ✓" in result.stdout.decode("utf-8")


def test_rereview_dry_run_preserves_unicode_without_github_writes():
    result = subprocess.run([sys.executable, str(SCRIPTS / "request_rereview.py"), "--repo", "owner/repo", "--pr", "1", "--phrase", "review ✓", "--dry-run", "--no-cap-check"], capture_output=True, env=env_with_legacy_encoding(), timeout=20)
    assert result.returncode == 0, result.stderr
    assert "review ✓" in result.stdout.decode("utf-8")
