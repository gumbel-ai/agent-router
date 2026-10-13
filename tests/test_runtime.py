import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_control import settings

from router.child import launch
from router.daemon import Controller, Runtime
from router.process import UncertainProcess, owned_state, stop_owned
from router.snapshot import apply_delta, git


class RuntimeHarness:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / ".gitignore").write_text(".venv/\n.env\n")
        (self.repo / "tracked.txt").write_text("baseline\n")
        (self.repo / "opencode.json").write_text(json.dumps({"permission": {"*": "allow", "external_directory": "deny"}}))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "initial")
        self.config = settings(self.repo, command=[sys.executable, str(Path(__file__).with_name("fixture_executor.py"))])
        self.control = Controller(self.config, os.environ.copy(), self.root / "state")
        self.runtime = Runtime(self.control)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.instruction = self.control.handle("submit", {"session_id": "S", "prompt_id": "P"})["instruction_id"]

    def mutate(self, operation, payload):
        self.control.handle("authorize", {"session_id": "S", "prompt_id": "P", "operation": operation, "invocation": payload})
        return self.control.handle(operation, payload)

    def start(self, prompt="flood", request="R"):
        return self.mutate("start", {"request_version": "1", "request_id": request, "instruction_id": self.instruction,
                                     "executor": "opencode", "model": "test", "task": prompt,
                                     "parent_repo": str(self.repo), "parent_worktree": str(self.repo), "mode": "write"})["task_id"]

    def wait(self, task_id, states, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            task = self.control.handle("status", {"task_id": task_id})
            if task["status"] in states:
                return task
            time.sleep(0.04)
        self.fail(f"Task did not reach {states}: {task}")

class RuntimeTests(RuntimeHarness, unittest.TestCase):
    def test_full_cycle_same_session_and_cleanup(self):
        (self.repo / "tracked.txt").write_text("dirty parent\n")
        task_id = self.start()
        first = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(first["status"], "awaiting_review", first)
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["cost"], 0.25)
        self.assertEqual(first["usage"]["cache"]["read"], 1)
        self.assertEqual((self.repo / "tracked.txt").read_text(), "dirty parent\n")
        self.assertFalse((self.repo / "result.txt").exists())
        self.config.models["test"] = "provider/changed-registry-entry"
        self.mutate("review", {"task_id": task_id, "request_id": "reject", "expected_attempt": 1,
                               "outcome": "rejected", "feedback": "Correct the result"})
        second = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(second["status"], "awaiting_review", second)
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["cost"], .5)
        self.assertEqual(second["usage"]["cache"]["read"], 2)
        self.assertEqual(second["elapsed_ms"], sum(attempt["duration_ms"] for attempt in second["attempt_history"]))
        sessions = json.loads((Path(second["worktree"]) / "session.json").read_text())
        self.assertEqual(sessions["sessions"], [first["child_session_id"]] * 2)
        self.assertEqual(sessions["models"], ["provider/exact"] * 2)
        self.assertEqual(second["worktree"], first["worktree"])
        with self.assertRaisesRegex(ValueError, "ceiling"):
            self.mutate("review", {"task_id": task_id, "request_id": "over-ceiling", "expected_attempt": 2,
                                   "outcome": "rejected", "feedback": "One more attempt"})
        self.assertEqual(self.control.handle("status", {"task_id": task_id})["attempts"], 2)
        (self.repo / "later.txt").write_text("later edit\n")
        self.mutate("review", {"task_id": task_id, "request_id": "accept", "expected_attempt": 2, "outcome": "accepted"})
        apply_delta(self.repo, Path(second["patch"]))
        self.assertEqual((self.repo / "result.txt").read_text(), "corrected\n")
        self.assertEqual((self.repo / "later.txt").read_text(), "later edit\n")
        self.mutate("close", {"task_id": task_id, "request_id": "close", "disposition": "integrated"})
        end = time.monotonic() + 5
        while time.monotonic() < end:
            closed = self.control.handle("status", {"task_id": task_id})
            if not closed["cleanup_pending"]:
                break
            time.sleep(0.04)
        self.assertFalse(closed["cleanup_pending"], closed)
        self.assertFalse(Path(second["worktree"]).exists())
        self.assertEqual(closed["execution_outcome"], "completed")

    def test_timeout_preserves_work_and_salvage(self):
        self.config.attempt_timeout_seconds = 1.5
        task_id = self.start("silent hang")
        failed = self.wait(task_id, {"failed", "interrupted"})
        self.assertEqual(failed["status"], "failed", failed)
        self.assertEqual(failed["failure_reason"], "router-timeout")
        self.assertEqual((Path(failed["worktree"]) / "result.txt").read_text(), "first\n")
        self.assertIn("final_tree", failed)
        self.assertNotIn("last_event_at", failed)
        accepted = self.mutate("review", {"task_id": task_id, "request_id": "salvage", "expected_attempt": 1, "outcome": "accepted"})
        self.assertEqual(accepted["execution_outcome"], "failed")
        self.assertEqual(accepted["attempts"], 1)

    def test_gate_registration_failure_releases_no_command(self):
        marker = self.root / "must-not-exist"
        def refuse(record):
            self.assertEqual(owned_state(record), "live")
            self.assertFalse(marker.exists())
            raise ValueError("registration failed")
        with self.assertRaisesRegex(ValueError, "registration failed"):
            launch([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                   self.root, os.environ.copy(), self.root / "gate", refuse)
        self.assertFalse(marker.exists())

    def test_identity_mismatch_never_signals(self):
        records = []
        process, record = launch([sys.executable, "-c", "import time; time.sleep(60)"],
                                 self.root, os.environ.copy(), self.root / "group", records.append)
        self.addCleanup(lambda: stop_owned(record, process))
        with self.assertRaises(UncertainProcess):
            stop_owned({**record, "creation_identity": "another-process"}, process)
        self.assertIsNone(process.poll())

    def test_admission_before_preparation_and_twelve_task_monitoring(self):
        self.config.concurrency_cap = 3
        tasks = [self.start("hang", f"fanout-{i}") for i in range(5)]
        for task_id in tasks[:3]:
            self.wait(task_id, {"running"})
        for task_id in tasks[3:]:
            queued = self.control.handle("status", {"task_id": task_id})
            self.assertEqual(queued["status"], "created")
            self.assertNotIn("worktree", queued)
            self.mutate("cancel", {"task_id": task_id, "request_id": "cancel-" + task_id})
            self.mutate("close", {"task_id": task_id, "request_id": "close-" + task_id, "disposition": "discarded"})
        self.config.concurrency_cap = 12
        tasks = tasks[:3] + [self.start("hang", f"extra-{i}") for i in range(9)]
        for task_id in tasks:
            self.wait(task_id, {"running"})
        rows = self.control.handle("status", {})
        self.assertEqual(len(rows), 12)
        self.assertEqual({row["status"] for row in rows}, {"running"})
        self.assertEqual(self.control.handle("status", {"filter": "needs_decision"}), [])
        self.assertEqual(len({self.control.store.get(task)["worktree"] for task in tasks}), 12)
        for task_id in tasks:
            self.mutate("cancel", {"task_id": task_id, "request_id": "stop-" + task_id})
        for task_id in tasks:
            self.assertEqual(self.wait(task_id, {"cancelled", "interrupted"})["status"], "cancelled")

    def test_setup_creates_own_environment_and_runs_repository_tests(self):
        project_tests = self.repo / "tests"
        project_tests.mkdir()
        (project_tests / "test_project.py").write_text(
            "import sys, unittest\nfrom pathlib import Path\n"
            "class ProjectTest(unittest.TestCase):\n"
            " def test_result(self):\n"
            "  self.assertEqual(Path('result.txt').read_text(), 'first\\n')\n"
            "  Path('environment.txt').write_text(sys.prefix)\n")
        self.config.repositories[str(self.repo.resolve())]["setup_command"] = f"{sys.executable} -m venv .venv"
        task_id = self.start("real setup")
        result = self.wait(task_id, {"awaiting_review", "failed", "interrupted"}, seconds=40)
        self.assertEqual(result["status"], "awaiting_review", result)
        child = Path(result["worktree"])
        self.assertTrue((child / ".venv" / "bin" / "python").exists())
        self.assertFalse((self.repo / ".venv").exists())
        self.assertEqual(Path((child / "environment.txt").read_text()), child / ".venv")
        self.assertNotIn(".venv/", Path(result["patch"]).read_text())

    def test_cancel_during_setup_prevents_child_launch(self):
        self.config.repositories[str(self.repo.resolve())]["setup_command"] = "touch setup-generated.txt; sleep 60"
        task_id = self.start("must not launch")
        end = time.monotonic() + 10
        while time.monotonic() < end:
            task = self.control.handle("status", {"task_id": task_id})
            if task.get("phase") == "setup" and (Path(task["worktree"]) / "setup-generated.txt").exists():
                break
            time.sleep(0.03)
        self.assertEqual(task.get("phase"), "setup")
        self.mutate("cancel", {"task_id": task_id, "request_id": "cancel-setup"})
        stopped = self.wait(task_id, {"cancelled", "interrupted"})
        self.assertEqual(stopped["status"], "cancelled", stopped)
        self.assertEqual(stopped["attempts"], 0)
        self.assertFalse((Path(stopped["worktree"]) / "session.json").exists())
        self.assertIn("final_tree", stopped)

    def test_final_error_event_is_collected_after_process_exit(self):
        task_id = self.start("final error")
        failed = self.wait(task_id, {"failed", "interrupted", "awaiting_review"})
        self.assertEqual(failed["status"], "failed", failed)
        self.assertEqual(failed["exit_code"], 0)
        self.assertTrue(failed["execution_error"])
        self.assertIn("final_tree", failed)

    def test_uncertain_execution_reserves_admission_until_cancel(self):
        self.runtime.stop()
        self.config.concurrency_cap = 1
        uncertain_id = self.start("never dispatched", "uncertain")
        self.control.store.update(uncertain_id, lambda t: t.update(status="interrupted", execution_stopped=False))
        self.runtime = Runtime(self.control)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        queued_id = self.start("queued", "queued")
        time.sleep(0.2)
        queued = self.control.handle("status", {"task_id": queued_id})
        self.assertEqual(queued["status"], "created")
        self.assertNotIn("worktree", queued)
        self.assertEqual([row["task_id"] for row in self.control.handle("status", {"filter": "needs_decision"})], [uncertain_id])
        self.mutate("cancel", {"task_id": uncertain_id, "request_id": "resolve"})
        self.wait(uncertain_id, {"cancelled"})
        finished = self.wait(queued_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(finished["status"], "awaiting_review", finished)

    def test_close_replay_finishes_partial_cleanup_without_touching_other_worktree(self):
        task_id = self.start("cleanup")
        result = self.wait(task_id, {"awaiting_review", "failed", "interrupted"})
        self.assertEqual(result["status"], "awaiting_review", result)
        other = self.root / "other-worktree"
        git(self.repo, "worktree", "add", "--force", str(other), result["branch"])
        close = {"task_id": task_id, "request_id": "discard", "disposition": "discarded"}
        self.mutate("close", close)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            partial = self.control.handle("status", {"task_id": task_id})
            if partial.get("cleanup_error"):
                break
            time.sleep(0.03)
        self.assertTrue(partial["cleanup_pending"])
        self.assertTrue(partial["cleanup_error"])
        self.assertTrue(other.exists())
        git(self.repo, "worktree", "remove", "--force", str(other))
        self.control.handle("close", close)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            closed = self.control.handle("status", {"task_id": task_id})
            if not closed["cleanup_pending"]:
                break
            time.sleep(0.03)
        self.assertFalse(closed["cleanup_pending"], closed)
        self.assertFalse(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/router/").strip())


if __name__ == '__main__':
    unittest.main()
