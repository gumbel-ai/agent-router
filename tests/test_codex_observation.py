"""Native Codex observation through the real hook mapping and Router controller.

Payload shapes follow the hooks captured from a real Codex CLI 0.162.1 session
(docs/codex-lifecycle.md); content fields hold placeholders that must never be stored.
"""

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_control import settings

from router import observer_hook
from router.cli import fleet_table, sessions_table
from router.daemon import Controller

SESSION = "019a0000-0000-7000-8000-000000000001"
CHILD = "019a0000-0000-7000-8000-000000000002"
CONTENT = "PRIVATE-CONTENT"


def payload(name, **fields):
    return {"session_id": SESSION, "transcript_path": "/tmp/rollout.jsonl", "cwd": "/work/project",
            "hook_event_name": name, "model": "gpt-6.1-sol", "permission_mode": "default", **fields}


class CodexObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-codex-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control = Controller(settings(self.root / "repo"), {}, self.root / "state")
        # Wait for each write to persist instead of racing the 40 ms acknowledgement.
        timeout = patch("router.daemon.LIFECYCLE_ACK_TIMEOUT_SECONDS", 10)
        timeout.start()
        self.addCleanup(timeout.stop)
        self.control.start_lifecycle_writer()
        self.addCleanup(self.control.stop_lifecycle_writer)
        self.clock = int(time.time() * 1000) - 10_000

    def hook(self, name, **fields):
        self.clock += 10

        def send(operation, body, timeout):
            return self.control.handle(operation, body)

        responses = []

        def record(operation, body, timeout):
            responses.append(send(operation, body, timeout))
            return responses[-1]

        with patch("router.observer_hook.request", side_effect=record), \
                patch("router.observer_hook.time.time", return_value=self.clock / 1000):
            observer_hook.observe_codex(payload(name, **fields))
        for response in responses:
            self.assertFalse(response.get("queued"), response)  # Persisted before the test reads.

    def session(self):
        return next(row for row in self.control.handle("snapshot", {})["sessions"] if row["session_id"] == SESSION)

    def agent(self):
        return next(row for row in self.control.native.status() if row["agent_id"] == CHILD)

    def test_session_and_subagent_lifecycle_without_content(self):
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", turn_id="t1", prompt=CONTENT)
        self.hook("PreToolUse", turn_id="t1", tool_name="Bash", tool_input={"command": CONTENT},
                  tool_use_id="exec-1")
        self.assertEqual(self.session()["status"], "running")
        self.hook("PostToolUse", turn_id="t1", tool_name="Bash", tool_input={"command": CONTENT},
                  tool_response=CONTENT, tool_use_id="exec-1")
        self.hook("SubagentStart", turn_id="t2", agent_id=CHILD, agent_type="default")
        self.assertEqual(self.agent()["status"], "running")
        self.hook("PreToolUse", turn_id="t2", agent_id=CHILD, agent_type="default", tool_name="Bash",
                  tool_input={"command": CONTENT}, tool_use_id="exec-2")
        self.assertEqual(self.agent()["activity"], "Bash")
        self.hook("PostToolUse", turn_id="t2", agent_id=CHILD, agent_type="default", tool_name="Bash",
                  tool_input={"command": CONTENT}, tool_response=CONTENT, tool_use_id="exec-2")
        self.hook("SubagentStop", turn_id="t2", agent_id=CHILD, agent_type="default",
                  last_assistant_message=CONTENT, stop_hook_active=False)
        agent = self.agent()
        self.assertEqual((agent["status"], agent["source"], agent["parent_session_id"]), ("idle", "codex", SESSION))
        self.hook("Stop", turn_id="t1", last_assistant_message=CONTENT, stop_hook_active=False)
        session = self.session()
        self.assertEqual((session["status"], session["source"]), ("idle", "codex"))
        # A subagent turn ending is idle; the root session ending is its end (no subagent-end hook).
        self.hook("SessionEnd", reason="other")
        self.assertEqual(self.session()["status"], "stopped")
        self.assertEqual(self.agent()["status"], "stopped")
        with self.control.store.connect() as db:
            stored = json.dumps([row[0] for table in ("sessions", "observations")
                                 for row in db.execute(f"SELECT data FROM {table}")])
        self.assertNotIn(CONTENT, stored)

    def test_permission_request_waits_per_agent_and_tool_completion_clears_it(self):
        self.hook("SessionStart", source="startup")
        self.hook("SubagentStart", turn_id="t2", agent_id=CHILD, agent_type="default")
        self.hook("PreToolUse", turn_id="t2", agent_id=CHILD, tool_name="Bash", tool_input={}, tool_use_id="exec-3")
        self.hook("PermissionRequest", turn_id="t2", agent_id=CHILD, tool_name="Bash", tool_input={})
        self.assertTrue(self.session()["needs_input"])
        self.assertEqual(self.agent()["status"], "needs_input")
        self.hook("PostToolUse", turn_id="t2", agent_id=CHILD, tool_name="Bash", tool_input={},
                  tool_response="", tool_use_id="exec-3")
        self.assertFalse(self.session()["needs_input"])
        self.assertEqual(self.agent()["status"], "running")

    def test_subagent_prompt_answers_its_wait_without_resetting_the_root_turn(self):
        self.hook("SessionStart", source="startup")
        self.hook("PreToolUse", turn_id="t1", tool_name="Bash", tool_input={}, tool_use_id="exec-4")
        self.hook("PermissionRequest", turn_id="t2", agent_id=CHILD, tool_name="Bash", tool_input={})
        self.assertTrue(self.session()["needs_input"])
        self.hook("UserPromptSubmit", turn_id="t2", agent_id=CHILD, prompt=CONTENT)
        session = self.session()
        self.assertEqual(session["inflight_tool_count"], 1)  # The root tool is still live.
        self.assertFalse(session["needs_input"])

    def test_restart_marks_running_codex_work_uncertain(self):
        self.hook("SessionStart", source="startup")
        self.hook("SubagentStart", turn_id="t2", agent_id=CHILD, agent_type="default")
        self.control.sessions.reconcile_restart()
        self.control.native.reconcile_restart()
        self.assertEqual(self.agent()["status"], "unknown")
        self.assertIn("restart", self.session()["visibility_reason"].lower())

    def test_session_end_closes_subagents_left_unknown_by_a_restart(self):
        self.hook("SessionStart", source="startup")
        self.hook("SubagentStart", turn_id="t2", agent_id=CHILD, agent_type="default")
        self.control.sessions.reconcile_restart()
        self.control.native.reconcile_restart()
        self.hook("SessionEnd", reason="other")
        self.assertEqual(self.agent()["status"], "stopped")

    def test_subagent_turn_ending_after_root_turn_does_not_reopen_the_session(self):
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", turn_id="t1", prompt=CONTENT)
        self.hook("SubagentStart", turn_id="t2", agent_id=CHILD, agent_type="default")
        self.hook("Stop", turn_id="t1", last_assistant_message=CONTENT, stop_hook_active=False)
        self.hook("SubagentStop", turn_id="t2", agent_id=CHILD, agent_type="default",
                  last_assistant_message=CONTENT, stop_hook_active=False)
        self.assertEqual(self.session()["status"], "idle")
        with patch("router.sessions.now_ms", return_value=self.clock + 1):
            self.assertEqual(self.control.sessions.fleet_records(10)[1], 0)

    def test_late_subagent_events_after_session_end_are_stopped_in_either_order(self):
        for late in ("SubagentStart", "PreToolUse", "SubagentStop", "PermissionRequest"):
            with self.subTest(late):
                self.control = Controller(settings(self.root / "repo"), {}, self.root / f"state-{late}")
                self.control.start_lifecycle_writer()
                self.addCleanup(self.control.stop_lifecycle_writer)
                start = self.clock
                self.hook("SessionStart", source="startup")
                self.clock = start + 1000
                self.hook("SessionEnd", reason="other")
                self.clock = start + 100  # Delivered after the end, but timestamped before it.
                self.hook(late, turn_id="t2", agent_id=CHILD, agent_type="default", tool_name="Bash",
                          tool_input={}, tool_use_id="exec-9")
                self.assertEqual(self.agent()["status"], "stopped")
                self.clock = start + 2000

    def test_child_work_finishing_after_the_root_turn_returns_the_session_to_idle(self):
        root, child = {"model": "root-model"}, {"model": "child-model", "agent_id": CHILD, "turn_id": "t2"}
        self.hook("SessionStart", source="startup", **root)
        self.hook("UserPromptSubmit", turn_id="t1", prompt=CONTENT, **root)
        self.hook("SubagentStart", agent_type="default", **child)
        self.hook("Stop", turn_id="t1", last_assistant_message=CONTENT, stop_hook_active=False, **root)
        self.hook("PreToolUse", tool_name="Bash", tool_input={}, tool_use_id="exec-5", **child)
        self.assertEqual(self.session()["status"], "running")  # The child is still working.
        self.hook("PostToolUse", tool_name="Bash", tool_input={}, tool_response="", tool_use_id="exec-5", **child)
        self.hook("SubagentStop", agent_type="default", last_assistant_message=CONTENT,
                  stop_hook_active=False, **child)
        session = self.session()
        self.assertEqual((session["status"], session["model"]), ("idle", "root-model"))
        self.assertEqual(self.agent()["resolved_model"], "child-model")
        with patch("router.sessions.now_ms", return_value=self.clock + 1):
            self.assertEqual(self.control.sessions.fleet_records(10)[1], 0)

    def test_root_turn_state_is_ordered_by_root_events(self):
        child = {"agent_id": CHILD, "turn_id": "t2"}
        cases = {
            # Root tool activity after Stop reopens the root turn.
            "root tool after stop": ([("Stop", 30, {}), ("PreToolUse", 40, {"tool_use_id": "r1"}),
                                      ("PostToolUse", 50, {"tool_use_id": "r1"}), ("SubagentStop", 60, child)],
                                     "running"),
            # A delayed root Stop still closes the turn despite newer child events.
            "delayed root stop": ([("PreToolUse", 45, {**child, "tool_use_id": "c1"}),
                                   ("PostToolUse", 50, {**child, "tool_use_id": "c1"}),
                                   ("Stop", 40, {}), ("SubagentStop", 60, child)], "idle"),
        }
        for name, (events, status) in cases.items():
            with self.subTest(name):
                self.control = Controller(settings(self.root / "repo"), {}, self.root / f"state-{name}")
                self.control.start_lifecycle_writer()
                self.addCleanup(self.control.stop_lifecycle_writer)
                start = self.clock
                self.hook("SessionStart", source="startup")
                self.hook("UserPromptSubmit", turn_id="t1", prompt=CONTENT)
                self.hook("SubagentStart", agent_type="default", **child)
                for hook_name, offset, fields in events:
                    self.clock = start + offset
                    self.hook(hook_name, tool_name="Bash", tool_input={}, tool_response="", **fields)
                self.assertEqual(self.session()["status"], status)
                self.clock = start + 100

    def test_status_and_fleet_tables_label_the_source(self):
        self.hook("SessionStart", source="startup")
        self.hook("SubagentStart", turn_id="t2", agent_id=CHILD, agent_type="default")
        snapshot = self.control.handle("snapshot", {})
        self.assertIn("codex", sessions_table(snapshot["sessions"]))
        rendered = fleet_table({"hosts": [{"name": "local", "ok": True, "snapshot": snapshot}]})
        self.assertIn("codex:", rendered)

    def test_status_reports_an_incomplete_session_read_instead_of_none(self):
        rendered = sessions_table([], "database is locked")
        self.assertIn("incomplete", rendered)
        self.assertNotIn("No observed sessions", rendered)
        self.assertEqual(sessions_table([]), "No observed sessions.")

    def test_hook_entry_point_forwards_codex_source(self):
        sent = []

        def capture(operation, body, timeout):
            sent.append(body)
            return {"accepted": True}

        with patch("sys.stdin", io.StringIO(json.dumps(payload("SessionStart", source="startup")))), \
                patch("sys.argv", ["router-observe", "--source", "codex"]), \
                patch("router.observer_hook.request", side_effect=capture), redirect_stdout(io.StringIO()):
            observer_hook.main()
        self.assertEqual(sent[0]["session"]["source"], "codex")

    def test_hook_entry_point_always_answers_and_never_raises(self):
        for stdin in ("not json", "[]", json.dumps(payload("PreCompact", trigger="auto"))):
            with self.subTest(stdin=stdin[:20]), patch("sys.stdin", io.StringIO(stdin)), \
                    patch("sys.argv", ["router-observe", "--source", "codex"]), \
                    patch("router.observer_hook.request", side_effect=AssertionError("must not send")):
                output = io.StringIO()
                with redirect_stdout(output):
                    observer_hook.main()
                self.assertEqual(output.getvalue().strip(), "{}")
        # Deeply nested input can exhaust the parser's recursion limit on smaller stacks.
        with patch("router.observer_hook.json.load", side_effect=RecursionError), \
                patch("sys.argv", ["router-observe", "--source", "codex"]):
            output = io.StringIO()
            with redirect_stdout(output):
                observer_hook.main()
            self.assertEqual(output.getvalue().strip(), "{}")


if __name__ == "__main__":
    unittest.main()
