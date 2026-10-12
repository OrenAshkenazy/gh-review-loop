#!/usr/bin/env python3
"""Execute a verification profile's checks and compute the required-gate.

Each check ``command`` is an argv list or a host-quoted string, run WITHOUT a shell, with
``cwd`` set to the profile's ``working_directory`` and a wall-clock timeout.
The gate fails iff any ``required`` check fails or times out; non-required
failures are recorded but non-gating.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


_TAIL_LINES = 20


def command_argv(command: str | list[str]) -> list[str]:
    """Accept literal argument arrays; parse strings using the host's quoting."""
    if isinstance(command, list):
        if not command or not all(isinstance(arg, str) and "\0" not in arg for arg in command):
            raise ValueError("command must contain string arguments")
        return list(command)
    if not isinstance(command, str) or "\0" in command:
        raise ValueError("invalid command")
    if os.name != "nt":
        return shlex.split(command)
    # CommandLineToArgvW preserves Windows paths and embedded escaped quotes.
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not command.strip():
        return []
    # Windows accepts unclosed quotes; reject them without POSIX path parsing.
    quoted = False
    slashes = 0
    for char in command:
        if char == '"' and slashes % 2 == 0:
            quoted = not quoted
        slashes = slashes + 1 if char == "\\" else 0
    if quoted:
        raise ValueError("unclosed command quote")
    count = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(command.strip(), ctypes.byref(count))
    if not argv:
        raise ValueError("invalid Windows command")
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        kernel32.LocalFree(argv)


def _output_tail(text: str) -> str:
    """Last _TAIL_LINES lines of *text*, stripped of trailing whitespace."""
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-_TAIL_LINES:])


def _reason_code(status: str, returncode: int | None, stdout: str, stderr: str) -> str:
    """Classify a check outcome into a short machine-readable reason code."""
    if status == "timeout":
        return "timeout"
    if returncode is None:
        return "command_not_found"
    if returncode == 0:
        return "ok"
    # pytest exit codes (https://docs.pytest.org/en/stable/reference/exit-codes.html)
    if returncode == 5:
        return "no_tests_collected"
    if returncode == 4:
        return "usage_error"
    if returncode == 2:
        return "interrupted"
    combined = stdout + stderr
    if "ImportError" in combined or "ModuleNotFoundError" in combined:
        return "import_error"
    return f"exit_{returncode}"


@dataclasses.dataclass
class CheckResult:
    name: str
    command: str
    required: bool
    status: str  # "passed" | "failed" | "timeout"
    returncode: int | None
    duration_s: float
    # Populated only for non-passing checks to keep serialised output compact.
    stdout_tail: str = ""
    stderr_tail: str = ""
    reason_code: str = ""


@dataclasses.dataclass
class ProfileRunResult:
    verification: str  # "passed" | "failed"
    checks: list[CheckResult]
    failed_required: list[str]

    def to_details(self) -> dict[str, Any]:
        """JSON-serializable shape for --verification-details."""
        return {
            "verification": self.verification,
            # failed_check: first failed check name — consumed by format_run_summary.
            "failed_check": self.failed_required[0] if self.failed_required else None,
            "failed_required": self.failed_required,
            "checks": [dataclasses.asdict(c) for c in self.checks],
        }


