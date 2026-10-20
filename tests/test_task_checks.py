import json
import os
import socket
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from router.lifecycle import validate
from router.task_checks import (
    MAX_JSON_BYTES,
    completion,
    completion_projection,
    preflight,
)


class TaskCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task = {"executor": "codex", "invocation": {}, "worktree": str(self.root),
                     "permission_profile": {"sandbox": "workspace-write"}}

    def test_frozen_completion_contract_cases(self):
        fixtures = json.loads(Path(__file__).with_name("completion_contract_cases.json").read_text())
        for case in fixtures["cases"]:
            with self.subTest(case=case["name"]):
                root = self.root / case["name"]
                root.mkdir()
                for name, text in case["files"].items():
                    (root / name).write_text(text)
                invocation = {} if case["contract"] is None else {"completion": case["contract"]}
                task = {**self.task, "worktree": str(root), "invocation": invocation}
                if "final_response_present" in case:
                    task["completion_final_response_present"] = case["final_response_present"]
                self.assertEqual(completion(task)["status"], case["expected"])

    def test_completion_is_bounded_and_does_not_follow_links_or_fifos(self):
        (self.root / "outside.json").write_text('{"secret": true}')
        (self.root / "large.json").write_bytes(b" " * (MAX_JSON_BYTES + 1))
        (self.root / "duplicate-setup.json").write_text('{"status":"SETUP_FAILED","status":"READY"}')
        (self.root / "link.json").symlink_to("outside.json")
        (self.root / "directory").mkdir()
        (self.root / "parent-link").symlink_to("directory", target_is_directory=True)
        (self.root / "file-parent").write_text("not a directory")
        os.mkfifo(self.root / "fifo")
        cases = [("link.json", "json_files", "unknown"),
                 ("parent-link/missing.json", "json_files", "unknown"),
                 ("file-parent/answer.txt", "files", "violated"),
                 ("fifo", "json_files", "violated"), ("large.json", "json_files", "unknown"),
                 ("duplicate-setup.json", "setup_status_file", "violated")]
        for name, field, expected in cases:
            with self.subTest(path=name):
                requirement = [name] if field in {"files", "json_files"} else name
                task = {**self.task, "invocation": {"completion": {field: requirement}}}
                self.assertEqual(completion(task)["status"], expected)
        inaccessible = {**self.task, "invocation": {"completion": {"files": ["directory/a"]}}}
        with patch("router.task_checks.os.stat", side_effect=PermissionError):
            self.assertEqual(completion(inaccessible)["status"], "unknown")

    def test_declared_ignored_artifacts_are_checked_without_adding_them_to_git(self):
        (self.root / ".gitignore").write_text("results/\n")
        (self.root / "results").mkdir()
        (self.root / "results/answer.json").write_text('{"valid": true}')
        task = {**self.task, "invocation": {"completion": {"json_files": ["results/answer.json"]}}}
        self.assertEqual(completion(task)["status"], "satisfied")
        self.assertEqual(completion(task)["evidence_source"], "stopped_worktree")

    def test_unknown_is_not_satisfied_and_no_contract_does_not_require_a_response(self):
        declared = {**self.task, "invocation": {"completion": {"final_response": True}}}
        self.assertEqual(completion(declared)["status"], "unknown")
        self.assertEqual(completion(self.task)["status"], "not_declared")

    def test_launch_refuses_provable_local_failures(self):
        packet = self.root / "packet.md"
        packet.write_text("task")
        (self.root / "home-file").write_text("not a directory")
        (self.root / "file-parent").write_text("not a directory")
        cases = [
            ({**self.task, "invocation": {"launch": {"task_packets": ["missing.md"]}}}, [sys.executable], {}, self.root, "Task packet"),
            (self.task, ["nonexistent-agent-runtime-123"], {}, self.root, "executable"),
            (self.task, [sys.executable], {"CODEX_HOME": str(self.root / "home-file")}, self.root, "CODEX_HOME"),
            (self.task, [sys.executable], {}, self.root / "missing", "cwd"),
            ({**self.task, "invocation": {"launch": {"requires_router": True}}}, [sys.executable], {"AGENT_ROUTER_SOCKET": str(self.root / "absent.sock")}, self.root, "Router socket"),
            ({**self.task, "invocation": {"launch": {"writable_paths": ["file-parent/result"]}}}, [sys.executable], {}, self.root, "non-directory"),
            ({**self.task, "permission_profile": {"sandbox": "read-only"}, "invocation": {"launch": {"writable_paths": ["result"]}}}, [sys.executable], {}, self.root, "read-only"),
            ({**self.task, "executor": "claude", "permission_profile": {"tools": ["Read", "Grep"]}, "invocation": {"launch": {"writable_paths": ["result"]}}}, [sys.executable], {}, self.root, "no tool"),
        ]
        for task, command, environment, root, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                preflight(task, command, environment, root)

    def test_six_valid_configurations_proceed_concurrently_without_auth_or_router_probes(self):
        (self.root / "packet.md").write_text("task")
        cases = [
            (self.task, {}),
            ({**self.task, "invocation": {"launch": {"task_packets": ["packet.md"]}}}, {}),
            ({**self.task, "permission_profile": {"sandbox": "read-only"}, "invocation": {"completion": {"files": ["packet.md"]}}}, {}),
            ({**self.task, "executor": "claude", "permission_profile": {"tools": ["Read", "Write"]}}, {}),
            ({**self.task, "executor": "claude", "permission_profile": {"tools": ["Bash"]}, "invocation": {"completion": {"files": ["answer.txt"]}}}, {}),
            (self.task, {"CODEX_HOME": str(self.root / "new-profile")}),
        ]
        def check(case):
            task, environment = case
            before = json.dumps(task, sort_keys=True)
            preflight(task, [sys.executable], environment, self.root)
            self.assertEqual(json.dumps(task, sort_keys=True), before)
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(check, cases))

    def test_required_socket_connects_locally_and_a_timeout_does_not_refuse(self):
        endpoint = self.root / "router.sock"
        task = {**self.task, "invocation": {"launch": {"requires_router": True}}}
        environment = {"AGENT_ROUTER_SOCKET": str(endpoint)}
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(endpoint))
            server.listen(1)
            preflight(task, [sys.executable], environment, self.root)
        with patch("router.task_checks.socket.socket.connect", side_effect=TimeoutError):
            preflight(task, [sys.executable], environment, self.root)

    def test_relative_router_paths_are_resolved_from_the_child_root(self):
        daemon_root = self.root / "daemon"
        child_root = self.root / "child"
        daemon_root.mkdir()
        child_root.mkdir()
        (daemon_root / "state").mkdir()
        (child_root / "state").mkdir()
        (daemon_root / "router.sock").write_text("not a socket")
        (daemon_root / "state/router.sock").write_text("not a socket")
        task = {**self.task, "invocation": {"launch": {"requires_router": True}}}
        cases = [({"AGENT_ROUTER_SOCKET": "router.sock"}, child_root / "router.sock",
                  daemon_root / "router.sock"),
                 ({"AGENT_ROUTER_STATE_DIR": "state"}, child_root / "state/router.sock",
                  daemon_root / "state/router.sock")]
        previous_cwd = Path.cwd()
        try:
            os.chdir(daemon_root)
            for environment, child_endpoint, _daemon_endpoint in cases:
                with self.subTest(environment=environment), socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(child_endpoint))
                    server.listen(1)
                    preflight(task, [sys.executable], environment, child_root)
                child_endpoint.unlink()
            for environment, _child_endpoint, daemon_endpoint in cases:
                daemon_endpoint.unlink()
                with self.subTest(daemon_only=environment), socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(daemon_endpoint))
                    server.listen(1)
                    with self.assertRaisesRegex(ValueError, "Router socket"):
                        preflight(task, [sys.executable], environment, child_root)
                daemon_endpoint.unlink()
        finally:
            os.chdir(previous_cwd)

    def test_remote_projection_preserves_unknown_and_bounds_diagnostics(self):
        good = {"status": "satisfied", "violations": [], "unchecked": [], "evidence_source": "stopped_worktree"}
        violated = {**good, "status": "violated", "violations": [{"path": "result.json", "reason": "missing"}]}
        cases = [(good, "satisfied"), (violated, "violated"), (None, "unknown"), ([], "unknown"),
                 ({**good, "violations": [{"reason": "missing"}]}, "unknown"),
                 ({**good, "unchecked": [{"reason": "unavailable"}]}, "unknown"),
                 ({**good, "evidence_source": None}, "unknown"),
                 ({**good, "evidence_source": {}}, "unknown"),
                 ({**violated, "violations": [{"reason": ""}]}, "unknown"),
                 ({**violated, "violations": [{"path": "x" * 2049}]}, "unknown"),
                 ({**violated, "violations": [{"reason": "missing"}] * 67}, "unknown")]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(completion_projection(value)["status"], expected)

    def test_contract_schema_rejects_paths_and_types_before_any_filesystem_work(self):
        base = {"request_version": "1", "request_id": "R", "instruction_id": "I", "executor": "codex",
                "model": "test", "task": "task", "parent_repo": "/repo", "parent_worktree": "/repo", "mode": "write"}
        for contract in ({"files": ["../secret"]}, {"json_files": ["/absolute"]},
                         {"files": ["."]}, {"files": "answer.txt"}, {"final_response": 1},
                         {"files": ["a"] * 33}, {"unsupported": True}):
            with self.subTest(contract=contract), self.assertRaises(ValueError):
                validate("start", {**base, "completion": contract})


if __name__ == "__main__":
    unittest.main()
