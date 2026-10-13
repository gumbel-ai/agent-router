import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from router.config import Settings
from router.daemon import Controller


def settings(root: Path, cap: int = 3, timeout: float = 30, attempts: int = 2, command=None) -> Settings:
    return Settings(AGENT_ROUTER_CONCURRENCY_CAP=cap, AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS=timeout,
                    AGENT_ROUTER_MAX_ATTEMPTS=attempts, AGENT_ROUTER_OPENCODE_COMMAND=command or ["opencode"],
                    AGENT_ROUTER_MODELS={"test": "provider/exact"},
                    AGENT_ROUTER_REPOSITORIES={str(root): {"setup_command": "true"}})


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-router-control-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.router = Controller(settings(self.root / "repo"), {}, self.root / "state")

    def submission(self, session="S", prompt="P"):
        return self.router.handle("submit", {"session_id": session, "prompt_id": prompt, "parent_cli": "claude"})["instruction_id"]

    def payload(self, instruction, request="R"):
        return {"request_version": "1", "request_id": request, "instruction_id": instruction,
                "executor": "opencode", "model": "test", "task": "Implement a test",
                "parent_repo": str(self.root), "parent_worktree": str(self.root), "mode": "write"}

    def authorize(self, payload, session="S", prompt="P", operation="start"):
        return self.router.handle("authorize", {"session_id": session, "prompt_id": prompt,
                                               "operation": operation, "invocation": payload})

    def test_binding_consumption_and_idempotent_replay(self):
        first = self.submission()
        payload = self.payload(first)
        with self.assertRaisesRegex(ValueError, "authorization"):
            self.router.handle("start", payload)
        self.authorize(payload)
        changed = {**payload, "task": "changed"}
        with self.assertRaises(ValueError):
            self.router.handle("start", changed)
        # Concurrent mutation is intentional: prove transactional consumption.
        with ThreadPoolExecutor(max_workers=5) as pool:
            copies = list(pool.map(lambda _: self.router.handle("start", payload), range(5)))
        self.assertEqual(len({copy["task_id"] for copy in copies}), 1)
        task = copies[0]
        second = self.submission(prompt="P2")
        self.assertEqual(self.router.handle("start", payload)["task_id"], task["task_id"])
        with self.assertRaises(ValueError):
            self.router.handle("start", changed)
        with self.assertRaises(ValueError):
            self.authorize(self.payload(first, "stale"), prompt="P2")
        with self.assertRaises(ValueError):
            self.authorize(self.payload(second, "other-session"), session="other")
        for request in ("parallel-a", "parallel-b"):
            parallel = self.payload(second, request)
            self.authorize(parallel, prompt="P2")
            self.router.handle("start", parallel)
        self.assertEqual(len(self.router.store.all()), 3)
        with self.router.store.connect() as db:
            self.assertEqual(db.execute("SELECT sum(consumed) FROM authorizations").fetchone()[0], 3)
            self.assertNotIn("prompt", [row[1] for row in db.execute("PRAGMA table_info(submissions)")])

    def test_review_guards_and_failed_artifact_salvage(self):
        instruction = self.submission()
        invocation = self.payload(instruction)
        self.authorize(invocation)
        task_id = self.router.handle("start", invocation)["task_id"]
        self.router.store.update(task_id, lambda t: t.update(status="failed", execution_outcome="failed",
                                                           execution_stopped=True, failure_reason="router-timeout", attempts=1))
        review = {"task_id": task_id, "request_id": "accept", "expected_attempt": 1, "outcome": "accepted"}
        self.authorize(review, operation="review")
        with self.assertRaisesRegex(ValueError, "final tree"):
            self.router.handle("review", review)
        self.router.store.update(task_id, lambda t: t.update(final_tree="tree"))
        accepted = self.router.handle("review", review)
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["execution_outcome"], "failed")
        self.assertEqual(accepted["failure_reason"], "router-timeout")
        self.assertEqual(accepted["attempts"], 1)
        self.assertEqual(self.router.handle("review", review)["review_history"], accepted["review_history"])
        stale = {**review, "request_id": "stale", "expected_attempt": 0}
        self.authorize(stale, operation="review")
        with self.assertRaises(ValueError):
            self.router.handle("review", stale)
        close = {"task_id": task_id, "request_id": "close", "disposition": "integrated", "keep_artifacts": True}
        self.authorize(close, operation="close")
        closed = self.router.handle("close", close)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["execution_outcome"], "failed")


if __name__ == '__main__':
    unittest.main()
