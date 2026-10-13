import json
import subprocess
import sys
import unittest

from test_transport import TransportHarness

from router.client import request


class HookTests(TransportHarness, unittest.TestCase):
    def hook(self, event, name):
        command = [sys.executable, "-m", "router.observer_hook"] if name == "observe" else [sys.executable, "-m", "router.hooks", name]
        result = subprocess.run(command,
                                env=self.environment, input=json.dumps(event), capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_offline_submission_and_nonliteral_dispatch(self):
        event = {"session_id": "host", "prompt_id": "turn", "cwd": str(self.repo), "prompt": "never persist this body"}
        unconfigured = {**event, "session_id": "unconfigured", "cwd": str(self.root / "other-project")}
        self.assertEqual(self.hook(unconfigured, "submit"), {})
        self.assertEqual(request("submit", {"session_id": "unconfigured", "prompt_id": "turn",
                                           "cwd": unconfigured["cwd"]}, self.endpoint), {"instruction_id": None})
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
        self.assertEqual(offline, {})
        self.assertEqual(self.hook(unconfigured, "submit"), {})

    def test_native_hooks_preserve_parent_and_allow_offline_native_work(self):
        event = {"session_id": "native-host", "prompt_id": "turn", "cwd": str(self.repo),
                 "transcript_path": str(self.repo / "host.jsonl"), "agent_id": "parent-native"}
        launch = {**event, "hook_event_name": "PostToolUse", "tool_name": "Agent",
                  "tool_input": {"description": "Fix a parser", "prompt": "PRIVATE DELEGATED PROMPT", "model": "haiku"},
                  "tool_response": {"agentId": "nested-child", "resolvedModel": "claude-haiku-5-5", "isAsync": True}}
        self.assertEqual(self.hook(launch, "observe"), {})

        rows = request("status", {}, self.endpoint)
        self.assertEqual(len(rows), 1)
        native = rows[0]
        self.assertEqual(native["parent_agent_id"], "parent-native")
        self.assertEqual(native["task"], "Fix a parser")
        self.assertNotIn("PRIVATE", json.dumps(native))
        self.assertEqual(native["resolved_model"], "claude-haiku-5-5")
        instruction = request("submit", {"session_id": event["session_id"], "prompt_id": event["prompt_id"]}, self.endpoint)["instruction_id"]
        invocation = {"request_version": "1", "instruction_id": instruction, "request_id": "nested-router-start",
                      "executor": "opencode", "model": "test", "task": "flood", "mode": "write",
                      "parent_repo": str(self.repo), "parent_worktree": str(self.repo)}
        self.hook({**event, "tool_name": "Bash", "tool_input": {"command": "router start --json '" + json.dumps(invocation) + "'"}}, "authorize")
        controlled = request("start", invocation, self.endpoint)
        self.assertEqual(controlled["parent_agent_id"], "parent-native")
        self.assertEqual(controlled["control_mode"], "control")
        self.assertEqual(self.hook({**event, "agent_id": "nested-child", "hook_event_name": "SubagentStop"}, "observe"), {})
        self.assertEqual(request("status", {"task_id": native["task_id"]}, self.endpoint)["status"], "stopped")
        request("daemon_stop", {}, self.endpoint)
        self.daemons[-1].wait(timeout=15)
        self.assertEqual(self.hook(launch, "observe"), {})

    def test_observer_filter_ignores_main_tools_and_marks_unconfirmed_result_unknown(self):
        base = {"session_id": "foreground", "cwd": str(self.repo)}
        self.assertEqual(self.hook({**base, "hook_event_name": "PreToolUse", "tool_name": "Read"}, "observe"), {})
        self.assertEqual(request("status", {}, self.endpoint), [])
        self.assertEqual(self.hook({**base, "hook_event_name": "PostToolUse", "tool_name": "Agent",
            "tool_input": {"description": "Foreground result"}, "tool_response": {"agentId": "child", "isAsync": False}}, "observe"), {})
        result = request("status", {}, self.endpoint)[0]
        self.assertEqual(result["status"], "unknown")
        self.assertIn("without observed stop", result["visibility_reason"])


if __name__ == '__main__':
    unittest.main()
