import hashlib
import json
import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_control import settings

from router import remote_snapshot
from router.daemon import Controller, Runtime
from router.dispatch import canonical
from router.fleet_snapshot import compose
from router.remote_receiver import RemoteReceiver
from router.snapshot import git
from router.snapshot import main as snapshot_main


class RemoteReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="router-remote-receiver-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.global_config = self.root / "global.config"
        self.global_config.write_text("[core]\nexcludesFile =\n")
        isolated = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.global_config), "GIT_CONFIG_NOSYSTEM": "1",
                                           "GIT_CONFIG_COUNT": "0", "GIT_CONFIG_PARAMETERS": ""})
        isolated.start()
        self.addCleanup(isolated.stop)
        self.origin = self.repository("origin", "origin baseline\n")
        self.target = self.repository("target", "receiving baseline\n")
        self.config = settings(self.target, command=[sys.executable, str(Path(__file__).with_name("fixture_executor.py"))])
        self.state = self.root / "state"
        self.control = Controller(self.config, {}, self.state)
        self.receiver = RemoteReceiver(self.control, Runtime(self.control).validate)
        (self.origin / "code.txt").write_text("origin dirty work\n")
        (self.origin / "new.txt").write_text("origin new file\n")
        self.receipt = remote_snapshot.export_snapshot(self.origin, "origin-snapshot", self.root / "export", ["code.txt", "new.txt"])
        self.bundle = self.root / "export" / "snapshot.bundle"
        self.payload = {"schema_version": 1, "receiver_host_id": self.control.host.host_id,
                        "origin_host_id": "origin-host", "origin_session_id": "native-parent", "request_id": "start-one",
                        "operation": "start", "repository": str(self.target), "executor": "opencode",
                        "model": "test", "task": "Write example output", "snapshot": self.receipt,
                        "parent_agent_id": "worker-a", "work_item_id": "batch-job"}
        self.selection = {key: value for key, value in self.payload.items()
                          if key in {"schema_version", "receiver_host_id", "origin_host_id", "origin_session_id", "request_id"}}

    def repository(self, name, content):
        root = self.root / name
        root.mkdir()
        git(root, "init", "-q")
        git(root, "config", "user.name", "Test")
        git(root, "config", "user.email", "test@example.invalid")
        (root / "code.txt").write_text(content)
        (root / ".gitignore").write_text(".env\n")
        (root / "opencode.json").write_text(json.dumps({"permission": {"*": "allow", "external_directory": "deny"}}))
        git(root, "add", ".")
        git(root, "commit", "-qm", "initial")
        return root

    def test_lost_reply_restart_and_concurrent_replays_keep_one_binding(self):
        self.assertEqual(self.receiver.lookup(self.selection)["status"], "not_found")
        # Discard the committed reply, then reconstruct the receiving controller.
        self.receiver.start(self.payload, self.bundle)
        self.control = Controller(self.config, {}, self.state)
        self.receiver = RemoteReceiver(self.control, Runtime(self.control).validate)
        recovered = self.receiver.lookup(self.selection)
        self.assertEqual(recovered["status"], "found")
        task_id = recovered["task_id"]
        self.control.store.update(task_id, lambda task: task.update(status="awaiting_review"))
        # A replay needs neither transfer bytes nor a new native authorization.
        self.bundle.unlink()
        with ThreadPoolExecutor(max_workers=5) as pool:
            copies = list(pool.map(lambda _: self.receiver.start(self.payload, self.bundle), range(5)))
        self.assertEqual({copy["task_id"] for copy in copies}, {task_id})
        self.assertEqual({copy["task_status"] for copy in copies}, {"awaiting_review"})
        self.assertEqual(len(self.control.store.all()), 1)
        with self.control.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM remote_operations").fetchone()[0], 1)
            for table in ("submissions", "authorizations", "operations"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        for changed in ({"task": "Different work"}, {"snapshot": {**self.receipt, "task_id": "other-snapshot"}},
                        {"result_paths": ["additional.txt"]},
                        {"operation": "review"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.receiver.start({**self.payload, **changed}, self.bundle)
        self.assertEqual(self.receiver.lookup({**self.selection, "origin_session_id": "another-parent"})["status"], "not_found")
        self.assertEqual(self.receiver.lookup({**self.selection, "origin_host_id": "another-host"})["status"], "not_found")

    def test_concurrent_first_admission_commits_only_one_real_task(self):
        # Each request can finish verified import before contending on one atomic registration.
        verified = threading.Barrier(5, timeout=15)

        def import_before_registration(*args):
            result = remote_snapshot.import_snapshot(*args)
            verified.wait()
            return result

        with patch("router.remote_receiver.import_snapshot", side_effect=import_before_registration), ThreadPoolExecutor(max_workers=5) as pool:
            copies = list(pool.map(lambda _: self.receiver.start(self.payload, self.bundle), range(5)))
        self.assertEqual(len({copy["task_id"] for copy in copies}), 1)
        self.assertEqual(len(self.control.store.all()), 1)

    def test_receiving_host_configuration_and_import_are_required_before_registration(self):
        too_many_outputs = [f"output-{index}.txt" for index in range(remote_snapshot.MAX_FILES - len(self.receipt["paths"]) + 1)]
        variants = ({"receiver_host_id": "wrong-host"}, {"repository": str(self.root / "unconfigured")},
                    {"executor": "unsupported"}, {"model": "missing"}, {"setup_command": "origin setup"},
                    {"schema_version": True}, {"snapshot": {**self.receipt, "sha256": "1" * 64}},
                    {"result_paths": []}, {"result_paths": ["../outside.txt"]}, {"result_paths": too_many_outputs})
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.receiver.start({**self.payload, **changes}, self.bundle)
            self.assertEqual(self.control.store.all(), [])
        with self.assertRaises(ValueError):
            self.receiver.lookup({**self.selection, "receiver_host_id": "wrong-host"})
        self.assertEqual(git(self.target, "for-each-ref", "--format=%(refname)", "refs/router/"), "")

    def test_maintenance_during_import_prevents_registration_without_holding_a_writer(self):
        imported, release = threading.Event(), threading.Event()

        def delayed_import(*args):
            result = remote_snapshot.import_snapshot(*args)
            imported.set()
            if not release.wait(15):
                raise TimeoutError("Receiver test import was not released")
            return result

        with patch("router.remote_receiver.import_snapshot", side_effect=delayed_import), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.receiver.start, self.payload, self.bundle)
            try:
                self.assertTrue(imported.wait(15))
                # Import must not retain a SQLite writer or prevent closing admission.
                with self.control.store.transaction(timeout=.1):
                    pass
                with self.control.lifecycle_lock:
                    self.control.draining = True
            finally:
                release.set()
            with self.assertRaises(ValueError):
                future.result(timeout=15)
        self.assertEqual(self.control.store.all(), [])
        self.assertEqual(self.receiver.lookup(self.selection)["status"], "not_found")

    def test_preparation_uses_only_imported_snapshot_and_preserves_receiving_dirty_checkout(self):
        (self.target / "code.txt").write_text("receiving dirty work\n")
        (self.target / "receiving-only.txt").write_text("must stay local\n")
        before = git(self.target, "status", "--porcelain")
        result = self.receiver.start(self.payload, self.bundle)
        task = self.control.store.get(result["task_id"])
        self.assertNotIn("child_session_id", task)
        self.assertNotIn("permission_profile", task)
        self.assertEqual(task["work_item_id"], "batch-job")
        directory = self.root / "child-artifacts"
        source, destination = self.root / "prepare.input", self.root / "prepare.result"
        source.write_text(json.dumps({**task, "artifact_dir": str(directory), "attempt_number": 1}))
        with patch("sys.argv", ["snapshot.py", "prepare", str(source), str(destination)]):
            snapshot_main()
        prepared = json.loads(destination.read_text())
        child = Path(prepared["worktree"])
        self.assertEqual((child / "code.txt").read_text(), "origin dirty work\n")
        self.assertEqual((child / "new.txt").read_text(), "origin new file\n")
        self.assertFalse((child / "receiving-only.txt").exists())
        self.assertEqual(prepared["snapshot_commit"], self.receipt["snapshot_commit"])
        self.assertEqual(git(child, "rev-parse", "HEAD").strip(), self.receipt["snapshot_commit"])
        self.assertEqual(git(self.target, "status", "--porcelain"), before)

    def test_scheduler_launches_one_native_child_from_the_supplied_snapshot(self):
        smudge_marker, clean_marker = self.root / "smudge-ran", self.root / "clean-ran"
        for name, marker, replacement in (("smudge", smudge_marker, "modified"), ("clean", clean_marker, "cleaned")):
            git(self.target, "config", f"filter.example.{name}",
                f"touch {shlex.quote(str(marker))}; sed s/origin/{replacement}/g")
        git(self.target, "config", "core.autocrlf", "true")
        initial = {"code.txt": b"origin dirty work\n", "new.txt": b"origin new file\n",
                   "crlf.txt": b"origin line\n", "ident.txt": b"$Id$\n", "encoded.txt": b"origin utf8\n",
                   "executable.sh": b"#!/bin/sh\nexit 0\n", ".gitignore": b"session.json\n.env\n",
                   ".gitattributes": b"code.txt filter=example\ncrlf.txt text eol=crlf\nident.txt ident\nencoded.txt working-tree-encoding=UTF-16LE\n"}
        for name, content in initial.items():
            (self.origin / name).write_bytes(content)
        (self.origin / "executable.sh").chmod(0o755)
        self.receipt = remote_snapshot.export_snapshot(self.origin, "conversion-snapshot", self.root / "conversion-export", sorted(initial))
        self.bundle = self.root / "conversion-export" / "snapshot.bundle"
        changed = {"code.txt": b"origin changed raw\r\n", "crlf.txt": b"changed\r\n",
                   "ident.txt": b"$Id: keep literal $\n", "encoded.txt": b"changed utf8\n", "added.bin": bytes(range(256))}
        fixture = Path(__file__).with_name("fixture_executor.py")
        wrapper = self.root / "verify-input.py"
        wrapper.write_text("import pathlib,subprocess,sys\n"
                           f"expected={initial!r}\n"
                           "for name,content in expected.items():\n"
                           " assert pathlib.Path(name).read_bytes()==content, 'verified input bytes changed'\n"
                           f"changed={changed!r}\n"
                           "for name,content in changed.items(): pathlib.Path(name).write_bytes(content)\n"
                           "pathlib.Path('added.bin').chmod(0o755)\n"
                           "pathlib.Path('new.txt').unlink()\n"
                           "pathlib.Path('.env').write_text('synthetic excluded content')\n"
                           f"subprocess.run([sys.executable,{str(fixture)!r},*sys.argv[1:]],check=True)\n")
        self.config.opencode_command = [sys.executable, str(wrapper)]
        self.payload.update(snapshot=self.receipt, result_paths=["added.bin", "result.txt"])
        runtime = Runtime(self.control)
        self.addCleanup(runtime.stop)
        runtime.start()
        # Treat the start reply as lost while the ordinary scheduler executes.
        self.receiver.start(self.payload, self.bundle)
        task_id = self.receiver.lookup(self.selection)["task_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            task = self.control.store.get(task_id)
            if task["status"] in {"awaiting_review", "failed", "interrupted"}:
                break
            time.sleep(.04)
        self.assertEqual(task["status"], "awaiting_review", task)
        child = Path(task["worktree"])
        self.assertEqual((child / "code.txt").read_bytes(), changed["code.txt"])
        self.assertEqual((child / "executable.sh").stat().st_mode & 0o777, 0o755)
        native = json.loads((child / "session.json").read_text())
        self.assertEqual(native["sessions"], [task["child_session_id"]])
        self.assertEqual(task["attempts"], 1)
        self.assertEqual(task["snapshot_commit"], self.receipt["snapshot_commit"])
        for name, content in changed.items():
            self.assertEqual(git(self.target, "show", f"{task['final_tree']}:{name}", binary=True), content)
        final_paths = git(self.target, "ls-tree", "-r", "--name-only", task["final_tree"]).splitlines()
        self.assertNotIn("new.txt", final_paths)
        self.assertNotIn(".env", final_paths)
        self.assertNotIn("session.json", final_paths)
        self.assertTrue(git(self.target, "ls-tree", task["final_tree"], "added.bin").startswith("100755 "))
        self.assertFalse(smudge_marker.exists())
        self.assertFalse(clean_marker.exists())
        runtime.stop()
        restarted = Controller(self.config, {}, self.state)
        receiver = RemoteReceiver(restarted, Runtime(restarted).validate)
        self.assertEqual(receiver.lookup(self.selection)["task_id"], task_id)
        self.assertEqual(receiver.start(self.payload, self.bundle)["task_id"], task_id)
        self.assertEqual(json.loads((child / "session.json").read_text())["sessions"], native["sessions"])

    def test_binary_result_budget_covers_original_and_changed_content(self):
        names = [f"binary-{index}.bin" for index in range(3)]
        for name in names:
            (self.origin / name).write_bytes(hashlib.shake_256(("before-" + name).encode()).digest(remote_snapshot.MAX_FILE_BYTES))
        receipt = remote_snapshot.export_snapshot(self.origin, "binary-result", self.root / "binary-export", names)
        task_id = self.receiver.start({**self.payload, "snapshot": receipt}, self.root / "binary-export" / "snapshot.bundle")["task_id"]
        task = self.control.store.get(task_id)
        directory = self.root / "binary-artifacts"
        source, destination = self.root / "binary.input", self.root / "binary.result"
        task.update(artifact_dir=str(directory), attempt_number=1)
        source.write_text(json.dumps(task))
        with patch("sys.argv", ["snapshot.py", "prepare", str(source), str(destination)]):
            snapshot_main()
        task.update(json.loads(destination.read_text()))
        expected = {name: hashlib.shake_256(("after-" + name).encode()).digest(remote_snapshot.MAX_FILE_BYTES) for name in names}
        for name, content in expected.items():
            (Path(task["worktree"]) / name).write_bytes(content)
        source.write_text(json.dumps(task))
        with patch("sys.argv", ["snapshot.py", "capture", str(source), str(destination)]):
            snapshot_main()
        result = json.loads(destination.read_text())
        self.assertGreater(Path(result["patch"]).stat().st_size, remote_snapshot.MAX_BUNDLE_BYTES)
        self.assertLessEqual(Path(result["patch"]).stat().st_size, remote_snapshot.MAX_PATCH_BYTES)
        for name, content in expected.items():
            self.assertEqual(git(self.target, "show", f"{result['final_tree']}:{name}", binary=True), content)

    def test_internal_request_scope_is_not_published_as_a_native_parent(self):
        receipt = self.receiver.start(self.payload, self.bundle)
        task_id = receipt["task_id"]
        task = self.control.store.update_with_attempt(task_id, lambda task: task.update(
            status="awaiting_review", attempt_number=1, attempts=1, child_session_id="receiving-native-child"))
        internal = task["session_id"]
        views = [self.control.handle("status", {"task_id": task_id}), self.control.handle("status", {}), compose(self.control),
                 self.control.handle("correlation", {"limit": 100})]
        self.assertNotIn(internal, json.dumps(views))
        for view in (views[0], views[1][0]):
            self.assertIsNone(view["parent_session_id"])
            self.assertIsNone(view["parent_agent_id"])
            self.assertEqual(view["remote_origin"], {"origin_host_id": "origin-host", "origin_session_id": "native-parent",
                                                    "request_id": "start-one", "parent_agent_id": "worker-a"})
        for row in views[2]["router_tasks"]:
            self.assertIsNone(row["parent_session_id"])
        records = [row for row in views[3]["records"] if row["kind"] in {"router_task", "attempt"}]
        self.assertEqual({row["kind"] for row in records}, {"router_task", "attempt"})
        for row in records:
            self.assertIsNone(row["parent_session_id"])
            self.assertIsNone(row["parent_runtime"])
            self.assertIsNone(row["parent_agent_id"])

    def test_capture_refuses_undeclared_ignored_and_unsafe_outputs(self):
        receipt = remote_snapshot.export_snapshot(self.origin, "capture-snapshot", self.root / "capture-export",
                                                   [".gitignore", "code.txt", "new.txt"])
        bundle = self.root / "capture-export" / "snapshot.bundle"
        global_excludes, environment_excludes = self.root / "global excludes", self.root / "environment excludes"
        global_excludes.write_text("global-output.bin\n")
        environment_excludes.write_text("environment-output.bin\n")
        git(self.target, "config", "--file", str(self.global_config), "core.excludesFile", str(global_excludes))
        environment = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.excludesFile",
                       "GIT_CONFIG_VALUE_0": str(environment_excludes)}
        cases = (("undeclared", [], None, {}), ("ignored", [".env"], None, {}),
                 ("symlink", ["output.bin"], None, {}),
                 ("global-selected", ["global-output.bin"], "global-output.bin", {}),
                 ("global-unselected", [], "global-output.bin", {}),
                 ("environment-selected", ["environment-output.bin"], "environment-output.bin", environment),
                 ("environment-unselected", [], "environment-output.bin", environment))
        for kind, additional, excluded, configuration in cases:
            with self.subTest(kind=kind), patch.dict(os.environ, configuration):
                payload = {**self.payload, "request_id": "capture-" + kind, "snapshot": receipt}
                if additional:
                    payload["result_paths"] = additional
                task_id = self.receiver.start(payload, bundle)["task_id"]
                task = self.control.store.get(task_id)
                directory = self.root / ("capture-" + kind)
                source, destination = self.root / (kind + ".input"), self.root / (kind + ".result")
                task.update(artifact_dir=str(directory), attempt_number=1)
                source.write_text(json.dumps(task))
                with patch("sys.argv", ["snapshot.py", "prepare", str(source), str(destination)]):
                    snapshot_main()
                task.update(json.loads(destination.read_text()))
                destination.unlink()
                child = Path(task["worktree"])
                if kind == "undeclared":
                    (child / "unexpected.txt").write_text("synthetic undeclared output")
                elif kind == "ignored":
                    (child / ".env").write_text("synthetic excluded content")
                elif excluded:
                    (child / excluded).write_text("synthetic excluded output")
                else:
                    outside = self.root / "outside.bin"
                    outside.write_bytes(b"synthetic outside content")
                    (child / "output.bin").symlink_to(outside)
                source.write_text(json.dumps(task))
                with patch("sys.argv", ["snapshot.py", "capture", str(source), str(destination)]):
                    if excluded and not additional:
                        snapshot_main()
                        final = json.loads(destination.read_text())
                        self.assertEqual(git(self.target, "ls-tree", "-r", "--name-only", final["final_tree"]).splitlines(),
                                         receipt["paths"])
                    else:
                        with self.assertRaises(ValueError):
                            snapshot_main()
                        self.assertFalse(destination.exists())
                        self.assertFalse((directory / "attempt-1.patch").exists())

    def test_native_grants_cannot_take_remote_ownership_even_with_the_internal_scope(self):
        receipt = self.receiver.start(self.payload, self.bundle)
        task_id = receipt["task_id"]
        remote = self.control.store.update(task_id, lambda task: task.update(
            status="awaiting_review", attempts=1, final_tree=self.receipt["snapshot_tree"]))
        scope = remote["session_id"]
        instruction = self.control.handle("submit", {"session_id": scope, "prompt_id": "prompt-one"})["instruction_id"]
        review = {"task_id": task_id, "request_id": "native-review", "expected_attempt": 1, "outcome": "accepted"}
        with self.assertRaises(ValueError):
            self.control.handle("authorize", {"session_id": scope, "prompt_id": "prompt-one", "operation": "review", "invocation": review})
        # Even a retained native grant from an older implementation cannot acquire ownership.
        with self.control.store.transaction() as db:
            db.execute("INSERT INTO authorizations(session_id,request_id,operation,payload,instruction_id) VALUES(?,?,?,?,?)",
                       (scope, review["request_id"], "review", canonical(review), instruction))
        with self.assertRaises(ValueError):
            self.control.handle("review", review)
        self.assertEqual(self.control.store.get(task_id)["status"], "awaiting_review")
        local = {"request_version": "1", "request_id": "native-start", "instruction_id": instruction,
                 "executor": "opencode", "model": "test", "task": "Local work", "parent_repo": str(self.target),
                 "parent_worktree": str(self.target), "mode": "write"}
        self.control.handle("authorize", {"session_id": scope, "prompt_id": "prompt-one", "operation": "start", "invocation": local})
        local_task = self.control.handle("start", local)
        self.assertEqual(local_task["parent_session_id"], scope)
        self.assertNotIn("remote_origin", local_task)
        correlation = self.control.handle("correlation", {"limit": 100})
        self.assertFalse(correlation["partial"])
        rows = {row["router_task_id"]: row for row in correlation["records"] if row["kind"] == "router_task"}
        self.assertIsNone(rows[task_id]["parent_session_id"])
        self.assertEqual(rows[local_task["task_id"]]["parent_session_id"], scope)


if __name__ == "__main__":
    unittest.main()
