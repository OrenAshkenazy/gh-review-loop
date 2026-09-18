"""Reviewer profile import/export.

Lets a team share verification profiles and reviewer selections between
machines instead of each developer re-running detection on first use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_profile(path: Path) -> dict[str, Any]:
    """Read one exported verification profile."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("profile", {})


def load_reviewer_map(path: Path) -> dict[str, str]:
    """Read the repo -> reviewer login map."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("reviewers", {})


def load_team_defaults(path: Path) -> dict[str, Any]:
    """Read team-wide loop settings shipped alongside the profiles."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("defaults", {})
