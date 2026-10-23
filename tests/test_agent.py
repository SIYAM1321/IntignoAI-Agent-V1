import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("WORKSPACE", tempfile.mkdtemp(prefix="intignoai-test-"))

from agent.engine import canonical_tool_name, function_result
from tools.terminal import command_requires_confirmation, default_shell


class AgentTests(unittest.TestCase):
    def test_exact_function_name_is_preserved(self):
        call = {"name": r"default_api\:run_terminal", "call_id": "abc", "arguments": {}}
        payload = function_result(call, {"ok": True})
        self.assertEqual(payload["name"], call["name"])
        self.assertEqual(payload["call_id"], call["call_id"])
        self.assertEqual(json.loads(payload["result"][0]["text"])["ok"], True)

    def test_namespace_is_only_for_execution_lookup(self):
        self.assertEqual(canonical_tool_name("default_api:run_terminal"), "run_terminal")
        self.assertEqual(canonical_tool_name(r"default_api\:run_terminal"), "run_terminal")

    def test_dangerous_commands_require_confirmation(self):
        self.assertTrue(command_requires_confirmation("rm -rf folder"))
        self.assertTrue(command_requires_confirmation("Remove-Item thing -Recurse -Force"))
        self.assertFalse(command_requires_confirmation("python --version"))

    def test_shell_is_valid(self):
        self.assertIn(default_shell(), {"powershell", "cmd", "bash", "sh"})


if __name__ == "__main__":
    unittest.main()
