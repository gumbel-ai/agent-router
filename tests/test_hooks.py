import io
import json
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from test_transport import TransportHarness

from router.client import request
from router.observer_hook import observe


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
                  "tool_use_id": "launch-tool",
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
        local = request("snapshot", {}, self.endpoint)
        self.assertEqual(local["sessions"][0]["session_id"], "native-host")
        self.assertEqual(local["agents"][0]["agent_id"], "nested-child")
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
        local = request("snapshot", {}, self.endpoint)
        self.assertEqual(local["sessions"][0]["session_id"], "foreground")
        self.assertEqual(local["sessions"][0]["activity"], "Read")
        self.assertEqual(self.hook({**base, "hook_event_name": "PostToolUse", "tool_name": "Agent",
            "tool_input": {"description": "Foreground result"}, "tool_response": {"agentId": "child", "isAsync": False}}, "observe"), {})
        result = request("status", {}, self.endpoint)[0]
        self.assertEqual(result["status"], "unknown")
        self.assertIn("without observed stop", result["visibility_reason"])

    def test_lifecycle_routes_and_failure_evidence_is_generic(self):
        base = {"session_id": "lifecycle", "cwd": str(self.repo), "agent_id": "child"}
        events = [
            {"hook_event_name": "TaskCreated", "task_id": "T", "task_subject": "subject"},
            {"hook_event_name": "TaskCompleted", "task_id": "T"},
            {"hook_event_name": "TeammateIdle"},
            {"hook_event_name": "Stop", "background_tasks": []},
            {"hook_event_name": "Notification", "notification_type": "agent_needs_input"},
            {"hook_event_name": "StopFailure", "error": "SECRET raw failure"},
        ]
        for event in events:
            self.assertEqual(self.hook({**base, **event}, "observe"), {})
        local = request("snapshot", {}, self.endpoint)
        self.assertEqual(local["tasks"][0]["status"], "completed")
        session = local["sessions"][0]
        self.assertEqual(session["status"], "idle")
        self.assertIn("turn failed", session["visibility_reason"])
        native = request("status", {}, self.endpoint)[0]
        self.assertEqual(native["status"], "failed")
        self.assertNotIn("SECRET", json.dumps({"session": session, "native": native}))

    def test_locked_observation_database_is_best_effort_and_fast(self):
        database = self.root / "state" / "router.sqlite3"
        with sqlite3.connect(database, timeout=.1) as locked:
            locked.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            self.assertEqual(self.hook({"hook_event_name": "SessionStart", "session_id": "locked",
                                        "cwd": str(self.repo)}, "observe"), {})
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, .3)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if "locked" in {item["session_id"] for item in request("snapshot", {}, self.endpoint)["sessions"]}:
                break
            time.sleep(.02)
        else:
            self.fail("queued lifecycle observation was not persisted after the lock cleared")

    def test_observer_falls_back_to_legacy_native_operation_during_upgrade(self):
        calls = []
        def send(operation, payload, timeout):
            calls.append(operation)
            if operation == "lifecycle_observe":
                raise ValueError("Unknown operation")
            return {"observed": True}
        with patch("router.observer_hook.request", side_effect=send):
            observe({"hook_event_name": "SubagentStart", "session_id": "S", "agent_id": "A"})
        self.assertEqual(calls, ["lifecycle_observe", "observe"])

    def test_observer_redacts_task_metadata_before_bounding_hook_payload(self):
        requests = []
        secret = "x" * 217 + " https://user:swordfish@example.com"

        def send(operation, payload, timeout):
            requests.append((operation, payload))
            return {"accepted": True}

        with patch("router.observer_hook.request", side_effect=send):
            observe({"hook_event_name": "TaskCreated", "session_id": "S",
                     "task_id": "T", "task_subject": secret})
            observe({"hook_event_name": "PostToolUse", "session_id": "S", "tool_name": "Agent",
                     "tool_input": {"description": secret},
                     "tool_response": {"agentId": "A", "isAsync": True}})
            observe({"hook_event_name": "SubagentStop", "session_id": "S", "agent_id": "A",
                     "last_assistant_message": "status=failed\nclientSecret=swordfish"})

        serialized = json.dumps(requests)
        self.assertNotIn("swordfish", serialized)
        self.assertEqual(requests[0][1]["session"]["task_subject"].count("[REDACTED]"), 1)
        self.assertEqual(requests[1][1]["native"]["task"].count("[REDACTED]"), 1)
        self.assertNotIn("swordfish", requests[2][1]["native"]["summary"])

    def test_observer_reports_only_terminal_lifecycle_failures(self):
        cases = {
            "rejected": ({"accepted": False, "queued": False, "persisted": False,
                          "reason": "writer is stopping"}, "writer is stopping"),
            "current pending": ({"accepted": False, "queued": True, "persisted": False,
                                 "reason": "not durably persisted before timeout"},
                                "not durably persisted before timeout"),
            "current failed": ({"accepted": False, "queued": False, "persisted": False,
                                "reason": "write failed"}, "write failed"),
            "current partial without reason": ({
                "accepted": False, "queued": False, "persisted": False, "partial": True,
                "persistence": {"session": "persisted", "native": "failed"},
            }, "Lifecycle observation failed for: native"),
            "durable": ({"accepted": True, "queued": False, "persisted": True}, None),
            "legacy accepted nonpersisted": ({"accepted": True, "persisted": False,
                                               "reason": "legacy write failed"}, "legacy write failed"),
        }
        for name, (response, expected_error) in cases.items():
            with self.subTest(name=name):
                error = io.StringIO()
                with patch("router.observer_hook.request", return_value=response), \
                        patch("router.observer_hook.sys.stderr", error):
                    observe({"hook_event_name": "Stop", "session_id": "S", "background_tasks": []})
                if expected_error is None:
                    self.assertEqual(error.getvalue(), "")
                else:
                    self.assertIn(expected_error, error.getvalue())

    def test_session_start_forwards_structured_hook_parent_identity(self):
        requests = []

        def send(operation, payload, timeout):
            requests.append((operation, payload))
            return {"accepted": True}

        with patch("router.observer_hook.os.getppid", return_value=4321), \
                patch("router.process.creation_identity",
                      return_value="linux:boot-id:98765"), \
                patch("router.observer_hook.request", side_effect=send):
            observe({"hook_event_name": "SessionStart", "session_id": "S"})

        session = requests[0][1]["session"]
        self.assertEqual(session["claude_pid"], 4321)
        self.assertEqual(session["claude_process_start_time"], "98765")
        self.assertEqual(session["claude_creation_identity"], "linux:boot-id:98765")

    def test_prompt_and_stop_retry_missing_session_process_identity(self):
        requests = []

        def send(operation, payload, timeout):
            requests.append(payload["session"])
            return {"accepted": True}

        with patch("router.observer_hook.process_evidence", side_effect=[
                {},
                {"claude_pid": 4321, "claude_process_start_time": "98765",
                 "claude_creation_identity": "linux:boot-id:98765"},
                {"claude_pid": 4321, "claude_process_start_time": "98765",
                 "claude_creation_identity": "linux:boot-id:98765"},
        ]), patch("router.observer_hook.request", side_effect=send):
            observe({"hook_event_name": "SessionStart", "session_id": "S"})
            observe({"hook_event_name": "UserPromptSubmit", "session_id": "S"})
            observe({"hook_event_name": "Stop", "session_id": "S"})

        self.assertNotIn("claude_pid", requests[0])
        self.assertEqual(requests[1]["claude_pid"], 4321)
        self.assertEqual(requests[2]["claude_creation_identity"], "linux:boot-id:98765")


if __name__ == '__main__':
    unittest.main()
