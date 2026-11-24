"""Bundle several exported profiles into one importable archive.

A bundle is unpacked into a directory of JSON files. Every file is optional:
a bundle exported before a section existed simply does not contain it, so each
reader returns an empty value when its file is absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_manifest(path: Path) -> dict[str, Any]:
    """Read the manifest listing every included profile, or {} when absent."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("manifest", {})


def read_lockfile(path: Path) -> list[str]:
    """Read the pinned plugin versions, or [] for a bundle with no lockfile."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("pinned", [])


def read_overrides(path: Path) -> dict[str, Any]:
    """Read per-developer overrides, or {} when the developer set none."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("overrides", {})
