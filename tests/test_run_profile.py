from __future__ import annotations

from run_profile import run_profile


def _profile(checks, working_directory=".", timeout_seconds=300):
    return {
        "checks": checks,
        "working_directory": working_directory,
        "timeout_seconds": timeout_seconds,
    }


def test_all_required_pass_gate_passes(tmp_path):
    prof = _profile([
        {"name": "ok", "command": "python3 -c \"import sys; sys.exit(0)\"",
         "required": True},
    ])
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"
    assert result.failed_required == []
    assert result.checks[0].status == "passed"


def test_required_failure_fails_gate(tmp_path):
    prof = _profile([
        {"name": "boom", "command": "python3 -c \"import sys; sys.exit(1)\"",
         "required": True},
    ])
    result = run_profile(prof, tmp_path)
    assert result.verification == "failed"
    assert result.failed_required == ["boom"]


def test_optional_failure_does_not_fail_gate(tmp_path):
    prof = _profile([
        {"name": "tests", "command": "python3 -c \"import sys; sys.exit(0)\"",
         "required": True},
        {"name": "types", "command": "python3 -c \"import sys; sys.exit(1)\"",
         "required": False},
    ])
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"
    assert result.failed_required == []
    statuses = {c.name: c.status for c in result.checks}
    assert statuses == {"tests": "passed", "types": "failed"}


def test_required_timeout_fails_gate(tmp_path):
    prof = _profile(
        [{"name": "slow", "command": "python3 -c \"import time; time.sleep(5)\"",
          "required": True}],
        timeout_seconds=1,
    )
    result = run_profile(prof, tmp_path)
    assert result.verification == "failed"
    assert result.checks[0].status == "timeout"
    assert result.failed_required == ["slow"]


def test_working_directory_is_honored(tmp_path):
    sub = tmp_path / "backend"
    sub.mkdir()
    (sub / "marker.txt").write_text("x")
    prof = _profile(
        [{"name": "find", "command": "python3 -c \"import os,sys; sys.exit(0 "
          "if os.path.exists('marker.txt') else 1)\"", "required": True}],
        working_directory="backend",
    )
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"


def test_to_details_dict_is_json_serializable(tmp_path):
    import json
    prof = _profile([
        {"name": "ok", "command": "python3 -c \"import sys; sys.exit(0)\"",
         "required": True},
    ])
    result = run_profile(prof, tmp_path)
    json.dumps(result.to_details())  # must not raise
    assert result.to_details()["verification"] == "passed"


def test_empty_command_is_failed_not_crash(tmp_path):
    prof = _profile([{"name": "blank", "command": "   ", "required": True}])
    result = run_profile(prof, tmp_path)
    assert result.verification == "failed"
    assert result.checks[0].status == "failed"
    assert result.failed_required == ["blank"]


# --- Corrupt / hand-edited profile robustness (Gemini review #30) -------------

_OK = 'python3 -c "import sys; sys.exit(0)"'


def test_non_dict_profile_does_not_crash(tmp_path):
    result = run_profile("not a profile", tmp_path)
    assert result.verification == "failed"  # no required checks cannot verify a change
    assert result.checks == []


def test_non_list_checks_does_not_crash(tmp_path):
    result = run_profile({"checks": 5}, tmp_path)
    assert result.verification == "failed"
    assert result.checks == []


def test_non_dict_check_fails_gate(tmp_path):
    result = run_profile({"checks": ["garbage"]}, tmp_path)
    assert result.verification == "failed"
    assert result.checks[0].status == "failed"


def test_non_str_command_fails_gate(tmp_path):
    result = run_profile(
        {"checks": [{"name": "x", "command": 123, "required": True}]}, tmp_path
    )
    assert result.verification == "failed"
    assert result.checks[0].status == "failed"


def test_invalid_timeout_falls_back_to_default(tmp_path):
    prof = _profile([{"name": "ok", "command": _OK, "required": True}])
    prof["timeout_seconds"] = "not-a-number"
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"


def test_non_str_working_directory_falls_back(tmp_path):
    prof = _profile([{"name": "ok", "command": _OK, "required": True}])
    prof["working_directory"] = 12345
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"


# --- Follow-up robustness from Gemini review (PR #30, cycle 2) ----------------

def test_malformed_command_quotes_is_failed_not_crash(tmp_path):
    # Unclosed quote -> shlex.split raises ValueError; must be caught.
    prof = _profile([{"name": "bad", "command": 'echo "unclosed', "required": True}])
    result = run_profile(prof, tmp_path)
    assert result.verification == "failed"
    assert result.checks[0].status == "failed"


def test_non_positive_timeout_is_coerced(tmp_path):
    prof = _profile([{"name": "ok", "command": _OK, "required": True}])
    for bad in (0, -5):
        prof["timeout_seconds"] = bad
        result = run_profile(prof, tmp_path)
        assert result.verification == "passed"  # coerced to 300, runs fine


def test_utf8_output_is_decoded(tmp_path):
    # Non-ASCII output (emoji) must decode via explicit encoding="utf-8" rather
    # than the platform locale, without crashing the check (Gemini PR #30 c4).
    cmd = 'python3 -c "print(\'done \\u2713 \\U0001f389\')"'
    prof = _profile([{"name": "emoji", "command": cmd, "required": True}])
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"
    assert result.checks[0].status == "passed"


