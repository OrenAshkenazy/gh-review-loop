"""Bundle several exported profiles into one importable archive.

A bundle is unpacked into a directory of JSON files, one per section. Bundles
written by exporters before 0.3 stored each section as a bare JSON list rather
than an object. Readers accept both layouts: a legacy list carries nothing
this version can use, so it reads as an empty section.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_manifest(path: Path) -> dict[str, Any]:
    """Read the manifest listing every included profile ({} for a legacy bundle)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("manifest", {})


def read_lockfile(path: Path) -> list[str]:
    """Read the pinned plugin versions ([] for a legacy bundle)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("pinned", [])


def read_overrides(path: Path) -> dict[str, Any]:
    """Read per-developer overrides ({} for a legacy bundle)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("overrides", {})
