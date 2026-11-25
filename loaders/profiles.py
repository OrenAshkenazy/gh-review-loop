"""Reviewer profile import/export.

Lets a team share verification profiles and reviewer selections between
machines instead of each developer re-running detection on first use.

Every file here is optional. A machine that has never imported anything has
none of them, so each loader returns an empty value when its file is absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_optional_json(path: Path) -> dict[str, Any]:
    """Decode an optional JSON file; an absent file reads as an empty object."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return json.loads(text)


def load_profile(path: Path) -> dict[str, Any]:
    """Read one exported verification profile, or {} when none was exported."""
    return _read_optional_json(path).get("profile", {})


def load_reviewer_map(path: Path) -> dict[str, str]:
    """Read the repo -> reviewer login map, or {} when none was shared."""
    return _read_optional_json(path).get("reviewers", {})


def load_team_defaults(path: Path) -> dict[str, Any]:
    """Read team-wide loop settings, or {} when the team ships none."""
    return _read_optional_json(path).get("defaults", {})
