import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from router.config import Settings
from router.daemon import Controller
from router.db import Store


def settings(root: Path, cap: int = 3, timeout: float = 30, attempts: int = 2, command=None) -> Settings:
    return Settings(AGENT_ROUTER_CONCURRENCY_CAP=cap, AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS=timeout,
                    AGENT_ROUTER_MAX_ATTEMPTS=attempts, AGENT_ROUTER_OPENCODE_COMMAND=command or ["opencode"],
                    AGENT_ROUTER_MODELS={"test": "provider/exact"},
                    AGENT_ROUTER_REPOSITORIES={str(root): {"setup_command": "true"}})


def observe_every_session(test: unittest.TestCase) -> None:
    """Admit every lifecycle event, for tests of recording itself; test_project_scope covers scoping."""
    scope = patch.object(Controller, "in_scope", return_value=True)
    scope.start()
    test.addCleanup(scope.stop)


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

    def test_existing_authorizations_survive_additive_attribution_migration(self):
        path = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE authorizations(session_id TEXT,request_id TEXT,operation TEXT,payload TEXT,instruction_id TEXT,consumed INTEGER,PRIMARY KEY(session_id,request_id))")
            db.execute("INSERT INTO authorizations VALUES('S','old','start','{}','original',0)")
            db.commit()
        for _ in range(2):
            store = Store(path)
            with store.connect() as db:
                row = db.execute("SELECT request_id,instruction_id,parent_agent_id FROM authorizations").fetchone()
                self.assertEqual(tuple(row), ("old", "original", None))

    def test_binding_consumption_and_idempotent_replay(self):
        first = self.submission()
        payload = {**self.payload(first), "work_item_id": "batch  α".ljust(240, "x")}
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
        self.assertEqual(task["work_item_id"], payload["work_item_id"])
        self.assertEqual(self.router.handle("status", {"task_id": task["task_id"]})["work_item_id"], payload["work_item_id"])
        self.assertEqual(self.router.handle("status", {})[0]["work_item_id"], payload["work_item_id"])
        changed_identity = {**payload, "work_item_id": "other-job"}
        with self.assertRaises(ValueError):
            self.router.handle("start", changed_identity)
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
        self.assertNotIn("work_item_id", self.router.store.all()[-1])
        with self.router.store.connect() as db:
            self.assertEqual(db.execute("SELECT sum(consumed) FROM authorizations").fetchone()[0], 3)
            self.assertNotIn("prompt", [row[1] for row in db.execute("PRAGMA table_info(submissions)")])

    def test_declared_work_item_id_refuses_invalid_values_without_registration(self):
        instruction = self.submission()
        for index, value in enumerate((None, True, 1, [], {}, "", " leading", "trailing ", "line\nbreak", "x" * 241)):
            payload = {**self.payload(instruction, f"invalid-{index}"), "work_item_id": value}
            self.authorize(payload)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "work_item_id"):
                self.router.handle("start", payload)
        self.assertEqual(self.router.store.all(), [])

    def test_request_status_recovers_committed_start_without_reauthorizing_or_relaunching(self):
        selection = {"session_id": "S", "request_id": "R", "host_id": self.router.host.host_id}
        missing = self.router.handle("request_status", selection)
        self.assertEqual(missing["status"], "not_found")
        self.assertEqual(self.router.store.all(), [])
        invocation = self.payload(self.submission())
        self.authorize(invocation)
        # Ignore the committed reply, as a disconnected caller would.
        task_id = self.router.handle("start", invocation)["task_id"]
        self.submission(prompt="next-prompt")
        restarted = Controller(settings(self.root / "repo"), {}, self.root / "state")
        before = restarted.store.all()
        for _ in range(2):
            result = restarted.handle("request_status", selection)
            self.assertEqual(result, {"schema_version": 1, **selection, "status": "found",
                                      "operation": "start", "task_id": task_id})
            self.assertEqual(restarted.store.all(), before)
        self.assertEqual(len(before), 1)
        self.assertNotIn("Implement a test", str(result))
        self.assertNotIn(str(self.root), str(result))
        self.assertEqual(restarted.handle("request_status", {**selection, "session_id": "other"})["status"], "not_found")
        mismatch = restarted.handle("request_status", {**selection, "host_id": "other-host"})
        self.assertEqual((mismatch["status"], mismatch["error"]["code"]), ("unknown", "identity_mismatch"))

    def test_request_status_observes_only_committed_coherent_bindings(self):
        selection = {"session_id": "S", "request_id": "R"}
        with self.router.store.transaction() as db:
            self.router.store.put(db, {"task_id": "task-example", "session_id": "S"})
            db.execute("INSERT INTO operations VALUES('S','R','start','{}','task-example')")
            # The writer may still be committing after its caller disconnected.
            self.assertEqual(self.router.handle("request_status", selection)["status"], "not_found")
        self.assertEqual(self.router.handle("request_status", selection)["status"], "found")
        for operation, task_id, owner in (("unknown", "task-example", "S"), ("start", "missing", "S"),
                                          ("start", "bad\nidentity", "S"), ("start", "task-example", "other")):
            with self.subTest(operation=operation, task_id=task_id, owner=owner):
                with self.router.store.transaction() as db:
                    db.execute("UPDATE operations SET operation=?,task_id=?", (operation, task_id))
                    db.execute("UPDATE tasks SET session_id=?", (owner,))
                result = self.router.handle("request_status", selection)
                self.assertEqual((result["status"], result["error"]["code"]), ("unknown", "binding_invalid"))

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
