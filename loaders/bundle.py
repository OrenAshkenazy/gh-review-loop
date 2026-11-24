"""Bundle several exported profiles into one importable archive."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_manifest(path: Path) -> dict[str, Any]:
    """Read the bundle manifest that lists every included profile."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("manifest", {})


def read_lockfile(path: Path) -> list[str]:
    """Read the pinned plugin versions the bundle was exported against."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("pinned", [])


def read_overrides(path: Path) -> dict[str, Any]:
    """Read per-developer overrides layered on top of the bundle."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("overrides", {})
