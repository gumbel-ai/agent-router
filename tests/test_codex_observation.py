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

from test_control import observe_every_session, settings

from router import observer_hook
from router.cli import fleet_table, sessions_table
from router.daemon import Controller
from router.native import STALE_AFTER_MS
from router.sessions import MAX_INFLIGHT_AGE_MS

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
        observe_every_session(self)
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
        cases = (("known root", True, False, False), ("missing root", False, False, False),
                 ("historical unknown", False, False, True), ("tool uncertainty", False, True, False))
        for name, root_observed, live_tool, historical in cases:
            with self.subTest(name=name):
                self.control = Controller(settings(self.root / "repo"), {}, self.root / f"restart-{name}")
                self.control.start_lifecycle_writer()
                self.addCleanup(self.control.stop_lifecycle_writer)
                self.clock = int(time.time() * 1000) - 10_000 - (STALE_AFTER_MS if historical else 0)
                if root_observed:
                    self.hook("SessionStart", source="startup")
                self.hook("SubagentStart", turn_id="t2", agent_id=CHILD, agent_type="default")
                if live_tool:
                    self.hook("PreToolUse", turn_id="t2", agent_id=CHILD, tool_name="Bash",
                              tool_use_id="restart-tool", tool_input={})
                    tool_at = self.session()["last_event_at"]
                self.hook("SubagentStop", turn_id="t2", agent_id=CHILD, agent_type="default")
                latest = self.session()["last_event_at"]
                deadline = tool_at + MAX_INFLIGHT_AGE_MS if live_tool else latest + STALE_AFTER_MS
                self.control.sessions.reconcile_restart()
                self.control.native.reconcile_restart()
                for offset in (-15, -10):
                    # Older and tied pre-restart evidence cannot resolve the restart.
                    self.clock = latest + offset
                    self.hook("Stop", turn_id="t1", stop_hook_active=False)
                    self.assertEqual(self.agent()["status"], "unknown")
                    self.assertEqual(self.session()["status"], "unknown")
                    self.assertIn("restart", self.session()["visibility_reason"].lower())
                if live_tool:
                    # A tied notice may add a waiter without resolving the unknown state.
                    self.clock = latest - 10
                    self.hook("PermissionRequest", agent_id=CHILD, turn_id="t2")
                    self.assertEqual(self.session()["status"], "unknown")
                    self.assertTrue(self.session()["needs_input"])
                for _ in range(2):
                    self.control.sessions.reconcile_restart()
                    self.control.native.reconcile_restart()
                    with self.control.store.connect() as db:
                        stored = json.loads(db.execute(
                            "SELECT data FROM sessions WHERE source='codex' AND session_id=?", (SESSION,)
                        ).fetchone()[0])
                    self.assertEqual(stored["restart_uncertain_until"], deadline)
                    self.assertEqual(stored["last_event_at"], latest)
                actual_now = int(time.time() * 1000)
                checks = ((actual_now, None if actual_now <= deadline else 0),
                          (deadline - 1, None), (deadline, None), (deadline + 1, 0))
                for checked_at, active in checks:
                    healthy = {"logical_cpus": 8, "load_1m": 0, "cpu_busy_pct": 0,
                               "memory_available_bytes": 32 * 1024**3,
                               "disk_available_bytes": 64 * 1024**3,
                               "observed_at": checked_at - 1000, "partial": False}
                    with self.subTest(checked_at=checked_at), \
                            patch("router.capacity.time.time", return_value=checked_at / 1000), \
                            patch("router.sessions.now_ms", return_value=checked_at), \
                            patch("router.native.now_ms", return_value=checked_at), \
                            patch.object(self.control.capacity, "_latest", healthy):
                        snapshot = self.control.handle("snapshot", {})
                        self.assertEqual(snapshot["capacity"]["active_claude_sessions"], active)
                        self.assertEqual(snapshot["capacity"]["capacity_state"],
                                         "unknown" if active is None else "available")
                self.clock = int(time.time() * 1000) - 1000
                self.hook("Stop", turn_id="t1", stop_hook_active=False)
                self.hook("SubagentStop", turn_id="t2", agent_id=CHILD, agent_type="default")
                self.assertEqual(self.session()["status"], "idle")
                self.assertEqual(self.control.sessions.fleet_records(0)[1], 0)
                self.assertEqual(self.agent()["status"], "idle")

    def test_delayed_root_stop_keeps_clock_rollback_uncertain(self):
        for child_running in (False, True):
            with self.subTest(child_running=child_running):
                self.control = Controller(settings(self.root / "repo"), {}, self.root / f"clock-{child_running}")
                self.control.start_lifecycle_writer()
                self.addCleanup(self.control.stop_lifecycle_writer)
                self.hook("SessionStart", source="startup")
                self.hook("SubagentStart", turn_id="t2", agent_id=CHILD, agent_type="default")
                if not child_running:
                    self.hook("SubagentStop", turn_id="t2", agent_id=CHILD, agent_type="default")
                latest = self.clock
                self.clock = latest - 15
                with patch("router.sessions.now_ms", return_value=latest - 1):
                    self.hook("Stop", turn_id="t1", stop_hook_active=False)
                    session = self.session()
                    self.assertEqual(session["status"], "unknown")
                    self.assertIn("clock", session["visibility_reason"])
                    self.assertIsNone(self.control.sessions.fleet_records(10)[1])
                # No new hook: the stored live child must survive the clock catching up.
                with patch("router.sessions.now_ms", return_value=latest + 1), \
                        patch("router.native.now_ms", return_value=latest + 1):
                    snapshot = self.control.handle("snapshot", {})
                    self.assertEqual(snapshot["sessions"][0]["status"], "running" if child_running else "idle")
                    self.assertEqual(snapshot["capacity"]["active_claude_sessions"], int(child_running))
                    self.assertEqual(snapshot["capacity"]["active_agents"], int(child_running))
                self.clock = latest + 100

    def test_missing_root_uncertainty_reaches_capacity_until_freshness_expires(self):
        for waited in (False, True):
            self.control = Controller(settings(self.root / "repo"), {}, self.root / f"missing-root-{waited}")
            self.control.start_lifecycle_writer()
            self.addCleanup(self.control.stop_lifecycle_writer)
            if waited:
                self.hook("PermissionRequest", agent_id=CHILD, turn_id="t2")
            self.hook("SubagentStop", agent_id=CHILD, turn_id="t2")
            event_at = self.clock
            healthy = {"logical_cpus": 8, "load_1m": 0, "cpu_busy_pct": 0,
                       "memory_available_bytes": 32 * 1024**3, "disk_available_bytes": 64 * 1024**3,
                       "observed_at": int(time.time() * 1000) - 1000, "partial": False}
            for age in (1, STALE_AFTER_MS - 1, STALE_AFTER_MS, STALE_AFTER_MS + 1):
                fresh = age <= STALE_AFTER_MS
                with self.subTest(waited=waited, age=age), \
                        patch("router.sessions.now_ms", return_value=event_at + age), \
                        patch("router.native.now_ms", return_value=event_at + age), \
                        patch.object(self.control.capacity, "_latest", healthy):
                    snapshot = self.control.handle("snapshot", {})
                    self.assertEqual(snapshot["sessions"][0]["status"], "unknown")
                    self.assertEqual(snapshot["capacity"]["active_claude_sessions"], None if fresh else 0)
                    self.assertEqual(snapshot["capacity"]["active_agents"], 0)
                    self.assertEqual(snapshot["capacity"]["capacity_state"], "unknown" if fresh else "available")
                    self.assertEqual("session_active_count" in snapshot.get("errors", {}), fresh)

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

    def test_agent_events_stamped_after_session_end_stay_stopped_until_resume(self):
        for seen_before_end in (False, True):
            with self.subTest(seen_before_end=seen_before_end):
                self.control = Controller(settings(self.root / "repo"), {}, self.root / f"state-{seen_before_end}")
                self.control.start_lifecycle_writer()
                self.addCleanup(self.control.stop_lifecycle_writer)
                self.hook("SessionStart", source="startup")
                if seen_before_end:
                    self.hook("SubagentStart", turn_id="t1", agent_id=CHILD, agent_type="default")
                self.hook("SessionEnd", reason="other")
                # Async hooks are stamped when they run, so these arrive later than the end.
                for late in ("SubagentStart", "PreToolUse"):
                    self.hook(late, turn_id="t2", agent_id=CHILD, agent_type="default", tool_name="Bash",
                              tool_input={}, tool_use_id="exec-9")
                    self.assertEqual(self.agent()["status"], "stopped")
                self.hook("SessionStart", source="resume")
                # A late event stamped between the end and the resume is not resumed work.
                resumed = self.clock
                self.hook("Stop", stop_hook_active=False)
                self.clock = resumed - 15  # hook() advances 10 ms: stamped 5 ms before resume.
                self.hook("PreToolUse", turn_id="t2", agent_id=CHILD, agent_type="default", tool_name="Bash",
                          tool_input={}, tool_use_id="exec-9b")
                self.assertEqual(self.agent()["status"], "stopped")
                self.assertEqual(self.session()["status"], "idle")
                self.clock = resumed + 20
                self.hook("SessionStart", source="compact")  # Must not move the resume boundary.
                self.clock = resumed
                self.hook("PreToolUse", turn_id="t3", agent_id=CHILD, agent_type="default", tool_name="Bash",
                          tool_input={}, tool_use_id="exec-10")
                self.assertEqual(self.agent()["status"], "running")

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
        sibling = {"agent_id": "worker-b", "turn_id": "t3"}
        cases = {
            "root tool after stop": ([("Stop", 30, {}), ("PreToolUse", 40, {"tool_use_id": "r1"}),
                                      ("PostToolUse", 50, {"tool_use_id": "r1"}), ("SubagentStop", 60, child)],
                                     "running", False, 0, 1),
            "delayed root stop": ([("PreToolUse", 45, {**child, "tool_use_id": "c1"}),
                                   ("PostToolUse", 50, {**child, "tool_use_id": "c1"}),
                                   ("SubagentStop", 60, child), ("Stop", 40, {})], "idle", False, 0, 0),
            "delayed root prompt": ([("Stop", 30, {}), ("SubagentStop", 60, child),
                                     ("UserPromptSubmit", 40, {})], "running", False, 0, 1),
            "delayed session start": ([("Stop", 30, {}), ("SubagentStop", 60, child),
                                       ("SessionStart", 40, {"source": "compact"})], "running", False, 0, 1),
            "older child tool": ([("PreToolUse", 35, {**child, "tool_use_id": "c1"}),
                                  ("SubagentStop", 60, child), ("Stop", 40, {})], "idle", False, 0, 0),
            "newer child tool": ([("PreToolUse", 50, {**child, "tool_use_id": "c1"}),
                                  ("SubagentStop", 60, child), ("Stop", 40, {})], "running", False, 1, 1),
            "waiting with live child tool": ([("PreToolUse", 50, {**child, "tool_use_id": "c1"}),
                                              ("PermissionRequest", 60, child), ("Stop", 40, {})],
                                             "idle", True, 1, 1),
            "older child waiter": ([("PermissionRequest", 35, child), ("SubagentStop", 60, sibling),
                                    ("Stop", 40, {})], "idle", False, 0, 0),
            "newer child waiter": ([("SubagentStop", 60, child), ("PermissionRequest", 50, sibling),
                                    ("Stop", 40, {})], "idle", True, 0, 1),
            "running sibling": ([("SubagentStart", 50, sibling), ("SubagentStop", 60, child),
                                 ("Stop", 40, {})], "running", False, 0, 1),
            "delayed sibling stop": ([("SubagentStart", 35, sibling), ("Stop", 40, {}),
                                      ("SubagentStop", 60, child), ("SubagentStop", 50, sibling)],
                                     "idle", False, 0, 0),
            "terminal prompt tie": ([("SubagentStop", 35, child), ("Stop", 40, {}),
                                     ("UserPromptSubmit", 40, {})], "idle", False, 0, 0),
            "terminal start tie": ([("SubagentStop", 35, child), ("Stop", 40, {}),
                                    ("SessionStart", 40, {"source": "compact"})], "idle", False, 0, 0),
            "terminal tie": ([("Stop", 40, {}), ("SubagentStop", 60, child),
                              ("UserPromptSubmit", 40, {})], "idle", False, 0, 0),
        }
        cases.update({
            "reset behind newer root activity": ([
                ("PreToolUse", 35, {**child, "tool_use_id": "c1"}),
                ("PreToolUse", 50, {"tool_use_id": "r1"}),
                ("PostToolUse", 55, {"tool_use_id": "r1"}),
                ("SubagentStop", 60, child), ("Stop", 40, {})], "running", False, 0, 1),
            "tied child completion last": ([("Stop", 40, {}), ("SubagentStop", 40, child)],
                                           "idle", False, 0, 0),
            "tied root completion last": ([("SubagentStop", 40, child), ("Stop", 40, {})],
                                           "idle", False, 0, 0),
            "replayed stop with live child": ([("Stop", 40, {}), ("Stop", 40, {})],
                                              "running", False, 0, 1),
            "replayed stop preserves tied waiter": ([
                ("SubagentStop", 35, child), ("Stop", 40, {}),
                ("PermissionRequest", 40, child), ("Stop", 40, {})], "idle", True, 0, 1),
            "replayed stop preserves newer waiter": ([
                ("SubagentStop", 35, child), ("Stop", 40, {}), ("UserPromptSubmit", 50, {}),
                ("PermissionRequest", 60, child), ("Stop", 40, {})], "idle", True, 0, 1),
        })
        reset_cases = {"reset behind newer root activity"}
        for reset, fields in (("UserPromptSubmit", {}), ("SessionStart", {"source": "compact"})):
            name = f"reset behind newer root activity {reset}"
            reset_cases.add(name)
            cases[name] = ([
                ("PreToolUse", 35, {**child, "tool_use_id": "c1"}),
                ("PreToolUse", 50, {"tool_use_id": "r1"}),
                ("PostToolUse", 55, {"tool_use_id": "r1"}),
                ("SubagentStop", 60, child), (reset, 40, fields)], "running", False, 0, 1)
        cases.update({
            "delayed sibling start": ([("Stop", 40, {}), ("SubagentStop", 60, child),
                                       ("SubagentStart", 50, sibling)], "running", False, 0, 1),
            "delayed sibling activity": ([
                ("SubagentStart", 35, sibling), ("Stop", 40, {}), ("PermissionRequest", 45, sibling),
                ("SubagentStop", 60, child), ("UserPromptSubmit", 50, sibling)], "running", False, 0, 1),
            "superseded sibling start": ([
                ("SubagentStart", 35, sibling), ("Stop", 40, {}), ("SubagentStop", 55, sibling),
                ("SubagentStop", 60, child), ("SubagentStart", 50, sibling)], "idle", False, 0, 0),
        })
        for opening, fields in (("UserPromptSubmit", {}), ("SessionStart", {"source": "compact"})):
            cases[f"replayed {opening} preserves tied waiter"] = ([
                ("SubagentStop", 35, child), ("Stop", 40, {}), (opening, 50, fields),
                ("PermissionRequest", 50, child), (opening, 50, fields)], "idle", True, 0, 1)
        for first, second in (("Stop", "SubagentStart"), ("SubagentStart", "Stop")):
            cases[f"tied live child {first} then {second}"] = ([
                ("SubagentStop", 35, child),
                (first, 40, sibling if first == "SubagentStart" else {}),
                (second, 40, sibling if second == "SubagentStart" else {})], "running", False, 0, 1)
        for terminal in ("Stop", "Interrupt"):
            for first, second in ((terminal, "SubagentStop"), ("SubagentStop", terminal)):
                cases[f"live sibling {first} then {second}"] = ([
                    ("SubagentStart", 35, sibling),
                    (first, 40, child if first == "SubagentStop" else {}),
                    (second, 40, child if second == "SubagentStop" else {})], "running", False, 0, 1)
            for opening, fields in (("UserPromptSubmit", {}), ("SessionStart", {"source": "compact"})):
                cases[f"{terminal} {opening} tie after notice"] = ([
                    ("SubagentStop", 35, child), (terminal, 40, {}),
                    ("PermissionRequest", 40, child), (opening, 40, fields)], "idle", True, 0, 1)
                cases[f"{terminal} {opening} tie after child activity"] = ([
                    (terminal, 40, {}), ("SubagentStop", 40, child),
                    (opening, 40, fields)], "idle", False, 0, 0)
                cases[f"{terminal} {opening} tie after sibling start"] = ([
                    ("SubagentStop", 35, child), (terminal, 40, {}), ("SubagentStart", 40, sibling),
                    (opening, 40, fields), ("SubagentStop", 60, sibling)], "idle", False, 0, 0)
        for age in (STALE_AFTER_MS - 1, STALE_AFTER_MS, STALE_AFTER_MS + 1):
            fresh = age <= STALE_AFTER_MS
            cases[f"child freshness {age}"] = (
                [("SubagentStart", 90 - age, sibling), ("SubagentStop", 60, child), ("Stop", 40, {})],
                "running" if fresh else "idle", False, 0, 1 if fresh else 0,
            )
        cases = {name: (True, *case) for name, case in cases.items()}
        cases.update({
            "missing root child completion": (False, [("SubagentStart", 35, child),
                                                       ("SubagentStop", 60, child)], "unknown", False, 0, None),
            "missing root lone completion": (False, [("SubagentStop", 60, child)], "unknown", False, 0, None),
            "missing root stale child": (False, [("SubagentStart", -STALE_AFTER_MS, child),
                                                  ("SubagentStop", 60, child)], "unknown", False, 0, None),
            "missing root live sibling": (False, [("SubagentStart", 50, sibling),
                                                   ("SubagentStop", 60, child)], "running", False, 0, 1),
            "missing root live tool": (False, [("PreToolUse", 50, {**child, "tool_use_id": "c1"}),
                                                ("SubagentStop", 60, child)], "running", False, 1, 1),
            "missing root newer waiter": (False, [("PermissionRequest", 60, sibling),
                                                   ("SubagentStop", 50, child)], "idle", True, 0, 1),
            "missing root waiting with live tool": (False, [
                ("PreToolUse", 50, {**child, "tool_use_id": "c1"}), ("PermissionRequest", 60, sibling),
                ("SubagentStop", 70, child)], "idle", True, 1, 1),
        })
        for name, (root_observed, events, status, waiting, tools, active) in cases.items():
            for reordered in (False, True):
                self.clock = int(time.time() * 1000) - 10_000
                with self.subTest(name=name, reordered=reordered), \
                        patch("router.sessions.now_ms", return_value=self.clock + 100), \
                        patch("router.native.now_ms", return_value=self.clock + 100):
                    self.control = Controller(settings(self.root / "repo"), {},
                                              self.root / f"state-{name}-{reordered}")
                    self.control.start_lifecycle_writer()
                    self.addCleanup(self.control.stop_lifecycle_writer)
                    start = self.clock
                    if root_observed:
                        self.hook("SessionStart", source="startup")
                        self.hook("UserPromptSubmit", turn_id="t1", prompt=CONTENT)
                        self.hook("SubagentStart", agent_type="default", **child)
                    ordered = events if reordered else sorted(events, key=lambda item: item[1])
                    for hook_name, offset, fields in ordered:
                        self.clock = start + offset
                        self.hook(hook_name, tool_name="Bash", tool_input={}, tool_response="", **fields)
                    snapshot = self.control.handle("snapshot", {})
                    session = next(row for row in snapshot["sessions"] if row["session_id"] == SESSION)
                    self.assertEqual(snapshot["capacity"]["active_claude_sessions"], active)
                    self.assertEqual(session["last_event_at"], start + max(item[1] for item in events) + 10)
                    self.assertEqual((session["status"], session["needs_input"], session["inflight_tool_count"]),
                                     (status, waiting, tools))
                    with patch("router.sessions.now_ms", return_value=start + 100):
                        self.assertEqual(self.control.sessions.fleet_records(10)[1], active)
                    if name in reset_cases:
                        expired = session["last_event_at"] + STALE_AFTER_MS + 1
                        with patch("router.sessions.now_ms", return_value=expired), \
                                patch("router.native.now_ms", return_value=expired):
                            stale = self.control.handle("snapshot", {})
                            self.assertEqual(stale["sessions"][0]["status"], "unknown")
                            self.assertEqual(stale["capacity"]["active_claude_sessions"], 0)
                    if name == "older child tool":
                        self.clock = start + 35
                        self.hook("PreToolUse", **child, tool_use_id="c1", tool_name="Bash", tool_input={})
                        self.assertEqual(self.session()["inflight_tool_count"], 0)
                    self.control.stop_lifecycle_writer()

    def test_delayed_root_reset_preserves_unresolved_tool_uncertainty(self):
        child = {"agent_id": CHILD, "turn_id": "t2"}
        cases = {"older": ("idle", False, 0), "newer": ("unknown", True, 1), "unorderable": ("unknown", True, 0)}
        for timing, expected in cases.items():
            with self.subTest(timing=timing), patch("router.sessions.MAX_EXACT_INFLIGHT_TOOLS", 1):
                self.control = Controller(settings(self.root / "repo"), {}, self.root / f"overflow-{timing}")
                self.control.start_lifecycle_writer()
                self.addCleanup(self.control.stop_lifecycle_writer)
                start = self.clock
                self.hook("SessionStart", source="startup")
                self.hook("SubagentStart", agent_type="default", **child)
                for number in range(2):
                    self.clock = start + (50 if timing == "newer" else 30) + number
                    self.hook("PreToolUse", **child, tool_use_id=f"tool-{number}", tool_name="Bash", tool_input={})
                self.clock = start + 60
                self.hook("SubagentStop", **child)
                if timing == "unorderable":
                    # An unresolved overflow without a deadline cannot be ordered against a root reset.
                    with self.control.store.transaction() as db:
                        db.execute("""UPDATE sessions SET data=json_set(data,'$.tool_correlation_partial_until',NULL)
                            WHERE source='codex' AND session_id=?""", (SESSION,))
                self.clock = start + 40
                self.hook("Stop", stop_hook_active=False)
                session = self.session()
                self.assertEqual((session["status"], session["tool_visibility_partial"], session["inflight_tool_count"]),
                                 expected)
                with patch("router.sessions.now_ms", return_value=start + 100):
                    self.assertEqual(self.control.sessions.fleet_records(10)[1], 0 if timing == "older" else None)
                self.clock = start + 200

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
