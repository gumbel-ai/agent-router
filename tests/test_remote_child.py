import base64
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from router import remote_child
from router.daemon import Controller, PhaseStopped, Runtime
from router.db import Store
from router.dispatch import canonical
from router.fleet import FleetHost


class RemoteChildProtocolTests(unittest.TestCase):
    def setUp(self):
        self.receipt = {
            "schema_version": 1, "task_id": "task-a", "snapshot_commit": "a" * 40,
            "snapshot_tree": "b" * 40, "object_format": "sha1", "paths": ["code.txt"],
            "file_count": 1, "total_bytes": 3, "byte_count": 3, "sha256": "c" * 64,
        }
        self.invocation = {
            "request_version": "1", "request_id": "request-a", "instruction_id": "instruction-a",
            "executor": "opencode", "model": "worker", "task": "remote task",
            "parent_repo": "/srv/project", "parent_worktree": "/srv/project", "mode": "write",
        }

    def payload(self, **changes):
        result = {"session_id": "session-a", "request_id": "request-a", "invocation": self.invocation,
                  "resolved_model": "provider/model-a",
                  "remote_repo": "/srv/project", "remote_worktree": "/srv/project",
                  "snapshot_receipt": self.receipt,
                  "snapshot_base64": base64.b64encode(b"abc").decode(), "tracing_requested": False}
        result.update(changes)
        return result

    def test_start_payload_validates_identity_and_bytes(self):
        parsed = remote_child._start_payload(self.payload())
        self.assertEqual(parsed["snapshot_bytes"], b"abc")
        with self.assertRaisesRegex(ValueError, "request identity"):
            remote_child._start_payload(self.payload(invocation={**self.invocation, "request_id": "other"}))
        with self.assertRaisesRegex(ValueError, "bytes"):
            remote_child._start_payload(self.payload(snapshot_base64=base64.b64encode(b"abcd").decode()))
        with self.assertRaisesRegex(ValueError, "resolved model"):
            remote_child._start_payload(self.payload(resolved_model=""))

    def test_transport_start_keeps_snapshot_base64_on_wire(self):
        transport = remote_child.RemoteChildTransport(
            FleetHost("worker", "ssh", target="example"), 1
        )
        with patch("router.remote_child._exchange", return_value={"status": "created"}) as exchange:
            result = transport.start(
                session_id="session-a", request_id="request-a", invocation=self.invocation,
                resolved_model="provider/model-a", remote_repo="/srv/project", remote_worktree="/srv/project",
                snapshot_receipt=self.receipt, snapshot_bundle=b"abc", tracing_requested=False,
            )
        self.assertEqual(result, {"status": "created"})
        payload = exchange.call_args.args[3]
        self.assertEqual(payload["snapshot_base64"], base64.b64encode(b"abc").decode())
        self.assertNotIn("snapshot_bytes", payload)

    def test_remote_start_sends_only_the_tracing_request(self):
        transport = remote_child.RemoteChildTransport(FleetHost("worker", "ssh", target="example"), 1)
        with patch("router.remote_child._exchange", return_value={"status": "created"}) as exchange:
            transport.start(
                session_id="session-a", request_id="request-a", invocation=self.invocation,
                resolved_model="provider/model-a", remote_repo="/srv/project", remote_worktree="/srv/project",
                snapshot_receipt=self.receipt, snapshot_bundle=b"abc", tracing_requested=True,
            )
        payload = exchange.call_args.args[3]
        self.assertEqual(set(payload), set(self.payload()))
        self.assertIs(payload["tracing_requested"], True)
        wire = canonical(payload)
        self.assertNotIn("LANGFUSE", wire)
        self.assertNotIn("plugin", wire)
        with self.assertRaisesRegex(ValueError, "tracing_requested"):
            remote_child._start_payload(self.payload(tracing_requested="yes"))

    def test_tracing_request_is_optional_in_both_upgrade_directions(self):
        older_parent = self.payload()
        del older_parent["tracing_requested"]
        self.assertIs(remote_child._start_payload(older_parent)["tracing_requested"], False)
        transport = remote_child.RemoteChildTransport(FleetHost("worker", "ssh", target="example"), 1)
        with patch("router.remote_child._exchange", return_value={"status": "created"}) as exchange:
            transport.start(
                session_id="session-a", request_id="request-a", invocation=self.invocation,
                resolved_model="provider/model-a", remote_repo="/srv/project", remote_worktree="/srv/project",
                snapshot_receipt=self.receipt, snapshot_bundle=b"abc", tracing_requested=False,
            )
        self.assertNotIn("tracing_requested", exchange.call_args.args[3])

    def test_agent_hook_denies_router_remote(self):
        from router.hooks import authorize
        result = authorize({"tool_name": "Bash", "tool_input": {"command": "router remote --json"}})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_remote_request_parser_rejects_duplicate_and_nonfinite_fields(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            remote_child.parse_request(b'{"operation":"status","operation":"start","payload":{}}')
        with self.assertRaisesRegex(ValueError, "non-integer"):
            remote_child.parse_request(b'{"operation":"status","payload":{"expected_attempt":NaN}}')

    def test_control_payloads_are_strict_and_feedback_is_required_for_rejection(self):
        self.assertEqual(remote_child._control_payload("status", {"session_id": "s", "request_id": "r"})["session_id"], "s")
        with self.assertRaises(ValueError):
            remote_child._control_payload("review", {"task_id": "t", "request_id": "r", "expected_attempt": 1,
                                                       "outcome": "rejected", "feedback": ""})
        with self.assertRaises(ValueError):
            remote_child._control_payload("close", {"task_id": "t", "request_id": "r",
                                                      "disposition": "integrated", "keep_artifacts": 1})

    def test_control_payloads_keep_local_optional_fields_optional(self):
        accepted = {"task_id": "t", "request_id": "r", "expected_attempt": 1, "outcome": "accepted"}
        self.assertEqual(remote_child._control_payload("review", accepted), accepted)
        closing = {"task_id": "t", "request_id": "r", "disposition": "discarded"}
        self.assertEqual(remote_child._control_payload("close", closing), closing)
        with self.assertRaises(ValueError):
            remote_child._control_payload("review", {"task_id": "t", "request_id": "r", "expected_attempt": 1,
                                                       "outcome": "rejected"})

    def test_review_allows_attempt_zero_and_multiline_feedback(self):
        review = {"task_id": "t", "request_id": "r", "expected_attempt": 0, "outcome": "rejected",
                  "feedback": "Fix the parser\nAdd a regression test"}
        self.assertEqual(remote_child._control_payload("review", review), review)

    def test_partial_remote_destination_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "host"):
            Controller.envelope({**self.invocation, "remote_repo": "/srv/project", "remote_worktree": "/srv/project"})

    def test_remote_mutations_respect_maintenance_drain(self):
        controller = Controller.__new__(Controller)
        controller.settings = SimpleNamespace(delegation_enabled=True)
        controller.lifecycle_lock = __import__("threading").Lock()
        controller.draining = True
        with patch.object(Controller, "remote_start") as remote_start, \
                self.assertRaisesRegex(ValueError, "maintenance"):
            controller.handle("remote_start", {})
        remote_start.assert_not_called()

    def test_pre_send_start_failure_stays_stopped(self):
        directory = Path(tempfile.mkdtemp())
        task = {"task_id": "task-a", "session_id": "session-a", "remote_host": "worker",
                "remote_repo": "/srv/project", "remote_worktree": "/srv/project", "status": "preparing",
                "execution_stopped": True, "artifact_dir": str(directory), "resolved_model": "provider/model-a",
                "invocation": {**self.invocation, "parent_worktree": str(directory),
                               "context": {"relevant_paths": ["code.txt"]}}}

        class Store:
            def get(self, _task_id):
                return task

            def update(self, _task_id, change):
                change(task)
                return task

            update_with_attempt = update

        class Transport:
            def __init__(self, _host, _timeout):
                pass

            def start(self, **_kwargs):
                raise failure

        def export(_root, _task_id, artifact, _paths):
            artifact.mkdir(parents=True)
            (artifact / "snapshot.bundle").write_bytes(b"abc")
            return self.receipt

        runtime = Runtime.__new__(Runtime)
        runtime.store = Store()
        config = SimpleNamespace(hosts=(FleetHost("worker", "ssh", target="example"),), ssh_timeout_seconds=1)
        for failure in (ValueError("request_id must be a bounded non-empty string"),
                        remote_child.RemoteChildError("spawn_failed", "no ssh")):
            task.update(status="preparing", execution_stopped=True)
            artifact = directory / "remote-snapshot"
            if artifact.exists():
                (artifact / "snapshot.bundle").unlink()
                artifact.rmdir()
            with patch("router.fleet.load_fleet_config", return_value=config), \
                    patch("router.remote_child.RemoteChildTransport", Transport), \
                    patch("router.remote_snapshot.export_snapshot", export):
                runtime.execute_remote("task-a")
            self.assertEqual(task["status"], "failed")
            self.assertTrue(task["execution_stopped"])
        task.update(status="preparing", execution_stopped=True)
        with patch("router.fleet.load_fleet_config", side_effect=ValueError("Invalid fleet TOML")):
            runtime.execute_remote("task-a")
        self.assertEqual(task["status"], "failed")
        self.assertTrue(task["execution_stopped"])

    def test_lost_start_reply_keeps_remote_ownership_uncertain(self):
        directory = Path(tempfile.mkdtemp())
        task = {"task_id": "task-a", "session_id": "session-a", "remote_host": "worker",
                "remote_repo": "/srv/project", "remote_worktree": "/srv/project", "status": "preparing",
                "execution_stopped": True, "artifact_dir": str(directory), "resolved_model": "provider/model-a",
                "invocation": {**self.invocation, "parent_worktree": str(directory),
                               "context": {"relevant_paths": ["code.txt"]}}}

        class Store:
            def get(self, _task_id):
                return task

            def update(self, _task_id, change):
                change(task)
                return task

            update_with_attempt = update

        class Transport:
            def __init__(self, _host, _timeout):
                pass

            def start(self, **_kwargs):
                raise remote_child.RemoteChildError("deadline", "lost reply")

        def export(_root, _task_id, artifact, _paths):
            artifact.mkdir(parents=True)
            (artifact / "snapshot.bundle").write_bytes(b"abc")
            return self.receipt

        runtime = Runtime.__new__(Runtime)
        runtime.store = Store()
        config = SimpleNamespace(hosts=(FleetHost("worker", "ssh", target="example"),), ssh_timeout_seconds=1)
        with patch("router.fleet.load_fleet_config", return_value=config), \
                patch("router.remote_child.RemoteChildTransport", Transport), \
                patch("router.remote_snapshot.export_snapshot", export):
            runtime.execute_remote("task-a")
        self.assertEqual(task["status"], "interrupted")
        self.assertFalse(task["execution_stopped"])

    def test_cancel_during_export_suppresses_unsent_start(self):
        directory = Path(tempfile.mkdtemp())
        task = {"task_id": "task-a", "session_id": "session-a", "remote_host": "worker",
                "remote_repo": "/srv/project", "remote_worktree": "/srv/project", "status": "cancelling",
                "cancel_requested": True, "execution_stopped": True, "artifact_dir": str(directory),
                "resolved_model": "provider/model-a",
                "invocation": {**self.invocation, "parent_worktree": str(directory),
                               "context": {"relevant_paths": ["code.txt"]}}}
        started = []

        class Store:
            def get(self, _task_id):
                return task

            def update(self, _task_id, change):
                change(task)
                return task

        class Transport:
            def __init__(self, _host, _timeout):
                pass

            def start(self, **kwargs):
                started.append(kwargs)

        def export(_root, _task_id, artifact, _paths):
            artifact.mkdir(parents=True)
            (artifact / "snapshot.bundle").write_bytes(b"abc")
            return self.receipt

        runtime = Runtime.__new__(Runtime)
        runtime.store = Store()
        config = SimpleNamespace(hosts=(FleetHost("worker", "ssh", target="example"),), ssh_timeout_seconds=1)
        with patch("router.fleet.load_fleet_config", return_value=config), \
                patch("router.remote_child.RemoteChildTransport", Transport), \
                patch("router.remote_snapshot.export_snapshot", export), \
                self.assertRaises(PhaseStopped):
            runtime.execute_remote("task-a")
        self.assertEqual(started, [])
        self.assertTrue(task["execution_stopped"])

    def test_locally_invalid_review_or_close_is_refused_before_ssh(self):
        controller = Controller.__new__(Controller)
        controller.settings = SimpleNamespace(max_attempts=1)
        task = {"task_id": "task-a", "remote_host": "worker", "remote_task_id": "task-a",
                "status": "awaiting_review", "attempts": 1, "execution_stopped": True,
                "final_tree": "a" * 40, "review_history": []}
        with patch("router.fleet.load_fleet_config") as load:
            with self.assertRaisesRegex(ValueError, "ceiling"):
                controller.forward_remote(task, "review", {"task_id": "task-a", "request_id": "r",
                                                           "expected_attempt": 1, "outcome": "rejected",
                                                           "feedback": "again"})
            with self.assertRaises(ValueError):
                controller.forward_remote(task, "close", {"task_id": "task-a", "request_id": "r",
                                                          "disposition": "integrated"})
        load.assert_not_called()
        self.assertEqual(task["status"], "awaiting_review")

    def test_known_unsent_task_closes_without_fleet_config(self):
        controller = Controller.__new__(Controller)
        task = {"task_id": "task-a", "remote_host": "worker", "status": "failed", "execution_stopped": True}
        with patch("router.fleet.load_fleet_config", side_effect=ValueError("Invalid fleet TOML")) as load:
            self.assertIsNone(controller.forward_remote(task, "close", {"task_id": "task-a", "request_id": "r",
                                                                        "disposition": "discarded"}))
        load.assert_not_called()

    def test_restart_reconciles_interrupted_remote_task(self):
        task = {"task_id": "task-a", "remote_host": "worker", "status": "interrupted",
                "execution_stopped": False}
        runtime = Runtime.__new__(Runtime)
        runtime.control = SimpleNamespace(native=SimpleNamespace(reconcile_restart=lambda: None),
                                          sessions=SimpleNamespace(reconcile_restart=lambda: None),
                                          start_lifecycle_writer=lambda: None,
                                          capacity=SimpleNamespace(start=lambda: None),
                                          settings=SimpleNamespace(max_attempts=None))
        runtime.store = SimpleNamespace(all=lambda: [task])
        with patch.object(Runtime, "spawn") as spawn, patch.object(Runtime, "schedule"):
            runtime.start()
            runtime.scheduler.join(1)
        spawn.assert_called_once_with(task, recovered=True)

    def test_restart_fails_known_unsent_remote_start_as_stopped(self):
        task = {"task_id": "task-a", "remote_host": "worker", "status": "preparing",
                "execution_stopped": True, "max_attempts": 1}
        runtime = Runtime.__new__(Runtime)
        runtime.control = SimpleNamespace(native=SimpleNamespace(reconcile_restart=lambda: None),
                                          sessions=SimpleNamespace(reconcile_restart=lambda: None),
                                          start_lifecycle_writer=lambda: None,
                                          capacity=SimpleNamespace(start=lambda: None),
                                          settings=SimpleNamespace(max_attempts=None))
        runtime.store = SimpleNamespace(all=lambda: [task], update=lambda _task_id, change: change(task))
        with patch.object(Runtime, "spawn") as spawn, patch.object(Runtime, "schedule"):
            runtime.start()
            runtime.scheduler.join(1)
        spawn.assert_not_called()
        self.assertEqual(task["status"], "failed")
        self.assertTrue(task["execution_stopped"])
        task.update(status="cancelling", cancel_requested=True)
        with patch.object(Runtime, "spawn"), patch.object(Runtime, "schedule"):
            runtime.start()
            runtime.scheduler.join(1)
        self.assertEqual(task["status"], "cancelled")

    def test_remote_cancelling_and_recovery_reconcile_remotely(self):
        for status, recovered in (("cancelling", False), ("running", True)):
            task = {"task_id": "task-a", "remote_host": "worker", "status": status}
            runtime = Runtime.__new__(Runtime)
            runtime.store = SimpleNamespace(get=lambda _task_id, task=task: task)
            with patch.object(Runtime, "execute_remote") as execute_remote:
                runtime.execute("task-a", recovered)
            execute_remote.assert_called_once_with("task-a", recovered=True)

    def test_recovery_projects_remote_liveness_and_terminal_capture(self):
        task = {
            "task_id": "task-a", "session_id": "session-a", "remote_host": "worker",
            "remote_task_id": "task-a", "execution_stopped": False, "status": "interrupted",
            "invocation": {"request_id": "request-a", "completion": {"files": ["result.txt"]}},
            "completion_check": {"status": "violated", "violations": [{"reason": "previous attempt"}], "unchecked": []},
        }

        class Store:
            def __init__(self, value):
                self.value = value

            def get(self, _task_id):
                return self.value

            def update_with_attempt(self, _task_id, change):
                change(self.value)
                return self.value

        class Transport:
            def __init__(self, _host, _timeout):
                self.responses = iter((
                    {"status": "running", "task_id": "task-a", "attempts": 1, "attempt_number": 1},
                    {"status": "awaiting_review", "task_id": "task-a", "execution_stopped": True,
                     "attempts": 1, "attempt_number": 1, "final_tree": "a" * 40,
                     "snapshot_commit": "b" * 40, "snapshot_tree": "c" * 40,
                     "execution_outcome": "completed",
                     "completion_check": {"status": "violated", "violations": [{"path": "result.txt", "reason": "required file missing"}],
                                          "unchecked": [], "evidence_source": "stopped_worktree"}},
                ))

            def status(self, _session_id, _request_id):
                response = next(self.responses)
                if response["status"] == "awaiting_review" and "completion_check" in task:
                    raise AssertionError("Previous attempt diagnostic survived active remote recovery")
                return response

        runtime = Runtime.__new__(Runtime)
        runtime.store = Store(task)
        config = SimpleNamespace(hosts=(FleetHost("worker", "ssh", target="example"),), ssh_timeout_seconds=1)
        with patch("router.fleet.load_fleet_config", return_value=config), \
                patch("router.remote_child.RemoteChildTransport", Transport), \
                patch("router.daemon.time.sleep"):
            runtime.execute_remote("task-a", recovered=True)
        self.assertEqual(task["status"], "awaiting_review")
        self.assertTrue(task["execution_stopped"])
        self.assertEqual(task["remote_task_id"], "task-a")
        self.assertEqual(task["final_tree"], "a" * 40)
        self.assertEqual(task["completion_check"]["violations"][0]["path"], "result.txt")
        task["invocation"].pop("completion")
        task.update(status="interrupted", completion_check={"status": "violated"})
        with patch("router.fleet.load_fleet_config", return_value=config), \
                patch("router.remote_child.RemoteChildTransport", Transport), \
                patch("router.daemon.time.sleep"):
            runtime.execute_remote("task-a", recovered=True)
        self.assertNotIn("completion_check", task)



    def test_remote_final_states_require_satisfied_declared_completion(self):
        good = {"status": "satisfied", "violations": [], "unchecked": [],
                "evidence_source": "stopped_worktree"}
        diagnostics = [
            ("satisfied", True, good, "satisfied"),
            ("missing", False, None, "unknown"),
            ("malformed", True, [], "unknown"),
            ("violated", True, {**good, "status": "violated",
                                "violations": [{"reason": "required output missing"}]}, "violated"),
        ]
        config = SimpleNamespace(hosts=(FleetHost("worker", "ssh", target="example"),),
                                 ssh_timeout_seconds=1)
        for remote_status in ("accepted", "integrated", "closed"):
            for name, included, diagnostic, expected in diagnostics:
                response = {"status": remote_status, "task_id": "task-a"}
                if included:
                    response["completion_check"] = diagnostic

                class Transport:
                    def __init__(self, _host, _timeout):
                        pass

                    def status(self, _session_id, _request_id):
                        return response

                invocation = {**self.invocation, "executor": "codex",
                              "completion": {"files": ["result.txt"]}}
                store = self.remote_store({
                    "task_id": "task-a", "status": "interrupted", "execution_stopped": False,
                    "invocation": invocation,
                })
                runtime = Runtime.__new__(Runtime)
                runtime.store = store
                with self.subTest(remote_status=remote_status, diagnostic=name), \
                        patch("router.fleet.load_fleet_config", return_value=config), \
                        patch("router.remote_child.RemoteChildTransport", Transport):
                    runtime.execute_remote("task-a", recovered=True)
                task = store.get("task-a")
                self.assertEqual(task["completion_check"]["status"], expected)
                if expected == "satisfied":
                    self.assertEqual(task["status"], remote_status)
                    self.assertTrue(task["execution_stopped"])
                else:
                    self.assertEqual(task["status"], "interrupted")
                    self.assertFalse(task["execution_stopped"])
                    self.assertIn("invalid_response", task["failure_reason"])

    def remote_store(self, task: dict) -> Store:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(Path(directory.name) / "router.db")
        task = {"session_id": "S", "remote_host": "worker", "remote_task_id": "task-a", "attempts": 1,
                "review_history": [], "created_at": 1, "invocation": {**self.invocation, "request_id": "start"},
                **task}
        with store.transaction() as db:
            store.put(db, task)
        return store

    def test_remote_forward_runs_outside_db_transaction_and_lifecycle_lock(self):
        controller = Controller.__new__(Controller)
        controller.settings = SimpleNamespace(delegation_enabled=True, max_attempts=3)
        controller.lifecycle_lock = threading.Lock()
        controller.changed = threading.Event()
        controller.draining = False
        controller.store = self.remote_store({"task_id": "task-a", "status": "running", "execution_stopped": False})
        payload = {"task_id": "task-a", "request_id": "cancel-a"}
        with controller.store.transaction() as db:
            db.execute("INSERT INTO authorizations(session_id,request_id,operation,payload,instruction_id) VALUES(?,?,?,?,?)",
                       ("S", "cancel-a", "cancel", canonical(payload), "instruction-a"))
        observed = []

        def forward(_task, operation, _payload):
            # Another writer and lifecycle admission proceed while SSH is in flight.
            observed.append(controller.lifecycle_lock.acquire(blocking=False))
            controller.lifecycle_lock.release()
            with controller.store.transaction(timeout=.5) as db:
                db.execute("SELECT 1")
            return {"status": "cancelling", "task_id": "task-a"}

        with patch.object(Controller, "forward_remote", side_effect=forward):
            result = controller.handle("cancel", payload)
        self.assertEqual(observed, [True])
        self.assertEqual(result["status"], "cancelling")
        with controller.store.snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM operations WHERE request_id='cancel-a'").fetchone())

    def test_remote_cleanup_failure_is_projected_after_one_retry(self):
        store = self.remote_store({"task_id": "task-a", "status": "closed", "execution_stopped": True,
                                   "cleanup_pending": True})
        close = {"task_id": "task-a", "request_id": "close-a", "disposition": "discarded"}
        with store.transaction() as db:
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", ("S", "close-a", "close", canonical(close), "task-a"))
        closes = []

        class Transport:
            def __init__(self, _host, _timeout):
                pass

            def status(self, _session_id, _request_id):
                return {"status": "closed", "task_id": "task-a", "cleanup_pending": True, "cleanup_error": "busy"}

            def close(self, payload):
                closes.append(payload)
                return {"status": "closed", "task_id": "task-a"}

        runtime = Runtime.__new__(Runtime)
        runtime.store = store
        runtime.workers, runtime.lock = {}, threading.Lock()
        runtime.control = SimpleNamespace(changed=threading.Event())
        config = SimpleNamespace(hosts=(FleetHost("worker", "ssh", target="example"),), ssh_timeout_seconds=1)
        with patch("router.fleet.load_fleet_config", return_value=config), \
                patch("router.remote_child.RemoteChildTransport", Transport):
            runtime.clean("task-a")
        self.assertEqual(closes, [close])
        task = store.get("task-a")
        self.assertTrue(task["cleanup_pending"])
        self.assertEqual(task["cleanup_error"], "Remote cleanup failed: busy")


if __name__ == "__main__":
    unittest.main()
