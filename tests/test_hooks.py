import io
import json
import sqlite3
import subprocess
import sys
import time
import unittest
from contextlib import closing
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

    def eventually(self, read, accept):
        # Hooks acknowledge within 50 ms; the daemon may persist slightly later.
        deadline = time.monotonic() + 5
        value = read()
        while not accept(value) and time.monotonic() < deadline:
            time.sleep(.02)
            value = read()
        return value

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

        rows = self.eventually(lambda: request("status", {}, self.endpoint), lambda rows: len(rows) == 1)
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
        stopped = self.eventually(lambda: request("status", {"task_id": native["task_id"]}, self.endpoint),
                                  lambda row: row["status"] == "stopped")
        self.assertEqual(stopped["status"], "stopped")
        request("daemon_stop", {}, self.endpoint)
        self.daemons[-1].wait(timeout=15)
        self.assertEqual(self.hook(launch, "observe"), {})

    def test_observer_filter_ignores_main_tools_and_marks_unconfirmed_result_unknown(self):
        base = {"session_id": "foreground", "cwd": str(self.repo)}
        self.assertEqual(self.hook({**base, "hook_event_name": "PreToolUse", "tool_name": "Read"}, "observe"), {})
        self.assertEqual(request("status", {}, self.endpoint), [])
        local = self.eventually(lambda: request("snapshot", {}, self.endpoint),
                                lambda local: local["sessions"] and local["sessions"][0]["activity"] == "Read")
        self.assertEqual(local["sessions"][0]["session_id"], "foreground")
        self.assertEqual(local["sessions"][0]["activity"], "Read")
        self.assertEqual(self.hook({**base, "hook_event_name": "PostToolUse", "tool_name": "Agent",
            "tool_input": {"description": "Foreground result"}, "tool_response": {"agentId": "child", "isAsync": False}}, "observe"), {})
        rows = self.eventually(lambda: request("status", {}, self.endpoint),
                               lambda rows: rows and rows[0]["status"] == "unknown")
        result = rows[0]
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
        local = self.eventually(lambda: request("snapshot", {}, self.endpoint),
                                lambda local: local["sessions"]
                                and "turn failed" in (local["sessions"][0]["visibility_reason"] or ""))
        self.assertEqual(local["tasks"][0]["status"], "completed")
        session = local["sessions"][0]
        self.assertEqual(session["status"], "idle")
        self.assertIn("turn failed", session["visibility_reason"])
        native = self.eventually(lambda: request("status", {}, self.endpoint)[0],
                                 lambda native: native["status"] == "failed")
        self.assertEqual(native["status"], "failed")
        self.assertNotIn("SECRET", json.dumps({"session": session, "native": native}))

    def test_locked_observation_database_is_best_effort_and_fast(self):
        database = self.root / "state" / "router.sqlite3"
        with closing(sqlite3.connect(database, timeout=.1)) as locked, locked:
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

    def test_observer_keeps_labels_strips_controls_bounds_and_forwards_no_content(self):
        requests = []

        def send(operation, payload, timeout):
            requests.append(payload)
            return {"accepted": True}

        with patch("router.observer_hook.request", side_effect=send):
            observe({"hook_event_name": "TaskCreated", "session_id": "S", "task_id": "T",
                     "task_subject": "Rotate \x1b[31mkeys\x1b[0m for https://user:pw@host " + "s" * 600})
            observe({"hook_event_name": "PostToolUse", "session_id": "S", "tool_name": "Agent",
                     "tool_input": {"description": "Fix parser " + "d" * 300},
                     "tool_response": {"agentId": "A", "isAsync": True}})
            observe({"hook_event_name": "SubagentStop", "session_id": "S", "agent_id": "A",
                     "last_assistant_message": "Done.\nSet API_KEY=example in .env " + "m" * 900})
            observe({"hook_event_name": "PreToolUse", "session_id": "S", "agent_id": "A",
                     "tool_name": "Bash\x1b]0;title\x07", "tool_input": {"file_path": "/p/" + "f" * 2100}})

        subject = requests[0]["session"]["task_subject"]
        self.assertTrue(subject.startswith("Rotate keys for https://user:pw@host s"))
        self.assertEqual(len(subject), 500)
        self.assertEqual(len(requests[1]["native"]["task"]), 240)
        # Content stays with Langfuse: no assistant text, transcript path, or file path.
        for request_payload in requests:
            self.assertFalse({"summary", "transcript", "file_path", "cost"} & (request_payload["native"] or {}).keys())
        self.assertEqual(requests[3]["native"]["activity"], "Bash")
        self.assertNotIn("\\u001b", json.dumps(requests))

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


if __name__ == '__main__':
    unittest.main()
