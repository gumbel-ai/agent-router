"""Codex's native turn identity drives the existing parent authorization contract."""

import io
import json
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_control import settings

from router import hooks
from router.daemon import Controller


class CodexDelegationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ar-codex-parent-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "example-project"
        self.project.mkdir()
        (self.project / ".agent-router.json").write_text('{"observe": true}')
        self.unregistered = self.root / "unregistered"
        self.unregistered.mkdir()
        (self.unregistered / ".agent-router.json").write_text('{"observe": true}')
        self.unmarked = self.project / "not-enabled"
        self.unmarked.mkdir()
        (self.unmarked / ".agent-router.json").write_text('{"observe": false}')
        self.markerless = self.root / "markerless"
        self.markerless.mkdir()
        self.controller = Controller(settings(self.project), {}, self.root / "state")
        configuration = patch("router.hooks.configuration", return_value=(
            {"AGENT_ROUTER_REPOSITORIES": {str(self.project): {"setup_command": "true"},
                                           str(self.markerless): {"setup_command": "true"}}}, {}))
        configuration.start()
        self.addCleanup(configuration.stop)
        self.forwarded = []

        def forward(operation, body):
            self.forwarded.append((operation, body))
            return self.controller.handle(operation, body)

        transport = patch("router.hooks.request", side_effect=forward)
        transport.start()
        self.addCleanup(transport.stop)

    def event(self, name="UserPromptSubmit", **fields):
        return {"hook_event_name": name, "session_id": "parent-session", "turn_id": "turn-a",
                "cwd": str(self.project), **fields}

    def hook(self, mode, event):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["router-host-hook", mode]), patch("sys.stdin", io.StringIO(json.dumps(event))), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            hooks.main()
        return json.loads(stdout.getvalue()), stderr.getvalue()

    def submit(self, **fields):
        result, stderr = self.hook("codex-submit", self.event(prompt="CONTENT-NOT-FORWARDED", **fields))
        self.assertEqual(stderr, "")
        return result["hookSpecificOutput"]["additionalContext"].removeprefix("ROUTER_INSTRUCTION_ID=")

    def authorize(self, operation, invocation, **fields):
        command = shlex.join(["router", operation, "--json", json.dumps(invocation)])
        return self.hook("codex-authorize", self.event("PreToolUse", tool_name="Bash",
                                                     tool_input={"command": command}, **fields))[0]

    def test_parent_lifecycle_replays_and_resumed_turns_use_existing_ownership(self):
        first = self.submit()
        self.assertEqual(self.submit(), first)
        invocation = {"request_version": "1", "request_id": "start-a", "instruction_id": first,
                      "executor": "opencode", "model": "test", "task": "Update a parser",
                      "parent_repo": str(self.project), "parent_worktree": str(self.project), "mode": "write"}
        self.assertEqual(self.authorize("start", invocation), {})
        created = self.controller.handle("start", invocation)
        self.assertEqual(self.controller.handle("start", invocation)["task_id"], created["task_id"])
        self.assertIsNone(created["parent_agent_id"])  # Root hook has no native agent_id.
        changed = {**invocation, "task": "different"}
        self.assertEqual(self.authorize("start", changed)["hookSpecificOutput"]["permissionDecision"], "deny")
        with self.assertRaises(ValueError):
            self.controller.handle("start", changed)

        # Resume preserves the native session and supplies a fresh native turn ID.
        second = self.submit(turn_id="resumed-turn")
        self.assertNotEqual(second, first)
        self.assertEqual(self.submit(), first)  # A lost old reply cannot replace the current turn.
        for fields, payload in (({}, invocation), ({"turn_id": "resumed-turn"}, invocation),
                                ({"session_id": "other-session", "turn_id": "resumed-turn"}, invocation)):
            self.assertEqual(self.authorize("start", payload, **fields)["hookSpecificOutput"]["permissionDecision"], "deny")
        task_id = created["task_id"]
        cancel = {"task_id": task_id, "request_id": "cancel-a"}
        self.assertEqual(self.authorize("cancel", cancel, turn_id="resumed-turn"), {})
        self.assertEqual(self.controller.handle("cancel", cancel)["status"], "cancelled")
        close = {"task_id": task_id, "request_id": "close-a", "disposition": "discarded", "keep_artifacts": True}
        self.assertEqual(self.authorize("close", close, turn_id="resumed-turn"), {})
        self.assertEqual(self.controller.handle("close", close)["status"], "closed")

        # A second task exercises the unchanged parent review/acceptance fence.
        next_start = {**invocation, "request_id": "start-b", "instruction_id": second}
        self.assertEqual(self.authorize("start", next_start, turn_id="resumed-turn"), {})
        task_id = self.controller.handle("start", next_start)["task_id"]
        self.controller.store.update(task_id, lambda task: task.update(status="awaiting_review", attempts=1,
                                                                      execution_stopped=True, final_tree="tree"))
        review = {"task_id": task_id, "request_id": "review-a", "expected_attempt": 1, "outcome": "accepted"}
        self.assertEqual(self.authorize("review", review, session_id="other-session", turn_id="resumed-turn")
                         ["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.authorize("review", review, turn_id="resumed-turn"), {})
        self.assertEqual(self.controller.handle("review", review)["status"], "accepted")
        with self.controller.store.connect() as db:
            rows = db.execute("SELECT session_id,prompt_id,parent_cli FROM submissions").fetchall()
        self.assertCountEqual([tuple(row) for row in rows], [("parent-session", "turn-a", "codex"),
                                                           ("parent-session", "resumed-turn", "codex")])
        self.assertNotIn("CONTENT-NOT-FORWARDED", json.dumps(self.forwarded))
        self.assertEqual(len(self.controller.store.all()), 2)

    def test_unknown_identity_subagents_and_missing_scope_never_gain_authorization(self):
        instruction = self.submit()
        invocation = {"request_version": "1", "request_id": "not-authorized", "instruction_id": instruction,
                      "executor": "codex", "model": "test", "task": "task", "mode": "write",
                      "parent_repo": str(self.project), "parent_worktree": str(self.project)}
        variants = [{"turn_id": value} for value in (None, "", 7, " padded ", "turn\n")]
        variants += [{"session_id": value} for value in (None, "", 7, " padded ", "session\n")]
        variants += [{"agent_id": value} for value in ("worker-a", "", None, 7)]
        variants += [{"agent_type": value} for value in ("default", "coordinator", None)]
        variants += [{"cwd": str(self.root)}, {"cwd": str(self.root / "elsewhere")},
                     {"cwd": str(self.unregistered)}, {"cwd": str(self.unmarked)}, {"cwd": str(self.markerless)},
                     {"hook_event_name": "SessionStart"}]
        for fields in variants:
            with self.subTest(fields=fields):
                before = len(self.forwarded)
                result, diagnostics = self.hook("codex-submit", self.event(**fields))
                self.assertEqual(result, {})
                if "cwd" not in fields:
                    self.assertIn("Agent Router delegation unavailable", diagnostics)
                denied = self.authorize("start", invocation, **fields)
                self.assertEqual(denied.get("hookSpecificOutput", {}).get("permissionDecision"), "deny", denied)
                self.assertEqual(len(self.forwarded), before)
        for missing in ("turn_id", "session_id", "hook_event_name"):
            event = self.event()
            del event[missing]
            result, diagnostics = self.hook("codex-submit", event)
            self.assertEqual(result, {})
            self.assertIn("Agent Router delegation unavailable", diagnostics)
        # Unknown fields on an unrelated tool do not acquire delegation authority or block it.
        result, diagnostics = self.hook("codex-authorize", {"tool_name": "Read", "tool_input": {}})
        self.assertEqual((result, diagnostics), ({}, ""))
        for mode in ("authorize", "codex-authorize"):
            for tool_input in (None, [], "command", {}, {"command": None}, {"command": 7}):
                with self.subTest(mode=mode, tool_input=tool_input):
                    result, diagnostics = self.hook(mode, self.event("PreToolUse", tool_name="Bash", tool_input=tool_input))
                    self.assertEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny", result)
                    self.assertEqual(diagnostics, "")
        for value in (None, [], "unknown", 7):
            with self.subTest(input=value):
                result, diagnostics = self.hook("codex-submit", value)
                self.assertEqual(result, {})
                self.assertIn("Host hook input must be a JSON object", diagnostics)
                with self.assertRaises(SystemExit) as interrupted:
                    self.hook("codex-authorize", value)
                self.assertEqual(interrupted.exception.code, 2)
        self.assertEqual(self.controller.store.all(), [])

    def test_missing_grants_and_lost_replies_use_the_durable_backend_fence(self):
        instruction = self.submit()
        invocation = {"request_version": "1", "request_id": "lost-reply-start", "instruction_id": instruction,
                      "executor": "opencode", "model": "test", "task": "Update a parser",
                      "parent_repo": str(self.project), "parent_worktree": str(self.project), "mode": "write"}
        with self.assertRaises(ValueError) as refused:
            self.controller.handle("start", invocation)
        self.assertIn("single-use dispatch authorization", str(refused.exception))
        self.assertEqual(self.controller.store.all(), [])

        def authorize_then_disconnect(operation, payload):
            self.controller.handle(operation, payload)
            raise ConnectionResetError("authorization reply was lost")

        with patch("router.hooks.request", side_effect=authorize_then_disconnect), self.assertRaises(SystemExit):
            self.authorize("start", invocation)
        # The caller deliberately delivers the same literal request, never an alternative launch.
        first = self.controller.handle("start", invocation)
        self.assertEqual(self.controller.handle("start", invocation)["task_id"], first["task_id"])
        self.assertEqual(len(self.controller.store.all()), 1)
        task_id = first["task_id"]
        self.controller.store.update(task_id, lambda task: task.update(status="accepted", attempts=1,
                                                                      execution_stopped=True, final_tree="tree"))
        before = self.controller.store.get(task_id)
        self.assertEqual(self.controller.handle("start", invocation)["status"], "accepted")
        for operation, fields in (("review", {"expected_attempt": 1, "outcome": "accepted"}),
                                  ("cancel", {}), ("close", {"disposition": "integrated", "keep_artifacts": True})):
            with self.subTest(operation=operation), self.assertRaises(ValueError) as refused:
                self.controller.handle(operation, {"task_id": task_id, "request_id": "ungranted-" + operation, **fields})
            self.assertIn("single-use dispatch authorization", str(refused.exception))
            self.assertEqual(self.controller.store.get(task_id), before)
        self.assertEqual(len(self.controller.store.all()), 1)
        self.assertEqual(self.controller.store.get(task_id), before)  # Attempt count and accepted state cannot change.


if __name__ == "__main__":
    unittest.main()
