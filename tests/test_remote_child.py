import base64
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from router import remote_child
from router.daemon import Runtime
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
                  "remote_repo": "/srv/project", "remote_worktree": "/srv/project",
                  "snapshot_receipt": self.receipt,
                  "snapshot_base64": base64.b64encode(b"abc").decode()}
        result.update(changes)
        return result

    def test_start_payload_validates_identity_and_bytes(self):
        parsed = remote_child._start_payload(self.payload())
        self.assertEqual(parsed["snapshot_bytes"], b"abc")
        with self.assertRaisesRegex(ValueError, "request identity"):
            remote_child._start_payload(self.payload(invocation={**self.invocation, "request_id": "other"}))
        with self.assertRaisesRegex(ValueError, "bytes"):
            remote_child._start_payload(self.payload(snapshot_base64=base64.b64encode(b"abcd").decode()))

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

    def test_recovery_projects_remote_liveness_and_terminal_capture(self):
        task = {
            "task_id": "task-a", "session_id": "session-a", "remote_host": "worker",
            "remote_task_id": "task-a", "execution_stopped": False, "status": "interrupted",
            "invocation": {"request_id": "request-a"},
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
                     "execution_outcome": "completed"},
                ))

            def status(self, _session_id, _request_id):
                return next(self.responses)

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


if __name__ == "__main__":
    unittest.main()