def test_per_check_working_directory_overrides_profile(tmp_path):
    sub = tmp_path / "backend"
    sub.mkdir()
    # marker file only exists in the subdir; the check must run there to find it
    (sub / "marker.txt").write_text("x")
    prof = _profile(
        [
            {
                "name": "backend",
                "command": 'python3 -c "import os,sys; sys.exit(0 if os.path.exists(\'marker.txt\') else 1)"',
                "working_directory": "backend",
                "required": True,
            }
        ],
        working_directory=".",
    )
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"


def test_check_without_cwd_falls_back_to_profile_directory(tmp_path):
    sub = tmp_path / "svc"
    sub.mkdir()
    (sub / "marker.txt").write_text("x")
    prof = _profile(
        [
            {
                "name": "svc",
                "command": 'python3 -c "import os,sys; sys.exit(0 if os.path.exists(\'marker.txt\') else 1)"',
                "required": True,
            }
        ],
        working_directory="svc",
    )
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"


def test_mixed_per_check_and_inherited_cwd(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "marker.txt").write_text("x")
    (b / "marker.txt").write_text("x")
    prof = _profile(
        [
            {
                "name": "in_a",
                "command": 'python3 -c "import os,sys; sys.exit(0 if os.path.exists(\'marker.txt\') else 1)"',
                "working_directory": "a",
                "required": True,
            },
            {
                "name": "in_b_inherited",
                "command": 'python3 -c "import os,sys; sys.exit(0 if os.path.exists(\'marker.txt\') else 1)"',
                "required": True,
            },
        ],
        working_directory="b",
    )
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"
    assert {c.name for c in result.checks} == {"in_a", "in_b_inherited"}


# --- reason_code, stdout/stderr tail, failed_check in to_details() ----------

def test_reason_code_ok_for_passing_check(tmp_path):
    prof = _profile([{"name": "ok", "command": "python3 -c \"import sys; sys.exit(0)\"",
                      "required": True}])
    result = run_profile(prof, tmp_path)
    assert result.checks[0].reason_code == "ok"


def test_reason_code_no_tests_collected_for_exit_5(tmp_path):
    # pytest exits 5 when no tests are collected.
    prof = _profile([{"name": "tests",
                      "command": "python3 -c \"import sys; sys.exit(5)\"",
                      "required": True}])
    result = run_profile(prof, tmp_path)
    assert result.checks[0].reason_code == "no_tests_collected"


def test_reason_code_exit_1_for_test_failure(tmp_path):
    prof = _profile([{"name": "tests",
                      "command": "python3 -c \"import sys; sys.exit(1)\"",
                      "required": True}])
    result = run_profile(prof, tmp_path)
    assert result.checks[0].reason_code == "exit_1"


def test_reason_code_command_not_found(tmp_path):
    prof = _profile([{"name": "missing",
                      "command": "no_such_binary_xyz_abc_123",
                      "required": True}])
    result = run_profile(prof, tmp_path)
    assert result.checks[0].reason_code == "command_not_found"


def test_reason_code_timeout(tmp_path):
    prof = _profile(
        [{"name": "slow",
          "command": "python3 -c \"import time; time.sleep(5)\"",
          "required": True}],
        timeout_seconds=1,
    )
    result = run_profile(prof, tmp_path)
    assert result.checks[0].reason_code == "timeout"


def test_stdout_tail_populated_for_failed_check(tmp_path):
    # Write known output to stdout on failure.
    prof = _profile([{"name": "boom",
                      "command": "python3 -c \"print('failure line'); import sys; sys.exit(1)\"",
                      "required": True}])
    result = run_profile(prof, tmp_path)
    check = result.checks[0]
    assert check.status == "failed"
    assert "failure line" in check.stdout_tail


def test_stderr_tail_populated_for_failed_check(tmp_path):
    prof = _profile([{"name": "boom",
                      "command": "python3 -c \"import sys; print('err line', file=sys.stderr); sys.exit(1)\"",
                      "required": True}])
    result = run_profile(prof, tmp_path)
    assert "err line" in result.checks[0].stderr_tail


def test_output_tail_empty_for_passing_check(tmp_path):
    # Passing checks do not capture output (it's noise).
    prof = _profile([{"name": "ok",
                      "command": "python3 -c \"print('lots of output'); import sys; sys.exit(0)\"",
                      "required": True}])
    result = run_profile(prof, tmp_path)
    check = result.checks[0]
    assert check.stdout_tail == ""
    assert check.stderr_tail == ""


def test_to_details_includes_failed_check_key(tmp_path):
    prof = _profile([{"name": "tests",
                      "command": "python3 -c \"import sys; sys.exit(1)\"",
                      "required": True}])
    result = run_profile(prof, tmp_path)
    details = result.to_details()
    assert details["failed_check"] == "tests"


def test_to_details_failed_check_none_when_all_pass(tmp_path):
    prof = _profile([{"name": "ok",
                      "command": "python3 -c \"import sys; sys.exit(0)\"",
                      "required": True}])
    result = run_profile(prof, tmp_path)
    assert result.to_details()["failed_check"] is None


def test_to_details_checks_include_reason_code(tmp_path):
    prof = _profile([{"name": "ok",
                      "command": "python3 -c \"import sys; sys.exit(0)\"",
                      "required": True}])
    result = run_profile(prof, tmp_path)
    import json
    serialized = json.loads(json.dumps(result.to_details()))
    assert serialized["checks"][0]["reason_code"] == "ok"
