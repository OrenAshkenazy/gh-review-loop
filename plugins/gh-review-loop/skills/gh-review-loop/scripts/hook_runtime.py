"""Runtime helpers shared by Claude and Codex hook entrypoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_hook_payload(raw: str) -> dict[str, Any]:
    """Parse a hook JSON payload, failing open to an empty dict."""
    try:
        payload = json.loads(raw or "{}")
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def tool_name(payload: dict[str, Any]) -> str:
    """Return the invoked tool name across known hook payload shapes."""
    for key in ("tool_name", "toolName", "name"):
        value = payload.get(key)
        if isinstance(value, str):
            return value

    tool = payload.get("tool")
    if isinstance(tool, str):
        return tool
    if isinstance(tool, dict):
        for key in ("name", "tool_name", "toolName"):
            value = tool.get(key)
            if isinstance(value, str):
                return value
    return ""


def tool_command(payload: dict[str, Any]) -> str:
    """Return a Bash command string across known hook payload shapes."""
    for key in ("tool_input", "toolInput", "input", "arguments"):
        value = payload.get(key)
        if isinstance(value, dict) and isinstance(value.get("command"), str):
            return value["command"]
    value = payload.get("command")
    return value if isinstance(value, str) else ""


def hook_cwd(payload: dict[str, Any]) -> str | None:
    """The session's working directory the hook payload reports, if any."""
    value = payload.get("cwd")
    return value if isinstance(value, str) and value else None


def script_path(script_name: str) -> Path:
    """Return an absolute path to a sibling script in this plugin install."""
    return Path(__file__).resolve().with_name(script_name)


def python_script_command(script_name: str) -> str:
    """Return a quoted python command for user-facing hook remediation text."""
    return f'python3 "{script_path(script_name)}"'
