import base64
import unittest

from router import remote_child


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

    def test_control_payloads_are_strict_and_feedback_is_required_for_rejection(self):
        self.assertEqual(remote_child._control_payload("status", {"session_id": "s", "request_id": "r"})["session_id"], "s")
        with self.assertRaises(ValueError):
            remote_child._control_payload("review", {"task_id": "t", "request_id": "r", "expected_attempt": 1,
                                                       "outcome": "rejected", "feedback": ""})
        with self.assertRaises(ValueError):
            remote_child._control_payload("close", {"task_id": "t", "request_id": "r",
                                                      "disposition": "integrated", "keep_artifacts": 1})


if __name__ == "__main__":
    unittest.main()