def _run_one(check: Any, cwd: Path, timeout: int) -> CheckResult:
    start = time.monotonic()
    # A profile can be hand-edited or corrupted; never trust the shape.
    if not isinstance(check, dict):
        return CheckResult("<invalid>", repr(check), True, "failed", None,
                           time.monotonic() - start, reason_code="invalid_check")
    name = str(check.get("name", "<unnamed>"))
    command = check.get("command")
    required = bool(check.get("required", True))
    if not isinstance(command, (str, list)):
        return CheckResult(name, str(command), required, "failed", None,
                           time.monotonic() - start, reason_code="invalid_command")
    try:
        argv = command_argv(command)
    except ValueError:
        # Malformed/unclosed quotes in a hand-edited command.
        return CheckResult(name, command, required, "failed", None,
                           time.monotonic() - start, reason_code="invalid_command")
    if not argv:
        return CheckResult(name, command, required, "failed", None,
                           time.monotonic() - start, reason_code="invalid_command")
    if isinstance(command, list):
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.run(  # noqa: S603 - command is user-confirmed, no shell
            argv,
            cwd=str(cwd),
            timeout=timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(name, command, required, "timeout", None,
                           time.monotonic() - start, reason_code="timeout")
    except (FileNotFoundError, OSError, ValueError):
        # ValueError covers bad subprocess args and UnicodeDecodeError
        # (a ValueError subclass), which the OSError clause would miss.
        return CheckResult(name, command, required, "failed", None,
                           time.monotonic() - start, reason_code="command_not_found")
    status = "passed" if proc.returncode == 0 else "failed"
    if status == "passed":
        return CheckResult(name, command, required, status, proc.returncode,
                           time.monotonic() - start, reason_code="ok")
    return CheckResult(
        name, command, required, status, proc.returncode,
        time.monotonic() - start,
        stdout_tail=_output_tail(proc.stdout),
        stderr_tail=_output_tail(proc.stderr),
        reason_code=_reason_code(status, proc.returncode, proc.stdout, proc.stderr),
    )


def run_profile(profile: Any, repo_root: Path | str) -> ProfileRunResult:
    """Run all checks in ``profile`` rooted at ``repo_root``; compute the gate.

    The profile may have been hand-edited in preferences.json, so every field
    is validated/coerced before use rather than trusted.
    """
    root = Path(repo_root)
    if not isinstance(profile, dict):
        profile = {}
    working_directory = profile.get("working_directory", ".")
    if not isinstance(working_directory, str):
        working_directory = "."
    cwd = root / working_directory
    try:
        timeout = int(profile.get("timeout_seconds", 300))
    except (TypeError, ValueError):
        timeout = 300
    if timeout <= 0:
        # subprocess.run treats 0/negative as an immediate/invalid timeout.
        timeout = 300
    checks = profile.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    results = []
    for c in checks:
        check_cwd = cwd
        if isinstance(c, dict):
            override = c.get("working_directory")
            if isinstance(override, str) and override:
                check_cwd = root / override
        results.append(_run_one(c, check_cwd, timeout))
    failed_required = [
        c.name for c in results if c.required and c.status != "passed"
    ]
    if not any(c.required for c in results):
        failed_required.append("<missing required checks>")
    verification = "failed" if failed_required else "passed"
    return ProfileRunResult(verification, results, failed_required)


def main(argv: list[str]) -> int:
    """CLI: run_profile.py <owner/repo> <repo_root>.

    Loads the saved profile for the repo, runs it, prints to_details() JSON,
    and exits 0 if verification passed / 1 if it failed. A missing, skipped,
    or empty profile prints a 'skipped' result and exits 0 (the loop falls
    back to ad-hoc verification).
    """
    if len(argv) < 3:
        print("usage: run_profile.py <owner/repo> <repo_root>", file=sys.stderr)
        return 2
    repo, repo_root = argv[1], argv[2]
    try:
        from judge import get_profile  # noqa: PLC0415
    except ImportError:
        print(json.dumps({"verification": "skipped",
                          "reason": "judge module unavailable"}))
        return 0
    profile = get_profile(repo)
    if (
        not isinstance(profile, dict)
        or profile.get("source") == "skipped"
        or not isinstance(profile.get("checks"), list)
        or not profile.get("checks")
    ):
        print(json.dumps({"verification": "skipped",
                          "reason": "no runnable profile"}))
        return 0
    result = run_profile(profile, repo_root)
    print(json.dumps(result.to_details(), indent=2))
    return 0 if result.verification == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
