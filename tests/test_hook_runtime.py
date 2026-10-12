import json

from hook_runtime import load_hook_payload, python_script_command, tool_command, tool_name


class TestLoadHookPayload:
    def test_empty_or_invalid_payload_fails_open(self):
        assert load_hook_payload("") == {}
        assert load_hook_payload("not json") == {}
        assert load_hook_payload("[]") == {}

    def test_parses_object_payload(self):
        assert load_hook_payload('{"tool_name": "Bash"}') == {"tool_name": "Bash"}


class TestHookPayloadNormalization:
    def test_claude_style_payload(self):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git push"},
        }
        assert tool_name(payload) == "Bash"
        assert tool_command(payload) == "git push"

    def test_camel_case_payload(self):
        payload = {
            "toolName": "Bash",
            "toolInput": {"command": "git push origin HEAD"},
        }
        assert tool_name(payload) == "Bash"
        assert tool_command(payload) == "git push origin HEAD"

    def test_nested_tool_payload(self):
        payload = {
            "tool": {"name": "Bash"},
            "input": {"command": "git push --force-with-lease"},
        }
        assert tool_name(payload) == "Bash"
        assert tool_command(payload) == "git push --force-with-lease"

    def test_json_round_trip_payload(self):
        raw = json.dumps({"tool": "Edit", "arguments": {"file_path": "x.py"}})
        payload = load_hook_payload(raw)
        assert tool_name(payload) == "Edit"
        assert tool_command(payload) == ""


class TestPythonScriptCommand:
    def test_uses_installed_script_path(self):
        command = python_script_command("fetch_gemini_threads.py")
        assert command.startswith('python3 "')
        assert command.replace("\\", "/").endswith('/fetch_gemini_threads.py"')
        assert "CLAUDE_PLUGIN_ROOT" not in command
