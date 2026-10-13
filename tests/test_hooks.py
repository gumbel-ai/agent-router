import json
import subprocess
import sys
import unittest

from test_transport import TransportHarness

from router.client import request


class HookTests(TransportHarness, unittest.TestCase):
    def hook(self, event, name):
        result = subprocess.run([sys.executable, "-m", "router.cli", "hook", name],
                                env=self.environment, input=json.dumps(event), capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_offline_submission_and_nonliteral_dispatch(self):
        event = {"session_id": "host", "prompt_id": "turn", "prompt": "never persist this body"}
        submission = self.hook(event, "submit")
        self.assertIn("ROUTER_INSTRUCTION_ID=", submission["hookSpecificOutput"]["additionalContext"])
        ordinary = self.hook({**event, "tool_name": "Bash", "tool_input": {"command": "git status"}}, "authorize")
        self.assertEqual(ordinary, {})
        refused = self.hook({**event, "tool_name": "Bash", "tool_input": {
            "command": 'router start --json "{\\"task\\":\\"$(printf changed)\\"}"'}}, "authorize")
        self.assertEqual(refused["hookSpecificOutput"]["permissionDecision"], "deny")
        request("daemon_stop", {}, self.endpoint)
        self.daemons[-1].wait(timeout=15)
        offline = self.hook(event, "submit")
        self.assertIn("unavailable", offline["hookSpecificOutput"]["additionalContext"])


if __name__ == '__main__':
    unittest.main()
